#!/usr/bin/env python3

import os


def test_static_ipv4_validation():
    from network_config import validate_static_ipv4

    assert validate_static_ipv4("192.168.1.20", "24", "192.168.1.1", "1.1.1.1, 8.8.8.8") is None
    assert validate_static_ipv4("bad", "24", "", "") == "IPv4 address is not valid."
    assert validate_static_ipv4("192.168.1.20", "33", "", "") == "Network prefix must be between 0 and 32."


def test_network_manager_profile_contains_static_settings():
    from network_config import network_manager_profile

    profile = network_manager_profile("eth0", "192.168.1.20", "24", "192.168.1.1", "1.1.1.1, 8.8.8.8")

    assert "interface-name=eth0" in profile
    assert "autoconnect-priority=100" in profile
    assert "address1=192.168.1.20/24,192.168.1.1" in profile
    assert "dns=1.1.1.1;8.8.8.8;" in profile


def test_network_manager_availability_uses_target_binaries(tmp_path):
    from network_config import network_manager_available

    assert not network_manager_available(str(tmp_path))
    binary = tmp_path / "usr" / "sbin" / "NetworkManager"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    assert network_manager_available(str(tmp_path))


def test_source_supports_selected_live_network_component(tmp_path):
    from network_config import source_supports_live_network

    bundles = tmp_path / "bundles"
    registry = bundles / "02-gui.sb" / "usr/share/minios/capabilities/minios-live-config.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"capabilities":{"live-config.network-method":{}}}')
    modules = ["00-core.sb", "01-kernel.sb", "02-gui.sb"]

    assert source_supports_live_network(str(tmp_path / "source"), modules, modules, str(bundles))
    assert not source_supports_live_network(str(tmp_path / "source"), modules, ["00-core.sb"], str(bundles))


def test_write_network_manager_profile_cleans_legacy_name(tmp_path):
    from network_config import write_network_manager_profile

    directory = tmp_path / "etc/NetworkManager/system-connections"
    directory.mkdir(parents=True)
    (directory / "minios-installer.nmconnection").write_text("legacy")
    path = write_network_manager_profile(str(tmp_path), "eth0", "192.168.1.20", "24", "", "")

    assert path.endswith("minios-static.nmconnection")
    assert not (directory / "minios-installer.nmconnection").exists()
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_network_profile_falls_back_to_ifupdown(tmp_path):
    from network_config import write_network_profile

    ifup = tmp_path / "sbin/ifup"
    ifup.parent.mkdir(parents=True)
    ifup.write_text("")
    path = write_network_profile(str(tmp_path), "eth0", "192.168.1.20", "24", "192.168.1.1", "1.1.1.1")

    assert path.endswith("etc/network/interfaces.d/minios-static")
    result = open(path, encoding="utf-8").read()
    assert "address 192.168.1.20/24" in result
    assert "dns-nameservers 1.1.1.1" in result
