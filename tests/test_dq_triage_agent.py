"""DQ triage agent: pure, deterministic root-cause hints.

No database -- `suggest_triage` is a pure function of the same value-free
fields every `DataQualityIncident` already carries (`anomaly_type`,
`evidence`, `occurrence_count`, `source`), so it is fully testable without
seeding a datasource/table/incident chain, the same "no database needed"
property `test_agent_eval_gate.py`'s own layer-1 tests exercise for
`evaluate_agent_eval_gate`.
"""

from __future__ import annotations

from aida.dq_triage_agent import suggest_triage


def test_volume_drop_names_a_delayed_or_failed_load_and_points_at_ingestion() -> None:
    result = suggest_triage(
        anomaly_type="VOLUME_CHANGE",
        source="INTERNAL",
        evidence={"volume_change_percent": -42.3, "baseline_row_count": 10_000},
        occurrence_count=1,
    )

    assert result.anomaly_type == "VOLUME_CHANGE"
    assert any("dropped 42.3%" in cause for cause in result.likely_causes)
    assert any("ingestion batch" in step for step in result.recommended_next_steps)
    assert "volume_change_percent" in result.basis


def test_volume_rise_names_a_duplicate_load_or_backfill() -> None:
    result = suggest_triage(
        anomaly_type="VOLUME_CHANGE",
        source="INTERNAL",
        evidence={"volume_change_percent": 55.0},
        occurrence_count=1,
    )

    assert any("rose 55.0%" in cause for cause in result.likely_causes)
    assert any("backfill" in step for step in result.recommended_next_steps)


def test_thin_seasonal_baseline_is_flagged_as_a_caution() -> None:
    result = suggest_triage(
        anomaly_type="VOLUME_CHANGE",
        source="INTERNAL",
        evidence={
            "volume_change_percent": -10.0,
            "threshold_strategy": "SEASONAL_MONTH_END",
            "seasonal_sample_count": 2,
        },
        occurrence_count=1,
    )

    assert any("2 prior sample" in cause for cause in result.likely_causes)
    assert "seasonal_sample_count" in result.basis


def test_null_rate_shift_names_affected_columns_when_present() -> None:
    result = suggest_triage(
        anomaly_type="NULL_RATE_SHIFT",
        source="INTERNAL",
        evidence={
            "max_null_rate_change_percent": 18.5,
            "affected_column_ids": ["col-a", "col-b"],
        },
        occurrence_count=1,
    )

    assert any("18.5 percentage points" in cause for cause in result.likely_causes)
    assert any("2 column(s)" in cause for cause in result.likely_causes)
    assert "affected_column_ids" in result.basis


def test_schema_change_points_at_the_source_owner_before_anything_else() -> None:
    result = suggest_triage(
        anomaly_type="SCHEMA_CHANGE",
        source="INTERNAL",
        evidence={"schema_fingerprint_changed": True},
        occurrence_count=1,
    )

    assert "schema_fingerprint_changed" in result.basis
    assert any("source system owner" in step for step in result.recommended_next_steps)


def test_custom_rule_defers_to_the_rule_packs_own_condition() -> None:
    result = suggest_triage(
        anomaly_type="CUSTOM_RULE:abc123",
        source="INTERNAL",
        evidence={},
        occurrence_count=1,
    )

    assert any("custom quality rule" in cause for cause in result.likely_causes)
    assert any("rule pack" in step for step in result.recommended_next_steps)


def test_external_source_defers_to_the_vendor_console_regardless_of_anomaly_type() -> None:
    result = suggest_triage(
        anomaly_type="MONTE_CARLO:freshness",
        source="EXTERNAL",
        evidence={"volume_change_percent": -90.0},  # would otherwise match VOLUME_CHANGE
        occurrence_count=1,
    )

    assert any("third-party detector" in cause for cause in result.likely_causes)
    assert any("vendor" in step.lower() for step in result.recommended_next_steps)


def test_unknown_anomaly_type_falls_back_honestly_rather_than_guessing() -> None:
    result = suggest_triage(
        anomaly_type="SOME_FUTURE_DETECTOR",
        source="INTERNAL",
        evidence={},
        occurrence_count=1,
    )

    assert "SOME_FUTURE_DETECTOR" in result.likely_causes[0]
    assert result.basis == ()


def test_recurring_incident_adds_a_recurrence_note_and_basis_field() -> None:
    result = suggest_triage(
        anomaly_type="VOLUME_CHANGE",
        source="INTERNAL",
        evidence={"volume_change_percent": -5.0},
        occurrence_count=4,
    )

    assert any("fired 4 times" in cause for cause in result.likely_causes)
    assert "occurrence_count" in result.basis


def test_basis_never_duplicates_a_field_name() -> None:
    result = suggest_triage(
        anomaly_type="VOLUME_CHANGE",
        source="INTERNAL",
        evidence={"volume_change_percent": -5.0},
        occurrence_count=2,
    )

    assert len(result.basis) == len(set(result.basis))


def test_missing_structured_evidence_still_returns_a_usable_hint_not_an_error() -> None:
    result = suggest_triage(
        anomaly_type="VOLUME_CHANGE",
        source="INTERNAL",
        evidence={},
        occurrence_count=1,
    )

    assert result.likely_causes
    assert result.recommended_next_steps
