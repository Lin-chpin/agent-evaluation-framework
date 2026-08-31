from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import build_container_command, run_agent_container


class ContainerRunnerTest(unittest.TestCase):
    def test_builds_restricted_container_command_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = build_container_command("python:3.12", ["python", "agent.py"], Path(directory))

        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("none", command)
        self.assertIn("ALL", command)
        self.assertIn("no-new-privileges", command)
        self.assertTrue(any(item.endswith(",readonly") for item in command))
        self.assertEqual(command[-3:], ["python:3.12", "python", "agent.py"])

    @patch("agent_eval.container_runner.run_agent_process")
    def test_delegates_timeout_and_workspace_to_process_runner(self, run_process) -> None:
        run_process.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = run_agent_container(
                "python:3.12",
                ["python", "agent.py"],
                workspace,
                timeout_seconds=12,
            )

        self.assertEqual(result.stdout, "ok")
        args, kwargs = run_process.call_args
        self.assertEqual(args[1], workspace)
        self.assertEqual(kwargs["timeout_seconds"], 12)


if __name__ == "__main__":
    unittest.main()
