"""The policy engine. One question, three answers, always with reasons.

`evaluate()` returns a `PolicyDecision` whose outcome is `ALLOWED`, `REQUIRES_APPROVAL` or
`BLOCKED`. There is no fourth answer and no `None`: an action the pack does not mention is blocked,
and an engine whose pack failed to load blocks everything. That is what "fail closed" means here,
and it is implemented as a *property of the lookup* -- `pack.rule_for()` returns `None` and `None`
is handled as a refusal -- rather than as a defensive `if` somebody could forget to write.

Three design decisions worth stating, because each has a plausible-looking alternative:

**Every applicable check runs; the engine does not short-circuit on the first block.** The obvious
implementation returns as soon as a refusal is found, and it produces a miserable operator
experience: you fix the stale telemetry, re-run, and now learn the attempt limit is also reached. A
`PolicyDecision` here carries *all* the reason codes that apply, so one read tells the operator
everything standing between the incident and the action. The cost is that a few checks evaluate
inputs that a short-circuiting engine would have skipped; they are all arithmetic on values the
caller already has.

**Policy does not ask whether writes are enabled.** `integrations.base.WriteGate` owns that, and
`ReasonCode.POLICY_WRITES_DISABLED` is its reason code, not ours. Policy answers "should we?" from
the pack; the gate answers "may this deployment at all?" from configuration. An action passes both,
policy first. Duplicating the two-switch check here would create a second owner for the most
safety-critical boolean in the system, and the copy would be the one that goes stale.

**Reads are exempt from the evidence and confidence bars, and from nothing else.** `read_status` and
`run_diagnostic` are how evidence and confidence are *obtained*, so gating them on either is a
closed loop that deadlocks the diagnosis stage on its opening call. The exemption is an explicit set
(`_READ_ONLY_ACTIONS`) rather than a property inferred from the risk class, and the reasoning is at
each of the two checks. Everything else -- the allowlist, the role, duplicate suppression -- still
applies to a read.

**Low RCA confidence requires approval; it does not block.** Blocking would leave the incident with
nowhere to go, and the specification's `low_confidence_rca` interrupt exists precisely so a human
can look at an ambiguous hypothesis set and decide. A confidence below `rca.review_below` and one
just below `min_for_dispatch` therefore reach the same interrupt with different explanations -- the
severity of the doubt changes what the operator is told, not who decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Final

from lpr_cpe.config.clock import Clock, SystemClock
from lpr_cpe.domain.base import new_id
from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    DataQualityFlag,
    PolicyOutcome,
    ReasonCode,
    Severity,
)
from lpr_cpe.domain.governance import PolicyDecision
from lpr_cpe.policies.loader import (
    PolicyPackError,
    canonical_digest,
    load_pack,
    policy_version,
)
from lpr_cpe.policies.models import PolicyPack
from lpr_cpe.security.rbac import Role, ToolAllowlist, approvers_for

#: Which evidence and confidence bars apply to an action, when the caller does not say. Derived from
#: the action rather than defaulted to one value, because "how much proof does this need" is a
#: property of the consequence: a work order is a dispatch decision whatever node requests it.
#: A caller may override via `PolicyInput.decision_class`; an unrecognised class is held to the
#: strictest bar in the pack (see `RCAPolicy.minimum_for` and `EvidencePolicy.min_sources_for`).
_DECISION_CLASS: Final[dict[ActionType, str]] = {
    ActionType.READ_STATUS: "diagnosis",
    ActionType.RUN_DIAGNOSTIC: "diagnosis",
    ActionType.CREATE_WORK_ORDER: "dispatch",
    ActionType.CANCEL_WORK_ORDER: "diagnosis",
    ActionType.RAISE_MR: "mr",
    ActionType.UPDATE_MR: "diagnosis",
    ActionType.CLOSE_INCIDENT: "closure",
    ActionType.CREATE_PM_CASE: "diagnosis",
    ActionType.NOTIFY_CUSTOMER: "diagnosis",
    ActionType.SEND_SELF_HELP: "remote_action",
}

#: Actions that reach the customer. Quiet hours, contact caps and the vulnerable-customer rules
#: apply to exactly these, and to nothing else -- rebooting a modem at 03:00 is not a contact.
#:
#: Public, because the graph has to count the contacts this set defines before it can hand the
#: engine a `contacts_today` to compare against the cap. `graph.subgraphs._shared.contact_history`
#: is that counter, and it imports this name rather than listing the two members again: the cap and
#: the count must be over the same set, and two copies would disagree the first time a channel was
#: added -- silently, since the symptom is a limit that stops applying rather than one that fires.
CUSTOMER_CONTACT_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.NOTIFY_CUSTOMER, ActionType.SEND_SELF_HELP}
)

#: Actions exempt from the root-cause confidence bar, because running them is how confidence is
#: obtained. Kept as an explicit set so adding a read-only action requires deciding this, rather
#: than inheriting an exemption from whatever risk class it lands in.
_READ_ONLY_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.READ_STATUS, ActionType.RUN_DIAGNOSTIC}
)


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """Everything the engine may consider, gathered by the caller.

    A single frozen input object rather than eighteen keyword arguments, for two reasons: the graph
    builds one of these from state and passes it whole, so a new check can read a field without
    changing every call site; and `PolicyDecision.evaluated_inputs` records the subset that actually
    mattered, which is only possible if the inputs are enumerable.

    Every optional field is `None` for "not measured", never `0` for it. The distinction is
    load-bearing: `evidence_source_count=0` means we looked and found nothing, and must block;
    `None` means the caller did not gather it, and must *also* block, but for a different reason and
    with a different fix. Collapsing them into `0` would hide a caller bug behind a data problem.
    """

    action_type: ActionType
    incident_id: str = ""
    target_ref: str = ""

    #: The principal requesting the action. `automation` is the graph acting unattended.
    actor_role: Role | str | None = None
    #: Overrides the class derived from `action_type`. See `_DECISION_CLASS`.
    decision_class: str | None = None

    rca_confidence: float | None = None
    #: The runner-up hypothesis's confidence, for the ambiguity check. Without it, two hypotheses at
    #: 0.66 and 0.65 produce a confident-looking decision built on a coin flip.
    competing_confidence: float | None = None

    evidence_source_count: int | None = None
    evidence_age_minutes: float | None = None
    data_quality_flags: tuple[DataQualityFlag, ...] = ()

    attempt: int = 1
    blast_radius: int | None = None
    severity: Severity = Severity.MEDIUM
    in_maintenance_window: bool = False

    vulnerable_customer: bool = False
    #: Operating-timezone wall clock. Supplied rather than read from the clock so a replayed node
    #: evaluates against the instant the decision belongs to, not the instant of the replay.
    local_time: time | None = None
    contacts_today: int = 0
    minutes_since_last_contact: float | None = None

    idempotency_key: str = ""
    #: Keys already executed for this incident. The graph carries them in state; a node that replays
    #: after an interrupt re-derives the same key and this is what makes the replay a no-op.
    executed_idempotency_keys: frozenset[str] = frozenset()

    #: Closure preconditions. `None` means "not applicable to this action".
    validation_passed: bool | None = None
    reconciled: bool | None = None

    def effective_decision_class(self) -> str:
        return self.decision_class or _DECISION_CLASS.get(self.action_type, "remote_action")


@dataclass(slots=True)
class _Finding:
    """One check's objection. Internal; the engine folds these into a `PolicyDecision`."""

    reason_code: ReasonCode
    explanation: str
    rule: str
    blocks: bool = True
    approval_kind: ApprovalKind | None = None
    inputs: dict[str, Any] = field(default_factory=dict)


class PolicyEngine:
    """Evaluates one action against one pack.

    Stateless per call and cheap to construct: the pack is cached by `loader.load_pack`, so building
    an engine per request costs a dict lookup. Nothing here mutates, which is what lets the API, the
    graph nodes and the scan job share one instance without coordinating.
    """

    __slots__ = ("_clock", "_pack", "_unavailable_reason")

    def __init__(
        self,
        pack: PolicyPack | None = None,
        *,
        clock: Clock | None = None,
        unavailable_reason: str = "",
    ) -> None:
        if pack is None and not unavailable_reason:
            raise ValueError(
                "PolicyEngine needs either a pack or an unavailable_reason. An engine with neither "
                "would block every action without being able to say why, which is unauditable -- "
                "use PolicyEngine.load() or PolicyEngine.load_or_unavailable()"
            )
        self._pack = pack
        self._clock: Clock = clock or SystemClock()
        self._unavailable_reason = unavailable_reason

    # -- construction ----------------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | None = None, *, clock: Clock | None = None) -> PolicyEngine:
        """Load the pack, raising `PolicyPackError` if it is unusable.

        What a *starting* service should call. A process that boots with an invalid pack is a
        process that will make its first decision wrongly, and refusing to start is the cheapest
        possible moment to find out.
        """
        return cls(load_pack(path), clock=clock)

    @classmethod
    def load_or_unavailable(
        cls, path: str | None = None, *, clock: Clock | None = None
    ) -> PolicyEngine:
        """Load the pack, or return an engine that blocks everything and says why.

        What a *running* service should call when reloading. The degraded engine is not a fallback
        to permissiveness -- it is the fail-closed state, and `/health` reports it as unhealthy so
        the blocked incidents have a visible cause rather than looking like a policy that got
        stricter.
        """
        try:
            return cls(load_pack(path), clock=clock)
        except PolicyPackError as exc:
            return cls(None, clock=clock, unavailable_reason=str(exc))

    # -- introspection ---------------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._pack is not None

    @property
    def pack(self) -> PolicyPack:
        """The loaded pack. Raises if unavailable rather than returning a default one.

        The decision services read thresholds through this. A default pack here would be a set of
        thresholds nobody reviewed, applied silently at exactly the moment the reviewed ones could
        not be read.
        """
        if self._pack is None:
            raise PolicyPackError(f"policy pack unavailable: {self._unavailable_reason}")
        return self._pack

    @property
    def policy_version(self) -> str:
        """`<declared>+<content digest>`, or `unavailable+<reason digest>`.

        The unavailable form is still a version string, and deliberately a distinguishable one: an
        audit trail that records `unavailable` for a block is telling the reviewer the truth,
        whereas recording the last known good version would attribute the block to rules that were
        not consulted.

        The reason is *digested* rather than appended raw for two reasons. A pack validation error
        is a multi-line pydantic report and a version string has to fit in a column; and two
        distinct failures -- a missing file and a malformed threshold -- must not collapse into one
        indistinguishable `unavailable`, or a reviewer reading a month of blocked decisions cannot
        tell whether they had one cause or twenty. `unavailable_reason` carries the prose.
        """
        if self._pack is None:
            return f"unavailable+{canonical_digest(self._unavailable_reason)[:12]}"
        return policy_version(self._pack)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    def health(self) -> dict[str, Any]:
        """For `GET /health`. Reports the pack's identity, or why there is none."""
        if self._pack is None:
            return {
                "policy": "unavailable",
                "policy_version": self.policy_version,
                "reason": self._unavailable_reason,
                "effect": "every action is blocked",
            }
        return {"policy": "ok", "policy_version": self.policy_version, **self._pack.summary()}

    # -- evaluation ------------------------------------------------------------------------------

    def evaluate(self, request: PolicyInput) -> PolicyDecision:
        """The engine's only real entry point.

        Runs every applicable check, then folds the findings into one decision: any blocking finding
        makes the outcome `BLOCKED`; otherwise any approval finding makes it `REQUIRES_APPROVAL`;
        otherwise `ALLOWED`. Blocks win over approvals because an approval for an action that is
        blocked anyway would put a question to a human whose answer cannot be honoured.
        """
        if self._pack is None:
            return self._decision(
                request,
                outcome=PolicyOutcome.BLOCKED,
                findings=[
                    _Finding(
                        reason_code=ReasonCode.POLICY_NO_MATCHING_RULE,
                        explanation=(
                            "policy evaluation is unavailable, so no action is permitted: "
                            f"{self._unavailable_reason}"
                        ),
                        rule="engine.unavailable",
                    )
                ],
                approval_kind=None,
            )

        findings: list[_Finding] = []
        pack = self._pack
        rule = pack.rule_for(request.action_type)

        # The allowlist first. Not because it short-circuits -- it does not -- but because every
        # later check reads `rule`, and a missing rule means those checks have nothing to compare
        # against and must be skipped rather than guessed at.
        findings.extend(self._check_allowlist(request, pack))
        findings.extend(self._check_role(request))
        findings.extend(self._check_duplicate(request))

        if rule is not None and rule.allowed:
            findings.extend(self._check_evidence(request, pack))
            findings.extend(self._check_confidence(request, pack))
            findings.extend(self._check_attempts(request, pack))
            findings.extend(self._check_blast_radius(request, pack))
            findings.extend(self._check_risk_class(request, pack))
            findings.extend(self._check_maintenance_window(request, pack))
            findings.extend(self._check_customer_contact(request, pack))
            findings.extend(self._check_closure(request, pack))

        blocking = [f for f in findings if f.blocks]
        approvals = [f for f in findings if not f.blocks and f.approval_kind is not None]

        if blocking:
            return self._decision(
                request, outcome=PolicyOutcome.BLOCKED, findings=findings, approval_kind=None
            )
        if approvals:
            chosen = self._most_restrictive(approvals)
            return self._decision(
                request,
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                findings=findings,
                approval_kind=chosen,
            )
        return self._decision(
            request,
            outcome=PolicyOutcome.ALLOWED,
            findings=[
                _Finding(
                    reason_code=ReasonCode.POLICY_ALLOWED,
                    explanation=(
                        f"{request.action_type.value} permitted: every applicable check passed"
                    ),
                    rule=f"remote_actions.{request.action_type.value}",
                    blocks=False,
                )
            ],
            approval_kind=None,
        )

    # -- individual checks -----------------------------------------------------------------------

    def _check_allowlist(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        rule = pack.rule_for(request.action_type)
        if rule is None:
            # The fail-closed path. Reached when an `ActionType` exists in code but has no row --
            # which `PolicyPack._allowlist_is_exhaustive` refuses at load, so in practice this fires
            # only for a pack loaded from an older file than the code. Handled anyway, because "this
            # cannot happen" is how permissive defaults get written.
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_NO_MATCHING_RULE,
                    explanation=(
                        f"{request.action_type.value} has no rule in the policy pack. Absence "
                        "is not permission: add an explicit row, allowed or refused"
                    ),
                    rule="remote_actions.<missing>",
                    inputs={"action_type": request.action_type.value},
                )
            ]
        if not rule.allowed:
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_NO_MATCHING_RULE,
                    explanation=(
                        f"{request.action_type.value} is refused by the policy pack "
                        f"(risk class {rule.risk!r}); no approval can override a refusal"
                    ),
                    rule=f"remote_actions.{request.action_type.value}.allowed=false",
                    inputs={"action_type": request.action_type.value, "allowed": False},
                )
            ]
        return []

    def _check_role(self, request: PolicyInput) -> list[_Finding]:
        # `None` means the caller did not establish a principal. That is a block: an action with no
        # actor cannot be attributed, and `ActionRequest.actor` is one of the six mandatory fields
        # precisely so this case cannot reach an adapter.
        if request.actor_role is None:
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE,
                    explanation=(
                        "no actor role was supplied, so the action cannot be attributed to a "
                        "principal or checked against the tool allowlist"
                    ),
                    rule="rbac.actor_required",
                )
            ]
        if not ToolAllowlist.permits(request.actor_role, request.action_type):
            role_name = (
                request.actor_role.value
                if isinstance(request.actor_role, Role)
                else str(request.actor_role)
            )
            permitted = sorted(r.value for r in ToolAllowlist.roles_permitting(request.action_type))
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE,
                    explanation=(
                        f"role {role_name!r} may not request {request.action_type.value}; "
                        f"roles that may: {', '.join(permitted)}"
                    ),
                    rule="rbac.tool_allowlist",
                    inputs={"actor_role": role_name},
                )
            ]
        return []

    def _check_duplicate(self, request: PolicyInput) -> list[_Finding]:
        if request.idempotency_key and request.idempotency_key in request.executed_idempotency_keys:
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_DUPLICATE_SUPPRESSED,
                    explanation=(
                        f"idempotency key {request.idempotency_key[:16]}... has already been "
                        "executed for this incident; a node replaying after an interrupt must not "
                        "repeat its effect"
                    ),
                    rule="idempotency.already_executed",
                    inputs={"idempotency_key": request.idempotency_key[:16]},
                )
            ]
        return []

    def _check_evidence(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        # Reads are exempt, for the same reason they are exempt from the confidence bar below and
        # stated here rather than assumed: a read is how evidence is *obtained*. Requiring two
        # corroborating sources before permitting the action that produces the first one is a closed
        # loop, and the diagnosis stage would deadlock on its opening call with
        # `evidence_source_count=None` -- which is not a hypothetical, it is what the first
        # `read_status` of every incident looks like.
        #
        # The freshness window and the blocking flags are exempt for the same reason and it is worth
        # spelling out, because those two look safe to keep. They are not: refusing to re-read
        # because the evidence we hold is stale, or because an adapter was unavailable last time,
        # blocks the only action that could fix either condition. Evidence gates the decisions that
        # *consume* evidence. `_READ_ONLY_ACTIONS` consume none -- they change no record, take no
        # approval and are separately gated by `rbac.ToolAllowlist` and the write gate, so the
        # exemption widens no write path.
        if request.action_type in _READ_ONLY_ACTIONS:
            return []

        out: list[_Finding] = []
        cls_ = request.effective_decision_class()
        required = pack.evidence.min_sources_for(cls_)

        if request.evidence_source_count is None:
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_EVIDENCE_INSUFFICIENT,
                    explanation=(
                        f"evidence source count was not measured; {cls_} requires at least "
                        f"{required} corroborating sources and 'unknown' is not 'enough'"
                    ),
                    rule=f"evidence.min_sources_for_{cls_}",
                )
            )
        elif request.evidence_source_count < required:
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_EVIDENCE_INSUFFICIENT,
                    explanation=(
                        f"{request.evidence_source_count} corroborating source(s) for a {cls_} "
                        f"decision, which requires {required}. A single source that agrees with "
                        "itself is not corroboration"
                    ),
                    rule=f"evidence.min_sources_for_{cls_}",
                    inputs={"evidence_source_count": request.evidence_source_count},
                )
            )

        is_dispatch = cls_ == "dispatch"
        max_age = pack.evidence.max_age_minutes_for(dispatch=is_dispatch)
        if request.evidence_age_minutes is not None and request.evidence_age_minutes > max_age:
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_EVIDENCE_INSUFFICIENT,
                    explanation=(
                        f"newest evidence is {request.evidence_age_minutes:.0f} minutes old, past "
                        f"the {max_age}-minute window for a {cls_} decision; the fault may have "
                        "changed or cleared since"
                    ),
                    rule=(
                        "evidence.max_age_for_dispatch_minutes"
                        if is_dispatch
                        else "evidence.max_telemetry_age_minutes"
                    ),
                    inputs={"evidence_age_minutes": round(request.evidence_age_minutes, 1)},
                )
            )

        blocking = sorted(
            f.value for f in set(request.data_quality_flags) & set(pack.evidence.blocking_flags)
        )
        if blocking:
            out.append(
                _Finding(
                    reason_code=ReasonCode.DATA_QUALITY_INSUFFICIENT,
                    explanation=(
                        f"blocking data-quality flags present: {', '.join(blocking)}. These mean "
                        "we are guessing rather than merely uncertain"
                    ),
                    rule="evidence.blocking_flags",
                    inputs={"blocking_flags": blocking},
                )
            )
        return out

    def _check_confidence(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        # Read-only actions do not need a root cause: running a diagnostic is *how* confidence is
        # acquired, and requiring confidence first would deadlock the diagnosis stage before it
        # could gather anything. Keyed on the action, not on the risk class -- an earlier draft
        # sniffed for `max_blast_radius >= 100000`, which read as arithmetic and meant "read_only",
        # and would have silently exempted any future class that happened to have a large cap.
        if request.action_type in _READ_ONLY_ACTIONS:
            return []

        out: list[_Finding] = []
        cls_ = request.effective_decision_class()
        bar = pack.rca.minimum_for(cls_)

        if request.rca_confidence is None:
            out.append(
                _Finding(
                    reason_code=ReasonCode.RCA_LOW_CONFIDENCE,
                    explanation=(
                        f"no root-cause confidence was supplied; {cls_} requires at least {bar:.2f}"
                    ),
                    rule=f"rca.min_for_{cls_}",
                    blocks=False,
                    approval_kind=ApprovalKind.LOW_CONFIDENCE_RCA,
                )
            )
        elif request.rca_confidence < bar:
            deep = request.rca_confidence < pack.rca.review_below
            out.append(
                _Finding(
                    reason_code=ReasonCode.RCA_LOW_CONFIDENCE,
                    explanation=(
                        f"root-cause confidence {request.rca_confidence:.2f} is below the "
                        f"{bar:.2f} required for a {cls_} decision"
                        + (
                            f" and below the {pack.rca.review_below:.2f} review floor, so the "
                            "hypothesis set is weak rather than merely short of the bar"
                            if deep
                            else ""
                        )
                    ),
                    rule=f"rca.min_for_{cls_}",
                    blocks=False,
                    approval_kind=ApprovalKind.LOW_CONFIDENCE_RCA,
                    inputs={"rca_confidence": round(request.rca_confidence, 3)},
                )
            )

        if request.rca_confidence is not None and request.competing_confidence is not None:
            margin = request.rca_confidence - request.competing_confidence
            if abs(margin) < pack.rca.ambiguity_margin:
                out.append(
                    _Finding(
                        reason_code=ReasonCode.RCA_CONFLICTING_EVIDENCE,
                        explanation=(
                            f"the two leading hypotheses are {abs(margin):.3f} apart, inside the "
                            f"{pack.rca.ambiguity_margin:.2f} ambiguity margin: they are "
                            "conflicting, not ranked"
                        ),
                        rule="rca.ambiguity_margin",
                        blocks=False,
                        approval_kind=ApprovalKind.LOW_CONFIDENCE_RCA,
                        inputs={"confidence_margin": round(margin, 3)},
                    )
                )
        return out

    def _check_attempts(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        rule = pack.rule_for(request.action_type)
        if rule is None or rule.max_attempts_key is None:
            # No counter means the action is naturally bounded (a cancellation, a notification).
            # Inventing a limit for those would produce a counter nothing increments.
            return []
        limit = pack.attempt_limits.limit_for(rule.max_attempts_key)
        if limit is None:
            return []
        if request.attempt > limit:
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_ATTEMPT_LIMIT_REACHED,
                    explanation=(
                        f"attempt {request.attempt} of {request.action_type.value} exceeds the "
                        f"limit of {limit}. Repeating an action that has already failed twice is "
                        "not persistence; each retry costs the customer another interruption"
                    ),
                    rule=f"attempt_limits.{rule.max_attempts_key}",
                    inputs={"attempt": request.attempt, "limit": limit},
                )
            ]
        threshold = pack.attempt_limits.require_reason_beyond.get(rule.max_attempts_key)
        if threshold is not None and request.attempt > threshold:
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                    explanation=(
                        f"attempt {request.attempt} of {request.action_type.value} is beyond the "
                        f"{threshold} that proceed without a stated reason; a further attempt "
                        "needs a human to say why this time is different"
                    ),
                    rule="attempt_limits.require_reason_beyond",
                    blocks=False,
                    approval_kind=pack.approval_kind_for(request.action_type)
                    or ApprovalKind.HIGH_RISK_REMOTE_ACTION,
                    inputs={"attempt": request.attempt},
                )
            ]
        return []

    def _check_blast_radius(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        if request.blast_radius is None:
            return []
        out: list[_Finding] = []
        cap = pack.blast_radius_cap_for(request.action_type)
        if cap is not None and request.blast_radius > cap:
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED,
                    explanation=(
                        f"{request.blast_radius} services affected exceeds the {cap} permitted for "
                        f"{request.action_type.value}. An action this wide is a plant "
                        "intervention, not a customer repair"
                    ),
                    rule="risk_classes.max_blast_radius",
                    inputs={"blast_radius": request.blast_radius, "cap": cap},
                )
            )
            return out
        threshold = pack.blast_radius.network_action_threshold
        if request.blast_radius > threshold:
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                    explanation=(
                        f"{request.blast_radius} services affected is past the {threshold} at "
                        "which any action becomes a network event, including customers who never "
                        "reported a fault"
                    ),
                    rule="blast_radius.network_action_threshold",
                    blocks=False,
                    approval_kind=ApprovalKind.HIGH_BLAST_RADIUS_ACTION,
                    inputs={"blast_radius": request.blast_radius},
                )
            )
        return out

    def _check_risk_class(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        rule = pack.rule_for(request.action_type)
        klass = pack.risk_class_for(request.action_type)
        if rule is None or klass is None:
            return []
        # An approval kind on the *action* row requires approval on its own, without needing the
        # risk class to say so. Otherwise `create_work_order: {risk: medium, approval_kind:
        # dispatch}` -- medium being a class that does not require approval -- would send a crew
        # with no dispatch interrupt at all, which is the one thing that row exists to prevent.
        # Naming an interrupt for an action that never raises one is not a configuration anyone
        # means.
        if not klass.requires_approval and rule.approval_kind is None:
            return []
        kind = pack.approval_kind_for(request.action_type)
        if kind is None:
            # Unreachable through a validated pack (`RiskClass._approval_names_its_interrupt` and
            # `PolicyPack._action_rules_reference_defined_things` both refuse it). Blocking rather
            # than allowing, because an approval requirement we cannot route is not one we may skip.
            return [
                _Finding(
                    reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                    explanation=(
                        f"{request.action_type.value} requires approval but no interrupt kind "
                        "resolves; the request cannot be routed to a human and must not proceed"
                    ),
                    rule="risk_classes.approval_kind=<missing>",
                )
            ]
        source = (
            f"remote_actions.{request.action_type.value}.approval_kind"
            if rule.approval_kind is not None
            else f"risk_classes.{rule.risk}"
        )
        return [
            _Finding(
                reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,
                explanation=(
                    f"{request.action_type.value} is in risk class {rule.risk!r}"
                    + ("" if klass.reversible else ", which is not reversible")
                    + f", so a {kind.value} approval is required"
                ),
                rule=source,
                blocks=False,
                approval_kind=kind,
            )
        ]

    def _check_maintenance_window(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        rule = pack.rule_for(request.action_type)
        if rule is None or not rule.requires_maintenance_window or request.in_maintenance_window:
            return []
        return [
            _Finding(
                reason_code=ReasonCode.POLICY_MAINTENANCE_WINDOW_REQUIRED,
                explanation=(
                    f"{request.action_type.value} may only run inside a maintenance window; the "
                    "current time is outside one"
                ),
                rule=f"remote_actions.{request.action_type.value}.requires_maintenance_window",
            )
        ]

    def _check_customer_contact(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        if request.action_type not in CUSTOMER_CONTACT_ACTIONS:
            return []
        out: list[_Finding] = []
        contact = pack.customer_contact

        if request.vulnerable_customer and (
            request.action_type is ActionType.SEND_SELF_HELP
            and contact.vulnerable_customer_skip_self_help
        ):
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_VULNERABLE_CUSTOMER_PROTECTION,
                    explanation=(
                        "this customer is flagged vulnerable and guided self-help assumes someone "
                        "who can climb behind furniture and read a label; the case escalates to a "
                        "human instead"
                    ),
                    rule="customer_contact.vulnerable_customer_skip_self_help",
                )
            )

        if request.local_time is not None and contact.in_quiet_hours(request.local_time):
            override = contact.quiet_hours_override_severity
            if request.severity.rank() < override.rank():
                out.append(
                    _Finding(
                        reason_code=ReasonCode.POLICY_QUIET_HOURS,
                        explanation=(
                            f"{request.local_time.strftime('%H:%M')} local is inside quiet hours "
                            f"({contact.quiet_hours_start:%H:%M}-{contact.quiet_hours_end:%H:%M}) "
                            f"and severity {request.severity.value} does not reach the "
                            f"{override.value} override"
                        ),
                        rule="customer_contact.quiet_hours_start",
                        inputs={"local_time": request.local_time.strftime("%H:%M")},
                    )
                )

        if request.contacts_today >= contact.max_contacts_per_incident_per_day:
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_DUPLICATE_SUPPRESSED,
                    explanation=(
                        f"{request.contacts_today} contact(s) already sent for this incident "
                        f"today, at the cap of {contact.max_contacts_per_incident_per_day}. Past "
                        "that the workflow is talking to itself"
                    ),
                    rule="customer_contact.max_contacts_per_incident_per_day",
                    inputs={"contacts_today": request.contacts_today},
                )
            )
        elif (
            request.minutes_since_last_contact is not None
            and request.minutes_since_last_contact < contact.min_minutes_between_contacts
        ):
            out.append(
                _Finding(
                    reason_code=ReasonCode.POLICY_DUPLICATE_SUPPRESSED,
                    explanation=(
                        f"last contact was {request.minutes_since_last_contact:.0f} minutes ago, "
                        f"inside the {contact.min_minutes_between_contacts}-minute minimum spacing"
                    ),
                    rule="customer_contact.min_minutes_between_contacts",
                    inputs={
                        "minutes_since_last_contact": round(request.minutes_since_last_contact, 1)
                    },
                )
            )
        return out

    def _check_closure(self, request: PolicyInput, pack: PolicyPack) -> list[_Finding]:
        if request.action_type is not ActionType.CLOSE_INCIDENT:
            return []
        out: list[_Finding] = []
        closure = pack.closure

        if closure.require_validation_passed and request.validation_passed is not True:
            if not closure.allow_exceptional_closure:
                out.append(
                    _Finding(
                        reason_code=ReasonCode.VALIDATION_FAILED,
                        explanation=(
                            "closure requires a passed validation and this pack permits no "
                            "exceptional path; a succeeded command and a quiet alarm are not proof"
                        ),
                        rule="closure.require_validation_passed",
                    )
                )
            else:
                out.append(
                    _Finding(
                        reason_code=ReasonCode.VALIDATION_FAILED,
                        explanation=(
                            "closing without a passed validation is the exceptional path: service "
                            "restoration has not been proven over a stability window, so a named "
                            "supervisor must accept that"
                        ),
                        rule="closure.allow_exceptional_closure",
                        blocks=False,
                        approval_kind=ApprovalKind.EXCEPTIONAL_CLOSURE,
                        inputs={"validation_passed": request.validation_passed},
                    )
                )

        if closure.require_reconciliation and request.reconciled is not True:
            out.append(
                _Finding(
                    reason_code=ReasonCode.RECONCILIATION_MISMATCH,
                    explanation=(
                        "linked records in the systems of record named by "
                        "policy.reconciliation.systems ("
                        + ", ".join(pack.reconciliation.systems)
                        + ") have not been reconciled; closing now leaves an open work order or "
                        "MR behind a closed incident"
                    ),
                    rule="closure.require_reconciliation",
                    inputs={
                        "reconciled": request.reconciled,
                        "systems": list(pack.reconciliation.systems),
                    },
                )
            )
        return out

    # -- folding ---------------------------------------------------------------------------------

    def _most_restrictive(self, approvals: list[_Finding]) -> ApprovalKind:
        """Which interrupt to raise when several apply.

        The narrowest approver set wins: satisfying it is the hardest of the requirements, so the
        others are satisfied incidentally by the same signature. Derived from `rbac.approvers_for`
        rather than a second hand-ordered list here -- that list would be a duplicate of the
        approval matrix and would drift from it. Ties break on the kind's name so the choice is
        deterministic; two identical inputs must produce byte-identical decisions or the audit
        trail cannot be reproduced.
        """
        kinds = {f.approval_kind for f in approvals if f.approval_kind is not None}
        return min(kinds, key=lambda k: (len(approvers_for(k)), k.value))

    def _decision(
        self,
        request: PolicyInput,
        *,
        outcome: PolicyOutcome,
        findings: list[_Finding],
        approval_kind: ApprovalKind | None,
    ) -> PolicyDecision:
        # De-duplicate reason codes while preserving the order the checks ran in: the order is the
        # explanation's narrative (allowlist, then role, then evidence, then confidence...), and
        # sorting it would scramble a reader's sense of what went wrong first.
        seen: dict[ReasonCode, None] = {}
        for f in findings:
            seen.setdefault(f.reason_code, None)

        required_role = ""
        if approval_kind is not None and self._pack is not None:
            approval_rule = self._pack.approvals.get(approval_kind)
            if approval_rule is not None:
                required_role = approval_rule.required_role.value

        evaluated: dict[str, Any] = {"decision_class": request.effective_decision_class()}
        for f in findings:
            evaluated.update(f.inputs)

        constraints: dict[str, Any] = {}
        if approval_kind is not None and self._pack is not None:
            approval_rule = self._pack.approvals.get(approval_kind)
            if approval_rule is not None:
                constraints["expires_after_minutes"] = approval_rule.expires_after_minutes
                constraints["escalate_on_expiry"] = approval_rule.escalate_on_expiry
            other = sorted(
                {
                    f.approval_kind.value
                    for f in findings
                    if f.approval_kind is not None and f.approval_kind is not approval_kind
                }
            )
            if other:
                # Recorded rather than dropped: the operator answering the narrowest interrupt
                # should be told which other requirements their signature also satisfies.
                constraints["also_required"] = other
        if self._pack is not None:
            cap = self._pack.blast_radius_cap_for(request.action_type)
            if cap is not None:
                constraints["max_blast_radius"] = cap

        return PolicyDecision(
            decision_id=new_id("POL"),
            decided_at=self._clock.now(),
            action_type=request.action_type,
            outcome=outcome,
            reason_codes=tuple(seen),
            explanation=" ".join(f.explanation for f in findings).strip(),
            policy_version=self.policy_version,
            matched_rule=findings[0].rule if findings else "",
            required_approval_kind=approval_kind,
            required_role=required_role,
            constraints=constraints,
            evaluated_inputs=evaluated,
        )

    # -- helpers the graph and the API need ------------------------------------------------------

    def approval_expiry(self, kind: ApprovalKind, requested_at: datetime) -> datetime | None:
        """When an interrupt of `kind` expires. `None` if the pack sets no expiry.

        Derived from `requested_at` rather than from the clock so that a node replayed after a
        resume computes the same deadline it computed the first time -- otherwise every replay would
        quietly
        extend the operator's window.
        """
        if self._pack is None:
            return None
        rule = self._pack.approvals.get(kind)
        if rule is None:
            return None
        return requested_at + timedelta(minutes=rule.expires_after_minutes)

    def escalates_on_expiry(self, kind: ApprovalKind) -> bool:
        if self._pack is None:
            # An unavailable pack escalates: the alternative is an expired approval that proceeds,
            # which is the one behaviour an approval gate must never have.
            return True
        rule = self._pack.approvals.get(kind)
        return rule.escalate_on_expiry if rule else True

    def threshold(self, name: str, default: float) -> float:
        """A detector threshold, or the detector's own default when the pack is silent or absent."""
        if self._pack is None:
            return default
        return self._pack.threshold(name, default)
