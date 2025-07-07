#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniOS Installer - Command Utilities
Common command execution utilities.

Copyright (C) 2025 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

import subprocess
from typing import List, Optional


def run_command(cmd: List[str], error_message: str) -> str:
    """
    Run subprocess.check_output(cmd). On failure, raise RuntimeError(error_message).
    """
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        raise RuntimeError(error_message)
