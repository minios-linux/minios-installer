import hashlib
import json
import os
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest

from kernel_metadata import (
    INCOMPLETE_MARKER,
    KernelMetadataError,
    prepare_kernel_registration,
    restore_kernel_dpkg_metadata,
)


VERSION = "6.1.0-test-amd64"


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _md5(data):
    return hashlib.md5(data).hexdigest()


def write_format1_fixture(target, userspace_arch="amd64", kernel_arch="amd64",
                          flavour=None, kernel_version=VERSION):
    metadata = target / "usr/share/minios/kernel-dpkg"
    for directory in ("status.d", "info", "payload.d", "keyrings"):
        (metadata / directory).mkdir(parents=True, exist_ok=True)
    (target / "etc").mkdir(exist_ok=True)
    (target / "etc/os-release").write_text("ID=debian\nVERSION_CODENAME=trixie\n", encoding="utf-8")
    (target / "var/lib/dpkg").mkdir(parents=True, exist_ok=True)
    (target / "var/lib/dpkg/status").write_text(
        "Package: base-files\nStatus: install ok installed\nVersion: 1\n"
        "Architecture: {}\nEssential: yes\n\n"
        "Package: kmod\nStatus: install ok installed\nVersion: 30+test\nArchitecture: {}\n".format(
            userspace_arch, userspace_arch
        ),
        encoding="utf-8",
    )

    payload = {
        "/boot/vmlinuz-" + kernel_version: b"kernel\n",
        "/boot/config-" + kernel_version: (
            b"CONFIG_TEST=y\nCONFIG_EFI_STUB=y\nCONFIG_BINFMT_ELF=y\n"
            b"CONFIG_IA32_EMULATION=y\nCONFIG_EFI_MIXED=y\n"
        ),
        "/boot/System.map-" + kernel_version: b"symbols\n",
        "/lib/modules/" + kernel_version + "/kernel/test.ko": b"module\n",
    }
    for path, data in payload.items():
        disk_path = target / path.lstrip("/")
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(data)

    tracking_name = "linux-image-" + (flavour or kernel_arch)
    image_name = "linux-image-" + kernel_version
    tracking_instance = tracking_name if userspace_arch == kernel_arch else tracking_name + ":" + kernel_arch
    image_instance = image_name if userspace_arch == kernel_arch else image_name + ":" + kernel_arch
    keyring = b"test archive keyring\n"
    (metadata / "keyrings/debian-trixie.gpg").write_bytes(keyring)

    packages = []
    for role, name, instance, mark, depends in (
        ("tracking-meta", tracking_name, tracking_instance, "manual", image_name + " (= 1)"),
        ("image", image_name, image_instance, "auto", ""),
    ):
        entry = {
            "role": role,
            "name": name,
            "version": "1",
            "architecture": kernel_arch,
            "dpkg_instance": instance,
            "source_package": "linux-signed-test",
            "source_archive_sha256": "1" * 64,
            "registration": "synthetic-installed",
            "status": "status.d/{}.status".format(instance),
            "info_prefix": "info/{}.".format(instance),
            "apt_mark": mark,
            "hold": False,
        }
        control = (
            "Package: {}\nStatus: install ok installed\nVersion: 1\nArchitecture: {}\n"
            "Source: linux-signed-test\n".format(
                name, kernel_arch
            )
        )
        if depends:
            control += "Depends: {}\n".format(depends)
        (metadata / entry["status"]).write_text(control, encoding="utf-8")
        if role == "image":
            entries = []
            for path, data in sorted(payload.items()):
                entries.append({"path": path, "type": "file", "sha256": _hash(data)})
            entry["payload_manifest"] = "payload.d/{}.json".format(instance)
            (metadata / entry["payload_manifest"]).write_text(
                json.dumps({"format": 1, "dpkg_instance": instance, "files": entries}),
                encoding="utf-8",
            )
            paths = sorted(payload)
            (metadata / "info" / (instance + ".list")).write_text(
                "\n".join(paths) + "\n", encoding="utf-8"
            )
            (metadata / "info" / (instance + ".md5sums")).write_text(
                "".join("{}  {}\n".format(_md5(payload[path]), path.lstrip("/")) for path in paths),
                encoding="utf-8",
            )
        else:
            (metadata / "info" / (instance + ".list")).write_text("/.\n", encoding="utf-8")
            (metadata / "info" / (instance + ".md5sums")).write_text("", encoding="utf-8")
        packages.append(entry)

    manifest = {
        "format": 1,
        "install_policy": "register-materialized-payload",
        "update_policy": "track",
        "userspace": {
            "family": "debian",
            "suite": "trixie",
            "dpkg_architecture": userspace_arch,
        },
        "kernel": {
            "distribution": "trixie",
            "version": kernel_version,
            "package_architecture": kernel_arch,
        },
        "repositories": [{
            "family": "debian",
            "uris": ["https://deb.debian.org/debian"],
            "suite": "trixie",
            "components": ["main"],
            "architectures": [kernel_arch],
            "release_identity": {
                "origin": "Debian",
                "label": "Debian",
                "codename": "trixie",
                "inrelease_sha256": "2" * 64,
            },
            "keyring": {
                "path": "keyrings/debian-trixie.gpg",
                "sha256": _hash(keyring),
                "fingerprints": ["B" * 40],
            },
        }],
        "packages": packages,
    }
    (metadata / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_format1_rejects_symlink_target_outside_kernel_payload(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    metadata = tmp_path / "usr/share/minios/kernel-dpkg"
    image = next(package for package in manifest["packages"]
                 if package["role"] == "image")
    payload_path = metadata / image["payload_manifest"]
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    unsafe_path = "/lib/modules/{}/leak".format(VERSION)
    payload["files"].append({
        "path": unsafe_path,
        "type": "symlink",
        "target": "../../../../etc/shadow",
    })
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    list_path = metadata / (image["info_prefix"] + "list")
    with list_path.open("a", encoding="utf-8") as stream:
        stream.write(unsafe_path + "\n")

    with pytest.raises(KernelMetadataError, match="escapes the kernel payload"):
        prepare_kernel_registration(str(tmp_path))


def test_format1_registration_uses_existing_sources_and_publishes_pins_and_marks(tmp_path):
    write_format1_fixture(tmp_path)
    sources = tmp_path / "etc/apt/sources.list"
    sources.parent.mkdir(parents=True, exist_ok=True)
    sources.write_text("deb https://deb.debian.org/debian trixie main\n", encoding="utf-8")
    plan = prepare_kernel_registration(str(tmp_path))
    assert plan.apply() == 2
    plan.complete()

    status = (tmp_path / "var/lib/dpkg/status").read_text(encoding="utf-8")
    assert "Package: linux-image-amd64" in status
    assert "Package: linux-image-{}".format(VERSION) in status
    assert (tmp_path / "boot" / ("config-" + VERSION)).is_file()
    assert (tmp_path / "boot" / ("System.map-" + VERSION)).is_file()
    assert (tmp_path / "var/lib/dpkg/info/linux-image-amd64.list").exists()
    extended = (tmp_path / "var/lib/apt/extended_states").read_text(encoding="utf-8")
    assert "Package: linux-image-{}".format(VERSION) in extended
    assert "Package: linux-image-amd64" not in extended
    assert sources.read_text(encoding="utf-8") == "deb https://deb.debian.org/debian trixie main\n"
    assert not (tmp_path / "etc/apt/sources.list.d/minios-kernel.sources").exists()
    assert not (tmp_path / "usr/share/keyrings").exists()
    pin = (tmp_path / "etc/apt/preferences.d/minios-kernel").read_text(encoding="utf-8")
    assert "Package: *" not in pin
    assert "Package: linux-image-*-amd64:amd64\nPin: release o=Debian,l=Debian,n=trixie\nPin-Priority: 990" in pin
    assert "Package: linux-image-*-amd64:amd64\nPin: version *\nPin-Priority: -1" in pin
    assert "Package: linux-image-*-amd64:amd64" in pin
    assert "Package: linux-base-*-amd64:amd64" in pin
    assert "Package: linux-binary-*-amd64:amd64" in pin
    assert "Package: linux-modules-*-amd64:amd64" in pin
    assert "Package: linux-modules-extra-*-amd64:amd64" in pin
    assert "Package: linux-image-*:amd64\n" not in pin
    assert "Package: linux-modules-*:amd64\n" not in pin
    assert "Pin-Priority: 990" in pin
    assert not (tmp_path / INCOMPLETE_MARKER).exists()


def test_track_pins_allow_every_manifest_repository_before_denying_other_sources(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    base = manifest["repositories"][0]
    updates = json.loads(json.dumps(base))
    updates["suite"] = "trixie-updates"
    updates["release_identity"]["codename"] = "trixie-updates"
    security = json.loads(json.dumps(base))
    security["uris"] = ["https://security.debian.org/debian-security"]
    security["suite"] = "trixie-security"
    security["release_identity"]["label"] = "Debian-Security"
    security["release_identity"]["codename"] = "trixie-security"
    manifest["repositories"] = [base, updates, security]
    metadata = tmp_path / "usr/share/minios/kernel-dpkg"
    (metadata / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = prepare_kernel_registration(str(tmp_path))
    plan.apply()
    plan.complete()

    preferences = (tmp_path / "etc/apt/preferences.d/minios-kernel").read_text(
        encoding="utf-8"
    )
    for pattern in ("linux-image-amd64:amd64", "linux-image-*-amd64:amd64"):
        stanzas = [stanza for stanza in preferences.strip().split("\n\n")
                   if stanza.startswith("Package: {}\n".format(pattern))]
        assert stanzas == [
            "Package: {}\nPin: release o=Debian,l=Debian,n=trixie\n"
            "Pin-Priority: 990".format(pattern),
            "Package: {}\nPin: release o=Debian,l=Debian,n=trixie-updates\n"
            "Pin-Priority: 990".format(pattern),
            "Package: {}\nPin: release o=Debian,l=Debian-Security,n=trixie-security\n"
            "Pin-Priority: 990".format(pattern),
            "Package: {}\nPin: version *\nPin-Priority: -1".format(pattern),
        ]


def _write_local_apt_repository(root, suite, origin, label, codename, version):
    packages = (
        "Package: linux-image-amd64\n"
        "Version: {}\n"
        "Architecture: amd64\n"
        "Maintainer: MiniOS Test <test@example.invalid>\n"
        "Description: local policy fixture\n"
        "Filename: pool/linux-image-amd64_{}_amd64.deb\n"
        "Size: 1\n"
        "SHA256: {}\n\n"
    ).format(version, version, "0" * 64).encode("utf-8")
    relative = "main/binary-amd64/Packages"
    package_path = root / "dists" / suite / relative
    package_path.parent.mkdir(parents=True)
    package_path.write_bytes(packages)
    release = (
        "Origin: {}\n"
        "Label: {}\n"
        "Suite: {}\n"
        "Codename: {}\n"
        "Architectures: amd64\n"
        "Components: main\n"
        "SHA256:\n"
        " {} {} {}\n"
    ).format(origin, label, suite, codename, _hash(packages), len(packages), relative)
    (root / "dists" / suite / "Release").write_text(release, encoding="utf-8")


@pytest.mark.skipif(not shutil.which("apt-get") or not shutil.which("apt-cache"),
                    reason="APT tools are unavailable")
def test_generated_policy_selects_foreign_arch_security_from_local_repositories(tmp_path):
    target = tmp_path / "target"
    manifest = write_format1_fixture(
        target, userspace_arch="i386", kernel_arch="amd64")
    base = manifest["repositories"][0]
    security = json.loads(json.dumps(base))
    security["uris"] = ["https://security.example.invalid/debian-security"]
    security["suite"] = "trixie-security"
    security["release_identity"]["label"] = "Debian-Security"
    security["release_identity"]["codename"] = "trixie-security"
    manifest["repositories"] = [base, security]
    metadata = target / "usr/share/minios/kernel-dpkg"
    (metadata / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    plan = prepare_kernel_registration(
        str(target), allow_foreign_architectures={"amd64"})
    plan.apply()
    plan.complete()
    assert not (target / "etc/apt/sources.list.d/minios-kernel.sources").exists()
    assert not (target / "usr/share/keyrings").exists()

    repositories = tmp_path / "repositories"
    base_repo = repositories / "base"
    security_repo = repositories / "security"
    outsider_repo = repositories / "outsider"
    _write_local_apt_repository(
        base_repo, "trixie", "Debian", "Debian", "trixie", "1")
    _write_local_apt_repository(
        security_repo, "trixie-security", "Debian", "Debian-Security",
        "trixie-security", "2")
    _write_local_apt_repository(
        outsider_repo, "other", "Other", "Other", "other", "3")

    apt_root = tmp_path / "apt"
    lists = apt_root / "state/lists"
    archives = apt_root / "cache/archives"
    source_parts = apt_root / "etc/sources.list.d"
    preference_parts = apt_root / "etc/preferences.d"
    for directory in (lists / "partial", archives / "partial", source_parts,
                      preference_parts):
        directory.mkdir(parents=True, exist_ok=True)
    sources = apt_root / "etc/sources.list"
    sources.write_text(
        "deb [trusted=yes arch=amd64] file:{} trixie main\n"
        "deb [trusted=yes arch=amd64] file:{} trixie-security main\n"
        "deb [trusted=yes arch=amd64] file:{} other main\n".format(
            base_repo, security_repo, outsider_repo),
        encoding="utf-8",
    )
    status = apt_root / "state/status"
    status.write_text(
        "Package: base-files\nStatus: install ok installed\nVersion: 1\n"
        "Architecture: i386\nEssential: yes\n\n"
        "Package: linux-image-amd64\nStatus: install ok installed\nVersion: 2\n"
        "Architecture: amd64\n",
        encoding="utf-8",
    )
    options = [
        "-o", "Dir::Etc::sourcelist={}".format(sources),
        "-o", "Dir::Etc::sourceparts={}".format(source_parts),
        "-o", "Dir::Etc::preferences={}".format(
            target / "etc/apt/preferences.d/minios-kernel"),
        "-o", "Dir::Etc::preferencesparts={}".format(preference_parts),
        "-o", "Dir::State={}".format(apt_root / "state"),
        "-o", "Dir::State::status={}".format(status),
        "-o", "Dir::Cache={}".format(apt_root / "cache"),
        "-o", "Dir::Cache::archives={}".format(archives),
        "-o", "APT::Architecture=i386",
        "-o", "APT::Architectures::=i386",
        "-o", "APT::Architectures::=amd64",
        "-o", "Acquire::Languages=none",
        "-o", "Acquire::AllowInsecureRepositories=true",
        "-o", "Debug::NoLocking=1",
        "-o", "APT::Sandbox::User=root",
    ]
    environment = dict(os.environ, LC_ALL="C", LANG="C")
    subprocess.run(
        ["apt-get"] + options + ["update"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, env=environment)
    result = subprocess.run(
        ["apt-cache"] + options + ["policy", "linux-image-amd64:amd64"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, env=environment)

    assert "Installed: 2" in result.stdout
    assert "Candidate: 2" in result.stdout
    assert re.search(r"^\s+3\s+-1$", result.stdout, re.MULTILINE)
    assert re.search(r"^\s+\*\*\*\s+2\s+990$", result.stdout, re.MULTILINE)
    assert re.search(r"^\s+1\s+990$", result.stdout, re.MULTILINE)


@pytest.mark.parametrize(
    "flavour,kernel_version,kernel_architecture",
    [
        ("amd64", "6.12.1-test-amd64", "amd64"),
        ("generic", "6.8.0-1-generic", "amd64"),
        ("686", "6.1.0-1-686", "i386"),
        ("686-pae", "6.1.0-1-686-pae", "i386"),
        ("rt-amd64", "6.12.1-1-rt-amd64", "amd64"),
        ("cloud-amd64", "6.12.1-1-cloud-amd64", "amd64"),
    ],
)
def test_track_patterns_are_architecture_qualified_and_flavour_bounded(
        tmp_path, flavour, kernel_version, kernel_architecture):
    write_format1_fixture(
        tmp_path, userspace_arch=kernel_architecture,
        kernel_arch=kernel_architecture, flavour=flavour,
        kernel_version=kernel_version)

    plan = prepare_kernel_registration(str(tmp_path))
    plan.apply()
    plan.complete()

    preferences = (tmp_path / "etc/apt/preferences.d/minios-kernel").read_text(
        encoding="utf-8"
    )
    assert "Package: linux-image-*-{}:{}\n".format(
        flavour, kernel_architecture) in preferences
    assert "Package: linux-modules-*-{}:{}\n".format(
        flavour, kernel_architecture) in preferences
    assert "Package: linux-image-{}:{}\n".format(
        kernel_version, kernel_architecture) in preferences
    assert "Package: linux-image-*:{}\n".format(kernel_architecture) not in preferences
    assert "Package: linux-modules-*:{}\n".format(kernel_architecture) not in preferences


def test_track_package_names_must_preserve_the_tracking_flavour(tmp_path):
    write_format1_fixture(
        tmp_path, flavour="generic", kernel_version="6.12.1-test-amd64")

    with pytest.raises(KernelMetadataError, match="preserve tracking kernel flavour generic"):
        prepare_kernel_registration(str(tmp_path))


def test_restore_entry_point_is_strict_and_supports_dry_run(tmp_path):
    write_format1_fixture(tmp_path)
    original = (tmp_path / "var/lib/dpkg/status").read_bytes()
    assert restore_kernel_dpkg_metadata(str(tmp_path), lambda _line: None, dry_run=True) == 2
    assert (tmp_path / "var/lib/dpkg/status").read_bytes() == original


def test_target_identity_accepts_confined_usrmerged_os_release(tmp_path):
    write_format1_fixture(tmp_path)
    (tmp_path / "usr/lib").mkdir(parents=True, exist_ok=True)
    (tmp_path / "usr/lib/os-release").write_text(
        "ID=debian\nVERSION_CODENAME=trixie\n", encoding="utf-8"
    )
    (tmp_path / "etc/os-release").unlink()
    (tmp_path / "etc/os-release").symlink_to("../usr/lib/os-release")

    assert restore_kernel_dpkg_metadata(
        str(tmp_path), lambda _line: None, dry_run=True
    ) == 2


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda manifest: manifest.pop("repositories"), "missing repositories"),
        (lambda manifest: manifest.update({"format": "1"}), "format must be integer 1"),
        (lambda manifest: manifest["packages"][0].update({"role": "guessed"}), "unknown role"),
        (lambda manifest: manifest["packages"][0].update({"dpkg_instance": "wrong"}), "not canonical"),
        (lambda manifest: manifest["kernel"].update({"version": "other"}), "escapes the kernel payload boundary"),
    ],
)
def test_schema_and_identity_fail_closed(tmp_path, mutation, match):
    manifest = write_format1_fixture(tmp_path)
    mutation(manifest)
    (tmp_path / "usr/share/minios/kernel-dpkg/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(KernelMetadataError, match=match):
        prepare_kernel_registration(str(tmp_path))
    assert "linux-image-amd64" not in (tmp_path / "var/lib/dpkg/status").read_text(encoding="utf-8")


def test_exact_json_rejects_duplicate_members(tmp_path):
    write_format1_fixture(tmp_path)
    path = tmp_path / "usr/share/minios/kernel-dpkg/manifest.json"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('{"format": 1,', '{"format": 1, "format": 1,', 1), encoding="utf-8")
    with pytest.raises(KernelMetadataError, match="duplicate JSON member"):
        prepare_kernel_registration(str(tmp_path))


def test_exact_control_rejects_duplicate_fields(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    status = tmp_path / "usr/share/minios/kernel-dpkg" / manifest["packages"][1]["status"]
    status.write_text(status.read_text(encoding="utf-8") + "Version: 1\n", encoding="utf-8")
    with pytest.raises(KernelMetadataError, match="duplicate field Version"):
        prepare_kernel_registration(str(tmp_path))


def test_control_parser_accepts_empty_field_with_continuations(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    status = tmp_path / "usr/share/minios/kernel-dpkg" / manifest["packages"][1]["status"]
    status.write_text(
        status.read_text(encoding="utf-8") + "Conffiles:\n /etc/kernel-test abcdef\n",
        encoding="utf-8",
    )

    assert restore_kernel_dpkg_metadata(
        str(tmp_path), lambda _line: None, dry_run=True
    ) == 2


def test_dependencies_are_satisfied_by_target_native_packages(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    status = tmp_path / "usr/share/minios/kernel-dpkg" / manifest["packages"][1]["status"]
    status.write_text(
        status.read_text(encoding="utf-8") + "Depends: kmod (>= 1)\n",
        encoding="utf-8",
    )

    assert restore_kernel_dpkg_metadata(
        str(tmp_path), lambda _line: None, dry_run=True
    ) == 2


def test_preflight_can_defer_dependencies_until_target_packages_are_staged(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    status = tmp_path / "usr/share/minios/kernel-dpkg" / manifest["packages"][1]["status"]
    status.write_text(
        status.read_text(encoding="utf-8") + "Depends: initramfs-tools\n",
        encoding="utf-8",
    )

    with pytest.raises(KernelMetadataError, match="unsatisfied Depends initramfs-tools"):
        prepare_kernel_registration(str(tmp_path))

    plan = prepare_kernel_registration(str(tmp_path), verify_dependencies=False)
    plan.close()


def test_payload_hash_and_list_are_final_and_exact(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    (tmp_path / "boot" / ("vmlinuz-" + VERSION)).write_bytes(b"corrupt")
    with pytest.raises(KernelMetadataError, match="wrong SHA-256"):
        prepare_kernel_registration(str(tmp_path))

    write_format1_fixture(tmp_path)
    instance = manifest["packages"][1]["dpkg_instance"]
    list_path = tmp_path / "usr/share/minios/kernel-dpkg/info" / (instance + ".list")
    list_path.write_text(list_path.read_text(encoding="utf-8") + "/boot/unlisted\n", encoding="utf-8")
    with pytest.raises(KernelMetadataError, match="do not exactly match"):
        prepare_kernel_registration(str(tmp_path))


def test_keyring_hash_and_safe_relative_paths_are_mandatory(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    manifest["repositories"][0]["keyring"]["sha256"] = "0" * 64
    path = tmp_path / "usr/share/minios/kernel-dpkg/manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(KernelMetadataError, match="keyring hash mismatch"):
        prepare_kernel_registration(str(tmp_path))

    manifest = write_format1_fixture(tmp_path)
    manifest["repositories"][0]["keyring"]["path"] = "../outside.gpg"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(KernelMetadataError, match="normalized relative path"):
        prepare_kernel_registration(str(tmp_path))


def test_control_source_and_conflicting_specific_pin_fail_closed(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    status_path = tmp_path / "usr/share/minios/kernel-dpkg" / manifest["packages"][1]["status"]
    status_path.write_text(
        status_path.read_text(encoding="utf-8").replace("Source: linux-signed-test", "Source: other"),
        encoding="utf-8",
    )
    with pytest.raises(KernelMetadataError, match="source package does not match"):
        prepare_kernel_registration(str(tmp_path))

    write_format1_fixture(tmp_path)
    preferences = tmp_path / "etc/apt/preferences.d/vendor"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(
        "Package: linux-image-*-amd64\nPin: release n=other\nPin-Priority: 1002\n",
        encoding="utf-8",
    )
    with pytest.raises(KernelMetadataError, match="override the narrow kernel source policy"):
        prepare_kernel_registration(str(tmp_path))


@pytest.mark.parametrize(
    "record",
    [
        "Package: linux-image-amd64\nPin: release n=other\nPin-Priority: 500\n",
        "Package: linux-image-*\nPin: release n=other\nPin-Priority: 999\n",
        "Package: /^linux-(image|modules)-.*$/\nPin: release n=other\nPin-Priority: 100\n",
        "Package: linux-image-*:any\nPin: release n=other\nPin-Priority: -10\n",
        "Package: src:linux-signed-*\nPin: release n=other\nPin-Priority: 1\n",
        "Package: *\nPin: version 6.*\nPin-Priority: 500\n",
        "Package: *\nPin: release n=trixie*\nPin-Priority: 500\n",
    ],
)
def test_any_preceding_specific_pin_that_may_overlap_kernel_policy_is_rejected(
        tmp_path, record):
    write_format1_fixture(tmp_path)
    preferences = tmp_path / "etc/apt/preferences"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(record, encoding="utf-8")

    with pytest.raises(KernelMetadataError, match="override the narrow kernel source policy"):
        prepare_kernel_registration(str(tmp_path))


@pytest.mark.parametrize(
    "record",
    [
        "Package: firefox*\nPin: release n=other\nPin-Priority: 1001\n",
        "Package: /^firefox.*$/\nPin: release n=other\nPin-Priority: 500\n",
        "Package: linux-image-*-generic\nPin: release n=other\nPin-Priority: 500\n",
        "Package: linux-image-*:i386\nPin: release n=other\nPin-Priority: 500\n",
        "Package: *\nPin: release l=MiniOS Repository\nPin-Priority: 1001\n",
    ],
)
def test_nonoverlapping_or_generic_existing_pins_remain_allowed(tmp_path, record):
    write_format1_fixture(tmp_path)
    preferences = tmp_path / "etc/apt/preferences"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(record, encoding="utf-8")

    plan = prepare_kernel_registration(str(tmp_path))
    plan.close()


def test_global_vendor_pin_is_overridden_by_specific_kernel_policy(tmp_path):
    write_format1_fixture(tmp_path)
    preferences = tmp_path / "etc/apt/preferences.d/minios-linux"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(
        "Package: *\nPin: release l=MiniOS Repository\nPin-Priority: 1001\n",
        encoding="utf-8",
    )

    plan = prepare_kernel_registration(str(tmp_path))
    plan.close()


def test_unreferenced_archive_and_generated_lifecycle_ownership_are_rejected(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    metadata = tmp_path / "usr/share/minios/kernel-dpkg"
    (metadata / "kernel.deb").write_bytes(b"archive")
    with pytest.raises(KernelMetadataError, match="file set is not exact"):
        prepare_kernel_registration(str(tmp_path))

    (metadata / "kernel.deb").unlink()
    instance = manifest["packages"][1]["dpkg_instance"]
    payload_path = metadata / manifest["packages"][1]["payload_manifest"]
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["files"].append({
        "path": "/lib/modules/{}/modules.dep".format(VERSION),
        "type": "file",
        "sha256": "0" * 64,
    })
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(KernelMetadataError, match="generated lifecycle output"):
        prepare_kernel_registration(str(tmp_path))


def test_unverified_mixed_architecture_is_rejected_without_mutation(tmp_path):
    write_format1_fixture(tmp_path, userspace_arch="i386", kernel_arch="amd64")
    with pytest.raises(KernelMetadataError, match="not verified"):
        prepare_kernel_registration(str(tmp_path))
    assert not (tmp_path / "var/lib/dpkg/arch").exists()


def test_approved_foreign_architecture_is_staged_and_rolled_back(tmp_path):
    write_format1_fixture(tmp_path, userspace_arch="i386", kernel_arch="amd64")
    plan = prepare_kernel_registration(str(tmp_path), allow_foreign_architectures={"amd64"})
    plan.apply()
    assert (tmp_path / "var/lib/dpkg/arch").read_text(encoding="utf-8") == "i386\namd64\n"
    preferences = (tmp_path / "etc/apt/preferences.d/minios-kernel").read_text(
        encoding="utf-8"
    )
    assert "Package: linux-image-amd64:amd64\n" in preferences
    assert "Package: linux-image-*-amd64:amd64\n" in preferences
    assert "Package: linux-image-amd64\n" not in preferences
    assert not (tmp_path / "etc/apt/sources.list.d/minios-kernel.sources").exists()
    assert not (tmp_path / "usr/share/keyrings").exists()
    plan.rollback("fault")
    assert not (tmp_path / "var/lib/dpkg/arch").exists()
    assert "fault" in (tmp_path / INCOMPLETE_MARKER).read_text(encoding="utf-8")
    plan.close()


def test_publication_failure_restores_overwritten_and_created_files_and_keeps_marker(tmp_path):
    write_format1_fixture(tmp_path)
    extended = tmp_path / "var/lib/apt/extended_states"
    extended.parent.mkdir(parents=True)
    extended.write_text("Package: old\nArchitecture: amd64\nAuto-Installed: 1\n", encoding="utf-8")
    source = tmp_path / "etc/apt/sources.list.d/minios-kernel.sources"
    pin = tmp_path / "etc/apt/preferences.d/minios-kernel"
    source.parent.mkdir(parents=True, exist_ok=True)
    pin.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("old source\n", encoding="utf-8")
    pin.write_text("old pin\n", encoding="utf-8")
    original_status = (tmp_path / "var/lib/dpkg/status").read_bytes()
    original_extended = extended.read_bytes()
    plan = prepare_kernel_registration(str(tmp_path))
    real_replace = os.replace

    def fail_pin(source, destination):
        if destination.endswith("/etc/apt/preferences.d/minios-kernel"):
            raise OSError("injected publication failure")
        return real_replace(source, destination)

    with patch("kernel_metadata.os.replace", side_effect=fail_pin):
        with pytest.raises(OSError, match="injected publication failure"):
            plan.apply()
    assert (tmp_path / "var/lib/dpkg/status").read_bytes() == original_status
    assert extended.read_bytes() == original_extended
    assert source.read_text(encoding="utf-8") == "old source\n"
    assert pin.read_text(encoding="utf-8") == "old pin\n"
    assert not (tmp_path / "var/lib/dpkg/info/linux-image-amd64.list").exists()
    assert (tmp_path / INCOMPLETE_MARKER).exists()
    plan.close()


def test_frozen_registration_uses_declared_hold_without_repository_files(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    metadata = tmp_path / "usr/share/minios/kernel-dpkg"
    tracking = manifest["packages"].pop(0)
    (metadata / tracking["status"]).unlink()
    (metadata / "info" / (tracking["dpkg_instance"] + ".list")).unlink()
    (metadata / "info" / (tracking["dpkg_instance"] + ".md5sums")).unlink()
    (metadata / manifest["repositories"][0]["keyring"]["path"]).unlink()
    manifest["repositories"] = []
    manifest["update_policy"] = "frozen"
    manifest["packages"][0]["apt_mark"] = "manual"
    manifest["packages"][0]["hold"] = True
    (metadata / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    plan = prepare_kernel_registration(str(tmp_path))
    plan.apply()
    plan.complete()

    status = (tmp_path / "var/lib/dpkg/status").read_text(encoding="utf-8")
    assert "Status: hold ok installed" in status
    assert not (tmp_path / "etc/apt/sources.list.d/minios-kernel.sources").exists()
    assert not (tmp_path / "etc/apt/preferences.d/minios-kernel").exists()


def test_frozen_registration_requires_manual_held_image(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    metadata = tmp_path / "usr/share/minios/kernel-dpkg"
    tracking = manifest["packages"].pop(0)
    (metadata / tracking["status"]).unlink()
    (metadata / "info" / (tracking["dpkg_instance"] + ".list")).unlink()
    (metadata / "info" / (tracking["dpkg_instance"] + ".md5sums")).unlink()
    (metadata / manifest["repositories"][0]["keyring"]["path"]).unlink()
    manifest["repositories"] = []
    manifest["update_policy"] = "frozen"
    (metadata / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(KernelMetadataError, match="manual and held"):
        prepare_kernel_registration(str(tmp_path))
    assert not (tmp_path / "usr/share/keyrings").exists()


def test_payload_parent_symlink_cannot_escape_target(tmp_path):
    write_format1_fixture(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    shutil.copytree(str(tmp_path / "boot"), str(outside))
    shutil.rmtree(str(tmp_path / "boot"))
    (tmp_path / "boot").symlink_to(outside, target_is_directory=True)

    with pytest.raises(KernelMetadataError, match="outside the target root"):
        prepare_kernel_registration(str(tmp_path))


def test_publication_rejects_destination_directory_symlink_and_rolls_back(tmp_path):
    write_format1_fixture(tmp_path)
    original_status = (tmp_path / "var/lib/dpkg/status").read_bytes()
    plan = prepare_kernel_registration(str(tmp_path))
    outside = tmp_path.parent / (tmp_path.name + "-apt-outside")
    outside.mkdir()
    (tmp_path / "var/lib/apt").symlink_to(outside, target_is_directory=True)

    with pytest.raises(KernelMetadataError, match="symbolic link"):
        plan.apply()

    assert (tmp_path / "var/lib/dpkg/status").read_bytes() == original_status
    assert not (outside / "extended_states").exists()
    assert (tmp_path / INCOMPLETE_MARKER).exists()
    plan.close()


def test_kernel_roles_cannot_use_the_userspace_architecture_in_mixed_mode(tmp_path):
    manifest = write_format1_fixture(tmp_path, userspace_arch="i386", kernel_arch="amd64")
    package = manifest["packages"][0]
    package["architecture"] = "i386"
    package["dpkg_instance"] = package["name"]
    path = tmp_path / "usr/share/minios/kernel-dpkg/manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(KernelMetadataError, match="unapproved package architecture"):
        prepare_kernel_registration(str(tmp_path), allow_foreign_architectures={"amd64"})


def test_dependency_marks_cannot_mutate_target_native_package_state(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    manifest["packages"].append({
        "role": "dependency",
        "name": "kmod",
        "version": "30+test",
        "architecture": "amd64",
        "dpkg_instance": "kmod",
        "source_package": "kmod",
        "source_archive_sha256": "3" * 64,
        "registration": "verify-installed",
        "apt_mark": "auto",
        "hold": False,
    })
    path = tmp_path / "usr/share/minios/kernel-dpkg/manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(KernelMetadataError, match="marks must remain unchanged"):
        prepare_kernel_registration(str(tmp_path))


def test_native_extended_state_without_architecture_is_replaced_not_duplicated(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    image = manifest["packages"][1]
    extended = tmp_path / "var/lib/apt/extended_states"
    extended.parent.mkdir(parents=True)
    extended.write_text(
        "Package: {}\nAuto-Installed: 1\n".format(image["name"]), encoding="utf-8"
    )

    plan = prepare_kernel_registration(str(tmp_path))
    plan.apply()
    plan.complete()

    content = extended.read_text(encoding="utf-8")
    assert content.count("Package: {}\n".format(image["name"])) == 1
    assert "Architecture: amd64" in content


def test_target_native_dependencies_and_held_prerequisites_are_valid(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    metadata = tmp_path / "usr/share/minios/kernel-dpkg"
    image = manifest["packages"][1]
    image_status = metadata / image["status"]
    image_status.write_text(
        image_status.read_text(encoding="utf-8") + "Depends: kmod (= 30+test)\n",
        encoding="utf-8",
    )

    plan = prepare_kernel_registration(str(tmp_path))
    plan.close()
    status = tmp_path / "var/lib/dpkg/status"
    status.write_text(
        status.read_text(encoding="utf-8").replace(
            "Package: kmod\nStatus: install ok installed",
            "Package: kmod\nStatus: hold ok installed",
        ),
        encoding="utf-8",
    )
    plan = prepare_kernel_registration(str(tmp_path))
    plan.close()


def test_external_boot_payload_is_validated_without_duplication(tmp_path):
    write_format1_fixture(tmp_path)
    external = tmp_path / "iso-boot"
    external.mkdir()
    paths = {}
    for name in ("vmlinuz-" + VERSION,):
        source = tmp_path / "boot" / name
        target = external / name
        source.rename(target)
        paths["/boot/" + name] = str(target)

    plan = prepare_kernel_registration(
        str(tmp_path), external_payload_paths=paths
    )
    plan.close()

    (external / ("vmlinuz-" + VERSION)).write_bytes(b"tampered\n")
    with pytest.raises(KernelMetadataError, match="wrong SHA-256"):
        prepare_kernel_registration(str(tmp_path), external_payload_paths=paths)


def test_conflicting_target_package_is_rejected_before_registration(tmp_path):
    manifest = write_format1_fixture(tmp_path)
    image = manifest["packages"][1]
    status = tmp_path / "usr/share/minios/kernel-dpkg" / image["status"]
    status.write_text(
        status.read_text(encoding="utf-8") + "Conflicts: base-files (>= 1)\n",
        encoding="utf-8",
    )

    with pytest.raises(KernelMetadataError, match="conflicts with installed package"):
        prepare_kernel_registration(str(tmp_path))
