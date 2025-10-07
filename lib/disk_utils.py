#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Disk Utilities
Utilities for disk detection, sizing, and partitioning operations.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import re
import subprocess
import json
import gettext
import gi
from typing import List, Dict, Optional, Callable
from command_utils import run_command

gi.require_version('UDisks', '2.0')
from gi.repository import UDisks, GLib


# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def format_size_to_gb(size_str: str) -> str:
    """
    Convert size string from lsblk to GB format.
    Examples: "10G" -> "10 GB", "500M" -> "0.5 GB", "2T" -> "2000 GB"
    """
    if not size_str:
        return "? GB"
    
    # Remove any whitespace
    size_str = size_str.strip()
    
    # Extract number and unit
    import re
    match = re.match(r'([0-9.,]+)([KMGTPB]?)', size_str.upper())
    if not match:
        return size_str  # Return original if can't parse
    
    number_str, unit = match.groups()
    try:
        # Replace comma with dot for proper float parsing
        number_str = number_str.replace(',', '.')
        number = float(number_str)
    except ValueError:
        return size_str  # Return original if can't parse number
    
    # Convert to GB
    if unit == 'B' or unit == '':
        gb = number / (1024 ** 3)
    elif unit == 'K':
        gb = number / (1024 ** 2)
    elif unit == 'M':
        gb = number / 1024
    elif unit == 'G':
        gb = number
    elif unit == 'T':
        gb = number * 1024
    elif unit == 'P':
        gb = number * 1024 * 1024
    else:
        return size_str  # Unknown unit
    
    # Format the result
    if gb >= 1000:
        return f"{gb:.0f} GB"
    elif gb >= 100:
        return f"{gb:.0f} GB"
    elif gb >= 10:
        return f"{gb:.1f} GB"
    else:
        return f"{gb:.2f} GB"


def find_available_disks() -> List[Dict]:
    """
    Return a list of available block devices with name, size, model, serial, transport, icon.
    """
    
    output = run_command(
        ['lsblk','-P','-o','NAME,SIZE,MODEL,SERIAL,ROTA,TRAN','-d','-n','-I','3,8,179,259,252'],
        _("Failed to retrieve disk list.")
    )
    devices = []
    # Exclude disk from which live media is loaded (always live mode)
    try:
        # Check both livekit and dracut paths
        if os.path.exists('/run/initramfs/memory/data'):
            src_path = '/run/initramfs/memory/data'
        elif os.path.exists('/lib/live/mount/medium'):
            src_path = '/lib/live/mount/medium'
        
        root_src = run_command(
            ['findmnt', '-n', '-o', 'SOURCE', src_path],
            _("Failed to detect live media device")
        ).strip()
        # Determine the block device name via lsblk PKNAME, fallback to basename stripping digits
        pkname = run_command(
            ['lsblk', '-n', '-o', 'PKNAME', root_src],
            _("Failed to detect root disk")
        ).strip()
        if pkname:
            root_disk = pkname
        else:
            root_disk = re.sub(r'\d+$', '', os.path.basename(root_src))
    except Exception:
        root_disk = ''
    for line in output.splitlines():
        props = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', line)}
        name = props.get('name')
        # skip system boot disk and non-block devices
        if name == root_disk or not name or name.startswith(('loop','nbd')):
            continue

        size   = format_size_to_gb(props.get('size',''))
        model  = props.get('model','')
        serial = props.get('serial','')
        rota   = props.get('rota','0') == '1'
        tran   = props.get('tran','')

        if tran == 'usb':
            icon = 'drive-harddisk-usb'
        elif tran in ('ata', 'sata'):
            icon = 'drive-harddisk'
        elif tran == 'nvme':
            icon = 'drive-harddisk-solidstate'
        elif tran in ('mmc', 'sd'):
            icon = 'drive-removable-media'
        elif rota:
            icon = 'drive-harddisk'
        else:
            icon = 'drive-removable-media'

        devices.append({
            'name': name,
            'size': size,
            'model': model,
            'serial': serial,
            'transport': tran or ('rotational' if rota else 'non‑rotational'),
            'icon': icon
        })
    return devices


def _is_removable_disk(device_path: str) -> bool:
    """
    Check if a disk is removable by examining sysfs.
    """
    try:
        device_name = os.path.basename(device_path)
        removable_path = f"/sys/block/{device_name}/removable"
        if os.path.exists(removable_path):
            with open(removable_path, 'r') as f:
                return f.read().strip() == '1'
    except:
        pass
    return False


def get_disk_size_mib(device: str) -> int:
    """
    Return disk size in MiB (integer) by parsing parted output.
    """
    
    output = run_command(
        ['parted','-s',device,'unit','MiB','print'],
        _("Failed to get disk size.")
    )
    for line in output.splitlines():
        if device in line:
            m = re.search(r'(\d+(?:\.\d+)?)MiB', line)
            if m:
                return int(float(m.group(1)))
    raise RuntimeError(_("Could not parse disk size."))


def partition_disk(device: str, fs: str, use_gpt: bool) -> None:
    """
    Partition the disk; if fs is not 'fat32', create an EFI partition.
    """
    
    efi = (fs != 'fat32')
    
    if efi and use_gpt:
        run_command(['parted', '-s', device, 'mklabel', 'gpt'], 
                   _("Failed to set GPT label on ") + device + ".")
        size = get_disk_size_mib(device) - 100
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', fs, '1MiB', f'{size}MiB'],
            _("Failed to create primary partition on ") + device + "."
        )
        run_command(
            ['parted', '-s', device, 'mkpart', 'ESP', 'fat32', f'{size}MiB', '100%'],
            _("Failed to create EFI partition on ") + device + "."
        )
        run_command(['parted', '-s', device, 'set', '2', 'boot', 'on'], 
                   _("Failed to set boot flag."))
    elif efi:
        run_command(['parted', '-s', device, 'mklabel', 'msdos'], 
                   _("Failed to set MSDOS label on ") + device + ".")
        size = get_disk_size_mib(device) - 100
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', fs, '1MiB', f'{size}MiB'],
            _("Failed to create primary partition on ") + device + "."
        )
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', 'fat32', f'{size}MiB', '100%'],
            _("Failed to create second partition on ") + device + "."
        )
        run_command(['parted', '-s', device, 'set', '1', 'boot', 'on'], 
                   _("Failed to set boot flag."))
    else:
        run_command(['parted', '-s', device, 'mklabel', 'msdos'], 
                   _("Failed to set MSDOS label on ") + device + ".")
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', fs, '1MiB', '100%'],
            _("Failed to create primary partition on ") + device + "."
        )


def zero_fill_disk(device: str) -> None:
    """
    Overwrite the beginning of the disk with zeros (2MB).
    """
    try:
        subprocess.check_call(
            ['dd', 'if=/dev/zero', f'of={device}', 'bs=1M', 'count=2', 'status=none'],
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(_("Failed to erase ") + device + ".")


class DiskMonitor:
    """
    Monitor disk changes using UDisks2 and notify callbacks when changes occur.
    """
    
    def __init__(self):
        self.udisks_client = None
        self.listener_id = None
        self.callbacks = []
        
    def start_monitoring(self, callback: Callable[[], None]) -> None:
        """
        Start monitoring disk changes and call the callback when changes occur.
        """
        if callback not in self.callbacks:
            self.callbacks.append(callback)
            
        if self.udisks_client is None:
            self.udisks_client = UDisks.Client.new_sync()
            self.listener_id = self.udisks_client.connect("changed", self._on_disks_changed)
    
    def stop_monitoring(self, callback: Callable[[], None] = None) -> None:
        """
        Stop monitoring disk changes. If callback is provided, remove only that callback.
        If no callback provided, remove all callbacks and stop monitoring.
        """
        if callback and callback in self.callbacks:
            self.callbacks.remove(callback)
        elif callback is None:
            self.callbacks.clear()
            
        if not self.callbacks and self.udisks_client and self.listener_id:
            self.udisks_client.disconnect(self.listener_id)
            self.udisks_client = None
            self.listener_id = None
    
    def pause_monitoring(self) -> None:
        """
        Temporarily pause monitoring (useful during disk operations).
        """
        if self.udisks_client and self.listener_id:
            self.udisks_client.handler_block(self.listener_id)
    
    def resume_monitoring(self) -> None:
        """
        Resume monitoring after pause.
        """
        if self.udisks_client and self.listener_id:
            self.udisks_client.handler_unblock(self.listener_id)
    
    def _on_disks_changed(self, client, *args) -> None:
        """
        Internal callback when UDisks detects changes.
        """
        for callback in self.callbacks:
            GLib.idle_add(callback)


# Global disk monitor instance
_disk_monitor = DiskMonitor()


def start_disk_monitoring(callback: Callable[[], None]) -> None:
    """
    Start monitoring disk changes globally.
    """
    _disk_monitor.start_monitoring(callback)


def stop_disk_monitoring(callback: Callable[[], None] = None) -> None:
    """
    Stop monitoring disk changes globally.
    """
    _disk_monitor.stop_monitoring(callback)


def pause_disk_monitoring() -> None:
    """
    Pause disk monitoring globally.
    """
    _disk_monitor.pause_monitoring()


def resume_disk_monitoring() -> None:
    """
    Resume disk monitoring globally.
    """
    _disk_monitor.resume_monitoring()
