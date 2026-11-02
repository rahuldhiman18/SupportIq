"""Tests for the SOLID worker lifecycle boundary."""

import threading

from app.services.worker import WorkerRuntime


class FakeWorkerTask:
    """A replaceable task implementation for the runtime contract."""

    def __init__(self, stop_event: threading.Event) -> None:
        self.calls = 0
        self.stop_event = stop_event

    def run_once(self) -> bool:
        self.calls += 1
        self.stop_event.set()
        return True


def test_worker_runtime_accepts_a_replaceable_task() -> None:
    stop_event = threading.Event()
    task = FakeWorkerTask(stop_event)

    WorkerRuntime(task, poll_seconds=0).run_forever(stop_event)

    assert task.calls == 1
