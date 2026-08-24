#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Callable, List, Optional, Set

from bundle_source import BundleOverlay, preflight_selected_bundles
from disk_utils import get_live_source_mount, native_install_supported, partition_device_path, resolve_install_device
from install_state import InstallCanceled, InstallState
from kernel_metadata import (
    prepare_kernel_registration,
)
from manual_executor import execute_manual_plan, required_manual_tools
from manual_partitioning import ExistingPartitionRef, ManualPlanError, ManualPlanner
from mount_utils import mount_partition, unmount_mountpoints, unmount_partitions
from network_config import write_network_profile
from package_preflight import (
    manual_native_package_requirements,
    native_missing_packages,
    native_kernel_architecture_preflight,
    native_requires_standard_bootloader,
    package_installed_or_provided,
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

# Mixed-architecture native combinations remain empty until their registration,
# package lifecycle, BIOS/EFI boot, and reboot evidence exists.
VERIFIED_NATIVE_MIXED_ARCHITECTURES = ()


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
    mounted = []
    try:
        for src, dst in (("/dev", "dev"), ("/proc", "proc"), ("/sys", "sys"), ("/run", "run")):
            mountpoint = os.path.join(target, dst)
            os.makedirs(mountpoint, exist_ok=True)
            _run(["mount", "--bind", src, mountpoint], log_cb)
            mounted.append(mountpoint)
        efivars = "/sys/firmware/efi/efivars"
        if os.path.ismount(efivars):
            target_efivars = os.path.join(target, efivars.lstrip("/"))
            os.makedirs(target_efivars, exist_ok=True)
            _run(["mount", "--bind", efivars, target_efivars], log_cb)
            mounted.append(target_efivars)
        dev_pts = os.path.join(target, "dev", "pts")
        os.makedirs(dev_pts, exist_ok=True)
        _run(["mount", "--bind", "/dev/pts", dev_pts], log_cb)
        mounted.append(dev_pts)
        if esp_mount:
            target_esp = os.path.join(target, "boot", "efi")
            os.makedirs(target_esp, exist_ok=True)
            _run(["mount", "--bind", esp_mount, target_esp], log_cb)
            mounted.append(target_esp)
    except BaseException:
        for mountpoint in reversed(mounted):
            subprocess.run(
                ["umount", mountpoint], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        raise


def _unmount_chroot_api(target: str, esp_mount: Optional[str], log_cb: Callable[[str], None]) -> None:
    for dst in (["boot/efi"] if esp_mount else []):
        subprocess.run(["umount", os.path.join(target, dst)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        ["umount", os.path.join(target, "sys", "firmware", "efi", "efivars")],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
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


def _install_grub_206_compatibility(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    script = "/etc/grub.d/30_uefi-firmware"
    target_script = os.path.join(target, script.lstrip("/"))
    diversion = "/usr/lib/minios-grub-compat/30_uefi-firmware.distrib"
    if not dry_run:
        os.makedirs(os.path.join(target, "usr", "lib", "minios-grub-compat"), exist_ok=True)
    _chroot(
        target,
        ["dpkg-divert", "--quiet", "--local", "--rename", "--add", "--divert", diversion, script],
        log_cb,
        dry_run=dry_run,
    )
    _write_text(
        target_script,
        "#!/bin/sh\n"
        "# MiniOS keeps grub.cfg loadable by the Bookworm GRUB 2.06 IA32 core.\n"
        "exit 0\n",
        dry_run=dry_run,
    )
    if not dry_run:
        os.chmod(target_script, 0o755)


def _validate_grub_206_compatibility(path: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    with open(path, "r", encoding="utf-8", errors="strict") as stream:
        content = stream.read()
    if re.search(r"^\s*fwsetup\s+--is-supported(?:\s|$)", content, re.MULTILINE):
        raise RuntimeError(
            _("The generated GRUB configuration requires GRUB 2.12 and cannot be loaded by the IA32 GRUB 2.06 core."))


def _can_install_grub_native(target: str, use_efi: bool) -> bool:
    if not _grub_config_command(target):
        return False
    if use_efi:
        source = get_live_source_mount()
        return (os.path.isfile(os.path.join(source, "EFI", "boot", "bootx64.efi")) and
                os.path.isfile(os.path.join(source, "EFI", "boot", "bootia32.efi")))
    if not _target_has_executable(target, "/usr/sbin/grub-install", "/usr/bin/grub-install"):
        return False
    return _target_has_executable(target, "/usr/lib/grub/i386-pc/modinfo.sh")


def _copy_regular_efi_tree(source: str, destination: str) -> None:
    try:
        source_mode = os.lstat(source).st_mode
    except OSError as exc:
        raise RuntimeError(_("The verified EFI source is missing.")) from exc
    if not stat.S_ISDIR(source_mode):
        raise RuntimeError(_("The verified EFI source is not a real directory."))
    os.makedirs(destination, exist_ok=True)
    for root, directories, files in os.walk(source, followlinks=False):
        relative = os.path.relpath(root, source)
        target_root = destination if relative == "." else os.path.join(destination, relative)
        os.makedirs(target_root, exist_ok=True)
        for name in directories:
            path = os.path.join(root, name)
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise RuntimeError(_("The verified EFI source contains an unsafe directory."))
            os.makedirs(os.path.join(target_root, name), exist_ok=True)
        for name in files:
            path = os.path.join(root, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise RuntimeError(_("The verified EFI source contains an unsafe file."))
            target = os.path.join(target_root, name)
            if os.path.lexists(target):
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.unlink(target)
            shutil.copy2(path, target, follow_symlinks=False)


def _validate_regular_efi_tree(path: str) -> None:
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise RuntimeError(_("The verified EFI source is not a real directory."))
    except OSError as exc:
        raise RuntimeError(_("The verified EFI source is missing.")) from exc
    for root, directories, files in os.walk(path, followlinks=False):
        for name in directories:
            if not stat.S_ISDIR(os.lstat(os.path.join(root, name)).st_mode):
                raise RuntimeError(_("The verified EFI source contains an unsafe directory."))
        for name in files:
            if not stat.S_ISREG(os.lstat(os.path.join(root, name)).st_mode):
                raise RuntimeError(_("The verified EFI source contains an unsafe file."))


def _regular_efi_tree_bytes(path: str) -> int:
    if not os.path.exists(path):
        return 0
    _validate_regular_efi_tree(path)
    return sum(os.path.getsize(os.path.join(root, name))
               for root, _directories, files in os.walk(path, followlinks=False)
               for name in files)


def _sha256_regular_file(path: str) -> str:
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise RuntimeError(_("The verified EFI manifest references an unsafe file."))
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _preflight_efi_payload_contract(use_efi: bool) -> None:
    if not use_efi:
        return
    source = get_live_source_mount()
    manifest_path = os.path.join(source, "minios", "boot", "efi-manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8", errors="strict") as stream:
            manifest = json.load(stream)
    except (OSError, ValueError) as exc:
        raise RuntimeError(_("The live medium has no valid EFI architecture contract.")) from exc
    if manifest.get("format") != 1 or manifest.get("layout") != "dual-architecture-esp":
        raise RuntimeError(_("The live medium has an unsupported EFI architecture contract."))
    architectures = manifest.get("architectures")
    if not isinstance(architectures, dict):
        raise RuntimeError(_("The live medium EFI architecture contract is incomplete."))
    x64 = architectures.get("x64")
    ia32 = architectures.get("ia32")
    if not isinstance(x64, dict) or not isinstance(ia32, dict):
        raise RuntimeError(_("The live medium EFI architecture contract is incomplete."))
    if ia32.get("vendor") != "debian" or ia32.get("suite") != "bookworm" or ia32.get("grub_version") != "2.06":
        raise RuntimeError(_("The IA32 EFI chain must use the Bookworm GRUB 2.06 compatibility core."))
    if x64.get("vendor") == "debian" and (
            x64.get("suite") != "trixie" or x64.get("grub_version") != "2.12"):
        raise RuntimeError(_("The Debian x64 EFI chain must use the Trixie GRUB 2.12 core."))
    if x64.get("vendor") not in ("debian", "ubuntu") or not re.fullmatch(
            r"[0-9]+\.[0-9]+", str(x64.get("grub_version", ""))):
        raise RuntimeError(_("The x64 EFI chain has no supported GRUB version contract."))
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RuntimeError(_("The live medium EFI manifest has no verified files."))
    expected_hashes = {}
    for entry in files:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("sha256"), str):
            expected_hashes[entry["path"]] = entry["sha256"].lower()
    contracts = (
        (x64, {"shim_path": "/EFI/boot/bootx64.efi", "grub_path": "/EFI/boot/grubx64.efi"}),
        (ia32, {"shim_path": "/EFI/boot/bootia32.efi", "grub_path": "/EFI/boot/grubia32.efi"}),
    )
    for contract, required_paths in contracts:
        for field, required_path in required_paths.items():
            manifest_name = contract.get(field)
            if manifest_name != required_path:
                raise RuntimeError(_("The live medium EFI architecture contract has an unsafe loader path."))
            expected = expected_hashes.get(manifest_name)
            path = os.path.join(source, manifest_name.lstrip("/"))
            try:
                actual = _sha256_regular_file(path)
            except OSError as exc:
                raise RuntimeError(_("The verified EFI loader is missing.")) from exc
            if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
                raise RuntimeError(_("The verified EFI loader does not match its manifest."))


def _efi_loader_name() -> str:
    try:
        with open("/sys/firmware/efi/fw_platform_size", "r", encoding="ascii") as stream:
            return "bootia32.efi" if stream.read().strip() == "32" else "bootx64.efi"
    except OSError:
        return "bootia32.efi" if os.uname().machine in ("i386", "i486", "i586", "i686") else "bootx64.efi"


def _efi_grub_vendors(source_boot: str) -> Set[str]:
    vendors = set()
    for name in ("grubx64.efi", "grubia32.efi"):
        path = os.path.join(source_boot, name)
        try:
            mode = os.lstat(path).st_mode
            if not stat.S_ISREG(mode):
                raise RuntimeError(_("The verified EFI GRUB binary is unsafe."))
            with open(path, "rb") as stream:
                data = stream.read()
        except OSError as exc:
            raise RuntimeError(_("The verified EFI GRUB binary is missing.")) from exc
        matches = [vendor for vendor in ("debian", "ubuntu")
                   if ("/EFI/" + vendor).encode("ascii") in data]
        if len(matches) != 1:
            raise RuntimeError(_("The verified EFI GRUB vendor prefix is ambiguous."))
        vendors.add(matches[0])
    return vendors


def _native_grub_wrapper() -> str:
    return (
        "insmod ext2\n"
        "search --no-floppy --file --set=root /boot/grub/grub.cfg\n"
        "set prefix=($root)/boot/grub\n"
        "configfile $prefix/grub.cfg\n"
    )


def _efi_grub_architectures():
    return ("i386-efi", "x86_64-efi")


def _efi_boot_numbers(output: str) -> Set[str]:
    return set(re.findall(r"^Boot([0-9A-Fa-f]{4})(?:\*|\s)", output, re.MULTILINE))


def _efi_boot_order(output: str) -> Set[str]:
    match = re.search(r"^BootOrder:\s*([^\r\n]+)", output, re.MULTILINE)
    return set(item.strip().upper() for item in match.group(1).split(",") if item.strip()) if match else set()


def _matching_minios_efi_entries(output: str, loader: str, partuuid: str,
                                 require_bootable: bool = True) -> Set[str]:
    order = _efi_boot_order(output)
    normalized_partuuid = partuuid.replace("-", "").lower()
    matches = set()
    for line in output.splitlines():
        entry = re.match(r"^Boot([0-9A-Fa-f]{4})(\*)?\s+MiniOS(?:\s|$)", line)
        if not entry:
            continue
        number = entry.group(1).upper()
        normalized_line = line.replace("-", "").lower()
        if loader.lower() not in line.lower() or normalized_partuuid not in normalized_line:
            continue
        if require_bootable and (not entry.group(2) or number not in order):
            continue
        matches.add(number)
    return matches


def _install_native_efi_boot_entry(target: str, disk: str, esp_device: str,
                                   log_cb: Callable[[str], None]) -> None:
    disk_path = os.path.realpath(disk)
    esp_path = os.path.realpath(esp_device)
    topology = subprocess.run(
        ["lsblk", "-nro", "PKNAME,PARTN", "--", esp_path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, check=False,
    )
    fields = topology.stdout.split()
    if (topology.returncode != 0 or len(fields) != 2 or
            fields[0] != os.path.basename(disk_path) or not fields[1].isdigit()):
        raise RuntimeError(_("The reused EFI system partition does not belong to the target disk."))
    before = _chroot_capture(target, ["efibootmgr", "-v"], log_cb)
    if before is None or before.returncode != 0:
        raise RuntimeError(_("Existing UEFI boot entries cannot be read."))
    loader = "\\EFI\\minios\\" + _efi_loader_name()
    partuuid = subprocess.check_output(
        ["blkid", "-s", "PARTUUID", "-o", "value", esp_path],
        universal_newlines=True, stderr=subprocess.DEVNULL,
    ).strip()
    if not partuuid:
        raise RuntimeError(_("The reused EFI system partition has no stable partition identity."))
    if _matching_minios_efi_entries(before.stdout, loader, partuuid):
        return
    command = [
        "efibootmgr", "--create", "--disk", disk, "--part", fields[1],
        "--label", "MiniOS", "--loader", loader,
    ]
    created = _chroot_capture(target, command, log_cb)
    if created is None or created.returncode != 0:
        raise RuntimeError(_("The MiniOS UEFI boot entry could not be created."))
    after = _chroot_capture(target, ["efibootmgr", "-v"], log_cb)
    if after is None or after.returncode != 0:
        raise RuntimeError(_("The MiniOS UEFI boot entry could not be verified."))
    new_entries = _efi_boot_numbers(after.stdout) - _efi_boot_numbers(before.stdout)
    owned = _matching_minios_efi_entries(after.stdout, loader, partuuid, require_bootable=False) & new_entries
    valid = _matching_minios_efi_entries(after.stdout, loader, partuuid) & new_entries
    if len(owned) != 1 or len(valid) != 1:
        for boot_number in sorted(owned):
            _chroot(target, ["efibootmgr", "--bootnum", boot_number, "--delete-bootnum"],
                    log_cb, check=False)
        raise RuntimeError(_("The MiniOS UEFI boot entry could not be verified."))


def _publish_native_efi(source_efi: str, target_efi: str, wrapper: str,
                        register_reused: Optional[Callable[[], None]] = None,
                        reuse_esp: bool = False) -> None:
    """Publish a full fresh ESP or only the MiniOS-owned tree on a reused ESP."""
    if os.path.lexists(target_efi) and (os.path.islink(target_efi) or not os.path.isdir(target_efi)):
        raise RuntimeError(_("The EFI destination is not a real directory."))
    reused = bool(reuse_esp)
    parent = os.path.dirname(target_efi)
    os.makedirs(parent, exist_ok=True)
    staged = tempfile.mkdtemp(prefix=".minios-native-efi-", dir=parent)
    candidate = os.path.join(staged, "minios.candidate" if reused else "EFI.candidate")
    owned_target = os.path.join(target_efi, "minios") if reused else target_efi
    backup = os.path.join(staged, "EFI.original", "minios") if reused else os.path.join(staged, "EFI.original")
    moved_original = False
    preserve_staged = False
    created_target_efi = False
    created_vendor_configs = []
    created_vendor_directories = []
    try:
        source_boot = os.path.join(source_efi, "boot")
        grub_vendors = sorted(_efi_grub_vendors(source_boot))
        if reused:
            _copy_regular_efi_tree(source_boot, candidate)
            source_vendor = os.path.join(source_efi, "minios")
            if os.path.isdir(source_vendor):
                _copy_regular_efi_tree(source_vendor, candidate)
            wrapper_directories = [candidate]
            vendor_configs = []
            for vendor in grub_vendors:
                vendor_directory = os.path.join(target_efi, vendor)
                if os.path.lexists(vendor_directory) and (
                        os.path.islink(vendor_directory) or not os.path.isdir(vendor_directory)):
                    raise RuntimeError(
                        _("The reused EFI system partition has an unsafe {vendor} directory.").format(
                            vendor=vendor))
                vendor_config = os.path.join(vendor_directory, "grub.cfg")
                if os.path.lexists(vendor_config):
                    existing_wrapper = None
                    if not os.path.islink(vendor_config) and os.path.isfile(vendor_config):
                        with open(vendor_config, "r", encoding="utf-8", errors="strict") as stream:
                            existing_wrapper = stream.read()
                    if existing_wrapper != wrapper:
                        raise RuntimeError(
                            _("The reused EFI system partition has a conflicting {vendor} boot configuration.").format(
                                vendor=vendor))
                else:
                    vendor_configs.append((vendor_directory, vendor_config))
                for architecture in _efi_grub_architectures():
                    module_target = os.path.join(vendor_directory, architecture)
                    if os.path.lexists(module_target):
                        raise RuntimeError(
                            _("The reused EFI system partition has a conflicting {vendor} module tree.").format(
                                vendor=vendor))
        else:
            _copy_regular_efi_tree(source_efi, candidate)
            vendor_configs = []
            wrapper_directories = [
                os.path.join(candidate, vendor)
                for vendor in ("boot", "debian", "ubuntu", "minios")
            ]
        for directory in wrapper_directories:
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "grub.cfg"), "w", encoding="utf-8") as config:
                config.write(wrapper)

        if reused and not os.path.isdir(target_efi):
            os.makedirs(target_efi)
            created_target_efi = True
        if os.path.lexists(owned_target):
            if os.path.islink(owned_target) or not os.path.isdir(owned_target):
                raise RuntimeError(_("The MiniOS EFI destination is not a real directory."))
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            os.replace(owned_target, backup)
            moved_original = True
        try:
            os.replace(candidate, owned_target)
            if reused:
                if register_reused is None:
                    raise RuntimeError(_("A reused EFI system partition requires a MiniOS firmware boot entry."))
                for vendor_directory, vendor_config in vendor_configs:
                    if not os.path.isdir(vendor_directory):
                        os.makedirs(vendor_directory)
                        created_vendor_directories.append(vendor_directory)
                    descriptor, temporary = tempfile.mkstemp(prefix=".minios-grub-", dir=vendor_directory)
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8") as config:
                            config.write(wrapper)
                            config.flush()
                            os.fsync(config.fileno())
                        os.replace(temporary, vendor_config)
                        created_vendor_configs.append(vendor_config)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                register_reused()
        except BaseException as publication_error:
            removal_error = None
            for path in reversed(created_vendor_configs):
                try:
                    os.unlink(path)
                except BaseException as exc:
                    removal_error = removal_error or exc
            for path in reversed(created_vendor_directories):
                try:
                    os.rmdir(path)
                except BaseException as exc:
                    removal_error = removal_error or exc
            if os.path.lexists(owned_target):
                try:
                    if os.path.isdir(owned_target) and not os.path.islink(owned_target):
                        shutil.rmtree(owned_target)
                    else:
                        os.unlink(owned_target)
                except BaseException as exc:
                    removal_error = exc
            if moved_original and removal_error is None:
                try:
                    os.replace(backup, owned_target)
                    moved_original = False
                except BaseException as exc:
                    removal_error = exc
            if created_target_efi and not moved_original:
                try:
                    os.rmdir(target_efi)
                    created_target_efi = False
                except BaseException as exc:
                    removal_error = removal_error or exc
            if moved_original:
                preserve_staged = True
                raise RuntimeError(
                    _("EFI publication failed and the original EFI tree remains at {backup}: {error}").format(
                        backup=backup, error=removal_error)
                ) from publication_error
            if removal_error is not None:
                raise RuntimeError(
                    _("EFI publication failed and its partial destination could not be removed: {error}").format(
                        error=removal_error)
                ) from publication_error
            raise
    finally:
        if not preserve_staged:
            shutil.rmtree(staged, ignore_errors=True)


def _install_grub_native(target: str, disk: str, use_efi: bool, progress_cb: Callable[[int, str], None], log_cb: Callable[[str], None], dry_run: bool, esp_device: Optional[str] = None, reuse_esp: bool = False) -> None:
    progress_cb(92, _("Installing GRUB bootloader..."))
    if not use_efi:
        install_cmd = ["grub-install", "--target=i386-pc", "--recheck", disk]
    _write_text(
        os.path.join(target, "etc", "default", "grub.d", "minios-native.cfg"),
        'GRUB_CMDLINE_LINUX="rw"\n'
        + ('GRUB_DISABLE_OS_PROBER=false\n' if _target_has_executable(target, "/usr/bin/os-prober", "/usr/sbin/os-prober") else ''),
        dry_run=dry_run,
    )
    config_cmd = _grub_config_command(target)
    if not config_cmd:
        raise RuntimeError(_("GRUB configuration generator not found in installed system."))
    if not dry_run:
        os.makedirs(os.path.join(target, "boot", "grub"), exist_ok=True)
    if use_efi:
        _install_grub_206_compatibility(target, log_cb, dry_run=dry_run)
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
    if use_efi:
        _validate_grub_206_compatibility(grub_cfg, dry_run=dry_run)
    if _target_has_executable(target, "/usr/bin/grub-script-check", "/usr/sbin/grub-script-check"):
        _chroot(target, ["grub-script-check", "/boot/grub/grub.cfg"], log_cb, dry_run=dry_run)
    if use_efi:
        source_efi = os.path.join(get_live_source_mount(), "EFI")
        target_efi = os.path.join(target, "boot", "efi", "EFI")
        wrapper = _native_grub_wrapper()
        if not dry_run:
            if not os.path.isdir(source_efi):
                raise RuntimeError(_("The live medium has no verified EFI chain."))
            register_reused = None
            if reuse_esp:
                if not esp_device:
                    raise RuntimeError(_("The reused EFI system partition has no stable device identity."))
                register_reused = lambda: _install_native_efi_boot_entry(
                    target, disk, esp_device, log_cb)
            _publish_native_efi(
                source_efi, target_efi, wrapper, register_reused, reuse_esp=reuse_esp)

    else:
        # grub-install is the firmware-visible BIOS publication step. Keep it last.
        _chroot(target, install_cmd, log_cb, dry_run=dry_run)


def _install_native_bootloader(target: str, disk: str, root_part: str, use_efi: bool, esp_mount: Optional[str], progress_cb: Callable[[int, str], None], log_cb: Callable[[str], None], dry_run: bool = False, esp_already_mounted: bool = False, require_grub: bool = False, version: Optional[str] = None, kernel: Optional[str] = None, initrd: Optional[str] = None, esp_device: Optional[str] = None, reuse_esp: bool = False) -> None:
    progress_cb(92, _("Installing native bootloader..."))
    _mount_chroot_api(target, None if esp_already_mounted else esp_mount, dry_run, log_cb)
    try:
        version = version or _kernel_version(target)
        kernel = kernel or _copy_native_kernel(target, version, dry_run, log_cb)
        initrd = initrd or _generate_native_initramfs(target, version, dry_run, log_cb)
        if _can_install_grub_native(target, use_efi):
            _install_grub_native(target, disk, use_efi, progress_cb, log_cb, dry_run,
                                 esp_device=esp_device, reuse_esp=reuse_esp)
        elif use_efi:
            raise RuntimeError(
                _("Native UEFI install requires the verified EFI chain and GRUB configuration tools.")
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


def _preflight_selected_kernel(state, use_efi: bool, log_cb: Callable[[str], None]) -> None:
    """Validate the selected format-1 layer before any partition mutation."""
    with BundleOverlay(log_cb=log_cb, selected_modules=state.selected_modules) as source_root:
        approved_foreign = {kernel for _userspace, kernel in VERIFIED_NATIVE_MIXED_ARCHITECTURES}
        source_boot = os.path.join(get_live_source_mount(), "minios", "boot")
        external_payload = {}
        if os.path.isdir(source_boot):
            for name in os.listdir(source_boot):
                if name.startswith("vmlinuz-"):
                    external_payload["/boot/" + name] = os.path.join(source_boot, name)
        registration = prepare_kernel_registration(
            source_root, allow_foreign_architectures=approved_foreign,
            external_payload_paths=external_payload,
            verify_dependencies=False,
        )
        try:
            native_kernel_architecture_preflight(
                registration.native_architecture,
                registration.manifest["kernel"]["package_architecture"],
                use_efi,
                verified_combinations=VERIFIED_NATIVE_MIXED_ARCHITECTURES,
            )
            log_cb(
                _("Validated format-1 kernel {version} before disk modification.").format(
                    version=registration.kernel_version
                )
            )
        finally:
            registration.close()


def _preflight_writable_efi_variables() -> None:
    variables = "/sys/firmware/efi/efivars"
    if not os.path.isdir(variables) or not os.path.ismount(variables):
        readonly = True
    else:
        try:
            readonly = bool(os.statvfs(variables).f_flag & os.ST_RDONLY)
        except OSError:
            readonly = True
    if readonly or not os.access(variables, os.W_OK):
        raise RuntimeError(
            _("Reusing an EFI system partition requires writable UEFI firmware variables before the disk can be modified."))


def _preflight_reused_efi_variables(plan) -> None:
    if getattr(plan, "use_efi", False) and getattr(plan, "reuse_esp", False):
        _preflight_writable_efi_variables()


def _preflight_reused_efi_payload_path(esp_path: str) -> None:
    source_efi = os.path.join(get_live_source_mount(), "EFI")
    source_boot = os.path.join(source_efi, "boot")
    _validate_regular_efi_tree(source_efi)
    vendors = _efi_grub_vendors(source_boot)
    wrapper = _native_grub_wrapper()
    mount_dir = tempfile.mkdtemp(prefix="minios-efi-publication-preflight-")
    mounted = False
    try:
        result = subprocess.run(
            ["mount", "-t", "vfat", "-o", "ro,nosuid,nodev,noexec", esp_path, mount_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(_("Could not mount the reused EFI system partition for publication validation."))
        mounted = True
        target_efi = os.path.join(mount_dir, "EFI")
        target_minios = os.path.join(target_efi, "minios")
        if os.path.lexists(target_minios) and (
                os.path.islink(target_minios) or not os.path.isdir(target_minios)):
            raise RuntimeError(_("The MiniOS EFI destination is not a real directory."))
        for vendor in vendors:
            vendor_directory = os.path.join(target_efi, vendor)
            if os.path.lexists(vendor_directory) and (
                    os.path.islink(vendor_directory) or not os.path.isdir(vendor_directory)):
                raise RuntimeError(
                    _("The reused EFI system partition has an unsafe {vendor} directory.").format(
                        vendor=vendor))
            vendor_config = os.path.join(vendor_directory, "grub.cfg")
            if os.path.lexists(vendor_config):
                existing_wrapper = None
                if not os.path.islink(vendor_config) and os.path.isfile(vendor_config):
                    with open(vendor_config, "r", encoding="utf-8", errors="strict") as stream:
                        existing_wrapper = stream.read()
                if existing_wrapper != wrapper:
                    raise RuntimeError(
                        _("The reused EFI system partition has a conflicting {vendor} boot configuration.").format(
                            vendor=vendor))
            for architecture in _efi_grub_architectures():
                if os.path.lexists(os.path.join(vendor_directory, architecture)):
                    raise RuntimeError(
                        _("The reused EFI system partition has a conflicting {vendor} module tree.").format(
                            vendor=vendor))
        source_bytes = _regular_efi_tree_bytes(source_efi)
        existing_minios_bytes = _regular_efi_tree_bytes(target_minios)
        filesystem = os.statvfs(mount_dir)
        available = filesystem.f_bavail * filesystem.f_frsize
        required = ((source_bytes + existing_minios_bytes) * 125 + 99) // 100 + 1024 * 1024
        if available < required:
            raise RuntimeError(_("Reused EFI system partition does not have enough free space for transactional publication."))
    finally:
        if mounted:
            result = subprocess.run(
                ["umount", mount_dir], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(_("Could not unmount the reused EFI system partition after publication validation."))
        shutil.rmtree(mount_dir, ignore_errors=True)


def _preflight_reused_efi_payload(plan) -> None:
    if getattr(plan, "use_efi", False) and getattr(plan, "reuse_esp", False):
        _preflight_reused_efi_payload_path(plan.esp_path)


def _manual_reused_esp_path(plan) -> Optional[str]:
    if not getattr(plan, "use_efi", False) or not hasattr(plan, "snapshot"):
        return None
    for assignment in plan.assignments:
        if (assignment.role == "esp" and not assignment.format and
                isinstance(assignment.target, ExistingPartitionRef)):
            return partition_device_path(plan.snapshot.device, assignment.target.number)
    return None


def _preflight_manual_new_efi_capacity(plan) -> None:
    if not getattr(plan, "use_efi", False) or not hasattr(plan, "assignments"):
        return
    if _manual_reused_esp_path(plan):
        return
    assignment = next((item for item in plan.assignments if item.role == "esp"), None)
    if assignment is None:
        return
    payload = _regular_efi_tree_bytes(os.path.join(get_live_source_mount(), "EFI"))
    required = (payload * 125 + 99) // 100 + 1024 * 1024
    available = assignment.target.size_sectors * plan.snapshot.sector_size
    if required > available:
        raise RuntimeError(_("The EFI payload does not fit the selected EFI system partition."))


def _preflight_manual_reused_efi(plan) -> None:
    esp_path = _manual_reused_esp_path(plan)
    if not esp_path:
        return
    _preflight_writable_efi_variables()
    _preflight_reused_efi_payload_path(esp_path)


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
    _preflight_efi_payload_contract(plan.use_efi)
    _preflight_manual_new_efi_capacity(plan)
    _preflight_manual_reused_efi(plan)
    if hasattr(plan, "snapshot"):
        plan = _refresh_manual_preflight(state, plan, dry_run=dry_run, log_cb=log_cb)
        state.manual_partition_plan = plan
    _preflight_selected_kernel(state, plan.use_efi, log_cb)
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
        registration, kernel, initrd = _maybe_restore_kernel_metadata(root_target, log_cb)
        try:
            _install_native_bootloader(root_target, state.target_device, root_entry[0], plan.use_efi,
                                        esp_entry[0] if esp_entry else None, progress_cb, log_cb,
                                        esp_already_mounted=bool(esp_entry), require_grub=True,
                                        version=registration.kernel_version, kernel=kernel, initrd=initrd,
                                        esp_device=esp_entry[0] if esp_entry else None,
                                        reuse_esp=bool(_manual_reused_esp_path(plan)))
            registration.complete(root_target)
        except BaseException as exc:
            registration.rollback(str(exc), root_target)
            raise
        finally:
            registration.close()
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
    if not package_installed_or_provided("linux-initramfs-tool", root=target):
        missing = list(missing)
        if "initramfs-tools" not in missing:
            missing.append("initramfs-tools")
    if not missing:
        log_cb(_("No additional native packages are required."))
        return
    install_packages = list(missing)
    if not state.download_missing_packages:
        install_packages = [package for package in missing if package == "initramfs-tools"]
        optional = [package for package in missing if package not in install_packages]
        if optional:
            log_cb(
                _("Offline bootloader fallback will be used. Missing optional packages: {packages}").format(
                    packages=", ".join(optional)
                )
            )
        if not install_packages:
            return
        if not state.package_cache_path:
            raise RuntimeError(_("The mandatory native kernel toolchain was not staged before disk modification."))

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

    log_cb(_("Installing packages for standard native system: {packages}").format(packages=", ".join(install_packages)))
    env = [
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "APT_LISTCHANGES_FRONTEND=none",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
    ]
    if not use_efi and "grub-pc" in install_packages:
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
        env + ["apt-get", "--no-download", "install", "-y", "--no-install-recommends"] + install_packages,
        log_cb,
        dry_run=dry_run,
    )


def _update_kernel_symlinks(target: str, version: str, kernel: str, log_cb: Callable[[str], None]) -> None:
    if _target_has_executable(target, "/usr/sbin/linux-update-symlinks", "/usr/bin/linux-update-symlinks"):
        _chroot(target, ["linux-update-symlinks", "install", version, kernel], log_cb)
        return
    for name in ("vmlinuz",):
        path = os.path.join(target, name)
        if os.path.lexists(path):
            os.unlink(path)
        os.symlink(kernel.lstrip("/"), path)


def _update_initramfs_symlink(target: str, initrd: str) -> None:
    path = os.path.join(target, "initrd.img")
    if os.path.lexists(path):
        os.unlink(path)
    os.symlink(initrd.lstrip("/"), path)


def _verify_registered_kernel(target: str, registration, log_cb: Callable[[str], None]) -> None:
    commands = [
        ["dpkg", "--audit"],
        ["apt-get", "check"],
        ["apt-get", "-s", "autoremove"],
    ]
    for command in commands:
        result = _chroot_capture(target, command, log_cb)
        if result is None or result.returncode != 0:
            raise RuntimeError(_("Kernel package registration verification failed: {command}").format(command=" ".join(command)))
        if command == ["dpkg", "--audit"] and result.stdout.strip():
            raise RuntimeError(_("Kernel package registration left an inconsistent dpkg database."))
        if command[-1] == "autoremove":
            for package in registration.packages:
                if package["role"] != "dependency" and re.search(r"^Remv\s+{}(?:\s|$)".format(re.escape(package["name"])), result.stdout, re.MULTILINE):
                    raise RuntimeError(_("Registered kernel package is an autoremove candidate: {package}").format(package=package["name"]))

    for package in registration.packages:
        if package["registration"] != "synthetic-installed":
            continue
        result = _chroot_capture(
            target,
            ["dpkg-query", "-W", "-f=${Version}\t${db:Status-Abbrev}\n", package["dpkg_instance"]],
            log_cb,
        )
        expected_status = "hi " if package["hold"] else "ii "
        if result is None or result.returncode != 0 or result.stdout.strip() != "{}\t{}".format(package["version"], expected_status.strip()):
            raise RuntimeError(_("Registered kernel version/status mismatch: {package}").format(package=package["dpkg_instance"]))
        payload_entries = package.get("payload_entries", [])
        if not payload_entries:
            continue
        list_path = os.path.join(
            target, "var", "lib", "dpkg", "info",
            package["dpkg_instance"] + ".list",
        )
        if not os.path.isfile(list_path) or os.path.islink(list_path):
            raise RuntimeError(
                _("Registered kernel package has no trusted file list: {package}").format(
                    package=package["dpkg_instance"]
                )
            )
        try:
            with open(list_path, "r", encoding="utf-8", errors="strict") as stream:
                owned_paths = set(line.strip() for line in stream if line.strip())
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                _("Cannot read registered kernel file list: {package}").format(
                    package=package["dpkg_instance"]
                )
            ) from error
        for entry in payload_entries:
            if entry["path"] not in owned_paths:
                raise RuntimeError(
                    _("Registered kernel path has no dpkg owner: {path}").format(
                        path=entry["path"]
                    )
                )

    for mark in ("manual", "auto", "hold"):
        result = _chroot_capture(target, ["apt-mark", "show" + mark], log_cb)
        if result is None or result.returncode != 0:
            raise RuntimeError(_("Cannot verify kernel APT marks and holds."))
        actual = set(result.stdout.splitlines())
        for package in registration.packages:
            expected = package["hold"] if mark == "hold" else package["apt_mark"] == mark
            if package["apt_mark"] == "unchanged" and mark != "hold":
                continue
            if expected != (package["dpkg_instance"] in actual):
                raise RuntimeError(_("Kernel APT mark/hold mismatch: {package}").format(package=package["name"]))


def _maybe_restore_kernel_metadata(target: str, log_cb: Callable[[str], None]):
    """Apply registration, then explicit initial integration without scripts."""
    approved_foreign = {kernel for _userspace, kernel in VERIFIED_NATIVE_MIXED_ARCHITECTURES}
    source_boot = os.path.join(get_live_source_mount(), "minios", "boot")
    external_payload = {}
    if os.path.isdir(source_boot):
        for name in os.listdir(source_boot):
            if name.startswith("vmlinuz-"):
                external_payload["/boot/" + name] = os.path.join(source_boot, name)
    registration = prepare_kernel_registration(
        target, allow_foreign_architectures=approved_foreign,
        external_payload_paths=external_payload,
    )
    try:
        version = registration.kernel_version
        kernel = _copy_native_kernel(target, version, False, log_cb)
        registration.apply(target)
        registration.verify_payload(target)
        foreign_architectures = sorted({
            package["architecture"] for package in registration.packages
            if package["architecture"] not in ("all", registration.native_architecture)
        })
        for architecture in foreign_architectures:
            _chroot(target, ["dpkg", "--add-architecture", architecture], log_cb)
        if not _target_has_executable(target, "/sbin/depmod", "/usr/sbin/depmod"):
            raise RuntimeError(_("depmod is required for format-1 kernel integration."))
        _chroot(target, ["depmod", "-a", version], log_cb)
        _update_kernel_symlinks(target, version, kernel, log_cb)
        initrd = _generate_native_initramfs(target, version, False, log_cb)
        _update_initramfs_symlink(target, initrd)
        _verify_registered_kernel(target, registration, log_cb)
        return registration, kernel, initrd
    except BaseException as exc:
        if registration.applied:
            registration.rollback(str(exc), target)
        registration.close()
        raise


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
    if not native_install_supported():
        raise RuntimeError(_("Full installation is not supported by this live image. Use live installation instead."))
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

    # The selected bundle is authoritative. Validate its complete format-1
    # registration contract before execute_plan can resize, format, or write.
    _preflight_reused_efi_variables(plan)
    _preflight_efi_payload_contract(plan.use_efi)
    _preflight_reused_efi_payload(plan)
    _preflight_selected_kernel(state, plan.use_efi, log_cb)

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
            mandatory = ["initramfs-tools"]
            package_result = preflight_package_download(mandatory)
            if not preflight_ok(package_result):
                raise RuntimeError(
                    _("The mandatory native kernel toolchain cannot be downloaded before disk modification.")
                )
            log_cb(_("Downloading the mandatory native kernel toolchain before modifying the disk..."))
            try:
                state.package_cache_path = prepare_package_cache(mandatory)
                _log_package_cache(state.package_cache_path, mandatory, log_cb)
            except Exception as exc:
                raise RuntimeError(
                    _("The mandatory native kernel toolchain could not be staged before disk modification: {error}").format(error=exc)
                )

    # Measure the real, selected overlay input after non-destructive package
    # staging but before execute_plan. Rebuild so its complete payload governs
    # every placement rather than the UI's advisory cache.
    bundle_bytes = preflight_selected_bundles(selected_modules=state.selected_modules)
    state.required_root_mib = max(state.required_root_mib, required_root_mib(bundle_bytes))
    efi_bytes = _regular_efi_tree_bytes(os.path.join(get_live_source_mount(), "EFI")) if plan.use_efi else 0
    plan = build_plan(
        layout, state.placement, state.filesystem, install_mode=state.install_mode,
        swap_size_mib=state.swap_size_mib, boot_layout=state.boot_layout,
        alongside_size_mib=state.alongside_size_mib, required_root_mib=state.required_root_mib,
        efi_payload_bytes=efi_bytes,
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
        registration, kernel, initrd = _maybe_restore_kernel_metadata(root_mount, log_cb)
        try:
            _install_native_bootloader(
                root_mount, state.target_device, root_part, plan.use_efi, esp_mount,
                progress_cb, log_cb, dry_run=dry_run,
                version=registration.kernel_version, kernel=kernel, initrd=initrd,
                esp_device=esp_part, reuse_esp=plan.reuse_esp,
            )
            registration.complete(root_mount)
        except BaseException as exc:
            registration.rollback(str(exc), root_mount)
            raise
        finally:
            registration.close()
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
