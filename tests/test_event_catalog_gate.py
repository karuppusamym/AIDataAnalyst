"""TS-11: Event-catalog CI gate.

Fails the build the moment a *new* ``event_type=`` shows up in ``src/`` that is neither
documented in ``Docs/30-contracts/04-event-catalog.md`` nor already named in the
``KNOWN_ST14_DRIFT`` baseline below -- see :mod:`event_catalog_lib` for how both sides are
read.

Why a baseline instead of a single hard assertion: a documentation-truth pass across this repo
(``Docs/review-2026-08/gap/04-documentation-truth-pass.md`` §3) found that most of the catalog
predates a repo-wide switch to ``.v1``-suffixed event names, so the bulk of today's emitted
event types are *renames* of an already-documented concept (`datasource.registered` ->
`datasource.registered.v1`, `certification.completed` -> `connector.certification.completed.v1`,
etc.) or a consolidation of several documented rows into one (`quality.incident_opened` /
`.reopened` / `.acknowledged` / `.resolved` / `.auto_recovered` -> the single
`data_quality.incident.transitioned.v1`). Picking the surviving name is an authorial call --
tracked as ST-14 -- not something this gate should guess at. Renaming the `event_type=` string
in `src/` to match a guess, or inventing a second catalog row for what might just be the same
event under a new name, would both be worse than leaving it alone.

So this gate enforces the tracker item's exit condition ("every event_type= published from
src/ appears in the catalog") going forward: anything already known and named below is treated
as pre-existing ST-14 debt, and anything new must be either documented (the usual case -- add a
row to the catalog) or, if it is genuinely the same rename/consolidation situation, added here
with a citation. What it does NOT do is let a brand new, never-before-seen event type slip in
undocumented and undocumented-forever the way the previous ones did.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from event_catalog_lib import parse_catalog_event_names, scan_emitted_event_types

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
CATALOG_PATH = REPO_ROOT / "Docs" / "30-contracts" / "04-event-catalog.md"

# --------------------------------------------------------------------------------------------
# ST-14 baseline: event types emitted today that are *not* literally in the catalog, but that a
# documentation-truth pass traced to a specific existing row (or set of rows) they are a rename
# or consolidation of. Do not add to this list to make a newly-introduced, genuinely
# undocumented event pass the gate -- document it in the catalog instead. Only add here (with a
# reason citing the row(s) it collides with) when the same "authorial rename decision" situation
# applies, and remove an entry once ST-14 resolves it one way or the other.
# --------------------------------------------------------------------------------------------
_ANALYSIS_RUN_LIFECYCLE = (
    "the documented `analysis_run.started / .completed / .cancelled` lifecycle"
)

KNOWN_ST14_DRIFT: dict[str, str] = {
    # --- tenancy hierarchy: the generic `tenant.created` row was replaced by one specific
    # event per level, never reconciled ---
    "organization.created.v1": "same event as documented `tenant.created` (org level)",
    "line_of_business.created.v1": "same event as documented `tenant.created` (LOB level)",
    "project.created.v1": "same event as documented `tenant.created` (project level)",
    # --- straight .v1 renames of an already-documented event ---
    "datasource.registered.v1": "same event as documented `datasource.registered`",
    "catalog.asset.certified.v1": "same event as documented `catalog.asset.certified`",
    "connector.certification.completed.v1": (
        "same event as documented `certification.completed`"
    ),
    "model_route.created.v1": "same event as documented `model.route_version_created`",
    "model_route.approved.v1": (
        "same event as documented `model.route_version_created / .approved`"
    ),
    "model_route.rejected.v1": "reject sibling of the model-route-approval rename above",
    "tool.version.draft_created.v1": "same event as documented `tool.drafted`",
    "tool.version.published.v1": "same event as documented `tool.drafted / .published`",
    "tool.version.deprecated.v1": "same event as documented `tool.drafted / .deprecated`",
    "tool.version.rejected.v1": (
        "reject sibling with no documented counterpart; same family as the "
        "tool-lifecycle rename above"
    ),
    "tool.version.deprecation_rejected.v1": (
        "reject sibling with no documented counterpart; same family as the "
        "tool-lifecycle rename above"
    ),
    "tool.execution.completed.v1": "same event as documented `tool.invoked`",
    "semantic_model.published.v1": "same event as documented `semantic.version_published`",
    "semantic_model.rejected.v1": (
        "reject sibling with no documented counterpart; same family as the "
        "semantic-model-publish rename above"
    ),
    "agent.analysis.completed.v1": (
        "same aggregate/lifecycle as documented "
        "`agent.run_started / .run_completed / .run_denied`"
    ),
    "query.execution.completed.v1": "same event as documented `execution.completed`",
    "query.feedback.updated.v1": "same event as documented `agent.feedback_recorded`",
    # RL-4 (2026-08-30): the single consolidated `relationship_candidate.decided.v1`
    # above was split back into two literal event types because
    # `aida.projectors.graph_projector.run_projector` already listened for exactly
    # these two names to trigger `project_unified_lineage` and they never matched --
    # decided candidates were silently never projected to Neo4j. Each is the
    # approve/reject sibling of the documented `relationship.approved` /
    # `relationship.rejected` rows.
    "relationship_candidate.approved.v1": "same event as documented `relationship.approved`",
    "relationship_candidate.rejected.v1": "same event as documented `relationship.rejected`",
    "business_semantics.proposals_created.v1": (
        "same event as documented `semantic.inference_completed` "
        "(run_id/proposal_count payload matches)"
    ),
    "business_semantics.approved.v1": (
        "same event as documented `semantic.annotation_published` "
        "(annotation_id/table_id payload matches)"
    ),
    "business_semantics.rejected.v1": (
        "reject sibling with no documented counterpart; same family as the "
        "annotation-publish rename above"
    ),
    "dbt_artifact.imported.v1": (
        "same event as documented `lineage.artifact_ingested` (dbt manifest case)"
    ),
    "openlineage.run_event.ingested.v1": (
        "same event as documented `lineage.artifact_ingested` (OpenLineage case)"
    ),
    "metadata.discovery.snapshot.v1": "same event as documented `ingestion.delivered`",
    "metadata.ingestion.batch.queued.v1": "same event as documented `batch.finalized`",
    "data_quality.incident.transitioned.v1": (
        "consolidates documented `quality.incident_opened` / `.incident_reopened` / "
        "`.incident_acknowledged` / `.resolved` / `.incident_auto_recovered` into one "
        "event with a status field"
    ),
    # --- analysis_run lifecycle: the documented row only names started/completed/cancelled;
    # code now emits a larger, differently-prefixed state set for the same aggregate ---
    "analysis_run.requested.v1": f"pre-`started` state of {_ANALYSIS_RUN_LIFECYCLE}",
    "analysis_run.scheduled.v1": f"pre-`started` state of {_ANALYSIS_RUN_LIFECYCLE}",
    "analysis_run.resumed.v1": f"additional state of {_ANALYSIS_RUN_LIFECYCLE}",
    "analysis_run.cancellation_requested.v1": f"additional state of {_ANALYSIS_RUN_LIFECYCLE}",
    "metadata.analysis.completed.v1": "renamed `analysis_run.completed`",
    "metadata.analysis.cancelled.v1": "renamed `analysis_run.cancelled`",
    "metadata.analysis.failed.v1": (
        "additional terminal state of the same renamed analysis_run lifecycle"
    ),
    "metadata.analysis.cancellation_race_completed.v1": (
        "additional terminal state of the same renamed analysis_run lifecycle"
    ),
    # --- added 2026-08-30: concurrent work landed a `.v1` (or re-prefixed) sibling of an
    # already-documented event without reconciling the older row, same pattern as above ---
    "context.product_consumed.v1": (
        "same event as documented `context.product_consumed`, emitted via the MCP/REST "
        "read paths that were added after that row was written"
    ),
    "context.product_consumption_denied.v1": (
        "same event as documented `context.consumption_denied`"
    ),
    "data_quality.incident_opened": "same event as documented `quality.incident_opened`",
    "data_quality.incident_resolved": (
        "same event as documented `quality.incident_acknowledged` / `.resolved`"
    ),
    # --- RL-2/RL-3 (module 06 canonical resolution, composite candidates): same
    # `.v1`-suffix drift as the rest of this baseline. RL-1 (table family
    # detection) is not here: it shipped as `table_family_candidate.decided.v1`,
    # already directly documented -- no drift entry needed. ---
    "canonical_table.resolved.v1": "same event as documented `canonical_table.resolved`",
    "composite_relationship_candidate.decided.v1": (
        "composite-candidate sibling of the already-documented-as-drift "
        "`relationship_candidate.decided.v1` (itself a consolidation of `relationship.approved` "
        "/ `.rejected`); same decided-with-status-field shape, multi-column candidates instead "
        "of single-column"
    ),
}

# Sites where `event_type=` could not be fully resolved to a closed set of string literals by
# static analysis (see event_catalog_lib docstring for what counts as resolvable). Reported so
# they are never silently invisible to whoever reads this gate's output -- see
# test_non_literal_event_type_sites_are_reported below. Manually traced at the time this gate
# was written (by following the called helper functions by hand, not by the scanner) to:
# glossary.conflict_resolved.v1, glossary.conflict_resolution_rejected.v1,
# glossary.link_proposal_approved.v1, glossary.link_proposal_rejected.v1 (stewardship_service.py
# apply_conflict_resolution/reject_conflict_resolution -- already documented), and
# ownership.assigned.v1, glossary.term_linked_bulk.v1, glossary.term_deprecated.v1,
# certification.granted.v1 (stewardship_service.py apply_bulk_operation -- already documented).
# None of those were new/undocumented, but the gate cannot prove that on its own, hence the
# permanent warning below.
EXPECTED_UNRESOLVED_SITE_COUNT = 1


def test_every_emitted_event_type_is_documented_or_known_st14_drift() -> None:
    scan = scan_emitted_event_types(SRC_ROOT)
    documented = parse_catalog_event_names(CATALOG_PATH)

    undocumented = scan.literals - documented - KNOWN_ST14_DRIFT.keys()
    assert not undocumented, (
        "The following event_type= value(s) are published via record_outbox() in src/ but do "
        "not appear in Docs/30-contracts/04-event-catalog.md and are not in this test's "
        "KNOWN_ST14_DRIFT baseline:\n  - "
        + "\n  - ".join(sorted(undocumented))
        + "\n\nAdd a row to the catalog for each new event (preferred), or, only if this is "
        "genuinely the same authorial-rename situation as ST-14, add it to KNOWN_ST14_DRIFT "
        "with a citation of which existing row it collides with."
    )


def test_st14_baseline_has_no_stale_entries() -> None:
    """Keeps the baseline honest: an entry that is no longer emitted, or that has since been
    documented directly, should be deleted rather than left to accumulate forever."""
    scan = scan_emitted_event_types(SRC_ROOT)
    documented = parse_catalog_event_names(CATALOG_PATH)

    # `possible_literals` (not just the strictly-resolved `literals`) so a baseline entry that
    # is only reachable through the semantic_api.py governance-review dispatch's partially
    # literal branches isn't flagged as "no longer emitted" when it plainly still is.
    no_longer_emitted = KNOWN_ST14_DRIFT.keys() - scan.literals - scan.possible_literals
    if no_longer_emitted:
        stale_names = ", ".join(sorted(no_longer_emitted))
        warnings.warn(
            f"KNOWN_ST14_DRIFT names event(s) that src/ no longer emits -- safe to prune "
            f"from the baseline in tests/test_event_catalog_gate.py: {stale_names}",
            stacklevel=1,
        )

    now_documented = KNOWN_ST14_DRIFT.keys() & documented
    if now_documented:
        resolved_names = ", ".join(sorted(now_documented))
        warnings.warn(
            f"KNOWN_ST14_DRIFT names event(s) that are now directly documented in the "
            f"catalog -- ST-14 resolved these; prune from the baseline in "
            f"tests/test_event_catalog_gate.py: {resolved_names}",
            stacklevel=1,
        )


def test_non_literal_event_type_sites_are_reported() -> None:
    """Non-blocking by design (see the module docstring and TS-11's exit condition, which only
    binds the code -> catalog direction), but never silent: any record_outbox() call whose
    event_type= could not be resolved to a closed set of literals is surfaced here as a
    warning, listing exactly where it is and why, instead of being invisible to the gate."""
    scan = scan_emitted_event_types(SRC_ROOT)
    for site in scan.unresolved:
        warnings.warn(f"non-literal event_type= not statically checkable: {site}", stacklevel=1)

    assert len(scan.unresolved) >= EXPECTED_UNRESOLVED_SITE_COUNT, (
        "Expected at least the known non-literal record_outbox() event_type= site(s) "
        f"(semantic_api.py's governance-review dispatch) to still be reported; found "
        f"{len(scan.unresolved)}. If this legitimately dropped to zero, lower "
        "EXPECTED_UNRESOLVED_SITE_COUNT; if a new non-literal site appeared, that's fine too as "
        "long as it is genuinely not resolvable to a literal."
    )


def test_catalog_rows_with_no_current_emitter_are_reported_softly() -> None:
    """Hygiene, not a hard gate (deliberately): TS-11's exit condition is one-directional
    (code -> catalog). A documented-but-unemitted row is often just a planned event for a
    module that has not shipped yet, so this only warns -- it never fails the build."""
    scan = scan_emitted_event_types(SRC_ROOT)
    documented = parse_catalog_event_names(CATALOG_PATH)

    known_emitted_elsewhere = {
        # Resolved only by manually tracing a helper function's return value (see
        # test_non_literal_event_type_sites_are_reported); real, but invisible to the static
        # scan, so excluded here to avoid a false "no emitter" warning.
        "glossary.conflict_resolved.v1",
        "glossary.conflict_resolution_rejected.v1",
        "glossary.link_proposal_approved.v1",
        "glossary.link_proposal_rejected.v1",
        "ownership.assigned.v1",
        "glossary.term_linked_bulk.v1",
        "glossary.term_deprecated.v1",
        "certification.granted.v1",
    }
    unemitted = documented - scan.literals - scan.possible_literals - known_emitted_elsewhere
    if unemitted:
        warnings.warn(
            f"{len(unemitted)} catalog row(s) have no emitter the scanner could find (this is "
            "expected for planned-but-unshipped events; informational only): "
            + ", ".join(sorted(unemitted)),
            stacklevel=1,
        )
