"""DVD-Video structure parser.

Only the tables needed to list titles and to feed a remux are read:
``TT_SRPT`` (title -> title set / title number), ``VTS_PGCIT`` (duration,
chapters) and the ``VTSI`` stream attribute blocks.  Layouts follow the public
DVD-Video structure documentation (dvd.sourceforge.net/dvdinfo), and were
validated against a real disc as described in CLONE_SPEC §25.

CSS/CPPM decryption is not part of parsing: the parser reads the unencrypted
IFO files, and :func:`looks_encrypted` reports when the VOB payload is
scrambled.  :mod:`opticrip.dvdcss` handles decryption when a user-supplied
libdvdcss is available.
"""

from __future__ import annotations

import struct

from . import cc as cclib
from .discfs import DiscFS
from .model import (Disc, FLAG_ALT_COMMENTARY, FLAG_COMMENTARY,
                    FLAG_VISUALLY_IMPAIRED, Segment, Stream, Title)

VIDEO_TS = "VIDEO_TS"


class DvdError(Exception):
    pass


def _u16(blob: bytes, offset: int) -> int:
    return struct.unpack_from(">H", blob, offset)[0]


def _u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from(">I", blob, offset)[0]


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


def decode_playtime(blob: bytes, offset: int) -> float:
    """4-byte BCD ``hh mm ss ff``; bits 7-6 of the frame byte are the rate."""
    hours, minutes, seconds, frames = blob[offset:offset + 4]
    rate = frames >> 6
    fps = 30.0 if rate == 3 else 25.0 if rate == 1 else 30.0
    # The low six bits are BCD frames; retail discs often contain values that
    # look binary (0x24 = 24 frames, not 36).
    frame = _bcd(frames & 0x3F)
    if frame > int(fps) - 1:
        frame = min(frame & 0x0F, int(fps) - 1)
    return _bcd(hours) * 3600 + _bcd(minutes) * 60 + _bcd(seconds) + frame / fps


_SHORT_LANG = {
    "en": "eng", "fr": "fre", "es": "spa", "de": "ger", "it": "ita",
    "ja": "jpn", "zh": "chi", "ko": "kor", "pt": "por", "ru": "rus",
    "nl": "dut", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "pl": "pol", "cs": "cze", "hu": "hun", "el": "gre", "tr": "tur",
    "he": "heb", "ar": "ara", "hi": "hin", "th": "tha",
}


def decode_language(blob: bytes, offset: int) -> str:
    """Language is either the 2-byte 5-bit code or two ASCII letters.

    Retail discs authored by some tools store ``en``/``fr`` literally; the
    reference resolves both forms to ISO 639-2.
    """
    raw = blob[offset:offset + 2]
    if len(raw) < 2:
        return "und"
    if all(0x61 <= byte <= 0x7A for byte in raw):
        short = raw.decode("ascii")
        return _SHORT_LANG.get(short, short)
    code = _u16(blob, offset)
    chars = [
        chr(((code >> 10) & 0x1F) + 0x60),
        chr(((code >> 5) & 0x1F) + 0x60),
        chr((code & 0x1F) + 0x60),
    ]
    text = "".join(chars)
    return text if text.isalpha() else "und"


def _lang_name(code: str) -> str:
    names = {
        "eng": "English", "fre": "French", "fra": "French", "spa": "Spanish",
        "ger": "German", "deu": "German", "ita": "Italian", "jpn": "Japanese",
        "chi": "Chinese", "zho": "Chinese", "kor": "Korean", "por": "Portuguese",
        "rus": "Russian", "dut": "Dutch", "nld": "Dutch", "swe": "Swedish",
        "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "pol": "Polish",
        "cze": "Czech", "ces": "Czech", "hun": "Hungarian", "gre": "Greek",
        "ell": "Greek", "tur": "Turkish", "heb": "Hebrew", "ara": "Arabic",
        "hin": "Hindi", "tha": "Thai", "und": "Unknown",
    }
    return names.get(code.lower(), "")


def _if_name(vts: int, kind: str) -> str:
    if kind == "video":
        return f"{VIDEO_TS}/VTS_{vts:02d}_0.IFO"
    return f"{VIDEO_TS}/VIDEO_TS.IFO"


def _read_ifo(fs: DiscFS, name: str) -> bytes:
    if not fs.exists(name):
        raise DvdError(f"missing {name}")
    return fs.read(name)


def _pgc_offsets(pgcit: bytes, index: int) -> int:
    count = _u16(pgcit, 0)
    if index >= count:
        raise DvdError(f"PGC {index} out of range ({count})")
    return _u32(pgcit, 12 + index * 8)


def parse_pgc(pgcit_sector: bytes, pgc_offset: int) -> dict:
    pgc = pgcit_sector[pgc_offset:]
    if len(pgc) < 0xEC:
        raise DvdError("short PGC")
    programs = pgc[2]
    cells = pgc[3]
    duration = decode_playtime(pgc, 4)
    program_map_offset = _u16(pgc, 0xE6)
    cell_playback_offset = _u16(pgc, 0xE8)
    program_map = [pgc[program_map_offset + i] for i in range(programs)]
    cell_times: list[float] = []
    cell_starts: list[int] = []
    cell_sectors: list[int] = []
    for i in range(cells):
        base = cell_playback_offset + i * 24
        if base + 24 > len(pgc):
            break
        cell_times.append(decode_playtime(pgc, base + 4))
        first = _u32(pgc, base + 8)
        last = _u32(pgc, base + 20)
        cell_starts.append(first)
        cell_sectors.append((last - first + 1) * 2048 if last >= first else 0)
    chapters: list[float] = []
    elapsed = 0.0
    starts = sorted(set(program_map)) or [1]
    cell_index = 0
    for cell_number in starts:
        target = max(0, cell_number - 1)
        while cell_index < target and cell_index < len(cell_times):
            elapsed += cell_times[cell_index]
            cell_index += 1
        chapters.append(round(elapsed, 3))
    return {
        "programs": programs,
        "cells": cells,
        "duration": duration,
        "chapters": chapters,
        "cell_starts": cell_starts,
        "cell_times": cell_times,
        "cell_bytes": cell_sectors,
        "byte_size": sum(cell_sectors),
    }


def _video_stream(attr: int) -> Stream:
    compression = (attr >> 14) & 0x03
    tv = (attr >> 12) & 0x03
    aspect = (attr >> 10) & 0x03
    # On a real disc 0x5300 decodes as MPEG-2/PAL and 0x4E80 as MPEG-2/NTSC
    # with 16:9, so the 01 value means MPEG-2 in practice.
    codec = "MPEG-2" if compression in (1, 2) else "MPEG-1"
    return Stream(
        kind="video", index=0, codec=codec, codec_long=codec,
        codec_id="V_MPEG2" if codec == "MPEG-2" else "V_MPEG1",
        width=720 if tv else 720,
        height=480 if tv == 0 else 576,
        aspect="16:9" if aspect == 3 else "4:3",
        fps="29.97 (30000/1001)" if tv == 0 else "25",
        language="und",
        output_codec=codec,
        # VERIFIED: the reference reports the DVD-Video peak rate for every
        # video stream, regardless of the title's real average bitrate.
        bitrate=9_800_000,
    )


_AUDIO_CODECS = {0: "AC-3", 1: "MPEG-1", 2: "MPEG-2", 3: "LPCM", 4: "DTS", 6: "SDDS"}

# AC-3 frame-size codes, duplicated because each bitrate has two codes.
_AC3_BITRATES = [
    32000, 32000, 40000, 40000, 48000, 48000, 56000, 56000,
    64000, 64000, 80000, 80000, 96000, 96000, 112000, 112000,
    128000, 128000, 160000, 160000, 192000, 192000, 224000, 224000,
    256000, 256000, 320000, 320000, 384000, 384000, 448000, 448000,
    512000, 512000, 576000, 576000, 640000, 640000,
]


def _audio_stream(index: int, attr: bytes, bitrate: int = 0) -> Stream:
    # VERIFIED: attribute 5 is the code extension (0 unspecified, 1 normal,
    # 2 visually impaired, 3 director's comments, 4 alternate comments).
    flags = {2: FLAG_VISUALLY_IMPAIRED, 3: FLAG_COMMENTARY,
             4: FLAG_ALT_COMMENTARY}.get(attr[5], 0)
    fmt = (attr[0] >> 5) & 0x07
    channels = (attr[1] & 0x07) + 1
    rate = 96000 if (attr[1] >> 4) & 0x03 == 1 else 48000
    language = decode_language(attr, 2)
    codec = _AUDIO_CODECS.get(fmt, "AC-3")
    # VERIFIED: the reference spells multichannel layouts "Surround 5.1" and
    # keeps "Stereo" plain (CLONE_SPEC §25.3).
    layouts = {1: "Mono", 2: "Stereo", 3: "2.1", 4: "Surround 4.0",
               5: "Surround 5.0", 6: "Surround 5.1", 7: "Surround 6.1",
               8: "Surround 7.1"}
    return Stream(
        kind="audio", index=index, codec=codec, codec_long=codec,
        codec_id={"AC-3": "A_AC3", "DTS": "A_DTS", "LPCM": "A_PCM"}.get(codec, "A_AC3"),
        language=language, language_name=_lang_name(language),
        channels=channels, channel_layout=layouts.get(channels, ""),
        sample_rate=rate, bitrate=bitrate, flags=flags, output_codec=codec,
    )


def _subtitle_stream(index: int, attr: bytes) -> Stream:
    language = decode_language(attr, 2)
    kind = attr[0] & 0x0F
    # VERIFIED: SPRM 19 spells 13 director's comments and 14 alternate
    # director's comments in the subpicture code-extension byte.
    flags = {13: FLAG_COMMENTARY, 14: FLAG_ALT_COMMENTARY}.get(attr[5], 0)
    return Stream(
        kind="subtitle", index=index, codec="DVD subtitles", codec_long="DVD subtitles",
        codec_id="S_VOBSUB", language=language, language_name=_lang_name(language),
        output_codec="DVD subtitles", forced=(kind == 5), flags=flags,
    )


def _ac3_bitrates_from_head(data: bytes) -> dict[int, int]:
    """Read AC-3 frame headers from the clear head of each VOB sector.

    CSS encrypts sector payloads but leaves the first 0x80 bytes readable, and
    a DVD audio PES packet starts there: ``00 00 01 BD``, its MPEG-2 optional
    header, then ``substream_id``, a three-byte frame pointer and the AC-3 sync
    word.  The frame-size code in that first header gives the track's bitrate
    without decrypting anything (verified on the test disc: 448/192/384/192).
    """
    result: dict[int, int] = {}
    for sector in range(0, len(data) - 23, 2048):
        block = data[sector:sector + 2048]
        start = block.find(b"\x00\x00\x01\xbd")
        if not 0 <= start <= 64 or start + 12 > len(block):
            continue
        header_length = block[start + 8]
        sub = start + 9 + header_length
        if sub + 9 > len(block):
            continue
        substream = block[sub]
        if not 0x80 <= substream <= 0x87:
            continue
        # substream:1, frame headers:1, first access unit pointer:2, then AC-3.
        if block[sub + 4:sub + 6] != b"\x0b\x77":
            continue
        code = block[sub + 8] & 0x3F
        if code < len(_AC3_BITRATES):
            result.setdefault(substream - 0x80, _AC3_BITRATES[code])
        if len(result) == 8:
            break
    return result


def read_streams(vts_ifo: bytes,
                 audio_bitrates: dict[int, int] | None = None) -> list[Stream]:
    if len(vts_ifo) < 0x2C6:
        return []
    streams: list[Stream] = []
    video_attr = _u16(vts_ifo, 0x200)
    if video_attr:
        streams.append(_video_stream(video_attr))
    bitrates = audio_bitrates or {}
    audio_count = min(_u16(vts_ifo, 0x202), 8)
    for i in range(audio_count):
        streams.append(_audio_stream(i, vts_ifo[0x204 + i * 8:0x20C + i * 8],
                                     bitrates.get(i, 0)))
    # VERIFIED offsets (dvdinfo ifo.html): 16 reserved bytes follow the eight
    # audio attributes, so the subpicture table starts at 0x254/0x256, not
    # 0x244/0x246.
    sub_count = min(_u16(vts_ifo, 0x254), 32)
    for i in range(sub_count):
        streams.append(_subtitle_stream(i, vts_ifo[0x256 + i * 6:0x25C + i * 6]))
    for ordinal, stream in enumerate(streams):
        stream.index = ordinal
        # ffmpeg presents VOB streams in this same order (video, audio,
        # subtitle), so the ordinal doubles as the input stream index.
        stream.source_index = ordinal
    audio = [s for s in streams if s.kind == "audio"]
    subtitles = [s for s in streams if s.kind == "subtitle"]
    if audio:
        audio[0].default = True
    if subtitles:
        subtitles[0].default = True
    return streams


def _cell_map(count: int) -> str:
    """TINFO attribute 26, the cell range that makes up a title.

    VERIFIED on the test disc: a 17-cell title reports ``1-17``, a two-cell
    title reports ``1,2`` (a two-cell run is not collapsed), and a one-cell
    title reports ``1`` (CLONE_SPEC §25.3).
    """
    if count <= 0:
        return ""
    if count == 2:
        return "1,2"
    return "1" if count == 1 else f"1-{count}"


def _ptt_table(vts_ifo: bytes, ttn: int) -> list[tuple[int, int]]:
    """Return ``(PGC number, program number)`` per chapter for ``ttn``.

    Titles marked "not one sequential PGC" (TT_SRPT type bit 6) do not index
    ``VTS_PGCI`` by TTN: the first PTT entry names the PGC that holds the
    title.  VERIFIED on the test disc, where title 76 (TTN 2) lives in PGCN 5.
    """
    if len(vts_ifo) < 0xCC:
        return []
    base = _u32(vts_ifo, 0xC8) * 2048
    if base <= 0 or base >= len(vts_ifo):
        return []
    blob = vts_ifo[base:]
    count = _u16(blob, 0)
    if not (1 <= ttn <= count) or len(blob) < 8 + count * 4:
        return []
    # The per-title words are *starts* (relative to VTS_PTT_SRPT); the last
    # table ends at the end address in the header.  Read as end addresses the
    # table would be one byte short and decode every value byte-swapped.
    start = _u32(blob, 8 + (ttn - 1) * 4)
    if ttn < count:
        stop = _u32(blob, 8 + ttn * 4)
    else:
        stop = _u32(blob, 4) + 1
    entries: list[tuple[int, int]] = []
    for offset in range(start, min(stop, len(blob)), 4):
        if offset + 4 > len(blob):
            break
        entries.append((_u16(blob, offset), _u16(blob, offset + 2)))
    return entries


def _vob_files(fs: DiscFS, vts: int) -> list[str]:
    names = []
    for entry in fs.listdir(VIDEO_TS):
        upper = entry.upper()
        if upper.startswith(f"VTS_{vts:02d}_") and upper.endswith(".VOB") and not upper.endswith("_0.VOB"):
            names.append(f"{VIDEO_TS}/{entry}")
    return sorted(names)


def _cell_segments(fs: DiscFS, vobs: list[str], starts: list[int],
                   sizes: list[int], times: list[float]) -> list[Segment]:
    """Map VTS cell addresses to byte ranges in the VOB set.

    DVD PGC cell starts are 2048-byte sector numbers relative to the first
    content VOB (``VTS_xx_1.VOB``), not to the menu VOB.  A cell can cross a
    VOB boundary, so split it and coalesce adjacent ranges afterwards.  This
    is what keeps a title from carrying the next title's packets, whose PTS
    would otherwise overlap and make playback jump.
    """
    if not vobs or not starts or not sizes:
        return []
    if len(starts) != len(sizes) or len(starts) != len(times):
        return []
    lengths: list[int] = []
    for name in vobs:
        try:
            lengths.append(fs.size(name))
        except OSError:
            return []
    total = sum(lengths)
    ranges: list[Segment] = []
    for sector, size, seconds in zip(starts, sizes, times):
        offset = int(sector) * 2048
        remaining = min(int(size), max(0, total - offset))
        if remaining <= 0:
            continue
        cursor = 0
        for name, length in zip(vobs, lengths):
            if remaining <= 0:
                break
            if offset >= cursor + length:
                cursor += length
                continue
            local = offset - cursor
            take = min(remaining, length - local)
            if take > 0:
                ranges.append(Segment(path=name, duration=float(seconds),
                                      byte_offset=local, byte_size=take))
                offset += take
                remaining -= take
            cursor += length
    merged: list[Segment] = []
    for segment in ranges:
        if (merged and merged[-1].path == segment.path
                and merged[-1].byte_offset + merged[-1].byte_size
                == segment.byte_offset):
            merged[-1].byte_size += segment.byte_size
        else:
            merged.append(segment)
    return merged


def _sector_is_scrambled(sector: bytes) -> bool:
    """Read CSS scrambling bits from the PES header inside one sector.

    A VOB sector starts with a pack header; a system header may follow
    before the PES packet.  The scrambling-control bits live in the first
    PES flags byte, so the offsets must be walked rather than assuming a
    PES packet always starts at 0x0E.  Some unencrypted discs put a system
    header there whose bytes can otherwise look like a scrambled PES
    (observed with ffmpeg's VOB muxer).
    """
    if len(sector) < 0x18 or sector[:3] != b"\x00\x00\x01":
        return False
    position = 0
    if sector[3] == 0xBA:
        if len(sector) < 14:
            return False
        position = 14 + (sector[13] & 0x07)
        if sector[position:position + 3] == b"\x00\x00\x01" and sector[position + 3] == 0xBB:
            if len(sector) < position + 6:
                return False
            length = int.from_bytes(sector[position + 4:position + 6], "big")
            position += 6 + length
            while position < len(sector) and sector[position] == 0xFF:
                position += 1
    if sector[position:position + 3] != b"\x00\x00\x01":
        return False
    stream = sector[position + 3]
    if not 0xBD <= stream <= 0xEF:
        return False
    return position + 6 < len(sector) and bool(sector[position + 6] & 0x30)


def has_scrambled_sectors(blob: bytes) -> bool:
    """True when any 2048-byte sector has CSS scrambling-control bits set.

    The pack header and the first bytes of the PES header stay clear, so a
    sequence start code can survive at the beginning of a scrambled sector.
    """
    flagged = 0
    for offset in range(0, len(blob) - 0x18, 2048):
        if _sector_is_scrambled(blob[offset:offset + 2048]):
            flagged += 1
            # One stray bit in a PES header is not enough to call a disc CSS.
            if flagged >= 2:
                return True
    return False


def looks_encrypted(fs: DiscFS, vts_numbers: list[int],
                    heads: dict[int, bytes] | None = None) -> bool:
    """Detect CSS from per-sector scrambling flags, with a header fallback.

    A sequence header can remain in the clear part of a scrambled sector, so
    the old "no sequence header" test is not sufficient; the sector flags are.
    """
    for vts in vts_numbers:
        if heads is not None and vts in heads:
            # An empty cached head means the read failed; do not seek again.
            head = heads[vts]
        else:
            files = _vob_files(fs, vts)
            if not files:
                continue
            try:
                head = fs.read(files[0], 0, 128 * 1024)
            except OSError:
                continue
        if not head:
            continue
        if has_scrambled_sectors(head):
            return True
        if b"\x00\x00\x01\xb3" in head:
            return False
        return True
    return False


def parse_disc(fs: DiscFS, min_length: int = 0, message=None,
               skip_vob_reads: bool = False) -> Disc:
    """Parse the IFOs.

    ``skip_vob_reads`` is used when the VOBs are already known to be CSS
    protected and libdvdcss is active.  Reading them once for AC-3 bitrate
    tables would trigger a multi-second title-key search at open time; the
    save path decodes them anyway.  Audio bitrate rows are then omitted.
    """
    vmgi = _read_ifo(fs, f"{VIDEO_TS}/VIDEO_TS.IFO")
    if vmgi[:12] != b"DVDVIDEO-VMG":
        raise DvdError("not a DVD-Video manager IFO")
    tt_sector = _u32(vmgi, 0xC4)
    if tt_sector * 2048 >= len(vmgi):
        vmgi = fs.read(f"{VIDEO_TS}/VIDEO_TS.IFO")
    tt = vmgi[tt_sector * 2048:] if len(vmgi) > tt_sector * 2048 else b""
    if len(tt) < 8:
        raise DvdError("short title table")
    count = _u16(tt, 0)
    disc = Disc(kind="DVD")
    used_vts: list[int] = []
    cache: dict[int, bytes] = {}
    bitrates: dict[int, dict[int, int]] = {}
    heads: dict[int, bytes] = {}
    vob_cache: dict[int, list[str]] = {}
    read_protected = bool(skip_vob_reads)
    for i in range(count):
        base = 8 + i * 12
        if base + 12 > len(tt):
            break
        title_type = tt[base]
        angles = tt[base + 1] or 1
        ptts = _u16(tt, base + 2)
        vts = tt[base + 6]
        ttn = tt[base + 7]
        if vts == 0:
            continue
        index = ttn - 1
        if vts not in cache:
            cache[vts] = _read_ifo(fs, f"{VIDEO_TS}/VTS_{vts:02d}_0.IFO")
        vts_ifo = cache[vts]
        if vts_ifo[:12] != b"DVDVIDEO-VTS":
            continue
        pgcit_sector = _u32(vts_ifo, 0xCC)
        pgcit = vts_ifo[pgcit_sector * 2048:]
        if len(pgcit) < 16:
            pgcit = fs.read(f"{VIDEO_TS}/VTS_{vts:02d}_0.IFO", pgcit_sector * 2048)
        # Non-sequential titles (type bit 6) are located through the PTT
        # table, not by TTN; using TTN there picks a 9-second cell instead of
        # the 4:31 title on the verified disc.
        pgc_index = index
        if title_type & 0x40:
            ptt = _ptt_table(vts_ifo, ttn)
            if ptt and ptt[0][0]:
                pgc_index = ptt[0][0] - 1
        try:
            pgc = parse_pgc(pgcit, _pgc_offsets(pgcit, pgc_index))
        except (DvdError, struct.error):
            continue
        duration = pgc["duration"] or ptts * 0.0
        if not duration:
            continue
        # The reference trims trailing structure-protection cells (verified
        # capture: "Cells 18-18 were removed from title end", turning 83:12
        # into 83:02).  Cells shorter than 12 s at the very end are dropped.
        cells = list(pgc["cell_times"])
        cell_bytes = list(pgc.get("cell_bytes") or [])
        cell_starts = list(pgc.get("cell_starts") or [])
        packed = int(pgc.get("byte_size") or 0)
        trimmed = 0
        while len(cells) > 1 and cells[-1] < 12.0:
            duration -= cells.pop()
            if cell_bytes:
                packed -= cell_bytes.pop()
            if cell_starts:
                cell_starts.pop()
            trimmed += 1
        chapters = [c for c in pgc["chapters"] if c < duration - 0.5]
        if not chapters:
            chapters = [0.0]
        if min_length and duration < min_length:
            if message:
                message(3025, f"{i + 1}", int(duration), min_length)
            continue
        if trimmed and message:
            # Verified wording: "Cells 18-18 were removed from title end".
            message(3038, pgc["cells"], pgc["cells"])
        if vts not in vob_cache:
            vob_cache[vts] = _vob_files(fs, vts)
        vobs = vob_cache[vts]
        if vts not in heads:
            if read_protected:
                # One protected VOB is enough to classify the disc; further
                # seeks only make the optical drive spin longer.
                heads[vts] = b""
            else:
                try:
                    # One read serves both the AC-3 bitrate table and the CSS
                    # heuristic; optical-drive seeks are the expensive part.
                    heads[vts] = fs.read(vobs[0], 0, 256 * 1024) if vobs else b""
                except OSError as exc:
                    heads[vts] = b""
                    # Windows reports CSS-locked VOB payloads as EINVAL even
                    # though the IFOs stay readable.
                    if exc.errno == 22:
                        read_protected = True
        if vts not in bitrates:
            bitrates[vts] = _ac3_bitrates_from_head(heads[vts])
        if vts not in used_vts:
            used_vts.append(vts)
        size = max(0, packed)
        if not size:
            for name in vobs:
                try:
                    size += fs.size(name)
                except OSError:
                    pass
        segments = _cell_segments(fs, vobs, cell_starts, cell_bytes, cells)
        if not segments:
            # A damaged or synthetic IFO without usable cell addresses still
            # lists the title; fall back to whole VOBs rather than losing it.
            segments = [Segment(path=name) for name in vobs]
        streams = read_streams(vts_ifo, bitrates.get(vts, {}))
        # DVD captions live in MPEG-2 user data, so the reference lists a
        # synthetic ``S_CC608/DVD`` track after the real subtitle tracks.
        # Detection samples the start of the title; a protected disc whose
        # payload cannot be read (no libdvdcss yet) gets no row rather than a
        # guessed one, exactly like the other VOB-derived fields.
        if not read_protected and cclib.title_has_captions(fs, segments):
            streams.append(cclib.caption_stream(len(streams)))
        title = Title(
            index=len(disc.titles),
            source="dvd",
            duration=duration,
            size_bytes=size,
            chapter_times=chapters,
            streams=streams,
            vts=vts,
            # TINFO attr 24 is the global title number, not the TTN.
            original_id=f"{i + 1:02d}",
            angle=1,
            segments=segments,
            segment_map=_cell_map(len(cells)),
        )
        title.chapter_times = sorted(set(title.chapter_times))
        if message:
            message(3028, i + 1, len(cells), title.duration_text)
        disc.titles.append(title)
        _ = (title_type, angles, ptts)
    disc.title_count = len(disc.titles)
    disc.label = ""
    disc.encrypted = read_protected or looks_encrypted(fs, used_vts, heads)
    disc.protection = "CSS/CPPM" if disc.encrypted else "None"
    disc.size_bytes = sum(t.size_bytes for t in disc.titles)
    # Deduplicate titles pointing at the same PGC (angles / duplicate TT
    # entries are common on retail discs).
    seen: set[tuple[int, int]] = set()
    unique: list[Title] = []
    for title in disc.titles:
        key = (title.vts, int(round(title.duration * 100)))
        if key in seen:
            continue
        seen.add(key)
        title.index = len(unique)
        unique.append(title)
    disc.titles = unique
    disc.title_count = len(disc.titles)
    return disc


def volume_size(fs: DiscFS) -> int:
    total = 0
    try:
        for name in fs.listdir(VIDEO_TS):
            if name.upper().endswith((".VOB", ".IFO", ".BUP")):
                total += fs.size(f"{VIDEO_TS}/{name}")
    except OSError:
        return 0
    return total
