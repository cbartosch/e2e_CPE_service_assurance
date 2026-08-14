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

import time
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
    #
    # `None` and an empty collection say different things, and the difference is load-bearing.
    # `None` is "never fetched, or the fetch failed"; an empty dict or list is "fetched, and there
    # was nothing there". For `power_outages` the empty list is the *healthy* answer -- no outage
    # near this customer -- so a contract that called it missing data would make the power
    # correlation detector report itself unavailable on nearly every incident it ran on, and the
    # predictive-scan KPIs would count those as data-quality defects rather than clean results.
    nxt: dict[str, Any] | None = None
    plant: dict[str, Any] | None = None
    cpe_raw: dict[str, Any] | None = None
    wifi: dict[str, Any] | None = None
    service_platform: dict[str, Any] | None = None
    recent_changes: list[dict[str, Any]] | None = None
    power_outages: list[dict[str, Any]] | None = None
    weather: dict[str, Any] | None = None

    peers: list[dict[str, Any]] | None = None
    baseline: dict[str, float] | None = None
    history: dict[str, Any] | None = None
    evidence: list[EvidenceItem] = field(default_factory=list)

    #: What the detectors that already ran this pass produced, for the five that classify over the
    #: others' output rather than over telemetry. `None` means those detectors have not run, which
    #: is not the same as their having run and found nothing -- and the difference decides whether
    #: "no fault found" is a conclusion or a statement that nobody looked.
    prior: list[DetectorResult] | None = None

    # Thresholds come from the policy pack so a detector holds no tunable literal of its own.
    thresholds: dict[str, float] = field(default_factory=dict)

    def threshold(self, name: str, default: float) -> float:
        """Read a threshold, falling back to the detector's stated default.

        The default is passed at the call site rather than stored here so that reading the detector
        tells you what it compares against, even when the pack does not override it.
        """
        return float(self.thresholds.get(name, default))

    def missing(self, *names: str) -> list[str]:
        """Which of these context attributes were never supplied.

        `None` only, per the note on the fields above: an empty dict or list is a read that came
        back with nothing, which is a result the detector should interpret rather than a defect
        that should stop it running. Detectors call this instead of writing their own
        `if not ctx.cpe` chains, so the resulting `data_quality_warnings` are phrased identically
        across all thirteen.
        """
        return [name for name in names if getattr(self, name, None) is None]

    def payload(self, name: str) -> dict[str, Any]:
        """A dict read the `requires` gate has already proven present; `{}` is a valid answer."""
        value = getattr(self, name, None)
        if not isinstance(value, dict):
            raise LookupError(f"{name} is not an available dict payload on the context")
        return value

    def rows(self, name: str) -> list[dict[str, Any]]:
        """A list read the `requires` gate has already proven present; `[]` is a valid answer."""
        value = getattr(self, name, None)
        if not isinstance(value, list):
            raise LookupError(f"{name} is not an available list payload on the context")
        return value

    def findings_from(
        self, *detector_names: str, include_derived: bool = False
    ) -> list[AnomalyFinding]:
        """Findings from earlier detectors this pass; empty when they ran and were clean.

        Reads only results whose `ran` is True, so a detector that could not look contributes
        nothing here rather than contributing a silent absence of findings.

        Derived results are excluded by default. A derived finding restates other findings rather
        than adding evidence -- the domain classifier's output *is* the telemetry detectors'
        output, folded -- so counting both would weigh the same observation twice and hand whichever
        side the classifier picked a second vote it did not earn. Ask for them explicitly when the
        summary is what you want.
        """
        wanted = set(detector_names)
        return [
            finding
            for result in (self.prior or ())
            if result.ran
            and (include_derived or not result.derived)
            and (not wanted or result.detector_name in wanted)
            for finding in result.findings
        ]


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
    #: These findings restate earlier findings rather than adding evidence. See `findings_from`.
    derived: bool = False

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
        """Could not look. `flags=[]` is distinct from `flags=None` and means "and that is fine".

        The default applies only when the caller says nothing at all. Passing an explicit empty
        list is how a detector says the data quality is sound despite its not having run -- see
        `not_applicable`, which is the case that needs it.
        """
        return cls(
            detector_name=name,
            detector_version=version,
            ran=False,
            unavailable_reason=reason,
            data_quality_warnings=(
                [DataQualityFlag.MISSING_FIELD] if flags is None else list(flags)
            ),
        )

    @classmethod
    def not_applicable(cls, name: str, version: str, reason: str) -> DetectorResult:
        """Does not apply to this subject, and nothing is wrong with the data.

        A DOCSIS RF detector handed a PON service is the case: the reading it wants does not exist
        for this technology, and the adapter says so by design. Reporting that as `MISSING_FIELD`
        would make every PON incident accumulate phantom quality defects from the HFC-only
        detectors and every HFC incident accumulate them from the PON-only ones, which would in
        turn drag the evidence checks in the policy pack towards blocking on healthy services.
        """
        return cls.unavailable(name, version, reason, flags=[])


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
    #: Technologies this detector applies to at all. Empty means every technology.
    applies_to: tuple[Technology, ...] = ()
    #: True when this detector's findings summarise other detectors' findings rather than adding
    #: evidence of their own. Declared on the class rather than passed at each return, so a detector
    #: cannot be derived on one code path and not on another.
    derives_from_prior: bool = False

    async def detect(self, context: DetectionContext) -> DetectorResult:
        # Technology first, `requires` second, and the order is the whole point. A DOCSIS detector
        # handed a PON service has no `nxt` payload, so a `requires` check running first would
        # report MISSING_FIELD -- a data-quality defect -- for a reading that does not exist and was
        # never meant to. Every PON incident would then carry a phantom defect from the HFC
        # detectors and vice versa, which is exactly what `not_applicable` was added to prevent.
        #
        # UNKNOWN is deliberately not excluded. "Not applicable" is a claim about the plant, and it
        # can only be made when the plant's technology is known; skipping both physical detectors on
        # a metadata gap would leave zero physical evidence, which the no-fault-found scorer reads
        # as an argument against dispatch. A missing label must not become a diagnosis.
        if (
            self.applies_to
            and context.technology is not Technology.UNKNOWN
            and context.technology not in self.applies_to
        ):
            return DetectorResult.not_applicable(
                self.name,
                self.version,
                f"service is {context.technology.value}; this detector applies to "
                f"{', '.join(t.value for t in self.applies_to)}",
            )
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
            findings=list(findings) if findings is not None else [],
            data_quality_warnings=list(flags) if flags is not None else [],
            evidence=list(evidence) if evidence is not None else [],
            derived=self.derives_from_prior,
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
