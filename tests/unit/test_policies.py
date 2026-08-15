"""The policy layer, verified by execution against the real `pack.yaml`.

This is the authorization surface: 2,600 lines whose entire job is to answer "may this happen?" and
whose only acceptable failure mode is answering *no* when it is unsure. Nothing here is mocked. Every
test drives the shipped pack through the real loader and the real engine, because a policy test
against a synthetic pack proves the engine works on a pack nobody deploys.

The standard is falsification, not coverage, and three habits follow from it.

**Every validator is driven to failure on its own.** `test_a_pack_that_breaks_one_rule_is_refused`
parametrises all twenty-six, each with the rest of the pack intact. A validator only ever observed
firing behind another is a validator nobody has tested: it could be reading the wrong field, and the
suite would stay green because a neighbour raised first. Each case also asserts the message names its
row -- an error that refuses a 544-line file without saying which line is a refusal nobody can act
on.

**Refusals are asserted to be reachable, not merely present.** A test that only ever sees `BLOCKED`
cannot tell a working gate from one wired to `return True`, so every gate below is exercised in both
directions: the input that trips it and the neighbouring input that does not.

**`None` and `0` are held apart.** `PolicyInput` uses `None` for "not measured" and `0` for "measured
as none", and both must block for different reasons with different fixes. Tests that assert only the
outcome would pass against a collapse of the two; the tests here assert the explanations differ.

Four tests are marked REGRESSION and each names a defect found by running this code:

* `test_regression_the_first_read_of_an_incident_is_not_blocked_for_want_of_evidence` -- the evidence
  gate applied to `read_status` and `run_diagnostic`. Requiring two corroborating sources before
  permitting the action that produces the first one is a closed loop, and the diagnosis stage
  deadlocked on its opening call. `_check_confidence` had documented and taken this exemption;
  `_check_evidence` had not.
* `test_regression_two_unavailable_engines_do_not_share_a_version` -- `policy_version` returned a
  bare `"unavailable"` for every failure, so a missing pack and a malformed threshold produced
  byte-identical audit records. Its own docstring promised `unavailable+<reason digest>`.
* `test_regression_an_approval_gate_names_a_role_that_can_answer_it` -- an earlier `pack.yaml` named
  `senior_engineer`, `dispatch_supervisor`, `field_supervisor` and `assurance_manager` as required
  roles. `Role` defines none of them, so all four gates named a role no principal can hold: an
  approval that can never be granted, discovered at 02:00 by whoever is on call.
* `test_regression_an_unknown_decision_class_gets_the_strictest_bar` -- the fail-closed rule applied
  to a lookup. A misspelled `decision_class` at a new call site must make the engine harder to
  satisfy, not easier. Asserted as an *ordering* against a known class, because `== 3` passes against
  a function that returns a constant.
"""

from __future__ import annotations

import copy
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, time
from typing import Any

import pytest
import yaml

from lpr_cpe.domain.enums import (
    ActionType,
    ApprovalKind,
    DataQualityFlag,
    HealthBand,
    PolicyOutcome,
    ReasonCode,
    Severity,
)
from lpr_cpe.policies.engine import PolicyEngine, PolicyInput
from lpr_cpe.policies.loader import (
    DEFAULT_PACK_PATH,
    PolicyPackError,
    canonical_digest,
    load_pack,
    parse_pack,
    policy_version,
)
from lpr_cpe.security.rbac import Role, approvers_for

#: A fixed instant. `PolicyDecision.decided_at` comes from the engine's clock, and a test that let it
#: come from the wall clock would be asserting against a value that changes between runs.
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

#: For the one test that re-enters the interpreter to observe a cross-process property. Derived from
#: this file rather than the working directory, so it holds under `pytest` run from anywhere.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# -------------------------------------------------------------------------------------------------
# Fixtures and builders
# -------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pack_text() -> str:
    return DEFAULT_PACK_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pack() -> Any:
    """The real shipped pack, through the real loader."""
    return load_pack()


@pytest.fixture(scope="module")
def engine(pack: Any) -> PolicyEngine:
    return PolicyEngine(pack)


def req(**over: Any) -> PolicyInput:
    """A request that passes every check, so exactly one field at a time can be made bad.

    A builder rather than a fixture because half these tests need two requests that differ in one
    field, and the comparison is the assertion.
    """
    base: dict[str, Any] = {
        "action_type": ActionType.CPE_REBOOT,
        "incident_id": "INC-1",
        "target_ref": "CPE-1",
        "actor_role": Role.AUTOMATION,
        "rca_confidence": 0.90,
        "evidence_source_count": 4,
        "evidence_age_minutes": 2.0,
        "attempt": 1,
        "blast_radius": 1,
        "severity": Severity.HIGH,
    }
    base.update(over)
    return PolicyInput(**base)


#: A work order: `medium` risk class (which does not require approval) with `approval_kind: dispatch`
#: on the action row. Every field but `attempt` satisfied.
WORK_ORDER = {
    "action_type": ActionType.CREATE_WORK_ORDER,
    "actor_role": Role.NOC_OPERATOR,
    "evidence_source_count": 3,
    "evidence_age_minutes": 2.0,
    "blast_radius": 1,
}
#: A node reset: the `network` risk class, cap 2000, past the 25-service network threshold.
NODE_RESET = {
    "action_type": ActionType.NODE_LEVEL_RESET,
    "actor_role": Role.NOC_SUPERVISOR,
    "evidence_source_count": 4,
    "evidence_age_minutes": 2.0,
}
#: An outbound contact, in daylight, with the contact counters clear.
NOTIFY = {
    "action_type": ActionType.NOTIFY_CUSTOMER,
    "actor_role": Role.NOC_OPERATOR,
    "evidence_source_count": 3,
    "blast_radius": 1,
    "local_time": time(10, 0),
}
#: A closure with both preconditions met.
CLOSE = {
    "action_type": ActionType.CLOSE_INCIDENT,
    "actor_role": Role.NOC_OPERATOR,
    "evidence_source_count": 3,
    "blast_radius": 1,
    "validation_passed": True,
    "reconciled": True,
}


def variant(pack_text: str, **edits: Any) -> str:
    """The shipped pack with sections deep-merged, as YAML text.

    Deep-merged rather than replaced so a case can change one threshold and inherit the other 540
    lines: a test that rebuilds a whole pack is a test of the pack it built.
    """

    raw = yaml.safe_load(pack_text)

    def merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                merge(dst[key], value)
            else:
                dst[key] = value
        return dst

    merge(raw, copy.deepcopy(edits))
    return yaml.safe_dump(raw)


def without(pack_text: str, *path: str) -> str:
    """The shipped pack with one key removed. `variant` merges, so it cannot express a deletion."""
    raw = yaml.safe_load(pack_text)
    cursor = raw
    for key in path[:-1]:
        cursor = cursor[key]
    del cursor[path[-1]]
    return yaml.safe_dump(raw)


# =================================================================================================
# Failing closed
# =================================================================================================


class TestFailsClosed:
    """The layer's whole premise: absence is never permission."""

    def test_an_unavailable_engine_blocks_every_action_type(self) -> None:
        # Not "some actions" and not "writes": all twenty-one, including the reads. An engine that
        # cannot consult a pack has no basis for permitting anything.
        dead = PolicyEngine(None, unavailable_reason="pack.yaml failed validation")
        for action in ActionType:
            decision = dead.evaluate(req(action_type=action, actor_role=Role.ADMIN))
            assert decision.outcome is PolicyOutcome.BLOCKED, action.value
            assert ReasonCode.POLICY_NO_MATCHING_RULE in decision.reason_codes, action.value
            assert "unavailable" in decision.explanation

    def test_an_engine_with_neither_a_pack_nor_a_reason_is_refused(self) -> None:
        # The failure mode this guards is an engine that blocks everything without being able to say
        # why, which is unauditable.
        with pytest.raises(ValueError, match="either a pack or an unavailable_reason"):
            PolicyEngine(None)

    def test_the_pack_property_raises_rather_than_returning_a_default(self) -> None:
        # A default pack here would be a set of thresholds nobody reviewed, applied silently at
        # exactly the moment the reviewed ones could not be read.
        dead = PolicyEngine(None, unavailable_reason="boom")
        assert dead.available is False
        with pytest.raises(PolicyPackError, match="boom"):
            _ = dead.pack

    def test_an_unavailable_engine_always_escalates_an_expiry(self) -> None:
        # The alternative is an expired approval that proceeds, which is the one behaviour an
        # approval gate must never have.
        dead = PolicyEngine(None, unavailable_reason="boom")
        assert all(dead.escalates_on_expiry(kind) for kind in ApprovalKind)
        assert dead.approval_expiry(ApprovalKind.DISPATCH, NOW) is None

    def test_an_unavailable_engine_still_reports_a_detector_default(self) -> None:
        # `threshold` is the one helper that degrades permissively, and deliberately: the fallback is
        # the detector's own stated default, not a permissive constant, so a detector keeps working
        # to the value it was written with rather than to nothing.
        dead = PolicyEngine(None, unavailable_reason="boom")
        assert dead.threshold("any_name", 0.42) == 0.42

    def test_an_action_with_no_rule_is_blocked_and_says_why(self, pack: Any) -> None:
        # `_allowlist_is_exhaustive` refuses such a pack at load, so this branch is only reachable
        # through a pack built past validation -- which is exactly a pack loaded from a file older
        # than the code, and the case "this cannot happen" is how permissive defaults get written.
        thinned = pack.model_copy(
            update={
                "remote_actions": {
                    action: rule
                    for action, rule in pack.remote_actions.items()
                    if action is not ActionType.CPE_REBOOT
                }
            }
        )
        decision = PolicyEngine(thinned).evaluate(req())
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "Absence is not permission" in decision.explanation
        assert decision.matched_rule == "remote_actions.<missing>"

    def test_a_refused_action_cannot_be_approved_around(self, engine: PolicyEngine) -> None:
        # `bulk_config_push` is the one row set `allowed: false`. An admin with perfect evidence and
        # a blast radius of one still cannot have it, and -- the load-bearing half -- no interrupt is
        # offered, because an approval implies there is a signature that would unblock this.
        decision = engine.evaluate(
            req(
                action_type=ActionType.BULK_CONFIG_PUSH,
                actor_role=Role.ADMIN,
                rca_confidence=1.0,
                evidence_source_count=9,
                blast_radius=1,
            )
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert decision.required_approval_kind is None
        assert "no approval can override a refusal" in decision.explanation

    def test_regression_two_unavailable_engines_do_not_share_a_version(self) -> None:
        """REGRESSION: every unavailability collapsed into one indistinguishable version string.

        `policy_version` returned a bare `"unavailable"` whatever the cause, while its own docstring
        promised `unavailable+<reason digest>`. A reviewer reading a month of blocked decisions could
        not tell whether they had one cause or twenty -- and the audit trail's only means of
        reconstructing a past decision is the version string.
        """
        missing = PolicyEngine(None, unavailable_reason="policy pack not found at /etc/pack.yaml")
        malformed = PolicyEngine(None, unavailable_reason="rca.review_below is at or above ...")

        assert missing.policy_version != malformed.policy_version
        # Both are still recognisably unavailable: the prefix is what `/health` and the audit reader
        # key on, and a digest alone would be unreadable.
        assert missing.policy_version.startswith("unavailable+")
        assert malformed.policy_version.startswith("unavailable+")
        # Deterministic, or two replays of the same failure would disagree.
        assert (
            missing.policy_version
            == PolicyEngine(
                None, unavailable_reason="policy pack not found at /etc/pack.yaml"
            ).policy_version
        )
        # And it is on the decision, not merely on the engine.
        assert missing.evaluate(req()).policy_version == missing.policy_version


# =================================================================================================
# Role and duplicate suppression
# =================================================================================================


class TestActorAndReplay:
    def test_an_action_with_no_actor_is_blocked_as_unattributable_not_as_unpermitted(
        self, engine: PolicyEngine
    ) -> None:
        """Both `_check_role` branches share one reason code, so the rule is what separates them.

        "No principal was established" and "this principal may not do that" are different faults
        with different fixes -- the first is a caller that never authenticated, the second is a
        permissions question -- and asserting only `BLOCKED` cannot tell them apart. Measured:
        deleting the no-actor branch entirely leaves this action blocked anyway, because
        `ToolAllowlist.permits(None, ...)` is falsy, so an outcome-only assertion stays green while
        the engine reports the wrong cause. `matched_rule` is the field that distinguishes them.
        """
        decision = engine.evaluate(req(actor_role=None))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE in decision.reason_codes
        assert decision.matched_rule == "rbac.actor_required"

    def test_a_principal_who_may_not_act_is_blocked_by_the_allowlist_not_by_attribution(
        self, engine: PolicyEngine
    ) -> None:
        # The other side of the pair above. Same reason code, different rule: here a principal was
        # established and the allowlist is what refused.
        decision = engine.evaluate(
            req(action_type=ActionType.CPE_FACTORY_RESET, actor_role=Role.AUTOMATION)
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_ACTION_NOT_PERMITTED_FOR_ROLE in decision.reason_codes
        assert decision.matched_rule == "rbac.tool_allowlist"

    def test_an_unknown_role_string_is_blocked_rather_than_raising(
        self, engine: PolicyEngine
    ) -> None:
        # An API handler passes what the token carried without pre-parsing it, and must get a
        # refusal rather than a 500.
        decision = engine.evaluate(req(actor_role="not_a_role"))
        assert decision.outcome is PolicyOutcome.BLOCKED

    def test_automation_may_not_request_a_factory_reset(self, engine: PolicyEngine) -> None:
        # The graph running unattended is not a named human. Asserted with everything else perfect,
        # so the refusal can only be the role.
        decision = engine.evaluate(
            req(
                action_type=ActionType.CPE_FACTORY_RESET,
                actor_role=Role.AUTOMATION,
                rca_confidence=0.99,
                evidence_source_count=5,
            )
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        # The refusal names who *may*, or the operator is told no without being told the route.
        assert "noc_operator" in decision.explanation

    def test_the_same_action_is_permitted_for_a_role_that_holds_it(
        self, engine: PolicyEngine
    ) -> None:
        # The other half of the assertion above. Without it, a `permits()` wired to `return False`
        # would pass every role test in this file.
        decision = engine.evaluate(
            req(
                action_type=ActionType.CPE_FACTORY_RESET,
                actor_role=Role.NOC_OPERATOR,
                rca_confidence=0.99,
                evidence_source_count=5,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL

    def test_an_auditor_may_read_status_but_not_run_a_diagnostic(
        self, engine: PolicyEngine
    ) -> None:
        # A diagnostic takes a line out of service briefly, so it changes the customer's experience
        # even though it changes no record. Both directions, on one role.
        allowed = engine.evaluate(req(action_type=ActionType.READ_STATUS, actor_role=Role.AUDITOR))
        refused = engine.evaluate(
            req(action_type=ActionType.RUN_DIAGNOSTIC, actor_role=Role.AUDITOR)
        )
        assert allowed.outcome is PolicyOutcome.ALLOWED
        assert refused.outcome is PolicyOutcome.BLOCKED

    def test_a_replayed_idempotency_key_is_suppressed(self, engine: PolicyEngine) -> None:
        # What makes a node replaying after an interrupt a no-op rather than a second reboot.
        replayed = engine.evaluate(
            req(idempotency_key="k1", executed_idempotency_keys=frozenset({"k1"}))
        )
        fresh = engine.evaluate(
            req(idempotency_key="k2", executed_idempotency_keys=frozenset({"k1"}))
        )
        assert replayed.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_DUPLICATE_SUPPRESSED in replayed.reason_codes
        assert fresh.outcome is PolicyOutcome.ALLOWED

    def test_an_empty_idempotency_key_is_not_a_duplicate(self, engine: PolicyEngine) -> None:
        # `""` means the caller did not derive one. Treating it as a key would make the second
        # keyless action of an incident a duplicate of the first.
        decision = engine.evaluate(
            req(idempotency_key="", executed_idempotency_keys=frozenset({""}))
        )
        assert decision.outcome is PolicyOutcome.ALLOWED


# =================================================================================================
# Evidence
# =================================================================================================


class TestEvidence:
    def test_unmeasured_and_measured_zero_block_for_different_reasons(
        self, engine: PolicyEngine
    ) -> None:
        # The distinction is load-bearing and invisible in the outcome, so the outcome is not what is
        # asserted. `None` is a caller bug; `0` is a data problem; collapsing them hides the first
        # behind the second, and they have different fixes.
        unmeasured = engine.evaluate(req(evidence_source_count=None))
        measured_zero = engine.evaluate(req(evidence_source_count=0))

        assert unmeasured.outcome is PolicyOutcome.BLOCKED
        assert measured_zero.outcome is PolicyOutcome.BLOCKED
        assert unmeasured.explanation != measured_zero.explanation
        assert "not measured" in unmeasured.explanation
        # And the audit record keeps them apart: an unmeasured count is absent from the inputs
        # rather than recorded as a zero somebody would later read as a measurement.
        assert "evidence_source_count" not in unmeasured.evaluated_inputs
        assert measured_zero.evaluated_inputs["evidence_source_count"] == 0

    def test_dispatch_needs_more_corroboration_than_a_diagnosis(self, engine: PolicyEngine) -> None:
        # Asserted as a comparison between two actions at the same source count, not against the
        # literal 3: the property is that sending a crew costs more proof than cancelling one.
        dispatch = engine.evaluate(
            req(**{**WORK_ORDER, "evidence_source_count": 2}, rca_confidence=0.9)
        )
        diagnosis = engine.evaluate(
            req(
                action_type=ActionType.CANCEL_WORK_ORDER,
                actor_role=Role.NOC_OPERATOR,
                evidence_source_count=2,
                rca_confidence=0.9,
                blast_radius=1,
            )
        )
        assert dispatch.outcome is PolicyOutcome.BLOCKED
        assert diagnosis.outcome is PolicyOutcome.ALLOWED

    def test_regression_an_unknown_decision_class_gets_the_strictest_bar(self, pack: Any) -> None:
        """REGRESSION: the fail-closed rule, applied to a lookup rather than to a gate.

        A new call site that misspells its `decision_class` must be held to the *highest* bar in the
        section, not the lowest. The first is a visible bug report from an operator; the second is an
        action that should not have happened.

        Asserted as an ordering against a known class as well as against the maximum. `== 3` passes
        against a function that ignores its argument and returns a constant.
        """
        strictest_sources = max(
            pack.evidence.min_sources_for_diagnosis,
            pack.evidence.min_sources_for_remote_action,
            pack.evidence.min_sources_for_dispatch,
            pack.evidence.min_sources_for_closure,
        )
        assert pack.evidence.min_sources_for("typo") == strictest_sources
        assert pack.evidence.min_sources_for("typo") > pack.evidence.min_sources_for("diagnosis")

        strictest_confidence = max(
            pack.rca.min_for_autonomous_action,
            pack.rca.min_for_remote_action,
            pack.rca.min_for_dispatch,
            pack.rca.min_for_mr,
        )
        assert pack.rca.minimum_for("typo") == strictest_confidence
        assert pack.rca.minimum_for("typo") > pack.rca.minimum_for("remote_action")

    def test_a_caller_supplied_decision_class_overrides_the_action_default(
        self, engine: PolicyEngine
    ) -> None:
        # A reboot defaults to `remote_action` (2 sources). Declaring it a dispatch decision raises
        # the bar to 3 without changing the action, which is how a node asks "hold this to the
        # dispatch standard".
        default = engine.evaluate(req(evidence_source_count=2))
        overridden = engine.evaluate(req(evidence_source_count=2, decision_class="dispatch"))
        assert default.outcome is PolicyOutcome.ALLOWED
        assert overridden.outcome is PolicyOutcome.BLOCKED
        assert overridden.evaluated_inputs["decision_class"] == "dispatch"

    def test_evidence_past_the_freshness_window_blocks(self, engine: PolicyEngine) -> None:
        stale = engine.evaluate(req(evidence_age_minutes=20.0))
        fresh = engine.evaluate(req(evidence_age_minutes=10.0))
        assert stale.outcome is PolicyOutcome.BLOCKED
        assert fresh.outcome is PolicyOutcome.ALLOWED

    def test_the_dispatch_freshness_window_is_the_one_that_applies_to_dispatch(
        self, pack: Any
    ) -> None:
        # Two windows exist and picking the wrong one is invisible in the permissive direction, so
        # the selector is asserted directly rather than only through a decision.
        assert pack.evidence.max_age_minutes_for(
            dispatch=True
        ) != pack.evidence.max_age_minutes_for(dispatch=False)
        assert (
            pack.evidence.max_age_minutes_for(dispatch=True)
            == pack.evidence.max_age_for_dispatch_minutes
        )

    def test_an_unmeasured_age_does_not_block(self, engine: PolicyEngine) -> None:
        # Unlike the source count. The count is the corroboration gate and "unknown" fails it; the
        # age has no meaning without evidence to be old, and the count already caught that case.
        decision = engine.evaluate(req(evidence_age_minutes=None))
        assert decision.outcome is PolicyOutcome.ALLOWED

    @pytest.mark.parametrize(
        "flag",
        [
            DataQualityFlag.ADAPTER_UNAVAILABLE,
            DataQualityFlag.CONFLICTING_SOURCES,
            DataQualityFlag.INCONSISTENT_TOPOLOGY,
        ],
    )
    def test_each_blocking_flag_blocks_on_its_own(
        self, engine: PolicyEngine, flag: DataQualityFlag
    ) -> None:
        decision = engine.evaluate(req(data_quality_flags=(flag,)))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.DATA_QUALITY_INSUFFICIENT in decision.reason_codes
        assert flag.value in decision.explanation

    def test_stale_data_is_not_a_blocking_flag(self, engine: PolicyEngine) -> None:
        # Deliberate, and the reason is worth pinning: staleness is handled by the freshness windows,
        # which say how stale is too stale *per decision*. Duplicating it as a hard block would stop
        # every action on data sixteen minutes old.
        decision = engine.evaluate(req(data_quality_flags=(DataQualityFlag.STALE_DATA,)))
        assert decision.outcome is PolicyOutcome.ALLOWED

    @pytest.mark.parametrize("action", [ActionType.READ_STATUS, ActionType.RUN_DIAGNOSTIC])
    def test_regression_the_first_read_of_an_incident_is_not_blocked_for_want_of_evidence(
        self, engine: PolicyEngine, action: ActionType
    ) -> None:
        """REGRESSION: the evidence gate deadlocked the stage that produces evidence.

        `_check_evidence` applied to reads. The opening `read_status` of every incident carries
        `evidence_source_count=None` -- nothing has been read yet -- and was refused for wanting two
        corroborating sources it was about to produce. There is no input that escapes the loop.

        `_check_confidence` had already reasoned its way to this exemption and taken it;
        `_check_evidence` had not, which is what made the inconsistency findable by running the two
        side by side.

        The second half of the test is the one that matters: the exemption must not widen a write
        path. A read is exempt because it consumes no evidence, not because reads are trusted.
        """
        opening_call = engine.evaluate(
            req(
                action_type=action,
                actor_role=Role.AUTOMATION,
                evidence_source_count=None,
                evidence_age_minutes=None,
                rca_confidence=None,
            )
        )
        assert opening_call.outcome is PolicyOutcome.ALLOWED, opening_call.explanation

        # The worst possible evidence is exactly when re-reading is most necessary: refusing to
        # re-read because the last read failed blocks the only action that could fix the condition.
        retry_after_failure = engine.evaluate(
            req(
                action_type=action,
                actor_role=Role.AUTOMATION,
                evidence_source_count=0,
                evidence_age_minutes=999.0,
                data_quality_flags=(DataQualityFlag.ADAPTER_UNAVAILABLE,),
            )
        )
        assert retry_after_failure.outcome is PolicyOutcome.ALLOWED

        # A write with those same inputs is still refused -- the exemption is about what the action
        # consumes, not about who is asking.
        write = engine.evaluate(
            req(
                evidence_source_count=None,
                evidence_age_minutes=999.0,
                data_quality_flags=(DataQualityFlag.ADAPTER_UNAVAILABLE,),
            )
        )
        assert write.outcome is PolicyOutcome.BLOCKED

        # And a read is still subject to every check that is not about evidence.
        assert (
            engine.evaluate(
                req(
                    action_type=action,
                    actor_role=Role.AUTOMATION,
                    idempotency_key="k",
                    executed_idempotency_keys=frozenset({"k"}),
                )
            ).outcome
            is PolicyOutcome.BLOCKED
        )


# =================================================================================================
# Confidence
# =================================================================================================


class TestConfidence:
    def test_low_confidence_asks_a_human_rather_than_blocking(self, engine: PolicyEngine) -> None:
        # Blocking would leave the incident with nowhere to go. The `low_confidence_rca` interrupt
        # exists precisely so a human can look at an ambiguous hypothesis set and decide.
        decision = engine.evaluate(req(rca_confidence=0.10))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert decision.required_approval_kind is ApprovalKind.LOW_CONFIDENCE_RCA

    def test_deep_doubt_and_a_near_miss_reach_the_same_interrupt_with_different_words(
        self, engine: PolicyEngine
    ) -> None:
        # The severity of the doubt changes what the operator is told, not who decides. Asserting
        # only the outcome would pass against an engine that says the same thing both times, and the
        # operator would have no way to tell "weak hypothesis set" from "just short of the bar".
        below_review_floor = engine.evaluate(req(rca_confidence=0.10))
        just_short = engine.evaluate(req(rca_confidence=0.60))

        assert below_review_floor.outcome is just_short.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert below_review_floor.required_approval_kind is just_short.required_approval_kind
        assert "review floor" in below_review_floor.explanation
        assert "review floor" not in just_short.explanation

    def test_unmeasured_confidence_asks_a_human(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(req(rca_confidence=None))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert ReasonCode.RCA_LOW_CONFIDENCE in decision.reason_codes

    def test_near_tied_hypotheses_conflict_even_when_both_are_confident(
        self, engine: PolicyEngine
    ) -> None:
        # 0.90 clears every bar in the pack. It is the *margin* that is the finding: ranking two
        # hypotheses 0.05 apart is how a confident-looking RCA result gets built out of a coin flip.
        tied = engine.evaluate(req(rca_confidence=0.90, competing_confidence=0.85))
        separated = engine.evaluate(req(rca_confidence=0.90, competing_confidence=0.70))

        assert tied.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert ReasonCode.RCA_CONFLICTING_EVIDENCE in tied.reason_codes
        assert separated.outcome is PolicyOutcome.ALLOWED

    def test_no_competitor_supplied_raises_no_ambiguity_finding(self, engine: PolicyEngine) -> None:
        # `None` here means "there was no runner-up", not "the runner-up was zero". A zero would make
        # the margin maximal and pass anyway, so this asserts the branch rather than the arithmetic.
        decision = engine.evaluate(req(rca_confidence=0.90, competing_confidence=None))
        assert decision.outcome is PolicyOutcome.ALLOWED

    @pytest.mark.parametrize("action", [ActionType.READ_STATUS, ActionType.RUN_DIAGNOSTIC])
    def test_a_read_needs_no_root_cause(self, engine: PolicyEngine, action: ActionType) -> None:
        # Running a diagnostic is *how* confidence is acquired. The exemption is keyed on the action
        # rather than sniffed from the risk class's large blast-radius cap, which would silently
        # exempt any future class that happened to have one.
        decision = engine.evaluate(
            req(action_type=action, actor_role=Role.AUTOMATION, rca_confidence=0.0)
        )
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_a_write_at_the_same_confidence_is_not_exempt(self, engine: PolicyEngine) -> None:
        # The other side of the exemption above, without which a `_READ_ONLY_ACTIONS` containing
        # every action would pass.
        decision = engine.evaluate(req(rca_confidence=0.0))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL


# =================================================================================================
# Attempts, blast radius, risk class
# =================================================================================================


class TestLimits:
    def test_the_attempt_limit_is_reachable_and_not_reached_early(
        self, engine: PolicyEngine
    ) -> None:
        # `remote: 2`. Both sides asserted: a limit that blocked the second attempt would be a
        # different policy, and one that never blocks is not a limit.
        assert engine.evaluate(req(attempt=2)).outcome is PolicyOutcome.ALLOWED
        third = engine.evaluate(req(attempt=3))
        assert third.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_ATTEMPT_LIMIT_REACHED in third.reason_codes

    def test_an_action_with_no_counter_is_not_attempt_limited(self, engine: PolicyEngine) -> None:
        # `notify_customer` has no `max_attempts_key`: it is naturally bounded by the contact caps,
        # and inventing a limit would produce a counter nothing increments.
        decision = engine.evaluate(req(**NOTIFY, rca_confidence=0.9, attempt=99))
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_a_second_work_order_needs_a_stated_reason_and_a_third_is_refused(
        self, engine: PolicyEngine
    ) -> None:
        # `require_reason_beyond.work_order: 1` under a hard limit of 2. Three outcomes across three
        # attempts, which is the only way to show the soft gate sits *between* the other two.
        outcomes = [
            engine.evaluate(req(**WORK_ORDER, rca_confidence=0.9, attempt=n)).outcome
            for n in (1, 2, 3)
        ]
        assert outcomes == [
            PolicyOutcome.REQUIRES_APPROVAL,  # the dispatch interrupt, from the action row
            PolicyOutcome.REQUIRES_APPROVAL,  # plus the stated reason
            PolicyOutcome.BLOCKED,
        ]
        second = engine.evaluate(req(**WORK_ORDER, rca_confidence=0.9, attempt=2))
        assert "needs a human to say why this time is different" in second.explanation

    def test_a_blast_radius_past_the_cap_blocks(self, engine: PolicyEngine) -> None:
        # `cpe_reboot` is risk class `low`, cap 1. "Reboot everything behind the node" is a different
        # action with a different risk class, not a bulk version of this one.
        over = engine.evaluate(req(blast_radius=2))
        at_cap = engine.evaluate(req(blast_radius=1))
        assert over.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED in over.reason_codes
        assert at_cap.outcome is PolicyOutcome.ALLOWED

    def test_past_the_network_threshold_an_action_becomes_a_network_event(
        self, engine: PolicyEngine
    ) -> None:
        # Under its class cap of 2000 but past the 25 at which any action affects customers who never
        # reported a fault.
        decision = engine.evaluate(req(**NODE_RESET, rca_confidence=0.95, blast_radius=100))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert decision.required_approval_kind is ApprovalKind.HIGH_BLAST_RADIUS_ACTION

    def test_past_the_class_cap_it_is_refused_rather_than_escalated(
        self, engine: PolicyEngine
    ) -> None:
        # The distinction the check exists for: 100 services is a question, 5000 is an answer. An
        # interrupt here would put to a human a decision whose answer cannot be honoured.
        decision = engine.evaluate(req(**NODE_RESET, rca_confidence=0.95, blast_radius=5000))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert decision.required_approval_kind is None

    def test_an_unmeasured_blast_radius_raises_no_finding(self, engine: PolicyEngine) -> None:
        # Unlike evidence, where unknown blocks. A blast radius is derived by the delimiter service
        # and its absence means the action is not against a shared element at all.
        assert engine.evaluate(req(blast_radius=None)).outcome is PolicyOutcome.ALLOWED

    def test_the_tighter_of_the_action_cap_and_the_class_cap_wins(self, pack: Any) -> None:
        # Asserted through the accessor rather than a decision, because the property is `min`, and a
        # decision can only show which side was tighter in the pack as shipped.
        tightened = pack.model_copy(
            update={
                "remote_actions": {
                    **pack.remote_actions,
                    ActionType.NODE_LEVEL_RESET: pack.remote_actions[
                        ActionType.NODE_LEVEL_RESET
                    ].model_copy(update={"max_blast_radius": 10}),
                }
            }
        )
        assert pack.blast_radius_cap_for(ActionType.NODE_LEVEL_RESET) == 2000
        assert tightened.blast_radius_cap_for(ActionType.NODE_LEVEL_RESET) == 10

    def test_an_irreversible_action_says_so_in_its_explanation(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(
            req(
                action_type=ActionType.CPE_FIRMWARE_UPDATE,
                actor_role=Role.NOC_OPERATOR,
                rca_confidence=0.95,
                evidence_source_count=4,
                blast_radius=1,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert decision.required_approval_kind is ApprovalKind.HIGH_RISK_REMOTE_ACTION
        assert "not reversible" in decision.explanation
        assert decision.required_role == "noc_operator"
        assert decision.constraints["expires_after_minutes"] == 120

    def test_an_action_row_can_require_approval_a_permissive_class_would_not(
        self, engine: PolicyEngine, pack: Any
    ) -> None:
        # `create_work_order` is `medium`, a class with `requires_approval: false`. Without the
        # action row's own `approval_kind`, sending a crew would raise no dispatch interrupt at all
        # -- the one thing that row exists to prevent.
        assert pack.risk_classes["medium"].requires_approval is False
        decision = engine.evaluate(req(**WORK_ORDER, rca_confidence=0.9, attempt=1))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert decision.required_approval_kind is ApprovalKind.DISPATCH

    def test_an_mr_raises_the_handover_interrupt(self, engine: PolicyEngine) -> None:
        # The operator is being asked about a Clean-to-Dirty handover, and the question they see has
        # to match the decision they are making.
        decision = engine.evaluate(
            req(
                action_type=ActionType.RAISE_MR,
                actor_role=Role.NOC_OPERATOR,
                rca_confidence=0.9,
                evidence_source_count=4,
                blast_radius=1,
            )
        )
        assert decision.required_approval_kind is ApprovalKind.CLEAN_TO_DIRTY_HANDOVER
        assert decision.required_role == "osp_engineer"

    def test_a_maintenance_window_requirement_is_reachable(self, pack_text: str) -> None:
        # No shipped action sets `requires_maintenance_window`, so the check would be dead code the
        # tests never reach. A pack that turns it on proves the gate is wired, in both directions.
        gated = parse_pack(
            variant(
                pack_text,
                remote_actions={
                    "olt_port_reset": {
                        "allowed": True,
                        "risk": "network",
                        "requires_maintenance_window": True,
                    }
                },
            )
        )
        engine = PolicyEngine(gated)
        base = {
            "action_type": ActionType.OLT_PORT_RESET,
            "actor_role": Role.NOC_SUPERVISOR,
            "rca_confidence": 0.95,
            "evidence_source_count": 4,
            "blast_radius": 1,
        }
        outside = engine.evaluate(req(**base, in_maintenance_window=False))
        inside = engine.evaluate(req(**base, in_maintenance_window=True))

        assert outside.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_MAINTENANCE_WINDOW_REQUIRED in outside.reason_codes
        assert inside.outcome is PolicyOutcome.REQUIRES_APPROVAL


# =================================================================================================
# Customer contact
# =================================================================================================


class TestCustomerContact:
    @pytest.mark.parametrize(
        ("local", "quiet"),
        [
            (time(22, 0), True),
            (time(2, 0), True),  # the wrap: `start <= t <= end` gets this exactly backwards
            (time(13, 0), False),
            (time(21, 0), True),  # start is inclusive
            (time(7, 0), False),  # end is exclusive
            (time(6, 59), True),
        ],
    )
    def test_quiet_hours_wrap_midnight(self, pack: Any, local: time, quiet: bool) -> None:
        assert pack.customer_contact.in_quiet_hours(local) is quiet

    def test_a_quiet_hours_contact_is_blocked_unless_the_severity_overrides(
        self, engine: PolicyEngine
    ) -> None:
        # Waking someone at 03:00 to tell them their Wi-Fi is slow is a worse customer outcome than
        # telling them at 08:00. A critical incident is a different question.
        high = engine.evaluate(
            req(**{**NOTIFY, "local_time": time(2, 0)}, rca_confidence=0.9, severity=Severity.HIGH)
        )
        critical = engine.evaluate(
            req(
                **{**NOTIFY, "local_time": time(2, 0)},
                rca_confidence=0.9,
                severity=Severity.CRITICAL,
            )
        )
        assert high.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_QUIET_HOURS in high.reason_codes
        assert critical.outcome is PolicyOutcome.ALLOWED

    def test_no_local_time_raises_no_quiet_hours_finding(self, engine: PolicyEngine) -> None:
        # Supplied rather than read from the clock, so a replayed node evaluates against the instant
        # the decision belongs to. `None` means the caller did not establish a wall clock.
        decision = engine.evaluate(req(**{**NOTIFY, "local_time": None}, rca_confidence=0.9))
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_the_daily_contact_cap_is_reachable_and_not_reached_early(
        self, engine: PolicyEngine
    ) -> None:
        assert (
            engine.evaluate(req(**NOTIFY, rca_confidence=0.9, contacts_today=2)).outcome
            is PolicyOutcome.ALLOWED
        )
        at_cap = engine.evaluate(req(**NOTIFY, rca_confidence=0.9, contacts_today=3))
        assert at_cap.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_DUPLICATE_SUPPRESSED in at_cap.reason_codes

    def test_contacts_are_spaced(self, engine: PolicyEngine) -> None:
        too_soon = engine.evaluate(
            req(**NOTIFY, rca_confidence=0.9, minutes_since_last_contact=10.0)
        )
        spaced = engine.evaluate(req(**NOTIFY, rca_confidence=0.9, minutes_since_last_contact=90.0))
        assert too_soon.outcome is PolicyOutcome.BLOCKED
        assert spaced.outcome is PolicyOutcome.ALLOWED

    def test_a_vulnerable_customer_is_not_sent_self_help_but_may_still_be_notified(
        self, engine: PolicyEngine
    ) -> None:
        # Guided self-help assumes someone who can climb behind furniture and read a label. The
        # protection is specific to self-help: blocking every contact would leave a vulnerable
        # customer *less* informed, which inverts the intent.
        self_help = {
            "action_type": ActionType.SEND_SELF_HELP,
            "actor_role": Role.NOC_OPERATOR,
            "rca_confidence": 0.9,
            "evidence_source_count": 3,
            "blast_radius": 1,
            "local_time": time(10, 0),
        }
        blocked = engine.evaluate(req(**self_help, vulnerable_customer=True))
        allowed = engine.evaluate(req(**self_help, vulnerable_customer=False))
        notified = engine.evaluate(req(**NOTIFY, rca_confidence=0.9, vulnerable_customer=True))

        assert blocked.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.POLICY_VULNERABLE_CUSTOMER_PROTECTION in blocked.reason_codes
        assert allowed.outcome is PolicyOutcome.ALLOWED
        assert notified.outcome is PolicyOutcome.ALLOWED

    def test_the_contact_rules_apply_to_contacts_only(self, engine: PolicyEngine) -> None:
        # Rebooting a modem at 03:00 is not a contact. Every contact rule violated at once, on an
        # action that is not one.
        decision = engine.evaluate(
            req(
                local_time=time(2, 0),
                contacts_today=99,
                minutes_since_last_contact=0.0,
                vulnerable_customer=True,
            )
        )
        assert decision.outcome is PolicyOutcome.ALLOWED


# =================================================================================================
# Closure
# =================================================================================================


class TestClosure:
    def test_a_proven_closure_is_allowed(self, engine: PolicyEngine) -> None:
        assert engine.evaluate(req(**CLOSE, rca_confidence=0.9)).outcome is PolicyOutcome.ALLOWED

    def test_closing_without_validation_costs_a_supervisors_name(
        self, engine: PolicyEngine
    ) -> None:
        # The only exit from an incident without proof, and the specification's forbidden case is
        # exactly this one closing silently.
        decision = engine.evaluate(req(**{**CLOSE, "validation_passed": False}, rca_confidence=0.9))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert decision.required_approval_kind is ApprovalKind.EXCEPTIONAL_CLOSURE
        assert decision.required_role == "noc_supervisor"

    def test_an_unmeasured_validation_is_not_a_passed_one(self, engine: PolicyEngine) -> None:
        # `is not True` rather than `is False`. `None` means nobody looked, which is not proof.
        decision = engine.evaluate(req(**{**CLOSE, "validation_passed": None}, rca_confidence=0.9))
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL

    def test_an_unreconciled_closure_is_refused_rather_than_escalated(
        self, engine: PolicyEngine
    ) -> None:
        # Closing now leaves an open work order behind a closed incident. There is no signature that
        # makes two systems agree, so this is a block and not an interrupt.
        decision = engine.evaluate(req(**{**CLOSE, "reconciled": False}, rca_confidence=0.9))
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert ReasonCode.RECONCILIATION_MISMATCH in decision.reason_codes

    def test_a_pack_that_forbids_the_exceptional_path_blocks_instead_of_asking(
        self, pack_text: str
    ) -> None:
        # The same input, two packs, two outcomes -- which is the only way to show the branch is read
        # from the pack rather than hard-coded.
        strict = parse_pack(
            variant(
                pack_text,
                closure={
                    "allow_exceptional_closure": False,
                    "exceptional_closure_requires_approval": False,
                },
            )
        )
        decision = PolicyEngine(strict).evaluate(
            req(**{**CLOSE, "validation_passed": False}, rca_confidence=0.9)
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert "permits no exceptional path" in decision.explanation

    def test_the_closure_rules_apply_to_closure_only(self, engine: PolicyEngine) -> None:
        decision = engine.evaluate(req(validation_passed=False, reconciled=False))
        assert decision.outcome is PolicyOutcome.ALLOWED


# =================================================================================================
# Folding several findings into one decision
# =================================================================================================


class TestFolding:
    def test_every_applicable_check_runs_rather_than_short_circuiting(
        self, engine: PolicyEngine
    ) -> None:
        # The alternative produces a miserable operator experience: fix the stale telemetry, re-run,
        # and now learn the attempt limit is also reached. One read has to tell them everything.
        decision = engine.evaluate(req(rca_confidence=0.20, evidence_source_count=0, attempt=9))
        assert {
            ReasonCode.POLICY_EVIDENCE_INSUFFICIENT,
            ReasonCode.RCA_LOW_CONFIDENCE,
            ReasonCode.POLICY_ATTEMPT_LIMIT_REACHED,
        } <= set(decision.reason_codes)

    def test_reason_codes_are_deduplicated_but_keep_the_order_the_checks_ran_in(
        self, engine: PolicyEngine
    ) -> None:
        # The order is the explanation's narrative. Sorting it would scramble a reader's sense of
        # what went wrong first; a set would lose it entirely.
        decision = engine.evaluate(
            req(
                evidence_source_count=0,
                evidence_age_minutes=999.0,
                data_quality_flags=(DataQualityFlag.CONFLICTING_SOURCES,),
                rca_confidence=0.1,
            )
        )
        codes = list(decision.reason_codes)
        assert len(codes) == len(set(codes))
        assert codes.index(ReasonCode.POLICY_EVIDENCE_INSUFFICIENT) < codes.index(
            ReasonCode.RCA_LOW_CONFIDENCE
        )

    def test_a_block_outranks_every_approval(self, engine: PolicyEngine) -> None:
        # An approval for an action that is blocked anyway would put a question to a human whose
        # answer cannot be honoured.
        decision = engine.evaluate(
            req(
                **NODE_RESET,
                rca_confidence=0.20,
                blast_radius=100,
                data_quality_flags=(DataQualityFlag.ADAPTER_UNAVAILABLE,),
            )
        )
        assert decision.outcome is PolicyOutcome.BLOCKED
        assert decision.required_approval_kind is None
        # The approval reasons are still recorded: the operator fixing the block needs to know what
        # is waiting behind it.
        assert ReasonCode.RCA_LOW_CONFIDENCE in decision.reason_codes

    def test_the_narrowest_approver_set_wins_and_the_others_are_recorded(
        self, engine: PolicyEngine
    ) -> None:
        # Satisfying the narrowest is the hardest of the requirements, so the others are satisfied
        # incidentally by the same signature -- but the operator is told which, rather than the
        # engine dropping them.
        decision = engine.evaluate(req(**NODE_RESET, rca_confidence=0.20, blast_radius=100))
        assert decision.required_approval_kind is ApprovalKind.HIGH_BLAST_RADIUS_ACTION
        assert "low_confidence_rca" in decision.constraints["also_required"]
        # And the choice is a property of the approval matrix, not a second hand-ordered list here.
        assert len(approvers_for(ApprovalKind.HIGH_BLAST_RADIUS_ACTION)) < len(
            approvers_for(ApprovalKind.LOW_CONFIDENCE_RCA)
        )

    def test_a_tie_in_approver_count_reaches_the_same_interrupt_in_every_process(self) -> None:
        """The tie-break in `_most_restrictive`, tested across processes because that is where it
        matters.

        `_most_restrictive` picks from a *set*, and CPython's iteration order over enum members
        varies with `PYTHONHASHSEED`. So when two kinds have equally narrow approver sets, a `min`
        without an explicit tie-break returns a different interrupt on different machines -- two
        identical incidents get different audit trails, and neither is reproducible.

        This cannot be observed in one process: within a single interpreter, set order is fixed, so
        a same-process test passes whether or not the tie-break exists (measured -- an earlier
        version of this test did exactly that and a mutation removing the tie-break survived it).
        Hence the subprocesses. `clean_to_dirty_handover` and `low_confidence_rca` both have four
        approvers and an MR raised at low confidence requires both, so the pair is reachable rather
        than hypothetical.

        The control matters as much as the assertion: if no seed in the range perturbs set order,
        this experiment cannot fail and a pass would mean nothing, so it skips loudly instead.
        """
        tied = sorted(
            (k for k in ApprovalKind if len(approvers_for(k)) == 4), key=lambda k: k.value
        )
        assert len(tied) == 2, "the tie this test exists for no longer occurs; pick another pair"

        probe = (
            "from lpr_cpe.domain.enums import ApprovalKind, ReasonCode\n"
            "from lpr_cpe.policies.engine import PolicyEngine, _Finding\n"
            f"tied = [ApprovalKind({tied[0].value!r}), ApprovalKind({tied[1].value!r})]\n"
            "findings = [_Finding(reason_code=ReasonCode.POLICY_APPROVAL_REQUIRED,\n"
            "                     explanation='probe', rule='probe', blocks=False,\n"
            "                     approval_kind=k) for k in tied]\n"
            "engine = PolicyEngine(pack=None, unavailable_reason='probe')\n"
            "set_order = [k.value for k in {f.approval_kind for f in findings}]\n"
            "print(set_order[0], engine._most_restrictive(findings).value)\n"
        )
        set_orders: list[str] = []
        winners: list[str] = []
        for seed in range(6):
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                cwd=REPO_ROOT,
            )
            assert result.returncode == 0, f"seed {seed}: {result.stderr[-600:]}"
            first_out, winner = result.stdout.split()
            set_orders.append(first_out)
            winners.append(winner)

        if len(set(set_orders)) == 1:
            pytest.skip(
                "no seed in 0..5 perturbed set iteration order, so this run cannot distinguish a "
                "real tie-break from set order; widen the range rather than trusting a pass"
            )
        assert len(set(winners)) == 1, (
            "the interrupt raised depends on PYTHONHASHSEED: "
            f"{dict(zip(range(6), winners, strict=True))}"
        )
        assert winners[0] == tied[0].value, (
            f"expected the alphabetically first of {[k.value for k in tied]}, got {winners[0]}"
        )

    def test_the_tie_that_the_subprocess_test_probes_is_reachable_from_a_real_request(
        self, engine: PolicyEngine
    ) -> None:
        # The pair above is only worth defending if a request can actually raise both. An MR is a
        # clean-to-dirty handover; raising one on a weak root cause also trips the confidence bar.
        decision = engine.evaluate(
            req(
                action_type=ActionType.RAISE_MR,
                actor_role=Role.NOC_OPERATOR,
                rca_confidence=0.40,
                evidence_source_count=4,
                blast_radius=1,
            )
        )
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert ReasonCode.RCA_LOW_CONFIDENCE in decision.reason_codes
        assert decision.required_approval_kind is ApprovalKind.CLEAN_TO_DIRTY_HANDOVER

    def test_two_identical_requests_produce_identical_reasoning(self, engine: PolicyEngine) -> None:
        first = engine.evaluate(req(rca_confidence=0.20))
        second = engine.evaluate(req(rca_confidence=0.20))
        assert first.reason_codes == second.reason_codes
        assert first.explanation == second.explanation
        assert first.evaluated_inputs == second.evaluated_inputs
        # But not identical records: a decision is an event, and two evaluations are two events.
        assert first.decision_id != second.decision_id

    def test_an_allowed_decision_still_carries_a_reason(self, engine: PolicyEngine) -> None:
        # A permit with an empty explanation is indistinguishable in the audit log from a permit that
        # was never evaluated.
        decision = engine.evaluate(req())
        assert decision.outcome is PolicyOutcome.ALLOWED
        assert ReasonCode.POLICY_ALLOWED in decision.reason_codes
        assert decision.explanation
        assert decision.policy_version == engine.policy_version


# =================================================================================================
# Helpers the graph and the API need
# =================================================================================================


class TestHelpers:
    def test_an_expiry_is_derived_from_the_request_not_the_clock(
        self, engine: PolicyEngine
    ) -> None:
        # Otherwise every replay after a resume would quietly extend the operator's window.
        assert engine.approval_expiry(ApprovalKind.DISPATCH, NOW) == datetime(
            2026, 8, 14, 16, 0, tzinfo=UTC
        )

    def test_escalation_on_expiry_is_read_from_the_pack_in_both_directions(
        self, engine: PolicyEngine
    ) -> None:
        # `exceptional_closure` is the one kind that does not escalate: it is already the exception,
        # and escalating it would route it to the same supervisor who declined to sign.
        assert engine.escalates_on_expiry(ApprovalKind.DISPATCH) is True
        assert engine.escalates_on_expiry(ApprovalKind.EXCEPTIONAL_CLOSURE) is False

    def test_a_detector_threshold_falls_through_to_the_detectors_own_default(
        self, engine: PolicyEngine
    ) -> None:
        # `detector_thresholds` is sparse by design: an empty section means "the detector's default
        # stands", which is a meaningful thing to be able to say.
        assert engine.threshold("nothing_overrides_this", 0.31) == 0.31

    def test_a_detector_threshold_override_is_honoured(self, pack_text: str) -> None:
        # The other direction, without which a `threshold` wired to `return default` would pass.
        overridden = parse_pack(variant(pack_text, detector_thresholds={"some_metric": 0.9}))
        assert PolicyEngine(overridden).threshold("some_metric", 0.31) == 0.9

    def test_health_reports_the_pack_identity(self, engine: PolicyEngine) -> None:
        health = engine.health()
        assert health["policy"] == "ok"
        assert health["policy_version"] == engine.policy_version
        assert health["actions_refused"] == ["bulk_config_push"]

    def test_health_reports_the_effect_of_being_unavailable(self) -> None:
        # `/health` must report the degraded engine as unhealthy, so blocked incidents have a visible
        # cause rather than looking like a policy that got stricter.
        health = PolicyEngine(None, unavailable_reason="boom").health()
        assert health["policy"] == "unavailable"
        assert health["reason"] == "boom"
        assert health["effect"] == "every action is blocked"


# =================================================================================================
# The loader: the version describes the rules that were actually applied
# =================================================================================================


class TestVersioning:
    def test_the_version_is_the_declared_string_plus_a_content_digest(
        self, engine: PolicyEngine, pack: Any
    ) -> None:
        declared, _, digest = engine.policy_version.partition("+")
        assert declared == pack.version
        assert len(digest) == 12
        assert digest == pack.content_hash[:12]

    def test_editing_a_threshold_changes_the_version_without_touching_the_declared_string(
        self, pack: Any, pack_text: str
    ) -> None:
        # The failure this exists to prevent, spelled out: someone drops `min_for_dispatch` to 0.60
        # to unblock a stuck incident and does not bump `version:`. Every decision made afterwards
        # would claim to have been made under rules that no longer exist.
        edited = parse_pack(variant(pack_text, rca={"min_for_dispatch": 0.60}))
        assert edited.version == pack.version
        assert policy_version(edited) != policy_version(pack)

    def test_editorial_changes_do_not_change_the_version(self, pack: Any, pack_text: str) -> None:
        # The other direction, and it is not a nicety: if re-wrapping a comment invalidated the audit
        # trail, nobody would ever improve the comments in a 544-line file.
        recommented = pack_text.replace(
            "# A reboot is the cheapest useful action", "# EDITED COMMENT"
        )
        assert "EDITED COMMENT" in recommented, "the anchor comment has moved"
        assert policy_version(parse_pack(recommented)) == policy_version(pack)

        reordered = yaml.safe_dump(yaml.safe_load(pack_text), sort_keys=True)
        assert policy_version(parse_pack(reordered)) == policy_version(pack)

    def test_a_hand_written_content_hash_is_refused(self, pack_text: str) -> None:
        # A file that supplies its own hash could claim a version it does not have, which is
        # precisely the failure the digest exists to prevent.
        with pytest.raises(PolicyPackError, match="content_hash"):
            parse_pack(pack_text + "\ncontent_hash: deadbeef\n")

    def test_the_digest_ignores_key_order_and_notices_a_value(self) -> None:
        assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})
        assert canonical_digest({"a": 1}) != canonical_digest({"a": 2})

    def test_the_pack_is_cached_by_identity(self) -> None:
        # Not merely equal: the engine, the API and the scan job share one instance, and the sections
        # are frozen so that sharing is safe.
        assert load_pack() is load_pack()

    def test_a_pack_section_cannot_be_mutated(self, pack: Any) -> None:
        # A mutable section would let one node's convenience edit become another incident's policy.
        # Alternation, not a literal: pydantic's wording for a frozen-instance error has changed
        # between versions and the test should survive the next change. `Exception` rather than
        # `ValidationError` for the same reason.
        with pytest.raises(Exception, match=r"frozen|immutable"):
            pack.evidence.min_sources_for_dispatch = 1


# =================================================================================================
# Every pack validator, driven to failure on its own
# =================================================================================================


#: `(label, edits, expected fragment)`. The label is what appears in the pytest node id, so a failure
#: names the rule rather than an index. The fragment is asserted because an error that refuses a
#: 544-line file without naming the row is a refusal nobody can act on.
BROKEN_PACKS: list[tuple[str, dict[str, Any], str]] = [
    (
        "evidence_blocking_flags_loosened_below_the_code_floor",
        {"evidence": {"blocking_flags": ["adapter_unavailable", "conflicting_sources"]}},
        "superset",
    ),
    (
        "dispatch_needs_less_corroboration_than_a_remote_fix",
        {"evidence": {"min_sources_for_dispatch": 1, "min_sources_for_remote_action": 2}},
        "less corroboration",
    ),
    (
        "dispatch_evidence_may_predate_the_fault",
        {"evidence": {"max_age_for_dispatch_minutes": 120, "max_telemetry_age_minutes": 15}},
        "predate the fault",
    ),
    (
        "a_confidence_band_is_both_reviewable_and_actionable",
        {"rca": {"review_below": 0.80}},
        "reviewable and actionable",
    ),
    (
        "a_risk_class_requires_approval_but_names_no_interrupt",
        {
            "risk_classes": {
                "high": {
                    "requires_approval": True,
                    "approval_kind": None,
                    "max_blast_radius": 64,
                    "reversible": False,
                }
            }
        },
        "must name an approval_kind",
    ),
    (
        "blast_radius_defaults_do_not_nest_outward",
        {"blast_radius": {"delimiter_default": 900, "distribution_default": 120}},
        "must grow as the element does",
    ),
    (
        "the_network_threshold_exceeds_the_network_class_cap",
        {"blast_radius": {"network_action_threshold": 5000}},
        "exceeds risk_classes.network",
    ),
    (
        "a_reason_threshold_can_never_fire",
        {"attempt_limits": {"require_reason_beyond": {"work_order": 5}}},
        "can never fire",
    ),
    (
        "a_reason_threshold_names_something_that_is_not_a_counter",
        {"attempt_limits": {"require_reason_beyond": {"nonsense": 1}}},
        "not an attempt limit",
    ),
    (
        "a_plant_repair_is_proven_faster_than_a_single_service",
        {"validation": {"stability_window_plant_minutes": 10, "stability_window_minutes": 30}},
        "cannot be proven in less time",
    ),
    (
        "an_exceptional_closure_without_a_signature",
        {
            "closure": {
                "allow_exceptional_closure": True,
                "exceptional_closure_requires_approval": False,
            }
        },
        "without a signature",
    ),
    (
        "the_later_retries_would_have_no_backoff",
        {"reconciliation": {"max_retries": 5, "retry_backoff_seconds": [30, 120, 600]}},
        "no delay",
    ),
    (
        "the_sla_warning_fires_at_or_after_the_breach",
        {"escalation": {"sla_breach_warning_fraction": 1.0}},
        "is a breach report",
    ),
    (
        "escalation_targets_a_role_that_may_approve_nothing",
        {"escalation": {"target_role": "auditor"}},
        "cannot unblock an incident",
    ),
    (
        "the_health_bands_do_not_descend",
        {"health_bands": {"degraded_at_or_above": 90}},
        "must strictly descend",
    ),
    ("the_predictive_sweep_would_never_run", {"scan": {"windows_local": []}}, "would never run"),
    (
        "every_dispatch_objective_weight_is_zero",
        {
            "dispatch": {
                "objective_weights": {
                    "sla_risk": 0,
                    "blast_radius": 0,
                    "travel_minutes": 0,
                    "crew_skill_match": 0,
                    "appointment_window": 0,
                    "vulnerable_customer": 0,
                }
            }
        },
        "arbitrary one",
    ),
    (
        "the_longest_visit_does_not_fit_in_a_shift",
        {"dispatch": {"joint_visit_minutes": 900}},
        "reported infeasible",
    ),
    (
        "the_sla_relaxes_as_severity_rises",
        {
            "sla": {
                "response_minutes": {
                    "critical": 999,
                    "high": 60,
                    "medium": 240,
                    "low": 480,
                    "info": 1440,
                }
            }
        },
        "does not tighten as severity rises",
    ),
    (
        "service_is_restored_before_it_is_responded_to",
        {
            "sla": {
                "restore_minutes": {
                    "critical": 10,
                    "high": 480,
                    "medium": 1440,
                    "low": 2880,
                    "info": 5760,
                }
            }
        },
        "cannot be restored",
    ),
    (
        "an_action_names_an_undefined_risk_class",
        {"remote_actions": {"cpe_reboot": {"allowed": True, "risk": "extreme"}}},
        "not a defined risk class",
    ),
    (
        "an_action_spends_a_graph_level_counter",
        {
            "remote_actions": {
                "cpe_reboot": {"allowed": True, "risk": "low", "max_attempts_key": "total_steps"}
            }
        },
        "not a per-action attempt counter",
    ),
    (
        "an_approval_names_a_role_the_enum_does_not_define",
        {
            "approvals": {
                "dispatch": {
                    "required_role": "senior_engineer",
                    "expires_after_minutes": 240,
                    "escalate_on_expiry": True,
                }
            }
        },
        "Input should be",
    ),
    (
        "a_mistyped_key_is_not_silently_ignored",
        {"evidence": {"min_sources_for_dispath": 3}},
        "extra_forbidden",
    ),
]


class TestPackValidation:
    @pytest.mark.parametrize(
        ("label", "edits", "fragment"),
        [pytest.param(label, edits, fragment, id=label) for label, edits, fragment in BROKEN_PACKS],
    )
    def test_a_pack_that_breaks_one_rule_is_refused(
        self, pack_text: str, label: str, edits: dict[str, Any], fragment: str
    ) -> None:
        with pytest.raises(PolicyPackError) as caught:
            parse_pack(variant(pack_text, **edits), source=f"<{label}>")
        assert fragment in str(caught.value), str(caught.value)[:400]

    @pytest.mark.parametrize(
        ("label", "path", "fragment"),
        [
            pytest.param(
                "an_action_type_has_no_row",
                ("remote_actions", "cpe_reboot"),
                "indistinguishable from an oversight",
                id="an_action_type_has_no_row",
            ),
            # Two validators guard the approvals table and they are not interchangeable, so each is
            # driven to failure on its own. Deleting a kind some action *routes to* is caught by the
            # reachability check, which can name the orphaned action; deleting one no action routes
            # to gets past that check entirely and is caught only by the completeness check. Four of
            # the six kinds resolve from a `remote_actions` row; `exceptional_closure` and
            # `low_confidence_rca` are raised by the closure and RCA stages instead, so they are the
            # only two that can reach the second validator. Parametrising both over one fragment
            # would have let the completeness check rot untested behind the reachability one.
            pytest.param(
                "an_interrupt_kind_some_action_routes_to_has_no_rule",
                ("approvals", "dispatch"),
                "create_work_order would raise a dispatch interrupt",
                id="an_interrupt_kind_some_action_routes_to_has_no_rule",
            ),
            pytest.param(
                "an_interrupt_kind_no_action_routes_to_has_no_rule",
                ("approvals", "exceptional_closure"),
                "each needs an expiry and a required role",
                id="an_interrupt_kind_no_action_routes_to_has_no_rule",
            ),
            pytest.param(
                "an_archetype_has_no_speed",
                ("dispatch", "archetype_speed_kph", "central_mountain_rural"),
                "central_mountain_rural",
                id="an_archetype_has_no_speed",
            ),
            pytest.param(
                "an_archetype_has_no_ferry_number",
                ("dispatch", "archetype_ferry_minutes", "remote_island"),
                "remote_island",
                id="an_archetype_has_no_ferry_number",
            ),
            pytest.param(
                "a_severity_has_no_deadline",
                ("sla", "response_minutes", "low"),
                "reads as 'never late'",
                id="a_severity_has_no_deadline",
            ),
        ],
    )
    def test_a_pack_missing_one_required_row_is_refused(
        self, pack_text: str, label: str, path: tuple[str, ...], fragment: str
    ) -> None:
        # Omissions, which the merge helper above cannot express. Every one of these is a row whose
        # absence would otherwise be answered by a default that is wrong somewhere.
        with pytest.raises(PolicyPackError) as caught:
            parse_pack(without(pack_text, *path), source=f"<{label}>")
        assert fragment in str(caught.value), str(caught.value)[:400]

    def test_regression_an_approval_gate_names_a_role_that_can_answer_it(
        self, pack_text: str, pack: Any
    ) -> None:
        """REGRESSION: four approval gates named roles no principal could hold.

        An earlier `pack.yaml` required `senior_engineer`, `dispatch_supervisor`, `field_supervisor`
        and `assurance_manager`. `Role` defines none of them. Each gate would have named a role that
        `can_approve()` refuses -- an approval that can never be granted, found at 02:00 by whoever
        is on call.

        Parsing into the enum catches an invented name. It does not catch the subtler half: a role
        that *exists* but that `rbac.approvers_for` does not permit for that kind. `field_technician`
        is a real role and may answer a handover; it may not sign off an exceptional closure. That
        gate would load cleanly and still be unpassable, which is why the check compares the two
        tables rather than only validating the string.
        """
        with pytest.raises(PolicyPackError, match="can_approve\\(\\) refuses"):
            parse_pack(
                variant(
                    pack_text,
                    approvals={
                        "exceptional_closure": {
                            "required_role": "field_technician",
                            "expires_after_minutes": 1440,
                            "escalate_on_expiry": False,
                        }
                    },
                )
            )

        # And the pack as shipped satisfies it: every floor role is one `rbac` agrees can answer.
        for kind, rule in pack.approvals.items():
            assert rule.required_role in approvers_for(kind), kind.value

    def test_the_shipped_pack_loads(self, pack: Any) -> None:
        # The check that would have caught every case above at once, and the reason the others exist
        # separately: this one goes red without saying which rule broke.
        assert pack.version
        assert set(pack.remote_actions) == set(ActionType)
        assert set(pack.approvals) == set(ApprovalKind)

    @pytest.mark.parametrize(
        ("label", "text", "fragment"),
        [
            ("empty", "", "is empty"),
            ("a list", "- a\n- b\n", "mapping at the top level"),
            ("not yaml", "a: [1, 2\n", "not valid YAML"),
        ],
    )
    def test_a_pack_that_is_not_a_document_is_refused_by_name(
        self, label: str, text: str, fragment: str
    ) -> None:
        # `source` appears in every message because "1 validation error for PolicyPack" with no
        # filename is unhelpful when three packs are mounted.
        with pytest.raises(PolicyPackError) as caught:
            parse_pack(text, source=f"<{label}>")
        assert fragment in str(caught.value)
        assert f"<{label}>" in str(caught.value)


# =================================================================================================
# Section accessors
# =================================================================================================


class TestSectionAccessors:
    def test_the_backoff_clamps_rather_than_wrapping(self, pack: Any) -> None:
        # Wrapping would send the fourth retry back to a 30-second delay against a system that has
        # already failed three times.
        assert pack.reconciliation.backoff_for(1) == pack.reconciliation.retry_backoff_seconds[0]
        assert pack.reconciliation.backoff_for(99) == pack.reconciliation.retry_backoff_seconds[-1]
        assert pack.reconciliation.backoff_for(0) == pack.reconciliation.retry_backoff_seconds[0]
        assert pack.reconciliation.backoff_for(2) > pack.reconciliation.backoff_for(1)

    @pytest.mark.parametrize(
        ("score", "band"),
        [
            (100.0, HealthBand.HEALTHY),
            (80.0, HealthBand.HEALTHY),
            (79.9, HealthBand.DEGRADED),
            (60.0, HealthBand.DEGRADED),
            (59.9, HealthBand.AT_RISK),
            (40.0, HealthBand.AT_RISK),
            (39.9, HealthBand.CRITICAL),
            (0.0, HealthBand.CRITICAL),
        ],
    )
    def test_each_health_boundary_is_inclusive_from_above(
        self, pack: Any, score: float, band: HealthBand
    ) -> None:
        assert pack.health_bands.band_for(score) is band

    def test_band_comparison_is_not_alphabetical(self, pack: Any) -> None:
        # `HealthBand` is a `StrEnum`, so `<=` compares strings: "at_risk" < "healthy" is True by
        # accident and "critical" < "degraded" is True for the wrong reason.
        assert pack.health_bands.at_or_below(HealthBand.CRITICAL, HealthBand.AT_RISK) is True
        assert pack.health_bands.at_or_below(HealthBand.HEALTHY, HealthBand.DEGRADED) is False
        assert pack.health_bands.at_or_below(HealthBand.AT_RISK, HealthBand.AT_RISK) is True
        # The pair a string comparison gets backwards.
        assert pack.health_bands.at_or_below(HealthBand.CRITICAL, HealthBand.DEGRADED) is True

    def test_a_vulnerable_customer_gets_a_tighter_sla(self, pack: Any) -> None:
        assert pack.sla.response_for(Severity.HIGH, vulnerable=True) < pack.sla.response_for(
            Severity.HIGH, vulnerable=False
        )
        assert pack.sla.restore_for(Severity.MEDIUM, vulnerable=True) < pack.sla.restore_for(
            Severity.MEDIUM, vulnerable=False
        )

    def test_the_top_severity_cannot_tighten_further(self, pack: Any) -> None:
        # `Severity.from_rank` has to clamp. Without it, a vulnerable customer on a critical incident
        # would ask for a band that does not exist.
        assert pack.sla.response_for(Severity.CRITICAL, vulnerable=True) == pack.sla.response_for(
            Severity.CRITICAL, vulnerable=False
        )

    def test_the_attempt_limit_lookup_holds_none_and_garbage_apart_from_a_limit(
        self, pack: Any
    ) -> None:
        assert pack.attempt_limit_for(ActionType.CPE_REBOOT) == pack.attempt_limits.remote
        assert pack.attempt_limit_for(ActionType.NOTIFY_CUSTOMER) is None
        assert pack.attempt_limits.limit_for(None) is None
        assert pack.attempt_limits.limit_for("garbage") is None
        # `total_steps` is a graph guard, not a per-action counter, and must not resolve as one.
        assert "total_steps" not in pack.attempt_limits.counter_names()

    def test_the_lookups_return_none_rather_than_a_default_for_an_absent_action(
        self, pack: Any
    ) -> None:
        # `None` must be read as a block by the caller. A default here would be the permissive one.
        thinned = pack.model_copy(
            update={
                "remote_actions": {
                    action: rule
                    for action, rule in pack.remote_actions.items()
                    if action is not ActionType.CPE_REBOOT
                }
            }
        )
        assert thinned.rule_for(ActionType.CPE_REBOOT) is None
        assert thinned.risk_class_for(ActionType.CPE_REBOOT) is None
        assert thinned.approval_kind_for(ActionType.CPE_REBOOT) is None
        assert thinned.blast_radius_cap_for(ActionType.CPE_REBOOT) is None

    def test_the_summary_names_the_refusals_rather_than_counting_them(self, pack: Any) -> None:
        # "which pack is running" has to be answerable without diffing two files, and a count would
        # not distinguish refusing `bulk_config_push` from refusing `cpe_reboot`.
        summary = pack.summary()
        assert summary["actions_refused"] == ["bulk_config_push"]
        assert summary["actions_allowed"] + len(summary["actions_refused"]) == len(ActionType)
        assert summary["approval_kinds"] == sorted(kind.value for kind in ApprovalKind)

    def test_scan_windows_are_sorted_and_deduplicated(self, pack_text: str) -> None:
        parsed = parse_pack(variant(pack_text, scan={"windows_local": ["21:00", "07:00", "21:00"]}))
        assert list(parsed.scan.windows_local) == [time(7, 0), time(21, 0)]
