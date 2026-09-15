"""R11-FP15: what changed in a source since it was last read, as value-free signals.

A rescan used to leave only counters behind (`changed_objects`), so nothing downstream could
tell *which* view was redefined, which routine was retired or whose grant was revoked -- and so
nothing could be re-examined selectively. Signals are recorded at the one place each change is
seen, inside the same transaction as the write that makes it:

* `DEFINITION_CHANGED` for a view definition or routine body whose source text changed. The
  class says whether only literals changed (`LITERAL_ONLY`: the raw-text fingerprint moved but
  the stored, value-free text did not) or anything else did (`STRUCTURAL`). Lineage parsed from
  the structure is still right after a literal-only change; a tool's result may not be.
* `DEPRECATED` / `REACTIVATED` for a table, view or routine leaving or returning to a snapshot.
* `PERMISSION_CHANGED` for a source grant that changed, returned or was revoked.
* `MEANING_PUBLISHED` when an ontology version is approved.

**Idempotent without a key.** A change is detected by comparing the stored fingerprint with the
incoming one. A retried batch whose first attempt committed finds them already equal and records
nothing; one whose first attempt rolled back rolled its signals back with it.

**New objects are not signals.** Nothing can depend on an object that did not exist, and a first
scan would otherwise emit one signal per object in the estate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal

SIGNAL_DEFINITION_CHANGED: Final = "DEFINITION_CHANGED"
SIGNAL_STRUCTURE_CHANGED: Final = "STRUCTURE_CHANGED"
SIGNAL_DEPRECATED: Final = "DEPRECATED"
SIGNAL_REACTIVATED: Final = "REACTIVATED"
SIGNAL_PERMISSION_CHANGED: Final = "PERMISSION_CHANGED"
SIGNAL_MEANING_PUBLISHED: Final = "MEANING_PUBLISHED"
CHANGE_LITERAL_ONLY: Final = "LITERAL_ONLY"
CHANGE_STRUCTURAL: Final = "STRUCTURAL"


@dataclass(frozen=True, slots=True)
class ChangeSignal:
    subject_kind: str
    subject_id: UUID
    signal_type: str
    change_class: str | None = None


@dataclass(frozen=True, slots=True)
class CodeState:
    """What a stored view definition or routine body looked like at one moment."""

    status: str
    availability: str
    raw_fingerprint: str | None
    stored_text: str | None


def code_change_signal(
    subject_kind: str, subject_id: UUID, before: CodeState | None, after: CodeState
) -> ChangeSignal | None:
    if before is None:
        return None
    if before.status != "ACTIVE":
        return ChangeSignal(subject_kind, subject_id, SIGNAL_REACTIVATED)
    if (before.availability, before.raw_fingerprint) == (after.availability, after.raw_fingerprint):
        return None
    literal_only = (
        before.availability == after.availability and before.stored_text == after.stored_text
    )
    return ChangeSignal(
        subject_kind,
        subject_id,
        SIGNAL_DEFINITION_CHANGED,
        CHANGE_LITERAL_ONLY if literal_only else CHANGE_STRUCTURAL,
    )


def record_change_signals(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    analysis_run_id: UUID | None,
    signals: Iterable[ChangeSignal],
) -> int:
    """Add the signals to the caller's transaction; the caller commits them with its write."""
    detected_at = datetime.now(UTC)
    recorded = 0
    for signal in signals:
        session.add(
            MetadataChangeSignal(
                organization_id=organization_id,
                datasource_id=datasource_id,
                analysis_run_id=analysis_run_id,
                subject_kind=signal.subject_kind,
                subject_id=signal.subject_id,
                signal_type=signal.signal_type,
                change_class=signal.change_class,
                detected_at=detected_at,
            )
        )
        recorded += 1
    return recorded
