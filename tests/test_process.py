from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import run_agent_process


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


if __name__ == "__main__":
    unittest.main()
