"""ST-A8: usage-derived eval question suite as a Studio change-set gate.

Two layers, mirroring the split between `aida.studio_eval` (domain/mining)
and `aida.studio_api` (the HTTP surface that wires the gate into the existing
test harness):

  1. Pure-function coverage of `check_eval_regressions` -- no database,
     matching `tests/test_studio.py`'s existing convention for this module.
  2. A real (in-memory sqlite) database scenario that seeds a genuine
     consumption edge and a genuine BI report -> metric -> column edge chain,
     mines eval questions from them, and then proves the single most
     important property this item exists for: a change set that breaks the
     mined metric's aggregation is rejected at the test gate *because of the
     mined eval question* -- not merely because the edited item also fails
     its own shape check. `run_tests`/`submit_change_set` are called
     directly against a shared session, the same pattern
     `tests/test_semantic_glossary_binding.py` already established for
     read-path Studio-adjacent coverage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from itertools import count
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.db import Base
from aida.models import (
    AuditEvent,
    BiArtifactImport,
    BiConnection,
    BiMetricColumnEdge,
    BiMetricNode,
    BiReportMetricEdge,
    BiReportNode,
    ConsumptionRecord,
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
    StudioEvalQuestion,
    StudioEvalResult,
    StudioEvalRun,
)
from aida.schemas import StudioChangeItemCreate, StudioChangeSetCreate
from aida.studio import ChangeItem
from aida.studio_api import (
    add_item,
    create_change_set,
    mine_eval_suite,
    run_tests,
    submit_change_set,
)
from aida.studio_eval import check_eval_regressions, mine_eval_questions
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# 1. check_eval_regressions -- pure, no database
# ---------------------------------------------------------------------------


def _metric_item(
    *, object_id: str = "m1", operation: str = "UPDATE", aggregation: str = "SUM"
) -> ChangeItem:
    return ChangeItem(
        id=uuid4(),
        object_type="METRIC",
        object_id=object_id,
        operation=operation,
        after_snapshot={"name": object_id, "aggregation": aggregation, "grain": "daily"},
    )


def _question(*, object_type: str = "METRIC", object_id: str = "m1") -> StudioEvalQuestion:
    return StudioEvalQuestion(
        id=uuid4(),
        organization_id=uuid4(),
        object_type=object_type,
        object_id=object_id,
        evidence_source="BI",
        evidence_edge_id=str(uuid4()),
        label=f"metric:{object_id}",
    )


class TestCheckEvalRegressions:
    def test_untouched_question_is_skipped(self) -> None:
        item = _metric_item(object_id="touched")
        question = _question(object_id="not-touched-at-all")
        checks = check_eval_regressions([item], [question])
        assert checks == []

    def test_touched_question_with_valid_snapshot_passes(self) -> None:
        item = _metric_item(object_id="m1", aggregation="SUM")
        question = _question(object_id="m1")
        checks = check_eval_regressions([item], [question])
        assert len(checks) == 1
        assert checks[0].result.passed is True
        assert checks[0].question.id == question.id

    def test_touched_question_with_broken_snapshot_fails(self) -> None:
        item = _metric_item(object_id="m1", aggregation="MEDIAN")  # not a valid aggregation
        question = _question(object_id="m1")
        checks = check_eval_regressions([item], [question])
        assert len(checks) == 1
        assert checks[0].result.passed is False
        assert any("MEDIAN" in f for f in checks[0].result.failures)

    def test_multiple_questions_each_checked_independently(self) -> None:
        items = [
            _metric_item(object_id="good", aggregation="SUM"),
            _metric_item(object_id="bad", aggregation="MEDIAN"),
        ]
        questions = [_question(object_id="good"), _question(object_id="bad")]
        checks = check_eval_regressions(items, questions)
        results = {c.question.object_id: c.result.passed for c in checks}
        assert results == {"good": True, "bad": False}


# ---------------------------------------------------------------------------
# 2. Real-engine scenario: mining + the change-set regression gate
# ---------------------------------------------------------------------------


_audit_id_counter = count(1)


def _assign_audit_id(mapper: object, connection: object, target: AuditEvent) -> None:
    """SQLite has no autoincrement for a `BIGINT` primary key (only for the
    sole-INTEGER-PK "rowid alias" case) -- `AuditEvent.id` is `BigInteger`,
    which is what Postgres's real `BIGSERIAL` needs in production, so it
    compiles to a plain `BIGINT NOT NULL` under sqlite with no server-side
    default. `record_audit` relies on the database assigning `id`, which is
    exactly why the closest existing real-engine test
    (`test_semantic_glossary_binding.py`) restricts itself to read paths that
    never call it. Assigning the id here, only for this in-memory sqlite
    engine, is what lets this file additionally exercise a real *write* path
    (`run_tests`/`submit_change_set`) end to end against real SQL."""
    if target.id is None:
        target.id = next(_audit_id_counter)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    event.listen(AuditEvent, "before_insert", _assign_audit_id)
    try:
        async with maker() as active:
            yield active
    finally:
        event.remove(AuditEvent, "before_insert", _assign_audit_id)
        await engine.dispose()


class _Scenario:
    """Seeds one organization with a governed tool (consumed via MCP) and a
    governed metric (bound to a BI dashboard field via a shared physical
    column) -- the minimum real evidence both mining paths need.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()

        self.measure_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="sale_amount",
            ordinal_position=1,
            physical_type="NUMERIC",
            nullable=False,
            fingerprint="fp-column",
        )
        db.add(self.measure_column)
        await db.flush()
        return self

    async def published_metric(
        self, *, slug: str, name: str, aggregation: str = "SUM"
    ) -> SemanticMetric:
        db = self.db
        model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=1,
            name=f"model-{slug}",
            change_summary="initial",
            status="PUBLISHED",
            created_by="metric-maker",
        )
        db.add(model)
        await db.flush()
        metric = SemanticMetric(
            organization_id=self.organization.id, project_id=self.project.id, slug=slug
        )
        db.add(metric)
        await db.flush()
        version = SemanticMetricVersion(
            organization_id=self.organization.id,
            semantic_model_version_id=model.id,
            metric_id=metric.id,
            version=1,
            status="PUBLISHED",
            name=name,
            description=f"{name} metric",
            aggregation=aggregation,
            grain="daily",
            source_table_id=self.table.id,
            measure_column_id=self.measure_column.id,
            fingerprint=f"fp-{slug}",
            created_by="metric-maker",
        )
        db.add(version)
        await db.flush()
        return metric

    async def governed_tool(self, *, slug: str, name: str) -> GovernedTool:
        db = self.db
        tool = GovernedTool(
            organization_id=self.organization.id, project_id=self.project.id, slug=slug
        )
        db.add(tool)
        await db.flush()
        version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=tool.id,
            version=1,
            status="PUBLISHED",
            name=name,
            description=f"{name} tool",
            datasource_id=self.datasource.id,
            sql_template="SELECT 1",
            referenced_tables=[],
            parameter_schema=[],
            allowed_roles=["Analyst"],
            fingerprint=f"fp-{slug}",
            created_by="tool-maker",
        )
        db.add(version)
        await db.flush()
        self.tool_version = version
        return tool

    async def tool_consumption_edge(self, *, tool_version_id: UUID) -> ConsumptionRecord:
        db = self.db
        record = ConsumptionRecord(
            organization_id=self.organization.id,
            consumer_id="agent-1",
            consumer_type="AGENT",
            resource_type="governed_tool_version",
            resource_id=str(tool_version_id),
            channel="MCP",
            correlation_id=str(uuid4()),
            policy_decision="ALLOW",
        )
        db.add(record)
        await db.flush()
        return record

    async def bi_report_metric_edge(self) -> BiReportMetricEdge:
        """A dashboard field bound to `self.measure_column` -- the same
        physical column `published_metric` is defined on."""
        db = self.db
        connection = BiConnection(
            organization_id=self.organization.id,
            project_id=self.project.id,
            datasource_id=self.datasource.id,
            bi_tool="TABLEAU",
            connection_key=f"conn-{uuid4().hex[:8]}",
            display_name="Sales Site",
            created_by="bi-admin",
        )
        db.add(connection)
        await db.flush()

        artifact_import = BiArtifactImport(
            organization_id=self.organization.id,
            connection_id=connection.id,
            artifact_fingerprint=f"fp-{uuid4().hex}",
            bi_tool="TABLEAU",
            report_count=1,
            metric_count=1,
            report_metric_edge_count=1,
            metric_column_edge_count=1,
            matched_column_count=1,
            unmatched_column_count=0,
            imported_by="bi-admin",
        )
        db.add(artifact_import)
        await db.flush()

        report = BiReportNode(
            organization_id=self.organization.id,
            artifact_import_id=artifact_import.id,
            external_id="dash-1",
            name="Sales Dashboard",
            report_type="DASHBOARD",
        )
        db.add(report)
        await db.flush()

        bi_metric = BiMetricNode(
            organization_id=self.organization.id,
            artifact_import_id=artifact_import.id,
            external_id="field-1",
            name="Total Sale Amount",
            field_type="ColumnField",
        )
        db.add(bi_metric)
        await db.flush()

        column_edge = BiMetricColumnEdge(
            organization_id=self.organization.id,
            artifact_import_id=artifact_import.id,
            metric_id=bi_metric.id,
            source_table_name=self.table.name,
            source_column_name=self.measure_column.name,
            matched_table_id=self.table.id,
            matched_column_id=self.measure_column.id,
        )
        db.add(column_edge)
        await db.flush()

        report_metric_edge = BiReportMetricEdge(
            organization_id=self.organization.id,
            artifact_import_id=artifact_import.id,
            report_id=report.id,
            metric_id=bi_metric.id,
        )
        db.add(report_metric_edge)
        await db.flush()
        return report_metric_edge

    def maker(self):
        return security_context(
            organization_id=self.organization.id,
            principal_id="maker@example.com",
            roles=frozenset({"DataSteward"}),
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


# ---------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------


async def test_mining_creates_one_tool_question_from_consumption(scenario: _Scenario) -> None:
    tool = await scenario.governed_tool(slug="lookup", name="Lookup")
    await scenario.tool_consumption_edge(tool_version_id=scenario.tool_version.id)

    result = await mine_eval_questions(scenario.db, organization_id=scenario.organization.id)
    assert result.consumption_edges_scanned == 1
    assert result.questions_created == 1

    questions = (
        await scenario.db.scalars(
            select(StudioEvalQuestion).where(
                StudioEvalQuestion.organization_id == scenario.organization.id
            )
        )
    ).all()
    assert len(questions) == 1
    assert questions[0].object_type == "TOOL"
    assert questions[0].object_id == str(tool.id)
    assert questions[0].evidence_source == "CONSUMPTION"
    # Value-free: the label names the governed object, never raw query text.
    assert questions[0].label == "tool:lookup"


async def test_mining_creates_one_metric_question_from_bi_edge(scenario: _Scenario) -> None:
    metric = await scenario.published_metric(slug="revenue", name="Revenue")
    edge = await scenario.bi_report_metric_edge()

    result = await mine_eval_questions(scenario.db, organization_id=scenario.organization.id)
    assert result.bi_edges_scanned == 1
    assert result.questions_created == 1

    questions = (
        await scenario.db.scalars(
            select(StudioEvalQuestion).where(
                StudioEvalQuestion.organization_id == scenario.organization.id
            )
        )
    ).all()
    assert len(questions) == 1
    assert questions[0].object_type == "METRIC"
    assert questions[0].object_id == str(metric.id)
    assert questions[0].evidence_source == "BI"
    assert questions[0].evidence_edge_id == str(edge.id)
    assert questions[0].label == "metric:Revenue"


async def test_mining_is_idempotent(scenario: _Scenario) -> None:
    tool = await scenario.governed_tool(slug="lookup", name="Lookup")
    await scenario.tool_consumption_edge(tool_version_id=scenario.tool_version.id)

    first = await mine_eval_questions(scenario.db, organization_id=scenario.organization.id)
    assert first.questions_created == 1

    # A second consumption edge for the *same* tool must not create a
    # duplicate question -- dedup is per distinct object, not per raw event.
    await scenario.tool_consumption_edge(tool_version_id=scenario.tool_version.id)
    second = await mine_eval_questions(scenario.db, organization_id=scenario.organization.id)
    assert second.questions_created == 0
    assert second.questions_already_mined == 1

    total = await scenario.db.scalar(
        select(func.count()).select_from(StudioEvalQuestion).where(
            StudioEvalQuestion.organization_id == scenario.organization.id
        )
    )
    assert total == 1
    assert tool.id is not None  # sanity: fixture built the tool this asserts against


# ---------------------------------------------------------------------------
# The exit-condition test: the mined question blocks a regressing submission
# ---------------------------------------------------------------------------


async def test_mined_eval_question_blocks_regressing_change_set(scenario: _Scenario) -> None:
    metric = await scenario.published_metric(slug="revenue", name="Revenue", aggregation="SUM")
    await scenario.bi_report_metric_edge()

    mining = await mine_eval_questions(scenario.db, organization_id=scenario.organization.id)
    assert mining.questions_created == 1
    await scenario.db.flush()

    question = (
        await scenario.db.scalars(
            select(StudioEvalQuestion).where(
                StudioEvalQuestion.organization_id == scenario.organization.id,
                StudioEvalQuestion.object_type == "METRIC",
                StudioEvalQuestion.object_id == str(metric.id),
            )
        )
    ).one()

    context = scenario.maker()
    cs_read = await create_change_set(
        StudioChangeSetCreate(name="break-revenue"), context=context, session=scenario.db
    )

    # The proposed edit breaks the metric's aggregation -- an invalid value
    # `_validate_metric_item` also rejects on its own. The point of this test
    # is what happens *because a mined question exists for this object*, so
    # every assertion below checks eval-specific evidence, not just the
    # generic "submission failed" outcome that item-level validation alone
    # would already produce.
    await add_item(
        cs_read.id,
        StudioChangeItemCreate(
            object_type="METRIC",
            object_id=str(metric.id),
            operation="UPDATE",
            before_snapshot={"name": "Revenue", "aggregation": "SUM", "grain": "daily"},
            after_snapshot={"name": "Revenue", "aggregation": "MEDIAN", "grain": "daily"},
        ),
        context=context,
        session=scenario.db,
    )

    test_result = await run_tests(cs_read.id, context=context, session=scenario.db)
    assert test_result.passed is False

    # 1. A StudioEvalResult row exists, tied to *this specific mined
    #    question*, recording the regression -- this is the ST-A8 mechanism
    #    operating, not an inference from the generic suite result.
    eval_results = (
        await scenario.db.scalars(
            select(StudioEvalResult).where(
                StudioEvalResult.eval_question_id == question.id
            )
        )
    ).all()
    assert len(eval_results) == 1
    assert eval_results[0].passed is False
    assert any("MEDIAN" in f for f in eval_results[0].evidence.get("failures", []))

    # 2. The eval run itself records the failing question id explicitly.
    eval_run = (
        await scenario.db.scalars(
            select(StudioEvalRun).where(StudioEvalRun.change_set_id == cs_read.id)
        )
    ).one()
    assert eval_run.passed is False
    assert eval_run.evidence["failed_question_ids"] == [str(question.id)]

    # 3. Submission is rejected, and the rejection detail specifically names
    #    the mined eval-question regression -- not a generic failure message.
    with pytest.raises(HTTPException) as exc_info:
        await submit_change_set(cs_read.id, context=context, session=scenario.db)
    assert exc_info.value.status_code == 409
    assert "mined eval question" in exc_info.value.detail
    assert str(question.id) in exc_info.value.detail


async def test_change_set_with_no_mined_questions_is_unaffected(scenario: _Scenario) -> None:
    """A change set touching an object nothing was ever mined for gets no
    eval checks at all -- the gate adds nothing to inspect, and does not
    spuriously block on emptiness."""
    metric = await scenario.published_metric(slug="revenue", name="Revenue")
    # Deliberately no mining pass, no BI edge: no StudioEvalQuestion exists.

    context = scenario.maker()
    cs_read = await create_change_set(
        StudioChangeSetCreate(name="untracked-edit"), context=context, session=scenario.db
    )
    await add_item(
        cs_read.id,
        StudioChangeItemCreate(
            object_type="METRIC",
            object_id=str(metric.id),
            operation="UPDATE",
            before_snapshot={"name": "Revenue", "aggregation": "SUM", "grain": "daily"},
            after_snapshot={"name": "Revenue", "aggregation": "AVG", "grain": "daily"},
        ),
        context=context,
        session=scenario.db,
    )

    test_result = await run_tests(cs_read.id, context=context, session=scenario.db)
    assert test_result.passed is True
    assert test_result.evidence["eval_regression_checked"] == 0

    submitted = await submit_change_set(cs_read.id, context=context, session=scenario.db)
    assert submitted.status == "SUBMITTED"


async def test_mining_api_endpoint_is_audited_and_bounded(scenario: _Scenario) -> None:
    tool = await scenario.governed_tool(slug="lookup", name="Lookup")
    await scenario.tool_consumption_edge(tool_version_id=scenario.tool_version.id)

    context = scenario.maker()
    result = await mine_eval_suite(context=context, session=scenario.db)
    assert result.questions_created == 1
    assert result.truncated is False
    assert tool.id is not None
