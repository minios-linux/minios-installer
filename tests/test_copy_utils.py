#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for copy_utils module.
"""

import os


class TestSyslinuxConfigProcessing:
    """Tests for SYSLINUX config language processing."""

    def test_remove_live_config_params_bytes(self):
        """Removes live-config parameters from bytes content."""
        from copy_utils import _remove_live_config_params_bytes

        content = (
            b"APPEND boot=live locales=ru_RU.UTF-8 timezone=Europe/Moscow "
            b"keyboard-layouts=us,ru toram\n"
        )

        cleaned = _remove_live_config_params_bytes(content)

        assert b"locales=" not in cleaned
        assert b"timezone=" not in cleaned
        assert b"keyboard-layouts=" not in cleaned
        assert b"toram" in cleaned

    def test_process_syslinux_config_preserves_cp866(self, tmp_path):
        """Uses localized ru_RU.cfg in CP866 without UTF-8 decode errors."""
        from copy_utils import _process_syslinux_config

        syslinux_dir = tmp_path / "minios" / "boot" / "syslinux"
        lang_dir = syslinux_dir / "lang"
        os.makedirs(lang_dir, exist_ok=True)

        ru_label = "Русский".encode("cp866")
        ru_cfg = (
            b"UI vesamenu.c32\n"
            b"LABEL live\n"
            b"MENU LABEL " + ru_label + b"\n"
            b"APPEND vga=788 locales=ru_RU.UTF-8 timezone=Europe/Moscow keyboard-layouts=us,ru toram\n"
        )
        (lang_dir / "ru_RU.cfg").write_bytes(ru_cfg)

        logs = []
        _process_syslinux_config(str(tmp_path), "ru_RU", logs.append)

        result = (syslinux_dir / "syslinux.cfg").read_bytes()
        assert ru_label in result
        assert b"locales=" not in result
        assert b"timezone=" not in result
        assert b"keyboard-layouts=" not in result
        assert b"toram" in result

    def test_process_syslinux_config_fallback_to_en_us(self, tmp_path):
        """Falls back to en_US.cfg when selected language file is missing."""
        from copy_utils import _process_syslinux_config

        syslinux_dir = tmp_path / "minios" / "boot" / "syslinux"
        lang_dir = syslinux_dir / "lang"
        os.makedirs(lang_dir, exist_ok=True)

        en_cfg = (
            b"LABEL live\n"
            b"MENU LABEL English\n"
            b"APPEND locales=en_US.UTF-8 timezone=Etc/UTC keyboard-layouts=us\n"
        )
        (lang_dir / "en_US.cfg").write_bytes(en_cfg)

        logs = []
        _process_syslinux_config(str(tmp_path), "ru_RU", logs.append)

        result = (syslinux_dir / "syslinux.cfg").read_bytes()
        assert b"MENU LABEL English" in result
        assert b"locales=" not in result
        assert b"timezone=" not in result
        assert b"keyboard-layouts=" not in result
