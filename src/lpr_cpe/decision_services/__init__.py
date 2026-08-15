"""The deterministic decision layer: everything the graph decides without asking a model.

Each module here answers one question with arithmetic and the policy pack, and returns a value the
graph nodes place into state. Nothing in this package calls a model, reads a clock, opens a socket
or writes a record -- the times, the payloads and the policy objects all arrive as arguments. That
is what makes these functions testable without fixtures and reproducible across a resumed run: a
checkpoint restored an hour later re-derives the same numbers from the same state, which it could
not do if any of them had been read from `datetime.now()` at the moment of the first call.

The division of labour with the two neighbouring packages is the part worth stating, because all
three could plausibly own the same decision and only one of them does:

* `detectors` measures. It turns payloads into `AnomalyFinding`s and scores them.
* **This package weighs.** It turns findings, topology and policy into the assessments a decision
  rests on -- how far the fault reaches, what the contract demands, which explanations survive, what
  could be done about it, and whether a fix held.
* `policies.engine` permits. It is the only place an action is allowed or refused, and none of the
  functions here decide that even where they hold the policy object that would let them.

`blast_radius` is the clearest case: it sizes the reach of a fault and the reach of an action, and
it deliberately does not compare either against `network_action_threshold`. That comparison is a
permission, `policies.engine` owns it, and a second copy here would be a second place an action
could be approved.

Two facts are owned outside this package and imported rather than recomputed, both because a second
implementation would be discovered by a customer rather than by a test:

* **The Wi-Fi health score and band** belong to `detectors.cpe_wifi.wifi_health_verdict`.
  `forecast` converts the score to the 0-100 scale `PredictionResult` declares and reads the band's
  severity from `SEVERITY_BY_BAND`, which the detector also uses. There is no `wifi.py` or
  `scoring.py` in this package and there must not be one: the score decides what a customer is told
  about their Wi-Fi and whether an engineer is sent, and two implementations of it would disagree in
  front of someone who had been told both answers about the same week.
* **The weighting of findings across fault domains** belongs to `detectors.localisation`.
  `rca.build_hypotheses` imports `domain_weights` rather than recomputing `score * confidence`,
  because `RCAResult`'s validator refuses a result whose primary hypothesis sits outside the stated
  fault domain -- so a second formula here would not disagree quietly, it would raise part-way
  through a live incident.

Import from the modules directly. This package deliberately re-exports nothing: the names below are
of a size where a flat namespace stops saying where a decision was made, and `sla.resolve_sla` next
to `impact.assess_impact` in one import line is how two of these come to look interchangeable.
"""

from __future__ import annotations

__all__: list[str] = []
