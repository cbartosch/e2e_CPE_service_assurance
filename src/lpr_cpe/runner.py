"""Drive one incident from event to standstill, answering every pause. What `lpr-cpe run` calls.

Until this module existed the workflow could not be run. The graph was complete -- 26 nodes, 24
decisions, six approval gates -- and every one of those gates raises `interrupt()`, so a caller who
did not answer them got a paused thread and nothing else. `make serve` names an HTTP surface that is
unbuilt and `make demo` named scenarios that are unwritten, which left `tests/unit/test_builder.py`
as the only code in the repository that drove the parent to a standstill. This is that walk, made a
command.

Five pause shapes, and answering the wrong one is silent
-------------------------------------------------------
`interrupt()` payloads are dicts, and which key is present is the only thing distinguishing an
approval question from a crew briefing. Handing an approval payload to `field_submission_request`
does not raise -- the parser returns `None`, the node records an unusable report, and the crew is
asked again until a re-entry budget trips. IMPLEMENTATION_PLAN.md §5 records a sweep that did
exactly that and wrote the result up as a product defect before finding it was the harness. So
`_answer` dispatches on the key, and `PAUSE_SHAPES` names all five with the parser that consumes
each, so a sixth added to `src` fails `tests/unit/test_runner.py` rather than falling through to an
approval.

The stability window is not answered at all
-------------------------------------------
`await_service_stability` is `while ctx.clock.now() < deadline: interrupt(...)`, so the resume value
is not what releases it -- the clock is. Handing it a signature leaves the deadline where it was and
re-raises the identical interrupt, which is a loop the re-entry budget ends. The runner moves its
clock past the deadline instead, which is what a scheduler firing at the resume time amounts to.

What this is not
----------------
A simulation of real operators. Every answer is scripted and the script is chosen to reach `closed`;
`--decline` inverts the approvals to show what a refusal does. The seventeen specification scenarios
are a different artefact and are still unwritten.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TextIO

from langgraph.types import Command

from lpr_cpe.config.settings import Settings, get_settings
from lpr_cpe.domain.enums import CaseType, EventSource, Severity, Technology
from lpr_cpe.domain.field_ops import HandoverContract
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.persistence.checkpointer import build_memory_checkpointer

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from lpr_cpe.config.clock import Clock

#: Every `interrupt()` payload shape in `src`, and the module that raises it. Read by
#: `tests/unit/test_runner.py`, which greps `src` for payload keys and fails if one is missing here
#: -- an unanswered shape falls through to the approval branch, where it is rejected silently.
PAUSE_SHAPES: dict[str, str] = {
    "approval_request": "graph/interrupts.py -- all six approval gates",
    "briefing": "graph/subgraphs/field_execution.py -- P17's crew brief",
    "field_submission_request": "graph/subgraphs/field_execution.py -- the crew's report",
    "customer_response_request": "graph/subgraphs/self_help.py -- D12's customer window",
    "plant_report_request": "graph/subgraphs/plant_execution.py -- OSP's report",
    "stability_window_wait": "graph/subgraphs/restoration_validation.py -- released by the clock",
}

#: How many resume laps before the runner gives up. Every loop in the graph is bounded by the budget
#: guard, so a run that has not settled by here is a run whose guards are not working -- which is
#: worth reporting as such rather than hanging.
MAX_LAPS = 60


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one drive produced. Printed by `report_run`; asserted by the tests."""

    service_ref: str
    status: str
    escalated: bool
    reason: str
    pauses: dict[str, int]
    nodes_entered: int
    audit_events: int
    actions: int
    settled: bool

    @property
    def pauses_answered(self) -> int:
        return sum(self.pauses.values())


class ProductionWritesRefusedError(RuntimeError):
    """Raised rather than running a demonstration against a system that permits real writes."""


def _event(service: dict[str, Any], case_type: CaseType, now: datetime) -> AssuranceEvent:
    return AssuranceEvent(
        event_id=f"EVT-{service['service_ref']}",
        source=EventSource.NXT,
        case_type=case_type,
        technology=Technology(service["technology"]),
        severity=Severity.HIGH,
        occurred_at=now - timedelta(minutes=6),
        received_at=now - timedelta(minutes=5),
        customer_ref=service["customer_ref"],
        service_ref=service["service_ref"],
        cpe_ref=service["cpe_ref"],
        summary=f"degraded service on {service['service_ref']}",
    )


def initial_state(service: dict[str, Any], *, case_type: CaseType, now: datetime) -> Any:
    """The intake state one alarm produces. The same shape `test_builder.py` builds."""
    return make_initial_state(
        incident_id=f"INC-{service['service_ref']}",
        correlation_id=f"COR-{service['service_ref']}",
        event=_event(service, case_type, now),
        sla=SLAContext(
            clock_started_at=now - timedelta(minutes=5),
            product_tier=service["product_tier"],
            vulnerable_customer=service["vulnerable_customer"],
            priority_customer=service["priority_customer"],
        ),
        now=now,
    )


def approval(*, approved: bool) -> dict[str, Any]:
    """A supervisor's answer. `decided_by_role` matters: RBAC refuses an unqualified approver."""
    return {
        "status": "approved" if approved else "rejected",
        "decided_by": "sofia.reyes",
        "decided_by_role": "noc_supervisor",
        "rationale": (
            "demonstration run: approving so the incident proceeds"
            if approved
            else "demonstration run: refusing to show the other arm"
        ),
    }


def crew_report(service: dict[str, Any]) -> dict[str, Any]:
    """A Clean Boots crew reporting a fault found and fixed at the drop.

    The measurement keys come out of `HandoverContract.REQUIRED_BY_TECHNOLOGY` rather than being
    spelled here, for `test_subgraph_field_execution.py`'s reason: the key is the thing the contract
    checks, and a hand-copied list drifts away from the contract it has to match.
    """
    required = HandoverContract.REQUIRED_BY_TECHNOLOGY[service["technology"]]
    return {
        "fault_domain": "drop",
        "delimiter_kind": "tap" if service["technology"] == "hfc" else "odp",
        "delimiter_ref": service["delimiter_ref"],
        "fault_confirmed": True,
        "no_fault_found": False,
        "work_completed": True,
        "requires_plant_work": False,
        "requires_permit": False,
        "measurements": dict.fromkeys(required, -14.5),
        "parts_replaced": ["drop cable"],
        "evidence_refs": ["PHOTO-1"],
        "technician_note": "replaced the drop; the premises tests clean",
        "recorded_by": "t.nguyen",
        "last_clean_point": "drop at premises",
        "first_failed_point": service["delimiter_ref"],
        "customer_confirmed": True,
    }


def _release_the_window(payload: dict[str, Any], clock: Clock) -> dict[str, Any]:
    """Move the clock to the window's deadline. The resume value is not what ends this pause.

    See the module docstring: the wait re-checks the clock after every resume, so an answer that
    left the clock alone would re-raise the identical interrupt.
    """
    resume_at = (payload.get("stability_window_wait") or {}).get("resume_at")
    if resume_at:
        deadline = datetime.fromisoformat(resume_at)
        if deadline > clock.now():
            clock.set(deadline + timedelta(minutes=1))  # type: ignore[attr-defined]
    return {"resumed_by": "scheduler"}


def pause_kind(payload: Any) -> str:
    """Which of `PAUSE_SHAPES` this payload is, or `"unknown"`.

    Returns `"unknown"` rather than guessing an approval, because guessing is the failure this
    function exists to prevent -- an approval handed to a crew-report parser is rejected in silence.
    `drive` counts unknowns and reports them, so a sixth shape shows up as a number rather than as a
    run that mysteriously stops making progress.
    """
    if not isinstance(payload, dict):
        return "unknown"
    for key in PAUSE_SHAPES:
        if key in payload:
            return key
    return "unknown"


def _answer(
    kind: str, service: dict[str, Any], clock: Clock, payload: Any, *, approve: bool
) -> Any:
    if kind == "stability_window_wait":
        return _release_the_window(payload, clock)
    if kind in {"briefing", "field_submission_request"}:
        return crew_report(service)
    if kind == "plant_report_request":
        return {
            "status": "completed",
            "osp_owner": "osp.crew",
            "note": "demonstration run: span repaired",
            "resolution_code": "splice_replaced",
        }
    if kind == "customer_response_request":
        return {"response": "completed", "customer_completed_step": True}
    return approval(approved=approve)


async def drive(
    service: dict[str, Any],
    *,
    case_type: CaseType = CaseType.PROACTIVE_ALARM,
    approve: bool = True,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Run one incident to a standstill and report what happened.

    Refuses outright when the resolved settings permit production writes. `build_context` reaches
    only for the fixture-backed simulators today, so nothing real is reachable either way -- the
    check is here so that stays true if a real adapter is ever wired, rather than depending on the
    adapters being fakes.
    """
    resolved = settings or get_settings()
    if resolved.writes_permitted:
        # The environment variables are named with `Settings.model_config`'s `env_prefix`, not with
        # the bare field names. Getting that wrong is not hypothetical: the first run of this guard
        # was tested with `APP_MODE=production` and the guard did not fire, because the setting is
        # `LPR_APP_MODE` and the unprefixed name is silently ignored (`extra="ignore"`). A message
        # naming the field rather than the variable would send the next reader down the same path.
        raise ProductionWritesRefusedError(
            "`lpr-cpe run` drives a scripted demonstration and will not do it against a "
            "configuration that permits production writes. LPR_APP_MODE resolves to "
            f"{resolved.app_mode.value!r} and LPR_ALLOW_PRODUCTION_WRITES to "
            f"{resolved.allow_production_writes!r}; unset both and run again."
        )

    from lpr_cpe.config.clock import FrozenClock

    # UTC, not `resolved.timezone`, which is the *operating* zone and a string. A3: every stored
    # timestamp in this system is timezone-aware UTC, and the local zone is a rendering concern that
    # `Clock.local_now` owns.
    started = now or datetime.now(tz=UTC)

    class _Ticking(FrozenClock):
        """Advance on read, so durations are non-zero and the run stays deterministic.

        The same clock `test_builder.py` uses, and for its reason: inside a compiled graph the
        caller cannot advance time between nodes, so a frozen instant would make every KPI duration
        zero and every evidence age identical.
        """

        def now(self) -> datetime:
            return self.advance(timedelta(seconds=3))

    clock = _Ticking(started)
    ctx = build_context(settings=resolved, clock=clock)
    app = build_parent_graph().compile(
        name="lpr_cpe_parent", checkpointer=build_memory_checkpointer()
    )
    # Annotated rather than left to inference. `Pregel.ainvoke` and `aget_state` both want a
    # `RunnableConfig`, and a bare dict literal infers as `dict[str, dict[str, Any]]`, which is not
    # one -- four strict-mode errors, all of them the same mistake. D1: the thread id *is* the
    # incident id, so resumption is not a lookup problem.
    config: RunnableConfig = {"configurable": {"thread_id": service["service_ref"]}}

    await app.ainvoke(
        initial_state(service, case_type=case_type, now=started), context=ctx, config=config
    )

    pauses: dict[str, int] = {}
    settled = False
    for _ in range(MAX_LAPS):
        snapshot = await app.aget_state(config)
        if not snapshot.interrupts:
            settled = True
            break
        payload = snapshot.interrupts[0].value
        kind = pause_kind(payload)
        pauses[kind] = pauses.get(kind, 0) + 1
        answer = _answer(kind, service, clock, payload, approve=approve)
        await app.ainvoke(Command(resume=answer), context=ctx, config=config)

    final = (await app.aget_state(config)).values
    status = final.get("status")
    return Outcome(
        service_ref=service["service_ref"],
        status=getattr(status, "value", str(status)),
        escalated=bool(final.get("escalated")),
        reason=str(final.get("escalation_reason") or ""),
        pauses=pauses,
        nodes_entered=len(final.get("node_visits", {})),
        audit_events=len(final.get("audit_events", [])),
        actions=len(final.get("action_history", [])),
        settled=settled,
    )


def report_run(outcome: Outcome, out: TextIO) -> None:
    """Print one outcome. Every line is a fact read off the final state, not a summary of intent."""
    out.write(f"incident {outcome.service_ref}\n")
    out.write(f"  status          {outcome.status}\n")
    out.write(f"  escalated       {outcome.escalated}\n")
    if outcome.reason:
        out.write(f"  reason          {outcome.reason}\n")
    out.write(f"  pauses answered {outcome.pauses_answered}\n")
    for kind, count in sorted(outcome.pauses.items()):
        out.write(f"    {kind:26s} {count}\n")
    out.write(f"  nodes entered   {outcome.nodes_entered}\n")
    out.write(f"  audit events    {outcome.audit_events}\n")
    out.write(f"  actions taken   {outcome.actions}\n")
    if not outcome.settled:
        out.write(
            f"  DID NOT SETTLE  still pausing after {MAX_LAPS} resumes; every loop in this graph "
            "is meant to be bounded by the budget guard\n"
        )
    if outcome.pauses.get("unknown"):
        out.write(
            f"  UNKNOWN PAUSES  {outcome.pauses['unknown']} payload(s) matched no entry in "
            "PAUSE_SHAPES and were answered as approvals, which the parser will have rejected\n"
        )


def run_service(
    service_ref: str, out: TextIO, *, approve: bool = True, predictive: bool = False
) -> int:
    """Look the service up in the fixture set, drive it, print the outcome. Returns an exit code.

    Non-zero when the run did not settle or met a pause it could not name -- both are defects in
    this runner or in the guards, and neither should exit zero. An *escalated* incident exits zero:
    escalation is a legitimate ending and 40 of the 41 fixtures reach one.
    """
    from lpr_cpe.simulation.loader import load_fixtures

    fixtures = load_fixtures()
    if service_ref not in fixtures.services:
        out.write(f"no such service: {service_ref}\n")
        out.write(f"the fixture set holds {len(fixtures.services)}:\n")
        for ref in sorted(fixtures.services):
            out.write(f"  {ref}\n")
        return 2

    outcome = asyncio.run(
        drive(
            fixtures.services[service_ref],
            case_type=(CaseType.PREDICTIVE_MAINTENANCE if predictive else CaseType.PROACTIVE_ALARM),
            approve=approve,
        )
    )
    report_run(outcome, out)
    return 0 if outcome.settled and not outcome.pauses.get("unknown") else 1


__all__ = [
    "MAX_LAPS",
    "PAUSE_SHAPES",
    "Outcome",
    "ProductionWritesRefusedError",
    "approval",
    "crew_report",
    "drive",
    "initial_state",
    "pause_kind",
    "report_run",
    "run_service",
]
