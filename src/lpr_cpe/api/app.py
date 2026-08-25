"""The thirteen endpoints, and the lifespan that owns the checkpointer for all of them.

`checkpointer_scope` is an async context manager -- IMPLEMENTATION_PLAN.md §2 records what it cost
to learn that `AsyncPostgresSaver.from_conn_string` returns an unentered helper rather than a saver,
and that a synchronous factory returning it could never have worked. So the connection is opened in
the lifespan and closed on shutdown, and the compiled graph is built once and held on `app.state`.
Compiling per request would be wasteful and, worse, would give each request its own subgraph
instances, which is the kind of thing that works until something is cached on a compiled object.

Every read goes through `graph.inspect`
---------------------------------------
Not through `aget_state(...).values`. A paused subgraph's writes are not in the parent's state --
measured, four of the six gates are nested, and the naive read reports `diagnosing` for an incident
that has been on a supervisor's queue for a week. `effective_state` merges parent-first so the
paused child's newer values win, and `is_awaiting_human` reads `.interrupts` rather than inferring
from status.

Every response is redacted
--------------------------
`security.redaction.redact` runs over every body on the way out. An audit timeline is precisely the
payload that carries a MAC, an IP and a customer name out of the process, and this is the boundary
that obligation attaches to. A caller cannot read a full MAC back out of this API; that is the
trade,
and it is the one the specification asks for.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Request, status
from langgraph.types import Command

from lpr_cpe.api.models import (
    ApprovalIn,
    CustomerResponseIn,
    EventIn,
    IncidentAccepted,
    ResumePayload,
    ResumeResult,
    TimelineEntry,
    WebhookIn,
    WebhookResult,
)
from lpr_cpe.api.security import WriteGuard
from lpr_cpe.config.settings import Settings, get_settings
from lpr_cpe.domain.enums import KPIName
from lpr_cpe.domain.records import AssuranceEvent, SLAContext
from lpr_cpe.graph import inspect as graph_inspect
from lpr_cpe.graph.builder import build_parent_graph
from lpr_cpe.graph.context import build_context
from lpr_cpe.graph.state import make_initial_state
from lpr_cpe.observability.kpi import NOT_DERIVABLE_FROM_STATE
from lpr_cpe.persistence.checkpointer import checkpointer_scope
from lpr_cpe.security.rbac import can_approve
from lpr_cpe.security.redaction import redact

#: The webhook sources the specification names. A fifth would need a route, so the set is the route
#: table rather than a free-form path parameter -- an unknown source is a 404 rather than a silently
#: accepted delivery for a system nobody integrated.
WEBHOOK_SOURCES: frozenset[str] = frozenset({"nxt", "wfm", "jtrack", "tmf"})


class _Deliveries:
    """Seen webhook delivery ids, for the duplicate suppression the specification asks for.

    **In-process and unbounded, and both are gaps rather than choices.** A restart forgets every
    delivery, so a redelivery after one is processed twice; and nothing evicts, so a long-running
    process grows by one entry per webhook forever. The honest fix is the transactional outbox's
    table -- a delivery id is exactly the kind of thing that wants to be durable and unique -- and
    until that exists this is what "idempotent webhook processing" amounts to here. Gap API-2.

    Kept as a class rather than a module-level dict so that two apps in one process (which the tests
    build) do not share suppression state.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def first_time(self, delivery_id: str) -> bool:
        """True the first time this id is offered, False every time after."""
        if delivery_id in self._seen:
            return False
        self._seen.add(delivery_id)
        return True

    def __len__(self) -> int:
        return len(self._seen)


def _graph(request: Request) -> Any:
    return request.app.state.graph


def _deliveries(request: Request) -> _Deliveries:
    seen = request.app.state.deliveries
    # A lifespan invariant rather than input validation: a wrong object here is a wiring bug and
    # should say so, not fail three frames later.
    assert isinstance(seen, _Deliveries)
    return seen


def _config(incident_id: str) -> Any:
    """D1: the thread id *is* the incident id, so resumption is not a lookup problem."""
    return {"configurable": {"thread_id": incident_id}}


async def _known(app: Any, incident_id: str) -> Any:
    """The state of a thread that exists, or a 404.

    A thread nobody has started has an empty state rather than raising, so "does this incident
    exist?" is `not state` -- there is no index to ask. That is gap API-1, and the 404 is the
    honest answer either way.
    """
    state = await graph_inspect.effective_state(app, _config(incident_id))
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no thread for incident {incident_id!r}",
        )
    return state


def _status_of(state: Any) -> str:
    value = state.get("status")
    return str(getattr(value, "value", value or "unknown"))


async def _accepted(app: Any, incident_id: str) -> IncidentAccepted:
    state = await graph_inspect.effective_state(app, _config(incident_id))
    return IncidentAccepted(
        incident_id=incident_id,
        status=_status_of(state),
        awaiting_human=await graph_inspect.is_awaiting_human(app, _config(incident_id)),
    )


def build_app(*, settings: Settings | None = None) -> FastAPI:
    """Assemble the app. `settings` is injectable so a test can drive the production profile."""
    resolved = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the checkpointer once, compile once, and hold both for the process.

        The scope is entered here rather than per request because a Postgres saver holds a
        connection: `checkpointer_scope` opens it on `__aenter__` and `setup()`s the DDL, and doing
        that per request would be a connection per request and a migration check per request.
        """
        async with checkpointer_scope(resolved) as saver:
            app.state.graph = build_parent_graph().compile(
                name="lpr_cpe_parent", checkpointer=saver
            )
            app.state.deliveries = _Deliveries()
            app.state.settings = resolved
            yield

    app = FastAPI(
        title="LPR CPE service assurance",
        version="0.1.0",
        summary="Predictive CPE service assurance for HFC and PON, with resumable approval gates.",
        lifespan=lifespan,
    )

    # --------------------------------------------------------------------------------------------
    # Liveness, readiness, metrics
    # --------------------------------------------------------------------------------------------

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, Any]:
        """Liveness. Deliberately answers without touching the graph or the checkpointer.

        A health check that compiled a graph would report unhealthy for a topology error, which is a
        deploy-time fault rather than a liveness one, and would make a load balancer pull a process
        that was serving fine.
        """
        return {"status": "ok", "mode": resolved.app_mode.value}

    @app.get("/ready", tags=["ops"])
    async def ready(request: Request) -> dict[str, Any]:
        """Readiness. Answers `false` until the lifespan has compiled the graph.

        The distinction from `/health` is the one a scheduler needs: a process that is up but has
        not opened its checkpointer cannot serve a resume, and reporting it ready would route an
        approval at it and lose the answer.
        """
        compiled = getattr(request.app.state, "graph", None)
        return {
            "ready": compiled is not None,
            "writes_permitted": resolved.writes_permitted,
            "deliveries_seen": len(getattr(request.app.state, "deliveries", ()) or ()),
        }

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> dict[str, Any]:
        """The KPI vocabulary, not the values.

        There is no metric store: KPI events live on an incident's state, so "the current value of
        `truck_rolls_per_incident`" is a question about a population this process cannot enumerate
        -- the same missing index as `GET /incidents`. What is publishable is the definition set,
        which is what a dashboard needs to know it is asking for the right names. Gap API-3.
        """
        derivable = [name for name in KPIName if name not in NOT_DERIVABLE_FROM_STATE]
        return {
            "declared": len(KPIName),
            "derivable_from_state": sorted(name.value for name in derivable),
            "not_derivable_from_state": sorted(name.value for name in NOT_DERIVABLE_FROM_STATE),
            "note": (
                "definitions only. KPI values are per-incident and live on graph state; this "
                "process holds no incident index to aggregate over. See gap API-3."
            ),
        }

    # --------------------------------------------------------------------------------------------
    # Starting a thread
    # --------------------------------------------------------------------------------------------

    async def _start(app_: Any, body: EventIn) -> IncidentAccepted:
        incident_id = body.incident_id or f"INC-{body.service_ref}"
        now = datetime.now(tz=UTC)
        event = AssuranceEvent(
            event_id=body.event_id,
            source=body.source,
            case_type=body.case_type,
            technology=body.technology,
            severity=body.severity,
            occurred_at=now,
            received_at=now,
            customer_ref=body.customer_ref,
            service_ref=body.service_ref,
            cpe_ref=body.cpe_ref,
            summary=body.summary,
        )
        state = make_initial_state(
            incident_id=incident_id,
            correlation_id=f"COR-{body.service_ref}",
            event=event,
            # No product tier or vulnerability flag on the wire, so the SLA context is the default
            # one. That is a real limitation rather than a simplification: `sla.at_risk` and the
            # dispatch objective both read it, and a caller who knows the customer is vulnerable has
            # no way to say so. Gap API-4.
            sla=SLAContext(clock_started_at=now),
            now=now,
        )
        await app_.ainvoke(
            state, context=build_context(settings=resolved), config=_config(incident_id)
        )
        return await _accepted(app_, incident_id)

    @app.post("/events", status_code=status.HTTP_202_ACCEPTED, tags=["intake"])
    async def post_event(
        body: EventIn, _: WriteGuard, app_: Annotated[Any, Depends(_graph)]
    ) -> IncidentAccepted:
        """An event from a monitoring system. Starts a thread and runs it to its first pause."""
        return await _start(app_, body)

    @app.post("/incidents", status_code=status.HTTP_202_ACCEPTED, tags=["intake"])
    async def post_incident(
        body: EventIn, _: WriteGuard, app_: Annotated[Any, Depends(_graph)]
    ) -> IncidentAccepted:
        """A customer-reported incident. The same intake, and the specification names both.

        Identical to `POST /events` today, and that is honest rather than lazy: intake normalises
        every source through P01, and `EventSource` is what distinguishes a customer call from an
        NXT
        alarm. Two routes because the specification asks for two; one implementation because there
        is
        one intake.
        """
        return await _start(app_, body)

    # --------------------------------------------------------------------------------------------
    # Reading one incident
    # --------------------------------------------------------------------------------------------

    @app.get("/incidents/{incident_id}", tags=["incidents"])
    async def get_incident(
        app_: Annotated[Any, Depends(_graph)],
        incident_id: Annotated[str, Path(max_length=128)],
    ) -> dict[str, Any]:
        """The summary a queue row is built from."""
        state = await _known(app_, incident_id)
        summary: dict[str, Any] = redact(
            {
                "incident_id": incident_id,
                "status": _status_of(state),
                "escalated": bool(state.get("escalated")),
                "escalation_reason": state.get("escalation_reason") or None,
                "technology": getattr(state.get("technology"), "value", None),
                "fault_domain": getattr(state.get("fault_domain"), "value", None),
                "awaiting_human": await graph_inspect.is_awaiting_human(app_, _config(incident_id)),
                "truck_rolls": int(state.get("field_visit_count", 0)),
                "updated_at": str(state.get("updated_at") or ""),
            }
        )
        return summary

    @app.get("/incidents/{incident_id}/state", tags=["incidents"])
    async def get_incident_state(
        app_: Annotated[Any, Depends(_graph)],
        incident_id: Annotated[str, Path(max_length=128)],
    ) -> dict[str, Any]:
        """Where the thread is, and what it is waiting for. Read through `graph.inspect`.

        `pending_approval` and `awaiting_node_path` are the two fields the naive read gets wrong,
        and they are the two an operator's queue is built from. See the module docstring.
        """
        await _known(app_, incident_id)
        config = _config(incident_id)
        pending = await graph_inspect.pending_approval_for(app_, config)
        state = await graph_inspect.effective_state(app_, config)
        detail: dict[str, Any] = redact(
            {
                "incident_id": incident_id,
                "status": _status_of(state),
                "awaiting_human": await graph_inspect.is_awaiting_human(app_, config),
                "awaiting_node_path": list(await graph_inspect.awaiting_node_path(app_, config)),
                "pending_approval": (
                    pending.model_dump(mode="json") if pending is not None else None
                ),
                "interrupts": await graph_inspect.interrupt_payloads(app_, config),
                "node_visits": dict(state.get("node_visits", {})),
            }
        )
        return detail

    @app.get("/incidents/{incident_id}/timeline", tags=["incidents"])
    async def get_incident_timeline(
        app_: Annotated[Any, Depends(_graph)],
        incident_id: Annotated[str, Path(max_length=128)],
    ) -> list[TimelineEntry]:
        """Every audit event, in the order the graph wrote them. The `why` for each decision."""
        state = await _known(app_, incident_id)
        entries = [
            TimelineEntry(
                event_id=event.event_id,
                occurred_at=event.occurred_at.isoformat(),
                node=event.node,
                action=event.action,
                outcome=event.outcome,
                actor=event.actor,
                reason_code=getattr(event.reason_code, "value", None),
            )
            for event in state.get("audit_events", [])
        ]
        return [TimelineEntry.model_validate(redact(e.model_dump())) for e in entries]

    # --------------------------------------------------------------------------------------------
    # Answering a gate
    # --------------------------------------------------------------------------------------------

    async def _resume(app_: Any, incident_id: str, value: Any) -> ResumeResult:
        config = _config(incident_id)
        await _known(app_, incident_id)
        if not await graph_inspect.is_awaiting_human(app_, config):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"incident {incident_id!r} is not paused, so there is nothing to resume. Its "
                    f"status is {_status_of(await graph_inspect.effective_state(app_, config))!r}."
                ),
            )
        await app_.ainvoke(
            Command(resume=value), context=build_context(settings=resolved), config=config
        )
        state = await graph_inspect.effective_state(app_, config)
        return ResumeResult(
            incident_id=incident_id,
            status=_status_of(state),
            awaiting_human=await graph_inspect.is_awaiting_human(app_, config),
            resumed=True,
        )

    @app.post("/incidents/{incident_id}/resume", tags=["approvals"])
    async def resume_incident(
        body: ResumePayload,
        _: WriteGuard,
        app_: Annotated[Any, Depends(_graph)],
        incident_id: Annotated[str, Path(max_length=128)],
    ) -> ResumeResult:
        """The generic resume, for whichever gate is paused.

        `ResumePayload` refuses an empty mapping, and that validator is the whole reason this body
        is
        a model rather than a raw dict: `Command(resume={})` is read as a map that resumes nothing,
        so the graph re-pauses having run no node and written no audit event, and this endpoint
        would
        return 200 for a request that did nothing. Measured -- see IMPLEMENTATION_PLAN.md §2.
        """
        return await _resume(app_, incident_id, body.value)

    @app.post("/incidents/{incident_id}/approvals", tags=["approvals"])
    async def post_approval(
        body: ApprovalIn,
        _: WriteGuard,
        app_: Annotated[Any, Depends(_graph)],
        incident_id: Annotated[str, Path(max_length=128)],
    ) -> ResumeResult:
        """Answer the approval gate this incident is paused at, with the role checked here.

        The RBAC check is at the boundary and not in the graph, deliberately. `can_approve` reads a
        role against an `ApprovalKind`, and the kind is a property of the *pending request* -- so
        the only place that can ask the question is one that has read the pause. A node cannot: by
        the time it sees the answer it has already been resumed.

        A wrong role is a 403 and the gate stays paused, which is the behaviour that matters: the
        alternative is resuming with a refusal the operator did not intend, which is
        indistinguishable
        downstream from a real rejection.
        """
        config = _config(incident_id)
        await _known(app_, incident_id)
        pending = await graph_inspect.pending_approval_for(app_, config)
        if pending is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"incident {incident_id!r} has no approval outstanding. Use `/resume` for the "
                    "gates that are not approvals -- a crew report or a customer reply."
                ),
            )
        if not can_approve(body.decided_by_role, pending.kind):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role {body.decided_by_role!r} may not decide a "
                    f"{pending.kind.value!r} approval. The gate is still waiting."
                ),
            )
        return await _resume(app_, incident_id, body.model_dump(mode="json"))

    @app.post("/incidents/{incident_id}/customer-response", tags=["approvals"])
    async def post_customer_response(
        body: CustomerResponseIn,
        _: WriteGuard,
        app_: Annotated[Any, Depends(_graph)],
        incident_id: Annotated[str, Path(max_length=128)],
    ) -> ResumeResult:
        """A customer's reply to a self-help instruction. Parsed by `self_help.customer_reply`."""
        return await _resume(app_, incident_id, body.model_dump(mode="json"))

    # --------------------------------------------------------------------------------------------
    # Webhooks
    # --------------------------------------------------------------------------------------------

    @app.post("/webhooks/{source}", tags=["webhooks"])
    async def post_webhook(
        body: WebhookIn,
        _: WriteGuard,
        app_: Annotated[Any, Depends(_graph)],
        seen: Annotated[_Deliveries, Depends(_deliveries)],
        source: Annotated[str, Path(max_length=32)],
    ) -> WebhookResult:
        """One notification from an external system, suppressed if it is a redelivery.

        **Suppression comes first, before anything reads the graph**, which is the only order that
        satisfies "a duplicate webhook does not create a duplicate incident, work order, remote
        action or MR": a check performed after the side effect is not a check.

        A delivery naming no incident is accepted and recorded and does nothing else. That is not a
        stub -- it is the honest shape of a notification this system cannot route. There is no
        incident index to search for the subject, so an NXT alarm arriving for a service with no
        open thread has nowhere to go; making it *start* one would turn every alarm redelivery into
        a new incident, which is precisely what the specification forbids. Gap API-1 again.
        """
        if source not in WEBHOOK_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no webhook for {source!r}; this system has {sorted(WEBHOOK_SOURCES)}",
            )
        if not seen.first_time(body.delivery_id):
            return WebhookResult(
                delivery_id=body.delivery_id,
                accepted=False,
                duplicate=True,
                detail="already processed; nothing was done a second time",
            )
        if body.incident_id is None:
            return WebhookResult(
                delivery_id=body.delivery_id,
                accepted=True,
                duplicate=False,
                detail=(
                    "recorded. No incident named and no index to resolve one, so the graph was not "
                    "touched -- see gap API-1"
                ),
            )
        state = await graph_inspect.effective_state(app_, _config(body.incident_id))
        if not state:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no thread for incident {body.incident_id!r}",
            )
        awaiting = await graph_inspect.is_awaiting_human(app_, _config(body.incident_id))
        return WebhookResult(
            delivery_id=body.delivery_id,
            accepted=True,
            duplicate=False,
            detail=(
                f"incident {body.incident_id} is {_status_of(state)}"
                + (" and awaiting a human" if awaiting else "")
                + ". This system consumes notifications through adapters rather than through "
                "webhook bodies, so nothing was applied -- see gap API-5"
            ),
        )

    return app


def create_app() -> FastAPI:
    """The ASGI factory `uvicorn lpr_cpe.api.app:create_app --factory` names."""
    return build_app()


__all__ = ["WEBHOOK_SOURCES", "build_app", "create_app"]
