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
from typing import List, Dict, Optional
from command_utils import run_command


# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


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
        # Always use initramfs media mount in live mode
        src_path = '/run/initramfs/memory/data'
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

        size   = props.get('size','')
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
    Overwrite the beginning of the disk with zeros.
    """
    try:
        subprocess.check_call(
            ['dd', 'if=/dev/zero', f'of={device}', 'bs=4096', 'count=273', 'status=none'],
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(_("Failed to erase ") + device + ".")
