"""Checks for installation from a live system copied into RAM."""
import json
import stat
from unittest.mock import Mock
import pytest
import disk_utils

MEMORY = '/run/initramfs/memory'

@pytest.fixture
def ram_tree(monkeypatch):
    rows = [
        dict(target='/', source='overlay', fstype='overlay'),
        dict(target=MEMORY, source='tmpfs', fstype='tmpfs'),
        dict(target=MEMORY + '/changes', source='tmpfs[/data/minios/changes/1]', fstype='tmpfs'),
        dict(target=MEMORY + '/bundles/core.sb', source='/dev/loop0', fstype='squashfs'),
    ]
    loops = {'/dev/loop0': '/memory/data/minios/core.sb'}
    def command(argv, _error):
        if argv[0] == 'findmnt':
            return json.dumps({'filesystems': rows})
        if argv[0] == 'losetup':
            return loops[argv[-1]]
        raise AssertionError(argv)
    monkeypatch.setattr(disk_utils, 'get_live_source_mount', lambda: MEMORY + '/data')
    monkeypatch.setattr(disk_utils, 'run_command', command)
    monkeypatch.setattr(disk_utils.os.path, 'isdir', lambda _path: True)
    return rows, loops

@pytest.mark.parametrize('root_type', ['overlay', 'aufs'])
def test_ram_source_with_native_session(ram_tree, root_type):
    rows, _loops = ram_tree
    rows[0]['fstype'] = root_type
    assert disk_utils.live_source_is_ram_backed()

def test_ram_backed_container_session(ram_tree):
    rows, loops = ram_tree
    rows[2].update(source='/dev/loop1', fstype='ext4')
    loops['/dev/loop1'] = '/memory/data/minios/changes/1/changes.img'
    assert disk_utils.live_source_is_ram_backed()

@pytest.mark.parametrize('part', ['data', 'changes', 'modules'])
def test_disk_backed_live_subtree_stays_protected(ram_tree, part):
    rows, _loops = ram_tree
    if part == 'changes':
        rows[2].update(source='/dev/sda1', fstype='ext4')
    else:
        rows.append(dict(target=MEMORY + '/' + part, source='/dev/sda1', fstype='ext4'))
    assert not disk_utils.live_source_is_ram_backed()

def test_loop_backing_on_physical_media_stays_protected(ram_tree):
    rows, loops = ram_tree
    rows.append(dict(target='/media/disk', source='/dev/sda1', fstype='ext4'))
    loops['/dev/loop0'] = '/media/disk/core.sb'
    assert not disk_utils.live_source_is_ram_backed()

@pytest.mark.parametrize('backing', ['/memory/bundles/core.sb', '/memory/data/core.sb (deleted)', ''])
def test_unverifiable_loop_is_rejected(ram_tree, backing):
    _rows, loops = ram_tree
    loops['/dev/loop0'] = backing
    assert not disk_utils.live_source_is_ram_backed()

def test_missing_modules_are_not_a_ram_boot(ram_tree):
    rows, _loops = ram_tree
    rows.pop()
    assert not disk_utils.live_source_is_ram_backed()

def test_shadowed_mount_is_rejected(ram_tree):
    rows, _loops = ram_tree
    rows.append(dict(target=MEMORY, source='/dev/sda1', fstype='ext4'))
    assert not disk_utils.live_source_is_ram_backed()

@pytest.mark.parametrize('data', ['{}', 'null', '{"filesystems":null}', '{"filesystems":[{}]}', 'invalid'])
def test_mount_probe_errors_fail_closed(ram_tree, monkeypatch, data):
    monkeypatch.setattr(disk_utils, 'run_command', lambda *_args: data)
    assert not disk_utils.live_source_is_ram_backed()

@pytest.mark.parametrize('ram_only', [False, True])
def test_unknown_live_disk_is_permitted_only_after_ram_proof(monkeypatch, ram_only):
    monkeypatch.setattr(disk_utils.os.path, 'exists', lambda _p: True)
    monkeypatch.setattr(disk_utils.os.path, 'islink', lambda _p: False)
    monkeypatch.setattr(disk_utils.os, 'stat', lambda _p: Mock(st_mode=stat.S_IFBLK))
    monkeypatch.setattr(disk_utils, '_lsblk_type', lambda _p: 'disk')
    monkeypatch.setattr(disk_utils, 'get_live_root_disk', Mock(side_effect=RuntimeError('unavailable')))
    monkeypatch.setattr(disk_utils, 'live_source_is_ram_backed', lambda: ram_only)
    if ram_only:
        assert disk_utils.ensure_safe_target_device('/dev/sdb') == '/dev/sdb'
    else:
        with pytest.raises(RuntimeError, match='could not determine the source disk'):
            disk_utils.ensure_safe_target_device('/dev/sdb')
