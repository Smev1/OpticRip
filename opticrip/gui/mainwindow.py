"""OpticRip main window (Phase 7).

Runs the explicit inspect -> select -> queue workflow:

* source picker + "Inspect" drives the same analyzer stack as the CLI;
* title table allows selecting titles to queue;
* queue panel shows JobQueue events live and supports cancellation;
* log panel tails the LogBus.

The important rule from ARCHITECTURE.md holds: **the GUI owns no business
logic** — analyse/extract/remux/validate all happen in the engine behind
``JobQueue``.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ..cli.main import _pick_analyzer
from ..core import errors as E
from ..core.config import Settings
from ..core.logging_bus import LogBus
from ..core.models import (
    Drive, DriveKind, MediaType, RipJob, TitleSelection, new_id,
)
from ..disc.fs import open_disc_filesystem
from ..drives import AutoPathProvider
from ..rip.queue import JobQueue, QueueEvent
from .theme import QSS
from .widgets import LogPanel, QueuePanel, TitleTable


class MainWindow(QMainWindow):
    """Inspect → select → rip, three panels, one thread each."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpticRip")
        self.resize(960, 640)
        self.settings = Settings.load()
        self.log_bus = LogBus()
        self.queue: JobQueue | None = None

        self._build_ui()
        self.setStyleSheet(QSS)

        self.log_bus.subscribe(self.log_panel.on_log_record)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        central.setAttribute(  # keep Python-owned children alive
            Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # -- header ------------------------------------------------------ #
        header = QWidget()
        header.setObjectName("header")
        hlay = QHBoxLayout(header)
        from PySide6.QtWidgets import QLabel
        title = QLabel("OpticRip")
        title.setObjectName("title")
        hlay.addWidget(title)
        hlay.addStretch(1)
        outer.addWidget(header)

        # -- source row ---------------------------------------------------- #
        src_box = QGroupBox("Source")
        grid = QGridLayout(src_box)
        self.source_edit = QLineEdit()
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setObjectName("secondary")
        self.inspect_btn = QPushButton("Inspect")
        self.rip_btn = QPushButton("Queue rips")
        self.rip_btn.setEnabled(False)
        grid.addWidget(self.source_edit, 0, 0, 1, 3)
        grid.addWidget(self.browse_btn, 0, 3)
        grid.addWidget(self.inspect_btn, 1, 2)
        grid.addWidget(self.rip_btn, 1, 3)
        outer.addWidget(src_box)

        # -- splitters ---------------------------------------------------- #
        split = QSplitter(Qt.Vertical)
        self.title_table = TitleTable()
        tabs = QTabWidget()
        queue_page = QWidget()
        qlay = QVBoxLayout(queue_page)
        self.queue_panel = QueuePanel()
        qlay.addWidget(self.queue_panel)
        log_page = QWidget()
        llay = QVBoxLayout(log_page)
        self.log_panel = LogPanel()
        llay.addWidget(self.log_panel)
        tabs.addTab(queue_page, "Queue")
        tabs.addTab(log_page, "Log")
        split.addWidget(self.title_table)
        split.addWidget(tabs)
        split.setStretchFactor(0, 3)
        outer.addWidget(split, 1)

        self.statusBar().showMessage("Ready")

        self.browse_btn.clicked.connect(self._browse)
        self.inspect_btn.clicked.connect(self.inspect_source)
        self.rip_btn.clicked.connect(self.queue_selected)
        self.queue_panel.cancel_btn.clicked.connect(self.cancel_selected_job)

    # ------------------------------------------------------------------ #
    def log(self, message: str = "", **_: object) -> None:
        if message:
            self.log_bus.info(message, context="gui")

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select a disc folder")
        if path:
            self.source_edit.setText(path)

    # -- inspect ------------------------------------------------------- #
    def inspect_source(self) -> None:
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.information(self, "OpticRip", "Pick a source first.")
            return
        self.title_table.setRowCount(0)
        try:
            fs = open_disc_filesystem(source)
            drive = self._drive_for(source)
            analyzer = _pick_analyzer(fs, drive, self.log_bus)
            self.analysis = analyzer.analyze(drive, fs)
        except E.OpticRipError as exc:
            detail = f"\n\n{exc.detail}" if exc.detail else ""
            QMessageBox.critical(self, exc.title, f"{exc.message}\n\n{exc.hint or ''}{detail}")
            return
        self.title_table.set_titles(self.analysis.titles)
        n = len(self.analysis.titles)
        self.statusBar().showMessage(
            f"{self.analysis.disc.media_type.label} — {n} title(s)")
        self.rip_btn.setEnabled(n > 0)

    @staticmethod
    def _drive_for(source: str) -> Drive:
        kind = DriveKind.FOLDER if os.path.isdir(source) else DriveKind.RAW
        media = MediaType.DVD_VIDEO  # picked by analyzer; placeholder for path
        return Drive(path=source, kind=kind, media_type=media)

    # -- queue --------------------------------------------------------- #
    def queue_selected(self) -> None:
        if self.queue is None:
            self.log_bus.info("starting job queue", context="gui")
            self.queue = JobQueue(self.settings)
            self.queue.subscribe(self.queue_panel.on_queue_event)
            self.queue.start()
        titles = self.analysis.titles
        selected_rows = {ix.row() for ix in self.title_table.selectedIndexes()}
        chosen = ([titles[r] for r in selected_rows]
                  if selected_rows else titles)
        if not chosen:
            QMessageBox.information(self, "OpticRip", "No titles to rip.")
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Choose output directory", self.settings.output_dir)
        if not out_dir:
            return
        self.settings.output_dir = out_dir
        policy, ok = QInputDialog.getItem(
            self, "Overwrite policy",
            "When the output file already exists:",
            ("ask", "overwrite", "skip", "rename", "error"), 3, False)
        if ok:
            self.settings.overwrite_policy = policy
        for title in chosen:
            job = RipJob(
                id=new_id(), drive_device=self.analysis.source_path,
                title=title, selection=TitleSelection.all_streams(title),
                output_path=os.path.join(out_dir, ""),
                disc_label=self.analysis.disc.label,
                media_type=self.analysis.disc.media_type,
                drive_kind=self._drive_for(
                    self.analysis.source_path).kind)
            self.queue.add(job)
        self.statusBar().showMessage(f"{len(chosen)} job(s) queued")

    def cancel_selected_job(self) -> None:
        if self.queue is None:
            return
        job_id = self.queue_panel.selected_job_id()
        if job_id:
            self.queue.cancel(job_id)

    def closeEvent(self, event) -> None:   # pragma: no cover - Qt teardown
        self._shutdown_queue()
        super().closeEvent(event)
