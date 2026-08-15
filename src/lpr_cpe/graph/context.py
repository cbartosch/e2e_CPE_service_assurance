"""The dependencies a node is handed at runtime, and the one place they are assembled.

Nodes need adapters, a policy engine, a clock and settings. None of those belong in `IncidentState`:
state is checkpointed on every super-step and restored hours later from Postgres, so an open socket
or a live `PolicyEngine` in it is either unserialisable or -- worse -- serialisable and stale. The
split is the rule this module exists to enforce:

* **State is what happened.** Serialisable, replayable, and identical when re-read next week.
* **Context is what we happen to be talking to.** Rebuilt per process, never checkpointed.

LangGraph's `context_schema` is exactly this distinction, so it is what carries the bundle. A node
reaches it with `get_runtime(GraphContext).context` rather than through a closure over a module
global, which keeps two graphs with different adapters usable in the same process -- the arrangement
every test that wants its own `WriteGate` depends on.

**The clock is here and not in state on purpose, and it is the subtle one.** A node must never call
`datetime.now()`, but the reason is not tidiness: a resumed run re-executes nodes, and a deadline
computed from "now" would move every time the incident was resumed. So the clock is used to
*stamp* facts as they are observed, and those stamps go into state; nothing downstream re-derives a
deadline by asking the clock again. `sla_clock_started_at` is written once at intake (D1) and every
later deadline is arithmetic on that stored value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lpr_cpe.config.clock import Clock, SystemClock
from lpr_cpe.config.settings import Settings, get_settings
from lpr_cpe.policies.engine import PolicyEngine
from lpr_cpe.security.rbac import Role

if TYPE_CHECKING:
    from lpr_cpe.simulation.loader import SimulatedAdapters


@dataclass(frozen=True, slots=True)
class GraphContext:
    """Everything a node may reach for that is not a recorded fact.

    Frozen: a node that mutated shared context would be writing state through the side door, where
    no reducer can make it replay-safe and no checkpoint records it. Anything a node needs to
    *change* goes in its return mapping.

    `adapters` is typed as the Protocol container, not the simulator, so the production wiring is a
    substitution rather than an edit.
    """

    settings: Settings
    clock: Clock
    policy: PolicyEngine
    adapters: SimulatedAdapters

    #: Who the graph acts as when no human is in the loop. Every automated action carries this in
    #: its `ActionRequest.actor`, because `policies.engine` blocks an action with no actor as
    #: *unattributable* -- a distinct outcome from *unpermitted*, and one no node should be able to
    #: trigger by omission.
    automation_actor: str = "lpr-cpe-automation"
    automation_role: Role = Role.AUTOMATION

    #: Overrides for the bounded-loop guard. Empty in production, where `settings` supplies the
    #: numbers; a scenario test lowers them here to prove the guard fires without running 60 real
    #: super-steps. Kept separate from `settings` so that lowering a bound in a test cannot leak
    #: into the settings singleton other tests read.
    step_budget_override: int | None = None
    node_visit_budget: dict[str, int] = field(default_factory=dict)

    @property
    def max_graph_steps(self) -> int:
        return self.step_budget_override or self.settings.max_graph_steps

    @property
    def max_diagnostic_cycles(self) -> int:
        return self.settings.max_diagnostic_cycles


def build_context(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    policy: PolicyEngine | None = None,
    adapters: SimulatedAdapters | None = None,
    step_budget_override: int | None = None,
    node_visit_budget: dict[str, int] | None = None,
) -> GraphContext:
    """Assemble a context, defaulting each dependency to its simulation-mode implementation.

    Every argument is injectable because every one of them is something a test needs to control:
    the clock to make deadlines deterministic, the gate inside `adapters` to assert no write
    escaped, the policy engine to point at a modified pack.

    The import of `build_simulated_adapters` is deferred to call time rather than placed at module
    scope. `simulation.loader` reads fixture files when it is imported, and this module is imported
    by every node module; a top-level import would make the fixture set a hard dependency of merely
    *describing* the graph, including for a production process that will never use it.

    The policy engine defaults to `PolicyEngine.load()`, which **raises** on an unusable pack rather
    than to `load_or_unavailable()`, which degrades to blocking everything. Building a context is a
    start, and the engine's own contract is that a starting service should refuse to boot with an
    invalid pack -- the degraded engine exists for a *reload* in a process already serving. Getting
    this backwards would turn a typo in the YAML into a run of incidents blocked one at a time.

    The clock is passed into the engine rather than letting it construct its own, so that a decision
    made at a frozen instant stamps that instant. Two clocks in one graph is how a `PolicyDecision`
    comes to be timestamped after the action it authorised.
    """
    from lpr_cpe.simulation.loader import build_simulated_adapters

    resolved_settings = settings or get_settings()
    resolved_clock = clock or SystemClock(resolved_settings.timezone)
    return GraphContext(
        settings=resolved_settings,
        clock=resolved_clock,
        policy=policy or PolicyEngine.load(clock=resolved_clock),
        adapters=adapters or build_simulated_adapters(clock=resolved_clock),
        step_budget_override=step_budget_override,
        node_visit_budget=dict(node_visit_budget or {}),
    )
