import threading
import time

import pytest

from web.jobs import JobRunner


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def status(runner, job_id):
    return runner.get(job_id)["status"]


@pytest.fixture
def runner():
    return JobRunner(keep=20)


def test_job_runs_and_returns_result(runner):
    job_id = runner.submit("t", {"a": 1}, lambda emit: 42)
    wait_until(lambda: status(runner, job_id) == "done")
    job = runner.get(job_id)
    assert job["result"] == 42 and job["error"] is None and job["params"] == {"a": 1}


def test_progress_and_partial_are_recorded(runner):
    release = threading.Event()

    def fn(emit):
        emit("live", {"enabled": False})
        emit("ticker", {"stage": 1, "i": 3, "n": 9, "symbol": "X"})
        emit("stage1_done", {"rows": [1]})
        emit("stage1_row", {"ignored": True})
        release.wait(5)
        return "ok"

    job_id = runner.submit("t", {}, fn)
    wait_until(lambda: runner.get(job_id)["partial"].get("stage1") is not None)
    job = runner.get(job_id)
    assert job["status"] == "running"
    assert job["progress"] == {"stage": 1, "i": 3, "n": 9, "symbol": "X"}
    assert job["partial"] == {"live": {"enabled": False}, "stage1": {"rows": [1]}}
    release.set()
    wait_until(lambda: status(runner, job_id) == "done")


def test_failure_is_captured(runner):
    def boom(emit):
        raise ValueError("boom")

    job_id = runner.submit("t", {}, boom)
    wait_until(lambda: status(runner, job_id) == "failed")
    assert runner.get(job_id)["error"] == "ValueError: boom"
    ok = runner.submit("t", {}, lambda emit: 1)          # worker survived
    wait_until(lambda: status(runner, ok) == "done")


def test_cancel_running_job_stops_at_next_emit(runner):
    started, steps = threading.Event(), []

    def fn(emit):
        for i in range(500):
            emit("ticker", {"stage": 1, "i": i, "n": 500, "symbol": "X"})
            steps.append(i)
            started.set()
            time.sleep(0.01)
        return "finished"

    job_id = runner.submit("t", {}, fn)
    started.wait(5)
    assert runner.cancel(job_id) is True
    wait_until(lambda: status(runner, job_id) == "cancelled")
    assert len(steps) < 500 and runner.get(job_id)["result"] is None


def test_cancel_queued_job_never_runs(runner):
    release, ran = threading.Event(), []
    first = runner.submit("t", {}, lambda emit: release.wait(5))
    second = runner.submit("t", {}, lambda emit: ran.append("second"))
    assert runner.cancel(second) is True
    assert status(runner, second) == "cancelled"
    release.set()
    third = runner.submit("t", {}, lambda emit: "third")
    wait_until(lambda: status(runner, third) == "done")
    assert status(runner, first) == "done" and ran == []


def test_jobs_run_in_fifo_order(runner):
    order = []
    ids = [runner.submit("t", {}, lambda emit, n=n: order.append(n)) for n in range(5)]
    wait_until(lambda: all(status(runner, i) == "done" for i in ids))
    assert order == [0, 1, 2, 3, 4]


def test_latest_returns_running_job(runner):
    release = threading.Event()
    runner.submit("shortlist", {}, lambda emit: "old")
    newest = runner.submit("shortlist", {}, lambda emit: release.wait(5))
    runner.submit("other", {}, lambda emit: None)
    wait_until(lambda: status(runner, newest) == "running")
    assert runner.latest("shortlist")["id"] == newest
    assert runner.latest("nope") is None
    release.set()


def test_old_finished_jobs_are_pruned():
    runner = JobRunner(keep=2)
    ids = []
    for n in range(3):
        ids.append(runner.submit("t", {}, lambda emit: None))
        wait_until(lambda: status(runner, ids[-1]) == "done")
    assert runner.get(ids[0]) is None
    assert runner.get(ids[1]) is not None and runner.get(ids[2]) is not None


def test_cancel_unknown_or_finished_returns_false(runner):
    job_id = runner.submit("t", {}, lambda emit: 1)
    wait_until(lambda: status(runner, job_id) == "done")
    assert runner.cancel(job_id) is False
    assert runner.cancel("missing") is False
