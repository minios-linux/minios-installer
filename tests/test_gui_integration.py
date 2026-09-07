from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from main_installer import InstallerWindow, TokenCompletionPopover


ROOT = Path(__file__).resolve().parents[1]


def test_split_packages_have_disjoint_payloads_and_exact_backend_dependency():
    backend = set((ROOT / "debian/minios-deploy.install").read_text(
        encoding="utf-8").splitlines())
    frontend = set((ROOT / "debian/minios-installer.install").read_text(
        encoding="utf-8").splitlines())
    provider = set((ROOT / "debian/minios-native-dracut.install").read_text(
        encoding="utf-8").splitlines())
    control = (ROOT / "debian/control").read_text(encoding="utf-8")

    assert backend.isdisjoint(frontend)
    assert backend.isdisjoint(provider)
    assert frontend.isdisjoint(provider)
    assert "usr/bin/minios-deploy" in backend
    assert "usr/bin/minios-installer" in frontend
    assert "etc/kernel/postinst.d/minios-dracut" in provider
    assert "etc/kernel/postrm.d/minios-dracut" in provider
    assert "minios-deploy (= ${binary:Version})" in control
    assert "minios-native-dracut (= ${binary:Version}) | linux-initramfs-tool" in control
    assert "Provides: linux-initramfs-tool" in control
    assert "Depends: ${misc:Depends}, dracut-core" in control
    assert "Breaks: minios-installer (<< 3.1.0)" in control
    assert "Replaces: minios-installer (<< 3.1.0)" in control


def test_content_buttons_use_shared_height_contract():
    source = Path(__file__).resolve().parents[1].joinpath(
        "lib/main_installer.py").read_text(encoding="utf-8")

    assert "button.set_size_request(104, -1)" in source
    assert "detect_location_btn.set_size_request(120, -1)" in source
    assert "reboot_btn.set_size_request(130, -1)" in source
    assert "back_btn.set_size_request(150, -1)" in source
    assert "set_size_request(104, 32)" not in source
    assert "set_size_request(120, 34)" not in source
    assert "set_size_request(130, 34)" not in source
    assert "set_size_request(150, 34)" not in source


class FakeEntry:
    def __init__(self, text, position):
        self.text = text
        self.position = position

    def get_text(self):
        return self.text

    def get_position(self):
        return self.position

    def set_text(self, text):
        self.text = text

    def set_position(self, position):
        self.position = position


def test_completion_uses_shared_comma_token_popover_case_insensitively():
    entry = Mock()

    with patch('main_installer.TokenCompletionPopover') as completion:
        InstallerWindow._attach_completion(
            SimpleNamespace(), entry, ['Europe/Berlin', 'Etc/UTC'])

    completion.assert_called_once()
    args, kwargs = completion.call_args
    assert args == (entry,)
    assert kwargs['delimiters'] == ','
    assert kwargs['min_chars'] == 1
    assert kwargs['provider']('e') == ('Europe/Berlin', 'Etc/UTC')
    assert kwargs['provider']('EUROPE/') == ('Europe/Berlin',)


def test_comma_completion_replaces_only_the_token_at_the_cursor():
    entry = FakeEntry('us, rux, de', len('us, ru'))
    completion = TokenCompletionPopover.__new__(TokenCompletionPopover)
    completion.entry = entry
    completion.delimiters = ','
    completion.append_text = ''
    completion.popover = Mock()

    completion.insert('ru')

    assert entry.text == 'us, ru, de'
    assert entry.position == len('us, ru')
    completion.popover.popdown.assert_called_once_with()


def test_resize_tool_install_uses_shared_confirmation():
    button = Mock()
    window = SimpleNamespace(_resize_missing_packages=['ntfs-3g'])

    with patch('main_installer.ask_confirmation', return_value=False) as confirm, \
            patch('main_installer.subprocess.run') as run:
        InstallerWindow._on_install_resize_tools(window, button)

    confirm.assert_called_once()
    assert confirm.call_args[1]['confirm_label'] == 'Install'
    run.assert_not_called()


def test_reboot_uses_shared_confirmation():
    window = SimpleNamespace()

    with patch('main_installer.ask_confirmation', return_value=False) as confirm, \
            patch('main_installer.subprocess.call') as call, \
            patch('main_installer.subprocess.Popen') as popen:
        InstallerWindow._on_reboot_clicked(window, None)

    confirm.assert_called_once()
    assert confirm.call_args[1]['confirm_label'] == 'Restart'
    call.assert_not_called()
    popen.assert_not_called()


def test_password_entry_uses_shared_hold_widget_and_exposes_gtk_entry():
    password = Mock()
    password.entry = Mock()

    on_changed = Mock()
    with patch('main_installer.PasswordEntry', return_value=password) as factory:
        widget, entry = InstallerWindow._create_password_entry(
            SimpleNamespace(), 'Password', on_changed, initial='secret')

    factory.assert_called_once_with(
        reveal_mode='hold', placeholder_text='Password',
        show_label='Show password', hide_label='Hide password')
    assert widget is password
    assert entry is password.entry
    password.set_text.assert_called_once_with('secret')
    password.reveal_button.connect.assert_not_called()
    password.reveal_button.set_image.assert_not_called()
    password.reveal_button.set_tooltip_text.assert_not_called()
    password.reveal_button.get_accessible.assert_not_called()
    entry.connect.assert_called_once()
    assert entry.connect.call_args[0][0] == 'changed'


def test_module_size_precalculation_uses_owner_safe_background_task():
    captured = {}

    class Task:
        def __init__(self, worker, finished_callback, owner):
            captured.update(worker=worker, finished=finished_callback, owner=owner)

        def start(self):
            value = captured['worker'](None)
            captured['finished'](SimpleNamespace(succeeded=True, value=value))
            return self

    window = SimpleNamespace(
        available_modules=['01-core.sb'],
        _finish_module_size_calculation=Mock(),
    )
    with patch('main_installer.BackgroundTask', Task), \
            patch('main_installer.calculate_module_sizes', return_value={'01-core.sb': 10}), \
            patch('main_installer.payload_overhead_bytes', return_value=2):
        InstallerWindow._start_module_size_calculation(window)

    assert captured['owner'] is window
    window._finish_module_size_calculation.assert_called_once_with(
        {'live': {'01-core.sb': 10}, 'native': {'01-core.sb': 10}},
        {'live': 2, 'native': 2})


def test_location_detection_uses_background_task_outcome():
    captured = {}

    class Task:
        def __init__(self, worker, finished_callback, owner):
            captured.update(worker=worker, finished=finished_callback, owner=owner)

        def start(self):
            value = captured['worker'](None)
            captured['finished'](SimpleNamespace(succeeded=True, value=value))
            return self

    result = {'locale': 'en_US.UTF-8', 'source': 'local'}
    button = Mock()
    window = SimpleNamespace(
        available_locales=['en_US.UTF-8'],
        available_timezones=['Etc/UTC'],
        available_keyboard_layouts=[('us', 'English')],
        _apply_detect_location_result=Mock(),
    )
    with patch('main_installer.BackgroundTask', Task), \
            patch('main_installer.detect_location_best_effort', return_value=result):
        InstallerWindow._on_detect_location(window, button)

    assert captured['owner'] is window
    window._apply_detect_location_result.assert_called_once_with(result, button)


def test_mounted_disk_warning_is_width_bounded_and_wrappable():
    source = (ROOT / "lib/main_installer.py").read_text(encoding="utf-8")
    start = source.index("mount_label = Gtk.Label(xalign=0)")
    end = source.index("texts.pack_start(mount_label, False, False, 0)", start)
    block = source[start:end]

    assert "mount_label.set_line_wrap(True)" in block
    assert "mount_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)" in block
    assert "mount_label.set_max_width_chars(64)" in block
    assert "mount_label.set_hexpand(True)" in block


def test_disk_refresh_shows_only_empty_state_when_no_disks_are_available():
    disk_list = Mock()
    disk_list.get_parent.return_value = object()
    disk_list.get_children.return_value = []
    window = SimpleNamespace(
        state=SimpleNamespace(
            target_device='/dev/sdz', target_device_identity={'path': '/dev/sdz'}),
        disk_list=disk_list,
        disk_rows={'/dev/sdz': Mock()},
        partition_state_stack=Mock(),
        next_button=Mock(),
        _current_step_name=Mock(return_value='partitioning'),
        _update_partition_preview=Mock(),
    )

    with patch('main_installer.find_available_disks', return_value=[]):
        result = InstallerWindow._refresh_disks(window)

    assert result is False
    window.partition_state_stack.set_visible_child_name.assert_called_once_with(
        'no-disks')
    assert window.disk_rows == {}
    assert window.state.target_device is None
    assert window.state.target_device_identity is None
    window.next_button.set_sensitive.assert_called_once_with(False)
