"""Pre-registration and deterministic ordering helpers for study task sets."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RegistrationError(ValueError):
    """A task/condition registry is not immutable enough to run."""


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def deterministic_seed(study_id: str, task_set_version: str, case_id: str, condition: str, repetition: int) -> int:
    """Implement the WI022 preregistered unsigned first-64-bit seed policy."""
    if repetition < 0:
        raise ValueError("repetition must be non-negative")
    material = f"{study_id}|{task_set_version}|{case_id}|{condition}|{repetition}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=False)


@dataclass(frozen=True)
class RegisteredSet:
    study_id: str
    version: str
    registered: str
    records: tuple[dict[str, Any], ...]
    source_digest: str


def load_registered_set(path: str | Path, *, study_id: str, records_key: str) -> RegisteredSet:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RegistrationError(f"{source} must contain an object")
    if data.get("study_id") != study_id:
        raise RegistrationError(f"{source} study_id must be {study_id}")
    for key in ("version", "registered", records_key):
        if key not in data:
            raise RegistrationError(f"{source} missing {key}")
    records = data[records_key]
    if not isinstance(records, list) or not records:
        raise RegistrationError(f"{source}.{records_key} must be a non-empty array")
    if any(not isinstance(record, dict) for record in records):
        raise RegistrationError(f"{source}.{records_key} entries must be objects")
    ids = [record.get("id") for record in records]
    if any(not isinstance(identifier, str) or not identifier for identifier in ids):
        raise RegistrationError(f"{source}.{records_key} entries require non-empty id")
    if ids != sorted(ids):
        raise RegistrationError(f"{source}.{records_key} must be sorted by id")
    if len(ids) != len(set(ids)):
        raise RegistrationError(f"{source}.{records_key} ids must be unique")
    return RegisteredSet(
        study_id=study_id,
        version=str(data["version"]),
        registered=str(data["registered"]),
        records=tuple(records),
        source_digest=file_digest(source),
    )
