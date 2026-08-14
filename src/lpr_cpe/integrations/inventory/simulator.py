"""Fixture-backed plant inventory: what exists, what changed recently, what is in stock.

No inventory API was supplied (A1/A2). Every field name is ours. Gaps INV-1 to INV-5.

Two things make this adapter unusual and both are deliberate:

**It is the existence oracle, so it does not raise on a miss.** Every other subject read raises
`AdapterUnavailableError` for an unknown reference, on the grounds that two systems disagreeing
about a customer is a data-quality fact. Inventory is the system that *answers* that question.
Asking it "is `AMP-SJ-011-3` a real amplifier" and getting an exception means it cannot answer its
own question, so `fetch_plant_object` returns `{"found": False}` and the workflow reads it as
evidence rather than as an outage. `Fixtures.plant_object` is the one lookup that returns `None`
for the same reason.

**Parts live in the same reference namespace as plant.** The Protocol has three methods and the
specification also wants parts, van stock, reservation and consumption. Rather than invent a fourth
method the contract does not have, a `PART-*` reference is a plant object like any other: read it
with `fetch_plant_object`, reserve or consume it with `update_plant_object`. The part vocabulary is
not defined here -- it is derived from `CREWS[*]["carried_parts"]`, so there is exactly one place
that decides which parts exist and van stock cannot disagree with the catalogue.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from lpr_cpe.config.clock import Clock
from lpr_cpe.domain.enums import ActionType
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterError, WriteGate
from lpr_cpe.simulation.fixtures.determinism import unit
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase

if TYPE_CHECKING:
    from lpr_cpe.simulation.loader import Fixtures

#: Prefix that marks a reference as a consumable rather than a piece of plant. Ours.
_PART_PREFIX = "PART-"

#: Parts held at zero in the warehouse, so `ReasonCode.PARTS_UNAVAILABLE` has a case to fire on. A
#: 16-port ODP closure is a slow-moving item, which is why this one and not a drop cable.
_OUT_OF_STOCK = frozenset({"PART-ODP-16PORT"})

#: `change_kind` values that move stock, and therefore require a `PART-*` target. Ours.
_PART_CHANGE_KINDS = frozenset({"parts_reservation", "parts_consumption", "parts_release"})

#: Actions that change a device, refused here for the same reason the TMF adapter refuses them: a
#: mis-routed reboot must fail loudly rather than be filed as an inventory correction.
_DEVICE_ACTIONS = frozenset(
    {
        ActionType.CPE_REBOOT,
        ActionType.CPE_RESYNC,
        ActionType.CPE_FIRMWARE_UPDATE,
        ActionType.CPE_FACTORY_RESET,
        ActionType.WIFI_CHANNEL_CHANGE,
        ActionType.WIFI_POWER_CHANGE,
        ActionType.NODE_LEVEL_RESET,
        ActionType.OLT_PORT_RESET,
        ActionType.BULK_CONFIG_PUSH,
    }
)


class SimulatedInventoryAdapter(SimulatedAdapterBase):
    """Plant and parts records, recent-change correlation, and the record-update write."""

    system_name = "inventory"
    external_ref_prefix = "INVCHG"

    def __init__(self, fixtures: Fixtures, clock: Clock, gate: WriteGate) -> None:
        super().__init__(fixtures, clock, gate)
        # part_ref -> the crews that carry it. Built from the crew fixtures, which are the only
        # place part references are authored; a second hand-written catalogue here would drift from
        # them the first time a crew's van list changed.
        catalogue: dict[str, list[str]] = {}
        for crew in fixtures.crews:
            for part_ref in crew["carried_parts"]:
                catalogue.setdefault(str(part_ref), []).append(str(crew["crew_id"]))
        self._part_carriers = catalogue
        # part_ref -> units reserved by writes made in this process. Reservations are a running
        # total rather than a fixture, because the point of a reservation is that it changes.
        self._reserved: dict[str, int] = {}

    # -- reads -----------------------------------------------------------------------------------

    async def fetch_plant_object(self, object_ref: str) -> dict[str, Any]:
        """One plant object or part. **Existence read**: an unknown reference is `found: False`.

        See the module docstring for why this one does not raise. `record_confidence` is here
        because an as-built record that was last audited in 2021 is not the same evidence as one
        audited this year, and a workflow that treats them alike will dispatch a crew to a tap that
        was moved.
        """
        self._ensure_available()
        if object_ref.startswith(_PART_PREFIX):
            return self._part_record(object_ref)

        record = self._fixtures.plant_object(object_ref)
        if record is None:
            return {
                "object_ref": object_ref,
                "found": False,
                "object_kind": self._kind_from_ref(object_ref),
                "data_available": False,
                "data_quality_notes": [
                    "reference not present in the simulated inventory; it may be a valid object "
                    "this fixture set does not model, or a stale reference in an alarm"
                ],
                **self._provenance(object_ref),
            }

        kind = self._kind_of(object_ref)
        last_audit = record.get("last_audit_year")
        payload: dict[str, Any] = {
            "object_ref": object_ref,
            "found": True,
            "object_kind": kind,
            "archetype": self._archetype_of(record),
            "latitude": record.get("latitude"),
            "longitude": record.get("longitude"),
            "parent_ref": self._parent_of(kind, record),
            "housing": record.get("housing"),
            "ports": record.get("ports"),
            "commissioned_year": record.get("commissioned_year"),
            "last_audit_year": last_audit,
            # Ours, and the kind of judgement a real inventory does not usually expose: how far a
            # planner should trust these coordinates. Derived from audit recency so it is stable.
            "record_confidence": self._record_confidence(last_audit),
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(object_ref),
        }
        if kind == "service":
            payload["service_detail"] = {
                "technology": record["technology"],
                "delimiter_ref": record["delimiter_ref"],
                "delimiter_port": record["delimiter_port"],
                "node_ref": record.get("node_ref"),
                "pon_port_ref": record.get("pon_port_ref"),
            }
        return payload

    def _part_record(self, part_ref: str) -> dict[str, Any]:
        """Stock levels for a consumable. Unknown part refs are `found: False`, as above."""
        carriers = self._part_carriers.get(part_ref)
        if carriers is None:
            return {
                "object_ref": part_ref,
                "found": False,
                "object_kind": "part",
                "data_available": False,
                "data_quality_notes": [
                    "no such part reference; the simulated catalogue is exactly the set of parts "
                    "some crew carries"
                ],
                **self._provenance(part_ref),
            }
        # Deterministic per part, so a scenario that asserts "parts available" keeps asserting it.
        on_hand = 0 if part_ref in _OUT_OF_STOCK else 4 + int(unit(part_ref, "stock") * 26)
        reserved = self._reserved.get(part_ref, 0)
        available = max(on_hand - reserved, 0)
        return {
            "object_ref": part_ref,
            "found": True,
            "object_kind": "part",
            "warehouse_quantity": on_hand,
            "reserved_quantity": reserved,
            "available_quantity": available,
            # Van stock is a *capability*, not a count: the fixtures say which crews carry the part,
            # not how many they have. Inventing a per-van count would imply a stock feed we do not
            # have -- gap INV-4.
            "crews_carrying": list(carriers),
            "van_stock_counted": False,
            "lead_time_days": 0 if available else 5,
            "data_available": True,
            "data_quality_notes": (
                [] if available else ["zero available; a visit needing this part will stall"]
            ),
            **self._provenance(part_ref),
        }

    async def fetch_recent_changes(
        self, *, object_refs: list[str], since: datetime
    ) -> list[dict[str, Any]]:
        """Changes to any of `object_refs` after `since`. **Collection query**: `[]` when none.

        Matching is on the change's own `object_refs`, which name every object the work touched
        rather than only its headline target. A change filed against an amplifier that also
        realigned the node has to be findable from either reference, because the workflow arrives
        holding whichever one the alarm named.
        """
        self._ensure_available()
        wanted = set(object_refs)
        out: list[dict[str, Any]] = []
        for change in self._fixtures.plant_changes:
            touched = [ref for ref in change["object_refs"] if ref in wanted]
            if not touched:
                continue
            changed_at = self._offset_hours(float(change["changed_offset_hours"]))
            if datetime.fromisoformat(changed_at) < since:
                continue
            out.append(
                {
                    "change_ref": change["change_ref"],
                    "change_type": change["change_type"],
                    "changed_at": changed_at,
                    "changed_by": change["changed_by"],
                    "work_order_ref": change["work_order_ref"],
                    "description": change["description"],
                    "matched_object_refs": touched,
                    "all_object_refs": list(change["object_refs"]),
                    "hours_ago": round(-float(change["changed_offset_hours"]), 1),
                    "data_available": True,
                    "data_quality_notes": [],
                    **self._provenance(str(change["change_ref"])),
                }
            )
        out.sort(key=lambda c: str(c["changed_at"]), reverse=True)
        return out

    # -- write -----------------------------------------------------------------------------------

    async def update_plant_object(self, request: ActionRequest) -> dict[str, Any]:
        """Correct a record, or reserve/consume/release a part. Goes through the gate; no I/O.

        There is no `ActionType` that means "correct the inventory record", and one was not
        invented: `ActionType` is the key the policy pack matches on, so a member with no rule
        behind it makes the engine fail closed on a legitimate action. The intent therefore travels
        in `request.parameters["change_kind"]`, which is honest about being a local convention --
        gap INV-5 is exactly "what does a real inventory write look like, and what authorises it".
        """
        if request.action_type in _DEVICE_ACTIONS:
            raise AdapterError(
                self.system_name,
                f"{request.action_type.value} changes a device and belongs to the CPE adapter, "
                "not the inventory record store",
                retryable=False,
            )
        change_kind = str(request.parameters.get("change_kind", "record_correction"))
        target = request.target_ref
        is_part = target.startswith(_PART_PREFIX)
        if is_part and change_kind == "record_correction":
            change_kind = "parts_reservation"
        if not is_part and change_kind in _PART_CHANGE_KINDS:
            raise AdapterError(
                self.system_name,
                f"change_kind {change_kind!r} needs a {_PART_PREFIX}* target_ref, got {target!r}",
                retryable=False,
            )

        extra: dict[str, Any] = {"change_kind": change_kind, "object_ref": target}
        movement = 0
        if is_part:
            quantity = int(request.parameters.get("quantity", 1))
            stock = self._part_record(target)
            if not stock["found"]:
                raise AdapterError(
                    self.system_name, f"unknown part reference {target!r}", retryable=False
                )
            movement = {
                "parts_reservation": quantity,
                "parts_consumption": quantity,
                "parts_release": -quantity,
            }.get(change_kind, 0)
            fulfilled = movement <= 0 or int(stock["available_quantity"]) >= quantity
            extra.update(
                {
                    "quantity": quantity,
                    "fulfilled": fulfilled,
                    "available_before": stock["available_quantity"],
                    # Projected, not applied: the ledger check inside `simulate_write` has not run
                    # yet, so nothing may be mutated on this line. See below.
                    "available_after": (
                        int(stock["available_quantity"]) - movement
                        if fulfilled
                        else stock["available_quantity"]
                    ),
                    "crew_id": request.parameters.get("crew_id"),
                    "shortfall": max(quantity - int(stock["available_quantity"]), 0),
                }
            )
            if not fulfilled:
                movement = 0
        else:
            extra["fields"] = dict(request.parameters.get("fields", {}))
            extra["as_built_verified"] = bool(request.parameters.get("as_built_verified", False))
            extra["fulfilled"] = True

        detail = (
            f"{change_kind} recorded against {target}"
            if extra["fulfilled"]
            else f"{change_kind} against {target} not fulfilled: insufficient stock"
        )
        result = self.simulate_write(request, detail=detail, extra=extra)
        # The stock movement is applied *after* the write path has decided whether this is a new
        # effect. Applying it while building `extra` would decrement twice for a replayed key --
        # which is the exact bug the idempotency ledger exists to prevent, moved one line earlier.
        if movement and not result["replayed"] and result["simulated"]:
            self._reserved[target] = max(self._reserved.get(target, 0) + movement, 0)
        return result

    # -- helpers ---------------------------------------------------------------------------------

    def _kind_of(self, object_ref: str) -> str:
        if object_ref in self._fixtures.hfc_nodes:
            return "hfc_node"
        if object_ref in self._fixtures.olts:
            return "olt"
        if object_ref in self._fixtures.taps:
            return "tap"
        if object_ref in self._fixtures.odps:
            return "odp"
        return "service"

    @staticmethod
    def _kind_from_ref(object_ref: str) -> str:
        """A guess at what an unknown reference was meant to be, from its prefix.

        A guess, and labelled as one by living on a `found: False` payload: it exists so a
        data-quality note can say "an amplifier reference we do not model" rather than "unknown".
        """
        prefixes = {
            "AMP-": "amplifier",
            "TAP-": "tap",
            "ODP-": "odp",
            "OLT-": "olt",
            "PON-": "pon_port",
            "SPL-": "splitter",
            "SVC-": "service",
            "HFC-NODE-": "hfc_node",
            "CCAP-": "cmts",
        }
        for prefix, kind in prefixes.items():
            if object_ref.startswith(prefix):
                return kind
        return "unknown"

    def _archetype_of(self, record: dict[str, Any]) -> str | None:
        archetype = record.get("archetype")
        if archetype is not None:
            return str(archetype)
        # Taps and ODPs do not carry an archetype; their parent does.
        parent = record.get("node_ref") or record.get("olt_ref")
        parent_record = (
            self._fixtures.hfc_nodes.get(str(parent)) or self._fixtures.olts.get(str(parent))
            if parent
            else None
        )
        return None if parent_record is None else str(parent_record["archetype"])

    @staticmethod
    def _parent_of(kind: str, record: dict[str, Any]) -> str | None:
        if kind in {"tap", "odp"}:
            return str(record.get("node_ref") or record.get("olt_ref"))
        if kind == "service":
            return str(record["delimiter_ref"])
        if kind == "hfc_node":
            return str(record["headend_ref"])
        if kind == "olt":
            return str(record["headend_ref"])
        return None

    def _record_confidence(self, last_audit_year: int | None) -> str:
        """How far to trust this record, from audit recency. Bands are ours -- gap INV-2."""
        if last_audit_year is None:
            return "unaudited"
        years = self._clock.now().year - int(last_audit_year)
        if years <= 1:
            return "high"
        return "medium" if years <= 3 else "low"
