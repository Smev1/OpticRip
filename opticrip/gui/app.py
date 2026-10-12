"""Entry point: ``python -m opticrip.gui`` (or the packaged OpticRip.exe)."""
from __future__ import annotations

import os
import sys


def _ensure_package_context() -> None:
    """Make the import below work in every launch mode.

    PyInstaller runs the spec's script as a top-level ``app`` module, so the
    package-relative import of ``.mainwindow`` has no parent package to hang
    off. If we detect that case, mount the real package path explicitly so
    the import resolves to ``opticrip.gui.mainwindow`` either way.
    """
    if __package__ in (None, ""):
        src = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        # frozen (PyInstaller): modules ship inside the bundle; fix sys.path
        # so `import opticrip` still finds the package.
        if getattr(sys, "frozen", False):
            sys.path.insert(0, os.path.dirname(src))
        import opticrip.gui  # noqa: F401
        globals()["__package__"] = "opticrip.gui"


def main(argv: list[str] | None = None) -> int:
    # Headless fallback ONLY where "no DISPLAY" really means "no display"
    # (Linux CI / containers). On Windows there is never a DISPLAY variable,
    # and forcing offscreen there launches an invisible window.
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _ensure_package_context()
    from opticrip.gui.mainwindow import MainWindow

    app = QApplication(sys.argv or ["opticrip"])
    app.setApplicationName("OpticRip")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
