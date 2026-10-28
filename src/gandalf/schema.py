"""A small structural checker for stage outputs (DESIGN.md §11.2, D-51).

The standard library has no JSON Schema validator, so stage shapes are declared with
the few spec classes below. ``check(value, spec)`` returns a list of error strings,
each prefixed with the JSON path of the offending value; an empty list means valid.
"""

from __future__ import annotations

import re
from typing import Any


class Spec:
    def check(self, value: Any, path: str, errors: list[str]) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class Str(Spec):
    def __init__(self, pattern: str | None = None, nonempty: bool = True):
        self.pattern = re.compile(pattern) if pattern else None
        self.nonempty = nonempty

    def check(self, value, path, errors):
        if not isinstance(value, str):
            errors.append(f"{path}: expected a string, got {type(value).__name__}")
            return
        if self.nonempty and not value.strip():
            errors.append(f"{path}: must not be empty")
            return
        if self.pattern and not self.pattern.fullmatch(value):
            errors.append(f"{path}: {value!r} does not match {self.pattern.pattern}")

    def describe(self):
        return f"string matching {self.pattern.pattern}" if self.pattern else "string"


class Int(Spec):
    def __init__(self, minimum: int | None = 0):
        self.minimum = minimum

    def check(self, value, path, errors):
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: expected an integer, got {type(value).__name__}")
            return
        if self.minimum is not None and value < self.minimum:
            errors.append(f"{path}: must be >= {self.minimum}")

    def describe(self):
        return "integer"


class Bool(Spec):
    def check(self, value, path, errors):
        if not isinstance(value, bool):
            errors.append(f"{path}: expected true or false, got {type(value).__name__}")

    def describe(self):
        return "boolean"


class Enum(Spec):
    def __init__(self, *choices: str):
        self.choices = choices

    def check(self, value, path, errors):
        if value not in self.choices:
            errors.append(f"{path}: {value!r} is not one of {', '.join(self.choices)}")

    def describe(self):
        return " | ".join(self.choices)


class List(Spec):
    def __init__(self, item: Spec, min_items: int = 0, unique: bool = False):
        self.item = item
        self.min_items = min_items
        self.unique = unique

    def check(self, value, path, errors):
        if not isinstance(value, list):
            errors.append(f"{path}: expected a list, got {type(value).__name__}")
            return
        if len(value) < self.min_items:
            errors.append(f"{path}: needs at least {self.min_items} item(s)")
        for i, item in enumerate(value):
            self.item.check(item, f"{path}[{i}]", errors)
        if self.unique:
            seen = set()
            for item in value:
                key = repr(item)
                if key in seen:
                    errors.append(f"{path}: duplicate item {item!r}")
                seen.add(key)

    def describe(self):
        return f"list of {self.item.describe()}"


class Opt(Spec):
    """Marks an object field as optional. ``None`` is accepted as absent."""

    def __init__(self, spec: Spec):
        self.spec = spec

    def check(self, value, path, errors):
        if value is not None:
            self.spec.check(value, path, errors)

    def describe(self):
        return f"optional {self.spec.describe()}"


class Obj(Spec):
    def __init__(self, fields: dict[str, Spec], allow_extra: bool = False):
        self.fields = fields
        self.allow_extra = allow_extra

    def check(self, value, path, errors):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected an object, got {type(value).__name__}")
            return
        for name, spec in self.fields.items():
            if name not in value:
                if not isinstance(spec, Opt):
                    errors.append(f"{path}.{name}: required field is missing")
                continue
            spec.check(value[name], f"{path}.{name}", errors)
        if not self.allow_extra:
            for name in value:
                if name not in self.fields:
                    errors.append(f"{path}.{name}: unknown field")

    def describe(self):
        return "object"


class OneOf(Spec):
    def __init__(self, *specs: Spec):
        self.specs = specs

    def check(self, value, path, errors):
        attempts = []
        for spec in self.specs:
            sub: list[str] = []
            spec.check(value, path, sub)
            if not sub:
                return
            attempts.append(sub)
        best = min(attempts, key=len)
        errors.extend(best)

    def describe(self):
        return " or ".join(s.describe() for s in self.specs)


def check(value: Any, spec: Spec, path: str = "$") -> list[str]:
    errors: list[str] = []
    spec.check(value, path, errors)
    return errors


def _norm_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def normalise(value: Any, spec: Spec, path: str = "$") -> tuple[Any, list[str]]:
    """Fix obvious variants of allowed values ('stored data' -> 'stored_data') before
    validation, so a spelling difference isn't a rejection (D-64). Returns the
    normalised value and a note per change; never changes anything else."""
    notes: list[str] = []

    def walk(v, s, p):
        if isinstance(s, Opt):
            return v if v is None else walk(v, s.spec, p)
        if isinstance(s, Enum) and isinstance(v, str) and v not in s.choices:
            fixed = _norm_token(v)
            if fixed in s.choices:
                notes.append(f"{p}: {v!r} -> {fixed!r}")
                return fixed
            return v
        if isinstance(s, List) and isinstance(v, list):
            return [walk(item, s.item, f"{p}[{i}]") for i, item in enumerate(v)]
        if isinstance(s, Obj) and isinstance(v, dict):
            return {k: (walk(val, s.fields[k], f"{p}.{k}") if k in s.fields else val) for k, val in v.items()}
        if isinstance(getattr(s, "shape", None), Spec):
            return walk(v, s.shape, p)
        return v

    return walk(value, spec, path), notes


def allowed_values(spec: Spec, path: str = "") -> list[tuple[str, tuple[str, ...]]]:
    """Every field with a fixed set of values, as (field path, choices)."""
    out: list[tuple[str, tuple[str, ...]]] = []

    def walk(s, p):
        if isinstance(s, Opt):
            walk(s.spec, p)
        elif isinstance(s, Enum):
            out.append((p or "(value)", s.choices))
        elif isinstance(s, List):
            walk(s.item, p + "[]")
        elif isinstance(s, Obj):
            for name, sub in s.fields.items():
                walk(sub, f"{p}.{name}" if p else name)
        elif isinstance(s, OneOf):
            for sub in s.specs:
                walk(sub, p)
        elif isinstance(getattr(s, "shape", None), Spec):
            walk(s.shape, p)

    walk(spec, path)
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique
