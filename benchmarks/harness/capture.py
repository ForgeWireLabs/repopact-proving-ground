"""Deterministic, secret-aware run capture layout."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .execution import RunEnvelope, require_valid_envelope


CAPTURE_SCHEMA_VERSION = "repopact.run-capture.v1"
_SECRET_PATTERNS = (
    re.compile(r"(?i)[\"']?(api[_-]?key|access[_-]?token|secret|password)[\"']?\s*[:=]\s*[\"']?[^\s,}\"']+"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b"),
)


class CaptureIntegrityError(ValueError):
    """A capture is unsafe or does not preserve its reproducibility metadata."""


def _serialized(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)


def assert_no_secrets(value: Any) -> None:
    text = _serialized(value)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise CaptureIntegrityError("capture contains a secret-like value")


def capture_relative_path(envelope: RunEnvelope) -> Path:
    """Return the stable path for a capture, independent of machine location."""
    safe = lambda value: re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return Path(safe(envelope.study_id)) / safe(envelope.condition) / (
        f"{safe(envelope.case_id)}-r{envelope.repetition}.json"
    )


def write_capture(root: str | Path, envelope: RunEnvelope, *, raw_output: Any = None) -> Path:
    """Write one capture and mark its classification in machine-readable form."""
    require_valid_envelope(envelope, empirical=not envelope.illustrative)
    payload = envelope.to_dict()
    payload["capture_schema_version"] = CAPTURE_SCHEMA_VERSION
    payload["classification"] = "illustrative" if envelope.illustrative else "empirical"
    if raw_output is not None:
        payload["raw_output"] = raw_output
    assert_no_secrets(payload)
    relative = capture_relative_path(envelope)
    destination = Path(root) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    envelope.raw_capture_ref = str(relative).replace("\\", "/")
    payload["raw_capture_ref"] = envelope.raw_capture_ref
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def load_capture(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("capture_schema_version") != CAPTURE_SCHEMA_VERSION:
        raise CaptureIntegrityError("unsupported capture schema")
    if data.get("classification") not in {"illustrative", "empirical"}:
        raise CaptureIntegrityError("capture classification must be illustrative or empirical")
    assert_no_secrets(data)
    return data
