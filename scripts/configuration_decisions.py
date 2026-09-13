"""R11-S9: the decision recorded for every setting that ships off, empty or zero.

`generate_configuration_inventory.py` finds these settings; it cannot decide
them, and a generated file that proposed retirements would be read as having
made them. So the decisions live here, written by a person, and
`tests/test_configuration_inventory.py` fails when a setting ships off without
one -- or when a decision outlives its setting.

Four kinds, and what each commits to:

* **Supplied** -- a value only the deployment has (an issuer, a vault, a key, a
  destination). Empty is "not configured", never "disabled", and there is
  nothing to enable on the platform's side.
* **Off by design** -- a switch whose off state is the safe one. The reason
  names what would have to change before it is turned on.
* **Opt-in** -- a capability that works, and that an estate turns on after a
  precondition the reason names: a service to run, a contract to approve, a
  behaviour change to review.
* **Blocked** -- waiting on a named tracker row.

Decided 2026-09-13 on the product owner's delegation. The one retirement --
`dq_itsm_webhook_enabled`, which gated its own URL a second time -- is gone
from the settings, so it is gone from here.
"""

from __future__ import annotations

SUPPLIED = "Supplied"
OFF_BY_DESIGN = "Off by design"
OPT_IN = "Opt-in"
BLOCKED = "Blocked"

KINDS = frozenset({SUPPLIED, OFF_BY_DESIGN, OPT_IN, BLOCKED})

_SCHEDULED_AGENT = (
    "each run is a governed agent run under an approved contract; an estate "
    "schedules it once that contract exists"
)

DECISIONS: dict[str, tuple[str, str]] = {
    "oidc_issuer": (SUPPLIED, "the identity provider's issuer"),
    "oidc_audience": (SUPPLIED, "the audience the provider issues tokens for"),
    "oidc_jwks_url": (SUPPLIED, "one of this or `oidc_jwks_json` is required with OIDC"),
    "oidc_jwks_json": (SUPPLIED, "a pinned key set, instead of `oidc_jwks_url`"),
    "oidc_role_mappings": (SUPPLIED, "maps the provider's group claims to platform roles"),
    "oidc_persona_mappings": (SUPPLIED, "maps provider claims to personas"),
    "oidc_default_persona": (SUPPLIED, "the persona for a principal no mapping names"),
    "secrets_vault_url": (SUPPLIED, "production refuses the environment secret provider"),
    "secrets_vault_token": (SUPPLIED, "the vault credential"),
    "neo4j_password": (SUPPLIED, "only for an organization served from Neo4j"),
    "object_store_secret_key": (SUPPLIED, "the object store credential"),
    "reaper_retention_overrides": (
        OPT_IN,
        "per-rule retention; the rule defaults apply when unset",
    ),
    "lineage_cache_enabled": (
        OPT_IN,
        "needs the optional Redis service (`--profile cache`); a Redis error is a cache miss",
    ),
    "lineage_neo4j_read_enabled": (
        OFF_BY_DESIGN,
        "INV-9: Neo4j reads wait for the projection rebuild drill (E5)",
    ),
    "reviewer_agent_enabled": (
        OFF_BY_DESIGN,
        "R11-C3: measured unsafe, and production refuses it",
    ),
    "reviewer_agent_suspended": (
        OFF_BY_DESIGN,
        "the process-wide kill switch; off means agents act only under their contracts",
    ),
    "steward_agent_interval_minutes": (OPT_IN, _SCHEDULED_AGENT),
    "lineage_agent_interval_minutes": (OPT_IN, _SCHEDULED_AGENT),
    "quality_agent_interval_minutes": (OPT_IN, _SCHEDULED_AGENT),
    "classification_propagation_interval_minutes": (
        OPT_IN,
        "files proposals for human review on a cadence; an estate opts into that review load",
    ),
    "freshness_evaluation_interval_minutes": (
        OPT_IN,
        "an open CRITICAL freshness incident fails governed tools closed, so an estate opts "
        "in after approving its contracts (R11-B8)",
    ),
    "governance_notifications_enabled": (
        OPT_IN,
        "needs a Slack or Teams destination to deliver to (R11-I1, R11-B10)",
    ),
    "delivery_worker_enabled": (
        OPT_IN,
        "needs a delivery destination (R11-I1, R11-B10)",
    ),
    "slack_webhook_url": (SUPPLIED, "the Slack destination"),
    "teams_webhook_url": (SUPPLIED, "the Teams destination"),
    "portal_base_url": (SUPPLIED, "links in notifications are omitted when unset"),
    "mcp_budget_enabled": (OPT_IN, "needs Redis for the budget buckets"),
    "quality_seasonal_thresholds_enabled": (
        OPT_IN,
        "changes VOLUME_CHANGE verdicts; an estate reviews it against its own history first",
    ),
    "quality_seasonal_month_end_enabled": (
        OPT_IN,
        "the month-end refinement of the seasonal baseline, reviewed the same way",
    ),
    "quality_certification_expiry_enabled": (
        OPT_IN,
        "opens incidents at write time; a reviewed opt-in, not a behaviour change on upgrade",
    ),
    "principal_reconciliation_enabled": (
        OPT_IN,
        "useful only once an identity source emits principal lifecycle events",
    ),
    "vector_index_url": (
        SUPPLIED,
        "the persisted vector index; the vector channel embeds live when unset (R11-B2)",
    ),
    "embedding_credential_reference": (SUPPLIED, "the embedding provider credential"),
    "entitlement_webhook_url": (SUPPLIED, "the entitlement fulfilment target"),
    "entitlement_webhook_token": (SUPPLIED, "the entitlement webhook credential"),
    "dq_itsm_webhook_url": (
        SUPPLIED,
        "the ITSM target; setting it is the opt-in (R11-S9 retired the separate switch)",
    ),
    "dq_itsm_webhook_token": (SUPPLIED, "the ITSM webhook credential"),
    "agent_query_memory_enabled": (
        OPT_IN,
        "changes what grounds a generated prompt; the SQL still passes the gateway",
    ),
    "model_generation_enabled": (OPT_IN, "requires an approved model route, checked at startup"),
    "model_route": (SUPPLIED, "the approved route generation uses"),
    "model_route_fallbacks": (SUPPLIED, "approved routes tried in order after the primary"),
    "model_endpoint_urls": (SUPPLIED, "private model endpoints by alias"),
    "openai_api_key": (SUPPLIED, "the provider credential"),
    "gemini_api_key": (SUPPLIED, "the provider credential"),
    "hmac_signing_vault_url": (SUPPLIED, "production refuses the local HMAC signer"),
    "hmac_signing_vault_token_reference": (SUPPLIED, "the signing vault credential"),
    "tokenization_vault_url": (SUPPLIED, "production refuses the local tokenization provider"),
    "tokenization_vault_token_reference": (SUPPLIED, "the tokenization vault credential"),
    "audit_archive_legal_hold_enabled": (
        BLOCKED,
        "R11-B9: needs an S3 bucket with Object Lock to verify against",
    ),
    "audit_archive_filesystem_root": (SUPPLIED, "only for a filesystem archive destination"),
}
