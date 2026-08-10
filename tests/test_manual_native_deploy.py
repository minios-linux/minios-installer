import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from manual_executor import InstallTarget, ManualExecutionResult
from manual_partitioning import ExistingPartitionRef, LayoutSnapshot, ManualAction, ManualPlanner, MountAssignment, SectorExtent, scan_manual_layout
from partition_models import DiskLayout, PartitionInfo


def _result():
    root = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    home = ExistingPartitionRef(2, 'home', 12048, 10000, 'linux', 'ext4')
    esp = ExistingPartitionRef(3, 'esp', 22048, 1000, 'efi', 'vfat')
    swap = ExistingPartitionRef(4, 'swap', 23048, 1000, 'swap', 'swap')
    assignments = (
        MountAssignment(root, 'root', '/', 'ext4'),
        MountAssignment(home, 'data', '/home', 'ext4'),
        MountAssignment(esp, 'esp', '/boot/efi', 'vfat'),
        MountAssignment(swap, 'swap', '', 'swap'),
    )
    targets = tuple(InstallTarget(ref, '/dev/sda{0}'.format(ref.number), ref.fstype)
                    for ref in (root, home, esp, swap))
    return ManualExecutionResult(targets, assignments)


def test_collection_mounts_children_before_copy_and_cleans_in_reverse(tmp_path):
    import native_deploy

    calls = []
    result = _result()
    with patch('native_deploy.tempfile.mkdtemp', return_value=str(tmp_path / 'owned')), \
         patch('native_deploy.mount_partition', side_effect=lambda part, target, fs: calls.append(('mount', part, target, fs))), \
         patch('native_deploy.unmount_mountpoints', side_effect=lambda points: calls.append(('unmount', tuple(points)))):
        root, _workdir, mounted, _entries = native_deploy._mount_manual_targets(result)
        calls.append(('copy', root))
        native_deploy.unmount_mountpoints(mounted)

    assert [item[1] for item in calls[:3]] == ['/dev/sda1', '/dev/sda2', '/dev/sda3']
    assert calls[3] == ('copy', root)
    assert calls[4][1] == tuple(mounted)


def test_fstab_has_uuid_root_children_esp_and_multiple_swap(tmp_path):
    import native_deploy

    entries = [
        ('/dev/sda1', '/', 'ext4', 'root'),
        ('/dev/sda2', '/home', 'ext4', 'data'),
        ('/dev/sda3', '/boot/efi', 'vfat', 'esp'),
        ('/dev/sda4', '', 'swap', 'swap'),
        ('/dev/sda5', '', 'swap', 'swap'),
    ]
    with patch('native_deploy._blkid_value', side_effect=lambda path, tag: ('UUID-' + path[-1]) if tag == 'UUID' else {'1': 'ext4', '2': 'ext4', '3': 'vfat'}[path[-1]]):
        native_deploy._write_assignment_fstab(str(tmp_path), entries, False, lambda _msg: None)
    assert (tmp_path / 'etc/fstab').read_text() == (
        'UUID=UUID-1 / ext4 defaults,noatime 0 1\n'
        'UUID=UUID-2 /home ext4 defaults,noatime 0 2\n'
        'UUID=UUID-3 /boot/efi vfat umask=0077 0 2\n'
        'tmpfs /tmp tmpfs defaults,nosuid,nodev 0 0\n'
        'UUID=UUID-4 none swap sw 0 0\n'
        'UUID=UUID-5 none swap sw 0 0\n'
    )


def test_native_manual_failure_cleans_mounted_collection(tmp_path):
    import native_deploy
    from install_state import InstallState

    result = _result()
    state = InstallState(install_mode='native', target_device='/dev/sda')
    state.manual_partition_plan = object()
    mounted = [str(tmp_path / 'root'), str(tmp_path / 'root/home')]
    with patch('native_deploy._preflight_manual_native'), \
         patch('native_deploy.execute_manual_plan', return_value=result), \
         patch('native_deploy._mount_manual_targets', return_value=(
             mounted[0], str(tmp_path), mounted, native_deploy._manual_assignment_entries(result))), \
         patch('native_deploy.BundleOverlay') as overlay, \
         patch('native_deploy._copy_native_root', side_effect=RuntimeError('copy failed')), \
         patch('native_deploy.unmount_mountpoints') as unmount:
        overlay.return_value.__enter__.return_value = '/source'
        with pytest.raises(RuntimeError, match='copy failed'):
            native_deploy._run_manual_native_install(state, lambda *_: None, lambda *_: None)
    unmount.assert_called_once_with(mounted)


def test_manual_preflight_rejects_unavailable_format_tool_before_execution():
    import native_deploy

    ref = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    snapshot = LayoutSnapshot('/dev/sda', 512, 20000, 'msdos', (ref,))
    plan = ManualPlanner().stage(snapshot, [ManualAction('format', ref, fstype='ext4')],
                                 [MountAssignment(ref, 'root', '/', 'ext4', True)])
    with patch('native_deploy.shutil.which', return_value=None):
        with pytest.raises(RuntimeError, match='unavailable tools'):
            native_deploy._preflight_manual_native(plan)


def test_manual_preflight_covers_partition_resize_format_and_mount_tools():
    import native_deploy

    ref = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    snapshot = LayoutSnapshot('/dev/sda', 512, 20000, 'msdos', (ref,))
    plan = ManualPlanner().stage(
        snapshot,
        [ManualAction('shrink', ref, extent=SectorExtent(2048, 8192)),
         ManualAction('format', ref, fstype='ext4')],
        [MountAssignment(ref, 'root', '/', 'ext4', True)],
    )
    with patch('native_deploy.shutil.which', return_value='/tool') as which:
        native_deploy._preflight_manual_native(plan)
    required = {mock_call[0][0] for mock_call in which.call_args_list}
    assert {'sfdisk', 'partprobe', 'blockdev', 'udevadm', 'e2fsck', 'resize2fs',
            'mkfs.ext4', 'mount', 'umount', 'blkid', 'lsblk'} <= required


def test_manual_preflight_stages_boot_packages_even_when_live_host_has_them():
    import native_deploy
    from install_state import InstallState

    root = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    plan = ManualPlanner().stage(LayoutSnapshot('/dev/sda', 512, 20000, 'msdos', (root,)), [],
                                 [MountAssignment(root, 'root', '/', 'ext4')])
    state = InstallState(install_mode='native', placement='manual', download_missing_packages=True)
    with patch('native_deploy.preflight_selected_bundles', return_value=1), \
         patch('native_deploy.native_missing_packages', return_value=[]), \
         patch('native_deploy.preflight_package_download', return_value={'missing': [], 'apt_available': False}), \
         patch('native_deploy.prepare_package_cache', return_value='/cache') as stage:
        native_deploy._refresh_manual_preflight(state, plan)

    assert state.package_cache_path == '/cache'
    assert 'grub-pc' in stage.call_args[0][0]
    assert 'grub-common' in stage.call_args[0][0]


def test_manual_preflight_refuses_unproven_target_when_downloads_disabled():
    import native_deploy
    from install_state import InstallState

    root = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    plan = ManualPlanner().stage(LayoutSnapshot('/dev/sda', 512, 20000, 'msdos', (root,)), [],
                                 [MountAssignment(root, 'root', '/', 'ext4')])
    state = InstallState(install_mode='native', placement='manual', download_missing_packages=False)

    with patch('native_deploy.preflight_selected_bundles', return_value=1):
        with pytest.raises(RuntimeError, match='cannot prove'):
            native_deploy._refresh_manual_preflight(state, plan)


def test_manual_preflight_accepts_staged_boot_package_cache():
    import native_deploy
    from install_state import InstallState

    root = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    plan = ManualPlanner().stage(LayoutSnapshot('/dev/sda', 512, 20000, 'msdos', (root,)), [],
                                 [MountAssignment(root, 'root', '/', 'ext4')])
    state = InstallState(install_mode='native', placement='manual', download_missing_packages=True)
    with patch('native_deploy.preflight_selected_bundles', return_value=1), \
         patch('native_deploy.preflight_package_download', return_value={'missing': [], 'apt_available': False}), \
         patch('native_deploy.prepare_package_cache', return_value='/cache'):
        native_deploy._refresh_manual_preflight(state, plan)

    assert state.package_cache_path == '/cache'


def test_manual_bootloader_does_not_fall_back_to_extlinux(tmp_path):
    import native_deploy

    target = tmp_path / 'target'
    target.mkdir()
    with patch('native_deploy._mount_chroot_api'), \
         patch('native_deploy._unmount_chroot_api'), \
         patch('native_deploy._kernel_version', return_value='test'), \
         patch('native_deploy._copy_native_kernel', return_value='/boot/vmlinuz-test'), \
         patch('native_deploy._generate_native_initramfs', return_value='/boot/initrd.img-test'), \
         patch('native_deploy._install_extlinux_native') as extlinux:
        with pytest.raises(RuntimeError, match='requires GRUB packages'):
            native_deploy._install_native_bootloader(
                str(target), '/dev/sda', '/dev/sda1', False, None,
                lambda *_: None, lambda _msg: None, require_grub=True,
            )

    assert not extlinux.called


def test_manual_deploy_preflight_rejects_bios_gpt_even_for_injected_plan():
    import native_deploy

    ref = ExistingPartitionRef(1, 'root', 2048, 10000, 'linux', 'ext4')
    plan = type('Plan', (), {'use_efi': False,
                             'snapshot': LayoutSnapshot('/dev/sda', 512, 20000, 'gpt', (ref,)),
                             'assignments': (), 'actions': ()})()
    with pytest.raises(Exception, match='BIOS.*GPT.*bios_grub'):
        native_deploy._preflight_manual_native(plan)


def test_reused_home_is_not_formatted_but_explicit_existing_format_is():
    from manual_executor import execute_manual_plan

    reused = _result()
    root_ref, home_ref = (target.ref for target in reused.targets[:2])
    layout = DiskLayout('/dev/sda', 100, partition_table='msdos', logical_sector_size=512,
                        size_sectors=30000, partitions=[
                            PartitionInfo('sda1', '/dev/sda1', 1, fstype='ext4', start_sector=2048,
                                          size_sectors=10000, partition_number=1, parttype='linux', partuuid='root'),
                            PartitionInfo('sda2', '/dev/sda2', 1, fstype='ext4', start_sector=12048,
                                          size_sectors=10000, partition_number=2, parttype='linux', partuuid='home'),
                        ])
    snapshot = scan_manual_layout(layout)
    root_ref, home_ref = snapshot.partitions
    reused_plan = ManualPlanner().stage(snapshot, [], [
        MountAssignment(root_ref, 'root', '/', 'ext4'),
        MountAssignment(home_ref, 'data', '/home', 'ext4'),
    ])
    formatted_plan = ManualPlanner().stage(snapshot, [ManualAction('format', home_ref, fstype='ext4')], [
        MountAssignment(root_ref, 'root', '/', 'ext4'),
        MountAssignment(home_ref, 'data', '/home', 'ext4', True),
    ])
    reused_log, formatted_log = [], []
    execute_manual_plan(reused_plan, reused_log.append, lambda _device: layout, dry_run=True)
    execute_manual_plan(formatted_plan, formatted_log.append, lambda _device: layout, dry_run=True)
    assert not any('mkfs' in line for line in reused_log)
    assert any('mkfs.ext4' in line and '/dev/sda2' in line for line in formatted_log)
