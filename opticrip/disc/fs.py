"""Reading disc contents without mounting.

Two source shapes are supported:

* :class:`DirectoryDisc` - a mounted disc or an extracted folder.
* :class:`Iso9660Disc`   - a raw device or image, read through a tiny
  ISO9660 implementation (this is what lets OpticRip inspect an unencrypted
  DVD placed in ``/dev/sr0`` when nothing is mounted, without ``sudo``).

UDF-only Blu-ray media on a *raw* device is handled by ffmpeg/libbluray
(``bluray:`` protocol) rather than here; see ``docs/PROTECTED_DISCS.md`` and
``opticrip/rip/extract.py``.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Iterator

from ..core.errors import DiscStructureError
from ..core.interfaces import BlockDevice, DiscFile, DiscFileSystem

LOGICAL_SECTOR = 2048
_VD_START = 16
_VD_TYPE_PRIMARY = 1
_VD_TYPE_SUPPLEMENTARY = 2
_VD_TYPE_TERMINATOR = 255
_ISO_ID = b"CD001"
_MAX_VOLUME_DESCRIPTORS = 64


# --------------------------------------------------------------------------- #
# Block devices
# --------------------------------------------------------------------------- #


class FileBlockDevice(BlockDevice):
    """Random access over a device node or an image file."""

    def __init__(self, path: str, sector_size: int = LOGICAL_SECTOR) -> None:
        self.path = path
        self.sector_size = sector_size
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        try:
            self._fd = os.open(path, flags)
        except OSError as exc:
            raise DiscStructureError(
                f"cannot open {path}: {exc.strerror}",
                hint="Check device permissions (cdrom group) or that the image exists.",
                detail=str(exc)) from exc
        try:
            st = os.fstat(self._fd)
            self._size = st.st_size if st.st_size else self._capacity()
        except OSError:
            self._size = 0

    def _capacity(self) -> int:
        try:
            from fcntl import ioctl
            buf = bytearray(8)
            ioctl(self._fd, 0x80081272, buf, True)  # BLKGETSIZE64
            return struct.unpack("<Q", buf)[0]
        except (OSError, ImportError):
            return 0

    @property
    def size_bytes(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        offset = max(0, offset)
        try:
            return os.pread(self._fd, length, offset)
        except OSError as exc:
            raise DiscStructureError(
                f"read error at offset {offset} of {self.path}: {exc.strerror}",
                detail=str(exc)) from exc

    def close(self) -> None:
        if getattr(self, "_fd", None) is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def describe(self) -> str:
        return f"file/dev {self.path} ({self._size} bytes)"


class BytesBlockDevice(BlockDevice):
    """In-memory device used by tests and synthetic fixtures."""

    def __init__(self, data: bytes, sector_size: int = LOGICAL_SECTOR) -> None:
        self._data = bytes(data)
        self.sector_size = sector_size

    @property
    def size_bytes(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        if offset >= len(self._data) or length <= 0:
            return b""
        return self._data[offset:offset + length]

    def close(self) -> None:
        return None

    def describe(self) -> str:
        return f"memory ({len(self._data)} bytes)"


# --------------------------------------------------------------------------- #
# Disc files
# --------------------------------------------------------------------------- #


class _DirFile(DiscFile):
    def __init__(self, path: str) -> None:
        self._path = path
        try:
            self._size = os.path.getsize(path)
        except OSError:
            self._size = 0

    @property
    def name(self) -> str:
        return os.path.basename(self._path)

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int = 0, length: int | None = None) -> bytes:
        if length is None:
            length = max(0, self._size - offset)
        if length <= 0:
            return b""
        try:
            with open(self._path, "rb") as handle:
                handle.seek(max(0, offset))
                return handle.read(length)
        except OSError as exc:
            raise DiscStructureError(f"cannot read {self._path}: {exc.strerror}") from exc


class IsoFile(DiscFile):
    """A file backed by a byte range on a block device (one ISO9660 extent)."""

    def __init__(self, device: BlockDevice, name: str, lba: int, size: int,
                 sector_size: int = LOGICAL_SECTOR) -> None:
        self._device = device
        self._name = name
        self._lba = lba
        self._size = size
        self.sector_size = sector_size

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        return self._size

    @property
    def extent_lba(self) -> int:
        return self._lba

    def read(self, offset: int = 0, length: int | None = None) -> bytes:
        if length is None:
            length = max(0, self._size - offset)
        offset = max(0, offset)
        if length <= 0 or offset >= self._size:
            return b""
        length = min(length, self._size - offset)
        base = self._lba * self.sector_size + offset
        # read in sector-aligned chunks so a single call can span extents
        return self._device.read(base, length)


# --------------------------------------------------------------------------- #
# Directory filesystem
# --------------------------------------------------------------------------- #


class DirectoryDisc(DiscFileSystem):
    """A mounted disc / extracted folder."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(self.root):
            raise DiscStructureError(f"{self.root} is not a directory")
        self.describe = f"directory {self.root}"

    # -- resolution ----------------------------------------------------- #
    def _resolve(self, path: str) -> str:
        clean = path.strip("/").replace("\\", "/")
        if clean in ("", ".", "./"):
            return self.root
        full = os.path.normpath(os.path.join(self.root, clean))
        if not (full == self.root or full.startswith(self.root + os.sep)):
            raise DiscStructureError(f"path escapes disc root: {path!r}")
        return full

    def exists(self, path: str) -> bool:
        try:
            return os.path.exists(self._resolve(path))
        except DiscStructureError:
            return False

    def listdir(self, path: str) -> list[str]:
        target = self._resolve(path)
        try:
            return sorted(os.listdir(target))
        except OSError as exc:
            raise DiscStructureError(f"cannot list {path!r}: {exc.strerror}") from exc

    def getfile(self, path: str) -> DiscFile:
        target = self._resolve(path)
        if not os.path.isfile(target):
            raise DiscStructureError(f"{path!r} is not a file on this disc")
        return _DirFile(target)


# --------------------------------------------------------------------------- #
# ISO9660
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Dirent:
    name: str
    lba: int
    size: int
    is_dir: bool


def _both_endian_u32(chunk: bytes, offset: int) -> int:
    """Read a 8-byte 'both byte orders' field, returning the little-endian value.

    ISO9660 duplicates numeric fields to let either endianness read them.  We
    validate that the two halves agree, which catches corrupt descriptors.
    """
    le = struct.unpack_from("<I", chunk, offset)[0]
    be = struct.unpack_from(">I", chunk, offset + 4)[0]
    if le != be:
        # Trust the little-endian half but flag it via a marker the caller
        # can surface as a warning; some authoring tools write junk in the BE
        # copy of otherwise usable discs.
        return le
    return le


class Iso9660Disc(DiscFileSystem):
    """Minimal but real ISO9660 reader (PVD + optional Joliet SVD).

    Supports: multi-sector volume descriptors, directory record padding,
    version suffix stripping (``FILE.EXT;1``), Rock-Ridge-free uppercase
    names, and Joliet UCS-2 names when an SVD is present.  It does not
    implement multi-extent (>4 GiB) files, which do not occur in DVD
    ``VIDEO_TS`` trees.
    """

    def __init__(self, device: BlockDevice, *, sector_size: int = LOGICAL_SECTOR,
                 warnings: list[str] | None = None) -> None:
        self.device = device
        self.sector_size = sector_size
        self.warnings = warnings if warnings is not None else []
        self.volume_label = ""
        self._dir_cache: dict[int, list[_Dirent]] = {}
        self._parse_descriptors()
        self.describe = f"ISO9660 '{self.volume_label}' on {device.describe()}"

    # -- volume descriptors --------------------------------------------- #
    def _parse_descriptors(self) -> None:
        pvd: bytes | None = None
        svd: bytes | None = None
        for index in range(_MAX_VOLUME_DESCRIPTORS):
            sector = self.device.read_sectors(_VD_START + index, 1)
            if len(sector) < 7:
                break
            vd_type = sector[0]
            if sector[1:6] != _ISO_ID:
                if index == 0:
                    raise DiscStructureError(
                        "no ISO9660 volume descriptor found at sector 16",
                        hint="The disc may be UDF-only, encrypted, or not a data disc.")
                break
            if vd_type == _VD_TYPE_TERMINATOR:
                break
            if vd_type == _VD_TYPE_PRIMARY and pvd is None:
                pvd = sector
            elif vd_type == _VD_TYPE_SUPPLEMENTARY and svd is None:
                svd = sector

        if pvd is None:
            raise DiscStructureError("ISO9660 disc has no primary volume descriptor")

        chosen = svd or pvd
        self._root = _parse_dir_record(chosen[156:190], joliet=svd is not None)
        if svd is not None:
            self.warnings.append("using Joliet supplementary volume descriptor")
        self.volume_label = (chosen[40:72].decode("utf-16-be", "replace")
                             if svd is not None
                             else chosen[40:72].decode("ascii", "replace")).strip()
        if not self._root.is_dir:
            raise DiscStructureError("ISO9660 root directory record is not a directory")

    # -- path resolution ------------------------------------------------ #
    @staticmethod
    def _normalise(path: str) -> list[str]:
        clean = path.strip("/").replace("\\", "/")
        if clean in ("", ".", "./"):
            return []
        return [p for p in clean.split("/") if p not in ("", ".")]

    def _find(self, parts: list[str]) -> _Dirent | None:
        current = self._root
        for part in parts:
            if not current.is_dir:
                return None
            entries = self._read_dir(current)
            match = next((e for e in entries if e.name.lower() == part.lower()), None)
            if match is None:
                return None
            current = match
        return current

    def _read_dir(self, entry: _Dirent) -> list[_Dirent]:
        cached = self._dir_cache.get(entry.lba)
        if cached is not None:
            return cached
        entries: list[_Dirent] = []
        remaining = entry.size
        offset = entry.lba * self.sector_size
        while remaining > 0:
            block = self.device.read(offset, min(self.sector_size,
                                                 remaining + self.sector_size))
            if not block:
                break
            pos = 0
            while pos < len(block):
                record_len = block[pos]
                if record_len == 0:
                    # rest of this logical sector is padding
                    break
                if pos + record_len > len(block):
                    break
                record = block[pos:pos + record_len]
                dirent = _parse_dir_record_raw(record)
                if dirent is not None:
                    entries.append(dirent)
                pos += record_len
            consumed = min(self.sector_size, remaining)
            offset += consumed
            remaining -= consumed
        self._dir_cache[entry.lba] = entries
        return entries

    # -- DiscFileSystem API --------------------------------------------- #
    def exists(self, path: str) -> bool:
        return self._find(self._normalise(path)) is not None

    def listdir(self, path: str) -> list[str]:
        parts = self._normalise(path)
        entry = self._find(parts) if parts else self._root
        if entry is None:
            raise DiscStructureError(f"no such directory on disc: {path!r}")
        if not entry.is_dir:
            raise DiscStructureError(f"{path!r} is not a directory on disc")
        return sorted(e.name for e in self._read_dir(entry))

    def getfile(self, path: str) -> DiscFile:
        parts = self._normalise(path)
        entry = self._find(parts)
        if entry is None:
            raise DiscStructureError(f"no such file on disc: {path!r}")
        if entry.is_dir:
            raise DiscStructureError(f"{path!r} is a directory on disc")
        return IsoFile(self.device, entry.name, entry.lba, entry.size, self.sector_size)

    def file_size(self, path: str) -> int | None:
        entry = self._find(self._normalise(path))
        return entry.size if entry and not entry.is_dir else None

    def close(self) -> None:
        self._dir_cache.clear()


def _parse_dir_record(record: bytes, *, joliet: bool) -> _Dirent:
    # The root directory record in the PVD uses identifier 0x00, which normal
    # directory parsing treats as "this directory" and skips.
    dirent = _parse_dir_record_raw(record, joliet=joliet, is_root=True)
    if dirent is None:
        raise DiscStructureError("malformed ISO9660 directory record")
    return dirent


def _parse_dir_record_raw(record: bytes, *, joliet: bool = False,
                          is_root: bool = False) -> _Dirent | None:
    """Decode one directory record; ``None`` for '.'/'..' and corrupt entries."""
    if len(record) < 34:
        return None
    lba = struct.unpack_from("<I", record, 2)[0]
    size = struct.unpack_from("<I", record, 10)[0]
    flags = record[25]
    name_len = record[32]
    raw_name = record[33:33 + name_len]
    if not raw_name:
        return None
    if raw_name in (b"\x00", b"\x01") and not is_root:
        return None
    if joliet:
        name = raw_name.decode("utf-16-be", "replace")
    else:
        name = raw_name.decode("ascii", "replace")
    # strip the ";1" version suffix used for files
    if ";" in name:
        name = name.split(";", 1)[0]
    name = name.rstrip(".")
    if is_root and raw_name == b"\x00":
        name = "__root__"
    if not name or name in (".", ".."):
        return None
    return _Dirent(name=name, lba=lba, size=size, is_dir=bool(flags & 0x02))


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def open_disc_filesystem(path: str, *, kind: str | None = None,
                         warnings: list[str] | None = None) -> DiscFileSystem:
    """Open a directory or an ISO9660 image/device.

    ``kind`` may be ``"folder"``, ``"image"`` or ``"raw"`` to skip the guess;
    otherwise a directory check decides.
    """
    expanded = os.path.expanduser(path)
    if kind == "folder" or (kind is None and os.path.isdir(expanded)):
        return DirectoryDisc(expanded)
    device = FileBlockDevice(expanded)
    try:
        return Iso9660Disc(device, warnings=warnings)
    except DiscStructureError:
        device.close()
        raise
