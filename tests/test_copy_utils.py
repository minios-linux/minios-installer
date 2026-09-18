#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for copy_utils module.
"""

import os
import pytest


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


class TestGrubConfigProcessing:
    """Tests for localized GRUB menu generation."""

    def test_localized_grub_translates_all_current_menu_entries(self, tmp_path):
        from copy_utils import _generate_localized_grub_config

        grub_dir = tmp_path / "grub"
        (grub_dir / "po").mkdir(parents=True)
        entries = {
            "Start MiniOS": "Запустить MiniOS",
            "Start a new session": "Начать новую сессию",
            "Choose a saved session": "Выбрать сохранённую сессию",
            "Start without saving": "Запустить без сохранения",
            "Run from RAM": "Запустить из ОЗУ",
        }
        (grub_dir / "grub.template.cfg").write_text(
            "\n".join(f'menuentry "{text}" {{}}' for text in entries) + "\n",
            encoding="utf-8",
        )
        (grub_dir / "po" / "ru_RU.po").write_text(
            "\n\n".join(
                f'msgid "{source}"\nmsgstr "{translated}"'
                for source, translated in entries.items()
            ) + "\n",
            encoding="utf-8",
        )
        output = grub_dir / "grub.cfg"

        assert _generate_localized_grub_config(
            str(grub_dir), "ru_RU", str(output), lambda *_: None
        )
        result = output.read_text(encoding="utf-8")
        for source, translated in entries.items():
            assert f'menuentry "{translated}"' in result
            assert f'menuentry "{source}"' not in result

    @pytest.mark.parametrize('language', ['en_US', 'ru_RU', 'multilang'])
    def test_new_navigation_is_only_available_on_multilingual_media(self, tmp_path, language):
        from copy_utils import _process_grub_config, _process_syslinux_config

        grub = tmp_path / 'minios' / 'boot' / 'grub'
        syslinux = tmp_path / 'minios' / 'boot' / 'syslinux'
        (grub / 'po').mkdir(parents=True)
        (grub / 'minios-theme').mkdir()
        (syslinux / 'lang').mkdir(parents=True)
        (syslinux / 'help').mkdir()
        template = (
            'set theme=/minios/boot/grub/minios-theme/theme.txt\n'
            'menuentry "Start MiniOS" --class resume {\n'
            ' linux /minios/boot/vmlinuz boot=live perchdir=resume\n}\n'
            'source /minios/boot/grub/navigation.cfg\n')
        (grub / 'grub.template.cfg').write_text(template)
        (grub / 'grub.multilang.cfg').write_text(template)
        navigation = (
            'menuentry "$language_label" --class locale --hotkey=f2 --id minios-language {\n'
            ' configfile /minios/boot/grub/languages.cfg\n}\n'
            'menuentry " " --id minios-separator {\n true\n}\n'
            'menuentry "$help_label" --class help --hotkey=f1 --id minios-help {\n'
            ' echo $"F2 changes the menu and system language."\n'
            ' echo $"Saving requires writable storage."\n read answer\n}\n')
        (grub / 'navigation.cfg').write_text(navigation)
        for locale, title, codec in (('en_US', 'Start MiniOS', 'ascii'),
                                     ('ru_RU', 'Запустить MiniOS', 'cp866')):
            (grub / 'po' / (locale + '.po')).write_text(
                'msgid "Start MiniOS"\nmsgstr "{}"\n'.format(title), encoding='utf-8')
            (grub / 'minios-theme' / ('theme_' + locale + '.txt')).write_text(
                'desktop-image: "/minios/boot/bootlogo791.png"\n'
                'text = "[F1] Help  [F2] Language  [E] Edit"\n')
            config = (
                'UI minios-menu.c32\nTIMEOUT 100\n'
                'MENU HIDDENKEY F2 minios-language\n'
                'MENU TABMSG [F1] Help [F2] Language [Tab] Edit\n'
                'F1 help/modes_{}.txt zblack.png\n'
                'LABEL default\nMENU LABEL {}\n'
                'KERNEL /minios/boot/vmlinuz\n'
                'APPEND boot=live perchdir=resume locales={}.UTF-8\n'
                'LABEL minios-language\nMENU HIDE\nCONFIG lang/select_{}.cfg\n'
            ).format(locale, title, locale, locale).encode(codec)
            (syslinux / 'lang' / (locale + '.cfg')).write_bytes(config)
            if locale == 'en_US':
                (syslinux / 'syslinux.multilang.cfg').write_bytes(config)
            (syslinux / 'help' / ('modes_' + locale + '.txt')).write_bytes(
                ('{}\nF2 changes the menu and system language, keyboard\n'
                 'and time zone defaults.\nTab edits boot parameters.\n\nReturn\n'
                 ).format(title).encode(codec))

        _process_grub_config(str(tmp_path), language, lambda *_: None)
        _process_syslinux_config(str(tmp_path), language, lambda *_: None)
        grub_result = (grub / 'grub.cfg').read_text()
        nav_result = (grub / 'navigation.cfg').read_text()
        sys_result = (syslinux / 'syslinux.cfg').read_bytes()
        assert '--hotkey=f1' in nav_result
        assert b'F1 help/modes_' in sys_result
        assert 'perchdir=resume' in grub_result
        assert b'perchdir=resume' in sys_result
        if language == 'multilang':
            assert nav_result == navigation
            assert b'MENU HIDDENKEY F2' in sys_result
        else:
            assert 'set lang=' + language in grub_result
            assert 'minios-language' not in nav_result
            assert 'F2' not in nav_result
            assert b'F2' not in sys_result
            assert b'CONFIG lang/select_' not in sys_result
            assert b'locales=' not in sys_result
            hint = (grub / 'minios-theme' / ('theme_' + language + '.txt')).read_text()
            assert '[F2]' not in hint
            assert '[F1]' in hint and '[E]' in hint
            help_text = (syslinux / 'help' / ('modes_' + language + '.txt')).read_bytes()
            assert b'F2' not in help_text and b'time zone' not in help_text
            assert b'Tab edits' in help_text
            if language == 'ru_RU':
                assert 'Запустить MiniOS'.encode('cp866') in sys_result


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
    def test_copy_minios_files_does_not_rewrite_session_boot_options(self, tmp_path):
        from copy_utils import copy_minios_files

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "boot" / "grub").mkdir(parents=True)
        (src / "boot" / "syslinux" / "lang").mkdir(parents=True)
        (src / "boot" / "vmlinuz").write_text("kernel")
        (src / "boot" / "grub" / "grub.multilang.cfg").write_text(
            "menuentry 'English' {\n    configfile /minios/boot/grub/main.cfg\n}\n"
        )
        (src / "boot" / "grub" / "main.cfg").write_text(
            "linux /minios/boot/vmlinuz perchmode=old perchsize=1 perchencrypt=old quiet\n"
        )
        (src / "boot" / "syslinux" / "syslinux.multilang.cfg").write_text(
            "LABEL en_US\nCONFIG lang/en_US.cfg\n"
        )
        (src / "boot" / "syslinux" / "lang" / "en_US.cfg").write_text(
            "APPEND boot=live perchmode=old perchsize=1 perchencrypt=old\n"
        )
        dst.mkdir()

        copy_minios_files(
            str(src), str(dst), lambda *_: None, lambda *_: None,
        )

        for path in (
            dst / "minios" / "boot" / "grub" / "main.cfg",
            dst / "minios" / "boot" / "syslinux" / "lang" / "en_US.cfg",
        ):
            content = path.read_text()
            relative = path.relative_to(dst / "minios")
            assert content == (src / relative).read_text()
            assert "perchmode=raw" not in content
            assert "perchsize=2048" not in content
            assert "perchencrypt=luks" not in content

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
