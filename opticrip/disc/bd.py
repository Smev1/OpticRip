"""Blu-ray disc navigation structures: MPLS + CLPI parsers (Phase 8).

Pure-Python, big-endian, no dependencies.  Byte layouts follow libbluray's
``src/libbluray/bdnav/mpls_parse.c`` / ``clpi_parse.c`` (see
``docs/references`` and docs/PROGRESS.md for provenance):

* MPLS  — ``BDMV/PLAYLIST/<NNNNN>.mpls``: header (5 offsets) then at
  ``list_pos`` a PlayList section of PlayItems; each PlayItem carries the
  clip id, IN/OUT times at 45 kHz, and an STN stream table per stream kind.
* CLPI  — ``BDMV/CLIPINF/<NNNNN>.clpi``: header ``HDMV`` with section
  offsets; the SequenceInfo section contains ATC/STC sequences with the
  SPN + presentation start/end times of each clip's continuous span.

All timestamps are 45 kHz ticks; ``ticks_to_seconds`` is the single
conversion point.

Protected-content note: these structures are the *navigation* metadata of a
disc, exactly like DVD IFOs.  Commercial Blu-ray streams themselves are
typically CSS-style protected; when the underlying M2TS cannot be read (or
is scrambled) OpticRip reports
:class:`~opticrip.core.errors.EncryptedDiscError` rather than attempting
any circumvention — see docs/PROTECTED_DISCS.md.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from ..core.errors import DiscStructureError

TICKS_HZ = 45_000  # Blu-ray time base: 45 kHz


def ticks_to_seconds(ticks: int) -> float:
    return ticks / TICKS_HZ


@dataclass
class BdStreamEntry:
    """One STN_table entry (a stream within a PlayItem)."""

    kind: str          # "video"|"audio"|"pg"|"ig"|"sec-audio"|"sec-video"
    stream_type: int
    pid: int
    coding_type: int
    language: str | None = None
    format: int | None = None
    rate: int | None = None


@dataclass
class BdPlayItem:
    clip_id: str                       # 5-char clip information file name
    clip_codec: str                    # "M2TS"
    connection_condition: int
    is_multi_angle: bool
    in_time_ticks: int
    out_time_ticks: int
    stn: list[BdStreamEntry] = field(default_factory=list)
    stc_spn_start: int | None = None   # from CLPI, filled by the analyzer

    @property
    def duration_ticks(self) -> int:
        return max(0, self.out_time_ticks - self.in_time_ticks)

    @property
    def duration_seconds(self) -> float:
        return ticks_to_seconds(self.duration_ticks)


@dataclass
class BdPlayList:
    path: str
    name: str                          # e.g. "00000.mpls"
    play_items: list[BdPlayItem] = field(default_factory=list)
    marks: list[dict] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return sum(pi.duration_seconds for pi in self.play_items)


# --------------------------------------------------------------------------- #
# MPLS
# --------------------------------------------------------------------------- #

def _parse_mpls_header(data: memoryview):
    if bytes(data[:4]) != b"MPLS":
        raise DiscStructureError("MPLS: bad type indicator")
    version = bytes(data[4:8])
    if version[:4] not in (b"0200", b"0100"):
        raise DiscStructureError(f"MPLS: unsupported version {version!r}")
    list_pos, mark_pos, ext_pos = struct.unpack_from(">III", data, 8)
    return list_pos, mark_pos


def _read_stream_entry(kind: str, sec: memoryview, off: int):
    """Read one STN stream entry: (entry, new_offset)."""
    s_len = sec[off]; off += 1
    s_start = off
    stream_type = sec[off]; off += 1
    if stream_type == 1:
        pid = struct.unpack_from(">H", sec, off)[0]; off += 2
    elif stream_type in (2, 4):
        off += 2                                  # subpath_id, subclip_id
        pid = struct.unpack_from(">H", sec, off)[0]; off += 2
    elif stream_type == 3:
        off += 1                                  # subpath_id
        pid = struct.unpack_from(">H", sec, off)[0]; off += 2
    else:
        pid = 0
        off = s_start + s_len
    a_len = sec[off]; off += 1
    a_start = off
    coding = sec[off]; off += 1
    language = fmt = rate = None
    if coding in (0x01, 0x02, 0xEA, 0x1B):        # video codecs
        v = sec[off]
        fmt, rate = (v >> 4) & 0xF, v & 0xF
        off += 1
    elif (coding in (0x03, 0x04, 0xA1, 0xA2)
          or 0x80 <= coding <= 0x86):             # audio
        v = sec[off]
        fmt, rate = (v >> 4) & 0xF, v & 0xF
        off += 1
        language = bytes(sec[off:off + 3]).decode("ascii", "replace")
        off += 3
    elif coding in (0x90, 0x91):                  # PG / IG presentation
        language = bytes(sec[off:off + 3]).decode("ascii", "replace")
        off += 3
    elif coding == 0x92:                          # text subtitle
        off += 1                                  # character code
        language = bytes(sec[off:off + 3]).decode("ascii", "replace")
        off += 3
    off = a_start + a_len                         # skip any unknown tail
    return BdStreamEntry(kind=kind, stream_type=stream_type, pid=pid,
                         coding_type=coding, language=language,
                         format=fmt, rate=rate), off


def _parse_stn_table(sec: memoryview, off: int):
    """Parse the STN table: returns (entries, end_offset)."""
    (length,) = struct.unpack_from(">H", sec, off); off += 2
    table_end = off + length
    off += 2                                      # reserved
    counts = dict(zip(
        ("video", "audio", "pg", "ig", "sec-audio", "sec-video", "sec-tail"),
        struct.unpack_from(">BBBBBBB", sec, off)))
    counts.pop("sec-tail")
    off += 7 + 5                                  # counts + reserved 40 bits
    entries: list[BdStreamEntry] = []
    for kind, count in counts.items():
        for _ in range(count):
            entry, off = _read_stream_entry(kind, sec, off)
            entries.append(entry)
    return entries, table_end


def parse_mpls(data: bytes, *, path: str = "", name: str = "") -> BdPlayList:
    data = bytes(data)
    mv = memoryview(data)
    list_pos, mark_pos = _parse_mpls_header(mv)
    result = BdPlayList(path=path, name=name)

    (length,) = struct.unpack_from(">I", mv, list_pos)
    section_end = list_pos + 4 + length
    off = list_pos + 4 + 2                        # length + reserved16
    (n_items,) = struct.unpack_from(">H", mv, off); off += 2
    (n_sub,) = struct.unpack_from(">H", mv, off); off += 2

    for _ in range(n_items):
        (pi_len,) = struct.unpack_from(">H", mv, off); pi_start = off; off += 2
        clip_id = bytes(mv[off:off + 5]).decode("ascii", "replace"); off += 5
        clip_codec = bytes(mv[off:off + 4]).decode("ascii", "replace"); off += 4
        # reserved(11) | multi_angle(1) | conn(4) span 2 bytes (libbluray)
        is_multi_angle = bool(mv[off] & 0x38) >> 3 if False else \
            bool((mv[off + 1] >> 4) & 0x01)
        conn = mv[off + 1] & 0x0F
        off += 2
        stc_id = mv[off]; off += 1
        in_time, out_time = struct.unpack_from(">II", mv, off); off += 8
        off += 8                                  # UO masks 64 bits
        off += 1                                  # random-access + reserved
        still_mode = mv[off]; off += 1
        if still_mode == 1:
            off += 2                              # still time
        if is_multi_angle:
            # angle block: count 8, packed flags 8, then clip_id+codec+stc
            # (40+32+8) == 80 bits per additional angle (libbluray layout)
            n_ang = mv[off]; off += 2
            off += n_ang * 9
        while off < pi_start + 2 + pi_len:
            entries, off = _parse_stn_table(mv, off)
            result.play_items.append(BdPlayItem(
                clip_id=clip_id, clip_codec=clip_codec,
                connection_condition=conn, is_multi_angle=is_multi_angle,
                in_time_ticks=in_time, out_time_ticks=out_time,
                stn=entries))
            break
        off = pi_start + 2 + pi_len
    # playlist marks (chapter-like menu marks)
    try:
        (m_len,) = struct.unpack_from(">I", mv, mark_pos)
        m_end = min(mark_pos + 4 + m_len, len(mv))
        mo = mark_pos + 4
        (n_marks,) = struct.unpack_from(">H", mv, mo) if m_end - mo >= 2 \
            else (0,)
        mo += 2
        if not result.play_items:   # mark times need in_times not yet parsed
            pass
        for _ in range(n_marks):
            if mo + 14 > m_end:
                break
            mo += 1                                  # reserved 8
            mark_type = mv[mo]; mo += 1
            (pi_index,) = struct.unpack_from(">H", mv, mo); mo += 2
            (t, pid16, _dur) = struct.unpack_from(">IHI", mv, mo); mo += 10
            result.marks.append({"type": int(mark_type & 0x01),
                                 "play_item": pi_index,
                                 "time_ticks": t})
    except (struct.error, IndexError):
        pass                                       # marks are optional
    return result


# --------------------------------------------------------------------------- #
# CLPI
# --------------------------------------------------------------------------- #

@dataclass
class BdStcSequence:
    pcr_pid: int
    spn_stc_start: int
    start_ticks: int
    end_ticks: int


@dataclass
class BdClipInfo:
    path: str
    stc_sequences: list[BdStcSequence] = field(default_factory=list)


def parse_clpi(data: bytes, *, path: str = "") -> BdClipInfo:
    """Parse the sequence-info (ATC/STC) section of a CLPI file."""
    data = bytes(data)
    mv = memoryview(data)
    if bytes(mv[:4]) != b"HDMV":
        raise DiscStructureError("CLPI: bad type indicator (want HDMV)")
    version = bytes(mv[4:8])
    if version[:4] not in (b"0200", b"0100"):
        raise DiscStructureError(f"CLPI: unsupported version {version!r}")
    off = struct.unpack_from(">I", mv, 8)[0]    # sequence_info_start_addr
    result = BdClipInfo(path=path)
    (length,) = struct.unpack_from(">I", mv, off); off += 4
    section_end = off + length
    off += 1                                     # reserved 8 bits
    (n_atc,) = struct.unpack_from(">B", mv, off); off += 1
    for _ in range(n_atc):
        (spn_atc_start, n_stc) = struct.unpack_from(">IB", mv, off); off += 5
        off += 1                                 # offset_stc_id
        for _ in range(n_stc):
            (pcr_pid, spn_stc, t_start, t_end) = struct.unpack_from(
                ">HIII", mv, off)
            off += 14
            result.stc_sequences.append(BdStcSequence(
                pcr_pid=pcr_pid, spn_stc_start=spn_stc,
                start_ticks=t_start, end_ticks=t_end))
    return result


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #

from dataclasses import replace as dc_replace  # noqa: E402

from ..core.logging_bus import LogBus  # noqa: E402
from ..core.models import (  # noqa: E402
    Chapter,
    DiscAnalysis,
    DiscInfo,
    MediaType,
    StreamInfo,
    StreamKind,
    Title,
    mark_main_feature,
)


class BluRayAnalyzer:
    """``DiscAnalyzer`` for Blu-ray (BDMV) folder/ISO sources."""

    name = "bd-mpls"

    def __init__(self, log: LogBus | None = None) -> None:
        self._log = (log or LogBus()).bind("bd")

    def can_handle(self, drive, fs) -> bool:
        if fs is None:
            return False
        try:
            return fs.find_case_insensitive("BDMV/index.bdmv") is not None
        except Exception:
            return False

    # -- helpers ------------------------------------------------------- #
    @staticmethod
    def _playlist_dirs(fs):
        for base in ("BDMV/PLAYLIST", "PLAYLIST"):
            try:
                names = fs.listdir(base)
            except Exception:
                continue
            return base, [n for n in names if n.lower().endswith(".mpls")]
        return None, []

    @staticmethod
    def _clip_file(fs, clip_id: str) -> bytes | None:
        for base in ("BDMV/CLIPINF", "CLIPINF"):
            try:
                return fs.getfile(f"{base}/{clip_id}.clpi").read()
            except Exception:
                continue
        return None

    def analyze(self, drive, fs, *, progress=None, cancelled=None
                ) -> DiscAnalysis:
        if fs is None:
            raise DiscStructureError("no filesystem available for this source")
        base, names = self._playlist_dirs(fs)
        if not names:
            raise DiscStructureError(
                "no .mpls playlists under BDMV/PLAYLIST",
                hint="Unencrypted Blu-ray folder sources only; UDF ISO "
                     "images are handled by ffmpeg/libbluray.")
        titles: list[Title] = []
        label = (getattr(drive, "volume_label", "") or "").strip() \
            or os.path.basename(os.path.normpath(getattr(fs, "root", "") or "."))
        video_codecs = {0x01: "mpeg2video", 0x02: "h261",
                        0xEA: "vc1", 0x1B: "h264"}
        audio_codecs = {0x80: "ac3", 0x81: "eac3", 0x82: "dts",
                        0x83: "truehd", 0x84: "truehd", 0x85: "dts",
                        0x86: "dts", 0xA1: "aac", 0xA2: "aac"}
        kind_map = {
            "video": (StreamKind.VIDEO, video_codecs),
            "audio": (StreamKind.AUDIO, audio_codecs),
            "pg": (StreamKind.SUBTITLE, None),
            "ig": (StreamKind.SUBTITLE, None),
        }
        for name in sorted(names):
            m = parse_mpls(fs.getfile(f"{base}/{name}").read(),
                           path=f"{base}/{name}", name=name)
            clips: dict[str, BdClipInfo] = {}
            for pi in m.play_items:
                raw = self._clip_file(fs, pi.clip_id)
                if raw:
                    clips[pi.clip_id] = parse_clpi(raw, path=pi.clip_id)
            streams: list[StreamInfo] = []
            for pi in m.play_items:
                for entry in pi.stn:
                    if entry.kind not in kind_map:
                        continue
                    kind, codec_map = kind_map[entry.kind]
                    codec = (codec_map.get(entry.coding_type, "unknown")
                             if codec_map else
                             ("hdmv_pgs_subtitle" if entry.kind == "pg"
                              else "hdmv_ig_subtitle"))
                    streams.append(StreamInfo(
                        index=len(streams) + 1, kind=kind, codec=codec,
                        language=entry.language,
                        source_id=f"{pi.clip_id}:{entry.pid}"))
            chapters: list[Chapter] = []
            for mk in m.marks:
                if mk["type"] == 1:   # entry mark == chapter point
                    chapters.append(Chapter(
                        index=len(chapters) + 1,
                        start_seconds=ticks_to_seconds(mk["time_ticks"])))
            title = Title(
                id=f"bd-{name}", index=len(titles) + 1,
                name=os.path.splitext(name)[0],
                duration_seconds=m.duration_seconds, chapters=chapters,
                streams=streams, notes=["blu-ray-playlist"],
                source_ref={"kind": "bd-playlist", "playlist": name})
            titles.append(title)
            self._log.info(f"playlist {name}: {len(m.play_items)} play "
                           f"item(s), {len(chapters)} entry mark(s), "
                           f"{len(streams)} stream(s)")
        mark_main_feature(titles)
        disc = DiscInfo(label=label, media_type=MediaType.BLURAY,
                        feature_count=len(titles))
        analysis = DiscAnalysis(disc=disc, titles=titles, analyzer=self.name)
        self._log.info(f"analysis complete: {len(titles)} playlist title(s)")
        return analysis
