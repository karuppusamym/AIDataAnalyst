"""Behavioral coverage for `aida.retrieval` -- the hybrid BM25 + weighted-boost
metadata retrieval engine used by the agent orchestrator. Before this file, the
only tests touching retrieval were hand-constructed `RetrievalHit` fixtures fed
into unrelated planner tests; the scoring algorithm and `hybrid_retrieve`'s
ranking/dedup/cap behavior itself had never been exercised.
"""

from uuid import uuid4

import pytest

from aida.config import Settings
from aida.models import (
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    MetadataTable,
)
from aida.retrieval import (
    _bm25_score,
    _exact_phrase_bonus,
    _idf_weight,
    _tokenise,
    hybrid_retrieve,
)

# --- Pure scoring functions --------------------------------------------------


def test_tokenise_strips_stop_words_and_splits_snake_case_and_camel_case() -> None:
    tokens = _tokenise("Show me the Total_Revenue NetIncome")

    assert tokens == ["total", "revenue", "net", "income"]


def test_idf_weight_rewards_longer_less_common_looking_tokens() -> None:
    assert _idf_weight("ab") == 0.5
    assert _idf_weight("abcd") == 0.8
    assert _idf_weight("abcde") == 1.0


def test_bm25_score_is_the_idf_weighted_fraction_of_matched_query_tokens() -> None:
    # "revenue" (idf 1.0, len 7) matches; "tax" (idf 0.8, len 3) does not.
    score = _bm25_score(["revenue", "tax"], "quarterly revenue report")

    assert score == pytest.approx(1.0 / 1.8)


def test_bm25_score_returns_zero_for_empty_query_or_candidate_text() -> None:
    assert _bm25_score([], "quarterly revenue report") == 0.0
    assert _bm25_score(["revenue"], "") == 0.0


def test_exact_phrase_bonus_only_rewards_a_verbatim_substring_match() -> None:
    assert _exact_phrase_bonus("net revenue", "Net Revenue Fact Table") == 0.2
    assert _exact_phrase_bonus("net revenue", "gross margin by region") == 0.0


# --- hybrid_retrieve: ranking, boosts, cap -----------------------------------


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values

    def __iter__(self):
        return iter(self._values)


class _RetrievalSession:
    """Answers hybrid_retrieve's sequential fetches in call order: three
    `scalars()` calls (tables, columns, dbt project ids) and four `execute()`
    calls (governed tool versions, business annotations, SM-2 semantic-metric
    term bindings, SM-2 glossary-term semantic bindings). Leaving
    dbt_project_ids empty (the default) short-circuits the dbt-resource branch,
    which otherwise issues two further fetches.
    """

    def __init__(
        self,
        *,
        table_rows: list[object] | None = None,
        column_rows: list[object] | None = None,
        tool_rows: list[tuple[object, object]] | None = None,
        biz_rows: list[tuple[object, ...]] | None = None,
        dbt_project_ids: list[object] | None = None,
        metric_term_rows: list[tuple[object, ...]] | None = None,
        term_binding_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self._scalars_queue: list[list[object]] = [
            table_rows or [],
            column_rows or [],
            dbt_project_ids or [],
        ]
        self._execute_queue: list[list[tuple[object, ...]]] = [
            tool_rows or [],
            biz_rows or [],
            metric_term_rows or [],
            term_binding_rows or [],
        ]

    async def scalars(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self._scalars_queue.pop(0))

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self._execute_queue.pop(0))


def _sample_datasource(*, organization_id) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=organization_id,
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="core-banking",
        connector_type="POSTGRES",
        dialect="postgres",
        environment="PRODUCTION",
        credential_reference="vault://core-banking",
        status="ACTIVE",
    )


def _sample_table(*, organization_id, datasource_id, name: str, description: str) -> MetadataTable:
    return MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        schema_id=uuid4(),
        name=name,
        object_type="TABLE",
        status="ACTIVE",
        fingerprint=f"fp-{name}",
        source_description=description,
    )


def _sample_published_tool(
    *, organization_id, datasource_id, name: str, slug: str, description: str
) -> tuple[GovernedToolVersion, GovernedTool]:
    tool = GovernedTool(id=uuid4(), organization_id=organization_id, project_id=uuid4(), slug=slug)
    version = GovernedToolVersion(
        id=uuid4(),
        organization_id=organization_id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name=name,
        description=description,
        datasource_id=datasource_id,
        sql_template="SELECT 1",
        referenced_tables=[],
        parameter_schema=[],
        allowed_roles=["Analyst"],
        fingerprint=f"fp-{slug}",
        created_by="tool-dev",
    )
    return version, tool


async def test_hybrid_retrieve_ranks_a_governed_tool_above_an_equally_relevant_table() -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    # Both candidates match only the "customer" token out of four query tokens --
    # identical BM25 -- so the governed-tool priority boost is the only thing
    # that can separate them.
    table = _sample_table(
        organization_id=organization_id,
        datasource_id=datasource.id,
        name="dim_customer",
        description="Customer master dimension",
    )
    version, _tool = _sample_published_tool(
        organization_id=organization_id,
        datasource_id=datasource.id,
        name="Customer Master Lookup",
        slug="customer_master_lookup",
        description="Look up customer master records",
    )
    session = _RetrievalSession(table_rows=[table], tool_rows=[(version, _tool)])

    hits = await hybrid_retrieve(
        session,  # type: ignore[arg-type]
        datasource=datasource,
        question="customer lifetime value forecast",
        settings=Settings(_env_file=None),
    )

    assert [hit.object_type for hit in hits] == ["TOOL_VERSION", "TABLE"]
    tool_hit, table_hit = hits
    assert tool_hit.score == pytest.approx(0.5)  # 0.25 BM25 + 0.25 governed-tool boost
    assert table_hit.score == pytest.approx(0.25)  # same BM25, no boost
    assert tool_hit.score > table_hit.score


async def test_hybrid_retrieve_boosts_the_callers_preferred_tool_version() -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    version_a, tool_a = _sample_published_tool(
        organization_id=organization_id,
        datasource_id=datasource.id,
        name="Customer Master Lookup",
        slug="customer_master_lookup",
        description="Look up customer master records",
    )
    version_b, tool_b = _sample_published_tool(
        organization_id=organization_id,
        datasource_id=datasource.id,
        name="Customer Master Lookup Alt",
        slug="customer_master_lookup_alt",
        description="Look up customer master records",
    )
    session = _RetrievalSession(tool_rows=[(version_a, tool_a), (version_b, tool_b)])

    hits = await hybrid_retrieve(
        session,  # type: ignore[arg-type]
        datasource=datasource,
        question="customer lifetime value forecast",
        settings=Settings(_env_file=None),
        preferred_tool_version_id=version_b.id,
    )

    assert [hit.object_id for hit in hits] == [str(version_b.id), str(version_a.id)]
    preferred_hit, other_hit = hits
    assert preferred_hit.score == pytest.approx(0.85)  # +0.25 tool boost +0.35 preferred boost
    assert other_hit.score == pytest.approx(0.5)  # +0.25 tool boost only


async def test_hybrid_retrieve_sorts_by_score_and_caps_at_the_configured_limit() -> None:
    organization_id = uuid4()
    datasource = _sample_datasource(organization_id=organization_id)
    strong_match = _sample_table(
        organization_id=organization_id,
        datasource_id=datasource.id,
        name="fact_net_revenue",
        description="Net revenue after returns and discounts",
    )
    weak_match = _sample_table(
        organization_id=organization_id,
        datasource_id=datasource.id,
        name="dim_region",
        description="Region reference dimension",
    )
    session = _RetrievalSession(table_rows=[weak_match, strong_match])

    hits = await hybrid_retrieve(
        session,  # type: ignore[arg-type]
        datasource=datasource,
        question="net revenue",
        settings=Settings(agent_retrieval_limit=1, _env_file=None),
    )

    assert len(hits) == 1
    assert hits[0].object_id == str(strong_match.id)
