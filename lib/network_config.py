#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Small, init-independent NetworkManager profile helpers."""

import ipaddress
import os
import re
import tempfile
from typing import Optional

from module_selection import DEFAULT_BUNDLES_DIR, normalize_selected_modules


INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
NM_PROFILE_NAME = "minios-static.nmconnection"
LEGACY_NM_PROFILE_NAME = "minios-installer.nmconnection"


def validate_static_ipv4(address: str, prefix: str, gateway: str, dns: str) -> Optional[str]:
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        return "IPv4 address is not valid."
    try:
        prefix_value = int(prefix)
    except (TypeError, ValueError):
        return "Network prefix must be between 0 and 32."
    if prefix_value < 0 or prefix_value > 32:
        return "Network prefix must be between 0 and 32."
    for value, label in ((gateway, "Gateway"),):
        if value:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                return "{} is not a valid IPv4 address.".format(label)
    for value in [item.strip() for item in dns.split(",") if item.strip()]:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return "DNS server is not a valid IP address."
    return None


def network_manager_available(root: str = "/") -> bool:
    return any(
        os.path.exists(os.path.join(root, path))
        for path in ("usr/sbin/NetworkManager", "usr/bin/nmcli", "bin/nmcli")
    )


def ifupdown_available(root: str = "/") -> bool:
    return any(os.path.exists(os.path.join(root, path)) for path in ("sbin/ifup", "usr/sbin/ifup"))


def network_backend_available(root: str = "/") -> bool:
    return network_manager_available(root) or ifupdown_available(root)


def select_network_backend(root: str = "/") -> Optional[str]:
    if network_manager_available(root):
        return "nm"
    if ifupdown_available(root):
        return "ifupdown"
    return None


def source_supports_live_network(source: str, module_names=None, selected_modules=None, bundles_dir: str = DEFAULT_BUNDLES_DIR) -> bool:
    """Return whether selected source modules declare live network support."""
    import json

    def registry_supports(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return "live-config.network-method" in (data.get("capabilities") or {})
        except (OSError, ValueError, TypeError):
            return False

    registry = os.path.join(source, "usr", "share", "minios", "capabilities", "minios-live-config.json")
    if registry_supports(registry):
        return True
    names = list(module_names or [])
    if selected_modules is not None and names:
        try:
            names = normalize_selected_modules(names, selected_modules)
        except ValueError:
            return False
    for name in names:
        path = os.path.join(
            bundles_dir,
            name,
            "usr",
            "share",
            "minios",
            "capabilities",
            "minios-live-config.json",
        )
        if registry_supports(path):
            return True
    return False


def network_manager_profile(interface: str, address: str, prefix: str, gateway: str, dns: str) -> str:
    if not INTERFACE_RE.match(interface):
        raise ValueError("Invalid network interface.")
    error = validate_static_ipv4(address, prefix, gateway, dns)
    if error:
        raise ValueError(error)
    address_value = "{}/{}".format(address, prefix)
    if gateway:
        address_value += ",{}".format(gateway)
    dns_values = [item.strip() for item in dns.split(",") if item.strip()]
    lines = [
        "[connection]",
        "id=MiniOS static wired network",
        "type=ethernet",
        "interface-name={}".format(interface),
        "autoconnect=true",
        "autoconnect-priority=100",
        "",
        "[ipv4]",
        "method=manual",
        "address1={}".format(address_value),
    ]
    if dns_values:
        lines.append("dns={};".format(";".join(dns_values)))
    lines.extend(["", "[ipv6]", "method=auto", ""])
    return "\n".join(lines)


def write_network_manager_profile(target: str, interface: str, address: str, prefix: str, gateway: str, dns: str, dry_run: bool = False) -> str:
    profile = network_manager_profile(interface, address, prefix, gateway, dns)
    directory = os.path.join(target, "etc", "NetworkManager", "system-connections")
    path = os.path.join(directory, NM_PROFILE_NAME)
    if not dry_run:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        for name in (LEGACY_NM_PROFILE_NAME, NM_PROFILE_NAME):
            try:
                os.unlink(os.path.join(directory, name))
            except FileNotFoundError:
                pass
        fd, temp_path = tempfile.mkstemp(prefix=NM_PROFILE_NAME + ".", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(profile)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    return path


def write_ifupdown_profile(target: str, interface: str, address: str, prefix: str, gateway: str, dns: str, dry_run: bool = False) -> str:
    if not INTERFACE_RE.match(interface):
        raise ValueError("Invalid network interface.")
    error = validate_static_ipv4(address, prefix, gateway, dns)
    if error:
        raise ValueError(error)
    path = os.path.join(target, "etc", "network", "interfaces.d", "minios-static")
    dns_values = " ".join(item.strip() for item in dns.split(",") if item.strip())
    lines = [
        "# Managed by MiniOS installer",
        "auto {}".format(interface),
        "iface {} inet static".format(interface),
        "    address {}/{}".format(address, prefix),
    ]
    if gateway:
        lines.append("    gateway {}".format(gateway))
    if dns_values:
        lines.append("    dns-nameservers {}".format(dns_values))
    lines.append("")
    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="minios-static.", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    return path


def write_network_profile(target: str, interface: str, address: str, prefix: str, gateway: str, dns: str, dry_run: bool = False) -> str:
    backend = select_network_backend(target)
    if backend == "nm":
        return write_network_manager_profile(target, interface, address, prefix, gateway, dns, dry_run=dry_run)
    if backend == "ifupdown":
        return write_ifupdown_profile(target, interface, address, prefix, gateway, dns, dry_run=dry_run)
    raise RuntimeError("No supported network backend is available in the target system.")


def create_live_network_hook(interface: str, address: str, prefix: str, gateway: str, dns: str) -> str:
    profile = network_manager_profile(interface, address, prefix, gateway, dns)
    fd, path = tempfile.mkstemp(prefix="minios-network-hook-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nset -eu\n")
            fh.write("install -d -m 700 /etc/NetworkManager/system-connections\n")
            fh.write("rm -f /etc/NetworkManager/system-connections/minios-installer.nmconnection /etc/NetworkManager/system-connections/minios-static.nmconnection\n")
            fh.write("umask 077\n")
            fh.write("cat > /etc/NetworkManager/system-connections/minios-static.nmconnection <<'MINIOS_NETWORK_PROFILE'\n")
            fh.write(profile)
            fh.write("MINIOS_NETWORK_PROFILE\n")
            fh.write("chmod 600 /etc/NetworkManager/system-connections/minios-static.nmconnection\n")
        os.chmod(path, 0o700)
    except Exception:
        os.unlink(path)
        raise
    return path
