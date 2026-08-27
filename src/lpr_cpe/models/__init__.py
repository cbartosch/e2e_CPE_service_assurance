"""Model integration: a provider abstraction, two providers, and the one function that uses them.

`ModelProvider` is the abstraction, `FakeModelProvider` and `AnthropicModelProvider` the two
implementations, and `write_narrative` the only model-assisted function in this system. That last
count is deliberate and is A5 in practice: nothing here is asked for a number, and the one thing a
model is asked for is prose an operator reads.

D7 said this package's shape before it existed -- "the fake is to hash its prompt to pick a canned
response, so that the suite stays offline and reproducible, with the Anthropic-compatible
implementation behind the same Protocol and exercised only when a key is present". That is what
landed, and the two parts of it worth reading are `providers._CANNED` (one entry is deliberately not
JSON, so the re-ask path is reachable) and `narrative.NarrativeDraft` (the two forbidden fields are
absent from the class, not filtered out of it).
"""

from lpr_cpe.models.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUnavailableError,
    RecordingProvider,
)
from lpr_cpe.models.narrative import (
    NarrativeDraft,
    NarrativeResult,
    draft_schema,
    write_narrative,
)
from lpr_cpe.models.providers import (
    AnthropicModelProvider,
    FakeModelProvider,
    build_provider,
)

__all__ = [
    "AnthropicModelProvider",
    "FakeModelProvider",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelUnavailableError",
    "NarrativeDraft",
    "NarrativeResult",
    "RecordingProvider",
    "build_provider",
    "draft_schema",
    "write_narrative",
]
