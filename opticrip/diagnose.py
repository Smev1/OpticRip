"""Explain a failure in the words the GUI log shows.

Every failure ends up in the log pane, so the log has to answer three
questions on its own: what went wrong, what probably caused it and what to try
next.  :func:`explain` maps an exception plus a little context onto that
answer.  The mapping is deliberately rule-based and ordered from the most
specific signal to the most general, so an ``OSError`` raised three layers
down still gets a useful answer, and an unrecognised error says so instead of
inventing a cause.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass

from .aacs import AacsError
from .backup import BackupError
from .bddecrypt import BlurayDecryptError
from .bdmv import BdmvError
from .bdplus import BdplusError
from .dvd import DvdError
from .dvdcss import DvdcssError
from .ffmpeg import FFmpegError
from .iso9660 import IsoError
from .profiles import ProfileError

#: How much of a raw exception string is kept before it stops being readable.
_LIMIT = 300


@dataclass(frozen=True)
class Diagnosis:
    """One failure, said plainly.

    ``headline`` is a short clause naming the probable cause, ``problem`` is
    the technical detail worth searching for, ``causes`` runs from most to
    least likely and ``advice`` is the single most useful next step.
    """

    headline: str
    problem: str
    causes: tuple[str, ...]
    advice: str

    def summary(self) -> str:
        """One line for the numeric message catalogue, cause first."""
        return f"{self.headline} ({self.problem})" if self.problem else self.headline


# Windows system error codes, as the drive and the file system report them.
_WINDOWS: dict[int, tuple[str, tuple[str, ...], str]] = {
    5: (
        "the drive or the output file is locked by another program",
        (
            "Another disc tool (MakeMKV, VLC, AnyDVD, ImgBurn) still has the drive open.",
            "The output file is being played or scanned by antivirus while it is written.",
            "The destination folder needs administrator rights on this machine.",
        ),
        "Close every other disc tool, save to a plain folder such as Videos, and retry.",
    ),
    19: (
        "the disc is write-protected",
        ("The drive reported a read-only medium for a write request.",),
        "Check the output folder is on a writable disk and is not itself a disc or a read-only share.",
    ),
    21: (
        "the drive stopped responding",
        (
            "The drive dropped off the USB bus or went to sleep mid-read; a loose cable or "
            "USB power management is the usual reason.",
            "The disc was ejected or the tray was bumped during the rip.",
            "The drive could not focus on the disc at all, which points at a very dirty disc "
            "or the wrong kind of media for this drive.",
        ),
        "Re-seat the USB cable at both ends, keep the drive on mains power if it has an "
        "adapter, then re-open the disc and retry.",
    ),
    23: (
        "a spot on the disc could not be read",
        (
            "Dirt, a smudge or a scratch in that area; the drive retried and gave up.",
            "The disc is fine but the drive's read speed is too high for that area once it "
            "has retried a few times.",
            "A failing laser or drive, if other discs also fail at different places.",
        ),
        "Clean the disc from the centre outwards with a soft lint-free cloth and retry. If it "
        "fails at the same percentage every time, the disc is damaged at that spot.",
    ),
    55: (
        "the drive is no longer present",
        (
            "The USB connection was lost, so Windows removed the device.",
            "An external power supply for the enclosure was switched off or dropped out.",
        ),
        "Reconnect the drive, wait for Windows to see it again, then reopen the disc.",
    ),
    87: (
        "the drive rejected a read request",
        (
            "The disc uses a layout or protection this drive cannot address directly.",
            "The drive's firmware is older than the disc's media profile.",
        ),
        "Try opening the disc again; if it repeats, update the drive firmware or make a "
        "backup with MakeMKV and open that instead.",
    ),
    112: (
        "the output disk is full",
        ("The destination ran out of free space part-way through the rip.",),
        "Free space on the output disk (a full Blu-ray title needs up to 40 GB) and retry into "
        "a folder with room.",
    ),
    1117: (
        "the drive reported a hardware read failure",
        (
            "A mechanical or laser fault inside the drive.",
            "A badly damaged disc that the drive cannot track.",
        ),
        "Test with a known-good disc. If that also fails, the drive itself is the problem.",
    ),
}

# POSIX error numbers, used when Windows did not supply a system code.
_ERRNO: dict[int, tuple[str, tuple[str, ...], str]] = {
    errno.EACCES: _WINDOWS[5],
    errno.EPERM: _WINDOWS[5],
    errno.ENOSPC: _WINDOWS[112],
    errno.EIO: (
        "the disc or the output disk returned an I/O error",
        (
            "A read on the disc failed after retries, usually a dirty or damaged spot.",
            "The output disk disconnected or reported a write error.",
        ),
        "Clean the disc and retry; if the rip was writing to an external disk, check its cable "
        "and free space.",
    ),
    errno.ENOENT: (
        "a file or device that was expected has disappeared",
        (
            "The drive was removed, the disc was ejected, or the output folder was deleted or "
            "renamed while the rip ran.",
            "A staged temporary file was cleaned up by another program.",
        ),
        "Reopen the disc from the source list and save again into a folder that will not move "
        "during the rip.",
    ),
    errno.EROFS: _WINDOWS[19],
}


def _describe(exc: BaseException, detail: str) -> str:
    """The technical line: what the code actually raised."""
    text = str(exc).strip() or type(exc).__name__
    if len(text) > _LIMIT:
        text = text[: _LIMIT - 3] + "..."
    if detail:
        return f"{text} ({detail})"
    return text


def _file_system(exc: OSError, stage: str) -> tuple[str, tuple[str, ...], str]:
    # A Windows system code is authoritative when the OS supplied one; a plain
    # errno is read from its own table first because the two number spaces
    # overlap (EIO is 5, which is ERROR_ACCESS_DENIED to Windows).
    winerror = int(getattr(exc, "winerror", 0) or 0)
    code = winerror or (int(exc.errno) if exc.errno is not None else 0)
    for table in ((_WINDOWS, _ERRNO) if winerror else (_ERRNO, _WINDOWS)):
        if code in table:
            return table[code]
    opening = stage.startswith("open")
    return (
        "the source could not be read or the result could not be written",
        (
            "A read on the disc failed, which usually means a dirty or damaged spot.",
            "The output disk refused a write, which points at free space or a disconnected disk.",
            "The disc was removed while it was being read.",
        )
        if not opening else (
            "The drive could not deliver the disc's first sectors.",
            "The disc is dirty, upside down or not the media this drive expects.",
            "A virtual or network drive was selected instead of an optical drive.",
        ),
        "Clean the disc, reconnect the drive, then reopen the source and retry."
        if not opening else
        "Eject and re-seat the disc, wait for it to spin up, then open the drive again.",
    )


def _aacs(exc: AacsError) -> tuple[str, tuple[str, ...], str]:
    text = str(exc).lower()
    if "keydb" in text or "no usable aacs" in text or "key file" in text:
        detail = (
            "The AACS key file (keydb.cfg) is missing, empty or not readable.",
            "The configured path points at a folder instead of the keydb.cfg file.",
        )
        advice = ("Set Preferences -> AACS/BD+ to the keydb.cfg that MakeMKV and libaacs use, "
                  "then reopen the disc.")
    elif "volume key" in text or "vuk" in text or "unknown" in text:
        detail = (
            "The key file has no volume key for this disc, so its title key cannot be unwrapped.",
            "The disc was mastered with an MKB newer than the installed libaacs understands.",
        )
        advice = ("Add this disc to the key file (playing it once in MakeMKV caches the key on "
                  "this machine), or update libaacs, then reopen the disc.")
    else:
        detail = (
            "The AACS handshake with the drive failed, so the stream stayed encrypted.",
            "The drive does not expose the disc's encrypted bus key to this host.",
        )
        advice = "Reopen the disc; if it repeats, check that the drive is not in a locked or vendor mode."
    return ("the disc's encryption key is not available", detail, advice)


def _bdplus(exc: BdplusError) -> tuple[str, tuple[str, ...], str]:
    return (
        "the disc's BD+ protection could not be handled",
        (
            "The BD+ virtual machine could not run this disc's protection code, usually a "
            "missing or mismatched libbdplus or VM data file.",
            "The disc's BD+ code needs a newer libbdplus than the one installed.",
        ),
        "Check Preferences -> AACS/BD+ shows a libbdplus path, then reopen the disc. AACS-only "
        "decryption still produces a playable file for most titles.",
    )


def _dvdcss(exc: DvdcssError) -> tuple[str, tuple[str, ...], str]:
    return (
        "CSS decryption is not available",
        (
            "No usable libdvdcss library was found for this Python build's bitness.",
            "The configured library is a different architecture or is not a libdvdcss build.",
        ),
        "Install libdvdcss and point Preferences at it, then reopen the disc.",
    )


def _decrypt(exc: BlurayDecryptError) -> tuple[str, tuple[str, ...], str]:
    return (
        "decryption failed part-way through the disc",
        (
            "The key worked for the start of the disc but a later unit failed, which usually "
            "means a damaged sector rather than a wrong key.",
            "The drive returned short or unreadable sectors while the clip was staged.",
        ),
        "Clean the disc and retry. If it fails on a later title only, save the titles that did "
        "succeed and treat the disc's damaged area as a known loss.",
    )


_FFMPEG: tuple[tuple[str, tuple[str, tuple[str, ...], str]], ...] = (
    (
        "empty output",
        (
            "every selected stream was rejected, so ffmpeg had nothing it could write",
            (
                "The staged clip was damaged or still encrypted at the start.",
                "The selected streams are not the ones ffmpeg can read from this container.",
            ),
            "Retry with only the main video track selected; if that also comes out empty, the "
            "clip itself is unreadable.",
        ),
    ),
    (
        "no selected streams",
        (
            "no streams were selected for this title",
            ("Every track was cleared in the title tree before saving.",),
            "Tick at least one audio or video track, then save the title again.",
        ),
    ),
    (
        "no space",
        (
            "the output disk ran out of space",
            ("The remux needed more room than the destination had left.",),
            "Free space on the output disk and retry.",
        ),
    ),
    (
        "permission denied",
        (
            "ffmpeg could not write the output file",
            (
                "The file is open in a player or locked by antivirus.",
                "The output folder is read-only for this user.",
            ),
            "Close anything using the file and retry into a normal folder.",
        ),
    ),
    (
        "invalid data",
        (
            "ffmpeg could not decode the disc's data",
            (
                "The staged clip contains garbage where the drive could not read, which points "
                "at a damaged area of the disc.",
                "The clip is still encrypted, which means decryption did not apply to it.",
            ),
            "Clean the disc and retry; if the same timestamp fails every time, that spot is damaged.",
        ),
    ),
    (
        "not found",
        (
            "ffmpeg or ffprobe could not be started",
            (
                "ffmpeg is not on PATH and no copy was found next to the application.",
                "The configured ffmpeg path no longer exists.",
            ),
            "Install ffmpeg or set the ffmpeg path in Preferences, then retry.",
        ),
    ),
)


def _ffmpeg(exc: FFmpegError) -> tuple[str, tuple[str, ...], str]:
    text = str(exc).lower()
    for needle, rule in _FFMPEG:
        if needle in text:
            return rule
    return (
        "ffmpeg could not produce the output file",
        (
            "ffmpeg rejected the staged clip or its stream layout; its own last error line is "
            "part of the message above.",
            "The clip was unreadable or still encrypted where ffmpeg started reading.",
        ),
        "Look at the ffmpeg line in the message for the offending stream, then retry that title "
        "with only its main video and audio tracks selected.",
    )


def _structure(exc: BaseException) -> tuple[str, tuple[str, ...], str]:
    if isinstance(exc, BdmvError):
        what = "the Blu-ray's playlists or movie objects are inconsistent"
    elif isinstance(exc, DvdError):
        what = "the DVD's navigation data is inconsistent"
    elif isinstance(exc, IsoError):
        what = "the disc's file system could not be read"
    else:
        what = "the disc's file system could not be read"
    return (
        what,
        (
            "The disc's structure is damaged, or uses a layout this build cannot parse.",
            "A partial or interrupted backup or ISO is being opened instead of the disc.",
        ),
        "Open the physical drive rather than an extracted folder, or make a fresh backup with "
        "MakeMKV and open that.",
    )


def explain(exc: BaseException, stage: str = "", detail: str = "") -> Diagnosis:
    """Return the cause and next step for ``exc``.

    ``stage`` names what OpticRip was doing ("saving", "opening the disc") and
    only refines the wording; ``detail`` is appended to the technical line so
    the log says which title or clip failed.
    """
    problem = _describe(exc, detail)
    if isinstance(exc, AacsError):
        headline, causes, advice = _aacs(exc)
    elif isinstance(exc, BdplusError):
        headline, causes, advice = _bdplus(exc)
    elif isinstance(exc, DvdcssError):
        headline, causes, advice = _dvdcss(exc)
    elif isinstance(exc, FFmpegError):
        headline, causes, advice = _ffmpeg(exc)
    elif isinstance(exc, (BdmvError, DvdError, IsoError, ProfileError)):
        if isinstance(exc, ProfileError):
            headline = "the conversion profile could not be read"
            causes = (
                "The profile file is a different format or was edited and is no longer valid.",
                "The profile references an output format this build does not offer.",
            )
            advice = ("Choose the default profile in Preferences, or reselect the profile file, "
                      "then retry.")
        else:
            headline, causes, advice = _structure(exc)
    elif isinstance(exc, (BlurayDecryptError, BackupError)):
        headline, causes, advice = _decrypt(exc)
    elif isinstance(exc, OSError):
        headline, causes, advice = _file_system(exc, stage)
    else:
        headline = "an unexpected internal error"
        causes = (
            "OpticRip hit a case it does not handle, rather than a fault in the disc, the drive "
            "or the output disk.",
        )
        advice = ("Retry once. If it happens again, turn on debug logging in Preferences so the "
                  "full traceback is written to OpticRip_log.txt, and include it with the report.")
    return Diagnosis(headline=headline, problem=problem, causes=tuple(causes), advice=advice)
