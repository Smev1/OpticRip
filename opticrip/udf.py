"""Read-only UDF support for Blu-ray ``.iso`` images.

Blu-ray images carry UDF 2.50 instead of the ISO9660 tree used by DVDs.
Only the read path needed by the disc browser is implemented: volume
descriptors, partition maps (including the UDF 2.50 metadata partition used
by Blu-ray), the file set, directories and file allocation descriptors.

The parser is deliberately tolerant: descriptor checksums are not enforced,
and unknown optional descriptors are skipped.  A damaged or unsupported
volume raises :class:`~opticrip.iso9660.UnsupportedImage` with a reason so the
engine can show its normal ``File system not recognized`` message.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .iso9660 import IsoError, UnsupportedImage

SECTOR = 2048

TAG_PRIMARY = 1
TAG_ANCHOR = 2
TAG_PARTITION = 5
TAG_LOGICAL = 6
TAG_TERMINATING = 8
TAG_FILE_SET = 256
TAG_FILE_IDENTIFIER = 257
TAG_ALLOCATION_EXTENT = 258
TAG_FILE_ENTRY = 261
TAG_EXTENDED_FILE_ENTRY = 266

#: Allocation descriptor type from the ICB tag flags.
AD_SHORT = 0
AD_LONG = 1
AD_EXTENDED = 2
AD_EMBEDDED = 3

#: File types from the ICB tag.
FT_DIRECTORY = 4
FT_FILE = 5


def _u16(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<H", blob, offset)[0]


def _u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<I", blob, offset)[0]


def _u64(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", blob, offset)[0]


def _tag_id(sector: bytes) -> int:
    return _u16(sector, 0) if len(sector) >= 2 else 0


def _dstring(payload: bytes) -> str:
    """Decode an OSTA compressed-unicode string (UDF 2.1.3)."""
    if not payload:
        return ""
    kind = payload[0]
    body = payload[1:]
    if kind == 8:
        return body.split(b"\x00", 1)[0].decode("latin-1", "replace")
    if kind == 16:
        text = body[: len(body) - (len(body) % 2)].decode("utf-16-be", "replace")
        return text.split("\x00", 1)[0]
    if kind == 0:
        return ""
    return body.split(b"\x00", 1)[0].decode("latin-1", "replace")


def _dstring_field(field: bytes) -> str:
    """Decode a fixed-size d-string field whose last byte is the length."""
    if not field:
        return ""
    length = field[-1]
    if length > len(field) - 1:
        return ""
    return _dstring(field[:length])


@dataclass(frozen=True)
class _Icb:
    block: int
    partition: int
    length: int = SECTOR


@dataclass
class _Partition:
    number: int              # partition reference used by lb_addr
    base: int                # absolute sector
    mirror: int = 0          # absolute mirror sector, or 0
    metadata: bool = False


@dataclass
class _Extent:
    block: int
    length: int
    partition: int | None    # None -> the data partition
    recorded: bool = True


@dataclass
class _Entry:
    file_type: int
    info_length: int
    ad_type: int
    extents: list[_Extent] = field(default_factory=list)
    embedded: bytes = b""
    continuation: _Extent | None = None


class UDFImage:
    """A mounted UDF volume (same surface as :class:`ISOImage`)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._file = open(self.path, "rb")
        self._size = self._file.seek(0, 2) // SECTOR
        self._dir_cache: dict[tuple[int, int], dict[str, tuple[_Icb, bool]]] = {}
        self._entry_cache: dict[tuple[int, int], _Entry] = {}
        self.partitions: dict[int, _Partition] = {}
        self.data_partition = 0
        self.volume_id = ""
        try:
            self._mount()
        except Exception:
            self._file.close()
            raise

    # -- raw sectors -----------------------------------------------------
    def _sector(self, number: int) -> bytes:
        if number < 0 or number >= self._size:
            raise IsoError(f"UDF sector {number} past the end of the image")
        self._file.seek(number * SECTOR)
        data = self._file.read(SECTOR)
        if len(data) < SECTOR:
            raise IsoError(f"short read at UDF sector {number}")
        return data

    def _read_at(self, sector: int, offset: int, size: int) -> bytes:
        if size <= 0:
            return b""
        if sector < 0 or sector >= self._size:
            raise IsoError(f"UDF sector {sector} past the end of the image")
        self._file.seek(sector * SECTOR + offset)
        data = self._file.read(size)
        if len(data) < size:
            raise IsoError(f"short read at UDF sector {sector}")
        return data

    def _absolute(self, block: int, partition: int) -> int:
        part = self.partitions.get(partition)
        if part is None:
            raise IsoError(f"UDF partition {partition} is not mapped")
        return part.base + block

    # -- mount -----------------------------------------------------------
    def _mount(self) -> None:
        self._probe_vrs()
        avdp = self._find_anchor()
        main_len = _u32(avdp, 16)
        main_loc = _u32(avdp, 20)
        if main_len == 0:
            main_len = _u32(avdp, 24)
            main_loc = _u32(avdp, 28)
        descriptors = self._volume_descriptors(main_loc, main_len)
        if not descriptors:
            descriptors = self._volume_descriptors(_u32(avdp, 24), _u32(avdp, 28))
        pvd = next((d for d in descriptors if _tag_id(d) == TAG_PRIMARY), None)
        lvd = next((d for d in descriptors if _tag_id(d) == TAG_LOGICAL), None)
        if lvd is None:
            raise UnsupportedImage("UDF image has no Logical Volume Descriptor")
        if pvd is not None:
            self.volume_id = _dstring_field(pvd[24:56])
        self._parse_partition_maps(lvd, descriptors)
        if not self.partitions:
            raise UnsupportedImage("UDF image has no usable partition")
        self._read_file_set(lvd)

    def _probe_vrs(self) -> None:
        """The VRS is optional in practice, so it is only a soft probe."""
        for sector in range(16, min(32, self._size)):
            ident = self._sector(sector)[1:6]
            if ident == b"TEA01":
                return

    def _find_anchor(self) -> bytes:
        candidates = [256, self._size - 1, self._size - 257]
        for sector in candidates:
            if sector < 0 or sector >= self._size:
                continue
            data = self._sector(sector)
            if _tag_id(data) == TAG_ANCHOR:
                return data
        raise UnsupportedImage("UDF anchor volume descriptor is missing")

    def _volume_descriptors(self, location: int, length: int) -> list[bytes]:
        if location <= 0 or length <= 0:
            return []
        found: list[bytes] = []
        count = min(length // SECTOR + 1, 256)
        for index in range(count):
            sector = location + index
            if sector >= self._size:
                break
            data = self._sector(sector)
            tag = _tag_id(data)
            if tag == TAG_TERMINATING:
                break
            if tag in (TAG_PRIMARY, TAG_PARTITION, TAG_LOGICAL):
                found.append(data)
        return found

    def _parse_partition_maps(self, lvd: bytes,
                              descriptors: list[bytes]) -> None:
        if len(lvd) < 440:
            raise UnsupportedImage("UDF Logical Volume Descriptor is truncated")
        block_size = _u32(lvd, 212)
        if block_size != SECTOR:
            raise UnsupportedImage(
                f"UDF logical block size {block_size} is not supported"
            )
        table_length = min(_u32(lvd, 264), len(lvd) - 440)
        map_count = _u32(lvd, 268)
        table = lvd[440:440 + table_length]

        # Partition Descriptors give the physical partition starts.  They
        # normally live in the Volume Descriptor Sequence well below sector
        # 256, so use the descriptors already read instead of probing.
        physical: dict[int, tuple[int, int]] = {}
        for data in descriptors:
            if _tag_id(data) != TAG_PARTITION or len(data) < 196:
                continue
            number = _u16(data, 22)
            physical.setdefault(number, (_u32(data, 188), _u32(data, 192)))
        for sector in range(256, min(256 + 64, self._size)):
            data = self._sector(sector)
            if _tag_id(data) != TAG_PARTITION or len(data) < 196:
                continue
            number = _u16(data, 22)
            physical.setdefault(number, (_u32(data, 188), _u32(data, 192)))

        pending_metadata: list[tuple[int, int, int, int]] = []
        offset = 0
        for index in range(min(map_count, 64)):
            if offset + 2 > len(table):
                break
            kind = table[offset]
            length = table[offset + 1]
            if length < 2 or offset + length > len(table):
                break
            entry = table[offset:offset + length]
            if kind == 1:
                if len(entry) >= 6:
                    number = _u16(entry, 4)
                    start = physical.get(number)
                    if start is not None:
                        if not self.partitions:
                            self.data_partition = index
                        self.partitions[index] = _Partition(index, start[0])
            elif kind == 2:
                if len(entry) >= 48 and entry[4:].startswith(b"*UDF Metadata Partition"):
                    number = _u16(entry, 38)
                    meta = _u32(entry, 40)
                    mirror = _u32(entry, 44)
                    start = physical.get(number)
                    if start is not None:
                        pending_metadata.append((index, start[0], meta, mirror))
            offset += length

        for index, start, meta, mirror in pending_metadata:
            partition = self._metadata_partition(index, start, meta, mirror)
            if partition is not None:
                self.partitions[index] = partition

    def _metadata_partition(self, index: int, start: int,
                            meta: int, mirror: int) -> _Partition | None:
        for candidate, mirror_flag in ((meta, False), (mirror, True)):
            if not candidate:
                continue
            try:
                entry = self._parse_entry(
                    self._sector(start + candidate), start, index,
                )
            except (IsoError, IndexError, struct.error):
                continue
            if not entry.extents:
                continue
            first = entry.extents[0]
            base = self._extent_absolute(first, start)
            if base is None:
                continue
            if entry.file_type not in (250, 251):
                # The metadata file normally carries type 250, but some
                # authoring tools leave it as a plain file.  The first
                # extent is still the metadata area.
                pass
            return _Partition(index, base, metadata=True)
        return None

    def _extent_absolute(self, extent: _Extent, default_base: int) -> int | None:
        if extent.partition is None:
            return default_base + extent.block
        part = self.partitions.get(extent.partition)
        if part is not None:
            return part.base + extent.block
        return None

    def _read_file_set(self, lvd: bytes) -> None:
        if len(lvd) < 264:
            raise UnsupportedImage("UDF Logical Volume Descriptor is truncated")
        length = _u32(lvd, 248)
        block = _u32(lvd, 252)
        partition = _u16(lvd, 256)
        part = self.partitions.get(partition)
        if part is None:
            raise UnsupportedImage("UDF File Set Descriptor uses an unknown partition")
        fsd = None
        for index in range(max(1, (length + SECTOR - 1) // SECTOR)):
            data = self._sector(part.base + block + index)
            if _tag_id(data) == TAG_FILE_SET:
                fsd = data
                break
        if fsd is None or len(fsd) < 416:
            raise UnsupportedImage("UDF File Set Descriptor is missing")
        self.root_icb = _Icb(_u32(fsd, 404), _u16(fsd, 408))

    # -- file entries ----------------------------------------------------
    def _parse_entry(self, blob: bytes, default_base: int,
                     default_partition: int) -> _Entry:
        tag = _tag_id(blob)
        if tag not in (TAG_FILE_ENTRY, TAG_EXTENDED_FILE_ENTRY):
            raise IsoError("not a UDF File Entry")
        extended = tag == TAG_EXTENDED_FILE_ENTRY
        prefix = 216 if extended else 176
        if len(blob) < prefix:
            raise IsoError("UDF File Entry is truncated")
        flags = _u16(blob, 34)
        file_type = blob[27]
        info_length = _u64(blob, 56)
        l_ea = _u32(blob, 208 if extended else 168)
        l_ad = _u32(blob, 212 if extended else 172)
        ad_type = flags & 0x07
        ad_start = prefix + l_ea
        ad_end = ad_start + l_ad
        if ad_end > len(blob):
            raise IsoError("UDF allocation descriptors overrun the File Entry")
        area = blob[ad_start:ad_end]
        entry = _Entry(file_type=file_type, info_length=info_length,
                       ad_type=ad_type)
        if ad_type == AD_EMBEDDED:
            entry.embedded = area
            return entry
        entry.extents, entry.continuation = self._parse_extents(
            ad_type, area, default_partition,
        )
        seen = 0
        continuation = entry.continuation
        while continuation is not None and seen < 32:
            seen += 1
            base = self._extent_absolute(continuation, default_base)
            if base is None:
                raise IsoError("UDF descriptor continuation is in an unknown partition")
            block = self._sector(base)
            if _tag_id(block) != TAG_ALLOCATION_EXTENT or len(block) < 24:
                raise IsoError("UDF allocation extent descriptor is missing")
            length = _u32(block, 20)
            if length > 1 << 20 or 24 + length > len(block):
                raise IsoError("UDF allocation extent descriptor is malformed")
            more, entry.continuation = self._parse_extents(
                ad_type, block[24:24 + length], default_partition,
            )
            entry.extents.extend(more)
            continuation = entry.continuation
        if continuation is not None:
            raise IsoError("UDF allocation descriptor chain is too long")
        return entry

    @staticmethod
    def _parse_extents(ad_type: int, area: bytes,
                       default_partition: int) -> tuple[list[_Extent], _Extent | None]:
        extents: list[_Extent] = []
        offset = 0
        size = 8 if ad_type in (AD_SHORT, AD_LONG) else 20
        while offset + size <= len(area):
            raw_length = _u32(area, offset)
            extent_type = raw_length >> 30
            length = raw_length & 0x3FFFFFFF
            if length == 0:
                break
            if ad_type == AD_SHORT:
                block = _u32(area, offset + 4)
                partition = None
            elif ad_type == AD_LONG:
                block = _u32(area, offset + 4)
                partition = _u16(area, offset + 8)
            else:
                block = _u32(area, offset + 12)
                partition = _u16(area, offset + 16)
                length = _u32(area, offset + 8)
                recorded_length = _u32(area, offset + 4) & 0x3FFFFFFF
                if recorded_length != length:
                    raise UnsupportedImage("compressed UDF extents are not supported")
            if extent_type == 3:
                return extents, _Extent(block, length, partition)
            if extent_type != 0:
                # Allocated but unrecorded space reads as zeroes.
                extents.append(_Extent(block, length, partition, recorded=False))
            else:
                extents.append(_Extent(block, length, partition))
            offset += size
        return extents, None

    def _entry(self, icb: _Icb) -> _Entry:
        key = (icb.block, icb.partition)
        cached = self._entry_cache.get(key)
        if cached is not None:
            return cached
        part = self.partitions.get(icb.partition)
        if part is None:
            raise IsoError(f"UDF partition {icb.partition} is not mapped")
        count = max(1, (icb.length + SECTOR - 1) // SECTOR)
        bases = [part.base]
        if part.mirror:
            bases.append(part.mirror)
        last_error: Exception | None = None
        for base in bases:
            try:
                blob = b"".join(self._sector(base + icb.block + i)
                                for i in range(count))
                entry = self._parse_entry(blob, part.base, part.number)
                self._entry_cache[key] = entry
                return entry
            except (IsoError, struct.error) as exc:
                last_error = exc
        raise IsoError(f"cannot read UDF File Entry: {last_error}")

    def _entry_bytes(self, icb: _Icb) -> bytes:
        entry = self._entry(icb)
        if entry.ad_type == AD_EMBEDDED:
            return entry.embedded[:entry.info_length]
        out = bytearray()
        for extent in entry.extents:
            base = self._extent_absolute(extent, self.partitions[self.data_partition].base)
            if base is None:
                raise IsoError("UDF extent belongs to an unknown partition")
            remaining = extent.length
            sector = base
            while remaining > 0:
                take = min(remaining, SECTOR)
                out.extend(self._sector(sector)[:take] if extent.recorded
                           else b"\x00" * take)
                sector += 1
                remaining -= take
                if len(out) >= entry.info_length:
                    break
            if len(out) >= entry.info_length:
                break
        return bytes(out[:entry.info_length])

    def _read_range(self, icb: _Icb, offset: int, size: int | None) -> bytes:
        entry = self._entry(icb)
        total = entry.info_length
        start = min(max(0, offset), total)
        length = total - start if size is None else min(size, total - start)
        if length <= 0:
            return b""
        if entry.ad_type == AD_EMBEDDED:
            return entry.embedded[start:start + length]
        out = bytearray()
        cursor = 0
        for extent in entry.extents:
            if cursor + extent.length <= start:
                cursor += extent.length
                continue
            within = max(0, start - cursor)
            take = min(extent.length - within, length - len(out))
            if take > 0:
                base = self._extent_absolute(
                    extent, self.partitions[self.data_partition].base,
                )
                if base is None:
                    raise IsoError("UDF extent belongs to an unknown partition")
                if extent.recorded:
                    sector = base + within // SECTOR
                    inner = within % SECTOR
                    remaining = take
                    while remaining > 0:
                        chunk = self._read_at(sector, inner,
                                              min(remaining, SECTOR - inner))
                        out.extend(chunk)
                        remaining -= len(chunk)
                        inner = 0
                        sector += 1
                else:
                    out.extend(b"\x00" * take)
            cursor += extent.length
            if len(out) >= length:
                break
        return bytes(out[:length])

    def _directory(self, icb: _Icb) -> dict[str, tuple[_Icb, bool]]:
        key = (icb.block, icb.partition)
        cached = self._dir_cache.get(key)
        if cached is not None:
            return cached
        data = self._entry_bytes(icb)
        entries: dict[str, tuple[_Icb, bool]] = {}
        offset = 0
        while offset + 38 <= len(data):
            if _tag_id(data[offset:]) != TAG_FILE_IDENTIFIER:
                break
            version = _u16(data, offset + 16)
            characteristics = data[offset + 18]
            name_length = data[offset + 19]
            child = _Icb(_u32(data, offset + 24),
                         _u16(data, offset + 28))
            impl_length = _u16(data, offset + 36)
            name_start = offset + 38 + impl_length
            name_end = name_start + name_length
            if name_end > len(data):
                break
            length = (name_end - offset + 3) & ~3
            name = _dstring(data[name_start:name_end])
            offset += max(length, 38)
            if not name or characteristics & 0x08 or characteristics & 0x04:
                continue
            entries[name.upper()] = (child, bool(characteristics & 0x02))
        self._dir_cache[key] = entries
        return entries

    def _resolve(self, path: str) -> tuple[_Icb, bool]:
        current = self.root_icb
        is_dir = True
        parts = [part for part in path.replace("\\", "/").split("/")
                 if part and part != "."]
        for part in parts:
            if not is_dir:
                raise FileNotFoundError(path)
            hit = self._directory(current).get(part.upper())
            if hit is None:
                raise FileNotFoundError(path)
            current, is_dir = hit
        return current, is_dir

    # -- public API ------------------------------------------------------
    @property
    def label(self) -> str:
        return self.volume_id

    def exists(self, path: str) -> bool:
        try:
            self._resolve(path)
            return True
        except (FileNotFoundError, IsoError):
            return False

    def isdir(self, path: str) -> bool:
        try:
            return self._resolve(path)[1]
        except (FileNotFoundError, IsoError):
            return False

    def size(self, path: str) -> int:
        icb, is_dir = self._resolve(path)
        if is_dir and not path.strip("/\\"):
            return 0
        return self._entry(icb).info_length

    def listdir(self, path: str) -> list[str]:
        icb, is_dir = self._resolve(path)
        if not is_dir:
            raise IsoError(f"{path} is not a directory")
        return sorted(self._directory(icb))

    def read(self, path: str, offset: int = 0,
             size: int | None = None) -> bytes:
        icb, is_dir = self._resolve(path)
        if is_dir:
            raise IsoError(f"{path} is a directory")
        return self._read_range(icb, offset, size)

    def extract(self, path: str, destination: Path,
                mpeg: bool = False) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        icb, is_dir = self._resolve(path)
        if is_dir:
            raise IsoError(f"{path} is a directory")
        entry = self._entry(icb)
        remaining = entry.info_length
        offset = 0
        with destination.open("wb") as out:
            while remaining > 0:
                chunk = self._read_range(icb, offset, min(1 << 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
        return destination

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "UDFImage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def is_udf(path: Path) -> bool:
    """Cheap probe used by :func:`opticrip.iso9660.sniff`."""
    try:
        with open(path, "rb") as handle:
            handle.seek(16 * SECTOR)
            for _ in range(8):
                head = handle.read(7)
                if not head:
                    break
                if head[1:6] in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
                    return True
                handle.seek((7 - 1) * SECTOR, 1)
            handle.seek(256 * SECTOR)
            head = handle.read(2)
        return head == b"\x02\x00"
    except OSError:
        return False
