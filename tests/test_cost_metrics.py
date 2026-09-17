"""R11-FP17: parser and model cost, and the two ways a cost figure lies.

`aida.cost_metrics` exists because the review asked for "parser/model cost
metrics per source" and there were none. These pin the two decisions that make
it honest, because both are the kind that decay quietly:

**Showback, not chargeback.** A provider-reported token count is money; this
platform's 4-bytes-per-token estimate is not. They are the same series with
different `basis` label values and are never summed into one number, and
`plan_cost` -- a connector-shaped proxy that is not comparable to itself across
connectors, let alone to a token -- is not carried here at all. A test that
merely checked "tokens are counted" would pass on a version that added the two
together, which is the version this file exists to prevent.

**Per-source attribution lives in rows, not in a label.** A `datasource_id`
label on the highest-frequency series in the platform is the unbounded
cardinality review F17 records the cost of, and a tenant identifier on a
scrapeable surface besides. So the metric labels are closed sets and the source
dimension is carried in `source_usage_window`. The cardinality half of that is
machine-checked below rather than left to code review, because adding a label is
one keyword argument and the consequence arrives months later in someone's
Prometheus.
"""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.cost_metrics import (
    MODEL_CALLS,
    MODEL_PAYLOAD_BYTES,
    MODEL_TOKENS,
    PARSER_INPUT_BYTES,
    PARSER_SECONDS,
    PARSER_STATEMENTS,
    SPEND_ATTRIBUTION,
    ParseOutcome,
    Parser,
    SpendBasis,
    billable_tokens,
    classify_parse,
    dialect_label,
    observe_model_call,
    parser_span,
    record_model_spend,
    record_parser_spend,
)
from aida.model_gateway import ModelCallEvidence
from aida.models import SourceUsageWindow, TenantUsageWindow
from aida.usage_quotas import QUOTA_DECISIONS, UsageDimension, source_usage, tenant_usage
from tests.support.task_agents import seed_estate, task_agent_session


@pytest.fixture
async def session() -> AsyncSession:  # type: ignore[misc]
    async with task_agent_session() as active:
        yield active


def _evidence(
    *,
    provider_type: str = "OPENAI",
    estimated_input: int = 400,
    estimated_output: int = 100,
    provider_input: int | None = None,
    provider_output: int | None = None,
) -> ModelCallEvidence:
    """A real `ModelCallEvidence`, not a stand-in.

    Using the gateway's own frozen dataclass is part of the test: it is what
    proves `ModelSpendEvidence` is a protocol that `ModelCallEvidence` actually
    satisfies, rather than one that happens to type-check against a double
    written to match it.
    """
    return ModelCallEvidence(
        route="openai:gpt-test",
        provider_type=provider_type,
        model_id="gpt-test",
        endpoint_alias="default",
        input_fingerprint="a" * 64,
        output_fingerprint="b" * 64,
        input_size_bytes=1_600,
        output_size_bytes=400,
        schema_name="SqlGenerationOutput",
        estimated_input_tokens=estimated_input,
        estimated_output_tokens=estimated_output,
        provider_input_tokens=provider_input,
        provider_output_tokens=provider_output,
    )


def _value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


# --- showback / chargeback ---------------------------------------------------


def test_an_unreported_call_is_estimated_and_says_so() -> None:
    charged, basis = billable_tokens(_evidence(estimated_input=400, estimated_output=100))

    assert charged == 500
    assert basis is SpendBasis.ESTIMATED


def test_a_reported_call_is_charged_on_what_the_provider_billed() -> None:
    charged, basis = billable_tokens(
        _evidence(
            estimated_input=400,
            estimated_output=100,
            provider_input=380,
            provider_output=95,
        )
    )

    # 475, not 975: the estimate is not added to the report. A version that
    # summed them would double-count every billed call.
    assert charged == 475
    assert basis is SpendBasis.REPORTED


def test_a_partially_reported_call_fills_the_missing_direction_with_the_estimate() -> None:
    """Input billed, output not. Treating the missing direction as zero would
    understate the bill; calling the whole figure an estimate would understate
    what is known. It is REPORTED with the gap filled, and the basis says so."""
    charged, basis = billable_tokens(
        _evidence(estimated_input=400, estimated_output=100, provider_input=380)
    )

    assert charged == 480
    assert basis is SpendBasis.REPORTED


def test_observe_model_call_keeps_the_two_bases_as_separate_series() -> None:
    before_reported = _value(
        "aida_model_call_tokens_total",
        provider_type="TESTPROVIDER",
        direction="input",
        basis="REPORTED",
    )
    before_estimated = _value(
        "aida_model_call_tokens_total",
        provider_type="TESTPROVIDER",
        direction="input",
        basis="ESTIMATED",
    )

    observe_model_call(
        _evidence(
            provider_type="TESTPROVIDER",
            estimated_input=400,
            estimated_output=100,
            provider_input=380,
            provider_output=95,
        )
    )

    # Both bases move for one call: the report because it is what was billed,
    # the estimate because it is what the input cap was checked against, and
    # comparing them is how a drifting estimator gets noticed.
    assert (
        _value(
            "aida_model_call_tokens_total",
            provider_type="TESTPROVIDER",
            direction="input",
            basis="REPORTED",
        )
        - before_reported
        == 380
    )
    assert (
        _value(
            "aida_model_call_tokens_total",
            provider_type="TESTPROVIDER",
            direction="input",
            basis="ESTIMATED",
        )
        - before_estimated
        == 400
    )


def test_a_call_with_no_provider_usage_counts_only_the_estimate() -> None:
    provider = "NOUSAGEPROVIDER"
    observe_model_call(_evidence(provider_type=provider, estimated_input=8, estimated_output=2))

    assert (
        _value(
            "aida_model_call_tokens_total",
            provider_type=provider,
            direction="input",
            basis="REPORTED",
        )
        == 0.0
    )
    assert (
        _value("aida_model_calls_total", provider_type=provider, basis="ESTIMATED") >= 1.0
    )
    # Payload bytes need no provider cooperation and no estimate at all.
    assert (
        _value("aida_model_call_bytes_total", provider_type=provider, direction="input") == 1_600
    )


def test_no_series_here_is_denominated_in_money() -> None:
    """There is no billing integration, so a `*_cost_dollars` or `*_cost_usd`
    series would be a fabricated number wearing a unit. `aida.cost_showback`'s
    COST_BASIS is the standing decision; this keeps it true of the metric
    surface as well as of the report."""
    forbidden = ("dollar", "usd", "_cost_", "_money", "_price", "_spend_amount")
    names = [
        metric.name
        for metric in REGISTRY.collect()
        if metric.name.startswith(("aida_parser", "aida_model", "aida_usage_quota"))
    ]

    assert names, "no cost or quota series are registered; this test has stopped checking"
    offenders = [name for name in names if any(token in name for token in forbidden)]
    assert not offenders, f"cost series denominated in money: {offenders}"


# --- cardinality -------------------------------------------------------------


def test_no_cost_or_quota_series_carries_a_tenant_or_source_label() -> None:
    """The cardinality invariant, machine-checked.

    `footprint_metrics`, `projection_metrics` and `retrieval_metrics` all refuse
    a tenant identifier in a label on F17's grounds, and these series are higher
    frequency than any of them -- a model call per drafting batch, a parse per
    view definition. Adding `datasource_id=` here is one keyword argument and the
    consequence lands months later in someone's Prometheus, so it is a failing
    test rather than a comment.
    """
    forbidden = {
        "organization",
        "organization_id",
        "org",
        "org_id",
        "tenant",
        "tenant_id",
        "datasource",
        "datasource_id",
        "source_id",
        "workspace",
        "workspace_id",
        "line_of_business",
        "principal",
        "principal_id",
        "table",
        "table_id",
        "column",
        "column_id",
    }
    metrics: list[Any] = [
        PARSER_SECONDS,
        PARSER_STATEMENTS,
        PARSER_INPUT_BYTES,
        MODEL_CALLS,
        MODEL_TOKENS,
        MODEL_PAYLOAD_BYTES,
        SPEND_ATTRIBUTION,
        QUOTA_DECISIONS,
    ]

    for metric in metrics:
        # `_labelnames` is prometheus_client's own storage for the declared
        # label set. Read directly because the public surface only exposes
        # labels that have been used, and an unused-but-declared tenant label is
        # exactly as dangerous as a used one.
        offenders = sorted(set(metric._labelnames) & forbidden)
        assert not offenders, f"{metric._name} declares unbounded label(s): {offenders}"


def test_the_dialect_label_is_a_closed_set() -> None:
    """`DataSource.dialect` is a validated column today, but a metric label taken
    from a column is one schema change away from being unbounded."""
    assert dialect_label("postgres") == "postgres"
    assert dialect_label("tsql") == "tsql"
    assert dialect_label("db2") == "other"
    assert dialect_label("") == "other"


# --- parser spans ------------------------------------------------------------


def test_classify_parse_keeps_a_deployment_fault_out_of_the_expected_noise() -> None:
    """A missing sqlglot and an unparseable definition both produce zero edges.
    Collapsing them would bury the only alertable outcome inside the one every
    estate always has some of."""
    assert (
        classify_parse(["sqlglot library is not available"], has_edges=False)
        is ParseOutcome.FAILED
    )
    assert (
        classify_parse(["unsupported dialect: db2"], has_edges=False)
        is ParseOutcome.UNSUPPORTED_DIALECT
    )
    assert classify_parse(["parse error: boom"], has_edges=False) is ParseOutcome.UNPARSEABLE
    assert classify_parse([], has_edges=True) is ParseOutcome.PARSED
    # A deployment fault wins over a dialect complaint when both are present:
    # with no sqlglot, the dialect was never really the problem.
    assert (
        classify_parse(
            ["unsupported dialect: db2", "sqlglot library is not available"], has_edges=False
        )
        is ParseOutcome.FAILED
    )


def test_a_parse_that_raises_is_still_timed_and_counted_as_failed() -> None:
    before = _value(
        "aida_parser_statements_total",
        parser="query_history",
        dialect="oracle",
        outcome="FAILED",
    )

    with pytest.raises(RuntimeError, match="boom"), parser_span(
        Parser.QUERY_HISTORY, dialect="oracle", sql="SELECT 1"
    ):
        raise RuntimeError("boom")

    # The span's outcome defaults to FAILED precisely so that the only way to
    # leave it unreported -- raising through it -- is counted as what it is.
    assert (
        _value(
            "aida_parser_statements_total",
            parser="query_history",
            dialect="oracle",
            outcome="FAILED",
        )
        - before
        == 1
    )
    assert (
        REGISTRY.get_sample_value(
            "aida_parser_duration_seconds_count", {"parser": "query_history", "dialect": "oracle"}
        )
        or 0.0
    ) >= 1.0


def test_a_span_counts_the_statements_and_bytes_it_was_told_about() -> None:
    sql = "SELECT a FROM t"
    before_statements = _value(
        "aida_parser_statements_total",
        parser="procedure_lineage",
        dialect="tsql",
        outcome="PARSED",
    )
    before_bytes = _value(
        "aida_parser_input_bytes_total", parser="procedure_lineage", dialect="tsql"
    )

    with parser_span(Parser.PROCEDURE_LINEAGE, dialect="tsql", sql=sql) as span:
        span.observed(ParseOutcome.PARSED, statements=17)

    assert (
        _value(
            "aida_parser_statements_total",
            parser="procedure_lineage",
            dialect="tsql",
            outcome="PARSED",
        )
        - before_statements
        == 17
    )
    assert (
        _value("aida_parser_input_bytes_total", parser="procedure_lineage", dialect="tsql")
        - before_bytes
        == len(sql.encode())
    )


def test_the_view_lineage_parser_is_instrumented_at_its_entry_point() -> None:
    """Every caller of `parse_view_lineage` is counted without any of them having
    to remember -- which is the only way the peer-owned call sites get counted."""
    from aida.sql_lineage_parser import parse_view_lineage

    before = _value(
        "aida_parser_statements_total",
        parser="view_lineage",
        dialect="postgres",
        outcome="PARSED",
    )

    result = parse_view_lineage("CREATE VIEW v AS SELECT a FROM t", dialect="postgres")

    assert result.edges
    assert (
        _value(
            "aida_parser_statements_total",
            parser="view_lineage",
            dialect="postgres",
            outcome="PARSED",
        )
        - before
        == 1
    )


# --- per-source attribution in rows -----------------------------------------


async def test_model_spend_is_attributed_to_the_source_in_a_row(
    session: AsyncSession,
) -> None:
    org, datasource, _schema = await seed_estate(session)

    await record_model_spend(
        session,
        organization_id=org.id,
        datasource_id=datasource.id,
        evidence=_evidence(provider_input=300, provider_output=200),
    )

    assert (
        await source_usage(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ).date(),
        )
        == 500
    )


async def test_spend_with_no_source_is_recorded_against_the_tenant_and_counted_unattributed(
    session: AsyncSession,
) -> None:
    """A call on a path with no single source is still real spend. Dropping it
    would make the tenant total quietly wrong, so it is recorded at the tenant
    and the attribution counter says how much of the fleet's spend is in that
    state."""
    org, _datasource, _schema = await seed_estate(session)
    before = _value("aida_model_spend_attribution_total", attributed="false")

    await record_model_spend(
        session,
        organization_id=org.id,
        datasource_id=None,
        evidence=_evidence(provider_input=10, provider_output=5),
    )

    assert _value("aida_model_spend_attribution_total", attributed="false") - before == 1
    assert (await session.scalars(select(SourceUsageWindow))).all() == []
    assert (await session.scalars(select(TenantUsageWindow))).all()


async def test_recording_spend_never_refuses_it(session: AsyncSession) -> None:
    """`record_model_spend` goes through `record_usage`, not `consume_quota`.
    Attributing spend that has already happened must never raise: the money is
    gone either way, and losing the record is the only avoidable harm."""
    org, datasource, _schema = await seed_estate(session)

    for _ in range(4):
        await record_model_spend(
            session,
            organization_id=org.id,
            datasource_id=datasource.id,
            evidence=_evidence(provider_input=1_000_000, provider_output=1_000_000),
        )

    assert (
        await tenant_usage(
            session,
            organization_id=org.id,
            dimension=UsageDimension.MODEL_TOKENS,
            window_date=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ).date(),
        )
        == 8_000_000
    )


async def test_a_zero_token_call_records_nothing_but_is_still_attributed(
    session: AsyncSession,
) -> None:
    org, datasource, _schema = await seed_estate(session)
    before = _value("aida_model_spend_attribution_total", attributed="true")

    await record_model_spend(
        session,
        organization_id=org.id,
        datasource_id=datasource.id,
        evidence=_evidence(estimated_input=0, estimated_output=0),
    )

    assert _value("aida_model_spend_attribution_total", attributed="true") - before == 1
    assert (await session.scalars(select(SourceUsageWindow))).all() == []


async def test_a_declined_parse_attributes_no_statements(session: AsyncSession) -> None:
    """A parse that produced nothing still cost the parser's time -- which the
    duration histogram already has -- but attributing zero statements to the
    source is the truthful record, so this is a no-op rather than an error."""
    org, datasource, _schema = await seed_estate(session)

    await record_parser_spend(
        session, organization_id=org.id, datasource_id=datasource.id, statements=0
    )

    assert (await session.scalars(select(SourceUsageWindow))).all() == []
