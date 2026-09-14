import errno
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from benchmarks.harness.workspace_io import (
    WorkspaceIOError,
    read_bytes_after_quiescence,
    read_text_after_quiescence,
)


class WorkspaceIOTests(unittest.TestCase):
    def test_immediately_readable_file_returns_exact_bytes_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.txt"
            target.write_bytes(b"line 1\r\nline 2")
            result = read_text_after_quiescence(target, workspace=root)
            self.assertEqual(result.content, b"line 1\r\nline 2")
            self.assertEqual(result.evidence["attempts_required"], 1)
            self.assertEqual(result.evidence["transient_errors_encountered"], [])
            self.assertEqual(result.evidence["final_file_metadata"]["stat"]["size"], 14)

    def test_nonexistent_file_fails_closed_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(WorkspaceIOError) as caught:
                read_bytes_after_quiescence(root / "missing.txt", workspace=root)
            evidence = caught.exception.evidence
            self.assertEqual(evidence["attempts"][0]["classification"], "nonexistent")
            self.assertEqual(evidence["attempts"][0]["error"]["exception_class"], "FileNotFoundError")
            self.assertFalse(evidence["attempts"][0]["path"]["is_file"])

    def test_directory_substitution_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.txt"
            target.mkdir()
            with self.assertRaises(WorkspaceIOError) as caught:
                read_bytes_after_quiescence(target, workspace=root)
            self.assertEqual(caught.exception.evidence["attempts"][0]["classification"], "path_type")

    def test_permanent_permission_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.txt"
            target.write_bytes(b"value")

            def denied(_path):
                raise PermissionError(errno.EACCES, "controlled access denied", str(target))

            with self.assertRaises(WorkspaceIOError) as caught:
                read_bytes_after_quiescence(target, workspace=root, reader=denied)
            evidence = caught.exception.evidence
            self.assertEqual(len(evidence["attempts"]), 1)
            self.assertEqual(evidence["attempts"][0]["classification"], "permanent_access_denied")
            self.assertIn(evidence["attempts"][0]["error"]["winerror"], (None, 0, 5))

    def test_transient_sharing_timeout_fails_closed_and_preserves_all_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.txt"
            target.write_bytes(b"value")

            def sharing(_path):
                error = PermissionError(errno.EACCES, "controlled sharing violation", str(target))
                error.winerror = 32
                raise error

            with self.assertRaises(WorkspaceIOError) as caught:
                read_bytes_after_quiescence(target, workspace=root, deadline_seconds=0.12, retry_interval_seconds=0.02, reader=sharing)
            evidence = caught.exception.evidence
            self.assertGreaterEqual(len(evidence["attempts"]), 2)
            self.assertTrue(all(item["classification"] == "transient_sharing_or_lock" for item in evidence["attempts"]))
            self.assertTrue(all(item["error"]["winerror"] == 32 for item in evidence["attempts"]))

    def test_wrong_content_is_read_but_does_not_pass_objective(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "value.txt"
            target.write_bytes(b"wrong")
            result = read_bytes_after_quiescence(target, workspace=root)
            self.assertNotEqual(result.content, b"probe tool operation complete")

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_bytes(b"outside")
            try:
                with self.assertRaises(WorkspaceIOError) as caught:
                    read_bytes_after_quiescence(outside, workspace=root)
                self.assertEqual(caught.exception.evidence["attempts"][0]["classification"], "path_escape")
            finally:
                outside.unlink(missing_ok=True)

    @unittest.skipUnless(platform.system() == "Windows", "Windows reparse-point semantics")
    def test_symlink_or_reparse_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / f"{root.name}-outside.txt"
            link = root / "link.txt"
            outside.write_bytes(b"outside")
            try:
                try:
                    link.symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"symlink unavailable: {exc}")
                with self.assertRaises(WorkspaceIOError) as caught:
                    read_bytes_after_quiescence(link, workspace=root)
                self.assertEqual(caught.exception.evidence["attempts"][0]["classification"], "symlink_or_reparse_rejected")
            finally:
                link.unlink(missing_ok=True)
                outside.unlink(missing_ok=True)

    @unittest.skipUnless(platform.system() == "Windows", "Windows exclusive-lock regression")
    def test_real_windows_exclusive_lock_is_retried_until_release(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "locked.txt"
            target.write_bytes(b"locked content")
            escaped = str(target).replace("'", "''")
            script = (
                "$path = '" + escaped + "'; "
                "$stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, "
                "[System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None); "
                "[Console]::Out.WriteLine('ready'); [Console]::Out.Flush(); "
                "Start-Sleep -Milliseconds 350; $stream.Dispose()"
            )
            child = subprocess.Popen(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip() if child.stdout else "", "ready")
                result = read_bytes_after_quiescence(target, workspace=root, deadline_seconds=2.0, retry_interval_seconds=0.025)
                self.assertEqual(result.content, b"locked content")
                self.assertTrue(any(item["classification"] == "transient_sharing_or_lock" for item in result.evidence["attempts"]))
                self.assertEqual(result.evidence["attempts"][0]["error"]["winerror"], 32)
                self.assertGreaterEqual(result.evidence["attempts_required"], 2)
            finally:
                child.wait(timeout=5)
                if child.stdout:
                    child.stdout.close()
                if child.stderr:
                    child.stderr.close()


if __name__ == "__main__":
    unittest.main()
