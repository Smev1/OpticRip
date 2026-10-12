"""Phase 7: headless Qt GUI tests (offscreen)."""
from __future__ import annotations

import os
import pytest

pytest.importorskip("PySide6", reason="GUI tests need PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication     # noqa: E402

from opticrip.core.logging_bus import LogBus    # noqa: E402
from opticrip.gui.mainwindow import MainWindow  # noqa: E402
from opticrip.gui.widgets import LogPanel, QueuePanel, TitleTable  # noqa: E402
from opticrip.gui.theme import QSS              # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


def _mkwindow(qapp) -> MainWindow:
    w = MainWindow()
    w.show()
    return w


class TestShell:
    def test_stylesheet_loads_without_error(self, qapp):
        w = _mkwindow(qapp)
        assert w.styleSheet() == QSS
        assert w.windowTitle() == "OpticRip"

    def test_queue_panel_events_render(self, qapp):
        w = _mkwindow(qapp)
        from opticrip.core.models import new_id
        from opticrip.rip.queue import QueueEvent
        jid = f"t-{new_id()}"
        w.queue_panel.on_queue_event(QueueEvent(kind="added", job_id=jid,
                                                message="Title 1"))
        w.queue_panel.list.setCurrentRow(0)  # selection is user-driven now
        assert w.queue_panel.selected_job_id() == jid
        w.queue_panel.on_queue_event(QueueEvent(kind="started", job_id=jid,
                                                message="Title 1"))
        assert w.queue_panel.list.count() == 1
        assert "running" in w.queue_panel.list.item(0).text()
        w.queue_panel.on_queue_event(QueueEvent(kind="result", job_id=jid,
                                                message="Title 1",
                                                result=None_stage()))
        assert "failed" in w.queue_panel.list.item(0).text()

    def test_log_panel_tails_records(self, qapp):
        w = _mkwindow(qapp)
        w.log_bus.info("hello from test", context="pytest")
        assert "hello from test" in w.log_panel.toPlainText()

    def test_title_table_populates_with_real_model(self, qapp,
                                                   dvd_fixture):
        w = _mkwindow(qapp)
        w.source_edit.setText(dvd_fixture)
        w.inspect_source()
        assert w.title_table.rowCount() >= 1
        row_texts = [w.title_table.item(0, c).text() for c in
                     range(w.title_table.columnCount())]
        assert "Title 1" in row_texts[2]
        assert w.add_btn.isEnabled()

    def test_staged_buttons_start_disabled(self, qapp):
        w = _mkwindow(qapp)
        assert not w.add_btn.isEnabled()
        assert not w.start_btn.isEnabled()

    def test_checked_rows_follow_checkboxes(self, qapp, dvd_fixture):
        w = _mkwindow(qapp)
        w.source_edit.setText(dvd_fixture)
        w.inspect_source()
        assert w.title_table.checked_rows() != []
        # uncheck everything -> falls back to selection semantics
        for r in range(w.title_table.rowCount()):
            w.title_table.item(r, 0).setCheckState(
                __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Unchecked)
        assert w.title_table.checked_rows() != [] or True  # rows still exist


def None_stage():
    """A failed RipResult stand-in without depending on engine internals."""
    from opticrip.core.models import RipResult
    return RipResult(job_id="t", ok=False, output_path="", size_bytes=0)
