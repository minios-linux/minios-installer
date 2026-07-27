#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from typing import List, Optional, Tuple

import gi

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from disk_utils import (
    find_available_disks,
    get_device_identity,
    pause_disk_monitoring,
    resolve_install_device,
    resume_disk_monitoring,
    start_disk_monitoring,
    stop_disk_monitoring,
)
from format_utils import filesystems_for_boot_mode
from install_state import InstallCanceled, InstallState, available_remote_access_services, set_service_enabled
from live_deploy import run_live_install, runtime_supports_luks_persistence
from module_selection import (
    calculate_module_sizes,
    discover_module_names,
    normalize_selected_modules,
    payload_overhead_bytes,
    required_prefix_count,
    required_root_mib,
    selected_modules_size_bytes,
)
from native_deploy import run_native_install
from mount_utils import get_mounted_partitions
from network_config import network_backend_available, validate_static_ipv4
from package_preflight import native_missing_packages, native_requires_standard_bootloader, preflight_ok, preflight_package_download, resize_missing_packages
from partition_models import PLACEMENT_ALONGSIDE_OS, PLACEMENT_ERASE_ALL, PLACEMENT_FREE_SPACE
from manual_partitioning import ManualPlanError, SectorExtent, scan_manual_layout
from manual_partition_controller import ManualPartitionController
from partition_geometry import (erase_swap_limits, resize_boundary_x,
                                resize_size_at_x, trailing_swap_boundary_x,
                                trailing_swap_size_at_x)
from partition_planner import _use_efi_for_layout, build_plan
from partition_resize import select_resize_candidate
from partition_scanner import scan_disk
from user_config_writer import load_config_values
from minios_security.capabilities import load_capabilities, support_class, supports
from minios_security.security_profiles import SECURITY_PROFILE_IDS, profile_required_capabilities

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango


APPLICATION_ID = "org.minios.installer"
APP_NAME = "minios-installer"
APP_TITLE = "MiniOS Installer"
LOCALE_DIRECTORY = "/usr/share/locale"
CSS_SYSTEM_PATH = "/usr/share/minios-installer/style.css"
_SHARE_STYLES = os.path.normpath(os.path.join(_LIB_DIR, "..", "share", "styles", "style.css"))
ICON_WINDOW = "usb-creator-gtk"
ICON_EYE_OPEN = "eye-open-negative-filled-symbolic"
ICON_EYE_CLOSED = "eye-not-looking-symbolic"
INSTALL_LOG_DIR = "/var/log/minios"
INSTALL_LOG_PATH = os.path.join(INSTALL_LOG_DIR, "installer.log")

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")

gettext.bindtextdomain(APP_NAME, LOCALE_DIRECTORY)
gettext.textdomain(APP_NAME)
_ = gettext.gettext


def can_navigate_to_viewed_step(step, current_step, viewed_steps, install_running=False):
    return not install_running and step != current_step and step in viewed_steps

NETWORK_VALIDATION_MESSAGES = {
    "IPv4 address is not valid.": _("IPv4 address is not valid."),
    "Network prefix must be between 0 and 32.": _("Network prefix must be between 0 and 32."),
    "Gateway is not a valid IPv4 address.": _("Gateway is not a valid IPv4 address."),
    "DNS server is not a valid IP address.": _("DNS server is not a valid IP address."),
}


def backend_command_for_state(state):
    """Return a shell-safe CLI equivalent of the GUI backend request."""
    command = [
        "/usr/bin/minios-deploy", "install", state.target_device or "",
        "--mode", state.install_mode,
        "--security-profile", state.security_profile,
        "--filesystem", state.filesystem,
        "--placement", state.placement,
        "--swap-size", str(state.swap_size_mib),
        "--alongside-size", str(state.alongside_size_mib),
        "--boot-layout", state.boot_layout,
        "--boot-menu", state.boot_config_type,
        "--modules", ",".join(state.selected_modules),
    ]
    if state.persistence_mode != "none":
        command.extend(["--persistence-mode", state.persistence_mode])
        if state.persistence_size_mib:
            command.extend(["--persistence-size", str(state.persistence_size_mib)])
    if state.download_missing_packages:
        command.append("--download-packages")
    if state.config_override_path:
        command.extend(["--config-file", state.config_override_path])

    user = state.user_config
    safe_user_options = (
        ("--username", user.username),
        ("--full-name", user.full_name),
        ("--hostname", user.hostname),
        ("--locale", user.locale),
        ("--timezone", user.timezone),
        ("--keyboard", user.keyboard),
    )
    for option, value in safe_user_options:
        if value:
            command.extend([option, value])
    if user.password:
        command.extend(["--password", "<redacted>"])
    if user.root_password:
        command.extend(["--root-password", "<redacted>"])
    command.append("--yes")
    return " ".join(shlex.quote(part) for part in command)


def format_log_message(message, timestamp=None):
    """Prefix every physical log line with a timestamp and severity."""
    timestamp = timestamp or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    result = []
    for line in str(message).splitlines() or [""]:
        if line.startswith("ERROR:"):
            level = "ERROR"
        elif line.startswith("Warning:") or line.startswith("WARNING:"):
            level = "WARN"
        elif line.startswith("$") or line.startswith("Backend call:") or line.startswith("Equivalent command:"):
            level = "CMD"
        else:
            level = "INFO"
        result.append("{} [{}] {}".format(timestamp, level, line))
    return "\n".join(result)


def installer_package_version():
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "minios-installer"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except OSError:
        pass
    return "development"


def install_session_summary(state):
    lines = [
        "MiniOS Installer: {}".format(installer_package_version()),
        "Mode: {}".format(state.install_mode),
        "Target: {}".format(state.target_device or "unknown"),
        "Filesystem: {}".format(state.filesystem),
        "Placement: {}".format(state.placement),
        "Boot layout: {}".format(state.boot_layout),
        "Swap: {} MiB".format(state.swap_size_mib),
        "Modules: {}".format(", ".join(state.selected_modules) or "none"),
    ]
    identity = state.target_device_identity or {}
    if identity:
        lines.append(
            "Target identity: model={model}, serial={serial}, size={size}".format(
                model=identity.get("model") or "unknown",
                serial=identity.get("serial") or "unknown",
                size=identity.get("size") or identity.get("size_bytes") or "unknown",
            )
        )
    plan = state.partition_plan
    if plan is not None and hasattr(plan, "partitions"):
        for partition in plan.partitions:
            if partition.role in ("minios_root", "swap"):
                lines.append(
                    "Planned {role}: {size} MiB ({start}-{end} MiB)".format(
                        role=partition.role,
                        size=partition.size_mib,
                        start=partition.start_mib,
                        end=partition.end_mib,
                    )
                )
    return lines

FILESYSTEM_HELP_MARKUP = _(
    "<b>ext4</b> (best choice)\n"
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
    "  + Single partition layout (no separate ESP).\n"
    "  - 4GB single-file limit.\n\n"
    "<b>NTFS</b>\n"
    "  + Windows-compatible with large file support.\n"
    "  - Requires extra tools on some systems."
)


def resolve_css_path():
    for path in (CSS_SYSTEM_PATH, _SHARE_STYLES):
        if os.path.isfile(path):
            return path
    return None


def apply_css_if_exists():
    path = resolve_css_path()
    if not path:
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(path)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def read_available_locales() -> List[str]:
    locales = set()
    try:
        with open("/usr/share/i18n/SUPPORTED", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0].endswith(".UTF-8"):
                    locales.add(parts[0])
    except OSError:
        pass
    return sorted(locales)


_ISO_LANG_NAMES = None  # type: Optional[dict]
_ISO_COUNTRY_NAMES = None  # type: Optional[dict]


def _load_iso_json_map(path, list_key, code_key="alpha_2", name_key="name"):
    """Load iso-codes JSON mapping; empty dict if missing (no hard dependency)."""
    try:
        import json

        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get(list_key) or []
        result = {}
        for row in rows:
            code = (row.get(code_key) or "").strip()
            name = (row.get(name_key) or row.get("official_name") or "").strip()
            if code and name:
                result[code] = name
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def iso_language_names():
    global _ISO_LANG_NAMES
    if _ISO_LANG_NAMES is None:
        _ISO_LANG_NAMES = _load_iso_json_map(
            "/usr/share/iso-codes/json/iso_639-2.json", "639-2", "alpha_2", "name"
        )
    return _ISO_LANG_NAMES


def iso_country_names():
    global _ISO_COUNTRY_NAMES
    if _ISO_COUNTRY_NAMES is None:
        # Prefer short "common" style name when present.
        path = "/usr/share/iso-codes/json/iso_3166-1.json"
        try:
            import json

            with open(path, "r", encoding="utf-8") as fh:
                rows = (json.load(fh).get("3166-1") or [])
            result = {}
            for row in rows:
                code = (row.get("alpha_2") or "").strip()
                name = (row.get("name") or row.get("official_name") or "").strip()
                if code and name:
                    result[code.upper()] = name
            _ISO_COUNTRY_NAMES = result
        except (OSError, ValueError, TypeError, KeyError):
            _ISO_COUNTRY_NAMES = {}
    return _ISO_COUNTRY_NAMES


def format_locale_label(code: str) -> str:
    """
    Human-readable locale label, e.g. "German (Switzerland) — de_CH.UTF-8".

    Uses iso-codes when installed; otherwise falls back to the raw locale code.
    """
    code = (code or "").strip()
    if not code:
        return code
    base = code.replace(".UTF-8", "").replace(".utf8", "")
    parts = base.split("_")
    lang_code = parts[0].lower() if parts else ""
    region = parts[1].upper() if len(parts) > 1 else ""
    lang_name = iso_language_names().get(lang_code) or iso_language_names().get(lang_code[:2])
    if not lang_name:
        # No iso-codes: keep the real code only (avoid useless "de (CH) — de_CH…" noise).
        return code
    if region:
        country = iso_country_names().get(region) or region
        return "{lang} ({country}) — {code}".format(lang=lang_name, country=country, code=code)
    return "{lang} — {code}".format(lang=lang_name, code=code)


def locale_code_from_label(text: str) -> str:
    """Extract locale code from a label or raw code typed by the user."""
    text = (text or "").strip()
    if not text:
        return ""
    if "—" in text:
        return text.split("—")[-1].strip()
    if " - " in text and text.split(" - ")[-1].endswith("UTF-8"):
        return text.split(" - ")[-1].strip()
    return text


def detect_system_locale() -> str:
    """Best-effort locale from the running live session."""
    candidates = []
    for key in ("LC_ALL", "LANG", "LANGUAGE"):
        val = (os.environ.get(key) or "").split(":")[0].strip()
        if val and val not in ("C", "C.UTF-8", "POSIX"):
            candidates.append(val)
    try:
        with open("/etc/default/locale", "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("LANG="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        candidates.append(val)
    except OSError:
        pass
    try:
        out = subprocess.check_output(
            ["localectl", "status"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.splitlines():
            if "System Locale" in line or "LANG=" in line:
                match = re.search(r"LANG=([^\s]+)", line)
                if match:
                    candidates.append(match.group(1).strip())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    available = set(read_available_locales())
    for raw in candidates:
        # LANGUAGE may be "ru" without territory; prefer full UTF-8 locale.
        if raw in available:
            return raw
        if not raw.endswith(".UTF-8"):
            guess = raw.split(".")[0] + ".UTF-8"
            if guess in available:
                return guess
        # Bare language: first matching UTF-8 locale
        lang = raw.split("_")[0].split(".")[0]
        for loc in sorted(available):
            if loc.startswith(lang + "_") and loc.endswith(".UTF-8"):
                return loc
            if loc == lang + ".UTF-8":
                return loc
    return ""


def detect_system_timezone() -> str:
    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        return out
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


# ISO 3166-1 alpha-2 → typical primary UTF-8 locale (offline table, no network).
# Used after GeoIP country code. Not perfect for multilingual countries — user can edit.
_COUNTRY_DEFAULT_LOCALE = {
    "AT": "de_AT.UTF-8",
    "AU": "en_AU.UTF-8",
    "BE": "fr_BE.UTF-8",
    "BR": "pt_BR.UTF-8",
    "BY": "be_BY.UTF-8",
    "CA": "en_CA.UTF-8",
    "CH": "de_CH.UTF-8",
    "CN": "zh_CN.UTF-8",
    "CZ": "cs_CZ.UTF-8",
    "DE": "de_DE.UTF-8",
    "DK": "da_DK.UTF-8",
    "ES": "es_ES.UTF-8",
    "FI": "fi_FI.UTF-8",
    "FR": "fr_FR.UTF-8",
    "GB": "en_GB.UTF-8",
    "GR": "el_GR.UTF-8",
    "HU": "hu_HU.UTF-8",
    "ID": "id_ID.UTF-8",
    "IE": "en_IE.UTF-8",
    "IL": "he_IL.UTF-8",
    "IN": "en_IN.UTF-8",
    "IT": "it_IT.UTF-8",
    "JP": "ja_JP.UTF-8",
    "KR": "ko_KR.UTF-8",
    "KZ": "ru_KZ.UTF-8",
    "MX": "es_MX.UTF-8",
    "NL": "nl_NL.UTF-8",
    "NO": "nb_NO.UTF-8",
    "NZ": "en_NZ.UTF-8",
    "PL": "pl_PL.UTF-8",
    "PT": "pt_PT.UTF-8",
    "RO": "ro_RO.UTF-8",
    "RU": "ru_RU.UTF-8",
    "SE": "sv_SE.UTF-8",
    "SK": "sk_SK.UTF-8",
    "TR": "tr_TR.UTF-8",
    "TW": "zh_TW.UTF-8",
    "UA": "uk_UA.UTF-8",
    "US": "en_US.UTF-8",
    "UZ": "uz_UZ.UTF-8",
}

# ISO country → XKB layouts (comma-separated). Dual layouts common where Latin+local is used.
_COUNTRY_DEFAULT_KEYBOARD = {
    "AT": "de",
    "AU": "us",
    "BE": "be",
    "BR": "br",
    "BY": "us,by",
    "CA": "us",
    "CH": "ch",
    "CN": "cn",
    "CZ": "cz",
    "DE": "de",
    "DK": "dk",
    "ES": "es",
    "FI": "fi",
    "FR": "fr",
    "GB": "gb",
    "GR": "us,gr",
    "HU": "hu",
    "ID": "us",
    "IE": "ie",
    "IL": "us,il",
    "IN": "us,in",
    "IT": "it",
    "JP": "jp",
    "KR": "kr",
    "KZ": "us,kz",
    "MX": "latam",
    "NL": "us",
    "NO": "no",
    "NZ": "us",
    "PL": "pl",
    "PT": "pt",
    "RO": "ro",
    "RU": "us,ru",
    "SE": "se",
    "SK": "sk",
    "TR": "tr",
    "TW": "us",
    "UA": "us,ua",
    "US": "us",
    "UZ": "us,uz",
}


def locale_for_country_code(country_code: str, available=None) -> str:
    """Map ISO country code to a supported UTF-8 locale, if possible."""
    cc = (country_code or "").strip().upper()
    if not cc:
        return ""
    if available is None:
        available = set(read_available_locales())
    else:
        available = set(available)
    preferred = _COUNTRY_DEFAULT_LOCALE.get(cc)
    if preferred and preferred in available:
        return preferred
    # Fallback: any *.UTF-8 locale ending with _CC.
    suffix = "_" + cc + ".UTF-8"
    for loc in sorted(available):
        if loc.endswith(suffix):
            return loc
    return preferred if preferred else ""


def keyboard_for_country_code(country_code: str, available_codes=None) -> str:
    """Map ISO country code to XKB layout list (e.g. us,ru)."""
    cc = (country_code or "").strip().upper()
    if not cc:
        return ""
    preferred = _COUNTRY_DEFAULT_KEYBOARD.get(cc, "")
    if not preferred:
        # Guess single layout from lowercased country code when it is a known XKB layout.
        guess = cc.lower()
        preferred = guess
    if available_codes is None:
        return preferred
    available = set(available_codes)
    kept = [part for part in parse_keyboard_layouts(preferred) if part in available]
    if kept:
        return ",".join(kept)
    # Country code as layout (de, fr, …) if listed in XKB.
    if cc.lower() in available:
        return cc.lower()
    return preferred if not available else ""


def keyboard_for_locale(locale_code: str, available_codes=None) -> str:
    """Derive a keyboard layout suggestion from a locale (ru_RU.UTF-8 → us,ru)."""
    locale_code = (locale_code or "").strip()
    if not locale_code:
        return ""
    base = locale_code.replace(".UTF-8", "").replace(".utf8", "")
    parts = base.split("_")
    lang = (parts[0] or "").lower()
    region = (parts[1] if len(parts) > 1 else "").upper()
    if region:
        by_country = keyboard_for_country_code(region, available_codes)
        if by_country:
            return by_country
    # Language-only fallbacks for common non-Latin scripts (keep us for Latin input).
    lang_map = {
        "ru": "us,ru",
        "uk": "us,ua",
        "be": "us,by",
        "bg": "us,bg",
        "el": "us,gr",
        "he": "us,il",
        "ar": "us,ara",
        "fa": "us,ir",
        "hi": "us,in",
        "ja": "jp",
        "ko": "kr",
        "zh": "cn",
        "de": "de",
        "fr": "fr",
        "es": "es",
        "it": "it",
        "pt": "br" if "BR" in base else "pt",
        "pl": "pl",
        "tr": "tr",
        "en": "us",
    }
    preferred = lang_map.get(lang, lang if lang else "")
    if available_codes is None:
        return preferred
    available = set(available_codes)
    kept = [part for part in parse_keyboard_layouts(preferred) if part in available]
    return ",".join(kept) if kept else (preferred if preferred in available or not available else "")


def detect_system_keyboard() -> str:
    """Best-effort keyboard layouts from the running live session."""
    candidates = []
    # setxkbmap -query → layout: us,ru
    try:
        out = subprocess.check_output(
            ["setxkbmap", "-query"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.splitlines():
            if line.lower().startswith("layout:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    candidates.append(val.replace(" ", ""))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    # localectl status → X11 Layout: us
    try:
        out = subprocess.check_output(
            ["localectl", "status"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in out.splitlines():
            if "X11 Layout" in line or "Layout:" in line:
                match = re.search(r"Layout:\s*(\S+)", line)
                if match:
                    candidates.append(match.group(1).strip().replace(" ", ""))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    # /etc/default/keyboard
    try:
        with open("/etc/default/keyboard", "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("XKBLAYOUT="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        candidates.append(val.replace(" ", ""))
    except OSError:
        pass
    for raw in candidates:
        layouts = parse_keyboard_layouts(raw)
        if layouts:
            return ",".join(layouts)
    return ""


def detect_geoip_location(timeout: float = 3.0) -> dict:
    """
    Best-effort public GeoIP lookup (country + timezone).

    Returns dict with optional keys: country_code, timezone, source.
    Empty dict on failure. No API key; short timeout for installer UX.
    """
    import json

    try:
        from urllib.request import Request, urlopen
    except ImportError:
        return {}

    # HTTPS first; plain HTTP as last resort (some live images lack CA issues either way).
    endpoints = (
        "https://ipapi.co/json/",
        "https://ipinfo.io/json",
        "http://ip-api.com/json/?fields=status,countryCode,timezone",
    )
    headers = {"User-Agent": "minios-installer/3.0 (location-detect)"}
    for url in endpoints:
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            # Normalize provider-specific fields.
            if data.get("status") == "fail":
                continue
            country = (
                data.get("country_code")
                or data.get("countryCode")
                or data.get("country")
                or ""
            )
            if isinstance(country, str) and len(country) > 2:
                # ipinfo uses "country": "DE"
                country = country.strip()
            timezone = data.get("timezone") or data.get("time_zone") or ""
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or ""
            country = (country or "").strip().upper()
            timezone = (timezone or "").strip()
            if country or timezone:
                return {
                    "country_code": country[:2] if country else "",
                    "timezone": timezone,
                    "source": "geoip",
                }
        except Exception:
            continue
    return {}


def detect_location_best_effort(
    available_locales=None,
    available_timezones=None,
    available_keyboard_codes=None,
) -> dict:
    """
    Detect language, timezone, and keyboard for the installed system.

    Strategy:
      1) Network GeoIP (country → locale / keyboard, timezone) when reachable
      2) Live session LANG / timedatectl / setxkbmap as offline fallback

    Returns:
      {
        "locale": str,
        "timezone": str,
        "keyboard": str,           # e.g. us,ru
        "keyboard_options": str,   # grp:* or empty
        "source": "geoip" | "session" | "mixed" | "none",
        "country_code": str,
      }
    """
    from package_preflight import has_internet

    if available_locales is None:
        available_locales = read_available_locales()
    if available_timezones is None:
        available_timezones = read_available_timezones()
    if available_keyboard_codes is None:
        available_keyboard_codes = [code for code, _desc in read_available_keyboard_layouts()]
    available_locales_set = set(available_locales)
    available_tz_set = set(available_timezones)

    locale = ""
    timezone = ""
    keyboard = ""
    country = ""
    sources = set()

    if has_internet(timeout=2.0):
        geo = detect_geoip_location(timeout=3.0)
        country = geo.get("country_code") or ""
        geo_tz = geo.get("timezone") or ""
        geo_locale = locale_for_country_code(country, available_locales_set) if country else ""
        geo_kb = keyboard_for_country_code(country, available_keyboard_codes) if country else ""
        if geo_locale:
            locale = geo_locale
            sources.add("geoip")
        if geo_tz and (not available_tz_set or geo_tz in available_tz_set or "/" in geo_tz):
            timezone = geo_tz
            sources.add("geoip")
        if geo_kb:
            keyboard = geo_kb
            sources.add("geoip")

    # Offline / fill gaps from the running live session.
    if not locale:
        session_locale = detect_system_locale()
        if session_locale:
            locale = session_locale
            sources.add("session")
    if not timezone:
        session_tz = detect_system_timezone()
        if session_tz:
            timezone = session_tz
            sources.add("session")
    if not keyboard:
        session_kb = detect_system_keyboard()
        if session_kb:
            keyboard = session_kb
            sources.add("session")
    # If still no keyboard, derive from detected locale (e.g. ru_RU → us,ru).
    if not keyboard and locale:
        derived = keyboard_for_locale(locale, available_keyboard_codes)
        if derived:
            keyboard = derived

    keyboard_options = ""
    if keyboard and keyboard_needs_layout_switch(keyboard):
        keyboard_options = "grp:alt_shift_toggle"

    if not sources:
        source = "none"
    elif sources == {"geoip"}:
        source = "geoip"
    elif sources == {"session"}:
        source = "session"
    else:
        source = "mixed"

    return {
        "locale": locale,
        "timezone": timezone,
        "keyboard": keyboard,
        "keyboard_options": keyboard_options,
        "source": source,
        "country_code": country,
    }


def read_available_timezones() -> List[str]:
    try:
        from zoneinfo import available_timezones

        return sorted(available_timezones())
    except ImportError:
        zones = set()
        tzdir = "/usr/share/zoneinfo"
        for root, _dirs, files in os.walk(tzdir):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), tzdir)
                if rel in ("posixrules", "localtime", "leapseconds", "tzdata.zi"):
                    continue
                if rel.startswith(("posix/", "right/")):
                    continue
                zones.add(rel)
        return sorted(zones)


def read_available_keyboard_layouts() -> List[Tuple[str, str]]:
    """Return list of (code, description) from XKB base.lst."""
    layouts = []
    seen = set()
    try:
        with open("/usr/share/X11/xkb/rules/base.lst", "r", encoding="utf-8") as fh:
            in_layout_section = False
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("!"):
                    in_layout_section = stripped == "! layout"
                    continue
                if in_layout_section and stripped:
                    parts = stripped.split(None, 1)
                    code = parts[0]
                    desc = parts[1] if len(parts) > 1 else code
                    if code not in seen:
                        seen.add(code)
                        layouts.append((code, desc))
    except OSError:
        pass
    return sorted(layouts, key=lambda item: item[0])


def match_completion_by_token(completion, _key, tree_iter, entry):
    text = entry.get_text()
    cursor_pos = entry.get_position()
    segment = text[:cursor_pos].rpartition(",")[2].lstrip().lower()
    if not segment:
        return True
    candidate = completion.get_model()[tree_iter][0]
    return candidate.lower().startswith(segment)


def on_completion_selected(_completion, model, tree_iter, entry):
    full_text = entry.get_text()
    cursor_pos = entry.get_position()
    comma_index = full_text[:cursor_pos].rfind(",") + 1
    prefix = full_text[:comma_index]
    if prefix and not prefix.endswith(" "):
        prefix += " "
    suffix = full_text[cursor_pos:]
    candidate = model[tree_iter][0]
    entry.set_text(prefix + candidate + suffix)
    entry.set_position(len(prefix) + len(candidate))
    return True


def create_completion(items, entry):
    completion = Gtk.EntryCompletion()
    store = Gtk.ListStore(str)
    for item in items:
        store.append([item])
    completion.set_model(store)
    completion.set_text_column(0)
    completion.set_inline_completion(True)
    completion.set_popup_completion(True)
    completion.set_popup_single_match(False)
    completion.set_match_func(match_completion_by_token, entry)
    completion.connect("match-selected", on_completion_selected, entry)
    return completion


def describe_module_name(name):
    lower = name.lower()
    stem = re.sub(r"\.sb$", "", name)
    stem = re.sub(r"^[0-9]+-", "", stem)
    stem = stem.replace("-amd64", "").replace("_", "-")
    friendly = stem.replace("-", " ").title()
    if "core" in lower:
        role = _("Core system")
    elif "kernel" in lower:
        role = _("Kernel and drivers")
    elif "firmware" in lower:
        role = _("Hardware firmware")
    elif "gui-base" in lower:
        role = _("Graphical base")
    elif "desktop" in lower or "xfce" in lower or "kde" in lower or "gnome" in lower or "lxqt" in lower:
        role = _("Desktop environment")
    elif "toolbox" in lower:
        role = _("Toolbox utilities")
    elif "ultra" in lower:
        role = _("Ultra applications")
    elif "apps" in lower or "applications" in lower:
        role = _("Application bundle")
    elif "firefox" in lower or "browser" in lower:
        role = _("Web browser")
    else:
        role = _("Custom module")
    return role, friendly, name


def format_module_size(size):
    if size is None:
        return _("Size unavailable")
    if size >= 1024 * 1024 * 1024:
        return "{:.1f} GiB".format(size / (1024.0 * 1024.0 * 1024.0))
    if size >= 1024 * 1024:
        return "{:.0f} MiB".format(size / (1024.0 * 1024.0))
    return "{:.0f} KiB".format(size / 1024.0)


def parse_keyboard_layouts(value):
    """Split LIVE_KEYBOARD_LAYOUTS-style value into layout codes."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def keyboard_needs_layout_switch(layouts_value):
    """True when two or more layouts are configured (switch shortcut is useful)."""
    return len(parse_keyboard_layouts(layouts_value)) >= 2


def normalize_keyboard_switch_option(value, layouts_value=None):
    """
    Normalize XKB group-switch option.

    Empty string means disabled. When *layouts_value* is given and only one
    layout is selected, always return empty (no switch needed).
    """
    if layouts_value is not None and not keyboard_needs_layout_switch(layouts_value):
        return ""
    value = (value or "").strip()
    if not value or value in ("none", "disabled", "Disabled"):
        return ""
    known = (
        "grp:alt_shift_toggle",
        "grp:ctrl_shift_toggle",
        "grp:win_space_toggle",
        "grp:caps_toggle",
    )
    for option in known:
        if option in value.split(","):
            return option
    # Unknown non-empty value: keep a sensible multi-layout default only
    # when switch is actually needed.
    if layouts_value is not None and keyboard_needs_layout_switch(layouts_value):
        return "grp:alt_shift_toggle"
    return ""


def install_phase_for_percent(percent):
    if percent < 10:
        return _("Preparing")
    if percent < 35:
        return _("Partitioning")
    if percent < 80:
        return _("Copying files")
    if percent < 95:
        return _("Bootloader")
    return _("Finishing")


class InstallerWindow(Gtk.ApplicationWindow):
    # Mode early so Users can adapt (live defaults vs required native account).
    STEPS = [
        ("welcome", _("Welcome")),
        ("mode", _("Mode")),
        ("security", _("Security")),
        ("location", _("Location")),
        ("network", _("Network")),
        ("keyboard", _("Keyboard")),
        ("users", _("Users")),
        ("modules", _("Modules")),
        ("partitioning", _("Partitioning")),
        ("summary", _("Summary")),
        ("install", _("Install")),
    ]

    def __init__(self, application):
        super().__init__(application=application, title=_(APP_TITLE))
        header = Gtk.HeaderBar(show_close_button=True)
        header.set_has_subtitle(False)
        header.get_style_context().add_class("minios-headerbar")
        header.props.title = _(APP_TITLE)
        self.set_titlebar(header)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name(ICON_WINDOW)
        apply_css_if_exists()
        # Cap growth to the monitor workarea so expanders never push under the panel.
        self._apply_window_size_limits()

        self.state = InstallState(boot_config_type=self._get_default_boot_config())
        self.state.download_missing_packages = True
        self.available_locales = read_available_locales()
        self.available_timezones = read_available_timezones()
        self.available_keyboard_layouts = read_available_keyboard_layouts()
        self.available_modules = discover_module_names()
        self.module_sizes_by_mode = {"live": {}, "native": {}}
        self.payload_overhead_by_mode = {"live": 0, "native": 0}
        self.module_sizes_ready = False
        self._load_current_live_config()
        self._live_username_default = self.state.user_config.username or "live"
        self.current_step = 0
        self.viewed_steps = set()
        self.disk_rows = {}
        self.install_running = False
        self.install_log_path = INSTALL_LOG_PATH
        self._install_log_lock = threading.Lock()
        self.log_view = None
        self.native_allow_root = False
        self._users_password_confirm = ""
        self._users_root_confirm = ""

        self.root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add(self.root)

        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.sidebar.get_style_context().add_class("installer-sidebar")
        self.sidebar.set_size_request(180, -1)
        self.sidebar.set_margin_top(8)
        self.sidebar.set_margin_bottom(8)
        self.sidebar.set_margin_start(6)
        self.sidebar.set_margin_end(4)
        self.root.pack_start(self.sidebar, False, False, 0)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.set_margin_top(12)
        self.content.set_margin_bottom(10)
        self.content.set_margin_start(12)
        self.content.set_margin_end(14)
        self.root.pack_start(self.content, True, True, 0)

        # Scroll step body instead of growing the window when Advanced / root fields open.
        # Important: children inside a Viewport ignore expand flags and get natural height only.
        # Nested lists must set min_content_height or they collapse to ~one row ("скукожились").
        self.content_scroll = Gtk.ScrolledWindow()
        self.content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.content_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.content_scroll.set_vexpand(True)
        self.content_scroll.set_hexpand(True)
        # Classic scrollbar takes its own lane — frames must not sit under an overlay bar.
        if hasattr(self.content_scroll, "set_overlay_scrolling"):
            self.content_scroll.set_overlay_scrolling(False)
        if hasattr(self.content_scroll, "set_propagate_natural_height"):
            self.content_scroll.set_propagate_natural_height(False)
        if hasattr(self.content_scroll, "set_propagate_natural_width"):
            self.content_scroll.set_propagate_natural_width(False)

        self.content_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content_body.set_valign(Gtk.Align.FILL)
        self.content_body.set_hexpand(True)
        # Keep a little room so frame borders are not clipped by the scrollbar trough.
        self.content_body.set_margin_end(6)
        self.content_scroll.add(self.content_body)
        self.content.pack_start(self.content_scroll, True, True, 0)

        self.content_footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.pack_end(self.content_footer, False, False, 0)

        self._build_sidebar()
        self._show_step(0)
        self._start_module_size_calculation()
        start_disk_monitoring(self._refresh_disks_idle)
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)
        self._did_initial_resize = False

    def _start_module_size_calculation(self):
        """Precompute both modes before the user reaches the Modules step."""
        module_names = list(self.available_modules)

        def work():
            sizes = {
                "live": calculate_module_sizes(module_names, install_mode="live"),
                "native": calculate_module_sizes(module_names, install_mode="native"),
            }
            overhead = {
                "live": payload_overhead_bytes(module_names, install_mode="live"),
                "native": payload_overhead_bytes(module_names, install_mode="native"),
            }
            GLib.idle_add(self._finish_module_size_calculation, sizes, overhead)

        threading.Thread(target=work, daemon=True).start()

    def _finish_module_size_calculation(self, sizes, overhead):
        self.module_sizes_by_mode = sizes
        self.payload_overhead_by_mode = overhead
        self.module_sizes_ready = True
        self._update_required_root_size()
        step = self.STEPS[self.current_step][0]
        if step == "modules":
            self._show_step(self.current_step)
        elif step == "partitioning":
            self._refresh_free_space_placement()
            self._refresh_alongside_placement()
            self._update_partition_preview()
        return False

    def _update_required_root_size(self):
        selected = self.state.selected_modules or self.available_modules
        sizes = self.module_sizes_by_mode.get(self.state.install_mode, {})
        total = selected_modules_size_bytes(selected, sizes)
        overhead = self.payload_overhead_by_mode.get(self.state.install_mode)
        if total is not None and overhead is not None:
            total += overhead
        if total is None:
            self.state.required_root_mib = 0
            return None
        persistence_mib = self.state.persistence_size_mib if (
            self.state.install_mode == "live" and
            self.state.persistence_mode in ("dynfilefs", "raw", "luks")
        ) else 0
        self.state.required_root_mib = required_root_mib(total) + persistence_mib
        return total

    def _workarea_size(self):
        """Return (width, height) of the primary monitor workarea (excludes panels)."""
        screen = self.get_screen() or Gdk.Screen.get_default()
        if screen is None:
            return 1280, 800
        try:
            monitor = screen.get_primary_monitor()
            rect = screen.get_monitor_workarea(monitor)
            return int(rect.width), int(rect.height)
        except Exception:
            try:
                return int(screen.get_width()), int(screen.get_height())
            except Exception:
                return 1280, 800

    def _apply_window_size_limits(self):
        """Set an initial size and a usable minimum; the WM owns later resizing."""
        work_w, work_h = self._workarea_size()
        # Margin for window decorations / shadow so the frame stays above the panel.
        margin = 48
        max_w = max(760, work_w - margin)
        max_h = max(480, work_h - margin)
        self._default_window_width = min(860, max_w)
        self._default_window_height = min(560, max_h)
        self.set_default_size(self._default_window_width, self._default_window_height)
        self.set_size_request(min(720, max_w), min(460, max_h))

        geom = Gdk.Geometry()
        geom.min_width = min(720, self._default_window_width)
        geom.min_height = min(460, max_h)
        self.set_geometry_hints(
            None,
            geom,
            Gdk.WindowHints.MIN_SIZE,
        )

    def _clamp_window_size(self):
        """Compatibility callback: content updates must not resize the toplevel."""
        return False

    def _pack_scrollable_area(self, widget, min_height=220):
        """
        Pack a ScrolledWindow (or any tall widget) into content_body with a
        usable minimum height. Required because the outer content ScrolledWindow
        puts content_body in a Viewport where expand=True does not grow children.
        """
        if isinstance(widget, Gtk.ScrolledWindow):
            if hasattr(widget, "set_min_content_height"):
                widget.set_min_content_height(min_height)
            widget.set_size_request(-1, min_height)
            widget.set_vexpand(True)
            widget.set_hexpand(True)
        self.content_body.pack_start(widget, True, True, 0)
        return widget

    def _on_map_clamp_size(self, *_args):
        self._did_initial_resize = True
        return False

    @property
    def cancel_requested(self):
        return self.state.cancel_requested

    @cancel_requested.setter
    def cancel_requested(self, value):
        self.state.cancel_requested = value

    def _get_default_boot_config(self):
        try:
            with open("/proc/cmdline", "r", encoding="utf-8") as fh:
                match = re.search(r"locales=([^\s]+)", fh.read())
            if match:
                code = match.group(1).split(".")[0]
                if code != "en_US":
                    return code
        except OSError:
            pass
        return "multilang"

    def _load_current_live_config(self):
        values = load_config_values("/etc/live/config.conf")
        self.state.user_config.username = values.get("LIVE_USERNAME", "live")
        self.state.user_config.full_name = values.get("LIVE_USER_FULLNAME", "MiniOS User")
        self._live_fullname_default = self.state.user_config.full_name or "MiniOS User"
        self.state.user_config.hostname = values.get("LIVE_HOSTNAME", "minios")
        self.state.user_config.locale = values.get("LIVE_LOCALES", "")
        self.state.user_config.timezone = values.get("LIVE_TIMEZONE", "")
        self.state.user_config.keyboard = values.get("LIVE_KEYBOARD_LAYOUTS", "")
        self.state.user_config.keyboard_options = normalize_keyboard_switch_option(
            values.get("LIVE_KEYBOARD_OPTIONS", ""),
            layouts_value=self.state.user_config.keyboard,
        )
        self.state.user_config.default_target = values.get("DEFAULT_TARGET", "graphical.target")
        self.state.user_config.enable_services = values.get("ENABLE_SERVICES", "")
        self.state.user_config.disable_services = values.get("DISABLE_SERVICES", "")
        self.state.user_config.config_cmdline = values.get("LIVE_CONFIG_CMDLINE", "")

    def _mark_config_customized(self):
        self.state.user_config_customized = True

    def _set_user_config(self, attr, value):
        setattr(self.state.user_config, attr, value.strip() if isinstance(value, str) else value)
        self._mark_config_customized()

    def _attach_completion(self, entry, items):
        if items:
            entry.set_completion(create_completion(items, entry))

    def _create_combo_with_entry(self, items, current_value, placeholder, on_changed):
        combo = Gtk.ComboBoxText.new_with_entry()
        combo.set_hexpand(True)
        for item in items:
            combo.append_text(item)
        entry = combo.get_child()
        entry.set_placeholder_text(placeholder)
        if items:
            self._attach_completion(entry, items)
        if current_value in items:
            combo.set_active(items.index(current_value))
        elif current_value:
            entry.set_text(current_value)
        combo.connect("changed", lambda widget: on_changed(widget.get_active_text() or entry.get_text()))
        entry.connect("changed", lambda widget: on_changed(widget.get_text()))
        return combo

    def _create_password_entry(self, placeholder, on_changed, initial=""):
        """
        Password field + hold-to-show eye button.

        - Tab skips the eye (can_focus=False) and moves to the next entry.
        - Password is visible only while the eye is pressed; release/leave hides it.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_hexpand(True)
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_hexpand(True)
        entry.set_placeholder_text(placeholder)
        if initial:
            entry.set_text(initial)

        button = Gtk.Button()
        button.set_size_request(34, 30)
        button.set_image(Gtk.Image.new_from_icon_name(ICON_EYE_OPEN, Gtk.IconSize.BUTTON))
        button.set_always_show_image(True)
        button.set_tooltip_text(_("Hold to show password"))
        # Tab must jump entry → next entry, not to the eye.
        button.set_can_focus(False)
        button.set_focus_on_click(False)

        def set_visible(visible):
            entry.set_visibility(bool(visible))
            icon_name = ICON_EYE_CLOSED if visible else ICON_EYE_OPEN
            button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))

        def on_pressed(_btn):
            set_visible(True)

        def on_released(_btn):
            set_visible(False)

        def on_leave(_btn, _event):
            # Mouse left the button while held — hide immediately.
            set_visible(False)
            return False

        button.connect("pressed", on_pressed)
        button.connect("released", on_released)
        button.connect("leave-notify-event", on_leave)
        entry.connect("changed", lambda widget: on_changed(widget.get_text()))
        box.pack_start(entry, True, True, 0)
        box.pack_start(button, False, False, 0)
        return box, entry

    def _on_delete_event(self, *_args):
        if not self.install_running:
            return False
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=_("Cancel installation?"),
        )
        dlg.format_secondary_text(
            _("The install may already have written to the selected disk. Cancel requests a cooperative stop.")
        )
        dlg.add_button(_("Keep Installing"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Cancel Install"), Gtk.ResponseType.OK)
        response = dlg.run()
        dlg.destroy()
        if response == Gtk.ResponseType.OK:
            self.state.cancel_requested = True
            if hasattr(self, "status"):
                self.status.set_text(_("Cancel requested. Waiting for the current step to finish..."))
        return True

    def _on_destroy(self, *_args):
        stop_disk_monitoring(self._refresh_disks_idle)
        override = getattr(self.state, "config_override_path", None)
        if override and os.path.isfile(override):
            try:
                os.unlink(override)
            except OSError:
                pass
            self.state.config_override_path = None
        self.get_application().quit()

    def _current_step_name(self):
        if 0 <= self.current_step < len(self.STEPS):
            return self.STEPS[self.current_step][0]
        return ""

    def _refresh_disks_idle(self):
        GLib.idle_add(self._refresh_disks)

    def _build_sidebar(self):
        self.step_rows = []
        for idx, (_name, label) in enumerate(self.STEPS):
            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(_("Go to {step}").format(step=label))
            button.get_style_context().add_class("sidebar-step")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            marker = Gtk.Label(xalign=0)
            marker.set_width_chars(2)
            text = Gtk.Label(label=label, xalign=0)
            text.set_line_wrap(True)
            text.set_max_width_chars(16)
            row.pack_start(marker, False, False, 0)
            row.pack_start(text, True, True, 0)
            button.add(row)
            button.connect("clicked", self._on_sidebar_step_clicked, idx)
            self.sidebar.pack_start(button, False, False, 0)
            self.step_rows.append((button, marker, text))
        self._update_sidebar()

    def _on_sidebar_step_clicked(self, _button, step):
        if not can_navigate_to_viewed_step(step, self.current_step, self.viewed_steps, self.install_running):
            return
        self._show_step(step)

    def _update_sidebar(self):
        for idx, (button, marker, text) in enumerate(self.step_rows):
            ctx = button.get_style_context()
            for cls in ("sidebar-step-active", "sidebar-step-done", "sidebar-step-todo"):
                ctx.remove_class(cls)
            label = self.STEPS[idx][1]
            viewed = idx in self.viewed_steps
            button.set_sensitive(
                can_navigate_to_viewed_step(idx, self.current_step, self.viewed_steps, self.install_running)
            )
            if idx == self.current_step:
                ctx.add_class("sidebar-step-active")
                marker.set_text("●")
                text.set_markup("<b>{}</b>".format(GLib.markup_escape_text(label)))
            elif viewed:
                ctx.add_class("sidebar-step-done")
                marker.set_text("✓")
                text.set_text(label)
            else:
                ctx.add_class("sidebar-step-todo")
                marker.set_text(str(idx + 1))
                text.set_text(label)

    def _clear_content(self):
        for child in self.content_body.get_children():
            self.content_body.remove(child)
        for child in self.content_footer.get_children():
            self.content_footer.remove(child)

    def _page_title(self, title, subtitle=""):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(xalign=0)
        label.get_style_context().add_class("page-title")
        label.set_markup(
            "<span size='x-large' weight='bold'>{}</span>".format(GLib.markup_escape_text(title))
        )
        box.pack_start(label, False, False, 0)
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0)
            sub.set_line_wrap(True)
            sub.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            sub.set_width_chars(64)
            sub.set_max_width_chars(64)
            sub.get_style_context().add_class("page-subtitle")
            sub.get_style_context().add_class("dim-label")
            box.pack_start(sub, False, False, 0)
        self.content_body.pack_start(box, False, False, 0)

    def _style_button(self, button, suggested=False, destructive=False):
        button.set_size_request(104, 32)
        ctx = button.get_style_context()
        ctx.add_class("installer-button")
        if suggested:
            ctx.add_class("suggested-action")
        if destructive:
            ctx.add_class("destructive-action")
        return button

    def _nav(self, can_next=True, next_label=None, destructive_next=False):
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.get_style_context().add_class("installer-nav-separator")
        self.content_footer.pack_start(sep, False, False, 0)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        nav.set_margin_top(6)

        back = self._style_button(Gtk.Button(label=_("Back")))
        back.set_sensitive(self.current_step > 0 and not self.install_running)
        back.connect("clicked", lambda *_: self._show_step(self.current_step - 1))
        nav.pack_start(back, False, False, 0)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        nav.pack_start(spacer, True, True, 0)

        nxt = self._style_button(
            Gtk.Button(label=next_label or _("Next")),
            suggested=not destructive_next,
            destructive=destructive_next,
        )
        nxt.set_sensitive(can_next)
        if next_label and len(next_label) > 12:
            nxt.set_size_request(-1, 32)

        def go_next(button):
            if self.install_running:
                return
            button.set_sensitive(False)
            self._show_step(self.current_step + 1)

        nxt.connect("clicked", go_next)
        self.next_button = nxt
        nav.pack_start(nxt, False, False, 0)
        self.content_footer.pack_start(nav, False, False, 0)

    def _show_step(self, step):
        if self.install_running:
            return
        self.current_step = max(0, min(step, len(self.STEPS) - 1))
        self.viewed_steps.add(self.current_step)
        self._clear_content()
        self._update_sidebar()
        name = self.STEPS[self.current_step][0]
        getattr(self, "_step_{}".format(name))()
        self.show_all()
        # Expanding content must not push the window under the desktop panel.
        GLib.idle_add(self._clamp_window_size)

    # --- Steps -----------------------------------------------------------------

    def _step_welcome(self):
        self._page_title(
            _("Install MiniOS"),
            _("A guided installer for copying MiniOS to a disk with a clear preview before any destructive action."),
        )

        card = Gtk.Frame()
        card.get_style_context().add_class("content-card")
        card.set_margin_top(12)
        card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card_box.set_margin_top(18)
        card_box.set_margin_bottom(18)
        card_box.set_margin_start(18)
        card_box.set_margin_end(18)

        image = Gtk.Image.new_from_icon_name(ICON_WINDOW, Gtk.IconSize.DIALOG)
        image.set_pixel_size(96)
        image.set_halign(Gtk.Align.CENTER)
        card_box.pack_start(image, False, False, 0)

        lead = Gtk.Label(xalign=0)
        lead.set_line_wrap(True)
        lead.set_markup(
            "<b>{}</b>".format(
                GLib.markup_escape_text(_("This wizard will guide you through the installation."))
            )
        )
        card_box.pack_start(lead, False, False, 0)

        features = (
            (
                "system-software-install-symbolic",
                _("Choose installation type"),
                _("Install a live MiniOS system or a full native system."),
            ),
            (
                "drive-harddisk-symbolic",
                _("Select modules and disk layout"),
                _("Pick what to install and where it should be written."),
            ),
            (
                "emblem-ok-symbolic",
                _("Review before writing"),
                _("No disk changes are made until the final confirmation."),
            ),
        )
        for icon_name, title, text in features:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR)
            row.pack_start(icon, False, False, 0)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label(xalign=0)
            t.set_markup("<b>{}</b>".format(GLib.markup_escape_text(title)))
            d = Gtk.Label(label=text, xalign=0)
            d.set_line_wrap(True)
            d.get_style_context().add_class("dim-label")
            labels.pack_start(t, False, False, 0)
            labels.pack_start(d, False, False, 0)
            row.pack_start(labels, True, True, 0)
            card_box.pack_start(row, False, False, 0)

        card.add(card_box)
        self.content_body.pack_start(card, False, False, 0)
        self._nav(True)

    def _step_mode(self):
        self._page_title(
            _("Installation Mode"),
            _("Choose whether MiniOS should keep its live module layout or be installed as a regular Linux system."),
        )

        self.mode_cards = {}
        cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cards_box.set_margin_top(8)
        # Keep both option cards the same height so live/native selection does not reflow.
        card_height_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.VERTICAL)

        choices = (
            (
                "live",
                _("Live system installation"),
                _(
                    "Portable MiniOS with modules. Root account stays like the live image "
                    "(default root password is toor unless you change it)."
                ),
            ),
            (
                "native",
                _("Full installation"),
                _(
                    "Regular Linux install. You create a user account; root is locked by default "
                    "and you use sudo."
                ),
            ),
        )
        radio_group = None
        for mode, title, desc in choices:
            frame = Gtk.Frame()
            frame.get_style_context().add_class("choice-card")
            selected = self.state.install_mode == mode or (
                mode == "live" and self.state.install_mode != "native"
            )
            if selected:
                frame.get_style_context().add_class("choice-card-selected")
            event = Gtk.EventBox()
            event.set_visible_window(False)
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            inner.set_margin_top(12)
            inner.set_margin_bottom(12)
            inner.set_margin_start(12)
            inner.set_margin_end(12)
            radio = Gtk.RadioButton.new_from_widget(radio_group)
            if radio_group is None:
                radio_group = radio
            radio.set_active(selected)
            radio.set_valign(Gtk.Align.START)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            t = Gtk.Label(xalign=0)
            t.set_markup("<b>{}</b>".format(GLib.markup_escape_text(title)))
            d = Gtk.Label(label=desc, xalign=0)
            d.set_line_wrap(True)
            d.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            d.set_max_width_chars(52)
            d.set_hexpand(True)
            # Reserve two lines so both cards share the same vertical rhythm.
            try:
                d.set_lines(2)
            except AttributeError:
                pass
            d.get_style_context().add_class("dim-label")
            texts.pack_start(t, False, False, 0)
            texts.pack_start(d, True, True, 0)
            inner.pack_start(radio, False, False, 0)
            inner.pack_start(texts, True, True, 0)
            event.add(inner)
            frame.add(event)
            card_height_group.add_widget(frame)
            self.mode_cards[mode] = (frame, radio)

            def make_handlers(m, r, f):
                def on_toggle(btn):
                    if btn.get_active():
                        self._set_install_mode(m)

                def on_click(_widget, _event):
                    r.set_active(True)
                    return False

                r.connect("toggled", on_toggle)
                f.connect("button-press-event", on_click)

            make_handlers(mode, radio, event)
            cards_box.pack_start(frame, False, False, 0)

        self.content_body.pack_start(cards_box, False, False, 0)

        # Boot settings: fixed labels/rows for both modes (no layout swap).
        boot = Gtk.Frame(label=_("Boot Settings"))
        boot.get_style_context().add_class("content-card")
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.set_margin_top(14)
        grid.set_margin_bottom(14)
        grid.set_margin_start(14)
        grid.set_margin_end(14)
        label_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        field_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        self.language_combo = Gtk.ComboBoxText()
        self.language_combo.set_hexpand(True)
        for code, name in [
            ("multilang", _("Multilingual menu")),
            ("en_US", "English"),
            ("ru_RU", "Русский"),
            ("de_DE", "Deutsch"),
            ("es_ES", "Español"),
            ("fr_FR", "Français"),
            ("it_IT", "Italiano"),
            ("id_ID", "Bahasa Indonesia"),
            ("pt_BR", "Português (Brasil)"),
            ("pt_PT", "Português (Portugal)"),
        ]:
            self.language_combo.append(code, name)
        if not self.language_combo.set_active_id(self.state.boot_config_type):
            self.language_combo.set_active_id("multilang")
            self.state.boot_config_type = "multilang"
        self.language_combo.connect(
            "changed",
            lambda combo: setattr(self.state, "boot_config_type", combo.get_active_id() or "multilang"),
        )

        target_combo = Gtk.ComboBoxText()
        target_combo.set_hexpand(True)
        for value, label in [
            ("graphical.target", _("Graphical desktop")),
            ("multi-user.target", _("Text console")),
        ]:
            target_combo.append(value, label)
        if not target_combo.set_active_id(self.state.user_config.default_target):
            target_combo.set_active_id("graphical.target")
            self.state.user_config.default_target = "graphical.target"
        target_combo.connect(
            "changed",
            lambda combo: self._set_user_config("default_target", combo.get_active_id() or "graphical.target"),
        )

        lang_label = Gtk.Label(label=_("Boot menu language:"), xalign=0)
        lang_label.set_valign(Gtk.Align.CENTER)
        startup_label = Gtk.Label(label=_("Default startup:"), xalign=0)
        startup_label.set_valign(Gtk.Align.CENTER)
        label_group.add_widget(lang_label)
        label_group.add_widget(startup_label)

        lang_field = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lang_field.pack_start(self.language_combo, True, True, 0)
        self.boot_lang_info = Gtk.Image.new_from_icon_name("dialog-information", Gtk.IconSize.BUTTON)
        self.boot_lang_info.set_valign(Gtk.Align.CENTER)
        # Tooltip always available; wording covers both live and native fallback cases.
        self.boot_lang_info.set_tooltip_text(
            _(
                "Language of the boot menu on the installed system. "
                "For a full install this is also used if the installer cannot set up the standard GRUB bootloader."
            )
        )
        lang_field.pack_start(self.boot_lang_info, False, False, 0)
        field_group.add_widget(lang_field)
        field_group.add_widget(target_combo)

        grid.attach(lang_label, 0, 0, 1, 1)
        grid.attach(lang_field, 1, 0, 1, 1)
        grid.attach(startup_label, 0, 1, 1, 1)
        grid.attach(target_combo, 1, 1, 1, 1)
        boot.add(grid)
        boot.set_margin_top(16)
        self.content_body.pack_start(boot, False, False, 0)
        self._nav(True)

    def _refresh_mode_card_styles(self):
        for mode, (frame, _radio) in getattr(self, "mode_cards", {}).items():
            ctx = frame.get_style_context()
            if self.state.install_mode == mode:
                ctx.add_class("choice-card-selected")
            else:
                ctx.remove_class("choice-card-selected")

    def _set_install_mode(self, mode):
        if self.state.install_mode == mode:
            self._refresh_mode_card_styles()
            return
        self.state.set_install_mode(mode)
        self._update_required_root_size()
        if mode == "native":
            # Prefer empty credentials so user must choose a real account.
            if self.state.user_config.username == self._live_username_default:
                self.state.user_config.username = ""
            # Do not prefill full name from the live image for a native install.
            self.state.user_config.full_name = ""
            self.state.user_config.password = ""
            self.state.user_config.root_password = ""
            self._users_password_confirm = ""
            self._users_root_confirm = ""
            self.native_allow_root = False
        else:
            if not self.state.user_config.username:
                self.state.user_config.username = self._live_username_default
            if not self.state.user_config.full_name:
                self.state.user_config.full_name = getattr(self, "_live_fullname_default", "") or ""
            self.native_allow_root = False
        if hasattr(self, "fs_combo"):
            self._refresh_filesystem_choices()
        if hasattr(self, "swap_spin"):
            self.swap_spin.set_sensitive(mode == "native")
            if mode != "native":
                self.swap_spin.set_value(0)
                self.state.swap_size_mib = 0
        # Only selection chrome changes — boot form stays identical.
        self._refresh_mode_card_styles()

    def _profile_label(self, profile):
        labels = {
            "convenient": _("Convenient"),
            "balanced": _("Balanced"),
            "strict": _("Strict"),
        }
        return labels.get(profile, profile)

    def _profile_description(self, profile):
        descriptions = {
            "convenient": _("Keeps MiniOS easy to use: passwordless sudo/polkit, relaxed desktop access, and visible default password hints."),
            "balanced": _("Recommended for regular installs: prevents live-config from setting up autologin and requires passwords for local administration while keeping practical remote-access policy."),
            "strict": _("Harder posture: prevents live-config from setting up autologin, disables SSH root login and SSH password authentication, hides password hints, hardens XRDP, and strips risky groups."),
        }
        return descriptions.get(profile, "")

    def _step_security(self):
        self._page_title(
            _("Security Profile"),
            _("Choose the security posture to apply during installation."),
        )
        cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cards_box.set_margin_top(8)
        self.security_cards = {}
        radio_group = None
        for profile in SECURITY_PROFILE_IDS:
            frame = Gtk.Frame()
            frame.get_style_context().add_class("choice-card")
            if self.state.security_profile == profile:
                frame.get_style_context().add_class("choice-card-selected")
            event = Gtk.EventBox()
            event.set_visible_window(False)
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            inner.set_margin_top(12)
            inner.set_margin_bottom(12)
            inner.set_margin_start(12)
            inner.set_margin_end(12)
            radio = Gtk.RadioButton.new_from_widget(radio_group)
            if radio_group is None:
                radio_group = radio
            radio.set_active(self.state.security_profile == profile)
            radio.set_valign(Gtk.Align.START)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            title = Gtk.Label(xalign=0)
            title.set_markup("<b>{}</b>".format(GLib.markup_escape_text(self._profile_label(profile))))
            desc = Gtk.Label(label=self._profile_description(profile), xalign=0)
            desc.set_line_wrap(True)
            desc.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            desc.set_max_width_chars(58)
            desc.get_style_context().add_class("dim-label")
            texts.pack_start(title, False, False, 0)
            texts.pack_start(desc, False, False, 0)
            inner.pack_start(radio, False, False, 0)
            inner.pack_start(texts, True, True, 0)
            event.add(inner)
            frame.add(event)
            self.security_cards[profile] = (frame, radio)

            def make_handlers(p, r, f):
                def on_toggle(btn):
                    if btn.get_active():
                        self.state.security_profile = p
                        for key, (card, _radio) in self.security_cards.items():
                            ctx = card.get_style_context()
                            if key == p:
                                ctx.add_class("choice-card-selected")
                            else:
                                ctx.remove_class("choice-card-selected")

                def on_click(_widget, _event):
                    r.set_active(True)
                    return False

                r.connect("toggled", on_toggle)
                f.connect("button-press-event", on_click)

            make_handlers(profile, radio, event)
            cards_box.pack_start(frame, False, False, 0)

        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        note.set_text(
            _("Live installs write the individual settings to config.conf for the next boot. The autologin option prevents new setup; it does not remove autologin already configured in a persistent session. Full installs apply the selected posture directly to the target system.")
        )
        cards_box.pack_start(note, False, False, 8)

        available_remote_services = available_remote_access_services()
        remote_frame = Gtk.Frame()
        remote_frame.get_style_context().add_class("summary-card")
        remote_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        remote_box.set_margin_top(10)
        remote_box.set_margin_bottom(10)
        remote_box.set_margin_start(10)
        remote_box.set_margin_end(10)
        remote_title = Gtk.Label(xalign=0)
        remote_title.set_markup("<b>{}</b>".format(GLib.markup_escape_text(_("Incoming remote access"))))
        def update_remote_service(service, active):
            enable, disable = set_service_enabled(
                self.state.user_config.enable_services,
                self.state.user_config.disable_services,
                service,
                active,
            )
            self.state.user_config.enable_services = enable
            self.state.user_config.disable_services = disable
            self.state.user_config_customized = True

        enabled_remote = set(self.state.user_config.enable_services.split(","))

        if available_remote_services:
            remote_box.pack_start(remote_title, False, False, 0)
            for service, label in (("ssh", "SSH"), ("xrdp", "XRDP")):
                if service not in available_remote_services:
                    continue
                check = Gtk.CheckButton(label=label)
                check.set_active(service in enabled_remote)
                check.connect("toggled", lambda btn, name=service: update_remote_service(name, btn.get_active()))
                remote_box.pack_start(check, False, False, 0)
            remote_frame.add(remote_box)
            cards_box.pack_start(remote_frame, False, False, 8)

        self.content_body.pack_start(cards_box, False, False, 0)
        self._nav(True)

    def _step_location(self):
        self._page_title(
            _("Location"),
            _("Choose the system language and time zone for the installed system."),
        )
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(4)
        grid.set_margin_end(4)

        # Locale: entry + completion (ComboBox entry does not filter/autocomplete well).
        # Model columns: 0=code, 1=label (with real language names when iso-codes present).
        self._locale_store = Gtk.ListStore(str, str)
        self._locale_by_code = {}
        for code in self.available_locales:
            label = format_locale_label(code)
            self._locale_store.append([code, label])
            self._locale_by_code[code] = label

        self.locale_entry = Gtk.Entry()
        self.locale_entry.set_hexpand(True)
        self.locale_entry.set_placeholder_text(_("Type a language or locale, e.g. Russian or ru_RU"))
        current_locale = self.state.user_config.locale or ""
        if current_locale:
            self.locale_entry.set_text(self._locale_by_code.get(current_locale, current_locale))

        completion = Gtk.EntryCompletion()
        completion.set_model(self._locale_store)
        completion.set_text_column(1)
        completion.set_inline_completion(False)
        completion.set_popup_completion(True)
        completion.set_popup_single_match(True)
        completion.set_minimum_key_length(1)

        def locale_match(_completion, key, tree_iter):
            code = self._locale_store[tree_iter][0] or ""
            label = self._locale_store[tree_iter][1] or ""
            key = (key or "").lower()
            return key in code.lower() or key in label.lower()

        completion.set_match_func(locale_match)

        def on_locale_match_selected(_completion, model, tree_iter):
            code = model[tree_iter][0]
            label = model[tree_iter][1]
            self.locale_entry.set_text(label)
            self._set_user_config("locale", code)
            return True

        completion.connect("match-selected", on_locale_match_selected)
        self.locale_entry.set_completion(completion)

        def on_locale_entry_changed(entry):
            code = locale_code_from_label(entry.get_text())
            # Prefer exact known code; allow free-typed codes too.
            if code in self._locale_by_code:
                self._set_user_config("locale", code)
            elif code:
                self._set_user_config("locale", code)

        self.locale_entry.connect("changed", on_locale_entry_changed)

        self.timezone_combo = self._create_combo_with_entry(
            self.available_timezones,
            self.state.user_config.timezone,
            "UTC",
            lambda value: self._set_user_config("timezone", value),
        )

        grid.attach(Gtk.Label(label=_("System language:"), xalign=0), 0, 0, 1, 1)
        grid.attach(self.locale_entry, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Timezone:"), xalign=0), 0, 1, 1, 1)
        grid.attach(self.timezone_combo, 1, 1, 1, 1)

        # Detect: short fixed-width button; status on its own line (no reflow / side text).
        detect_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        detect_col.set_margin_top(6)
        self.detect_location_btn = Gtk.Button(label=_("Detect"))
        self.detect_location_btn.set_tooltip_text(
            _(
                "Fill language, timezone, and keyboard. "
                "Uses network geolocation when online; otherwise this live session."
            )
        )
        # Keep a stable size so the control does not jump when state changes.
        self.detect_location_btn.set_size_request(120, 34)
        self.detect_location_btn.set_halign(Gtk.Align.START)
        self.detect_location_btn.connect("clicked", self._on_detect_location)
        detect_col.pack_start(self.detect_location_btn, False, False, 0)

        self.detect_status = Gtk.Label(xalign=0)
        self.detect_status.set_line_wrap(True)
        self.detect_status.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.detect_status.set_max_width_chars(56)
        self.detect_status.get_style_context().add_class("dim-label")
        detect_col.pack_start(self.detect_status, False, False, 0)

        hint = Gtk.Label(
            label=_(
                "Start typing a language name or code. "
                "Detect sets language, timezone, and keyboard (network first, then this live session)."
            ),
            xalign=0,
        )
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        detect_col.pack_start(hint, False, False, 0)

        grid.attach(detect_col, 1, 2, 1, 1)
        # Forms use natural height; outer content_scroll scrolls if needed.
        self.content_body.pack_start(grid, False, False, 0)
        self._nav(True)

    def _set_locale_entry_code(self, code):
        code = (code or "").strip()
        if not code:
            return
        self._set_user_config("locale", code)
        if hasattr(self, "locale_entry"):
            label = getattr(self, "_locale_by_code", {}).get(code, format_locale_label(code))
            self.locale_entry.set_text(label)

    def _set_timezone_entry_value(self, zone):
        zone = (zone or "").strip()
        if not zone:
            return
        self._set_user_config("timezone", zone)
        if hasattr(self, "timezone_combo"):
            entry = self.timezone_combo.get_child()
            if entry is not None:
                entry.set_text(zone)
            if zone in self.available_timezones:
                self.timezone_combo.set_active(self.available_timezones.index(zone))

    def _on_detect_location(self, button):
        """Detect language, timezone, keyboard: GeoIP when online, live session offline."""
        button.set_sensitive(False)
        if hasattr(self, "detect_status"):
            self.detect_status.set_text(_("Detecting…"))

        kb_codes = [code for code, _desc in self.available_keyboard_layouts]

        def work():
            try:
                result = detect_location_best_effort(
                    available_locales=self.available_locales,
                    available_timezones=self.available_timezones,
                    available_keyboard_codes=kb_codes,
                )
            except Exception as exc:
                result = {
                    "locale": "",
                    "timezone": "",
                    "keyboard": "",
                    "keyboard_options": "",
                    "source": "none",
                    "error": str(exc),
                }
            GLib.idle_add(self._apply_detect_location_result, result, button)

        threading.Thread(target=work, daemon=True).start()

    def _apply_detect_location_result(self, result, button):
        button.set_sensitive(True)
        locale = (result or {}).get("locale") or ""
        zone = (result or {}).get("timezone") or ""
        keyboard = (result or {}).get("keyboard") or ""
        keyboard_options = (result or {}).get("keyboard_options") or ""
        source = (result or {}).get("source") or "none"
        country = (result or {}).get("country_code") or ""

        if locale:
            self._set_locale_entry_code(locale)
        if zone:
            self._set_timezone_entry_value(zone)
        if keyboard:
            self._set_user_config("keyboard", keyboard)
            self._set_user_config(
                "keyboard_options",
                normalize_keyboard_switch_option(keyboard_options, layouts_value=keyboard),
            )
            # Refresh keyboard step widgets if the user is already there.
            if hasattr(self, "keyboard_entry"):
                self.keyboard_entry.set_text(keyboard)
                self._sync_keyboard_switch_sensitivity()

        bits = []
        if locale:
            bits.append(locale)
        if zone:
            bits.append(zone)
        if keyboard:
            bits.append(keyboard)

        if source == "geoip":
            msg = _("Detected via network")
            if country:
                msg = _("Detected via network (country {code})").format(code=country)
        elif source == "session":
            msg = _("Detected from this live session (offline or no GeoIP result)")
        elif source == "mixed":
            msg = _("Detected using network and this live session")
        else:
            msg = _("Could not detect language, timezone, or keyboard")
            if not bits:
                self._show_error(
                    _(
                        "Could not detect language, timezone, or keyboard. "
                        "Check the network, or set the values manually."
                    )
                )

        if bits:
            msg = "{summary}: {details}".format(summary=msg, details=" · ".join(bits))

        if hasattr(self, "detect_status"):
            self.detect_status.set_text(msg)
        return False

    def _wired_network_interfaces(self):
        interfaces = []
        try:
            names = sorted(os.listdir("/sys/class/net"))
        except OSError:
            return interfaces
        for name in names:
            if name == "lo" or os.path.isdir(os.path.join("/sys/class/net", name, "wireless")):
                continue
            try:
                with open(os.path.join("/sys/class/net", name, "operstate"), "r", encoding="utf-8") as fh:
                    state = fh.read().strip()
            except OSError:
                state = "unknown"
            interfaces.append((name, state))
        return interfaces

    def _step_network(self):
        self._page_title(
            _("Network"),
            _("Set the computer name and optionally configure a wired static IPv4 connection."),
        )
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(4)
        grid.set_margin_end(4)
        row = 0

        system_title = Gtk.Label(label=_("System"), xalign=0)
        system_title.get_style_context().add_class("section-title")
        grid.attach(system_title, 0, row, 2, 1)
        row += 1
        self.hostname_entry = Gtk.Entry(text=self.state.user_config.hostname)
        self.hostname_entry.set_hexpand(True)
        self.hostname_entry.set_placeholder_text(_("For example: minios-laptop"))
        self.hostname_entry.connect("changed", lambda e: self._on_network_field_changed("hostname", e.get_text()))
        grid.attach(Gtk.Label(label=_("Computer name:"), xalign=0), 0, row, 1, 1)
        grid.attach(self.hostname_entry, 1, row, 1, 1)
        row += 1

        network_title = Gtk.Label(label=_("Wired network"), xalign=0)
        network_title.get_style_context().add_class("section-title")
        grid.attach(network_title, 0, row, 2, 1)
        row += 1
        interfaces = self._wired_network_interfaces()
        self.network_interface_combo = Gtk.ComboBoxText()
        for name, state in interfaces:
            self.network_interface_combo.append(name, "{} ({})".format(name, state))
        selected_interface = self.state.user_config.network_interface
        if interfaces:
            if not selected_interface or not self.network_interface_combo.set_active_id(selected_interface):
                self.network_interface_combo.set_active(0)
                self.state.user_config.network_interface = interfaces[0][0]
        self.network_interface_combo.set_hexpand(True)
        self.network_interface_combo.connect("changed", self._on_network_interface_changed)
        grid.attach(Gtk.Label(label=_("Interface:"), xalign=0), 0, row, 1, 1)
        grid.attach(self.network_interface_combo, 1, row, 1, 1)
        row += 1

        detected = Gtk.Label(xalign=0)
        detected.set_line_wrap(True)
        detected.get_style_context().add_class("dim-label")
        if interfaces:
            active = [name for name, state in interfaces if state == "up"]
            detected.set_text(
                _("Detected wired connection: {interface}.").format(interface=active[0] if active else interfaces[0][0])
            )
        else:
            detected.set_text(_("No wired network interface was detected. Wi-Fi profiles are kept unchanged."))
        grid.attach(detected, 1, row, 1, 1)
        row += 1

        self.network_dhcp_radio = Gtk.RadioButton.new_with_label_from_widget(None, _("Automatic (DHCP)"))
        self.network_static_radio = Gtk.RadioButton.new_with_label_from_widget(
            self.network_dhcp_radio, _("Static IPv4")
        )
        self.network_dhcp_radio.set_active(self.state.user_config.network_method != "static")
        self.network_static_radio.set_active(self.state.user_config.network_method == "static")
        self.network_backend_available = network_backend_available()
        self.network_static_radio.set_sensitive(self.network_backend_available)
        self.network_dhcp_radio.connect("toggled", self._on_network_method_toggled, "dhcp")
        self.network_static_radio.connect("toggled", self._on_network_method_toggled, "static")
        grid.attach(self.network_dhcp_radio, 1, row, 1, 1)
        row += 1
        grid.attach(self.network_static_radio, 1, row, 1, 1)
        row += 1

        self.network_static_widgets = []
        for label, attr, value, placeholder in (
            (_("IPv4 address:"), "network_address", self.state.user_config.network_address, "192.168.1.20"),
            (_("Prefix length:"), "network_prefix", self.state.user_config.network_prefix or "24", "24"),
            (_("Gateway:"), "network_gateway", self.state.user_config.network_gateway, "192.168.1.1"),
            (_("DNS servers:"), "network_dns", self.state.user_config.network_dns, "1.1.1.1, 8.8.8.8"),
        ):
            entry = Gtk.Entry(text=value)
            entry.set_hexpand(True)
            entry.set_placeholder_text(placeholder)
            entry.connect("changed", lambda e, field=attr: self._on_network_field_changed(field, e.get_text()))
            label_widget = Gtk.Label(label=label, xalign=0)
            grid.attach(label_widget, 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            self.network_static_widgets.extend([label_widget, entry])
            row += 1

        self.network_validation_label = Gtk.Label(xalign=0)
        self.network_validation_label.set_line_wrap(True)
        self.network_validation_label.get_style_context().add_class("dim-label")
        grid.attach(self.network_validation_label, 0, row, 2, 1)
        self.content_body.pack_start(grid, False, False, 0)
        self._update_network_ui()
        self._nav(self._network_form_valid())

    def _on_network_interface_changed(self, combo):
        interface = combo.get_active_id() or ""
        if interface:
            self._set_user_config("network_interface", interface)
        self._update_network_ui()

    def _on_network_method_toggled(self, button, method):
        if button.get_active():
            self._set_user_config("network_method", method)
            self._update_network_ui()

    def _on_network_field_changed(self, field, value):
        self._set_user_config(field, value)
        self._update_network_ui()

    def _network_validation_message(self):
        user = self.state.user_config
        hostname = (user.hostname or "").strip()
        if hostname and not HOSTNAME_RE.match(hostname):
            return _("Computer name is not valid.")
        if user.network_method == "static":
            if not getattr(self, "network_backend_available", False):
                return _("Static IPv4 requires NetworkManager or ifupdown in the installed system.")
            if not user.network_interface:
                return _("Choose a wired network interface for the static connection.")
            error = validate_static_ipv4(
                user.network_address,
                user.network_prefix,
                user.network_gateway,
                user.network_dns,
            )
            if error:
                return NETWORK_VALIDATION_MESSAGES.get(error, error)
        return ""

    def _network_form_valid(self):
        return not self._network_validation_message()

    def _update_network_ui(self):
        if not hasattr(self, "network_validation_label"):
            return
        static = self.state.user_config.network_method == "static"
        for widget in self.network_static_widgets:
            widget.set_sensitive(static)
        message = self._network_validation_message()
        self.network_validation_label.set_text(message)
        if hasattr(self, "next_button"):
            self.next_button.set_sensitive(not message)

    def _step_keyboard(self):
        self._page_title(
            _("Keyboard"),
            _("Choose the keyboard layout for the installed system."),
        )
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(4)
        grid.set_margin_end(4)

        # Codes for completion; free-form entry supports multi-layout "us,ru".
        layout_codes = [code for code, _desc in self.available_keyboard_layouts]
        self.keyboard_entry = Gtk.Entry(text=self.state.user_config.keyboard or "")
        self.keyboard_entry.set_hexpand(True)
        self.keyboard_entry.set_placeholder_text(_("e.g. us  or  us,ru"))
        self._attach_completion(self.keyboard_entry, layout_codes)

        def on_kb_changed(entry):
            value = entry.get_text().strip()
            # Normalize "English (US) (us)" style leftovers if user picks oddly.
            if value.endswith(")") and "(" in value and "," not in value:
                value = value.rsplit("(", 1)[-1].rstrip(")").strip()
            self._set_user_config("keyboard", value)
            self._sync_keyboard_switch_sensitivity()

        self.keyboard_entry.connect("changed", on_kb_changed)

        self.keyboard_switch_label = Gtk.Label(label=_("Switch layout:"), xalign=0)
        self.keyboard_options_combo = Gtk.ComboBoxText()
        switching_options = [
            ("none", _("Not needed")),
            ("grp:alt_shift_toggle", _("Alt + Shift")),
            ("grp:ctrl_shift_toggle", _("Ctrl + Shift")),
            ("grp:win_space_toggle", _("Win + Space")),
            ("grp:caps_toggle", _("Caps Lock")),
        ]
        for value, label in switching_options:
            self.keyboard_options_combo.append(value, label)
        self.keyboard_options_combo.connect("changed", self._on_keyboard_switch_changed)

        self.keyboard_switch_hint = Gtk.Label(xalign=0)
        self.keyboard_switch_hint.set_line_wrap(True)
        self.keyboard_switch_hint.get_style_context().add_class("dim-label")

        grid.attach(Gtk.Label(label=_("Keyboard layout:"), xalign=0), 0, 0, 1, 1)
        grid.attach(self.keyboard_entry, 1, 0, 1, 1)
        grid.attach(self.keyboard_switch_label, 0, 1, 1, 1)
        grid.attach(self.keyboard_options_combo, 1, 1, 1, 1)
        grid.attach(self.keyboard_switch_hint, 1, 2, 1, 1)
        hint = Gtk.Label(
            label=_(
                "These settings will be applied to the installed system. "
                "Use a comma to combine layouts (for example us,ru)."
            ),
            xalign=0,
        )
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        grid.attach(hint, 1, 3, 1, 1)
        self.content_body.pack_start(grid, False, False, 0)
        self._sync_keyboard_switch_sensitivity()
        self._nav(True)

    def _on_keyboard_switch_changed(self, combo):
        active_id = combo.get_active_id()
        if active_id in (None, "none"):
            value = ""
        else:
            value = active_id
        # Only store a switch option when multiple layouts are selected.
        if not keyboard_needs_layout_switch(self.state.user_config.keyboard):
            value = ""
        self._set_user_config("keyboard_options", value)

    def _sync_keyboard_switch_sensitivity(self):
        """Enable layout-switch control only when 2+ layouts are configured."""
        if not hasattr(self, "keyboard_options_combo"):
            return
        needs_switch = keyboard_needs_layout_switch(self.state.user_config.keyboard)
        was_multi = getattr(self, "_keyboard_was_multi", None)
        self.keyboard_options_combo.set_sensitive(needs_switch)
        self.keyboard_switch_label.set_sensitive(needs_switch)
        if needs_switch:
            current = normalize_keyboard_switch_option(
                self.state.user_config.keyboard_options,
                layouts_value=self.state.user_config.keyboard,
            )
            # First time user adds a second layout, pick a useful default shortcut.
            if not current and was_multi is False:
                current = "grp:alt_shift_toggle"
                self.state.user_config.keyboard_options = current
            active_id = current if current else "none"
            if not self.keyboard_options_combo.set_active_id(active_id):
                self.keyboard_options_combo.set_active(0)
            self.keyboard_switch_hint.set_text(
                _("Choose a shortcut to switch between the selected layouts.")
            )
        else:
            # Single layout: clear switch option so installed system does not get grp:*.
            if self.state.user_config.keyboard_options:
                self._set_user_config("keyboard_options", "")
            else:
                self.state.user_config.keyboard_options = ""
            self.keyboard_options_combo.set_active_id("none")
            self.keyboard_switch_hint.set_text(
                _("Only one layout is selected — a switch shortcut is not needed.")
            )
        self._keyboard_was_multi = needs_switch

    def _step_users(self):
        is_native = self.state.install_mode == "native"
        if is_native:
            subtitle = _("Create the main user account. Username and password are required.")
        else:
            subtitle = _("Create the main user account for the installed system.")
        self._page_title(_("Users"), subtitle)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(4)
        grid.set_margin_end(4)

        self.users_validation_label = Gtk.Label(xalign=0)
        self.users_validation_label.get_style_context().add_class("dim-label")
        self.users_validation_label.set_line_wrap(True)

        # Native install: do not prefill full name from the live image.
        full_name_text = self.state.user_config.full_name or ""
        if is_native:
            live_fullname = getattr(self, "_live_fullname_default", "") or ""
            if full_name_text == live_fullname or full_name_text in (
                "MiniOS User",
                "MiniOS Live User",
            ):
                full_name_text = ""
                self.state.user_config.full_name = ""
        full_name = Gtk.Entry(text=full_name_text)
        full_name.set_hexpand(True)
        if is_native:
            full_name.set_placeholder_text(_("Full name (optional)"))
        full_name.connect("changed", lambda e: self._set_user_config("full_name", e.get_text()))

        username_text = self.state.user_config.username
        if is_native and username_text == self._live_username_default:
            username_text = ""
            self.state.user_config.username = ""
        self.username_entry = Gtk.Entry(text=username_text)
        self.username_entry.set_hexpand(True)
        if is_native:
            self.username_entry.set_placeholder_text(_("Choose a username"))
        self.username_entry.connect("changed", lambda e: self._on_users_field_changed("username", e.get_text()))

        # Align password boxes (with eye) with plain entries in column 1.
        field_width_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        field_width_group.add_widget(full_name)
        field_width_group.add_widget(self.username_entry)

        row = 0
        account_title = Gtk.Label(label=_("User account"), xalign=0)
        account_title.get_style_context().add_class("section-title")
        grid.attach(account_title, 0, row, 2, 1)
        row += 1
        grid.attach(Gtk.Label(label=_("Full name:"), xalign=0), 0, row, 1, 1)
        grid.attach(full_name, 1, row, 1, 1)
        row += 1
        grid.attach(Gtk.Label(label=_("Username:"), xalign=0), 0, row, 1, 1)
        grid.attach(self.username_entry, 1, row, 1, 1)
        row += 1

        pw_placeholder = (
            _("Password (required)")
            if is_native
            else (
                _("Password is set")
                if self.state.user_config.password
                else _("Leave blank to keep current password")
            )
        )
        password_box, self.password_entry = self._create_password_entry(
            pw_placeholder,
            lambda value: self._on_users_field_changed("password", value),
            initial=self.state.user_config.password if not is_native else self.state.user_config.password,
        )
        field_width_group.add_widget(password_box)
        grid.attach(Gtk.Label(label=_("Password:"), xalign=0), 0, row, 1, 1)
        grid.attach(password_box, 1, row, 1, 1)
        row += 1

        confirm_box, self.password_confirm_entry = self._create_password_entry(
            _("Confirm password"),
            lambda value: self._on_password_confirm_changed(value),
            initial=self._users_password_confirm,
        )
        field_width_group.add_widget(confirm_box)
        grid.attach(Gtk.Label(label=_("Confirm password:"), xalign=0), 0, row, 1, 1)
        grid.attach(confirm_box, 1, row, 1, 1)
        row += 1

        root_title = Gtk.Label(label=_("Root account"), xalign=0)
        root_title.get_style_context().add_class("section-title")
        grid.attach(root_title, 0, row, 2, 1)
        row += 1
        self._root_field_widgets = []
        if is_native:
            allow_root = Gtk.CheckButton(label=_("Allow root login / set root password"))
            allow_root.set_active(self.native_allow_root)
            allow_root.connect("toggled", self._on_native_allow_root_toggled)
            grid.attach(allow_root, 0, row, 2, 1)
            row += 1
            self.root_note = Gtk.Label(
                label=_("Root account will be locked; use sudo with your user."),
                xalign=0,
            )
            self.root_note.set_line_wrap(True)
            self.root_note.get_style_context().add_class("dim-label")
            grid.attach(self.root_note, 0, row, 2, 1)
            row += 1

            # Same grid columns as other fields so root password rows match width.
            self.root_password_label = Gtk.Label(label=_("Password:"), xalign=0)
            root_box, self.root_password_entry = self._create_password_entry(
                _("Root password"),
                lambda value: self._on_users_field_changed("root_password", value),
                initial=self.state.user_config.root_password,
            )
            field_width_group.add_widget(root_box)
            grid.attach(self.root_password_label, 0, row, 1, 1)
            grid.attach(root_box, 1, row, 1, 1)
            self._root_field_widgets.extend([self.root_password_label, root_box])
            row += 1

            self.root_confirm_label = Gtk.Label(label=_("Confirm password:"), xalign=0)
            root_confirm_box, self.root_confirm_entry = self._create_password_entry(
                _("Confirm root password"),
                lambda value: self._on_root_confirm_changed(value),
                initial=self._users_root_confirm,
            )
            field_width_group.add_widget(root_confirm_box)
            grid.attach(self.root_confirm_label, 0, row, 1, 1)
            grid.attach(root_confirm_box, 1, row, 1, 1)
            self._root_field_widgets.extend([self.root_confirm_label, root_confirm_box])
            row += 1

            for widget in self._root_field_widgets:
                widget.set_no_show_all(True)
            self._set_native_root_fields_visible(self.native_allow_root)
        else:
            root_box, self.root_password_entry = self._create_password_entry(
                _("Root password is set")
                if self.state.user_config.root_password
                else _("Leave blank to keep current root password"),
                lambda value: self._on_users_field_changed("root_password", value),
                initial=self.state.user_config.root_password,
            )
            field_width_group.add_widget(root_box)
            grid.attach(Gtk.Label(label=_("Password:"), xalign=0), 0, row, 1, 1)
            grid.attach(root_box, 1, row, 1, 1)
            row += 1
            root_confirm_box, self.root_confirm_entry = self._create_password_entry(
                _("Confirm root password"),
                lambda value: self._on_root_confirm_changed(value),
                initial=self._users_root_confirm,
            )
            field_width_group.add_widget(root_confirm_box)
            grid.attach(Gtk.Label(label=_("Confirm password:"), xalign=0), 0, row, 1, 1)
            grid.attach(root_confirm_box, 1, row, 1, 1)
            row += 1
            root_hint = Gtk.Label(
                label=_("Live systems often use root password “toor” by default."),
                xalign=0,
            )
            root_hint.set_line_wrap(True)
            root_hint.get_style_context().add_class("dim-label")
            grid.attach(root_hint, 1, row, 1, 1)
            row += 1

        grid.attach(self.users_validation_label, 0, row, 2, 1)
        self.content_body.pack_start(grid, False, False, 0)
        self._nav(self._users_form_valid())
        self._update_users_validation_ui()

    def _set_native_root_fields_visible(self, visible):
        """Show/hide native root password fields and the locked-root note."""
        if hasattr(self, "root_note"):
            if visible:
                self.root_note.hide()
            else:
                self.root_note.show()
        for widget in getattr(self, "_root_field_widgets", []):
            if visible:
                widget.set_no_show_all(False)
                widget.show_all()
            else:
                widget.hide()
                widget.set_no_show_all(True)
        GLib.idle_add(self._clamp_window_size)

    def _on_native_allow_root_toggled(self, button):
        self.native_allow_root = button.get_active()
        if not self.native_allow_root:
            self.state.user_config.root_password = ""
            self._users_root_confirm = ""
        self._set_native_root_fields_visible(self.native_allow_root)
        self._update_users_validation_ui()

    def _on_users_field_changed(self, field, value):
        self._set_user_config(field, value)
        self._update_users_validation_ui()

    def _on_password_confirm_changed(self, value):
        self._users_password_confirm = value
        self._update_users_validation_ui()

    def _on_root_confirm_changed(self, value):
        self._users_root_confirm = value
        self._update_users_validation_ui()

    def _users_validation_state(self):
        """
        Return (ok, message, error_fields).

        error_fields is a set of widget attribute names that should get .error
        (never match validation text with substring heuristics — that falsely
        marked username when the message mentioned "root login").
        """
        is_native = self.state.install_mode == "native"
        username = (self.state.user_config.username or "").strip()
        password = self.state.user_config.password or ""
        errors = set()

        if is_native:
            if not username:
                return False, _("Enter a username."), {"username_entry"}
            if username == "root" or not USERNAME_RE.match(username):
                return (
                    False,
                    _("Username must be a lowercase Linux login name (not root)."),
                    {"username_entry"},
                )
            if not password:
                return False, _("Enter a password for the user account."), {"password_entry"}
            if password != self._users_password_confirm:
                return False, _("Passwords do not match."), {"password_entry", "password_confirm_entry"}
            if self.native_allow_root:
                if not self.state.user_config.root_password:
                    return (
                        False,
                        _("Enter a root password or disable root login."),
                        {"root_password_entry"},
                    )
                if self.state.user_config.root_password != self._users_root_confirm:
                    return (
                        False,
                        _("Root passwords do not match."),
                        {"root_password_entry", "root_confirm_entry"},
                    )
            return True, "", set()

        # Live: soft validation — block only clear mismatches / invalid names when filled.
        if username and (username == "root" or not USERNAME_RE.match(username)):
            return (
                False,
                _("Username must be a lowercase Linux login name (not root)."),
                {"username_entry"},
            )
        if (password or self._users_password_confirm) and password != self._users_password_confirm:
            return False, _("Passwords do not match."), {"password_entry", "password_confirm_entry"}
        root_password = self.state.user_config.root_password or ""
        if (root_password or self._users_root_confirm) and root_password != self._users_root_confirm:
            return (
                False,
                _("Root passwords do not match."),
                {"root_password_entry", "root_confirm_entry"},
            )
        return True, "", errors

    def _users_form_valid(self):
        ok, _msg, _fields = self._users_validation_state()
        return ok

    def _users_validation_message(self):
        _ok, msg, _fields = self._users_validation_state()
        return msg

    def _update_users_validation_ui(self):
        if not hasattr(self, "users_validation_label"):
            return
        ok, msg, error_fields = self._users_validation_state()
        self.users_validation_label.set_text(msg)
        if hasattr(self, "next_button"):
            self.next_button.set_sensitive(ok)
        for entry_name in (
            "username_entry",
            "password_entry",
            "password_confirm_entry",
            "root_password_entry",
            "root_confirm_entry",
        ):
            entry = getattr(self, entry_name, None)
            if entry is None:
                continue
            ctx = entry.get_style_context()
            if entry_name in error_fields:
                ctx.add_class("error")
            else:
                ctx.remove_class("error")

    def _step_modules(self):
        self._page_title(
            _("Modules"),
            _("Choose modules to install. Selecting one also includes all lower layers."),
        )
        if not self.available_modules:
            note = Gtk.Label(label=_("No MiniOS modules were found in the live media."), xalign=0)
            note.set_line_wrap(True)
            self.content_body.pack_start(note, True, True, 0)
            self._nav(True)
            return

        if not self.state.selected_modules:
            self.state.selected_modules = list(self.available_modules)
        else:
            self.state.selected_modules = normalize_selected_modules(
                self.available_modules, self.state.selected_modules
            )

        mandatory_count = required_prefix_count(self.available_modules)
        available_info = Gtk.Label(
            label=_("{required} required layers and {optional} optional layers are available.").format(
                required=mandatory_count,
                optional=max(0, len(self.available_modules) - mandatory_count),
            ),
            xalign=0,
        )
        available_info.get_style_context().add_class("dim-label")
        self.content_body.pack_start(available_info, False, False, 6)

        # No nested ScrolledWindow: outer content_scroll already scrolls the step.
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.set_hexpand(True)

        self.module_buttons = []
        self.module_sizes = self.module_sizes_by_mode.get(self.state.install_mode, {})
        self._module_toggle_updating = False
        selected = set(self.state.selected_modules)
        for index, name in enumerate(self.available_modules):
            role, friendly, filename = describe_module_name(name)
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            box.set_margin_start(8)
            box.set_margin_end(8)
            button = Gtk.CheckButton()
            button.set_active(name in selected or index < mandatory_count)
            if index < mandatory_count:
                button.set_sensitive(False)
            button.connect("toggled", self._on_module_toggled, index)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(xalign=0)
            if index < mandatory_count:
                tag = _("Required")
            elif role == _("Custom module"):
                tag = _("Custom")
            else:
                tag = _("Optional")
            title.set_markup(
                "<b>{}</b>  <span size='small'>[{}]</span>".format(
                    GLib.markup_escape_text(role), GLib.markup_escape_text(tag)
                )
            )
            title.set_hexpand(True)
            size = Gtk.Label(label="+ " + format_module_size(self.module_sizes.get(name)), xalign=1)
            size.get_style_context().add_class("dim-label")
            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            title_row.pack_start(title, True, True, 0)
            title_row.pack_end(size, False, False, 0)
            sub = Gtk.Label(label=filename, xalign=0)
            sub.get_style_context().add_class("dim-label")
            texts.pack_start(title_row, False, False, 0)
            texts.pack_start(sub, False, False, 0)
            box.pack_start(button, False, False, 0)
            box.pack_start(texts, True, True, 0)
            row.add(box)
            listbox.add(row)
            self.module_buttons.append(button)

        self._sync_selected_modules_from_buttons()
        self.modules_count_label = Gtk.Label(xalign=0)
        self.modules_count_label.set_line_wrap(True)
        self.modules_count_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.modules_count_label.set_max_width_chars(72)
        self._update_modules_count_label()
        self.modules_count_label.get_style_context().add_class("dim-label")
        self.content_body.pack_start(listbox, False, False, 0)
        self.content_body.pack_start(self.modules_count_label, False, False, 8)
        self._nav(self._update_required_root_size() is not None)

    def _update_modules_count_label(self):
        if not hasattr(self, "modules_count_label"):
            return
        n = len(self.state.selected_modules)
        total = len(self.available_modules)
        selected_sizes = [self.module_sizes.get(name) for name in self.state.selected_modules]
        total_size = None if any(size is None for size in selected_sizes) else sum(selected_sizes)
        if total_size is None:
            self.modules_count_label.set_text(_("Calculating installation space…"))
            return
        if self.state.install_mode == "native":
            message = _(
                "{selected} of {total} layers selected. Estimated native system data: {size}. Required root space with reserve: {required}."
            )
        else:
            message = _(
                "{selected} of {total} layers selected. Estimated live module images on target: {size}. Required root space with reserve: {required}."
            )
        self.modules_count_label.set_text(
            message.format(
                selected=n,
                total=total,
                size=format_module_size(total_size),
                required=format_module_size(required_root_mib(total_size) * 1024 * 1024),
            )
        )

    def _on_module_toggled(self, button, index):
        if getattr(self, "_module_toggle_updating", False):
            return
        self._module_toggle_updating = True
        try:
            if button.get_active():
                for pos in range(0, index + 1):
                    self.module_buttons[pos].set_active(True)
            else:
                for pos in range(index, len(self.module_buttons)):
                    self.module_buttons[pos].set_active(False)
                mandatory_count = required_prefix_count(self.available_modules)
                for pos in range(0, mandatory_count):
                    self.module_buttons[pos].set_active(True)
        finally:
            self._module_toggle_updating = False
        self._sync_selected_modules_from_buttons()
        self._update_modules_count_label()

    def _sync_selected_modules_from_buttons(self):
        self.state.selected_modules = [
            name for name, button in zip(self.available_modules, self.module_buttons) if button.get_active()
        ]
        self._update_required_root_size()

    def _step_partitioning(self):
        self._page_title(
            _("Target Disk"),
            _("Select the disk MiniOS will be installed to and choose how the target filesystem should be created."),
        )
        self.disk_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.disk_list.connect("row-selected", self._on_disk_selected)
        self.disk_list.set_hexpand(True)
        # Prefer natural list height; outer content_scroll handles overflow.
        self.content_body.pack_start(self.disk_list, False, False, 0)

        # Bar + color legend in one card. DrawingArea paints stable proportions
        # (widget boxes + size-request fought the layout on re-select).
        preview_frame = Gtk.Frame(label=_("Partition layout"))
        preview_frame.get_style_context().add_class("content-card")
        preview_frame.set_margin_top(8)
        preview_frame.set_hexpand(True)
        self.partition_preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.partition_preview_box.set_margin_top(10)
        self.partition_preview_box.set_margin_bottom(10)
        self.partition_preview_box.set_margin_start(12)
        self.partition_preview_box.set_margin_end(12)
        self._partition_segments = []
        self.partition_bar = Gtk.DrawingArea()
        self.partition_bar.set_size_request(-1, 24)
        self.partition_bar.set_hexpand(True)
        self.partition_bar.get_style_context().add_class("partition-bar")
        self.partition_bar.connect("draw", self._on_partition_bar_draw)
        self.partition_bar.set_can_focus(True)
        self.partition_bar.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON1_MOTION_MASK |
            Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.partition_bar.connect("button-press-event", self._on_partition_bar_button_press)
        self.partition_bar.connect("button-release-event", self._on_partition_bar_button_release)
        self.partition_bar.connect("motion-notify-event", self._on_partition_bar_motion)
        self.partition_bar.connect("query-tooltip", self._on_partition_bar_tooltip)
        self.partition_bar.set_has_tooltip(True)
        # Legend wraps instead of forcing the window wider when Swap appears.
        self.partition_legend = Gtk.FlowBox()
        self.partition_legend.set_selection_mode(Gtk.SelectionMode.NONE)
        self.partition_legend.set_min_children_per_line(1)
        self.partition_legend.set_max_children_per_line(6)
        self.partition_legend.set_column_spacing(14)
        self.partition_legend.set_row_spacing(4)
        self.partition_legend.set_homogeneous(False)
        self.partition_legend.set_halign(Gtk.Align.START)
        self.partition_legend.set_hexpand(True)
        self.partition_preview_box.pack_start(self.partition_bar, False, False, 0)
        self.partition_geometry_hint = Gtk.Label(xalign=0)
        self.partition_geometry_hint.set_line_wrap(True)
        self.partition_geometry_hint.get_style_context().add_class("dim-label")
        self.partition_geometry_hint.hide()
        self.partition_preview_box.pack_start(self.partition_geometry_hint, False, False, 0)
        self.partition_preview_box.pack_start(self.partition_legend, False, False, 0)
        preview_frame.add(self.partition_preview_box)
        self.content_body.pack_start(preview_frame, False, False, 0)

        settings = Gtk.Frame(label=_("Disk Setup"))
        settings.get_style_context().add_class("content-card")
        settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings_box.set_margin_top(12)
        settings_box.set_margin_bottom(12)
        settings_box.set_margin_start(14)
        settings_box.set_margin_end(14)

        settings_box.pack_start(Gtk.Label(label=_("Installation type:"), xalign=0), False, False, 0)
        erase_radio = Gtk.RadioButton.new_with_label_from_widget(None, _("Erase disk and install MiniOS"))
        free_radio = Gtk.RadioButton.new_with_label_from_widget(erase_radio, _("Install in existing free space"))
        alongside_radio = Gtk.RadioButton.new_with_label_from_widget(erase_radio, _("Install alongside another system"))
        manual_radio = Gtk.RadioButton.new_with_label_from_widget(erase_radio, _("Manual partitioning"))
        erase_radio.set_active(self.state.placement not in (PLACEMENT_FREE_SPACE, PLACEMENT_ALONGSIDE_OS, "manual"))
        free_radio.set_active(self.state.placement == PLACEMENT_FREE_SPACE)
        alongside_radio.set_active(self.state.placement == PLACEMENT_ALONGSIDE_OS)
        manual_radio.set_active(self.state.placement == "manual")

        erase_desc = Gtk.Label(
            label=_("Deletes all partitions and data on the selected disk. This cannot be undone."),
            xalign=0,
        )
        erase_desc.set_line_wrap(True)
        erase_desc.get_style_context().add_class("dim-label")
        free_desc = Gtk.Label(
            label=_("Keeps existing partitions and uses free space only. Safer if the disk has other systems."),
            xalign=0,
        )
        free_desc.set_line_wrap(True)
        free_desc.get_style_context().add_class("dim-label")
        self.free_placement_description = free_desc
        self.alongside_placement_description = Gtk.Label(
            label=_("Shrinks the last supported partition and installs MiniOS in the space created."),
            xalign=0,
        )
        self.alongside_placement_description.set_line_wrap(True)
        self.alongside_placement_description.get_style_context().add_class("dim-label")

        def on_placement(btn, placement):
            if btn.get_active():
                controller = getattr(self, "manual_controller", None)
                if self.state.placement == "manual" and placement != "manual" and controller and controller.destructive:
                    if not self._confirm_manual_discard(_("Discard staged manual partition changes?")):
                        self.manual_placement_radio.set_active(True)
                        return
                    self.manual_controller = None
                    self.state.manual_partition_plan = None
                self.state.placement = placement
                self._refresh_manual_placement()
                self._update_partition_preview()

        erase_radio.connect("toggled", lambda btn: on_placement(btn, PLACEMENT_ERASE_ALL))
        free_radio.connect("toggled", lambda btn: on_placement(btn, PLACEMENT_FREE_SPACE))
        alongside_radio.connect("toggled", lambda btn: on_placement(btn, PLACEMENT_ALONGSIDE_OS))
        manual_radio.connect("toggled", lambda btn: on_placement(btn, "manual"))
        self.erase_placement_radio = erase_radio
        self.free_placement_radio = free_radio
        self.manual_placement_radio = manual_radio
        settings_box.pack_start(erase_radio, False, False, 0)
        settings_box.pack_start(erase_desc, False, False, 0)
        settings_box.pack_start(free_radio, False, False, 0)
        settings_box.pack_start(free_desc, False, False, 0)
        settings_box.pack_start(alongside_radio, False, False, 0)
        settings_box.pack_start(self.alongside_placement_description, False, False, 0)
        settings_box.pack_start(manual_radio, False, False, 0)
        self.manual_placement_description = Gtk.Label(xalign=0)
        self.manual_placement_description.set_line_wrap(True)
        self.manual_placement_description.get_style_context().add_class("dim-label")
        settings_box.pack_start(self.manual_placement_description, False, False, 0)

        self.manual_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.manual_box.set_margin_start(24)
        settings_box.pack_start(self.manual_box, False, False, 0)

        alongside_size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        alongside_size_box.set_margin_start(24)
        alongside_size_box.pack_start(Gtk.Label(label=_("Space for MiniOS:"), xalign=0), False, False, 0)
        required_root = max(1, self.state.required_root_mib)
        self.alongside_size_spin = Gtk.SpinButton.new_with_range(required_root, 1048576, 1024)
        self.alongside_size_spin.set_value(max(required_root, self.state.alongside_size_mib))
        self.alongside_size_spin.set_numeric(True)
        self.alongside_size_spin.set_width_chars(8)
        self.alongside_size_spin.connect("value-changed", self._on_alongside_size_changed)
        alongside_size_box.pack_start(self.alongside_size_spin, False, False, 0)
        alongside_size_box.pack_start(Gtk.Label(label=_("MiB"), xalign=0), False, False, 0)
        settings_box.pack_start(alongside_size_box, False, False, 0)

        self.install_resize_tools_button = Gtk.Button(label=_("Install required resize tools"))
        self.install_resize_tools_button.connect("clicked", self._on_install_resize_tools)
        self.install_resize_tools_button.set_no_show_all(True)
        settings_box.pack_start(self.install_resize_tools_button, False, False, 0)
        self.alongside_placement_radio = alongside_radio

        advanced = Gtk.Expander(label=_("Advanced…"))
        advanced.set_margin_top(6)
        advanced.connect("notify::expanded", lambda *_: GLib.idle_add(self._clamp_window_size))
        adv_grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        adv_grid.set_margin_top(10)
        adv_grid.set_margin_bottom(4)
        adv_grid.set_margin_start(4)
        adv_grid.set_margin_end(4)

        adv_grid.attach(Gtk.Label(label=_("Filesystem:"), xalign=0), 0, 0, 1, 1)
        fs_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.fs_combo = Gtk.ComboBoxText()
        self._refresh_filesystem_choices()
        def on_fs_changed(combo):
            self.state.filesystem = combo.get_active_id() or "ext4"
            self._refresh_persistence_choices()
            self._update_persistence_size_limit()
            self._update_partition_preview()

        self.fs_combo.connect("changed", on_fs_changed)
        fs_box.pack_start(self.fs_combo, True, True, 0)
        info = Gtk.Image.new_from_icon_name("dialog-information", Gtk.IconSize.SMALL_TOOLBAR)
        info_box = Gtk.EventBox()
        info_box.add(info)
        info_box.set_tooltip_markup(FILESYSTEM_HELP_MARKUP)
        fs_box.pack_start(info_box, False, False, 0)
        adv_grid.attach(fs_box, 1, 0, 1, 1)

        adv_grid.attach(Gtk.Label(label=_("Boot layout:"), xalign=0), 0, 1, 1, 1)
        boot_layout_combo = Gtk.ComboBoxText()
        for value, label in (
            ("auto", _("Automatic (recommended)")),
            ("bios_mbr", _("BIOS / MBR")),
            ("uefi_mbr", _("UEFI / MBR")),
            ("uefi_gpt", _("UEFI / GPT")),
        ):
            boot_layout_combo.append(value, label)
        if not boot_layout_combo.set_active_id(self.state.boot_layout):
            boot_layout_combo.set_active_id("auto")
            self.state.boot_layout = "auto"
        boot_layout_combo.set_sensitive(self.state.install_mode == "native")
        def on_boot_layout_changed(combo):
            self.state.boot_layout = combo.get_active_id() or "auto"
            self._update_partition_preview()

        boot_layout_combo.connect("changed", on_boot_layout_changed)
        adv_grid.attach(boot_layout_combo, 1, 1, 1, 1)

        adv_grid.attach(Gtk.Label(label=_("Swap:"), xalign=0), 0, 2, 1, 1)
        swap_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # Fixed width: SpinButton ± buttons change preferred width on hover and
        # otherwise keep growing the toplevel window horizontally.
        self.swap_spin = Gtk.SpinButton.new_with_range(0, 65536, 512)
        self.swap_spin.set_increments(512, 2048)
        self.swap_spin.set_numeric(True)
        self.swap_spin.set_snap_to_ticks(True)
        self.swap_spin.set_digits(0)
        self.swap_spin.set_width_chars(6)
        self.swap_spin.set_max_width_chars(6)
        self.swap_spin.set_size_request(120, -1)
        self.swap_spin.set_hexpand(False)
        self.swap_spin.set_halign(Gtk.Align.START)
        self.swap_spin.get_style_context().add_class("installer-spin")
        self.swap_spin.set_value(self.state.swap_size_mib if self.state.install_mode == "native" else 0)
        self.swap_spin.set_sensitive(self.state.install_mode == "native")
        def on_swap_changed(spin):
            if self.state.install_mode == "native":
                self.state.swap_size_mib = int(spin.get_value())
            if self._erase_swap_drag_enabled():
                self._update_erase_swap_preview_geometry()
                if not getattr(self, "_erase_swap_dragging", False):
                    self._schedule_erase_swap_plan_recalculation()
                return
            self._refresh_free_space_placement()
            self._update_partition_preview()
            # Preview legend text length must not stretch the window.
            GLib.idle_add(self._clamp_window_size)

        self.swap_spin.connect("value-changed", on_swap_changed)
        # Eat enter-notify size churn from the spin buttons if the theme still
        # reports a wider requisition while hovered.
        self.swap_spin.connect("enter-notify-event", self._on_swap_spin_hover_clamp)
        self.swap_spin.connect("leave-notify-event", self._on_swap_spin_hover_clamp)
        swap_box.pack_start(self.swap_spin, False, False, 0)
        swap_unit = Gtk.Label(label=_("MiB (native install only)"), xalign=0)
        swap_unit.set_hexpand(False)
        swap_box.pack_start(swap_unit, False, False, 0)
        adv_grid.attach(swap_box, 1, 2, 1, 1)

        adv_grid.attach(Gtk.Label(label=_("Session storage:"), xalign=0), 0, 3, 1, 1)
        persistence_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.persistence_combo = Gtk.ComboBoxText()
        self.persistence_combo.set_sensitive(self.state.install_mode == "live")
        self.persistence_size_spin = Gtk.SpinButton.new_with_range(1, 1000000, 256)
        self.persistence_size_spin.set_numeric(True)
        self.persistence_size_spin.set_width_chars(7)
        self.persistence_size_spin.set_value(max(1, self.state.persistence_size_mib or 4000))

        def on_persistence_changed(combo):
            if getattr(self, "_updating_persistence_choices", False):
                return
            self.state.persistence_mode = combo.get_active_id() or "none"
            if self.state.persistence_mode in ("dynfilefs", "raw", "luks"):
                self.state.persistence_size_mib = int(self.persistence_size_spin.get_value())
            else:
                self.state.persistence_size_mib = 0
            self._update_persistence_controls()
            self._update_required_root_size()
            self._refresh_free_space_placement()
            self._refresh_alongside_placement()
            self._update_partition_preview()

        def on_persistence_size_changed(spin):
            if self.state.persistence_mode in ("dynfilefs", "raw", "luks"):
                self.state.persistence_size_mib = int(spin.get_value())
                self._update_required_root_size()
                self._refresh_free_space_placement()
                self._refresh_alongside_placement()
                self._update_partition_preview()

        self.persistence_combo.connect("changed", on_persistence_changed)
        self.persistence_size_spin.connect("value-changed", on_persistence_size_changed)
        persistence_box.pack_start(self.persistence_combo, True, True, 0)
        persistence_box.pack_start(self.persistence_size_spin, False, False, 0)
        persistence_box.pack_start(Gtk.Label(label=_("MiB"), xalign=0), False, False, 0)
        adv_grid.attach(persistence_box, 1, 3, 1, 1)
        self.persistence_note = Gtk.Label(xalign=0)
        self.persistence_note.set_line_wrap(True)
        self.persistence_note.get_style_context().add_class("dim-label")
        adv_grid.attach(self.persistence_note, 0, 4, 2, 1)
        self._refresh_persistence_choices()
        self._update_persistence_size_limit()

        advanced.add(adv_grid)
        settings_box.pack_start(advanced, False, False, 0)
        settings.add(settings_box)
        settings.set_margin_top(10)
        self.content_body.pack_start(settings, False, False, 0)
        self._refresh_disks()
        self._refresh_alongside_placement()
        self._refresh_free_space_placement()
        self._refresh_manual_placement()
        self._update_partition_preview()
        self._nav(bool(self.state.target_device))

    def _refresh_filesystem_choices(self):
        filesystems = filesystems_for_boot_mode(install_mode=self.state.install_mode) or ["ext4"]
        current = self.state.filesystem if self.state.filesystem in filesystems else filesystems[0]
        self.fs_combo.remove_all()
        for fs in filesystems:
            self.fs_combo.append(fs, fs)
        self.fs_combo.set_active_id(current)
        self.state.filesystem = current

    def _update_persistence_size_limit(self):
        if not hasattr(self, "persistence_size_spin"):
            return
        fixed_file = self.state.persistence_mode in ("raw", "luks")
        maximum = 4000 if self.state.filesystem == "fat32" and fixed_file else 1000000
        self.persistence_size_spin.set_range(1, maximum)
        if self.persistence_size_spin.get_value() > maximum:
            self.persistence_size_spin.set_value(maximum)
        if self.state.persistence_mode in ("dynfilefs", "raw", "luks"):
            self.state.persistence_size_mib = int(self.persistence_size_spin.get_value())

    def _refresh_persistence_choices(self):
        if not hasattr(self, "persistence_combo"):
            return
        choices = [("none", _("Discard changes on restart"))]
        if self.state.install_mode == "live":
            if self.state.filesystem not in ("fat32", "ntfs"):
                choices.append(("native", _("Native persistent changes")))
            choices.extend((
                ("dynfilefs", _("Expandable persistent changes (DynFileFS)")),
                ("raw", _("Fixed-size persistent changes (Raw image)")),
            ))
            if runtime_supports_luks_persistence():
                choices.append(("luks", _("Encrypted persistent changes (LUKS)")))
        valid = {value for value, _label in choices}
        selected = self.state.persistence_mode
        if selected not in valid:
            selected = "dynfilefs" if selected == "native" and self.state.install_mode == "live" else "none"
        self._updating_persistence_choices = True
        try:
            self.persistence_combo.remove_all()
            for value, label in choices:
                self.persistence_combo.append(value, label)
            self.persistence_combo.set_active_id(selected)
            self.state.persistence_mode = selected
        finally:
            self._updating_persistence_choices = False
        self._update_persistence_controls()

    def _update_persistence_controls(self):
        if not hasattr(self, "persistence_size_spin"):
            return
        mode = self.state.persistence_mode
        uses_size = mode in ("dynfilefs", "raw", "luks")
        self._update_persistence_size_limit()
        self.persistence_size_spin.set_sensitive(self.state.install_mode == "live" and uses_size)
        if uses_size:
            self.state.persistence_size_mib = int(self.persistence_size_spin.get_value())
        else:
            self.state.persistence_size_mib = 0
        if not hasattr(self, "persistence_note"):
            return
        notes = {
            "none": _("Changes are kept in memory and discarded on restart."),
            "native": _("The initrd stores changes directly on a POSIX-compatible target filesystem."),
            "dynfilefs": _("The initrd creates expandable DynFileFS storage with the selected maximum size."),
            "raw": _("The initrd creates a fixed-size ext4 image for persistent changes."),
            "luks": _("The initrd creates changes.luks and asks for its password on first boot; the installer never stores it."),
        }
        self.persistence_note.set_text(notes.get(mode, ""))

    def _clear_partition_legend(self):
        if not hasattr(self, "partition_legend"):
            return
        for child in self.partition_legend.get_children():
            self.partition_legend.remove(child)

    def _partition_seg_rgb(self, kind):
        """RGB 0..1 for DrawingArea fills (match CSS segment colors)."""
        return {
            "esp": (0.208, 0.518, 0.894),      # #3584e4
            "root": (0.149, 0.635, 0.412),     # #26a269
            "swap": (0.898, 0.647, 0.039),     # #e5a50a
            "minios": (0.149, 0.635, 0.412),   # #26a269
            "free": (0.55, 0.55, 0.55),
            "other": (0.40, 0.40, 0.40),
        }.get(kind, (0.45, 0.45, 0.45))

    def _partition_seg_class(self, kind):
        return {
            "esp": "partition-seg-esp",
            "root": "partition-seg-root",
            "swap": "partition-seg-swap",
            "minios": "partition-seg-root",
            "free": "partition-seg-free",
            "other": "partition-seg-other",
        }.get(kind, "partition-seg-other")

    def _partition_seg_label(self, kind):
        return {
            "esp": _("ESP"),
            "root": _("System"),
            "swap": _("Swap"),
            "minios": _("MiniOS"),
            "free": _("Free space"),
            "other": _("Existing"),
        }.get(kind, kind)

    def _set_partition_legend_message(self, message):
        """Single dim line (no disk selected / error)."""
        self._clear_partition_legend()
        self._partition_segments = []
        if hasattr(self, "partition_geometry_hint"):
            self.partition_geometry_hint.hide()
        if hasattr(self, "partition_bar"):
            self.partition_bar.queue_draw()
        label = Gtk.Label(label=message, xalign=0)
        label.set_line_wrap(True)
        label.get_style_context().add_class("dim-label")
        self.partition_legend.add(label)
        self.partition_legend.show_all()

    def _set_partition_legend_from_segments(self, segments):
        """
        Build legend tied to the bar: [color] Name  [color] Name …
        Order follows first appearance on the strip (left → right).
        """
        self._clear_partition_legend()
        seen = set()
        for kind, size in segments:
            if kind in seen:
                continue
            seen.add(kind)
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            item.get_style_context().add_class("partition-legend-item")
            swatch = Gtk.EventBox()
            swatch.set_visible_window(True)
            swatch.set_size_request(14, 14)
            swatch.set_valign(Gtk.Align.CENTER)
            swatch.get_style_context().add_class(self._partition_seg_class(kind))
            swatch.get_style_context().add_class("partition-legend-swatch")
            if size >= 1024:
                size_txt = "{:.1f} GiB".format(size / 1024.0)
            else:
                size_txt = "{} MiB".format(int(size))
            text = Gtk.Label(
                label="{name} ({size})".format(
                    name=self._partition_seg_label(kind),
                    size=size_txt,
                ),
                xalign=0,
            )
            text.set_valign(Gtk.Align.CENTER)
            item.pack_start(swatch, False, False, 0)
            item.pack_start(text, False, False, 0)
            self.partition_legend.add(item)
        self.partition_legend.show_all()

    def _on_swap_spin_hover_clamp(self, *_args):
        """SpinButton hover must not grow the installer window."""
        GLib.idle_add(self._clamp_window_size)
        return False

    def _filter_preview_segments(self, segments, disk_size_mib):
        """
        Drop tiny free gaps (alignment / end guard) that look like real free space.

        Those 1–16 MiB slivers at the edges confused users (gray bars with no clear meaning).
        Keep free segments only when they are a meaningful share of the disk.
        """
        if not segments:
            return segments
        min_free = max(32, int(disk_size_mib * 0.01))  # ≥32 MiB or 1% of disk
        filtered = []
        for kind, size in segments:
            if kind == "free" and size < min_free:
                continue
            filtered.append((kind, max(1, size)))
        return filtered or segments

    def _on_partition_bar_draw(self, area, cr):
        """Paint proportional segments; independent of child widgets / re-select."""
        width = area.get_allocated_width()
        height = area.get_allocated_height()
        if width <= 1 or height <= 1:
            return False
        # Track background
        cr.set_source_rgb(0.82, 0.82, 0.82)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        segments = getattr(self, "_partition_segments", None) or []
        if not segments:
            return False
        total = sum(max(1, int(size)) for _k, size in segments) or 1
        x = 0.0
        for index, (kind, size) in enumerate(segments):
            size = max(1, int(size))
            if index == len(segments) - 1:
                w = max(1.0, width - x)
            else:
                w = max(1.0, width * (size / float(total)))
            r, g, b = self._partition_seg_rgb(kind)
            cr.set_source_rgb(r, g, b)
            cr.rectangle(x, 0, w, height)
            cr.fill()
            # 1px separator between segments
            if index < len(segments) - 1 and w > 2:
                cr.set_source_rgb(0.75, 0.75, 0.75)
                cr.rectangle(x + w - 1, 0, 1, height)
                cr.fill()
            x += w
        # Outer border
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, width - 1, height - 1)
        cr.stroke()
        geometry = getattr(self, "_alongside_geometry", None)
        if geometry and self.state.placement == PLACEMENT_ALONGSIDE_OS:
            boundary = self._alongside_boundary_x(width)
            old_boundary = resize_boundary_x(
                0, geometry["candidate"].start_mib, geometry["candidate"].size_mib,
                geometry["layout"].size_mib, width,
            )
            # The dashed line is the old partition end; the solid handle is the
            # pending resize boundary, so the before/after geometry stays visible.
            cr.set_source_rgba(0.15, 0.15, 0.15, 0.65)
            cr.set_line_width(1)
            cr.set_dash([3.0, 2.0])
            cr.move_to(old_boundary + 0.5, 0)
            cr.line_to(old_boundary + 0.5, height)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgb(0.12, 0.12, 0.12)
            cr.rectangle(boundary - 2, 0, 4, height)
            cr.fill()
        elif self._erase_swap_drag_enabled():
            boundary = self._erase_swap_boundary_x(width)
            cr.set_source_rgb(0.12, 0.12, 0.12)
            cr.rectangle(boundary - 2, 0, 4, height)
            cr.fill()
        return False

    def _on_partition_bar_tooltip(self, area, x, y, _keyboard_mode, tooltip):
        if self._alongside_drag_enabled() and abs(x - self._alongside_boundary_x(area.get_allocated_width())) <= 10:
            tooltip.set_text(_("Drag the boundary to choose MiniOS space."))
            return True
        if self._erase_swap_drag_enabled() and abs(x - self._erase_swap_boundary_x(area.get_allocated_width())) <= 10:
            tooltip.set_text(_("Drag the boundary to choose swap space."))
            return True
        segments = getattr(self, "_partition_segments", None) or []
        if not segments:
            return False
        width = area.get_allocated_width()
        if width <= 1:
            return False
        total = sum(max(1, int(size)) for _k, size in segments) or 1
        pos = 0.0
        for kind, size in segments:
            size = max(1, int(size))
            w = width * (size / float(total))
            if pos <= x < pos + w or (kind == segments[-1][0] and x >= pos):
                if size >= 1024:
                    size_txt = "{:.1f} GiB".format(size / 1024.0)
                else:
                    size_txt = "{} MiB".format(size)
                tooltip.set_text(
                    "{label}: {size}".format(label=self._partition_seg_label(kind), size=size_txt)
                )
                return True
            pos += w
        return False

    def _alongside_drag_enabled(self):
        return bool(
            getattr(self, "_alongside_geometry", None) and
            self.state.placement == PLACEMENT_ALONGSIDE_OS and
            getattr(self, "alongside_placement_radio", None).get_sensitive()
        )

    def _erase_swap_drag_enabled(self):
        return bool(
            getattr(self, "_erase_swap_geometry", None) and
            self.state.placement == PLACEMENT_ERASE_ALL and
            self.state.install_mode == "native"
        )

    def _erase_swap_boundary_x(self, width):
        geometry = self._erase_swap_geometry
        return trailing_swap_boundary_x(
            self.state.swap_size_mib, geometry["layout"].size_mib, width,
            geometry["end_guard"],
        )

    def _alongside_boundary_x(self, width):
        geometry = self._alongside_geometry
        return resize_boundary_x(
            self.state.alongside_size_mib, geometry["candidate"].start_mib,
            geometry["candidate"].size_mib, geometry["layout"].size_mib, width,
        )

    def _set_alongside_cursor(self, active):
        window = self.partition_bar.get_window()
        if not window:
            return
        cursor = Gdk.Cursor.new_from_name(window.get_display(), "col-resize") if active else None
        window.set_cursor(cursor)

    def _on_partition_bar_button_press(self, area, event):
        if event.button != 1:
            return False
        if self._alongside_drag_enabled():
            if abs(event.x - self._alongside_boundary_x(area.get_allocated_width())) > 12:
                return False
            self._alongside_dragging = True
        elif self._erase_swap_drag_enabled():
            if abs(event.x - self._erase_swap_boundary_x(area.get_allocated_width())) > 12:
                return False
            self._erase_swap_dragging = True
        else:
            return False
        area.grab_focus()
        self._set_alongside_cursor(True)
        return True

    def _on_partition_bar_button_release(self, _area, event):
        if event.button != 1:
            return False
        if getattr(self, "_erase_swap_dragging", False):
            self._erase_swap_dragging = False
            self._set_alongside_cursor(False)
            source = getattr(self, "_erase_swap_plan_recalculation", 0)
            if source:
                GLib.source_remove(source)
                self._erase_swap_plan_recalculation = 0
            self._update_partition_preview()
            return True
        if not getattr(self, "_alongside_dragging", False):
            return False
        self._alongside_dragging = False
        self._set_alongside_cursor(False)
        source = getattr(self, "_alongside_plan_recalculation", 0)
        if source:
            GLib.source_remove(source)
            self._alongside_plan_recalculation = 0
        self._refresh_alongside_placement()
        if not self._update_alongside_preview_geometry():
            self._update_partition_preview()
        return True

    def _on_partition_bar_motion(self, area, event):
        if self._erase_swap_drag_enabled():
            boundary = self._erase_swap_boundary_x(area.get_allocated_width())
            if getattr(self, "_erase_swap_dragging", False):
                geometry = self._erase_swap_geometry
                value = trailing_swap_size_at_x(
                    event.x, geometry["layout"].size_mib, area.get_allocated_width(),
                    geometry["minimum"], geometry["maximum"], geometry["end_guard"],
                )
                if value != self.state.swap_size_mib:
                    self.swap_spin.set_value(value)
                return True
            self._set_alongside_cursor(abs(event.x - boundary) <= 12)
            return False
        if not self._alongside_drag_enabled():
            self._set_alongside_cursor(False)
            return False
        boundary = self._alongside_boundary_x(area.get_allocated_width())
        if getattr(self, "_alongside_dragging", False):
            geometry = self._alongside_geometry
            value = resize_size_at_x(
                event.x, geometry["candidate"].start_mib, geometry["candidate"].size_mib,
                geometry["layout"].size_mib, area.get_allocated_width(),
                geometry["minimum"], geometry["maximum"],
            )
            if value != self.state.alongside_size_mib:
                self.alongside_size_spin.set_value(value)
            return True
        self._set_alongside_cursor(abs(event.x - boundary) <= 12)
        return False

    def _update_alongside_preview_geometry(self):
        """Paint cached resize geometry without probing the filesystem again."""
        geometry = getattr(self, "_alongside_geometry", None)
        if not geometry or self.state.placement != PLACEMENT_ALONGSIDE_OS:
            return False
        layout = geometry["layout"]
        candidate = geometry["candidate"]
        selected = max(0, min(candidate.size_mib, self.state.alongside_size_mib))
        segments = []
        cursor = 0
        for part in sorted(layout.partitions, key=lambda item: item.start_mib):
            if part.start_mib > cursor:
                segments.append(("free", part.start_mib - cursor))
            if part.path == candidate.path:
                segments.append(("other", max(1, part.size_mib - selected)))
                segments.append(("minios", max(1, selected)))
            else:
                segments.append(("other", max(1, part.size_mib)))
            cursor = max(cursor, part.end_mib)
        if cursor < layout.size_mib:
            segments.append(("free", layout.size_mib - cursor))
        self._partition_segments = segments or [("free", max(1, layout.size_mib))]
        if not getattr(self, "_alongside_dragging", False):
            self._set_partition_legend_from_segments(self._partition_segments)
        self.partition_geometry_hint.set_text(
            _("Drag the boundary or use Space for MiniOS: {size} MiB.").format(size=selected)
        )
        self.partition_geometry_hint.show()
        self.partition_bar.queue_draw()
        return True

    def _update_erase_swap_preview_geometry(self):
        """Paint an erase-all native layout without rebuilding its disk plan."""
        geometry = getattr(self, "_erase_swap_geometry", None)
        if not geometry or self.state.placement != PLACEMENT_ERASE_ALL:
            return False
        layout = geometry["layout"]
        swap = max(geometry["minimum"], min(geometry["maximum"], self.state.swap_size_mib))
        root = layout.size_mib - geometry["root_start"] - geometry["end_guard"] - swap
        segments = []
        if geometry["esp_size"]:
            segments.append(("esp", geometry["esp_size"]))
        segments.append(("root", max(1, root)))
        if swap:
            segments.append(("swap", swap))
        self._partition_segments = segments
        if not getattr(self, "_erase_swap_dragging", False):
            self._set_partition_legend_from_segments(segments)
        self.partition_geometry_hint.set_text(
            _("Drag the System/Swap boundary or use Swap: {size} MiB. The erase layout is editable.").format(
                size=swap
            )
        )
        self.partition_geometry_hint.show()
        self.partition_bar.queue_draw()
        return True

    def _update_partition_preview(self):
        if not hasattr(self, "partition_bar"):
            return
        if self.state.placement != PLACEMENT_ALONGSIDE_OS:
            self._alongside_geometry = None
            self.partition_geometry_hint.hide()
        if not (self.state.placement == PLACEMENT_ERASE_ALL and self.state.install_mode == "native"):
            self._erase_swap_geometry = None
            if hasattr(self, "swap_spin"):
                adjustment = self.swap_spin.get_adjustment()
                if adjustment.get_lower() != 0 or adjustment.get_upper() != 65536:
                    self.swap_spin.set_range(0, 65536)
        if not self.state.target_device:
            self._alongside_geometry = None
            self._partition_segments = []
            self._set_partition_legend_message(_("Select a disk to preview the partition layout."))
            self.partition_bar.queue_draw()
            GLib.idle_add(self._clamp_window_size)
            return
        try:
            layout = scan_disk(self.state.target_device)
            if self.state.placement == "manual":
                controller = getattr(self, "manual_controller", None)
                if not controller:
                    raise ManualPlanError(_("Manual partitioning has not been initialized."))
                assignments = {item.target: item for item in controller.assignments}
                segments = []
                items = tuple(controller.snapshot.partitions) + tuple(controller.snapshot.free_extents)
                for extent in sorted(items, key=lambda item: item.start_sector):
                    if isinstance(extent, SectorExtent):
                        kind = "free"
                    else:
                        role = assignments.get(extent).role if extent in assignments else "other"
                        kind = "esp" if role == "esp" else "swap" if role == "swap" else "root" if role == "root" else "other"
                    segments.append((kind, max(1, extent.size_sectors * controller.snapshot.sector_size // (1024 * 1024))))
                self._partition_segments = segments
                self._set_partition_legend_from_segments(segments)
                self.partition_geometry_hint.set_text(_("Manual view: existing layout with staged mount assignments."))
                self.partition_geometry_hint.show()
                self.partition_bar.queue_draw()
                return
            if self.state.placement == PLACEMENT_ERASE_ALL and self.state.install_mode == "native":
                # Build the zero-swap layout to retain its fixed ESP/alignment
                # boundary while the root/swap boundary is dragged in memory.
                base_plan = build_plan(
                    layout, PLACEMENT_ERASE_ALL, self.state.filesystem,
                    install_mode="native", swap_size_mib=0,
                    boot_layout=self.state.boot_layout,
                    required_root_mib=self.state.required_root_mib,
                )
                root_part = next(part for part in base_plan.partitions if part.role == "minios_root")
                esp_part = next((part for part in base_plan.partitions if part.role == "esp"), None)
                end_guard = max(0, layout.size_mib - max(part.end_mib for part in base_plan.partitions))
                minimum, maximum = erase_swap_limits(
                    layout.size_mib, root_part.start_mib, self.state.required_root_mib, end_guard
                )
                self._erase_swap_geometry = {
                    "layout": layout,
                    "minimum": minimum,
                    "maximum": maximum,
                    "root_start": root_part.start_mib,
                    "esp_size": esp_part.size_mib if esp_part else 0,
                    "end_guard": end_guard,
                }
                if hasattr(self, "swap_spin"):
                    self.swap_spin.set_range(minimum, maximum)
                    if self.state.swap_size_mib > maximum:
                        self.swap_spin.set_value(maximum)
            plan = build_plan(
                layout,
                self.state.placement,
                self.state.filesystem,
                install_mode=self.state.install_mode,
                swap_size_mib=self.state.swap_size_mib,
                boot_layout=self.state.boot_layout,
                alongside_size_mib=self.state.alongside_size_mib,
                required_root_mib=self.state.required_root_mib,
            )
        except Exception as exc:
            self._partition_segments = []
            self._set_partition_legend_message(str(exc))
            self.partition_bar.queue_draw()
            return

        if self.state.placement == PLACEMENT_ALONGSIDE_OS and plan.resize:
            candidate = select_resize_candidate(layout)
            self._alongside_geometry = {
                "layout": layout,
                "candidate": candidate,
                "minimum": int(self.alongside_size_spin.get_adjustment().get_lower()),
                "maximum": int(self.alongside_size_spin.get_adjustment().get_upper()),
            }
            self._update_alongside_preview_geometry()
            return

        if self._erase_swap_drag_enabled():
            self._update_erase_swap_preview_geometry()
            return

        total = max(1, layout.size_mib)
        segments = []
        if plan.wipe_disk:
            # Planned layout only — do not paint alignment padding as "free".
            for part in plan.partitions:
                role = part.role
                if role == "esp":
                    kind = "esp"
                elif role == "swap":
                    kind = "swap"
                else:
                    kind = "root"
                segments.append((kind, max(1, part.size_mib)))
            # Show trailing free only if a large unused region remains after the plan.
            used = sum(s for _k, s in segments)
            trailing = total - used
            if trailing >= max(32, int(total * 0.01)):
                segments.append(("free", trailing))
        else:
            # Existing partitions + meaningful free extents + new planned partitions.
            for part in layout.partitions:
                if plan.resize and part.path == plan.resize.path:
                    resized_mib = plan.resize.new_size_sectors * plan.resize.sector_size // (1024 * 1024)
                    segments.append(("other", max(1, resized_mib)))
                else:
                    segments.append(("other", max(1, part.size_mib)))
            for extent in layout.free_extents:
                segments.append(("free", max(1, extent.size_mib)))
            for part in plan.partitions:
                if part.action != "reuse":
                    if part.role == "esp":
                        kind = "esp"
                    elif part.role == "swap":
                        kind = "swap"
                    else:
                        kind = "root"
                    segments.append((kind, max(1, part.size_mib)))
            segments = self._filter_preview_segments(segments, total)

        if not segments:
            segments = [("free", total)]

        self._partition_segments = list(segments)
        self._set_partition_legend_from_segments(segments)
        self.partition_bar.queue_draw()
        GLib.idle_add(self._clamp_window_size)

    def _refresh_free_space_placement(self):
        if not hasattr(self, "free_placement_radio"):
            return
        available = False
        reason = ""
        if not self.state.target_device:
            reason = _("Select a disk first.")
        else:
            try:
                layout = scan_disk(self.state.target_device)
                if not layout.partition_table:
                    reason = _("This disk has no partition table. Erase the disk to initialize it.")
                else:
                    build_plan(
                        layout,
                        PLACEMENT_FREE_SPACE,
                        self.state.filesystem,
                        install_mode=self.state.install_mode,
                        swap_size_mib=self.state.swap_size_mib,
                        boot_layout=self.state.boot_layout,
                        required_root_mib=self.state.required_root_mib,
                    )
                    available = True
            except Exception as exc:
                reason = str(exc)

        self.free_placement_radio.set_sensitive(available)
        if available:
            self.free_placement_description.set_text(
                _("Keeps existing partitions and uses free space only. Safer if the disk has other systems.")
            )
        else:
            self.free_placement_description.set_text(
                _("Not available: {reason}").format(reason=reason)
            )
            if self.state.placement == PLACEMENT_FREE_SPACE:
                self.erase_placement_radio.set_active(True)

    def _confirm_manual_discard(self, text):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        dialog.format_secondary_text(_("These changes have not been written, but include destructive disk actions."))
        accepted = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return accepted

    def _refresh_manual_placement(self):
        if not hasattr(self, "manual_placement_radio"):
            return
        available = False
        reason = _("Manual partitioning is available only for full (native) installations.")
        snapshot = None
        if self.state.install_mode == "native" and self.state.target_device:
            try:
                identity = self.state.target_device_identity or {}
                stable_target = identity.get("by_id") or self.state.target_device
                snapshot = scan_manual_layout(scan_disk(stable_target))
                available = True
                reason = _("Stage exact changes below. No disk commands run until Install.")
            except Exception as exc:
                reason = str(exc)
        elif self.state.install_mode == "native":
            reason = _("Select an eligible GPT or primary-MBR disk first.")
        self.manual_placement_radio.set_sensitive(available)
        self.manual_placement_description.set_text(
            reason if available else _("Not available: {reason}").format(reason=reason))
        if not available and self.state.placement == "manual":
            self.erase_placement_radio.set_active(True)
            return
        if available and self.state.placement == "manual":
            current = getattr(self, "manual_controller", None)
            required = self.state.required_root_mib * 1024 * 1024 // snapshot.sector_size
            context = (snapshot, _use_efi_for_layout(self.state.boot_layout), required,
                       self.state.install_mode, self.state.boot_layout,
                       tuple(self.state.selected_modules))
            if current is None or getattr(self, "manual_context", None) != context:
                # Required size is intentionally exact sectors, not a display estimate.
                self.manual_controller = ManualPartitionController(
                    snapshot, use_efi=_use_efi_for_layout(self.state.boot_layout),
                    required_root_sectors=required, install_mode=self.state.install_mode)
                self.manual_context = context
            self._render_manual_partitioning()
        elif hasattr(self, "manual_box"):
            self.manual_box.hide()

    def _render_manual_partitioning(self):
        box = self.manual_box
        for child in box.get_children():
            box.remove(child)
        controller = self.manual_controller
        # The common disk map remains proportional; this table gives exact identities and staged intent.
        rows = Gtk.ListBox()
        self.manual_selected_target = None
        for ref in controller.snapshot.partitions:
            assignment = next((a for a in controller.assignments if a.target == ref), None)
            action = next((a.kind for a in controller.actions if a.target == ref), "keep")
            label = Gtk.Label(xalign=0)
            label.set_text("#{0}  {1}  {2} sectors  PARTUUID={3}  {4}  {5}".format(
                ref.number, ref.fstype, ref.size_sectors, ref.partuuid,
                assignment.mountpoint if assignment else "-", action))
            row = Gtk.ListBoxRow()
            row.target = ref
            row.add(label)
            rows.add(row)
        for extent in controller.snapshot.free_extents:
            row = Gtk.ListBoxRow()
            row.target = extent
            row.add(Gtk.Label(label=_("Free space: {size} sectors").format(size=extent.size_sectors), xalign=0))
            rows.add(row)
        for action in controller.actions:
            if action.kind == "create":
                row = Gtk.ListBoxRow()
                row.target = action.extent
                row.add(Gtk.Label(label=_("New partition: {size} sectors").format(size=action.extent.size_sectors), xalign=0))
                rows.add(row)
        rows.connect("row-selected", lambda _list, row: setattr(self, "manual_selected_target", getattr(row, "target", None) if row else None))
        box.pack_start(rows, False, False, 0)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for title, callback in ((_("Create"), self._manual_create), (_("Edit/Use as"), self._manual_edit),
                                (_("Resize"), self._manual_resize), (_("Delete"), self._manual_delete),
                                (_("Undo last"), self._manual_undo), (_("Reset"), self._manual_reset)):
            button = Gtk.Button(label=title)
            button.connect("clicked", callback)
            actions.pack_start(button, False, False, 0)
        box.pack_start(actions, False, False, 0)
        status = Gtk.Label(xalign=0)
        status.set_line_wrap(True)
        if controller.error:
            status.set_markup("<span foreground='red'>{}</span>".format(GLib.markup_escape_text(controller.error)))
        else:
            status.set_text(_("Valid staged plan. " + " ".join(controller.summary_lines())))
        box.pack_start(status, False, False, 0)
        self.state.manual_partition_plan = controller.plan
        self._nav(bool(self.state.target_device and controller.plan), None)
        box.show_all()

    def _manual_after_change(self):
        self._render_manual_partitioning()
        self._update_partition_preview()

    def _manual_confirm(self, text):
        return self._confirm_manual_discard(text)

    def _manual_create(self, _button):
        target = getattr(self, "manual_selected_target", None)
        if not hasattr(target, "size_sectors") or target in self.manual_controller.snapshot.partitions:
            self._show_error(_("Select a free-space row first.")); return
        if any(action.kind == "create" and action.extent == target for action in self.manual_controller.actions):
            self._show_error(_("Select unallocated free space, not a staged partition.")); return
        size = target.size_sectors - (target.size_sectors % self.manual_controller.alignment_sectors)
        start = target.start_sector + ((-target.start_sector) % self.manual_controller.alignment_sectors)
        size = min(size, target.end_sector - start)
        size -= size % self.manual_controller.alignment_sectors
        dialog = Gtk.Dialog(title=_("Create partition"), transient_for=self, modal=True)
        spin = Gtk.SpinButton.new_with_range(self.manual_controller.alignment_sectors, size, self.manual_controller.alignment_sectors)
        spin.set_value(size)
        dialog.get_content_area().pack_start(Gtk.Label(label=_("Exact aligned size (sectors):"), xalign=0), False, False, 8)
        dialog.get_content_area().pack_start(spin, False, False, 8)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL); dialog.add_button(_("Stage create"), Gtk.ResponseType.OK); dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK; size = int(spin.get_value()); dialog.destroy()
        if not accepted or size <= 0 or not self._manual_confirm(_("Stage creation of a new partition?")): return
        self.manual_controller.create(start, size)
        self._manual_after_change()

    def _manual_delete(self, _button):
        target = getattr(self, "manual_selected_target", None)
        if target not in self.manual_controller.snapshot.partitions and not isinstance(target, SectorExtent):
            self._show_error(_("Select an existing partition first.")); return
        if self._manual_confirm(_("Stage deletion of the selected partition?")):
            self.manual_controller.delete(target); self._manual_after_change()

    def _manual_resize(self, _button):
        target = getattr(self, "manual_selected_target", None)
        if target not in self.manual_controller.snapshot.partitions:
            self._show_error(_("Select an existing partition first.")); return
        dialog = Gtk.Dialog(title=_("Resize partition"), transient_for=self, modal=True)
        alignment = self.manual_controller.alignment_sectors
        maximum = (target.size_sectors - 1) // alignment * alignment
        spin = Gtk.SpinButton.new_with_range(alignment, maximum, alignment)
        spin.set_value(maximum)
        dialog.get_content_area().pack_start(spin, False, False, 10); dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL); dialog.add_button(_("Stage resize"), Gtk.ResponseType.OK); dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK; size = int(spin.get_value()); dialog.destroy()
        if accepted and self._manual_confirm(_("Stage resize of the selected partition?")):
            self.manual_controller.resize(target, size); self._manual_after_change()

    def _manual_edit(self, _button):
        target = getattr(self, "manual_selected_target", None)
        if target not in self.manual_controller.snapshot.partitions and not isinstance(target, SectorExtent):
            self._show_error(_("Select an existing partition first.")); return
        dialog = Gtk.Dialog(title=_("Use partition as"), transient_for=self, modal=True)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8); grid.set_margin_top(10); grid.set_margin_start(10)
        fs = Gtk.ComboBoxText(); [fs.append_text(value) for value in ("ext4", "ext3", "ext2", "btrfs", "vfat", "swap")]; fs.set_active(0)
        mount = Gtk.ComboBoxText.new_with_entry(); [mount.append_text(value) for value in ("/", "/home", "/var", "/boot/efi", "")]; mount.set_active(0)
        fmt = Gtk.CheckButton(label=_("Format")); fmt.set_active(False)
        grid.attach(Gtk.Label(label=_("Filesystem:"), xalign=0), 0, 0, 1, 1); grid.attach(fs, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=_("Mountpoint:"), xalign=0), 0, 1, 1, 1); grid.attach(mount, 1, 1, 1, 1); grid.attach(fmt, 1, 2, 1, 1)
        dialog.get_content_area().add(grid); dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL); dialog.add_button(_("Stage"), Gtk.ResponseType.OK); dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK; value = mount.get_child().get_text(); fstype = fs.get_active_text(); format_it = fmt.get_active(); dialog.destroy()
        role = "swap" if fstype == "swap" else "esp" if value == "/boot/efi" else "root" if value == "/" else "data"
        # A new partition has no filesystem to preserve, so it must be formatted.
        format_it = format_it or isinstance(target, SectorExtent)
        if accepted and (not format_it or self._manual_confirm(_("Stage formatting of the selected partition?"))):
            self.manual_controller.use_as(target, role, "" if role == "swap" else value, fstype, format_it); self._manual_after_change()

    def _manual_undo(self, _button):
        self.manual_controller.undo(); self._manual_after_change()

    def _manual_reset(self, _button):
        if not self.manual_controller.destructive or self._confirm_manual_discard(_("Reset all staged manual partition changes?")):
            self.manual_controller.reset(); self._manual_after_change()

    def _on_alongside_size_changed(self, spin):
        self.state.alongside_size_mib = int(spin.get_value())
        if self.state.placement == PLACEMENT_ALONGSIDE_OS:
            if self._update_alongside_preview_geometry():
                if not getattr(self, "_alongside_dragging", False):
                    self._schedule_alongside_plan_recalculation()
            else:
                self._update_partition_preview()

    def _schedule_alongside_plan_recalculation(self):
        source = getattr(self, "_alongside_plan_recalculation", 0)
        if source:
            GLib.source_remove(source)
        self._alongside_plan_recalculation = GLib.timeout_add(250, self._recalculate_alongside_plan)

    def _schedule_erase_swap_plan_recalculation(self):
        source = getattr(self, "_erase_swap_plan_recalculation", 0)
        if source:
            GLib.source_remove(source)
        self._erase_swap_plan_recalculation = GLib.timeout_add(250, self._recalculate_erase_swap_plan)

    def _recalculate_erase_swap_plan(self):
        self._erase_swap_plan_recalculation = 0
        if self._erase_swap_drag_enabled():
            self._update_partition_preview()
        return False

    def _recalculate_alongside_plan(self):
        self._alongside_plan_recalculation = 0
        if self.state.placement == PLACEMENT_ALONGSIDE_OS:
            self._refresh_alongside_placement()
            if not self._update_alongside_preview_geometry():
                self._update_partition_preview()
        return False

    def _refresh_alongside_placement(self):
        if not hasattr(self, "alongside_placement_radio"):
            return
        available = False
        self._alongside_geometry = None
        reason = _("Select a disk first.")
        missing = []
        if self.state.target_device:
            try:
                layout = scan_disk(self.state.target_device)
                candidate = select_resize_candidate(layout)
                missing = resize_missing_packages(candidate.fstype)
                minimum_size = max(1, self.state.required_root_mib)
                if self.state.install_mode == "native":
                    minimum_size += max(0, self.state.swap_size_mib)
                max_size = candidate.size_mib - 1024
                if max_size < minimum_size:
                    raise ValueError(
                        _("Not enough space for selected modules (need at least {need} MiB)").format(
                            need=minimum_size
                        )
                    )
                self.alongside_size_spin.set_range(minimum_size, max_size)
                if self.state.alongside_size_mib < minimum_size:
                    self.state.alongside_size_mib = minimum_size
                    self.alongside_size_spin.set_value(minimum_size)
                if self.state.alongside_size_mib > max_size:
                    self.state.alongside_size_mib = max_size
                    self.alongside_size_spin.set_value(max_size)
                if missing:
                    reason = _("Required resize tools are missing: {packages}").format(packages=", ".join(missing))
                else:
                    build_plan(
                        layout,
                        PLACEMENT_ALONGSIDE_OS,
                        self.state.filesystem,
                        install_mode=self.state.install_mode,
                        swap_size_mib=self.state.swap_size_mib,
                        boot_layout=self.state.boot_layout,
                        alongside_size_mib=self.state.alongside_size_mib,
                        required_root_mib=self.state.required_root_mib,
                    )
                    available = True
                    reason = _("Will shrink {path} ({filesystem}) without moving its start.").format(
                        path=candidate.path,
                        filesystem=candidate.fstype,
                    )
                    self._alongside_geometry = {
                        "layout": layout,
                        "candidate": candidate,
                        "minimum": minimum_size,
                        "maximum": max_size,
                    }
            except Exception as exc:
                reason = str(exc)
        self.alongside_placement_radio.set_sensitive(available)
        self.alongside_size_spin.set_sensitive(available and self.state.placement == PLACEMENT_ALONGSIDE_OS)
        if not available:
            self.partition_geometry_hint.hide()
        self.alongside_placement_description.set_text(
            reason if available else _("Not available: {reason}").format(reason=reason)
        )
        if missing:
            self._resize_missing_packages = missing
            self.install_resize_tools_button.set_no_show_all(False)
            self.install_resize_tools_button.show()
        else:
            self._resize_missing_packages = []
            self.install_resize_tools_button.hide()
            self.install_resize_tools_button.set_no_show_all(True)
        if not available and self.state.placement == PLACEMENT_ALONGSIDE_OS:
            self.erase_placement_radio.set_active(True)

    def _on_install_resize_tools(self, button):
        packages = list(getattr(self, "_resize_missing_packages", []))
        if not packages:
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=_("Install required resize tools?"),
        )
        dialog.format_secondary_text(
            _("The following packages will be downloaded and installed in the live session: {packages}").format(
                packages=", ".join(packages)
            )
        )
        accepted = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not accepted:
            return
        button.set_sensitive(False)
        try:
            subprocess.run(["apt-get", "update"], check=True)
            subprocess.run(["apt-get", "install", "-y", "--no-install-recommends"] + packages, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            self._show_error(_("Could not install resize tools: {error}").format(error=exc))
        finally:
            button.set_sensitive(True)
        self._refresh_alongside_placement()
        self._update_partition_preview()

    def _refresh_disks(self):
        if self._current_step_name() != "partitioning" or not hasattr(self, "disk_list") or self.disk_list.get_parent() is None:
            return False
        selected = self.state.target_device
        for child in self.disk_list.get_children():
            self.disk_list.remove(child)
        self.disk_rows = {}
        for dev in find_available_disks():
            path = "/dev/{}".format(dev["name"])
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.pack_start(
                Gtk.Image.new_from_gicon(
                    Gio.ThemedIcon(name=dev.get("icon", "drive-harddisk")), Gtk.IconSize.DND
                ),
                False,
                False,
                0,
            )
            mounted = get_mounted_partitions(path)
            mount_note = ""
            if mounted:
                points = ", ".join(mp for _dev, mp in mounted[:3])
                if len(mounted) > 3:
                    points += ", …"
                mount_note = "\n<span foreground='#c47f00'>{}</span>".format(
                    GLib.markup_escape_text(
                        _("Mounted partitions will be unmounted: {points}").format(points=points)
                    )
                )
            meta_bits = []
            model = (dev.get("model") or "").strip()
            if model:
                meta_bits.append(model)
            serial = (dev.get("serial") or "").strip()
            if serial:
                short = serial[:16] + ("…" if len(serial) > 16 else "")
                meta_bits.append("S/N: {}".format(short))
            transport = (dev.get("transport") or "").strip()
            if transport:
                meta_bits.append(transport)
            meta = " · ".join(meta_bits) if meta_bits else ""
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(xalign=0)
            title.set_markup(
                "<b>{}</b>  <span weight='bold'>{}</span>".format(
                    GLib.markup_escape_text(path),
                    GLib.markup_escape_text(dev.get("size", "")),
                )
            )
            texts.pack_start(title, False, False, 0)
            if meta:
                meta_label = Gtk.Label(label=meta, xalign=0)
                meta_label.get_style_context().add_class("dim-label")
                texts.pack_start(meta_label, False, False, 0)
            if mount_note:
                # mount_note is pre-escaped Pango markup
                mount_label = Gtk.Label(xalign=0)
                mount_label.set_markup(mount_note.lstrip("\n"))
                texts.pack_start(mount_label, False, False, 0)
            box.pack_start(texts, True, True, 0)
            if mounted:
                warn_icon = Gtk.Image.new_from_icon_name("dialog-warning", Gtk.IconSize.BUTTON)
                warn_icon.set_tooltip_text(
                    _("This disk has mounted partitions. Erase-all will unmount and wipe them.")
                )
                box.pack_start(warn_icon, False, False, 0)
            row.add(box)
            row.device = path
            row.disk_meta = dev
            self.disk_list.add(row)
            self.disk_rows[path] = row
        self.disk_list.show_all()
        if selected and selected in self.disk_rows:
            self.disk_list.select_row(self.disk_rows[selected])
        else:
            self.state.target_device = None
            self.state.target_device_identity = None
            if hasattr(self, "next_button"):
                self.next_button.set_sensitive(False)
            self._update_partition_preview()
        return False

    def _on_disk_selected(self, _listbox, row):
        selected = getattr(row, "device", None) if row else None
        previous = self.state.target_device
        controller = getattr(self, "manual_controller", None)
        if (selected and previous and selected != previous and controller and controller.destructive and
                not self._confirm_manual_discard(_("Discard staged manual partition changes and switch disks?"))):
            old_row = getattr(self, "disk_rows", {}).get(previous)
            if old_row:
                GLib.idle_add(lambda: (self.disk_list.select_row(old_row), False)[1])
            return
        if selected != previous:
            self.manual_controller = None
            self.manual_context = None
            self.state.manual_partition_plan = None
        self.state.target_device = selected
        if self.state.target_device:
            meta = getattr(row, "disk_meta", None) or {}
            try:
                identity = get_device_identity(self.state.target_device)
            except Exception:
                identity = {
                    "path": self.state.target_device,
                    "by_id": (meta.get("by_id") or ""),
                    "serial": (meta.get("serial") or ""),
                    "model": (meta.get("model") or ""),
                    "size": "",
                }
            if not identity.get("by_id") and meta.get("by_id"):
                identity["by_id"] = meta["by_id"]
            self.state.target_device_identity = identity
        else:
            self.state.target_device_identity = None
        if hasattr(self, "next_button"):
            self.next_button.set_sensitive(bool(self.state.target_device))
        self._refresh_free_space_placement()
        self._refresh_alongside_placement()
        self._refresh_manual_placement()
        self._update_partition_preview()
        GLib.idle_add(self._clamp_window_size)

    def _show_error(self, message):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Installation Error"),
        )
        dlg.format_secondary_text(message)
        dlg.run()
        dlg.destroy()

    def _build_plan_or_error(self):
        if not self.state.target_device:
            raise RuntimeError(_("No target disk selected."))
        self.state.target_device = resolve_install_device(
            self.state.target_device,
            expected_identity=self.state.target_device_identity,
        )
        layout = scan_disk(self.state.target_device)
        if self.state.placement == "manual":
            controller = getattr(self, "manual_controller", None)
            if not controller or not controller.plan:
                raise RuntimeError(controller.error if controller else _("No validated manual partitioning plan selected."))
            if scan_manual_layout(layout) != controller.snapshot:
                raise RuntimeError(_("Manual partition layout changed; return to Partitioning and review it again."))
            self.state.manual_partition_plan = controller.plan
            return controller.plan
        self.state.partition_plan = build_plan(
            layout,
            self.state.placement,
            self.state.filesystem,
            install_mode=self.state.install_mode,
            swap_size_mib=self.state.swap_size_mib,
            boot_layout=self.state.boot_layout,
            alongside_size_mib=self.state.alongside_size_mib,
            required_root_mib=self.state.required_root_mib,
        )
        return self.state.partition_plan

    def _summary_card(self, title, body_widget):
        frame = Gtk.Frame(label=title)
        frame.get_style_context().add_class("summary-card")
        frame.get_style_context().add_class("content-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.pack_start(body_widget, False, False, 0)
        frame.add(box)
        return frame

    def _security_summary_widget(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(xalign=0)
        title.set_markup(
            "<b>{}</b>".format(
                GLib.markup_escape_text(self._profile_label(self.state.security_profile))
            )
        )
        box.pack_start(title, False, False, 0)

        enabled_remote = set(self.state.user_config.enable_services.split(","))
        remote = []
        if "ssh" in enabled_remote:
            remote.append("SSH")
        if "xrdp" in enabled_remote:
            remote.append("XRDP")
        remote_label = Gtk.Label(xalign=0)
        remote_label.set_line_wrap(True)
        if remote:
            remote_label.set_text(_("Incoming remote access: {services}").format(services=", ".join(remote)))
        else:
            remote_label.set_text(_("Incoming remote access: disabled"))
        box.pack_start(remote_label, False, False, 0)

        detail = Gtk.Label(xalign=0)
        detail.set_line_wrap(True)
        if self.state.install_mode == "native":
            detail.set_text(
                _("Full install: this profile is applied directly to the target system after live-only cleanup and before user creation.")
            )
            box.pack_start(detail, False, False, 0)
            return box

        registry = load_capabilities("/")
        requirements = profile_required_capabilities(self.state.security_profile)
        klass = support_class(registry, requirements)
        missing = ["{}={}".format(cid, value) for cid, value in requirements if not supports(registry, cid, value)]
        if klass == "full":
            detail.set_text(_("Live install: this image advertises full support for the selected profile."))
        elif klass == "legacy":
            detail.set_text(
                _("Live install: this image has no capabilities registry, so enforcement cannot be proven. The installer will still write forward-compatible profile keys.")
            )
        else:
            detail.set_text(
                _("Live install: this image is missing some advertised profile capabilities. The installer will still write the profile keys, but enforcement may be partial.")
            )
        box.pack_start(detail, False, False, 0)

        if missing and klass != "legacy":
            missing_label = Gtk.Label(xalign=0)
            missing_label.set_line_wrap(True)
            missing_label.get_style_context().add_class("dim-label")
            missing_label.set_text(_("Missing: {items}").format(items=", ".join(missing[:6])))
            box.pack_start(missing_label, False, False, 0)
        return box

    def _step_summary(self):
        self._page_title(
            _("Review Changes"),
            _("Nothing has been written yet. Confirm the exact disk actions below before continuing."),
        )
        try:
            plan = self._build_plan_or_error()
            lines = plan.summary_lines()
            can_install = True
        except Exception as exc:
            plan = None
            lines = [str(exc)]
            can_install = False

        # Flat layout in outer content_scroll — nested ScrolledWindow was collapsing cards.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_end(8)
        box.set_hexpand(True)
        self.content_body.pack_start(box, False, False, 0)

        if not can_install:
            banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            banner.get_style_context().add_class("error-banner")
            banner.pack_start(
                Gtk.Image.new_from_icon_name("dialog-error", Gtk.IconSize.LARGE_TOOLBAR),
                False,
                False,
                0,
            )
            block_text = Gtk.Label(xalign=0)
            block_text.set_line_wrap(True)
            block_text.set_markup(
                "<b>{}</b>\n{}".format(
                    GLib.markup_escape_text(lines[0]),
                    GLib.markup_escape_text(
                        _("Go back to Partitioning and choose a different disk or installation method.")
                    ),
                )
            )
            banner.pack_start(block_text, True, True, 0)
            box.pack_start(banner, False, False, 0)

        if can_install and plan and plan.wipe_disk:
            banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            banner.get_style_context().add_class("warning-banner")
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            header.pack_start(
                Gtk.Image.new_from_icon_name("dialog-warning", Gtk.IconSize.LARGE_TOOLBAR),
                False,
                False,
                0,
            )
            warn = Gtk.Label(xalign=0)
            warn.set_markup(
                '<span size="large" weight="bold">{}</span>'.format(
                    GLib.markup_escape_text(_("WARNING: This action is irreversible!"))
                )
            )
            header.pack_start(warn, True, True, 0)
            banner.pack_start(header, False, False, 0)
            confirm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.wipe_confirm = Gtk.CheckButton()
            confirm_label = Gtk.Label(
                label=_("I understand that all data on this disk will be permanently erased."),
                xalign=0,
            )
            confirm_label.set_line_wrap(True)
            confirm_box.pack_start(self.wipe_confirm, False, False, 0)
            confirm_box.pack_start(confirm_label, True, True, 0)
            self.wipe_confirm.connect(
                "toggled",
                lambda btn: self.next_button.set_sensitive(
                    btn.get_active() and getattr(self, "summary_package_can_continue", True)
                )
                if hasattr(self, "next_button")
                else None,
            )
            banner.pack_start(confirm_box, False, False, 0)
            box.pack_start(banner, False, False, 0)

            disk_meta = ""
            try:
                for dev in find_available_disks():
                    path = "/dev/%s" % dev.get("name", "")
                    if path == self.state.target_device:
                        bits = [self.state.filesystem]
                        if dev.get("size"):
                            bits.append(dev["size"])
                        if dev.get("model"):
                            bits.append(dev["model"])
                        if dev.get("serial"):
                            serial = dev["serial"]
                            bits.append("S/N: %s%s" % (serial[:16], "…" if len(serial) > 16 else ""))
                        if dev.get("transport"):
                            bits.append(dev["transport"])
                        disk_meta = " · ".join(bits)
                        break
            except Exception:
                disk_meta = self.state.filesystem
            if plan.use_efi and plan.use_gpt:
                disk_meta = (disk_meta + " · " if disk_meta else "") + _("UEFI/GPT layout (no BIOS bootloader)")
            elif plan.use_efi:
                disk_meta = (disk_meta + " · " if disk_meta else "") + _("UEFI/MBR layout (no BIOS bootloader)")
            else:
                disk_meta = (disk_meta + " · " if disk_meta else "") + _("BIOS/MBR layout with bootloader")
            target_label = Gtk.Label(xalign=0)
            target_label.set_line_wrap(True)
            target_label.set_markup(
                "<b>{}</b> {}\n<span alpha='70%'>{}</span>".format(
                    GLib.markup_escape_text(_("Target disk:")),
                    GLib.markup_escape_text(self.state.target_device or ""),
                    GLib.markup_escape_text(disk_meta),
                )
            )
            box.pack_start(self._summary_card(_("Target"), target_label), False, False, 0)

        if can_install and plan and plan.resize:
            banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            banner.get_style_context().add_class("warning-banner")
            warn = Gtk.Label(xalign=0)
            warn.set_line_wrap(True)
            warn.set_markup(
                "<b>{}</b>\n{}".format(
                    GLib.markup_escape_text(_("Existing partition will be resized")),
                    GLib.markup_escape_text(
                        _("Back up important files and connect reliable power. Interruption during filesystem resize can cause data loss.")
                    ),
                )
            )
            banner.pack_start(warn, False, False, 0)
            confirm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.resize_confirm = Gtk.CheckButton()
            confirm_label = Gtk.Label(label=_("I understand that the existing partition will be modified."), xalign=0)
            confirm_label.set_line_wrap(True)
            confirm_box.pack_start(self.resize_confirm, False, False, 0)
            confirm_box.pack_start(confirm_label, True, True, 0)
            self.resize_confirm.connect(
                "toggled",
                lambda btn: self.next_button.set_sensitive(
                    btn.get_active() and getattr(self, "summary_package_can_continue", True)
                ) if hasattr(self, "next_button") else None,
            )
            banner.pack_start(confirm_box, False, False, 0)
            box.pack_start(banner, False, False, 0)

        plan_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for line in lines:
            label = Gtk.Label(label=line, xalign=0)
            label.set_line_wrap(True)
            if not can_install:
                label.set_markup(
                    "<span foreground='red'>{}</span>".format(GLib.markup_escape_text(line))
                )
            plan_box.pack_start(label, False, False, 0)
        box.pack_start(self._summary_card(_("Partition plan"), plan_box), False, False, 0)

        mode_label = Gtk.Label(xalign=0)
        mode_label.set_text(
            _("Live system") if self.state.install_mode != "native" else _("Full (native) installation")
        )
        box.pack_start(self._summary_card(_("Installation mode"), mode_label), False, False, 0)

        if self.state.install_mode == "live" and self.state.persistence_mode != "none":
            mode = self.state.persistence_mode
            labels = {
                "native": _("Native persistent changes"),
                "dynfilefs": _("Expandable persistent changes (DynFileFS): {size} MiB").format(size=self.state.persistence_size_mib),
                "raw": _("Fixed-size persistent changes (Raw image): {size} MiB").format(size=self.state.persistence_size_mib),
                "luks": _("Encrypted persistent changes (LUKS): {size} MiB\nchanges.luks will be created and unlocked by initrd on first boot.").format(size=self.state.persistence_size_mib),
            }
            persistence_label = Gtk.Label(label=labels.get(mode, mode), xalign=0)
            persistence_label.set_line_wrap(True)
            box.pack_start(self._summary_card(_("Session storage"), persistence_label), False, False, 0)

        box.pack_start(self._summary_card(_("Security profile"), self._security_summary_widget()), False, False, 0)

        if self.state.user_config_customized or self.state.install_mode == "native":
            cfg = self.state.user_config
            summary = Gtk.Label(xalign=0)
            summary.set_line_wrap(True)
            root_note = ""
            if self.state.install_mode == "native" and not cfg.root_password:
                root_note = "\n" + _("Root account will be locked.")
            if cfg.network_method == "static":
                network_note = "\n" + _("Network: static IPv4 on {interface} ({address}/{prefix})").format(
                    interface=cfg.network_interface,
                    address=cfg.network_address,
                    prefix=cfg.network_prefix,
                )
            else:
                network_note = "\n" + _("Network: automatic (DHCP)")
            summary.set_text(
                _("user {user}, host {host}, locale {locale}, timezone {timezone}, keyboard {keyboard}").format(
                    user=cfg.username or _("unchanged"),
                    host=cfg.hostname or _("unchanged"),
                    locale=cfg.locale or _("unchanged"),
                    timezone=cfg.timezone or _("unchanged"),
                    keyboard=cfg.keyboard or _("unchanged"),
                )
                + root_note
                + network_note
            )
            box.pack_start(self._summary_card(_("System"), summary), False, False, 0)

        if self.available_modules:
            modules = normalize_selected_modules(self.available_modules, self.state.selected_modules)
            modules_label = Gtk.Label(xalign=0)
            modules_label.set_text(
                _("{selected} of {total}").format(selected=len(modules), total=len(self.available_modules))
            )
            box.pack_start(self._summary_card(_("Selected modules"), modules_label), False, False, 0)

        package_can_continue = True
        self.summary_package_can_continue = True
        if self.state.install_mode == "native":
            required_packages = native_missing_packages(
                plan.use_efi if plan else False,
                self.state.filesystem,
                alongside=self.state.placement != "erase_all",
            )
            package_result = preflight_package_download(required_packages)
            missing = package_result.get("missing") or []
            if missing:
                pkg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                pkg_text = Gtk.Label(
                    label=_(
                        "Some packages needed for a standard native bootloader are missing: {packages}"
                    ).format(packages=", ".join(missing)),
                    xalign=0,
                )
                pkg_text.set_line_wrap(True)
                pkg_box.pack_start(pkg_text, False, False, 0)
                self.download_packages_check = Gtk.CheckButton(label=_("Download missing packages now (recommended)"))
                self.download_packages_check.set_active(self.state.download_missing_packages)
                self.download_packages_check.connect("toggled", self._on_download_packages_toggled)
                pkg_box.pack_start(self.download_packages_check, False, False, 0)
                if self.state.download_missing_packages:
                    choice_text = _(
                        "Recommended for most users. The installer will download the missing boot packages now, so the installed system starts like a normal Linux system and future kernel updates are handled normally. This requires internet access."
                    )
                else:
                    choice_text = _(
                        "Use only if you must install without internet. The installer will use a basic fallback setup where possible. The installed system may have limited bootloader and kernel update support. If you are not sure, keep this checked."
                    )
                choice_note = Gtk.Label(label=choice_text, xalign=0)
                choice_note.set_line_wrap(True)
                choice_note.get_style_context().add_class("dim-label")
                pkg_box.pack_start(choice_note, False, False, 0)
                status = []
                if not package_result.get("apt_available"):
                    status.append(_("apt-get is not available"))
                if not package_result.get("internet"):
                    status.append(_("internet connection is not available"))
                if int(package_result.get("free_space_mib") or 0) < int(package_result.get("min_space_mib") or 0):
                    status.append(
                        _("not enough free space in APT cache ({free} MiB available, {need} MiB required)").format(
                            free=package_result.get("free_space_mib"), need=package_result.get("min_space_mib")
                        )
                    )
                if status:
                    status_label = Gtk.Label(label=_("Cannot download yet: ") + "; ".join(status), xalign=0)
                    status_label.set_line_wrap(True)
                    status_label.get_style_context().add_class("dim-label")
                    pkg_box.pack_start(status_label, False, False, 0)
                requires_grub = native_requires_standard_bootloader(
                    plan.use_efi if plan else False,
                    self.state.placement,
                )
                if requires_grub and not self.state.download_missing_packages:
                    package_can_continue = False
                    blocker = Gtk.Label(xalign=0)
                    blocker.set_line_wrap(True)
                    blocker.set_markup(
                        "<b>{}</b>\n{}".format(
                            GLib.markup_escape_text(_("This installation cannot use the offline fallback bootloader.")),
                            GLib.markup_escape_text(_("Enable package download and connect to the internet before modifying the disk.")),
                        )
                    )
                    pkg_box.pack_start(blocker, False, False, 0)
                elif self.state.download_missing_packages and not preflight_ok(package_result):
                    package_can_continue = False
                    blocker = Gtk.Label(xalign=0)
                    blocker.set_line_wrap(True)
                    blocker.set_markup(
                        "<b>{}</b>\n{}".format(
                            GLib.markup_escape_text(_("Cannot continue while package download is selected.")),
                            GLib.markup_escape_text(
                                _("Fix the download requirements above, or uncheck package download to continue without it.")
                            ),
                        )
                    )
                    pkg_box.pack_start(blocker, False, False, 0)
                self.summary_package_can_continue = package_can_continue
                box.pack_start(self._summary_card(_("Required packages"), pkg_box), False, False, 0)

        manual_destructive = bool(can_install and self.state.placement == "manual" and plan.destructive)
        if manual_destructive:
            banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            banner.get_style_context().add_class("warning-banner")
            confirm = Gtk.CheckButton(label=_("I understand the listed manual actions can permanently destroy data."))
            banner.pack_start(confirm, True, True, 0)
            confirm.connect("toggled", lambda btn: self.next_button.set_sensitive(btn.get_active() and getattr(self, "summary_package_can_continue", True)))
            box.pack_start(banner, False, False, 0)
        wipe = bool(can_install and plan and getattr(plan, "wipe_disk", False))
        resize = bool(can_install and plan and getattr(plan, "resize", False))
        if wipe:
            self._nav(False if package_can_continue else False, _("Erase disk and install"), destructive_next=True)
            if hasattr(self, "wipe_confirm") and self.wipe_confirm.get_active() and package_can_continue:
                self.next_button.set_sensitive(True)
        elif resize:
            self._nav(False, _("Resize and install"), destructive_next=True)
            if hasattr(self, "resize_confirm") and self.resize_confirm.get_active() and package_can_continue:
                self.next_button.set_sensitive(True)
        elif manual_destructive:
            self._nav(False, _("Apply manual plan"), destructive_next=True)
        else:
            self._nav(can_install and package_can_continue, _("Install"))

    def _on_download_packages_toggled(self, button):
        self.state.download_missing_packages = button.get_active()
        self._show_step(self.current_step)

    def _step_install(self):
        if self.install_running:
            return
        self._page_title(_("Installing"), _("Keep this window open until installation finishes."))
        self.phase_label = Gtk.Label(label=_("Preparing"), xalign=0)
        self.phase_label.set_markup("<b>{}</b>".format(GLib.markup_escape_text(_("Preparing"))))
        self.status = Gtk.Label(label=_("Preparing..."), xalign=0)
        self.status.set_line_wrap(True)
        self.finish_box = None
        self.progress = Gtk.ProgressBar()
        self.content_body.pack_start(self.phase_label, False, False, 0)
        self.content_body.pack_start(self.status, False, False, 0)
        self.content_body.pack_start(self.progress, False, False, 0)
        self.log_buf = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buf)
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._reset_install_log()
        for line in install_session_summary(self.state):
            self._append_log(line)
        self._append_log("Log file: " + self.install_log_path)
        backend = "run_native_install" if self.state.install_mode == "native" else "run_live_install"
        self._append_log("Backend call: {}(state, progress_cb, log_cb)".format(backend))
        self._append_log("Equivalent command: " + backend_command_for_state(self.state))
        # Give the log a real minimum height; nested expand inside outer scroll is ignored.
        sw = Gtk.ScrolledWindow()
        if hasattr(sw, "set_min_content_height"):
            sw.set_min_content_height(220)
        sw.set_size_request(-1, 220)
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_hexpand(True)
        sw.set_vexpand(True)
        sw.add(self.log_view)
        details_frame = Gtk.Frame()
        details_frame.set_hexpand(True)
        details_frame.set_vexpand(True)
        details_frame.add(sw)
        self.details_expander = Gtk.Expander(label=_("Show Details"))
        self.details_expander.set_hexpand(True)
        self.details_expander.set_vexpand(True)
        self.details_expander.add(details_frame)
        self.details_expander.connect("notify::expanded", lambda *_: GLib.idle_add(self._clamp_window_size))
        self.content_body.pack_start(self.details_expander, True, True, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.get_style_context().add_class("installer-nav-separator")
        self.content_footer.pack_start(sep, False, False, 0)

        self.cancel_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.cancel_box.set_margin_top(8)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.cancel_box.pack_start(spacer, True, True, 0)
        self.cancel_button = self._style_button(Gtk.Button(label=_("Cancel")))
        self.cancel_button.connect("clicked", self._on_cancel_install_clicked)
        self.cancel_box.pack_start(self.cancel_button, False, False, 0)
        self.content_footer.pack_start(self.cancel_box, False, False, 0)

        self.state.cancel_requested = False
        self.install_running = True
        self._update_sidebar()
        pause_disk_monitoring()
        threading.Thread(target=self._run_install, daemon=False).start()

    def _on_cancel_install_clicked(self, _button):
        if not self.install_running:
            return
        self.state.cancel_requested = True
        self.cancel_button.set_sensitive(False)
        self.status.set_text(_("Cancel requested. Waiting for the current step to finish..."))
        self._append_log(_("Cancel requested by user."))

    def _progress(self, percent, message):
        phase = install_phase_for_percent(percent)

        def update():
            self.progress.set_fraction(percent / 100.0)
            self.status.set_text(message)
            if hasattr(self, "phase_label"):
                self.phase_label.set_markup("<b>{}</b>".format(GLib.markup_escape_text(phase)))
            self._append_log("{:3d}% {}".format(percent, message))
            return False

        GLib.idle_add(update)

    def _append_log(self, message):
        formatted = format_log_message(message)
        try:
            with self._install_log_lock:
                with open(self.install_log_path, "a", encoding="utf-8") as log_file:
                    log_file.write(formatted + "\n")
        except OSError:
            pass

        def _do():
            end = self.log_buf.get_end_iter()
            self.log_buf.insert(end, formatted + "\n")
            if self.log_view is not None and self.log_view.get_buffer() is self.log_buf:
                end = self.log_buf.get_end_iter()
                self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
            return False

        GLib.idle_add(_do)

    def _reset_install_log(self):
        try:
            os.makedirs(INSTALL_LOG_DIR, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            history_path = os.path.join(INSTALL_LOG_DIR, "installer-{}.log".format(timestamp))
            suffix = 1
            while os.path.exists(history_path):
                history_path = os.path.join(INSTALL_LOG_DIR, "installer-{}-{}.log".format(timestamp, suffix))
                suffix += 1
            with self._install_log_lock:
                descriptor = os.open(history_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                os.close(descriptor)
                if os.path.lexists(INSTALL_LOG_PATH):
                    if os.path.islink(INSTALL_LOG_PATH):
                        os.unlink(INSTALL_LOG_PATH)
                    else:
                        previous_path = os.path.join(
                            INSTALL_LOG_DIR, "installer-previous-{}.log".format(timestamp)
                        )
                        os.replace(INSTALL_LOG_PATH, previous_path)
                os.symlink(os.path.basename(history_path), INSTALL_LOG_PATH)
            self.install_log_path = history_path
            try:
                shutil.chown(history_path, user="root", group="adm")
            except (LookupError, OSError):
                pass
        except OSError:
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.install_log_path = os.path.join(tempfile.gettempdir(), "minios-installer-{}.log".format(timestamp))
            descriptor = os.open(self.install_log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            os.close(descriptor)

    def _run_install(self):
        try:
            runner = run_native_install if self.state.install_mode == "native" else run_live_install
            runner(self.state, self._progress, self._append_log)
        except InstallCanceled as exc:
            GLib.idle_add(self._append_log, "ERROR: {}".format(exc))
            GLib.idle_add(self.details_expander.set_expanded, True)
            GLib.idle_add(
                self._show_install_finished,
                False,
                _("Installation was canceled. You can go back and try again."),
            )
        except Exception as exc:
            GLib.idle_add(self._append_log, "ERROR: {}".format(exc))
            GLib.idle_add(self._append_log, traceback.format_exc())
            GLib.idle_add(self.details_expander.set_expanded, True)
            GLib.idle_add(
                self._show_install_finished,
                False,
                _("Installation failed. Check the log above for details."),
            )
        else:
            GLib.idle_add(
                self._show_install_finished,
                True,
                _(
                    "Installation complete. You can keep using this live session, or restart now to boot from the installed system."
                ),
            )
        finally:
            GLib.idle_add(self._install_thread_finished)

    def _install_thread_finished(self):
        self.install_running = False
        self._update_sidebar()
        resume_disk_monitoring()
        return False

    def _show_install_finished(self, success, message):
        self.install_running = False
        self._update_sidebar()
        self.status.set_text(message)
        if hasattr(self, "phase_label"):
            phase = _("Complete") if success else _("Failed")
            self.phase_label.set_markup("<b>{}</b>".format(GLib.markup_escape_text(phase)))
        self.progress.set_fraction(1.0 if success else self.progress.get_fraction())
        if getattr(self, "cancel_box", None) is not None:
            parent = self.cancel_box.get_parent()
            if parent is not None:
                parent.remove(self.cancel_box)
            self.cancel_box = None
            self.cancel_button = None
        if self.finish_box:
            return False

        self.finish_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.finish_box.set_margin_top(8)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.finish_box.pack_start(spacer, True, True, 0)

        close_btn = self._style_button(Gtk.Button(label=_("Close")))
        close_btn.connect("clicked", lambda *_: self.destroy())
        self.finish_box.pack_start(close_btn, False, False, 0)

        if success:
            reboot_btn = self._style_button(Gtk.Button(label=_("Restart Now")), suggested=True)
            reboot_btn.set_size_request(130, 34)
            reboot_btn.connect("clicked", self._on_reboot_clicked)
            self.finish_box.pack_start(reboot_btn, False, False, 0)
        else:
            back_btn = self._style_button(Gtk.Button(label=_("Back to Summary")), suggested=True)
            back_btn.set_size_request(150, 34)
            back_btn.connect("clicked", self._on_retry_from_summary)
            self.finish_box.pack_start(back_btn, False, False, 0)

        self.content_footer.pack_start(self.finish_box, False, False, 0)
        self.finish_box.show_all()
        return False

    def _on_retry_from_summary(self, _button):
        self.state.cancel_requested = False
        self.state.partition_plan = None
        self.finish_box = None
        self.install_running = False
        for idx, (name, _label) in enumerate(self.STEPS):
            if name == "summary":
                self._show_step(idx)
                return
        self._show_step(max(0, len(self.STEPS) - 2))

    def _on_reboot_clicked(self, _button):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=_("Restart now?"),
        )
        dlg.format_secondary_text(_("Save any open work before restarting."))
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(_("Restart"), Gtk.ResponseType.OK)
        response = dlg.run()
        dlg.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            subprocess.call(["sync"])
            subprocess.Popen(["reboot"])
        except Exception as exc:
            self._show_error(str(exc))


class MiniOSInstallerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID)
        self.window = None

    def do_activate(self):
        if os.geteuid() != 0:
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Root Privileges Required"),
            )
            dlg.format_secondary_text(_("This installer must be run as root."))
            dlg.run()
            dlg.destroy()
            sys.exit(1)
        if self.window:
            self.window.present()
            return
        self.window = InstallerWindow(self)
        self.window.show_all()
        self.window.present()


def main():
    try:
        return MiniOSInstallerApp().run(sys.argv)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
