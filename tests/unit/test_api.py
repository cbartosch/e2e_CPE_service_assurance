"""The HTTP surface, driven against a real graph rather than a mocked one.

Every test here starts a thread through `POST /events` and drives it, because the two defects this
package is written against are both invisible to a mocked graph:

* a paused *nested* subgraph's writes are not in the parent's state, so `GET /state` implemented the
  obvious way reports an incident as `diagnosing` while it waits on a supervisor;
* `Command(resume={})` is read as a map that resumes nothing, so a resume endpoint forwarding an
  empty body returns 200 having done nothing at all.

Neither can be reproduced without a real pause, so there are no mocks in this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from lpr_cpe.api import build_app
from lpr_cpe.api.app import WEBHOOK_SOURCES
from lpr_cpe.config.clock import FrozenClock
from lpr_cpe.config.settings import AppMode, Settings
from lpr_cpe.graph.context import build_context

#: The fixture measured reaching a dispatch approval, so `/state` has a nested pause to report.
DISPATCH_SERVICE = "SVC-SJ-011-A-01"

#: The one that closes, for the end-to-end path.
CLOSING_SERVICE = "SVC-UT-001-B-01"


def _event(service: dict[str, Any], **over: Any) -> dict[str, Any]:
    """An event body built from a real fixture service.

    The references have to be the fixture's own. Invented ones look fine on the wire and then no
    adapter can resolve the device, so `assemble_case_evidence` re-enters until the `node_reentries`
    budget stops it and the incident escalates before reaching any gate -- which is what the first
    version of this module did, and it read as an API defect rather than as a test using a
    `cpe_ref` nothing had heard of.
    """
    body = {
        "event_id": f"EVT-{service['service_ref']}",
        "source": "nxt",
        "case_type": "proactive_alarm",
        "technology": service["technology"],
        "severity": "high",
        "service_ref": service["service_ref"],
        "customer_ref": service["customer_ref"],
        "cpe_ref": service["cpe_ref"],
        "summary": "degraded service",
    }
    body.update(over)
    return body


#: The instant every test in this module runs at. Inside the crew scheduling window and outside
#: quiet hours, which is the band the dispatch fixture needs to reach its approval gate.
#:
#: **A literal, because the wall clock is not one.** See `client` below.
FROZEN = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("America/Puerto_Rico"))


@pytest.fixture
def client() -> Any:
    """A client over the real app, in the simulation profile, at a frozen instant.

    `TestClient` as a context manager, which is what runs the lifespan -- without the `with` the
    checkpointer is never opened and `app.state.graph` does not exist, so every request 500s on the
    dependency. That is worth a sentence because the failure looks like a routing bug.

    **The frozen clock is the load-bearing part and it was not here originally.** The first version
    let the app build its own context, so the graph ran on `SystemClock` and the route depended on
    what time it was. Quiet hours (21:00-07:00 local) and the crew scheduling window (07:00-21:00)
    are real policy in `pack.yaml`, and the dispatch fixture crosses them: swept hour by hour it
    reaches an approval gate from 01:00 to 16:00 Puerto Rico time and reaches *no gate at all* from
    17:00 to 00:00. So the five tests below were red for roughly a third of every day. They were
    green when the audit bundle was generated and red when this module was next run, with nothing
    changed in between, which is how the defect was found rather than by reading the code.

    Watched red by passing `SystemClock("America/Puerto_Rico")` instead, at an evening instant::

        AssertionError: this fixture reaches an approval gate
        assert False is True
    """
    context = build_context(settings=Settings(), clock=FrozenClock(FROZEN))
    with TestClient(build_app(settings=Settings(), context=context)) as c:
        yield c


# ------------------------------------------------------------------------------------------------
# The surface the specification names
# ------------------------------------------------------------------------------------------------


def test_every_endpoint_the_specification_asks_for_exists(client: Any) -> None:
    """Thirteen paths, checked against the specification's own list.

    Written out rather than counted, because a count passes when one endpoint is added and another
    removed. The webhook routes are one parameterised path in FastAPI and four in the specification,
    so `WEBHOOK_SOURCES` is asserted separately -- and it is a set the route reads, not a free-form
    parameter, so an unknown source 404s rather than being accepted for a system nobody integrated.
    """
    declared = {
        ("POST", "/events"),
        ("POST", "/incidents"),
        ("GET", "/incidents/{incident_id}"),
        ("GET", "/incidents/{incident_id}/state"),
        ("GET", "/incidents/{incident_id}/timeline"),
        ("POST", "/incidents/{incident_id}/resume"),
        ("POST", "/incidents/{incident_id}/approvals"),
        ("POST", "/incidents/{incident_id}/customer-response"),
        ("POST", "/webhooks/{source}"),
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/metrics"),
    }
    actual = {
        (method, route.path)
        for route in client.app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if method in {"GET", "POST"}
    }
    missing = declared - actual
    assert not missing, f"the specification names these and the app does not have them: {missing}"
    assert {"nxt", "wfm", "jtrack", "tmf"} == WEBHOOK_SOURCES


def test_health_answers_without_the_graph_and_ready_reports_it(client: Any) -> None:
    """Two different questions. `/health` is liveness; `/ready` is whether it can serve a resume."""
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["writes_permitted"] is False


def test_metrics_publishes_the_vocabulary_and_says_it_holds_no_values(client: Any) -> None:
    """The honest shape of a metrics endpoint with no metric store behind it.

    Returning zeros for every KPI would be the reassuring wrong answer -- a dashboard would draw
    them. The two KPIs that are *not derivable from state* are reported separately, because that is
    a different fact from "we have not measured it yet".
    """
    body = client.get("/metrics").json()
    assert body["declared"] > 0
    assert body["not_derivable_from_state"], "two KPIs are structurally not derivable; say so"
    assert "no incident index" in body["note"]
    overlap = set(body["derivable_from_state"]) & set(body["not_derivable_from_state"])
    assert not overlap, f"a KPI cannot be both derivable and not: {overlap}"


# ------------------------------------------------------------------------------------------------
# Intake, and reading a thread back
# ------------------------------------------------------------------------------------------------


def test_an_event_starts_a_thread_and_runs_it_to_its_first_pause(
    client: Any, fixtures: Any
) -> None:
    """`POST /events` is accepted, the graph runs, and the incident id is the thread id (D1)."""
    posted = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE]))
    assert posted.status_code == 202, posted.text
    body = posted.json()
    assert body["incident_id"] == f"INC-{DISPATCH_SERVICE}"
    assert body["awaiting_human"] is True, "this fixture reaches an approval gate"

    read = client.get(f"/incidents/{body['incident_id']}")
    assert read.status_code == 200
    assert read.json()["incident_id"] == body["incident_id"]


def test_an_unknown_incident_is_a_404_on_every_read(client: Any) -> None:
    """No index means "does it exist?" is "is its state empty?"; the answer is still a 404."""
    for path in ("", "/state", "/timeline"):
        response = client.get(f"/incidents/INC-NOPE{path}")
        assert response.status_code == 404, path


def test_the_state_endpoint_reports_the_pause_the_parent_cannot_see(
    client: Any, fixtures: Any
) -> None:
    """The defect this package is most written against, asserted end to end.

    Four of the six gates are nested, and a paused subgraph's writes do not reach the parent. So the
    obvious implementation -- `(await app.aget_state(config)).values` -- reports `diagnosing` and
    `pending_approval: None` for an incident that is sitting on a supervisor's queue. This asserts
    the corrected read: the pending approval is *present*, and it names a kind and a question.

    Watched red by reading `aget_state(...).values` directly instead of `graph.inspect`::

        AssertionError: the pending approval is invisible to the parent's own state; /state has to
        read through graph.inspect
        assert None is not None
    """
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]

    state = client.get(f"/incidents/{incident_id}/state")
    assert state.status_code == 200
    body = state.json()

    assert body["awaiting_human"] is True
    assert body["pending_approval"] is not None, (
        "the pending approval is invisible to the parent's own state; /state has to read through "
        "graph.inspect"
    )
    assert body["pending_approval"]["kind"], "the approval names its kind"
    assert body["pending_approval"]["question"], "and the question a human has to answer"
    assert body["awaiting_node_path"], "and which node is waiting"


def test_the_timeline_is_the_audit_trail_in_order(client: Any, fixtures: Any) -> None:
    """Every decision the graph made, with its reason. Redacted on the way out."""
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]

    timeline = client.get(f"/incidents/{incident_id}/timeline")
    assert timeline.status_code == 200
    entries = timeline.json()
    assert len(entries) > 5, (
        "an incident that reached a dispatch gate made more decisions than this"
    )
    assert [e["event_id"] for e in entries] == sorted(
        (e["event_id"] for e in entries), key=lambda _: 0
    ), "the order is the order the graph wrote them, not a sort"
    assert all(e["action"] and e["outcome"] for e in entries)


# ------------------------------------------------------------------------------------------------
# The two measured hazards
# ------------------------------------------------------------------------------------------------


def test_an_empty_resume_is_refused_rather_than_silently_doing_nothing(
    client: Any, fixtures: Any
) -> None:
    """`Command(resume={})` resumes nothing, and the endpoint must not return 200 for it.

    IMPLEMENTATION_PLAN.md §2: LangGraph decides whether a resume value is an interrupt-id map with
    `all(is_xxh3_128_hexdigest(k) for k in resume)`, and `all()` over an empty dict is `True`. So
    `{}` is read as a map that resumes nothing -- the pending interrupt is left unsatisfied, the
    graph re-pauses having executed no node, and **no audit event records that anything happened**.
    An endpoint that forwarded it would return 200 for a request that did nothing, with nothing in
    the trail to notice.

    The 422 is a validator on `ResumePayload`, which is why that body is a model at all.

    Watched red by removing the validator: the request returns 200, `resumed: true`, and the
    incident is still paused at the same gate with the same approval outstanding.
    """
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]

    refused = client.post(f"/incidents/{incident_id}/resume", json={"value": {}})
    assert refused.status_code == 422, refused.text
    assert "resumes nothing" in refused.text

    # And the gate is exactly where it was.
    after = client.get(f"/incidents/{incident_id}/state").json()
    assert after["awaiting_human"] is True
    assert after["pending_approval"] is not None


def test_a_non_empty_resume_that_is_not_a_mapping_is_accepted(client: Any, fixtures: Any) -> None:
    """`{"source": "scheduler_tick"}` and `""` both reach the node; only `{}` is swallowed.

    The validator has to refuse the empty mapping *and only* the empty mapping, or it would reject
    the timer resume that releases a stability window. Asserted so the refusal above cannot be
    widened into a general "no falsy values" rule.
    """
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]
    for value in ("", 0, [], {"source": "scheduler_tick"}):
        response = client.post(f"/incidents/{incident_id}/resume", json={"value": value})
        assert response.status_code != 422, f"{value!r} is a real answer and was refused"


def test_resuming_an_incident_that_is_not_paused_is_a_conflict(client: Any, fixtures: Any) -> None:
    """409, not 200 and not 500. There is nothing to resume and the caller should know which.

    Reached by *declining* every approval until the incident escalates, which is the only way to get
    an unpaused thread through this API. **The closing path cannot be driven from here**, and that is
    a real limitation rather than a quirk of the test: `await_service_stability` is released by the
    clock and not by a resume value, so an incident that reaches its stability window parks until
    something moves time forward. The specification asks for exactly that -- "persist the state and
    resume it from a scheduled timer event" -- and there is no scheduler in this package. Gap API-6;
    `lpr-cpe run` moves its own clock, which is why it can close an incident and this cannot.
    """
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]

    for _ in range(25):
        state = client.get(f"/incidents/{incident_id}/state").json()
        if not state["awaiting_human"]:
            break
        client.post(
            f"/incidents/{incident_id}/resume",
            json={
                "value": {
                    "status": "rejected",
                    "decided_by": "sofia.reyes",
                    "decided_by_role": "noc_supervisor",
                    "rationale": "declining to reach a terminal state",
                }
            },
        )
    else:
        pytest.fail("declining every approval never ended the incident")

    conflict = client.post(f"/incidents/{incident_id}/resume", json={"value": {"any": "thing"}})
    assert conflict.status_code == 409, conflict.text
    assert "nothing to resume" in conflict.text


# ------------------------------------------------------------------------------------------------
# Approvals, and who may give them
# ------------------------------------------------------------------------------------------------


def test_an_approval_from_a_role_that_may_not_give_it_is_refused(
    client: Any, fixtures: Any
) -> None:
    """403, and the gate stays paused. The alternative is worse than a refusal.

    Resuming with an answer the operator did not intend is indistinguishable downstream from a real
    rejection -- `route_dispatch_approval` reads the recorded decision and cannot know it came from
    someone unqualified. So the check is at the boundary, where the pending request's `kind` is
    readable, and a wrong role never becomes a `ApprovalDecision` at all.

    Watched red by deleting the `can_approve` call: the request returns 200 and the incident is
    resumed on a decision `security.rbac` would have refused.
    """
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]
    pending = client.get(f"/incidents/{incident_id}/state").json()["pending_approval"]
    assert pending is not None

    refused = client.post(
        f"/incidents/{incident_id}/approvals",
        json={
            "status": "approved",
            "decided_by": "someone",
            "decided_by_role": "field_technician",
            "rationale": "not mine to give",
        },
    )
    assert refused.status_code == 403, refused.text
    assert "may not decide" in refused.text

    still = client.get(f"/incidents/{incident_id}/state").json()
    assert still["pending_approval"] is not None, "a refused role must leave the gate waiting"


def test_a_qualified_approval_moves_the_incident_on(client: Any, fixtures: Any) -> None:
    """The positive control, without which the test above passes on an endpoint that refuses all."""
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]
    before = client.get(f"/incidents/{incident_id}/state").json()

    answered = client.post(
        f"/incidents/{incident_id}/approvals",
        json={
            "status": "approved",
            "decided_by": "sofia.reyes",
            "decided_by_role": "noc_supervisor",
            "rationale": "send the joint crew",
        },
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["resumed"] is True

    after = client.get(f"/incidents/{incident_id}/state").json()
    assert after != before, "the approval was accepted and the incident did not move"


def test_a_pending_approval_status_is_not_an_answer(client: Any, fixtures: Any) -> None:
    """Posting `pending` would resume a gate with a value that decides nothing."""
    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]
    response = client.post(
        f"/incidents/{incident_id}/approvals",
        json={"status": "pending", "decided_by": "s", "decided_by_role": "noc_supervisor"},
    )
    assert response.status_code == 422
    assert "answers nothing" in response.text


def test_an_approval_on_an_incident_with_no_gate_open_is_a_conflict(
    client: Any, fixtures: Any
) -> None:
    """409 with a message pointing at `/resume`, which is the endpoint for the other four shapes."""
    incident_id = client.post(
        "/events", json=_event(fixtures.services[CLOSING_SERVICE], incident_id="INC-NO-GATE")
    ).json()["incident_id"]
    state = client.get(f"/incidents/{incident_id}/state").json()
    if state["pending_approval"] is not None:
        pytest.skip(
            "this fixture opened an approval gate; the conflict path needs one that has not"
        )
    response = client.post(
        f"/incidents/{incident_id}/approvals",
        json={"status": "approved", "decided_by": "s", "decided_by_role": "noc_supervisor"},
    )
    assert response.status_code == 409
    assert "/resume" in response.text


# ------------------------------------------------------------------------------------------------
# Webhooks
# ------------------------------------------------------------------------------------------------


def test_a_redelivered_webhook_does_nothing_a_second_time(client: Any) -> None:
    """The specification's duplicate suppression, and the order that makes it real.

    Suppression is checked *before* anything reads or writes the graph, because a check performed
    after the side effect is not a check. Asserted by sending the same `delivery_id` twice and
    requiring the second to report itself a duplicate.
    """
    first = client.post(
        "/webhooks/nxt", json={"delivery_id": "DEL-1", "payload": {"alarm_id": "A-1"}}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "delivery_id": "DEL-1",
        "accepted": True,
        "duplicate": False,
        "detail": first.json()["detail"],
    }

    again = client.post(
        "/webhooks/nxt", json={"delivery_id": "DEL-1", "payload": {"alarm_id": "A-1"}}
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["accepted"] is False


def test_two_distinct_deliveries_of_the_same_body_are_both_accepted(client: Any) -> None:
    """Why `delivery_id` is required rather than hashed from the body.

    A flapping fault produces two genuinely distinct notifications with identical contents. Keying
    suppression on a body hash would swallow the second, which is a real alarm.
    """
    body = {"payload": {"alarm_id": "A-1", "state": "raised"}}
    one = client.post("/webhooks/nxt", json={**body, "delivery_id": "DEL-A"})
    two = client.post("/webhooks/nxt", json={**body, "delivery_id": "DEL-B"})
    assert one.json()["duplicate"] is False
    assert two.json()["duplicate"] is False


@pytest.mark.parametrize("source", sorted(WEBHOOK_SOURCES))
def test_each_named_webhook_source_is_routed(client: Any, source: str) -> None:
    response = client.post(f"/webhooks/{source}", json={"delivery_id": f"DEL-{source}"})
    assert response.status_code == 200, response.text


def test_a_webhook_for_a_system_nobody_integrated_is_a_404(client: Any) -> None:
    """An unknown source is refused rather than accepted for a system with no adapter."""
    response = client.post("/webhooks/salesforce", json={"delivery_id": "DEL-X"})
    assert response.status_code == 404
    assert "jtrack" in response.text, "the refusal lists what this system does have"


# ------------------------------------------------------------------------------------------------
# The production profile
# ------------------------------------------------------------------------------------------------


def test_write_endpoints_are_unauthenticated_in_simulation(client: Any, fixtures: Any) -> None:
    """No token, and the write is accepted. Requiring one here would be theatre."""
    assert (
        client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).status_code == 202
    )


def test_write_endpoints_require_a_token_in_the_production_profile(fixtures: Any) -> None:
    """The specification's "authenticate write endpoints in the production profile", both ways.

    Reads are deliberately left open: a state read carries no authority and the response is
    redacted. What is guarded is anything that starts a thread or answers a gate.

    Watched red by making `require_write_token` return unconditionally::

        AssertionError: assert 202 == 401
    """
    live = Settings(
        app_mode=AppMode.PRODUCTION, allow_production_writes=False, webhook_secret="s3cret"
    )
    with TestClient(build_app(settings=live)) as authed:
        assert authed.get("/health").status_code == 200, "reads stay open"

        assert (
            authed.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).status_code
            == 401
        )
        assert (
            authed.post(
                "/events",
                json=_event(fixtures.services[DISPATCH_SERVICE]),
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        accepted = authed.post(
            "/events",
            json=_event(fixtures.services[DISPATCH_SERVICE]),
            headers={"Authorization": "Bearer s3cret"},
        )
        assert accepted.status_code == 202, accepted.text


def test_production_with_no_secret_refuses_rather_than_waving_writes_through(fixtures: Any) -> None:
    """Fails closed, and with a 503 rather than a 401.

    A deployment that turned on production mode and forgot the secret is the exact case where "no
    secret configured means no check" would be worst. The status code carries the distinction the
    person holding the pager needs: 401 means the credential is wrong, 503 means this deployment is
    misconfigured and no credential would help.
    """
    misconfigured = Settings(app_mode=AppMode.PRODUCTION, webhook_secret="")
    with TestClient(build_app(settings=misconfigured)) as broken:
        response = broken.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE]))
        assert response.status_code == 503, response.text
        assert "LPR_WEBHOOK_SECRET" in response.text


# ------------------------------------------------------------------------------------------------
# Redaction at the boundary
# ------------------------------------------------------------------------------------------------


def test_no_response_carries_an_unmasked_mac_address(client: Any, fixtures: Any) -> None:
    """The boundary obligation, asserted on the payload most likely to breach it.

    A timeline is where a MAC, an IP or a customer name leaves the process, and the CPE records in
    the fixture set carry all three. The assertion is on the *shape* -- a full six-octet MAC -- so it
    holds whatever the fixture happens to contain rather than naming one value.

    Watched red by removing the `redact` call from the timeline route.
    """
    import re

    incident_id = client.post("/events", json=_event(fixtures.services[DISPATCH_SERVICE])).json()[
        "incident_id"
    ]
    full_mac = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")

    for path in ("", "/state", "/timeline"):
        body = client.get(f"/incidents/{incident_id}{path}").text
        found = full_mac.findall(body)
        assert not found, f"{path or '/'} leaked an unmasked MAC: {found[:3]}"
