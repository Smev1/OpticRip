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
from dataclasses import dataclass, field

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


#: ffprobe ``codec_type`` -> our :class:`StreamKind`.  Anything else (notably
#: ``data``) cannot be written to Matroska and must never be mapped.
_KIND_BY_TYPE = {
    "video": StreamKind.VIDEO,
    "audio": StreamKind.AUDIO,
    "subtitle": StreamKind.SUBTITLE,
}

#: the only stream kinds a Matroska file can hold
MUXABLE_KINDS = (StreamKind.VIDEO, StreamKind.AUDIO, StreamKind.SUBTITLE)


@dataclass(frozen=True)
class ProbedStream:
    """One stream ffprobe actually found in the extracted media."""

    index: int
    kind: StreamKind
    codec: str = ""


def probe_input_streams(toolchain: Toolchain,
                        path: str) -> list[ProbedStream] | None:
    """What the extracted file *really* contains, in ffmpeg stream order.

    A spec-conformant DVD VOB starts every VOBU with a navigation pack
    (``private_stream_2``), which ffmpeg exposes as a *data* stream - so a
    real disc's extracted cells carry one more stream than the IFO selection,
    and it sits at index 0.  Assuming "promised stream i is input i" therefore
    hands the navigation stream to the Matroska muxer, which refuses anything
    that is not audio/video/subtitles.  Probing first is the only way to know
    which index is which.

    Returns ``None`` when probing is impossible (no ffprobe, or it failed) so
    the caller can degrade to positional mapping instead of refusing to work.
    """
    ffprobe = toolchain.ffprobe.path if toolchain.ffprobe.available else ""
    ffprobe = ffprobe or shutil.which("ffprobe") or ""
    if not ffprobe or not path:
        return None
    try:
        import json as _json
        from . import subproc
        proc = subproc.run(
            [ffprobe, "-hide_banner", "-loglevel", "error",
             "-show_entries", "stream=index,codec_type,codec_name",
             "-print_format", "json", path],
            capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode != 0:
            return None
        raw_streams = _json.loads(proc.stdout or "{}").get("streams", [])
    except Exception:
        return None
    probed: list[ProbedStream] = []
    for position, raw in enumerate(raw_streams):
        kind = _KIND_BY_TYPE.get(raw.get("codec_type") or "",
                                 StreamKind.OTHER)
        try:
            index = int(raw.get("index", position))
        except (TypeError, ValueError):
            index = position
        probed.append(ProbedStream(index, kind, raw.get("codec_name") or ""))
    return probed


@dataclass
class StreamPlan:
    """How the promised selection lines up with the media that exists."""

    #: (promised stream, actual input index for ``-map 0:<index>``), in order
    mapped: list[tuple[StreamInfo, int]] = field(default_factory=list)
    #: promised streams whose kind is nowhere in the extracted media
    absent: list[StreamInfo] = field(default_factory=list)
    #: navigation/data/attachment streams that cannot go into an MKV
    ignored: list[ProbedStream] = field(default_factory=list)
    #: muxable streams present but not asked for by the selection
    unpromised: list[ProbedStream] = field(default_factory=list)
    #: False when ffprobe was unusable and mapping fell back to positions
    probed: bool = True


def plan_stream_maps(promised: list[StreamInfo],
                     probed: list[ProbedStream] | None) -> StreamPlan:
    """Match each promised stream to a real stream *of the same kind*.

    Streams are matched per kind in file order, which preserves the selection's
    meaning (first chosen audio -> first audio in the media) while dropping
    navigation/data streams entirely.  When ``probed`` is ``None`` the
    historical positional mapping is kept, flagged via ``probed=False``, so the
    caller can warn.
    """
    if probed is None:
        return StreamPlan(
            mapped=[(stream, index) for index, stream in enumerate(promised)],
            probed=False)

    by_kind: dict[StreamKind, list[ProbedStream]] = {}
    for stream in probed:
        by_kind.setdefault(stream.kind, []).append(stream)

    plan = StreamPlan(probed=True)
    for stream in promised:
        bucket = by_kind.get(stream.kind) or []
        if not bucket:
            plan.absent.append(stream)
            continue
        plan.mapped.append((stream, bucket.pop(0).index))
    for stream in probed:
        if stream.kind not in MUXABLE_KINDS:
            plan.ignored.append(stream)
    for kind, leftover in by_kind.items():
        if kind in MUXABLE_KINDS:      # non-muxable streams are `ignored`
            plan.unpromised.extend(leftover)
    plan.unpromised.sort(key=lambda s: s.index)
    return plan


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

        # Map every stream the extractor promised onto the stream that is
        # actually there, *by kind* rather than by position.  A real VOB
        # begins each VOBU with a navigation pack, which ffmpeg presents as a
        # data stream (usually at index 0); mapping positionally used to feed
        # it to the Matroska muxer, which rejects anything that is not
        # audio/video/subtitles and failed the whole job instantly.  Streams
        # promised by the disc structure but physically absent (e.g. an empty
        # subtitle track) are still skipped, loudly.
        # Languages travel as per-stream metadata: `-metadata:s:<spec>
        # language=<iso639>`; ffmpeg has no bare `-language` option.
        probe_path = extracted.paths[0] if extracted.paths else ""
        probed = (probe_input_streams(self.tc, probe_path)
                  if probe_path else None)
        plan = plan_stream_maps(extracted.streams, probed)

        if probed is not None:
            log.info(
                f"extracted media carries {len(probed)} stream(s): "
                f"{len(plan.mapped)} selected, {len(plan.ignored)} "
                f"navigation/data ignored; selection promised "
                f"{len(extracted.streams)}")
            log.debug("probed streams: " + ", ".join(
                f"[{s.index}] {s.kind.value}/{s.codec or 'unknown'}"
                for s in probed))
        else:
            log.warning(
                "ffprobe could not inspect the extraction; mapping streams "
                "positionally, which fails on media that carries navigation "
                "streams")
        for stream in plan.ignored:
            log.info(
                f"ignoring navigation/data stream [{stream.index}] "
                f"({stream.codec or 'unknown'}) - not audio, video or "
                "subtitles, so Matroska cannot hold it")
        for stream in plan.unpromised:
            log.info(f"extraction also carries [{stream.index}] "
                     f"({stream.codec or stream.kind.label}) which the "
                     "selection did not ask for; leaving it out")

        audio_n = sub_n = 0
        for stream, input_index in plan.mapped:
            args += ["-map", f"0:{input_index}"]
            if stream.kind is StreamKind.AUDIO:
                # the ordinal counts *mapped* audio tracks, so it stays in
                # step with ffmpeg's output numbering even when an earlier
                # track has no language tag
                if stream.language:
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
        for stream in plan.absent:
            log.warning(
                f"stream [{stream.index}] ({stream.summary()}) is declared "
                "in the disc structure but absent from the extracted media; "
                "skipping - nothing was lost that the disc actually contained")

        if not plan.mapped:
            if meta_path:
                try:
                    os.unlink(meta_path)
                except OSError:
                    pass
            raise RemuxError(
                "the extraction holds no audio, video or subtitle streams "
                "to mux",
                hint=("The selected title produced no muxable media - try "
                      "another title, or report the disc: "
                      "docs/TROUBLESHOOTING.md."),
                detail="probed streams: " + ", ".join(
                    f"[{s.index}] {s.kind.value}/{s.codec or 'unknown'}"
                    for s in (probed or [])))

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

        # remember exactly what landed in the MKV (matched promised streams
        # only) so the validator checks against reality, not the selection
        extracted.mapped_streams = [stream for stream, _index in plan.mapped]
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
