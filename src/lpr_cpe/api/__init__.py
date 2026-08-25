"""The HTTP surface: the thing that makes six approval gates answerable by a person.

Until this package existed the workflow could be *run* -- `lpr-cpe run` drives one incident with a
scripted operator -- and could not be *operated*. Every approval gate raises `interrupt()`, and the
only things that could resume one were a test harness and that command's own script. An incident
waiting on a supervisor had nowhere for the supervisor to answer.

The four things this package is shaped by, each of them measured rather than assumed
------------------------------------------------------------------------------------
**A paused subgraph's writes are not in the parent's state.** IMPLEMENTATION_PLAN.md §2 measures it:
with an incident parked at a nested gate, `(await app.aget_state(config)).values` reports
`dispatch_planning` and `pending_approval: None` while the child holds `awaiting_approval` and the
request. Four of the six gates are nested. So `GET /incidents/{id}/state` reads through
`graph.inspect`, never through `aget_state` directly -- the naive implementation would report an
incident as `diagnosing` while it had been sitting on someone's queue since Tuesday, which is the
most misleading answer this system could give.

**`Command(resume={})` is a silent no-op.** Also §2: LangGraph decides whether a resume value is an
interrupt-id map with `all(is_xxh3_128_hexdigest(k) for k in resume)`, and `all()` over an empty
dict is `True`. An empty mapping is therefore read as "a map that resumes nothing" -- the graph
re-pauses having run no node, writes no audit event, and the endpoint returns 200. `POST
/incidents/{id}/resume` rejects `{}` explicitly, with a 422 that says why, because nothing
downstream can tell that request from a successful one.

**This is a boundary, so redaction applies here.** `security.redaction.redact` is a boundary
obligation, and an audit timeline is exactly the payload that carries a customer's MAC, IP and name
out of the process. Every response body goes through it. The cost is that a caller cannot read a
full MAC back out of this API, which is the intended trade.

**Write endpoints are authenticated in the production profile only.** The specification asks for
that shape, and it is the honest one for a system whose adapters are all simulators: requiring a
token in simulation would be theatre, and requiring none in production would be a hole.
`security.py` owns the switch, and the failure mode it is written against is the one where the
check is *skipped*
rather than the one where it is wrong -- see the test that asserts a missing token is rejected under
production settings.

What is not here
---------------
No persistence of anything but graph state: there is no incident index, so `GET /incidents` (plural)
does not exist and neither does any listing. `POST /events` and `POST /incidents` both start a
thread and the caller is expected to hold the id. The specification's "canonical incident index" is
a store this package does not have, and inventing one behind an endpoint that could not survive a
restart would be worse than its absence. Recorded as gap API-1.
"""

from lpr_cpe.api.app import build_app, create_app

__all__ = ["build_app", "create_app"]
