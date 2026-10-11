"""Reusable Qt widgets for the OpticRip GUI (Phase 7)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.models import JobStatus


class TitleTable(QTableWidget):
    """Titles of the inspected disc: idx, name, duration, chapters,
    streams."""

    COLS = ("#", "Title", "Duration", "Chapters", "Streams")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)

    def set_titles(self, titles) -> None:
        self.setRowCount(0)
        for t in titles:
            row = self.rowCount()
            self.insertRow(row)
            values = [str(t.index), t.display_name,
                      t.duration_label, str(t.chapter_count),
                      ", ".join(s.summary() for s in t.streams) or "—"]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                self.setItem(row, col, item)


class QueuePanel(QWidget):
    """Live per-job progress fed by :class:`JobQueue` events."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.cancel_btn = QPushButton("Cancel selected")
        self.cancel_btn.setObjectName("danger")
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self._rows: dict[str, QListWidgetItem] = {}

    def on_queue_event(self, ev) -> None:
        job_id = ev.job_id
        label_before = job_id
        if ev.kind == "added":
            item = QListWidgetItem(label_before)
            item.setData(Qt.UserRole, job_id)
            self.list.addItem(item)
            self._rows[job_id] = item
            self.list.setCurrentItem(item)   # newest job is the target of Cancel
            item.setText(f"{label_before} — queued")
        elif ev.kind == "started":
            item = self._rows.get(job_id)
            if item:
                item.setText(f"{ev.message or label_before} — running")
        elif ev.kind == "progress" and ev.update is not None:
            item = self._rows.get(job_id)
            if item:
                pct = ev.update.percent if ev.update.percent is not None else 0
                item.setText(f"{ev.update.phase.label:>12} {ev.update.message}"
                             f" — {pct}%")
        elif ev.kind == "result":
            item = self._rows.get(job_id)
            if not item:
                item = QListWidgetItem(job_id)
                item.setData(Qt.UserRole, job_id)
                self.list.addItem(item)
                self._rows[job_id] = item
            r = ev.result
            suffix = f" {r.output_path} ({r.size_bytes} B)" if r.ok else " failed"
            item.setText(f"{ev.message or job_id}{suffix}")
            item.setForeground(Qt.darkGreen if r.ok else Qt.darkRed)

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

    def on_log_record(self, record) -> None:
        self.appendPlainText(record.to_line())
