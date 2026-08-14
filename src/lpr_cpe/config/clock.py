"""One clock, injected.

Time is read through a `Clock` rather than by calling `datetime.now()` so that SLA arithmetic,
scan-window scheduling and the scenario tests all agree about "now". A test that has to sleep to
advance time is a test that will be flaky on a loaded machine.

`America/Puerto_Rico` is fixed UTC-04:00 with no daylight saving, which is why the scan windows can
be compared as wall-clock times without a DST correction. That is a property of the zone, not an
assumption we are making, but it is worth stating because the same code in a DST zone would need the
07:00/21:00 windows resolved per-day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo


@runtime_checkable
class Clock(Protocol):
    """The only source of the current instant."""

    def now(self) -> datetime:
        """Timezone-aware UTC."""
        ...

    def local_now(self) -> datetime:
        """The same instant rendered in the operating timezone."""
        ...

    @property
    def timezone(self) -> tzinfo:
        """The operating timezone."""
        ...


class SystemClock:
    """Reads the host clock. The implementation used in a running service."""

    __slots__ = ("_tz",)

    def __init__(self, timezone_name: str = "America/Puerto_Rico") -> None:
        self._tz: tzinfo = ZoneInfo(timezone_name)

    def now(self) -> datetime:
        return datetime.now(UTC)

    def local_now(self) -> datetime:
        return self.now().astimezone(self._tz)

    @property
    def timezone(self) -> tzinfo:
        return self._tz


class FrozenClock:
    """A clock that only moves when told to.

    Used by every test that asserts on an elapsed duration or a deadline. `advance()` returns the
    new instant so a test can read it without a second call.
    """

    __slots__ = ("_now", "_tz")

    def __init__(
        self,
        instant: datetime | None = None,
        timezone_name: str = "America/Puerto_Rico",
    ) -> None:
        if instant is None:
            instant = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
        if instant.tzinfo is None:
            raise ValueError("FrozenClock refuses a naive datetime; pass tzinfo")
        self._now = instant.astimezone(UTC)
        self._tz: tzinfo = ZoneInfo(timezone_name)

    def now(self) -> datetime:
        return self._now

    def local_now(self) -> datetime:
        return self._now.astimezone(self._tz)

    @property
    def timezone(self) -> tzinfo:
        return self._tz

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock refuses a naive datetime; pass tzinfo")
        self._now = instant.astimezone(UTC)
        return self._now


def parse_scan_windows(raw: str) -> tuple[time, ...]:
    """`"07:00,21:00"` -> two `time` objects, sorted and de-duplicated.

    Raises on anything unparseable rather than dropping it: a typo'd scan window that silently
    disappears means a sweep that never runs and nothing that says so.
    """
    out: set[time] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2:
            raise ValueError(f"scan window {chunk!r} is not HH:MM")
        hh, mm = parts
        try:
            out.add(time(hour=int(hh), minute=int(mm)))
        except ValueError as exc:
            raise ValueError(f"scan window {chunk!r} is not a valid HH:MM: {exc}") from exc
    if not out:
        raise ValueError("no scan windows configured")
    return tuple(sorted(out))


def next_scan_window(
    after: datetime,
    windows: tuple[time, ...],
    timezone: tzinfo,
) -> datetime:
    """The first scan instant strictly after `after`, as UTC.

    Strictly after, so calling this from inside a scan that started exactly on the window does not
    return the window it is already running.
    """
    local = after.astimezone(timezone)
    for day_offset in (0, 1):
        day: date = (local + timedelta(days=day_offset)).date()
        for w in windows:
            candidate = datetime.combine(day, w, tzinfo=timezone)
            if candidate > local:
                return candidate.astimezone(UTC)
    raise AssertionError("unreachable: two days always contain a next window")
