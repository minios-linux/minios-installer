#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Format Utilities
Utilities for formatting partitions with different filesystems.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import gettext
import shutil
from typing import Optional, List
from command_utils import run_command

# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext

# Regular native installs need Linux root filesystems. FAT32/NTFS remain valid
# for live-layout installs, but are not suitable as a native Linux root here.
NATIVE_FILESYSTEMS = ('ext4', 'ext2', 'btrfs')


def detect_filesystem_tools() -> List[str]:
    """
    Detect which mkfs.* tools are available and return filesystem types.
    """
    fss = []
    if shutil.which('mkfs.ext4'):
        fss.append('ext4')
    if shutil.which('mkfs.ext2'):
        fss.append('ext2')
    if shutil.which('mkfs.btrfs'):
        fss.append('btrfs')
    if shutil.which('mkfs.vfat'):
        fss.append('fat32')
    if shutil.which('mkfs.ntfs'):
        fss.append('ntfs')
    return fss


def filesystems_for_boot_mode(available: Optional[List[str]] = None, uefi: Optional[bool] = None, install_mode: str = "live") -> List[str]:
    """
    Restrict selectable filesystems by firmware/boot mode.

    Live-layout installs preserve the legacy list. Native installs are limited
    to Linux root filesystems supported by the current native boot path.
    """
    if available is None:
        available = detect_filesystem_tools()
    if uefi is None:
        try:
            from partition_scanner import is_uefi_system
            uefi = is_uefi_system()
        except Exception:
            uefi = False
    if install_mode != "native":
        return list(available)
    return [fs for fs in available if fs in NATIVE_FILESYSTEMS]


def validate_filesystem_for_plan(filesystem: str, use_gpt: bool, install_mode: str = "live") -> None:
    """Raise ValueError if *filesystem* is not bootable for the planned layout."""
    if install_mode != "native":
        return
    if filesystem not in NATIVE_FILESYSTEMS:
        raise ValueError(
            _("Filesystem '{fs}' is not supported for native installs. "
              "Choose ext4, ext2, or btrfs.").format(fs=filesystem)
        )


def format_partitions(primary: str, fs: str, efi: Optional[str]) -> None:
    """
    Format the primary partition (and EFI partition if provided).

    Always force FAT32 (-F 32): 100 MiB ESPs otherwise default to FAT16 under
    dosfstools, which many UEFI firmwares reject.
    """
    if fs == 'fat32':
        run_command(
            ['mkfs.vfat', '-F', '32', primary],
            _("Failed to format ") + primary + ".",
        )
    elif fs in ('btrfs', 'ntfs'):
        run_command([f'mkfs.{fs}', '-f', primary], _("Failed to format ") + primary + ".")
    else:
        run_command([f'mkfs.{fs}', '-F', primary], _("Failed to format ") + primary + ".")
    if efi:
        run_command(
            ['mkfs.vfat', '-F', '32', efi],
            _("Failed to format EFI ") + efi + ".",
        )


def check_filesystem_support() -> dict:
    """
    Check which filesystem utilities are available on the system.
    Returns a dict of filesystem -> bool indicating availability.
    """
    filesystems = {
        'ext4': 'mkfs.ext4',
        'ext2': 'mkfs.ext2',
        'fat32': 'mkfs.vfat',
        'btrfs': 'mkfs.btrfs',
        'ntfs': 'mkfs.ntfs',
    }

    support = {}
    for fs, cmd in filesystems.items():
        support[fs] = shutil.which(cmd) is not None

    return support
