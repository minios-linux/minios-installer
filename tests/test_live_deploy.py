#!/usr/bin/env python3

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


class TestLiveDeploySafety:
    def test_runtime_luks_option_requires_initrd_marker(self):
        from live_deploy import runtime_supports_luks_persistence

        with patch('live_deploy.os.path.isfile', return_value=False):
            assert runtime_supports_luks_persistence() is False
        with patch('live_deploy.os.path.isfile', return_value=True):
            assert runtime_supports_luks_persistence() is True

    def test_luks_support_inspects_every_source_initrd(self, tmp_path):
        from live_deploy import source_supports_luks_persistence

        boot = tmp_path / 'boot'
        boot.mkdir()
        (boot / 'initrfs-a.img').write_bytes(b'first')
        (boot / 'initrd-b.img').write_bytes(b'second')
        result = MagicMock(returncode=0, stdout='etc/minios-initramfs-crypt\n')
        with patch('live_deploy.shutil.which', return_value='/usr/bin/lsinitramfs'), \
             patch('live_deploy.subprocess.run', return_value=result) as run:
            assert source_supports_luks_persistence(str(tmp_path)) is True

        assert run.call_count == 2
        assert {mock_call[0][0][1] for mock_call in run.call_args_list} == {
            str(boot / 'initrfs-a.img'), str(boot / 'initrd-b.img'),
        }

    def test_luks_source_without_crypto_hook_is_rejected(self, tmp_path):
        from live_deploy import source_supports_luks_persistence

        boot = tmp_path / 'boot'
        boot.mkdir()
        (boot / 'initrfs.img').write_bytes(b'initrd')
        result = MagicMock(returncode=0, stdout='etc/other-hook\n')
        with patch('live_deploy.shutil.which', return_value='/usr/bin/lsinitramfs'), \
             patch('live_deploy.subprocess.run', return_value=result):
            assert source_supports_luks_persistence(str(tmp_path)) is False

    def test_luks_source_resolves_generic_initrd_symlink_once(self, tmp_path):
        from live_deploy import source_supports_luks_persistence

        boot = tmp_path / 'boot'
        boot.mkdir()
        versioned = boot / 'initrfs-6.12.img'
        versioned.write_bytes(b'initrd')
        (boot / 'initrfs.img').symlink_to(versioned.name)
        result = MagicMock(returncode=0, stdout='etc/minios-initramfs-crypt\n')
        with patch('live_deploy.shutil.which', return_value='/usr/bin/lsinitrd'), \
             patch('live_deploy.subprocess.run', return_value=result) as run:
            assert source_supports_luks_persistence(str(tmp_path)) is True

        run.assert_called_once()
        assert run.call_args[0][0][1] == str(versioned)

    def test_luks_boot_options_require_crypto_initrd_without_a_passphrase(self):
        from install_state import InstallState
        from live_deploy import _persistence_boot_options

        state = InstallState(
            install_mode='live', persistence_mode='luks', persistence_size_mib=2048,
        )
        with patch('live_deploy.source_supports_luks_persistence', return_value=True):
            assert _persistence_boot_options(state, '/media/minios') == ('perchmode=luks', 'perchsize=2048')
        with patch('live_deploy.source_supports_luks_persistence', return_value=False):
            try:
                _persistence_boot_options(state, '/media/minios')
                assert False, 'expected cryptsetup capability failure'
            except RuntimeError as exc:
                assert 'Encrypted session storage is not supported' in str(exc)

    def test_non_encrypted_persistence_boot_options_do_not_require_crypto(self):
        from install_state import InstallState
        from live_deploy import _persistence_boot_options

        native = InstallState(install_mode='live', persistence_mode='native')
        dyn = InstallState(install_mode='live', persistence_mode='dynfilefs', persistence_size_mib=8000)
        raw = InstallState(install_mode='live', persistence_mode='raw', persistence_size_mib=4000)

        assert _persistence_boot_options(native, '/media/minios') == ('perchmode=native',)
        assert _persistence_boot_options(dyn, '/media/minios') == ('perchmode=dynfilefs', 'perchsize=8000')
        assert _persistence_boot_options(raw, '/media/minios') == ('perchmode=raw', 'perchsize=4000')

    def test_luks_source_validation_happens_before_partitioning(self):
        from install_state import InstallState
        from live_deploy import run_live_install

        state = InstallState(
            install_mode='live', target_device='/dev/sdb',
            persistence_mode='luks', persistence_size_mib=4000,
        )
        with patch('live_deploy.resolve_install_device', return_value='/dev/sdb'), \
             patch('live_deploy.find_minios_source', return_value='/media/minios'), \
             patch('live_deploy.source_supports_luks_persistence', return_value=False), \
             patch('live_deploy.execute_plan') as execute:
            try:
                run_live_install(state, lambda *_: None, lambda *_: None)
                assert False, 'expected source initrd capability failure'
            except RuntimeError as exc:
                assert 'Encrypted session storage is not supported' in str(exc)
        assert not execute.called

    def test_run_live_install_cleans_generated_config(self, tmp_path):
        from install_state import InstallState, UserConfig
        from live_deploy import run_live_install
        from partition_models import PartitionPlan

        generated = tmp_path / "generated.conf"
        generated.write_text("LIVE_USER_PASSWORD='secret'\n", encoding="utf-8")

        stale_plan = PartitionPlan(device="/dev/sdb", use_gpt=False, wipe_disk=True)
        fresh_plan = PartitionPlan(device="/dev/sdb", use_gpt=False, wipe_disk=True)
        state = InstallState(
            install_mode="live",
            placement="erase_all",
            target_device="sdb",
            filesystem="ext4",
            partition_plan=stale_plan,
            user_config=UserConfig(username="alice", password="secret"),
            user_config_customized=True,
        )

        with patch("live_deploy.resolve_install_device", return_value="/dev/sdb"), \
             patch("live_deploy.scan_disk", return_value=MagicMock()) as scan, \
             patch("live_deploy.build_plan", return_value=fresh_plan) as build, \
             patch("live_deploy.execute_plan", return_value=("/dev/sdb1", None, "/mnt/root", None)) as exec_plan, \
             patch("live_deploy.find_minios_source", return_value="/media/minios"), \
             patch("live_deploy.write_live_config", return_value=str(generated)) as writer, \
             patch("live_deploy.copy_minios_files"), \
             patch("live_deploy.copy_efi_files"), \
             patch("live_deploy.install_bootloader") as boot, \
             patch("live_deploy.unmount_partitions") as unmount:
            run_live_install(state, lambda *_: None, lambda *_: None)

        assert not generated.exists()
        assert state.target_device == "/dev/sdb"
        # Preview plan is ignored; install always rebuilds from current state.
        build.assert_called_once()
        scan.assert_called_once_with("/dev/sdb")
        assert state.partition_plan is fresh_plan
        # MBR plan must install BIOS bootloader; success unmount is not only in finally.
        boot.assert_called_once()
        unmount.assert_called()
        writer_kwargs = writer.call_args[1] if writer.call_args else {}
        assert "LIVE_SECURITY_PROFILE" not in writer_kwargs["extra_entries"]
        assert writer_kwargs["extra_entries"]["LIVE_SUDO_MODE"] == "passwordless"
        # Tuple access works with the Mock implementation shipped on Bionic.
        call_kwargs = exec_plan.call_args[1] if exec_plan.call_args else {}
        assert call_kwargs.get("cancel_cb") is not None

    def test_live_bios_esp_payload_is_preflighted_before_partitioning(self):
        from install_state import InstallState
        from live_deploy import run_live_install
        from partition_models import PartitionPlan, PlannedPartition

        plan = PartitionPlan(device='/dev/sdb', use_gpt=False, wipe_disk=True, use_efi=False)
        plan.partitions.append(PlannedPartition(
            'create', 'esp', 100, 200, 'fat32', path='/dev/sdb2', mountpoint='/boot/efi'))
        state = InstallState(
            install_mode='live', placement='erase_all', target_device='/dev/sdb', filesystem='ext4')

        with patch('live_deploy.resolve_install_device', return_value='/dev/sdb'), \
             patch('live_deploy.find_minios_source', return_value='/media/minios'), \
             patch('live_deploy.scan_disk', return_value=MagicMock()), \
             patch('live_deploy.build_plan', return_value=plan), \
             patch('live_deploy.efi_payload_bytes', side_effect=ValueError('EFI payload too large')) as measure, \
             patch('live_deploy.execute_plan') as execute:
            try:
                run_live_install(state, lambda *_: None, lambda *_: None)
                assert False, 'expected EFI payload preflight failure'
            except ValueError as exc:
                assert 'EFI payload' in str(exc)

        measure.assert_called_once_with('/media/minios')
        assert not execute.called

    def test_run_live_install_refuses_live_disk(self):
        from install_state import InstallState
        from live_deploy import run_live_install

        state = InstallState(target_device="/dev/sdb")
        with patch(
            "live_deploy.resolve_install_device",
            side_effect=RuntimeError("Refusing to install to the running live media device: /dev/sdb"),
        ):
            try:
                run_live_install(state, lambda *_: None, lambda *_: None)
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "live media" in str(exc)
