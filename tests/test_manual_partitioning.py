import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from manual_partitioning import (ExistingPartitionRef, LayoutSnapshot, ManualAction,
                                 ManualPlanError, ManualPlanner, MountAssignment,
                                 SectorExtent)


def snapshot():
    return LayoutSnapshot('/dev/sda', 512, 400000, 'msdos', [
        ExistingPartitionRef(1, 'root-id', 2048, 40000, '0fc63daf', 'ext4'),
        ExistingPartitionRef(2, 'esp-id', 43008, 262144, 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b', 'vfat'),
    ], [SectorExtent(34, 2014), SectorExtent(305152, 94814)])


def valid_plan(**kwargs):
    layout = snapshot()._replace(partition_table='gpt')
    refs = layout.partitions
    required = kwargs.pop('required_root_sectors', 1000)
    return ManualPlanner().stage(layout, [ManualAction('keep', refs[0]), ManualAction('keep', refs[1])], [
        MountAssignment(refs[0], 'root', '/', 'ext4'),
        MountAssignment(refs[1], 'esp', '/boot/efi', 'vfat'),
    ], use_efi=True, required_root_sectors=required, **kwargs)


def test_stages_immutable_sector_exact_plan_and_serializes():
    plan = valid_plan()
    assert plan.as_dict()['snapshot']['partitions'][0]['partuuid'] == 'root-id'
    assert 'Assign partition 1 as root' in plan.summary_lines()[-2]
    with pytest.raises(AttributeError):
        plan.snapshot.partitions[0].partuuid = 'changed'


@pytest.mark.parametrize('mount', ['/var/lib/data', '/srv/build-output'])
def test_allows_arbitrary_safe_absolute_mountpoints(mount):
    refs = snapshot().partitions
    plan = ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[0], 'root', '/', 'ext4'), MountAssignment(refs[1], 'data', mount, 'vfat')])
    assert plan.assignments[-1].mountpoint == mount


@pytest.mark.parametrize('mount', ['', 'relative', '/a//b', '/a/../b'])
def test_rejects_unsafe_mountpoints(mount):
    refs = snapshot().partitions
    with pytest.raises(ManualPlanError):
        ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[0], 'root', '/', 'ext4'), MountAssignment(refs[1], 'data', mount, 'vfat')])


def test_rejects_overlap_stale_identity_unsupported_and_bad_swap():
    refs = snapshot().partitions
    with pytest.raises(ManualPlanError, match='overlap'):
        ManualPlanner().stage(snapshot(), [ManualAction('create', extent=SectorExtent(307200, 4096)), ManualAction('create', extent=SectorExtent(309248, 4096))], [MountAssignment(refs[0], 'root', '/', 'ext4')])
    stale = ExistingPartitionRef(1, 'other-id', 2048, 40000, '0fc63daf', 'ext4')
    with pytest.raises(ManualPlanError, match='stale'):
        ManualPlanner().stage(snapshot(), [ManualAction('keep', stale)], [MountAssignment(refs[0], 'root', '/', 'ext4')])
    with pytest.raises(ManualPlanError, match='unsupported'):
        ManualPlanner().stage(snapshot(), [ManualAction('lvm')], [MountAssignment(refs[0], 'root', '/', 'ext4')])
    with pytest.raises(ManualPlanError, match='swap'):
        ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[0], 'root', '/', 'ext4'), MountAssignment(refs[1], 'swap', '/swap', 'swap')])


def test_validates_capacity_alignment_format_intent_and_uefi_esp():
    refs = snapshot().partitions
    with pytest.raises(ManualPlanError, match='capacity'):
        valid_plan(required_root_sectors=50000)
    with pytest.raises(ManualPlanError, match='aligned'):
        ManualPlanner().stage(snapshot(), [ManualAction('create', extent=SectorExtent(307201, 2048))], [MountAssignment(refs[0], 'root', '/', 'ext4')])
    with pytest.raises(ManualPlanError, match='format intent'):
        ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[0], 'root', '/', 'ext4', True)])
    with pytest.raises(ManualPlanError, match='ESP'):
        ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[0], 'root', '/', 'ext4')], use_efi=True)


def test_rejects_missing_identity_duplicate_root_mount_and_incompatible_root():
    refs = snapshot().partitions
    with pytest.raises(ManualPlanError, match='identity'):
        ExistingPartitionRef(3, '', 60000, 2048, '0fc63daf', 'ext4')
    with pytest.raises(ManualPlanError, match='exactly one root'):
        ManualPlanner().stage(snapshot(), [], [
            MountAssignment(refs[0], 'root', '/', 'ext4'),
            MountAssignment(refs[1], 'root', '/', 'vfat'),
        ])
    with pytest.raises(ManualPlanError, match='unique'):
        ManualPlanner().stage(snapshot(), [], [
            MountAssignment(refs[0], 'root', '/', 'ext4'),
            MountAssignment(refs[1], 'data', '/srv', 'vfat'),
            MountAssignment(refs[1], 'logs', '/srv', 'vfat'),
        ])
    with pytest.raises(ManualPlanError, match='native root'):
        ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[1], 'root', '/', 'vfat')])


def test_rejects_bios_manual_plan_on_gpt_without_bios_grub_support():
    refs = snapshot().partitions
    gpt = snapshot()._replace(partition_table='gpt')
    with pytest.raises(ManualPlanError, match='BIOS.*GPT.*bios_grub'):
        ManualPlanner().stage(gpt, [], [MountAssignment(refs[0], 'root', '/', 'ext4')])


def test_shrink_delete_and_existing_format_are_staged_without_execution():
    refs = snapshot().partitions
    plan = ManualPlanner().stage(snapshot(), [
        ManualAction('shrink', refs[0], SectorExtent(2048, 20480)),
        ManualAction('format', refs[0], fstype='ext4'),
        ManualAction('delete', refs[1]),
    ], [MountAssignment(refs[0], 'root', '/', 'ext4', format=True)])
    assert plan.summary_lines()[1:4] == [
        'Delete partition 2', 'Shrink partition 1: 2048-42047 to 2048-22527 sectors',
        'Format partition 1 as ext4']


def test_reserves_partition_table_metadata_and_validates_existing_filesystem_intent():
    refs = snapshot().partitions
    with pytest.raises(ManualPlanError, match='free space'):
        ManualPlanner().stage(snapshot(), [ManualAction('create', extent=SectorExtent(0, 2048))],
                              [MountAssignment(refs[0], 'root', '/', 'ext4')])
    with pytest.raises(ManualPlanError, match='filesystem must match'):
        ManualPlanner().stage(snapshot(), [], [MountAssignment(refs[0], 'root', '/', 'btrfs')])
    tiny_esp = ExistingPartitionRef(3, 'tiny', 307200, 2048,
                                    'c12a7328-f81f-11d2-ba4b-00a0c93ec93b', 'vfat')
    with pytest.raises(ManualPlanError, match='ESP'):
        ManualPlanner().stage(LayoutSnapshot('/dev/sda', 512, 400000, 'gpt', refs + (tiny_esp,),
                                             (SectorExtent(34, 2014), SectorExtent(309248, 90718))), [],
                              [MountAssignment(refs[0], 'root', '/', 'ext4'),
                               MountAssignment(tiny_esp, 'esp', '/boot/efi', 'vfat')], use_efi=True)
