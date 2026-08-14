"""Fixture-backed geography, weather and utility power. Read-only, and read-only on purpose.

No GIS, weather or utility API was supplied (A1/A2). Gaps GIS-1 to GIS-5.

Read-only is a design decision, not an omission: nothing in this workflow may change a map, a
forecast or a utility's outage record, so the Protocol has no write method and this adapter
therefore never touches the write gate. An adapter with no writes cannot leak one.

The travel model is the part a reader is most likely to mistake for something real. It is a
great-circle distance multiplied by a per-archetype minutes-per-km rate, plus a fixed overhead, plus
a ferry allowance -- see `travel_minutes`. It is not a routing engine, it does not know about roads,
and on the day a bridge is out it will be confidently wrong.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from lpr_cpe.integrations.base import AdapterError, AdapterUnavailableError
from lpr_cpe.simulation.fixtures.determinism import jitter
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase

#: Mean Earth radius in km. The one number in this file that is not ours.
_EARTH_RADIUS_KM = 6371.0088

#: Ferry crossing to Vieques/Culebra, in minutes each way, plus the wait. Ours, and the reason
#: `remote_island` dispatch is a different decision rather than a slower one -- gap GIS-4.
_FERRY_MINUTES = 95.0

#: Wind above which the ferry stops running, in kph. Ours. A real deployment reads this from the
#: ferry operator, and on that day the answer to "can we dispatch" changes from slow to no.
FERRY_WIND_LIMIT_KPH = 55.0

#: How far ahead of, or behind, "now" a weather reading may be asked for before it stops being a
#: reading at all. Ours: the fixtures hold one current condition per area, so a request for next
#: Tuesday gets an honest `data_available: False` rather than an extrapolation.
FORECAST_HORIZON_HOURS = 12.0


class SimulatedGISAdapter(SimulatedAdapterBase):
    """Service location, current weather, utility outages, and a travel-time estimate."""

    system_name = "gis"
    # No writes. The prefix is inherited and unused; it is not overridden because overriding it
    # would imply this adapter issues references, and it does not.

    # -- reads -----------------------------------------------------------------------------------

    async def fetch_location(self, service_ref: str) -> dict[str, Any]:
        """Where the service is, and what makes it hard to reach. **Subject read**: unknown raises.

        Coordinates are rounded to five decimal places -- about a metre. That is enough to route a
        van and it is deliberately not enough to distinguish two flats on the same landing, because
        a location precise to the doorway is a customer identifier with a different name.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        archetype = str(service["archetype"])
        area = self._fixtures.area_for(archetype, system=self.system_name)
        return {
            "service_ref": service_ref,
            "latitude": round(float(service["latitude"]), 5),
            "longitude": round(float(service["longitude"]), 5),
            "area_ref": area["area_ref"],
            "area_name": area["name"],
            "archetype": archetype,
            "municipality": area["municipality"],
            "mdu_ref": service.get("mdu_ref"),
            # Copied from the area rather than restated per service, so "roof access permit" is
            # true of every home in Santurce or of none of them.
            "access_constraints": list(area["access_constraints"]),
            "ferry_required": bool(area["ferry_required"]),
            "delimiter_ref": service["delimiter_ref"],
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    async def fetch_weather(
        self, *, latitude: float, longitude: float, at: datetime
    ) -> dict[str, Any]:
        """Weather at a point. **Point read**: coordinates always resolve, to the nearest area.

        Neither of the other two miss policies fits. There is no such thing as an unknown coordinate
        -- every point on Earth has weather -- so raising would be wrong, and an empty list is not a
        weather report. The nearest modelled area is used instead, and `nearest_area_km` is returned
        so a caller can see how far the answer was carried. That number is the honest part: 60 km
        away in mountain terrain, the reading means very little.

        `field_work_safe` is the field the dispatch stage actually consumes, and it is not derived
        from a threshold here -- it is authored per area in the fixtures, because "is it safe to put
        a technician on a ladder" is a safety rule and not an arithmetic one. Gap GIS-3.
        """
        self._ensure_available()
        archetype, distance_km = self._nearest_area(latitude, longitude)
        reading = self._fixtures.weather_by_area[archetype]
        now = self._clock.now()
        hours_out = (at - now).total_seconds() / 3600.0
        in_horizon = abs(hours_out) <= FORECAST_HORIZON_HOURS
        notes: list[str] = []
        if not in_horizon:
            notes.append(
                f"requested time is {hours_out:+.1f}h from now, beyond the "
                f"{FORECAST_HORIZON_HOURS:.0f}h horizon this fixture set models"
            )
        if distance_km > 25.0:
            notes.append(
                f"nearest modelled area is {distance_km:.1f} km away; treat as regional, not local"
            )
        payload: dict[str, Any] = {
            "latitude": round(latitude, 5),
            "longitude": round(longitude, 5),
            "at": at.isoformat(),
            "archetype": archetype,
            "nearest_area_ref": self._fixtures.areas[archetype]["area_ref"],
            "nearest_area_km": round(distance_km, 1),
            "data_available": in_horizon,
            "data_quality_notes": notes,
            **self._provenance(f"{latitude:.4f},{longitude:.4f}"),
        }
        if not in_horizon:
            # Beyond the horizon the honest answer is no answer. Returning the current condition
            # labelled as a forecast is how a dispatch gets planned into a storm.
            return payload
        # A small deterministic wobble on the numbers, seeded from the rounded coordinates, so two
        # homes behind the same tap do not report byte-identical weather while a re-read of the same
        # home always does. Never `random`: a reading that drifts makes every scenario test flaky.
        seed = f"{latitude:.3f},{longitude:.3f}"
        payload.update(
            {
                "condition": reading["condition"],
                "temperature_c": round(
                    float(reading["temperature_c"]) + jitter(seed, "temp", 0.6), 1
                ),
                "wind_kph": round(
                    max(float(reading["wind_kph"]) + jitter(seed, "wind", 3.0), 0.0), 1
                ),
                "rain_mm_1h": round(
                    max(float(reading["rain_mm_1h"]) + jitter(seed, "rain", 0.8), 0.0), 1
                ),
                "lightning_within_10km": bool(reading["lightning_within_10km"]),
                "field_work_safe": bool(reading["field_work_safe"]),
                "advisory": reading["advisory"],
            }
        )
        return payload

    async def fetch_power_outages(
        self, *, latitude: float, longitude: float, radius_km: float
    ) -> list[dict[str, Any]]:
        """Utility outages whose footprint overlaps the search circle. **Collection query**: `[]`.

        Overlap, not containment: two circles overlap when their centres are closer than the sum of
        their radii. Testing containment instead would miss the case that matters -- an outage
        centred a kilometre up the road whose edge covers the street being asked about.

        Restored outages are returned as well as open ones, flagged. A fault that started while the
        power was out and did not clear when it came back is a different diagnosis from either one
        alone, and a caller that only ever sees open outages cannot reach it.
        """
        self._ensure_available()
        if radius_km <= 0:
            raise AdapterError(
                self.system_name, f"radius_km must be positive, got {radius_km}", retryable=False
            )
        out: list[dict[str, Any]] = []
        for outage in self._fixtures.power_outages:
            separation = self._haversine_km(
                latitude, longitude, float(outage["latitude"]), float(outage["longitude"])
            )
            if separation > radius_km + float(outage["radius_km"]):
                continue
            started_at = self._offset_hours(float(outage["started_offset_hours"]))
            restore = float(outage["estimated_restore_offset_hours"])
            out.append(
                {
                    "outage_ref": outage["outage_ref"],
                    "utility": outage["utility"],
                    "status": outage["status"],
                    "open": str(outage["status"]) == "open",
                    "latitude": outage["latitude"],
                    "longitude": outage["longitude"],
                    "radius_km": outage["radius_km"],
                    "distance_km": round(separation, 2),
                    # True when the queried point is inside the outage footprint itself, rather than
                    # merely within reach of the search radius. The stronger claim, so it is named.
                    "point_inside_footprint": separation <= float(outage["radius_km"]),
                    "started_at": started_at,
                    "estimated_restore_at": self._offset_hours(restore),
                    "customers_affected": outage["customers_affected"],
                    "cause": outage["cause"],
                    "hours_since_start": round(-float(outage["started_offset_hours"]), 1),
                    "data_available": True,
                    "data_quality_notes": [],
                    **self._provenance(str(outage["outage_ref"])),
                }
            )
        # Open first, then nearest: the order a correlation check wants to read them in.
        out.sort(key=lambda o: (not o["open"], float(o["distance_km"])))
        return out

    async def travel_minutes(
        self,
        *,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        archetype: str,
    ) -> float:
        """Estimated door-to-door minutes. **Subject read on the archetype**: unknown one raises.

        Great-circle distance times a per-archetype minutes-per-km rate, plus that archetype's fixed
        overhead, plus a ferry allowance where one is needed. It is not a routing engine -- gap
        GIS-2 is precisely "replace this with one".

        The archetype is a required argument rather than something derived from the coordinates, and
        that is the honest shape: 12 km across the San Juan grid and 12 km of mountain road are not
        the same journey, and the caller knows which one it is asking about. Deriving it from the
        destination would also silently misprice a crew driving *in* from a different archetype.

        The fixed overhead is the term that matters most in the metro: parking, a concierge and a
        riser key are 22 minutes whether the van drove 400 m or 4 km, and a pure per-km model would
        promise four-minute visits.
        """
        self._ensure_available()
        area = self._fixtures.area_for(archetype, system=self.system_name)
        distance_km = self._haversine_km(from_lat, from_lon, to_lat, to_lon)
        minutes = float(area["fixed_overhead_minutes"]) + distance_km * float(
            area["travel_minutes_per_km"]
        )
        if bool(area["ferry_required"]):
            # A ferry is not slower driving, it is a scheduled crossing. Adding a flat allowance is
            # already a simplification; a real model needs the timetable, which we do not have.
            minutes += _FERRY_MINUTES
        return round(minutes, 1)

    # -- helpers ---------------------------------------------------------------------------------

    def _nearest_area(self, latitude: float, longitude: float) -> tuple[str, float]:
        """The modelled area closest to a point, and how far away it is in km."""
        if not self._fixtures.areas:
            raise AdapterUnavailableError(self.system_name, "no areas in the fixture set")
        best_archetype = ""
        best_km = math.inf
        for archetype, area in self._fixtures.areas.items():
            km = self._haversine_km(
                latitude, longitude, float(area["latitude"]), float(area["longitude"])
            )
            if km < best_km:
                best_archetype, best_km = archetype, km
        return best_archetype, best_km

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Great-circle distance. Straight-line, which is why `travel_minutes` scales it."""
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        d_phi = phi2 - phi1
        d_lambda = math.radians(lon2 - lon1)
        a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(a, 1.0)))
