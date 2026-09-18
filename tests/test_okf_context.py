"""R11-OKF02 consumption: a bundle split so a reader can take what a question needs, and the
selection that takes it (design §14 steps 5-6, acceptance OKF-E).

What this module proves:

* **Wide objects are split, stably.** A table wider than `MAX_COLUMNS_PER_DOCUMENT` is its own
  document plus column sets that each hold at most that many rows; every name stays findable in
  the object's document; appending or dropping a column rewrites only the set it falls in; and
  an incremental rebuild of a split object is byte-identical to a full export (OKF-C).
* **A question gets the sections it needs, with receipts.** A business phrase reaches its table
  through the concept's alias, a column question reads one column set and only its matching
  rows, a lineage question follows the dependency one hop, a figure question is pointed at the
  approved tool, and a question the bundle cannot answer is `NO_MATCH` rather than a guess.
  Budgets keep meaning first and list what they cut. Selection is deterministic, and every line
  handed out is a line of the stored document.
* **Every door reads the one store.** REST, the MCP knowledge tool and Ask all select from the
  caller's own stored publication, so a revoked grant removes a source from context exactly as
  it does from a download. Audit records name sections, never the question.
* **Ask gives the model the product's knowledge (OKF-E).** Asked through a context product, the
  generation payload carries the selected sections and the run keeps their receipts.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import tests.test_f01_context_product_execution_boundary as f01
from aida.agent_orchestrator import AMBIGUOUS_KNOWLEDGE, AgentClarificationRequired
from aida.config import Settings
from aida.db import Base
from aida.mcp_server import _handle_tools_call, _handle_tools_list
from aida.models import (
    AgentRun,
    AuditEvent,
    ContextProductConsumptionEdge,
    CrossBoundaryGrant,
    GovernanceReview,
)
from aida.okf_context import (
    GUIDANCE,
    MAX_HOP_DOCUMENTS,
    MAX_SUBJECTS,
    OMITTED_BUDGET,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    hop_targets,
    parse_document,
    plan_context,
    render_markdown,
    select_context,
)
from aida.okf_export import (
    DESCRIPTION_APPROVED,
    MAX_COLUMNS_PER_DOCUMENT,
    TYPE_COLUMN_SET,
    OkfApproval,
    OkfColumnFacts,
    OkfConceptFacts,
    OkfDescription,
    OkfLink,
    OkfObjectFacts,
    OkfPolicyPartition,
    OkfPublicationStamp,
    OkfRoutineFacts,
    OkfSchemaFacts,
    OkfScope,
    OkfSnapshot,
    OkfSourceFacts,
    OkfToolFacts,
    OkfToolInput,
    concept_key,
    document_subjects,
    export_okf_bundle,
    export_okf_bundle_incremental,
    object_key,
    routine_key,
    schema_key,
    source_key,
    tool_version_key,
    validate_atlas_publish_policy,
    validate_okf_conformance,
)
from aida.okf_export_api import select_okf_context
from aida.okf_store import load_documents, read_okf_context, read_published_bundle
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.schemas import OkfContextRequest
from tests.support.app_surface import reaches_call
from tests.test_f01_context_product_execution_boundary import (  # noqa: F401 -- fixtures
    _no_real_credentials,
    scenario,
)
from tests.test_okf_export import _context, _estate, _product

REPO_ROOT = Path(__file__).resolve().parents[1]
DS = "22222222-2222-2222-2222-222222222222"
_AT = "2026-09-10T00:00:00+00:00"


# --- a banking snapshot: narrow and wide tables, a routine, a concept with an alias, a tool --


def _approved(text: str, version: int = 1) -> OkfDescription:
    return OkfDescription(
        state=DESCRIPTION_APPROVED,
        text=text,
        version=version,
        approval=OkfApproval("steward", _AT, True),
    )


def _column(
    name: str, ordinal: int, text: str | None = None, physical_type: str = "text"
) -> OkfColumnFacts:
    return OkfColumnFacts(
        name=name,
        ordinal=ordinal,
        physical_type=physical_type,
        nullable=True,
        classification="INTERNAL",
        lifecycle="ACTIVE",
        description=_approved(text) if text else OkfDescription(),
    )


def _customer_columns(count: int = 250) -> tuple[OkfColumnFacts, ...]:
    named = {
        5: ("customer_id", "The customer's identifier."),
        180: ("email_address", "The customer's email address."),
    }
    return tuple(
        _column(named[ordinal][0], ordinal, named[ordinal][1])
        if ordinal in named
        else _column(f"attr_{ordinal:03d}", ordinal)
        for ordinal in range(1, count + 1)
    )


def _keys() -> dict[str, str]:
    return {
        "source": source_key(DS),
        "schema": schema_key(DS, "bank", "warehouse"),
        "balances": object_key(DS, "bank", "warehouse", "fact_account_balances"),
        "customers": object_key(DS, "bank", "warehouse", "dim_customer"),
        "payments": object_key(DS, "bank", "warehouse", "fact_payments"),
        "rollup": routine_key(DS, "bank", "warehouse", "", "nightly_settlement_rollup", "()"),
        "concept": concept_key("banking", 1, "end_of_day_position"),
        "tool": tool_version_key("project-1", "daily_balance", 1),
    }


def _bank(**changes: Any) -> OkfSnapshot:
    key = _keys()
    values: dict[str, Any] = {
        "captured_at": "2026-09-18T09:00:00+00:00",
        "scope": OkfScope(
            kind="CONTEXT_PRODUCT",
            organization_id="org-1",
            policy_partition=OkfPolicyPartition(
                allowed_consumer_roles=("Analyst",),
                source_values="GATEWAY_ONLY",
                classifications=("INTERNAL",),
            ),
            product_key="banking_context",
            product_version=1,
        ),
        "sources": (
            OkfSourceFacts(
                key=key["source"],
                name="bank",
                dialect="postgres",
                connector_type="postgres",
                environment="PROD",
                lifecycle="ACTIVE",
            ),
        ),
        "schemas": (
            OkfSchemaFacts(
                key=key["schema"],
                name="warehouse",
                catalog_name="bank",
                qualified_name="bank.warehouse",
                source_key=key["source"],
                lifecycle="ACTIVE",
            ),
        ),
        "objects": (
            OkfObjectFacts(
                key=key["balances"],
                kind="TABLE",
                native_object_type="BASE_TABLE",
                name="fact_account_balances",
                qualified_name="bank.warehouse.fact_account_balances",
                schema_key=key["schema"],
                source_key=key["source"],
                lifecycle="ACTIVE",
                columns=(
                    _column("account_id", 1, "The account the balance belongs to.", "uuid"),
                    _column("balance_date", 2, "The business day the balance closes.", "date"),
                    _column(
                        "closing_balance",
                        3,
                        "The account's balance at the end of the business day.",
                        "numeric",
                    ),
                ),
                description=_approved(
                    "One row per account per business day, holding the day's closing balance."
                ),
                links=(OkfLink(target_key=key["rollup"], relation="written by"),),
            ),
            OkfObjectFacts(
                key=key["customers"],
                kind="TABLE",
                native_object_type="BASE_TABLE",
                name="dim_customer",
                qualified_name="bank.warehouse.dim_customer",
                schema_key=key["schema"],
                source_key=key["source"],
                lifecycle="ACTIVE",
                columns=_customer_columns(),
                description=_approved("One row per retail customer."),
            ),
            OkfObjectFacts(
                key=key["payments"],
                kind="TABLE",
                native_object_type="BASE_TABLE",
                name="fact_payments",
                qualified_name="bank.warehouse.fact_payments",
                schema_key=key["schema"],
                source_key=key["source"],
                lifecycle="ACTIVE",
                columns=(_column("payment_id", 1, "The payment's identifier.", "uuid"),),
                description=_approved("One row per settled payment."),
            ),
        ),
        "routines": (
            OkfRoutineFacts(
                key=key["rollup"],
                name="nightly_settlement_rollup",
                package_name="",
                signature="()",
                qualified_name="bank.warehouse.nightly_settlement_rollup",
                routine_type="PROCEDURE",
                schema_key=key["schema"],
                source_key=key["source"],
                lifecycle="ACTIVE",
                description=_approved(
                    "Rolls the day's settled payments into each account's closing balance."
                ),
                links=(
                    OkfLink(target_key=key["balances"], relation="writes"),
                    OkfLink(target_key=key["payments"], relation="reads"),
                ),
            ),
        ),
        "concepts": (
            OkfConceptFacts(
                key=key["concept"],
                name="end_of_day_position",
                ontology_key="banking",
                ontology_version=1,
                lifecycle="APPROVED",
                label="End of day position",
                definition="An account's balance at the close of a business day.",
                mapped_object_keys=(key["balances"],),
                approval=OkfApproval("steward", _AT, True),
                aliases=("closing position",),
            ),
        ),
        "tools": (
            OkfToolFacts(
                key=key["tool"],
                tool_version_id="tool-1",
                slug="daily_balance",
                name="Daily balance",
                version=1,
                lifecycle="PUBLISHED",
                source_key=key["source"],
                fingerprint="t" * 64,
                inputs=(
                    OkfToolInput("account_id", "uuid", True),
                    OkfToolInput("business_day", "date", True),
                ),
                description=_approved(
                    "Returns one account's balance for one business day, through Atlas."
                ),
            ),
        ),
    }
    values.update(changes)
    return OkfSnapshot(**values)


def _documents(snapshot: OkfSnapshot) -> dict[str, tuple[str, str]]:
    return {
        document.path: (document.text, document.sha256)
        for document in export_okf_bundle(snapshot).documents
    }


def _path(snapshot: OkfSnapshot, key: str) -> str:
    return next(path for path, subject in document_subjects(snapshot).items() if subject == key)


def _frontmatter(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(loaded, dict)
    return loaded


def _schema_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("| `")]


# --- splitting ---------------------------------------------------------------------------


def test_a_wide_table_is_its_own_document_plus_column_sets_within_the_limit() -> None:
    snapshot = _bank()
    bundle = export_okf_bundle(snapshot)
    documents = {document.path: document.text for document in bundle.documents}
    parent = _path(snapshot, _keys()["customers"])
    stem = parent.removesuffix(".md")
    sets = sorted(path for path in documents if path.startswith(f"{stem}-columns-"))
    assert [path.removeprefix(f"{stem}-columns-") for path in sets] == ["1.md", "2.md", "3.md"]
    rows = [_schema_rows(documents[path]) for path in sets]
    assert [len(item) for item in rows] == [100, 100, 50]
    assert all(len(item) <= MAX_COLUMNS_PER_DOCUMENT for item in rows)
    for number, path in enumerate(sets, start=1):
        meta = _frontmatter(documents[path])
        assert meta["type"] == TYPE_COLUMN_SET
        assert meta["atlas"]["part_of"]["key"] == _keys()["customers"]
        assert meta["atlas"]["part_of"]["set"] == number
        assert meta["atlas"]["part_of"]["sets"] == 3
        # Self-contained: which object, which part, and the way back.
        assert f"({'/' + parent})" in documents[path]
    own = documents[parent]
    assert _schema_rows(own) == []
    for column in snapshot.objects[1].columns:
        assert f"`{column.name}`" in own
    assert [item["path"] for item in _frontmatter(own)["atlas"]["column_sets"]] == sets
    # The object's own document stays the one subject document; its sets are not subjects.
    assert parent in document_subjects(snapshot)
    assert not set(sets) & set(document_subjects(snapshot))
    assert validate_okf_conformance(documents).valid
    assert validate_atlas_publish_policy(bundle).valid, validate_atlas_publish_policy(bundle)


def test_a_table_at_the_limit_stays_one_document() -> None:
    customers = replace(
        _bank().objects[1], columns=_customer_columns(MAX_COLUMNS_PER_DOCUMENT)
    )
    snapshot = _bank(objects=(_bank().objects[0], customers, *_bank().objects[2:]))
    documents = {document.path: document.text for document in export_okf_bundle(snapshot).documents}
    parent = _path(snapshot, _keys()["customers"])
    assert not [path for path in documents if "-columns-" in path]
    assert len(_schema_rows(documents[parent])) == MAX_COLUMNS_PER_DOCUMENT
    assert "column_sets" not in _frontmatter(documents[parent])["atlas"]


def _with_customer_columns(columns: tuple[OkfColumnFacts, ...]) -> OkfSnapshot:
    base = _bank()
    customers = replace(base.objects[1], columns=columns)
    return _bank(objects=(base.objects[0], customers, *base.objects[2:]))


def test_an_appended_or_dropped_column_rewrites_only_the_set_it_falls_in() -> None:
    before = {d.path: d.text for d in export_okf_bundle(_bank()).documents}
    stem = _path(_bank(), _keys()["customers"]).removesuffix(".md")
    appended = _with_customer_columns((*_customer_columns(), _column("loyalty_tier", 251)))
    after = {d.path: d.text for d in export_okf_bundle(appended).documents}
    assert after[f"{stem}-columns-1.md"] == before[f"{stem}-columns-1.md"]
    assert after[f"{stem}-columns-2.md"] == before[f"{stem}-columns-2.md"]
    assert after[f"{stem}-columns-3.md"] != before[f"{stem}-columns-3.md"]

    dropped = _with_customer_columns(
        tuple(column for column in _customer_columns() if column.ordinal != 50)
    )
    after = {d.path: d.text for d in export_okf_bundle(dropped).documents}
    assert after[f"{stem}-columns-1.md"] != before[f"{stem}-columns-1.md"]
    assert after[f"{stem}-columns-2.md"] == before[f"{stem}-columns-2.md"]
    assert after[f"{stem}-columns-3.md"] == before[f"{stem}-columns-3.md"]


def test_ordinals_the_source_did_not_report_still_bound_every_set() -> None:
    unordered = tuple(
        replace(column, ordinal=0) for column in _customer_columns(150)
    )
    bundle = export_okf_bundle(_with_customer_columns(unordered))
    documents = {d.path: d.text for d in bundle.documents}
    stem = _path(_bank(), _keys()["customers"]).removesuffix(".md")
    sets = sorted(path for path in documents if path.startswith(f"{stem}-columns-"))
    assert [path.removeprefix(f"{stem}-columns-") for path in sets] == ["1-1.md", "1-2.md"]
    assert [len(_schema_rows(documents[path])) for path in sets] == [100, 50]


def test_an_incremental_rebuild_of_a_split_object_matches_a_full_export() -> None:
    base = _bank()
    first, _report, history = export_okf_bundle_incremental(
        base,
        prior_snapshot=None,
        prior_documents={},
        stamp=OkfPublicationStamp("2026-09-18", 1, "INITIAL"),
    )
    prior = {document.path: document.text for document in first.documents}
    columns = list(_customer_columns())
    columns[179] = replace(columns[179], description=_approved("The customer's contact email."))
    changed = _with_customer_columns(tuple(columns))
    bundle, report, new_history = export_okf_bundle_incremental(
        changed,
        prior_snapshot=base,
        prior_documents=prior,
        prior_history=history,
        stamp=OkfPublicationStamp("2026-09-18", 2, "SOURCE_CHANGE"),
    )
    full = export_okf_bundle(changed, history=new_history)
    assert [(d.path, d.text) for d in bundle.documents] == [
        (d.path, d.text) for d in full.documents
    ]
    stem = _path(base, _keys()["customers"]).removesuffix(".md")
    changed_content = {path for path in report.changed if not path.endswith("log.md")}
    assert changed_content == {f"{stem}-columns-2.md"}
    # The other tables' documents were carried, never re-rendered.
    assert _path(base, _keys()["balances"]) in report.carried


def test_a_concept_prints_its_approved_aliases() -> None:
    snapshot = _bank()
    text = _documents(snapshot)[_path(snapshot, _keys()["concept"])][0]
    assert "# Also called\n\n* closing position\n" in text
    assert _frontmatter(text)["atlas"]["statements"]["approved"] == [
        "aliases",
        "definition",
        "mappings",
        "relations",
    ]


def test_the_column_set_extension_keys_are_documented() -> None:
    """The profile doc is the extension's contract (see `tests/test_okf_export.py`); the column
    set keys only appear on a wide object, so they are asserted here over one."""
    reference = (REPO_ROOT / "Docs" / "90-reference" / "okf-export-profile.md").read_text(
        encoding="utf-8"
    )
    emitted: set[str] = set()
    for document in export_okf_bundle(_bank()).documents:
        if not document.text.startswith("---\n"):
            continue
        extension = _frontmatter(document.text).get("atlas")
        if not isinstance(extension, dict):
            continue
        for key, value in extension.items():
            emitted.add(key)
            if isinstance(value, dict):
                emitted.update(f"{key}.{inner}" for inner in value)
    assert {"column_sets", "part_of", "part_of.set"} <= emitted
    assert sorted(key for key in emitted if f"`{key}`" not in reference) == []
    assert TYPE_COLUMN_SET in reference


# --- selection ---------------------------------------------------------------------------


def test_a_business_phrase_reaches_its_table_through_the_concepts_alias() -> None:
    snapshot = _bank()
    selected = select_context(
        snapshot, _documents(snapshot), "What was the closing position of an account yesterday?"
    )
    assert selected.status == STATUS_MATCHED
    concept = _path(snapshot, _keys()["concept"])
    balances = _path(snapshot, _keys()["balances"])
    assert selected.documents[0].path == concept
    assert selected.documents[0].hop == 0
    reached = next(item for item in selected.documents if item.path == balances)
    assert reached.linked_from == concept
    assert any(section.heading == "Purpose" for section in reached.sections)
    headings = [section.heading for section in selected.documents[0].sections]
    assert "Definition" in headings and "Also called" in headings


def test_a_column_question_reads_one_column_set_and_only_its_matching_rows() -> None:
    snapshot = _bank()
    documents = _documents(snapshot)
    question = "Which column holds the customer's email address?"
    plan = plan_context(snapshot, question)
    parent = _path(snapshot, _keys()["customers"])
    stem = parent.removesuffix(".md")
    candidate = next(item for item in plan.candidates if item.path == parent)
    assert candidate.column_set_paths == (f"{stem}-columns-2.md",)
    # Sets 1 and 3 are never loaded, not merely not returned.
    assert f"{stem}-columns-1.md" not in plan.paths
    assert f"{stem}-columns-3.md" not in plan.paths
    selected = select_context(snapshot, documents, question)
    chosen = next(item for item in selected.documents if item.path == f"{stem}-columns-2.md")
    schema = next(section for section in chosen.sections if section.heading == "Schema")
    assert (schema.rows_shown, schema.rows_total) == (1, 100)
    assert "`email_address`" in schema.text and "`attr_101`" not in schema.text
    # The names-only index of the parent is not handed out beside the set that answers.
    own = next(item for item in selected.documents if item.path == parent)
    assert "Schema" not in [section.heading for section in own.sections]


def test_where_a_table_comes_from_follows_its_dependency_to_the_routine() -> None:
    snapshot = _bank()
    selected = select_context(
        snapshot, _documents(snapshot), "Where does fact_account_balances come from?"
    )
    balances = _path(snapshot, _keys()["balances"])
    rollup = _path(snapshot, _keys()["rollup"])
    first = selected.documents[0]
    assert first.path == balances
    assert "Dependencies" in [section.heading for section in first.sections]
    routine = next(item for item in selected.documents if item.path == rollup)
    assert routine.hop == 1 and routine.linked_from == balances
    assert "settled payments" in " ".join(section.text for section in routine.sections)


def test_a_figure_question_is_pointed_at_the_approved_tool_and_carries_no_value() -> None:
    snapshot = _bank()
    selected = select_context(
        snapshot, _documents(snapshot), "What is the daily balance for this account?"
    )
    tool = _path(snapshot, _keys()["tool"])
    assert tool in [item.path for item in selected.documents]
    assert selected.tool_paths and tool in selected.tool_paths
    assert "approved Atlas tool" in GUIDANCE


def test_a_question_the_bundle_cannot_answer_is_no_match_not_a_guess() -> None:
    snapshot = _bank()
    selected = select_context(snapshot, _documents(snapshot), "Weather in Paris tomorrow?")
    assert selected.status == STATUS_NO_MATCH
    assert selected.documents == ()
    assert "No document in this knowledge bundle matches" in render_markdown(selected)


def test_a_tight_budget_keeps_meaning_first_and_lists_what_it_cut() -> None:
    snapshot = _bank()
    selected = select_context(
        snapshot,
        _documents(snapshot),
        "What was the closing position of an account yesterday?",
        max_chars=1_000,
    )
    assert selected.used_chars <= 1_000
    assert any(item.reason == OMITTED_BUDGET for item in selected.omitted)
    assert selected.omitted_count >= len(selected.omitted)
    # The concept's definition survives a budget that cuts the linked table's schema.
    assert selected.documents[0].sections[0].heading == "Definition"


def test_equal_matches_of_one_kind_are_flagged_ambiguous() -> None:
    base = _bank()
    archive = replace(
        base.objects[2],
        key=object_key(DS, "bank", "archive", "fact_payments"),
        qualified_name="bank.archive.fact_payments",
        schema_key=schema_key(DS, "bank", "archive"),
        description=OkfDescription(),
        columns=(),
    )
    live = replace(base.objects[2], description=OkfDescription(), columns=())
    snapshot = _bank(
        schemas=(
            *base.schemas,
            replace(
                base.schemas[0],
                key=schema_key(DS, "bank", "archive"),
                name="archive",
                qualified_name="bank.archive",
            ),
        ),
        objects=(base.objects[0], base.objects[1], live, archive),
        routines=(replace(base.routines[0], links=()),),
    )
    selected = select_context(snapshot, _documents(snapshot), "fact payments")
    assert set(selected.ambiguous) == {
        _path(snapshot, live.key),
        _path(snapshot, archive.key),
    }
    assert "Ambiguous:" in render_markdown(selected)


def test_a_name_the_question_gives_whole_outranks_one_it_only_half_gives() -> None:
    """`payments` names `fact_payments`' own words whole and `fact_payments_archive`'s only in
    part. Without the whole-name credit the two tied and read as ambiguous."""
    base = _bank()
    archive = replace(
        base.objects[2],
        key=object_key(DS, "bank", "warehouse", "fact_payments_archive"),
        name="fact_payments_archive",
        qualified_name="bank.warehouse.fact_payments_archive",
        description=OkfDescription(),
        columns=(),
    )
    live = replace(base.objects[2], description=OkfDescription(), columns=())
    snapshot = _bank(
        objects=(base.objects[0], base.objects[1], live, archive),
        routines=(replace(base.routines[0], links=()),),
    )
    selected = select_context(snapshot, _documents(snapshot), "fact payments")
    assert selected.ambiguous == ()
    assert selected.documents[0].path == _path(snapshot, live.key)


def test_selection_is_deterministic_and_hands_out_only_stored_lines() -> None:
    snapshot = _bank()
    documents = _documents(snapshot)
    question = "closing position and the customer's email address"
    first = select_context(snapshot, documents, question)
    assert first == select_context(snapshot, documents, question)
    for document in first.documents:
        text, sha = documents[document.path]
        assert document.sha256 == sha
        stored = set(text.splitlines())
        for section in document.sections:
            assert set(section.text.splitlines()) <= stored, (document.path, section.anchor)


def test_what_is_loaded_is_bounded_by_the_ranking_not_the_bundle() -> None:
    snapshot = _bank()
    documents = _documents(snapshot)
    plan = plan_context(snapshot, "closing position")
    loaded = {path: documents[path] for path in plan.paths if path in documents}
    hops = hop_targets(plan, loaded)
    assert len(plan.candidates) <= MAX_SUBJECTS
    assert len(hops) <= MAX_HOP_DOCUMENTS
    assert len(loaded) + len(hops) < len(documents)


def test_parsing_reads_frontmatter_and_ignores_navigation_links() -> None:
    snapshot = _bank()
    documents = _documents(snapshot)
    parent = _path(snapshot, _keys()["customers"])
    parsed = parse_document(parent, *documents[parent])
    # The schema index's links to the object's own sets are navigation, not knowledge hops.
    assert not [link for link in parsed.links if "-columns-" in link]
    assert parsed.approved == ("purpose",)
    assert parsed.sections[0].anchor == "purpose"


# --- the doors: one store, the caller's own publication -----------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


async def test_the_store_selects_from_the_callers_own_stored_publication(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    found = await read_okf_context(
        session, version.id, context, settings, "globally unique order identifier"
    )
    assert found.context.status == STATUS_MATCHED
    stored = await read_published_bundle(session, version.id, context, settings)
    assert found.stored.publication.id == stored.publication.id
    rows = {row.path: row for row in await load_documents(session, stored.publication)}
    first = found.context.documents[0]
    assert first.title.endswith("orders")
    assert first.sha256 == rows[first.path].sha256
    assert any("Globally unique order identifier." in s.text for s in first.sections)


async def test_a_revoked_grant_removes_its_source_from_context(
    session: AsyncSession, settings: Settings
) -> None:
    """OKF-D on the context door: the selection is from the caller's lineage, so once the grant
    is revoked the far source is neither returned nor linked to."""
    estate = await _estate(session)
    grant = CrossBoundaryGrant(
        id=uuid4(),
        organization_id=estate["organization"].id,
        source_data_domain_id=estate["far_domain"].id,
        target_data_domain_id=estate["domain"].id,
        reason="approved revenue-to-people crossing",
        status="ACTIVE",
        edge_kinds=[],
        requested_by="steward",
        approved_by="reviewer",
    )
    session.add(grant)
    await session.flush()
    _product_row, version = await _product(session, estate, include_far_source=True)
    context = _context(estate["organization"].id)
    granted = await read_okf_context(session, version.id, context, settings, "salaries")
    assert any(item.title.endswith("salaries") for item in granted.context.documents)

    grant.status = "REVOKED"
    await session.flush()
    revoked = await read_okf_context(session, version.id, context, settings, "salaries")
    assert not any("salaries" in item.title for item in revoked.context.documents)
    assert "salaries" not in render_markdown(revoked.context)


async def test_the_rest_route_audits_sections_and_never_the_question(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    _product_row, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    question = "globally unique order identifier ZZQ-QUESTION-TEXT"
    read = await select_okf_context(
        version.id, OkfContextRequest(question=question), context, session, settings
    )
    assert read.status == STATUS_MATCHED
    assert read.documents[0].citation == "K1"
    assert read.markdown.startswith("Knowledge from revenue_context v2")
    event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "context_product.okf_context_read")
    )
    assert event is not None
    details = event.details or {}
    assert details["section_count"] == len(details["sections"]) > 0
    assert "ZZQ-QUESTION-TEXT" not in json.dumps(details)
    edge = await session.scalar(
        select(ContextProductConsumptionEdge).where(
            ContextProductConsumptionEdge.channel == "OKF_CONTEXT"
        )
    )
    assert edge is not None


async def test_the_mcp_knowledge_tool_is_listed_and_answers_from_the_same_publication(
    session: AsyncSession, settings: Settings
) -> None:
    estate = await _estate(session)
    product, version = await _product(session, estate, include_far_source=False)
    context = _context(estate["organization"].id)
    listed = await _handle_tools_list(session, context)
    assert "atlas__get_knowledge_context" in [tool["name"] for tool in listed["tools"]]

    arguments = {
        "product_key": product.product_key,
        "version": version.version,
        "question": "globally unique order identifier",
    }
    result = await _handle_tools_call(
        {"name": "atlas__get_knowledge_context", "arguments": arguments},
        session,
        context,
        settings,
        "corr",
    )
    assert not result.get("isError"), result
    assert "Path: " in result["content"][0]["text"]
    structured = json.loads(
        result["content"][1]["text"].removeprefix("```json\n").removesuffix("\n```")
    )
    rest = await select_okf_context(
        version.id,
        OkfContextRequest(question="globally unique order identifier"),
        context,
        session,
        settings,
    )
    assert structured["publication"]["publication_id"] == str(rest.publication.publication_id)
    assert [item["path"] for item in structured["documents"]] == [
        item.path for item in rest.documents
    ]

    unknown = await _handle_tools_call(
        {"name": "atlas__get_knowledge_context", "arguments": {**arguments, "version": 99}},
        session,
        context,
        settings,
        "corr",
    )
    assert unknown["isError"] is True
    assert "not found or not accessible" in unknown["content"][0]["text"]
    malformed = await _handle_tools_call(
        {"name": "atlas__get_knowledge_context", "arguments": {**arguments, "question": ""}},
        session,
        context,
        settings,
        "corr",
    )
    assert malformed["isError"] is True


@pytest.mark.parametrize(
    ("module", "handler"),
    [
        ("aida.okf_export_api", "select_okf_context"),
        ("aida.mcp_server", "_handle_native_knowledge_tool_call"),
    ],
)
def test_every_context_door_reads_the_one_store(module: str, handler: str) -> None:
    assert reaches_call(module, handler, frozenset({"read_published_bundle"}))


# --- Ask: generation grounded in the product's knowledge (OKF-E) --------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """The F01 scenario's database, with the maker the OKF freeze's gate records through."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        active.info["maker"] = maker
        yield active
    await engine.dispose()


async def _ask_quietly(orchestrator: Any, built: Any, **kwargs: Any) -> None:
    """What happens after generation is F01's subject, not this module's: the model payload
    and the stored run are what is asserted, whatever the run's outcome."""
    with contextlib.suppress(Exception):
        await f01._ask(orchestrator, built, **kwargs)


async def test_an_ask_through_a_product_grounds_generation_in_its_knowledge(
    scenario: Any,  # noqa: F811 -- the F01 fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await scenario.product(table_ids=[scenario.orders.id])
    orchestrator, model = f01._orchestrator(
        monkeypatch, model_sql="SELECT o.order_id FROM retail.orders AS o"
    )
    await _ask_quietly(orchestrator, scenario)
    assert model is not None and model.calls
    run = await f01._latest_run(scenario)
    # Read the stored row, not the identity map: evidence added after the plan stage was
    # mutated in place and, until `RunLedger.publish_plan_evidence` flagged it, never written.
    stored, stored_trace = (
        await scenario.db.execute(
            select(AgentRun.plan_evidence, AgentRun.step_trace).where(AgentRun.id == run.id)
        )
    ).one()
    assert {"okf_context", "model_call_evidence", "budget_evidence"} <= set(stored)
    assert stored_trace == run.step_trace
    evidence = stored["okf_context"]
    assert evidence["used"] is True, evidence
    grounding = model.calls[0]["payload"]["okf_context"]
    assert grounding[0]["citation"] == "K1"
    assert grounding[0]["path"] == evidence["documents"][0]["path"]
    assert grounding[0]["title"].endswith("retail.orders")
    # The run keeps receipts -- publication, paths, digests, anchors -- and no section text.
    assert evidence["documents"][0]["sha256"]
    assert all(isinstance(anchor, str) for anchor in evidence["documents"][0]["sections"])
    assert "text" not in json.dumps(evidence["documents"])
    audit = await scenario.db.scalar(
        select(AuditEvent).where(AuditEvent.action == "agent.okf_context_read")
    )
    assert audit is not None and (audit.details or {}).get("sections")


async def test_an_ask_without_a_product_is_generated_exactly_as_before(
    scenario: Any,  # noqa: F811 -- the F01 fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, model = f01._orchestrator(
        monkeypatch, model_sql="SELECT o.order_id FROM retail.orders AS o"
    )
    await _ask_quietly(orchestrator, scenario, product_key=None)
    assert model is not None and model.calls
    assert "okf_context" not in model.calls[0]["payload"]
    run = await f01._latest_run(scenario)
    stored = await scenario.db.scalar(select(AgentRun.plan_evidence).where(AgentRun.id == run.id))
    assert "okf_context" not in (stored or {})
    assert "model_call_evidence" in (stored or {})


async def test_knowledge_that_matched_but_did_not_fit_is_not_grounding(
    scenario: Any,  # noqa: F811 -- the F01 fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every section over the budget: the model is not handed an empty `okf_context` and an
    instruction about it, and the run records that the match was not used."""
    await scenario.product(table_ids=[scenario.orders.id])
    orchestrator, model = f01._orchestrator(
        monkeypatch, model_sql="SELECT o.order_id FROM retail.orders AS o"
    )
    # Below the setting's own floor on purpose: `model_copy` does not validate, and a budget
    # nothing fits is the case under test.
    orchestrator.settings = orchestrator.settings.model_copy(
        update={"okf_context_ask_max_chars": 1}
    )
    await _ask_quietly(orchestrator, scenario)
    assert model is not None and model.calls
    assert "okf_context" not in model.calls[0]["payload"]
    run = await f01._latest_run(scenario)
    stored = await scenario.db.scalar(select(AgentRun.plan_evidence).where(AgentRun.id == run.id))
    evidence = (stored or {})["okf_context"]
    assert evidence["status"] == STATUS_MATCHED
    assert evidence["used"] is False and evidence["documents"] == []
    assert evidence["omitted_sections"] > 0


async def test_a_stored_concept_links_its_mapped_table_and_carries_its_approval(
    session: AsyncSession, settings: Settings
) -> None:
    """The loader delivers a concept's mappings as `mappings`, and an ontology definition's
    lifecycle is ACTIVE or DEPRECATED. The freeze read `table_ids` and the renderer wanted
    lifecycle APPROVED, so every real concept exported with no mapped table and as draft --
    and the hop from a business word to its table, the one this whole selection exists for,
    could not happen. Proven here through the database, not over hand-built facts."""
    estate = await _estate(session)
    organization = estate["organization"]
    orders = estate["tables"]["warehouse.orders"]
    head = OntologyHead(
        organization_id=organization.id, ontology_key="sales", last_version=1, published_version=1
    )
    session.add(head)
    await session.flush()
    ontology = OntologyVersion(
        organization_id=organization.id,
        ontology_id=head.id,
        version=1,
        base_version=0,
        status="APPROVED",
        definition={
            "name": "Sales",
            "concepts": [
                {
                    "key": "completed_sale",
                    "name": "Completed sale",
                    "description": "An order the customer has paid for.",
                    "aliases": ["closed deal"],
                }
            ],
            "mappings": [
                {"concept": "completed_sale", "subject_type": "TABLE", "subject_id": str(orders.id)}
            ],
        },
        created_by="author",
        approved_by="reviewer",
    )
    session.add(ontology)
    await session.flush()
    session.add(
        GovernanceReview(
            organization_id=organization.id,
            object_type="ONTOLOGY_VERSION",
            object_id=str(ontology.id),
            requested_action="PUBLISH",
            status="APPROVED",
            requested_by="author",
            decided_by="reviewer",
            decided_at=datetime(2026, 9, 12, tzinfo=UTC),
        )
    )
    _product_row, version = await _product(session, estate, include_far_source=False)
    version.ontology_version_ids = [str(ontology.id)]
    await session.flush()
    context = _context(organization.id)

    found = await read_okf_context(session, version.id, context, settings, "any closed deal today?")
    concept = found.context.documents[0]
    assert concept.type == "Atlas Business Concept"
    assert "Mapped objects" in [section.heading for section in concept.sections]
    assert concept.status == "stable" and "mappings" in concept.approved
    reached = next(item for item in found.context.documents if item.linked_from == concept.path)
    assert reached.title.endswith("orders")
    rows = {row.path: row for row in await load_documents(session, found.stored.publication)}
    assert "verified:" in rows[concept.path].content


async def test_knowledge_naming_two_tables_equally_asks_which_one_before_generating(
    scenario: Any,  # noqa: F811 -- the F01 fixture, imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design §14 step 6: ambiguity produces clarification. The product holds `retail.orders`
    and `staging.orders`, and "orders" names both identically: Ask asks which one, before any
    model is called, instead of letting the model choose silently."""
    await scenario.product(table_ids=[scenario.orders.id, scenario.staging_orders.id])
    orchestrator, model = f01._orchestrator(
        monkeypatch, model_sql="SELECT o.order_id FROM retail.orders AS o"
    )
    with pytest.raises(AgentClarificationRequired) as asked:
        await f01._ask(orchestrator, scenario)
    assert asked.value.code == AMBIGUOUS_KNOWLEDGE
    assert sorted(asked.value.candidates) == ["warehouse.retail.orders", "warehouse.staging.orders"]
    assert model is not None and model.calls == []
    run = await f01._latest_run(scenario)
    assert run.failure_reason == AMBIGUOUS_KNOWLEDGE
    stored = await scenario.db.scalar(select(AgentRun.plan_evidence).where(AgentRun.id == run.id))
    assert len((stored or {})["okf_context"]["ambiguous"]) == 2
