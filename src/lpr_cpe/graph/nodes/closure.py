"""P23, confirm the customer outcome: does the customer agree the service is fixed?

One node, and it is a *parent* node rather than part of `subgraphs.restoration_validation`, which
is not where it would naturally sit. `graph.builder._terminal_targets` is why. D21 is asked on the
parent's edge out of the validation subgraph because two of its three answers leave that graph, and
D22 has to be asked after this node -- but D21 and D22 both answer `retry_diagnosis`, and chaining
two decisions that share an answer is refused at build time: one conditional edge would carry both,
so the branch could no longer name which question was asked. Putting P23 inside the subgraph would
force exactly that chain. It sits here instead, between the two decisions, where each has a node of
its own to hang from.

What "confirm" means here, and what it does not
-----------------------------------------------
The specification's rule is *"require customer confirmation only where telemetry and service tests
cannot establish the actual customer experience"*, and the policy pack already implements that rule
by name: `validation.require_customer_confirmation_for_domains` lists the fault domains where the
readings cannot see what the customer is complaining about. This node reads that list rather than
re-deciding it, so the pack stays the one owner of when a customer's word is needed.

It **records** an answer; it does not send a message. That is a deliberate boundary and not an
oversight, so it is worth being exact about what is missing. Contacting a customer in this codebase
means an `ActionRequest` carrying a `PolicyDecision`, because `NOTIFY_CUSTOMER` is one of
`policies.engine.CUSTOMER_CONTACT_ACTIONS` and the pack caps how often it may happen -- and every
existing contact is built from a `ResolutionOption` through `_shared.policy_input_for`. Stage 5 has
no `ResolutionOption`: nothing is being resolved any more. Synthesising one to satisfy the signature
would put a fabricated option in `resolution_plan` and let this node contact a customer without the
cap that governs every other contact. That seam is the same one the preventive branch waits on; see
`builder.PENDING_STAGES` and docs/vendor-integration-gaps.md. The specification's English and
Spanish templates already exist in the communications simulator and are reached through that seam,
not through a second table here.

So the outbound half is owed. The inbound half is not, and is what this node does: an answer the
customer has already given -- through the self-help channel, or to a technician, or out of band --
is read from the communications adapter and written where `route_resolution` looks for it.
"""

from __future__ import annotations

from typing import Any

from lpr_cpe.domain.enums import FaultDomain
from lpr_cpe.graph.context import GraphContext
from lpr_cpe.graph.nodes._runtime import (
    NodeUpdate,
    audit,
    check_node_registry,
    node,
)
from lpr_cpe.graph.nodes.evidence import subject_of
from lpr_cpe.graph.state import IncidentState

#: Replies that answer *"is your service working now?"*, which is not the question
#: `self_help.customer_reply` reads. That one asks whether the customer carried out a step, and a
#: customer who did the reboot they were asked to do has not thereby said the fault is gone. Two
#: questions, two vocabularies, deliberately not shared.
_CONFIRMS = frozenset({"confirmed", "fixed", "resolved", "working", "yes"})
_DENIES = frozenset({"denied", "not_fixed", "still_broken", "no"})


def confirmation_required(state: IncidentState, ctx: GraphContext) -> bool:
    """Whether this incident's fault domain is one telemetry cannot speak for.

    Read from the pack rather than decided here, and read with the same default
    `assess_restoration` uses, so that the node which asks and the validator which judges cannot
    disagree about whether an answer was needed.
    """
    domain = state.get("fault_domain", FaultDomain.UNKNOWN)
    return domain in ctx.policy.pack.validation.require_customer_confirmation_for_domains


def customer_verdict(rows: list[dict[str, Any]]) -> bool | None:
    """The customer's answer about the service, or `None` if they have not given one.

    `None` and `False` are kept apart all the way down: `route_resolution` sends a `False` back to
    diagnosis and lets a `None` through, because a customer who was never asked is not a customer
    who said no. Collapsing them here would resolve nothing and re-diagnose every incident nobody
    needed to phone.

    The **first** understood row wins, because `fetch_customer_responses` returns newest first and a
    customer who says "still broken" and then "actually it's fine" has changed their mind. That
    ordering is not in the `CommunicationsAdapter` protocol, which says only `list[dict]`, and this
    is the second reader to depend on it -- `self_help.await_customer_response` takes the first
    matching row for the same reason. `test_nodes.py` pins it against the simulator so the
    assumption both readers make has one place that can go red.
    """
    for row in rows:
        reply = str(row.get("response") or "").strip().lower()
        if reply in _CONFIRMS:
            return True
        if reply in _DENIES:
            return False
    return None


@node("confirm_customer_outcome")
async def confirm_customer_outcome(state: IncidentState, ctx: GraphContext) -> NodeUpdate:
    """Record what the customer says about the restored service, for D22 to route on.

    Reached only from D21's `confirm_outcome`, so `validation` exists and has passed. The record is
    revised rather than rebuilt: `validate_restoration` owns every other field on it, and a second
    constructor here would be a second opinion about a window this node did not observe.

    A `False` is written onto a record whose `passed` is `True`, which reads like a contradiction
    and is the point. The telemetry did pass; the customer disagrees; `route_resolution` exists to
    say which of those wins. Overwriting `passed` here would destroy the evidence that they
    disagreed.
    """
    validation = state.get("validation")
    if validation is None:
        raise ValueError(
            "confirm_customer_outcome was reached with no validation record. Only D21's "
            "`confirm_outcome` leads here, and `route_stability` gives that answer only for a "
            "validation that passed."
        )

    subject = subject_of(state)
    required = confirmation_required(state, ctx)
    rows = await ctx.adapters.communications.fetch_customer_responses(subject.incident_id)
    verdict = customer_verdict(rows)

    update: NodeUpdate = {
        "audit_events": [
            audit(
                state,
                ctx,
                node="confirm_customer_outcome",
                action="confirm_customer_outcome",
                outcome=(
                    "confirmed"
                    if verdict is True
                    else "denied"
                    if verdict is False
                    else "not_answered"
                    if required
                    else "not_required"
                ),
                subject_ref=subject.service_ref or subject.incident_id,
                detail={
                    "confirmation_required": required,
                    "customer_confirmed": verdict,
                    "responses_read": len(rows),
                    "validation_id": validation.validation_id,
                },
            )
        ],
    }
    if verdict is not None and verdict != validation.customer_confirmed:
        update["validation"] = validation.model_copy(update={"customer_confirmed": verdict})
    return update


#: P23, alone. Its own registry rather than an entry appended to another module's, because
#: `PARENT_NODES` composes these tuples in order and `builder._plain_edges` reads that order as the
#: linear edges -- so where this lands is wiring, not filing.
#:
#: It goes **before** `GOVERNANCE_NODES`, which ends with `record_escalation`. That node is declared
#: in `builder.DELIBERATE_TERMINALS`: nothing follows an escalation, and it is terminal only
#: because it is last. Appending Stage 5 after it would draw `record_escalation` ->
#: `confirm_customer_outcome` and quietly resume an incident a human had been handed.
CLOSURE_NODES: tuple[tuple[str, Any], ...] = (
    ("confirm_customer_outcome", confirm_customer_outcome),
)

check_node_registry(CLOSURE_NODES, "the closure node registry")


__all__ = [
    "CLOSURE_NODES",
    "confirm_customer_outcome",
    "confirmation_required",
    "customer_verdict",
]
