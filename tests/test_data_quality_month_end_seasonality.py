"""DQ-6 follow-up: a month-end (day-of-month) seasonal baseline for VOLUME_CHANGE,
additive alongside DQ-6's day-of-week baseline, not replacing it.

`tests/test_data_quality_seasonality.py` covers DQ-6's own weekly-cycle case:
a table whose row count differs by weekday, judged against its own day-of-week
history. That grouping cannot see a *month-end* pattern -- a recurring close
batch (e.g. a `daily_transactions` table that spikes for a legitimate month-end
reconciliation load) lands on a different weekday every month, so it is spread
across several weekday buckets instead of forming a pattern there, and DQ-6's
own "Honest gaps" note named this exact case as an unattempted follow-up.

`data_quality.day_of_month_baseline` (pure, DB-free, same shape as
`day_of_week_baseline`) groups a table's own persisted scan history by
"calendar days before month end" -- `0` = the month's last day, `1` = the
second-to-last, etc. -- so a 28-day February's last day lines up with a 31-day
March's, and a close-window reading is judged against the table's own other
close windows instead of the single ordinary day right before it.

This file proves, with a concrete synthetic month-end-close cycle and exact
counts:
  1. The existing rolling-previous comparison flags every entry into the
     close window as a VOLUME_CHANGE anomaly -- a real, measured false-positive
     rate for a table whose month-end spike is normal and expected.
  2. The month-end-aware comparison, given the same table's own persisted
     history, flags none of those same transitions -- 0 false positives.
  3. The month-end-aware comparison still flags a value that is genuinely
     anomalous *for its own month-end* -- a true positive is not lost.
  4. `day_of_month_baseline` itself, as a pure function, groups correctly
     across months of different lengths and falls back to `None` with thin
     history, matching `day_of_week_baseline`'s contract.
  5. The two seasonal strategies are additive, not exclusive: with both
     enabled, a month-end reading prefers the month-end baseline while an
     ordinary day still gets the day-of-week baseline.
"""

import calendar
from datetime import UTC, datetime, timedelta

from aida.data_quality import QualityProfile, day_of_month_baseline, evaluate_quality

# A 2026 Thursday to build a deterministic multi-month calendar from.
_START = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
_NORMAL_ROW_COUNT = 1000
_CLOSE_ROW_COUNT = 3000
_CLOSE_WINDOW_DAYS = 2  # the month's last 2 calendar days count as "month-end"


def _is_close_window(day: datetime) -> bool:
    last_day = calendar.monthrange(day.year, day.month)[1]
    return (last_day - day.day) < _CLOSE_WINDOW_DAYS


def _row_count_for(day_offset: int) -> int:
    """A table's normal row count `day_offset` days after `_START`.

    Every day in the month's last `_CLOSE_WINDOW_DAYS` is a flat, exact
    month-end close spike (so the zero-stdev percent-change verdict path is
    exercised deterministically); every other day is close to 1000 with small
    deterministic (not random) jitter, closer to a real table's day-to-day
    variation than a perfectly flat constant.
    """
    day = _START + timedelta(days=day_offset)
    if _is_close_window(day):
        return _CLOSE_ROW_COUNT
    jitter = ((day_offset * 7) % 11) - 5  # deterministic wobble in [-5, 5]
    return _NORMAL_ROW_COUNT + jitter


def _build_history(num_days: int) -> list[tuple[datetime, int]]:
    return [(_START + timedelta(days=offset), _row_count_for(offset)) for offset in range(num_days)]


def _profile(row_count: int) -> QualityProfile:
    return QualityProfile(row_count, "schema-a", {})


# --- 1 & 2: the measured false-positive comparison -----------------------------


def test_month_end_seasonality_eliminates_every_close_window_false_positive() -> None:
    """The concrete before/after this row's exit condition asks for.

    Five months (Jan-May 2026, 151 days) of history establish the close-window
    pattern -- each month's own month-end position seen once. The next three
    months (Jun/Jul/Aug 2026) are each evaluated two ways at the moment they
    enter the close window. Every one of those 3 entries is a *normal* month-end
    for this table -- not one of them should ever open an incident.
    """
    history = _build_history(151)  # Jan 1 .. May 31, 2026: five real month-end cycles
    naive_false_positives = 0
    month_end_false_positives = 0
    transitions_checked = 0

    for offset in range(151, 151 + 92):  # Jun, Jul, Aug 2026
        current_day = _START + timedelta(days=offset)
        previous_day = _START + timedelta(days=offset - 1)
        entering_close_window = _is_close_window(current_day) and not _is_close_window(previous_day)
        if not entering_close_window:
            continue  # only the actual normal->close-window transition is the false-positive risk
        transitions_checked += 1

        current_profile = _profile(_row_count_for(offset))
        baseline_profile = _profile(_row_count_for(offset - 1))

        naive = evaluate_quality(current_profile, baseline_profile)
        if "VOLUME_CHANGE" in naive.anomaly_types:
            naive_false_positives += 1

        month_end = evaluate_quality(
            current_profile,
            baseline_profile,
            row_count_history=history,
            current_observed_at=current_day,
            month_end_seasonality_enabled=True,
        )
        if "VOLUME_CHANGE" in month_end.anomaly_types:
            month_end_false_positives += 1

    # The measured numbers this row's exit condition ("Reduced false positives,
    # measured") asks for: today's rolling-previous baseline flags every single
    # normal close-window entry; the month-end baseline flags none of them.
    assert transitions_checked == 3
    assert naive_false_positives == 3
    assert month_end_false_positives == 0


def test_single_close_window_entry_before_after_evidence_is_concrete() -> None:
    """One transition in detail: June 29, 2026 -- June has 30 days, so day 29 is
    the first day of its 2-day close window (`days_before_month_end == 1`)."""
    history = _build_history(151)
    june_29 = _START + timedelta(days=179)
    june_28 = _START + timedelta(days=178)
    assert june_29.month == 6 and june_29.day == 29
    assert calendar.monthrange(2026, 6)[1] == 30

    current = _profile(_row_count_for(179))
    baseline = _profile(_row_count_for(178))

    naive = evaluate_quality(current, baseline)
    assert naive.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"
    # ~1000 -> 3000, comfortably past the 30% default threshold: a false positive.
    assert naive.evidence["volume_change_percent"] > 100.0
    assert "VOLUME_CHANGE" in naive.anomaly_types

    month_end = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=june_29,
        month_end_seasonality_enabled=True,
    )
    assert month_end.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"
    assert month_end.evidence["seasonal_days_before_month_end"] == 1
    # Exactly Jan/Feb/Mar/Apr/May's own "1 day before month end" points.
    assert month_end.evidence["seasonal_sample_count"] == 5
    assert month_end.evidence["seasonal_mean_row_count"] == 3000.0
    assert "seasonal_change_percent" in month_end.evidence  # flat close values -> zero stdev path
    assert "VOLUME_CHANGE" not in month_end.anomaly_types
    assert month_end.status == "HEALTHY"
    assert june_28  # the naive baseline day, referenced above for clarity only


# --- 3: a true positive is preserved, not lost, by switching baselines --------


def test_month_end_baseline_still_catches_a_real_close_failure() -> None:
    """A close window that collapses to near-zero (e.g. the reconciliation batch
    failed to run) is still an incident under the month-end baseline -- switching
    away from "compare to the day before" does not mean "stop detecting anomalies
    at month-end", only "stop misjudging a normal close as one"."""
    history = _build_history(151)
    june_29 = _START + timedelta(days=179)
    baseline = _profile(_row_count_for(178))

    real_incident_row_count = 50  # this table's close windows are normally exactly 3000
    current = _profile(real_incident_row_count)

    month_end = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=june_29,
        month_end_seasonality_enabled=True,
    )
    assert month_end.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"
    assert month_end.evidence["seasonal_change_percent"] > 90.0
    assert "VOLUME_CHANGE" in month_end.anomaly_types
    assert month_end.severities["VOLUME_CHANGE"] == "CRITICAL"

    # And the naive comparison also catches it -- the point is only that the
    # month-end path no longer needs a real incident to fire.
    naive = evaluate_quality(current, baseline)
    assert "VOLUME_CHANGE" in naive.anomaly_types


def test_day_of_month_baseline_uses_zscore_when_history_has_real_spread() -> None:
    """A hand-picked, small-numbers case exercising the nonzero-stdev z-score
    path (the flat synthetic close values above always take the zero-stdev
    percent-change path; this proves the other branch independently)."""
    # "1 day before month end" points from three 31-day months, with a real spread.
    history = [
        (datetime(2026, 1, 30, tzinfo=UTC), 100),
        (datetime(2026, 3, 30, tzinfo=UTC), 104),
        (datetime(2026, 5, 30, tzinfo=UTC), 96),
    ]
    current_day = datetime(2026, 7, 30, tzinfo=UTC)  # July: also 31 days, same position

    baseline = day_of_month_baseline(history, current_day, min_samples=3)
    assert baseline is not None
    assert baseline.days_before_month_end == 1
    assert baseline.sample_count == 3
    assert 99.0 < baseline.mean < 101.0
    assert baseline.stdev > 0

    result = evaluate_quality(
        _profile(300),  # wildly outside a mean-100 baseline
        _profile(100),
        row_count_history=history,
        current_observed_at=current_day,
        month_end_seasonality_enabled=True,
    )
    assert result.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"
    assert "seasonal_zscore" in result.evidence
    assert result.evidence["seasonal_zscore"] > 3.0
    assert "VOLUME_CHANGE" in result.anomaly_types
    assert result.severities["VOLUME_CHANGE"] == "CRITICAL"


# --- 4: day_of_month_baseline as a pure function, and its fallback ------------


def test_day_of_month_baseline_groups_by_month_end_position_across_month_lengths() -> None:
    history = [
        (datetime(2026, 1, 30, tzinfo=UTC), 3000),  # Jan (31 days): 1 day before month end
        (datetime(2026, 2, 27, tzinfo=UTC), 3000),  # Feb (28 days): 1 day before month end
        (datetime(2026, 3, 30, tzinfo=UTC), 3000),  # Mar (31 days): 1 day before month end
        (datetime(2026, 1, 15, tzinfo=UTC), 1000),  # a normal mid-month day: must not be grouped in
    ]
    fourth_month_end = datetime(2026, 4, 29, tzinfo=UTC)  # Apr (30 days): same position
    baseline = day_of_month_baseline(history, fourth_month_end, min_samples=3)

    assert baseline is not None
    assert baseline.days_before_month_end == 1
    assert baseline.sample_count == 3  # exactly the 3 prior month-ends, not the mid-month point
    assert baseline.mean == 3000.0


def test_day_of_month_baseline_falls_back_to_none_with_thin_history() -> None:
    history = [
        (datetime(2026, 1, 30, tzinfo=UTC), 3000),
        (datetime(2026, 2, 27, tzinfo=UTC), 3000),
    ]
    third_month_end = datetime(2026, 3, 30, tzinfo=UTC)
    assert day_of_month_baseline(history, third_month_end, min_samples=3) is None


def test_evaluate_quality_falls_back_when_month_end_history_is_too_thin() -> None:
    """Even with `month_end_seasonality_enabled=True`, a table too new to have 3
    same-position points yet gets the original rolling-previous verdict."""
    thin_history = _build_history(60)  # Jan + Feb 2026: only 2 month-end cycles so far
    march_30 = _START + timedelta(days=88)  # Mar 30, 2026: 1 day before March's month end
    assert march_30.month == 3 and march_30.day == 30

    current = _profile(_row_count_for(88))
    baseline = _profile(_row_count_for(87))

    result = evaluate_quality(
        current,
        baseline,
        row_count_history=thin_history,
        current_observed_at=march_30,
        month_end_seasonality_enabled=True,
    )
    assert result.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"


def test_month_end_strategy_not_applied_outside_its_window() -> None:
    """A comfortably mid-month day does not use the month-end baseline even
    when the flag is on -- only readings inside `month_end_window_days` do."""
    history = _build_history(151)
    mid_june = _START + timedelta(days=165)  # June 15, 2026: far from any month end
    assert mid_june.day == 15

    current = _profile(_row_count_for(165))
    baseline = _profile(_row_count_for(164))

    result = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=mid_june,
        month_end_seasonality_enabled=True,
    )
    assert result.evidence["threshold_strategy"] == "ROLLING_PREVIOUS"


# --- 5: additive, not exclusive -- month-end and day-of-week coexist -----------


def test_both_seasonal_strategies_enabled_month_end_wins_inside_its_window() -> None:
    """With both flags on: a month-end-window reading prefers the month-end
    baseline (the more specific signal for that day); an ordinary day in the
    same run still gets the day-of-week baseline DQ-6 already shipped -- proving
    this row's grouping is additive alongside DQ-6's, not a replacement for it."""
    history = _build_history(151)

    june_29 = _START + timedelta(days=179)
    in_window = evaluate_quality(
        _profile(_row_count_for(179)),
        _profile(_row_count_for(178)),
        row_count_history=history,
        current_observed_at=june_29,
        seasonality_enabled=True,
        month_end_seasonality_enabled=True,
    )
    assert in_window.evidence["threshold_strategy"] == "SEASONAL_MONTH_END"

    mid_june = _START + timedelta(days=165)
    outside_window = evaluate_quality(
        _profile(_row_count_for(165)),
        _profile(_row_count_for(164)),
        row_count_history=history,
        current_observed_at=mid_june,
        seasonality_enabled=True,
        month_end_seasonality_enabled=True,
    )
    assert outside_window.evidence["threshold_strategy"] == "SEASONAL_DAY_OF_WEEK"


def test_month_end_seasonality_disabled_by_default_is_byte_identical_to_before() -> None:
    """Without opting in, passing history/timestamp changes nothing: the flag
    fully gates the new code path, matching DQ-6's own rollout convention."""
    history = _build_history(151)
    june_29 = _START + timedelta(days=179)
    current = _profile(_row_count_for(179))
    baseline = _profile(_row_count_for(178))

    without_history = evaluate_quality(current, baseline)
    with_history_but_disabled = evaluate_quality(
        current,
        baseline,
        row_count_history=history,
        current_observed_at=june_29,
        month_end_seasonality_enabled=False,
    )
    assert without_history == with_history_but_disabled
