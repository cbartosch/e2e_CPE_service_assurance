"""The audit bundle: run every gate, capture what it said, and record it where a test can read it.

The specification's deliverable 17 asks for "a final implementation report listing what was
implemented, assumptions made, commands run, test results, coverage, remaining vendor-integration
gaps, risks before production use". Six of those seven are prose. Three of them -- commands run,
test results, coverage -- are *numbers*, and this repository has a documented history of numbers in
prose going stale within the day: IMPLEMENTATION_PLAN.md §5 says so at length, and gap 7 is the
general form, "no gate reads the prose".

So the report does not carry those three. `audit/MANIFEST.json` carries them, this module writes it,
and `tests/unit/test_audit_bundle.py` fails the build when `docs/implementation-report.md` states a
figure the manifest does not. That is the smallest arrangement in which a stale number in the report
is a red test rather than a thing somebody notices in six months.

Why this is not a `cli.py` subcommand
--------------------------------------
`cli.py`'s own docstring: *"The commands report; they do not run an incident"*, and its parser
description promises it "reads no external system and writes to none". This command runs six
subprocesses and writes a directory. Adding it there would make that sentence false, and the
sentence is load-bearing -- it is why `lpr-cpe topology` is safe to run against anything.

It is a second `[project.scripts]` entry instead, which `tests/unit/test_cli.py` already covers
without being extended: that test reads the declarations out of the packaging metadata and imports
what each one names, precisely so a second entry point cannot ship unimportable.

What a bundle is, and what it is not
-------------------------------------
It is the raw stdout of each gate, byte for byte, plus a manifest naming the commit the run was made
against and whether the tree was clean at the time. It is **not** a claim that the gates pass: a
failing run produces a bundle too, with the failure in it and `all_gates_passed` false, because a
bundle that only existed when everything was green would be an artefact nobody could use to
investigate anything. The process exit code is what says whether the gates passed.

The one figure this module deliberately does not derive is a pass/fail *judgement* about the
project. `coverage_percent` is recorded; whether 85.33% is enough is a question for the report's
prose and for whoever reads it.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

#: The distributions whose versions the design depends on. IMPLEMENTATION_PLAN.md §2 pins observed
#: behaviour to these, so a bundle that did not record them would leave every §2 claim unfalsifiable
#: -- the measurements there are true of `langgraph` 1.2.11 and are not promises about 2.x.
PINNED: tuple[str, ...] = (
    "langgraph",
    "langgraph-checkpoint",
    "langchain-core",
    "pydantic",
    "fastapi",
    "psycopg",
)


@dataclass(frozen=True, slots=True)
class Gate:
    """One command, and what running it establishes.

    `argv` is given without the interpreter: `run_gate` prepends `sys.executable`, so a bundle is
    always produced by the interpreter that is running this module rather than by whichever `python`
    happens to be on the path. IMPLEMENTATION_PLAN.md §5 records that the two are *not* the same
    environment here and that it is easy to check the wrong one, which is exactly the mistake this
    removes rather than documents.
    """

    name: str
    argv: tuple[str, ...]
    proves: str


#: Every gate `make check` runs, plus the two reports that prove the graph compiles.
#:
#: `topology` earns its place because it is the cheapest honest check in the system:
#: `build_parent_graph` runs `_check_tables` before returning, so this fails on tables that disagree
#: with each other without needing a database, a network or a model provider. A bundle whose pytest
#: gate passed and whose topology gate did not would be a very specific and very interesting
#: failure, and there is no reason to make an auditor run it separately to find out.
GATES: tuple[Gate, ...] = (
    Gate(
        "ruff-check",
        ("-m", "ruff", "check", "src", "tests"),
        "no lint finding in the shipped source or its tests",
    ),
    Gate(
        "ruff-format",
        ("-m", "ruff", "format", "--check", "src", "tests"),
        "every file is formatted as the committed configuration formats it",
    ),
    Gate(
        "mypy",
        ("-m", "mypy"),
        "strict-mode type checking over src/lpr_cpe, with pydantic's plugin",
    ),
    Gate(
        "pytest",
        ("-m", "pytest", "--cov", "--cov-report=term-missing", "--cov-fail-under=85"),
        "the committed suite, behind the coverage gate `make test` uses",
    ),
    Gate(
        "topology",
        ("-m", "lpr_cpe.cli", "topology"),
        "the parent graph compiles, so its four topology tables agree",
    ),
    Gate(
        "config",
        ("-m", "lpr_cpe.cli", "config"),
        "the settings this process would run under, with no secret printed",
    ),
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """What one gate did. `output` is stdout and stderr interleaved, exactly as they arrived."""

    gate: Gate
    exit_code: int
    output: str
    seconds: float

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


# ------------------------------------------------------------------------------------------------
# Parsing what the gates said
# ------------------------------------------------------------------------------------------------

# Anchored to the shapes the tools actually emit, captured on 2026-08-24 and quoted in
# `tests/unit/test_audit_bundle.py` so that a tool changing its wording fails a test that shows the
# old wording beside the new, rather than silently recording `None`.
# Not anchored to the start of a line, and that is the whole of what this comment is for. It was
# `^(\d+) passed`, which reads a green run's `924 passed in 83.69s` and records `None` for a red
# one's `1 failed, 933 passed in 85.56s` -- so the manifest lost its test count at exactly the
# moment somebody would open it to find out what broke. Found on the bundle's second run, by the
# bundle.
_TESTS_PASSED = re.compile(r"(\d+) passed\b")
_TESTS_FAILED = re.compile(r"(\d+) failed\b")
_COVERAGE = re.compile(r"Total coverage: ([\d.]+)%")
_COVERAGE_GATE = re.compile(r"Required test coverage of ([\d.]+)%")
_FILES_FORMATTED = re.compile(r"(\d+) files? already formatted")
_MYPY_FILES = re.compile(r"no issues found in (\d+) source files?")


def _search_int(pattern: re.Pattern[str], text: str) -> int | None:
    found = pattern.search(text)
    return int(found.group(1)) if found else None


def _search_float(pattern: re.Pattern[str], text: str) -> float | None:
    found = pattern.search(text)
    return float(found.group(1)) if found else None


def figures_from(results: dict[str, GateResult]) -> dict[str, Any]:
    """The countable claims, pulled out of the raw output rather than typed in.

    `None` where a gate did not run or did not say -- deliberately, and not zero. A failing pytest
    run prints no `N passed` line in the shape this reads, and recording `0` would put a number in
    the manifest that reads as "the suite has no tests" rather than "the suite did not finish".
    The report's figure table is compared against this mapping key by key, so a `None` there fails
    the comparison rather than quietly matching a prose claim.
    """
    pytest_out = results["pytest"].output if "pytest" in results else ""
    ruff_fmt = results["ruff-format"].output if "ruff-format" in results else ""
    mypy_out = results["mypy"].output if "mypy" in results else ""
    return {
        "tests_passing": _search_int(_TESTS_PASSED, pytest_out),
        # `0` rather than `None` when the summary parsed and named no failures, because "the run
        # finished and nothing failed" is a measurement. `None` stays for a run that said nothing at
        # all, which is the case the report comparison must refuse.
        "tests_failed": (
            _search_int(_TESTS_FAILED, pytest_out)
            or (0 if _TESTS_PASSED.search(pytest_out) else None)
        ),
        # Collected, not passing, and this is the figure the report is allowed to state.
        #
        # The bundle writes the manifest *after* pytest, so the suite always compares the report
        # against the **previous** run's manifest. Any figure that moves when the suite goes green
        # therefore cannot converge: with the report saying 935 and a stale manifest saying 934,
        # the comparison fails, which keeps the manifest at 934 -- a fixed point at the wrong
        # number. Measured, not reasoned about: two runs in a row sat there.
        #
        # `tests_total` does not move. A test that fails is still collected, so this is the same
        # number on a red run and a green one, and it is the one figure of the three that a
        # self-referential gate can safely hold the prose to. `tests_passing` and `tests_failed`
        # stay in the manifest for whoever is reading it, and out of the report.
        "tests_total": (
            None
            if _search_int(_TESTS_PASSED, pytest_out) is None
            else (_search_int(_TESTS_PASSED, pytest_out) or 0)
            + (_search_int(_TESTS_FAILED, pytest_out) or 0)
        ),
        "coverage_percent": _search_float(_COVERAGE, pytest_out),
        "coverage_gate_percent": _search_float(_COVERAGE_GATE, pytest_out),
        "files_formatted": _search_int(_FILES_FORMATTED, ruff_fmt),
        "source_files_typechecked": _search_int(_MYPY_FILES, mypy_out),
    }


# ------------------------------------------------------------------------------------------------
# Running them
# ------------------------------------------------------------------------------------------------


def run_gate(gate: Gate, repo: Path) -> GateResult:
    """One gate, its output captured whole. Never raises on a non-zero exit; that is data."""
    started = time.monotonic()
    # `argv` is a module constant and never user input, so there is no injection surface here. Said
    # in a comment rather than a `noqa`: the `S` rules are not enabled in this project, so a
    # suppression naming `S603` would be an unused directive -- which RUF100 reports, and which
    # IMPLEMENTATION_PLAN.md §2 already records as a defect class in its own right, a suppression
    # pointed at an error nobody is emitting.
    proc = subprocess.run(
        [sys.executable, *gate.argv],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return GateResult(
        gate=gate,
        exit_code=proc.returncode,
        output=proc.stdout + proc.stderr,
        seconds=round(time.monotonic() - started, 2),
    )


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            # `git` is resolved from PATH by design: a bundle is generated from a working checkout
            # by whoever is standing in front of it, and hard-coding an absolute path would make
            # this work on one machine. Absent git is handled below rather than assumed away.
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in PINNED:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            # Recorded as absent rather than omitted. `psycopg` is an optional extra and its absence
            # is a fact about the run, not a hole in the manifest.
            out[name] = None
    return out


def build_manifest(repo: Path, results: dict[str, GateResult]) -> dict[str, Any]:
    """Everything needed to say what was run, against what, and what it said.

    `commit` and `dirty` are recorded together and both matter. A bundle generated against a dirty
    tree describes a state that is not in the history and cannot be reproduced from the sha alone;
    saying so is the difference between evidence and an assertion.
    """
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git(repo, "rev-parse", "HEAD") or None,
        "dirty": bool(_git(repo, "status", "--porcelain")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "interpreter": sys.executable,
        "packages": _versions(),
        "gates": [
            {
                "name": result.gate.name,
                "command": " ".join(("python", *result.gate.argv)),
                "proves": result.gate.proves,
                "exit_code": result.exit_code,
                "seconds": result.seconds,
                "output_file": f"latest/{result.gate.name}.txt",
            }
            for result in results.values()
        ],
        "figures": figures_from(results),
        "all_gates_passed": all(result.passed for result in results.values()),
    }


def write_bundle(repo: Path, results: dict[str, GateResult], manifest: dict[str, Any]) -> Path:
    """Write `audit/latest/*.txt` and `audit/MANIFEST.json`, and return the bundle directory.

    One directory, overwritten each run, rather than one per timestamp. The history of these bundles
    is the git history of this directory, which is a better record than a pile of dated folders: it
    diffs, it is bisectable, and it cannot accumulate.
    """
    bundle = repo / "audit"
    latest = bundle / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for result in results.values():
        (latest / f"{result.gate.name}.txt").write_text(result.output, encoding="utf-8")
    (bundle / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return bundle


def repo_root() -> Path:
    """The repository this package was installed from, found by walking up to `pyproject.toml`.

    Walked rather than assumed, because an editable install puts `src/lpr_cpe/` inside the checkout
    while a wheel install does not -- and in the second case there is no repository to audit, which
    this reports as an error rather than writing a bundle into `site-packages`.
    """
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError(
        "no pyproject.toml above this module, so there is no repository to audit. `lpr-cpe-audit` "
        "runs the project's own gates and only means anything from a source checkout."
    )


def main(argv: list[str] | None = None) -> int:
    """Run every gate, write the bundle, and exit non-zero if any gate failed."""
    parser = argparse.ArgumentParser(
        prog="lpr-cpe-audit",
        description=(
            "Run every gate `make check` runs, capture the output verbatim into audit/, and write "
            "audit/MANIFEST.json. Exits non-zero if any gate failed."
        ),
    )
    parser.parse_args(argv)

    repo = repo_root()
    results: dict[str, GateResult] = {}
    for gate in GATES:
        sys.stdout.write(f"  {gate.name:<14} ")
        sys.stdout.flush()
        result = run_gate(gate, repo)
        results[gate.name] = result
        sys.stdout.write(f"{'ok  ' if result.passed else 'FAIL'}  {result.seconds:>6.2f}s\n")

    manifest = build_manifest(repo, results)
    bundle = write_bundle(repo, results, manifest)
    figures = manifest["figures"]
    sys.stdout.write(f"\nbundle {bundle}\n")
    sys.stdout.write(
        f"  {figures['tests_passing']} tests, {figures['coverage_percent']}% coverage, "
        f"{figures['source_files_typechecked']} source files typechecked\n"
    )
    if manifest["dirty"]:
        sys.stdout.write("  tree was dirty: these results are not reproducible from the commit\n")
    return 0 if manifest["all_gates_passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
