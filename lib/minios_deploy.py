#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import sys

from disk_utils import ensure_safe_target_device, get_device_identity
from disk_utils import find_available_disks
from install_state import InstallState, UserConfig
from live_deploy import run_live_install, runtime_supports_luks_persistence
from module_selection import (
    discover_module_names,
    normalize_selected_modules,
    parse_module_list,
    required_root_mib,
    payload_size_bytes,
)
from native_deploy import run_native_install
from partition_planner import build_plan
from partition_scanner import scan_disk
from minios_security.security_profiles import SECURITY_PROFILE_IDS, default_security_profile


# CLI flags that map 1:1 onto UserConfig attributes (except password hashing).
# Keep in sync with minios-configurator TAB_DEFINITIONS / live-config keys.
_USER_CONFIG_CLI = (
    # (argparse dest, UserConfig attr)
    ("username", "username"),
    ("full_name", "full_name"),
    ("user_default_groups", "user_default_groups"),
    ("password", "password"),
    ("root_password", "root_password"),
    ("link_user_dirs", "link_user_dirs"),
    ("bind_user_dirs", "bind_user_dirs"),
    ("user_dirs_path", "user_dirs_path"),
    ("noroot", "noroot"),
    ("hostname", "hostname"),
    ("locale", "locale"),
    ("timezone", "timezone"),
    ("default_target", "default_target"),
    ("enable_services", "enable_services"),
    ("disable_services", "disable_services"),
    ("keyboard_model", "keyboard_model"),
    ("keyboard", "keyboard"),
    ("keyboard_options", "keyboard_options"),
    ("keyboard_variants", "keyboard_variants"),
    ("module_mode", "module_mode"),
    ("config_cmdline", "config_cmdline"),
    ("config_debug", "config_debug"),
    ("export_logs", "export_logs"),
)


def _bool_config_value(value: str) -> str:
    """Normalize CLI boolean tokens to 'true'/'false' (configurator format)."""
    v = value.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return "true"
    if v in ("false", "0", "no", "off"):
        return "false"
    raise argparse.ArgumentTypeError(
        "expected true/false (also accepts yes/no, on/off, 1/0)"
    )


def user_config_from_args(args) -> tuple:
    """
    Build UserConfig from install CLI args.

    Returns (UserConfig, customized: bool).
    """
    user = UserConfig()
    customized = False
    for dest, attr in _USER_CONFIG_CLI:
        value = getattr(args, dest, None)
        if value is None or value == "":
            continue
        setattr(user, attr, value)
        customized = True
    return user, customized


def _plan_dict(plan):
    return {
        "device": plan.device,
        "use_gpt": plan.use_gpt,
        "use_efi": plan.use_efi,
        "wipe_disk": plan.wipe_disk,
        "reuse_esp": plan.reuse_esp,
        "esp_path": plan.esp_path,
        "partitions": [p.__dict__ for p in plan.partitions],
        "summary": plan.summary_lines(),
    }


def _module_space_requirement(install_mode: str, selected_value: str = "", persistence_size_mib: int = 0) -> tuple:
    available = discover_module_names()
    requested = parse_module_list(selected_value)
    selected = normalize_selected_modules(available, requested)
    if not selected:
        return selected, 0
    total = payload_size_bytes(selected, install_mode=install_mode)
    if total is None:
        raise RuntimeError("Cannot calculate selected module sizes.")
    persistence_size_mib = max(0, int(persistence_size_mib or 0)) if install_mode == "live" else 0
    return selected, required_root_mib(total) + persistence_size_mib


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number")
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _validate_cli_inputs(args) -> None:
    """Reject unsafe or malformed textual CLI values before planning a disk write."""
    user, _customized = user_config_from_args(args)
    checks = (
        ("username", user.username, r"[a-z_][a-z0-9_-]{0,31}"),
        ("hostname", user.hostname, r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"),
        ("locale", user.locale, r"[A-Za-z0-9_.@-]+(?:,[A-Za-z0-9_.@-]+)*"),
        ("timezone", user.timezone, r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+"),
    )
    for name, value, pattern in checks:
        if value and not re.fullmatch(pattern, value):
            raise ValueError("invalid --{0} value".format(name))
    for name in ("full_name", "keyboard", "keyboard_model", "keyboard_options", "keyboard_variants",
                 "enable_services", "disable_services", "config_cmdline"):
        value = getattr(user, name, "")
        if value and (len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("invalid --{0} value".format(name.replace("_", "-")))
    persistence_mode = getattr(args, "persistence_mode", "none")
    if persistence_mode != "none":
        if getattr(args, "mode", "live") != "live":
            raise ValueError("--persistence-mode is available only with --mode live")
        size = int(getattr(args, "persistence_size", 0) or 0)
        if persistence_mode == "native" and size:
            raise ValueError("--persistence-size applies only to dynfilefs, raw, and luks modes")
        if persistence_mode == "native" and getattr(args, "filesystem", "ext4") in ("fat32", "ntfs"):
            raise ValueError("--persistence-mode native requires a POSIX-compatible target filesystem")
        if size < 0 or size > 1000000:
            raise ValueError("--persistence-size must be 0 (default) or at most 1000000 MiB")
        if persistence_mode in ("raw", "luks") and getattr(args, "filesystem", "ext4") == "fat32" and size > 4000:
            raise ValueError("--persistence-size must not exceed 4000 MiB on FAT32")


def _effective_persistence_size(args) -> int:
    if getattr(args, "persistence_mode", "none") not in ("dynfilefs", "raw", "luks"):
        return 0
    return int(getattr(args, "persistence_size", 0) or 4000)


def cmd_list_disks(args):
    disks = find_available_disks()
    if args.json:
        print(json.dumps(disks, indent=2))
        return 0
    for disk in disks:
        print(f"{disk['name']:12} {disk['size']:>10}  {disk.get('model', '')}")
    return 0


def cmd_plan(args):
    _validate_cli_inputs(args)
    layout = scan_disk(ensure_safe_target_device(args.device))
    install_mode = getattr(args, "mode", "live") or "live"
    _selected, root_mib = _module_space_requirement(
        install_mode, getattr(args, "modules", ""), _effective_persistence_size(args)
    )
    plan = build_plan(
        layout,
        args.placement,
        args.filesystem,
        install_mode=install_mode,
        swap_size_mib=getattr(args, "swap_size", 0),
        boot_layout=getattr(args, "boot_layout", "auto"),
        alongside_size_mib=getattr(args, "alongside_size", 0),
        required_root_mib=root_mib,
    )
    if args.json:
        print(json.dumps(_plan_dict(plan), indent=2))
        return 0
    for line in plan.summary_lines():
        print(line)
    return 0


def cmd_install(args):
    if not args.yes and not args.dry_run:
        print("Refusing to install without --yes. Use --dry-run to preview commands.", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("minios-deploy install must be run as root.", file=sys.stderr)
        return 2

    _validate_cli_inputs(args)

    user, customized = user_config_from_args(args)
    config_override = None
    if getattr(args, "config_file", None):
        config_override = args.config_file
        if not os.path.isfile(config_override):
            print(f"minios-deploy: config file not found: {config_override}", file=sys.stderr)
            return 2

    target_device = ensure_safe_target_device(args.device)
    persistence_mode = getattr(args, "persistence_mode", "none")
    persistence_size = _effective_persistence_size(args)
    selected_modules, root_mib = _module_space_requirement(args.mode, getattr(args, "modules", ""), persistence_size)
    state = InstallState(
        install_mode=args.mode,
        persistence_mode=persistence_mode,
        persistence_size_mib=persistence_size,
        placement=args.placement,
        target_device=target_device,
        target_device_identity=get_device_identity(target_device),
        filesystem=args.filesystem,
        swap_size_mib=max(0, int(getattr(args, "swap_size", 0) or 0)),
        alongside_size_mib=max(0, int(getattr(args, "alongside_size", 0) or 0)),
        required_root_mib=root_mib,
        boot_layout=getattr(args, "boot_layout", "auto"),
        boot_config_type=args.boot_menu,
        security_profile=args.security_profile or default_security_profile(args.mode),
        selected_modules=selected_modules,
        download_missing_packages=bool(getattr(args, "download_packages", False)),
        user_config=user,
        user_config_customized=customized,
        config_override_path=config_override,
    )

    def progress(percent, message):
        print(f"P: {percent:3d}% {message}", flush=True)

    def log(message):
        print(f"I: {message}", flush=True)

    if args.mode == "native":
        run_native_install(state, progress, log, dry_run=args.dry_run)
    else:
        run_live_install(state, progress, log, dry_run=args.dry_run)
    return 0


def _add_user_config_arguments(parser):
    """All live-config settings exposed by minios-configurator."""
    g = parser.add_argument_group(
        "live configuration",
        "Overrides written to minios/config.conf (same keys as minios-configurator)",
    )
    g.add_argument("--config-file", metavar="PATH",
                   help="base live config.conf to copy/merge (default: /etc/live/config.conf when overrides are set)")
    # User
    g.add_argument("--username", help="LIVE_USERNAME")
    g.add_argument("--full-name", dest="full_name", help="LIVE_USER_FULLNAME")
    g.add_argument("--user-groups", dest="user_default_groups",
                   help="LIVE_USER_DEFAULT_GROUPS (comma/space separated)")
    g.add_argument("--password", help="user password (stored as LIVE_USER_PASSWORD_CRYPTED)")
    g.add_argument("--root-password", dest="root_password",
                   help="root password (stored as LIVE_ROOT_PASSWORD_CRYPTED)")
    g.add_argument("--link-user-dirs", type=_bool_config_value, metavar="BOOL",
                   help="LIVE_LINK_USER_DIRS (true/false)")
    g.add_argument("--bind-user-dirs", type=_bool_config_value, metavar="BOOL",
                   help="LIVE_BIND_USER_DIRS (true/false)")
    g.add_argument("--user-dirs-path", dest="user_dirs_path",
                   help="LIVE_USER_DIRS_PATH")
    # System
    g.add_argument("--noroot", type=_bool_config_value, metavar="BOOL",
                   help="LIVE_CONFIG_NOROOT (true/false)")
    g.add_argument("--hostname", help="LIVE_HOSTNAME")
    g.add_argument("--locale", "--locales", dest="locale",
                   help="LIVE_LOCALES (comma-separated, first is default)")
    g.add_argument("--timezone", help="LIVE_TIMEZONE")
    g.add_argument("--default-target", dest="default_target",
                   help="DEFAULT_TARGET (graphical[.target]|multi-user[.target]|rescue[.target])")
    g.add_argument("--enable-services", dest="enable_services",
                   help="ENABLE_SERVICES (comma-separated)")
    g.add_argument("--disable-services", dest="disable_services",
                   help="DISABLE_SERVICES (comma-separated)")
    # Keyboard
    g.add_argument("--keyboard-model", dest="keyboard_model",
                   help="LIVE_KEYBOARD_MODEL (e.g. pc105)")
    g.add_argument("--keyboard", "--keyboard-layouts", dest="keyboard",
                   help="LIVE_KEYBOARD_LAYOUTS (e.g. us,ru)")
    g.add_argument("--keyboard-options", dest="keyboard_options",
                   help="LIVE_KEYBOARD_OPTIONS (e.g. grp:alt_shift_toggle)")
    g.add_argument("--keyboard-variants", dest="keyboard_variants",
                   help="LIVE_KEYBOARD_VARIANTS")
    # Advanced
    g.add_argument("--module-mode", dest="module_mode", choices=["simple", "merged"],
                   help="LIVE_MODULE_MODE")
    g.add_argument("--live-config-cmdline", dest="config_cmdline",
                   help="LIVE_CONFIG_CMDLINE (extra live-config boot params)")
    g.add_argument("--config-debug", type=_bool_config_value, metavar="BOOL",
                   help="LIVE_CONFIG_DEBUG (true/false)")
    g.add_argument("--export-logs", type=_bool_config_value, metavar="BOOL",
                   help="EXPORT_LOGS (true/false)")


def build_parser(luks_available=None):
    if luks_available is None:
        luks_available = runtime_supports_luks_persistence()
    persistence_modes = ["none", "native", "dynfilefs", "raw"]
    if luks_available:
        persistence_modes.append("luks")
    parser = argparse.ArgumentParser(
        prog="minios-deploy",
        description="MiniOS installer command-line interface",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("list-disks", help="list installable disks")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list_disks)

    p = sub.add_parser("plan", help="print the partition plan")
    p.add_argument("device")
    p.add_argument("--filesystem", default="ext4")
    p.add_argument("--placement", default="erase_all", choices=["erase_all", "free_space", "alongside_os"])
    p.add_argument("--alongside-size", type=_nonnegative_int, default=0, help="space to create for MiniOS when resizing, in MiB (default: calculated requirement)")
    p.add_argument("--mode", default="live", choices=["live", "native"])
    p.add_argument("--modules", default="", help="comma-separated .sb modules used for the space calculation")
    p.add_argument("--persistence-mode", default="none", choices=persistence_modes, help="live session persistence mode")
    p.add_argument("--persistence-size", type=_nonnegative_int, default=0, metavar="MIB", help="container persistence size in MiB (default: 4000)")
    p.add_argument("--boot-layout", default="auto", choices=["auto", "bios_mbr", "uefi_mbr", "uefi_gpt"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("install", help="perform an install")
    p.add_argument("device")
    p.add_argument("--mode", default="live", choices=["live", "native"],
                   help="install live modules layout or a regular native system")
    p.add_argument("--security-profile", choices=SECURITY_PROFILE_IDS,
                   help="security profile preset (default: convenient for live, balanced for native)")
    p.add_argument("--filesystem", default="ext4")
    p.add_argument("--placement", default="erase_all", choices=["erase_all", "free_space", "alongside_os"])
    p.add_argument("--alongside-size", type=_nonnegative_int, default=0, help="space to create for MiniOS when resizing, in MiB (default: calculated requirement)")
    p.add_argument("--swap-size", type=_nonnegative_int, default=0, help="native swap size in MiB")
    p.add_argument("--boot-layout", default="auto", choices=["auto", "bios_mbr", "uefi_mbr", "uefi_gpt"],
                   help="native boot layout: auto, bios_mbr, uefi_mbr, or uefi_gpt")
    p.add_argument("--boot-menu", default="multilang",
                   help="boot menu language code or 'multilang'")
    p.add_argument("--modules", default="",
                    help="comma-separated .sb modules to install; selecting a higher module includes lower modules")
    p.add_argument("--persistence-mode", default="none", choices=persistence_modes,
                    help="live session persistence mode (storage is created by initrd)")
    p.add_argument("--persistence-size", type=_nonnegative_int, default=0, metavar="MIB",
                    help="container persistence size in MiB (default: 4000)")
    p.add_argument("--download-packages", action="store_true",
                   help="download missing packages for standard native installation")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true",
                   help="confirm destructive install (required unless --dry-run)")
    _add_user_config_arguments(p)
    p.set_defaults(func=cmd_install)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as exc:
        print(f"minios-deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
