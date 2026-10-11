<h1 align="center">📼 OpticRip</h1>

<p align="center">
  An open-source DVD & Blu-ray <b>remuxer</b> — rip disc titles to MKV without re-encoding.<br/>
  A modular alternative to MakeMKV with its own disc engine (not a MakeMKV wrapper).
</p>

<p align="center">
  <a href="#features"><img alt="license" src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue"/></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-informational"/>
  <img alt="gui" src="https://img.shields.io/badge/GUI-PySide6%20(Qt6)-orange"/>
  <img alt="engine" src="https://img.shields.io/badge/engine-ffmpeg%20%7C%20mkvmerge-yellow"/>
  <img alt="status" src="https://img.shields.io/badge/status-alpha%20(Phases%201--3%20of%208)-lightgrey"/>
</p>

---

## Table of contents

1. [Features](#features)
2. [What makes it different](#what-makes-it-different)
3. [Architecture](#architecture)
4. [Current status](#current-status)
5. [Installation](#installation)
6. [Quick start](#quick-start)
7. [Usage](#usage)
8. [Permissions & optical drive access](#permissions--optical-drive-access)
9. [Protected / encrypted discs — read this](#protected--encrypted-discs--read-this)
10. [Testing](#testing)
11. [Known limitations](#known-limitations)
12. [Roadmap](#roadmap)
13. [Dependencies & licenses](#dependencies--licenses)
14. [Contributing](#contributing)
15. [FAQ](#faq)
16. [Project layout](#project-layout)

---

## Features

### Already implemented and verified ✅

| Area | Capability |
|---|---|
| **Optical drives** | Enumerate CD/DVD/BD drives on Linux via sysfs (`/sys/block/*/device/type == 5`); classify SATA vs USB vs virtual buses; report vendor/model/firmware per drive. |
| **Media probing** | Detect what is in a drive **without mounting it** — raw 2048-byte sector reads at `os.pread` level: ISO9660 PVD + label, UDF Volume Recognition Sequence (`BEA01`/`NSR02`/`NSR03`), audio-CD table of contents, DVD/Blu-ray signatures (`VIDEO_TS`, `BDMV`, `AACS`), dual-layer sizing. |
| **Disc sources** | Real drives, `.iso`/`.img` images, or already-mounted `VIDEO_TS/` / `BDMV/` folders — all three go through the identical downstream pipeline. |
| **DVD-Video parsing** | Full `VIDEO_TS.IFO` (VMG) and `VTS_xx_0.IFO` (VTSI) tree parsing in pure Python: title search pointer table, PTT/PGC mapping, program chains with cell playback/position tables, cell → sector ranges, chapter timing reconstruction, video/audio/subpicture attribute tables (codec, language, channels, sample rate, quantisation). Modelled directly on libdvdread's `ifo_types.h`. |
| **Title/track model** | `DiscAnalysis` / `Title` / `StreamInfo` / `Chapter` domain objects with duration-derived main-feature guessing, multi-angle awareness, and graceful degradation on malformed discs (warnings are preserved, parsing continues). |
| **Filename templating** | `{disc} {title} {index} {duration} {date} {year} {id} {chapters}` tokens, cross-platform sanitisation, unknown-token passthrough (so you can *see* typos instead of silently losing a name), explicit chapter-range tokens. |
| **Toolchain probing** | Runtime discovery of ffmpeg/ffprobe/mkvmerge + capability checks (is `dvdvideo` demuxer present? `bluray:` protocol? `matroska` muxer?) surfaced in the diagnostics panel. No silent "assumed present" behaviour. |
| **Safety plumbing** | Free-space checks before rips start, cooperative cancellation interface, overwrite policies (`ask`/`overwrite`/`skip`/`rename`), read-retry budget. |

### Already implemented and verified ✅ (continued)

| Area | Capability |
|---|---|
| **DVD ripping (now working)** | Own cell-range extractor (`rip/dvdcell.py`): copies the exact 2048-byte sector ranges each title's PGC cells occupy out of the VOB part files; retries with backoff; cooperative cancel mid-copy. Alternative path via ffmpeg's `dvdvideo` demuxer for raw devices/images. |
| **MKV remuxing** | ffmpeg `-c copy` — no re-encoding, asserted by tests (codec/geometry/rate identity through the whole chain). Per-track language metadata and chapter markers injected via ffmetadata. Optional mkvmerge backend. |
| **Output validation** | ffprobe re-probe comparing what was *actually mapped* (container, streams, languages, chapters, duration) against the selection — issues surface as data. |
| **Job engine** | Overwrite policies (`ask` propagates to the caller, `skip` never touches, `overwrite` replaces); free-space pre-check; atomic `.part` → `os.replace`; temp-dir cleanup; cooperative cancellation. |

### In progress 🚧

- **Phase 5 — chapter/language/metadata preservation edge cases** (first-VOBU
  skew, forced-subtitle flags, angle handling in output).
- **Phase 6 — job queue, multi-drive concurrency, error recovery, output validation.**
- **Phase 7 — PySide6 dark-theme desktop GUI.**
- **Phase 8 — Blu-ray MPLS/CLPI parsing + protected-disc documentation.**

### Explicitly out of scope ⛔

- **DRM circumvention** (CSS, CPPM, AACS, BD+). See [below](#protected--encrypted-discs--read-this).

---

## What makes it different

Most "alternative" tools wrap MakeMKV's CLI. OpticRip implements the disc layer
itself:

| | OpticRip | Typical wrappers |
|---|---|---|
| Disc structure parsing | Own pure-Python IFO/MPLS parsers (verified against libdvdread's reference C code) | Calls `makemkvcon` and scrapes its output |
| Re-encoding | Never. Stream copy only (`-c copy`) | Same, but opaque |
| Decrypting | Nothing. Cleanly factored attach point if *you* can lawfully use a decryptor | Built in |
| Graceful degradation | Warnings collected, parsing continues on damaged discs | Often whole-run abort |
| Testability | Full pipeline runs against synthetic disc fixtures — no hardware, no copyrighted media | Manual |

The engine is the point: get each layer right **once**, with an interface
guardrail between it and the UI, so components can be replaced instead of
forking the whole app.

---

## Architecture

```
            +------------------------------------------------------+
            |  opticrip.gui (PySide6)      opticrip.cli (argparse) |
            |  presentation only - no OS calls, no parsing         |
            +-----------------------------+------------------------+
                                          |  domain models + events
            +-----------------------------v------------------------+
            |  opticrip.core                                       |
            |    models      enums/dataclasses (Title, StreamInfo...)|
            |    interfaces  ABCs every backend implements          |
            |    config      Settings (+ JSON persistence)          |
            |    naming      output filename templating/sanitising  |
            |    diskutil    free-space checks                      |
            |    logging_bus in-process log fan-out                 |
            +-----------------------------+------------------------+
                                          |
    +-----------------+-------------------+-------------------+
    |  drives         |  disc             |  rip              |
    |  linux sysfs    |  identify         |  tools (ffmpeg)   |
    |  virtual (iso/  |  dvd (IFO parser) |  extract          |
    |  folder)        |  bd (MPLS parser) |  remux            |
    |                 |  fs (dir, iso9660)|  validate, queue  |
    +-----------------+-------------------+-------------------+
```

Full text: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). In short: every layer
below `core` implements an ABC from `opticrip/core/interfaces.py`
(`DriveProvider`, `BlockDevice`, `DiscFileSystem`, `DiscAnalyzer`,
`StreamExtractor`, `Remuxer`, `OutputValidator`, `DiskSpaceChecker`,
`JobQueue`), so any single component is replaceable — a Windows drive provider,
an external decryptor, an alternative muxer — without rewriting the app.

---

## Current status

**Alpha. Phases 1–4 of 8 complete.** See [`docs/PROGRESS.md`](docs/PROGRESS.md)
for the detailed, no-spin progress report.

Concretely what works today:

- `opticrip-cli inspect <src>` — analyse a device/image/folder and list
  titles with durations, chapters, streams and languages.
- `opticrip-cli rip <src>` — **real rips**: select titles and stream tracks,
  produce a valid MKV with original streams and preserved chapters, then
  validate the output with ffprobe. Verified end-to-end with a 38-test
  suite (`pytest`), including cross-engine conformance against ffmpeg's
  libdvdread on the same fixtures.
- Fixture building: `tests/synth.py` generates spec-accurate, legal,
  synthetic unencrypted DVD-Video trees for the whole pipeline to run on.

Concretely what does *not* work yet: no rip-to-MKV pipeline yet (Phase 4), no
GUI yet (Phase 7), no Blu-ray title list yet (Phase 8).

---

## Installation

### Linux (Debian/Ubuntu) — recommended development target

```bash
sudo apt install ffmpeg            # ffprobe ships with it
# optional alternative muxer:
sudo apt install mkvtoolnix

git clone <repo-url> opticrip && cd opticrip
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui,dev]'
```

### Permissions: no root required

Local user reads the optical device directly. To have access to `/dev/sr*`
as a non-root user:

```bash
# see what group your drive has:
ls -l /dev/sr0         # e.g. group 'cdrom'
sudo usermod -aG cdrom $USER    # log out and back in afterwards
```

Udev rules that grant read access to optical devices (`uaccess` tag) are
normally present on stock desktop systems for at least the primary seat.
OpticRip's diagnostics panel will tell you *exactly* which check failed if a
drive cannot be opened.

### No optical drive? No problem

OpticRip is fully usable against `.iso` images and extracted `VIDEO_TS`/
`BDMV` folders — this is also how the automated tests run. See
[Usage](#usage).

### Development-only ffmpeg in sandboxes

If your environment has no distro ffmpeg (e.g. CI containers):
`pip install static-ffmpeg` (dev extra) provides a static build with
`--enable-libdvdread --enable-libdvdnav --enable-libbluray` precompiled.

---

## Quick start

```bash
cd opticrip
. .venv/bin/activate

# 1) build a synthetic unencrypted DVD-Video fixture in ./output/demo
python tools/make_fixtures.py output/demo

# 2) probe + parse it with the OpticRip engine
python -m opticrip.cli.main inspect output/demo
```

You should see the title table (duration, chapters, streams) rendered from the
IFO structures, matching `ffprobe --f dvdvideo` output.

---

## Usage

```
opticrip                      # launch the PySide6 GUI (Phase 7)
opticrip-cli inspect <src>    # analyse a drive/image/folder and list titles
opticrip-cli rip <src> ...    # rip titles to MKV (working)
```

CLI *inspect* currently accepts:

- `—isc`                        — raw block devices such as `/dev/sr0`
- `—iso` or `—img`              — disc image files
- `—dir`                        — mounted or extracted `VIDEO_TS` / `BDMV` directories

All three go through the same pipeline; discerning who handles what is
handled by `disc.identify`.

### GUI (when Phase 7 lands)

Dark theme, drive panel with live media type, title/track tables, per-track
language & codec columns, queue with elapsed/ETA, destination picker,
diagnostics tab with the toolchain capability report.

---

## Permissions & optical drive access

| Access needed | Why | How to grant |
|---|---|---|
| Read `/dev/sr*` | Raw sector access for probing + ripping | `sudo usermod -aG cdrom $USER` (most distros) |
| Read/audio-CD ioctls | Detect audio-CD table of contents | Same group membership |
| Write to output dir | MKV files | Own the directory (default `~/Videos/OpticRip`) |
| **Not** needed | Running the GUI as root | **Never do this.** |

The engine never calls `sudo` and never writes outside the configured output
directory and its temp folder.

---

## Protected / encrypted discs — read this

OpticRip **does not and will not ship DRM circumvention**. Concretely:

| Technology | Problem | Status in OpticRip |
|---|---|---|
| **CSS** (commercial DVDs) | OpticRip's own parser reads IFOs but cannot decrypt payload sectors; ffmpeg/libdvdread alone will not either without an external decryptor | Out of scope; the `StreamExtractor` interface is the documented attach point |
| **AACS** (Blu-ray) | Requires a key database + libaacs, which libbluray does not bundle | Out of scope |
| **BD+** | Separate bytecode interpreter | Out of scope |
| **Unencrypted discs and fixtures** | Zero | ✅ fully supported target |

If you have *lawfully obtainable* decryption (your jurisdiction, your library,
your copies), OpticRip does the noise-free things around it. The decryptor
seam sits between `StreamExtractor` and `Remuxer`. See
[`docs/PROTECTED_DISCS.md`](docs/PROTECTED_DISCS.md) for the component-level
plan. OpticRip does not mangle, re-mangle or strip legal disc metadata in ways
that would break the disc's own integrity.

---

## Testing

```bash
pytest                                  # unit + synthetic end-to-end
pytest -m "not requires_hardware"       # CIs without a real drive
pytest -m requires_hardware             # manual run with a real drive
```

- **Synthetic fixtures** (`tests/synth.py`): real MPEG-PS VOBs built by
  ffmpeg's `lavfi` sources with *spec-conformant hand-built IFO/MPLS files*
  authored byte-accurately against libdvdread's `ifo_types.h`. These are legal
  (synthetic), tiny, and reveal real bugs (three real layout errors surfaced
  while authoring).
- **Cross-engine conformance**: each fixture is fed to *both* OpticRip's
  own parser and ffmpeg's libdvdread-backed `dvdvideo` demuxer;
  disagreements are treated as bugs.
- **`requires_hardware` tests** exist for real-drive integration and are
  skipped automatically when no drive is present.

---

## Known limitations

- Batch queue execution lands in Phase 6 (engine is job-for-job today).
- No Blu-ray title enumeration yet (Phase 8). DVD works today; Blu-ray
  playlist parsing is designed but not written.
- ISO images > 4 GiB are read but multi-extent (>4 GiB single-file) ISO9660
  notions are not, which is practically fine: `VIDEO_TS`/`BDMV` comprise
  many small files, none large.
- Raw (unmounted) UDF-only Blu-ray drives are handled through ffmpeg's
  `bluray:` protocol, so they need a libbluray-capable ffmpeg. Pure-Python
  UDF directory iteration for BD *structure* is planned in Phase 8.
- Windows support: designed-for, not implemented (platform-specific code is
  isolated inside `drives/` + `disc/identify.py`).

---

## Roadmap

See the phase plan and per-phase status in
[`docs/PROGRESS.md`](docs/PROGRESS.md).

- **Phase 5** — chapter/language fidelity edge cases (first-VOBU skew,
  forced-subtitle flags, angle-in-output handling).
- **Phase 6** — batch/multi-title queue execution.
- **Phase 6** — multi-drive job queue with per-drive concurrency, better
  retries and diagnostics.
- **Phase 7** — the polished dark-theme PySide6 GUI.
- **Phase 8** — Blu-ray MPLS/CLPI native parsing and the protected-disc docs.

---

## Dependencies & licenses

OpticRip source: **GPL-3.0-or-later**. All external components are invoked as
*separate processes*, never linked. Full version/license/capability table:
[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).

| Tool | Role | License |
|---|---|---|
| ffmpeg / ffprobe | demux, remux, inspect | LGPL-2.1+ / GPL if built so |
| mkvmerge (MKVToolNix) | optional remux backend | GPL-2.0-or-later |
| libdvdread / libdvdnav / libbluray | used *through* ffmpeg; parsing/nav only | GPL-2.0+ / LGPL-2.1+ |
| PySide6 | GUI (dev extra `gui`) | LGPL-3.0 / GPL-3.0 |
| pytest, static-ffmpeg | dev/test only | MIT |

---

## Contributing

Small focused PRs preferred. Every backend change must:

1. Keep the interface contract in `opticrip/core/interfaces.py` stable — or
   justify the break aloud.
2. Come with tests. For anything parser-related, cross-check against the
   reference C implementation (see `docs/references/`) or the behavior of
   `ffprobe -f dvdvideo` / `bluray:` on the same structure.
3. Not break the "no silent data loss" rule — warnings and errors are data
   too, and survive.

Bug reports that include an **actual disc structure** (a synthetic trip or an
`lsdvd`/`ffprobe` dump) get fixed *fast*. Patches that simply skip the fixture
checks do not.

---

## FAQ

**Does this replace MakeMKV?**
The goal is yes, for unencrypted discs today and for *your legally-decrypted*
content if and where that is lawful for you.

**Will it ever ship DRM circumvention?**
No. Not for technical-yes-no reasons — because forcibly bundling circumvention
would put this project (and its users) at legal risk for behaviour that users
can already do themselves with a dedicated tool. See
[protected-disc docs](docs/PROTECTED_DISCS.md).

**Why Python instead of Rust/C++?**
The heavy lifting (parsing MPEG-PS, muxing MKV) is already ffmpeg/mkvmerge.
OpticRip's own code — structures, files, state machines, progress, user data —
is I/O and control logic, where Python is fast to write, easy to review, and
correct-by-inspection. Performance-critical spots (block-level extraction) are
 delegated to native tools. If a hot loop ever needs rewriting, it is a single
module behind `StreamExtractor`.

**Why is the main-feature detection "guessed"?**
It is explicitly labelled as such: a heuristic, always surfaced alongside an
ambiguity note when two titles are similarly long. You can re-pick anything
the heuristic pre-selected.

**Test disc source?**
None, ever. All fixture media is generated synthetically by ffmpeg's `lavfi`
sources — zero copyrighted inputs, zero legal risk, and cheap to regenerate.

---

## Project layout

```
opticrip/
  core/          domain models, interfaces, config, naming, diskutil, log bus
  drives/        Linux + virtual drive providers
  disc/          media identification, ISO9660, DVD IFO parser, (BD MPLS in phase 8)
  rip/           toolchain probing, extraction, remuxing, validation, queue
  gui/           PySide6 frontend (phase 7)
  cli/           argparse frontend
tests/
  synth.py       synthetic, spec-accurate disc fixture builder
  ...            unit + integration tests across every phase
docs/
  ARCHITECTURE.md  layering and modules
  DEPENDENCIES.md  exact tool/library licenses and capabilities
  PROGRESS.md      detailed phase-by-phase progress report
  PROTECTED_DISCS.md legal/technical component plan
  references/    verbatim C sources of libdvdread/FFmpeg used as ground truth
tools/
  make_fixtures.py  commands to regenerate the synthetic fixtures
scripts/
  install-runtime-libs.sh  unprivileged vendor of Qt's runtime deps (containers)
```
