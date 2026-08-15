"""Where the service sits in the plant, and which object is its Clean/Dirty Boots boundary.

`delimiter_kind_for` is named by `domain.enums.DelimiterKind`'s own docstring as the one place the
tap/ODP fork is decided. That is the whole reason it exists as a function rather than as a
conditional at each use: the fork appears in dispatch (which crew), in blast radius (which default
size), in the MR text (which object OSP is being asked to repair) and in the customer narrative, and
four copies of `if technology is HFC` is four chances to write `ODP` for an HFC service. The
`TopologyContext` validator refuses that combination, so the fourth copy would not ship a wrong
crew -- it would raise a validation error inside an incident, which is a better failure than a wrong
truck and a worse one than not having the copy.

`resolve_topology` returns the flags alongside the context rather than raising on a payload that
disagrees with itself. An inventory record that calls a PON service's delimiter a tap is a real
condition -- systems drift -- and the workflow's answer to it is `INCONSISTENT_TOPOLOGY`, which
blocks automated action and routes to a human. Raising would instead lose the twelve fields that
were correct.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lpr_cpe.decision_services._payload import read_float, read_int, read_text
from lpr_cpe.domain.enums import AreaArchetype, DataQualityFlag, DelimiterKind, Technology
from lpr_cpe.domain.records import TopologyContext

#: The one mapping. `UNKNOWN` in, `UNKNOWN` out -- deliberately, and this is the case worth stating:
#: a service whose technology has not been read yet has no known delimiter kind, and defaulting to
#: either one would produce a confident answer from an absence of information. Every caller of this
#: function is deciding something a wrong answer makes expensive.
_KIND_BY_TECHNOLOGY: dict[Technology, DelimiterKind] = {
    Technology.HFC: DelimiterKind.TAP,
    Technology.PON: DelimiterKind.ODP,
}


def delimiter_kind_for(technology: Technology) -> DelimiterKind:
    """Tap for HFC, ODP for PON, `UNKNOWN` for an unread technology."""
    return _KIND_BY_TECHNOLOGY.get(technology, DelimiterKind.UNKNOWN)


class ResolvedTopology:
    """A `TopologyContext` plus what the payload could not tell us.

    Two returns rather than one because the caller needs both and they have different lifetimes: the
    context goes into graph state and is read for the rest of the incident, while the flags are
    folded into that pass's `DataQualityAssessment` and are a statement about this read.
    """

    __slots__ = ("context", "flags", "notes")

    def __init__(
        self,
        context: TopologyContext,
        flags: tuple[DataQualityFlag, ...],
        notes: tuple[str, ...],
    ) -> None:
        self.context = context
        self.flags = flags
        self.notes = notes

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"ResolvedTopology(delimiter={self.context.delimiter_ref!r}, "
            f"flags={[f.value for f in self.flags]})"
        )


def resolve_topology(
    payload: dict[str, Any] | None,
    *,
    technology: Technology,
    resolved_at: datetime,
) -> ResolvedTopology:
    """Turn an HFC or PON `fetch_topology` read into a `TopologyContext`.

    Three rules, each of which had an obvious wrong alternative:

    **The delimiter kind comes from `technology`, not from the payload.** The payload states one
    too, and when the two disagree the disagreement is reported as `INCONSISTENT_TOPOLOGY` rather
    than resolved in the payload's favour. Deriving it means the returned context always satisfies
    `TopologyContext._delimiter_matches_technology`, so a drifted inventory record gives a flagged
    context the workflow can reason about instead of a `ValidationError` in a node.

    **A missing count stays missing.** `homes_behind_delimiter` is the blast-radius denominator and
    `TopologyContext` documents it as unknown-rather-than-defaulted. Substituting the configured tap
    size here would produce a number that reads as measured everywhere downstream;
    `decision_services.blast_radius` is where a default is applied, and it says that it did.

    **`None` payload is not an empty payload.** A failed topology fetch returns a context with
    nothing in it *and* `ADAPTER_UNAVAILABLE`, which is a blocking flag. An empty context with no
    flag would look like a service that genuinely sits nowhere.
    """
    flags: list[DataQualityFlag] = []
    notes: list[str] = []
    kind = delimiter_kind_for(technology)

    if payload is None:
        return ResolvedTopology(
            TopologyContext(
                technology=technology,
                delimiter_kind=kind,
                topology_source="",
                resolved_at=resolved_at,
            ),
            (DataQualityFlag.ADAPTER_UNAVAILABLE,),
            ("topology was not fetched, so nothing about this service's plant position is known",),
        )

    stated = read_text(payload, "delimiter_kind")
    if stated is not None and stated != kind.value:
        flags.append(DataQualityFlag.INCONSISTENT_TOPOLOGY)
        notes.append(
            f"inventory calls this delimiter a {stated!r} while the service is "
            f"{technology.value}, whose delimiter is a {kind.value}; the two systems disagree "
            "about which object is the Clean/Dirty Boots boundary"
        )
    if kind is DelimiterKind.UNKNOWN:
        flags.append(DataQualityFlag.MISSING_FIELD)
        notes.append(
            "the service's technology has not been read, so its delimiter kind cannot be derived"
        )

    delimiter_ref = read_text(payload, "delimiter_ref")
    if delimiter_ref is None:
        flags.append(DataQualityFlag.MISSING_FIELD)
        notes.append("no delimiter reference: the Clean/Dirty Boots boundary object is unnamed")

    behind_delimiter = read_int(payload, "homes_behind_delimiter")
    behind_parent = read_int(payload, "homes_behind_node_or_port")
    if behind_delimiter is None or behind_parent is None:
        flags.append(DataQualityFlag.MISSING_FIELD)
        notes.append(
            "plant records do not give both homes-behind counts, so blast radius will be estimated "
            "from the policy pack's defaults rather than measured"
        )
    if (
        behind_delimiter is not None
        and behind_parent is not None
        and behind_delimiter > behind_parent
    ):
        # `TopologyContext._nesting_is_sane` refuses this pair. Dropping the inner count keeps the
        # outer, larger one -- which is the conservative half for a blast-radius estimate.
        flags.append(DataQualityFlag.INCONSISTENT_TOPOLOGY)
        notes.append(
            f"{behind_delimiter} homes behind the delimiter exceeds {behind_parent} behind its "
            "parent, which cannot be true; the delimiter count is discarded"
        )
        behind_delimiter = None

    archetype: AreaArchetype | None = None
    raw_archetype = read_text(payload, "area_archetype")
    if raw_archetype is not None:
        try:
            archetype = AreaArchetype(raw_archetype)
        except ValueError:
            flags.append(DataQualityFlag.MISSING_FIELD)
            notes.append(
                f"area archetype {raw_archetype!r} is not one of the four operating contexts, so "
                "travel time and access overhead will fall back to the dispatch defaults"
            )

    amplifiers = payload.get("amplifier_refs")
    context = TopologyContext(
        technology=technology,
        delimiter_kind=kind,
        delimiter_ref=delimiter_ref,
        area_archetype=archetype,
        node_ref=read_text(payload, "node_ref"),
        amplifier_refs=[str(a) for a in amplifiers] if isinstance(amplifiers, list) else [],
        cmts_ref=read_text(payload, "cmts_ref"),
        service_group_ref=read_text(payload, "service_group_ref"),
        olt_ref=read_text(payload, "olt_ref"),
        pon_port_ref=read_text(payload, "pon_port_ref"),
        primary_splitter_ref=read_text(payload, "primary_splitter_ref"),
        odp_ref=read_text(payload, "odp_ref"),
        split_ratio=read_int(payload, "split_ratio") or None,
        headend_ref=read_text(payload, "headend_ref"),
        homes_behind_delimiter=behind_delimiter,
        homes_behind_node_or_port=behind_parent,
        mdu_ref=read_text(payload, "mdu_ref"),
        latitude=read_float(payload, "latitude"),
        longitude=read_float(payload, "longitude"),
        topology_source=read_text(payload, "topology_source") or "",
        resolved_at=resolved_at,
    )
    return ResolvedTopology(context, tuple(flags), tuple(notes))


__all__ = ["ResolvedTopology", "delimiter_kind_for", "resolve_topology"]
