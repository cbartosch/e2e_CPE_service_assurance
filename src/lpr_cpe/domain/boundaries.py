"""Which crew owns which fault domain, in one table.

The Clean/Dirty Boots split is the single most consequential fact in this system: it decides who is
dispatched, whether a handover contract is needed, whether an MR is raised, and which of the six
approval gates fires. It is read in at least four places -- crew selection, the shared-network
routing decision, the boundary-evidence test, and a `FieldFinding` validator that refuses a
"send it to OSP" finding pointing at a premises domain.

Four readers means four chances to disagree, and the disagreement would not look like a bug. It
would look like an incident that was dispatched to the wrong crew for a defensible-sounding reason.
So the table below is the owner, in the same way `lifecycle.TRANSITIONS` owns status changes.

The boundary itself
-------------------
Clean Boots own everything from the tap or ODP **inward**, to and including the customer's premises.
Dirty Boots (OSP) own the plant **beyond** it. The tap or ODP itself is the boundary object, and it
is deliberately neither: a fault at the tap needs both crews, because the drop-side and plant-side
halves cannot be tested from the same side. That is `CrewType.JOINT`, and it exists to stop the case
that otherwise becomes two sequential visits and a repeat-visit KPI hit.

Three domains map to **no** crew, and that is not an omission. `SERVICE_PLATFORM` and `PROVISIONING`
are fixed from a NOC console, so dispatching anyone would be the defect. `NO_FAULT_FOUND` has
nothing to fix. `MULTIPLE` and `UNKNOWN` are answers diagnosis has not finished giving, so choosing
a crew from them would be guessing -- callers must escalate instead. `crew_for` returns `None` for
all five rather than defaulting to `CLEAN`, because a wrong default here sends a van to a customer
whose problem is in a provisioning system.

What does *not* belong here
---------------------------
`detectors/risk.py` splits the same enum into `physical` and `soft` to score dispatch risk, and it
is not this boundary wearing a different name. It counts `DROP` as physical although the drop is
premises-side, and it leaves `POWER` out of both although power is plant. The two tables disagree
because they answer different questions -- "who is sent" versus "does anyone have to touch
anything" -- so merging them would silently change one of the two answers. One owner per fact means
one owner per *fact*, not one table per enum.
"""

from __future__ import annotations

from lpr_cpe.domain.enums import CrewType, FaultDomain

#: Inward of the tap/ODP. A truck roll here is a customer-premises visit.
PREMISES_DOMAINS: frozenset[FaultDomain] = frozenset(
    {
        FaultDomain.CPE,
        FaultDomain.INSIDE_HOME_WIRING,
        FaultDomain.DROP,
        FaultDomain.CUSTOMER_ENVIRONMENT,
    }
)

#: Beyond the tap/ODP, including the boundary object itself. OSP territory.
#:
#: `TAP_OR_ODP` is a member: a fault *at* the boundary is plant work even though it also needs a
#: Clean Boots technician on the drop side. `FieldFinding` enforces the same membership when it
#: refuses `requires_plant_work=True` on a premises domain, and imports this set to do it.
PLANT_DOMAINS: frozenset[FaultDomain] = frozenset(
    {
        FaultDomain.TAP_OR_ODP,
        FaultDomain.DISTRIBUTION,
        FaultDomain.FEEDER,
        FaultDomain.NODE_OR_OLT,
        FaultDomain.HEADEND_OR_CO,
        FaultDomain.POWER,
    }
)

#: Fixed from a console. Dispatching any crew for these is the defect, not the fallback.
BACK_OFFICE_DOMAINS: frozenset[FaultDomain] = frozenset(
    {
        FaultDomain.SERVICE_PLATFORM,
        FaultDomain.PROVISIONING,
    }
)

#: Diagnosis has not produced a dispatchable answer. Callers escalate rather than guess.
UNDISPATCHABLE_DOMAINS: frozenset[FaultDomain] = frozenset(
    {
        FaultDomain.NO_FAULT_FOUND,
        FaultDomain.MULTIPLE,
        FaultDomain.UNKNOWN,
    }
)


def crew_for(domain: FaultDomain) -> CrewType | None:
    """Which crew must attend a fault in `domain`, or `None` if none should be sent.

    `None` is a real answer with three distinct causes -- back-office, nothing to fix, and diagnosis
    incomplete -- which callers separate with the sets above. It is never a fallback for an
    unrecognised domain: `is_classified` guarantees there are none.
    """
    if domain is FaultDomain.TAP_OR_ODP:
        return CrewType.JOINT
    if domain in PREMISES_DOMAINS:
        return CrewType.CLEAN
    if domain in PLANT_DOMAINS:
        return CrewType.DIRTY
    return None


def is_plant_side(domain: FaultDomain) -> bool:
    """Whether the fault is at or beyond the tap/ODP, and so needs a handover or an MR."""
    return domain in PLANT_DOMAINS


def is_premises_side(domain: FaultDomain) -> bool:
    """Whether the fault is inward of the tap/ODP, and so is Clean Boots' to finish."""
    return domain in PREMISES_DOMAINS


def is_classified(domain: FaultDomain) -> bool:
    """Whether the table has an opinion about `domain` at all.

    Exists so a test can assert every `FaultDomain` member is covered exactly once. A new member
    added without a home would otherwise fall through `crew_for` to `None` and be silently treated
    as "diagnosis incomplete" -- the failure mode this module is written to prevent.
    """
    return domain in (
        PREMISES_DOMAINS | PLANT_DOMAINS | BACK_OFFICE_DOMAINS | UNDISPATCHABLE_DOMAINS
    )
