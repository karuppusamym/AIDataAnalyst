"""Backward-compatible re-export shim.

Canonical location: `atlas.modules.catalog.service` (the "Bulk actions"
section at the bottom of that file), moved under tracker ST-07 Commit B
(Phase 5 of `Docs/40-engineering/06-refactor-plan.md`) on 2026-09-03.
Every existing `from aida.catalog_bulk_actions import ...` caller keeps
working unchanged.

Externally-used symbols at the time of the move, one line per caller so a
future reader can trace who depended on what without re-grepping:

* `aida.api` -- `CATALOG_BULK_ACTION_MAX_ITEMS`, `CATALOG_BULK_FILTER_SCAN_CAP`,
  `BulkItemResult`, `BulkPlan`, `CatalogBulkItemError`,
  `apply_certify_item`, `apply_classify_item`, `apply_own_item`,
  `apply_tag_item`, `dedupe_preserving_order`, `match_columns_by_pattern`,
  `match_tables_by_filter`. The catalog endpoints that dispatch to these
  are themselves scheduled to move to `atlas.modules.catalog.router` under
  ST-07 Commit C.
* `aida.playbooks` -- same set as `aida.api` minus `dedupe_preserving_order`.
* `aida.playbooks_api` -- `ALLOWED_CLASSIFICATIONS`.
* `aida.schemas` -- `ALLOWED_CLASSIFICATIONS`, `CATALOG_BULK_ACTION_MAX_ITEMS`.
* `aida.stewardship_service` -- `CatalogBulkItemError`, `apply_classify_item`,
  `apply_tag_item`.
* `tests/test_catalog_bulk_actions.py` -- `CATALOG_BULK_ACTION_MAX_ITEMS`,
  `CatalogBulkItemError`, `apply_certify_item`, `apply_classify_item`,
  `apply_own_item`, `apply_tag_item`, `dedupe_preserving_order`,
  `match_columns_by_pattern`, `match_tables_by_filter`.
* `tests/test_catalog_bulk_actions_endpoints.py` -- `CATALOG_BULK_ACTION_MAX_ITEMS`.

New code should import from `atlas.modules.catalog.service` directly.
"""

from atlas.modules.catalog.service import (
    ALLOWED_CLASSIFICATIONS,
    CATALOG_BULK_ACTION_MAX_ITEMS,
    CATALOG_BULK_FILTER_SCAN_CAP,
    BulkItemResult,
    BulkPlan,
    CatalogBulkItemError,
    apply_certify_item,
    apply_classify_item,
    apply_own_item,
    apply_tag_item,
    dedupe_preserving_order,
    match_columns_by_pattern,
    match_tables_by_filter,
)

__all__ = [
    "ALLOWED_CLASSIFICATIONS",
    "CATALOG_BULK_ACTION_MAX_ITEMS",
    "CATALOG_BULK_FILTER_SCAN_CAP",
    "BulkItemResult",
    "BulkPlan",
    "CatalogBulkItemError",
    "apply_certify_item",
    "apply_classify_item",
    "apply_own_item",
    "apply_tag_item",
    "dedupe_preserving_order",
    "match_columns_by_pattern",
    "match_tables_by_filter",
]
