"""DQ-6: seasonality-aware thresholds for the VOLUME_CHANGE control.

`quality_service.evaluate_analysis_run` compares each new `TableProfile.row_count`
only to the single most recent prior profile for that table (see the
`baseline_rank == 1` window function in `quality_service.py`). That "rolling
previous value" comparison has no notion of day-of-week, so a table with a
completely normal weekly cycle -- e.g. a `daily_transactions` table that runs
~1000 rows/day on weekdays and ~400 rows/day on weekends, every week, by design
-- trips `VOLUME_CHANGE` on every single Friday-to-Saturday and Sunday-to-Monday
transition, at a fixed 30% threshold. This is a real false positive: the metric
did not degrade, it just did what it always does on that day of the week.

`data_quality.day_of_week_baseline` (pure, DB-free: it takes only a sequence of
already-observed (timestamp, value) points and a timestamp to judge) groups a
table's own persisted scan history by weekday and returns that weekday's own
mean/stdev, so `evaluate_quality`'s seasonality-aware path can judge a Saturday
against other Saturdays instead of against Friday.

This file proves, with a concrete synthetic weekly cycle and exact counts:
  1. The existing rolling-previous comparison flags every weekend transition as
     a VOLUME_CHANGE anomaly -- a real, measured false-positive rate.
  2. The seasonality-aware comparison, given the same table's own persisted
     history, flags none of those same transitions -- 0 false positives.
  3. The seasonality-aware comparison still flags a value that is genuinely
     anomalous *for its own day of week* -- a true positive is not lost by
     switching baselines.
  4. `day_of_week_baseline` itself, as a pure function, groups correctly and
     falls back to `None` (letting the caller use the non-seasonal path) when
     a table has not accumulated enough same-weekday history yet.
"""

from datetime import UTC, datetime, timedelta

from aida.data_quality import QualityProfile, day_of_week_baseline, evaluate_quality

# A 2026 Monday to build a deterministic multi-week calendar from.
_WEEK_0_MONDAY = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
_WEEKDAY_BASE = 1000
_WEEKEND_BASE = 400


def _row_count_for(day_offset: int) -> int:
    """A table's normal row count `day_offset` days after `_WEEK_0_MONDAY`.

    Small, deterministic (not random) +/-1% jitter keyed off the day index, so
    every weekday value is close to 1000 and every weekend value is close to
    400, but no two same-weekday values are bit-for-bit identical -- closer to
    a real table's week-over-week variation than a perfectly flat constant.
    """
    weekday = (_WEEK_0_MONDAY + timedelta(days=day_offset)).weekday()
    base = _WEEKEND_BASE if weekday >= 5 else _WEEKDAY_BASE
    jitter = ((day_offset * 7) % 11) - 5  # deterministic wobble in [-5, 5]
    return base + jitter


def _build_history(num_days: int) -> list[tuple[datetime, int]]:
    return [
        (_WEEK_0_MONDAY + timedelta(days=offset), _row_count_for(offset))
        for offset in range(num_days)
    ]


def _profile(row_count: int) -> QualityProfile:
    return QualityProfile(row_count, "schema-a", {})


# --- 1 & 2: the measured false-positive comparison -----------------------------


def test_seasonality_eliminates_every_weekend_false_positive_naive_flags() -> None:
    """The concrete before/after this row's exit condition asks for.

    Eight weeks (56 days) of history establish the pattern. The next four
    weeks (28 more days, 8 weekend transitions: Fri->Sat and Sun->Mon x4) are
    each evaluated two ways: today's rolling-previous comparison, and the
    seasonality-aware comparison fed that same table's own persisted history.
    Every one of those 8 transitions is a *normal* week for this table -- not
    a single one should ever have opened an incident.
    """
    history = _build_history(56)
    naive_false_positives = 0
    seasonal_false_positives = 0
    transitions_checked = 0

    for offset in range(56, 84):
        previous_day = _WEEK_0_MONDAY + timedelta(days=offset - 1)
        current_day = _WEEK_0_MONDAY + timedelta(days=offset)
        crosses_weekday_boundary = (previous_day.weekday(), current_day.weekday()) in {
            (4, 5),  # Friday -> Saturday
            (6, 0),  # Sunday -> Monday
        }
        if not crosses_weekday_boundary:
            continue  # only the actual weekday<->weekend transitions are the false-positive risk
        transitions_checked += 1

        current_row_count = _row_count_for(offset)
        baseline_row_count = _row_count_for(offset - 1)
        current_profile = _profile(current_row_count)
        baseline_profile = _profile(baseline_row_count)

        naive = evaluate_quality(current_profile, baseline_profile)
        if "VOLUME_CHANGE" in naive.anomaly_types:
            naive_false_positives += 1

        seasonal = evaluate_quality(
            current_profile,
            baseline_profile,
            row_count_history=history,
            current_observed_at=current_day,
            seasonality_enabled=True,
        )
        if "VOLUME_CHANGE" in seasonal.anomaly_types:
            seasonal_false_positives += 1

    # The measured numbers this row's exit condition ("Reduced false positives,
    # measured") asks for: today's rolling-previous baseline flags every single
    # normal weekend transition; the day-of-week baseline flags none of them.
    assert transitions_checked == 8
    assert naive_false_positives == 8
    assert seasonal_false_positives == 0


def test_single_saturday_before_after_evidence_is_concrete() -> None:
    """One transition in detail: the exact percentages/z-scores behind the tally above."""
    history = _build_history(56)
    saturday = _WEEK_0_MONDAY + timedelta(days=61)  # a week-9 Saturday, ~400 rows, fully normal
    friday_before_it = _WEEK_0_MONDAY + timedelta(days=60)  # ~1000 rows

    current = _profile(_row_count_for(61))
    baseline = _profile(_row_count_for(60))

    naive = evaluate_quality(current, baseline)
    assert naive.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"
    # ~60% drop vs. Friday, comfortably past the 30% default threshold: a false positive.
    assert naive.evidence["volume_change_percent"] > 50.0
    assert "VOLUME_CHANGE" in naive.anomaly_types
    assert naive.status in {"WARNING", "CRITICAL"}

    seasonal = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=saturday,
        seasonality_enabled=True,
    )
    assert seasonal.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"
    assert saturday.weekday() == friday_before_it.weekday() + 1
    assert seasonal.evidence["seasonal_weekday"] == saturday.weekday()
    # Small z-score against this table's own other Saturdays: nowhere near anomalous.
    assert seasonal.evidence["seasonal_zscore"] < 1.5
    assert "VOLUME_CHANGE" not in seasonal.anomaly_types
    assert seasonal.status == "HEALTHY"


# --- 3: a true positive is preserved, not lost, by switching baselines --------


def test_seasonal_baseline_still_catches_a_real_weekend_incident() -> None:
    """A Saturday that collapses to near-zero is still an incident under the
    seasonal baseline -- switching away from "compare to Friday" does not mean
    "stop detecting anomalies on weekends", only "stop misjudging a normal
    weekend as one".
    """
    history = _build_history(56)
    saturday = _WEEK_0_MONDAY + timedelta(days=61)

    real_incident_row_count = 20  # this table's Saturdays are normally ~400
    current = _profile(real_incident_row_count)
    baseline = _profile(_row_count_for(60))

    seasonal = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=saturday,
        seasonality_enabled=True,
    )
    assert seasonal.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"
    assert seasonal.evidence["seasonal_zscore"] > 3.0
    assert "VOLUME_CHANGE" in seasonal.anomaly_types
    assert seasonal.severities["VOLUME_CHANGE"] == "CRITICAL"

    # And the naive comparison (Saturday vs. the Friday right before it) also
    # catches it -- nothing about the seasonal path is needed to see *this*
    # drop; the point is only that it no longer needs a real incident to fire.
    naive = evaluate_quality(current, baseline)
    assert "VOLUME_CHANGE" in naive.anomaly_types


# --- 4: day_of_week_baseline as a pure function --------------------------------


def test_day_of_week_baseline_groups_by_weekday_only() -> None:
    history = _build_history(26)  # offsets 0..25: 3 full Saturdays (5, 12, 19), none is day 26
    fourth_saturday = _WEEK_0_MONDAY + timedelta(days=26)
    baseline = day_of_week_baseline(history, fourth_saturday, min_samples=3)

    assert baseline is not None
    assert baseline.weekday == 5
    assert baseline.sample_count == 3  # exactly the 3 prior Saturdays (offsets 5, 12, 19)
    assert 395 <= baseline.mean <= 405  # every Saturday in the fixture is ~400, never ~1000


def test_day_of_week_baseline_falls_back_to_none_with_thin_history() -> None:
    # Only 9 days of history: at most 2 same-weekday points for any weekday.
    history = _build_history(9)
    tenth_day = _WEEK_0_MONDAY + timedelta(days=9)
    assert day_of_week_baseline(history, tenth_day, min_samples=3) is None


def test_evaluate_quality_falls_back_when_seasonal_history_is_too_thin() -> None:
    """Even with `seasonality_enabled=True`, a table too new to have 3 same-weekday
    points yet gets the original rolling-previous verdict, not a seasonal guess
    built on too little data -- the safe default this row's flag was designed for.
    """
    thin_history = _build_history(9)
    tenth_day = _WEEK_0_MONDAY + timedelta(days=9)
    current = _profile(_row_count_for(9))
    baseline = _profile(_row_count_for(8))

    result = evaluate_quality(
        current,
        baseline,
        row_count_history=thin_history,
        current_observed_at=tenth_day,
        seasonality_enabled=True,
    )
    assert result.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"


def test_seasonality_disabled_by_default_is_byte_identical_to_before() -> None:
    """Without opting in, passing history/timestamp changes nothing: the flag
    fully gates the new code path, matching this repo's established rollout
    convention for a new quality strategy.
    """
    history = _build_history(56)
    saturday = _WEEK_0_MONDAY + timedelta(days=61)
    current = _profile(_row_count_for(61))
    baseline = _profile(_row_count_for(60))

    without_history = evaluate_quality(current, baseline)
    with_history_but_disabled = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=saturday,
        seasonality_enabled=False,
    )
    assert without_history == with_history_but_disabled
