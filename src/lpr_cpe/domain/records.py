"""Intake and context records: the canonical event, the CPE, the topology, the SLA, data quality
and evidence.

`AssuranceEvent` is the funnel. NXT alarms, customer calls, CRM tickets, scheduler-driven scan
results and field observations all become one of these before anything else looks at them, so every
downstream node reads one shape. The raw vendor payload is kept on `raw_payload` for audit but
nothing routes on it -- if a field matters, it is promoted to a named field here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, Self

from pydantic import Field, field_validator, model_validator

from lpr_cpe.domain.base import DomainModel, FrozenDomainModel, evidence_ref
from lpr_cpe.domain.enums import (
    AreaArchetype,
    CaseType,
    DataQualityFlag,
    DelimiterKind,
    EventSource,
    EvidenceKind,
    Severity,
    Technology,
)


class AssuranceEvent(FrozenDomainModel):
    """The canonical trigger. Frozen: what arrived is a fact, and enrichment goes elsewhere."""

    event_id: str
    source: EventSource
    case_type: CaseType
    technology: Technology = Technology.UNKNOWN
    severity: Severity = Severity.MEDIUM
    occurred_at: datetime
    received_at: datetime

    # Subject of the event. At least one must be present -- an event about nothing cannot be
    # triaged, and accepting it would create an incident no node could progress.
    customer_ref: str | None = None
    service_ref: str | None = None
    cpe_ref: str | None = None
    network_element_ref: str | None = None

    summary: str = Field(min_length=1, max_length=500)
    detail: str = ""
    vendor_event_type: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    # The source system's own correlation key, carried across at construction. NXT groups repeats of
    # one alarm under a key before we ever see them; care systems key a ticket to a call. P04 reads
    # it as one correlation input among several.
    #
    # Set here and nowhere else, because it cannot be set anywhere else. `events` reduces with
    # `append_unique`, which keys on `event_id` and keeps the *first* write -- so a node appending
    # an enriched `model_copy` of an event already in state has that copy silently dropped. The
    # event is what arrived; where P04 concludes this incident belongs to a larger one, it says so
    # in `linked_records` under a `routing.PARENT_RECORD_KEYS` key, the field D03 reads.
    dedupe_key: str = ""

    @field_validator("occurred_at", "received_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _has_a_subject(self) -> Self:
        if not any((self.customer_ref, self.service_ref, self.cpe_ref, self.network_element_ref)):
            raise ValueError(
                "AssuranceEvent needs at least one of customer_ref, service_ref, cpe_ref or "
                "network_element_ref: an event with no subject cannot be triaged"
            )
        return self

    @property
    def detection_latency(self) -> timedelta:
        """How long the signal took to reach us. Feeds `time_to_detect_seconds`.

        Clamped at zero: a vendor clock ahead of ours would otherwise produce a negative latency
        that averages into the KPI and quietly improves it.
        """
        delta = self.received_at - self.occurred_at
        return max(delta, timedelta(0))


class WifiRadioSnapshot(DomainModel):
    """Per-radio summary extracted from TR-181 `Device.WiFi.*`.

    Client MAC addresses are masked before this object exists -- see `security.redaction`. There is
    no field here that could hold one, which is the point: PII minimization enforced by the schema
    rather than by remembering to call a masker.
    """

    band: str
    channel: int | None = None
    channel_width_mhz: int | None = None
    utilization_pct: float | None = Field(default=None, ge=0, le=100)
    noise_floor_dbm: float | None = None
    error_rate_pct: float | None = Field(default=None, ge=0, le=100)
    client_count: int = Field(default=0, ge=0)
    rssi_avg_dbm: float | None = None
    rssi_worst_dbm: float | None = None
    rssi_best_dbm: float | None = None
    throughput_mbps: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _rssi_ordering(self) -> Self:
        # worst <= avg <= best, or the extraction is wrong and every score derived from it is too.
        worst, avg, best = self.rssi_worst_dbm, self.rssi_avg_dbm, self.rssi_best_dbm
        if (
            worst is not None
            and avg is not None
            and best is not None
            and not (worst <= avg <= best)
        ):
            raise ValueError(
                f"RSSI summary is not ordered worst<=avg<=best: {worst}, {avg}, {best}"
            )
        return self


class CPERecord(DomainModel):
    """The customer-premises device as last read.

    `last_inform_at` and `data_available` carry the staleness that everything else must respect: a
    Wi-Fi verdict computed from a device that has not informed in three days is a verdict about
    three-day-old conditions, and `DataQualityAssessment` is expected to say so.
    """

    cpe_ref: str
    serial_number: str | None = None
    model: str = ""
    vendor: str = ""
    firmware_version: str = ""
    technology: Technology = Technology.UNKNOWN
    management_protocol: str = "tr-069"

    online: bool | None = None
    last_inform_at: datetime | None = None
    uptime_seconds: int | None = Field(default=None, ge=0)

    # HFC optics/RF and PON optics. Both nullable on one record rather than two subclasses: a
    # service can be migrated between technologies and the record survives the migration.
    downstream_power_dbmv: float | None = None
    upstream_power_dbmv: float | None = None
    downstream_snr_db: float | None = None
    uncorrectable_codewords: int | None = Field(default=None, ge=0)
    rx_optical_power_dbm: float | None = None
    tx_optical_power_dbm: float | None = None

    radios: list[WifiRadioSnapshot] = Field(default_factory=list)
    data_available: bool = True
    data_quality_notes: list[str] = Field(default_factory=list)

    @field_validator("last_inform_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("last_inform_at must be timezone-aware")
        return v

    def inform_age(self, now: datetime) -> timedelta | None:
        if self.last_inform_at is None:
            return None
        return max(now - self.last_inform_at, timedelta(0))


class TopologyContext(DomainModel):
    """Where the service sits in the plant, and how many neighbours share each hop.

    `homes_behind_delimiter` is the blast-radius denominator. It is nullable and *must* be treated
    as unknown rather than defaulted to the configured tap/ODP size at the point of use, because a
    guessed denominator produces a confident blast-radius number that no measurement supports.
    """

    technology: Technology = Technology.UNKNOWN
    delimiter_kind: DelimiterKind = DelimiterKind.UNKNOWN
    delimiter_ref: str | None = None
    area_archetype: AreaArchetype | None = None

    # HFC chain
    node_ref: str | None = None
    amplifier_refs: list[str] = Field(default_factory=list)
    cmts_ref: str | None = None
    service_group_ref: str | None = None

    # PON chain
    olt_ref: str | None = None
    pon_port_ref: str | None = None
    primary_splitter_ref: str | None = None
    odp_ref: str | None = None
    split_ratio: int | None = Field(default=None, ge=1)

    headend_ref: str | None = None
    homes_behind_delimiter: int | None = Field(default=None, ge=0)
    homes_behind_node_or_port: int | None = Field(default=None, ge=0)
    mdu_ref: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    topology_source: str = ""
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def _delimiter_matches_technology(self) -> Self:
        # A PON service behind a "tap" means two systems disagree about this customer. Catching it
        # here is cheaper than catching it when a Clean Boots crew is sent to a tap that is an ODP.
        if self.technology is Technology.PON and self.delimiter_kind is DelimiterKind.TAP:
            raise ValueError("PON service cannot sit behind a TAP delimiter")
        if self.technology is Technology.HFC and self.delimiter_kind is DelimiterKind.ODP:
            raise ValueError("HFC service cannot sit behind an ODP delimiter")
        return self

    @model_validator(mode="after")
    def _nesting_is_sane(self) -> Self:
        a, b = self.homes_behind_delimiter, self.homes_behind_node_or_port
        if a is not None and b is not None and a > b:
            raise ValueError(
                f"homes_behind_delimiter ({a}) exceeds homes_behind_node_or_port ({b}): the "
                "delimiter is downstream of the node/port, so it cannot serve more homes"
            )
        return self


class SLAContext(DomainModel):
    """The one clock (D1).

    `clock_started_at` is written once at intake. Deadlines are *derived* from it every time they
    are read rather than stored, so there is no second copy to fall out of step, and a pause that
    should not stop the clock cannot accidentally stop it.
    """

    sla_ref: str = ""
    product_tier: str = "residential"
    clock_started_at: datetime
    response_target: timedelta = timedelta(hours=4)
    restore_target: timedelta = timedelta(hours=24)
    business_hours_only: bool = False
    vulnerable_customer: bool = False
    priority_customer: bool = False
    credit_at_risk: bool = False
    paused_intervals: list[tuple[datetime, datetime]] = Field(default_factory=list)

    @field_validator("clock_started_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("clock_started_at must be timezone-aware")
        return v

    @property
    def paused_duration(self) -> timedelta:
        """Total time excluded from the clock.

        Only intervals a policy explicitly permits pausing for (awaiting customer access, for
        instance) are ever added. The customer-facing SLA is not paused by our own queueing.
        """
        total = timedelta(0)
        for start, end in self.paused_intervals:
            if end > start:
                total += end - start
        return total

    def response_deadline(self) -> datetime:
        return self.clock_started_at + self.response_target + self.paused_duration

    def restore_deadline(self) -> datetime:
        return self.clock_started_at + self.restore_target + self.paused_duration

    def time_remaining(self, now: datetime) -> timedelta:
        return self.restore_deadline() - now

    def is_breached(self, now: datetime) -> bool:
        return now > self.restore_deadline()

    def at_risk(self, now: datetime, threshold_fraction: float = 0.25) -> bool:
        """True once less than `threshold_fraction` of the restore budget is left.

        A breached SLA is also at risk, deliberately: a dashboard that shows `at_risk` falling as
        incidents breach out of it would read as improvement.
        """
        budget = self.restore_target + self.paused_duration
        if budget <= timedelta(0):
            return True
        return self.time_remaining(now) < budget * threshold_fraction


class DataQualityAssessment(DomainModel):
    """What we could not see, stated explicitly.

    `sufficient_for_action` is the field the graph routes on. It is computed, not asserted: a caller
    cannot construct an assessment that claims sufficiency while carrying blocking flags.
    """

    assessed_at: datetime
    flags: list[DataQualityFlag] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    stale_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    completeness_score: float = Field(default=1.0, ge=0.0, le=1.0)

    # Flags that make automated *action* unsafe, as opposed to merely reducing confidence. A
    # missing baseline weakens a prediction; an unavailable adapter means we are guessing.
    # ClassVar, not a field: Pydantic would otherwise make this settable per instance, and an
    # instance that narrowed its own blocking set could declare itself sufficient.
    BLOCKING_FLAGS: ClassVar[frozenset[DataQualityFlag]] = frozenset(
        {
            DataQualityFlag.ADAPTER_UNAVAILABLE,
            DataQualityFlag.CONFLICTING_SOURCES,
            DataQualityFlag.INCONSISTENT_TOPOLOGY,
        }
    )

    @property
    def blocking(self) -> list[DataQualityFlag]:
        return [f for f in self.flags if f in self.BLOCKING_FLAGS]

    @property
    def sufficient_for_action(self) -> bool:
        return not self.blocking and self.completeness_score >= 0.5


class EvidenceItem(FrozenDomainModel):
    """One observation, with provenance. Append-only in state.

    `ref` is derived from kind + subject + observation time, so the same alarm cited by three
    detectors de-duplicates to one item. Payloads are summaries; anything large is an
    `object_reference` (see `domain.base.object_reference`).
    """

    ref: str = ""
    kind: EvidenceKind
    subject_ref: str
    observed_at: datetime
    recorded_at: datetime
    source_system: str
    summary: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    object_ref: dict[str, Any] | None = None
    cited_by: tuple[str, ...] = ()
    trustworthiness: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _derive_ref(self) -> Self:
        if not self.ref:
            # A frozen model still permits this during validation, which is why the derivation
            # lives here rather than in a factory a caller could forget to use.
            object.__setattr__(
                self, "ref", evidence_ref(self.kind, self.subject_ref, self.observed_at)
            )
        return self

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return v
