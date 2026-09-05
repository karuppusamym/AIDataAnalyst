from pathlib import Path

import pytest
from pydantic import ValidationError

from aida.config import Settings
from atlas.platform import config as config_module


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


# --- AU-3 / C1: config fails closed on unknown or missing AIDA_* variables ------


def test_misspelled_env_var_name_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit's exact repro: `AIDA_ENVIRONMNET` (missing the 'E') must be as
    loud a failure as a misspelled *value* -- not silently discarded, leaving
    every production guard on its default 'development' posture."""
    monkeypatch.setenv("AIDA_ENVIRONMNET", "production")
    with pytest.raises(ValidationError, match="unrecognized AIDA_\\* environment variable"):
        Settings(_env_file=None)


def test_other_near_miss_env_var_names_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDA_LOG_LEVL", "INFO")
    with pytest.raises(ValidationError, match="did you mean AIDA_LOG_LEVEL"):
        Settings(_env_file=None)


def test_unrelated_aida_prefixed_credential_reference_vars_are_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`credential_reference="env://AIDA_SOME_DSN"` (aida.secrets) is a deliberate,
    open-ended use of the same `AIDA_` prefix for operator-named credential
    lookups that this model has never modeled and never will -- see
    AIDA_SAMPLE_SOURCE_DSN in .env.example/compose.yaml. Those must keep working
    even though they are, strictly, "unrecognized AIDA_* env vars": only a
    *close match* of a real setting name is a likely typo worth failing on."""
    monkeypatch.setenv("AIDA_SAMPLE_SOURCE_DSN", "postgresql://source@localhost/bank_demo")
    monkeypatch.setenv("AIDA_SAMPLE_ORACLE_SOURCE_DSN", "oracle://source@localhost/FREEPDB1")
    monkeypatch.setenv("AIDA_SAMPLE_MSSQL_SOURCE_DSN", "mssql://source@localhost/bank_demo_mssql")
    monkeypatch.setenv("AIDA_TEST_SECRET", "shh")
    Settings(_env_file=None)  # must not raise


def test_environment_must_be_explicit_outside_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside of tests, leaving `AIDA_ENVIRONMENT` unset must fail loudly rather
    than silently booting 'development' posture -- the other half of C1: a
    missing name is exactly as dangerous as a misspelled one."""
    monkeypatch.setattr(config_module, "_running_under_pytest", lambda: False)
    with pytest.raises(ValidationError, match="AIDA_ENVIRONMENT must be set explicitly"):
        Settings(_env_file=None)


def test_environment_explicitly_set_passes_outside_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_running_under_pytest", lambda: False)
    Settings(environment="development", _env_file=None)  # must not raise


def test_settings_construct_from_env_example_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped local-dev bootstrap (.env.example) already sets AIDA_ENVIRONMENT
    explicitly and never misnames a real field, so the stricter config must still
    boot cleanly from it -- the whole point is failing on typos, not on legitimate
    configuration."""
    env_example = Path(__file__).resolve().parent.parent / ".env.example"
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        monkeypatch.setenv(key.strip(), value.strip())

    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.identity_provider == "development"
