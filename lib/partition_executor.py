#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import os
import stat
import subprocess
import time
from typing import Callable, List, Optional, Tuple

from command_utils import run_command
from disk_utils import partition_device_path
from format_utils import format_partitions
from install_state import InstallCanceled
from mount_utils import force_unmount_device, mount_partition
from partition_models import PartitionPlan, PlannedPartition


gettext.bindtextdomain("minios-installer", "/usr/share/locale")
gettext.textdomain("minios-installer")
_ = gettext.gettext


def part_name(device: str, index: int) -> str:
    return partition_device_path(device, index)


def _check_cancel(cancel_cb: Optional[Callable[[], bool]], after_wipe: bool = False) -> None:
    if cancel_cb and cancel_cb():
        if after_wipe:
            raise InstallCanceled(
                _("Installation canceled after disk wipe started; the target disk may be partially erased.")
            )
        raise InstallCanceled(_("Installation canceled by user."))


def _run(cmd: List[str], message: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    log_cb("$ " + " ".join(cmd))
    if dry_run:
        return
    run_command(cmd, message)


def _partition_index(plan: PartitionPlan, part: PlannedPartition) -> int:
    if part.path:
        import re
        name = os.path.basename(part.path)
        by_id_match = re.search(r"-part(\d+)$", name)
        if by_id_match:
            return int(by_id_match.group(1))
        # nvme0n1p2 / mmcblk0p1 / sda3
        match = re.search(r"(?:p)?(\d+)$", name)
        if match:
            return int(match.group(1))
    created = [p for p in plan.partitions if p.action == "create"]
    return created.index(part) + 1


def _apply_partition_table(
    plan: PartitionPlan,
    log_cb: Callable[[str], None],
    dry_run: bool = False,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> None:
    wiped = False
    if not plan.wipe_disk and not dry_run:
        _revalidate_preserve_plan(plan)
        _revalidate_reused_esp(plan)
    if plan.resize:
        _check_cancel(cancel_cb)
        _apply_resize(plan, log_cb, dry_run, cancel_cb=cancel_cb)
        if not dry_run:
            _revalidate_planned_free_extent(plan)
    if plan.wipe_disk:
        if not dry_run:
            _prepare_erase_target(plan.device, log_cb, dry_run)
        label = "gpt" if plan.use_gpt else "msdos"
        log_cb(_("Unmounting existing target mounts on {device}").format(device=plan.device))
        if not dry_run:
            force_unmount_device(plan.device)
        _check_cancel(cancel_cb)
        _run(["wipefs", "-a", plan.device], _("Failed to wipe existing signatures."), log_cb, dry_run)
        wiped = True
        _check_cancel(cancel_cb, after_wipe=True)
        _run(
            ["parted", "-s", plan.device, "mklabel", label],
            _("Failed to create partition table."),
            log_cb,
            dry_run,
        )
        _check_cancel(cancel_cb, after_wipe=True)

    for part in plan.partitions:
        if part.action != "create":
            continue
        fs_arg = "fat32" if part.fstype == "fat32" else part.fstype
        name = "ESP" if part.role == "esp" and plan.use_gpt else "primary"
        if part.role == "swap":
            fs_arg = "linux-swap"
        _run(
            ["parted", "-s", plan.device, "mkpart", name, fs_arg, f"{part.start_mib}MiB", f"{part.end_mib}MiB"],
            _("Failed to create {role} partition.").format(role=part.role),
            log_cb,
            dry_run,
        )
        index = _partition_index(plan, part)
        if part.role == "esp" and plan.use_efi:
            _run(["parted", "-s", plan.device, "set", str(index), "esp", "on"], _("Failed to set ESP flag."), log_cb, dry_run)
        elif part.role == "minios_root" and plan.use_efi and part.fstype == "fat32":
            # Single-partition FAT32 on GPT: mark root as ESP so firmware can boot it.
            _run(["parted", "-s", plan.device, "set", str(index), "esp", "on"], _("Failed to set ESP flag."), log_cb, dry_run)
        elif part.role == "minios_root" and not plan.use_efi:
            _run(["parted", "-s", plan.device, "set", str(index), "boot", "on"], _("Failed to set boot flag."), log_cb, dry_run)
        _check_cancel(cancel_cb, after_wipe=wiped or plan.wipe_disk)

    if not dry_run:
        r_probe = subprocess.run(["partprobe", plan.device], check=False)
        r_reread = subprocess.run(["blockdev", "--rereadpt", plan.device], check=False)
        if r_probe.returncode != 0 or r_reread.returncode != 0:
            log_cb(
                _("Warning: kernel partition table re-read reported errors; "
                  "waiting longer for partition nodes...")
            )
            timeout = 30
        else:
            timeout = 20
        _wait_for_partitions(plan, timeout=timeout)


def _verify_existing_partition(plan: PartitionPlan, path: str, expected_fstype: Optional[str] = None) -> None:
    """Reject a stale partition path that no longer belongs to this target disk."""
    if not os.path.exists(path):
        raise RuntimeError(_("Planned existing partition no longer exists: {path}").format(path=path))
    target = os.path.realpath(plan.device)
    parent = subprocess.check_output(["lsblk", "-n", "-o", "PKNAME", path], universal_newlines=True).strip()
    if not parent or os.path.realpath(os.path.join("/dev", parent)) != target:
        raise RuntimeError(_("Planned existing partition is no longer on the selected target disk."))
    if expected_fstype:
        fstype = subprocess.check_output(["lsblk", "-n", "-o", "FSTYPE", path], universal_newlines=True).strip().lower()
        accepted = ("vfat", "fat", "fat16", "fat32") if expected_fstype == "fat32" else (expected_fstype.lower(),)
        if fstype not in accepted:
            raise RuntimeError(_("Planned existing partition no longer has the expected filesystem."))


def _revalidate_preserve_plan(plan: PartitionPlan) -> None:
    """Check the entire preserved layout immediately before its first write."""
    if not plan.expected_partition_table:
        return
    from partition_scanner import scan_disk
    current = scan_disk(plan.device)
    if current.partition_table != plan.expected_partition_table:
        raise RuntimeError(_("Partition table changed after the plan was created."))
    observed = [(p.partition_number, p.start_sector, p.size_sectors, p.parttype, p.partuuid) for p in current.partitions]
    if observed != plan.expected_partitions:
        raise RuntimeError(_("Existing partition geometry changed after the plan was created."))
    if plan.expected_free_extent and not plan.resize:
        if plan.expected_free_extent not in [(e.start_mib, e.end_mib) for e in current.free_extents]:
            raise RuntimeError(_("Planned free extent is no longer available."))


def _revalidate_planned_free_extent(plan: PartitionPlan) -> None:
    """The alongside extent only exists after a successful shrink."""
    if not plan.expected_free_extent:
        return
    from partition_scanner import scan_disk
    current = scan_disk(plan.device)
    if plan.expected_free_extent not in [(e.start_mib, e.end_mib) for e in current.free_extents]:
        raise RuntimeError(_("Resized partition did not create the planned free extent."))


def _revalidate_reused_esp(plan: PartitionPlan) -> None:
    if not plan.reuse_esp:
        return
    if not plan.use_efi or not plan.esp_path or not plan.esp_partuuid:
        raise RuntimeError(_("ESP reuse plan is incomplete; refusing to modify the disk."))
    _verify_existing_partition(plan, plan.esp_path, "fat32")
    current_uuid = subprocess.check_output(["blkid", "-s", "PARTUUID", "-o", "value", plan.esp_path], universal_newlines=True).strip()
    if current_uuid != plan.esp_partuuid:
        raise RuntimeError(_("Planned ESP identity changed after the plan was created."))
    try:
        available = int(subprocess.check_output(["lsblk", "-b", "-n", "-o", "FSAVAIL", plan.esp_path], universal_newlines=True).strip())
    except Exception as exc:
        raise RuntimeError(_("Could not measure free space on the reused ESP.")) from exc
    required = plan.esp_min_mib * 1024 * 1024
    if available < required:
        raise RuntimeError(_("Reused ESP does not have enough free space for the EFI payload."))


def _target_block_names(device: str) -> set:
    """Return the target kernel node and its partitions for swap/holder checks."""
    names = set()
    try:
        out = subprocess.check_output(["lsblk", "-nr", "-o", "NAME", device], universal_newlines=True)
        names.update(line.strip() for line in out.splitlines() if line.strip())
    except Exception:
        names.add(os.path.basename(os.path.realpath(device)))
    return names


def _prepare_erase_target(device: str, log_cb: Callable[[str], None], dry_run: bool) -> None:
    """Swapoff only direct target swaps; reject mapped/holder stacks outright."""
    names = _target_block_names(device)
    try:
        types = subprocess.check_output(["lsblk", "-nr", "-o", "TYPE", device], universal_newlines=True).split()
    except Exception as exc:
        raise RuntimeError(_("Could not inspect target device stack; refusing erase-all.")) from exc
    if any(kind in ("crypt", "lvm", "raid", "md") for kind in types):
        raise RuntimeError(_("Target participates in an active mapped storage stack; refusing erase-all."))
    for name in names:
        holders = "/sys/class/block/{}/holders".format(name)
        try:
            if os.path.isdir(holders) and os.listdir(holders):
                raise RuntimeError(_("Target has active block-device holders; refusing erase-all."))
        except OSError:
            raise RuntimeError(_("Could not inspect target block-device holders; refusing erase-all."))
    active = []
    try:
        with open("/proc/swaps", "r") as fh:
            for line in fh.readlines()[1:]:
                path = line.split()[0] if line.split() else ""
                if path and os.path.basename(os.path.realpath(path)) in names:
                    active.append(path)
    except OSError as exc:
        raise RuntimeError(_("Could not inspect active swap; refusing erase-all.")) from exc
    for path in active:
        _run(["swapoff", path], _("Failed to disable swap on target disk."), log_cb, dry_run)
    if active and not dry_run:
        # Fail closed if any target swap survives swapoff (including a remapped path).
        try:
            with open("/proc/swaps", "r") as fh:
                if any(os.path.basename(os.path.realpath(line.split()[0])) in names for line in fh.readlines()[1:] if line.split()):
                    raise RuntimeError(_("Target swap remains active; refusing erase-all."))
        except OSError as exc:
            raise RuntimeError(_("Could not recheck active swap; refusing erase-all.")) from exc


def _apply_resize(plan: PartitionPlan, log_cb: Callable[[str], None], dry_run: bool,
                  cancel_cb: Optional[Callable[[], bool]] = None) -> None:
    resize = plan.resize
    path = resize.path
    new_bytes = resize.new_size_sectors * resize.sector_size
    if not dry_run:
        _verify_existing_partition(plan, path, resize.fstype)
        mounted = subprocess.run(["findmnt", "-rn", "-S", path], stdout=subprocess.PIPE, check=False)
        if mounted.returncode == 0 and mounted.stdout:
            raise RuntimeError(_("Refusing to resize a mounted partition: {path}").format(path=path))
        geometry = subprocess.check_output(
            ["lsblk", "-b", "-n", "-o", "START,SIZE", path], universal_newlines=True
        ).split()
        old_bytes = resize.old_size_sectors * resize.sector_size
        if len(geometry) != 2 or int(geometry[0]) != resize.start_sector or int(geometry[1]) != old_bytes:
            raise RuntimeError(_("Partition geometry changed after the resize plan was created."))
    if resize.fstype in ("ext2", "ext3", "ext4"):
        _check_cancel(cancel_cb)
        _run(["e2fsck", "-f", "-y", path], _("Filesystem check failed before resize."), log_cb, dry_run)
        # From filesystem shrink through partition-boundary verification, stopping
        # would leave two sizes inconsistent. Cancellation is deferred below.
        _run(["resize2fs", path, str(new_bytes // 1024) + "K"], _("Failed to resize filesystem."), log_cb, dry_run)
        _run(["e2fsck", "-f", "-y", path], _("Filesystem check failed after resize."), log_cb, dry_run)
    elif resize.fstype == "ntfs":
        _check_cancel(cancel_cb)
        _run(["ntfsresize", "--check", path], _("NTFS check failed before resize."), log_cb, dry_run)
        _check_cancel(cancel_cb)
        _run(["ntfsresize", "--no-action", "--size", str(new_bytes), path], _("NTFS resize validation failed."), log_cb, dry_run)
        _check_cancel(cancel_cb)
        _run(["ntfsresize", "--size", str(new_bytes), path], _("Failed to resize NTFS."), log_cb, dry_run)
    else:
        raise RuntimeError(_("Unsupported resize filesystem."))
    command = ["sfdisk", "--no-reread", "-N", str(resize.partition_number), plan.device]
    log_cb("$ " + " ".join(command))
    if dry_run:
        log_cb("< size=" + str(resize.new_size_sectors))
        return
    result = subprocess.run(command, input="size=" + str(resize.new_size_sectors) + "\n",
                            universal_newlines=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(_("Failed to update resized partition boundary.") + "\n" + (result.stdout or ""))
    subprocess.run(["partprobe", plan.device], check=False)
    subprocess.run(["blockdev", "--rereadpt", plan.device], check=False)
    subprocess.run(["udevadm", "settle"], check=False)
    output = subprocess.check_output(["lsblk", "-b", "-n", "-o", "START,SIZE", path], universal_newlines=True)
    values = output.split()
    if len(values) != 2 or int(values[0]) != resize.start_sector or int(values[1]) != new_bytes:
        raise RuntimeError(_("Kernel partition geometry does not match the resize plan."))
    if resize.fstype == "ntfs":
        _run(["ntfsresize", "--check", path], _("NTFS check failed after resize."), log_cb, dry_run)
    _check_cancel(cancel_cb)


def _format_swap(path: str, log_cb: Callable[[str], None], dry_run: bool = False) -> None:
    _run(["mkswap", path], _("Failed to format swap on {path}.").format(path=path), log_cb, dry_run)


def _wait_for_partitions(plan: PartitionPlan, timeout: int = 20) -> None:
    expected = []
    for part in plan.partitions:
        if part.action != "create":
            continue
        expected.append(part.path or part_name(plan.device, _partition_index(plan, part)))

    if not expected:
        return

    subprocess.run(["udevadm", "settle", "--timeout", str(min(timeout, 30))], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = True
        for path in expected:
            try:
                if not stat.S_ISBLK(os.stat(path).st_mode):
                    ready = False
                    break
            except OSError:
                ready = False
                break
        if ready:
            return
        time.sleep(0.2)

    missing = ", ".join(path for path in expected if not os.path.exists(path))
    raise RuntimeError(_("Partition devices did not appear in time: {missing}").format(missing=missing))


def execute_plan(
    plan: PartitionPlan,
    log_cb: Callable[[str], None],
    dry_run: bool = False,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> Tuple[str, Optional[str], str, Optional[str]]:
    """Apply plan and mount root/ESP. Returns (root_part, esp_part, root_mount, esp_mount)."""
    _apply_partition_table(plan, log_cb, dry_run, cancel_cb=cancel_cb)
    _check_cancel(cancel_cb, after_wipe=plan.wipe_disk)

    root_part = None
    esp_part = plan.esp_path or None
    root_mount = None
    esp_mount = None

    for part in plan.partitions:
        _check_cancel(cancel_cb, after_wipe=plan.wipe_disk)
        if part.action == "reuse" and part.role == "esp":
            if not plan.use_efi:
                raise RuntimeError(_("BIOS plans must not reuse an EFI system partition."))
            esp_part = part.path
            esp_mount = f"/mnt/install/{os.path.basename(part.path)}"
            if not dry_run:
                _verify_existing_partition(plan, part.path, "fat32")
                if plan.esp_partuuid:
                    current_uuid = subprocess.check_output(["blkid", "-s", "PARTUUID", "-o", "value", part.path], universal_newlines=True).strip()
                    if current_uuid != plan.esp_partuuid:
                        raise RuntimeError(_("Planned ESP identity changed after the plan was created."))
                mount_partition(part.path, esp_mount)
            continue
        if part.action != "create":
            continue
        path = part.path or part_name(plan.device, _partition_index(plan, part))
        if part.role == "minios_root":
            root_part = path
            root_mount = f"/mnt/install/{os.path.basename(path)}"
            if not dry_run:
                log_cb(_("Formatting root partition {path} ({fstype})...").format(path=path, fstype=part.fstype))
                format_partitions(path, part.fstype, None)
                mount_partition(path, root_mount, fstype=part.fstype)
        elif part.role == "esp":
            esp_part = path
            esp_mount = f"/mnt/install/{os.path.basename(path)}"
            if not dry_run:
                log_cb(_("Formatting ESP {path}...").format(path=path))
                format_partitions(path, "fat32", None)
                mount_partition(path, esp_mount, fstype="fat32")
        elif part.role == "swap":
            _format_swap(path, log_cb, dry_run)

    if not root_part:
        raise RuntimeError(_("Partition plan does not contain a root partition."))
    if dry_run:
        root_mount = root_mount or f"/mnt/install/{os.path.basename(root_part)}"
    return root_part, esp_part, root_mount or "", esp_mount
