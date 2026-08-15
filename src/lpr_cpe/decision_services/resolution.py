"""What could be tried, for a fault in a given domain, in the order it should be tried.

The catalogue below is the deterministic half of resolution planning: for each fault domain, the
actions that address a fault *there*, each with a success probability and a disruption cost. The
language model does not author this list. It may write the customer-facing wording around a chosen
option, and `ResolutionOption.rationale` is where that goes, but the set of options and their
ordering are a function of the fault domain and the policy pack.

Two rules the catalogue exists to enforce:

**An option only appears if the pack allows it.** `remote_actions` is an exhaustive allowlist and
`bulk_config_push` is in it with `allowed: false`. Offering a blocked action produces a plan whose
top-ranked entry the policy engine will refuse -- the incident spends a cycle discovering that, and
an operator reading the plan sees a choice that was never available. So the allowlist filters the
catalogue, and the filtered entries are named in `ResolutionPlan.notes` rather than vanishing.

**Ordering is `ResolutionOption.rank_key`, which is not here.** It weighs success against customer
disruption and lives on the model, so the plan, the API and any node that re-sorts all agree. This
module's job is to fill the fields that key reads honestly: a factory reset really is 0.9
disruptive, and understating that is how it ranks above a channel change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from lpr_cpe.decision_services.blast_radius import action_radius
from lpr_cpe.domain.enums import ActionType, FaultDomain
from lpr_cpe.domain.records import TopologyContext
from lpr_cpe.domain.resolution import ResolutionOption, ResolutionPlan
from lpr_cpe.policies.models import ActionRule, BlastRadiusPolicy


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One catalogue entry, before the pack and the topology have had their say."""

    action_type: ActionType
    label: str
    success: float
    disruption: float
    minutes: int = 5
    reversible: bool = True
    customer_present: bool = False
    truck_roll: bool = False
    rationale: str = ""


#: Remote-first, then guided, then a truck. Within each domain the entries are written in the order
#: an experienced operator would try them, but that order is *not* what the plan uses --
#: `ResolutionOption.rank_key` re-sorts on success against disruption. The written order is
#: documentation of intent; the ranking is the behaviour, and where the two disagree the numbers are
#: wrong and should be changed here rather than the sort being bypassed.
#:
#: Success probabilities are estimates, not measurements. Nothing in this repository has observed a
#: reboot fixing 45% of CPE faults -- `docs/vendor-integration-gaps.md` records that a deployment
#: replaces these with its own outcome history, and until then they are ordering weights whose
#: absolute values should not be quoted to anyone.
_CATALOGUE: dict[FaultDomain, tuple[_Candidate, ...]] = {
    FaultDomain.CPE: (
        _Candidate(
            ActionType.CPE_REBOOT,
            "Reboot the CPE",
            success=0.45,
            disruption=0.3,
            minutes=6,
            rationale="clears transient firmware and memory faults; service returns in minutes",
        ),
        _Candidate(
            ActionType.CPE_RESYNC,
            "Resync the CPE to the network",
            success=0.35,
            disruption=0.2,
            minutes=4,
            rationale="re-establishes the modem's session without a full restart",
        ),
        _Candidate(
            ActionType.CPE_FIRMWARE_UPDATE,
            "Update CPE firmware",
            success=0.4,
            disruption=0.5,
            minutes=20,
            reversible=False,
            rationale="addresses known-defective firmware; a failed flash needs a replacement unit",
        ),
        _Candidate(
            ActionType.CPE_FACTORY_RESET,
            "Factory-reset the CPE",
            success=0.5,
            disruption=0.9,
            minutes=15,
            reversible=False,
            rationale=(
                "clears corrupt configuration, and destroys the customer's own Wi-Fi name and "
                "password with it"
            ),
        ),
        _Candidate(
            ActionType.CREATE_WORK_ORDER,
            "Send a Clean Boots technician to replace the CPE",
            success=0.9,
            disruption=0.6,
            minutes=240,
            customer_present=True,
            truck_roll=True,
            rationale="hardware failure that no remote action can address",
        ),
    ),
    FaultDomain.CUSTOMER_ENVIRONMENT: (
        _Candidate(
            ActionType.WIFI_CHANNEL_CHANGE,
            "Move the Wi-Fi to a quieter channel",
            success=0.55,
            disruption=0.15,
            minutes=3,
            rationale="interference from neighbouring networks is the commonest cause here",
        ),
        _Candidate(
            ActionType.WIFI_POWER_CHANGE,
            "Adjust Wi-Fi transmit power",
            success=0.3,
            disruption=0.1,
            minutes=3,
            rationale="widens coverage where clients are far from the gateway",
        ),
        _Candidate(
            ActionType.SEND_SELF_HELP,
            "Guide the customer through repositioning the gateway",
            success=0.4,
            disruption=0.25,
            minutes=45,
            customer_present=True,
            rationale=(
                "placement fixes what no remote setting can, and needs the person who can move it"
            ),
        ),
    ),
    FaultDomain.INSIDE_HOME_WIRING: (
        _Candidate(
            ActionType.SEND_SELF_HELP,
            "Guide the customer through checking their in-home cabling",
            success=0.3,
            disruption=0.3,
            minutes=45,
            customer_present=True,
            rationale="a loose or damaged connector is often visible and fixable by the customer",
        ),
        _Candidate(
            ActionType.CREATE_WORK_ORDER,
            "Send a Clean Boots technician to the premises",
            success=0.85,
            disruption=0.6,
            minutes=240,
            customer_present=True,
            truck_roll=True,
            rationale="in-home wiring is inside the Clean Boots boundary and needs access",
        ),
    ),
    FaultDomain.PROVISIONING: (
        _Candidate(
            ActionType.REPROVISION,
            "Reprovision the service",
            success=0.65,
            disruption=0.35,
            minutes=10,
            rationale="re-pushes the subscriber's configuration from the provisioning system",
        ),
        _Candidate(
            ActionType.PROFILE_CHANGE,
            "Correct the service profile",
            success=0.5,
            disruption=0.3,
            minutes=8,
            rationale="applies the profile the subscription should have had",
        ),
    ),
    FaultDomain.DROP: (
        _Candidate(
            ActionType.CREATE_WORK_ORDER,
            "Send a Clean Boots technician to the drop",
            success=0.85,
            disruption=0.5,
            minutes=240,
            truck_roll=True,
            rationale=(
                "the drop runs from the delimiter to the premises and is the Clean Boots crew's"
            ),
        ),
    ),
    # From the delimiter outward the fault is the plant's, and the action is a maintenance request
    # to OSP rather than a work order. That boundary is the Clean/Dirty Boots handover and it is why
    # `raise_mr` carries the `clean_to_dirty_handover` approval kind in the pack.
    FaultDomain.TAP_OR_ODP: (
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request against the tap or ODP",
            success=0.8,
            disruption=0.4,
            minutes=1440,
            rationale="the delimiter is plant, so OSP owns the repair",
        ),
    ),
    FaultDomain.DISTRIBUTION: (
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request against the distribution plant",
            success=0.8,
            disruption=0.5,
            minutes=1440,
            rationale="an amplifier or distribution leg fault affects every home behind it",
        ),
    ),
    FaultDomain.FEEDER: (
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request against the feeder",
            success=0.8,
            disruption=0.6,
            minutes=1440,
            rationale="feeder faults are plant work and cannot be addressed from the premises",
        ),
    ),
    FaultDomain.NODE_OR_OLT: (
        _Candidate(
            ActionType.OLT_PORT_RESET,
            "Reset the PON port",
            success=0.4,
            disruption=0.8,
            minutes=10,
            reversible=False,
            rationale="clears a wedged port, and drops every subscriber behind it while it runs",
        ),
        _Candidate(
            ActionType.NODE_LEVEL_RESET,
            "Reset the node",
            success=0.4,
            disruption=0.85,
            minutes=15,
            reversible=False,
            rationale="clears a wedged node, and interrupts the whole service group while it runs",
        ),
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request against the node or OLT",
            success=0.85,
            disruption=0.5,
            minutes=1440,
            rationale="a fault that survives a reset is hardware and needs the plant crew",
        ),
    ),
    FaultDomain.HEADEND_OR_CO: (
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request against the headend or central office",
            success=0.85,
            disruption=0.7,
            minutes=1440,
            rationale="headend faults are handled by the platform team, not by field dispatch",
        ),
    ),
    FaultDomain.SERVICE_PLATFORM: (
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request against the service platform",
            success=0.8,
            disruption=0.6,
            minutes=1440,
            rationale=(
                "a platform fault is not fixable per-subscriber and must not be retried "
                "per-subscriber"
            ),
        ),
    ),
    FaultDomain.POWER: (
        _Candidate(
            ActionType.NOTIFY_CUSTOMER,
            "Tell the customer their service is waiting on power",
            success=0.15,
            disruption=0.05,
            minutes=2,
            rationale=(
                "commercial power is not ours to restore; the honest action is to say so rather "
                "than to keep testing a line that has no electricity behind it"
            ),
        ),
        _Candidate(
            ActionType.RAISE_MR,
            "Raise a maintenance request for plant power",
            success=0.7,
            disruption=0.5,
            minutes=1440,
            rationale="power at a plant element is ours, unlike power at the premises",
        ),
    ),
}


def plan_resolution(
    *,
    plan_id: str,
    created_at: datetime,
    fault_domain: FaultDomain,
    target_ref: str,
    allowlist: Mapping[ActionType, ActionRule],
    blast_radius_policy: BlastRadiusPolicy,
    topology: TopologyContext | None = None,
    attempted_option_ids: Sequence[str] = (),
) -> ResolutionPlan:
    """The plan for a fault in `fault_domain`, filtered by the pack's allowlist.

    A domain with no catalogue entry produces an empty plan rather than a guess. `NO_FAULT_FOUND`,
    `MULTIPLE` and `UNKNOWN` are all in that position and all mean something different -- nothing to
    fix, more than one thing to fix, and not yet known what to fix -- but they share the property
    that no single action addresses them. `ResolutionPlan.exhausted` is then true on arrival, which
    is what routes the incident to a human instead of to an action.

    `blast_radius` on each option is `action_radius`, not the incident's impact radius. A reboot
    offered during a node outage carries a blast radius of one, because that is what rebooting one
    modem touches; the outage's size is `ImpactAssessment`'s to report.
    """
    candidates = _CATALOGUE.get(fault_domain, ())
    notes: list[str] = []
    options: list[ResolutionOption] = []

    for candidate in candidates:
        rule = allowlist.get(candidate.action_type)
        if rule is None:
            # The allowlist is exhaustive by construction, so a missing entry is a pack that has
            # fallen behind the `ActionType` enum. Fail closed and say which action, because the
            # symptom otherwise is an option that silently stops being offered.
            notes.append(
                f"{candidate.action_type.value} is not in the policy pack's allowlist at all, so "
                "it was not offered; the pack is missing a row the code expects"
            )
            continue
        if not rule.allowed:
            notes.append(
                f"{candidate.action_type.value} addresses this fault domain but the policy pack "
                "does not allow it, so it is not offered"
            )
            continue
        radius = action_radius(topology, action=candidate.action_type, policy=blast_radius_policy)
        options.append(
            ResolutionOption(
                option_id=f"{plan_id}-{candidate.action_type.value}",
                action_type=candidate.action_type,
                target_ref=target_ref,
                label=candidate.label,
                addresses_domain=fault_domain,
                estimated_success_probability=candidate.success,
                estimated_duration=timedelta(minutes=candidate.minutes),
                customer_disruption=candidate.disruption,
                reversible=candidate.reversible,
                requires_customer_present=candidate.customer_present,
                requires_truck_roll=candidate.truck_roll,
                blast_radius=radius.count,
                rationale=candidate.rationale,
            )
        )

    if not candidates:
        notes.append(
            f"no catalogued action addresses {fault_domain.value}, so this plan is empty rather "
            "than speculative"
        )

    return ResolutionPlan(
        plan_id=plan_id,
        created_at=created_at,
        fault_domain=fault_domain,
        options=options,
        attempted_option_ids=list(attempted_option_ids),
        escalation_path=_escalation_path(fault_domain),
        notes=notes,
    )


#: What to do when every option has been tried. Named steps rather than free text because
#: `ResolutionPlan.escalation_path` is read by the escalation node, and a human-written sentence
#: there would have to be parsed by something.
_ESCALATION: dict[FaultDomain, tuple[str, ...]] = {
    FaultDomain.CPE: ("create_work_order", "field_engineering_review"),
    FaultDomain.CUSTOMER_ENVIRONMENT: ("create_work_order", "field_engineering_review"),
    FaultDomain.INSIDE_HOME_WIRING: ("create_work_order", "field_engineering_review"),
    FaultDomain.PROVISIONING: ("provisioning_team", "service_platform_review"),
    FaultDomain.DROP: ("raise_mr", "osp_review"),
    FaultDomain.TAP_OR_ODP: ("osp_review", "noc_supervisor"),
    FaultDomain.DISTRIBUTION: ("osp_review", "noc_supervisor"),
    FaultDomain.FEEDER: ("osp_review", "noc_supervisor"),
    FaultDomain.NODE_OR_OLT: ("noc_supervisor", "network_operations"),
    FaultDomain.HEADEND_OR_CO: ("network_operations",),
    FaultDomain.SERVICE_PLATFORM: ("service_platform_review", "network_operations"),
    FaultDomain.POWER: ("noc_supervisor",),
}


def _escalation_path(fault_domain: FaultDomain) -> list[str]:
    """Where an exhausted plan goes. `noc_supervisor` for anything uncatalogued, never nowhere."""
    return list(_ESCALATION.get(fault_domain, ("noc_supervisor",)))


__all__ = ["plan_resolution"]
