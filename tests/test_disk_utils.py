#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for disk_utils module.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock, mock_open

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


class TestFormatSizeToGb:
    """Tests for format_size_to_gb function."""

    def test_gigabytes(self):
        """Test conversion of gigabyte values."""
        from disk_utils import format_size_to_gb
        
        assert format_size_to_gb("10G") == "10.0 GB"
        assert format_size_to_gb("100G") == "100 GB"
        assert format_size_to_gb("1000G") == "1000 GB"
        assert format_size_to_gb("1.5G") == "1.50 GB"

    def test_megabytes(self):
        """Test conversion of megabyte values."""
        from disk_utils import format_size_to_gb
        
        assert format_size_to_gb("500M") == "0.49 GB"
        assert format_size_to_gb("1024M") == "1.00 GB"
        assert format_size_to_gb("2048M") == "2.00 GB"

    def test_terabytes(self):
        """Test conversion of terabyte values."""
        from disk_utils import format_size_to_gb
        
        assert format_size_to_gb("1T") == "1024 GB"
        assert format_size_to_gb("2T") == "2048 GB"

    def test_kilobytes(self):
        """Test conversion of kilobyte values."""
        from disk_utils import format_size_to_gb
        
        result = format_size_to_gb("1048576K")  # 1 GB in KB
        assert "1.00 GB" in result or "1 GB" in result

    def test_bytes(self):
        """Test conversion of byte values."""
        from disk_utils import format_size_to_gb
        
        result = format_size_to_gb("1073741824B")  # 1 GB in bytes
        assert "1.00 GB" in result or "1 GB" in result

    def test_empty_string(self):
        """Test handling of empty string."""
        from disk_utils import format_size_to_gb
        
        assert format_size_to_gb("") == "? GB"

    def test_none_input(self):
        """Test handling of None input."""
        from disk_utils import format_size_to_gb
        
        assert format_size_to_gb(None) == "? GB"

    def test_invalid_input(self):
        """Test handling of invalid input."""
        from disk_utils import format_size_to_gb
        
        assert format_size_to_gb("invalid") == "invalid"

    def test_comma_decimal(self):
        """Test handling of comma as decimal separator."""
        from disk_utils import format_size_to_gb
        
        result = format_size_to_gb("1,5G")
        assert "1.50 GB" in result


class TestIsRemovableDisk:
    """Tests for _is_removable_disk function."""

    def test_removable_disk(self):
        """Test detection of removable disk."""
        from disk_utils import _is_removable_disk
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data='1\n')):
            assert _is_removable_disk('/dev/sdb') is True

    def test_non_removable_disk(self):
        """Test detection of non-removable disk."""
        from disk_utils import _is_removable_disk
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', mock_open(read_data='0\n')):
            assert _is_removable_disk('/dev/sda') is False

    def test_missing_sysfs(self):
        """Test handling of missing sysfs entry."""
        from disk_utils import _is_removable_disk
        
        with patch('os.path.exists', return_value=False):
            assert _is_removable_disk('/dev/sda') is False

    def test_read_error(self):
        """Test handling of read error."""
        from disk_utils import _is_removable_disk
        
        with patch('os.path.exists', return_value=True), \
             patch('builtins.open', side_effect=IOError("Permission denied")):
            assert _is_removable_disk('/dev/sda') is False


class TestGetDiskSizeMib:
    """Tests for get_disk_size_mib function."""

    def test_valid_disk_size(self):
        """Test parsing valid disk size."""
        from disk_utils import get_disk_size_mib
        
        parted_output = '''Model: ATA Samsung SSD 860 (scsi)
Disk /dev/sda: 500107MiB
Sector size (logical/physical): 512B/512B
Partition Table: gpt'''
        
        with patch('disk_utils.run_command', return_value=parted_output):
            size = get_disk_size_mib('/dev/sda')
            assert size == 500107

    def test_fractional_size(self):
        """Test parsing fractional MiB size."""
        from disk_utils import get_disk_size_mib
        
        parted_output = '''Model: USB Drive
Disk /dev/sdb: 30517.5MiB
Sector size: 512B'''
        
        with patch('disk_utils.run_command', return_value=parted_output):
            size = get_disk_size_mib('/dev/sdb')
            assert size == 30517

    def test_unparseable_output(self):
        """Test handling of unparseable output."""
        from disk_utils import get_disk_size_mib
        
        parted_output = '''Model: Unknown
Disk /dev/sdc: unknown'''
        
        with patch('disk_utils.run_command', return_value=parted_output):
            with pytest.raises(RuntimeError, match="Could not parse disk size"):
                get_disk_size_mib('/dev/sdc')


class TestZeroFillDisk:
    """Tests for zero_fill_disk function."""

    def test_successful_zero_fill(self):
        """Test successful disk zero fill."""
        from disk_utils import zero_fill_disk
        
        with patch('subprocess.check_call') as mock_call:
            zero_fill_disk('/dev/sdb')
            mock_call.assert_called_once()
            args = mock_call.call_args[0][0]
            assert 'dd' in args
            assert 'if=/dev/zero' in args
            assert 'of=/dev/sdb' in args

    def test_zero_fill_failure(self):
        """Test handling of zero fill failure."""
        import subprocess
        from disk_utils import zero_fill_disk
        
        with patch('subprocess.check_call', side_effect=subprocess.CalledProcessError(1, 'dd')):
            with pytest.raises(RuntimeError, match="Failed to erase"):
                zero_fill_disk('/dev/sdb')


class TestFindAvailableDisks:
    """Tests for find_available_disks function."""

    def test_basic_disk_detection(self):
        """Test basic disk detection."""
        from disk_utils import find_available_disks
        
        lsblk_output = '''NAME="sda" SIZE="500G" MODEL="Samsung SSD" SERIAL="123" ROTA="0" TRAN="sata"'''
        
        with patch('disk_utils.run_command', return_value=lsblk_output), \
             patch('os.path.exists', return_value=False):  # No live media paths
            disks = find_available_disks()
            assert len(disks) == 1
            assert disks[0]['name'] == 'sda'
            assert 'Samsung SSD' in disks[0]['model']

    def test_multiple_disks(self):
        """Test detection of multiple disks."""
        from disk_utils import find_available_disks
        
        lsblk_output = '''NAME="sda" SIZE="500G" MODEL="SSD" SERIAL="1" ROTA="0" TRAN="sata"
NAME="sdb" SIZE="1T" MODEL="HDD" SERIAL="2" ROTA="1" TRAN="sata"
NAME="sdc" SIZE="32G" MODEL="USB" SERIAL="3" ROTA="0" TRAN="usb"'''
        
        with patch('disk_utils.run_command', return_value=lsblk_output), \
             patch('os.path.exists', return_value=False):
            disks = find_available_disks()
            assert len(disks) == 3

    def test_icon_assignment_usb(self):
        """Test USB disk icon assignment."""
        from disk_utils import find_available_disks
        
        lsblk_output = '''NAME="sdc" SIZE="32G" MODEL="USB" SERIAL="3" ROTA="0" TRAN="usb"'''
        
        with patch('disk_utils.run_command', return_value=lsblk_output), \
             patch('os.path.exists', return_value=False):
            disks = find_available_disks()
            assert disks[0]['icon'] == 'drive-harddisk-usb'

    def test_icon_assignment_nvme(self):
        """Test NVMe disk icon assignment."""
        from disk_utils import find_available_disks
        
        lsblk_output = '''NAME="nvme0n1" SIZE="256G" MODEL="Samsung 970" SERIAL="456" ROTA="0" TRAN="nvme"'''
        
        with patch('disk_utils.run_command', return_value=lsblk_output), \
             patch('os.path.exists', return_value=False):
            disks = find_available_disks()
            assert disks[0]['icon'] == 'drive-harddisk-solidstate'

    def test_icon_assignment_mmc(self):
        """Test MMC/SD card icon assignment."""
        from disk_utils import find_available_disks
        
        lsblk_output = '''NAME="mmcblk0" SIZE="16G" MODEL="" SERIAL="" ROTA="0" TRAN="mmc"'''
        
        with patch('disk_utils.run_command', return_value=lsblk_output), \
             patch('os.path.exists', return_value=False):
            disks = find_available_disks()
            assert disks[0]['icon'] == 'drive-removable-media'

    def test_excludes_loop_devices(self):
        """Test that loop devices are excluded."""
        from disk_utils import find_available_disks
        
        lsblk_output = '''NAME="loop0" SIZE="1G" MODEL="" SERIAL="" ROTA="0" TRAN=""
NAME="sda" SIZE="500G" MODEL="SSD" SERIAL="1" ROTA="0" TRAN="sata"'''
        
        with patch('disk_utils.run_command', return_value=lsblk_output), \
             patch('os.path.exists', return_value=False):
            disks = find_available_disks()
            assert len(disks) == 1
            assert disks[0]['name'] == 'sda'


class TestPartitionDisk:
    """Tests for partition_disk function."""

    def test_fat32_msdos(self):
        """Test FAT32 partition with MSDOS table."""
        from disk_utils import partition_disk
        
        with patch('disk_utils.run_command') as mock_run:
            partition_disk('/dev/sdb', 'fat32', use_gpt=False)
            
            # Check mklabel call
            calls = [str(c) for c in mock_run.call_args_list]
            assert any('msdos' in c for c in calls)
            assert any('mkpart' in c for c in calls)

    def test_ext4_gpt_with_efi(self):
        """Test ext4 partition with GPT and EFI."""
        from disk_utils import partition_disk
        
        call_count = [0]
        def mock_run(cmd, msg):
            call_count[0] += 1
            if 'print' in cmd:
                return 'Disk /dev/sdb: 30000MiB'
            return ''
        
        with patch('disk_utils.run_command', side_effect=mock_run):
            partition_disk('/dev/sdb', 'ext4', use_gpt=True)
            # Should have multiple calls: mklabel, mkpart primary, mkpart ESP, set boot
            assert call_count[0] >= 4

    def test_ext4_msdos_with_efi(self):
        """Test ext4 partition with MSDOS and EFI."""
        from disk_utils import partition_disk
        
        call_count = [0]
        def mock_run(cmd, msg):
            call_count[0] += 1
            if 'print' in cmd:
                return 'Disk /dev/sdb: 30000MiB'
            return ''
        
        with patch('disk_utils.run_command', side_effect=mock_run):
            partition_disk('/dev/sdb', 'ext4', use_gpt=False)
            # Should have multiple calls
            assert call_count[0] >= 4


class TestDiskMonitor:
    """Tests for DiskMonitor class."""

    def test_monitor_creation(self):
        """Test DiskMonitor instantiation."""
        from disk_utils import DiskMonitor
        
        monitor = DiskMonitor()
        assert monitor.udisks_client is None
        assert monitor.listener_id is None
        assert monitor.callbacks == []

    def test_callback_management(self):
        """Test callback registration and removal."""
        from disk_utils import DiskMonitor
        
        monitor = DiskMonitor()
        callback = lambda: None
        
        # Can't actually start monitoring without UDisks, but we can test callback list
        monitor.callbacks.append(callback)
        assert callback in monitor.callbacks
        
        monitor.callbacks.remove(callback)
        assert callback not in monitor.callbacks
