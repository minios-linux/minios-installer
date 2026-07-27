#!/usr/bin/env python3

import os
import sys
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


class TestMiniOSDeploy:
    def test_luks_persistence_defaults_to_raw_compatible_4000_mib(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        args = parser.parse_args(['plan', '/dev/sdb', '--persistence-mode', 'luks'])
        assert minios_deploy._effective_persistence_size(args) == 4000

    def test_luks_persistence_fat32_limit_is_4000_mib(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        accepted = parser.parse_args([
            'plan', '/dev/sdb', '--filesystem', 'fat32',
            '--persistence-mode', 'luks', '--persistence-size', '4000',
        ])
        minios_deploy._validate_cli_inputs(accepted)
        rejected = parser.parse_args([
            'plan', '/dev/sdb', '--filesystem', 'fat32',
            '--persistence-mode', 'luks', '--persistence-size', '4001',
        ])
        try:
            minios_deploy._validate_cli_inputs(rejected)
            assert False, 'expected FAT32 persistence limit failure'
        except ValueError as exc:
            assert '4000 MiB' in str(exc)

    def test_dynfilefs_persistence_is_not_limited_to_4000_mib_on_fat32(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        args = parser.parse_args([
            'plan', '/dev/sdb', '--filesystem', 'fat32',
            '--persistence-mode', 'dynfilefs', '--persistence-size', '16000',
        ])
        minios_deploy._validate_cli_inputs(args)
        assert minios_deploy._effective_persistence_size(args) == 16000

    def test_native_persistence_has_no_container_size(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        args = parser.parse_args([
            'plan', '/dev/sdb', '--filesystem', 'ext4',
            '--persistence-mode', 'native',
        ])
        minios_deploy._validate_cli_inputs(args)
        assert minios_deploy._effective_persistence_size(args) == 0

    def test_luks_cli_choice_is_hidden_without_initrd_capability(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=False)
        try:
            parser.parse_args(['plan', '/dev/sdb', '--persistence-mode', 'luks'])
            assert False, 'expected hidden LUKS mode to be rejected'
        except SystemExit as exc:
            assert exc.code == 2

    def test_main_without_command_returns_2(self):
        import minios_deploy

        assert minios_deploy.main([]) == 2

    def test_install_requires_root(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        args = parser.parse_args(['install', '/dev/sdb', '--yes'])
        with patch('os.geteuid', return_value=1000):
            assert minios_deploy.cmd_install(args) == 2

    def test_plan_normalizes_bare_device_name(self):
        import minios_deploy
        from partition_models import PartitionPlan

        parser = minios_deploy.build_parser()
        args = parser.parse_args(['plan', 'sdb', '--filesystem', 'ext4'])
        fake_layout = MagicMock()
        fake_plan = PartitionPlan(device='/dev/sdb', use_gpt=False, wipe_disk=True)
        with patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb') as safe, \
             patch('minios_deploy.scan_disk', return_value=fake_layout) as scan, \
             patch('minios_deploy.build_plan', return_value=fake_plan):
            assert minios_deploy.cmd_plan(args) == 0
            safe.assert_called_once_with('sdb')
            scan.assert_called_once_with('/dev/sdb')

    def test_plan_passes_calculated_module_requirement(self):
        import minios_deploy
        from partition_models import PartitionPlan

        parser = minios_deploy.build_parser()
        args = parser.parse_args(['plan', '/dev/sdb', '--mode', 'native'])
        fake_plan = PartitionPlan(device='/dev/sdb', use_gpt=False, wipe_disk=True)
        with patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.scan_disk', return_value=MagicMock()), \
             patch('minios_deploy._module_space_requirement', return_value=(['00-core.sb'], 3456)), \
             patch('minios_deploy.build_plan', return_value=fake_plan) as build:
            assert minios_deploy.cmd_plan(args) == 0

        assert build.call_args.kwargs['required_root_mib'] == 3456
        assert build.call_args.kwargs['alongside_size_mib'] == 0

    def test_luks_persistence_reserves_root_space_and_rejects_native_mode(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        args = parser.parse_args([
            'install', '/dev/sdb', '--yes', '--dry-run',
            '--persistence-mode', 'luks', '--persistence-size', '2048',
        ])
        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.get_device_identity', return_value={'path': '/dev/sdb'}), \
             patch('minios_deploy._module_space_requirement', return_value=(['00-core.sb'], 4096)) as requirement, \
             patch('minios_deploy.run_live_install') as run:
            assert minios_deploy.cmd_install(args) == 0
        assert requirement.call_args.args == ('live', '', 2048)
        state = run.call_args[0][0]
        assert state.persistence_mode == 'luks'
        assert state.persistence_size_mib == 2048
        assert state.required_root_mib == 4096

        native = parser.parse_args([
            'install', '/dev/sdb', '--mode', 'native', '--yes', '--dry-run',
            '--persistence-mode', 'luks', '--persistence-size', '2048',
        ])
        with patch('os.geteuid', return_value=0):
            try:
                minios_deploy.cmd_install(native)
                assert False, 'expected native persistence validation failure'
            except ValueError as exc:
                assert 'only with --mode live' in str(exc) or 'available only with --mode live' in str(exc)

    def test_luks_persistence_is_added_to_live_root_requirement(self):
        import minios_deploy

        with patch('minios_deploy.discover_module_names', return_value=['00-core.sb']), \
             patch('minios_deploy.normalize_selected_modules', return_value=['00-core.sb']), \
             patch('minios_deploy.payload_size_bytes', return_value=1), \
             patch('minios_deploy.required_root_mib', return_value=512):
            selected, root_mib = minios_deploy._module_space_requirement(
                'live', '00-core.sb', persistence_size_mib=2048,
            )

        assert selected == ['00-core.sb']
        assert root_mib == 2560

    def test_install_accepts_all_configurator_cli_flags(self):
        import minios_deploy

        parser = minios_deploy.build_parser()
        args = parser.parse_args([
            'install', '/dev/sdb', '--yes', '--dry-run',
            '--username', 'alice',
            '--full-name', 'Alice',
            '--user-groups', 'audio,video',
            '--password', 'secret',
            '--root-password', 'rootsecret',
            '--link-user-dirs', 'true',
            '--bind-user-dirs', 'false',
            '--user-dirs-path', '/minios/userdirs',
            '--noroot', 'no',
            '--hostname', 'box',
            '--locales', 'ru_RU.UTF-8',
            '--timezone', 'Europe/Moscow',
            '--default-target', 'multi-user',
            '--enable-services', 'ssh',
            '--disable-services', 'bluetooth',
            '--keyboard-model', 'pc105',
            '--keyboard-layouts', 'us,ru',
            '--keyboard-options', 'grp:alt_shift_toggle',
            '--keyboard-variants', ',',
            '--module-mode', 'merged',
            '--live-config-cmdline', 'live-config.debug',
            '--config-debug', 'yes',
            '--export-logs', 'on',
        ])
        user, customized = minios_deploy.user_config_from_args(args)
        assert customized is True
        assert user.username == 'alice'
        assert user.full_name == 'Alice'
        assert user.user_default_groups == 'audio,video'
        assert user.password == 'secret'
        assert user.root_password == 'rootsecret'
        assert user.link_user_dirs == 'true'
        assert user.bind_user_dirs == 'false'
        assert user.user_dirs_path == '/minios/userdirs'
        assert user.noroot == 'false'
        assert user.hostname == 'box'
        assert user.locale == 'ru_RU.UTF-8'
        assert user.timezone == 'Europe/Moscow'
        assert user.default_target == 'multi-user'
        assert user.enable_services == 'ssh'
        assert user.disable_services == 'bluetooth'
        assert user.keyboard_model == 'pc105'
        assert user.keyboard == 'us,ru'
        assert user.keyboard_options == 'grp:alt_shift_toggle'
        assert user.keyboard_variants == ','
        assert user.module_mode == 'merged'
        assert user.config_cmdline == 'live-config.debug'
        assert user.config_debug == 'true'
        assert user.export_logs == 'true'

        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.get_device_identity', return_value={'path': '/dev/sdb'}), \
             patch('minios_deploy.run_live_install') as run:
            assert minios_deploy.cmd_install(args) == 0
            state = run.call_args[0][0]
            assert state.user_config_customized is True
            assert state.user_config.username == 'alice'
            assert state.user_config.config_debug == 'true'
            assert state.security_profile == 'convenient'

    def test_install_security_profile_flag(self):
        import minios_deploy

        parser = minios_deploy.build_parser()
        args = parser.parse_args([
            'install', '/dev/sdb', '--yes', '--dry-run',
            '--mode', 'native', '--security-profile', 'strict',
        ])
        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.get_device_identity', return_value={'path': '/dev/sdb'}), \
             patch('minios_deploy.run_native_install') as native:
            assert minios_deploy.cmd_install(args) == 0
            assert native.call_args[0][0].security_profile == 'strict'

    def test_install_state_defaults_security_profile_by_mode(self):
        from install_state import InstallState

        live = InstallState(install_mode='live')
        native = InstallState(install_mode='native')
        assert live.security_profile == 'convenient'
        assert native.security_profile == 'balanced'

        live.set_install_mode('native')
        assert live.security_profile == 'balanced'

        live.security_profile = 'strict'
        live.set_install_mode('live')
        assert live.security_profile == 'strict'

    def test_install_config_file_missing_returns_2(self, tmp_path):
        import minios_deploy

        parser = minios_deploy.build_parser()
        args = parser.parse_args([
            'install', '/dev/sdb', '--yes', '--dry-run',
            '--config-file', str(tmp_path / 'nope.conf'),
        ])
        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'):
            assert minios_deploy.cmd_install(args) == 2

    def test_install_native_uses_native_runner(self):
        import minios_deploy

        parser = minios_deploy.build_parser()
        args = parser.parse_args(['install', '/dev/sdb', '--mode', 'native', '--yes', '--dry-run'])
        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.get_device_identity', return_value={'path': '/dev/sdb'}), \
             patch('minios_deploy.run_native_install') as native, \
             patch('minios_deploy.run_live_install') as live:
            assert minios_deploy.cmd_install(args) == 0
            assert native.called
            assert not live.called
            assert native.call_args[0][0].install_mode == 'native'

    def test_install_native_download_packages_flag(self):
        import minios_deploy

        parser = minios_deploy.build_parser()
        args = parser.parse_args(['install', '/dev/sdb', '--mode', 'native', '--download-packages', '--yes', '--dry-run'])
        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.get_device_identity', return_value={'path': '/dev/sdb'}), \
             patch('minios_deploy.run_native_install') as native:
            assert minios_deploy.cmd_install(args) == 0
            assert native.call_args[0][0].download_missing_packages is True

    def test_install_native_boot_layout_flag(self):
        import minios_deploy

        parser = minios_deploy.build_parser()
        args = parser.parse_args(['install', '/dev/sdb', '--mode', 'native', '--boot-layout', 'uefi_gpt', '--yes', '--dry-run'])
        with patch('os.geteuid', return_value=0), \
             patch('minios_deploy.ensure_safe_target_device', return_value='/dev/sdb'), \
             patch('minios_deploy.get_device_identity', return_value={'path': '/dev/sdb'}), \
             patch('minios_deploy.run_native_install') as native:
            assert minios_deploy.cmd_install(args) == 0
            assert native.call_args[0][0].boot_layout == 'uefi_gpt'

    def test_native_extlinux_uses_rw_root(self, tmp_path):
        import native_deploy

        target = tmp_path / 'target'
        (target / 'boot' / 'extlinux').mkdir(parents=True)
        source = tmp_path / 'source'
        (source / 'minios' / 'boot' / 'syslinux').mkdir(parents=True)
        for name in ['extlinux.x64', 'mbr.bin']:
            (source / 'minios' / 'boot' / 'syslinux' / name).write_text('x')

        with patch('native_deploy.get_live_source_mount', return_value=str(source)), \
             patch('native_deploy._run'):
            native_deploy._install_extlinux_native(
                str(target),
                '/dev/sda',
                '/dev/sda1',
                'ROOT-UUID',
                '/boot/vmlinuz-test',
                '/boot/initrd.img-test',
                lambda _msg: None,
                dry_run=False,
            )

        conf = (target / 'boot' / 'extlinux' / 'extlinux.conf').read_text()
        assert 'APPEND root=UUID=ROOT-UUID rw quiet' in conf

    def test_native_initramfs_uses_update_initramfs_when_dracut_missing(self, tmp_path):
        import native_deploy

        target = tmp_path / 'target'
        (target / 'usr/sbin').mkdir(parents=True)
        (target / 'usr/sbin/update-initramfs').write_text('#!/bin/sh\n', encoding='utf-8')
        calls = []
        with patch('native_deploy._chroot', side_effect=lambda _target, args, *_a, **_kw: calls.append(args)):
            assert native_deploy._generate_native_initramfs(str(target), '6.1.0-test', False, lambda *_: None) == '/boot/initrd.img-6.1.0-test'
        assert calls == [['update-initramfs', '-c', '-k', '6.1.0-test']]

    def test_native_bootloader_prefers_grub_when_available(self, tmp_path):
        import native_deploy

        target = tmp_path / 'target'
        (target / 'usr/sbin').mkdir(parents=True)
        (target / 'usr/lib/grub/i386-pc').mkdir(parents=True)
        (target / 'usr/sbin/grub-install').write_text('x')
        (target / 'usr/sbin/update-grub').write_text('x')
        (target / 'usr/bin').mkdir(parents=True)
        (target / 'usr/bin/grub-script-check').write_text('x')
        (target / 'usr/lib/grub/i386-pc/modinfo.sh').write_text('x')
        (target / 'boot/grub').mkdir(parents=True)
        (target / 'boot/grub/grub.cfg').write_text('menuentry test {}\n')

        with patch('native_deploy._mount_chroot_api'), \
             patch('native_deploy._unmount_chroot_api'), \
             patch('native_deploy._kernel_version', return_value='test'), \
             patch('native_deploy._copy_native_kernel', return_value='/boot/vmlinuz-test'), \
             patch('native_deploy._generate_native_initramfs', return_value='/boot/initrd.img-test'), \
             patch('native_deploy._install_extlinux_native') as extlinux, \
             patch('native_deploy._chroot') as chroot:
            native_deploy._install_native_bootloader(
                str(target), '/dev/sda', '/dev/sda1', False, None, lambda *_: None, lambda _msg: None
            )

        assert not extlinux.called
        assert chroot.call_args_list[0][0][1] == ['grub-install', '--target=i386-pc', '--recheck', '/dev/sda']
        assert chroot.call_args_list[1][0][1] == ['update-grub']
        assert chroot.call_args_list[2][0][1] == ['grub-script-check', '/boot/grub/grub.cfg']
        assert (target / 'etc/default/grub.d/minios-native.cfg').read_text() == 'GRUB_CMDLINE_LINUX="rw"\n'

    def test_native_bootloader_falls_back_to_extlinux_on_mbr_without_grub(self, tmp_path):
        import native_deploy

        target = tmp_path / 'target'
        target.mkdir()

        with patch('native_deploy._mount_chroot_api'), \
             patch('native_deploy._unmount_chroot_api'), \
             patch('native_deploy._kernel_version', return_value='test'), \
             patch('native_deploy._copy_native_kernel', return_value='/boot/vmlinuz-test'), \
             patch('native_deploy._generate_native_initramfs', return_value='/boot/initrd.img-test'), \
             patch('native_deploy._blkid_value', return_value='ROOT-UUID'), \
             patch('native_deploy._install_extlinux_native') as extlinux:
            native_deploy._install_native_bootloader(
                str(target), '/dev/sda', '/dev/sda1', False, None, lambda *_: None, lambda _msg: None
            )

        assert extlinux.called

    def test_native_bootloader_requires_grub_for_gpt(self, tmp_path):
        import native_deploy
        import pytest

        target = tmp_path / 'target'
        target.mkdir()

        with patch('native_deploy._mount_chroot_api'), \
             patch('native_deploy._unmount_chroot_api'), \
             patch('native_deploy._kernel_version', return_value='test'), \
             patch('native_deploy._copy_native_kernel', return_value='/boot/vmlinuz-test'), \
             patch('native_deploy._generate_native_initramfs', return_value='/boot/initrd.img-test'):
            with pytest.raises(RuntimeError, match='GRUB EFI packages'):
                native_deploy._install_native_bootloader(
                    str(target), '/dev/sda', '/dev/sda1', True, '/mnt/esp', lambda *_: None, lambda _msg: None
                )

    def test_native_package_install_preseeds_grub_pc(self, tmp_path):
        import native_deploy
        from install_state import InstallState

        state = InstallState(target_device='/dev/sda', download_missing_packages=True)
        calls = []

        with patch('native_deploy.native_missing_packages', return_value=['grub-pc', 'grub-common']), \
             patch('native_deploy._chroot', side_effect=lambda *args, **kwargs: calls.append((args, kwargs))):
            native_deploy._install_native_packages(str(tmp_path), False, 'ext4', state, lambda _msg: None)

        assert calls[0][0][1][-1:] == ['debconf-set-selections']
        assert calls[0][1]['input_text'] == 'grub-pc grub-pc/install_devices multiselect /dev/sda\n'
        assert calls[1][0][1][-2:] == ['apt-get', 'update']
        assert calls[2][0][1][-6:] == [
            'apt-get', '--no-download', '--fix-broken', 'install', '-y', '--no-install-recommends',
        ]
        assert calls[3][0][1][-7:] == [
            'apt-get', '--no-download', 'install', '-y', '--no-install-recommends',
            'grub-pc', 'grub-common',
        ]

    def test_native_copy_reserves_100_percent_for_completed_tar(self, tmp_path):
        import native_deploy

        source = tmp_path / 'source'
        target = tmp_path / 'target'
        source.mkdir()
        target.mkdir()
        (source / 'payload').write_bytes(b'x' * (3 * 1024 * 1024))
        progress = []

        native_deploy._copy_native_root(
            str(source), str(target), lambda percent, message: progress.append((percent, message)), lambda _message: None
        )

        copy_messages = [message for _percent, message in progress if 'Copying system files' in message]
        assert copy_messages
        assert all('100%' not in message for message in copy_messages)
        assert '100%' in progress[-1][1]
        assert (target / 'payload').stat().st_size == 3 * 1024 * 1024

    def test_chroot_logs_command_output_on_failure(self, tmp_path):
        import subprocess
        import native_deploy
        import pytest

        logs = []

        with patch('native_deploy.subprocess.run', return_value=subprocess.CompletedProcess(['cmd'], 100, stdout='apt error\nmore detail\n')):
            with pytest.raises(subprocess.CalledProcessError):
                native_deploy._chroot(str(tmp_path), ['apt-get', 'install'], logs.append)

        assert 'apt error' in logs
        assert 'more detail' in logs

    def test_chroot_redacts_password_hash_from_command_log(self, tmp_path):
        import subprocess
        import native_deploy

        logs = []
        password_hash = '$y$j9T$secret-hash'

        with patch('native_deploy.subprocess.run', return_value=subprocess.CompletedProcess(['cmd'], 0, stdout='')) as run:
            native_deploy._chroot(str(tmp_path), ['usermod', '-p', password_hash, 'alice'], logs.append)

        assert password_hash not in '\n'.join(logs)
        assert '<redacted>' in logs[0]
        assert run.call_args.args[0][-2] == password_hash

    def test_chroot_mounts_and_unmounts_dev_pts(self, tmp_path):
        import native_deploy

        commands = []
        with patch('native_deploy._run', side_effect=lambda command, *_args, **_kwargs: commands.append(command)):
            native_deploy._mount_chroot_api(str(tmp_path), None, False, lambda _message: None)

        assert ['mount', '--bind', '/dev/pts', str(tmp_path / 'dev' / 'pts')] in commands

        with patch('native_deploy.subprocess.run') as run:
            native_deploy._unmount_chroot_api(str(tmp_path), None, lambda _message: None)

        unmounts = [call.args[0] for call in run.call_args_list]
        assert unmounts[0] == ['umount', str(tmp_path / 'dev' / 'pts')]

    def test_offline_native_keeps_kernel_metadata_inactive(self, tmp_path):
        import native_deploy
        from install_state import InstallState

        metadata = tmp_path / 'usr' / 'share' / 'minios' / 'kernel-dpkg'
        metadata.mkdir(parents=True)
        (metadata / 'manifest.json').write_text('{"packages": []}')
        logs = []

        state = InstallState(download_missing_packages=False)
        with patch('native_deploy.restore_kernel_dpkg_metadata') as restore:
            assert native_deploy._maybe_restore_kernel_metadata(str(tmp_path), state, logs.append) == 0

        assert not restore.called
        assert any('inactive for offline fallback' in message for message in logs)
