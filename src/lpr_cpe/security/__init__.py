"""PII masking, prompt-injection handling, and role-based access control.

Three controls from the specification's Security and privacy section, each with exactly one owner:

* **`redaction`** -- masking, applied at the *collection* boundary rather than at the model call.
  Doing it only before a model call is too late: state is checkpointed every super-step and nodes
  log as they go, so the unmasked value is already persisted and already in the log stream.
* **`injection`** -- the data-not-instruction rule for technician notes, customer statements and
  retrieved knowledge. The real control is that no code path executes those fields; `neutralize`
  reduces, and does not eliminate, the residual prompt-injection risk.
* **`rbac`** -- the role vocabulary, the tool allowlist and who may satisfy which approval. Both
  tables are data, both fail closed.

Secrets are not handled here: they come from `config.Settings` (env-prefixed, never in source
control), and the encryption-ready storage interfaces live in `persistence`.
"""

from lpr_cpe.security.injection import (
    INJECTION_PATTERNS,
    MAX_TEXT_CHARS,
    assert_data_not_instruction,
    contains_injection_attempt,
    neutralize,
)
from lpr_cpe.security.rbac import (
    SUPERVISOR_ONLY_KINDS,
    Role,
    ToolAllowlist,
    approvals_as_dict,
    approvers_for,
    can_approve,
    requires_supervisor,
)
from lpr_cpe.security.redaction import (
    MODEL_FORBIDDEN_KEYS,
    UNTRUSTED_TEXT_KEYS,
    looks_like_pii,
    mask_email,
    mask_free_text,
    mask_ip,
    mask_mac,
    mask_name,
    mask_phone,
    redact,
    redact_for_model,
)

__all__ = [
    "INJECTION_PATTERNS",
    "MAX_TEXT_CHARS",
    "MODEL_FORBIDDEN_KEYS",
    "SUPERVISOR_ONLY_KINDS",
    "UNTRUSTED_TEXT_KEYS",
    "Role",
    "ToolAllowlist",
    "approvals_as_dict",
    "approvers_for",
    "assert_data_not_instruction",
    "can_approve",
    "contains_injection_attempt",
    "looks_like_pii",
    "mask_email",
    "mask_free_text",
    "mask_ip",
    "mask_mac",
    "mask_name",
    "mask_phone",
    "neutralize",
    "redact",
    "redact_for_model",
    "requires_supervisor",
]
