# OpticRip

A clean-room, cross-platform optical-disc ripper with a `makemkvcon`-shaped
command line, a robot (machine-readable) protocol, a Tk desktop UI, and a
dependency-free browser UI that runs locally or in Docker.

OpticRip was built from the behavioural evidence collected in
[CLONE_SPEC.md](CLONE_SPEC.md). No vendor code, assets or profiles were
copied, and no decryption keys are included.

> **Status: beta (0.9.0).** The DVD path is the most mature and was checked
> against a physical disc. Unencrypted Blu-ray and ISO/UDF sources work.
> Encrypted Blu-ray decryption is implemented and has been exercised once on a
> retail AACS disc, which staged 83 % of a 38 GB clip before the observation
> ended; that run's remux was not checked, so treat it as staging evidence
> rather than a proven retail rip. UHD is detected and deliberately refused.
> There is no installer, CI pipeline or license yet — see
> [Where the project stands](#where-the-project-stands).

## Contents

- [What it is](#what-it-is)
- [Where the project stands](#where-the-project-stands)
- [Feature support](#feature-support)
- [Requirements](#requirements)
- [Install and run](#install-and-run)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [GUI](#gui)
- [Web UI and Docker](#web-ui-and-docker)
- [Configuration](#configuration)
- [Decryption libraries](#decryption-libraries)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Testing](#testing)
- [Verification status](#verification-status)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Compatibility and clean-room notes](#compatibility-and-clean-room-notes)
- [License](#license)

## What it is

OpticRip splits into a headless engine and thin front ends:

* **The engine** (`opticrip.engine`) owns every disc operation: source
  resolution, drive enumeration, title/stream inspection, selection, saving,
  whole-disc backup, registration, and the GUI IPC loop.
* **The front ends** are thin. `opticrip.cli` is the `makemkvcon`-style
  console; `opticrip.gui` is a Tk window that starts the engine as a child
  process and talks to it over JSON lines. `opticrip.web` embeds the same
  engine in a small HTTP server for the browser UI. No front end reads a
  disc itself.

Supported sources are physical drives (`disc:`, `dev:`), folders (`file:`),
and ISO images (`iso:`). DVD-Video is parsed from IFOs, Blu-ray from MPLS
playlists, ISO9660 and UDF 2.50 are read directly, and ffmpeg performs the
final remux into Matroska. Copy protection is delegated to user-supplied
libdvdcss/libaacs/libbdplus; OpticRip contains no cryptography or keys.

The project is intentionally dependency-free: everything outside the optional
shared libraries and FFmpeg uses the Python standard library.

## Where the project stands

| Area | State | What that means |
| --- | --- | --- |
| DVD-Video inspection | **Working** | IFO title/chapter/stream parsing, PTT fallback for non-sequential titles, structure-protection trimming, minimum-length filtering. Checked against a physical disc; the captured output is in `analysis/evidence/`. |
| DVD save (VOB → MKV) | **Working** | Cells are staged into one VOB and remuxed with ffmpeg, with chapters and optional captions. Covered by a synthetic end-to-end test when FFmpeg is installed. |
| CSS DVDs | **Working with user library** | libdvdcss is loaded only for scrambled discs; metadata stays unencrypted and VOB payload is decrypted sector by sector. Verified against the physical test disc. No library is bundled. |
| DVD closed captions | **Working** | CEA-608 (CC1) is decoded from MPEG-2 user data into a SubRip track. Pop-on, paint-on, roll-up, PAC positioning and the special/extended tables are handled. Synthetic end-to-end test. |
| Blu-ray inspection | **Working, playlists only** | MPLS playitems and the STN stream table produce titles, streams and segments. CLPI is **not** parsed; stream metadata comes from the playlist. |
| Blu-ray save | **Working for unencrypted discs** | m2ts clips are concatenated and remuxed. Encrypted clips are staged through libaacs/libbdplus when available. |
| AACS | **Implemented, one retail disc exercised** | libaacs does the cryptography. Unit tests use fake libraries and encrypted fixtures. A retail dual-layer Blu-ray has been staged to 83 % of its 38 GB clip at 2×–5× disc speed with a KEYDB entry; the remux of that run was not verified. |
| BD+ | **Implemented, not retail-verified** | libbdplus plus its VM configuration files. Same fake-library test status. |
| UHD (AACS 2.x) | **Detected, refused** | The MKB type is reported as `AACS 2.x (UHD)` and saving/backup stop with message 9143. This is a designed hard limit, not a TODO. |
| ISO / UDF sources | **Working** | ISO9660 for DVD/data images, read-only UDF 2.50 (including metadata partitions) for Blu-ray images. Unit tests plus fixtures. |
| Whole-disc backup | **Working** | Raw or decrypted copy through the same filesystem abstraction; AACS/BD+ metadata is relocated to `MAKEMKV/` so the result opens as an unencrypted disc. A cancelled backup leaves the files copied so far. |
| Tk GUI | **Working, manually tested** | Source picker, title/stream tree, output folder, progress, cancel for saves/backups, a log pane that explains failures with a probable cause and a next step, preferences, registration and backup dialogs. The log formatting is unit-tested; there are no automated UI tests. |
| Web UI / Docker | **Working, HTTP-tested** | Stdlib-only browser front end over the same engine with title/stream selection, save, backup, cancel, progress and log. Dockerfile and compose recipe install FFmpeg and run non-root. The image build is not covered by CI yet, and the UI has no authentication. |
| Profiles, selection, naming | **Working** | Four `.mmcp.xml` profiles, the selection-rule language, and the output-name template with collision suffixes. Unit-tested. |
| Registration and settings | **Working** | `HKCU\Software\OpticRip` on Windows, JSON elsewhere. Unit-tested. |
| Firmware tool (`f`) | **Partial** | `f list` prints drive slots; other firmware arguments return message 9111 and exit code 2. Flashing is not implemented. |
| Online metadata | **Not implemented** | Out of scope. Titles without metadata are named `title_tNN.mkv`, matching the reference's behaviour for unknown titles. |
| Packaging / CI / license | **Missing** | No `pyproject.toml`, installer, GitHub Actions workflow or LICENSE file. Run from a checkout for now. |

In short: DVD ripping is usable today; Blu-ray is usable for unencrypted
discs, and AACS/BD+ decryption is implemented with one retail AACS staging run
behind it but no verified end-to-end protected rip; UHD is out of scope by
design.

## Feature support

### Disc and stream handling

* **DVD-Video** — `VIDEO_TS.IFO` / `VTS_*.IFO` parsing; title and chapter
  lists; stream attributes; segment maps; structure-protection cell
  trimming; `TT_SRPT` titles resolved through `VTS_PTT_SRPT` when they are not
  a single sequential PGC; minimum title length (default 120 s).
* **Blu-ray** — `PLAYLIST/*.mpls` playitems, `STREAM/*.m2ts` clips, STN
  codec/PID/language rows, 45 kHz clip timing, playlist marks as chapters.
  `CLPI` files are not read.
* **ISO images** — ISO9660 first, UDF 2.50 as a fallback; UDF metadata
  partitions are supported. Exotic layouts (multi-session, VAT-remapped)
  may be rejected; mount them or use `file:` instead.
* **Drives** — 16 `DRV` slots, disc-presence detection and labels. Full drive
  enumeration and IOCTL queries are Windows-only; on other platforms use
  `dev:` with a device path, or `file:`/`iso:`.
* **CSS** — libdvdcss-backed `CssFS` decrypts VOB sectors while ISO9660
  metadata reads stay clear. On Windows libdvdcss accepts drive letters, not
  image paths, so CSS ISOs need the physical disc there.
* **AACS / BD+** — decrypted clips are staged into a temporary folder before
  ffmpeg sees them; staging reports progress, checks cancellation every
  6144-byte unit, and removes the staged clips on cancel. Metadata for
  non-folder sources is materialised into a temporary tree so libaacs and
  libbdplus can read it.

### Output

* Matroska remux with stream selection, chapters (`ffmetadata`), language and
  title tags, and an MP4-intermediate retry when the MKV muxer rejects
  packets.
* Output-name templates. The default is
  `{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}`, which yields `title_t00.mkv`
  for an unnamed title. Existing files get `_1`, `_2`, … suffixes.
* Selection strings such as
  `-sel:all,+sel:(favlang|nolang|single),-sel:(havemulti|havecore),+sel:mvcvideo,=100:all,-10:favlang`
  with the profile language list. Profiles: `default`, `flac`, `aac-st`,
  `wdtv`.
* Decrypted whole-disc backup, with the reference's `MAKEMKV/` relocation of
  `AACS`/`BDSVM` metadata.

## Requirements

* **Python 3.11 or newer.** No third-party Python packages.
* **Tk 8.6+** for the GUI (`python3-tk` on many Linux distributions). The CLI,
  engine and browser UI do not need Tk.
* **Docker (optional)** for the container recipe. The image installs FFmpeg
  and needs no Python packages beyond the standard library.
* **FFmpeg** — `ffmpeg` and `ffprobe` are required to save titles and to run
  the end-to-end tests. Inspection and raw backups do not call them.
* **Optional decryption libraries** — libdvdcss for CSS, libaacs (+ a
  KEYDB.cfg entry) for AACS, libbdplus (+ VM files) for BD+. See
  [Decryption libraries](#decryption-libraries).
* **Windows** is the primary platform for physical drives. Linux and macOS
  are supported for folders and ISO images; drive enumeration there is
  limited to explicit device paths.

## Install and run

There is no package or installer yet. From a checkout:

```bash
git clone <this repository> opticrip
cd opticrip
python run.py            # GUI
```

Or use the engine directly:

```bash
python -m opticrip.cli -r info disc:0
```

`python run.py` starts the GUI with an empty window.
`python run.py --open file:/path/to/VIDEO_TS` (or `-o`) opens a source after
startup. The CLI must be run from the repository root (or with the repository
root on `PYTHONPATH`), because `opticrip` is not installed as a package.

## Quick start

Inspect a DVD folder in robot mode:

```bash
python -m opticrip.cli -r info file:/media/discs/OVER_THE_HEDGE
```

Expected shape:

```text
MSG:1005,0,1,"OpticRip v0.9.0 win(x64-release) started","%1 started","OpticRip v0.9.0 win(x64-release)"
MSG:3006,0,1,"Opening files on harddrive at file:///media/discs/OVER_THE_HEDGE","Opening files on harddrive at %1","file:///media/discs/OVER_THE_HEDGE"
TCOUNT:13
```

The 13 titles then follow as `TINFO` rows with their `SINFO` streams. (If the
open fails you get `MSG:5010` and a non-zero exit instead.)

Save title 0:

```bash
python -m opticrip.cli mkv file:/media/discs/OVER_THE_HEDGE 0 /output
```

Back up a disc, decrypting when a library is configured:

```bash
python -m opticrip.cli backup disc:0 /output/backup
```

## CLI reference

```text
Use: opticrip [switches] Command [Parameters]

Commands:
  info <source>                                  print disc information
  mkv <source> <title id> <destination folder>   save one title
  backup <source> <destination folder>           back up the whole disc
  f <args>                                       firmware tool (list only)
  reg <key string or file name>                  enter a registration key
```

`guiserver` is also accepted; it starts the JSON-lines loop used by the GUI.
It is internal, not part of the compatible surface, and may change.

### Sources

| Form | Meaning |
| --- | --- |
| `disc:<n>` | Drive slot `n` (0-based, 16 slots) |
| `dev:<device>` | Drive by OS device name, e.g. `D:` or `/dev/sr0` |
| `file:<folder>` | A folder containing `VIDEO_TS`, `BDMV` or `BDAV`; a `VIDEO_TS` folder resolves to its parent |
| `iso:<image>` | An ISO9660 or UDF image |
| bare path | A directory or `.iso`; the prefix is inferred |

The GUI additionally accepts an `.mkv` file as a source (it probes it with
ffprobe and shows its tracks). The CLI intentionally does not: passing an
`.mkv` to `file:` reproduces the reference's exit code 127.

### Switches

| Switch | Effect |
| --- | --- |
| `-r`, `--robot` | Emit `MSG`/`DRV`/`TCOUNT`/`CINFO`/`TINFO`/`SINFO` records instead of prose |
| `-h`, `--help` | Not supported: rejected as an unrecognised option (usage is printed, exit 1) |

Unknown switches print `opticrip: unrecognized option: <x>` and exit 1.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Usage error, bad key, or unrecognised switch |
| 2 | Firmware-tool argument error / unsupported firmware operation |
| 10 | Source could not be opened |
| 11 | Save failed |
| 127 | Unknown command, or an unhandled source such as `file:x.mkv` |
| 130 | Interrupted with Ctrl-C |

### Verified quirks that are reproduced

* `list` is advertised in the usage text but is **not** a command
  (`Invalid command: list`, exit 127).
* `file:<file.mkv>` prints message 3006 and exits 127.
* A folder with no disc files exits 10 without a message.
* Robot mode suppresses progress records (`PRGV`), matching the reference.
* The banner always reports `win(x64-release)` regardless of host, because
  consumers parse its shape.

## GUI

Start with `python run.py`. The window offers:

* opening a drive, folder, ISO or MKV;
* a title tree with working-title and per-track checkboxes;
* an output folder chooser and a configurable name template;
* a progress bar and a Cancel button that works for saves and backups;
* a log pane fed by engine messages, where a failure is reported as a
  probable cause, the other possibilities and a suggested next step;
* **File** actions for loading libdvdcss/libaacs/libbdplus, selecting a
  `KEYDB.cfg`, restoring automatic detection, backup, preferences and
  registration;
* saved window geometry and pane positions.

Architecture: the GUI spawns `python -m opticrip.cli guiserver`, sends
newline-delimited JSON commands (`open`, `drives`, `save`, `save_all`,
`backup`, `settings`, `register`, `cancel`, `close`, `quit`) and renders
events (`hello`, `ready`, `opened`, `disc`, `drive`, `message`, `progress`,
`saved`, `backup_done`, `settings_saved`, `registered`, `cancelling`,
`closed`, `failure`, `error`, `stderr`, `engine_exit`, `bye`). Saves and backups
run on
an engine worker thread so Cancel remains responsive.

One caveat: `open` is handled synchronously on the engine's stdin thread, so
the UI waits for a slow scan and Cancel does not interrupt opening. Saving
and backup are cancellable.

## Web UI and Docker

The browser front end wraps the same engine in a small standard-library HTTP
server. It supports opening a source, title and track selection, saving the
selected titles, cancelling a save or backup, whole-disc backup, progress
and the engine log; failures appear there with the same probable cause and
suggested fix the desktop log shows. It is single-user and deliberately has no
authentication.

```bash
python -m opticrip.web --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`. `--data-dir` overrides the settings directory
for that process; otherwise `OPTICRIP_DATA_DIR` or `~/.opticrip` applies.

### HTTP API

The API is clone-local and is not part of the `makemkvcon`-compatible
surface. Jobs are asynchronous: a POST returns `202` and the client polls
`/api/state`; a second job returns `409`.

| Method | Path | Body / purpose |
| --- | --- | --- |
| GET | `/api/health` | liveness and version |
| GET | `/api/state` | disc, job, progress, log, drives, output directory |
| POST | `/api/open` | `{"spec": "disc:0"}` |
| POST | `/api/save` | `{"title": 0, "dest": "/out", "streams": {"0": [1]}}` |
| POST | `/api/save_all` | `{"titles": [0], "dest": "/out", "streams": {"0": [1]}}` |
| POST | `/api/backup` | `{"spec": "disc:0", "dest": "/backup", "decrypt": true}` |
| POST | `/api/cancel` | request cancellation of the running save/backup |
| POST | `/api/close` | close the current source |
| POST | `/api/drives` | rescan drive slots |

A `file:` or `iso:` source is enough for inspection, saving and MKV export.
Backup still requires a drive source (`disc:`), matching the engine and the
reference. Web requests are same-origin checked, and static files are served
from a fixed allow-list.

### Docker

```bash
docker build -t opticrip .
docker run --rm -p 8080:8080 \
  -v "$PWD/data:/config" \
  -v "$PWD/media:/media:ro" \
  opticrip
```

Or use the included recipe:

```bash
docker compose up --build
```

`/config` holds settings and the default output directory
(`/config/output`). Mount a media folder read-only and enter its container
path in the UI. The container runs as UID 1000; make sure mounted output is
writable by that user.

For a physical drive, pass the device and its group (the group id differs
per host):

```bash
docker run --rm -p 8080:8080 \
  --device=/dev/sr0 \
  --group-add "$(stat -c '%g' /dev/sr0)" \
  -v "$PWD/data:/config" \
  opticrip
```

Use `dev:/dev/sr0`, not `disc:0`, inside a Linux container: drive enumeration
is Windows-only, so `disc:` slots are empty there. Decryption libraries are
not bundled. Mount them and pass `OPTICRIP_DVDCSS`, `OPTICRIP_LIBAACS`,
`OPTICRIP_LIBBDPLUS` and `OPTICRIP_AACS_KEYDB` as needed; the Tk preferences
dialog for loading libraries is not exposed in the browser UI.

**Do not publish the port to the internet.** There is no login, and the API
can read mounted media and write to mounted output. The Dockerfile and
compose file are provided, but the image build is not yet exercised in CI.

## Configuration

### Environment variables

| Variable | Purpose |
| --- | --- |
| `OPTICRIP_FFMPEG` | Path to an `ffmpeg` binary **or** a folder containing `ffmpeg` and `ffprobe`. Takes precedence over `PATH`. |
| `OPTICRIP_FFMPEG_DIR` | Folder-only alias for `OPTICRIP_FFMPEG`, kept for existing installs. |
| `OPTICRIP_DVDCSS` | Full path to libdvdcss, used when no path is stored in settings. |
| `OPTICRIP_LIBAACS` | Full path to libaacs (CLI use, when settings have no path). |
| `OPTICRIP_LIBBDPLUS` | Full path to libbdplus. |
| `OPTICRIP_AACS_KEYDB` | Path to `KEYDB.cfg` when it is not in libaacs' standard location. |
| `OPTICRIP_DATA_DIR` | Overrides the application data directory (default `~/.opticrip`). |
| `OPTICRIP_REG_ROOT` | Overrides the Windows registry key (default `Software\OpticRip`). |

### Settings storage

* **Windows:** `HKCU\Software\OpticRip`, values stored as `REG_SZ`.
* **Other platforms and tests:** `<data dir>/settings.json`.
* The reference key is deliberately not used, so a real MakeMKV installation
  is never touched.
* Unknown values are preserved when settings are rewritten.

Frequently relevant keys (all defined in `opticrip/settings.py`):

| Key | Default | Meaning |
| --- | --- | --- |
| `app_MinTitleLength` | `120` | Skip titles shorter than this many seconds |
| `app_DefaultSelectionString` | see above | Track selection rules |
| `app_DefaultOutputFileName` | `{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}` | Name template |
| `app_DefaultProfileName` | `default` | Profile used by the CLI |
| `app_BackupDecrypted` | `1` | Default for the backup dialog |
| `app_StructureProtection` | `Auto` | Structure-protection handling |
| `app_DvdcssPath` | `""` | Staged/selected libdvdcss path |
| `app_AacsPath` | `""` | Staged/selected libaacs path |
| `app_BdplusPath` | `""` | Staged/selected libbdplus path |
| `app_AacsKeyFile` | `""` | `KEYDB.cfg` location (used in place, never copied) |
| `app_DataDir` | `""` | Overrides the data directory |

### Data directory

The default is `~/.opticrip` (`%USERPROFILE%\.opticrip` on Windows), or the
`OPTICRIP_DATA_DIR` path. User-supplied libraries are validated and staged
under `libdvdcss/`, `libaacs/` and `libbdplus/`. On Windows a loaded DLL
cannot be replaced while the process runs, so staged copies are not loaded
there; the library is used from its original location.

## Decryption libraries

OpticRip ships **no** decryption code, keys, certificates or VM files. It
loads libraries you provide, and only when a protected disc is saved.

### libdvdcss (CSS DVDs)

Supply a copy in one of these ways:

* **GUI:** *Load libdvdcss library…* validates the file and stages it under
  `<data dir>/libdvdcss/`. *Use automatic libdvdcss detection* clears the
  configured path (it does not delete the staged copy).
* **By hand:** put `libdvdcss-2.dll` (or the platform equivalent) in
  `<data dir>/libdvdcss/`, next to `opticrip/`, or on the system loader path.
  VLC's directory is checked on Windows.
* **Environment:** `OPTICRIP_DVDCSS` for CLI use.

libdvdcss is only loaded when the disc looks scrambled. Opening an old CSS
disc can pause for around a minute while the title key is recovered; title
inspection skips VOB reads, so the pause normally happens on first save.

### libaacs (AACS Blu-ray)

* **GUI:** *Load libaacs library…* stages it under `<data dir>/libaacs/`;
  *Load AACS key file (KEYDB.cfg)…* points libaacs at a key database without
  copying it.
* **By hand:** put `libaacs.dll` / `.so` / `.dylib` in `<data dir>/libaacs/`,
  next to `opticrip/`, or on the loader path. libaacs also searches its
  normal locations: `%APPDATA%\aacs\KEYDB.cfg` on Windows,
  `~/.config/aacs/KEYDB.cfg` elsewhere.
* **Environment:** `OPTICRIP_LIBAACS` and `OPTICRIP_AACS_KEYDB`.

AACS needs a KEYDB entry with a `D |` disc key or enough material (`V |`
volume ID, `M |` media key) to derive it. A drive supplies the VID; a folder
or ISO image generally needs `V |` in KEYDB. Both libraries must match the
interpreter's bitness (64-bit Python needs 64-bit libraries).

### libbdplus (BD+)

BD+ additionally needs libbdplus 0.2 or newer and its VM configuration files
(`bdplus/vm0`) in the user or system configuration directory. Without them
the conversion table cannot be built, and OpticRip reports that instead of
writing corrupt video. On Windows, the DLLs' own dependencies (for example
libgcrypt) must sit beside them or on `PATH`.

### Legal

Using these libraries may be restricted in your jurisdiction, and you are
responsible for using them only with discs you are entitled to copy. OpticRip
does not circumvent anything on its own and does not distribute keys.

## Architecture

```text
┌──────────────────────┐        JSON lines         ┌───────────────────────────┐
│ Tk GUI               │ ────────────────────────> │ Engine (subprocess)       │
│ opticrip/gui/        │ <──────────────────────── │ opticrip.engine           │
└──────────────────────┘   events + messages       └────────────┬──────────────┘
                                                                │
┌──────────────────────┐                                        │
│ CLI (makemkvcon)     │ ───────────────────────────────────────┤
│ opticrip/cli.py      │                                        ▼
└──────────────────────┘                         ┌───────────────────────────┐
                                                 │ DiscFS                    │
                                                 │ FolderFS / IsoFS / CssFS  │
                                                 └────────────┬──────────────┘
                                                              │
                       ┌──────────────────────────────────────┼───────────────────────┐
                       ▼                                      ▼                       ▼
             ┌──────────────────┐                  ┌────────────────────┐   ┌──────────────────┐
             │ DVD IFO parser   │                  │ Blu-ray MPLS parser│   │ UDF / ISO9660    │
             │ opticrip/dvd.py  │                  │ opticrip/bdmv.py   │   │ udf.py/iso9660.py│
             └────────┬─────────┘                  └─────────┬──────────┘   └──────────────────┘
                      │                                      │
                      ▼                                      ▼
             ┌──────────────────────────────────────────────────────────────┐
             │ selection → naming → staging → ffmpeg remux / backup writer │
             │ selection.py  naming.py  cc.py  ffmpeg.py  backup.py         │
             └──────────────────────────────────────────────────────────────┘
```

### Modules

| Module | Responsibility |
| --- | --- |
| `opticrip/engine.py` | All disc operations, event emission, save/backup orchestration, JSON-lines IPC |
| `opticrip/model.py` | `Disc`, `Title`, `Stream`, `Segment` data model |
| `opticrip/messages.py` | Numeric message catalogue and rendering |
| `opticrip/diagnose.py` | Turns an exception into a probable cause, ranked causes and a next step for the log |
| `opticrip/robot.py` | Robot and human record writers (`MSG`, `DRV`, `CINFO`, `TINFO`, `SINFO`, `TCOUNT`, `PRGV`, `PRGC`, `PRGT`) |
| `opticrip/drives.py` | Windows drive enumeration, media presence, labels (16 slots) |
| `opticrip/discfs.py` | `FolderFS`, `IsoFS`, `CssFS` and the shared `DiscFS` protocol |
| `opticrip/dvd.py` | DVD-Video IFO/PGC parsing, PTT resolution, CSS scrambling detection |
| `opticrip/bdmv.py` | Blu-ray MPLS playlist/STN parsing (no CLPI) |
| `opticrip/iso9660.py` | Read-only ISO9660 |
| `opticrip/udf.py` | Read-only UDF 2.50, including metadata partitions |
| `opticrip/cc.py` | CEA-608 → SubRip caption decoder |
| `opticrip/backup.py` | Raw/decrypted whole-disc copy and `MAKEMKV/` relocation |
| `opticrip/dvdcss.py` | libdvdcss loading, validation, staging, sector reads |
| `opticrip/aacs.py` | libaacs loading, MKB parsing, VID/key hand-off |
| `opticrip/bdplus.py` | libbdplus loading, VM setup, conversion-table calls |
| `opticrip/bddecrypt.py` | AACS/BD+ clip staging and 6144-byte unit fixing |
| `opticrip/ffmpeg.py` | Tool discovery, probing, remux, progress parsing, cancellation |
| `opticrip/profiles.py` | `.mmcp.xml` loader and built-in default profile |
| `opticrip/selection.py` | Selection-rule language |
| `opticrip/naming.py` | Output-name template expansion and collision handling |
| `opticrip/settings.py` | Registry/JSON persistence, defaults, data directory |
| `opticrip/cli.py` | `makemkvcon`-shaped command line and exit codes |
| `opticrip/gui/app.py` | Tk window, state machine, engine event pump |
| `opticrip/gui/engine_client.py` | Child-process JSON-lines client |
| `opticrip/gui/dialogs.py` | Preferences, backup, registration, about, help dialogs |
| `opticrip/web/server.py` | HTTP server, JSON API, single-job state, static assets |
| `opticrip/web/static/` | Browser UI (HTML, CSS, vanilla JavaScript) |
| `run.py` | GUI entry point |

### Data flows

**Open/inspect.** `Engine.open_source` parses the source spec, builds a
`DiscFS`, sniffs the disc kind, parses IFOs or playlists, applies the default
selection and name template, then emits `CINFO`/`TINFO`/`SINFO` records and
(optionally) a `TCOUNT`. Opening never decrypts clips.

**Save one title.** `Engine.save_title` resolves the selected streams and
segments, stages DVD cells into one VOB or Blu-ray clips into decrypted temp
files when needed, then invokes ffmpeg with a stream map, chapter metadata
and optional caption SRT. Progress is parsed from ffmpeg's `-progress` output;
cancellation is checked by the progress callback and between copy units.

**Save all.** `Engine.save_selected` iterates selected titles, applying the
selection rules and name template, and stops on the first failure or cancel.

**Backup.** `BackupWriter` walks the disc tree, copies files in 8 MiB chunks,
decrypts VOB/m2ts payload when requested, and writes AACS/BDSVM metadata to
`MAKEMKV/`. It reports bytes, not ffmpeg progress.

### Protocol details

Robot lines are quoted and escaped, one per line, flushed immediately:

```text
MSG:<id>,<flags>,<count>,"<rendered>","<template>","<param>"...
DRV:<index>,<state>,<code>,<visible>,"<drive>","<disc>","<device>"
TCOUNT:<n>
CINFO:<attr>,<msgcode>,"<value>"
TINFO:<title>,<attr>,<msgcode>,"<value>"
SINFO:<title>,<stream>,<attr>,<msgcode>,"<value>"
PRGV:<current>,<total>,<max>
PRGC:<code>,<id>,"<action>"
PRGT:<code>,<id>,"<operation>"
```

Message ids observed from the reference live in the normal catalogue range;
clone-only ids are in the 9xxx range and are documented as `LOCAL` in
`opticrip/messages.py` so they can never be confused with reference ids.
`CLONE_SPEC.md` §21 lists the behaviours that could not be verified, and
§22 lists the acceptance scenarios.

## Repository layout

```text
.
├── README.md               this file
├── CLONE_SPEC.md           behavioural specification (1128 lines)
├── run.py                  GUI launcher
├── Dockerfile              web UI container (FFmpeg installed)
├── docker-compose.yml      config/media/output volumes
├── .dockerignore
├── opticrip/
│   ├── engine.py           disc operations and IPC
│   ├── ...                 modules listed above
│   ├── gui/                Tk front end
│   ├── web/                browser front end and static assets
│   └── profiles/           default, flac, aac-st, wdtv
├── tests/                  23 unittest modules
└── analysis/               evidence captures and the tooling used for the spec
```

`analysis/` holds the research scripts, text evidence and screenshots behind
`CLONE_SPEC.md`. It is not needed at runtime and is not a supported part of the
application.

Large media fixtures are deliberately **not** tracked: the 104 MB rip sample,
the junk `test.mkv`/`junk.iso` probes and the scratch `.tmp-udf/*.iso` images
were removed from version control (they remain in older history). The only
fixtures left in `analysis/fixtures/` are the few-kilobyte `fakebd/` Blu-ray
structures. Everything the suite needs is generated: DVD/ISO/UDF inputs are
built in-process, and the `guiserver` save test renders its own four-second
MKV with FFmpeg.

## Testing

```bash
python -m unittest discover -s tests
```

With FFmpeg installed and a drive present the whole suite runs:

```text
Ran 207 tests
OK
```

Without `ffmpeg`/`ffprobe` eight tests skip: the end-to-end
remux, caption muxing, encrypted-clip staging and the `guiserver` save
protocol. Those skips are environmental, not failures, and installing FFmpeg
is enough to exercise them.

The current suite includes four regression tests from the maintenance pass
(three for FFmpeg tool discovery, one for message rendering), twelve tests for
the failure-diagnosis rules and the log block they feed, and eight HTTP-level
tests for the web UI.

No test requires real CSS/AACS/BD+ keys or retail media, and none reads a
committed media file: synthetic inputs are built in-process and the MKV used by
the GUI save test is generated by FFmpeg at run time.

### What the suite covers

Robot record grammar and quoting, message rendering, CLI behaviour and exit
codes, settings persistence, registration, naming, selection rules, profile
loading, DVD IFO parsing, Blu-ray MPLS parsing, ISO9660, UDF, caption
decoding, libdvdcss/libaacs/libbdplus wrappers with fake libraries and fake
devices, encrypted-clip staging and cancellation cleanup, backup, failure
diagnosis (Windows and errno codes, AACS/FFmpeg/structure errors and the
unknown-error fallback), the
`guiserver` protocol, the web API (health, state, open failure, validation,
cross-origin refusal and drive scans), and synthetic end-to-end rips when
FFmpeg is present.

Tests are fast and offline. There is no CI configuration, so the suite only
runs when someone runs it.

## Verification status

**Verified against a physical disc (recorded in `analysis/evidence/`):**
13 DVD titles in the captured order, sizes, chapter counts, TINFO ids
24/25/26/27/30, segment maps, PTT-resolved non-sequential title, stream
rows, GUI tree and protection label, CSS decryption through libdvdcss, and
the refusal path when no library is configured.

**Observed once on a retail AACS Blu-ray:** a 38 GB dual-layer title
(`LOTR_THE_TWO_TOWERS_PT1`) was opened from a USB `HL-DT-ST BD-RE BU40N` with
libaacs and a `keydb.cfg` entry, and its single 38.25 GiB clip was staged for
decryption to 83 % at 2×–5× disc speed (4.5–22 MiB/s, 2 % engine CPU). The
same clip took MakeMKV 50.8 minutes on the same drive (12.86 MiB/s), so the
staging path is neither slower nor CPU-bound. The remux that follows staging
was not observed, so this evidences decryption and staging only.

**Covered by synthetic/unit tests:** unencrypted DVD rip end to end (when
FFmpeg is installed), captioned DVD with a real SubRip track in the output
MKV, AACS/BD+ staging with fake libraries, UHD refusal (message 9143),
ISO9660/UDF fixtures, the full GUI save protocol, and the web API.

**Not verified:** a protected retail rip carried through to a checked
output, retail BD+ discs, real libaacs/libbdplus builds,
macOS, BD drive enumeration on non-Windows hosts, automated GUI interaction,
a Docker image build, and UHD (unsupported by design). The verification
claims above describe captured evidence; they were not re-run in this
environment.

## Known limitations

* **Encryption needs user-supplied libraries.** CSS needs libdvdcss; AACS
  needs libaacs plus a matching KEYDB entry; BD+ additionally needs libbdplus
  and its VM files. CPPM is unsupported. No keys or libraries are bundled.
* **UHD cannot be decrypted.** AACS 2.x needs a different key tree, bus
  encryption and a licensed host certificate. OpticRip detects it and refuses
  with a specific message rather than a generic failure.
* **Protected Blu-rays are staged.** Decryption happens before ffmpeg reads a
  clip, so peak disk use is roughly the title's clip size plus the output.
* **No CLPI parsing.** Blu-ray stream metadata comes from the MPLS STN table.
  This is sufficient for the tested discs but is not a full clip-info reader.
* **Captions are sampled at inspection time.** The `S_CC608/DVD` track is
  listed only if caption data appears in the first 4 MiB of the title (or the
  VOBs are readable). A caption that starts later appears only after a save,
  which decodes the whole stream. CC1/field 1 only; CC2–4, XDS and Text mode
  are ignored.
* **One DVD duration delta.** The 83-minute main feature reports `1:23:11`
  where the reference reports `1:23:02` (9 s), due to trailing-cell command
  handling. All other durations, sizes, maps and ids in the capture match.
* **A cancelled backup keeps partial output.** Files copied before the cancel
  remain in the destination; deleting them automatically could destroy
  pre-existing files, so removal is left to the user.
* **Opening is not cancellable.** `open` runs on the engine's stdin thread;
  Cancel applies to saves and backups.
* **No online metadata.** Title names come from the disc; unknown titles use
  `title_tNN` naming. There is no title database.
* **`guiserver` is private.** The reference's binary socket and HTTP
  streaming endpoints were UNKNOWN, so the JSON-lines protocol is
  clone-local and may change.
* **`f` is incomplete.** Only `f list` is implemented; firmware flashing is
  not attempted.
* **The web UI is unauthenticated and single-job.** Bind it to `localhost` or
  a trusted network, never the public internet. Library selection,
  registration and preferences remain CLI/Tk-only; in a container, pass the
  library paths through environment variables.
* **The container is a recipe, not a tested release.** CI does not build the
  image yet, and there is no published image.
* **No installer or CI.** The project runs from a checkout.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `ffmpeg was not found` | FFmpeg missing or not on `PATH` | Install FFmpeg, or set `OPTICRIP_FFMPEG` to the binary or its folder |
| `This disc uses CSS protection…` and no save | No libdvdcss configured | Load libdvdcss in the GUI, stage it under `<data dir>/libdvdcss/`, or set `OPTICRIP_DVDCSS` |
| `volume key is unknown` | KEYDB has no entry for the disc | Add the disc's `D |` key or `V |`/`M |` material to `KEYDB.cfg`; check the key file path |
| BD+ fails before any output | libbdplus too old or `bdplus/vm0` missing | Install libbdplus 0.2+ and its VM configuration files |
| CSS ISO opens on Linux but not Windows | libdvdcss' Windows backend accepts drive letters only | Rip from the physical drive on Windows |
| GUI does not start | Tk missing | Install Tk (`python3-tk`); the CLI still works |
| `disc:0` shows nothing on Linux/macOS | Drive enumeration is Windows-only | Use `dev:/dev/sr0` or `file:`/`iso:` |
| Save fails at the mux step on Blu-ray | ffmpeg rejected the packet stream | Check the log; the remux falls back through an MP4 intermediate only when no subtitles are selected |
| GUI/web Cancel does nothing while opening | Opening is synchronous | Wait for the scan to finish; Cancel works for saves/backups |
| The log says `Saving failed: the drive stopped responding` | The drive dropped off the USB bus or went to sleep mid-read | Re-seat the USB cable at both ends, keep the drive on mains power if it has an adapter, then reopen the disc |
| A log block ends with `what to try:` | OpticRip matched the error to a known cause and printed its suggestion | Follow that line; every failure also lists the alternatives it considered |
| No cause is printed for a failure | The exception matched no rule, so it is reported as an unexpected internal error rather than a guess | Turn on debug logging (Preferences) and the full traceback goes to `OpticRip_log.txt` |
| Web UI says `offline` | Server stopped, wrong port, or blocked | Start `python -m opticrip.web`, check the port, and reload |
| `disc:0` is empty in Docker | Drive enumeration is Windows-only | Use `dev:/dev/sr0` with `--device` and the device group |
| Web save fails because FFmpeg is missing | Running the bare server without FFmpeg, or a custom image | Install FFmpeg or build the provided Dockerfile |

## Roadmap

Ordered by value, not promise:

1. **Validate AACS/BD+ against retail media** with a real drive and real
   libraries; record evidence like the DVD capture.
2. **Add CI** (GitHub Actions) running the suite with FFmpeg installed, plus
   a lint/type pass, the web tests and a Docker image build.
3. **Package the project** (`pyproject.toml`, console entry points) so
   `opticrip` works without a checkout.
4. **Add GUI tests** or at least a headless smoke test for the engine event
   pump and settings flow.
5. **Make opening cancellable** by moving `open` to the worker thread with a
   generation token.
6. **Clean partial backups on cancel**, tracking only files created by the
   current run.
7. **Parse CLPI** if a disc shows stream metadata that MPLS does not carry.
8. **Repository hygiene:** `.gitignore`, the removal of tracked bytecode,
   conversation dumps and media fixtures are done. Still open: normalise line
   endings (a few files are CRLF) and choose a license. Purging the removed
   blobs from existing history would need a rewrite and a force-push, and is
   only worth doing if repository size matters.

## Compatibility and clean-room notes

OpticRip is not affiliated with, endorsed by, or derived from MakeMKV source
code. The CLI surface, message ids, robot grammar and observed quirks come
from black-box captures documented in `CLONE_SPEC.md`; each requirement there
is tagged VERIFIED, INFERRED or UNKNOWN. Where behaviour could not be
observed, this README and the spec say so instead of guessing.

The compatible surface is the CLI and its robot output. The Tk UI, the
`guiserver` protocol, the web UI/HTTP API and the `9xxx` message ids are
clone-local.

## License

No license has been chosen yet. Add one before distributing the project or
accepting contributions; until then, default copyright applies.
