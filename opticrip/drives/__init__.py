"""Optical drive discovery. OS specific code lives behind DriveProvider."""
from .base import CompositeProvider, DriveWatcher, poll_until_changed  # noqa: F401
from .linux import LinuxDriveProvider  # noqa: F401
from .virtual import FolderProvider, ImageProvider, AutoPathProvider  # noqa: F401
