"""Application settings (tracker ST-04, Phase 1 of
`Docs/40-engineering/06-refactor-plan.md`).

Moved verbatim from `aida.config`. `aida.config` now re-exports from here
for backward compatibility; new code should import from this module
directly.
"""

import difflib
import os
import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _running_under_pytest() -> bool:
    """Whether the current process is a pytest run.

    `PYTEST_VERSION` is set by pytest itself for the entire session, from
    collection onward -- including module-level `Settings()`/`get_settings()`
    calls at import time, unlike `PYTEST_CURRENT_TEST`, which is only set
    during an individual test's own run phase and would miss those imports.
    `sys.modules` is a belt-and-suspenders fallback for older pytest or
    invocations that skip the env var.
    """
    return "PYTEST_VERSION" in os.environ or "pytest" in sys.modules


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIDA_",
        env_file_encoding="utf-8",
        # C1 (2026-08-30 audit): a misspelled *value* (e.g. an invalid Literal)
        # already fails closed; a misspelled *name* used to be silently
        # discarded. "forbid" makes an unrecognized key passed directly to
        # Settings(**kwargs) -- or read from a non-env source, e.g. a secrets
        # file -- a loud error instead. It does NOT by itself catch a
        # misspelled *env var* name (pydantic-settings' env source drops
        # anything that doesn't match a known field before this ever sees it);
        # `reject_unrecognized_aida_env_vars` below covers that gap.
        extra="forbid",
    )

    service_name: str = "aida-control-plane"
    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    identity_provider: Literal["development", "oidc"] = "development"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_jwks_json: str | None = None
    oidc_subject_claim: str = "sub"
    oidc_roles_claim: str = "roles"
    oidc_organization_claim: str = "organization_id"
    oidc_principal_type_claim: str = "principal_type"
    oidc_business_purpose_claim: str = "business_purpose"
    oidc_role_mappings: dict[str, list[str]] = Field(default_factory=dict)
    # UX-1: the shell's persona-oriented nav (module 21 SS5) is derived from the same
    # verified OIDC groups claim used for role mapping, via a configurable claim path
    # and mapping dict -- never a browser-selectable value in production. A principal
    # can belong to several groups; the first one (in claim order) with a mapping to a
    # recognized persona wins, so the bank's group contract controls priority simply by
    # how groups are ordered in the token. `oidc_default_persona` is the landing
    # persona for a principal whose groups map to none of the configured personas.
    oidc_groups_claim: str = "groups"
    oidc_persona_mappings: dict[str, str] = Field(default_factory=dict)
    oidc_default_persona: str | None = None
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=86_400)
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    credential_provider: Literal["env", "vault", "cyberark", "aws-sm", "azure-kv", "gcp-sm"] = "env"
    secret_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)
    # AU-10: HashiCorp Vault's KV v2 secrets engine, the `credential_provider="vault"`
    # implementation of `aida.secrets.SecretProvider` (see
    # `aida.secrets.VaultKvSecretProvider`) -- the provider every production-valid
    # `credential_provider` reference actually resolves through, since "env" is
    # forbidden in production below. `secrets_vault_token` is the bootstrap credential
    # that authenticates *to* Vault; it is deliberately not itself a `SecretResolver`
    # reference (that would be circular -- this is the "vault" scheme provider
    # `SecretResolver` uses to resolve every other reference, including
    # `hmac_signing_vault_token_reference` and `tokenization_vault_token_reference`
    # below). It is injected directly into process config the way a Vault Agent
    # auto-auth sidecar (or an equivalent platform mechanism) ordinarily delivers a
    # short-lived root token -- the same directly-injected-bearer-credential shape
    # `entitlement_webhook_token` already uses.
    secrets_vault_url: str | None = Field(default=None, max_length=500)
    secrets_vault_token: SecretStr | None = Field(default=None)
    secrets_vault_kv_mount: str = Field(default="secret", max_length=200)
    secrets_vault_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    database_url: str = "postgresql+asyncpg://aida:aida-local-only@localhost:5432/aida"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "aida-metadata"
    temporal_enabled: bool = True
    # AU-12: `Client.connect` performs a real RPC handshake by default (lazy=False)
    # and has no built-in timeout, so an unreachable/slow Temporal server used to
    # hang or raise inside `lifespan` and take the whole process down with it --
    # including every read-only, non-Temporal-dependent route. This bounds that
    # handshake; `aida.main.lifespan` treats a timeout or connect error as a
    # degraded-start (not fatal) condition and hands off to a background retry
    # loop (`temporal_reconnect_interval_seconds`) instead of crashing.
    temporal_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    temporal_reconnect_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    metadata_batch_max_chunks: int = Field(default=1_000, ge=1, le=10_000)
    metadata_batch_max_tables: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    metadata_batch_max_columns: int = Field(default=5_000_000, ge=10_000, le=50_000_000)
    # ING-4 / P0-01: on ingest of a table that has never been described,
    # emit `catalog.table.newly_created.v1` so a downstream handler can
    # auto-enqueue an asset-description draft (and, once an AnalysisRun
    # completes for the datasource, a semantic-inference proposal)
    # instead of the table sitting empty until a steward manually POSTs
    # each drafter endpoint. Kill switch: an operator can set
    # `AIDA_AUTO_ENQUEUE_ON_INGEST=false` to suppress the emission
    # entirely -- the ingest path itself keeps working exactly as
    # before. Consumed by `persist_discovery_snapshot` in
    # `src/aida/workflows/activities.py` and by
    # `handle_newly_created_table` in `src/aida/newly_created_table_drafter.py`.
    auto_enqueue_on_ingest: bool = True
    # GV-2 / P0-02: catalog-router bulk-ownership and bulk-certify endpoints
    # (`atlas.modules.catalog.router.bulk_assign_ownership`,
    # `bulk_certify_tables`) used to write straight to ACTIVE under only
    # RBAC, letting a `DataSteward` bypass the maker-checker contract that
    # `aida.stewardship_api._create_bulk_operation` enforces for the same
    # subjects on the governed path. The catalog router now consults these
    # two knobs before deciding whether a given call may direct-write:
    #
    #   * `bulk_governance_threshold` -- an item count above which the
    #     operation MUST go through `BulkStewardshipOperation` +
    #     `GovernanceReview`, no matter the caller's role. Default 10,
    #     sized so a small manual clean-up still lands immediately but a
    #     wholesale reassignment can never be a one-person action.
    #   * `bulk_governance_roles_requiring_review` -- roles that always
    #     route through review regardless of count. Default
    #     `["DataSteward"]` (the role the audit flagged as bypass-capable).
    #     Higher-privileged admins are deliberately absent so they can
    #     still direct-write within the count threshold, matching the
    #     "single deliberate action by an authorized user" comment on
    #     the single-item endpoints. The RBAC allowed-writers list
    #     (`CATALOG_BULK_ACTION_WRITE_ROLES`) is unchanged; this filter
    #     only decides which of the already-authorized callers may skip
    #     review.
    bulk_governance_threshold: int = Field(default=10, ge=0, le=10_000)
    bulk_governance_roles_requiring_review: list[str] = Field(
        default_factory=lambda: ["DataSteward"]
    )
    redis_url: str = "redis://localhost:6379/0"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    kafka_bootstrap_servers: str = "localhost:19092"
    object_store_endpoint: str = "http://localhost:9000"
    object_store_access_key: str = "aida"
    object_store_secret_key: str = ""
    default_query_row_limit: int = Field(default=5000, ge=1, le=100_000)
    hard_query_row_limit: int = Field(default=100_000, ge=1, le=1_000_000)
    query_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    # QG-3: fairness under contention. Each line of business (DataSource.line_of_
    # business_id, the same per-LOB dimension aida.cost_showback already groups
    # QueryExecution rows by) may hold at most this many concurrently in-flight
    # executions against the gateway's real source-execution path
    # (aida.lob_concurrency.LobConcurrencyController, checked in
    # QueryExecutionGateway.execute). A single default applied to every LOB, not a
    # per-LOB override table -- a persisted override would need a new schema this
    # item is deliberately not adding.
    query_gateway_lob_max_concurrent: int = Field(default=8, ge=1, le=1_000)
    # A request past its LOB's limit waits, bounded, for another in-flight
    # execution from the same LOB to free a slot, rather than either queuing
    # forever or rejecting on the first collision -- see aida.lob_concurrency's
    # module docstring. A wait that outlives this bound is rejected with a clear,
    # distinguishable error (LobConcurrencyRejected) instead of growing the queue.
    query_gateway_lob_queue_timeout_seconds: float = Field(default=5.0, gt=0, le=300)
    max_postgres_plan_cost: float = Field(default=1_000_000.0, gt=0)
    # BigQuery bills by bytes scanned rather than exposing a comparable cost plan,
    # so the gateway gates dry-run byte estimates against this separate budget
    # instead of max_postgres_plan_cost. Default is a conservative 10 GB per query
    # (~$0.05-0.06 at standard on-demand pricing); override per environment.
    max_bigquery_dry_run_bytes: int = Field(default=10_000_000_000, gt=0)
    profile_sample_rows: int = Field(default=10_000, ge=100, le=100_000)
    profile_column_batch_size: int = Field(default=40, ge=1, le=100)
    # PR-5: raised from the original `le=100_000` so a page-based plan can
    # actually reach the 1,000,000-table exit condition -- pagination alone
    # cannot compensate for a ceiling that makes a 1M-table run structurally
    # impossible regardless of continue-as-new.
    profile_max_tables_per_run: int = Field(default=5_000, ge=1, le=1_000_000)
    # PR-5: bounded page size for `plan_profile_tasks`'s keyset pagination --
    # each activity call plans at most this many tables, never the whole run
    # in one payload (which is what made large runs fatal at scale).
    profile_plan_page_size: int = Field(default=500, ge=1, le=10_000)
    # PR-5: once a single `DatasourceDiscoveryWorkflow` execution has
    # processed this many tables, it hands off to a fresh execution via
    # `workflow.continue_as_new` instead of letting its own history keep
    # growing. Deliberately a multiple of `profile_plan_page_size` in the
    # default so the boundary lands on a page edge, not mid-page.
    profile_continue_as_new_after_tables: int = Field(default=2_000, ge=1, le=1_000_000)
    # CN-3/PR-5. Bounded page size for `PostgresConnector.discover_streaming`'s
    # per-axis queries -- a distinct concern from `profile_plan_page_size`
    # above (that pages *profiling* tasks over tables the catalog already
    # knows about; this bounds the *discovery* scan itself, before any table
    # for this run exists in the catalog yet). Kept an order of magnitude
    # smaller than the profiling page size by default because each discovery
    # batch issues roughly ten separate per-axis queries (columns,
    # constraints, views, indexes, partitions, comments, grants) against the
    # batch's tables rather than one -- a 500-table discovery batch is already
    # ~5,000 query executions across a 100K-table run.
    discovery_stream_batch_size: int = Field(default=500, ge=1, le=50_000)
    # PR-2: how many (value, count) pairs `profile_column_values` captures per
    # gated column -- the "top values" half of the policy-approved exception.
    profile_value_top_n: int = Field(default=10, ge=1, le=100)
    # PR-2: default retention window pinned onto a `ColumnValueProfileArtifact`
    # at capture time from the policy that authorized it -- changing this
    # setting later never retroactively extends or shortens an
    # already-captured artifact's `expires_at`.
    profiling_exception_default_retention_days: int = Field(default=30, ge=1, le=3650)
    # PR-2: how many expired value-bearing artifacts the background purge
    # sweep deletes per scheduler iteration, mirroring `scheduler_batch_size`.
    profiling_exception_purge_batch_size: int = Field(default=500, ge=1, le=5_000)
    max_active_runs_per_organization: int = Field(default=100, ge=1, le=10_000)
    scheduler_poll_seconds: int = Field(default=10, ge=1, le=300)
    scheduler_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_max_attempts: int = Field(default=10, ge=1, le=100)
    outbox_max_backoff_seconds: int = Field(default=300, ge=1, le=3600)
    relationship_candidate_scan_max_columns: int = Field(default=100_000, ge=1_000, le=1_000_000)
    cross_source_candidate_max_datasource_pairs: int = Field(default=50, ge=1, le=2_000)
    rename_candidate_scan_max_tables: int = Field(default=200, ge=10, le=5_000)
    rename_candidate_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    object_resolution_scan_max_tables_per_datasource: int = Field(
        default=300, ge=10, le=5_000
    )
    object_resolution_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    relationship_candidate_composite_max_columns: int = Field(default=4, ge=2, le=8)
    relationship_candidate_composite_max_per_table: int = Field(default=25, ge=1, le=500)
    usage_boost_enabled_default: bool = False
    usage_boost_refresh_minutes: int = Field(default=60, ge=5, le=1440)
    usage_boost_window_days: int = Field(default=7, ge=1, le=90)
    usage_boost_batch_size: int = Field(default=200, ge=1, le=5_000)
    usage_boost_max: int = Field(default=30, ge=0, le=100)
    # GL-6: unowned-asset backlog owner routing runs on an aged-backlog cadence, not a
    # real-time one -- its own thresholds (glossary_owner_routing.DEFAULT_ROUTE_AFTER /
    # DEFAULT_ESCALATE_AFTER) are 7 and 14 days, so a sub-daily sweep buys no earlier
    # routing/escalation, only wasted per-tick DB scans across every organization.
    # Default once a day; bounded 5 minutes to 7 days so an operator can tighten or
    # loosen it without a code change but cannot accidentally turn it into a per-tick scan.
    owner_routing_interval_minutes: int = Field(default=1_440, ge=5, le=10_080)
    # KG-7: scheduled Postgres/Neo4j knowledge-graph reconciliation cadence.
    # Reconciliation is read-only against both stores and diffs the projector's
    # own selection criteria against what Neo4j actually holds -- not a
    # real-time check, so a sub-hourly cadence would only add per-tick,
    # per-datasource Postgres+Neo4j reads for no earlier drift detection.
    # Default every 6 hours; bounded 15 minutes to 7 days so an operator can
    # tighten or loosen it without a code change.
    graph_reconciliation_interval_minutes: int = Field(default=360, ge=15, le=10_080)
    # P2-06: generic stale-row reaper. Daily by default (86_400s); bounded 15
    # minutes to 7 days so an operator can tighten or loosen it without a code
    # change, but never turn it into a per-tick scan. `reaper_enabled=False`
    # is the ops kill switch (turns the pass into a no-op without stopping the
    # rest of the scheduler). `reaper_retention_overrides` is a per-rule days
    # override, comma-separated (`rule_name:days,other_rule:days`) --
    # `aida.reaper_service.parse_retention_overrides` drops a malformed entry
    # with a warning rather than raising, so a typo here never takes the
    # scheduler offline.
    reaper_enabled: bool = True
    reaper_sweep_interval_seconds: int = Field(default=86_400, ge=900, le=604_800)
    reaper_retention_overrides: str | None = None
    knowledge_graph_max_nodes: int = Field(default=250, ge=25, le=2_000)
    knowledge_graph_max_edges: int = Field(default=1_000, ge=50, le=10_000)
    knowledge_graph_max_depth: int = Field(default=4, ge=1, le=8)
    lineage_cache_enabled: bool = False
    lineage_cache_ttl_seconds: int = Field(default=30, ge=1, le=3600)
    lineage_projection_max_nodes: int = Field(default=20_000, ge=100, le=100_000)
    lineage_projection_max_edges: int = Field(default=100_000, ge=500, le=500_000)
    lineage_neo4j_read_enabled: bool = False
    # C7 / ADR-0020 amendment (2026-08-30, Group J): process-wide default backend for
    # `aida.graph_store.resolve_graph_store_backend` when an organization has not set
    # its own `GraphStoreOrganizationSetting` row. `postgres` needs no second system and
    # is certified by the port's own conformance suite (`tests/test_graph_store_conformance.py`);
    # `neo4j` is additionally gated by `lineage_neo4j_read_enabled` above (INV-9 -- E5,
    # the projection rebuild drill, has not run) regardless of what this or the
    # per-organization setting says.
    graph_store_backend: Literal["postgres", "neo4j", "disabled"] = "postgres"
    mcp_budget_enabled: bool = False
    mcp_require_workload_identity: bool = True
    mcp_requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    mcp_tool_calls_per_day: int = Field(default=1_000, ge=1, le=1_000_000)
    mcp_context_reads_per_day: int = Field(default=5_000, ge=1, le=1_000_000)
    # Per-consumer rate limits (CX-6): narrower throttle for individual consumers
    mcp_consumer_requests_per_minute: int = Field(default=30, ge=1, le=100_000)
    mcp_consumer_tool_calls_per_day: int = Field(default=200, ge=1, le=1_000_000)
    mcp_consumer_context_reads_per_day: int = Field(default=1_000, ge=1, le=1_000_000)
    # --- Data quality (DQ-6) -------------------------------------------------
    #
    # Off by default so a tenant that has not reviewed the feature keeps today's
    # rolling-previous-profile comparison in `quality_service.evaluate_analysis_run`
    # (VOLUME_CHANGE compares the current row count only to the single most recent
    # prior `TableProfile`). Flipping it on makes `evaluate_analysis_run` also fetch
    # each table's own bounded history of past `TableProfile` row counts and hand it
    # to `data_quality.day_of_week_baseline`, which -- purely, with no DB access of
    # its own -- groups those already-persisted points by weekday and judges the
    # current value against its own day-of-week mean/stdev instead of whatever day
    # happened to run last (see `data_quality.evaluate_quality`'s `seasonality_*`
    # parameters). It falls back to the unchanged rolling-previous comparison,
    # automatically and per-table, whenever fewer than `quality_seasonal_min_samples`
    # same-weekday points exist yet -- so enabling this can only change a VOLUME_CHANGE
    # verdict where there is already enough real history to trust one.
    quality_seasonal_thresholds_enabled: bool = False
    quality_seasonal_min_samples: int = Field(default=3, ge=2, le=52)
    quality_seasonal_zscore_threshold: float = Field(default=3.0, ge=1.0, le=10.0)
    # --- Data quality: month-end seasonality (DQ-6 follow-up) ----------------
    #
    # A second, independent, off-by-default seasonal grouping alongside (not
    # replacing) `quality_seasonal_thresholds_enabled`'s day-of-week baseline above.
    # A recurring month-end close batch lands on a different weekday every month, so
    # the day-of-week grouping alone spreads it across several weekday buckets
    # instead of recognizing it as a pattern. When on, `evaluate_analysis_run` also
    # hands its already-fetched history to `data_quality.day_of_month_baseline`,
    # which -- purely, with no DB access of its own -- groups those points by
    # calendar days-before-month-end (so a 28-day February's last day lines up with
    # a 31-day March's) and judges a reading that falls within the last
    # `quality_seasonal_month_end_window_days` days of its month against that
    # position's own mean/stdev instead. When both this and the day-of-week flag are
    # on, a reading inside the month-end window prefers this baseline (the more
    # specific signal for that day); everything else still falls back to the
    # day-of-week baseline, then to the unchanged rolling-previous comparison,
    # exactly as before this flag existed.
    quality_seasonal_month_end_enabled: bool = False
    quality_seasonal_month_end_window_days: int = Field(default=3, ge=1, le=10)
    # --- Data quality: certification expiry on sustained incidents (DQ-3) ----
    #
    # Off by default: unlike the other DQ-3 coupling points (retrieval
    # demotion, tool gating, answer trust warnings -- all read-time, harmless
    # to enable unconditionally), this one *writes*, flipping an
    # `AssetCertification.status` from "ACTIVE" to "EXPIRED" the moment a
    # table crosses `quality_certification_sustained_threshold` unresolved
    # incidents. Turning this on for the first time in an estate with
    # existing certified-but-currently-incident-affected tables would expire
    # all of them in the very next `evaluate_analysis_run`, not just future
    # ones -- a real, visible governance action that deserves an explicit,
    # reviewed opt-in rather than silently taking effect the moment this
    # code ships (same reasoning `quality_seasonal_thresholds_enabled` above
    # already applies to a read-time-only behavior; this is the write-time
    # case that reasoning was written for).
    quality_certification_expiry_enabled: bool = False
    quality_certification_sustained_threshold: int = Field(default=3, ge=1, le=50)
    # P2-08: manual revoke endpoint + daily "your cert expires in N days" warning
    # job + partial-unique-index backstop on the ACTIVE tuple.
    #
    # `certification_expiry_warn_days` is the horizon the warning job looks
    # ahead by (`now < expires_at < now + warn_days`), and the same value is
    # also (doubled) the idempotency cooldown -- a cert whose warning was
    # already emitted inside `warn_days * 2` does not warn again -- so N=7
    # gives a one-warning-per-cycle "expires next week" ping without spamming
    # the owner. `certification_expiry_warn_interval_seconds` is the scheduler
    # cadence for the pass itself (daily by default, matching the reaper);
    # `certification_revoke_enforce_maker_checker` is the maker-checker guard
    # on the new revoke endpoint (a principal cannot revoke a certification
    # they themselves granted), off-switchable for single-steward
    # deployments where maker-checker would deadlock every revoke.
    certification_expiry_warn_days: int = Field(default=7, ge=1, le=90)
    certification_expiry_warn_interval_seconds: int = Field(
        default=86_400, ge=900, le=604_800
    )
    certification_revoke_enforce_maker_checker: bool = True
    # --- Vector index (ADR-0019) -------------------------------------------
    #
    # `pgvector` is not assumed. A regulated PostgreSQL estate frequently forbids
    # extensions outright, so the default backend is the one that needs none.
    #   postgres_bruteforce -- exact cosine over a policy-narrowed candidate set,
    #                          no extension, no second system
    #   external           -- the bank's own in-network vector service over HTTP
    #   pgvector           -- only selectable where the extension is actually
    #                          installed; refused at startup otherwise (INV-4, INV-9)
    #   disabled           -- semantic retrieval off; lexical only, honestly reported
    vector_index_backend: Literal[
        "disabled", "postgres_bruteforce", "external", "pgvector"
    ] = "postgres_bruteforce"
    vector_index_url: str | None = None
    vector_index_credential_reference: str | None = Field(default=None, max_length=500)
    vector_index_collection: str = Field(default="atlas-metadata", max_length=200)
    vector_index_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Exact cosine is linear in candidates. Measured end to end on PostgreSQL 16 with
    # 200,000 stored 768-dimension embeddings -- fetch, unpack and score, to top-25:
    #     200 candidates ->    45 ms
    #   1,000 candidates ->   100 ms
    #   5,000 candidates ->   427 ms
    #  20,000 candidates -> 1,697 ms
    # So the workable envelope is a lexical/policy pre-filter down to order 1,000, then
    # exact re-ranking. The default cap is set where the curve is still interactive; the
    # cap is a refusal with a reason code, not a truncation, because scoring an
    # arbitrary slice of a larger set returns plausible answers that are wrong.
    vector_bruteforce_candidate_cap: int = Field(default=5_000, ge=100, le=1_000_000)
    # Which service produces embeddings (N5, decided 2026-08-30: OpenAI or Gemini, the
    # same two providers the generation path already supports). `unset` is not a disabled
    # feature but an unmade decision, and it fails closed: `resolve_embedding_provider`
    # refuses rather than falling back to the deterministic hash double, because a
    # "vector similarity" score computed from a SHA-256 digest is noise wearing the name
    # of a signal (INV-4, INV-9).
    embedding_provider: Literal["unset", "openai", "gemini"] = "unset"
    # Resolved through the same path as every other model credential, so an embedding key
    # inherits the same rotation, the same registry and the same production refusal of
    # `env://`. Empty means unconfigured, which is a refusal.
    embedding_credential_reference: str = Field(default="", max_length=500)
    # An embedding is only comparable to embeddings made by the same model. These are
    # pinned so that changing the model invalidates the index rather than silently
    # mixing incomparable vectors -- the failure mode of which is quietly bad search.
    # Left at `unset`, the provider's documented default is used.
    embedding_model_id: str = Field(default="unset", max_length=200)
    embedding_model_version: str = Field(default="unset", max_length=100)
    embedding_dimensions: int = Field(default=768, ge=8, le=8192)
    embedding_chunking_version: int = Field(default=1, ge=1)

    # What to do with a request whose workspace cannot be resolved (ADR-0018 rollout).
    # SHADOW proceeds and logs; DENY refuses. It defaults to SHADOW because the API
    # contracts predate ADR-0018 and almost no caller names a workspace yet -- defaulting
    # to DENY would take the platform down on the day the gate was wired, which is how an
    # authorization rollout gets reverted instead of finished. The
    # `authorization.workspace_unresolved` log line counts the callers still to migrate;
    # when it reaches zero for an environment, this flips there. That flip is the actual
    # completion of the rollout, and until it happens the platform should say so (INV-9).
    unresolved_workspace_posture: Literal["SHADOW", "DENY"] = "SHADOW"

    # PG-5: which product edition this deployment is licensed for
    # (`Docs/00-product/07-packaging-and-editions.md` §3's capability matrix).
    # Deployment-wide, not per-`Organization` -- `07-packaging-and-editions.md`
    # §2 names self-hosted (BYOK), one customer per running deployment, as the
    # primary and only "target for v1" model, and multi-tenant SaaS as "not
    # planned"; a per-deployment license setting is therefore the accurate
    # description of where an edition actually lives today, not a workaround.
    # `aida.edition_entitlements.evaluate_entitlement` is the pure evaluator
    # this feeds; `atlas.platform` must not import from `aida` (see the
    # import-linter contract this module's docstring already documents), so
    # the literal values are repeated here rather than importing `aida`'s
    # `Edition` alias -- the two are kept in sync by
    # `tests/test_edition_entitlements.py`. Defaults to the ceiling
    # (`REGULATED`) so an unconfigured deployment's existing behaviour is
    # unchanged by this setting's mere existence (PK-2, `07-packaging-and-
    # editions.md` §6, is still an open product decision on whether a
    # `FOUNDATION` edition is even offered; defaulting down would make this
    # setting's addition alone start denying capability nobody asked to gate).
    edition: Literal["FOUNDATION", "ENTERPRISE", "REGULATED"] = "REGULATED"

    entitlement_provider: Literal["outbox", "webhook"] = "outbox"
    entitlement_webhook_url: str | None = None
    entitlement_webhook_token: SecretStr | None = None
    entitlement_timeout_seconds: int = Field(default=10, ge=1, le=60)

    # --- GROUP C (DQ-1): ITSM webhook emitter for routed quality incidents.
    # Off by default (`dq_itsm_webhook_enabled=False`) so an unconfigured
    # deployment's behaviour is unchanged -- a quality incident is still
    # routed and persisted (`NotificationEventRecord`) even with the emitter
    # disabled, it just stays in status PENDING rather than attempting an
    # outbound call. The actual ITSM system (ServiceNow/Jira/...) is an infra
    # concern; this is a generic, configurable webhook target that receives
    # `notification_routing.format_itsm_payload`'s JSON body, mirroring
    # `entitlement_webhook_url`'s shape.
    dq_itsm_webhook_enabled: bool = False
    dq_itsm_webhook_url: str | None = None
    dq_itsm_webhook_token: SecretStr | None = None
    dq_itsm_webhook_timeout_seconds: int = Field(default=10, ge=1, le=60)
    agent_retrieval_limit: int = Field(default=25, ge=1, le=100)
    agent_retrieval_scan_limit: int = Field(default=5_000, ge=100, le=100_000)
    agent_tool_match_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    # AG-7: query-memory similarity/adaptation. Off by default so a tenant that has
    # not reviewed the feature keeps today's MODEL_GATEWAY-only behaviour; flipping
    # it on only changes what grounding a MODEL_GENERATION-strategy run's prompt
    # carries (a matched-and-version-checked prior query's redacted SQL shape) --
    # the generated SQL still reaches the identical `query_gateway.execute` guard
    # call every other path uses (see `query_memory.py`, `agent_orchestrator.py`).
    agent_query_memory_enabled: bool = False
    agent_query_memory_min_similarity: float = Field(default=0.6, ge=0.0, le=1.0)
    agent_query_memory_scan_limit: int = Field(default=200, ge=1, le=5_000)
    model_generation_enabled: bool = False
    model_route: str | None = Field(default=None, min_length=3, max_length=255)
    # 2026-09-03: comma-separated list of additional model_route_keys to try in
    # PREFERENCE ORDER when the primary `model_route` fails with a transient
    # provider error (HTTP 429, 502, 503, 504) after its own in-route retries
    # (`model_provider_max_attempts`) are exhausted. Each entry must itself be
    # an APPROVED `ModelRouteConfiguration` for the organization -- unapproved
    # entries are silently skipped rather than failing the whole call, so
    # revoking a route via governance is a no-op for callers that had it as a
    # fallback. Non-retryable errors (401/403 authentication, 400 malformed
    # request) do NOT trigger fallback: they indicate a broken route, not a
    # busy provider, so switching would just move the failure. Empty/None
    # means no fallback (behavior identical to before this setting existed).
    # Governance-compatible: fallback only ever picks between routes that are
    # ALREADY APPROVED, never a route it discovers itself.
    model_route_fallbacks: str | None = Field(default=None, max_length=1024)

    @property
    def model_route_fallback_keys(self) -> list[str]:
        """Ordered, deduplicated list of fallback route_keys.

        The primary `model_route` is NOT included -- callers iterate primary
        first, then this list. Empty/None settings collapse to []; whitespace
        is trimmed; empty entries between commas are dropped. The primary is
        also filtered out so a misconfiguration where the same key appears
        in both settings doesn't cost a doubled retry on outage.
        """
        if not self.model_route_fallbacks:
            return []
        seen: set[str] = set()
        keys: list[str] = []
        for raw in self.model_route_fallbacks.split(","):
            key = raw.strip()
            if not key or key in seen:
                continue
            if self.model_route and key == self.model_route:
                continue
            seen.add(key)
            keys.append(key)
        return keys
    model_timeout_seconds: int = Field(default=30, ge=1, le=300)
    model_max_input_tokens: int = Field(default=8_000, ge=100, le=1_000_000)
    model_max_output_tokens: int = Field(default=2_000, ge=100, le=100_000)
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # MG-3: private routing. A `ModelRouteConfiguration.endpoint_alias` that appears
    # as a key here resolves to this base URL instead of the public
    # openai_base_url/gemini_base_url default -- e.g. an Azure OpenAI private
    # endpoint or a PrivateLink-fronted Gemini proxy reachable only from inside the
    # bank's network. An alias with no entry here keeps using the public default,
    # so every route approved before this setting existed is unaffected.
    model_endpoint_urls: dict[str, str] = Field(default_factory=dict)
    model_provider_max_attempts: int = Field(default=3, ge=1, le=5)
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    allow_development_sql_override: bool = True
    audit_hmac_key: str = "development-only-change-me"
    # QG-5: which signer produces the audit HMAC evidence in query_gateway.py.
    # "local" holds `audit_hmac_key` in process config -- the pre-QG-5 behaviour,
    # kept only as a development fallback and forbidden in production below, the
    # same shape as `credential_provider`'s "env" refusal. "vault_transit" calls
    # out to HashiCorp Vault's Transit secrets engine for every sign/verify; the
    # raw key never enters this process (see aida.signing).
    hmac_signing_provider: Literal["local", "vault_transit"] = "local"
    hmac_signing_vault_url: str | None = Field(default=None, max_length=500)
    hmac_signing_vault_key_name: str = Field(default="audit-hmac", max_length=200)
    # A reference resolved through the same `SecretResolver` path (and the same
    # `credential_provider`) as every other credential in this codebase -- the
    # Vault token authenticates the *request*, not the HMAC key itself.
    hmac_signing_vault_token_reference: str = Field(default="", max_length=500)
    hmac_signing_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # QG-6: reversible, format-preserving tokenization for sensitive query-gateway
    # output columns configured for TOKENIZE rather than plain redaction. Same
    # provider-neutral, config-selected, resolved-fresh-per-call shape as
    # `hmac_signing_provider` above (see `aida.tokenization`); "local" holds
    # `tokenization_key` in process config and is forbidden in production below,
    # "vault_transform" calls out to HashiCorp Vault's Transform secrets engine for
    # every tokenize/detokenize call, and the raw key never enters this process.
    tokenization_key: str = "development-only-change-me"
    tokenization_provider: Literal["local", "vault_transform"] = "local"
    tokenization_vault_url: str | None = Field(default=None, max_length=500)
    tokenization_vault_role_name: str = Field(default="pii-tokens", max_length=200)
    # Resolved through the same `SecretResolver` path (and the same
    # `credential_provider`) as every other credential in this codebase -- the
    # Vault token authenticates the *request*, not the tokenization key itself.
    tokenization_vault_token_reference: str = Field(default="", max_length=500)
    tokenization_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # --- OB-1: OpenTelemetry tracing/metrics (aida.observability) -----------
    # `console` is the zero-dependency default -- ConsoleSpanExporter and
    # ConsoleMetricExporter ship inside opentelemetry-sdk (already a pinned
    # dependency), so tracing/metrics are genuinely active from process
    # start in every environment without requiring a live collector or the
    # separate opentelemetry-exporter-otlp-proto-grpc package. Point
    # `otel_exporter` at "otlp" (and install that exporter package) to ship
    # to a real collector in production.
    otel_tracing_enabled: bool = True
    otel_metrics_enabled: bool = True
    otel_exporter: Literal["console", "otlp"] = "console"
    otel_endpoint: str = "http://localhost:4317"
    otel_insecure: bool = True
    otel_metrics_export_interval_millis: int = Field(default=60_000, ge=1_000, le=600_000)

    # --- OB-2: SIEM routing (aida.siem_routing) ------------------------------
    # `route_to_siem` formats and logs a structured event (see its docstring)
    # rather than opening a network connection itself, so enabling it by
    # default carries no network risk -- the existing structlog pipeline is
    # its transport to a log-shipping SOC integration. Point `siem_endpoint`
    # at a real webhook/syslog collector to layer an actual transport on top.
    siem_enabled: bool = True
    siem_transport: Literal["syslog", "webhook"] = "webhook"
    siem_endpoint: str = "internal://security-log-pipeline"
    siem_include_details: bool = True

    # --- OB-3: WORM audit archive (aida.worm_archive) ------------------------
    audit_archive_enabled: bool = True
    audit_archive_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    audit_archive_batch_size: int = Field(default=1_000, ge=1, le=10_000)
    audit_archive_retention_days: int = Field(default=2555, ge=1, le=10_950)
    audit_archive_storage_backend: Literal["s3", "gcs", "azure_blob"] = "s3"
    audit_archive_bucket_name: str = "audit-archive"
    audit_archive_legal_hold_enabled: bool = False
    audit_archive_classification: Literal[
        "PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"
    ] = "CONFIDENTIAL"

    @property
    def max_query_estimate_cost(self) -> float:
        return self.max_postgres_plan_cost

    @property
    def max_query_estimate_bytes(self) -> float:
        return float(self.max_bigquery_dry_run_bytes)

    @classmethod
    def _known_env_names(cls) -> set[str]:
        """Every environment-variable name pydantic-settings will actually bind
        to a field on this model, upper-cased for case-insensitive comparison.
        `AIDA_`-prefixed for ordinary fields; unprefixed for the couple of
        fields (the model-provider API keys) that use an explicit
        `validation_alias` to match an external SDK's own env var name.
        """
        names: set[str] = set()
        for field_name, field in cls.model_fields.items():
            alias = field.validation_alias
            if isinstance(alias, str):
                names.add(alias.upper())
            else:
                names.add(f"AIDA_{field_name}".upper())
        return names

    @model_validator(mode="after")
    def reject_unrecognized_aida_env_vars(self) -> "Settings":
        # C1 (2026-08-30 audit): pydantic-settings' env source silently drops any
        # env var whose name doesn't match a known field *before* validation ever
        # sees it, so `extra="forbid"` above -- correct as far as it goes -- can't
        # by itself catch a misspelled *name* like `AIDA_ENVIRONMNET`. This walks
        # the real process environment instead.
        #
        # A blanket "any unrecognized AIDA_*-prefixed var is an error" would break
        # a second, unrelated, and entirely legitimate use of the same prefix:
        # `credential_reference="env://AIDA_SOME_DATASOURCE_DSN"` (aida.secrets)
        # resolves arbitrary operator-chosen env var names this model has never
        # heard of and never will -- see AIDA_SAMPLE_SOURCE_DSN in
        # .env.example/compose.yaml. So this only flags a var whose name is a
        # *close* (fuzzy, not exact) match of a real field's env var name: close
        # enough to be almost certainly a typo of a known setting, not an
        # intentionally-named credential reference.
        known = self._known_env_names()
        suspects: list[str] = []
        for raw_name in os.environ:
            name = raw_name.upper()
            if not name.startswith("AIDA_") or name in known:
                continue
            match = difflib.get_close_matches(name, known, n=1, cutoff=0.84)
            if match:
                suspects.append(f"{raw_name} (did you mean {match[0]}?)")
        if suspects:
            raise ValueError(
                "unrecognized AIDA_* environment variable name(s), likely typo'd: "
                + "; ".join(sorted(suspects))
            )
        return self

    @model_validator(mode="after")
    def reject_implicit_environment_outside_tests(self) -> "Settings":
        # C1 (2026-08-30 audit): `environment` defaulting to "development" is
        # exactly how a missing or mistyped `AIDA_ENVIRONMENT` used to boot every
        # production guard disabled -- no error, no log line. Requiring it
        # explicitly everywhere except under pytest closes that hole. The shipped
        # bootstrap paths (.env.example, compose.yaml) already set it explicitly,
        # so this is a no-op for them; only a truly unconfigured process (or a
        # unit test that doesn't care about `environment`) is affected, and the
        # latter is exempted so the existing test suite keeps constructing bare
        # `Settings(...)` without having to name it.
        if "environment" not in self.model_fields_set and not _running_under_pytest():
            raise ValueError(
                "AIDA_ENVIRONMENT must be set explicitly; it no longer defaults "
                "silently to 'development' outside of tests"
            )
        return self

    @model_validator(mode="after")
    def reject_insecure_production_configuration(self) -> "Settings":
        if self.environment == "production" and self.identity_provider == "development":
            raise ValueError("development identity provider is forbidden in production")
        if self.identity_provider == "oidc":
            if not self.oidc_issuer or not self.oidc_audience:
                raise ValueError("OIDC issuer and audience are required")
            if not self.oidc_jwks_url and not self.oidc_jwks_json:
                raise ValueError("OIDC JWKS URL or pinned JWKS JSON is required")
            if (
                self.environment == "production"
                and self.oidc_jwks_url
                and not self.oidc_jwks_url.startswith("https://")
            ):
                raise ValueError("production OIDC JWKS URL must use HTTPS")
        if self.environment == "production" and self.credential_provider == "env":
            raise ValueError("environment secret provider is forbidden in production")
        if self.default_query_row_limit > self.hard_query_row_limit:
            raise ValueError("default query row limit cannot exceed the hard limit")
        if self.environment == "production" and self.allow_development_sql_override:
            raise ValueError("development SQL override is forbidden in production")
        if self.model_generation_enabled and not self.model_route:
            raise ValueError("model generation requires an explicit approved route")
        if self.environment == "production" and (
            not self.openai_base_url.startswith("https://")
            or not self.gemini_base_url.startswith("https://")
        ):
            raise ValueError("production model provider URLs must use HTTPS")
        if self.environment == "production":
            insecure_endpoint_aliases = [
                alias
                for alias, url in self.model_endpoint_urls.items()
                if not url.startswith("https://")
            ]
            if insecure_endpoint_aliases:
                raise ValueError(
                    "production private model endpoint URLs must use HTTPS: "
                    f"{sorted(insecure_endpoint_aliases)}"
                )
        if self.environment == "production" and len(self.audit_hmac_key) < 32:
            raise ValueError("a production audit HMAC key must contain at least 32 characters")
        if self.environment == "production" and self.hmac_signing_provider == "local":
            raise ValueError(
                "an application-managed local HMAC signer is forbidden in production; "
                "configure a KMS-backed hmac_signing_provider (QG-5)"
            )
        if self.environment == "production" and len(self.tokenization_key) < 32:
            raise ValueError("a production tokenization key must contain at least 32 characters")
        if self.environment == "production" and self.tokenization_provider == "local":
            raise ValueError(
                "an application-managed local tokenization provider is forbidden in "
                "production; configure a KMS-backed tokenization_provider (QG-6)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=".env")
