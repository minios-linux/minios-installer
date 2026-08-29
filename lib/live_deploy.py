#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import os
import shutil
import subprocess
from typing import Callable, Optional

from bootloader_utils import install_bootloader
from copy_utils import copy_efi_files, copy_minios_files, efi_payload_bytes, find_minios_source, verify_efi_payload
from disk_utils import resolve_install_device
from install_state import InstallCanceled, InstallState
from mount_utils import unmount_partitions
from network_config import create_live_network_hook, source_supports_live_network
from module_selection import discover_module_names
from partition_executor import execute_plan
from partition_planner import build_plan
from partition_scanner import scan_disk
from user_config_writer import write_live_config
from minios_security.security_profiles import live_config_for_profile


gettext.bindtextdomain("minios-installer", "/usr/share/locale")
gettext.textdomain("minios-installer")
_ = gettext.gettext

INITRD_CRYPTO_MARKER = "/run/initramfs/etc/minios-initramfs-crypt"


class _ProgressAdapter:
    def __init__(self, state, progress_cb, minimum_percent=0):
        self._state = state
        self._progress_cb = progress_cb
        self._minimum_percent = minimum_percent

    @property
    def cancel_requested(self):
        return self._state.cancel_requested

    def __call__(self, percent, message):
        self._progress_cb(max(percent, self._minimum_percent), message)


def runtime_supports_luks_persistence() -> bool:
    """Return whether the running initrd advertises LUKS persistence."""
    return os.path.isfile(INITRD_CRYPTO_MARKER)


def _cleanup_temp_config(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


def _raise_if_canceled(state: InstallState) -> None:
    if state.cancel_requested:
        raise InstallCanceled(_("Installation canceled by user."))


def _append_live_hook_option(value: str) -> str:
    options = [item for item in (value or "").split() if item]
    if not any(item in ("hooks=medium", "live-config.hooks=medium") for item in options):
        options.append("hooks=medium")
    return " ".join(options)


def _source_initrd_paths(source: str) -> tuple:
    boot_dir = os.path.join(source, "boot")
    try:
        names = os.listdir(boot_dir)
    except OSError:
        return ()

    boot_dir_real = os.path.realpath(boot_dir)
    initrds = []
    seen = set()
    for name in names:
        if not name.startswith(("initrfs", "initrd")):
            continue
        path = os.path.realpath(os.path.join(boot_dir, name))
        try:
            if os.path.commonpath((boot_dir_real, path)) != boot_dir_real:
                continue
        except ValueError:
            continue
        if not os.path.isfile(path) or path in seen:
            continue
        seen.add(path)
        initrds.append(path)
    return tuple(initrds)


def source_supports_luks_persistence(source: str) -> bool:
    """Read source initrd file lists and require the MiniOS crypto hook in each."""
    initrds = _source_initrd_paths(source)
    tool = shutil.which("lsinitramfs") or shutil.which("lsinitrd")
    if not initrds or not tool:
        return False
    for initrd in initrds:
        try:
            result = subprocess.run(
                [tool, initrd], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                universal_newlines=True, check=False, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 or not any(
            line.strip().lstrip("./").endswith("etc/minios-initramfs-crypt")
            for line in result.stdout.splitlines()
        ):
            return False
    return True


def _persistence_boot_options(state: InstallState, source: str) -> tuple:
    mode = state.persistence_mode
    if mode == "none":
        return ()
    if state.install_mode != "live":
        raise RuntimeError(_("Session persistence is available only for live installations."))
    if mode not in ("native", "dynfilefs", "raw", "luks"):
        raise RuntimeError(_("Unknown session persistence mode: {mode}").format(mode=mode))
    if mode == "native":
        return ("perchmode=native",)
    if state.persistence_size_mib <= 0:
        raise RuntimeError(_("Container persistence requires a size greater than zero."))
    if mode == "luks" and not source_supports_luks_persistence(source):
        raise RuntimeError(_("Encrypted session storage is not supported by this MiniOS image. Choose another session storage mode."))
    return ("perchmode={}".format(mode), "perchsize={}".format(state.persistence_size_mib))


def run_live_install(
    state: InstallState,
    progress_cb: Callable[[int, str], None],
    log_cb: Callable[[str], None],
    dry_run: bool = False,
) -> None:
    if state.placement == "manual":
        raise RuntimeError(_("Manual partitioning plans are not executable yet."))
    if not state.target_device:
        raise RuntimeError(_("No target device selected."))
    state.target_device = resolve_install_device(
        state.target_device,
        expected_identity=state.target_device_identity,
    )
    # Prove the actual source image supports the requested initrd feature before
    # any partitioning work. The running initrd can differ from the copied one.
    src = find_minios_source()
    if not src:
        raise RuntimeError(_("Cannot find MiniOS image."))
    boot_options = _persistence_boot_options(state, src)
    layout = scan_disk(state.target_device)
    # Always rebuild from current placement/filesystem; summary plan is preview-only.
    plan = build_plan(layout, state.placement, state.filesystem, install_mode=state.install_mode, swap_size_mib=0, boot_layout=state.boot_layout, alongside_size_mib=state.alongside_size_mib, required_root_mib=state.required_root_mib)
    if any(part.role == "esp" for part in plan.partitions):
        # Live media keep an EFI payload even when the installer itself booted via BIOS.
        # Prove it fits the fixed/new or reused ESP before any destructive action.
        efi_bytes = efi_payload_bytes(src)
        plan = build_plan(layout, state.placement, state.filesystem, install_mode=state.install_mode, swap_size_mib=0, boot_layout=state.boot_layout, alongside_size_mib=state.alongside_size_mib, required_root_mib=state.required_root_mib, efi_payload_bytes=efi_bytes)
    state.partition_plan = plan

    progress_cb(0, _("Preparing target disk..."))

    root_part = esp_part = root_mount = esp_mount = None
    generated_config = None
    generated_network_hook = None
    success = False
    try:
        root_part, esp_part, root_mount, esp_mount = execute_plan(
            plan,
            log_cb,
            dry_run=dry_run,
            cancel_cb=lambda: state.cancel_requested,
        )
        if dry_run:
            progress_cb(100, _("Dry run complete."))
            success = True
            return

        _raise_if_canceled(state)
        network_hooks = None
        use_live_network = False
        if state.user_config.network_method == "static":
            use_live_network = source_supports_live_network(
                src,
                module_names=discover_module_names(),
                selected_modules=state.selected_modules,
            )
            if not use_live_network:
                generated_network_hook = create_live_network_hook(
                    state.user_config.network_interface,
                    state.user_config.network_address,
                    state.user_config.network_prefix,
                    state.user_config.network_gateway,
                    state.user_config.network_dns,
                )
                state.user_config.config_cmdline = _append_live_hook_option(state.user_config.config_cmdline)
                network_hooks = {"1000-network-manager.sh": generated_network_hook}
            state.user_config_customized = True
        profile_config = live_config_for_profile(state.security_profile)
        profile_config.pop("LIVE_SECURITY_PROFILE", None)
        if state.config_override_path and (state.user_config_customized or profile_config):
            generated_config = write_live_config(state.user_config, state.config_override_path, profile_config, include_live_network=use_live_network)
            config_override = generated_config
        elif state.config_override_path:
            config_override = state.config_override_path
        elif state.user_config_customized or profile_config:
            generated_config = write_live_config(state.user_config, extra_entries=profile_config, include_live_network=use_live_network)
            config_override = generated_config
        else:
            config_override = None

        progress_cb(18, _("Copying MiniOS files..."))
        copy_minios_files(
            src,
            root_mount,
            progress_cb,
            log_cb,
            config_override,
            state.boot_config_type,
            state.selected_modules,
            cancel_cb=lambda: state.cancel_requested,
            config_hooks=network_hooks,
            boot_options=boot_options,
        )
        _raise_if_canceled(state)
        if esp_mount:
            progress_cb(97, _("Copying EFI files to ESP..."))
            copy_efi_files(src, esp_mount, log_cb)
            verify_efi_payload(src, esp_mount)
        else:
            progress_cb(97, _("Copying EFI files to root..."))
            copy_efi_files(src, root_mount, log_cb)
            verify_efi_payload(src, root_mount)
        _raise_if_canceled(state)

        # BIOS/MBR: install SYSLINUX/GRUB-BIOS. UEFI/GPT: EFI files already on ESP
        # (or on FAT32 root marked as ESP when no separate ESP).
        if not plan.use_efi:
            progress_cb(98, _("Installing BIOS bootloader..."))
            install_bootloader(
                state.target_device,
                root_part,
                esp_part,
                _ProgressAdapter(state, progress_cb, 98),
                log_cb,
                root_mount,
                cancel_cb=lambda: state.cancel_requested,
            )
        else:
            log_cb(_("UEFI/GPT install: EFI files copied; no BIOS bootloader written."))

        _raise_if_canceled(state)

        progress_cb(99, _("Unmounting disk..."))
        if root_mount or esp_mount:
            unmount_partitions(root_part, esp_part, root_mount, esp_mount)
            log_cb(_("Cleaned up target mounts."))
            root_mount = None
            esp_mount = None
        progress_cb(100, _("Installation complete!"))
        success = True
    finally:
        # Generated merged configs may contain passwords; never leave them in /tmp.
        if generated_config and generated_config != state.config_override_path:
            _cleanup_temp_config(generated_config)
        _cleanup_temp_config(generated_network_hook)
        # Best-effort cleanup on cancel/failure (success path already unmounted).
        if not success and (root_mount or esp_mount):
            try:
                unmount_partitions(root_part, esp_part, root_mount, esp_mount)
                log_cb(_("Cleaned up target mounts."))
            except Exception as exc:
                log_cb(_("Warning: failed to unmount target: {error}").format(error=exc))
