"""R11-FP02: what one discovery run actually took in, facet by facet -- its scope receipt.

A run's counters (`discovered_tables`, `created_objects`, ...) say how much; they do not say what
kind, or how completely. "We scanned the source" and "we scanned the source but its routine
bodies were withheld from our principal" read the same in those counters. The receipt keeps them
apart:

* per object kind, how many the source returned into scope, how many the selection left
  out (R11-FP01), and how many exist that this run's login may not see at all. On a pull run
  `excluded` counts objects the selection kept Atlas from reading; on a pushed snapshot it
  counts objects a sender delivered that the selection kept Atlas from persisting
  (`batch_ingestion`). Both are "the source offered it and this scan did not take it in",
  which is why they share the counter, and `mode` plus the `canonical_push` capability say
  which path a reader is looking at;
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

from aida.capability_states import (
    REASON_ADAPTER_NOT_IMPLEMENTED,
    REASON_SOURCE_RETURNED_NO_TEXT,
    REASON_SOURCE_TEXT_TRUNCATED,
    CapabilityState,
    reason_code,
)
from aida.connectors.base import DiscoveredCatalog
from aida.connectors.discovery import (
    DISCOVERY_FACETS,
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INDEXES,
    FACET_OBJECT_COMMENTS,
    FACET_PARTITIONS,
)
from aida.connectors.discovery import (
    # Re-exported deliberately (the `as` form is what strict mypy reads as an
    # explicit re-export). `batch_ingestion` imports nothing else from
    # `connectors` -- a pushed snapshot has no connector -- so the receipt is the
    # right place for the push path to read the facet vocabulary from.
    FACET_SEQUENCES as FACET_SEQUENCES,
)
from aida.connectors.discovery import (
    FACET_TRIGGERS as FACET_TRIGGERS,
)
from aida.discovery_selection import routine_kind, table_kind

#: 2 (R11-FP02) adds `invisible` to every kind. A reader of a version-1 receipt finds the
#: key absent, which reads the same as `null` does: that run never asked.
#:
#: 3 (review 2026-09-16 §5) adds `state` and `reason` to every facet, in the shared
#: vocabulary `aida.capability_states` defines, plus an `object_visibility` facet. Every
#: version-2 key keeps its meaning and its value -- `support`, `captured`, `withheld`,
#: `truncated` are untouched -- so this is purely additive: a version-2 reader sees no
#: change, and a version-3 reader of a version-2 receipt finds the new keys absent, which
#: reads as "that run did not distinguish these outcomes".
RECEIPT_VERSION: Final = 3
STREAM_IN_PROGRESS: Final = "IN_PROGRESS"
STREAM_COMPLETE: Final = "COMPLETE"
STREAM_INTERRUPTED: Final = "INTERRUPTED"

# ---------------------------------------------------------------------------
# R11-FP01: triggers and sequences are facets in their own right.
#
# The question the peer who added the two axes left open -- "`DISCOVERY_FACETS`
# will want `triggers`/`sequences` entries once the receipt grows those facets"
# -- is answered yes, on this module's own definition of a facet: a facet is one
# read of the source, against its own catalog relation, that can succeed, be
# refused or be unimplemented independently of every other read. A trigger read
# is `pg_trigger` / `sys.triggers` / `ALL_TRIGGERS`; a sequence read is
# `pg_sequence` / `sys.sequences` / `ALL_SEQUENCES`. Neither is a by-product of
# the roster, and each already has its own flag on `ConnectorCapabilities`.
#
# Two facets and not one, for the reason `ConnectorCapabilities.triggers` /
# `.sequences` are two flags: Snowflake has sequences and no trigger object at
# all, so a single `native_objects` facet could only be honest on the engines
# that happen to have both, and Snowflake would read UNSUPPORTED for a kind it
# does support.
#
# **They are flagged facets, not code facets.** `view_definitions` and
# `routine_bodies` publish `captured` / `withheld` / `truncated`, and a trigger
# body looks superficially like it wants the same three counters. It must not
# have them: on PostgreSQL a trigger has *no body to give* -- the action is
# `EXECUTE FUNCTION f()` and `f`'s body arrives on the routine axis -- so every
# PostgreSQL trigger would count as `withheld`, the facet would read UNAVAILABLE
# on every scan, and an administrator would go hunting for a grant that does not
# exist. That per-engine truth is already published where it can be told
# properly, as `PARTIAL` with its reason on the engine capability matrix
# (`discovery_selection._trigger_definition_state`), and per trigger as
# `MetadataTrigger.availability` + `unavailable_reason`. A sequence has no text
# at all and so could never have had them.
#
# **What is deliberately not done here.** `DISCOVERY_FACETS` itself lives in
# `connectors.discovery`, which is another session's this cycle, so no connector
# can yet attribute a *refused* trigger read to this facet -- `FacetReadScope.record`
# validates against that frozenset. The receipt accepts the outcome
# (`RECEIPT_FACETS` below) and the reconciliation honours it
# (`workflows.activities._FACET_AXES`), so the protection is in place the moment
# that one entry lands. Until it does, a refused trigger read raises out of
# `discover_streaming` and fails the run -- which reconciles nothing and so
# retires nothing, the same safe direction `RETIREMENT_BEARING_FACETS` relies on.
# (Both now come from `connectors.discovery` with the other eight -- see the
# import at the top of this module. They were declared here while that module
# belonged to another session; the entry it was waiting for has landed.)

#: Facets a connector reports by capability flag alone -- Atlas counts no withheld share for them.
#: Taken from `connectors.discovery`'s own names so the facet a connector attributes a refused
#: read to and the facet this receipt publishes can never drift apart; for these five the facet
#: name is also the capability flag's name, which is what `support()` below relies on. The two
#: R11-FP01 names above obey that same rule -- they *are* the `ConnectorCapabilities` field
#: names -- which is what lets them join this tuple rather than needing a branch of their own.
_FLAGGED_FACETS: Final = (
    FACET_CONSTRAINTS,
    FACET_INDEXES,
    FACET_PARTITIONS,
    FACET_GRANTS,
    FACET_OBJECT_COMMENTS,
    FACET_TRIGGERS,
    FACET_SEQUENCES,
)

#: R11-FP02 / review 2026-09-16 §5: the facet name under which a run records how much of the
#: source its own login could not see at all. Not a capability flag -- there is no
#: `visibility` on `ConnectorCapabilities` -- so it is named here, beside the facet it reports.
#: Not in `DISCOVERY_FACETS` either: it is a question *about* the catalog rather than one of the
#: reads that builds it, and no connector attributes a facet read to it.
FACET_OBJECT_VISIBILITY: Final = "object_visibility"

#: Every facet name this receipt will record an outcome for. A refusal recorded against a name
#: outside this set would be accepted and then quietly dropped by `as_json`, which is the one
#: failure mode a receipt must not have, so `record_facet_outcome` refuses it instead.
RECEIPT_FACETS: Final[frozenset[str]] = DISCOVERY_FACETS | {
    FACET_OBJECT_VISIBILITY,
    FACET_TRIGGERS,
    FACET_SEQUENCES,
}


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
    #: R11-FP15: change signals this run recorded, by signal type.
    changes: dict[str, int] = field(default_factory=dict)
    #: R11-FP01: whether the connector took the selection's schema scope into its own queries.
    selection_pushed_down: bool = False
    #: R11-FP02: per kind, how many objects in scope this run's login may not see; `None` where
    #: the source cannot be asked, which is not the same as none hidden.
    invisible: Mapping[str, int] | None = None
    #: Review 2026-09-16 §5: facet -> (state, reason code) for a facet read this run could not
    #: complete. Written by `record_facet_outcome`; empty for a run in which every facet the
    #: connector collects came back.
    facet_outcomes: dict[str, tuple[str, str]] = field(default_factory=dict)

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
                # R11-FP01: counted here for the same reason every other kind is, and
                # the reason is sharper for these two. `apply_selection` already counts
                # an excluded TRIGGER and SEQUENCE, so without this a scoped run
                # published `{"discovered": 0, "excluded": 7}` for a kind it had read
                # seven of -- and a reader has no way to tell that from a source with
                # none. A count is the only thing that tells "we looked and there were
                # none" apart from "nobody counted"; `excluded` has no such ambiguity
                # because it is only ever written by the pass that does the excluding.
                #
                # No code counters go with them: see FACET_TRIGGERS above for why a
                # trigger body's captured/withheld share would misreport PostgreSQL.
                #
                # Counted through `update` rather than `+= len(...)`, exactly as
                # `discovery_selection.apply_selection` counts the excluded side: on a
                # `Counter` the latter materialises a zero entry, so every Snowflake and
                # BigQuery run -- engines with no trigger object at all -- would publish
                # a `TRIGGER` row reading `discovered: 0`. An absent kind row is how this
                # receipt already says "nothing to report" (a source with no
                # materialized views has no MATERIALIZED_VIEW row), and
                # `facets.triggers.support` is what tells "this adapter does not collect
                # them" apart from "it collected none" -- INV-9's absent-rather-than-empty
                # rule, in the one place a zero would have quietly broken it.
                self.discovered.update({"TRIGGER": len(schema.triggers)} if schema.triggers else {})
                self.discovered.update(
                    {"SEQUENCE": len(schema.sequences)} if schema.sequences else {}
                )

    def record_reconciliation(self, *, deprecated: int, retained_out_of_scope: int) -> None:
        self.reconciliation = {
            "performed": True,
            "deprecated": deprecated,
            "retained_out_of_scope": retained_out_of_scope,
        }

    def record_changes(self, counts: Mapping[str, int]) -> None:
        self.changes = {signal_type: counts[signal_type] for signal_type in sorted(counts)}

    def record_facet_outcome(
        self, facet: str, *, state: CapabilityState, reason: str | None
    ) -> None:
        """Record that one facet's read did not complete, and why -- without failing the run.

        Review 2026-09-16 §5 asks for `PERMISSION_DENIED` as a distinguishable outcome, and
        tracker R11-FP01/FP02 both name its absence: until now a facet the source refused
        either failed the whole scan or was indistinguishable from a facet that came back
        empty. A refused facet recorded here leaves the rest of the run to read what it is
        allowed to, which is the whole point -- a login granted one schema of five should
        produce a receipt that says so, not a failed run.

        `reason` passes through the closed vocabulary in `aida.capability_states`, so a
        driver's own message can never land in a receipt (INV-6): a code cannot carry a
        value, and a source's explanation of what it withheld routinely quotes one.

        `facet` is checked against `RECEIPT_FACETS` because the alternative is the one
        thing worse than an unrecorded refusal: a recorded one that `as_json` never
        publishes, so the run looks clean and the facet looks empty.
        """
        if facet not in RECEIPT_FACETS:
            raise ValueError(f"unknown receipt facet: {facet}")
        self.facet_outcomes[facet] = (state.value, reason_code(reason))

    def as_json(self, state: str) -> dict[str, Any]:
        def support(flag: str) -> str:
            return "SUPPORTED" if self.capabilities.get(flag) else "UNSUPPORTED"

        def recorded(facet: str) -> tuple[str, str] | None:
            return self.facet_outcomes.get(facet)

        def facet_state(facet: str, flag: str, counter: Counter[str] | None = None) -> str:
            """One facet's state in the shared vocabulary, by a documented precedence.

            An adapter that does not collect the facet outranks everything: there was no
            read to refuse. Then a recorded refusal or failure, because nothing arrived.
            Then truncation, because part of the text arrived and anything derived from it
            is incomplete by construction. Then a withheld share, which is PARTIAL -- some
            objects' text came back and some did not. `support` and the three counters
            beside this key are unchanged, so an existing reader of a receipt sees exactly
            what it saw before and this is an additive key.
            """
            if not self.capabilities.get(flag):
                return CapabilityState.UNSUPPORTED.value
            outcome = recorded(facet)
            if outcome is not None:
                return outcome[0]
            if counter is None:
                return CapabilityState.SUPPORTED.value
            if counter.get("truncated", 0):
                return CapabilityState.TRUNCATED.value
            if counter.get("withheld", 0):
                return (
                    CapabilityState.PARTIAL.value
                    if counter.get("captured", 0)
                    else CapabilityState.UNAVAILABLE.value
                )
            return CapabilityState.SUPPORTED.value

        def facet_reason(facet: str, counter: Counter[str] | None = None) -> str | None:
            outcome = recorded(facet)
            if outcome is not None:
                return outcome[1]
            if facet == FACET_OBJECT_VISIBILITY and self.invisible is None:
                # `Connector.count_invisible_objects` answers None where the adapter has no
                # unfiltered catalog to ask. That is not a refusal and not a false zero.
                return REASON_ADAPTER_NOT_IMPLEMENTED
            if counter is None:
                return None
            if counter.get("truncated", 0):
                return REASON_SOURCE_TEXT_TRUNCATED
            if counter.get("withheld", 0):
                return REASON_SOURCE_RETURNED_NO_TEXT
            return None

        def code(counter: Counter[str], flag: str, facet: str) -> dict[str, Any]:
            return {
                "support": support(flag),
                "captured": counter.get("captured", 0),
                "withheld": counter.get("withheld", 0),
                "truncated": counter.get("truncated", 0),
                "state": facet_state(facet, flag, counter),
                "reason": facet_reason(facet, counter),
            }

        def flagged(facet: str) -> dict[str, Any]:
            return {
                "support": support(facet),
                "state": facet_state(facet, facet),
                "reason": facet_reason(facet),
            }

        if self.reconciliation is not None:
            reconciliation = self.reconciliation
        else:
            # Only a FULL run that saw its whole stream may reconcile what it did not see.
            reason = "INCREMENTAL_MODE" if self.mode != "FULL" else "STREAM_NOT_FINISHED"
            reconciliation = {"performed": False, "reason": reason}
        invisible = self.invisible
        kinds = sorted(set(self.discovered) | set(self.excluded) | set(invisible or {}))
        return {
            "receipt_version": RECEIPT_VERSION,
            "mode": self.mode,
            "selection_fingerprint": self.selection_fingerprint,
            "selection_pushed_down": self.selection_pushed_down,
            "stream": {"state": state, "batches": self.batches},
            "kinds": {
                kind: {
                    "discovered": self.discovered.get(kind, 0),
                    "excluded": self.excluded.get(kind, 0),
                    # R11-FP02: `None` is "the source cannot say", never zero.
                    "invisible": (None if invisible is None else invisible.get(kind, 0)),
                }
                for kind in kinds
            },
            "facets": {
                "inventory": {
                    "support": "SUPPORTED",
                    "state": (
                        self.facet_outcomes["inventory"][0]
                        if "inventory" in self.facet_outcomes
                        else CapabilityState.SUPPORTED.value
                    ),
                    "reason": facet_reason("inventory"),
                },
                "view_definitions": code(self.view_definitions, "views", "view_definitions"),
                "routine_bodies": code(self.routine_bodies, "routines", "routine_bodies"),
                **{facet: flagged(facet) for facet in _FLAGGED_FACETS},
                # R11-FP02: not a capability flag. `invisible` above carries the counts;
                # this says whether the question could be put to the source at all, and
                # since review 2026-09-16 tells a refusal apart from an inability to ask.
                FACET_OBJECT_VISIBILITY: {
                    "state": self.facet_outcomes.get(
                        FACET_OBJECT_VISIBILITY,
                        (
                            CapabilityState.SUPPORTED.value
                            if self.invisible is not None
                            else CapabilityState.UNAVAILABLE.value,
                            "",
                        ),
                    )[0],
                    "reason": facet_reason(FACET_OBJECT_VISIBILITY),
                    "asked": self.invisible is not None,
                },
            },
            "reconciliation": reconciliation,
            "changes": dict(self.changes),
        }
