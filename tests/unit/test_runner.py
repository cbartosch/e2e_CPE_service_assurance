"""`lpr-cpe run`, and the one claim it is easy to make falsely: that it answered the pause.

An `interrupt()` payload is a dict, and which key it holds is the only thing telling an approval
question apart from a crew briefing. Handing the wrong answer does not raise -- the parser returns
`None`, the node records an unusable report, and the crew is asked again until a budget trips.
IMPLEMENTATION_PLAN.md §5 records a sweep that did exactly that and wrote the result up as a product
defect before finding it was the harness. So the tests below are mostly about the *dispatch*, and the
sharpest of them reads `src` rather than the runner.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pytest

from lpr_cpe.config.settings import AppMode, Settings, get_settings
from lpr_cpe.domain.enums import CaseType
from lpr_cpe.runner import (
    MAX_LAPS,
    PAUSE_SHAPES,
    Outcome,
    ProductionWritesRefusedError,
    approval,
    crew_report,
    drive,
    pause_kind,
    report_run,
    run_service,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "lpr_cpe"

#: The one fixture measured reaching `closed`. Named rather than found, so a change of outcome fails
#: here with the service in the message.
CLOSING_SERVICE = "SVC-UT-001-B-01"


# ------------------------------------------------------------------------------------------------
# The dispatch, and the gate that keeps it complete
# ------------------------------------------------------------------------------------------------


def test_every_interrupt_shape_in_src_is_one_the_runner_answers() -> None:
    """The gate. A pause shape the runner does not know is answered as an approval, in silence.

    `PAUSE_SHAPES` is hand-written -- it has to be, since the answer for each shape is a different
    payload -- so the thing that keeps it honest is reading the interrupt payload keys back out of
    `src`. A seventh gate added to a subgraph fails here rather than falling through to
    `approval()`, which is the failure that cost a sweep its conclusions.

    The pattern is the naming convention every gate here follows -- `*_request`, `*_wait`, or the
    literal `briefing` -- and it is narrow on purpose. A first version matched any dict key inside an
    `interrupt(` call and reported `event_id` and `mr_id`: keys *inside* a payload rather than the
    key that identifies it. A gate with two false positives out of eight is a gate that gets
    deleted, so the convention is what it reads, and `test_no_declared_pause_shape_has_gone_away` is
    the other half that stops the convention drifting.

    Watched red by deleting `plant_report_request` from `PAUSE_SHAPES`::

        AssertionError: these interrupt payload shapes exist in src and the runner does not answer
        them: ['plant_report_request']
    """
    shape = re.compile(r"\"(\w+_request|\w+_wait|briefing)\"\s*:")
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        found.update(shape.findall(path.read_text(encoding="utf-8")))

    assert found, "no interrupt payload keys were found in src, so this test proves nothing"
    missing = sorted(found - set(PAUSE_SHAPES))
    assert not missing, (
        "these interrupt payload shapes exist in src and the runner does not answer them: "
        f"{missing}. Each would fall through to `approval()`, which the parser rejects silently."
    )


def test_no_declared_pause_shape_has_gone_away() -> None:
    """The other direction: a shape the runner answers that nothing raises any more.

    Same rule as `builder._check_pending_stages`. A stale entry is not harmful on its own, but it is
    a claim about the graph that nothing else checks, and the `_answer` branch behind it becomes
    dead code nobody can reach.

    Watched red by adding a shape nothing raises::

        AssertionError: PAUSE_SHAPES declares shapes that no interrupt in src raises:
        ['imaginary_request']
    """
    text = "".join(p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.py")))
    stale = sorted(key for key in PAUSE_SHAPES if f'"{key}"' not in text)
    assert not stale, (
        f"PAUSE_SHAPES declares shapes that no interrupt in src raises: {stale}. Delete the entry "
        "and the `_answer` branch behind it."
    )


@pytest.mark.parametrize("shape", sorted(PAUSE_SHAPES))
def test_each_shape_is_recognised_from_its_key_alone(shape: str) -> None:
    """`pause_kind` reads the key, not the position. Payloads carry other keys too."""
    assert pause_kind({shape: {"anything": 1}, "requested_items": []}) == shape


def test_an_unrecognised_payload_is_named_rather_than_guessed() -> None:
    """`unknown` is a real answer and the run reports it.

    Returning `"approval_request"` for an unknown shape would be the guess that makes the failure
    silent; returning `"unknown"` is what lets `run_service` exit non-zero and `report_run` say so.
    """
    assert pause_kind({"something_else": {}}) == "unknown"
    assert pause_kind("a string") == "unknown"
    assert pause_kind(None) == "unknown"


def test_the_crew_report_carries_every_measurement_the_contract_demands() -> None:
    """Derived from `HandoverContract.REQUIRED_BY_TECHNOLOGY`, not written out.

    A hand-copied measurement list drifts away from the contract that checks it, and the failure is
    a crew report the handover rejects as incomplete -- which reads as a workflow defect.
    """
    from lpr_cpe.domain.field_ops import HandoverContract

    for technology in ("hfc", "pon"):
        report = crew_report({"service_ref": "S", "technology": technology, "delimiter_ref": "D"})
        assert set(report["measurements"]) == set(
            HandoverContract.REQUIRED_BY_TECHNOLOGY[technology]
        )
        assert report["delimiter_kind"] == ("tap" if technology == "hfc" else "odp")


def test_the_approval_names_a_role_rbac_accepts() -> None:
    """An approval from an unqualified role is refused, so the scripted one has to name a real one."""
    from lpr_cpe.security.rbac import _APPROVERS

    approvers = {role for roles in _APPROVERS.values() for role in roles}
    assert approvers, "no role may approve anything, which cannot be right"
    assert approval(approved=True)["decided_by_role"] in approvers, (
        "the scripted approver names a role no approval kind accepts, so every gate would refuse it"
    )
    assert approval(approved=True)["status"] == "approved"
    assert approval(approved=False)["status"] == "rejected"


# ------------------------------------------------------------------------------------------------
# The run itself
# ------------------------------------------------------------------------------------------------


async def test_one_incident_reaches_closure_through_the_command(fixtures: Any) -> None:
    """The claim the command exists to make: this workflow runs, end to end, unattended.

    `SVC-UT-001-B-01` is the one fixture measured reaching `closed`, on the remote path, and it does
    it here through the same `drive` the CLI calls rather than through a test-only harness. The
    pause tally is asserted by shape because that is the part that silently goes wrong: a run that
    answered five approvals instead of two approvals and three window waits would reach the same
    status having exercised nothing.
    """
    outcome = await drive(fixtures.services[CLOSING_SERVICE])

    assert outcome.status == "closed", f"expected closure, got {outcome.status}: {outcome.reason}"
    assert outcome.escalated is False
    assert outcome.settled is True
    assert outcome.pauses.get("unknown", 0) == 0
    assert set(outcome.pauses) == {"approval_request", "stability_window_wait"}
    assert outcome.actions > 0, "a closed incident with no action taken is not a repair"
    assert outcome.audit_events > 0


async def test_declining_every_approval_changes_the_ending(fixtures: Any) -> None:
    """`--decline` is not decoration: the refusal has to reach a different place.

    Asserted as a difference rather than as a specific status, because which refusal arm a service
    takes is the graph's business and this test's business is that the flag is wired to it.
    """
    service = fixtures.services[CLOSING_SERVICE]
    approved = await drive(service, approve=True)
    refused = await drive(service, approve=False)

    assert approved.status != refused.status, (
        "granting and refusing every approval reached the same ending, so --decline is inert"
    )
    assert approved.status == "closed"


async def test_a_predictive_filing_takes_d04s_other_arm(fixtures: Any) -> None:
    """`--predictive` reaches the preventive stage, which the proactive filing cannot.

    `route_predictive_or_active` answers `preventive` only for the two predictive case types, so
    this is the flag's whole purpose. Measured: the preventive arm needs no approval at all, which
    is why the pause tally is asserted empty rather than merely different.
    """
    service = fixtures.services["SVC-UT-001-A-03"]
    outcome = await drive(service, case_type=CaseType.PREDICTIVE_MAINTENANCE)

    assert outcome.settled is True
    assert outcome.pauses == {}, "the preventive arm holds no interrupt; it selects and stops"
    assert outcome.escalated is False
    assert outcome.nodes_entered < 12, (
        "the preventive arm is intake plus three nodes, not the graph"
    )


async def test_the_runner_refuses_to_drive_a_configuration_that_permits_writes(
    fixtures: Any,
) -> None:
    """A5 and A4, enforced at the one command that runs the workflow rather than describes it.

    Nothing real is reachable today -- `build_context` defaults to the fixture-backed simulators --
    so this guard is about the day one is. It is here rather than trusted to the adapters because
    "the adapters happen to be fakes" is a property that changes without this file being touched.

    Watched red by deleting the check::

        Failed: DID NOT RAISE <class 'lpr_cpe.runner.ProductionWritesRefusedError'>
    """
    live = Settings(app_mode=AppMode.PRODUCTION, allow_production_writes=True)
    assert live.writes_permitted, "this test needs a configuration that really would write"

    with pytest.raises(ProductionWritesRefusedError, match="will not do it"):
        await drive(fixtures.services[CLOSING_SERVICE], settings=live)


def test_the_refusal_names_the_environment_variables_and_not_the_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message has to name `LPR_APP_MODE`, because the unprefixed name is silently ignored.

    This is a usability defect that was shipped and caught by running the command: the guard was
    verified with `APP_MODE=production ALLOW_PRODUCTION_WRITES=true` and **did not fire**, because
    `Settings.model_config` sets `env_prefix="LPR_"` and `extra="ignore"`, so the unprefixed names
    are read by nothing and discarded without complaint. The run proceeded and closed an incident,
    which is exactly the reassuring wrong answer.

    Driven through `main` with the environment patched, rather than by constructing `Settings` —
    which is what the test above does, and which is why the test above could not have caught this.
    A guard tested only through its injectable parameter is a guard whose real entry point is
    untested.

    Watched red by dropping the prefix from the message::

        AssertionError: the refusal names APP_MODE, which is not the variable that sets it
    """
    from lpr_cpe.cli import main

    monkeypatch.setenv("LPR_APP_MODE", "production")
    monkeypatch.setenv("LPR_ALLOW_PRODUCTION_WRITES", "true")
    get_settings.cache_clear()

    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    assert main(["run", CLOSING_SERVICE]) == 3, "a refused run exits 3, not 0 and not 1"
    get_settings.cache_clear()

    message = err.getvalue()
    assert "LPR_APP_MODE" in message, (
        "the refusal names APP_MODE, which is not the variable that sets it"
    )
    assert "LPR_ALLOW_PRODUCTION_WRITES" in message


# ------------------------------------------------------------------------------------------------
# The command surface
# ------------------------------------------------------------------------------------------------


def test_an_unknown_service_lists_what_there_is_and_exits_two() -> None:
    """A typo should not read as a broken install, and should not read as a failed run either.

    Exit 2 rather than 1, so a caller can tell "no such service" from "the run did not settle".
    """
    out = io.StringIO()
    assert run_service("SVC-NOPE", out) == 2
    printed = out.getvalue()
    assert "no such service" in printed
    assert "SVC-UT-001-B-01" in printed, "the available services are listed, not just the count"


def test_the_report_states_the_pause_tally_and_flags_what_it_could_not_answer() -> None:
    """`report_run` is what an operator reads, and the two warnings are its whole value.

    A run that did not settle, or met a pause it could not name, is a defect in the runner or in the
    guards -- and either is invisible in a status line that says `escalated`. Both are constructed
    here rather than driven, because neither is reachable today and a warning nothing prints is a
    warning nobody will notice has broken.
    """
    stuck = Outcome(
        service_ref="SVC-X",
        status="diagnosing",
        escalated=False,
        reason="",
        pauses={"approval_request": 2, "unknown": 1},
        nodes_entered=9,
        audit_events=20,
        actions=1,
        settled=False,
    )
    out = io.StringIO()
    report_run(stuck, out)
    printed = out.getvalue()

    assert "pauses answered 3" in printed
    assert "approval_request" in printed and "unknown" in printed
    assert f"still pausing after {MAX_LAPS} resumes" in printed
    assert "matched no entry in PAUSE_SHAPES" in printed


def test_the_cli_exposes_run_without_breaking_the_bare_invocation() -> None:
    """Adding a subcommand must not turn `lpr-cpe` with no arguments into an error.

    That bare form prints both reports, is what the README documents, and is what
    `test_cli.py` drives. `run` is a subparser beside the positional rather than instead of it, and
    this is the test that keeps the two arrangements compatible.
    """
    from lpr_cpe.cli import _build_parser, _build_run_parser

    reports = _build_parser()
    assert reports.parse_args([]).section is None, "a bare invocation prints both reports"
    assert reports.parse_args(["topology"]).section == "topology"
    assert reports.parse_args(["config"]).section == "config"

    run = _build_run_parser().parse_args(["SVC-1", "--decline", "--predictive"])
    assert run.service_ref == "SVC-1"
    assert run.decline is True and run.predictive is True

    # The regression this test exists for: `run` must not be an `add_subparsers` entry on the report
    # parser, because a subparser and an optional positional compete for the first token and the
    # subparser wins. Asserted on the parser rather than on the outcome, so the failure names the
    # cause.
    assert not reports._subparsers, (
        "`run` is back on the report parser as a subparser, which makes `lpr-cpe topology` fail "
        "with `invalid choice: 'topology' (choose from run)`"
    )
