# Implementation report — LPR CPE predictive service assurance

The specification's deliverable 17. It lists what was implemented, the assumptions made, the
commands run, the test results, the coverage, the remaining vendor-integration gaps, and the risks
before production use.

**Read this with `audit/MANIFEST.json` open.** Every countable claim below lives in §4, in one
table, and that table is compared key by key against the manifest by
`tests/unit/test_audit_bundle.py`. Editing a number here without re-running `make audit` is a red
test. The arrangement exists because this repository has four recorded occasions on which a figure
in prose was correct when written and stale within the day, and nothing noticed any of them —
IMPLEMENTATION_PLAN.md gap 7 is the general form.

Everything that is *not* a number is prose and carries no such guarantee. Where prose and code
disagree, the code is right.

---

## 1. What was implemented

The build order followed the specification's own fallback ordering: correct domain models and state
contract, then the parent graph with real routing and policy checks, then persistence and resumable
approvals, then adapters and dispatch and tests, then documentation. Fewer fully working paths were
preferred to more stubs.

| Area | State |
| --- | --- |
| `config` — settings, clock, scan windows, the two write switches | done |
| `domain` — 34 specification models + 6 supporting, with validators | done |
| `domain/boundaries.py` — the Clean/Dirty crew split | done |
| `graph/state.py` — the state contract and its seven reducers | done |
| `security` — redaction, prompt-injection screening, RBAC | done |
| `observability` — structured logging, trace attributes, KPI derivation | done |
| `integrations` — ten Protocols, the write gate, ten fixture-backed simulators | done |
| `policies` — YAML pack and the engine that fails closed | done |
| `detectors` — the thirteen baseline detectors behind one Protocol | done |
| `decision_services` — classification, banding, blast radius, forecast, restoration | done |
| `dispatch` — greedy optimizer, twelve constraints, the seam a solver would fill | done |
| `graph` — 17 parent steps, 9 subgraphs, all 24 decisions wired | done |
| `persistence` — checkpointer factory and the allowlisted serialiser | done |
| `cli.py` + `audit.py` + `runner.py` — reports, the audit bundle, and `lpr-cpe run` | done |
| `persistence` — transactional outbox and migrations | **not built** |
| `api` — the FastAPI surface and its webhooks | **not built** |
| model provider and the deterministic fake | **not built** |
| the seventeen specification scenarios | **not built** — `lpr-cpe run` drives one scripted path, which is not the same thing |
| Docker Compose development environment | **not built** |
| CI | **none exists** |

Of the specification's seventeen deliverables, seven are complete (1, 2, 3, 4, 7, 11 in part, 16),
and the rest are partial or unstarted. Deliverables 5, 6 and 15 — FastAPI, the PostgreSQL profile
against a live server, and Docker Compose — are the largest single block of unbuilt work.

### Documentation

`docs/vendor-integration-gaps.md` is one of the nine documents the specification asks for. Two more
exist and are **not** on that list: `docs/workflow-diagram.md` and `docs/dashboard-architecture.md`.
This report is a third. `docs/specification.md` is the vendored input, not a deliverable.

Of the ten required diagrams, one is done: the ten Mermaid figures in `docs/workflow-diagram.md`
covering the parent graph and its nine subgraphs.

---

## 2. Assumptions made

Recorded in full as A1–A5 in IMPLEMENTATION_PLAN.md §1. In short:

- **A1** — the master prompt is the sole source of truth. The repository was empty at the start; no
  LPR materials, API specifications or fixtures existed to reuse.
- **A2** — no vendor endpoint is real. Every adapter is a Protocol plus a fixture-backed simulator,
  and every invented field name is listed in `docs/vendor-integration-gaps.md`. That file is what
  makes A1 falsifiable.
- **A3** — Puerto Rico, `America/Puerto_Rico`, fixed UTC−04:00. All stored timestamps are
  timezone-aware UTC; there is no naive datetime anywhere.
- **A4** — production writes are off unless switched on. `APP_MODE` defaults to `simulation` and
  `ALLOW_PRODUCTION_WRITES` to `false`, enforced in one place.
- **A5** — no language model is consulted for any number a decision depends on. Nothing in `src`
  calls a model provider at all today.

Eight further decisions worth their own record are D1–D8 in IMPLEMENTATION_PLAN.md §4.

---

## 3. Commands run

Six gates, run by `make audit`, captured verbatim into `audit/latest/`. The manifest records the
commit, whether the tree was clean, the interpreter, and the versions of the six distributions the
design's measured behaviour is pinned to.

| Gate | Command | What it establishes | Output |
| --- | --- | --- | --- |
| ruff-check | `python -m ruff check src tests` | no lint finding | `audit/latest/ruff-check.txt` |
| ruff-format | `python -m ruff format --check src tests` | formatted as configured | `audit/latest/ruff-format.txt` |
| mypy | `python -m mypy` | strict-mode types over `src/lpr_cpe` | `audit/latest/mypy.txt` |
| pytest | `python -m pytest --cov --cov-report=term-missing --cov-fail-under=85` | the suite, behind the coverage gate | `audit/latest/pytest.txt` |
| topology | `python -m lpr_cpe.cli topology` | the parent graph compiles, so its four tables agree | `audit/latest/topology.txt` |
| config | `python -m lpr_cpe.cli config` | the settings, with no secret printed | `audit/latest/config.txt` |

`make check` runs the first four. The last two are here because `build_parent_graph` runs
`_check_tables` before returning anything, which makes `topology` the cheapest honest check in the
system: it fails on tables that disagree without a database, a network or a model provider.

**Both interpreters were checked.** `make` uses `.venv`, which is where the version table in
IMPLEMENTATION_PLAN.md §2 was resolved and the only one with the `optimizer` extra; a bare
`python -m pytest` uses the system interpreter, where `ortools` is absent. The audit bundle records
which one produced it, because the two are easy to confuse and one of them cannot run every test.

---

## 4. Test results and coverage

Every figure in this table is read out of `audit/MANIFEST.json` by
`tests/unit/test_audit_bundle.py::test_the_report_states_no_figure_the_manifest_does_not`. None of
them is typed by hand into prose anywhere else in this document.

It is `tests_total` and not `tests_passing` for a reason worth stating, because it is the one place
this arrangement is genuinely awkward: the bundle writes the manifest *after* pytest, so the suite
always compares this table against the **previous** run's manifest. A figure that moves when the
suite goes green cannot converge — a report saying 935 against a stale manifest saying 934 fails,
which keeps the manifest at 934. Collected tests do not move between a red run and a green one, so
that is the figure the gate can hold. `tests_passing` and `tests_failed` are in the manifest for
whoever is reading it, and deliberately not here.

| figure | value | note |
| --- | --- | --- |
| `tests_total` | 985 | all unit tests; see the caveat below |
| `coverage_percent` | 85.67 | line and branch, over `src/lpr_cpe` |
| `coverage_gate_percent` | 85 | enforced from 2026-08-24; see below |
| `source_files_typechecked` | 111 | `mypy --strict`, no issues |
| `files_formatted` | 139 | `ruff format --check` |

**The suite is unit-only.** There are no integration, contract or scenario tests, and none of the
seventeen required scenarios exist. Every "done" row in §1 rests on committed tests, but they are
all of one kind, and the two resolution subgraphs being driven through the real parent graph from
intake onwards is the closest thing to end-to-end that exists.

**Three incidents run from event to closure**, and the figure moved because the harness got
better rather than the graph. Swept through `lpr-cpe run`, which answers all five pause shapes, the
41 services close 3 and escalate 38; the earlier count of 1 came from a walk that answered two
shapes and handed an approval payload to the other three. The other forty escalate, for three causes, all three
recorded as gaps: EXEC-1, the static `Fixtures.telemetry` field no repair moves, and EXEC-2.

**The coverage gate did not mean what it said until 2026-08-24.** `--cov-fail-under=85` compares
the total *rounded to the configured precision*, which defaults to zero — so 84.92% rounded to 85,
satisfied a bar of 85, and the run exited 0 while printing
`FAIL Required test coverage of 85% not reached`. Measured against coverage 7.15.4 and pytest-cov
7.1.0: the identical run exits 1 with `precision = 2` in `pyproject.toml` and 0 without it. The
effective bar had been 84.5%, and `make test` and `make check` had never once failed on coverage.
The line is now set and the gate is real. This was found by the audit bundle on its first run, which
is the best argument for the bundle existing that this report can offer.

Coverage is not evenly spread, and the modules it concentrates in are the ones whose job is keeping
customer data out of logs and prompts: `security/redaction.py` and `security/injection.py` are the
two lowest in the tree, with `observability/tracing.py` and `logging.py` beside them. Raising the
aggregate is the wrong objective; a scenario test that ran one incident end to end would reach most
of them at once.

### Mutation checking

A green suite is not evidence that its assertions are load-bearing, so rows in IMPLEMENTATION_PLAN.md
§5 marked *mutation-checked* mean every regression assertion was verified by reinstating the defect
it names and watching that test fail.

**Six subgraphs were swept for the first time on 2026-08-24 and roughly half of every sweep
survived** — 93 mutations, 46 caught, 47 through. All 47 were then re-run against the entire test
suite rather than just their own module, and **that caught nothing beyond the three
`restoration_validation` had already yielded: 0 of the remaining 37**. A survivor of a module's own
tests survives the repository.

**All six are now closed and mutation-checked.** 47 survivors: 45 were real gaps, now held by
named tests each watched red, and 2 are proven equivalent mutants — a non-mapping plant report that
falls through to `MRStatus("")` anyway, and a `blocking_code` guard whose input for an assigned plan
is a satisfied-set summary no `ConstraintCode` parses. Gap 8 in IMPLEMENTATION_PLAN.md carries every
number and the severity notes.

Every subgraph row in IMPLEMENTATION_PLAN.md §5 now carries the mark. What that is worth is
bounded by the mutations someone thought to write: a sweep measures the claims it tries, and half of
the ones tried here were unheld. Treat the mark as "these 93 defects are refused", not as "the tests
are complete".

---

## 5. Remaining vendor-integration gaps

76 numbered gaps across eleven adapter families and eight workflow stages, in
`docs/vendor-integration-gaps.md`. Every external field name in this system is invented and labelled
as such; that file is the list to check when real API documentation arrives.

The ones that most change what this system can do:

| Gap | What it costs |
| --- | --- |
| EXEC-1 | the Clean-to-Dirty handover chain is entered and stalls at D18 — no first-cycle RCA rules a hypothesis out, so the three nodes past it are never reached |
| EXEC-2 | jTrack has no status feed, so a closed MR here and a submitted one there is an unresolvable mismatch at reconciliation |
| PREVENTIVE-2 | a preventive case is never re-read by anything, so the predictive true-positive rate is unmeasurable in principle |
| FIELD-1 | seven of the optimizer's twelve constraints cannot refuse anything, six for want of a fact nothing in state supplies |
| DISPATCH-1 | no CP-SAT implementation exists; the `optimizer` extra changes nothing even when installed |
| WFM-3 | crew skills are a vocabulary nobody has agreed, so skill matching is inert |
| CPE-8 | the simulator only recovers a device on a healthy plant profile, which shapes every end-to-end sweep |

---

## 6. Risks before production use

Ordered by what would hurt soonest.

1. **There is no HTTP surface, so there is no way to answer an approval.** Six approval gates
   interrupt and wait; nothing can resume them but a test harness. Until `api` exists the system
   cannot be operated at all, only run.
2. **No checkpoint has ever been written to PostgreSQL.** The lazy import, the open/setup/close
   sequence and the `setup=False` path are exercised against an injected stand-in. `setup()`'s DDL
   and resume-after-restart are the two things a stand-in cannot prove, and both are exactly what a
   production restart depends on.
3. **No CI.** Every gate in this repository is manual. The audit bundle makes a run reproducible and
   recorded; it does not make it automatic, and nothing stops a commit that never ran one.
4. **The suite is unit-only and half the subgraphs have unmeasured test strength.** Five of the six
   swept modules have known survivors and no fixes. A green suite over those modules is not evidence.
5. **Every vendor field name is invented.** 76 gaps. First contact with a real ServAssure NXT,
   jTrack or WFM will move field names, and the adapters are where that lands.
6. **Redaction and prompt-injection screening are the least covered modules in the tree.** They are
   the two whose failure is a privacy incident rather than an outage, and they are the two the
   coverage figure is thinnest over.
7. **`simulated: True` is returned even when the write gate permits the write.** Deliberate — a
   fixture-backed adapter never opens a TR-069 session — but it means "did this really happen?" must
   be read from `result["gate"]["permitted"]`, and a reader who checks the obvious field will get
   the wrong answer.
8. **No language model is wired.** D6 and D7 describe a narrative model behind a Protocol; neither
   exists, so every operator-facing narrative today is a template.

---

## 7. How to reproduce this report

```bash
make audit
```

Writes `audit/latest/*.txt` and `audit/MANIFEST.json`, and exits non-zero if any gate failed. The
suite then checks this document against what it wrote.
