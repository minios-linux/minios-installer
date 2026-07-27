#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for copy_utils module.
"""

import os


def test_efi_payload_preflight_and_copy_verification(tmp_path):
    from copy_utils import efi_payload_bytes, verify_efi_payload
    source = tmp_path / "source"
    payload = source / "boot" / "EFI" / "BOOT" / "BOOTX64.EFI"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"efi")
    assert efi_payload_bytes(str(source)) == 3
    destination = tmp_path / "destination"
    copied = destination / "EFI" / "BOOT" / "BOOTX64.EFI"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(b"efi")
    verify_efi_payload(str(source), str(destination))
    copied.write_bytes(b"x")
    try:
        verify_efi_payload(str(source), str(destination))
    except RuntimeError:
        pass
    else:
        assert False


def test_efi_payload_requires_architecture_fallback_loader(tmp_path):
    from copy_utils import efi_payload_bytes

    source = tmp_path / "source"
    vendor_loader = source / "boot" / "EFI" / "MiniOS" / "grubx64.efi"
    vendor_loader.parent.mkdir(parents=True)
    vendor_loader.write_bytes(b"efi")

    try:
        efi_payload_bytes(str(source))
    except RuntimeError as exc:
        assert "EFI/BOOT/BOOTX64.EFI" in str(exc)
    else:
        raise AssertionError("UEFI preflight must require the removable fallback loader")


def test_efi_payload_accepts_case_insensitive_fat_layout(tmp_path):
    from copy_utils import efi_payload_bytes

    source = tmp_path / "source"
    fallback = source / "boot" / "EFI" / "boot" / "bootx64.efi"
    fallback.parent.mkdir(parents=True)
    fallback.write_bytes(b"efi")

    assert efi_payload_bytes(str(source)) == 3


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


class TestCopyMiniosFiles:
    def test_copy_minios_files_adds_luks_boot_options(self, tmp_path):
        from copy_utils import copy_minios_files

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "boot" / "grub").mkdir(parents=True)
        (src / "boot" / "syslinux").mkdir()
        (src / "boot" / "vmlinuz").write_text("kernel")
        (src / "boot" / "grub" / "grub.multilang.cfg").write_text(
            "linux /minios/boot/vmlinuz perchmode=old perchsize=1 quiet\n"
        )
        (src / "boot" / "syslinux" / "syslinux.multilang.cfg").write_text(
            "APPEND boot=live perchmode=old perchsize=1\n"
        )
        dst.mkdir()

        copy_minios_files(
            str(src), str(dst), lambda *_: None, lambda *_: None,
            boot_options=("perchmode=luks", "perchsize=2048"),
        )

        for path in (
            dst / "minios" / "boot" / "grub" / "grub.cfg",
            dst / "minios" / "boot" / "syslinux" / "syslinux.cfg",
        ):
            content = path.read_text()
            assert content.count("perchmode=luks") == 1
            assert content.count("perchsize=2048") == 1
            assert "perchmode=old" not in content
            assert "perchsize=1" not in content

    def test_copy_minios_files_filters_unselected_top_level_modules(self, tmp_path):
        from copy_utils import copy_minios_files

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "boot").mkdir(parents=True)
        (src / "changes").mkdir()
        for name in ["00-core.sb", "01-kernel.sb", "02-fw.sb", "03-gui.sb"]:
            (src / name).write_text(name)
        (src / "boot" / "vmlinuz").write_text("kernel")
        (src / "changes" / "ignored").write_text("changes")
        dst.mkdir()

        copy_minios_files(str(src), str(dst), lambda *_: None, lambda *_: None, selected_modules=["02-fw.sb"])

        assert (dst / "minios" / "00-core.sb").exists()
        assert (dst / "minios" / "01-kernel.sb").exists()
        assert (dst / "minios" / "02-fw.sb").exists()
        assert not (dst / "minios" / "03-gui.sb").exists()
        assert (dst / "minios" / "boot" / "vmlinuz").exists()
        assert not (dst / "minios" / "changes" / "ignored").exists()
