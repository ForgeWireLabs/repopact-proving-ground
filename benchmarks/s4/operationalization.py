"""Frozen, local implementations of the runnable S4 context regimes.

This module is deliberately deterministic and provider-neutral.  It renders the
exact prompt/context payload for an empirical adapter; it never calls a model,
embedding API, memory service, or RepoPact command.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .driver import REGISTERED_CONDITIONS, validate_condition


S4_OPERATIONALIZATION_VERSION = "2026-09-14.s4-methods.1"
S4_CONDITION_REGISTRY_VERSION = "2026-09-13.s4.1"
CONTEXT_CHAR_LIMIT = 120_000
SUMMARY_CHAR_LIMIT = 8_000
RETRIEVAL_TOP_K = 8
EMBEDDING_DIMENSION = 256
_EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache"}
_REPOPACT_RECORDS = (
    "AGENTS.md",
    "governance/invariants.json",
    "governance/owners.json",
    "governance/frozen-surface.json",
)
_TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".html", ".ini", ".java", ".js",
    ".json", ".md", ".py", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}


def condition_implementation_fingerprint(condition: str) -> str:
    """Fingerprint the frozen algorithm/config, independent of a task corpus."""
    basis = {
        "version": S4_OPERATIONALIZATION_VERSION,
        "condition": condition,
        "char_limit": CONTEXT_CHAR_LIMIT,
        "summary_char_limit": SUMMARY_CHAR_LIMIT,
        "retrieval_top_k": RETRIEVAL_TOP_K,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "embedding": "sha256-token-bucket-v1",
        "memory": "sqlite3:memory",
        "tools": ["list_files", "read_file", "search_text"],
        "records": list(_REPOPACT_RECORDS),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class OperationalizationError(ValueError):
    """A frozen context regime cannot be rendered without an assumption."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]+", value.lower())


def _task_text(task_payload: dict[str, Any] | str) -> str:
    if isinstance(task_payload, str):
        return _normalize(task_payload)
    if not isinstance(task_payload, dict):
        raise OperationalizationError("S4 task payload must be an object or string")
    prompt = task_payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return _normalize(prompt)
    return json.dumps(task_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _files(root: Path, *, include: Any = None) -> list[tuple[str, str]]:
    if not root.is_dir():
        raise OperationalizationError(f"context source root is not a directory: {root}")
    result: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or any(part in _EXCLUDED_PARTS for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if include is not None and not include(relative):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES and path.name != "AGENTS.md":
            continue
        try:
            text = _normalize(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        result.append((relative, text))
    return result


def _format_files(files: list[tuple[str, str]], *, limit: int = CONTEXT_CHAR_LIMIT) -> tuple[str, bool]:
    pieces: list[str] = []
    used = 0
    truncated = False
    for relative, content in files:
        piece = f"\n--- {relative} ---\n{content}\n"
        remaining = limit - used
        if remaining <= 0:
            truncated = True
            break
        if len(piece) > remaining:
            pieces.append(piece[:remaining])
            truncated = True
            break
        pieces.append(piece)
        used += len(piece)
    payload = "".join(pieces)
    if truncated:
        payload = payload[:limit]
    return payload, truncated


def _file_fingerprint(files: list[tuple[str, str]]) -> str:
    return _sha256(json.dumps(files, ensure_ascii=False, separators=(",", ":")))


def _hashed_embedding(value: str) -> dict[int, int]:
    vector: dict[int, int] = {}
    for token in _tokens(value):
        index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big") % EMBEDDING_DIMENSION
        vector[index] = vector.get(index, 0) + 1
    return vector


def _cosine(left: dict[int, int], right: dict[int, int]) -> float:
    numerator = sum(value * right.get(index, 0) for index, value in left.items())
    denominator = math.sqrt(sum(value * value for value in left.values())) * math.sqrt(sum(value * value for value in right.values()))
    return numerator / denominator if denominator else 0.0


def _rag_files(task: str, files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    query = _hashed_embedding(task)
    ranked = sorted(
        ((_cosine(query, _hashed_embedding(content)), relative, content) for relative, content in files),
        key=lambda row: (-row[0], row[1]),
    )
    selected = [row for row in ranked if row[0] > 0][:RETRIEVAL_TOP_K]
    if not selected:
        selected = ranked[:RETRIEVAL_TOP_K]
    return [(relative, content) for _, relative, content in selected]


def _summary(task: str, files: list[tuple[str, str]]) -> str:
    query = set(_tokens(task))
    lines: list[tuple[int, str, int, int]] = []
    for relative, content in files:
        for number, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            words = _tokens(line)
            overlap = sum(word in query for word in words)
            lines.append((-overlap, relative, number, line))
    lines.sort()
    selected = lines[: max(1, min(64, len(lines)))]
    selected.sort(key=lambda row: (row[1], row[2]))
    summary = "\n".join(f"{relative}:{number}: {line}" for _, relative, number, line in selected)
    return summary[:SUMMARY_CHAR_LIMIT]


class SQLiteMemoryStore:
    """Small local memory store used by C5; reset means a new in-memory DB."""

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE memory (path TEXT PRIMARY KEY, content TEXT NOT NULL)")

    def load(self, files: list[tuple[str, str]]) -> None:
        self.connection.executemany("INSERT INTO memory(path, content) VALUES (?, ?)", files)
        self.connection.commit()

    def retrieve(self, task: str) -> list[tuple[str, str]]:
        rows = list(self.connection.execute("SELECT path, content FROM memory ORDER BY path"))
        return _rag_files(task, [(str(path), str(content)) for path, content in rows])

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class RenderedContext:
    condition: str
    payload: str
    fingerprint: str
    allowed_sources: tuple[str, ...]
    dependencies: dict[str, Any]
    auxiliary_calls: tuple[dict[str, Any], ...]
    reset_policy: str
    metadata: dict[str, Any]


def _rendered(condition: str, payload: str, sources: list[str], deps: dict[str, Any], *, reset: str, metadata: dict[str, Any] | None = None) -> RenderedContext:
    normalized = _normalize(payload)
    return RenderedContext(
        condition=condition,
        payload=normalized,
        fingerprint=_sha256(normalized),
        allowed_sources=tuple(sources),
        dependencies={"operationalization_version": S4_OPERATIONALIZATION_VERSION, **deps},
        auxiliary_calls=(),
        reset_policy=reset,
        metadata=metadata or {},
    )


def render_condition(
    condition: str,
    task_payload: dict[str, Any] | str,
    source_root: str | Path,
    *,
    repopact_root: str | Path | None = None,
) -> RenderedContext:
    """Render one runnable C0-C8 condition; C9 remains explicitly out of scope."""
    validate_condition(condition)
    source = Path(source_root)
    task = _task_text(task_payload)
    all_files = _files(source)
    if condition == "C0":
        return _rendered(condition, task, ["task"], {"source": "task-only"}, reset="per-task")
    if condition == "C1":
        payload, truncated = _format_files(all_files)
        return _rendered(condition, task + "\n\nCONTEXT\n" + payload, [path for path, _ in all_files], {
            "source": "full-prompt-stuffing", "char_limit": CONTEXT_CHAR_LIMIT, "truncated": truncated,
        }, reset="per-task")
    if condition == "C2":
        conventions = [(path, content) for path, content in all_files if Path(path).name == "AGENTS.md"]
        payload, truncated = _format_files(conventions)
        return _rendered(condition, task + "\n\nCONVENTIONS\n" + payload, [path for path, _ in conventions], {
            "source": "AGENTS.md-only", "char_limit": CONTEXT_CHAR_LIMIT, "truncated": truncated,
        }, reset="per-task")
    if condition == "C3":
        selected = _rag_files(task, all_files)
        payload, truncated = _format_files(selected)
        return _rendered(condition, task + "\n\nRETRIEVED\n" + payload, [path for path, _ in selected], {
            "source": "local-hashed-vector-retrieval", "embedding": "sha256-token-bucket-v1",
            "dimension": EMBEDDING_DIMENSION, "top_k": RETRIEVAL_TOP_K, "char_limit": CONTEXT_CHAR_LIMIT, "truncated": truncated,
        }, reset="per-task")
    if condition == "C2+C3":
        c2 = render_condition("C2", task, source, repopact_root=repopact_root)
        c3 = render_condition("C3", task, source, repopact_root=repopact_root)
        return _rendered(condition, c2.payload + "\n\n=== C2+C3 COMPOSITION ===\n" + c3.payload, list(dict.fromkeys(c2.allowed_sources + c3.allowed_sources)), {
            "source": "C2-then-C3", "components": [c2.fingerprint, c3.fingerprint],
        }, reset="per-task")
    if condition == "C4":
        summary = _summary(task, all_files)
        return _rendered(condition, task + "\n\nROLLING SUMMARY\n" + summary, [path for path, _ in all_files], {
            "source": "deterministic-extractive-local", "summary_char_limit": SUMMARY_CHAR_LIMIT,
        }, reset="per-task-and-summary-window")
    if condition == "C5":
        store = SQLiteMemoryStore()
        try:
            store.load(all_files)
            selected = store.retrieve(task)
        finally:
            store.close()
        payload, truncated = _format_files(selected)
        return _rendered(condition, task + "\n\nMEMORY RETRIEVAL\n" + payload, [path for path, _ in selected], {
            "source": "local-sqlite-memory", "sqlite_backend": "sqlite3:memory", "top_k": RETRIEVAL_TOP_K,
            "char_limit": CONTEXT_CHAR_LIMIT, "truncated": truncated,
        }, reset="new-in-memory-store-per-task")
    if condition == "C6":
        tools = [
            {"name": "list_files", "access": "read-only", "arguments": ["glob"]},
            {"name": "read_file", "access": "read-only", "arguments": ["path"]},
            {"name": "search_text", "access": "read-only", "arguments": ["query", "glob"]},
        ]
        return _rendered(condition, task, ["task"], {"source": "on-demand-read-only-tools", "tools": tools}, reset="per-task")
    if condition in {"C7", "C8"}:
        if repopact_root is None:
            raise OperationalizationError(f"{condition} requires a RepoPact records root")
        records_root = Path(repopact_root)
        record_files: list[tuple[str, str]] = []
        for relative in _REPOPACT_RECORDS:
            path = records_root / relative
            if not path.is_file():
                raise OperationalizationError(f"missing required RepoPact record: {relative}")
            record_files.append((relative, _normalize(path.read_text(encoding="utf-8"))))
        active = records_root / "work" / "active"
        work_items = sorted(active.glob("*/work-item.json"), key=lambda p: p.as_posix()) if active.is_dir() else []
        if not work_items:
            raise OperationalizationError("C7 requires an active RepoPact work-item.json")
        work_item = work_items[0]
        readme = work_item.with_name("README.md")
        if not readme.is_file():
            raise OperationalizationError("C7 requires the selected active work-item README.md")
        record_files.extend([
            (work_item.relative_to(records_root).as_posix(), _normalize(work_item.read_text(encoding="utf-8"))),
            (readme.relative_to(records_root).as_posix(), _normalize(readme.read_text(encoding="utf-8"))),
        ])
        c7_payload, _ = _format_files(record_files)
        if condition == "C7":
            return _rendered(condition, task + "\n\nREPOPact RECORDS\n" + c7_payload, [path for path, _ in record_files], {
                "source": "exact-repopact-records", "record_order": [path for path, _ in record_files],
            }, reset="per-task")
        selected = _rag_files(task, all_files)
        rag_payload, truncated = _format_files(selected)
        return _rendered(condition, task + "\n\nREPOPact RECORDS\n" + c7_payload + "\n\n=== C7+C3 COMPOSITION ===\n" + rag_payload, [path for path, _ in record_files] + [path for path, _ in selected], {
            "source": "RepoPact-records-plus-local-rag", "record_order": [path for path, _ in record_files],
            "rag_component": _file_fingerprint(selected), "char_limit": CONTEXT_CHAR_LIMIT, "truncated": truncated,
        }, reset="per-task")
    raise OperationalizationError(f"unsupported S4 condition: {condition}")


def condition_fingerprints(
    task_payload: dict[str, Any] | str,
    source_root: str | Path,
    *,
    repopact_root: str | Path | None = None,
) -> dict[str, str]:
    return {
        condition: render_condition(condition, task_payload, source_root, repopact_root=repopact_root).fingerprint
        for condition in REGISTERED_CONDITIONS
        if condition != "C9"
    }
