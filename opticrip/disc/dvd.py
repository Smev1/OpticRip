"""DVD-Video structure parser: ``VIDEO_TS.IFO`` and ``VTS_xx_0.IFO``.

Implemented from the DVD-Video specification as mirrored by libdvdread's
``ifo_types.h``/``ifo_read.c`` and the ``dvd.sourceforge.net`` layout pages
(see ``docs/REFERENCES.md``).  Offsets used below:

``VIDEO_TS.IFO`` (VMG)
    0x00  identifier ``DVDVIDEO-VMG``
    0xC4  VMG_TT_SRPT sector pointer

``VTS_xx_0.IFO`` (VTSI)
    0x00  identifier ``DVDVIDEO-VTS``
    0xC8  VTS_PTT_SRPT sector pointer
    0xCC  VTS_PGCIT   sector pointer
    0xE0  VTS_C_ADT   sector pointer
    0x200 video attributes (2 bytes), 0x203 audio stream count,
    0x204 audio attributes (8 x 8 bytes), 0x255 subpicture count,
    0x256 subpicture attributes (32 x 6 bytes)

Sector pointers are always *relative to the IFO file* and must be multiplied
by 2048.  Byte addresses inside a table are relative to that table's start.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.errors import DiscStructureError
from ..core.interfaces import DiscFileSystem
from ..core.logging_bus import LogBus
from ..core.models import (
    Chapter,
    DiscAnalysis,
    DiscInfo,
    Drive,
    MediaType,
    StreamInfo,
    StreamKind,
    Title,
    mark_main_feature,
    normalise_language,
)
from .binary import Buffer, read_playback_time

IFO_SECTOR = 2048

_VMG_IDENTIFIER = b"DVDVIDEO-VMG"
_VTS_IDENTIFIER = b"DVDVIDEO-VTS"

#: VMG table pointers
_VMG_TT_SRPT_PTR = 0xC4
#: VTSI table pointers
_VTS_PTT_SRPT_PTR = 0xC8
_VTS_PGCIT_PTR = 0xCC
_VTS_C_ADT_PTR = 0xE0
#: VTSI_MAT stream attribute block
_VTS_VIDEO_ATTR = 0x200
_VTS_AUDIO_COUNT = 0x203
_VTS_AUDIO_ATTR = 0x204
_VTS_AUDIO_ATTR_COUNT = 8
_VTS_AUDIO_ATTR_SIZE = 8
_VTS_SUBPIC_COUNT = 0x255
_VTS_SUBPIC_ATTR = 0x256
_VTS_SUBPIC_ATTR_COUNT = 32
_VTS_SUBPIC_ATTR_SIZE = 6
#: PGC header layout
_PGC_SIZE = 0xEC
_PGC_NR_PROGRAMS = 0x02
_PGC_NR_CELLS = 0x03
_PGC_PLAYBACK_TIME = 0x04
_PGC_PROGRAM_MAP_PTR = 0xE6
_PGC_CELL_PLAYBACK_PTR = 0xE8
_PGC_CELL_POSITION_PTR = 0xEA
_CELL_PLAYBACK_SIZE = 24
_CELL_CATEGORY = 0x00
_CELL_PLAYBACK_TIME = 0x04
_CELL_FIRST_SECTOR = 0x08
_CELL_FIRST_ILVU_END = 0x0C
_CELL_LAST_VOBU_START = 0x10
_CELL_LAST_SECTOR = 0x14

_AUDIO_CODECS = {0: "ac3", 2: "mp2", 3: "mp2", 4: "pcm_dvd", 6: "dts"}
_AUDIO_CODEC_LONG = {"ac3": "AC-3", "mp2": "MPEG-1 Audio", "pcm_dvd": "LPCM",
                     "dts": "DTS"}
_SUBPIC_CODECS = {0: "dvd_subtitle", 1: "dvd_subtitle", 2: "dvd_subtitle"}
_SAMPLE_RATES = {0: 48000, 1: 96000}


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #


@dataclass
class DvdCell:
    """One cell of a program chain, with its sector range in the VOB set."""

    number: int
    vob_id: int = 0
    cell_id: int = 0
    duration: float = 0.0
    first_sector: int = 0
    last_sector: int = 0
    first_ilvu_end_sector: int = 0
    last_vobu_start_sector: int = 0
    seamless: bool = False
    interleaved: bool = False
    stc_discontinuity: bool = False
    block_mode: int = 0
    block_type: int = 0

    @property
    def sector_count(self) -> int:
        """Number of 2048-byte sectors this cell occupies (never negative)."""
        return max(0, self.last_sector - self.first_sector + 1)

    @property
    def byte_length(self) -> int:
        return self.sector_count * IFO_SECTOR


@dataclass
class DvdPgc:
    """A program chain: one playable title."""

    number: int
    duration: float = 0.0
    programs: int = 0
    cells: list[DvdCell] = field(default_factory=list)
    #: program number (1-based) -> index into ``cells`` of its first cell
    program_cells: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def chapter_times(self, chapter_count: int) -> list[float]:
        """Chapter start times derived from the program map / cell durations."""
        times: list[float] = []
        cumulative = [0.0]
        for cell in self.cells:
            cumulative.append(cumulative[-1] + cell.duration)
        if self.program_cells:
            for index in self.program_cells[:chapter_count or None]:
                if 0 <= index < len(cumulative):
                    times.append(cumulative[index])
        if not times:
            # fall back to cell boundaries
            times = cumulative[:max(1, chapter_count)] if cumulative else [0.0]
        return times


@dataclass
class DvdTitleRef:
    """Where a title lives: which VTS, which PGC, which PTT numbers."""

    number: int
    vts_number: int
    vts_ttn: int
    angles: int
    ptt_count: int
    pgc_number: int
    vts_start_sector: int


@dataclass
class VtsSet:
    """A parsed ``VTS_xx_0.IFO``."""

    vts_number: int
    buffer: Buffer
    pgcs: list[DvdPgc] = field(default_factory=list)
    ptts: list[list[tuple[int, int]]] = field(default_factory=list)
    stream_attrs: dict[str, object] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def pgc(self, number: int) -> DvdPgc | None:
        for pgc in self.pgcs:
            if pgc.number == number:
                return pgc
        return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_vmg(data: bytes, *, label: str = "VIDEO_TS.IFO") -> list[DvdTitleRef]:
    """Parse the video manager IFO into per-title references."""
    buf = Buffer(data, label)
    if data[:12] != _VMG_IDENTIFIER:
        raise DiscStructureError(
            f"{label} is not a DVD video manager IFO",
            detail=f"identifier bytes were {data[:12]!r}")
    ptr = buf.u32(_VMG_TT_SRPT_PTR)
    if ptr == 0:
        raise DiscStructureError(f"{label} has no title search pointer table")
    table = Buffer(buf.bytes(ptr * IFO_SECTOR, 8 + 12 * 99), f"{label}:VMG_TT_SRPT")
    count = table.u16(0)
    refs: list[DvdTitleRef] = []
    for i in range(count):
        base = 8 + i * 12
        if base + 12 > len(table):
            break
        refs.append(DvdTitleRef(
            number=i + 1,
            vts_number=table.u8(base + 6),
            vts_ttn=table.u8(base + 7) or 1,
            angles=max(1, table.u8(base + 1) or 1),
            ptt_count=table.u16(base + 2),
            pgc_number=0,
            vts_start_sector=table.u32(base + 8),
        ))
    for problem in table.problems:
        LogBus().warning(problem, context="dvd")
    return refs


def parse_vts(data: bytes, vts_number: int, *,
              label: str | None = None) -> VtsSet:
    """Parse one ``VTS_xx_0.IFO``."""
    label = label or f"VTS_{vts_number:02d}_0.IFO"
    buf = Buffer(data, label)
    if data[:12] != _VTS_IDENTIFIER:
        raise DiscStructureError(
            f"{label} is not a DVD video title set IFO",
            detail=f"identifier bytes were {data[:12]!r}")
    result = VtsSet(vts_number=vts_number, buffer=buf)
    result.pgcs = _parse_pgcs(buf, result)
    result.ptts = _parse_ptts(buf, label)
    result.stream_attrs = _parse_stream_attributes(buf)
    result.problems = list(buf.problems)
    return result


def _parse_pgcs(buf: Buffer, result: VtsSet) -> list[DvdPgc]:
    ptr = buf.u32(_VTS_PGCIT_PTR)
    if ptr == 0:
        return []
    pgcit_offset = ptr * IFO_SECTOR
    pgcit = buf.slice(pgcit_offset, label=f"{buf.label}:VTS_PGCIT")
    count = pgcit.u16(0)
    pgcs: list[DvdPgc] = []
    for i in range(min(count, 255)):
        entry = 8 + i * 8
        pgc_start = pgcit.u32(entry + 4)
        if pgc_start == 0:
            continue
        pgc = _parse_pgc(pgcit, pgc_start, i + 1)
        pgcs.append(pgc)
        if pgc.problems:
            result.problems.extend(pgc.problems)
    return pgcs


def _parse_pgc(parent: Buffer, start: int, number: int) -> DvdPgc:
    pgc = DvdPgc(number=number)
    if start + _PGC_SIZE > len(parent):
        pgc.problems.append(
            f"PGC {number} starts at 0x{start:x} which is beyond the "
            f"{len(parent)}-byte PGC information table")
        return pgc

    view = parent.slice(start, label=f"{parent.label}:PGC{number}")
    pgc.programs = view.u8(_PGC_NR_PROGRAMS)
    cell_count = view.u8(_PGC_NR_CELLS)
    playback = read_playback_time(view, _PGC_PLAYBACK_TIME)
    pgc.duration = playback.seconds

    program_map_offset = view.u16(_PGC_PROGRAM_MAP_PTR)
    cell_playback_offset = view.u16(_PGC_CELL_PLAYBACK_PTR)
    cell_position_offset = view.u16(_PGC_CELL_POSITION_PTR)

    # program map: one 1-based cell number per program
    if program_map_offset and pgc.programs:
        for i in range(pgc.programs):
            cell_no = view.u8(program_map_offset + i)
            pgc.program_cells.append(max(0, cell_no - 1))

    # cell position table: vob id + cell id per cell
    positions: list[tuple[int, int]] = []
    if cell_position_offset:
        for i in range(cell_count):
            base = cell_position_offset + i * 4
            positions.append((view.u16(base), view.u8(base + 3)))

    # cell playback table: sectors + durations
    cells: list[DvdCell] = []
    for i in range(cell_count):
        base = cell_playback_offset + i * _CELL_PLAYBACK_SIZE
        if base + _CELL_PLAYBACK_SIZE > len(view):
            pgc.problems.append(
                f"PGC {number}: cell playback entry {i + 1} is truncated")
            break
        category = view.u32(base + _CELL_CATEGORY)
        cell = DvdCell(
            number=i + 1,
            vob_id=positions[i][0] if i < len(positions) else 1,
            cell_id=positions[i][1] if i < len(positions) else i + 1,
            duration=read_playback_time(view, base + _CELL_PLAYBACK_TIME).seconds,
            first_sector=view.u32(base + _CELL_FIRST_SECTOR),
            first_ilvu_end_sector=view.u32(base + _CELL_FIRST_ILVU_END),
            last_vobu_start_sector=view.u32(base + _CELL_LAST_VOBU_START),
            last_sector=view.u32(base + _CELL_LAST_SECTOR),
            block_mode=(category >> 6) & 0x03,
            block_type=(category >> 4) & 0x03,
            seamless=bool((category >> 3) & 0x01),
            interleaved=bool((category >> 2) & 0x01),
            stc_discontinuity=bool((category >> 1) & 0x01),
        )
        cells.append(cell)
    pgc.cells = cells
    if pgc.duration <= 0:
        pgc.duration = sum(c.duration for c in cells)
    return pgc


def _parse_ptts(buf: Buffer, label: str) -> list[list[tuple[int, int]]]:
    """``VTS_PTT_SRPT`` -> per-title lists of ``(pgc_number, program_number)``."""
    ptr = buf.u32(_VTS_PTT_SRPT_PTR)
    if ptr == 0:
        return []
    table = buf.slice(ptr * IFO_SECTOR, label=f"{label}:VTS_PTT_SRPT")
    count = table.u16(0)
    titles: list[list[tuple[int, int]]] = []
    for i in range(min(count, 99)):
        offset = table.u32(8 + i * 4)
        if offset == 0:
            titles.append([])
            continue
        entries: list[tuple[int, int]] = []
        # a PTT entry is 4 bytes; we cannot know the count from the table
        # alone, so read until the next title's offset (or the table end).
        next_offset = table.u32(8 + (i + 1) * 4) if i + 1 < count else 0
        end = next_offset or (len(table) - offset)
        position = offset
        while position + 4 <= min(end, len(table)):
            pgcn = table.u16(position)
            pgn = table.u16(position + 2)
            if pgcn == 0 and pgn == 0:
                break
            entries.append((pgcn, pgn))
            position += 4
        titles.append(entries)
    return titles


def _parse_stream_attributes(buf: Buffer) -> dict[str, object]:
    """Video/audio/subpicture attributes from the VTSI_MAT block."""
    attrs: dict[str, object] = {}

    video_raw = buf.u16(_VTS_VIDEO_ATTR)
    mpeg_version = (video_raw >> 14) & 0x03
    video_format = (video_raw >> 12) & 0x03
    aspect = (video_raw >> 10) & 0x03
    picture_size = (video_raw >> 6) & 0x03
    widths = {0: 720, 1: 704, 2: 352, 3: 352}
    height = 480 if video_format == 0 else 576
    frame_rate = 29.97 if video_format == 0 else 25.0
    attrs["video"] = {
        "codec": "mpeg2video" if mpeg_version == 1 else "mpeg1video",
        "width": widths.get(picture_size, 720),
        "height": height,
        "fps": frame_rate,
        "aspect": "16:9" if aspect == 3 else "4:3",
        "video_format": "NTSC" if video_format == 0 else "PAL",
        "raw": video_raw,
    }

    audio_count = min(buf.u8(_VTS_AUDIO_COUNT), _VTS_AUDIO_ATTR_COUNT)
    audio: list[dict[str, object]] = []
    for i in range(audio_count):
        base = _VTS_AUDIO_ATTR + i * _VTS_AUDIO_ATTR_SIZE
        byte0 = buf.u8(base)
        byte1 = buf.u8(base + 1)
        coding = (byte0 >> 5) & 0x07
        multichannel = bool((byte0 >> 4) & 0x01)
        language_type = (byte0 >> 2) & 0x03
        app_mode = byte0 & 0x03
        quantisation = (byte1 >> 6) & 0x03
        sample_code = (byte1 >> 4) & 0x03
        channels = (byte1 & 0x07) + 1
        if multichannel:
            channels += 1  # 5.1 stored as 6-1=5 in the base field
        codec = _AUDIO_CODECS.get(coding, "unknown")
        language = None
        if language_type in (1, 2):
            language = normalise_language(buf.ascii(base + 2, 2))
        audio.append({
            "index": i,
            "codec": codec,
            "codec_long": _AUDIO_CODEC_LONG.get(codec, codec.upper()),
            "language": language,
            "channels": channels,
            "sample_rate": _SAMPLE_RATES.get(sample_code, 48000),
            "quantisation": {0: 16, 1: 20, 2: 24}.get(quantisation, 16),
            "application_mode": app_mode,
            "multichannel": multichannel,
        })
    attrs["audio"] = audio

    subpic_count = min(buf.u8(_VTS_SUBPIC_COUNT), _VTS_SUBPIC_ATTR_COUNT)
    subpics: list[dict[str, object]] = []
    for i in range(subpic_count):
        base = _VTS_SUBPIC_ATTR + i * _VTS_SUBPIC_ATTR_SIZE
        byte0 = buf.u8(base)
        coding = (byte0 >> 5) & 0x07
        sub_type = byte0 & 0x03
        language = normalise_language(buf.ascii(base + 2, 2))
        subpics.append({
            "index": i,
            "codec": _SUBPIC_CODECS.get(coding, "dvd_subtitle"),
            "language": language,
            "type": sub_type,
            # lang extension 9 == 'forced' display in players (per ffmpeg)
            "forced": int(buf.u8(base + 4)) == 9,
        })
    attrs["subtitle"] = subpics
    attrs["problems"] = list(buf.problems)
    return attrs


# --------------------------------------------------------------------------- #
# Disc-level assembly
# --------------------------------------------------------------------------- #


class DvdStructure:
    """All parsed structure of one DVD-Video disc."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.title_refs: list[DvdTitleRef] = []
        self.vts: dict[int, VtsSet] = {}
        self.warnings: list[str] = []
        self.vmg_problems: list[str] = []


def load_dvd_structure(fs: DiscFileSystem, *,
                       log: LogBus | None = None) -> DvdStructure:
    """Read ``VIDEO_TS/*.IFO`` through a :class:`DiscFileSystem`."""
    log = log or LogBus()
    structure = DvdStructure()

    ifo_root = fs.find_case_insensitive("VIDEO_TS")
    if ifo_root is None:
        raise DiscStructureError("this disc has no VIDEO_TS directory")
    ifo_root = ifo_root.rstrip("/")

    vmg_path = fs.find_case_insensitive(f"{ifo_root}/VIDEO_TS.IFO")
    if vmg_path is None:
        raise DiscStructureError(
            "VIDEO_TS.IFO is missing",
            hint="The disc may be malformed; try re-reading it.")
    try:
        structure.title_refs = parse_vmg(fs.getfile(vmg_path).read(),
                                         label=vmg_path)
    except DiscStructureError as exc:
        structure.warnings.append(str(exc))
        structure.vmg_problems.append(str(exc))
        log.warning(f"{exc}", context="dvd")

    # Every VTS_xx_0.IFO present, whether or not the VMG referenced it.
    for name in fs.listdir(ifo_root):
        lowered = name.lower()
        if not (lowered.startswith("vts_") and lowered.endswith("_0.ifo")):
            continue
        digits = "".join(ch for ch in name[4:6] if ch.isdigit())
        if len(digits) != 2:
            continue
        vts_number = int(digits)
        try:
            data = fs.getfile(f"{ifo_root}/{name}").read()
        except (DiscStructureError, OSError) as exc:
            structure.warnings.append(f"cannot read {name}: {exc}")
            continue
        try:
            structure.vts[vts_number] = parse_vts(data, vts_number, label=name)
        except DiscStructureError as exc:
            structure.warnings.append(str(exc))
            log.warning(f"{exc}", context="dvd")
    return structure


def analyse(structure: DvdStructure, *,
            log: LogBus | None = None) -> DiscAnalysis:
    """Turn parsed DVD structure into the engine's :class:`DiscAnalysis`."""
    log = log or LogBus()
    titles: list[Title] = []

    refs = structure.title_refs
    if not refs:
        # No VMG title table (malformed VMG): synthesise titles from VTS PGCs
        # so the user still gets something usable.
        for vts_number, vts in sorted(structure.vts.items()):
            for pgc in vts.pgcs:
                refs.append(DvdTitleRef(
                    number=len(refs) + 1, vts_number=vts_number, vts_ttn=1,
                    angles=1, ptt_count=pgc.programs,
                    pgc_number=pgc.number, vts_start_sector=0))
        if refs:
            structure.warnings.append(
                "title list rebuilt from title sets because VIDEO_TS.IFO "
                "was unreadable")

    for ref in refs:
        vts = structure.vts.get(ref.vts_number)
        if vts is None:
            structure.warnings.append(
                f"title {ref.number} references missing title set "
                f"VTS_{ref.vts_number:02d}")
            continue
        pgc_number = ref.pgc_number or _first_pgc_for_title(vts, ref)
        pgc = vts.pgc(pgc_number) if pgc_number else None
        if pgc is None:
            if not vts.pgcs:
                structure.warnings.append(
                    f"VTS_{ref.vts_number:02d} contains no program chains")
                continue
            pgc = vts.pgcs[0]
            structure.warnings.append(
                f"title {ref.number}: PGC {pgc_number} not found, using PGC "
                f"{pgc.number}")

        title = _build_title(ref, vts, pgc)
        titles.append(title)

    disc = DiscInfo(
        label=structure.label,
        media_type=MediaType.DVD_VIDEO,
        format_label="DVD-Video",
        feature_count=len(titles),
        extras={"titlesets": len(structure.vts)},
    )
    mark_main_feature(titles)
    analysis = DiscAnalysis(disc=disc, titles=titles, analyzer="dvd-ifo",
                           warnings=list(structure.warnings))
    if not titles:
        analysis.empty_reason = ("No titles could be read from this DVD's "
                                 "IFO files.")
    for warning in structure.warnings:
        log.warning(warning, context="dvd")
    return analysis


def _first_pgc_for_title(vts: VtsSet, ref: DvdTitleRef) -> int:
    """PGC that a title's first PTT points at (DVD titles map 1:1 to PGCs)."""
    title_index = max(0, ref.vts_ttn - 1)
    if title_index < len(vts.ptts) and vts.ptts[title_index]:
        return vts.ptts[title_index][0][0]
    if title_index < len(vts.pgcs):
        return vts.pgcs[title_index].number
    return vts.pgcs[0].number if vts.pgcs else 0


def _build_title(ref: DvdTitleRef, vts: VtsSet, pgc: DvdPgc) -> Title:
    attrs = vts.stream_attrs
    streams: list[StreamInfo] = []
    index = 0

    video = attrs.get("video")
    if isinstance(video, dict):
        streams.append(StreamInfo(
            index=index, kind=StreamKind.VIDEO,
            codec=str(video.get("codec", "mpeg2video")),
            codec_long="MPEG-2" if video.get("codec") == "mpeg2video" else "MPEG-1",
            width=int(video.get("width") or 720),
            height=int(video.get("height") or 480),
            fps=float(video.get("fps") or 25.0),
            source_id="e0",
            extras={"aspect": video.get("aspect"),
                    "video_format": video.get("video_format")},
        ))
        index += 1

    for entry in attrs.get("audio", []) or []:
        if not isinstance(entry, dict):
            continue
        channels = int(entry.get("channels") or 2)
        streams.append(StreamInfo(
            index=index, kind=StreamKind.AUDIO,
            codec=str(entry["codec"]), codec_long=str(entry["codec_long"]),
            language=entry.get("language"),  # type: ignore[arg-type]
            channels=channels,
            channel_layout=_dvd_channel_layout(channels),
            sample_rate=int(entry.get("sample_rate") or 48000),
            source_id=f"80{int(entry['index']):x}",
            extras={"quantisation": entry.get("quantisation")},
        ))
        index += 1

    for entry in attrs.get("subtitle", []) or []:
        if not isinstance(entry, dict):
            continue
        streams.append(StreamInfo(
            index=index, kind=StreamKind.SUBTITLE,
            codec="dvd_subtitle", codec_long="DVD Subtitle",
            language=entry.get("language"),  # type: ignore[arg-type]
            subtitle_format="vobsub",
            forced=bool(entry.get("forced")),
            source_id=f"20{int(entry['index']):x}",
        ))
        index += 1

    chapter_count = ref.ptt_count or pgc.programs or len(pgc.cells)
    times = pgc.chapter_times(chapter_count)
    chapters: list[Chapter] = []
    for i, start in enumerate(times):
        duration = (times[i + 1] - start) if i + 1 < len(times) else \
            max(0.0, pgc.duration - start)
        chapters.append(Chapter(index=i + 1, start_seconds=start,
                                duration_seconds=duration))
    if chapters and pgc.duration > chapters[-1].end_seconds:
        chapters[-1].duration_seconds = pgc.duration - chapters[-1].start_seconds

    size = sum(cell.byte_length for cell in pgc.cells) or None
    notes: list[str] = []
    if ref.angles > 1:
        notes.append(f"{ref.angles} angles")
    for problem in pgc.problems[:3]:
        notes.append(problem)

    return Title(
        id=f"vts{ref.vts_number:02d}-pgc{pgc.number}",
        index=ref.number,
        name=f"Title {ref.number}",
        duration_seconds=pgc.duration,
        chapters=chapters,
        streams=streams,
        size_bytes=size,
        angles=ref.angles,
        notes=notes,
        source_ref={
            "kind": "dvd-pgc",
            "vts": ref.vts_number,
            "pgc": pgc.number,
            "title_number": ref.number,
            "vts_ttn": ref.vts_ttn,
            "cells": [
                {"vob_id": c.vob_id, "cell_id": c.cell_id,
                 "first_sector": c.first_sector, "last_sector": c.last_sector,
                 "duration": c.duration,
                 "block_mode": c.block_mode, "block_type": c.block_type,
                 "seamless": c.seamless,
                 }
                for c in pgc.cells
            ],
        },
    )


def _dvd_channel_layout(channels: int) -> str:
    return {1: "mono", 2: "stereo", 3: "2.1", 4: "4.0", 5: "5.0",
            6: "5.1", 7: "6.1", 8: "7.1"}.get(channels, f"{channels} ch")


class DvdIfoAnalyzer:
    """``DiscAnalyzer`` for DVD-Video discs."""

    name = "dvd-ifo"

    def __init__(self, log: LogBus | None = None) -> None:
        self._log = log or LogBus()

    def can_handle(self, drive: Drive, fs: DiscFileSystem | None) -> bool:
        if fs is None:
            return False
        try:
            return fs.find_case_insensitive("VIDEO_TS/VIDEO_TS.IFO") is not None
        except Exception:
            return False

    def analyze(self, drive: Drive, fs: DiscFileSystem | None, *,
                progress=None, cancelled=None) -> DiscAnalysis:
        if fs is None:
            raise DiscStructureError("no filesystem available for this source")
        structure = load_dvd_structure(fs, log=self._log)
        structure.label = structure.label or drive.volume_label or drive.display_name
        return analyse(structure, log=self._log)
