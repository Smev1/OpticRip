"""Main window and workflow state machine.

State is observable exactly like the reference (R-FSM-01): the big central
button, the log pane and menu enablement.  All disc work happens in the engine
process; this module never touches a disc itself.
"""

from __future__ import annotations

import json
import re
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from .. import PRODUCT, PRODUCT_TAG, __version__, messages
from .. import drives as drivelib
from ..aacs import AacsError, stage_library as stage_aacs
from ..bdplus import BdplusError, stage_library as stage_bdplus
from ..dvdcss import DvdcssError, stage_library
from ..model import fmt_size
from ..settings import Settings
from .dialogs import (
    AboutDialog, BackupDialog, HelpDialog, PreferencesDialog, RegisterDialog,
)
from .engine_client import EngineClient

VIDEO_FILTER = (
    "VIDEO_TS.IFO;DISCID.DAT;HVA00001.VTI;MKBROM.AACS;index.bdmv;INDEX.BDM;"
    "MovieObject.bdmv;MOVIEOBJ.BDM;info.bdav;BDMV;BDAV;MKB_RO.inf;discatt.dat;*.iso;*.mkv"
)
TREE_COLUMNS = ("name", "duration", "size", "source", "output")
TREE_WIDTHS = {
    "#0": 210, "name": 230, "duration": 82, "size": 92,
    "source": 190, "output": 150,
}
STREAM_GROUPS = (("video", "Video"), ("audio", "Audio"),
                 ("subtitle", "Subtitles"))


class MainWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings = Settings()
        self.settings.bump_launch_counter()
        self.client = EngineClient()
        self.drives: dict[int, dict] = {}
        self.disc: dict | None = None
        self.state = "no_source"
        self.title_items: dict[str, int] = {}
        self.stream_items: dict[str, tuple[int, int]] = {}
        self.group_items: list[str] = []
        self.output_dir = str(self.settings.output_dir)
        self.source_spec = ""
        self._save_started = 0.0
        self._busy_text = ""
        self._progress_title: dict | None = None
        self._progress_fraction = 0.0
        self._phase_started = 0.0

        root.title(PRODUCT_TAG)
        root.minsize(920, 620)
        self._build()
        self._restore_geometry()
        for key, ident in (("app_DvdcssPath", 9121), ("app_AacsPath", 9131),
                           ("app_BdplusPath", 9135)):
            configured = self.settings.get_str(key).strip()
            if configured and Path(configured).is_file():
                self.log_line(messages.render(ident, configured))
        self._set_state("no_source")
        self.root.after(80, self._pump)
        self.root.after(500, lambda: self.client.send(cmd="drives"))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self._build_menu()
        self._build_toolbar()
        self._build_source()
        self._build_body()
        self._build_status()
        self._bind_keys()

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        self.root.config(menu=bar)
        file_menu = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="File", menu=file_menu)
        self.open_disc_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_command(label="Open files", accelerator="Ctrl+O",
                              command=self.open_files, underline=0)
        file_menu.add_cascade(label="Open disc", menu=self.open_disc_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Load libdvdcss library...",
                              command=self.load_libdvdcss)
        file_menu.add_command(label="Use automatic libdvdcss detection",
                              command=self.clear_libdvdcss)
        file_menu.add_command(label="Load libaacs library...",
                              command=self.load_libaacs)
        file_menu.add_command(label="Load libbdplus library...",
                              command=self.load_libbdplus)
        file_menu.add_command(label="Load AACS key file (KEYDB.cfg)...",
                              command=self.load_aacs_keyfile)
        file_menu.add_command(label="Use automatic AACS/BD+ detection",
                              command=self.clear_aacs_libraries)
        file_menu.add_separator()
        file_menu.add_command(label="Close", command=self.close_disc)
        file_menu.add_command(label="Eject disc", command=self.eject_disc)
        file_menu.add_separator()
        file_menu.add_command(label="Set output folder", command=self.set_output_folder)
        file_menu.add_command(label="Save selected titles", command=self.save_titles)
        file_menu.add_command(label="Backup...", command=self.backup_disc)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self._on_close)
        self.file_menu = file_menu
        self.menu_index = {}
        for label in ("Open files", "Open disc", "Close", "Eject disc",
                      "Load libdvdcss library...", "Use automatic libdvdcss detection",
                      "Load libaacs library...", "Load libbdplus library...",
                      "Load AACS key file (KEYDB.cfg)...",
                      "Use automatic AACS/BD+ detection",
                      "Set output folder", "Save selected titles", "Backup...", "Quit"):
            try:
                self.menu_index[label] = file_menu.index(label)
            except tk.TclError:
                self.menu_index[label] = 0

        view = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="View", menu=view)
        self.toolbar_var = tk.BooleanVar(value=True)
        view.add_checkbutton(label="Toolbar", variable=self.toolbar_var,
                             command=self._toggle_toolbar)
        view.add_command(label="Expand all titles", command=lambda: self._expand_all(True))
        view.add_command(label="Collapse all titles", command=lambda: self._expand_all(False))
        view.add_command(label="Clear log", command=self.clear_log)
        view.add_command(label="Preferences", command=self.preferences)

        help_menu = tk.Menu(bar, tearoff=0)
        bar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Online Help", command=self.online_help)
        help_menu.add_command(label="About", command=self.about)
        help_menu.add_command(label="Purchase", command=self.purchase)
        help_menu.add_command(label="Register", command=self.register)

    def _build_toolbar(self) -> None:
        self.toolbar = ttk.Frame(self.root, padding=(6, 4))
        self.toolbar.grid(row=0, column=0, sticky="we")
        self.tb_open = ttk.Button(self.toolbar, text="Open files", command=self.open_files)
        self.tb_backup = ttk.Button(self.toolbar, text="Backup", command=self.backup_disc)
        self.tb_save = ttk.Button(self.toolbar, text="Save selected titles", command=self.save_titles)
        self.tb_prefs = ttk.Button(self.toolbar, text="Preferences", command=self.preferences)
        self.tb_eject = ttk.Button(self.toolbar, text="Eject disc", command=self.eject_disc)
        for button in (self.tb_open, self.tb_backup, self.tb_save,
                       self.tb_prefs, self.tb_eject):
            button.pack(side="left", padx=(0, 4))

    def _build_source(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Source", padding=(8, 6))
        frame.grid(row=1, column=0, sticky="we", padx=6, pady=(2, 0))
        self.source_combo = ttk.Combobox(frame, state="readonly", width=52)
        self.source_combo.grid(row=0, column=0, sticky="ew")
        self.source_combo.bind("<<ComboboxSelected>>", self._combo_selected)
        ttk.Button(frame, text="Open files...", command=self.open_files).grid(
            row=0, column=1, padx=(6, 0),
        )
        info = ttk.Frame(frame)
        info.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.type_var = tk.StringVar(value="Type: —")
        self.label_var = tk.StringVar(value="Label: —")
        self.protection_var = tk.StringVar(value="Protection: —")
        self.capacity_var = tk.StringVar(value="Capacity: —")
        for column, variable in enumerate((self.type_var, self.label_var,
                                           self.protection_var,
                                           self.capacity_var)):
            ttk.Label(info, textvariable=variable).grid(
                row=0, column=column, sticky="w", padx=(0, 18),
            )
        frame.columnconfigure(0, weight=1)

    def _build_body(self) -> None:
        output = ttk.Frame(self.root, padding=(8, 2))
        output.grid(row=2, column=0, sticky="we", padx=6, pady=(2, 0))
        ttk.Label(output, text="Output folder:").pack(side="left")
        self.output_var = tk.StringVar(value=self.output_dir)
        self.output_entry = ttk.Entry(output, textvariable=self.output_var,
                                      state="readonly")
        self.output_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(output, text="Change...", command=self.set_output_folder).pack(
            side="right",
        )

        self.paned = ttk.PanedWindow(self.root, orient="vertical")
        self.paned.grid(row=3, column=0, sticky="nsew", padx=6, pady=4)

        tree_frame = ttk.Frame(self.paned)
        # Keep the hierarchy in #0 so ttk draws the disclosure arrows and
        # indentation; normal columns carry the disc/title details.
        self.tree = ttk.Treeview(tree_frame, columns=TREE_COLUMNS,
                                 show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Title")
        self.tree.column("#0", width=TREE_WIDTHS["#0"], anchor="w", stretch=True)
        for column, title in (
            ("name", "Name"), ("duration", "Duration"), ("size", "Size"),
            ("source", "Source"), ("output", "Output"),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=TREE_WIDTHS[column], anchor="w",
                             stretch=column == "name")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(tree_frame, orient="horizontal",
                                command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.bind("<Double-1>", self._tree_double)
        self.paned.add(tree_frame, weight=4)

        log_frame = ttk.Frame(self.paned)
        log_header = ttk.Frame(log_frame)
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Log").pack(side="left")
        ttk.Button(log_header, text="Clear", width=7,
                   command=self.clear_log).pack(side="right")
        self.log = tk.Text(log_frame, height=8, wrap="word", state="disabled",
                           relief="sunken", borderwidth=1)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self.paned.add(log_frame, weight=1)

        self.big_button = tk.Button(
            self.root, text="No disc inserted", font=("Segoe UI", 11),
            relief="groove", command=self.open_files,
        )
        self.big_button.grid(row=4, column=0, sticky="we", padx=6, pady=(0, 2))

        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        self.tree_menu = tk.Menu(self.root, tearoff=0)
        self.tree_menu.add_command(label="Toggle item", command=lambda: self._toggle_selected())
        self.tree_menu.add_command(label="Select all", command=lambda: self._select_all(True))
        self.tree_menu.add_command(label="Unselect all", command=lambda: self._select_all(False))

        style = ttk.Style(self.root)
        base_font = tkfont.nametofont("TkDefaultFont")
        bold = base_font.copy()
        bold.configure(weight="bold")
        self._tree_bold = bold
        style.configure("Treeview", rowheight=max(22, base_font.metrics("linespace") + 6))
        style.configure("Treeview.Heading", font=bold)
        self.tree.tag_configure("disc", font=bold)
        self.tree.tag_configure("title", font=bold, background="#eef3f7")
        self.tree.tag_configure("group", foreground="#4d5863")
        self.tree.tag_configure("chapter", foreground="#6c7680")

    def _build_status(self) -> None:
        frame = ttk.Frame(self.root, padding=(6, 2))
        frame.grid(row=5, column=0, sticky="we")
        self.status_var = tk.StringVar(value="Ready")
        self.elapsed_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, width=30).pack(side="left")
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=65536)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(frame, textvariable=self.elapsed_var, width=34).pack(side="right")
        self.cancel_button = ttk.Button(frame, text="Cancel", command=self.cancel_save,
                                        state="disabled", width=8)
        self.cancel_button.pack(side="right", padx=(0, 6))

    def _bind_keys(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self.open_files())
        self.root.bind("<Control-q>", lambda _e: self._on_close())
        self.root.bind("<F5>", lambda _e: self.client.send(cmd="drives"))

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        self.state = state
        ready = state == "ready"
        saving = state == "saving"
        open_failed = state == "open_failed"
        has_disc = self.disc is not None and state in ("ready", "saving")
        any_drive = any(d.get("present") for d in self.drives.values())
        selected = bool(self.disc and any(t["selected"] for t in self.disc["titles"]))
        enabled = {
            "Open files": not saving,
            "Open disc": any_drive and not saving,
            "Close": (has_disc or open_failed) and not saving,
            "Eject disc": ready,
            "Load libdvdcss library...": not saving,
            "Use automatic libdvdcss detection": not saving,
            "Load libaacs library...": not saving,
            "Load libbdplus library...": not saving,
            "Load AACS key file (KEYDB.cfg)...": not saving,
            "Use automatic AACS/BD+ detection": not saving,
            "Set output folder": ready,
            "Save selected titles": ready and selected,
            "Backup...": ready,
            "Quit": True,
        }
        for label, index in self.menu_index.items():
            self.file_menu.entryconfig(index, state="normal" if enabled[label] else "disabled")
        self.tb_open.state(["disabled"] if saving else ["!disabled"])
        self.tb_prefs.state(["!disabled"])
        for button, label in ((self.tb_backup, "Backup..."), (self.tb_save, "Save selected titles"),
                              (self.tb_eject, "Eject disc")):
            button.state(["!disabled"] if enabled[label] else ["disabled"])
        self.cancel_button.state(["!disabled"] if saving else ["disabled"])
        if self.disc is not None:
            self.source_combo.configure(state="readonly")
        labels = {
            "no_source": "No disc inserted",
            "opening": "Disc is loading",
            "open_failed": "Disc can't be opened",
            "ready": "No disc inserted",
            "saving": "Saving...",
        }
        self.big_button.configure(text=labels.get(state, "No disc inserted"))
        if state == "ready" and self.disc is not None:
            self.big_button.grid_remove()
        else:
            self.big_button.grid()
        if state == "open_failed":
            self.status_var.set("Disc can't be opened")
        elif state == "no_source":
            self.status_var.set("Ready")

    def _busy(self, operation: str, action: str) -> None:
        self._busy_text = f"{operation} / {action}"
        self.status_var.set(self._busy_text)
        self._reset_progress()
        self._save_started = datetime.now().timestamp()
        self.elapsed_var.set("Elapsed 0:00")

    def _reset_progress(self) -> None:
        self._progress_title = None
        self._progress_fraction = 0.0
        self._phase_started = 0.0
        self.progress.configure(maximum=65536, value=0)

    @staticmethod
    def _short_size(num_bytes: float) -> str:
        return fmt_size(max(0, int(num_bytes))).replace(" ", "") or "0"

    @staticmethod
    def _clock(seconds: float) -> str:
        total = int(max(0.0, seconds))
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    # ------------------------------------------------------------------
    # engine events
    # ------------------------------------------------------------------
    def _pump(self) -> None:
        while True:
            try:
                event = self.client.events.get_nowait()
            except Exception:
                break
            try:
                self._handle(event)
            except Exception as exc:  # never let one event kill the pump
                self.log_line(f"UI error handling {event.get('event')}: {exc}")
        if self.state == "saving" and self._save_started:
            seconds = int(datetime.now().timestamp() - self._save_started)
            elapsed = self._clock(seconds)
            if self._progress_title is not None:
                phase = self._progress_title.get("phase", "")
                if phase == "staging":
                    done = int(self._progress_title.get("stage_current") or 0)
                    total = int(self._progress_title.get("stage_total") or 0)
                    size = f"{self._short_size(done)}/{self._short_size(total)}"
                    phase_elapsed = datetime.now().timestamp() - self._phase_started
                    if done > 0 and phase_elapsed > 0.2:
                        rate = done / phase_elapsed
                        remaining = max(0.0, (total - done) / rate)
                        self.elapsed_var.set(
                            f"{size} · {self._clock(remaining)} left"
                        )
                    else:
                        self.elapsed_var.set(size)
                else:
                    duration = float(self._progress_title.get("duration") or 0.0)
                    if self._progress_fraction > 0:
                        remaining = max(0.0, duration * (1.0 - self._progress_fraction))
                        self.elapsed_var.set(
                            f"Elapsed {elapsed} · {self._clock(remaining)} left"
                        )
                    else:
                        self.elapsed_var.set(f"Elapsed {elapsed} · preparing")
            else:
                self.elapsed_var.set(f"Elapsed {elapsed}")
        self.root.after(80, self._pump)

    def _handle(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "hello":
            self.log_line(f"{PRODUCT} v{__version__} engine started")
        elif kind == "drive":
            self.drives[int(event["index"])] = event
            self._refresh_sources()
        elif kind == "disc":
            self.disc = event["disc"]
            self._render_disc()
            self._set_state("ready")
            self._reset_progress()
            self.elapsed_var.set("")
        elif kind == "opened":
            if not event.get("ok"):
                self._set_state("open_failed")
                self._reset_progress()
        elif kind == "message":
            ident = int(event.get("id", 0))
            text = event.get("text", "")
            self.log_line(f"MSG:{ident}: {text}" if self.settings.get_bool("app_ExpertMode")
                          else text)
            if self.state == "opening":
                self.status_var.set(text)
            if self.settings.get_bool("app_ShowDebug"):
                self._debug(text)
        elif kind == "progress":
            try:
                total = max(1, int(event.get("total") or 65536))
                current = max(0, int(event.get("current") or 0))
            except (TypeError, ValueError):
                return
            self.progress.configure(maximum=total, value=min(current, total))
            phase = str(event.get("phase") or "")
            try:
                fraction = float(event.get("phase_fraction"))
            except (TypeError, ValueError):
                # Older engines only sent the aggregate counter.
                fraction = min(1.0, current / total)
            self._progress_fraction = max(0.0, min(1.0, fraction))
            if event.get("title_number"):
                number = int(event.get("title_number") or 1)
                title_total = int(event.get("title_total") or 1)
                previous = self._progress_title
                self._progress_title = {
                    "index": int(event.get("title") or 0) + 1,
                    "number": number,
                    "total": title_total,
                    "duration": float(event.get("duration") or 0.0),
                    "phase": phase,
                    "stage_current": int(event.get("stage_current") or 0),
                    "stage_total": int(event.get("stage_total") or 0),
                }
                if (previous is None or previous.get("phase") != phase
                        or previous.get("number") != number
                        or previous.get("total") != title_total):
                    self._phase_started = datetime.now().timestamp()
                if self.state == "saving":
                    action = "Staging" if phase == "staging" else "Saving"
                    self.status_var.set(
                        f"{action} title {self._progress_title['index']} "
                        f"({number} of {title_total})"
                    )
        elif kind == "saved":
            self.elapsed_var.set("")
            self._progress_title = None
            if event.get("cancelled"):
                self.status_var.set("Save cancelled")
                self._reset_progress()
            else:
                title = "Save failed" if not event.get("ok") else "Save complete"
                self.status_var.set(title)
                if not event.get("ok"):
                    self._reset_progress()
                    messagebox.showerror(PRODUCT_TAG,
                                         "Saving failed. See the log for details.")
            self._set_state("ready")
        elif kind == "backup_done":
            self.elapsed_var.set("")
            self._progress_title = None
            if event.get("ok"):
                messagebox.showinfo(PRODUCT_TAG, "Backup complete.")
            else:
                messagebox.showerror(PRODUCT_TAG, "Backup failed. See the log for details.")
            self._set_state("ready")
        elif kind == "registered":
            if not event.get("ok"):
                messagebox.showerror(PRODUCT_TAG, "Key not found or invalid")
        elif kind == "error":
            self.log_line(f"Engine error: {event.get('message')}")
        elif kind == "stderr":
            self._debug(event.get("message", ""))
        elif kind == "engine_exit":
            self.log_line("Engine process exited")
        elif kind == "bye":
            pass

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _refresh_sources(self) -> None:
        values = []
        for index in sorted(self.drives):
            drive = self.drives[index]
            if drive.get("present"):
                values.append(f"{drive.get('device', index)} {drive.get('name', '')} [{drive.get('label', '')}]")
        self.source_combo.configure(values=values)
        if values and not self.source_combo.get():
            self.source_combo.set(values[0])
        self._build_open_disc_menu()
        self._set_state(self.state)

    def _build_open_disc_menu(self) -> None:
        self.open_disc_menu.delete(0, "end")
        present = [i for i in sorted(self.drives) if self.drives[i].get("present")]
        if not present:
            self.open_disc_menu.add_command(label="No usable optical drives", state="disabled")
            return
        for index in present:
            drive = self.drives[index]
            self.open_disc_menu.add_command(
                label=f"{drive.get('device', '')} {drive.get('label', 'UNNAMED DISC')}",
                command=lambda i=index: self._open_spec(f"disc:{i}"),
            )

    def _combo_selected(self, _event=None) -> None:
        value = self.source_combo.get()
        for index in sorted(self.drives):
            drive = self.drives[index]
            if value.startswith(str(drive.get("device", "\0"))):
                self._open_spec(f"disc:{index}")
                return

    def _render_disc(self) -> None:
        assert self.disc is not None
        disc = self.disc
        self.tree.delete(*self.tree.get_children())
        self.title_items.clear()
        self.stream_items.clear()
        self.group_items.clear()
        media = self.tree.insert(
            "", "end", text=f"{disc['kind']} disc",
            values=(disc["label"], "", disc.get("size_text", ""),
                    disc["kind"], f"{disc['title_count']} title(s)"),
            open=True, tags=("disc",),
        )
        for title in disc["titles"]:
            glyph = "☑" if title["selected"] else "☐"
            name = title.get("name") or title.get("comment") or "—"
            output = title.get("output_name") or "Auto"
            iid = self.tree.insert(
                media, "end", text=f"{glyph} Title {int(title['index']) + 1}",
                values=(name, title["duration_text"], title["size_text"],
                        disc["kind"], output),
                open=True, tags=("title",),
            )
            self.title_items[iid] = int(title["index"])
            for kind, label in STREAM_GROUPS:
                streams = [s for s in title["streams"] if s.get("kind") == kind]
                if not streams:
                    continue
                group = self.tree.insert(
                    iid, "end", text=label,
                    values=(f"{len(streams)} track(s)", "", "", "", ""),
                    open=True, tags=("group",),
                )
                self.group_items.append(group)
                for stream in streams:
                    sglyph = "☑" if stream.get("selected", True) else "☐"
                    sid = self.tree.insert(
                        group, "end",
                        text=f"{sglyph} {self._stream_name(stream)}",
                        values=(self._stream_detail(stream), "", "",
                                self._stream_source(stream),
                                self._stream_output(stream)),
                        tags=("stream",),
                    )
                    self.stream_items[sid] = (int(title["index"]), int(stream["index"]))
            if title["chapters"]:
                self.tree.insert(
                    iid, "end", text="Chapters",
                    values=(f"{title['chapters']} chapter(s)", "", "", "", ""),
                    tags=("chapter",),
                )
        self._update_info_panel()

    @staticmethod
    def _stream_name(stream: dict) -> str:
        """Short label for a stream row; its group already names the kind."""
        if stream.get("kind") == "subtitle":
            return (stream.get("language_name") or stream.get("language")
                    or stream.get("codec") or "Subtitles")
        return stream.get("codec") or stream.get("kind_label") or "Track"

    @staticmethod
    def _stream_detail(stream: dict) -> str:
        bits: list[str] = []
        kind = stream.get("kind")
        if kind == "video":
            if stream.get("width") and stream.get("height"):
                bits.append(f"{stream['width']}x{stream['height']}")
            if stream.get("fps"):
                bits.append(stream["fps"])
            if stream.get("aspect"):
                bits.append(stream["aspect"])
        elif kind == "audio":
            if stream.get("channels"):
                bits.append(f"{stream['channels']}ch")
            if stream.get("language_name"):
                bits.append(stream["language_name"])
        else:
            if stream.get("codec"):
                bits.append(stream["codec"])
            if stream.get("forced"):
                bits.append("forced")
        return " · ".join(bits)

    @staticmethod
    def _stream_source(stream: dict) -> str:
        if stream.get("kind") == "subtitle":
            return "Forced" if stream.get("forced") else ""
        return (stream.get("codec_long") or stream.get("codec_id")
                or stream.get("codec") or "")

    @staticmethod
    def _stream_output(stream: dict) -> str:
        return stream.get("output_codec") or "Auto"

    def _update_info_panel(self) -> None:
        disc = self.disc
        if disc:
            self.type_var.set(f"Type: {disc['kind']}")
            self.label_var.set(f"Label: {disc['label'] or '—'}")
            self.protection_var.set(f"Protection: {disc['protection'] or 'None'}")
            self.capacity_var.set(f"Capacity: {disc.get('size_text') or '—'}")
        else:
            self.type_var.set("Type: —")
            self.label_var.set("Label: —")
            self.protection_var.set("Protection: —")
            self.capacity_var.set("Capacity: —")

    def log_line(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        if self.settings.get_bool("app_ShowDebug"):
            self._debug(text)

    def _debug(self, text: str) -> None:
        from ..settings import DebugLog
        log = DebugLog(self.settings.data_dir)
        log.write(text)

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------
    # tree selection
    # ------------------------------------------------------------------
    def _on_indicator(self, event) -> bool:
        """True when the click landed on a disclosure arrow."""
        try:
            return "indicator" in self.tree.identify_element(event.x, event.y)
        except tk.TclError:
            return False

    def _tree_click(self, event) -> None:
        if self._on_indicator(event):
            return
        if self.tree.identify_column(event.x) != "#0":
            return
        iid = self.tree.identify_row(event.y)
        if iid in self.title_items or iid in self.stream_items:
            self._toggle(iid)

    def _tree_double(self, event) -> None:
        if self._on_indicator(event):
            return
        iid = self.tree.identify_row(event.y)
        if iid in self.title_items or iid in self.stream_items:
            self._toggle(iid)

    def _tree_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        try:
            self.tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_menu.grab_release()

    def _toggle_selected(self) -> None:
        for iid in self.tree.selection():
            if iid in self.title_items or iid in self.stream_items:
                self._toggle(iid)

    def _title(self, index: int) -> dict | None:
        if not self.disc:
            return None
        return next((t for t in self.disc["titles"] if int(t["index"]) == index), None)

    def _toggle(self, iid: str) -> None:
        if not self.disc:
            return
        if iid in self.title_items:
            index = self.title_items[iid]
            title = self._title(index)
            if title is None:
                return
            title["selected"] = not title["selected"]
            glyph = "☑" if title["selected"] else "☐"
            self.tree.item(iid, text=f"{glyph} Title {index + 1}")
        elif iid in self.stream_items:
            title_index, stream_index = self.stream_items[iid]
            title = self._title(title_index)
            if title is None:
                return
            stream = next((s for s in title["streams"]
                           if int(s["index"]) == stream_index), None)
            if stream is None:
                return
            stream["selected"] = not stream.get("selected", True)
            glyph = "☑" if stream["selected"] else "☐"
            self.tree.item(iid, text=f"{glyph} {self._stream_name(stream)}")
        self._set_state(self.state)

    def _selection_payload(self) -> dict:
        """The title/track ticks as JSON-friendly indices for the engine."""
        if not self.disc:
            return {}
        titles = [int(t["index"]) for t in self.disc["titles"] if t["selected"]]
        streams = {
            str(int(t["index"])): [int(s["index"]) for s in t["streams"]
                                    if s.get("selected", True)]
            for t in self.disc["titles"]
        }
        return {"titles": titles, "streams": streams}

    def _expand_all(self, value: bool) -> None:
        for iid in (*self.tree.get_children(""), *self.title_items,
                    *self.group_items):
            self.tree.item(iid, open=value)

    def _select_all(self, value: bool) -> None:
        if not self.disc:
            return
        for title in self.disc["titles"]:
            title["selected"] = value
        for iid, index in self.title_items.items():
            title = next(t for t in self.disc["titles"] if int(t["index"]) == index)
            self.tree.item(iid, text=("☑" if value else "☐") + f" Title {index + 1}")
        self._set_state(self.state)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def open_files(self) -> None:
        if self.state == "saving":
            return
        patterns = VIDEO_FILTER.replace(";", " ")
        path = filedialog.askopenfilename(
            parent=self.root, title="Select existing video source file",
            filetypes=[("All video files", patterns), ("All files", "*.*")],
        )
        if not path:
            return
        if Path(path).suffix.lower() == ".mkv":
            self._open_spec(path)
        elif Path(path).is_dir():
            self._open_spec(f"file:{path}")
        else:
            self._open_spec(f"file:{path}")

    #: Library pickers share one implementation; only the messages and the
    #: description of what the library unlocks differ.
    _LIBRARY_KINDS = {
        "dvdcss": {
            "name": "libdvdcss", "setting": "app_DvdcssPath",
            "install": stage_library, "error": DvdcssError,
            "installed": 9125,
            "blurb": "CSS-protected DVDs can now be decrypted while they are saved.",
        },
        "aacs": {
            "name": "libaacs", "setting": "app_AacsPath",
            "install": stage_aacs, "error": AacsError,
            "installed": 9134,
            "blurb": ("AACS-protected Blu-rays can now be decrypted while they are "
                      "saved. A KEYDB.cfg containing the disc's keys is also needed."),
        },
        "bdplus": {
            "name": "libbdplus", "setting": "app_BdplusPath",
            "install": stage_bdplus, "error": BdplusError,
            "installed": 9138,
            "blurb": ("BD+ protected Blu-rays can now be patched after AACS "
                      "decryption. libaacs and its key file are required too."),
        },
    }

    def _install_library(self, kind: str) -> None:
        spec = self._LIBRARY_KINDS[kind]
        name = spec["name"]
        path = filedialog.askopenfilename(
            parent=self.root, title=f"Select {name} shared library",
            filetypes=[
                (f"{name} library", f"{name}*.dll {name}*.so* {name}*.dylib"),
                ("Shared libraries", "*.dll *.so* *.dylib"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            target = spec["install"](path, self.settings.data_dir)
        except spec["error"] as exc:
            messagebox.showerror(PRODUCT_TAG, str(exc), parent=self.root)
            return
        except OSError as exc:
            messagebox.showerror(PRODUCT_TAG, f"Could not install {name}: {exc}",
                                 parent=self.root)
            return
        if not self._apply_settings({spec["setting"]: str(target)}):
            return
        self.log_line(messages.render(spec["installed"], target))
        messagebox.showinfo(
            PRODUCT_TAG,
            f"{name} is ready.\n\n{spec['blurb']}\nLibrary: {target}",
            parent=self.root,
        )
        self._reopen_after_setting()

    def load_libdvdcss(self) -> None:
        """Install a user-supplied libdvdcss copy for CSS DVDs."""
        self._install_library("dvdcss")

    def load_libaacs(self) -> None:
        """Install a user-supplied libaacs copy for AACS Blu-rays."""
        self._install_library("aacs")

    def load_libbdplus(self) -> None:
        """Install a user-supplied libbdplus copy for BD+ Blu-rays."""
        self._install_library("bdplus")

    def load_aacs_keyfile(self) -> None:
        """Point libaacs at an existing KEYDB.cfg (never copied or displayed)."""
        path = filedialog.askopenfilename(
            parent=self.root, title="Select AACS key file (KEYDB.cfg)",
            filetypes=[
                ("AACS key database", "KEYDB.cfg KEYDB.cfg.txt *.cfg"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        if not Path(path).is_file():
            messagebox.showerror(PRODUCT_TAG, f"Key file not found: {path}",
                                 parent=self.root)
            return
        if not self._apply_settings({"app_AacsKeyFile": path}):
            return
        self.log_line(messages.render(9139, path))
        messagebox.showinfo(
            PRODUCT_TAG,
            "AACS key file selected.\n\n"
            "libaacs reads the disc's keys from KEYDB.cfg; the file is used "
            "where it is and is never copied into the application data "
            f"folder.\nKey file: {path}",
            parent=self.root,
        )
        self._reopen_after_setting()

    def _apply_settings(self, values: dict[str, object]) -> bool:
        """Persist settings, forward them to the engine and report failure."""
        for key, value in values.items():
            self.settings.set(key, value)
        try:
            self.settings.save()
        except OSError:
            messagebox.showerror(PRODUCT_TAG, "Failed to save settings", parent=self.root)
            return False
        self.client.send(cmd="settings", values=values)
        return True

    def _reopen_after_setting(self) -> None:
        if self.source_spec and self.state in ("ready", "open_failed"):
            self._open_spec(self.source_spec)

    def clear_libdvdcss(self) -> None:
        if not self._apply_settings({"app_DvdcssPath": ""}):
            return
        self.log_line(messages.render(9126))
        self._reopen_after_setting()

    def clear_aacs_libraries(self) -> None:
        values = {"app_AacsPath": "", "app_BdplusPath": "",
                  "app_AacsKeyFile": ""}
        if not self._apply_settings(values):
            return
        self.log_line(messages.render(9140))
        self._reopen_after_setting()

    def _open_spec(self, spec: str) -> None:
        self.source_spec = spec
        self._busy("Opening disc", "Reading disc structure")
        self._set_state("opening")
        self.client.send(cmd="open", spec=spec)

    def close_disc(self) -> None:
        self.client.send(cmd="close")
        self.disc = None
        self.tree.delete(*self.tree.get_children())
        self.title_items.clear()
        self.stream_items.clear()
        self.group_items.clear()
        self._update_info_panel()
        self._set_state("no_source")
        self._reset_progress()
        self.elapsed_var.set("")

    def eject_disc(self) -> None:
        device = ""
        prefix, rest = (self.source_spec.split(":", 1) + [""])[:2] if ":" in self.source_spec else ("", "")
        if prefix == "disc" and rest.isdigit():
            device = str(self.drives.get(int(rest), {}).get("device", ""))
        elif prefix == "dev":
            device = rest
        if device:
            drivelib.eject(device)
        self.close_disc()

    def set_output_folder(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="Select output folder")
        if not path:
            return
        self.output_dir = path
        self.output_var.set(path)
        self.settings.set_output_dir(path)
        try:
            self.settings.save()
        except OSError:
            messagebox.showerror(PRODUCT_TAG, "Failed to save settings")

    def save_titles(self) -> None:
        if not self.disc:
            return
        if not any(t["selected"] for t in self.disc["titles"]):
            messagebox.showwarning(PRODUCT_TAG, "No titles were selected")
            return
        directory = self.output_dir
        if not directory:
            directory = filedialog.askdirectory(parent=self.root, title="Select output folder")
            if not directory:
                return
            self.output_dir = directory
            self.output_var.set(directory)
        self._busy("Saving titles", "Preparing title list")
        self._set_state("saving")
        self.client.send(cmd="save_all", dest=directory, **self._selection_payload())

    def cancel_save(self) -> None:
        if self.state != "saving":
            return
        self.status_var.set("Cancelling...")
        self.client.send(cmd="cancel")

    def backup_disc(self) -> None:
        if not self.disc:
            return
        dialog = BackupDialog(self.root, self.settings)
        result = dialog.show()
        if not result:
            return
        directory, decrypt = result
        self._busy("Backup disc", "Copying files")
        self._set_state("saving")
        self.client.send(cmd="backup", spec=self.source_spec, dest=directory,
                         decrypt=decrypt)

    def preferences(self) -> None:
        dialog = PreferencesDialog(self.root, self.settings,
                                   on_apply=self._apply_preferences)
        dialog.show()

    def _apply_preferences(self, values: dict) -> None:
        if not self._apply_settings(values):
            return
        self.output_dir = str(self.settings.output_dir)
        self.output_var.set(self.output_dir)
        self.log_line("Some settings will apply next time you start the application.")

    def about(self) -> None:
        AboutDialog(self.root, self.settings).show()

    def register(self) -> None:
        def applied(key: str) -> None:
            self.client.send(cmd="register", key=key)
        RegisterDialog(self.root, self.settings, apply_key=applied).show()

    def purchase(self) -> None:
        messagebox.showinfo(PRODUCT_TAG,
                            "This clean-room build is not for sale.\n"
                            "See the project documentation for key generation.")

    def online_help(self) -> None:
        HelpDialog(
            self.root, f"{PRODUCT} help",
            "Keyboard shortcuts\n"
            "  Ctrl+O   Open files\n"
            "  Ctrl+Q   Quit\n"
            "  F5       Re-enumerate drives\n\n"
            "Workflow\n"
            "  1. Insert a disc or choose Open files.\n"
            "  2. Wait for the title tree to appear.\n"
            "  3. Tick the titles to keep.\n"
            "  4. Set the output folder.\n"
            "  5. Save selected titles.\n\n"
            "Supported sources: DVD-Video, Blu-ray (BDMV/BDAV), unencrypted\n"
            "MKV files, ISO9660 disc images and folders.\n\n"
            "Content protection\n"
            "  OpticRip does not ship any decryption library. CSS DVDs can\n"
            "  use a libdvdcss copy (File > Load libdvdcss library...).\n"
            "  AACS Blu-rays use libaacs plus a KEYDB.cfg key file; BD+\n"
            "  also needs libbdplus and its VM configuration files. Choose\n"
            "  the libraries under File > Load libaacs/libbdplus library...\n"
            "  Protected discs are listed even without the libraries, but\n"
            "  saving is refused until decryption is available.\n\n"
            "Command line\n"
            "  python -m opticrip.cli -r info disc:0\n"
            "  python -m opticrip.cli mkv disc:0 0 C:\\Video\n",
        ).show()

    def _toggle_toolbar(self) -> None:
        if self.toolbar_var.get():
            self.toolbar.grid()
        else:
            self.toolbar.grid_remove()

    # ------------------------------------------------------------------
    # window state
    # ------------------------------------------------------------------
    def _restore_geometry(self) -> None:
        geometry, state = self.settings.load_window_state()
        width, height, x, y = self._fit_to_screen(geometry)
        try:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self.root.geometry("1060x720")
        layout: dict = {}
        if state:
            try:
                layout = json.loads(state)
            except ValueError:
                layout = {}
        self.root.after(150, lambda: self._apply_layout(layout))

    def _fit_to_screen(self, geometry: str) -> tuple[int, int, int, int]:
        """Clamp a remembered geometry to the display we are actually on.

        The reference restores the last window rectangle blindly; doing that
        with a rectangle saved on a larger or mirrored display leaves the menu
        bar and the left toolbar outside the screen.  Everything is trimmed to
        the visible area and re-centred when the saved position is unusable.
        """
        screen_w = max(self.root.winfo_screenwidth(), 640)
        screen_h = max(self.root.winfo_screenheight(), 480)
        min_w, min_h = 920, 620
        max_w = max(min_w, screen_w - 40)
        max_h = max(min_h, screen_h - 150)
        width, height = 1180, 780
        x = y = None
        match = re.match(r"(\d+)x(\d+)([+-])(-?\d+)([+-])(-?\d+)", geometry or "")
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            x = int(match.group(4)) * (-1 if match.group(3) == "-" else 1)
            y = int(match.group(6)) * (-1 if match.group(5) == "-" else 1)
        width = min(max(width, min_w), max_w)
        height = min(max(height, min_h), max_h)
        if x is None or y is None:
            x = (screen_w - width) // 2
            y = max(0, (screen_h - height) // 2 - 20)
        x = min(max(x, 0), max(0, screen_w - width))
        y = min(max(y, 0), max(0, screen_h - height - 90))
        return width, height, x, y

    def _apply_layout(self, layout: dict) -> None:
        """Give the title tree about two thirds and keep the log usable."""
        try:
            self.root.update_idletasks()
            total = self.paned.winfo_height()
            if total < 240:
                total = max(self.root.winfo_height() - 260, 360)
            top_min = min(220, int(total * 0.5))
            log_min = min(150, int(total * 0.35))
            position = int(layout.get("vertical", total * 0.62))
            self.paned.sashpos(0, max(min(position, total - log_min), top_min))
        except (tk.TclError, ValueError, TypeError):
            pass

    def _on_close(self) -> None:
        try:
            layout = {"vertical": self.paned.sashpos(0)}
            self.settings.save_window_state(self.root.geometry(), json.dumps(layout))
            self.settings.save()
        except (tk.TclError, OSError):
            pass
        self.client.close()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    spec = ""
    for index, argument in enumerate(argv):
        if argument in ("--open", "-o") and index + 1 < len(argv):
            spec = argv[index + 1]
        elif not argument.startswith("-") and not spec:
            spec = argument
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    window = MainWindow(root)
    if spec:
        root.after(1200, lambda: window._open_spec(spec))
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
