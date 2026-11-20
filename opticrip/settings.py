"""Persistent settings.

The reference stores everything in ``HKCU\\Software\\MakeMKV`` as REG_SZ
(CLONE_SPEC.md §17.1).  A clone must not share that key or it would overwrite
the user's real registration data, so the same *value names* are used under
``HKCU\\Software\\OpticRip`` (override with ``OPTICRIP_REG_ROOT``).  On
non-Windows hosts, and in tests, a JSON file backend under the data directory
is used instead.  Unknown values are preserved (R-PERS-03).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform dependent
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


DEFAULT_SELECTION = (
    "-sel:all,+sel:(favlang|nolang|single),-sel:(havemulti|havecore),"
    "+sel:mvcvideo,=100:all,-10:favlang"
)
DEFAULT_NAME_TEMPLATE = "{NAME2}{-:CMNT2}{-:DT}{title:+DFLT}{_t:N2}"

#: Every setting the application writes.  Keys marked UNKNOWN in the spec
#: (minimum title length, retry count, ...) get clone-local names.
DEFAULTS: dict[str, Any] = {
    "app_Key": "",
    "app_KeyHash": "",
    "app_SiteInfoString": "",
    "app_ShowDebug": 0,
    "app_ExpertMode": 0,
    "app_ShowAVSyncMessages": 0,
    "app_UpdateEnable": 1,
    "app_AutoSubmitEnabled": 0,
    "app_Proxy": "",
    "app_DestinationType": "Auto",
    "app_DestinationDir": "",
    "app_BackupDecrypted": 1,
    "app_InterfaceLanguage": "eng",
    "app_PreferredLanguage": "",
    "app_DefaultProfileName": "default",
    "app_DefaultSelectionString": DEFAULT_SELECTION,
    "app_DefaultOutputFileName": DEFAULT_NAME_TEMPLATE,
    "app_DataDir": "",
    "app_Java": "",
    "app_ccextractor": "",
    "app_DvdcssPath": "",
    "app_AacsPath": "",
    "app_BdplusPath": "",
    "app_AacsKeyFile": "",
    "app_MinTitleLength": 120,
    "app_ReadRetryCount": 5,
    "app_ReadBufferMB": 0,
    "app_AskSingleDrive": 1,
    "app_DisableDirectAccess": 0,
    "app_StructureProtection": "Auto",
    "app_StreamingPort": 5100,
    "path_DestDir": "",
    "path_DestDirMRU": "",
    "path_BackupDirMRU": "",
    "path_OpenFile": "",
    "screen_geometry": "",
    "screen_state": "",
    "sdf_Stop": "",
}


def default_data_dir() -> Path:
    override = os.environ.get("OPTICRIP_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".opticrip"


class JsonBackend:
    """Small file-backed store used by tests and non-Windows hosts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _store(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), "utf-8")
        os.replace(tmp, self.path)

    def values(self) -> dict[str, Any]:
        return dict(self._load())

    def get(self, name: str) -> Any | None:
        return self._load().get(name)

    def set(self, name: str, value: Any) -> None:
        data = self._load()
        data[name] = value
        self._store(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if name in data:
            del data[name]
            self._store(data)


class RegistryBackend:
    """``HKCU\\Software\\OpticRip`` (or ``$OPTICRIP_REG_ROOT``)."""

    def __init__(self, root: str) -> None:
        assert winreg is not None
        self.root = root

    def _key(self, access: int):
        return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.root, 0, access)

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            with self._key(winreg.KEY_READ) as key:
                index = 0
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    out[name] = value
                    index += 1
        except OSError:
            return {}
        return out

    def get(self, name: str) -> Any | None:
        try:
            with self._key(winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def set(self, name: str, value: Any) -> None:
        with self._key(winreg.KEY_WRITE) as key:
            if isinstance(value, bool):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
            elif isinstance(value, int):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))

    def delete(self, name: str) -> None:
        try:
            with self._key(winreg.KEY_WRITE) as key:
                winreg.DeleteValue(key, name)
        except OSError:
            pass


class Settings:
    """Typed façade over a backend."""

    def __init__(self, backend: JsonBackend | RegistryBackend | None = None) -> None:
        if backend is None:
            backend = self.default_backend()
        self.backend = backend
        self._cache: dict[str, Any] = {}
        self._changed: set[str] = set()

    @staticmethod
    def default_backend() -> JsonBackend | RegistryBackend:
        if winreg is not None and sys.platform == "win32" and not os.environ.get("OPTICRIP_JSON_SETTINGS"):
            root = os.environ.get("OPTICRIP_REG_ROOT", r"Software\OpticRip")
            return RegistryBackend(root)
        return JsonBackend(default_data_dir() / "settings.json")

    # -- values ---------------------------------------------------------
    def raw(self) -> dict[str, Any]:
        return self.backend.values()

    def get(self, name: str, default: Any = None) -> Any:
        if name in self._cache:
            return self._cache[name]
        value = self.backend.get(name)
        if value is None:
            value = DEFAULTS.get(name, default)
        self._cache[name] = value
        return value

    def get_str(self, name: str, default: str = "") -> str:
        value = self.get(name, default)
        return str(value) if value is not None else default

    def get_int(self, name: str, default: int = 0) -> int:
        try:
            return int(self.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, name: str, default: bool = False) -> bool:
        value = self.get(name, int(default))
        try:
            return bool(int(value))
        except (TypeError, ValueError):
            return str(value).lower() in ("1", "true", "yes")

    def set(self, name: str, value: Any) -> None:
        self._cache[name] = value
        self._changed.add(name)

    def save(self) -> None:
        """Persist changed values.  Raises OSError if the backend refuses."""
        for name in self._changed:
            self.backend.set(name, self._cache[name])
        self._changed.clear()

    # -- well-known values ---------------------------------------------
    @property
    def data_dir(self) -> Path:
        configured = self.get_str("app_DataDir").strip()
        path = Path(os.path.expandvars(configured)) if configured else default_data_dir()
        return path

    @property
    def output_dir(self) -> Path:
        configured = self.get_str("path_DestDir").strip()
        if configured:
            return Path(os.path.expandvars(configured))
        return self.data_dir / "output"

    def set_output_dir(self, path: str | os.PathLike[str]) -> None:
        text = str(path)
        self.set("path_DestDir", text)
        self.set("path_DestDirMRU", text)

    def bump_launch_counter(self) -> int:
        """Reproduce ``kslc_<hex>`` (R-PERS-01); not user data."""
        values = self.backend.values()
        names = [n for n in values if n.startswith("kslc_")]
        name = names[0] if names else f"kslc_{int(time.time()):016x}"
        try:
            counter = int(str(values.get(name, "0")), 16) + 1
        except ValueError:
            counter = 1
        self.backend.set(name, f"{counter:08x}")
        return counter

    # -- window state ---------------------------------------------------
    @staticmethod
    def encode_state(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode_state(value: str) -> str:
        try:
            return base64.b64decode(value.encode("ascii")).decode("utf-8")
        except Exception:
            return ""

    def save_window_state(self, geometry: str, state: str) -> None:
        self.set("screen_geometry", self.encode_state(geometry))
        self.set("screen_state", self.encode_state(state))

    def load_window_state(self) -> tuple[str, str]:
        return (
            self.decode_state(self.get_str("screen_geometry")),
            self.decode_state(self.get_str("screen_state")),
        )


class DebugLog:
    """``<data dir>/OpticRip_log.txt`` with one rolled-over backup.

    The reference uses ``MakeMKV_log.txt`` and ``MakeMKV_log_<n>.txt``; the
    clone keeps the convention but its own file name.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "OpticRip_log.txt"

    def enable(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024:
            rolled = self.directory / "OpticRip_log_1.txt"
            try:
                if rolled.exists():
                    rolled.unlink()
                self.path.rename(rolled)
            except OSError:
                pass
        return self.path

    def write(self, line: str) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass
