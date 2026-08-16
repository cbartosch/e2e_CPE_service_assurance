"""The `lpr-cpe` console script, and the reason it is more than a courtesy.

`pyproject.toml`'s `[project.scripts]` names `lpr_cpe.cli:main`. Nothing imports an entry point
during a test run, so a declaration naming a module that does not exist is not a build error, not a
lint error and not a test failure -- it is a `ModuleNotFoundError` in the user's shell, after
`pip install` reported success. That is the state this module was written to end, and
`tests/unit/test_cli.py` keeps it ended by reading the declaration out of the packaging metadata and
importing what it names, rather than importing this module under a name it hard-codes.

The commands report; they do not run an incident. Compiling the parent graph is the cheapest honest
check this system has, because `build_parent_graph` runs `_check_tables` before it returns anything:
`topology` therefore fails on tables that disagree without a database, a network or a model
provider. What it then prints comes from the compiled graph where that is the complete answer and
from `builder`'s own tables where it is not -- see `report_topology`, which documents the one place
the rendering is lossy and would otherwise have hidden four declared answers.

There is deliberately no `demo` command, though the Makefile's `demo` target reaches for one. The
scenarios it would run are not written -- IMPLEMENTATION_PLAN.md §5 carries `demo` as pending and
the parent graph still stops at the resolution fork -- and a command that printed nothing and exited
zero would be indistinguishable from a demonstration that had run. An unrecognised argument names
the commands that do exist, which is the more useful failure until the scenarios are written.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from langgraph.graph import END, START

from lpr_cpe.config import get_settings
from lpr_cpe.graph.builder import (
    BRANCH_TARGETS,
    DECISION_AFTER,
    PENDING_STAGES,
    compile_parent_graph,
)
from lpr_cpe.graph.routing import DECISIONS


def report_topology(out: TextIO) -> None:
    """Compile the parent graph and describe it, including the exits that lead nowhere yet.

    Compiling is the check. `build_parent_graph` runs `_check_tables` before returning, so tables
    that disagree with the node registry, with `routing.DECISIONS` or with each other raise
    `GraphTopologyError` here rather than printing a plausible diagram.

    The node list is read off the compiled graph; **the branches are not**, and the difference is
    not a stylistic one. `get_graph()` renders for drawing and keeps one edge per
    `(source, target)` pair, so an answer sharing a destination with an earlier answer is dropped
    from the rendering entirely. Measured on the graph as it stands, that loses four of the
    fourteen declared answers -- `quarantine`, `manual_review`, `preventive` and
    `approve_low_confidence` -- because each shares `END` with the escalation edge, and it collapses
    D03's `associate` and `continue`, which `builder` documents as deliberately distinct. Two of the
    four are `PENDING_STAGES` exits, so a report built on the rendering would hide precisely the
    branches that most need naming. `BRANCH_TARGETS` is therefore read directly, which
    `_check_tables` has already held against `Decision.branches` in both directions.

    The pending exits are printed rather than kept behind a flag, for the reason `PENDING_STAGES`
    exists at all: an `END` reached for want of a subgraph is otherwise indistinguishable from a
    finished run, and a report that omitted them would recreate that ambiguity one level up.
    """
    app = compile_parent_graph()
    steps = [name for name in app.get_graph().nodes if name not in {START, END}]

    out.write(f"graph {app.name}\n")
    out.write(f"  nodes {len(steps)}, in specification order\n")
    for position, name in enumerate(steps, start=1):
        out.write(f"    P{position:02d} {name}\n")

    out.write(f"  decisions {len(DECISION_AFTER)} wired here, of {len(DECISIONS)} declared\n")
    for node, identifier in DECISION_AFTER.items():
        out.write(f"    {identifier} after {node} -- {DECISIONS[identifier].question}\n")
        targets = BRANCH_TARGETS[identifier]
        width = max(len(answer) for answer in targets)
        for answer, target in targets.items():
            out.write(f"      {answer:<{width}} -> {target}\n")

    out.write("  a node with no decision runs on to the next; every node also has an escalation\n")
    out.write(f"    edge to {END}, wired uniformly from the budget guard\n")

    out.write(f"  exits awaiting a stage {len(PENDING_STAGES)}\n")
    for exit_point, missing in PENDING_STAGES.items():
        out.write(f"    {exit_point}: {missing}\n")


def report_config(out: TextIO) -> None:
    """The settings this process would run under, the two safety switches first.

    No URL, DSN or secret is printed. `postgres_dsn` routinely carries a password and the report is
    the kind of thing that gets pasted into a ticket, so the checkpointer is described by the
    question anyone actually asks of it -- whether Postgres is configured at all.

    Read through `get_settings()` rather than taken as an argument, because that cache *is* the
    process's configuration; a report built from an instance handed in by its caller could disagree
    with what every adapter in the same process is reading.
    """
    settings = get_settings()

    out.write("config\n")
    out.write(f"  app_mode {settings.app_mode}\n")
    out.write(f"  allow_production_writes {settings.allow_production_writes}\n")
    out.write(f"  writes_permitted {settings.writes_permitted}\n")
    out.write(f"  environment {settings.environment}\n")
    out.write(f"  model_provider {settings.model_provider}\n")
    out.write(f"  model_name {settings.model_name}\n")
    out.write(f"  postgres_enabled {settings.postgres_enabled}\n")
    out.write(f"  timezone {settings.timezone}\n")
    windows = ", ".join(window.isoformat("minutes") for window in settings.scan_window_times)
    out.write(f"  scan_windows {windows}\n")
    out.write(f"  max_graph_steps {settings.max_graph_steps}\n")
    out.write(f"  max_diagnostic_cycles {settings.max_diagnostic_cycles}\n")


#: The reports, in the order a bare `lpr-cpe` prints them. This mapping is the only list of command
#: names: the parser's `choices` is built from its keys, so a report cannot be added without
#: becoming reachable, and a name cannot be offered that nothing implements.
REPORTS: Mapping[str, Callable[[TextIO], None]] = {
    "topology": report_topology,
    "config": report_config,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lpr-cpe",
        description=(
            "Report the compiled parent graph and the configuration this process would run "
            "under. Reads no external system and writes to none."
        ),
    )
    parser.add_argument(
        "section",
        nargs="?",
        choices=tuple(REPORTS),
        help="which report to print; both are printed when this is omitted",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point `[project.scripts]` names. Returns the process exit code.

    `argv` is a parameter rather than read from `sys.argv` inside so that the tests drive the real
    entry point instead of a helper beside it. A console script calls this with no arguments, which
    is the path a bare `lpr-cpe` takes and the one the tests exercise by passing `None`.
    """
    args = _build_parser().parse_args(argv)
    section: str | None = args.section
    for name in REPORTS if section is None else (section,):
        REPORTS[name](sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
