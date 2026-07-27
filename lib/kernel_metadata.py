#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import shutil
from typing import Callable, List


KERNEL_METADATA_PATH = os.path.join("usr", "share", "minios", "kernel-dpkg")


def _read_status_stanzas(status_path: str) -> List[str]:
    if not os.path.exists(status_path):
        return []
    with open(status_path, "r", encoding="utf-8", errors="ignore") as fh:
        return [stanza for stanza in fh.read().split("\n\n") if stanza.strip()]


def _status_has_package(status_path: str, package: str) -> bool:
    needle = f"Package: {package}"
    for stanza in _read_status_stanzas(status_path):
        if needle in stanza.splitlines():
            return True
    return False


def _append_status_stanza(status_path: str, stanza_path: str) -> None:
    with open(stanza_path, "r", encoding="utf-8", errors="ignore") as fh:
        stanza = fh.read().strip()
    if not stanza:
        return
    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, "a+", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.write("\n\n")
        fh.write(stanza)
        fh.write("\n")


def restore_kernel_dpkg_metadata(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> int:
    metadata_dir = os.path.join(target, KERNEL_METADATA_PATH)
    manifest_path = os.path.join(metadata_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        log_cb("No preserved kernel dpkg metadata found.")
        return 0

    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    packages = manifest.get("packages") or []
    if not packages:
        info_dir = os.path.join(metadata_dir, "info")
        if os.path.isdir(info_dir) and any(name.endswith(".list") for name in os.listdir(info_dir)):
            log_cb("Warning: preserved kernel metadata is incomplete: manifest has no packages.")
        return 0

    status_path = os.path.join(target, "var", "lib", "dpkg", "status")
    info_src = os.path.join(metadata_dir, "info")
    info_dst = os.path.join(target, "var", "lib", "dpkg", "info")
    restored = 0

    for entry in packages:
        package = entry.get("name") if isinstance(entry, dict) else ""
        if not package:
            continue
        if _status_has_package(status_path, package):
            log_cb(f"Kernel package metadata already present: {package}")
            continue
        stanza_path = os.path.join(metadata_dir, "status.d", f"{package}.status")
        if not os.path.exists(stanza_path):
            log_cb(f"Warning: missing preserved status for {package}")
            continue
        log_cb(f"Restoring kernel package metadata: {package}")
        if dry_run:
            restored += 1
            continue
        _append_status_stanza(status_path, stanza_path)
        os.makedirs(info_dst, exist_ok=True)
        if os.path.isdir(info_src):
            for name in os.listdir(info_src):
                if name.startswith(package + "."):
                    shutil.copy2(os.path.join(info_src, name), os.path.join(info_dst, name))
        restored += 1

    return restored
