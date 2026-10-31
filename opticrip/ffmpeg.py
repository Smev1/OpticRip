"""ffmpeg integration: probing, remuxing, progress and cancellation.

The reference uses its own FFmpeg-derived muxer.  A clean-room clone delegates
to a normal ``ffmpeg`` binary; everything else (selection, naming, chapters)
stays in this package.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .model import Stream, Title

_CACHE: dict[str, str] = {}
_CODEC_LABELS = {
    "h264": "H.264", "hevc": "H.265", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1",
    "vc1": "VC-1", "av1": "AV1", "vp9": "VP9", "mjpeg": "MJPEG",
    "ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS", "truehd": "TrueHD",
    "flac": "FLAC", "aac": "AAC", "mp2": "MP2", "mp3": "MP3", "opus": "Opus",
    "pcm_s16le": "PCM", "pcm_s24le": "PCM", "pcm_bluray": "PCM", "lpcm": "LPCM",
    "subrip": "SubRip", "ass": "ASS", "ssa": "SSA", "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "DVD subtitles", "dvb_subtitle": "DVB subtitles",
    "mov_text": "Text",
}


class FFmpegError(Exception):
    pass


def tool(name: str) -> str:
    """Resolve ``ffmpeg`` or ``ffprobe``.

    ``OPTICRIP_FFMPEG`` (binary path or folder) wins over ``PATH``, which
    wins over the bundled ``tools/`` folder.  ``OPTICRIP_FFMPEG_DIR`` is kept
    as a folder-only alias for existing installations.
    """
    if name in _CACHE:
        return _CACHE[name]
    candidates: list[str] = []
    env_dir = os.environ.get("OPTICRIP_FFMPEG_DIR", "").strip()
    if env_dir:
        candidates.append(str(Path(env_dir) / (name + ".exe")))
        candidates.append(str(Path(env_dir) / name))
    override = os.environ.get("OPTICRIP_FFMPEG", "").strip()
    if override:
        path = Path(override)
        if path.is_dir():
            candidates.append(str(path / (name + ".exe")))
            candidates.append(str(path / name))
        elif path.is_file():
            if path.stem.lower() == name.lower():
                candidates.append(str(path))
            else:
                # The override names the other tool; look beside it.
                candidates.append(str(path.with_name(name + path.suffix)))
    found = shutil.which(name)
    if found:
        candidates.append(found)
    candidates.append(
        str(Path(__file__).resolve().parent.parent / "tools" / (name + ".exe"))
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            _CACHE[name] = candidate
            return candidate
    raise FFmpegError(
        f"{name} was not found. Install FFmpeg or set OPTICRIP_FFMPEG to an "
        "ffmpeg binary or a folder containing ffmpeg and ffprobe."
    )


def available() -> bool:
    try:
        tool("ffmpeg")
        tool("ffprobe")
        return True
    except FFmpegError:
        return False


def _run(cmd: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=_no_window(),
    )


def _no_window() -> int:
    if os.name == "nt":
        return 0x08000000
    return 0


def ffprobe_json(path: str) -> dict:
    cmd = [
        tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters", path,
    ]
    result = _run(cmd)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise FFmpegError(message[-1] if message else f"ffprobe exited {result.returncode}")
    try:
        return json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise FFmpegError(f"invalid ffprobe output: {exc}") from exc


def _fps(text: str) -> str:
    try:
        numerator, _, denominator = text.partition("/")
        value = float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        return text
    if abs(value - 29.97) < 0.02:
        return "29.97 (30000/1001)"
    if abs(value - 23.976) < 0.02:
        return "23.976 (24000/1001)"
    if abs(value - 59.94) < 0.02:
        return "59.94 (60000/1001)"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _language(stream: dict) -> str:
    tags = stream.get("tags") or {}
    return (tags.get("language") or "und").lower()


def streams_from_probe(info: dict) -> list[Stream]:
    streams: list[Stream] = []
    for entry in info.get("streams", []):
        kind = entry.get("codec_type")
        if kind not in ("video", "audio", "subtitle"):
            continue
        codec_name = entry.get("codec_name") or ""
        codec = _CODEC_LABELS.get(codec_name, codec_name.upper())
        disposition = entry.get("disposition") or {}
        stream = Stream(
            kind=kind,
            index=len(streams),
            codec_id=codec_name,
            codec=codec,
            codec_long=codec,
            language=_language(entry),
            channels=int(entry.get("channels") or 0),
            channel_layout=entry.get("channel_layout") or "",
            width=int(entry.get("width") or 0),
            height=int(entry.get("height") or 0),
            fps=_fps(entry.get("avg_frame_rate") or entry.get("r_frame_rate") or ""),
            bitrate=int(entry.get("bit_rate") or 0),
            sample_rate=int(entry.get("sample_rate") or 0),
            forced=bool(disposition.get("forced")),
            source_index=int(entry.get("index", len(streams))),
            stream_id=str(entry.get("id") or ""),
            output_codec=codec,
        )
        pid = entry.get("id")
        if isinstance(pid, str) and pid.startswith("0x"):
            try:
                stream.pid = int(pid, 16)
            except ValueError:
                stream.pid = 0
        streams.append(stream)
    return streams


def probe_title(path: str, index: int = 0) -> Title:
    info = ffprobe_json(path)
    fmt = info.get("format") or {}
    duration = float(fmt.get("duration") or 0.0)
    chapters = [
        float(ch.get("start_time") or 0.0) for ch in info.get("chapters", [])
    ]
    title = Title(
        index=index,
        source="mkv",
        duration=duration,
        size_bytes=int(fmt.get("size") or 0),
        chapter_times=chapters,
        streams=streams_from_probe(info),
        name=Path(path).stem,
    )
    return title


def concat_list(paths: Iterable[str], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for path in paths:
            escaped = path.replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    return destination


def chapters_file(times: Sequence[float], duration: float, destination: Path,
                  insert_zero: bool = True) -> Path:
    points = sorted({round(max(0.0, t), 3) for t in times})
    if insert_zero and (not points or points[0] > 0.5):
        points.insert(0, 0.0)
    lines = [";FFMETADATA1"]
    for i, start in enumerate(points):
        end = points[i + 1] if i + 1 < len(points) else max(duration, start + 0.001)
        lines.extend([
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(start * 1000))}",
            f"END={int(round(end * 1000))}",
            f"title=Chapter {i + 1:02d}",
        ])
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def _progress_seconds(line: str) -> float | None:
    """Decode ffmpeg's ``out_time_*`` progress records.

    ffmpeg prints both ``out_time_us`` and ``out_time_ms`` with *microsecond*
    values; ``out_time_ms`` is a historical misnomer.  Treating it as
    milliseconds made every progress update a thousand times too large and
    pinned the GUI bar at 100%.
    """
    key, sep, raw = line.partition("=")
    if not sep or key not in ("out_time_us", "out_time_ms"):
        return None
    try:
        return int(raw) / 1_000_000
    except ValueError:
        return None


def run_ffmpeg(args: Sequence[str], duration: float = 0.0,
               on_progress: Callable[[float], None] | None = None,
               cancelled: Callable[[], bool] | None = None,
               cwd: str | None = None) -> int:
    cmd = [
        tool("ffmpeg"), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats", *args,
    ]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, creationflags=_no_window(),
    )
    assert process.stdout is not None
    try:
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if cancelled is not None and cancelled():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise KeyboardInterrupt
            if on_progress is None:
                continue
            seconds = _progress_seconds(line)
            if seconds is not None and duration > 0:
                on_progress(max(0.0, min(1.0, seconds / duration)))
            elif line == "progress=end" and duration > 0:
                on_progress(1.0)
        code = process.wait()
        detail = ""
        if process.stderr is not None:
            raw = process.stderr.read()
            text = raw.decode("utf-8", "replace").strip() if raw else ""
            if text:
                detail = text.splitlines()[-1]
        if code != 0:
            raise FFmpegError(f"ffmpeg exited {code}" + (f": {detail}" if detail else ""))
        return code
    finally:
        if process.poll() is None:
            process.kill()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _dvd_rate(title: Title) -> str:
    """The display rate the IFO declares, used as container metadata."""
    videos = title.video_streams()
    if not videos:
        return ""
    text = videos[0].fps.lower()
    if "30000/1001" in text or "29.97" in text:
        return "30000/1001"
    if "24000/1001" in text or "23.976" in text:
        return "24000/1001"
    if text.startswith("25"):
        return "25"
    return ""


def _mux_args(
    source_args: Sequence[str],
    output: Path,
    streams: Sequence[Stream],
    title: Title,
    chapters: bool,
    compression: str | None,
    temporary: Path,
    map_all: bool,
    container: str,
    copy_all: bool = False,
    rate: str | None = None,
    caption_srt: Path | None = None,
) -> list[str]:
    args = list(source_args)
    # A closed-caption track is not part of the video container, so it is an
    # extra input generated from the MPEG-2 user data before muxing.
    caption_index = None
    if caption_srt is not None:
        args.extend(["-i", str(caption_srt)])
        caption_index = 1
    meta_path = None
    meta_index = 2 if caption_srt is not None else 1
    if chapters and title.chapter_times:
        meta_path = chapters_file(title.chapter_times, title.duration,
                                  temporary / f"chapters-{output.stem}.ffmeta")
        args.extend(["-i", str(meta_path)])
    if map_all:
        args.extend(["-map", "0"])
    else:
        # Map by stream kind and ordinal rather than absolute index: a DVD
        # VOB carries a leading dvd_nav_packet data stream and Blu-ray clips
        # expose extra streams, so absolute indices are not stable.
        kind_ordinal: dict[int, int] = {}
        counters = {"video": 0, "audio": 0, "subtitle": 0}
        for stream in title.streams:
            if stream.codec_id == "S_CC608/DVD":
                continue
            kind_ordinal[id(stream)] = counters.get(stream.kind, 0)
            counters[stream.kind] = counters.get(stream.kind, 0) + 1
        letters = {"video": "v", "audio": "a", "subtitle": "s"}
        mapping: list[str] = []
        for stream in streams:
            if stream.codec_id == "S_CC608/DVD":
                if caption_index is None:
                    raise FFmpegError("closed captions were not extracted")
                mapping.extend(["-map", f"{caption_index}:0"])
                continue
            ordinal = kind_ordinal.get(id(stream))
            if ordinal is None:
                ordinal = 0
            mapping.extend(["-map", f"0:{letters.get(stream.kind, 'v')}:{ordinal}"])
        if not mapping:
            raise FFmpegError("no selected streams")
        args.extend(mapping)
    if copy_all:
        args.extend(["-c", "copy"])
    else:
        audio_codecs = {s.output_codec for s in streams if s.kind == "audio"}
        sub_codecs = {s.output_codec for s in streams if s.kind == "subtitle"}
        if "FLAC" in audio_codecs:
            args.extend(["-c:a", "flac"])
            if compression:
                args.extend(["-compression_level", compression])
        elif "AAC" in audio_codecs:
            args.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            args.extend(["-c:a", "copy"])
        text_subs = sub_codecs & {"SRT", "SubRip"}
        if text_subs and text_subs == sub_codecs:
            args.extend(["-c:s", "srt"])
        else:
            args.extend(["-c:s", "copy"])
            if text_subs:
                # The CC track is text while the disc's tracks are VobSub/PGS;
                # select the encoder per subtitle-output ordinal.
                ordinal = 0
                for stream in streams:
                    if stream.kind != "subtitle":
                        continue
                    if stream.output_codec in text_subs:
                        args.extend([f"-c:s:{ordinal}", "srt"])
                    ordinal += 1
        args.extend(["-c:v", "copy"])
    if meta_path is not None:
        args.extend(["-map_metadata", str(meta_index),
                     "-map_chapters", str(meta_index)])
    if rate:
        # Container metadata only: the packets keep their soft-telecine
        # timestamps, but players that consult r_frame_rate no longer guess a
        # bogus 119/4 fps from the alternating 0.033/0.050 durations.
        args.extend(["-r", rate])
    args.extend(["-f", container, str(output)])
    return args


def remux(
    inputs: Sequence[str],
    output: Path,
    streams: Sequence[Stream],
    title: Title,
    chapters: bool = True,
    compression: str | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    workdir: Path | None = None,
    caption_srt: Path | None = None,
) -> Path:
    """Remux the selected ``streams`` of ``inputs`` into ``output``.

    Some MPEG-PS streams (notably a few DVD authorings) produce packets the
    Matroska muxer rejects even though every other container accepts them.
    When that happens and no subtitle must be preserved, the payload is
    normalised through an MP4 intermediate and muxed again, still stream-copy.
    """
    # run_ffmpeg changes cwd to the destination, so every path handed to it
    # must be absolute.  GUI saves already are; CLI relative folders are not.
    workdir = Path(workdir or output.parent).resolve()
    output = Path(output).resolve()
    inputs = [str(Path(path).resolve()) for path in inputs]
    temporary = Path(tempfile.mkdtemp(prefix="opticrip-", dir=str(workdir)))
    try:
        source_args: list[str] = []
        if title.source == "dvd":
            # DVD PES packets frequently omit DTS, and soft-telecine frames
            # share timestamps.  Let the demuxer rebuild them; otherwise the
            # Matroska muxer emits duplicate/backwards DTS and players warble.
            source_args.extend(["-fflags", "+genpts"])
        if len(inputs) == 1:
            source_args.extend(["-i", inputs[0]])
        else:
            list_path = temporary / "input.ffconcat"
            concat_list(inputs, list_path)
            source_args.extend(["-f", "concat", "-safe", "0", "-i", str(list_path)])
        rate = _dvd_rate(title) if title.source == "dvd" else ""
        args = _mux_args(source_args, output, streams, title, chapters,
                         compression, temporary, map_all=False,
                         container="matroska", rate=rate or None,
                         caption_srt=caption_srt)
        try:
            run_ffmpeg(args, duration=title.duration, on_progress=on_progress,
                       cancelled=cancelled, cwd=str(workdir))
            return output
        except FFmpegError:
            if any(stream.kind == "subtitle" for stream in streams):
                raise
        intermediate = temporary / "intermediate.mp4"
        first = _mux_args(source_args, intermediate, streams, title, False,
                          None, temporary, map_all=False, container="mp4",
                          copy_all=True)
        run_ffmpeg(first, duration=title.duration, cancelled=cancelled, cwd=str(workdir))
        second = _mux_args(["-i", str(intermediate)], output, streams, title,
                           chapters, compression, temporary, map_all=True,
                           container="matroska", rate=rate or None)
        run_ffmpeg(second, duration=title.duration, on_progress=on_progress,
                   cancelled=cancelled, cwd=str(workdir))
        return output
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def can_decode_video(path: str) -> bool | None:
    """Return True/False when a video frame decodes, None if untestable.

    Used to distinguish CSS-scrambled VOBs (pack headers are clear, payload
    is not) from ordinary MPEG-PS files.
    """
    try:
        result = _run([
            tool("ffmpeg"), "-nostdin", "-v", "error", "-i", path,
            "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
        ], timeout=30)
    except (FFmpegError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def extract_frame(path: str, seconds: float, output: Path) -> bool:
    """Grab one frame; used by the UI's preview thumbnail."""
    try:
        result = _run([
            tool("ffmpeg"), "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{max(0.0, seconds):.3f}", "-i", path,
            "-frames:v", "1", "-vf", "scale=320:-1", str(output),
        ], timeout=30)
        return result.returncode == 0 and output.exists()
    except (FFmpegError, subprocess.SubprocessError):
        return False
