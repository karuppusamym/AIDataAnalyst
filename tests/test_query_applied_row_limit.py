"""F20: an executed query reports the row bound it actually ran under.

The review's F20 work put returned rows on screen and then had to *infer*
whether they were truncated, because `QueryExecutionResponse` carried no
applied row limit: `ui-next/src/components/QueryResultTable.tsx` reads the
first `LIMIT n` back out of `normalized_sql` with a regex and compares it to
`row_count`. The gateway has always known the real number -- `SqlGuard`
rewrites the statement with it -- and recorded it in `query.validate.gateway`
audit details, where no caller can see it.

This module covers the two claims the new fields make:

1. `applied_row_limit` is the number the guard actually applied, for each of
   the three bounds that can produce it; and
2. `row_limit_source` says *which* bound produced it, so "the platform capped
   your result" and "your own statement said LIMIT 3" are not the same
   answer -- the distinction the single number cannot carry.

`test_a_subquery_limit_defeats_the_regex_the_field_replaces` is the
regression: it constructs the case where reading the limit back off the
generated SQL is not merely fragile but *wrong*, and pins that the field
disagrees with the guess.

Both API routes are covered by covering `query_execution_response` once --
`aida.api` and `aida.tool_api` share that single projection by construction
(see `aida/query_execution_view.py`), which is what makes one test here
enough for both.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aida.config import Settings
from aida.models import DataSource, QueryExecution
from aida.query_execution_view import query_execution_response
from aida.query_gateway import GatewayResult, QueryExecutionGateway, row_limit_source
from tests.support.doubles import CatalogSession, FakeSqlExecutor, security_context


def _datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="row-limit-source",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://sentinel",
        status="ACTIVE",
    )


def _wire_fake_source(monkeypatch: pytest.MonkeyPatch, executor: FakeSqlExecutor) -> None:
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type("_Resolver", (), {"resolve": staticmethod(lambda ref: "dsn://x")})(),
    )


def _catalog() -> CatalogSession:
    return CatalogSession(
        tables=[("analytics_db", "analytics", "customers")],
        columns=[("analytics_db", "analytics", "customers", "id")],
        sensitive_columns=[],
    )


async def _execute(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sql: str,
    requested_limit: int | None,
    settings: Settings | None = None,
) -> GatewayResult:
    datasource = _datasource()
    executor = FakeSqlExecutor(({"id": "C-1"},))
    _wire_fake_source(monkeypatch, executor)
    gateway = QueryExecutionGateway(settings or Settings(_env_file=None))
    return await gateway.execute(
        _catalog(),
        datasource=datasource,
        context=security_context(organization_id=datasource.organization_id),
        correlation_id="corr-row-limit",
        sql=sql,
        requested_limit=requested_limit,
        semantic_version=None,
    )


# ---------------------------------------------------------------------------
# 1. the three bounds that can produce the applied limit
# ---------------------------------------------------------------------------


async def test_an_uncapped_statement_reports_the_gateway_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `LIMIT` in the statement and no `max_rows`: the configured default
    is the bound, and it is reported as the platform's, not the caller's."""
    settings = Settings(_env_file=None)
    result = await _execute(
        monkeypatch, sql="SELECT id FROM analytics.customers", requested_limit=None
    )

    assert result.execution.status == "COMPLETED"
    assert result.applied_row_limit == settings.default_query_row_limit
    assert result.row_limit_source == "GATEWAY_CAP"


async def test_the_callers_own_max_rows_is_reported_as_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_rows=10` below the default is the caller's own bound. Reporting
    it as `GATEWAY_CAP` would tell a reader the platform withheld rows it
    never asked for."""
    result = await _execute(
        monkeypatch, sql="SELECT id FROM analytics.customers", requested_limit=10
    )

    assert result.applied_row_limit == 10
    assert result.row_limit_source == "REQUEST"


async def test_a_statements_own_limit_below_the_cap_is_reported_as_the_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the review named: a query whose own `LIMIT` is below the
    gateway's cap. The applied limit is the statement's, and the source says
    so -- so a reader is not told the platform truncated something the
    statement itself bounded."""
    result = await _execute(
        monkeypatch, sql="SELECT id FROM analytics.customers LIMIT 3", requested_limit=100
    )

    assert result.applied_row_limit == 3
    assert result.row_limit_source == "STATEMENT"


async def test_a_request_above_the_hard_limit_is_clamped_and_reported_as_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking for more than the hard limit allows is the one case where a
    `max_rows` request still ends in a platform cap, and it must report as
    one: this is exactly "there may be rows you were not shown"."""
    settings = Settings(_env_file=None, default_query_row_limit=50, hard_query_row_limit=100)
    result = await _execute(
        monkeypatch,
        sql="SELECT id FROM analytics.customers",
        requested_limit=100_000,
        settings=settings,
    )

    assert result.applied_row_limit == 100
    assert result.row_limit_source == "GATEWAY_CAP"


# ---------------------------------------------------------------------------
# 2. the regression the field exists to remove
# ---------------------------------------------------------------------------


async def test_a_subquery_limit_defeats_the_regex_the_field_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the bound back off the generated SQL is not just fragile here,
    it is wrong.

    `SqlGuard` rewrites only the *outer* statement, so a `LIMIT` inside a
    subquery survives ahead of it in the normalized text. The browser's
    `/\\blimit\\s+(\\d+)\\b/i` takes the first match, so it would read 7 --
    a number that bounds an inner scan and says nothing about how many rows
    the caller received. `applied_row_limit` is the outer bound the result
    actually ran under, and this test pins that the two disagree, so the
    field can never be quietly replaced by the guess again.
    """
    import re

    settings = Settings(_env_file=None)
    result = await _execute(
        monkeypatch,
        sql="SELECT c.id FROM (SELECT id FROM analytics.customers LIMIT 7) AS c",
        requested_limit=None,
    )

    assert result.applied_row_limit == settings.default_query_row_limit
    assert result.row_limit_source == "GATEWAY_CAP"

    normalized = result.execution.normalized_sql or ""
    guessed = re.search(r"\blimit\s+(\d+)\b", normalized, re.IGNORECASE)
    assert guessed is not None
    assert int(guessed.group(1)) == 7, "the regex reads the subquery's bound, not the applied one"
    assert int(guessed.group(1)) != result.applied_row_limit


# ---------------------------------------------------------------------------
# 3. the projection both routes share
# ---------------------------------------------------------------------------


def _gateway_result(**overrides: object) -> GatewayResult:
    execution = QueryExecution(
        id=uuid4(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        principal_id="p-1",
        dialect="postgres",
        status="COMPLETED",
        normalized_sql="SELECT\n  id\nFROM analytics.customers\nLIMIT 5000",
        referenced_tables=["analytics.customers"],
        referenced_columns=["id"],
        column_lineage=[],
        row_count=5000,
        elapsed_ms=12,
    )
    return GatewayResult(
        execution=execution,
        rows=(),
        masked_columns=(),
        **overrides,  # type: ignore[arg-type]
    )


def test_the_shared_projection_carries_both_fields() -> None:
    """`aida.api` and `aida.tool_api` both project through this one function,
    so proving it here proves both routes disclose the bound identically --
    the whole reason `query_execution_view` exists (R07)."""
    response = query_execution_response(
        _gateway_result(applied_row_limit=5000, row_limit_source="GATEWAY_CAP")
    )

    assert response.applied_row_limit == 5000
    assert response.row_limit_source == "GATEWAY_CAP"


def test_no_applied_limit_projects_as_null_not_as_a_default() -> None:
    """A gateway result with no applied limit must produce `null`, not the
    configured default. A default here would be the response claiming a cap
    the execution never ran under -- the same class of lie as inferring the
    bound from the SQL."""
    response = query_execution_response(_gateway_result())

    assert response.applied_row_limit is None
    assert response.row_limit_source is None


def test_the_response_field_is_optional_so_the_schema_change_is_additive() -> None:
    """The `openapi-diff` gate treats a newly *required* response field as
    breaking. Constructing the response without either field must work."""
    from aida.schemas import QueryExecutionResponse

    response = QueryExecutionResponse(
        execution_id=uuid4(),
        status="COMPLETED",
        normalized_sql="SELECT 1",
        referenced_tables=[],
        referenced_columns=[],
        column_lineage=[],
        plan_cost=0.0,
        warehouse_query_id=None,
        row_count=0,
        elapsed_ms=0,
        masked_columns=[],
        rows=[],
    )

    assert response.applied_row_limit is None
    assert response.row_limit_source is None
    assert "applied_row_limit" in QueryExecutionResponse.model_fields
    assert QueryExecutionResponse.model_fields["applied_row_limit"].is_required() is False


# ---------------------------------------------------------------------------
# 4. the classifier itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("applied", "requested", "expected"),
    [
        (None, None, None),
        (5000, None, "GATEWAY_CAP"),
        (5000, 5000, "GATEWAY_CAP"),
        (5000, 9000, "GATEWAY_CAP"),
        (10, 10, "REQUEST"),
        (3, 10, "STATEMENT"),
        (3, None, "STATEMENT"),
    ],
)
def test_row_limit_source_classification(
    applied: int | None, requested: int | None, expected: str | None
) -> None:
    """Every branch of the classifier, stated as a table.

    The boundary worth naming is `(5000, 5000)`: a caller that asks for
    exactly the default gets `GATEWAY_CAP`, because the platform would have
    applied the same bound anyway -- there is no observable difference to
    report, and claiming `REQUEST` would understate a genuine cap.
    """
    assert (
        row_limit_source(applied, requested_limit=requested, settings=Settings(_env_file=None))
        == expected
    )
