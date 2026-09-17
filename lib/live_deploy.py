#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import os
import json
import shutil
import subprocess
import tempfile
from typing import Callable, Optional

from bootloader_utils import install_bootloader
from copy_utils import copy_efi_files, copy_minios_files, efi_payload_bytes, find_minios_source, verify_efi_payload
from disk_utils import resolve_install_device
from install_state import InstallCanceled, InstallState
from session_storage import create_live_session, preflight_session_storage
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
INITRD_DYNBLK_MARKER = "/run/initramfs/etc/minios-initramfs-dynblk"
LUKS_LAYER_CAPABILITY = "luks-layer-v1"
DYNBLK_COMPRESSION_CODECS = (
    "none", "lz4", "lz4hc", "lzo", "lzo-rle", "zstd", "deflate", "842",
)


def _dynblk_codecs_from_initramfs_tree(root: str, kernel: str = None) -> tuple:
    """Return crypto_comp codecs available from one unpacked kernel/initrd tree."""
    modprobe = shutil.which("modprobe")
    if not modprobe:
        modprobe = next(
            (path for path in ("/usr/sbin/modprobe", "/sbin/modprobe")
             if os.access(path, os.X_OK)), None)
    if not modprobe:
        return ("none",)
    module_dirs = []
    seen_module_dirs = set()
    for prefix in ("", "main", "early"):
        base = os.path.normpath(os.path.join(root, prefix))
        for relative in (os.path.join("lib", "modules"), os.path.join("usr", "lib", "modules")):
            modules = os.path.join(base, relative)
            real_modules = os.path.realpath(modules)
            if os.path.isdir(modules) and real_modules not in seen_module_dirs:
                seen_module_dirs.add(real_modules)
                module_dirs.append((base, modules))
    if not module_dirs:
        return ("none",)

    supported = set(DYNBLK_COMPRESSION_CODECS)
    probed = False
    with tempfile.TemporaryDirectory(prefix="minios-kmod-probe-") as scratch:
        config_dir = os.path.join(scratch, "modprobe.d")
        os.mkdir(config_dir)
        for index, (base, modules) in enumerate(module_dirs):
            probe_root = base
            if os.path.realpath(modules) != os.path.realpath(os.path.join(base, "lib", "modules")):
                probe_root = os.path.join(scratch, "root-{}".format(index))
                os.makedirs(os.path.join(probe_root, "lib"))
                os.symlink(modules, os.path.join(probe_root, "lib", "modules"))
            versions = sorted(
                name for name in os.listdir(modules)
                if os.path.isdir(os.path.join(modules, name)) and
                (kernel is None or name == kernel)
            )
            for version in versions:
                probed = True
                available = {"none"}
                for codec in DYNBLK_COMPRESSION_CODECS[1:]:
                    try:
                        result = subprocess.run(
                            [modprobe, "-d", probe_root, "-S", version,
                             "-C", config_dir, "--ignore-install", "--show-depends",
                             "crypto-{}".format(codec)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=5,
                        )
                    except (OSError, subprocess.SubprocessError):
                        continue
                    if result.returncode == 0:
                        available.add(codec)
                supported.intersection_update(available)
    if not probed:
        return ("none",)
    return tuple(codec for codec in DYNBLK_COMPRESSION_CODECS if codec in supported)


def runtime_dynblk_compression_codecs() -> tuple:
    """Return codecs available to the currently running MiniOS initramfs."""
    return _dynblk_codecs_from_initramfs_tree(
        "/run/initramfs", kernel=os.uname().release)


def source_dynblk_compression_codecs(source: str) -> tuple:
    """Return codecs supported by every initrd that the installer will copy."""
    initrds = _source_initrd_paths(source)
    if not initrds:
        return ("none",)
    supported = set(DYNBLK_COMPRESSION_CODECS)
    for initrd in initrds:
        with tempfile.TemporaryDirectory(prefix="minios-initrd-codecs-") as extracted:
            if not _unpack_source_initrd(initrd, extracted):
                return ("none",)
            supported.intersection_update(_dynblk_codecs_from_initramfs_tree(extracted))
    return tuple(codec for codec in DYNBLK_COMPRESSION_CODECS if codec in supported)


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


def _marker_has_capability(path: str, capability: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as marker:
            return capability in {line.strip() for line in marker if line.strip()}
    except OSError:
        return False


def runtime_supports_dynblk_persistence() -> bool:
    if not shutil.which("dynblk") or not os.path.isfile(INITRD_DYNBLK_MARKER):
        return False
    if os.path.isdir("/sys/module/dynblk"):
        return True
    if not shutil.which("modinfo"):
        return False
    try:
        return subprocess.run(
            ["modinfo", "dynblk"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
    except OSError:
        return False


def runtime_supports_vmdk_persistence() -> bool:
    """Require a driver and boot scripts which understand VMDK session metadata."""
    if not runtime_supports_dynblk_persistence() or not _marker_has_capability(
            INITRD_DYNBLK_MARKER, "vmdk-session-v1"):
        return False
    try:
        result = subprocess.run(["dynblk", "limits", "--format", "vmdk", "--json"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, check=False)
        return result.returncode == 0 and json.loads(result.stdout).get("storage_format") == "vmdk"
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return False


def source_supports_vmdk_persistence(source: str) -> bool:
    """All copied initrds must be able to resume the new session mode."""
    initrds = _source_initrd_paths(source)
    if not initrds:
        return False
    for initrd in initrds:
        content = _source_marker_content(initrd, "minios-initramfs-dynblk")
        if content is None or b"vmdk-session-v1" not in content.splitlines():
            return False
    return True


def runtime_supports_luks_persistence(backend: str = "raw") -> bool:
    """Return whether layered LUKS is usable with one running backend."""
    if backend not in ("raw", "dynfilefs", "dynblk", "vmdk"):
        return False
    if not shutil.which("cryptsetup") or not _marker_has_capability(
        INITRD_CRYPTO_MARKER, LUKS_LAYER_CAPABILITY
    ):
        return False
    if backend in ("raw", "dynfilefs") and not shutil.which("losetup"):
        return False
    if backend == "dynfilefs" and not (
        shutil.which("dynfilefs") or shutil.which("mount.dynfilefs")
    ):
        return False
    if backend == "vmdk":
        return runtime_supports_vmdk_persistence()
    return backend != "dynblk" or runtime_supports_dynblk_persistence()


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


def _unpack_source_initrd(initrd: str, destination: str) -> bool:
    tool = shutil.which("unmkinitramfs")
    if not tool:
        return False
    try:
        result = subprocess.run(
            [tool, initrd, destination], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _unpacked_marker_paths(root: str, name: str) -> tuple:
    relative = os.path.join("etc", name)
    return tuple(os.path.join(root, prefix, relative) for prefix in ("", "main", "early"))


def _source_marker_content(initrd: str, name: str):
    lsinitrd = shutil.which("lsinitrd")
    if lsinitrd:
        try:
            result = subprocess.run(
                [lsinitrd, "-f", "etc/{}".format(name), initrd],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False, timeout=30,
            )
            if result.returncode == 0:
                return result.stdout
        except (OSError, subprocess.SubprocessError):
            return None
    with tempfile.TemporaryDirectory(prefix="minios-initrd-check-") as extracted:
        if not _unpack_source_initrd(initrd, extracted):
            return None
        for marker in _unpacked_marker_paths(extracted, name):
            try:
                with open(marker, "rb") as stream:
                    return stream.read(4096)
            except OSError:
                continue
    return None


def source_supports_luks_persistence(source: str, backend: str = "raw") -> bool:
    """Require the layered-LUKS contract in every copied source initrd."""
    if backend not in ("raw", "dynfilefs", "dynblk", "vmdk"):
        return False
    initrds = _source_initrd_paths(source)
    if not initrds:
        return False
    for initrd in initrds:
        content = _source_marker_content(initrd, "minios-initramfs-crypt")
        if content is None or LUKS_LAYER_CAPABILITY not in {
            line.strip() for line in content.decode("utf-8", "replace").splitlines()
        }:
            return False
        if backend in ("dynblk", "vmdk"):
            content = _source_marker_content(initrd, "minios-initramfs-dynblk")
            if content is None or (backend == "vmdk" and b"vmdk-session-v1" not in content.splitlines()):
                return False
    return True


def source_supports_dynblk_persistence(source: str) -> bool:
    """Require the DynBlk resume marker in every copied source initrd."""
    initrds = _source_initrd_paths(source)
    if not initrds:
        return False
    for initrd in initrds:
        if _source_marker_content(initrd, "minios-initramfs-dynblk") is None:
            return False
    return True


def _validate_persistence_settings(state: InstallState, source: str) -> None:
    mode = state.persistence_mode
    encryption = state.persistence_encryption
    compression = state.persistence_compression
    if encryption not in ("none", "luks"):
        raise RuntimeError(_("Unknown session persistence encryption: {encryption}").format(
            encryption=encryption))
    if compression not in DYNBLK_COMPRESSION_CODECS:
        raise RuntimeError(_("Unknown compression codec for DynBlk: {compression}").format(
            compression=compression))
    if mode == "none":
        if encryption != "none":
            raise RuntimeError(_("Session encryption requires a persistence storage mode."))
        if compression != "none":
            raise RuntimeError(_("DynBlk compression requires DynBlk session storage."))
        return
    if state.install_mode != "live":
        raise RuntimeError(_("Session persistence is available only for live installations."))
    if mode not in ("native", "dynfilefs", "dynblk", "vmdk", "raw"):
        raise RuntimeError(_("Unknown session persistence mode: {mode}").format(mode=mode))
    if encryption == "luks" and mode not in ("raw", "dynfilefs", "dynblk", "vmdk"):
        raise RuntimeError(_("LUKS encryption is unavailable for this session storage mode."))
    if compression != "none" and mode != "dynblk":
        raise RuntimeError(_("DynBlk compression requires DynBlk session storage."))
    if encryption == "luks" and compression != "none":
        raise RuntimeError(_("DynBlk compression is unavailable with LUKS encryption."))
    if (mode == "dynblk" and compression != "none" and
            compression not in runtime_dynblk_compression_codecs()):
        raise RuntimeError(_(
            "DynBlk compression {compression} is not supported by the running kernel/initrd."
        ).format(compression=compression))
    if (mode == "dynblk" and compression != "none" and
            compression not in source_dynblk_compression_codecs(source)):
        raise RuntimeError(_(
            "DynBlk compression {compression} is not supported by the kernel/initrd in this MiniOS image."
        ).format(compression=compression))
    if mode == "native":
        if state.filesystem in ("fat32", "ntfs"):
            raise RuntimeError(_("Native session storage requires a POSIX-compatible filesystem."))
        return
    if state.persistence_size_mib <= 0:
        raise RuntimeError(_("Container persistence requires a size greater than zero."))
    if (mode == "dynblk" and encryption != "luks" and
            not source_supports_dynblk_persistence(source)):
        raise RuntimeError(_("DynBlk session storage is not supported by this MiniOS image. Choose another session storage mode."))
    if mode == "vmdk" and not source_supports_vmdk_persistence(source):
        raise RuntimeError(_("VMDK sessions are not supported by the initrd in this MiniOS image."))
    if encryption == "luks" and not source_supports_luks_persistence(source, mode):
        raise RuntimeError(_("Encrypted session storage is not supported by this MiniOS image. Choose another session storage mode."))



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
    _validate_persistence_settings(state, src)
    session_command = preflight_session_storage(state, require_password=not dry_run)
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
        )
        _raise_if_canceled(state)
        create_live_session(state, root_mount, progress_cb, log_cb,
                            command=session_command)
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
