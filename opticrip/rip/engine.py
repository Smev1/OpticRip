"""The rip engine: orchestrates analyse -> extract -> remux -> validate.

Thin, explicit, and sync-friendly. Each job runs synchronously in a
worker (GUI will wrap it in a thread; the CLI runs it inline). All
state used by the GUI lives in :class:`RipJob` so the caller can poll
or serialise it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

from ..core import errors as E
from ..core.config import Settings
from ..core.interfaces import (
    CancelCheck,
    DiscFileSystem,
    OutputValidator,
    ProgressCallback,
    Remuxer,
    StreamExtractor,
    null_progress,
)
from ..core.logging_bus import LogBus
from ..core.models import (
    ProgressUpdate,
    RipJob,
    RipPhase,
    RipResult,
    StreamKind,
    JobStatus,
    ValidationReport,
    new_id,
    validate_selection,
)
from ..core.naming import resolve_output_path
from .ffmpeg import make_remuxer
from .tools import Toolchain


def normalise_chapter_times(times: list[float] | None, *, title, log) -> list[float] | None:
    """Shift chapter times so the first chapter starts at 0.

    DVD chapters derive from PGC program maps, and cell 0 can hide menu or
    angle padding before the first VOBU that plays. Our cell-range extractor
    copies exactly the chosen cell sectors, so the *content* stream begins
    at what the PGC calls "cell 0 start". If the chapter list does not start
    at 0 we shift it, and log the shift - never silently.
    """
    if not times:
        return times
    first = times[0]
    if abs(first) < 0.001:
        return times
    shifted = [t - first for t in times]
    log.info(f"chapter base skew on {title.display_name}: first chapter "
             f"starts at {first:.3f}s on disc; shifted to 0 in the output")
    return shifted


class RipEngine:
    """Runs one :class:`RipJob` end-to-end. No threading, no callbacks beyond
    ``progress``; orchestration belongs to the caller."""

    def __init__(self, settings: Settings, *,
                 toolchain: Toolchain | None = None,
                 log: LogBus | None = None,
                 extractors: list[StreamExtractor] | None = None,
                 remuxer: Remuxer | None = None,
                 validator: OutputValidator | None = None) -> None:
        self.settings = settings
        self.tc = toolchain or Toolchain.detect()
        self.log = log or LogBus()
        from .extract import FfmpegDvdExtractor, PassthroughExtractor
        from .dvdcell import DvdCellExtractor
        from .validate import FfprobeValidator
        # order matters: try the most-specific extractor first
        self.extractors = extractors or [
            DvdCellExtractor(self.tc, self.log),      # own engine, no libdvdnav
            FfmpegDvdExtractor(self.tc, self.log),    # fallback for images/devices
            PassthroughExtractor(self.tc, self.log),
        ]
        self.remuxer = remuxer or make_remuxer(settings.remux_backend,
                                               self.tc, self.log)
        self.validator = validator or FfprobeValidator(self.tc, self.log)
        self._cancelled: set[str] = set()

    # ---------------------------------------------------------------- #
    def request_cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled

    # ---------------------------------------------------------------- #
    def run(self, job: RipJob, *,
            progress: ProgressCallback = null_progress,
            cancelled: "CancelCheck | None" = None) -> RipResult:
        """Execute one job synchronously. Never raises for a normal,
        recoverable failure - that is reported on the job and the result.
        Programming errors (bug) still raise."""
        log = self.log.bind(f"job:{job.id}")
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or time.time()
        job.attempts += 1
        work_dir = tempfile.mkdtemp(prefix=f"opticrip-{job.id}-")

        external_cancel = cancelled or (lambda: False)

        def cancelled() -> bool:
            if external_cancel():
                return True
            return self.is_cancelled(job.id)

        def emit(update: ProgressUpdate) -> None:
            update.job_id = job.id
            progress(update)

        try:
            validate_selection(job.title, job.selection)

            # -- output path + overwrite policy ------------------------ #
            final_path = self._resolve_output_path(job)
            emit(ProgressUpdate(job_id=job.id, phase=RipPhase.ANALYSING,
                                message=f"destination: {final_path}"))
            if os.path.exists(final_path):
                if self.settings.overwrite_policy == "skip":
                    job.status = JobStatus.SKIPPED
                    log.info(f"skipped - output already exists: {final_path}")
                    return RipResult(job_id=job.id, ok=False,
                                     output_path=final_path,
                                     warnings=["destination already exists"])
                if self.settings.overwrite_policy == "ask":
                    raise E.OutputExistsError(
                        f"{final_path} already exists",
                        hint="Pick a different name or allow overwriting in "
                             "Settings.")
                if self.settings.overwrite_policy == "error":
                    raise E.OutputExistsError(
                        f"{final_path} already exists",
                        hint="The batch refuses to clobber an existing file; "
                             "pick another name.")
            requires_disc = job.drive_kind_is_raw() if hasattr(
                job, "drive_kind_is_raw") else True

            # -- disk-space check ------------------------------------- #
            from ..core.diskutil import OsDiskSpaceChecker
            checker = OsDiskSpaceChecker()
            fits, required, free = checker.check(
                os.path.dirname(final_path), job.title, job.selection)
            if not fits:
                raise E.InsufficientSpaceError(
                    f"need ~{required >> 20} MiB, only {free >> 20} MiB free",
                    hint="Free some space or pick another output directory.")

            # -- extraction ------------------------------------------- #
            emit(ProgressUpdate(job_id=job.id, phase=RipPhase.EXTRACTING,
                                fraction=0.0, message="opening disc"))
            extractor = self._pick_extractor(job)
            log.info(f"extractor: {extractor.name}")
            extracted = extractor.extract(
                job.drive_as_drive(), job.title, job.selection, work_dir,
                progress=emit, cancelled=cancelled)

            # -- remux ------------------------------------------------- #
            emit(ProgressUpdate(job_id=job.id, phase=RipPhase.REMUXING,
                                fraction=0.0))
            chapter_times = (job.selection.include_chapters
                             and job.title.chapters
                             and [c.start_seconds for c in job.title.chapters]
                             or None)
            chapter_times = normalise_chapter_times(
                chapter_times, title=job.title, log=log)
            self.remuxer.remux(extracted, final_path,
                               progress=emit, cancelled=cancelled,
                               chapter_times=chapter_times)

            # -- validation ------------------------------------------- #
            report: ValidationReport | None = None
            if self.settings.validate_after_rip:
                emit(ProgressUpdate(job_id=job.id, phase=RipPhase.VALIDATING,
                                    fraction=0.5))
                report = self.validator.validate(
                    final_path,
                    # expected = the streams the muxer actually wrote,
                    # not the raw selection - an authored-but-absent VOB
                    # subtitle track is already logged as skipped.
                    expected=(extracted.mapped_streams
                              if extracted.mapped_streams is not None
                              else extracted.streams),
                    expected_chapters=None,   # phase 5: validate chapter count
                    expected_duration=None,   # phase 5: strict duration check
                )
                if not report.ok and not self.settings.keep_failed_outputs:
                    log.warning("validation failed; removing output "
                                "(keep_failed_outputs=False)")
                    try:
                        os.unlink(final_path)
                    except OSError:
                        pass

            job.status = (JobStatus.DONE if (report is None or report.ok)
                          else JobStatus.FAILED)
            job.finished_at = time.time()
            result = RipResult(
                job_id=job.id, ok=job.status is JobStatus.DONE,
                output_path=final_path,
                duration_seconds=(report.duration_seconds
                                  if report else 0.0),
                size_bytes=(report.size_bytes if report else
                            os.path.getsize(final_path)
                            if os.path.exists(final_path) else 0),
                validation=report)
            job.result = result
            emit(ProgressUpdate(job_id=job.id, phase=RipPhase.FINISHED,
                                fraction=1.0))
            log.info(f"job finished: {job.status.label} -> {final_path}")
            return result
        except E.CancelledError:
            job.status = JobStatus.CANCELLED
            job.finished_at = time.time()
            self._cleanup(work_dir, job, log)
            log.info("job cancelled")
            return RipResult(job_id=job.id, ok=False, output_path="")
        except E.OutputExistsError:
            # "ask" means the caller must interact with the user; the engine
            # cannot answer for them, so this one propagates.
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            raise
        except E.OpticRipError as exc:
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            job.error_title = getattr(exc, "title", "Error")
            job.error = exc.message
            job.error_hint = getattr(exc, "hint", None)
            log.error(f"{exc.title}: {exc.message}")
            if getattr(exc, "detail", None):
                log.debug(f"  detail: {exc.detail}")
            self._cleanup(work_dir, job, log)
            return RipResult(job_id=job.id, ok=False, output_path="",
                             warnings=[exc.message])
        finally:
            self._cancelled.discard(job.id)
            if job.status is JobStatus.RUNNING:  # safety net
                job.status = JobStatus.FAILED
                job.finished_at = time.time()

    # ---------------------------------------------------------------- #
    def _cleanup(self, work_dir: str, job: RipJob, log) -> None:
        if self.settings.delete_temp_files and os.path.isdir(work_dir):
            try:
                shutil.rmtree(work_dir)
            except OSError as exc:
                log.warning(f"could not remove temp dir {work_dir}: {exc}")

    def _resolve_output_path(self, job: RipJob) -> str:
        """Where this job's MKV lands.

        Precedence:
          1. an explicit, absolute ``selection.output_name``;
          2. ``job.output_path`` when it is already a full file path
             (ends with ``.mkv``) - the CLI/GUI resolved the template there;
          3. the template, re-rendered against the settings directory.
        """
        if job.selection.output_name and os.path.isabs(
                job.selection.output_name):
            return job.selection.output_name
        if job.output_path.lower().endswith(".mkv"):
            directory = os.path.dirname(job.output_path) or                 self.settings.output_dir
            return os.path.join(directory, os.path.basename(job.output_path))
        return resolve_output_path(
            directory=(self.settings.output_dir),
            template=self.settings.filename_template,
            title=job.title,
            selection=job.selection,
            disc_label=job.disc_label,
        )

    def _pick_extractor(self, job: RipJob) -> StreamExtractor:
        for extractor in self.extractors:
            if extractor.can_handle(job.drive_as_drive(), job.title):
                return extractor
        raise E.ExtractionError(
            f"no extractor can handle {job.drive_device} / "
            f"{job.title.display_name}",
            hint="This source type may not be supported yet.")
