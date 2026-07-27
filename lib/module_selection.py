#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from typing import Iterable, List, Optional


LIVE_MINIOS_CANDIDATES = (
    "/run/initramfs/memory/data/minios",
    "/run/initramfs/memory/iso/minios",
    "/lib/live/mount/medium/minios",
    "/lib/live/mount/iso/minios",
)
DEFAULT_BUNDLES_DIR = "/run/initramfs/memory/bundles"
MIB = 1024 * 1024
INSTALL_SPACE_PERCENT = 125


def module_basename(path: str) -> str:
    return os.path.basename(path.rstrip(os.sep))


def list_live_module_names(minios_source: Optional[str] = None) -> List[str]:
    candidates = [minios_source] if minios_source else list(LIVE_MINIOS_CANDIDATES)
    for base in candidates:
        if not base or not os.path.isdir(base):
            continue
        names = sorted(name for name in os.listdir(base) if name.endswith(".sb") and os.path.isfile(os.path.join(base, name)))
        if names:
            return names
    return []


def list_bundle_module_names(base_dir: str = DEFAULT_BUNDLES_DIR) -> List[str]:
    if not os.path.isdir(base_dir):
        return []
    return sorted(name for name in os.listdir(base_dir) if name.endswith(".sb") and os.path.isdir(os.path.join(base_dir, name)))


def discover_module_names() -> List[str]:
    live = list_live_module_names()
    if live:
        return live
    return list_bundle_module_names()


def module_size_bytes(
    name: str,
    install_mode: str = "live",
    minios_source: Optional[str] = None,
    bundles_dir: str = DEFAULT_BUNDLES_DIR,
) -> Optional[int]:
    """Return storage used by a live image or expanded native module data."""
    if install_mode == "live":
        candidates = [minios_source] if minios_source else list(LIVE_MINIOS_CANDIDATES)
        for base in candidates:
            path = os.path.join(base, name) if base else ""
            if os.path.isfile(path):
                return os.path.getsize(path)
        return None

    bundle = os.path.join(bundles_dir, name)
    if not os.path.isdir(bundle):
        return None
    total = 0
    try:
        for root, _, files in os.walk(bundle):
            for filename in files:
                path = os.path.join(root, filename)
                if os.path.isfile(path):
                    total += os.path.getsize(path)
    except OSError:
        return None
    return total


def calculate_module_sizes(module_names: Iterable[str], install_mode: str = "live") -> dict:
    """Calculate module sizes once so callers can cache the potentially slow scan."""
    return {name: module_size_bytes(name, install_mode=install_mode) for name in module_names}


def selected_modules_size_bytes(selected_modules: Iterable[str], module_sizes: dict) -> Optional[int]:
    sizes = [module_sizes.get(name) for name in selected_modules]
    if any(size is None for size in sizes):
        return None
    return sum(sizes)


def payload_size_bytes(selected_modules: Iterable[str], install_mode: str = "live") -> Optional[int]:
    """Measure all selected payload, including live boot and metadata assets."""
    selected = set(selected_modules)
    if install_mode != "live":
        sizes = calculate_module_sizes(selected, install_mode=install_mode)
        return selected_modules_size_bytes(selected, sizes)
    for source in LIVE_MINIOS_CANDIDATES:
        if not os.path.isdir(source):
            continue
        total = 0
        try:
            for root, _, files in os.walk(source):
                for name in files:
                    path = os.path.join(root, name)
                    rel = os.path.relpath(path, source)
                    if os.path.dirname(rel) == "." and name.endswith(".sb") and name not in selected:
                        continue
                    total += os.path.getsize(path)
        except OSError:
            return None
        return total
    return None


def payload_overhead_bytes(module_names: Iterable[str], install_mode: str = "live") -> Optional[int]:
    """Return non-module live assets that every selected installation needs."""
    if install_mode != "live":
        return 0
    names = list(module_names)
    total = payload_size_bytes(names, install_mode=install_mode)
    modules = selected_modules_size_bytes(names, calculate_module_sizes(names, install_mode=install_mode))
    if total is None or modules is None:
        return None
    return max(0, total - modules)


def required_root_mib(module_bytes: int) -> int:
    """Selected data plus proportional working/filesystem headroom, rounded up."""
    if module_bytes is None or module_bytes < 0:
        raise ValueError("module size is unavailable")
    required_bytes = (module_bytes * INSTALL_SPACE_PERCENT + 99) // 100
    return max(1, (required_bytes + MIB - 1) // MIB)


def required_prefix_count(module_names: List[str]) -> int:
    for index, name in enumerate(module_names):
        if "kernel" in name.lower():
            return index + 1
    return 1 if module_names else 0


def parse_module_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_selected_modules(module_names: List[str], selected_modules: Optional[Iterable[str]] = None) -> List[str]:
    """Return a bootable prefix that includes dependencies below selected modules."""
    if not module_names:
        return []
    selected = [module_basename(item) for item in (selected_modules or []) if item]
    if not selected:
        return list(module_names)
    index_by_name = {name: idx for idx, name in enumerate(module_names)}
    unknown = [name for name in selected if name not in index_by_name]
    if unknown:
        raise ValueError("Unknown module(s): " + ", ".join(unknown))
    highest = max(index_by_name[name] for name in selected)
    highest = max(highest, required_prefix_count(module_names) - 1)
    return module_names[: highest + 1]


def selected_module_set(module_names: List[str], selected_modules: Optional[Iterable[str]] = None) -> set:
    return set(normalize_selected_modules(module_names, selected_modules))
