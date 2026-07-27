#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import glob
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Callable, List, Optional, Set

from bundle_source import BundleOverlay, preflight_selected_bundles
from disk_utils import get_live_source_mount, resolve_install_device
from install_state import InstallCanceled, InstallState
from kernel_metadata import KERNEL_METADATA_PATH, restore_kernel_dpkg_metadata
from manual_executor import execute_manual_plan, required_manual_tools
from manual_partitioning import ManualPlanError, ManualPlanner
from mount_utils import mount_partition, unmount_mountpoints, unmount_partitions
from network_config import write_network_profile
from package_preflight import (
    manual_native_package_requirements,
    native_missing_packages,
    native_requires_standard_bootloader,
    package_cache_summary,
    prepare_package_cache,
    preflight_ok,
    preflight_package_download,
)
from partition_executor import execute_plan
from partition_planner import build_plan
from module_selection import required_root_mib
from partition_scanner import scan_disk
from user_config_writer import hash_system_password, normalize_default_target, process_services_field
from minios_security.security_profiles import apply_security_profile, filter_groups_for_profile, merge_service_lists


gettext.bindtextdomain("minios-installer", "/usr/share/locale")
gettext.textdomain("minios-installer")
_ = gettext.gettext


NATIVE_COPY_EXCLUDES = (
    "./dev",
    "./proc",
    "./sys",
    "./run",
    "./tmp",
    "./mnt",
    "./media",
    "./lost+found",
)

NATIVE_LIVE_ONLY_PACKAGES = (
    "live-config",
    "live-boot",
    "live-tools",
    "minios-live-config",
    "minios-live-config-systemd",
    "minios-live-config-sysvinit",
    "minios-live-config-doc",
    "user-setup",
)

NATIVE_REMOVED_APPLICATION_PACKAGES = (
    "minios-configurator",
    "minios-installer",
    "minios-kernel-manager",
    "minios-session-manager",
    "minios-store-gui",
    "minios-welcome",
)

NATIVE_LIVE_ONLY_ARTIFACTS = (
    "usr/bin/audio-allowuser.sh",
    "usr/lib/systemd/system/audio-allowuser.service",
    "lib/systemd/system/audio-allowuser.service",
)

NATIVE_DEFAULT_USER_GROUPS = (
    "adm",
    "sudo",
    "dialout",
    "cdrom",
    "floppy",
    "audio",
    "video",
    "plugdev",
    "users",
    "fuse",
    "netdev",
    "powerdev",
    "scanner",
    "bluetooth",
    "weston-launch",
    "kvm",
    "libvirt",
    "libvirt-qemu",
    "vboxusers",
    "vboxsf",
    "lpadmin",
    "dip",
    "sambashare",
    "docker",
    "wireshark",
)


def _raise_if_canceled(state: InstallState) -> None:
    if state.cancel_requested:
        raise InstallCanceled(_("Installation canceled by user."))


def _logged_command(cmd) -> str:
    displayed = [str(part) for part in cmd]
    account_tools = {"useradd", "usermod"}
    tool_index = next(
        (index for index, part in enumerate(displayed) if os.path.basename(part) in account_tools),
        None,
    )
    if tool_index is not None:
        index = tool_index + 1
        while index < len(displayed):
            if displayed[index] in ("-p", "--password") and index + 1 < len(displayed):
                displayed[index + 1] = "<redacted>"
                index += 2
                continue
            if displayed[index].startswith("--password="):
                displayed[index] = "--password=<redacted>"
            index += 1
    return "$ " + " ".join(shlex.quote(part) for part in displayed)


def _run(cmd, log_cb: Callable[[str], None], dry_run: bool = False, **kwargs) -> None:
    log_cb(_logged_command(cmd))
    if dry_run:
        return
    subprocess.run(cmd, check=True, **kwargs)


def _estimate_native_copy_bytes(source: str) -> int:
    total = 0
    exclude_names = {item[2:] for item in NATIVE_COPY_EXCLUDES if item.startswith("./")}
    for dirpath, dirnames, filenames in os.walk(source):
        if dirpath == source:
            dirnames[:] = [name for name in dirnames if name not in exclude_names]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            total += max(st.st_size, 512)
    return max(total, 1)


def _copy_native_root(
    source: str,
    target: str,
    progress_cb: Callable[[int, str], None],
    log_cb: Callable[[str], None],
    dry_run: bool = False,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> None:
    progress_cb(24, _("Preparing file copy..."))
    excludes = []
    for item in NATIVE_COPY_EXCLUDES:
        excludes.extend(["--exclude", item])
    cmd = [
        "tar",
        "--acls",
        "--one-file-system",
        *excludes,
        "-C",
        source,
        "-cpf",
        "-",
        ".",
    ]
    extract = ["tar", "--acls", "-C", target, "-xpf", "-"]
    log_cb(
        "$ "
        + " ".join(shlex.quote(str(part)) for part in cmd)
        + " | "
        + " ".join(shlex.quote(str(part)) for part in extract)
    )
    if dry_run:
        return
    total_bytes = _estimate_native_copy_bytes(source)
    copied_bytes = 0
    last_percent = -5
    started_at = time.monotonic()
    last_reported_at = started_at
    producer = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    consumer = subprocess.Popen(extract, stdin=subprocess.PIPE)
    try:
        if not producer.stdout or not consumer.stdin:
            raise RuntimeError(_("Failed to start native file copy."))
        while True:
            if cancel_cb and cancel_cb():
                raise InstallCanceled(_("Installation canceled by user."))
            chunk = producer.stdout.read(1024 * 1024)
            if not chunk:
                break
            consumer.stdin.write(chunk)
            copied_bytes += len(chunk)
            # The tar stream includes headers, padding, and xattrs absent from
            # the file-size estimate. Reserve 100% for successful completion.
            copy_percent = min(99, int(copied_bytes * 100 / total_bytes))
            copied_mib = copied_bytes // (1024 * 1024)
            now = time.monotonic()
            if last_percent < 0 or copy_percent >= last_percent + 5 or now - last_reported_at >= 5:
                elapsed = max(now - started_at, 0.001)
                rate_mib = copied_mib / elapsed
                overall = 24 + int(copy_percent * 58 / 100)
                progress_cb(
                    overall,
                    _("Copying system files... {percent}% ({copied} MiB, {rate:.1f} MiB/s)").format(
                        percent=copy_percent,
                        copied=copied_mib,
                        rate=rate_mib,
                    ),
                )
                last_percent = copy_percent
                last_reported_at = now
        consumer.stdin.close()
        rc = producer.wait()
        extract_rc = consumer.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
        if extract_rc != 0:
            raise subprocess.CalledProcessError(extract_rc, extract)
        elapsed = max(time.monotonic() - started_at, 0.001)
        progress_cb(
            82,
            _("System files copied. 100% ({copied} MiB in {seconds:.1f}s).").format(
                copied=copied_bytes // (1024 * 1024),
                seconds=elapsed,
            ),
        )
    finally:
        if producer.poll() is None:
            producer.kill()
        if consumer.poll() is None:
            consumer.kill()


def _prepare_runtime_dirs(target: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    for path in ("dev", "proc", "sys", "run", "tmp", "mnt", "media"):
        os.makedirs(os.path.join(target, path), exist_ok=True)
    os.chmod(os.path.join(target, "tmp"), 0o1777)


def _patch_sysv_quiet_wrapper(target: str, dry_run: bool, log_cb: Callable[[str], None]) -> None:
    path = os.path.join(target, "usr", "sbin", "minios-sysv-rc")
    if not os.path.exists(path):
        return
    log_cb(_("Patching sysvinit quiet logger for native boot..."))
    if dry_run:
        return
    content = """#!/bin/sh

if grep -qw quiet /proc/cmdline 2>/dev/null && ! grep -qw debug /proc/cmdline 2>/dev/null; then
    LOG=/var/log/sysvinit-quiet.log
    if ! { [ -d /var/log ] && : >>\"$LOG\"; } 2>/dev/null; then
        LOG=/run/sysvinit-quiet.log
        mkdir -p /run 2>/dev/null || true
    fi
    exec /etc/init.d/rc \"$@\" >>\"$LOG\" 2>&1
fi

exec /etc/init.d/rc \"$@\"
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, 0o755)


def _blkid_value(path: str, tag: str) -> str:
    out = subprocess.check_output(["blkid", "-s", tag, "-o", "value", path], universal_newlines=True)
    return out.strip()


def _write_fstab(target: str, root_part: str, esp_part: Optional[str], swap_part: Optional[str], dry_run: bool, log_cb: Callable[[str], None]) -> None:
    # Compatibility wrapper for the automatic tuple executor.
    entries = [(root_part, "/", None, "root")]
    if esp_part:
        entries.append((esp_part, "/boot/efi", "vfat", "esp"))
    for part in ([swap_part] if swap_part else []):
        entries.append((part, "", "swap", "swap"))
    _write_assignment_fstab(target, entries, dry_run, log_cb)


def _write_assignment_fstab(target: str, entries, dry_run: bool, log_cb: Callable[[str], None]) -> None:
    log_cb(_("Writing /etc/fstab..."))
    if dry_run:
        return
    filesystems = [entry for entry in entries if entry[3] != "swap"]
    swaps = [entry for entry in entries if entry[3] == "swap"]
    lines = []
    for part, mountpoint, requested_type, role in filesystems:
        fstype = _blkid_value(part, "TYPE") or requested_type or "ext4"
        options = "umask=0077" if role == "esp" else "defaults,noatime"
        passno = "1" if mountpoint == "/" else "2"
        lines.append("UUID={0} {1} {2} {3} 0 {4}\n".format(
            _blkid_value(part, "UUID"), mountpoint, fstype, options, passno))
    lines.append("tmpfs /tmp tmpfs defaults,nosuid,nodev 0 0\n")
    for part, _mountpoint, _requested_type, _role in swaps:
        lines.append("UUID={0} none swap sw 0 0\n".format(_blkid_value(part, "UUID")))
    etc_dir = os.path.join(target, "etc")
    os.makedirs(etc_dir, exist_ok=True)
    with open(os.path.join(etc_dir, "fstab"), "w", encoding="utf-8") as fh:
        fh.writelines(lines)


def _mount_chroot_api(target: str, esp_mount: Optional[str], dry_run: bool, log_cb: Callable[[str], None]) -> None:
    if dry_run:
        return
    for src, dst in (("/dev", "dev"), ("/proc", "proc"), ("/sys", "sys"), ("/run", "run")):
        mountpoint = os.path.join(target, dst)
        os.makedirs(mountpoint, exist_ok=True)
        _run(["mount", "--bind", src, mountpoint], log_cb)
    dev_pts = os.path.join(target, "dev", "pts")
    os.makedirs(dev_pts, exist_ok=True)
    _run(["mount", "--bind", "/dev/pts", dev_pts], log_cb)
    if esp_mount:
        target_esp = os.path.join(target, "boot", "efi")
        os.makedirs(target_esp, exist_ok=True)
        _run(["mount", "--bind", esp_mount, target_esp], log_cb)


def _unmount_chroot_api(target: str, esp_mount: Optional[str], log_cb: Callable[[str], None]) -> None:
    for dst in (["boot/efi"] if esp_mount else []):
        subprocess.run(["umount", os.path.join(target, dst)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for dst in ("dev/pts", "run", "sys", "proc", "dev"):
        subprocess.run(["umount", os.path.join(target, dst)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _kernel_version(target: str) -> str:
    def version_key(value: str):
        return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]

    for base in ("lib/modules", "usr/lib/modules"):
        modules_dir = os.path.join(target, base)
        if os.path.isdir(modules_dir):
            versions = sorted(
                (name for name in os.listdir(modules_dir) if os.path.isdir(os.path.join(modules_dir, name))),
                key=version_key,
            )
            if versions:
                return versions[-1]
    raise RuntimeError(_("No kernel modules found in installed system; native boot cannot be configured."))


def _copy_native_kernel(target: str, version: str, dry_run: bool, log_cb: Callable[[str], None]) -> str:
    boot_dir = os.path.join(target, "boot")
    kernel_name = f"vmlinuz-{version}"
    dest = os.path.join(boot_dir, kernel_name)
    source_boot = os.path.join(get_live_source_mount(), "minios", "boot")
    candidates = [
        os.path.join(source_boot, kernel_name),
        os.path.join(source_boot, "vmlinuz"),
    ]
    source = next((path for path in candidates if os.path.exists(path)), None)
    if not source:
        raise RuntimeError(_("No live kernel image found for native install."))
    log_cb(_("Copying kernel {kernel}...").format(kernel=os.path.basename(source)))
    if not dry_run:
        os.makedirs(boot_dir, exist_ok=True)
        shutil.copy2(source, dest)
    return f"/boot/{kernel_name}"


def _generate_native_initramfs(target: str, version: str, dry_run: bool, log_cb: Callable[[str], None]) -> str:
    initrd = f"/boot/initrd.img-{version}"
    if _target_has_executable(target, "/usr/bin/dracut", "/usr/sbin/dracut"):
        _chroot(target, ["dracut", "--force", initrd, version], log_cb, dry_run=dry_run)
        return initrd
    if _target_has_executable(target, "/usr/sbin/update-initramfs", "/usr/bin/update-initramfs"):
        _chroot(target, ["update-initramfs", "-c", "-k", version], log_cb, dry_run=dry_run)
        return initrd
    raise RuntimeError(_("No supported initramfs generator is available in the installed system."))


def _partition_index_from_path(path: str) -> Optional[int]:
    name = os.path.basename(path)
    match = re.search(r"-part(\d+)$", name) or re.search(r"(?:p)?(\d+)$", name)
    return int(match.group(1)) if match else None


def _install_extlinux_native(target: str, disk: str, root_part: str, root_uuid: str, kernel: str, initrd: str, log_cb: Callable[[str], None], dry_run: bool) -> None:
    source_dir = os.path.join(get_live_source_mount(), "minios", "boot", "syslinux")
    if not os.path.isdir(source_dir):
        raise RuntimeError(_("SYSLINUX boot files not found on live media."))
    boot_dir = os.path.join(target, "boot", "extlinux")
    extlinux = "extlinux.x64" if os.uname().machine == "x86_64" else "extlinux.x32"
    extlinux_path = os.path.join(boot_dir, extlinux)
    if dry_run:
        log_cb(_("Would install EXTLINUX native bootloader."))
        return
    os.makedirs(boot_dir, exist_ok=True)
    for name in (extlinux, "mbr.bin", "ldlinux.c32", "libcom32.c32", "libutil.c32", "menu.c32"):
        src = os.path.join(source_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(boot_dir, name))
    if not os.path.exists(extlinux_path):
        raise RuntimeError(_("EXTLINUX installer not found: {path}").format(path=extlinux_path))
    os.chmod(extlinux_path, 0o755)
    config = (
        "DEFAULT minios\n"
        "PROMPT 0\n"
        "TIMEOUT 30\n"
        "LABEL minios\n"
        "  MENU LABEL MiniOS\n"
        f"  LINUX {kernel}\n"
        f"  INITRD {initrd}\n"
        f"  APPEND root=UUID={root_uuid} rw quiet\n"
    )
    with open(os.path.join(boot_dir, "extlinux.conf"), "w", encoding="utf-8") as fh:
        fh.write(config)
    _run([extlinux_path, "--install", boot_dir], log_cb, dry_run=False)
    mbr = os.path.join(boot_dir, "mbr.bin")
    if not os.path.exists(mbr):
        raise RuntimeError(_("SYSLINUX MBR image not found: {path}").format(path=mbr))
    _run(["dd", "bs=440", "count=1", "conv=notrunc", f"if={mbr}", f"of={disk}"], log_cb, dry_run=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    index = _partition_index_from_path(root_part)
    if index is not None:
        _run(["parted", "-s", disk, "set", str(index), "boot", "on"], log_cb, dry_run=False)


def _target_has_executable(target: str, *paths: str) -> bool:
    return any(os.path.exists(os.path.join(target, path.lstrip("/"))) for path in paths)


def _target_service_action(target: str, action: str, name: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    """Apply a target service action with legacy fallbacks.

    New images provide minios-svc. Older MiniOS targets may only have systemctl
    or sysvinit tools, so service actions are best-effort and must not abort a
    native install when the abstraction is unavailable.
    """
    if not name:
        return
    if _target_has_executable(target, "/usr/sbin/minios-svc", "/usr/bin/minios-svc", "/sbin/minios-svc"):
        _chroot(target, ["minios-svc", action, name], log_cb, dry_run=dry_run, check=False)
        return
    if _target_has_executable(target, "/bin/systemctl", "/usr/bin/systemctl"):
        if action == "default":
            _chroot(target, ["systemctl", "set-default", name], log_cb, dry_run=dry_run, check=False)
        elif action in ("enable", "disable"):
            unit = name if name.endswith(".service") else name + ".service"
            _chroot(target, ["systemctl", action, unit], log_cb, dry_run=dry_run, check=False)
        return
    if _target_has_executable(target, "/usr/sbin/update-rc.d", "/sbin/update-rc.d") and action in ("enable", "disable"):
        service = name[:-8] if name.endswith(".service") else name
        if action == "enable":
            _chroot(target, ["update-rc.d", service, "defaults"], log_cb, dry_run=dry_run, check=False)
        else:
            _chroot(target, ["update-rc.d", service, "disable"], log_cb, dry_run=dry_run, check=False)
        return
    log_cb(_("Service action skipped for legacy target without service tools: {action} {name}").format(action=action, name=name))


def _normalize_locale_list(value: str) -> List[str]:
    locales = []
    for item in re.split(r"[,\s]+", value or ""):
        item = item.strip()
        if item and item not in locales:
            locales.append(item)
    return locales


def _locale_gen_entry(locale_name: str) -> str:
    if "." in locale_name:
        suffix = locale_name.split(".", 1)[1].split("@", 1)[0]
        if suffix.lower() in ("utf-8", "utf8"):
            return "{locale} UTF-8".format(locale=locale_name)
        return "{locale} {charset}".format(locale=locale_name, charset=suffix)
    return "{locale} UTF-8".format(locale=locale_name)


def _language_for_locale(locale_name: str) -> str:
    base = (locale_name or "").split(".", 1)[0].split("@", 1)[0]
    lang = base.split("_", 1)[0].strip()
    if not lang or lang in ("C", "POSIX", "en"):
        return ""
    return lang


def _enable_locales_in_locale_gen(target: str, locales: List[str], dry_run: bool = False) -> None:
    if not locales or dry_run:
        return
    path = os.path.join(target, "etc", "locale.gen")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError:
        lines = []

    entries = {locale_name: _locale_gen_entry(locale_name) for locale_name in locales}
    remaining = set(entries)
    result = []
    for line in lines:
        stripped = line.strip()
        uncommented = stripped[1:].strip() if stripped.startswith("#") else stripped
        parts = uncommented.split()
        if parts and parts[0] in entries:
            result.append(entries[parts[0]] + "\n")
            remaining.discard(parts[0])
        else:
            result.append(line)

    if remaining:
        if result and result[-1] and not result[-1].endswith("\n"):
            result[-1] += "\n"
        for locale_name in locales:
            if locale_name in remaining:
                result.append(entries[locale_name] + "\n")
    _write_text(path, "".join(result), dry_run=False)


def _apply_native_locale(target: str, locale_value: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    locales = _normalize_locale_list(locale_value)
    if not locales:
        return
    first_locale = locales[0]
    language = _language_for_locale(first_locale)
    locale_lines = ["LANG={locale}\n".format(locale=first_locale)]
    if language:
        locale_lines.append("LANGUAGE={language}:en\n".format(language=language))
    _write_text(os.path.join(target, "etc", "default", "locale"), "".join(locale_lines), dry_run=dry_run)
    _enable_locales_in_locale_gen(target, locales, dry_run=dry_run)

    if _target_has_executable(target, "/usr/sbin/locale-gen", "/usr/bin/locale-gen"):
        rc = _chroot_returncode(target, ["locale-gen"], log_cb, dry_run=dry_run)
        if rc != 0:
            raise RuntimeError(_("Locale generation failed for the requested locale configuration."))
    if _target_has_executable(target, "/usr/sbin/update-locale", "/usr/bin/update-locale"):
        args = ["update-locale", "LANG={locale}".format(locale=first_locale)]
        if language:
            args.append("LANGUAGE={language}:en".format(language=language))
        rc = _chroot_returncode(target, args, log_cb, dry_run=dry_run)
        if rc != 0:
            log_cb(_("Warning: update-locale failed; /etc/default/locale was written directly."))


def _grub_config_command(target: str):
    if _target_has_executable(target, "/usr/sbin/update-grub", "/usr/bin/update-grub"):
        return ["update-grub"]
    if _target_has_executable(target, "/usr/sbin/grub-mkconfig", "/usr/bin/grub-mkconfig"):
        return ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]
    return None


def _can_install_grub_native(target: str, use_efi: bool) -> bool:
    if not _target_has_executable(target, "/usr/sbin/grub-install", "/usr/bin/grub-install"):
        return False
    if not _grub_config_command(target):
        return False
    if use_efi:
        return _target_has_executable(target, "/usr/lib/grub/x86_64-efi/modinfo.sh")
    return _target_has_executable(target, "/usr/lib/grub/i386-pc/modinfo.sh")


def _install_grub_native(target: str, disk: str, use_efi: bool, progress_cb: Callable[[int, str], None], log_cb: Callable[[str], None], dry_run: bool) -> None:
    progress_cb(92, _("Installing GRUB bootloader..."))
    if use_efi:
        if os.uname().machine != "x86_64":
            raise RuntimeError(_("Native UEFI GRUB installation is currently implemented only for x86_64."))
        install_cmd = [
            "grub-install",
            "--target=x86_64-efi",
            "--efi-directory=/boot/efi",
            "--bootloader-id=MiniOS",
            "--removable",
            "--no-nvram",
            "--recheck",
        ]
    else:
        install_cmd = ["grub-install", "--target=i386-pc", "--recheck", disk]
    _chroot(target, install_cmd, log_cb, dry_run=dry_run)
    _write_text(
        os.path.join(target, "etc", "default", "grub.d", "minios-native.cfg"),
        'GRUB_CMDLINE_LINUX="rw"\n'
        + ('GRUB_DISABLE_OS_PROBER=false\n' if _target_has_executable(target, "/usr/bin/os-prober", "/usr/sbin/os-prober") else ''),
        dry_run=dry_run,
    )
    config_cmd = _grub_config_command(target)
    if not config_cmd:
        raise RuntimeError(_("GRUB configuration generator not found in installed system."))
    if _target_has_executable(target, "/usr/bin/os-prober", "/usr/sbin/os-prober"):
        probe = _chroot_capture(target, ["os-prober"], log_cb, dry_run=dry_run)
        if probe is not None and probe.returncode != 0:
            raise RuntimeError(_("os-prober failed; refusing to create an incomplete multiboot configuration."))
        if probe is not None and probe.stdout.strip():
            log_cb(_("Detected other installed systems:"))
            for line in probe.stdout.splitlines():
                log_cb("  " + line)
    _chroot(target, config_cmd, log_cb, dry_run=dry_run)
    grub_cfg = os.path.join(target, "boot", "grub", "grub.cfg")
    if not dry_run and (not os.path.isfile(grub_cfg) or os.path.getsize(grub_cfg) == 0):
        raise RuntimeError(_("GRUB configuration was not created."))
    if _target_has_executable(target, "/usr/bin/grub-script-check", "/usr/sbin/grub-script-check"):
        _chroot(target, ["grub-script-check", "/boot/grub/grub.cfg"], log_cb, dry_run=dry_run)


def _install_native_bootloader(target: str, disk: str, root_part: str, use_efi: bool, esp_mount: Optional[str], progress_cb: Callable[[int, str], None], log_cb: Callable[[str], None], dry_run: bool = False, esp_already_mounted: bool = False, require_grub: bool = False) -> None:
    progress_cb(92, _("Installing native bootloader..."))
    _mount_chroot_api(target, None if esp_already_mounted else esp_mount, dry_run, log_cb)
    try:
        version = _kernel_version(target)
        kernel = _copy_native_kernel(target, version, dry_run, log_cb)
        initrd = _generate_native_initramfs(target, version, dry_run, log_cb)
        if _can_install_grub_native(target, use_efi):
            _install_grub_native(target, disk, use_efi, progress_cb, log_cb, dry_run)
        elif use_efi:
            raise RuntimeError(
                _("Native UEFI install requires GRUB EFI packages. Enable package download or install grub-efi-amd64 and efibootmgr.")
            )
        elif require_grub:
            raise RuntimeError(
                _("Native installation requires GRUB packages in the installed target. Enable package download before modifying the disk.")
            )
        else:
            log_cb(_("GRUB is not available; using offline EXTLINUX fallback."))
            root_uuid = _blkid_value(root_part, "UUID") if not dry_run else "DRY-RUN-UUID"
            _install_extlinux_native(target, disk, root_part, root_uuid, kernel, initrd, log_cb, dry_run)
    finally:
        _unmount_chroot_api(target, None if esp_already_mounted else esp_mount, log_cb)


def _manual_assignment_entries(result):
    return [(target.path, assignment.mountpoint, assignment.fstype, assignment.role)
            for target, assignment in zip(result.targets, result.assignments)]


def _preflight_manual_native(plan) -> None:
    """Reject unsupported mount/filesystem work before the executor changes disks."""
    supported = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "ntfs", "fat", "fat16", "fat32", "vfat", "swap"}
    if not plan.use_efi and plan.snapshot.partition_table == "gpt":
        raise ManualPlanError("BIOS manual installation on GPT is unsupported because MiniOS does not provide bios_grub support")
    required = required_manual_tools(plan)
    required.update(("mount", "umount", "blkid"))
    for assignment in plan.assignments:
        fstype = assignment.fstype or getattr(assignment.target, "fstype", "")
        if fstype not in supported:
            raise ManualPlanError("manual native deployment does not support filesystem: {0}".format(fstype))
    missing = sorted(command for command in required if not shutil.which(command))
    if missing:
        raise RuntimeError(_("Manual native deployment requires unavailable tools before disk changes: {tools}").format(tools=", ".join(missing)))


def _log_package_cache(cache, required, log_cb):
    count, size_bytes = package_cache_summary(cache)
    log_cb(
        _("APT cache prepared: {count} packages, {size:.1f} MiB. Required target packages: {required}").format(
            count=count,
            size=size_bytes / (1024 * 1024),
            required=", ".join(required),
        )
    )


def _refresh_manual_preflight(state, plan, dry_run=False, log_cb=None):
    """Repeat capacity and boot-package checks immediately before manual writes."""
    bundle_bytes = preflight_selected_bundles(selected_modules=state.selected_modules)
    required_sectors = (required_root_mib(bundle_bytes) * 1024 * 1024 +
                        plan.snapshot.sector_size - 1) // plan.snapshot.sector_size
    if required_sectors > plan.required_root_sectors:
        plan = plan._replace(required_root_sectors=required_sectors)
    ManualPlanner().validate(plan, install_mode="native")
    root = next(item for item in plan.assignments if item.role == "root")
    root_fstype = root.fstype or getattr(root.target, "fstype", "")
    # Manual targets are assembled only after this point. Host package state
    # cannot prove the selected bundle contains GRUB, its platform modules, or
    # os-prober, so stage the complete standard target closure before writes.
    required = manual_native_package_requirements(
        plan.use_efi, root_fstype, alongside=state.placement != "erase_all"
    )
    if not state.download_missing_packages:
        raise RuntimeError(_("This manual installation cannot prove its target boot packages without package download before the disk can be modified."))
    if not dry_run:
        result = preflight_package_download(required)
        if not preflight_ok(result):
            raise RuntimeError(_("Required manual-install boot packages cannot be downloaded before disk modification."))
        try:
            state.package_cache_path = prepare_package_cache(required)
            if log_cb:
                _log_package_cache(state.package_cache_path, required, log_cb)
        except Exception as exc:
            raise RuntimeError(
                _("Required manual-install boot packages could not be staged before disk modification: {error}").format(error=exc)
            )
    return plan


def _mount_manual_targets(result):
    entries = _manual_assignment_entries(result)
    root = next(entry for entry in entries if entry[3] == "root")
    mounted = []
    workdir = tempfile.mkdtemp(prefix="minios-installer-native-")
    try:
        root_target = os.path.join(workdir, "root")
        mount_partition(root[0], root_target, root[2])
        mounted.append(root_target)
        children = sorted((entry for entry in entries if entry[3] not in ("root", "swap")),
                          key=lambda entry: (entry[1].count("/"), entry[1]))
        for part, mountpoint, fstype, _role in children:
            mount_target = os.path.join(root_target, mountpoint.lstrip("/"))
            mount_partition(part, mount_target, fstype)
            mounted.append(mount_target)
        return root_target, workdir, mounted, entries
    except Exception:
        try:
            unmount_mountpoints(mounted)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        raise


def _run_manual_native_install(state, progress_cb, log_cb, dry_run=False):
    plan = state.manual_partition_plan
    if plan is None:
        raise RuntimeError(_("No validated manual partitioning plan selected."))
    # Revalidate the immutable plan itself before any preflight or disk write.
    # The attribute guard preserves injectable opaque test doubles; production
    # state receives only ManualPartitionPlan instances from the GUI/controller.
    if hasattr(plan, "snapshot"):
        ManualPlanner().validate(plan, install_mode="native")
    # The resolved by-id path is the only path the executor may mutate.
    if hasattr(plan, "snapshot"):
        resolved = resolve_install_device(plan.snapshot.device, state.target_device_identity)
        plan = plan._replace(snapshot=plan.snapshot._replace(device=resolved))
    _preflight_manual_native(plan)
    if hasattr(plan, "snapshot"):
        plan = _refresh_manual_preflight(state, plan, dry_run=dry_run, log_cb=log_cb)
        state.manual_partition_plan = plan
    success = False
    root_target = workdir = None
    mounted = []
    try:
        progress_cb(0, _("Preparing target disk..."))
        result = execute_manual_plan(
            plan, log_cb, scan_disk, dry_run=dry_run,
            cancel_cb=lambda: state.cancel_requested,
        )
        if dry_run:
            progress_cb(100, _("Dry run complete."))
            return
        _raise_if_canceled(state)
        root_target, workdir, mounted, entries = _mount_manual_targets(result)
        root_entry = next(entry for entry in entries if entry[3] == "root")
        esp_entry = next((entry for entry in entries if entry[3] == "esp"), None)
        # All child filesystems are mounted before extraction so their source
        # directories are populated on their own filesystems, not the root.
        with BundleOverlay(log_cb=log_cb, selected_modules=state.selected_modules) as source_root:
            _copy_native_root(source_root, root_target, progress_cb, log_cb,
                              cancel_cb=lambda: state.cancel_requested)
        _raise_if_canceled(state)
        progress_cb(82, _("Configuring installed system..."))
        _prepare_runtime_dirs(root_target)
        _patch_sysv_quiet_wrapper(root_target, False, log_cb)
        _maybe_restore_kernel_metadata(root_target, state, log_cb)
        _mount_chroot_api(root_target, None, False, log_cb)
        try:
            _install_native_packages(root_target, plan.use_efi, root_entry[2], state, log_cb)
            extra_user_groups = _collect_live_allowuser_groups(root_target)
            apply_security_profile(root_target, state.security_profile, log_cb, runtime_mode="native")
            _write_assignment_fstab(root_target, entries, False, log_cb)
            _apply_native_settings(root_target, state, log_cb, extra_user_groups=extra_user_groups)
            _cleanup_native_live_packages(root_target, log_cb)
            _generate_ssh_host_keys(root_target, log_cb)
            _generate_ssl_snakeoil_cert(root_target, log_cb)
        finally:
            _unmount_chroot_api(root_target, None, log_cb)
        _raise_if_canceled(state)
        _install_native_bootloader(root_target, state.target_device, root_entry[0], plan.use_efi,
                                    esp_entry[0] if esp_entry else None, progress_cb, log_cb,
                                   esp_already_mounted=bool(esp_entry), require_grub=True)
        progress_cb(99, _("Unmounting disk..."))
        unmount_mountpoints(mounted)
        mounted = []
        shutil.rmtree(workdir, ignore_errors=True)
        workdir = None
        progress_cb(100, _("Installation complete!"))
        success = True
    finally:
        if not success and mounted:
            try:
                unmount_mountpoints(mounted)
                log_cb(_("Cleaned up target mounts."))
            except Exception as exc:
                log_cb(_("Warning: failed to unmount target: {error}").format(error=exc))
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        if state.package_cache_path:
            shutil.rmtree(state.package_cache_path, ignore_errors=True)
            state.package_cache_path = None


def _write_text(path: str, content: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _chroot(target: str, args, log_cb: Callable[[str], None], dry_run: bool = False, input_text: Optional[str] = None, check: bool = True) -> None:
    cmd = ["chroot", target] + list(args)
    log_cb(_logged_command(cmd))
    if dry_run:
        return
    result = subprocess.run(
        cmd,
        input=input_text,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.stdout:
        for line in result.stdout.splitlines():
            log_cb(line)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout)
    return result


def _chroot_capture(target: str, args, log_cb: Callable[[str], None], dry_run: bool = False):
    cmd = ["chroot", target] + list(args)
    log_cb(_logged_command(cmd))
    if dry_run:
        return None
    return subprocess.run(cmd, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)


def _chroot_returncode(target: str, args, log_cb: Callable[[str], None], dry_run: bool = False, input_text: Optional[str] = None) -> int:
    cmd = ["chroot", target] + list(args)
    log_cb(_logged_command(cmd))
    if dry_run:
        return 0
    result = subprocess.run(cmd, input=input_text, universal_newlines=True, check=False)
    return int(result.returncode or 0)


def _installed_target_packages(target: str, packages) -> List[str]:
    status_path = os.path.join(target, "var", "lib", "dpkg", "status")
    installed = set()
    try:
        with open(status_path, "r", encoding="utf-8", errors="ignore") as fh:
            name = None
            ok = False
            for line in fh:
                if line.startswith("Package:"):
                    name = line.split(":", 1)[1].strip()
                    ok = False
                elif line.startswith("Status:"):
                    ok = line.strip() == "Status: install ok installed"
                elif not line.strip():
                    if name and ok:
                        installed.add(name)
                    name = None
                    ok = False
            if name and ok:
                installed.add(name)
    except OSError:
        return []
    return [pkg for pkg in packages if pkg in installed]


def _remove_target_path(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _remove_native_live_artifacts(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    patterns = list(NATIVE_LIVE_ONLY_ARTIFACTS)
    patterns.extend(
        [
            "etc/systemd/system/**/*.wants/live-config.service",
            "etc/systemd/system/**/*.requires/live-config.service",
            "etc/systemd/system/**/*.wants/*allowuser.service",
            "etc/systemd/system/**/*.requires/*allowuser.service",
            "etc/rc*.d/*audio-allowuser",
            "etc/rc*.d/**/*allowuser",
            "usr/bin/*-allowuser.sh",
            "usr/lib/systemd/system/*-allowuser.service",
            "lib/systemd/system/*-allowuser.service",
        ]
    )
    removed = []
    for pattern in patterns:
        for path in glob.glob(os.path.join(target, pattern), recursive=True):
            rel = os.path.relpath(path, target)
            if rel in removed:
                continue
            removed.append(rel)
            log_cb(_("Removing live-only artifact: {path}").format(path="/" + rel))
            if not dry_run:
                _remove_target_path(path)


def _collect_live_allowuser_groups(target: str) -> List[str]:
    groups = []
    seen = set()
    for pattern in ("usr/bin/*-allowuser.sh", "usr/sbin/*-allowuser.sh"):
        for path in glob.glob(os.path.join(target, pattern)):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            for match in re.finditer(r"\busermod\s+-a\s+-G\s+([A-Za-z0-9_.-]+)", content):
                group = match.group(1)
                if group not in seen:
                    seen.add(group)
                    groups.append(group)
    return groups


def _cleanup_native_live_packages(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    packages = _installed_target_packages(
        target,
        NATIVE_LIVE_ONLY_PACKAGES + NATIVE_REMOVED_APPLICATION_PACKAGES,
    )
    if packages:
        log_cb(_("Removing live-only packages: {packages}").format(packages=", ".join(packages)))
        env = ["env", "DEBIAN_FRONTEND=noninteractive", "APT_LISTCHANGES_FRONTEND=none"]
        if _target_has_executable(target, "/usr/bin/apt-get", "/bin/apt-get"):
            rc = _chroot_returncode(
                target,
                env + ["apt-get", "purge", "-y", "--allow-remove-essential"] + packages,
                log_cb,
                dry_run=dry_run,
            )
            if rc == 0:
                _chroot_returncode(
                    target,
                    env + ["apt-get", "autoremove", "--purge", "-y"],
                    log_cb,
                    dry_run=dry_run,
                )
        else:
            rc = 1
            log_cb(_("Warning: apt-get not found in target; falling back to dpkg cleanup."))
        if rc != 0:
            log_cb(_("Warning: apt purge failed; trying forced dpkg remove for live-only packages."))
            rc = _chroot_returncode(target, env + ["dpkg", "--remove", "--force-depends"] + packages, log_cb, dry_run=dry_run)
        if rc != 0:
            log_cb(_("Warning: live-only package removal failed; continuing with artifact cleanup."))
    else:
        log_cb(_("No live-only packages need removal."))
    _remove_native_live_artifacts(target, log_cb, dry_run=dry_run)


def _generate_ssh_host_keys(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    if not _target_has_executable(target, "/usr/sbin/sshd") or not _target_has_executable(target, "/usr/bin/ssh-keygen"):
        return
    if glob.glob(os.path.join(target, "etc", "ssh", "ssh_host_*_key")):
        return
    log_cb(_("Generating SSH host keys..."))
    _chroot(target, ["ssh-keygen", "-A"], log_cb, dry_run=dry_run)


def _generate_ssl_snakeoil_cert(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    cert = os.path.join(target, "etc", "ssl", "certs", "ssl-cert-snakeoil.pem")
    key = os.path.join(target, "etc", "ssl", "private", "ssl-cert-snakeoil.key")
    if os.path.exists(cert) or os.path.exists(key):
        return
    if not _target_has_executable(target, "/usr/sbin/make-ssl-cert", "/usr/bin/make-ssl-cert"):
        return
    log_cb(_("Generating default SSL snakeoil certificate..."))
    _chroot(target, ["make-ssl-cert", "generate-default-snakeoil", "--force-overwrite"], log_cb, dry_run=dry_run)


def _install_native_packages(target: str, use_efi: bool, filesystem: str, state: InstallState, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    missing = native_missing_packages(use_efi, filesystem, root=target, alongside=state.placement != "erase_all")
    if not missing:
        log_cb(_("No additional native packages are required."))
        return
    if not state.download_missing_packages:
        log_cb(
            _("Skipping online package installation; offline boot fallback will be used. Missing packages: {packages}").format(
                packages=", ".join(missing)
            )
        )
        return

    if state.package_cache_path and not dry_run:
        target_archives = os.path.join(target, "var", "cache", "apt", "archives")
        os.makedirs(target_archives, exist_ok=True)
        for package in glob.glob(os.path.join(state.package_cache_path, "archives", "*.deb")):
            shutil.copy2(package, target_archives)
        source_lists = os.path.join(state.package_cache_path, "lists")
        target_lists = os.path.join(target, "var", "lib", "apt", "lists")
        if os.path.isdir(source_lists):
            shutil.rmtree(target_lists, ignore_errors=True)
            shutil.copytree(source_lists, target_lists, symlinks=True)

    log_cb(_("Installing packages for standard native system: {packages}").format(packages=", ".join(missing)))
    env = [
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "APT_LISTCHANGES_FRONTEND=none",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
    ]
    if not use_efi and "grub-pc" in missing:
        _chroot(
            target,
            env + ["debconf-set-selections"],
            log_cb,
            dry_run=dry_run,
            input_text=f"grub-pc grub-pc/install_devices multiselect {state.target_device}\n",
            check=False,
        )
    if not state.package_cache_path:
        _chroot(target, env + ["apt-get", "update"], log_cb, dry_run=dry_run)
    log_cb(_("Repairing copied package state from the staged APT cache..."))
    _chroot(
        target,
        env + ["apt-get", "--no-download", "--fix-broken", "install", "-y", "--no-install-recommends"],
        log_cb,
        dry_run=dry_run,
    )
    _chroot(
        target,
        env + ["apt-get", "--no-download", "install", "-y", "--no-install-recommends"] + missing,
        log_cb,
        dry_run=dry_run,
    )


def _maybe_restore_kernel_metadata(target: str, state: InstallState, log_cb: Callable[[str], None], dry_run: bool = False) -> int:
    if state.download_missing_packages:
        return restore_kernel_dpkg_metadata(target, log_cb, dry_run=dry_run)
    if os.path.exists(os.path.join(target, KERNEL_METADATA_PATH, "manifest.json")):
        log_cb(_("Keeping preserved kernel package metadata inactive for offline fallback."))
    return 0


def _replace_hosts_hostname(target: str, hostname: str, dry_run: bool = False) -> None:
    hosts = os.path.join(target, "etc", "hosts")
    if dry_run:
        return
    lines = []
    if os.path.exists(hosts):
        with open(hosts, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("127.0.1.1"):
                    continue
                lines.append(line)
    lines.append(f"127.0.1.1\t{hostname}\n")
    _write_text(hosts, "".join(lines), dry_run=False)


def _target_user_exists(target: str, username: str) -> bool:
    passwd = os.path.join(target, "etc", "passwd")
    try:
        with open(passwd, "r", encoding="utf-8", errors="ignore") as fh:
            return any(line.split(":", 1)[0] == username for line in fh if line)
    except OSError:
        return False


def _target_groups(target: str) -> Set[str]:
    groups = set()
    group_file = os.path.join(target, "etc", "group")
    try:
        with open(group_file, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if ":" in line:
                    groups.add(line.split(":", 1)[0])
    except OSError:
        pass
    return groups


def _split_group_list(value: str) -> List[str]:
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def _ensure_native_user(target: str, state: InstallState, log_cb: Callable[[str], None], dry_run: bool = False, extra_groups: Optional[List[str]] = None) -> str:
    username = state.user_config.username or "live"
    if _target_user_exists(target, username):
        log_cb(_("Using existing user account: {user}").format(user=username))
        return username

    log_cb(_("Creating user account: {user}").format(user=username))
    available_groups = _target_groups(target)
    requested_groups = _split_group_list(state.user_config.user_default_groups)
    if not requested_groups:
        requested_groups = list(NATIVE_DEFAULT_USER_GROUPS)
    for group in extra_groups or []:
        if group not in requested_groups:
            requested_groups.append(group)
    requested_groups = filter_groups_for_profile(requested_groups, state.security_profile)
    groups = [group for group in requested_groups if group in available_groups]

    if _target_has_executable(target, "/usr/lib/user-setup/user-setup-apply"):
        selections = "".join([
            "user-setup passwd/make-user boolean true\n",
            "user-setup passwd/root-password string\n",
            "user-setup passwd/root-password-again string\n",
            "user-setup passwd/root-password-crypted string *\n",
            "user-setup passwd/user-password string\n",
            "user-setup passwd/user-password-again string\n",
            "user-setup passwd/user-password-crypted string *\n",
            "user-setup passwd/user-default-groups string {groups}\n".format(groups=" ".join(groups)),
            "user-setup passwd/user-fullname string {name}\n".format(name=state.user_config.full_name or username),
            "user-setup passwd/username string {user}\n".format(user=username),
            "user-setup passwd/user-uid string 1000\n",
        ])
        rc = _chroot_returncode(target, ["debconf-set-selections"], log_cb, dry_run=dry_run, input_text=selections)
        if rc == 0:
            rc = _chroot_returncode(target, ["/usr/lib/user-setup/user-setup-apply"], log_cb, dry_run=dry_run)
        if rc == 0:
            return username
        if _target_user_exists(target, username):
            log_cb(_("Warning: user-setup reported failure after creating {user}; continuing with that account.").format(user=username))
            return username
        log_cb(_("Warning: user-setup failed; falling back to useradd."))

    cmd = ["useradd", "-m", "-s", "/bin/bash"]
    if groups:
        cmd.extend(["-G", ",".join(groups)])
    cmd.append(username)
    _chroot(target, cmd, log_cb, dry_run=dry_run)
    return username


def _apply_native_settings(target: str, state: InstallState, log_cb: Callable[[str], None], dry_run: bool = False, extra_user_groups: Optional[List[str]] = None) -> None:
    user = state.user_config
    if user.hostname:
        log_cb(_("Setting hostname..."))
        _write_text(os.path.join(target, "etc", "hostname"), user.hostname + "\n", dry_run=dry_run)
        _replace_hosts_hostname(target, user.hostname, dry_run=dry_run)
    if user.locale:
        log_cb(_("Setting locale..."))
        _apply_native_locale(target, user.locale, log_cb, dry_run=dry_run)
    if user.timezone:
        log_cb(_("Setting timezone..."))
        _write_text(os.path.join(target, "etc", "timezone"), user.timezone + "\n", dry_run=dry_run)
        if not dry_run:
            zone = os.path.join(target, "usr", "share", "zoneinfo", user.timezone)
            localtime = os.path.join(target, "etc", "localtime")
            if os.path.exists(zone):
                try:
                    if os.path.lexists(localtime):
                        os.unlink(localtime)
                    os.symlink(os.path.relpath(zone, os.path.dirname(localtime)), localtime)
                except OSError:
                    pass
    if user.keyboard:
        log_cb(_("Setting keyboard layout..."))
        model = user.keyboard_model or "pc105"
        variants = user.keyboard_variants or ""
        options = user.keyboard_options or ""
        _write_text(
            os.path.join(target, "etc", "default", "keyboard"),
            f'XKBMODEL="{model}"\nXKBLAYOUT="{user.keyboard}"\nXKBVARIANT="{variants}"\nXKBOPTIONS="{options}"\n',
            dry_run=dry_run,
        )
    if user.network_method == "static":
        log_cb(_("Configuring static IPv4 network..."))
        write_network_profile(
            target,
            user.network_interface,
            user.network_address,
            user.network_prefix,
            user.network_gateway,
            user.network_dns,
            dry_run=dry_run,
        )

    account = _ensure_native_user(target, state, log_cb, dry_run=dry_run, extra_groups=extra_user_groups)
    if user.full_name:
        _chroot(target, ["chfn", "-f", user.full_name, account], log_cb, dry_run=dry_run)
    if user.password:
        password_hash = hash_system_password(user.password)
        _chroot(target, ["usermod", "-p", password_hash, account], log_cb, dry_run=dry_run)
    if user.root_password:
        password_hash = hash_system_password(user.root_password)
        _chroot(target, ["usermod", "-p", password_hash, "root"], log_cb, dry_run=dry_run)
        log_cb(_("Root password configured."))
    else:
        # Native default: lock root so only the regular user logs in (sudo).
        log_cb(_("Locking root account (no root password set)..."))
        _chroot(target, ["usermod", "-L", "root"], log_cb, dry_run=dry_run, check=False)
        _chroot(target, ["usermod", "-p", "*", "root"], log_cb, dry_run=dry_run, check=False)

    if user.default_target:
        target_name = normalize_default_target(user.default_target)
        _target_service_action(target, "default", target_name, log_cb, dry_run=dry_run)
    enable_services, disable_services = merge_service_lists(
        process_services_field(user.enable_services).split(",") if user.enable_services else [],
        process_services_field(user.disable_services).split(",") if user.disable_services else [],
        state.security_profile,
    )
    for action, services in (("enable", enable_services), ("disable", disable_services)):
        for service in services:
            if service:
                _target_service_action(target, action, service, log_cb, dry_run=dry_run)


def run_native_install(
    state: InstallState,
    progress_cb: Callable[[int, str], None],
    log_cb: Callable[[str], None],
    dry_run: bool = False,
) -> None:
    if state.placement == "manual":
        if not state.target_device:
            raise RuntimeError(_("No target device selected."))
        state.target_device = resolve_install_device(state.target_device, expected_identity=state.target_device_identity)
        return _run_manual_native_install(state, progress_cb, log_cb, dry_run=dry_run)
    if not state.target_device:
        raise RuntimeError(_("No target device selected."))
    state.target_device = resolve_install_device(state.target_device, expected_identity=state.target_device_identity)
    layout = scan_disk(state.target_device)
    plan = build_plan(
        layout,
        state.placement,
        state.filesystem,
        install_mode=state.install_mode,
        swap_size_mib=state.swap_size_mib,
        boot_layout=state.boot_layout,
        alongside_size_mib=state.alongside_size_mib,
        required_root_mib=state.required_root_mib,
    )
    state.partition_plan = plan

    if not dry_run:
        multiboot = state.placement != "erase_all"
        required = manual_native_package_requirements(
            plan.use_efi, state.filesystem, alongside=multiboot
        )
        missing = native_missing_packages(plan.use_efi, state.filesystem, alongside=multiboot)
        if state.download_missing_packages:
            package_result = preflight_package_download(required)
            if not preflight_ok(package_result):
                raise RuntimeError(
                    _("Required boot packages cannot be downloaded. Check the internet connection, APT availability, and free cache space before continuing.")
                )
            log_cb(_("Downloading required boot packages before modifying the disk..."))
            try:
                state.package_cache_path = prepare_package_cache(required)
                _log_package_cache(state.package_cache_path, required, log_cb)
            except Exception as exc:
                raise RuntimeError(
                    _("Required boot packages could not be downloaded before disk modification: {error}").format(error=exc)
                )
        elif missing:
            requires_grub = native_requires_standard_bootloader(plan.use_efi, state.placement)
            if requires_grub:
                raise RuntimeError(
                    _("This installation requires GRUB and os-prober before the disk can be modified. "
                      "Enable package download and connect to the internet, or choose a single BIOS/MBR erase-all installation.")
                )

    # Measure the real, selected overlay input after non-destructive package
    # staging but before execute_plan. Rebuild so its complete payload governs
    # every placement rather than the UI's advisory cache.
    bundle_bytes = preflight_selected_bundles(selected_modules=state.selected_modules)
    state.required_root_mib = max(state.required_root_mib, required_root_mib(bundle_bytes))
    plan = build_plan(
        layout, state.placement, state.filesystem, install_mode=state.install_mode,
        swap_size_mib=state.swap_size_mib, boot_layout=state.boot_layout,
        alongside_size_mib=state.alongside_size_mib, required_root_mib=state.required_root_mib,
    )
    state.partition_plan = plan

    root_part = esp_part = root_mount = esp_mount = None
    success = False
    progress_cb(0, _("Preparing target disk..."))
    try:
        root_part, esp_part, root_mount, esp_mount = execute_plan(plan, log_cb, dry_run=dry_run, cancel_cb=lambda: state.cancel_requested)
        if dry_run:
            progress_cb(100, _("Dry run complete."))
            success = True
            return
        _raise_if_canceled(state)
        with BundleOverlay(log_cb=log_cb, selected_modules=state.selected_modules) as source_root:
            _copy_native_root(source_root, root_mount, progress_cb, log_cb, dry_run=dry_run, cancel_cb=lambda: state.cancel_requested)
        _raise_if_canceled(state)
        progress_cb(82, _("Configuring installed system..."))
        _prepare_runtime_dirs(root_mount, dry_run=dry_run)
        _patch_sysv_quiet_wrapper(root_mount, dry_run, log_cb)
        _maybe_restore_kernel_metadata(root_mount, state, log_cb, dry_run=dry_run)
        _mount_chroot_api(root_mount, esp_mount, dry_run, log_cb)
        try:
            _raise_if_canceled(state)
            _install_native_packages(root_mount, plan.use_efi, state.filesystem, state, log_cb, dry_run=dry_run)
            _raise_if_canceled(state)
            extra_user_groups = _collect_live_allowuser_groups(root_mount)
            if extra_user_groups:
                log_cb(_("Applying live module user groups to native user: {groups}").format(groups=", ".join(extra_user_groups)))
            apply_security_profile(root_mount, state.security_profile, log_cb, dry_run=dry_run, runtime_mode="native")
            swap_part = next((p.path for p in plan.partitions if p.role == "swap"), None)
            _write_fstab(root_mount, root_part, esp_part, swap_part, dry_run, log_cb)
            _apply_native_settings(root_mount, state, log_cb, dry_run=dry_run, extra_user_groups=extra_user_groups)
            _raise_if_canceled(state)
            _cleanup_native_live_packages(root_mount, log_cb, dry_run=dry_run)
            _generate_ssh_host_keys(root_mount, log_cb, dry_run=dry_run)
            _generate_ssl_snakeoil_cert(root_mount, log_cb, dry_run=dry_run)
        finally:
            _unmount_chroot_api(root_mount, esp_mount, log_cb)
        _raise_if_canceled(state)
        _install_native_bootloader(root_mount, state.target_device, root_part, plan.use_efi, esp_mount, progress_cb, log_cb, dry_run=dry_run)
        progress_cb(99, _("Unmounting disk..."))
        if root_mount or esp_mount:
            unmount_partitions(root_part, esp_part, root_mount, esp_mount)
            log_cb(_("Cleaned up target mounts."))
            root_mount = esp_mount = None
        progress_cb(100, _("Installation complete!"))
        success = True
    finally:
        if not success and (root_mount or esp_mount):
            try:
                unmount_partitions(root_part, esp_part, root_mount, esp_mount)
                log_cb(_("Cleaned up target mounts."))
            except Exception as exc:
                log_cb(_("Warning: failed to unmount target: {error}").format(error=exc))
        if state.package_cache_path:
            shutil.rmtree(state.package_cache_path, ignore_errors=True)
            state.package_cache_path = None
