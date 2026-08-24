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


def _raise_if_canceled(cancel_cb: Optional[Callable[[], bool]]) -> None:
    if cancel_cb and cancel_cb():
        from install_state import InstallCanceled
        raise InstallCanceled(_("Installation canceled by user."))


def install_bootloader(device: str, primary: str, efi: Optional[str],
                      progress_cb: Callable, log_cb: Callable, root_mount: str,
                      cancel_cb: Optional[Callable[[], bool]] = None) -> None:
    """
    Install bootloader based on detected type from MiniOS boot directory.
    Supports: grub-only, syslinux-grub, syslinux-native
    Aborts immediately if cancellation is requested.
    """
    _raise_if_canceled(cancel_cb)

    # Detect bootloader type from live system source
    bootloader_type = detect_bootloader_type()

    log_cb(_("Detected bootloader type: {type}").format(type=bootloader_type))

    if bootloader_type == 'grub-only':
        install_grub_only(device, primary, efi, progress_cb, log_cb, root_mount)
    elif bootloader_type == 'syslinux-native':
        install_syslinux_native(device, primary, efi, progress_cb, log_cb, root_mount)
    else:  # syslinux-grub (default)
        install_syslinux_grub(device, primary, efi, progress_cb, log_cb, root_mount)


def install_grub_only(device: str, primary: str, efi: Optional[str],
                     progress_cb: Callable, log_cb: Callable, root_mount: str) -> None:
    """
    Install GRUB only (no SYSLINUX) for BIOS and UEFI boot.
    Uses pre-built core.img from the MiniOS image.
    """
    progress_cb(96, _("Installing GRUB bootloader..."))

    boot_dir = os.path.join(root_mount, "minios", "boot")
    grub_dir = os.path.join(boot_dir, "grub", "i386-pc")
    core_img = os.path.join(grub_dir, "core.img")
    boot_img = os.path.join(grub_dir, "boot.img")

    if not os.path.exists(core_img):
        raise RuntimeError(_("GRUB core.img not found in image"))

    if not os.path.exists(boot_img):
        raise RuntimeError(_("GRUB boot.img not found in image"))

    # core.img is embedded in the post-MBR gap. Never overwrite partition data
    # if an image has a larger core than the actual first-partition gap.
    try:
        start_sector = int(subprocess.check_output(["lsblk", "-n", "-o", "START", primary], universal_newlines=True).strip())
        sector_size = int(subprocess.check_output(["lsblk", "-n", "-o", "LOG-SEC", device], universal_newlines=True).strip())
    except (ValueError, subprocess.CalledProcessError):
        raise RuntimeError(_("Cannot determine the MBR embedding area for GRUB core.img."))
    embedding_bytes = max(0, start_sector * sector_size - 512)
    if os.path.getsize(core_img) > embedding_bytes:
        raise RuntimeError(_("GRUB core.img does not fit in the post-MBR embedding area."))

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
                           progress_cb: Callable, log_cb: Callable, root_mount: str) -> None:
    """
    Install native SYSLINUX for BIOS boot (no GRUB BIOS components).
    """
    boot_dir = os.path.join(root_mount, "minios", "boot", "syslinux")
    log_cb(_("Using native SYSLINUX bootloader"))
    log_cb(_("Entering bootloader directory: {boot_dir}").format(boot_dir=boot_dir))

    install_extlinux_bootloader(device, primary, efi, boot_dir, progress_cb, log_cb)


def install_syslinux_grub(device: str, primary: str, efi: Optional[str],
                         progress_cb: Callable, log_cb: Callable, root_mount: str) -> None:
    """
    Install SYSLINUX that loads GRUB for BIOS boot.
    """
    boot_dir = os.path.join(root_mount, "minios", "boot", "syslinux")
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
    tmp_exe = None
    if not os.path.exists(exe_path):
        raise RuntimeError(_("EXTLINUX installer not found: {path}").format(path=exe_path))

    proc = None
    try:
        os.chmod(exe_path, 0o755)
        try:
            proc = subprocess.run(
                [exe_path, '--install', boot_dir],
                cwd=boot_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True
            )
        except OSError as exc:
            log_cb(_("Could not run EXTLINUX from the target filesystem: {error}").format(error=str(exc)))

        if proc is not None:
            for line in proc.stdout.splitlines():
                log_cb(line)
            for line in proc.stderr.splitlines():
                log_cb(line)

        if proc is None or proc.returncode != 0:
            if proc is not None:
                log_cb(_("EXTLINUX install failed (code {code}); retrying from a temporary executable.").format(code=proc.returncode))
            import tempfile
            fd, tmp_exe = tempfile.mkstemp(prefix='extlinux-', suffix='.bin')
            os.close(fd)
            shutil.copyfile(exe_path, tmp_exe)
            os.chmod(tmp_exe, 0o755)
            proc = subprocess.run(
                [tmp_exe, '--install', boot_dir],
                cwd=boot_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True
            )
            for line in proc.stdout.splitlines():
                log_cb(line)
            for line in proc.stderr.splitlines():
                log_cb(line)

        if proc.returncode != 0:
            raise RuntimeError(_("Error installing boot loader (code {code}).").format(code=proc.returncode))
        log_cb(_("Ran extlinux installer (code {code}).").format(code=proc.returncode))

        # Write MBR and set active partition if needed
        if device != primary:
            _write_mbr(device, boot_dir, log_cb)
            _set_active_partition(device, primary, log_cb)

        log_cb(_("EXTLINUX bootloader installation completed"))
    finally:
        if tmp_exe and os.path.isfile(tmp_exe):
            try:
                os.remove(tmp_exe)
            except OSError:
                pass


def _find_mount_point(path: str) -> Optional[str]:
    """Walk up from *path* until a mount point is found."""
    path = os.path.abspath(path)
    while True:
        if os.path.ismount(path):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


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

    On BIOS/MBR this is required for many firmwares; failure is a hard error
    so install does not report success with an inactive boot partition.
    """
    # Extract partition number
    part_num = re.sub(r'.*[^0-9]', '', primary)
    if not part_num:
        raise RuntimeError(
            _("Could not extract partition number from {primary}").format(primary=primary)
        )

    if not shutil.which('sfdisk'):
        raise RuntimeError(
            _("sfdisk not found; required to set the active partition for BIOS installs. "
              "Install the util-linux package.")
        )

    try:
        proc = subprocess.run(
            ['sfdisk', '-A', device, part_num],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=30
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(_("sfdisk timed out while setting active partition on {device}").format(device=device))
    except OSError as exc:
        raise RuntimeError(_("Failed to run sfdisk: {error}").format(error=str(exc)))

    if proc.stdout:
        for line in proc.stdout.splitlines():
            log_cb(f"[sfdisk] {line}")
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log_cb(f"[sfdisk] {line}")

    if proc.returncode != 0:
        raise RuntimeError(
            _("Failed to set active (bootable) partition {primary} on {device} "
              "(sfdisk exit code {code}). BIOS firmware may not boot this disk.").format(
                primary=primary, device=device, code=proc.returncode
            )
        )
    log_cb(_("Set partition active: {primary}").format(primary=primary))
