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

# Initialize gettext
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def detect_bootloader_type(boot_dir: str) -> str:
    """
    Detect which bootloader to use based on available files.
    Returns 'grub' if GRUB files are present, 'extlinux' otherwise.
    """
    # Check for GRUB files
    grub_files = [
        os.path.join(boot_dir, 'grub', 'i386-pc'),
        os.path.join(boot_dir, 'grub', 'x86_64-efi'),  
        os.path.join(boot_dir, 'grub', 'grub.cfg')
    ]
    
    # If any essential GRUB files exist, use GRUB
    if any(os.path.exists(f) for f in grub_files):
        return 'grub'
    
    # Check for EXTLINUX files
    arch = subprocess.check_output(['uname', '-m'], text=True).strip()
    extlinux_exe = 'extlinux.x64' if arch == 'x86_64' else 'extlinux.x32'
    extlinux_path = os.path.join(boot_dir, extlinux_exe)
    
    if os.path.exists(extlinux_path):
        return 'extlinux'
    
    # Default to GRUB (since it's now the default in build system)
    return 'grub'


def install_bootloader(device: str, primary: str, efi: Optional[str], 
                      progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install the appropriate bootloader (GRUB or EXTLINUX) based on available files.
    Aborts immediately if cancellation is requested.
    """
    # Check for user cancellation
    owner = getattr(progress_cb, "__self__", None)
    if owner and owner.cancel_requested:
        raise RuntimeError(_("Installation canceled by user."))
    
    # Prepare paths
    boot_dir = os.path.join("/mnt/install", os.path.basename(primary), "minios", "boot")
    log_cb(_("Entering bootloader directory: {boot_dir}").format(boot_dir=boot_dir))
    
    # Detect bootloader type
    bootloader_type = detect_bootloader_type(boot_dir)
    log_cb(_("Detected bootloader type: {type}").format(type=bootloader_type))
    
    if bootloader_type == 'grub':
        install_grub_bootloader(device, primary, efi, boot_dir, progress_cb, log_cb)
    else:
        install_extlinux_bootloader(device, primary, efi, boot_dir, progress_cb, log_cb)


def install_grub_bootloader(device: str, primary: str, efi: Optional[str], boot_dir: str,
                           progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install GRUB bootloader using files from the MiniOS image.
    """
    progress_cb(96, _("Installing GRUB bootloader..."))
    
    # Paths to GRUB files in the image
    grub_dir = os.path.join(boot_dir, 'grub')
    arch = subprocess.check_output(['uname', '-m'], text=True).strip()
    grub_pc_dir = os.path.join(grub_dir, 'i386-pc')
    
    # For BIOS boot, we need to install GRUB using the image files
    if device != primary:
        # Try portable grub-bios-setup from the image first
        grub_bios_setup = os.path.join(grub_pc_dir, 'grub-bios-setup')
        
        if os.path.exists(grub_bios_setup):
            # Make it executable
            os.chmod(grub_bios_setup, 0o755)
            log_cb(_("Using portable grub-bios-setup from image"))
            
            try:
                # Use the portable grub-bios-setup tool
                cmd = [
                    grub_bios_setup,
                    '--directory=' + grub_pc_dir,
                    '--device-map=/dev/null',
                    device
                ]
                
                proc = subprocess.run(cmd, capture_output=True, text=True, cwd=grub_pc_dir)
                for line in proc.stdout.splitlines():
                    log_cb(line)
                for line in proc.stderr.splitlines():
                    log_cb(line)
                    
                if proc.returncode == 0:
                    log_cb(_("GRUB installed successfully using grub-bios-setup"))
                    _set_active_partition(device, primary, log_cb)
                else:
                    log_cb(_("grub-bios-setup failed (code {code}), trying manual installation...").format(code=proc.returncode))
                    install_grub_manual(device, primary, boot_dir, grub_pc_dir, log_cb)
                    
            except Exception as e:
                log_cb(_("grub-bios-setup failed with exception: {error}").format(error=str(e)))
                install_grub_manual(device, primary, boot_dir, grub_pc_dir, log_cb)
        else:
            # Fallback to manual installation
            log_cb(_("grub-bios-setup not found in image, using manual installation..."))
            install_grub_manual(device, primary, boot_dir, grub_pc_dir, log_cb)
    
    log_cb(_("GRUB bootloader installation completed"))


def install_grub_manual(device: str, primary: str, boot_dir: str, grub_pc_dir: str, log_cb: Callable) -> None:
    """
    Manually install GRUB using boot images from the MiniOS image.
    Uses boot.img for MBR and diskboot.img for partition boot record.
    """
    log_cb(_("Performing manual GRUB installation..."))
    log_cb(_("Device: {device}, Primary partition: {primary}").format(device=device, primary=primary))
    log_cb(_("GRUB directory: {grub_dir}").format(grub_dir=grub_pc_dir))
    
    # Stage 1: Install boot.img to MBR (first 440 bytes)
    boot_img = os.path.join(grub_pc_dir, 'boot.img')
    if os.path.exists(boot_img):
        boot_img_size = os.path.getsize(boot_img)
        log_cb(_("Installing boot.img (size: {size} bytes) to MBR").format(size=boot_img_size))
        subprocess.check_call(
            ['dd', 'if=' + boot_img, 'of=' + device, 'bs=440', 'count=1', 'conv=notrunc'],
            stderr=subprocess.DEVNULL
        )
        log_cb(_("Installed GRUB stage 1 (boot.img) to MBR"))
    else:
        # Fallback to boot_hybrid.img if available
        boot_hybrid = os.path.join(grub_pc_dir, 'boot_hybrid.img')
        if os.path.exists(boot_hybrid):
            subprocess.check_call(
                ['dd', 'if=' + boot_hybrid, 'of=' + device, 'bs=440', 'count=1', 'conv=notrunc'],
                stderr=subprocess.DEVNULL
            )
            log_cb(_("Installed GRUB boot_hybrid.img to MBR as fallback"))
        else:
            raise RuntimeError(_("No GRUB boot images found in {dir}").format(dir=grub_pc_dir))
    
    # Stage 2: Install diskboot.img to partition boot sector if it exists
    diskboot_img = os.path.join(grub_pc_dir, 'diskboot.img') 
    if os.path.exists(diskboot_img):
        diskboot_img_size = os.path.getsize(diskboot_img)
        log_cb(_("Installing diskboot.img (size: {size} bytes) to partition").format(size=diskboot_img_size))
        # Write to the beginning of the primary partition
        subprocess.check_call(
            ['dd', 'if=' + diskboot_img, 'of=' + primary, 'bs=512', 'count=1', 'conv=notrunc'],
            stderr=subprocess.DEVNULL
        )
        log_cb(_("Installed GRUB stage 1.5 (diskboot.img) to partition"))
    else:
        log_cb(_("diskboot.img not found, skipping partition boot sector installation"))
    
    # Stage 3: Install core.img if available
    core_img = os.path.join(grub_pc_dir, 'core.img')
    if os.path.exists(core_img):
        try:
            subprocess.check_call(
                ['dd', 'if=' + core_img, 'of=' + device, 'bs=512', 'seek=1', 'conv=notrunc'],
                stderr=subprocess.DEVNULL
            )
            log_cb(_("Installed core.img to boot gap"))
        except subprocess.CalledProcessError as e:
            log_cb(_("Warning: Failed to install core.img: {error}").format(error=str(e)))
    else:
        log_cb(_("Warning: core.img not found in image - GRUB may not boot properly"))
    
    # Set active partition
    _set_active_partition(device, primary, log_cb)
    
    # Verify installation
    _verify_grub_installation(device, primary, log_cb)
    
    log_cb(_("GRUB bootloader installation completed"))


def install_extlinux_bootloader(device: str, primary: str, efi: Optional[str], boot_dir: str,
                               progress_cb: Callable, log_cb: Callable) -> None:
    """
    Install EXTLINUX bootloader using files from the MiniOS image.
    """
    progress_cb(96, _("Installing EXTLINUX bootloader..."))
    
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
    
    log_cb(_("EXTLINUX bootloader installation completed"))


def _verify_grub_installation(device: str, primary: str, log_cb: Callable) -> None:
    """
    Verify GRUB installation by checking MBR and partition boot sector.
    """
    try:
        # Check MBR signature
        with open(device, 'rb') as f:
            mbr = f.read(512)
            if len(mbr) >= 2:
                signature = mbr[-2:]
                if signature == b'\x55\xaa':
                    log_cb(_("MBR signature OK (0x55AA)"))
                else:
                    log_cb(_("Warning: MBR signature incorrect: {sig}").format(sig=signature.hex()))
            
            # Check for GRUB signature in MBR
            if b'GRUB' in mbr[:440]:
                log_cb(_("GRUB signature found in MBR"))
            else:
                log_cb(_("Warning: GRUB signature not found in MBR"))
        
        # Check partition boot sector
        with open(primary, 'rb') as f:
            pbs = f.read(512)
            if b'GRUB' in pbs:
                log_cb(_("GRUB signature found in partition boot sector"))
            else:
                log_cb(_("Warning: GRUB signature not found in partition boot sector"))
    
    except Exception as e:
        log_cb(_("Warning: Failed to verify GRUB installation: {error}").format(error=str(e)))


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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
