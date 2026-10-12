"""Providers for disc images and already-mounted folders.

These make the whole engine usable - and testable - without hardware, and
they are the mechanism behind the "open an ISO / open a folder" menu actions.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from ..core.interfaces import DriveProvider
from ..core.models import Drive, DriveKind, DriveState, MediaType
from ..disc.identify import MediaProbe, MediaProbeResult


def _default_probe():
    """Platform probe: WindowsMediaProbe-like on win32, Linux elsewhere."""
    import sys
    if sys.platform.startswith("win"):
        from .windows import _WindowsMediaProbeAdapter
        return _WindowsMediaProbeAdapter()
    from ..disc.identify import LinuxMediaProbe
    return LinuxMediaProbe()

_IMAGE_SUFFIXES = (".iso", ".img", ".udf", ".bin")


def locate_video_root(path: str) -> str:
    """The effective disc root for ``path``.

    Accepts a disc whose VIDEO_TS/BDMV is nested one level below the given
    folder (common for ISOs extracted with a top-level wrapper folder).
    Returns the directory that directly contains VIDEO_TS or BDMV, or the
    path unchanged when no nesting applies.
    """
    try:
        names = os.listdir(path)
    except OSError:
        return path
    upper = {n.upper() for n in names}
    if "VIDEO_TS" in upper or "BDMV" in upper:
        return path
    for entry in names:
        sub = os.path.join(path, entry)
        if os.path.isdir(sub):
            sub_upper = {n.upper() for n in _safe_listdir(sub)}
            if "VIDEO_TS" in sub_upper or "BDMV" in sub_upper:
                return sub
    return path


def classify_folder(root: str) -> MediaType:
    """DVD-Video / Blu-ray / data from the folder's own contents."""
    root = locate_video_root(root)
    try:
        names = os.listdir(root)
    except OSError:
        return MediaType.UNKNOWN
    upper = {n.upper() for n in names}
    if "BDMV" in upper:
        return MediaType.BLURAY
    if "VIDEO_TS" in upper:
        return MediaType.DVD_VIDEO
    return MediaType.DATA


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def folder_drive(root: str) -> Drive:
    """Build a ready-to-use :class:`Drive` for a directory."""
    root = os.path.abspath(os.path.expanduser(root))
    # Re-root at the level that actually holds VIDEO_TS/BDMV so the analyzer
    # (which opens drive.path) sees the sentinel files. classify_folder peeks
    # into the tree either way, but without this the analyzer would search at
    # the wrapper folder and report an unsupported structure.
    root = locate_video_root(root)
    media = classify_folder(root)
    exists = os.path.isdir(root)
    drive = Drive(
        path=root,
        kind=DriveKind.FOLDER,
        id=f"folder:{root}",
        vendor="Folder",
        model=os.path.basename(root) or root,
        bus="virtual",
        state=DriveState.READY if (exists and media.is_video) else
              (DriveState.READY if media is MediaType.DATA else DriveState.INACCESSIBLE),
        media_type=media,
        volume_label=os.path.basename(root),
        mount_points=[root],
    )
    if not exists:
        drive.state = DriveState.INACCESSIBLE
        drive.status_detail = "Folder no longer exists."
    elif not media.is_video:
        drive.status_detail = "Folder does not contain VIDEO_TS or BDMV."
    return drive


class FolderProvider(DriveProvider):
    """Exposes a fixed list of disc folders as drives."""

    name = "folder"

    def __init__(self, roots: Iterable[str] = ()) -> None:
        self.roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]

    def add(self, root: str) -> None:
        path = os.path.abspath(os.path.expanduser(root))
        if path not in self.roots:
            self.roots.append(path)

    def remove(self, root: str) -> None:
        if root in self.roots:
            self.roots.remove(root)

    def list_drives(self) -> list[Drive]:
        return [folder_drive(r) for r in self.roots]


class ImageProvider(DriveProvider):
    """Exposes disc image files (``.iso``/``.img``) as drives."""

    name = "image"

    def __init__(self, paths: Iterable[str] = (),
                 probe: MediaProbe | None = None) -> None:
        self.paths = [os.path.abspath(os.path.expanduser(p)) for p in paths]
        self.probe = probe or _default_probe()

    def add(self, path: str) -> None:
        path = os.path.abspath(os.path.expanduser(path))
        if path not in self.paths:
            self.paths.append(path)

    def remove(self, path: str) -> None:
        if path in self.paths:
            self.paths.remove(path)

    def list_drives(self) -> list[Drive]:
        return [self._build(p) for p in self.paths]

    def _build(self, path: str) -> Drive:
        drive = Drive(
            path=path,
            kind=DriveKind.IMAGE,
            id=f"image:{path}",
            vendor="Image",
            model=os.path.basename(path),
            bus="virtual",
            read_only=True,
        )
        if not os.path.isfile(path):
            drive.state = DriveState.INACCESSIBLE
            drive.status_detail = "Image file not found."
            return drive
        drive.size_bytes = os.path.getsize(path)
        result = self.probe.probe(path)
        if result.error:
            drive.status_detail = result.error
            drive.state = DriveState.INACCESSIBLE
            return drive
        drive.volume_label = result.volume_label or os.path.basename(path)
        drive.media_type = result.media_type
        # content classification from the ISO9660 tree, done lazily by the
        # analyzer; report UNKNOWN rather than lying here.
        drive.state = DriveState.READY
        drive.extras["filesystem"] = result.filesystem
        for note in result.protection:
            drive.extras["protection"] = note
        return drive


class AutoPathProvider(DriveProvider):
    """Accepts a mix of devices, images and folders on the command line.

    ``opticrip /dev/sr0 movie.iso ~/discs/THING`` just works.
    """

    name = "auto-path"

    def __init__(self, paths: Iterable[str],
                 probe: MediaProbe | None = None) -> None:
        self.probe = probe or _default_probe()
        self.folders = FolderProvider()
        self.images = ImageProvider(probe=self.probe)
        self.raw: list[str] = []
        for raw in paths:
            path = os.path.expanduser(raw)
            if os.path.isdir(path):
                self.folders.add(path)
            elif path.lower().endswith(_IMAGE_SUFFIXES) or os.path.isfile(path):
                self.images.add(path)
            else:
                self.raw.append(path)

    def list_drives(self) -> list[Drive]:
        drives = self.folders.list_drives() + self.images.list_drives()
        for node in self.raw:
            drive = Drive(path=node, kind=DriveKind.RAW, id=node,
                          vendor="Optical drive", model=os.path.basename(node))
            result = self.probe.probe(node)
            if result.error:
                drive.state = DriveState.INACCESSIBLE
                drive.status_detail = result.error
            elif not result.has_media:
                drive.state = DriveState.NO_DISC
            else:
                drive.state = DriveState.READY
                drive.media_type = result.media_type
                drive.volume_label = result.volume_label
                drive.size_bytes = result.capacity_bytes
            drives.append(drive)
        return drives
