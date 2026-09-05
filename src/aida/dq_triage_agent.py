"""DQ triage agent: deterministic root-cause hints for one open incident.

Mirrors `reviewer_agent.py`'s own discipline exactly: a pure function of
evidence the platform already holds -- `anomaly_type`, the detector's own
`evidence` blob, `occurrence_count`, `severity`, `source` -- never a model
call. A steward opening an incident today sees the raw detector evidence
(`volume_change_percent: -42.3`) with no interpretation; this turns that
into "row count dropped 42% versus baseline, consistent with a delayed or
failed upstream load -- check the most recent ingestion batch."

Deliberately not persisted. There is no `dq_triage` table and no column on
`DataQualityIncident`: this is computed fresh on every request from the
incident row already loaded, the same "recompute rather than cache a
verdict" idiom `agent_eval_gate.py`'s own `CONFIRMED_RUN` half uses --
nothing here can go stale because nothing here is stored. If a future row
wants a steward's edited/confirmed triage note to persist, that is a
genuinely different feature (a governed annotation, reviewed like every
other one in this codebase) and does not belong in what stays a pure,
value-free scorer.

Every hint traces to a named evidence field (`basis`) so a steward can check
"why does it say that" against the incident's own `evidence` blob, the same
transparency `stewardship_worklist.py`'s score/usage/impact/deficit split
already commits to for its own ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TriageSuggestion:
    anomaly_type: str
    likely_causes: tuple[str, ...]
    recommended_next_steps: tuple[str, ...]
    #: The evidence field name(s) each cause/step above was derived from --
    #: e.g. `("volume_change_percent", "occurrence_count")` -- so a steward
    #: can check this against the incident's own `evidence` blob rather than
    #: trust an unattributed sentence.
    basis: tuple[str, ...] = field(default_factory=tuple)


def _volume_change(evidence: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    causes: list[str] = []
    steps: list[str] = []
    basis: list[str] = []
    change = evidence.get("volume_change_percent")
    if isinstance(change, int | float):
        basis.append("volume_change_percent")
        if change < 0:
            causes.append(
                f"Row count dropped {abs(change):.1f}% versus the baseline -- consistent "
                "with a delayed, partial, or failed upstream load."
            )
            steps.append(
                "Check the most recent ingestion batch or analysis run for this "
                "datasource for a failure, timeout, or early termination."
            )
        else:
            causes.append(
                f"Row count rose {change:.1f}% versus the baseline -- consistent with a "
                "duplicate load, a re-run without deduplication, or a genuine upstream "
                "backfill."
            )
            steps.append(
                "Check for a re-run or backfill job on the source system that may have "
                "appended rows without a corresponding delete/upsert."
            )
    strategy = evidence.get("threshold_strategy")
    if strategy in ("SEASONAL_MONTH_END", "SEASONAL_DAY_OF_WEEK"):
        basis.append("threshold_strategy")
        sample_count = evidence.get("seasonal_sample_count")
        if isinstance(sample_count, int) and sample_count < 4:
            basis.append("seasonal_sample_count")
            causes.append(
                f"The seasonal baseline this compared against has only {sample_count} "
                "prior sample(s) -- treat this verdict cautiously until more history "
                "accumulates."
            )
    if not causes:
        causes.append(
            "Volume changed materially versus the rolling baseline; no further "
            "structured evidence is available on this incident."
        )
        steps.append("Inspect the datasource's recent load history for this table directly.")
    return causes, steps, basis


def _null_rate_shift(evidence: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    causes: list[str] = []
    steps: list[str] = []
    basis: list[str] = []
    change = evidence.get("max_null_rate_change_percent")
    if isinstance(change, int | float):
        basis.append("max_null_rate_change_percent")
        causes.append(
            f"At least one column's null rate shifted {change:.1f} percentage points "
            "versus the baseline."
        )
    affected = evidence.get("affected_column_ids")
    if isinstance(affected, list) and affected:
        basis.append("affected_column_ids")
        causes.append(
            f"{len(affected)} column(s) are implicated -- see `evidence.affected_column_ids` "
            "for which ones, rather than re-profiling the whole table."
        )
        steps.append(
            "Check whether the source added a new optional field, changed a default, "
            "or whether an upstream join/transform started dropping matches for those "
            "specific columns."
        )
    if not causes:
        causes.append("A column's null rate shifted materially versus its baseline.")
        steps.append("Compare the current and prior profile for this table's columns.")
    return causes, steps, basis


def _schema_change(evidence: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    basis: list[str] = []
    if evidence.get("schema_fingerprint_changed"):
        basis.append("schema_fingerprint_changed")
    causes = [
        "The table's column set, types, or ordinal positions changed versus the last scan."
    ]
    steps = [
        "Coordinate with the source system owner before this table is queried again -- a "
        "DDL change upstream (added/dropped/retyped column) is the most common cause, and "
        "any tool or metric built against the prior shape may now be reading the wrong "
        "column or failing outright.",
    ]
    return causes, steps, basis


def _custom_rule(evidence: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    causes = [
        "This incident was raised by an organization-authored custom quality rule, not "
        "one of the platform's built-in detectors -- the rule's own name and condition "
        "(configured under Data quality → rule packs) are the authoritative "
        "explanation of what fired."
    ]
    steps = ["Open the rule pack that owns this rule to see its exact condition and threshold."]
    return causes, steps, []


def _external_source(evidence: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    causes = [
        "This incident was reconciled from a third-party detector (Monte Carlo, Anomalo, "
        "or similar), not computed by Atlas -- the vendor's own console has the detection "
        "logic and any richer diagnosis it produced."
    ]
    steps = ["Open this incident in the originating vendor's console for its full diagnosis."]
    return causes, steps, []


def suggest_triage(
    *,
    anomaly_type: str,
    source: str,
    evidence: dict[str, Any],
    occurrence_count: int,
) -> TriageSuggestion:
    """A deterministic root-cause hint for one incident's already-recorded
    `anomaly_type`/`evidence`/`occurrence_count`. Never sees a source value
    (INV-6) -- only the value-free detector evidence every incident already
    carries.
    """
    if source == "EXTERNAL":
        causes, steps, basis = _external_source(evidence)
    elif anomaly_type == "VOLUME_CHANGE":
        causes, steps, basis = _volume_change(evidence)
    elif anomaly_type == "NULL_RATE_SHIFT":
        causes, steps, basis = _null_rate_shift(evidence)
    elif anomaly_type == "SCHEMA_CHANGE":
        causes, steps, basis = _schema_change(evidence)
    elif anomaly_type.startswith("CUSTOM_RULE:"):
        causes, steps, basis = _custom_rule(evidence)
    else:
        causes = [f"No structured triage rule is registered for anomaly type {anomaly_type!r}."]
        steps = ["Review the incident's own evidence and summary directly."]
        basis = []

    if occurrence_count > 1:
        basis.append("occurrence_count")
        causes.append(
            f"This is a recurring incident -- it has fired {occurrence_count} times. If a "
            "prior fix was applied, either it did not address the root cause or the "
            "condition has recurred independently."
        )

    return TriageSuggestion(
        anomaly_type=anomaly_type,
        likely_causes=tuple(causes),
        recommended_next_steps=tuple(steps),
        basis=tuple(dict.fromkeys(basis)),
    )
