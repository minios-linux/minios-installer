"""Test target-session ordering without disk operations."""
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

from install_state import InstallState


@pytest.mark.parametrize("dry_run,creation_error", [(False, False), (True, False), (False, True)])
def test_session_creation_is_between_copy_and_unmount(dry_run, creation_error):
    from live_deploy import run_live_install
    from partition_models import PartitionPlan

    state = InstallState(target_device="/dev/test", persistence_mode="native")
    plan = PartitionPlan(device="/dev/test", use_gpt=False, wipe_disk=True)
    events = []

    def event(name, result=None):
        def invoke(*args, **kwargs):
            events.append(name)
            if name == "create" and creation_error:
                raise RuntimeError("session creation failed")
            return result
        return invoke

    functions = {
        "resolve_install_device": event("resolve", "/dev/test"),
        "find_minios_source": event("source", "/source/minios"),
        "preflight_session_storage": event("preflight", "/usr/bin/minios-session"),
        "scan_disk": event("scan", Mock()),
        "build_plan": event("plan", plan),
        "execute_plan": event("execute", ("/dev/test1", None, "/mnt/target", None)),
        "live_config_for_profile": event("profile", {}),
        "copy_minios_files": event("copy"),
        "create_live_session": event("create"),
        "copy_efi_files": event("efi"),
        "verify_efi_payload": event("verify"),
        "install_bootloader": event("boot"),
        "unmount_partitions": event("unmount"),
    }
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(patch("live_deploy." + name, side_effect=fn))
                 for name, fn in functions.items()}
        if creation_error:
            with pytest.raises(RuntimeError, match="session creation failed"):
                run_live_install(state, Mock(), Mock(), dry_run=dry_run)
        else:
            run_live_install(state, Mock(), Mock(), dry_run=dry_run)
    assert events.index("preflight") < events.index("execute")
    if dry_run:
        assert "copy" not in events and "create" not in events
    else:
        assert events.index("copy") < events.index("create") < events.index("unmount")
        call = mocks["create_live_session"].call_args
        assert call[0][0] is state and call[0][1] == "/mnt/target"
        assert call[1]["command"] == "/usr/bin/minios-session"
        assert "boot_options" not in mocks["copy_minios_files"].call_args[1]
    if creation_error:
        assert "boot" not in events


def test_missing_boot_directory_preserves_initrd_discovery_type(tmp_path):
    from live_deploy import _source_initrd_paths
    assert _source_initrd_paths(str(tmp_path)) == ()
