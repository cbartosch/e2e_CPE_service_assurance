"""The Wi-Fi narrative: the one model-assisted function in this system, and D6 enforced in code.

The specification offers the reference implementation's output schema and then names the problem
with it: `verdict` and `wifi_health_score` are in it, and a language model must not produce either.
It gives two options, and D6 takes (a) -- *"strip `verdict` and `wifi_health_score` from the model's
own output schema"* and merge the deterministic scorer's values in afterwards.

**That stripping is the whole design of this module, and is structural rather than conventional.**
`NarrativeDraft` is the schema the model is given, and the two forbidden fields are simply not
declared on it. A model that returned them would have them rejected by `extra="forbid"`, and an
edit that wanted to read a score from the model would have to add a field -- which is a visible
change to a class whose docstring says why it must not happen. Compare the alternative: asking for
the full schema and ignoring two keys, where the ignoring is a line of code somebody deletes.

The re-ask, and why exactly one
-------------------------------
"Validate against a strict schema and apply an automatic re-ask/repair step on validation failure
before accepting the output" -- the specification's own words. One re-ask, with the validation error
attached, and then a **templated** narrative. Not two re-asks: a model that failed a schema twice
with the error in front of it is not going to pass on the third attempt, and each attempt is a
timeout the graph node is holding a thread open for.

`ModelUnavailableError` does *not* earn a re-ask, and the distinction is the reason
`models.base` declares that exception at all. A provider that is down will be down for the retry;
spending the re-ask on it delays the fallback for nothing.

The fallback is a real narrative
--------------------------------
`_templated` produces prose from the deterministic values alone, and it is what an operator reads
when there is no model configured -- which is the default. So it is not a stub. `narrative_source`
records which one spoke: `model`, `model_repaired`, or `template`. An operator-facing sentence whose
provenance is unrecorded is one nobody can weigh.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lpr_cpe.models.base import ModelRequest, ModelUnavailableError

if TYPE_CHECKING:
    from lpr_cpe.config.settings import Settings
    from lpr_cpe.domain.diagnosis import PredictionResult
    from lpr_cpe.models.base import ModelProvider

#: What `narrative_source` may say. A closed set, because it is read by an operator UI and by the
#: audit trail, and "some string the code happened to write" is not a provenance.
NarrativeSource = Literal["none", "model", "model_repaired", "template"]


class NarrativeIssue(BaseModel):
    """One problem the model is describing. The specification's `issues` item, unchanged."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "minor", "major", "critical"]
    category: str = Field(min_length=1, max_length=64)
    evidence: str = Field(min_length=1, max_length=1000)
    suggested_fix: str = Field(min_length=1, max_length=1000)


class NarrativeRecommendation(BaseModel):
    """One thing the model suggests. Advisory: no node reads `action` to decide anything."""

    model_config = ConfigDict(extra="forbid")

    priority: Literal["low", "medium", "high"]
    action: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=1000)


class NarrativeDraft(BaseModel):
    """The schema the model is given. **`verdict` and `wifi_health_score` are absent by design.**

    Do not add them. D6 is that no language model produces a number a decision depends on, and the
    deterministic scorer already owns both -- `decision_services.forecast` derives the band from the
    breached metrics and `PredictionResult` carries them. A field here would create a second author
    for a value with one owner, and the one that drifted would be the one nothing loaded.

    `extra="forbid"` is what makes the absence enforced rather than merely intended: a model that
    returns `verdict` fails validation, earns the re-ask, and if it insists, falls back to a
    template. That is the correct outcome -- a model ignoring the schema on a safety-relevant field
    is a model whose prose should not be trusted either.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    key_findings: list[str] = Field(default_factory=list, max_length=20)
    issues: list[NarrativeIssue] = Field(default_factory=list, max_length=20)
    recommendations: list[NarrativeRecommendation] = Field(default_factory=list, max_length=20)


def _without_prose(node: Any) -> Any:
    """Strip `description` and `title` from a generated schema, recursively.

    **Pydantic puts each class docstring into the schema as `description`,** and these docstrings
    explain at length *why* `verdict` and `wifi_health_score` are absent -- naming both fields, D6,
    and the drift argument. So the schema handed to the model contained the two words the schema
    exists to exclude, along with several hundred characters of our internal reasoning.

    Found by the test that reads the schema text rather than the field set, which is why that test
    checks the text: the field list was correct throughout and the *prompt* was not. Two costs, and
    the second is the one that matters. Tokens, for reasoning the model cannot act on. And a prompt
    that says "do not produce a verdict" four times in prose is a prompt that has mentioned a
    verdict four times -- which is a strange thing to hand a model and ask it not to think about.

    Titles go too, for a smaller reason: pydantic derives them from class names, so the schema would
    advertise `NarrativeDraft` and `NarrativeIssue`, which are facts about this codebase rather than
    about the answer wanted.
    """
    if isinstance(node, dict):
        return {
            key: _without_prose(value)
            for key, value in node.items()
            if key not in {"description", "title"}
        }
    if isinstance(node, list):
        return [_without_prose(item) for item in node]
    return node


def draft_schema() -> str:
    """The JSON schema the prompt carries. Generated, then stripped of our own prose.

    Hand-writing it would let the prompt drift from the model that validates the answer, and the
    failure mode is a model correctly following a schema we no longer enforce. Generating it and
    removing the descriptions keeps the structure authoritative and the commentary out -- see
    `_without_prose` for what was in there.
    """
    return json.dumps(_without_prose(NarrativeDraft.model_json_schema()), indent=2, sort_keys=True)


_INSTRUCTION = (
    "You are writing the operator-facing assessment for one customer premises equipment reading in "
    "a telecoms service-assurance workflow. Describe what the measurements show and what a "
    "technician or an account team should understand from them. Be specific and do not speculate "
    "beyond the readings. Do not state a pass/fail verdict and do not state a health score: both "
    "are calculated elsewhere and will be attached to your text. Reply with JSON only."
)


def _facts(prediction: PredictionResult) -> str:
    """The deterministic reading, rendered for the prompt. Numbers in, prose out.

    The band and the score *are* included here even though the model may not produce them, and that
    is not a contradiction: the model is being told the verdict so its prose agrees with it. What it
    may not do is compute one.
    """
    lines = [
        f"subject: {prediction.subject_ref}",
        f"failure probability (calculated): {prediction.failure_probability:.3f}",
        f"confidence in the reading (calculated): {prediction.confidence:.3f}",
    ]
    if prediction.band is not None:
        lines.append(f"health band (calculated, authoritative): {prediction.band.value}")
    if prediction.wifi_health_score is not None:
        lines.append(
            f"health score (calculated, authoritative): {prediction.wifi_health_score:.1f}"
        )
    if prediction.top_features:
        measured = ", ".join(f"{k}={v:g}" for k, v in sorted(prediction.top_features.items()))
        lines.append(f"measured features: {measured}")
    if prediction.recommended_actions:
        levers = ", ".join(a.value for a in prediction.recommended_actions)
        lines.append(f"remote levers the detector indicated: {levers}")
    if prediction.data_quality_warnings:
        flags = ", ".join(str(getattr(f, "value", f)) for f in prediction.data_quality_warnings)
        lines.append(f"data-quality warnings on this reading: {flags}")
    return "\n".join(lines)


def _templated(prediction: PredictionResult) -> str:
    """The narrative when no model spoke. Prose from the deterministic values alone.

    Not a placeholder. With the fake provider configured by default and the Anthropic one behind an
    extra, this is what most runs produce, and it has to read like something written on purpose.
    """
    band = prediction.band.value.replace("_", " ") if prediction.band is not None else "unrated"
    opening = f"The radios at {prediction.subject_ref} read as {band}"
    if prediction.wifi_health_score is not None:
        opening += f", scoring {prediction.wifi_health_score:.0f} of 100"
    opening += (
        f", with a calculated failure probability of {prediction.failure_probability:.0%} "
        f"at {prediction.confidence:.0%} confidence."
    )

    parts = [opening]
    if prediction.top_features:
        worst = ", ".join(f"{k} at {v:g}" for k, v in sorted(prediction.top_features.items())[:3])
        parts.append(f"The readings behind that are {worst}.")
    if prediction.recommended_actions:
        levers = " and ".join(a.value.replace("_", " ") for a in prediction.recommended_actions)
        parts.append(f"The detector indicates {levers} as the remote lever to try.")
    else:
        parts.append("No remote lever is indicated for this reading.")
    if prediction.data_quality_warnings:
        parts.append(
            "This assessment was computed over data the collector flagged, so treat it as "
            "provisional."
        )
    return " ".join(parts)


class NarrativeResult(BaseModel):
    """The narrative and its provenance, ready to merge onto a `PredictionResult`."""

    model_config = ConfigDict(frozen=True)

    text: str
    source: NarrativeSource
    provider: str = ""
    model: str = ""
    total_tokens: int = 0
    attempts: int = 0
    detail: str = ""


async def write_narrative(
    prediction: PredictionResult,
    *,
    provider: ModelProvider | None,
    settings: Settings,
    context: str = "",
) -> NarrativeResult:
    """Ask for prose, validate it, re-ask once, then fall back to a template.

    `provider=None` is a supported call and returns the template immediately. That is the path a
    graph node takes when no model is configured at all, and it must not be an error: A5 is that
    nothing a decision depends on comes from a model, so a system with no model configured is a
    fully functioning system.

    `context` is untrusted -- a technician note, typically -- and `ModelRequest` screens and redacts
    it. Nothing here has to remember to.
    """
    if provider is None:
        return NarrativeResult(text=_templated(prediction), source="template", attempts=0)

    request = ModelRequest(
        instruction=_INSTRUCTION,
        context=f"{_facts(prediction)}\n{context}".strip(),
        schema_hint=draft_schema(),
        max_tokens=settings.model_max_tokens,
        timeout_seconds=settings.model_timeout_seconds,
    )

    first = await _attempt(provider, request)
    if first.draft is not None:
        return NarrativeResult(
            text=_render(first.draft),
            source="model",
            provider=first.provider,
            model=first.model,
            total_tokens=first.tokens,
            attempts=1,
        )
    if first.unavailable:
        # No re-ask: the provider is down and will be down for the retry. See the module docstring.
        return NarrativeResult(
            text=_templated(prediction),
            source="template",
            attempts=1,
            detail=f"provider unavailable: {first.detail}",
        )

    repair = await _attempt(
        provider,
        request.model_copy(
            update={
                "instruction": (
                    f"{_INSTRUCTION}\n\nYour previous reply did not validate against the schema. "
                    f"The error was:\n{first.detail}\nReply with JSON only, matching the schema "
                    "exactly. Do not include any field the schema does not declare."
                )
            }
        ),
    )
    if repair.draft is not None:
        return NarrativeResult(
            text=_render(repair.draft),
            source="model_repaired",
            provider=repair.provider,
            model=repair.model,
            total_tokens=first.tokens + repair.tokens,
            attempts=2,
        )
    return NarrativeResult(
        text=_templated(prediction),
        source="template",
        attempts=2,
        detail=f"schema violation twice: {repair.detail or first.detail}",
    )


class _Attempt(BaseModel):
    """One round trip and what became of it. Internal; `write_narrative` is the surface."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    draft: NarrativeDraft | None = None
    unavailable: bool = False
    detail: str = ""
    provider: str = ""
    model: str = ""
    tokens: int = 0


async def _attempt(provider: ModelProvider, request: ModelRequest) -> _Attempt:
    """Call, parse, validate. Every failure is described rather than raised.

    Returning a description instead of raising is what lets `write_narrative` read as the policy it
    is -- try, repair once, fall back -- rather than as three nested try blocks.
    """
    try:
        response = await provider.complete(request)
    except ModelUnavailableError as down:
        return _Attempt(unavailable=True, detail=str(down))

    try:
        payload = json.loads(response.text)
    except (TypeError, ValueError) as bad:
        return _Attempt(
            detail=f"the reply was not JSON: {bad}",
            provider=response.provider,
            model=response.model,
            tokens=response.total_tokens,
        )

    try:
        draft = NarrativeDraft.model_validate(payload)
    except ValidationError as invalid:
        return _Attempt(
            detail=_first_error(invalid),
            provider=response.provider,
            model=response.model,
            tokens=response.total_tokens,
        )

    return _Attempt(
        draft=draft,
        provider=response.provider,
        model=response.model,
        tokens=response.total_tokens,
    )


def _first_error(invalid: ValidationError) -> str:
    """One error, not all of them. The re-ask prompt has a token budget like everything else."""
    errors = invalid.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "the reply did not match the schema"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
    return f"{location}: {first.get('msg', 'invalid')}"


def _render(draft: NarrativeDraft) -> str:
    """The draft as the paragraph an operator reads.

    Flattened here rather than stored as structure, because `PredictionResult.narrative` is a `str`
    and the structured form has no reader. Keeping the model's JSON on state would be storing a
    shape nothing consumes, which is the thing `docs/vendor-integration-gaps.md` calls out
    elsewhere as inviting drift.
    """
    parts = [draft.summary]
    if draft.key_findings:
        parts.append("Findings: " + "; ".join(draft.key_findings) + ".")
    for issue in draft.issues:
        parts.append(f"{issue.severity.title()} {issue.category}: {issue.evidence}.")
    for recommendation in draft.recommendations:
        parts.append(
            f"Recommended ({recommendation.priority}): {recommendation.action} "
            f"-- {recommendation.rationale}."
        )
    return " ".join(parts)


__all__ = [
    "NarrativeDraft",
    "NarrativeIssue",
    "NarrativeRecommendation",
    "NarrativeResult",
    "NarrativeSource",
    "draft_schema",
    "write_narrative",
]
