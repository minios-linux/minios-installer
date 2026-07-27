#!/usr/bin/env python3
"""GUI-independent staged state for the manual partitioning page."""
from __future__ import absolute_import

from manual_partitioning import (ManualAction, ManualPlanError, ManualPlanner,
                                 MountAssignment, SectorExtent)


class ManualPartitionController(object):
    """Mutates only an in-memory plan and retains one-step undo snapshots."""

    def __init__(self, snapshot, use_efi=False, required_root_sectors=0,
                 alignment_sectors=2048, install_mode="native"):
        self.snapshot = snapshot
        self.use_efi = use_efi
        self.required_root_sectors = required_root_sectors
        self.alignment_sectors = alignment_sectors
        self.install_mode = install_mode
        self.actions = []
        self.assignments = []
        self.plan = None
        self.error = ""
        self._undo = None
        self._validate()

    @property
    def destructive(self):
        return any(action.kind in ("create", "delete", "shrink", "format")
                   for action in self.actions)

    def _save(self):
        self._undo = (list(self.actions), list(self.assignments))

    def _validate(self):
        try:
            self.plan = ManualPlanner().stage(
                self.snapshot, self.actions, self.assignments, self.use_efi,
                self.required_root_sectors, self.alignment_sectors, self.install_mode)
            self.error = ""
        except ManualPlanError as exc:
            self.plan = None
            self.error = str(exc)
        return self.plan

    def _change(self, func):
        self._save()
        func()
        return self._validate()

    def create(self, start_sector, size_sectors, fstype="ext4"):
        extent = SectorExtent(start_sector, size_sectors)
        def change():
            self.actions.append(ManualAction("create", extent=extent, fstype=fstype))
        return self._change(change)

    def delete(self, target):
        def change():
            if isinstance(target, SectorExtent):
                self.actions[:] = [a for a in self.actions if a.target != target and
                                   not (a.kind == "create" and a.extent == target)]
                self.assignments[:] = [a for a in self.assignments if a.target != target]
                return
            self.actions[:] = [a for a in self.actions if a.target != target]
            self.actions.append(ManualAction("delete", target))
            self.assignments[:] = [a for a in self.assignments if a.target != target]
        return self._change(change)

    def resize(self, target, size_sectors):
        extent = SectorExtent(target.start_sector, size_sectors)
        def change():
            self.actions[:] = [a for a in self.actions if not (a.kind == "shrink" and a.target == target)]
            self.actions.append(ManualAction("shrink", target, extent))
        return self._change(change)

    def use_as(self, target, role, mountpoint="", fstype="", format=False):
        """Replace one assignment; existing partitions format only when requested."""
        def change():
            self.assignments[:] = [a for a in self.assignments if a.target != target]
            self.actions[:] = [a for a in self.actions if not (a.kind == "format" and a.target == target)]
            actual_fs = (fstype or getattr(target, "fstype", "")).lower()
            if format:
                self.actions.append(ManualAction("format", target, fstype=actual_fs))
            self.assignments.append(MountAssignment(target, role, mountpoint, actual_fs, format))
        return self._change(change)

    def undo(self):
        if self._undo is None:
            return self.plan
        self.actions, self.assignments = self._undo
        self._undo = None
        return self._validate()

    def reset(self):
        self._save()
        self.actions = []
        self.assignments = []
        return self._validate()

    def summary_lines(self):
        if self.plan:
            return self.plan.summary_lines()
        return [self.error] if self.error else []
