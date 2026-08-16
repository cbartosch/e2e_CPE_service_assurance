"""The console script, tested through the declaration rather than through the import.

`pyproject.toml` declared `lpr-cpe = "lpr_cpe.cli:main"` from the initial commit and no `cli.py`
was ever written, in this or in any earlier revision -- `git log --all --diff-filter=D -- "*cli*"`
returns nothing, so the module was never deleted; it was never there. That survived a green suite
for the whole of the project's history because nothing imports an entry point unless it is asked
to. `pip install .` succeeded, hatch wrote the script, and `lpr-cpe` raised `ModuleNotFoundError`
in the user's shell.

So the first test here reads the declaration out of the packaging metadata rather than importing
`lpr_cpe.cli` by a name it hard-codes. Importing the module directly would prove the module exists,
which was never the question -- the question is whether the string in `pyproject.toml` names
something that resolves, and only pyproject can answer that. A second entry point added later is
covered by the same test without anyone remembering to extend it.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

from lpr_cpe.cli import REPORTS, main
from lpr_cpe.graph.builder import BRANCH_TARGETS, PENDING_STAGES

REPO_ROOT = Path(__file__).resolve().parents[2]


def _declared_scripts() -> dict[str, str]:
    """The `[project.scripts]` table, read from the file that ships it."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        metadata: dict[str, Any] = tomllib.load(handle)
    scripts: dict[str, str] = metadata["project"].get("scripts", {})
    return scripts


def test_pyproject_is_where_the_scripts_are_declared() -> None:
    """A positive control for the guard below, which would otherwise pass over an empty table.

    `test_every_declared_console_script_resolves` loops over `_declared_scripts()`. Delete the
    `[project.scripts]` block and that loop has nothing to iterate, so it passes -- proving nothing
    while looking exactly like a test that proved something. That is not hypothetical: deleting the
    block was tried, and the guard next door passed, while this test failed::

        E   AssertionError: pyproject.toml declares no [project.scripts]. If that is deliberate,
            delete this test and the one below; if it is not, the console script has gone missing.
        E   assert {}
        E    +  where {} = _declared_scripts()

    If the system is ever made library-only on purpose, this is the assertion to delete, and
    deleting it is then a visible decision rather than a silently hollowed-out guard.
    """
    assert _declared_scripts(), (
        "pyproject.toml declares no [project.scripts]. If that is deliberate, delete this test "
        "and the one below; if it is not, the console script has gone missing."
    )


def test_every_declared_console_script_resolves() -> None:
    """Every `[project.scripts]` target imports and is callable.

    This is the guard that was missing, and it was watched to fail before it was trusted. Both
    mutations below leave `cli.py` in place, so the other five tests in this file passed while this
    one failed: the guard is specific to the declaration, not merely sensitive to the module being
    gone.

    Pointing the declaration at a module that does not exist -- the shipped defect, in the one
    place it ever lived::

        lpr-cpe = "lpr_cpe.cli_missing:main"

        E   ModuleNotFoundError: No module named 'lpr_cpe.cli_missing'

    and the same class of defect on the other side of the colon, module intact, function renamed::

        lpr-cpe = "lpr_cpe.cli:main_"

        E   AssertionError: module 'lpr_cpe.cli' has no attribute 'main_'. 'lpr_cpe.cli' declared
            as the target of console script 'lpr-cpe'
        E   assert None is not None

    The literal original -- the committed declaration with `cli.py` moved aside -- was reinstated
    too, and does *not* produce a failure of this test. It produces a collection error, because
    this module imports `lpr_cpe.cli` at the top::

        E   ModuleNotFoundError: No module named 'lpr_cpe.cli'
        ERROR tests/unit/test_cli.py                                    (pytest exit code 4)

    That is worth knowing rather than tidying away: a collection error is reported differently from
    a failure and skips every test in the file, so the two mutations above are the ones that
    demonstrate this assertion specifically. The other 762 tests passed with `cli.py` absent, which
    is the check that nothing else was quietly made to depend on it.
    """
    scripts = _declared_scripts()
    for name, target in scripts.items():
        module_name, separator, attribute = target.partition(":")
        assert separator, f"console script {name!r} is {target!r}, which names no attribute"

        module = importlib.import_module(module_name)
        entry_point = getattr(module, attribute, None)
        assert entry_point is not None, (
            f"module {module_name!r} has no attribute {attribute!r}. "
            f"{module_name!r} declared as the target of console script {name!r}"
        )
        assert callable(entry_point), f"{target} resolves to {entry_point!r}, which is not callable"


#: One line that appears in a given report and in no other. Deliberately not the section's own
#: name: `"topology" in printed` is satisfied by the node `resolve_identity_and_topology`, so a
#: report keyed on its name would pass whether or not that report ever ran.
_ANCHORS = {"topology": "graph lpr_cpe_parent", "config": "app_mode "}


def test_a_bare_invocation_prints_every_report(capsys: pytest.CaptureFixture[str]) -> None:
    """`lpr-cpe` with no section prints both, and every report has an anchor here to prove it.

    `[]` and not `None`: `argparse` falls back to `sys.argv[1:]` when handed `None`, which under
    pytest is pytest's own command line. `[]` is how a test says "no arguments"; the shipped
    console script reaches the same branch by calling `main()`.
    """
    assert set(_ANCHORS) == set(REPORTS), (
        "a report was added or renamed without an anchor, so this test would stop checking it"
    )

    assert main([]) == 0

    printed = capsys.readouterr().out
    for section, anchor in _ANCHORS.items():
        assert anchor in printed, f"a bare invocation did not reach the {section!r} report"


def test_topology_names_every_answer_the_builder_declares(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No declared answer may be missing from the report, and four of them nearly were.

    `compile_parent_graph().get_graph()` renders for drawing and keeps one edge per
    `(source, target)`, so an answer that shares a destination with an earlier one is dropped. The
    first version of `report_topology` listed edges from that rendering and silently lost
    `quarantine`, `manual_review`, `preventive` and `approve_low_confidence` -- all four share
    `END` with the escalation edge -- along with D03's `continue`, which `builder` documents as
    deliberately distinct from `associate`. Two of the four are `PENDING_STAGES` exits, so the
    report was hiding the branches it most needed to name.

    Asserted over `BRANCH_TARGETS` rather than a written-out list, so a seventh decision or a new
    answer is covered the day it is added.

    Watched to fail twice. Reverting `report_topology` to list `get_graph().edges` fails on the
    decision identifier, because that version prints no identifiers at all::

        E   AssertionError: D01 is wired but absent from the report

    which shows the test notices the rewrite but not that it notices a *dropped answer*. So the
    lossy step was reproduced on its own instead -- skipping any answer whose target is `END`,
    which is exactly what the rendering's per-`(source, target)` dedupe amounts to::

        E   AssertionError: D01's 'quarantine' branch is absent from the report

    Only this test failed under either mutation.
    """
    assert main(["topology"]) == 0

    printed = capsys.readouterr().out
    for identifier, targets in BRANCH_TARGETS.items():
        assert identifier in printed, f"{identifier} is wired but absent from the report"
        for answer in targets:
            assert answer in printed, f"{identifier}'s {answer!r} branch is absent from the report"

    for exit_point in PENDING_STAGES:
        assert exit_point in printed, f"{exit_point} reaches END unwired but is not reported"


def test_config_reports_the_write_gate_without_printing_a_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derived gate is reported; the DSN and the webhook secret are not.

    A topology-and-config dump is the kind of output that gets pasted into a ticket, and
    `postgres_dsn` routinely carries a password. `writes_permitted` is the answer worth printing
    because it is the one `WriteGate` actually asks, and it is not readable off either switch
    alone -- both are set here so the report is exercised in the state where it says `True`.

    Watched to fail by adding the obvious line back to `report_config`::

        out.write(f"  postgres_dsn {settings.postgres_dsn}\n")

        E   AssertionError: assert 'hunter2' not in 'config\\n  a...c_cycles 3\\n'

    Note what that truncated repr means for a real leak: pytest elides the middle of a long string,
    so the password would not have appeared in the failure output either. The assertion has to name
    the secret it is looking for, which is why the DSN here is a literal with a recognisable
    password rather than a realistic-looking one.
    """
    from lpr_cpe.config import reset_settings_cache

    monkeypatch.setenv("LPR_APP_MODE", "production")
    monkeypatch.setenv("LPR_ALLOW_PRODUCTION_WRITES", "true")
    monkeypatch.setenv("LPR_POSTGRES_DSN", "postgresql://lpr:hunter2@db.internal:5432/lpr")
    monkeypatch.setenv("LPR_WEBHOOK_SECRET", "s3cret-token")
    reset_settings_cache()

    try:
        assert main(["config"]) == 0
        printed = capsys.readouterr().out
    finally:
        reset_settings_cache()

    assert "writes_permitted True" in printed
    assert "postgres_enabled True" in printed
    assert "hunter2" not in printed
    assert "db.internal" not in printed
    assert "s3cret-token" not in printed


def test_an_unknown_section_fails_rather_than_reporting_nothing() -> None:
    """`argparse` exits 2 and names the sections that exist.

    This is the behaviour the Makefile's `demo` target meets, and it is why `cli.py` has no `demo`
    command: the scenarios are not written, and a command that printed nothing and exited zero
    would look like a demonstration that had run.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["demo"])

    assert exit_info.value.code == 2
