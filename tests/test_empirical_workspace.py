import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.harness.empirical import EmpiricalContractError, EmpiricalExecutor
from benchmarks.harness.empirical_workspace import (
    EmpiricalWorkspace,
    EmpiricalWorkspaceError,
    WORKSPACE_SECURITY_VERSION,
    _normalized_acl,
)


class EmpiricalWorkspaceTests(unittest.TestCase):
    def test_non_windows_allocator_is_isolated_and_preflighted(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("benchmarks.harness.empirical_workspace._is_windows", return_value=False):
                workspace = EmpiricalWorkspace.allocate(repo_root=temp, prefix="unit")
                try:
                    self.assertTrue(workspace.path.is_dir())
                    self.assertEqual(workspace.security["version"], WORKSPACE_SECURITY_VERSION)
                    self.assertEqual(workspace.security["sandbox_mode"], "not-applicable")
                    self.assertTrue(all(workspace.security["preflight"].values()))
                    self.assertEqual(workspace.child("worker-a"), workspace.path / "worker-a")
                    with self.assertRaises(EmpiricalWorkspaceError):
                        workspace.child("..")
                finally:
                    workspace.close()
                self.assertFalse(workspace.path.exists())

    def test_non_windows_validation_rejects_path_outside_configured_root(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            with patch("benchmarks.harness.empirical_workspace._is_windows", return_value=False):
                with self.assertRaises(EmpiricalWorkspaceError):
                    EmpiricalWorkspace.validate_for_inference(Path(outside), repo_root=temp)

    def test_acl_normalization_is_machine_stable(self):
        snapshot = {
            "owner_sid": "host",
            "are_access_rules_protected": True,
            "are_access_rules_canonical": True,
            "integrity_sddl": None,
            "access": [
                {"sid": "sandbox", "access": "FullControl", "control": "Allow", "is_inherited": False, "inheritance": "None", "propagation": "None"},
                {"sid": "system", "access": "FullControl", "control": "Allow", "is_inherited": False, "inheritance": "None", "propagation": "None"},
            ],
        }
        normalized = _normalized_acl(snapshot)
        self.assertEqual([rule["sid"] for rule in normalized["access"]], ["sandbox", "system"])
        self.assertTrue(normalized["are_access_rules_protected"])

    def test_executor_rejects_unproven_workspace_before_app_server(self):
        with tempfile.TemporaryDirectory() as temp:
            executor = EmpiricalExecutor(
                model=type("M", (), {"version": "model", "provider": "provider"})(),
                cwd=temp,
                capture_root=temp,
                pricing_id="test",
                output_schema={"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
            )
            with patch("benchmarks.harness.empirical.CodexAppServer.run") as app_server_run:
                with self.assertRaises(EmpiricalContractError):
                    executor.run(
                        "probe",
                        capture_name="capture.json",
                        study_id="S",
                        case_id="C",
                        condition="x",
                        fixture="f",
                        fixture_version="v",
                        workspace_identity={"fixture": "f"},
                    )
                app_server_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
