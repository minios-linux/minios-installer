"""The installer must remain usable without the optional session backend."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import shutil
import subprocess

import pytest

from install_state import InstallState
from main_installer import InstallerWindow
import minios_deploy
from session_storage import session_creation_available

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('command', [None, '/usr/bin/minios-session'])
def test_optional_backend_detection(command):
    with patch('session_storage.shutil.which', return_value=command) as which:
        assert session_creation_available() is (command is not None)
    which.assert_called_once_with('minios-session')


@pytest.fixture
def window():
    state = InstallState(persistence_mode='raw', persistence_encryption='luks',
                         persistence_size_mib=4096, persistence_password='test-only')
    window = SimpleNamespace(
        state=state, _persistence_password_confirm='test-only',
        _persistence_widgets=[Mock() for _ in range(6)],
        _persistence_password_widgets=[Mock() for _ in range(4)],
        _update_required_root_size=Mock(), _update_persistence_validation=Mock(),
        _update_persistence_size_limit=Mock(), persistence_combo=Mock(),
        persistence_size_spin=Mock(), persistence_encryption_combo=Mock(),
        persistence_compression_combo=Mock(),
        persistence_password_entry=Mock(), persistence_password_confirm_entry=Mock())
    window.persistence_size_spin.get_value.return_value = 4096
    window._dynblk_compression_codecs_cache = ('none', 'zstd')
    for name in ('_update_persistence_controls', '_update_persistence_password_controls',
                 '_available_dynblk_compression_codecs'):
        setattr(window, name, getattr(InstallerWindow, name).__get__(window))
    return window


@pytest.mark.parametrize('mode', ['none', 'native', 'raw', 'dynfilefs', 'dynblk'])
def test_missing_backend_hides_and_clears_storage(window, mode):
    window.state.persistence_mode = mode
    window.state.persistence_compression = 'zstd'
    with patch('main_installer.session_creation_available', return_value=False):
        InstallerWindow._refresh_persistence_choices(window)
    assert window.state.persistence_mode == 'none'
    assert window.state.persistence_encryption == 'none'
    assert window.state.persistence_compression == 'none'
    assert window.state.persistence_size_mib == 0
    assert window.state.persistence_password == ''
    assert window._persistence_password_confirm == ''
    window.persistence_combo.set_active_id.assert_called_once_with('none')
    window._update_required_root_size.assert_called_once_with()
    for widget in window._persistence_widgets + window._persistence_password_widgets:
        widget.set_no_show_all.assert_called_with(True)
        widget.hide.assert_called_once_with()
    window.persistence_password_entry.set_text.assert_called_once_with('')
    window.persistence_password_confirm_entry.set_text.assert_called_once_with('')


@pytest.mark.parametrize('available', [False, True])
@pytest.mark.parametrize('command', ['plan', 'install'])
def test_cli_help_hides_unavailable_storage(available, command, capsys):
    parser = minios_deploy.build_parser(
        luks_available=True, dynblk_available=True, session_available=available)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([command, '--help'])
    assert exc.value.code == 0
    assert ('--persistence-mode' in capsys.readouterr().out) is available


@pytest.mark.parametrize('mode', ['none', 'native', 'raw', 'dynfilefs'])
def test_cli_missing_backend_is_required_only_for_creation(mode):
    parser = minios_deploy.build_parser(session_available=False)
    args = parser.parse_args(['plan', '/dev/test', '--persistence-mode', mode])
    with patch('minios_deploy.session_creation_available', return_value=False), \
         patch('minios_deploy.ensure_safe_target_device') as disk:
        if mode == 'none':
            minios_deploy._validate_cli_inputs(args)
        else:
            with pytest.raises(ValueError, match='Install minios-session'):
                minios_deploy.cmd_plan(args)
        disk.assert_not_called()


def test_session_backend_is_only_suggested():
    control = (ROOT / 'debian/control').read_text()
    packages = [part for part in control.split('\n\n') if part.startswith('Package:')]
    deploy = next(part for part in packages if part.startswith('Package: minios-deploy\n'))
    assert 'Suggests: minios-session (>= 2.2.0)' in deploy
    for package in packages:
        for line in package.splitlines():
            if line.startswith(('Depends:', 'Recommends:')):
                assert 'minios-session' not in line


@pytest.mark.parametrize('available', [False, True])
@pytest.mark.parametrize('command', ['plan', 'install'])
def test_completion_respects_optional_backend(tmp_path, available, command):
    if available:
        backend = tmp_path / 'minios-session'
        backend.write_text('#!/bin/sh\nexit 0\n')
        backend.chmod(0o755)
    script = '''source "$1"
PATH="$2"
COMP_WORDS=(minios-deploy "$3" /dev/test --)
COMP_CWORD=3
_minios_deploy
printf '%s\\n' "${COMPREPLY[@]}"
'''
    result = subprocess.run(
        [shutil.which('bash'), '--noprofile', '--norc', '-c', script, 'test',
         str(ROOT / 'completion/minios-deploy'), str(tmp_path), command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0, result.stderr
    options = result.stdout.split()
    assert '--filesystem' in options
    assert ('--persistence-mode' in options) is available
    if command == 'install':
        assert '--boot-menu' in options
        assert ('--persistence-password-stdin' in options) is available


@pytest.mark.parametrize('available,filesystem,expected', [
    (True, 'ext4', 'native'), (True, 'fat32', 'dynfilefs'),
    (False, 'ext4', 'none'), (False, 'fat32', 'none'),
])
def test_native_fallback_requires_backend(window, available, filesystem, expected):
    window.state.persistence_mode = 'native'
    window.state.filesystem = filesystem
    with patch('main_installer.session_creation_available', return_value=available):
        InstallerWindow._refresh_persistence_choices(window)
    assert window.state.persistence_mode == expected
    if available:
        for widget in window._persistence_widgets:
            widget.set_no_show_all.assert_called_with(False)
            widget.show_all.assert_called_once_with()


@pytest.mark.parametrize('mode,encryption,can_encrypt,show_encryption,show_compression', [
    ('none', 'none', False, False, False),
    ('native', 'none', False, False, False),
    ('raw', 'none', True, True, False),
    ('dynfilefs', 'none', True, True, False),
    ('vmdk', 'none', True, True, False),
    ('dynblk', 'none', True, True, True),
    ('dynblk', 'luks', True, True, False),
    ('dynblk', 'none', False, False, True),
])
def test_irrelevant_settings_are_hidden(window, mode, encryption, can_encrypt,
                                       show_encryption, show_compression):
    window.state.persistence_mode = mode
    window.state.persistence_encryption = encryption
    window._persistence_encryption_widgets = [Mock(), Mock()]
    window._persistence_compression_widgets = [Mock(), Mock()]
    with patch('main_installer.session_creation_available', return_value=True), \
         patch('main_installer.runtime_supports_luks_persistence', return_value=can_encrypt):
        window._update_persistence_controls()
    for widget in window._persistence_encryption_widgets:
        widget.set_visible.assert_called_with(show_encryption)
        widget.set_no_show_all.assert_called_with(not show_encryption)
    for widget in window._persistence_compression_widgets:
        widget.set_visible.assert_called_with(show_compression)
        widget.set_no_show_all.assert_called_with(not show_compression)
