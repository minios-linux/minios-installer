#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gettext
import os
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Iterable, List, Optional

from module_selection import module_basename, normalize_selected_modules


gettext.bindtextdomain("minios-installer", "/usr/share/locale")
gettext.textdomain("minios-installer")
_ = gettext.gettext


DEFAULT_BUNDLES_DIR = "/run/initramfs/memory/bundles"
DEFAULT_CHANGES_DIR = "/run/initramfs/memory/changes"


def find_bundle_dirs(base_dir: str = DEFAULT_BUNDLES_DIR, selected_modules: Optional[Iterable[str]] = None) -> List[str]:
    """Return mounted MiniOS bundle directories sorted in module order."""
    if not os.path.isdir(base_dir):
        raise RuntimeError(_("MiniOS bundle directory not found: {path}").format(path=base_dir))
    bundles = []
    for name in sorted(os.listdir(base_dir)):
        path = os.path.join(base_dir, name)
        if os.path.isdir(path) and name.endswith(".sb"):
            bundles.append(path)
    if not bundles:
        raise RuntimeError(_("No MiniOS bundles found in {path}").format(path=base_dir))
    if selected_modules:
        names = [module_basename(path) for path in bundles]
        selected = set(normalize_selected_modules(names, selected_modules))
        bundles = [path for path in bundles if module_basename(path) in selected]
    return bundles


def preflight_selected_bundles(base_dir: str = DEFAULT_BUNDLES_DIR, selected_modules: Optional[Iterable[str]] = None) -> int:
    """Validate and measure the exact native overlay input before partitioning."""
    total = 0
    for bundle in find_bundle_dirs(base_dir, selected_modules):
        bundle_bytes = 0
        try:
            for root, _dirs, files in os.walk(bundle):
                for name in files:
                    path = os.path.join(root, name)
                    size = os.lstat(path).st_size
                    if size < 0:
                        raise OSError("negative size")
                    bundle_bytes += max(size, 512)
        except OSError as exc:
            raise RuntimeError(_("Selected bundle cannot be measured: {path}").format(path=bundle)) from exc
        if bundle_bytes <= 0:
            raise RuntimeError(_("Selected bundle is empty: {path}").format(path=bundle))
        total += bundle_bytes
    if total <= 0:
        raise RuntimeError(_("Selected native bundle payload is absent or empty."))
    return total


def build_overlay_lowerdir(bundle_dirs: List[str]) -> str:
    """
    Build overlayfs lowerdir with highest-priority bundle first.

    Bundle names are sorted ascending for module order (00, 01, ...). overlayfs
    expects the most specific layer first, so lowerdir is reversed.
    """
    if not bundle_dirs:
        raise RuntimeError(_("No MiniOS bundles specified."))
    return ":".join(reversed(bundle_dirs))


class BundleOverlay:
    def __init__(self, base_dir: str = DEFAULT_BUNDLES_DIR, log_cb: Optional[Callable[[str], None]] = None, selected_modules: Optional[Iterable[str]] = None):
        self.base_dir = base_dir
        self.log_cb = log_cb or (lambda _msg: None)
        self.selected_modules = list(selected_modules or [])
        self.mount_dir: Optional[str] = None
        self.scratch_dir: Optional[str] = None
        self.mounted = False

    def __enter__(self) -> str:
        bundles = find_bundle_dirs(self.base_dir, self.selected_modules)
        lowerdir = build_overlay_lowerdir(bundles)
        scratch_parent = _scratch_parent_for_bundles(self.base_dir)
        self.scratch_dir = tempfile.mkdtemp(prefix="minios-native-overlay-", dir=scratch_parent)
        self.mount_dir = os.path.join(self.scratch_dir, "union")
        upperdir = os.path.join(self.scratch_dir, "upper")
        workdir = os.path.join(self.scratch_dir, "work")
        os.makedirs(self.mount_dir, exist_ok=True)
        os.makedirs(upperdir, exist_ok=True)
        os.makedirs(workdir, exist_ok=True)
        # Empty upperdir/workdir are used only for overlayfs compatibility. They
        # are never populated from /run/initramfs/memory/changes, so the source
        # remains a clean merge of bundles only.
        opts = f"lowerdir={lowerdir},upperdir={upperdir},workdir={workdir}"
        cmd = ["mount", "-t", "overlay", "overlay", "-o", opts, self.mount_dir]
        self.log_cb("$ " + " ".join(cmd))
        try:
            env = os.environ.copy()
            env.setdefault("LIBMOUNT_FORCE_MOUNT2", "always")
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env)
            self.mounted = True
        except (OSError, subprocess.CalledProcessError) as exc:
            self.log_cb(
                _("Overlay merge failed, materializing clean bundle root instead: {error}").format(error=exc)
            )
            shutil.rmtree(self.mount_dir, ignore_errors=True)
            os.makedirs(self.mount_dir, exist_ok=True)
            try:
                materialize_bundles(bundles, self.mount_dir)
            except Exception as copy_exc:
                shutil.rmtree(self.mount_dir, ignore_errors=True)
                self.mount_dir = None
                raise RuntimeError(
                    _("Failed to create clean merged root from MiniOS bundles: {error}").format(error=copy_exc)
                )
        return self.mount_dir

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if not self.mount_dir:
            return
        if self.mounted:
            subprocess.run(["umount", self.mount_dir], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.mount_dir, ignore_errors=True)
        if self.scratch_dir:
            shutil.rmtree(self.scratch_dir, ignore_errors=True)
        self.mount_dir = None
        self.scratch_dir = None
        self.mounted = False


def _scratch_parent_for_bundles(base_dir: str) -> Optional[str]:
    if base_dir == DEFAULT_BUNDLES_DIR:
        parent = os.path.join(DEFAULT_CHANGES_DIR, "tmp")
    elif base_dir.endswith("/bundles"):
        parent = os.path.join(os.path.dirname(base_dir), "changes", "tmp")
    else:
        parent = tempfile.gettempdir()
    try:
        os.makedirs(parent, exist_ok=True)
        return parent
    except OSError:
        return None


def materialize_bundles(bundle_dirs: List[str], target_dir: str) -> None:
    """Create a clean merged root by copying bundle layers in module order."""
    for bundle_dir in bundle_dirs:
        _copy_layer(bundle_dir, target_dir)


def _copy_layer(source_dir: str, target_dir: str) -> None:
    _copy_tree_no_follow(source_dir, target_dir)


def _copy_tree_no_follow(source_dir: str, target_dir: str) -> None:
    os.makedirs(target_dir, exist_ok=True)
    shutil.copystat(source_dir, target_dir, follow_symlinks=False)
    with os.scandir(source_dir) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".wh."):
                _apply_whiteout(target_dir, name)
                continue
            src = entry.path
            dest = os.path.join(target_dir, name)
            if entry.is_symlink():
                _copy_entry(src, dest)
            elif entry.is_dir(follow_symlinks=False):
                _remove_path(dest)
                _copy_tree_no_follow(src, dest)
            else:
                _copy_entry(src, dest)


def _apply_whiteout(dest_root: str, name: str) -> None:
    if name == ".wh..wh..opq":
        for entry in os.listdir(dest_root):
            _remove_path(os.path.join(dest_root, entry))
        return
    target = name[4:]
    if not target or target in (".", "..") or os.path.sep in target:
        return
    _remove_path(os.path.join(dest_root, target))


def _copy_entry(src: str, dest: str) -> None:
    _remove_path(dest)
    src_stat = os.lstat(src)
    if os.path.islink(src):
        os.symlink(os.readlink(src), dest)
        return
    if stat.S_ISDIR(src_stat.st_mode):
        os.makedirs(dest, exist_ok=True)
        shutil.copystat(src, dest, follow_symlinks=False)
        return
    if stat.S_ISCHR(src_stat.st_mode) or stat.S_ISBLK(src_stat.st_mode) or stat.S_ISFIFO(src_stat.st_mode) or stat.S_ISSOCK(src_stat.st_mode):
        os.mknod(dest, src_stat.st_mode, src_stat.st_rdev)
        os.chown(dest, src_stat.st_uid, src_stat.st_gid, follow_symlinks=False)
        os.utime(dest, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns), follow_symlinks=False)
        return
    shutil.copy2(src, dest, follow_symlinks=False)


def _remove_path(path: str) -> None:
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
