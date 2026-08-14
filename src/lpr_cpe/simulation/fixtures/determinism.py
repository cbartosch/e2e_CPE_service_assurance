"""Seeded jitter, so a simulated reading is a function of its reference and nothing else.

Every simulated measurement that is not written out in the fixtures is derived here from a hash of
the object reference. That is not decoration -- it is the difference between a scenario suite that
means something and one that is flaky. If `fetch_rf_measurements("SVC-SJ-A-01")` returned a fresh
`random.gauss()` each call, a detector threshold test would pass or fail depending on the draw, and
the two calls a graph makes for the same service inside one incident would disagree with each other.

`random.Random(hash(ref))` would not do: `str.__hash__` is salted per process (PYTHONHASHSEED), so
the same ref would produce different readings in different runs. SHA-256 of the reference is stable
across processes, machines and Python versions.
"""

from __future__ import annotations

import hashlib

__all__ = ["jitter", "pick", "unit"]

_SPREAD = 2**32


def unit(ref: str, salt: str = "") -> float:
    """A stable pseudo-random float in [0, 1) for this `(ref, salt)` pair.

    `salt` names the quantity being derived, so `unit(ref, "snr")` and `unit(ref, "power")` are
    independent while both staying reproducible for that ref.
    """
    digest = hashlib.sha256(f"{ref}\x1f{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / _SPREAD


def jitter(ref: str, salt: str, amplitude: float) -> float:
    """A stable offset in `[-amplitude, +amplitude]`.

    Used to spread a fixture's nominal value across the homes behind one delimiter so that neighbour
    comparison has something to compare, without any home's reading changing between calls.
    """
    return (unit(ref, salt) * 2.0 - 1.0) * amplitude


def pick[T](ref: str, salt: str, options: tuple[T, ...]) -> T:
    """Choose one of `options` deterministically for this ref (Wi-Fi channels, for instance).

    Generic in the element type because the choice has nothing to do with what is being chosen: a
    Wi-Fi channel is an int, a self-help decline reason is a str, and the arithmetic is the same.
    Typed `tuple[int, ...]` originally, which forced the one str caller to either lie in a cast or
    reimplement the index -- and a second copy of "seeded choice" is a second thing that can drift
    from this one.
    """
    if not options:
        raise ValueError("pick() needs at least one option")
    return options[int(unit(ref, salt) * len(options)) % len(options)]
