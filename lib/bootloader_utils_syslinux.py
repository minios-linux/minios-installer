#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - SYSLINUX Bootloader Support
Additional function for SYSLINUX bootloader installation.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import shutil
import subprocess
from typing import Optional, Callable
import gettext

# Initialize gettext
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def install_syslinux_bootloader(device: str, primary: str, efi: Optional[str], boot_dir: str,
                               progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install SYSLINUX bootloader using files from the MiniOS image.
    This is essentially the same as EXTLINUX installation.
    """
    progress_cb(96, _("Installing SYSLINUX bootloader..."))
    
    arch = subprocess.check_output(['uname', '-m'], text=True).strip()
    exe = 'extlinux.x64' if arch == 'x86_64' else 'extlinux.x32'
    exe_path = os.path.join(boot_dir, exe)
    
    # Check if SYSLINUX/EXTLINUX is executable; try remount+chmod if not
    if not os.access(exe_path, os.X_OK):
        try:
            subprocess.run(
                ['mount', '-o', 'remount,exec', boot_dir],
                cwd=boot_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True
            )
            log_cb(_("Remounted boot directory with exec."))
        except subprocess.CalledProcessError:
            log_cb(_("Failed to remount boot directory; proceeding."))
        os.chmod(exe_path, 0o755)
        log_cb(_("Made extlinux executable."))
    
    # If still not executable, copy to syslinux.exe and adjust exe/exe_path
    if not os.access(exe_path, os.X_OK):
        fallback_name = 'syslinux.exe'
        fallback_path = os.path.join(boot_dir, fallback_name)
        shutil.copy2(exe_path, fallback_path)
        os.chmod(fallback_path, 0o755)
        log_cb(_("Copied syslinux to fallback: {fallback_name}").format(fallback_name=fallback_name))
        exe = fallback_name
        exe_path = fallback_path
    
    # Try primary syslinux install, logging output line by line
    proc = subprocess.run(
        [exe_path, '--install', boot_dir],
        cwd=boot_dir,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True
    )
    for line in proc.stdout.splitlines():
        log_cb(line)
    for line in proc.stderr.splitlines():
        log_cb(line)
    
    # If primary failed, try fallback in /tmp
    if proc.returncode != 0:
        log_cb(_("syslinux install failed (code {code}), trying fallback in /tmp...").format(code=proc.returncode))
        tmp_exe = os.path.join('/tmp', exe)
        shutil.copy2(exe_path, tmp_exe)
        os.chmod(tmp_exe, 0o755)
        proc2 = subprocess.run(
            [tmp_exe, '--install', boot_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True
        )
        for line in proc2.stdout.splitlines():
            log_cb(line)
        for line in proc2.stderr.splitlines():
            log_cb(line)
        
        if proc2.returncode != 0:
            raise RuntimeError(_("Error installing boot loader (fallback code {code}).").format(code=proc2.returncode))
        else:
            os.remove(tmp_exe)
            log_cb(_("Boot loader installation succeeded via fallback."))
    else:
        log_cb(_("Ran syslinux installer (code {code}).").format(code=proc.returncode))
    
    # Import MBR and partition functions from main bootloader_utils
    from bootloader_utils import _write_mbr, _set_active_partition
    
    # Write MBR and set active partition if needed
    if device != primary:
        _write_mbr(device, boot_dir, log_cb)
        _set_active_partition(device, primary, log_cb)
    
    # Cleanup fallback binary if it exists in boot_dir
    fallback = os.path.join(boot_dir, 'syslinux.exe')
    if os.path.isfile(fallback):
        try:
            os.remove(fallback)
            log_cb(_("Removed fallback binary: syslinux.exe."))
        except OSError:
            pass
    
    log_cb(_("SYSLINUX bootloader installation completed"))