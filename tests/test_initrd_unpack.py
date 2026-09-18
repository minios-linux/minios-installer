"""Codec detection on Dracut images must not depend on initramfs-tools."""
import subprocess
from unittest.mock import patch

import pytest

from live_deploy import _unpack_source_initrd


@pytest.mark.parametrize('tool', ['unmkinitramfs', 'lsinitrd'])
def test_unpack_uses_available_tool_in_private_directory(tmp_path, tool):
    image = tmp_path / 'initrfs.img'
    image.write_bytes(b'test')
    destination = tmp_path / 'unpacked'
    destination.mkdir()
    executable = '/usr/bin/' + tool
    with patch('live_deploy.shutil.which', side_effect=lambda name:
               executable if name == tool else None), \
         patch('live_deploy.subprocess.run', return_value=
               subprocess.CompletedProcess([], 0)) as run:
        assert _unpack_source_initrd(str(image), str(destination))
    assert destination.is_dir()
    expected = ([executable, str(image), str(destination)] if tool == 'unmkinitramfs'
                else [executable, '--unpack', str(image)])
    assert run.call_args[0][0] == expected
    if tool == 'lsinitrd':
        assert run.call_args[1]['cwd'] == str(destination)
    assert run.call_args[1]['timeout'] == 30


@pytest.mark.parametrize('failure', [
    subprocess.CompletedProcess([], 1),
    OSError('unpacker failed'),
    subprocess.TimeoutExpired('lsinitrd', 30),
])
def test_failed_dracut_unpack_is_not_supported(tmp_path, failure):
    with patch('live_deploy.shutil.which', side_effect=lambda name:
               '/usr/bin/lsinitrd' if name == 'lsinitrd' else None), \
         patch('live_deploy.subprocess.run') as run:
        if isinstance(failure, Exception):
            run.side_effect = failure
        else:
            run.return_value = failure
        assert not _unpack_source_initrd(str(tmp_path / 'bad.img'), str(tmp_path / 'out'))


def test_missing_unpackers_does_not_claim_support(tmp_path):
    with patch('live_deploy.shutil.which', return_value=None), \
         patch('live_deploy.subprocess.run') as run:
        assert not _unpack_source_initrd(str(tmp_path / 'image'), str(tmp_path / 'out'))
    run.assert_not_called()


def test_dracut_receives_resolved_image_path(tmp_path):
    image = tmp_path / 'initrfs-6.12.img'
    image.write_bytes(b'test')
    link = tmp_path / 'initrfs.img'
    link.symlink_to(image.name)
    destination = tmp_path / 'out'
    destination.mkdir()
    with patch('live_deploy.shutil.which', side_effect=lambda name:
               '/usr/bin/lsinitrd' if name == 'lsinitrd' else None), \
         patch('live_deploy.subprocess.run', return_value=
               subprocess.CompletedProcess([], 0)) as run:
        assert _unpack_source_initrd(str(link), str(destination))
    assert run.call_args[0][0] == ['/usr/bin/lsinitrd', '--unpack', str(image)]


def test_runtime_probe_uses_current_kernel_not_shutdown_tree():
    from live_deploy import runtime_dynblk_compression_codecs
    with patch('live_deploy._dynblk_codecs_from_initramfs_tree',
               return_value=('none', 'lz4')) as probe:
        assert runtime_dynblk_compression_codecs() == ('none', 'lz4')
    assert probe.call_args[0] == ('/',)
    assert probe.call_args[1]['kernel']
