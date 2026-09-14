import shutil
import tempfile
import unittest
from pathlib import Path

from benchmarks.pactbench.materialize import (
    PACTBENCH_ROOT,
    MaterializationError,
    audit_task_set,
    load_cases,
    load_task,
    load_task_set,
    materialize_functional_seed,
    materialize_case,
    run_check,
    validate_grader_contract,
)


class PactBenchMaterializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.task_set = load_task_set()
        cls.cases = {case.original_id: case for case in load_cases(cls.task_set)}

    def _original_fixture(self, case_id):
        case = self.cases[case_id]
        task = load_task(case, effective=False)
        fixture = PACTBENCH_ROOT / task["seed"]["fixture"]
        root = Path(tempfile.mkdtemp(prefix=f"pactbench-original-{case_id}-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        destination = root / "fixture"
        shutil.copytree(fixture, destination)
        return destination

    def test_absent_registered_typo_is_rejected(self):
        root = self._original_fixture("0003")
        check = self.cases["0003"].original_preflight[0]
        result = run_check(root, check)
        self.assertFalse(result["passed"])

    def test_add_task_rejects_already_present_artifact(self):
        root = self._original_fixture("0012")
        check = self.cases["0012"].original_preflight[0]
        result = run_check(root, check)
        self.assertFalse(result["passed"])

    def test_absent_referenced_symbol_is_rejected(self):
        root = self._original_fixture("0013")
        check = self.cases["0013"].original_preflight[0]
        result = run_check(root, check)
        self.assertFalse(result["passed"])

    def test_claimed_red_test_when_seed_is_green_is_rejected(self):
        root = self._original_fixture("0001")
        check = self.cases["0001"].original_preflight[0]
        result = run_check(root, check)
        self.assertFalse(result["passed"])

    def test_broken_must_not_weaken_invariant_is_rejected(self):
        root = self._original_fixture("0018")
        source = root / "calc.py"
        source.write_text(source.read_text(encoding="utf-8").replace("ROUND_HALF_EVEN", "ROUND_DOWN"), encoding="utf-8")
        check = self.cases["0018"].preflight[0]
        result = run_check(root, check)
        self.assertFalse(result["passed"])

    def test_missing_grader_reference_is_rejected(self):
        root = self._original_fixture("0002")
        grader = dict(self.cases["0002"].grader)
        grader["deterministic_checks"] = [{"kind": "path_exists", "path": "not-present.py", "expected": True}]
        with self.assertRaises(MaterializationError):
            validate_grader_contract(grader, root)

    def test_setup_is_deterministic(self):
        case = self.cases["0007"]
        with tempfile.TemporaryDirectory(prefix="pactbench-setup-") as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            _, _, first_fingerprint = materialize_functional_seed(case, first)
            _, _, second_fingerprint = materialize_functional_seed(case, second)
            self.assertEqual(first_fingerprint, second_fingerprint)
            self.assertEqual((first / "tests/test_auth.py").read_bytes(), (second / "tests/test_auth.py").read_bytes())

    def test_baseline_and_repopact_functional_seeds_are_identical(self):
        case = self.cases["0023"]
        with tempfile.TemporaryDirectory(prefix="pactbench-arms-") as temp:
            baseline = materialize_case(case, Path(temp) / "baseline", "baseline")
            repopact = materialize_case(case, Path(temp) / "repopact", "repopact")
            self.assertEqual(baseline["functional_seed_fingerprint"], repopact["functional_seed_fingerprint"])

    def test_materialization_fingerprint_is_stable_across_directories(self):
        case = self.cases["0021"]
        with tempfile.TemporaryDirectory(prefix="pactbench-fingerprint-") as temp:
            first = materialize_case(case, Path(temp) / "first", "repopact")
            second = materialize_case(case, Path(temp) / "second", "repopact")
            self.assertEqual(first["materialization_fingerprint"], second["materialization_fingerprint"])

    def test_all_24_effective_cases_are_preflight_eligible(self):
        result = audit_task_set(self.task_set)
        self.assertEqual(result["status_totals"], {
            "ready": 7,
            "needs_seed_setup": 6,
            "task_definition_mismatch": 5,
            "grader_mismatch": 0,
            "retire_or_replace": 6,
        })
        self.assertEqual(result["effective_eligible_count"], 24)
        self.assertEqual(result["effective_ineligible_count"], 0)
        self.assertEqual(result["grader_invalid_count"], 0)


if __name__ == "__main__":
    unittest.main()
