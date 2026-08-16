# Operator dashboard: architecture and implementation plan

**Status: proposed. Nothing here is built.** This document is a design to be argued with, not a
record of work done. Where it states a measurement, the measurement was taken; where it states a
choice, the choice is mine and is marked as such.

## 0. What was asked, and what the specification says about it

The request: *every node in the graph visible in a Streamlit dashboard, which also provides the GUI
for human intervention and decisions.*

**The specification requires no dashboard and names no UI framework.** It requires an HTTP surface —
fifteen endpoints at `docs/specification.md` lines 1476-1490, including `POST
/incidents/{id}/resume`, `/approvals` and `/customer-response` — and it requires that P19's handover
approval display eight named fields. It says nothing about how a human sees any of that. It also
specifies no approval-payload schema, no approval timeout and no auto-approve rule.

So every choice below is a design decision rather than a requirement being met, and the ones that
could reasonably have gone the other way are marked **D-n** and given their reason. This matters for
a repo whose §5 table is read as a compliance checklist: a dashboard cannot be "behind" on a
deliverable that was never asked for, and it must not be allowed to displace the endpoints that
were.

---

## 1. The read layer already exists. The write layer does not.

The single most useful finding of the design pass: `graph/inspect.py` was written for exactly this
consumer and already provides four of the five reads a dashboard needs.

| Need | Already there |
| --- | --- |
| The incident's real state while a subgraph is paused | `effective_state(app, config)` |
| What is being asked | `pending_approval_for(app, config)` |
| The resume handle | `interrupt_payloads(app, config)` → `[{"id", "value"}]` |
| Is a human being waited on at all | `is_awaiting_human(app, config)` |
| **Which node is asking** | **missing — see §3** |

Its module docstring already states the case for its own existence in dashboard terms: an endpoint
built on the parent's `aget_state(config).values` alone "would report 'dispatch planning' for an
incident that has been sitting on someone's approval queue since Tuesday."

`security/rbac.py` likewise anticipated a UI in as many words — reason 4 on `_ALLOWLIST` is that the
structure "serialises into `docs/policy-controls.md` and into an API response for a UI that greys out
buttons, so the UI cannot disagree with the server." `ToolAllowlist.as_dict()` and
`approvals_as_dict()` are that export.

What does not exist is any way to *answer*. Nothing in the tree calls `Command(resume=...)` outside
tests. That asymmetry sets the shape of the work: the dashboard is mostly a rendering problem on top
of existing reads, plus one small, genuinely dangerous write path that deserves most of the
verification effort.

---

## 2. Six measured constraints the design has to survive

These are not style preferences. Each has a failure mode that a plausible naive dashboard walks
straight into, and each was measured — the first four today, against langgraph 1.2.11.

**C1 — A paused subgraph's state is invisible from the parent.** `aget_state(config).values` reports
`status=dispatch_planning, pending_approval=None` at the exact moment an approval is outstanding.
*Naive failure:* the dashboard shows an empty approval queue while work is blocked. *Mitigation:*
every read goes through `inspect.py`. Never `aget_state` directly.

**C2 — `Command(resume={})` is a silent no-op.** The graph re-pauses with the interrupt still
outstanding and the node leaves **no audit trail** — pinned by
`test_an_empty_resume_map_never_reaches_the_node`. *Naive failure:* a form submitted before a
selection is made looks to the operator like a click that did nothing, and looks to the audit trail
like a click that never happened. §2 of the plan already states the obligation: "whatever validates
resume payloads at the API boundary has to reject `{}` explicitly."

**C3 — `_decision_from_answer` is deliberately total and never raises.** A non-dict answer, a missing
`decided_by`, a role that may not approve, or an unrecognised status each produce a **recorded
rejection**. That is the right behaviour for the graph — an exception there "would leave the incident
un-resumable at the one moment a human is already involved" — and it is the most dangerous thing in
this design. *Naive failure:* a Streamlit form that forgets to send `decided_by` does not error. It
**silently refuses the approval** and the incident proceeds down the refused branch, with an audit
trail that says a human declined. **This is the single failure the verification plan in §8 is most
concerned with.**

**C4 — The interrupt object does not say which node raised it.** Measured: `Interrupt` exposes `id`
(an opaque 32-hex hash, e.g. `5ed6ae3c455c6fcdc2eee48356fbbf12`) and `value`. `from_ns` is a
*classmethod*, not data — reading it yields a bound method. So `interrupt_payloads()` alone cannot
answer "which step is this?", which is the dashboard's central question. §3 shows where the answer
actually lives.

**C5 — No streaming API is used or verified anywhere in this repo.** Every test drives the graph with
`ainvoke`; `grep` for `astream|stream_mode|stream_events` matches no test file. §2 of the plan
records `stream_events(version="v3")` as "deliberately not relied upon", and §6 gap 4 repeats it.
*Consequence:* live node-by-node progress cannot be built on an API this repo has never exercised
without first verifying it, and §7 treats that as optional work with its own gate — not as a
foundation.

**C6 — `checkpointer_scope` is an async context manager owned by whoever owns the application
lifespan.** Its module docstring documents at length why an earlier synchronous factory could not
work. *Naive failure:* Streamlit reruns its entire script on every widget interaction. A
`checkpointer_scope` entered at script top level is re-entered on every click; with `InMemorySaver`
that discards the incident's whole history each time, and with Postgres it opens a connection per
click in an event loop that dies with the rerun. Caching it in `st.cache_resource` moves the problem
rather than solving it: the cached saver's connection belongs to the loop that created it.

C6 is the constraint that decides §4.

---

## 3. Where "which node is this?" actually lives

`inspect._snapshots()` already walks the parent snapshot and every paused child, outermost first. It
returns the snapshots — and **discards `.next` and `task.name`, which are exactly the node
identity.** Measured on a parent → subgraph → gate arrangement paused at the interrupt:

```
root.next                        ('resolution',)
root.tasks[0].name               'resolution'
root.tasks[0].state.next         ('request_x_approval',)
[s.next for s in _snapshots(…)]  [('resolution',), ('request_x_approval',)]
```

So the full path to the paused node is already inside data the existing walk fetches. The dashboard
needs **one new function** in `inspect.py`, not a new subsystem:

```
awaiting_node_path(app, config) -> tuple[str, ...]   # ("resolution", "request_x_approval")
```

**D1 — the node path is derived from the snapshot walk, not from the interrupt id or from
`get_graph()`.** The interrupt id is opaque (C4). `get_graph()` is separately known to be lossy: it
keeps one edge per `(source, target)` pair and drops four of the fourteen declared answers on this
graph, including both `PENDING_STAGES` exits. `builder.BRANCH_TARGETS` + `DECISION_AFTER` +
`routing.DECISIONS` are the authoritative topology, and `_check_tables()` already holds them against
each other at compile time. `tests/unit/test_cli.py::test_topology_names_every_answer_the_builder_declares`
is the existing guard; the dashboard must fall under the same rule.

---

## 4. The central decision: where the graph runs

This is the one choice that has to be made before anything is written, and it is not primarily a UI
question.

### Option A — Streamlit imports the graph and drives it in-process

Streamlit calls `compile_parent_graph()`, opens `checkpointer_scope`, and issues
`ainvoke` / `Command(resume=…)` itself.

*For:* nothing else has to exist first. A demo could run this week.

*Against:* it puts the application lifespan inside a script that re-executes top to bottom on every
click (C6). It makes Streamlit the owner of resume validation, so the C2 and C3 guards live in the
presentation layer — and then have to be written a second time when the specification's
`POST /incidents/{id}/resume` is built, at which point there are two owners of "what is a valid
answer" and they will diverge. Two browser tabs are two Streamlit sessions are two graphs writing one
checkpoint with no coordination. And it inverts the repo's own doctrine: RBAC enforced in the client
is RBAC a client can skip.

### Option B — Streamlit is an HTTP client of `src/lpr_cpe/api/`

The API is built first. Streamlit imports nothing from `lpr_cpe.graph` and holds no checkpointer.

*For:* FastAPI's `lifespan` is precisely the "caller that owns the application lifespan" that
`checkpointer_scope` was designed for. A Streamlit rerun becomes a fresh GET and is therefore
harmless. The C2/C3/RBAC guards live at the boundary the specification already names, are written
once, and protect the CLI and any future client too. The server stays authoritative on permissions
and the UI merely greys out buttons — which is the arrangement `rbac.py`'s own comment describes.
And the API is a required deliverable regardless; this sequences it rather than competing with it.

*Against:* more to build before anything is visible. Two deliverables, not one.

### Option C — read in-process, write through the API

Rejected. It re-creates C6 for the read path (an in-process read still needs a live checkpointer) and
creates two owners of "what state is this incident in".

### Recommendation

**Option B**, with §7's phasing chosen so that a useful dashboard exists at the end of phase 2 rather
than only at the end.

**D2 — Streamlit never imports `lpr_cpe.graph`.** Stated as a rule because it is checkable: a test
asserting the dashboard package's import graph is free of `lpr_cpe.graph` and `lpr_cpe.persistence`
is a cheap, honest guard against Option A leaking back in one convenience import at a time.

---

## 5. Architecture

```
  Browser
     │  HTTP
     ▼
┌──────────────────────────────────────────────────────────────────┐
│ dashboard/            Streamlit. Rendering only. No lpr_cpe.graph │
│   app.py              page shell, session, auth handoff           │
│   pages/              incident list · incident detail · approvals │
│   view/               ← pure functions, no Streamlit import       │
│     node_view.py        node inventory × node_visits → per-node   │
│     approval_view.py    ApprovalRequest → form spec               │
│     timeline_view.py    audit_events → ordered per-node timeline  │
└──────────────────────────────────────────────────────────────────┘
     │  httpx
     ▼
┌──────────────────────────────────────────────────────────────────┐
│ src/lpr_cpe/api/      FastAPI. Owns lifespan and checkpointer     │
│   lifespan            async with checkpointer_scope(...)          │
│   routes/incidents    GET state  (→ inspect.effective_state)      │
│   routes/approvals    GET pending · POST decision                 │
│   routes/resume       POST resume  ← C2 and C3 guards live HERE   │
│   routes/topology     GET the 25-node inventory + 24 decisions    │
└──────────────────────────────────────────────────────────────────┘
     │  in-process
     ▼
  graph.inspect  ·  compile_parent_graph()  ·  checkpointer_scope
```

**D3 — all dashboard logic lives in `view/`, which does not import Streamlit.** Streamlit code is
notoriously hard to unit-test, and this repo is at **80.96% coverage against a
`--cov-fail-under=85` gate that already fails**. Several hundred untestable statements would push an
already-red gate further red and make it permanently unreachable. Keeping the logic in pure functions
that take an `IncidentState` and return a data structure means the dashboard's *behaviour* is
covered by ordinary unit tests and only the thin `st.*` render layer is not. This is a design
constraint imposed by an existing failing gate, and it is worth naming as such.

**D4 — one new dependency, and only for the UI.** Measured against `pyproject.toml`: `fastapi`,
`uvicorn[standard]` and `httpx` are already **base** dependencies, not extras — declared, installed,
and currently imported by nothing. So phase 2 adds no dependency whatsoever; it uses three that the
project already carries and has never exercised. Only `streamlit` is new, and it belongs in a
`dashboard` extra rather than in the base set, so that a production API deployment does not install a
web UI it will never serve. Per the convention `README.md` now follows, that extra must be described
by what it actually enables at the time it is added — the repo already labels two extras
(`optimizer`, `anthropic`) "change nothing if installed today", and a third that silently did nothing
would be worse than either.

That `fastapi` and `uvicorn` are already declared is itself an argument for Option B over Option A:
the dependency decision was made when the project was scaffolded, and Option A would leave them
declared and permanently unused.

---

## 6. What the two halves of the request actually mean

### 6.1 "Each node visible" — 25 nodes, and a state per node

The inventory, measured today:

| Registry | Count | Nodes |
| --- | --- | --- |
| `graph.nodes.PARENT_NODES` | 11 | `receive_signal` … `generate_resolution_options` |
| `subgraphs.remote_resolution.REMOTE_RESOLUTION_NODES` | 6 | `select_remote_action` … `abandon_remote_action` |
| `subgraphs.self_help.SELF_HELP_NODES` | 8 | `select_self_help_script` … `abandon_self_help` |
| **total** | **25** | plus `routing.DECISIONS` — 24 declared, 6 wired |

The per-node state comes from three sources that already exist and are already checkpointed. **No
streaming API is required for any of it** (C5):

| Displayed state | Source | Why it is trustworthy |
| --- | --- | --- |
| not yet reached | absent from `node_visits` | — |
| done (×n) | `node_visits[name]` | written by the `@node` decorator on **every** node, unconditionally — including the budget-escalation path. Reduces per-key `max`, so a replay cannot inflate it |
| **waiting on a human** | `awaiting_node_path()` (§3) | the only node that can be *currently* paused |
| what it did | `audit_events` filtered on `.node` | every event carries the node name, and `check_node_registry` guarantees at import that the name in the audit trail equals the name in the topology |
| unreachable today | `builder.PENDING_STAGES` | the three unwired exits, named rather than hidden |

That last row is a deliberate inclusion. Six of twenty-four decisions are wired; a dashboard that
drew only what runs would show a tidy graph and conceal that most of it is unbuilt — the same failure
the README's "four things this table would otherwise be expected to list" paragraph exists to
prevent.

**D5 — per-node progress is reconstructed from the checkpoint, not streamed.** It therefore survives a
process restart, a browser refresh and a Streamlit rerun, and it is identical for an incident being
watched live and one read back in six months. The cost is granularity: state advances at super-step
boundaries, so a long-running node shows as "not yet done" rather than "running". §7 phase 5 offers
`astream` as an *optional* refinement on top, gated behind its own verification, and nothing else
depends on it.

### 6.2 "Human intervention and decisions" — **two** forms, not one

The GUI has two distinct interventions with different payloads, different validation and different
answers to "who may do this". Treating them as one form is the most likely design error.

**Form 1 — approval.** Six kinds, raised by `interrupts.request_approval`. The interrupt payload is
`{"approval_request": <ApprovalRequest json>, "permitted_roles": [...]}`. The answer contract is
defined by `_decision_from_answer`: `status`, `decided_by` (**must be non-empty**),
`decided_by_role`, `rationale`, `reason_code`, `conditions`, `modified_action`. Approver roles,
measured from `rbac.approvers_for`:

| Kind | May approve |
| --- | --- |
| `low_confidence_rca` | admin, noc_operator, noc_supervisor, osp_engineer |
| `high_risk_remote_action` | admin, noc_operator, noc_supervisor |
| `dispatch` | admin, noc_operator, noc_supervisor |
| `clean_to_dirty_handover` | admin, field_technician, noc_supervisor, osp_engineer |
| `high_blast_radius_action` | admin, noc_supervisor |
| `exceptional_closure` | admin, noc_supervisor |

`automation` appears in none, by design.

**Form 2 — customer response.** Raised by `self_help.await_customer_response`. Payload
`{"customer_response_request": {...}, "accepted_responses": ["completed", "declined"]}`. Parsed by
`customer_reply`, which accepts `{"response": "completed"|"declined"}` or
`{"customer_completed_step": <bool>}`, and returns `None` for anything else.

**D6 — the two forms are discriminated on the payload's top-level key** (`approval_request` vs
`customer_response_request`), not on the node name or the interrupt id. Both keys are literals in the
interrupting nodes, and a new gate that invents a third shape should render as "unknown question,
refusing to guess" rather than being coerced into the approval form.

Form 2 is **not** an approval and the difference is operational, not cosmetic. An operator using it
is *relaying* what a customer said, which is a different act from authorising something. Two
properties of `customer_reply` have to reach the UI intact: `None` is **not** a decline — the
docstring is explicit that reading a parse failure as refusal "would end a customer's window early
and roll a truck at them on the strength of a parse failure" — and "completed" is not resolution,
because `verify_self_help` and the telemetry decide that. A form offering *completed / declined /
no answer yet* as three equal buttons would be wrong on both counts: the third is not an answer, it
is the absence of one, and submitting it must produce a re-pause, not a decision.

**Open question O1, for the user:** the specification does not say who may speak for a customer.
`service_desk` is the plausible role and it is in no approver set today. This needs a decision before
form 2 is built; it is not derivable from the code.

---

## 7. Implementation plan

Five phases. Each ends at a point where the repo is green and something is demonstrably better; none
depends on a later one.

**Phase 1 — `inspect.awaiting_node_path()`.** One function, in the module that already does the
walk (§3). Small, self-contained, and useful to the CLI and the API independently of any UI.
*Gate:* the three commands, plus the red-test in §8.1.

**Phase 2 — the read-only API.** `src/lpr_cpe/api/` with `lifespan` wrapping `checkpointer_scope`,
and the read endpoints only: incident state (via `effective_state`), pending approval, interrupt
payloads, node path, and the topology inventory from the three tables. No writes at all.
*Why reads first:* it closes the largest specification gap, it is safe by construction, and it
unblocks phase 3 without any of the C2/C3 risk. `api/` is `docs/vendor-integration-gaps.md`'s and
§5's outstanding row; this is that row, not a detour around it.

**Phase 3 — the Streamlit dashboard, read-only.** Node inventory with per-node state (§6.1), incident
timeline from `audit_events`, and the pending question **rendered but not answerable**. At the end of
this phase the user's first request is fully met and nothing can be broken from the UI.

**Phase 4 — the write path.** `POST /incidents/{id}/resume` and the two answer forms. This is where
C2 and C3 live and where the verification effort concentrates (§8). Server-side RBAC first, then the
UI's greying-out as a courtesy on top — in that order, so the courtesy is never mistaken for the
control.

**Phase 5 — optional: live progress via `astream`.** Only if per-super-step granularity proves
insufficient in practice. Gated behind first *verifying* a streaming mode against 1.2.11 and
recording the measurement in IMPLEMENTATION_PLAN §2 the way every other LangGraph behaviour there was
recorded. **§2's existing entry on `stream_events(version="v3")` must not simply be deleted** — it is
a record that something was researched and not reproduced, and if a different mode is verified
instead, that is a new line rather than an edit to the old one.

---

## 8. What must be shown able to go red

The repo's doctrine — stated in `check_node_registry` and throughout `tests/unit/test_persistence.py`
— is that a guard is not trusted until it has been seen to fail. Six guards, each with the defect to
reinstate and the failure to record in the test's docstring.

**8.1 The node path is real.** Pause an incident at a nested gate; assert `awaiting_node_path()`
returns the two-element path *and* that the parent's own `.next` alone does not name the inner node.
*Red by:* reading only `root.next` — reports `("resolution",)`, naming the subgraph rather than the
step.

**8.2 The naive read is wrong.** Assert the dashboard's view disagrees with
`aget_state(config).values` while paused, in the direction §2's table measured. *Red by:* building
the view on the naive read — reports `dispatch_planning` / no pending approval.

**8.3 An empty resume is rejected at the boundary.** *Red by:* removing the check — the endpoint
returns success, the graph re-pauses, and **no audit event is written**. The absence of the audit
event is the assertion that matters; a test that only checks the re-pause cannot tell "dropped before
delivery" from "delivered and found unusable", which is the distinction
`test_an_empty_resume_map_never_reaches_the_node` was written to preserve.

**8.4 A malformed approval answer never reaches the graph.** *Red by:* removing the boundary
validation and posting an answer with no `decided_by` — the endpoint returns success and the incident
proceeds with a **recorded rejection nobody made**. This is C3, and it is the one to write first.

**8.5 RBAC is enforced server-side.** Post an approval as a role not in `approvers_for(kind)` with
the UI bypassed entirely. *Red by:* enforcing only in Streamlit — accepted.

**8.6 The dashboard's node list cannot drift from the graph's.** Assert the rendered inventory equals
the union of the three registries. *Red by:* adding a node to a registry and not to the dashboard.
The mirror of `check_node_registry`, one layer out.

**8.7 The import boundary holds (D2).** Assert `lpr_cpe.graph` and `lpr_cpe.persistence` are absent
from the dashboard package's import graph. *Red by:* one convenience import.

---

## 9. Open questions the specification does not answer

These need the user, not the code.

- **O1** Who may relay a customer response? (§6.2)
- **O2** Approval timeout. `ApprovalRequest.expires_at` is populated and
  `ApprovalRequest.is_expired(now)` is written — and measured today, **`is_expired` has exactly one
  occurrence in the tree, its own definition. Nothing calls it, in `src` or in `tests`.** So an
  approval that has expired is indistinguishable, to the graph, from one that has not. Should the
  dashboard show it as still actionable, refuse it, or show it as expired and let a supervisor
  override? There is no timeout rule in the pack, and answering this in the UI alone would put the
  rule in the client — the C3/8.5 mistake in a different costume.
- **O3** Authentication. The specification requires writes to be authenticated in production. Who
  issues the identity that becomes `decided_by`? Until that is answered, `decided_by` is
  operator-typed free text, which is an audit trail that records a claim rather than a fact.
- **O4** Whether the dashboard may *start* an incident, or only observe and answer. Starting one
  makes it a control plane and raises the write-switch question (`LPR_APP_MODE` +
  `LPR_ALLOW_PRODUCTION_WRITES`); observing keeps it strictly downstream. This plan assumes the
  latter throughout.

---

## 10. Deployment: what Option B is as running processes, and what Docker changes

### 10.1 Option B is two processes

Option A is one process: `streamlit run` imports the graph, holds the checkpointer, and drives it.
Option B is two, and the second is the whole difference:

| | Process | Runs | Holds | Imports |
| --- | --- | --- | --- | --- |
| 1 | **API** | `uvicorn lpr_cpe.api.app:app` | the checkpointer, via `lifespan` → `checkpointer_scope` | `lpr_cpe.graph`, `lpr_cpe.persistence` |
| 2 | **Dashboard** | `streamlit run dashboard/app.py` | nothing but a base URL and a session | `httpx` and its own `view/` — **never `lpr_cpe.graph`** (D2) |

The dashboard holds no graph, no checkpointer and no incident state. Every screen is an HTTP GET;
every approval is an HTTP POST. That is what makes a Streamlit rerun harmless (C6) and what leaves
one owner for resume validation (C2, C3).

### 10.2 Docker Desktop

**There is nothing to containerise yet.** Measured: the tracked tree has six files outside
`src/`, `tests/` and `docs/` — `.env.example`, `.gitignore`, `IMPLEMENTATION_PLAN.md`, `Makefile`,
`README.md`, `pyproject.toml`. **No `Dockerfile`, no `docker-compose.yml`, no `.dockerignore`, and no
`api/` or dashboard to put in them.** So the question is not "will it run" but "will it run once
built", and the answer is yes, with three things that are worth knowing before rather than after.

Nothing in the dependency set resists a container. Everything is pure Python except `psycopg[binary]`
and the unused `ortools`, both of which ship manylinux wheels; `requires-python = ">=3.12"` is
satisfied by any current slim image. The two processes map onto two services cleanly because Option B
already separated them.

**Docker forces the persistence decision that in-process development lets you defer.** With
`LPR_POSTGRES_DSN` empty the checkpointer is `InMemorySaver`, which lives in the API process's heap.
In a container that means every `docker compose restart` discards every in-flight incident and every
pending approval — and the API cannot be scaled past one replica, because two replicas would hold
two divergent copies of the same incident and an approval posted to one would be invisible to the
other. A serious compose file therefore has **three** services (dashboard → api → postgres) and sets
the DSN. A demo stack can keep one and accept that restarting it is a reset, but that should be a
choice rather than a discovery.

**That is an opportunity, not just a cost.** IMPLEMENTATION_PLAN §6 gap 5 records that Postgres is
**untested against a live server** — "no checkpoint has ever been written to Postgres", and
`setup()`'s DDL and the resume-after-restart scenario are precisely the two things the current
injected stand-in cannot prove. A compose file with a Postgres service is exactly the fixture
`@pytest.mark.postgres` was reserved for. Containerising this stack retires a known gap rather than
merely packaging around it, and **the resume-after-restart test is the one to write first**, because
it is the claim the whole approval design rests on and the only one no in-memory run can make.

**Three concrete gotchas, all cheap if known in advance:**

- **Streamlit binds localhost by default**, which inside a container means unreachable from the host.
  It needs `--server.address=0.0.0.0` (port 8501). Uvicorn needs `--host 0.0.0.0` for the same
  reason.
- **The dashboard's API base URL is a setting that does not exist yet.** `Settings` has no host, port
  or base-URL field for anything internal — the eight `*_base_url` fields are all outbound to vendor
  systems. Inside compose the value is `http://api:8000`; from a browser on the host it is
  `http://localhost:8000`. One new setting, and it belongs to the dashboard rather than to
  `lpr_cpe.config.Settings`, since D2 says the dashboard shares no configuration object with the
  graph.
- **`LPR_ENVIRONMENT=prod` refuses to start without `LPR_WEBHOOK_SECRET`** — it is a
  `model_validator`, not a warning. A production-shaped compose file that omits it fails at startup
  with a pydantic error, which is correct behaviour and confusing if unexpected.

The safety switches need no special handling: `LPR_APP_MODE` defaults to `simulation` and
`LPR_ALLOW_PRODUCTION_WRITES` to `false`, so a containerised stack cannot reach an external system
until someone sets both. A Docker demo is safe by default, which is the design working as intended.

**One Windows-specific note.** This checkout lives under a OneDrive-synced path. Bind-mounting it
into a container for hot reload works but puts a file watcher on a synced directory, which is a
known source of spurious reload loops and slow rebuilds under WSL2. Copying the source into the image
for demo runs, and mounting only when actively editing, avoids it.
