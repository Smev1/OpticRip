"""Batch job queue with multi-drive scheduling.

Design:

* The queue holds :class:`RipJob`s grouped by **drive device**.
* Each drive gets *serial* execution (an optical drive cannot seek to two
  places at once), while different drives proceed in parallel when
  ``Settings.allow_parallel_drives`` is set.
* A bounded worker pool (``max_parallel_jobs`` = concurrent *drives*) is
  created lazily; every job is at most a thread's work item.
* Cancellation is cooperative: ``cancel(job_id)`` marks the job so the
  running engine finishes its current chunk and stops; queued jobs are
  simply dequeued and marked ``SKIPPED``.
* Events fan out through :class:`QueueListener` callbacks so a GUI log or
  progress panel can subscribe without polling.
* Failure isolation is explicit: one job's exception/retry exhaustion marks
  only that job ``FAILED``; the queue proceeds with the rest.

This module contains no Qt and no ffmpeg specifics - it orchestrates
:class:`RipEngine` instances, one per job (engines are cheap state bags).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core import errors as E
from ..core.config import Settings
from ..core.logging_bus import LogBus
from ..core.models import (
    JobStatus,
    ProgressUpdate,
    RipJob,
    RipResult,
)
from .engine import RipEngine


@dataclass
class QueueEvent:
    """Something a subscriber may want to know about."""

    kind: str                 # "added" | "started" | "progress" | "result"
    job_id: str
    message: str = ""
    update: ProgressUpdate | None = None
    result: RipResult | None = None

    @property
    def is_terminal(self) -> bool:
        return self.kind == "result"


class JobQueue:
    """Ordered, cancellable, multi-drive-aware batch queue."""

    def __init__(self, settings: Settings, *,
                 log: LogBus | None = None,
                 engine_factory: Callable[[], RipEngine] | None = None) -> None:
        self.settings = settings
        self.log = (log or LogBus()).bind("queue")
        self._engine_factory = (engine_factory or
                                (lambda: RipEngine(self.settings)))
        self._jobs: dict[str, RipJob] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._cancel_requested: set[str] = set()
        self._listeners: list[Callable[[QueueEvent], None]] = []
        self._result_events: dict[str, threading.Event] = {}
        self._results: dict[str, RipResult] = {}
        self._workers: list[threading.Thread] = []
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._max_workers: int = 0

    # ------------------------------------------------------------------ #
    @property
    def max_workers(self) -> int:
        """How many drives may work at once."""
        if self._max_workers:
            return max(1, self._max_workers)
        if not self.settings.allow_parallel_drives:
            return 1
        return min(8, max(1, int(self.settings.max_parallel_jobs)))

    @max_workers.setter
    def max_workers(self, value: int) -> None:
        self._max_workers = max(1, int(value))

    # -- subscriptions -------------------------------------------------- #
    def subscribe(self, callback: Callable[[QueueEvent], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def dispose() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return dispose

    def _emit(self, event: QueueEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a bad listener never breaks the queue
                self.log.error("queue listener raised", context="queue")

    # -- job management -------------------------------------------------- #
    def add(self, job: RipJob) -> None:
        with self._lock:
            if job.id in self._jobs:
                raise ValueError(f"job {job.id} already queued")
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._result_events[job.id] = threading.Event()
        self._emit(QueueEvent(kind="added", job_id=job.id,
                              message=job.display_name))
        self.log.info(f"queued: {job.display_name} <- {job.drive_device}")
        self._wake.set()

    def cancel(self, job_id: str) -> None:
        """Request cancellation of a queued or running job.

        Queued jobs are dequeued immediately; running jobs are told the
        engine via ``request_cancel`` and finish their current chunk.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            status = job.status
        if status.is_terminal:
            return
        self._cancel_requested.add(job_id)
        with self._lock:
            if job_id in self._order:
                self._order.remove(job_id)
                job = self._jobs.pop(job_id)
                job.status = JobStatus.SKIPPED
                job.finished_at = time.time()
                self._results[job_id] = RipResult(job_id=job_id, ok=False,
                                                  output_path="",
                                                  warnings=["cancelled before start"])
                self._result_events[job_id].set()
        self._emit(QueueEvent(kind="result", job_id=job_id,
                              message="skipped (cancelled before start)"))
        self.log.info(f"cancelled: {job_id}")

    def jobs(self) -> list[RipJob]:
        with self._lock:
            return [self._jobs[jid] for jid in self._order
                    if not self._jobs[jid].status.is_terminal]

    def size(self) -> int:
        return len(self.jobs())

    def result(self, job_id: str) -> RipResult | None:
        return self._results.get(job_id)

    def wait(self, job_id: str, timeout: float | None = None) -> RipResult | None:
        """Block until one job finishes (or timeout). Returns its result."""
        ev = self._result_events.get(job_id)
        if ev is None:
            return None
        ev.wait(timeout=timeout)
        return self._results.get(job_id)

    # -- orchestration -------------------------------------------------- #
    def run_forever(self) -> None:
        """Serialised-per-drive, parallel-per-drive scheduler loop.

        Spawns one *dispatcher* thread that takes the next eligible job by
        drive occupancy; workers run the engine synchronously.
        """
        workers = self.max_workers
        self.log.info(f"starting queue with {workers} concurrent "
                      f"drive worker(s)")
        threads = []
        for i in range(workers):
            t = threading.Thread(target=self._worker_loop, name=f"optiworker-{i}",
                                 daemon=True)
            threads.append(t)
            t.start()
        self._workers = threads

    def start(self) -> None:
        self._shutdown.clear()
        self.run_forever()

    def is_running(self) -> bool:
        """True once ``start`` has spawned workers and ``stop`` has not."""
        return bool(self._workers) and not self._shutdown.is_set()

    def pending_count(self) -> int:
        """Jobs that are queued or running (non-terminal) right now."""
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if not j.status.is_terminal)

    def stop(self, wait: bool = True, timeout: float = 30.0) -> None:
        """Graceful stop: queued jobs stay queued; running ones finish."""
        self._shutdown.set()
        self._wake.set()
        if wait:
            deadline = time.time() + timeout
            for t in self._workers:
                t.join(max(0.0, deadline - time.time()))

    def join(self, timeout: float | None = None) -> bool:
        """Wait until every added job reaches a terminal status."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            with self._lock:
                pending = [jid for jid in self._order
                           if not self._jobs[jid].status.is_terminal]
                running = [jid for jid, job in self._jobs.items()
                           if job.status in (JobStatus.RUNNING,
                                             JobStatus.CANCELLING)]
            if not pending and not running:
                return True
            if deadline is not None and time.time() > deadline:
                return False
            time.sleep(0.05)

    # -- internals -------------------------------------------------------- #
    def _cancelled(self, job_id: str) -> bool:
        if job_id in self._cancel_requested:
            return True
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.status is JobStatus.CANCELLING)

    def _claim_next(self, drives_in_use: set[str]) -> RipJob | None:
        """Pick the first queued job whose drive is idle."""
        with self._lock:
            for jid in list(self._order):
                job = self._jobs.get(jid)
                if job is None or job.status is not JobStatus.QUEUED:
                    continue
                if job.drive_device in drives_in_use:
                    continue
                self._order.remove(jid)
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                return job
        return None

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            self._wake.wait(timeout=0.2)
            self._wake.clear()
            # snapshot of currently-busy drives
            with self._lock:
                busy = {j.drive_device for j in self._jobs.values()
                        if j.status is JobStatus.RUNNING}
            job = self._claim_next(busy)
            if job is None:
                continue
            self._run_job(job)
            self._wake.set()   # nudge other workers for the next queued item
            if self.jobs() or any(j.status is JobStatus.RUNNING
                                  for j in self._jobs.values()):
                continue

    def _run_job(self, job: RipJob) -> None:
        """Run one job to completion, absorbing every *expected* error.

        A job can never poison the batch: engine errors become a FAILED
        result.  Only :class:`OutputExistsError` under ``ask`` replays to
        the caller (the GUI shows a prompt; the CLI re-raises).  A raising
        subscriber/handler still cannot take the worker down uncaught.
        """
        self._emit(QueueEvent(kind="started", job_id=job.id,
                              message=job.display_name))
        engine = self._engine_factory()
        attempts_left = max(0, int(getattr(self.settings, "read_retries", 0)))
        result: RipResult | None = None
        try:
            while True:
                last_exc: Exception | None = None
                try:
                    result = engine.run(
                        job,
                        progress=lambda u, jid=job.id: self._emit(
                            QueueEvent(kind="progress", job_id=jid,
                                       update=u)),
                        cancelled=lambda jid=job.id: self._cancelled(jid))
                except E.DiscReadError as exc:
                    # transient-looking media error: retry within budget
                    last_exc = exc
                    result = None
                if result is not None and (result.ok or not result.warnings):
                    break
                if last_exc is None and result is not None and result.ok:
                    break
                if last_exc is None:
                    # fatal failure (bad selection, no space, ...) - no retry
                    break
                if attempts_left <= 0 or self._cancelled(job.id):
                    job.status = JobStatus.FAILED
                    job.finished_at = time.time()
                    job.error_title = last_exc.title
                    job.error = last_exc.message
                    job.error_hint = getattr(last_exc, "hint", None)
                    result = RipResult(job_id=job.id, ok=False,
                                       output_path="",
                                       warnings=[last_exc.message])
                    break
                attempts_left -= 1
                self.log.warning(f"transient disc-read failure on "
                                 f"{job.display_name}; retrying "
                                 f"({attempts_left} attempt(s) left)")
                time.sleep(0.5)
        except E.CancelledError:
            job.status = JobStatus.CANCELLED
            result = RipResult(job_id=job.id, ok=False, output_path="",
                               warnings=["cancelled"])
        except E.OutputExistsError as exc:
            if self.settings.overwrite_policy == "ask":
                # "ask" must reach the caller so it can prompt; the queue is
                # only the wrong layer when nothing answers, so surface it.
                job.status = JobStatus.FAILED
                job.finished_at = time.time()
                raise
            # any other policy ("error"/"skip"/...) is a normal failed job
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            job.error_title = getattr(exc, "title", "Error")
            job.error = getattr(exc, "message", str(exc))
            job.error_hint = getattr(exc, "hint", None)
            result = RipResult(job_id=job.id, ok=False, output_path="",
                               warnings=[job.error])
        except Exception as exc:                       # noqa: BLE001
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            job.error_title = getattr(exc, "title", "Error")
            job.error = getattr(exc, "message", str(exc))
            job.error_hint = getattr(exc, "hint", None)
            result = RipResult(job_id=job.id, ok=False, output_path="",
                               warnings=[job.error])
            self.log.error(f"job crashed: {job.error}")
        if result is None:                             # pragma: no cover
            job.status = JobStatus.FAILED
            result = RipResult(job_id=job.id, ok=False, output_path="")

        self._results[job.id] = result
        self._result_events[job.id].set()
        self._cancel_requested.discard(job.id)
        self.log.info(f"{job.status.label}: {job.display_name}")
        self._emit(QueueEvent(kind="result", job_id=job.id,
                              message=f"{job.status.label}",
                              result=result))
