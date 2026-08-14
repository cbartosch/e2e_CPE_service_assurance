"""Fixture-backed jTrack: LPR's maintenance-request system of record.

jTrack is named in the specification but no schema, endpoint or authentication scheme was supplied
(A1/A2, gap JTRACK-1). `MRStatus` is ours -- it is in `domain.enums` because the workflow reasons
about states, not because a vendor confirmed these nine. Every other field name here is ours too.

The handover packet is the reason this adapter exists. Clean-to-Dirty handover is the point the
specification calls out as the one that fails in practice: a Clean crew confirms plant, raises an
MR, and OSP rejects it days later for missing evidence. So `create_mr` **refuses to file an
incomplete MR** rather than filing it and letting the rejection arrive later. A required field that
is checked at submission is a rejection that never happens; the same field checked by a human three
days later is the stall the reconciliation stage was built to detect.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.domain.enums import ActionType, MRStatus
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterError
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase

#: What an MR must carry to be accepted. Ours, and the single most consequential invention in this
#: file: a real jTrack has its own mandatory set and it will not be this one -- gap JTRACK-2.
#:
#: Each entry is here because its absence is a rejection reason a Dirty crew would actually give:
#: without a plant object there is nothing to send anyone to, without evidence there is no reason to
#: believe the fault is in the plant, and without an access note a crew arrives at a locked riser.
REQUIRED_MR_FIELDS: tuple[str, ...] = (
    "plant_object_ref",
    "fault_description",
    "evidence_refs",
    "access_notes",
)

#: MR states from which an update is meaningful. Updating a closed MR is a no-op in every ticketing
#: system worth using, and silently accepting it would let the workflow believe it had reopened one.
_MUTABLE_STATES = frozenset(
    {
        MRStatus.DRAFT,
        MRStatus.SUBMITTED,
        MRStatus.ACCEPTED,
        MRStatus.IN_PROGRESS,
        MRStatus.PLANNED,
    }
)


class SimulatedJTrackAdapter(SimulatedAdapterBase):
    """MR create/update, plus the reads reconciliation and duplicate-suppression need."""

    system_name = "jtrack"
    external_ref_prefix = "MR"

    # -- writes ----------------------------------------------------------------------------------

    async def create_mr(self, request: ActionRequest) -> dict[str, Any]:
        """File a maintenance request. Goes through the gate; never performs I/O.

        Raises a **non-retryable** `AdapterError` when the handover packet is incomplete, and
        non-retryable matters: a missing evidence reference is not a transient condition, and
        `with_retry` would otherwise send the same deficient MR three more times before giving up.
        """
        if request.action_type is not ActionType.RAISE_MR:
            raise AdapterError(
                self.system_name,
                f"create_mr requires action_type=raise_mr, got {request.action_type.value}",
                retryable=False,
            )
        missing = self.missing_handover_fields(request.parameters)
        if missing:
            raise AdapterError(
                self.system_name,
                "MR rejected before submission, incomplete handover packet: missing "
                + ", ".join(missing),
                retryable=False,
            )
        params = request.parameters
        return self.simulate_write(
            request,
            detail=(
                f"MR filed against {params['plant_object_ref']} for incident {request.incident_id}"
            ),
            extra={
                # SUBMITTED, not ACCEPTED. The distinction is the whole point: filing an MR is our
                # act, accepting it is OSP's, and a simulator that reported ACCEPTED would hide the
                # silent stall between the two.
                "status": MRStatus.SUBMITTED.value,
                "plant_object_ref": params["plant_object_ref"],
                "fault_description": params["fault_description"],
                "evidence_refs": list(params["evidence_refs"]),
                "access_notes": params["access_notes"],
                "crew_type_required": params.get("crew_type_required", "dirty"),
                "priority": params.get("priority", "standard"),
                "homes_affected": params.get("homes_affected"),
                "suspected_fault_class": params.get("suspected_fault_class"),
                "raised_by_crew_id": params.get("raised_by_crew_id"),
                "handover_complete": True,
                "accepted_at": None,
                "update_count": 0,
                "history": [],
            },
        )

    async def update_mr(self, request: ActionRequest) -> dict[str, Any]:
        """Append to an existing MR. Goes through the gate; never performs I/O.

        Append, not overwrite. An MR is a conversation between two crews and the audit question is
        "what did we tell OSP and when", which a field that gets replaced cannot answer. The update
        carries its own idempotency key so it is not mistaken for a replay of the creation.
        """
        if request.action_type is not ActionType.UPDATE_MR:
            raise AdapterError(
                self.system_name,
                f"update_mr requires action_type=update_mr, got {request.action_type.value}",
                retryable=False,
            )
        mr_ref = str(request.parameters.get("mr_ref") or request.target_ref)
        stored = self._stored_mr(mr_ref)
        if stored is None:
            raise AdapterError(
                self.system_name,
                f"no simulated MR {mr_ref!r} to update; simulated MRs exist only for MRs this "
                "process filed",
                retryable=False,
            )
        current = MRStatus(str(stored["status"]))
        if current not in _MUTABLE_STATES:
            raise AdapterError(
                self.system_name,
                f"MR {mr_ref} is {current.value}; an update would not be applied",
                retryable=False,
            )
        new_status = self._requested_status(request, current)
        note = str(request.parameters.get("note", request.reason_code.value))
        result = self.simulate_write(
            request,
            detail=f"MR {mr_ref} updated to {new_status.value}",
            extra={
                "status": new_status.value,
                "mr_ref": mr_ref,
                "note": note,
                "added_evidence_refs": list(request.parameters.get("evidence_refs", [])),
            },
        )
        if not result["replayed"] and result["simulated"]:
            # Reflect onto the MR the create call recorded, so `fetch_mr` and `fetch_open_mrs` agree
            # with what the update said. Two records of one MR is the reconciliation bug itself.
            stored["status"] = new_status.value
            stored["update_count"] = int(stored.get("update_count", 0)) + 1
            history = list(stored.get("history", []))
            history.append(
                {
                    "at": result["recorded_at"],
                    "status": new_status.value,
                    "note": note,
                    "reason_code": request.reason_code.value,
                    "idempotency_key": request.idempotency_key,
                }
            )
            stored["history"] = history
            if new_status is MRStatus.ACCEPTED and stored.get("accepted_at") is None:
                stored["accepted_at"] = result["recorded_at"]
            for ref in request.parameters.get("evidence_refs", []):
                if ref not in stored["evidence_refs"]:
                    stored["evidence_refs"].append(ref)
        return result

    # -- reads -----------------------------------------------------------------------------------

    async def fetch_mr(self, mr_ref: str) -> dict[str, Any]:
        """One MR. **Record read**: an unknown reference is `{"found": False}`, not an exception.

        In simulation an MR exists only if `create_mr` ran in this process, so a miss is expected
        after a restart rather than evidence that jTrack and the workflow disagree.
        """
        self._ensure_available()
        stored = self._stored_mr(mr_ref)
        if stored is None:
            return {
                "mr_ref": mr_ref,
                "found": False,
                "data_available": False,
                "data_quality_notes": [
                    "no simulated MR with this reference; simulated MRs exist only for writes made "
                    "by this process"
                ],
                **self._provenance(mr_ref),
            }
        return self._mr_view(stored)

    async def fetch_open_mrs(self, plant_object_ref: str) -> list[dict[str, Any]]:
        """Every open MR against one plant object. **Collection query**: `[]` when there are none.

        This is the duplicate-suppression read. Two Clean crews confirming the same tap fault on the
        same afternoon must not produce two MRs, and the only way to know is to ask before filing --
        which is why this returns the *open* ones and not the whole history.
        """
        self._ensure_available()
        out = [
            self._mr_view(stored)
            for stored in self._ledger.values()
            if stored.get("plant_object_ref") == plant_object_ref
            and MRStatus(str(stored["status"])) in _MUTABLE_STATES
        ]
        out.sort(key=lambda m: str(m["created_at"]), reverse=True)
        return out

    # -- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def missing_handover_fields(parameters: dict[str, Any]) -> list[str]:
        """Which of `REQUIRED_MR_FIELDS` are absent or empty.

        Public because the handover stage should be able to tell an operator what is missing
        *before* it builds an `ActionRequest`, and it should not do that by catching the exception
        this adapter raises.
        """
        return [field for field in REQUIRED_MR_FIELDS if not parameters.get(field)]

    def _stored_mr(self, mr_ref: str) -> dict[str, Any] | None:
        """The ledger entry for a created MR, found by the reference it was issued.

        Only entries created by `create_mr` qualify: an `update_mr` entry also carries `mr_ref`, and
        matching on that would let an update be mistaken for the MR it updated.
        """
        for stored in self._ledger.values():
            if stored.get("external_ref") == mr_ref and "plant_object_ref" in stored:
                return stored
        return None

    def _mr_view(self, stored: dict[str, Any]) -> dict[str, Any]:
        status = MRStatus(str(stored["status"]))
        return {
            "mr_ref": stored["external_ref"],
            "found": True,
            "status": status.value,
            "open": status in _MUTABLE_STATES,
            # SUBMITTED and never accepted is the state reconciliation is looking for; naming it
            # here saves every caller re-deriving it and getting the boundary wrong.
            "awaiting_acceptance": status is MRStatus.SUBMITTED,
            "plant_object_ref": stored["plant_object_ref"],
            "fault_description": stored["fault_description"],
            "evidence_refs": list(stored["evidence_refs"]),
            "access_notes": stored["access_notes"],
            "crew_type_required": stored["crew_type_required"],
            "priority": stored["priority"],
            "homes_affected": stored.get("homes_affected"),
            "suspected_fault_class": stored.get("suspected_fault_class"),
            "raised_by_crew_id": stored.get("raised_by_crew_id"),
            "incident_id": stored["incident_id"],
            "created_at": stored["recorded_at"],
            "accepted_at": stored.get("accepted_at"),
            "update_count": int(stored.get("update_count", 0)),
            "history": list(stored.get("history", [])),
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(str(stored["external_ref"])),
        }

    @staticmethod
    def _requested_status(request: ActionRequest, current: MRStatus) -> MRStatus:
        """The status an update asks for, defaulting to leaving it alone.

        An unknown status string raises rather than being coerced to `IN_PROGRESS`: a typo'd state
        that silently becomes a plausible one is how an MR ends up reported as progressing when
        nobody has touched it.
        """
        requested = request.parameters.get("status")
        if requested is None:
            return current
        try:
            return MRStatus(str(requested))
        except ValueError:
            raise AdapterError(
                "jtrack",
                f"{requested!r} is not an MRStatus; valid values are "
                + ", ".join(s.value for s in MRStatus),
                retryable=False,
            ) from None
