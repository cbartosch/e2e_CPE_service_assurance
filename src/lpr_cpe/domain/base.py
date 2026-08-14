"""Shared model behaviour and identifier construction.

Two things every model in this package inherits:

* **`extra="forbid"`.** A typo'd field name on a model that silently accepts extras is a value that
  is set, readable in a debugger, and never used by anything. Forbidding extras turns that into an
  error at construction.
* **`frozen=True` on records that have left the system.** Anything that has been *filed* -- an
  action, a decision, an audit event -- is immutable, because state uses append-only reducers for
  exactly those and a mutable record would let a later node edit history in place while the reducer
  believed it was appending.

Identifiers are constructed here rather than in each node, and are deterministic wherever
determinism is available: `idempotency_key` is a hash of what makes the action unique, so two nodes
that independently decide to reboot the same CPE for the same incident produce the same key and the
second call is a no-op at the adapter instead of a second reboot.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base for mutable operational objects."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        ser_json_timedelta="float",
        str_strip_whitespace=True,
    )


class FrozenDomainModel(DomainModel):
    """Base for records that are filed and never edited."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=False,
        str_strip_whitespace=True,
    )


def new_incident_id() -> str:
    """`INC-` plus a UUID4 hex. Used as the LangGraph `thread_id` unchanged (D1)."""
    return f"INC-{uuid.uuid4().hex}"


def new_correlation_id() -> str:
    return f"COR-{uuid.uuid4().hex[:16]}"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def idempotency_key(
    incident_id: str,
    action_type: str,
    target_ref: str,
    attempt: int = 1,
    *,
    extra: str = "",
) -> str:
    """A stable key for one intended effect.

    Deterministic in its inputs so a retry, a node replay after an `interrupt()`, or two nodes
    reaching the same conclusion all produce the same key. `attempt` is what makes a *deliberate*
    second reboot distinguishable from an accidental duplicate: the caller must increment it on
    purpose, so an accidental repeat cannot.

    Truncated to 32 hex characters, which is a collision risk of roughly 2^-128 over the inputs
    actually used here (one incident's actions) and short enough to pass through vendor fields that
    cap at 64 characters.
    """
    material = "\x1f".join((incident_id, action_type, target_ref, str(attempt), extra))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def evidence_ref(kind: str, subject: str, observed_at: datetime) -> str:
    """A short, stable reference for a piece of evidence.

    Evidence is de-duplicated on this, so two detectors that both cite the same NXT alarm produce
    one evidence item with two referrers rather than two identical items.
    """
    material = f"{kind}\x1f{subject}\x1f{observed_at.isoformat()}"
    return f"EV-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def object_reference(bucket: str, key: str) -> dict[str, Any]:
    """A pointer to a blob that must NOT be in graph state.

    Spectrum captures, PDF reports and technician photos live in object storage; state carries this
    dict. The specification requires it, and the practical reason is that graph state is
    checkpointed on every super-step -- a 4 MB capture in state is 4 MB written per step, per
    incident.
    """
    return {"bucket": bucket, "key": key, "kind": "object_reference"}
