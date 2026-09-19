"""Regression coverage for target-only creation through the shared CLI."""
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from install_state import InstallCanceled, InstallState
from session_storage import (create_live_session, preflight_session_storage,
                             secure_boot_enabled)


@pytest.fixture
def target(tmp_path):
    (tmp_path / 'minios' / 'changes').mkdir(parents=True)
    return tmp_path


def test_disabled_storage_never_calls_backend():
    with patch('session_storage.subprocess.run') as run:
        assert preflight_session_storage(InstallState()) is None
        create_live_session(InstallState(), None, None, None)
    run.assert_not_called()

@pytest.mark.parametrize('mode,size,compression,encryption', [
    ('native', 0, 'none', 'none'),
    ('raw', 1024, 'none', 'none'),
    ('dynfilefs', 8000, 'none', 'none'),
    ('dynblk', 16384, 'zstd', 'none'),
    ('vmdk', 16384, 'none', 'none'),
    ('vmdk', 16384, 'none', 'luks'),
    ('raw', 2048, 'none', 'luks'),
    ('dynfilefs', 8000, 'none', 'luks'),
    ('dynblk', 16384, 'none', 'luks'),
])
def test_creation_delegates_to_target_scoped_cli(target, mode, size, compression, encryption):
    secret = 'test-only-passphrase' if encryption == 'luks' else ''
    state = InstallState(persistence_mode=mode, persistence_size_mib=size,
                         persistence_compression=compression,
                         persistence_encryption=encryption, persistence_password=secret)
    result = subprocess.CompletedProcess([], 0, b'{"success": true}', b'')
    logs = []
    with patch('session_storage.os.path.ismount', return_value=True), \
         patch('session_storage.subprocess.run', return_value=result) as run:
        create_live_session(state, str(target), lambda *_: None, logs.append,
                            command='/usr/bin/minios-session')
    args = run.call_args[0][0]
    expected = ['/usr/bin/minios-session', 'create', mode]
    if size:
        expected.append(str(size))
    expected += ['--sessions-dir', str(target / 'minios' / 'changes'), '--activate', '--json']
    if compression != 'none':
        expected += ['--compression', compression]
    if encryption == 'luks':
        expected += ['--encryption', 'luks', '--password-stdin']
    assert args == expected
    expected_input = ((secret + '\n') * 2).encode() if secret else b''
    assert run.call_args[1]['input'] == expected_input
    if secret:
        assert secret not in repr(state)
        assert secret not in ' '.join(args)
        assert secret not in ''.join(logs)


def test_unmounted_target_is_rejected(target):
    with patch('session_storage.os.path.ismount', return_value=False), \
         patch('session_storage.subprocess.run') as run:
        with pytest.raises(RuntimeError, match='not a mounted'):
            create_live_session(InstallState(persistence_mode='native'), str(target), None, None)
    run.assert_not_called()


def test_symlink_cannot_redirect_creation_to_source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    target = tmp_path / 'target'
    (target / 'minios').mkdir(parents=True)
    (target / 'minios' / 'changes').symlink_to(source)
    with patch('session_storage.os.path.ismount', return_value=True), \
         patch('session_storage.subprocess.run') as run:
        with pytest.raises(RuntimeError, match='symbolic links'):
            create_live_session(InstallState(persistence_mode='native'), str(target), None, None)
    run.assert_not_called()

@pytest.mark.parametrize('code,payload', [
    (1, b'{"success":false,"message":"allocation failed"}'),
    (0, b'{"success":false,"message":"metadata failed"}'),
    (0, b'not json'),
    (0, b'[]'),
])
def test_backend_failure_never_reports_success(target, code, payload):
    result = subprocess.CompletedProcess([], code, payload, b'')
    logs = []
    with patch('session_storage.os.path.ismount', return_value=True), \
         patch('session_storage.subprocess.run', return_value=result):
        with pytest.raises(RuntimeError):
            create_live_session(InstallState(persistence_mode='native'), str(target),
                                lambda *_: None, logs.append, command='minios-session')
    assert not logs


@pytest.mark.parametrize('password', ['', 'line\nbreak', 'line\rbreak', 'nul\0byte'])
def test_invalid_password_rejected_before_backend_probe(password):
    state = InstallState(persistence_mode='raw', persistence_encryption='luks',
                         persistence_password=password, persistence_size_mib=64)
    with patch('session_storage.subprocess.run') as run:
        with pytest.raises(ValueError):
            preflight_session_storage(state)
    run.assert_not_called()


def test_secure_boot_efivar_rejects_dynblk_before_backend_probe(tmp_path):
    efivars = tmp_path / 'efivars'
    efivars.mkdir()
    variable = efivars / 'SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c'
    variable.write_bytes(b'\x07\x00\x00\x00\x01')
    assert secure_boot_enabled(str(efivars)) is True

    state = InstallState(persistence_mode='dynblk', persistence_size_mib=16384)
    with patch.dict(os.environ, {'MINIOS_EFIVARS_DIR': str(efivars)}), \
         patch('session_storage.subprocess.run') as run:
        with pytest.raises(RuntimeError, match='Secure Boot'):
            preflight_session_storage(state)
        run.assert_not_called()

    variable.write_bytes(b'\x07\x00\x00\x00\x00')
    assert secure_boot_enabled(str(efivars)) is False


def test_secure_boot_rejects_dynblk_before_partitioning():
    from live_deploy import run_live_install
    state = InstallState(target_device='/dev/test', persistence_mode='dynblk',
                         persistence_size_mib=16384)
    with patch('live_deploy.resolve_install_device', return_value='/dev/test'), \
         patch('live_deploy.find_minios_source', return_value='/source'), \
         patch('live_deploy.secure_boot_enabled', return_value=True), \
         patch('live_deploy.execute_plan') as execute:
        with pytest.raises(RuntimeError, match='Secure Boot'):
            run_live_install(state, lambda *_: None, lambda *_: None)
    execute.assert_not_called()


def test_preflight_rejects_old_cli_before_partitioning():
    from live_deploy import run_live_install
    result = subprocess.CompletedProcess([], 0, b'old create help', b'')
    state = InstallState(target_device='/dev/test', persistence_mode='native')
    with patch('live_deploy.resolve_install_device', return_value='/dev/test'), \
         patch('live_deploy.find_minios_source', return_value='/source'), \
         patch('session_storage.shutil.which', return_value='/usr/bin/minios-session'), \
         patch('session_storage.subprocess.run', return_value=result), \
         patch('live_deploy.execute_plan') as execute:
        with pytest.raises(RuntimeError, match='Update minios-session'):
            run_live_install(state, lambda *_: None, lambda *_: None)
    execute.assert_not_called()


def test_cli_password_confirmation_and_dry_run(monkeypatch):
    from minios_deploy import _read_persistence_password
    args = SimpleNamespace(persistence_encryption='luks', dry_run=False,
                           persistence_password_stdin=True)
    stream = SimpleNamespace(buffer=io.BytesIO(b'test-only\ntest-only\n'))
    monkeypatch.setattr(sys, 'stdin', stream)
    assert _read_persistence_password(args) == 'test-only'
    stream.buffer = io.BytesIO(b'first\nsecond\n')
    with pytest.raises(ValueError, match='do not match'):
        _read_persistence_password(args)
    args.dry_run = True
    stream.buffer = io.BytesIO(b'not-consumed\n')
    assert _read_persistence_password(args) == ''
    assert stream.buffer.tell() == 0


def test_cancellation_waits_for_creation_cleanup(target):
    state = InstallState(persistence_mode='native')
    def finish(*args, **kwargs):
        state.cancel_requested = True
        return subprocess.CompletedProcess([], 0, b'{"success":true}', b'')
    with patch('session_storage.os.path.ismount', return_value=True), \
         patch('session_storage.subprocess.run', side_effect=finish) as run:
        with pytest.raises(InstallCanceled):
            create_live_session(state, str(target), lambda *_: None, lambda *_: None,
                                command='minios-session')
    run.assert_called_once()

@pytest.mark.parametrize('dry_run,fail', [(False, False), (False, True), (True, False)])
def test_deploy_creates_after_copy_and_always_unmounts(dry_run, fail):
    from contextlib import ExitStack
    from live_deploy import run_live_install
    from partition_models import PartitionPlan
    state = InstallState(target_device='/dev/test', persistence_mode='native')
    events = []
    def create(*args, **kwargs):
        events.append('create')
        assert args[1] == '/target'
        if fail:
            raise RuntimeError('creation failed')
    with ExitStack() as stack:
        for name, value in (
                ('resolve_install_device', '/dev/test'),
                ('find_minios_source', '/source'),
                ('preflight_session_storage', '/usr/bin/minios-session'),
                ('scan_disk', MagicMock()),
                ('build_plan', PartitionPlan(device='/dev/test', use_gpt=False, wipe_disk=True)),
                ('execute_plan', ('/dev/test1', None, '/target', None)),
                ('copy_efi_files', None), ('verify_efi_payload', None),
                ('install_bootloader', None)):
            stack.enter_context(patch('live_deploy.' + name, return_value=value))
        copy = stack.enter_context(patch('live_deploy.copy_minios_files',
                                         side_effect=lambda *a, **k: events.append('copy')))
        make = stack.enter_context(patch('live_deploy.create_live_session', side_effect=create))
        unmount = stack.enter_context(patch('live_deploy.unmount_partitions'))
        if fail:
            with pytest.raises(RuntimeError, match='creation failed'):
                run_live_install(state, lambda *_: None, lambda *_: None)
        else:
            run_live_install(state, lambda *_: None, lambda *_: None, dry_run=dry_run)
        if dry_run:
            copy.assert_not_called()
            make.assert_not_called()
        else:
            assert events == ['copy', 'create']
            assert 'boot_options' not in copy.call_args[1]
            unmount.assert_called_once_with('/dev/test1', None, '/target', None)
