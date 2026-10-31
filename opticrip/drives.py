"""Optical drive enumeration (Windows).

The reference always emits 16 ``DRV`` slots (R-ROB-04) and names a drive
``<vendor> <product> <revision> <serial>``.  Everything here is best effort:
a drive that refuses the storage query still appears, just with a generic
name, because the UI must be usable without administrator rights.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DRIVE_CDROM = 5
DRIVE_NO_ROOT_DIR = 1
MAX_SLOTS = 16

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
STORAGE_DEVICE_PROPERTY = 0
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass
class DriveInfo:
    index: int
    device: str                      # "D:"
    path: str                        # "D:\\"
    present: bool = False
    label: str = ""
    vendor: str = ""
    product: str = ""
    revision: str = ""
    serial: str = ""
    media_kind: str = "Data"         # DVD | Blu-ray | Data
    capacity: int = 0
    media_flags: int = 0

    @property
    def name(self) -> str:
        parts = [p for p in (self.vendor, self.product, self.revision, self.serial) if p]
        return " ".join(parts).strip() or f"Optical drive {self.device}"

    @property
    def state(self) -> int:
        # 2 with media, 256 when empty; observed in the reference capture.
        return 2 if self.present else 256

    @property
    def slot_visible(self) -> int:
        return 1 if self.present else 0


def _configure_kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.DeviceIoControl.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetLogicalDrives.restype = wintypes.DWORD
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    ]
    k32.GetVolumeInformationW.restype = wintypes.BOOL
    k32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ]
    k32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    return k32


if sys.platform == "win32":  # pragma: no branch
    _K32 = _configure_kernel32()
else:  # pragma: no cover - the GUI is Windows-only; tests skip drive code
    _K32 = None


def _open_device(device: str):
    if _K32 is None:
        return None
    handle = _K32.CreateFileW(
        f"\\\\.\\{device}", GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        return None
    return handle


def _close(handle) -> None:
    if handle and _K32 is not None:
        _K32.CloseHandle(handle)


def _asciiz(blob: bytes, offset: int) -> str:
    if not offset or offset >= len(blob):
        return ""
    end = blob.find(b"\x00", offset)
    if end < 0:
        end = len(blob)
    raw = blob[offset:end]
    return raw.decode("ascii", "ignore").strip()


def query_descriptor(device: str) -> dict[str, str]:
    """Read vendor/product/revision/serial via IOCTL_STORAGE_QUERY_PROPERTY."""
    handle = _open_device(device)
    if not handle:
        return {}
    try:
        # STORAGE_PROPERTY_QUERY: PropertyId(4), QueryType(4), extra[1]
        query = ctypes.create_string_buffer(12)
        ctypes.memmove(query, ctypes.byref(ctypes.c_int(STORAGE_DEVICE_PROPERTY)), 4)
        ctypes.memmove(ctypes.byref(query, 4), ctypes.byref(ctypes.c_int(0)), 4)
        out = ctypes.create_string_buffer(1024)
        returned = wintypes.DWORD(0)
        ok = _K32.DeviceIoControl(
            handle, IOCTL_STORAGE_QUERY_PROPERTY, query, 12,
            out, 1024, ctypes.byref(returned), None,
        )
        if not ok:
            return {}
        blob = out.raw[: max(returned.value, 36)]
        # STORAGE_DEVICE_DESCRIPTOR
        vendor_off = int.from_bytes(blob[12:16], "little")
        product_off = int.from_bytes(blob[16:20], "little")
        revision_off = int.from_bytes(blob[20:24], "little")
        serial_off = int.from_bytes(blob[24:28], "little")
        return {
            "vendor": _asciiz(blob, vendor_off),
            "product": _asciiz(blob, product_off),
            "revision": _asciiz(blob, revision_off),
            "serial": _asciiz(blob, serial_off),
        }
    finally:
        _close(handle)


def _volume_label(path: str) -> str:
    if _K32 is None:
        return ""
    buffer = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_len = wintypes.DWORD()
    flags = wintypes.DWORD()
    fs_name = ctypes.create_unicode_buffer(261)
    ok = _K32.GetVolumeInformationW(
        path, buffer, 261, ctypes.byref(serial), ctypes.byref(max_len),
        ctypes.byref(flags), fs_name, 261,
    )
    return buffer.value if ok else ""


def _capacity(path: str) -> int:
    if _K32 is None:
        return 0
    total = ctypes.c_ulonglong()
    free = ctypes.c_ulonglong()
    available = ctypes.c_ulonglong()
    ok = _K32.GetDiskFreeSpaceExW(
        path, ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)
    )
    return int(total.value) if ok else 0


def classify_media(root: Path) -> tuple[str, int]:
    """Return (media kind, AP_DskFs flags) for a mounted volume."""
    flags = 0
    video_ts = root / "VIDEO_TS"
    bdmv = root / "BDMV"
    if (video_ts / "VIDEO_TS.IFO").is_file() or video_ts.is_dir():
        flags |= 1
    if (bdmv / "index.bdmv").is_file():
        flags |= 4
    if (bdmv / "AACS").is_dir() or (root / "AACS").is_dir():
        flags |= 8
    if any((bdmv / name).is_file() for name in ("MovieObject.bdmv", "index.bdmv")):
        return "Blu-ray", flags
    if video_ts.is_dir():
        return "DVD", flags
    return "Data", flags


def probe_drive(letter: str, index: int) -> DriveInfo:
    device = letter.upper().rstrip(":\\") + ":"
    path = device + "\\"
    info = DriveInfo(index=index, device=device, path=path)
    descriptor = query_descriptor(device)
    info.vendor = descriptor.get("vendor", "")
    info.product = descriptor.get("product", "")
    info.revision = descriptor.get("revision", "")
    info.serial = descriptor.get("serial", "")
    info.label = _volume_label(path)
    try:
        info.present = Path(path).exists() and (bool(info.label) or os.access(path, os.R_OK))
    except OSError:
        info.present = False
    if info.present:
        info.media_kind, info.media_flags = classify_media(Path(path))
        info.capacity = _capacity(path)
    return info


def enumerate_drives() -> list[DriveInfo]:
    if _K32 is None or sys.platform != "win32":  # pragma: no cover
        return []
    mask = _K32.GetLogicalDrives()
    drives: list[DriveInfo] = []
    for offset in range(26):
        if not mask & (1 << offset):
            continue
        letter = chr(ord("A") + offset)
        root = f"{letter}:\\"
        if _K32.GetDriveTypeW(root) != DRIVE_CDROM:
            continue
        drives.append(probe_drive(letter, len(drives)))
    return drives


def drive_slots(drives: list[DriveInfo] | None = None) -> list[DriveInfo | None]:
    """16 fixed slots; the reference always emits indices 0..15."""
    drives = enumerate_drives() if drives is None else drives
    slots: list[DriveInfo | None] = list(drives[:MAX_SLOTS])
    slots.extend([None] * (MAX_SLOTS - len(slots)))
    return slots


def find_drive(device: str) -> DriveInfo | None:
    wanted = device.upper().rstrip(":\\")
    for drive in enumerate_drives():
        if drive.device.upper().rstrip(":\\") == wanted:
            return drive
    return None


def eject(device: str) -> bool:
    """Open the tray.  Falls back silently when the OS refuses."""
    handle = _open_device(device.rstrip(":\\"))
    if not handle:
        return False
    try:
        returned = wintypes.DWORD(0)
        ok = _K32.DeviceIoControl(
            handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0,
            ctypes.byref(returned), None,
        )
        return bool(ok)
    finally:
        _close(handle)


def disc_timestamp(path: str) -> str:
    """Best-effort volume timestamp; empty when unavailable."""
    try:
        stat = os.stat(path)
        import time
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
    except OSError:
        return ""
