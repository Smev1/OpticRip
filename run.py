"""Launch the OpticRip GUI.

    python run.py                 # empty window
    python run.py --open disc:0   # open a source immediately

On Windows the console launcher hands off to ``pythonw.exe`` and exits, so
double-clicking ``run.py`` does not leave a command prompt open behind the
application.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _relaunch_without_console() -> None:
    """Replace a ``python.exe`` launch with an equivalent ``pythonw.exe`` one."""
    if os.name != "nt":
        return
    executable = Path(sys.executable)
    if executable.name.lower() != "python.exe":
        return
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.is_file():
        return
    script = Path(__file__).resolve()
    subprocess.Popen(
        [str(pythonw), str(script), *sys.argv[1:]],
        cwd=str(script.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    raise SystemExit(0)


if __name__ == "__main__":
    _relaunch_without_console()
    from opticrip.gui.app import main

    raise SystemExit(main(sys.argv[1:]))
