"""Fixture-backed CRM / ITSM / TMF-aligned record store.

No CRM or ITSM API was supplied (A1/A2). The field names below are loosely TM Forum-*flavoured*
(`serviceSpecification`, `relatedParty`) but they are **not** conformant TMF621/TMF641 payloads and
must not be treated as such -- inventing a half-correct TMF body is worse than an obviously local
one, because it looks like it would validate. Gaps TMF-1 to TMF-4.

Customer detail is minimised at the boundary, not later: `fetch_customer` returns a contact
*channel preference* and a language, never a name, address, phone number or email. The workflow's
only legitimate uses are "which channel may we contact them on" and "which language", and a payload
that also carried a name would end up in an approval context and an audit event by accident.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.domain.enums import ActionType
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterError, AdapterUnavailableError
from lpr_cpe.simulation.fixtures.determinism import unit
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase

#: SLA targets by product tier, as (response hours, restore hours). Ours, and a policy question a
#: real deployment answers from contracts rather than from code -- gap TMF-3.
_SLA_TARGETS: dict[str, tuple[float, float]] = {
    "residential": (4.0, 24.0),
    "business": (1.0, 8.0),
}

#: Actions that change a device rather than a record. Refused here so a mis-routed request fails
#: loudly instead of filing a problem record that says a reboot happened in the CRM.
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


class SimulatedTMFAdapter(SimulatedAdapterBase):
    """Customer, service and SLA reads, plus the service-problem upsert."""

    system_name = "tmf"
    external_ref_prefix = "SP"

    # -- reads -----------------------------------------------------------------------------------

    async def fetch_customer(self, customer_ref: str) -> dict[str, Any]:
        """Contact preferences and protection flags. **Subject read**: unknown ref raises.

        No name, address, phone or email -- see the module docstring. `vulnerable_customer` is here
        rather than derived downstream because it is a CRM-held fact, and the policy that protects
        those customers has to read it from the system of record instead of inferring it.
        """
        self._ensure_available()
        service_ref = self._fixtures.service_by_customer.get(customer_ref)
        if service_ref is None:
            raise AdapterUnavailableError(
                self.system_name, f"unknown customer_ref {customer_ref!r}"
            )
        service = self._fixtures.services[service_ref]
        return {
            "customer_ref": customer_ref,
            "service_refs": [service_ref],
            "account_status": "active",
            "language": service["language"],
            "preferred_channels": (
                ["sms", "email"] if service["language"] == "en" else ["sms", "app"]
            ),
            "contactable": True,
            "vulnerable_customer": bool(service["vulnerable_customer"]),
            "priority_customer": bool(service["priority_customer"]),
            "tenure_months": 6 + int(unit(customer_ref, "tenure") * 90),
            "open_ticket_refs": [],
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(customer_ref),
        }

    async def fetch_service(self, service_ref: str) -> dict[str, Any]:
        """The service record. **Subject read**: unknown ref raises.

        This is the adapter that owns `technology`. Every other adapter branches on it, and taking
        it from the CRM record rather than guessing from a reference prefix is what keeps the
        HFC/PON fork correct for a service that has been migrated between them.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        return {
            "service_ref": service_ref,
            "customer_ref": service["customer_ref"],
            "cpe_ref": service["cpe_ref"],
            "serviceSpecification": service["product_name"],
            "technology": service["technology"],
            "state": "active",
            "product_tier": service["product_tier"],
            "downstream_speed_mbps": service["downstream_speed_mbps"],
            "area_archetype": service["archetype"],
            "area_ref": service["area_ref"],
            "delimiter_ref": service["delimiter_ref"],
            "mdu_ref": service["mdu_ref"],
            "activated_days_ago": service["activated_days_ago"],
            "activated_at": self._offset_hours(-24.0 * float(service["activated_days_ago"])),
            # Under the post-install baseline interval, so this service is a legitimate candidate
            # for a baseline scan rather than a recurring one.
            "post_install_baseline_done": int(service["activated_days_ago"]) > 14,
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    async def fetch_sla(self, service_ref: str) -> dict[str, Any]:
        """SLA targets in hours. **Subject read**: unknown ref raises.

        Returns targets only, never a deadline. `SLAContext` derives deadlines from one
        `clock_started_at` written at intake; an adapter that also returned a deadline would create
        a second copy of the one clock, and the two would disagree the first time an incident
        paused.
        """
        self._ensure_available()
        service = self._fixtures.service(service_ref, system=self.system_name)
        tier = str(service["product_tier"])
        response_hours, restore_hours = _SLA_TARGETS[tier]
        return {
            "service_ref": service_ref,
            "sla_ref": f"SLA-{tier.upper()}-{'BUS' if tier == 'business' else 'RES'}",
            "product_tier": tier,
            "response_target_hours": response_hours,
            "restore_target_hours": restore_hours,
            "business_hours_only": False,
            "vulnerable_customer": bool(service["vulnerable_customer"]),
            "priority_customer": bool(service["priority_customer"]),
            "credit_at_risk": tier == "business",
            "data_available": True,
            "data_quality_notes": [],
            **self._provenance(service_ref),
        }

    # -- write -----------------------------------------------------------------------------------

    async def upsert_service_problem(self, request: ActionRequest) -> dict[str, Any]:
        """Create or update the service-problem record. Goes through the gate; never performs I/O.

        Upsert rather than create, and keyed on the idempotency key: the specification requires one
        incident for the life of the fault, so a second call for the same key must update the same
        record instead of opening a sibling problem that closure would then have to reconcile.
        """
        if request.action_type in _DEVICE_ACTIONS:
            raise AdapterError(
                self.system_name,
                f"{request.action_type.value} is a device action and belongs to the CPE adapter, "
                "not the problem-record store",
                retryable=False,
            )
        return self.simulate_write(
            request,
            detail=(
                f"service problem upserted for incident {request.incident_id} "
                f"on {request.target_ref}"
            ),
            extra={
                "status": "open",
                "problem_kind": request.action_type.value,
                "related_party_ref": None,
                "linked_incident_id": request.incident_id,
                "correlation_id": request.correlation_id,
            },
        )
