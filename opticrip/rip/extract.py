"""Stream extraction backends.

Two implementations share one goal: hand the remuxer a *local, complete*
mpeg stream that contains exactly the selected title's data:

``FfmpegDvdExtractor``
    Drives ffmpeg's ``dvdvideo`` demuxer over the disc source. libdvdnav
    resolves titles/cells/angles for us; we just demux + copy. This is the
    correct default path for DVDs.

``PassthroughExtractor``
    For an already-demuxable source (raw image / folder recognised by
    ffmpeg, e.g. a VOB or a .m2ts clip) - no disc navigation needed.

Neither extractor ever re-encodes: `-c copy` is used end-to-end.
Decryption (CSS/AACS/BD+) is *not* implemented here by design; see
docs/PROTECTED_DISCS.md. If the source looks protected, extraction fails
with a specific, honest error.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import subprocess
from pathlib import Path

from ..core.errors import (
    CancelledError,
    DiscReadError,
    EncryptedDiscError,
    ExtractionError,
    ToolMissingError,
    UnsupportedMediaError,
)
from ..core.interfaces import (
    CancelCheck,
    ExtractionResult,
    ProgressCallback,
    StreamExtractor,
    never_cancelled,
    null_progress,
)
from ..core.logging_bus import LogBus
from ..core.models import (
    Drive,
    DriveKind,
    MediaType,
    StreamInfo,
    StreamKind,
    Title,
    TitleSelection,
    selection_to_streams,
)
from .tools import Toolchain

CHUNK_SECONDS = 30   # progress granularity
#: ffmpeg reports this when a disc has decryption middleware present but
#: wrong/absent keys - the honest message distinguishes it from "no support".
PROTECTION_MARKERS = (
    "css",
    "aacs",
    "bd+",
    "discencrypted",
    "cannot decrypt",
    "no key",
    "decrypt",
)


def _err_text(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip()


def classify_ffmpeg_failure(stderr: str) -> Exception:
    """Turn raw ffmpeg stderr into a specific, honest exception.

    We never claim "encryption" when the data says otherwise, and we never
    hide behind a generic message when we could tell the user *what* broke.
    """
    lowered = (stderr or "").lower()
    if any(marker in lowered for marker in PROTECTION_MARKERS):
        return EncryptedDiscError(
            "the disc appears to be copy-protected and cannot be read",
            hint="OpticRip does not circumvent copy protection. See docs/PROTECTED_DISCS.md.",
            detail=stderr[-2000:])
    if "not permit" in lowered or "denied" in lowered:
        return DiscReadError(
            "the drive refused the read",
            hint="Check drive permissions (cdrom group) or that the disc is clean.",
            detail=stderr[-2000:])
    if "no medium" in lowered or "no disc" in lowered:
        return DiscReadError("no disc in the drive",
                             hint="Insert a disc and try again.")
    return ExtractionError(
        "extraction failed",
        detail=stderr[-2000:])


class FfmpegDvdExtractor(StreamExtractor):
    """DVD title extraction via ffmpeg's ``dvdvideo`` demuxer.

    Output: a temp MPEG-PS file holding this title's elementary streams in
    their original, untranscoded form, ready for the remuxer's `-c copy`.
    """

    name = "ffmpeg-dvdvideo"

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    # ---------------------------------------------------------------- #
    def can_handle(self, drive: Drive, title: Title) -> bool:
        if not self.tc.dvd_navigation():
            return False
        return drive.media_type in (MediaType.DVD_VIDEO, MediaType.UNKNOWN,
                                    MediaType.DATA) and \
            title.source_ref.get("kind") in ("dvd-pgc", "dvd", "")

    # ---------------------------------------------------------------- #
    def extract(self, drive: Drive, title: Title, selection: TitleSelection,
                work_dir: str, *, progress: ProgressCallback = null_progress,
                cancelled: CancelCheck = never_cancelled) -> ExtractionResult:
        ffmpeg = self.tc.require("ffmpeg")

        os.makedirs(work_dir, exist_ok=True)
        temp_path = os.path.join(work_dir, f"extract-{title.id}.ps")
        log = self._log.bind(f"extract:{title.id}")

        title_arg = str(title.source_ref.get("title_number") or title.index)
        args = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-f", "dvdvideo",
            "-title", title_arg,
            "-progress", "pipe:1", "-nostats",
            "-i", drive.path,
            "-map", "0", "-c", "copy",
            "-f", "mpeg", temp_path,
        ]
        log.info(f"running ffmpeg dvdvideo title={title_arg}")

        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
        except FileNotFoundError as exc:
            raise ToolMissingError("ffmpeg disappeared between check and run",
                                   detail=str(exc)) from exc

        # `-progress pipe:1` gives us light-weight line updates like
        # "out_time_ms=1234567"; parse and re-emit as ProgressUpdate.
        try:
            for line in proc.stdout or []:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    pts = line.split("=", 1)[1]
                    try:
                        seconds = float(pts) / 1_000_000
                    except ValueError:
                        seconds = 0.0
                    fraction = None
                    if title.duration_seconds > 0:
                        fraction = min(1.0, seconds / title.duration_seconds)
                    progress(_update(title, seconds, fraction))
                if cancelled():
                    log.warning("cancelled by user")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise CancelledError("extraction cancelled")
            proc.wait(timeout=60)
        finally:
            if proc.stdout:
                proc.stdout.close()
            stderr_text = ""
            if proc.stderr:
                stderr_text = proc.stderr.read().strip()
                proc.stderr.close()

        if proc.returncode != 0:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise classify_ffmpeg_failure(stderr_text)

        size = os.path.getsize(temp_path)
        log.info(f"extracted {size} bytes of mpeg-ps to {temp_path}")
        return ExtractionResult(
            paths=[temp_path],
            streams=selection_to_streams(title, selection),
            cleanup=True,
            protocol="file",
            size_bytes=size,
            duration_hint=title.duration_seconds,
        )


class PassthroughExtractor(StreamExtractor):
    """For sources ffmpeg can already demux directly (VOB files, .iso, etc.)."""

    name = "passthrough"

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    def can_handle(self, drive: Drive, title: Title) -> bool:
        # Accepts anything that is not a nav-required DVD title.
        return title.source_ref.get("kind") not in ("dvd-pgc",)

    def extract(self, drive: Drive, title: Title, selection: TitleSelection,
                work_dir: str, *, progress: ProgressCallback = null_progress,
                cancelled: CancelCheck = never_cancelled) -> ExtractionResult:
        # No intermediate file is needed - the remuxer can open the source
        # directly; we just report it so the remuxer knows what it's getting.
        return ExtractionResult(
            paths=[drive.path],
            streams=selection_to_streams(title, selection),
            cleanup=False,
            protocol="file",
            duration_hint=title.duration_seconds,
        )


def _update(title: Title, seconds: float, fraction: float | None):
    from ..core.models import ProgressUpdate, RipPhase
    return ProgressUpdate(
        job_id="", phase=RipPhase.EXTRACTING,
        fraction=fraction,
        message=f"{seconds:,.0f}s of {title.display_name}",
        processed_bytes=None, total_bytes=title.size_bytes)
