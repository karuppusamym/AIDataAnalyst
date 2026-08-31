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
    metadata_batch_max_chunks: int = Field(default=1_000, ge=1, le=10_000)
    metadata_batch_max_tables: int = Field(default=1_000_000, ge=1_000, le=10_000_000)
    metadata_batch_max_columns: int = Field(default=5_000_000, ge=10_000, le=50_000_000)
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
    knowledge_graph_max_nodes: int = Field(default=250, ge=25, le=2_000)
    knowledge_graph_max_edges: int = Field(default=1_000, ge=50, le=10_000)
    knowledge_graph_max_depth: int = Field(default=4, ge=1, le=8)
    lineage_cache_enabled: bool = False
    lineage_cache_ttl_seconds: int = Field(default=30, ge=1, le=3600)
    lineage_projection_max_nodes: int = Field(default=20_000, ge=100, le=100_000)
    lineage_projection_max_edges: int = Field(default=100_000, ge=500, le=500_000)
    lineage_neo4j_read_enabled: bool = False
    mcp_budget_enabled: bool = False
    mcp_require_workload_identity: bool = True
    mcp_requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    mcp_tool_calls_per_day: int = Field(default=1_000, ge=1, le=1_000_000)
    mcp_context_reads_per_day: int = Field(default=5_000, ge=1, le=1_000_000)
    # Per-consumer rate limits (CX-6): narrower throttle for individual consumers
    mcp_consumer_requests_per_minute: int = Field(default=30, ge=1, le=100_000)
    mcp_consumer_tool_calls_per_day: int = Field(default=200, ge=1, le=1_000_000)
    mcp_consumer_context_reads_per_day: int = Field(default=1_000, ge=1, le=1_000_000)
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

    entitlement_provider: Literal["outbox", "webhook"] = "outbox"
    entitlement_webhook_url: str | None = None
    entitlement_webhook_token: SecretStr | None = None
    entitlement_timeout_seconds: int = Field(default=10, ge=1, le=60)
    agent_retrieval_limit: int = Field(default=25, ge=1, le=100)
    agent_retrieval_scan_limit: int = Field(default=5_000, ge=100, le=100_000)
    agent_tool_match_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    model_generation_enabled: bool = False
    model_route: str | None = Field(default=None, min_length=3, max_length=255)
    model_timeout_seconds: int = Field(default=30, ge=1, le=300)
    model_max_input_tokens: int = Field(default=8_000, ge=100, le=1_000_000)
    model_max_output_tokens: int = Field(default=2_000, ge=100, le=100_000)
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
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
