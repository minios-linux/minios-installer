#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple


KERNEL_METADATA_PATH = os.path.join("usr", "share", "minios", "kernel-dpkg")
INCOMPLETE_MARKER = os.path.join("var", "lib", "minios-installer", "kernel-registration.incomplete")

_TOP_LEVEL_FIELDS = {
    "format", "install_policy", "update_policy", "userspace", "kernel",
    "repositories", "packages",
}
_USERSPACE_FIELDS = {"family", "suite", "dpkg_architecture"}
_KERNEL_FIELDS = {"distribution", "version", "package_architecture"}
_REPOSITORY_FIELDS = {
    "family", "uris", "suite", "components", "architectures",
    "release_identity", "keyring",
}
_RELEASE_FIELDS = {"origin", "label", "codename", "inrelease_sha256"}
_KEYRING_FIELDS = {"path", "sha256", "fingerprints"}
_PACKAGE_BASE_FIELDS = {
    "role", "name", "version", "architecture", "dpkg_instance",
    "source_package", "source_archive_sha256", "registration", "apt_mark", "hold",
}
_PACKAGE_SYNTHETIC_FIELDS = _PACKAGE_BASE_FIELDS | {"status", "info_prefix"}
_ROLES = {
    "tracking-meta", "base-meta", "image", "binary", "base", "modules",
    "modules-extra", "dependency",
}
_PAYLOAD_ROLES = {"image", "binary", "base", "modules", "modules-extra"}
_AUTO_ROLES = {"base-meta", "image", "binary", "base", "modules", "modules-extra"}
_INFO_SUFFIXES = {
    "list", "md5sums", "conffiles", "preinst", "postinst", "prerm", "postrm",
    "triggers", "shlibs", "symbols", "config", "templates",
}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
_ARCH_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]*$")
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_APT_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_. -]*$")
_DEPMOD_OUTPUTS = {
    "modules.alias", "modules.alias.bin", "modules.builtin.bin", "modules.dep",
    "modules.dep.bin", "modules.devname", "modules.softdep", "modules.symbols",
    "modules.symbols.bin", "modules.weakdep",
}
_KERNEL_PACKAGE_PREFIXES = (
    "linux-image-", "linux-base-", "linux-binary-", "linux-modules-",
    "linux-modules-extra-",
)


class KernelMetadataError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise KernelMetadataError("Invalid format-1 kernel metadata: " + message)


def _require_exact_keys(value: Dict, expected: Set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        _fail("{} has {}".format(context, "; ".join(details)))


def _require_string(value: object, context: str, pattern=None) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail("{} must be a non-empty string".format(context))
    if pattern is not None and not pattern.match(value):
        _fail("{} has an invalid value".format(context))
    return value


def _require_string_list(value: object, context: str, pattern=None) -> List[str]:
    if not isinstance(value, list) or not value:
        _fail("{} must be a non-empty array".format(context))
    result = []
    for index, item in enumerate(value):
        item = _require_string(item, "{}[{}]".format(context, index), pattern)
        if item in result:
            _fail("{} contains a duplicate value".format(context))
        result.append(item)
    return result


def _json_object(pairs: List[Tuple[str, object]]) -> Dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member: {}".format(key))
        result[key] = value
    return result


def _read_bytes(path: str, limit: int, context: str) -> bytes:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            _fail("{} is not a regular file".format(context))
        if st.st_size > limit:
            _fail("{} exceeds the size limit".format(context))
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError as exc:
        _fail("{} cannot be read: {}".format(context, exc))
    if len(data) > limit:
        _fail("{} exceeds the size limit".format(context))
    return data


def _read_text(path: str, limit: int, context: str) -> str:
    data = _read_bytes(path, limit, context)
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("{} is not valid UTF-8".format(context))
    return ""  # pragma: no cover


def _load_json(path: str, limit: int, context: str):
    text = _read_text(path, limit, context)
    try:
        return json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid number " + value)),
        )
    except (TypeError, ValueError) as exc:
        _fail("{} is not exact JSON: {}".format(context, exc))


def _safe_relative_path(metadata_dir: str, relative: object, context: str) -> str:
    relative = _require_string(relative, context)
    if os.path.isabs(relative) or relative != relative.replace("\\", "/"):
        _fail("{} must be a portable relative path".format(context))
    normalized = os.path.normpath(relative)
    if normalized in ("", ".", "..") or normalized.startswith("../") or normalized != relative:
        _fail("{} is not a normalized relative path".format(context))
    current = metadata_dir
    for component in relative.split("/"):
        if component in ("", ".", ".."):
            _fail("{} contains an unsafe component".format(context))
        current = os.path.join(current, component)
        if os.path.lexists(current) and os.path.islink(current):
            _fail("{} traverses a symbolic link".format(context))
    return current


def _target_path(root: str, absolute: str, context: str) -> str:
    root_path = os.path.abspath(root)
    try:
        root_stat = os.lstat(root_path)
    except OSError as exc:
        _fail("{} root cannot be inspected: {}".format(context, exc))
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        _fail("{} root is not a real directory".format(context))
    path = os.path.join(root_path, absolute.lstrip("/"))
    parent = os.path.realpath(os.path.dirname(path))
    try:
        confined = os.path.commonpath((root_path, parent)) == root_path
    except ValueError:
        confined = False
    if not confined:
        _fail("{} traverses outside the target root".format(context))
    return path


def _ensure_target_directory(root: str, absolute: str, context: str) -> str:
    root_path = os.path.abspath(root)
    try:
        root_stat = os.lstat(root_path)
    except OSError as exc:
        raise KernelMetadataError("{} root cannot be inspected: {}".format(context, exc))
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise KernelMetadataError("{} root is not a real directory".format(context))
    relative = absolute.lstrip("/")
    if os.path.normpath(relative) != relative or relative.startswith("../"):
        raise KernelMetadataError("kernel registration destination is unsafe")
    current = root_path
    for component in relative.split("/") if relative else []:
        current = os.path.join(current, component)
        if os.path.lexists(current):
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise KernelMetadataError("{} traverses a non-directory or symbolic link".format(context))
        else:
            os.mkdir(current, 0o755)
    return current


def _require_target_directory(root: str, relative: str, context: str) -> str:
    current = os.path.abspath(root)
    try:
        root_stat = os.lstat(current)
    except OSError as exc:
        _fail("{} root cannot be inspected: {}".format(context, exc))
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _fail("{} root is not a real directory".format(context))
    for component in relative.split("/"):
        current = os.path.join(current, component)
        try:
            st = os.lstat(current)
        except OSError as exc:
            _fail("{} cannot be inspected: {}".format(context, exc))
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            _fail("{} traverses a non-directory or symbolic link".format(context))
    return current


def _optional_target_path(root: str, absolute: str, context: str, directory: bool = False) -> str:
    path = _target_path(root, absolute, context)
    root_path = os.path.abspath(root)
    relative = os.path.relpath(path, root_path)
    components = relative.split(os.path.sep)
    current = root_path
    for index, component in enumerate(components):
        current = os.path.join(current, component)
        if not os.path.lexists(current):
            break
        st = os.lstat(current)
        is_parent = index < len(components) - 1
        if is_parent or directory:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                _fail("{} traverses a non-directory or symbolic link".format(context))
        elif stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            _fail("{} is not a regular file".format(context))
    return path


def _require_single_line(value: object, context: str, pattern=None) -> str:
    value = _require_string(value, context, pattern)
    if any(char in value for char in "\r\n"):
        _fail("{} must be a single-line value".format(context))
    return value


def _parse_control(text: str, context: str) -> Tuple[Dict[str, str], str]:
    if "\x00" in text or "\r" in text:
        _fail("{} contains invalid control characters".format(context))
    stripped = text.strip("\n")
    if not stripped or "\n\n" in stripped:
        _fail("{} must contain exactly one control stanza".format(context))
    fields = {}  # type: Dict[str, str]
    current = None  # type: Optional[str]
    for line in stripped.split("\n"):
        if line.startswith((" ", "\t")):
            if current is None:
                _fail("{} starts with a continuation line".format(context))
            fields[current] += "\n" + line
            continue
        if ":" not in line:
            _fail("{} contains a malformed control line".format(context))
        name, value = line.split(":", 1)
        if (not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*$", name) or
                (value and not value.startswith(" "))):
            _fail("{} contains a malformed field".format(context))
        if name in fields:
            _fail("{} contains duplicate field {}".format(context, name))
        fields[name] = value[1:] if value else ""
        current = name
    return fields, stripped + "\n"


def _status_stanzas(path: str) -> List[Tuple[Dict[str, str], str]]:
    if not os.path.isfile(path):
        _fail("target dpkg status is missing")
    text = _read_text(path, 64 * 1024 * 1024, "target dpkg status")
    result = []
    for index, stanza in enumerate(re.split(r"\n\s*\n", text.strip())):
        if stanza.strip():
            result.append(_parse_control(stanza + "\n", "target status stanza {}".format(index)))
    return result


def _control_source(fields: Dict[str, str]) -> str:
    return fields.get("Source", fields.get("Package", "")).split(None, 1)[0]


def _is_installed(fields: Dict[str, str]) -> bool:
    parts = fields.get("Status", "").split()
    return len(parts) == 3 and parts[0] in ("install", "hold") and parts[1:] == ["ok", "installed"]


def _target_identity(root: str) -> Tuple[str, str, str]:
    release_path = os.path.join(root, "etc", "os-release")
    if os.path.islink(release_path):
        resolved = os.path.realpath(release_path)
        root_path = os.path.abspath(root)
        try:
            confined = os.path.commonpath((root_path, resolved)) == root_path
        except ValueError:
            confined = False
        if not confined:
            _fail("target os-release symbolic link escapes the target root")
        release_path = resolved
    text = _read_text(release_path, 128 * 1024, "target os-release")
    release = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.startswith(('"', "'")) and len(value) >= 2 and value[-1] == value[0]:
            value = value[1:-1]
        release[key] = value
    family = release.get("ID", "").lower()
    if family == "devuan":
        manifest_family = "debian"
    elif family in ("debian", "ubuntu"):
        manifest_family = family
    else:
        _fail("target userspace family is unsupported")
    suite = release.get("VERSION_CODENAME", "") or release.get("DEBIAN_CODENAME", "")
    if not suite:
        _fail("target userspace suite cannot be determined")

    architectures = set()
    for fields, _text in _status_stanzas(os.path.join(root, "var", "lib", "dpkg", "status")):
        if not _is_installed(fields):
            continue
        if fields.get("Essential") == "yes" or fields.get("Package") == "base-files":
            architecture = fields.get("Architecture")
            if architecture and architecture != "all":
                architectures.add(architecture)
    if len(architectures) != 1:
        _fail("target native dpkg architecture cannot be determined exactly")
    return manifest_family, suite, next(iter(architectures))


def _canonical_instance(name: str, architecture: str, native_architecture: str) -> str:
    return name if architecture in ("all", native_architecture) else "{}:{}".format(name, architecture)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_payload_path(value: object, kernel_version: str, context: str) -> str:
    path = _require_string(value, context)
    if not path.startswith("/") or "//" in path or os.path.normpath(path) != path:
        _fail("{} is not a normalized absolute path".format(context))
    allowed = (
        path == "/",
        path == "/boot",
        path.startswith("/boot/"),
        path in ("/lib", "/lib/modules", "/usr", "/usr/lib", "/usr/lib/modules"),
        path.startswith("/lib/modules/{}/".format(kernel_version)),
        path == "/lib/modules/{}".format(kernel_version),
        path.startswith("/usr/lib/modules/{}/".format(kernel_version)),
        path == "/usr/lib/modules/{}".format(kernel_version),
    )
    if not any(allowed):
        _fail("{} escapes the kernel payload boundary".format(context))
    basename = os.path.basename(path)
    if path.startswith("/boot/initrd") or path.startswith("/boot/initramfs") or basename in _DEPMOD_OUTPUTS:
        _fail("{} claims a generated lifecycle output".format(context))
    return path


def _payload_entries(metadata_dir: str, package: Dict, kernel_version: str) -> List[Dict]:
    relative = package.get("payload_manifest")
    if relative is None:
        if package["role"] in _PAYLOAD_ROLES:
            _fail("package {} is missing payload_manifest".format(package["dpkg_instance"]))
        return []
    expected = "payload.d/{}.json".format(package["dpkg_instance"])
    if relative != expected:
        _fail("package {} payload_manifest is not canonical".format(package["dpkg_instance"]))
    path = _safe_relative_path(metadata_dir, relative, "package payload_manifest")
    payload = _load_json(path, 32 * 1024 * 1024, "package payload manifest")
    if not isinstance(payload, dict):
        _fail("package payload manifest must be an object")
    _require_exact_keys(payload, {"format", "dpkg_instance", "files"}, "package payload manifest")
    if type(payload["format"]) is not int or payload["format"] != 1:
        _fail("package payload manifest format must be integer 1")
    if payload["dpkg_instance"] != package["dpkg_instance"]:
        _fail("package payload manifest instance mismatch")
    if not isinstance(payload["files"], list):
        _fail("package payload manifest files must be an array")
    result = []
    seen = set()
    for index, entry in enumerate(payload["files"]):
        context = "payload file {}".format(index)
        if not isinstance(entry, dict):
            _fail("{} must be an object".format(context))
        entry_type = entry.get("type")
        expected_fields = {"path", "type", "sha256"} if entry_type == "file" else {"path", "type"}
        if entry_type == "symlink":
            expected_fields.add("target")
        _require_exact_keys(entry, expected_fields, context)
        if entry_type not in ("file", "directory", "symlink"):
            _fail("{} has an unsupported type".format(context))
        payload_path = _normalize_payload_path(entry["path"], kernel_version, context + " path")
        if payload_path in seen:
            _fail("payload manifest contains duplicate path {}".format(payload_path))
        seen.add(payload_path)
        normalized = {"path": payload_path, "type": entry_type}
        if entry_type == "file":
            digest = _require_string(entry["sha256"], context + " sha256", _HASH_RE).lower()
            normalized["sha256"] = digest
        elif entry_type == "symlink":
            target = _require_string(entry["target"], context + " target")
            if "\x00" in target or os.path.isabs(target):
                _fail("{} has an unsafe symlink target".format(context))
            resolved = os.path.normpath(
                os.path.join(os.path.dirname(payload_path), target)
            )
            _normalize_payload_path(
                resolved, kernel_version, context + " symlink target"
            )
            normalized["target"] = target
        result.append(normalized)
    return result


def _read_list(path: str, kernel_version: str, context: str) -> List[str]:
    text = _read_text(path, 32 * 1024 * 1024, context)
    result = []
    for index, line in enumerate(text.splitlines()):
        if not line or line != line.strip():
            _fail("{} contains an empty or non-canonical line".format(context))
        if line == "/.":
            value = "/"
        elif line.startswith("/./"):
            value = line[2:]
        else:
            value = line
        value = _normalize_payload_path(value, kernel_version, "{} line {}".format(context, index + 1))
        if value in result:
            _fail("{} contains duplicate path {}".format(context, value))
        result.append(value)
    return result


def _payload_source_path(root: str, payload_path: str, context: str,
                         external_paths: Optional[Dict[str, str]] = None) -> str:
    if payload_path in (external_paths or {}):
        path = external_paths[payload_path]
        if not os.path.isabs(path) or os.path.islink(path):
            _fail("{} external path {} is unsafe".format(context, payload_path))
        return path
    return _target_path(root, payload_path, context)


def _verify_payload(root: str, entries: List[Dict], context: str,
                    external_paths: Optional[Dict[str, str]] = None) -> None:
    external_paths = external_paths or {}
    for entry in entries:
        path = _payload_source_path(
            root, entry["path"], context + " payload path", external_paths)
        try:
            st = os.lstat(path)
        except OSError as exc:
            _fail("{} path {} is missing: {}".format(context, entry["path"], exc))
        if entry["type"] == "file":
            if not stat.S_ISREG(st.st_mode):
                _fail("{} path {} is not a regular file".format(context, entry["path"]))
            with open(path, "rb") as fh:
                digest = hashlib.sha256()
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                _fail("{} path {} has the wrong SHA-256".format(context, entry["path"]))
        elif entry["type"] == "directory" and not stat.S_ISDIR(st.st_mode):
            _fail("{} path {} is not a directory".format(context, entry["path"]))
        elif entry["type"] == "symlink":
            if not stat.S_ISLNK(st.st_mode) or os.readlink(path) != entry["target"]:
                _fail("{} path {} has the wrong symlink target".format(context, entry["path"]))


def _verify_md5sums(path: str, root: str, regular_paths: Set[str], context: str,
                    external_paths: Optional[Dict[str, str]] = None) -> None:
    text = _read_text(path, 32 * 1024 * 1024, context)
    found = set()
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{32})  ([^\x00]+)$", line)
        if not match:
            _fail("{} contains a malformed checksum".format(context))
        payload_path = "/" + match.group(2).lstrip("/")
        if payload_path in found or payload_path not in regular_paths:
            _fail("{} contains an unexpected checksum path".format(context))
        found.add(payload_path)
        target_path = _payload_source_path(
            root, payload_path, context + " checksum path", external_paths)
        with open(target_path, "rb") as fh:
            digest = hashlib.md5()  # nosec - dpkg compatibility metadata
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        if digest.hexdigest() != match.group(1).lower():
            _fail("{} checksum does not match {}".format(context, payload_path))
    if found != regular_paths:
        _fail("{} does not exactly cover the package regular files".format(context))


def _verify_kernel_architecture(root: str, native_architecture: str,
                                kernel: Dict,
                                external_paths: Optional[Dict[str, str]] = None) -> None:
    kernel_architecture = kernel["package_architecture"]
    if native_architecture == "amd64" and kernel_architecture == "i386":
        _fail("an i386 kernel cannot run amd64 userspace")
    config_path = "/boot/config-{}".format(kernel["version"])
    source = _payload_source_path(root, config_path, "kernel config", external_paths)
    config = _read_text(source, 16 * 1024 * 1024, "kernel config")
    symbols = set(line.strip() for line in config.splitlines() if line.endswith("=y"))
    if "CONFIG_EFI_STUB=y" not in symbols:
        _fail("selected kernel lacks CONFIG_EFI_STUB=y")
    if native_architecture == "i386" and kernel_architecture == "amd64":
        required = {"CONFIG_BINFMT_ELF=y", "CONFIG_IA32_EMULATION=y", "CONFIG_EFI_MIXED=y"}
        missing = sorted(required - symbols)
        if missing:
            _fail("selected mixed-mode kernel lacks {}".format(", ".join(missing)))


def _split_dependencies(value: str, delimiter: str) -> List[str]:
    result = []
    level = 0
    current = []
    for char in value:
        if char in "([":
            level += 1
        elif char in ")]":
            level = max(0, level - 1)
        if char == delimiter and level == 0:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    result.append("".join(current).strip())
    return [item for item in result if item]


def _dependency_atom(value: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    match = re.match(
        r"^([a-z0-9][a-z0-9+.-]*)(?::(any|native|[a-z0-9-]+))?"
        r"(?:\s*\((<<|<=|=|>=|>>)\s*([^()\s]+)\))?(?:\s*\[[^]]+\])?$",
        value,
    )
    if not match:
        _fail("unsupported dependency expression {!r}".format(value))
    return match.group(1), match.group(2), match.group(3), match.group(4)


def _version_satisfies(installed: str, relation: Optional[str], required: Optional[str]) -> bool:
    if relation is None:
        return True
    try:
        result = os.spawnlp(os.P_WAIT, "dpkg", "dpkg", "--compare-versions", installed, relation, required or "")
    except OSError:
        _fail("dpkg is required to validate versioned dependencies")
    return result == 0


def _verify_dependencies(packages: List[Dict], target_controls: Iterable[Dict[str, str]] = ()) -> None:
    available = {}  # type: Dict[str, List[Tuple[Dict[str, str], Optional[str]]]]

    def add_control(fields: Dict[str, str]) -> None:
        available.setdefault(fields.get("Package", ""), []).append((fields, fields.get("Version")))
        for provided in _split_dependencies(fields.get("Provides", ""), ","):
            match = re.match(r"^([a-z0-9][a-z0-9+.-]*)(?:\s*\(=\s*([^()\s]+)\))?$", provided)
            if not match:
                _fail("unsupported Provides expression {!r}".format(provided))
            available.setdefault(match.group(1), []).append((fields, match.group(2)))

    for fields in target_controls:
        add_control(fields)
    for package in packages:
        add_control(package["control"])
    for package in packages:
        fields = package.get("control")
        if not fields:
            continue
        for field_name in ("Pre-Depends", "Depends"):
            for group in _split_dependencies(fields.get(field_name, ""), ","):
                satisfied = False
                for atom in _split_dependencies(group, "|"):
                    name, qualifier, relation, required = _dependency_atom(atom)
                    for candidate, provided_version in available.get(name, []):
                        if not _is_installed(candidate):
                            continue
                        wanted_architecture = package["architecture"] if qualifier is None else qualifier
                        if wanted_architecture == "native":
                            wanted_architecture = package.get("native_architecture")
                        if wanted_architecture != "any" and candidate.get("Architecture") not in ("all", wanted_architecture):
                            continue
                        if qualifier == "any" and candidate.get("Architecture") != "all" and candidate.get("Multi-Arch") not in ("allowed", "foreign"):
                            continue
                        if relation is not None and not provided_version:
                            continue
                        if _version_satisfies(provided_version or candidate.get("Version", ""), relation, required):
                            satisfied = True
                            break
                    if satisfied:
                        break
                if not satisfied:
                    _fail("package {} has unsatisfied {} {}".format(package["dpkg_instance"], field_name, group))


def _verify_negative_relations(packages: List[Dict],
                               target_controls: Iterable[Dict[str, str]]) -> None:
    target = [fields for fields in target_controls if _is_installed(fields)]
    kernel = [package["control"] for package in packages
              if package.get("registration") == "synthetic-installed"]

    def conflicts(source: Iterable[Dict[str, str]], candidates: Iterable[Dict[str, str]],
                  owner: str) -> None:
        by_name = {}  # type: Dict[str, List[Dict[str, str]]]
        for fields in candidates:
            by_name.setdefault(fields.get("Package", ""), []).append(fields)
        for fields in source:
            for field_name in ("Conflicts", "Breaks"):
                for group in _split_dependencies(fields.get(field_name, ""), ","):
                    for atom in _split_dependencies(group, "|"):
                        name, _qualifier, relation, required = _dependency_atom(atom)
                        for candidate in by_name.get(name, []):
                            version = candidate.get("Version", "")
                            if relation is None or _version_satisfies(version, relation, required):
                                _fail("{} {} conflicts with installed package {} {}".format(
                                    owner, fields.get("Package", "kernel package"), name, version))

    conflicts(kernel, target, "kernel package")
    conflicts(target, kernel, "target package")


def _read_extended_states(path: str) -> List[Tuple[Dict[str, str], str]]:
    if not os.path.exists(path):
        return []
    text = _read_text(path, 32 * 1024 * 1024, "APT extended states")
    result = []
    for index, stanza in enumerate(re.split(r"\n\s*\n", text.strip())):
        if stanza.strip():
            result.append(_parse_control(stanza + "\n", "APT extended state {}".format(index)))
    return result


def _candidate_extended_states(path: str, packages: List[Dict], native_architecture: str) -> bytes:
    updates = {
        (item["name"], native_architecture if item["architecture"] == "all" else item["architecture"]): item["apt_mark"]
        for item in packages if item["apt_mark"] != "unchanged"
    }
    result = []
    seen = set()
    for fields, text in _read_extended_states(path):
        key = (fields.get("Package", ""), fields.get("Architecture", ""))
        if not key[1]:
            matching = [candidate for candidate in updates if candidate[0] == key[0]]
            if len(matching) == 1:
                key = matching[0]
        if key not in updates:
            result.append(text)
            continue
        if key in seen:
            _fail("APT extended states contains a duplicate package instance")
        seen.add(key)
        if updates[key] == "auto":
            result.append("Package: {}\nArchitecture: {}\nAuto-Installed: 1\n".format(*key))
    for key, mark in sorted(updates.items()):
        if key not in seen and mark == "auto":
            result.append("Package: {}\nArchitecture: {}\nAuto-Installed: 1\n".format(*key))
    return ("\n".join(item.rstrip("\n") for item in result).rstrip("\n") + ("\n" if result else "")).encode("utf-8")


def _package_patterns(packages: List[Dict], kernel_architecture: str) -> List[str]:
    result = []

    def add(pattern: str) -> None:
        if pattern in {prefix + "*" for prefix in _KERNEL_PACKAGE_PREFIXES}:
            _fail("kernel APT pattern is namespace-wide")
        qualified = "{}:{}".format(pattern, kernel_architecture)
        if qualified not in result:
            result.append(qualified)

    for package in packages:
        if package["role"] == "dependency":
            continue
        add(package["name"])
    tracking = next((package["name"] for package in packages
                     if package["role"] == "tracking-meta"), "")
    if tracking.startswith("linux-image-"):
        flavour = tracking[len("linux-image-"):]
        if not flavour:
            _fail("tracking-meta package does not identify a kernel flavour")
        role_prefixes = {
            "image": "linux-image-",
            "binary": "linux-binary-",
            "base": "linux-base-",
            "modules": "linux-modules-",
            "modules-extra": "linux-modules-extra-",
        }
        for package in packages:
            prefix = role_prefixes.get(package["role"])
            if not prefix:
                continue
            name = package["name"]
            suffix = "-" + flavour
            middle = name[len(prefix):-len(suffix)] if name.endswith(suffix) else ""
            if not name.startswith(prefix) or not middle:
                _fail("package {} does not preserve tracking kernel flavour {}".format(
                    package["dpkg_instance"], flavour))
        for pattern in (
                "linux-base-" + flavour,
                "linux-image-*-" + flavour,
                "linux-base-*-" + flavour,
                "linux-binary-*-" + flavour,
                "linux-modules-*-" + flavour,
                "linux-modules-extra-*-" + flavour):
            add(pattern)
    return result


def _apt_selector(selector: str) -> Tuple[bool, str, Optional[str]]:
    source = selector.startswith("src:")
    value = selector[4:] if source else selector
    architecture = None
    match = re.match(r"^(.*):(any|native|[a-z0-9][a-z0-9-]*)$", value)
    if match:
        value, architecture = match.groups()
    if not value:
        _fail("existing APT preferences contain an empty package selector")
    return source, value, architecture


def _apt_architecture_overlaps(selector_architecture: Optional[str],
                               native_architecture: str,
                               kernel_architecture: str,
                               includes_architecture_all: bool) -> bool:
    if includes_architecture_all:
        return True
    if selector_architecture == "any":
        return True
    if selector_architecture in (None, "native"):
        return native_architecture == kernel_architecture
    return selector_architecture == kernel_architecture


def _glob_parts(pattern: str) -> Tuple[str, str]:
    positions = [position for position in
                 (pattern.find("*"), pattern.find("?"), pattern.find("["))
                 if position >= 0]
    if not positions:
        return pattern, pattern
    prefix = pattern[:min(positions)]
    if "[" in pattern:
        return prefix, ""
    last = max(pattern.rfind("*"), pattern.rfind("?"))
    return prefix, pattern[last + 1:]


def _globs_may_overlap(left: str, right: str) -> bool:
    left_prefix, left_suffix = _glob_parts(left)
    right_prefix, right_suffix = _glob_parts(right)
    if (left_prefix and right_prefix and
            not (left_prefix.startswith(right_prefix) or
                 right_prefix.startswith(left_prefix))):
        return False
    if (left_suffix and right_suffix and
            not (left_suffix.endswith(right_suffix) or
                 right_suffix.endswith(left_suffix))):
        return False
    return True


def _regex_literal_prefix(expression: str) -> str:
    if not expression.startswith("^"):
        return ""
    result = []
    index = 1
    metacharacters = set(".^$*+?{}[]|()")
    while index < len(expression):
        char = expression[index]
        if char == "\\":
            index += 1
            if index >= len(expression) or expression[index].isalnum():
                break
            result.append(expression[index])
        elif char in metacharacters:
            break
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _regex_may_overlap(expression: str, targets: List[str]) -> bool:
    try:
        compiled = re.compile(expression)
    except re.error:
        _fail("existing APT preferences contain an invalid package regular expression")
    witnesses = []
    for target in targets:
        witnesses.extend(target.replace("*", value) for value in ("0", "7.0", "abi"))
    if any(compiled.search(witness) for witness in witnesses):
        return True
    prefix = _regex_literal_prefix(expression)
    if prefix:
        target_prefixes = [_glob_parts(target)[0] for target in targets]
        if all(not (prefix.startswith(target_prefix) or
                    target_prefix.startswith(prefix))
               for target_prefix in target_prefixes if target_prefix):
            return False
    # Python and APT use different regular-expression dialects. Unless an
    # anchored literal prefix proves disjointness, an inconclusive expression
    # must be treated as overlapping.
    return True


def _selector_overlaps(selector: str, targets: List[str],
                       native_architecture: str,
                       kernel_architecture: str,
                       includes_architecture_all: bool) -> bool:
    _source, value, architecture = _apt_selector(selector)
    if value.startswith("?"):
        # Modern APT package-pattern selectors can encode architecture and
        # source predicates internally. Their overlap is not safely decidable
        # with the format-1 glob policy, so fail closed.
        return True
    if not _apt_architecture_overlaps(
            architecture, native_architecture, kernel_architecture,
            includes_architecture_all):
        return False
    if value == "*":
        return True
    if value.startswith("/") or value.endswith("/"):
        if not (len(value) > 2 and value.startswith("/") and value.endswith("/")):
            _fail("existing APT preferences contain a malformed package regular expression")
        return _regex_may_overlap(value[1:-1], targets)
    return any(_globs_may_overlap(value, target) for target in targets)


def _preference_record(stanza: str) -> Optional[Tuple[List[str], str]]:
    fields = {}  # type: Dict[str, str]
    current = None  # type: Optional[str]
    for line in stanza.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if current in fields:
                fields[current] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.lower()
        current = key
        if key not in ("package", "pin", "pin-priority"):
            continue
        if key in fields:
            _fail("existing APT preferences contain duplicate {} fields".format(name))
        fields[key] = value.strip()
    if not fields:
        return None
    if set(fields) != {"package", "pin", "pin-priority"}:
        _fail("existing APT preferences contain an incomplete pin record")
    if not fields["package"] or not fields["pin"] or not re.match(r"^-?\d+$", fields["pin-priority"]):
        _fail("existing APT preferences contain a malformed pin record")
    return fields["package"].split(), fields["pin"]


def _reject_conflicting_pins(root: str, patterns: List[str], packages: List[Dict],
                             native_architecture: str,
                             kernel_architecture: str) -> None:
    paths = []
    primary = _optional_target_path(root, "/etc/apt/preferences", "existing APT preferences")
    if os.path.isfile(primary):
        paths.append(primary)
    directory = _optional_target_path(
        root, "/etc/apt/preferences.d", "existing APT preferences directory", directory=True
    )
    if os.path.isdir(directory):
        paths.extend(os.path.join(directory, name) for name in sorted(os.listdir(directory))
                     if os.path.isfile(os.path.join(directory, name)) and name != "minios-kernel")
    binary_targets = [pattern.rsplit(":", 1)[0] for pattern in patterns]
    source_targets = sorted({package["source_package"] for package in packages
                             if package["role"] != "dependency"})
    includes_architecture_all = any(
        package["architecture"] == "all" for package in packages
        if package["role"] != "dependency")
    for path in paths:
        text = _read_text(path, 4 * 1024 * 1024, "existing APT preferences")
        for stanza in re.split(r"\n\s*\n", text.strip()):
            record = _preference_record(stanza)
            if record is None:
                continue
            selectors, pin = record
            # APT 1.6 and current APT both use the first matching specific-form
            # record. Its priority need not be >= 1000 to bypass our later
            # allow/deny records, so reject on overlap rather than magnitude.
            package_specific = any(selector != "*" for selector in selectors)
            pin_parts = pin.split(None, 1)
            pin_kind = pin_parts[0]
            pin_value = pin_parts[1] if len(pin_parts) == 2 else ""
            wildcard_pin = ((any(char in pin_value for char in "*?[") or
                             "/" in pin_value) and pin_value.strip() != "*")
            if not package_specific:
                if ((pin_kind in ("version", "source-version") and
                     pin_value.strip() != "*") or wildcard_pin):
                    _fail("existing APT preferences override the narrow kernel source policy")
                continue
            overlap = False
            for selector in selectors:
                source, _value, _architecture = _apt_selector(selector)
                targets = source_targets if source else binary_targets
                if _selector_overlaps(
                        selector, targets, native_architecture,
                        kernel_architecture, includes_architecture_all):
                    overlap = True
                    break
            if overlap:
                _fail("existing APT preferences override the narrow kernel source policy")


def _repository_files(root: str, manifest: Dict, packages: List[Dict]) -> List[Tuple[str, bytes, int]]:
    if manifest["update_policy"] != "track":
        return []
    pin_lines = []
    approved_pins = []
    kernel_architecture = manifest["kernel"]["package_architecture"]
    patterns = _package_patterns(packages, kernel_architecture)
    _reject_conflicting_pins(
        root, patterns, packages, manifest["userspace"]["dpkg_architecture"],
        kernel_architecture)
    for repository in manifest["repositories"]:
        release = repository["release_identity"]
        pin = "release o={},l={},n={}".format(release["origin"], release["label"], release["codename"])
        if pin not in approved_pins:
            approved_pins.append(pin)
    for pattern in patterns:
        for pin in approved_pins:
            pin_lines.extend(["Package: " + pattern, "Pin: " + pin, "Pin-Priority: 990", ""])
        pin_lines.extend(["Package: " + pattern, "Pin: version *", "Pin-Priority: -1", ""])
    return [("/etc/apt/preferences.d/minios-kernel", "\n".join(pin_lines).encode("utf-8"), 0o644)]


class KernelRegistrationPlan:
    def __init__(self, root: str, manifest: Dict, packages: List[Dict], metadata_dir: str,
                 native_architecture: str, candidate_files: List[Tuple[str, bytes, int]]):
        self.root = root
        self.manifest = manifest
        self.packages = packages
        self.metadata_dir = metadata_dir
        self.native_architecture = native_architecture
        self.kernel_version = manifest["kernel"]["version"]
        self.candidate_files = candidate_files
        self._backups = []  # type: List[Tuple[str, Optional[str]]]
        self._workspace = tempfile.mkdtemp(prefix="minios-kernel-registration-")
        self.applied = False

    @property
    def count(self) -> int:
        return len([item for item in self.packages if item["registration"] == "synthetic-installed"])

    def verify_payload(self, root: Optional[str] = None) -> None:
        verify_root = root or self.root
        for package in self.packages:
            _verify_payload(verify_root, package.get("payload_entries", []), package["dpkg_instance"])
            if package.get("md5sums"):
                regular = {item["path"] for item in package.get("payload_entries", []) if item["type"] == "file"}
                _verify_md5sums(package["md5sums"], verify_root, regular, package["dpkg_instance"] + " md5sums")

    def apply(self, root: Optional[str] = None) -> int:
        if self.applied:
            raise KernelMetadataError("kernel registration transaction was already applied")
        target = root or self.root
        marker = os.path.join(target, INCOMPLETE_MARKER)
        _ensure_target_directory(target, os.path.dirname("/" + INCOMPLETE_MARKER),
                                 "kernel registration marker destination")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("format-1 kernel registration incomplete\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            for relative, data, mode in self.candidate_files:
                path = os.path.join(target, relative.lstrip("/"))
                directory = os.path.dirname(path)
                _ensure_target_directory(target, "/" + os.path.relpath(directory, target).replace(os.path.sep, "/"),
                                         "kernel registration destination")
                if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
                    raise KernelMetadataError("kernel registration destination is not a regular file")
                fd, staged = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".minios-stage.", dir=directory)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(staged, mode)
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    try:
                        os.unlink(staged)
                    except OSError:
                        pass
                    raise
                backup = None
                if os.path.lexists(path):
                    backup_fd, backup = tempfile.mkstemp(
                        prefix="." + os.path.basename(path) + ".minios-backup.", dir=directory
                    )
                    os.close(backup_fd)
                    os.unlink(backup)
                    try:
                        os.replace(path, backup)
                    except BaseException:
                        os.unlink(staged)
                        raise
                try:
                    os.replace(staged, path)
                except BaseException:
                    try:
                        os.unlink(staged)
                    except OSError:
                        pass
                    if backup is not None:
                        os.rename(backup, path)
                    raise
                self._backups.append((path, backup))
            self.applied = True
            return self.count
        except BaseException as exc:
            self.rollback(str(exc), root=target)
            raise

    def rollback(self, reason: str, root: Optional[str] = None) -> None:
        target = root or self.root
        for path, backup in reversed(self._backups):
            try:
                if os.path.lexists(path):
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path)
                    else:
                        os.unlink(path)
                if backup is not None:
                    os.rename(backup, path)
            except OSError:
                pass
        self._backups = []
        self.applied = False
        marker = os.path.join(target, INCOMPLETE_MARKER)
        try:
            _ensure_target_directory(target, os.path.dirname("/" + INCOMPLETE_MARKER),
                                     "kernel registration marker destination")
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("format-1 kernel registration failed: {}\n".format(reason))
        except OSError:
            pass

    def complete(self, root: Optional[str] = None) -> None:
        target = root or self.root
        if not self.applied:
            raise KernelMetadataError("kernel registration transaction is not applied")
        marker = os.path.join(target, INCOMPLETE_MARKER)
        if os.path.exists(marker):
            os.unlink(marker)
        for _path, backup in self._backups:
            if backup is not None:
                try:
                    os.unlink(backup)
                except OSError:
                    pass
        self._backups = []
        shutil.rmtree(self._workspace, ignore_errors=True)

    def close(self) -> None:
        if not self.applied:
            shutil.rmtree(self._workspace, ignore_errors=True)


def prepare_kernel_registration(root: str, allow_foreign_architectures: Optional[Iterable[str]] = None,
                                 verify_payload: bool = True,
                                 external_payload_paths: Optional[Dict[str, str]] = None,
                                 verify_dependencies: bool = True) -> KernelRegistrationPlan:
    if os.path.exists(os.path.join(root, INCOMPLETE_MARKER)):
        _fail("target retains an incomplete kernel-registration marker")
    metadata_dir = os.path.join(root, KERNEL_METADATA_PATH)
    _require_target_directory(root, "etc", "target etc directory")
    _require_target_directory(root, os.path.join("var", "lib", "dpkg"), "target dpkg directory")
    _require_target_directory(root, KERNEL_METADATA_PATH, "kernel metadata directory")
    manifest_path = os.path.join(metadata_dir, "manifest.json")
    referenced_files = {"manifest.json"}
    manifest = _load_json(manifest_path, 4 * 1024 * 1024, "kernel manifest")
    if not isinstance(manifest, dict):
        _fail("kernel manifest must be an object")
    _require_exact_keys(manifest, _TOP_LEVEL_FIELDS, "kernel manifest")
    if type(manifest["format"]) is not int or manifest["format"] != 1:
        _fail("format must be integer 1")
    if manifest["install_policy"] != "register-materialized-payload":
        _fail("install_policy is unsupported")
    if manifest["update_policy"] not in ("track", "frozen"):
        _fail("update_policy is unsupported")

    userspace = manifest["userspace"]
    kernel = manifest["kernel"]
    if not isinstance(userspace, dict) or not isinstance(kernel, dict):
        _fail("userspace and kernel must be objects")
    _require_exact_keys(userspace, _USERSPACE_FIELDS, "userspace")
    _require_exact_keys(kernel, _KERNEL_FIELDS, "kernel")
    for field in _USERSPACE_FIELDS:
        _require_string(userspace[field], "userspace." + field, _IDENTITY_RE)
    for field in ("version", "package_architecture"):
        _require_string(kernel[field], "kernel." + field, _IDENTITY_RE)
    if manifest["update_policy"] == "track":
        _require_string(kernel["distribution"], "kernel.distribution", _IDENTITY_RE)
    else:
        _require_single_line(kernel["distribution"], "kernel.distribution")

    actual_family, actual_suite, native_arch = _target_identity(root)
    expected_identity = (userspace["family"], userspace["suite"], userspace["dpkg_architecture"])
    if (actual_family, actual_suite, native_arch) != expected_identity:
        _fail("userspace identity does not match the selected native root")
    if kernel["package_architecture"] == "all":
        _fail("kernel package architecture cannot be all")
    allowed_foreign = set(allow_foreign_architectures or [])
    if kernel["package_architecture"] != native_arch and kernel["package_architecture"] not in allowed_foreign:
        _fail("mixed-architecture native installation is not verified")

    repositories = manifest["repositories"]
    if not isinstance(repositories, list):
        _fail("repositories must be an array")
    if manifest["update_policy"] == "track" and not repositories:
        _fail("track policy requires at least one repository")
    repository_architectures = set()
    repository_distributions = set()
    for index, repository in enumerate(repositories):
        context = "repository {}".format(index)
        if not isinstance(repository, dict):
            _fail("{} must be an object".format(context))
        _require_exact_keys(repository, _REPOSITORY_FIELDS, context)
        for field in ("family", "suite"):
            _require_string(repository[field], context + "." + field, _IDENTITY_RE)
        if repository["family"] not in ("debian", "devuan", "ubuntu"):
            _fail("{} family is unsupported".format(context))
        repository_distributions.add(repository["suite"])
        uris = _require_string_list(repository["uris"], context + ".uris")
        if any(not uri.startswith("https://") or any(char.isspace() for char in uri) for uri in uris):
            _fail("{} URIs must use HTTPS without whitespace".format(context))
        _require_string_list(repository["components"], context + ".components", _IDENTITY_RE)
        archs = _require_string_list(repository["architectures"], context + ".architectures", _ARCH_RE)
        repository_architectures.update(archs)
        release = repository["release_identity"]
        keyring = repository["keyring"]
        if not isinstance(release, dict) or not isinstance(keyring, dict):
            _fail("{} release identity and keyring must be objects".format(context))
        _require_exact_keys(release, _RELEASE_FIELDS, context + ".release_identity")
        _require_exact_keys(keyring, _KEYRING_FIELDS, context + ".keyring")
        for field in ("origin", "label", "codename"):
            _require_single_line(release[field], context + ".release_identity." + field, _APT_RELEASE_RE)
        repository_distributions.add(release["codename"])
        _require_string(release["inrelease_sha256"], context + ".release_identity.inrelease_sha256", _HASH_RE)
        _require_string(keyring["sha256"], context + ".keyring.sha256", _HASH_RE)
        fingerprints = _require_string_list(keyring["fingerprints"], context + ".keyring.fingerprints", _FINGERPRINT_RE)
        if not fingerprints:
            _fail("{} keyring has no fingerprints".format(context))
        keyring_path = _safe_relative_path(metadata_dir, keyring["path"], context + ".keyring.path")
        if not re.match(r"^keyrings/[A-Za-z0-9][A-Za-z0-9+_.-]*\.gpg$", keyring["path"]):
            _fail("{} keyring path is not canonical".format(context))
        referenced_files.add(keyring["path"])
        if _sha256(_read_bytes(keyring_path, 16 * 1024 * 1024, context + " keyring")) != keyring["sha256"].lower():
            _fail("{} keyring hash mismatch".format(context))
    if repositories:
        if kernel["package_architecture"] not in repository_architectures:
            _fail("repositories do not provide the kernel package architecture")
        if kernel["distribution"] not in repository_distributions:
            _fail("repositories do not match the kernel distribution")

    package_data = manifest["packages"]
    if not isinstance(package_data, list) or not package_data:
        _fail("packages must be a non-empty array")
    status_stanzas = _status_stanzas(os.path.join(root, "var", "lib", "dpkg", "status"))
    installed = {}  # type: Dict[str, List[Dict[str, str]]]
    installed_instances = set()
    for fields, _text in status_stanzas:
        name = fields.get("Package")
        architecture = fields.get("Architecture")
        if name:
            installed.setdefault(name, []).append(fields)
            if architecture:
                instance = _canonical_instance(name, architecture, native_arch)
                if instance in installed_instances:
                    _fail("target status contains duplicate package instance {}".format(instance))
                installed_instances.add(instance)

    packages = []
    instances = set()
    payload_paths = set()
    roles = []
    status_additions = []
    info_files = []  # type: List[Tuple[str, bytes, int]]
    for index, raw in enumerate(package_data):
        context = "package {}".format(index)
        if not isinstance(raw, dict):
            _fail("{} must be an object".format(context))
        registration = raw.get("registration")
        expected_fields = _PACKAGE_SYNTHETIC_FIELDS if registration == "synthetic-installed" else _PACKAGE_BASE_FIELDS
        if registration == "synthetic-installed" and "payload_manifest" in raw:
            expected_fields = expected_fields | {"payload_manifest"}
        _require_exact_keys(raw, expected_fields, context)
        package = dict(raw)
        for field in ("name", "source_package"):
            _require_string(package[field], context + "." + field, _NAME_RE)
        _require_single_line(package["version"], context + ".version")
        _require_string(package["architecture"], context + ".architecture", _ARCH_RE)
        _require_single_line(package["dpkg_instance"], context + ".dpkg_instance")
        _require_string(package["source_archive_sha256"], context + ".source_archive_sha256", _HASH_RE)
        _require_string(package["role"], context + ".role")
        _require_string(package["registration"], context + ".registration")
        _require_string(package["apt_mark"], context + ".apt_mark")
        if package["role"] not in _ROLES:
            _fail("{} has an unknown role".format(context))
        if package["registration"] not in ("synthetic-installed", "verify-installed"):
            _fail("{} has an unknown registration mode".format(context))
        if package["apt_mark"] not in ("manual", "auto", "unchanged") or type(package["hold"]) is not bool:
            _fail("{} has invalid APT mark or hold types".format(context))
        expected_instance = _canonical_instance(package["name"], package["architecture"], native_arch)
        if package["dpkg_instance"] != expected_instance:
            _fail("{} dpkg_instance is not canonical".format(context))
        if package["dpkg_instance"] in instances:
            _fail("duplicate package instance {}".format(package["dpkg_instance"]))
        instances.add(package["dpkg_instance"])
        roles.append(package["role"])
        if package["role"] == "dependency" and package["registration"] != "verify-installed":
            _fail("dependency packages must use verify-installed")
        if package["role"] == "dependency" and package["architecture"] not in ("all", native_arch):
            _fail("dependency packages must be target-native")
        if package["role"] == "dependency" and package["apt_mark"] != "unchanged":
            _fail("dependency package marks must remain unchanged")
        if package["role"] != "dependency" and package["registration"] != "synthetic-installed":
            _fail("kernel package roles must use synthetic-installed")
        if package["role"] != "dependency" and package["architecture"] not in ("all", kernel["package_architecture"]):
            _fail("{} uses an unapproved package architecture".format(context))
        role_prefixes = {
            "tracking-meta": "linux-image-",
            "base-meta": "linux-base-",
            "image": "linux-image-",
            "binary": "linux-binary-",
            "base": "linux-base-",
            "modules": "linux-modules-",
            "modules-extra": "linux-modules-extra-",
        }
        if package["role"] in role_prefixes and not package["name"].startswith(role_prefixes[package["role"]]):
            _fail("{} name is incompatible with its package role".format(context))
        package["native_architecture"] = native_arch

        if package["registration"] == "verify-installed":
            candidates = installed.get(package["name"], [])
            if not any(_is_installed(item) and
                       item.get("Version") == package["version"] and
                       item.get("Architecture") == package["architecture"] for item in candidates):
                _fail("target prerequisite {} is not installed at the exact version".format(package["dpkg_instance"]))
            package["control"] = next(item for item in candidates if _is_installed(item) and
                                       item.get("Version") == package["version"] and
                                       item.get("Architecture") == package["architecture"])
            if _control_source(package["control"]) != package["source_package"]:
                _fail("{} installed source package does not match the manifest".format(context))
            packages.append(package)
            continue

        if package["dpkg_instance"] in installed_instances:
            _fail("target status collides with {}".format(package["dpkg_instance"]))
        expected_status = "status.d/{}.status".format(package["dpkg_instance"])
        expected_prefix = "info/{}.".format(package["dpkg_instance"])
        if package["status"] != expected_status or package["info_prefix"] != expected_prefix:
            _fail("{} status or info prefix is not canonical".format(context))
        status_path = _safe_relative_path(metadata_dir, package["status"], context + ".status")
        referenced_files.add(package["status"])
        fields, stanza = _parse_control(_read_text(status_path, 1024 * 1024, context + " status"), context + " status")
        mandatory = {"Package", "Status", "Version", "Architecture"}
        if not mandatory.issubset(fields):
            _fail("{} status is missing mandatory control fields".format(context))
        if fields["Package"] != package["name"] or fields["Version"] != package["version"] or fields["Architecture"] != package["architecture"]:
            _fail("{} status control fields do not match the manifest".format(context))
        if _control_source(fields) != package["source_package"]:
            _fail("{} status source package does not match the manifest".format(context))
        if fields["Status"] != "install ok installed":
            _fail("{} status is not install ok installed".format(context))
        if fields.get("Essential") == "yes":
            _fail("{} attempts to synthesize an essential package".format(context))
        package["control"] = fields
        if package["hold"]:
            stanza = stanza.replace("Status: install ok installed\n", "Status: hold ok installed\n", 1)
        status_additions.append(stanza.rstrip("\n"))

        info_dir = os.path.join(metadata_dir, "info")
        expected_prefix_name = package["dpkg_instance"] + "."
        names = []
        try:
            directory_names = sorted(os.listdir(info_dir))
        except OSError as exc:
            _fail("package info directory cannot be read: {}".format(exc))
        for name in directory_names:
            if not name.startswith(expected_prefix_name):
                continue
            suffix = name[len(expected_prefix_name):]
            if suffix not in _INFO_SUFFIXES:
                _fail("{} has unsupported dpkg info file {}".format(context, suffix))
            source = _safe_relative_path(metadata_dir, "info/" + name, context + " info file")
            referenced_files.add("info/" + name)
            data = _read_bytes(source, 4 * 1024 * 1024, context + " info file " + suffix)
            destination = os.path.join(root, "var", "lib", "dpkg", "info", name)
            if os.path.lexists(destination):
                _fail("target dpkg info collides with {}".format(name))
            mode = 0o755 if suffix in ("preinst", "postinst", "prerm", "postrm") else 0o644
            info_files.append(("/var/lib/dpkg/info/" + name, data, mode))
            names.append(suffix)
        if "list" not in names or "md5sums" not in names:
            _fail("{} is missing mandatory .list or .md5sums info".format(context))
        entries = _payload_entries(metadata_dir, package, kernel["version"])
        if package.get("payload_manifest"):
            referenced_files.add(package["payload_manifest"])
        package["payload_entries"] = entries
        list_path = os.path.join(metadata_dir, "info", expected_prefix_name + "list")
        list_paths = set(_read_list(list_path, kernel["version"], context + " .list"))
        entry_paths = {item["path"] for item in entries}
        controlled_list_paths = {item for item in list_paths if item.startswith("/boot/") or "/modules/" in item}
        if not entry_paths.issubset(list_paths) or controlled_list_paths != {item for item in entry_paths if item.startswith("/boot/") or "/modules/" in item}:
            _fail("{} payload manifest and .list do not exactly match".format(context))
        non_directory_paths = {item["path"] for item in entries if item["type"] != "directory"}
        overlap = payload_paths.intersection(non_directory_paths)
        if overlap:
            _fail("payload ownership collision at {}".format(sorted(overlap)[0]))
        payload_paths.update(non_directory_paths)
        package["md5sums"] = os.path.join(metadata_dir, "info", expected_prefix_name + "md5sums")
        packages.append(package)

    if manifest["update_policy"] == "track":
        if roles.count("tracking-meta") != 1:
            _fail("track policy requires exactly one tracking-meta package")
        for package in packages:
            if package["role"] == "tracking-meta":
                if package["apt_mark"] != "manual" or package["hold"]:
                    _fail("tracking-meta must be manual and unheld in track mode")
            elif package["role"] in _AUTO_ROLES and (package["apt_mark"] != "auto" or package["hold"]):
                _fail("tracked kernel closure packages must be auto and unheld")
            elif package["role"] == "dependency" and package["apt_mark"] != "unchanged":
                _fail("target-native dependency marks must remain unchanged")
    else:
        if roles.count("tracking-meta"):
            _fail("frozen policy cannot claim a tracking-meta package")
        for package in packages:
            if package["role"] == "image":
                if package["apt_mark"] != "manual" or not package["hold"]:
                    _fail("frozen image must be manual and held")
            elif package["role"] in (_AUTO_ROLES - {"image"}):
                if package["apt_mark"] != "auto" or package["hold"]:
                    _fail("frozen subordinate kernel packages must be auto and unheld")
            elif package["role"] == "dependency" and package["apt_mark"] != "unchanged":
                _fail("target-native dependency marks must remain unchanged")
    if roles.count("image") != 1:
        _fail("package closure must contain exactly one image role")
    split_roles = {"binary", "base", "modules"}
    if {"binary", "base"}.intersection(roles) and not split_roles.issubset(set(roles)):
        _fail("Debian split kernel package closure is incomplete")
    if "modules-extra" in roles and "modules" not in roles:
        _fail("modules-extra requires a modules package")

    owned_payload_paths = {
        entry["path"] for package in packages for entry in package.get("payload_entries", [])
        if entry["type"] != "directory"
    }
    target_info_dir = _target_path(root, "/var/lib/dpkg/info/.keep", "target dpkg info directory")
    target_info_dir = os.path.dirname(target_info_dir)
    if os.path.lexists(target_info_dir):
        info_stat = os.lstat(target_info_dir)
        if stat.S_ISLNK(info_stat.st_mode) or not stat.S_ISDIR(info_stat.st_mode):
            _fail("target dpkg info is not a real directory")
    if os.path.isdir(target_info_dir):
        for name in sorted(os.listdir(target_info_dir)):
            if not name.endswith(".list"):
                continue
            path = os.path.join(target_info_dir, name)
            if not os.path.isfile(path) or os.path.islink(path):
                _fail("target dpkg info contains an unsafe file list")
            for line in _read_text(path, 32 * 1024 * 1024, "target dpkg file list").splitlines():
                normalized = "/" if line == "/." else (line[2:] if line.startswith("/./") else line)
                if normalized in owned_payload_paths:
                    _fail("target package ownership collides at {}".format(normalized))

    actual_metadata_files = set()
    for directory, dirnames, filenames in os.walk(metadata_dir):
        for dirname in list(dirnames):
            path = os.path.join(directory, dirname)
            if os.path.islink(path):
                _fail("kernel metadata contains a symbolic-link directory")
        for filename in filenames:
            path = os.path.join(directory, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                _fail("kernel metadata contains an unsafe file")
            actual_metadata_files.add(os.path.relpath(path, metadata_dir).replace(os.path.sep, "/"))
    if actual_metadata_files != referenced_files:
        extra = sorted(actual_metadata_files - referenced_files)
        missing = sorted(referenced_files - actual_metadata_files)
        _fail("kernel metadata file set is not exact (extra {}; missing {})".format(
            ", ".join(extra) or "none", ", ".join(missing) or "none"
        ))

    target_controls = [fields for fields, _text in status_stanzas]
    if verify_dependencies:
        _verify_dependencies(packages, target_controls)
    _verify_negative_relations(packages, target_controls)
    all_entries = [entry for package in packages for entry in package.get("payload_entries", [])]
    paths = {item["path"] for item in all_entries}
    if "/boot/vmlinuz-{}".format(kernel["version"]) not in paths:
        _fail("payload is missing the manifest-bound kernel image")
    if "/boot/config-{}".format(kernel["version"]) not in paths:
        _fail("payload is missing the manifest-bound kernel config")
    if "/boot/System.map-{}".format(kernel["version"]) not in paths:
        _fail("payload is missing the manifest-bound kernel symbol map")
    module_prefixes = ("/lib/modules/{}/".format(kernel["version"]), "/usr/lib/modules/{}/".format(kernel["version"]))
    if not any(item.startswith(module_prefixes) and entry["type"] == "file" for entry in all_entries for item in [entry["path"]]):
        _fail("payload is missing files from the manifest-bound modules tree")
    _verify_kernel_architecture(root, native_arch, kernel, external_payload_paths)

    if verify_payload:
        for package in packages:
            _verify_payload(root, package.get("payload_entries", []), package["dpkg_instance"],
                            external_payload_paths)
            if package.get("md5sums"):
                regular_paths = {item["path"] for item in package.get("payload_entries", []) if item["type"] == "file"}
                _verify_md5sums(package["md5sums"], root, regular_paths,
                                package["dpkg_instance"] + " md5sums",
                                external_payload_paths)

    status_path = os.path.join(root, "var", "lib", "dpkg", "status")
    original_status = _read_bytes(status_path, 64 * 1024 * 1024, "target dpkg status").decode("utf-8", "strict").rstrip("\n")
    candidate_status = original_status
    if status_additions:
        candidate_status += "\n\n" + "\n\n".join(status_additions)
    candidate_status = (candidate_status.rstrip("\n") + "\n").encode("utf-8")
    candidate_files = [("/var/lib/dpkg/status", candidate_status, 0o644)] + info_files
    extended_path = _optional_target_path(root, "/var/lib/apt/extended_states", "APT extended states")
    candidate_files.append((
        "/var/lib/apt/extended_states",
        _candidate_extended_states(extended_path, packages, native_arch),
        0o644,
    ))
    foreign_archs = sorted({item["architecture"] for item in packages if item["architecture"] not in ("all", native_arch)})
    if foreign_archs:
        arch_path = os.path.join(root, "var", "lib", "dpkg", "arch")
        existing_archs = []
        if os.path.exists(arch_path):
            existing_archs = [line for line in _read_text(arch_path, 128 * 1024, "dpkg architecture list").splitlines() if line]
        for architecture in existing_archs:
            _require_string(architecture, "dpkg architecture list entry", _ARCH_RE)
        if len(existing_archs) != len(set(existing_archs)):
            _fail("dpkg architecture list contains duplicates")
        architecture_data = "\n".join(
            existing_archs + [item for item in [native_arch] + foreign_archs if item not in existing_archs]
        ) + "\n"
        candidate_files.append(("/var/lib/dpkg/arch", architecture_data.encode("ascii"), 0o644))
    candidate_files.extend(_repository_files(root, manifest, packages))

    destinations = [item[0] for item in candidate_files]
    if len(destinations) != len(set(destinations)):
        _fail("candidate package database contains a destination collision")

    # Materialize the complete candidate privately before returning a plan.
    plan = KernelRegistrationPlan(root, manifest, packages, metadata_dir, native_arch, candidate_files)
    for index, (_relative, data, mode) in enumerate(candidate_files):
        candidate = os.path.join(plan._workspace, "candidate-{}".format(index))
        with open(candidate, "wb") as fh:
            fh.write(data)
        os.chmod(candidate, mode)
    return plan


def restore_kernel_dpkg_metadata(target: str, log_cb: Callable[[str], None], dry_run: bool = False) -> int:
    """Strict format-1 compatibility entry point used by focused callers."""
    plan = prepare_kernel_registration(target)
    try:
        if dry_run:
            log_cb("Validated format-1 kernel registration for {} packages.".format(plan.count))
            return plan.count
        count = plan.apply()
        plan.complete()
        log_cb("Registered format-1 kernel metadata for {} packages.".format(count))
        return count
    except Exception as exc:
        if plan.applied:
            plan.rollback(str(exc))
        raise
    finally:
        plan.close()
