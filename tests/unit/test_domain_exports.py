"""The domain package's exports, checked against the specification's own list.

`domain/__init__.py` claims its `__all__` contains exactly the models the specification requires by
name, and that the list has 34 entries rather than the 33 a quick read suggests. Both are claims a
docstring cannot keep. This module parses the requirement out of `docs/specification.md` and
compares it, so adding a model to the package without the specification asking for it, or missing
one it does ask for, fails here.

The specification is vendored into `docs/` for this reason. Before that it lived only outside the
repository, which meant its own bullet list -- the thing every model, enum and node in this codebase
is answerable to -- could not be read by anything that runs in CI. A requirement no test can reach
is a requirement on trust.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lpr_cpe import domain

SPECIFICATION = Path(__file__).resolve().parents[2] / "docs" / "specification.md"

#: The heading the required-model list sits under, and the heading that ends it. Matched rather than
#: hard-coded by line number so re-ordering the specification does not silently empty the list.
_SECTION = "# Required domain models"
_BULLET = re.compile(r"^- `([A-Za-z_][A-Za-z0-9_]*)`\s*$")

#: Models this package defines that the specification does not name. Each is here to keep a required
#: model honest rather than to add a concept -- see the `domain/__init__.py` docstring. Listed
#: explicitly so a *new* unrequested model cannot appear without this test failing.
SUPPORTING = {
    "ActionRecord",
    "ActionRequest",
    "AuditEvent",
    "CrewSlot",
    "DispatchAssignment",
    "WifiRadioSnapshot",
}


def required_models() -> list[str]:
    """The specification's `Required domain models` bullet list, in the order it is written."""
    assert SPECIFICATION.is_file(), f"the specification is not vendored at {SPECIFICATION}"
    lines = SPECIFICATION.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == _SECTION), None)
    assert start is not None, f"{_SECTION!r} is no longer a heading in the specification"

    names: list[str] = []
    for line in lines[start + 1 :]:
        # Any following top-level heading ends the section. Without this the walk would run to the
        # end of the file and collect every backticked bullet in the document.
        if line.startswith("# "):
            break
        match = _BULLET.match(line.strip())
        if match:
            names.append(match.group(1))
    return names


def test_the_required_model_list_has_thirty_four_entries() -> None:
    """The number two docstrings in this repository previously got wrong, counted from the source."""
    names = required_models()
    assert len(names) == 34, names
    assert len(set(names)) == 34, "the specification names a model twice"


def test_every_required_model_is_exported() -> None:
    missing = [name for name in required_models() if name not in domain.__all__]
    assert missing == []


def test_every_required_model_is_importable_as_a_model() -> None:
    """In `__all__` is not the same as defined. A stale export would pass the test above."""
    from pydantic import BaseModel

    for name in required_models():
        model = getattr(domain, name, None)
        assert model is not None, f"{name} is exported but not defined"
        assert isinstance(model, type) and issubclass(model, BaseModel), f"{name} is not a model"


def test_no_unrequested_model_has_appeared() -> None:
    """Every exported model is either required by name or a declared supporting type.

    The point is the *declared* part. Six supporting models exist because the required ones need
    them; a seventh appearing without a decision would otherwise be indistinguishable from the six.
    """
    from pydantic import BaseModel

    exported_models = {
        name
        for name in domain.__all__
        if isinstance(getattr(domain, name, None), type)
        and issubclass(getattr(domain, name), BaseModel)
    }
    # The two abstract bases are machinery, not records.
    exported_models -= {"DomainModel", "FrozenDomainModel"}
    unexplained = exported_models - set(required_models()) - SUPPORTING
    assert unexplained == set(), f"undeclared models: {sorted(unexplained)}"


def test_the_vendored_specification_matches_the_original_when_it_is_available() -> None:
    """Vendoring created a second copy, so the copy is checked against the first where possible.

    One owner per fact: `docs/specification.md` is the owner for anything that runs in CI, but on a
    machine that still has the original the two must not have diverged. Skipped rather than failed
    when the original is absent -- its absence is the normal case everywhere except here.
    """
    original = Path.home() / "Downloads" / "LPR_CPE_Service_Assurance_LangGraph_Master_Prompt_v3.md"
    if not original.is_file():
        pytest.skip(f"the original specification is not present at {original}")
    assert SPECIFICATION.read_bytes() == original.read_bytes(), (
        "the vendored specification has drifted from the original"
    )
