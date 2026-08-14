"""Configuration-driven policy: the pack, its types, and the engine that reads it.

Import from here rather than from the submodules. The split into `models` / `loader` / `engine` is
a file-size convenience; the contract is this namespace.

The shape of the layer in one paragraph: `pack.yaml` holds every operational threshold in the system
and nothing else does. `models.PolicyPack` is that file as validated types, including the
cross-section rules YAML cannot express -- an approver role must be one `rbac` recognises, an action
must name a defined risk class, the six interrupt kinds must all be configured. `loader.load_pack`
parses it, digests the parsed content into the version string, and caches on file identity.
`engine.PolicyEngine.evaluate` answers one question about one action, three ways, always with reason
codes and always with that version attached.

Two invariants hold across the whole layer, and both are properties of the code rather than promises
in a docstring:

* **Absence is never permission.** An action type with no row is blocked by `pack.rule_for()`
  returning `None`; an engine whose pack failed to load blocks everything with
  `POLICY_NO_MATCHING_RULE` and reports itself unhealthy. There is no path through `evaluate()` that
  returns `ALLOWED` without a matched rule.
* **The version describes the rules that were actually applied.** `policy_version` is
  `<declared>+<sha256 of parsed content>`, so editing a threshold without touching `version:` still
  changes what every subsequent decision records. A hand-maintained version alone would let the
  audit trail attribute a decision to rules that no longer exist.
"""

from lpr_cpe.policies.engine import PolicyEngine, PolicyInput
from lpr_cpe.policies.loader import (
    DEFAULT_PACK_PATH,
    PACK_PATH_ENV_VAR,
    PolicyPackError,
    clear_pack_cache,
    load_pack,
    parse_pack,
    policy_version,
    resolve_pack_path,
)
from lpr_cpe.policies.models import (
    ActionRule,
    ApprovalRule,
    AttemptLimits,
    BlastRadiusPolicy,
    ClosurePolicy,
    CustomerContactPolicy,
    DispatchPolicy,
    EscalationPolicy,
    EvidencePolicy,
    HealthBandPolicy,
    PolicyPack,
    RCAPolicy,
    ReconciliationPolicy,
    RiskClass,
    ScanPolicy,
    SLAPolicy,
    ValidationPolicy,
)

__all__ = [
    "DEFAULT_PACK_PATH",
    "PACK_PATH_ENV_VAR",
    "ActionRule",
    "ApprovalRule",
    "AttemptLimits",
    "BlastRadiusPolicy",
    "ClosurePolicy",
    "CustomerContactPolicy",
    "DispatchPolicy",
    "EscalationPolicy",
    "EvidencePolicy",
    "HealthBandPolicy",
    "PolicyEngine",
    "PolicyInput",
    "PolicyPack",
    "PolicyPackError",
    "RCAPolicy",
    "ReconciliationPolicy",
    "RiskClass",
    "SLAPolicy",
    "ScanPolicy",
    "ValidationPolicy",
    "clear_pack_cache",
    "load_pack",
    "parse_pack",
    "policy_version",
    "resolve_pack_path",
]
