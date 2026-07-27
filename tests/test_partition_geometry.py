#!/usr/bin/env python3

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))

from partition_geometry import (clamp_resize_size, erase_swap_limits,
                                 resize_boundary_x, resize_size_at_x,
                                 trailing_swap_boundary_x,
                                 trailing_swap_size_at_x)


def test_resize_size_is_clamped_to_dynamic_limits():
    assert clamp_resize_size(10, 512, 4096) == 512
    assert clamp_resize_size(9999, 512, 4096) == 4096
    assert clamp_resize_size(1536.6, 512, 4096) == 1537


def test_boundary_maps_requested_space_from_candidate_end():
    assert resize_boundary_x(2000, 1000, 8000, 20000, 1000) == 350


def test_pointer_mapping_round_trips_and_clamps_outside_handle_range():
    boundary = resize_boundary_x(2000, 1000, 8000, 20000, 1000)
    assert resize_size_at_x(boundary, 1000, 8000, 20000, 1000, 512, 4096) == 2000
    assert resize_size_at_x(-100, 1000, 8000, 20000, 1000, 512, 4096) == 4096
    assert resize_size_at_x(2000, 1000, 8000, 20000, 1000, 512, 4096) == 512


def test_erase_swap_limits_reserve_fixed_root_start_and_disk_guards():
    assert erase_swap_limits(20000, 101, 4096) == (0, 15802)
    assert erase_swap_limits(1000, 101, 1000) == (0, 0)


def test_trailing_swap_boundary_round_trips_from_zero_and_clamps():
    boundary = trailing_swap_boundary_x(2000, 20000, 1000)
    assert boundary == 899.95
    assert trailing_swap_size_at_x(boundary, 20000, 1000, 0, 4000) == 2000
    assert trailing_swap_size_at_x(1000, 20000, 1000, 0, 4000) == 0
    assert trailing_swap_size_at_x(-100, 20000, 1000, 0, 4000) == 4000
