"""Blu-ray playlist extractor (Phase 8).

Reads a playlist's M2TS clips straight from the STREAM folder and cuts the
IN/OUT span of each PlayItem with ffmpeg ``-ss``/``-t`` **stream copy** —
original M2TS packets in, original packets out, no transcoding.

DRM note: this path only touches *unencrypted* Blu-ray structures. If the
clip''s packets read as scrambled, ffmpeg''s own error surfaces through
:class:`classify_ffmpeg_failure` as :class:`EncryptedDiscError` - honest,
and no circumvention is attempted (see docs/PROTECTED_DISCS.md).
"""
from __future__ import annotations

import os
import subprocess

from ..core.errors import (
    CancelledError, DiscReadError, EncryptedDiscError, ExtractionError,
)
from ..core.interfaces import (
    CancelCheck, ExtractionResult, ProgressCallback, StreamExtractor,
    never_cancelled, null_progress,
)
from ..core.logging_bus import LogBus
from ..core.models import (
    Drive, MediaType, ProgressUpdate, RipPhase, StreamInfo, Title,
    TitleSelection,
)
from ..disc.bd import BdPlayItem
from .extract import StreamExtractor, Toolchain, classify_ffmpeg_failure


class BdPlaylistExtractor(StreamExtractor):
    """Extract one Blu-ray playlist (title) into a temp M2TS file."""

    name = "bd-playlist"

    CANCEL_CHUNK_SECONDS = 5.0

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    # ---------------------------------------------------------------- #
    def can_handle(self, drive: Drive, title: Title) -> bool:
        return (drive.media_type in (MediaType.BLURAY,
                                     MediaType.UHD_BLURAY,
                                     MediaType.UNKNOWN) and
                title.source_ref.get("kind") == "bd-playlist")

    # ---------------------------------------------------------------- #
    @staticmethod
    def _clip_paths(drive_path: str, clip_id: str) -> str:
        """First existing path for a clip's M2TS under the source root."""
        cands = [
            os.path.join(drive_path, "BDMV", "STREAM", f"{clip_id}.m2ts"),
            os.path.join(drive_path, "STREAM", f"{clip_id}.m2ts"),
            os.path.join(drive_path, f"{clip_id}.m2ts"),
        ]
        for c in cands:
            if os.path.isfile(c):
                return c
        return cands[0]

    @staticmethod
    def _playlist_path(drive_path: str, name: str) -> str:
        return os.path.join(drive_path, "BDMV", "PLAYLIST", name)

    # ---------------------------------------------------------------- #
    def extract(self, drive: Drive, title: Title, selection: TitleSelection,
                work_dir: str, *, progress: ProgressCallback = null_progress,
                cancelled: CancelCheck = never_cancelled) -> ExtractionResult:
        ffmpeg = self.tc.require("ffmpeg")
        os.makedirs(work_dir, exist_ok=True)
        log = self._log.bind(f"extract:{title.id}")

        playlist_name = title.source_ref.get("playlist")
        if not playlist_name:
            raise ExtractionError(
                f"title {title.id} carries no playlist reference")
        playlist_path = self._playlist_path(drive.path, playlist_name)

        from ..disc.bd import parse_mpls, ticks_to_seconds
        m = parse_mpls(open(playlist_path, "rb").read(),
                       name=playlist_name)
        if not m.play_items:
            raise ExtractionError(f"playlist {playlist_name} has no "
                                  "play items")

        os.makedirs(work_dir, exist_ok=True)
        temp_path = os.path.join(work_dir, f"extract-{title.id}.m2ts")

        ordered = [pi for pi in m.play_items]     # play order
        total = sum(pi.duration_seconds for pi in ordered)
        done = 0.0
        for chunk_index, pi in enumerate(ordered):
            if cancelled():
                raise CancelledError("cancelled by user")
            clip_file = self._clip_paths(drive.path, pi.clip_id)
            if not os.path.isfile(clip_file):
                raise DiscReadError(
                    f"clip {pi.clip_id}.m2ts is missing from STREAM")
            args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel",
                    "error",
                    "-ss", f"{ticks_to_seconds(pi.in_time_ticks):.3f}",
                    "-t", f"{pi.duration_seconds + 0.5:.3f}",
                    "-i", clip_file,
                    "-map", "0", "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    "-muxrate", "24000000",
                    "-f", "mpegts",
                    temp_path]   # output file goes to ffmpeg, not stdout
            res = subprocess.run(args, stderr=subprocess.PIPE)
            if res.returncode != 0:
                stderr = res.stderr.decode("utf-8", "replace")
                raise classify_ffmpeg_failure(stderr)
            log.info(f"play item {pi.clip_id} done "
                     f"({done:.1f}/{total:.1f}s)")
        done = total

        progress(ProgressUpdate(
            job_id="",                                   # engine stamps it
            phase=RipPhase.EXTRACTING,
            fraction=min(0.99, done / max(total, 0.1)),
            message=f"{len(ordered)} clip(s)"))
        from ..core.models import format_bytes
        log.info(f"extracted {format_bytes(os.path.getsize(temp_path))}")
        return ExtractionResult(
            paths=[temp_path],
            streams=title.streams,
            chapter_times=([c.start_seconds for c in title.chapters]
                           if selection.include_chapters else None),
            cleanup=True,
            size_bytes=os.path.getsize(temp_path),
            duration_hint=total)
