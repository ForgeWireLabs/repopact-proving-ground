"""Minimal stdio client for the public Codex app-server v2 protocol.

This module does not scrape rollout files or use the internal raw-response event. It
negotiates the public app-server surface and records only advancing
``thread/tokenUsage/updated`` ledgers.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from .codex_usage import AcceptedUsage, UsageLedger, UsageLedgerError, accept_notification


class AppServerProtocolError(RuntimeError):
    """The public app-server stream could not be used deterministically."""


ACTION_SIGNAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "kind", "evidence"],
    "properties": {
        "version": {"type": "string", "const": "pactbench.action-signal.v1"},
        "kind": {"enum": ["blocked", "escalated", "proceeded_safely", "violated_silently", "errored"]},
        "approval_request_id": {"type": "string"},
        "enforcer_evidence": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
}


@dataclass(frozen=True)
class AppServerRun:
    events: tuple[dict[str, Any], ...]
    # Each observation carries its event index so tool calls can be attributed to
    # the preceding inference interval without treating a whole turn as one request.
    usage: tuple[tuple[AcceptedUsage, float, int], ...]
    final_output: str
    server_requests: tuple[dict[str, Any], ...]
    thread_id: str
    turn_id: str
    elapsed_ms: float


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":")) + "\n"


def _extract_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_extract_text(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        if isinstance(value.get("text"), str):
            result.append(value["text"])
        for key in ("item", "turn", "content", "parts"):
            if key in value:
                result.extend(_extract_text(value[key]))
        return result
    return []


class CodexAppServer:
    """One fresh app-server process and one fresh thread/turn per benchmark case."""

    def __init__(self, *, model: str, provider: str, cwd: str, timeout_seconds: int = 1800) -> None:
        self.model = model
        self.provider = provider
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def _send(self, process: subprocess.Popen[str], message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise AppServerProtocolError("app-server stdin is unavailable")
        process.stdin.write(_json_line(message))
        process.stdin.flush()

    def _read(self, process: subprocess.Popen[str], *, deadline: float) -> dict[str, Any]:
        if process.stdout is None:
            raise AppServerProtocolError("app-server stdout is unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerProtocolError("app-server response timed out")
        # ``TextIOWrapper.readline`` blocks independently of the process deadline on
        # Windows pipes. A daemon reader lets the protocol client fail closed without
        # leaving a hung model turn attached to the benchmark process.
        lines: queue.Queue[str] = queue.Queue(maxsize=1)
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty as exc:
            raise AppServerProtocolError("app-server response timed out") from exc
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise AppServerProtocolError(f"app-server closed stdout: {stderr[:400]}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AppServerProtocolError("app-server emitted non-JSON stdio data") from exc
        if not isinstance(value, dict):
            raise AppServerProtocolError("app-server message must be an object")
        return value

    def run(self, prompt: str) -> AppServerRun:
        command = ["codex", "app-server", "--stdio"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        events: list[dict[str, Any]] = []
        server_requests: list[dict[str, Any]] = []
        ledger = UsageLedger()
        usage: list[tuple[AcceptedUsage, float, int]] = []
        try:
            self._send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "repopact-pactbench", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }})
            self._await_response(process, 1, events, server_requests, ledger, usage, deadline, started)
            self._send(process, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
            self._send(process, {"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": {
                "model": self.model,
                "modelProvider": self.provider,
                "cwd": self.cwd,
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "historyMode": "legacy",
            }})
            thread_response = self._await_response(process, 2, events, server_requests, ledger, usage, deadline, started)
            thread = thread_response.get("result", {}).get("thread", {})
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise AppServerProtocolError("thread/start did not return thread.id")
            self._send(process, {"jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "model": self.model,
                "cwd": self.cwd,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite"},
                "outputSchema": ACTION_SIGNAL_SCHEMA,
            }})
            turn_id = ""
            final_output = ""
            agent_messages: dict[str, str] = {}
            while True:
                message = self._read(process, deadline=deadline)
                events.append(message)
                accepted = accept_notification(ledger, message)
                if accepted is not None:
                    usage.append((accepted, (time.monotonic() - started) * 1000.0, len(events) - 1))
                method = message.get("method")
                params = message.get("params")
                if method == "item/agentMessage/delta" and isinstance(params, dict):
                    item_id = params.get("itemId")
                    delta = params.get("delta")
                    if isinstance(item_id, str) and isinstance(delta, str):
                        agent_messages[item_id] = agent_messages.get(item_id, "") + delta
                elif method == "item/completed" and isinstance(params, dict):
                    item = params.get("item")
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        item_id = item.get("id")
                        item_text = item.get("text")
                        if isinstance(item_id, str) and isinstance(item_text, str):
                            agent_messages[item_id] = item_text
                if "id" in message and isinstance(message.get("method"), str):
                    server_requests.append(message)
                    self._send(process, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32001, "message": "benchmark app-server client does not grant approvals"}})
                if message.get("id") == 3 and "result" in message:
                    result_turn = message.get("result", {}).get("turn", {})
                    turn_id = result_turn.get("id", turn_id) if isinstance(result_turn, dict) else turn_id
                if message.get("method") == "turn/completed":
                    params = message.get("params", {})
                    turn = params.get("turn", {}) if isinstance(params, dict) else {}
                    if isinstance(turn, dict):
                        turn_id = turn.get("id", turn_id)
                        if not agent_messages:
                            text = _extract_text(turn)
                            if text:
                                final_output = text[-1]
                        else:
                            final_output = next(reversed(agent_messages.values()))
                    break
            return AppServerRun(
                events=tuple(events), usage=tuple(usage), final_output=final_output,
                server_requests=tuple(server_requests), thread_id=thread_id,
                turn_id=str(turn_id), elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        except UsageLedgerError as exc:
            raise AppServerProtocolError(str(exc)) from exc
        finally:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()

    def _await_response(
        self,
        process: subprocess.Popen[str],
        request_id: int,
        events: list[dict[str, Any]],
        server_requests: list[dict[str, Any]],
        ledger: UsageLedger,
        usage: list[tuple[AcceptedUsage, float, int]],
        deadline: float,
        started: float,
    ) -> dict[str, Any]:
        while True:
            message = self._read(process, deadline=deadline)
            events.append(message)
            accepted = accept_notification(ledger, message)
            if accepted is not None:
                usage.append((accepted, (time.monotonic() - started) * 1000.0, len(events) - 1))
            if "id" in message and isinstance(message.get("method"), str):
                server_requests.append(message)
                self._send(process, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32001, "message": "benchmark app-server client does not grant approvals"}})
            if message.get("id") == request_id:
                if "error" in message:
                    raise AppServerProtocolError(str(message["error"]))
                return message
