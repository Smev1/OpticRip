"""Dialogs: About, Register, Backup, Preferences, Help."""

from __future__ import annotations

import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import BANNER, COPYRIGHT, GUI_VERSION, PRODUCT, PRODUCT_TAG, __version__
from .. import registration
from ..profiles import discover
from ..settings import DEFAULTS, Settings

LANGUAGES = [
    ("English", "eng"), ("Deutsch", "deu"), ("Français", "fra"), ("Español", "spa"),
    ("Italiano", "ita"), ("Nederlands", "nld"), ("Svenska", "swe"),
    ("Norsk", "nor"), ("Dansk", "dan"), ("Polski", "pol"), ("日本語", "jpn"),
    ("中文", "chi"), ("Português", "ptb"), ("Türkçe", "tur"), ("فارسی", "per"),
]
PREFERRED = [("Auto", "")] + [(name, code) for name, code in LANGUAGES]


class _Dialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: object | None = None

    def _buttons(self, row: int, ok_text: str = "OK", cancel: bool = True) -> ttk.Frame:
        frame = ttk.Frame(self)
        frame.grid(row=row, column=0, sticky="e", padx=12, pady=(6, 12))
        if cancel:
            ttk.Button(frame, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        self.ok_button = ttk.Button(frame, text=ok_text, command=self._ok)
        self.ok_button.pack(side="right")
        return frame

    def _ok(self) -> None:
        self.destroy()

    def show(self) -> object | None:
        self.grab_set()
        self.wait_window()
        return self.result


class AboutDialog(_Dialog):
    def __init__(self, parent: tk.Misc, settings: Settings) -> None:
        super().__init__(parent, f"About {PRODUCT_TAG}")
        key = settings.get_str("app_Key")
        valid, _ = registration.validate(key) if key else (False, "")
        license_type = "Registered" if valid else "Time-limited"
        first = settings.get_int("app_FirstRun", 0)
        if not first:
            first = int(datetime.now().timestamp())
            settings.set("app_FirstRun", first)
            try:
                settings.save()
            except OSError:
                pass
        expires = datetime.fromtimestamp(first) + timedelta(days=30)
        text = (
            f"{BANNER}\n"
            f"GUI version: {GUI_VERSION}\n"
            f"License type : {license_type}\n"
            f"Evaluation expires : {expires.strftime('%a %b %d %Y')} ({(expires - datetime.now()).days} day(s))\n"
            f"This version expires : Fri Oct 09 2026 (0 day(s))\n"
            f"Website URL: https://example.invalid/opticrip\n"
            f"{COPYRIGHT}\n\n"
            f"{PRODUCT} is a clean-room implementation of the documented\n"
            f"MakeMKV behaviour. It does not contain any third-party code\n"
            f"or decryption technology."
        )
        label = ttk.Label(self, text=text, justify="left", padding=16)
        label.grid(row=0, column=0, sticky="w")
        self._buttons(1, cancel=False)


class RegisterDialog(_Dialog):
    def __init__(self, parent: tk.Misc, settings: Settings,
                 apply_key=None) -> None:
        super().__init__(parent, "Enter registration code")
        self.settings = settings
        self.apply_key = apply_key
        frame = ttk.Frame(self, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            frame,
            text=("Please enter the registration (license) key.\n"
                  "You can also paste the entire registration email message,\n"
                  "or load any text file containing a key."),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="License key :").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.key_var = tk.StringVar(value=settings.get_str("app_Key"))
        self.entry = ttk.Entry(frame, textvariable=self.key_var, width=52)
        self.entry.grid(row=1, column=1, sticky="we", pady=(12, 0))
        ttk.Button(frame, text="Load file...", command=self._load).grid(
            row=2, column=1, sticky="e", pady=(4, 0))
        self.type_var = tk.StringVar(value="License type : Time-limited")
        self.expiry_var = tk.StringVar(value="License expiration : ")
        ttk.Label(frame, textvariable=self.type_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(frame, textvariable=self.expiry_var).grid(row=4, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Purchase", command=self._purchase).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="left")
        self.entry.focus_set()
        self.key_var.trace_add("write", lambda *_: self._preview())
        self._preview()

    def _preview(self) -> None:
        valid, key = registration.validate(self.key_var.get())
        if valid:
            self.type_var.set("License type : Registered")
            self.expiry_var.set("License expiration : never")
        elif self.key_var.get().strip():
            self.type_var.set("License type : Invalid")
            self.expiry_var.set("License expiration : ")
        else:
            self.type_var.set("License type : Time-limited")
            self.expiry_var.set("License expiration : ")

    def _load(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Load registration key",
                                          filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.key_var.set(registration.read_key_file(path).strip())

    def _purchase(self) -> None:
        messagebox.showinfo(PRODUCT_TAG,
                            "This clean-room build is not for sale.\n"
                            "Use a key in the documented ORIP format.")

    def _ok(self) -> None:
        valid, key = registration.validate(self.key_var.get())
        if not valid:
            self._preview()
            return
        self.settings.set("app_Key", key)
        self.settings.set("app_KeyHash", registration.key_hash(key))
        try:
            self.settings.save()
        except OSError:
            messagebox.showerror(PRODUCT_TAG, "Failed to store registration code in settings database")
            return
        if self.apply_key is not None:
            self.apply_key(key)
        self.result = key
        messagebox.showinfo(PRODUCT_TAG,
                            "Thank you. Please restart the application for changes to take effect.")
        self.destroy()


class BackupDialog(_Dialog):
    def __init__(self, parent: tk.Misc, settings: Settings) -> None:
        super().__init__(parent, f"{PRODUCT} disc backup")
        self.settings = settings
        frame = ttk.Frame(self, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(
            frame,
            text=("This function backs up the disc to a folder on your hard drive.\n\n"
                  "You can choose to save the disc data as-is, without decrypting or\n"
                  "changing any files, or to decrypt video files\n"
                  "and change the metadata of the backup.\n\n"
                  "In all cases the produced backup can later be opened by the\n"
                  "application as a video source."),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Please specify output directory").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(12, 2))
        self.dir_var = tk.StringVar(value=settings.get_str("path_BackupDirMRU").lstrip("*"))
        ttk.Entry(frame, textvariable=self.dir_var, width=56).grid(row=2, column=0, sticky="we")
        ttk.Button(frame, text="Browse...", command=self._browse).grid(row=2, column=1, padx=(6, 0))
        self.decrypt_var = tk.BooleanVar(value=settings.get_bool("app_BackupDecrypted", True))
        ttk.Checkbutton(frame, text="Decrypt video files",
                        variable=self.decrypt_var).grid(row=3, column=0, sticky="w", pady=(10, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")

    def _browse(self) -> None:
        path = filedialog.askdirectory(parent=self, title="Select backup folder")
        if path:
            self.dir_var.set(os_path(path))

    def _ok(self) -> None:
        directory = self.dir_var.get().strip()
        if not directory:
            messagebox.showwarning(PRODUCT_TAG, "Please specify output directory", parent=self)
            return
        self.settings.set("app_BackupDecrypted", int(self.decrypt_var.get()))
        self.settings.set("path_BackupDirMRU", "*" + directory)
        try:
            self.settings.save()
        except OSError:
            pass
        self.result = (directory, self.decrypt_var.get())
        self.destroy()


class PreferencesDialog(_Dialog):
    def __init__(self, parent: tk.Misc, settings: Settings, on_apply=None) -> None:
        super().__init__(parent, f"{PRODUCT} preferences")
        self.settings = settings
        self.on_apply = on_apply
        self.resizable(True, True)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self._general()
        self._video()
        self._io()
        buttons = ttk.Frame(self)
        buttons.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        ttk.Button(buttons, text="Apply", command=self._apply).pack(side="right")
        self.minsize(560, 520)

    # -- tabs -----------------------------------------------------------
    def _general(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="General")
        dest = ttk.LabelFrame(tab, text="Default destination", padding=8)
        dest.pack(fill="x")
        self.dest_var = tk.StringVar(value=self.settings.get_str("app_DestinationType", "Auto"))
        for i, label in enumerate(("None", "Auto", "SemiAuto", "Custom")):
            ttk.Radiobutton(dest, text=label, value=label, variable=self.dest_var).grid(
                row=0, column=i, sticky="w", padx=(0, 12))
        row = ttk.Frame(dest)
        row.grid(row=1, column=0, columnspan=4, sticky="we", pady=(8, 0))
        self.dest_dir = tk.StringVar(value=self.settings.get_str("app_DestinationDir"))
        ttk.Entry(row, textvariable=self.dest_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=lambda: self._pick_dir(self.dest_dir)).pack(
            side="left", padx=(6, 0))
        opts = ttk.LabelFrame(tab, text="Options", padding=8)
        opts.pack(fill="x", pady=(10, 0))
        self.min_len = tk.IntVar(value=self.settings.get_int("app_MinTitleLength", 120))
        line = ttk.Frame(opts)
        line.pack(fill="x")
        ttk.Label(line, text="Minimum title length (seconds) :").pack(side="left")
        ttk.Spinbox(line, from_=0, to=100000, textvariable=self.min_len, width=8).pack(
            side="left", padx=(6, 0))
        self.bd_dump = tk.BooleanVar(value=self.settings.get_bool("app_AlwaysCreateBDDumps"))
        ttk.Checkbutton(opts, text="Always create BD+ dumps",
                        variable=self.bd_dump).pack(anchor="w", pady=(6, 0))
        lang = ttk.LabelFrame(tab, text="Language", padding=8)
        lang.pack(fill="x", pady=(10, 0))
        self.interface = tk.StringVar(value=self._language_name(
            self.settings.get_str("app_InterfaceLanguage", "eng")))
        self.preferred = tk.StringVar(value=self._preferred_name(
            self.settings.get_str("app_PreferredLanguage")))
        ttk.Label(lang, text="Interface language :").grid(row=0, column=0, sticky="w")
        ttk.Combobox(lang, textvariable=self.interface, state="readonly",
                     values=[n for n, _ in LANGUAGES], width=24).grid(row=0, column=1, padx=6, pady=2)
        ttk.Label(lang, text="Preferred language :").grid(row=1, column=0, sticky="w")
        ttk.Combobox(lang, textvariable=self.preferred, state="readonly",
                     values=[n for n, _ in PREFERRED], width=24).grid(row=1, column=1, padx=6, pady=2)

    def _video(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="Video")
        self.retry = tk.IntVar(value=self.settings.get_int("app_ReadRetryCount", 5))
        self.buffer = tk.IntVar(value=self.settings.get_int("app_ReadBufferMB", 0))
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        ttk.Label(grid, text="Read retry count :").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(grid, from_=0, to=100, textvariable=self.retry, width=8).grid(
            row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(grid, text="Read buffer size (MB) :").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(grid, from_=0, to=1024, textvariable=self.buffer, width=8).grid(
            row=1, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(grid, text="(0 = Auto)").grid(row=1, column=2, sticky="w")
        self.single_drive = tk.BooleanVar(value=self.settings.get_bool("app_AskSingleDrive"))
        self.disable_direct = tk.BooleanVar(value=self.settings.get_bool("app_DisableDirectAccess"))
        ttk.Checkbutton(tab, text="Ask for single drive mode",
                        variable=self.single_drive).pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(tab, text="Disable direct access",
                        variable=self.disable_direct).pack(anchor="w")
        protect = ttk.LabelFrame(tab, text="DVD structure protection removal method", padding=8)
        protect.pack(fill="x", pady=(10, 0))
        self.structure = tk.StringVar(value=self.settings.get_str("app_StructureProtection", "Auto"))
        ttk.Combobox(protect, textvariable=self.structure, state="readonly",
                     values=["Auto", "CellWalk", "CellTrim", "None"]).pack(fill="x")
        ttk.Label(tab, text=("The clone never removes CSS protection; these options only\n"
                             "control how damaged/structured titles are interpreted."),
                  justify="left", foreground="#555").pack(anchor="w", pady=(10, 0))

    def _io(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="IO")
        data = ttk.LabelFrame(tab, text=f"{PRODUCT} data directory", padding=8)
        data.pack(fill="x")
        self.data_dir = tk.StringVar(value=str(self.settings.data_dir))
        ttk.Entry(data, textvariable=self.data_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(data, text="Browse...", command=lambda: self._pick_dir(self.data_dir)).pack(
            side="left", padx=(6, 0))
        self.debug = tk.BooleanVar(value=self.settings.get_bool("app_ShowDebug"))
        self.expert = tk.BooleanVar(value=self.settings.get_bool("app_ExpertMode"))
        self.avsync = tk.BooleanVar(value=self.settings.get_bool("app_ShowAVSyncMessages"))
        for text, var in (("Log debug messages", self.debug),
                          ("Expert mode", self.expert),
                          ("Show AV synchronization messages", self.avsync)):
            ttk.Checkbutton(tab, text=text, variable=var).pack(anchor="w", pady=(6, 0))
        online = ttk.LabelFrame(tab, text="Online updates", padding=8)
        online.pack(fill="x", pady=(8, 0))
        self.internet = tk.BooleanVar(value=self.settings.get_bool("app_UpdateEnable", True))
        self.autosubmit = tk.BooleanVar(value=self.settings.get_bool("app_AutoSubmitEnabled"))
        ttk.Checkbutton(online, text="Enable Internet access", variable=self.internet).pack(anchor="w")
        ttk.Checkbutton(online, text="Submit dumps automatically", variable=self.autosubmit).pack(anchor="w")
        proxy = ttk.Frame(online)
        proxy.pack(fill="x", pady=(4, 0))
        ttk.Label(proxy, text="Proxy server :").pack(side="left")
        self.proxy = tk.StringVar(value=self.settings.get_str("app_Proxy"))
        ttk.Entry(proxy, textvariable=self.proxy).pack(side="left", fill="x", expand=True, padx=6)
        advanced = ttk.LabelFrame(tab, text="Advanced", padding=8)
        advanced.pack(fill="x", pady=(8, 0))
        self.profile_name = tk.StringVar(value=self.settings.get_str("app_DefaultProfileName", "default"))
        self.selection = tk.StringVar(value=self.settings.get_str("app_DefaultSelectionString"))
        self.template = tk.StringVar(value=self.settings.get_str("app_DefaultOutputFileName"))
        self.java = tk.StringVar(value=self.settings.get_str("app_Java"))
        self.ccextractor = tk.StringVar(value=self.settings.get_str("app_ccextractor"))
        self._labelled_entry(advanced, "Default profile :", self.profile_name, 0)
        self._labelled_entry(advanced, "Default selection rule :", self.selection, 1)
        self._labelled_entry(advanced, "Output file name template :", self.template, 2)
        self._labelled_entry(advanced, "<Java> executable location", self.java, 3)
        self._labelled_entry(advanced, "<ccextractor> executable location", self.ccextractor, 4)

    def _labelled_entry(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=1)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="we", padx=(6, 0), pady=1)
        parent.columnconfigure(1, weight=1)

    # -- helpers --------------------------------------------------------
    def _pick_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(parent=self)
        if path:
            var.set(os_path(path))

    @staticmethod
    def _language_name(code: str) -> str:
        for name, value in LANGUAGES:
            if value == code:
                return name
        return "English"

    @staticmethod
    def _preferred_name(code: str) -> str:
        for name, value in PREFERRED:
            if value == code:
                return name
        return "Auto"

    def _collect(self) -> dict:
        interface = dict((name, code) for name, code in LANGUAGES).get(
            self.interface.get(), "eng")
        preferred = dict((name, code) for name, code in PREFERRED).get(
            self.preferred.get(), "")
        return {
            "app_DestinationType": self.dest_var.get(),
            "app_DestinationDir": self.dest_dir.get(),
            "app_MinTitleLength": int(self.min_len.get()),
            "app_AlwaysCreateBDDumps": int(self.bd_dump.get()),
            "app_InterfaceLanguage": interface,
            "app_PreferredLanguage": preferred,
            "app_ReadRetryCount": int(self.retry.get()),
            "app_ReadBufferMB": int(self.buffer.get()),
            "app_AskSingleDrive": int(self.single_drive.get()),
            "app_DisableDirectAccess": int(self.disable_direct.get()),
            "app_StructureProtection": self.structure.get(),
            "app_DataDir": self.data_dir.get(),
            "app_ShowDebug": int(self.debug.get()),
            "app_ExpertMode": int(self.expert.get()),
            "app_ShowAVSyncMessages": int(self.avsync.get()),
            "app_UpdateEnable": int(self.internet.get()),
            "app_AutoSubmitEnabled": int(self.autosubmit.get()),
            "app_Proxy": self.proxy.get(),
            "app_DefaultProfileName": self.profile_name.get(),
            "app_DefaultSelectionString": self.selection.get(),
            "app_DefaultOutputFileName": self.template.get(),
            "app_Java": self.java.get(),
            "app_ccextractor": self.ccextractor.get(),
        }

    def _apply(self) -> bool:
        values = self._collect()
        for key, value in values.items():
            self.settings.set(key, value)
        try:
            self.settings.save()
        except OSError:
            messagebox.showerror(PRODUCT_TAG, "Failed to save settings", parent=self)
            return False
        if self.on_apply is not None:
            self.on_apply(values)
        return True

    def _ok(self) -> None:
        if self._apply():
            self.result = True
            self.destroy()


class HelpDialog(_Dialog):
    def __init__(self, parent: tk.Misc, title: str, text: str) -> None:
        super().__init__(parent, title)
        self.resizable(True, True)
        widget = tk.Text(self, wrap="word", width=80, height=28, padx=12, pady=12)
        widget.insert("1.0", text)
        widget.configure(state="disabled")
        widget.grid(row=0, column=0, sticky="nsew")
        self._buttons(1, cancel=False)
        self.minsize(480, 320)


def os_path(path: str) -> str:
    """Normalise a Tk path for Windows display."""
    return str(Path(path))
