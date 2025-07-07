#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Bootloader Utilities
Utilities for installing and configuring the EXTLINUX bootloader.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import re
import shutil
import subprocess
from typing import Optional, Callable
import gettext

# Initialize gettext
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def install_bootloader(device: str, primary: str, efi: Optional[str], 
                      progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install the EXT Linux bootloader; write MBR if needed.
    Aborts immediately if cancellation is requested.
    """
    # Check for user cancellation
    owner = getattr(progress_cb, "__self__", None)
    if owner and owner.cancel_requested:
        raise RuntimeError(_("Installation canceled by user."))
    
    # Prepare paths
    boot_dir = os.path.join("/mnt/install", os.path.basename(primary), "minios", "boot")
    log_cb(_("Entering bootloader directory: {boot_dir}").format(boot_dir=boot_dir))
    
    # Notify progress
    progress_cb(96, _("Setting up bootloader."))
    arch = subprocess.check_output(['uname', '-m'], text=True).strip()
    exe = 'extlinux.x64' if arch == 'x86_64' else 'extlinux.x32'
    exe_path = os.path.join(boot_dir, exe)
    
    # Check if EXTLINUX is executable; try remount+chmod if not
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
    
    # If still not executable, copy to extlinux.exe and adjust exe/exe_path
    if not os.access(exe_path, os.X_OK):
        fallback_name = 'extlinux.exe'
        fallback_path = os.path.join(boot_dir, fallback_name)
        shutil.copy2(exe_path, fallback_path)
        os.chmod(fallback_path, 0o755)
        log_cb(_("Copied extlinux to fallback: {fallback_name}").format(fallback_name=fallback_name))
        exe = fallback_name
        exe_path = fallback_path
    
    # Try primary extlinux install, logging output line by line
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
        log_cb(_("extlinux install failed (code {code}), trying fallback in /tmp...").format(code=proc.returncode))
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
        log_cb(_("Ran extlinux installer (code {code}).").format(code=proc.returncode))
    
    # Write MBR and set active partition if needed
    if device != primary:
        _write_mbr(device, boot_dir, log_cb)
        _set_active_partition(device, primary, log_cb)
    
    # Cleanup fallback binary if it exists in boot_dir
    fallback = os.path.join(boot_dir, 'extlinux.exe')
    if os.path.isfile(fallback):
        try:
            os.remove(fallback)
            log_cb(_("Removed fallback binary: extlinux.exe."))
        except OSError:
            pass


def _write_mbr(device: str, boot_dir: str, log_cb: Callable) -> None:
    """
    Write MBR to the device.
    """
    mbr = os.path.join(boot_dir, 'mbr.bin')
    subprocess.check_call(
        ['dd', 'bs=440', 'count=1', 'conv=notrunc', f'if={mbr}', f'of={device}'],
        stderr=subprocess.DEVNULL
    )
    log_cb(_("Wrote MBR to {device}.").format(device=device))


def _set_active_partition(device: str, primary: str, log_cb: Callable) -> None:
    """
    Set the primary partition as active and deactivate others.
    """
    # Deactivate existing active partitions
    try:
        fdisk_output = subprocess.check_output(
            ['fdisk', '-l', device],
            stderr=subprocess.STDOUT,
            text=True
        )
    except subprocess.CalledProcessError:
        fdisk_output = ""
    
    old_parts = []
    for line in fdisk_output.splitlines():
        m = re.match(r'^(\S+)\s+\*', line)
        if m and m.group(1).startswith(device):
            num = re.sub(r'.*[^0-9]', '', m.group(1))
            if num:
                old_parts.append(num)
    
    # Build fdisk commands: toggle off old, toggle on new, then write
    cmds = []
    for old in old_parts:
        cmds += ['a', old]
    part_num = re.sub(r'.*[^0-9]', '', primary)
    cmds += ['a', part_num, 'w']
    fd_input = "\n".join(cmds) + "\n"
    # Run fdisk commands and log output
    proc = subprocess.run(
        ['fdisk', device],
        input=fd_input, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    for line in proc.stdout.splitlines():
        log_cb(line)
    for line in proc.stderr.splitlines():
        log_cb(line)
    
    log_cb(_("Set partition active: {primary}.").format(primary=primary))
