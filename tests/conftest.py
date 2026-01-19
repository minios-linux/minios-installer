#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pytest fixtures for minios-installer tests.
"""

import sys
import os
import pytest
from unittest.mock import MagicMock, patch

# Add lib directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


@pytest.fixture
def mock_subprocess():
    """Mock subprocess module."""
    with patch('subprocess.check_output') as mock_check_output, \
         patch('subprocess.check_call') as mock_check_call, \
         patch('subprocess.call') as mock_call, \
         patch('subprocess.run') as mock_run:
        yield {
            'check_output': mock_check_output,
            'check_call': mock_check_call,
            'call': mock_call,
            'run': mock_run
        }


@pytest.fixture
def mock_os_path():
    """Mock os.path functions."""
    with patch('os.path.exists') as mock_exists, \
         patch('os.path.isdir') as mock_isdir, \
         patch('os.path.ismount') as mock_ismount, \
         patch('os.path.basename') as mock_basename:
        mock_basename.side_effect = os.path.basename  # Keep real behavior by default
        yield {
            'exists': mock_exists,
            'isdir': mock_isdir,
            'ismount': mock_ismount,
            'basename': mock_basename
        }


@pytest.fixture
def mock_shutil():
    """Mock shutil module."""
    with patch('shutil.which') as mock_which, \
         patch('shutil.rmtree') as mock_rmtree:
        yield {
            'which': mock_which,
            'rmtree': mock_rmtree
        }


@pytest.fixture
def sample_lsblk_output():
    """Sample lsblk output for testing disk detection."""
    return '''NAME="sda" SIZE="500G" MODEL="Samsung SSD 860" SERIAL="S3Z2NB0K123456" ROTA="0" TRAN="sata"
NAME="sdb" SIZE="1T" MODEL="WD Blue" SERIAL="WD-WMATV1234567" ROTA="1" TRAN="sata"
NAME="sdc" SIZE="32G" MODEL="USB Flash" SERIAL="12345678" ROTA="0" TRAN="usb"
NAME="nvme0n1" SIZE="256G" MODEL="Samsung 970 EVO" SERIAL="S5HCNF0N123456" ROTA="0" TRAN="nvme"
NAME="mmcblk0" SIZE="16G" MODEL="" SERIAL="" ROTA="0" TRAN="mmc"'''


@pytest.fixture
def sample_parted_output():
    """Sample parted output for testing disk size parsing."""
    return '''Model: ATA Samsung SSD 860 (scsi)
Disk /dev/sda: 500107MiB
Sector size (logical/physical): 512B/512B
Partition Table: gpt'''


@pytest.fixture
def sample_proc_mounts():
    """Sample /proc/mounts content."""
    return '''/dev/sda1 /mnt/data ext4 rw,relatime 0 0
/dev/sda2 /boot/efi vfat rw,relatime,fmask=0077,dmask=0077 0 0
/dev/sdb1 /mnt/backup ext4 rw,relatime 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev 0 0'''
