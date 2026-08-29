#!/usr/bin/env python3

import datetime
import os
import stat
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from install_state import InstallState
from main_installer import (InstallerWindow, backend_command_for_state,
                            can_navigate_to_viewed_step, format_log_message)


def test_missing_native_contracts_keep_live_installation():
    state = InstallState(install_mode="live")
    window = SimpleNamespace(
        state=state,
        native_install_available=False,
        _refresh_mode_card_styles=Mock(),
    )

    InstallerWindow._set_install_mode(window, "native")

    assert state.install_mode == "live"
    window._refresh_mode_card_styles.assert_called_once_with()

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


def test_fallback_log_is_private_and_does_not_follow_predictable_symlink(tmp_path):
    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 10, 12, 34, 56, tzinfo=tz)

    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    predictable = tmp_path / "minios-installer-20260810-123456.log"
    predictable.symlink_to(victim)
    window = SimpleNamespace(
        install_log_path="/var/log/minios/installer.log",
        _install_log_lock=threading.Lock(),
    )
    descriptors = []
    real_mkstemp = tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        descriptor, path = real_mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, path

    with patch("main_installer.os.makedirs", side_effect=PermissionError), \
         patch("main_installer.datetime.datetime", FixedDatetime), \
         patch("main_installer.tempfile.gettempdir", return_value=str(tmp_path)), \
         patch("main_installer.tempfile.mkstemp", side_effect=capture_mkstemp):
        InstallerWindow._reset_install_log(window)

    assert victim.read_text(encoding="utf-8") == "keep"
    assert window.install_log_path != str(predictable)
    assert os.path.dirname(window.install_log_path) == str(tmp_path)
    assert not os.path.islink(window.install_log_path)
    assert stat.S_IMODE(os.stat(window.install_log_path).st_mode) == 0o640
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_security_profile_descriptions_match_install_mode():
    state = InstallState(install_mode="live", security_profile="balanced")
    window = SimpleNamespace(state=state)

    live_text = InstallerWindow._profile_description(window, "balanced")
    assert "Automatic login is disabled" in live_text
    assert "SSH allows password login for users but blocks root login" in live_text

    state.install_mode = "native"
    native_text = InstallerWindow._profile_description(window, "balanced")
    assert "Recommended for a regular installation" in native_text
    assert "Automatic login" not in native_text


def test_partial_security_summary_does_not_promise_unsupported_behavior():
    state = InstallState(install_mode="live", security_profile="balanced")
    window = SimpleNamespace(state=state)
    window._profile_description = lambda profile: InstallerWindow._profile_description(
        window, profile
    )

    full = InstallerWindow._profile_summary_description(window, "balanced", "full")
    partial = InstallerWindow._profile_summary_description(window, "balanced", "partial")

    assert "SSH allows password login" in full
    assert "not supported" in partial
    assert "SSH allows password login" not in partial


def test_strict_profile_disables_xrdp_control():
    state = InstallState(install_mode="live", security_profile="strict")
    xrdp = Mock()
    xrdp.get_active.return_value = True
    note = Mock()
    window = SimpleNamespace(
        state=state,
        remote_service_checks={"xrdp": xrdp},
        remote_access_note=note,
    )

    InstallerWindow._refresh_security_remote_controls(window)

    xrdp.set_sensitive.assert_called_once_with(False)
    xrdp.set_active.assert_called_once_with(False)
    note.set_text.assert_called_once()


def test_append_log_sends_identical_record_to_disk_and_shared_view(tmp_path):
    path = tmp_path / "installer.log"
    log_view = Mock()
    window = SimpleNamespace(
        install_log_path=str(path),
        _install_log_lock=threading.Lock(),
        log_view=log_view,
    )

    def idle_add(callback, *args):
        callback(*args)
        return 1

    with patch("main_installer.format_log_message", return_value="record"), \
         patch("main_installer.GLib.idle_add", side_effect=idle_add):
        InstallerWindow._append_log(window, "message")

    assert path.read_text(encoding="utf-8") == "record\n"
    log_view.feed.assert_called_once_with("record\n")
