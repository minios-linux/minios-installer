#!/usr/bin/env python3

import os
from unittest.mock import patch

import pytest


class TestUserConfigWriter:
    def test_write_live_config_preserves_base_file(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        source = tmp_path / "config.conf"
        source.write_text(
            "LIVE_USERNAME='live'\n"
            "LIVE_HOSTNAME='minios'\n"
            "ENABLE_SERVICES='ssh'\n",
            encoding="utf-8",
        )

        user = UserConfig(username="alice", hostname="workstation")
        path = write_live_config(user, str(source))

        try:
            assert path is not None
            result = open(path, encoding="utf-8").read()
            assert "LIVE_USERNAME='alice'" in result
            assert "LIVE_HOSTNAME='workstation'" in result
            assert "ENABLE_SERVICES='ssh'" in result
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_write_live_config_appends_new_fields(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        source = tmp_path / "config.conf"
        source.write_text("LIVE_USERNAME='live'\n", encoding="utf-8")

        user = UserConfig(locale="ru_RU.UTF-8", timezone="Europe/Moscow", keyboard="us,ru")
        path = write_live_config(user, str(source))

        try:
            result = open(path, encoding="utf-8").read()
            assert "LIVE_LOCALES='ru_RU.UTF-8'" in result
            assert "LIVE_TIMEZONE='Europe/Moscow'" in result
            assert "LIVE_KEYBOARD_LAYOUTS='us,ru'" in result
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_write_live_config_emits_static_network_only_when_supported(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        user = UserConfig(
            network_method="static",
            network_interface="eth0",
            network_address="192.168.1.20",
            network_prefix="24",
            network_gateway="192.168.1.1",
            network_dns="1.1.1.1,8.8.8.8",
        )
        unsupported = write_live_config(user, str(tmp_path / "missing.conf"))
        supported = write_live_config(user, str(tmp_path / "missing.conf"), include_live_network=True)
        try:
            assert unsupported is None
            result = open(supported, encoding="utf-8").read()
            assert "LIVE_NETWORK_METHOD='static'" in result
            assert "LIVE_NETWORK_INTERFACE='eth0'" in result
            assert "LIVE_NETWORK_ADDRESS='192.168.1.20'" in result
            assert "LIVE_NETWORK_PREFIX='24'" in result
            assert "LIVE_NETWORK_GATEWAY='192.168.1.1'" in result
            assert "LIVE_NETWORK_DNS='1.1.1.1,8.8.8.8'" in result
        finally:
            if supported and os.path.exists(supported):
                os.unlink(supported)

    def test_write_live_config_returns_none_without_changes(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        source = tmp_path / "config.conf"
        source.write_text("LIVE_USERNAME='live'\n", encoding="utf-8")

        assert write_live_config(UserConfig(), str(source)) is None

    def test_write_live_config_emits_concrete_profile_entries(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        source = tmp_path / "config.conf"
        source.write_text("LIVE_USERNAME='live'\n", encoding="utf-8")

        path = write_live_config(
            UserConfig(),
            str(source),
            {
                "LIVE_SUDO_MODE": "password",
                "LIVE_CONFIG_CMDLINE": "noautologin",
            },
        )

        try:
            result = open(path, encoding="utf-8").read()
            assert "LIVE_SECURITY_PROFILE=" not in result
            assert "LIVE_SUDO_MODE='password'" in result
            assert "LIVE_CONFIG_CMDLINE='noautologin'" in result
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_write_live_config_user_overrides_profile_entry(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        path = write_live_config(
            UserConfig(config_cmdline="quiet", noroot="true"),
            str(tmp_path / "missing.conf"),
            {"LIVE_CONFIG_CMDLINE": "noautologin", "LIVE_CONFIG_NOROOT": "false"},
        )

        try:
            result = open(path, encoding="utf-8").read()
            assert "LIVE_CONFIG_CMDLINE='quiet'" in result
            assert "LIVE_CONFIG_NOROOT='true'" in result
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    @pytest.mark.parametrize("path", ["", "/", "/minios/../etc", "/minios//userdata", "/minios/./userdata"])
    def test_write_live_config_rejects_unsafe_user_dirs_path(self, tmp_path, path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        with pytest.raises(ValueError):
            write_live_config(
                UserConfig(link_user_dirs="true", user_dirs_path=path),
                str(tmp_path / "missing.conf"),
            )

    def test_write_live_config_rejects_both_user_dirs_modes(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        with pytest.raises(ValueError):
            write_live_config(
                UserConfig(
                    link_user_dirs="true",
                    bind_user_dirs="true",
                    user_dirs_path="/minios/userdata",
                ),
                str(tmp_path / "missing.conf"),
            )

    def test_write_live_config_preserves_explicit_service_choices(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        path = write_live_config(
            UserConfig(enable_services="ssh,xrdp,cups", disable_services="bluetooth"),
            str(tmp_path / "missing.conf"),
            {},
        )

        try:
            result = open(path, encoding="utf-8").read()
            assert "ENABLE_SERVICES='ssh,xrdp,cups'" in result
            assert "DISABLE_SERVICES='bluetooth'" in result
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_write_live_config_creates_file_without_source(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        missing = tmp_path / "missing.conf"
        user = UserConfig(username="bob", default_target="multi-user.target", password="secret")
        with patch("user_config_writer.hash_system_password", return_value="$6$salt$hash"):
            path = write_live_config(user, str(missing))

        try:
            assert path is not None
            result = open(path, encoding="utf-8").read()
            assert "LIVE_USERNAME='bob'" in result
            assert "DEFAULT_TARGET='multi-user.target'" in result
            assert "LIVE_USER_PASSWORD_CRYPTED='$6$salt$hash'" in result
            assert "LIVE_USER_PASSWORD=" not in result  # no plaintext password key
            assert os.path.exists(path)
            # Password-bearing temp config must not be world-readable.
            assert (os.stat(path).st_mode & 0o077) == 0
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_write_live_config_does_not_preserve_world_readable_mode(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        source = tmp_path / "config.conf"
        source.write_text("LIVE_USERNAME='live'\n", encoding="utf-8")
        os.chmod(source, 0o644)

        with patch("user_config_writer.hash_system_password", return_value="$6$x$y"):
            path = write_live_config(UserConfig(password="secret"), str(source))
        try:
            assert path is not None
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_write_live_config_all_configurator_fields(self, tmp_path):
        from install_state import UserConfig
        from user_config_writer import write_live_config

        user = UserConfig(
            username="alice",
            full_name="Alice Example",
            user_default_groups="audio,video",
            password="userpass",
            root_password="rootpass",
            link_user_dirs="true",
            bind_user_dirs="false",
            user_dirs_path="/minios/userdirs",
            noroot="false",
            hostname="workstation",
            locale="en_US.UTF-8,ru_RU.UTF-8",
            timezone="UTC",
            default_target="graphical",
            enable_services="ssh, NetworkManager",
            disable_services="bluetooth",
            keyboard_model="pc105",
            keyboard="us,ru",
            keyboard_options="grp:alt_shift_toggle",
            keyboard_variants=",",
            module_mode="merged",
            config_cmdline="live-config.debug",
            config_debug="true",
            export_logs="true",
        )
        with patch("user_config_writer.hash_system_password", side_effect=lambda p: f"HASH({p})"):
            path = write_live_config(user, str(tmp_path / "missing.conf"))

        try:
            result = open(path, encoding="utf-8").read()
            expected = {
                "LIVE_USERNAME": "alice",
                "LIVE_USER_FULLNAME": "Alice Example",
                "LIVE_USER_DEFAULT_GROUPS": "audio,video",
                "LIVE_USER_PASSWORD_CRYPTED": "HASH(userpass)",
                "LIVE_ROOT_PASSWORD_CRYPTED": "HASH(rootpass)",
                "LIVE_LINK_USER_DIRS": "true",
                "LIVE_BIND_USER_DIRS": "false",
                "LIVE_USER_DIRS_PATH": "/minios/userdirs",
                "LIVE_CONFIG_NOROOT": "false",
                "LIVE_HOSTNAME": "workstation",
                "LIVE_LOCALES": "en_US.UTF-8,ru_RU.UTF-8",
                "LIVE_TIMEZONE": "UTC",
                "DEFAULT_TARGET": "graphical.target",
                "ENABLE_SERVICES": "ssh,NetworkManager",
                "DISABLE_SERVICES": "bluetooth",
                "LIVE_KEYBOARD_MODEL": "pc105",
                "LIVE_KEYBOARD_LAYOUTS": "us,ru",
                "LIVE_KEYBOARD_OPTIONS": "grp:alt_shift_toggle",
                "LIVE_KEYBOARD_VARIANTS": ",",
                "LIVE_MODULE_MODE": "merged",
                "LIVE_CONFIG_CMDLINE": "live-config.debug",
                "LIVE_CONFIG_DEBUG": "true",
                "EXPORT_LOGS": "true",
            }
            for key, value in expected.items():
                assert f"{key}='{value}'" in result, f"missing {key}"
        finally:
            if path and os.path.exists(path):
                os.unlink(path)
