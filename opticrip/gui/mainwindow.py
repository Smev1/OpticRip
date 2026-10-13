"""OpticRip main window (Phase 10b rework).

The workflow is explicit and staged:

1. **Pick a source** (folder / .iso image / drive path) and choose where the
   MKVs should go (*Browse…* for source and output).
2. **Inspect** — drives the same analyzer stack as the CLI; titles appear in
   the table with chapters/durations/streams.
3. **Add to queue** — takes the *selected* table rows (or every title if
   nothing is selected) into the :class:`JobQueue`.  The *queue does not
   start* here; the jobs wait with the "queued" state.
4. **Start queue** — workers spawn and the Queue tab paints events live.
   **Cancel selected** stops a running/queued job.

Threading rule: the :class:`JobQueue` emits events from worker threads, so
every event is relayed to the panel through Qt's queued signal/slot path
(`QueueBridge`) instead of painting QListWidget rows from a worker thread.

The important rule from ARCHITECTURE.md still holds: **the GUI owns no
business logic** — analyse/extract/remux/validate all happen in the engine
behind ``JobQueue``.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ..cli.main import _pick_analyzer
from ..core import errors as E
from ..core.config import OVERWRITE_POLICIES, Settings
from ..core.logging_bus import LogBus
from ..core.models import (
    Drive, DriveKind, MediaType, RipJob, TitleSelection, new_id,
)
from ..disc.fs import open_disc_filesystem
from ..drives import AutoPathProvider
from ..rip.queue import JobQueue
from .theme import QSS
from .widgets import LogPanel, QueuePanel, TitleTable


class QueueBridge(QObject):
    """Relay JobQueue events onto the GUI thread.

    ``JobQueue`` runs workers on plain threads and invokes subscribers
    there; touching widgets off-thread corrupts them.  Connecting this
    signal with the default (queued) connection makes Qt deliver the event
    on the thread that created the bridge — i.e. the GUI thread.
    """

    forward = Signal(object)

    def __init__(self, panel: QueuePanel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.forward.connect(panel.on_queue_event)

    def __call__(self, event) -> None:
        self.forward.emit(event)


class AddToQueueDialog(QDialog):
    """One dialog for output folder + overwrite policy + stream choice."""

    def __init__(self, settings: Settings, count: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add to queue")
        self.setModal(True)

        lay = QVBoxLayout(self)
        head = QLabel(f"Queue {count} title(s) with:")
        lay.addWidget(head)

        form = QFormLayout()
        self.out_edit = QLineEdit(settings.output_dir or "")
        self.out_browse = QPushButton("Browse…")
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.out_browse)
        out_wrap = QWidget()
        out_wrap.setLayout(out_row)
        form.addRow("Output folder:", out_wrap)

        self.policy_combo = QComboBox()
        self.policy_combo.addItems(list(OVERWRITE_POLICIES))
        self.policy_combo.setCurrentText(
            settings.overwrite_policy
            if settings.overwrite_policy in OVERWRITE_POLICIES
            else OVERWRITE_POLICIES[0])
        form.addRow("If file exists:", self.policy_combo)

        self.all_streams_check = QCheckBox(
            "Include every audio/subtitle track")
        self.all_streams_check.setChecked(True)
        form.addRow("", self.all_streams_check)
        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.out_browse.clicked.connect(self._browse_out)
        self.accepted.connect(self._apply)

    def _browse_out(self) -> None:
        picked = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.out_edit.text())
        if picked:
            self.out_edit.setText(picked)

    def _apply(self) -> None:
        self.output_dir = self.out_edit.text().strip()
        self.policy = self.policy_combo.currentText()
        self.all_streams = self.all_streams_check.isChecked()


class MainWindow(QMainWindow):
    """Inspect → select → queue → start, one explicit step each."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpticRip")
        self.resize(1024, 680)
        self.settings = Settings.load()
        self.log_bus = LogBus()
        self.queue: JobQueue | None = None
        self.analysis = None
        self._source = ""            # resolved source path of current analysis
        self._bridge: QueueBridge | None = None
        self._tabs: QTabWidget | None = None
        self._queue_tab: QWidget | None = None
        self._log_file = None

        self._build_ui()
        self.setStyleSheet(QSS)

        self.log_bus.subscribe(self.log_panel.on_log_record)
        self._log_file = self._open_log_file()
        if self._log_file is not None:
            self.log_bus.subscribe(self._write_log_record, replay=False)

    def _open_log_file(self):
        """Append-mode session log, so a failed rip can be diagnosed later."""
        try:
            from ..core.config import default_output_dir
            base = os.path.join(os.path.expanduser("~"), ".opticrip")
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, "opticrip-gui.log")
            handle = open(path, "a", encoding="utf-8")
            self.log_bus.info(f"session log: {path}", context="gui")
            return handle
        except OSError:
            return None

    def _write_log_record(self, record) -> None:
        try:
            if self._log_file is not None:
                self._log_file.write(record.to_line() + "\n")
                self._log_file.flush()
        except OSError:
            self._log_file = None

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setSpacing(8)

        # -- header ------------------------------------------------------ #
        header = QWidget()
        header.setObjectName("header")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(4, 2, 4, 2)
        title = QLabel("OpticRip")
        title.setObjectName("title")
        hlay.addWidget(title)
        self.drive_hint = QLabel("Pick a disc folder, .iso image, or drive")
        self.drive_hint.setObjectName("hint")
        hlay.addWidget(self.drive_hint)
        hlay.addStretch(1)
        self.queue_state = QLabel("Queue: idle")
        self.queue_state.setObjectName("hint")
        hlay.addWidget(self.queue_state)
        outer.addWidget(header)

        # -- source ------------------------------------------------------ #
        src_box = QGroupBox("1 · Source")
        grid = QGridLayout(src_box)
        grid.setColumnStretch(0, 1)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(
            "e.g. /media/OVER_THE_HEDGE, C:\\discs\\movie.iso, /dev/sr0")
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setObjectName("secondary")
        self.inspect_btn = QPushButton("🔍  Inspect")
        self.inspect_btn.setObjectName("primary")
        grid.addWidget(self.source_edit, 0, 0)
        grid.addWidget(self.browse_btn, 0, 1)
        grid.addWidget(self.inspect_btn, 0, 2)
        outer.addWidget(src_box)

        # -- titles + queue actions -------------------------------------- #
        mid = QGroupBox("2 · Titles — select rows, then Add to queue")
        mid_lay = QVBoxLayout(mid)
        self.title_table = TitleTable()
        mid_lay.addWidget(self.title_table, 1)
        row = QHBoxLayout()
        self.add_btn = QPushButton("➕  Add to queue")
        self.add_btn.setObjectName("primary")
        self.add_btn.setEnabled(False)
        self.start_btn = QPushButton("▶  Start queue")
        self.start_btn.setEnabled(False)
        self.start_btn.setToolTip("Begin processing the queued jobs")
        row.addWidget(self.add_btn)
        row.addWidget(self.start_btn)
        row.addStretch(1)
        mid_lay.addLayout(row)
        outer.addWidget(mid, 2)

        # -- queue + log -------------------------------------------------- #
        split = QSplitter(Qt.Vertical)
        tabs = QTabWidget()
        queue_page = QWidget()
        qlay = QVBoxLayout(queue_page)
        qlay.setContentsMargins(4, 4, 4, 4)
        self.queue_panel = QueuePanel()
        qlay.addWidget(self.queue_panel)
        log_page = QWidget()
        llay = QVBoxLayout(log_page)
        llay.setContentsMargins(4, 4, 4, 4)
        self.log_panel = LogPanel()
        llay.addWidget(self.log_panel)
        tabs.addTab(queue_page, "3 · Queue")
        tabs.addTab(log_page, "Log")
        self._tabs = tabs
        self._queue_tab = queue_page
        split.addWidget(tabs)
        split.setStretchFactor(0, 1)
        outer.addWidget(split, 3)

        self.statusBar().showMessage("Ready — pick a source and Inspect")

        self.browse_btn.clicked.connect(self._browse)
        self.inspect_btn.clicked.connect(self.inspect_source)
        self.add_btn.clicked.connect(self.add_selected_to_queue)
        self.start_btn.clicked.connect(self.start_queue)
        self.queue_panel.cancel_btn.clicked.connect(self.cancel_selected_job)

    # ------------------------------------------------------------------ #
    def log(self, message: str = "", **_: object) -> None:
        if message:
            self.log_bus.info(message, context="gui")

    def _browse(self) -> None:
        picked, _ = QFileDialog.getOpenFileName(
            self, "Select a disc folder or .iso image", "",
            "Disc sources (*.iso *.img *.udf);;All files (*)")
        if not picked:
            picked = QFileDialog.getExistingDirectory(
                self, "Select a disc folder")
        if picked:
            self.source_edit.setText(picked)

    # -- inspect ------------------------------------------------------- #
    def inspect_source(self) -> None:
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.information(self, "OpticRip", "Pick a source first.")
            return
        self.title_table.setRowCount(0)
        self.analysis = None
        self._source = ""
        try:
            drive = folder_or_image_drive(source)
            fs = open_disc_filesystem(drive.path)
            analyzer = _pick_analyzer(fs, drive, self.log_bus)
            self.analysis = analyzer.analyze(drive, fs)
            self._source = drive.path
        except E.OpticRipError as exc:
            detail = f"\n\n{exc.detail}" if exc.detail else ""
            QMessageBox.critical(
                self, exc.title,
                f"{exc.message}\n\n{exc.hint or ''}{detail}")
            self.statusBar().showMessage("Inspect failed")
            self.add_btn.setEnabled(False)
            return
        self.title_table.set_titles(self.analysis.titles)
        n = len(self.analysis.titles)
        self.statusBar().showMessage(
            f"{self.analysis.disc.media_type.label} — {n} title(s). "
            "Select rows, then Add to queue.")
        self.add_btn.setEnabled(n > 0)

    # -- queue --------------------------------------------------------- #
    def _ensure_queue(self) -> JobQueue:
        if self.queue is None:
            self.log_bus.info("queue created (not started)", context="gui")
            self.queue = JobQueue(self.settings, log=self.log_bus)
            self._bridge = QueueBridge(self.queue_panel, self)
            self.queue.subscribe(self._bridge)
        return self.queue

    def add_selected_to_queue(self) -> None:
        if self.analysis is None or not self.analysis.titles:
            QMessageBox.information(
                self, "OpticRip", "Inspect a source first.")
            return
        titles = self.analysis.titles
        selected_rows = sorted(
            {ix.row() for ix in self.title_table.selectedIndexes()},
            reverse=False)
        chosen = ([titles[r] for r in selected_rows if r < len(titles)]
                  if selected_rows else list(titles))
        if not chosen:
            QMessageBox.information(self, "OpticRip", "No titles to queue.")
            return

        dlg = AddToQueueDialog(self.settings, len(chosen), self)
        if dlg.exec() != QDialog.Accepted:
            return
        out_dir = dlg.output_dir
        if not out_dir:
            QMessageBox.information(
                self, "OpticRip", "Choose an output folder.")
            return
        os.makedirs(out_dir, exist_ok=True)
        self.settings.output_dir = out_dir
        self.settings.overwrite_policy = dlg.policy

        drive_kind = (DriveKind.FOLDER if os.path.isdir(self._source)
                      else DriveKind.IMAGE if os.path.isfile(self._source)
                      else DriveKind.RAW)
        queue = self._ensure_queue()
        added = 0
        for title in chosen:
            selection = (TitleSelection.all_streams(title)
                         if dlg.all_streams
                         else _default_selection(title))
            safe = "".join(c if (c.isalnum() or c in " ._-") else "_"
                           for c in title.display_name).strip() or "title"
            job = RipJob(
                id=new_id(), drive_device=self._source,
                title=title, selection=selection,
                output_path=os.path.join(out_dir, f"{safe}.mkv"),
                disc_label=self.analysis.disc.label,
                media_type=self.analysis.disc.media_type,
                drive_kind=drive_kind)
            queue.add(job)
            added += 1
        self.log_bus.info(f"queued {added} job(s)", context="gui")
        # raise the Queue tab so the user sees the jobs land
        if self._queue_tab is not None and self._tabs is not None:
            self._tabs.setCurrentWidget(self._queue_tab)
        self.statusBar().showMessage(
            f"{added} job(s) in queue — press Start queue")
        self.start_btn.setEnabled(True)
        self._refresh_queue_state()

    def start_queue(self) -> None:
        queue = self._ensure_queue()
        if queue.is_running():
            return
        self.log_bus.info("queue started", context="gui")
        queue.start()
        self.start_btn.setEnabled(False)
        self._refresh_queue_state("running")

    def cancel_selected_job(self) -> None:
        if self.queue is None:
            return
        job_id = self.queue_panel.selected_job_id()
        if job_id:
            self.queue.cancel(job_id)

    def _refresh_queue_state(self, state: str | None = None) -> None:
        pending = (self.queue.pending_count()
                   if self.queue is not None else 0)
        text = (f"Queue: {state or ('idle' if pending == 0 else 'waiting')}"
                f" — {pending} pending")
        self.queue_state.setText(text)

    # -- teardown ------------------------------------------------------- #
    def _shutdown_queue(self) -> None:
        if self.queue is not None:
            try:
                self.queue.stop(wait=False)
            except Exception:
                pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def closeEvent(self, event) -> None:   # pragma: no cover - Qt teardown
        self._shutdown_queue()
        super().closeEvent(event)


def folder_or_image_drive(source: str) -> Drive:
    """One canonical drive object for files *and* folders.

    Uses the same provider as the CLI so classify/re-root behaviour matches.
    """
    provider = AutoPathProvider([source])
    drives = provider.list_drives()
    if not drives:
        kind = (DriveKind.FOLDER if os.path.isdir(source)
                else DriveKind.IMAGE if os.path.isfile(source)
                else DriveKind.RAW)
        return Drive(path=source, kind=kind, media_type=MediaType.UNKNOWN)
    return drives[0]


def _default_selection(title) -> TitleSelection:
    """First video + first audio + first subtitle track of a title."""
    from ..core.models import StreamKind
    sel = TitleSelection(title_id=title.id)
    vids = [s.index for s in title.streams_of(StreamKind.VIDEO)]
    auds = [s.index for s in title.streams_of(StreamKind.AUDIO)]
    subs = [s.index for s in title.streams_of(StreamKind.SUBTITLE)]
    return TitleSelection(
        title_id=title.id,
        video_index=vids[0] if vids else None,
        audio_indices=tuple(auds[:1]),
        subtitle_indices=tuple(subs[:1]),
    )
