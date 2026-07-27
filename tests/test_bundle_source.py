#!/usr/bin/env python3

import os


class TestBundleSource:
    def test_find_bundle_dirs_sorted(self, tmp_path):
        from bundle_source import find_bundle_dirs

        for name in ["04-xfce.sb", "00-core.sb", "notes", "02-fw.sb"]:
            path = tmp_path / name
            path.mkdir()

        result = find_bundle_dirs(str(tmp_path))

        assert [os.path.basename(path) for path in result] == ["00-core.sb", "02-fw.sb", "04-xfce.sb"]

    def test_find_bundle_dirs_filters_selected_prefix(self, tmp_path):
        from bundle_source import find_bundle_dirs

        for name in ["00-core.sb", "01-kernel.sb", "02-fw.sb", "03-gui.sb"]:
            (tmp_path / name).mkdir()

        result = find_bundle_dirs(str(tmp_path), selected_modules=["02-fw.sb"])

        assert [os.path.basename(path) for path in result] == ["00-core.sb", "01-kernel.sb", "02-fw.sb"]

    def test_build_overlay_lowerdir_reverses_priority(self):
        from bundle_source import build_overlay_lowerdir

        assert build_overlay_lowerdir(["/b/00", "/b/01", "/b/05"]) == "/b/05:/b/01:/b/00"

    def test_preflight_rejects_empty_selected_bundle(self, tmp_path):
        from bundle_source import preflight_selected_bundles
        (tmp_path / "00-core.sb").mkdir()
        try:
            preflight_selected_bundles(str(tmp_path))
        except RuntimeError as exc:
            assert "empty" in str(exc).lower()
        else:
            assert False

    def test_materialize_bundles_merges_in_module_order(self, tmp_path):
        from bundle_source import materialize_bundles

        core = tmp_path / "00-core.sb"
        desktop = tmp_path / "04-desktop.sb"
        target = tmp_path / "target"
        (core / "etc").mkdir(parents=True)
        (desktop / "etc").mkdir(parents=True)
        target.mkdir()
        (core / "etc" / "issue").write_text("core\n")
        (desktop / "etc" / "issue").write_text("desktop\n")

        materialize_bundles([str(core), str(desktop)], str(target))

        assert (target / "etc" / "issue").read_text() == "desktop\n"

    def test_materialize_bundles_applies_whiteouts(self, tmp_path):
        from bundle_source import materialize_bundles

        core = tmp_path / "00-core.sb"
        desktop = tmp_path / "04-desktop.sb"
        target = tmp_path / "target"
        (core / "etc").mkdir(parents=True)
        (desktop / "etc").mkdir(parents=True)
        target.mkdir()
        (core / "etc" / "removed.conf").write_text("remove me\n")
        (desktop / "etc" / ".wh.removed.conf").write_text("")

        materialize_bundles([str(core), str(desktop)], str(target))

        assert not (target / "etc" / "removed.conf").exists()

    def test_materialize_bundles_replaces_symlink(self, tmp_path):
        from bundle_source import materialize_bundles

        core = tmp_path / "00-core.sb"
        desktop = tmp_path / "04-desktop.sb"
        target = tmp_path / "target"
        (core / "usr" / "bin").mkdir(parents=True)
        (desktop / "usr" / "bin").mkdir(parents=True)
        target.mkdir()
        (core / "usr" / "bin" / "tool").write_text("binary\n")
        (desktop / "usr" / "bin" / "tool").symlink_to("tool.real")

        materialize_bundles([str(core), str(desktop)], str(target))

        assert os.path.islink(target / "usr" / "bin" / "tool")
        assert os.readlink(target / "usr" / "bin" / "tool") == "tool.real"

    def test_materialize_bundles_copies_dangling_symlink(self, tmp_path):
        from bundle_source import materialize_bundles

        core = tmp_path / "00-core.sb"
        target = tmp_path / "target"
        (core / "etc" / "rc0.d").mkdir(parents=True)
        target.mkdir()
        (core / "etc" / "rc0.d" / "K02eudev").symlink_to("../init.d/eudev")

        materialize_bundles([str(core)], str(target))

        copied = target / "etc" / "rc0.d" / "K02eudev"
        assert os.path.islink(copied)
        assert os.readlink(copied) == "../init.d/eudev"

    def test_materialize_bundles_copies_dangling_directory_symlink(self, tmp_path):
        from bundle_source import materialize_bundles

        core = tmp_path / "00-core.sb"
        target = tmp_path / "target"
        (core / "var").mkdir(parents=True)
        target.mkdir()
        (core / "var" / "lock").symlink_to("../run/lock", target_is_directory=True)

        materialize_bundles([str(core)], str(target))

        copied = target / "var" / "lock"
        assert os.path.islink(copied)
        assert os.readlink(copied) == "../run/lock"
