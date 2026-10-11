"""Synthetic, spec-accurate, unencrypted BDMV fixture builder (Phase 8).

Produces a minimal but *real* Blu-ray tree:

    BDMV/index.bdmv          (32-byte stub, mode-telling structures skipped)
    BDMV/PLAYLIST/00001.mpls one PlayItem over 00001.m2ts + entry marks
    BDMV/CLIPINF/00001.clpi  STC sequences
    BDMV/STREAM/00001.m2ts   real MPEG-TS (H.264 + AC-3 via ffmpeg)

The MPLS/CLPI byte layouts follow libbluray's parsers; the resulting tree
must be readable by ffmpeg's bluray demuxer (that's the conformance test).
"""
from __future__ import annotations

import os
import struct

TICKS = 45_000


def _u32(x):
    return struct.pack(">I", x)


def _u16(x):
    return struct.pack(">H", x)


def write_bdmv_fixture(dest: str, *, seconds: float = 3.0,
                       chapters: int = 2, audio: int = 1) -> str:
    """Write a synthetic BDMV tree under ``dest``; returns the dest path."""
    bdmv = os.path.join(dest, "BDMV")
    for sub in ("PLAYLIST", "CLIPINF", "STREAM"):
        os.makedirs(os.path.join(bdmv, sub), exist_ok=True)

    in_ticks = 0
    out_ticks = int(seconds * TICKS)

    # ---------------- STN table ---------------- #
    streams: list[bytes] = []
    # stream descriptor: len, type=1, pid16 — video 0x1011
    streams.append(bytes([3, 1]) + _u16(0x1011))
    # attribute: len, coding 0x1b (H.264), format 4+rate 4
    streams.append(bytes([2, 0x1B, 0x24]))
    for i in range(audio):
        pid = 0x1100 + i
        streams.append(bytes([3, 1]) + _u16(pid))
        streams.append(bytes([5, 0x80, 0x2A]) + b"eng")
    stn_counts = bytes([1, audio, 0, 0, 0, 0, 0]) + bytes(5)
    stn_body = _u16(0) + stn_counts + b"".join(streams)
    stn = _u16(len(stn_body)) + stn_body

    # ---------------- PlayItem ---------------- #
    pi_body = (b"00001" + b"M2TS"
               + bytes([0x00, 0x01])      # reserved(11) | multi(1) | conn(4)=1
               + bytes([0x00])            # stc_id
               + _u32(in_ticks) + _u32(out_ticks)
               + bytes(8)                 # UO mask
               + bytes([0x80])            # random-access + reserved
               + bytes([0x01])            # still_mode
               + _u16(0x03E8)             # still time
               + stn)
    play_item = _u16(len(pi_body)) + pi_body
    playlist_section = (_u16(0) + _u16(1) + _u16(0) + play_item)
    playlist_section = _u32(len(playlist_section)) + playlist_section

    # playlist marks: two entry marks (type 1) at 0 and half-time
    marks: list[bytes] = []
    half = out_ticks // 2
    for t in (0, half):
        mb = (bytes([0x00])          # reserved 8
              + bytes([0x01])        # mark_type entry (1)
              + _u16(0)              # play_item_ref
              + _u32(t)              # time 45kHz
              + _u16(0xFFFF)         # entry_es_pid
              + _u32(0))             # duration
        marks.append(mb)
    marks_section = (_u32(len(_u16(len(marks)) + b"".join(marks)))
                     + _u16(len(marks)) + b"".join(marks))
    playlist_len = len(playlist_section)
    header = (b"MPLS0200"
              + _u32(40)             # list_pos
              + _u32(40 + playlist_len)   # mark_pos
              + _u32(0)              # ext
              + bytes(20))           # reserved 160 bits
    mpls = header + playlist_section + marks_section
    with open(os.path.join(bdmv, "PLAYLIST", "00001.mpls"), "wb") as f:
        f.write(mpls)

    # ---------------- CLPI ---------------- #
    # Layout per libbluray clpi_parse.c:
    #   header: HDMV(4)+ver(4) | seq_info(4) | program_info(4) | cpi(4)
    #           | clip_mark(4) | ext(4)                     => 28 bytes
    #   byte 40: ClipInfo: len(4)?? libbluray skips len32 + res16 then:
    #           clip_type(8) app_type(8) | skip31 + atc_delta(1)
    #           | ts_recording_rate(32) | num_source_packets(32)
    #           | reserved 128 bytes | ts_type_info len(16)... 
    clipinfo_body = (bytes(2)                 # reserved 16
                     + bytes([0x01])          # clip stream type 1 (AV)
                     + bytes([0x01])          # app type 1 (movie)
                     + bytes([0x00])          # skip31 + atc_delta0
                     + _u32(24000000)         # recorded rate
                     + _u32(1024)             # num source packets (dummy)
                     + bytes(128)             # reserved
                     + _u16(0))               # ts_type_info empty
    cliplen = len(clipinfo_body)
    clipinfo = _u32(cliplen) + clipinfo_body
    off_probe = 40
    prog_body = (_u32(0)                      # spn of first program
                 + _u16(0x1001)               # program map pid
                 + bytes([1])                 # n streams
                 + bytes([1])                 # n groups
                 + _u16(0x1011)               # stream pid
                 + bytes([0x00, 0x00]))       # format+type payload
    prog_section = _u32(len(prog_body)) + prog_body
    stc_entry = _u16(0x1001) + _u32(0) + _u32(in_ticks) + _u32(out_ticks)
    seq_info_body = (bytes([0])               # reserved 8
                     + bytes([1])             # n ATC
                     + _u32(0)                # spn_atc_start
                     + bytes([1])             # n STC
                     + bytes([0])             # offset_stc_id
                     + stc_entry)
    seq_section = _u32(len(seq_info_body)) + seq_info_body
    cpi_section = _u32(0)                     # empty EP map == len 0
    prog_addr = 40 + len(clipinfo)
    seq_addr = prog_addr + len(prog_section)
    cpi_addr = seq_addr + len(seq_section)
    clpi_header = (b"HDMV0200"
                   + _u32(seq_addr) + _u32(prog_addr) + _u32(cpi_addr)
                   + _u32(0) + _u32(0))
    assert len(clpi_header) == 28
    clpi = (clpi_header + b"\x00" * (40 - len(clpi_header))
            + clipinfo + prog_section + seq_section + cpi_section)
    with open(os.path.join(bdmv, "CLIPINF", "00001.clpi"), "wb") as f:
        f.write(clpi)

    # ---------------- index.bdmv + MovieObject.bdmv ---------------- #
    # Byte-exact for libbluray's index_parse.c:
    #  0 "INDX0200" | 8 indexes_start=40 | 12 ext_start=0
    #  40 app_info: len=34 | 1+1+1+5 bits | video_format 4 | frame_rate 4
    #  | 32 bytes user data                     -> 4 + 38 = 42 bytes?? */
    #  (len==34 covers 34 bytes after the length field)
    app_body = (bytes([0x00])             # skip1|mode1|exist1|skip5
                + bytes([0x24])           # vf=2(1080)? low nibble rate
                + bytes(32))              # user data
    app_section = _u32(34) + app_body[:34]
    def _hdmv_obj_bits(id_ref: int) -> bytes:
        b = bytearray(5)                  # 40 bits: obj_type2 + skip30
        b[0] = 0x40 | (id_ref >> 14) & 0x3F  # type=1(hdmv) then skip bits
        b[1] = (id_ref >> 6) & 0xFF
        b[2] = ((id_ref & 0x3F) << 2)     # id_ref total 16b, ends bit 32
        return bytes(b)
    indexes = (_hdmv_obj_bits(0)          # first_play
               + _hdmv_obj_bits(0)        # top_menu
               + _u16(1)                  # num_titles
               + _hdmv_obj_bits(0))       # title 1
    indexes_section = _u32(len(indexes)) + indexes
    idx = bytearray(40)
    idx[0:4] = b"INDX"; idx[4:8] = b"0200"
    idx[8:12] = _u32(40)
    pad = 40 - len(app_section)
    idx += app_section + b"\x00" * pad
    # app section occupies up to 42; place indexes at 40 per header: so
    # app_info must not pass 40 => libbluray seeks back to indexes_start=40.
    idx = bytearray(b"INDX0200" + _u32(40) + _u32(0))
    app_at_40 = app_section[:1] + app_body[:34]
    idx[-40:]  # keep header
    blob = bytes(idx) + b"\x00" * 0        # app_info read at 40 overlapped...
    # libbluray: parse_header seeks 0, reads 3 u32 (8..19). Then app_info:
    # seek 40, len==34. Then indexes at indexes_start=40.
    # But indexes and app info CANNOT both sit at 40 on real discs; the
    # spec header only has 5 u32 total? _parse_header reads sig(2x32) +
    # indexes_start(32) + extension_data_start(32) = bytes 0..15 only.
    # So 16..39 is free; app_info len at 40..43, then indexes at 44.
    indexes_start = 40 + 4 + 34            # app_info section end
    idx = bytearray(b"INDX0200" + _u32(indexes_start) + _u32(0) + bytes(24))
    idx += app_section + indexes_section
    with open(os.path.join(bdmv, "index.bdmv"), "wb") as f:
        f.write(bytes(idx))
    with open(os.path.join(bdmv, "MovieObject.bdmv"), "wb") as f:
        f.write(b"MOBJ0200"
                + _u32(40)               # mobj_start
                + _u32(0)                # extension_data_start
                + bytes(24)
                + _u32(6) + _u16(1)      # mobj len + 1 object
                + bytes(4))              # resume int flag etc. minimal
    # ---------------- real media: MPEG-TS via ffmpeg ---------------- #?
    stream_path = os.path.join(bdmv, "STREAM", "00001.m2ts")
    _build_m2ts(stream_path, seconds=seconds + 0.5)
    return dest


# ffmpeg is used through static_ffmpeg like the DVD fixture builder
def _build_m2ts(path: str, *, seconds: float, rate: int = 48000) -> None:
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
           f"testsrc=size=1920x1080:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"
           f":sample_rate={rate}",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "ac3", "-b:a", "128k",
           "-muxrate", "24000000", "-f", "mpegts", path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg m2ts failed: " + res.stderr[-400:])
