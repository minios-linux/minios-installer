#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for mount_utils module.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock, mock_open

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


class TestMountPartition:
    """Tests for mount_partition function."""

    def test_already_mounted(self):
        """Test error when destination is already mounted."""
        from mount_utils import mount_partition
        
        with patch('os.path.ismount', return_value=True):
            with pytest.raises(RuntimeError, match="already mounted"):
                mount_partition('/dev/sda1', '/mnt/test')

    def test_successful_mount(self):
        """Test successful partition mount."""
        from mount_utils import mount_partition
        
        with patch('os.path.ismount', return_value=False), \
             patch('os.path.isdir', return_value=False), \
             patch('os.makedirs') as mock_makedirs, \
             patch('mount_utils.run_command', return_value='ext4'), \
             patch('subprocess.call', return_value=0) as mock_call:
            
            mount_partition('/dev/sda1', '/mnt/test')
            
            mock_makedirs.assert_called_once_with('/mnt/test', exist_ok=True)
            mock_call.assert_called_once()
            args = mock_call.call_args[0][0]
            assert 'mount' in args
            assert '-t' in args
            assert 'ext4' in args

    def test_mount_failure(self):
        """Test handling of mount failure."""
        from mount_utils import mount_partition
        
        with patch('os.path.ismount', return_value=False), \
             patch('os.path.isdir', return_value=False), \
             patch('os.makedirs'), \
             patch('mount_utils.run_command', return_value='ext4'), \
             patch('subprocess.call', return_value=1):
            
            with pytest.raises(RuntimeError, match="Failed to mount"):
                mount_partition('/dev/sda1', '/mnt/test')

    def test_clears_existing_directory(self):
        """Test that existing non-empty directory is cleared."""
        from mount_utils import mount_partition
        
        with patch('os.path.ismount', return_value=False), \
             patch('os.path.isdir', return_value=True), \
             patch('os.listdir', return_value=['file1', 'file2']), \
             patch('shutil.rmtree') as mock_rmtree, \
             patch('os.makedirs'), \
             patch('mount_utils.run_command', return_value='ext4'), \
             patch('subprocess.call', return_value=0):
            
            mount_partition('/dev/sda1', '/mnt/test')
            mock_rmtree.assert_called_once()


class TestUnmountPartitions:
    """Tests for unmount_partitions function."""

    def test_unmount_single_partition(self):
        """Test unmounting single partition."""
        from mount_utils import unmount_partitions
        
        with patch('os.path.ismount', side_effect=[False, True]), \
             patch('subprocess.check_call') as mock_check_call, \
             patch('os.path.isdir', return_value=True), \
             patch('shutil.rmtree'):
            
            unmount_partitions('/dev/sda1', None, '/mnt/p1', None)
            mock_check_call.assert_called_once()

    def test_unmount_both_partitions(self):
        """Test unmounting both partitions."""
        from mount_utils import unmount_partitions
        
        with patch('os.path.ismount', return_value=True), \
             patch('subprocess.check_call') as mock_check_call, \
             patch('os.path.isdir', return_value=True), \
             patch('shutil.rmtree'):
            
            unmount_partitions('/dev/sda1', '/dev/sda2', '/mnt/p1', '/mnt/p2')
            assert mock_check_call.call_count == 2

    def test_lazy_unmount_fallback(self):
        """Test fallback to lazy unmount on failure."""
        import subprocess
        from mount_utils import unmount_partitions
        
        call_count = [0]
        def side_effect(cmd):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.CalledProcessError(1, 'umount')
            return None
        
        with patch('os.path.ismount', return_value=True), \
             patch('subprocess.check_call', side_effect=side_effect), \
             patch('os.path.isdir', return_value=True), \
             patch('shutil.rmtree'):
            
            # Should not raise, falls back to lazy unmount
            unmount_partitions('/dev/sda1', None, '/mnt/p1', None)

    def test_removes_mount_directories(self):
        """Test that mount directories are removed after unmount."""
        from mount_utils import unmount_partitions
        
        with patch('os.path.ismount', return_value=False), \
             patch('os.path.isdir', return_value=True), \
             patch('shutil.rmtree') as mock_rmtree:
            
            unmount_partitions('/dev/sda1', '/dev/sda2', '/mnt/p1', '/mnt/p2')
            assert mock_rmtree.call_count == 2


class TestGetMountedPartitions:
    """Tests for get_mounted_partitions function."""

    def test_finds_mounted_partitions(self):
        """Test finding mounted partitions for a device."""
        from mount_utils import get_mounted_partitions
        
        proc_mounts = '''/dev/sda1 /mnt/data ext4 rw,relatime 0 0
/dev/sda2 /boot/efi vfat rw,relatime 0 0
/dev/sdb1 /mnt/backup ext4 rw,relatime 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev 0 0'''
        
        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            mounted = get_mounted_partitions('/dev/sda')
            
            assert len(mounted) == 2
            assert ('/dev/sda1', '/mnt/data') in mounted
            assert ('/dev/sda2', '/boot/efi') in mounted

    def test_no_mounted_partitions(self):
        """Test when no partitions are mounted."""
        from mount_utils import get_mounted_partitions
        
        proc_mounts = '''tmpfs /tmp tmpfs rw,nosuid,nodev 0 0'''
        
        with patch('builtins.open', mock_open(read_data=proc_mounts)):
            mounted = get_mounted_partitions('/dev/sda')
            assert len(mounted) == 0

    def test_handles_read_error(self):
        """Test handling of /proc/mounts read error."""
        from mount_utils import get_mounted_partitions
        
        with patch('builtins.open', side_effect=IOError("Permission denied")):
            mounted = get_mounted_partitions('/dev/sda')
            assert mounted == []


class TestForceUnmountDevice:
    """Tests for force_unmount_device function."""

    def test_force_unmount_all_partitions(self):
        """Test force unmounting all partitions of a device."""
        from mount_utils import force_unmount_device
        
        proc_mounts = '''/dev/sda1 /mnt/data ext4 rw 0 0
/dev/sda2 /boot/efi vfat rw 0 0'''
        
        with patch('builtins.open', mock_open(read_data=proc_mounts)), \
             patch('subprocess.run') as mock_run:
            
            force_unmount_device('/dev/sda')
            assert mock_run.call_count == 2

    def test_handles_unmount_errors(self):
        """Test handling of unmount errors during force unmount."""
        import subprocess
        from mount_utils import force_unmount_device
        
        proc_mounts = '''/dev/sda1 /mnt/data ext4 rw 0 0'''
        
        with patch('builtins.open', mock_open(read_data=proc_mounts)), \
             patch('subprocess.run', side_effect=subprocess.SubprocessError("error")):
            
            # Should not raise
            force_unmount_device('/dev/sda')
