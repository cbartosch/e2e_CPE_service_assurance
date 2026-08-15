"""Deterministic field-schedule optimisation: P15, and the evidence D14 needs to route on.

The specification is unusually specific about this package, in two ways worth restating because
they shape every module in it.

**No language model chooses a schedule.** P15 says so outright. Nothing here calls a provider, and
nothing here should: a schedule is a constrained assignment problem with an auditable objective, and
a model asked to produce one would be generating plausible-looking crew names.

**A refusal is a deliverable.** D14 does not ask whether a plan exists, it asks which constraint is
binding, and forbids committing an infeasible slot. So `DispatchPlan.unassigned` is a first-class
output rather than an error path, `DispatchPlan` refuses at construction to carry an unexplained
one, and every explanation opens with a `ConstraintCode` a router can read.

## The split

* `travel` -- how long the journey takes, and whether that number was routed or estimated. The one
  place the three travel terms (drive, access overhead, ferry) are combined.
* `constraints` -- the twelve hard constraints the specification names, one function each, each
  returning a coded reason.
* `objective` -- the six weighted terms from `dispatch.objective_weights`, kept separate so a plan
  can be argued with rather than merely read.
* `optimizer` -- the queue, the search, and the plan.

## Facts this package does not own

Travel is the sharp one. `integrations.base.GISAdapter.travel_minutes` answers point-to-point
journeys and is a routing engine in production; the pack's `dispatch.archetype_*` numbers are the
fallback for when it is unavailable or coordinates are missing. Both models exist on purpose, they
are held to the same *shape*, and `TravelEstimate.basis` records which answered -- because a
schedule costed on a straight-line average and one costed on a road network look identical and only
one is a reason to promise a customer an arrival time.

The others are carried in on `JobContext` rather than recomputed: SLA standing from
`decision_services.sla.sla_status`, affected customers from `decision_services.blast_radius`, wind
from the GIS adapter. A second SLA calculation here would eventually disagree with the one the
escalation queue is sorted by, and both would be called "time remaining".

## What belongs here and what does not

Whether to dispatch at all is not this package's question -- by P15 that is settled, and D13 has
already chosen the crew type. This package answers *who and when*, and reports what stopped it.
Approval is `policies.engine`'s, and committing the slot is P16's.

Nothing is re-exported. The four modules name different things and a flat namespace would invite
`from lpr_cpe.dispatch import check_skill` at a call site that has no business reaching past the
optimizer.
"""

from __future__ import annotations

__all__: list[str] = []
