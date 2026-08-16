"""The simulator write path, and what a read sees after an action has been applied.

Until now the ten simulators were exercised only through the detectors and the parent nodes, which
read. Nothing tested `apply_action`, so the three obligations `simulate_write` names -- ask the
gate, honour the verdict, apply at most once per idempotency key -- were asserted by its docstring
and by nothing else. The first half of this module tests them directly.

The second half tests something the write path did not used to have: an *effect*. A simulator whose
`apply_action` records an intent and changes nothing means the verification read after a remote
repair returns the same degraded telemetry it returned before, so verification can only ever fail,
so D10 can only ever answer `retry_diagnosis` and the specification's Scenario 2 has no way to
happen. A simulator that recovered every device would be worse: every incident would close on the
first reboot and Scenario 3's field visit would never be dispatched.

So the property worth testing is not "a reboot fixes it" but "a reboot fixes exactly the faults a
reboot fixes". The fixtures already draw that line -- `SVC-UT-001-B-01` is a wedged device behind
healthy plant, `SVC-VQ-002-A-01` is dark because utility power is out -- and the tests below drive
both through the same action and assert they diverge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from lpr_cpe.domain.enums import ActionOutcome, ActionType, ReasonCode
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.domain.resolution import RemoteAction
from lpr_cpe.integrations.base import WriteGate, WriteVerdict
from lpr_cpe.simulation.loader import build_simulated_adapters

AT = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

#: Offline, but behind an ODP whose every other service is `pon_healthy` and outside the outage
#: area. Nothing physical explains it, so a reboot is a reasonable thing to try.
WEDGED = "CPE-UT-001-B-01"

#: Offline because the ONT sent a dying gasp when utility power failed. No number of reboots
#: reaches a device with no power, and a simulator that pretended otherwise would close the
#: incident on a customer still sitting in the dark.
POWER_CUT = "CPE-VQ-002-A-01"


def _request(
    target_ref: str,
    action_type: ActionType = ActionType.CPE_REBOOT,
    *,
    key: str | None = None,
) -> ActionRequest:
    return ActionRequest(
        action_id=f"ACT-{target_ref}-{action_type.value}",
        incident_id=f"INC-{target_ref}",
        action_type=action_type,
        target_ref=target_ref,
        requested_at=AT,
        idempotency_key=key or f"IDEM-{target_ref}-{action_type.value}",
        actor="test",
        reason_code=ReasonCode.REMOTE_FIX_APPLIED,
        correlation_id=f"COR-{target_ref}",
    )


# ------------------------------------------------------------------------------------------------
# The three obligations of the write path
# ------------------------------------------------------------------------------------------------


async def test_the_gate_is_asked_before_the_ledger_so_replays_are_still_audited(
    adapters: Any,
) -> None:
    """Two attempts on one key: one effect, but *two* entries in the gate's record.

    The order is the whole point and `simulate_write` says so: consulting the ledger first would
    return the cached result without telling the gate, and "how often do we try to send the same
    work order twice" would become unanswerable from the audit trail. Asserting one count without
    the other would pass under exactly the implementation this is meant to rule out.
    """
    request = _request(WEDGED)
    await adapters.cpe.apply_action(request)
    await adapters.cpe.apply_action(request)

    assert len(adapters.gate.recorded) == 2, "both attempts must reach the gate"
    assert len(adapters.cpe.recorded_writes) == 1, "but only one of them is an effect"


async def test_a_replay_returns_the_first_result_rather_than_building_a_second(
    adapters: Any,
) -> None:
    """Same key, same external reference -- and the replay says so.

    A fresh reference on the second call would leave the caller holding two identifiers for one
    work order with no way to tell they were the same thing, which is the confusion idempotency
    exists to prevent.
    """
    request = _request(WEDGED)
    first = await adapters.cpe.apply_action(request)
    second = await adapters.cpe.apply_action(request)

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["external_ref"] == first["external_ref"]
    assert "replayed" in second["detail"]


async def test_a_simulated_write_reports_simulated_rather_than_succeeded(adapters: Any) -> None:
    """The simulator refuses to claim an effect it did not have.

    This is load-bearing rather than pedantic: `RemoteAction._RAN` admits `SIMULATED` precisely
    *because* this is what a permitted write reports here, and the closure stage reads the
    `simulated` flag to decide what it may tell a customer.
    """
    result = await adapters.cpe.apply_action(_request(WEDGED))
    assert result["outcome"] == ActionOutcome.SIMULATED.value
    assert result["simulated"] is True


async def test_an_action_this_adapter_cannot_perform_is_refused_rather_than_faked(
    adapters: Any,
) -> None:
    """`RAISE_MR` is a real action, but not one an ACS performs.

    Policy decides *whether* an action is allowed; the adapter still has to know whether it is even
    implementable. A plausible-looking success here would be an MR nobody raised.
    """
    from lpr_cpe.integrations.base import AdapterError

    with pytest.raises(AdapterError):
        await adapters.cpe.apply_action(_request(WEDGED, ActionType.RAISE_MR))


# ------------------------------------------------------------------------------------------------
# The effect of an action on what a later read sees
# ------------------------------------------------------------------------------------------------


async def test_a_reboot_brings_back_a_wedged_device_behind_healthy_plant(adapters: Any) -> None:
    """Offline before, online after -- which is what makes a verification step meaningful.

    Also asserts the uptime, because a device reporting the fixture's multi-day uptime moments
    after being rebooted would contradict the action sitting in the same incident's history, and
    an uptime detector would be right to disbelieve one of the two.
    """
    before = await adapters.cpe.read_status(WEDGED)
    assert before["online"] is False

    result = await adapters.cpe.apply_action(_request(WEDGED))
    assert result["expected_to_restore_service"] is True

    after = await adapters.cpe.read_status(WEDGED)
    assert after["online"] is True
    assert after["data_available"] is True
    assert after["uptime_seconds"] == 120


async def test_a_reboot_does_not_bring_back_a_device_whose_power_is_out(adapters: Any) -> None:
    """The control, and the more important half of the pair.

    If this ever passes as a recovery, every scenario that needs a field visit closes on the first
    reboot instead: Scenario 3's Clean Boots dispatch never happens, and a customer with no
    electricity is told their service is restored.
    """
    result = await adapters.cpe.apply_action(_request(POWER_CUT))
    assert result["expected_to_restore_service"] is False

    after = await adapters.cpe.read_status(POWER_CUT)
    assert after["online"] is False
    assert after["uptime_seconds"] is None


async def test_a_radio_change_does_not_resurrect_a_device_that_is_not_talking(
    adapters: Any,
) -> None:
    """Recovery is a property of the action as well as of the fault.

    A channel change reconfigures a radio on a device that is already reachable. Letting it clear
    an offline state would make the recovery model "any write fixes anything", which is the failure
    the `_RECOVERING_ACTIONS` list exists to prevent.
    """
    result = await adapters.cpe.apply_action(_request(WEDGED, ActionType.WIFI_CHANNEL_CHANGE))
    assert result["expected_to_restore_service"] is False
    assert (await adapters.cpe.read_status(WEDGED))["online"] is False


async def test_every_read_agrees_about_whether_the_device_came_back(adapters: Any) -> None:
    """Three reads consult `offline`, and evidence that disagrees with itself is unusable.

    Before the recovery model had a single owner this was the live failure mode: `read_status`
    would have reported the device online while `run_diagnostic` refused the same device as
    offline, and the case would carry both claims into RCA.
    """
    await adapters.cpe.apply_action(_request(WEDGED))

    assert (await adapters.cpe.read_status(WEDGED))["online"] is True
    assert (await adapters.cpe.read_wifi_status(WEDGED))["data_available"] is True
    # Previously raised `AdapterError("... is offline")`; the device is back, so the test runs.
    diagnostic = await adapters.cpe.run_diagnostic(WEDGED, "ip_ping")
    assert diagnostic["data_available"] is True


async def test_a_recovery_does_not_leak_into_the_next_incident(fixtures: Any) -> None:
    """`load_fixtures()` is cached and shared, so recovery is held on the adapter, not written back.

    Two adapter sets over the *same* fixture object: rebooting through the first must leave the
    second seeing the device exactly as the fixture describes it. If recovery were mutated into the
    fixture dict this would pass locally and then make every later test in the process depend on
    which tests ran before it.
    """
    first = build_simulated_adapters(fixtures=fixtures)
    second = build_simulated_adapters(fixtures=fixtures)

    await first.cpe.apply_action(_request(WEDGED))

    assert (await first.cpe.read_status(WEDGED))["online"] is True
    assert (await second.cpe.read_status(WEDGED))["online"] is False, (
        "the reboot leaked out of the adapter that performed it and into the shared fixtures"
    )


class _RefusingGate(WriteGate):
    """A gate that blocks outright, which the real one cannot currently do.

    `WriteGate.authorize` has exactly two returns: permitted, or `permitted=False, simulated=True`.
    Neither yields `permitted=False, simulated=False`, so `WriteVerdict.outcome_if_refused` always
    resolves to `SIMULATED` and `BLOCKED_BY_POLICY` is unreachable through the real gate -- gap
    WRITE-1. Four guards downstream are written against it anyway, correctly, and would otherwise
    be dead code that nobody notices rotting. This subclass is what keeps them honest.
    """

    def authorize(self, request: ActionRequest) -> WriteVerdict:
        super().authorize(request)  # still recorded: a refused attempt is an attempt
        return WriteVerdict(
            permitted=False,
            simulated=False,
            reason_code=ReasonCode.POLICY_BLAST_RADIUS_EXCEEDED,
            explanation="test gate: refused outright",
        )


async def test_an_action_the_gate_refuses_does_not_recover_anything(fixtures: Any) -> None:
    """A blocked write had no effect, so the device must still be offline afterwards.

    This is the guard the mutation check found unprotected. Without it a reboot that policy refused
    to send would still mark the device recovered, the verification read would pass, and the
    incident would close on an action that never left the process -- the worst available outcome,
    because it is indistinguishable in the audit trail from one that worked.
    """
    adapters = build_simulated_adapters(fixtures=fixtures, gate=_RefusingGate())

    result = await adapters.cpe.apply_action(_request(WEDGED))
    assert result["outcome"] == ActionOutcome.BLOCKED_BY_POLICY.value
    assert result["external_ref"] is None, "nothing was created, so there is nothing to name"

    assert (await adapters.cpe.read_status(WEDGED))["online"] is False
    assert adapters.cpe.recorded_writes == (), "a blocked action must not occupy the ledger"


# ------------------------------------------------------------------------------------------------
# What the graph makes of a simulated action
# ------------------------------------------------------------------------------------------------


def test_a_simulated_action_can_still_be_a_restoration_once_it_is_verified() -> None:
    """`fixed_it` admits `SIMULATED`, and still insists on the verification.

    Both halves matter. Rejecting `SIMULATED` would make `fixed_it` a constant `False` in the only
    mode that runs today -- D10's `verify` branch dead, Scenario 2 unreachable. Dropping the
    verification requirement would close incidents on actions nobody checked, which is the failure
    the property was written for. The third case below is the one that keeps the pair honest.
    """
    common = {
        "action_id": "ACT-1",
        "action_type": ActionType.CPE_REBOOT,
        "target_ref": WEDGED,
        "idempotency_key": "KEY-12345678",
        "requested_at": AT,
    }

    verified = RemoteAction(
        **common, outcome=ActionOutcome.SIMULATED, verified_at=AT, verification_passed=True
    )
    assert verified.fixed_it is True

    unverified = RemoteAction(**common, outcome=ActionOutcome.SIMULATED)
    assert unverified.fixed_it is False, "an unchecked action is not a restoration"

    failed_verification = RemoteAction(
        **common, outcome=ActionOutcome.SIMULATED, verified_at=AT, verification_passed=False
    )
    assert failed_verification.fixed_it is False

    # And an action that never ran is not a restoration however it was verified.
    blocked = RemoteAction(
        **common,
        outcome=ActionOutcome.BLOCKED_BY_POLICY,
        verified_at=AT,
        verification_passed=True,
    )
    assert blocked.fixed_it is False
