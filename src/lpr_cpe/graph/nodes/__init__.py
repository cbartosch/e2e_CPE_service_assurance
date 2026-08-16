"""The parent graph's eleven nodes, in the order the specification numbers them.

`PARENT_NODES` is the registry `graph.builder` iterates. It is a tuple of `(name, callable)` pairs
rather than a dict for one reason: the builder adds the nodes and then wires the linear edges
between consecutive entries, so the order *is* data. A dict would preserve insertion order in
CPython and say nothing about whether that order was intended.

The name in each pair is the LangGraph node name and is also what the `@node` decorator stamps into
every `AuditEvent` and `KPIEvent` the node emits. They are checked against each other on import
below, because a rename that updated one and not the other produces an audit trail that names a
node the graph does not contain -- which is invisible until somebody tries to trace an incident.

Stage boundaries, for the reader following the specification:

* **P01-P06** `intake` -- one signal becomes one incident.
* **P07-P09** `evidence` -- assemble, plan, test. Read-only throughout.
* **P10-P11** `diagnosis` -- name the fault, offer the remedies.

Nothing beyond P11 lives here. Stage 3 onwards is subgraphs, because every stage past this point
contains an interrupt and an interrupt inside the parent would checkpoint the parent's state at a
point where a subgraph's is what needs resuming.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from lpr_cpe.graph.nodes._runtime import check_node_registry
from lpr_cpe.graph.nodes.diagnosis import (
    DIAGNOSIS_NODES,
    determine_root_cause,
    generate_resolution_options,
)
from lpr_cpe.graph.nodes.evidence import (
    EVIDENCE_NODES,
    assemble_case_evidence,
    create_diagnostic_test_plan,
    execute_read_only_tests,
)
from lpr_cpe.graph.nodes.intake import (
    INTAKE_NODES,
    assess_impact_and_priority,
    create_or_attach_incident,
    deduplicate_and_correlate,
    normalize_event,
    receive_signal,
    resolve_identity_and_topology,
)

#: Every parent node, P01 to P11, in specification order.
PARENT_NODES: Sequence[tuple[str, Any]] = (*INTAKE_NODES, *EVIDENCE_NODES, *DIAGNOSIS_NODES)


check_node_registry(PARENT_NODES, "the parent node registry")


__all__ = [
    "DIAGNOSIS_NODES",
    "EVIDENCE_NODES",
    "INTAKE_NODES",
    "PARENT_NODES",
    "assemble_case_evidence",
    "assess_impact_and_priority",
    "create_diagnostic_test_plan",
    "create_or_attach_incident",
    "deduplicate_and_correlate",
    "determine_root_cause",
    "execute_read_only_tests",
    "generate_resolution_options",
    "normalize_event",
    "receive_signal",
    "resolve_identity_and_topology",
]
