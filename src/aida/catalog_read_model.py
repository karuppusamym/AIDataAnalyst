"""Backward-compatible re-export shim.

Canonical location: `atlas.modules.catalog.service` (public composer) plus
`atlas.modules.catalog.repository` (the `_prefixed` batch helpers), moved
under tracker ST-07 (Phase 5 of `Docs/40-engineering/06-refactor-plan.md`)
on 2026-09-03. Every existing `from aida.catalog_read_model import ...`
caller keeps working unchanged.

Externally-used symbols at the time of the move, one line per caller so a
future reader can trace who depended on what without re-grepping:

* `compose_catalog_rows` -- `aida.api` (the `/v1/organizations/{id}/catalog/rows`
  endpoint's read-model). Public.
* `_business_annotations`, `_description`, `_latest_approved_documentation`,
  `_latest_pending_drafts` -- `aida.stewardship_api`. Still `_prefixed` here
  and in the canonical location so this shim's re-export shape matches the
  original module's shape; a follow-up (not this commit) may rename them
  public (e.g. `latest_business_annotations`) and move them onto
  `atlas.modules.catalog.api` as first-class contract, at which point this
  shim can shrink.
* `_business_annotations`, `_certification_state`, `_description`,
  `_earliest_active_owners`, `_glossary_terms_by_table`,
  `_latest_approved_documentation`, `_latest_certifications`,
  `_latest_observation_at`, `_latest_pending_drafts`,
  `_open_incident_table_ids`, `_quality_state` -- `aida.asset_evidence`
  (the AT-6 evidence pane), same reasoning.
* `_certification_state`, `_earliest_active_owners`,
  `_latest_approved_documentation`, `_latest_certifications`,
  `_latest_observation_at`, `_open_incident_table_ids`, `_quality_state`
  -- `aida.asset_context` (the agent-context composer), same reasoning.
* `_latest_observation_at`, `_open_incident_table_ids`, `_quality_state`
  -- `aida.unified_lineage_api`, same reasoning.

New code should import from `atlas.modules.catalog.service` (the public
composer) directly. The `_prefixed` helpers stay reachable via this shim
only until the follow-up above renames them public.
"""

from atlas.modules.catalog.repository import (
    _business_annotations,
    _earliest_active_owners,
    _glossary_terms_by_table,
    _latest_approved_documentation,
    _latest_certifications,
    _latest_observation_at,
    _latest_pending_drafts,
    _open_incident_table_ids,
)
from atlas.modules.catalog.service import (
    _certification_state,
    _description,
    _quality_state,
    compose_catalog_rows,
)

__all__ = [
    "compose_catalog_rows",
    # `_prefixed` helpers re-exported for the four external callers named
    # above; kept exported until the follow-up rename lands. Listed here so
    # a future importer sees they are deliberately part of the shim's
    # published surface rather than incidental imports.
    "_business_annotations",
    "_certification_state",
    "_description",
    "_earliest_active_owners",
    "_glossary_terms_by_table",
    "_latest_approved_documentation",
    "_latest_certifications",
    "_latest_observation_at",
    "_latest_pending_drafts",
    "_open_incident_table_ids",
    "_quality_state",
]
