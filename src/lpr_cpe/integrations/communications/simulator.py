"""Fixture-backed outbound customer contact: notifications, self-help, and what came back.

No messaging platform was supplied (A1/A2). Gaps COMMS-1 to COMMS-6.

**Quiet hours, contact caps and vulnerable-customer protection are not implemented here.** They are
policy, they live in `lpr_cpe.policies`, and they run before an `ActionRequest` reaches this
adapter. An adapter that also refused to send at 03:00 would be a second owner of a rule the policy
engine owns, and the two would disagree the first time someone changed one of them -- most likely by
this adapter quietly suppressing a message that policy had deliberately allowed for a P1 outage.

**No contact address is returned.** A send reports the *channel* it used and a masked destination,
never the number or the address, because this payload becomes an `ActionRecord` and then an audit
event, and a phone number written into an audit log is a phone number retained for as long as the
audit log is. `mask_by_shape` from `lpr_cpe.security.redaction` does the masking; the destination is
masked at this boundary rather than by a later log processor, since by then it has been copied.

The Spanish strings are ours and have not been reviewed by a translator. That is a real defect for a
Puerto Rico deployment, where Spanish is the majority language and a clumsy message is worse than an
English one -- gap COMMS-3.
"""

from __future__ import annotations

import re
from typing import Any

from lpr_cpe.domain.enums import ActionType
from lpr_cpe.domain.governance import ActionRequest
from lpr_cpe.integrations.base import AdapterError
from lpr_cpe.security.redaction import mask_by_shape
from lpr_cpe.simulation.fixtures.determinism import pick, unit
from lpr_cpe.simulation.simulated_base import SimulatedAdapterBase

#: Channels this simulator will pretend to send on. `app` is a push notification, `voice` is an
#: outbound call placed by an agent rather than by us -- which is why it is not in the set: nothing
#: in this workflow may cause a phone to ring.
SUPPORTED_CHANNELS: frozenset[str] = frozenset({"sms", "email", "app"})

#: Languages with templates. Anything else falls back to English *and says so* in a data-quality
#: note, rather than sending an untranslated string as though it were the customer's language.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"en", "es"})

#: A template slot. Deliberately narrow -- lowercase identifiers only -- so a literal brace in a
#: message body could never be mistaken for one.
_SLOT_PATTERN = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: The message catalogue. Keys are template ids; each has one string per language, with `{}`-style
#: named placeholders filled from `ActionRequest.parameters`.
#:
#: Templates rather than model-generated prose, and this is a deliberate constraint: a
#: customer-facing message is a regulated artefact in a telecoms deployment, and "the model wrote
#: something reasonable" is not a defence. The model may choose *which* template and fill its slots;
#: it does not get to write the sentence. Gap COMMS-2 is the approval workflow real templates would
#: need.
TEMPLATES: dict[str, dict[str, str]] = {
    "fault_detected": {
        "en": (
            "We have detected a problem affecting your internet service and our team is already "
            "working on it. Reference {incident_id}. No action is needed from you."
        ),
        "es": (
            "Hemos detectado un problema que afecta su servicio de internet y nuestro equipo ya "
            "esta trabajando en ello. Referencia {incident_id}. No necesita hacer nada."
        ),
    },
    "area_fault": {
        "en": (
            "A fault in your area is affecting internet service for several customers. Crews are "
            "on the way. Reference {incident_id}. Estimated update by {eta_local}."
        ),
        "es": (
            "Una averia en su zona esta afectando el servicio de internet de varios clientes. "
            "Nuestras cuadrillas van en camino. Referencia {incident_id}. Proxima actualizacion "
            "aproximada: {eta_local}."
        ),
    },
    "power_outage_related": {
        "en": (
            "Your internet service is affected by a power outage in your area, reported by the "
            "utility. Service should return when power does. Reference {incident_id}."
        ),
        "es": (
            "Su servicio de internet esta afectado por una interrupcion de energia en su zona, "
            "reportada por la companhia electrica. El servicio deberia regresar cuando vuelva la "
            "energia. Referencia {incident_id}."
        ),
    },
    "visit_booked": {
        "en": (
            "A technician visit is booked for {window_local}. Someone aged 18 or over must be "
            "home. Reference {incident_id}. Reply C to change the appointment."
        ),
        "es": (
            "Hemos programado una visita tecnica para {window_local}. Debe haber una persona "
            "mayor de 18 anhos en el hogar. Referencia {incident_id}. Responda C para cambiar la "
            "cita."
        ),
    },
    "resolved": {
        "en": (
            "Your internet service has been restored. Reference {incident_id}. If the problem "
            "continues, reply P and we will reopen it."
        ),
        "es": (
            "Su servicio de internet ha sido restablecido. Referencia {incident_id}. Si el "
            "problema continua, responda P y lo reabriremos."
        ),
    },
}

#: Self-help scripts, separate from notifications because they ask the customer to *do* something
#: and therefore have a success condition, a duration and a way to decline. A notification has none.
SELF_HELP_SCRIPTS: dict[str, dict[str, Any]] = {
    "reboot_gateway": {
        "expected_minutes": 6,
        "requires_customer_action": True,
        "en": (
            "Unplug the power from your internet box, wait 30 seconds, then plug it back in. It "
            "takes about 5 minutes to come back. Reply D when done, or N if you would rather not."
        ),
        "es": (
            "Desconecte la corriente de su equipo de internet, espere 30 segundos y vuelva a "
            "conectarlo. Tarda unos 5 minutos en volver. Responda L cuando termine, o N si "
            "prefiere no hacerlo."
        ),
    },
    "check_cable_connections": {
        "expected_minutes": 4,
        "requires_customer_action": True,
        "en": (
            "Please check that the cable into your internet box is finger-tight at both ends. "
            "Reply D when done, or N if you would rather not."
        ),
        "es": (
            "Verifique que el cable de su equipo de internet este bien ajustado en ambos "
            "extremos. Responda L cuando termine, o N si prefiere no hacerlo."
        ),
    },
    "move_device_closer": {
        "expected_minutes": 3,
        "requires_customer_action": True,
        "en": (
            "Try using your device in the same room as the internet box and tell us if it "
            "improves. Reply D when done, or N if you would rather not."
        ),
        "es": (
            "Intente usar su dispositivo en la misma habitacion que el equipo de internet y "
            "diganos si mejora. Responda L cuando termine, o N si prefiere no hacerlo."
        ),
    },
}


class SimulatedCommunicationsAdapter(SimulatedAdapterBase):
    """Notification and self-help sends, plus the inbound responses they produce."""

    system_name = "communications"
    external_ref_prefix = "MSG"

    # -- writes ----------------------------------------------------------------------------------

    async def send_notification(self, request: ActionRequest) -> dict[str, Any]:
        """Tell the customer something. Goes through the gate; never performs I/O.

        No reply is expected, so nothing here creates a pending response. A notification whose
        outcome depended on the customer answering would be self-help wearing the wrong name.
        """
        if request.action_type is not ActionType.NOTIFY_CUSTOMER:
            raise AdapterError(
                self.system_name,
                f"send_notification requires action_type=notify_customer, "
                f"got {request.action_type.value}",
                retryable=False,
            )
        params = request.parameters
        template_id = str(params.get("template_id", "fault_detected"))
        if template_id not in TEMPLATES:
            raise AdapterError(
                self.system_name,
                f"unknown template_id {template_id!r}; known templates are "
                + ", ".join(sorted(TEMPLATES)),
                retryable=False,
            )
        channel = self._resolve_channel(params)
        language, language_notes = self._resolve_language(params)
        body, missing_slots = self._render(TEMPLATES[template_id][language], request)
        return self.simulate_write(
            request,
            detail=f"{template_id} notification prepared for {request.target_ref} via {channel}",
            extra={
                "message_kind": "notification",
                "template_id": template_id,
                "channel": channel,
                "language": language,
                "body": body,
                # Masked here, at the boundary, not by a downstream log processor. See the module
                # docstring: by the time a processor sees this it has already been copied.
                "destination_masked": self._masked_destination(params),
                "expects_response": False,
                # Named because a slot the template wanted and did not get is a message that went
                # out saying "by {eta_local}". A caller must be able to see that without diffing
                # strings.
                "unfilled_slots": missing_slots,
                "data_quality_notes": language_notes
                + (
                    [f"template slots not supplied: {', '.join(missing_slots)}"]
                    if missing_slots
                    else []
                ),
            },
        )

    async def send_self_help(self, request: ActionRequest) -> dict[str, Any]:
        """Ask the customer to try something. Goes through the gate; never performs I/O.

        Records a *deadline*, not a result. Whether the customer complied is discovered by
        `fetch_customer_responses`, because the alternative -- returning "self-help succeeded" from
        the send -- would let the workflow close an incident on the strength of having asked.
        """
        if request.action_type is not ActionType.SEND_SELF_HELP:
            raise AdapterError(
                self.system_name,
                f"send_self_help requires action_type=send_self_help, "
                f"got {request.action_type.value}",
                retryable=False,
            )
        params = request.parameters
        script_id = str(params.get("script_id", "reboot_gateway"))
        if script_id not in SELF_HELP_SCRIPTS:
            raise AdapterError(
                self.system_name,
                f"unknown script_id {script_id!r}; known scripts are "
                + ", ".join(sorted(SELF_HELP_SCRIPTS)),
                retryable=False,
            )
        script = SELF_HELP_SCRIPTS[script_id]
        channel = self._resolve_channel(params)
        language, language_notes = self._resolve_language(params)
        body, missing_slots = self._render(str(script[language]), request)
        wait_minutes = float(params.get("response_wait_minutes", 30))
        return self.simulate_write(
            request,
            detail=f"{script_id} self-help prepared for {request.target_ref} via {channel}",
            extra={
                "message_kind": "self_help",
                "script_id": script_id,
                "channel": channel,
                "language": language,
                "body": body,
                "destination_masked": self._masked_destination(params),
                "expects_response": True,
                "expected_minutes": script["expected_minutes"],
                "requires_customer_action": script["requires_customer_action"],
                "response_deadline": self._offset_hours(wait_minutes / 60.0),
                "unfilled_slots": missing_slots,
                "data_quality_notes": language_notes,
            },
        )

    # -- read ------------------------------------------------------------------------------------

    async def fetch_customer_responses(self, incident_id: str) -> list[dict[str, Any]]:
        """Replies to self-help sent for one incident. **Collection query**: `[]` when none.

        Only self-help sends produce responses, and only sends this process made: an incident with
        no self-help returns `[]`, which is the same answer as "nothing yet" and is correct for
        both.

        The reply is deterministic in the incident id, seeded through `unit`, so a scenario
        asserting "the customer declined" keeps getting a decline. All three real outcomes are
        reachable -- completed, declined, and no reply at all -- because a self-help step that can
        only succeed makes the dispatch path unreachable, and the dispatch path is most of the
        workflow.
        """
        self._ensure_available()
        out: list[dict[str, Any]] = []
        for stored in self._ledger.values():
            if stored.get("message_kind") != "self_help":
                continue
            if stored.get("incident_id") != incident_id:
                continue
            script_id = str(stored["script_id"])
            seed = f"{incident_id}:{script_id}"
            roll = unit(seed, "self_help_reply")
            if roll < 0.15:
                # No reply. Represented as an absent entry rather than a "timed_out" response,
                # because a timeout is the caller's conclusion from the deadline it set, not a
                # message the customer sent. Inventing one would put words in their mouth.
                continue
            completed = roll >= 0.35
            out.append(
                {
                    "incident_id": incident_id,
                    "message_ref": stored["external_ref"],
                    "script_id": script_id,
                    "channel": stored["channel"],
                    "language": stored["language"],
                    "response": "completed" if completed else "declined",
                    "customer_completed_step": completed,
                    # Deterministic, and always inside the window the send allowed, so a caller
                    # comparing against `response_deadline` never sees a reply from the future.
                    "responded_at": self._offset_hours(-0.1 * (1.0 + roll)),
                    "free_text": None,
                    "decline_reason": (
                        None
                        if completed
                        else pick(seed, "decline", ("not_at_home", "unwilling", "cannot_reach_box"))
                    ),
                    "data_available": True,
                    "data_quality_notes": [],
                    **self._provenance(incident_id),
                }
            )
        out.sort(key=lambda r: str(r["responded_at"]), reverse=True)
        return out

    # -- helpers ---------------------------------------------------------------------------------

    def _resolve_channel(self, parameters: dict[str, Any]) -> str:
        """The channel to use, refusing any this adapter will not send on.

        Refusing rather than falling back to SMS: a caller that asked for `voice` believes a human
        will speak to the customer, and silently sending a text instead means the workflow records a
        contact that did not happen in the way it thinks it did.
        """
        channel = str(parameters.get("channel", "sms"))
        if channel not in SUPPORTED_CHANNELS:
            raise AdapterError(
                self.system_name,
                f"channel {channel!r} is not supported; supported channels are "
                + ", ".join(sorted(SUPPORTED_CHANNELS)),
                retryable=False,
            )
        return channel

    @staticmethod
    def _resolve_language(parameters: dict[str, Any]) -> tuple[str, list[str]]:
        """The language to send in, plus a note when it is not the one that was asked for."""
        requested = str(parameters.get("language", "es"))
        if requested in SUPPORTED_LANGUAGES:
            return requested, []
        return "en", [
            f"no {requested!r} template exists; sent in English. A message in the wrong language "
            "is a contact attempt that did not land, not a successful one"
        ]

    @staticmethod
    def _render(template: str, request: ActionRequest) -> tuple[str, list[str]]:
        """Fill a template's slots from the request, reporting any it could not fill.

        Unfilled slots are left visible as `{eta_local}` rather than blanked. A message with an
        obvious hole in it is caught in review; one with a smooth gap reads as finished and ships.

        `str.format` is not used, and not for style: it raises `KeyError` on the first missing slot,
        so the caller learns about one hole at a time and learns it as an exception rather than as a
        field on the result. Only scalars are eligible as slot values -- a list formatted into a
        customer message renders as `['a', 'b']`, and a dict could carry a field the message should
        never contain.
        """
        slots: dict[str, Any] = {
            "incident_id": request.incident_id,
            "target_ref": request.target_ref,
            **{k: v for k, v in request.parameters.items() if isinstance(v, str | int | float)},
        }
        missing: list[str] = []

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in slots:
                return str(slots[name])
            if name not in missing:
                missing.append(name)
            return match.group(0)

        return _SLOT_PATTERN.sub(substitute, template), missing

    @staticmethod
    def _masked_destination(parameters: dict[str, Any]) -> str | None:
        """The destination, masked by shape, or `None` when the caller did not supply one.

        `None` rather than `"unknown"`: the send is addressed by the platform from the customer
        record in a real deployment, so "we were not told" and "there is no destination" are
        different facts and only one of them is a defect.
        """
        destination = parameters.get("destination")
        return None if destination is None else mask_by_shape(str(destination))
