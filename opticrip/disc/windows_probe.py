r"""Windows media probing and block reads (Windows client).

Pure ``ctypes`` against kernel32.  Strategy, per Microsoft docs:

* open ``\\.\D:`` (volume) with ``CreateFileW`` + ``FILE_SHARE_READ`` and
  read *cooked* 2048-byte sectors with plain buffered ``ReadFile`` —
  buffered I/O avoids the ``FILE_FLAG_NO_BUFFERING`` alignment traps;
* an empty drive typically surfaces as ``ERROR_NOT_READY`` (21) when the
  volume info is queried — reported as "no disc", never as a generic
  failure;
* ``SEM_FAILCRITICALERRORS`` is set so Windows does not pop a "there is no
  disc in the drive" dialog during probing.

Everything Windows-specific stays in this module; the rest of the codebase
uses the same ``MediaProbe`` contract as Linux.
"""
from __future__ import annotations

import os
import sys

from ..core.models import MediaType

SECTOR = 2048
PVD_SECTOR = 16

ERROR_NOT_READY = 21
ERROR_NO_MEDIA_IN_DRIVE = 1112
ERROR_INVALID_FUNCTION = 1
ERROR_ACCESS_DENIED = 5
ERROR_FILE_NOT_FOUND = 2

_VOLUME_LABEL_MAX = 261

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    # Suppress the "no disc in the drive" shell prompt during probes.
    SEM_FAILCRITICALERRORS = 0x0001
    kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)

    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel32.ReadFile.restype = ctypes.c_int32
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetLastError.restype = ctypes.c_uint32
    kernel32.GetDriveTypeW.restype = ctypes.c_uint32
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetLogicalDrives.restype = ctypes.c_uint32
    kernel32.GetLogicalDrives.argtypes = []
    kernel32.GetVolumeInformationW.restype = ctypes.c_int32
    kernel32.GetVolumeInformationW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_wchar_p, ctypes.c_uint32]
    kernel32.SetFilePointerEx.restype = ctypes.c_int32
    kernel32.SetFilePointerEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64),
        ctypes.c_uint32]
    kernel32.GetFileSizeEx.restype = ctypes.c_int32
    kernel32.GetFileSizeEx.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)]
else:
    #: Import-safe stub on POSIX; every public function checks IS_WINDOWS
    #: before touching it and tests monkeypatch it with raising=False.
    kernel32 = None  # type: ignore[assignment]


class WindowsVolumeError(OSError):
    """Open/read/seek failure with the win32 error code attached."""

    def __init__(self, message: str, win_error: int) -> None:
        super().__init__(message)
        self.win_error = win_error


# --------------------------------------------------------------------------- #
# Sector reads
# --------------------------------------------------------------------------- #

def _open_volume(drive_letter: str):
    r"""Open \\.\D:; returns the handle or raises WindowsVolumeError."""
    path = f"\\\\.\\{drive_letter}"
    handle = kernel32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, 0, None)
    if handle in (None, INVALID_HANDLE_VALUE, 0):
        raise WindowsVolumeError(f"cannot open {path}",
                                 kernel32.GetLastError())
    return handle


def _read_at(handle, offset: int, length: int) -> bytes:
    """ReadFile at ``offset`` (cooked sectors, buffered)."""
    import ctypes
    moved = ctypes.c_int64(0)
    if not kernel32.SetFilePointerEx(handle, offset, ctypes.byref(moved), 0):
        raise WindowsVolumeError(f"seek to {offset} failed",
                                 kernel32.GetLastError())
    buf = ctypes.create_string_buffer(length)
    got = ctypes.c_uint32(0)
    if not kernel32.ReadFile(handle, buf, length, ctypes.byref(got), None):
        raise WindowsVolumeError("read failed", kernel32.GetLastError())
    return buf.raw[:got.value]


def read_sectors(root: str, sector: int, count: int = 1) -> bytes | None:
    """Read ``count`` sectors of ``root`` ('D:', 'D:\\', or image path).

    Image/file paths use plain file reads.  Returns ``None`` (never raises)
    for not-ready media so the drive watcher can keep polling.
    """
    if os.path.isfile(root):
        try:
            with open(root, "rb") as handle:
                handle.seek(sector * SECTOR)
                return handle.read(count * SECTOR)
        except OSError:
            return None
    if not IS_WINDOWS:
        return None
    letter = os.path.splitdrive(os.path.abspath(root))[0].lstrip("/") \
        or root
    try:
        handle = _open_volume(letter)
    except WindowsVolumeError:
        return None
    try:
        return _read_at(handle, sector * SECTOR, count * SECTOR)
    except WindowsVolumeError:
        return None
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# Media probe (mirrors the LinuxMediaProbe contract)
# --------------------------------------------------------------------------- #

def probe_root(root: str):
    """Classify media at ``root`` (drive letter, image or folder)."""
    from .identify import (
        MediaProbeResult, _classify_iso9660_root, is_iso9660_pvd,
        iso9660_volume_label, udf_versions,
    )
    result = MediaProbeResult()

    if os.path.isdir(root):
        result.has_media = True
        result.media_type = MediaType.UNKNOWN
        try:
            result.volume_label = _volume_label(root) or ""
        except WindowsVolumeError as exc:
            if exc.win_error in (ERROR_NOT_READY, ERROR_NO_MEDIA_IN_DRIVE):
                result.media_type = MediaType.EMPTY
            else:
                result.error = f"cannot access {root}"
        return result

    available = read_sectors(root, PVD_SECTOR, 6)
    if available is None:
        result.media_type = MediaType.EMPTY
        result.error_hint = "Insert a disc."
        return result
    if len(available) < 6 * SECTOR:
        result.error = "the disc did not return its volume descriptors"
        result.error_hint = ("The disc may be dirty, scratched or "
                             "not finalised.")
        return result

    pvd = available[:SECTOR]
    result.has_media = True
    if is_iso9660_pvd(pvd):
        result.filesystem = "ISO9660"
        result.volume_label = iso9660_volume_label(pvd)
    labels = udf_versions(available)
    if labels:
        result.filesystem = (result.filesystem + "+UDF") if result.filesystem \
            else "UDF"
        result.raw_signatures["udf"] = ",".join(sorted(set(labels)))
    result.media_type = MediaType.UNKNOWN if result.filesystem \
        else MediaType.DATA
    if is_iso9660_pvd(pvd):
        # Same content classification as the Linux probe: peek the PVD root
        # extent for the VIDEO_TS/BDMV sentinels.  read_sectors handles both
        # drive letters and image files on Windows.
        result.media_type = _classify_iso9660_root(
            lambda lba, n: read_sectors(root, lba, n) or b"",
            pvd, result.media_type)
    if os.path.isfile(root):
        result.capacity_bytes = os.path.getsize(root)
    return result


def _volume_label(root: str) -> str | None:
    """GetVolumeInformationW on 'D:\\' — raises on NOT READY."""
    import ctypes
    from os.path import splitdrive
    if not IS_WINDOWS:
        return None
    drv = splitdrive(os.path.abspath(root))[0]
    if not drv:
        raise WindowsVolumeError("no drive on path", 3)
    root_path = drv + os.sep if drv.endswith(":") else drv
    name = ctypes.create_unicode_buffer(_VOLUME_LABEL_MAX)
    if not kernel32.GetVolumeInformationW(root_path, name,
                                          _VOLUME_LABEL_MAX,
                                          None, None, None, None, 0):
        raise WindowsVolumeError(f"no volume on {root_path}",
                                 kernel32.GetLastError())
    return name.value


# --------------------------------------------------------------------------- #
# Drive discovery
# --------------------------------------------------------------------------- #

DRIVE_CDROM = 5


def list_cdrom_letters() -> list[str]:
    """['D:', 'E:'] for every drive Windows tagged DRIVE_CDROM (5)."""
    import string
    if not IS_WINDOWS or kernel32 is None:
        return []
    bits = kernel32.GetLogicalDrives()
    letters = []
    for index, letter in enumerate(string.ascii_uppercase):
        if bits & (1 << index):
            root = f"{letter}:\\"
            if kernel32.GetDriveTypeW(root) == DRIVE_CDROM:
                letters.append(f"{letter}:")
    return letters



def _volume_capacity_win(path: str) -> int | None:  # pragma: no cover
    """Capacity via GetFileSizeEx for the volume/dev handle on Windows."""
    letter = os.path.splitdrive(os.path.abspath(path))[0].lstrip("\\") \
        or path
    handle = _open_volume(letter)
    try:
        import ctypes
        size = ctypes.c_int64(0)
        if kernel32.GetFileSizeEx(handle, ctypes.byref(size)):
            return size.value
        return None
    finally:
        kernel32.CloseHandle(handle)
