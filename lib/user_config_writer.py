#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, Optional

from install_state import UserConfig
from minios_security.security_profiles import merge_live_config


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def load_config_values(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                match = re.match(r"^([A-Z0-9_]+)=(?:'(.*)'|\"(.*)\"|(.*))", line.strip())
                if match:
                    values[match.group(1)] = match.group(2) or match.group(3) or match.group(4) or ""
    except OSError:
        pass
    return values


def normalize_default_target(value: str) -> str:
    """Normalize short aliases to systemd unit names (same as minios-configurator)."""
    mapping = {
        "graphical": "graphical.target",
        "graphical.target": "graphical.target",
        "multi-user": "multi-user.target",
        "multi-user.target": "multi-user.target",
        "rescue": "rescue.target",
        "rescue.target": "rescue.target",
    }
    return mapping.get(value, value)


def process_services_field(text: str) -> str:
    parts = [s.strip() for s in text.split(",") if s.strip()]
    return ",".join(parts)


def hash_system_password(plain_password: str) -> str:
    """
    Hash a password for LIVE_*_PASSWORD_CRYPTED (same approach as minios-configurator).

    Prefer mkpasswd, then openssl passwd -6. Raises RuntimeError if neither works.
    """
    if not plain_password:
        return ""

    for cmd in (
        ["mkpasswd", "--stdin"],
        ["mkpasswd", "-m", "sha-512", "--stdin"],
    ):
        try:
            result = subprocess.run(
                cmd,
                input=plain_password,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                check=True,
                timeout=30,
            )
            hashed = (result.stdout or "").strip()
            if hashed:
                return hashed
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            continue

    try:
        salt_result = subprocess.run(
            ["openssl", "rand", "-hex", "8"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            check=True,
            timeout=10,
        )
        salt = (salt_result.stdout or "").strip()
        hash_result = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
            input=plain_password,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            check=True,
            timeout=30,
        )
        hashed = (hash_result.stdout or "").strip()
        if hashed:
            return hashed
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(
            "Cannot hash password: neither mkpasswd nor openssl available or working. "
            "Install whois (mkpasswd) or openssl."
        ) from exc
    raise RuntimeError("Cannot hash password: empty result from hashing tools.")


def _updated_entries(user: UserConfig, include_live_network: bool = False) -> Dict[str, str]:
    """
    Map UserConfig fields to live-config / minios-configurator keys.

    Only non-empty string fields are emitted (empty = do not override).
    """
    entries: Dict[str, str] = {}

    if user.link_user_dirs == "true" and user.bind_user_dirs == "true":
        raise ValueError("link_user_dirs and bind_user_dirs cannot both be enabled")
    if user.link_user_dirs == "true" or user.bind_user_dirs == "true":
        path = (user.user_dirs_path or "").strip().lstrip("/")
        parts = path.split("/") if path else []
        if (
            not parts
            or len(path) > 240
            or any(part in ("", ".", "..") for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or not re.match(r"^[A-Za-z0-9._ /-]+$", path)
        ):
            raise ValueError(
                "user_dirs_path must be a safe path inside the MiniOS drive, "
                "for example /minios/userdata"
            )

    # Simple string fields: (attr, config_key)
    simple = (
        ("username", "LIVE_USERNAME"),
        ("full_name", "LIVE_USER_FULLNAME"),
        ("user_default_groups", "LIVE_USER_DEFAULT_GROUPS"),
        ("link_user_dirs", "LIVE_LINK_USER_DIRS"),
        ("bind_user_dirs", "LIVE_BIND_USER_DIRS"),
        ("user_dirs_path", "LIVE_USER_DIRS_PATH"),
        ("noroot", "LIVE_CONFIG_NOROOT"),
        ("hostname", "LIVE_HOSTNAME"),
        ("locale", "LIVE_LOCALES"),
        ("timezone", "LIVE_TIMEZONE"),
        ("keyboard_model", "LIVE_KEYBOARD_MODEL"),
        ("keyboard", "LIVE_KEYBOARD_LAYOUTS"),
        ("keyboard_options", "LIVE_KEYBOARD_OPTIONS"),
        ("keyboard_variants", "LIVE_KEYBOARD_VARIANTS"),
        ("module_mode", "LIVE_MODULE_MODE"),
        ("config_cmdline", "LIVE_CONFIG_CMDLINE"),
        ("config_debug", "LIVE_CONFIG_DEBUG"),
        ("export_logs", "EXPORT_LOGS"),
    )
    for attr, key in simple:
        value = getattr(user, attr, "") or ""
        if value:
            entries[key] = value

    if user.default_target:
        entries["DEFAULT_TARGET"] = normalize_default_target(user.default_target)
    if user.enable_services:
        entries["ENABLE_SERVICES"] = process_services_field(user.enable_services)
    if user.disable_services:
        entries["DISABLE_SERVICES"] = process_services_field(user.disable_services)
    if include_live_network and user.network_method == "static":
        entries["LIVE_NETWORK_METHOD"] = "static"
        entries["LIVE_NETWORK_INTERFACE"] = user.network_interface
        entries["LIVE_NETWORK_ADDRESS"] = user.network_address
        entries["LIVE_NETWORK_PREFIX"] = user.network_prefix or "24"
        if user.network_gateway:
            entries["LIVE_NETWORK_GATEWAY"] = user.network_gateway
        if user.network_dns:
            entries["LIVE_NETWORK_DNS"] = user.network_dns

    # Passwords: store crypted hashes like minios-configurator (never plaintext in config).
    if user.password:
        entries["LIVE_USER_PASSWORD_CRYPTED"] = hash_system_password(user.password)
    if user.root_password:
        entries["LIVE_ROOT_PASSWORD_CRYPTED"] = hash_system_password(user.root_password)

    return entries


def _secure_temp_config(prefix: str = "minios-deploy-config-", suffix: str = ".conf") -> str:
    """Create a temp config path mode 0600 (passwords may be written here)."""
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def write_live_config(user: UserConfig, source_config: str = "/etc/live/config.conf", extra_entries: Optional[Dict[str, str]] = None, include_live_network: bool = False) -> Optional[str]:
    profile_entries = dict(extra_entries or {})
    profile_entries.pop("LIVE_SECURITY_PROFILE", None)
    user_entries = _updated_entries(user, include_live_network=include_live_network)
    entries = merge_live_config(profile_entries, user_entries)
    if "LIVE_CONFIG_CMDLINE" in user_entries:
        entries["LIVE_CONFIG_CMDLINE"] = user_entries["LIVE_CONFIG_CMDLINE"]
    if not entries:
        return None
    path = _secure_temp_config()

    try:
        if os.path.exists(source_config):
            # copyfile: content only — do not preserve world-readable mode from source.
            shutil.copyfile(source_config, path)
            os.chmod(path, 0o600)
            with open(path, "r", encoding="utf-8") as fh:
                original = fh.read().splitlines()
            output = []
            seen = set()
            for line in original:
                match = re.match(r"^([A-Z0-9_]+)=", line)
                if match and match.group(1) in entries:
                    key = match.group(1)
                    seen.add(key)
                    output.append(f"{key}={_quote(entries[key])}")
                else:
                    output.append(line)
            for key, value in entries.items():
                if key not in seen:
                    output.append(f"{key}={_quote(value)}")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(output) + "\n")
        else:
            with open(path, "w", encoding="utf-8") as fh:
                for key, value in entries.items():
                    fh.write(f"{key}={_quote(value)}\n")
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path
