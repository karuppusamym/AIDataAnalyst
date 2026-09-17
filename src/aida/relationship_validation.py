"""R11-FP06: validate a proposed join before it can become an approved relationship.

A relationship candidate is proposed from column names, types, declared keys or query
history. Approving one turns it into a join that lineage, context and generated tools rely
on, so the decision needs more than a plausible name. This module derives, at decision time
and from the catalog as it is now:

* evidence classes: which facts support the join, and whether any of them is evidence that
  one table references the other (``outcome``);
* key columns, uniqueness per side, cardinality and direction, so a join that can multiply
  rows is visible before anyone uses it;
* optionality of the referencing columns, declared and observed;
* observation bounds: the profile behind each statistic, and whether it saw the whole table
  or a sample.

What corroborates a join:

* a declared foreign key between these columns, in either direction;
* joins between these columns observed in query history;
* a key on exactly one side (declared, a unique index, an approved key candidate, or a
  profile showing the column unique), for a column name that says more than ``id``.

A name and type match on its own does not, and neither does a match between two keys: two
primary keys that share a name are usually two unrelated tables' own identifiers.

Nothing here queries the source. Every fact comes from stored metadata and ``ColumnProfile``
counts (ADR-0014). Checking that every referencing value exists on the key side needs a
source query, so that check is recorded as not run, with its reason, and never assumed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.connectors.base import OBSERVATION_SCOPES
from aida.models import (
    ColumnProfile,
    CompositeKeyCandidate,
    MetadataColumn,
    MetadataConstraint,
    MetadataIndex,
    RelationshipCandidate,
    RelationshipCandidateGroup,
    RelationshipCandidateGroupMember,
    TableProfile,
)
from aida.relationship_naming import canonical_column_name, physical_type_family
from atlas.modules.profiling.facets import (
    PROFILED_UNIQUE_MIN_RATIO as _PROFILED_UNIQUE_MIN_RATIO,
)
from atlas.modules.profiling.facets import (
    effectively_unique as _effectively_unique,
)

RELATIONSHIP_VALIDATION_VERSION = "relationship-validation-v1"
NAME_MATCH_ONLY_CODE = "RELATIONSHIP_NAME_MATCH_ONLY"
NAME_MATCH_ONLY_MESSAGE = (
    "This join rests only on matching column names and types, which is not evidence that one "
    "table references the other. Approval needs a declared foreign key, joins observed in "
    "query history, or a key on exactly one side (declared, approved or profiled) for a column "
    "name more specific than a bare identifier."
)
CORROBORATED = "CORROBORATED"
NAME_MATCH_ONLY = "NAME_MATCH_ONLY"
INCLUSION_CHECK_STATUS = "NOT_RUN"
INCLUSION_CHECK_REASON = (
    "Checking that every referencing value exists on the key side needs a query against the "
    "source, and validation runs none."
)
QUERY_LOG_JOIN_RULE = "QUERY_LOG_JOIN_V1"
# Approximate distinct counts can undercount a unique column slightly.
#
# R11-FP04: the rule now lives in `atlas.modules.profiling.facets`, which
# applies it once when a profile is written, because it was also being
# re-derived -- against a different denominator -- in
# `aida.composite_key_inference`. Re-exported under its original name so
# nothing that reads it from here changed.
PROFILED_UNIQUE_MIN_RATIO = _PROFILED_UNIQUE_MIN_RATIO
# A match on one of these canonical names says nothing about which table is referenced.
GENERIC_COLUMN_NAMES = frozenset({"id", "key", "pk", "uuid", "guid", "code", "name", "value"})

_KEY_CONSTRAINT_TYPES = ("PRIMARY_KEY", "UNIQUE")
_BASIS_CLASS = {
    "DECLARED_KEY": "DECLARED_KEY",
    "UNIQUE_INDEX": "UNIQUE_INDEX",
    "APPROVED_KEY": "APPROVED_KEY",
    "PROFILED": "PROFILED_UNIQUE",
}
_BASIS_WORDS = {
    "DECLARED_KEY": "a declared primary or unique key",
    "UNIQUE_INDEX": "a unique index",
    "APPROVED_KEY": "an approved key candidate",
    "PROFILED": "a profile showing every non-null value distinct",
}


class RelationshipColumnsMissingError(LookupError):
    """A candidate names a column the catalog no longer holds."""


@dataclass(frozen=True, slots=True)
class ColumnFacts:
    column_id: UUID
    name: str
    physical_type: str
    nullable: bool
    status: str = "ACTIVE"
    null_count: int | None = None
    non_null_count: int | None = None
    approximate_distinct_count: int | None = None
    # R11-FP04: `ColumnProfile.effectively_unique`, the stored answer, when the
    # profile that produced these counts recorded one. None for a profile
    # written before the facet existed -- see `profiled_unique`.
    stored_effectively_unique: bool | None = None

    @property
    def profiled_unique(self) -> bool:
        """Whether the profile behind these counts showed the column unique.

        R11-FP04: reads the stored facet when the profile has one, and falls
        back to deriving it from the counts otherwise, so a profile written
        before the facet existed still answers -- and answers identically,
        since the fallback calls the same function the writer does.

        Still `bool` rather than `bool | None` at this seam: `_side_uniqueness`
        treats "not shown unique" and "cannot tell" the same way (neither is
        evidence of a key), and widening the type here would put a three-valued
        logic into every caller for no decision that depends on it. The
        distinction that *does* matter -- whether the evidence was
        sample-bounded -- is `ProfileBounds.scope`, which is separate and is
        carried through to the reviewer.
        """
        if self.stored_effectively_unique is not None:
            return self.stored_effectively_unique
        return bool(
            _effectively_unique(
                non_null_count=self.non_null_count,
                approximate_distinct_count=self.approximate_distinct_count,
            )
        )


@dataclass(frozen=True, slots=True)
class ProfileBounds:
    table_profile_id: UUID
    profiled_at: datetime
    sampled_row_count: int
    row_count_estimate: int | None
    # R11-FP04: `TableProfile.observation_scope` -- what the connector itself
    # said about its own scan. None for a profile written before the column
    # existed.
    stored_observation_scope: str | None = None

    @property
    def scope(self) -> str:
        """How much of the table the profile behind these statistics saw.

        R11-FP04. Prefers the stored facet, because only the connector knows
        whether it issued a bound, and the comparison below is a proxy that got
        it wrong in both directions: BigQuery reported the sample size as the
        row estimate (so a bounded profile of a huge table read as FULL), and
        Snowflake reported a nominal sample size for an unbounded full scan (so
        a complete profile read as SAMPLE). Since this scope is what decides
        whether a join's uniqueness evidence is flagged sample-bounded to a
        reviewer, both errors silently mis-stated the strength of approval
        evidence.

        The old derivation is kept, unchanged, for rows written before the
        facet existed -- it is imperfect but it is what those rows support, and
        dropping it would turn every historical profile into UNKNOWN and
        withdraw evidence from candidates that were assessed with it.
        """
        if self.stored_observation_scope in OBSERVATION_SCOPES:
            return self.stored_observation_scope or "UNKNOWN"
        if self.row_count_estimate is None:
            return "UNKNOWN"
        return "FULL" if self.sampled_row_count >= self.row_count_estimate else "SAMPLE"

    def as_evidence(self) -> dict[str, Any]:
        return {
            "table_profile_id": str(self.table_profile_id),
            "profiled_at": self.profiled_at.isoformat(),
            "sampled_row_count": self.sampled_row_count,
            "row_count_estimate": self.row_count_estimate,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class DeclaredForeignKey:
    columns: tuple[str, ...]
    referenced_table_id: UUID
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TableFacts:
    table_id: UUID
    declared_keys: tuple[frozenset[str], ...] = ()
    unique_indexes: tuple[frozenset[str], ...] = ()
    approved_keys: tuple[frozenset[str], ...] = ()
    foreign_keys: tuple[DeclaredForeignKey, ...] = ()
    profile: ProfileBounds | None = None


@dataclass(frozen=True, slots=True)
class RelationshipFacts:
    source: TableFacts
    target: TableFacts
    pairs: tuple[tuple[ColumnFacts, ColumnFacts], ...]
    detection_rule: str
    observed_join_count: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceClass:
    name: str
    corroborating: bool
    detail: str
    sample_bounded: bool = False

    def as_evidence(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "corroborating": self.corroborating,
            "detail": self.detail,
            "sample_bounded": self.sample_bounded,
        }


@dataclass(frozen=True, slots=True)
class SideUniqueness:
    unique: bool
    basis: str | None = None
    sample_bounded: bool = False

    def as_evidence(self) -> dict[str, Any]:
        return {"unique": self.unique, "basis": self.basis, "sample_bounded": self.sample_bounded}


@dataclass(frozen=True, slots=True)
class RelationshipValidation:
    outcome: str
    evidence_classes: tuple[EvidenceClass, ...]
    source_key_columns: tuple[str, ...]
    target_key_columns: tuple[str, ...]
    cardinality: str
    direction: str
    source_uniqueness: SideUniqueness
    target_uniqueness: SideUniqueness
    referencing_side: str
    optionality: str
    optionality_columns: tuple[dict[str, Any], ...]
    source_observation: ProfileBounds | None
    target_observation: ProfileBounds | None
    grain_warnings: tuple[str, ...]

    @property
    def approvable(self) -> bool:
        return self.outcome == CORROBORATED

    @property
    def join_condition(self) -> str:
        return " AND ".join(
            f"source.{source} = target.{target}"
            for source, target in zip(self.source_key_columns, self.target_key_columns, strict=True)
        )

    @property
    def fingerprint(self) -> str:
        """Digest of the conclusions, not of which profile run produced them.

        A re-profile that reaches the same conclusions keeps the digest; a lost key, a new
        null or a changed direction moves it.
        """
        material = {
            "version": RELATIONSHIP_VALIDATION_VERSION,
            "outcome": self.outcome,
            "classes": sorted(
                [c.name, c.corroborating, c.sample_bounded] for c in self.evidence_classes
            ),
            "cardinality": self.cardinality,
            "direction": self.direction,
            "source_uniqueness": self.source_uniqueness.as_evidence(),
            "target_uniqueness": self.target_uniqueness.as_evidence(),
            "optionality": self.optionality,
            "warnings": sorted(self.grain_warnings),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    def as_evidence(self) -> dict[str, Any]:
        return {
            "validation_version": RELATIONSHIP_VALIDATION_VERSION,
            "outcome": self.outcome,
            "evidence_classes": [item.as_evidence() for item in self.evidence_classes],
            "source_key_columns": list(self.source_key_columns),
            "target_key_columns": list(self.target_key_columns),
            "join_condition": self.join_condition,
            "cardinality": self.cardinality,
            "direction": self.direction,
            "source_uniqueness": self.source_uniqueness.as_evidence(),
            "target_uniqueness": self.target_uniqueness.as_evidence(),
            "referencing_side": self.referencing_side,
            "optionality": self.optionality,
            "optionality_columns": [dict(column) for column in self.optionality_columns],
            "source_observation": (
                self.source_observation.as_evidence() if self.source_observation else None
            ),
            "target_observation": (
                self.target_observation.as_evidence() if self.target_observation else None
            ),
            "inclusion_check_status": INCLUSION_CHECK_STATUS,
            "inclusion_check_reason": INCLUSION_CHECK_REASON,
            "grain_warnings": list(self.grain_warnings),
            "source_queries_executed": 0,
            "values_inspected": False,
            "fingerprint": self.fingerprint,
        }


# --------------------------------------------------------------------------
# Assessment (pure)
# --------------------------------------------------------------------------


def _foreign_key_matches(
    foreign_key: DeclaredForeignKey,
    referencing: Sequence[ColumnFacts],
    referenced_table_id: UUID,
    referenced: Sequence[ColumnFacts],
) -> bool:
    if foreign_key.referenced_table_id != referenced_table_id:
        return False
    names = [column.name.lower() for column in referencing]
    if sorted(foreign_key.columns) != sorted(names):
        return False
    if not foreign_key.referenced_columns:
        # The connector reported the referenced table but not its columns.
        return True
    declared = dict(zip(foreign_key.columns, foreign_key.referenced_columns, strict=False))
    wanted = {
        source.name.lower(): target.name.lower()
        for source, target in zip(referencing, referenced, strict=True)
    }
    return declared == wanted


def _side_uniqueness(
    table: TableFacts, columns: Sequence[ColumnFacts], *, referenced_by_foreign_key: bool
) -> SideUniqueness:
    names = frozenset(column.name.lower() for column in columns)
    ids = frozenset(str(column.column_id) for column in columns)
    # A key that is a subset of the join columns makes the whole column set unique.
    if any(key and key <= names for key in table.declared_keys):
        return SideUniqueness(True, "DECLARED_KEY")
    if any(key and key <= names for key in table.unique_indexes):
        return SideUniqueness(True, "UNIQUE_INDEX")
    if referenced_by_foreign_key:
        return SideUniqueness(True, "DECLARED_FOREIGN_KEY")
    if any(key and key <= ids for key in table.approved_keys):
        return SideUniqueness(True, "APPROVED_KEY")
    if any(column.profiled_unique for column in columns):
        scope = table.profile.scope if table.profile is not None else "UNKNOWN"
        return SideUniqueness(True, "PROFILED", sample_bounded=scope != "FULL")
    return SideUniqueness(False)


def _optionality(columns: Sequence[ColumnFacts]) -> str:
    if any((column.null_count or 0) > 0 for column in columns):
        return "OPTIONAL"
    if all(not column.nullable for column in columns):
        return "MANDATORY"
    if all(column.null_count is not None for column in columns if column.nullable):
        return "NULLABLE_NONE_OBSERVED"
    return "UNKNOWN"


def assess_relationship(facts: RelationshipFacts) -> RelationshipValidation:
    sources = [source for source, _ in facts.pairs]
    targets = [target for _, target in facts.pairs]
    classes: list[EvidenceClass] = []
    warnings: list[str] = []

    forward_fk = any(
        _foreign_key_matches(fk, sources, facts.target.table_id, targets)
        for fk in facts.source.foreign_keys
    )
    reverse_fk = any(
        _foreign_key_matches(fk, targets, facts.source.table_id, sources)
        for fk in facts.target.foreign_keys
    )
    if forward_fk:
        classes.append(
            EvidenceClass(
                "DECLARED_FOREIGN_KEY",
                True,
                "The source declares a foreign key on these columns referencing the target.",
            )
        )
    if reverse_fk:
        classes.append(
            EvidenceClass(
                "DECLARED_FOREIGN_KEY",
                True,
                "The target declares a foreign key on these columns referencing the source.",
            )
        )
    if facts.observed_join_count > 0:
        classes.append(
            EvidenceClass(
                "OBSERVED_QUERY_JOIN",
                True,
                f"Query history joins these columns {facts.observed_join_count} time(s).",
            )
        )

    source_unique = _side_uniqueness(facts.source, sources, referenced_by_foreign_key=reverse_fk)
    target_unique = _side_uniqueness(facts.target, targets, referenced_by_foreign_key=forward_fk)
    generic = all(canonical_column_name(column.name) in GENERIC_COLUMN_NAMES for column in sources)
    exactly_one_key = source_unique.unique != target_unique.unique
    for side, uniqueness in (("target", target_unique), ("source", source_unique)):
        if uniqueness.basis not in _BASIS_CLASS:
            continue
        corroborating = exactly_one_key and not generic
        if corroborating:
            detail = f"The {side} columns are unique by {_BASIS_WORDS[uniqueness.basis]}."
        elif not exactly_one_key:
            detail = (
                f"The {side} columns are unique by {_BASIS_WORDS[uniqueness.basis]}, but so is "
                "the other side; two keys sharing a name are not evidence of a reference."
            )
        else:
            detail = (
                f"The {side} columns are unique by {_BASIS_WORDS[uniqueness.basis]}, but the "
                "matched name is a bare identifier that any table can carry."
            )
        classes.append(
            EvidenceClass(
                _BASIS_CLASS[uniqueness.basis],
                corroborating,
                detail,
                sample_bounded=uniqueness.sample_bounded,
            )
        )

    if all(
        canonical_column_name(source.name) == canonical_column_name(target.name)
        for source, target in facts.pairs
    ):
        literal = all(source.name.lower() == target.name.lower() for source, target in facts.pairs)
        classes.append(
            EvidenceClass(
                "NAME_MATCH",
                False,
                "Column names match exactly."
                if literal
                else "Column names match after naming-convention normalization.",
            )
        )
    if all(
        physical_type_family(source.physical_type) == physical_type_family(target.physical_type)
        for source, target in facts.pairs
    ):
        literal = all(
            source.physical_type.lower() == target.physical_type.lower()
            for source, target in facts.pairs
        )
        classes.append(
            EvidenceClass(
                "TYPE_MATCH",
                False,
                "Physical types match exactly." if literal else "Physical types share a family.",
            )
        )
    else:
        warnings.append("TYPE_FAMILY_MISMATCH")

    if target_unique.unique and source_unique.unique:
        cardinality, direction = "ONE_TO_ONE", "EITHER"
    elif target_unique.unique:
        cardinality, direction = "MANY_TO_ONE", "SOURCE_REFERENCES_TARGET"
    elif source_unique.unique:
        cardinality, direction = "ONE_TO_MANY", "TARGET_REFERENCES_SOURCE"
        warnings.append("DIRECTION_REVERSED")
    else:
        cardinality, direction = "UNKNOWN", "UNDETERMINED"
        warnings.append("FAN_OUT_POSSIBLE")
    if source_unique.sample_bounded or target_unique.sample_bounded:
        warnings.append("UNIQUENESS_SAMPLE_BOUNDED")
    if generic:
        warnings.append("GENERIC_COLUMN_NAME")
    if any(column.status != "ACTIVE" for column in (*sources, *targets)):
        warnings.append("COLUMN_NOT_ACTIVE")

    referencing_side = "TARGET" if direction == "TARGET_REFERENCES_SOURCE" else "SOURCE"
    referencing = targets if referencing_side == "TARGET" else sources
    return RelationshipValidation(
        outcome=CORROBORATED if any(c.corroborating for c in classes) else NAME_MATCH_ONLY,
        evidence_classes=tuple(classes),
        source_key_columns=tuple(column.name for column in sources),
        target_key_columns=tuple(column.name for column in targets),
        cardinality=cardinality,
        direction=direction,
        source_uniqueness=source_unique,
        target_uniqueness=target_unique,
        referencing_side=referencing_side,
        optionality=_optionality(referencing),
        optionality_columns=tuple(
            {
                "column_name": column.name,
                "declared_nullable": column.nullable,
                "observed_null_count": column.null_count,
                "observed_non_null_count": column.non_null_count,
            }
            for column in referencing
        ),
        source_observation=facts.source.profile,
        target_observation=facts.target.profile,
        grain_warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# Loading facts from the catalog
# --------------------------------------------------------------------------


async def _load_facts(
    session: AsyncSession,
    *,
    source_table_id: UUID,
    target_table_id: UUID,
    column_pairs: Sequence[tuple[UUID, UUID]],
    detection_rule: str,
    evidence: Mapping[str, Any] | None,
) -> RelationshipFacts:
    table_ids = {source_table_id, target_table_id}
    column_ids = {column_id for pair in column_pairs for column_id in pair}
    columns = {
        column.id: column
        for column in (
            await session.scalars(
                # R11-FP06: a retired column is as gone as a deleted one; a join cannot rest on it.
                select(MetadataColumn).where(
                    MetadataColumn.id.in_(column_ids), MetadataColumn.status == "ACTIVE"
                )
            )
        ).all()
    }
    missing = column_ids - set(columns)
    if missing:
        raise RelationshipColumnsMissingError(
            f"{len(missing)} column(s) of this relationship are no longer in the catalog"
        )

    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id.in_(table_ids),
                MetadataConstraint.status == "ACTIVE",
            )
        )
    ).all()
    indexes = (
        await session.scalars(
            select(MetadataIndex).where(
                MetadataIndex.table_id.in_(table_ids),
                MetadataIndex.status == "ACTIVE",
                or_(MetadataIndex.is_unique.is_(True), MetadataIndex.is_primary.is_(True)),
            )
        )
    ).all()
    approved_keys = (
        await session.scalars(
            select(CompositeKeyCandidate).where(
                CompositeKeyCandidate.table_id.in_(table_ids),
                CompositeKeyCandidate.status == "APPROVED",
            )
        )
    ).all()
    profiles: dict[UUID, TableProfile] = {}
    for table_id in table_ids:
        profile = (
            await session.scalars(
                select(TableProfile)
                .where(TableProfile.table_id == table_id, TableProfile.status == "COMPLETED")
                .order_by(TableProfile.created_at.desc())
                .limit(1)
            )
        ).first()
        if profile is not None:
            profiles[table_id] = profile
    column_stats: dict[UUID, ColumnProfile] = {}
    if profiles:
        column_stats = {
            row.column_id: row
            for row in (
                await session.scalars(
                    select(ColumnProfile).where(
                        ColumnProfile.table_profile_id.in_([p.id for p in profiles.values()]),
                        ColumnProfile.column_id.in_(column_ids),
                    )
                )
            ).all()
        }

    def table_facts(table_id: UUID) -> TableFacts:
        own_constraints = [c for c in constraints if c.table_id == table_id]
        profile = profiles.get(table_id)
        return TableFacts(
            table_id=table_id,
            declared_keys=tuple(
                frozenset(name.lower() for name in c.columns)
                for c in own_constraints
                if c.constraint_type in _KEY_CONSTRAINT_TYPES and c.columns
            ),
            unique_indexes=tuple(
                frozenset(name.lower() for name in index.columns)
                for index in indexes
                if index.table_id == table_id and index.columns
            ),
            approved_keys=tuple(
                frozenset(key.column_ids)
                for key in approved_keys
                if key.table_id == table_id and key.column_ids
            ),
            foreign_keys=tuple(
                DeclaredForeignKey(
                    columns=tuple(name.lower() for name in c.columns),
                    referenced_table_id=c.referenced_table_id,
                    referenced_columns=tuple(name.lower() for name in c.referenced_columns),
                )
                for c in own_constraints
                if c.constraint_type == "FOREIGN_KEY" and c.referenced_table_id is not None
            ),
            profile=(
                ProfileBounds(
                    table_profile_id=profile.id,
                    profiled_at=profile.created_at,
                    sampled_row_count=profile.sampled_row_count,
                    row_count_estimate=profile.row_count_estimate,
                    stored_observation_scope=profile.observation_scope,
                )
                if profile is not None
                else None
            ),
        )

    def column_facts(column_id: UUID) -> ColumnFacts:
        column = columns[column_id]
        stats = column_stats.get(column_id)
        return ColumnFacts(
            column_id=column.id,
            name=column.name,
            physical_type=column.physical_type,
            nullable=column.nullable,
            status=column.status,
            null_count=stats.null_count if stats is not None else None,
            non_null_count=stats.non_null_count if stats is not None else None,
            approximate_distinct_count=(
                stats.approximate_distinct_count if stats is not None else None
            ),
            stored_effectively_unique=(stats.effectively_unique if stats is not None else None),
        )

    observed = 0
    if detection_rule == QUERY_LOG_JOIN_RULE:
        count = (evidence or {}).get("occurrence_count")
        observed = count if isinstance(count, int) and count > 0 else 0
    return RelationshipFacts(
        source=table_facts(source_table_id),
        target=table_facts(target_table_id),
        pairs=tuple((column_facts(s), column_facts(t)) for s, t in column_pairs),
        detection_rule=detection_rule,
        observed_join_count=observed,
    )


async def validate_relationship_candidate(
    session: AsyncSession, candidate: RelationshipCandidate
) -> RelationshipValidation:
    facts = await _load_facts(
        session,
        source_table_id=candidate.source_table_id,
        target_table_id=candidate.target_table_id,
        column_pairs=[(candidate.source_column_id, candidate.target_column_id)],
        detection_rule=candidate.detection_rule,
        evidence=candidate.evidence,
    )
    return assess_relationship(facts)


async def validate_composite_relationship_candidate(
    session: AsyncSession, group: RelationshipCandidateGroup
) -> RelationshipValidation:
    members = (
        await session.scalars(
            select(RelationshipCandidateGroupMember)
            .where(RelationshipCandidateGroupMember.group_id == group.id)
            .order_by(RelationshipCandidateGroupMember.ordinal)
        )
    ).all()
    if not members:
        raise RelationshipColumnsMissingError("this composite relationship has no column pairs")
    facts = await _load_facts(
        session,
        source_table_id=group.source_table_id,
        target_table_id=group.target_table_id,
        column_pairs=[(member.source_column_id, member.target_column_id) for member in members],
        detection_rule=group.detection_rule,
        evidence=group.evidence,
    )
    return assess_relationship(facts)


# --------------------------------------------------------------------------
# Decision helpers
# --------------------------------------------------------------------------


def refusal_detail(validation: RelationshipValidation) -> dict[str, Any]:
    """The 409 body for an approval the evidence does not support.

    The outcome only: the evidence names columns and carries profile counts from both sides, so
    it is served by the validation read, under that read's datasource and domain gates.
    """
    return {
        "code": NAME_MATCH_ONLY_CODE,
        "message": NAME_MATCH_ONLY_MESSAGE,
        "outcome": validation.outcome,
    }


#: Where an approval records the validation it rested on, inside the candidate's evidence.
RECORDED_VALIDATION_KEY: Final = "validation"


def with_recorded_validation(
    evidence: Mapping[str, Any] | None, validation: RelationshipValidation, validated_at: datetime
) -> dict[str, Any]:
    """A new evidence mapping carrying the validation the approval was made on.

    Returned as a new dict so the JSON column registers the change.
    """
    return {
        **(evidence or {}),
        RECORDED_VALIDATION_KEY: {
            **validation.as_evidence(),
            "validated_at": validated_at.isoformat(),
        },
    }


def public_relationship_evidence(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """A candidate's evidence without the validation an approval recorded.

    The recorded validation names the key columns of both sides and carries their profile
    counts, so it is served by the validation read alone, under that read's datasource and
    domain gates (R11-FP06). Every surface that serves a candidate on organization membership
    -- the candidate lists, a decision response, the review queue, a lineage graph edge --
    serves this instead, so approving a join never becomes the way to read what the gated read
    refuses.
    """
    return {key: value for key, value in (evidence or {}).items() if key != RECORDED_VALIDATION_KEY}


def validation_drift(evidence: Mapping[str, Any] | None, validation: RelationshipValidation) -> str:
    """How today's validation compares with the one recorded when the join was approved."""
    recorded = (evidence or {}).get(RECORDED_VALIDATION_KEY)
    if not isinstance(recorded, Mapping) or not recorded.get("fingerprint"):
        return "NOT_RECORDED"
    if recorded.get("outcome") == CORROBORATED and not validation.approvable:
        return "CORROBORATION_LOST"
    if recorded.get("fingerprint") != validation.fingerprint:
        return "CHANGED"
    return "UNCHANGED"
