#!/usr/bin/env python3

from unittest.mock import patch


def test_preflight_ok_when_no_missing_packages():
    from package_preflight import preflight_ok

    assert preflight_ok({"missing": [], "apt_available": False, "internet": False, "free_space_mib": 0, "min_space_mib": 512})


def test_preflight_blocks_missing_packages_without_internet():
    from package_preflight import preflight_ok

    result = {"missing": ["grub-pc"], "apt_available": True, "internet": False, "free_space_mib": 4096, "min_space_mib": 512}
    assert preflight_ok(result) is False


def test_preflight_blocks_missing_packages_without_space():
    from package_preflight import preflight_ok

    result = {"missing": ["grub-pc"], "apt_available": True, "internet": True, "free_space_mib": 128, "min_space_mib": 512}
    assert preflight_ok(result) is False


def test_missing_packages_uses_dpkg_status():
    from package_preflight import missing_packages


    def fake_installed(package, root="/"):
        return package == "grub-common"

    with patch("package_preflight.package_installed", side_effect=fake_installed):
        assert missing_packages(["grub-common", "grub-pc"]) == ["grub-pc"]


def test_native_requirements_use_bios_grub_for_mbr():
    from package_preflight import native_package_requirements

    packages = native_package_requirements(False, "ext4")
    assert "grub-pc" in packages
    assert "grub-common" in packages
    assert "grub-efi-amd64" not in packages


def test_native_requirements_use_efi_grub_for_gpt():
    from package_preflight import native_package_requirements

    packages = native_package_requirements(True, "ext4")
    assert "grub-efi-amd64" in packages
    assert "efibootmgr" in packages
    assert "dosfstools" in packages
    assert "grub-pc" not in packages


def test_native_requirements_add_os_prober_only_for_multiboot():
    from package_preflight import native_package_requirements

    assert "os-prober" not in native_package_requirements(False, "ext4", alongside=False)
    assert "os-prober" in native_package_requirements(False, "ext4", alongside=True)


def test_native_standard_bootloader_requirement():
    from package_preflight import native_requires_standard_bootloader

    assert not native_requires_standard_bootloader(False, "erase_all")
    assert native_requires_standard_bootloader(True, "erase_all")
    assert native_requires_standard_bootloader(False, "free_space")
    assert native_requires_standard_bootloader(False, "alongside_os")


def test_native_requirements_add_btrfs_tools():
    from package_preflight import native_package_requirements

    assert "btrfs-progs" in native_package_requirements(False, "btrfs")


def test_native_requirements_do_not_force_display_manager(tmp_path):
    from package_preflight import native_package_requirements

    status = tmp_path / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text(
        "Package: xserver-xorg\nStatus: install ok installed\n\n"
        "Package: grub-common\nStatus: install ok installed\n",
        encoding="utf-8",
    )

    packages = native_package_requirements(False, "ext4", root=str(tmp_path))
    assert "lightdm" not in packages
    assert "lightdm-gtk-greeter" not in packages
    assert "accountsservice" not in packages


def test_package_installed_reads_target_status(tmp_path):
    from package_preflight import package_installed

    status = tmp_path / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text("Package: grub-common\nStatus: install ok installed\n", encoding="utf-8")

    assert package_installed("grub-common", root=str(tmp_path))
    assert not package_installed("grub-pc", root=str(tmp_path))


def test_resize_requirements_map_filesystems_and_partition_tools():
    from package_preflight import resize_package_requirements, resize_required_binaries

    for filesystem in ("ext2", "ext3", "ext4"):
        assert resize_package_requirements(filesystem, False) == ["e2fsprogs"]
        assert resize_required_binaries(filesystem, False) == ["e2fsck", "resize2fs"]
    assert resize_package_requirements("ntfs", False) == ["ntfs-3g"]
    assert resize_required_binaries("ntfs", False) == ["ntfsresize"]
    assert resize_package_requirements("ext4") == ["e2fsprogs", "fdisk", "parted"]
    assert resize_required_binaries("ext4") == ["e2fsck", "resize2fs", "sfdisk", "parted"]


def test_resize_missing_packages_reuses_package_detection():
    from package_preflight import resize_missing_packages

    with patch("package_preflight.package_installed", side_effect=lambda package, root="/": package == "parted"):
        assert resize_missing_packages("ntfs") == ["ntfs-3g", "fdisk"]


def test_resize_preflight_reuses_download_checks():
    from package_preflight import resize_package_preflight

    expected = {"missing": ["e2fsprogs"], "apt_available": True}
    with patch("package_preflight.preflight_package_download", return_value=expected) as preflight:
        assert resize_package_preflight("ext3", False) == expected
        preflight.assert_called_once_with(["e2fsprogs"])


def test_prepare_cache_verifies_dependency_closure_without_download(tmp_path):
    import package_preflight

    calls = []
    with patch("package_preflight.tempfile.mkdtemp", return_value=str(tmp_path / "cache")), \
            patch("package_preflight.os.makedirs"), \
            patch("package_preflight.os.chmod"), \
            patch("package_preflight.shutil.chown"), \
            patch("package_preflight.shutil.copytree"), \
            patch("package_preflight.subprocess.run", side_effect=lambda command, **_kwargs: calls.append(command)):
        package_preflight.prepare_package_cache(["grub-pc"])

    assert any("--no-download" in command and "--download-only" in command for command in calls)
    install_calls = [command for command in calls if "install" in command]
    assert install_calls
    assert all("Dir::State::status=/dev/null" in command for command in install_calls)


def test_package_cache_summary_counts_only_debs(tmp_path):
    from package_preflight import package_cache_summary

    archives = tmp_path / "archives"
    archives.mkdir()
    (archives / "one.deb").write_bytes(b"1" * 1024)
    (archives / "two.deb").write_bytes(b"2" * 2048)
    (archives / "lock").write_bytes(b"")

    assert package_cache_summary(str(tmp_path)) == (2, 3072)
