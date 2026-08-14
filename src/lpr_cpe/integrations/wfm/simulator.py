"""Fixture-backed workforce management: crew availability and work orders.

No WFM API was supplied (A1/A2). Slot shape, crew fields, cancellation semantics: all ours. Gaps
WFM-1 to WFM-5.

`fetch_crew_availability` returns slots shaped to construct `CrewSlot` directly, and it intersects
each crew's shift with the *requested* window rather than returning the shift. A caller asking "who
is free between 14:00 and 18:00" that receives a 06:00-15:00 shift has to do the intersection
itself, and the dispatch optimizer would then be the second owner of an arithmetic the adapter
already had everything to do -- and the place that gets it wrong for the crew whose shift starts
before the window and ends inside it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from lpr_cpe.domain.enums import ActionType, WorkOrderStatus
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterError
from lpr_cpe.simulation.fixtures.determinism import unit
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase


class SimulatedWFMAdapter(SimulatedAdapterBase):
    """Crew search plus work-order create/cancel/read."""

    system_name = "wfm"
    external_ref_prefix = "WO"

    # -- reads -----------------------------------------------------------------------------------

    async def fetch_crew_availability(
        self, *, area: str, crew_type: str, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        """Crews of `crew_type` able to work `area`, with their availability inside the window.

        A **collection query**: an unknown area or crew type returns `[]`. That is the honest answer
        and it is also the one the dispatch stage must handle anyway -- "no crew available on
        Vieques tomorrow" is a routine outcome, not an adapter failure, and raising would send it
        down the data-quality path instead of the escalation path.

        `area` accepts either an archetype (`remote_island`) or an area reference
        (`AREA-VQ-ISABEL`), because callers legitimately hold either: the topology gives an
        archetype, the CRM record gives an area ref. One normalisation here beats two call sites
        guessing.
        """
        self._ensure_available()
        if window_end <= window_start:
            raise AdapterError(
                self.system_name,
                f"window_end {window_end.isoformat()} is not after window_start "
                f"{window_start.isoformat()}",
                retryable=False,
            )
        archetype = self._normalise_area(area)
        if archetype is None:
            return []

        out: list[dict[str, Any]] = []
        for crew in self._fixtures.crews:
            if str(crew["crew_type"]) != crew_type:
                continue
            if archetype not in crew["area_archetypes"]:
                continue
            for shift_start, shift_end in self._shifts_in(crew, window_start, window_end):
                available_from = max(shift_start, window_start)
                available_until = min(shift_end, window_end)
                if available_until <= available_from:
                    continue
                out.append(
                    {
                        "crew_id": crew["crew_id"],
                        "crew_type": crew["crew_type"],
                        "skills": list(crew["skills"]),
                        "available_from": available_from,
                        "available_until": available_until,
                        "base_latitude": crew["base_latitude"],
                        "base_longitude": crew["base_longitude"],
                        "area_archetypes": list(crew["area_archetypes"]),
                        "max_jobs": crew["max_jobs"],
                        "carried_parts": list(crew["carried_parts"]),
                        # Deterministic per crew and per shift date, so a scenario that asserts on a
                        # chosen crew keeps choosing the same one.
                        "jobs_already_booked": int(
                            unit(f"{crew['crew_id']}:{shift_start.date()}", "booked")
                            * int(crew["max_jobs"])
                        ),
                        "on_call": crew["on_call"],
                        **self._provenance(str(crew["crew_id"])),
                    }
                )
        return out

    def _normalise_area(self, area: str) -> str | None:
        if area in self._fixtures.areas:
            return area
        for archetype, data in self._fixtures.areas.items():
            if data["area_ref"] == area:
                return archetype
        return None

    def _shifts_in(
        self, crew: dict[str, Any], window_start: datetime, window_end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Concrete shift instants for every local day the window touches.

        Shifts are stored as local hours, not instants (fixtures authoring rule 2), and are resolved
        in the clock's operating timezone -- `America/Puerto_Rico`. Resolving them in UTC would
        slide a 07:00 shift to 03:00 local and hand the optimizer crews that are asleep.
        """
        tz = self._clock.timezone
        start_hour = int(crew["shift_start_hour_local"])
        end_hour = int(crew["shift_end_hour_local"])
        out: list[tuple[datetime, datetime]] = []
        first_day = window_start.astimezone(tz).date()
        last_day = window_end.astimezone(tz).date()
        day = first_day
        while day <= last_day:
            shift_start = datetime(day.year, day.month, day.day, start_hour, tzinfo=tz)
            shift_end = datetime(day.year, day.month, day.day, end_hour, tzinfo=tz)
            out.append((shift_start.astimezone(UTC), shift_end.astimezone(UTC)))
            day = day + timedelta(days=1)
        return out

    async def fetch_work_order(self, work_order_ref: str) -> dict[str, Any]:
        """One work order. **Record read**: returns `{"found": False}` for an unknown reference.

        Not an exception, because in simulation a work order exists only if a write in this same
        process created it. A miss after a restart is expected and is not evidence that WFM and the
        workflow disagree, which is what `AdapterUnavailableError` would assert.
        """
        self._ensure_available()
        record = next(
            (w for w in self.recorded_writes if w.get("external_ref") == work_order_ref), None
        )
        if record is None:
            return {
                "work_order_ref": work_order_ref,
                "found": False,
                "data_available": False,
                "data_quality_notes": [
                    "no simulated work order with this reference; simulated work orders exist only "
                    "for writes made by this process"
                ],
                **self._provenance(work_order_ref),
            }
        cancelled = bool(record.get("cancelled"))
        return {
            "work_order_ref": work_order_ref,
            "found": True,
            "status": (
                WorkOrderStatus.CANCELLED.value if cancelled else WorkOrderStatus.REQUESTED.value
            ),
            "incident_id": record["incident_id"],
            "target_ref": record["target_ref"],
            "crew_id": record.get("crew_id"),
            "crew_type": record.get("crew_type"),
            "scheduled_start": record.get("scheduled_start"),
            "scheduled_end": record.get("scheduled_end"),
            "idempotency_key": record["idempotency_key"],
            "created_at": record["recorded_at"],
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(work_order_ref),
        }

    # -- writes ----------------------------------------------------------------------------------

    async def create_work_order(self, request: ActionRequest) -> dict[str, Any]:
        """Request a visit. Goes through the gate; never performs I/O.

        The crew, window and skills travel on `request.parameters` rather than as loose arguments,
        which is what `ActionRequest` is for: the six mandatory governance fields cannot be dropped
        at this call site because they are part of the same object.
        """
        if request.action_type is not ActionType.CREATE_WORK_ORDER:
            raise AdapterError(
                self.system_name,
                f"create_work_order requires action_type=create_work_order, "
                f"got {request.action_type.value}",
                retryable=False,
            )
        params = request.parameters
        return self.simulate_write(
            request,
            detail=f"work order requested for {request.target_ref}",
            extra={
                "status": WorkOrderStatus.REQUESTED.value,
                "crew_id": params.get("crew_id"),
                "crew_type": params.get("crew_type"),
                "scheduled_start": params.get("scheduled_start"),
                "scheduled_end": params.get("scheduled_end"),
                "skills_required": list(params.get("skills_required", [])),
                "parts_required": list(params.get("parts_required", [])),
                "customer_access_required": bool(params.get("customer_access_required", False)),
                "cancelled": False,
            },
        )

    async def cancel_work_order(self, request: ActionRequest) -> dict[str, Any]:
        """Cancel a visit. Goes through the gate; never performs I/O.

        A cancellation carries its own idempotency key, distinct from the creation's. Reusing the
        creation's key would make the cancel look like a replay of the create and return the
        create's result -- a cancellation that silently did nothing and reported success.
        """
        if request.action_type is not ActionType.CANCEL_WORK_ORDER:
            raise AdapterError(
                self.system_name,
                f"cancel_work_order requires action_type=cancel_work_order, "
                f"got {request.action_type.value}",
                retryable=False,
            )
        target = str(request.parameters.get("work_order_ref") or request.target_ref)
        result = self.simulate_write(
            request,
            detail=f"cancellation recorded for work order {target}",
            extra={
                "status": WorkOrderStatus.CANCELLED.value,
                "cancelled": True,
                "cancelled_work_order_ref": target,
                "cancellation_reason": request.parameters.get("reason", request.reason_code.value),
            },
        )
        # Reflect the cancellation onto the original record so `fetch_work_order` agrees. Without
        # this the work order would read as REQUESTED forever and reconciliation would chase it.
        for key, stored in self._ledger.items():
            if stored.get("external_ref") == target and key != request.idempotency_key:
                stored["cancelled"] = True
                stored["status"] = WorkOrderStatus.CANCELLED.value
        return result
