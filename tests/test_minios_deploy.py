#!/usr/bin/env python3

import hashlib
import json
import os
import sys
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


def write_efi_manifest(source):
    files = []
    for name in ('bootx64.efi', 'grubx64.efi', 'bootia32.efi', 'grubia32.efi'):
        path = source / 'EFI/boot' / name
        files.append({
            'path': '/EFI/boot/' + name,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    manifest = {
        'format': 1,
        'layout': 'dual-architecture-esp',
        'architectures': {
            'x64': {
                'vendor': 'debian', 'suite': 'trixie', 'grub_version': '2.12',
                'shim_path': '/EFI/boot/bootx64.efi', 'grub_path': '/EFI/boot/grubx64.efi',
            },
            'ia32': {
                'vendor': 'debian', 'suite': 'bookworm', 'grub_version': '2.06',
                'shim_path': '/EFI/boot/bootia32.efi', 'grub_path': '/EFI/boot/grubia32.efi',
            },
        },
        'files': files,
    }
    manifest_path = source / 'minios/boot/efi-manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')


class TestMiniOSDeploy:
    def test_cli_hides_native_mode_when_image_lacks_contracts(self):
        import minios_deploy

        with patch('minios_deploy.native_install_supported', return_value=False):
            parser = minios_deploy.build_parser(luks_available=True)
        try:
            parser.parse_args(['plan', '/dev/sdb', '--mode', 'native'])
            assert False, 'expected native mode to be hidden without native contracts'
        except SystemExit as exc:
            assert exc.code == 2

    def test_cli_rejects_native_mode_when_image_lacks_contracts(self):
        import minios_deploy

        parser = minios_deploy.build_parser(luks_available=True)
        args = parser.parse_args(['plan', '/dev/sdb', '--mode', 'native'])
        with patch('minios_deploy.native_install_supported', return_value=False):
            try:
                minios_deploy._validate_cli_inputs(args)
                assert False, 'expected native-mode rejection without native contracts'
            except ValueError as exc:
                assert 'only live installation' in str(exc)

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

        assert build.call_args[1]['required_root_mib'] == 3456
        assert build.call_args[1]['alongside_size_mib'] == 0

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
        assert requirement.call_args[0] == ('live', '', 2048)
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
        assert chroot.call_args_list[0][0][1] == ['update-grub']
        assert chroot.call_args_list[1][0][1] == ['grub-script-check', '/boot/grub/grub.cfg']
        assert chroot.call_args_list[2][0][1] == ['grub-install', '--target=i386-pc', '--recheck', '/dev/sda']
        assert (target / 'etc/default/grub.d/minios-native.cfg').read_text() == 'GRUB_CMDLINE_LINUX="rw"\n'

    def test_native_grub_creates_configuration_directory(self, tmp_path):
        import native_deploy

        target = tmp_path / 'target'
        (target / 'usr/sbin').mkdir(parents=True)
        (target / 'usr/sbin/update-grub').write_text('x', encoding='utf-8')

        def run_chroot(_target, command, *_args, **_kwargs):
            if command == ['update-grub']:
                assert (target / 'boot/grub').is_dir()
                (target / 'boot/grub/grub.cfg').write_text(
                    'menuentry test {}\n', encoding='utf-8')

        with patch('native_deploy._chroot', side_effect=run_chroot):
            native_deploy._install_grub_native(
                str(target), '/dev/sda', False,
                lambda *_: None, lambda _msg: None, False,
            )

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
            with pytest.raises(RuntimeError, match='verified EFI chain'):
                native_deploy._install_native_bootloader(
                    str(target), '/dev/sda', '/dev/sda1', True, '/mnt/esp', lambda *_: None, lambda _msg: None
                )

    def test_native_uefi_publication_preserves_foreign_tree(self, tmp_path):
        import native_deploy

        source = tmp_path / 'source'
        (source / 'EFI/boot').mkdir(parents=True)
        (source / 'EFI/boot/bootx64.efi').write_bytes(b'minios-loader')
        (source / 'EFI/boot/bootia32.efi').write_bytes(b'minios-loader-ia32')
        (source / 'EFI/boot/grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'EFI/boot/grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        for architecture in ('i386-efi', 'x86_64-efi'):
            modules = source / 'minios/boot/grub' / architecture
            modules.mkdir(parents=True)
            (modules / 'ext2.mod').write_bytes(b'ext2')
            (modules / 'grub.cfg').write_text(
                'search --file --set=root /.disk/info\n'
                'source /minios/boot/grub/grub.cfg\n', encoding='utf-8')
        target = tmp_path / 'target'
        (target / 'boot/efi/EFI/Microsoft/Boot').mkdir(parents=True)
        (target / 'boot/efi/EFI/Microsoft/Boot/bootmgfw.efi').write_bytes(b'windows-loader')
        (target / 'boot/efi/EFI/Boot').mkdir(parents=True)
        windows_fallback = target / 'boot/efi/EFI/Boot/bootx64.efi'
        windows_fallback.write_bytes(b'windows-fallback')
        (target / 'boot/efi/EFI/OEM').mkdir(parents=True)
        (target / 'boot/efi/EFI/OEM/preserve.txt').write_text('keep', encoding='utf-8')
        (target / 'boot/efi/EFI/OtherLinux').mkdir(parents=True)
        (target / 'boot/efi/EFI/OtherLinux/grub.cfg').write_text(
            'other-config', encoding='utf-8')
        (target / 'usr/sbin').mkdir(parents=True)
        (target / 'usr/sbin/update-grub').write_text('x', encoding='utf-8')
        (target / 'boot/grub').mkdir(parents=True)
        (target / 'boot/grub/grub.cfg').write_text('menuentry test {}\n', encoding='utf-8')

        with patch('native_deploy.get_live_source_mount', return_value=str(source)), \
             patch('native_deploy._chroot'), \
             patch('native_deploy._install_native_efi_boot_entry') as boot_entry:
            native_deploy._install_grub_native(
                str(target), '/dev/sda', True, lambda *_: None, lambda _msg: None, False,
                esp_device='/dev/sda1', reuse_esp=True,
            )

        assert (target / 'boot/efi/EFI/Microsoft/Boot/bootmgfw.efi').read_bytes() == b'windows-loader'
        assert (target / 'boot/efi/EFI/OEM/preserve.txt').read_text(encoding='utf-8') == 'keep'
        assert windows_fallback.read_bytes() == b'windows-fallback'
        assert (target / 'boot/efi/EFI/OtherLinux/grub.cfg').read_text(encoding='utf-8') == 'other-config'
        assert (target / 'boot/efi/EFI/minios/bootx64.efi').read_bytes() == b'minios-loader'
        assert not (target / 'boot/efi/EFI/minios/x86_64-efi').exists()
        assert not (target / 'boot/efi/EFI/debian/x86_64-efi').exists()
        assert 'configfile $prefix/grub.cfg' in (
            target / 'boot/efi/EFI/minios/grub.cfg').read_text(encoding='utf-8')
        assert 'insmod ext2' in (
            target / 'boot/efi/EFI/minios/grub.cfg').read_text(encoding='utf-8')
        assert 'configfile $prefix/grub.cfg' in (
            target / 'boot/efi/EFI/debian/grub.cfg').read_text(encoding='utf-8')
        boot_entry.assert_called_once()

    def test_native_uefi_empty_existing_esp_is_still_reused_and_registered(self, tmp_path):
        import native_deploy

        source = tmp_path / 'source'
        boot = source / 'EFI/boot'
        boot.mkdir(parents=True)
        (boot / 'bootx64.efi').write_bytes(b'x64-shim')
        (boot / 'bootia32.efi').write_bytes(b'ia32-shim')
        (boot / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (boot / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        target = tmp_path / 'target'
        (target / 'boot/efi').mkdir(parents=True)
        (target / 'usr/sbin').mkdir(parents=True)
        (target / 'usr/sbin/update-grub').write_text('x', encoding='utf-8')
        (target / 'boot/grub').mkdir(parents=True)
        (target / 'boot/grub/grub.cfg').write_text('menuentry test {}\n', encoding='utf-8')

        with patch('native_deploy.get_live_source_mount', return_value=str(source)), \
             patch('native_deploy._chroot'), \
             patch('native_deploy._install_native_efi_boot_entry') as boot_entry:
            native_deploy._install_grub_native(
                str(target), '/dev/sda', True, lambda *_: None, lambda _msg: None, False,
                esp_device='/dev/sda1', reuse_esp=True,
            )

        boot_entry.assert_called_once()
        assert (target / 'boot/efi/EFI/minios/bootx64.efi').read_bytes() == b'x64-shim'
        assert not (target / 'boot/efi/EFI/boot/bootx64.efi').exists()

    def test_native_uefi_empty_reused_esp_rolls_back_created_efi_directory(self, tmp_path):
        import native_deploy
        import pytest

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        (source / 'bootx64.efi').write_bytes(b'x64-shim')
        (source / 'bootia32.efi').write_bytes(b'ia32-shim')
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        target = tmp_path / 'target/boot/efi/EFI'
        target.parent.mkdir(parents=True)

        def fail_registration():
            raise RuntimeError('registration failed')

        with pytest.raises(RuntimeError, match='registration failed'):
            native_deploy._publish_native_efi(
                str(source.parent), str(target), 'configfile $prefix/grub.cfg\n',
                fail_registration, reuse_esp=True,
            )

        assert not target.exists()

    def test_native_uefi_publication_restores_original_on_replace_failure(self, tmp_path):
        import native_deploy
        import pytest

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        (source / 'bootx64.efi').write_bytes(b'minios-loader')
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        target = tmp_path / 'target/EFI'
        (target / 'Microsoft/Boot').mkdir(parents=True)
        original = target / 'Microsoft/Boot/bootmgfw.efi'
        original.write_bytes(b'windows-loader')
        (target / 'minios').mkdir()
        original_minios = target / 'minios/grub.cfg'
        original_minios.write_text('old-minios', encoding='utf-8')
        real_replace = os.replace
        calls = []

        def fail_publication(source_path, target_path):
            calls.append((source_path, target_path))
            if len(calls) == 2:
                raise OSError('injected EFI publication failure')
            return real_replace(source_path, target_path)

        with patch('native_deploy.os.replace', side_effect=fail_publication):
            with pytest.raises(OSError, match='injected EFI publication failure'):
                native_deploy._publish_native_efi(
                    str(source.parent), str(target), 'configfile $prefix/grub.cfg\n', lambda: None,
                    reuse_esp=True,
                )

        assert original.read_bytes() == b'windows-loader'
        assert original_minios.read_text(encoding='utf-8') == 'old-minios'
        assert len(calls) == 3

    def test_native_uefi_publication_keeps_backup_when_restore_fails(self, tmp_path):
        import native_deploy
        import pytest

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        (source / 'bootx64.efi').write_bytes(b'minios-loader')
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        target = tmp_path / 'target/EFI'
        (target / 'minios').mkdir(parents=True)
        (target / 'minios/grub.cfg').write_text('old-minios', encoding='utf-8')
        staged = tmp_path / 'staged'
        staged.mkdir()
        real_replace = os.replace
        calls = []

        def fail_publication_and_restore(source_path, target_path):
            calls.append((source_path, target_path))
            if len(calls) in (2, 3):
                raise OSError('injected replace failure')
            return real_replace(source_path, target_path)

        with patch('native_deploy.tempfile.mkdtemp', return_value=str(staged)), \
             patch('native_deploy.os.replace', side_effect=fail_publication_and_restore):
            with pytest.raises(RuntimeError, match='original EFI tree remains'):
                native_deploy._publish_native_efi(
                    str(source.parent), str(target), 'configfile $prefix/grub.cfg\n', lambda: None,
                    reuse_esp=True,
                )

        assert (staged / 'EFI.original/minios/grub.cfg').read_text(encoding='utf-8') == 'old-minios'

    def test_native_uefi_publication_rejects_foreign_vendor_config(self, tmp_path):
        import native_deploy
        import pytest

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        (source / 'bootx64.efi').write_bytes(b'minios-loader')
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        target = tmp_path / 'target/EFI'
        (target / 'debian').mkdir(parents=True)
        foreign = target / 'debian/grub.cfg'
        foreign.write_text('foreign-config', encoding='utf-8')

        with pytest.raises(RuntimeError, match='conflicting debian'):
            native_deploy._publish_native_efi(
                str(source.parent), str(target), 'configfile $prefix/grub.cfg\n', lambda: None,
                reuse_esp=True,
            )

        assert foreign.read_text(encoding='utf-8') == 'foreign-config'
        assert not (target / 'minios').exists()

    def test_native_uefi_publication_rejects_source_symlink(self, tmp_path):
        import native_deploy
        import pytest

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        outside = tmp_path / 'outside'
        outside.write_text('outside', encoding='utf-8')
        os.symlink(str(outside), str(source / 'bootx64.efi'))
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        target = tmp_path / 'target/EFI'

        with pytest.raises(RuntimeError, match='unsafe file'):
            native_deploy._publish_native_efi(
                str(source.parent), str(target), 'configfile $prefix/grub.cfg\n'
            )

        assert outside.read_text(encoding='utf-8') == 'outside'

    def test_native_uefi_boot_entry_uses_target_partition_and_minios_loader(self):
        import native_deploy

        before = MagicMock(returncode=0, stdout='Boot0001* Windows Boot Manager\n')
        created = MagicMock(returncode=0, stdout='')
        after = MagicMock(
            returncode=0,
            stdout=(
                'BootOrder: 0002,0001\n'
                'Boot0001* Windows Boot Manager\n'
                'Boot0002* MiniOS HD(1,GPT,esp-partuuid,0x800,0x1000)/File(\\EFI\\minios\\bootx64.efi)\n'
            ),
        )
        with patch('native_deploy._chroot_capture', side_effect=[before, created, after]) as capture, \
             patch('native_deploy._efi_loader_name', return_value='bootx64.efi'), \
             patch('native_deploy.subprocess.check_output', return_value='esp-partuuid\n'), \
             patch('native_deploy.subprocess.run', return_value=MagicMock(
                  returncode=0, stdout='sda 1\n')):
            native_deploy._install_native_efi_boot_entry(
                '/target', '/dev/sda', '/dev/sda1', lambda _message: None
            )

        assert capture.call_args_list[1][0][1] == [
            'efibootmgr', '--create', '--disk', '/dev/sda', '--part', '1',
            '--label', 'MiniOS', '--loader', '\\EFI\\minios\\bootx64.efi',
        ]

    def test_native_uefi_boot_entry_rejects_unrelated_new_entry_without_deleting_it(self):
        import native_deploy
        import pytest

        before = MagicMock(returncode=0, stdout='BootOrder: 0001\nBoot0001* Windows Boot Manager\n')
        created = MagicMock(returncode=0, stdout='Boot0007* OtherOS\n')
        after = MagicMock(
            returncode=0,
            stdout=(
                'BootOrder: 0007,0001\n'
                'Boot0001* Windows Boot Manager\n'
                'Boot0007* OtherOS HD(1,GPT,other-partuuid,0x800,0x1000)/File(\\EFI\\other\\wrong.efi)\n'
            ),
        )
        with patch('native_deploy._chroot_capture', side_effect=[before, created, after]), \
             patch('native_deploy._efi_loader_name', return_value='bootx64.efi'), \
             patch('native_deploy.subprocess.check_output', return_value='esp-partuuid\n'), \
             patch('native_deploy.subprocess.run', return_value=MagicMock(returncode=0, stdout='sda 1\n')), \
             patch('native_deploy._chroot') as cleanup:
            with pytest.raises(RuntimeError, match='could not be verified'):
                native_deploy._install_native_efi_boot_entry(
                    '/target', '/dev/sda', '/dev/sda1', lambda _message: None
                )
        assert not cleanup.called

    def test_reused_uefi_preflight_requires_writable_firmware_variables(self):
        import native_deploy
        import pytest

        plan = MagicMock(use_efi=True, reuse_esp=True)
        with patch('native_deploy.os.path.isdir', return_value=False):
            with pytest.raises(RuntimeError, match='writable UEFI firmware variables'):
                native_deploy._preflight_reused_efi_variables(plan)

    def test_reused_uefi_preflight_rejects_unmounted_efivars_directory(self):
        import native_deploy
        import pytest

        plan = MagicMock(use_efi=True, reuse_esp=True)
        with patch('native_deploy.os.path.isdir', return_value=True), \
             patch('native_deploy.os.path.ismount', return_value=False), \
             patch('native_deploy.os.access', return_value=True):
            with pytest.raises(RuntimeError, match='writable UEFI firmware variables'):
                native_deploy._preflight_reused_efi_variables(plan)

    def test_reused_uefi_payload_preflight_rejects_vendor_conflict(self, tmp_path):
        import native_deploy
        import pytest
        import subprocess

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        mounted = tmp_path / 'mounted'
        (mounted / 'EFI/debian').mkdir(parents=True)
        (mounted / 'EFI/debian/grub.cfg').write_text(
            'foreign-config', encoding='utf-8')
        plan = MagicMock(use_efi=True, reuse_esp=True, esp_path='/dev/sda1')

        with patch('native_deploy.get_live_source_mount', return_value=str(source.parent.parent)), \
             patch('native_deploy.tempfile.mkdtemp', return_value=str(mounted)), \
             patch('native_deploy.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
             patch('native_deploy.shutil.rmtree'):
            with pytest.raises(RuntimeError, match='conflicting debian'):
                native_deploy._preflight_reused_efi_payload(plan)

    def test_reused_uefi_payload_preflight_rejects_transaction_space_shortage(self, tmp_path):
        import native_deploy
        import pytest
        import subprocess

        source = tmp_path / 'source/EFI/boot'
        source.mkdir(parents=True)
        (source / 'grubx64.efi').write_bytes(b'prefix=/EFI/debian')
        (source / 'grubia32.efi').write_bytes(b'prefix=/EFI/debian')
        mounted = tmp_path / 'mounted'
        (mounted / 'EFI').mkdir(parents=True)
        filesystem = MagicMock(f_bavail=0, f_frsize=4096)
        with patch('native_deploy.get_live_source_mount', return_value=str(source.parent.parent)), \
             patch('native_deploy.tempfile.mkdtemp', return_value=str(mounted)), \
             patch('native_deploy.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
             patch('native_deploy.os.statvfs', return_value=filesystem), \
             patch('native_deploy.shutil.rmtree'):
            with pytest.raises(RuntimeError, match='enough free space'):
                native_deploy._preflight_reused_efi_payload_path('/dev/sda1')

    def test_efi_payload_contract_accepts_trixie_x64_and_bookworm_ia32(self, tmp_path):
        import native_deploy

        source = tmp_path / 'source'
        boot = source / 'EFI/boot'
        boot.mkdir(parents=True)
        for name in ('bootx64.efi', 'grubx64.efi', 'bootia32.efi', 'grubia32.efi'):
            (boot / name).write_bytes(name.encode('ascii'))
        write_efi_manifest(source)

        with patch('native_deploy.get_live_source_mount', return_value=str(source)):
            native_deploy._preflight_efi_payload_contract(True)

    def test_efi_payload_contract_rejects_non_bookworm_ia32(self, tmp_path):
        import native_deploy

        source = tmp_path / 'source'
        boot = source / 'EFI/boot'
        boot.mkdir(parents=True)
        for name in ('bootx64.efi', 'grubx64.efi', 'bootia32.efi', 'grubia32.efi'):
            (boot / name).write_bytes(name.encode('ascii'))
        write_efi_manifest(source)
        manifest_path = source / 'minios/boot/efi-manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['architectures']['ia32']['suite'] = 'trixie'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

        with patch('native_deploy.get_live_source_mount', return_value=str(source)):
            try:
                native_deploy._preflight_efi_payload_contract(True)
                assert False, 'expected IA32 contract rejection'
            except RuntimeError as exc:
                assert 'Bookworm GRUB 2.06' in str(exc)

    def test_native_grub_installs_persistent_206_compatibility_policy(self, tmp_path):
        import native_deploy

        target = tmp_path / 'target'
        script = target / 'etc/grub.d/30_uefi-firmware'
        script.parent.mkdir(parents=True)
        script.write_text('fwsetup --is-supported\n', encoding='utf-8')
        calls = []

        with patch('native_deploy._chroot', side_effect=lambda _target, args, _log, **_kwargs: calls.append(args)):
            native_deploy._install_grub_206_compatibility(str(target), lambda _message: None)

        assert calls == [[
            'dpkg-divert', '--quiet', '--local', '--rename', '--add', '--divert',
            '/usr/lib/minios-grub-compat/30_uefi-firmware.distrib',
            '/etc/grub.d/30_uefi-firmware',
        ]]
        assert script.read_text(encoding='utf-8').endswith('exit 0\n')
        assert os.stat(str(script)).st_mode & 0o111

    def test_native_grub_rejects_212_firmware_probe(self, tmp_path):
        import native_deploy

        config = tmp_path / 'grub.cfg'
        config.write_text('fwsetup --is-supported\n', encoding='utf-8')
        try:
            native_deploy._validate_grub_206_compatibility(str(config))
            assert False, 'expected GRUB 2.12-only command rejection'
        except RuntimeError as exc:
            assert 'GRUB 2.12' in str(exc)

    def test_native_package_install_preseeds_grub_pc(self, tmp_path):
        import native_deploy
        from install_state import InstallState

        state = InstallState(target_device='/dev/sda', download_missing_packages=True)
        calls = []

        with patch('native_deploy.native_missing_packages', return_value=['grub-pc', 'grub-common']), \
             patch('native_deploy.package_installed_or_provided', return_value=True), \
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

    def test_offline_boot_fallback_still_installs_mandatory_initramfs_tool(self, tmp_path):
        import native_deploy
        from install_state import InstallState

        cache = tmp_path / 'cache'
        (cache / 'archives').mkdir(parents=True)
        state = InstallState(package_cache_path=str(cache))
        calls = []
        logs = []

        with patch('native_deploy.native_missing_packages', return_value=[
                'grub-pc', 'grub-common', 'initramfs-tools']), \
             patch('native_deploy.package_installed_or_provided', return_value=False), \
             patch('native_deploy._chroot', side_effect=lambda *args, **kwargs: calls.append((args, kwargs))):
            native_deploy._install_native_packages(
                str(tmp_path), False, 'ext4', state, logs.append)

        assert any('grub-pc, grub-common' in line for line in logs)
        assert calls[-1][0][1][-6:] == [
            'apt-get', '--no-download', 'install', '-y', '--no-install-recommends',
            'initramfs-tools',
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
        assert run.call_args[0][0][-2] == password_hash

    def test_chroot_mounts_and_unmounts_dev_pts_and_efivars(self, tmp_path):
        import native_deploy

        commands = []
        with patch('native_deploy._run', side_effect=lambda command, *_args, **_kwargs: commands.append(command)), \
             patch('native_deploy.os.path.ismount', return_value=True):
            native_deploy._mount_chroot_api(str(tmp_path), None, False, lambda _message: None)

        assert ['mount', '--bind', '/dev/pts', str(tmp_path / 'dev' / 'pts')] in commands
        assert [
            'mount', '--bind', '/sys/firmware/efi/efivars',
            str(tmp_path / 'sys' / 'firmware' / 'efi' / 'efivars'),
        ] in commands

        with patch('native_deploy.subprocess.run') as run:
            native_deploy._unmount_chroot_api(str(tmp_path), None, lambda _message: None)

        unmounts = [mock_call[0][0] for mock_call in run.call_args_list]
        assert unmounts[0] == [
            'umount', str(tmp_path / 'sys' / 'firmware' / 'efi' / 'efivars')]
        assert ['umount', str(tmp_path / 'dev' / 'pts')] in unmounts

    def test_chroot_mount_failure_unwinds_prior_mounts(self, tmp_path):
        import native_deploy
        import pytest

        calls = []

        def mount(command, *_args, **_kwargs):
            calls.append(command)
            if command[3].endswith('/sys'):
                raise RuntimeError('injected mount failure')

        with patch('native_deploy._run', side_effect=mount), \
             patch('native_deploy.subprocess.run') as run:
            with pytest.raises(RuntimeError, match='injected mount failure'):
                native_deploy._mount_chroot_api(
                    str(tmp_path), None, False, lambda _message: None)

        unmounts = [mock_call[0][0] for mock_call in run.call_args_list]
        assert unmounts == [
            ['umount', str(tmp_path / 'proc')],
            ['umount', str(tmp_path / 'dev')],
        ]
