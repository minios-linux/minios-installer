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
        return subprocess.check_output(cmd, universal_newlines=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        output = (exc.output or "").strip()
        if output:
            raise RuntimeError(f"{error_message}\n{output}")
        raise RuntimeError(error_message)
