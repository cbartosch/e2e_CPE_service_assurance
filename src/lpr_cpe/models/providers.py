"""The two providers: a deterministic fake, and an Anthropic client behind the same shape.

The specification: "Do not require a model API key to run unit or integration tests." So the fake is
the default and the real one is selected by configuration, not the other way round.

Why the fake hashes its prompt
------------------------------
D7: *"The fake is to hash its prompt to pick a canned response, so that the suite stays offline and
reproducible."* Hashing rather than round-robin is the load-bearing choice. A round-robin fake gives
a different answer depending on how many calls happened earlier in the process, so a test that
passed alone would fail in a suite -- and worse, a *retry* would get a different answer than the
first attempt, which is exactly the behaviour `narrative.py`'s re-ask logic must not accidentally
depend on. Hashing makes the answer a pure function of the question.

The hash is `blake2b` over the prompt, and the canned set is small on purpose: the fake exists to
exercise the plumbing, not to imitate a model. One of its responses is deliberately **invalid
JSON**, because the re-ask path is unreachable otherwise and an unreachable path is an untested one.

The Anthropic client
--------------------
Imported inside the constructor, never at module scope. `anthropic` is an optional extra, and a
top-level import would make `models` un-importable on a machine without it -- which is every machine
running the suite. The same lazy-import discipline `persistence.checkpointer` uses for `psycopg`,
for the reason recorded there: an optional dependency that breaks the default path is not optional.

It is **not exercised by the suite**, and that is a gap rather than an oversight: a test that called
it would need a key and a network. `test_models.py` asserts it satisfies the Protocol and that its
construction fails cleanly without the extra; everything past that is unverified. Gap MODEL-1.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lpr_cpe.models.base import ModelRequest, ModelResponse, ModelUnavailableError

if TYPE_CHECKING:
    from lpr_cpe.config.settings import Settings

#: The fake's canned answers. Keyed by nothing -- the index is `hash(prompt) % len` -- so adding one
#: changes which prompt gets which answer. That is acceptable because no test asserts a *specific*
#: narrative; they assert shape, validity, and the re-ask path. A test that pinned one answer to one
#: prompt would break on every edit to this list and would be testing the fake.
#:
#: The last entry is not JSON. `narrative.py` re-asks once on a schema violation and then falls back
#: to a template, and neither branch can be reached if every canned answer parses.
_CANNED: tuple[str, ...] = (
    json.dumps(
        {
            "summary": "The radios are congested on 2.4 GHz and the client count is high.",
            "key_findings": [
                "2.4 GHz channel utilisation is sustained above the comfortable range",
                "Client count on the band is above the profile's typical value",
            ],
            "issues": [
                {
                    "severity": "minor",
                    "category": "wifi_congestion",
                    "evidence": "channel utilisation and client count on the 2.4 GHz radio",
                    "suggested_fix": "move the radio to a quieter channel",
                }
            ],
            "recommendations": [
                {
                    "priority": "medium",
                    "action": "wifi_channel_change",
                    "rationale": "a quieter channel addresses the utilisation without a visit",
                }
            ],
        }
    ),
    json.dumps(
        {
            "summary": "Signal levels at the premises are weak; the access layer looks marginal.",
            "key_findings": ["Received signal is below the comfortable range here"],
            "issues": [
                {
                    "severity": "major",
                    "category": "access_layer",
                    "evidence": "received signal level against the technology's expected range",
                    "suggested_fix": "check the drop and the delimiter before changing the radios",
                }
            ],
            "recommendations": [
                {
                    "priority": "high",
                    "action": "inspect_drop",
                    "rationale": "a radio change cannot compensate for a weak access layer",
                }
            ],
        }
    ),
    json.dumps(
        {
            "summary": "Nothing in this reading warrants action; the service looks healthy.",
            "key_findings": ["All measured radio metrics are within their comfortable ranges"],
            "issues": [],
            "recommendations": [],
        }
    ),
    # Deliberately unparseable. See the module docstring.
    "I'm afraid I can't produce JSON for that. Here is a paragraph instead.",
)


@dataclass(frozen=True, slots=True)
class FakeModelProvider:
    """Offline, deterministic, and needs no key. The default everywhere.

    `model` is reported as the configured name rather than a literal, so an audit event from a fake
    run and one from a real run have the same shape and a reader can tell them apart by `provider`
    alone -- which is the field that means something.
    """

    model: str = "fake-1"

    @property
    def name(self) -> str:
        return "fake"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """The canned answer this prompt maps to. A pure function of the prompt.

        `blake2b` rather than `hash()`: Python's string hash is salted per process, so a
        `hash()`-keyed fake would answer differently on every run and the suite would be
        irreproducible in the one way this fake exists to prevent.
        """
        prompt = request.as_prompt()
        digest = hashlib.blake2b(prompt.encode("utf-8"), digest_size=8).digest()
        text = _CANNED[int.from_bytes(digest, "big") % len(_CANNED)]
        return ModelResponse(
            text=text,
            provider=self.name,
            model=self.model,
            # Whole-word counts, not tokens. Naming them `tokens` and returning a character count
            # would be a plausible-looking number that a cost dashboard would add up.
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
            detail="canned response selected by prompt digest",
        )


class AnthropicModelProvider:
    """The Anthropic-compatible implementation. Constructed only when configured.

    Untested against the real API -- gap MODEL-1. What *is* tested is that it satisfies the Protocol
    and that constructing it without the extra installed raises something a caller can act on rather
    than an `ImportError` from three frames down.
    """

    def __init__(self, *, model: str, api_key: str | None = None, timeout: float = 30.0) -> None:
        try:
            import anthropic
        except ImportError as missing:  # pragma: no cover - needs the extra absent
            raise ModelUnavailableError(
                "the `anthropic` extra is not installed, so the Anthropic provider cannot be "
                'constructed. Install it with `pip install -e ".[anthropic]"` or set '
                "LPR_MODEL_PROVIDER=fake."
            ) from missing

        self._model = model
        self._timeout = timeout
        # `api_key=None` lets the SDK read `ANTHROPIC_API_KEY` itself rather than this module
        # reaching into the environment for a secret and holding it in a field.
        self._client: Any = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)

    @property
    def name(self) -> str:
        return "anthropic"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """One message, with the request's own budget applied.

        Every failure becomes `ModelUnavailableError`, which is what lets `narrative.py` tell "the
        provider is down" apart from "the provider answered something unusable" -- the first falls
        back to a template immediately, the second earns one re-ask.
        """
        try:
            message = await self._client.messages.create(
                model=self._model,
                max_tokens=request.max_tokens,
                messages=[{"role": "user", "content": request.as_prompt()}],
            )
        except Exception as failure:  # pragma: no cover - needs a network
            raise ModelUnavailableError(f"the Anthropic API call failed: {failure}") from failure

        blocks = [getattr(block, "text", "") for block in getattr(message, "content", [])]
        usage = getattr(message, "usage", None)
        return ModelResponse(
            text="".join(blocks),
            provider=self.name,
            model=self._model,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            stopped_early=getattr(message, "stop_reason", None) == "max_tokens",
        )


def build_provider(settings: Settings | None = None) -> FakeModelProvider | AnthropicModelProvider:
    """The provider this configuration selects. Defaults to the fake, and that default is the point.

    `LPR_MODEL_PROVIDER` chooses; absent, it is `fake`. So a fresh checkout runs, the suite runs
    offline, and reaching a real model is something somebody has to ask for -- the direction the
    specification's "do not require a model API key" points.
    """
    from lpr_cpe.config.settings import ModelProvider as Configured
    from lpr_cpe.config.settings import get_settings

    resolved = settings or get_settings()
    if resolved.model_provider is Configured.ANTHROPIC:
        return AnthropicModelProvider(
            model=resolved.model_name, timeout=resolved.model_timeout_seconds
        )
    return FakeModelProvider(model=resolved.model_name)


__all__ = [
    "AnthropicModelProvider",
    "FakeModelProvider",
    "build_provider",
]
