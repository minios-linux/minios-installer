import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from manual_executor import execute_manual_plan, revalidate_manual_plan
from manual_partitioning import (ExistingPartitionRef, LayoutSnapshot, ManualAction,
                                 ManualPlanError, ManualPlanner, MountAssignment,
                                 SectorExtent, scan_manual_layout)
from partition_models import DiskLayout, PartitionInfo


def layout(parts=None, **kwargs):
    kwargs.setdefault('partition_table', 'msdos')
    return DiskLayout('/dev/sda', 200, logical_sector_size=512,
                       size_sectors=400000, partitions=parts or [
                          PartitionInfo('sda1', '/dev/sda1', 20, fstype='ext4', start_sector=2048,
                                        size_sectors=40000, partition_number=1, parttype='0fc63daf', partuuid='root'),
                           PartitionInfo('sda2', '/dev/sda2', 128, fstype='vfat', start_sector=43008,
                                         size_sectors=262144, partition_number=2, parttype='c12a7328-f81f-11d2-ba4b-00a0c93ec93b', partuuid='esp'),
                      ], **kwargs)


def plan(actions, assignments, use_efi=False):
    snapshot = scan_manual_layout(layout())
    return ManualPlanner().stage(snapshot, actions, assignments, use_efi=use_efi)


def test_adapter_captures_exact_free_extents_and_rejects_unsupported_layouts():
    snapshot = scan_manual_layout(layout())
    assert snapshot.free_extents == (SectorExtent(1, 2047), SectorExtent(42048, 960),
                                       SectorExtent(305152, 94848))
    for kwargs in ({'geometry_complete': False}, {'has_nested_layout': True}, {'has_mapped_layout': True}):
        with pytest.raises(ManualPlanError):
            scan_manual_layout(layout(**kwargs))
    extended = layout(parts=[PartitionInfo('sda1', '/dev/sda1', 20, fstype='ext4', start_sector=2048,
                                           size_sectors=40000, partition_number=1, parttype='05', partuuid='x')],
                      partition_table='msdos')
    with pytest.raises(ManualPlanError, match='extended'):
        scan_manual_layout(extended)
    logical = layout(parts=[PartitionInfo('sda5', '/dev/sda5', 20, fstype='ext4', start_sector=2048,
                                          size_sectors=40000, partition_number=5, parttype='83', partuuid='x')],
                     partition_table='msdos')
    with pytest.raises(ManualPlanError, match='extended'):
        scan_manual_layout(logical)


def test_revalidation_rejects_stale_layout_before_any_command_is_logged():
    snapshot = scan_manual_layout(layout())
    staged = ManualPlanner().stage(snapshot, [], [MountAssignment(snapshot.partitions[0], 'root', '/', 'ext4')])
    changed = layout()
    changed.partitions[0].size_sectors += 1
    logs = []
    with pytest.raises(ManualPlanError, match='changed'):
        execute_manual_plan(staged, logs.append, lambda _device: changed, dry_run=True)
    assert logs == []


def test_dry_run_orders_delete_shrink_create_esp_reread_then_format():
    snapshot = scan_manual_layout(layout(partition_table='gpt'))
    root, esp = snapshot.partitions
    staged = ManualPlanner().stage(snapshot, [
        ManualAction('delete', esp),
        ManualAction('shrink', root, SectorExtent(root.start_sector, 28672)),
        ManualAction('create', extent=SectorExtent(43008, 262144)),
        ManualAction('format', root, fstype='ext4'),
    ], [MountAssignment(root, 'root', '/', 'ext4', True),
        MountAssignment(SectorExtent(43008, 262144), 'esp', '/boot/efi', 'vfat')], use_efi=True)
    logs = []
    result = execute_manual_plan(staged, logs.append,
                                 lambda _device: layout(partition_table='gpt'), dry_run=True)
    commands = [line[2:] for line in logs if line.startswith('$ ')]
    assert commands[0].startswith('sfdisk --delete')
    assert commands[1].startswith('e2fsck')
    assert commands[5].startswith('sfdisk --no-reread -N')
    assert commands[6].startswith('parted -s /dev/sda set 2 esp on')
    assert commands[7].startswith('partprobe')
    assert commands[-1].startswith('mkfs.ext4')
    assert len(result.targets) == 2


def test_resize_failure_stops_before_create_and_cancellation_is_deferred():
    snapshot = scan_manual_layout(layout())
    root = snapshot.partitions[0]
    staged = plan([ManualAction('shrink', root, SectorExtent(root.start_sector, 28672)),
                   ManualAction('create', extent=SectorExtent(30720, 10240))],
                  [MountAssignment(root, 'root', '/', 'ext4')])
    logs = []
    with patch('partition_executor._apply_resize', side_effect=RuntimeError('resize failed')):
        with pytest.raises(RuntimeError, match='resize failed'):
            execute_manual_plan(staged, logs.append, lambda _device: layout(), dry_run=True)
    assert not any('sfdisk --no-reread' in line for line in logs)
    with patch('partition_executor._apply_resize') as resize:
        execute_manual_plan(staged, lambda _line: None, lambda _device: layout(), dry_run=True,
                            cancel_cb=lambda: False)
    assert resize.call_args.kwargs['cancel_cb'] is not None


def test_revalidation_helper_requires_matching_free_extents():
    snapshot = scan_manual_layout(layout())
    staged = ManualPlanner().stage(snapshot, [], [MountAssignment(snapshot.partitions[0], 'root', '/', 'ext4')])
    assert revalidate_manual_plan(staged, layout()) == snapshot


def test_mounted_existing_partition_is_rejected_before_commands():
    snapshot = scan_manual_layout(layout())
    staged = ManualPlanner().stage(snapshot, [ManualAction('delete', snapshot.partitions[0])],
                                   [MountAssignment(snapshot.partitions[1], 'root', '/', 'vfat')],
                                   install_mode='live')
    current = layout()
    current.partitions[0].mountpoint = '/mnt/data'
    with pytest.raises(ManualPlanError, match='mounted'):
        execute_manual_plan(staged, lambda _line: None, lambda _device: current, dry_run=True)


def test_created_assignment_is_formatted_only_with_explicit_format_action():
    snapshot = scan_manual_layout(layout())
    extent = SectorExtent(305152, 10240)
    staged = ManualPlanner().stage(
        snapshot,
        [ManualAction('create', extent=extent), ManualAction('format', extent, fstype='ext4')],
        [MountAssignment(extent, 'root', '/', 'ext4', True)],
    )
    logs = []
    execute_manual_plan(staged, logs.append, lambda _device: layout(), dry_run=True)
    assert any('mkfs.ext4' in line and '/dev/sda3' in line for line in logs)


def test_revalidation_accepts_by_id_transition_for_same_canonical_disk():
    by_id_layout = layout()
    by_id_layout.device = '/dev/disk/by-id/wwn-example'
    with patch('manual_partitioning.canonical_device_identity', return_value='/dev/sda'):
        snapshot = scan_manual_layout(by_id_layout)
    staged = ManualPlanner().stage(snapshot, [], [MountAssignment(snapshot.partitions[0], 'root', '/', 'ext4')])
    assert revalidate_manual_plan(staged, layout()) == snapshot


def test_revalidation_rejects_rebound_by_id_snapshot_without_gui():
    by_id_layout = layout()
    by_id_layout.device = '/dev/disk/by-id/wwn-example'
    with patch('manual_partitioning.canonical_device_identity', return_value='/dev/sda'):
        snapshot = scan_manual_layout(by_id_layout)
    staged = ManualPlanner().stage(snapshot, [], [MountAssignment(snapshot.partitions[0], 'root', '/', 'ext4')])
    with patch('manual_partitioning.canonical_device_identity', return_value='/dev/sdb'):
        with pytest.raises(ManualPlanError, match='changed'):
            revalidate_manual_plan(staged, by_id_layout)


def test_executor_preflights_required_tools_before_logging_commands():
    snapshot = scan_manual_layout(layout())
    staged = ManualPlanner().stage(
        snapshot, [ManualAction('delete', snapshot.partitions[1])],
        [MountAssignment(snapshot.partitions[0], 'root', '/', 'ext4')])
    logs = []
    with patch('manual_executor.shutil.which', return_value=None):
        with pytest.raises(ManualPlanError, match='unavailable tools'):
            execute_manual_plan(staged, logs.append, lambda _device: layout(), dry_run=False)
    assert logs == []


def test_active_holders_are_rejected_before_any_command():
    snapshot = scan_manual_layout(layout())
    staged = ManualPlanner().stage(snapshot, [ManualAction('format', snapshot.partitions[0], fstype='ext4')],
                                    [MountAssignment(snapshot.partitions[0], 'root', '/', 'ext4', True)])
    logs = []
    with patch('manual_executor._active_or_held', return_value='has active holders'):
        with pytest.raises(ManualPlanError, match='holders'):
            execute_manual_plan(staged, logs.append, lambda _device: layout(), dry_run=True)
    assert logs == []
