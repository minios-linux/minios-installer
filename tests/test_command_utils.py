#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for command_utils module.
"""

import sys
import os
import pytest
import subprocess
from unittest.mock import patch, MagicMock

# Add lib directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))


class TestRunCommand:
    """Tests for run_command function."""

    def test_successful_command(self):
        """Test successful command execution."""
        from command_utils import run_command
        
        with patch('subprocess.check_output', return_value='output') as mock_output:
            result = run_command(['echo', 'test'], 'Error message')
            
            assert result == 'output'
            mock_output.assert_called_once_with(
                ['echo', 'test'],
                text=True,
                stderr=subprocess.DEVNULL
            )

    def test_command_failure(self):
        """Test command failure raises RuntimeError."""
        from command_utils import run_command
        
        with patch('subprocess.check_output', 
                   side_effect=subprocess.CalledProcessError(1, 'cmd')):
            with pytest.raises(RuntimeError, match="Custom error"):
                run_command(['false'], 'Custom error')

    def test_returns_stdout(self):
        """Test that stdout is returned."""
        from command_utils import run_command
        
        with patch('subprocess.check_output', return_value='line1\nline2\n'):
            result = run_command(['cat', 'file'], 'Error')
            
            assert 'line1' in result
            assert 'line2' in result

    def test_empty_output(self):
        """Test handling of empty output."""
        from command_utils import run_command
        
        with patch('subprocess.check_output', return_value=''):
            result = run_command(['true'], 'Error')
            
            assert result == ''

    def test_error_message_preserved(self):
        """Test that error message is preserved in exception."""
        from command_utils import run_command
        
        with patch('subprocess.check_output',
                   side_effect=subprocess.CalledProcessError(1, 'cmd')):
            try:
                run_command(['cmd'], 'Specific error message')
                assert False, "Should have raised"
            except RuntimeError as e:
                assert str(e) == 'Specific error message'
