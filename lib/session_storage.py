#!/usr/bin/env python3
"""Create target-media sessions through the same backend as Session Manager."""

import gettext
import json
import os
import shutil
import subprocess
from typing import Callable, Optional

from install_state import InstallCanceled, InstallState

_ = gettext.gettext

_SECURE_BOOT_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"


def secure_boot_enabled(efivars_dir: Optional[str] = None) -> bool:
    """Return whether UEFI Secure Boot is enabled for the running system."""
    override = efivars_dir or os.environ.get("MINIOS_EFIVARS_DIR")
    directory = override or "/sys/firmware/efi/efivars"
    variable = os.path.join(directory, "SecureBoot-" + _SECURE_BOOT_GUID)
    try:
        with open(variable, "rb") as stream:
            data = stream.read(5)
        if len(data) >= 5:
            return data[4] == 1
    except OSError:
        pass
    if override:
        return False
    mokutil = shutil.which("mokutil")
    if not mokutil:
        return False
    try:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        result = subprocess.run(
            [mokutil, "--sb-state"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, check=False, env=env)
        text = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        return result.returncode == 0 and "SecureBoot enabled" in text
    except (OSError, subprocess.SubprocessError):
        return False


def session_creation_available() -> bool:
    """Whether the optional session CLI is installed and executable."""
    return shutil.which("minios-session") is not None


def runtime_dynblk_max_size_mib(storage_format='dynblk'):
    """Read the installed backend limit; old CLIs retain their legacy ceiling."""
    try:
        result = subprocess.run(
            ['dynblk', 'limits', '--format', storage_format, '--json'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            check=False)
        if result.returncode == 0:
            value = json.loads(result.stdout)['max_capacity_mib']
            if type(value) is int and 0 < value <= ((1 << 63) - 1) // (1 << 20):
                return value
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        pass
    # Conservative fallback for backends without the limits query.
    return 512 * 1024


def persistence_initial_mib(mode: str, size_mib: int) -> int:
    """Budget initial metadata, not the full payload of a thin container."""
    if mode == "raw":
        return size_mib
    if mode in ("dynblk", "vmdk"):
        # Headroom for metadata of every default 1-GiB logical extent.
        return max(min(size_mib, 100), (size_mib + 1023) // 1024)
    return min(size_mib, 100)


def validate_persistence_password(password: str) -> None:
    """The backend reads a confirmed UTF-8 passphrase from two stdin lines."""
    if not password:
        raise ValueError(_("Enter a password for the encrypted session."))
    if any(char in password for char in ("\n", "\r", "\0")):
        raise ValueError(_("The session password cannot contain NUL or line breaks."))


def preflight_session_storage(state: InstallState,
                              require_password: bool = True) -> Optional[str]:
    """Check creation support before the installer changes the target disk."""
    if state.persistence_mode == "none":
        return None
    if state.persistence_mode in ("dynblk", "vmdk") and secure_boot_enabled():
        raise RuntimeError(_(
            "DynBlk and VMDK session storage are unavailable while Secure Boot is enabled."
        ))
    if state.persistence_mode in ("dynblk", "vmdk") and state.persistence_size_mib > runtime_dynblk_max_size_mib(state.persistence_mode):
        raise RuntimeError(_("DynBlk persistence size exceeds the installed backend limit."))
    if state.persistence_encryption == "luks" and require_password:
        validate_persistence_password(state.persistence_password)
    command = shutil.which("minios-session")
    if not command:
        raise RuntimeError(_("Install minios-session to create session storage."))
    tools = [] if state.persistence_mode == "native" else ["mke2fs"]
    if state.persistence_mode == "dynfilefs":
        tools.append("dynfilefs")
    elif state.persistence_mode in ("dynblk", "vmdk"):
        tools.append("dynblk")
    elif state.persistence_mode == "raw":
        tools.append("fallocate")
    if state.persistence_encryption == "luks":
        tools.append("cryptsetup")
        if state.persistence_mode not in ("dynblk", "vmdk"):
            tools.append("losetup")
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        raise RuntimeError(_("Missing session storage tools: {tools}").format(
            tools=", ".join(missing)))
    try:
        probe = subprocess.run([command, "create", "--help"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(_("Cannot check the session creation backend.")) from exc
    required = [b"--activate", b"--compression"]
    if state.persistence_mode == "vmdk":
        required.append(b"vmdk")
    if state.persistence_encryption == "luks":
        required.append(b"--password-stdin")
    if probe.returncode or any(flag not in probe.stdout for flag in required):
        raise RuntimeError(_("Update minios-session: target session creation is unsupported."))
    return command


def create_live_session(state: InstallState, root_mount: str,
                        progress_cb: Callable, log_cb: Callable,
                        command: Optional[str] = None) -> None:
    """Create and select a real session only inside the mounted target media."""
    if state.persistence_mode == "none":
        return
    if state.cancel_requested:
        raise InstallCanceled(_("Installation canceled by user."))
    if not root_mount or not os.path.isabs(root_mount) or not os.path.ismount(root_mount):
        raise RuntimeError(_("The session target is not a mounted installation partition."))
    root = os.path.realpath(root_mount)
    sessions = os.path.join(root, "minios", "changes")
    # Never follow a copied link to the running medium or another filesystem.
    if os.path.realpath(sessions) != sessions:
        raise RuntimeError(_("The target sessions directory must not contain symbolic links."))
    if not os.path.isdir(sessions):
        raise RuntimeError(_("The target sessions directory is missing."))
    if os.stat(sessions).st_dev != os.stat(root).st_dev:
        raise RuntimeError(_("Session storage must be on the installation partition."))
    command = command or preflight_session_storage(state)
    args = [command, "create", state.persistence_mode]
    if state.persistence_mode in ("raw", "dynfilefs", "dynblk", "vmdk"):
        args.append(str(state.persistence_size_mib))
    args.extend(["--sessions-dir", sessions, "--activate", "--json"])
    if state.persistence_compression != "none":
        args.extend(["--compression", state.persistence_compression])
    password_input = b""
    if state.persistence_encryption == "luks":
        validate_persistence_password(state.persistence_password)
        args.extend(["--encryption", "luks", "--password-stdin"])
        password_input = (state.persistence_password + "\n").encode("utf-8") * 2
    progress_cb(96, _("Creating session storage on the target disk..."))
    # Finish this operation before honoring cancellation: the backend must have
    # time to detach loop/DynBlk/LUKS devices and publish or roll back metadata.
    try:
        result = subprocess.run(args, input=password_input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError(_("Cannot start the session creation backend.")) from exc
    finally:
        password_input = b""
    try:
        reply = json.loads(result.stdout.decode("utf-8"))
        if not isinstance(reply, dict):
            raise ValueError("not an object")
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(_("Invalid response from the session creation backend.")) from exc
    message = str(reply.get("message") or reply.get("error") or "")
    if state.persistence_password:
        message = message.replace(state.persistence_password, "<redacted>")
    if result.returncode or reply.get("success") is not True:
        raise RuntimeError(_("Failed to create target session: {error}").format(error=message))
    log_cb(message or _("Target session created and selected for boot."))
    if state.cancel_requested:
        raise InstallCanceled(_("Installation canceled by user."))
