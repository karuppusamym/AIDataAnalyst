"""R11-FP12/FP16: a context product says what meaning it stands on, and when its basis moved.

A context product is a published, reproducible basis for answers, and it went stale silently in two
ways: the meaning it depends on changed (an approved description superseded or withdrawn, a pinned
semantic model moved past), or a routine or view it covers was redefined underneath it. Coverage
reported each view's and routine's digest, but nothing compared a digest with what the version was
published over, and the pinned meaning appeared nowhere but as bare ids. These tests pin:

* coverage reports each pinned ontology, semantic model and glossary term version -- whether it
  is still current, and what it speaks about *within the product's own scope* -- and a product
  with no pinned meaning and nothing moved compiles byte-identically to before;
* `changed_since_published` names each covered definition that moved after publication (a
  structural move is never hidden behind a later literal-only one) and each covered description
  whose approved text the product was published over is no longer the text a reader is given --
  net, so text replaced and restored is no change, and an identical re-approval never is one;
  nothing outside the product's scope, nothing before publication, nothing for a draft;
* compile, download, drift and MCP's resource read render one coverage shape, and a literal-only
  redefinition that leaves every digest equal still reads as drift;
* the rebuild puts a product whose basis moved back into review and never publishes it: the
  published version keeps serving, a person re-verifies, the view's hold waits for that, and a
  rejected re-verification is not proposed again until something else moves;
* a covered routine the source retired is followed to the routine that replaced its signature,
  or dropped, so the product can be re-drafted at all.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import publish_asset_documentation_version
from aida.change_signal_models import MetadataChangeSignal
from aida.column_documentation import publish_column_description
from aida.context_compiler import (
    ResolvedCoverageChange,
    ResolvedMeaningCoverage,
    compile_context_product,
    coverage_section,
)
from aida.context_compiler_api import (
    compile_context_product_version,
    download_context_compilation,
    inspect_context_compilation_drift,
)
from aida.context_product_api import create_context_product, submit_context_product_version
from aida.context_product_coverage import (
    load_coverage_changes,
    load_pinned_meaning,
    publication_time,
)
from aida.context_rebuild import CONTEXT_REBUILD_PRINCIPAL, WAIT_PRODUCT_NOT_REVERIFIED
from aida.envelope_models import MetadataRoutine
from aida.mcp_server import _read_context_product_resource
from aida.models import (
    AssetDocumentationVersion,
    AssetTermLink,
    ColumnDocumentationVersion,
    ContextProduct,
    ContextProductVersion,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.platform_schemas import ContextCompilationDriftRequest
from aida.routine_description_service import publish_routine_documentation_version
from aida.schemas import ContextProductCreate
from aida.security import SecurityContext
from tests.support.task_agents import human, task_agent_session
from tests.test_context_product_routines import _fixture
from tests.test_context_rebuild import (
    Estate,
    _approve,
    _estate,
    _hold,
    _rebuild,
    _redefine,
    _reject_rebuilt,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with task_agent_session() as active:
        yield active


# --- the compiled artifact ------------------------------------------------------------------


def _meaning_entry(current: bool = True) -> ResolvedMeaningCoverage:
    return ResolvedMeaningCoverage(
        kind="SEMANTIC_MODEL",
        version_id="00000000-0000-0000-0000-000000000001",
        key=None,
        version=1,
        status="PUBLISHED" if current else "SUPERSEDED",
        current=current,
        table_ids=("00000000-0000-0000-0000-00000000000a",),
    )


def test_a_product_with_nothing_pinned_and_nothing_moved_compiles_as_before() -> None:
    product, version, tables = _fixture()

    for target in ("MCP", "REST", "YAML", "OSI", "ODCS"):
        plain = compile_context_product(product, version, target, tables)
        empty = compile_context_product(product, version, target, tables, meaning=[], changes=[])
        assert empty.content == plain.content and empty.artifact_hash == plain.artifact_hash
        assert "coverage" not in plain.content


def test_pinned_meaning_and_moved_basis_reach_only_the_atlas_native_coverage() -> None:
    product, version, tables = _fixture()
    meaning = [_meaning_entry(current=False)]
    changes = [
        ResolvedCoverageChange("VIEW", tables[0].table_id, "DEFINITION_CHANGED", "LITERAL_ONLY")
    ]
    expected = coverage_section([], [], meaning, changes)
    assert expected["meaning"][0]["current"] is False
    assert expected["changed_since_published"] == [
        {
            "subject_kind": "VIEW",
            "subject_id": tables[0].table_id,
            "change": "DEFINITION_CHANGED",
            "change_class": "LITERAL_ONLY",
        }
    ]

    for target, envelope in (("MCP", "context"), ("REST", "context"), ("YAML", "spec")):
        compiled = compile_context_product(
            product, version, target, tables, meaning=meaning, changes=changes
        )
        body = (yaml.safe_load if target == "YAML" else json.loads)(compiled.content)[envelope]
        assert body["coverage"] == expected
    for target in ("OSI", "ODCS", "SNOWFLAKE_SEMANTIC_VIEW", "DATABRICKS_METRIC_VIEW"):
        content = compile_context_product(
            product, version, target, tables, meaning=meaning, changes=changes
        ).content
        assert "changed_since_published" not in content and "coverage" not in content
    # Without either key the section is exactly the one MCP always rendered.
    assert coverage_section([], []) == {"routines": [], "views": []}


# --- resolution against the catalog ---------------------------------------------------------


async def _publish_product(estate: Estate, key: str, **references: Any) -> ContextProductVersion:
    """Create, submit and approve a product through the real routes, maker-checker."""
    await create_context_product(
        estate.project.id,
        ContextProductCreate(
            product_key=key,
            name="Orders",
            description="Orders and revenue, for agents answering order questions.",
            purpose="Answer questions about order amounts and customer revenue.",
            owner_type="INDIVIDUAL",
            owner_principal="steward-1",
            allowed_consumer_roles=["Analyst"],
            **references,
        ),
        context=estate.steward,
        session=estate.session,
    )
    version = await estate.session.scalar(
        select(ContextProductVersion)
        .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
        .where(ContextProduct.product_key == key)
    )
    assert version is not None
    review = await submit_context_product_version(
        version.id, context=estate.steward, session=estate.session
    )
    await _approve(estate, review.id)
    await estate.session.refresh(version)
    assert version.status == "PUBLISHED"
    return version


async def _customers(estate: Estate) -> MetadataTable:
    customers = await estate.session.scalar(
        select(MetadataTable).where(
            MetadataTable.datasource_id == estate.datasource.id, MetadataTable.name == "customers"
        )
    )
    assert customers is not None
    return customers


async def _column(estate: Estate, table: MetadataTable, name: str) -> MetadataColumn:
    column = await estate.session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.table_id == table.id, MetadataColumn.name == name
        )
    )
    assert column is not None
    return column


def _signal(
    estate: Estate,
    kind: str,
    subject_id: UUID,
    signal_type: str,
    change_class: str | None,
    at: datetime | None = None,
) -> MetadataChangeSignal:
    signal = MetadataChangeSignal(
        organization_id=estate.org.id,
        datasource_id=estate.datasource.id,
        subject_kind=kind,
        subject_id=subject_id,
        signal_type=signal_type,
        change_class=change_class,
        **({"detected_at": at} if at is not None else {}),
    )
    estate.session.add(signal)
    return signal


async def _routine(estate: Estate, name: str, *, status: str = "ACTIVE") -> MetadataRoutine:
    routine = MetadataRoutine(
        organization_id=estate.org.id,
        datasource_id=estate.datasource.id,
        schema_id=estate.orders.schema_id,
        name=name,
        signature="()",
        routine_type="PROCEDURE",
        body_sql_redacted="BEGIN NULL; END",
        redaction_status="PARSED",
        screening_status="CLEAN",
        status=status,
        fingerprint="fp",
    )
    estate.session.add(routine)
    await estate.session.flush()
    return routine


async def _describe_table(estate: Estate, table: MetadataTable, readme: str) -> UUID:
    version = await publish_asset_documentation_version(
        estate.session,
        organization_id=estate.org.id,
        table_id=table.id,
        readme=readme,
        created_by="steward-1",
        approved_by="reviewer-1",
        approved_at=datetime.now(UTC),
    )
    await estate.session.commit()
    return version.id


async def _withdraw(estate: Estate, version_id: UUID) -> None:
    version = await estate.session.get(AssetDocumentationVersion, version_id)
    assert version is not None
    version.status = "WITHDRAWN"
    version.updated_at = datetime.now(UTC)
    await estate.session.commit()


async def test_pinned_meaning_says_whether_it_stands_and_only_what_the_product_covers(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    customers = await _customers(estate)
    project_id = estate.project.id
    model = SemanticModelVersion(
        organization_id=estate.org.id,
        project_id=project_id,
        version=1,
        name="Revenue model",
        change_summary="First.",
        status="PUBLISHED",
        created_by="modeller",
    )
    session.add(model)
    await session.flush()
    for slug, table in (("order_amount", estate.orders), ("customer_count", customers)):
        metric = SemanticMetric(organization_id=estate.org.id, project_id=project_id, slug=slug)
        session.add(metric)
        await session.flush()
        session.add(
            SemanticMetricVersion(
                organization_id=estate.org.id,
                semantic_model_version_id=model.id,
                metric_id=metric.id,
                version=1,
                status="PUBLISHED",
                name=slug,
                description="A metric.",
                aggregation="SUM",
                grain="day",
                source_table_id=table.id,
                fingerprint="fp",
                created_by="modeller",
            )
        )
    term = GlossaryTerm(organization_id=estate.org.id, term_key="net_revenue")
    session.add(term)
    await session.flush()
    definition = GlossaryTermVersion(
        organization_id=estate.org.id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net revenue",
        definition="Revenue after discounts.",
        created_by="steward-2",
    )
    session.add(definition)
    for table in (estate.orders, customers):
        session.add(
            AssetTermLink(
                organization_id=estate.org.id, table_id=table.id, term_id=term.id, linked_by="s"
            )
        )
    amount = await _column(estate, estate.orders, "amount")
    head = OntologyHead(
        organization_id=estate.org.id, ontology_key="commerce", last_version=2, published_version=2
    )
    session.add(head)
    await session.flush()
    first = OntologyVersion(
        organization_id=estate.org.id,
        ontology_id=head.id,
        version=1,
        base_version=0,
        status="APPROVED",
        definition={
            "name": "Commerce",
            "concepts": [{"key": "revenue", "name": "Revenue"}],
            "mappings": [
                {"concept": "revenue", "subject_type": "COLUMN", "subject_id": str(amount.id)},
                {"concept": "revenue", "subject_type": "TABLE", "subject_id": str(customers.id)},
            ],
        },
        created_by="author",
    )
    session.add(first)
    await session.commit()

    async def load(organization_id: UUID) -> dict[str, ResolvedMeaningCoverage]:
        resolved = await load_pinned_meaning(
            session,
            organization_id,
            ontology_version_ids=[first.id],
            semantic_model_version_ids=[model.id],
            glossary_term_version_ids=[definition.id],
            scope_table_ids=[estate.orders.id],
            scope_routine_ids=[],
        )
        return {item.kind: item for item in resolved}

    pinned = await load(estate.org.id)
    orders_only = (str(estate.orders.id),)
    assert (pinned["SEMANTIC_MODEL"].current, pinned["SEMANTIC_MODEL"].table_ids) == (
        True,
        orders_only,
    )
    assert (pinned["GLOSSARY_TERM"].key, pinned["GLOSSARY_TERM"].table_ids) == (
        "net_revenue",
        orders_only,
    )
    # Pinned to version 1 while the ontology has published version 2: it no longer stands. The
    # column mapping reaches the product through its table; the customers mapping does not.
    assert (pinned["ONTOLOGY"].current, pinned["ONTOLOGY"].table_ids) == (False, orders_only)
    # Not by id and not by count: the out-of-scope table is nowhere in what was resolved.
    assert str(customers.id) not in repr(pinned)

    model.status = "SUPERSEDED"
    term.lifecycle_status = "DEPRECATED"
    await session.commit()
    moved = await load(estate.org.id)
    assert (moved["SEMANTIC_MODEL"].current, moved["GLOSSARY_TERM"].current) == (False, False)
    assert await load(uuid4()) == {}


async def test_changes_since_publication_are_the_covered_ones_net_and_never_before(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    customers = await _customers(estate)
    routine = await _routine(estate, "rebuild_totals")
    elsewhere = await _routine(estate, "unrelated")
    amount = await _column(estate, estate.orders, "amount")
    # Before publication: none of this is a change *since* it.
    await _describe_table(estate, estate.orders, "Orders placed online.")
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "STRUCTURAL")
    restored_first = await _describe_table(estate, customers, "Customers of the bank.")
    await publish_column_description(
        session,
        organization_id=estate.org.id,
        table_id=estate.orders.id,
        column_id=amount.id,
        description="Gross amount.",
        created_by="steward-1",
        approved_by="reviewer-1",
        approved_at=datetime.now(UTC),
    )
    await publish_routine_documentation_version(
        session,
        organization_id=estate.org.id,
        datasource_id=estate.datasource.id,
        routine_id=routine.id,
        description="Rebuilds totals.",
        created_by="steward-1",
        approved_by="reviewer-1",
        approved_at=datetime.now(UTC),
    )
    await session.commit()
    since = datetime.now(UTC)
    tables = [estate.orders.id, estate.view.id, customers.id]
    assert (
        await load_coverage_changes(session, estate.org.id, tables, [routine.id], since=since) == []
    )

    # After it: the view moves literal-only and then structurally, the routine literal-only, a
    # routine the product does not cover structurally; the orders description is replaced, the
    # column's withdrawn, the routine's re-approved word for word, and the customers description
    # replaced and then restored.
    later = since + timedelta(seconds=1)
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "LITERAL_ONLY", later)
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "STRUCTURAL", later)
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "LITERAL_ONLY", later)
    _signal(estate, "ROUTINE", routine.id, "DEFINITION_CHANGED", "LITERAL_ONLY", later)
    _signal(estate, "ROUTINE", elsewhere.id, "DEFINITION_CHANGED", "STRUCTURAL", later)
    await session.commit()
    await _describe_table(estate, estate.orders, "Orders from every channel.")
    column_description = await session.scalar(
        select(ColumnDocumentationVersion).where(ColumnDocumentationVersion.status == "APPROVED")
    )
    assert column_description is not None
    column_description.status = "WITHDRAWN"
    column_description.updated_at = datetime.now(UTC)
    await publish_routine_documentation_version(
        session,
        organization_id=estate.org.id,
        datasource_id=estate.datasource.id,
        routine_id=routine.id,
        description="Rebuilds totals.",
        created_by="steward-1",
        approved_by="reviewer-2",
        approved_at=datetime.now(UTC),
    )
    await session.commit()
    await _describe_table(estate, customers, "Former customers too.")
    await _describe_table(estate, customers, "Customers of the bank.")
    restored = await session.get(AssetDocumentationVersion, restored_first)
    assert restored is not None and restored.status == "SUPERSEDED"

    changes = await load_coverage_changes(session, estate.org.id, tables, [routine.id], since=since)

    assert [(c.subject_kind, c.subject_id, c.change, c.change_class) for c in changes] == sorted(
        [
            ("VIEW", str(estate.view.id), "DEFINITION_CHANGED", "STRUCTURAL"),
            ("ROUTINE", str(routine.id), "DEFINITION_CHANGED", "LITERAL_ONLY"),
            ("TABLE", str(estate.orders.id), "MEANING_RETIRED", "MEANING_REPLACED"),
            ("COLUMN", str(amount.id), "MEANING_RETIRED", "MEANING_WITHDRAWN"),
        ]
    )
    # Nothing outside the scope, nothing for a version never published, nothing for a stranger.
    assert str(elsewhere.id) not in repr(changes)
    assert (
        await load_coverage_changes(session, estate.org.id, tables, [routine.id], since=None) == []
    )
    assert await load_coverage_changes(session, uuid4(), tables, [routine.id], since=since) == []


async def test_every_door_renders_one_coverage_and_a_literal_only_move_is_drift(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    model = SemanticModelVersion(
        organization_id=estate.org.id,
        project_id=estate.project.id,
        version=1,
        name="Revenue model",
        change_summary="First.",
        status="PUBLISHED",
        created_by="modeller",
    )
    session.add(model)
    await session.commit()
    described = await _describe_table(estate, estate.orders, "Orders placed online.")
    product = await _publish_product(
        estate,
        "orders-coverage",
        table_ids=[estate.orders.id, estate.view.id],
        semantic_model_version_ids=[model.id],
    )
    admin = human(estate.org, "platform-admin", frozenset({"PlatformAdmin", "Analyst"}))

    async def compiled(target: str) -> dict[str, Any]:
        result = await compile_context_product_version(
            product.id, target=target, context=admin, session=session
        )
        return (yaml.safe_load if target == "YAML" else json.loads)(result.content)

    deployed = await compile_context_product_version(
        product.id, target="MCP", context=admin, session=session
    )
    before = json.loads(deployed.content)["context"]["coverage"]
    assert "changed_since_published" not in before
    assert [entry["kind"] for entry in before["meaning"]] == ["SEMANTIC_MODEL"]

    # The source changes only a literal in the view (the stored, value-free text -- and so the
    # digest -- is identical), and a steward withdraws the orders description.
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "LITERAL_ONLY")
    await session.commit()
    await _withdraw(estate, described)

    mcp_compiled = (await compiled("MCP"))["context"]["coverage"]
    rest = (await compiled("REST"))["context"]["coverage"]
    spec = (await compiled("YAML"))["spec"]["coverage"]
    download = await download_context_compilation(
        product.id, target="YAML", context=admin, session=session
    )
    downloaded = yaml.safe_load(download.body)["spec"]["coverage"]
    reader = SecurityContext(
        principal_id="analyst-1",
        principal_type="USER",
        organization_id=estate.org.id,
        roles=frozenset({"PlatformAdmin", "Analyst"}),
    )
    read = await _read_context_product_resource(
        "atlas://context-products/orders-coverage/versions/1", session, reader, "corr-coverage"
    )
    mcp_read = json.loads(read["contents"][0]["text"])["coverage"]

    assert mcp_compiled == rest == spec == downloaded == mcp_read
    assert mcp_read["changed_since_published"] == [
        {
            "subject_kind": "TABLE",
            "subject_id": str(estate.orders.id),
            "change": "MEANING_RETIRED",
            "change_class": "MEANING_WITHDRAWN",
        },
        {
            "subject_kind": "VIEW",
            "subject_id": str(estate.view.id),
            "change": "DEFINITION_CHANGED",
            "change_class": "LITERAL_ONLY",
        },
    ]
    # Every digest reads the same as at deployment, and the deployment is drifted anyway.
    assert mcp_read["views"] == before["views"]
    drift = await inspect_context_compilation_drift(
        product.id,
        ContextCompilationDriftRequest(target="MCP", deployed_content=deployed.content),
        context=admin,
        session=session,
    )
    assert drift.drifted
    assert "$.context.coverage.changed_since_published" in drift.changed_paths


# --- the rebuild ----------------------------------------------------------------------------


async def _pending_product_reviews(estate: Estate) -> list[GovernanceReview]:
    return list(
        await estate.session.scalars(
            select(GovernanceReview).where(
                GovernanceReview.requested_by == CONTEXT_REBUILD_PRINCIPAL,
                GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
                GovernanceReview.status == "PENDING",
            )
        )
    )


async def _versions(estate: Estate, product: ContextProductVersion) -> list[ContextProductVersion]:
    return list(
        await estate.session.scalars(
            select(ContextProductVersion)
            .where(ContextProductVersion.product_id == product.product_id)
            .order_by(ContextProductVersion.version)
            .execution_options(populate_existing=True)
        )
    )


async def test_a_product_over_a_redefined_view_goes_back_to_review_and_is_never_published(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    product = await _publish_product(estate, "revenue-view", table_ids=[estate.view.id])
    await _redefine(estate)

    first = await _rebuild(estate)

    assert first.products_drafted == 1, first.as_details()
    published, drafted = await _versions(estate, product)
    # The published version keeps serving; the draft is the same definition, in review.
    assert (published.status, published.id) == ("PUBLISHED", product.id)
    assert (drafted.status, drafted.created_by, drafted.based_on_version_id) == (
        "REVIEW_REQUIRED",
        CONTEXT_REBUILD_PRINCIPAL,
        product.id,
    )
    assert (drafted.table_ids, drafted.approved_by) == (published.table_ids, None)
    assert len(await _pending_product_reviews(estate)) == 1
    # And the view's hold waits for a person to re-verify the product, like a hand-written tool.
    assert first.waiting == {WAIT_PRODUCT_NOT_REVERIFIED: 1}
    assert (await _hold(estate, estate.view)).status == "OPEN"

    again = await _rebuild(estate)
    assert again.products_drafted == 0
    assert (
        await session.scalar(
            select(func.count())
            .select_from(ContextProductVersion)
            .where(
                ContextProductVersion.product_id == product.product_id,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
        == 1
    )

    (review,) = await _pending_product_reviews(estate)
    await _approve(estate, review.id)
    released = await _rebuild(estate)
    assert released.holds_released == 1, released.as_details()
    current = [v for v in await _versions(estate, product) if v.status == "PUBLISHED"]
    assert [v.id for v in current] == [drafted.id]
    assert (
        await load_coverage_changes(
            session,
            estate.org.id,
            drafted.table_ids,
            [],
            since=publication_time(drafted),
        )
        == []
    )


async def test_retired_meaning_redrafts_a_product_once_and_a_refusal_is_not_repeated(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    described = await _describe_table(estate, estate.orders, "Orders placed online.")
    product = await _publish_product(
        estate, "orders-meaning", table_ids=[estate.orders.id, estate.view.id]
    )
    assert (await _rebuild(estate)).products_drafted == 0

    await _withdraw(estate, described)
    first = await _rebuild(estate)
    assert (first.products_drafted, first.meaning_signals_recorded) == (1, 1), first.as_details()

    assert await _reject_rebuilt(estate, "CONTEXT_PRODUCT_VERSION") == 1
    refused = await _rebuild(estate)
    assert refused.products_drafted == 0, refused.as_details()
    # Refused, not forgotten: every door still says the published version is stale.
    stale = await load_coverage_changes(
        session, estate.org.id, product.table_ids, [], since=publication_time(product)
    )
    assert [(c.subject_kind, c.change_class) for c in stale] == [("TABLE", "MEANING_WITHDRAWN")]

    # Something else moves under the product -- the view it covers is redefined: it is proposed
    # again, once. (A description first approved after the refusal would not do it: new meaning
    # where there was none is not meaning the product was published over.)
    _signal(estate, "VIEW", estate.view.id, "DEFINITION_CHANGED", "STRUCTURAL")
    await session.commit()
    assert (await _rebuild(estate)).products_drafted == 1
    assert (await _rebuild(estate)).products_drafted == 0


async def test_a_covered_routine_the_source_retired_is_followed_or_dropped(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    renamed = await _routine(estate, "score")
    gone = await _routine(estate, "legacy_load")
    await session.commit()
    product = await _publish_product(
        estate,
        "orders-routines",
        table_ids=[estate.orders.id],
        routine_ids=[renamed.id, gone.id],
    )

    # One new signature replaced `score`'s one signature; `legacy_load` simply left.
    successor = await _routine(estate, "score_v2")
    renamed.status = "DEPRECATED"
    gone.status = "DEPRECATED"
    _signal(
        estate, "ROUTINE", renamed.id, "DEPRECATED", "SIGNATURE_CHANGED"
    ).related_subject_id = successor.id
    _signal(estate, "ROUTINE", gone.id, "DEPRECATED", None)
    await session.commit()

    outcome = await _rebuild(estate)

    assert (outcome.products_drafted, outcome.blocked) == (1, {}), outcome.as_details()
    drafted = (await _versions(estate, product))[-1]
    assert drafted.routine_ids == [str(successor.id)]
    assert drafted.status == "REVIEW_REQUIRED"
