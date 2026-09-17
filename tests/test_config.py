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


def test_production_refuses_unattended_reviewer_approvals() -> None:
    """R11-C3: measured unsafe with no fix, so off is a decision that holds
    where it matters rather than a default someone can flip."""
    with pytest.raises(ValidationError, match="unattended reviewer-agent approvals"):
        Settings(
            environment="production",
            identity_provider="oidc",
            oidc_issuer="https://identity.bank.example",
            oidc_audience="atlas",
            oidc_jwks_json='{"keys":[]}',
            credential_provider="vault",
            allow_development_sql_override=False,
            reviewer_agent_enabled=True,
            _env_file=None,
        )


def test_development_can_still_run_the_reviewer_agent_to_measure_it() -> None:
    assert Settings(reviewer_agent_enabled=True, _env_file=None).reviewer_agent_enabled


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
    monkeypatch.setenv("AIDA_SAMPLE_RISK_SOURCE_DSN", "postgresql://source@localhost/risk_demo")
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


def test_env_example_boots_as_an_actual_dotenv_file(tmp_path: Path) -> None:
    """The documented bootstrap is `cp .env.example .env`, so the shipped
    template has to work when it is *read as a dotenv file* -- not only when its
    keys are already exported.

    This is the gap that let the defect ship. The test above loads `.env.example`
    into the environment and passes `_env_file=None`, which exercises the env
    source; the env source silently drops a key that matches no field. The
    dotenv source does not drop it, it hands it to the model, and `extra="forbid"`
    refused it -- so `.env.example`'s own `AIDA_SAMPLE_SOURCE_DSN` line (a
    `credential_reference="env://..."` target, deliberately not a Settings field)
    made `Settings(_env_file=".env")` raise `extra_forbidden` for every host-side
    script, while the identical name exported into the environment worked.
    """
    env_example = Path(__file__).resolve().parent.parent / ".env.example"
    target = tmp_path / ".env"
    target.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")

    settings = Settings(_env_file=str(target))

    assert settings.environment == "development"
    assert settings.identity_provider == "development"


def test_a_misspelled_setting_in_a_dotenv_file_is_still_refused(tmp_path: Path) -> None:
    """Tolerating a credential reference must not tolerate a typo.

    The fix drops only an `AIDA_*` key that is *not* a close match of a real
    field, on the same fuzzy rule `reject_unrecognized_aida_env_vars` uses for
    the process environment. A near-miss of a real setting is the case
    `extra="forbid"` exists for, and it stays refused.
    """
    target = tmp_path / ".env"
    target.write_text(
        "AIDA_ENVIRONMENT=development\nAIDA_ENVIRONMNET=development\n", encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        Settings(_env_file=str(target))
