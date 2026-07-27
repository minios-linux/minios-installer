#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from partition_executor import execute_plan
from partition_models import PartitionPlan, PlannedPartition
from partition_executor import _verify_existing_partition


def test_executor_dry_run_uses_planned_offsets_for_non_wipe_plan():
    logs = []
    plan = PartitionPlan(
        device="/dev/sdb",
        use_gpt=True,
        wipe_disk=False,
        partitions=[PlannedPartition("create", "minios_root", 21025, 45000, "ext4", path="/dev/sdb3")],
    )
    root_part, esp_part, root_mount, esp_mount = execute_plan(plan, logs.append, dry_run=True)
    assert root_part == "/dev/sdb3"
    assert esp_part is None
    assert root_mount.endswith("/sdb3")
    assert esp_mount is None
    assert any("21025MiB" in line and "45000MiB" in line for line in logs)
    assert not any("mklabel" in line for line in logs)


def test_executor_dry_run_reuses_existing_esp_without_formatting_it():
    logs = []
    plan = PartitionPlan(
        device="/dev/sdb",
        use_gpt=True,
        use_efi=True,
        wipe_disk=False,
        reuse_esp=True,
        esp_path="/dev/sdb1",
        partitions=[
            PlannedPartition("reuse", "esp", 1, 513, "vfat", path="/dev/sdb1"),
            PlannedPartition("create", "minios_root", 21025, 45000, "ext4", path="/dev/sdb3"),
        ],
    )
    root_part, esp_part, root_mount, esp_mount = execute_plan(plan, logs.append, dry_run=True)
    assert root_part == "/dev/sdb3"
    assert esp_part == "/dev/sdb1"
    assert esp_mount.endswith("/sdb1")
    assert not any("mkpart ESP" in line for line in logs)


def test_bios_plan_refuses_reused_esp():
    plan = PartitionPlan(device="/dev/sdb", use_gpt=False, use_efi=False, wipe_disk=False,
                         partitions=[PlannedPartition("reuse", "esp", 1, 101, "fat32", path="/dev/sdb1"),
                                     PlannedPartition("create", "minios_root", 101, 200, "ext4", path="/dev/sdb2")])
    with pytest.raises(RuntimeError, match="BIOS"):
        execute_plan(plan, lambda _msg: None, dry_run=True)


def test_erase_rejects_active_mapped_stack():
    from partition_executor import _prepare_erase_target
    with patch("partition_executor._target_block_names", return_value={"sdb", "sdb1"}), \
            patch("partition_executor.subprocess.check_output", return_value="disk\ncrypt\n"):
        with pytest.raises(RuntimeError, match="mapped storage stack"):
            _prepare_erase_target("/dev/sdb", lambda _msg: None, False)


def test_preserve_revalidation_rejects_changed_free_extent():
    from partition_executor import _revalidate_preserve_plan
    from partition_models import DiskLayout, FreeExtent
    plan = PartitionPlan("/dev/sdb", True, False, expected_partition_table="gpt",
                         expected_partitions=[], expected_free_extent=(100, 200))
    with patch("partition_scanner.scan_disk", return_value=DiskLayout("/dev/sdb", 500, partition_table="gpt", free_extents=[FreeExtent(101, 200)])):
        with pytest.raises(RuntimeError, match="free extent"):
            _revalidate_preserve_plan(plan)


def test_executor_by_id_partition_path_uses_part_suffix_index_for_boot_flag():
    logs = []
    device = "/dev/disk/by-id/ata-VBOX_HARDDISK_VB6a21814d-8b698d04"
    plan = PartitionPlan(
        device=device,
        use_gpt=False,
        wipe_disk=True,
        partitions=[
            PlannedPartition(
                "create",
                "minios_root",
                1,
                20379,
                "ext4",
                path=f"{device}-part1",
            ),
            PlannedPartition(
                "create",
                "esp",
                20379,
                20479,
                "fat32",
                path=f"{device}-part2",
            ),
        ],
    )

    root_part, esp_part, _root_mount, _esp_mount = execute_plan(plan, logs.append, dry_run=True)

    assert root_part == f"{device}-part1"
    assert esp_part == f"{device}-part2"
    assert any(f"parted -s {device} set 1 boot on" == line[2:] for line in logs)
    assert not any(" set 41 boot on" in line for line in logs)


def test_executor_marks_mbr_esp_for_uefi_without_bios_root_flag():
    logs = []
    plan = PartitionPlan(
        device="/dev/sdb",
        use_gpt=False,
        use_efi=True,
        wipe_disk=True,
        partitions=[
            PlannedPartition("create", "minios_root", 1, 20379, "ext4", path="/dev/sdb1"),
            PlannedPartition("create", "esp", 20379, 20479, "fat32", path="/dev/sdb2"),
        ],
    )

    execute_plan(plan, logs.append, dry_run=True)

    assert any("parted -s /dev/sdb set 2 esp on" == line[2:] for line in logs)
    assert not any(" set 1 boot on" in line for line in logs)


def test_existing_partition_must_still_belong_to_resolved_target():
    plan = PartitionPlan(device="/dev/disk/by-id/target", use_gpt=True, wipe_disk=False)
    with patch("partition_executor.os.path.exists", return_value=True), \
            patch("partition_executor.os.path.realpath", side_effect=["/dev/sda", "/dev/sdb"]), \
            patch("partition_executor.subprocess.check_output", return_value="sdb\n"):
        with pytest.raises(RuntimeError, match="selected target"):
            _verify_existing_partition(plan, "/dev/sdb1")


def test_existing_esp_must_remain_fat_filesystem():
    plan = PartitionPlan(device="/dev/sda", use_gpt=True, wipe_disk=False)
    with patch("partition_executor.os.path.exists", return_value=True), \
            patch("partition_executor.os.path.realpath", return_value="/dev/sda"), \
            patch("partition_executor.subprocess.check_output", side_effect=["sda\n", "ext4\n"]):
        with pytest.raises(RuntimeError, match="expected filesystem"):
            _verify_existing_partition(plan, "/dev/sda1", "fat32")
