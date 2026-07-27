import json
import os

from kernel_metadata import restore_kernel_dpkg_metadata


def test_restore_kernel_metadata_appends_status_and_info(tmp_path):
    target = tmp_path
    metadata = target / "usr/share/minios/kernel-dpkg"
    (metadata / "status.d").mkdir(parents=True)
    (metadata / "info").mkdir(parents=True)
    (target / "var/lib/dpkg").mkdir(parents=True)
    (target / "var/lib/dpkg/status").write_text("Package: base-files\nStatus: install ok installed\n", encoding="utf-8")
    (metadata / "manifest.json").write_text(json.dumps({"packages": [{"name": "linux-image-test"}]}), encoding="utf-8")
    (metadata / "status.d/linux-image-test.status").write_text(
        "Package: linux-image-test\nStatus: install ok installed\nVersion: 1\n",
        encoding="utf-8",
    )
    (metadata / "info/linux-image-test.list").write_text("/.\n/boot/vmlinuz-test\n", encoding="utf-8")

    logs = []
    assert restore_kernel_dpkg_metadata(str(target), logs.append) == 1

    status = (target / "var/lib/dpkg/status").read_text(encoding="utf-8")
    assert "Package: linux-image-test" in status
    assert (target / "var/lib/dpkg/info/linux-image-test.list").exists()
    assert any("Restoring kernel package metadata" in line for line in logs)


def test_restore_kernel_metadata_skips_existing_package(tmp_path):
    target = tmp_path
    metadata = target / "usr/share/minios/kernel-dpkg"
    (metadata / "status.d").mkdir(parents=True)
    (target / "var/lib/dpkg").mkdir(parents=True)
    (target / "var/lib/dpkg/status").write_text("Package: linux-image-test\nStatus: install ok installed\n", encoding="utf-8")
    (metadata / "manifest.json").write_text(json.dumps({"packages": [{"name": "linux-image-test"}]}), encoding="utf-8")

    logs = []
    assert restore_kernel_dpkg_metadata(str(target), logs.append) == 0
    assert any("already present" in line for line in logs)


def test_restore_kernel_metadata_warns_on_empty_manifest_with_info(tmp_path):
    target = tmp_path
    metadata = target / "usr/share/minios/kernel-dpkg"
    (metadata / "info").mkdir(parents=True)
    (metadata / "manifest.json").write_text(json.dumps({"packages": []}), encoding="utf-8")
    (metadata / "info/linux-image-test.list").write_text("/.\n", encoding="utf-8")

    logs = []
    assert restore_kernel_dpkg_metadata(str(target), logs.append) == 0
    assert any("incomplete" in line for line in logs)
