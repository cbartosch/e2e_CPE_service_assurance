"""The two status tables, checked against each other rather than against a list of examples.

`domain.lifecycle` is consulted on every status write in the system, by one reducer, and it refuses
by raising. That makes it the cheapest place in the codebase to be wrong expensively: a row that is
one member too wide licenses a shortcut nobody asked for, and a row that is one member too narrow
kills a production run at the moment the shortcut was legitimate. Both happened. The second one is
why `STAGE_TRANSITIONS` exists.

**Nothing here is checked through `can_transition`.** The seam table is validated hop by hop against
the raw `TRANSITIONS` dict, because `can_transition` now consults `STAGE_TRANSITIONS` itself and a
check routed through it would be asking the seam table to vouch for the seam table.

**The counts are measured, not asserted round.** 18 statuses, so 324 ordered pairs; 97 of them are
single hops; no status lists itself, so the 18 no-ops are all additional; the seam table adds 2.
`can_transition` accepts 117. The alternative that was tried first -- accept any pair joined by
*some* path -- accepts 257 of the 324, which is why it was thrown away and why
`test_can_transition_accepts_the_two_tables_and_nothing_else` names a pair that is reachable and
still refused.

Mutation-checked: 7 defects reinstated one at a time, 7 caught, each docstring quoting the message
actually produced. Only one of the seven had collateral, and it was correct -- a seam entry keyed on
`triaging` also reddened the refusal-message test, because that test asserts a status with no seam
is not handed an empty list to puzzle over.
"""

from __future__ import annotations

from itertools import pairwise, product

from lpr_cpe.domain import (
    STAGE_TRANSITIONS,
    TRANSITIONS,
    IncidentStatus,
    can_transition,
    require_transition,
)
from lpr_cpe.domain.lifecycle import IllegalTransitionError

S = IncidentStatus

ALL_STATUSES: tuple[IncidentStatus, ...] = tuple(S)


def test_every_status_has_a_row_so_a_new_one_cannot_default_to_terminal() -> None:
    """A missing key is not a missing row -- it reads as "terminal" and never raises.

    `can_transition` and `IllegalTransitionError` both reach the table through
    `TRANSITIONS.get(current, frozenset())`. That default is deliberate for `CLOSED` and
    `CANCELLED`, which have explicit empty rows, but it means a status added to the enum and
    forgotten here would silently refuse every onward move rather than being caught. The incident
    would stop dead in whatever stage introduced it, and the message would say `permitted from x:
    ['(terminal)']`, which reads like a design decision.

    Reinstated by deleting the `S.RESOLVED` row:
    `AssertionError: statuses with no row in TRANSITIONS: ['resolved']`.
    """
    missing = sorted(s.value for s in ALL_STATUSES if s not in TRANSITIONS)
    assert not missing, f"statuses with no row in TRANSITIONS: {missing}"


def test_every_seam_jump_is_a_walkable_path() -> None:
    """The seam table may only record journeys the node table would have permitted step by step.

    This is the whole reason `STAGE_TRANSITIONS` is a second table keyed by its middle rather than a
    handful of extra members bolted onto the rows above. An entry here says "a subgraph walked
    `a -> m -> b` and the parent was shown `a -> b`", and that claim is only true if every one of
    those hops is separately legal. Without this check the seam table degenerates into an
    unaudited escape hatch: anyone hitting an `IllegalTransitionError` could silence it by adding a
    line, and the line would look exactly like the legitimate ones.

    The walk deliberately reads `TRANSITIONS` directly. Going through `can_transition` would consult
    `STAGE_TRANSITIONS` on every hop, so a self-referential entry -- one whose "path" is the jump
    itself -- would validate.

    Reinstated twice. With `(S.TRIAGING, S.VALIDATING): (S.DIAGNOSING, S.RESOLVED)`, an entry whose
    first two hops are perfectly real and whose last is not:
    `AssertionError: seam jumps whose path is not walkable in TRANSITIONS: ['triaging -> validating:
    hop 3 of 3, resolved -> validating is not in TRANSITIONS[resolved]']`. And with the path emptied
    to `()`, which is the form the escape hatch would actually take:
    `['dispatch_planning -> validating: empty path, so it is a bare widening']`.
    """
    offenders: list[str] = []
    for (entry, exit_), middle in sorted(
        STAGE_TRANSITIONS.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)
    ):
        if not middle:
            offenders.append(
                f"{entry.value} -> {exit_.value}: empty path, so it is a bare widening"
            )
            continue
        walk = (entry, *middle, exit_)
        for i, (a, b) in enumerate(pairwise(walk), start=1):
            if b not in TRANSITIONS.get(a, frozenset()):
                offenders.append(
                    f"{entry.value} -> {exit_.value}: hop {i} of {len(walk) - 1}, "
                    f"{a.value} -> {b.value} is not in TRANSITIONS[{a.value}]"
                )
    assert not offenders, f"seam jumps whose path is not walkable in TRANSITIONS: {offenders}"


def test_no_seam_entry_restates_a_hop_the_node_table_already_allows() -> None:
    """A seam entry that is already a single hop is dead weight that reads as load-bearing.

    Three of `field_execution`'s four exits need no entry here, because `dispatch_planning` reaches
    them in one step; only `validating` does not. If a future widening of `TRANSITIONS` makes an
    entry below redundant, the entry stops being evidence that a subgraph walked a middle and
    becomes a comment that happens to be a dict key -- and the next person to read it will believe a
    stage boundary exists where none does.

    Reinstated by adding `(S.DISPATCH_PLANNING, S.DIAGNOSING): (S.FIELD_IN_PROGRESS,)`, which is the
    plausible-looking mistake -- `abandon_handover` really does end in `diagnosing`:
    `AssertionError: seam entries TRANSITIONS already allows in one hop:
    ['dispatch_planning -> diagnosing']`.
    """
    redundant = sorted(
        f"{a.value} -> {b.value}"
        for a, b in STAGE_TRANSITIONS
        if a is b or b in TRANSITIONS.get(a, frozenset())
    )
    assert not redundant, f"seam entries TRANSITIONS already allows in one hop: {redundant}"


def test_can_transition_accepts_the_two_tables_and_nothing_else() -> None:
    """Sweeps all 324 ordered pairs, so the seam table's cost is bounded by what it names.

    The first attempt at this fix accepted any pair joined by some path through `TRANSITIONS`. It
    passed every test in the suite, because it is a superset of the correct answer. Sweeping the
    square is what showed the price: 257 of 324 pairs, against 117 for the table as written --
    `triaging -> field_in_progress` among the newly legal ones, which would let triage put a crew on
    a customer's roof without ever diagnosing the fault.

    So the expectation is built from the *data* -- the rows, the no-ops, the named seams -- and any
    implementation that answers by searching for a path fails, whatever the tables say.

    Reinstated by replacing the seam lookup with a reachability walk:
    `AssertionError: can_transition accepted 144 pairs the two tables do not name, first 8:
    ['awaiting_approval -> awaiting_customer', 'awaiting_approval -> awaiting_plant_repair',
    'awaiting_approval -> field_in_progress', 'awaiting_approval -> validating', 'awaiting_customer
    -> awaiting_approval', 'awaiting_customer -> awaiting_handover', 'awaiting_customer ->
    awaiting_plant_repair', 'awaiting_customer -> closed']`. The list is truncated because 146 lines
    of offender is not a diagnosis; the count is the number that matters.
    """
    expected = {(a, a) for a in ALL_STATUSES}
    expected |= {(a, b) for a, row in TRANSITIONS.items() for b in row}
    expected |= set(STAGE_TRANSITIONS)

    accepted = {(a, b) for a, b in product(ALL_STATUSES, repeat=2) if can_transition(a, b)}

    def _named(pairs: set[tuple[IncidentStatus, IncidentStatus]]) -> list[str]:
        return sorted(f"{a.value} -> {b.value}" for a, b in pairs)

    extra, missing = _named(accepted - expected), _named(expected - accepted)
    assert not extra, (
        f"can_transition accepted {len(extra)} pairs the two tables do not name, "
        f"first 8: {extra[:8]}"
    )
    assert not missing, (
        f"can_transition refused {len(missing)} pairs the tables allow, first 8: {missing[:8]}"
    )

    # The named example from the docstring, asserted rather than described: reachable, and refused.
    assert not can_transition(S.TRIAGING, S.FIELD_IN_PROGRESS)


def test_the_refusal_message_names_the_stage_boundary_as_well_as_the_row() -> None:
    """Two very different mistakes arrive as the same exception, so the message must tell them apart.

    A node that writes a status its row does not allow has made a routing error. A node that writes
    one only a *whole subgraph* is entitled to write has made a composition error -- it is doing at
    one hop what several were meant to do -- and the fix is in a different file. An operator reading
    `permitted from dispatch_planning: [...]` and not finding `validating` in the list would
    conclude the transition is impossible, when in fact it is the documented exit of a stage.

    Reinstated by dropping the seam clause from `IllegalTransitionError.__init__`:
    `AssertionError: assert 'across a stage boundary' in 'illegal incident transition
    dispatch_planning -> closed; permitted from dispatch_planning: ['awaiting_approval',
    'cancelled', 'diagnosing', 'escalated', 'field_in_progress']'`.
    """
    message = str(IllegalTransitionError(S.DISPATCH_PLANNING, S.CLOSED))
    assert "across a stage boundary" in message
    assert "validating" in message.split("across a stage boundary")[1]

    # A status with no seam entry is not given an empty list to puzzle over.
    plain = str(IllegalTransitionError(S.TRIAGING, S.CLOSED))
    assert "across a stage boundary" not in plain


def test_require_transition_returns_the_requested_status_so_reducers_can_assign_it() -> None:
    """The reducer writes what this returns, which is why it returns rather than validating in place.

    Reinstated by having `require_transition` return `current`:
    `AssertionError: assert <IncidentStatus.DISPATCH_PLANNING: 'dispatch_planning'> is
    <IncidentStatus.FIELD_IN_PROGRESS: 'field_in_progress'>`. The no-op on the first line survives
    that defect, which is why it is not the only case here.
    """
    assert require_transition(S.DIAGNOSING, S.DIAGNOSING) is S.DIAGNOSING
    assert require_transition(S.DISPATCH_PLANNING, S.FIELD_IN_PROGRESS) is S.FIELD_IN_PROGRESS
    assert require_transition(S.DISPATCH_PLANNING, S.VALIDATING) is S.VALIDATING
