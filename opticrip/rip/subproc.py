"""Subprocess spawning that never flashes a console window.

A PyInstaller ``console=False`` GUI has no console of its own, so every
child process Windows starts (``ffmpeg``, ``ffprobe``, ``mkvmerge`` ...)
would pop a black window for its lifetime - and a queue that probes tools
and retries reads looks like a machine-gun of flashing cmd prompts.

Every spawn in OpticRip therefore goes through :func:`run` /
:func:`popen`, which add ``CREATE_NO_WINDOW`` (+ a ``STARTUPINFO`` with
``SW_HIDE``) on Windows and are plain wrappers elsewhere.
"""
from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000


def no_window_kwargs() -> dict:
    """Platform kwargs that suppress the child console on Windows."""
    if not sys.platform.startswith("win"):
        return {}
    kwargs: dict = {"creationflags": CREATE_NO_WINDOW}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0          # SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:               # pragma: no cover - non-Windows STARTUPINFO
        pass
    return kwargs


def popen(args, **kwargs):
    """``subprocess.Popen`` with no console window on Windows."""
    merged = no_window_kwargs()
    merged.update(kwargs)
    return subprocess.Popen(args, **merged)


def run(args, **kwargs):
    """``subprocess.run`` with no console window on Windows."""
    merged = no_window_kwargs()
    merged.update(kwargs)
    return subprocess.run(args, **merged)


def call(args, **kwargs):
    merged = no_window_kwargs()
    merged.update(kwargs)
    return subprocess.call(args, **merged)
