#!/usr/bin/env python3

import os
import threading
from types import SimpleNamespace
from unittest.mock import patch

from install_state import InstallState
from main_installer import InstallerWindow, backend_command_for_state, can_navigate_to_viewed_step, format_log_message


def test_only_viewed_non_current_steps_are_clickable():
    viewed = {0, 1, 3}
    assert can_navigate_to_viewed_step(0, 1, viewed)
    assert not can_navigate_to_viewed_step(1, 1, viewed)
    assert not can_navigate_to_viewed_step(2, 1, viewed)
    assert can_navigate_to_viewed_step(3, 1, viewed)


def test_install_running_blocks_all_sidebar_navigation():
    assert not can_navigate_to_viewed_step(0, 2, {0, 1, 2}, install_running=True)


def test_backend_command_records_request_without_passwords():
    state = InstallState(
        install_mode="native",
        target_device="/dev/disk/by-id/test disk",
        filesystem="ext4",
        swap_size_mib=8192,
        selected_modules=["00-core.sb", "01-kernel.sb"],
        download_missing_packages=True,
    )
    state.user_config.username = "live"
    state.user_config.password = "secret"

    command = backend_command_for_state(state)

    assert command.startswith("/usr/bin/minios-deploy install ")
    assert "'/dev/disk/by-id/test disk'" in command
    assert "--swap-size 8192" in command
    assert "--download-packages" in command
    assert "secret" not in command
    assert "<redacted>" in command


def test_structured_log_prefixes_each_line_and_classifies_commands():
    timestamp = "2026-07-25T23:08:14+03:00"

    message = format_log_message("$ apt-get install\nERROR: failed", timestamp=timestamp)

    assert message.splitlines() == [
        timestamp + " [CMD] $ apt-get install",
        timestamp + " [ERROR] ERROR: failed",
    ]


def test_install_log_keeps_timestamped_history_and_latest_symlink(tmp_path):
    latest = tmp_path / "installer.log"
    window = SimpleNamespace(
        install_log_path=str(latest),
        _install_log_lock=threading.Lock(),
    )

    with patch("main_installer.INSTALL_LOG_DIR", str(tmp_path)), \
         patch("main_installer.INSTALL_LOG_PATH", str(latest)), \
         patch("main_installer.shutil.chown"):
        InstallerWindow._reset_install_log(window)
        first = window.install_log_path
        InstallerWindow._reset_install_log(window)
        second = window.install_log_path

    assert first != second
    assert os.path.isfile(first)
    assert os.path.isfile(second)
    assert latest.is_symlink()
    assert os.path.realpath(latest) == second
