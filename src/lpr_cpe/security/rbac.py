"""Roles, the tool allowlist, and who may satisfy which approval.

Two questions, deliberately kept apart:

* **"may this role *request* this action?"** -- `ToolAllowlist.permits`. A capability question,
  answered before policy is consulted.
* **"may this role *approve* this interrupt?"** -- `can_approve`. A separation-of-duties question.

They are different questions with different answers. A `field_technician` may request
`CREATE_WORK_ORDER` all day and may never approve a `HIGH_BLAST_RADIUS_ACTION`; `automation` may
request most remote repairs and may approve nothing at all, because an approval whose approver is
the system is not an approval.

**Both fail closed.** An unrecognised role permits nothing and approves nothing. That is what
makes a missing entry a visible bug (the action is refused, someone asks why) rather than an
invisible one (the action succeeds and nobody knows a rule was skipped).

This module does not authenticate anybody. It maps an already-established principal's role onto a
capability set. Authentication is the API's problem, and `policies` still gets the final say -- an
allowlisted action can still be blocked, require approval, or fall outside a maintenance window.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from lpr_cpe.domain.enums import ActionType, ApprovalKind

# --------------------------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------------------------


class Role(StrEnum):
    """The principals this system recognises.

    `AUTOMATION` is the graph itself acting unattended, and it is a *narrower* role than any human
    one, not a wider one: it may take the reversible remote actions and file the workflow records,
    and everything with a blast radius beyond one premises goes to an interrupt. `AUDITOR` is
    read-only by construction -- it exists so that "who can look at this without being able to
    change it?" has an answer other than "the admin".
    """

    NOC_OPERATOR = "noc_operator"
    NOC_SUPERVISOR = "noc_supervisor"
    FIELD_TECHNICIAN = "field_technician"
    OSP_ENGINEER = "osp_engineer"
    SERVICE_DESK = "service_desk"
    AUTOMATION = "automation"
    AUDITOR = "auditor"
    ADMIN = "admin"

    @classmethod
    def parse(cls, value: str | None) -> Role | None:
        """Best-effort parse of a role string from a token or an API payload.

        Returns `None` rather than raising or defaulting. A defaulted role is the failure this
        module exists to prevent: `Role(value)` with a fallback to `NOC_OPERATOR` would turn a
        typo in an identity provider's group name into a working operator account.
        """
        if not value:
            return None
        try:
            return cls(value.strip().lower())
        except ValueError:
            return None


# --------------------------------------------------------------------------------------------
# The allowlist
# --------------------------------------------------------------------------------------------

# Read-only actions. Every role that can see an incident at all can run these; they change nothing
# and refusing them only means a diagnosis happens with less information.
_READ_ONLY: Final[frozenset[ActionType]] = frozenset(
    {ActionType.READ_STATUS, ActionType.RUN_DIAGNOSTIC}
)

# Reversible remote repairs against one premises.
_SAFE_REMOTE: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.CPE_REBOOT,
        ActionType.CPE_RESYNC,
        ActionType.WIFI_CHANNEL_CHANGE,
        ActionType.WIFI_POWER_CHANGE,
    }
)

# Remote actions that lose customer configuration or take a service out for minutes, still scoped to
# one premises. Separated from `_SAFE_REMOTE` because a factory reset wipes a customer's Wi-Fi
# password and their smart-home pairings -- reversible for us, not for them.
_DISRUPTIVE_REMOTE: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.CPE_FIRMWARE_UPDATE,
        ActionType.CPE_FACTORY_RESET,
        ActionType.PROFILE_CHANGE,
        ActionType.REPROVISION,
    }
)

# Actions whose blast radius is a node, a PON port or a config population. No role gets these
# implicitly; the two that do are the ones a human supervisor sits behind.
_NETWORK_AFFECTING: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.NODE_LEVEL_RESET,
        ActionType.OLT_PORT_RESET,
        ActionType.BULK_CONFIG_PUSH,
    }
)

_WORKFLOW: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.SEND_SELF_HELP,
        ActionType.CREATE_WORK_ORDER,
        ActionType.CANCEL_WORK_ORDER,
        ActionType.RAISE_MR,
        ActionType.UPDATE_MR,
        ActionType.NOTIFY_CUSTOMER,
        ActionType.CLOSE_INCIDENT,
        ActionType.CREATE_PM_CASE,
    }
)

# The allowlist is DATA, not a chain of `if` statements, for four reasons that a conditional cannot
# give:
#
# 1. it is enumerable. A test can assert that every `Role` member has an entry and that every
#    `ActionType` member appears in at least one role's set -- so adding an `ActionType` without
#    deciding who may request it fails a test instead of silently being requestable by nobody, or
#    worse, by everybody through a permissive `else`.
# 2. it is reviewable. A security reviewer reads one table and sees the whole matrix. Fifty
#    `if role == ... and action in ...` branches have to be simulated in the head to be understood,
#    and a reviewer who has to simulate will miss the branch that matters.
# 3. it is diffable. "Field technicians can now request a reprovision" is a one-line diff with an
#    obvious meaning in a code review, not a modified boolean expression.
# 4. it can be exported. The same structure serialises into `docs/policy-controls.md` and into an
#    API response for a UI that greys out buttons, so the UI cannot disagree with the server.
_ALLOWLIST: Final[dict[Role, frozenset[ActionType]]] = {
    # The NOC operator drives an incident end to end but does not touch shared plant.
    Role.NOC_OPERATOR: _READ_ONLY | _SAFE_REMOTE | _DISRUPTIVE_REMOTE | _WORKFLOW,
    # The supervisor is the only human role that may request a network-affecting action -- and still
    # cannot execute it alone, because policy sends it to a HIGH_BLAST_RADIUS_ACTION interrupt that
    # the requester may not approve (see `can_approve`).
    Role.NOC_SUPERVISOR: _READ_ONLY
    | _SAFE_REMOTE
    | _DISRUPTIVE_REMOTE
    | _NETWORK_AFFECTING
    | _WORKFLOW,
    # Clean Boots in the field. Can retest and reboot the device in front of them, can record and
    # escalate; cannot reprovision, cannot reach anything shared, cannot close the incident --
    # closure requires validation over a stability window that outlasts the visit.
    Role.FIELD_TECHNICIAN: _READ_ONLY
    | _SAFE_REMOTE
    | frozenset(
        {
            ActionType.CREATE_WORK_ORDER,
            ActionType.CANCEL_WORK_ORDER,
            ActionType.RAISE_MR,
            ActionType.UPDATE_MR,
            ActionType.NOTIFY_CUSTOMER,
        }
    ),
    # Dirty Boots / OSP. Owns the plant record and the port-level actions on it; has no business
    # rebooting a customer's router or messaging a customer directly.
    Role.OSP_ENGINEER: _READ_ONLY
    | frozenset(
        {
            ActionType.OLT_PORT_RESET,
            ActionType.NODE_LEVEL_RESET,
            ActionType.RAISE_MR,
            ActionType.UPDATE_MR,
            ActionType.CREATE_WORK_ORDER,
            ActionType.CREATE_PM_CASE,
        }
    ),
    # First line. Can look, can guide the customer, can raise a visit. Cannot change the device.
    Role.SERVICE_DESK: _READ_ONLY
    | frozenset(
        {
            ActionType.SEND_SELF_HELP,
            ActionType.NOTIFY_CUSTOMER,
            ActionType.CREATE_WORK_ORDER,
        }
    ),
    # The graph running unattended. Everything here is either read-only, reversible against a single
    # premises, or a workflow record. `CPE_FACTORY_RESET`, `REPROVISION` and the network-affecting
    # set are absent on purpose: those need a named human, and `automation` is not one.
    Role.AUTOMATION: _READ_ONLY
    | _SAFE_REMOTE
    | frozenset(
        {
            ActionType.CPE_FIRMWARE_UPDATE,
            ActionType.PROFILE_CHANGE,
            ActionType.SEND_SELF_HELP,
            ActionType.CREATE_WORK_ORDER,
            ActionType.RAISE_MR,
            ActionType.UPDATE_MR,
            ActionType.NOTIFY_CUSTOMER,
            ActionType.CLOSE_INCIDENT,
            ActionType.CREATE_PM_CASE,
        }
    ),
    # Read-only by construction. Not even `RUN_DIAGNOSTIC`: a diagnostic takes a line out of service
    # briefly, so it is a change to the customer's experience even though it changes no record.
    Role.AUDITOR: frozenset({ActionType.READ_STATUS}),
    # Admin is deliberately NOT `set(ActionType)` spelled as a wildcard. It is the full set, written
    # as the full set, so that a new ActionType does not become admin-requestable the moment it is
    # declared -- someone has to decide.
    Role.ADMIN: _READ_ONLY
    | _SAFE_REMOTE
    | _DISRUPTIVE_REMOTE
    | _NETWORK_AFFECTING
    | _WORKFLOW,
}


class ToolAllowlist:
    """Which `ActionType` values a role may request.

    All methods are class-level: there is one allowlist in the process and an instantiable version
    would invite a caller to build a permissive copy. If a deployment ever needs a different
    matrix it belongs in the policy pack, which is versioned and audited, not in a constructor
    argument.
    """

    __slots__ = ()

    @staticmethod
    def for_role(role: Role | str | None) -> frozenset[ActionType]:
        """Everything `role` may request. An unknown role gets the empty set."""
        resolved = role if isinstance(role, Role) else Role.parse(role)
        if resolved is None:
            return frozenset()
        return _ALLOWLIST.get(resolved, frozenset())

    @staticmethod
    def permits(role: Role | str | None, action_type: ActionType) -> bool:
        """Whether `role` may request `action_type`. Fails closed on an unknown role.

        Accepts a raw string so an API handler can pass what the token carried without pre-parsing,
        and still get a refusal rather than an exception for a value that is not a role.
        """
        return action_type in ToolAllowlist.for_role(role)

    @staticmethod
    def roles_permitting(action_type: ActionType) -> frozenset[Role]:
        """Every role that may request `action_type`.

        Used to build the "required_role" text on an `ApprovalRequest` and to render the allowlist
        into `docs/policy-controls.md`, so the documentation is generated from the table rather than
        written alongside it and left to rot.
        """
        return frozenset(r for r, allowed in _ALLOWLIST.items() if action_type in allowed)

    @staticmethod
    def as_dict() -> dict[str, list[str]]:
        """Serialisable view, for the API and the generated docs. Sorted, so diffs are stable."""
        return {role.value: sorted(a.value for a in actions) for role, actions in
                sorted(_ALLOWLIST.items(), key=lambda kv: kv[0].value)}


# --------------------------------------------------------------------------------------------
# Approval authority
# --------------------------------------------------------------------------------------------

# Who may satisfy each of the six interrupts. Data for the same reasons as the allowlist above, and
# with one extra property worth stating: `AUTOMATION` appears in none of these sets. An approval
# granted by the system is not an approval, and the absence is enforced by a test that asserts
# `Role.AUTOMATION not in` any value here.
#
# The two supervisor-only entries are required by the specification's "human approval for
# destructive, network-wide, or high-blast-radius actions" and by the closure rule in
# `domain.closure`:
#
# * `HIGH_BLAST_RADIUS_ACTION` -- a node reset or bulk push affects customers who never reported a
#   fault, so the person accountable for that trade-off has to be the one who takes it.
# * `EXCEPTIONAL_CLOSURE` -- closing without proof. `ClosureRecord` already refuses a
# `CLOSED_NORMAL` without a passing validation, which leaves this as the only unproven exit, and
# it must cost a supervisor's name.
#
# A `FIELD_TECHNICIAN` therefore appears in exactly one set -- the handover they are party to -- and
# in neither supervisor-only one.
_APPROVERS: Final[dict[ApprovalKind, frozenset[Role]]] = {
    # A diagnosis review. Any NOC role can read the hypothesis set; the technician who will be sent
    # is included because they frequently know the premises history that the evidence does not
    # carry.
    ApprovalKind.LOW_CONFIDENCE_RCA: frozenset(
        {Role.NOC_OPERATOR, Role.NOC_SUPERVISOR, Role.OSP_ENGINEER, Role.ADMIN}
    ),
    # Single-premises but destructive (factory reset, reprovision). An operator suffices: the blast
    # radius is one customer and the action is logged against a named person.
    ApprovalKind.HIGH_RISK_REMOTE_ACTION: frozenset(
        {Role.NOC_OPERATOR, Role.NOC_SUPERVISOR, Role.ADMIN}
    ),
    # Sending a crew. Costs money and a customer appointment, not service.
    ApprovalKind.DISPATCH: frozenset({Role.NOC_OPERATOR, Role.NOC_SUPERVISOR, Role.ADMIN}),
    # Clean-to-Dirty. The receiving side must be able to accept or reject, which is the whole
    # point of the contract: an OSP engineer rejecting an incomplete packet here is the control
    # that stops a wasted second visit.
    ApprovalKind.CLEAN_TO_DIRTY_HANDOVER: frozenset(
        {Role.OSP_ENGINEER, Role.NOC_SUPERVISOR, Role.FIELD_TECHNICIAN, Role.ADMIN}
    ),
    # Supervisor-only. See the note above.
    ApprovalKind.HIGH_BLAST_RADIUS_ACTION: frozenset({Role.NOC_SUPERVISOR, Role.ADMIN}),
    ApprovalKind.EXCEPTIONAL_CLOSURE: frozenset({Role.NOC_SUPERVISOR, Role.ADMIN}),
}

# Kinds that require a supervisor. Derived from `_APPROVERS` rather than listed again, so the claim
# "a technician cannot approve a network-wide action" is a property of the table and not a second
# assertion that could drift from it.
SUPERVISOR_ONLY_KINDS: Final[frozenset[ApprovalKind]] = frozenset(
    kind
    for kind, roles in _APPROVERS.items()
    if roles <= {Role.NOC_SUPERVISOR, Role.ADMIN}
)


def can_approve(role: Role | str | None, kind: ApprovalKind) -> bool:
    """Whether `role` may satisfy an interrupt of `kind`. Fails closed.

    An unknown role, `None`, or `AUTOMATION` all return `False`. The last is the one worth naming:
    the graph can decide it *needs* an approval, and cannot supply one.
    """
    resolved = role if isinstance(role, Role) else Role.parse(role)
    if resolved is None:
        return False
    return resolved in _APPROVERS.get(kind, frozenset())


def approvers_for(kind: ApprovalKind) -> frozenset[Role]:
    """Every role that may approve `kind`.

    The `ApprovalRequest.required_role` field is a single string, so the API renders this set into
    the question the operator sees ("needs: noc_supervisor or admin") rather than each caller
    guessing.
    """
    return _APPROVERS.get(kind, frozenset())


def requires_supervisor(kind: ApprovalKind) -> bool:
    """Whether `kind` can only be satisfied by a supervisor (or admin)."""
    return kind in SUPERVISOR_ONLY_KINDS


def approvals_as_dict() -> dict[str, list[str]]:
    """Serialisable view of the approval matrix, for the API and the generated docs."""
    return {
        kind.value: sorted(r.value for r in roles)
        for kind, roles in sorted(_APPROVERS.items(), key=lambda kv: kv[0].value)
    }
