from types import SimpleNamespace
from unittest.mock import patch

from bootloader_utils import install_extlinux_bootloader


def _make_boot_dir(tmp_path):
    boot_dir = tmp_path / "minios" / "boot" / "syslinux"
    boot_dir.mkdir(parents=True)
    exe = boot_dir / "extlinux.x64"
    exe.write_bytes(b"extlinux")
    return boot_dir, exe


def test_extlinux_prefers_target_executable_with_target_cwd(tmp_path):
    boot_dir, exe = _make_boot_dir(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("bootloader_utils.subprocess.check_output", return_value="x86_64\n"), \
         patch("bootloader_utils.subprocess.run", side_effect=fake_run), \
         patch("bootloader_utils._write_mbr"), \
         patch("bootloader_utils._set_active_partition"):
        install_extlinux_bootloader(
            "/dev/sda", "/dev/sda1", None, str(boot_dir), lambda *_: None, lambda *_: None
        )

    assert calls[0][0][0] == str(exe)
    assert calls[0][1]["cwd"] == str(boot_dir)


def test_extlinux_temp_fallback_keeps_target_cwd(tmp_path):
    boot_dir, exe = _make_boot_dir(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == str(exe):
            raise PermissionError("noexec")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("bootloader_utils.subprocess.check_output", return_value="x86_64\n"), \
         patch("bootloader_utils.subprocess.run", side_effect=fake_run), \
         patch("bootloader_utils._write_mbr"), \
         patch("bootloader_utils._set_active_partition"):
        install_extlinux_bootloader(
            "/dev/sda", "/dev/sda1", None, str(boot_dir), lambda *_: None, lambda *_: None
        )

    assert len(calls) == 2
    assert calls[1][0][0] != str(exe)
    assert calls[1][1]["cwd"] == str(boot_dir)
