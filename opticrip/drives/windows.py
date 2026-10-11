"""Windows optical drive discovery (Phase 9 — Windows client).

``GetDriveTypeW(DRIVE_CDROM)`` finds CD/DVD/BD letter drives; media state
and volume label come from the same probe layer Linux uses, so the
``Drive`` model, the watcher and every downstream component behave
identically on both platforms.
"""
from __future__ import annotations

import os

from ..core.logging_bus import LogBus
from ..core.models import Drive, DriveKind, DriveState, MediaType
from ..disc.identify import MediaProbe, MediaProbeResult
from ..disc import windows_probe as wp
from ..core.interfaces import DriveProvider


class WindowsDriveProvider(DriveProvider):
    """Enumerate drive letters typed ``DRIVE_CDROM`` (5)."""

    name = "windows-cdrom"

    def __init__(self, *, probe: MediaProbe | None = None,
                 log: LogBus | None = None) -> None:
        self.probe = probe or _WindowsMediaProbeAdapter()
        self._log = log or LogBus()

    # -- public --------------------------------------------------------- #
    def list_drives(self) -> list[Drive]:
        return [self._build(letter) for letter in
                wp.list_cdrom_letters()]

    def list_device_nodes(self) -> list[str]:
        return list(wp.list_cdrom_letters())

    # -- internals ------------------------------------------------------ #
    def _build(self, letter: str) -> Drive:
        root = f"{letter}\\"
        drive = Drive(
            path=letter,          # already absolute. "D:" works with open()
            kind=DriveKind.RAW,
            id=letter,
            bus="sata",
            mount_points=[root],
        )
        result = self.probe.probe(letter)
        drive.media_type = result.media_type
        drive.volume_label = result.volume_label
        drive.size_bytes = result.capacity_bytes
        if result.media_type is MediaType.EMPTY:
            drive.state = DriveState.NO_DISC
            drive.status_detail = "No disc in the drive."
        elif result.error:
            drive.state = DriveState.INACCESSIBLE
            drive.status_detail = result.error
        else:
            drive.state = DriveState.READY
        self._log.debug(f"{letter}: {drive.state.label} "
                        f"{drive.volume_label or '-'}")
        return drive


class _WindowsMediaProbeAdapter(MediaProbe):
    """Adapts :func:`windows_probe.probe_root` to the ``MediaProbe`` ABC."""

    def probe(self, device_path: str) -> MediaProbeResult:
        return wp.probe_root(device_path)
