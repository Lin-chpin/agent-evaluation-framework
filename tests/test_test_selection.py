from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_eval.diff_analysis import DiffSnapshot, assess_rules
from agent_eval.test_selection import select_tests


class FakeReviewer:
    def __init__(self, result: dict[str, object], base_url: str) -> None:
        self.result = result
        self.base_url = base_url
        self.prompt = ""

    def request_json(self, prompt: str) -> dict[str, object]:
        self.prompt = prompt
        return self.result


def snapshot(*files: str) -> DiffSnapshot:
    from agent_eval.diff_analysis import _category

    categories: dict[str, int] = {}
    for path in files:
        category = _category(path)
        categories[category] = categories.get(category, 0) + 1
    return DiffSnapshot("secret-value", files, 1, 0, categories, {".py": len(files)}, tuple(categories))


class RuleAssessmentTests(unittest.TestCase):
    def test_docs_and_ui_use_smoke(self) -> None:
        self.assertEqual(assess_rules(snapshot("README.md", "ui/page.css")).mode, "smoke")

    def test_prompt_change_is_high_risk_regression(self) -> None:
        result = assess_rules(snapshot("prompts/planner.txt"))
        self.assertEqual(result.mode, "regression")
        self.assertEqual(result.risk, "high")

    def test_rag_change_uses_full(self) -> None:
        self.assertEqual(assess_rules(snapshot("src/rag/retriever.py")).mode, "full")


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        subprocess.run(["git", "-C", str(self.repository), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repository), "config", "user.name", "Test"], check=True)
        (self.repository / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repository), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-qm", "baseline"], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_local_ai_receives_raw_diff(self) -> None:
        (self.repository / "README.md").write_text("secret-value\n", encoding="utf-8")
        reviewer = FakeReviewer(
            {"mode": "smoke", "confidence": 0.9, "risk": "low", "reasons": ["docs"]},
            "http://127.0.0.1:11434/v1",
        )
        result = select_tests(self.repository, ai_provider="local", reviewer=reviewer)
        self.assertIn("secret-value", reviewer.prompt)
        self.assertFalse(result.human_review_required)

    def test_remote_ai_receives_only_sanitized_summary(self) -> None:
        private_file = self.repository / "private-secret.py"
        private_file.write_text("API_KEY = 'secret-value'\n", encoding="utf-8")
        reviewer = FakeReviewer(
            {"mode": "regression", "confidence": 0.9, "risk": "medium", "reasons": ["source"]},
            "https://example.com/v1",
        )
        select_tests(self.repository, ai_provider="remote", reviewer=reviewer)
        self.assertNotIn("secret-value", reviewer.prompt)
        self.assertNotIn("private-secret.py", reviewer.prompt)
        self.assertIn("changed_file_count", reviewer.prompt)

    def test_ai_cannot_downgrade_rule_floor(self) -> None:
        path = self.repository / "rag"
        path.mkdir()
        (path / "retriever.py").write_text("changed = True\n", encoding="utf-8")
        reviewer = FakeReviewer(
            {"mode": "smoke", "confidence": 0.95, "risk": "low", "reasons": ["small"]},
            "http://localhost:11434/v1",
        )
        result = select_tests(self.repository, ai_provider="local", reviewer=reviewer)
        self.assertEqual(result.mode, "full")
        self.assertTrue(result.human_review_required)

    def test_remote_raw_diff_is_rejected(self) -> None:
        reviewer = FakeReviewer({}, "https://example.com/v1")
        with self.assertRaisesRegex(ValueError, "sanitized summary"):
            select_tests(
                self.repository,
                ai_provider="remote",
                ai_input="raw",
                reviewer=reviewer,
            )


if __name__ == "__main__":
    unittest.main()
