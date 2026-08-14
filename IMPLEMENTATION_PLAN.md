# Implementation plan — LPR CPE predictive service assurance

Running status document. Updated as work lands; the "Status" table near the bottom is the single
owner of what is done and what is not. If this file and the code disagree, the code is right and
this file is a bug.

---

## 1. Assumptions recorded up front

**A1 — The master prompt is the sole source of truth.** The repository was empty at the start of
this work (`git clone` reported *"You appear to have cloned an empty repository"*; `main` had no
commits). No LPR-supplied materials, no NXT or jTrack API specifications, no fixtures and no
existing conventions were present to reuse. The specification therefore stands alone, as it
instructs, and every external-system field name below is our own invention *labelled as such*.

**A2 — No vendor endpoint is real.** Nothing in `src/lpr_cpe/integrations/` names a real ServAssure
NXT, jTrack, WFM or CRM endpoint. Every adapter is a Protocol plus a fixture-backed simulator, and
the difference between what the specification told us and what we guessed is recorded per-adapter in
`docs/vendor-integration-gaps.md`. That file is the deliverable that makes A1 falsifiable: when real
API documentation arrives, it is the list of things to check.

**A3 — Puerto Rico is the operating geography, and the clock is `America/Puerto_Rico`.** Fixed
UTC-04:00, no daylight saving. All scheduled work (the 07:00 / 21:00 predictive scans) is expressed
in that zone; all stored timestamps are timezone-aware UTC. There is no naive datetime anywhere.

**A4 — Production writes are off unless explicitly switched on.** `APP_MODE` defaults to
`simulation` and `ALLOW_PRODUCTION_WRITES` defaults to `false`. A write adapter reached in
simulation mode records the intent and returns a simulated result; it does not call out. This is
enforced in one place (`integrations/base.py`), not restated per adapter.

**A5 — Determinism boundary.** No language model is consulted for a number that a decision depends
on. Detectors, scoring, banding, verdicts, clustering, dispatch, SLA arithmetic and policy
evaluation are ordinary Python. The model is asked only for prose, and where the specification's own
reference pipeline had the model emit `verdict` and `wifi_health_score`, we take the documented
option (a): those fields are **absent from the schema the model is given** and are merged in from
the deterministic scorer afterwards. See §4.

---

## 2. Verified API surface (measured, not remembered)

The specification requires that LangGraph and LangChain APIs be checked against the current release
rather than recalled. Checked on **2026-08-14** by introspecting the installed distributions and by
running a probe graph. Versions resolved into `.venv` on Python **3.14.2**:

| Package | Version |
| --- | --- |
| `langgraph` | 1.2.11 |
| `langgraph-checkpoint` | 4.2.0 |
| `langgraph-checkpoint-postgres` | 3.1.2 |
| `langchain-core` | 1.5.4 |
| `pydantic` | 2.13.4 |
| `fastapi` | 0.141.1 |
| `ortools` | 9.15.6755 |
| `psycopg` | 3.3.4 |

Observed signatures and behaviours that the design depends on:

- `from langgraph.graph import StateGraph, START, END` — `START == "__start__"`, `END == "__end__"`.
- `StateGraph(state_schema, context_schema=None, *, input_schema=None, output_schema=None)`.
  `config_schema` is only reachable through the deprecated-kwargs path, so we pass `context_schema`.
- `add_node(name, action, *, defer=False, retry_policy=..., cache_policy=..., destinations=...,
  timeout=...)`. Async callables are accepted directly; every node in this repo is `async def`.
- `compile(checkpointer=None, *, cache=None, store=None, interrupt_before=None,
  interrupt_after=None, debug=False, name=None)`.
- `from langgraph.types import interrupt, Command, Interrupt`; `interrupt(value: Any) -> Any`;
  `Command` carries `graph`, `update`, `resume`, `goto` and the `Command.PARENT` sentinel.
- **A paused `ainvoke` returns normally.** It does not raise. The returned mapping gains an
  `"__interrupt__"` key holding a list of `Interrupt`, each with `.value` and `.id` (observed id
  `bd5d6edb6e81a918fd3b0edfb8fff8e3`, a 32-char hex string). This is the observable the API layer
  uses to decide whether an incident is awaiting a human, so it is asserted in a test rather than
  trusted.
- `await graph.aget_state(config)` returns a snapshot with `.next` (`('gate',)` while paused, `()`
  when finished) **and** `.interrupts` as a tuple, mirrored on `.tasks[i].interrupts`. Both were
  confirmed present; `.interrupts` is what `GET /incidents/{id}/state` reports.
- Resuming several interrupts at once works by **id → value mapping**:
  `Command(resume={i.id: value for i in interrupts})`. Confirmed against two parallel nodes.
- A **compiled subgraph added as a node inherits the parent's checkpointer** and an `interrupt()`
  raised inside it surfaces on the parent's `__interrupt__`; `Command(resume=...)` at the parent
  resumes it. This is what lets the approval gates live inside subgraphs.
- `from langgraph.checkpoint.memory import InMemorySaver`.
- `from langgraph.checkpoint.postgres import PostgresSaver` and
  `from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver`, `await saver.setup()`.

**Finding that changed the design:** importing `langgraph.checkpoint.postgres` raises
`ImportError: no pq wrapper available` unless `libpq` is present — the bare `psycopg` wheel is not
enough, `psycopg[binary]` is. A top-level import would therefore make the in-memory path
un-runnable on any machine without Postgres client libraries, including CI. **The Postgres
checkpointer is imported lazily inside the factory**, and the in-memory path never touches it.

**Deliberately not relied upon:** a `graph.stream_events(..., version="v3")` form with
`stream.interrupted` / `stream.interrupts` attributes appeared in a documentation summary during
research. It was not reproduced against 1.2.11 and nothing here uses it.

---

## 3. Priority order for this pass

The specification's own fallback order is followed, because the full 19-step programme is larger
than one pass: **correct domain models and state contract → parent graph with real routing and
policy checks → persistence and resumable approvals → adapters, dispatch, tests → docs and
diagrams.** Fewer fully working, fully tested paths beat more stubs. Anything incomplete is named in
§6 rather than left to be discovered.

Build order actually used:

1. `config` — settings, modes, the clock, the two feature switches from A4.
2. `domain` — the record types the specification names, as Pydantic v2 models with validators. Its
   bullet list has **34** entries, not the 33 a quick count suggests; the number is asserted by a
   test that parses the specification rather than trusted to a comment.
3. `graph/state.py` — the state contract and its append-only reducers.
4. `security` — redaction and the boundary rule, because §4's MAC masking is a *boundary*
   obligation that later code must be able to call, not an afterthought.
5. `policies` — YAML pack plus the engine returning `allowed` / `requires_approval` / `blocked`
   with reason codes and a policy version, failing closed.
6. `integrations` — Protocols, the simulation/production gate, and the fixture-backed simulator.
7. `detectors` — the 13 baseline detectors behind one Protocol.
8. `decision_services` + `dispatch` — deterministic classification, banding, blast radius,
   scheduling; OR-Tools for the assignment problem with a documented fallback.
9. `graph/` — nodes, subgraphs, routing, the interrupt points.
10. `persistence` — checkpointer factory, outbox, migrations.
11. `api` — FastAPI surface and webhooks.
12. `observability` — structured logging, trace attributes, KPI counters.
13. `tests` — unit, integration, contract, scenario.
14. `docs` + diagrams + `examples/` demo.

---

## 4. Decisions worth writing down

**D1 — `thread_id = incident_id`, one clock.** The LangGraph thread id *is* the incident id, so
resumption is not a lookup problem. The SLA clock is stored once, at intake, as
`sla_clock_started_at`; every deadline is derived from it. No node may write it a second time — the
state reducer for that field rejects a second value rather than trusting callers to be careful. A
re-open creates a *linked* incident, never a reset clock.

**D2 — Predictive scans are a batch job, not an incident thread.** The 07:00 / 21:00 sweeps run
through their own entry point with their own state and only *create* incidents when a verdict
crosses the dispatch band. Modelling a scan as an incident thread would put tens of thousands of
threads through the approval machinery for no reason.

**D3 — Approval interrupts are separated from non-idempotent writes.** The gate node asks and
returns; a *separate downstream* node performs the write, carrying an idempotency key derived from
`(incident_id, action_type, target, attempt)`. This follows directly from the measured replay
semantics: LangGraph re-runs a node from its start on resume, so anything before an `interrupt()` in
the same node happens twice.

**D4 — Every production action is typed and carries six fields.** Incident id, idempotency key,
actor, reason code, approval ref, correlation id. This is enforced by the `ActionRequest` model, so
an adapter cannot be written that forgets one.

**D5 — HFC and PON differ only in the delimiter and the optics.** Rather than two parallel code
paths, access technology is a field and the *delimiter* (tap for HFC, ODP for PON) is resolved by
one function. Blast-radius arithmetic is then shared. Defaults for tap and ODP sizes are
configurable, not literals scattered through the detectors.

**D6 — Deterministic verdicts, model-written prose.** The Wi-Fi narrative model is handed a schema
with **no** `verdict` and **no** `wifi_health_score` field; the deterministic scorer's values are
merged into the payload after the model returns. A schema violation triggers one re-ask with the
validation error attached, then a hard fallback to a templated narrative. The band thresholds live
in the policy pack, so changing them is a config change with a version number.

**D7 — The fake model needs no API key.** Tests run against a deterministic fake that hashes its
prompt to pick a canned response, so the suite is offline and reproducible. The Anthropic-compatible
implementation sits behind the same Protocol and is exercised only when a key is present.

---

## 5. Status

"Done" below means the code exists **and** something ran against it, not that it was written.
`ruff check src tests` and `mypy --strict` are clean over 64 source files as of this row set, and
`pytest` collects and passes 65 tests.

| Area | State | How it was checked |
| --- | --- | --- |
| API verification (§2) | done | probe graph, output quoted in §2 |
| Project scaffold, pyproject, Makefile, README | done | `pip install -e ".[dev]"` succeeds |
| `config` (settings, clock, scan windows) | done | write-permission matrix, 4/4 combinations |
| `domain` (34 required models + 6 supporting) | done | 40 exports import; model set parsed from the specification and compared |
| `graph/state.py` contract + reducers | done | imports; reducer behaviour not yet exercised by the graph |
| `security` (redaction, injection, RBAC) | done | nested-dict masking with a verified positive control |
| `observability` (logging, tracing, KPIs) | done | 17 trace attributes; 26/28 KPI members derived, 2 declared non-derivable |
| `integrations` Protocols + `WriteGate` | done | `isinstance` **and** parameter-name match against the Protocols |
| ten fixture-backed simulators + 41-service network | done | 78-assertion smoke run; 21 HFC / 20 PON topologies resolve |
| `policies` engine + pack | done | 175-assertion scratch run: fail-closed on a missing pack, every threshold read from YAML |
| `detectors` (13) | done | 65 committed tests; fire/clean sweep over all 41 services; 4 defects found by execution, each now a named regression test |
| `decision_services`, `dispatch` | **pending** | — |
| `graph` parent + subgraphs + interrupts | **pending** | — |
| `persistence` + migrations | **pending** | — |
| `api` | **pending** | — |
| model provider + deterministic fake | **pending** | — |
| tests | detectors only | `tests/unit/test_detectors.py`, 65 passing; every other row above still rests on a scratch script |
| docs + diagrams | 1 of 9 | `docs/vendor-integration-gaps.md` only |
| demo | **pending** | — |

## 6. Known gaps

A gap named here is a gap acknowledged. An empty section at the end of a pass this large would be the
least believable part of the document.

1. **The committed test suite covers the detectors and nothing else.** `tests/unit/test_detectors.py`
   is real; every other row in §5 still rests on a throwaway script run outside the repository, which
   is enough to know that code works and not enough to know it keeps working. The policy pack's
   175 assertions are the largest such debt. The coverage gate is unmet.

   Each of the four detector regression tests was checked by reinstating the defect it names and
   confirming the suite goes red — and that check earned its keep immediately. The test written for
   the non-accumulating classifier pass **passed with the defect reinstated**: it asserted
   `physical_evidence > 0` on the first service with a localised delimiter fault, and that service
   already carried 1.54 of physical evidence from the telemetry detectors. The localiser's
   contribution is only observable on the 17 services where it is the *sole* source, so the
   invariant had to become the exact sum rather than its sign. A regression test never seen to fail
   is a regression test that has not been tested, and the rest of this suite should be held to the
   same standard rather than to a passing run.
2. **The specification's model list has 34 entries, not 33.** Counted from its own bullet list. Two
   earlier docstrings in this repository said 33 and were wrong; the count is now asserted by a test
   that parses the specification rather than restated in prose.
3. **`simulated: True` is returned even when the gate permits the write.** This is deliberate — a
   fixture-backed adapter never opens a TR-069 session, and claiming otherwise would be the defect —
   but it means "did this write really happen?" must be read from `result["gate"]["permitted"]`, not
   from `result["simulated"]`. A real adapter would return `simulated: False`.
4. **`stream_events(version="v3")` is unverified.** See the end of §2. Nothing depends on it.
5. **Postgres is untested against a live server.** The lazy-import path is exercised; `setup()` and
   the resume-after-restart scenario are not, and will be marked `@pytest.mark.postgres`.
6. **Vendor field names are invented.** 47 of them, listed in `docs/vendor-integration-gaps.md`.
   That file is the falsifiable part of assumption A1.
