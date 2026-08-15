"""The checkpoint serialiser, and the allowlist of types it is permitted to reconstruct.

Graph state holds Pydantic models and enums -- `ApprovalRequest`, `IncidentStatus`, `WorkOrder`.
LangGraph's msgpack serialiser reconstructs those on the way out of a checkpoint only for types on
an allowlist. Types not on it are **silently degraded to their primitive form**, not rejected:

    measured on langgraph 1.2.11, mismatched allowlist, permissive mode
      pending_approval : ApprovalRequest  ->  dict
      status           : IncidentStatus   ->  str

No exception, no failed invocation. The graph resumes and runs on, and the failure surfaces later
and elsewhere -- an `AttributeError` on `state["status"].value` inside some node, or, worse, no
error at all: `advance_status` comparing a `str` against an `IncidentStatus` finds no match and the
lifecycle check silently stops guarding anything.

That failure mode drives two decisions here.

**The allowlist is derived, not typed out.** `lpr_cpe.domain.__all__` already enumerates every model
and enum the state contract is built from -- 42 models and 25 enums as of this writing. Deriving the
allowlist from it means adding a domain model cannot leave a gap. A hand-maintained list would have
exactly one failure mode, omission, and omission is the one this module exists to prevent.

**Classes are passed, not strings.** The allowlist accepts `(module, name)` tuples or classes.
Tuples are string literals that a rename does not update and no checker verifies -- and getting one
wrong produces the silent degradation above rather than an error. Passing the class object makes a
rename a rename and a typo an `ImportError`.

The default is left permissive rather than forced strict. `LANGGRAPH_STRICT_MSGPACK` is a global
switch over every serialiser in the process, including LangGraph's own internals, so setting it here
would change behaviour a caller did not ask for. The allowlist achieves the same result for the
types this system owns.
"""

from __future__ import annotations

import enum

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

import lpr_cpe.domain as domain


def allowlisted_types() -> tuple[type, ...]:
    """Every domain model and enum that may appear in a checkpoint, derived from the public API.

    Sorted by qualified name so the tuple is stable across runs -- it ends up in a serialiser whose
    behaviour should not depend on dict ordering, and a stable order makes a diff of "what became
    checkpointable" readable.
    """
    found = [
        obj
        for name in domain.__all__
        if isinstance(obj := getattr(domain, name), type) and issubclass(obj, BaseModel | enum.Enum)
    ]
    return tuple(sorted(found, key=lambda t: f"{t.__module__}.{t.__qualname__}"))


def build_serde() -> JsonPlusSerializer:
    """The serialiser every checkpointer in this system is constructed with.

    One function rather than a module constant: `JsonPlusSerializer` holds mutable state for its
    warn-once bookkeeping, and two checkpointers sharing one instance is a coupling nothing here
    needs.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=list(allowlisted_types()))
