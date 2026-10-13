"""Builders for synthetic, spec-conformant disc fixtures.

These let the whole pipeline be tested with no optical drive and no
copyrighted media.  The structures follow the same specifications the
parsers are written against, so a fixture that parses correctly here is
also accepted by ffmpeg's libdvdread/libbluray based readers (the tests
assert exactly that, which is what stops the fixture and the parser from
sharing a single misunderstanding).

Run ``python tools/make_fixtures.py <dir>`` to write the same trees to disk
for manual inspection with ffprobe.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field

SECTOR = 2048
DVD_AUDIO_ATTR = 0x204
DVD_AUDIO_ATTR_COUNT = 8
DVD_SUBPIC_ATTR = 0x256
DVD_SUBPIC_ATTR_COUNT = 32


def _be16(value: int) -> bytes:
    return struct.pack(">H", value & 0xFFFF)


def _be32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def _bcd(value: int) -> int:
    return ((value // 10) << 4) | (value % 10)


def _playback_time(seconds: float, frame_rate: float = 29.97) -> bytes:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    frames = int(round((seconds - total) * frame_rate))
    rate_bits = 0b11 if frame_rate > 26 else 0b01
    frame_byte = (rate_bits << 6) | (_bcd(frames // 10) << 4) | _bcd(frames % 10)
    return bytes([_bcd(hours), _bcd(minutes), _bcd(secs), frame_byte])


def ffmpeg_binary() -> str:
    """Locate ffmpeg: PATH first, then the dev-only ``static-ffmpeg`` wheel."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
        found = shutil.which("ffmpeg")
    except Exception:
        found = None
    if not found:
        raise RuntimeError("ffmpeg is required to build media fixtures")
    return found


def _insert(buf: bytearray, offset: int, data: bytes) -> None:
    buf[offset:offset + len(data)] = data


def nav_pack(pack_header: bytes = b"\x00\x00\x01\xba\x44\x00\x00\x04\x00\x04"
                                         b"\x01\x00\x00\x03\xf8") -> bytes:
    """One sector-sized DVD navigation pack (``private_stream_2`` / PCI).

    A real VOB starts every VOBU with one of these. ffmpeg only accepts a
    ``0x1BF`` packet as DVD navigation data when the PES length is 980 (the
    PCI structure) or 1018 (DSI); otherwise it silently skips it. The PCI
    clock fields must also read as a sane BCD time, or ffmpeg treats the
    stream as Sofdec instead of DVD.
    """
    pci = bytearray(980)
    pci[0x00] = 0                                   # PCI structure tag
    pci[0x0d:0x11] = (0).to_bytes(4, "big")         # start PTS
    pci[0x11:0x15] = (1000).to_bytes(4, "big")      # end PTS >= start
    pci[0x19], pci[0x1a], pci[0x1b] = 0x01, 0x00, 0x00   # BCD 01:00:00
    pes = b"\x00\x00\x01\xbf" + len(pci).to_bytes(2, "big") + bytes(pci)
    return pack_header + pes + b"\xff" * (SECTOR - len(pack_header) - len(pes))


def splice_nav_packs(path: str, interval: int = 32 * SECTOR) -> None:
    """Rewrite ``path`` with a nav pack in front of every ``interval`` bytes.

    ``interval`` is a multiple of the 2048-byte sector, so the result stays
    readable: ffmpeg resyncs on pack headers and, once it has seen a valid
    PCI, exposes the navigation stream as ``Data: dvd_nav_packet``.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 14:
        return
    pack_header = data[:14]
    out = bytearray()
    for start in range(0, len(data), interval):
        out += nav_pack(pack_header)
        out += data[start:start + interval]
    with open(path, "wb") as handle:
        handle.write(bytes(out))


# --------------------------------------------------------------------------- #
# DVD-Video
# --------------------------------------------------------------------------- #


@dataclass
class DvdFixtureSpec:
    """What to put inside a synthetic DVD-Video tree."""

    cell_seconds: float = 3.0
    cell_count: int = 2
    audio_streams: int = 1
    subtitle_streams: int = 1
    video_width: int = 352
    video_height: int = 288
    video_fps: float = 25.0
    labels: list[str] = field(default_factory=lambda: ["eng"])
    title_count: int = 1
    #: splice DVD navigation packs into the VOB (as a real disc does).  ffmpeg
    #: exposes them as a *data* stream, so this is what makes a fixture
    #: faithful to a real rip: the extracted media then carries one more
    #: stream than the IFO selection, at index 0.
    nav_packs: bool = False

    @property
    def duration(self) -> float:
        return self.cell_seconds * self.cell_count


def build_vob(path: str, spec: DvdFixtureSpec, *, audio_codec: str = "ac3") -> int:
    """Render a real MPEG-PS source file and return its size in bytes."""
    ffmpeg = ffmpeg_binary()
    duration = spec.duration
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size={spec.video_width}x{spec.video_height}:"
        f"rate={spec.video_fps}:duration={duration:.3f}",
    ]
    if spec.audio_streams:
        # the IFO declares 48 kHz for every stream we emit; encode to match
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency=440:sample_rate=48000:duration={duration:.3f}"]
    args += [
        "-c:v", "mpeg2video", "-b:v", "1200k",
        "-f", "vob",
    ]
    if spec.audio_streams:
        args += ["-c:a", "ac3" if audio_codec == "ac3" else "mp2",
                 "-b:a", "192k", "-ac", "2", "-ar", "48000"]
    args.append(path)
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed building VOB: {proc.stderr[-2000:]}")
    if spec.nav_packs:
        splice_nav_packs(path)
    return os.path.getsize(path)


IFO_SECTORS = 6
VTSI_LAST_SECTOR = IFO_SECTORS // 2     # 2048-byte blocks: 6 sectors -> 3? see below
VTSI_LAST_SECTOR = 5                    # 12 KiB IFO / 2048 = 6; libdvdread wants >= 1 with vtsi_last_sectors*2 <= vts_last_sector


def _title_set_ifo(spec: DvdFixtureSpec, cell_sectors: list[tuple[int, int]],
                   *, fps: float, vts_last_sector: int) -> bytes:
    """Assemble a VTS_01_0.IFO with one PGC containing ``cell_count`` cells.

    Field offsets follow vmgi/vtsi ``ifo_types.h``; the values chosen satisfy
    libdvdread's CHECK_VALUE validation so ffmpeg can read the fixture too.
    """
    ifo = bytearray(SECTOR * IFO_SECTORS)
    _insert(ifo, 0, b"DVDVIDEO-VTS")
    _insert(ifo, 0x0C, _be32(vts_last_sector))     # vts_last_sector
    _insert(ifo, 0x1C, _be32(VTSI_LAST_SECTOR))    # vtsi_last_sector
    _insert(ifo, 0x21, bytes([0x21]))              # specification version 1.0
    _insert(ifo, 0x80, _be32(0x316))               # vtsi_last_byte

    # --- VTSI_MAT stream attributes ------------------------------------- #
    video_attr = ((1 & 0x03) << 14)      # MPEG-2
    video_attr |= (0 if fps > 26 else 1) << 12   # 0=NTSC, 1=PAL
    video_attr |= (3 << 10)              # 16:9
    video_attr |= (0 << 6)               # 720x480/576
    _insert(ifo, 0x200, _be16(video_attr))

    _insert(ifo, 0x203, bytes([spec.audio_streams]))
    for i in range(spec.audio_streams):
        base = DVD_AUDIO_ATTR + i * 8
        lang = spec.labels[i % len(spec.labels)] if spec.labels else "eng"
        # coding mode 0 (AC-3), language type 1 (present)
        byte0 = (0 << 5) | (1 << 2)
        # 48 kHz (0), 16-bit (0), channels-1 = 1 (stereo)
        byte1 = (0 << 6) | (0 << 4) | (2 - 1)
        _insert(ifo, base, bytes([byte0, byte1]))
        _insert(ifo, base + 2, lang[:2].encode("ascii", "replace").ljust(2, b" "))

    _insert(ifo, 0x255, bytes([spec.subtitle_streams]))
    for i in range(spec.subtitle_streams):
        base = DVD_SUBPIC_ATTR + i * 6
        byte0 = (0 << 5) | 1          # run-length + "language" type
        _insert(ifo, base, bytes([byte0, 0]))
        _insert(ifo, base + 2, b"eng")

    # --- VTS_PGCIT at sector 1 ------------------------------------------ #
    pgcit_off = SECTOR * 1
    _insert(ifo, 0xC8, _be32(4))      # vts_ptt_srpt pointer
    _insert(ifo, 0xCC, _be32(1))      # vts_pgcit pointer
    _insert(ifo, 0xE0, _be32(3))      # vts_c_adt pointer
    _insert(ifo, 0xE4, _be32(5))      # vts_vobu_admap pointer

    pgc_start = 16                     # bytes, relative to PGCIT
    pgc = bytearray(0x160)
    pgc[0x02] = spec.cell_count
    pgc[0x03] = spec.cell_count
    _insert(pgc, 0x04, _playback_time(spec.duration, fps))
    # audio_control[0]/subp_control[0]: the 0x8000/0x80000000 top bit marks the
    # stream "present"; libdvdread otherwise requires the word to be zero.
    if spec.audio_streams:
        _insert(pgc, 0x0C, _be16(0x8000))
    if spec.subtitle_streams:
        _insert(pgc, 0x1C, _be32(0x80000000))
    program_map = 0xEC
    cell_playback = program_map + spec.cell_count
    cell_position = cell_playback + 24 * spec.cell_count
    _insert(pgc, 0xE6, _be16(program_map))
    _insert(pgc, 0xE8, _be16(cell_playback))
    _insert(pgc, 0xEA, _be16(cell_position))
    for i in range(spec.cell_count):
        pgc[program_map + i] = i + 1                     # program -> cell
        base = cell_playback + i * 24
        _insert(pgc, base + 0x04, _playback_time(spec.cell_seconds, fps))
        _insert(pgc, base + 0x08, _be32(cell_sectors[i][0]))
        _insert(pgc, base + 0x0C, _be32(cell_sectors[i][0]))
        _insert(pgc, base + 0x10, _be32(max(cell_sectors[i][1] - 1,
                                           cell_sectors[i][0])))
        _insert(pgc, base + 0x14, _be32(cell_sectors[i][1]))
        pos = cell_position + i * 4
        _insert(pgc, pos, _be16(1))                      # vob id
        pgc[pos + 3] = i + 1                             # cell id

    pgcit = bytearray(SECTOR)
    _insert(pgcit, 0, _be16(1))                          # one PGC
    _insert(pgcit, 4, _be32(pgc_start + len(pgc) - 1))   # last_byte
    _insert(pgcit, 8, bytes([0x01, 0x00]))
    _insert(pgcit, 10, _be16(0))
    _insert(pgcit, 12, _be32(pgc_start))                 # pgc_start_byte
    _insert(pgcit, pgc_start, bytes(pgc))
    _insert(ifo, pgcit_off, bytes(pgcit))

    # --- VTS_C_ADT at sector 3 ------------------------------------------ #
    c_adt = bytearray(SECTOR)
    _insert(c_adt, 0, _be16(1))                          # nr_of_vobs
    _insert(c_adt, 4, _be32(8 + 12 * spec.cell_count - 1))
    for i in range(spec.cell_count):
        base = 8 + i * 12
        _insert(c_adt, base, _be16(1))
        c_adt[base + 2] = i + 1
        _insert(c_adt, base + 4, _be32(cell_sectors[i][0]))
        _insert(c_adt, base + 8, _be32(cell_sectors[i][1]))
    _insert(ifo, SECTOR * 3, bytes(c_adt))

    # --- VTS_VOBU_ADMAP at sector 5 (mandatory) ------------------------- #
    # one VOBU start sector per coverable sector; the table can exceed a
    # single IFO sector, so it is sized accordingly and any slack sectors
    # in the IFO are true zeros (libdvdread demands exactly this).
    vobu_count = (cell_sectors[-1][1] - cell_sectors[0][0] + 1)
    vobu_sectors = -(-(4 + 4 * vobu_count) // SECTOR)
    vobu = bytearray(SECTOR * vobu_sectors)
    _insert(vobu, 0, _be32(4 + 4 * vobu_count - 1))  # last_byte
    for i in range(vobu_count):
        _insert(vobu, 4 + i * 4, _be32(cell_sectors[0][0] + i))
    _insert(ifo, SECTOR * 5, bytes(vobu))

    # --- VTS_PTT_SRPT at sector 4 --------------------------------------- #
    ptt = bytearray(SECTOR)
    _insert(ptt, 0, _be16(1))                            # one title
    _insert(ptt, 4, _be32(12 + 4 * spec.cell_count - 1))
    # ttu offsets are read as "(data + offset - 8)", so an entry physically at
    # table offset 12 must be recorded as 20 (VTS_PTT_SRPT_SIZE = 8).
    _insert(ptt, 8, _be32(12 + 8))                       # ttu_offset[0]
    for i in range(spec.cell_count):
        _insert(ptt, 12 + i * 4, _be16(1) + _be16(i + 1))
    _insert(ifo, SECTOR * 4, bytes(ptt))

    return bytes(ifo)


def _video_manager_ifo(spec: DvdFixtureSpec, *, ptt_count: int) -> bytes:
    """Assemble VIDEO_TS.IFO with a title search pointer table."""
    # sector layout: 0=VMGI_MAT, 1=VMGM_PGCI_UT, 2=TT_SRPT, 3=VTS_ATRT,
    # 4=first-play PGC (236 zero bytes - a no-op PGC is valid)
    ifo = bytearray(SECTOR * 5)
    _insert(ifo, 0, b"DVDVIDEO-VMG")
    _insert(ifo, 0x0C, _be32(10))                  # vmg_last_sector (>= 2*vmgi)
    _insert(ifo, 0x1C, _be32(4))                   # vmgi_last_sector
    _insert(ifo, 0x21, bytes([0x21]))              # specification version 1.0
    _insert(ifo, 0x26, _be16(1))                   # vmg_nr_of_volumes
    _insert(ifo, 0x28, _be16(1))                   # vmg_this_volume_nr
    _insert(ifo, 0x2A, bytes([1]))                 # disc_side: 1
    _insert(ifo, 0x3E, _be16(1))                   # vmg_nr_of_title_sets
    _insert(ifo, 0x80, _be32(SECTOR * 5 - 1))       # vmgi_last_byte (file size - 1)
    # first_play_pgc: BYTE offset into the file itself (DVDFileSeek_ + read)
    _insert(ifo, 0x84, _be32(SECTOR * 4))
    _insert(ifo, 0xC4, _be32(2))                   # tt_srpt pointer -> sector 2
    _insert(ifo, 0xD0, _be32(3))                   # vts_atrt pointer -> sector 3

    # --- VMGM_PGCI_UT: one language unit, one FP PGC ---------------------- #
    # pgci_ut_t: nr_of_lus=1, last_byte, then per-lu entry: lang_code(2),
    # lang_ext(1), exists(1), lu_start_byte(4, relative to tab start)
    lut = bytearray(SECTOR)
    _insert(lut, 0, _be16(1))
    _insert(lut, 4, _be32(0x2FF))
    _insert(lut, 8, _be16(0x0021))                 # lang 'en'
    lut[10] = 0
    lut[11] = 1
    _insert(lut, 12, _be32(16))                    # language unit at +16
    # pgcit inside the language unit: 1 PGC (the first-play PGC)
    _insert(lut, 16, _be16(1))                     # nr_of_pgci_srp
    _insert(lut, 20, _be32(8 + 8 - 1))             # last_byte
    # pgci_srp_t: entry_id, block/ptl byte, ptl_id_mask (2), pgc_start_byte (4)
    _insert(lut, 24, bytes([0x01, 0x00]))
    _insert(lut, 26, _be16(0))
    _insert(lut, 28, _be32(16))

    # pgc_t at lut+16+24 = +40: no programs, no cells, time 0
    _insert(lut, 40, bytes(236))
    _insert(ifo, SECTOR * 1, bytes(lut))

    # --- TT_SRPT at sector 2 ---------------------------------------------- #
    tt = bytearray(SECTOR)
    _insert(tt, 0, _be16(spec.title_count))
    _insert(tt, 4, _be32(8 + 12 * spec.title_count - 1))
    for i in range(spec.title_count):
        base = 8 + i * 12
        tt[base + 0] = 0x00            # pb_ty
        tt[base + 1] = 1               # nr_of_angles
        _insert(tt, base + 2, _be16(ptt_count))
        _insert(tt, base + 6, bytes([1, 1]))       # title_set_nr, vts_ttn
        _insert(tt, base + 8, _be32(1))
    _insert(ifo, SECTOR * 2, bytes(tt))

    # --- VTS_ATRT at sector 3 (mandatory in the VMG) ---------------------- #
    atrt = bytearray(SECTOR)
    _insert(atrt, 0, _be16(1))                     # nr_of_vtss
    _insert(atrt, 4, _be32(8 + 8 + 356))           # last_byte
    _insert(atrt, 8, _be32(16))                    # offset of vts_attributes[1]
    va = bytearray(356)
    _insert(va, 0, _be32(355))
    _insert(va, 4, _be32(0))                       # vts_cat
    # vtstt_vobs_video_attr / audio / subp counts we leave 0 for a valid empty set
    _insert(atrt, 16, bytes(va))
    _insert(ifo, SECTOR * 3, bytes(atrt))

    # --- First-play PGC at sector 4: all zeros = valid no-op PGC --------- #
    _insert(ifo, SECTOR * 4, bytes(236))
    return bytes(ifo)


def write_dvd_fixture(root: str, spec: DvdFixtureSpec | None = None, *,
                      build_media: bool = True) -> str:
    """Create ``<root>/VIDEO_TS`` with a valid IFO set (and a real VOB)."""
    spec = spec or DvdFixtureSpec()
    video_ts = os.path.join(root, "VIDEO_TS")
    os.makedirs(video_ts, exist_ok=True)

    vob_path = os.path.join(video_ts, "VTS_01_1.VOB")
    if build_media:
        size = build_vob(vob_path, spec)
    else:
        # text-free placeholder: enough sectors for the cell ranges to resolve
        size = SECTOR * spec.cell_count * 4
        with open(vob_path, "wb") as handle:
            handle.write(b"\x00" * size)

    total_sectors = max(1, math.ceil(size / SECTOR))
    ifo_sectors = math.ceil((0x318 + 8 * 24) / SECTOR)
    vts_last_sector = total_sectors + ifo_sectors
    per_cell = max(1, total_sectors // spec.cell_count)
    cell_sectors: list[tuple[int, int]] = []
    offset = 0
    for i in range(spec.cell_count):
        last = offset + per_cell - 1
        if i == spec.cell_count - 1:
            last = total_sectors - 1
        cell_sectors.append((offset, last))
        offset = last + 1

    with open(os.path.join(video_ts, "VTS_01_0.IFO"), "wb") as handle:
        handle.write(_title_set_ifo(spec, cell_sectors, fps=spec.video_fps,
                                    vts_last_sector=vts_last_sector))
    with open(os.path.join(video_ts, "VIDEO_TS.IFO"), "wb") as handle:
        handle.write(_video_manager_ifo(spec, ptt_count=spec.cell_count))
    return video_ts
