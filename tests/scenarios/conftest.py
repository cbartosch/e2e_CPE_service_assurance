"""The harness the seventeen specification scenarios are driven through.

One arrangement, used by every scenario: the **real** parent graph, compiled over a real
checkpointer, against the committed fixture set, driven to a standstill by answering whatever it
pauses on. No node is mocked and no subgraph is substituted. A scenario suite that stubbed the part
under test would assert the stub.

Why this is not `runner.drive`
------------------------------
`runner.drive` exists to make `lpr-cpe run` demonstrable and answers every pause the same way: a
crew that found the fault at the drop and fixed it, a supervisor who says yes. That is one path.
Six of the seventeen scenarios are *about* the other answers -- a crew that finds plant work
outside its remit (6, 7), a customer who does not complete the steps (5), a plant repair that
leaves the premises still degraded (10). So the answering is a parameter here, and `drive`'s
`approve: bool` is not enough of one.

It also returns counts. A scenario asserts facts -- which delimiter was identified, whether a second
MR was raised, whether the SLA clock moved -- so this returns the final state.

What `Answers` is for
---------------------
A mapping from pause kind to a callable, with a default per kind. A scenario overrides the one it is
about and inherits the rest, so the thing that differs between two scenarios is visible in the two
lines that differ rather than in two copies of a script.

Answers can also vary by occurrence: `Answers.sequence(...)` gives a different answer to the first
and second pause of the same kind, which is what scenario 3 ("remote repair fails, *then* Clean
Boots succeeds") and scenario 9 need. Running out of scripted answers is an error rather than a
repeat of the last one -- a scenario that paused more times than its author expected has stopped
being the scenario they wrote, and silently repeating an answer would hide that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from langgraph.types import Command

from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.domain.enums import CaseType
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.persistence.checkpointer import build_memory_checkpointer
from lpr_cpe.runner import approval, crew_report, initial_state, pause_kind
from lpr_cpe.simulation.loader import build_simulated_adapters

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from langchain_core.runnables import RunnableConfig

    from lpr_cpe.config.settings import Settings

#: Every scenario starts here. Inside the crew scheduling window and outside quiet hours, for the
#: reason `tests/unit/test_api.py` records at length: the route this system takes genuinely depends
#: on the hour, and a scenario suite on the wall clock would be a suite that means something
#: different in the evening.
START = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)

#: Resume laps before the harness gives up. A scenario that pauses more than this is looping, and
#: the bounded-loop guard should have caught it -- so hitting this is a finding, which is why it
#: raises rather than returning what it has.
MAX_LAPS = 60


class TickingClock(FrozenClock):
    """Advances three seconds on every read.

    A frozen instant would make every KPI duration zero and every piece of evidence the same age,
    which is the opposite of what a scenario about stale telemetry needs. Deterministic because the
    step is fixed: the same run produces the same timestamps.
    """

    def now(self) -> datetime:
        return self.advance(timedelta(seconds=3))


# ------------------------------------------------------------------------------------------------
# The answers
# ------------------------------------------------------------------------------------------------


def release_window(payload: Mapping[str, Any], clock: TickingClock) -> dict[str, Any]:
    """Move the clock past the stability window's deadline.

    The resume *value* does not end this pause and that is the subtlety worth keeping: the wait
    re-checks the clock after every resume, so an answer that left the clock alone re-raises the
    identical interrupt and the harness spins to `MAX_LAPS`.
    """
    resume_at = (payload.get("stability_window_wait") or {}).get("resume_at")
    if resume_at:
        deadline = datetime.fromisoformat(resume_at)
        if deadline > clock.now():
            clock.set(deadline + timedelta(minutes=1))
    return {"resumed_by": "scheduler"}


def crew_found_plant_fault(service: Mapping[str, Any]) -> dict[str, Any]:
    """A Clean Boots crew that reaches the delimiter and finds the fault beyond it.

    The handover case. `work_completed` is false and `requires_plant_work` is true, which is what
    separates this from `runner.crew_report` -- and the measurements are still complete, because a
    handover with incomplete evidence is scenario 8 and has to be reachable *separately* from this
    one or neither test means anything.

    **`fault_domain` must move to `tap_or_odp` with it, and the first version of this helper did not
    move it.** `_submitted_finding` refuses a submission that claims plant work in a premises domain
    -- `if plant_work and not is_plant_side(domain): return None` -- and its docstring argues the
    case: promoting the domain on the crew's behalf would file an MR against a boundary nobody
    reported. So a report saying "the fault is at the drop *and* it is plant work" is a contradiction
    and is rejected whole, the crew is asked again, and the graph re-briefs until the node-reentries
    budget stops it. Measured: six `briefing` pauses and `node_reentries budget exhausted`, with
    scenarios 6, 7, 8, 9 and 10 all producing byte-identical runs because none of their reports ever
    parsed. The graph was right and the helper was wrong.
    """
    report = dict(crew_report(service))
    report.update(
        {
            "fault_domain": "tap_or_odp",
            "fault_confirmed": True,
            "work_completed": False,
            "requires_plant_work": True,
            "parts_replaced": [],
            "technician_note": (
                "premises and drop test clean to the delimiter; the fault is upstream of it"
            ),
            "customer_confirmed": False,
        }
    )
    return report


def crew_found_nothing(service: Mapping[str, Any]) -> dict[str, Any]:
    """A crew that finds no fault at all. Neither a repair nor a handover."""
    report = dict(crew_report(service))
    report.update(
        {
            "fault_confirmed": False,
            "no_fault_found": True,
            "work_completed": False,
            "requires_plant_work": False,
            "parts_replaced": [],
            "technician_note": "no fault found at the premises or the drop",
            "customer_confirmed": False,
        }
    )
    return report


def crew_evidence_incomplete(service: Mapping[str, Any]) -> dict[str, Any]:
    """A handover attempt with the measurements missing. Scenario 8's input.

    Only the measurements are dropped. Everything else stays complete, so a failure to raise the MR
    is attributable to the missing evidence and not to five other things being absent as well.
    """
    report = crew_found_plant_fault(service)
    report["measurements"] = {}
    report["evidence_refs"] = []
    return report


def plant_repaired() -> dict[str, Any]:
    return {
        "status": "completed",
        "osp_owner": "osp.crew",
        "note": "span repaired",
        "resolution_code": "splice_replaced",
    }


def plant_failed() -> dict[str, Any]:
    """OSP attended and did not fix it. Scenario 9."""
    return {
        "status": "failed",
        "osp_owner": "osp.crew",
        "note": "the reported tap tests clean; the impairment is not at this point",
        "resolution_code": "no_fault_found",
    }


def customer_completed() -> dict[str, Any]:
    return {"response": "completed", "customer_completed_step": True}


def customer_did_not_complete() -> dict[str, Any]:
    """The self-help window closes with nothing done. Scenario 5."""
    return {"response": "failed", "customer_completed_step": False}


#: The default answer for each pause kind: approve, and report a repair that worked. A scenario
#: overrides the one it is about.
_DEFAULTS: dict[str, Callable[..., Any]] = {
    "approval_request": lambda service, clock, payload: approval(approved=True),
    "briefing": lambda service, clock, payload: crew_report(service),
    "field_submission_request": lambda service, clock, payload: crew_report(service),
    "plant_report_request": lambda service, clock, payload: plant_repaired(),
    "customer_response_request": lambda service, clock, payload: customer_completed(),
    "stability_window_wait": lambda service, clock, payload: release_window(payload, clock),
}


@dataclass
class Answers:
    """How a scenario answers each kind of pause.

    `overrides` replaces the default for a kind outright. `sequences` gives an ordered list for a
    kind, consumed one per pause of that kind, and is what makes "the first attempt fails and the
    second succeeds" expressible without a stateful closure in every scenario that needs one.
    """

    overrides: dict[str, Callable[..., Any]] = field(default_factory=dict)
    sequences: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    _used: dict[str, int] = field(default_factory=dict, init=False)

    @classmethod
    def sequence(cls, kind: str, *answers: Callable[..., Any], **over: Any) -> Answers:
        built = cls(**over)
        built.sequences[kind] = list(answers)
        return built

    def answer(
        self, kind: str, service: Mapping[str, Any], clock: TickingClock, payload: Any
    ) -> Any:
        seen = self._used.get(kind, 0)
        self._used[kind] = seen + 1

        scripted = self.sequences.get(kind)
        if scripted is not None:
            if seen >= len(scripted):
                raise AssertionError(
                    f"the scenario scripted {len(scripted)} answer(s) for {kind!r} and the graph "
                    f"paused on it {seen + 1} times. That is a different run from the one the "
                    "scenario describes, so the harness stops rather than repeating the last "
                    "answer and hiding it."
                )
            return scripted[seen](service, clock, payload)

        handler = self.overrides.get(kind) or _DEFAULTS.get(kind)
        if handler is None:
            raise AssertionError(
                f"the graph paused on {kind!r}, which is not one of `runner.PAUSE_SHAPES` and has "
                "no answer here. A sixth pause shape needs a handler, not a guess: an approval "
                "handed to a crew-report parser is rejected in silence."
            )
        return handler(service, clock, payload)

    def counts(self) -> dict[str, int]:
        return dict(self._used)


# ------------------------------------------------------------------------------------------------
# The run
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioRun:
    """Everything one scenario needs to assert on, read off the final state.

    The state itself is exposed rather than a curated summary. A scenario asserting a fact the
    harness did not think to summarise should be able to, and a summary that grew a field per
    scenario would end up being the thing under test.
    """

    service_ref: str
    case_type: CaseType
    status: str
    escalated: bool
    escalation_reason: str
    settled: bool
    pauses: tuple[str, ...]
    state: Mapping[str, Any]
    gate_records: tuple[Mapping[str, Any], ...]
    staged_outbox: tuple[Any, ...]

    def nodes(self) -> frozenset[str]:
        """Every node the run entered, by name."""
        return frozenset(self.state.get("node_visits") or {})

    def entered(self, name: str) -> bool:
        return any(name in node for node in self.nodes())

    def action_types(self) -> tuple[str, ...]:
        history = self.state.get("action_history") or []
        return tuple(
            str(getattr(getattr(item, "action_type", None), "value", "")) for item in history
        )

    def audit_types(self) -> tuple[str, ...]:
        return tuple(
            str(
                getattr(getattr(item, "event_type", None), "value", getattr(item, "event_type", ""))
            )
            for item in (self.state.get("audit_events") or [])
        )

    def count(self, key: str) -> int:
        return len(self.state.get(key) or [])

    def value(self, key: str) -> Any:
        raw = self.state.get(key)
        return getattr(raw, "value", raw)

    @property
    def pause_counts(self) -> dict[str, int]:
        return {kind: self.pauses.count(kind) for kind in set(self.pauses)}


def with_one_rejection(values: Mapping[str, Any]) -> dict[str, Any]:
    """The state plus the one fact gap EXEC-1 says no fixture supplies: a discarded explanation.

    `HandoverContract.missing_items()` requires `ruled_out` to be non-empty and the field-execution
    stage fills it from `RCAResult.ruled_out`. **Nothing in `src` can ever put anything there.**
    `graph.nodes.diagnosis._rejected_before` is the only writer of rejections and it seeds them from
    the *previous* RCA's `ruled_out`, which is `[h for h in hypotheses if h.rejected]` -- a closed
    loop with an empty initial condition. Confirmed against all 41 services here as well: every one
    of the twelve fixtures that reaches a Clean Boots dispatch finishes with `ruled_out == 0`,
    `complete is False` and `missing_items() == ["ruled_out"]`.

    So scenarios 6 and 7 cannot reach their specified outcome unaided, and this is what lets them be
    driven anyway: one rejected hypothesis, seeded onto the state at the pause before the handover is
    validated. Built through `RCAHypothesis(...)` rather than `model_copy` because
    `_rejection_is_explained` refuses a rejection with no reason and a copy would skip the validator
    -- seeding a state P10 cannot produce would make the evidence worthless.

    The same technique `tests/unit/test_subgraph_field_execution.py` uses at the subgraph seam; here
    it runs against the parent graph, so the chain it exercises is the whole of D18 -> P19 -> P20.
    """
    from lpr_cpe.domain.diagnosis import RCAHypothesis, RCAResult

    rca = values.get("rca")
    if rca is None or not rca.hypotheses:
        raise AssertionError(
            "no RCA on the state yet, so there is no hypothesis to reject. Seed at a pause that "
            "happens after `determine_root_cause`."
        )
    live = rca.hypotheses[0]
    rejected = RCAHypothesis(
        **{
            **live.model_dump(),
            "hypothesis_id": f"{live.hypothesis_id}-RULED-OUT",
            "rejected": True,
            "rejection_reason": "the drop tested clean end to end at the premises",
        }
    )
    return {"rca": RCAResult(**{**rca.model_dump(), "hypotheses": [*rca.hypotheses, rejected]})}


async def run_scenario(
    service: Mapping[str, Any],
    *,
    case_type: CaseType = CaseType.PROACTIVE_ALARM,
    answers: Answers | None = None,
    settings: Settings | None = None,
    start: datetime = START,
    thread_suffix: str = "",
    on_pause: Callable[[int, str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
    restart_at: int | None = None,
) -> ScenarioRun:
    """Drive one incident through the real parent graph to a standstill.

    The gate is built here rather than left to `build_context` so that `gate_records` and the staged
    outbox are readable afterwards -- several scenarios are about what did *not* leave the process,
    and "no truck roll was created" is a claim about the gate's record, not about the state.

    `on_pause(lap, kind, state)` may return a state update, applied with `aupdate_state` before the
    resume. Two scenarios need it and neither is a convenience: 6 and 7 have to seed the rejected
    hypothesis gap EXEC-1 says no fixture produces, and it has to be seeded *mid-run*, after
    `determine_root_cause` and before the handover is validated.

    `restart_at` rebuilds the compiled graph at that lap while keeping the same checkpointer and
    thread, which is scenario 13. A new `Pregel` over the same saver is what "the application
    restarts" means to this system -- the state lives in the checkpointer, not in the object.
    """
    resolved_answers = answers or Answers()
    clock = TickingClock(start)
    adapters = build_simulated_adapters(clock=clock)
    context = build_context(settings=settings, clock=clock, adapters=adapters)
    saver = build_memory_checkpointer()
    app = build_parent_graph().compile(name="lpr_cpe_parent", checkpointer=saver)
    thread = f"INC-{service['service_ref']}{thread_suffix}"
    config: RunnableConfig = {"configurable": {"thread_id": thread}}

    await app.ainvoke(
        initial_state(dict(service), case_type=case_type, now=start), context=context, config=config
    )

    seen: list[str] = []
    settled = False
    for lap in range(MAX_LAPS):
        if restart_at is not None and lap == restart_at:
            # Deliberately a *new* compiled graph over the *same* saver. Reusing `app` would prove
            # nothing about persistence; rebuilding the saver too would prove nothing about resume.
            app = build_parent_graph().compile(name="lpr_cpe_parent", checkpointer=saver)

        snapshot = await app.aget_state(config)
        if not snapshot.interrupts:
            settled = True
            break
        payload = snapshot.interrupts[0].value
        kind = pause_kind(payload)
        seen.append(kind)

        if on_pause is not None:
            update = on_pause(lap, kind, snapshot.values)
            if update:
                await app.aupdate_state(config, dict(update))

        await app.ainvoke(
            Command(resume=resolved_answers.answer(kind, service, clock, payload)),
            context=context,
            config=config,
        )

    if not settled:
        raise AssertionError(
            f"{service['service_ref']} paused {MAX_LAPS} times without settling. The bounded-loop "
            f"guard should have stopped this first. Pauses seen: {seen}"
        )

    final = (await app.aget_state(config)).values
    status = final.get("status")
    return ScenarioRun(
        service_ref=str(service["service_ref"]),
        case_type=case_type,
        status=str(getattr(status, "value", status)),
        escalated=bool(final.get("escalated")),
        escalation_reason=str(final.get("escalation_reason") or ""),
        settled=settled,
        pauses=tuple(seen),
        state=final,
        gate_records=tuple(adapters.gate.recorded),
        staged_outbox=tuple(adapters.gate.staged.pending),
    )


@pytest.fixture
def scenario() -> Any:
    """`run_scenario` handed over as a value, for `tests/conftest.py`'s reason.

    Importing a helper out of a conftest works by accident of `sys.path` and stops the moment a
    nearer conftest appears.
    """
    return run_scenario


@pytest.fixture
def answers() -> Any:
    return Answers


def service_named(fixtures: Any, ref: str) -> Mapping[str, Any]:
    """One fixture service, or a failure that says which refs exist.

    A `KeyError` on a service ref is the least informative failure a scenario can have -- the ref
    looks plausible, the fixture set is 41 entries long, and the mistake is usually one character.
    """
    try:
        return dict(fixtures.services[ref])
    except KeyError:
        available = sorted(fixtures.services)
        raise AssertionError(
            f"no fixture service {ref!r}. {len(available)} exist; the nearest are "
            f"{[s for s in available if s[:10] == ref[:10]] or available[:5]}"
        ) from None


def services_on(fixtures: Any, *, delimiter_ref: str) -> Sequence[Mapping[str, Any]]:
    """Every service behind one delimiter. Scenario 1 needs the shared-node cohort."""
    return [
        dict(service)
        for service in fixtures.services.values()
        if service.get("delimiter_ref") == delimiter_ref
    ]
