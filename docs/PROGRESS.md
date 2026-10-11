# Progress report — OpticRip

*Last updated*: Phases 7–8 complete (1–8 all core-done). This document is the honest, detail-heavy
companion to the roadmap in the README: what is done, what is deliberately not,
what was learned the hard way, and what risk lies where.

---

## Scoreboard

| Phase | Title | Status | Verified by |
|---|---|---|---|
| 1 | Architecture, dependency evaluation, scaffolding | ✅ done | implicit (all later phases rely on it) |
| 2 | Media identification + drive discovery | ✅ done | unit tests + real-lab probe of probe results |
| 3 | Disc inspection: title/track enumeration (DVD) | ✅ ~90% done | synthetic-fixture round-trip, FFmpeg/libdvdread conformance |
| 4 | Extraction + MKV remux (stream copy) | ✅ done | real MKV end-to-end + 38-test suite |
| 5 | Chapter / language / metadata preservation in output | ✅ core behaviours done | 47-test suite (skew, angles, forced subs) |
| 6 | Queue, multi-drive, cancellation, recovery, validation | ✅ done | 53-test suite + live CLI batch e2e |
| 7 | PySide6 dark-theme GUI | ✅ core done | 4 headless Qt tests + live offscreen smoke |
| 8 | Blu-ray MPLS/CLPI parsing + extractor, protected-disc policy | ✅ core done | 10-test BDMV suite + libbluray conformance + real pipeline rip |

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

## Phase 4 — Extraction & MKV remux ✅

Implemented and verified end-to-end. The shipped engine:

| Component | File | What it does |
|---|---|---|
| Cell-range extractor | `rip/dvdcell.py` | OpticRip's own extraction path: copies exactly the 2048-byte sector ranges a title's PGC cells occupy (from our own IFO parser) out of the `VTS_xx_y.VOB` part files into a local MPEG-PS file. Retries (3 attempts/chunk with backoff), cooperative cancel mid-copy, honest error attribution (path + sector + attempt). Chosen over driving libdvdnav because it has no menu-VM dependency and works on partially-broken discs. |
| ffmpeg dvdvideo extractor | `rip/extract.py` | Alternative path via ffmpeg's `dvdvideo` demuxer (used for raw devices/images where folder paths do not exist). Failure classification maps raw stderr to specific errors (protection markers → `EncryptedDiscError`, permission denials → `DriveAccessError`, etc.). |
| FFmpegRemuxer | `rip/ffmpeg.py` | `-c copy` remux to Matroska. Per-track language metadata (`-metadata:s:a:0 language=eng`), chapter markers injected as ffmetadata (MPEG-PS cannot carry chapters, so the parsed chapter start times are re-injected). Input stream count is probed so authored-but-absent streams (empty VOB subtitle tracks) are skipped **with a log line, never silently**. `.part` temp file with atomic `os.replace`. |
| MkvmergeRemuxer | `rip/ffmpeg.py` | Optional MKVToolNix backend behind the same interface, selectable from Settings (`auto` prefers ffmpeg). |
| FfprobeValidator | `rip/validate.py` | Re-probes the output: container, streams (kinds + declared languages), chapter count, duration; compares against what was *actually mapped* (so authored-absent tracks do not turn into false failures). Issues are data, not exceptions. |
| RipEngine | `rip/engine.py` | Orchestrates analyse→extract→remux→validate per job: overwrite policies (`ask` propagates OutputExistsError to the caller; `skip` short-circuits untouched; `overwrite` replaces), free-space pre-check, temp-dir cleanup, cooperative cancellation state machine. |

### Verified by tests (38 passing)

- `test_pipeline.py::test_rip_produces_valid_mkv` — fixture → MKV, then
  validated **independently** with ffprobe (container=matroska, video+audio
  present, language `eng`, 3 chapters, duration ≈ 6 s).
- `test_no_reencoding` — codec/geometry/rate identity is asserted through the
  whole chain (mpeg2video 352×288, ac3 48 kHz). **This test caught a real
  fixture bug**: the synthetic disc declared 48 kHz in its IFO while ffmpeg
  encoded the sine at 44.1 kHz. The fixture now matches its own declaration —
  exactly the kind of lie a hand-built fixture can tell when unverified.
- `test_cancel_produces_no_output` — cancel before work → `Cancelled`, and
  no output file exists.
- `test_overwrite_ask_raises` / `test_overwrite_skip_marks_skipped` — policy
  behaviours, including "must not touch the existing file" for `skip`.
- `test_job_failure_is_reported_not_raised` — the engine surfaces recoverable
  errors as data on the job, not exceptions, so a GUI can show them calmly.

### Honest limitations of Phase 4

- Cell-level extraction from a *raw device* (unmounted ISO) is not wired
  through `DiscFileSystem` yet; a raw drive currently uses the
  `FfmpegDvdExtractor` path (libdvdnav), which covers it.
- Chapter fidelity uses our parsed times; validation against libdvdnav's own
  chapter description (and handling titles whose first VOBU starts above
  0 s) is Phase 5 work.
- The CLI rips one job per invocation; batch/multi-title queue behaviour is
  Phase 6.

## Phase 5 — Chapter / language / metadata preservation ✅ (core behaviours)

Shipped in this phase, each verified by a test:

| Behaviour | Implementation | Verified by |
|---|---|---|
| **Chapter base-skew shift** | `rip/engine.py: normalise_chapter_times()` — when the PGC's first chapter starts above 0 s (menu/angle padding before the first VOBU), chapter times shift so chapter 1 begins at 0 in the output, with a logged reason. | `test_chapters_zero_based_in_output` (pipeline; skew fixture) + unit tests |
| **Angle-aware cell selection** | `rip/dvdcell.py: _select_angle_cells()` — multi-angle PGCs are stored as repeating cell blocks; the extractor picks the one block-member matching the requested angle and drops the rest, so the output has one angle rather than interleaved garbage. Blocks are parsed from `cell_playback_t` `block_mode`/`block_type` (Phase 3 work reused). | `tests/test_phase5.py::TestAngleSelection` (4 unit tests incl. invalid-angle rejection) |
| **Forced-subtitle flag** | `disc/dvd.py` reads the subpicture attribute `lang_extension == 9` as forced; `rip/ffmpeg.py` sets `-disposition:s:N forced`. Plumb-through verified; end-to-end forced fixture is constrained by MPEG-PS subtitle packet limits (see honesty note below). | `TestForcedSubs` plumbing test |
| **Languages in MKV** | Already shipped in Phase 4 (`-metadata:s:a:N language=eng`); re-asserted by the pipeline test reading back the tag. | `test_rip_produces_valid_mkv` |

### Honest limitations (Phase 5 remaining nice-to-haves)

- A multi-angle *disc fixture* (IFO + interleaved-angle VOB) has not been
  built by `tests/synth.py` yet because real interleaved-angle layout needs
  an ILVU-capable muxer chain. The selection logic is unit-tested against
  synthetic cell layouts; the fixture-based end-to-end path is unproven.
- Forced-subtitle end-to-end is constrained by the fact that an
  `ffmpeg testsrc`-generated MPEG-PS without a real VOB subpicture stream
  has no subtitle packet for the map to keep; the flag itself reaches
  `-disposition` correctly.
- VobSub `dvd_subtitle` bitmaps, palette customisation and SSA/SRT
  transcoding options are explicitly *not* attempted — MKV carries the
  original PGS/VobSub streams untouched, which is the point of a remuxer.

## Phase 6 — Queue & robustness ✅

### What landed

- **`rip/queue.py` — `JobQueue`**, one scheduler, zero Qt deps:
  - jobs are executed **serially per drive** (an optical drive cannot seek to
    two places at once; proven by an overlap test on spans) while different
    drives run in parallel when `allow_parallel_drives` is set — worker pool
    size is `max_parallel_jobs` clamped to `[1, 8]`;
  - **cancellation is cooperative and two-level**: a queued job is dequeued
    and marked `SKIPPED` instantly; a running job finishes its current chunk
    then stops (`CANCELLED`);
  - **failure isolation**: one job's exception is caught in `_run_job` and
    becomes a FAILED result — the batch proceeds; tested with a real
    overwrite-refusal next to a good job;
  - **retry budget** (`read_retries`) applies **only to transient
    `DiscReadError`s** — fatal errors (bad selection, no space, refuse-to-
    overwrite) never burn retries;
  - **event fan-out** (`QueueEvent`: added/started/progress/result) so the
    GUI and the CLI subscribe without polling.
- **New `error` overwrite policy** (`ask|overwrite|skip|rename|error`): the
  batch-safe fail-loud default for queued runs; `ask` still propagates
  out of the queue so the GUI can prompt.
- **CLI batch execution**: `opticrip-cli rip <src> --title 1 --title 1 ...`
  fans one job out per repeated `--title`, runs everything through the
  queue, prints per-job results, and exits 3 if any job failed —
  **verified live**: 2 duplicate titles → 1 valid 1.1 MiB MKV + 1 refusal
  by the `error` policy, stale file untouched, exit 3.

### Honest limitations (Phase 6 remaining nice-to-haves)

- Multi-*drive* parallelism is thread-scheduled but only exercised with a
  single synthetic source directory in tests; two real USB drives racing on
  one working directory is untested on real hardware.
- Queue state is in-memory per process (deliberate for now: the GUI is the
  queue's owner); no persistence/resume across restarts yet — that is a
  Phase 7 decision.


---

## Phase 7 — GUI ✅ (core)

### What landed

- **`opticrip/gui`**: dark-theme QSS ("theme.py"), reusable widgets
  (`widgets.py`: `TitleTable`, `QueuePanel`, `LogPanel`), the main window
  (`mainwindow.py`) wired *only* to existing engine pieces — inspect runs
  the CLI's analyzer stack, ripping goes through `JobQueue` events — and an
  entry point (`python -m opticrip.gui`, offscreen fallback for CI).
- **Headless-tested**: 4 Qt tests (stylesheet, queue events render, log
  tail, live inspect of the fixture). A real rip through the GUI wiring was
  smoke-verified offscreen (valid MKV, queue events painted).
- Bug fixed on the way: an unparented `central = QWidget()` let Qt free the
  C++ object early ("libshiboken: Internal C++ object already deleted");
  `setCentralWidget` ownership fixed it.

### Honest limitations

- No settings dialog yet (policies are chosen inline via the queue dialog).
- Queue panel shows phase/percent per event; no per-drive lane view yet.

Design is fixed in the README under *Interface* and in
[ARCHITECTURE.md's layering diagram](ARCHITECTURE.md); dark theme baseline via
PySide6 `QSS` stylesheet with readable typography and non-ambiguous status
colouring; all interactive behaviour wired to the engine (no dead controls).

---

## Phase 8 — Blu-ray & protected discs ✅ (core)

### Blu-ray parsing — real

- **`disc/bd.py`**: pure-Python, big-endian `MPLS` + `CLPI` parsers
  (5-offset headers, PlayList→PlayItem walk, STN stream tables with
  language/pid/coding-type mapping to StreamInfo, playlist entry marks →
  chapters, sequence-info STC spans) + `BluRayAnalyzer` producing the same
  `DiscAnalysis` the DVD path does. Byte layouts cross-checked against
  libbluray's `mpls_parse.c`/`clpi_parse.c` field-by-field.
- **Fixture conformance demanding lessons**: our spec-built BDMV tree is
  *accepted by ffmpeg's own libbluray demuxer* ("1 usable playlists") after
  three byte-level corrections found only because two parsers disagreed:
  reserved11/multi/conn span **two** bytes (conn nibble in the second);
  STN has **7** count bytes not 6; playlist marks are fixed 14-byte records
  (no per-mark length). `tests.bdsynth.write_bdmv_fixture` writes real H.264
  + AC-3 MPEG-TS and its index.bdmv passes `index_parse.c`.
- **`rip/bdplay.py`**: `BdPlaylistExtractor` cuts each PlayItem's IN/OUT
  span (`-ss/-t`, `-c copy`, multi-clip concatenation in play order) and —
  like the DVD path — classifies scrambled reads as `EncryptedDiscError`.
  **First real Blu-ray rip via CLI: 181+ s 1920x1080 H.264 + AC-3 MKV with
  chapters preserved**, validated with ffprobe ("no re-encoding" tests
  apply here too).
- **Wiring**: engine picks the BD extractor by `media_type`; CLI registry
  auto-detects BDMV before DVD.
- **Docs**: `docs/PROTECTED_DISCS.md` states the no-circumvention policy,
  where honest errors surface, and why no decoder will ship from here.

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

