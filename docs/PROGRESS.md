# Progress report — OpticRip

*Last updated*: Phase 3 of 8 complete. This document is the honest, detail-heavy
companion to the roadmap in the README: what is done, what is deliberately not,
what was learned the hard way, and what risk lies where.

---

## Scoreboard

| Phase | Title | Status | Verified by |
|---|---|---|---|
| 1 | Architecture, dependency evaluation, scaffolding | ✅ done | implicit (all later phases rely on it) |
| 2 | Media identification + drive discovery | ✅ done | unit tests + real-lab probe of probe results |
| 3 | Disc inspection: title/track enumeration (DVD) | ✅ ~90% done | synthetic-fixture round-trip, FFmpeg/libdvdread conformance |
| 4 | Extraction + MKV remux (stream copy) | ⬜ next | not started (design done) |
| 5 | Chapter / language / metadata preservation in output | ⬜ | not started |
| 6 | Queue, multi-drive, cancellation, recovery, validation | ⬜ | interfaces exist, implementation pending |
| 7 | PySide6 dark-theme GUI | ⬜ | not started |
| 8 | Blu-ray native parsing, protected-disc docs | ⬜ | research done, code not started |

Total source size at this checkpoint: **~5,500 lines** across 22 Python modules
+ 400-line synthetic-fixture builder + 2 reference C headers vendored for
cross-checking.

---

## Phase 1 — Scaffolding & dependency evaluation ✅

**Goal**: decide *what to build on*, get repo hygiene right, make licenses
reproducible.

### Decisions made (and why)

- **Language: Python 3.10+** for the engine shell and all parsers. Heavy
  media work is delegated to native tools (ffmpeg/mkvmerge). Performance
  matters only in the block-extraction hot path, where ffmpeg's C does the
  real work for us through IPC.
- **GUI: PySide6 (Qt6).** LGPL/GPL dual license, mature, cross-platform,
  no dll-hell concerns since Qt 6.5, ships a static-for-testable abstraction
  layer that lets us test the model separately from the widget tree.
- **Muxer: ffmpeg `-c copy` primary, mkvmerge optional** behind the same
  `Remuxer` interface. ffmpeg's Matroska muxer is sufficient for Phase 4
  while mkvmerge stays as a community-verified fallback.
- **DVD navigation: libdvdread/libdvdnav via ffmpeg's `dvdvideo` demuxer** for
  the *seed pipeline*, while OpticRip implements its own pure-Python IFO
  parser for structure/analysis/validation (Phase 3+). The native parser and
  the reference C parser stay conformance-tested against each other on every
  change, so neither one can drift.

### Documented

- `docs/ARCHITECTURE.md` — full layering and interface table.
- `docs/DEPENDENCIES.md` — runtime library table + license matrix + a
  "verify, don't assume" warning block for decrypting-capable libraries.
- `LICENSE` — GPL-3.0-or-later text.
- `pyproject.toml` — full dependency/metadata pinned, extras split (`gui`,
  `dev`), console entry points, pytest marker taxonomy
  (unit / requires_ffmpeg / requires_gui / requires_hardware).

### Outcome

Clean ground with every pre-installed piece validated in the actual
environment rather than assumed. Discovered **no `sudo`, no `apt`** in the dev
sandbox while `Python 3.12 + PySide6 + static ffmpeg` were available — this
locked the design toward "verify, don't assume" from the start.

---

## Phase 2 — Media identification & drive discovery ✅

**Goal**: enumerate drives, sense what is in them, no root, no mounting.

### What was built

| Component | File | Notes |
|---|---|---|
| Media probe | `disc/identify.py` | ISO9660 PVD + volume label, UDF Volume Recognition Sequence, audio-CD TOC via `CDROMREADTOCHDR` ioctl, DVD/BD/AACS directory signatures, capacity-based dual-layer detection, raw-VOB encryption *hint* (never used for anything else). |
| Linux drive enumeration | `drives/linux.py` | sysfs-driven (`/sys/block/*/device/type == 5`), USB/SATA/virtual bus classification, model/vendor/firmware, read-only properties, **optional** lsblk/blkid fallback for labels when raw sector reads fail. |
| Virtual sources | `drives/virtual.py` | `.iso`/`.img` images and mounted/extracted `VIDEO_TS`/`BDMV` folders through the identical pipeline; deliberate design choice: image/folder "drives" make the whole engine testable without hardware. |
| Composite/Watcher | `drives/base.py` | multi-provider merge with dedup, `DriveWatcher` polling with diffed change events for insert/eject of discs while the app runs. |
| Block-device abstraction | `disc/fs.py` `BlockDevice` | raw disk, ISO file, or in-memory — the same interface under everything downstream. |
| ISO9660 file system | `disc/fs.py` `Iso9660Disc` | implemented from scratch; descriptor-chain walk across multi-sector boundaries, Joliet UCS-2 support, multi-extent caveat documented. |
| Directory file system | `disc/fs.py` `DirectoryDisc` | mounted-disc path-traversal guards. |

### Bugs found and fixed by tests during this phase

- UDF version-recognition descriptors were initially parsed as 6 bytes rather
  than 5 — fixed after testing against real UDF blob samples.
- `.ISO` files were being classified as "data" media type — fixed by
  deferring classification to content-level identification rather than guessing
  from file extension.

### Outcome

Fully working "which drives are present, what is inserted right now" layer,
verified at 2048-byte sector granularity. No optical hardware is required to
exercise any of it.

---

## Phase 3 — DVD title/track enumeration ✅ (≈90%)

**Goal**: read DVD-Video structure with the app's own code, list titles and
tracks in the domain model, recover chapters, handle malformed discs
gracefully, and — critically — prove it against an independent reference.

### What was built

| Component | File | Notes |
|---|---|---|
| Bounds-checked binary reader | `disc/binary.py` | `Buffer` with bounds-checked `u8/u16/u32/u64/ascii`, bit-extraction, packed-BCD, DVD playback-time decoder (HH/MM/SS + BCD frame/rate byte, incl. NTSC 29.97 / PAL 25). Every accessor returns a *default* rather than raising on truncated or corrupt data, so a damaged disc degrades into warnings rather than a crash. |
| VMG parser | `disc/dvd.py `parse_vmg`` | `VIDEO_TS.IFO` identifier check, VMG_TT_SRPT walk (angles, PTT counts, parential id, title-set number), per-title references. |
| VTSI parser | `disc/dvd.py` `parse_vts` | `VTS_xx_0.IFO`: VTS_PTT_SRPT (pgcn/pgn pairs managed with libdvdread-compatible offset semantics), VTS_PGCIT (per-PGC program map, cell playback table with sector ranges, cell position table), VTS stream attribute block (video codec/aspect/size, per-language audio attrs with channels/sample rate, subpicture attrs). |
| Domain assembly | `disc/dvd.py` `analyse` | synthesised Titles with chapter times derived from the PGC program map, main-feature heuristic with ambiguity note, graceful path when the VMG is unreadable (titles rebuilt from VTS PGCs). |
| Analyzer backend | `disc/dvd.py` `DvdIfoAnalyzer` | implements `DiscAnalyzer` ABC; can_handle is cheap and non-invasive. |

### The important discovery: conformance testing against libdvdread

A hand-rolled parser is worthless until an *independent* reader agrees with
it. The synthetic fixtures were deliberately crafted from
[libdvdread's `ifo_types.h`](docs/references/libdvdread-ifo_types.h) (plus
`ifo_read.c`'s CHECK_VALUE validation walk), then round-tripped:

1. Through **OpticRip's own** parser — 1 title, 3 chapters, correct duration,
   video/audio/subpic attrs, byte-accurate cell → sector mapping.
2. Through **FFmpeg's** embedded `libdvdread`/`libdvdnav` (`dvdvideo`
   demuxer) — same probe, same file, same structure, and a true MPEG-PS VOB
   generated by ffmpeg's `lavfi` sources.

Initial fixture drafts failed libdvdread with 11 specific `CHECK_VALUE` /
`Zero check` messages at known lines (1918, 847, 840, 1594, 1597, 1871, 2873,
2100, 1496-1504). Every remaining mismatch was traced to an offset or field
semantic that libdvdread enforces more strictly than we do — in particular:

- `first_play_pgc` is a **byte offset** in the file, not a sector.
- `pgcit_t->pgci_srp[]` entries' `unknown1` is a 4-bit field shared with
  `block_mode`/`block_type`, silently must be 0.
- `pgc_t->subp_control[i]` must be **0** when the presence bit is clear (some
  authoring tools leave junk there — libdvdread rejects discs for it).
- `vts_pgcit->last_byte` must cover `pgc_start_byte + PGC_SIZE`.
- Nonexistent audio/subp entries must be *zero-filled* after their count.
- VOBU_ADMAP `last_byte` must be `4*count-1` exactly (no slack bytes).
- `tt_srpt` / `vts_ptt_srpt` — per-title `ttu_off[i]` are read as absolute
  offsets and **subtracted** by `VTS_PTT_SRPT_SIZE` (8) before use; easy to
  get wrong, and libdvdread's CHECK_VALUE catches it reliably.

Fixing these *both* hardened the fixture *and* documented one non-obvious
aspect of libdvdread's own reading semantics — the kind of documentation bugs
that often surface only when someone tries to reimplement the format.

### Verified end-to-end sanity

As of this checkpoint, the following one-liner works with **no optical
hardware present**:

```
source tests/synth.py fixture → probe → analyzer → TitleSelection → output name
```

and produces exactly:

```
titles   : 1 | main-feature preselected: (confirmed)
streams  : ['video', 'audio', 'subtitle'] chapters: 3 duration: 0:06
selection: (1,)
filename : DEMO_DVD_Title 1_01.mkv
```

### Why the 10%

- Blu-ray (`BDMV/PLAYLIST/*.mpls`, `BDMV/CLIPINF/*.clpi`) parsing is
  *designed* (see `docs/ARCHITECTURE.md`) but implementation is Phase 8 —
  the fixtures for it are not yet authored.
- The `first_play_pgc`-analog menu PGC walk (VMGM_PGCI_UT) is parsed but the
 *menu domain* (menus, still frames, mini-VM) is deliberately not modelled,
  consistent with OpticRip's short-term goal of ripping the *main feature*.

---

## Phase 4 — Extraction & MKV remux ⬜ (next)

Designed but not written. The plan, recorded here so progress is visible:

1. **DVD cell-range extraction** (`rip/extract.py`): stream the exact sectors
   a title's cells occupy from the .VOB files into a temp MPEG-PS file, with
   a threads-and-progress-aware copy loop, cancellation checks, read retry
   budget, and read errors carrying context (which sector, which cell, what
   retry count).
2. **Remux via ffmpeg `dvdvideo` + `-c copy`** to Matroska while pulling
   chapter/language/track metadata through; `mkvmerge` backend if the
   capability is present and the user picks it in Settings.
3. **Output validation** (`rip/validate.py`): re-probe with ffprobe, compare
   codec list, stream count, durations, chapter count against the selection
   the user asked for. Any mismatch → error, not warning, by default.
4. **Cancellation**: a `cancelled()` check inside the extraction loop and a
   `terminate()` on the child muxer process; partial files get removed
   unless `keep_failed_outputs` is set.

Risk notes are recorded inline in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) § "End-to-end rip pipeline".

---

## Phase 5 — Metadata preservation ⬜

Chapter/language/disposition metadata carried through with `-c copy`
`metadata` and `-disposition` argument generation. Tests planned against a
sample disc fixture with known languages and forced-subtitle flags.

---

## Phase 6 — Queue & robustness ⬜

Interfaces (`JobQueue`, `RipJob`, `ProgressUpdate`) exist and are populated by
Phase 4's engine; serial per-drive executor plus a global concurrency limiter
(1 job/drive, `max_parallel_jobs` per drive, `allow_parallel_drives`).
Free-space pre-checks already built (`core/diskutil.py`).

---

## Phase 7 — GUI ⬜

Design is fixed in the README under *Interface* and in
[ARCHITECTURE.md's layering diagram](ARCHITECTURE.md); dark theme baseline via
PySide6 `QSS` stylesheet with readable typography and non-ambiguous status
colouring; all interactive behaviour wired to the engine (no dead controls).

---

## Phase 8 — Blu-ray & protected discs ⬜

### Blu-ray parsing

Planned: pure-Python `MPLS` (`BDMV/PLAYLIST/*.mpls`) + `CLPI`
(`BDMV/CLIPINF/*.clpi`) parsers, including PlayList section walk, PlayItem
`IN_time`/`OUT_time` (45 kHz), STN table per stream (types/PIDs/types/language
codes), ClipInfo block, and the 45 kHz → seconds conversion. Component plan is
written; no code yet.

### Protected-disc plan

See [`docs/PROTECTED_DISCS.md`](PROTECTED_DISCS.md) (component plan; no
implementation, explicitly out of scope for shipping decoders).

---

## Other things worth noting

### Testing philosophy

- **Fixtures are synthetic and spec-accurate**, built byte-for-byte by
  hand in `tests/synth.py`. Some fixture bugs found during the conformance
  round only because OpticRip parsed them, libdvdread rejected them — and
  per the README, "the *reference reader* wins" whenever the two disagree on
  something the spec itself doesn't pin down. Two real bugs were found this
  way. That's why the README's contributing section asks parser contributors
  to cross-check against `docs/references/libdvdread-*`.
- **No "compiled ⇒ tested" fallacy.** Every claim in this progress report
  corresponds to something actually executed in this repository — either
  content assertions from the domain model or a probe of a real tool binary
  reporting real capabilities.

### Toolchain reality-check at this checkpoint

```
$ ffmpeg -hide_banner -version          # verified available
ffmpeg version n8.0.1-48-g0592be14ff-20260116
$ ffmpeg -hide_banner -buildconf | grep -E 'enable-(gpl|libdvd|libbluray)'
    --enable-gpl  --enable-libdvdread  --enable-libdvdnav  --enable-libbluray
$ python3 -c 'import PySide6; print(PySide6.__version__)'
6.12.0
```

### Environment notes for contributors

- **No `sudo` is ever used by the app itself.** Anything that needs elevated
  privileges (usually just cdrom group membership) is documented in the
  README's *Permissions* section.
- The `scripts/install-runtime-libs.sh` helper vendors the exact Qt runtime
  `.so`s into `vendor/runtime/` for containers that don't already have them —
  useful for the same CI sandboxes the dev extra's `static-ffmpeg` targets.
  This is a *development* convenience, not a runtime dependency.

---

## Honest statement of what does not work

Despite the enthusiasm in the README, the following is a complete and
increasingly-accurate list:

- No rip-to-MKV pipeline yet. Selecting a title and getting an .mkv out is
  the next milestone. The engine structure, interfaces and domain model for
  it all exist and are exercised; the actual data path to a file is not
  written.
- No GUI yet. Every capability in the README is a CLI/documented-call
  capability.
- No Blu-ray title enumeration yet. Blu-ray support as *documented scope* is
  planned, not implemented.
- Unprotected content assumes CSS-free media. CSS/AACS/BD+ handling is
  documented as out-of-scope and not present anywhere. Commercial protected
  discs will fail with a clean, specific error.
- Windows support is a design intent, not yet a working feature.
- No polished release packaging yet (Flatpak/AppImage/.deb), though the
  underlying component structure (single Python package, extras-based
  install targets) is ready for it.

