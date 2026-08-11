"""Time-series forecasting.

The hard part of forecasting in a product like this is not the model — it is
refusing to produce one. A business uploads six months of data and asks what
next quarter looks like; a naive implementation fits a line and returns a
confident number that nobody should act on.

So the method is chosen by what the series can actually support, and thin data
is declined rather than extrapolated:

    fewer than 4 points  -> refuse. Two or three points define a line exactly;
                            the "trend" is an artifact of having no residuals.
    4 to 23 points       -> linear trend with proper prediction intervals.
    24 or more points    -> Holt-Winters, which can separate trend from
                            seasonality once two full annual cycles exist to
                            learn it from.

Every forecast carries an interval, never a bare point estimate. A single
number implies a precision that no forecast has, and the width of the band is
usually the most decision-relevant thing on the chart.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

import numpy as np
from scipy import stats

# Two or three points always fit a straight line perfectly, leaving zero
# residual variance and therefore an interval of zero width — a forecast that
# claims certainty it has not earned.
MIN_POINTS = 4
# Holt-Winters needs two complete cycles to distinguish "December is always
# high" from "things are trending up".
MIN_POINTS_SEASONAL = 24
DEFAULT_CONFIDENCE = 0.80


class ForecastMethod(StrEnum):
    LINEAR_TREND = "linear_trend"
    HOLT_WINTERS = "holt_winters"


class InsufficientDataError(Exception):
    """The series cannot support a forecast worth showing."""


@dataclass(slots=True)
class ForecastPoint:
    period: str
    value: float
    lower: float
    upper: float


@dataclass(slots=True)
class Forecast:
    method: ForecastMethod
    confidence: float
    points: list[ForecastPoint]
    # Plain-language account of what was fitted and how far it can be trusted.
    # This is a BI tool: an unexplained projection is not actionable.
    rationale: str
    history_points: int
    # Residual standard deviation as a share of the mean — a rough honesty
    # signal about how noisy the underlying series is.
    noise_ratio: float | None = None
    residuals: list[float] = field(default_factory=list)


def _infer_step(periods: list[datetime]) -> timedelta:
    """The spacing between periods, from the median gap.

    Median rather than mean: one missing month should not stretch every
    projected date.
    """
    if len(periods) < 2:
        return timedelta(days=30)
    gaps = [
        (periods[i + 1] - periods[i]).total_seconds() for i in range(len(periods) - 1)
    ]
    return timedelta(seconds=float(np.median(gaps)))


def _add_months(start: datetime, months: int) -> datetime:
    """Calendar-month arithmetic, clamped to the last valid day."""
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    # 31 January + 1 month is 28/29 February, not 31 February.
    last_day = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return start.replace(year=year, month=month, day=min(start.day, last_day))


def _next_periods(history: list[datetime], count: int) -> list[datetime]:
    """Future period labels.

    Calendar-aware for month-like spacing. Adding a fixed 30.44-day timedelta
    repeatedly makes monthly forecasts drift off the month boundary — the
    projection after 2026-05-01 comes out as 2026-05-31, which reads as the
    wrong month entirely.
    """
    step = _infer_step(history)
    last = history[-1]
    days = step.total_seconds() / 86400

    if 27 <= days <= 32:
        return [_add_months(last, i + 1) for i in range(count)]
    if 88 <= days <= 95:  # quarterly
        return [_add_months(last, 3 * (i + 1)) for i in range(count)]
    if 360 <= days <= 370:  # annual
        return [_add_months(last, 12 * (i + 1)) for i in range(count)]

    return [last + step * (i + 1) for i in range(count)]


def _parse_period(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def _linear_forecast(
    values: np.ndarray, horizon: int, confidence: float
) -> tuple[list[tuple[float, float, float]], np.ndarray]:
    """Ordinary least squares on the time index, with prediction intervals.

    The interval is a *prediction* interval, not a confidence interval on the
    mean: it must cover where a future observation lands, which is wider and is
    the question a business is actually asking.
    """
    n = len(values)
    x = np.arange(n, dtype=float)

    slope, intercept = np.polyfit(x, values, 1)
    fitted = slope * x + intercept
    residuals = values - fitted

    # Two parameters estimated, hence n-2 degrees of freedom.
    degrees = n - 2
    residual_std = float(np.sqrt(np.sum(residuals**2) / degrees))
    x_mean = float(np.mean(x))
    sum_squares = float(np.sum((x - x_mean) ** 2)) or 1.0
    t_crit = float(stats.t.ppf(0.5 + confidence / 2, degrees))

    points: list[tuple[float, float, float]] = []
    for step in range(1, horizon + 1):
        x0 = n - 1 + step
        prediction = slope * x0 + intercept
        # The interval widens with distance from the centre of the data — the
        # further out, the less the fit constrains the answer.
        margin = t_crit * residual_std * math.sqrt(
            1 + 1 / n + ((x0 - x_mean) ** 2) / sum_squares
        )
        points.append((float(prediction), float(prediction - margin), float(prediction + margin)))

    return points, residuals


def _holt_winters_forecast(
    values: np.ndarray, horizon: int, confidence: float, season_length: int
) -> tuple[list[tuple[float, float, float]], np.ndarray]:
    """Holt-Winters exponential smoothing with seasonality."""
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    with warnings.catch_warnings():
        # statsmodels is chatty about convergence on short series; the caller
        # already gates on length, and a warning stream is not an error.
        warnings.simplefilter("ignore")
        model = ExponentialSmoothing(
            values,
            trend="add",
            seasonal="add",
            seasonal_periods=season_length,
            initialization_method="estimated",
        ).fit()

    prediction = np.asarray(model.forecast(horizon), dtype=float)
    residuals = np.asarray(values - model.fittedvalues, dtype=float)

    # Holt-Winters has no closed-form interval here, so derive one from
    # residual spread, widening with the square root of horizon as uncertainty
    # accumulates across steps.
    residual_std = float(np.std(residuals, ddof=1))
    z = float(stats.norm.ppf(0.5 + confidence / 2))

    points = [
        (
            float(prediction[i]),
            float(prediction[i] - z * residual_std * math.sqrt(i + 1)),
            float(prediction[i] + z * residual_std * math.sqrt(i + 1)),
        )
        for i in range(horizon)
    ]
    return points, residuals


def forecast_series(
    periods: list[Any],
    values: list[float | None],
    *,
    horizon: int = 3,
    confidence: float = DEFAULT_CONFIDENCE,
    allow_negative: bool = False,
) -> Forecast:
    """Project a series forward, or refuse if it is too thin to support one."""
    pairs = [
        (_parse_period(period), float(value))
        for period, value in zip(periods, values, strict=True)
        if value is not None
    ]
    if len(pairs) < MIN_POINTS:
        raise InsufficientDataError(
            f"A forecast needs at least {MIN_POINTS} periods of history; "
            f"this series has {len(pairs)}."
        )

    pairs.sort(key=lambda pair: pair[0])
    history = [pair[0] for pair in pairs]
    series = np.array([pair[1] for pair in pairs], dtype=float)
    horizon = max(1, min(horizon, 24))

    if len(series) >= MIN_POINTS_SEASONAL:
        method = ForecastMethod.HOLT_WINTERS
        raw, residuals = _holt_winters_forecast(series, horizon, confidence, 12)
        rationale = (
            f"Fitted Holt-Winters on {len(series)} periods, which is enough to "
            "separate seasonal effects from the underlying trend."
        )
    else:
        method = ForecastMethod.LINEAR_TREND
        raw, residuals = _linear_forecast(series, horizon, confidence)
        rationale = (
            f"Fitted a linear trend to {len(series)} periods. Too few cycles to "
            f"detect seasonality — that needs {MIN_POINTS_SEASONAL}."
        )

    mean = float(np.mean(series))
    noise_ratio = (
        float(np.std(residuals, ddof=1) / abs(mean)) if mean and len(residuals) > 1 else None
    )

    future = _next_periods(history, horizon)
    points = [
        ForecastPoint(
            period=future[i].date().isoformat(),
            # Revenue and counts cannot go negative; a band that dips below
            # zero is arithmetically fine and visibly wrong to a reader.
            value=raw[i][0] if allow_negative else max(raw[i][0], 0.0),
            lower=raw[i][1] if allow_negative else max(raw[i][1], 0.0),
            upper=raw[i][2] if allow_negative else max(raw[i][2], 0.0),
        )
        for i in range(horizon)
    ]

    if noise_ratio is not None and noise_ratio > 0.5:
        rationale += (
            " The history is volatile relative to its average, so treat the "
            "range as wide rather than the midpoint as likely."
        )

    return Forecast(
        method=method,
        confidence=confidence,
        points=points,
        rationale=rationale,
        history_points=len(series),
        noise_ratio=noise_ratio,
        residuals=[float(value) for value in residuals],
    )
