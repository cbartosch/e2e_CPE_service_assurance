# LPR CPE Service Assurance

A predictive CPE service-assurance workflow for a cable operator running **both** HFC and PON access,
built as a LangGraph state machine with human approval gates that survive a process restart.

The system watches customer premises equipment, finds trouble before the customer calls, decides
whether the fault is in the home or in the plant, tries to fix it remotely, and only sends a
technician when it must -- and it can prove, afterwards, why it made each of those choices.

> **Status: in progress.** See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §5 for what is built
> and §6 for what is not. That file is the honest status document; this one is the map.

## Why this is shaped the way it is

Three decisions explain most of the code.

**Deterministic decisions are Python; the language model writes prose.** Every anomaly score, health
band, verdict, blast-radius count, dispatch assignment and SLA deadline is ordinary arithmetic. The
model's only job is customer-facing wording and technician narrative, and its output is never read
back as a decision. This is not distrust for its own sake -- it is what makes an incident from six
months ago reproducible, and it is why the same input yields the same routing every time. Note the
direction the gap runs in: no module calls a model provider yet, so today the deterministic half is
all there is.

**A node re-runs from its start when an approval resumes.** This is measured LangGraph behaviour, not
a guess (see IMPLEMENTATION_PLAN.md §2). Everything downstream follows from it: counters are absolute
and reduced with `max` rather than incremented, collections are append-only and de-duplicated on a
natural key, the SLA clock is write-once, and no non-idempotent external write shares a node with an
`interrupt()`.

**One owner per fact.** Whether a write may leave the process is answered in exactly one place
(`integrations.base.WriteGate`). Which status may follow which is one table
(`domain.lifecycle.TRANSITIONS`). Whether a handover is complete is one method
(`HandoverContract.missing_items()`). Whether an incident may close is a validator on
`ClosureRecord`, so "proof before closure" is a type error rather than a code review comment.

## Layout

| Path | What lives there |
| --- | --- |
| `src/lpr_cpe/config/` | Settings, the two write switches, the one clock, the access-network defaults |
| `src/lpr_cpe/domain/` | The validated record types and the incident lifecycle table |
| `src/lpr_cpe/graph/` | The state contract, its reducers, the parent graph and its subgraphs |
| `src/lpr_cpe/detectors/` | The baseline detectors. No model calls inside any of them |
| `src/lpr_cpe/decision_services/` | Classification, Wi-Fi scoring, blast radius, delimiter resolution, SLA arithmetic |
| `src/lpr_cpe/policies/` | The YAML policy pack and the fail-closed engine that reads it |
| `src/lpr_cpe/dispatch/` | The greedy dispatch optimizer, and the seam a solver would slot into |
| `src/lpr_cpe/integrations/` | Adapter protocols, the write gate, and one simulator per external system |
| `src/lpr_cpe/simulation/` | The fixture network the simulators read from |
| `src/lpr_cpe/persistence/` | The checkpointer factory and the state serde |
| `src/lpr_cpe/security/` | Redaction, prompt-injection neutralisation, role-based tool allowlists |
| `src/lpr_cpe/observability/` | Structured logging, tracing attributes, KPI derivation |
| `src/lpr_cpe/cli.py` | The `lpr-cpe` console script: compile the graph, report topology and config |
| `src/lpr_cpe/audit.py` | The `lpr-cpe-audit` console script: run every gate, write the evidence bundle |
| `src/lpr_cpe/runner.py` | What `lpr-cpe run` calls: drive one incident, answering every pause |
| `docs/` | The vendored specification, the gap register, the workflow diagrams, the implementation report |
| `audit/` | The last bundle: each gate's raw output, and `MANIFEST.json` |

Three things this table would otherwise be expected to list are **not written yet**. They are named
here rather than omitted, so that their absence reads as a gap and not as a table nobody updated:
`src/lpr_cpe/api/` (the HTTP surface, approval resume and the inbound webhooks), the transactional
outbox and migrations under `persistence/`, and seven of the nine documents `docs/` is eventually to
hold -- the decision tables, the runbook, the KPI definitions and the decision records.
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §5 tracks each one.

This paragraph said **four** things until 2026-08-24, and the fourth was "the six resolution
subgraphs past the fork in `graph/`". All nine subgraphs are built and all 24 decisions are wired;
that entry had been wrong since the plant branch landed on the 21st. It is corrected here rather
than deleted, because a stale gap list is the specific failure this paragraph exists to prevent and
it is worth one line admitting the list did it too.

## Setup

Requires Python 3.12 or newer. No external service is needed for the default path -- the
checkpointer runs in memory and every adapter is fixture-backed.

```bash
python -m pip install -e ".[dev]"
cp .env.example .env
```

Optional extras, each of which the code is written to survive the absence of:

```bash
python -m pip install -e ".[dev,postgres]"    # durable checkpoints; needs libpq
python -m pip install -e ".[dev,otel]"        # OpenTelemetry export; no-ops when absent
```

Two further extras are declared and **change nothing if installed today**, because the code behind
each is a seam rather than an implementation. They are named here so that installing one and
observing no difference reads as a known gap:

```bash
python -m pip install -e ".[dev,optimizer]"   # OR-Tools; select_optimizer still returns greedy
python -m pip install -e ".[dev,anthropic]"   # no module calls a model provider yet
```

`dispatch.optimizer.select_optimizer` discards its `prefer_solver` argument and returns the greedy
optimizer either way -- deliberately, because it is a dispatcher's infeasibility reporting rather
than its optimisation that an unexercised solver path gets wrong (gap DISPATCH-1 in
[docs/vendor-integration-gaps.md](docs/vendor-integration-gaps.md)). `ModelProvider` is an enum in
`config.settings` with no provider behind it; the deterministic fake described above is
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §5's `model provider + deterministic fake` row,
still pending.

## Running it

```bash
make check         # ruff check, ruff format --check, mypy --strict, then the suite
make audit         # all of the above, captured into audit/ with a manifest
make test-fast     # the full suite, without coverage instrumentation
```

`make check` passes, coverage gate included. That gate is worth one sentence of history: until
2026-08-24 `--cov-fail-under=85` compared the total *rounded to `precision`*, which defaults to 0,
so the effective bar was 84.5% and a run at 84.92% exited zero while printing `FAIL ... not
reached`. `precision = 2` is now set and the gate means what it says.

Every target is a thin wrapper over the commands its recipe shows, and [Makefile](Makefile) is their
only owner, so where `make` is unavailable -- a plain Windows shell, for instance -- run those
commands directly. `make help` lists every target.

### The audit bundle

`make audit` runs all six gates, writes each one's output verbatim to `audit/latest/`, and records
the commit, the interpreter, the pinned package versions and the derived figures in
[audit/MANIFEST.json](audit/MANIFEST.json). It exits non-zero if any gate failed, and it produces a
bundle either way -- a bundle that only existed when everything was green would be useless at the
one moment anybody needed it.

[docs/implementation-report.md](docs/implementation-report.md) is the specification's deliverable 17
and reads off that manifest. It is the only document here whose figures a test checks: state a
number it did not measure and the suite goes red.

### Running one incident

```bash
lpr-cpe run SVC-UT-001-B-01
```

Drives one fixture service from event to standstill against the simulators, answering every
approval gate, crew report, customer window and plant report itself, and prints what the run
produced. `make demo` is this command; `DEMO_SERVICE=...` picks another service, `--decline`
refuses every approval instead of granting it, and `--predictive` files the event as predictive
maintenance so it takes D04's preventive arm.

Swept across all 41 fixtures it closes 3 and escalates 38, which is the honest shape of the system
today rather than a demonstration tuned to look complete. It refuses to start if
`LPR_ALLOW_PRODUCTION_WRITES` is set.

**It is not the seventeen specification scenarios.** Those are still unwritten; this is one scripted
path, and the answers it gives are a script rather than a simulation of an operator.

The other two commands report and change nothing:

```bash
lpr-cpe topology   # compile the parent graph, then print its nodes, decisions and where runs end
lpr-cpe config     # the settings this process would run under, the safety switches first
lpr-cpe            # both
```

Compiling *is* the check. The parent graph refuses to build when its three topology tables disagree
with each other or with the node registry, so `lpr-cpe topology` fails on that without needing a
database, a network or a model provider.

`make serve` is declared and not yet runnable, because the HTTP surface is unwritten. It names what
is missing, points at the row in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §5 that tracks it,
and exits non-zero -- rather than failing on the import of something that was never there, which
reads as a broken install. `make demo` used to do the same and now runs `lpr-cpe run`.

## Writes are off by default

No adapter writes to an external system unless **both** switches are set:

```bash
LPR_APP_MODE=production
LPR_ALLOW_PRODUCTION_WRITES=true
```

One switch alone is not enough, deliberately. A staging deployment that inherits a production
`.env` still cannot dispatch a technician, and a production deployment mid-rollout still cannot
until someone turns writes on explicitly. `Settings.writes_permitted` is the only place that
question is answered, and `WriteGate` is the only thing that asks it.
