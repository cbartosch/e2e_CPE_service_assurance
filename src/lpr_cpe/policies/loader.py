"""Reading, hashing and caching the policy pack.

The interesting decision here is what `policy_version` means. `pack.yaml` declares a human-readable
version (`2026.08.1`), and this module appends a digest of the file's *parsed content*, producing
`2026.08.1+3f9a1c7d5e02`. Every `PolicyDecision` and `AuditEvent` carries the composite.

That matters because the alternative -- trusting the declared string alone -- fails in the most
ordinary way imaginable: someone edits `min_for_dispatch` from 0.70 to 0.60 to unblock a stuck
incident, does not bump the version, and every decision made afterwards claims to have been made
under `2026.08.1`. A month later a review of a bad dispatch reads the pack at `2026.08.1`, finds
0.70, and concludes the engine misbehaved. The audit trail's only means of reconstructing a past
decision is the version string, so it has to be derived from the thing it describes.

Digesting the *parsed* content rather than the file bytes is deliberate in both directions:

* reformatting a comment, re-wrapping a paragraph or changing indentation does **not** change the
  version, so editorial work on this heavily-commented file does not invalidate the audit trail;
* changing any value, adding any key or removing any row **does**.

`load_pack` is cached on `(resolved path, mtime_ns, size)`. The mtime is in the key so that a
running process picks up an edited pack on the next call rather than requiring a restart, and the
size is there because mtime granularity has bitten every project that has relied on it alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from lpr_cpe.policies.models import PolicyPack

#: The pack shipped with the package. Installed as package data by hatchling (it is inside
#: `src/lpr_cpe`), so this resolves in a wheel as well as in a source checkout.
DEFAULT_PACK_PATH: Final[Path] = Path(__file__).parent / "pack.yaml"

#: Environment override, so a deployment can mount a reviewed pack without rebuilding the image.
PACK_PATH_ENV_VAR: Final[str] = "LPR_POLICY_PACK_PATH"


class PolicyPackError(RuntimeError):
    """The pack could not be read, parsed or validated.

    Raised rather than swallowed, and this is the one place in the policy layer that raises. The
    engine catches it and fails **closed** -- every action blocked with
    `ReasonCode.POLICY_NO_MATCHING_RULE` -- which is a different and much louder failure than
    running with a half-loaded pack. See `engine.PolicyEngine.unavailable`.
    """


def resolve_pack_path(explicit: str | Path | None = None) -> Path:
    """Where the pack is, in precedence order: argument, environment, packaged default.

    The environment variable is checked before the default so an operator can point at a mounted
    pack without touching code, and after the argument so a test can be explicit and not be affected
    by
    the developer's shell.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get(PACK_PATH_ENV_VAR, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_PACK_PATH


def canonical_digest(parsed: Any) -> str:
    """A stable hash of parsed YAML content.

    `sort_keys=True` so a reordered mapping hashes identically -- mapping order carries no meaning
    in YAML, and a pack whose version changed because someone moved a section up would train people
    to ignore version changes. `default=str` rather than raising on an unexpected type: a hash
    function that refuses to hash is worse than one that stringifies a `date`, and the shape is
    validated immediately afterwards anyway.
    """
    blob = json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_pack(text: str, *, source: str = "<string>") -> PolicyPack:
    """Parse and validate pack text. The path-free half of `load_pack`, for tests.

    A test that needs a pack with one threshold changed builds the text and calls this, rather than
    writing a temporary file and defeating the cache. Every error mode below names `source`, because
    "1 validation error for PolicyPack" with no filename is unhelpful when three packs are mounted.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyPackError(f"{source} is not valid YAML: {exc}") from exc

    if raw is None:
        raise PolicyPackError(f"{source} is empty")
    if not isinstance(raw, dict):
        raise PolicyPackError(
            f"{source} must be a mapping at the top level, got {type(raw).__name__}"
        )
    if "content_hash" in raw:
        # The hash is derived, and a file that supplies its own would be able to claim a version it
        # does not have -- precisely the failure this module exists to prevent.
        raise PolicyPackError(
            f"{source} sets `content_hash`, which is derived from the file's content and must "
            "not be written by hand"
        )

    digest = canonical_digest(raw)
    try:
        return PolicyPack.model_validate({**raw, "content_hash": digest})
    except ValidationError as exc:
        raise PolicyPackError(f"{source} failed policy pack validation:\n{exc}") from exc


def _load_from_path(path: Path) -> PolicyPack:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyPackError(
            f"policy pack not found at {path}. Set {PACK_PATH_ENV_VAR} or restore the packaged "
            f"default at {DEFAULT_PACK_PATH}"
        ) from exc
    except OSError as exc:
        raise PolicyPackError(f"policy pack at {path} could not be read: {exc}") from exc
    return parse_pack(text, source=str(path))


@lru_cache(maxsize=8)
def _load_cached(path_str: str, _mtime_ns: int, _size: int) -> PolicyPack:
    """Cached by content-identifying key. The underscored arguments are cache keys, not inputs.

    They are in the signature rather than checked inside because that is what makes `lru_cache`
    invalidate on an edit: a changed mtime is a different key and therefore a fresh parse. Reading
    them inside the function would give a cache that never notices a change.
    """
    return _load_from_path(Path(path_str))


def load_pack(path: str | Path | None = None) -> PolicyPack:
    """Load, validate and cache the policy pack.

    Raises `PolicyPackError` on anything at all. Callers that must keep running -- the engine, the
    `/health` endpoint -- catch it; callers that are starting up should not, because a service that
    boots with an invalid policy pack is a service that will make its first decision wrongly.
    """
    resolved = resolve_pack_path(path)
    try:
        stat = resolved.stat()
    except OSError:
        # Let the reader produce the error: it has the better message, and stat failing for a reason
        # other than absence (a permission problem on a mounted secret, say) should say so rather
        # than be reported as "not found".
        return _load_from_path(resolved)
    return _load_cached(str(resolved), stat.st_mtime_ns, stat.st_size)


def policy_version(pack: PolicyPack) -> str:
    """`<declared>+<12 hex>`. The string that goes on every decision and audit event.

    Twelve hex characters is 48 bits: enough that two packs colliding is not a practical concern for
    a document edited by hand a few times a year, and short enough to read aloud on a call.
    """
    return f"{pack.version}+{pack.content_hash[:12]}"


def clear_pack_cache() -> None:
    """Forget cached packs. For tests, and for a deliberate mid-flight reload."""
    _load_cached.cache_clear()
