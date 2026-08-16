"""What every action-taking subgraph must work out before it may act.

Stage 3 has three branches that each pick one `ResolutionOption`, put it to the policy engine and
then either act on it or record why they did not: the remote branch, the self-help branch, and
field planning. The question "may this run, and is it the same thing the router meant?" is
identical in all three, and the parts of the answer that are pure functions of state live here.

This module was extracted, not designed up front. Every function below was written inside
`remote_resolution.py` first, and `evidence_support` carried the note *"the second caller is when it
moves"*. The self-help branch is the second caller. Moving them is cheaper than the alternative:
two copies of `attempt_number` that disagreed about whether a blocked action counts would let a
third reboot through on one path and not the other, and nothing would fail until it did.

Nothing here calls an adapter or reads a clock except through `ctx`, and nothing here decides
anything -- `PolicyEngine` remains the only thing that authorises an action. These are the readings
the engine is given, and the point of gathering them in one place is that the engine is then asked
the same question by every branch.

`reachability_verdict` arrived later and by the same route. It is not a policy input -- it is read
*after* the action, not before it -- but it is the other question every acting branch has to answer
in the same words: **did the device come back?** It began as `_verdict` inside `remote_resolution`
with one caller; the self-help branch is the second, and two branches that answered "was this
restored?" differently would disagree about which incidents may be closed.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from lpr_cpe.domain.enums import ActionType, Severity
from lpr_cpe.graph.nodes._runtime import derive_id
from lpr_cpe.graph.state import IncidentState
from lpr_cpe.policies.engine import CUSTOMER_CONTACT_ACTIONS, PolicyInput

if TYPE_CHECKING:
    from lpr_cpe.domain.resolution import ResolutionOption
    from lpr_cpe.graph.context import GraphContext


def attempt_number(state: IncidentState, action_type: ActionType) -> int:
    """Which attempt of this action type the next one would be. One-based.

    Counted over `ActionRecord.was_attempted`, which is the single owner of "this reached the
    external system". The policy engine compares `PolicyInput.attempt` against the pack's
    `attempt_limits` entry for the action -- a count of *this action*, not of diagnostic cycles --
    so a cycle counter here would compare two different quantities and let a third reboot through
    on the grounds that it was only the second cycle.

    The same number keys the approval id, so a genuine second request for the same action is a
    second question in the audit trail rather than a de-duplicated replay of the first.
    """
    already = sum(
        1
        for record in state.get("action_history", [])
        if record.action_type is action_type and record.was_attempted
    )
    return already + 1


def executed_idempotency_keys(state: IncidentState) -> frozenset[str]:
    """Keys that have already reached an adapter, for the engine's duplicate check.

    Only the attempted ones. A key attached to an action that policy blocked never left the process
    and the adapter's ledger does not hold it, so treating it as executed would refuse the retry
    that a corrected policy is supposed to permit -- and the simulator makes the same distinction,
    declining to occupy the key on a blocked write.
    """
    return frozenset(
        record.idempotency_key
        for record in state.get("action_history", [])
        if record.was_attempted and record.idempotency_key
    )


def idempotency_key_for(state: IncidentState, option: ResolutionOption) -> str:
    """The key this action would be sent under. Derived, so the policy check and the send agree.

    Keyed on the option rather than on the attempt, and that is the point of an idempotency key:
    `option_id` already carries the plan id, which carries the diagnostic cycle, so re-offering a
    reboot in a later cycle produces a new key while a replay within one cycle produces the same
    one. A key that included the attempt counter would make every retry a fresh write and the
    adapter's ledger would never suppress anything.
    """
    return derive_id("IDK", state.get("incident_id") or "", option.option_id)


def evidence_support(state: IncidentState, now: datetime) -> tuple[int, float | None]:
    """How many distinct systems have contributed evidence, and how old the newest of it is.

    `PolicyInput` wants both and distinguishes `0` from `None` sharply: zero means we looked and
    found nothing, `None` means the caller never gathered it. A node calling this *has* looked, so
    the count is always a number -- including zero, which blocks, correctly, because an action taken
    on no corroboration is an action taken on one system's opinion of itself.

    The age is `None` only when there is no evidence to be old, in which case the count has already
    blocked with the better reason. It is the age of the **newest** item, which is what the engine's
    message says it compares ("newest evidence is N minutes old"): the freshness question is whether
    anything recent supports the action, not whether everything does.
    """
    evidence = state.get("evidence", [])
    sources = {item.source_system for item in evidence if item.source_system}
    if not evidence:
        return len(sources), None
    newest = max(item.observed_at for item in evidence)
    return len(sources), max((now - newest).total_seconds() / 60.0, 0.0)


def contact_history(state: IncidentState, local_now: datetime) -> tuple[int, float | None]:
    """How many times we have contacted this customer today, and how long ago the last one was.

    The pack caps contacts per incident per day and imposes a minimum spacing between them, and
    `PolicyEngine._check_customer_contact` compares against exactly these two numbers. Until the
    self-help branch existed nothing supplied them and both sat at their defaults -- which reads as
    "no contact has ever been made" and is the *unsafe* direction for a cap: it can only ever
    under-count, so the first thing it would fail to stop is the fourth message of the day.

    Counted over `CUSTOMER_CONTACT_ACTIONS`, imported from the engine rather than re-listed here.
    A private copy of that set is drift this codebase has already been bitten by once: two
    definitions of "which actions reach the customer" would disagree the first time one of them
    learned about a new channel, and the symptom would be a cap that silently stopped applying.

    **`local_now` must be the operating timezone's instant, not UTC's**, which is why the argument
    is named for it. The cap exists so that a customer is not messaged repeatedly in one waking
    day, and Puerto Rico is UTC-04:00: a UTC day boundary falls at 20:00 local, so counting against
    UTC would reset the allowance in the middle of the evening -- the one time it most needs not to.
    Every stamp is converted into `local_now`'s zone before its date is taken, so a message sent at
    23:30 local yesterday does not count towards today however it was stored.

    Attempted contacts only, for the same reason as `executed_idempotency_keys`: a message policy
    blocked was never sent, and counting it would let one refusal consume the day's allowance.
    """
    sent = [
        record
        for record in state.get("action_history", [])
        if record.action_type in CUSTOMER_CONTACT_ACTIONS and record.was_attempted
    ]
    if not sent:
        return 0, None
    stamps = [record.completed_at or record.started_at for record in sent]
    zone = local_now.tzinfo
    today = local_now.date()
    contacts_today = sum(1 for stamp in stamps if stamp.astimezone(zone).date() == today)
    latest = max(stamps)
    return contacts_today, max((local_now - latest).total_seconds() / 60.0, 0.0)


def reachability_verdict(
    pre: dict[str, Any] | None, post: dict[str, Any] | None
) -> tuple[bool | None, str]:
    """Did the repair work? `True`, `False`, or `None` for "this cannot be told from here".

    Three-valued because the simulator's -- and TR-069's -- only unambiguous symptom is
    reachability. A device that was offline and is now online was fixed by whatever we just did. A
    device that is still offline was not. A device that was **online throughout**, which is every
    Wi-Fi and throughput fault, has no observable before-and-after here at all, and this is where a
    two-valued verdict does real damage in both directions: `True` closes a wifi channel change that
    changed nothing, and `False` re-diagnoses a repair that worked.

    So the third answer is recorded as itself. `RemoteAction.fixed_it` requires
    `verification_passed is True`, so `None` does not restore the service and D10 sends the incident
    for another pass -- the conservative direction -- while `verification_summary` says the
    verification was not possible rather than that it failed. This is a real gap in what the
    fixture-backed CPE adapter can show and it is recorded as one; see
    `docs/vendor-integration-gaps.md`.

    Both branches pass the *same* pair of `read_status` payloads, and that is the point of it living
    here: the self-help branch judges the customer's power-cycle by exactly the criterion the remote
    branch judges its own reboot by, because they are the same event with a different actor.
    """
    if post is None:
        return False, "verification read failed: the CPE adapter could not be reached afterwards"
    was_online = bool(pre.get("online")) if pre is not None else None
    is_online = bool(post.get("online"))
    if was_online is False and is_online:
        return True, "the device was offline before the action and has re-established its session"
    if not is_online:
        return False, "the device is still offline after the action"
    if was_online is None:
        return None, (
            "no reading was taken before the action, so the device being online now cannot be "
            "attributed to it"
        )
    return None, (
        "the device was online before the action and is online after it. The only symptom this "
        "adapter exposes is reachability, so this action can be neither confirmed nor refuted "
        "here; stability validation is what decides it"
    )


def policy_input_for(
    state: IncidentState, ctx: GraphContext, option: ResolutionOption
) -> PolicyInput:
    """Everything the engine may consider about one option, read off state.

    Two fields are deliberately left at their defaults, and each omission is a decision:

    * **`competing_confidence`** stays `None`. `RCAResult.confidence` is already folded --
      `leader / (leader + rival) * leader`, see `graph.nodes.diagnosis` -- so handing the raw
      runner-up alongside it would compare two different scales and fire the ambiguity margin on a
      case the fold has already accounted for.
    * **`in_maintenance_window`** stays `False`. Nothing in state records a maintenance window, and
      `False` is the fail-closed direction: an action that requires one is held for approval rather
      than waved through on an assumption.

    `local_time` and the two contact figures are supplied for *every* action, including the ones
    they cannot affect. `_check_customer_contact` returns immediately for anything outside
    `CUSTOMER_CONTACT_ACTIONS`, so a CPE reboot is unchanged by their presence -- and passing them
    unconditionally is what stops the next branch from being written without them. The earlier
    version of this function omitted them on the grounds that no caller needed them yet; the
    self-help branch then needed them, and an omission justified by "no caller needs this" is an
    omission that fails silently for the caller who does.
    """
    rca = state.get("rca")
    impact = state.get("impact")
    sla = state.get("sla")
    quality = state.get("data_quality")
    now = ctx.clock.now()
    local_now = ctx.clock.local_now()
    source_count, age_minutes = evidence_support(state, now)
    contacts_today, minutes_since_contact = contact_history(state, local_now)
    return PolicyInput(
        action_type=option.action_type,
        incident_id=state.get("incident_id") or "",
        target_ref=option.target_ref,
        actor_role=ctx.automation_role,
        rca_confidence=rca.confidence if rca is not None else None,
        evidence_source_count=source_count,
        evidence_age_minutes=age_minutes,
        data_quality_flags=tuple(quality.flags) if quality is not None else (),
        attempt=attempt_number(state, option.action_type),
        blast_radius=option.blast_radius,
        severity=impact.severity if impact is not None else Severity.MEDIUM,
        vulnerable_customer=sla.vulnerable_customer if sla is not None else False,
        local_time=local_now.time(),
        contacts_today=contacts_today,
        minutes_since_last_contact=minutes_since_contact,
        idempotency_key=idempotency_key_for(state, option),
        executed_idempotency_keys=executed_idempotency_keys(state),
    )
