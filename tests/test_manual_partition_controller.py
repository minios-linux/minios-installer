import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from manual_partition_controller import ManualPartitionController
from manual_partitioning import ExistingPartitionRef, LayoutSnapshot, SectorExtent


def snapshot():
    root = ExistingPartitionRef(1, 'root', 2048, 20000, 'linux', 'ext4')
    home = ExistingPartitionRef(2, 'home', 24000, 10000, 'linux', 'ext4')
    return LayoutSnapshot('/dev/sda', 512, 80000, 'msdos', (root, home),
                          (SectorExtent(0, 2048), SectorExtent(34000, 46000)))


def controller():
    return ManualPartitionController(snapshot(), required_root_sectors=1000)


def test_staged_edit_mountpoint_and_summary_are_in_memory_only():
    state = controller()
    root, home = state.snapshot.partitions
    state.use_as(root, 'root', '/', 'ext4')
    state.use_as(home, 'data', '/var/lib/build', 'ext4')
    assert state.plan is not None
    assert 'Assign partition 2 as data (/var/lib/build, keep)' in state.summary_lines()
    assert not state.destructive


def test_create_delete_resize_undo_and_reset_are_staged():
    state = controller()
    root, home = state.snapshot.partitions
    state.use_as(root, 'root', '/', 'ext4')
    state.create(34816, 4096)
    assert state.destructive
    assert any(action.kind == 'create' for action in state.actions)
    state.undo()
    assert not any(action.kind == 'create' for action in state.actions)
    state.resize(home, 8192)
    assert any(action.kind == 'shrink' for action in state.actions)
    state.delete(home)
    assert any(action.kind == 'delete' for action in state.actions)
    state.reset()
    assert not state.actions and not state.assignments
    assert state.plan is None and 'root' in state.error


def test_existing_format_requires_explicit_choice_and_invalid_mount_blocks_plan():
    state = controller()
    root, home = state.snapshot.partitions
    state.use_as(root, 'root', '/', 'ext4')
    state.use_as(home, 'data', '/home', 'ext4', True)
    assert any(action.kind == 'format' for action in state.actions)
    state.undo()
    state.use_as(home, 'data', 'relative', 'ext4')
    assert state.plan is None
    assert 'mountpoint' in state.error


def test_existing_filesystem_is_detected_and_locked_without_format():
    state = controller()
    root, home = state.snapshot.partitions
    state.use_as(root, 'root', '/', 'btrfs')
    assert state.plan is None
    assert 'filesystem must match' in state.error
    state.use_as(root, 'root', '/', 'ext4')
    assert state.plan is not None


def test_controller_surfaces_bios_gpt_limitation():
    state = ManualPartitionController(snapshot()._replace(partition_table='gpt'), use_efi=False)
    root = state.snapshot.partitions[0]
    state.use_as(root, 'root', '/', 'ext4')
    assert state.plan is None
    assert 'BIOS manual installation on GPT' in state.error
