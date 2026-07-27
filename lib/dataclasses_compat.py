#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal dataclasses fallback for Python 3.6 when the stdlib module is absent.

Only supports the features used by minios-installer: @dataclass, field(default=...),
field(default_factory=...), and simple attribute assignment in __init__.
Prefer installing python3-dataclasses on 3.6, or using Python 3.7+.
"""

from copy import deepcopy

MISSING = object()


def field(default=MISSING, default_factory=MISSING):
    return _Field(default=default, default_factory=default_factory)


class _Field(object):
    def __init__(self, default=MISSING, default_factory=MISSING):
        self.default = default
        self.default_factory = default_factory
        self.name = None


def dataclass(cls):
    annotations = getattr(cls, "__annotations__", {}) or {}
    fields = []
    for name in annotations:
        if name in cls.__dict__:
            value = cls.__dict__[name]
            if isinstance(value, _Field):
                value.name = name
                fields.append(value)
            else:
                f = _Field(default=value)
                f.name = name
                fields.append(f)
        else:
            f = _Field()
            f.name = name
            fields.append(f)

    def __init__(self, *args, **kwargs):
        # Map positional args to field order
        for idx, f in enumerate(fields):
            if idx < len(args):
                setattr(self, f.name, args[idx])
                continue
            if f.name in kwargs:
                setattr(self, f.name, kwargs.pop(f.name))
                continue
            if f.default_factory is not MISSING:
                setattr(self, f.name, f.default_factory())
            elif f.default is not MISSING:
                default = f.default
                # Avoid sharing mutable defaults
                if isinstance(default, (list, dict, set)):
                    setattr(self, f.name, deepcopy(default))
                else:
                    setattr(self, f.name, default)
            else:
                raise TypeError("Missing required argument: %s" % f.name)
        if kwargs:
            raise TypeError("Unexpected kwargs: %s" % sorted(kwargs.keys()))

    def __eq__(self, other):
        if other is None or type(other) is not type(self):
            return False
        for f in fields:
            if getattr(self, f.name) != getattr(other, f.name):
                return False
        return True

    def __repr__(self):
        parts = ["%s=%r" % (f.name, getattr(self, f.name)) for f in fields]
        return "%s(%s)" % (cls.__name__, ", ".join(parts))

    cls.__init__ = __init__
    cls.__eq__ = __eq__
    cls.__repr__ = __repr__
    cls.__dataclass_fields__ = {f.name: f for f in fields}
    return cls
