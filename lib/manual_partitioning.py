#!/usr/bin/env python3
"""Pure, non-executable manual partitioning plan model (phase 1)."""

from __future__ import absolute_import

from collections import namedtuple


EFI_SYSTEM_PARTITION_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
MIN_ESP_BYTES = 100 * 1024 * 1024


def canonical_device_identity(device):
    """Compare the kernel block device, not an unstable spelling of its path."""
    import os
    return os.path.realpath(device)


class ManualPlanError(ValueError):
    pass


class SectorExtent(namedtuple("SectorExtentBase", "start_sector size_sectors")):
    __slots__ = ()

    def __new__(cls, start_sector, size_sectors):
        if start_sector < 0 or size_sectors <= 0:
            raise ManualPlanError("sector extent must have a non-negative start and positive size")
        return super(SectorExtent, cls).__new__(cls, int(start_sector), int(size_sectors))

    @property
    def end_sector(self):
        return self.start_sector + self.size_sectors

    def as_dict(self):
        return {"start_sector": self.start_sector, "size_sectors": self.size_sectors}


class ExistingPartitionRef(namedtuple(
        "ExistingPartitionRefBase", "number partuuid start_sector size_sectors parttype fstype")):
    """Immutable identity captured from a scan, never a mutable /dev path."""
    __slots__ = ()

    def __new__(cls, number, partuuid, start_sector, size_sectors, parttype, fstype):
        if int(number) <= 0 or not partuuid or not parttype or not fstype:
            raise ManualPlanError("existing partition identity requires number, PARTUUID, type, and filesystem")
        return super(ExistingPartitionRef, cls).__new__(
            cls, int(number), str(partuuid), int(start_sector), int(size_sectors),
            str(parttype), str(fstype).lower())

    @property
    def extent(self):
        return SectorExtent(self.start_sector, self.size_sectors)

    def as_dict(self):
        return dict(self._asdict())


class LayoutSnapshot(namedtuple(
        "LayoutSnapshotBase", "device sector_size size_sectors partition_table partitions free_extents")):
    __slots__ = ()

    def __new__(cls, device, sector_size, size_sectors, partition_table, partitions, free_extents=()):
        if not device or int(sector_size) <= 0 or int(size_sectors) <= 0:
            raise ManualPlanError("layout snapshot requires device and positive sector geometry")
        refs = tuple(partitions)
        if len({ref.number for ref in refs}) != len(refs):
            raise ManualPlanError("layout snapshot has duplicate partition numbers")
        extents = tuple(free_extents)
        if any(not isinstance(extent, SectorExtent) for extent in extents):
            raise ManualPlanError("layout snapshot free extents must be sector extents")
        return super(LayoutSnapshot, cls).__new__(
            cls, canonical_device_identity(device), int(sector_size), int(size_sectors),
            str(partition_table).lower(), refs, extents)

    def as_dict(self):
        return {"device": self.device, "sector_size": self.sector_size,
                "size_sectors": self.size_sectors, "partition_table": self.partition_table,
                "partitions": [ref.as_dict() for ref in self.partitions],
                "free_extents": [extent.as_dict() for extent in self.free_extents]}


class ManualAction(namedtuple("ManualActionBase", "kind target extent fstype")):
    """A staged change. target is an ExistingPartitionRef for existing partitions."""
    __slots__ = ()

    def __new__(cls, kind, target=None, extent=None, fstype=""):
        return super(ManualAction, cls).__new__(
            cls, str(kind).lower(), target, extent, str(fstype).lower())

    def as_dict(self):
        return {"kind": self.kind,
                "target": self.target.as_dict() if self.target else None,
                "extent": self.extent.as_dict() if self.extent else None,
                "fstype": self.fstype}


class MountAssignment(namedtuple(
        "MountAssignmentBase", "target role mountpoint fstype format")):
    __slots__ = ()

    def __new__(cls, target, role, mountpoint="", fstype="", format=False):
        return super(MountAssignment, cls).__new__(
            cls, target, str(role).lower(), str(mountpoint), str(fstype).lower(), bool(format))

    def as_dict(self):
        return {"target": _target_dict(self.target), "role": self.role,
                "mountpoint": self.mountpoint, "fstype": self.fstype, "format": self.format}


def _target_dict(target):
    if isinstance(target, ExistingPartitionRef):
        return {"existing": target.as_dict()}
    if isinstance(target, SectorExtent):
        return {"create_extent": target.as_dict()}
    return None


def _safe_mountpoint(value):
    if not value or not value.startswith("/") or value == "/":
        return False
    if "\x00" in value or "//" in value or any(ord(char) < 32 for char in value):
        return False
    return all(component not in ("", ".", "..") for component in value.split("/")[1:])


def _is_esp(ref_or_fs):
    fs = ref_or_fs.fstype if hasattr(ref_or_fs, "fstype") else ref_or_fs
    return fs.lower() in ("fat", "fat16", "fat32", "vfat")


class ManualPartitionPlan(namedtuple(
        "ManualPartitionPlanBase", "snapshot actions assignments use_efi required_root_sectors alignment_sectors")):
    __slots__ = ()

    def __new__(cls, snapshot, actions=(), assignments=(), use_efi=False,
                required_root_sectors=0, alignment_sectors=2048):
        return super(ManualPartitionPlan, cls).__new__(
            cls, snapshot, tuple(actions), tuple(assignments), bool(use_efi),
            int(required_root_sectors), int(alignment_sectors))

    def as_dict(self):
        return {"snapshot": self.snapshot.as_dict(), "actions": [a.as_dict() for a in self.actions],
                "assignments": [a.as_dict() for a in self.assignments], "use_efi": self.use_efi,
                "required_root_sectors": self.required_root_sectors,
                "alignment_sectors": self.alignment_sectors}

    def summary_lines(self):
        lines = ["Target disk: {0}".format(self.snapshot.device)]
        order = ("delete", "shrink", "create", "format", "keep")
        for kind in order:
            for action in self.actions:
                if action.kind != kind:
                    continue
                if kind == "create":
                    lines.append("Create {0}-{1} ({2} sectors)".format(
                        action.extent.start_sector, action.extent.end_sector - 1, action.extent.size_sectors))
                elif kind == "shrink":
                    lines.append("Shrink partition {0}: {1}-{2} to {3}-{4} sectors".format(
                        action.target.number, action.target.start_sector, action.target.extent.end_sector - 1,
                        action.extent.start_sector, action.extent.end_sector - 1))
                elif kind == "format":
                    lines.append("Format partition {0} as {1}".format(action.target.number, action.fstype))
                else:
                    lines.append("{0} partition {1}".format(kind.capitalize(), action.target.number))
        for assignment in self.assignments:
            target = "new extent" if isinstance(assignment.target, SectorExtent) else "partition {0}".format(assignment.target.number)
            intent = "format" if assignment.format else "keep"
            lines.append("Assign {0} as {1} ({2}, {3})".format(target, assignment.role, assignment.mountpoint or "no mount", intent))
        return lines

    @property
    def destructive(self):
        return any(
            action.kind in ("delete", "shrink") or
            (action.kind == "format" and isinstance(action.target, ExistingPartitionRef))
            for action in self.actions
        )


def scan_manual_layout(layout):
    """Convert a complete direct DiskLayout into an immutable sector snapshot."""
    if not getattr(layout, "geometry_complete", False) or not layout.size_sectors:
        raise ManualPlanError("manual partitioning requires complete exact-sector geometry")
    if getattr(layout, "has_nested_layout", False) or getattr(layout, "has_mapped_layout", False):
        raise ManualPlanError("manual partitioning does not support nested or mapped layouts")
    table = (layout.partition_table or "").lower()
    if table not in ("gpt", "msdos"):
        raise ManualPlanError("manual partitioning supports GPT or primary MBR layouts only")
    refs = []
    for part in layout.partitions:
        if part.has_children or not part.partition_number or not part.partuuid or not part.parttype or not part.fstype:
            raise ManualPlanError("manual partitioning requires complete direct partition identities")
        # Extended/logical partition types cannot safely be changed by v1.
        if table == "msdos" and (part.partition_number > 4 or
                                  str(part.parttype).lower().replace("0x", "") in ("5", "05", "f", "0f", "85")):
            raise ManualPlanError("manual partitioning does not support MBR extended partitions")
        if part.fstype.lower() in ("crypto_luks", "lvm2_member", "linux_raid_member"):
            raise ManualPlanError("manual partitioning does not support mapped storage")
        refs.append(ExistingPartitionRef(part.partition_number, part.partuuid,
                                         part.start_sector, part.size_sectors,
                                         part.parttype, part.fstype))
    # GPT owns its primary header/array and backup array/header; MBR owns LBA 0.
    first_usable = 34 if table == "gpt" else 1
    end_usable = layout.size_sectors - 34 if table == "gpt" else layout.size_sectors
    if end_usable <= first_usable:
        raise ManualPlanError("disk has no usable partition sectors")
    occupied = sorted((ref.extent for ref in refs), key=lambda item: item.start_sector)
    cursor = first_usable
    free = []
    for extent in occupied:
        if extent.start_sector < cursor or extent.end_sector > end_usable:
            raise ManualPlanError("manual partitioning found unsupported partition geometry")
        if extent.start_sector > cursor:
            free.append(SectorExtent(cursor, extent.start_sector - cursor))
        cursor = extent.end_sector
    if cursor < end_usable:
        free.append(SectorExtent(cursor, end_usable - cursor))
    return LayoutSnapshot(layout.device, layout.logical_sector_size, layout.size_sectors,
                          table, refs, free)


class ManualPlanner(object):
    """Stages and validates a plan only. This class never invokes disk tools."""

    SUPPORTED_ACTIONS = ("create", "delete", "shrink", "format", "keep")

    def stage(self, snapshot, actions, assignments, use_efi=False,
              required_root_sectors=0, alignment_sectors=2048, install_mode="native"):
        plan = ManualPartitionPlan(snapshot, actions, assignments, use_efi,
                                   required_root_sectors, alignment_sectors)
        self.validate(plan, install_mode=install_mode)
        return plan

    def validate(self, plan, install_mode="native"):
        if plan.alignment_sectors <= 0:
            raise ManualPlanError("alignment must be positive")
        if not plan.use_efi and plan.snapshot.partition_table == "gpt":
            raise ManualPlanError("BIOS manual installation on GPT is unsupported because MiniOS does not provide bios_grub support")
        known = {ref: ref for ref in plan.snapshot.partitions}
        deleted = set()
        created = []
        shrunk = {}
        formatted = set()
        action_kinds = {}
        for action in plan.actions:
            if action.kind not in self.SUPPORTED_ACTIONS:
                raise ManualPlanError("unsupported manual operation: {0}".format(action.kind))
            if action.kind == "create":
                if action.target is not None or not isinstance(action.extent, SectorExtent):
                    raise ManualPlanError("create requires an exact sector extent")
                created.append(action.extent)
                continue
            if action.target not in known and not (action.kind == "format" and action.target in created):
                raise ManualPlanError("stale or missing existing partition identity")
            if action.target in known:
                seen = action_kinds.setdefault(action.target, set())
                if action.kind == "delete":
                    if seen:
                        raise ManualPlanError("delete cannot be combined with another operation on the same partition")
                elif "delete" in seen:
                    raise ManualPlanError("deleted partition cannot be modified")
                if action.kind == "shrink" and "shrink" in seen:
                    raise ManualPlanError("partition can be shrunk only once")
                seen.add(action.kind)
            if action.kind == "shrink":
                if not isinstance(action.extent, SectorExtent) or action.extent.start_sector != action.target.start_sector or action.extent.size_sectors >= action.target.size_sectors:
                    raise ManualPlanError("shrink must retain start sector and reduce size")
                if action.target.fstype not in ("ext2", "ext3", "ext4", "ntfs"):
                    raise ManualPlanError("shrink supports ext2, ext3, ext4, or NTFS only")
                shrunk[action.target] = action.extent
            elif action.kind == "delete":
                deleted.add(action.target)
            elif action.kind == "format":
                if not action.fstype:
                    raise ManualPlanError("format requires a filesystem")
                formatted.add(action.target)
        changed_extents = list(created) + list(shrunk.values())
        available = list(plan.snapshot.free_extents)
        available.extend(ref.extent for ref in deleted)
        available.extend(SectorExtent(extent.end_sector, ref.extent.end_sector - extent.end_sector)
                         for ref, extent in shrunk.items())
        for extent in created:
            if not any(extent.start_sector >= free.start_sector and extent.end_sector <= free.end_sector
                       for free in available):
                raise ManualPlanError("create extent is not within scanned or released free space")
        extents = list(changed_extents)
        extents.extend(ref.extent for ref in known if ref not in deleted and ref not in shrunk)
        for extent in extents:
            first_usable = 34 if plan.snapshot.partition_table == "gpt" else 1
            end_usable = plan.snapshot.size_sectors - (34 if plan.snapshot.partition_table == "gpt" else 0)
            if extent.start_sector < first_usable or extent.end_sector > end_usable:
                raise ManualPlanError("partition extent is out of range")
        for extent in changed_extents:
            if extent.start_sector % plan.alignment_sectors or extent.end_sector % plan.alignment_sectors:
                raise ManualPlanError("partition extent is not aligned")
        for index, left in enumerate(extents):
            for right in extents[index + 1:]:
                if left.start_sector < right.end_sector and right.start_sector < left.end_sector:
                    raise ManualPlanError("partition extents overlap")
        roots = [a for a in plan.assignments if a.role == "root"]
        if len(roots) != 1 or roots[0].mountpoint != "/":
            raise ManualPlanError("exactly one root assignment mounted at / is required")
        mounts = set()
        for assignment in plan.assignments:
            if assignment.target not in known and assignment.target not in created:
                raise ManualPlanError("assignment references an unstaged partition")
            if assignment.target in deleted:
                raise ManualPlanError("assignment references a deleted partition")
            fs = assignment.fstype or (assignment.target.fstype if isinstance(assignment.target, ExistingPartitionRef) else "")
            if assignment.role == "swap":
                if assignment.mountpoint or fs != "swap":
                    raise ManualPlanError("swap must use filesystem swap and no mountpoint")
            else:
                if isinstance(assignment.target, ExistingPartitionRef) and not assignment.format and fs != assignment.target.fstype:
                    raise ManualPlanError("existing partition filesystem must match unless format is selected")
                if assignment.role == "root":
                    if install_mode == "native" and fs not in ("ext2", "ext3", "ext4", "btrfs"):
                        raise ManualPlanError("native root requires ext2, ext3, ext4, or btrfs")
                elif assignment.role == "esp":
                    target = assignment.target
                    size = target.size_sectors * plan.snapshot.sector_size
                    correct_type = (not isinstance(target, ExistingPartitionRef) or
                                    target.parttype.lower() in (EFI_SYSTEM_PARTITION_GUID, "ef", "0xef"))
                    if (assignment.mountpoint != "/boot/efi" or not _is_esp(fs) or
                            size < MIN_ESP_BYTES or not correct_type):
                        raise ManualPlanError("ESP requires FAT filesystem mounted at /boot/efi")
                elif not _safe_mountpoint(assignment.mountpoint):
                    raise ManualPlanError("mountpoint must be a safe absolute path")
                if assignment.mountpoint in mounts:
                    raise ManualPlanError("mountpoints must be unique")
                mounts.add(assignment.mountpoint)
            if assignment.format and assignment.target not in formatted:
                raise ManualPlanError("format intent requires a staged format action")
        if plan.use_efi and len([a for a in plan.assignments if a.role == "esp"]) != 1:
            raise ManualPlanError("UEFI plan requires exactly one ESP assignment")
        root = roots[0].target
        root_extent = shrunk.get(root, root.extent if isinstance(root, ExistingPartitionRef) else root)
        if root_extent.size_sectors < plan.required_root_sectors:
            raise ManualPlanError("root partition does not meet required capacity")
