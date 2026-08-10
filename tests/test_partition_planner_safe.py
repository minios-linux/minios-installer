#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from partition_models import DiskLayout, PartitionInfo, PLACEMENT_ERASE_ALL, PLACEMENT_FREE_SPACE
from partition_planner import build_plan, _next_partition_index, plan_free_space


def test_erase_all_plan_uses_sequential_non_overlapping_offsets():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
    created = [p for p in plan.partitions if p.action == "create"]
    assert plan.wipe_disk is True
    assert plan.use_gpt is False
    assert created[0].start_mib == 1
    for previous, current in zip(created, created[1:]):
        assert previous.end_mib == current.start_mib
    assert created[-1].end_mib <= layout.size_mib - 1
    assert all(p.role != "swap" for p in created)
    assert created[-1].role == "esp"
    assert created[-1].size_mib == 100


def test_dynamic_root_requirement_replaces_fixed_eight_gib_floor():
    small_layout = DiskLayout(device="/dev/sdb", size_mib=4096, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(
            small_layout,
            PLACEMENT_ERASE_ALL,
            "ext4",
            install_mode="native",
            boot_layout="bios_mbr",
            required_root_mib=3000,
        )
    assert plan.partitions[0].size_mib > 3000


def test_dynamic_root_requirement_rejects_disk_for_selected_modules():
    layout = DiskLayout(device="/dev/sdb", size_mib=4096, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        try:
            build_plan(
                layout,
                PLACEMENT_ERASE_ALL,
                "ext4",
                install_mode="native",
                boot_layout="bios_mbr",
                required_root_mib=5000,
            )
        except ValueError as exc:
            assert "5000 MiB" in str(exc)
        else:
            raise AssertionError("dynamic module requirement must be enforced")


def test_erase_all_fat32_plan_does_not_create_esp():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "fat32")
    assert [p.role for p in plan.partitions] == ["minios_root"]
    assert plan.use_gpt is False


def test_native_bios_erase_does_not_create_unbootable_esp():
    layout = DiskLayout(device="/dev/sdb", size_mib=32768, partition_table="msdos")
    plan = build_plan(
        layout,
        PLACEMENT_ERASE_ALL,
        "ext4",
        install_mode="native",
        boot_layout="bios_mbr",
    )
    assert [part.role for part in plan.partitions] == ["minios_root"]


def test_native_uefi_mbr_erase_creates_efi_system_partition():
    layout = DiskLayout(device="/dev/sdb", size_mib=32768, partition_table="msdos")
    plan = build_plan(
        layout,
        PLACEMENT_ERASE_ALL,
        "ext4",
        install_mode="native",
        boot_layout="uefi_mbr",
    )
    assert plan.use_gpt is False
    assert plan.use_efi is True
    assert [part.role for part in plan.partitions] == ["esp", "minios_root"]


def test_erase_all_uefi_blank_disk_uses_gpt():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="")
    with patch("partition_planner.is_uefi_system", return_value=True):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
    assert plan.use_gpt is True
    assert plan.partitions[-1].role == "esp"


def test_erase_all_forced_uefi_gpt_uses_gpt_under_bios():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4", boot_layout="uefi_gpt")
    assert plan.use_gpt is True
    assert plan.partitions[-1].role == "esp"


def test_erase_all_forced_bios_mbr_uses_mbr_under_uefi():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="gpt")
    with patch("partition_planner.is_uefi_system", return_value=True):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4", boot_layout="bios_mbr")
    assert plan.use_gpt is False


def test_erase_all_bios_existing_gpt_uses_mbr():
    """BIOS erase-all must not inherit pre-wipe GPT (bootloader would be skipped)."""
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="gpt")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
    assert plan.use_gpt is False
    assert plan.wipe_disk is True


def test_erase_all_over_2tib_on_bios_is_refused():
    """BIOS + disk >= 2 TiB must refuse (would create unbootable GPT without BIOS bootloader)."""
    layout = DiskLayout(device="/dev/sdb", size_mib=2_097_152, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        try:
            build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
        except ValueError as exc:
            assert "BIOS" in str(exc) or "2 TiB" in str(exc)
        else:
            assert False, "expected refusal for BIOS + oversized disk"


def test_erase_all_bios_rejects_ntfs():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        try:
            build_plan(layout, PLACEMENT_ERASE_ALL, "ntfs", install_mode="native")
        except ValueError as exc:
            assert "ntfs" in str(exc).lower() or "BIOS" in str(exc) or "EXTLINUX" in str(exc)
        else:
            assert False, "expected refusal for BIOS + NTFS"


def test_erase_all_native_rejects_fat32_root():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        try:
            build_plan(layout, PLACEMENT_ERASE_ALL, "fat32", install_mode="native")
        except ValueError as exc:
            assert "fat32" in str(exc).lower() or "native" in str(exc).lower()
        else:
            assert False, "expected refusal for native FAT32 root"


def test_erase_all_native_allows_btrfs_root():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "btrfs", install_mode="native")
    assert plan.partitions[0].fstype == "btrfs"


def test_erase_all_bios_live_keeps_ntfs_available_for_legacy_mode():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ntfs", install_mode="live")
    assert plan.use_gpt is False


def test_summary_lines_are_translatable():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
    lines = plan.summary_lines()
    assert any("Erase /dev/sdb" in line for line in lines)
    assert any("esp" in line for line in lines)


def test_free_space_plan_reports_when_no_free_space():
    layout = DiskLayout(device="/dev/sdb", size_mib=20480, partition_table="msdos")
    try:
        build_plan(layout, PLACEMENT_FREE_SPACE, "ext4")
    except ValueError as exc:
        assert "free space" in str(exc).lower()
    else:
        raise AssertionError("free-space placement must require a suitable free extent")


def test_free_space_plan_creates_partitions_without_wiping_table():
    from partition_models import FreeExtent

    layout = DiskLayout(
        device="/dev/sdb",
        size_mib=40960,
        partition_table="msdos",
        free_extents=[FreeExtent(20000, 40959)],
    )
    with patch("partition_planner.is_uefi_system", return_value=False):
        plan = build_plan(layout, PLACEMENT_FREE_SPACE, "ext4")
    assert plan.wipe_disk is False
    assert plan.partitions[0].role == "minios_root"


def test_free_space_rejects_uefi_gpt_on_existing_mbr():
    from partition_models import FreeExtent

    layout = DiskLayout(device="/dev/sdb", size_mib=40960, partition_table="msdos", free_extents=[FreeExtent(20000, 40959)])
    try:
        build_plan(layout, PLACEMENT_FREE_SPACE, "ext4", boot_layout="uefi_gpt")
    except ValueError as exc:
        assert "UEFI/GPT" in str(exc)
    else:
        assert False, "expected layout mismatch refusal"


def test_free_space_rejects_existing_gpt_under_bios_auto():
    from partition_models import FreeExtent

    layout = DiskLayout(device="/dev/sdb", size_mib=40960, partition_table="gpt", free_extents=[FreeExtent(20000, 40959)])
    with patch("partition_planner.is_uefi_system", return_value=False):
        try:
            build_plan(layout, PLACEMENT_FREE_SPACE, "ext4", boot_layout="auto")
        except ValueError as exc:
            assert "GPT free space" in str(exc)
        else:
            assert False, "expected BIOS/GPT free-space refusal"


def test_next_partition_index_handles_sparse_numbers():
    layout = DiskLayout(
        device="/dev/sda",
        size_mib=20480,
        partitions=[
            PartitionInfo(name="sda1", path="/dev/sda1", size_mib=100, start_mib=1, end_mib=101),
            PartitionInfo(name="sda3", path="/dev/sda3", size_mib=100, start_mib=200, end_mib=300),
        ],
    )
    assert _next_partition_index(layout) == 2


def test_free_space_sparse_numbers_skip_existing_paths():
    from partition_models import FreeExtent

    layout = DiskLayout(
        device="/dev/sda",
        size_mib=20480,
        partition_table="gpt",
        partitions=[
            PartitionInfo(name="sda1", path="/dev/sda1", size_mib=100, start_mib=1, end_mib=101),
            PartitionInfo(name="sda3", path="/dev/sda3", size_mib=100, start_mib=200, end_mib=300),
        ],
        free_extents=[FreeExtent(1000, 20000)],
    )
    with patch("partition_planner.is_uefi_system", return_value=True):
        plan = plan_free_space(layout, "ext4")
    created = [part.path for part in plan.partitions if part.action == "create"]
    assert created == ["/dev/sda2", "/dev/sda4"]


def test_bios_free_space_never_reuses_existing_esp():
    from partition_models import FreeExtent
    layout = DiskLayout("/dev/sda", 1000, partition_table="msdos", free_extents=[FreeExtent(200, 999)], partitions=[
        PartitionInfo("sda1", "/dev/sda1", 100, 1, 101, "vfat", role="esp", flags=["esp"], parttype="0xef")])
    plan = plan_free_space(layout, "ext4", boot_layout="bios_mbr")
    assert not plan.reuse_esp


def test_uefi_reuses_only_valid_sized_esp():
    from partition_models import FreeExtent
    layout = DiskLayout("/dev/sda", 1000, partition_table="gpt", free_extents=[FreeExtent(200, 999)], partitions=[
        PartitionInfo("sda1", "/dev/sda1", 150, 1, 151, "vfat", role="esp", parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b", partuuid="x")])
    plan = plan_free_space(layout, "ext4", boot_layout="uefi_gpt", efi_payload_bytes=120 * 1024 * 1024)
    assert not plan.reuse_esp


# ----------------------------------------------------------------------
# Compatibility tests for old lsblk (Ubuntu 18.04 / Debian 10 util-linux)
# ----------------------------------------------------------------------

def test_scanner_erase_all_succeeds_without_start_column():
    """erase-all must not require lsblk START (missing on util-linux <= 2.33)."""
    from unittest.mock import patch
    from partition_scanner import scan_disk
    from partition_models import PLACEMENT_ERASE_ALL
    from partition_planner import build_plan

    # Simulate lsblk -J -b output without "start" and without "pttype" at top level.
    fake = {
        "name": "sda",
        "size": str(20 * 1024 * 1024 * 1024),  # 20 GiB in bytes
        "type": "disk",
        "model": "Test",
        "serial": "",
        "tran": "sata",
        "children": [
            {"name": "sda1", "size": "1000000000", "type": "part", "fstype": "ext4", "label": "", "mountpoint": "", "partflags": ""},
        ],
    }

    with patch("partition_scanner._run_lsblk", return_value=fake):
        layout = scan_disk("/dev/sda")
        # start should be populated from sysfs fallback or default to 0; erase-all doesn't need it
        assert layout.size_mib > 1000
        # Planning erase-all must succeed (no START required)
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
        assert plan.wipe_disk
        assert len(plan.partitions) == 2  # root + esp


def test_scanner_handles_missing_pttype_and_start():
    """Full reduced column set must still allow erase-all planning."""
    from unittest.mock import patch
    from partition_scanner import scan_disk
    from partition_models import PLACEMENT_ERASE_ALL
    from partition_planner import build_plan

    fake = {
        "name": "sdb",
        "size": str(20 * 1024 * 1024 * 1024),
        "type": "disk",
        "children": [],
    }

    with patch("partition_scanner._run_lsblk", return_value=fake):
        layout = scan_disk("/dev/sdb")
        plan = build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
        assert plan.device.endswith("sdb")


def test_install_state_dataclass_runs_post_init_validation():
    from install_state import InstallState

    with patch("install_state.default_security_profile", return_value="balanced") as default_profile:
        state = InstallState(install_mode="live")
    assert state.security_profile == "balanced"
    default_profile.assert_called_once_with("live")

    with patch("install_state.validate_security_profile", return_value="strict") as validate_profile:
        state = InstallState(install_mode="native", security_profile="strict")
    assert state.security_profile == "strict"
    validate_profile.assert_called_once_with("strict")


def test_erase_all_bios_over_2tib_refuses():
    """BIOS + disk > 2 TiB must not produce a plan (would be GPT without BIOS bootloader)."""
    layout = DiskLayout(device="/dev/sda", size_mib=3_000_000, partition_table="gpt")
    with patch("partition_planner.is_uefi_system", return_value=False):
        try:
            build_plan(layout, PLACEMENT_ERASE_ALL, "ext4")
        except ValueError as exc:
            assert "BIOS" in str(exc) or "2 TiB" in str(exc)
        else:
            assert False, "BIOS + oversized disk must be refused"
