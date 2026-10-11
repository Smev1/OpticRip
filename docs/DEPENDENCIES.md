# Dependencies, licenses and capabilities

OpticRip's own source is **GPL-3.0-or-later**. It calls external programs as
*separate processes*; it never links against them, so their licenses do not
change ours or restrict redistribution of this project.

## Runtime tools (invoked as subprocesses)

| Tool | Used for | License | Required? |
|------|----------|---------|-----------|
| `ffprobe` | media inspection: codecs, resolution, fps, channels, chapters | LGPL-2.1+ (GPL-2.0+ if built `--enable-gpl`) | **Yes** |
| `ffmpeg`  | stream extraction + lossless remux to MKV (`-c copy`) | same as ffprobe | **Yes** |
| `mkvmerge` | alternative lossless MKV muxer (MKVToolNix) | GPL-2.0-or-later | No (optional backend) |
| `blkid` / `lsblk` / `udevadm` | drive + media identification on Linux | GPL-2.0+ (util-linux) / LGPL-2.1+ (systemd) | No (sysfs fallback) |

`ffmpeg` and `ffprobe` are normally installed from your distribution
(`apt install ffmpeg`). OpticRip probes both at start-up and reports the exact
version in the diagnostics panel.

## Libraries OpticRip reuses *through* ffmpeg

The extractor and Blu-ray navigator rely on the protocols/demuxers ffmpeg was
built with. Verify yours with `ffmpeg -buildconf`:

| Library | Enables | License | Verified capability |
|---------|---------|---------|---------------------|
| **libdvdread** | `dvdvideo` demuxer, raw DVD-Video navigation | GPL-2.0-or-later | Reads unencrypted and **CSS-decrypted** discs; does **not** decrypt CSS itself |
| **libdvdnav** | DVD menus/navigation | GPL-2.0-or-later | Navigation only |
| **libbluray** | `bluray:` protocol, playlist/clip navigation, AACS *plug-in loading* | LGPL-2.1-or-later | Parses BDMV structure. Decryption requires an external AACS/BD+ library (libaacs/libbdplus) **and** a key database |
| libx264 / GPL parts | not used by OpticRip (`-c copy` only) | GPL-2.0+ | - |

> **Important:** libdvdread, libdvdnav and libbluray provide *navigation and
> reading*. None of them decrypt a protected commercial disc on its own.
> See `docs/PROTECTED_DISCS.md`.

## Python packages

| Package | Extra | License | Purpose |
|---------|-------|---------|---------|
| *(none)* | core | - | The engine uses only the standard library. |
| `PySide6-Essentials` | `gui` | LGPL-3.0 / GPL-3.0 (Qt for Python) | Desktop GUI (Qt 6 bindings) |
| `pytest` | `dev` | MIT | Test runner |
| `static-ffmpeg` | `dev` | MIT (wraps GPL/LGPL ffmpeg builds) | Sandboxes/CI without a system ffmpeg |

## Environment used to develop and verify this project

```
$ python3 --version            # 3.12.3
$ ffmpeg -hide_banner -version
ffmpeg version n8.0.1
$ ffmpeg -hide_banner -buildconf | grep enable
    --enable-gpl  --enable-libdvdread  --enable-libdvdnav  --enable-libbluray
$ python3 -c "import PySide6; print(PySide6.__version__)"   # 6.12.0
```

Capability probing is **runtime**, not assumed: `opticrip.rip.tools.Toolchain`
runs `-version`/`-buildconf` and records which of `dvdvideo`, `bluray`,
`matroska` and the needed decoders are actually present. The GUI shows the
result under *Diagnostics*.

## Debian/Ubuntu install

```
sudo apt install ffmpeg mkvtoolnix
# optional, only if your distro ffmpeg lacks DVD/Blu-ray support:
sudo apt install libdvdread8 libdvdnav4 libbluray2
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[gui]'
```


## Windows client (Phase 9)

* Python 3.11+ and PySide6-Essentials (GUI only). The engine itself remains
  dependency-free.
* ffmpeg + ffprobe ships *inside* the Windows bundle under `_bin\` and is
  found via `rip.tools._frozen_bins_dir()`; alternatively install ffmpeg
  normally and add its directory to PATH.
* Optical drives are discovered with `GetDriveTypeW == DRIVE_CDROM`;
  media presence/labels via `GetVolumeInformationW` with
  `SEM_FAILCRITICALERRORS` set so Windows never shows the "no disc"
  dialog during a probe. Empty drives report `ERROR_NOT_READY` -> "No disc
  in the drive", matching the Linux state.
* Raw images (.iso) probe identically — the sector reader falls back to
  plain file reads when the path is a file on every platform.
* Build: `pyinstaller packaging/opticrip-win.spec` (see README's Windows
  section); CI builds it in `.github/workflows/windows-build.yml`.
