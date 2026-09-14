"""Bounded, workspace-confined host reads for post-turn objective evaluation."""
from __future__ import annotations

import errno
import ctypes
import math
import os
import platform
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_DEADLINE_SECONDS = 2.0
DEFAULT_RETRY_INTERVAL_SECONDS = 0.05
TRANSIENT_WINDOWS_ERRORS = frozenset({32, 33})  # sharing violation, lock violation
_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class QuiescentRead:
    """Exact bytes plus structured evidence for one bounded host read."""

    content: bytes
    evidence: dict[str, Any]


class WorkspaceIOError(RuntimeError):
    """A workspace read failed closed with diagnostic evidence."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _exception_fields(exc: BaseException, path: Path) -> dict[str, Any]:
    return {
        "exception_class": type(exc).__name__,
        "errno": getattr(exc, "errno", None),
        "winerror": getattr(exc, "winerror", None),
        "filename": str(getattr(exc, "filename", None) or path),
        "message": str(exc),
    }


def _stat_metadata(path: Path) -> dict[str, Any] | None:
    try:
        value = os.lstat(path)
    except OSError as exc:
        return {"error": _exception_fields(exc, path)}
    return {
        "mode": value.st_mode,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "inode": value.st_ino,
        "device": value.st_dev,
        "file_attributes": getattr(value, "st_file_attributes", None),
    }


def _path_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"filename": str(path)}
    try:
        state["exists"] = path.exists()
    except OSError as exc:
        state["exists"] = None
        state["exists_error"] = _exception_fields(exc, path)
    try:
        state["is_file"] = path.is_file()
    except OSError as exc:
        state["is_file"] = None
        state["is_file_error"] = _exception_fields(exc, path)
    try:
        state["is_symlink"] = path.is_symlink()
    except OSError as exc:
        state["is_symlink"] = None
        state["is_symlink_error"] = _exception_fields(exc, path)
    state["stat"] = _stat_metadata(path)
    state["parent_stat"] = _stat_metadata(path.parent)
    return state


def _platform_state() -> dict[str, Any]:
    return {
        "os_name": os.name,
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "pid": os.getpid(),
        "executable": sys.executable,
    }


def _native_windows_error(path: Path, exc: BaseException) -> int | None:
    """Recover the Windows sharing/access code when Python omitted winerror."""
    if os.name != "nt" or not isinstance(exc, PermissionError) or getattr(exc, "winerror", None) is not None:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle != invalid:
            close_handle(handle)
            return 0
        return ctypes.get_last_error()
    except (AttributeError, OSError):
        return None


def _effective_winerror(path: Path, exc: BaseException) -> int | None:
    return getattr(exc, "winerror", None) or _native_windows_error(path, exc)


def _classify_os_error(path: Path, exc: BaseException, state: dict[str, Any]) -> str:
    winerror = _effective_winerror(path, exc)
    if winerror in TRANSIENT_WINDOWS_ERRORS:
        return "transient_sharing_or_lock"
    if isinstance(exc, FileNotFoundError) or state.get("exists") is False:
        return "nonexistent"
    if isinstance(exc, IsADirectoryError) or state.get("is_file") is False:
        return "path_type"
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM}:
        return "permanent_access_denied"
    return "filesystem_error"


def filesystem_diagnostic(
    path: str | Path,
    *,
    workspace: str | Path,
    attempt_ordinal: int,
    elapsed_since_turn_completion_ms: float | None,
    error: BaseException | None = None,
    classification: str | None = None,
) -> dict[str, Any]:
    """Capture host facts needed to distinguish filesystem failure classes."""
    target = Path(path)
    state = _path_state(target)
    diagnostic: dict[str, Any] = {
        "attempt_ordinal": attempt_ordinal,
        "elapsed_since_turn_completion_ms": (
            round(elapsed_since_turn_completion_ms, 3)
            if elapsed_since_turn_completion_ms is not None else None
        ),
        "path": state,
        "parent_directory": {"path": str(target.parent), "stat": state.get("parent_stat")},
        "process": _platform_state(),
    }
    if error is not None:
        diagnostic["error"] = _exception_fields(error, target)
        native_winerror = _effective_winerror(target, error)
        if diagnostic["error"]["winerror"] is None and native_winerror is not None:
            diagnostic["error"]["winerror"] = native_winerror
            diagnostic["error"]["winerror_source"] = "native CreateFileW classification probe"
        diagnostic["classification"] = classification or _classify_os_error(target, error, state)
    else:
        diagnostic["classification"] = classification or "unknown"
    return diagnostic


def _path_error(
    message: str,
    path: Path,
    workspace: Path,
    classification: str,
    *,
    started: float,
) -> WorkspaceIOError:
    return WorkspaceIOError(
        message,
        {
            "operation": "read_bytes_after_quiescence",
            "workspace": str(workspace),
            "path": str(path),
            "deadline_seconds": None,
            "retry_interval_seconds": None,
            "attempts_required": 0,
            "transient_errors_encountered": [],
            "elapsed_quiescence_ms": round((time.monotonic() - started) * 1000.0, 3),
            "attempts": [filesystem_diagnostic(
                path,
                workspace=workspace,
                attempt_ordinal=0,
                elapsed_since_turn_completion_ms=None,
                classification=classification,
            )],
            "final_file_metadata": _path_state(path),
        },
    )


def _validate_path(path: str | Path, workspace: str | Path, *, started: float) -> tuple[Path, Path]:
    root_value = Path(workspace)
    try:
        root = root_value.resolve(strict=True)
    except OSError as exc:
        raise _path_error("registered workspace cannot be resolved", root_value, root_value, "workspace_invalid", started=started) from exc
    if not root.is_dir():
        raise _path_error("registered workspace is not a directory", root, root, "workspace_invalid", started=started)
    requested = Path(path)
    candidate = requested if requested.is_absolute() else root / requested
    candidate = Path(os.path.abspath(candidate))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise _path_error("workspace read path escapes registered workspace", candidate, root, "path_escape", started=started) from exc
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if not os.path.lexists(current):
            break
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            diagnostic = filesystem_diagnostic(current, workspace=root, attempt_ordinal=0, elapsed_since_turn_completion_ms=None, error=exc, classification="path_inspection_error")
            raise WorkspaceIOError("workspace path could not be inspected", {"operation": "read_bytes_after_quiescence", "workspace": str(root), "path": str(current), "attempts": [diagnostic]}) from exc
        if stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT):
            raise _path_error("symlink or reparse point is not permitted in workspace read path", candidate, root, "symlink_or_reparse_rejected", started=started)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        diagnostic = filesystem_diagnostic(candidate, workspace=root, attempt_ordinal=0, elapsed_since_turn_completion_ms=None, error=exc, classification="path_resolution_error")
        raise WorkspaceIOError("workspace path could not be resolved", {"operation": "read_bytes_after_quiescence", "workspace": str(root), "path": str(candidate), "attempts": [diagnostic]}) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _path_error("workspace read path resolves outside registered workspace", resolved, root, "path_escape", started=started) from exc
    try:
        if resolved.is_dir():
            raise _path_error("workspace read path is a directory, not a file", resolved, root, "path_type", started=started)
    except OSError as exc:
        diagnostic = filesystem_diagnostic(resolved, workspace=root, attempt_ordinal=0, elapsed_since_turn_completion_ms=None, error=exc, classification="path_inspection_error")
        raise WorkspaceIOError("workspace read path could not be inspected", {"operation": "read_bytes_after_quiescence", "workspace": str(root), "path": str(resolved), "attempts": [diagnostic]}) from exc
    return root, resolved


def read_bytes_after_quiescence(
    path: str | Path,
    *,
    workspace: str | Path,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    turn_completed_monotonic: float | None = None,
    reader: Callable[[Path], bytes] | None = None,
) -> QuiescentRead:
    """Read exact bytes; retry only bounded Windows sharing/lock failures."""
    if deadline_seconds <= 0 or retry_interval_seconds <= 0 or retry_interval_seconds > deadline_seconds:
        raise ValueError("deadline must be positive and retry interval must be within the deadline")
    started = time.monotonic()
    root, resolved = _validate_path(path, workspace, started=started)
    read = reader or (lambda target: target.read_bytes())
    attempts: list[dict[str, Any]] = []
    transient_errors: list[dict[str, Any]] = []
    deadline = started + deadline_seconds
    max_attempts = max(1, math.ceil(deadline_seconds / retry_interval_seconds) + 1)
    for ordinal in range(1, max_attempts + 1):
        try:
            content = read(resolved)
            if not isinstance(content, bytes):
                raise TypeError("workspace reader must return bytes")
            evidence = {
                "operation": "read_bytes_after_quiescence",
                "workspace": str(root),
                "path": str(resolved),
                "deadline_seconds": deadline_seconds,
                "retry_interval_seconds": retry_interval_seconds,
                "attempts_required": ordinal,
                "transient_errors_encountered": transient_errors,
                "elapsed_quiescence_ms": round((time.monotonic() - started) * 1000.0, 3),
                "attempts": attempts,
                "final_file_metadata": _path_state(resolved),
                "content_byte_length": len(content),
            }
            return QuiescentRead(content, evidence)
        except OSError as exc:
            elapsed = (time.monotonic() - (turn_completed_monotonic or started)) * 1000.0
            state = _path_state(resolved)
            classification = _classify_os_error(resolved, exc, state)
            diagnostic = filesystem_diagnostic(
                resolved,
                workspace=root,
                attempt_ordinal=ordinal,
                elapsed_since_turn_completion_ms=elapsed,
                error=exc,
                classification=classification,
            )
            attempts.append(diagnostic)
            if classification != "transient_sharing_or_lock":
                raise WorkspaceIOError(
                    f"workspace read failed closed: {classification}",
                    {
                        "operation": "read_bytes_after_quiescence",
                        "workspace": str(root),
                        "path": str(resolved),
                        "deadline_seconds": deadline_seconds,
                        "retry_interval_seconds": retry_interval_seconds,
                        "attempts_required": ordinal,
                        "transient_errors_encountered": transient_errors,
                        "elapsed_quiescence_ms": round((time.monotonic() - started) * 1000.0, 3),
                        "attempts": attempts,
                        "final_file_metadata": _path_state(resolved),
                    },
                ) from exc
            transient_errors.append(diagnostic)
            if ordinal >= max_attempts or time.monotonic() >= deadline:
                break
            time.sleep(min(retry_interval_seconds, max(0.0, deadline - time.monotonic())))
        except TypeError as exc:
            diagnostic = filesystem_diagnostic(
                resolved,
                workspace=root,
                attempt_ordinal=ordinal,
                elapsed_since_turn_completion_ms=(time.monotonic() - (turn_completed_monotonic or started)) * 1000.0,
                error=exc,
                classification="reader_contract_error",
            )
            raise WorkspaceIOError(
                "workspace reader returned a non-byte value",
                {"operation": "read_bytes_after_quiescence", "workspace": str(root), "path": str(resolved), "attempts": [*attempts, diagnostic]},
            ) from exc
    raise WorkspaceIOError(
        "workspace read deadline expired while waiting for filesystem quiescence",
        {
            "operation": "read_bytes_after_quiescence",
            "workspace": str(root),
            "path": str(resolved),
            "deadline_seconds": deadline_seconds,
            "retry_interval_seconds": retry_interval_seconds,
            "attempts_required": len(attempts),
            "transient_errors_encountered": transient_errors,
            "elapsed_quiescence_ms": round((time.monotonic() - started) * 1000.0, 3),
            "attempts": attempts,
            "final_file_metadata": _path_state(resolved),
        },
    )


def read_text_after_quiescence(
    path: str | Path,
    *,
    workspace: str | Path,
    encoding: str = "utf-8",
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    turn_completed_monotonic: float | None = None,
    reader: Callable[[Path], bytes] | None = None,
) -> QuiescentRead:
    """Read exact encoded text; decoding never normalizes line endings."""
    result = read_bytes_after_quiescence(
        path,
        workspace=workspace,
        deadline_seconds=deadline_seconds,
        retry_interval_seconds=retry_interval_seconds,
        turn_completed_monotonic=turn_completed_monotonic,
        reader=reader,
    )
    try:
        result.evidence["encoding"] = encoding
        result.evidence["content_text_length"] = len(result.content.decode(encoding))
    except UnicodeDecodeError as exc:
        diagnostic = filesystem_diagnostic(
            path,
            workspace=workspace,
            attempt_ordinal=result.evidence["attempts_required"],
            elapsed_since_turn_completion_ms=result.evidence["elapsed_quiescence_ms"],
            error=exc,
            classification="decode_error",
        )
        raise WorkspaceIOError(
            "workspace text read failed closed during strict decoding",
            {**result.evidence, "attempts": [*result.evidence["attempts"], diagnostic], "decode_error": diagnostic},
        ) from exc
    return QuiescentRead(result.content, result.evidence)
