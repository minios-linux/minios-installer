#!/usr/bin/env python3
"""Executor for validated manual plans, used by native phase-3 deployment."""

from __future__ import absolute_import

from collections import namedtuple
import subprocess
import os
import shutil

from command_utils import run_command
from disk_utils import partition_device_path
from install_state import InstallCanceled
from manual_partitioning import (ExistingPartitionRef, ManualPlanError, SectorExtent,
                                  canonical_device_identity, scan_manual_layout)


class InstallTarget(namedtuple("InstallTargetBase", "ref path fstype")):
    """Resolved immutable partition identity for a future deploy/mount layer."""
    __slots__ = ()


class ManualExecutionResult(namedtuple("ManualExecutionResultBase", "targets assignments")):
    __slots__ = ()


def _cancel(cancel_cb):
    if cancel_cb and cancel_cb():
        raise InstallCanceled("Installation canceled by user.")


def _run(command, message, log_cb, dry_run=False, input_text=None):
    log_cb("$ " + " ".join(command))
    if dry_run:
        return
    if input_text is None:
        run_command(command, message)
        return
    result = subprocess.run(command, input=input_text, universal_newlines=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(message + "\n" + (result.stdout or ""))


def revalidate_manual_plan(plan, layout):
    """Fail closed unless the original immutable scan is still exactly current."""
    current = scan_manual_layout(layout)
    if (canonical_device_identity(current.device) != canonical_device_identity(plan.snapshot.device) or current.sector_size != plan.snapshot.sector_size or
            current.size_sectors != plan.snapshot.size_sectors or
            current.partition_table != plan.snapshot.partition_table or
            current.partitions != plan.snapshot.partitions or
            current.free_extents != plan.snapshot.free_extents):
        raise ManualPlanError("manual partition layout changed after the plan was created")
    return current


def _active_or_held(part):
    """Reject mounted, active-swap, and device-mapper holder mutations."""
    if part.mountpoint:
        return "mounted"
    try:
        with open("/proc/swaps", "r") as fh:
            if any(line.split() and line.split()[0] == part.path for line in fh.readlines()[1:]):
                return "active swap"
    except OSError:
        raise ManualPlanError("cannot verify active swap devices")
    holders = "/sys/class/block/{0}/holders".format(os.path.basename(part.path))
    try:
        if os.listdir(holders):
            return "has active holders"
    except FileNotFoundError:
        # Unit-test layouts do not have matching sysfs nodes. Real block devices do.
        pass
    except OSError:
        raise ManualPlanError("cannot verify partition holders")
    return ""


def _number_for_create(plan, extent, used):
    assigned = {assignment.target: assignment for assignment in plan.assignments}
    # New extents have no number in phase 1; pick the first number freed before creates.
    number = 1
    while number in used:
        number += 1
    used.add(number)
    assignment = assigned.get(extent)
    if assignment and assignment.role == "esp":
        parttype = "U" if plan.snapshot.partition_table == "gpt" else "ef"
    elif assignment and assignment.role == "swap":
        parttype = "S" if plan.snapshot.partition_table == "gpt" else "82"
    else:
        parttype = "L" if plan.snapshot.partition_table == "gpt" else "83"
    return number, parttype


def _reread(device, log_cb, dry_run):
    _run(["partprobe", device], "Failed to reread partition table.", log_cb, dry_run)
    _run(["blockdev", "--rereadpt", device], "Failed to reread partition table.", log_cb, dry_run)
    _run(["udevadm", "settle"], "Failed to settle partition devices.", log_cb, dry_run)


def _format_command(fstype, path):
    if fstype in ("fat", "fat16", "fat32", "vfat"):
        return ["mkfs.vfat", "-F", "32", path]
    if fstype == "swap":
        return ["mkswap", path]
    if fstype in ("btrfs", "ntfs"):
        return ["mkfs." + fstype, "-f", path]
    return ["mkfs." + fstype, "-F", path]


def required_manual_tools(plan):
    """Return the host commands required by this exact manual plan."""
    required = {"partprobe", "blockdev", "udevadm", "lsblk"}
    actions = {action.kind for action in plan.actions}
    if actions.intersection(("delete", "create", "shrink")):
        required.add("sfdisk")
    if any(assignment.role == "esp" for assignment in plan.assignments):
        required.add("parted")
    for action in plan.actions:
        if action.kind == "shrink":
            if action.target.fstype in ("ext2", "ext3", "ext4"):
                required.update(("e2fsck", "resize2fs"))
            elif action.target.fstype == "ntfs":
                required.add("ntfsresize")
        elif action.kind == "format":
            required.add(_format_command(action.fstype, "device")[0])
    return required


def preflight_manual_tools(plan):
    missing = sorted(tool for tool in required_manual_tools(plan) if not shutil.which(tool))
    if missing:
        raise ManualPlanError("manual execution requires unavailable tools: {0}".format(", ".join(missing)))


def execute_manual_plan(plan, log_cb, scan, dry_run=False, cancel_cb=None):
    """Apply v1 operations and return resolved targets plus mount assignments.

    ``scan`` is injected to keep this core testable; callers pass ``scan_disk``.
    No command, including dry-run logging, is emitted until revalidation succeeds.
    """
    if not dry_run:
        preflight_manual_tools(plan)
    current_layout = scan(plan.snapshot.device)
    revalidate_manual_plan(plan, current_layout)
    destructive = {action.target for action in plan.actions
                   if action.kind in ("delete", "shrink", "format") and
                   isinstance(action.target, ExistingPartitionRef)}
    for part in current_layout.partitions:
        if any(ref.number == part.partition_number for ref in destructive):
            reason = _active_or_held(part)
            if reason:
                raise ManualPlanError("{0} partition is not eligible for manual modification".format(reason))
    _cancel(cancel_cb)
    deleted = {action.target for action in plan.actions if action.kind == "delete"}
    used = {ref.number for ref in plan.snapshot.partitions if ref not in deleted}
    created = {}
    targets = {ref: InstallTarget(ref, partition_device_path(plan.snapshot.device, ref.number), ref.fstype)
               for ref in plan.snapshot.partitions if ref not in deleted}

    for action in plan.actions:
        if action.kind == "delete":
            _cancel(cancel_cb)
            _run(["sfdisk", "--delete", plan.snapshot.device, str(action.target.number)],
                 "Failed to delete partition.", log_cb, dry_run)
    for action in plan.actions:
        if action.kind != "shrink":
            continue
        # The existing resize sequence deliberately has no cancellation point after fs shrink.
        from partition_executor import _apply_resize
        from partition_models import PartitionPlan, ResizeOperation
        path = partition_device_path(plan.snapshot.device, action.target.number)
        resize = ResizeOperation(path, action.target.number, action.target.fstype,
                                 action.target.start_sector, action.target.size_sectors,
                                 action.extent.size_sectors, plan.snapshot.sector_size)
        _apply_resize(PartitionPlan(plan.snapshot.device, plan.snapshot.partition_table == "gpt", False,
                                    resize=resize), log_cb, dry_run, cancel_cb=cancel_cb)
    for action in plan.actions:
        if action.kind != "create":
            continue
        _cancel(cancel_cb)
        number, parttype = _number_for_create(plan, action.extent, used)
        created[action.extent] = number
        _run(["sfdisk", "--no-reread", "-N", str(number), plan.snapshot.device],
             "Failed to create partition.", log_cb, dry_run,
             "start={0}, size={1}, type={2}\n".format(action.extent.start_sector,
                                                         action.extent.size_sectors, parttype))
    # Type/flag belongs in the same table-write batch as creation, before discover.
    for assignment in plan.assignments:
        if assignment.role == "esp":
            _cancel(cancel_cb)
            number = (created[assignment.target] if isinstance(assignment.target, SectorExtent)
                      else assignment.target.number)
            _run(["parted", "-s", plan.snapshot.device, "set", str(number), "esp", "on"],
                 "Failed to set ESP flag.", log_cb, dry_run)
    _reread(plan.snapshot.device, log_cb, dry_run)
    if not dry_run:
        current = scan_manual_layout(scan(plan.snapshot.device))
        expected = {ref.number: ref for ref in plan.snapshot.partitions if ref not in deleted}
        for action in plan.actions:
            if action.kind == "shrink":
                expected[action.target.number] = ExistingPartitionRef(
                    action.target.number, action.target.partuuid, action.target.start_sector,
                    action.extent.size_sectors, action.target.parttype, action.target.fstype)
        observed = {ref.number: ref for ref in current.partitions}
        if any(observed.get(number) != ref for number, ref in expected.items()):
            raise RuntimeError("Existing partition identity changed during manual execution")
        for extent, number in created.items():
            ref = next((item for item in current.partitions if item.number == number and item.extent == extent), None)
            if ref is None:
                raise RuntimeError("Created partition identity could not be discovered")
            targets[extent] = InstallTarget(ref, partition_device_path(current.device, number), ref.fstype)
    else:
        for extent, number in created.items():
            targets[extent] = InstallTarget(None, partition_device_path(plan.snapshot.device, number), "")
    for action in plan.actions:
        if action.kind != "format":
            continue
        _cancel(cancel_cb)
        target = targets[action.target]
        _run(_format_command(action.fstype, target.path), "Failed to format partition.", log_cb, dry_run)
    return ManualExecutionResult(tuple(targets[assignment.target] for assignment in plan.assignments),
                                 plan.assignments)
