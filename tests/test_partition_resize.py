#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from partition_models import (DiskLayout, PartitionInfo, ResizeOperation,
                              PartitionPlan, PlannedPartition,
                              PLACEMENT_ALONGSIDE_OS)
from partition_planner import build_plan
from partition_resize import plan_shrink, select_resize_candidate
from partition_executor import execute_plan
from partition_scanner import scan_disk


MIB_SECTORS = 2048


def layout_with_last(fstype="ext4", mounted=False, nested=False):
    return DiskLayout(
        device="/dev/sda", size_mib=30000, partition_table="msdos",
        logical_sector_size=512, size_sectors=30000 * MIB_SECTORS,
        has_nested_layout=nested,
        partitions=[
            PartitionInfo("sda1", "/dev/sda1", 1000, 1, 1001, "vfat",
                          start_sector=MIB_SECTORS, size_sectors=1000 * MIB_SECTORS,
                          partition_number=1),
            PartitionInfo("sda2", "/dev/sda2", 25000, 1001, 26001, fstype,
                          mountpoint="/mnt" if mounted else "",
                          start_sector=1001 * MIB_SECTORS,
                          size_sectors=25000 * MIB_SECTORS,
                          partition_number=2, has_children=nested),
        ])


def ext_probe(command):
    if command[0] == "dumpe2fs":
        return "Block size: 4096\n"
    return "Estimated minimum size of the filesystem: 2000000\n"


def test_selects_supported_tail_partition():
    assert select_resize_candidate(layout_with_last()).path == "/dev/sda2"
    for kwargs in ({"fstype": "xfs"}, {"mounted": True}, {"nested": True}):
        with pytest.raises(ValueError):
            select_resize_candidate(layout_with_last(**kwargs))


def test_selects_supported_partition_before_trailing_swap():
    layout = layout_with_last()
    layout.partitions.append(
        PartitionInfo(
            "sda3", "/dev/sda3", 2000, 26001, 28001, "swap",
            start_sector=26001 * MIB_SECTORS,
            size_sectors=2000 * MIB_SECTORS,
            partition_number=3,
        )
    )

    assert select_resize_candidate(layout).path == "/dev/sda2"


def test_native_alongside_keeps_trailing_swap_candidate_compatible():
    layout = layout_with_last()
    layout.partition_table = "gpt"
    layout.partitions[0].role = "esp"
    layout.partitions[0].parttype = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    layout.partitions[0].partuuid = "existing-esp"
    layout.partitions.append(
        PartitionInfo(
            "sda3", "/dev/sda3", 2000, 26001, 28001, "swap",
            start_sector=26001 * MIB_SECTORS,
            size_sectors=2000 * MIB_SECTORS,
            partition_number=3,
        )
    )
    resize = plan_shrink(layout, 5121, output=ext_probe)
    with patch("partition_planner.plan_shrink", return_value=resize), \
            patch("partition_planner.is_uefi_system", return_value=True):
        plan = build_plan(
            layout, PLACEMENT_ALONGSIDE_OS, "ext4", install_mode="native",
            swap_size_mib=4096, required_root_mib=1024,
        )
    assert plan.resize.path == "/dev/sda2"
    assert [part.role for part in plan.partitions] == ["esp", "minios_root", "swap"]


def test_rejects_supported_partition_before_trailing_data_partition():
    layout = layout_with_last()
    layout.partitions.append(
        PartitionInfo(
            "sda3", "/dev/sda3", 2000, 26001, 28001, "xfs",
            start_sector=26001 * MIB_SECTORS,
            size_sectors=2000 * MIB_SECTORS,
            partition_number=3,
        )
    )

    with pytest.raises(ValueError):
        select_resize_candidate(layout)


def test_ext_shrink_is_sector_exact_and_keeps_minimum_margin():
    operation = plan_shrink(layout_with_last(), 8192, output=ext_probe)
    assert operation.old_size_sectors == 25000 * MIB_SECTORS
    assert operation.new_size_sectors == (25000 - 8192) * MIB_SECTORS
    assert operation.start_sector == 1001 * MIB_SECTORS


def test_ntfs_probe_parses_minimum_bytes():
    operation = plan_shrink(
        layout_with_last("ntfs"), 8192,
        output=lambda command: "You might resize at 7000000000 bytes or 7000 MB")
    assert operation.fstype == "ntfs"


def test_alongside_plan_shrinks_then_uses_allocator():
    layout = layout_with_last()
    with patch("partition_resize._default_output", side_effect=ext_probe), \
            patch("partition_planner.is_uefi_system", return_value=False), \
            patch("partition_planner.plan_shrink") as shrink:
        shrink.return_value = plan_shrink(layout, 8292, output=ext_probe)
        plan = build_plan(layout, PLACEMENT_ALONGSIDE_OS, "ext4")
    assert plan.resize.path == "/dev/sda2"
    assert [part.role for part in plan.partitions] == ["minios_root", "esp"]
    assert plan.partitions[0].start_mib >= 1001


def test_alongside_allocator_never_extends_past_released_sector_boundary():
    layout = layout_with_last()
    resize = plan_shrink(layout, 1125, output=ext_probe)
    with patch("partition_planner.plan_shrink", return_value=resize), \
            patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ALONGSIDE_OS, "ext4", required_root_mib=1024)
    released_end = (plan.resize.start_sector + plan.resize.old_size_sectors) * plan.resize.sector_size
    assert max(part.end_mib for part in plan.partitions if part.action == "create") * 1024 * 1024 <= released_end


def test_ext_executor_dry_run_orders_resize_before_partition_creation():
    logs = []
    resize = ResizeOperation("/dev/sda2", 2, "ext4", 2048, 40000000,
                             20000000, 512)
    plan = PartitionPlan("/dev/sda", False, False,
                         [PlannedPartition("create", "minios_root", 10000,
                                           19000, "ext4", path="/dev/sda3")],
                         resize=resize)
    execute_plan(plan, logs.append, dry_run=True)
    commands = [line for line in logs if line.startswith("$ ")]
    assert commands[0].startswith("$ e2fsck")
    assert commands[1].startswith("$ resize2fs")
    assert commands[2].startswith("$ e2fsck")
    assert commands[3].startswith("$ sfdisk --no-reread -N 2")
    assert commands[4].startswith("$ parted -s /dev/sda mkpart")


def test_ntfs_executor_uses_check_no_action_resize_then_boundary():
    logs = []
    resize = ResizeOperation("/dev/sda2", 2, "ntfs", 2048, 40000000,
                             20000000, 512)
    plan = PartitionPlan("/dev/sda", False, False,
                         [PlannedPartition("create", "minios_root", 10000,
                                           19000, "ext4", path="/dev/sda3")],
                         resize=resize)
    execute_plan(plan, logs.append, dry_run=True)
    text = "\n".join(logs)
    assert text.index("ntfsresize --check") < text.index("ntfsresize --no-action")
    assert "ntfsresize --force" not in text
    assert text.index("ntfsresize --no-action") < text.index("ntfsresize --size")
    assert text.index("ntfsresize --size") < text.index("sfdisk --no-reread")


def test_real_resize_verifies_geometry_before_mkpart():
    resize = ResizeOperation("/dev/sda2", 2, "ext4", 2048, 40000000,
                             20000000, 512)
    plan = PartitionPlan("/dev/sda", False, False,
                         [PlannedPartition("create", "minios_root", 10000,
                                           19000, "ext4", path="/dev/sda3")],
                         resize=resize)
    events = []

    class Result(object):
        returncode = 0
        stdout = ""

    def fake_run(command, **kwargs):
        events.append(command)
        return Result()

    geometry_checks = ["2048 20480000000\n", "2048 10240000000\n"]

    def fake_check_output(command, **kwargs):
        events.append(command)
        return geometry_checks.pop(0)

    with patch("partition_executor.run_command", side_effect=lambda command, message: events.append(command)), \
            patch("partition_executor.subprocess.run", side_effect=fake_run), \
            patch("partition_executor.subprocess.check_output", side_effect=fake_check_output), \
            patch("partition_executor._verify_existing_partition"), \
            patch("partition_executor._wait_for_partitions"), \
            patch("partition_executor.format_partitions"), \
            patch("partition_executor.mount_partition"):
        execute_plan(plan, lambda message: None)
    verify = next(i for i, command in enumerate(events) if command[:2] == ["lsblk", "-b"])
    create = next(i for i, command in enumerate(events) if command[:3] == ["parted", "-s", "/dev/sda"])
    assert verify < create


def test_scanner_records_exact_sector_geometry():
    disk = {
        "name": "sda", "size": str(4096 * 100000), "type": "disk",
        "log-sec": "4096", "pttype": "gpt",
        "children": [{"name": "sda1", "size": str(4096 * 90000),
                      "start": "2048", "partn": "1", "type": "part",
                      "fstype": "ext4", "mountpoint": ""}],
    }
    with patch("partition_scanner._run_lsblk", return_value=disk):
        layout = scan_disk("/dev/sda")
    assert layout.logical_sector_size == 4096
    assert layout.size_sectors == 100000
    assert layout.partitions[0].start_sector == 2048
    assert layout.partitions[0].size_sectors == 90000
    assert layout.geometry_complete is False


def test_scanner_recognizes_gpt_efi_partition_type():
    disk = {
        "name": "sda", "size": str(512 * 100000), "type": "disk",
        "log-sec": "512", "pttype": "gpt",
        "children": [{
            "name": "sda1", "size": str(512 * 204800), "start": "2048",
            "partn": "1", "type": "part", "fstype": "vfat", "mountpoint": "",
            "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
        }],
    }
    with patch("partition_scanner._run_lsblk", return_value=disk):
        layout = scan_disk("/dev/sda")
    assert layout.partitions[0].role == "esp"
    assert layout.has_efi is True
