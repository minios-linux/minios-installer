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
import gettext
from typing import Optional
from command_utils import run_command

# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def mount_partition(part: str, mount_dir: str) -> None:
    """
    Mount the partition to mount_dir, creating or clearing the directory first.
    """
    if os.path.ismount(mount_dir):
        raise RuntimeError(_("Destination ") + mount_dir + _(" is already mounted."))
    
    if os.path.isdir(mount_dir) and os.listdir(mount_dir):
        shutil.rmtree(mount_dir, ignore_errors=True)
    
    os.makedirs(mount_dir, exist_ok=True)
    
    fs_type = run_command(
        ['blkid', '-o', 'value', '-s', 'TYPE', part],
        _("Could not determine filesystem type of ") + part + "."
    ).strip()
    
    if subprocess.call(['mount', '-t', fs_type, part, mount_dir]) != 0:
        raise RuntimeError(_("Failed to mount ") + part + ".")


def unmount_partitions(p1: str, p2: Optional[str], m1: str, m2: Optional[str]) -> None:
    """
    Unmount all partitions and remove mount directories.
    """
    for mount_point in [m2, m1]:  # Unmount in reverse order
        if mount_point and os.path.ismount(mount_point):
            try:
                subprocess.check_call(['umount', mount_point])
            except subprocess.CalledProcessError:
                # Try lazy unmount if regular unmount fails
                try:
                    subprocess.check_call(['umount', '-l', mount_point])
                except subprocess.CalledProcessError:
                    pass  # Continue even if unmount fails
    
    # Remove mount directories
    for mount_point in [m1, m2]:
        if mount_point and os.path.isdir(mount_point):
            try:
                shutil.rmtree(mount_point, ignore_errors=True)
            except (OSError, IOError):
                pass


def get_mounted_partitions(target_device: str) -> list:
    """
    Get all mounted partitions for a given device.
    Returns a list of tuples (device, mount_point).
    """
    mounted = []
    try:
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    device = parts[0]
                    mount_point = parts[1]
                    if device.startswith(target_device):
                        mounted.append((device, mount_point))
    except (OSError, IOError):
        pass
    return mounted


def force_unmount_device(device: str) -> None:
    """
    Force unmount all partitions of a device.
    """
    mounted = get_mounted_partitions(device)
    for dev, mount_point in mounted:
        try:
            subprocess.run(['umount', '-l', dev], 
                         stderr=subprocess.DEVNULL, check=False)
        except (subprocess.SubprocessError, OSError):
            pass
