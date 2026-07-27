#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import shutil
import socket
import subprocess
import tempfile
from typing import Dict, Iterable, List


MIN_DOWNLOAD_SPACE_MIB = 512

FILESYSTEM_RESIZE_TOOLS = {
    "ext2": {"e2fsprogs": ("e2fsck", "resize2fs")},
    "ext3": {"e2fsprogs": ("e2fsck", "resize2fs")},
    "ext4": {"e2fsprogs": ("e2fsck", "resize2fs")},
    "ntfs": {"ntfs-3g": ("ntfsresize",)},
}

PARTITION_RESIZE_TOOLS = {
    "fdisk": ("sfdisk",),
    "parted": ("parted",),
}


def package_installed(package: str, root: str = "/") -> bool:
    status_path = os.path.join(root, "var", "lib", "dpkg", "status")
    if root != "/" and os.path.exists(status_path):
        with open(status_path, "r", encoding="utf-8", errors="ignore") as fh:
            stanza = []
            for line in fh:
                if line.strip():
                    stanza.append(line.rstrip("\n"))
                    continue
                if _status_stanza_installed(stanza, package):
                    return True
                stanza = []
            return _status_stanza_installed(stanza, package)
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", package],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
        check=False,
    )
    return result.returncode == 0 and "install ok installed" in result.stdout


def package_installed_or_provided(package: str, root: str = "/") -> bool:
    status_path = os.path.join(root, "var", "lib", "dpkg", "status")
    if root != "/" and os.path.exists(status_path):
        for stanza in _status_stanzas(status_path):
            if not any(line == "Status: install ok installed" for line in stanza):
                continue
            if any(line == f"Package: {package}" for line in stanza):
                return True
            for line in stanza:
                if not line.startswith("Provides: "):
                    continue
                provided = [item.strip().split()[0] for item in line[len("Provides: ") :].split(",")]
                if package in provided:
                    return True
        return False
    if package_installed(package, root=root):
        return True
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}\t${Provides}\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if not line.startswith("install ok installed\t"):
            continue
        provided = [item.strip().split()[0] for item in line.split("\t", 1)[1].split(",") if item.strip()]
        if package in provided:
            return True
    return False


def has_installed_package_prefix(prefix: str, root: str = "/") -> bool:
    status_path = os.path.join(root, "var", "lib", "dpkg", "status")
    if root != "/" and os.path.exists(status_path):
        for stanza in _status_stanzas(status_path):
            if not any(line == "Status: install ok installed" for line in stanza):
                continue
            if any(line.startswith(f"Package: {prefix}") for line in stanza):
                return True
        return False
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Status}\n", f"{prefix}*"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
        check=False,
    )
    return any("\tinstall ok installed" in line for line in result.stdout.splitlines())


def _status_stanzas(status_path: str) -> Iterable[List[str]]:
    with open(status_path, "r", encoding="utf-8", errors="ignore") as fh:
        stanza = []
        for line in fh:
            if line.strip():
                stanza.append(line.rstrip("\n"))
                continue
            if stanza:
                yield stanza
            stanza = []
        if stanza:
            yield stanza


def _status_stanza_installed(stanza: List[str], package: str) -> bool:
    return any(line == f"Package: {package}" for line in stanza) and any(line == "Status: install ok installed" for line in stanza)


def missing_packages(packages: Iterable[str], root: str = "/") -> List[str]:
    return [package for package in packages if not package_installed(package, root=root)]


def has_initramfs_generator(root: str = "/") -> bool:
    for rel in ("usr/sbin/update-initramfs", "usr/bin/dracut", "usr/sbin/dracut"):
        if os.path.exists(os.path.join(root, rel)):
            return True
    return False


def native_package_requirements(use_efi: bool, filesystem: str, root: str = "/", alongside: bool = False) -> List[str]:
    packages: List[str] = []
    if use_efi:
        packages.extend(["grub-efi-amd64", "grub-common", "efibootmgr"])
    else:
        packages.extend(["grub-pc", "grub-common"])

    if not has_initramfs_generator(root=root):
        packages.append("initramfs-tools")
    elif has_installed_package_prefix("linux-image", root=root) and not package_installed_or_provided("linux-initramfs-tool", root=root):
        packages.append("initramfs-tools")
    if filesystem == "btrfs":
        packages.append("btrfs-progs")
    elif filesystem in ("ext2", "ext4"):
        packages.append("e2fsprogs")
    if use_efi:
        packages.append("dosfstools")
    if alongside:
        packages.append("os-prober")
    seen = set()
    result = []
    for package in packages:
        if package not in seen:
            seen.add(package)
            result.append(package)
    return result


def native_missing_packages(use_efi: bool, filesystem: str, root: str = "/", alongside: bool = False) -> List[str]:
    return missing_packages(native_package_requirements(use_efi, filesystem, root=root, alongside=alongside), root=root)


def manual_native_package_requirements(use_efi: bool, filesystem: str, alongside: bool = False) -> List[str]:
    """Return packages that must be staged for a manual native target.

    The target does not exist yet, so host package state cannot establish that
    its copied bundle will contain a bootable GRUB and initramfs toolchain.
    """
    packages = ["grub-efi-amd64", "grub-common", "efibootmgr"] if use_efi else ["grub-pc", "grub-common"]
    packages.append("initramfs-tools")
    if filesystem == "btrfs":
        packages.append("btrfs-progs")
    elif filesystem in ("ext2", "ext4"):
        packages.append("e2fsprogs")
    if use_efi:
        packages.append("dosfstools")
    if alongside:
        packages.append("os-prober")
    return list(dict.fromkeys(packages))


def native_requires_standard_bootloader(use_efi: bool, placement: str) -> bool:
    """EFI and preserve-layout installs cannot use the single-system EXTLINUX fallback."""
    return use_efi or placement != "erase_all"


def prepare_package_cache(packages: Iterable[str]) -> str:
    """Download packages and current APT indexes before any disk is modified."""
    package_list = list(dict.fromkeys(packages))
    cache = tempfile.mkdtemp(prefix="minios-installer-apt-")
    archives = os.path.join(cache, "archives")
    os.makedirs(os.path.join(archives, "partial"))
    # APT drops privileges to _apt while downloading.
    os.chmod(cache, 0o755)
    os.chmod(archives, 0o755)
    partial = os.path.join(archives, "partial")
    os.chmod(partial, 0o700)
    try:
        shutil.chown(partial, user="_apt")
    except (LookupError, OSError):
        pass
    env = dict(
        os.environ,
        DEBIAN_FRONTEND="noninteractive",
        APT_LISTCHANGES_FRONTEND="none",
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
    )
    # Resolve against an empty dpkg status so APT stages the complete target
    # dependency closure instead of trusting packages installed only in live.
    isolated_state = ["-o", "Dir::State::status=/dev/null", "-o", "Debug::NoLocking=1"]
    try:
        subprocess.run(["apt-get", "update"], env=env, check=True)
        subprocess.run(
            ["apt-get"] + isolated_state + ["-o", "Dir::Cache::archives=" + archives,
              "--download-only", "install", "-y", "--no-install-recommends"] + package_list,
            env=env,
            check=True,
        )
        # Verify that APT can resolve the complete dependency closure using
        # only the staged archives, before any target disk changes begin.
        subprocess.run(
            ["apt-get"] + isolated_state + ["-o", "Dir::Cache::archives=" + archives,
              "--no-download", "--download-only", "install", "-y", "--no-install-recommends"] + package_list,
            env=env,
            check=True,
        )
        lists = os.path.join(cache, "lists")
        shutil.copytree("/var/lib/apt/lists", lists, symlinks=True)
        return cache
    except Exception:
        shutil.rmtree(cache, ignore_errors=True)
        raise


def package_cache_summary(cache: str) -> tuple:
    archives = os.path.join(cache, "archives")
    packages = []
    try:
        packages = [
            os.path.join(archives, name)
            for name in os.listdir(archives)
            if name.endswith(".deb") and os.path.isfile(os.path.join(archives, name))
        ]
    except OSError:
        return 0, 0
    return len(packages), sum(os.path.getsize(path) for path in packages)


def resize_package_requirements(filesystem: str, partition_resize: bool = True) -> List[str]:
    """Return packages needed to resize a filesystem and, optionally, its partition."""
    tools = FILESYSTEM_RESIZE_TOOLS.get(filesystem.lower(), {})
    packages = list(tools)
    if partition_resize:
        packages.extend(PARTITION_RESIZE_TOOLS)
    return packages


def resize_required_binaries(filesystem: str, partition_resize: bool = True) -> List[str]:
    """Return the binaries provided by the resize package requirements."""
    tools = FILESYSTEM_RESIZE_TOOLS.get(filesystem.lower(), {})
    binaries = [binary for package_binaries in tools.values() for binary in package_binaries]
    if partition_resize:
        binaries.extend(binary for package_binaries in PARTITION_RESIZE_TOOLS.values() for binary in package_binaries)
    return binaries


def resize_missing_packages(filesystem: str, partition_resize: bool = True, root: str = "/") -> List[str]:
    """Return resize packages that are not installed."""
    return missing_packages(resize_package_requirements(filesystem, partition_resize), root=root)


def resize_package_preflight(filesystem: str, partition_resize: bool = True) -> Dict[str, object]:
    """Return download readiness for missing resize packages."""
    return preflight_package_download(resize_package_requirements(filesystem, partition_resize))


def has_internet(timeout: float = 3.0) -> bool:
    for host in ("deb.debian.org", "deb.devuan.org", "archive.ubuntu.com", "1.1.1.1"):
        try:
            socket.create_connection((host, 80), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def free_space_mib(path: str = "/var/cache/apt/archives") -> int:
    check_path = path if os.path.exists(path) else "/"
    usage = shutil.disk_usage(check_path)
    return int(usage.free // (1024 * 1024))


def preflight_package_download(packages: Iterable[str], min_space_mib: int = MIN_DOWNLOAD_SPACE_MIB) -> Dict[str, object]:
    missing = missing_packages(packages)
    return {
        "missing": missing,
        "apt_available": shutil.which("apt-get") is not None,
        "internet": has_internet() if missing else True,
        "free_space_mib": free_space_mib(),
        "min_space_mib": min_space_mib,
    }


def preflight_ok(result: Dict[str, object]) -> bool:
    if not result.get("missing"):
        return True
    return bool(result.get("apt_available")) and bool(result.get("internet")) and int(result.get("free_space_mib") or 0) >= int(result.get("min_space_mib") or 0)
