#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import json
import os
import subprocess
import re
from typing import Dict, List

from partition_models import DiskLayout, FreeExtent, PartitionInfo


gettext.bindtextdomain("minios-installer", "/usr/share/locale")
gettext.textdomain("minios-installer")
_ = gettext.gettext


def is_uefi_system() -> bool:
    return os.path.isdir("/sys/firmware/efi")


def _run_lsblk(device: str) -> Dict:
    # Progressive column fallbacks for older util-linux:
    # full → drop START/PTTYPE → drop PARTFLAGS → minimal.
    column_sets = [
        "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL,SERIAL,TRAN,PARTFLAGS,PARTTYPE,PTTYPE,START,LOG-SEC,PARTN",
        "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL,SERIAL,TRAN,PARTFLAGS,PARTTYPE,PTTYPE,START",
        "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL,SERIAL,TRAN,PARTFLAGS",
        "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,MODEL,SERIAL,TRAN",
        "NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT",
    ]

    def _call(cols: str) -> str:
        return subprocess.check_output(
            ["lsblk", "-J", "-b", "-o", cols, device],
            universal_newlines=True,
            stderr=subprocess.STDOUT,
        )

    last_exc = None
    output = None
    for cols in column_sets:
        try:
            output = _call(cols)
            break
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            msg = (exc.output or "").lower()
            if "unknown column" in msg or "invalid column" in msg:
                continue
            raise
    if output is None:
        if last_exc:
            raise last_exc
        raise RuntimeError(_("Failed to probe device: {device}").format(device=device))

    data = json.loads(output)
    devices = data.get("blockdevices", [])
    if not devices:
        raise RuntimeError(_("Device not found: {device}").format(device=device))
    return devices[0]


def _role_for(fstype: str, label: str, mountpoint: str, flags: List[str], parttype: str = "") -> str:
    fs = (fstype or "").lower()
    lbl = (label or "").lower()
    mp = (mountpoint or "").lower()
    joined_flags = " ".join(flags).lower()
    efi_guid = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    if fs == "vfat" and ("esp" in joined_flags or "efi" in lbl or mp == "/boot/efi" or (parttype or "").lower() in (efi_guid, "0xef", "ef")):
        return "esp"
    if fs in ("ntfs", "exfat") or "windows" in lbl:
        return "windows"
    if fs == "swap":
        return "swap"
    if fs in ("ext2", "ext3", "ext4", "btrfs", "xfs"):
        return "linux_other"
    return "unknown"


def _part_path(name: str) -> str:
    return name if name.startswith("/dev/") else f"/dev/{name}"


def _sysfs_start_sectors(name: str):
    """Read start offset in sectors from sysfs. Returns None on any error."""
    try:
        base = os.path.basename(name)
        with open(f"/sys/class/block/{base}/start", "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _sysfs_sector_size(name: str):
    try:
        base = os.path.basename(name)
        with open(f"/sys/class/block/{base}/queue/logical_block_size", "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _ceil_mib(byte_count: int) -> int:
    mib = 1024 * 1024
    return (byte_count + mib - 1) // mib


def _free_extents(size_mib: int, partitions: List[PartitionInfo]) -> List[FreeExtent]:
    extents: List[FreeExtent] = []
    cursor = 1
    for part in sorted(partitions, key=lambda p: p.start_mib):
        if part.start_mib > cursor:
            extents.append(FreeExtent(cursor, part.start_mib))
        cursor = max(cursor, part.end_mib)
    if size_mib > cursor:
        extents.append(FreeExtent(cursor, size_mib - 1))
    return [extent for extent in extents if extent.size_mib >= 1]


def scan_disk(device: str) -> DiskLayout:
    if not device.startswith("/dev/"):
        device = f"/dev/{device}"
    disk = _run_lsblk(device)
    size_mib = max(1, int(disk.get("size") or 0) // (1024 * 1024))
    probed_sector_size = disk.get("log-sec") or _sysfs_sector_size(disk.get("name") or device)
    sector_size = int(probed_sector_size or 512)
    disk_size_bytes = int(disk.get("size") or 0)
    pttype = (disk.get("pttype") or "").lower()
    if not pttype:
        # Fallback for older lsblk that did not include PTTYPE in -J output.
        try:
            pttype = subprocess.check_output(
                ["lsblk", "-n", "-o", "PTTYPE", device],
                universal_newlines=True,
                stderr=subprocess.DEVNULL,
            ).strip().lower()
        except Exception:
            pass
    partitions: List[PartitionInfo] = []
    # Alongside resize is enabled only for the consistently tested 512-byte
    # sector convention across the supported util-linux versions.
    geometry_complete = probed_sector_size is not None and sector_size == 512

    for child in disk.get("children") or []:
        if child.get("type") != "part":
            continue
        start_value = child.get("start")
        start_sectors = int(start_value) if start_value not in (None, "") else None
        if start_sectors is None:
            # Older lsblk without START column: read from sysfs (sectors).
            start_sectors = _sysfs_start_sectors(child.get("name") or "")
        if start_sectors is None:
            geometry_complete = False
            start_sectors = 0
        start_bytes = start_sectors * 512
        size_bytes = int(child.get("size") or 0)
        size_sectors = size_bytes // sector_size if size_bytes % sector_size == 0 else 0
        if not size_sectors:
            geometry_complete = False
        start_mib = start_bytes // (1024 * 1024)
        part_size_mib = max(1, _ceil_mib(size_bytes))
        flags = [flag for flag in (child.get("partflags") or "").replace(",", " ").split() if flag]
        fstype = child.get("fstype") or ""
        label = child.get("label") or ""
        mountpoint = child.get("mountpoint") or ""
        role = _role_for(fstype, label, mountpoint, flags, child.get("parttype") or "")
        part_path = _part_path(child.get("name") or "")
        partuuid = ""
        try:
            partuuid = subprocess.check_output(["blkid", "-s", "PARTUUID", "-o", "value", part_path], universal_newlines=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            pass
        partitions.append(
            PartitionInfo(
                name=child.get("name") or "",
                path=part_path,
                size_mib=part_size_mib,
                start_mib=start_mib,
                end_mib=_ceil_mib(start_bytes + size_bytes),
                fstype=fstype,
                label=label,
                mountpoint=mountpoint,
                role=role,
                flags=flags,
                start_sector=start_sectors,
                size_sectors=size_sectors,
                partition_number=int(child.get("partn") or (re.search(r"(\d+)$", child.get("name") or "").group(1) if re.search(r"(\d+)$", child.get("name") or "") else 0)),
                has_children=bool(child.get("children")),
                parttype=(child.get("parttype") or "").lower(),
                partuuid=partuuid,
            )
        )

    return DiskLayout(
        device=device,
        size_mib=size_mib,
        model=(disk.get("model") or "").strip(),
        serial=(disk.get("serial") or "").strip(),
        transport=(disk.get("tran") or "").strip(),
        partition_table=pttype,
        partitions=partitions,
        free_extents=_free_extents(size_mib, partitions) if geometry_complete else [],
        has_efi=any(p.role == "esp" for p in partitions),
        has_windows=any(p.role == "windows" for p in partitions),
        has_linux=any(p.role == "linux_other" for p in partitions),
        logical_sector_size=sector_size,
        size_sectors=disk_size_bytes // sector_size if disk_size_bytes % sector_size == 0 else 0,
        geometry_complete=geometry_complete,
        has_nested_layout=any(p.has_children for p in partitions),
        has_mapped_layout=(disk.get("type") or "disk") != "disk",
    )
