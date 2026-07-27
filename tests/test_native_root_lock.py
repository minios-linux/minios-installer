# -*- coding: utf-8 -*-
from unittest.mock import patch

import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from install_state import InstallState, UserConfig  # noqa: E402
from native_deploy import _apply_native_settings, _cleanup_native_live_packages, _collect_live_allowuser_groups, _generate_ssh_host_keys, _generate_ssl_snakeoil_cert, _target_service_action  # noqa: E402


def _run_apply(user_config):
    calls = []

    def fake_chroot(target, args, log_cb, dry_run=False, check=True, input_text=None):
        calls.append(list(args))

    state = InstallState(install_mode="native")
    state.user_config = user_config
    logs = []
    with patch("native_deploy._chroot", side_effect=fake_chroot), patch(
        "native_deploy.hash_system_password", return_value="$6$hash"
    ), patch("native_deploy._write_text"), patch("native_deploy._replace_hosts_hostname"), patch(
        "native_deploy._target_user_exists", return_value=False
    ), patch("native_deploy._target_groups", return_value={"sudo", "audio", "video"}), patch(
        "native_deploy._target_service_action"
    ) as svc:
        _apply_native_settings("/target", state, logs.append, dry_run=False)
    return calls, logs, svc


def test_native_locks_root_when_no_root_password():
    calls, logs, _svc = _run_apply(UserConfig(username="alice", password="secret", hostname="pc"))
    joined = [" ".join(c) for c in calls]
    assert any(j == "useradd -m -s /bin/bash -G sudo,audio,video alice" for j in joined), joined
    assert not any(j.startswith("usermod -l ") for j in joined), joined
    assert any(j == "usermod -L root" for j in joined), joined
    assert any(j == "usermod -p * root" for j in joined), joined
    assert any("Locking root" in line for line in logs)


def test_native_sets_root_password_when_provided():
    calls, logs, _svc = _run_apply(
        UserConfig(username="alice", password="secret", root_password="toor", hostname="pc")
    )
    joined = [" ".join(c) for c in calls]
    assert not any("-L" in j and "root" in j for j in joined), joined
    assert any(j == "usermod -p $6$hash root" for j in joined), joined


def test_native_uses_existing_selected_user_without_renaming_live():
    calls = []

    def fake_chroot(target, args, log_cb, dry_run=False, check=True, input_text=None):
        calls.append(list(args))

    state = InstallState(install_mode="native")
    state.user_config = UserConfig(username="alice", password="secret")
    logs = []
    with patch("native_deploy._chroot", side_effect=fake_chroot), patch(
        "native_deploy.hash_system_password", return_value="$6$hash"
    ), patch("native_deploy._target_user_exists", return_value=True):
        _apply_native_settings("/target", state, logs.append, dry_run=False)
    joined = [" ".join(c) for c in calls]
    assert not any(j.startswith("useradd ") for j in joined), joined
    assert not any(j.startswith("usermod -l ") for j in joined), joined
    assert any("Using existing user account: alice" in line for line in logs)


def test_native_prefers_user_setup_for_user_creation():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/lib/user-setup/user-setup-apply"))
        _write(os.path.join(target, "etc/passwd"), "root:x:0:0:root:/root:/bin/bash\n")
        _write(os.path.join(target, "etc/group"), "sudo:x:27:\naudio:x:29:\ndocker:x:999:\n")
        calls = []

        def fake_chroot_returncode(target_path, args, log_cb, dry_run=False, input_text=None):
            calls.append((list(args), input_text))
            return 0

        state = InstallState(install_mode="native", security_profile="strict")
        state.user_config = UserConfig(username="alice", full_name="Alice", user_default_groups="sudo,docker,audio")
        with patch("native_deploy._chroot_returncode", side_effect=fake_chroot_returncode), patch("native_deploy._chroot") as chroot:
            _apply_native_settings(target, state, lambda line: None, dry_run=False)

        assert calls[0][0] == ["debconf-set-selections"]
        assert "passwd/username string alice" in calls[0][1]
        assert "passwd/user-fullname string Alice" in calls[0][1]
        assert "passwd/user-default-groups string sudo audio" in calls[0][1]
        assert calls[1][0] == ["/usr/lib/user-setup/user-setup-apply"]
        joined = [" ".join(call[0][1]) for call in chroot.call_args_list]
        assert not any(j.startswith("useradd ") for j in joined), joined


def test_native_falls_back_to_useradd_when_user_setup_fails():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/lib/user-setup/user-setup-apply"))
        _write(os.path.join(target, "etc/passwd"), "root:x:0:0:root:/root:/bin/bash\n")
        _write(os.path.join(target, "etc/group"), "sudo:x:27:\n")
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        state = InstallState(install_mode="native")
        state.user_config = UserConfig(username="alice")
        with patch("native_deploy._chroot_returncode", return_value=1), patch("native_deploy._chroot", side_effect=fake_chroot):
            _apply_native_settings(target, state, lambda line: None, dry_run=False)

        joined = [" ".join(c) for c in calls]
        assert any(j == "useradd -m -s /bin/bash -G sudo alice" for j in joined), joined


def test_native_uses_user_created_by_partial_user_setup_failure():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/lib/user-setup/user-setup-apply"))
        passwd_path = os.path.join(target, "etc/passwd")
        _write(passwd_path, "root:x:0:0:root:/root:/bin/bash\n")
        _write(os.path.join(target, "etc/group"), "sudo:x:27:\n")
        calls = []

        def fake_chroot_returncode(target_path, args, log_cb, dry_run=False, input_text=None):
            calls.append(list(args))
            if args == ["/usr/lib/user-setup/user-setup-apply"]:
                with open(passwd_path, "a", encoding="utf-8") as fh:
                    fh.write("alice:x:1000:1000:Alice:/home/alice:/bin/bash\n")
                return 1
            return 0

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        logs = []
        state = InstallState(install_mode="native")
        state.user_config = UserConfig(username="alice")
        with patch("native_deploy._chroot_returncode", side_effect=fake_chroot_returncode), patch("native_deploy._chroot", side_effect=fake_chroot):
            _apply_native_settings(target, state, logs.append, dry_run=False)

        joined = [" ".join(c) for c in calls]
        assert not any(j.startswith("useradd ") for j in joined), joined
        assert any("reported failure after creating alice" in line for line in logs)


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_native_locale_is_generated_and_persisted_for_target():
    import native_deploy

    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "etc/locale.gen"), "# ru_RU.UTF-8 UTF-8\n# en_US.UTF-8 UTF-8\n")
        _write(os.path.join(target, "usr/sbin/locale-gen"))
        _write(os.path.join(target, "usr/sbin/update-locale"))
        calls = []

        def fake_chroot_returncode(target_path, args, log_cb, dry_run=False, input_text=None):
            calls.append(list(args))
            return 0

        with patch("native_deploy._chroot_returncode", side_effect=fake_chroot_returncode):
            native_deploy._apply_native_locale(target, "ru_RU.UTF-8,en_US.UTF-8", lambda line: None, dry_run=False)

        with open(os.path.join(target, "etc/default/locale"), "r", encoding="utf-8") as fh:
            assert fh.read() == "LANG=ru_RU.UTF-8\nLANGUAGE=ru:en\n"
        with open(os.path.join(target, "etc/locale.gen"), "r", encoding="utf-8") as fh:
            locale_gen = fh.read()
        assert "ru_RU.UTF-8 UTF-8\n" in locale_gen
        assert "en_US.UTF-8 UTF-8\n" in locale_gen
        assert ["locale-gen"] in calls
        assert ["update-locale", "LANG=ru_RU.UTF-8", "LANGUAGE=ru:en"] in calls


def test_native_requested_locale_generation_failure_is_fatal():
    import native_deploy
    import pytest

    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/sbin/locale-gen"))
        with patch("native_deploy._chroot_returncode", return_value=1):
            with pytest.raises(RuntimeError, match="Locale generation failed"):
                native_deploy._apply_native_locale(target, "ru_RU.UTF-8", lambda _line: None)


def test_native_cleanup_purges_live_packages_and_artifacts():
    with tempfile.TemporaryDirectory() as target:
        _write(
            os.path.join(target, "var/lib/dpkg/status"),
            "Package: minios-live-config\nStatus: install ok installed\n\n"
            "Package: user-setup\nStatus: install ok installed\n\n"
            "Package: minios-tools\nStatus: install ok installed\n\n"
            "Package: minios-dracut\nStatus: install ok installed\n\n"
            "Package: minios-installer\nStatus: install ok installed\n\n"
            "Package: minios-configurator\nStatus: install ok installed\n\n"
            "Package: minios-kernel-manager\nStatus: install ok installed\n\n"
            "Package: minios-session-manager\nStatus: install ok installed\n\n"
            "Package: minios-welcome\nStatus: install ok installed\n\n"
            "Package: minios-store-gui\nStatus: install ok installed\n\n"
            "Package: minios-store\nStatus: install ok installed\n\n"
            "Package: minios-store-common\nStatus: install ok installed\n\n",
        )
        _write(os.path.join(target, "etc/systemd/system/basic.target.wants/live-config.service"))
        _write(os.path.join(target, "usr/bin/apt-get"))
        _write(os.path.join(target, "usr/bin/audio-allowuser.sh"))
        _write(os.path.join(target, "usr/bin/start-xorg.sh"))
        _write(os.path.join(target, "usr/bin/stop-xorg.sh"))
        _write(os.path.join(target, "usr/lib/systemd/system/xorg.service"))
        _write(os.path.join(target, "lib/systemd/system/lightdm.service"))
        os.makedirs(os.path.join(target, "etc/systemd/system"), exist_ok=True)
        os.symlink("/usr/lib/systemd/system/xorg.service", os.path.join(target, "etc/systemd/system/display-manager.service"))
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, input_text=None):
            calls.append(list(args))
            return 0

        logs = []
        with patch("native_deploy._chroot_returncode", side_effect=fake_chroot):
            _cleanup_native_live_packages(target, logs.append, dry_run=False)

        joined = [" ".join(c) for c in calls]
        purge = next(j for j in joined if "apt-get purge -y --allow-remove-essential" in j)
        for package in (
            "minios-live-config", "user-setup", "minios-configurator",
            "minios-installer", "minios-kernel-manager", "minios-session-manager",
            "minios-store-gui", "minios-welcome",
        ):
            assert package in purge
        for package in ("minios-store ", "minios-store-common", "minios-tools", "minios-dracut"):
            assert package not in purge
        assert any("apt-get autoremove --purge -y" in j for j in joined), joined
        assert not os.path.exists(os.path.join(target, "etc/systemd/system/basic.target.wants/live-config.service"))
        assert not os.path.exists(os.path.join(target, "usr/bin/audio-allowuser.sh"))
        assert os.path.exists(os.path.join(target, "usr/bin/start-xorg.sh"))
        assert os.path.exists(os.path.join(target, "usr/bin/stop-xorg.sh"))
        assert os.path.exists(os.path.join(target, "usr/lib/systemd/system/xorg.service"))
        assert os.readlink(os.path.join(target, "etc/systemd/system/display-manager.service")) == "/usr/lib/systemd/system/xorg.service"
        assert any("Removing live-only packages" in line for line in logs)


def test_native_cleanup_continues_when_live_package_purge_fails():
    with tempfile.TemporaryDirectory() as target:
        _write(
            os.path.join(target, "var/lib/dpkg/status"),
            "Package: minios-live-config\nStatus: install ok installed\n\n"
            "Package: user-setup\nStatus: install ok installed\n\n",
        )
        _write(os.path.join(target, "usr/bin/audio-allowuser.sh"))
        _write(os.path.join(target, "usr/bin/apt-get"))
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, input_text=None):
            calls.append(list(args))
            return 1

        logs = []
        with patch("native_deploy._chroot_returncode", side_effect=fake_chroot):
            _cleanup_native_live_packages(target, logs.append, dry_run=False)

        joined = [" ".join(c) for c in calls]
        assert any("apt-get purge -y --allow-remove-essential minios-live-config user-setup" in j for j in joined), joined
        assert any("dpkg --remove --force-depends minios-live-config user-setup" in j for j in joined), joined
        assert not os.path.exists(os.path.join(target, "usr/bin/audio-allowuser.sh"))
        assert any("continuing with artifact cleanup" in line for line in logs)


def test_native_generates_missing_ssh_host_keys():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/sbin/sshd"))
        _write(os.path.join(target, "usr/bin/ssh-keygen"))
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        with patch("native_deploy._chroot", side_effect=fake_chroot):
            _generate_ssh_host_keys(target, lambda line: None, dry_run=False)

        assert calls == [["ssh-keygen", "-A"]]


def test_native_does_not_regenerate_existing_ssh_host_keys():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/sbin/sshd"))
        _write(os.path.join(target, "usr/bin/ssh-keygen"))
        _write(os.path.join(target, "etc/ssh/ssh_host_ed25519_key"))

        with patch("native_deploy._chroot") as chroot:
            _generate_ssh_host_keys(target, lambda line: None, dry_run=False)

        chroot.assert_not_called()


def test_native_generates_missing_ssl_snakeoil_cert():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/sbin/make-ssl-cert"))
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        with patch("native_deploy._chroot", side_effect=fake_chroot):
            _generate_ssl_snakeoil_cert(target, lambda line: None, dry_run=False)

        assert calls == [["make-ssl-cert", "generate-default-snakeoil", "--force-overwrite"]]


def test_native_does_not_regenerate_existing_ssl_snakeoil_cert():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "usr/sbin/make-ssl-cert"))
        _write(os.path.join(target, "etc/ssl/certs/ssl-cert-snakeoil.pem"))

        with patch("native_deploy._chroot") as chroot:
            _generate_ssl_snakeoil_cert(target, lambda line: None, dry_run=False)

        chroot.assert_not_called()


def test_native_collects_allowuser_groups_for_direct_user_creation():
    with tempfile.TemporaryDirectory() as target:
        _write(
            os.path.join(target, "usr/bin/virt-manager-allowuser.sh"),
            "#!/bin/sh\n"
            "if ! grep libvirt /etc/group | grep $(id -nu 1000); then\n"
            "    usermod -a -G libvirt $(id -nu 1000)\n"
            "fi\n",
        )
        _write(os.path.join(target, "etc/passwd"), "root:x:0:0:root:/root:/bin/bash\n")
        _write(os.path.join(target, "etc/group"), "sudo:x:27:\nlibvirt:x:108:\n")
        groups = _collect_live_allowuser_groups(target)
        assert groups == ["libvirt"]

        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        state = InstallState(install_mode="native")
        state.user_config = UserConfig(username="alice")
        with patch("native_deploy._chroot", side_effect=fake_chroot):
            _apply_native_settings(target, state, lambda line: None, dry_run=False, extra_user_groups=groups)
        joined = [" ".join(c) for c in calls]
        assert any(j == "useradd -m -s /bin/bash -G sudo,libvirt alice" for j in joined), joined


def test_native_strict_filters_risky_allowuser_groups():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "etc/passwd"), "root:x:0:0:root:/root:/bin/bash\n")
        _write(os.path.join(target, "etc/group"), "sudo:x:27:\nlibvirt:x:108:\ndocker:x:999:\n")
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        state = InstallState(install_mode="native", security_profile="strict")
        state.user_config = UserConfig(username="alice", user_default_groups="sudo,docker")
        with patch("native_deploy._chroot", side_effect=fake_chroot):
            _apply_native_settings(target, state, lambda line: None, dry_run=False, extra_user_groups=["libvirt"])
        joined = [" ".join(c) for c in calls]
        assert any(j == "useradd -m -s /bin/bash -G sudo alice" for j in joined), joined


def test_native_default_remote_policy_and_generic_service_override():
    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "etc/passwd"), "root:x:0:0:root:/root:/bin/bash\n")
        _write(os.path.join(target, "etc/group"), "sudo:x:27:\n")
        calls = []

        def fake_chroot(target_path, args, log_cb, dry_run=False, check=True, input_text=None):
            calls.append(list(args))

        state = InstallState(install_mode="native")
        state.user_config = UserConfig(username="alice", enable_services="ssh,xrdp,cups")
        with patch("native_deploy._chroot", side_effect=fake_chroot), patch(
            "native_deploy._target_service_action",
            side_effect=lambda _target, action, service, *_args, **_kwargs: calls.append(["svc", action, service]),
        ):
            _apply_native_settings(target, state, lambda line: None, dry_run=False)
        joined = [" ".join(c) for c in calls]
        assert any(j == "svc enable cups" for j in joined), joined
        assert any(j == "svc enable ssh" for j in joined), joined
        assert any(j == "svc enable xrdp" for j in joined), joined
        assert not any(j == "svc disable ssh" for j in joined), joined
        assert not any(j == "svc disable xrdp" for j in joined), joined


def test_native_service_action_uses_legacy_systemctl_without_minios_svc():
    calls = []

    def fake_chroot(target, args, log_cb, dry_run=False, check=True, input_text=None):
        calls.append((list(args), check))

    with tempfile.TemporaryDirectory() as target:
        _write(os.path.join(target, "bin/systemctl"))
        with patch("native_deploy._chroot", side_effect=fake_chroot):
            _target_service_action(target, "disable", "ssh", lambda line: None)
            _target_service_action(target, "default", "multi-user.target", lambda line: None)

    assert calls == [
        (["systemctl", "disable", "ssh.service"], False),
        (["systemctl", "set-default", "multi-user.target"], False),
    ]


def test_native_service_action_logs_when_no_legacy_tool_exists():
    logs = []
    with tempfile.TemporaryDirectory() as target:
        _target_service_action(target, "disable", "ssh", logs.append)
    assert any("Service action skipped" in line for line in logs)


def test_native_user_creation_happens_before_live_package_cleanup():
    import native_deploy
    from partition_models import PartitionPlan

    state = InstallState(install_mode="native", target_device="/dev/sda")
    state.user_config = UserConfig(username="alice")
    plan = PartitionPlan(device="/dev/sda", use_gpt=False, wipe_disk=True)
    events = []

    with patch("native_deploy.resolve_install_device", return_value="/dev/sda"), \
         patch("native_deploy.scan_disk"), \
          patch("native_deploy.build_plan", return_value=plan), \
          patch("native_deploy.preflight_selected_bundles", return_value=1024), \
         patch("native_deploy.execute_plan", return_value=("/dev/sda1", None, "/target", None)), \
         patch("native_deploy.BundleOverlay") as overlay, \
         patch("native_deploy._copy_native_root"), \
         patch("native_deploy._prepare_runtime_dirs"), \
         patch("native_deploy._patch_sysv_quiet_wrapper"), \
         patch("native_deploy._maybe_restore_kernel_metadata"), \
         patch("native_deploy._mount_chroot_api"), \
         patch("native_deploy._unmount_chroot_api"), \
         patch("native_deploy._install_native_packages"), \
         patch("native_deploy._collect_live_allowuser_groups", return_value=[]), \
         patch("native_deploy._cleanup_native_live_packages", side_effect=lambda *_a, **_kw: events.append("cleanup")), \
         patch("native_deploy.apply_security_profile", side_effect=lambda *_a, **_kw: events.append("profile")), \
         patch("native_deploy._write_fstab"), \
         patch("native_deploy._apply_native_settings", side_effect=lambda *_a, **_kw: events.append("settings")), \
         patch("native_deploy._install_native_bootloader"), \
         patch("native_deploy.unmount_partitions"):
        overlay.return_value.__enter__.return_value = "/source"
        native_deploy.run_native_install(state, lambda *_: None, lambda *_: None)

    assert events == ["profile", "settings", "cleanup"]


def test_native_multiboot_offline_is_blocked_before_disk_changes():
    import native_deploy
    import pytest
    from partition_models import PartitionPlan

    state = InstallState(install_mode="native", target_device="/dev/sda", placement="free_space")
    plan = PartitionPlan(device="/dev/sda", use_gpt=False, wipe_disk=False, use_efi=False)

    with patch("native_deploy.resolve_install_device", return_value="/dev/sda"), \
         patch("native_deploy.scan_disk"), \
         patch("native_deploy.build_plan", return_value=plan), \
         patch("native_deploy.native_missing_packages", return_value=["grub-pc", "os-prober"]), \
         patch("native_deploy.execute_plan") as execute:
        with pytest.raises(RuntimeError, match="requires GRUB and os-prober"):
            native_deploy.run_native_install(state, lambda *_: None, lambda *_: None)

    execute.assert_not_called()


def test_native_download_failure_is_blocked_before_disk_changes():
    import native_deploy
    import pytest
    from partition_models import PartitionPlan

    state = InstallState(
        install_mode="native",
        target_device="/dev/sda",
        placement="alongside_os",
        download_missing_packages=True,
    )
    plan = PartitionPlan(device="/dev/sda", use_gpt=False, wipe_disk=False, use_efi=False)

    with patch("native_deploy.resolve_install_device", return_value="/dev/sda"), \
         patch("native_deploy.scan_disk"), \
         patch("native_deploy.build_plan", return_value=plan), \
         patch("native_deploy.native_missing_packages", return_value=["grub-pc"]), \
         patch("native_deploy.preflight_package_download", return_value={"missing": ["grub-pc"]}), \
         patch("native_deploy.execute_plan") as execute:
        with pytest.raises(RuntimeError, match="cannot be downloaded"):
            native_deploy.run_native_install(state, lambda *_: None, lambda *_: None)

    execute.assert_not_called()


def test_native_stages_complete_target_package_closure_before_disk_changes():
    import native_deploy
    import pytest
    from partition_models import PartitionPlan

    state = InstallState(
        install_mode="native",
        target_device="/dev/sda",
        placement="erase_all",
        download_missing_packages=True,
    )
    plan = PartitionPlan(device="/dev/sda", use_gpt=False, wipe_disk=True, use_efi=False)
    required = ["grub-pc", "grub-common", "initramfs-tools", "e2fsprogs"]

    with patch("native_deploy.resolve_install_device", return_value="/dev/sda"), \
         patch("native_deploy.scan_disk"), \
         patch("native_deploy.build_plan", return_value=plan), \
         patch("native_deploy.manual_native_package_requirements", return_value=required), \
         patch("native_deploy.native_missing_packages", return_value=["grub-pc"]), \
         patch("native_deploy.preflight_package_download", return_value={"missing": []}), \
         patch("native_deploy.preflight_ok", return_value=True), \
         patch("native_deploy.prepare_package_cache", side_effect=RuntimeError("stop")) as stage, \
         patch("native_deploy.execute_plan") as execute:
        with pytest.raises(RuntimeError, match="could not be downloaded"):
            native_deploy.run_native_install(state, lambda *_: None, lambda *_: None)

    stage.assert_called_once_with(required)
    execute.assert_not_called()
