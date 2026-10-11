"""Entry point: ``python -m opticrip.gui``."""
from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("QT_QPA_PLATFORM") is None and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from .mainwindow import MainWindow

    app = QApplication(sys.argv or ["opticrip"])
    app.setApplicationName("OpticRip")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
