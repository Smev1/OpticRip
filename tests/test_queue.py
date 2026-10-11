"""Phase 6: batch queue, multi-drive scheduling, cancellation and
failure isolation with real (ffmpeg-backed) rips."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from opticrip.core.config import Settings
from opticrip.core.models import (
    Drive,
    DriveKind,
    MediaType,
    RipJob,
    TitleSelection,
    new_id,
)
from opticrip.disc.dvd import DvdIfoAnalyzer
from opticrip.disc.fs import open_disc_filesystem
from opticrip.rip.engine import RipEngine
from opticrip.rip.queue import JobQueue, QueueEvent


@pytest.fixture
def analysed(dvd_fixture: str):
    fs = open_disc_filesystem(dvd_fixture)
    drive = Drive(path=dvd_fixture, kind=DriveKind.FOLDER,
                  media_type=MediaType.DVD_VIDEO)
    return drive, DvdIfoAnalyzer().analyze(drive, fs)


def make_job(drive: Drive, analysis, out: str, *, label: str = "j") -> RipJob:
    title = analysis.titles[0]
    return RipJob(
        id=f"{label}-{new_id()}", drive_device=drive.path, title=title,
        selection=TitleSelection.all_streams(title),
        output_path=out, drive_kind=DriveKind.FOLDER,
        media_type=MediaType.DVD_VIDEO,
        disc_label=analysis.disc.label)


@pytest.mark.requires_ffmpeg
class TestBatch:
    def test_three_jobs_all_complete(self, analysed, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        s = Settings().normalise(); s.allow_parallel_drives = False
        queue = JobQueue(s)
        queue.start()
        try:
            for i in range(3):
                queue.add(make_job(drive, analysis,
                                   str(tmp_path / f"n{i}.mkv"),
                                   label=f"j{i}"))
            assert queue.join(timeout=60), "queue must drain"
            for i in range(3):
                result = queue.result(queue._jobs[f"j{i}-{queue._jobs.keys().__iter__().__next__()}"].id) if False else None
            # verify outputs and results
            for jid, job in queue._jobs.items():
                res = queue.result(jid)
                assert res is not None and res.ok, f"{jid} failed: {job.error}"
                assert os.path.exists(res.output_path)
                assert os.path.getsize(res.output_path) > 0
        finally:
            queue.stop()

    def test_same_drive_jobs_run_serially(self, analysed, tmp_path,
                                          have_ffmpeg):
        """Two jobs on one drive must NOT overlap (drive can't seek twice)."""
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        s = Settings().normalise(); s.allow_parallel_drives = False
        overlaps: list[bool] = []
        def log_job(job_id):
            start, end = None, None
            def rec(ev: QueueEvent):
                nonlocal start, end
                if ev.job_id == job_id and ev.kind == "started":
                    start = time.time()
                elif ev.job_id == job_id and ev.kind == "result":
                    end = time.time()
            return rec, (lambda: (start, end))
        queue = JobQueue(s)
        queue.start()
        try:
            j1 = make_job(drive, analysis, str(tmp_path / "a.mkv"), label="a")
            j2 = make_job(drive, analysis, str(tmp_path / "b.mkv"), label="b")
            spans = {}
            def ev(ev_: QueueEvent):
                if ev_.kind == "started":
                    spans.setdefault(ev_.job_id, {})["start"] = time.time()
                elif ev_.kind == "result":
                    spans.setdefault(ev_.job_id, {})["end"] = time.time()
            queue.subscribe(ev)
            queue.add(j1); queue.add(j2)
            queue.join(timeout=60)
            a, b = spans.get(j1.id, {}), spans.get(j2.id, {})
            assert a and b, "both jobs must run"
            # serial: a's end <= b's start or vice versa (no overlap)
            overlap = not (a["end"] <= b["start"] + 0.01 or
                           b["end"] <= a["start"] + 0.01)
            assert not overlap, f"job spans overlap: {a} vs {b}"
        finally:
            queue.stop()

    def test_cancel_queued_job_leaves_it_skipped(self, analysed, tmp_path,
                                                have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        s = Settings().normalise(); s.allow_parallel_drives = False
        queue = JobQueue(s)
        queue.start()
        try:
            j_run = make_job(drive, analysis, str(tmp_path / "run.mkv"),
                             label="run")
            j_q = make_job(drive, analysis, str(tmp_path / "queued.mkv"),
                           label="q")
            queue.add(j_run)
            queue.add(j_q)
            queue.cancel(j_q.id)          # not started yet
            assert queue.join(timeout=30)
            res = queue.result(j_q.id)
            assert res is not None and not res.ok
            assert j_q.status.label == "Skipped"
            assert not (tmp_path / "queued.mkv").exists()
            # the running job still finished
            assert queue.result(j_run.id).ok
        finally:
            queue.stop()

    def test_failure_isolation_bad_job_keeps_batch_going(self, analysed,
                                                        tmp_path, have_ffmpeg):
        """One failing job (existing target, overwrite=error) must not
        poison the rest of the batch."""
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        existing = tmp_path / "exists.mkv"
        existing.write_bytes(b"stale block")   # job will refuse to overwrite
        s = Settings().normalise()
        s.allow_parallel_drives = False; s.read_retries = 0
        s.overwrite_policy = "error"
        queue = JobQueue(s)
        queue.start()
        try:
            bad = make_job(drive, analysis, str(existing), label="bad")
            good = make_job(drive, analysis, str(tmp_path / "good.mkv"),
                            label="good")
            queue.add(bad); queue.add(good)
            assert queue.join(timeout=60)
            bres, gres = queue.result(bad.id), queue.result(good.id)
            assert not bres.ok, "job onto existing file must fail"
            assert gres.ok, f"good job must still succeed: {gres}"

            print(gres) if False else None
            assert os.path.exists(gres.output_path)
            assert os.path.getsize(gres.output_path) > 0
            # the failed job left the stale file untouched
            assert existing.read_bytes() == b"stale block"
        finally:
            queue.stop()

    def test_events_flow_to_subscribers(self, analysed, tmp_path, have_ffmpeg):
        if not have_ffmpeg:
            pytest.skip("no ffmpeg")
        drive, analysis = analysed
        s = Settings().normalise(); s.allow_parallel_drives = False
        queue = JobQueue(s)
        events: list[QueueEvent] = []
        queue.subscribe(events.append)
        queue.start()
        try:
            queue.add(make_job(drive, analysis, str(tmp_path / "e.mkv"),
                               label="e"))
            assert queue.join(timeout=60)
            kinds = [e.kind for e in events]
            for expected in ("added", "started", "progress", "result"):
                assert expected in kinds, f"missing {expected} event"
        finally:
            queue.stop()

    def test_max_workers_clamped(self):
        s = Settings().normalise(); s.max_parallel_jobs = 99
        q = JobQueue(s)
        assert q.max_workers <= 8, "config clamps to [1, 8]"
