import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.s2 import materialize


COMMIT = "a" * 40
ASSET = b"deterministic-source-bytes"
ASSET_SHA = hashlib.sha256(ASSET).hexdigest()
REQUIRED_COLUMNS = [
    "repo", "instance_id", "base_commit", "patch", "test_patch",
    "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS", "environment_setup_commit",
]


def bed_template(**overrides):
    bed = {
        "id": "synthetic-bed",
        "dataset": "https://example.test/dataset",
        "revision": COMMIT,
        "source_type": "huggingface",
        "source_format": "parquet",
        "adapter": "swe-bench-verified-v1",
        "asset_path": "data/test.parquet",
        "asset_sha256": ASSET_SHA,
        "required_source_fields": REQUIRED_COLUMNS,
        "task_ids": ["repo__project-1"],
    }
    bed.update(overrides)
    return bed


def source_row(task_id="repo__project-1", **overrides):
    row = {
        "repo": "owner/project",
        "instance_id": task_id,
        "base_commit": COMMIT,
        "patch": "gold patch body",
        "test_patch": "gold test patch body",
        "problem_statement": "Fix the registered bug.",
        "FAIL_TO_PASS": ["tests/test_bug.py::test_regression"],
        "PASS_TO_PASS": ["tests/test_existing.py::test_stable"],
        "environment_setup_commit": COMMIT,
    }
    row.update(overrides)
    return row


class S2MaterializerTests(unittest.TestCase):
    def test_registration_requires_an_immutable_revision(self):
        bed = bed_template(revision="moving-main")
        with self.assertRaises(materialize.MaterializationError):
            materialize._validate_bed(bed)

    def test_git_bed_requires_registered_blob_identity(self):
        bed = bed_template(source_type="git", source_format="arrow-stream")
        with self.assertRaises(materialize.MaterializationError):
            materialize._validate_bed(bed)

    def test_wrong_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset"
            path.write_bytes(b"tampered")
            with self.assertRaises(materialize.MaterializationError):
                materialize._verify_asset(path, ASSET_SHA)

    def test_interrupted_download_does_not_publish_partial_bytes(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _size):
                if not hasattr(self, "read_once"):
                    self.read_once = True
                    return b"partial"
                raise OSError("simulated interrupted response")

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "nested" / "asset"
            with patch("urllib.request.build_opener", return_value=Opener()):
                with self.assertRaises(materialize.MaterializationError):
                    materialize._download_asset(
                        "https://example.test/asset", destination, ASSET_SHA, timeout=1
                    )
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob("*.partial")), [])

    def test_existing_wrong_cache_is_reverified_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "asset"
            destination.write_bytes(b"old wrong cache")
            with self.assertRaises(materialize.MaterializationError):
                materialize._download_asset(
                    "https://example.test/asset", destination, ASSET_SHA, timeout=1
                )

    def test_unknown_bed_is_rejected(self):
        with self.assertRaises(materialize.MaterializationError):
            materialize._find_bed("not-registered")

    def test_missing_registered_task_is_rejected(self):
        bed = bed_template()
        with self.assertRaises(materialize.MaterializationError):
            materialize._select_records([], bed, REQUIRED_COLUMNS)

    def test_duplicate_registered_task_is_rejected(self):
        bed = bed_template()
        rows = [source_row(), source_row()]
        with self.assertRaises(materialize.MaterializationError):
            materialize._select_records(rows, bed, REQUIRED_COLUMNS)

    def test_registered_order_controls_selection_order(self):
        second = "repo__project-2"
        bed = bed_template(task_ids=["repo__project-1", second])
        records, _ = materialize._select_records(
            [source_row(second), source_row()], bed, REQUIRED_COLUMNS
        )
        self.assertEqual([record["task_id"] for record in records], bed["task_ids"])

    def test_malformed_identifier_is_not_a_selector_match(self):
        bed = bed_template()
        with self.assertRaises(materialize.MaterializationError):
            materialize._select_records(
                [source_row(instance_id=["repo__project-1"])], bed, REQUIRED_COLUMNS
            )

    def test_source_schema_is_checked_before_adaptation(self):
        bed = bed_template()
        with self.assertRaises(materialize.MaterializationError):
            materialize._select_records([source_row()], bed, REQUIRED_COLUMNS[:-1])

    def test_model_projection_excludes_gold_solution_fields(self):
        bed = bed_template()
        record, _ = materialize._select_records([source_row()], bed, REQUIRED_COLUMNS)
        projection = materialize._model_projection(bed, record[0])
        materialize._assert_no_gold_leakage(projection, record[0])
        self.assertNotIn("patch", json.dumps(projection).lower())
        self.assertNotIn("gold patch body", json.dumps(projection))
        self.assertNotIn("gold test patch body", json.dumps(projection))

    def test_evaluation_projection_retains_scoring_metadata(self):
        bed = bed_template()
        records, _ = materialize._select_records([source_row()], bed, REQUIRED_COLUMNS)
        projection = materialize._evaluation_projection(bed, records[0])
        self.assertEqual(projection["fail_to_pass"], ["tests/test_bug.py::test_regression"])
        self.assertEqual(projection["gold_patch"], "gold patch body")
        self.assertEqual(projection["gold_test_patch"], "gold test patch body")

    def test_swe_evo_adapter_requires_evolution_fields(self):
        bed = bed_template(
            id="swe-evo-test", source_type="git", source_format="arrow-stream",
            adapter="swe-evo-v1", asset_git_blob_sha="b" * 40,
        )
        row = source_row(start_version="1.0", end_version="2.0", end_version_commit=COMMIT)
        records, _ = materialize._select_records([row], bed, REQUIRED_COLUMNS + [
            "start_version", "end_version", "end_version_commit"
        ])
        self.assertEqual(records[0]["source_metadata"]["start_version"], "1.0")

    def test_identity_is_independent_of_local_directory(self):
        bed = bed_template()
        records, schema = materialize._select_records([source_row()], bed, REQUIRED_COLUMNS)
        task_set = {"version": "test-registration"}
        first = materialize._identity(task_set, bed, schema, len(ASSET), ASSET_SHA, records, None)
        second = materialize._identity(task_set, bed, schema, len(ASSET), ASSET_SHA, records, None)
        self.assertEqual(first, second)
        self.assertEqual(materialize._canonical_digest(first), materialize._canonical_digest(second))

    def test_timestamp_does_not_change_fingerprint(self):
        bed = bed_template()
        records, schema = materialize._select_records([source_row()], bed, REQUIRED_COLUMNS)
        task_set = {"version": "test-registration"}
        preflight = [{
            "task_id": records[0]["task_id"], "repository": records[0]["repository"],
            "base_commit": records[0]["base_commit"], "status": "resolved",
            "method": "github-commit-api",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fingerprints = []
            for name, timestamp in (("one", "2026-09-13T22:00:00Z"), ("two", "2026-09-14T22:00:00Z")):
                destination = root / name
                asset = destination / "source" / "data" / "test.parquet"
                asset.parent.mkdir(parents=True)
                asset.write_bytes(ASSET)
                manifest = materialize._write_materialization(
                    destination, task_set, bed, asset, schema, records, preflight,
                    git_blob_sha=None, acquired_at=timestamp,
                )
                fingerprints.append(manifest["fingerprint"])
            self.assertEqual(fingerprints[0], fingerprints[1])

    def _write_synthetic_materialization(self, root):
        bed = bed_template()
        task_set = {"study_id": "S2", "version": "test-registration", "task_sets": [bed]}
        records, schema = materialize._select_records([source_row()], bed, REQUIRED_COLUMNS)
        preflight = [{
            "task_id": records[0]["task_id"], "repository": records[0]["repository"],
            "base_commit": records[0]["base_commit"], "status": "resolved",
            "method": "github-commit-api",
        }]
        asset = root / "source" / "data" / "test.parquet"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(ASSET)
        manifest = materialize._write_materialization(
            root, task_set, bed, asset, schema, records, preflight,
            git_blob_sha=None, acquired_at="2026-09-13T22:00:00Z",
        )
        return task_set, manifest

    def test_offline_verify_accepts_intact_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "synthetic-bed"
            task_set, _ = self._write_synthetic_materialization(root)
            registration = Path(tmp) / "task-set.json"
            registration.write_text(json.dumps(task_set), encoding="utf-8")
            with patch.object(materialize, "TASK_SET_PATH", registration), patch.object(
                materialize, "_load_arrow_rows", return_value=([source_row()], REQUIRED_COLUMNS)
            ):
                manifest = materialize.verify_materialization(root)
            self.assertEqual(manifest["acquisition"]["classification"], "deterministic/non-empirical")

    def test_offline_verify_detects_asset_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "synthetic-bed"
            task_set, _ = self._write_synthetic_materialization(root)
            (root / "source" / "data" / "test.parquet").write_bytes(b"tampered")
            registration = Path(tmp) / "task-set.json"
            registration.write_text(json.dumps(task_set), encoding="utf-8")
            with patch.object(materialize, "TASK_SET_PATH", registration):
                with self.assertRaises(materialize.MaterializationError):
                    materialize.verify_materialization(root)


if __name__ == "__main__":
    unittest.main()
