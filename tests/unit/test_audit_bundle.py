"""The audit bundle, and the gate that stops the report drifting away from it.

IMPLEMENTATION_PLAN.md gap 7 is "no gate reads the prose", and names the two cheapest gates that
would close it: a test that asserts every path a markdown file names exists, and a test that ties a
document's figures to something measured. Both are here. The first is
`test_every_relative_link_in_every_markdown_file_resolves`; the second is
`test_the_report_states_no_figure_the_manifest_does_not`, which is the reason `audit/MANIFEST.json`
is committed rather than gitignored -- a manifest nothing reads back is a log file, and the point of
this one is that a stale number in `docs/implementation-report.md` is a red test.

The parsers are asserted against **captured real output**, quoted inline, rather than against
strings written to match the regex. A parser test written from the regex is a test of the regex.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from lpr_cpe.audit import (
    GATES,
    Gate,
    GateResult,
    build_manifest,
    figures_from,
    repo_root,
)

REPO = repo_root()
MANIFEST_PATH = REPO / "audit" / "MANIFEST.json"
REPORT_PATH = REPO / "docs" / "implementation-report.md"


# ------------------------------------------------------------------------------------------------
# The parsers, against output the tools really produced
# ------------------------------------------------------------------------------------------------

#: Captured 2026-08-24 from `python -m pytest --cov --cov-report=term-missing --cov-fail-under=85`.
#: Both the pass and fail wordings are here because they are different sentences and the parser has
#: to read the number out of either -- a bundle for a failing run is the one somebody investigating
#: actually needs.
PYTEST_PASSING = """\
---------------------------------------------------------------------------------------------------
TOTAL                                                    9699   1057   2530    417  85.33%
Required test coverage of 85% reached. Total coverage: 85.33%
924 passed in 83.69s (0:01:23)
"""

PYTEST_FAILING = """\
---------------------------------------------------------------------------------------------------
TOTAL                                                    9796   1111   2542    417  84.92%
FAIL Required test coverage of 85% not reached. Total coverage: 84.92%
924 passed in 78.23s (0:01:18)
"""

RUFF_FORMAT = "136 files already formatted\n"
MYPY = "Success: no issues found in 110 source files\n"


def _results(**outputs: str) -> dict[str, GateResult]:
    """A result set built from raw output, with the gate objects the module really declares."""
    by_name = {gate.name: gate for gate in GATES}
    return {
        name: GateResult(gate=by_name[name], exit_code=0, output=text, seconds=0.0)
        for name, text in outputs.items()
    }


def test_the_figures_are_read_out_of_real_tool_output() -> None:
    """Every figure the manifest carries, parsed from output the tools actually emitted.

    Watched red by changing `Total coverage: ` to `Coverage: ` in the module's regex, which is the
    shape of the failure a pytest-cov release would cause::

        assert None == 85.33
    """
    figures = figures_from(
        _results(pytest=PYTEST_PASSING, **{"ruff-format": RUFF_FORMAT}, mypy=MYPY)
    )
    assert figures == {
        "tests_passing": 924,
        "tests_failed": 0,
        "tests_total": 924,
        "coverage_percent": 85.33,
        "coverage_gate_percent": 85.0,
        "files_formatted": 136,
        "source_files_typechecked": 110,
    }


def test_a_failing_coverage_run_still_yields_its_numbers() -> None:
    """The FAIL wording is a different sentence, and the parser has to read both.

    This is not symmetry for its own sake. The bundle exists to be read when something went wrong,
    and a manifest that recorded `null` for coverage exactly when coverage was the problem would be
    useless at the only moment it mattered.
    """
    figures = figures_from(_results(pytest=PYTEST_FAILING))
    assert figures["coverage_percent"] == 84.92
    assert figures["coverage_gate_percent"] == 85.0
    assert figures["tests_passing"] == 924
    assert figures["tests_failed"] == 0
    assert figures["tests_total"] == 924


#: Captured 2026-08-24 from the bundle's own second run, which is where this shape was found.
#: pytest writes a green run's count at the start of the line and a red run's after the failures,
#: so a parser anchored with `^` reads the first and loses the second.
PYTEST_RED_SUMMARY = """\
=========================== short test summary info ===========================
FAILED tests/unit/test_audit_bundle.py::test_the_report_states_no_figure_the_manifest_does_not
1 failed, 933 passed in 85.56s (0:01:25)
"""


def test_the_count_survives_a_failing_run() -> None:
    """A red run's summary puts the passing count second, and the manifest still has to hold it.

    This is the regression for a defect the bundle found in itself. `^(\\d+) passed` read
    `924 passed in 83.69s` and recorded `None` for `1 failed, 933 passed in 85.56s` -- so the run
    that someone would actually open the manifest to investigate was the one run whose test count
    it had thrown away.

    Watched red by restoring the `^` anchor::

        assert None == 933
    """
    figures = figures_from(_results(pytest=PYTEST_RED_SUMMARY))
    assert figures["tests_passing"] == 933
    assert figures["tests_failed"] == 1
    assert figures["tests_total"] == 934, "a failing test is still a collected test"


def test_a_gate_that_did_not_run_records_none_rather_than_zero() -> None:
    """`None` and `0` mean different things and only one of them is true here.

    `0` would read as "the suite has no tests"; `None` reads as "the suite did not say". The report
    comparison below treats `None` as a mismatch against any stated figure, so a gate that silently
    stopped emitting its summary line fails the build rather than agreeing with the prose.
    """
    figures = figures_from(_results(pytest="collected 0 items\n"))
    assert figures["tests_passing"] is None
    assert figures["tests_failed"] is None
    assert figures["tests_total"] is None
    assert figures["coverage_percent"] is None
    assert figures["files_formatted"] is None


def test_the_manifest_records_whether_the_tree_was_clean() -> None:
    """A bundle from a dirty tree is not reproducible from its commit, and has to say so.

    Asserted on shape rather than value -- whether *this* tree is dirty depends on who is running
    the suite -- but both keys must be present and typed, because the report's own reproducibility
    claim is read off them.
    """
    manifest = build_manifest(REPO, _results(pytest=PYTEST_PASSING))
    assert isinstance(manifest["dirty"], bool)
    assert manifest["commit"] is None or re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
    assert manifest["all_gates_passed"] is True
    assert {entry["name"] for entry in manifest["gates"]} == {"pytest"}


def test_every_declared_gate_names_a_command_and_what_it_proves() -> None:
    """No gate may be added without saying why it is in the bundle.

    `proves` is prose and cannot be checked for truth, but it can be checked for existence, and the
    failure it prevents is a bundle that grows a command nobody can account for.
    """
    assert GATES, "a bundle with no gates would pass vacuously"
    for gate in GATES:
        assert isinstance(gate, Gate)
        assert gate.argv, f"{gate.name} has no command"
        assert gate.proves.strip(), f"{gate.name} does not say what it proves"
    assert len({gate.name for gate in GATES}) == len(GATES), "two gates share a name"


# ------------------------------------------------------------------------------------------------
# The gate on the prose
# ------------------------------------------------------------------------------------------------

#: The report states its countable claims in one table and nowhere else, so that this test has a
#: single thing to compare. The row shape is `| key | value | note |`.
_FIGURE_ROW = re.compile(r"^\|\s*`(?P<key>[a-z_]+)`\s*\|\s*(?P<value>[\d.]+)\s*\|", re.MULTILINE)


def _manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        pytest.fail(
            f"{MANIFEST_PATH} is missing. It is a committed artefact, not a build output: "
            "run `make audit`, and commit what it writes."
        )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_report_states_no_figure_the_manifest_does_not() -> None:
    """Gap 7's second gate: a number in the report has to have been measured.

    The report's figure table is the single owner of every countable claim it makes, and this
    compares that table key by key against `audit/MANIFEST.json`. Editing one without re-running
    `make audit` is a red test, which is the whole arrangement: the plan records four separate
    occasions on which a figure in prose was correct when written and stale within the day, and
    nothing noticed any of them.

    Watched red by editing the report's `tests_passing` row by one::

        AssertionError: docs/implementation-report.md says tests_passing = 925.0, the manifest
        measured 924. Re-run `make audit` and update the report, or revert the prose.
    """
    manifest = _manifest()
    figures = manifest["figures"]
    text = REPORT_PATH.read_text(encoding="utf-8")
    stated = {m.group("key"): float(m.group("value")) for m in _FIGURE_ROW.finditer(text)}

    assert stated, (
        "the report states no figures at all, so this test proves nothing. The table it reads is "
        "described in `_FIGURE_ROW`; if the report's shape changed, change this with it."
    )
    for key, value in sorted(stated.items()):
        assert key in figures, (
            f"{REPORT_PATH.name} states `{key}`, which the manifest does not measure. Either the "
            "figure is invented or `audit.figures_from` needs to learn it."
        )
        measured = figures[key]
        assert measured is not None, (
            f"{REPORT_PATH.name} states `{key}` = {value}, but the manifest measured nothing for "
            "it -- the gate that produces it did not report. A stated figure with no measurement "
            "behind it is exactly what this test exists to refuse."
        )
        assert float(measured) == value, (
            f"{REPORT_PATH.name} says {key} = {value}, the manifest measured {measured}. Re-run "
            "`make audit` and update the report, or revert the prose."
        )


def test_the_manifest_says_which_commit_and_whether_it_was_clean() -> None:
    """The committed manifest has to be self-describing, or the bundle is an assertion.

    Deliberately does **not** require the manifest's commit to equal `HEAD`. A bundle is generated
    before the commit that carries it, so its sha is always the parent's; requiring equality would
    make the test unsatisfiable rather than strict.
    """
    manifest = _manifest()
    for key in ("generated_at", "commit", "dirty", "python", "packages", "gates", "figures"):
        assert key in manifest, f"the committed manifest has no {key!r}"
    assert manifest["packages"]["langgraph"], (
        "the manifest records no langgraph version, so IMPLEMENTATION_PLAN.md §2's measured "
        "behaviour is pinned to nothing"
    )


# ------------------------------------------------------------------------------------------------
# Gap 7's first gate: the links
# ------------------------------------------------------------------------------------------------

#: Markdown inline links, `[text](target)`. Reference-style links and bare URLs are out of scope and
#: none are used here; if one is added this finds nothing rather than misreporting, which is why the
#: count assertion below exists.
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: Directories that are not ours to audit.
_SKIP = {".venv", "venv", "node_modules", ".git", "htmlcov", "build", "dist"}


def _markdown_files() -> list[Path]:
    return sorted(
        path
        for path in REPO.rglob("*.md")
        if not any(part in _SKIP for part in path.relative_to(REPO).parts)
    )


def test_every_relative_link_in_every_markdown_file_resolves() -> None:
    """Gap 7's first gate, named there as unwritten since 2026-08-16.

    "A test that asserts every path a markdown file names exists, and every relative link resolves."
    This is the second half. It is worth having on its own evidence: the README once linked to
    `docs/operations-runbook.md`, which has never existed, and the link survived every green run in
    this repository because nothing read it.

    External links are skipped -- checking them needs a network, and a gate that fails when GitHub
    is slow is a gate people learn to ignore. Anchors are stripped and not verified; a missing
    heading is a much smaller lie than a missing file.
    """
    files = _markdown_files()
    assert files, "no markdown files found, so this test proves nothing"

    broken: list[str] = []
    checked = 0
    for path in files:
        for target in _LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(REPO)} -> {target}")

    assert checked, "no relative links were found, so this test proves nothing"
    assert not broken, "these markdown links point at nothing:\n  " + "\n  ".join(broken)


#: Markdown files whose paths are not this repository's claims to keep.
#:
#: `specification.md` is the vendored input, not a deliverable -- IMPLEMENTATION_PLAN.md §5 says so
#: where it declines to count it among the nine documents. It names eight documents the project is
#: *asked* to produce, so resolving its paths would turn "we have not written the runbook yet" into
#: a failing test, which is a status this section already tracks honestly and does not need to
#: relearn as a lint error.
_NOT_OUR_PROSE = {"specification.md"}

#: Paths that belong to somebody else's tree. Each is named in prose because a measurement was taken
#: against it, which is exactly the kind of citation this repository wants to keep.
_EXTERNAL_PATHS = {
    # LangGraph's own renderer, cited in `docs/dashboard-architecture.md` where the xray drawing was
    # measured. It lives in site-packages and its absence from this tree is not a defect.
    "pregel/_draw.py",
    # LangGraph's resume handling, quoted in IMPLEMENTATION_PLAN.md §2 for the `Command(resume={})`
    # no-op -- the `all()` over an empty dict that makes an empty map resume nothing.
    "langgraph/pregel/_loop.py",
}

#: Paths this documentation names **because they do not exist**, each with the gap that owns it.
#:
#: A gate that refused every unresolvable path would make it impossible to write down what is
#: missing, which is most of what this repository's prose is for. So the exception is declared here
#: rather than inferred, and it is checked in *both* directions -- exactly as
#: `builder._check_pending_stages` checks `PENDING_STAGES`. An entry whose path now exists is a
#: failure, because the day `src/lpr_cpe/api/` is written is the day three documents describing its
#: absence become wrong and nothing else would notice.
_DECLARED_MISSING = {
    # §5 row `api`, pending. Named by README's Layout table, the dashboard design and the plan.
    "src/lpr_cpe/api/",
    # Two of the eight documents the specification asks for and §5 counts as unwritten.
    "docs/policy-controls.md",
    "docs/operations-runbook.md",
    "docs/architecture-decisions/",
}


def _tree_paths() -> set[str]:
    """Every file and directory in the repository, as `/`-joined paths relative to the root."""
    out: set[str] = set()
    for path in REPO.rglob("*"):
        parts = path.relative_to(REPO).parts
        if any(part in _SKIP for part in parts):
            continue
        joined = "/".join(parts)
        out.add(joined + "/" if path.is_dir() else joined)
    return out


def _resolves(target: str, tree: set[str]) -> bool:
    """Does this documented path name something in the tree, under any writing convention?

    Matched by *suffix on a component boundary* rather than against a list of root directories.
    Both conventions are in use and both are correct -- the README and the plan write
    `src/lpr_cpe/graph/builder.py`, while a module docstring writes `subgraphs/field_planning.py`
    next to a sentence about `graph.subgraphs`, which reads better and is no less precise. Listing
    the roots that make each one resolve meant maintaining a list that grew every time a document
    referred to a new package; taking the suffix asks the tree instead, and the component boundary
    is what stops `jtrack/simulator.py` being satisfied by `tmf/simulator.py`.
    """
    return target in tree or any(known.endswith("/" + target) for known in tree)


def test_every_backticked_repository_path_in_the_docs_exists() -> None:
    """The other half of gap 7's first gate: a path *named* in prose, not linked.

    Restricted to backticked strings that contain a `/` and end in a known source extension, which
    is narrow on purpose. `graph.builder` and `route_dispatch_gate` are backticked too and are not
    paths; a rule that tried to resolve those would produce so much noise that the real finding --
    a Layout table naming `src/lpr_cpe/api/`, which does not exist -- would be lost in it.

    Directories are matched too, with a trailing slash, because that is the form the API row was
    written in and the form the finding took.
    """
    named = re.compile(r"`((?:[\w.\-]+/)+[\w.\-]*(?:\.py|\.md|\.toml|\.yaml|\.yml|/))`")
    tree = _tree_paths()
    broken: list[str] = []
    checked = 0
    for path in _markdown_files():
        if path.name in _NOT_OUR_PROSE:
            continue
        for target in named.findall(path.read_text(encoding="utf-8")):
            if target in _EXTERNAL_PATHS or target in _DECLARED_MISSING:
                continue
            checked += 1
            if not _resolves(target, tree):
                broken.append(f"{path.relative_to(REPO)} names `{target}`")

    assert checked, "no backticked paths were found, so this test proves nothing"
    assert not broken, (
        "these paths are named in the documentation and do not exist. If the path is a gap the "
        "prose is describing, declare it in `_DECLARED_MISSING` with the gap that owns it:\n  "
        + "\n  ".join(broken)
    )


def test_nothing_declared_missing_has_quietly_been_built() -> None:
    """The other direction, and the one nothing else in the repository would catch.

    `_DECLARED_MISSING` exists so that prose may name what does not exist. The failure it introduces
    is the mirror of the one `builder._check_pending_stages` guards: an entry that stops being true.
    The day `src/lpr_cpe/api/` is written, three documents describing its absence become wrong, and
    a one-way check would go on excusing every one of them.

    Watched red by adding `src/lpr_cpe/graph/` to the table::

        AssertionError: these paths are declared missing and exist: ['src/lpr_cpe/graph/'].
    """
    tree = _tree_paths()
    built = sorted(target for target in _DECLARED_MISSING if _resolves(target, tree))
    assert not built, (
        f"these paths are declared missing and exist: {built}. Delete the entry, and check the "
        "documents that describe them as unbuilt -- they are now wrong."
    )
