"""ST-15: the lineage ``edge_kind`` vocabulary is one agreed set, enforced at the DB level, and
matching the lineage contract.

Three things this locks down, matching ST-15's exit condition ("one agreed vocabulary, a DB-level
constraint enforcing it, and 30-contracts/06 matching"):

1. ``aida.models.LINEAGE_EDGE_KINDS`` is exactly the vocabulary documented in
   ``Docs/30-contracts/06-lineage-contract.md`` §2 -- neither drifts from the other.
2. The migration's own copy of the vocabulary is in lockstep with the model's.
3. Every one of the four lineage-edge tables rejects an out-of-vocabulary ``edge_kind`` at the
   database level (a real CHECK constraint, proven by an insert that must fail) and accepts an
   in-vocabulary one.

``SUGGESTED_RELATIONSHIP`` is used as the negative case on purpose: it is the value the ST-15
tracker row flagged, and it belongs to the *separate* relationship/grant edge-kind axis, not this
lineage vocabulary -- so it must be rejected here.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    LINEAGE_EDGE_KINDS,
    BiMetricColumnEdge,
    BiReportMetricEdge,
    OpenLineageColumnEdge,
    OpenLineageTableEdge,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "Docs" / "30-contracts" / "06-lineage-contract.md"
MIGRATION_PATH = (
    REPO_ROOT
    / "migrations"
    / "versions"
    / "d7b1e5a9c204_st15_lineage_edge_kind_vocabulary.py"
)


def test_model_vocabulary_matches_the_lineage_contract() -> None:
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    # There are two `"kind": "..."` lines: the node kind (TABLE | COLUMN | ...) and the edge kind.
    # Select the edge one by requiring a value that only appears in the edge vocabulary.
    candidates = re.findall(r'"kind":\s*"([^"]+)"', text)
    edge_lines = [c for c in candidates if "AI_DECISION" in c]
    assert edge_lines, "could not find the edge `kind` vocabulary line in the lineage contract"
    documented = {tok.strip() for tok in edge_lines[0].split("|")}
    assert documented == set(LINEAGE_EDGE_KINDS), (
        "aida.models.LINEAGE_EDGE_KINDS and Docs/30-contracts/06-lineage-contract.md have drifted:"
        f" model={sorted(LINEAGE_EDGE_KINDS)} contract={sorted(documented)}"
    )


def test_migration_vocabulary_is_in_lockstep_with_the_model() -> None:
    text = MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(r"_VOCAB\s*=\s*\(([^)]*)\)", text)
    assert match, "could not find _VOCAB in the ST-15 migration"
    migration_vocab = {v.strip().strip("'\"") for v in match.group(1).split(",") if v.strip()}
    assert migration_vocab == set(LINEAGE_EDGE_KINDS), (
        "migration d7b1e5a9c204 _VOCAB drifted from aida.models.LINEAGE_EDGE_KINDS"
    )


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


def _make_edge(cls, edge_kind: str):
    common = {"organization_id": uuid4(), "edge_kind": edge_kind}
    if cls is OpenLineageTableEdge:
        return cls(
            **common,
            run_event_id=uuid4(),
            input_dataset_namespace="ns",
            input_dataset_name="in_tbl",
            output_dataset_namespace="ns",
            output_dataset_name="out_tbl",
        )
    if cls is OpenLineageColumnEdge:
        return cls(
            **common,
            run_event_id=uuid4(),
            input_dataset_namespace="ns",
            input_dataset_name="in_tbl",
            input_column_name="in_col",
            output_dataset_namespace="ns",
            output_dataset_name="out_tbl",
            output_column_name="out_col",
        )
    if cls is BiReportMetricEdge:
        return cls(**common, artifact_import_id=uuid4(), report_id=uuid4(), metric_id=uuid4())
    if cls is BiMetricColumnEdge:
        return cls(
            **common,
            artifact_import_id=uuid4(),
            metric_id=uuid4(),
            source_table_name="tbl",
            source_column_name="col",
        )
    raise AssertionError(cls)


_EDGE_CLASSES = [
    OpenLineageTableEdge,
    OpenLineageColumnEdge,
    BiReportMetricEdge,
    BiMetricColumnEdge,
]


@pytest.mark.parametrize("cls", _EDGE_CLASSES)
async def test_out_of_vocabulary_edge_kind_is_rejected_at_the_db(session, cls) -> None:
    session.add(_make_edge(cls, "SUGGESTED_RELATIONSHIP"))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("cls", _EDGE_CLASSES)
@pytest.mark.parametrize("valid_kind", ["ETL", "BI", "QUERY", "AI_DECISION"])
async def test_in_vocabulary_edge_kind_is_accepted(session, cls, valid_kind) -> None:
    session.add(_make_edge(cls, valid_kind))
    await session.flush()  # must not raise
