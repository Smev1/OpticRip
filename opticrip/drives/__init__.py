"""Optical drive discovery. OS specific code lives behind DriveProvider."""
from .base import CompositeProvider, DriveWatcher, poll_until_changed  # noqa: F401
from .linux import LinuxDriveProvider  # noqa: F401
from .virtual import FolderProvider, ImageProvider, AutoPathProvider  # noqa: F401

import sys as _sys

#: platform drive provider factory; Windows imports lazily so Linux stays
#: dependency-free and the codebase boots identically in tests/CI.
if _sys.platform.startswith("win"):  # pragma: no cover - Windows only
    from .windows import WindowsDriveProvider as _Native
else:                                  # pragma: no cover - POSIX
    from .linux import LinuxDriveProvider as _Native


def native_drive_provider(**kwargs):
    """The default real-hardware provider for this OS."""
    return _Native(**kwargs)
