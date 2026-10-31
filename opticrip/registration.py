"""Local, offline registration keys.

The reference validates keys locally (R-DLG-02) and can extract one from a
pasted email body or a dropped file (R-DLG-03).  The clone cannot know the
vendor's key format, so it defines its own transparent format:

``ORIP-XXXX-XXXX-XXXX-CCCC``

* the first three groups are free-form upper-case alphanumerics;
* ``CCCC`` is derived from the first three groups, so a mistyped key fails
  without any network access.

Anything else reports ``Invalid`` exactly like the reference.
"""

from __future__ import annotations

import hashlib
import re

KEY_RE = re.compile(r"\bORIP(?:-[A-Z0-9]{4}){3}-[A-Z0-9]{4}\b")
PREFIX = "ORIP"


def checksum(groups: list[str]) -> str:
    digest = hashlib.sha256("".join(groups).encode("ascii")).hexdigest().upper()
    return digest[:4]


def make_key(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest().upper()
    groups = [digest[i * 4:(i + 1) * 4] for i in range(3)]
    return f"{PREFIX}-{'-'.join(groups)}-{checksum(groups)}"


def extract(text: str) -> str:
    """Find a key inside arbitrary text (email body, file contents)."""
    match = KEY_RE.search(text.upper())
    return match.group(0) if match else ""


def validate(text: str) -> tuple[bool, str]:
    """Return ``(valid, key)``; accepts the key embedded in any text."""
    key = extract(text)
    if not key:
        return False, ""
    groups = key.split("-")
    if len(groups) != 5:
        return False, key
    return checksum(groups[1:4]) == groups[4], key


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def read_key_file(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            blob = handle.read(1 << 20)
        return blob.decode("utf-8", "replace")
    except OSError:
        return ""
