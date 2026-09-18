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
