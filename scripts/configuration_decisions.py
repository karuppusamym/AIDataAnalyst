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
    "tool_agent_interval_minutes": (OPT_IN, _SCHEDULED_AGENT),
    "classification_propagation_interval_minutes": (
        OPT_IN,
        "files proposals for human review on a cadence; an estate opts into that review load",
    ),
    "freshness_evaluation_interval_minutes": (
        OPT_IN,
        "an open CRITICAL freshness incident fails governed tools closed, so an estate opts "
        "in after approving its contracts (R11-B8)",
    ),
    "sql_guard_allowed_functions": (
        SUPPLIED,
        "the user-defined functions a deployment has reviewed for effects; empty refuses "
        "every function the SQL guard does not recognise as a built-in (R11-FP14)",
    ),
    "context_rebuild_interval_minutes": (
        OPT_IN,
        "drafts regenerated tools, descriptions and context product versions into review "
        "queues and releases source-change holds once they are approved, so an estate turns "
        "it on after change-signal processing (R11-FP16)",
    ),
    "change_signal_processing_interval_minutes": (
        OPT_IN,
        "a redefined view or retired table opens a CRITICAL incident that holds the governed "
        "tools over it, so an estate opts into those holds (R11-FP16)",
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
    "graphql_budget_enabled": (OPT_IN, "needs Redis for the budget buckets, as MCP's does"),
    "graphql_introspection_enabled": (
        OFF_BY_DESIGN,
        "R11-GQL01: clients discover the schema from the published SDL; on only for a "
        "development or staging explorer (PlatformAdmin and AgentDeveloper), and production "
        "refuses it",
    ),
    "okf_import_enabled": (
        OFF_BY_DESIGN,
        "R11-OKF03: imported OKF edits become pending proposals only; the import stays off "
        "until the row's hostile-content, round-trip and review-journey evidence is accepted",
    ),
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
    "anthropic_api_key": (SUPPLIED, "the provider credential"),
    "openrouter_api_key": (SUPPLIED, "the provider credential"),
    "mcp_client_allowed_hosts": (
        SUPPLIED,
        "the hosts an upstream MCP server may live on; empty means none can be reached",
    ),
    "embedding_route_required": (
        OFF_BY_DESIGN,
        "unset means required in staging and production and not in development or test",
    ),
    "mcp_tool_certification_required": (
        OFF_BY_DESIGN,
        "unset means required in staging and production and not in development or test",
    ),
    "ask_budget_enabled": (
        OPT_IN,
        "needs Redis, like the MCP and GraphQL budgets; the model-token quota bounds spend without it",
    ),
    "azure_openai_api_key": (
        SUPPLIED,
        "R11-MP01: the Azure OpenAI resource's key, for routes naming env://AZURE_OPENAI_API_KEY",
    ),
    "source_query_concurrency_enabled": (
        OPT_IN,
        "R11-MP25: needs Redis; the per-process line-of-business bound applies without it",
    ),
    "source_query_max_concurrent_overrides": (
        OPT_IN,
        "R11-MP25: a per-source limit a DBA asks for; the default limit applies when unset",
    ),
    "model_routes_by_purpose": (
        OPT_IN,
        "one route for every purpose is the default; a purpose route is named once it is approved",
    ),
    "openrouter_provider_order": (
        SUPPLIED,
        "the upstreams each OpenRouter alias may use; without an entry the route fails closed",
    ),
    "hmac_signing_vault_url": (SUPPLIED, "production refuses the local HMAC signer"),
    "hmac_signing_vault_token_reference": (SUPPLIED, "the signing vault credential"),
    "tokenization_vault_url": (SUPPLIED, "production refuses the local tokenization provider"),
    "tokenization_vault_token_reference": (SUPPLIED, "the tokenization vault credential"),
    "audit_archive_legal_hold_enabled": (
        BLOCKED,
        "R11-B9: needs an S3 bucket with Object Lock to verify against",
    ),
    "audit_archive_filesystem_root": (SUPPLIED, "only for a filesystem archive destination"),
    "worker_metrics_port": (
        OPT_IN,
        "R11-FP17: the fleet scheduler, graph projector and Temporal worker publish their "
        "series into their own process registry, which only this port exposes; 0 opens no "
        "port, because opening one changes a deployment's network surface and belongs with "
        "whoever configures the scrape (`infra/monitoring/README.md`)",
    ),
    # R11-FP17. Every quota below is Supplied rather than Opt-in, and the
    # distinction is the whole point: there is no capability here for an estate
    # to enable, only a number only they can know. Unset means no quota is
    # declared -- never a quota of zero -- and `aida.usage_quotas` then issues
    # no statement at all, so admission behaves exactly as it did before these
    # settings existed. Shipping a default would be this repository inventing an
    # operator's number, the same mistake the alert thresholds in
    # `infra/monitoring/` refuse to make, and a quota nobody chose that starts
    # refusing work on upgrade is worse than no quota.
    "analysis_run_daily_quota_per_organization": (
        SUPPLIED,
        "R11-FP17: analysis runs one tenant may consume per UTC day; the two "
        "`max_active_runs_per_*` concurrency limits still apply when unset",
    ),
    "analysis_run_daily_quota_per_datasource": (
        SUPPLIED,
        "R11-FP17: analysis runs one source may consume per UTC day, which bounds a "
        "repeatedly-rescanned source that the one-at-a-time concurrency limit does not",
    ),
    "model_token_daily_quota_per_organization": (
        SUPPLIED,
        "R11-FP17: model tokens one tenant may consume per UTC day, across every agent and "
        "every source -- wider than `AgentContract.daily_token_cap`, which is per contract",
    ),
    "model_token_daily_quota_per_datasource": (
        SUPPLIED,
        "R11-FP17: model tokens attributable to one source per UTC day; counts "
        "provider-reported and estimated tokens alike, since a cap that only counted "
        "billed tokens could be walked past by a provider that reports nothing",
    ),
    "parser_statement_daily_quota_per_organization": (
        SUPPLIED,
        "R11-FP17: SQL statements one tenant may put through the lineage parsers per UTC day",
    ),
    "parser_statement_daily_quota_per_datasource": (
        SUPPLIED,
        "R11-FP17: SQL statements one source may put through the lineage parsers per UTC "
        "day -- the compute a change burst over that source actually spends",
    ),
}
