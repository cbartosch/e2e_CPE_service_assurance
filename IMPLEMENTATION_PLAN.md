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
- **Resuming an interrupt raised inside a subgraph re-applies the parent's already-committed
  write.** Measured on 2026-08-15 with a two-node parent (`upstream → gate`) whose fields carry
  different reducers. At the pause, `upstream`'s write is committed once. After
  `Command(resume=...)`, an `operator.add` field holds **2** while `upstream` has been invoked
  **once** — the node did not re-run; its recorded write was replayed. The interrupted node itself
  runs twice, as expected.

  | Arrangement | `upstream` invocations | `operator.add` field after resume |
  | --- | --- | --- |
  | gate inside a subgraph | 1 | **2** — write replayed |
  | same graph, gate flat in the parent | 1 | 1 — no replay |

  The asymmetry is the load-bearing part. Remembering this as "LangGraph replays parent writes"
  would be false and would justify defensive code in the flat case where none is needed. **This is
  why `graph/state.py` uses de-duplicating and absolute-valued reducers rather than the obvious
  `operator.add`** — the reducer cannot tell where the interrupt was raised, so it must be immune
  either way. All seven are driven through a real replaying graph in
  `tests/unit/test_langgraph_replay_contract.py`, with `operator.add` alongside them as a positive
  control: if a future LangGraph stopped replaying, the control fails and the reducer assertions
  stop passing for the wrong reason.

  One consequence was already latent in the code and is now pinned by a test:
  `lifecycle.can_transition` special-cases `current is requested` as always legal. `DIAGNOSING →
  DIAGNOSING` is absent from the transition table, so without that case a replayed status write
  would raise and **every** gate downstream of a status write would be fatal.
- **A paused subgraph's writes are not visible in the parent's state.** Measured on 2026-08-15 with
  the two-node approval gate nested one level and the incident paused at the interrupt:

  | Read | `status` | `pending_approval` |
  | --- | --- | --- |
  | `(await app.aget_state(config)).values` | `dispatch_planning` | `None` |
  | `.tasks[0].state.values` via `subgraphs=True` | `awaiting_approval` | set |

  A subgraph's writes reach the parent when the subgraph node *completes*, and a paused one has not.
  So for exactly as long as a human is being waited on, the parent understates what is happening.
  This matters because the obvious implementation of the specification's state-inspection endpoint
  is `(await app.aget_state(config)).values`, and that implementation would report an incident as
  `dispatch_planning` while it had been sitting on someone's approval queue since Tuesday — the most
  misleading answer this system could give. It is a property of nesting, and **four of the six
  approval gates are nested** — the ones a subgraph owns. The two D06 and D07 ask are flat in the
  parent (`graph/nodes/governance.py`, added 2026-08-18), because those decisions are the parent's:
  the router that reads the answer is a conditional edge on a parent node, so there is no subgraph
  to resume and nesting the question would only put it a level below its consumer. Their
  `pending_approval` is therefore on the parent's own state and the naive read finds it — which is
  the trap in the other direction, since code that reaches straight for `.tasks[0].state.values`
  finds nothing at all for those two.

  `graph/inspect.py` is the response: every reader there takes the compiled app rather than a state
  mapping, because the information is not in the mapping. `effective_state` merges parent-first so
  the paused child's newer values win. The test asserts the naive read is *wrong* as well as the
  corrected read being right, so that a future LangGraph which propagated eagerly would not leave
  `inspect` looking necessary after it had become redundant.
- `from langgraph.checkpoint.memory import InMemorySaver`.
- `from langgraph.checkpoint.postgres import PostgresSaver` and
  `from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver`, `await saver.setup()`.

**Finding that changed the design:** importing `langgraph.checkpoint.postgres` raises
`ImportError: no pq wrapper available` unless `libpq` is present — the bare `psycopg` wheel is not
enough, `psycopg[binary]` is. A top-level import would therefore make the in-memory path
un-runnable on any machine without Postgres client libraries, including CI. **The Postgres
checkpointer is imported lazily inside the function**, and the in-memory path never touches it.

**Finding that changed the design, and a defect it exposed:** `AsyncPostgresSaver.from_conn_string`
is an `@asynccontextmanager` — it does not return a saver. Measured on
langgraph-checkpoint-postgres **3.1.2**, with the optional extra installed on 2026-08-15:

| Expression | Result |
| --- | --- |
| `type(AsyncPostgresSaver.from_conn_string(dsn))` | `contextlib._AsyncGeneratorContextManager` |
| `isinstance(that, AsyncPostgresSaver)` | `False` |
| `isinstance(that, BaseCheckpointSaver)` | `False` |
| `AsyncPostgresSaver.__init__` | `(conn, pipe=None, serde=None)` — wants a live connection |
| `AsyncPostgresSaver.setup` | coroutine; version-tracked DDL; "MUST be called … the first time" |

The version committed in `a38fdd1` was a **synchronous** `build_checkpointer()` that returned that
unentered helper with `# type: ignore[return-value]` on it. It could never have worked: the
connection is opened on `__aenter__`, so `StateGraph.compile(checkpointer=…)` would have been handed
an object with no `aget_tuple`, and every incident in production would have failed to checkpoint
while the whole suite stayed green on the in-memory branch. It is now
`persistence.checkpointer.checkpointer_scope`, an async context manager, and the lifespan owner
holds the connection.

Three things let a non-working branch ship, and each is worth naming:

1. **The only test of the selector asserted the class of the branch that worked**
   (`isinstance(build_checkpointer(Settings(postgres_dsn="")), InMemorySaver)`). The broken branch
   needed a database, so it was never exercised. It is now exercised without one, by injecting a
   stand-in under the name the deferred import uses and asserting the *sequence* — open, setup, hand
   over the entered saver, close. Reinstating the shipped defect fails those two tests with
   "the scope handed back the connection helper instead of the saver inside it".
2. **`ignore_missing_imports` turned the library into `Any`.** The extra is optional and was not
   installed, so `AsyncPostgresSaver` had no type and mypy could not see the mismatch. The extra is
   now installed locally; strict mode is clean **both** with it and with
   `follow_imports = skip` forced on `langgraph.checkpoint.postgres.*`, which is how a machine
   without it resolves. A green that depends on which optional packages happen to be present is not
   a green.
3. **A `type: ignore` was treated as noise rather than as a claim.** mypy reported
   `Unused "type: ignore" comment` *and* `Returning Any …` on that very line, i.e. the suppression
   named a different error code from the one being emitted. A suppression whose code does not match
   the reported error is pointed at a bug the author has not identified. `unused-ignore` is the only
   signal a checker can give for that, and the run in which it appeared was reported as clean.

**Finding that changed the design:** the checkpoint serialiser **degrades unknown types silently**.
`JsonPlusSerializer` takes an `allowed_msgpack_modules` allowlist, and anything outside it is
restored as a plain container rather than rejected. Measured on 2026-08-15 across a real pause and
resume: an `ApprovalRequest` came back a `dict`, an `IncidentStatus` came back a `str`, and nothing
raised — the graph resumed and kept running on values whose methods no longer existed.

The trap is that the allowlist accepts `(module, name)` tuples as well as classes, so `["lpr_cpe"]`
*looks* like "trust everything under lpr_cpe". It matches nothing. `persistence/serde.py` therefore
passes **classes**, derived from `domain.__all__` rather than listed by hand, so a new model cannot
be forgotten. Both backends are built from the same `build_serde()`; a laxer serde in the in-memory
saver would hide exactly the bugs that only appear against Postgres.

Because a permissive default satisfies "the value survived" without the allowlist doing any work,
`tests/unit/test_persistence.py` pairs the claim with a **control that fails**: three plausibly-wrong
allowlists (empty, unrelated types, the package-name form above), each asserted to degrade every
field *without raising*. A green run of the real test then means the allowlist worked, rather than
that nothing was ever at risk.

**`Command(resume={})` is a silent no-op, and the API must never send one.** Found on 2026-08-16
while testing the self-help customer-response interrupt. `langgraph/pregel/_loop.py` decides whether
a resume value is a map of interrupt-id → value with `isinstance(resume, dict) and
all(is_xxh3_128_hexdigest(k) for k in resume)` — and `all()` over an empty dict is `True`. So `{}` is
read as *a map that resumes nothing*: the pending interrupt is left unsatisfied and the graph
re-pauses having executed no node.

The danger is that it fails clean. An endpoint that forwarded an empty webhook body would return
`200`, leave the incident exactly where it was, and write **no audit event to say so** — there is no
exception and no state change to notice. `{"source": "scheduler_tick"}` and `""` both reach the node
normally; only the empty mapping is swallowed. Pinned by
`test_an_empty_resume_map_never_reaches_the_node`, which asserts the graph re-pauses at
`await_customer_response` with the interrupt still pending *and* that the node left no audit trail —
so the test tells "the resume was dropped before delivery" apart from "delivered and found
unusable". Whatever validates resume payloads at the API boundary has to reject `{}` explicitly.

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
semantics in §2, which bite in two distinct places:

- *Inside* the interrupted node, everything before `interrupt()` executes on both passes. So a gate
  must not perform the action it is asking about.
- *Upstream* of it, a committed write is re-applied when the gate is nested — and every gate here
  is nested. So no node anywhere may express state as an increment.

The second is the one that would have been missed by reasoning alone; it was found by probe.

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

**D7 — The fake model needs no API key.** The fake is to hash its prompt to pick a canned response,
so that the suite stays offline and reproducible, with the Anthropic-compatible implementation
behind the same Protocol and exercised only when a key is present. Stated in the future tense
because **neither exists yet** — §5 carries `model provider + deterministic fake` as pending, and
the suite is offline today for the simpler reason that nothing in `src/` calls a model at all. The
decision is still worth recording here: it is what the Protocol has to be shaped for.

---

## 5. Status

"Done" below means the code exists **and** something ran against it, not that it was written.
Measured on **2026-08-18**, after D06's and D07's governance nodes landed: `ruff check src tests`
passes, `ruff format --check src tests` passes at **124 files already formatted**, `mypy --strict
src/lpr_cpe` reports no issues in **103** source files, and `pytest` collects and passes **853**
tests at **82.77%** line coverage. `make lint` therefore passes, which it did not on the 17th.

The previous revision of this paragraph was dated 2026-08-17 and read 122 / 102 / 839 / 82.65%.
Every one of those four had moved by the next morning, from a change that touched five source files
and one test module. That is the point the paragraph below about staleness is making, so the old
figures are left here rather than overwritten: the interval over which a count in this section stays
true is a **day**, not a release.

That format failure is worth keeping a line for rather than deleting, because of what it turned out
to be. Under ruff 0.16.3 five committed files were reported as **would be reformatted** —
`graph/subgraphs/field_planning.py`, `graph/subgraphs/preventive_maintenance.py`,
`tests/unit/test_builder.py`, `tests/unit/test_subgraph_preventive_maintenance.py` and
`tests/unit/test_subgraph_self_help.py`. The question that mattered was whether the pin had drifted,
because that decides between "reformat the code" and "pin ruff": `pyproject.toml` asks for
`ruff>=0.9`, an open upper bound, so a formatter change between 0.9 and 0.16.3 would have made the
five files a version artefact rather than a defect. Measured across the intervening releases the
formatting of those five is stable, so it was the code, and reformatting was the fix.

Run twice, in `.venv` and in the system interpreter, because they are **not the same environment**
and it is easy to check the wrong one: `make` uses `.venv` — which is where §2's version table was
resolved and the only one with the `optimizer` extra installed — while a bare `python -m pytest`
from the repository root uses the system interpreter, where `ortools` is absent and `pydantic`,
`fastapi` and `psycopg` are each a patch behind. Every number above is identical in both — the
format failure was, while it lasted, and the 124 files and 103 source files are — so nothing here
depends on which is used; §2's versions do, and it says so.

The previous revision of this section claimed the same for 86 files, and it was **wrong**: mypy was
reporting two errors in `persistence/checkpointer.py` at the moment `a38fdd1` was committed, and
they were the visible end of a Postgres branch that could not work (§2). The claim was made from a
run that was not re-read after the last edit. Every number in this section is now taken from a
command run *after* the final state of the tree, which is the only version of the claim worth
making.

That discipline is necessary and it is **not sufficient**, which the same numbers went on to
demonstrate — twice now. 98 and 758 were both correct when written and were both stale within the
day; 102, 122, 839 and 82.65% replaced them under exactly that discipline, were taken from commands
run after the final state of the tree, and were stale by the following morning all the same. The
tree moved, and nothing re-read them, because no gate in this repository can. A number in prose has
no expiry and nothing watches it, so treat every count in this section as a measurement dated
2026-08-18 rather than as a fact, and re-run the four commands before quoting one. Gap 7 in §6 is
the general form of this.

Where a row says *mutation-checked*, it means every regression assertion was verified by reinstating
the defect it names and watching that test — not merely some test — fail. A green suite is not
evidence that its assertions are load-bearing, and every sweep so far has found at least one that
was not: two of the policy assertions were tautologies, and the graph-foundations sweep surfaced two
more. Both of the latter are worth recording, because neither was visible by reading the tests.

- The total-step bound was driven to **65** against a limit of 60. That fires under `>=` and under
  `>` alike, so the off-by-one the test was named for passed straight through. Every bound is now
  checked at `limit - 1` and at `limit`, parametrised over `list(BudgetKind)` so a new bound cannot
  quietly skip the boundary question.
- Nothing read the **interrupt payload**. That the RBAC table permits a *set* of roles was asserted,
  but not that the set reaches the operator — so narrowing `permitted_roles` back to the pack's
  single `required_role` passed every test in the file, while an operator UI built on that payload
  would have told a supervisor they could not answer a question they could.

Tightening the first of these created the second-order version of the same problem, which is worth
naming: at a boundary `observed == limit`, so an assertion that the reason "contains the limit" was
satisfied by the observed count even after the limit was dropped from the message. That is now a
separate test built from a verdict whose three numbers are deliberately distinct.

The routing sweep (48 mutations, 42 caught on the first pass) continued the pattern. Six survived
and all six were informative:

- **Four were genuine gaps.** D05 re-deriving `completeness_score >= 0.5` instead of asking
  `sufficient_for_action` was invisible because no test held an assessment that was *complete and
  contradictory*. D11's policy filter could be deleted whole, because the only state reaching
  `self_help` carried no policy decision to filter. D19's `max(..., key=updated_at)` could be
  replaced by the dict head, because every multi-record state held revisions of *one* MR, which
  `latest_by_id` collapses to a single entry. And `latest_decision_of` could take the list tail
  instead of the newest timestamp, because no state had write order and decision order disagreeing —
  which matters because it is read immediately after `approval_outstanding`, which *does* order by
  timestamp, so the pair could have closed a gate on one decision and acted on another. Each now has
  a named test, and each of those tests was watched to fail against its mutant.
- **One was dead code.** D17 checked `is_plant_side(finding.fault_domain)` before handing over.
  Removing it killed nothing, and the reason is that `FieldFinding` refuses to construct with
  `requires_plant_work=True` and a premises-side domain — so the clause was unreachable by
  construction. It was removed rather than propped up with a test that would have had to build an
  object the model forbids. A branch no state can enter is a branch no test can hold to account.
- **One was a provably equivalent mutant.** D20's `status is not PASSED` in place of
  `status is FAILED` cannot differ, because `latest_conclusive_test` only ever returns results whose
  status is one of those two. The equivalence rests on `TestResult.conclusive`, and that is pinned
  by the `UNAVAILABLE` case in `test_a_test_that_could_not_run_does_not_send_a_second_truck`:
  widening `conclusive` breaks a test rather than silently changing what D20 means.

The re-run after those changes caught 46 of 46.

The self-help sweep (13 mutations) found something different: not a weak test, but **a defect the
whole existing suite was structurally unable to see**. Every KPI in both resolution subgraphs was
emitted with `emit_kpi(state, ...)` — the node's *input* state. LangGraph reduces a node's update
only after the node returns, so a KPI derived from a fact the node is itself writing measured the
world as it was on the way in: `policy_block_rate` counted an empty `policy_decisions`,
`automation_coverage_rate` an empty `action_history`, `self_help_success_rate` a session still
reading `in_progress`. `emit_kpi` swallows `KPINotDerivableError` by design — a KPI state cannot yet
support is normal at a stage boundary, not a fault — so all five call sites failed **perfectly
silently**: no exception, no event, and a green suite. The fix is `preview(state, update)`, which
applies the declared reducers; a plain `{**state, **update}` would have been wrong too, because
`metrics_timestamps` reduces with `merge_dict`. Two further defects surfaced from the same reading:
`customer_communications` had **no writer anywhere in `src`** while
`customer_contacts_per_incident` counts exactly that list and never returns `None` — so an incident
that had just texted its customer reported zero contacts, confidently — and
`self_help_success_rate` was emitted only from `verify_self_help`, which only a compliant customer
can reach, so every decline and every silence dropped out of the denominator and the rate would
have climbed each time a customer refused.

None of this was caught by a test because no test asserted on `kpi_events` at all in either
subgraph module. That is the general lesson and it is worth stating plainly: **a swallowed exception
converts a missing measurement into a silent one, and only a test that names the measurement can
tell the two apart.** Both modules now assert on the KPIs they emit, and reverting each `preview`
call turns one of those assertions red.

Writing those assertions produced the sweep's second finding, in the new tests rather than the
source. `KPIEvent.kpi_name` was declared `str` at the time, so pydantic coerced the `KPIName` member
down to a plain `str` and `e.kpi_name is KPIName.X` was **never true** — every such filter matched
nothing. The two *presence* assertions failed loudly and named the mistake; the *absence* assertion
sitting beside them passed, and would have gone on passing forever while proving nothing. Two things
changed in response. The field is now declared `KPIName` — the coercion was removed in `0f59e4b`,
and `KPIEvent.model_fields["kpi_name"].annotation` is `<enum 'KPIName'>` today — and the absence
assertion now runs a positive control through the same comparison first, so a broken filter fails
the test that depends on it. The declaration is the fix; the positive control is what stops the next
silent filter, whatever causes it.

| Area | State | How it was checked |
| --- | --- | --- |
| API verification (§2) | done | probe graph, output quoted in §2 |
| Project scaffold, pyproject, Makefile, README | done | every path and command the two files assert, checked against the tree; `[project.scripts]` resolved by importing what it names. The old check here was `pip install -e ".[dev]"` succeeds, which passed throughout and could not have caught any of it — see gap 7 |
| `config` (settings, clock, scan windows) | done | write-permission matrix, 4/4 combinations |
| `domain` (34 required models + 6 supporting) | done | 40 exports import; model set parsed from the specification and compared |
| `domain/boundaries.py` — the Clean/Dirty crew split | done | 52 committed tests, mutation-checked 9/9; the expected-crew table is written out by hand rather than derived from the sets it checks |
| `graph/state.py` contract + reducers | done | all 7 reducers driven through a real replaying LangGraph, 8 tests, mutation-checked 8/8 |
| `security` (redaction, injection, RBAC) | done | nested-dict masking with a verified positive control |
| `observability` (logging, tracing, KPIs) | done | 17 trace attributes; 26/28 KPI members derived, 2 declared non-derivable |
| `integrations` Protocols + `WriteGate` | done | `isinstance` **and** parameter-name match against the Protocols |
| ten fixture-backed simulators + 41-service network | done | 78-assertion smoke run; 21 HFC / 20 PON topologies resolve |
| `policies` engine + pack | done | 139 committed tests, mutation-checked 24/24; 2 defects found by execution, both now named regression tests |
| `detectors` (13) | done | 65 committed tests; fire/clean sweep over all 41 services; 4 defects found by execution, each now a named regression test |
| `decision_services` | done | 79 committed tests |
| `dispatch` (greedy + the seam a solver would fill) | done | 55 committed tests. **No CP-SAT implementation exists** — checked by running it in `.venv`, where `ortools` 9.15.6755 *is* installed: `select_optimizer` returns `GreedyDispatchOptimizer` for `prefer_solver` both True and False, so the `optimizer` extra changes nothing even when present (gap DISPATCH-1) |
| LangGraph replay semantics (§2) | done | 8 committed tests with a positive control, mutation-checked 8/8 |
| `graph` context, loop guard, approval gates, paused-state reads | done | 29 committed tests, mutation-checked 42/42; 2 of the 42 survived the first sweep and both were real gaps, now closed |
| `graph/routing.py` — the 24 decision points | done | 233 committed tests, mutation-checked 46/46; 6 of the first sweep's 48 survived — 4 real gaps, 1 dead branch since removed, 1 provably equivalent; every question string is parsed out of `docs/specification.md` rather than copied, and each router's `Literal` return type is compared against its declared `branches` |
| `graph/nodes/` P01–P11 + `graph/builder.py` — the parent to the resolution fork | done | 8 + 20 committed tests; a registry guard asserts every declared node is wired; the offline-CPE blocking-flag defect (CPE-7) was found by running it |
| `graph/subgraphs/remote_resolution.py` — P12, D10, the high-risk approval interrupt | done | 21 committed tests; mutation-checked 9/9 in the original sweep and 2/2 again when the KPI assertions were added; driven through the real parent graph rather than a hand-built plan |
| `graph/subgraphs/self_help.py` — P13 and D12, the customer-response interrupt | done | 25 committed tests, mutation-checked 11/11; three KPI defects found by execution (below) |
| `graph/subgraphs/field_planning.py` — P14, D13, P15, D14, D15, P16 | done | 20 committed tests; the joint incident driven from the real parent, which since the fork was wired runs into this subgraph by itself — the fixture passes `interrupt_after=["generate_resolution_options"]` to stop it. `is_field_option` was measured wrong for 16 of 50 arrivals and `is_dispatchable_option` written to narrow it |
| `graph/subgraphs/field_execution.py` — P17, D16, D17, P18, D18, P19, P20 | done | 13 committed tests, mutation-checked 10/10. Driven from the real parent through `interrupt_after=["field_planning"]`, so the arriving state is one the graph actually produces. Two constraints were found by execution rather than reasoned about: the handover-gate router is wired on **two** edges and a state where both answer alike cannot tell a mis-wire from a correct wiring, so that test drives a discriminating pair; and `_Ticking` advances on every read of `local_time`, so a context shared across drives makes a later verdict depend on how many nodes an earlier drive ran — each drive builds its own. The stage's headline finding is that **the handover chain is entered and stalls at D18**: a contract is incomplete until something is ruled out, no first-cycle RCA rejects a hypothesis, so D18 onwards is unreachable end to end and the tests that cover it seed a rejection to get there. That is gap EXEC-1 in `docs/vendor-integration-gaps.md`, and a test asserts the emptiness rather than leaving it implied. **This row previously said no fixture reaches the chain at all, and that was a defect in the sweep, not a finding.** The old harness answered two of the five pause types and handed an approval payload to `field_submission_request`, which `field_submission` rejects — so no crew ever reported and `determine_delimiter` had no finding to act on. Re-swept on 2026-08-22 answering all five, and counting the services that enter each node: `open_field_visit` **32**, and on a HANDOVER submission `determine_delimiter`, `request_additional_field_tests`, `evaluate_handover_policy` and `build_handover_contract` **20 each**. On a PREMISES submission `close_clean_boots_visit` is **20**. Only `prepare_handover_approval`, `request_handover_approval`, `file_plant_mr` and `abandon_handover` are 0 under both. So EXEC-1 is open exactly where `docs/vendor-integration-gaps.md` already said it was from a single-service drive — D18 rejects, and the three nodes past it are never entered — and this row was the one that disagreed. Twenty runs reach `plant_execution` and ten reach `plant_referral`, and both counts are identical under the two submissions. `plant_execution` has two entrances from elsewhere — `SUBGRAPH_SUCCESSOR` from `plant_referral`, and D16's `delimit` arm — plus D19's `await_plant` back into itself. Ten of the twenty arrive through `plant_referral`; how the other ten arrive is **not** measured, and this row asserts nothing about it, because the obvious answer cannot be right as stated: a PREMISES crew never delimits and `determine_delimiter` is 0 in that sweep, yet the count is 20 there too |
| `graph/subgraphs/plant_referral.py` — P19 and P20 on D08's direct arm | done | 11 committed tests. The arrival is the D08 fork itself, produced by running the parent with `interrupt_after=["generate_resolution_options"]` rather than assembled by hand. It landed together with its own wiring in `e78421e`, and the guard was watched red first: with `BRANCH_TARGETS["D08"]["plant_path"]` still pointing at `END`, the test that asserts a plant arrival fails |
| `graph/subgraphs/plant_execution.py` — P20's update instruction and P21, with D19 and D20 after it | done | 12 committed tests, driven on from `interrupt_after=["field_planning"]`, the same parent seam field execution's tests use. D19's `await_plant` answer targets this subgraph, so along with `restoration_validation` it is one of the two a decision of its own can re-enter, and that shows up in the sweep: on D08's direct arm `search_plant_mr` reaches seven visits against a re-entry limit of six. **This row used to add "which is what stops those ten runs", and that half came from the harness that never answered the OSP interrupt.** Re-measured on 2026-08-22 over exactly the ten services that enter `plant_referral`, the seven-visit peak stops **two** of them — `SVC-PO-042-A-04` and `SVC-UT-001-A-03`. Six sit at five visits and stop on `resolution_cycles` instead, and the last two, `SVC-VQ-002-B-01` and `-B-02`, sit at three, get all the way to `reconcile_linked_systems` and escalate there on a jTrack mismatch. So the re-entry ceiling is one of three things that end this arm, and the least common of them |
| `graph/subgraphs/restoration_validation.py` — P22 and D21 | done | 9 committed tests, arriving through `interrupt_after=["generate_resolution_options"]`. It is the most-fed stage in the graph — four arms reach it, D16 `validate`, D20 `verify`, D10 `verify` and D12 `verify` — and D21's `continue_observation` loops it back onto itself |
| `graph/subgraphs/reconciliation_closure.py` — P24, P25 and P26, with D23 and D24 inside it | done | 11 committed tests, arriving through `interrupt_after=["generate_resolution_options"]`. D23 and D24 are wired on this subgraph's own `add_conditional_edges` and so appear nowhere in `BRANCH_TARGETS` — two of the seven decisions a count taken from that table alone misses. It is a terminal node by design and is declared in `_DELIBERATE_TERMINALS` |
| `graph/subgraphs/preventive_maintenance.py` — D04's preventive arm | done | 27 committed tests, more than any other subgraph, arriving through `interrupt_after=["assess_impact_and_priority"]` — D04's own source node. The subgraph is done and the edge out of it is not: its exit is the single remaining entry in `PENDING_STAGES`, and no fixture reaches it at all |
| `graph/nodes/governance.py` — D06's review gate, D07's blast-radius gate and escalation | done | 12 committed tests, mutation-checked 4/5. Three specification nodes, **five** written, because `interrupts.py` makes every gate a `prepare`/`request` pair: a single node cannot both record that it is waiting and wait, since the interrupt aborts it before its update is checkpointed. Driven from the real parent graph, and getting there is the finding worth keeping. **D07's gate is never reached on its own. D06's is, and this row used to say otherwise.** The claim came from replacing both routers with recording proxies and driving all 41 services under both case types: 134 invocations, 67 at D06 and 67 at D07, every one answering `continue`. Re-measured on 2026-08-22 by counting entries into the arm targets themselves, under a harness that answers all five pause types, half of that does not hold. `D06.approve_low_confidence` is the only arm in `BRANCH_TARGETS` pointing at `prepare_low_confidence_review`, and **9 of the 41 services enter it**, once each, the same 9 under both crew answers — so D06 does take its review arm unaided. `prepare_blast_radius_approval`, `request_blast_radius_approval` and `record_escalation` are **0** in both sweeps, so it is D07 whose arms are live and unreached, and `record_escalation` sitting at 0 is the expected consequence of its being D07's `escalate` arm and nothing else's rather than a universal sink. The two instruments count different things — router invocations against node entries — so the 134 is not refuted, only the "every one answers `continue`" drawn from it. That figure has since been re-derived rather than left outstanding, by wrapping `DECISIONS[...].route` itself: D06 is asked **138** times under a handover answer and **238** under a premises one, D07 **129** and **229**, and each count closes against node entries rather than standing alone — D06 is asked after `determine_root_cause` (129) and after `request_low_confidence_review` (9), which is the 138. All nine take the arm on the **second** ask, through `approval_outstanding` and not through `rca is None`, which fires at none of D06's 376 asks because P10 always produces one. `graph/nodes/governance.py` owns those figures now and this row cites rather than repeats them. What still holds for D07 is the live-but-unreached distinction the probe was built to draw: at all **358** of its asks there is neither an outstanding demand of its kind nor an answer of one, which by `approval_outstanding` means the corpus never produced a `HIGH_BLAST_RADIUS_ACTION` decision at all. The tests therefore seed a `PolicyDecision` through `aupdate_state` at an `interrupt_after` seam, which turned out to **re-evaluate the outgoing branch**: paused after `determine_root_cause` with `next == ('generate_resolution_options',)`, appending a demand moves `next` to `('prepare_low_confidence_review',)` with no node having run. Both new arms are cycles back into their own `prepare`, and both close on `approval_outstanding` comparing `max(answers)` against `max(demands)` rather than on a counter. The fifth mutation **disproved a claim this document made**: reordering `route_rca_confidence`'s clauses was written up as leaving a graph that never halts, and reinstating it turned exactly one test red and no graph run at all, because P10 produces an RCA on every lap. The comment now records the measurement instead of the reasoning |
| `graph` parent past P11 — Stage 5, D08's plant path, Stage 4's tail | **in progress** | **All three of the subgraph-shaped gaps this row named are closed and the one edge is not**, which is why the state is unchanged while every figure in it has moved. Re-measured 2026-08-21: `SUBGRAPH_NODES` holds **nine** entries rather than five — `plant_referral`, `plant_execution`, `restoration_validation` and `reconciliation_closure` have landed — so the parent is **26 nodes, 17 steps and 9 subgraphs**, and **24 of 24 decisions are wired**, 17 on a parent edge and 7 inside a subgraph. Stage 5 is spread across three of those: P22 and D21 in `restoration_validation`, P23 as the parent step `confirm_customer_outcome` with D22 after it, and P24-P26 with D23 and D24 inside `reconciliation_closure` — which is also why D23 and D24 are absent from `BRANCH_TARGETS` and wired regardless, and why a count taken from that table alone reads 17 and not 24. D08's plant path is `plant_referral`, and Stage 4's tail is `plant_execution`. What is left is **the one edge**: `PENDING_STAGES` holds a single entry, `__onward__:preventive_maintenance`, the preventive-to-field-planning seam, down from five. `SUBGRAPH_SUCCESSOR` has grown to two rows, and the new one — `plant_referral → plant_execution` — makes `plant_execution` the one stage with two entrances that two *different* tables hold: that row, and D16's `delimit` arm out of `field_execution`. Neither table shows the convergence, so only reading both back out of the same `StateGraph` does. Four earlier readings of this row were wrong and are corrected rather than deleted — **`dispatch` is not among the gaps** (its own row is done at 55 tests, and P15 and P16 are written inside field planning); **escalation was not missing as a capability**, `guards.escalation_update` exists and D02's and D05's `manual_review` arms already ran it, so what D07 lacked was that one arm — though `record_escalation` does **not** reuse that helper, which takes a `BudgetVerdict` and would have stamped `LOOP_LIMIT_REACHED` on a case that never looped; and the row counted D06's and D07's work as **three** nodes when the gate-pair invariant makes it five. The fourth is arithmetic in this row's own text: it called Stage 5 "nine specification nodes, six of them decisions", and the nine it listed hold **four**. Comms is the claim that survives scrutiny, narrowly, and it was re-checked here rather than carried over: `self_help` calls `send_self_help` and `fetch_customer_responses`, and `send_notification` still has **no caller anywhere in `src`** — only the Protocol at `integrations/base.py:369` and the simulator that implements it. The sentence that used to follow it is what changed. D08 no longer routes to `END`: `plant_path` reaches `plant_referral`, and both `verify` answers reach `restoration_validation`. `field_execution` has stopped being terminal — it carries a `DECISION_AFTER` entry, D16, whose two arms are `validate → restoration_validation` and `delimit → plant_execution` — so the three exits this row explained by three different blockers are no longer gaps the build reports, and the only *terminal* subgraphs left are `preventive_maintenance` and `reconciliation_closure`. Wired is not reached, and the field-execution row above now says which of those three exits any fixture visits: none of them. `PENDING_STAGES` names **1** remaining such exit, down from 5, and `_check_pending_stages` fails the build in both directions. It guards terminal *nodes* as well as branch answers, and `_DELIBERATE_TERMINALS` now holds two: `reconciliation_closure` joined `record_escalation` there by becoming a legitimate ending. Emptying that table fails the build with both, which leaves three terminal nodes in total and `preventive_maintenance` the only one that is a gap |
| `persistence` checkpointer + serde | done | 14 committed tests, each paired with a control that fails; lazy Postgres import checked in a clean subprocess; the Postgres branch driven without a database and the shipped defect reinstated to watch it fail |
| `persistence` outbox + migrations | **pending** | — |
| `api` | **pending** | `src/lpr_cpe/api/` does not exist. `make serve` names the gap and exits non-zero rather than importing it |
| model provider + deterministic fake | **pending** | no module in `src/` calls a model provider. `ModelProvider` is an enum in `config.settings` with nothing behind it, so the `anthropic` extra changes nothing and D7 above describes an intent, not a running path |
| `cli.py` + `[project.scripts]` | done | 6 committed tests, each watched red. The declaration shipped naming a module that was never written; the guard reads `[project.scripts]` out of `pyproject.toml` and imports what it names, so it covers a second entry point without being extended |
| tests | 916 passing | unit only; no integration, contract or scenario tests yet, and none of the 17 required scenarios exist |
| coverage | **85.29%**, gate is 85% | **the gate is met and `make test` is green**, measured 2026-08-21 by running that target's own command, which prints `Required test coverage of 85% reached. Total coverage: 85.29%` and exits 0. It was 82.77% and failing on 2026-08-18. Nothing was written to raise it: the four modules gap 1 names as the concentration are unchanged at the precision that gap states them, and the 2.52 points are `jtrack/simulator.py` going 33% to 72.28% as the plant stages drove it |
| docs + diagrams | 1 of 9 documents, **1 of 10 diagrams** | `docs/vendor-integration-gaps.md` is still the only one of the nine the specification asks for — eight `.md` files and `docs/architecture-decisions/` — and `docs/specification.md` is the vendored input, not a deliverable. Two documents written since are **not on that list** and do not count against it, `docs/workflow-diagram.md` and `docs/dashboard-architecture.md`. The first does close a diagram: its ten Mermaid figures are the parent graph and its nine subgraphs, which is the specification's item 3, *LangGraph parent graph and subgraphs*. The other nine diagrams and the other eight documents are unwritten |
| demo | **pending** | the seven scenarios are unwritten. `make demo` names the gap and exits non-zero rather than invoking a subcommand `cli.py` deliberately does not define |
| CI | **none exists** | no `.github/`, GitLab, Azure, CircleCI, tox, nox or pre-commit configuration is tracked. Every gate in this repository is manual |

## 6. Known gaps

A gap named here is a gap acknowledged. An empty section at the end of a pass this large would be the
least believable part of the document.

1. **The committed suite is unit-only, and the coverage gate is now met — which is the least
   reassuring thing this section can report.** Every row marked *done* in §5 rests on committed
   tests rather than on a throwaway script, but all **916** are unit tests. There are no
   integration, contract or scenario tests and none of the 17 required scenarios exist. Coverage is
   **85.29%** against the 85% bar, measured 2026-08-21, so `make test` and `make check` are green
   where two revisions of this gap said they failed.

   **No work was aimed at the 2.23-point shortfall.** Sixty-three tests were written in that time
   and every one of them was written for a stage, which the aggregate conceals as efficiently as it
   concealed the shortfall. The uncovered lines were never spread evenly, and the
   modules they concentrate in have not moved: `security/redaction.py` is at 17.81% and
   `security/injection.py` at 34.09% — the two whose job is keeping customer data out of logs and
   prompts — with `observability/tracing.py` at 24.60% and `logging.py` at 26.19%. Each is the
   figure this gap recorded on the 18th, at the precision it recorded it. The 2.52 points came
   from somewhere else, and where is the whole point: `jtrack/simulator.py` went from **33% to
   72.28%** because the plant stages file and read MRs. The bar was cleared by building stages,
   exactly as the paragraph below said it would be, and clearing it settled nothing about the four
   modules that prompted the concern.

   Two entries have left that list, and both left it the same way, which is the useful part. `wfm`
   was at 16% and is at **62%**: the field branch arriving, because a dispatch really does go
   through it and something now drives one. `jtrack/simulator.py` was at **25%** and is at **33%**,
   and those eight points are Stage 4 filing an MR — measured, not attributed, by re-running the
   suite with `--ignore=tests/unit/test_subgraph_field_execution.py`, which puts jtrack back at 25%
   and the total back to **79.72%** (measured 2026-08-17, against that day's 82.65% aggregate). So
   twelve tests are worth 2.93 points, which is the clearest thing this section can say about what
   the remaining shortfall costs: it is not 2.23 points of tidying, it is roughly one more stage
   driven end to end. The governance nodes that landed on the 18th are the counter-example that
   makes the same point from the other side — fourteen tests, 0.12 points — because they added a
   64-statement module rather than reaching into an untouched integration. Neither module moved
   because anybody set out to raise coverage, and that is the point. Raising the number is the wrong
   objective; those modules are the work, and a scenario test that ran one incident end to end would
   reach most of them at once — as writing the stage that files an MR just did, for jtrack, without
   trying to.

   That has since happened twice over, which is why the paragraph above can say the bar was cleared
   without anyone aiming at it. `jtrack/simulator.py` is at **72.28%** on 2026-08-21, a further 39
   points, because the plant stages file and read MRs on two more paths. `wfm` is unchanged at 62%.

   The two resolution subgraphs are driven through the real parent graph from intake onwards, so
   those paths are end-to-end in everything but name, and the field branch has joined them for two
   stages rather than one. D11's and D12's `field_planning` answers reach a written subgraph, the
   parent runs straight into it, and `SUBGRAPH_SUCCESSOR` now carries it on into field execution.
   That is why `test_subgraph_field_planning.py` stops the parent with
   `interrupt_after=["generate_resolution_options"]` and `test_subgraph_field_execution.py` stops it
   with `interrupt_after=["field_planning"]` — each at its own seam, each driving on from the state
   the parent actually produced rather than one assembled by hand.

   **One incident now runs from event to closure**, which is new on 2026-08-21 and replaces this
   paragraph's previous claim that none did. Sweeping all 41 fixture services through the real
   parent graph, `SVC-UT-001-B-01` reaches `closed` and the sweep raises no error anywhere. It gets
   there on the remote path rather than the field one, lapping `await_service_stability` four times.

   **The other forty all escalate, and the sweep that said why was itself broken.** This paragraph
   previously reported one split — 30 on `node_reentries`, 10 on `resolution_cycles` — and gave one
   cause for all forty. Both were artefacts of the harness. It answered two of the five pause types
   and handed an approval payload to the other three, so `field_submission` and `plant_report`
   rejected it as unusable and the crew was re-asked until a re-entry budget tripped; what was
   written down as a product defect was in part the sweep never answering. Re-swept on 2026-08-22
   answering all five, the result **depends on what the crew reports** and so is two sweeps:
   HANDOVER, a fault confirmed at the tap or ODP, and PREMISES, a fix made on site. Both close one
   and escalate 40. HANDOVER stops **22 on `node_reentries`, 16 on `resolution_cycles`**; PREMISES
   stops **2 and 36**. A **third** reason appears in both that the old table had no row for at all —
   two runs escalate on `reconciliation did not converge after 3 attempts`. Stage counts move too:
   `field_planning` and `field_execution` are 32 each rather than 30, `restoration_validation` is 9
   under HANDOVER and 29 under PREMISES rather than 1, and `reconciliation_closure` is **3** rather
   than 1. `plant_execution` 20, `plant_referral` 10, `remote_resolution` 2, `self_help` 1 and
   `preventive_maintenance` 0 are unchanged. The three causes are EXEC-1, the `Fixtures.telemetry`
   defect below, and EXEC-2 respectively; `docs/workflow-diagram.md` §6 owns both sweeps and is
   cited rather than copied.

   Three defects stood in the way, and they were in series — each invisible until the one above it
   was fixed. Derived detectors were counted into the restoration comparison, where they hold the
   after-peak up on their own. `_check_confidence` demanded an approval kind `route_closure_gate`
   does not own, so a validated incident was abandoned where an unvalidated one was asked about.
   And the closure stage's exit collapsed onto a `validating -> closed` hop that `STAGE_TRANSITIONS`
   had no entry for. All three are fixed and each is guarded by a test seen red.

   A defect that is *not* fixed: `Fixtures.telemetry` is keyed on a static `health` field, so no
   repair this workflow performs moves it. **Ten** sites in `src` read the field and the only
   assignment is `simulation/fixtures/network.py`, the builder that constructs the record — so
   nothing an adapter or a node runs ever changes it, which is the accurate form of the "no writer"
   this paragraph used to assert. It is one of the three causes behind the forty escalations, not
   the only one; it is the one that dominates PREMISES.

   **The closing run is not an instance of it, and this paragraph used to offer it as one.** The
   claim was that `SVC-UT-001-B-01` laps `diagnosing -> remote_resolution -> validating` because a
   completed repair changes nothing a later read sees. Driven again on 2026-08-23 through
   `test_builder.py`'s own `_walk`, that is the one service the gap does not reach:
   `SVC-UT-001-B-01` is `pon_healthy`, which is precisely the condition
   `integrations/cpe/simulator.py:154-157` requires before it recovers a device, and CPE-8 names it
   as the worked example. Every lap reads two findings before the repair and one after and clears
   76% of the anomaly against the pack's 70% bar; every refusal is `STABILITY_WINDOW_PENDING`, "2
   of the required 3 post-fix samples have arrived". What holds it open is `min_post_fix_samples:
   3` — set to 1, the same walk closes in one lap.

   **The retraction in the previous revision of this paragraph was itself wrong and is withdrawn.**
   It gave `resolution_cycles` **5** and `await_service_stability` **4** "against the **3** laps
   this paragraph claimed", which reads as a correction and is not one: the three counters measure
   different things and all three hold simultaneously on the same run — three laps through
   `remote_resolution`, four entries into `await_service_stability`, `resolution_cycles` 5. The
   original "three laps, ten read sites, still no writer" was accurate in all three parts. What was
   wrong with it was never the count, only the cause, and swapping in two unrelated counters
   obscured that for a revision. `test_the_closure_stage_collapses_onto_the_parent_as_one_hop_too`
   carries the corrected cause in its docstring and deliberately does not assert it. An assertion
   was written — every lap must read fewer findings after the repair than before — and could not be
   shown red: emptying `_PLANT_HEALTHY_PROFILES`, and dropping `CPE_REBOOT` from
   `_RECOVERING_ACTIONS` to model late rather than absent recovery, both failed at that test's
   *first* assertion instead, `assert <IncidentStatus.ESCALATED: 'escalated'> is
   <IncidentStatus.CLOSED: 'closed'>`. No reachable world has an early lap read unmoved and the run
   still close, so `status is CLOSED` already owns the claim; the mechanism itself has four owners
   in `tests/unit/test_adapters.py`. By the standard CPE-9 was held to, a guard that cannot be shown
   red is dead defence and was not shipped.

   The standard those tests are held to is worth stating, because it was learned by being caught out.
   The first detector regression test **passed with the defect reinstated**: it asserted
   `physical_evidence > 0` on the first service with a localised delimiter fault, and that service
   already carried 1.54 of physical evidence from the telemetry detectors. The localiser's
   contribution is only observable on the 17 services where it is the *sole* source, so the invariant
   had to become the exact sum rather than its sign. A regression test never seen to fail is a
   regression test that has not been tested — which is why every *mutation-checked* row in §5 means
   exactly that, and why the two survivors described above were treated as defects in the tests
   rather than as noise.
2. **The specification's model list has 34 entries, not 33.** Counted from its own bullet list. Two
   earlier docstrings in this repository said 33 and were wrong.

   `domain/__init__.py` then claimed the count was "asserted by `tests/unit/test_domain_exports.py`"
   — a file that did not exist, which made the correction itself another unbacked claim. The reason
   it could not exist is worth recording: the specification lived only outside the repository, so
   nothing that runs in CI could read the list every model, enum and node here is answerable to. It
   is now vendored at `docs/specification.md` and parsed by that test, which checks 34 required names
   against 40 exported models and 6 declared supporting ones. A copy invites drift, so the test also
   compares the vendored file against the original byte-for-byte when the original is present, and
   skips when it is not.
3. **`simulated: True` is returned even when the gate permits the write.** This is deliberate — a
   fixture-backed adapter never opens a TR-069 session, and claiming otherwise would be the defect —
   but it means "did this write really happen?" must be read from `result["gate"]["permitted"]`, not
   from `result["simulated"]`. A real adapter would return `simulated: False`.
4. **`stream_events(version="v3")` is unverified.** See the end of §2. Nothing depends on it.
5. **Postgres is untested against a live server.** The lazy import, the open/setup/close sequence
   and the `setup=False` path are all exercised now — the last two against an injected stand-in
   rather than a database — but no checkpoint has ever been written to Postgres. `setup()`'s DDL and
   the resume-after-restart scenario are the two things a stand-in cannot prove, and they are what
   `@pytest.mark.postgres` will cover.

   This is the gap that hid the defect in §2, so it is worth being precise about what closed and
   what did not. What closed: the branch is no longer *unreachable* from the suite, and reinstating
   the shipped defect now turns two tests red. What did not: the stand-in was written from a
   measurement of the real `from_conn_string`, and it is only as good as that measurement stays.
   `test_the_postgres_saver_has_a_lifecycle_a_plain_factory_could_not_have` is the guard on that —
   it asserts the same shape against the installed library, and skips where the optional extra is
   absent. A fake nobody checks against the real thing is a test of the fake.
6. **Vendor field names are invented.** 47 of them, listed in `docs/vendor-integration-gaps.md`.
   That file is the falsifiable part of assumption A1.
7. **No gate reads the prose, and the prose had drifted.** Every check in this repository runs
   against `src/` and `tests/`. Nothing reads README.md, the Makefile or this file, so a false claim
   in any of them survives a green suite indefinitely — and §5 recorded the check for the row
   covering those files as `pip install -e ".[dev]"` succeeds, which is structurally incapable of
   catching one. Audited against the tree on 2026-08-16, it had missed all of the following:

   - `[project.scripts]` named `lpr_cpe.cli:main`, and **no `cli.py` had ever been written**.
     `git log --all --diff-filter=D` returns nothing for it, so it was not deleted; it was never
     there. `pip install` reported success and the console script raised `ModuleNotFoundError` in
     the user's shell. This is the sharpest form of the gap: packaging metadata is executable
     configuration that no test imports.
   - README's Layout table listed `src/lpr_cpe/api/`, which does not exist; described `persistence/`
     as holding a transactional outbox, which is gap-listed and unwritten; and described `docs/` as
     holding diagrams, decision tables, the runbook and the decision records, **none of which
     exists**. It linked to `docs/operations-runbook.md`, which is not there.
   - README advertised the `optimizer` and `anthropic` extras as enabling OR-Tools dispatch and real
     model calls. Neither has an implementation behind it, so installing either changes nothing.
   - `make demo` invoked a `cli.py` subcommand that `cli.py` deliberately does not define, and
     `make serve` invoked `lpr_cpe.api.app`, which has never existed. Both now name the gap and exit
     non-zero, because an argparse error or a `ModuleNotFoundError` reads as a broken install rather
     than as unbuilt work.
   - `make check` described itself as "Everything CI runs". There is no CI.
   - This section's own counts — 98 source files, 758 tests, "coverage has not been measured" — were
     each correct when written and stale within the day.

   Two things follow. The first is a rule: **verify a documentation claim against the tree, not
   against this file**, and treat §5's *State* column as a dated measurement rather than a fact. The
   second is that the rule is a poor substitute for a gate, and the cheapest ones are known —
   `tests/unit/test_cli.py` already closes the entry-point case by importing what `pyproject.toml`
   declares, and the same shape would close the rest: a test that asserts every path a markdown file
   names exists, and every relative link resolves. That test is not written. Until it is, this gap
   is open by construction rather than by oversight.
