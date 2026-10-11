"""Exception hierarchy shared by every backend.

Keeping a single hierarchy means the GUI can present a useful, specific
message for every failure without knowing which component raised it.
"""

from __future__ import annotations


class OpticRipError(Exception):
    """Base class for all OpticRip errors."""

    #: short, user-facing label used by the GUI
    title = "Error"

    def __init__(self, message: str, *, hint: str | None = None,
                 detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: actionable suggestion shown under the message
        self.hint = hint
        #: raw tool output / traceback fragment for the diagnostics panel
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class DriveNotFoundError(OpticRipError):
    title = "Optical drive not found"


class DriveAccessError(OpticRipError):
    title = "Cannot access optical drive"


class NoDiscError(OpticRipError):
    title = "No disc in drive"


class UnsupportedMediaError(OpticRipError):
    title = "Unsupported disc"


class DiscReadError(OpticRipError):
    title = "Disc read error"


class DiscStructureError(OpticRipError):
    """Disc is readable but its structure is malformed/unparsable."""

    title = "Malformed disc structure"


class ToolMissingError(OpticRipError):
    title = "External tool missing"


class ExtractionError(OpticRipError):
    title = "Extraction failed"


class RemuxError(OpticRipError):
    title = "MKV muxing failed"


class InsufficientSpaceError(OpticRipError):
    title = "Not enough free disk space"


class PermissionError_(OpticRipError):
    title = "Permission denied"


class OutputExistsError(OpticRipError):
    title = "Output file already exists"


class ValidationFailedError(OpticRipError):
    title = "Output validation failed"


class CancelledError(OpticRipError):
    """Raised when an operation is cancelled by the user (not a failure)."""

    title = "Cancelled"


class EncryptedDiscError(UnsupportedMediaError):
    """Raised when a disc is protected and no lawful decryption path exists."""

    title = "Encrypted / protected disc"

    def __init__(self, message: str, *, scheme: str | None = None,
                 hint: str | None = None, detail: str | None = None) -> None:
        super().__init__(message, hint=hint, detail=detail)
        self.scheme = scheme  # e.g. "CSS", "CPPM", "AACS", "BD+"
