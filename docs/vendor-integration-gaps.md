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
