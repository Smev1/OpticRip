"""Reusable Qt widgets for the OpticRip GUI (Phase 10b rework)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.models import JobStatus


class TitleTable(QTableWidget):
    """Titles of the inspected disc: checkbox, idx, name, duration, chapters,
    streams."""

    COLS = ("✓", "#", "Title", "Duration", "Chapters", "Streams")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        # Multi-select rows; the Queue dialog uses the check boxes too.
        self.setSelectionMode(QTableWidget.ExtendedSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(True)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setSectionsClickable(False)

    def set_titles(self, titles) -> None:
        self.setRowCount(0)
        for t in titles:
            row = self.rowCount()
            self.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Checked)
            check.setTextAlignment(Qt.AlignCenter)
            self.setItem(row, 0, check)
            values = [str(t.index), t.display_name,
                      t.duration_label, str(t.chapter_count),
                      ", ".join(s.summary() for s in t.streams) or "—"]
            for col, text in enumerate(values, start=1):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setTextAlignment(Qt.AlignCenter)
                self.setItem(row, col, item)

    def checked_rows(self) -> list[int]:
        """Rows whose checkbox is ticked (falls back to selection)."""
        rows = [r for r in range(self.rowCount())
                if (it := self.item(r, 0)) is not None
                and it.checkState() == Qt.Checked]
        if rows:
            return rows
        return sorted({ix.row() for ix in self.selectedIndexes()})


class QueuePanel(QWidget):
    """Live per-job rows fed by :class:`JobQueue` events (GUI thread only)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SingleSelection)
        self.cancel_btn = QPushButton("✕  Cancel selected")
        self.cancel_btn.setObjectName("danger")
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        hint = QLabel("Tip: queue several titles, then Start queue.")
        hint.setObjectName("hint")
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self._rows: dict[str, QListWidgetItem] = {}

    # -- events (always on the GUI thread via QueueBridge) --------------- #
    def on_queue_event(self, ev) -> None:
        job_id = ev.job_id
        name = ev.message or job_id
        if ev.kind == "added":
            item = self._rows.get(job_id)
            if item is None:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, job_id)
                self.list.addItem(item)
                self._rows[job_id] = item
            item.setText(f"⏳  {name} — queued")
            item.setForeground(Qt.lightGray)
            item.setData(Qt.UserRole + 1, "queued")
        elif ev.kind == "started":
            item = self._rows.get(job_id)
            if item:
                item.setText(f"▶  {name} — running")
                item.setData(Qt.UserRole + 1, "running")
        elif ev.kind == "progress" and ev.update is not None:
            item = self._rows.get(job_id)
            if item:
                pct = ev.update.percent if ev.update.percent is not None else 0
                item.setText(
                    f"▶  {name} — {ev.update.phase.label}: "
                    f"{ev.update.message} ({pct:.0f}%)")
        elif ev.kind == "result":
            item = self._rows.get(job_id)
            if item is None:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, job_id)
                self.list.addItem(item)
                self._rows[job_id] = item
            r = ev.result
            if r.ok:
                size = f"{r.size_bytes / 1e6:,.1f} MB" if r.size_bytes else "done"
                item.setText(f"✅  {name} — {size}: {r.output_path}")
                item.setForeground(Qt.darkGreen)
                item.setData(Qt.UserRole + 1, "done")
            else:
                item.setText(f"❌  {name} — failed: {ev.message or 'error'}")
                item.setForeground(Qt.darkRed)
                item.setData(Qt.UserRole + 1, "failed")

    def selected_job_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None



class LogPanel(QPlainTextEdit):
    """Read-only log fed by the LogBus."""

    MAX_LINES = 5000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(self.MAX_LINES)
        self.setPlaceholderText("Log output appears here…")

    def on_log_record(self, record) -> None:
        self.appendPlainText(record.to_line())
