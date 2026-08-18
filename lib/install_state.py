#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from dataclasses import dataclass, field

from typing import List, Optional
import os

from minios_security.security_profiles import default_security_profile, validate_security_profile


REMOTE_ACCESS_SERVICES = ("ssh", "xrdp")
REMOTE_ACCESS_SERVICE_BINARIES = {
    "ssh": ("usr/sbin/sshd", "usr/bin/sshd"),
    "xrdp": ("usr/sbin/xrdp", "usr/bin/xrdp"),
}


class InstallCanceled(RuntimeError):
    """Raised when the user cancels an in-progress install. Not localized for matching."""


@dataclass
class UserConfig:
    """
    Live-config overrides for the installed system.

    Fields mirror minios-configurator / live-config keys. Empty string means
    "do not override". Boolean-like fields use 'true'/'false' when set.
    """
    # User tab
    full_name: str = ""
    username: str = ""
    user_default_groups: str = ""
    password: str = ""  # plain; written as LIVE_USER_PASSWORD_CRYPTED
    root_password: str = ""  # plain; written as LIVE_ROOT_PASSWORD_CRYPTED
    link_user_dirs: str = ""  # 'true' / 'false'
    bind_user_dirs: str = ""
    user_dirs_path: str = ""
    # System tab
    noroot: str = ""  # LIVE_CONFIG_NOROOT
    hostname: str = ""
    locale: str = ""  # LIVE_LOCALES
    timezone: str = ""
    default_target: str = ""
    enable_services: str = ""
    disable_services: str = ""
    network_method: str = "dhcp"  # dhcp | static
    network_interface: str = ""
    network_address: str = ""
    network_prefix: str = "24"
    network_gateway: str = ""
    network_dns: str = ""
    # Keyboard tab
    keyboard_model: str = ""
    keyboard: str = ""  # LIVE_KEYBOARD_LAYOUTS
    keyboard_options: str = ""
    keyboard_variants: str = ""
    # Advanced tab
    module_mode: str = ""  # simple | merged
    config_cmdline: str = ""  # LIVE_CONFIG_CMDLINE
    config_debug: str = ""  # LIVE_CONFIG_DEBUG 'true'/'false'
    export_logs: str = ""  # EXPORT_LOGS 'true'/'false'

    def has_overrides(self) -> bool:
        return self != UserConfig()


@dataclass
class InstallState:
    install_mode: str = "live"
    # The live initrd creates persistence storage; the installer only writes
    # boot parameters and never receives a LUKS passphrase.
    persistence_mode: str = "none"  # none | native | dynfilefs | raw | luks
    persistence_size_mib: int = 0
    security_profile: str = ""
    placement: str = "erase_all"
    target_device: Optional[str] = None
    # Snapshot from selection time (by-id/serial/size/model) to refuse USB rebind.
    target_device_identity: Optional[dict] = None
    filesystem: str = "ext4"
    swap_size_mib: int = 0
    alongside_size_mib: int = 0
    required_root_mib: int = 0
    boot_layout: str = "auto"  # auto | bios_mbr | uefi_mbr | uefi_gpt
    boot_config_type: str = "multilang"
    selected_modules: List[str] = field(default_factory=list)
    download_missing_packages: bool = False
    package_cache_path: Optional[str] = None
    partition_plan: Optional[object] = None
    # Validated plans are consumed only by the native phase-3 deployment path;
    # GUI and CLI still do not create or expose them.
    manual_partition_plan: Optional[object] = None
    user_config: UserConfig = field(default_factory=UserConfig)
    user_config_customized: bool = False
    config_override_path: Optional[str] = None
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if self.security_profile:
            self.security_profile = validate_security_profile(self.security_profile)
        else:
            self.security_profile = default_security_profile(self.install_mode)

    def is_native(self) -> bool:
        return self.install_mode == "native"

    def set_install_mode(self, mode: str) -> None:
        previous_default = default_security_profile(self.install_mode)
        self.install_mode = mode
        if not self.security_profile or self.security_profile == previous_default:
            self.security_profile = default_security_profile(mode)


def _split_services(value: str) -> List[str]:
    return [item.strip() for item in (value or "").replace(" ", ",").split(",") if item.strip()]


def _join_services(values: List[str]) -> str:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return ",".join(result)


def available_remote_access_services(root: str = "/") -> List[str]:
    result = []
    for service, paths in REMOTE_ACCESS_SERVICE_BINARIES.items():
        if any(os.path.exists(os.path.join(root, path)) for path in paths):
            result.append(service)
    return result


def set_service_enabled(enable_services: str, disable_services: str, service: str, enabled: bool) -> tuple:
    enable = _split_services(enable_services)
    disable = _split_services(disable_services)
    enable = [item for item in enable if item != service]
    if enabled:
        disable = [item for item in disable if item != service]
        enable.append(service)
    return _join_services(enable), _join_services(disable)
