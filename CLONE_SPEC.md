# CLONE_SPEC — Clean-room behavioral specification for an optical-disc ripper compatible with MakeMKV 1.18.4 (Windows)

Status: **DRAFT v1** · Study date: 2026-10-29 · Target: `C:\Program Files (x86)\MakeMKV\makemkv.exe`
Method: black-box (execution + observation) and static metadata/string inspection only.

---

## 0. How to read this document

* Every requirement has an ID (`R-<area>-<n>`), a statement, a **Confidence** marker and **Evidence**.
* Confidence values:
  * **VERIFIED** – reproduced by executing the target or read directly from its metadata/strings.
  * **DOCUMENTED** – stated in vendor/third-party documentation (URL given), not reproduced locally.
  * **INFERRED** – consistent with several observations, not directly proven.
  * **UNKNOWN** – not determined. The clone must pick an implementation and must not claim compatibility.
**Contents:** [1 Target](#1-target-identification) · [2 Product](#2-what-the-product-is) · [3 Architecture](#3-architecture) · [4 Distribution](#4-distribution-layout) · [5 CLI](#5-console-interface-makemkvcon) · [6 Robot protocol](#6-robot-mode-protocol-primary-machine-interface) · [7 GUI](#7-gui-screens-and-widgets) · [8 Menus](#8-menus-actions-and-enablement) · [9 Preferences](#9-preferences) · [10 Dialogs](#10-dialogs) · [11 State machine](#11-workflow-state-machine) · [12 Inputs](#12-input-behaviour-and-detection) · [13 MKV output](#13-output-mkv) · [14 Backup](#14-output-disc-backup-verified-by-path-strings) · [15 Profiles](#15-conversion-profiles-mmcp-xml) · [16 Selection](#16-track-selection-language) · [17 Persistence](#17-persistence) · [18 Network](#18-network-behaviour) · [19 Errors](#19-error-model) · [20 Edge cases](#20-observed-edge-cases-and-quirks-use-as-clone-test-vectors) · [21 UNKNOWN](#21-explicit-unknown-register-must-be-resolved-before-claiming-compatibility) · [22 Tests](#22-acceptance-tests-for-the-clone) · [23 Evidence](#23-evidence-index) · [24 Non-goals](#24-deliberate-non-goals)

* Evidence tags used repeatedly:
  * `E:exe+0xNNNN` – file offset in `makemkv.exe`
  * `E:con+0xNNNN` – file offset in `makemkvcon64.exe`
  * `E:run:<file>` – captured console output stored in `analysis/evidence/<file>`
  * `E:uia:<file>` – UI Automation dump in `analysis/evidence/<file>`
  * `E:reg:<file>` – registry snapshot JSON
  * `E:ocr:<file>` – screenshot + OCR of a live window

### 0.1 Clean-room rules used for this study

1. No source code, resource, icon, translation or profile asset is reproduced here. Only observable behaviour, string identifiers, numeric constants and file/registry key names are recorded, because they are required for interoperability.
2. Extracted vendor assets were unpacked **outside** the project tree (`%TEMP%`) and are not part of the deliverable.
3. Observations were made on the user's own installed copy, on their machine, with their consent.

### 0.2 Copyright / DRM boundary (important)

MakeMKV contains licensed, proprietary AACS/BD+ decryption technology and cryptographic material. This specification **does not** and **must not** describe or enable circumvention of content protection. For the clone:

* The decryption subsystem is treated as an **external licensed component** behind an interface (see §3.4).
* Where the target's behaviour depends on that subsystem (volume keys, host certificates, BD+ SVQ execution, bus encryption), the requirement is expressed as "delegate to the licensed component", and internals are marked **UNKNOWN / out of scope**.
* Any clone must obtain its own licence for AACS/BD+ or operate only on unencrypted media.

---

## 1. Target identification

| Item | Value | Evidence |
|---|---|---|
| Product | MakeMKV | version resource |
| Version | `v1.18.4`, GUI string `MakeMKV v1.18.4 win(x64-release)`, window title `MakeMKV BETA` | `E:run:con_list.txt`, `E:ocr:about.png` |
| Vendor | `GuinpinSoft inc`, "Copyright (C) 2007-2025 GuinpinSoft inc" | `E:exe+0x4cfd0`, `E:exe+0x4d09c` |
| GUI executable | PE32 i386 GUI subsystem, image base `0x19000`, entry `0x1a2f6`, 5 sections | `analysis/tools/pe.py --sections` |
| GUI build timestamp | 2026-06-13 22:05:04 UTC (`TimeDateStamp=1781388304`) | PE header |
| GUI SHA-256 | `ce04187005b1ada9ef7ba1c50deaabb897da31e3b6d77a742f7c2db3b818b2c4` | `sha256sum` |
| Console engine | `makemkvcon64.exe`, PE32+ x86-64, 5,423,104 bytes | `file`, PE header |
| Engine SHA-256 | `e160c441c6eaab1870f66606a882d9fad62f6aa6e71a6acbae9067e2ac7a3fb6` | `sha256sum` |
| Core library | `libmakemkv64.dll`, 205,312 bytes, SHA-256 `7d39fed21d197719d6ea61c26eb2fc1682b8083436f1f5cff5b9eeaed36876fe` | `sha256sum` |
| UI toolkit | Qt 5.15.17 (window classes `Qt51517QWindowIcon`, `Qt51517QWindowPopupDropShadowSaveBits`; string `5.15.17` in `libmqt.dll`) | `E:uia:gui_probe_1.txt`, `E:libmqt` strings |

**R-IDN-01 (VERIFIED).** The clone must present itself with a product name, version string and window title of the implementer's choosing; the *format* of the version string is `MakeMKV v<major>.<minor>.<patch> win(<arch>-release)` when emulating log text.

---

## 2. What the product is

A Windows desktop application that:

1. Reads optical media (DVD-Video, Blu-ray, HD-DVD, AVCHD/BDAV), disc images (`.iso`), folders containing disc file systems, and `.mkv` files.
2. Discovers "titles" (playable clips) and their tracks (video/audio/subtitle/attachment/chapter).
3. Lets the user choose titles/tracks and remuxes them into Matroska (`.mkv`) files.
4. Alternatively copies a disc 1:1 to a directory/ISO ("backup"), optionally decrypting.
5. Exposes the same engine through a console program with a machine-readable "robot" protocol.
6. Acts as a decryption engine (`libmmbd`) for third-party players (integration feature).
7. Can run a local streaming server for a disc (feature present; protocol UNKNOWN).

Sources: menu/action catalogue `E:con+0x2be3c0..0x2bf3b0`, `E:uia:gui_probe_1.txt`, `E:run:con_help.txt`.

---

## 3. Architecture

### 3.1 Component map (VERIFIED)

```
makemkv.exe  (GUI, 32-bit, Qt 5.15.17 via libmqt.dll)
 ├─ spawns: makemkvcon64.exe  "xxx guiserver A0001+shm"
 │     └─ named pipe  \\.\pipe\LOCAL\makemkv-%08x-%08x-%02x
 ├─ libmqt.dll        Qt runtime + Windows integration (5.9 MB)
 └─ .rsrc/BIN/20      108,841,056-byte opaque blob (purpose UNKNOWN)

makemkvcon64.exe  (engine front-end, console-capable)
 ├─ libmakemkv64.dll  core ("MakeMKV multiplexer library")
 ├─ libdriveio64.dll  raw optical drive I/O + SDF invocation
 ├─ libffm64.dll      FFmpeg-derived codec/mux support
 ├─ libmmbd64.dll     decryption engine shim for 3rd-party apps
 └─ sdftool64.exe     external firmware tool (invoked by `f` command)
```

Evidence:
* `makemkv.exe` imports **only** `libmqt.dll` by ordinal; `.rsrc` contains `BIN/20` (size 108,841,056 at offset `0x4d198`) plus 5 icons, a manifest and version info (`analysis/tools/rsrc.py`).
* Running `makemkv.exe` starts `makemkvcon64.exe`; its command line observed as `xxx guiserver A0001+shm` (`Get-CimInstance Win32_Process`). Format string `E:exe+0x1f47c` `"xxx guiserver "` and pipe name `E:exe+0x1f430` `\\.\pipe\LOCAL\makemkv-%08x-%08x-%02x`.
* Failure strings `CANT_LOCATE_MAKEMKVCON` (`E:exe+0x18ab0`) and `NO_ANSWER_FROM_MAKEMKVCON` (`E:exe+0x18af4`); the GUI also resolves `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\makemkvcon.exe` (`E:exe+0x1f318`).
* Engine's own helper modes are visible as strings `guiserver`, `guimsg`, `mmbd-install-helper`, `sdftool`, `makemkvcon f` (`E:con+0x2b9698..0x2b96b0`).

### 3.2 Requirement

**R-ARCH-01 (VERIFIED).** The clone must be split into a UI process and an engine process that can also run headless, because the CLI and the GUI share all disc logic and message texts.

**R-ARCH-02 (VERIFIED).** Engine↔UI transport must support: progress events, log/message events, drive list, disc open result, title/stream metadata, and cancellation. The target uses a per-instance named pipe whose name embeds two 32-bit ids and a small integer (likely PID, a counter/session id, and a channel index). Exact pipe framing is **UNKNOWN**.

**R-ARCH-03 (INFERRED).** Multiple GUI instances are allowed (action `New MakeMKV instance`, "Start second copy of MakeMKV application", `E:con+0x2be6e0`), so the IPC endpoint must be per-instance.

### 3.3 Startup sequence (VERIFIED, GUI)

1. GUI process starts, creates its main window (`MainWnd` accessibility proxy; `Qt51517QWindowIcon`).
2. Engine process is spawned with `guiserver`.
3. Log pane receives `MakeMKV v1.18.4 win(x64-release) started` (MSG id 1005).
4. Drive enumeration runs; with no usable drive the log receives `The program can't find any usable optical drives.` (MSG 5042) and 16 `DRV:` records with state 256 are produced in robot mode.
   * `E:run:con_list.txt`, `E:uia:gui_probe_1.txt` (CLogText value).

### 3.4 Decryption subsystem boundary (out of scope)

**R-DRM-01 (INFERRED).** All CSS/AACS/BD+ handling must sit behind one interface so that a licensed implementation can be dropped in. User-visible contract: on success the disc opens normally; on failure the user sees one of the messages in §19.2 (`The volume key is unknown for this disc - video can't be decrypted`, `Can't read AACS VID from disc - most likely current AACS host certificate is revoked by your drive`, `AACS-protected discs that use SKB are not supported`).

**R-DRM-02 (UNKNOWN).** AACS/BD+ key exchange, MKB handling, host-certificate selection, BD+ VM execution and SVQ contents are not specified here.

---

## 4. Distribution layout

Observed installation directory (program files) contains:

| File | Role |
|---|---|
| `makemkv.exe` | GUI |
| `makemkvcon.exe`, `makemkvcon64.exe` | console engine (32/64-bit) |
| `libmakemkv{,64}.dll` | core library |
| `libmqt.dll` | Qt 5.15.17 runtime for the GUI |
| `libdriveio{,64}.dll` | drive I/O |
| `libffm{,64}.dll` | codec/mux |
| `libmcurl{,64}.dll` | HTTP client (not a static import of the engine; loaded on demand / by other modules) |
| `libmmbd{,64}.dll` | decryption engine for 3rd-party apps |
| `mmccextr{,64}.exe` | closed-caption extractor (external helper) |
| `mmgplsrv{,64}.exe` | helper service |
| `sdftool{,64}.exe` | firmware/SDF tool |
| `appdata.tar` | translations + profiles + SDF payload |
| `blues.jar`, `blues.policy` | Java component (BD-J/BD+ helper), `AllPermission` policy file observed |
| `License.rtf`, `Readme.rtf`, `MakeMKV.url`, `uninst.exe` | docs / shortcut / uninstaller |
| `HL-DT-ST-BD-RE_BU40N-1.03-NM00000-*.bin` | example firmware dump shipped with this install |

`appdata.tar` (20 entries, VERIFIED by `tar -tvf`): 14 `makemkv_<lang>.mo.gz` translation catalogues; `default.mmcp.xml`, `flac.mmcp.xml`, `wdtv.mmcp.xml`, `aac-st.mmcp.xml` conversion profiles; `sdf_000000a5.bin`; `--index-<sha1>.bin`.

**R-DIST-01 (VERIFIED).** Profiles and translations must be loadable from an archive next to the binaries, with user profiles able to override.

**R-DIST-02 (VERIFIED).** Interface languages shipped: deu, jpn, spa, ptb, dut, swe, nor, ita, chi, pol, dan, fra, per (Persian), tur + English (source strings are English).

---

## 5. Console interface (`makemkvcon`)

### 5.1 Usage text (VERIFIED, condensed; line breaks normalised)

```
Use: makemkvcon [switches] Command [Parameters]

Commands:
  info <source>            prints info about disc
  mkv <source> <title id> <destination folder>   saves a single title to mkv file
  backup <source> <destination folder>           backs up disc to a hard drive
  f <args>                 run universal firmware tool
  reg <key string or file name>                  enter registration key into program

Source specification:
  iso:<FileName>    - open iso image <FileName>
  file:<FolderName> - open files in folder <FolderName>
  disc:<DiscId>     - open disc with id <DiscId> (see list Command)
  dev:<DeviceName>  - open disc with OS device name <DeviceName>

Switches:
  -r --robot        - turn on "robot" mode, see http://www.makemkv.com/developers
```

Evidence: `E:run:con_help.txt`, `E:con+0x2b9180` region.

**R-CLI-01 (VERIFIED).** Every command accepts the four source prefixes. `backup` additionally *requires* `disc:` (see §20, item 8).

**R-CLI-02 (VERIFIED, quirk).** The usage text mentions "(see list Command)" but `list` is **not** a valid command (`Invalid command: list`, exit 127). The clone should either implement `list` or reproduce the same inconsistency.

**R-CLI-03 (VERIFIED).** Unknown options are rejected by the platform option parser before any engine work:
`<path>: unrecognized option: h` followed by the usage block; exit code 1. `-stdout` is likewise rejected (`unrecognized option: s`) although the strings `-stdout`,`-stderr`,`-null`,`-same` exist in the binary (`E:con+0x2b9158`).

**R-CLI-04 (VERIFIED).** Robot output goes to **stdout**; stderr stays empty (`2>/dev/null` produced all lines; `1>/dev/null` produced none).

**R-CLI-05 (VERIFIED).** Exit codes (see full table §19.3): 0 success, 1 usage/key error, 2 firmware-tool argument error, 10 disc/source open error, 11 save error, 127 unknown command/unhandled source.

**R-CLI-06 (DOCUMENTED).** Official robot documentation: <https://www.makemkv.com/developers/usage.txt> (linked from <https://www.makemkv.com/developers/>).

---

## 6. Robot mode protocol (primary machine interface)

### 6.1 Line grammar (VERIFIED from format strings)

| Line | Format string in binary | Observed example |
|---|---|---|
| `MSG` | `MSG:%u,%u,%u,"` | `MSG:5010,0,0,"Failed to open disc","Failed to open disc"` |
| `DRV` | `DRV:%u,%u,%u,%u,"` | `DRV:0,256,999,0,"","",""` |
| `TCOUNT` | `TCOUNT:%u` (and literal `TCOUNT:0`) | `TCOUNT:0` |
| `CINFO` | `CINFO:%u,%u,"` | not observed (needs open disc) |
| `TINFO` | `TINFO:%u,%u,%u,"` | not observed |
| `SINFO` | `SINFO:%u,%u,%u,%u,"` | not observed |
| `PRGC` | `PRGC:%u,%u,"` | not observed |
| `PRGT` | `PRGT:%u,%u,"` | not observed |
| `PRGV` | `PRGV:%u,%u,%u` | not observed |

Evidence: `E:con+0x2b9128` (MSG), `0x2b9140` (DRV), `0x2b9488` (CINFO), `0x2b9498` (TINFO), `0x2b94b0` (SINFO), `0x2b9468/0x2b9478` (TCOUNT), `0x2b90c8` (PRGC), `0x2b90b0` (PRGT), `0x2b9110` (PRGV).

**R-ROB-01 (VERIFIED).** Strings are quoted with `"`; embedded quotes/backslashes are escaped ("backlash-escaped" per official text). Robot mode is line-buffered and flushed per line.

### 6.2 MSG fields

`MSG:<code>,<flags>,<count>,"<rendered>","<format>",<param>…`

* `code` – stable numeric message id (e.g. 1005 startup banner, 5042 no drives, 5010 open failed, 2003 read error, 5006 file missing, 3006 opening files, 3305 AACS dir absent, 2024 unknown device).
* `flags` – bitmask. Observed values: `0`, `131072` (`0x20000`, message carries a URL / `AP_UIMSG_HAVE_URL`), `16777216` (`0x1000000`, error). DOCUMENTED constant set (from a public `apdefs.h` copy): `AP_UIMSG_DEBUG=32`, `HIDDEN=64`, `EVENT=128`, `HAVE_URL=131072`, `BOXOK=260`, `BOXERROR=516`, `BOXWARNING=1028`, `BOXYESNO=776`, `BOXYESNO_ERR=1288`.
* `count` – **number of parameters**, not severity (verified: a message with three args has `3`).
* `rendered` – final localized text; `format` – localized template; params follow.

**R-ROB-02 (VERIFIED).** Consumers must use `code` + params, never parse `rendered`.

**R-ROB-03 (INFERRED).** Byte-identical reproduction of all message ids requires a catalogue; ids observed in this study are listed in §19.2. Unobserved ids are **UNKNOWN**.

### 6.3 DRV fields

`DRV:<index>,<flags/state>,<code>,<visible/enabled>,<drive name>,<disc name>`

Observed with no drive: `DRV:0,256,999,0,"","",""` for indices 0–15.
From public `apdefs.h`: `AP_DskFsFlagDvdFilesPresent=1`, `HdvdFilesPresent=2`, `BlurayFilesPresent=4`, `AacsFilesPresent=8`, `BdsvmFilesPresent=16`.

**R-ROB-04 (VERIFIED).** 16 slots are always emitted, empty when no drive exists; index 0..15 maps to `disc:<id>` / `dev:<name>` openings.

**R-ROB-05 (UNKNOWN).** Meaning of the third field (`999` observed when absent) and of the 4th field are not confirmed.

### 6.4 CINFO/TINFO/SINFO (metadata records)

Format strings prove arity: `CINFO` = 2 ints + string; `TINFO` = 3 ints + string; `SINFO` = 4 ints + string.

**R-ROB-06 (INFERRED, cross-checked with DOCUMENTED samples).** Field layout:
* `CINFO:<attrId>,<msgCode>,"<value>"`
* `TINFO:<titleIndex>,<attrId>,<msgCode>,"<value>"`
* `SINFO:<titleIndex>,<streamIndex>,<attrId>,<msgCode>,"<value>"`
Community examples (`TINFO:0,2,0,"Breaking Bad: Season 1: Disc 1"`, `SINFO:3,0,1,6201,"Video"`) match this arity. The official usage text lists only `id,code,value`, so the leading indices are undocumented.

Attribute ids (DOCUMENTED, public `apdefs.h` copy) — the ones a clone needs most:

| id | name | id | name |
|---:|---|---:|---|
|1|Type|22|Stream flags|
|2|Name|23|Date/time|
|3|Language code|24|Original title id|
|4|Language name|25|Segment count|
|5|Codec id|26|Segment map|
|6|Codec (short)|27|Output file name|
|7|Codec (long)|28/29|Metadata language code/name|
|8|Chapter count|30|Tree info|
|9|Duration|31|Panel title|
|10/11|Size (text/bytes)|32|Volume name|
|12|Stream type extension|33|Order weight|
|13|Bitrate|34/35|Output format (name/description)|
|14|Audio channels|36|Seamless info|
|15|Angle info|37|Panel text|
|16|Source file name|38/39|MKV flags (value/text)|
|17/18|Audio sample rate/size|40|Channel layout name|
|19|Video size|41|Output codec (short)|
|20|Aspect ratio|42|Output conversion type|
|21|Frame rate|43..48|Output audio properties|
|49|Comment|50|Offset sequence id|

Stream-flag bits (DOCUMENTED): 1 director's comments, 2 alternate director's comments, 4 visually impaired, 256 core audio, 512 secondary audio, 1024 has core audio, 2048 derived stream, 4096 forced subtitles, 16384 profile secondary stream, 32768 offset sequence id.

**R-ROB-07 (VERIFIED).** The GUI uses the same catalogue for its human-readable flag labels: `Director's comments`, `Alternate director's comments`, `Secondary low-bitrate audio`, `For visually impaired`, `Core audio`, `Forced subtitles`, `Profile-defined secondary stream` (`E:con+0x2bf198..0x2bf230`).

### 6.5 Progress lines

* `PRGV:<current>,<total>,<max>` – two running counters plus a constant maximum (percentages).
* `PRGC:<code>,<id>,"<action name>"` – current action; `PRGT:<code>,<id>,"<operation name>"` – current operation.
* Non-robot equivalents: `Current operation: %s`, `Current action: %s`, `Current progress - %u%%  , Total progress - %u%%` (`E:con+0x2b9080..0x2b9110`).

**R-ROB-08 (VERIFIED, corrected 2026-10-29).** A full `-r mkv` rip of a real disc emitted **only `MSG` and `DRV` lines — no `PRGV`/`PRGC`/`PRGT` at all** (`E:run:rip_title0.txt`, 131 MSG + 16 DRV). The progress records exist but are not produced by the console `mkv` command; treat their emission as GUI-only until proven otherwise. Titles shown during work come from the catalogue: `Scanning contents`, `Processing title sets`, `Processing titles`, `Decrypting`, `Processing advanced titleset`, `Loading playlists`, `Processing BD+ code`, `Saving …`.

### 6.6 Human-readable console output (non-robot)

Without `-r`, output is plain lines to stdout, e.g.:

```
MakeMKV v1.18.4 win(x64-release) started
The program can't find any usable optical drives.
Backup source must start with "disc:"
```

and per-title summaries `Total %d titles`, `Title %2d`, `%u %s %s` (`E:con+0x2b94c8..0x2b9500`).

### 6.7 Data model implied by the protocol (INFERRED, internally consistent)

```
Disc
 ├─ attributes            (CINFO: type, name, volume name, metadata language, size…)
 ├─ drives[]              (DRV: index, state, media flags, drive name, disc name)
 └─ titles[]
     ├─ attributes        (TINFO: name, duration, size, chapters, segments, output name, angles)
     ├─ chapters[]        (start/end, optional chapter number/label)
     ├─ attachments[]     (fonts and other Matroska attachments)
     └─ streams[]
         ├─ kind          (video | audio | subtitle)
         ├─ attributes    (SINFO: codec, language, channels, bitrate, dimensions, fps, flags)
         └─ output spec   (conversion + selection state + MKV flags)
```

Evidence: record arities in §6.4, GUI column vocabulary in §7.2, and catalogue strings `Segment count`, `Segment map`, `Angle info`, `Chapter count`, `Attachment` (`E:con+0x2be098..0x2bf388`).

**R-MODEL-01 (INFERRED).** Requirements: a title can carry multiple angles (same content, different interleave) and multiple segment maps (seamless branching); a stream can be marked forced/core/secondary/commentary; chapters are per-title, attachments are per-title.

---

## 7. GUI: screens and widgets

### 7.1 Main window (VERIFIED)

Default size 566×630 (observed). Widgets found via UI Automation (`E:uia:gui_probe_1.txt`):

| Region | Widgets |
|---|---|
| Menu bar | `File`, `View`, `Help` |
| Toolbar (`MainToolbar`) | `Open files`, `Backup`, `Save selected titles`, `Preferences`, `Eject disc` |
| Source group | labels `Type :`, `Label :`, `Protection :`, source combo (`QComboBox`, disabled until sources exist) |
| Info group | read-only `QTextEdit` (`Info`) with drive/disc summary |
| Log pane | `CLogText` (read-only text view, selectable) |
| Centre/bottom | large drop-target button whose caption reflects state: `No disc inserted`, `Disc is loading`, `Disc can't be opened` (`E:con+0x2be750..0x2be778`) |
| Status bar | `QStatusBar` |
| Splitter | `QSplitter` with handle; view state persisted in `screen_state` |

**R-UI-01 (VERIFIED).** The window must support a log pane, a source/info pane, a title/track tree, a toolbar and a status bar, arranged by a splitter whose layout is restored across runs.

**R-UI-02 (VERIFIED by strings; live state only `No disc inserted` was observed).** The empty state must show a prominent click target labelled `No disc inserted`; while opening it becomes `Disc is loading`; on failure `Disc can't be opened` (`E:con+0x2be738..0x2be778`).

### 7.2 Title/track view (VERIFIED by strings; not populated without a disc)

Column/row vocabulary (`E:con+0x2bf300..0x2bf3a8`):
`Title`, `Audio`, `Subtitles`, `Attachment`, `Chapters`, `Chapter`, `Description`, `(forced only)`, ` (forced only)`.
Chapter/angle decorations: `%1 chapter(s)`, `(angle %1)`, `%1 cell(s)`, `%1 PGC(s)`.
Media type labels: `DVD disc`, `Blu-ray disc`, `HD-DVD disc`, `MKV file`, `Data`, `No disc`, `UNNAMED DISC`.

**R-UI-03 (VERIFIED).** Each title row must show: title index/name, duration, size, chapter count, and per-track rows for video/audio/subtitle with codec, language, channels, flags; subtitles may be flagged "forced only"; angles appear as variants of a title.

### 7.3 Info panel text (VERIFIED from the string table)

The info panel is an HTML-ish block built from these labels (`E:con+0x2bf660..0x2bf858`):

```
<b>Drive Information</b><br>
Manufacturer: , Product: , Revision: , Serial number: ,
Firmware details: , Firmware date: , Bus encryption flags: ,
Highest AACS version:
<br><b>Disc Information</b><br>
Disc type: , Data capacity: , Disc size: , Maximum read rate: ,
Number of layers: , Channel bit length: , OS device name:
<br><b>No disc inserted</b><br>   /  Disc is being loaded<br>
```
Layer descriptions: `74,5 nm (25.0 GB max. per layer)`, `69,0 nm (27.0 GB max. per layer)`.

Popup blocks for context help: `<b>Source information</b><br>`, `<b>Title information</b><br>`, `<b>Track information</b><br>`, `<b>Attachment information</b><br>`, `<b>Chapter information</b><br>`.

**R-UI-04 (VERIFIED).** Drive info must include manufacturer/product/revision/serial/firmware date/bus-encryption flags/highest AACS version; disc info must include type, capacity, size, read rate, layers, channel bit length, OS device name.

### 7.4 Status/progress

* `Elapsed: %1`
* `Elapsed: %1 / Remaining : %2`
* `Current operation: %s`, `Current action: %s`
* `Current progress - %u%%  , Total progress - %u%%`

(`E:con+0x2be390`, `0x2be3a0`, `0x2b9080`, `0x2b90b0`, `0x2b90d8`.)

**R-UI-05 (VERIFIED).** Long operations must show an operation title, a current-action title, two progress bars (current/total), elapsed time and ETA.

---

## 8. Menus, actions and enablement

### 8.1 File menu (VERIFIED by OCR + UIA + string catalogue)

| Item | Shortcut | Enabled with no disc | Tooltip/description string |
|---|---|---|---|
| `Open files` | `Ctrl+O` | yes | `Opens video files (disc image on harddrive)` |
| `Open disc` | — | **no** | `Open DVD disc` / `Open HD-DVD disc` / `Open Blu-ray disc` |
| `Close` | — | **no** | `Close opened video files` |
| `Eject disc` | — | **no** | `Open drive tray` |
| `Set output folder` | — | **no** | `Set folder where output files would be written` |
| `Save selected titles` | — | **no** | — |
| `Backup...` | — | **no** | `Backup disc` |
| `Quit` | `Ctrl+Q` | yes | `Quit application` |

Additional file-related actions present in the catalogue but not on the observed menu: `Open DVD files manually` ("Opens DVD files (disc image on harddrive) in manual mode"), `New MakeMKV instance` ("Start second copy of MakeMKV application"), `S&tream` ("Start streaming server"), `&Cancel` ("Cancel current operation"), `Revert` ("Revert to original value").

Evidence: `E:ocr:filemenu.png`, `E:uia:gui_probe_1.txt`, `E:con+0x2be3c0..0x2be6e0`.

### 8.2 View menu (VERIFIED)

`Toolbar` (checkable), `Clear log`, `Preferences`.

### 8.3 Help menu (VERIFIED)

`Online Help` ("Open Online help page"), `About` ("Show the application's About box"), `Purchase` ("Open purchase order page"), `Register` ("Enter registration code").

### 8.4 Context menu on titles (VERIFIED by strings)

`&Toggle item`, `&Select all`, `&Unselect all` (`E:con+0x2be7a0..0x2be7b0`).

### 8.5 Enablement matrix (VERIFIED, no-disc state)

Toolbar: `Open files` ✔, `Preferences` ✔, `Backup` ✘, `Save selected titles` ✘, `Eject disc` ✘.
File menu: only `Open files` and `Quit` enabled.
View: all enabled. Help: all enabled.

**R-UI-06 (VERIFIED).** Commands that require an open disc must be disabled, not hidden, in the no-disc state.

**R-UI-07 (VERIFIED with a disc, 2026-10-29).** With a DVD open: `Close`, `Eject disc`, `Set output folder`, `Save selected titles` are enabled, as are the `Save`/`Eject` toolbar buttons. **`Backup...` stayed disabled** for this disc even in the ready state (see §25.5) — do not assume it always enables.

### 8.6 Open disc submenu (INFERRED)

The catalogue contains `Open DVD disc`, `Open HD-DVD disc`, `Open Blu-ray disc` next to the `No disc inserted` / `Disc is loading` states. `File > Open disc` is therefore expected to expand into a per-drive list annotated with media type, disabled when no usable drive exists (observed disabled). The exact item order is **UNKNOWN**.

---

## 9. Preferences

Three tabs (`Video`, `General`, `IO`) plus groups (`Options`, `Default destination`, `Miscellaneous`, `Online updates`, `Language`, `Protection`, `Integration`, `Streaming`, `Advanced`, `Profile`). Dialog titled `MakeMKV preferences`, buttons `OK`, `Cancel`, `Apply`.

Evidence: `E:ocr:prefs_general_4x.png`, `E:ocr:prefs_tab2.png`, `E:ocr:prefs_video2.png`, catalogue `E:con+0x2be960..0x2bfa20`.

### 9.1 Visible controls (VERIFIED by OCR of live dialog)

| Tab | Control | Observed value | Persistence key (INFERRED from name) |
|---|---|---|---|
| General | `Default destination` radio group | `None`, `Auto`, `SemiAuto`, `Custom` | `app_DestinationType` |
| General | `Minimum title length (seconds) :` | `120` | **UNKNOWN key** (not present in string list) |
| General | `Options` group | checkboxes | — |
| General | `Language` → `Interface language :` | combo | `app_InterfaceLanguage` |
| General | `Language` → `Preferred language :` | combo (`Auto`/`None` seen) | `app_PreferredLanguage` |
| Video | `Read retry count :` | `5` | **UNKNOWN key** |
| Video | `Read buffer size (MB) :` | `Auto` | **UNKNOWN key** |
| Video | `Ask for single drive mode :` | checkbox | **UNKNOWN key** |
| Video | `Disable direct access :` (Windows catalogue) | checkbox | **UNKNOWN key** |
| Video | `DVD structure protection removal method :` | combo | **UNKNOWN key** |
| Video | `Mac OS X *_KEY_V2 bug workaround type :` (string exists) | — | **UNKNOWN key** |
| IO | `MakeMKV data directory` | `C:\Users\<user>\.MakeMKV` | `app_DataDir` |
| IO | `Log debug messages :` | checkbox | `app_ShowDebug` |
| IO | `Expert mode :` | checkbox | `app_ExpertMode` |
| IO | `Show AV synchronization messages :` | checkbox | `app_ShowAVSyncMessages` |
| IO | `Online updates` → `Enable Internet access :` | checkbox | `app_UpdateEnable` |
| IO | `Submit dumps automatically :` | checkbox | `app_AutoSubmitEnabled` |
| IO | `Proxy server :` | text | `app_Proxy` |
| General/Protection | `Always create BD+ dumps :` | checkbox | **UNKNOWN key** |
| Protection | `Key files directory` | path | **UNKNOWN key** |
| Advanced | `Default profile :` | combo | `app_DefaultProfileName` |
| Advanced | `Default selection rule :` | text | `app_DefaultSelectionString` |
| Advanced | `Output file name template :` | text | `app_DefaultOutputFileName` |
| Advanced | `<X> executable location` (Java, ccextractor) | path | `app_Java`, `app_ccextractor` |
| Advanced | `Custom Java executable location` | path | **UNKNOWN key** |
| Streaming | streaming server settings | — | **UNKNOWN** |
| Integration | libmmbd integration per application, path list | — | **UNKNOWN** |

**R-PREF-01 (VERIFIED).** Settings must be applied without restart except where explicitly stated; the target shows `Some settings will apply next time you start the application.` (`E:con+0x2bece8`).

**R-PREF-02 (VERIFIED).** Failure to persist must be surfaced as `Failed to save settings` (`E:con+0x2becd0`).

**R-PREF-03 (VERIFIED).** Pressing `Apply` with no changes persists at least `app_ccextractor`, `app_DestinationDir`, `app_Java`, `app_Proxy` as empty strings on a fresh profile (`E:reg:reg_after_apply.json`).

### 9.2 Integration text (user-facing, VERIFIED)

The Integration page explains that MakeMKV can act as a decryption engine for third-party applications via `libmmbd`, globally or per application, and links to a help page (`E:con+0x2bee30`).

---

## 10. Dialogs

### 10.1 About (VERIFIED, `E:ocr:about.png`)

Title `About MakeMKV BETA`; content lines:

```
MakeMKV v1.18.4 win(x64-release)
GUI version: v1.18.4
License type : Time-limited
Evaluation expires : Fri Oct 30 2026 (0 day(s))
This version expires : Fri Oct 09 2026 (0 day(s))
Website URL:
Latest available version:            (only after an update check)
```

**R-DLG-01 (VERIFIED).** About must show program version, GUI version, licence type, evaluation expiry, build expiry, website link, and an update-check result line when available.

### 10.2 Register (VERIFIED, `E:ocr:win1.png` + strings)

Title `Enter registration code`. Body text:

```
Please enter the registration (license) key.
You can also paste entire registration email message,
load any text file containing a key, or drag it onto this window.
License key : <text field>
License type : <Time-limited | Invalid | ...>
License expiration : <dd.MM.yyyy | empty>
[Purchase] [Cancel] [OK]
```

Behaviour verified by typing `BOGUS123` + Enter: `License type : Invalid`, expiration cleared, dialog stays open, no modal error, no network traffic.
Catalogue messages: `Registartion code is incorrect` *(sic)*, `Failed to store registration code in settings database`, `Thank you. Please restart the application for changes to take effect.`, `Key not found or invalid`, `Registration key saved.`

**R-DLG-02 (VERIFIED).** Key validation is local/offline; entering an invalid key never blocks the UI and never reports a network error.

**R-DLG-03 (VERIFIED).** A key may be pasted as a whole email body or loaded from a file; the field accepts drag-and-drop of a file.

### 10.3 Open files (VERIFIED, native Windows dialog)

Title: `Select existing video source file`.
File-type filter (exact string from the live dialog):

```
All video files (VIDEO_TS.IFO;DISCID.DAT;HVA00001.VTI;MKBROM.AACS;index.bdmv;INDEX.BDM;MovieObject.bdmv;MOVIEOBJ.BDM;info.bdav;BDMV;BDAV;MKB_RO.inf;discatt.dat;*.iso;*.mkv)
```

plus per-family filters built from `All video files (%1);;Dvd-video files (%2);;Blu-ray files (%3);;Hd-dvd files (%4)` and `%1 files (%2)`.

**R-IN-01 (VERIFIED).** The open dialog must accept: ISO images, MKV files, DVD-Video folders (`VIDEO_TS.IFO`), Blu-ray folders (`index.bdmv`, `MovieObject.bdmv`, `MKB_RO.inf`), BDAV (`info.bdav`), HD-DVD (`HVA00001.VTI`), AACS metadata (`MKBROM.AACS`), AnyDVD/DvdFab artefacts (`DISCID.DAT`, `discatt.dat`).

### 10.4 Set output folder (VERIFIED by strings)

Title `Select output folder`; label `Output folder`; persisted as `path_DestDir` and MRU `path_DestDirMRU`.

### 10.5 Backup (VERIFIED by strings; unreachable without a disc)

Title `MakeMKV disc backup`. Body text (three paragraphs): backup to a directory; choose whether to save data as-is without decrypting/changing files, or decrypt AV files and change metadata; in all cases the produced backup may later be opened by the application. Prompt `Please specify output directory`. Checkbox `Decrypt video files`. Persisted as `app_BackupDecrypted` and MRU `path_BackupDirMRU` (observed value format `*X:/Video/backup/OVER_THE_HEDGE.iso` — a leading `*` marks the last-used item).

**R-OUT-01 (INFERRED).** Backup output must be a folder containing a 1:1 copy of the disc file system, optionally decrypted, and must be re-openable as a source.

### 10.6 Single drive mode (VERIFIED by strings)

Shown when more than one optical drive exists: "You have more than one optical drive. Which drive do you want this instance of MakeMKV to work with?" with choices `All drives` / `Select the drive`. Controlled by the `Ask for single drive mode` preference.

### 10.7 Manual DVD mode (VERIFIED by strings)

Title `Open DVD in manual mode`; explanatory text (advanced knowledge of DVD layout, precise selection, exact title order, split by chapters/cells, force retention, syntax help link). Entry syntax itself is **UNKNOWN** (documented publicly at makemkv.com/manualdvd/).

### 10.8 Message boxes and fatal errors (VERIFIED)

* Generic message box window title: `MakeMKV BETA popup`.
* Cancel confirmation: `Are you sure you want to cancel the current operation?`
* Fatal: `Fatal error occurred, program will now exit.` and `Fatal error - there is not enough memory for application to continue, program will now exit.`

Observed live: opening a valid `.mkv` produced the fatal box (§20, item 6).

---

## 11. Workflow state machine

```
                 ┌──────────────┐
                 │  NoSource    │◄──────────── Close / eject / error
                 └──────┬───────┘
        Open files/disc │
                        ▼
                 ┌──────────────┐   cancel   ┌───────────┐
                 │  Opening     │───────────►│ Cancelled │
                 │ (loading)    │            └───────────┘
                 └──────┬───────┘
             success    │     failure
                        ▼            ▼
                 ┌──────────────┐  ┌──────────────┐
                 │   Ready      │  │  OpenFailed  │
                 │ (titles)     │  │ (retry/close)│
                 └──────┬───────┘  └──────────────┘
       select titles    │
                        ▼
                 ┌──────────────┐  cancel  ┌───────────┐
                 │   Saving     │─────────►│ Cancelled │
                 │ (progress)   │          └───────────┘
                 └──────┬───────┘
             done       │      error
                        ▼          ▼
                 ┌──────────┐  ┌────────────┐
                 │  Saved   │  │ SaveFailed │
                 └──────────┘  └────────────┘
```

**R-FSM-01 (VERIFIED).** States are observable through the large central button (`No disc inserted` → `Disc is loading` → …), the log pane, and toolbar enablement.

**R-FSM-02 (VERIFIED).** Cancellation is confirmed with a modal question and must return to the previous ready state.

**R-FSM-03 (VERIFIED).** While saving, the window must show operation/action titles, current/total progress, elapsed and remaining time.

**R-FSM-04 (UNKNOWN).** Exact retry policy after `Disc can't be opened`, and whether the engine re-enumerates drives automatically, are not determined.

---

## 12. Input behaviour and detection

### 12.1 Source prefixes (VERIFIED)

`iso:<file>` (image), `file:<folder>` (disc files on disk), `disc:<n>` (drive index), `dev:<device>` (OS path, e.g. `E:`).

**R-IN-02 (VERIFIED).** `file:` with a folder containing an MKV is **not** opened as an MKV (exit 10, no message); MKV input only happens through the GUI's explicit file selection.

**R-IN-03 (VERIFIED).** Relative paths are converted to `file://` URIs in messages: `Opening files on harddrive at file://analysis/fixtures/fakebd` (`E:run:con_open_fakebd.txt`).

### 12.2 Detection order (INFERRED from messages and filter)

1. If the path is a directory: look for `BDMV/index.bdmv`, `BDMV/MovieObject.bdmv`, `BDAV/info.bdav`, `VIDEO_TS/VIDEO_TS.IFO`, `HVA00001.VTI`, `MKBROM.AACS`, `MKB_RO.inf`.
2. If an image/device: probe ISO9660/UDF; report `File system not recognized, disc can't be opened` when neither is present.
3. For Blu-ray, read AACS metadata; absence yields `AACS directory not present, assuming unencrypted disc`.
4. For MKV files (GUI only): `Opening MKV file`.

**R-IN-04 (VERIFIED).** A disc with no AACS directory must be treated as unencrypted, not as an error.

### 12.3 Observed open outcomes (VERIFIED)

| Input | Output | Exit |
|---|---|---|
| `iso:` 1 MB of zeros | `MSG:2003 … at offset '1048576'`, `MSG:5010 Failed to open disc`, `TCOUNT:0` | 0 |
| `iso:` missing file | `MSG:5006 File nope.iso does not exist`, `MSG:5010`, `TCOUNT:0` | 0 |
| `file:` empty folder | 16 DRV lines then nothing | 10 |
| `file:` minimal fake BDMV | `MSG:3006 Opening files on harddrive at file://…`, `MSG:3305 AACS directory not present…`, `MSG:5010`, `TCOUNT:0` | 0 |
| `file:` folder containing only an MKV | 16 DRV lines then nothing | 10 |
| `file:` path to an `.mkv` file | `MSG:3006`, then nothing | 127 |
| `dev:E:` | `MSG:2024 Unknown device - 'E:'`, `MSG:5010` | 0 |
| `disc:0` (no drives) | `MSG:5010`, `TCOUNT:0` | 0 |

---

## 13. Output: MKV

**R-OUT-02 (VERIFIED).** Output is Matroska (`.mkv`) written by the bundled FFmpeg-derived muxer; per-track conversion is profile-driven (§15).

**R-OUT-03 (VERIFIED).** Default output-name template (`E:con+0x2b9700`):

```
{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}
```

producing names like `title_t00.mkv`, `title_t01.mkv` (documented community examples agree).

**R-OUT-04 (VERIFIED).** Before writing, the engine logs `Saving %1 titles into directory %2 using profile '%3' from file '%4'` (`E:con+0x2bcd20`).

**R-OUT-05 (VERIFIED by strings).** MKV flags supported by profiles: `ignoreForcedSubtitlesFlag`, `useISO639Type2T`, `setFirstAudioTrackAsDefault`, `setFirstSubtitleTrackAsDefault`, `setFirstForcedSubtitleTrackAsDefault`, `insertFirstChapter00IfMissing`; per-track `compression="zlib" compressionLevel="9"`.

**R-OUT-06 (VERIFIED).** Chapter handling: chapters are written into the MKV; a leading chapter 00 can be inserted when missing; chapter counts are shown in the UI as `%1 chapter(s)`.

**R-OUT-07 (UNKNOWN).** Exact muxer settings (timecodes, attachments naming, 3D/MVC handling, `M2TS hash check`) are not specified beyond message strings.

---

## 14. Output: disc backup (VERIFIED by path strings)

Backup directory layout (paths observed in the binary):

```
/MAKEMKV/                       auxiliary data directory
/MAKEMKV/discattd.dat
/MAKEMKV/AACS/Unit_Key_RO.inf
/MAKEMKV/BDMV/BACKUP/%s
/MAKEMKV/BDSVM/                 BD+ virtual machine data
/MAKEMKV/BIN/%05u.bin
/MAKEMKV/CMAP/%05u.cmap
/MAKEMKV/M2TS/%05u.m2ts
/MAKEMKV/MEXT%s
/discatt.dat                    disc attributes (non-decrypted backup)
/ta.dat
```

**R-BAK-01 (VERIFIED).** The backup must be able to store AACS/BD+/M2TS auxiliary data in a `MAKEMKV` subtree so the copy remains openable later.

**R-BAK-02 (VERIFIED).** `Backup source must start with "disc:"` (`E:con+0x2b9518`) — the CLI refuses non-disc backups with exit 10.

**R-BAK-03 (UNKNOWN).** When `Decrypt video files` is off vs on, exactly which files are rewritten (and whether M2TS hashes are patched) is not confirmed.

---

## 15. Conversion profiles (`.mmcp.xml`)

Four shipped profiles: `default`, `flac`, `wdtv`, `aac-st`. Schema as exercised by the shipped files (VERIFIED by reading the shipped XML):

```xml
<profile>
  <name lang="eng">…</name>
  <mkvSettings ignoreForcedSubtitlesFlag="true" useISO639Type2T="false"
               setFirstAudioTrackAsDefault="true" setFirstSubtitleTrackAsDefault="true"
               setFirstForcedSubtitleTrackAsDefault="true" insertFirstChapter00IfMissing="true"/>
  <profileSettings app_DefaultSelectionString="…"/>            <!-- legacy, ignored since 1.12.4 -->
  <outputSettings name="copy" outputFormat="directCopy">
      <description lang="eng">Copy track as is</description>
      <mkvTrackSettings compression="zlib" compressionLevel="9"/>   <!-- optional -->
      <extraArgs>…</extraArgs>                                       <!-- optional, repeatable -->
  </outputSettings>
  <audioMixSettings name="stereo16" outputBPS="16" outputChannelLayout="stereo"
      mix="DolbyPrologic2" centerMixLevel="-3dB" surroundMixLevel="-6dB" lfeMixLevel="0">
      <description lang="eng">…</description>
  </audioMixSettings>
  <trackSettings input="LPCM-stereo">
      <output outputSettingsName="flac-fast" defaultSelection="$app_DefaultSelectionString"/>
  </trackSettings>
</profile>
```

* `outputFormat` values observed: `directCopy`, `LPCM-raw`, `LPCM-wavex`, `FLAC`, `AAC`, `SRT`.
* `trackSettings/@input` values observed: `default`, `LPCM-stereo`, `LPCM-multi`, `TRUEHD-stereo`, `CC`.
* `defaultSelection` supports the variable `$app_DefaultSelectionString` and literal rule text with `+sel:true`.
* `extraArgs` are passed to the encoder; examples: `-compression_level 12`, `codec=libfdk_aac profile=LC`, `afterburner=1`, `aac_coder=twoloop`.
* If no `<trackSettings input="CC">` exists, a closed-caption→SRT rule is added automatically (comment in the shipped default profile).

**R-PROF-01 (VERIFIED).** Load failure must log `Failed to load conversion profile from file '%1'` / `Failed to load profile from file '%s', using default` and continue with defaults.

**R-PROF-02 (VERIFIED).** Profile parse errors must surface as `Profile parsing error: %1`.

**R-PROF-03 (UNKNOWN).** Whether unknown elements/attributes are rejected or ignored.

---

## 16. Track selection language

Default selection rule (VERIFIED, from `default.mmcp.xml` and the `app_DefaultSelectionString` setting):

```
-sel:all,+sel:(favlang|nolang|single),-sel:(havemulti|havecore),+sel:mvcvideo,=100:all,-10:favlang
```

**R-SEL-01 (VERIFIED).** The language supports: `+sel:`/`-sel:` group selectors, bare group names, `=100:` (raise priority/weight), `-10:` (lower weight), and groups `all`, `favlang`, `nolang`, `single`, `havemulti`, `havecore`, `mvcvideo`, `true`.

**R-SEL-02 (UNKNOWN).** Full grammar (precedence, additional operators, quoting) is not documented here; community documentation exists but was not verified against the binary.

---

## 17. Persistence

### 17.1 Registry (VERIFIED)

Root: `HKEY_CURRENT_USER\Software\MakeMKV` (32-bit process; no `WOW6432Node` redirection observed).

| Value | Type | Observed | Meaning |
|---|---|---|---|
| `app_Key` | REG_SZ | 64-char key | registration key |
| `app_KeyHash` | REG_SZ | (not present initially) | key fingerprint (name from strings) |
| `app_SiteInfoString` | REG_SZ | opaque ~64 chars | per-install token (purpose inferred) |
| `kslc_<16 hex>` | REG_SZ | `00000412` → `0000042d` across runs | hex launch counter; suffix stable per machine (inferred) |
| `app_UpdateLastCheck` | REG_DWORD | `20756` | last update-check day = days since 1970-01-01 (`20746` ≈ 2026-10-20) |
| `screen_geometry` | REG_SZ | `AdnQywAD…` | Qt `saveGeometry()` QByteArray, base64 |
| `screen_state` | REG_SZ | `AAAA/wAA…` | Qt `saveState()` (toolbar/panel layout) |
| `path_DestDir`, `path_DestDirMRU` | REG_SZ | empty / path | output folder + MRU |
| `path_BackupDirMRU` | REG_SZ | `*X:/Video/backup/OVER_THE_HEDGE.iso` | backup MRU (`*` marks active entry) |
| `path_OpenFile` | REG_SZ | name from strings only | last opened source |
| `sdf_Stop` | REG_SZ | empty | firmware-tool stop flag |
| `app_ShowDebug` | — | — | "Log debug messages" |
| `app_DebugKey` | — | — | debug logging key |
| `app_ExpertMode` | — | — | "Expert mode" |
| `app_DestinationType` | — | — | None/Auto/SemiAuto/Custom |
| `app_DestinationDir` | REG_SZ | `""` after Apply | default destination directory |
| `app_BackupDecrypted` | — | — | "Decrypt video files" default |
| `app_InterfaceLanguage`, `app_PreferredLanguage` | — | — | language settings |
| `app_UpdateEnable`, `app_AutoSubmitEnabled`, `app_Proxy` | — | — | online updates |
| `app_ShowAVSyncMessages` | — | — | AV sync log verbosity |
| `app_DefaultProfileName`, `app_DefaultSelectionString`, `app_DefaultOutputFileName` | — | — | Advanced tab |
| `app_DataDir` | — | — | MakeMKV data directory |
| `app_Java`, `app_ccextractor` | REG_SZ | `""` after Apply | helper executable paths |

Evidence: `E:reg:reg_snapshot_1.json`, `E:reg:reg_after_apply.json`, key name list `E:con+0x2bfc78..0x2c00e8`.

**R-PERS-01 (VERIFIED).** All settings live under one HKCU key; binary/Qt state is stored as base64 text, not REG_BINARY.

**R-PERS-02 (VERIFIED).** `app_UpdateLastCheck` advances after an update-check attempt even when the network is unavailable.

**R-PERS-03 (INFERRED).** A clone must tolerate unknown/extra values in this key (forward/backward compatibility), because the vendor adds keys between versions.

### 17.2 Data directory (VERIFIED)

Default `%USERPROFILE%\.MakeMKV` (shown in Preferences; observed to exist after first run) containing `_private_data.tar` (3,073,024 bytes in this install). Strings also reference:

```
/.MakeMKV/_private_data.tar
/.MakeMKV/sdf.bin
%s%s/MakeMKV_log.txt
%s%s/MakeMKV_log_%u.txt
```

**R-PERS-04 (VERIFIED).** Debug logging must write `MakeMKV_log.txt` (rolling to `MakeMKV_log_<n>.txt`) into the data directory and log `Debug logging enabled, log will be saved as %1`.

**R-PERS-05 (UNKNOWN).** `_private_data.tar` contents/format; the app treats it as opaque internal state.

---

## 18. Network behaviour

**R-NET-01 (VERIFIED).** Base URL string `http://www.makemkv.com/` (`E:con+0x2c00e8`); HTTP and HTTPS schemes are both supported by the client (`http://`, `https://` strings at `E:con+0x2b97d8`).

**R-NET-02 (VERIFIED).** Before network work the engine performs a connectivity probe: TCP connections to port 443 of `8.8.8.8` and `1.1.1.1`/`9.9.9.9` were observed from `makemkvcon64.exe`, and when a proxy was configured the requests were `CONNECT dns.google:443`, `CONNECT cloudflare-dns.com:443`, `CONNECT dns-nosec.quad9.net:443` (`E:run:proxy_log.txt`, live `Get-NetTCPConnection` capture).

**R-NET-03 (VERIFIED).** The `Proxy server` preference is honoured for outbound HTTP(S); value format observed as `host:port`.

**R-NET-04 (VERIFIED).** Update checks are gated by `app_UpdateLastCheck` (day granularity) and can be disabled (`Enable Internet access`); the UI discloses `Automatic checking for updates is enabled, you may disable it in preferences if you don't want MakeMKV to contact web server.`

**R-NET-05 (UNKNOWN).** The actual update/registration/SVQ endpoint paths, request methods, payload schemas and TLS details were **not captured** — all external traffic was blocked in the test environment and no `makemkv.com` request was observed. Treat as UNKNOWN and re-measure on a networked host.

**R-NET-06 (DOCUMENTED; related messages VERIFIED).** BD+ SVQ files are downloaded automatically (vendor page <https://www.makemkv.com/svq/>); the binary reports `Loaded %1 SVQ file(s)`, `No SVQ files were found, BD+ code can't be processed`, `No matching SVQ files were found…`, and `SVQ in %1 can't be processed by this version, please download latest program version from %2`.

**R-NET-07 (VERIFIED).** Registration-key validation is offline; no network request occurs when an invalid key is entered (`E:run:con_reg_invalid.txt`).

---

## 19. Error model

### 19.1 Internal error taxonomy (VERIFIED, `E:con+0x2ba3a8..0x2ba558`)

`Unspecified error`, `Operation was cancelled`, `Input parameter is incorrect`, `Operation result is incorrect`, `Unable to allocate resources`, `Synchronisation failed`, `Decryption error`, `AV transform failed`, `Unknown media status`, `Request rejected by OS`, `Pipe communication error`, `M2TS hash check failed`, `OS error - %1`, `Posix error - %1`, `Scsi error - %1`, `Scsi error - %1:%2`, `Internal error - %1 (%2)`.

Wrapped as: `Error '%1' occurred while reading '%2' at offset '%3'`, `… while writing data to '%2' at offset '%3'`, `… while creating '%2'`, `… while issuing SCSI command %4 to device '%2:%3'`.

### 19.2 Selected message catalogue (VERIFIED strings; ids only where observed)

| ID | Text |
|---:|---|
|1005|`%1 started`|
|2003|`Error '%1' occurred while reading '%2' at offset '%3'`|
|2024|`Unknown device - '%1'`|
|3006|`Opening files on harddrive at %1`|
|3305|`AACS directory not present, assuming unencrypted disc`|
|5006|`File %1 does not exist`|
|5010|`Failed to open disc`|
|5042|`The program can't find any usable optical drives.`|

Other catalogued messages (ids UNKNOWN) covering expected clone behaviour:

* Disc/FS: `File system not recognized, disc can't be opened`; `Dvd files are corrupt`; `Dvd files are inconsistent. Probably structure protection was applied or files are missing`; `This dvd has %1 titlesets which is more than 99`; `Signature is invalid in IFO file`; `Failed to parse UDF file record for file "%1", UDF filesystem on disc seems to be corrupt…`.
* Drive access: `Operating system blocked access to optical drive "%1". Program needs to be run as administrator…`; `Cannot obtain exclusive access for optical drive "%1" - disc reading will likely fail…`; `Optical drive "%1" opened in OS access mode.`; `Unknown media status`; `Request rejected by OS`.
* Performance: `Program reads data faster than it can write to disk…`; `Failed to allocate memory for read cache…`; `Failed to allocate memory for write cache, can't continue…`.
* Protection: `The volume key is unknown for this disc - video can't be decrypted`; `Can't read AACS VID from disc - most likely current AACS host certificate is revoked by your drive`; `AACS-protected discs that use SKB are not supported`; `This disc uses newer version of AACS protection. Please let us know about it by visiting %1`; `Drive %1 has RPC protection that can not be bypassed…`; `Region setting of drive %1:%2 does not match the region of currently inserted disc, trying to work around…`.
* DVD structure protection: `CellWalk algorithm failed (structure protection is too tough?), trying CellTrim algorithm`; `CellTrim algorithm failed since title has only %1 chapters`; `Cells 1-%1 were removed from title start`; `Cells %1-%2 were removed from title end`; `Fake cells occupy %1%% of the title - assuming fake title`; `Loop detected. Possibly due to unknown structure protection.`
* Title filtering: `Title #%1 has length of %2 seconds which is less than minimum title length of %3 seconds and was therefore skipped`; `Title #%1 declared length is %2 while its real length is %3 - assuming fake title`; `Title %3 in VTS %1 is equal to title %2 and was skipped`; `Title #%1 was added (%2 cell(s), %3)`; `Audio stream #%1 is identical to stream #%2 and was skipped`; `Subtitle stream #%1 is identical to stream #%2 and was skipped`; `Audio stream #%1 in title #%2 looks empty and was skipped`.
* Environment: `The system date on your computer is invalid, the program will likely behave incorrectly.`; `MakeMKV application directory '%1' is invalid, the program will likely behave incorrectly`; `It appears that you have AnyDVD enabled…`; `It appears that you are opening the disc processed by DvdFab…`; `It appears that you have on-the-fly decryption software enabled…`.
* AV sync: `AV synchronization issues were found in file '%1' (title #%2)`; `AV sync issue in stream %1 at %2 with duration of %3 : %4`; `Too many AV synchronization issues in file '%1' (title #%2) , future messages will be printed only to log file`.

### 19.3 Exit codes (VERIFIED)

| Code | Meaning | Evidence |
|---:|---|---|
|0|success, or "open failed" reported only via MSG/TCOUNT|junk ISO, missing ISO, fake BDMV, `disc:0`|
|1|option parsing error; `reg` key rejected|`-h`, `reg BOGUSTESTKEY123`|
|2|firmware tool argument error|`f` with no drive|
|10|source/disc could not be opened as required|`file:` empty/unusable folder, `backup` with non-`disc:` source|
|11|`mkv` command failed (disc not open)|`mkv disc:0 99 …`|
|127|unknown command / unhandled source|`bogus`, `file:<file.mkv>`|

**R-ERR-01 (VERIFIED).** Failure to open a disc is **not** always an error exit; robot consumers must rely on MSG/TCOUNT, not only the exit code.

**R-ERR-02 (VERIFIED).** Fatal engine/UI errors produce a modal `MakeMKV BETA popup` with `Fatal error occurred, program will now exit.` after which the process exits.

---

## 20. Observed edge cases and quirks (use as clone test vectors)

1. **Zero-filled 1 MB ISO** → `MSG:2003 …Internal error - Input parameter is incorrect (489)… at offset '1048576'` (offset equals file size), `MSG:5010`, `TCOUNT:0`, exit 0.
2. **Missing ISO** → `MSG:5006 File nope.iso does not exist`, `MSG:5010`, `TCOUNT:0`, exit 0.
3. **Empty folder as `file:`** → no message, exit 10.
4. **Minimal fake BDMV** (empty index.bdmv/MovieObject.bdmv/00000.mpls/clpi/m2ts) → `MSG:3006` with `file://` URI, `MSG:3305 AACS directory not present…`, `MSG:5010`, `TCOUNT:0`, exit 0.
5. **Folder containing only an MKV** → exit 10, no message.
6. **Valid H.264/AAC MKV opened in the GUI** → log/progress shows `Parsing file :` and `Opening MKV file`, then **fatal popup** and exit. Reproducible in this build; the clone should either support MKV input properly or fail gracefully (this is a target defect, not a requirement).
7. **`reg` with invalid key** → `Key not found or invalid` + `Current registration key: …`, exit 1, no network.
8. **`backup` with `iso:` source** → `Backup source must start with "disc:"`, exit 10.
9. **`mkv` with unopened disc** → `Failed to open disc`, exit 11.
10. **`dev:E:` with no drive** → `MSG:2024 Unknown device - 'E:'`, `MSG:5010`.
11. **`f` with no drive** → `Error: Drive not specified`, exit 2.
12. **`-h`** → `unrecognized option: h` + full usage, exit 1.
13. **`list`** → `Invalid command: list` (despite being referenced in usage), exit 127.
14. **Preferences Apply with no edits** → writes four empty string values (`app_ccextractor`, `app_DestinationDir`, `app_Java`, `app_Proxy`).
15. **About/Register date formats differ**: About uses `Fri Oct 30 2026`, Register uses `31.10.2026`.
16. **UI typo present**: `Registartion code is incorrect` — the clone should fix it unless byte-compat is required.
17. **`kslc_<hex>` counter increments every GUI start** — do not treat as user data.

---

## 21. Explicit UNKNOWN register (must be resolved before claiming compatibility)

| # | Area | Unknown |
|---|---|---|
|U1|IPC|Pipe framing, message schema between GUI and `guiserver`, shm usage (`A0001+shm`)|
|U2|GUI blob|Purpose/content of `.rsrc/BIN/20` (108,841,056 bytes)|
|U3|Preferences|Registry keys for: min title length, read retry count, read buffer size, single-drive mode, DVD structure-protection method, BD+ dump dir, key files dir, streaming settings, libmmbd integration list|
|U4|Streaming|Protocol, port, URL scheme of "Start streaming server"|
|U5|Network|All server endpoints/paths/payloads; whether keys are validated server-side for full licences|
|U6|Robot|CINFO/TINFO/SINFO attribute id/order on this build; PRGV/PRGC/PRGT emission cadence; meaning of DRV field 3/4|
|U7|Backup|Exact file set and rewrite rules for encrypted vs decrypted backup; `discattd.dat` vs `discatt.dat` semantics|
|U8|Profiles|Validation strictness; undocumented elements; `SRT` output details|
|U9|Selection|Full selection-rule grammar and evaluation order|
|U10|MKV mux|Timecode/attachment/3D policy, chapter rounding, track ordering|
|U11|DRM|AACS/BD+/CSS internals (out of scope by design)|
|U12|Localisation|Catalog format (`.mo.gz` naming, id mapping), string ids ↔ translations|
|U13|Errors|Complete MSG id table (only ids observed live are pinned above)|
|U14|Single instance|Whether a second GUI attaches to an existing engine or always spawns one|
|U15|Shell integration|Drag-and-drop of a disc image or folder onto the main window; tray icon / notification behaviour|
|U16|Shortcuts|Accelerators other than `Ctrl+O` (`Open files`) and `Ctrl+Q` (`Quit`)|
|U17|Menu detail|Order and labels of the per-drive items under `Open disc`|

---

## 22. Acceptance tests for the clone

**Architecture**
1. Headless engine can run `info/mkv/backup/reg/f` with all four source prefixes.
2. GUI starts the engine and receives log lines and drive list.
3. Two GUI instances coexist without cross-talk.

**Protocol**
4. `-r info disc:0` on a machine with no drive emits `MSG` 1005 and 5042, 16 `DRV` lines with index 0..15, and `TCOUNT:0`; exit 0.
5. Unknown command → stderr-free stdout message `Invalid command: <x>`, exit 127.
6. All `MSG` lines have the form `MSG:<id>,<flags>,<count>,"<text>","<fmt>",<count params>`.
7. `PRGV/PRGC/PRGT` appear during a save and are monotonic in `PRGV.current`.

**UI**
8. No-disc state: toolbar enablement exactly as §8.5; central button reads `No disc inserted`.
9. File menu and View/Help menus match §8.1–8.3 including `Ctrl+O`/`Ctrl+Q`.
10. Preferences has three tabs and persists changes to `HKCU\Software\MakeMKV`; restart restores window geometry and splitter state.
11. About shows version, GUI version, licence type, both expiry lines.
12. Invalid registration key shows `License type : Invalid` without blocking.

**Behaviour**
13. Clean ISO/DVD/BD source opens; titles and tracks are listed with the vocabulary of §7.2.
14. Saving selected titles produces `.mkv` files named by the template; chapters are preserved; forced-subtitle flag is honoured per profile.
15. Backup to a folder is re-openable as a source.
16. Corrupt image produces a read-error message with file name and offset, then a failed-open message; the app remains usable.
17. Cancel asks `Are you sure you want to cancel the current operation?` and returns to the ready state.

**Robustness / parity**
18. Missing file, empty folder, non-disc folder, and `dev:` of a non-drive each behave as §20.
19. Messages contain no unlocalised `%1`-style placeholders in the default (English) locale.
20. Log file is created in the data directory when debug logging is enabled.

---

## 23. Evidence index

All captured artefacts are in [`analysis/evidence/`](analysis/evidence/) and the tooling that produced them in [`analysis/tools/`](analysis/tools/).

| File | Contents |
|---|---|
| `con_help.txt`, `con_noargs.txt` | usage text and exit codes |
| `con_list.txt` | `-r list`: MSG 1005/5042 + 16 DRV + `Invalid command: list` |
| `con_info_disc0.txt` | no-drive `info disc:0` robot output |
| `con_open_fakebd.txt` | fake BDMV open (MSG 3006/3305/5010, TCOUNT) |
| `con_open_mkvfile.txt` | MKV passed to `file:` (exit 127) |
| `con_reg_invalid.txt` | offline key rejection |
| `gui_probe_1.txt`, `gui_prefs_*.txt`, `openfiledlg.txt` | UIA trees of main window, preferences, file dialog |
| `filemenu.png`, `helpmenu.png`, `about.png`, `prefs_*.png`, `win*.png` | screenshots used for OCR |
| `msg_catalog_region.txt` | 801 consecutive UI/message strings with file offsets |
| `strings_makemkvcon64.txt`, `strings_libmqt.txt`, `strings_libmakemkv64.txt` | full string dumps (offsets = evidence anchors) |
| `reg_snapshot_1.json`, `reg_before_apply.json`, `reg_after_apply.json` | registry persistence snapshots |
| `proxy_log.txt` | proxy CONNECT capture (connectivity probe hosts) |

Reproduce with:
```
python analysis/tools/pe.py  "<install>\makemkvcon64.exe" --sections --imports --strings --minlen 4
python analysis/tools/rsrc.py "<install>\makemkv.exe"
powershell -File analysis/tools/uia.ps1 all
powershell -File analysis/tools/shot_ocr.ps1 -Title preferences
```

---

## 25. Disc-path addendum — verified with a real DVD (2026-10-29)

A BD-RE drive (`HL-DT-ST BD-RE BU40N`, firmware 1.00, serial `SIM21218G5517`, device `D:`) with a DVD-Video disc labelled `OVER_THE_HEDGE` (CSS/CPPM, 6.83 Gb, DVD-ROM, 2 layers OTP) was used to close part of the UNKNOWN register. Raw captures: `analysis/evidence/disc_info.txt` (1607 lines), `rip_title0.txt`, `gui_disc*.txt`, `disc_info.txt`.

### 25.1 DRV record, populated (VERIFIED)

```
DRV:0,2,999,1,"BD-RE HL-DT-ST BD-RE BU40N 1.00 SIM21218G5517","OVER_THE_HEDGE","D:"
```

So `DRV` = `<index>,<flags>,<code>,<visible>,"<drive>","<disc label>","<device>"`. Field 2 was `2` for a loaded DVD (media-present bit) and `256` when empty; field 3 stayed `999`; field 4 was `1` when the slot was populated. **R-DRV-01 (VERIFIED).** Three quoted strings, not two — the format string is truncated in the binary and only shows the prefix.

### 25.2 CINFO (VERIFIED)

```
CINFO:1,6206,"DVD disc"          // attribute 1 = media type
CINFO:2,0,"OVER_THE_HEDGE"      // attribute 2 = disc name/label
CINFO:30,0,"OVER_THE_HEDGE"     // tree info
CINFO:31,6119,"<b>Source information</b><br>"  // panel title
CINFO:32,0,"OVER_THE_HEDGE"     // volume name
CINFO:33,0,"0"                  // order weight
```

**R-ROB-09 (VERIFIED).** The second numeric field is a UI message code (0 when plain text, 6119/6120/6121 for panel headings). Attribute ids match the public `apdefs.h` table in §6.4.

### 25.3 TINFO / SINFO (VERIFIED)

Observed attribute usage on 13 titles:

| Record | Attribute | Example | Meaning |
|---|---:|---|---|
| TINFO | 8 | `1` | chapter count |
| TINFO | 9 | `0:03:43` | duration (H:MM:SS) |
| TINFO | 10 | `101.8 MB` | size, human |
| TINFO | 11 | `106754048` | size, bytes |
| TINFO | 24 | `02` | original title id (2-digit) |
| TINFO | 25 | `1` | segment count |
| TINFO | 26 | `1` | segment map |
| TINFO | 27 | `YB8_t00.mkv` | output file name |
| TINFO | 30 | `1 chapter(s) , 101.8 MB (YB8)` | tree info / summary |
| TINFO | 31 | `<b>Title information</b><br>` | panel title (code 6120) |
| TINFO | 33 | `0` | order weight |
| TINFO | 49 | `YB8` | comment (used as name in output) |

Streams (`SINFO:<title>,<stream>,<attr>,<code>,"<value>"`):

| Attribute | Example | Meaning |
|---:|---|---|
| 1 | `Video` / `Audio` | stream kind (code 6201/6202) |
| 2 | `Stereo` | channel layout |
| 3 / 4 | `eng` / `English` | language code / name |
| 5 / 6 / 7 | `V_MPEG2` / `Mpeg2` / `Mpeg2` | codec id / short / long |
| 13 | `9.8 Mb/s` | bitrate |
| 19 / 20 / 21 | `720x480` / `16:9` / `29.97 (30000/1001)` | dimensions / aspect / frame rate |
| 22 | `0` | stream flags |
| 30 / 31 / 33 / 38 / 42 | `Mpeg2` / panel title / `0` / `` / `( Lossless conversion )` | output codec, panel, weight, MKV flags, conversion note |

**R-ROB-10 (VERIFIED).** Stream indices are per title and ordered video, audio, subtitle. SINFO index 0 of title 0 was `Video`, index 1 `Audio`, indices 2–5 subtitles (3× DVD subtitle, 1× text).

### 25.4 Message ids newly verified (VERIFIED)

| ID | Flags | Text |
|---:|---:|---|
|1011|0|`Using LibreDrive mode (v%1)`|
|3007|0|`Using direct disc access mode`|
|3010|16777216|`Can't locate a cell for VTS %1 TTN %2 PGCN %3 PGN %4`|
|3025|0|`Title #%1 has length of %2 seconds which is less than minimum title length of %3 seconds and was therefore skipped`|
|3028|0/16777216|`Title #%1 was added (%2 cell(s), %3)`|
|3038|0/16777216|`Cells %1-%2 were removed from title end`|
|5005|128|`%1 titles saved`|
|5011|0|`Operation successfully completed`|
|5014|131072|`Saving %1 titles into directory %2`|
|5036|260|`Copy complete. %1 titles saved.`|

### 25.5 GUI behaviour with a disc (VERIFIED)

* Source combo value: `BD-RE HL-DT-ST BD-RE BU40N 1.00 SIM21218G5517`; labels `Type : DVD`, `Label : OVER_THE_HEDGE`, `Protection : CSS/CPPM`.
* Info pane (exact structure):

```
Drive Information
OS device name: D:
Current profile: DVD-ROM
Manufacturer: HL-DT-ST / Product: BD-RE BU40N / Revision: 1.00
Serial number: SIM21218G5517 / Firmware date: 2116-12-20 15:28
Bus encryption flags: 17

LibreDrive Information
Status: Enabled / Drive platform: MT1959 / Firmware type: Original (unpatched)
Firmware version: 1.00 / DVD all regions: Yes / BD raw data read: Yes
BD raw metadata read: Yes / Unrestricted read speed: Yes

Disc Information
Label: OVER_THE_HEDGE / Timestamp: 2006-08-29 22:00:17
Protection: CSS/CPPM / Data capacity: 6.83 Gb / Disc type: DVD-ROM
Disc size: 120mm / Maximum read rate: 10.08 Mbps [1x] / Number of layers: 2 (OTP)
```

* During opening, the toolbar is fully disabled and an `Information` progress page shows: `Source :`, `Titleset id :`, `CSS sectors scanned :`, two `QProgressBar`s, `Cancel`, `Opening DVD disc`, `Elapsed: 0:01:16 / Remaining : 0:00:34`, `Decrypting data`. Opening this DVD took ≈2.5 minutes.
* Ready state: title tree is a `QTreeWidget` with columns **`Type`** and **`Description`**; rows are `DVD disc` → disc label → `Title` (checkbox, on) with description like `1 chapter(s) , 101.8 MB (YB8)`. Right side has `Output folder` and `Info` tabs; `Source information` panel.
* `Save selected titles` enabled, `Eject disc` enabled, `Close` enabled, `Set output folder` enabled, **`Backup...` disabled** (also disabled in the toolbar) on this disc.

### 25.6 End-to-end rip (VERIFIED)

`makemkvcon64 -r mkv disc:0 0 out` saved `YB8_t00.mkv` (104,003,209 bytes) containing: MPEG-2 720×480 video (eng), AC-3 2.0 48 kHz (eng), 3× DVD subtitles (eng/fre/spa) and 1× SubRip (eng); duration 223.69 s. Log lines ended with `MSG:5011`, `MSG:5014`, `MSG:5005`, `MSG:5036`; no PRG* lines. **R-OUT-08 (VERIFIED).** Naming used the title comment (`YB8`) plus `_t00`, matching the template.

### 25.7 UNKNOWN register changes

* U6 (robot metadata): **partially resolved** — attr ids and record order confirmed for DVD; Blu-ray/HD-DVD and GUI-only PRG* emission remain unconfirmed.
* U7 (backup): still UNKNOWN; `Backup...` was disabled for this disc, so the dialog could not be exercised.
* New **U18**: why `Backup...` is disabled for an opened CSS DVD (disk-space check? decryption state? drive type?) — unresolved.

### 25.8 Clone re-verification (2026-10-30)

Re-running `opticrip -r info disc:0` against the same disc now reproduces 13
titles, `TCOUNT:13`, and the reference's title order/sizes. Points that were
not captured in §25.3 and are now implemented:

* **Non-sequential titles.** `TT_SRPT` type bit `0x40` means the title is not
  one sequential PGC. Its first `VTS_PTT_SRPT` entry names the real PGCN.
  Using TTN directly picked a 0.5 s cell for title 76; the PTT lookup returns
  the 4:31 title. This is why the earlier clone reported 12 titles.
* **Segment map.** `TINFO 26` is the cell map, not a VOB count: a 17-cell title
  is `1-17`, a two-cell title `1,2`, a one-cell title `1`. `TINFO 25` stays `1`
  for these (it is the number of maps).
* **Subpicture table.** The `VTSI` subpicture attributes start at `0x254`
  (count) and `0x256` (first entry), not `0x244`/`0x246`; the 16 reserved bytes
  after the audio table account for the difference. Three VobSub tracks now
  match the reference.
* **Bitrates/layout.** Video streams report the constant `9.8 Mb/s`; AC-3
  bitrates can be read from the clear first `0x80` bytes of each VOB sector
  (PES -> substream id `0x80+i` -> AC-3 `frmsizecod`). Six-channel layouts are
  spelled `Surround 5.1` with attribute 40 `5.1(side)`; stereo is `Stereo` /
  `stereo`.
* **Remaining differences.** (a) No `S_CC608/DVD` track: the reference creates
  a `CC→Text` SubRip track (23 `SINFO` rows and one output stream per captioned
  title); OpticRip does not parse DVD CC user data. (b) The main feature is
  `1:23:11` instead of `1:23:02` (9 s), apparently the reference's handling of
  cell 17's cell command before the trailing 0.467 s cell. (c) The reference's
  title names (`YB8`, `ZB1`, ...) come from its online database, not the IFOs.
* **Drive read protection.** On this encrypted disc Windows returns
  `EINVAL`/"Copy Protection Error" for VOB payload reads while the IFOs stay
  readable. The parser treats that as encrypted and still lists the disc; the
  AC-3 bitrate rows are then absent rather than guessed. Unencrypted sources
  are unaffected.
* **GUI.** The ready state now shows all 13 titles with per-track checkboxes,
  a working Cancel button, and sends the title/stream selection to the engine
  with the save command. A synthetic unencrypted DVD was used to verify that
  unticking a track removes it from the remuxed MKV.

---

## 26. Deliberate non-goals

* No reproduction of icons, translations, profiles, or other assets.
* No description of AACS/BD+ algorithms, keys, or certificate handling.
* No claim that UNKNOWN items behave as the original.
