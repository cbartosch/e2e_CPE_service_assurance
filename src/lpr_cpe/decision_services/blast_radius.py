"""How many customers a fault reaches, and how many an action would reach.

The specification calls both of these the blast radius. They are different numbers about different
events, and this module keeps them apart because collapsing them produces a specific bad outcome: a
single CPE reboot, requested while the customer sits inside a 500-home node outage, would be gated
as a network action. It affects one modem. `blast_radius.network_action_threshold` would stop it,
an operator would be paged to approve a reboot, and the one cheap thing that might have helped that
customer would wait for a human who is already busy with the outage.

So there are two entry points, and neither can be called with the other's input:

* `impact_radius(...)` takes a `FaultDomain` and answers "how many customers does this fault
  affect". It feeds `ImpactAssessment`, severity and dispatch priority.
* `action_radius(...)` takes an `ActionType` and answers "how many customers would this action
  touch". It feeds `PolicyInput.blast_radius`, which the policy engine compares against the
  threshold and the per-action `max_blast_radius` cap.

Both return a `BlastRadius`, which carries its own provenance. That is the second thing this module
exists for. `TopologyContext.homes_behind_delimiter` is nullable and `decision_services.delimiter`
deliberately leaves it null rather than substituting the configured tap size, precisely so that the
substitution happens *here* and is visible when it happens: `ImpactAssessment.count_is_estimated`
and `estimation_basis` are filled from `BlastRadius.measured` and `.basis`, and a `BlastRadius`
cannot be constructed without a basis at all.

What is *not* here: the comparison against `network_action_threshold`. `policies.engine` owns that,
already applies it, and reports it with a reason code and a rule name. A second implementation here
would be a second answer to "is this a network action" that no test would notice had diverged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from lpr_cpe.domain.enums import ActionType, DelimiterKind, FaultDomain, Technology
from lpr_cpe.domain.records import TopologyContext
from lpr_cpe.policies.models import BlastRadiusPolicy


class BlastRadiusScope(StrEnum):
    """The plant element a count is a count of.

    Five members, one per number in `BlastRadiusPolicy`. That correspondence is the constraint: a
    scope the pack cannot size would have to be sized by a literal in this file, which is how a
    threshold ends up outside the policy pack and stops being reviewable.

    Lives here rather than in `domain.enums` because `ImpactAssessment.blast_radius_scope` is typed
    `str` and no domain model refers to this type. Promoting it would put a name in the domain
    package that the domain package does not use.
    """

    SINGLE_PREMISES = "single_premises"
    DELIMITER = "delimiter"
    DISTRIBUTION = "distribution"
    NODE_OR_PORT = "node_or_port"
    HEADEND_OR_OLT = "headend_or_olt"

    def rank(self) -> int:
        """Position in the outward nesting, 0 at the premises. Alphabetical order is not this."""
        return _SCOPE_ORDER.index(self)


_SCOPE_ORDER: tuple[BlastRadiusScope, ...] = (
    BlastRadiusScope.SINGLE_PREMISES,
    BlastRadiusScope.DELIMITER,
    BlastRadiusScope.DISTRIBUTION,
    BlastRadiusScope.NODE_OR_PORT,
    BlastRadiusScope.HEADEND_OR_OLT,
)


@dataclass(frozen=True, slots=True)
class BlastRadius:
    """A count that carries where it came from.

    `basis` is required whether or not the count was measured, which is stricter than
    `ImpactAssessment._estimate_states_its_basis` needs. The reason is that the two numbers become
    indistinguishable the moment either is copied into a report: "8 customers affected" reads the
    same whether inventory counted eight drops or a default said taps hold eight. Requiring the
    sentence at construction means the caller writes it while it is still true.
    """

    count: int
    measured: bool
    basis: str
    scope: BlastRadiusScope
    notes: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError(f"blast radius cannot be negative (got {self.count})")
        if not self.basis.strip():
            raise ValueError(
                "a BlastRadius must state its basis: an unattributed count is indistinguishable "
                "from a measurement once it reaches a report or an approval prompt"
            )

    @property
    def estimated(self) -> bool:
        """`ImpactAssessment.count_is_estimated` reads this. Named for that field, not for us."""
        return not self.measured


# -------------------------------------------------------------------------------------------------
# Scope selection
# -------------------------------------------------------------------------------------------------

#: Where each fault domain places the fault. The domains absent from this mapping fall through to
#: `SINGLE_PREMISES`, and the fall-through is the interesting part rather than an oversight:
#:
#: * `POWER` -- a dark modem and a dark node are both power faults. The domain name carries no
#:   extent at all, so the extent has to come from corroboration (see `impact_radius`), and starting
#:   from one premises means an unattended commercial power cut at one house is not reported as a
#:   node outage.
#: * `UNKNOWN`, `MULTIPLE` -- diagnosis has not concluded, or concluded ambiguously. Widening on
#:   either would let an undiagnosed incident inflate its own severity.
#: * `NO_FAULT_FOUND` -- by definition nothing is affected beyond the one service that reported.
#: * `SERVICE_PLATFORM`, `PROVISIONING` -- both usually *are* wide, and both are wide in a way this
#:   module cannot size: a broken RADIUS realm affects a customer list, not a plant element. Sizing
#:   them off `olt_default` would be a fabricated number with a plausible shape.
_SCOPE_BY_FAULT_DOMAIN: dict[FaultDomain, BlastRadiusScope] = {
    FaultDomain.CPE: BlastRadiusScope.SINGLE_PREMISES,
    FaultDomain.INSIDE_HOME_WIRING: BlastRadiusScope.SINGLE_PREMISES,
    FaultDomain.CUSTOMER_ENVIRONMENT: BlastRadiusScope.SINGLE_PREMISES,
    # A drop serves one premises. It is listed rather than left to the fall-through because it is
    # the domain most often confused with the one below it, and the confusion is expensive: the
    # delimiter is where Clean Boots stop and Dirty Boots start.
    FaultDomain.DROP: BlastRadiusScope.SINGLE_PREMISES,
    FaultDomain.TAP_OR_ODP: BlastRadiusScope.DELIMITER,
    FaultDomain.DISTRIBUTION: BlastRadiusScope.DISTRIBUTION,
    # A feeder sits between the distribution leg and the node, and the pack sizes neither it nor
    # anything between. It takes the larger of its two neighbours: for an impact estimate that
    # overstates severity, and for an action estimate it asks for approval that may not be needed.
    # The opposite error sends a crew to a fault it cannot see the size of.
    FaultDomain.FEEDER: BlastRadiusScope.NODE_OR_PORT,
    FaultDomain.NODE_OR_OLT: BlastRadiusScope.NODE_OR_PORT,
    FaultDomain.HEADEND_OR_CO: BlastRadiusScope.HEADEND_OR_OLT,
}

#: The three actions whose reach is not one premises. Everything else -- every CPE action, every
#: customer contact, every workflow action -- touches the one service it names, which is why the
#: mapping is a three-entry exception list rather than a table of every `ActionType`. A new
#: network-affecting action added to the enum without being added here would be sized as a single
#: premises, so `tests/unit/test_decision_services.py` checks this against the pack's risk classes.
_SCOPE_BY_ACTION: dict[ActionType, BlastRadiusScope] = {
    ActionType.NODE_LEVEL_RESET: BlastRadiusScope.NODE_OR_PORT,
    ActionType.OLT_PORT_RESET: BlastRadiusScope.NODE_OR_PORT,
    ActionType.BULK_CONFIG_PUSH: BlastRadiusScope.HEADEND_OR_OLT,
}


def scope_for_fault_domain(domain: FaultDomain) -> BlastRadiusScope:
    """The plant element a fault in this domain sits on. See `_SCOPE_BY_FAULT_DOMAIN`."""
    return _SCOPE_BY_FAULT_DOMAIN.get(domain, BlastRadiusScope.SINGLE_PREMISES)


def scope_for_action(action: ActionType) -> BlastRadiusScope:
    """The plant element this action reaches. See `_SCOPE_BY_ACTION`."""
    return _SCOPE_BY_ACTION.get(action, BlastRadiusScope.SINGLE_PREMISES)


# -------------------------------------------------------------------------------------------------
# Sizing
# -------------------------------------------------------------------------------------------------


def size_of(
    scope: BlastRadiusScope,
    topology: TopologyContext | None,
    policy: BlastRadiusPolicy,
) -> BlastRadius:
    """How many services sit behind this plant element, measured if the plant records say so.

    The preference order is the same at every scope: a count from inventory, then a structural
    figure that constrains the count, then the pack's default. Only the first is a measurement.

    The middle rung is worth naming because it is easy to mistake for the first. A PON port's
    `split_ratio` is 32 in the fixtures, which is what the splitter can carry, not how many homes
    are lit behind it -- so it produces a better-founded estimate than the pack's default, and is
    still an estimate. Reporting it as measured would put a capacity figure into a report as a
    customer count.
    """
    if scope is BlastRadiusScope.SINGLE_PREMISES:
        return BlastRadius(
            count=1,
            measured=True,
            basis="the fault is at this premises, so one service is affected",
            scope=scope,
        )

    technology = topology.technology if topology is not None else Technology.UNKNOWN
    kind = topology.delimiter_kind if topology is not None else DelimiterKind.UNKNOWN

    if scope is BlastRadiusScope.DELIMITER:
        if topology is not None and topology.homes_behind_delimiter is not None:
            return BlastRadius(
                count=topology.homes_behind_delimiter,
                measured=True,
                basis=(
                    f"plant records list {topology.homes_behind_delimiter} homes behind "
                    f"{topology.delimiter_ref or 'this delimiter'}"
                ),
                scope=scope,
            )
        default = {
            DelimiterKind.TAP: policy.tap_default,
            DelimiterKind.ODP: policy.odp_default,
        }.get(kind, policy.delimiter_default)
        label = kind.value if kind is not DelimiterKind.UNKNOWN else "delimiter"
        return BlastRadius(
            count=default,
            measured=False,
            basis=(
                f"no homes-behind count in plant records; the policy pack's default {label} size "
                f"of {default} was used"
            ),
            scope=scope,
        )

    if scope is BlastRadiusScope.DISTRIBUTION:
        # No field in `TopologyContext` counts a distribution leg, and none should be invented: the
        # HFC chain records which amplifiers are in the path, not how many homes hang off each.
        return BlastRadius(
            count=policy.distribution_default,
            measured=False,
            basis=(
                "plant records do not count homes per distribution leg; the policy pack's default "
                f"of {policy.distribution_default} was used"
            ),
            scope=scope,
        )

    if scope is BlastRadiusScope.NODE_OR_PORT:
        if topology is not None and topology.homes_behind_node_or_port is not None:
            parent = topology.node_ref or topology.pon_port_ref or "this node or port"
            return BlastRadius(
                count=topology.homes_behind_node_or_port,
                measured=True,
                basis=f"plant records list {topology.homes_behind_node_or_port} homes behind "
                f"{parent}",
                scope=scope,
            )
        if technology is Technology.PON and topology is not None and topology.split_ratio:
            return BlastRadius(
                count=topology.split_ratio,
                measured=False,
                basis=(
                    f"no homes-behind count; the port's 1:{topology.split_ratio} split is its "
                    "capacity rather than its occupancy, so this is an upper bound"
                ),
                scope=scope,
            )
        # An unread technology takes the node figure, which is the larger of the two. Understating
        # here understates severity on a real outage; overstating asks for an approval.
        default = policy.pon_port_default if technology is Technology.PON else policy.node_default
        element = "PON port" if technology is Technology.PON else "node"
        return BlastRadius(
            count=default,
            measured=False,
            basis=(
                f"no homes-behind count in plant records; the policy pack's default {element} size "
                f"of {default} was used"
            ),
            scope=scope,
        )

    return BlastRadius(
        count=policy.olt_default,
        measured=False,
        basis=(
            "headend and OLT populations are not held per service; the policy pack's default of "
            f"{policy.olt_default} was used"
        ),
        scope=scope,
    )


# -------------------------------------------------------------------------------------------------
# The two questions
# -------------------------------------------------------------------------------------------------


def impact_radius(
    topology: TopologyContext | None,
    *,
    fault_domain: FaultDomain,
    policy: BlastRadiusPolicy,
    corroborating_services: int | None = None,
) -> BlastRadius:
    """How many customers this fault affects.

    `corroborating_services` is the number of services *observed* to have the same symptom, from
    correlation -- `None` when correlation has not run, which is not the same as zero. It can only
    ever raise the count, never lower it:

    * Three of a tap's eight peers complaining does not mean three are affected. The other five have
      the same fault and have not called yet, so the tap's population stands.
    * Thirty services down on a node the pack sizes at eight, because diagnosis placed the fault at
      the delimiter, means the diagnosis is too small for what is being seen. An observation cannot
      be estimated away, so the count becomes thirty and says where it came from.

    The second case leaves `scope` naming the smaller element on purpose. The scope is what
    diagnosis concluded, and quietly promoting it here would hide the disagreement in the one field
    a reviewer would use to spot it; the note says so instead.
    """
    scope = scope_for_fault_domain(fault_domain)
    sized = size_of(scope, topology, policy)
    if corroborating_services is None or corroborating_services <= sized.count:
        return sized
    return BlastRadius(
        count=corroborating_services,
        measured=True,
        basis=(
            f"{corroborating_services} services are observed with the same symptom, more than the "
            f"{sized.count} behind the {scope.value} the fault was placed at"
        ),
        scope=scope,
        notes=(
            f"the observed count exceeds the {scope.value} population, so either the fault is "
            f"further upstream than {fault_domain.value} or the plant records are wrong",
        ),
    )


def action_radius(
    topology: TopologyContext | None,
    *,
    action: ActionType,
    policy: BlastRadiusPolicy,
) -> BlastRadius:
    """How many customers this action would touch, whatever the fault around it is doing.

    Deliberately blind to the incident. A `CPE_REBOOT` is one premises during a node outage exactly
    as it is on a quiet Tuesday, because the reboot reaches one modem either way -- and the policy
    engine's blast-radius gate is a question about the action, not about the weather.
    """
    return size_of(scope_for_action(action), topology, policy)


__all__ = [
    "BlastRadius",
    "BlastRadiusScope",
    "action_radius",
    "impact_radius",
    "scope_for_action",
    "scope_for_fault_domain",
    "size_of",
]
