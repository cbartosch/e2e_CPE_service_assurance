# Role

Act as a principal software architect and senior Python engineer. Build a production-oriented reference implementation of an **LPR CPE predictive service assurance workflow** using **LangGraph as the durable workflow controller** and LangChain integrations where model assistance is appropriate.

Work directly in the current repository. Do not stop after producing a design or implementation plan. Inspect the repository, document assumptions, build the application, run it, test it, and leave the repository in a demonstrable state.

Use clear telecommunications and field-operations language. Avoid unnecessary “agentic AI” terminology in code, APIs, documentation, and diagrams. Prefer names such as:

- Workflow Controller
- Correlation Service
- Diagnostic Assistant
- Test Planner
- Resolution Selector
- Dispatch Optimizer
- Clean Boots Field Assistant
- Handover Validator
- MR Coordinator
- Restoration Validator
- Closure Controller

# Glossary

Definitions below cover terms used in a workflow-specific or otherwise ambiguous way. Standard telecom-systems acronyms not listed here (CMTS, OLT, DOCSIS, and similar) can be treated as generally understood.

- **LPR** — abbreviation used throughout for the operating company this workflow is built for. The context (Puerto Rico dispatch archetypes, an HFC/PON technology mix, CommScope NXT) is consistent with Liberty Puerto Rico, but confirm against repository or organizational documentation rather than assuming.
- **CPE** — Customer Premises Equipment: the modem, gateway, ONT, or related device installed at the customer's home or business.
- **ACS** — Auto Configuration Server, the TR-069 management endpoint that CPE connect to and that OSS/BSS systems query for device status and configuration.
- **TR-069 / CWMP** — the CPE WAN Management Protocol connecting an ACS to managed CPE.
- **TR-181** — the associated Broadband Forum data model (for example `Device.WiFi.Radio`, `Device.WiFi.SSID`, `Device.WiFi.AccessPoint`) used to represent CPE parameters, including the Wi-Fi KPIs used in the predictive scan reference under Detector and prediction services.
- **NBI** — Northbound Interface, the REST API an ACS exposes for OSS/BSS systems to read device data. GenieACS is a real open-source ACS with this shape and is the basis for the synthetic simulator in the supplied predictive-scan reference; that does not confirm it as LPR's actual ACS vendor.
- **HFC** — Hybrid Fiber-Coaxial, the DOCSIS cable access technology.
- **PON** — Passive Optical Network, the fiber access technology.
- **Tap** — the HFC distribution point used in this workflow as the Clean Boots/Dirty Boots responsibility delimiter.
- **ODP** — Optical Distribution Point, the PON equivalent of the HFC tap delimiter.
- **Clean Boots** — the field-technician role working the customer side of the responsibility boundary: CPE, Wi-Fi, premise wiring, and the drop, up to the tap or ODP.
- **Dirty Boots** — the field or plant role working upstream of the tap or ODP: OSP, plant, and NOC-coordinated repair.
- **OSP** — Outside Plant, the shared cable/fiber infrastructure between the network and the customer's tap or ODP.
- **NOC** — Network Operations Center.
- **NXT** — CommScope ServAssure NXT, the HFC/PON network, CPE health, alarm, and performance assurance system referenced throughout for evidence.
- **jTrack** — the LPR system of record for MR records covering upstream plant work. Treat it as an internal system whose API is not yet documented in the repository; do not assume a vendor or platform behind it.
- **MR** — the repair-ticket record type tracked in jTrack for Dirty Boots/plant work. Do not assume a specific expansion of the acronym (for example, "Maintenance Request") unless the repository confirms one.
- **WFM** — Workforce Management, the scheduling and dispatch system integration.
- **PNM** — Proactive Network Maintenance, the HFC RF diagnostic data used for degradation detection.
- **MDU** — Multi-Dwelling Unit, relevant to building-access constraints during dispatch.
- **Blast radius** — the number of customers or services potentially affected by a fault, or by an action taken to resolve it.
- **Delimiter** — the configurable point (tap for HFC, ODP for PON) that separates Clean Boots and Dirty Boots responsibility.
- **TMFxxx references** (TMF621, TMF642, TMF653, TMF656, TMF697, TMF724) — TM Forum Open API reference numbers, used here for conceptual field-mapping only, not as a requirement to implement the literal TM Forum APIs. These six numbers have been checked against TM Forum's current published API catalog and correctly identify Trouble Ticket, Alarm Management, Service Test Management, Service Problem Management, Work Order Management, and Incident Management respectively.

# Mission

Implement an end-to-end workflow covering:

1. Predictive degradation detection.
2. Customer-reported incidents.
3. Network and CPE alarms.
4. Event validation, deduplication, and common-cause correlation.
5. Service-impact assessment and prioritization.
6. Incident creation or association with an existing incident.
7. Evidence collection and root-cause analysis.
8. Remote repair.
9. Guided customer self-help.
10. Optimized field-service dispatch.
11. Clean Boots diagnosis and repair.
12. HFC tap or PON ODP responsibility determination.
13. Clean Boots-to-Dirty Boots handover.
14. MR creation and tracking in jTrack.
15. Dirty Boots, plant, OSP, or NOC repair.
16. Reverse handover when plant repair does not fully restore the customer service.
17. Post-repair service validation.
18. Coordinated closure of alarms, tickets, incidents, work orders, and MRs.
19. KPI capture and closed-loop improvement.

The application must support remote repair or self-help followed by a truck roll, multiple truck rolls, repeated MRs, failed field actions, and return to root-cause analysis without creating an unrelated replacement incident.

# Sources of truth

Use the supplied LPR workflow and architecture materials as the operational source of truth. If no such supplied materials exist in the repository at the time of implementation, treat this document as the sole source of truth, record that assumption explicitly in `IMPLEMENTATION_PLAN.md`, and proceed rather than stalling on their absence.

Do not invent proprietary CommScope NXT or jTrack API paths, message formats, authentication methods, or vendor-specific fields that are not present in the repository.

Where real integration specifications are unavailable:

- Define a clear adapter interface.
- Implement an in-memory or fixture-backed simulator.
- Provide example canonical payloads.
- Mark the vendor-specific mapping as an integration-discovery item.
- Do not make up endpoints that look real.

Treat **jTrack as the LPR MR system**. Do not assign a vendor to jTrack unless the repository contains verified documentation.

# Non-negotiable operating rules

Implement these rules as executable policy, not merely documentation.

## One case and one clock

- Use one incident identifier and one LangGraph thread for the life of the fault.
- The SLA clock begins once and does not reset at remote repair, customer self-help, Clean Boots dispatch, Dirty Boots handover, MR creation, or repeat dispatch.
- All work orders, MRs, tests, actions, approvals, and evidence must remain linked to the original incident.
- Create a new incident only when evidence establishes a separate event or root cause.

## Evidence before action

Every decision must identify:

- The evidence used.
- Evidence timestamps.
- Evidence freshness.
- Missing evidence.
- Confidence.
- Ruled-out causes.
- The policy or rule authorizing the next action.

No field dispatch or MR creation is allowed from a customer symptom alone.

## Deterministic models remain deterministic

Do not ask a language model to calculate:

- Degradation forecasts.
- Remaining useful life.
- Alarm thresholds.
- Common-cause clusters.
- Fault probabilities.
- Dispatch schedules.
- Travel-time optimization.
- Parts allocation.
- SLA priority.
- Policy authorization.
- Final restoration status.

Implement these as rules, statistical services, classifiers, optimization services, or adapter interfaces.

A language model may assist with:

- Summarizing evidence.
- Converting unstructured technician notes into a validated structure.
- Suggesting additional diagnostic tests.
- Producing a structured root-cause hypothesis for review.
- Drafting customer communications.
- Drafting an MR evidence narrative.
- Summarizing the completed case.

A model-drafted root-cause hypothesis (P10) is a candidate narrative only. The ranked confidence values in `RCAResult`, and any resulting fault-probability, priority, or policy decision, must come from the deterministic RCA and policy services — never solely from the model's stated confidence.

All model outputs must use validated structured schemas and must pass deterministic policy checks before affecting the workflow.

## Typed production actions only

- No node may send free-text instructions directly to a production system.
- Every external operation must use a typed tool or adapter method.
- Every write operation must include an idempotency key.
- High-risk tools must require a valid approval reference.
- Production writes must be disabled by default.
- The default runtime mode must be `simulation`.
- A `dry_run` option must show intended actions without executing them.

## Proof before closure

Do not close an incident because:

- A command completed successfully.
- A technician completed a work order.
- An MR was marked completed.
- An alarm temporarily disappeared.
- A customer completed self-help instructions.

Closure requires a configurable restoration-validation policy using:

- NXT or equivalent post-fix telemetry.
- Service tests.
- A stability observation window.
- Absence of the original anomaly.
- Reconciliation of linked records.
- Customer confirmation when telemetry cannot establish user experience.

## Re-diagnose before repeating work

After an unsuccessful remote action, self-help action, Clean Boots visit, Dirty Boots repair, work order, or MR:

- Record the attempt and outcome.
- Increment the appropriate attempt counter.
- Add all new evidence.
- Return to root-cause analysis.
- Re-evaluate the fault domain and responsibility boundary.
- Do not blindly repeat the same action.
- Require an explicit reason before issuing another work order or MR.

# LPR operating model

## Technology domains

Support:

- `HFC`
- `PON`

## Responsibility boundary

Use a configurable operational boundary with these LPR defaults:

- **HFC delimiter:** tap.
- **PON delimiter:** ODP.

Clean Boots normally works from the customer environment toward the delimiter, including the applicable CPE, power, Wi-Fi, premise wiring, and customer-side service path.

Dirty Boots, plant, OSP, or NOC normally works upstream of the accepted tap or ODP handover.

The boundary must remain configurable because actual organizational responsibilities may vary.

## Field crew types

Support:

- `clean`
- `dirty`
- `joint`

Use joint dispatch when evidence shows that both customer-domain and plant-domain work are likely and one coordinated visit has a lower expected cost or repeat-visit risk than two sequential visits.

## Puerto Rico dispatch context

Represent these operating archetypes as dispatch context, not as hard-coded administrative regions:

- Metro / MDU.
- Coastal City / Suburb.
- Central Mountain / Rural.
- Remote / Island.

Use archetype context for:

- Travel-time assumptions.
- MDU or building access.
- Road and terrain constraints.
- Ferry or remote-access constraints.
- Parts positioning.
- Skill availability.
- Same-day feasibility.
- SLA risk.

# TM Forum-aligned record model

Create internal canonical contracts that can map to:

- TMF642 Alarm Management.
- TMF621 Trouble Ticket.
- TMF724 Incident Management.
- TMF656 Service Problem Management when common-cause or service-level problem management is needed.
- TMF653 Service Test Management.
- A generic Work Order Management adapter, with an optional TMF697 mapping where appropriate for the selected implementation baseline.

Do not make TM Forum objects the internal workflow engine. LangGraph controls the process, while authoritative operational records remain in their systems of record.

Use this relationship:

```text
Alarm or customer report
        ↓
One operational incident
        ↓
Optional shared service problem
        ↓
Tests, resolution attempts, work orders and MRs
        ↓
Validated restoration
        ↓
Coordinated closure of linked records
```

# Required end-to-end workflow

Implement the following stages, process steps, decisions, branches, and loops.

## Stage 1 — Detect, validate, and correlate

### P01 — Receive signal

Accept any of:

- Predictive degradation event, including the scheduled CPE/Wi-Fi scans described under Predictive CPE/Wi-Fi scan reference.
- NXT alarm or trend event.
- HFC or PON network alarm.
- Customer call, app, chat, or care ticket.
- Technician observation.
- Existing incident, work-order, or MR update.

### P02 — Normalize event

Convert source data into a canonical `AssuranceEvent`.

Record:

- Source system.
- Source event ID.
- Event timestamp.
- Receipt timestamp.
- Technology.
- Customer, service, and CPE references when present.
- Resource references.
- Measurements.
- Data-quality score.
- Source lineage.

### D01 — Is the event valid and actionable?

If no:

- Quarantine it.
- Record the rejection reason.
- Generate a data-quality metric.
- Do not create an incident.

If yes, continue.

### P03 — Resolve identity and topology

Resolve:

```text
Customer
→ Product
→ Service
→ CPE
→ Premise/drop
→ HFC tap or PON ODP
→ HFC node/CMTS or PON OLT/port/splitter
→ upstream plant
```

### D02 — Is identity and topology sufficiently resolved?

If no:

- Request missing inventory or topology data.
- Use a bounded enrichment retry.
- Route to manual data-quality review if still unresolved.

If yes, continue.

### P04 — Deduplicate and correlate

Compare the event with:

- Existing alarms.
- Existing incidents.
- Existing customer tickets.
- Existing outages.
- Existing work orders.
- Existing jTrack MRs.
- Planned maintenance.
- Recent configuration changes.
- Commercial-power events.
- Weather events.
- Neighboring CPE or shared-resource symptoms.

### D03 — Is this planned work, a known outage, or part of an existing common cause?

If yes:

- Associate the event with the existing parent incident or service problem.
- Do not create a duplicate CPE incident.
- Create or update a customer-facing ticket only when communication or SLA handling is required.
- Continue to impact assessment for the affected customer.

If no, continue as a new incident candidate.

### P05 — Assess impact and priority

Calculate:

- Number of affected customers.
- Services affected.
- Customer priority or SLA.
- Safety or emergency conditions.
- Predicted degradation window.
- Blast radius.
- Business impact.
- Likelihood of imminent service failure.

### D04 — Is this predictive risk only or an active service incident?

For predictive risk without current service impact:

- Create or update a preventive-maintenance case.
- Select remote prevention, planned Clean Boots work, planned Dirty Boots work, or monitoring.
- Keep it linked to any later service incident.

For active service impact:

- Continue to incident creation or association.

### P06 — Create or attach to one incident

Create or update the canonical incident.

Use:

```text
thread_id = incident_id
```

The identifier must be stable, UUID-compatible, and safe for the configured persistence implementation.

## Stage 2 — Build evidence and diagnose

### P07 — Assemble the case evidence

Collect available:

- NXT alarm history and health trends.
- HFC RF and PNM evidence.
- PON optical and registration evidence.
- CPE and Wi-Fi data.
- DHCP, DNS, AAA, provisioning, latency, loss, jitter, and throughput evidence.
- Inventory and topology.
- Recent network and CPE changes.
- Customer contact history.
- Prior incidents and repair outcomes.
- Work-order and MR history.
- Technician measurements and photos.
- Weather, power, and GIS context.

### D05 — Is the evidence complete and fresh enough for the next decision?

If no:

- Identify missing items.
- Build a read-only test plan.
- Request current snapshots.
- Reject stale results outside configurable age limits.
- Once missing snapshots are available, return to P07 to reassemble evidence.
- Route to manual review when required evidence cannot be obtained.

If yes, continue.

### P08 — Create diagnostic test plan

Select the minimum safe set of tests needed to distinguish among likely causes.

Tests must be read-only unless a separate repair action is approved.

### P09 — Execute read-only tests

Store:

- Test request.
- Test result.
- Timestamp.
- Source.
- Units.
- Quality.
- Pass/fail.
- Related evidence.
- Any failure to execute.

### P10 — Determine likely root cause and fault domain

Classify the current fault domain as one of:

- `cpe`
- `wifi_or_home_network`
- `premise_wiring`
- `drop`
- `hfc_tap`
- `pon_odp`
- `shared_access_network`
- `plant`
- `provisioning`
- `service_platform`
- `commercial_power`
- `unknown`

Produce a structured `RCAResult` containing:

- Ranked hypotheses.
- Confidence by hypothesis.
- Evidence supporting each hypothesis.
- Ruled-out hypotheses.
- Recommended next tests.
- Suspected delimiter.
- Whether the cause appears common or individual.
- Whether customer access is required.

### D06 — Is root-cause confidence sufficient for the proposed action?

Thresholds must be configurable by action risk.

If no:

- Pause for L2 or SME review using a LangGraph human-approval interruption.
- Resume the same incident thread with the reviewer’s structured response.

If yes, continue.

### P11 — Generate resolution options

Produce structured candidates for:

- Monitoring.
- Remote repair.
- Guided self-help.
- Clean Boots dispatch.
- Dirty Boots or plant action.
- Joint dispatch.
- NOC action.
- Escalation.

Each candidate must contain:

- Expected success probability.
- Risk.
- Cost class.
- Customer effort.
- Required evidence.
- Required approval.
- Expected restoration time.
- Required skill and parts.
- Rollback plan.
- Reason codes.

The final selection must be made by deterministic policy and scoring services.

## Stage 3 — Select and execute the resolution

### D07 — Is there a safety, security, or high-blast-radius condition?

If yes:

- Block automatic execution.
- Require human approval or escalation.
- Record the reason.

If no, continue to D08.

### D08 — Is this a shared network, provisioning, or plant issue?

If yes:

- Route to NOC, provisioning, plant, Dirty Boots, or MR handling.
- Do not send an unnecessary customer-premises truck roll.
- If this leads directly to MR creation without a prior Clean Boots visit, use the NOC/plant evidence path in P20; the same policy-driven MR-approval requirement described in P19 still applies, using NOC/plant evidence in place of a handover contract.

If no, evaluate remote repair.

### D09 — Is an allowlisted remote repair eligible?

Eligibility must consider:

- Root-cause confidence.
- Action risk.
- CPE state.
- Customer impact.
- Blast radius.
- Prior failed attempts.
- Firmware or configuration compatibility.
- Rollback availability.
- Policy authorization.

If yes, continue to P12. If no, continue to D11.

### P12 — Execute remote repair

Examples may include:

- Reboot.
- Reauthentication.
- Reprovisioning.
- Approved configuration correction.
- Profile resynchronization.
- Approved firmware action.
- Wi-Fi or mesh correction.
- Rollback.

All operations must be typed, idempotent, audited, and policy checked.

### D10 — Did remote repair produce stable restoration?

If yes, route to verification.

If no:

- Record the failed attempt.
- Increment `remote_attempt_count`.
- Return to evidence assembly and root-cause analysis (P07 and P10).

### D11 — Is guided customer self-help suitable?

Consider:

- Customer language and support preference.
- Technical complexity.
- Safety.
- Customer presence.
- Likelihood of success.
- Whether telemetry can validate completion.
- Prior unsuccessful instructions.

If yes, continue to P13. If no, continue to P14.

### P13 — Execute guided self-help

Generate bilingual-ready, stepwise instructions.

Capture:

- Which steps were delivered.
- Which steps were completed.
- Customer responses.
- Resulting telemetry.
- Any point of failure.

### D12 — Did self-help produce stable restoration?

If yes, route to verification.

If no:

- Record the failed attempt.
- Increment `self_help_attempt_count`.
- Return to diagnosis (P10) or proceed to field planning (P14) according to policy.

### P14 — Build field-service requirement

Determine:

- Crew type.
- Skills.
- Certification.
- Tools.
- Parts bill of materials.
- Van-stock requirements.
- Customer access.
- MDU or building access.
- Safety conditions.
- Geography.
- SLA.
- Expected on-site tests.
- Expected proof of repair.

### D13 — Which dispatch type is required: Clean Boots, Dirty Boots, or joint?

By the time the workflow reaches this decision, remote repair and self-help have been ruled out or exhausted, so field dispatch is assumed necessary — this step selects the crew type rather than whether to dispatch at all.

Assign `clean`, `dirty`, or `joint` using the fault domain and responsibility boundary, then continue to P15.

### P15 — Optimize the field schedule

Use a deterministic scheduling service or solver.

Do not use a language model to choose the schedule.

The optimizer must consider:

- Skill match.
- Crew type.
- Parts availability.
- Van stock.
- Customer access window.
- Building access.
- Travel time.
- Puerto Rico operating archetype.
- Ferry or remote-access constraints.
- Safety.
- Working hours.
- SLA deadline.
- Emergency priority.
- Existing route.
- Joint-dispatch benefit.
- Expected repeat-visit risk.
- Expected first-visit success.

Return a structured plan with constraint explanations.

### D14 — Are all dispatch constraints satisfied?

If no:

- Identify the blocking constraint.
- Search alternatives.
- Queue for dispatcher action.
- Do not commit an infeasible slot.

If yes, continue.

### D15 — Is dispatch approval required?

Use policy configuration.

By default, require human approval before committing a field slot and reserving parts.

Use a LangGraph interruption with a JSON-serializable approval payload. If yes, pause on the interruption and resume into P16 once approved. If no, continue directly to P16.

### P16 — Commit field action

After approval:

- Reserve parts.
- Create or update the linked work order.
- Assign crew.
- Confirm access.
- Notify the customer.
- Send the diagnostic and evidence package to the field application.

## Stage 4 — Clean Boots execution and handover

### P17 — Clean Boots diagnosis and repair

Provide the technician with:

- Original symptom.
- Current incident state.
- NXT evidence.
- Suspected fault domain.
- Tests already performed.
- Ruled-out causes.
- Required tests.
- Parts and tools.
- Tap or ODP topology.
- Prior work orders and MRs.
- Expected success criteria.

Capture structured field evidence:

- Arrival and departure.
- Access result.
- Measurements.
- Units.
- Test points.
- Photos.
- CPE or components changed.
- Parts used.
- Actions taken.
- Last known clean point.
- First known failed point.
- Technician disposition.
- Customer confirmation when obtained.

### D16 — Was the issue resolved within the Clean Boots service domain?

If yes, route to restoration validation.

If no, continue to delimiter determination.

### D17 — Is evidence sufficient to place the fault beyond the HFC tap or PON ODP boundary?

If no:

- Do not create an incomplete MR.
- Request missing tests.
- Return to Clean Boots work or root-cause analysis.
- Escalate if the boundary cannot be established.

If yes, create the handover contract.

### P18 — Build the handover contract

The contract must contain at least:

1. The unchanged incident ID.
2. The unchanged SLA clock.
3. Technology: HFC or PON.
4. Exact tap or ODP identifier.
5. Address and GIS reference.
6. Customer, product, service, and CPE identifiers.
7. Node/CMTS or OLT/port/splitter context.
8. Reclassified fault domain.
9. Fault-domain confidence.
10. Evidence references.
11. Ruled-out causes.
12. NXT pre-fix snapshot and trends.
13. Remote and self-help actions already attempted.
14. Clean Boots measurements and test points.
15. Last clean and first failed point.
16. Photos and attachments.
17. Parts used.
18. Required Dirty Boots skill and equipment.
19. Customer-access requirement.
20. Priority and SLA.
21. Existing outage and MR deduplication result.
22. Prior related incidents, work orders, and MRs.
23. Repeat-visit count.
24. Recommended plant action.

### D18 — Is the handover complete and non-duplicative?

Validate:

- Required evidence.
- Evidence freshness.
- Exact delimiter.
- Topology consistency.
- Existing outage.
- Existing incident.
- Existing MR.
- Correct receiving owner.
- SLA and priority.
- Required skill and equipment.

If validation fails:

- Reject the handover with structured reason codes.
- Return to diagnosis or Clean Boots evidence collection.

If validation passes, continue to P19.

### P19 — Request handover approval

By default, require a dispatcher or supervisor to approve the change in responsibility domain.

The approval payload must show:

- Incident.
- Current domain.
- Proposed domain.
- Confidence.
- Missing evidence, if any.
- Existing MR result.
- Crew and equipment requirement.
- SLA impact.

### P20 — Create or update jTrack MR

After approval:

- Search for an existing related MR.
- Update the existing MR when appropriate.
- Otherwise create one MR using an idempotency key.
- Link it to the original incident and, when one exists, the Clean Boots work order.
- Attach the handover evidence, or the NOC/plant evidence package when the case reached this step directly from D08 without a Clean Boots visit — in that case the Clean-Boots-specific `HandoverContract` fields (technician measurements, last-clean/first-failed point, parts used) do not apply and may be omitted.
- Record MR acceptance status and ownership.
- Keep the incident active.

### P21 — Dirty Boots, plant, OSP, or NOC execution

Capture:

- Acceptance.
- Assignment.
- Dispatch.
- Measurements.
- Repair actions.
- Components changed.
- Photos.
- Resolution code.
- Completion time.
- Post-repair evidence.

### D19 — Did the Dirty Boots or plant action restore the affected network domain?

If no:

- Record the failed action.
- Increment `plant_attempt_count` or `mr_attempt_count`.
- Return to cross-domain root-cause analysis (P10), incorporating the failed-action evidence.
- Do not automatically duplicate the MR.

If yes, continue.

### D20 — Is customer service still degraded after plant restoration?

If yes:

- Perform a reverse handover to Clean Boots, returning to P17 (Clean Boots diagnosis and repair).
- Reuse the `HandoverContract` mechanism from P18, populated in the Dirty-Boots-to-Clean-Boots direction with the plant findings in place of the Clean Boots fields.
- Preserve the incident and SLA clock.
- Create or update a linked Clean Boots work order.
- Do not create a new incident.

If no, route to verification.

## Stage 5 — Verify, reconcile, close, and learn

### P22 — Run post-fix validation

Use:

- NXT post-fix snapshot.
- HFC or PON health metrics.
- CPE reachability.
- Service tests.
- Wi-Fi or home-network validation when relevant.
- Original anomaly comparison.
- Customer confirmation when required.

### D21 — Is the service stable for the required observation window?

If no:

- Continue observation when evidence is improving but incomplete.
- Roll back an adverse remote action where applicable.
- Return to diagnosis (P10) when degradation remains.
- Do not close.

If yes, continue to P23.

### P23 — Confirm customer outcome

Require customer confirmation only where telemetry and service tests cannot establish the actual customer experience.

Support English and Spanish message templates.

### D22 — Is the incident resolved?

If no, return to root-cause analysis (P10) with all new evidence.

If yes, continue to reconciliation.

### P24 — Reconcile linked systems

Reconcile:

- NXT alarm state.
- TMF642-aligned alarm.
- TMF621-aligned customer ticket.
- TMF724-aligned incident.
- TMF656-aligned service problem when present.
- TMF653-aligned tests.
- Clean Boots work orders.
- Dirty Boots work orders.
- jTrack MR.
- Customer communications.
- Parts usage.

### D23 — Are all linked records consistent?

If no:

- Hold closure.
- Create reconciliation tasks.
- Record which system is inconsistent.
- Retry with limits.
- Escalate unresolved reconciliation failures.

If yes, continue to P25.

### P25 — Close linked records

Close in a controlled sequence only after validated restoration.

Record:

- Root cause.
- Fault domain.
- Delimiter.
- Successful action.
- Failed actions.
- Number of remote attempts.
- Number of self-help attempts.
- Number of truck rolls.
- Number of MRs.
- Restoration time.
- Customer impact.
- Closure evidence.

### P26 — Update KPIs and learning data

Generate structured outcome labels for:

- Detector accuracy.
- Root-cause accuracy.
- Fault-domain accuracy.
- Remote-action success.
- Self-help success.
- Correct dispatch.
- First-time field resolution.
- No-fault-found.
- Avoidable dispatch.
- Correct delimiter handover.
- MR acceptance.
- Repeat work order.
- Repeat MR.
- Premature closure.
- Reopen.
- Chronic fault.

### D24 — Is this a chronic or repeating pattern?

If yes:

- Create or update a service problem or preventive-maintenance action.
- Link the relevant customers, resources, incidents, and repairs.
- Do not hide chronic problems by treating every recurrence as isolated.

If no, the incident lifecycle is complete; take no additional chronic-pattern action.

# Required LangGraph design

Use a parent `StateGraph` for the full incident lifecycle.

Use subgraphs for:

- Predictive-maintenance handling.
- Remote resolution.
- Guided self-help.
- Field planning and dispatch.
- Clean Boots execution.
- MR and Dirty Boots handling.
- Restoration validation.
- Reconciliation and closure.

Use conditional transitions for operational decisions. Do not use an unconstrained conversational agent to decide graph routing.

Use LangGraph interruptions for:

- Low-confidence RCA review.
- High-risk remote action approval.
- Dispatch approval.
- Clean-to-Dirty Boots handover approval.
- Network-wide or high-blast-radius action approval.
- Exceptional closure approval.

Use persistent checkpointing.

Provide:

- In-memory persistence for local tests.
- PostgreSQL-backed persistence for the production profile.
- A stable `thread_id` equal to the incident ID.
- Restart and resume tests.
- State inspection endpoints.
- A bounded loop and escalation strategy.

Do not use long `sleep` calls for field work, customer waits, MR waits, or stability windows. Persist the state and resume it from:

- A webhook.
- A scheduled timer event.
- A customer response.
- A work-order update.
- An MR update.
- An approval response.

# State contract

Use a `TypedDict` or equivalent lightweight schema for top-level graph state, with validated Pydantic models for nested operational objects.

Include at least:

```text
incident_id
thread_id
correlation_id
source
case_type
status
technology
customer_ref
product_ref
service_ref
cpe_ref
topology
sla
events
evidence
data_quality
anomaly_findings
prediction
impact
test_plan
test_results
rca
fault_domain
delimiter
resolution_options
selected_action
action_history
remote_attempt_count
self_help_attempt_count
field_visit_count
mr_attempt_count
plant_attempt_count
work_orders
mr_records
crew_type
dispatch_plan
handover_contract
approvals
validation
linked_records
customer_communications
audit_events
errors
retries
metrics_timestamps
created_at
updated_at
```

Use append-only reducers for:

- Evidence.
- Test results.
- Action history.
- Work orders.
- MR records.
- Approvals.
- Audit events.
- Errors.

Do not store large images or raw telemetry directly in graph state. Store secure object references and metadata.

# Required domain models

Create validated models for at least:

- `AssuranceEvent`
- `CPERecord`
- `TopologyContext`
- `SLAContext`
- `DataQualityAssessment`
- `EvidenceItem`
- `AnomalyFinding`
- `PredictionResult`
- `ImpactAssessment`
- `PreventiveMaintenanceCase`
- `ServiceProblemRecord`
- `TestPlan`
- `TestRequest`
- `TestResult`
- `RCAHypothesis`
- `RCAResult`
- `ResolutionOption`
- `ResolutionPlan`
- `PolicyDecision`
- `ApprovalRequest`
- `ApprovalDecision`
- `RemoteAction`
- `SelfHelpSession`
- `DispatchRequirement`
- `DispatchPlan`
- `WorkOrder`
- `FieldFinding`
- `HandoverContract`
- `MRRequest`
- `MRRecord`
- `ValidationResult`
- `ReconciliationResult`
- `ClosureRecord`
- `KPIEvent`

Use enums or literals for controlled statuses and reason codes.

# Detector and prediction services

Create a common interface such as:

```python
class Detector(Protocol):
    async def detect(self, context: DetectionContext) -> DetectorResult:
        ...
```

Implement fixture-backed or simple baseline versions of:

- HFC RF and PNM degradation detector.
- PON optical degradation detector.
- CPE and Wi-Fi anomaly detector — see Predictive CPE/Wi-Fi scan reference below for a concrete pipeline shape and trigger cadence.
- Service-platform anomaly detector.
- Common-cause cluster detector.
- Recent-change detector.
- Power and weather correlation detector.
- Fault-domain classifier.
- Tap/ODP delimiter localizer.
- No-fault-found risk scorer.
- Repeat-visit risk scorer.
- Handover-quality validator.
- Post-fix stability detector.

Each detector result must include:

- Detector name.
- Version.
- Observation time.
- Score.
- Confidence.
- Severity.
- Affected objects.
- Evidence references.
- Explanation fields.
- Recommended tests.
- Data-quality warnings.

Do not place a language model inside these baseline detectors.

## Predictive CPE/Wi-Fi scan reference

A reference implementation for the CPE and Wi-Fi anomaly detector was supplied as a working n8n workflow (ACS-simulator → KPI extraction → feature vector → ML inference → LLM narrative → PDF report). Treat its pipeline shape as inspiration for this detector's internal implementation. Where it conflicts with this document's non-negotiable rules — noted explicitly below — follow this document, not the reference.

### Trigger cadence (authoritative)

This overrides the schedule implied by the reference implementation's own demo configuration, which uses a single arbitrary daily cron hour for testing purposes only:

- Run a post-install baseline scan once per CPE, a configurable interval after the install work order closes, to catch cabling, configuration, or signal problems before they surface as a customer complaint.
- Run a recurring scan twice daily thereafter, by default at 07:00 and 21:00 America/Puerto_Rico time (AST, UTC-4, no DST observed), configurable by policy.
- Trigger both from an external scheduler (cron, a managed scheduler service, or equivalent) that emits a detection request per device or per target population. Do not implement the cadence as a `sleep` inside a LangGraph thread — use the scheduled-timer-event resumption mechanism already required under Required LangGraph design.
- The scheduled scan run is a batch detector job, not itself an incident thread. A clean result is recorded for KPI and audit purposes only. Only a result crossing the configured anomaly threshold produces an `AssuranceEvent` that enters P01.

### Reference pipeline shape

1. **Target selection** — resolve the population of CPE due for a scan this run (all active devices, or those due by cadence or segmentation).
2. **CPE read** — pull TR-181 `Device.WiFi.*` data (radios, SSIDs, access points, associated devices, and stats) through the CPE management adapter's read-status operation. Use a fixture-backed simulator behind that adapter until a real ACS integration is verified, per Sources of truth — the reference implementation's synthetic GenieACS-shaped responses are a reasonable starting shape for that simulator, not a confirmed production integration.
3. **KPI extraction** — flatten the raw tree into a compact per-device summary: inform recency, per-radio band/channel/width/utilization/noise/error-rate, per-access-point client counts and a signal-strength summary (average, worst, and best RSSI; throughput), plus explicit data-availability and data-quality-notes fields so missing or stale data stays visible rather than silently defaulting. Mask client MAC addresses, and any other client-identifying value, at this boundary — before the payload leaves the CPE adapter or reaches any model call. This is the PII-minimization control from Security and privacy applied at the point of collection.
4. **Feature vector** — reduce the KPI summary to the small numeric feature set a scoring service needs (for example: inform age, client count, worst RSSI, maximum utilization, maximum error rate). Keep this step deterministic and independently testable, separate from both the raw extraction and the scoring call.
5. **Anomaly scoring** — call the deterministic CPE and Wi-Fi anomaly detector with the feature vector. Return the standard `DetectorResult` shape (score, confidence, severity, evidence references, explanation fields).
6. **Deterministic banding and verdict** — classify RSSI, utilization, and error-rate into severity bands, and derive the pass/warn/fail-equivalent verdict and a numeric health score from the detector's output using rules or a classifier. **Adapt the reference implementation here**: it lets the language model derive the verdict and score itself from raw thresholds described in its own prompt. That is a fault-severity determination and belongs in the deterministic layer per Deterministic models remain deterministic — feed the language model the already-computed bands and verdict, and restrict its role to narrative output.
7. **Narrative generation** — a language model turns the deterministic verdict, score, and evidence into a structured, human-readable assessment: summary, key findings, issues (severity, category, evidence, suggested fix), and recommendations (priority, action, rationale). Validate against a strict schema and apply an automatic re-ask/repair step on validation failure before accepting the output — both are good patterns from the supplied reference.
8. **Reporting** — aggregate per-device assessments into a scan-run report for technicians or account teams (for example, a PDF). This is a customer/technician-communication use of the model, not a control-plane action, and does not by itself authorize any repair.

### Example structured narrative-output schema

The reference implementation's output schema is a reasonable starting contract:

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "device_id": { "type": "string" },
    "wifi_health_score": { "type": "number", "minimum": 0, "maximum": 100 },
    "verdict": { "type": "string", "enum": ["PASS", "WARN", "FAIL"] },
    "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
    "summary": { "type": "string" },
    "key_findings": { "type": "array", "items": { "type": "string" } },
    "issues": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "severity": { "type": "string", "enum": ["info", "minor", "major", "critical"] },
          "category": { "type": "string" },
          "evidence": { "type": "string" },
          "suggested_fix": { "type": "string" }
        },
        "required": ["severity", "category", "evidence", "suggested_fix"]
      }
    },
    "recommendations": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "priority": { "type": "string", "enum": ["low", "medium", "high"] },
          "action": { "type": "string" },
          "rationale": { "type": "string" }
        },
        "required": ["priority", "action", "rationale"]
      }
    }
  },
  "required": ["device_id", "wifi_health_score", "verdict", "confidence", "summary", "key_findings", "issues", "recommendations"]
}
```

Two implementation options satisfy the deterministic-verdict rule while still reusing this schema: (a) strip `verdict` and `wifi_health_score` from the model's own output schema and merge them in afterward from step 6's deterministic result, mirroring how the reference implementation's own finalize step already merges telemetry, detector output, and narrative into one row; or (b) keep the fields in the model's schema but treat them as required pass-through values, and add a deterministic policy check that rejects the narrative if they don't match step 6's output. Prefer (a) — it removes the possibility of drift rather than only detecting it after the fact.

# External systems and typed adapters

Create explicit protocols and simulation adapters for:

## CommScope ServAssure NXT

Read operations:

- Alarm snapshot.
- CPE health snapshot.
- Historical trends.
- Affected-subscriber cluster.
- HFC or PON performance evidence.
- Post-fix stability.

Write operations, when supported by verified integration specifications:

- Acknowledge alarm.
- Clear alarm.

Do not invent NXT endpoints.

## HFC and PON assurance systems

- CMTS, CCAP, node, DOCSIS, and PNM data.
- OLT, ONT, ONU, OMCI, optical, port, splitter, and feeder data.

## CPE management

- Read status, including TR-181 Wi-Fi KPIs for the predictive scan described under Predictive CPE/Wi-Fi scan reference.
- Run service tests.
- Reboot.
- Reprovision.
- Reauthenticate.
- Apply approved configuration.
- Roll back.
- Apply approved firmware action.

Use TR-369, USP, TR-069, ACS, or other technology only behind an adapter.

## CRM, ITSM, and TM Forum records

- Find or create customer ticket.
- Find or create incident.
- Link alarm, ticket, incident, problem, work order, MR, service, resource, and customer.
- Update status.
- Add evidence.
- Resolve and close.

## Workforce management

- Search appointment slots.
- Search qualified crews.
- Retrieve travel estimates.
- Create or update work order.
- Assign crew.
- Cancel or reschedule.
- Receive technician updates.

## Inventory

- Check parts.
- Check van stock.
- Reserve parts.
- Release reservation.
- Record consumption.

## jTrack MR

- Search existing MR.
- Create MR.
- Attach evidence.
- Accept or reject.
- Assign owner.
- Update status.
- Add plant measurements.
- Add resolution code.
- Close MR.

Do not invent jTrack endpoints.

## GIS, weather, and power

- Resolve location.
- Determine archetype.
- Estimate access constraints.
- Retrieve weather or power context.

## Customer communication

- Send notification.
- Send guided self-help.
- Request confirmation.
- Record response.
- Support English and Spanish templates.

Every write method must require:

- Incident ID.
- Idempotency key.
- Actor or service identity.
- Reason code.
- Approval reference when policy requires one.
- Correlation ID.

# Dispatch optimizer

Create a deterministic `DispatchOptimizer` interface.

A baseline implementation may use a constraint solver such as OR-Tools, but keep the optimizer replaceable.

The objective should balance:

- SLA breach risk.
- Travel time.
- Customer impact.
- First-time-fix probability.
- Repeat-visit risk.
- Parts availability.
- Joint-dispatch benefit.
- Overtime or premium cost.
- Remote-region access.
- Customer appointment preference.

Hard constraints must include:

- Required skill.
- Crew type.
- Required equipment.
- Parts or van stock.
- Working hours.
- Customer access.
- Building access.
- Safety.
- Geography.
- Ferry or remote-access window.
- Technician capacity.
- Work-order dependency.

Return both the selected plan and a machine-readable explanation of satisfied and binding constraints.

# Policy and approval controls

Create a configuration-driven policy engine.

Store policies in YAML or equivalent version-controlled configuration.

Policies must cover:

- Evidence minimums.
- Evidence freshness.
- RCA confidence thresholds.
- Remote-action allowlists.
- Action-risk classes.
- Blast-radius limits.
- Approval requirements.
- Maximum remote attempts.
- Maximum self-help attempts.
- Maximum repeated work orders.
- Maximum repeated MRs.
- Stability-window length.
- Closure requirements.
- Reconciliation retries.
- Escalation rules.

Return one of:

- `allowed`
- `requires_approval`
- `blocked`

Every policy decision must include reason codes and the policy version.

Fail closed when policy evaluation is unavailable.

# Idempotency and resilience

Design every side-effecting node for replay.

Required controls:

- Idempotency keys derived from incident, action type, and attempt.
- Upsert or read-before-write behavior.
- Transactional outbox for external writes.
- Retry with exponential backoff and jitter.
- Timeouts.
- Circuit breakers.
- Dead-letter handling.
- Duplicate webhook suppression.
- Optimistic locking or equivalent concurrency control.
- Event ordering safeguards.
- Stale-event detection.
- Maximum graph-loop protection.
- Manual recovery procedure.

Separate approval interruptions from non-idempotent writes. Perform external writes only after approval has resumed.

# Security and privacy

Implement:

- Role-based access control.
- Tool allowlists.
- Environment-based secrets.
- No credentials in source control.
- PII minimization.
- Redaction before model calls and traces.
- Audit logging.
- Encryption-ready storage interfaces.
- Model input and output size limits.
- Prompt-injection protection for retrieved knowledge.
- No execution of instructions found inside technician notes or knowledge documents.
- Human approval for destructive, network-wide, or high-blast-radius actions.

# API

Provide a FastAPI service with at least:

```text
POST /events
POST /incidents
GET  /incidents/{incident_id}
GET  /incidents/{incident_id}/state
GET  /incidents/{incident_id}/timeline
POST /incidents/{incident_id}/resume
POST /incidents/{incident_id}/approvals
POST /incidents/{incident_id}/customer-response
POST /webhooks/nxt
POST /webhooks/wfm
POST /webhooks/jtrack
POST /webhooks/tmf
GET  /health
GET  /ready
GET  /metrics
```

Requirements:

- Validate all payloads.
- Return correlation IDs.
- Authenticate write endpoints in the production profile.
- Make webhook processing idempotent.
- Provide generated OpenAPI documentation.
- Include simulation examples.

# Persistence

Use PostgreSQL for the production profile.

Persist:

- LangGraph checkpoints.
- Canonical incident index.
- External-record links.
- Idempotency records.
- Audit events.
- Outbox events.
- Approval history.
- KPI timestamps.

Use an in-memory profile for unit tests.

Provide database migrations.

# Model integration

Use a provider abstraction.

Provide:

- An Anthropic-compatible implementation.
- A deterministic fake model for tests.
- Environment-based model configuration.
- Timeouts and retry limits.
- Token and cost metadata.
- Structured Pydantic outputs.

Do not require a model API key to run unit or integration tests.

Model-assisted functions should be isolated and replaceable.

# Observability

Implement structured logs and OpenTelemetry-compatible tracing.

Provide optional LangSmith tracing controlled by environment variables.

Include these trace attributes:

- Incident ID.
- Correlation ID.
- Technology.
- Source.
- Puerto Rico archetype.
- Current workflow stage.
- Current node.
- Fault domain.
- Selected lane.
- Attempt counts.
- Approval state.
- Work-order IDs.
- MR IDs.
- Policy version.
- Detector versions.
- Model version.
- Outcome.

Never send unredacted customer PII to tracing systems.

# KPI instrumentation

Capture timestamps and reason codes needed to calculate:

- Mean time to detect.
- Mean time to incident creation.
- Mean time to diagnose.
- Remote restoration time.
- Field restoration time.
- End-to-end restoration time.
- Remote resolution rate.
- Self-help resolution rate.
- Truck-roll rate.
- Truck roll after remote repair.
- Truck roll after self-help.
- First-time field resolution.
- Strict no-fault-found rate.
- Operationally avoidable dispatch rate.
- Repeat truck-roll rate.
- One-, two-, three-, and four-plus-visit distribution.
- Average dispatches per incident.
- Correct fault-domain rate.
- Correct tap/ODP handover rate.
- MR acceptance rate.
- MR rejection reason.
- Repeat MR rate.
- Premature closure.
- Seven-day reopen.
- Thirty-day recurrence.
- Detection precision and recall where labels exist.
- RCA top-one and top-three accuracy.
- Customer-notification latency.

Do not hard-code KPI values as outcomes. Calculate them from event timestamps and case history.

# Required diagrams and documentation

Create editable Mermaid source for:

1. System architecture.
2. Complete process flow.
3. LangGraph parent graph and subgraphs.
4. Clean Boots-to-Dirty Boots sequence.
5. Reverse handover sequence.
6. External-system integration map.
7. Incident-state lifecycle.
8. Approval and policy flow.
9. Data model.
10. Deployment topology.

Also create:

- `docs/decision-table.md`
- `docs/state-contract.md`
- `docs/integration-contracts.md`
- `docs/policy-controls.md`
- `docs/security-and-privacy.md`
- `docs/operations-runbook.md`
- `docs/kpi-definition.md`
- `docs/vendor-integration-gaps.md`
- `docs/architecture-decisions/`

The diagrams must show every major decision branch and return loop.

# Repository structure

Use a structure similar to:

```text
src/lpr_cpe/
  api/
  config/
  domain/
  graph/
    builder.py
    state.py
    routing.py
    nodes/
    subgraphs/
  detectors/
  decision_services/
  dispatch/
  policies/
  integrations/
    nxt/
    hfc/
    pon/
    cpe/
    tmf/
    wfm/
    inventory/
    jtrack/
    gis/
    communications/
  persistence/
  observability/
  security/
  simulation/
tests/
  unit/
  integration/
  contract/
  scenarios/
examples/
docs/
migrations/
pyproject.toml
docker-compose.yml
.env.example
Makefile
README.md
```

Adapt this to existing repository conventions rather than creating unnecessary duplication.

# Required test scenarios

Implement automated end-to-end tests for at least these cases.

## Scenario 1 — HFC common-cause impairment

Multiple modems show related RF degradation on one shared node.

Expected:

- Correlation identifies common cause.
- Events attach to one parent incident or problem.
- No duplicate customer-premises truck rolls.
- Plant action is selected.
- Affected customers receive appropriate updates.

## Scenario 2 — HFC remote repair succeeds

A single CPE has a provisioning or recoverable configuration issue.

Expected:

- Remote repair is approved by policy.
- Typed action executes.
- NXT and service tests validate restoration.
- Incident closes without a truck roll.

## Scenario 3 — Remote repair fails, then Clean Boots succeeds

Expected:

- Failed remote attempt is recorded.
- Workflow returns to RCA.
- Clean Boots dispatch is optimized and approved.
- First field visit resolves the issue.
- No MR is created.
- Closure waits for validation.

## Scenario 4 — Guided self-help succeeds

Expected:

- Instructions are selected and delivered.
- Completion is captured.
- Telemetry validates restoration.
- No truck roll is created.

## Scenario 5 — Self-help fails, then Clean Boots dispatch

Expected:

- Failed self-help attempt remains linked to the incident.
- Dispatch package includes prior evidence.
- SLA clock does not reset.

## Scenario 6 — Clean Boots hands HFC case over at tap

Expected:

- Exact tap is identified.
- Handover evidence is complete.
- Existing outage and MR are checked.
- One jTrack MR is created or updated.
- Incident stays active.
- Dirty Boots completes repair.
- NXT validates restoration.
- All linked records close in sequence.

## Scenario 7 — Clean Boots hands PON case over at ODP

Expected behavior equivalent to Scenario 6 using PON topology and optical evidence.

## Scenario 8 — Incomplete MR evidence

Expected:

- MR creation is blocked.
- Structured missing-evidence reasons are returned.
- Workflow routes back to Clean Boots evidence collection or RCA.
- No duplicate MR is created.

## Scenario 9 — Dirty Boots repair fails

Expected:

- Failed MR attempt is recorded.
- Workflow returns to cross-domain RCA.
- A second MR or work order is not issued without a new reason.
- The same incident and SLA clock continue.

## Scenario 10 — Reverse handover

Plant is repaired, but the customer remains degraded due to an in-home issue.

Expected:

- Workflow returns from Dirty Boots to Clean Boots.
- Same incident continues.
- A linked Clean Boots work order is created.
- Repeat counts remain accurate.

## Scenario 11 — Joint dispatch

Evidence implicates both customer and plant domains.

Expected:

- Optimizer selects joint dispatch when superior to sequential visits.
- Required skills, tools, parts, access, and timing are validated.

## Scenario 12 — Duplicate event and replay

Expected:

- Duplicate webhook does not create duplicate incident, work order, remote action, or MR.
- Replaying a checkpoint is safe.

## Scenario 13 — Restart during approval

Expected:

- Graph pauses at an approval interruption.
- Application restarts.
- Same thread resumes from persisted state.
- No pre-approval side effect is repeated.

## Scenario 14 — Stale telemetry

Expected:

- Stale evidence is rejected.
- Current evidence is requested.
- Automated resolution does not proceed on stale data.

## Scenario 15 — Premature closure attempt

Expected:

- Closure is blocked when stability window, test result, or linked-record reconciliation is incomplete.

## Scenario 16 — Predictive maintenance

Expected:

- Degradation risk creates a preventive case.
- Action occurs before customer impact where policy permits.
- Any later customer incident links back to the preventive case.

A concrete example of this scenario is the post-install and twice-daily predictive Wi-Fi/CPE scan described under Predictive CPE/Wi-Fi scan reference.

## Scenario 17 — Puerto Rico remote-access constraint

Expected:

- Remote/island access, crew, parts, or ferry limitations affect the dispatch plan.
- An infeasible appointment is not committed.

# Quality requirements

Use:

- Python 3.12 or the repository’s established supported version.
- Current mutually compatible stable LangChain, LangGraph, and Pydantic releases.
- A lockfile.
- Async interfaces for I/O.
- Type hints throughout.
- Ruff or equivalent linting.
- Mypy or Pyright.
- Pytest.
- At least 85% test coverage for workflow, routing, policy, and adapter logic.
- Contract tests for every external adapter.
- No failing tests.
- No unresolved type errors.
- No secrets.
- No fabricated vendor APIs.

Verify exact LangGraph and LangChain APIs against current official documentation before coding — in particular `StateGraph` construction, checkpointer/persistence classes, and the current interrupt-and-resume pattern for human approval, since these have changed across LangGraph releases. Do not rely on possibly stale method signatures from memory.

# Required runnable demonstration

Provide a simulation mode with fixture data and commands such as:

```bash
make setup
make test
make lint
make typecheck
make demo
```

The demo must run at least the following, matching the numbered scenarios defined later in this document so the two lists cannot drift apart:

- HFC remote success (Scenario 2).
- HFC Clean Boots-to-tap MR (Scenario 6).
- PON Clean Boots-to-ODP MR (Scenario 7).
- Failed plant action and re-RCA (Scenario 9).
- Reverse handover (Scenario 10).
- Common-cause incident (Scenario 1).
- Predictive-maintenance case (Scenario 16).

Print or expose:

- Current process step.
- Decision result.
- Evidence used.
- Policy result.
- Pending approval.
- External actions.
- Work-order and MR links.
- Attempt counts.
- Validation result.
- Final KPI timestamps.

# Deliverables

Produce:

1. Working source code.
2. LangGraph workflow with persistent state.
3. Deterministic detector and optimizer interfaces.
4. Simulation adapters.
5. FastAPI service.
6. PostgreSQL persistence profile.
7. Human-approval resume flow.
8. Complete automated tests.
9. Mermaid diagrams.
10. Decision table.
11. Integration contracts.
12. Operations runbook.
13. Security and policy documentation.
14. KPI definitions.
15. Docker Compose development environment.
16. README with exact setup and demonstration commands.
17. A final implementation report listing:
    - What was implemented.
    - Assumptions made.
    - Commands run.
    - Test results.
    - Coverage.
    - Remaining vendor-integration gaps.
    - Risks before production use.

# Definition of done

The work is complete only when all of the following are demonstrated:

- One incident maps to one durable graph thread.
- The graph resumes after process restart.
- The SLA clock survives every handover.
- Remote, self-help, field, MR, repeat-work, and reverse-handover paths work.
- Multiple work orders remain linked to one incident.
- Duplicate events and retries do not duplicate side effects.
- Model outputs are structured and policy checked.
- Models cannot directly execute production actions.
- Dispatch is produced by a deterministic optimizer.
- Clean Boots cannot create an MR without the evidence contract.
- HFC tap and PON ODP handovers are represented explicitly.
- jTrack MR creation is idempotent.
- Failed work returns to RCA.
- Closure is blocked until restoration is stable and records reconcile.
- Operational KPIs can be calculated from stored events.
- All tests, linting, and type checks pass.
- The repository can be demonstrated without real NXT, jTrack, WFM, or TM Forum credentials.

# Execution instructions

Proceed in this order:

1. Inspect the repository and attached specifications.
2. Identify reusable code and existing conventions.
3. Create a concise implementation plan in `IMPLEMENTATION_PLAN.md`.
4. Create architecture decisions for major technical choices.
5. Build the domain models and canonical events.
6. Build policy, detector, and adapter interfaces.
7. Build the LangGraph state and routing logic.
8. Add subgraphs.
9. Add persistence and resumable approvals.
10. Add simulation adapters.
11. Add the API.
12. Add dispatch optimization.
13. Add observability and KPI events.
14. Add all required tests.
15. Add diagrams and documentation.
16. Run the complete quality suite.
17. Fix all failures.
18. Run the demonstration scenarios.
19. Produce the final implementation report.

## If time or context runs short

This is a large scope. If it cannot be finished responsibly in one pass:

- Keep `IMPLEMENTATION_PLAN.md` current as a running status file — what is done, in progress, and not started — so work is resumable.
- Prioritize in this order: correct domain models and state contract, the parent graph with real routing and policy checks, persistence and resumable approvals, then adapters, dispatch, tests, and finally documentation and diagrams.
- Prefer a smaller set of fully working, fully tested paths over a larger set of stubbed ones.
- State plainly in the final implementation report what is incomplete and why, rather than describing partial work as finished.

Do not respond with only a proposal. Implement the system in the repository. Make reasonable assumptions where specifications are incomplete, document those assumptions, and use safe simulation adapters rather than fabricating production integrations.