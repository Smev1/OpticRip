"""Output validation.

Re-opens a finished MKV with ffprobe and checks it against what the user
asked for. Anything less than an honest, specific report is treated as a
failure: a "success" that the user cannot re-verify later is worthless for
a disc ripper.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import Counter

from ..core.errors import CancelledError, ToolMissingError, ValidationFailedError
from ..core.interfaces import CancelCheck, OutputValidator, never_cancelled
from ..core.logging_bus import LogBus
from ..core.models import (
    StreamInfo,
    StreamKind,
    ValidationReport,
    format_bytes,
    format_duration,
)
from .tools import Toolchain

_KIND_MAP = {
    "video": StreamKind.VIDEO,
    "audio": StreamKind.AUDIO,
    "subtitle": StreamKind.SUBTITLE,
}


class FfprobeValidator(OutputValidator):
    """:class:`OutputValidator` backed by ffprobe."""

    name = "ffprobe"

    def __init__(self, toolchain: Toolchain | None = None,
                 log: LogBus | None = None) -> None:
        self.tc = toolchain or Toolchain.detect()
        self._log = log or LogBus()

    # ---------------------------------------------------------------- #
    def validate(self, path: str, *, expected: list[StreamInfo] | None = None,
                 expected_chapters: int | None = None,
                 expected_duration: float | None = None,
                 cancelled: CancelCheck = never_cancelled) -> ValidationReport:
        ffprobe = self.tc.require("ffprobe")
        log = self._log.bind("validate")
        report = ValidationReport()

        if not os.path.isfile(path):
            raise ValidationFailedError(f"output file disappeared: {path}")
        size = os.path.getsize(path)
        if size == 0:
            raise ValidationFailedError("output file is empty")

        args = [ffprobe, "-hide_banner", "-loglevel", "error",
                "-show_streams", "-show_format", "-show_chapters",
                "-print_format", "json", path]
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationFailedError(f"ffprobe could not run on {path}",
                                        detail=str(exc)) from exc
        if proc.returncode != 0:
            raise ValidationFailedError(
                "ffprobe could not read the output file",
                detail=(proc.stderr or "")[-2000:])
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationFailedError(
                "ffprobe output was not JSON", detail=str(exc)) from exc

        fmt = data.get("format", {})
        report.container = fmt.get("format_name", "")
        try:
            report.duration_seconds = float(fmt.get("duration") or 0.0)
        except (TypeError, ValueError):
            report.duration_seconds = 0.0
        report.size_bytes = size

        streams = []
        for raw in data.get("streams", []):
            streams.append({
                "kind": _KIND_MAP.get(raw.get("codec_type"), StreamKind.OTHER),
                "codec": raw.get("codec_name") or "",
                "language": (raw.get("tags") or {}).get("language"),
                "channels": raw.get("channels"),
                "width": raw.get("width"),
                "height": raw.get("height"),
            })
        report.stream_summaries = [
            f"{s['kind'].label} {s['codec']}"
            + (f" ({s['language']})" if s.get("language") else "")
            for s in streams]

        if expected:
            want = Counter(s.kind for s in expected)
            got = Counter(s["kind"] for s in streams)
            for kind in set(want) | set(got):
                if want.get(kind, 0) != got.get(kind, 0):
                    report.add_issue(
                        f"{kind.label if hasattr(kind, 'label') else kind} stream "
                        f"count mismatch: requested {want.get(kind, 0)}, "
                        f"got {got.get(kind, 0)}")
            # language spot-check for tracks where we asked for one
            want_langs = [s.language for s in expected if s.language]
            got_langs = [s.get("language") for s in streams if s.get("language")]
            for lang in want_langs:
                if lang not in got_langs:
                    report.add_issue(f"requested language '{lang}' is missing "
                                     "from the output")

        chapters = data.get("chapters", [])
        if expected_chapters is not None and len(chapters) != expected_chapters:
            report.add_issue(
                f"chapter count mismatch: requested {expected_chapters}, "
                f"got {len(chapters)}")

        if expected_duration and report.duration_seconds and abs(
                report.duration_seconds - expected_duration) > max(3.0,
                0.05 * expected_duration):
            report.add_issue(
                f"duration mismatch: requested ~{format_duration(expected_duration)}, "
                f"got {format_duration(report.duration_seconds)}")

        if not report.issues:
            report.ok = True
            log.info(f"validation OK: {report.container}, "
                     f"{report.duration_seconds:.1f}s, "
                     f"{len(streams)} streams "
                     f"({format_bytes(size)})")
        else:
            log.error("validation failed: " + "; ".join(report.issues))
        return report


class AlwaysPassValidator(OutputValidator):
    """For tests: bypass probe, assert only that a file exists."""

    name = "always-pass"

    def __init__(self) -> None:
        pass

    def validate(self, path: str, *, expected=None, expected_chapters=None,
                 expected_duration=None, cancelled=never_cancelled) -> ValidationReport:
        report = ValidationReport()
        report.ok = os.path.isfile(path) and os.path.getsize(path) > 0
        report.size_bytes = os.path.getsize(path) if report.ok else 0
        report.container = "unknown"
        return report
