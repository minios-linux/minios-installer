"""VMDK client capability checks do not touch installation media."""
import subprocess
from unittest.mock import patch
import pytest
import live_deploy
from install_state import InstallState
from session_storage import preflight_session_storage


@pytest.mark.parametrize('marker,expected', [(b'', False), (b'vmdk-session-v1\n', True), (None, False)])
def test_source_vmdk_requires_new_boot_contract(marker, expected):
    with patch.object(live_deploy, '_source_initrd_paths', return_value=['/source/initrd']), \
         patch.object(live_deploy, '_source_marker_content', return_value=marker):
        assert live_deploy.source_supports_vmdk_persistence('/source') is expected


def test_vmdk_target_rejects_old_session_cli():
    state = InstallState(persistence_mode='vmdk', persistence_size_mib=64)
    old_help = subprocess.CompletedProcess([], 0, b'--activate --compression', b'')
    with patch('session_storage.shutil.which', return_value='/usr/bin/tool'), \
         patch('session_storage.runtime_dynblk_max_size_mib', return_value=1024), \
         patch('session_storage.subprocess.run', return_value=old_help):
        with pytest.raises(RuntimeError, match='Update minios-session'):
            preflight_session_storage(state)


def test_runtime_vmdk_checks_driver_format_support():
    reply = subprocess.CompletedProcess([], 0, b'{"storage_format":"vmdk"}', b'')
    with patch.object(live_deploy, 'runtime_supports_dynblk_persistence', return_value=True), \
         patch.object(live_deploy, '_marker_has_capability', return_value=True), \
         patch.object(live_deploy.subprocess, 'run', return_value=reply) as run:
        assert live_deploy.runtime_supports_vmdk_persistence()
        assert run.call_args[0][0] == ['dynblk', 'limits', '--format', 'vmdk', '--json']
