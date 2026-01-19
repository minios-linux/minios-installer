#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Bootloader Utilities
Utilities for installing and configuring bootloaders (GRUB and EXTLINUX).

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import re
import shutil
import subprocess
from typing import Optional, Callable
import gettext
from copy_utils import find_minios_source

# SYSLINUX support is now integrated directly in this file

# Initialize gettext
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def detect_bootloader_type(minios_source: str = None) -> str:
    """
    Detect bootloader type based on files present in the boot directory.
    Supports: grub-only, syslinux-grub, syslinux-native
    """
    if not minios_source:
        minios_source = find_minios_source()
    
    if not minios_source or not os.path.exists(minios_source):
        # Fallback to syslinux-grub if can't detect
        return 'syslinux-grub'
    
    boot_dir = os.path.join(minios_source, "boot")
    if not os.path.exists(boot_dir):
        return 'syslinux-grub'
    
    # Check for SYSLINUX directory
    syslinux_dir = os.path.join(boot_dir, "syslinux")
    has_syslinux = os.path.exists(syslinux_dir)
    
    # Check for GRUB BIOS components
    grub_bios_dir = os.path.join(boot_dir, "grub", "i386-pc")
    has_grub_bios = os.path.exists(grub_bios_dir)
    
    # Determine bootloader type based on what's present
    # Check syslinux-grub first as it can coexist with grub-only files
    if has_syslinux and has_grub_bios:
        return 'syslinux-grub'
    elif has_syslinux and not has_grub_bios:
        return 'syslinux-native'
    elif not has_syslinux and has_grub_bios:
        return 'grub-only'
    else:
        # Fallback
        return 'syslinux-grub'


def install_bootloader(device: str, primary: str, efi: Optional[str], 
                      progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install bootloader based on detected type from MiniOS boot directory.
    Supports: grub-only, syslinux-grub, syslinux-native
    Aborts immediately if cancellation is requested.
    """
    # Check for user cancellation
    owner = getattr(progress_cb, "__self__", None)
    if owner and owner.cancel_requested:
        raise RuntimeError(_("Installation canceled by user."))
    
    # Detect bootloader type from live system source
    bootloader_type = detect_bootloader_type()
    
    log_cb(_("Detected bootloader type: {type}").format(type=bootloader_type))
    
    if bootloader_type == 'grub-only':
        install_grub_only(device, primary, efi, progress_cb, log_cb)
    elif bootloader_type == 'syslinux-native':
        install_syslinux_native(device, primary, efi, progress_cb, log_cb) 
    else:  # syslinux-grub (default)
        install_syslinux_grub(device, primary, efi, progress_cb, log_cb)


def install_grub_only(device: str, primary: str, efi: Optional[str], 
                     progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install GRUB only (no SYSLINUX) for BIOS and UEFI boot.
    Uses pre-built core.img from the MiniOS image.
    """
    progress_cb(96, _("Installing GRUB bootloader..."))
    
    boot_dir = os.path.join("/mnt/install", os.path.basename(primary), "minios", "boot")
    grub_dir = os.path.join(boot_dir, "grub", "i386-pc")
    core_img = os.path.join(grub_dir, "core.img")
    boot_img = os.path.join(grub_dir, "boot.img")
    
    if not os.path.exists(core_img):
        raise RuntimeError(_("GRUB core.img not found in image"))
        
    if not os.path.exists(boot_img):
        raise RuntimeError(_("GRUB boot.img not found in image"))
    
    try:
        # Install boot.img to MBR (first 440 bytes)
        subprocess.check_call([
            'dd', 'bs=440', 'count=1', 'conv=notrunc',
            f'if={boot_img}', f'of={device}'
        ], stderr=subprocess.DEVNULL)
        log_cb(_("Installed GRUB boot.img to MBR"))
        
        # Install core.img after MBR
        subprocess.check_call([
            'dd', 'bs=512', 'seek=1', 'conv=notrunc',
            f'if={core_img}', f'of={device}'
        ], stderr=subprocess.DEVNULL)  
        log_cb(_("Installed GRUB core.img"))
        
        # Set active partition if needed
        if device != primary:
            _set_active_partition(device, primary, log_cb)
            
        log_cb(_("GRUB bootloader installation completed"))
            
    except subprocess.CalledProcessError as e:
        raise RuntimeError(_("Error installing GRUB bootloader: {error}").format(error=str(e)))


def install_syslinux_native(device: str, primary: str, efi: Optional[str],
                           progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install native SYSLINUX for BIOS boot (no GRUB BIOS components).
    """
    boot_dir = os.path.join("/mnt/install", os.path.basename(primary), "minios", "boot", "syslinux")
    log_cb(_("Using native SYSLINUX bootloader"))
    log_cb(_("Entering bootloader directory: {boot_dir}").format(boot_dir=boot_dir))
    
    install_extlinux_bootloader(device, primary, efi, boot_dir, progress_cb, log_cb)


def install_syslinux_grub(device: str, primary: str, efi: Optional[str],
                         progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install SYSLINUX that loads GRUB for BIOS boot.
    """
    boot_dir = os.path.join("/mnt/install", os.path.basename(primary), "minios", "boot", "syslinux")
    log_cb(_("Using SYSLINUX to load GRUB"))
    log_cb(_("Entering bootloader directory: {boot_dir}").format(boot_dir=boot_dir))
    
    install_extlinux_bootloader(device, primary, efi, boot_dir, progress_cb, log_cb)


def install_extlinux_bootloader(device: str, primary: str, efi: Optional[str], boot_dir: str,
                               progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install EXTLINUX bootloader using files from the MiniOS image.
    """
    progress_cb(96, _("Installing EXTLINUX bootloader..."))
    
    arch = subprocess.check_output(['uname', '-m'], universal_newlines=True).strip()
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
        universal_newlines=True
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
            universal_newlines=True
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
    
    log_cb(_("EXTLINUX bootloader installation completed"))


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
    Set the primary partition as active using sfdisk.
    """
    # Extract partition number
    part_num = re.sub(r'.*[^0-9]', '', primary)
    if not part_num:
        log_cb(_("Error: Could not extract partition number from {primary}").format(primary=primary))
        return
    
    # Use sfdisk to set partition as bootable
    try:
        proc = subprocess.run(
            ['sfdisk', '-A', device, part_num],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=30
        )
        
        # Log output for debugging
        if proc.stdout:
            for line in proc.stdout.splitlines():
                log_cb(f"[sfdisk] {line}")
        if proc.stderr:
            for line in proc.stderr.splitlines():
                log_cb(f"[sfdisk error] {line}")
        
        if proc.returncode == 0:
            log_cb(_("Set partition active: {primary}").format(primary=primary))
        else:
            log_cb(_("Error: sfdisk returned exit code {code}").format(code=proc.returncode))
            
    except subprocess.TimeoutExpired:
        log_cb(_("Error: sfdisk command timed out"))
    except FileNotFoundError:
        log_cb(_("Error: sfdisk not found - install util-linux package"))
    except Exception as e:
        log_cb(_("Error running sfdisk: {error}").format(error=str(e)))
