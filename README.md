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
model is asked for customer-facing wording and technician narrative, and its output is never read
back as a decision. This is not distrust for its own sake -- it is what makes an incident from six
months ago reproducible, and it is why the same input yields the same routing every time.

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
| `src/lpr_cpe/domain/` | The validated record types and the incident lifecycle table |
| `src/lpr_cpe/graph/` | The state contract, its reducers, the parent graph and its subgraphs |
| `src/lpr_cpe/detectors/` | The baseline detectors. No model calls inside any of them |
| `src/lpr_cpe/decision_services/` | Classification, Wi-Fi scoring, blast radius, delimiter resolution, SLA arithmetic |
| `src/lpr_cpe/policies/` | The YAML policy pack and the fail-closed engine that reads it |
| `src/lpr_cpe/dispatch/` | The dispatch optimizer and its greedy fallback |
| `src/lpr_cpe/integrations/` | Adapter protocols, the write gate, and one simulator per external system |
| `src/lpr_cpe/simulation/` | The fixture network the simulators read from |
| `src/lpr_cpe/persistence/` | Checkpointer factory and the transactional outbox |
| `src/lpr_cpe/security/` | Redaction, prompt-injection neutralisation, role-based tool allowlists |
| `src/lpr_cpe/observability/` | Structured logging, tracing attributes, KPI derivation |
| `src/lpr_cpe/api/` | The HTTP surface, including approval resume and the inbound webhooks |
| `docs/` | Diagrams, decision tables, the runbook, and the architecture decision records |

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
python -m pip install -e ".[dev,optimizer]"   # OR-Tools dispatch; falls back to greedy without it
python -m pip install -e ".[dev,anthropic]"   # real model calls; a deterministic fake is the default
python -m pip install -e ".[dev,otel]"        # OpenTelemetry export; no-ops when absent
```

## Running it

```bash
make demo          # the demonstration scenarios end to end, against fixtures
make test          # the full suite with the coverage gate
make lint          # ruff
make typecheck     # mypy --strict
make check         # lint, typecheck and test
make serve         # the API on http://127.0.0.1:8000
```

Exact per-scenario commands are listed by `lpr-cpe --help` and in
[docs/operations-runbook.md](docs/operations-runbook.md).

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
