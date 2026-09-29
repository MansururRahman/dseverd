"""In-memory job runner for long shortlist runs.

One daemon worker thread runs jobs FIFO, so only one job ever fetches from DSE.
A job is `fn(emit)`; emit(event, data) records progress/partial results and
raises Cancelled once cancellation was requested, which stops the job at its
next event. State lives in memory only -- a server restart forgets every job.
"""
from __future__ import annotations

import collections
import datetime as dt
import queue
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

ACTIVE = ("queued", "running")


class Cancelled(Exception):
    """Raised from a job's emit() once cancellation has been requested."""


@dataclass
class _Job:
    id: str
    kind: str
    params: dict
    fn: Callable
    status: str = "queued"
    progress: dict | None = None
    partial: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    created: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    cancel_requested: bool = False

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "params": self.params,
                "status": self.status, "progress": self.progress,
                "partial": dict(self.partial), "result": self.result,
                "error": self.error, "created": self.created}


class JobRunner:
    def __init__(self, keep: int = 20):
        self.keep = keep
        self._jobs: collections.OrderedDict[str, _Job] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._queue: queue.Queue[_Job] = queue.Queue()
        threading.Thread(target=self._work, name="job-runner", daemon=True).start()

    def submit(self, kind: str, params: dict, fn: Callable) -> str:
        job = _Job(id=uuid.uuid4().hex[:12], kind=kind, params=params, fn=fn)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
        self._queue.put(job)
        return job.id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def latest(self, kind: str) -> dict | None:
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.kind == kind:
                    return job.snapshot()
        return None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in ACTIVE:
                return False
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
            return True

    def _prune(self) -> None:
        while len(self._jobs) > self.keep:
            oldest = next((j for j in self._jobs.values() if j.status not in ACTIVE), None)
            if oldest is None:
                return
            del self._jobs[oldest.id]

    def _emit(self, job: _Job, event: str, data) -> None:
        if job.cancel_requested:
            raise Cancelled()
        with self._lock:
            if event == "ticker":
                job.progress = dict(data)
            elif event == "live":
                job.partial["live"] = data
            elif event.endswith("_done"):
                job.partial[event[: -len("_done")]] = data

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            with self._lock:
                if job.status == "cancelled":
                    continue
                job.status = "running"
            try:
                result = job.fn(lambda event, data: self._emit(job, event, data))
            except Cancelled:
                with self._lock:
                    job.status = "cancelled"
            except Exception as exc:  # noqa: BLE001 - a failed job must not kill the worker
                traceback.print_exc()
                with self._lock:
                    job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            else:
                with self._lock:
                    job.status, job.result = "done", result
