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
    DELIBERATE_TERMINALS,
    PENDING_STAGES,
    SUBGRAPH_NODES,
    chain_from,
    compile_parent_graph,
)
from lpr_cpe.graph.routing import DECISIONS


def _asked(identifier: str, node: str, chain: Sequence[str]) -> str:
    """Where in the chain this question is reached from -- `after P11`, or `when D07 answers ...`.

    Only the head of a chain follows a node. The rest are reached by an answer, and saying which
    answer is the whole reason the report prints a chain as several blocks rather than as the one
    edge the graph actually holds: `_terminal_targets` has already flattened D07, D08, D09 and D11
    into six branches off P11, and flattened is exactly how the specification does *not* read them.
    """
    if identifier == chain[0]:
        return f"after {node}"
    reached_by = [
        f"{earlier} answers {answer!r}"
        for earlier in chain
        for answer, target in BRANCH_TARGETS[earlier].items()
        if target == identifier
    ]
    return "when " + " or ".join(reached_by)


def report_topology(out: TextIO) -> None:
    """Compile the parent graph and describe it, including the exits that lead nowhere yet.

    Compiling is the check. `build_parent_graph` runs `_check_tables` before returning, so tables
    that disagree with the node registry, with `routing.DECISIONS` or with each other raise
    `GraphTopologyError` here rather than printing a plausible diagram.

    The node list is read off the compiled graph; **the branches are not**, and the difference is
    not a stylistic one. `get_graph()` renders for drawing and keeps one edge per
    `(source, target)` pair -- 77 edges over 77 distinct pairs, so exactly one each -- and an answer
    that shares a destination with an earlier answer keeps the edge and loses its label. Re-measured
    on 2026-08-21, and in the unit this report prints rather than a global one, because an earlier
    revision of this paragraph mixed the two: the loss is per `(source, answer)`, so `continue` is
    drawn elsewhere in the graph and still missing where D03 asks it. This report prints **53** rows
    of source, decision and answer. Forty-six of them are offered to `add_conditional_edges` at all,
    and **nine of those are lost** -- the three `END`-bound answers, each sharing `END` with the
    uniform escalation edge; D03's `continue`, which `builder` documents as deliberately distinct
    from `associate`; and five where LangGraph omits a label equal to its target's name, which are
    D11's `self_help` and `field_planning` from each of two sources and D12's `field_planning`. The
    other **seven are never offered as edges**, and they are the group worth knowing about: each
    names another decision rather than a node, so `_cascade` flattens it away before the drawing
    sees it. Those seven are the chain links themselves -- D07 to D08 to D09 to D11 from each of two
    sources, and D19's `restored` to D20 -- so the rendering loses precisely the structure the next
    paragraph is about. `BRANCH_TARGETS` is therefore read directly, which `_check_tables` has
    already held against `Decision.branches` in both directions.

    `DECISION_AFTER` is not sufficient either, for an unrelated reason: it names the thirteen
    decisions that follow a node, and D08, D09, D11 and D20 are reached only by another decision's
    answer. `chain_from` is what takes the report from thirteen questions to seventeen -- and no
    further, which the header line below admits by printing seventeen wired against twenty-four
    declared, the other seven being wired inside subgraphs this report does not descend into. Both
    omissions have the same shape -- a structure that reads as the whole topology and is a
    projection of it -- so neither the drawing nor either table is trusted to be complete alone.

    The pending exits are printed rather than kept behind a flag, for the reason `PENDING_STAGES`
    exists at all: an `END` reached for want of a subgraph is otherwise indistinguishable from a
    finished run, and a report that omitted them would recreate that ambiguity one level up. That
    table is empty as of 2026-08-23, which is exactly when the line under it starts earning its
    place: `DELIBERATE_TERMINALS` is now the only table naming where a run may stop, and printing a
    bare `0` without it would answer the ambiguity by deleting both halves of the question.
    """
    app = compile_parent_graph()
    drawn = [name for name in app.get_graph().nodes if name not in {START, END}]
    steps = [name for name in drawn if name not in SUBGRAPH_NODES]

    out.write(f"graph {app.name}\n")
    out.write(f"  nodes {len(drawn)}\n")
    out.write(f"    steps {len(steps)}, in specification order\n")
    for position, name in enumerate(steps, start=1):
        out.write(f"      P{position:02d} {name}\n")
    # Deliberately unnumbered. A subgraph is several specification steps and owns an interrupt, so
    # `P12` would be a claim about which step it is that no table here makes.
    out.write(f"    subgraphs {len(SUBGRAPH_NODES)}, each a compiled graph reached by name\n")
    for name in drawn:
        if name in SUBGRAPH_NODES:
            out.write(f"      {name}\n")

    out.write(f"  decisions {len(BRANCH_TARGETS)} wired here, of {len(DECISIONS)} declared\n")
    for node, head in DECISION_AFTER.items():
        chain = chain_from(head)
        for identifier in chain:
            out.write(f"    {identifier} {_asked(identifier, node, chain)}")
            out.write(f" -- {DECISIONS[identifier].question}\n")
            targets = BRANCH_TARGETS[identifier]
            width = max(len(answer) for answer in targets)
            for answer, target in targets.items():
                out.write(f"      {answer:<{width}} -> {target}\n")

    out.write("  a node with no decision runs on to the next; every node also has an escalation\n")
    out.write(f"    edge to {END}, wired uniformly from the budget guard\n")

    out.write(f"  exits awaiting a stage {len(PENDING_STAGES)}\n")
    for exit_point, missing in PENDING_STAGES.items():
        out.write(f"    {exit_point}: {missing}\n")

    # Printed beside the count above rather than instead of it, and both are needed now that the
    # count is zero. "Nothing is awaiting a stage" and "here is where a run legitimately stops" are
    # different facts, and a report that showed only the first would leave a reader unable to tell
    # an empty frontier from a report that had stopped looking.
    out.write(f"  nodes that end the workflow on purpose {len(DELIBERATE_TERMINALS)}\n")
    for name in sorted(DELIBERATE_TERMINALS):
        out.write(f"    {name}\n")


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
    out.write(f"  max_resolution_cycles {settings.max_resolution_cycles}\n")


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
