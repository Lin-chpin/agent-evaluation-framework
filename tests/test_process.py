from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import run_agent_process
from agent_eval.process import TRUNCATED_OUTPUT_MARKER


class AgentProcessTest(unittest.TestCase):
    def test_captures_output_from_an_explicit_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_agent_process(
                [sys.executable, "-c", "from pathlib import Path; print(Path.cwd().name)"],
                Path(directory),
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), Path(directory).name)

    def test_terminates_a_timed_out_agent_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_agent_process(
                    [
                        sys.executable,
                        "-c",
                        "import time; print('started', flush=True); time.sleep(5)",
                    ],
                    Path(directory),
                    timeout_seconds=0.05,
                )
        self.assertIn("started", raised.exception.output)

    def test_limits_captured_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_agent_process(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('o' * 100); print('e' * 100, file=sys.stderr)",
                ],
                Path(directory),
                max_output_bytes=16,
            )

        self.assertEqual(result.stdout, "o" * 16 + TRUNCATED_OUTPUT_MARKER)
        self.assertEqual(result.stderr, "e" * 16 + TRUNCATED_OUTPUT_MARKER)


if __name__ == "__main__":
    unittest.main()
