"""QG-6: dynamic masking / tokenization integration, wired into the query gateway.

`tests/test_tokenization.py` covers the `TokenizationProvider` protocol and its
two implementations in isolation. This module covers the wiring: a column an
enabled `ColumnTokenizationPolicy` covers comes back tokenized rather than
flatly redacted, a column with no such policy keeps today's ``"***MASKED***"``
behaviour unchanged, tokenization and redaction never double up on the same
column, and a query that needs to tokenize but has no usable provider
configured fails the whole query closed rather than silently falling back to
redaction.
"""

from uuid import uuid4

import pytest

from aida.config import Settings
from aida.models import DataSource
from aida.query_gateway import QueryExecutionGateway, QueryRejected
from tests.support.doubles import CatalogSession, FakeSqlExecutor, security_context


def _gateway_datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="tokenization-source",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://sentinel",
        status="ACTIVE",
    )


def _wire_fake_source(
    monkeypatch: pytest.MonkeyPatch, executor: FakeSqlExecutor
) -> None:
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type("_Resolver", (), {"resolve": staticmethod(lambda ref: "dsn://x")})(),
    )


async def test_a_tokenization_configured_column_comes_back_tokenized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasource = _gateway_datasource()
    sql = "SELECT customer_id, card_number FROM analytics.customers"
    executor = FakeSqlExecutor(({"customer_id": "C-1", "card_number": "4111111111111111"},))
    _wire_fake_source(monkeypatch, executor)

    session = CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[
            ("analytics_db", "analytics", "customers", "customer_id"),
            ("analytics_db", "analytics", "customers", "card_number"),
        ],
        sensitive_columns=["card_number"],
        tokenized_columns=[("NUMERIC", "card_number")],
    )
    gateway = QueryExecutionGateway(Settings(_env_file=None, tokenization_key="k" * 32))

    result = await gateway.execute(
        session,
        datasource=datasource,
        context=security_context(organization_id=datasource.organization_id),
        correlation_id="corr-tokenize",
        sql=sql,
        requested_limit=10,
        semantic_version=None,
    )

    assert result.execution.status == "COMPLETED"
    assert result.tokenized_columns == ("card_number",)
    # Tokenized, not fully redacted: not the flat mask, but also not the raw value.
    assert result.rows[0]["card_number"] not in ("***MASKED***", "4111111111111111")
    assert len(result.rows[0]["card_number"]) == len("4111111111111111")
    # A column the policy does not cover keeps today's behaviour: untouched.
    assert result.rows[0]["customer_id"] == "C-1"
    assert "card_number" not in result.masked_columns


async def test_tokenization_round_trips_through_the_provider_used_by_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact value the gateway's tokenize call produced is recoverable
    through the same provider a detokenize call would resolve -- the property
    that makes tokenization meaningfully different from redaction."""
    from aida.tokenization import resolve_tokenization_provider

    datasource = _gateway_datasource()
    sql = "SELECT ssn FROM analytics.customers"
    original = "078051120"
    executor = FakeSqlExecutor(({"ssn": original},))
    _wire_fake_source(monkeypatch, executor)

    session = CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "ssn")],
        sensitive_columns=["ssn"],
        tokenized_columns=[("NUMERIC", "ssn")],
    )
    settings = Settings(_env_file=None, tokenization_key="k" * 32)
    gateway = QueryExecutionGateway(settings)

    result = await gateway.execute(
        session,
        datasource=datasource,
        context=security_context(organization_id=datasource.organization_id),
        correlation_id="corr-roundtrip",
        sql=sql,
        requested_limit=10,
        semantic_version=None,
    )

    token = result.rows[0]["ssn"]
    provider = resolve_tokenization_provider(settings)
    assert await provider.detokenize(token) == original


async def test_a_sensitive_column_without_a_tokenization_policy_stays_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No behaviour change for a sensitive column nobody opted into
    tokenization for -- the conservative default (full redaction) is unchanged."""
    datasource = _gateway_datasource()
    sql = "SELECT email FROM analytics.customers"
    executor = FakeSqlExecutor(({"email": "person@example.com"},))
    _wire_fake_source(monkeypatch, executor)

    session = CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "email")],
        sensitive_columns=["email"],
        # No tokenized_columns configured.
    )
    gateway = QueryExecutionGateway(Settings(_env_file=None, tokenization_key="k" * 32))

    result = await gateway.execute(
        session,
        datasource=datasource,
        context=security_context(organization_id=datasource.organization_id),
        correlation_id="corr-redact",
        sql=sql,
        requested_limit=10,
        semantic_version=None,
    )

    assert result.rows[0]["email"] == "***MASKED***"
    assert result.masked_columns == ("email",)
    assert result.tokenized_columns == ()


async def test_a_query_needing_tokenization_fails_closed_without_a_usable_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A column configured for tokenization must never silently fall back to
    redaction (or to the raw value) just because the configured provider could
    not be resolved -- the whole query is rejected instead (QG-6 mirrors QG-5's
    `SigningUnavailable` fail-closed shape)."""
    datasource = _gateway_datasource()
    sql = "SELECT card_number FROM analytics.customers"
    executor = FakeSqlExecutor(({"card_number": "4111111111111111"},))
    _wire_fake_source(monkeypatch, executor)

    session = CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "card_number")],
        sensitive_columns=["card_number"],
        tokenized_columns=[("NUMERIC", "card_number")],
    )
    # vault_transform selected but not configured -- resolve_tokenization_provider
    # raises TokenizationUnavailable, which is a TokenizationError.
    settings = Settings(_env_file=None, tokenization_provider="vault_transform")
    gateway = QueryExecutionGateway(settings)

    with pytest.raises(QueryRejected, match="TOKENIZATION_PROVIDER_UNAVAILABLE"):
        await gateway.execute(
            session,
            datasource=datasource,
            context=security_context(organization_id=datasource.organization_id),
            correlation_id="corr-fail-closed",
            sql=sql,
            requested_limit=10,
            semantic_version=None,
        )
    executions = [row for row in session.added if type(row).__name__ == "QueryExecution"]
    assert executions and executions[0].status == "REJECTED"
