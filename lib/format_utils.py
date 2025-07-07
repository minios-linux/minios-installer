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


def format_partitions(primary: str, fs: str, efi: Optional[str]) -> None:
    """
    Format the primary partition (and EFI partition if provided).
    """
    if fs == 'fat32':
        run_command(['mkfs.vfat', primary], _("Failed to format ") + primary + ".")
    elif fs in ('btrfs','ntfs','exfat'):
        run_command([f'mkfs.{fs}','-f',primary], _("Failed to format ") + primary + ".")
    else:
        run_command([f'mkfs.{fs}','-F',primary], _("Failed to format ") + primary + ".")
    if efi:
        run_command(['mkfs.vfat', efi], _("Failed to format EFI ") + efi + ".")


def check_filesystem_support() -> dict:
    """
    Check which filesystem utilities are available on the system.
    Returns a dict of filesystem -> bool indicating availability.
    """
    import shutil
    
    filesystems = {
        'ext4': 'mkfs.ext4',
        'ext2': 'mkfs.ext2',
        'fat32': 'mkfs.vfat', 
        'btrfs': 'mkfs.btrfs',
        'ntfs': 'mkfs.ntfs',
        'exfat': 'mkfs.exfat'
    }
    
    support = {}
    for fs, cmd in filesystems.items():
        support[fs] = shutil.which(cmd) is not None
    
    return support
