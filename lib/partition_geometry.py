#!/usr/bin/env python3

"""Pure geometry helpers for interactive partition-layout previews."""


def clamp_resize_size(size_mib, minimum_mib, maximum_mib):
    """Return a whole-MiB size within the currently safe range."""
    minimum = int(minimum_mib)
    maximum = max(minimum, int(maximum_mib))
    return min(maximum, max(minimum, int(round(size_mib))))


def resize_boundary_x(size_mib, candidate_start_mib, candidate_size_mib,
                      disk_size_mib, width):
    """Map MiniOS's requested size to the resized partition's right boundary."""
    disk = max(1, int(disk_size_mib))
    boundary_mib = int(candidate_start_mib) + int(candidate_size_mib) - int(size_mib)
    return max(0.0, min(float(width), float(width) * boundary_mib / disk))


def resize_size_at_x(x, candidate_start_mib, candidate_size_mib,
                      disk_size_mib, width, minimum_mib, maximum_mib):
    """Map a pointer position to a clamped whole-MiB MiniOS size."""
    if width <= 0:
        return clamp_resize_size(minimum_mib, minimum_mib, maximum_mib)
    disk = max(1, int(disk_size_mib))
    boundary_mib = float(x) * disk / float(width)
    size_mib = int(candidate_start_mib) + int(candidate_size_mib) - boundary_mib
    return clamp_resize_size(size_mib, minimum_mib, maximum_mib)


def erase_swap_limits(disk_size_mib, root_start_mib, required_root_mib,
                      end_guard_mib=1):
    """Return the safe range for a native erase-all trailing swap partition."""
    disk = max(0, int(disk_size_mib))
    root_start = max(0, int(root_start_mib))
    required_root = max(1, int(required_root_mib or 0))
    end_guard = max(0, int(end_guard_mib))
    return 0, max(0, disk - root_start - required_root - end_guard)


def trailing_swap_boundary_x(swap_size_mib, disk_size_mib, width,
                             end_guard_mib=1):
    """Map a trailing swap size to its root/swap boundary on the disk strip."""
    disk = max(1, int(disk_size_mib))
    boundary_mib = disk - max(0, int(end_guard_mib)) - max(0, int(swap_size_mib))
    return max(0.0, min(float(width), float(width) * boundary_mib / disk))


def trailing_swap_size_at_x(x, disk_size_mib, width, minimum_mib, maximum_mib,
                            end_guard_mib=1):
    """Map a pointer position to a clamped trailing swap size."""
    if width <= 0:
        return clamp_resize_size(minimum_mib, minimum_mib, maximum_mib)
    disk = max(1, int(disk_size_mib))
    boundary_mib = float(x) * disk / float(width)
    swap_mib = disk - max(0, int(end_guard_mib)) - boundary_mib
    return clamp_resize_size(swap_mib, minimum_mib, maximum_mib)
