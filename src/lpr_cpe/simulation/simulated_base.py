"""Shared plumbing for the ten simulators: the write path, the idempotency ledger, availability.

Why this is one class rather than a paragraph repeated ten times: the write path has exactly three
obligations -- ask the gate, honour the verdict, apply the effect at most once per idempotency key
-- and an adapter that gets the third one wrong sends a second work order to a crew. Ten copies of
that logic is nine chances to drop one obligation, and the copy that drops it is the one with no
test.

It lives under `simulation/` rather than in `integrations/base.py` because it is simulator
behaviour, not contract: a real HTTP adapter would inherit the obligations and none of this code
(its idempotency ledger is the vendor's, not a local dict). `integrations.base` stays the file that
describes what an adapter *is*.

**Read-miss policy, one decision for all ten adapters.** An unknown reference is handled by which
kind of read it is, and the kind is a property of the question, not of the adapter:

* A read about a **subject the workflow believes exists** -- a service, CPE, delimiter, node, OLT,
  customer or area reference that arrived from an alarm or from inventory -- raises
  `AdapterUnavailableError`. "That service is not in my records" is not an answer to "what is this
  service's RF level"; it means two systems disagree about a customer, and that has to reach
  `DataQualityAssessment` rather than being absorbed as an empty reading.
* A **query over a collection** -- alarms since a time, recent changes, open MRs, crew availability,
  outages nearby, customer responses -- returns an empty list. "Nothing matched" is a real and
  common answer, and raising would make a quiet night look like an outage.
* A read of a **record this process created** -- a work order, an MR -- returns an explicit
  `{"found": False, ...}`. In simulation these exist only if a write in this same process created
  them, so a miss after a restart is expected rather than a disagreement.

Every method's docstring restates which of the three it is, because the choice is only defensible if
a reader can see it without reading this file.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from lpr_cpe.config.clock import Clock
from lpr_cpe.domain.enums import ActionOutcome
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterUnavailableError, WriteGate, WriteVerdict

if TYPE_CHECKING:
    # Type-checking only, and load-bearing: `loader` imports the ten simulators, which import this
    # module. A runtime import of `Fixtures` here would close that loop and leave whichever module a
    # process reached first half-initialised. `from __future__ import annotations` makes the
    # annotations below strings, so mypy sees the real type and the interpreter never needs it.
    from lpr_cpe.simulation.loader import Fixtures


class SimulatedAdapterBase:
    """Base for every `Simulated*Adapter`: holds the fixtures, clock, gate and write ledger."""

    #: Provenance string. Appears on every `EvidenceItem.source_system` this adapter produces and in
    #: audit events, so it is stable and lowercase -- it is compared against, not displayed.
    system_name: str = "simulated"

    #: Prefix for the external references this adapter hands back ("MR", "WO", ...).
    external_ref_prefix: str = "SIM"

    def __init__(self, fixtures: Fixtures, clock: Clock, gate: WriteGate) -> None:
        self._fixtures = fixtures
        self._clock = clock
        self._gate = gate
        # idempotency_key -> the first result produced for that key. The whole point of the
        # simulator's write path: a repeat returns this, it does not build a second one.
        self._ledger: dict[str, dict[str, Any]] = {}
        self._unavailable_reason: str | None = None

    # -- availability ----------------------------------------------------------------------------

    async def health(self) -> bool:
        """True unless a test has deliberately taken this adapter down.

        A simulator whose health is hard-coded `True` cannot exercise the branch that matters --
        `DataQualityFlag.ADAPTER_UNAVAILABLE` and the `CircuitBreaker` -- so this one can go red.
        """
        return self._unavailable_reason is None

    def simulate_unavailable(self, reason: str = "simulated outage") -> None:
        """Take this adapter down. Subsequent reads raise and `health()` returns False."""
        self._unavailable_reason = reason

    def simulate_recovered(self) -> None:
        self._unavailable_reason = None

    def _ensure_available(self) -> None:
        if self._unavailable_reason is not None:
            raise AdapterUnavailableError(self.system_name, self._unavailable_reason)

    # -- writes ----------------------------------------------------------------------------------

    @property
    def recorded_writes(self) -> tuple[dict[str, Any], ...]:
        """Every distinct effect this adapter has recorded, in insertion order."""
        return tuple(self._ledger.values())

    def external_ref_for(self, request: ActionRequest, prefix: str | None = None) -> str:
        """A plausible vendor reference, derived from the idempotency key.

        Derived rather than random so that a replay reports the *same* reference. A fresh
        `uuid4()` here would mean the caller saw two references for one effect and had no way to
        tell they were the same thing -- which is precisely the confusion idempotency exists to
        prevent. The format is ours; a real system's reference format is an open question.
        """
        digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:8].upper()
        return f"{prefix or self.external_ref_prefix}-{digest}"

    def simulate_write(
        self,
        request: ActionRequest,
        *,
        detail: str,
        prefix: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The only write path in the simulator. Ask the gate, honour it, apply once.

        Order is deliberate and is asserted on: `gate.authorize()` runs **first**, before the
        idempotency ledger is consulted, so the gate's record is a complete log of every write the
        process *intended* -- including the duplicate ones. Consulting the ledger first would hide
        replayed attempts from the audit trail, and "how often do we try to send the same work order
        twice" is a question worth being able to answer.

        No branch of this method performs network I/O. There is no endpoint to perform it against.
        """
        verdict: WriteVerdict = self._gate.authorize(request)

        replay = self._ledger.get(request.idempotency_key)
        if replay is not None:
            # The first result, verbatim, flagged. Not a second effect and not a fresh reference.
            replayed = dict(replay)
            replayed["replayed"] = True
            replayed["detail"] = f"{replay['detail']} (replayed: idempotency key already applied)"
            return replayed

        if not verdict.permitted:
            outcome = verdict.outcome_if_refused
        else:
            # The gate permits a real write, but this adapter is fixture-backed and has no endpoint
            # to call. Reporting SUCCEEDED here would be a lie that the closure and reconciliation
            # stages would then act on, so the simulator says what it actually did.
            outcome = ActionOutcome.SIMULATED

        blocked = outcome is ActionOutcome.BLOCKED_BY_POLICY
        result: dict[str, Any] = {
            "outcome": outcome.value,
            # Nothing was created when the write was refused outright, so there is nothing to name.
            "external_ref": None if blocked else self.external_ref_for(request, prefix),
            "detail": detail if not blocked else f"blocked before send: {verdict.explanation}",
            "simulated": outcome is ActionOutcome.SIMULATED,
            "idempotency_key": request.idempotency_key,
            "replayed": False,
            "system": self.system_name,
            "action_type": request.action_type.value,
            "target_ref": request.target_ref,
            "incident_id": request.incident_id,
            "recorded_at": self._clock.now().isoformat(),
            "gate": {
                "permitted": verdict.permitted,
                "simulated": verdict.simulated,
                "reason_code": verdict.reason_code.value,
                "explanation": verdict.explanation,
            },
        }
        if extra:
            result.update(extra)
        if not blocked:
            # A blocked action is not an effect, so it does not occupy the key: if policy is
            # corrected and the same action retried, it must be allowed to happen.
            self._ledger[request.idempotency_key] = dict(result)
        return result

    # -- helpers ---------------------------------------------------------------------------------

    def _offset_hours(self, hours: float) -> str:
        """A fixture offset resolved against the injected clock, as ISO-8601.

        Fixtures store `-96.0`, not a literal timestamp, so "four days stale" stays four days stale
        whichever clock is injected. See `simulation/fixtures/network.py`, authoring rule 2.
        """
        return (self._clock.now() + timedelta(hours=hours)).isoformat()

    def _provenance(self, subject_ref: str) -> dict[str, Any]:
        """The provenance block every read carries.

        `simulated: True` on every payload, in-band. A reader of a stored evidence payload six
        months from now must be able to tell that it came from a fixture without knowing which mode
        the process ran in.
        """
        return {
            "source_system": self.system_name,
            "subject_ref": subject_ref,
            "observed_at": self._clock.now().isoformat(),
            "simulated": True,
        }
