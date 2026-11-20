"""DVD closed captions (CEA-608) to SubRip.

DVD-Video carries caption data as MPEG-2 user-data blocks in the video
stream.  Each block is introduced by ``00 00 01 B2`` and starts with
``CC 01 F8``; after that comes a pattern/truncate byte and one or two
pairs of three-byte caption segments per frame (one field-1 and one
field-2 pair).  This is the format the ``mmccextr`` helper of the
reference writes to SubRip.

Only the first caption channel (CC1, carried in field 1) is decoded.
That is what a single ``S_CC608/DVD``/``CC→Text`` track contains on
DVD-Video.  The decoder understands pop-on, paint-on and roll-up
captions, including PAC positioning, backspace/erase, and the special
and extended character tables.  Timing comes from the video PES
timestamps (with GOP timecode as a fallback), so the SubRip timeline is
independent of the drive's read speed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .discfs import DiscFS
from .model import Stream

CC_MAGIC = b"CC\x01\xf8"
USER_DATA = b"\x00\x00\x01\xb2"

_FRAME_RATES = {
    1: 24000 / 1001,
    2: 24.0,
    3: 25.0,
    4: 30000 / 1001,
    5: 30.0,
    6: 50.0,
    7: 60000 / 1001,
    8: 60.0,
}

#: First PAC byte -> caption row.  Index is ``((c1 << 1) & 14) | (c2 >> 5)``.
_ROWDATA = (11, -1, 1, 2, 3, 4, 12, 13, 14, 15, 5, 6, 7, 8, 9, 10)

_SPECIAL = {
    0x20: "\u00ae", 0x21: "\u00b0", 0x22: "\u00bd", 0x23: "\u00bf",
    0x24: "\u2122", 0x25: "\u00a2", 0x26: "\u00a3", 0x27: "\u266a",
    0x28: "\u00e0", 0x29: "\u00a0", 0x2A: "\u00e8", 0x2B: "\u00e2",
    0x2C: "\u00ea", 0x2D: "\u00ee", 0x2E: "\u00f4", 0x2F: "\u00fb",
    0x30: "\u00c1", 0x31: "\u00c9", 0x32: "\u00d3", 0x33: "\u00da",
    0x34: "\u00dc", 0x35: "\u00fc", 0x36: "\u2018", 0x37: "\u00a1",
    0x38: "*", 0x39: "'", 0x3A: "\u2014", 0x3B: "\u00a9",
    0x3C: "\u2120", 0x3D: "\u2022", 0x3E: "\u201c", 0x3F: "\u201d",
}

_EXTENDED_SPANISH = {
    0x20: "\u00c0", 0x21: "\u00c2", 0x22: "\u00c7", 0x23: "\u00c8",
    0x24: "\u00ca", 0x25: "\u00cb", 0x26: "\u00eb", 0x27: "\u00ce",
    0x28: "\u00cf", 0x29: "\u00ef", 0x2A: "\u00d4", 0x2B: "\u00d9",
    0x2C: "\u00f9", 0x2D: "\u00db", 0x2E: "\u00ab", 0x2F: "\u00bb",
    0x30: "\u00c3", 0x31: "\u00e3", 0x32: "\u00cd", 0x33: "\u00cc",
    0x34: "\u00ec", 0x35: "\u00d2", 0x36: "\u00f2", 0x37: "\u00d5",
    0x38: "\u00f5", 0x39: "{", 0x3A: "}", 0x3B: "\\", 0x3C: "^",
    0x3D: "_", 0x3E: "|", 0x3F: "~",
}

_EXTENDED_NORTH_AMERICAN = {
    0x30: "\u00c6", 0x31: "\u00e6", 0x32: "\u00d8", 0x33: "\u00f8",
    0x34: "\u0152", 0x35: "\u0153", 0x36: "\u00df", 0x37: "\u00a5",
    0x38: "\u00a4", 0x39: "\u2502", 0x3A: "\u00c5", 0x3B: "\u00e5",
    0x3C: "\u00d8", 0x3D: "\u00f8", 0x3E: "\u250c", 0x3F: "\u2510",
}

_BLANK_ROW = " " * 32


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _strip_parity(value: int) -> int:
    return value & 0x7F


def _caption_blocks(payload: bytes) -> Iterator[tuple[int, int, int]]:
    """Yield ``(field, byte1, byte2)`` for one DVD CC user-data payload.

    ``field`` is 1 or 2.  Malformed or truncated payloads yield what could
    be read before the first inconsistency.  Most discs use ``DD 01 F8``
    as the header, but some authoring tools omit the second byte; both
    forms occur in the wild and both carry the same block layout.
    """
    if len(payload) < 5 or payload[0:2] != b"CC":
        return
    if payload[2:4] == b"\x01\xf8":
        offset = 4
    elif payload[2] == 0x01:
        offset = 3
    else:
        return
    control = payload[offset]
    pattern = (control >> 7) & 1
    count = (control >> 1) & 0x1F
    truncate = control & 1
    offset += 1
    # The field order is signalled by pattern_flag: field 1 first when set,
    # second when clear.  Marker bytes are 0xFF (field 1) and 0xFE (field 2).
    field1_slot = 0 if pattern else 1
    total = count + truncate
    for index in range(total):
        for slot in (0, 1):
            if offset + 3 > len(payload):
                return
            block = payload[offset:offset + 3]
            offset += 3
            if truncate and index == total - 1 and slot == 1:
                return
            marker = block[0]
            if marker & 0xFE != 0xFE:
                return
            field = 1 if (marker == 0xFF and slot == field1_slot) else 2
            yield field, block[1], block[2]
    # Some DVDs append one more field pair after the declared count.
    while offset + 6 <= len(payload) and payload[offset] & 0xFE == 0xFE:
        extra: list[tuple[int, int, int]] = []
        for slot in (0, 1):
            block = payload[offset:offset + 3]
            offset += 3
            marker = block[0]
            if marker & 0xFE != 0xFE:
                return
            field = 1 if (marker == 0xFF and slot == field1_slot) else 2
            extra.append((field, block[1], block[2]))
        # Trailing 0xFF filler passes the marker test but is not caption data
        # (it would otherwise decode as a pair of music notes).
        if all(_strip_parity(first) == 0x7F and _strip_parity(second) == 0x7F
               for _field, first, second in extra):
            return
        for item in extra:
            yield item


def has_captions(data: bytes) -> bool:
    """True when ``data`` contains a non-empty DVD caption block."""
    pos = 0
    needle = USER_DATA + CC_MAGIC
    while True:
        found = data.find(needle, pos)
        if found < 0:
            return False
        for _field, first, second in _caption_blocks(data[found + 4:]):
            if _strip_parity(first) or _strip_parity(second):
                return True
        pos = found + 4


def title_has_captions(fs: DiscFS, segments: Sequence,
                       budget: int = 4 << 20) -> bool:
    """Sample the start of a title for caption data.

    Reading the whole title during disc inspection would spin an optical
    drive for minutes; captions almost always start within the first few
    seconds, so a bounded sample is enough to decide whether the reference
    would list a ``S_CC608/DVD`` track.  A title whose first caption is
    later than the sample keeps working, it simply has no CC track listed
    until a save reads the whole stream.
    """
    remaining = max(0, int(budget))
    for segment in segments:
        if remaining <= 0:
            break
        try:
            size = int(segment.byte_size) or int(fs.size(segment.path))
        except OSError:
            continue
        offset = int(segment.byte_offset)
        while size > 0 and remaining > 0:
            take = min(size, remaining)
            try:
                chunk = fs.read(segment.path, offset, take)
            except OSError:
                return False
            if not chunk:
                break
            if has_captions(chunk):
                return True
            offset += len(chunk)
            size -= len(chunk)
            remaining -= len(chunk)
    return False


def caption_stream(index: int) -> Stream:
    """The stream the reference lists after the disc's subtitle tracks."""
    return Stream(
        kind="subtitle",
        index=index,
        codec="CC",
        codec_long="Closed Captions",
        codec_id="S_CC608/DVD",
        language="eng",
        language_name="English",
        # The reference reports the video's constant DVD peak rate on this
        # synthetic track (CLONE_SPEC §25.3).
        bitrate=9_800_000,
        output_codec="SRT",
        conversion="SRT",
    )


def _screen_text(rows: Sequence[str]) -> str:
    lines = [row.rstrip() for row in rows]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line for line in lines if line.strip())


class _CaptionDecoder:
    """Minimal CEA-608 decoder for the CC1 channel."""

    def __init__(self) -> None:
        self.mode = "popon"
        self.visible = [_BLANK_ROW] * 15
        self.hidden = [_BLANK_ROW] * 15
        self.row = 14
        self.col = 0
        self.roll_rows = 2
        self.base_row = 14
        self._have_cursor = False
        self._last_control: tuple[int, int] | None = None
        self._last_control_time = -10.0
        self.cues: list[Cue] = []
        self._cue_start: float | None = None
        self._cue_text = ""

    # -- cue bookkeeping ---------------------------------------------------
    def _update(self, time: float) -> None:
        text = _screen_text(self.visible)
        if text == self._cue_text:
            return
        if self._cue_text:
            if text.startswith(self._cue_text):
                # Paint-on and roll-up captions type themselves onto the
                # screen; extend the current cue instead of emitting a cue
                # for every character.
                self._cue_text = text
                return
            self._close(time)
        if text:
            self._cue_start = max(0.0, time)
            self._cue_text = text

    def _close(self, time: float) -> None:
        if self._cue_start is None or not self._cue_text:
            self._cue_text = ""
            self._cue_start = None
            return
        end = max(time, self._cue_start + 0.04)
        self.cues.append(Cue(self._cue_start, end, self._cue_text))
        self._cue_text = ""
        self._cue_start = None

    def _restart(self, time: float) -> None:
        """End the current cue and start a fresh one with the visible text.

        Roll-up captions scroll on a carriage return: the text that was on
        screen is now a different line, so it gets its own cue instead of
        being extended in place.
        """
        self._close(time)
        text = _screen_text(self.visible)
        if text:
            self._cue_start = max(0.0, time)
            self._cue_text = text

    def finish(self, duration: float) -> list[Cue]:
        if self._cue_text:
            self._close(max(duration, (self._cue_start or 0.0) + 0.04))
        if duration > 0:
            self.cues = [cue for cue in self.cues if cue.start < duration]
        return self.cues

    # -- buffer helpers ----------------------------------------------------
    def _buffer(self) -> list[str]:
        return self.hidden if self.mode == "popon" else self.visible

    def _set_cursor(self, row: int, col: int = 0) -> None:
        self.row = max(0, min(14, row))
        self.col = max(0, min(31, col))
        self._have_cursor = True

    def _write(self, char: str, time: float) -> None:
        if self.mode == "text":
            return
        buffer = self._buffer()
        row = max(0, min(14, self.row))
        col = max(0, min(31, self.col))
        line = buffer[row]
        buffer[row] = line[:col] + char + line[col + 1:]
        self.col = min(31, col + 1)
        if self.mode != "popon":
            self._update(time)

    def _space(self, time: float) -> None:
        self._write(" ", time)

    def _erase_row(self, time: float) -> None:
        if self.mode == "text":
            return
        buffer = self._buffer()
        row = max(0, min(14, self.row))
        buffer[row] = buffer[row][:self.col] + _BLANK_ROW[self.col:]
        if self.mode != "popon":
            self._update(time)

    def _backspace(self, time: float) -> None:
        if self.mode == "text":
            return
        if self.col > 0:
            self.col -= 1
            buffer = self._buffer()
            row = max(0, min(14, self.row))
            line = buffer[row]
            buffer[row] = line[:self.col] + " " + line[self.col + 1:]
            if self.mode != "popon":
                self._update(time)

    def _carriage_return(self, time: float) -> None:
        if self.mode == "text":
            return
        if self.mode == "roll":
            base = max(0, min(14, self.base_row))
            top = max(0, base - self.roll_rows + 1)
            for row in range(top, base):
                self.visible[row] = self.visible[row + 1]
            self.visible[base] = _BLANK_ROW
            self._set_cursor(base, 0)
            self._restart(time)
        else:
            self.col = 0
            if self.mode == "paint":
                self.row = min(14, self.row + 1)

    # -- commands ----------------------------------------------------------
    def _erase_memory(self, displayed: bool, time: float) -> None:
        if displayed:
            self.visible = [_BLANK_ROW] * 15
            if self.mode == "popon":
                self._set_cursor(14, 0)
            self._update(time)
        else:
            self.hidden = [_BLANK_ROW] * 15
            if self.mode == "popon":
                self._set_cursor(14, 0)

    def _pac(self, first: int, second: int, time: float) -> None:
        if self.mode == "text":
            return
        index = ((first << 1) & 14) | ((second >> 5) & 1)
        row = _ROWDATA[index]
        if row < 1:
            return
        if 0x40 <= second <= 0x5F:
            indent_index = second - 0x40
        elif 0x60 <= second <= 0x7F:
            indent_index = second - 0x60
        else:
            return
        indent = (indent_index >> 1) * 4
        self._set_cursor(row - 1, indent)
        if self.mode == "roll":
            self.base_row = row - 1
            for below in range(row, 15):
                self.visible[below] = _BLANK_ROW
            self._update(time)

    def _command(self, first: int, second: int, time: float) -> None:
        if first in (0x17, 0x1F) and 0x21 <= second <= 0x23:
            if self.mode != "text":
                self.col = min(31, self.col + (second - 0x20))
            return
        if first not in (0x14, 0x1C):
            return
        if second == 0x20:                     # RCL
            self.mode = "popon"
        elif second == 0x21:                   # BS
            self._backspace(time)
        elif second == 0x24:                   # DER
            self._erase_row(time)
        elif second in (0x25, 0x26, 0x27):      # RU2/RU3/RU4
            rows = second - 0x23
            if self.mode in ("popon", "paint"):
                self.visible = [_BLANK_ROW] * 15
            self.mode = "roll"
            self.roll_rows = rows
            if not self._have_cursor:
                self._set_cursor(14, 0)
            self.base_row = self.row
            self.visible[self.base_row:self.base_row + 1] = [_BLANK_ROW]
            self._update(time)
        elif second == 0x29:                   # RDC
            self.mode = "paint"
        elif second == 0x2A:                   # TR (text restart)
            self.mode = "text"
        elif second == 0x2B:                   # RTD (text display)
            self.mode = "text"
        elif second == 0x2C:                   # ENM
            self._erase_memory(False, time)
        elif second == 0x2D:                   # CR
            self._carriage_return(time)
        elif second == 0x2E:                   # EDM
            self._erase_memory(True, time)
        elif second == 0x2F:                   # EOC
            self.visible, self.hidden = self.hidden, [_BLANK_ROW] * 15
            self._set_cursor(14, 0)
            self._update(time)

    def _extended(self, first: int, second: int, time: float) -> None:
        if first == 0x11 and 0x30 <= second <= 0x3F:
            char = _EXTENDED_NORTH_AMERICAN.get(second)
        elif first in (0x12, 0x13) and 0x20 <= second <= 0x3F:
            char = _EXTENDED_SPANISH.get(second)
        else:
            char = None
        if char is not None:
            self._write(char, time)

    def feed(self, first: int, second: int, time: float) -> None:
        first = _strip_parity(first)
        second = _strip_parity(second)
        if not first and not second:
            return
        if first >= 0x20:
            if second < 0x20:
                return
            for code in (first, second):
                char = "\u266a" if code == 0x7F else chr(code)
                self._write(char, time)
            return
        # Control codes are transmitted twice; ignore an immediate repeat.
        pair = (first, second)
        if pair == self._last_control and time - self._last_control_time < 0.5:
            return
        self._last_control = pair
        self._last_control_time = time
        if first >= 0x18:
            first -= 8
        if first == 0x10:
            if 0x40 <= second <= 0x7F:
                self._pac(first, second, time)
            elif 0x20 <= second <= 0x3F:
                char = _SPECIAL.get(second)
                if char is not None:
                    self._write(char, time)
        elif first == 0x11:
            if 0x20 <= second <= 0x2F:
                self._space(time)
            elif 0x30 <= second <= 0x3F:
                self._extended(first, second, time)
            elif 0x40 <= second <= 0x7F:
                self._pac(first, second, time)
        elif first in (0x12, 0x13):
            if 0x20 <= second <= 0x3F:
                self._extended(first, second, time)
            elif 0x40 <= second <= 0x7F:
                self._pac(first, second, time)
        elif first in (0x14, 0x15):
            if 0x20 <= second <= 0x2F:
                self._command(first, second, time)
            elif 0x40 <= second <= 0x7F:
                self._pac(first, second, time)
        elif first == 0x16:
            if 0x40 <= second <= 0x7F:
                self._pac(first, second, time)
        elif first == 0x17:
            if 0x21 <= second <= 0x23 or 0x2E <= second <= 0x2F:
                self._command(first, second, time)
            elif 0x40 <= second <= 0x7F:
                self._pac(first, second, time)


class _UnitSplitter:
    """Turn a byte stream into MPEG start-code units."""

    def __init__(self, limit: int = 8 << 20) -> None:
        self.buffer = bytearray()
        self.limit = limit

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer += chunk
        units: list[bytes] = []
        while True:
            start = self.buffer.find(b"\x00\x00\x01")
            if start < 0:
                if len(self.buffer) > 2:
                    del self.buffer[:-2]
                break
            if start:
                del self.buffer[:start]
            end = self.buffer.find(b"\x00\x00\x01", 3)
            if end < 0:
                if len(self.buffer) > self.limit:
                    # Keep the three-byte prefix so the next chunk can be
                    # searched from a sane position; the oversized unit's
                    # contents are not needed.
                    del self.buffer[3:self.limit]
                break
            units.append(bytes(self.buffer[3:end]))
            del self.buffer[:end]
        return units


def _pts(data: bytes) -> int | None:
    if len(data) < 5:
        return None
    return (((data[0] >> 1) & 0x07) << 30
            | data[1] << 22
            | ((data[2] >> 1) & 0x7F) << 15
            | data[3] << 7
            | (data[4] >> 1) & 0x7F)


class _EsParser:
    """Extract caption blocks and their timestamps from MPEG-2/PS bytes."""

    def __init__(self, decoder: _CaptionDecoder) -> None:
        self.decoder = decoder
        self.fps = 30000 / 1001
        self.gop_base: float | None = None
        self.gop_time = 0.0
        self.pts_base = 0.0
        self.frame_index = 0
        self.pending_pts: int | None = None
        self.seen_pts = False
        self.current_time = 0.0
        self._splitter = _UnitSplitter()

    def _picture_time(self, body: bytes) -> None:
        if self.pending_pts is not None:
            self.pts_base = self.pending_pts / 90000.0
            self.frame_index = 0
            self.pending_pts = None
            self.seen_pts = True
        if self.seen_pts:
            self.current_time = self.pts_base + self.frame_index / self.fps
        else:
            temporal = 0
            if len(body) >= 2:
                temporal = (body[0] << 2) | (body[1] >> 6)
            self.current_time = self.gop_time + temporal / self.fps
        self.frame_index += 1

    def feed(self, chunk: bytes) -> None:
        for unit in self._splitter.feed(chunk):
            if not unit:
                continue
            code = unit[0]
            body = unit[1:]
            if code == 0xB3 and len(body) >= 4:
                rate = _FRAME_RATES.get(body[3] & 0x0F)
                if rate:
                    self.fps = rate
            elif code == 0xB8 and len(body) >= 4:
                first, second, third = body[0], body[1], body[2]
                hours = (first >> 2) & 0x1F
                minutes = ((first & 0x03) << 4) | ((second >> 4) & 0x0F)
                seconds = ((second & 0x0F) << 2) | ((third >> 6) & 0x03)
                frames = third & 0x3F
                stamp = (hours * 3600 + minutes * 60 + seconds
                         + frames / self.fps)
                if self.gop_base is None:
                    self.gop_base = stamp
                self.gop_time = max(0.0, stamp - self.gop_base)
            elif code == 0xE0 and len(body) >= 5:
                if body[2] & 0xC0 == 0x80 and body[3] & 0x80:
                    value = _pts(body[5:10])
                    if value is not None:
                        self.pending_pts = value
            elif code == 0x00 and len(body) >= 2:
                self._picture_time(body)
            elif code == 0xB2 and body[:4] == CC_MAGIC:
                for field, first, second in _caption_blocks(body):
                    if field == 1:
                        self.decoder.feed(first, second, self.current_time)


def _timestamp(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    whole, milliseconds = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


def write_srt(cues: Iterable[Cue], destination: Path) -> Path:
    lines: list[str] = []
    number = 1
    for cue in cues:
        text = cue.text.strip()
        if not text:
            continue
        if cue.end <= cue.start:
            continue
        lines.append(str(number))
        lines.append(f"{_timestamp(cue.start)} --> {_timestamp(cue.end)}")
        lines.extend(text.splitlines())
        lines.append("")
        number += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + ("\n" if lines else ""),
                           encoding="utf-8")
    return destination


def extract_srt(inputs: Sequence[str], destination: Path,
                duration: float = 0.0,
                cancelled: Callable[[], bool] | None = None) -> Path | None:
    """Decode captions from ``inputs`` into ``destination``.

    Returns ``None`` when the title carries no captions (or only padding),
    so the caller can drop the synthetic track instead of muxing an empty
    subtitle stream.
    """
    decoder = _CaptionDecoder()
    parser = _EsParser(decoder)
    for name in inputs:
        if cancelled is not None and cancelled():
            raise KeyboardInterrupt
        with open(name, "rb") as handle:
            while True:
                if cancelled is not None and cancelled():
                    raise KeyboardInterrupt
                chunk = handle.read(4 << 20)
                if not chunk:
                    break
                parser.feed(chunk)
    cues = decoder.finish(duration)
    if not any(cue.text.strip() for cue in cues):
        destination.unlink(missing_ok=True)
        return None
    return write_srt(cues, destination)


__all__ = [
    "CC_MAGIC", "Cue", "caption_stream", "extract_srt", "has_captions",
    "title_has_captions", "write_srt",
]
