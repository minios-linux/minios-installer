#!/usr/bin/env python3
# -*- coding: utf-8 -*-

try:
    from dataclasses import dataclass, field
except ImportError:  # Python 3.6 until the backport is repository-published
    from dataclasses_compat import dataclass, field  # type: ignore

from typing import List, Optional


PLACEMENT_ERASE_ALL = "erase_all"
PLACEMENT_FREE_SPACE = "free_space"
PLACEMENT_ALONGSIDE_WINDOWS = "alongside_windows"
PLACEMENT_ALONGSIDE_OS = "alongside_os"
PLACEMENT_MANUAL = "manual"


@dataclass
class FreeExtent:
    start_mib: int
    end_mib: int

    @property
    def size_mib(self) -> int:
        return max(0, self.end_mib - self.start_mib)


@dataclass
class PartitionInfo:
    name: str
    path: str
    size_mib: int
    start_mib: int = 0
    end_mib: int = 0
    fstype: str = ""
    label: str = ""
    mountpoint: str = ""
    role: str = "unknown"
    flags: List[str] = field(default_factory=list)
    start_sector: int = 0
    size_sectors: int = 0
    partition_number: int = 0
    has_children: bool = False
    parttype: str = ""
    partuuid: str = ""


@dataclass
class DiskLayout:
    device: str
    size_mib: int
    model: str = ""
    serial: str = ""
    transport: str = ""
    partition_table: str = ""
    partitions: List[PartitionInfo] = field(default_factory=list)
    free_extents: List[FreeExtent] = field(default_factory=list)
    has_efi: bool = False
    has_windows: bool = False
    has_linux: bool = False
    logical_sector_size: int = 512
    size_sectors: int = 0
    geometry_complete: bool = True
    has_nested_layout: bool = False
    has_mapped_layout: bool = False


@dataclass
class PlannedPartition:
    action: str
    role: str
    start_mib: int
    end_mib: int
    fstype: str
    path: str = ""
    mountpoint: str = ""
    label: str = ""

    @property
    def size_mib(self) -> int:
        return max(0, self.end_mib - self.start_mib)


@dataclass
class ResizeOperation:
    path: str
    partition_number: int
    fstype: str
    start_sector: int
    old_size_sectors: int
    new_size_sectors: int
    sector_size: int


@dataclass
class PartitionPlan:
    device: str
    use_gpt: bool
    wipe_disk: bool
    partitions: List[PlannedPartition] = field(default_factory=list)
    reuse_esp: bool = False
    esp_path: str = ""
    resize: Optional[ResizeOperation] = None
    use_efi: bool = False
    # Immutable preserve-layout facts captured while planning and checked before
    # the first write, so a rescanned /dev node cannot redirect a plan.
    expected_partition_table: str = ""
    expected_partitions: List[tuple] = field(default_factory=list)
    expected_free_extent: Optional[tuple] = None
    esp_min_mib: int = 0
    esp_partuuid: str = ""

    def root_partition(self) -> Optional[PlannedPartition]:
        return next((p for p in self.partitions if p.role == "minios_root"), None)

    def esp_partition(self) -> Optional[PlannedPartition]:
        return next((p for p in self.partitions if p.role == "esp"), None)

    def summary_lines(self) -> List[str]:
        import gettext

        gettext.bindtextdomain("minios-installer", "/usr/share/locale")
        gettext.textdomain("minios-installer")
        _ = gettext.gettext

        lines = []
        table = "GPT" if self.use_gpt else "MBR"
        if self.wipe_disk:
            lines.append(
                _("Erase {device} and create a new {table} partition table").format(
                    device=self.device, table=table
                )
            )
        else:
            lines.append(
                _("Preserve existing partition table on {device}").format(device=self.device)
            )
        if self.resize:
            old_mib = self.resize.old_size_sectors * self.resize.sector_size // (1024 * 1024)
            new_mib = self.resize.new_size_sectors * self.resize.sector_size // (1024 * 1024)
            lines.append(
                _("Shrink {path} ({fstype}) from {old} MiB to {new} MiB").format(
                    path=self.resize.path,
                    fstype=self.resize.fstype,
                    old=old_mib,
                    new=new_mib,
                )
            )
        for part in self.partitions:
            if part.action == "reuse":
                lines.append(
                    _("Reuse {path} as {role}").format(path=part.path, role=part.role)
                )
            else:
                lines.append(
                    _(
                        "Create {role} {fstype} partition: {start}MiB-{end}MiB ({size}MiB)"
                    ).format(
                        role=part.role,
                        fstype=part.fstype,
                        start=part.start_mib,
                        end=part.end_mib,
                        size=part.size_mib,
                    )
                )
        return lines
