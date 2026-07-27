#!/usr/bin/env python3

import os


def test_normalize_selected_modules_selects_lower_prefix():
    from module_selection import normalize_selected_modules

    modules = ["00-core.sb", "01-kernel.sb", "02-fw.sb", "03-gui.sb", "04-xfce.sb"]

    assert normalize_selected_modules(modules, ["04-xfce.sb"]) == modules
    assert normalize_selected_modules(modules, ["03-gui.sb"]) == modules[:4]


def test_normalize_selected_modules_keeps_kernel_prefix_required():
    from module_selection import normalize_selected_modules

    modules = ["00-core.sb", "01-kernel.sb", "02-fw.sb"]

    assert normalize_selected_modules(modules, ["00-core.sb"]) == modules[:2]


def test_normalize_selected_modules_rejects_unknown_module():
    from module_selection import normalize_selected_modules

    modules = ["00-core.sb", "01-kernel.sb"]

    try:
        normalize_selected_modules(modules, ["99-missing.sb"])
    except ValueError as exc:
        assert "99-missing.sb" in str(exc)
    else:
        raise AssertionError("unknown modules must be rejected")


def test_list_live_module_names_sorted(tmp_path):
    from module_selection import list_live_module_names

    for name in ["02-fw.sb", "00-core.sb", "notes.txt", "01-kernel.sb"]:
        (tmp_path / name).write_text("x")

    assert list_live_module_names(str(tmp_path)) == ["00-core.sb", "01-kernel.sb", "02-fw.sb"]


def test_module_size_bytes_uses_module_image(tmp_path):
    from module_selection import module_size_bytes

    module = tmp_path / "00-core.sb"
    module.write_bytes(b"x" * 1234)

    assert module_size_bytes("00-core.sb", minios_source=str(tmp_path)) == 1234


def test_module_size_bytes_uses_expanded_bundle_for_native(tmp_path):
    from module_selection import module_size_bytes

    bundle = tmp_path / "00-core.sb"
    bundle.mkdir()
    (bundle / "file").write_bytes(b"x" * 5678)

    assert module_size_bytes("00-core.sb", install_mode="native", bundles_dir=str(tmp_path)) == 5678


def test_selected_module_sizes_drive_required_root_with_proportional_reserve():
    from module_selection import required_root_mib, selected_modules_size_bytes

    mib = 1024 * 1024
    sizes = {"00-core.sb": 2 * mib, "01-kernel.sb": 3 * mib, "02-gui.sb": 7 * mib}
    selected_bytes = selected_modules_size_bytes(["00-core.sb", "01-kernel.sb"], sizes)

    assert selected_bytes == 5 * mib
    assert required_root_mib(selected_bytes) == 7  # 6.25 MiB rounded up


def test_selected_module_size_is_unavailable_when_any_scan_failed():
    from module_selection import selected_modules_size_bytes

    assert selected_modules_size_bytes(["00-core.sb"], {"00-core.sb": None}) is None
