from __future__ import annotations

import sys
import tempfile
import unittest
import multiprocessing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import ProjectAdapter
from agent_eval.engine import EvaluationEngine
from agent_eval.store import ResultStore
from agent_eval.workspace import TextArtifactWorkspace


def _hold_loop_lock(root: str, ready: object, release: object) -> None:
    workspace = TextArtifactWorkspace(Path(root))
    with workspace.lock_loop("cross-process-loop"):
        ready.set()
        release.wait(10)


def _write_runs(path: str, prefix: str, count: int, start: object) -> None:
    start.wait(10)
    with ResultStore(Path(path)) as store:
        for index in range(count):
            run_id = f"{prefix}-{index}"
            store.start_run(run_id, "adapter", "suite", "online", {})
            store.finish_run(run_id, "passed")


def _hold_run_lock(path: str, ready: object, release: object) -> None:
    with ResultStore(Path(path)) as store:
        with store.lock_run("shared-run"):
            ready.set()
            release.wait(10)


class ConcurrencySafetyTest(unittest.TestCase):
    def test_loop_lock_rejects_a_second_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = TextArtifactWorkspace(Path(directory))

            with workspace.lock_loop("same-loop"):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with workspace.lock_loop("same-loop"):
                        pass

    def test_loop_lock_rejects_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_loop_lock,
                args=(directory, ready, release),
            )
            process.start()
            try:
                self.assertTrue(ready.wait(10))
                workspace = TextArtifactWorkspace(Path(directory))
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with workspace.lock_loop("cross-process-loop"):
                        pass
            finally:
                release.set()
                process.join(10)

            self.assertEqual(process.exitcode, 0)

    def test_store_uses_wal_and_rejects_duplicate_run_without_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.db"
            with ResultStore(path) as first, ResultStore(path) as second:
                first.start_run("run", "adapter", "suite", "online", {})

                with self.assertRaisesRegex(ValueError, "run already exists"):
                    second.start_run("run", "adapter", "suite", "online", {})
                second.start_run("run", "adapter", "suite", "online", {}, resume=True)
                mode = second.connection.execute("PRAGMA journal_mode").fetchone()[0]

            self.assertEqual(mode.lower(), "wal")

    def test_two_processes_write_distinct_runs_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.db"
            with ResultStore(path):
                pass
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            processes = [
                context.Process(target=_write_runs, args=(str(path), prefix, 25, start))
                for prefix in ("a", "b")
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(15)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            with ResultStore(path) as store:
                count = store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            self.assertEqual(count, 50)

    def test_run_lock_rejects_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.db"
            with ResultStore(path):
                pass
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_run_lock,
                args=(str(path), ready, release),
            )
            process.start()
            try:
                self.assertTrue(ready.wait(10))
                with ResultStore(path) as store:
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        with store.lock_run("shared-run"):
                            pass
            finally:
                release.set()
                process.join(10)

            self.assertEqual(process.exitcode, 0)

    def test_adapter_caps_requested_case_concurrency(self) -> None:
        adapter = ProjectAdapter(
            "limited",
            lambda *_: None,
            lambda *_: None,
            (),
            (),
            max_concurrency=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            with ResultStore(Path(directory) / "evaluation.db") as store:
                engine = EvaluationEngine(adapter, store, workers=32)

        self.assertEqual(engine.requested_workers, 32)
        self.assertEqual(engine.workers, 2)
        self.assertEqual(engine.max_in_flight, 4)


if __name__ == "__main__":
    unittest.main()
