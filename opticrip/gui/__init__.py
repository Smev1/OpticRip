"""OpticRip desktop GUI (Phase 7).

Qt is imported lazily so the rest of the codebase (and the test suite) runs
fine on machines without PySide6 installed.
"""
from __future__ import annotations

QT_AVAILABLE = True
try:  # pragma: no cover - exercised only on Qt-less environments
    import PySide6  # noqa: F401
except ImportError:  # pragma: no cover
    QT_AVAILABLE = False
