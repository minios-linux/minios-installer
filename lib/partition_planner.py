#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import os
import re
from typing import Optional

from disk_utils import partition_device_path
from partition_models import (
    DiskLayout,
    FreeExtent,
    PartitionPlan,
    PlannedPartition,
    PLACEMENT_ALONGSIDE_OS,
    PLACEMENT_ALONGSIDE_WINDOWS,
    PLACEMENT_ERASE_ALL,
    PLACEMENT_FREE_SPACE,
    PLACEMENT_MANUAL,
)
from partition_scanner import is_uefi_system
from partition_resize import plan_shrink


gettext.bindtextdomain("minios-installer", "/usr/share/locale")
gettext.textdomain("minios-installer")
_ = gettext.gettext


ESP_SIZE_MIB = 100
ALIGNMENT_MIB = 1
END_GUARD_MIB = 1
# Disks at or above 2 TiB need GPT: MBR tops out just under 2^32 sectors (~2 TiB).
GPT_SIZE_MIB = 2_097_152
BOOT_LAYOUT_AUTO = "auto"
BOOT_LAYOUT_BIOS_MBR = "bios_mbr"
BOOT_LAYOUT_UEFI_MBR = "uefi_mbr"
BOOT_LAYOUT_UEFI_GPT = "uefi_gpt"


def _part_name(device: str, index: int) -> str:
    return partition_device_path(device, index)


def _use_gpt_for_erase(layout: DiskLayout, boot_layout: str = BOOT_LAYOUT_AUTO) -> bool:
    """
    Table type for wipe/erase-all installs.

    Do not inherit the pre-wipe table: a BIOS session wiping an existing GPT
    disk must still create MBR + install a BIOS bootloader (baseline behavior).
    GPT only when firmware is UEFI or the disk is >= 2 TiB.
    """
    if boot_layout == BOOT_LAYOUT_UEFI_GPT:
        return True
    if boot_layout in (BOOT_LAYOUT_BIOS_MBR, BOOT_LAYOUT_UEFI_MBR):
        return False
    if is_uefi_system():
        return True
    return layout.size_mib >= GPT_SIZE_MIB


def _use_gpt_for_preserve(layout: DiskLayout, boot_layout: str = BOOT_LAYOUT_AUTO) -> bool:
    """Table type when keeping an existing partition table (future free-space)."""
    if not layout.partition_table:
        raise ValueError(_("No partition table was found. Use erase-all to initialize this disk."))
    if layout.partition_table == "gpt":
        if boot_layout in (BOOT_LAYOUT_BIOS_MBR, BOOT_LAYOUT_UEFI_MBR):
            raise ValueError(_("BIOS/MBR layout cannot be used with existing GPT free space. Choose Automatic/UEFI-GPT or erase the disk."))
        if boot_layout == BOOT_LAYOUT_AUTO and not is_uefi_system():
            raise ValueError(
                _("Existing GPT free space cannot be used from a BIOS boot without BIOS-on-GPT bootloader support. "
                  "Boot the installer under UEFI, force UEFI/GPT if you know the target firmware supports it, or erase the disk for BIOS/MBR.")
            )
        return True
    if boot_layout == BOOT_LAYOUT_UEFI_GPT:
        raise ValueError(_("UEFI/GPT layout cannot be used with existing MBR free space. Choose Automatic/BIOS-MBR or erase the disk."))
    return _use_gpt_for_erase(layout, boot_layout=boot_layout)


def _use_efi_for_layout(boot_layout: str = BOOT_LAYOUT_AUTO) -> bool:
    if boot_layout == BOOT_LAYOUT_BIOS_MBR:
        return False
    if boot_layout in (BOOT_LAYOUT_UEFI_MBR, BOOT_LAYOUT_UEFI_GPT):
        return True
    return is_uefi_system()


def _append_partitions(plan: PartitionPlan, start_mib: int, end_mib: int, filesystem: str, create_esp: bool, start_index: int, swap_size_mib: int = 0, partition_indices=None, esp_first: bool = False, required_root_mib: int = 0, esp_size_mib: int = ESP_SIZE_MIB) -> None:
    available = end_mib - start_mib
    esp_mib = esp_size_mib if create_esp else 0
    swap_mib = max(0, int(swap_size_mib or 0))
    root_mib = available - esp_mib - swap_mib
    required_root_mib = max(1, int(required_root_mib or 0))
    if root_mib < required_root_mib:
        raise ValueError(
            _("Not enough space for MiniOS root partition (need at least {need} MiB, have {have} MiB)").format(
                need=required_root_mib, have=root_mib
            )
        )

    cursor = start_mib
    index = start_index
    indices = list(partition_indices or [])

    def take_index():
        nonlocal index
        if indices:
            return indices.pop(0)
        current = index
        index += 1
        return current

    if create_esp and esp_first:
        plan.partitions.append(PlannedPartition("create", "esp", cursor, cursor + esp_mib, "fat32", path=_part_name(plan.device, take_index()), mountpoint="/boot/efi"))
        cursor += esp_mib
    plan.partitions.append(PlannedPartition("create", "minios_root", cursor, cursor + root_mib, filesystem, path=_part_name(plan.device, take_index()), mountpoint="/"))
    cursor += root_mib
    if swap_mib:
        plan.partitions.append(PlannedPartition("create", "swap", cursor, cursor + swap_mib, "swap", path=_part_name(plan.device, take_index()), mountpoint="swap"))
        cursor += swap_mib
    if create_esp and not esp_first:
        plan.partitions.append(PlannedPartition("create", "esp", cursor, cursor + esp_mib, "fat32", path=_part_name(plan.device, take_index()), mountpoint="/boot/efi"))


def _needs_esp(filesystem: str, install_mode: str, use_efi: bool) -> bool:
    if filesystem == "fat32":
        return False
    # Native BIOS boots from the disk MBR and root /boot; its FAT32 partition
    # would not contain an EFI loader. Live installs retain their portable layout.
    return install_mode != "native" or use_efi


def plan_erase_all(layout: DiskLayout, filesystem: str = "ext4", install_mode: str = "live", swap_size_mib: int = 0, boot_layout: str = BOOT_LAYOUT_AUTO, required_root_mib: int = 0) -> PartitionPlan:
    use_gpt = _use_gpt_for_erase(layout, boot_layout=boot_layout)
    use_efi = _use_efi_for_layout(boot_layout)
    if use_gpt and not use_efi:
        # BIOS + we decided GPT (almost always because size >= 2 TiB).
        # We do not currently install a BIOS bootloader (GRUB bios_grub + core.img) for GPT erase-all.
        # Refuse to avoid leaving the user with an unbootable system.
        raise ValueError(
            _("BIOS firmware cannot reliably boot a GPT layout. "
              "Erase-all requires MBR on BIOS; disks 2 TiB or larger are not supported in this mode. "
              "Use a smaller disk or boot the installer under UEFI.")
        )
    from format_utils import validate_filesystem_for_plan
    validate_filesystem_for_plan(filesystem, use_gpt, install_mode=install_mode)
    # Product rule: FAT32 root is a single partition (no ESP). On UEFI, EFI files
    # are still copied onto the FAT32 root; on BIOS, SYSLINUX/MBR is installed.
    create_esp = _needs_esp(filesystem, install_mode, use_efi)
    plan = PartitionPlan(layout.device, use_gpt, wipe_disk=True, use_efi=use_efi)
    _append_partitions(plan, ALIGNMENT_MIB, layout.size_mib - END_GUARD_MIB, filesystem, create_esp, 1, swap_size_mib=swap_size_mib if install_mode == "native" else 0, esp_first=install_mode == "native" and use_efi, required_root_mib=required_root_mib)
    return plan


def _largest_extent(layout: DiskLayout, minimum_mib: int = 1) -> Optional[FreeExtent]:
    extents = [extent for extent in layout.free_extents if extent.size_mib >= max(1, minimum_mib)]
    if not extents:
        return None
    return max(extents, key=lambda extent: extent.size_mib)


def _next_partition_index(layout: DiskLayout) -> int:
    """Lowest free partition number from existing names/paths (handles sparse p1,p3)."""
    numbers = []
    for part in layout.partitions:
        for value in (part.name, part.path):
            if not value:
                continue
            match = re.search(r"(\d+)$", os.path.basename(value) if "/" in value else value)
            if match:
                numbers.append(int(match.group(1)))
                break
    used = set(numbers)
    index = 1
    while index in used:
        index += 1
    return index


def _free_partition_indices(layout: DiskLayout, count: int) -> list:
    used = set()
    for part in layout.partitions:
        for value in (part.name, part.path):
            if not value:
                continue
            match = re.search(r"(\d+)$", os.path.basename(value) if "/" in value else value)
            if match:
                used.add(int(match.group(1)))
                break
    result = []
    index = 1
    while len(result) < count:
        if index not in used:
            result.append(index)
        index += 1
    return result


def _valid_esp(layout: DiskLayout, part) -> bool:
    if not part.partuuid or part.fstype.lower() not in ("vfat", "fat", "fat16", "fat32") or part.size_mib < ESP_SIZE_MIB:
        return False
    if layout.partition_table == "gpt":
        return part.parttype.lower() == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    return part.parttype.lower() in ("0xef", "ef") or any(flag.lower() in ("esp", "boot") for flag in part.flags)


def _snapshot_preserve_plan(plan: PartitionPlan, layout: DiskLayout, extent: FreeExtent) -> None:
    plan.expected_partition_table = layout.partition_table
    plan.expected_partitions = [(p.partition_number, p.start_sector, p.size_sectors, p.parttype, p.partuuid) for p in layout.partitions]
    plan.expected_free_extent = (extent.start_mib, extent.end_mib)


def plan_free_space(layout: DiskLayout, filesystem: str = "ext4", install_mode: str = "live", swap_size_mib: int = 0, boot_layout: str = BOOT_LAYOUT_AUTO, required_root_mib: int = 0, efi_payload_bytes: int = 0) -> PartitionPlan:
    use_gpt = _use_gpt_for_preserve(layout, boot_layout=boot_layout)
    use_efi = _use_efi_for_layout(boot_layout)
    esp_min_mib = max(ESP_SIZE_MIB, (max(0, efi_payload_bytes) * 125 + 99) // 100 // (1024 * 1024) + 1)
    esp = next((p for p in layout.partitions if p.role == "esp" and _valid_esp(layout, p) and p.size_mib >= esp_min_mib), None) if use_efi else None
    plan = PartitionPlan(layout.device, use_gpt, wipe_disk=False, reuse_esp=bool(esp), esp_path=esp.path if esp else "", use_efi=use_efi, esp_min_mib=esp_min_mib, esp_partuuid=esp.partuuid if esp else "")
    if esp:
        plan.partitions.append(PlannedPartition("reuse", "esp", esp.start_mib, esp.end_mib, esp.fstype, path=esp.path, mountpoint="/boot/efi"))
    create_esp = _needs_esp(filesystem, install_mode, use_efi) and not esp
    required_total_mib = max(1, int(required_root_mib or 0))
    if create_esp:
        required_total_mib += esp_min_mib
    if install_mode == "native":
        required_total_mib += max(0, int(swap_size_mib or 0))
    extent = _largest_extent(layout, required_total_mib)
    if extent is None:
        raise ValueError(
            _("No suitable free space available (need at least {need} MiB)").format(need=required_total_mib)
        )
    create_count = 1 + (1 if create_esp else 0) + (1 if install_mode == "native" and int(swap_size_mib or 0) > 0 else 0)
    if not use_gpt:
        if len(layout.partitions) + create_count > 4:
            raise ValueError(
                _("Not enough MBR primary partition slots for this free-space install. "
                  "Use erase-all, remove a partition, or use a GPT/UEFI layout.")
            )
    indices = _free_partition_indices(layout, create_count)
    _append_partitions(plan, extent.start_mib, extent.end_mib, filesystem, create_esp, indices[0], swap_size_mib=swap_size_mib if install_mode == "native" else 0, partition_indices=indices, esp_first=install_mode == "native" and use_efi, required_root_mib=required_root_mib, esp_size_mib=esp_min_mib)
    _snapshot_preserve_plan(plan, layout, extent)
    return plan


def build_plan(layout: DiskLayout, placement: str, filesystem: str = "ext4", install_mode: str = "live", swap_size_mib: int = 0, boot_layout: str = BOOT_LAYOUT_AUTO, alongside_size_mib: int = 0, required_root_mib: int = 0, efi_payload_bytes: int = 0) -> PartitionPlan:
    if placement == PLACEMENT_ERASE_ALL:
        return plan_erase_all(layout, filesystem, install_mode=install_mode, swap_size_mib=swap_size_mib, boot_layout=boot_layout, required_root_mib=required_root_mib)
    if placement == PLACEMENT_FREE_SPACE:
        from format_utils import validate_filesystem_for_plan
        use_gpt = _use_gpt_for_preserve(layout, boot_layout=boot_layout)
        use_efi = _use_efi_for_layout(boot_layout)
        validate_filesystem_for_plan(filesystem, use_gpt, install_mode=install_mode)
        return plan_free_space(layout, filesystem, install_mode=install_mode, swap_size_mib=swap_size_mib, boot_layout=boot_layout, required_root_mib=required_root_mib, efi_payload_bytes=efi_payload_bytes)
    if placement in (PLACEMENT_ALONGSIDE_WINDOWS, PLACEMENT_ALONGSIDE_OS):
        from format_utils import validate_filesystem_for_plan
        use_gpt = _use_gpt_for_preserve(layout, boot_layout=boot_layout)
        use_efi = _use_efi_for_layout(boot_layout)
        validate_filesystem_for_plan(filesystem, use_gpt, install_mode=install_mode)
        esp = next((p for p in layout.partitions if p.role == "esp" and use_efi and _valid_esp(layout, p)), None)
        create_esp = _needs_esp(filesystem, install_mode, use_efi) and not esp
        minimum_required = max(1, int(required_root_mib or 0)) + (ESP_SIZE_MIB if create_esp else 0)
        if install_mode == "native":
            minimum_required += max(0, int(swap_size_mib or 0))
        required = max(minimum_required, int(alongside_size_mib or 0))
        # The allocator uses MiB coordinates while the resize boundary is in
        # sectors. Reserve one extra MiB so rounding the new extent inward can
        # never create a partition beyond the sectors released by the shrink.
        resize = plan_shrink(layout, required + ALIGNMENT_MIB)
        new_end_bytes = (resize.start_sector + resize.new_size_sectors) * resize.sector_size
        start_mib = (new_end_bytes + 1024 * 1024 - 1) // (1024 * 1024)
        old_end_bytes = (resize.start_sector + resize.old_size_sectors) * resize.sector_size
        end_mib = old_end_bytes // (1024 * 1024)
        if end_mib - start_mib < required:
            raise ValueError(_("The resized free space cannot be represented safely in MiB-aligned partitions."))
        synthetic = DiskLayout(layout.device, layout.size_mib, partition_table=layout.partition_table,
                                partitions=layout.partitions,
                                free_extents=[FreeExtent(start_mib, end_mib)],
                               has_efi=layout.has_efi, logical_sector_size=layout.logical_sector_size,
                               size_sectors=layout.size_sectors)
        plan = plan_free_space(synthetic, filesystem, install_mode=install_mode,
                               swap_size_mib=swap_size_mib, boot_layout=boot_layout,
                               required_root_mib=required_root_mib, efi_payload_bytes=efi_payload_bytes)
        plan.resize = resize
        return plan
    if placement in (PLACEMENT_MANUAL,):
        raise ValueError(_("This placement mode is not implemented yet"))
    raise ValueError(_("Unsupported placement: {placement}").format(placement=placement))
