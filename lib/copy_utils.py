#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Copy Utilities
Utilities for copying MiniOS files and EFI files.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import shutil
import gettext
from typing import Optional, Callable

# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def copy_minios_files(src: str, dst: str, progress_cb: Callable, log_cb: Callable, 
                     config_override: Optional[str] = None) -> None:
    """
    Copy MiniOS files from src to dst with progress reporting.
    """
    # Calculate total size for progress reporting
    total = _calculate_copy_size(src)
    copied = 0
    
    # Get reference to the owner object for cancellation checking
    owner = getattr(progress_cb, "__self__", None)
    
    entries = []
    
    # 1) Main tree → minios/
    for root, dirs, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), src)
            if rel.startswith('changes/'):
                continue
            entries.append((os.path.join('minios', rel), os.path.join(root, fn)))
    
    # 2) .disk/info
    with open('/tmp/info', 'w', encoding='utf-8') as f:
        f.write('MiniOS')
    entries.append(('.disk/info', '/tmp/info'))
    
    # 3) config.conf
    config_dst = 'minios/config.conf'
    if config_override and os.path.exists(config_override):
        entries.append((config_dst, config_override))
    else:
        config_src = '/etc/live/config.conf'
        if os.path.exists(config_src):
            entries.append((config_dst, config_src))
    
    for rel, path in entries:
        if owner and owner.cancel_requested:
            log_cb(_("Installation canceled by user."))
            raise RuntimeError(_("Installation canceled by user."))
        
        dest = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        
        # Report copying start, then perform copy, then log completion
        size = os.path.getsize(path)
        # Calculate progress including this file
        percent = int(18 + (78 * (copied + size) / total))
        progress_cb(percent, _("Copying MiniOS files: ") + rel)
        shutil.copy2(path, dest)
        log_cb(_("Copied file: ") + path)
        copied += size
    
    # Create required directories
    for sub in ('boot', 'modules', 'changes', 'scripts'):
        p = os.path.join(dst, 'minios', sub)
        os.makedirs(p, exist_ok=True)
        log_cb(_("Created directory: ") + p)


def copy_efi_files(src: str, dst: str, log_cb: Callable) -> None:
    """
    Copy EFI files from src/boot/EFI → dst/EFI/...
    """
    efi_dir = os.path.join(src, 'boot', 'EFI')
    if not os.path.isdir(efi_dir):
        return
    
    for root, dirs, files in os.walk(efi_dir):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), efi_dir)
            src_path = os.path.join(root, fn)
            dest_path = os.path.join(dst, 'EFI', rel)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(src_path, dest_path)
            log_cb(_("Copied EFI file: ") + src_path)


def find_minios_source() -> Optional[str]:
    """
    Find the MiniOS source directory from common locations.
    Returns the path if found, None otherwise.
    """
    candidates = [
        "/run/initramfs/memory/data/minios",
        "/run/initramfs/memory/iso/minios", 
        "/run/initramfs/memory/toram",
        "/run/initramfs/memory/data/from/0/minios"
    ]
    
    for candidate in candidates:
        if (os.path.isdir(candidate) and 
            os.path.exists(os.path.join(candidate, "boot", "vmlinuz"))):
            return candidate
    
    return None


def _calculate_copy_size(src: str) -> int:
    """
    Calculate total size of files to be copied.
    """
    total = 0
    for root, dirs, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), src)
            if rel.startswith('changes/'):
                continue
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except:
                pass
    return total
