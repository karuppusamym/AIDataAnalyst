from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIDA_",
        env_file_encoding="utf-8",
        extra="ignore",
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
    oidc_role_mappings: dict[str, list[str]] = Field(default_factory=dict)
    oidc_jwks_cache_seconds: int = Field(default=300, ge=30, le=86_400)
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    credential_provider: Literal["env", "vault", "cyberark", "aws-sm", "azure-kv", "gcp-sm"] = "env"
    secret_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)
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
    profile_max_tables_per_run: int = Field(default=5_000, ge=1, le=100_000)
    max_active_runs_per_organization: int = Field(default=100, ge=1, le=10_000)
    scheduler_poll_seconds: int = Field(default=10, ge=1, le=300)
    scheduler_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_max_attempts: int = Field(default=10, ge=1, le=100)
    outbox_max_backoff_seconds: int = Field(default=300, ge=1, le=3600)
    relationship_candidate_scan_max_columns: int = Field(default=100_000, ge=1_000, le=1_000_000)
    knowledge_graph_max_nodes: int = Field(default=250, ge=25, le=2_000)
    knowledge_graph_max_edges: int = Field(default=1_000, ge=50, le=10_000)
    knowledge_graph_max_depth: int = Field(default=4, ge=1, le=8)
    lineage_cache_enabled: bool = False
    lineage_cache_ttl_seconds: int = Field(default=30, ge=1, le=3600)
    lineage_projection_max_nodes: int = Field(default=20_000, ge=100, le=100_000)
    lineage_projection_max_edges: int = Field(default=100_000, ge=500, le=500_000)
    lineage_neo4j_read_enabled: bool = False
    mcp_budget_enabled: bool = False
    mcp_requests_per_minute: int = Field(default=120, ge=1, le=100_000)
    mcp_tool_calls_per_day: int = Field(default=1_000, ge=1, le=1_000_000)
    mcp_context_reads_per_day: int = Field(default=5_000, ge=1, le=1_000_000)
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

    @property
    def max_query_estimate_cost(self) -> float:
        return self.max_postgres_plan_cost

    @property
    def max_query_estimate_bytes(self) -> float:
        return float(self.max_bigquery_dry_run_bytes)

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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=".env")
