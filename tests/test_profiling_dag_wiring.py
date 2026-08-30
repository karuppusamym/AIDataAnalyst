"""Wiring PR-3 into the actual analysis DAG (`aida.workflows.activities`).

One internal seam is proven here against real behaviour, not just read:

* `_get_or_create_column` -- discovery-time classification -- writes a
  `classification_evidence` row for every rule-based decision, and never lets
  rediscovery's rule inference silently overwrite a column an authoritative
  feed already spoke for (module 05 sec 9 exit condition, PR-3).

PR-1 (composite key inference) is covered separately by
`tests/test_composite_key_inference.py` and `tests/test_composite_key_api.py`
against the already-merged `aida.composite_key_inference`/`composite_key_api`
implementation -- this module no longer duplicates that coverage.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from aida.classification_feed import CLASSIFICATION_SOURCE_EXTERNAL, CLASSIFICATION_SOURCE_RULE
from aida.connectors.base import DiscoveredColumn
from aida.models import ClassificationEvidence, DataSource, MetadataColumn, MetadataTable
from aida.workflows.activities import ChangeTracker, _get_or_create_column

# ---------------------------------------------------------------------------
# _get_or_create_column: rule-classification evidence + external-feed protection
# ---------------------------------------------------------------------------


@dataclass
class _EvidenceSession:
    """Mirrors the lookup-then-mutate shape `_get_or_create_column` uses:
    one `scalar()` to find (or miss) the existing column, then `execute()`
    (superseding prior evidence) and `add()` for whatever it writes."""

    existing: Any | None = None
    added: list[Any] = field(default_factory=list)
    executed: list[Any] = field(default_factory=list)

    async def scalar(self, _statement: object) -> Any | None:
        return self.existing

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)


def _sample_datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        name="Warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )


def _discovered(name: str) -> DiscoveredColumn:
    return DiscoveredColumn(name=name, ordinal_position=1, physical_type="varchar", nullable=True)


async def test_new_column_gets_rule_classification_and_evidence() -> None:
    datasource = _sample_datasource()
    table = MetadataTable(id=uuid4(), organization_id=datasource.organization_id)
    session = _EvidenceSession(existing=None)
    tracker = ChangeTracker()

    column = await _get_or_create_column(
        session, datasource, table, _discovered("customer_email_address"), tracker
    )

    assert column.classification == "PII"
    assert column.classification_source == CLASSIFICATION_SOURCE_RULE
    assert len(session.added) == 2  # the column itself, plus one evidence row
    evidence = next(item for item in session.added if isinstance(item, ClassificationEvidence))
    assert evidence.classification == "PII"
    assert evidence.source_type == CLASSIFICATION_SOURCE_RULE
    assert evidence.rule_id == "NAME_PATTERN_PII_V1"
    assert evidence.is_current is True
    assert evidence.column_id == column.id
    assert evidence.matched_signal["actual_values_inspected"] is False


async def test_rediscovery_refines_an_unclassified_column() -> None:
    datasource = _sample_datasource()
    table = MetadataTable(id=uuid4(), organization_id=datasource.organization_id)
    existing = MetadataColumn(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=table.id,
        name="tax_id",
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        classification="UNCLASSIFIED",
        classification_source=CLASSIFICATION_SOURCE_RULE,
        fingerprint="old",
    )
    session = _EvidenceSession(existing=existing)
    tracker = ChangeTracker()

    column = await _get_or_create_column(session, datasource, table, _discovered("tax_id"), tracker)

    assert column is existing
    assert column.classification == "PII"
    evidence_rows = [item for item in session.added if isinstance(item, ClassificationEvidence)]
    assert len(evidence_rows) == 1
    assert evidence_rows[0].rule_id == "NAME_PATTERN_PII_V1"


async def test_rediscovery_never_overwrites_an_externally_authoritative_column() -> None:
    """module 05 sec 9 exit condition: once EXTERNAL_AUTHORITATIVE, rediscovery
    must never let rule-based inference silently reclassify the column again --
    even in the edge case where the feed's own classification was UNCLASSIFIED."""
    datasource = _sample_datasource()
    table = MetadataTable(id=uuid4(), organization_id=datasource.organization_id)
    existing = MetadataColumn(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=table.id,
        name="customer_email_address",
        ordinal_position=1,
        physical_type="varchar",
        nullable=True,
        classification="UNCLASSIFIED",
        classification_source=CLASSIFICATION_SOURCE_EXTERNAL,
        fingerprint="old",
    )
    session = _EvidenceSession(existing=existing)
    tracker = ChangeTracker()

    column = await _get_or_create_column(
        session, datasource, table, _discovered("customer_email_address"), tracker
    )

    assert column.classification == "UNCLASSIFIED"
    assert column.classification_source == CLASSIFICATION_SOURCE_EXTERNAL
    assert session.added == []  # no evidence row appended; nothing was re-decided


