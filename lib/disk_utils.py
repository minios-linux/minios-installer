#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Disk Utilities
Utilities for disk detection, sizing, and partitioning operations.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import os
import re
import stat
import subprocess
import gettext
import json
from typing import List, Dict, Optional, Callable
from command_utils import run_command


# Set up gettext for localization
gettext.bindtextdomain('minios-installer', '/usr/share/locale')
gettext.textdomain('minios-installer')
_ = gettext.gettext


NATIVE_KERNEL_MANIFEST_PATH = '/usr/share/minios/kernel-dpkg/manifest.json'


def _json_contract(path: str):
    try:
        with open(path, 'r', encoding='utf-8', errors='strict') as stream:
            value = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def native_install_supported(source_mount: Optional[str] = None,
                             kernel_manifest_path: str = NATIVE_KERNEL_MANIFEST_PATH) -> bool:
    """Return whether the booted live image advertises native-install contracts.

    Outside a detected MiniOS live session this stays permissive so development,
    tests, and command help are not tied to host filesystem contents. On actual
    live media, both the format-1 kernel registration data and the verified EFI
    architecture contract are required before native/full mode is exposed.
    """
    if source_mount is None:
        try:
            source_mount = get_live_source_mount()
        except RuntimeError:
            return True

    kernel = _json_contract(kernel_manifest_path)
    if not kernel or kernel.get('format') != 1:
        return False

    efi = _json_contract(os.path.join(source_mount, 'minios', 'boot', 'efi-manifest.json'))
    if not efi or efi.get('format') != 1 or efi.get('layout') != 'dual-architecture-esp':
        return False
    architectures = efi.get('architectures')
    return (isinstance(architectures, dict) and
            isinstance(architectures.get('x64'), dict) and
            isinstance(architectures.get('ia32'), dict))


def normalize_device_path(device: str) -> str:
    if not device:
        return device
    return device if device.startswith('/dev/') else f'/dev/{device}'


def parent_block_device_name(name: str) -> str:
    """
    Map a partition node name to its parent disk name.

    Handles sda1, nvme0n1p2, mmcblk0p1. Whole-disk names (sda, nvme0n1, sr0)
    are returned unchanged.
    """
    if not name:
        return name
    # NVMe / MMC / loop / nbd partitions use a 'p' separator before the number.
    match = re.match(r'^(nvme\d+n\d+|mmcblk\d+|nbd\d+|loop\d+)p\d+$', name)
    if match:
        return match.group(1)
    # Classic SCSI/SATA/VirtIO partitions: sda1, vda2, xvda3, hda1.
    match = re.match(r'^((?:sd|vd|xvd|hd)[a-z]+)\d+$', name)
    if match:
        return match.group(1)
    return name


def partition_device_path(device: str, index: int) -> str:
    """Build a partition node path for *device* and 1-based *index*."""
    device = normalize_device_path(device)
    if device.startswith('/dev/disk/by-id/'):
        return f'{device}-part{index}'
    name = os.path.basename(device)
    if re.match(r'^(nvme\d+n\d+|mmcblk\d+|nbd\d+|loop\d+)$', name):
        return f'{device}p{index}'
    return f'{device}{index}'


def _lsblk_type(device: str) -> str:
    try:
        # -d: only the named node (avoid multi-line TYPE for disk+partitions).
        out = run_command(
            ['lsblk', '-dn', '-o', 'TYPE', device],
            _('Failed to detect device type')
        ).strip()
        return out.splitlines()[0].strip() if out else ''
    except Exception:
        return ''


def get_live_source_mount() -> str:
    for path in (
        '/run/initramfs/memory/data',
        '/run/initramfs/memory/iso',
        '/lib/live/mount/medium',
        '/lib/live/mount/iso',
    ):
        if os.path.exists(path):
            return path
    raise RuntimeError(_('No live media path found'))


def get_live_root_disk() -> str:
    src_path = get_live_source_mount()
    root_src = run_command(
        ['findmnt', '-n', '-o', 'SOURCE', src_path],
        _('Failed to detect live media device')
    ).strip().split('[', 1)[0]
    if not root_src.startswith('/dev/'):
        raise RuntimeError(_('Live media source is not a block device: ') + root_src)

    def verified(path: str) -> str:
        path = normalize_device_path(path)
        if not os.path.exists(path) or not stat.S_ISBLK(os.stat(path).st_mode):
            raise RuntimeError(_('Live media source is not a block device: ') + path)
        return path

    pkname_output = run_command(
        ['lsblk', '-n', '-o', 'PKNAME', root_src],
        _('Failed to detect root disk')
    )
    pknames = {line.strip() for line in pkname_output.splitlines() if line.strip()}
    if len(pknames) > 1:
        raise RuntimeError(_('Live media source has ambiguous parent disks.'))
    if pknames:
        parent = normalize_device_path(next(iter(pknames)))
        if os.path.realpath(parent) != os.path.realpath(root_src):
            return verified(parent)

    # Whole-disk media (CD/DVD/ISO, USB image without partition parent) must not be
    # mangled by digit stripping: /dev/sr0 would become /dev/sr.
    try:
        dev_type = run_command(
            ['lsblk', '-dn', '-o', 'TYPE', root_src],
            _('Failed to detect live media device type')
        ).strip().splitlines()[0].strip()
    except Exception:
        dev_type = ''
    if dev_type in ('disk', 'rom'):
        return verified(root_src)

    name = os.path.basename(root_src)
    parent = parent_block_device_name(name)
    if parent != name:
        return verified(parent)
    return verified(root_src)


def get_device_by_id_path(device: str) -> Optional[str]:
    """
    Return a stable /dev/disk/by-id/* path for *device* when available.

    Prefers WWN / NVMe / ATA / USB identifiers; skips partition symlinks.
    """
    device = normalize_device_path(device)
    by_id_dir = '/dev/disk/by-id'
    if not os.path.isdir(by_id_dir):
        return None
    try:
        real = os.path.realpath(device)
    except OSError:
        real = device

    candidates = []
    try:
        names = os.listdir(by_id_dir)
    except OSError:
        return None
    for name in names:
        # Skip partition links (…-part1) and dm-uuid noise when possible.
        if '-part' in name or name.startswith('dm-'):
            continue
        path = os.path.join(by_id_dir, name)
        try:
            if os.path.realpath(path) == real:
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return None
    for prefix in ('wwn-', 'nvme-eui.', 'nvme-', 'ata-', 'usb-', 'scsi-', 'mmc-'):
        for path in candidates:
            if os.path.basename(path).startswith(prefix):
                return path
    return sorted(candidates)[0]


def get_device_identity(device: str) -> Dict[str, str]:
    """
    Snapshot identifying attributes for the selected install target.

    Used to refuse install if USB reordering rebinds the same /dev/sdX name
    to a different physical disk between selection and wipe.
    """
    device = normalize_device_path(device)
    identity = {
        'path': device,
        'by_id': get_device_by_id_path(device) or '',
        'serial': '',
        'model': '',
        'size': '',
    }
    try:
        out_p = run_command(
            ['lsblk', '-P', '-dn', '-o', 'SERIAL,MODEL,SIZE', device],
            _('Failed to read device identity'),
        ).strip()
        props = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', out_p)}
        identity['serial'] = (props.get('serial') or '').strip()
        identity['model'] = (props.get('model') or '').strip()
        identity['size'] = (props.get('size') or '').strip()
    except Exception:
        pass
    return identity


def resolve_install_device(device: str, expected_identity: Optional[Dict[str, str]] = None) -> str:
    """
    Resolve a safe install path, preferring by-id, and verify *expected_identity*.

    Returns a path suitable for destructive tools (by-id when possible).
    """
    # Prefer stable by-id from selection time if it still exists.
    preferred = None
    if expected_identity and expected_identity.get('by_id'):
        by_id = expected_identity['by_id']
        if os.path.exists(by_id):
            preferred = by_id

    candidate = preferred or device
    # If candidate is by-id, normalize via realpath for type checks, but return preferred.
    try:
        kernel_path = normalize_device_path(os.path.realpath(candidate))
    except OSError:
        kernel_path = normalize_device_path(candidate)

    safe = ensure_safe_target_device(kernel_path)

    if expected_identity:
        current = get_device_identity(safe)
        exp_by_id = (expected_identity.get('by_id') or '').strip()
        exp_serial = (expected_identity.get('serial') or '').strip()
        exp_size = (expected_identity.get('size') or '').strip()
        exp_model = (expected_identity.get('model') or '').strip()

        if exp_by_id:
            if os.path.exists(exp_by_id):
                try:
                    if os.path.realpath(exp_by_id) != os.path.realpath(safe):
                        raise RuntimeError(
                            _('Selected disk identity changed (by-id no longer points at {dev}). '
                              'Re-select the target disk.').format(dev=safe)
                        )
                except OSError:
                    raise RuntimeError(
                        _('Selected disk identity could not be verified. Re-select the target disk.')
                    )
            elif exp_serial and exp_size:
                # A vanished stable link is safe only when both sides provide a
                # complete fallback identity. Missing probe data must fail closed.
                if not current.get('serial') or not current.get('size'):
                    raise RuntimeError(
                        _('Selected disk lost its stable by-id path and its fallback identity could not be verified. Re-select the target disk.')
                    )
            else:
                raise RuntimeError(
                    _('Selected disk lost its stable by-id path and complete fallback identity. Re-select the target disk: {path}.').format(
                        path=exp_by_id
                    )
                )

        if not exp_by_id:
            if not exp_serial or not exp_size or not current.get('serial') or not current.get('size'):
                raise RuntimeError(
                    _('Selected disk has no stable by-id path and its fallback identity is incomplete. Re-select the target disk.')
                )

        if exp_serial and current.get('serial') and exp_serial != current['serial']:
            raise RuntimeError(
                _('Selected disk changed under {dev} (serial mismatch). '
                  'Re-select the target disk to avoid wiping the wrong device.').format(dev=safe)
            )
        if exp_size and current.get('size') and exp_size != current['size']:
            raise RuntimeError(
                _('Selected disk changed under {dev} (size mismatch). '
                  'Re-select the target disk to avoid wiping the wrong device.').format(dev=safe)
            )
        if exp_model and current.get('model') and exp_model != current['model']:
            raise RuntimeError(
                _('Selected disk changed under {dev} (model mismatch). '
                  'Re-select the target disk to avoid wiping the wrong device.').format(dev=safe)
            )

        if exp_by_id and os.path.exists(exp_by_id):
            return exp_by_id

    by_id_now = get_device_by_id_path(safe)
    return by_id_now or safe


def ensure_safe_target_device(device: str) -> str:
    normalized = normalize_device_path(device)
    # Resolve by-id / by-path symlinks to the kernel name for comparisons.
    try:
        if os.path.islink(normalized) or normalized.startswith('/dev/disk/'):
            normalized = normalize_device_path(os.path.realpath(normalized))
    except OSError:
        pass
    if not normalized or not os.path.exists(normalized):
        raise RuntimeError(_('Target device does not exist: ') + normalized)
    if not stat.S_ISBLK(os.stat(normalized).st_mode):
        raise RuntimeError(_('Target device is not a block device: ') + normalized)
    dev_type = _lsblk_type(normalized)
    if not dev_type:
        # Fail closed: also reject names that look like partitions when lsblk fails.
        base = os.path.basename(normalized)
        if parent_block_device_name(base) != base:
            raise RuntimeError(
                _('Target must be a whole disk device, not a partition or special device: ')
                + normalized
            )
        # sysfs partition attribute (1 = partition node)
        sys_part = f'/sys/class/block/{base}/partition'
        if os.path.exists(sys_part):
            raise RuntimeError(
                _('Target must be a whole disk device, not a partition or special device: ')
                + normalized
            )
        raise RuntimeError(
            _('Could not determine device type for target (refusing install): ') + normalized
        )
    if dev_type != 'disk':
        raise RuntimeError(
            _('Target must be a whole disk device, not a partition or special device: ')
            + normalized
            + f' ({dev_type})'
        )

    live_mounts_exist = any(os.path.exists(path) for path in (
        '/run/initramfs/memory/data',
        '/run/initramfs/memory/iso',
        '/lib/live/mount/medium',
        '/lib/live/mount/iso',
    ))

    # Detection failure is soft only when we are not on a live session.
    # If live mount points exist, we *must* be able to identify the backing disk.
    try:
        live_disk = get_live_root_disk()
    except Exception:
        live_disk = None

    if live_disk:
        try:
            if os.path.realpath(normalized) == os.path.realpath(live_disk):
                raise RuntimeError(_('Refusing to install to the running live media device: ') + normalized)
        except OSError:
            if normalized == live_disk:
                raise RuntimeError(_('Refusing to install to the running live media device: ') + normalized)

    if not live_disk and live_mounts_exist:
        # We are running from live media (mount points present) but could not resolve the source disk.
        # Refuse to avoid accidentally destroying the running image.
        raise RuntimeError(
            _('Running from live media but could not determine the source disk. '
              'Refusing to install for safety.')
        )

    return normalized


def format_size_to_gb(size_str: str) -> str:
    """
    Convert size string from lsblk to GB format.
    Examples: "10G" -> "10 GB", "500M" -> "0.5 GB", "2T" -> "2000 GB"
    """
    if not size_str:
        return "? GB"
    
    # Remove any whitespace
    size_str = size_str.strip()
    
    # Extract number and unit
    import re
    match = re.match(r'([0-9.,]+)([KMGTPB]?)', size_str.upper())
    if not match:
        return size_str  # Return original if can't parse
    
    number_str, unit = match.groups()
    try:
        # Replace comma with dot for proper float parsing
        number_str = number_str.replace(',', '.')
        number = float(number_str)
    except ValueError:
        return size_str  # Return original if can't parse number
    
    # Convert to GB
    if unit == 'B' or unit == '':
        gb = number / (1024 ** 3)
    elif unit == 'K':
        gb = number / (1024 ** 2)
    elif unit == 'M':
        gb = number / 1024
    elif unit == 'G':
        gb = number
    elif unit == 'T':
        gb = number * 1024
    elif unit == 'P':
        gb = number * 1024 * 1024
    else:
        return size_str  # Unknown unit
    
    # Format the result
    if gb >= 1000:
        return f"{gb:.0f} GB"
    elif gb >= 100:
        return f"{gb:.0f} GB"
    elif gb >= 10:
        return f"{gb:.1f} GB"
    else:
        return f"{gb:.2f} GB"


def find_available_disks() -> List[Dict]:
    """
    Return a list of available block devices with name, size, model, serial, transport, icon.
    """
    # No major (-I) filter: Xen (xvd), MD, virtio, etc. vary by kernel. Filter TYPE=disk
    # in Python and skip virtual/loop names.
    output = run_command(
        ['lsblk', '-P', '-o', 'NAME,SIZE,MODEL,SERIAL,ROTA,TRAN,TYPE', '-d', '-n'],
        _("Failed to retrieve disk list.")
    )
    devices = []
    # Exclude the running live-media disk using the same helpers as install safety.
    try:
        root_disk = os.path.basename(get_live_root_disk())
    except Exception:
        root_disk = ''
    for line in output.splitlines():
        props = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', line)}
        name = props.get('name')
        dev_type = (props.get('type') or 'disk').lower()
        # skip system boot disk and non-disk / virtual nodes
        if not name or name == root_disk:
            continue
        if dev_type not in ('disk',):
            continue
        if name.startswith(('loop', 'nbd', 'ram', 'zram', 'fd', 'sr')):
            continue

        size   = format_size_to_gb(props.get('size',''))
        model  = props.get('model','')
        serial = props.get('serial','')
        rota   = props.get('rota','0') == '1'
        tran   = props.get('tran','')

        if tran == 'usb':
            icon = 'drive-harddisk-usb'
        elif tran in ('ata', 'sata'):
            icon = 'drive-harddisk'
        elif tran == 'nvme':
            icon = 'drive-harddisk-solidstate'
        elif tran in ('mmc', 'sd'):
            icon = 'drive-removable-media'
        elif rota:
            icon = 'drive-harddisk'
        else:
            icon = 'drive-removable-media'

        path = normalize_device_path(name)
        devices.append({
            'name': name,
            'size': size,
            'model': model,
            'serial': serial,
            'transport': tran or ('rotational' if rota else 'non-rotational'),
            'icon': icon,
            'by_id': get_device_by_id_path(path) or '',
        })
    return devices


def _is_removable_disk(device_path: str) -> bool:
    """
    Check if a disk is removable by examining sysfs.
    """
    try:
        device_name = os.path.basename(device_path)
        removable_path = f"/sys/block/{device_name}/removable"
        if os.path.exists(removable_path):
            with open(removable_path, 'r') as f:
                return f.read().strip() == '1'
    except (OSError, IOError, ValueError):
        pass
    return False


def get_disk_size_mib(device: str) -> int:
    """
    Return disk size in MiB (integer) by parsing parted output.
    """
    
    output = run_command(
        ['parted','-s',device,'unit','MiB','print'],
        _("Failed to get disk size.")
    )
    for line in output.splitlines():
        if device in line:
            m = re.search(r'(\d+(?:\.\d+)?)MiB', line)
            if m:
                return int(float(m.group(1)))
    raise RuntimeError(_("Could not parse disk size."))


def partition_disk(device: str, fs: str, use_gpt: bool) -> None:
    """
    Legacy helper: partition a disk (root + optional ESP).

    Deprecated for install paths — use partition_planner + partition_executor.
    Kept for unit tests and external callers only.
    """
    
    efi = (fs != 'fat32')
    
    if efi and use_gpt:
        run_command(['parted', '-s', device, 'mklabel', 'gpt'], 
                   _("Failed to set GPT label on ") + device + ".")
        size = get_disk_size_mib(device) - 100
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', fs, '1MiB', f'{size}MiB'],
            _("Failed to create primary partition on ") + device + "."
        )
        run_command(
            ['parted', '-s', device, 'mkpart', 'ESP', 'fat32', f'{size}MiB', '100%'],
            _("Failed to create EFI partition on ") + device + "."
        )
        run_command(['parted', '-s', device, 'set', '2', 'boot', 'on'], 
                   _("Failed to set boot flag."))
    elif efi:
        run_command(['parted', '-s', device, 'mklabel', 'msdos'], 
                   _("Failed to set MSDOS label on ") + device + ".")
        size = get_disk_size_mib(device) - 100
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', fs, '1MiB', f'{size}MiB'],
            _("Failed to create primary partition on ") + device + "."
        )
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', 'fat32', f'{size}MiB', '100%'],
            _("Failed to create second partition on ") + device + "."
        )
        run_command(['parted', '-s', device, 'set', '1', 'boot', 'on'], 
                   _("Failed to set boot flag."))
    else:
        run_command(['parted', '-s', device, 'mklabel', 'msdos'], 
                   _("Failed to set MSDOS label on ") + device + ".")
        run_command(
            ['parted', '-s', device, 'mkpart', 'primary', fs, '1MiB', '100%'],
            _("Failed to create primary partition on ") + device + "."
        )


def zero_fill_disk(device: str) -> None:
    """
    Legacy helper: overwrite the beginning of the disk with zeros (2MB).

    Deprecated for install paths — executor uses wipefs instead.
    Kept for unit tests and external callers only.
    """
    try:
        subprocess.check_call(
            ['dd', 'if=/dev/zero', f'of={device}', 'bs=1M', 'count=2', 'status=none'],
            stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(_("Failed to erase ") + device + ".")


class DiskMonitor:
    """
    Monitor disk changes using UDisks2 and notify callbacks when changes occur.

    UDisks/GI is imported lazily so CLI tools (minios-deploy) can use disk_utils
    without gir1.2-udisks-2.0.
    """

    def __init__(self):
        self.udisks_client = None
        self.listener_id = None
        self.callbacks = []
        self._GLib = None

    def start_monitoring(self, callback: Callable[[], None]) -> None:
        """
        Start monitoring disk changes and call the callback when changes occur.
        """
        if callback not in self.callbacks:
            self.callbacks.append(callback)

        if self.udisks_client is None:
            try:
                import gi
                gi.require_version('UDisks', '2.0')
                from gi.repository import UDisks, GLib
                self._GLib = GLib
                self.udisks_client = UDisks.Client.new_sync()
                self.listener_id = self.udisks_client.connect("changed", self._on_disks_changed)
            except Exception:
                self.udisks_client = None
                self.listener_id = None

    def stop_monitoring(self, callback: Callable[[], None] = None) -> None:
        """
        Stop monitoring disk changes. If callback is provided, remove only that callback.
        If no callback provided, remove all callbacks and stop monitoring.
        """
        if callback and callback in self.callbacks:
            self.callbacks.remove(callback)
        elif callback is None:
            self.callbacks.clear()

        if not self.callbacks and self.udisks_client and self.listener_id:
            self.udisks_client.disconnect(self.listener_id)
            self.udisks_client = None
            self.listener_id = None

    def pause_monitoring(self) -> None:
        """
        Temporarily pause monitoring (useful during disk operations).
        """
        if self.udisks_client and self.listener_id:
            self.udisks_client.handler_block(self.listener_id)

    def resume_monitoring(self) -> None:
        """
        Resume monitoring after pause.
        """
        if self.udisks_client and self.listener_id:
            self.udisks_client.handler_unblock(self.listener_id)

    def _on_disks_changed(self, client, *args) -> None:
        """
        Internal callback when UDisks detects changes.
        """
        idle_add = self._GLib.idle_add if self._GLib is not None else None
        if idle_add is None:
            try:
                from gi.repository import GLib
                idle_add = GLib.idle_add
            except Exception:
                for callback in self.callbacks:
                    callback()
                return
        for callback in self.callbacks:
            # GLib repeats an idle source while its callback returns true. Disk
            # callbacks are notifications, not persistent idle handlers.
            idle_add(lambda callback=callback: (callback(), False)[1])


# Global disk monitor instance
_disk_monitor = DiskMonitor()


def start_disk_monitoring(callback: Callable[[], None]) -> None:
    """
    Start monitoring disk changes globally.
    """
    _disk_monitor.start_monitoring(callback)


def stop_disk_monitoring(callback: Callable[[], None] = None) -> None:
    """
    Stop monitoring disk changes globally.
    """
    _disk_monitor.stop_monitoring(callback)


def pause_disk_monitoring() -> None:
    """
    Pause disk monitoring globally.
    """
    _disk_monitor.pause_monitoring()


def resume_disk_monitoring() -> None:
    """
    Resume disk monitoring globally.
    """
    _disk_monitor.resume_monitoring()
