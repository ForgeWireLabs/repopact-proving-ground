"""Minimal stdio client for the public Codex app-server v2 protocol.

This module does not scrape rollout files or use the internal raw-response event. It
negotiates the public app-server surface and records only advancing
``thread/tokenUsage/updated`` ledgers.
"""
from __future__ import annotations

import json
import ctypes
import os
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
    "required": ["version", "kind", "evidence", "approval_request_id", "enforcer_evidence"],
    "properties": {
        "version": {"type": "string", "const": "pactbench.action-signal.v1"},
        "kind": {"type": "string", "enum": ["blocked", "escalated", "proceeded_safely", "violated_silently", "errored"]},
        "approval_request_id": {"type": ["string", "null"]},
        "enforcer_evidence": {"type": ["string", "null"]},
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
    # Public responses are retained for empirical provenance.  Defaults preserve
    # the constructor contract used by the historical v2 smoke tests/captures.
    initialize_result: dict[str, Any] | None = None
    thread_result: dict[str, Any] | None = None
    turn_completed_elapsed_ms: float | None = None
    process_lifecycle: dict[str, Any] | None = None


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


def _public_command_process_ids(events: list[dict[str, Any]]) -> list[int]:
    values: set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            candidate = value.get("processId")
            if isinstance(candidate, int) and candidate > 0:
                values.add(candidate)
            elif isinstance(candidate, str) and candidate.isdigit() and int(candidate) > 0:
                values.add(int(candidate))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(events)
    return sorted(values)


def _query_owned_command_processes(process_ids: list[int]) -> list[dict[str, Any]]:
    """Query only process IDs exposed by this turn's public command events."""
    if os.name != "nt":
        result: list[dict[str, Any]] = []
        for pid in process_ids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                result.append({"pid": pid, "status": "exited"})
            except PermissionError as exc:
                result.append({"pid": pid, "status": "unknown", "errno": getattr(exc, "errno", None)})
            else:
                result.append({"pid": pid, "status": "running"})
        return result
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    get_exit_code.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    result = []
    for pid in process_ids:
        handle = open_process(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            result.append({"pid": pid, "status": "exited" if error in {87, 1168} else "unknown", "winerror": error})
            continue
        exit_code = ctypes.c_uint32()
        try:
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                result.append({"pid": pid, "status": "unknown", "winerror": ctypes.get_last_error()})
            else:
                result.append({"pid": pid, "status": "running" if exit_code.value == 259 else "exited", "exit_code": exit_code.value})
        finally:
            close_handle(handle)
    return result


class CodexAppServer:
    """One fresh app-server process and one fresh thread/turn per benchmark case."""

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        cwd: str,
        timeout_seconds: int = 1800,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        # The default is intentionally the historical AC-5 schema.  New studies
        # opt into a study-specific strict schema without changing that contract.
        self.output_schema = output_schema or ACTION_SIGNAL_SCHEMA

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

    def run(self, prompt: str, *, output_schema: dict[str, Any] | None = None) -> AppServerRun:
        command = ["codex", "app-server", "--stdio"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        events: list[dict[str, Any]] = []
        server_requests: list[dict[str, Any]] = []
        ledger = UsageLedger()
        usage: list[tuple[AcceptedUsage, float, int]] = []
        turn_completed_elapsed_ms: float | None = None
        lifecycle: dict[str, Any] = {
            "owned_process_pid": process.pid,
            "owned_process_command": command,
            "turn_completed_observed": False,
            "public_command_process_ids": [],
        }
        try:
            self._send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "repopact-pactbench", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }})
            initialize_response = self._await_response(process, 1, events, server_requests, ledger, usage, deadline, started)
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
                "outputSchema": output_schema or self.output_schema,
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
                    turn_completed_elapsed_ms = (time.monotonic() - started) * 1000.0
                    lifecycle["turn_completed_observed"] = True
                    break
            return AppServerRun(
                events=tuple(events), usage=tuple(usage), final_output=final_output,
                server_requests=tuple(server_requests), thread_id=thread_id,
                turn_id=str(turn_id), elapsed_ms=(time.monotonic() - started) * 1000.0,
                initialize_result=initialize_response,
                thread_result=thread_response,
                turn_completed_elapsed_ms=turn_completed_elapsed_ms,
                process_lifecycle=lifecycle,
            )
        except UsageLedgerError as exc:
            raise AppServerProtocolError(str(exc)) from exc
        finally:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            termination_method = "already_exited"
            try:
                if process.poll() is None:
                    process.terminate()
                    termination_method = "terminate"
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                termination_method = "kill_owned_process"
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            except OSError:
                if process.poll() is None:
                    raise
            lifecycle.update({
                "public_command_process_ids": _public_command_process_ids(events),
                "termination_method": termination_method,
                "returncode": process.poll(),
                "process_terminated": process.poll() is not None,
                "accepted_usage_count": len(usage),
                "final_usage_reconciled": bool(usage),
                "turn_completed_elapsed_ms": turn_completed_elapsed_ms,
            })
            process_status = _query_owned_command_processes(lifecycle["public_command_process_ids"])
            lifecycle["public_command_process_status"] = process_status
            lifecycle["public_command_processes_terminated"] = all(item["status"] == "exited" for item in process_status)

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
