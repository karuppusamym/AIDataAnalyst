"""R11-FP02: what one discovery run actually took in, facet by facet -- its scope receipt.

A run's counters (`discovered_tables`, `created_objects`, ...) say how much; they do not say what
kind, or how completely. "We scanned the source" and "we scanned the source but its routine
bodies were withheld from our principal" read the same in those counters. The receipt keeps them
apart:

* per object kind, how many the source returned into scope and how many the selection left out
  (R11-FP01);
* per facet, whether the connector collects it at all (`SUPPORTED` / `UNSUPPORTED`, from its own
  capability flags, which INV-9 keeps honest) and, for code -- view definitions and routine
  bodies -- how much arrived, how much was withheld, and how much was truncated;
* whether the stream finished, and what reconciliation did with what it did not see.

It is value-free: counts, codes and a fingerprint, never a name or a body. It is written after
every committed batch, so a run that is interrupted keeps an honest `IN_PROGRESS` or
`INTERRUPTED` receipt, and retrying goes through the existing resume path
(`POST /v1/analysis-runs/{id}/resume`), which reserves a new run with a receipt of its own
instead of rewriting this one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from aida.connectors.base import DiscoveredCatalog
from aida.discovery_selection import routine_kind, table_kind

RECEIPT_VERSION: Final = 1
STREAM_IN_PROGRESS: Final = "IN_PROGRESS"
STREAM_COMPLETE: Final = "COMPLETE"
STREAM_INTERRUPTED: Final = "INTERRUPTED"

#: Facets a connector reports by capability flag alone -- Atlas counts no withheld share for them.
_FLAGGED_FACETS: Final = ("constraints", "indexes", "partitions", "grants", "object_comments")


def _count_code(counter: Counter[str], text: str | None, truncated: bool) -> None:
    if text is None:
        counter["withheld"] += 1
        return
    counter["captured"] += 1
    if truncated:
        counter["truncated"] += 1


@dataclass(slots=True)
class DiscoveryReceipt:
    mode: str
    selection_fingerprint: str | None
    capabilities: Mapping[str, Any]
    batches: int = 0
    discovered: Counter[str] = field(default_factory=Counter)
    excluded: Counter[str] = field(default_factory=Counter)
    view_definitions: Counter[str] = field(default_factory=Counter)
    routine_bodies: Counter[str] = field(default_factory=Counter)
    reconciliation: dict[str, Any] | None = None

    def observe_batch(
        self, catalogs: Iterable[DiscoveredCatalog], excluded: Mapping[str, int]
    ) -> None:
        """Count one batch as persisted -- after the selection has been applied to it."""
        self.batches += 1
        self.excluded.update(excluded)
        for catalog in catalogs:
            for schema in catalog.schemas:
                self.discovered["SCHEMA"] += 1
                for table in schema.tables:
                    kind = table_kind(table.object_type)
                    self.discovered[kind] += 1
                    if kind != "TABLE":
                        definition = table.view_definition
                        _count_code(
                            self.view_definitions,
                            definition.definition_sql if definition else None,
                            bool(definition and definition.truncated),
                        )
                for routine in schema.routines:
                    self.discovered[routine_kind(routine.routine_type)] += 1
                    _count_code(self.routine_bodies, routine.body_sql, routine.truncated)

    def record_reconciliation(self, *, deprecated: int, retained_out_of_scope: int) -> None:
        self.reconciliation = {
            "performed": True,
            "deprecated": deprecated,
            "retained_out_of_scope": retained_out_of_scope,
        }

    def as_json(self, state: str) -> dict[str, Any]:
        def support(flag: str) -> str:
            return "SUPPORTED" if self.capabilities.get(flag) else "UNSUPPORTED"

        def code(counter: Counter[str], flag: str) -> dict[str, Any]:
            return {
                "support": support(flag),
                "captured": counter.get("captured", 0),
                "withheld": counter.get("withheld", 0),
                "truncated": counter.get("truncated", 0),
            }

        if self.reconciliation is not None:
            reconciliation = self.reconciliation
        else:
            # Only a FULL run that saw its whole stream may reconcile what it did not see.
            reason = "INCREMENTAL_MODE" if self.mode != "FULL" else "STREAM_NOT_FINISHED"
            reconciliation = {"performed": False, "reason": reason}
        kinds = sorted(set(self.discovered) | set(self.excluded))
        return {
            "receipt_version": RECEIPT_VERSION,
            "mode": self.mode,
            "selection_fingerprint": self.selection_fingerprint,
            "stream": {"state": state, "batches": self.batches},
            "kinds": {
                kind: {
                    "discovered": self.discovered.get(kind, 0),
                    "excluded": self.excluded.get(kind, 0),
                }
                for kind in kinds
            },
            "facets": {
                "inventory": {"support": "SUPPORTED"},
                "view_definitions": code(self.view_definitions, "views"),
                "routine_bodies": code(self.routine_bodies, "routines"),
                **{facet: {"support": support(facet)} for facet in _FLAGGED_FACETS},
            },
            "reconciliation": reconciliation,
        }
