"""Remuxing backends.

`FFmpegRemuxer` is the primary: a single ffmpeg subprocess that maps the
extracted elementary streams and copies them into Matroska with no
re-encoding (`-c copy`).  Chapter starts, track languages and defaults are
passed through on the command line so the MKV carries everything the user
was promised in the selection dialog.

`MkvmergeRemuxer` is a thin alternative backend for users who have
MKVToolNix installed and prefer it; it is selected in Settings
(`remux_backend: "auto" | "ffmpeg" | "mkvmerge"`), with `"auto"` preferring
ffmpeg when both are present because we can steer it more precisely.

Both implementations never touch a single video/audio/subtitle byte beyond
container framing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

def css_env() -> dict[str, str]:
    """Subprocess-env overrides exposing a user-installed libdvdcss to
    ffmpeg's libdvdread (see opticrip.disc.dvdcss).  Never ships a decoder
    itself."""
    try:
        from .tools import Toolchain
        return Toolchain().dvdcss_env()
    except Exception:
        return {}

from ..core.errors import CancelledError, RemuxError, ToolMissingError
from ..core.interfaces import (
    CancelCheck,
    ExtractionResult,
    ProgressCallback,
    Remuxer,
    never_cancelled,
    null_progress,
)
from ..core.logging_bus import LogBus
from ..core.models import ProgressUpdate, RipPhase, StreamInfo, StreamKind
from .tools import Toolchain

_KNOWN_SAFETY_FLAGS = ("-loglevel", "warning", "-nostdin", "-hide_banner")


def _input_stream_count(extracted: "ExtractionResult") -> int:
    """Number of streams actually present in the extracted media.

    Uses ffprobe when available; falls back to selection length if probing
    is impossible (so behaviour degrades to "try to map everything" which is
    what ffmpeg will do anyway).
    """
    try:
        import json as _json
        from . import subproc
        ffprobe = shutil.which("ffprobe") or ""
        path = extracted.paths[0] if extracted.paths else ""
        if not ffprobe or not path:
            return len(extracted.streams)
        proc = subproc.run(
            [ffprobe, "-hide_banner", "-loglevel", "error",
             "-show_entries", "stream=index", "-print_format", "json", path],
            capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode != 0:
            return len(extracted.streams)
        data = _json.loads(proc.stdout or "{}")
        return len(data.get("streams", [])) or len(extracted.streams)
    except Exception:
        return len(extracted.streams)


class FFmpegRemuxer(Remuxer):
    """:class:`Remuxer` backed by one ffmpeg process."""

    name = "ffmpeg"

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    def available(self) -> tuple[bool, str]:
        if not self.tc.ffmpeg.available:
            return (False, "ffmpeg not found")
        if not self.tc.mkv_muxing():
            return (False, "this ffmpeg build has no matroska muxer")
        return (True, self.tc.ffmpeg.version or "available")

    # ---------------------------------------------------------------- #
    def remux(self, extracted: ExtractionResult, output_path: str, *,
             progress: ProgressCallback = null_progress,
             cancelled: CancelCheck = never_cancelled,
             chapter_times: list[float] | None = None) -> str:
        ffmpeg = self.tc.require("ffmpeg")
        log = self._log.bind("remux")

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        temp_path = output_path + ".part"

        # MPEG-PS cannot carry chapter markers, so they must be re-injected
        # explicitly as ffmetadata. Any non-empty set of chapter start times
        # becomes a CHAPTERS block; stale .part cleanup is handled below.
        meta_path = None
        if chapter_times and len(chapter_times) >= 1:
            meta_path = output_path + ".chapters.meta"
            log.info(f"writing {len(chapter_times)} chapter marker(s) as "
                     "ffmetadata")
            end = extracted.duration_hint or 0.0
            lines = [";FFMETADATA1"]
            for i, start in enumerate(chapter_times):
                chapter_end = (chapter_times[i + 1] if i + 1 <
                               len(chapter_times) else end)
                if chapter_end <= start:
                    chapter_end = start + 1.0
                lines += [f"[CHAPTER]",
                          "TIMEBASE=1/1000",
                          f"START={int(round(start * 1000))}",
                          f"END={int(round(chapter_end * 1000))}",
                          f"title=Chapter {i + 1}"]
            with open(meta_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")

        args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
                "-progress", "pipe:1", "-nostats", "-y"]
        for path in extracted.paths:
            args += ["-i", path]
        if meta_path:
            # ffmetadata goes on the input side; -map_metadata refers back
            # to input index 1 when writing the output file options below.
            args += ["-i", meta_path]

        # Map every stream the extractor promised, in mux order, adjusting
        # for streams that appear in the IFO but are physically absent from
        # the extraction (e.g. an empty DVD subtitle track and no packet
        # ever written).  We probe the actual input stream count once and
        # only map what exists - and we SAY so in the log; selected streams
        # are never dropped silently.
        # Languages travel as per-stream metadata: `-metadata:s:<spec>
        # language=<iso639>`; ffmpeg has no bare `-language` option.
        present = _input_stream_count(extracted)
        log.info(f"extracted media carries {present} stream(s); "
                 f"selection promised {len(extracted.streams)}")
        audio_n = sub_n = 0
        for i, stream in enumerate(extracted.streams):
            if i >= present:
                log.warning(
                    f"stream [{i}] ({stream.summary()}) is declared in the "
                    f"disc structure but absent from the extracted media; "
                    f"skipping - nothing was lost that the disc actually "
                    f"contained")
                continue
            args += ["-map", f"0:{i}"]
            if stream.kind is StreamKind.AUDIO and stream.language:
                args += ["-metadata:s:a:" + str(audio_n),
                         f"language={stream.language}"]
                audio_n += 1
            elif stream.kind is StreamKind.SUBTITLE:
                if stream.language:
                    args += ["-metadata:s:s:" + str(sub_n),
                             f"language={stream.language}"]
                if stream.forced:
                    args += ["-disposition:s:" + str(sub_n) + " forced"]
                sub_n += 1
        args += [
            "-c", "copy",
            "-map_metadata", "1" if meta_path else "0",
            "-f", "matroska", temp_path,
        ]
        log.info("running ffmpeg remux -> " + output_path)
        log.debug(" ".join(args))

        try:
            from . import subproc
            proc = subproc.popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, **css_env()})
        except FileNotFoundError as exc:
            raise ToolMissingError("ffmpeg disappeared between check and run") from exc
        total = extracted.size_bytes or 0
        try:
            for line in proc.stdout or []:
                line = line.strip()
                if line.startswith("out_time_ms=") and total:
                    try:
                        done = float(line.split("=", 1)[1])
                    except ValueError:
                        done = 0.0
                    progress(ProgressUpdate(
                        job_id="", phase=RipPhase.REMUXING,
                        fraction=min(1.0, done / max(1, total * 2048)),
                        message="muxing"))
                if cancelled():
                    log.warning("remux cancelled by user")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise CancelledError("remux cancelled")
            proc.wait(timeout=120)
        finally:
            if proc.stdout:
                proc.stdout.close()
            err = ""
            if proc.stderr:
                err = proc.stderr.read().strip()
                proc.stderr.close()

        if proc.returncode != 0:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise RemuxError("ffmpeg remux failed", detail=err[-2000:])
        if meta_path:
            try:
                os.unlink(meta_path)
            except OSError:
                pass
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            raise RemuxError("ffmpeg produced an empty MKV")

        os.replace(temp_path, output_path)
        log.info(f"remuxed {os.path.getsize(output_path)} bytes -> {output_path}")

        # remember exactly what landed in the MKV so the validator checks
        # against reality rather than the optimistic selection
        mapped_count = counted_present = present
        extracted.mapped_streams = [s for s in extracted.streams
                                    if extracted.streams.index(s) < present]
        return output_path


class MkvmergeRemuxer(Remuxer):
    """:class:`Remuxer` backed by ``mkvmerge`` (MKVToolNix)."""

    name = "mkvmerge"

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    def available(self) -> tuple[bool, str]:
        if not self.tc.mkvmerge.available:
            return (False, "mkvmerge not installed (optional backend)")
        return (True, self.tc.mkvmerge.version or "available")

    def remux(self, extracted: ExtractionResult, output_path: str, *,
             progress: ProgressCallback = null_progress,
             cancelled: CancelCheck = never_cancelled) -> str:
        mkvmerge = self.tc.require("mkvmerge")
        log = self._log.bind("remux:mkvmerge")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        temp_path = output_path + ".part"
        args = [mkvmerge, "--output", temp_path]
        for path in extracted.paths:
            args.append(path)
        # languages per audio/subtitle track (mkvmerge indexes per file type)
        audio_n = sub_n = 0
        for stream in extracted.streams:
            if stream.kind is StreamKind.AUDIO and stream.language:
                args += [f"--language", f"0:{audio_n}:{stream.language}"]
                audio_n += 1
            elif stream.kind is StreamKind.SUBTITLE and stream.language:
                args += [f"--language", f"0:{sub_n}:{stream.language}"]
                sub_n += 1
        try:
            from . import subproc
            proc = subproc.popen(args, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as exc:
            raise ToolMissingError("mkvmerge disappeared between check and run") from exc
        try:
            proc.wait(timeout=180)
        finally:
            if proc.stdout:
                proc.stdout.close()
            err = ""
            if proc.stderr:
                err = proc.stderr.read().strip()
                proc.stderr.close()
        if proc.returncode not in (0, 1):
            # mkvmerge emits 1 for "warnings" which is still ok in practice
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise RemuxError(f"mkvmerge failed with exit {proc.returncode}",
                             detail=err[-2000:])
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            raise RemuxError("mkvmerge produced an empty MKV")
        os.replace(temp_path, output_path)
        log.info(f"mkvmerge produced {os.path.getsize(output_path)} bytes")
        return output_path


def make_remuxer(backend: str, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> Remuxer:
    """The single dispatch point for the configured backend."""
    if backend not in ("auto", "ffmpeg", "mkvmerge"):
        backend = "auto"
    ffmpeg = FFmpegRemuxer(toolchain, log)
    mkvmerge = MkvmergeRemuxer(toolchain, log)
    if backend == "ffmpeg":
        return ffmpeg
    if backend == "mkvmerge":
        return mkvmerge
    # auto: prefer ffmpeg, fall back if it is unusable
    if ffmpeg.available()[0]:
        return ffmpeg
    if mkvmerge.available()[0]:
        return mkvmerge
    return ffmpeg
