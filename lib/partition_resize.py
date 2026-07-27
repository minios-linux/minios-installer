#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import subprocess
from typing import Callable, Optional

from partition_models import DiskLayout, PartitionInfo, ResizeOperation


SUPPORTED_FILESYSTEMS = ("ext2", "ext3", "ext4", "ntfs")


def select_resize_candidate(layout: DiskLayout) -> PartitionInfo:
    if not layout.geometry_complete or not layout.size_sectors:
        raise ValueError("Exact partition geometry is unavailable")
    if layout.has_nested_layout or any(part.has_children for part in layout.partitions):
        raise ValueError("Nested partition layouts cannot be resized safely")
    if not layout.partitions:
        raise ValueError("No partition is available to resize")
    ordered = sorted(layout.partitions, key=lambda part: part.start_sector + part.size_sectors)
    candidate = None
    for index in range(len(ordered) - 1, -1, -1):
        part = ordered[index]
        if part.fstype.lower() not in SUPPORTED_FILESYSTEMS:
            continue
        trailing = ordered[index + 1:]
        if all(item.fstype.lower() in ("swap", "linux-swap", "linux-swap(v1)") for item in trailing):
            candidate = part
            break
    if candidate is None:
        raise ValueError("No supported tail partition is available to resize")
    if candidate.mountpoint:
        raise ValueError("The resize candidate is mounted")
    if not candidate.partition_number or not candidate.start_sector or not candidate.size_sectors:
        raise ValueError("Exact resize candidate geometry is unavailable")
    return candidate


def _default_output(command):
    return subprocess.check_output(command, universal_newlines=True, stderr=subprocess.STDOUT)


def probe_minimum_size_sectors(partition: PartitionInfo, sector_size: int,
                               output: Callable = _default_output) -> int:
    fs = partition.fstype.lower()
    if fs in ("ext2", "ext3", "ext4"):
        info = output(["dumpe2fs", "-h", partition.path])
        estimate = output(["resize2fs", "-P", partition.path])
        block_match = re.search(r"Block size:\s*(\d+)", info)
        count_match = re.search(r"minimum size of the filesystem(?: is|:)\s+(\d+)", estimate, re.I)
        if not block_match or not count_match:
            raise ValueError("Could not determine the minimum ext filesystem size")
        minimum_bytes = int(block_match.group(1)) * int(count_match.group(1))
    elif fs == "ntfs":
        info = output(["ntfsresize", "--info", partition.path])
        match = re.search(r"resize at\s+(\d+)\s+bytes", info, re.I)
        if not match:
            match = re.search(r"minimum.*?([0-9]+)\s+bytes", info, re.I)
        if not match:
            raise ValueError("Could not determine the minimum NTFS size")
        minimum_bytes = int(match.group(1))
    else:
        raise ValueError("Unsupported filesystem for resizing")
    return (minimum_bytes + sector_size - 1) // sector_size


def plan_shrink(layout: DiskLayout, required_mib: int,
                output: Callable = _default_output) -> ResizeOperation:
    candidate = select_resize_candidate(layout)
    required_sectors = (required_mib * 1024 * 1024 + layout.logical_sector_size - 1) // layout.logical_sector_size
    new_size = candidate.size_sectors - required_sectors
    minimum = probe_minimum_size_sectors(candidate, layout.logical_sector_size, output=output)
    # Minimum-size probes are theoretical. Keep material working space beyond them.
    alignment = max(1, (1024 * 1024) // layout.logical_sector_size)
    safety_mib = 1024 if candidate.fstype.lower() == "ntfs" else 256
    safety_sectors = (safety_mib * 1024 * 1024) // layout.logical_sector_size
    new_size = (new_size // alignment) * alignment
    if new_size < minimum + safety_sectors:
        raise ValueError("The last partition cannot be shrunk enough safely")
    return ResizeOperation(candidate.path, candidate.partition_number,
                           candidate.fstype.lower(), candidate.start_sector,
                           candidate.size_sectors, new_size,
                           layout.logical_sector_size)
