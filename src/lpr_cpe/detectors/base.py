"""The detector contract.

Thirteen baseline detectors share one interface so the diagnosis stage can run them as a set without
knowing which ones exist. Three properties of that interface are deliberate:

**No language model inside a detector.** The specification forbids it and the reason is
reproducibility: an `AnomalyFinding` carries a `score` that a threshold is compared against, and a
score that varies between identical inputs makes every downstream decision unreproducible. `detect`
is ordinary arithmetic over the context it is handed.

**A detector never fetches.** It is handed a `DetectionContext` that already contains what it needs.
This is not tidiness -- it is what makes the detectors testable without a network, and it is what
lets the graph fetch once and run thirteen detectors over the same snapshot rather than thirteen
adapters' worth of calls. A detector that finds a field missing says so through
`data_quality_warnings` and returns a low-confidence finding; it does not go looking.

**A detector that cannot run returns, it does not raise.** `DetectorResult.ran` is False and
`unavailable_reason` says why. A raising detector would take the whole diagnosis stage down for one
missing optical reading, and the graph would lose the twelve findings that did work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from lpr_cpe.domain.diagnosis import AnomalyFinding
from lpr_cpe.domain.enums import DataQualityFlag, Technology
from lpr_cpe.domain.records import CPERecord, EvidenceItem, SLAContext, TopologyContext


@dataclass(slots=True)
class DetectionContext:
    """Everything a detector may read. Assembled once per diagnosis pass.

    `peers` is the neighbour set behind the same delimiter or node, and it is what turns "this
    customer's upstream power is high" into "this customer's upstream power is high and their seven
    tap-mates are fine", which is the difference between a plant fault and a drop fault. It is a
    list of already-masked summaries, not customer records.

    `baseline` is the same subject's own history. Absent for a newly installed service, which is why
    `DataQualityFlag.NO_BASELINE` exists rather than a detector inventing a baseline from a default.
    """

    incident_id: str
    now: datetime
    technology: Technology = Technology.UNKNOWN

    cpe: CPERecord | None = None
    topology: TopologyContext | None = None
    sla: SLAContext | None = None

    # Raw-ish reads, keyed by the adapter that produced them. Dicts rather than models because each
    # vendor payload shape is ours to define (A2) and freezing it into a model would imply a
    # confirmed contract.
    nxt: dict[str, Any] = field(default_factory=dict)
    plant: dict[str, Any] = field(default_factory=dict)
    cpe_raw: dict[str, Any] = field(default_factory=dict)
    wifi: dict[str, Any] = field(default_factory=dict)
    service_platform: dict[str, Any] = field(default_factory=dict)
    recent_changes: list[dict[str, Any]] = field(default_factory=list)
    power_outages: list[dict[str, Any]] = field(default_factory=list)
    weather: dict[str, Any] = field(default_factory=dict)

    peers: list[dict[str, Any]] = field(default_factory=list)
    baseline: dict[str, float] = field(default_factory=dict)
    history: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)

    # Thresholds come from the policy pack so a detector holds no tunable literal of its own.
    thresholds: dict[str, float] = field(default_factory=dict)

    def threshold(self, name: str, default: float) -> float:
        """Read a threshold, falling back to the detector's stated default.

        The default is passed at the call site rather than stored here so that reading the detector
        tells you what it compares against, even when the pack does not override it.
        """
        return float(self.thresholds.get(name, default))

    def missing(self, *names: str) -> list[str]:
        """Which of these context attributes are absent or empty.

        Detectors call this instead of writing their own `if not ctx.cpe` chains, so the resulting
        `data_quality_warnings` are phrased identically across all thirteen.
        """
        out: list[str] = []
        for name in names:
            value = getattr(self, name, None)
            if value is None or (isinstance(value, dict | list) and not value):
                out.append(name)
        return out


@dataclass(slots=True)
class DetectorResult:
    """One detector's output.

    `ran` distinguishes "looked and found nothing" from "could not look". The first is a clean
    result that counts towards the predictive-scan KPIs; the second is a data-quality defect. A
    single `findings == []` would conflate them, and the conflation flatters the system: every
    unavailable adapter would read as a healthy device.
    """

    detector_name: str
    detector_version: str
    ran: bool
    findings: list[AnomalyFinding] = field(default_factory=list)
    unavailable_reason: str = ""
    data_quality_warnings: list[DataQualityFlag] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def clean(self) -> bool:
        """Ran and found nothing. Not the same as `not self.findings`."""
        return self.ran and not self.findings

    @property
    def worst_severity_rank(self) -> int:
        return max((f.severity.rank() for f in self.findings), default=0)

    @classmethod
    def unavailable(
        cls,
        name: str,
        version: str,
        reason: str,
        *,
        flags: list[DataQualityFlag] | None = None,
    ) -> DetectorResult:
        return cls(
            detector_name=name,
            detector_version=version,
            ran=False,
            unavailable_reason=reason,
            data_quality_warnings=flags or [DataQualityFlag.MISSING_FIELD],
        )


@runtime_checkable
class Detector(Protocol):
    """The interface the specification asks for, with the name and version alongside.

    `name` and `version` are attributes rather than being read off the class, so a fixture-backed
    detector and a future model-backed replacement can report different versions while sharing a
    name -- which is what makes an `AnomalyFinding` from six months ago interpretable.
    """

    name: str
    version: str

    async def detect(self, context: DetectionContext) -> DetectorResult: ...


class BaseDetector:
    """Shared plumbing: timing, the unavailable path, and finding construction.

    Subclasses implement `_detect`. The wrapper around it is what guarantees the "returns, does not
    raise" property for every detector at once -- a subclass cannot forget it, because the subclass
    never sees the caller.
    """

    name: str = "base"
    version: str = "0.0.0"
    #: Context attributes this detector cannot work without.
    requires: tuple[str, ...] = ()

    async def detect(self, context: DetectionContext) -> DetectorResult:
        import time

        missing = context.missing(*self.requires)
        if missing:
            return DetectorResult.unavailable(
                self.name,
                self.version,
                f"context missing {', '.join(missing)}",
                flags=[DataQualityFlag.MISSING_FIELD],
            )
        started = time.perf_counter()
        try:
            result = await self._detect(context)
        except Exception as exc:  # noqa: BLE001 -- see the module docstring: never raise upward
            return DetectorResult(
                detector_name=self.name,
                detector_version=self.version,
                ran=False,
                unavailable_reason=f"{type(exc).__name__}: {exc}",
                data_quality_warnings=[DataQualityFlag.ADAPTER_UNAVAILABLE],
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    async def _detect(self, context: DetectionContext) -> DetectorResult:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------------------------------

    def ok(
        self,
        findings: list[AnomalyFinding] | None = None,
        *,
        flags: list[DataQualityFlag] | None = None,
        evidence: list[EvidenceItem] | None = None,
    ) -> DetectorResult:
        return DetectorResult(
            detector_name=self.name,
            detector_version=self.version,
            ran=True,
            findings=findings or [],
            data_quality_warnings=flags or [],
            evidence=evidence or [],
        )

    def finding(
        self,
        context: DetectionContext,
        *,
        score: float,
        confidence: float,
        severity: Any,
        explanation: str,
        affected: tuple[str, ...] = (),
        features: dict[str, float] | None = None,
        recommended_tests: tuple[Any, ...] = (),
        flags: tuple[DataQualityFlag, ...] = (),
        suspected_domain: Any = None,
        suspected_delimiter_ref: str | None = None,
    ) -> AnomalyFinding:
        """Build a finding with this detector's identity already filled in.

        Every mandatory field the specification lists is either a parameter here or supplied
        from the detector -- there is no path to constructing a finding that omits one, because
        `AnomalyFinding` requires them and this is the only constructor the detectors use.
        """
        return AnomalyFinding(
            detector_name=self.name,
            detector_version=self.version,
            observed_at=context.now,
            score=score,
            confidence=confidence,
            severity=severity,
            affected_objects=affected,
            evidence_refs=tuple(e.ref for e in context.evidence),
            explanation=explanation,
            contributing_features=features or {},
            recommended_tests=recommended_tests,
            data_quality_warnings=flags,
            suspected_domain=suspected_domain,
            suspected_delimiter_ref=suspected_delimiter_ref,
        )
