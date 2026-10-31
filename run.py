"""Launch the OpticRip GUI.

    python run.py                 # empty window
    python run.py --open disc:0   # open a source immediately
"""

import sys

from opticrip.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
