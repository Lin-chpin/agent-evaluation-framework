from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import OutputLimitExceeded, run_agent_process
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
                    timeout_seconds=1,
                )
        self.assertIn("started", raised.exception.output)

    def test_limits_captured_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OutputLimitExceeded) as raised:
                run_agent_process(
                    [
                        sys.executable,
                        "-c",
                        "import sys, time; print('o' * 100, flush=True); time.sleep(5)",
                    ],
                    Path(directory),
                    max_output_bytes=16,
                )

        self.assertEqual(raised.exception.stream, "stdout")
        self.assertEqual(raised.exception.stdout, "o" * 16 + TRUNCATED_OUTPUT_MARKER)

    def test_timeout_includes_inherited_output_pipes(self) -> None:
        code = (
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)']); "
            "print(child.pid, flush=True)"
        )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            run_agent_process(
                [sys.executable, "-c", code], Path(__file__).parent, timeout_seconds=1
            )
        self.assertLess(time.monotonic() - started, 3)
        if sys.platform == "win32":
            child_pid = raised.exception.output.strip()
            processes = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV"],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertNotIn(f'","{child_pid}","', processes)


if __name__ == "__main__":
    unittest.main()
