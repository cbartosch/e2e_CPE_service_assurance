"""How long it takes to get there, and which model said so.

Travel time has two possible sources and they are not interchangeable, so this module makes the
caller pick one and records which was picked:

* **`GISTravelModel`** -- distances resolved through `integrations.base.GISAdapter.travel_minutes`,
  which in production is a routing engine (gap GIS-2). This is a *lookup*.
* **`PolicyTravelModel`** -- the pack's `dispatch.archetype_*` numbers. This is an *estimate*, and
  the fallback when the adapter is unavailable or coordinates are missing.

`TravelEstimate.basis` carries the difference onto the plan, for the same reason
`BlastRadius.measured` exists: a schedule costed on a straight-line archetype average and one costed
on a road network are both plausible-looking, and only one of them is a reason to promise a customer
an arrival time.

**Why the optimizer does not simply call the adapter.** The solve must be deterministic and pure --
the specification says so twice, and a solver that awaits I/O mid-search cannot be replayed from a
checkpoint. So the caller resolves travel *first*, into a matrix, and hands the optimizer a plain
mapping. `GISTravelModel.prefetch` is that step, and it is the only `async` thing in this package.

**The three terms.** Driving, fixed per-visit overhead, and the ferry are separate because they
behave differently under optimisation. Halving the distance halves the first, does nothing to the
second, and does nothing at all to the third. Folding the crossing into a speed -- which the pack
used to do -- makes a Vieques job look like a slow drive that gets cheaper as jobs cluster, when in
fact the 95-minute crossing is paid once per trip to the island regardless.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from lpr_cpe.domain.enums import AreaArchetype
from lpr_cpe.policies.models import DispatchPolicy

#: Mean Earth radius in km, matching `integrations.gis.simulator`. Restated rather than imported
#: because importing a simulator into the optimizer would make the production path depend on the
#: fixture package; the value is a physical constant, not a decision either module owns.
_EARTH_RADIUS_KM = 6371.0088

#: Where a travel number came from. `estimated` is not a lesser answer, it is a different one, and
#: `DispatchPlan.objective` records it so a reviewer can tell which they are reading.
TravelBasis = Literal["routed", "estimated", "unknown"]


@dataclass(frozen=True, slots=True)
class TravelEstimate:
    """Minutes, plus how they were arrived at.

    `ferry_minutes` is broken out rather than folded into `minutes` because it is the term that
    decides whether a second island job is possible at all, and a dispatcher reading a 130-minute
    leg needs to know that 95 of it is a boat.
    """

    minutes: float
    basis: TravelBasis
    ferry_minutes: float = 0.0

    @property
    def drive_and_access_minutes(self) -> float:
        """The part of the journey that clustering jobs can actually reduce."""
        return max(0.0, self.minutes - self.ferry_minutes)


class TravelModel(Protocol):
    """What the optimizer needs from travel: a synchronous, total function.

    Total in the mathematical sense -- it must return an answer for every pair it is asked about,
    including pairs with no coordinates. Returning `None` and letting the caller decide would put
    the fallback choice inside the solve loop, where it would be made differently at each of the
    several sites that need it.
    """

    def between(
        self,
        *,
        from_lat: float | None,
        from_lon: float | None,
        to_lat: float | None,
        to_lon: float | None,
        archetype: AreaArchetype | None,
    ) -> TravelEstimate: ...


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Straight-line, which is why every caller scales it."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(a, 1.0)))


@dataclass(frozen=True, slots=True)
class PolicyTravelModel:
    """The pack's archetype model: `overhead + km x (60 / kph) + ferry`.

    Used when there are no coordinates or no adapter. The archetype is the required input rather
    than something inferred from the coordinates, because a crew driving *in* from the metro to a
    mountain job is doing a mountain journey, and inferring from the destination alone would price
    it as whichever archetype happened to be nearest.

    A missing archetype falls back to the slowest one in the pack. That direction is deliberate: an
    optimizer that guesses low schedules a day that cannot be driven and reports it as feasible,
    which is the failure D14 exists to prevent. Guessing high leaves a crew with spare time.
    """

    policy: DispatchPolicy

    def between(
        self,
        *,
        from_lat: float | None,
        from_lon: float | None,
        to_lat: float | None,
        to_lon: float | None,
        archetype: AreaArchetype | None,
    ) -> TravelEstimate:
        area = archetype if archetype is not None else self._slowest_archetype()
        overhead = float(self.policy.access_overhead_minutes(area))
        ferry = float(self.policy.ferry_minutes(area))
        if None in (from_lat, from_lon, to_lat, to_lon):
            # No coordinates: the fixed terms are still known and are the larger part of a short
            # job. Reporting zero here would let the optimizer pack a shift with jobs whose access
            # overhead alone would overrun it.
            return TravelEstimate(minutes=overhead + ferry, basis="unknown", ferry_minutes=ferry)
        assert from_lat is not None and from_lon is not None  # narrowed by the check above
        assert to_lat is not None and to_lon is not None
        km = haversine_km(from_lat, from_lon, to_lat, to_lon)
        drive = km * (60.0 / self.policy.speed_kph(area))
        return TravelEstimate(
            minutes=round(overhead + drive + ferry, 1), basis="estimated", ferry_minutes=ferry
        )

    def _slowest_archetype(self) -> AreaArchetype:
        """The worst case in the pack, chosen by total cost of a nominal 5 km job.

        Ranking on speed alone would pick the mountains and miss that a remote-island job carries a
        95-minute crossing on top. Ties break on the enum value so the choice is stable.
        """
        return min(
            AreaArchetype,
            key=lambda a: (
                -(
                    self.policy.access_overhead_minutes(a)
                    + self.policy.ferry_minutes(a)
                    + 5.0 * (60.0 / self.policy.speed_kph(a))
                ),
                a.value,
            ),
        )


@dataclass(frozen=True, slots=True)
class MatrixTravelModel:
    """Pre-resolved point-to-point minutes, keyed on rounded coordinates.

    Built by `prefetch_travel` from whatever the GIS adapter said, then handed to the optimizer as
    inert data. The rounding is to 4 decimal places -- about 11 metres, finer than any dispatch
    decision -- so that a float that made a round trip through JSON still hits its own entry.

    `fallback` answers pairs the matrix does not contain. It is required rather than optional
    because a matrix miss returning zero would make an unrouted job look like the cheapest one on
    the board and pull it to the front of every route.
    """

    matrix: Mapping[tuple[str, str], float]
    fallback: PolicyTravelModel
    ferry_by_archetype: Mapping[AreaArchetype, float]

    @staticmethod
    def key(lat: float, lon: float) -> str:
        return f"{lat:.4f},{lon:.4f}"

    def between(
        self,
        *,
        from_lat: float | None,
        from_lon: float | None,
        to_lat: float | None,
        to_lon: float | None,
        archetype: AreaArchetype | None,
    ) -> TravelEstimate:
        if None not in (from_lat, from_lon, to_lat, to_lon):
            assert from_lat is not None and from_lon is not None
            assert to_lat is not None and to_lon is not None
            found = self.matrix.get((self.key(from_lat, from_lon), self.key(to_lat, to_lon)))
            if found is not None:
                ferry = self.ferry_by_archetype.get(archetype, 0.0) if archetype else 0.0
                return TravelEstimate(minutes=found, basis="routed", ferry_minutes=ferry)
        return self.fallback.between(
            from_lat=from_lat,
            from_lon=from_lon,
            to_lat=to_lat,
            to_lon=to_lon,
            archetype=archetype,
        )


async def prefetch_travel(
    adapter: object,
    *,
    points: Sequence[tuple[float, float]],
    archetype: AreaArchetype,
    fallback: PolicyTravelModel,
) -> MatrixTravelModel:
    """Resolve every ordered pair of `points` through the GIS adapter, once, before the solve.

    Adapter failures are absorbed per pair rather than aborting: a routing engine that times out on
    one leg should cost that leg the pack's estimate, not cancel the dispatch. Which legs fell back
    is visible on each `TravelEstimate.basis`, so a plan that is mostly estimates does not present
    itself as a routed one.

    `adapter` is typed `object` and probed for the method rather than imported as `GISAdapter`,
    because `integrations` imports domain models and importing it here would close a cycle. The
    duck-type check is narrow -- one method, one signature -- and the fallback covers its absence.
    """
    travel = getattr(adapter, "travel_minutes", None)
    matrix: dict[tuple[str, str], float] = {}
    if callable(travel):
        for from_lat, from_lon in points:
            for to_lat, to_lon in points:
                if (from_lat, from_lon) == (to_lat, to_lon):
                    continue
                try:
                    minutes = await travel(
                        from_lat=from_lat,
                        from_lon=from_lon,
                        to_lat=to_lat,
                        to_lon=to_lon,
                        archetype=archetype.value,
                    )
                except Exception:  # noqa: BLE001 -- any adapter failure means "use the estimate"
                    continue
                key = (
                    MatrixTravelModel.key(from_lat, from_lon),
                    MatrixTravelModel.key(to_lat, to_lon),
                )
                matrix[key] = float(minutes)
    return MatrixTravelModel(
        matrix=matrix,
        fallback=fallback,
        ferry_by_archetype={a: float(fallback.policy.ferry_minutes(a)) for a in AreaArchetype},
    )


__all__ = [
    "MatrixTravelModel",
    "PolicyTravelModel",
    "TravelBasis",
    "TravelEstimate",
    "TravelModel",
    "haversine_km",
    "prefetch_travel",
]
