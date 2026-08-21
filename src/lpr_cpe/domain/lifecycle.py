"""The permitted incident-status transitions.

Status is written by many nodes. If each node decides for itself what the next status may be, the
lifecycle is defined by the union of those decisions and no document describes it -- which is how an
incident ends up back in `diagnosing` from `closed` because one retry path did not think about it.

So the table below is the single owner, `require_transition` is called by the state reducer, and an
illegal transition raises rather than being logged. The failure mode this prevents is not a wrong
status; it is a *plausible* wrong status that a dashboard then reports as normal.

There are two tables, and the second is small and subordinate. `TRANSITIONS` is what one *node* may
do. `STAGE_TRANSITIONS` is what the parent graph is shown when a whole *subgraph* runs as one of its
nodes and several of those hops arrive collapsed into one write; its own comment says why that
happens and why it is not solved by widening the first table.
"""

from __future__ import annotations

from lpr_cpe.domain.enums import IncidentStatus as S

TERMINAL_STATUSES: frozenset[S] = frozenset({S.CLOSED, S.CANCELLED})

# Read as: from -> the set of statuses reachable in one step.
#
# Two conventions worth stating because they are not obvious from the table:
#
# * ESCALATED is reachable from almost everywhere, because the bounded-loop guard and the
#   adapter-unavailable path can fire at any stage. It is not terminal -- a human can hand the
#   incident back -- so it routes onward to the diagnostic and dispatch stages.
# * AWAITING_APPROVAL returns to the stage that raised it rather than advancing. The approval says
#   yes or no to an action; it does not itself move the work forward.
TRANSITIONS: dict[S, frozenset[S]] = {
    S.NEW: frozenset({S.TRIAGING, S.CANCELLED, S.CLOSED, S.ESCALATED}),
    S.TRIAGING: frozenset({S.DIAGNOSING, S.AWAITING_APPROVAL, S.CLOSED, S.CANCELLED, S.ESCALATED}),
    S.DIAGNOSING: frozenset(
        {
            S.AWAITING_APPROVAL,
            S.REMOTE_RESOLUTION,
            S.SELF_HELP,
            S.DISPATCH_PLANNING,
            S.VALIDATING,
            S.RESOLVED,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.AWAITING_APPROVAL: frozenset(
        {
            S.DIAGNOSING,
            S.REMOTE_RESOLUTION,
            S.SELF_HELP,
            S.DISPATCH_PLANNING,
            S.AWAITING_HANDOVER,
            S.MR_RAISED,
            S.RECONCILING,
            S.RESOLVED,
            S.CLOSED,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.REMOTE_RESOLUTION: frozenset(
        {
            S.VALIDATING,
            S.DIAGNOSING,
            S.SELF_HELP,
            S.DISPATCH_PLANNING,
            S.AWAITING_APPROVAL,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.SELF_HELP: frozenset(
        {
            S.AWAITING_CUSTOMER,
            S.VALIDATING,
            S.DIAGNOSING,
            S.DISPATCH_PLANNING,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.AWAITING_CUSTOMER: frozenset(
        {S.SELF_HELP, S.VALIDATING, S.DIAGNOSING, S.DISPATCH_PLANNING, S.ESCALATED, S.CANCELLED}
    ),
    S.DISPATCH_PLANNING: frozenset(
        {S.AWAITING_APPROVAL, S.FIELD_IN_PROGRESS, S.DIAGNOSING, S.ESCALATED, S.CANCELLED}
    ),
    S.FIELD_IN_PROGRESS: frozenset(
        {
            S.AWAITING_HANDOVER,
            S.VALIDATING,
            S.DIAGNOSING,
            S.DISPATCH_PLANNING,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.AWAITING_HANDOVER: frozenset(
        {S.MR_RAISED, S.FIELD_IN_PROGRESS, S.AWAITING_APPROVAL, S.ESCALATED, S.CANCELLED}
    ),
    S.MR_RAISED: frozenset(
        {S.AWAITING_PLANT_REPAIR, S.AWAITING_HANDOVER, S.ESCALATED, S.CANCELLED}
    ),
    # DIAGNOSING is here for D19's `retry_diagnosis` arm: OSP can reject an MR, or close it having
    # found nothing, and then the plant theory was wrong and the incident owes itself a new one.
    S.AWAITING_PLANT_REPAIR: frozenset(
        {S.VALIDATING, S.MR_RAISED, S.DIAGNOSING, S.DISPATCH_PLANNING, S.ESCALATED, S.CANCELLED}
    ),
    S.VALIDATING: frozenset(
        {
            S.RECONCILING,
            S.DIAGNOSING,
            S.REMOTE_RESOLUTION,
            S.DISPATCH_PLANNING,
            S.ESCALATED,
            S.CANCELLED,
        }
    ),
    S.RECONCILING: frozenset({S.RESOLVED, S.AWAITING_APPROVAL, S.ESCALATED, S.CANCELLED}),
    S.RESOLVED: frozenset({S.CLOSED, S.RECONCILING, S.DIAGNOSING, S.ESCALATED}),
    # Terminal. Re-opening creates a LINKED incident with its own clock (D1) rather than moving
    # this one backwards, so there is deliberately no edge out of CLOSED except to nothing.
    S.CLOSED: frozenset(),
    S.CANCELLED: frozenset(),
    S.ESCALATED: frozenset(
        {
            S.DIAGNOSING,
            S.REMOTE_RESOLUTION,
            S.DISPATCH_PLANNING,
            S.AWAITING_HANDOVER,
            S.MR_RAISED,
            S.VALIDATING,
            S.RECONCILING,
            S.RESOLVED,
            S.CLOSED,
            S.CANCELLED,
        }
    ),
}


# Read as: a jump the *parent* graph is shown, mapped to the statuses a subgraph passed through to
# produce it. Every one of these is several rows of the table above walked inside one parent node.
#
# It exists because of how LangGraph composes a compiled subgraph. The child shares this state
# schema, so it shares the `status` channel -- but the parent is not shown the child's intermediate
# values, only its last one, as a single write. Measured with a three-status probe: the reducer was
# called with `A -> B` and `B -> C` inside the child and then, at the boundary, `A -> C`. The parent
# channel still held `A`, because nothing the child wrote had reached it.
#
# So `advance_status` was being asked to validate a stage as though it were a node, and refused. It
# refused in production, not in the suite: every test that crosses a stage boundary stops the parent
# at `interrupt_after` and hands the seam state to a standalone compile of the child, which is the
# one arrangement in which the boundary is never exercised. Driving all 41 fixture services through
# the real parent graph, 20 died here on `dispatch_planning -> validating` and no service had ever
# reached `closed`.
#
# **Why a second table and not wider rows above.** Widening `DISPATCH_PLANNING` to include
# `VALIDATING` would also license a single node to end a field visit without ever putting a crew on
# site, and nothing would notice. The pair is only legitimate when something walked the middle, so
# the middle is what is recorded here -- and `test_every_seam_jump_is_a_walkable_path` re-derives
# each jump hop by hop against `TRANSITIONS`, which means no entry below can smuggle in an edge the
# table above would not have allowed step by step.
#
# **Why not validate reachability instead.** Accepting any jump joined by some path was measured
# first and rejected: it takes the legal ordered pairs from 96 to 257 out of 324, which is most of
# the square. The check would have survived in name only.
STAGE_TRANSITIONS: dict[tuple[S, S], tuple[S, ...]] = {
    # `field_execution`, entered at `dispatch_planning`. Its exits are the four dispositions of a
    # visit, and only `abandon_handover`'s `diagnosing` is reachable in one hop already.
    (S.DISPATCH_PLANNING, S.VALIDATING): (S.FIELD_IN_PROGRESS,),
    # The same stage's handover exit, which goes two hops further: `open_field_visit` opens the
    # visit, `build_handover_contract` puts the case at the boundary, `file_plant_mr` raises the MR.
    # The fourth disposition, `no_visit`, needs no entry at all -- `open_field_visit` returns before
    # its status write when no work order is open, so the parent sees no hop.
    (S.DISPATCH_PLANNING, S.MR_RAISED): (S.FIELD_IN_PROGRESS, S.AWAITING_HANDOVER),
    # `reconciliation_closure`, entered at `validating`. P24 writes `reconciling`, P25b `resolved`
    # and P26 `closed` -- three hops for the one write the parent is shown. Its other exit,
    # `abandon_closure`'s `escalated`, needs no entry: `validating` reaches it in one step already.
    #
    # The exceptional path passes `awaiting_approval` between `reconciling` and `resolved`, and
    # that walk is legal too; the pair recorded is the one both closes share. The approval is
    # invisible to the parent for the ordinary reason -- an interrupt inside a child leaves the
    # parent node incomplete, so the only write it ever receives is the child's last.
    #
    # Unreachable until 2026-08-20 and therefore unmeasured: every incident escalated out of this
    # stage, on a `validating -> escalated` hop that was legal without any of this. What was
    # holding them there was the closure demanding an approval kind the stage's own gate does not
    # own, and fixing that in `PolicyEngine._check_confidence` is what first drove a parent run
    # into the write below.
    (S.VALIDATING, S.CLOSED): (S.RECONCILING, S.RESOLVED),
    # `plant_referral`, entered at `diagnosing` -- D08's plant arm, where the case goes to OSP with
    # no Clean Boots visit behind it. One hop of middle: `prepare_plant_referral_approval` writes
    # `awaiting_approval` for P19's authorisation, then `file_plant_referral_mr` raises the MR.
    #
    # The narrower entry of the two that were considered. The plan was to widen `TRANSITIONS` with
    # `DIAGNOSING -> AWAITING_HANDOVER` by analogy with the seam above, on the assumption that a
    # plant referral is a handover without the crew. Measured against `can_transition` first:
    # `diagnosing -> awaiting_approval` and `awaiting_approval -> mr_raised` are both already legal,
    # so the walk this stage takes needs nothing new in the node table -- while
    # `diagnosing -> awaiting_handover` is False and would have had to be invented to support it.
    # The analogy would have licensed a node to put a case at the plant boundary straight out of
    # diagnosis, which is the same thing the note above refuses for `DISPATCH_PLANNING`.
    #
    # This stage's other exit needs no entry either: `abandon_plant_referral` writes `escalated`,
    # and both `diagnosing -> escalated` and `awaiting_approval -> escalated` are single legal hops
    # already -- which is why one node can serve a policy block and a refused approval alike.
    (S.DIAGNOSING, S.MR_RAISED): (S.AWAITING_APPROVAL,),
}


class IllegalTransitionError(ValueError):
    """Raised when a node tries to move an incident somewhere the lifecycle does not allow."""

    def __init__(self, current: S, requested: S) -> None:
        allowed = sorted(t.value for t in TRANSITIONS.get(current, frozenset()))
        seams = sorted(b.value for a, b in STAGE_TRANSITIONS if a is current)
        super().__init__(
            f"illegal incident transition {current.value} -> {requested.value}; "
            f"permitted from {current.value}: {allowed or ['(terminal)']}"
            + (f"; and across a stage boundary: {seams}" if seams else "")
        )
        self.current = current
        self.requested = requested


def can_transition(current: S, requested: S) -> bool:
    """A no-op transition is always legal.

    Nodes re-write the status they already hold routinely -- a retried node sets `diagnosing` again
    -- and making that an error would force every caller to compare first.

    `STAGE_TRANSITIONS` is consulted second and never first. A caller asking about a pair that is
    already a single hop gets the same answer it always did; the seam table can only ever add, and
    what it adds is bounded by the entries written down in it.
    """
    if current is requested:
        return True
    if requested in TRANSITIONS.get(current, frozenset()):
        return True
    return (current, requested) in STAGE_TRANSITIONS


def require_transition(current: S, requested: S) -> S:
    if not can_transition(current, requested):
        raise IllegalTransitionError(current, requested)
    return requested


def is_terminal(status: S) -> bool:
    return status in TERMINAL_STATUSES


def reachable_from(current: S) -> frozenset[S]:
    return TRANSITIONS.get(current, frozenset())
