"""Minimal read-only ISO9660 reader for ``.iso`` sources.

Only what a disc tree needs: directory lookup, file ranges and sizes.  Joliet
is understood for names; Rock Ridge is ignored.  Images that carry UDF instead
(typical Blu-ray images) raise :class:`UnsupportedImage`, which the engine
turns into ``File system not recognized, disc can't be opened``.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path


SECTOR = 2048


class IsoError(Exception):
    pass


class UnsupportedImage(IsoError):
    pass


class ISOImage:
    def __init__(self, path: str | Path | None = None, source=None) -> None:
        """Open an image.

        ``path`` is the normal case.  ``source`` is a seekable, readable
        file-like object used when the sectors come from somewhere other than
        a regular file, such as a libdvdcss device.
        """
        if source is None:
            if path is None:
                raise ValueError("path or source is required")
            self.path = str(path)
            source = open(self.path, "rb")
        else:
            self.path = str(path) if path is not None else str(getattr(source, "name", ""))
        self._file = source
        self._dir_cache: dict[tuple[int, int], dict[str, tuple[int, int, bool]]] = {}
        self.joliet = False
        try:
            try:
                self._pvd = self._sector(16)
            except IsoError as exc:
                raise UnsupportedImage(str(exc)) from exc
            if self._pvd[1:6] != b"CD001":
                raise UnsupportedImage("not an ISO9660 image")
            self._root = self._parse_record(self._pvd[156:190])
            self._load_joliet()
        except Exception:
            self._file.close()
            raise

    # -- raw access -----------------------------------------------------
    def _set_mpeg(self, mpeg: bool) -> None:
        setter = getattr(self._file, "set_mpeg", None)
        if setter is not None:
            setter(mpeg)

    def _sector(self, number: int) -> bytes:
        self._set_mpeg(False)
        self._file.seek(number * SECTOR)
        data = self._file.read(SECTOR)
        if len(data) < SECTOR:
            raise IsoError(f"short read at sector {number}")
        return data

    def _read_at(self, lba: int, size: int) -> bytes:
        self._set_mpeg(False)
        self._file.seek(lba * SECTOR)
        return self._file.read(size)

    @staticmethod
    def _parse_record(record: bytes) -> tuple[int, int, bool]:
        if len(record) < 34:
            raise IsoError("truncated directory record")
        length = record[0]
        if length < 34:
            raise IsoError("bad directory record")
        extent = struct.unpack_from("<I", record, 2)[0]
        size = struct.unpack_from("<I", record, 10)[0]
        is_dir = bool(record[25] & 0x02)
        return extent, size, is_dir

    def _load_joliet(self) -> None:
        try:
            svd = self._sector(17)
        except IsoError:
            return
        if len(svd) < 190 or svd[0] != 2 or svd[1:6] != b"CD001":
            return
        if svd[88:91] in (b"%/E", b"%/C", b"%/@"):
            self.joliet = True
            self._root = self._parse_record(svd[156:190])

    # -- directories ----------------------------------------------------
    def _entries(self, lba: int, size: int) -> dict[str, tuple[int, int, bool]]:
        key = (lba, size)
        cached = self._dir_cache.get(key)
        if cached is not None:
            return cached
        blob = self._read_at(lba, size)
        entries: dict[str, tuple[int, int, bool]] = {}
        pos = 0
        while pos < len(blob):
            length = blob[pos]
            if length == 0:
                pos = ((pos // SECTOR) + 1) * SECTOR
                continue
            record = blob[pos:pos + length]
            if len(record) < 34:
                break
            extent, data_size, is_dir = self._parse_record(record)
            name_len = record[32]
            raw_name = record[33:33 + name_len]
            pos += length
            if raw_name in (b"\x00", b"\x01"):
                continue
            try:
                name = raw_name.decode("utf-16-be" if self.joliet else "ascii")
            except UnicodeDecodeError:
                name = raw_name.decode("latin-1")
            name = name.split(";", 1)[0].rstrip(".")
            if not name:
                continue
            entries[name.upper()] = (extent, data_size, is_dir)
        self._dir_cache[key] = entries
        return entries

    def _resolve(self, path: str) -> tuple[int, int, bool]:
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        lba, size, is_dir = self._root
        for part in parts:
            entries = self._entries(lba, size)
            hit = entries.get(part.upper())
            if hit is None:
                raise FileNotFoundError(path)
            lba, size, is_dir = hit
        # Trust the directory-record flag.  Probing a file's extent as if it
        # were a directory reads megabytes of VOB data for every size() call.
        return lba, size, is_dir

    # -- public API ------------------------------------------------------
    def exists(self, path: str) -> bool:
        try:
            self._resolve(path)
            return True
        except (FileNotFoundError, IsoError):
            return False

    def isdir(self, path: str) -> bool:
        try:
            lba, size, _ = self._resolve(path)
            if not path.strip("/\\"):
                return True
            parent, _, leaf = path.replace("\\", "/").rpartition("/")
            entries = self._entries(*self._resolve(parent or "")[:2])
            return entries.get(leaf.upper(), (0, 0, False))[2]
        except (FileNotFoundError, IsoError):
            return False

    def size(self, path: str) -> int:
        return self._resolve(path)[1]

    def listdir(self, path: str) -> list[str]:
        lba, size, _ = self._resolve(path)
        return sorted(self._entries(lba, size))

    def read(self, path: str, offset: int = 0, size: int | None = None,
             chunk: int = 1 << 20, emit=None, mpeg: bool = False) -> bytes:
        lba, total, _ = self._resolve(path)
        start = min(max(0, offset), total)
        length = total - start if size is None else min(size, total - start)
        self._set_mpeg(mpeg)
        self._file.seek(lba * SECTOR + start)
        out = bytearray()
        while len(out) < length:
            block = self._file.read(min(chunk, length - len(out)))
            if not block:
                break
            out.extend(block)
            if emit is not None:
                emit(len(out))
        return bytes(out)

    def extract(self, path: str, destination: Path, mpeg: bool = False) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            remaining = self.size(path)
            lba, total, _ = self._resolve(path)
            self._set_mpeg(mpeg)
            self._file.seek(lba * SECTOR)
            while remaining > 0:
                block = self._file.read(min(1 << 20, remaining))
                if not block:
                    break
                out.write(block)
                remaining -= len(block)
        return destination

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "ISOImage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def is_iso(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.seek(16 * SECTOR)
            head = handle.read(6)
        return head[1:6] == b"CD001"
    except OSError:
        return False


def sniff(path: str | Path) -> str:
    """Return ``iso9660``, ``udf`` or ``unknown``."""
    try:
        with open(path, "rb") as handle:
            handle.seek(16 * SECTOR)
            head = handle.read(6)
            if head[1:6] == b"CD001":
                return "iso9660"
            # Blu-ray images are UDF-only: the volume recognition sequence
            # starts at sector 16 with BEA01/NSR02/NSR03, and an anchor
            # volume descriptor sits at sector 256.
            for index in range(8):
                handle.seek((16 + index) * SECTOR)
                block = handle.read(7)
                if not block:
                    break
                if block[1:6] in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
                    return "udf"
            handle.seek(256 * SECTOR)
            if handle.read(2) == b"\x02\x00":
                return "udf"
    except OSError:
        pass
    return "unknown"
