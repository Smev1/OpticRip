"""Blu-ray (BDMV/BDAV) playlist parsing.

A title is a playlist (``PLAYLIST/xxxxx.mpls``).  Playitems reference clips in
``STREAM/xxxxx.m2ts``; the stream table (STN) supplies codec, PID and language.
This is enough to list titles exactly like the reference's playlist view and to
build a remux from the clips.  AACS/BD+ processing is not part of parsing;
:mod:`opticrip.bddecrypt` stages decrypted clips when a title is saved.
"""

from __future__ import annotations

import struct

from .discfs import DiscFS
from .model import Disc, Segment, Stream, Title

BDMV = "BDMV"
CLIP_SECONDS = 45000.0

# STN attribute coding types (CLONE_SPEC §12: "coding_type" byte, not an
# ordinal).  Keying these by ordinal silently mislabels every real disc.
_VIDEO_CODECS = {0x01: ("MPEG-2", "V_MPEG2"), 0x02: ("MPEG-1", "V_MPEG1"),
                 0x1B: ("H.264", "V_MPEG4/ISO/AVC"),
                 0xEA: ("VC-1", "V_MS/VFW/FOURCC"),
                 0x24: ("HEVC", "V_MPEGH/ISO/HEVC")}
_AUDIO_CODECS = {0x80: ("LPCM", "A_LPCM"), 0x81: ("AC-3", "A_AC3"),
                 0x82: ("DTS", "A_DTS"), 0x83: ("TrueHD", "A_TRUEHD"),
                 0x84: ("E-AC-3", "A_EAC3"), 0x85: ("DTS-HD", "A_DTS"),
                 0x86: ("DTS-HD", "A_DTS"), 0xA1: ("AC-3", "A_AC3"),
                 0xA2: ("DTS", "A_DTS")}
_VIDEO_FORMATS = {1: 480, 2: 576, 3: 480, 4: 1080, 5: 720, 6: 1080, 8: 2160}
_FRAME_RATES = {1: "23.976", 2: "24", 3: "25", 4: "29.97", 6: "59.94", 7: "50"}
_AUDIO_CHANNELS = {1: 1, 3: 2, 4: 3, 5: 4, 6: 6, 7: 8}
_AUDIO_RATES = {1: 48000, 2: 96000, 3: 192000, 4: 48000}

# PlayItem() offsets measured from the start of the item, which begins with its
# own 2-byte length: 5-byte clip name, 4-byte codec, 2 flag bytes, STC id,
# IN_time, OUT_time, 8-byte UO mask, random-access flag, still mode, still time.
# The STN table follows, after an angle block when is_multi_angle is set.
_PI_CLIP = 2
_PI_FLAGS = 11
_PI_IN = 14
_PI_OUT = 18
_PI_ANGLE_COUNT = 34
_PI_STN = 34
_PI_STN_MULTI = 36
_PI_MIN_LENGTH = 18
_LANG_NAMES = {"eng": "English", "fre": "French", "fra": "French", "ger": "German",
               "deu": "German", "spa": "Spanish", "ita": "Italian", "jpn": "Japanese",
               "chi": "Chinese", "zho": "Chinese", "kor": "Korean", "por": "Portuguese",
               "rus": "Russian", "dut": "Dutch", "nld": "Dutch", "swe": "Swedish",
               "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "pol": "Polish"}


class BdmvError(Exception):
    pass


def _u16(blob: bytes, offset: int) -> int:
    return struct.unpack_from(">H", blob, offset)[0]


def _u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from(">I", blob, offset)[0]


def _lang(blob: bytes, offset: int) -> str:
    raw = blob[offset:offset + 3]
    text = raw.decode("ascii", "ignore")
    return text if text.isalpha() else "und"


def _block(blob: bytes, pos: int, limit: int) -> tuple[int, bytes | None]:
    """Read one length-prefixed block; return (next offset, body).

    Both the stream entry and the stream attributes that follow it use this
    shape, and each length byte counts only the bytes after itself.
    """
    if pos >= limit:
        return limit, None
    length = blob[pos]
    end = pos + 1 + length
    if end > limit:
        return limit, None
    return end, blob[pos + 1:end]


def _stream_from_entry(kind: str, entry: bytes, attr: bytes) -> Stream | None:
    if len(entry) < 3:
        return None
    stream_type = entry[0]
    if stream_type == 1:                       # in-mux: PID lives here
        pid = _u16(entry, 1)
    elif stream_type in (2, 3, 4) and len(entry) >= 4:
        pid = _u16(entry, 2)                   # sub-path: subpath id, subclip id
    else:
        return None
    coding = attr[0] if attr else 0
    if kind == "video":
        name, codec_id = _VIDEO_CODECS.get(coding, (f"Video 0x{coding:02X}", ""))
        fmt = attr[1] if len(attr) > 1 else 0
        stream = Stream(kind="video", index=0, codec=name, codec_long=name,
                        codec_id=codec_id, pid=pid, output_codec=name,
                        fps=_FRAME_RATES.get(fmt & 0x0F, ""))
        stream.height = _VIDEO_FORMATS.get((fmt >> 4) & 0x0F, 0)
        return stream
    if kind == "audio":
        name, codec_id = _AUDIO_CODECS.get(coding, (f"Audio 0x{coding:02X}", ""))
        layout = attr[1] if len(attr) > 1 else 0
        language = _lang(attr, 2) if len(attr) >= 5 else "und"
        return Stream(kind="audio", index=0, codec=name, codec_long=name,
                      codec_id=codec_id, pid=pid, language=language,
                      language_name=_LANG_NAMES.get(language, ""),
                      channels=_AUDIO_CHANNELS.get((layout >> 4) & 0x0F, 0),
                      sample_rate=_AUDIO_RATES.get(layout & 0x0F, 0),
                      output_codec=name)
    if kind == "subtitle":
        language = _lang(attr, 1) if len(attr) >= 4 else "und"
        return Stream(kind="subtitle", index=0, codec="PGS", codec_long="PGS",
                      codec_id="S_HDMV/PGS", pid=pid, language=language,
                      language_name=_LANG_NAMES.get(language, ""),
                      output_codec="PGS")
    return None


def _streams_from_stn(stn: bytes, base: int, limit: int) -> list[Stream]:
    """Parse the STN table at ``base``.

    Layout: length, 2 reserved bytes, eight stream counts, four reserved bytes,
    then one entry/attribute pair per stream in count order.  Entries beyond
    video/audio/PG (IG, secondary, PIP) still have to be walked so the following
    attributes stay aligned, but they are not listed.
    """
    streams: list[Stream] = []
    counts = stn[base + 4:base + 12]
    if len(counts) < 8:
        return streams
    n_video, n_audio, n_pg = counts[0], counts[1], counts[2]
    order: list[str] = (["video"] * n_video + ["audio"] * n_audio
                        + ["subtitle"] * n_pg
                        + ["other"] * sum(counts[3:7]))
    pos = base + 16
    for kind in order:
        pos, entry = _block(stn, pos, limit)
        pos, attr = _block(stn, pos, limit)
        if entry is None or attr is None:
            break
        if kind == "other":
            continue
        stream = _stream_from_entry(kind, entry, attr)
        if stream is not None:
            streams.append(stream)
    for ordinal, stream in enumerate(streams):
        stream.index = ordinal
        stream.source_index = ordinal
    return streams


def _clip_name(raw: bytes) -> str:
    text = raw.decode("ascii", "ignore").strip("\x00").strip()
    return text or "00000"


def parse_mpls(blob: bytes) -> dict:
    if blob[:8] != b"MPLS0200":
        raise BdmvError("not a playlist")
    playlist_start = _u32(blob, 8)
    mark_start = _u32(blob, 12)
    header_items = _u16(blob, 0x28)
    if header_items > 1000:  # defensive: some BDAV variants differ
        header_items = 0
    table = blob[playlist_start:]
    if len(table) < 10:
        raise BdmvError("short playlist table")
    items = _u16(table, 6) or header_items
    pos = 10
    playitems: list[dict] = []
    for _ in range(min(items, 1000)):
        if pos + _PI_MIN_LENGTH > len(table):
            break
        length = _u16(table, pos)
        if length < _PI_MIN_LENGTH:
            break
        end = pos + 2 + length          # the length field excludes itself
        if end > len(table):
            break
        in_time = _u32(table, pos + _PI_IN)
        out_time = _u32(table, pos + _PI_OUT)
        flags = _u16(table, pos + _PI_FLAGS)
        stn_base = pos + _PI_STN
        if (flags >> 4) & 1:            # is_multi_angle
            angles = table[pos + _PI_ANGLE_COUNT] or 1
            stn_base = pos + _PI_STN_MULTI + 10 * (angles - 1)
        streams: list[Stream] = []
        if stn_base + 16 <= end:
            stn_limit = min(end, stn_base + 2 + _u16(table, stn_base))
            streams = _streams_from_stn(table, stn_base, stn_limit)
        playitems.append({
            "clip": _clip_name(table[pos + _PI_CLIP:pos + _PI_CLIP + 5]),
            "in": in_time / CLIP_SECONDS,
            "out": out_time / CLIP_SECONDS,
            "duration": max(0.0, (out_time - in_time) / CLIP_SECONDS),
            "streams": streams,
        })
        pos = end
    chapters: list[float] = []
    if mark_start and mark_start + 6 <= len(blob):
        # PlaylistMark(): 4-byte length, 2-byte count, then 14-byte marks of
        # reserved byte, mark_type (1 = chapter), playitem id, 45 kHz
        # timestamp, entry PID and duration.
        count = _u16(blob, mark_start + 4)
        # Marks sit on the playlist timeline, which starts at the first
        # playitem's IN_time; chapter files need them rebased to zero.
        offset = playitems[0]["in"] if playitems else 0.0
        pos = mark_start + 6
        for _ in range(min(count, 1000)):
            if pos + 14 > len(blob):
                break
            mark_type = blob[pos + 1]
            timestamp = _u32(blob, pos + 4)
            pos += 14
            if mark_type == 1:
                chapters.append(max(0.0, round(timestamp / CLIP_SECONDS - offset, 3)))
    return {"playitems": playitems, "chapters": chapters}


def parse_disc(fs: DiscFS, min_length: int = 0, message=None) -> Disc:
    base = BDMV if fs.exists(f"{BDMV}/index.bdmv") else "BDAV"
    playlist_dir = f"{base}/PLAYLIST"
    clip_dir = f"{base}/STREAM"
    try:
        names = [n for n in fs.listdir(playlist_dir) if n.lower().endswith(".mpls")]
    except OSError as exc:
        raise BdmvError(str(exc)) from exc
    disc = Disc(kind="Blu-ray")
    for name in sorted(names):
        try:
            blob = fs.read(f"{playlist_dir}/{name}")
            parsed = parse_mpls(blob)
        except (OSError, BdmvError, struct.error):
            continue
        items = [i for i in parsed["playitems"] if i["duration"] > 0]
        if not items:
            continue
        duration = sum(i["duration"] for i in items)
        if min_length and duration < min_length:
            if message:
                message(3025, name, int(duration), min_length)
            continue
        streams: list[Stream] = []
        seen: set[tuple[str, int]] = set()
        for item in items:
            for stream in item["streams"]:
                key = (stream.kind, stream.pid)
                if key in seen:
                    continue
                seen.add(key)
                streams.append(stream)
        for ordinal, stream in enumerate(streams):
            stream.index = ordinal
            stream.source_index = ordinal
        size = 0
        segments: list[Segment] = []
        for item in items:
            clip = f"{clip_dir}/{item['clip']}.m2ts"
            try:
                size += fs.size(clip)
            except OSError:
                pass
            segments.append(Segment(path=clip, start=item["in"], duration=item["duration"]))
        title = Title(
            index=len(disc.titles),
            name=name.rsplit(".", 1)[0],
            source="bluray",
            duration=duration,
            size_bytes=size,
            chapter_times=sorted({round(c, 3) for c in parsed["chapters"]}),
            streams=streams,
            segments=segments,
            playlist=name,
            original_id=name.rsplit(".", 1)[0],
        )
        if message:
            message(3028, title.index + 1, len(segments), title.duration_text)
        disc.titles.append(title)
    disc.title_count = len(disc.titles)
    disc.encrypted = fs.exists(f"{base}/AACS") or fs.exists("AACS")
    disc.protection = "AACS" if disc.encrypted else "None"
    disc.size_bytes = sum(t.size_bytes for t in disc.titles)
    return disc


def looks_like_bluray(fs: DiscFS) -> bool:
    return fs.exists(f"{BDMV}/index.bdmv") or fs.exists("BDAV/info.bdav")
