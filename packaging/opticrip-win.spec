# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the OpticRip Windows client (Phase 9).
#
# Build (on Windows):
#   py -m venv .venv && .venv\Scripts\pip install -r requirements.txt pyinstaller
#   (optional) place ffmpeg.exe / ffprobe.exe into packaging\win\_bin
#   .venv\Scripts\pyinstaller packaging\opticrip-win.spec --noconfirm
#
# dist\OpticRip\ then contains:
#   OpticRip.exe      GUI (windowed)
#   opticrip-cli.exe  CLI (console)
#   _bin\ffmpeg.exe   static binaries, found via rip.tools._frozen_bins_dir()

import os

# PyInstaller exec()s the spec file: no __file__ — use the SPECPATH global
# it injects (absolute path of the spec's directory).
_SOURCES = os.path.normpath(os.path.join(SPECPATH, ".."))
_BUNDLED_BIN = os.path.join(SPECPATH, "win", "_bin")

binaries = []
if os.path.isdir(_BUNDLED_BIN):
    for fname in sorted(os.listdir(_BUNDLED_BIN)):
        if fname.lower().endswith(".exe"):
            binaries.append((os.path.join(_BUNDLED_BIN, fname), "_bin"))

a = Analysis(
    [os.path.join(_SOURCES, "opticrip", "gui", "app.py")],
    pathex=[_SOURCES],
    binaries=binaries,
    datas=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pydub", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    name="OpticRip",
    exclude_binaries=True,
    console=False,
)

exe_cli = EXE(
    pyz,
    a.scripts,
    [],
    name="opticrip-cli",
    exclude_binaries=True,
    console=True,
)

coll = COLLECT(
    exe_gui, exe_cli, a.binaries, a.zipfiles, a.datas,
    name="OpticRip",
    upx=False,
)
