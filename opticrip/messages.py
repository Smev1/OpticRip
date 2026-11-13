"""Numeric message catalogue.

The reference product emits messages as ``MSG:<id>,<flags>,<count>,...`` and
advises consumers to switch on the id, never on the rendered text
(CLONE_SPEC.md R-ROB-02).  Ids and flag values below are the ones that were
observed in the reference build; ids added for clone-only behaviour are marked
``LOCAL`` and deliberately live outside the verified ranges so they cannot be
confused with reference ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# UiMsg flags (documented constants, confirmed for 128/260/131072/16777216).
DEBUG = 32
HIDDEN = 64
EVENT = 128
HAVE_URL = 131072
BOXOK = 260
BOXERROR = 516
BOXWARNING = 1028
BOXYESNO = 776
BOXYESNO_ERR = 1288
ERROR = 16777216


@dataclass(frozen=True)
class MessageDef:
    ident: int
    template: str
    flags: int = 0


_CATALOGUE: dict[int, MessageDef] = {
    # --- verified in the reference build -----------------------------------
    1005: MessageDef(1005, "%1 started"),
    1011: MessageDef(1011, "Using LibreDrive mode (v%1)"),
    2003: MessageDef(2003, "Error '%1' occurred while reading '%2' at offset '%3'", ERROR),
    2024: MessageDef(2024, "Unknown device - '%1'"),
    3006: MessageDef(3006, "Opening files on harddrive at %1"),
    3007: MessageDef(3007, "Using direct disc access mode"),
    3010: MessageDef(3010, "Can't locate a cell for VTS %1 TTN %2 PGCN %3 PGN %4", ERROR),
    3025: MessageDef(
        3025,
        "Title #%1 has length of %2 seconds which is less than minimum title "
        "length of %3 seconds and was therefore skipped",
    ),
    3028: MessageDef(3028, "Title #%1 was added (%2 cell(s), %3)"),
    3038: MessageDef(3038, "Cells %1-%2 were removed from title end"),
    3305: MessageDef(3305, "AACS directory not present, assuming unencrypted disc"),
    5005: MessageDef(5005, "%1 titles saved", EVENT),
    5006: MessageDef(5006, "File %1 does not exist", ERROR),
    5010: MessageDef(5010, "Failed to open disc"),
    5011: MessageDef(5011, "Operation successfully completed"),
    5014: MessageDef(5014, "Saving %1 titles into directory %2", HAVE_URL),
    5036: MessageDef(5036, "Copy complete. %1 titles saved.", BOXOK),
    5042: MessageDef(5042, "The program can't find any usable optical drives."),
    # --- clone-only ids (LOCAL) -------------------------------------------
    9001: MessageDef(9001, "Opening %1"),
    9002: MessageDef(9002, "Opened %1 in %2"),
    9003: MessageDef(9003, "Closing disc"),
    9004: MessageDef(9004, "Saving title #%1 to %2"),
    9005: MessageDef(9005, "Title #%1 saved as %2"),
    9006: MessageDef(9006, "Saving failed: %1", ERROR),
    9007: MessageDef(9007, "Backing up disc to %1"),
    9008: MessageDef(9008, "Backup complete. %1 file(s) copied.", EVENT),
    9009: MessageDef(9009, "Backup failed: %1", ERROR),
    9010: MessageDef(9010, "Cancelled by user"),
    9011: MessageDef(9011, "Registration key stored."),
    9012: MessageDef(9012, "No optical drive found."),
    9013: MessageDef(9013, "This source is encrypted (%1); a licensed decryption component is required"),
    9014: MessageDef(9014, "Title #%1 is not part of this disc"),
    9015: MessageDef(9015, "Output folder does not exist: %1", ERROR),
    9016: MessageDef(9016, "Using profile '%1' from file '%2'"),
    9017: MessageDef(9017, "Reading disc structure, please wait"),
    9018: MessageDef(9018, "Disc is not open"),
    9019: MessageDef(9019, "ffmpeg failed with exit code %1", ERROR),
    9020: MessageDef(9020, "Media is copy protected; only unencrypted media can be ripped"),
    9021: MessageDef(9021, "AACS/BD+ metadata present; assuming encrypted disc"),
    9022: MessageDef(9022, "Disc volume key is not available in this build"),
    9023: MessageDef(9023, "Opened MKV file %1"),
    9024: MessageDef(9024, "File system not recognized, disc can't be opened"),
    9025: MessageDef(9025, "Dvd files are corrupt"),
    9026: MessageDef(9026, "No titles were selected"),
    9027: MessageDef(9027, "Skipping title #%1: no selected streams"),
    9028: MessageDef(9028, "Writing chapters for %1 chapter(s)"),
    9029: MessageDef(9029, "Removing incomplete output %1"),
    9030: MessageDef(9030, "Enumerating drives"),
    # Strings catalogued by the reference but with unknown ids.
    9100: MessageDef(9100, "The volume key is unknown for this disc - video can't be decrypted", ERROR),
    9101: MessageDef(9101, "AACS-protected discs that use SKB are not supported", ERROR),
    9102: MessageDef(9102, "Fatal error occurred, program will now exit.", BOXERROR),
    9103: MessageDef(9103, "Failed to save settings", ERROR),
    9104: MessageDef(9104, "Some settings will apply next time you start the application."),
    9105: MessageDef(9105, "Key not found or invalid", ERROR),
    9106: MessageDef(9106, "Failed to load conversion profile from file '%1', using default", ERROR),
    9107: MessageDef(9107, "Profile parsing error: %1", ERROR),
    9108: MessageDef(9108, "Failed to open disc"),
    9109: MessageDef(9109, "Backup source must start with \"disc:\"", ERROR),
    9110: MessageDef(9110, "Invalid command: %1"),
    9111: MessageDef(9111, "Drive not specified", ERROR),
    9119: MessageDef(9119, "CSS-encrypted ISO images are not supported on Windows; insert the disc and open its drive instead"),
    9120: MessageDef(9120, "CSS decryption is unavailable. %1"),
    9121: MessageDef(9121, "Using libdvdcss library %1"),
    9122: MessageDef(9122, "libdvdcss could not open %1: %2"),
    9123: MessageDef(9123, "The selected file is not a usable libdvdcss library: %1", ERROR),
    9125: MessageDef(9125, "libdvdcss library installed at %1"),
    9126: MessageDef(9126, "Custom libdvdcss library cleared; automatic detection will be used"),
    9130: MessageDef(9130, "AACS/BD+ decryption is unavailable. %1", ERROR),
    9131: MessageDef(9131, "Using libaacs library %1"),
    9133: MessageDef(9133, "The selected file is not a usable libaacs library: %1", ERROR),
    9134: MessageDef(9134, "libaacs library installed at %1"),
    9135: MessageDef(9135, "Using libbdplus library %1"),
    9137: MessageDef(9137, "The selected file is not a usable libbdplus library: %1", ERROR),
    9138: MessageDef(9138, "libbdplus library installed at %1"),
    9139: MessageDef(9139, "AACS key file set to %1"),
    9140: MessageDef(9140, "Custom AACS/BD+ settings cleared; automatic detection will be used"),
    9141: MessageDef(9141, "BD+ code does not apply to clip %1; decrypting AACS only"),
    9142: MessageDef(9142, "Processing BD+ code"),
    9143: MessageDef(
        9143,
        "UHD Blu-ray (AACS %1) is not supported: it needs AACS 2.x content "
        "keys plus a bus-encryption-capable drive and host certificate",
        ERROR,
    ),
}


def definition(ident: int) -> MessageDef:
    """Return the catalogue entry, inventing a safe placeholder for unknown ids."""
    try:
        return _CATALOGUE[ident]
    except KeyError:
        return MessageDef(ident, f"(unknown message {ident}: %1)", 0)


def template(ident: int) -> str:
    return definition(ident).template


def flags(ident: int) -> int:
    return definition(ident).flags


_PLACEHOLDER = re.compile(r"%([1-9])")


def render(ident: int, *params: object) -> str:
    """Render a template.

    Only positional ``%1``..``%9`` placeholders exist in the reference
    catalogue.  Rendering is textual, so callers may pass ints, floats or
    strings; a placeholder inside a value is *not* re-expanded because the
    template is scanned once and values are inserted verbatim.
    """
    def substitute(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index > len(params):
            return match.group(0)
        return str(params[index - 1])

    return _PLACEHOLDER.sub(substitute, definition(ident).template)


def has_placeholders(text: str) -> bool:
    return any(f"%{i}" in text for i in range(1, 10))
