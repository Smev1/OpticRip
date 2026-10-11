"""Bounds-checked big-endian reads for disc structures.

Optical disc structures are big-endian and frequently truncated or corrupt.
Every accessor here returns a default instead of raising, so a damaged disc
degrades into a warning rather than a crash; the caller decides what is fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class Buffer:
    """A read-only, bounds-checked view over a byte string."""

    __slots__ = ("data", "label", "problems")

    def __init__(self, data: bytes, label: str = "buffer") -> None:
        self.data = data
        self.label = label
        self.problems: list[str] = []

    def __len__(self) -> int:
        return len(self.data)

    @property
    def ok(self) -> bool:
        return not self.problems

    def _need(self, offset: int, size: int) -> bool:
        if offset < 0 or offset + size > len(self.data):
            note = (f"{self.label}: read of {size} byte(s) at 0x{max(0, offset):x} "
                    f"is outside the {len(self.data)}-byte structure")
            if note not in self.problems:
                self.problems.append(note)
            return False
        return True

    def u8(self, offset: int, default: int = 0) -> int:
        return self.data[offset] if self._need(offset, 1) else default

    def u16(self, offset: int, default: int = 0) -> int:
        if not self._need(offset, 2):
            return default
        return int.from_bytes(self.data[offset:offset + 2], "big")

    def u32(self, offset: int, default: int = 0) -> int:
        if not self._need(offset, 4):
            return default
        return int.from_bytes(self.data[offset:offset + 4], "big")

    def u64(self, offset: int, default: int = 0) -> int:
        if not self._need(offset, 8):
            return default
        return int.from_bytes(self.data[offset:offset + 8], "big")

    def bytes(self, offset: int, size: int) -> bytes:
        if not self._need(offset, size):
            return b""
        return self.data[offset:offset + size]

    def ascii(self, offset: int, size: int) -> str:
        return self.bytes(offset, size).decode("ascii", "replace").strip()

    def slice(self, offset: int, size: int | None = None,
              label: str | None = None) -> "Buffer":
        """A sub-buffer; problems are copied up on demand via :meth:`problems`."""
        if size is None:
            size = len(self.data) - offset
        return Buffer(self.bytes(offset, size),
                      label or f"{self.label}[0x{offset:x}]")

    # -- composite reads ------------------------------------------------- #
    def bit_u32(self, offset: int, *, first_bit: int, bit_count: int,
                default: int = 0) -> int:
        """Extract ``bit_count`` bits starting at ``first_bit`` (0 = MSB)."""
        value = self.u32(offset, default)
        shift = 32 - first_bit - bit_count
        if shift < 0:
            return default
        return (value >> shift) & ((1 << bit_count) - 1)

    def bcd(self, offset: int, size: int = 1) -> int:
        """Decode packed BCD; returns ``default`` (0) for invalid nibbles."""
        total = 0
        for i in range(size):
            byte = self.u8(offset + i)
            high, low = (byte >> 4) & 0x0F, byte & 0x0F
            if high > 9 or low > 9:
                return 0
            total = total * 100 + high * 10 + low
        return total


#: DVD playback-time frame-rate codes (libdvdread ifo_read.c get_playback_time).
_FRAME_RATES = {0b11: 29.97, 0b01: 25.0}


@dataclass
class PlaybackTime:
    """A DVD 4-byte playback time."""

    seconds: float = 0.0
    frame_rate: float = 0.0
    raw: bytes = b""
    valid: bool = True

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.seconds:.3f}s @{self.frame_rate or 0:g}fps"


def read_playback_time(buf: Buffer, offset: int) -> PlaybackTime:
    """Decode the DVD BCD playback-time field.

    Layout: ``HH`` ``MM`` ``SS`` then a byte whose top two bits select the
    frame rate (``11`` = 29.97 fps NTSC, ``01`` = 25 fps PAL, others invalid)
    and whose low six bits are the BCD frame number.  A field of all-ones
    means "unknown".
    """
    raw = buf.bytes(offset, 4)
    if len(raw) < 4 or raw == b"\xff\xff\xff\xff":
        return PlaybackTime(valid=False, raw=raw)
    hours = buf.bcd(offset)
    minutes = buf.bcd(offset + 1)
    seconds = buf.bcd(offset + 2)
    frame_byte = buf.u8(offset + 3)
    rate_code = (frame_byte >> 6) & 0x03
    frame_rate = _FRAME_RATES.get(rate_code, 0.0)
    frame_number = ((frame_byte >> 4) & 0x03) * 10 + (frame_byte & 0x0F)
    total = hours * 3600 + minutes * 60 + seconds
    if frame_rate:
        total += frame_number / frame_rate
    return PlaybackTime(seconds=float(total), frame_rate=frame_rate, raw=raw)


@dataclass
class TableHeader:
    """Common header used by most DVD tables (count + last_byte)."""

    count: int = 0
    last_byte: int = 0
    flags: int = 0
    reserved: int = 0
    raw: bytes = b""
    problems: list[str] = field(default_factory=list)
