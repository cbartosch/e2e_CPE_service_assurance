"""Reading scalars out of adapter payloads without inventing any.

Every decision service in this package is handed a `dict[str, Any]` that came from an adapter, and
every one of them has the same problem: the field may be absent, may be null, may be the wrong type,
and may be a string where a number was promised. The three readers here answer that uniformly, and
they all answer it the same way -- `None`, meaning *this payload did not tell us*, never a zero or
an empty string standing in for it. The distinction is load-bearing everywhere downstream:
`decision_services.blast_radius` treats a missing count as an estimate and says so; a zero would be
treated as a measurement of nobody.

They live in one module rather than as private helpers in each service because of `read_int`. `bool`
is a subclass of `int` in Python, so `isinstance(True, int)` is True, and a payload carrying
`homes_behind_delimiter: true` becomes a blast radius of exactly 1 -- a measured-looking number
produced from a field that carried no number at all. That guard is one line, it is easy to leave out
of the fourth copy, and the failure it prevents is silent.
"""

from __future__ import annotations

from typing import Any

__all__ = ["read_float", "read_int", "read_text"]


def read_int(payload: dict[str, Any], key: str, *, minimum: int = 0) -> int | None:
    """A whole number at or above `minimum`, or `None`.

    Booleans are rejected before the `int` check, not after: see the module docstring. A value below
    `minimum` is `None` rather than clamped, because clamping -1 to 0 would report "no customers
    affected" for a field that was corrupt.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= minimum else None


def read_float(payload: dict[str, Any], key: str) -> float | None:
    """A real number, or `None`. Accepts an `int`, since JSON writes `18` for `18.0`."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def read_text(payload: dict[str, Any], key: str) -> str | None:
    """A non-empty string with surrounding whitespace removed, or `None`.

    `"  "` is `None` rather than `"  "`. A reference made of spaces passes every `is not None` check
    in this package and names no object in any system.
    """
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
