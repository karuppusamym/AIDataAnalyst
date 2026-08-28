import pytest
from pydantic import ValidationError

from aida.config import Settings


def test_production_rejects_development_identity() -> None:
    with pytest.raises(ValidationError, match="development identity provider is forbidden"):
        Settings(environment="production", identity_provider="development", _env_file=None)


def test_query_default_cannot_exceed_hard_limit() -> None:
    with pytest.raises(ValidationError, match="default query row limit"):
        Settings(default_query_row_limit=100, hard_query_row_limit=10, _env_file=None)


def test_production_rejects_development_sql_override() -> None:
    with pytest.raises(ValidationError, match="development SQL override is forbidden"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys":[]}',
            credential_provider="vault",
            allow_development_sql_override=True,
            _env_file=None,
        )


def test_production_requires_strong_audit_hmac_key() -> None:
    with pytest.raises(ValidationError, match="production audit HMAC key"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys":[]}',
            credential_provider="vault",
            allow_development_sql_override=False,
            audit_hmac_key="too-short",
            _env_file=None,
        )


def test_model_generation_requires_explicit_route() -> None:
    with pytest.raises(
        ValidationError, match="model generation requires an explicit approved route"
    ):
        Settings(model_generation_enabled=True, model_route=None, _env_file=None)


def test_production_rejects_environment_secret_provider() -> None:
    with pytest.raises(ValidationError, match="environment secret provider is forbidden"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys":[]}',
            credential_provider="env",
            allow_development_sql_override=False,
            audit_hmac_key="a-production-key-with-at-least-32-characters",
            _env_file=None,
        )


def test_production_rejects_insecure_model_provider_url() -> None:
    with pytest.raises(ValidationError, match="model provider URLs must use HTTPS"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas-api",
            oidc_jwks_json='{"keys": []}',
            credential_provider="vault",
            allow_development_sql_override=False,
            audit_hmac_key="a" * 32,
            openai_base_url="http://model-proxy.internal",
            _env_file=None,
        )
