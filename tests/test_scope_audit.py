from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval.cli import main
from agent_eval.diff_analysis import read_git_diff
from agent_eval.scope_audit import audit_scope
from agent_eval.reporting import write_scope_audit_artifacts
from agent_eval.test_selection import select_tests


class UncertainReviewer:
    base_url = "http://127.0.0.1:11434/v1"

    def request_json(self, _: str):
        return {"findings": [{
            "path": "src/math.py", "line": 999, "status": "clear_out_of_scope",
            "risk": "high", "reason": "unsupported location",
        }]}


class FixedReviewer:
    base_url = "http://127.0.0.1:11434/v1"

    def __init__(self, line: int, status: str) -> None:
        self.line = line
        self.status = status

    def request_json(self, _: str):
        return {"findings": [{
            "path": "src/math.py", "line": self.line, "status": self.status,
            "risk": "low", "reason": "The change supports the requested behavior.",
        }]}


class RowsReviewer:
    def __init__(self, rows):
        self.rows = rows

    def request_json(self, _: str):
        return {"findings": self.rows}


class ScopeAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "project"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        (self.repository / "src").mkdir()
        (self.repository / "src/math.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        self.base = self._commit("src/math.py")

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()

    def _commit(self, *paths: str) -> str:
        self._git("add", "--", *paths)
        self._git("commit", "-qm", "sample")
        return self._git("rev-parse", "HEAD")

    def _normal_change(self) -> str:
        (self.repository / "src/math.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        return self._commit("src/math.py")

    def _spec(self, **overrides):
        return {
            "requirement": "Change value to return 2.",
            "acceptance_criteria": ["value() returns 2"],
            "allowed_paths": ["src/math.py"],
            "forbidden_paths": [],
            **overrides,
        }

    def _cli(self, target: str, spec: dict, output: Path) -> tuple[int, dict]:
        spec_path = self.root / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        adapter = self.root / "adapter.py"
        adapter.write_text(
            "from pathlib import Path\n"
            "import runpy\n"
            "from agent_eval import ProjectAdapter, NormalizedTrace, Rule\n"
            f"ROOT = Path({str(self.repository)!r})\n"
            "def call(case, context):\n"
            "    value = runpy.run_path(str(ROOT / 'src/math.py'))['value']()\n"
            "    return {'trace_id': context.run_id + case.case_id, 'value': value}\n"
            "ADAPTER = ProjectAdapter('scope-fixture', call, "
            "lambda handle, case: NormalizedTrace(handle['trace_id'], handle, fields=handle), "
            "(Rule('value', 'fields.value', expected='value'),), ())\n",
            encoding="utf-8",
        )
        for suite in ("regression", "smoke"):
            (self.root / f"{suite}.jsonl").write_text(
                json.dumps({"id": suite, "input": {}, "expected": {"value": 2}}) + "\n",
                encoding="utf-8",
            )
        with redirect_stdout(io.StringIO()):
            code = main([
                "audit-scope", "--repository", str(self.repository),
                "--base", self.base, "--target", target,
                "--spec", str(spec_path), "--adapter", str(adapter),
                "--regression", str(self.root / "regression.jsonl"),
                "--smoke", str(self.root / "smoke.jsonl"),
                "--db", str(self.root / "evaluation.db"),
                "--output", str(output),
            ])
        return code, json.loads((output / "scope_audit.json").read_text(encoding="utf-8"))

    def test_normal_change_runs_selected_tests_and_writes_report(self) -> None:
        target = self._normal_change()
        output = self.root / "normal-audit"
        code, report = self._cli(target, self._spec(), output)
        self.assertEqual(code, 0)
        self.assertEqual(report["conclusion"], "no_obvious_issue")
        self.assertEqual(report["findings"][0]["status"], "related")
        self.assertEqual(report["test_selection"]["suites"], ["regression", "smoke"])
        self.assertEqual(report["tests"]["status"], "passed")
        self.assertEqual([run["case_count"] for run in report["tests"]["runs"]], [1, 1])
        self.assertTrue((output / "tests/regression/report.md").is_file())
        self.assertIn("+    return 2", (output / "diff.patch").read_text(encoding="utf-8"))
        self.assertIn("no_obvious_issue", (output / "report.md").read_text(encoding="utf-8"))

    def test_explicitly_forbidden_change_is_reported_even_when_tests_pass(self) -> None:
        self._normal_change()
        (self.repository / "src/extra.py").write_text("debug_export = True\n", encoding="utf-8")
        target = self._commit("src/extra.py")
        code, report = self._cli(
            target, self._spec(forbidden_paths=["src/extra.py"]), self.root / "forbidden-audit"
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["tests"]["status"], "passed")
        self.assertEqual(report["conclusion"], "clear_out_of_scope")
        finding = next(item for item in report["findings"] if item["path"] == "src/extra.py")
        self.assertEqual(finding["status"], "clear_out_of_scope")
        self.assertEqual(finding["line"], 1)

    def test_new_unrequested_public_behavior_is_suspected(self) -> None:
        (self.repository / "src/math.py").write_text(
            "def value():\n    return 2\n\ndef export_all():\n    return True\n", encoding="utf-8"
        )
        target = self._commit("src/math.py")
        result = audit_scope(read_git_diff(self.repository, self.base, target), self._spec())
        self.assertEqual(result["findings"][0]["status"], "suspected_out_of_scope")
        self.assertEqual(result["findings"][0]["line"], 4)

    def test_related_cross_module_change_is_allowed(self) -> None:
        (self.repository / "src/math.py").write_text(
            "from helper import factor\n\ndef value():\n    return factor\n", encoding="utf-8"
        )
        (self.repository / "src/helper.py").write_text("factor = 2\n", encoding="utf-8")
        target = self._commit("src/math.py", "src/helper.py")
        result = audit_scope(
            read_git_diff(self.repository, self.base, target),
            self._spec(allowed_paths=["src/math.py", "src/helper.py"],
                       acceptance_criteria=["value() returns 2", "helper factor is 2"]),
        )
        self.assertEqual({item["status"] for item in result["findings"]}, {"related"})

    def test_missing_requirement_empty_diff_and_invalid_model_line_stay_uncertain(self) -> None:
        empty = read_git_diff(self.repository, self.base, self.base)
        self.assertEqual(empty.files, ())
        _, empty_report = self._cli(self.base, self._spec(), self.root / "empty-audit")
        self.assertEqual(empty_report["conclusion"], "insufficient_evidence")
        self.assertEqual(empty_report["tests"]["status"], "not_run")
        target = self._normal_change()
        snapshot = read_git_diff(self.repository, self.base, target)
        self.assertEqual(select_tests(snapshot=snapshot).changed_files, 1)
        result = audit_scope(snapshot, self._spec(requirement="", acceptance_criteria=[]), UncertainReviewer())
        self.assertEqual(result["findings"][0]["status"], "insufficient_evidence")
        self.assertEqual(result["findings"][0]["source"], "rules")
        self.assertTrue(result["warnings"])
        uncertain = audit_scope(snapshot, self._spec(allowed_paths=[]), UncertainReviewer())
        self.assertEqual(uncertain["findings"][0]["status"], "insufficient_evidence")
        self.assertTrue(uncertain["warnings"])
        uncertain = audit_scope(snapshot, self._spec(), FixedReviewer(2, "insufficient_evidence"))
        self.assertEqual(uncertain["findings"][0]["status"], "insufficient_evidence")
        self.assertEqual(uncertain["findings"][0]["source"], "ai")
        missing = audit_scope(snapshot, self._spec(acceptance_criteria=[" "]), FixedReviewer(2, "related"))
        self.assertEqual(missing["findings"][0]["status"], "insufficient_evidence")
        self.assertEqual(missing["semantic_review"], "skipped")
        with self.assertRaises(ValueError):
            audit_scope(snapshot, self._spec(requirement=None))

    def test_model_can_supply_semantics_but_cannot_silently_downgrade_a_rule(self) -> None:
        target = self._normal_change()
        snapshot = read_git_diff(self.repository, self.base, target)
        semantic = audit_scope(snapshot, self._spec(allowed_paths=[]), FixedReviewer(2, "related"))
        self.assertEqual(semantic["findings"][0]["source"], "ai")
        self.assertEqual(semantic["findings"][0]["status"], "related")

        (self.repository / "src/math.py").write_text(
            "def value():\n    return 2\n\ndef export_all():\n    return True\n", encoding="utf-8"
        )
        extended = self._commit("src/math.py")
        conflict = audit_scope(
            read_git_diff(self.repository, self.base, extended), self._spec(),
            FixedReviewer(4, "related"),
        )
        self.assertEqual(conflict["findings"][0]["status"], "suspected_out_of_scope")
        self.assertIn("related", [item["status"] for item in conflict["findings"]])
        self.assertTrue(any("disagree" in warning for warning in conflict["warnings"]))

    def test_model_findings_keep_distinct_changed_lines_and_reject_context(self) -> None:
        (self.repository / "src/math.py").write_text(
            "def value():\n    result = 2\n    return result\n", encoding="utf-8"
        )
        target = self._commit("src/math.py")
        snapshot = read_git_diff(self.repository, self.base, target)
        def row(line, side, status, reason):
            return {"path": "src/math.py", "line": line, "line_side": side,
                    "status": status, "risk": "medium", "reason": reason}
        first = row(2, "target", "clear_out_of_scope", "Independent behavior change.")
        rows = [first, row(3, "target", "related", "Requested update."), dict(first),
                row(1, "target", "clear_out_of_scope", "Context is not a change."),
                row(2, "base", "clear_out_of_scope", "Deleted old behavior."),
                row(2, "target", "suspected_out_of_scope", "Different concern on the same line."),
                row(3, "base", "clear_out_of_scope", "Wrong side is not a change."),
                row(2, [], "clear_out_of_scope", "Malformed side must be rejected.")]
        result = audit_scope(snapshot, self._spec(), RowsReviewer(rows))
        self.assertEqual([(item["line_side"], item["line"], item["status"])
                          for item in result["findings"]],
                         [("target", 2, "clear_out_of_scope"), ("target", 3, "related"),
                          ("base", 2, "clear_out_of_scope"), ("target", 2, "suspected_out_of_scope")])
        trace = result["model_finding_trace"]
        self.assertEqual([item["disposition"] for item in trace],
                         ["retained", "retained", "merged_duplicate", "rejected", "retained", "retained",
                          "rejected", "rejected"])
        self.assertEqual((trace[0]["final_finding_index"], trace[2]["final_finding_index"]), (1, 1))
        self.assertIsNone(trace[3]["final_finding_index"])
        self.assertIn("No changed target line 1", trace[3]["reason"])
        self.assertIn("No changed base line 3", trace[6]["reason"])
        self.assertIn("Line side must be base or target", trace[7]["reason"])
        result.update(conclusion="review_required", test_selection={"mode": "smoke", "suites": ["smoke"]},
                      tests={"status": "not_run", "runs": []}, uncovered_risks=[])
        output = self.root / "model-trace"
        write_scope_audit_artifacts(result, output, snapshot.raw_diff)
        report = (output / "report.md").read_text(encoding="utf-8")
        self.assertIn("Model finding decisions", report)
        self.assertIn("No changed target line 1", report)
        self.assertIn("scope finding #1", report)

    def test_deleted_line_uses_base_location(self) -> None:
        (self.repository / "src/math.py").write_text("", encoding="utf-8")
        target = self._commit("src/math.py")
        result = audit_scope(read_git_diff(self.repository, self.base, target),
                             self._spec(forbidden_paths=["src/math.py"]))
        finding = result["findings"][0]
        self.assertEqual((finding["line_side"], finding["line"]), ("base", 1))
        self.assertEqual(finding["status"], "clear_out_of_scope")

    def test_unavailable_adapter_and_dirty_checkout_are_reported(self) -> None:
        from unittest.mock import patch

        target = self._normal_change()
        with patch("agent_eval.cli.load_adapter", side_effect=ValueError("invalid adapter")):
            code, report = self._cli(target, self._spec(), self.root / "adapter-error")
        self.assertEqual(code, 2)
        self.assertEqual(report["tests"]["status"], "not_run")
        self.assertTrue(any("invalid adapter" in risk for risk in report["uncovered_risks"]))

        (self.repository / "src/math.py").write_text("def value():\n    return 3\n", encoding="utf-8")
        code, report = self._cli(target, self._spec(), self.root / "dirty-audit")
        self.assertEqual(code, 2)
        self.assertEqual(report["tests"]["status"], "not_run")
        self.assertTrue(any("uncommitted" in risk for risk in report["uncovered_risks"]))

        failed_target = self._commit("src/math.py")
        code, report = self._cli(failed_target, self._spec(), self.root / "failed-tests")
        self.assertEqual(code, 2)
        self.assertEqual(report["tests"]["status"], "failed")
        self.assertEqual(report["conclusion"], "review_required")


if __name__ == "__main__":
    unittest.main()
