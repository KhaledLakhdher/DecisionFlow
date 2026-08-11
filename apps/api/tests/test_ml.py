"""Forecasting, anomaly detection and churn.

Fixtures carry a *known* signal — a fixed slope, a planted spike, a customer
who deliberately stopped buying — so the assertions test whether the model
found the right thing, rather than merely that it returned numbers.

The refusal cases matter as much as the successes. A forecast from three points
and a churn model over eight customers are the failure modes this product is
most likely to hit in the wild, and returning a confident answer to either
would be worse than returning nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from decisionflow.ml import anomalies, churn, forecasting


def _months(count: int, start: date = date(2026, 1, 1)) -> list[str]:
    return [
        ((start.replace(day=1) + timedelta(days=32 * i)).replace(day=1)).isoformat()
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# Forecasting
# --------------------------------------------------------------------------
def test_refuses_to_forecast_from_too_few_points() -> None:
    """Three points fit a line exactly — the 'trend' is an artifact."""
    with pytest.raises(forecasting.InsufficientDataError, match="at least 4"):
        forecasting.forecast_series(_months(3), [100.0, 200.0, 300.0])


def test_nulls_do_not_count_toward_the_minimum() -> None:
    periods = _months(6)
    values: list[float | None] = [100.0, None, 200.0, None, 300.0, None]

    with pytest.raises(forecasting.InsufficientDataError):
        forecasting.forecast_series(periods, values)


def test_linear_trend_extrapolates_a_known_slope() -> None:
    """A perfectly linear series must project its own slope."""
    values = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]
    result = forecasting.forecast_series(_months(6), values, horizon=2)

    assert result.method is forecasting.ForecastMethod.LINEAR_TREND
    assert result.points[0].value == pytest.approx(700.0, rel=0.01)
    assert result.points[1].value == pytest.approx(800.0, rel=0.01)


def test_a_noiseless_series_yields_a_tight_band() -> None:
    """Interval width should reflect actual uncertainty, not a fixed margin."""
    clean = forecasting.forecast_series(_months(8), [100.0 * i for i in range(1, 9)])
    noisy = forecasting.forecast_series(
        _months(8), [100.0, 900.0, 200.0, 850.0, 300.0, 950.0, 250.0, 800.0]
    )

    clean_width = clean.points[0].upper - clean.points[0].lower
    noisy_width = noisy.points[0].upper - noisy.points[0].lower
    assert clean_width < noisy_width


def test_intervals_widen_with_distance() -> None:
    """Uncertainty compounds; a flat band across the horizon would be a lie."""
    result = forecasting.forecast_series(
        _months(8), [100, 120, 90, 140, 130, 160, 150, 180], horizon=4
    )
    widths = [point.upper - point.lower for point in result.points]

    assert widths == sorted(widths), "each step out must be at least as wide"
    assert widths[-1] > widths[0]


def test_monthly_forecasts_land_on_month_boundaries() -> None:
    """A fixed 30.44-day step drifts: May 1 + step reads as May 31, not June."""
    values = [100.0 * i for i in range(1, 7)]
    result = forecasting.forecast_series(_months(6), values, horizon=4)

    periods = [point.period for point in result.points]
    assert periods == ["2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01"]


def test_month_end_start_dates_do_not_overflow() -> None:
    """31 January + 1 month is February's last day, not an invalid date."""
    periods = ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]
    result = forecasting.forecast_series(periods, [100.0, 120, 140, 160, 180], horizon=1)

    assert result.points[0].period == "2026-06-30"


def test_forecast_is_clamped_at_zero_by_default() -> None:
    """A revenue band dipping below zero is arithmetically fine and visibly wrong."""
    values = [500.0, 400.0, 300.0, 200.0, 100.0, 50.0]
    result = forecasting.forecast_series(_months(6), values, horizon=6)

    assert all(point.lower >= 0 for point in result.points)
    assert all(point.value >= 0 for point in result.points)


def test_negative_values_allowed_when_requested() -> None:
    """Profit can legitimately go negative."""
    values = [500.0, 400.0, 300.0, 200.0, 100.0, 50.0]
    result = forecasting.forecast_series(
        _months(6), values, horizon=6, allow_negative=True
    )
    assert min(point.lower for point in result.points) < 0


def test_seasonal_method_is_used_once_two_cycles_exist() -> None:
    """Below 24 monthly points, seasonality cannot be told from trend."""
    seasonal = [100, 120, 140, 200, 180, 160, 150, 130, 170, 190, 220, 260]
    values = [float(v) for v in seasonal * 2]  # 24 points
    result = forecasting.forecast_series(_months(24), values, horizon=3)

    assert result.method is forecasting.ForecastMethod.HOLT_WINTERS
    assert "seasonal" in result.rationale.lower()


def test_short_series_explains_why_it_is_not_seasonal() -> None:
    result = forecasting.forecast_series(_months(6), [100.0 * i for i in range(1, 7)])
    assert result.method is forecasting.ForecastMethod.LINEAR_TREND
    assert "seasonality" in result.rationale.lower()


def test_volatile_history_is_called_out() -> None:
    """The rationale should warn when the midpoint is not worth trusting."""
    values = [100.0, 900.0, 150.0, 850.0, 200.0, 950.0]
    result = forecasting.forecast_series(_months(6), values)

    assert result.noise_ratio is not None and result.noise_ratio > 0.5
    assert "volatile" in result.rationale.lower()


# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------
def test_a_planted_spike_is_found() -> None:
    values = [100.0, 110.0, 120.0, 130.0, 900.0, 150.0, 160.0, 170.0]
    found = anomalies.detect(_months(8), values)

    assert len(found) == 1
    assert found[0].direction == "above"
    assert found[0].period.startswith("2026-05")


def test_a_planted_dip_is_found() -> None:
    values = [500.0, 510.0, 520.0, 530.0, 40.0, 550.0, 560.0, 570.0]
    found = anomalies.detect(_months(8), values)

    assert len(found) == 1
    assert found[0].direction == "below"
    assert found[0].deviation < 0


def test_a_clean_trend_has_no_anomalies() -> None:
    """Growth is not an anomaly — the detector measures against the trend."""
    values = [100.0 * i for i in range(1, 13)]
    assert anomalies.detect(_months(12), values) == []


def test_growth_alone_is_not_flagged() -> None:
    """The largest value in a rising series is expected, not anomalous."""
    values = [100.0, 150.0, 210.0, 280.0, 360.0, 450.0, 550.0, 660.0]
    found = anomalies.detect(_months(8), values)
    assert not any(item.period.startswith("2026-08") for item in found)


def test_too_short_a_series_reports_nothing() -> None:
    assert anomalies.detect(_months(3), [100.0, 500.0, 100.0]) == []


def test_anomaly_message_quantifies_the_departure() -> None:
    """'Anomaly score 0.83' is not actionable; a percentage is."""
    values = [100.0, 110.0, 120.0, 130.0, 900.0, 150.0, 160.0, 170.0]
    found = anomalies.detect(_months(8), values)

    assert "%" in found[0].message
    assert "above" in found[0].message


# --------------------------------------------------------------------------
# Churn
# --------------------------------------------------------------------------
def _transactions(customers: int, *, lapsed_after: int = 0) -> list[dict[str, object]]:
    """Synthetic history: the first `lapsed_after` customers stop buying early."""
    rows: list[dict[str, object]] = []
    base = date(2026, 1, 1)

    for index in range(customers):
        is_lapsed = index < lapsed_after
        # Lapsed customers buy twice early on; active ones keep going.
        purchase_days = [0, 20] if is_lapsed else [0, 40, 80, 140, 190]
        for offset in purchase_days:
            rows.append(
                {
                    "customer_id": f"C-{index:03d}",
                    "order_date": (base + timedelta(days=offset)).isoformat(),
                    "revenue": 100.0 + index,
                }
            )
    return rows


def test_refuses_with_too_few_customers() -> None:
    with pytest.raises(churn.InsufficientDataError, match="at least 20"):
        churn.fit(
            _transactions(8, lapsed_after=3),
            customer_key="customer_id",
            date_key="order_date",
            value_key="revenue",
        )


def test_refuses_when_every_customer_looks_the_same() -> None:
    """With no variation in recency there is no boundary to learn."""
    with pytest.raises(churn.InsufficientDataError):
        churn.fit(
            _transactions(30, lapsed_after=0),
            customer_key="customer_id",
            date_key="order_date",
            value_key="revenue",
        )


def test_identifies_lapsed_customers() -> None:
    model = churn.fit(
        _transactions(40, lapsed_after=12),
        customer_key="customer_id",
        date_key="order_date",
        value_key="revenue",
    )

    assert model.customers == 40
    assert model.lapsed == 12
    # The planted lapsed cohort is C-000..C-011.
    top = {item.customer for item in model.at_risk[:5]}
    assert all(int(name.split("-")[1]) < 12 for name in top)


def test_churn_definition_is_returned() -> None:
    """A churn figure whose definition is hidden cannot be argued with."""
    model = churn.fit(
        _transactions(40, lapsed_after=12),
        customer_key="customer_id",
        date_key="order_date",
        value_key="revenue",
    )

    assert "lapsed" in model.definition.lower()
    assert str(int(model.cutoff_days)) in model.definition


def test_recency_is_excluded_from_predictors() -> None:
    """Recency defines the label; using it too would report a fake ~100%.

    The drivers must be the *other* features — what else predicts lapsing.
    """
    model = churn.fit(
        _transactions(40, lapsed_after=12),
        customer_key="customer_id",
        date_key="order_date",
        value_key="revenue",
    )

    assert "recency_days" not in model.drivers
    assert set(model.drivers) == {"frequency", "monetary", "tenure_days"}


def test_every_at_risk_customer_has_a_reason() -> None:
    model = churn.fit(
        _transactions(40, lapsed_after=12),
        customer_key="customer_id",
        date_key="order_date",
        value_key="revenue",
    )

    for item in model.at_risk:
        assert item.reason
        assert item.band in {"high", "medium", "low"}
        assert 0.0 <= item.risk <= 1.0


def test_as_of_is_the_last_date_in_the_data() -> None:
    """A file exported months ago must not mark everyone as churned."""
    rows = _transactions(40, lapsed_after=12)
    customers, features, as_of = churn.build_features(
        rows, customer_key="customer_id", date_key="order_date", value_key="revenue"
    )

    assert as_of.date() == date(2026, 1, 1) + timedelta(days=190)
    assert len(customers) == 40
    # No customer can have negative recency relative to the newest record.
    assert features[:, 0].min() >= 0
