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

_VIDEO_CODECS = {1: ("MPEG-2", "V_MPEG2"), 2: ("H.264", "V_MPEG4/ISO/AVC"),
                 3: ("VC-1", "V_MS/VFW/FOURCC"), 5: ("HEVC", "V_MPEGH/ISO/HEVC")}
_AUDIO_CODECS = {1: ("LPCM", "A_LPCM"), 2: ("AC-3", "A_AC3"), 3: ("DTS", "A_DTS"),
                 4: ("DTS-HD", "A_DTS"), 5: ("E-AC-3", "A_EAC3"),
                 6: ("TrueHD", "A_TRUEHD")}
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


def _streams_from_stn(stn: bytes, base: int) -> list[Stream]:
    streams: list[Stream] = []
    counts = stn[base + 4:base + 12]
    if len(counts) < 8:
        return streams
    n_video, n_audio, n_pg = counts[0], counts[1], counts[2]
    pos = base + 12
    order: list[str] = (["video"] * n_video + ["audio"] * n_audio
                        + ["subtitle"] * n_pg)
    for kind in order:
        if pos + 4 > len(stn):
            break
        length = stn[pos]
        stream_type = stn[pos + 1]
        pid = _u16(stn, pos + 2)
        entry = stn[pos:pos + length]
        pos += length
        if length < 4:
            continue
        if kind == "video":
            coding = entry[4] if len(entry) > 4 else 0
            name, codec_id = _VIDEO_CODECS.get(coding, ("Video", ""))
            streams.append(Stream(kind="video", index=0, codec=name, codec_long=name,
                                  codec_id=codec_id, pid=pid, output_codec=name))
        elif kind == "audio":
            coding = entry[4] if len(entry) > 4 else 0
            name, codec_id = _AUDIO_CODECS.get(coding, ("Audio", ""))
            language = _lang(entry, 6) if len(entry) >= 9 else "und"
            layout = entry[5] if len(entry) > 5 else 0
            channels = {1: 1, 3: 2, 4: 3, 5: 4, 6: 6, 7: 8}.get((layout >> 4) & 0x0F, 0)
            streams.append(Stream(kind="audio", index=0, codec=name, codec_long=name,
                                  codec_id=codec_id, pid=pid, language=language,
                                  language_name=_LANG_NAMES.get(language, ""),
                                  channels=channels, output_codec=name))
        else:
            language = _lang(entry, 4) if len(entry) >= 7 else "und"
            streams.append(Stream(kind="subtitle", index=0, codec="PGS",
                                  codec_long="PGS", codec_id="S_HDMV/PGS",
                                  pid=pid, language=language,
                                  language_name=_LANG_NAMES.get(language, ""),
                                  output_codec="PGS"))
    for ordinal, stream in enumerate(streams):
        stream.index = ordinal
        stream.source_index = ordinal
    return streams


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
        if pos + 22 > len(table):
            break
        length = _u16(table, pos)
        if length < 22:
            break
        clip_id = _u16(table, pos + 2)
        in_time = _u32(table, pos + 6)
        out_time = _u32(table, pos + 10)
        stn_base = pos + 26
        streams: list[Stream] = []
        if stn_base + 12 <= len(table) and stn_base < pos + length:
            streams = _streams_from_stn(table, stn_base)
        playitems.append({
            "clip": clip_id,
            "in": in_time / CLIP_SECONDS,
            "out": out_time / CLIP_SECONDS,
            "duration": max(0.0, (out_time - in_time) / CLIP_SECONDS),
            "streams": streams,
        })
        pos += length
    chapters: list[float] = []
    if mark_start and mark_start < len(blob):
        count = _u16(blob, mark_start)
        pos = mark_start + 4
        for _ in range(min(count, 1000)):
            if pos + 14 > len(blob):
                break
            mark_type = blob[pos]
            timestamp = _u32(blob, pos + 4)
            pos += 14
            if mark_type == 1:
                chapters.append(timestamp / CLIP_SECONDS)
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
            clip = f"{clip_dir}/{item['clip']:05d}.m2ts"
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
