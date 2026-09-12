"""Backward-compatible re-export shim.

Canonical location: `atlas.modules.observability_audit.router`, moved under
ST-07 Commit C for the observability_audit bounded context (analog of the
catalog module's own Commit C) on 2026-09-03. Every existing
`from aida.observability_api import ...` caller keeps working unchanged.

Externally-used symbols at the time of the move:

* `router` -- `aida.main` (mounts it on the app).
* `get_cost_showback` -- `tests/test_cost_showback.py` imports the handler
  function directly to unit-test the response shape without a HTTP round trip.
* `get_archive_status` -- `tests/test_worm_archive_wiring.py` imports the
  handler function directly to unit-test the wiring against a live
  `AuditArchiveRecord` row.

The three SLO handler functions (`create_slo_definition`,
`list_slo_definitions`, `get_slo_budget`) were re-exported here too, for a
future test that might want to bypass HTTP. They were retired with the rest of
the SLO feature on 2026-09-12 (R11-D10) -- nothing ever wrote `slo_measurement`
and no indicator source existed to write it from.

New code should import from `atlas.modules.observability_audit.router`
directly.
"""

from atlas.modules.observability_audit.router import (
    get_archive_status,
    get_cost_showback,
    router,
)

__all__ = [
    "router",
    "get_archive_status",
    "get_cost_showback",
]
