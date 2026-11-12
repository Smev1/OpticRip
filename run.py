"""Launch the OpticRip GUI.

    python run.py                 # empty window
    python run.py --open disc:0   # open a source immediately
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from opticrip.gui.app import main


def _relaunch_without_console() -> bool:
    """Hand the GUI off to pythonw.exe on Windows.

    ``python.exe`` is a console application, so launching this script from
    Explorer leaves an empty command prompt open for as long as the GUI runs.
    The sibling ``pythonw.exe`` runs the same script without creating a
    console; the marker keeps the hand-off one-shot.
    """
    if os.name != "nt" or os.environ.get("OPTICRIP_GUI_RELAUNCHED"):
        return False
    executable = Path(sys.executable)
    if executable.name.lower() != "python.exe":
        return False
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.is_file():
        return False
    environment = os.environ.copy()
    environment["OPTICRIP_GUI_RELAUNCHED"] = "1"
    try:
        subprocess.Popen(
            [str(pythonw), str(Path(__file__).resolve()), *sys.argv[1:]],
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return True


if __name__ == "__main__":
    if _relaunch_without_console():
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
