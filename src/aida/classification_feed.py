"""Deterministic rule-based classification plus authoritative feed overrides.

Module 05 §9 requires deterministic classification with rule-ID evidence, and
lists an "authoritative external classification feed" as the highest-accuracy
target source that should *override* the rule-based inference rather than
merely supplement it. This module is the single source of truth for both:

- ``classify_column_name_with_evidence`` — the deterministic, value-free rule
  used at discovery time (``aida.workflows.activities.classify_column_name``
  delegates here so the rule set is defined exactly once).
- ``resolve_classification_conflict`` — the pure decision of what happens when
  an authoritative external record disagrees with (or reaffirms) whatever is
  currently on the column: the external record always wins.
- ``ingest_classification_feed`` — the DB-facing ingestion path that matches
  external ``{schema, table, column}`` records to ``MetadataColumn`` rows,
  applies the override, and appends a ``ClassificationEvidence`` row so the
  provenance of every classification (inferred vs. externally authoritative)
  stays inspectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    ClassificationEvidence,
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
)

CLASSIFICATION_SOURCE_RULE = "RULE"
CLASSIFICATION_SOURCE_EXTERNAL = "EXTERNAL_AUTHORITATIVE"

_PCI_TOKENS: tuple[str, ...] = ("card_number", "pan_number", "cvv")
_PII_TOKENS: tuple[str, ...] = (
    "email",
    "social_security",
    "ssn",
    "tax_id",
    "passport",
    "customer_name",
)


@dataclass(frozen=True, slots=True)
class RuleClassificationResult:
    classification: str
    rule_id: str
    matched_signal: dict[str, Any]


def classify_column_name_with_evidence(name: str) -> RuleClassificationResult:
    """The deterministic name-pattern rule, with the evidence the module spec
    requires (rule ID, matched signal) alongside the classification itself."""
    normalized = name.lower()
    for token in _PCI_TOKENS:
        if token in normalized:
            return RuleClassificationResult(
                classification="PCI",
                rule_id="NAME_PATTERN_PCI_V1",
                matched_signal={"matched_token": token},
            )
    for token in _PII_TOKENS:
        if token in normalized:
            return RuleClassificationResult(
                classification="PII",
                rule_id="NAME_PATTERN_PII_V1",
                matched_signal={"matched_token": token},
            )
    return RuleClassificationResult(
        classification="UNCLASSIFIED",
        rule_id="NAME_PATTERN_NONE_V1",
        matched_signal={"matched_token": None},
    )


@dataclass(frozen=True, slots=True)
class ExternalClassificationRecord:
    """One row of a bank's own authoritative data-classification feed."""

    schema_name: str
    table_name: str
    column_name: str
    classification: str
    source: str
    confidence: float | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ClassificationResolution:
    final_classification: str
    final_source: str
    changed: bool
    reason: str


def resolve_classification_conflict(
    *,
    existing_source: str,
    existing_classification: str,
    incoming: ExternalClassificationRecord,
) -> ClassificationResolution:
    """An authoritative feed record always overrides whatever is on the column
    today — deterministic rules included. This never inspects source values;
    it only compares already-known classification labels."""
    if existing_source == CLASSIFICATION_SOURCE_EXTERNAL:
        reason = (
            "EXTERNAL_FEED_REAFFIRMED"
            if existing_classification == incoming.classification
            else "EXTERNAL_FEED_UPDATED"
        )
    else:
        reason = "EXTERNAL_FEED_OVERRIDES_RULE"
    changed = (
        existing_classification != incoming.classification
        or existing_source != CLASSIFICATION_SOURCE_EXTERNAL
    )
    return ClassificationResolution(
        final_classification=incoming.classification,
        final_source=CLASSIFICATION_SOURCE_EXTERNAL,
        changed=changed,
        reason=reason,
    )


def _external_matched_signal(record: ExternalClassificationRecord, reason: str) -> dict[str, Any]:
    return {
        "value_scope": "METADATA_ONLY",
        "actual_values_inspected": False,
        "external_source": record.source,
        "reason": reason,
        "note": record.note,
    }


@dataclass(frozen=True, slots=True)
class ClassificationFeedIngestResult:
    total: int
    matched: int
    changed: int
    unmatched: tuple[str, ...]
    changed_column_ids: tuple[str, ...]


async def ingest_classification_feed(
    session: AsyncSession,
    *,
    datasource: DataSource,
    records: list[ExternalClassificationRecord],
    created_by: str,
) -> ClassificationFeedIngestResult:
    """Match external records to active columns and apply the override.

    Every applied record supersedes prior evidence rows for that column
    (``is_current`` flips to False) and appends a new, current
    ``ClassificationEvidence`` row with ``source_type="EXTERNAL_AUTHORITATIVE"``
    so the override is always distinguishable from a rule-inferred value.
    """
    matched = 0
    changed = 0
    unmatched: list[str] = []
    changed_column_ids: list[str] = []
    for record in records:
        column = await session.scalar(
            select(MetadataColumn)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataColumn.organization_id == datasource.organization_id,
                MetadataTable.datasource_id == datasource.id,
                MetadataSchema.name == record.schema_name,
                MetadataTable.name == record.table_name,
                MetadataColumn.name == record.column_name,
                MetadataColumn.status == "ACTIVE",
            )
        )
        if column is None:
            unmatched.append(f"{record.schema_name}.{record.table_name}.{record.column_name}")
            continue
        matched += 1
        resolution = resolve_classification_conflict(
            existing_source=column.classification_source,
            existing_classification=column.classification,
            incoming=record,
        )
        if resolution.changed:
            changed += 1
            changed_column_ids.append(str(column.id))
        await session.execute(
            update(ClassificationEvidence)
            .where(
                ClassificationEvidence.column_id == column.id,
                ClassificationEvidence.is_current.is_(True),
            )
            .values(is_current=False)
        )
        session.add(
            ClassificationEvidence(
                organization_id=column.organization_id,
                column_id=column.id,
                classification=resolution.final_classification,
                source_type=CLASSIFICATION_SOURCE_EXTERNAL,
                rule_id=f"EXTERNAL_FEED:{record.source}",
                confidence=record.confidence,
                matched_signal=_external_matched_signal(record, resolution.reason),
                is_current=True,
                created_by=created_by,
            )
        )
        column.classification = resolution.final_classification
        column.classification_source = resolution.final_source
    return ClassificationFeedIngestResult(
        total=len(records),
        matched=matched,
        changed=changed,
        unmatched=tuple(unmatched),
        changed_column_ids=tuple(changed_column_ids),
    )
