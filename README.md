# OpticRip

A clean-room, cross-platform optical-disc ripper with the same command-line
surface and robot protocol as MakeMKV, plus a Tk desktop UI. It was built from
the behavioural evidence in [CLONE_SPEC.md](CLONE_SPEC.md); no vendor code,
assets or profiles were copied.

OpticRip does **not** bundle libdvdcss, libaacs or libbdplus. It can use
copies that **you** supply: libdvdcss decrypts CSS DVDs, while libaacs and
libbdplus decrypt and patch AACS/BD+ Blu-rays. The libraries are validated,
stored under the application data directory, and loaded only when a protected
disc is saved. Without them, protected discs can still be inspected but saving
is refused with the reference's "cannot decrypt" message. Raw and decrypted
whole-disc backups are supported for unencrypted discs and for CSS/AACS/BD+
discs when the matching library is available.

## Requirements

* Python 3.11 or newer (Tk 8.6+ for the GUI).
* `ffmpeg` and `ffprobe` on `PATH`. Set `OPTICRIP_FFMPEG` to a binary path or a
  directory containing both tools to override discovery.

There are no third-party Python packages.

## libdvdcss for CSS DVDs

OpticRip never downloads or ships libdvdcss. Supply your own copy in one of
three ways:

* **GUI:** *File -> Load libdvdcss library...* (the file picker accepts the
  Windows DLL, Linux `.so` and macOS `.dylib`). The library is validated and
  copied to `<data dir>/libdvdcss/`; the path is saved as
  `app_DvdcssPath`. *Use automatic libdvdcss detection* returns to discovery
  (it does not delete the staged copy).
* **By hand:** put `libdvdcss-2.dll` (or the platform equivalent) in
  `<data dir>/libdvdcss/`, next to `opticrip/`, or anywhere on the system
  loader's search path. VLC's `libdvdcss-2.dll` directory is also detected on
  Windows.
* **Environment:** set `OPTICRIP_DVDCSS` to a full library path for CLI use.

The library is only loaded when a DVD looks CSS-scrambled, so unencrypted
discs and folders need nothing. On Windows libdvdcss accepts drive letters
(`D:`), not image paths; CSS-protected `.iso` files therefore need the
physical disc (an unencrypted `.iso` works normally). On POSIX systems
libdvdcss can open an image file directly. Opening a CSS disc with older
libdvdcss builds can pause for about a minute while the title key is
recovered; title inspection skips VOB reads, so the pause normally happens
on the first save rather than at open time.

## libaacs and libbdplus for Blu-ray

AACS and BD+ decryption is delegated to the same libraries other open-source
players use. OpticRip does not implement the cryptography or ship any keys.

* **GUI:** *File -> Load libaacs library...* and *File -> Load libbdplus
  library...*. The copies are validated and staged in `<data dir>/libaacs/`
  and `<data dir>/libbdplus/`; the paths are saved as `app_AacsPath` and
  `app_BdplusPath`. *File -> Load AACS key file (KEYDB.cfg)...* points libaacs
  at a key database; the file is used where it is and never copied or shown.
  *Use automatic AACS/BD+ detection* clears all three paths.
* **By hand:** put `libaacs.dll` / `.so` / `.dylib` and the equivalent
  libbdplus file in `<data dir>/libaacs/` and `<data dir>/libbdplus/`, next to
  `opticrip/`, or on the system loader's search path. VLC's directory is also
  checked on Windows. libaacs also uses its normal `KEYDB.cfg` search path:
  `%APPDATA%\aacs\KEYDB.cfg` on Windows, `~/.config/aacs/KEYDB.cfg` elsewhere.
* **Environment:** `OPTICRIP_LIBAACS`, `OPTICRIP_LIBBDPLUS` and
  `OPTICRIP_AACS_KEYDB` supply library and key-file locations for CLI use
  when no path is stored in the settings. `OPTICRIP_AACS_KEYDB` is only
  needed when the key database is not in libaacs' standard location.

AACS needs a key database with an entry for the disc. Without a matching
volume/unit key, libaacs refuses to open the disc and OpticRip reports the
reference's "volume key is unknown" message. libaacs can use a `D |` volume
key directly, or derive it from the media key and volume ID. A drive supplies
the VID itself; for a folder or ISO image the KEYDB entry needs the disc's
`V |` volume ID (or a cached VID must exist) when libaacs has to derive the
key. BD+ always needs the VID and media key too, because libbdplus takes both
from libaacs. A drive supplies the VID itself and libaacs can derive the media
key from the MKB when device keys are available; for a folder or ISO image the
KEYDB entry generally needs `V |` and `M |`. OpticRip reports a clear error
if libaacs cannot provide them. Both libraries must match the platform
(64-bit OpticRip needs 64-bit libraries).

BD+ also needs libbdplus 0.2 or newer and its VM configuration files
(`bdplus/vm0` in the user or system configuration directory). A copy without
them cannot create the conversion table; OpticRip reports that instead of
writing corrupt video. On Windows the DLLs' own dependencies (for example
libgcrypt) must sit beside them or on `PATH`.

The decrypted clips are staged in a temporary folder before ffmpeg remuxes
them, so a protected Blu-ray save needs free space roughly equal to the title's
clip size. Staging reports byte progress and owns the first half of the title
bar; the remux owns the second half. Cancel is checked between 6144-byte units
and removes the staged clips.

When the source has no local path (for example an ISO image), its AACS/BD+
metadata is copied to a temporary folder so the libraries can read it; the
clips themselves stay in the source and are decrypted on the fly. A disc that
keeps `BDSVM` under `BDMV` (or its AACS folder under `BDMV` / `BDAV`) is
normalised the same way, because libbdplus only loads `BDSVM/00000.svm` from
the root it is given. UDF 2.50 Blu-ray images are read directly, including
images whose directory data lives in a metadata partition.

## Run

```bash
python run.py                    # GUI
python run.py --open disc:0      # GUI, open the first drive immediately
```

Headless engine (`makemkvcon`-shaped):

```bash
python -m opticrip.cli -r info disc:0
python -m opticrip.cli -r mkv disc:0 0 /output/folder
python -m opticrip.cli -r backup disc:0 /output/folder
python -m opticrip.cli -r reg KEY-OR-FILE
```

Sources: `disc:<index>`, `dev:<device>`, `file:<folder>`, `iso:<image>`.
`-r` / `--robot` switches the output from human-readable lines to the
`DRV:` / `TCOUNT:` / `CINFO:` / `TINFO:` / `SINFO:` / `MSG:` records described in
the spec. Exit codes: `0` success, `1` usage/key, `10` source, `11` save,
`127` unknown command.

## What works

* Drive enumeration and disc-presence detection on Windows; folder and
  ISO9660 sources on every platform.
* DVD-Video: `VIDEO_TS.IFO` / `VTS_*.IFO` parsing, title and chapter lists,
  stream attributes, segment maps, structure-protection cell trimming,
  min-title-length filtering, and VOB-concat remux to Matroska. Titles whose
  `TT_SRPT` entry is not a single sequential PGC are resolved through
  `VTS_PTT_SRPT` (this is what makes the verified disc report all 13 titles,
  including the 4:31 one).
* CSS decryption through a user-supplied libdvdcss: scrambled VOB payload is
  read sector-by-sector from the drive while ISO9660 metadata stays
  unencrypted; title/track selection and remuxing then work as usual.
* AACS decryption through a user-supplied libaacs, and BD+ patching through a
  user-supplied libbdplus: the clips a title needs are decrypted into a
  temporary folder and then remuxed like any unencrypted Blu-ray.
* Blu-ray: `BDMV` playlist (`MPLS`) and clip (`CLPI`) parsing to titles,
  streams and segment lists, with `m2ts` concat remux.
* ISO sources: ISO9660 for DVD/data images and UDF 2.50 for Blu-ray images,
  with a metadata-partition fallback for discs that keep directory data there.
* Decrypted whole-disc backup: raw copies keep the original sectors; AACS/BD+
  and CSS sources are decrypted while copying, and the AACS/BD+ metadata is
  moved to the reference's `MAKEMKV/` subtree so the result opens as a normal
  unencrypted disc.
* DVD closed captions: the MPEG-2 user-data blocks are decoded as CEA-608
  (CC1) and saved as a SubRip track, so a captioned title gets the reference's
  `S_CC608/DVD` / `CC→Text` stream. Pop-on, paint-on and roll-up captions,
  PAC positioning, erase/backspace and the special/extended character tables
  are handled.
* Profiles (`opticrip/profiles/*.mmcp.xml`): direct copy, FLAC, AAC stereo and
  WDTV-style conversion rules, plus the default selection string.
* Output naming templates (default `{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}`)
  with collision-safe `_1`, `_2` suffixes.
* Settings under `HKCU\Software\OpticRip` (Windows) or
  `~/.opticrip/settings.json`; `OPTICRIP_REG_ROOT` / `OPTICRIP_DATA_DIR`
  override the locations for testing.
* Tk GUI: source picker, title tree with working title and per-track
  checkboxes, output folder chooser, progress bar, working Cancel button,
  log pane, preferences, registration and backup dialogs. The GUI never
  touches a disc itself; it talks to the engine over a JSON-lines protocol
  and sends the ticked titles/tracks with each save.

## Layout

```
opticrip/
  engine.py        disc operations, event stream, save/backup
  robot.py         robot/human record writers
  messages.py      message catalogue
  drives.py        drive information
  dvd.py           DVD-Video IFO parser
  bdmv.py          Blu-ray MPLS/CLPI parser
  iso9660.py       read-only ISO9660
  udf.py           read-only UDF 2.50 (Blu-ray images)
  cc.py            DVD CEA-608 closed captions to SubRip
  backup.py        raw and decrypted whole-disc backup
  dvdcss.py        optional user-supplied libdvdcss wrapper
  aacs.py          optional user-supplied libaacs wrapper
  bdplus.py        optional user-supplied libbdplus wrapper
  bddecrypt.py     AACS/BD+ Blu-ray clip staging
  discfs.py        folder/ISO/CSS-disc abstraction
  profiles.py      .mmcp.xml loader
  selection.py     selection-rule language
  naming.py        output-name template
  settings.py      persistence
  ffmpeg.py        ffprobe/remux/progress
  cli.py           makemkvcon-compatible front end
  gui/             Tk shell and engine subprocess client
tests/             unit and end-to-end tests
analysis/          evidence captures and tooling used to write the spec
```

## Tests

```bash
python -m unittest discover -s tests
```

The suite covers the robot records, message catalogue, robot round-trips,
naming, selection rules, profiles, registration, settings, DVD/BDMV parsing,
ISO9660, UDF, DVD caption decoding, the libdvdcss/libaacs/libbdplus wrappers
and staging, CLI behaviour, the `guiserver` selection/save protocol, and a
synthetic DVD rip (folder -> IFO -> VOB concat -> MKV -> ffprobe), which
requires `ffmpeg`. The AACS/BD+ tests use fake libraries and encrypted
fixtures; they do not need real keys. The caption tests build a real MPEG-2
VOB, patch DVD caption user data into its picture units, and verify the saved
MKV has a SubRip track.

## Known limits

* **Encrypted discs need user-supplied libraries.** CSS needs libdvdcss;
  AACS needs libaacs plus a KEYDB.cfg with the disc's keys; BD+ additionally
  needs libbdplus and its VM configuration files. CPPM is unsupported. The
  libraries are not bundled. A decrypted backup requires the same library as a
  save: libdvdcss for CSS, libaacs (and libbdplus for BD+) for Blu-ray. CSS
  `.iso` images cannot be opened through libdvdcss on Windows because its
  Windows backend only accepts drive letters.
* **Protected Blu-rays are staged through a temporary copy.** Decrypting with
  libaacs/libbdplus has to happen before ffmpeg reads a clip, so the title's
  clips are written decrypted to `<destination>`'s temporary folder first.
  This doubles peak disk use for the title and adds a copy pass. BD+ runs in
  the no-menu conversion-table mode; on-disc BD+ application-layer
  interactions (menus, BD-J) are not emulated, which is normal for ripping.
* **AACS CPS units are auto-detected.** libaacs is reset per title and picks
  the CPS unit by validating decrypted TS headers instead of being told the
  on-disc title number. This handles the usual multi-title discs but costs a
  little extra work when a disc has several unit keys.
* **UHD Blu-ray is detected but cannot be decrypted.** The MKB type in
  `AACS/MKBROM.AACS` identifies AACS 2.0/2.1 (type `0x48141003` /
  `0x48151003`), and the disc is reported as `AACS 2.x (UHD)`; saving and
  backup refuse it with a specific message instead of a generic volume-key
  error. This is a hard limit rather than a missing feature: AACS 2.x uses a
  separate key tree that libaacs does not implement, bus encryption needs an
  authenticated, bus-encryption-capable drive, and every UHD disc also needs a
  licensed host certificate. Implementing it would require the AACS 2.x
  specification, licensed device keys/certificates and a LibreDrive-style
  drive; those are not available to this clone. AACS 1.0 and BD+ are the
  supported Blu-ray cases.
* **No online metadata.** The reference labels DVD titles with short names
  (`YB8`, `ZB1`, ...) that come from its title database; those names are not
  present in the disc's IFOs (verified: no DVD-TEXT table on the test disc)
  and are not reproduced. Titles without metadata are named `title_tNN.mkv`
  or by the ID number, exactly as the reference does for unknown titles.
* **Closed captions are a bounded sample at inspection time.** The reference
  adds a `S_CC608/DVD` text track per captioned title and converts it to
  SubRip; OpticRip now does the same. To keep a CSS disc from spinning up for
  minutes at open time, the CC track is listed only when caption data is found
  in the first 4 MiB of the title (or when the VOBs are readable at all). A
  title whose first caption appears later gets no CC row until a save, when the
  whole stream is decoded. Only CC1/field 1 is decoded (the English caption
  channel a single S_CC608 track carries); CC2/CC3/CC4, XDS and Text-mode
  channels are ignored.
* **One duration delta.** The 83-minute main feature is listed as `1:23:11`
  instead of the reference's `1:23:02` (9 s). Every other title duration, all
  sizes, all segment maps (`1`, `1,2`, `1-17`) and all `TINFO` ids match the
  reference capture. The difference comes from the reference's handling of a
  trailing cell command in that PGC and does not affect the remux.
* **UDF images are read-only UDF 2.50.** The parser follows the main volume,
  partition and (when present) metadata partition. Exotic layouts such as
  multi-session or VAT-remapped images may still be rejected; mount those or
  use the `file:` source instead.
* **`guiserver`** is a clone-local JSON-lines protocol used by the GUI; the
  reference's binary command socket and HTTP streaming endpoints were UNKNOWN
  (see §21 of the spec) and are not emulated. `f` implements only `list`;
  firmware flashing is not attempted.
* No icons, translations or vendor profiles; the UI uses plain Tk widgets.

## Verification status

Checked against the physical `OVER_THE_HEDGE` DVD and the captured reference
output in `analysis/evidence/disc_info.txt`:

* 13 titles, `TCOUNT:13`, correct order, sizes, chapter counts and ids.
* `TINFO 24/25/26/27/30` layout, including segment maps and the PTT-resolved
  non-sequential title.
* `SINFO` kind/language/codec/dimensions/aspect/fps/flags/layout/bitrate rows;
  video reports the reference's constant `9.8 Mb/s` and AC-3 bitrates are read
  from clear frame headers when the OS permits VOB reads. A CSS disc opened
  through libdvdcss intentionally skips that VOB read at open time, so its
  audio bitrate rows are omitted until a future pass can read them lazily.
* GUI tree, info panes, protection label and 13-title ready state.
* Synthetic unencrypted DVD rip, including title and per-track selection,
  chapter metadata and ffprobe validation.
* Synthetic captioned DVD: the generated VOB carries real DVD CC user data;
  the engine lists the `S_CC608/DVD` track and the saved MKV contains a
  SubRip stream with the decoded text.
* UHD detection: a fake AACS 2.0 MKB is identified as `AACS 2.0 (UHD)` and the
  save/backup paths refuse it with message 9143 before touching a library.
* Unit tests for the libdvdcss wrapper use a fake decrypting device and a
  fake ISO9660 image; they verify sector-aligned reads, per-VOB decryption,
  metadata reads that stay undecrypted, library validation and staging.

The encrypted disc is correctly refused for saving when no libdvdcss is
configured; its IFOs remain readable, which is why inspection works. With a
user-supplied library the VOB payload is decrypted and saving follows the same
remux path as an unencrypted disc.

AACS/BD+ support is covered by tests with fake libaacs/libbdplus handles: they
verify 6144-byte unit decryption and fixing, VID/media-key hand-off, BD+
conversion-table/stream calls, metadata materialisation from non-folder
sources, staging progress, cancellation cleanup, and the engine's refusal and
success paths. One end-to-end test generates a short MPEG-TS, "encrypts" it,
stages it through the real engine, remuxes it with ffmpeg and probes the
resulting MKV, so the staging -> remux hand-off is exercised too. No real AACS
or BD+ disc and no real libaacs/libbdplus build was available on the
development machine, so those paths are logically verified but not yet
exercised against retail media. CSS behaviour was verified against the
physical test disc described above.
