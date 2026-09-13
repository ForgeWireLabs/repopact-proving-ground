"""Acquire, validate, project, and verify the preregistered S2 task beds.

The materializer deliberately keeps third-party bytes outside Git. It accepts
only the immutable revisions in ``task-set.json`` and writes a manifest whose
fingerprint excludes local paths and acquisition timestamps. Model-facing
projections are structurally separate from evaluation projections so a future
runner cannot receive a complete upstream record by accident.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - requirements-repopact supplies it
    raise RuntimeError("S2 materialization requires jsonschema from requirements-repopact.txt") from exc


ROOT = Path(__file__).resolve().parents[2]
TASK_SET_PATH = Path(__file__).with_name("task-set.json")
MATERIALIZATION_SCHEMA_PATH = Path(__file__).with_name("materialization.schema.json")
MATERIALIZATION_SCHEMA_VERSION = "repopact.s2-materialization.v1"
MODEL_PROJECTION_SCHEMA_VERSION = "repopact.s2-model-task.v1"
MATERIALIZER_VERSION = "s2-materializer.v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[^/\\]+/[^/\\]+$")
GOLD_KEYS = frozenset({
    "patch", "test_patch", "all_patch", "gold", "gold_patch", "gold_test_patch",
    "solution", "solution_patch", "answer", "hidden_answer",
})


class MaterializationError(RuntimeError):
    """The registered bed or materialized bytes cannot be trusted."""


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permit HTTPS redirects only; never follow a file or plaintext redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
            raise MaterializationError(f"unsafe acquisition redirect rejected: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    return _digest_bytes(_canonical_json(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_relative(value: str, *, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MaterializationError(f"{label} must be a normalized relative path: {value}")
    return path


def _load_task_set(path: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path or TASK_SET_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot read S2 task-set registration: {exc}") from exc
    if not isinstance(data, dict) or data.get("study_id") != "S2":
        raise MaterializationError("S2 task-set registration must declare study_id S2")
    version = data.get("version")
    beds = data.get("task_sets")
    if not isinstance(version, str) or not version:
        raise MaterializationError("S2 task-set registration needs a version")
    if not isinstance(beds, list) or not beds:
        raise MaterializationError("S2 task-set registration needs task_sets")
    ids: list[str] = []
    for bed in beds:
        if not isinstance(bed, dict) or not isinstance(bed.get("id"), str):
            raise MaterializationError("S2 task_sets entries must have string ids")
        ids.append(bed["id"])
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise MaterializationError("S2 task_sets must be sorted and uniquely identified")
    return data, {bed["id"]: bed for bed in beds}


def _validate_bed(bed: dict[str, Any]) -> None:
    bed_id = bed.get("id")
    revision = bed.get("revision")
    task_ids = bed.get("task_ids")
    asset_path = bed.get("asset_path")
    asset_sha256 = bed.get("asset_sha256")
    if not isinstance(bed_id, str) or not bed_id:
        raise MaterializationError("S2 bed needs a non-empty id")
    if not isinstance(revision, str) or not COMMIT_RE.fullmatch(revision):
        raise MaterializationError(f"{bed_id}: immutable 40-character revision is required")
    if not isinstance(task_ids, list) or not task_ids or any(not isinstance(item, str) or not item for item in task_ids):
        raise MaterializationError(f"{bed_id}: task_ids must be non-empty strings")
    if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
        raise MaterializationError(f"{bed_id}: task_ids must be sorted and unique")
    if not isinstance(asset_path, str):
        raise MaterializationError(f"{bed_id}: immutable asset_path is required")
    _safe_relative(asset_path, label=f"{bed_id} asset_path")
    if not isinstance(asset_sha256, str) or not SHA256_RE.fullmatch(asset_sha256):
        raise MaterializationError(f"{bed_id}: asset_sha256 must be a lowercase SHA-256 digest")
    if bed.get("source_type") not in {"huggingface", "git"}:
        raise MaterializationError(f"{bed_id}: source_type must be huggingface or git")
    if bed.get("source_format") not in {"parquet", "arrow-stream"}:
        raise MaterializationError(f"{bed_id}: source_format must be parquet or arrow-stream")
    if bed.get("source_type") == "git":
        asset_git_blob_sha = bed.get("asset_git_blob_sha")
        if not isinstance(asset_git_blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", asset_git_blob_sha):
            raise MaterializationError(f"{bed_id}: Git asset blob identity is required")
    dataset = bed.get("dataset")
    if not isinstance(dataset, str) or not dataset.startswith("https://"):
        raise MaterializationError(f"{bed_id}: HTTPS dataset locator is required")
    if not isinstance(bed.get("adapter"), str) or not bed["adapter"]:
        raise MaterializationError(f"{bed_id}: study-owned adapter is required")


def _run_git(args: list[str], *, timeout: float) -> str:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaterializationError(f"git command failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
        raise MaterializationError(detail)
    return result.stdout.strip()


def _verify_asset(path: Path, expected_sha256: str) -> tuple[str, int]:
    if not path.is_file():
        raise MaterializationError(f"materialized asset is missing: {path}")
    actual = _digest_file(path)
    size = path.stat().st_size
    if actual != expected_sha256:
        raise MaterializationError(
            f"asset digest mismatch for {path.name}: expected {expected_sha256}, got {actual}"
        )
    return actual, size


def _download_asset(url: str, destination: Path, expected_sha256: str, *, timeout: float) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _verify_asset(destination, expected_sha256)
        return destination
    temp_path: Path | None = None
    opener = urllib.request.build_opener(SafeRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "RepoPact-S2-materializer/1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".partial",
                dir=destination.parent, delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    handle.write(chunk)
        _verify_asset(temp_path, expected_sha256)
        os.replace(temp_path, destination)
        temp_path = None
    except (MaterializationError, OSError, urllib.error.URLError, TimeoutError) as exc:
        raise MaterializationError(f"could not acquire immutable asset: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
    return destination


def _ensure_huggingface_asset(destination: Path, bed: dict[str, Any], *, timeout: float) -> tuple[Path, str | None]:
    asset_path = _safe_relative(bed["asset_path"], label="Hugging Face asset_path")
    local = destination / "source" / asset_path
    encoded_asset = urllib.parse.quote(bed["asset_path"], safe="/")
    url = (
        f"{bed['dataset'].rstrip('/')}/resolve/{bed['revision']}/{encoded_asset}"
        "?download=true"
    )
    return _download_asset(url, local, bed["asset_sha256"], timeout=timeout), None


def _ensure_swe_evo_checkout(destination: Path, bed: dict[str, Any], *, timeout: float) -> tuple[Path, str]:
    repository = destination / "source" / "repository"
    repository.parent.mkdir(parents=True, exist_ok=True)
    locator = bed["dataset"]
    if repository.exists():
        if not (repository / ".git").exists():
            raise MaterializationError(f"SWE-EVO source path is not a Git checkout: {repository}")
        remote = _run_git(["-C", str(repository), "remote", "get-url", "origin"], timeout=timeout)
        if remote.rstrip("/").removesuffix(".git") != locator.rstrip("/").removesuffix(".git"):
            raise MaterializationError(f"SWE-EVO checkout origin does not match {locator}")
        _run_git(["-C", str(repository), "fetch", "--quiet", "--no-tags", "origin", bed["revision"]], timeout=timeout)
    else:
        _run_git([
            "clone", "--filter=blob:none", "--no-checkout", "--no-tags", locator, str(repository)
        ], timeout=timeout)
        try:
            _run_git(["-C", str(repository), "cat-file", "-e", f"{bed['revision']}^{{commit}}"], timeout=timeout)
        except MaterializationError:
            _run_git(["-C", str(repository), "fetch", "--quiet", "--no-tags", "origin", bed["revision"]], timeout=timeout)
    # Keep the Git checkout bounded to the registered Arrow asset. The commit
    # object is still verified exactly, while unrelated repository files stay
    # out of the local materialization.
    _run_git(["-C", str(repository), "sparse-checkout", "init", "--no-cone"], timeout=timeout)
    _run_git(["-C", str(repository), "sparse-checkout", "set", "--no-cone", bed["asset_path"]], timeout=timeout)
    _run_git(["-C", str(repository), "checkout", "--quiet", "--detach", "--force", bed["revision"]], timeout=timeout)
    head = _run_git(["-C", str(repository), "rev-parse", "HEAD"], timeout=timeout)
    if head != bed["revision"]:
        raise MaterializationError(f"SWE-EVO checkout resolved {head}, expected {bed['revision']}")
    asset_path = _safe_relative(bed["asset_path"], label="SWE-EVO asset_path")
    local = repository / asset_path
    blob = _run_git(["-C", str(repository), "rev-parse", f"HEAD:{bed['asset_path']}"], timeout=timeout)
    if blob != bed["asset_git_blob_sha"]:
        raise MaterializationError(
            f"SWE-EVO asset Git blob resolved {blob}, expected {bed['asset_git_blob_sha']}"
        )
    _verify_asset(local, bed["asset_sha256"])
    return local, blob


def _acquire_asset(destination: Path, bed: dict[str, Any], *, timeout: float) -> tuple[Path, str | None]:
    if bed["source_type"] == "huggingface":
        return _ensure_huggingface_asset(destination, bed, timeout=timeout)
    return _ensure_swe_evo_checkout(destination, bed, timeout=timeout)


def _load_arrow_rows(path: Path, source_format: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        import pyarrow as pa
        import pyarrow.ipc as ipc
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise MaterializationError(
            "S2 materialization requires the acquisition-only parser; install requirements-s2.txt"
        ) from exc
    try:
        if source_format == "parquet":
            table = parquet.read_table(path)
        elif source_format == "arrow-stream":
            with path.open("rb") as handle:
                reader = ipc.open_stream(handle)
                table = pa.Table.from_batches(list(reader))
        else:  # pragma: no cover - bed validation rejects this first
            raise MaterializationError(f"unsupported source format: {source_format}")
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise MaterializationError(f"cannot parse {source_format} asset {path}: {exc}") from exc
    rows = table.to_pylist()
    if any(not isinstance(row, dict) for row in rows):
        raise MaterializationError("dataset rows must decode to objects")
    return rows, list(table.column_names)


def _text(value: Any, *, field: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise MaterializationError(f"selected record field {field} must be a non-empty string")
    return value


def _commit(value: Any, *, field: str) -> str:
    result = _text(value, field=field)
    if not COMMIT_RE.fullmatch(result):
        raise MaterializationError(f"selected record field {field} must be a 40-character commit")
    return result


def _list_field(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MaterializationError(f"selected record field {field} is not a JSON list") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MaterializationError(f"selected record field {field} must be a list of strings or JSON list text")
    return list(value)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    raise MaterializationError(f"dataset contains unsupported value type {type(value).__name__}")


def _adapt_swe_bench(row: dict[str, Any]) -> dict[str, Any]:
    task_id = _text(row.get("instance_id"), field="instance_id")
    repository = _text(row.get("repo"), field="repo")
    base_commit = _commit(row.get("base_commit"), field="base_commit")
    return {
        "task_id": task_id,
        "repository": repository,
        "base_commit": base_commit,
        "problem_statement": _text(row.get("problem_statement"), field="problem_statement"),
        "hints_text": _text(row.get("hints_text"), field="hints_text", required=False),
        "environment_setup_commit": _text(row.get("environment_setup_commit"), field="environment_setup_commit", required=False),
        "fail_to_pass": _list_field(row.get("FAIL_TO_PASS"), field="FAIL_TO_PASS"),
        "pass_to_pass": _list_field(row.get("PASS_TO_PASS"), field="PASS_TO_PASS"),
        "gold_patch": _text(row.get("patch"), field="patch", required=False),
        "gold_test_patch": _text(row.get("test_patch"), field="test_patch", required=False),
        "source_metadata": {
            key: _jsonable(row[key])
            for key in ("created_at", "version", "difficulty")
            if key in row
        },
    }


def _adapt_swe_evo(row: dict[str, Any]) -> dict[str, Any]:
    # SWE-EVO is intentionally adapted here rather than in the generic S2
    # driver. Its Arrow schema has evolution-specific version and provenance
    # fields in addition to its SWE-bench-shaped core.
    normalized = _adapt_swe_bench(row)
    normalized["source_metadata"].update({
        key: _jsonable(row[key])
        for key in (
            "start_version", "end_version", "end_version_commit", "instance_id_swe",
            "bench", "test_cmds", "log_parser", "image",
        )
        if key in row
    })
    for field in ("start_version", "end_version"):
        _text(row.get(field), field=field)
    _commit(row.get("end_version_commit"), field="end_version_commit")
    return normalized


def _adapter_for(bed: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    adapter = bed["adapter"]
    if adapter == "swe-bench-verified-v1":
        return _adapt_swe_bench
    if adapter == "swe-evo-v1":
        return _adapt_swe_evo
    raise MaterializationError(f"unsupported study-owned S2 adapter: {adapter}")


def _source_schema_check(bed: dict[str, Any], columns: Iterable[str]) -> list[str]:
    source_schema = sorted(str(column) for column in columns)
    required = set(bed.get("required_source_fields", []))
    missing = sorted(required - set(source_schema))
    if missing:
        raise MaterializationError(f"{bed['id']}: source schema is missing {', '.join(missing)}")
    return source_schema


def _select_records(
    rows: list[dict[str, Any]], bed: dict[str, Any], columns: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    _validate_bed(bed)
    source_schema = _source_schema_check(bed, columns)
    task_ids = list(bed["task_ids"])
    counts = {task_id: 0 for task_id in task_ids}
    matches: dict[str, dict[str, Any]] = {}
    adapter = _adapter_for(bed)
    for row in rows:
        identifier = row.get("instance_id")
        if not isinstance(identifier, str):
            continue
        if identifier in counts:
            counts[identifier] += 1
            if counts[identifier] > 1:
                raise MaterializationError(f"{bed['id']}: registered task {identifier} occurs more than once")
            matches[identifier] = row
    missing = [task_id for task_id in task_ids if counts[task_id] == 0]
    if missing:
        raise MaterializationError(f"{bed['id']}: registered task(s) missing from source: {', '.join(missing)}")
    normalized: list[dict[str, Any]] = []
    for task_id in task_ids:
        row = matches[task_id]
        adapted = adapter(row)
        if adapted["task_id"] != task_id:
            raise MaterializationError(f"{bed['id']}: selected identifier type/value mismatch for {task_id}")
        adapted["source_schema"] = source_schema
        adapted["record_sha256"] = _canonical_digest({
            "bed_id": bed["id"],
            "record": adapted,
            "source_record": _jsonable(row),
        })
        normalized.append(adapted)
    return normalized, source_schema


def _model_projection(bed: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MODEL_PROJECTION_SCHEMA_VERSION,
        "study_id": "S2",
        "bed_id": bed["id"],
        "task_id": record["task_id"],
        "repository": record["repository"],
        "base_commit": record["base_commit"],
        "problem_statement": record["problem_statement"],
    }


def _evaluation_projection(bed: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "repopact.s2-evaluation-task.v1",
        "study_id": "S2",
        "bed_id": bed["id"],
        "task_id": record["task_id"],
        "repository": record["repository"],
        "base_commit": record["base_commit"],
        "environment_setup_commit": record["environment_setup_commit"],
        "fail_to_pass": record["fail_to_pass"],
        "pass_to_pass": record["pass_to_pass"],
        "gold_patch": record["gold_patch"],
        "gold_test_patch": record["gold_test_patch"],
        "source_metadata": record["source_metadata"],
    }


def _assert_no_gold_leakage(projection: Any, record: dict[str, Any]) -> None:
    serialized = json.dumps(projection, sort_keys=True, ensure_ascii=True)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).lower().replace("-", "_")
                if normalized_key in GOLD_KEYS or "patch" in normalized_key:
                    raise MaterializationError(f"model-facing projection contains forbidden field {key}")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(projection)
    for field in ("gold_patch", "gold_test_patch"):
        gold = record[field]
        if gold and gold in serialized:
            raise MaterializationError(f"model-facing projection contains {field} content")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{path.name}.", suffix=".partial",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _base_commit_preflight(records: list[dict[str, Any]], *, timeout: float) -> list[dict[str, Any]]:
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        repository = record["repository"]
        commit = record["base_commit"]
        if not REPOSITORY_RE.fullmatch(repository):
            raise MaterializationError(f"invalid source repository identity: {repository}")
        key = (repository, commit)
        if key in cache:
            continue
        url = f"https://api.github.com/repos/{repository}/commits/{commit}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "RepoPact-S2-materializer/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            resolved = payload.get("sha") if isinstance(payload, dict) else None
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise MaterializationError(f"base commit preflight failed for {repository}@{commit}: {exc}") from exc
        if resolved != commit:
            raise MaterializationError(
                f"base commit preflight failed for {repository}@{commit}: resolved {resolved!r}"
            )
        cache[key] = {"repository": repository, "base_commit": commit, "status": "resolved", "method": "github-commit-api"}
    return [
        {"task_id": record["task_id"], **cache[(record["repository"], record["base_commit"])]}
        for record in records
    ]


def _local_asset_path(destination: Path, asset: Path) -> str:
    try:
        return asset.relative_to(destination).as_posix()
    except ValueError as exc:
        raise MaterializationError(f"asset escaped materialization directory: {asset}") from exc


def _identity(
    task_set: dict[str, Any], bed: dict[str, Any], source_schema: list[str],
    asset_size: int, asset_sha256: str, records: list[dict[str, Any]],
    git_blob_sha: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "study_id": "S2",
        "task_set_registration_version": task_set["version"],
        "bed_id": bed["id"],
        "adapter": bed["adapter"],
        "source_type": bed["source_type"],
        "source_format": bed["source_format"],
        "upstream_locator": bed["dataset"],
        "upstream_revision": bed["revision"],
        "asset_relative_path": bed["asset_path"],
        "asset_size_bytes": asset_size,
        "asset_sha256": asset_sha256,
        "upstream_git_blob_sha": git_blob_sha,
        "source_schema": source_schema,
        "selected_task_ids": [record["task_id"] for record in records],
        "selected_tasks": [
            {
                "task_id": record["task_id"],
                "record_sha256": record["record_sha256"],
                "repository": record["repository"],
                "base_commit": record["base_commit"],
            }
            for record in records
        ],
        "model_projection_schema": MODEL_PROJECTION_SCHEMA_VERSION,
        "gold_solution_fields_excluded_from_model_projection": True,
        "materializer_version": MATERIALIZER_VERSION,
    }


def _write_materialization(
    destination: Path, task_set: dict[str, Any], bed: dict[str, Any], source_asset: Path,
    source_schema: list[str], records: list[dict[str, Any]], base_preflight: list[dict[str, Any]],
    *, git_blob_sha: str | None, acquired_at: str,
) -> dict[str, Any]:
    asset_sha256, asset_size = _verify_asset(source_asset, bed["asset_sha256"])
    selected_tasks: list[dict[str, Any]] = []
    for record in records:
        filename = f"{record['task_id']}.json"
        _safe_relative(filename, label="projection filename")
        model_path = Path("projections") / "model-facing" / filename
        evaluation_path = Path("projections") / "evaluation" / filename
        model = _model_projection(bed, record)
        _assert_no_gold_leakage(model, record)
        evaluation = _evaluation_projection(bed, record)
        _atomic_json(destination / model_path, model)
        _atomic_json(destination / evaluation_path, evaluation)
        selected_tasks.append({
            "task_id": record["task_id"],
            "record_sha256": record["record_sha256"],
            "repository": record["repository"],
            "base_commit": record["base_commit"],
            "model_projection": model_path.as_posix(),
            "evaluation_projection": evaluation_path.as_posix(),
        })
    identity = _identity(task_set, bed, source_schema, asset_size, asset_sha256, records, git_blob_sha)
    manifest = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "study_id": "S2",
        "task_set_registration_version": task_set["version"],
        "bed_id": bed["id"],
        "upstream": {
            "locator": bed["dataset"],
            "revision": bed["revision"],
            "asset_relative_path": bed["asset_path"],
            "asset_size_bytes": asset_size,
            "asset_sha256": asset_sha256,
            "git_blob_sha": git_blob_sha,
            "source_format": bed["source_format"],
        },
        "local_asset_path": _local_asset_path(destination, source_asset),
        "source_schema": source_schema,
        "acquisition": {
            "timestamp": acquired_at,
            "materializer_version": MATERIALIZER_VERSION,
            "classification": "deterministic/non-empirical",
        },
        "selected_task_ids": [record["task_id"] for record in records],
        "selected_task_count": len(records),
        "selected_tasks": selected_tasks,
        "base_commit_preflight": base_preflight,
        "projections": {
            "model_facing_directory": "projections/model-facing",
            "evaluation_directory": "projections/evaluation",
            "model_facing_fields": ["task_id", "repository", "base_commit", "problem_statement"],
            "gold_solution_fields_excluded_from_model_projection": True,
        },
        "fingerprint_basis": identity,
        "fingerprint": _canonical_digest(identity),
    }
    _atomic_json(destination / "REPOPACT-S2-MATERIALIZATION.json", manifest)
    return manifest


def materialize_bed(
    bed: dict[str, Any], out: str | Path = ".s2-materialized", *, timeout: float = 60.0,
    clock: Callable[[], str] = _utc_now,
) -> Path:
    task_set, _ = _load_task_set()
    _validate_bed(bed)
    destination = Path(out).resolve() / bed["id"]
    destination.mkdir(parents=True, exist_ok=True)
    source_asset, git_blob_sha = _acquire_asset(destination, bed, timeout=timeout)
    rows, columns = _load_arrow_rows(source_asset, bed["source_format"])
    records, source_schema = _select_records(rows, bed, columns)
    base_preflight = _base_commit_preflight(records, timeout=timeout)
    _write_materialization(
        destination, task_set, bed, source_asset, source_schema, records, base_preflight,
        git_blob_sha=git_blob_sha, acquired_at=clock(),
    )
    return destination


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot read {label}: {exc}") from exc


def _validate_manifest_schema(manifest: dict[str, Any]) -> None:
    schema = _read_json(MATERIALIZATION_SCHEMA_PATH, label="materialization schema")
    try:
        jsonschema.Draft202012Validator(schema).validate(manifest)
    except jsonschema.ValidationError as exc:
        raise MaterializationError(f"materialization manifest schema failure: {exc.message}") from exc


def verify_materialization(path: str | Path) -> dict[str, Any]:
    destination = Path(path).resolve()
    manifest = _read_json(destination / "REPOPACT-S2-MATERIALIZATION.json", label="materialization manifest")
    if not isinstance(manifest, dict):
        raise MaterializationError("materialization manifest must be an object")
    _validate_manifest_schema(manifest)
    task_set, beds = _load_task_set()
    bed = beds.get(manifest.get("bed_id"))
    if bed is None:
        raise MaterializationError(f"materialization bed is not registered: {manifest.get('bed_id')}")
    _validate_bed(bed)
    if manifest["task_set_registration_version"] != task_set["version"]:
        raise MaterializationError("materialization task-set registration version is stale")
    upstream = manifest["upstream"]
    expected_upstream = {
        "locator": bed["dataset"],
        "revision": bed["revision"],
        "asset_relative_path": bed["asset_path"],
        "source_format": bed["source_format"],
        "asset_sha256": bed["asset_sha256"],
        "git_blob_sha": bed.get("asset_git_blob_sha"),
    }
    for key, expected in expected_upstream.items():
        if upstream.get(key) != expected:
            raise MaterializationError(f"materialization upstream {key} does not match registration")
    local_asset = destination / _safe_relative(manifest["local_asset_path"], label="local_asset_path")
    actual_sha, actual_size = _verify_asset(local_asset, upstream["asset_sha256"])
    if actual_size != upstream["asset_size_bytes"]:
        raise MaterializationError("materialized asset byte size changed")
    git_blob_sha = upstream.get("git_blob_sha")
    if bed["source_type"] == "git":
        repository = destination / "source" / "repository"
        head = _run_git(["-C", str(repository), "rev-parse", "HEAD"], timeout=20)
        if head != bed["revision"]:
            raise MaterializationError(f"offline SWE-EVO checkout is {head}, expected {bed['revision']}")
        actual_blob = _run_git(["-C", str(repository), "rev-parse", f"HEAD:{bed['asset_path']}"], timeout=20)
        if actual_blob != git_blob_sha:
            raise MaterializationError("offline SWE-EVO Git blob identity changed")
    rows, columns = _load_arrow_rows(local_asset, bed["source_format"])
    records, source_schema = _select_records(rows, bed, columns)
    if source_schema != manifest["source_schema"]:
        raise MaterializationError("materialized source schema changed")
    if manifest["selected_task_ids"] != [record["task_id"] for record in records]:
        raise MaterializationError("registered selected task order changed")
    if manifest["selected_task_count"] != len(records):
        raise MaterializationError("selected task count changed")
    expected_tasks: list[dict[str, Any]] = []
    for record in records:
        selected = next((item for item in manifest["selected_tasks"] if item.get("task_id") == record["task_id"]), None)
        if selected is None:
            raise MaterializationError(f"manifest has no selected task record for {record['task_id']}")
        if selected.get("record_sha256") != record["record_sha256"]:
            raise MaterializationError(f"selected record digest changed for {record['task_id']}")
        model_path = _safe_relative(selected["model_projection"], label="model_projection")
        evaluation_path = _safe_relative(selected["evaluation_projection"], label="evaluation_projection")
        model = _read_json(destination / model_path, label=f"model projection {record['task_id']}")
        evaluation = _read_json(destination / evaluation_path, label=f"evaluation projection {record['task_id']}")
        expected_model = _model_projection(bed, record)
        _assert_no_gold_leakage(model, record)
        if model != expected_model:
            raise MaterializationError(f"model projection changed for {record['task_id']}")
        if evaluation != _evaluation_projection(bed, record):
            raise MaterializationError(f"evaluation projection changed for {record['task_id']}")
        expected_tasks.append({
            "task_id": record["task_id"],
            "record_sha256": record["record_sha256"],
            "repository": record["repository"],
            "base_commit": record["base_commit"],
            "model_projection": selected["model_projection"],
            "evaluation_projection": selected["evaluation_projection"],
        })
    if manifest["selected_tasks"] != expected_tasks:
        raise MaterializationError("selected task manifest entries changed")
    preflight = manifest["base_commit_preflight"]
    expected_preflight = {
        (record["task_id"], record["repository"], record["base_commit"])
        for record in records
    }
    actual_preflight = {
        (item.get("task_id"), item.get("repository"), item.get("base_commit"))
        for item in preflight
        if item.get("status") == "resolved"
    }
    if actual_preflight != expected_preflight or len(preflight) != len(records):
        raise MaterializationError("base-commit preflight is incomplete or changed")
    identity = _identity(task_set, bed, source_schema, actual_size, actual_sha, records, git_blob_sha)
    if manifest["fingerprint_basis"] != identity:
        raise MaterializationError("materialization fingerprint basis changed")
    if manifest["fingerprint"] != _canonical_digest(identity):
        raise MaterializationError("materialization fingerprint changed")
    if manifest["projections"]["gold_solution_fields_excluded_from_model_projection"] is not True:
        raise MaterializationError("manifest does not prove gold-solution exclusion")
    return manifest


def _find_bed(bed_id: str) -> dict[str, Any]:
    _, beds = _load_task_set()
    bed = beds.get(bed_id)
    if bed is None:
        raise MaterializationError(f"unknown bed: {bed_id}")
    _validate_bed(bed)
    return bed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize or verify preregistered S2 task beds")
    parser.add_argument("--bed", required=True)
    parser.add_argument("--out", default=".s2-materialized")
    parser.add_argument("--verify", action="store_true", help="verify an existing bed without network access")
    args = parser.parse_args(argv)
    try:
        bed = _find_bed(args.bed)
        destination = Path(args.out).resolve() / bed["id"]
        if args.verify:
            manifest = verify_materialization(destination)
            print(f"OFFLINE VERIFY PASS: {bed['id']} fingerprint={manifest['fingerprint']}")
        else:
            destination = materialize_bed(bed, args.out)
            manifest = _read_json(destination / "REPOPACT-S2-MATERIALIZATION.json", label="materialization manifest")
            print(f"MATERIALIZATION PASS: {destination} fingerprint={manifest['fingerprint']}")
        return 0
    except MaterializationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
