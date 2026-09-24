"""R11-MP22: act on screening verdicts a classifier upgrade left stale.

`ingest_screening.is_verdict_current` reports a verdict written by older rules,
and nothing acted on it: a view definition, routine body or trigger body
screened CLEAN by yesterday's classifier stayed eligible for model context until
the next scan of its source, however long that took.

This sweep re-screens those rows now, from the text the platform holds -- the
literal-redacted definition or body -- and is **tighten-only**:

* a row the current rules quarantine is quarantined, with the current version
  stamped beside the verdict, and leaves model context at once;
* a row the current rules pass is left exactly as it was. Its original verdict
  was taken over the raw text, which the platform does not keep, so a CLEAN
  result over the redacted form is not the same finding and is not stamped as
  current. The next scan of the source settles it.

Bounded per call, oldest-screened first by id order, so a large estate is worked
through over successive passes rather than in one.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine, MetadataTrigger, MetadataViewDefinition
from aida.ingest_screening import CLEAN, QUARANTINED, SCREENING_VERSION, screen_text

RESCREEN_LIMIT: Final = 500

#: Each screened store and the column holding the text it was screened from.
_STORES: Final[tuple[tuple[Any, str, str], ...]] = (
    (MetadataViewDefinition, "definition_sql_redacted", "view_definition"),
    (MetadataRoutine, "body_sql_redacted", "routine_body"),
    (MetadataTrigger, "body_sql_redacted", "trigger_body"),
)


async def requarantine_stale_verdicts(
    session: AsyncSession, organization_id: UUID, *, limit: int = RESCREEN_LIMIT
) -> int:
    """Re-screen CLEAN rows whose verdict is stale; quarantine what now fails.

    Returns how many rows were quarantined. Never clears a quarantine and never
    stamps a CLEAN row as current.
    """
    quarantined = 0
    remaining = limit
    for model, text_column, origin in _STORES:
        if remaining <= 0:
            break
        rows = (
            await session.scalars(
                select(model)
                .where(
                    model.organization_id == organization_id,
                    model.screening_status == CLEAN,
                    or_(
                        model.screening_version.is_(None),
                        model.screening_version != SCREENING_VERSION,
                    ),
                    getattr(model, text_column).is_not(None),
                )
                .order_by(model.id)
                .limit(remaining)
            )
        ).all()
        remaining -= len(rows)
        for row in rows:
            verdict = screen_text(getattr(row, text_column), content_origin=origin)
            if verdict.status == QUARANTINED:
                row.screening_status = QUARANTINED
                row.screening_reason_codes = verdict.reason_codes
                row.screening_version = verdict.version
                quarantined += 1
    if quarantined:
        await session.flush()
    return quarantined
