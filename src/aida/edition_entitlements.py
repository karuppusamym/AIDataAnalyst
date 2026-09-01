"""PG-5: edition entitlement evaluation. Pure, DB-free -- given an edition and
a requested capability, ALLOW or DENY, with a reason naming which edition the
capability requires and which edition the organization actually has.

**This is a different concept from `aida.entitlements`.** That module's
`EntitlementResult`/`apply_entitlement` provision *data-product access
grants* through an external webhook -- a fulfillment side-effect for a
`DataProductAccessRequest` the governance-review flow already approved.
Nothing in it evaluates whether an organization's product edition includes a
capability, despite the shared vocabulary; `Docs/90-reference/01-glossary.md`
already defines "Entitlement" as "edition or licence gating, evaluated
alongside permissions" (the concept this module implements), so the name
collision is a pre-existing one in the codebase, not one introduced here.
This module is named `edition_entitlements` specifically so the two are never
confused at an import site.

**Design mirrors `Docs/00-product/07-packaging-and-editions.md` §3's
capability matrix.** `CAPABILITY_MIN_EDITION` is that table transcribed as
data: for each capability the doc marks with a column, the value here is the
lowest edition (Foundation < Enterprise < Regulated) at which the doc shows
the capability as available at all -- "*full*" or "*bounded/partial*" both
count as ALLOW here, since this evaluator answers a boolean gate question,
not a metering or feature-completeness one. A capability marked "not offered"
at an edition is DENY at that edition and every edition below it.

**Fails closed on an unregistered capability id** (INV-4-style default-deny,
the same posture `policy_engine.py` documents for ABAC): a capability this
evaluator has never heard of is a bug at the call site -- a typo'd or
newly-added capability id that was never added to the table -- and should be
loud, not silently free.

**Reason codes only, never resource detail (INV-6 discipline, matching
`AuthorizationDenied` in `authorization_gate.py`).** `EntitlementDecision`
carries the capability id and the two edition names -- both closed,
non-secret vocabulary defined in this module and the packaging doc, not
caller-supplied or resource-derived values -- and nothing else.
"""

from dataclasses import asdict, dataclass
from typing import Any, Literal

Edition = Literal["FOUNDATION", "ENTERPRISE", "REGULATED"]

_EDITION_RANK: dict[Edition, int] = {"FOUNDATION": 0, "ENTERPRISE": 1, "REGULATED": 2}

# Transcribed from Docs/00-product/07-packaging-and-editions.md §3. Comment on
# each entry notes the doc's own marker where it is anything other than a
# plain "not offered below this edition, offered (fully) at and above it".
CAPABILITY_MIN_EDITION: dict[str, Edition] = {
    "catalog_discovery_search": "FOUNDATION",
    "profiling_and_classification": "FOUNDATION",
    "query_and_dbt_lineage": "FOUNDATION",
    "etl_openlineage_bi_lineage": "ENTERPRISE",
    "semantic_layer_and_metrics": "FOUNDATION",
    "glossary_and_stewardship_workflows": "FOUNDATION",  # "basic" (◐) at Foundation
    "knowledge_graph_explorer": "FOUNDATION",  # "bounded" (◐) at Foundation
    "data_quality_thresholds_and_incidents": "FOUNDATION",
    "data_quality_runtime_coupling": "ENTERPRISE",
    "ai_analyst_governed": "FOUNDATION",
    "governed_tool_registry": "FOUNDATION",
    "multi_step_tool_plans": "ENTERPRISE",
    "mcp_context_products": "ENTERPRISE",
    "studio_semantic_and_tool_authoring": "ENTERPRISE",
    "rbac": "FOUNDATION",
    "abac_purpose_based_access": "ENTERPRISE",
    "delegated_source_identity": "ENTERPRISE",  # "partial" (◐) at Enterprise
    "maker_checker_governance": "FOUNDATION",
    "audit_ledger": "FOUNDATION",
    "worm_archive_and_siem_routing": "ENTERPRISE",  # "partial" (◐) at Enterprise
    "compliance_packs": "REGULATED",
    "source_side_connector_agents_restricted_zones": "ENTERPRISE",  # "partial" (◐)
    "multi_region_dr_with_failover": "ENTERPRISE",  # "partial" (◐) at Enterprise
    "kill_switch_and_model_risk_harness": "FOUNDATION",  # "partial" (◐) at Foundation
}

# Reason codes. Named the way `policy_engine.py`/`authorization_gate.py`
# name theirs: an all-caps token a caller can branch on or log, never a
# free-text sentence.
ALLOWED = "ENTITLEMENT_ALLOWED"
DENIED_EDITION_INSUFFICIENT = "ENTITLEMENT_EDITION_INSUFFICIENT"
DENIED_CAPABILITY_UNREGISTERED = "ENTITLEMENT_CAPABILITY_UNREGISTERED"


@dataclass(frozen=True, slots=True)
class EntitlementDecision:
    """What the evaluator decided, and why -- inspectable like every other
    governance decision in this codebase (the PG-1/PG-6 convention): the
    reason names which edition the capability requires and which edition the
    organization actually has, nothing about the request itself.
    """

    allowed: bool
    capability: str
    reason_code: str
    organization_edition: str
    required_edition: str | None

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_entitlement(*, organization_edition: Edition, capability: str) -> EntitlementDecision:
    """Pure, DB-free: given an edition and a capability id, ALLOW or DENY.

    No I/O, no session, no settings object -- callers resolve the edition
    (today, `Settings.edition`; see that field's docstring for why it is
    deployment-wide rather than per-`Organization`) and pass it in, so this
    function is trivially unit-testable and safe to call from a hot path.
    """
    required = CAPABILITY_MIN_EDITION.get(capability)
    if required is None:
        return EntitlementDecision(
            allowed=False,
            capability=capability,
            reason_code=DENIED_CAPABILITY_UNREGISTERED,
            organization_edition=organization_edition,
            required_edition=None,
        )
    if _EDITION_RANK[organization_edition] >= _EDITION_RANK[required]:
        return EntitlementDecision(
            allowed=True,
            capability=capability,
            reason_code=ALLOWED,
            organization_edition=organization_edition,
            required_edition=required,
        )
    return EntitlementDecision(
        allowed=False,
        capability=capability,
        reason_code=DENIED_EDITION_INSUFFICIENT,
        organization_edition=organization_edition,
        required_edition=required,
    )
