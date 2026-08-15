"""The Clean/Dirty Boots boundary, checked as a partition rather than as a lookup.

`domain.boundaries` decides who is dispatched. Getting it wrong does not raise: it sends a Clean
Boots technician to a fibre distribution fault, or an OSP crew to a customer's living room, and the
first anyone hears of it is a repeat visit. So the tests here are stricter than "the function
returns the right thing for the cases I thought of".

**The expected-crew table is written out by hand.** It would have been shorter to derive it from
`PREMISES_DOMAINS` and `PLANT_DOMAINS`, and it would also have been worthless: `crew_for` reads
those same sets, so a derived table agrees with the implementation by construction and moving
`DROP` from one set to the other would keep every assertion green. The table below is a second,
independent statement of the same fact, taken from the specification's Clean/Dirty split. Its
disagreeing with the sets is the entire point.

**The sets are checked as a partition, not as a collection of memberships.** Every `FaultDomain`
member must appear in exactly one of the four sets -- not at least one, and not at most one. A
member in two sets makes `crew_for` order-dependent; a member in none falls through to `None` and is
silently treated as "diagnosis incomplete", which is the failure `is_classified` exists to catch.
A new enum member fails both this and the table above, so it cannot be added without someone
deciding who attends it.

Mutation-checked: 9 defects reinstated one at a time, 9 caught. Two are worth naming because they
are the ones that look harmless in a diff:

* moving the `TAP_OR_ODP` check in `crew_for` below the `PLANT_DOMAINS` check. The tap is a member
  of `PLANT_DOMAINS`, so this returns `DIRTY` -- a defensible-looking answer that quietly deletes
  `JOINT` dispatch and turns every tap fault into two sequential visits.
* reinstating `field_ops`' own copy of the plant set, one member short. Nothing in `field_ops`'
  own tests notices, because the copy is self-consistent; only an assertion that the *reader* and
  the *owner* agree catches it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lpr_cpe.domain import (
    BACK_OFFICE_DOMAINS,
    PLANT_DOMAINS,
    PREMISES_DOMAINS,
    UNDISPATCHABLE_DOMAINS,
    CrewType,
    FaultDomain,
    FieldFinding,
    crew_for,
    is_classified,
    is_plant_side,
    is_premises_side,
)

#: Who attends a fault in each domain, transcribed from the specification's Clean/Dirty split rather
#: than computed from the module under test. `None` means nobody is dispatched -- see the three
#: distinct reasons in `test_nobody_is_dispatched_for_three_different_reasons`.
EXPECTED_CREW: dict[FaultDomain, CrewType | None] = {
    FaultDomain.CPE: CrewType.CLEAN,
    FaultDomain.INSIDE_HOME_WIRING: CrewType.CLEAN,
    FaultDomain.DROP: CrewType.CLEAN,
    FaultDomain.CUSTOMER_ENVIRONMENT: CrewType.CLEAN,
    FaultDomain.TAP_OR_ODP: CrewType.JOINT,
    FaultDomain.DISTRIBUTION: CrewType.DIRTY,
    FaultDomain.FEEDER: CrewType.DIRTY,
    FaultDomain.NODE_OR_OLT: CrewType.DIRTY,
    FaultDomain.HEADEND_OR_CO: CrewType.DIRTY,
    FaultDomain.POWER: CrewType.DIRTY,
    FaultDomain.SERVICE_PLATFORM: None,
    FaultDomain.PROVISIONING: None,
    FaultDomain.NO_FAULT_FOUND: None,
    FaultDomain.MULTIPLE: None,
    FaultDomain.UNKNOWN: None,
}

SETS = {
    "PREMISES_DOMAINS": PREMISES_DOMAINS,
    "PLANT_DOMAINS": PLANT_DOMAINS,
    "BACK_OFFICE_DOMAINS": BACK_OFFICE_DOMAINS,
    "UNDISPATCHABLE_DOMAINS": UNDISPATCHABLE_DOMAINS,
}


def _finding(domain: FaultDomain, *, requires_plant_work: bool) -> FieldFinding:
    return FieldFinding(
        finding_id="FND-boundary",
        work_order_id="WO-boundary",
        incident_id="INC-boundary",
        recorded_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        recorded_by="tech-01",
        fault_domain=domain,
        requires_plant_work=requires_plant_work,
    )


def test_every_fault_domain_is_in_exactly_one_set() -> None:
    """The partition. Both halves matter, and they fail for different reasons.

    In two sets: `crew_for` starts depending on the order its `if`s happen to be written in, and a
    reordering that reads like tidying changes who is dispatched.

    In no set: `crew_for` returns `None` by falling off the end, which callers read as "diagnosis
    has not concluded". A domain nobody classified would be quietly escalated forever.
    """
    for domain in FaultDomain:
        homes = [name for name, members in SETS.items() if domain in members]
        assert len(homes) == 1, f"{domain} is in {len(homes)} sets: {homes or 'none'}"


def test_is_classified_agrees_with_the_partition() -> None:
    """`is_classified` is what the rest of the system asks, so it must not drift from the sets."""
    for domain in FaultDomain:
        assert is_classified(domain), f"{domain} has no home in the boundary table"


def test_the_expected_crew_table_covers_every_fault_domain() -> None:
    """Guards the sweep below against shrinking silently.

    A new `FaultDomain` member that nobody added here would simply not be swept, and the
    parametrised test would go on passing with one fewer case.
    """
    assert set(EXPECTED_CREW) == set(FaultDomain), (
        f"undecided: {sorted(str(d) for d in set(FaultDomain) - set(EXPECTED_CREW))}"
    )


@pytest.mark.parametrize("domain", list(FaultDomain), ids=str)
def test_each_domain_dispatches_the_crew_the_specification_names(domain: FaultDomain) -> None:
    assert crew_for(domain) == EXPECTED_CREW[domain]


def test_a_fault_at_the_tap_dispatches_both_crews_although_the_tap_is_plant() -> None:
    """The one case membership alone gets wrong, called out because it is one `if` away.

    `TAP_OR_ODP` is in `PLANT_DOMAINS`, so the `PLANT_DOMAINS` branch would answer `DIRTY` for it.
    Only the explicit check ahead of that branch produces `JOINT`. Moving the check down is a
    two-line diff that reads like simplification and costs a second truck roll on every tap fault,
    because the drop side and the plant side cannot be tested from the same side of the boundary.
    """
    assert FaultDomain.TAP_OR_ODP in PLANT_DOMAINS
    assert crew_for(FaultDomain.TAP_OR_ODP) is CrewType.JOINT
    assert is_plant_side(FaultDomain.TAP_OR_ODP)
    assert not is_premises_side(FaultDomain.TAP_OR_ODP)


def test_nobody_is_dispatched_for_three_different_reasons() -> None:
    """`None` is one answer with three causes, and callers must be able to tell them apart.

    A back-office fault is fixed from a console; `NO_FAULT_FOUND` has nothing to fix; `MULTIPLE` and
    `UNKNOWN` mean diagnosis has not produced a dispatchable answer and the caller must escalate.
    Collapsing them would make "escalate to a human" and "close the incident" the same branch.
    """
    for domain in BACK_OFFICE_DOMAINS | UNDISPATCHABLE_DOMAINS:
        assert crew_for(domain) is None, f"{domain} would dispatch a crew"

    assert BACK_OFFICE_DOMAINS.isdisjoint(UNDISPATCHABLE_DOMAINS)
    assert FaultDomain.NO_FAULT_FOUND in UNDISPATCHABLE_DOMAINS
    assert FaultDomain.MULTIPLE in UNDISPATCHABLE_DOMAINS


def test_none_is_never_the_answer_for_a_domain_that_has_a_crew() -> None:
    """The contract `crew_for`'s docstring states: `None` is a decision, not a fallback.

    Stated as an implication over every member rather than as a list, so it keeps holding when the
    enum grows.
    """
    for domain in FaultDomain:
        if crew_for(domain) is None:
            assert domain in BACK_OFFICE_DOMAINS | UNDISPATCHABLE_DOMAINS, (
                f"{domain} fell through to None instead of being classified"
            )
        else:
            assert domain in PREMISES_DOMAINS | PLANT_DOMAINS


@pytest.mark.parametrize("domain", list(FaultDomain), ids=str)
def test_the_two_sides_of_the_boundary_never_overlap(domain: FaultDomain) -> None:
    """No domain is both, and the predicates are not each other's alias.

    Written as `not both` rather than `plant == not premises`, because the two are not complements:
    the five unclassified-for-dispatch domains are neither.
    """
    assert not (is_plant_side(domain) and is_premises_side(domain))
    if domain in BACK_OFFICE_DOMAINS | UNDISPATCHABLE_DOMAINS:
        assert not is_plant_side(domain)
        assert not is_premises_side(domain)


@pytest.mark.parametrize("domain", list(FaultDomain), ids=str)
def test_a_field_finding_accepts_plant_work_for_exactly_the_plant_domains(
    domain: FaultDomain,
) -> None:
    """`FieldFinding` reads `PLANT_DOMAINS`; this is what stops it holding a second copy.

    The validator refuses `requires_plant_work=True` on a non-plant domain, because an MR that does
    not name a plant object gets rejected by OSP. It used to spell the set out itself. A private
    copy is self-consistent, so `field_ops`' own tests would keep passing while it and the boundary
    table disagreed -- and the disagreement would show up as a finding accepted here and routed to
    the wrong crew there. Sweeping all fifteen domains against the owner is what makes the copy
    impossible to reintroduce quietly.
    """
    if is_plant_side(domain):
        finding = _finding(domain, requires_plant_work=True)
        assert finding.requires_plant_work
        return

    with pytest.raises(ValueError, match="not a plant domain"):
        _finding(domain, requires_plant_work=True)


def test_a_premises_finding_is_still_accepted_when_it_claims_no_plant_work() -> None:
    """The control for the sweep above: it is `requires_plant_work` that is refused, not the domain.

    Without this, a validator that rejected every premises finding outright would pass every
    assertion in this module.
    """
    for domain in PREMISES_DOMAINS:
        assert not _finding(domain, requires_plant_work=False).requires_plant_work
