# Reference sources

The following upstream sources were used as **ground truth** while writing the
disc parsers, and are vendored verbatim under `docs/references/` so that
results are reproducible and reviewers can see exactly which byte offset was
assumed where.

| File | Upstream | Used for |
|---|---|---|
| `docs/references/libdvdread-ifo_types.h` | https://raw.githubusercontent.com/mirror/libdvdread/master/src/dvdread/ifo_types.h | Packed struct layouts: `vmgi_mat_t`, `vtsi_mat_t`, `pgcit_t`, `pgc_t`, `cell_playback_t`, `cell_position_t`, `c_adt_t`, `vobu_admap_t`, `tt_srpt_t`, `title_info_t`, `vts_ptt_srpt_t`, `audio_attr_t`, `subp_attr_t`, `video_attr_t`, `dvd_time_t` |
| `docs/references/libdvdread-ifo_read.c` | https://raw.githubusercontent.com/mirror/libdvdread/master/src/ifo_read.c | Field byte offsets, CHECK_VALUE/Zero-check validation semantics, "offset-minus-size" PTT addressing quirk, BCD playback-time decoding |
| `docs/references/libdvdread-dvd_reader.c` | https://raw.githubusercontent.com/mirror/libdvdread/master/src/dvd_reader.c | `DVDOpenFilePath` path resolution, `DVDReadBlocksPath` multi-part file semantics |
| `docs/references/libdvdread-dvd_input.c` | https://raw.githubusercontent.com/mirror/libdvdread/master/src/dvd_input.c | `file_seek`/`file_read` block-level read semantics |
| `docs/references/ffmpeg-dvdvideo.c` | FFmpeg `libavformat/dvdvideodec.c` | How the `dvdvideo` demuxer drives libdvdread/libdvdnav, its NAV-packet expectations, and its audio/subp stream setup logic (informs our `rip/extract.py` design) |
| `docs/references/libbluray` (researched) | https://github.com/xbmc/libbluray (mpls_parse.c, clpi_parse.c, mpls_parse.h, clpi_data.h) | Blu-ray MPLS/CLPI binary layout research (headings, AppInfoPlayList, PlayItem/STN tables, 45 kHz time base) — informs Phase 8 |

## The offsets that matter most

The tables below nail down the byte offsets that are non-obvious from the
spec prose and that both our parser and libdvdread agree on; they are what
`opticrip/disc/dvd.py` and `tests/synth.py` are written against.

### `vmgi_mat_t` (VIDEO_TS.IFO)

| Offset | Field |
|---|---|
| 0x000 | `vmg_identifier` (`DVDVIDEO-VMG`) |
| 0x00C | `vmg_last_sector` |
| 0x01C | `vmgi_last_sector` |
| 0x021 | `specification_version` (0x21 = 1.0) |
| 0x026 | `vmg_nr_of_volumes` |
| 0x028 | `vmg_this_volume_nr` |
| 0x02A | `disc_side` (1 or 2) |
| 0x03E | `vmg_nr_of_title_sets` |
| 0x080 | `vmgi_last_byte` |
| **0x084** | **`first_play_pgc`** — a *byte offset*, not a sector |
| 0x0C4 | `tt_srpt` (sector pointer) |
| 0x0C8 | `vmgm_pgci_ut` |
| 0x0CC | `ptl_mait` |
| **0x0D0** | **`vts_atrt`** (sector pointer; *mandatory* — libdvdread won't load a VMG without it) |
| 0x0D8 | `vmgm_c_adt` |
| 0x0DC | `vmgm_vobu_admap` |
| 0x100 | `vmgm_video_attr` |
| 0x103 | `nr_of_vmgm_audio_streams` (≤ 1) |
| 0x104 | `vmgm_audio_attr` (8×8) |
| 0x155 | `nr_of_vmgm_subp_streams` (≤ 1) |
| 0x156 | `vmgm_subp_attr` (32×6) |

### `vtsi_mat_t` (VTS_xx_0.IFO)

| Offset | Field |
|---|---|
| 0x000 | `vts_identifier` (`DVDVIDEO-VTS`) |
| 0x00C | `vts_last_sector` |
| 0x01C | `vtsi_last_sector` |
| 0x080 | `vtsi_last_byte` |
| 0x0C8 | `vts_ptt_srpt` (sector pointer) |
| **0x0CC** | **`vts_pgcit`** (sector pointer) |
| 0x0E0 | `vts_c_adt` (sector pointer) |
| **0x0E4** | **`vts_vobu_admap`** (sector pointer; *mandatory* for libdvdread) |
| 0x200 | `vts_video_attr` |
| 0x203 | `nr_of_vts_audio_streams` (≤ 8) |
| **0x204** | **`vts_audio_attr[0..7]`** (audio_attr_t, 8 bytes each) |
| 0x255 | `nr_of_vts_subp_streams` (≤ 32) |
| **0x256** | **`vts_subp_attr[0..31]`** (subp_attr_t, 6 bytes each) |
| 0x318 | `vts_mu_audio_attr[0..7]` (multichannel_ext_t, 24 bytes each) |

### `pgcit_t` and `pgc_t`

| Structure | Offset | Field |
|---|---|---|
| pgcit_t | 0x00 | `nr_of_pgci_srp` |
| pgcit_t | 0x04 | `last_byte` (must cover `pgc_start_byte + PGC_SIZE`, i.e. +236, for every entry) |
| pgci_srp_t | 0x00 | `entry_id`, `block_mode`/`block_type`/`unknown1` (bits, `unknown1` must be 0) |
| pgci_srp_t | 0x02 | `ptl_id_mask` |
| pgci_srp_t | 0x04 | `pgc_start_byte` (relative to PGCIT start, **bytes**) |
| pgc_t | 0x000 | `zero_1` |
| pgc_t | 0x002 | `nr_of_programs`, `nr_of_cells` |
| pgc_t | 0x004 | `playback_time` (dvd_time_t: HH/MM/SS BCD, `frame_u` top 2 bits = frame rate: 0b11=29.97, 0b01=25) |
| pgc_t | 0x00C | `audio_control[8]` (bit 15 = "present"; otherwise the word must be zero) |
| pgc_t | 0x01C | `subp_control[32]` (bit 31 = "present"; otherwise the word must be zero) |
| pgc_t | 0x0E6 | `program_map_offset` |
| pgc_t | 0x0E8 | `cell_playback_offset` |
| pgc_t | 0x0EA | `cell_position_offset` |
| cell_playback_t | +0x04 | `playback_time` |
| cell_playback_t | +0x08 | `first_sector` |
| cell_playback_t | +0x10 | `last_vobu_start_sector` (must satisfy `first_sector <= last_vobu_start_sector <= last_sector`) |
| cell_playback_t | +0x14 | `last_sector` |
| cell_position_t | +0x00 | `vob_id_nr`, `cell_nr` |

### `vts_ptt_srpt_t`

| Offset | Field | Note |
|---|---|---|
| 0x00 | `nr_of_srpts` | |
| 0x04 | `last_byte` | |
| 0x08 | `ttu_offset[0..n-1]` | **absolute table offsets**; libdvdread computes effective entry position as `ttu_offset[i] - 8`.|\n| `ttu_offset[i]` | ptt entries (pgcn/pgn pairs) | start at the value stored in `ttu_offset[i] - 8` |

### Bit-field layouts that catch authoring bugs

These are the specific validation rules libdvdread enforces that OpticRip
mirrors and that commonly-encountered pirate-authored media trips over:

- `audio_control[i]`: the presence bit is `0x8000`; when absent, the whole
  16-bit word must be **zero**, not just clear the top bit.
- `subp_control[i]`: presence bit `0x80000000`; otherwise zero.
- Nonexistent `vts_audio_attr[i]` / `vts_subp_attr[i]` slots must be zeroed
  out past their declared counts.
- `vmg_nr_of_volumes` and `vmg_this_volume_nr` must both be non-zero.
- `disc_side` must be 1 or 2.
- `nr_of_angles` must be 1 ≤ n < 10.
- `stream` PTT `pgcn`/`pgn` must be ≥ 1 (zero is rejected).
