#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Mount Utilities
Utilities for mounting and unmounting partitions and disks.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import subprocess
import shutil
import time
import gettext
from typing import Optional
from command_utils import run_command

# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def _map_mount_fstype(fstype: str) -> str:
    fs = (fstype or "").lower()
    if fs in ("fat32", "fat16", "vfat", "efi"):
        return "vfat"
    return fs


def _blkid_type_with_retry(part: str, attempts: int = 10, delay: float = 0.3) -> str:
    """
    Resolve FS type after mkfs. blkid can return empty until udev settles.
    """
    subprocess.run(["udevadm", "settle", "--timeout", "10"], check=False)
    last_err = None
    for i in range(attempts):
        try:
            fs_type = run_command(
                ["blkid", "-o", "value", "-s", "TYPE", part],
                _("Could not determine filesystem type of ") + part + ".",
            ).strip()
            if fs_type:
                return fs_type
        except RuntimeError as exc:
            last_err = exc
        time.sleep(delay)
        if i == attempts // 2:
            subprocess.run(["udevadm", "settle", "--timeout", "5"], check=False)
            # Hint kernel to re-read partition info
            subprocess.run(["blkid", "-p", part], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if last_err:
        raise last_err
    raise RuntimeError(_("Could not determine filesystem type of ") + part + ".")


def mount_partition(part: str, mount_dir: str, fstype: Optional[str] = None) -> None:
    """
    Mount the partition to mount_dir, creating or clearing the directory first.

    If *fstype* is known from the plan (e.g. after format), pass it to avoid
    depending on a post-mkfs blkid race. fat32 is mapped to vfat for mount(8).
    """
    if os.path.ismount(mount_dir):
        raise RuntimeError(_("Destination ") + mount_dir + _(" is already mounted."))

    if os.path.isdir(mount_dir) and os.listdir(mount_dir):
        shutil.rmtree(mount_dir, ignore_errors=True)

    os.makedirs(mount_dir, exist_ok=True)

    if fstype:
        subprocess.run(["udevadm", "settle", "--timeout", "10"], check=False)
        mount_type = _map_mount_fstype(fstype)
    else:
        mount_type = _map_mount_fstype(_blkid_type_with_retry(part))

    if subprocess.call(["mount", "-t", mount_type, part, mount_dir]) != 0:
        # Fallback: let mount(8) auto-detect (still better than hard fail after wipe).
        if subprocess.call(["mount", part, mount_dir]) != 0:
            raise RuntimeError(_("Failed to mount ") + part + ".")


def unmount_partitions(p1: str, p2: Optional[str], m1: str, m2: Optional[str]) -> None:
    """
    Unmount all partitions and remove mount directories.
    """
    for mount_point in [m2, m1]:  # Unmount in reverse order
        if mount_point and os.path.ismount(mount_point):
            unmounted = False
            try:
                subprocess.check_call(["umount", mount_point])
                unmounted = True
            except subprocess.CalledProcessError:
                # Try lazy unmount if regular unmount fails
                try:
                    subprocess.check_call(["umount", "-l", mount_point])
                    unmounted = True
                except subprocess.CalledProcessError:
                    pass
            if not unmounted and os.path.ismount(mount_point):
                raise RuntimeError(_("Failed to unmount target filesystem: {path}").format(path=mount_point))

    for mount_point in [m1, m2]:
        _remove_mount_dir(mount_point)


def unmount_mountpoints(mount_points) -> None:
    """Unmount an ordered target collection in reverse dependency order."""
    for mount_point in reversed(tuple(mount_points)):
        if mount_point and os.path.ismount(mount_point):
            unmounted = False
            try:
                subprocess.check_call(["umount", mount_point])
                unmounted = True
            except subprocess.CalledProcessError:
                try:
                    subprocess.check_call(["umount", "-l", mount_point])
                    unmounted = True
                except subprocess.CalledProcessError:
                    pass
            if not unmounted and os.path.ismount(mount_point):
                raise RuntimeError(_("Failed to unmount target filesystem: {path}").format(path=mount_point))
    for mount_point in mount_points:
        _remove_mount_dir(mount_point)


def _is_device_or_partition_of(device: str, target_device: str) -> bool:
    """
    True if *device* is *target_device* or a partition of it.

    Uses parent_block_device_name so /dev/nvme0n1 does not match /dev/nvme0n10.
    """
    if not device or not target_device:
        return False
    try:
        device = os.path.realpath(device)
        target_device = os.path.realpath(target_device)
    except OSError:
        pass
    if device == target_device:
        return True
    try:
        from disk_utils import parent_block_device_name, normalize_device_path

        device = normalize_device_path(device)
        target_device = normalize_device_path(target_device)
        if device == target_device:
            return True
        dev_base = os.path.basename(device)
        target_base = os.path.basename(target_device)
        # Target itself is a partition: only exact match (handled above).
        if parent_block_device_name(target_base) != target_base:
            return False
        return parent_block_device_name(dev_base) == target_base
    except Exception:
        return False


def get_mounted_partitions(target_device: str) -> list:
    """
    Get all mounted partitions for a given device.
    Returns a list of tuples (device, mount_point).
    """
    mounted = []
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    device = parts[0]
                    mount_point = parts[1]
                    if _is_device_or_partition_of(device, target_device):
                        mounted.append((device, mount_point))
    except (OSError, IOError):
        pass
    return mounted


def _remove_mount_dir(path: Optional[str]) -> None:
    if not path or not os.path.isdir(path):
        return
    if os.path.ismount(path):
        raise RuntimeError(_("Refusing to remove still-mounted target: {path}").format(path=path))
    try:
        shutil.rmtree(path, ignore_errors=True)
    except (OSError, IOError):
        pass


def force_unmount_device(device: str) -> None:
    """
    Force unmount all partitions of a device.
    """
    mounted = get_mounted_partitions(device)
    for dev, mount_point in mounted:
        try:
            subprocess.run(
                ["umount", "-l", mount_point],
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            pass
    remaining = get_mounted_partitions(device)
    if remaining:
        mounts = ", ".join("{} on {}".format(dev, mount) for dev, mount in remaining)
        raise RuntimeError(_("Target device still has mounted filesystems: {mounts}").format(mounts=mounts))
