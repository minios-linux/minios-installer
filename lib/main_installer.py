#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer
A graphical tool for installing MiniOS onto a disk via GTK.

Usage:
    main_installer.py

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
Original idea: FershoUno <https://github.com/FershoUno>
"""

import os
import sys
import gi
import gettext
import threading
import tempfile
import shutil
import subprocess
from typing import Optional

# Add lib directory to Python path
sys.path.insert(0, '/usr/lib/minios-installer')

# Import our library modules
from disk_utils import find_available_disks, get_disk_size_mib, start_disk_monitoring, stop_disk_monitoring, pause_disk_monitoring, resume_disk_monitoring
from mount_utils import mount_partition, unmount_partitions, force_unmount_device
from format_utils import format_partitions, check_filesystem_support, detect_filesystem_tools
from copy_utils import copy_minios_files, copy_efi_files, find_minios_source
from bootloader_utils import install_bootloader
from disk_utils import partition_disk, zero_fill_disk

gi.require_version('Gtk', '3.0')
gi.require_version('Gio', '2.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, GLib, Gio, Gdk, Pango

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
APPLICATION_ID   = 'org.minios.installer'
APP_NAME         = 'minios-installer'
APP_TITLE        = 'MiniOS Installer'
LOCALE_DIRECTORY = '/usr/share/locale'
CSS_FILE_PATH    = '/usr/share/minios-installer/style.css'

ICON_WINDOW      = 'usb-creator-gtk'
ICON_INSTALL     = 'document-save-symbolic'
ICON_CONFIGURE   = 'preferences-system-symbolic'

# ──────────────────────────────────────────────────────────────────────────────
# Internationalization
# ──────────────────────────────────────────────────────────────────────────────
gettext.bindtextdomain(APP_NAME, LOCALE_DIRECTORY)
gettext.textdomain(APP_NAME)
_ = gettext.gettext


def apply_css_if_exists():
    """
    Load and apply CSS if the file exists.
    """
    provider = Gtk.CssProvider()
    if os.path.exists(CSS_FILE_PATH):
        provider.load_from_path(CSS_FILE_PATH)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


# ──────────────────────────────────────────────────────────────────────────────
# InstallerWindow Definition
# ──────────────────────────────────────────────────────────────────────────────

class InstallerWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title=_(APP_TITLE))
        self.set_default_size(600, 450)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name(ICON_WINDOW)

        self.selected_device     = None
        self.selected_filesystem = None
        self.use_gpt             = False
        self.create_efi          = False
        self.boot_config_type    = "multilang"  # "multilang" or language code like "ru_RU"
        self.cancel_requested    = False

        self.p1 = None
        self.m1 = None
        self.config_path = None
        self.temp_config_path = None

        self.main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("set_margin_top","set_margin_bottom","set_margin_start","set_margin_end"):
            getattr(self.main_vbox, m)(12)
        self.add(self.main_vbox)

        # Apply CSS if available
        apply_css_if_exists()

        # Build the user interface
        self._build_header_bar()
        self._build_selection_ui()

        # Start disk monitoring
        start_disk_monitoring(self._refresh_disk_list)

        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, widget):
        stop_disk_monitoring(self._refresh_disk_list)
        self.get_application().quit()

    def _refresh_disk_list(self):
        selected_path = self.selected_device

        # Clear existing rows
        for child in self.disk_list.get_children():
            self.disk_list.remove(child)

        new_row_to_select = None
        # Repopulate the list
        for dev in find_available_disks():
            row = Gtk.ListBoxRow()
            row.set_margin_top(8)
            row.set_margin_bottom(8)
            row.set_margin_start(12)
            row.set_margin_end(12)
            
            # Main horizontal container
            main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            # Device icon
            img = Gtk.Image.new_from_gicon(Gio.ThemedIcon(name=dev['icon']), Gtk.IconSize.DND)
            main_box.pack_start(img, False, False, 0)
            
            # Device information container
            info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            info_box.set_hexpand(True)
            
            # Primary line: Device name and size
            primary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            
            device_label = Gtk.Label()
            device_label.set_markup(f'<b><span size="large" weight="regular">/dev/{GLib.markup_escape_text(dev["name"])}</span></b>')
            device_label.set_halign(Gtk.Align.START)
            primary_box.pack_start(device_label, False, False, 0)
            
            size_label = Gtk.Label()
            size_label.set_markup(f'<span size="medium" weight="bold">{GLib.markup_escape_text(dev["size"])}</span>')
            size_label.set_halign(Gtk.Align.CENTER)
            primary_box.pack_end(size_label, False, False, 0)
            
            info_box.pack_start(primary_box, False, False, 0)
            
            # Secondary line: Model and additional info
            secondary_info = []
            if dev['model']:
                secondary_info.append(GLib.markup_escape_text(dev['model']))
            
            transport = dev.get('transport', '')
            if transport and transport != 'non‑rotational':
                if transport == 'usb':
                    secondary_info.append(_("USB Device"))
                elif transport == 'sata':
                    secondary_info.append(_("SATA Drive"))
                elif transport == 'ata':
                    secondary_info.append(_("IDE Drive"))
                elif transport == 'nvme':
                    secondary_info.append(_("NVMe SSD"))
                elif transport == 'mmc':
                    secondary_info.append(_("MMC/SD Card"))
                elif transport == 'rotational':
                    secondary_info.append(_("Hard Disk Drive"))
                else:
                    secondary_info.append(transport.upper())
            
            if dev.get('serial'):
                secondary_info.append(f"S/N: {GLib.markup_escape_text(dev['serial'][:16])}{'...' if len(dev['serial']) > 16 else ''}")
            
            if secondary_info:
                secondary_label = Gtk.Label()
                secondary_text = " • ".join(secondary_info)
                secondary_label.set_markup(f'<span size="small" color="#666666">{secondary_text}</span>')
                secondary_label.set_halign(Gtk.Align.START)
                secondary_label.set_ellipsize(Pango.EllipsizeMode.END)
                info_box.pack_start(secondary_label, False, False, 0)
            
            main_box.pack_start(info_box, True, True, 0)
            
            # Warning indicator for system disks or mounted devices
            device_path = f"/dev/{dev['name']}"
            try:
                # Check if this might be a system disk or mounted device
                is_system = False
                is_mounted = False
                lsblk_output = subprocess.run(
                    ['lsblk', '-n', '-o', 'MOUNTPOINT', device_path],
                    capture_output=True, text=True, timeout=5
                ).stdout
                
                # Check for MiniOS system disk
                if '/run/initramfs/memory/data' in lsblk_output:
                    is_system = True
                # Check if any partition is mounted
                elif lsblk_output.strip() and not lsblk_output.strip() == '':
                    is_mounted = True
            except:
                is_system = False
                is_mounted = False
                
            if is_system:
                warning_icon = Gtk.Image.new_from_icon_name("dialog-warning", Gtk.IconSize.MENU)
                warning_icon.set_tooltip_text(_("Warning: This appears to be a system disk"))
                main_box.pack_start(warning_icon, False, False, 0)
            elif is_mounted:
                mounted_icon = Gtk.Image.new_from_icon_name("dialog-warning", Gtk.IconSize.MENU)
                mounted_icon.set_tooltip_text(_("Warning: This disk has mounted partitions"))
                main_box.pack_start(mounted_icon, False, False, 0)
            
            row.add(main_box)
            row.device = device_path
            self.disk_list.add(row)
            if row.device == selected_path:
                new_row_to_select = row

        self.disk_list.show_all()

        if new_row_to_select:
            self.disk_list.select_row(new_row_to_select)
        else:
            self.selected_device = None
            if hasattr(self, 'btn_install'):
                self._update_install_sensitive()

    # ──────────────────────────────────────────────────────────────────────────
    # UI construction
    # ──────────────────────────────────────────────────────────────────────────
    def _build_header_bar(self):
        header = Gtk.HeaderBar(show_close_button=True)
        header.props.title = _(APP_TITLE)
        self.set_titlebar(header)

    def _build_selection_ui(self):
        """
        Build disk list, filesystem combo, and Install button.
        """
        for child in self.main_vbox.get_children():
            self.main_vbox.remove(child)

        lbl = Gtk.Label(label=_("Please select a target disk and filesystem:"), xalign=0)
        lbl.set_margin_bottom(8)
        self.main_vbox.pack_start(lbl, False, False, 0)

        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        self.main_vbox.pack_start(hb, True, True, 0)

        # Disk list
        vb_disk = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vb_disk.pack_start(Gtk.Label(label=_("Select Target Disk:"), xalign=0), False, False, 0)

        self.disk_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.disk_list.connect("row-selected", self._on_disk_selected)

        self._refresh_disk_list()

        sw = Gtk.ScrolledWindow()
        sw.set_min_content_width(350)
        sw.set_min_content_height(200)
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.disk_list)
        vb_disk.pack_start(sw, True, True, 0)
        hb.pack_start(vb_disk, True, True, 0)

        # Filesystem combo and information
        vb_fs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vb_fs.pack_start(Gtk.Label(label=_("Select Filesystem Type:"), xalign=0), False, False, 0)

        hfs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.combo_fs = Gtk.ComboBoxText(hexpand=True)
        for fs in detect_filesystem_tools():
            self.combo_fs.append_text(fs)
        self.combo_fs.set_active(-1)
        self.combo_fs.connect("changed", self._on_fs_selected)
        hfs.pack_start(self.combo_fs, True, True, 0)

        info = Gtk.Image.new_from_icon_name("dialog-information", Gtk.IconSize.SMALL_TOOLBAR)
        eb = Gtk.EventBox()
        eb.add(info)
        eb.set_tooltip_markup(
            _("<b>ext4</b> (best choice)\n"
            "  + Stable and has journaling.\n"
            "  + Fast performance with large-file support.\n"
            "  - Not compatible with Windows, Mac and most hardware devices.\n\n"
            "<b>ext2</b>\n"
            "  + Minimal write overhead preserves flash lifespan.\n"
            "  + Simple structure is easy to recover.\n"
            "  - No journaling increases risk of data loss if unplugged.\n"
            "  - Not compatible with Windows, Mac and most hardware devices.\n\n"
            "<b>btrfs</b>\n"
            "  + Snapshots enable easy rollback.\n"
            "  + Built-in compression saves space.\n"
            "  - Complex configuration may be needed.\n"
            "  - Additional metadata can slow transfers on USB drives.\n"
            "  - Not compatible with Windows, Mac and most hardware devices.\n\n"
            "<b>FAT32</b>\n"
            "  + Universally readable by Windows, macOS, and Linux.\n"
            "  + No extra drivers or setup needed.\n"
            "  - 4 GiB file size limit.\n"
            "  - No journaling increases risk of data loss if unplugged.\n"
            "  - FUSE-based persistence reduce performance.\n\n"
            "<b>NTFS</b>\n"
            "  + Supports large files and has journaling.\n"
            "  + Native Windows support; fast read/write in Linux via ntfs3.\n"
            "  - Not compatible with Mac and most hardware devices.\n"
            "  - FUSE-based persistence reduce performance.\n\n")
        )
        hfs.pack_start(eb, False, False, 0)
        vb_fs.pack_start(hfs, False, False, 0)
        
        # Boot menu language selection (for both GRUB and SYSLINUX)
        boot_config_label = Gtk.Label(label=_("Boot Menu Language:"))
        boot_config_label.set_halign(Gtk.Align.START)
        boot_config_label.set_margin_top(12)
        vb_fs.pack_start(boot_config_label, False, False, 0)
        
        # Language selection dropdown
        boot_config_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        
        # Available languages from po files
        self.available_languages = [
            ("multilang", _("Multilingual menu")),
            ("en_US", "English"),
            ("ru_RU", "Русский"),
            ("de_DE", "Deutsch"), 
            ("es_ES", "Español"),
            ("fr_FR", "Français"),
            ("it_IT", "Italiano"),
            ("id_ID", "Bahasa Indonesia"),
            ("pt_BR", "Português (Brasil)"),
            ("pt_PT", "Português (Portugal)")
        ]
        
        self.language_combo = Gtk.ComboBoxText()
        for lang_code, lang_name in self.available_languages:
            self.language_combo.append(lang_code, lang_name)
        self.language_combo.set_active(0)  # Select multilingual by default
        self.language_combo.connect("changed", self._on_language_changed)
        boot_config_box.pack_start(self.language_combo, False, False, 0)
        
        vb_fs.pack_start(boot_config_box, False, False, 6)
        
        hb.pack_start(vb_fs, True, True, 0)

        # Install button
        self.btn_install = Gtk.Button(label=_("Install"))
        self.btn_install.get_style_context().add_class('suggested-action')
        self.btn_install.set_image(Gtk.Image.new_from_icon_name(ICON_INSTALL, Gtk.IconSize.BUTTON))
        self.btn_install.set_always_show_image(True)
        self.btn_install.set_sensitive(False)
        self.btn_install.connect("clicked", self._on_install_clicked)
        self.btn_install.set_margin_top(12)
        self.main_vbox.pack_start(self.btn_install, False, False, 0)

        # Button to launch configurator
        self.btn_cfg = Gtk.Button(label=_("Configure MiniOS before installation"))
        self.btn_cfg.get_style_context().add_class('suggested-action')
        self.btn_cfg.set_margin_top(0)
        self.btn_cfg.set_image(Gtk.Image.new_from_icon_name(ICON_CONFIGURE, Gtk.IconSize.BUTTON))
        self.btn_cfg.set_always_show_image(True)
        self.btn_cfg.connect("clicked", self._on_launch_configurator)
        self.main_vbox.pack_start(self.btn_cfg, False, False, 0)

        self.show_all()

    def _on_disk_selected(self, listbox, row):
        self.selected_device = getattr(row, 'device', None)
        self.selected_disk_info = None  # Store disk info for warning dialog
        try:
            size_mib = get_disk_size_mib(self.selected_device)
            self.use_gpt = (size_mib > 2_097_152)
        except Exception:
            self.use_gpt = False        
        # Save disk info for later use in warning dialog
        if row:
            dev_name = getattr(row, 'device', None)
            dev_short = dev_name.replace("/dev/", "") if dev_name else None
            for dev in find_available_disks():
                if dev.get('name') == dev_short:
                    size = dev.get('size', '')
                    model = dev.get('model', '')
                    serial = dev.get('serial', '')
                    desc = []
                    if model:
                        desc.append(model)
                    if size:
                        desc.append(size)
                    if serial:
                        desc.append(f"SN: {serial}")
                    self.selected_disk_info = {
                        'device': dev_name,
                        'desc': " — " + ", ".join(desc) if desc else ""
                    }
                    break
        else:
            self.selected_disk_info = None
        self._update_install_sensitive()

    def _on_fs_selected(self, combo):
        text = combo.get_active_text()
        self.selected_filesystem = text or None
        self.create_efi = bool(text and text != 'fat32')
        self._update_install_sensitive()

    def _on_language_changed(self, combo):
        """Handle boot menu language selection change (affects both GRUB and SYSLINUX)."""
        lang_code = combo.get_active_id()
        if lang_code is not None:
            self.boot_config_type = lang_code


    def _update_install_sensitive(self):
        if hasattr(self, 'btn_install'):
            ok = bool(self.selected_device and self.selected_filesystem)
            self.btn_install.set_sensitive(ok)

    def _on_install_clicked(self, button):
        # Show confirmation warning before proceeding
        self._show_erase_warning()

    def _show_erase_warning(self):
        # xgettext doesn't recognize f-strings, so we duplicate _(...) 
        # to ensure the string is extracted for translation
        _("WARNING: This action is irreversible!")
        _("Device information:")
        _("What will be erased:")
        
        # Remove all widgets from main_vbox
        for child in self.main_vbox.get_children():
            self.main_vbox.remove(child)

        # Use cached disk info if available
        disk_info = self.selected_disk_info or {}
        disk_label = disk_info.get('device') or _("selected device")
        disk_desc = disk_info.get('desc') or ""

        # Main container with spacing
        main_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        main_container.set_margin_top(20)
        main_container.set_margin_bottom(20)
        main_container.set_margin_start(20)
        main_container.set_margin_end(20)

        # Warning header with icon
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_halign(Gtk.Align.CENTER)
        
        warning_icon = Gtk.Image.new_from_icon_name("dialog-warning", Gtk.IconSize.LARGE_TOOLBAR)
        header_box.pack_start(warning_icon, False, False, 0)
        
        warning_label = Gtk.Label()
        warning_label.set_markup('<span size="x-large" weight="bold" foreground="red">' + _("WARNING: This action is irreversible!") + '</span>')
        warning_label.set_halign(Gtk.Align.CENTER)
        header_box.pack_start(warning_label, False, False, 0)
        
        main_container.pack_start(header_box, False, False, 0)

        # Device info section
        device_frame = Gtk.Frame()
        device_frame.set_label_align(0.5, 0.5)
        device_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        
        device_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        device_info_icon = Gtk.Image.new_from_icon_name("drive-harddisk", Gtk.IconSize.MENU)
        device_label_box.pack_start(device_info_icon, False, False, 0)
        device_label_widget = Gtk.Label()
        device_label_widget.set_markup('<b>' + _("Device information:") + '</b>')
        device_label_box.pack_start(device_label_widget, False, False, 0)
        device_frame.set_label_widget(device_label_box)
        
        device_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        device_content.set_margin_top(10)
        device_content.set_margin_bottom(10)
        device_content.set_margin_start(15)
        device_content.set_margin_end(15)
        
        device_info_label = Gtk.Label()
        device_info_label.set_markup(f'<span size="medium"><b>{GLib.markup_escape_text(disk_label)}</b>{GLib.markup_escape_text(disk_desc)}</span>')
        device_info_label.set_halign(Gtk.Align.START)
        device_content.pack_start(device_info_label, False, False, 0)
        
        device_frame.add(device_content)
        main_container.pack_start(device_frame, False, False, 0)

        # What will be erased section
        erase_frame = Gtk.Frame()
        erase_frame.set_label_align(0.5, 0.5)
        erase_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        
        erase_label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        erase_icon = Gtk.Image.new_from_icon_name("edit-delete", Gtk.IconSize.MENU)
        erase_label_box.pack_start(erase_icon, False, False, 0)
        erase_label_widget = Gtk.Label()
        erase_label_widget.set_markup('<b>' + _("What will be erased:") + '</b>')
        erase_label_box.pack_start(erase_label_widget, False, False, 0)
        erase_frame.set_label_widget(erase_label_box)
        
        erase_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        erase_content.set_margin_top(10)
        erase_content.set_margin_bottom(10)
        erase_content.set_margin_start(15)
        erase_content.set_margin_end(15)
        
        # Risk items with icons
        risk_items = [
            ("applications-office", _("Files, documents, and applications")),
            ("applications-system", _("Operating systems")),
            ("user-home", _("Personal data and settings")),
            ("drive-harddisk", _("All partitions and file systems"))
        ]
        
        for icon_name, description in risk_items:
            item_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            item_icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            item_box.pack_start(item_icon, False, False, 0)
            item_label = Gtk.Label(label=description)
            item_label.set_halign(Gtk.Align.START)
            item_box.pack_start(item_label, False, False, 0)
            erase_content.pack_start(item_box, False, False, 0)
        
        erase_frame.add(erase_content)
        main_container.pack_start(erase_frame, False, False, 0)

        # Add main container to scrolled window for better handling
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(main_container)
        self.main_vbox.pack_start(scrolled, True, True, 0)

        # Button box
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        btn_box.set_homogeneous(True)
        btn_box.set_margin_top(10)
        btn_box.set_margin_bottom(10)

        btn_cancel = Gtk.Button(label=_("Go Back"))
        btn_cancel.get_style_context().add_class('suggested-action')
        btn_cancel.connect("clicked", lambda b: self._build_selection_ui())
        btn_box.pack_start(btn_cancel, True, True, 0)

        btn_continue = Gtk.Button(label=_("Continue"))
        btn_continue.get_style_context().add_class('destructive-action')
        btn_continue.connect("clicked", self._on_confirm_install)
        btn_box.pack_start(btn_continue, True, True, 0)

        self.main_vbox.pack_start(btn_box, False, False, 0)
        self.show_all()

    def _on_confirm_install(self, button):
        pause_disk_monitoring()
        self._build_progress_ui()
        threading.Thread(target=self._run_install_sequence, daemon=True).start()

    def _build_progress_ui(self):
        for child in self.main_vbox.get_children():
            self.main_vbox.remove(child)

        self.lbl_status = Gtk.Label(label=_("Preparing to install..."), xalign=0)
        self.lbl_status.set_line_wrap(True)
        self.lbl_status.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.main_vbox.pack_start(self.lbl_status, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.set_fraction(0.0)
        self.main_vbox.pack_start(self.progress, False, False, 0)

        # Toggle log view button
        self.btn_toggle = Gtk.ToggleButton(label=_("Show Log"))
        self.btn_toggle.connect("toggled", self._on_toggle_log)
        self.main_vbox.pack_start(self.btn_toggle, False, False, 0)

        # Cancel button
        self.btn_cancel = Gtk.Button(label=_("Cancel"))
        self.btn_cancel.get_style_context().add_class('destructive-action')
        self.btn_cancel.set_sensitive(True)
        self.btn_cancel.connect("clicked", self._on_cancel)
        self.main_vbox.pack_end(self.btn_cancel, False, False, 0)

        # Log view
        self.log_buf = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buf)
        self.log_view.set_editable(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_monospace(True)

        self.log_sw = Gtk.ScrolledWindow()
        self.log_sw.set_min_content_height(200)
        self.log_sw.add(self.log_view)

        self.show_all()

    def _on_toggle_log(self, toggle):
        if toggle.get_active():
            toggle.set_label(_("Hide Log"))
            self.main_vbox.pack_start(self.log_sw, True, True, 0)
            self.log_sw.show_all()
        else:
            toggle.set_label(_("Show Log"))
            self.main_vbox.remove(self.log_sw)

    def _on_cancel(self, button):
        if not self.cancel_requested:
            self.cancel_requested = True

            # reset progress/bar
            GLib.idle_add(self.progress.set_fraction, 0.0)
            GLib.idle_add(self.lbl_status.set_text, _("Installation canceled."))
            GLib.idle_add(self._append_log, _("Installation canceled by user."))

            # Change Cancel to Restart button
            self._setup_restart_button()

    def _setup_restart_button(self):
        self.btn_cancel.set_label(_("Restart Installation"))
        self.btn_cancel.get_style_context().remove_class('destructive-action')
        self.btn_cancel.get_style_context().add_class('suggested-action')
        try:
            self.btn_cancel.disconnect_by_func(self._on_cancel)
        except TypeError:
            # This may happen if the handler is already disconnected
            pass
        self.btn_cancel.connect("clicked", lambda b: self._build_selection_ui())
        self.btn_cancel.set_sensitive(True)

    def _report_progress(self, percent: int, message: str):
        fraction = percent / 100.0
        GLib.idle_add(self.progress.set_fraction, fraction)
        GLib.idle_add(self.lbl_status.set_text, message)
        GLib.idle_add(self._append_log, message)

    def _append_log(self, message: str):
        timestamp = GLib.DateTime.new_now_local().format("%Y-%m-%d %H:%M:%S")
        text = f"[{timestamp}] {message}\n"
        end_iter = self.log_buf.get_end_iter()
        self.log_buf.place_cursor(end_iter)
        self.log_buf.insert_at_cursor(text)

    def _log_async(self, message: str):
        # Always schedule _append_log on the GTK main loop
        GLib.idle_add(self._append_log, message)

    def _run_install_sequence(self):
        try:
            dev = self.selected_device
            fs  = self.selected_filesystem
            use_gpt = self.use_gpt
            if dev.startswith("/dev/nvme") or dev.startswith("/dev/mmcblk"):
                # For NVMe and MMC devices, use partition names like nvme0n1p1
                p1, p2 = f"{dev}p1", f"{dev}p2"
            else:
                p1, p2 = f"{dev}1", f"{dev}2"
            m1 = f"/mnt/install/{os.path.basename(p1)}"
            m2 = f"/mnt/install/{os.path.basename(p2)}" if self.create_efi else None

            self.p1 = p1
            self.m1 = m1
            # Use temporary config if available, otherwise use standard
            config_override = self.temp_config_path if (self.temp_config_path and os.path.exists(self.temp_config_path)) else None
            self.config_path = config_override or '/etc/live/config.conf'

            steps = [
                ( 0,  _("Unmounting disk..."),       lambda: unmount_partitions(p1, p2, m1, m2)),
                ( 2,  _("Erasing disk..."),          lambda: zero_fill_disk(dev)),
                ( 4,  _("Partitioning disk..."),     lambda: partition_disk(dev, fs, use_gpt)),
                ( 8,  _("Formatting partitions..."), lambda: format_partitions(p1, fs, p2 if self.create_efi else None)),
                (15,  _("Mounting partition..."),    lambda: mount_partition(p1, m1)),
            ]

            for percent, message, func in steps:
                if self.cancel_requested:
                    return
                self._report_progress(percent, message)
                try:
                    func()
                except Exception as e:
                    if self.cancel_requested:
                        return
                    import traceback
                    tb = traceback.format_exc()
                    GLib.idle_add(self._append_log, _("Error at step: ") + message)
                    GLib.idle_add(self._append_log, tb)
                    GLib.idle_add(self._show_error, _("Installation failed: ") + str(e))
                    return

            if self.create_efi and not self.cancel_requested:
                self._report_progress(18, _("Mounting EFI partition..."))
                try:
                    mount_partition(p2, m2)
                except Exception as e:
                    if self.cancel_requested:
                        return
                    import traceback
                    tb = traceback.format_exc()
                    GLib.idle_add(self._append_log, _("Error mounting EFI partition:"))
                    GLib.idle_add(self._append_log, tb)
                    GLib.idle_add(self._show_error, _("Installation failed: ") + str(e))
                    return

            if not self.cancel_requested:
                self._report_progress(18, _("Copying files..."))
                src = find_minios_source()
                if not src:
                    if not self.cancel_requested:
                        GLib.idle_add(self._show_error, _("Cannot find MiniOS image."))
                    return
                try:
                    copy_minios_files(src, m1, self._report_progress, self._log_async, config_override, self.boot_config_type)
                    if not self.create_efi:
                        self._report_progress(50, _("Copying EFI files to root..."))
                        copy_efi_files(src, m1, self._log_async)
                    elif self.create_efi:
                        self._report_progress(50, _("Copying EFI files to ESP..."))
                        copy_efi_files(src, m2, self._log_async)
                except Exception as e:
                    if self.cancel_requested:
                        return
                    import traceback
                    tb = traceback.format_exc()
                    GLib.idle_add(self._append_log, _("Error during file copy:"))
                    GLib.idle_add(self._append_log, tb)
                    GLib.idle_add(self._show_error, _("Installation failed: ") + str(e))
                    return

            if not use_gpt and fs != 'exfat' and not self.cancel_requested:
                self._report_progress(96, _("Setting up bootloader..."))
                try:
                    install_bootloader(
                        dev, p1,
                        p2 if self.create_efi else None,
                        self._report_progress,
                        self._log_async
                    )
                except Exception as e:
                    if self.cancel_requested:
                        return
                    import traceback
                    tb = traceback.format_exc()
                    GLib.idle_add(self._append_log, _("Error setting up bootloader:"))
                    GLib.idle_add(self._append_log, tb)
                    GLib.idle_add(self._show_error, _("Installation failed: ") + str(e))
                    return

            if not self.cancel_requested:
                self._report_progress(98, _("Unmounting disk..."))
                try:
                    unmount_partitions(p1, p2, m1, m2)
                except Exception as e:
                    if self.cancel_requested:
                        return
                    import traceback
                    tb = traceback.format_exc()
                    GLib.idle_add(self._append_log, _("Error during unmount:"))
                    GLib.idle_add(self._append_log, tb)
                    GLib.idle_add(self._show_error, _("Installation failed: ") + str(e))
                    return
                self._report_progress(100, _("Installation complete!"))
                # Change Cancel to Restart button
                GLib.idle_add(self._setup_restart_button)
        finally:
            resume_disk_monitoring()

    def _show_error(self, message: str):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Installation Error")
        )
        dlg.format_secondary_text(message)
        dlg.run()
        dlg.destroy()

    def _on_launch_configurator(self, button):
        """
        Copy the standard config to a temp file and launch the configurator with it.
        """
        src_config = '/etc/live/config.conf'
        if not os.path.exists(src_config):
            dlg = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Config file not found")
            )
            dlg.format_secondary_text(_("Standard config file not found at {}.").format(src_config))
            dlg.run()
            dlg.destroy()
            return

        fd, temp_path = tempfile.mkstemp(prefix="minios-config-", suffix=".conf")
        os.close(fd)
        shutil.copy2(src_config, temp_path)
        self.temp_config_path = temp_path

        try:
            subprocess.Popen(['minios-configurator', temp_path])
        except Exception as e:
            dlg = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Launch Error")
            )
            dlg.format_secondary_text(str(e))
            dlg.run()
            dlg.destroy()
            return


# ──────────────────────────────────────────────────────────────────────────────
# Application Class and Entry Point
# ──────────────────────────────────────────────────────────────────────────────

class MiniOSInstallerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID)
        self.window = None

    def do_activate(self):
        if self.window:
            self.window.present()
        else:
            if os.geteuid() != 0:
                dlg = Gtk.MessageDialog(
                    modal=True,
                    destroy_with_parent=False,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text=_("Root Privileges Required")
                )
                dlg.format_secondary_text(_("This installer must be run as root."))
                dlg.run()
                dlg.destroy()
                sys.exit(1)

            self.window = InstallerWindow(self)
            self.window.show_all()
            self.window.present()


def main():
    try:
        app = MiniOSInstallerApp()
        return app.run(sys.argv)
    except KeyboardInterrupt:
        return 130

if __name__ == '__main__':
    sys.exit(main())
