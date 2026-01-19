#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for format_utils module.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


class TestDetectFilesystemTools:
    """Tests for detect_filesystem_tools function."""

    def test_all_tools_available(self):
        """Test when all filesystem tools are available."""
        from format_utils import detect_filesystem_tools
        
        with patch('shutil.which', return_value='/usr/sbin/mkfs.ext4'):
            fss = detect_filesystem_tools()
            assert 'ext4' in fss
            assert 'ext2' in fss
            assert 'btrfs' in fss
            assert 'fat32' in fss
            assert 'ntfs' in fss

    def test_no_tools_available(self):
        """Test when no filesystem tools are available."""
        from format_utils import detect_filesystem_tools
        
        with patch('shutil.which', return_value=None):
            fss = detect_filesystem_tools()
            assert fss == []

    def test_partial_tools_available(self):
        """Test when some filesystem tools are available."""
        from format_utils import detect_filesystem_tools
        
        def which_side_effect(cmd):
            if cmd in ('mkfs.ext4', 'mkfs.vfat'):
                return f'/usr/sbin/{cmd}'
            return None
        
        with patch('shutil.which', side_effect=which_side_effect):
            fss = detect_filesystem_tools()
            assert 'ext4' in fss
            assert 'fat32' in fss
            assert 'btrfs' not in fss
            assert 'ntfs' not in fss


class TestFormatPartitions:
    """Tests for format_partitions function."""

    def test_format_fat32(self):
        """Test formatting FAT32 partition."""
        from format_utils import format_partitions
        
        with patch('format_utils.run_command') as mock_run:
            format_partitions('/dev/sdb1', 'fat32', None)
            
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert 'mkfs.vfat' in args
            assert '/dev/sdb1' in args

    def test_format_ext4(self):
        """Test formatting ext4 partition."""
        from format_utils import format_partitions
        
        with patch('format_utils.run_command') as mock_run:
            format_partitions('/dev/sdb1', 'ext4', None)
            
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert 'mkfs.ext4' in args
            assert '-F' in args

    def test_format_btrfs(self):
        """Test formatting btrfs partition."""
        from format_utils import format_partitions
        
        with patch('format_utils.run_command') as mock_run:
            format_partitions('/dev/sdb1', 'btrfs', None)
            
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert 'mkfs.btrfs' in args
            assert '-f' in args

    def test_format_ntfs(self):
        """Test formatting NTFS partition."""
        from format_utils import format_partitions
        
        with patch('format_utils.run_command') as mock_run:
            format_partitions('/dev/sdb1', 'ntfs', None)
            
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert 'mkfs.ntfs' in args
            assert '-f' in args

    def test_format_with_efi_partition(self):
        """Test formatting with EFI partition."""
        from format_utils import format_partitions
        
        call_count = [0]
        calls = []
        def mock_run(cmd, msg):
            call_count[0] += 1
            calls.append(cmd)
            return ''
        
        with patch('format_utils.run_command', side_effect=mock_run):
            format_partitions('/dev/sdb1', 'ext4', '/dev/sdb2')
            
            assert call_count[0] == 2
            # First call formats primary partition
            assert 'mkfs.ext4' in calls[0]
            # Second call formats EFI partition
            assert 'mkfs.vfat' in calls[1]
            assert '/dev/sdb2' in calls[1]


class TestCheckFilesystemSupport:
    """Tests for check_filesystem_support function."""

    def test_all_supported(self):
        """Test when all filesystems are supported."""
        from format_utils import check_filesystem_support
        
        with patch('shutil.which', return_value='/usr/sbin/mkfs'):
            support = check_filesystem_support()
            
            assert support['ext4'] is True
            assert support['ext2'] is True
            assert support['fat32'] is True
            assert support['btrfs'] is True
            assert support['ntfs'] is True

    def test_none_supported(self):
        """Test when no filesystems are supported."""
        from format_utils import check_filesystem_support
        
        with patch('shutil.which', return_value=None):
            support = check_filesystem_support()
            
            assert support['ext4'] is False
            assert support['ext2'] is False
            assert support['fat32'] is False
            assert support['btrfs'] is False
            assert support['ntfs'] is False

    def test_partial_support(self):
        """Test with partial filesystem support."""
        from format_utils import check_filesystem_support
        
        def which_side_effect(cmd):
            supported = {'mkfs.ext4', 'mkfs.vfat'}
            return f'/usr/sbin/{cmd}' if cmd in supported else None
        
        with patch('shutil.which', side_effect=which_side_effect):
            support = check_filesystem_support()
            
            assert support['ext4'] is True
            assert support['fat32'] is True
            assert support['btrfs'] is False
            assert support['ntfs'] is False
