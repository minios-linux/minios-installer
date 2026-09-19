"""The running root retains modules after LiveKit removes its initrd copy."""
from types import SimpleNamespace
from unittest.mock import patch

import live_deploy


def test_runtime_codecs_use_current_root_not_shutdown_initramfs():
    with patch.object(live_deploy.os, 'uname', return_value=SimpleNamespace(release='test-kernel')), \
         patch.object(live_deploy, '_dynblk_codecs_from_initramfs_tree',
                      return_value=('none', 'lz4', 'zstd')) as probe:
        assert live_deploy.runtime_dynblk_compression_codecs() == ('none', 'lz4', 'zstd')
    probe.assert_called_once_with('/', kernel='test-kernel')


def test_secure_boot_disables_runtime_dynblk_before_module_probe():
    with patch.object(live_deploy, 'secure_boot_enabled', return_value=True), \
         patch.object(live_deploy.subprocess, 'run') as run:
        assert live_deploy.runtime_supports_dynblk_persistence() is False
        assert live_deploy.runtime_supports_vmdk_persistence() is False
    run.assert_not_called()
