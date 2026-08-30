"""CT-5: asset certification lifecycle with expiry (module 04 catalog / module 08 stewardship).

``AssetCertification`` already existed as GL-5's reviewed bulk table certification
plumbing, and CT-1 added an immediate per-table bulk certify path
(``aida.catalog_bulk_actions.plan_certify``). Both left two gaps against module
04's own public interface (``certify_asset(scope, table_id, decision) ->
AssetCertification``, ``POST /v1/tables/{id}/certification``):

* certification could only ever target a table, never a column, even though
  column is the dominant catalog entity (module 04's scale note: ~30 columns
  per table);
* "is this asset currently certified" was re-derived ad hoc at each call site
  (``status == "ACTIVE" and expires_at > now``), which is easy to get half
  right -- CT-1's own bulk-certify supersede lookup only checked ``status``.

This module centralizes the query-time active-certification projection,
mirroring ``aida.tool_certification.certification_is_active`` for tool version
certification: a certification that was granted keeps reading back with
``status == "ACTIVE"`` after ``expires_at`` passes (the row is retained audit
evidence, never mutated by a background job), so every caller must apply this
projection rather than trusting the raw status column.
"""

from datetime import UTC, datetime
from typing import Protocol


class AssetCertificationLike(Protocol):
    status: str
    expires_at: datetime


def asset_certification_is_active(
    certification: AssetCertificationLike, *, at: datetime | None = None
) -> bool:
    """Whether a certification row currently counts as an active certification."""
    moment = at or datetime.now(UTC)
    return certification.status == "ACTIVE" and certification.expires_at > moment


def current_asset_certification[T: AssetCertificationLike](
    certifications: list[T], *, at: datetime | None = None
) -> T | None:
    """The certification that is currently active among a set of rows, if any.

    ``certifications`` should already be ordered newest-first (e.g. by
    ``created_at desc``); this returns the first one that is still active, so a
    fresh recertification naturally wins without mutating older rows.
    """
    for certification in certifications:
        if asset_certification_is_active(certification, at=at):
            return certification
    return None
