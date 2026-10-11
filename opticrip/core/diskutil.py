"""Disk-space accounting.

Optical rips are large (a dual-layer DVD title is 4-9 GB, a Blu-ray title can
be 40 GB+) so we refuse to start a job that cannot possibly fit, *before*
spending an hour reading the disc.
"""

from __future__ import annotations

import os
import shutil

from .interfaces import DiskSpaceChecker
from .models import StreamInfo, StreamKind, Title, TitleSelection, selection_to_streams

#: fraction of the source size added as headroom for container overhead
CONTAINER_OVERHEAD = 0.03
#: never start a rip that would leave less than this free
SAFETY_MARGIN_BYTES = 512 * 1024 * 1024


class OsDiskSpaceChecker(DiskSpaceChecker):
    """Uses ``os.statvfs`` on the destination, falling back to ``shutil``."""

    name = "statvfs"

    def free_bytes(self, path: str) -> int:
        target = path
        while target and not os.path.exists(target):
            parent = os.path.dirname(target.rstrip("/"))
            if parent == target:
                break
            target = parent
        if not target:
            target = "."
        try:
            usage = shutil.disk_usage(target)
            return usage.free
        except OSError:
            pass
        try:
            stat = os.statvfs(target)
            return stat.f_bavail * stat.f_frsize
        except OSError as exc:
            raise OSError(f"cannot determine free space for {path!r}: {exc}") from exc

    def required_bytes(self, title: Title, selection: TitleSelection) -> int:
        """Estimate the MKV size from the source title size or bitrates.

        Preference order:
          1. exact title size recovered from the disc structure;
          2. summed stream bitrates x duration (audio/subtitle only adds);
          3. a resolution-based rule of thumb.
        """
        if title.size_bytes and title.size_bytes > 0:
            return int(title.size_bytes * (1 + CONTAINER_OVERHEAD))

        selected = selection_to_streams(title, selection)
        video = next((s for s in selected if s.kind is StreamKind.VIDEO), None)
        duration = max(0.0, title.duration_seconds)

        if video is not None and video.bitrate and duration:
            total = 0.0
            for s in selected:
                rate = s.bitrate
                if s.kind is StreamKind.VIDEO:
                    total += rate or 0
                elif s.kind is StreamKind.AUDIO:
                    total += rate or 320_000
                elif s.kind is StreamKind.SUBTITLE:
                    total += 24_000
            if total <= 0:
                total = float(video.bitrate)
            estimate = total / 8.0 * duration
            return int(estimate * (1 + CONTAINER_OVERHEAD))

        if duration:
            rough = _rough_bitrate(video)
            return int(rough / 8.0 * duration * (1 + CONTAINER_OVERHEAD))
        return 0

    def check(self, directory: str, title: Title, selection: TitleSelection,
              margin: int = SAFETY_MARGIN_BYTES) -> tuple[bool, int, int]:
        """``(fits, required, free)`` - never raises for a missing directory."""
        try:
            free = self.free_bytes(directory)
        except OSError:
            return (True, 0, 0)  # unknown: let the rip surface the real error
        required = self.required_bytes(title, selection)
        return (free - required >= margin, required, free)


def _rough_bitrate(video: StreamInfo | None) -> float:
    """bits/s rule of thumb when the disc gives us nothing better."""
    if video is None:
        return 8_000_000
    pixels = (video.width or 720) * (video.height or 480)
    if pixels >= 1920 * 1080:
        return 30_000_000   # Blu-ray HD
    if pixels >= 1280 * 720:
        return 15_000_000
    return 6_000_000        # DVD/SD
