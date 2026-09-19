"""R11-FP15: meaning signals -- the approved meaning a reader is given, retired.

`change_signals` records what moved in a *source*, at the one write that sees each change. Meaning
moved without a record: an approved table, column or routine description superseded or withdrawn,
a semantic model or glossary term version superseded. A context product that stood on that meaning
went on serving it as if nothing had happened -- the product promise failing without anyone told.

**Detected after the fact, deliberately, rather than written at the write.** The ontology's
`MEANING_PUBLISHED` is recorded inside `ontology_api.decide_ontology`, because that is its one
write. Description meaning has no one write: a table description is superseded by
`asset_description_service.publish_asset_documentation_version` (reached from GL-9 drafts and
approved document claims), by `semantic_api._decide_asset_documentation_version`, by a model
import; a column's by `column_documentation.publish_column_description` and
`column_description_api`; a routine's by `routine_description_service`; any of them withdrawn by
`description_withdrawal` or a workbook re-import. Eight writers in six modules, and a ninth would
be added by whoever next builds an authoring route and does not know this list exists. A write-site
signal that one of them forgot is exactly the silent staleness this module exists to end, so the
record is taken from the one place every writer agrees on -- the append-only version rows
themselves, whose retired status is terminal and whose content is never edited -- and every writer,
present and future, is covered by construction.

**What "changed" means.** A retired version is a change when the reader is now given something
else:

* a description or glossary term version `WITHDRAWN` -- the reader is given no approved text --
  is always one (`MEANING_WITHDRAWN`);
* one `SUPERSEDED` is one only when the version that replaced it says something different
  (`MEANING_REPLACED`, with `related_subject_id` naming that version). A supersession that
  re-approves identical text -- a re-review, a rebuild redraft that came out the same, an alias or
  owner edit on a table's documentation -- records nothing, now or on any later sweep, because the
  comparison is with the version that *replaced* it, which never changes;
* a superseded semantic model version is always one: a model is its metrics, not a text, and its
  pins are by version id, so a new published version is new meaning whatever it contains.

Drafts are never signalled -- only a version a reader was once given can be retired.

**Idempotent without a key, like every other signal.** A retired version is terminal and is
signalled at most once: the sweep anti-joins on `(subject_kind, subject_id)`, which
`ix_metadata_change_signal_subject` already indexes, and one whose replacement said the same thing
never matches at all, so the sweep never re-examines it. The first sweep in an estate records the
retirements already in it, once -- a backfill, not a flood that repeats.

Value-free: the text comparison happens inside the database; a signal carries ids and codes only.
`detected_at` is when the sweep saw the retirement, as the name says; the retired row's own
`updated_at` still says when it happened.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, Select, exists, null, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import (
    CHANGE_MEANING_REPLACED,
    CHANGE_MEANING_WITHDRAWN,
    SIGNAL_MEANING_RETIRED,
    SUBJECT_COLUMN_DESCRIPTION,
    SUBJECT_GLOSSARY_TERM,
    SUBJECT_ROUTINE_DESCRIPTION,
    SUBJECT_SEMANTIC_MODEL,
    SUBJECT_TABLE_DESCRIPTION,
    ChangeSignal,
    record_change_signals,
)
from aida.envelope_models import RoutineDocumentation, RoutineDocumentationVersion
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    GlossaryTermVersion,
    MetadataTable,
    SemanticModelVersion,
)

SUPERSEDED: Final = "SUPERSEDED"
WITHDRAWN: Final = "WITHDRAWN"
RETIRED_STATUSES: Final = (SUPERSEDED, WITHDRAWN)


@dataclass(frozen=True, slots=True)
class MeaningStore:
    """One append-only store of approved meaning, as the sweep reads it.

    `lineage` is the column grouping one object's versions (its documentation row, its project's
    model, its term); `text` the column a reader is given, or `None` where every supersession is a
    change; `current_status` the status a version holds while it is the one a reader is given.
    """

    subject_kind: str
    version: Any
    lineage: str
    text: str | None
    current_status: str


STORES: Final = (
    MeaningStore(
        SUBJECT_TABLE_DESCRIPTION,
        AssetDocumentationVersion,
        "documentation_id",
        "readme",
        "APPROVED",
    ),
    MeaningStore(
        SUBJECT_COLUMN_DESCRIPTION,
        ColumnDocumentationVersion,
        "documentation_id",
        "description",
        "APPROVED",
    ),
    MeaningStore(
        SUBJECT_ROUTINE_DESCRIPTION,
        RoutineDocumentationVersion,
        "documentation_id",
        "description",
        "APPROVED",
    ),
    MeaningStore(SUBJECT_SEMANTIC_MODEL, SemanticModelVersion, "project_id", None, "PUBLISHED"),
    MeaningStore(SUBJECT_GLOSSARY_TERM, GlossaryTermVersion, "term_id", "definition", "APPROVED"),
)


def _datasource_column(store: MeaningStore) -> tuple[Any, Any]:
    """The datasource a store's signal belongs to, and the join that reaches it.

    Descriptions belong to their object's source, so `GET /v1/datasources/{id}/change-signals`
    lists them beside that source's definition changes. A semantic model or glossary term belongs
    to no one source -- the ontology's rule -- and carries none.
    """
    version = store.version
    if store.subject_kind == SUBJECT_TABLE_DESCRIPTION:
        return MetadataTable.datasource_id, (
            (AssetDocumentation, AssetDocumentation.id == version.documentation_id),
            (MetadataTable, MetadataTable.id == AssetDocumentation.table_id),
        )
    if store.subject_kind == SUBJECT_COLUMN_DESCRIPTION:
        return MetadataTable.datasource_id, (
            (ColumnDocumentation, ColumnDocumentation.id == version.documentation_id),
            (MetadataTable, MetadataTable.id == ColumnDocumentation.table_id),
        )
    if store.subject_kind == SUBJECT_ROUTINE_DESCRIPTION:
        return RoutineDocumentation.datasource_id, (
            (RoutineDocumentation, RoutineDocumentation.id == version.documentation_id),
        )
    return null(), ()


def _unsignalled_retirements(store: MeaningStore, organization_id: UUID | None) -> Select[Any]:
    """Retired versions in one store that changed meaning and are not yet signalled.

    One organization's, or -- `None`, for `organizations_with_unsignalled_retirements` -- every
    organization's, so "is there anything to sweep?" is asked with the very predicate the sweep
    uses rather than a second spelling of it that could disagree. Rows: retired version id, its
    status, the version that replaced it (or `None`), the datasource the signal belongs to, and
    the organization.
    """
    version = store.version
    successor = aliased(version)
    lineage = getattr(version, store.lineage)

    def next_approved(column: str) -> Any:
        # The version that replaced this one: the next that was ever the one a reader is given.
        # Ordered by version number, which every store allocates in publication order; if a
        # store ever published out of order the successor reads as missing, and a missing
        # successor counts as a change below -- the safe direction for a staleness signal.
        return (
            select(getattr(successor, column))
            .where(
                getattr(successor, store.lineage) == lineage,
                successor.version > version.version,
                successor.status.in_((store.current_status, *RETIRED_STATUSES)),
            )
            .order_by(successor.version)
            .limit(1)
            .correlate(version)
            .scalar_subquery()
        )

    successor_id = next_approved("id")
    if store.text is None:
        changed: ColumnElement[bool] = true()
    else:
        successor_text = next_approved(store.text)
        changed = or_(
            version.status == WITHDRAWN,
            successor_text.is_(None),
            successor_text != getattr(version, store.text),
        )
    signalled = exists().where(
        MetadataChangeSignal.organization_id == version.organization_id,
        MetadataChangeSignal.subject_kind == store.subject_kind,
        MetadataChangeSignal.subject_id == version.id,
        MetadataChangeSignal.signal_type == SIGNAL_MEANING_RETIRED,
    )
    datasource_id, joins = _datasource_column(store)
    statement = select(
        version.id, version.status, successor_id, datasource_id, version.organization_id
    ).select_from(version)
    for target, on in joins:
        statement = statement.join(target, on)
    scoped = () if organization_id is None else (version.organization_id == organization_id,)
    return statement.where(
        *scoped,
        version.status.in_(RETIRED_STATUSES),
        changed,
        ~signalled,
    )


async def organizations_with_unsignalled_retirements(session: AsyncSession) -> set[UUID]:
    """Every organization holding a retirement the sweep would record -- the rebuild pass's cue
    to visit it, so a description withdrawn in an organization with no other change is still
    swept."""
    found: set[UUID] = set()
    for store in STORES:
        subquery = _unsignalled_retirements(store, None).subquery()
        found.update(await session.scalars(select(subquery.c.organization_id).distinct()))
    return found


async def record_meaning_signals(
    session: AsyncSession, *, organization_id: UUID, limit: int
) -> int:
    """Record MEANING_RETIRED for this organization's unsignalled retirements, at most `limit`.

    Adds the signals to the caller's transaction, like `record_change_signals`; the caller
    commits. Oldest retirement first, so a backlog drains in the order it happened.
    """
    remaining = limit
    by_datasource: dict[UUID | None, list[ChangeSignal]] = defaultdict(list)
    for store in STORES:
        if remaining <= 0:
            break
        rows = (
            await session.execute(
                _unsignalled_retirements(store, organization_id)
                .order_by(store.version.updated_at, store.version.id)
                .limit(remaining)
            )
        ).all()
        for version_id, status, successor_id, datasource_id, _ in rows:
            withdrawn = status == WITHDRAWN
            by_datasource[datasource_id].append(
                ChangeSignal(
                    store.subject_kind,
                    version_id,
                    SIGNAL_MEANING_RETIRED,
                    CHANGE_MEANING_WITHDRAWN if withdrawn else CHANGE_MEANING_REPLACED,
                    None if withdrawn else successor_id,
                )
            )
        remaining -= len(rows)
    recorded = 0
    for datasource_id, signals in by_datasource.items():
        recorded += record_change_signals(
            session,
            organization_id=organization_id,
            datasource_id=datasource_id,
            analysis_run_id=None,
            signals=signals,
        )
    if recorded:
        await session.flush()
    return recorded


__all__ = [
    "RETIRED_STATUSES",
    "STORES",
    "MeaningStore",
    "organizations_with_unsignalled_retirements",
    "record_meaning_signals",
]
