"""Linux optical drive enumeration via sysfs, /proc mounts and udev (optional).

Design notes
------------
* ``/sys/block/*/device/type == 5`` is the SCSI peripheral-device type for a
  CD/DVD reader/writer.  This catches SATA and USB drives alike, including
  USB-SATA bridges.
* Media presence, capacity and format come from :class:`LinuxMediaProbe`
  (2048-byte sector reads) rather than from udev, so the provider works in
  minimal containers where udev is absent.
* ``lsblk``/``blkid`` are used only as a *fallback* for labels when a plain
  sector read is not enough.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Iterable

from ..core.interfaces import DriveProvider
from ..core.logging_bus import LogBus
from ..core.models import Drive, DriveKind, DriveState, MediaType
from ..disc.identify import LinuxMediaProbe, MediaProbe, MediaProbeResult

#: SCSI peripheral device type for CD/DVD
_CDROM_SCSI_TYPE = 5

_WHITESPACE = re.compile(r"\s+")


@dataclass
class SysfsDrive:
    """Raw facts about one optical device, read from sysfs."""

    name: str
    device_node: str
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    bus: str = "sata"
    removable: bool = True
    size_512b_sectors: int | None = None
    fields: dict[str, str] = field(default_factory=dict)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _read_int(path: str) -> int | None:
    raw = _read_text(path)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def scan_sysfs(sysfs_root: str = "/sys", dev_root: str = "/dev") -> list[SysfsDrive]:
    """Find every CD/DVD device under ``sysfs_root`` (pure I/O, no probing)."""
    block_root = os.path.join(sysfs_root, "block")
    found: list[SysfsDrive] = []
    try:
        names = sorted(os.listdir(block_root))
    except OSError:
        return []

    for name in names:
        base = os.path.join(block_root, name)
        device = os.path.join(base, "device")
        if not os.path.isdir(device):
            continue
        scsi_type = _read_int(os.path.join(device, "type"))
        is_cdrom = scsi_type == _CDROM_SCSI_TYPE or name.startswith("sr")
        if not is_cdrom:
            continue
        real_device = os.path.realpath(device)
        bus = "usb" if "/usb" in real_device else "sata"
        if "virtual" in real_device or real_device.startswith("/sys/devices/virtual"):
            bus = "virtual"
        found.append(SysfsDrive(
            name=name,
            device_node=os.path.join(dev_root, name),
            vendor=_read_text(os.path.join(device, "vendor")),
            model=_read_text(os.path.join(device, "model")),
            firmware=_read_text(os.path.join(device, "firmware_rev")),
            bus=bus,
            removable=_read_int(os.path.join(base, "removable")) == 1,
            size_512b_sectors=_read_int(os.path.join(base, "size")),
            fields={"scsi_type": str(scsi_type or "")},
        ))
    return found


def mount_points_for(device: str, mountinfo: str = "/proc/self/mountinfo") -> list[str]:
    """Mount points currently backing ``device`` (may be several)."""
    points: list[str] = []
    try:
        with open(mountinfo, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if " - " not in line or len(parts) < 10:
                    continue
                sep = parts.index("-")
                source = parts[sep + 2] if len(parts) > sep + 2 else ""
                if source == device:
                    points.append(_unescape_mount(parts[4]))
    except OSError:
        return []
    return points


def _unescape_mount(text: str) -> str:
    return (text.replace("\\040", " ").replace("\\011", "\t")
                .replace("\\012", "\n").replace("\\134", "\\"))


class LinuxDriveProvider(DriveProvider):
    """Enumerates real optical drives on Linux."""

    name = "linux-sysfs"

    def __init__(self, *, sysfs_root: str = "/sys", dev_root: str = "/dev",
                 mountinfo: str = "/proc/self/mountinfo",
                 probe: MediaProbe | None = None,
                 log: LogBus | None = None) -> None:
        self.sysfs_root = sysfs_root
        self.dev_root = dev_root
        self.mountinfo = mountinfo
        self.probe = probe or LinuxMediaProbe()
        self._log = log or LogBus()

    # -- public --------------------------------------------------------- #
    def list_drives(self) -> list[Drive]:
        return [self._build(sysfs_drive) for sysfs_drive in
                scan_sysfs(self.sysfs_root, self.dev_root)]

    def list_device_nodes(self) -> list[str]:
        return [d.device_node for d in scan_sysfs(self.sysfs_root, self.dev_root)]

    # -- internals ------------------------------------------------------ #
    def _build(self, raw: SysfsDrive) -> Drive:
        drive = Drive(
            path=raw.device_node,
            kind=DriveKind.RAW,
            id=raw.device_node,
            vendor=raw.vendor,
            model=raw.model,
            firmware=raw.firmware,
            bus=raw.bus,
            sector_size=2048,
            mount_points=mount_points_for(raw.device_node, self.mountinfo),
            extras={"scsi_type": raw.fields.get("scsi_type", "")},
        )
        self._apply_probe(drive, self.probe.probe(raw.device_node))
        if drive.state is DriveState.NO_DISC and raw.size_512b_sectors:
            drive.status_detail = "Drive present, waiting for a disc."
        return drive

    def _apply_probe(self, drive: Drive, result: MediaProbeResult) -> None:
        drive.extras["probe_error"] = result.error or ""
        if result.error and not result.has_media:
            # ENOENT / no medium are normal states rather than failures
            if "does not exist" in result.error:
                drive.state = DriveState.INACCESSIBLE
            else:
                drive.state = DriveState.NO_DISC
            drive.status_detail = result.error
            drive.extras["probe_hint"] = result.error_hint or ""
            return
        if not result.has_media:
            drive.state = DriveState.NO_DISC
            drive.status_detail = "No disc detected."
            return

        drive.state = DriveState.READY
        drive.media_type = result.media_type
        drive.volume_label = result.volume_label
        drive.size_bytes = result.capacity_bytes
        if result.capacity_bytes and result.capacity_bytes > 8_500_000_000:
            drive.extras["dual_layer"] = "true"
        drive.extras["filesystem"] = result.filesystem
        for note in result.protection:
            drive.extras.setdefault("protection", "")
            drive.extras["protection"] = ",".join(
                filter(None, [drive.extras.get("protection", ""), note]))


# --------------------------------------------------------------------------- #
# Fallback helpers (only used when a sector read cannot give us a label)
# --------------------------------------------------------------------------- #


def blkid_label(device: str) -> str | None:
    """Ask ``blkid`` for the volume label; returns ``None`` on any failure."""
    try:
        out = subprocess.run(["blkid", "-o", "value", "-s", "LABEL", device],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    label = out.stdout.strip()
    return label or None


def lsblk_inventory() -> list[dict[str, str]]:
    """``lsblk -J`` parsing used by the diagnostics panel, never required."""
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "NAME,TYPE,SIZE,MODEL,TRAN"],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    import json
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    result: list[dict[str, str]] = []
    for entry in data.get("blockdevices", []):
        if str(entry.get("type", "")) == "rom":
            result.append({k: str(entry.get(k, "") or "") for k in
                           ("name", "size", "model", "tran")})
    return result
