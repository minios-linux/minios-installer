#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Copy Utilities
Utilities for copying MiniOS files and EFI files.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import shutil
import gettext
import re
from typing import Optional, Callable, Dict

# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


def copy_minios_files(src: str, dst: str, progress_cb: Callable, log_cb: Callable, 
                     config_override: Optional[str] = None, boot_config_type: str = "multilang") -> None:
    """
    Copy MiniOS files from src to dst with progress reporting.
    """
    # Calculate total size for progress reporting
    total = _calculate_copy_size(src)
    copied = 0

    # Get reference to the owner object for cancellation checking
    owner = getattr(progress_cb, "__self__", None)

    entries = []

    # 1) Main tree → minios/
    for root, dirs, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), src)
            if rel.startswith('changes/'):
                continue
            entries.append((os.path.join('minios', rel), os.path.join(root, fn)))

    # 2) .disk/info
    with open('/tmp/info', 'w', encoding='utf-8') as f:
        f.write('MiniOS')
    entries.append(('.disk/info', '/tmp/info'))

    # 3) config.conf
    config_dst = 'minios/config.conf'
    if config_override and os.path.exists(config_override):
        entries.append((config_dst, config_override))
    else:
        config_src = '/etc/live/config.conf'
        if os.path.exists(config_src):
            entries.append((config_dst, config_src))

    for rel, path in entries:
        if owner and owner.cancel_requested:
            log_cb(_("Installation canceled by user."))
            raise RuntimeError(_("Installation canceled by user."))

        dest = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # Report copying start, then perform copy, then log completion
        size = os.path.getsize(path)
        # Calculate progress including this file
        percent = int(18 + (78 * (copied + size) / total))
        progress_cb(percent, _("Copying MiniOS files: ") + rel)
        shutil.copy2(path, dest)
        log_cb(_("Copied file: ") + path)
        copied += size

    # Create required directories
    for sub in ('boot', 'modules', 'changes', 'scripts'):
        p = os.path.join(dst, 'minios', sub)
        os.makedirs(p, exist_ok=True)
        log_cb(_("Created directory: ") + p)

    # Handle GRUB configuration selection
    _process_grub_config(dst, boot_config_type, log_cb)

    # Handle SYSLINUX configuration selection  
    _process_syslinux_config(dst, boot_config_type, log_cb)


def copy_efi_files(src: str, dst: str, log_cb: Callable) -> None:
    """
    Copy EFI files from src/boot/EFI → dst/EFI/...
    """
    efi_dir = os.path.join(src, 'boot', 'EFI')
    if not os.path.isdir(efi_dir):
        return

    for root, dirs, files in os.walk(efi_dir):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), efi_dir)
            src_path = os.path.join(root, fn)
            dest_path = os.path.join(dst, 'EFI', rel)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(src_path, dest_path)
            log_cb(_("Copied EFI file: ") + src_path)


def find_minios_source() -> Optional[str]:
    """
    Find the MiniOS source directory from common locations.
    Returns the path if found, None otherwise.
    """
    # Check both livekit and dracut initramfs paths
    candidates = [
        "/run/initramfs/memory/data/minios",
        "/run/initramfs/memory/iso/minios",
        "/lib/live/mount/medium/minios",
        "/lib/live/mount/iso/minios"
    ]

    def get_boot_files_from_cmdline():
        """Extract vmlinuz and initramfs filenames from /proc/cmdline"""
        try:
            with open('/proc/cmdline', 'r') as f:
                cmdline = f.read().strip()

            vmlinuz_file = None
            initramfs_file = None

            # Parse boot parameters (support multiple formats)
            for param in cmdline.split():
                # GRUB: BOOT_IMAGE= parameter contains kernel path
                if param.startswith('BOOT_IMAGE='):
                    boot_path = param.split('=', 1)[1]
                    # Extract filename from path like /minios/boot/vmlinuz-version
                    vmlinuz_file = os.path.basename(boot_path)
                # SYSLINUX: linux= parameter
                elif param.startswith('linux='):
                    boot_path = param.split('=', 1)[1]
                    vmlinuz_file = os.path.basename(boot_path)
                # initrd parameter
                elif param.startswith('initrd='):
                    initrd_path = param.split('=', 1)[1]
                    # Extract filename from path like /minios/boot/initrfs-version.img
                    initramfs_file = os.path.basename(initrd_path)

            # If we found kernel but no initramfs, try to guess initramfs name
            if vmlinuz_file and not initramfs_file:
                # Convert vmlinuz-version to initrfs-version.img
                if vmlinuz_file.startswith('vmlinuz-'):
                    version = vmlinuz_file[8:]  # Remove 'vmlinuz-' prefix
                    initramfs_file = f'initrfs-{version}.img'
                elif vmlinuz_file.startswith('vmlinuz'):
                    # Handle generic vmlinuz case
                    initramfs_file = 'initrd.img'

            return vmlinuz_file, initramfs_file
        except (IOError, OSError):
            return None, None

    for candidate in candidates:
        if os.path.isdir(candidate):
            boot_dir = os.path.join(candidate, "boot")
            if os.path.isdir(boot_dir):
                try:
                    boot_files = os.listdir(boot_dir)

                    # First check for generic vmlinuz and initramfs files
                    if "vmlinuz" in boot_files:
                        return candidate

                    # If no generic files, check for files matching the currently loaded kernel from /proc/cmdline
                    cmdline_vmlinuz, cmdline_initramfs = get_boot_files_from_cmdline()
                    if (cmdline_vmlinuz and cmdline_vmlinuz in boot_files and
                        cmdline_initramfs and cmdline_initramfs in boot_files):
                        return candidate

                    # Fallback: if cmdline parsing failed, look for any versioned kernel files
                    # This handles cases where we're not running from MiniOS live environment
                    has_vmlinuz = any(f.startswith("vmlinuz") for f in boot_files)
                    has_initramfs = any(f.startswith("initrfs") or f.startswith("initrd") for f in boot_files)
                    if has_vmlinuz and has_initramfs:
                        return candidate

                except (OSError, PermissionError):
                    continue

    return None


def _calculate_copy_size(src: str) -> int:
    """
    Calculate total size of files to be copied.
    """
    total = 0
    for root, dirs, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), src)
            if rel.startswith('changes/'):
                continue
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except:
                pass
    return total


def _remove_live_config_params(content: str) -> str:
    """
    Remove live-config parameters (locales, timezone, keyboard-layouts) from boot config.
    These parameters should be in minios/config.conf instead of boot loader configs
    when installing to disk.
    """
    # Remove locales parameter
    content = re.sub(r'\s+locales=[^\s]+', '', content)
    # Remove timezone parameter
    content = re.sub(r'\s+timezone=[^\s]+', '', content)
    # Remove keyboard-layouts parameter
    content = re.sub(r'\s+keyboard-layouts=[^\s]+', '', content)
    return content


def _parse_po_file(po_path: str) -> Dict[str, str]:
    """
    Parse a .po file and return a dictionary of msgid -> msgstr mappings.
    """
    translations = {}

    if not os.path.exists(po_path):
        return translations

    try:
        with open(po_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Find all msgid/msgstr pairs using regex
        pattern = r'msgid\s+"([^"]*(?:\\.[^"]*)*?)"\s*msgstr\s+"([^"]*(?:\\.[^"]*)*?)"'
        matches = re.findall(pattern, content, re.MULTILINE | re.DOTALL)

        for msgid, msgstr in matches:
            if msgid and msgstr:  # Skip empty translations
                # Unescape the strings
                msgid = msgid.replace('\\"', '"').replace('\\n', '\n')
                msgstr = msgstr.replace('\\"', '"').replace('\\n', '\n')
                translations[msgid] = msgstr

    except Exception:
        pass  # Return empty dict on error

    return translations


def _generate_localized_grub_config(grub_dir: str, lang_code: str, grub_cfg_path: str, log_cb: Callable) -> bool:
    """
    Generate a localized GRUB configuration using po files.
    Removes live-config parameters (locales/timezone/keyboard-layouts)
    as they will be in minios/config.conf instead.
    Writes directly to grub.cfg. Returns True if successful.
    """
    # Parse po file for the language
    po_path = os.path.join(grub_dir, 'po', f'{lang_code}.po')
    translations = _parse_po_file(po_path)

    if not translations:
        log_cb(_("Warning: No translations found for {lang}, using English fallback").format(lang=lang_code))
        return False

    # Get template from english config (has the structure we want)
    template_cfg_path = os.path.join(grub_dir, 'grub.template.cfg')
    if not os.path.exists(template_cfg_path):
        log_cb(_("Error: grub.template.cfg template not found"))
        return False

    try:
        with open(template_cfg_path, 'r', encoding='utf-8') as f:
            template_content = f.read()

        # Apply translations to the template
        localized_content = template_content

        # Define the menu entries to translate
        menu_entries = {
            "Resume previous session": "resume",
            "Start a new session": "newsession", 
            "Choose session during startup": "choosesession",
            "Fresh start": "freshstart",
            "Copy to RAM": "copyram",
            "Loading kernel and ramdisk...": "loading",
            "MiniOS": "OS"
        }

        # Replace English menu text with localized versions
        for english_text, var_name in menu_entries.items():
            if english_text in translations:
                localized_text = translations[english_text]
                # Replace the menuentry labels
                localized_content = localized_content.replace(f'menuentry "{english_text}"', f'menuentry "{localized_text}"')
                # Replace variable assignments if they exist
                localized_content = re.sub(f'set {var_name}="{re.escape(english_text)}"', 
                                         f'set {var_name}="{localized_text}"', localized_content)

        # Set localized theme if available
        theme_path = f'/minios/boot/grub/minios-theme/theme_{lang_code}.txt'
        if os.path.exists(os.path.join(grub_dir, 'minios-theme', f'theme_{lang_code}.txt')):
            # Replace theme setting with localized version
            localized_content = re.sub(
                r'set theme=/minios/boot/grub/minios-theme/theme\.txt',
                f'set theme={theme_path}',
                localized_content
            )
            log_cb(_("Using localized theme: {theme}").format(theme=f'theme_{lang_code}.txt'))
        else:
            log_cb(_("Localized theme not found for {lang}, using default").format(lang=lang_code))

        # Remove live-config parameters (will be in minios/config.conf)
        localized_content = _remove_live_config_params(localized_content)

        # Write localized config directly to grub.cfg
        with open(grub_cfg_path, 'w', encoding='utf-8') as f:
            f.write(localized_content)

        log_cb(_("Generated localized GRUB config for {lang}").format(lang=lang_code))
        return True

    except Exception as e:
        log_cb(_("Error generating localized GRUB config: {error}").format(error=str(e)))
        return False



def _process_syslinux_config(dst: str, config_type: str, log_cb: Callable) -> None:
    """
    Process SYSLINUX boot configuration:
    - For multilang: Keep default syslinux.cfg (multilingual support)
    - For specific language: Use localized config from lang/ directory,
      removing live-config parameters (locales/timezone/keyboard-layouts)
      as they will be in minios/config.conf instead
    """
    boot_dir = os.path.join(dst, 'minios', 'boot')
    syslinux_dir = os.path.join(boot_dir, 'syslinux')
    syslinux_cfg_path = os.path.join(syslinux_dir, 'syslinux.cfg')

    if not os.path.exists(syslinux_dir):
        log_cb(_("SYSLINUX directory not found, skipping SYSLINUX configuration"))
        return

    if config_type == "multilang":
        # Use multilingual configuration if available
        multilang_cfg = os.path.join(syslinux_dir, 'syslinux.multilang.cfg')
        if os.path.exists(multilang_cfg):
            try:
                shutil.copy2(multilang_cfg, syslinux_cfg_path)
                log_cb(_("Using multilingual SYSLINUX menu"))
            except Exception as e:
                log_cb(_("Error copying multilingual SYSLINUX config: ") + str(e))
        else:
            log_cb(_("Using multilingual SYSLINUX menu"))
        return

    # For specific language codes, try to use localized configuration
    lang_dir = os.path.join(syslinux_dir, 'lang')
    if os.path.exists(lang_dir):
        localized_cfg = os.path.join(lang_dir, f"{config_type}.cfg")

        if os.path.exists(localized_cfg):
            # Read, clean, and write localized version
            try:
                with open(localized_cfg, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Remove live-config parameters (will be in minios/config.conf)
                content = _remove_live_config_params(content)

                with open(syslinux_cfg_path, 'w', encoding='utf-8') as f:
                    f.write(content)

                log_cb(_("Using localized SYSLINUX configuration for: ") + config_type)
            except Exception as e:
                log_cb(_("Error processing localized SYSLINUX config: ") + str(e))
        else:
            # Fallback to English if localized version not found
            english_cfg = os.path.join(lang_dir, "en_US.cfg")
            if os.path.exists(english_cfg):
                try:
                    with open(english_cfg, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # Remove live-config parameters (will be in minios/config.conf)
                    content = _remove_live_config_params(content)

                    with open(syslinux_cfg_path, 'w', encoding='utf-8') as f:
                        f.write(content)

                    log_cb(_("Localized SYSLINUX config not found for ") + config_type + _(", using English"))
                except Exception as e:
                    log_cb(_("Error processing English SYSLINUX config: ") + str(e))
            else:
                log_cb(_("Warning: No localized SYSLINUX configurations found"))
    else:
        log_cb(_("SYSLINUX lang directory not found, keeping default configuration"))


def _process_grub_config(dst: str, config_type: str, log_cb: Callable) -> None:
    """
    Process GRUB boot menu language selection:
    - For multilang: Copy grub.multilang.cfg to grub.cfg 
    - For specific language: Generate localized config and copy to grub.cfg
    """
    grub_dir = os.path.join(dst, 'minios', 'boot', 'grub')

    if not os.path.exists(grub_dir):
        log_cb(_("GRUB directory not found, skipping GRUB boot menu configuration"))
        return

    grub_cfg_path = os.path.join(grub_dir, 'grub.cfg')

    if config_type == "multilang":
        # Use multilingual config
        source_cfg_path = os.path.join(grub_dir, 'grub.multilang.cfg')
        config_name = _("multilingual menu")

        if os.path.exists(source_cfg_path):
            shutil.copy2(source_cfg_path, grub_cfg_path)
            log_cb(_("Applied {config} GRUB boot menu from {source}").format(
                config=config_name, source=os.path.basename(source_cfg_path)))
        else:
            log_cb(_("Warning: {config} configuration file not found, keeping original grub.cfg").format(
                config=config_name))

    else:
        # Generate localized config for specific language
        lang_code = config_type
        log_cb(_("Generating localized GRUB menu for language: {lang}").format(lang=lang_code))

        if _generate_localized_grub_config(grub_dir, lang_code, grub_cfg_path, log_cb):
            log_cb(_("Applied {lang} GRUB boot menu").format(lang=lang_code))
        else:
            # Fallback to template if localized generation failed
            log_cb(_("Failed to generate {lang} config, falling back to template").format(lang=lang_code))
            template_cfg_path = os.path.join(grub_dir, 'grub.template.cfg')
            if os.path.exists(template_cfg_path):
                shutil.copy2(template_cfg_path, grub_cfg_path)
                log_cb(_("Applied template GRUB boot menu as fallback"))
            else:
                log_cb(_("Warning: No fallback configuration available, keeping original grub.cfg"))
