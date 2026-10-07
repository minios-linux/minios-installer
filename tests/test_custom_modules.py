"""Use the same module inventory for selection, sizing and target copying."""
import os
from unittest.mock import patch
import pytest
import module_selection as modules
from copy_utils import copy_minios_files, _calculate_copy_size


def media(tmp_path):
    source = tmp_path / 'source'
    for name, data in [('00-core.sb', b'core'), ('01-kernel.sb', b'kernel'),
                       ('05-desktop.sb', b'desktop'),
                       ('modules/10-low.sb', b'low'),
                       ('modules/tools/90-custom.sb', b'custom'),
                       ('boot/vmlinuz', b'boot')]:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
    return source


def test_discovers_custom_images_and_uses_real_sizes(tmp_path, monkeypatch):
    source = media(tmp_path)
    monkeypatch.setattr(modules, 'LIVE_MINIOS_CANDIDATES', (str(source),))
    names = modules.discover_module_names()
    assert names == ['00-core.sb', '01-kernel.sb', '05-desktop.sb',
                     '10-low.sb', '90-custom.sb']
    assert modules.module_size_bytes('90-custom.sb') == 6
    assert modules.payload_overhead_bytes(names) == 4


@pytest.mark.parametrize('include', [False, True])
def test_copy_respects_custom_selection_and_preserves_subdirectory(tmp_path, monkeypatch, include):
    source = media(tmp_path)
    monkeypatch.setattr(modules, 'LIVE_MINIOS_CANDIDATES', (str(source),))
    names = modules.discover_module_names()
    selected = names if include else names[:3]
    assert modules.payload_size_bytes(selected) == _calculate_copy_size(str(source), set(selected))
    with patch('copy_utils._process_grub_config'), patch('copy_utils._process_syslinux_config'):
        copy_minios_files(str(source), str(tmp_path / 'out'), lambda *_: None,
                          lambda *_: None, selected_modules=selected)
    assert (tmp_path / 'out/minios/modules/tools/90-custom.sb').exists() is include
    assert (tmp_path / 'out/minios/modules/10-low.sb').exists() is include


def test_root_layers_precede_custom_with_lower_number(tmp_path):
    source = media(tmp_path)
    (source / 'modules/01-custom.sb').write_bytes(b'custom')
    names = modules.list_live_module_names(str(source))
    assert names.index('05-desktop.sb') < names.index('01-custom.sb')


def test_ambiguous_module_basename_is_rejected(tmp_path):
    source = media(tmp_path)
    (source / 'modules/00-core.sb').write_bytes(b'ambiguous')
    with pytest.raises(ValueError, match='Duplicate module'):
        modules.list_live_module_names(str(source))


def test_normalization_keeps_custom_choices_independent(tmp_path, monkeypatch):
    source = media(tmp_path)
    monkeypatch.setattr(modules, 'LIVE_MINIOS_CANDIDATES', (str(source),))
    names = modules.discover_module_names()
    assert modules.normalize_selected_modules(names, ['90-custom.sb']) == [
        '00-core.sb', '01-kernel.sb', '90-custom.sb']
    assert modules.normalize_selected_modules(names, ['05-desktop.sb', '90-custom.sb']) == [
        '00-core.sb', '01-kernel.sb', '05-desktop.sb', '90-custom.sb']
    assert modules.normalize_selected_modules(names, ['10-low.sb']) == [
        '00-core.sb', '01-kernel.sb', '10-low.sb']


def test_custom_kernel_filename_does_not_extend_required_prefix(tmp_path, monkeypatch):
    source = media(tmp_path)
    (source / '01-kernel.sb').unlink()
    (source / 'modules/99-kernel.sb').write_bytes(b'custom kernel')
    monkeypatch.setattr(modules, 'LIVE_MINIOS_CANDIDATES', (str(source),))
    names = modules.discover_module_names()
    assert modules.required_prefix_count(names) == 1
    assert modules.normalize_selected_modules(names, ['00-core.sb']) == ['00-core.sb']


def test_copy_does_not_restore_unselected_system_or_custom_layers(tmp_path):
    source = media(tmp_path)
    selected = ['00-core.sb', '01-kernel.sb', '90-custom.sb']
    with patch('copy_utils._process_grub_config'), patch('copy_utils._process_syslinux_config'):
        copy_minios_files(str(source), str(tmp_path / 'out'), lambda *_: None,
                          lambda *_: None, selected_modules=selected)
    assert (tmp_path / 'out/minios/modules/tools/90-custom.sb').exists()
    assert not (tmp_path / 'out/minios/modules/10-low.sb').exists()
    assert not (tmp_path / 'out/minios/05-desktop.sb').exists()


def test_native_bundles_keep_independent_custom_selection(tmp_path, monkeypatch):
    from bundle_source import find_bundle_dirs

    source = media(tmp_path)
    monkeypatch.setattr(modules, 'LIVE_MINIOS_CANDIDATES', (str(source),))
    bundles = tmp_path / 'bundles'
    for name in modules.discover_module_names():
        (bundles / name).mkdir(parents=True)
    selected = ['00-core.sb', '01-kernel.sb', '90-custom.sb']
    assert [os.path.basename(path) for path in find_bundle_dirs(str(bundles), selected)] == selected
