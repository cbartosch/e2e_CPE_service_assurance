# Vendor integration gaps

**No vendor API documentation was supplied for any of the ten external systems** (see
`IMPLEMENTATION_PLAN.md`, assumptions A1/A2). Every adapter in `src/lpr_cpe/integrations/` is a
`Protocol` plus a fixture-backed simulator, and **every field name in every payload is ours**. None
of it is a confirmed vendor contract and none of it should be quoted at a vendor as though it were.

This file exists so that the line between "the spec told us this" and "we made this up" is written
down somewhere other than the code. Each section says what was supplied, what was invented, and the
specific question a real integration has to answer. The gap IDs are cited from the source, so a
reader who hits `-- gap PON-4` in a docstring can find out here what is actually unknown.

Two things are *not* gaps, and are named here so nobody closes them by mistake:

* **The write path.** `WriteGate` is the single owner of "may this write leave the process", every
  simulated write calls `authorize()` before it consults its idempotency ledger, and every write
  returns `ActionOutcome.SIMULATED` rather than `SUCCEEDED`. A real adapter inherits those three
  obligations. It does not inherit the fixture lookups.
* **PII minimisation at the collection boundary.** Client MAC addresses are masked inside
  `read_wifi_status` before the payload is returned, and destinations are masked inside the
  communications adapter, because these payloads become `ActionRecord`s and then audit events. Doing
  it in a later log processor would be doing it after the value had been copied.

---

## NXT — CommScope ServAssure NXT (`integrations/nxt/`)

**Supplied:** the product name, and that it is the source of alarms and RF/PNM measurements.
**Invented:** `rf_verdict`, `service_group_health`, the alarm envelope, the PNM capture envelope,
every threshold behind a verdict.

* **NXT-1** — No endpoint, protocol or auth model. Is NXT reached over REST, a Kafka topic, an SNMP
  trap receiver, or a read replica of its database? Answer changes whether `fetch_alarms(since=)` is
  a query or a subscription, and a subscription makes the `since` parameter the wrong shape.
* **NXT-2** — The alarm shape is ours. What is NXT's alarm *identity*, and does it already
  de-duplicate and correlate? If it does, the workflow's clustering is a second opinion on a
  question NXT has answered, and the two will disagree.
* **NXT-3** — `rf_verdict` is a classification we compute. Does NXT emit a health verdict of its
  own, or only raw MER/SNR/codeword counts? If only raw values, LPR owns the thresholds and they
  belong in `policies`, not in an adapter.
* **NXT-4** — `fetch_pnm_capture` deliberately returns an `object_reference` plus summary statistics,
  never a spectrum array, so that a capture never enters checkpointed graph state. How are captures
  actually retrieved, what is their retention, and can a stored reference still be resolved a week
  later when someone audits the decision?
* **NXT-5** — This adapter is read-only, which is a guess. Does NXT expose any state-changing call
  the workflow would want — alarm acknowledgement, or suppression during a maintenance window? A
  write we did not model is a write the gate never sees.

## HFC — plant records and per-tap neighbour sets (`integrations/hfc/`)

**Supplied:** the HFC topology vocabulary (CMTS/CCAP, node, amplifier chain, tap) and that
blast-radius denominators matter. **Invented:** `tap_value_db`, `return_path_state`, `housing`, and
the whole `fetch_tap_view` shape.

* **HFC-1** — No plant-records API. Which system is *authoritative* for the node/amplifier/tap chain,
  and can it be queried per service reference, or only per plant object? The latter means the
  workflow needs a service-to-plant resolution step that does not exist yet.
* **HFC-2** — `tap_value_db` and `housing` are ours. Are tap value and housing type recorded at all,
  and to what accuracy? A pedestal-versus-aerial distinction the records get wrong sends the wrong
  crew type.
* **HFC-3** — `return_path_state` per amplifier is ours. Is upstream/return path state available
  per amplifier, or only at the node?
* **HFC-4** — The `homes_passed` versus `services_in_service` distinction is load-bearing: it is the
  denominator of every common-cause fraction. Which one can plant records actually produce? If only
  homes passed, every degraded-fraction the workflow computes is diluted by vacant homes.

## PON — OLT, splitters, ODPs and optical levels (`integrations/pon/`)

**Supplied:** the PON vocabulary (OLT, PON port, primary splitter, ODP, ONT, OMCI, dying gasp) and
that drop-versus-plant faults are distinguished by the peer set. **Invented:** `optical_verdict`,
`omci_state`, `secondary_split`, the `fetch_odp_view` shape.

* **PON-1** — No OLT northbound, OMCI or fibre-records API. Which OLT vendor, and what northbound —
  NETCONF/YANG, TL1, or a vendor EMS with its own model? Optical power is reported differently by
  each and the sign convention is not universal.
* **PON-2** — `optical_verdict` is ours. Does the EMS classify a reading, or must the thresholds be
  ours? They differ by PON standard (GPON versus XGS-PON) and by feeder length, both of which the
  adapter reports precisely so a real threshold set can use them.
* **PON-3** — `omci_state` is ours. What ONT/OMCI state vocabulary does the OLT actually report, and
  does it distinguish "administratively down" from "no light"? Those are a billing question and a
  truck roll respectively.
* **PON-4** — `secondary_split` is ours. Are secondary splitters recorded in fibre records at all?
  An unrecorded secondary split makes the loss budget wrong and makes an ODP's peer set incomplete —
  which breaks the exact comparison PON diagnosis depends on.
* **PON-5** — `fetch_odp_view` assumes every ONT on an ODP can be enumerated *with its own current
  reading* in one call. If readings must be fetched per ONT, the peer comparison costs 16 calls and
  the latency budget for the diagnosis stage changes.

## CPE — ACS / TR-069 (`integrations/cpe/`)

**Supplied:** that CPE telemetry is read through an ACS, and that TR-181 `Device.WiFi.*` is the data
model. **Invented:** the exact parameter paths, the diagnostic result shapes, the action parameter
names, the staleness model.

* **CPE-1** — GenieACS-*shaped*, not confirmed GenieACS. Which ACS is deployed, and is it TR-069
  (CWMP) or USP/TR-369? The two have different session models, and TR-369 would make several of
  these reads push rather than pull.
* **CPE-2** — The TR-181 paths are written as the standard defines them. Does the deployed CPE estate
  actually expose them, at which data model version, and how much sits behind vendor extensions
  instead? A mixed estate means per-model path mapping, which no adapter here models.
* **CPE-3** — Device actions (reboot, Wi-Fi channel change, firmware update) are modelled as
  gate-authorised writes. Which does the ACS expose, and specifically **what authorises a firmware
  update**? That is a change-management decision with a fleet-sized blast radius, not an assurance
  action, and it may not belong in this workflow at all.
* **CPE-4** — Associated clients are returned with a masked MAC and a `device_class` ("smart-tv"),
  never a hostname. A real ACS returns the client-supplied hostname, which routinely contains a
  person's name. Which fields in the real payload carry PII, and is the masking list complete?
* **CPE-5** — TR-069 diagnostics are **asynchronous**: the ACS sets a state and the device informs
  on completion. This simulator returns a terminal `status: "Complete"` directly. What is the real
  callback or polling model, and what is the timeout — and does the graph need a wait state that it
  currently does not have?
* **CPE-6** — Staleness is simulated against an invented Inform interval. What is the estate's real
  Inform interval, and at what age does a reading stop being usable for a dispatch decision? The
  workflow flags `STALE` today on a number we chose.
* **CPE-7** — **What does the ACS return for a diagnostic against an offline device?** The simulator
  first raised `AdapterUnavailableError`, the class that means "the system could not be reached at
  all". That was wrong in a way worth recording, because the ACS *did* answer — it answered that the
  device is not there. Since `ADAPTER_UNAVAILABLE` is one of `DataQualityAssessment.BLOCKING_FLAGS`,
  a single offline CPE made `sufficient_for_action` false, D05 answered `gather_more` on every pass,
  and the `pon_power_affected` fixture spent its diagnostic-cycle budget and escalated with
  `fault_domain=unknown` and no options — while a dying gasp, an open utility outage and a
  power-correlation finding were already in state. It now raises a plain `AdapterError`
  (`retryable=False`), which lands as `MISSING_FIELD` and does not block; the fixture diagnoses
  `power` and offers two options. The sibling `read_status` had it right all along, returning
  `data_available: False`. **The open question is which of these the real ACS does**: a CWMP fault
  code, a session timeout, or silence. A timeout is genuinely "unreachable", and would restore the
  old behaviour for a legitimate reason.
* **CPE-8** — **The simulator now models the *effect* of an action, and the model is ours.** Until
  this was added, `apply_action` recorded an intent and changed nothing, so the verification read
  after a remote repair returned the same telemetry as before it. That made `RemoteAction.fixed_it`
  unreachable, D10's `verify` branch dead, and the specification's Scenario 2 ("HFC remote repair
  succeeds") impossible to express. A device is now recovered by an action when two things hold: the
  action is one of `_RECOVERING_ACTIONS` (those that re-establish a management session), and the
  service's telemetry profile is one where the *plant* is healthy, so the device is the only thing
  left that can be wrong. The recovered device reports `_UPTIME_AFTER_RECOVERY = 120` seconds.

  Three things here are invented and none should be quoted to anyone: **which actions actually
  recover a wedged device** (a resync is listed alongside a reboot on the assumption that
  re-provisioning clears a bad session; that may be optimistic), **how long a real device takes to
  come back and re-Inform** — 120 s is a placeholder for a delay that in reality is long enough that
  the graph may need the wait state CPE-5 already asks about — and **whether recovery is even
  observable in one read**, since a device that reboots is unreachable for a while before it is
  healthy, and this simulator steps straight from one to the other.

  What is *not* arbitrary is the discriminator, and it is the part worth keeping: a reboot recovers
  the wedged device at `SVC-UT-001-B-01` and does **not** recover `SVC-VQ-002-A-01`, whose ONT is
  dark because utility power is out. A simulator that recovered both would close every incident on
  the first reboot and never dispatch anyone; one that recovered neither is where this started.

* **CPE-9** — **The simulator has no failure mode, so a *failed* action cannot be exercised.**
  `simulate_write` has two outcomes: `ActionOutcome.SIMULATED` when the gate permits, and
  `verdict.outcome_if_refused` when it does not — which WRITE-1 records as always resolving to
  `SIMULATED` through the real gate. `apply_action` adds only an `AdapterError` for an action outside
  `SUPPORTED_ACTIONS`. Nothing anywhere returns `FAILED`, `TIMED_OUT` or `PARTIAL`, so three of the
  eight `ActionOutcome` members are unreachable from any fixture.

  The consequence is specific and it was measured, not predicted. `RemoteAction.fixed_it` is
  `outcome in _RAN and verification_passed is True`; when the outcome is always `SIMULATED`, the
  first conjunct is always true and `fixed_it` becomes an alias for `verification_passed`. Replacing
  `verified.fixed_it` with `passed` in `verify_remote_repair` left all fourteen end-to-end tests in
  `tests/unit/test_subgraph_remote_resolution.py` green. The distinction it protects is real —
  a real ACS reports a failed write whenever the CPE takes the reboot and the session times out
  before it can acknowledge, and crediting that recovery to the action would put a fix we know
  failed into `remote_fix_success_rate` — but no fixture can produce it, so it read as dead defence
  and would have been refactored away as such.

  Closed for the reason code by
  `test_a_device_that_recovered_after_a_failed_action_claims_no_fix`, which drives
  `verify_remote_repair` directly over a seeded `FAILED` action rather than through the graph. That
  is a test-only affordance standing in for a capability the adapter does not have, the same shape
  as WRITE-1's `_RefusingGate`. **Should the simulator grow an injectable failure mode?** All three
  are load-bearing downstream: every reference to `FAILED`, `PARTIAL` and `TIMED_OUT` in `src/` is a
  membership test in `ActionRecord.changed_something`, `ActionRecord.was_attempted` or
  `KPICalculator.automation_coverage_rate` — six reads, no writes. A fixture set that cannot produce
  an outcome cannot test the branches that read it.

  One of those readers disagreed with the others, which is what made this more than housekeeping.
  `was_attempted` is the single owner of "this reached the external system" and counts `TIMED_OUT`
  deliberately — we sent it and never learned the result. `automation_coverage_rate` built its own
  denominator from `{SUCCEEDED, PARTIAL, FAILED, SIMULATED}`, the same set minus `TIMED_OUT`: the
  second private copy `was_attempted`'s own docstring warns about, invisible only because
  `TIMED_OUT` is unreachable.

  **Resolved in favour of `was_attempted`**, which the KPI now calls instead of re-spelling. The
  case for excluding a timeout — that we cannot confirm it executed — does not survive the set it
  would leave behind: `FAILED` is *confirmed* not to have worked and is counted, so the denominator
  has never meant "took effect", it means "we sent it". Approval is decided before the send either
  way, so the outcome cannot change whether a human was asked. Excluding timeouts would also bias
  the rate upward, since they concentrate in the slow network-affecting actions the pack gates
  behind approval: the rows dropped are the *attended* ones, and coverage would climb the more work
  went unconfirmed — the same direction of error the denominator was narrowed to prevent.

  Two tests in `tests/unit/test_subgraph_remote_resolution.py` hold it, and they fail to different
  mutations on purpose. `test_the_coverage_denominator_is_the_set_was_attempted_owns` sweeps every
  `ActionOutcome` member and catches a re-spelled set, including a future member taught to one
  reader and not the other. The second, `test_an_action_whose_result_we_never_learned_still_counts_against_coverage`,
  pins the *direction*, because the sweep would also pass if the two were reconciled the other way
  by dropping `TIMED_OUT` from `was_attempted`. Both were mutation-checked.

## Write gate (`integrations/base.py`, `simulation/simulated_base.py`)

* **WRITE-1** — **`WriteGate` cannot currently block.** `authorize()` has exactly two returns:
  `permitted=True, simulated=False` when `settings.writes_permitted`, otherwise `permitted=False,
  simulated=True`. Neither yields `permitted=False, simulated=False`, so
  `WriteVerdict.outcome_if_refused` always resolves to `SIMULATED` and `ActionOutcome`
  `BLOCKED_BY_POLICY` is unreachable through the real gate.

  Four guards downstream are written against it and are all correct: `simulate_write` withholds the
  external reference, rewrites the detail, and keeps the idempotency key free for a retry, and
  `SimulatedCPEAdapter.apply_action` declines to mark the device recovered. Being unreachable, they
  were also untested, and the mutation check confirmed it — deleting the recovery guard changed no
  test's result. `tests/unit/test_adapters.py` now injects a `_RefusingGate` subclass to exercise
  them, which is a test-only affordance standing in for a capability the gate does not yet have.

  This is the same shape of finding as INTAKE-1 below: a branch that is wired, defensible and dead.
  The open question is where blocking belongs. The gate answers "may writes leave this process?"
  from configuration alone, which is a deployment-wide fact; "may *this* action proceed?" is
  `PolicyEngine`'s question and is answered earlier, before an `ActionRequest` is built at all —
  `ActionRequest` refuses to be constructed with `policy_outcome=BLOCKED`. It may be that
  `BLOCKED_BY_POLICY` is genuinely unreachable *by design* and the guards should stay as defence in
  depth, or that the gate should grow a policy-aware refusal. That is a decision, not an oversight,
  and it should be taken deliberately rather than discovered.

## TMF — CRM / ITSM (`integrations/tmf/`)

**Supplied:** that customer, service and SLA records are read from a CRM/ITSM, and that TM Forum is
the reference vocabulary. **Invented:** the payload bodies. The names are TMF-*flavoured*
(`serviceSpecification`, `relatedParty`) and are deliberately **not** conformant TMF621/TMF641 — a
half-correct TMF body is worse than an obviously local one, because it looks like it would validate.

* **TMF-1** — No CRM or ITSM API. Which systems, and are customer/service/SLA one system or three?
* **TMF-2** — Is there a real TMF gateway in front of them, and at which version? If yes, these
  payloads should be replaced wholesale rather than mapped, because a partial mapping is how a
  non-conformant body reaches a conformant consumer.
* **TMF-3** — SLA response/restore targets are hard-coded per product tier in the adapter. Where do
  the *contractual* targets live? They are a commercial fact with legal weight and they must not be
  a constant in an assurance simulator.
* **TMF-4** — `upsert_service_problem` assumes the workflow may create or update an ITSM problem
  record. Does a ticket already exist for the incident by the time this runs, and **who owns
  de-duplication** — the workflow or the ITSM? Both owning it produces duplicate tickets on retry.

## WFM — workforce management (`integrations/wfm/`)

**Supplied:** that crews are searched by area, type and time window, and that work orders are
created and cancelled. **Invented:** the slot shape, crew fields, cancellation semantics.

* **WFM-1** — No WFM API. Which product, and what does it expose?
* **WFM-2** — `fetch_crew_availability` returns bookable slots, intersected with the *requested*
  window rather than the raw shift. Does the real WFM return slots or shifts, and is a returned slot
  *held* for the caller or merely advertised? If it is not held, two incidents will book it.
* **WFM-3** — Crew capability is modelled as a type ("clean boots" versus "dirty boots") plus
  carried parts. How are capabilities really encoded — skills, certifications, licences? Ladder and
  confined-space work are certification-gated in ways a two-value type cannot express.
* **WFM-4** — Cancellation is modelled as always possible. Can a work order be cancelled, inside
  what window, and is there a penalty or a customer notification obligation attached?
* **WFM-5** — Booking is modelled as a single write. Does an appointment require customer
  confirmation before it is firm? If so there is a two-phase commit here that the workflow currently
  treats as one step.

## Inventory — plant records and parts (`integrations/inventory/`)

**Supplied:** that plant objects are read and corrected, that recent changes are queried, and that
parts availability affects dispatch. **Invented:** every field name, the parts namespace, the record
confidence model.

* **INV-1** — No inventory API. Which system is authoritative for plant records, and is it the same
  one the HFC and PON adapters read topology from? If not, they will disagree, and the workflow has
  no rule for which wins.
* **INV-2** — `record_confidence` is derived from audit recency, with bands we chose. Does inventory
  carry a confidence or last-verified field of its own? Deriving trust from a year is a proxy for a
  fact the system may already hold.
* **INV-3** — Parts (`PART-*`) share the plant-object namespace: read with `fetch_plant_object`,
  moved with `update_plant_object` plus `parameters["change_kind"]`. That is a consequence of the
  three-method Protocol, not a claim about reality. Are parts and plant one system or two?
* **INV-4** — Van stock is modelled as a **capability, not a count**: the fixtures say which crews
  carry a part, not how many they hold, and `van_stock_counted` is `False` to say so in-band. Is
  there a per-van stock feed? Without one, "the crew has the part" is an assumption at the moment of
  dispatch.
* **INV-5** — `ActionType` has no member for an inventory correction, and **one was deliberately not
  added**: `ActionType` is the key the policy pack matches on, so a member with no rule behind it
  makes the engine fail closed on a legitimate action. Intent therefore travels in
  `parameters["change_kind"]`. Two questions: what does a real inventory write look like, and **does
  this workflow have authority to make one at all**? If it does, `ActionType` needs a member and the
  policy pack needs a rule.

## jTrack — maintenance requests (`integrations/jtrack/`)

**Supplied:** the system name, that it is LPR's MR system of record, and that MRs carry a status
through to acceptance. **Invented:** the MR body, the mandatory handover field set, `MRStatus`
itself.

* **JTRACK-1** — No schema, no API, no state machine. `MRStatus` lives in `domain.enums` because the
  workflow reasons about it, but its nine states are ours. What are jTrack's real states, and which
  transitions is an external system permitted to drive?
* **JTRACK-2** — `REQUIRED_MR_FIELDS` (plant object, fault description, evidence references, access
  notes) is our guess at a complete handover, enforced by refusing an incomplete `create_mr`
  non-retryably. A real jTrack has its own mandatory set and it will not be this one. What is it, and
  what are the acceptance criteria a submitted MR is judged against?

## GIS — geography, weather and utility power (`integrations/gis/`)

**Supplied:** that location, weather, utility outages and travel time all bear on dispatch, and the
four Puerto Rico area archetypes. **Invented:** the travel model, the safety flag, the ferry
allowance, the forecast horizon. This adapter is **read-only by design** — nothing in this workflow
may change a map, a forecast or a utility's outage record — so it never touches the write gate.

* **GIS-1** — No GIS API. Which GIS, and can it return a service-point coordinate? Coordinates are
  rounded to five decimal places (about a metre) on purpose: enough to route a van, deliberately not
  enough to distinguish two flats on the same landing, because a doorway-precise location is a
  customer identifier under a different name.
* **GIS-2** — `travel_minutes` is a great-circle distance times a per-archetype minutes-per-km rate,
  plus a fixed overhead. **It is not a routing engine**, it knows nothing about roads, and on the day
  a bridge is out it will be confidently wrong. Which routing engine, and does it know about Puerto
  Rico road closures?
* **GIS-3** — `field_work_safe` is authored per area in the fixtures rather than derived from a wind
  or rain threshold, because "may a technician go up a ladder" is a safety rule and not an arithmetic
  one. Who owns that rule, and what are its actual thresholds?
* **GIS-4** — The Vieques/Culebra ferry is a flat 95-minute allowance, and `FERRY_WIND_LIMIT_KPH` is
  ours. A ferry is a scheduled crossing, not slower driving. Is there a timetable feed and a
  cancellation feed? On a cancelled day the answer to "can we dispatch" changes from slow to **no**,
  which is a different decision and not a longer estimate.
* **GIS-5** — Weather is one current condition per area with a 12-hour horizon; beyond it the adapter
  returns `data_available: False` rather than passing the current condition off as a forecast. Which
  weather provider, and does the utility (LUMA/PREPA) publish a machine-readable outage feed? Power
  correlation is only as good as that feed, and a manual one cannot be queried per incident.

## Communications — outbound customer contact (`integrations/communications/`)

**Supplied:** that the workflow notifies customers, offers self-help, and reads what came back.
**Invented:** the channel set, the templates, the self-help scripts, the response model.

* **COMMS-1** — No messaging platform. Which platform carries SMS, email and app push, and is it one
  or three? Delivery receipts differ per channel and the adapter currently reports one shape.
* **COMMS-2** — Templates are ours, and the design rule is that **the model may choose a template but
  never write the sentence**: customer-facing prose is a regulated artefact. What is the template
  approval workflow, who signs off, and where do approved templates live so that this adapter reads
  them rather than holding them?
* **COMMS-3** — The Spanish strings are ours and **have not been reviewed by a translator**. For a
  Puerto Rico deployment, where Spanish is the majority language, a clumsy message is worse than an
  English one. This is a real defect, not a modelling simplification.
* **COMMS-4** — Voice/IVR is deliberately excluded from `SUPPORTED_CHANNELS`. Is voice in scope? It
  is the channel most likely to be required for a vulnerable customer and the least likely to be
  satisfiable by a template.
* **COMMS-5** — `fetch_customer_responses` synthesises replies. How does a real reply reach the
  workflow — webhook, polling, an inbound queue — and **how is it bound to an incident**? A reply
  that cannot be attributed to the message that prompted it is not evidence.
* **COMMS-6** — A send reports the channel and a masked destination, never the number or address,
  because this payload becomes an audit event and a phone number in an audit log is retained as long
  as the log is. Can the platform accept a *customer reference* instead of an address, so the
  workflow never handles a contact detail at all? That would close the gap rather than mask it.

---

## Quiet hours, contact caps and vulnerable-customer protection

Not implemented in the communications adapter, on purpose. They are policy, they live in
`lpr_cpe.policies`, and they run before an `ActionRequest` reaches the adapter. An adapter that also
refused to send at 03:00 would be a second owner of a rule the policy engine owns, and the two would
disagree the first time one of them changed — most likely by the adapter quietly suppressing a
message that policy had deliberately allowed for a P1 outage.

---

## Dispatch optimisation (`dispatch/`)

Not an adapter, and listed here anyway. `dispatch/` is ours end to end — no external system is
consulted and P15 forbids a model choosing a schedule — so nothing about it is a *vendor* unknown.
But the gap IDs are cited from source the same way the adapter ones are, and a reader who hits
`-- gap DISPATCH-1` in `optimizer.py` needs somewhere to land. The distinction worth keeping is that
these are things we **have not built**, not things we **do not know**.

* **DISPATCH-1** — **There is no CP-SAT implementation.** `DispatchOptimizer` is a `Protocol` and
  `select_optimizer(prefer_solver=...)` is a factory, but both settings return
  `GreedyDispatchOptimizer` today and `test_the_solver_seam_returns_greedy_either_way` asserts
  exactly that, so the seam cannot quietly look implemented. The greedy pass places one requirement
  at a time and never revisits an earlier placement, which means it cannot trade a cheap first
  assignment for a cheaper total — the classic case being two crews and two island jobs, where
  taking the nearer job first strands the second behind its own ferry crossing. **What batch size
  does a real dispatch run at?** At a dozen jobs the optimality gap is not worth an `ortools`
  dependency; at two hundred it is the whole problem.
* **DISPATCH-2** — The pack's `dispatch.archetype_speed_kph`, `archetype_access_overhead_minutes`
  and `archetype_ferry_minutes` are **ours**, and they are the *fallback* used when the GIS adapter
  is unavailable or a coordinate is missing (the routed path is GIS-2). Two travel models exist
  deliberately, are held to the same three-term shape, and
  `test_regression_pack_and_gis_fixture_price_the_same_geography` fails if either is edited alone.
  `TravelEstimate.basis` records which one answered. **Is a fallback estimate ever acceptable in
  production, or should an unroutable job be a refusal?** A schedule costed on straight lines and one
  costed on a road network are indistinguishable in a plan, and only one is a reason to promise a
  customer an arrival time.
* **DISPATCH-3** — All twelve constraints are enforced as **hard**. Two can be relaxed by policy —
  `respect_appointment_windows` and `require_crew_type_match` — and the other ten cannot be relaxed
  at all. The specification names the twelve; it does not say which a real dispatcher overrides at
  16:00 on a Friday. **Which are genuinely inviolable?** An aerial wind limit and an appointment
  window are the same kind of object in this code, and they should not be: one is a safety rule and
  the other is a promise, and only one of them is a manager's call.
* **DISPATCH-4** — Skills, equipment and parts are free-text strings compared by set membership,
  inheriting WFM-3. No certification expiry, no skill hierarchy (a crew qualified for fusion
  splicing is not automatically credited with mechanical splicing), and no notion of a part being
  reserved for another job. **How does the WFM really encode capability, and does it expose expiry?**
  A lapsed confined-space certificate reads as a held skill here.

---

## Resolution options (`decision_services/resolution.py`)

Also not an adapter, and listed for the same reason as `dispatch/`: these are things we **have not
built**, not things we **do not know**. P11 says a candidate must carry ten attributes and that the
node must produce candidates in eight categories. `ResolutionOption` carries six of the ten outright
and the `_CATALOGUE` covers five of the eight. What follows is the remainder, named so that a reader
comparing the spec against the code can tell a deliberate omission from an oversight.

Two things here are **not** gaps. `risk` and `required_approval` are copied onto every option from
the policy pack's `ActionRule` at plan time — they were the two spec attributes the pack already
knew and `plan_resolution` was discarding. And the plan is not the authoriser: `PolicyEngine`
re-reads the pack at execution time, and the copies exist for display and for the audit record.

* **RESOLUTION-1** — **Every success probability is an estimate.** Nothing in this repository has
  observed a reboot fixing 45% of CPE faults. They are ordering weights and their absolute values
  should not be quoted to anyone. **What is the real per-action, per-domain success rate?** A
  deployment with outcome history should replace the whole column; until then the *ordering* is the
  only part of the number that is load-bearing.
* **RESOLUTION-2** — Four of P11's ten required attributes have no field: **cost class**, **required
  skill and parts**, **rollback plan** and **reason codes**. `reversible` is a bool where the spec
  asks for a plan, and "required evidence" is only loosely `prerequisites`, a free-text tuple.
  Skills and parts would inherit WFM-3's vocabulary problem, and a per-option `ReasonCode` has no
  honest member today — see the note in `graph/nodes/diagnosis.py`, where P11 wanted one for "the
  catalogue has nothing for this domain" and none of the 45 members distinguishes that from "the
  pack disallowed everything". **Which of these does an operator actually act on?** Adding four
  fields nobody reads is worse than naming them here.
* **RESOLUTION-3** — **`rank_key` has no cost term, and it shows.** It weighs success against
  *customer* disruption, so the 240 minutes of a technician's day and the 6 minutes of a reboot are
  priced the same. Measured consequence: for `cpe`, `create_work_order` ranks first at 0.630 against
  `cpe_reboot`'s 0.383; for `inside_home_wiring`, a truck outranks self-help 0.595 to 0.255. This is
  cosmetic today and only because the routers filter `untried()` by *kind* rather than taking the
  top rank — D09 asks for the first *remote* option and gets `cpe_reboot`. But the plan an operator
  reads recommends a truck first. This is the same missing quantity as RESOLUTION-2's cost class,
  which is why fudging the success numbers to force the intended order would be the wrong repair:
  it would falsify an estimate to compensate for an absent term.
* **RESOLUTION-4** — **Monitoring is not offered as a P11 candidate.** The spec lists it among the
  eight, but the action that would express it, `create_pm_case`, is assigned by the spec to D04
  (predictive risk with no current impact) and D24 (chronic pattern). Offering it here as well would
  give preventive-maintenance cases two owners. **Is "watch it and do nothing yet" a real option for
  an incident that already has customer impact?** If it is, it needs its own action type, because
  reusing the D04 one conflates a preventive case with a deferred repair.
* **RESOLUTION-5** — **`is_remote_option` is structural, so paperwork can pass for a remote repair.**
  It is defined as "no truck and no customer", which is true of raising a maintenance request and of
  notifying a customer. This was observed rather than reasoned: for `tap_or_odp` — the one plant
  domain D08 deliberately does not divert, because its remedy is a joint dispatch — D08 returned
  `continue` and D09 returned `remote` for a plan whose only option was `raise_mr`, handing a
  day-long plant request to the remote-repair stage. Closed for MRs by flagging them `truck_roll`
  (the flag describes the work the option causes, as `minutes=1440` already did) and by giving the
  tap the Clean Boots half D08 assumes exists; the tap now routes D09 → `self_help_check` → D11 →
  `field_planning` with both halves in hand. **Still open:** `notify_customer` under `power` remains
  structurally "remote" and is only hidden by D08 diverting `power`. That half is now demonstrable
  rather than argued: since the CPE-7 fix let `pon_power_affected` reach P11 at all,
  that fixture produces `fault_domain=power` with options `[notify_customer, raise_mr]`, of which
  `is_remote_option` accepts `notify_customer`. Only D08 answering `plant_path` for `power` keeps it
  away from D09. **Should D09 ask a positive question** — does this option repair the device or its
  configuration — rather than inferring a repair from the absence of a truck?

  The open half is now at least *guarded*, which it was not before. `execute_remote_repair` hands
  the selected option straight to `ctx.adapters.cpe.apply_action`, and that raises `AdapterError` on
  anything outside `SUPPORTED_ACTIONS` — measured, by driving `raise_mr` into the subgraph. So the
  safety of every non-CPE option `is_remote_option` admits rests entirely on D08 diverting its
  domain first, an agreement between `boundaries.crew_for`, `resolution._CATALOGUE` and the CPE
  adapter that none of the three mentions.
  `test_only_cpe_executable_actions_can_reach_the_remote_branch` now walks every `FaultDomain`,
  skips the ones D08 diverts, and fails if any survivor offers a remote option the adapter cannot
  perform. Mutation-checked: adding a `raise_mr` row under `cpe`
  turns it red naming `cpe/raise_mr`. That does not answer the design question above — it makes the
  wrong answer fail in CI rather than in an incident.
* **RESOLUTION-6** — **A back-office domain's remote repairs are never attempted.** The mirror image
  of RESOLUTION-5, found while writing its guard. `provisioning` offers exactly two options,
  `reprovision` and `profile_change`; both are in the CPE adapter's `SUPPORTED_ACTIONS`, both are
  structurally remote (no truck, no customer), and the console could execute either. D08 diverts the
  domain anyway, because `provisioning` is in `BACK_OFFICE_DOMAINS`, so neither is ever offered to
  D09 and the remote branch never sees them.

  D08 is not obviously wrong — its docstring says back-office domains "are equally not a truck roll,
  and D08's remedy list names them explicitly", which follows the spec. But the two questions have
  been collapsed. "Back office" says *who owns the fix*; the remote branch asks *can this be fixed
  from a console*. For provisioning the honest answers are "the provisioning team" and "yes", and
  D08 currently lets the first veto the second. **Should a back-office domain with an executable
  remote repair try it before being routed to the plant path?** If yes, D08's condition needs
  splitting; if no, the reason is that a reprovision needs an owner's authority rather than a
  technical capability, and that belongs in the policy pack as an approval demand rather than in a
  routing table as an omission.

## Event validation (`graph/nodes/intake.py`, D01)

**Supplied:** that D01 asks whether the event is valid and actionable, quarantines it if not, and
records a rejection reason and a data-quality metric. **Invented:** every condition that makes an
event invalid.

* **INTAKE-1** — **D01's quarantine branch is unreachable from a well-formed start.** The router
  itself is right, and is tested directly against hand-built states: it quarantines when there are
  no events, and when the assessment carries one of the three
  `DataQualityAssessment.BLOCKING_FLAGS`. Neither arm can fire in the assembled graph. P02 is the
  only node that writes `data_quality` before D01, and the two flags it raises — `MISSING_FIELD` for
  an unknown technology, and `CLOCK_SKEW` — are deliberately non-blocking; the blocking three
  (`ADAPTER_UNAVAILABLE`, `CONFLICTING_SOURCES`, `INCONSISTENT_TOPOLOGY`) are raised by adapters in
  P03 and later, which run *after* D01. `make_initial_state` always supplies an event, so the
  empty-events arm cannot fire either. The branch is wired, tested in isolation, and dead in
  practice — which is why P02 emits the data-quality metric on the continuing path too, and why the
  spec's "generate a data-quality metric" bullet is satisfied only in the sense that the metric
  exists. **What does an invalid event actually look like?** That is the vendor question underneath:
  with no real NXT alarm schema there is nothing to validate an event *against*, so P02 checks the
  two things a canonical model can check unaided. A real integration should say which of a malformed
  payload, an unknown customer reference, a decommissioned service or a replayed vendor id is a
  quarantine and which is a warning. Wherever that line falls, the check belongs in P02, because D01
  is the last point before an incident exists.

---

## Guided self-help (`graph/subgraphs/self_help.py`, P13–P15, D11/D12)

**Supplied:** that the workflow can offer a customer a self-help step, wait for them, and verify
whether it worked. **Invented:** the script catalogue, the response window, and everything about the
customer as a party the workflow can address.

* **SELFHELP-1** — **A completed self-help step has no physical effect anywhere in the simulation, so
  `resolved` is unreachable end to end.** The CPE simulator recovers a device for the actions *it*
  applies (`SUPPORTED_ACTIONS`); a customer moving their router across the room is not one of them.
  `verify_self_help` therefore reads the same `online: True` before and after, `reachability_verdict`
  returns `None` — "cannot be told from here" — and the outcome is `not_resolved` however the
  customer answers. This is faithful rather than broken: reachability genuinely is the only symptom a
  TR-069 read exposes, and a Wi-Fi coverage complaint is precisely the fault it cannot see. The real
  question is **what evidence closes a self-help session**. Interface counters, a fresh speed test, a
  Wi-Fi RSSI read from the gateway's station table, or the customer's own word after a cooling-off
  period? Until that is answered the branch can prove a customer complied and cannot prove it helped.
  The `resolved` arm is exercised at node level with supplied readings, and the end-to-end tests
  assert `not_resolved`, so the gap is visible in the suite rather than hidden by a mock.
* **SELFHELP-2** — **The response window is ours.** `SelfHelpSession.response_deadline` comes from the
  communications adapter, and the policy pack has no self-help timeout at all — no
  `customer_contact.self_help_response_window`, nothing per-channel, nothing per-severity. An SMS at
  09:00 and an email at 23:00 plainly do not deserve the same window, and a P1 outage should not wait
  as long as a coverage complaint. How long does the business actually give a customer before it
  treats silence as a refusal and spends a truck?
* **SELFHELP-3** — **The customer has no representation in state.** `IncidentState` carries a
  `customer_ref` and nothing else about them: no contact address, no channel preference, no language,
  no accessibility or support needs. Two consequences are visible in the fixtures. The masked
  destination on every send is `None`, because `_masked_destination` correctly reports "the caller did
  not supply one" rather than inventing a number. And the adapter falls back to `'es'` for language,
  which is a reasonable default for Puerto Rico and is still a *default* standing in for a fact
  nobody recorded. Per COMMS-6 the right fix is not a phone-number field: the platform should resolve
  a customer reference to a destination itself, so the workflow never holds a contact detail. But
  language and support needs are decisions, not contact details, and they need an owner.
* **SELFHELP-4** — **`customer_communications` had no writer.** The state contract declared it and no
  node in `src` appended to it, while `KPICalculator.customer_contacts_per_incident` counts exactly
  that list — and never returns `None`. An incident that had just texted its customer therefore
  reported *zero contacts*, confidently, which is worse than reporting nothing. Now written by
  `send_self_help_instructions`, keyed on `action_id` so `append_unique` collapses a replay. Recorded
  here because the underlying question is a vendor one: **every outbound contact must land in this
  list, including the ones the comms subgraph will send and any the platform originates itself.** A
  contact cap enforced against a list only one node writes is not a cap.
