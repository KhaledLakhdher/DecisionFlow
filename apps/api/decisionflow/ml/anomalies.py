"""Anomaly detection on a time series.

Deliberately residual-based rather than a black box. A point is anomalous when
it departs from what the *trend* predicted — not merely when it is large.
December revenue being the year's highest is not an anomaly in a growing
business; December being triple the trend line is.

Isolation Forest and friends would find outliers too, but could not tell a
reader *why* a point was flagged. In a BI tool the explanation is the product:
"38% below the trend" is actionable, "anomaly score 0.83" is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

# Below this there are too few residuals for a spread estimate to mean
# anything, and everything looks like an outlier.
MIN_POINTS = 5
# Roughly the 99.7th percentile under normality. Deliberately conservative:
# a dashboard that flags every wobble trains people to ignore it.
DEFAULT_SENSITIVITY = 3.0


@dataclass(slots=True)
class Anomaly:
    period: str
    value: float
    expected: float
    deviation: float          # signed difference from the trend
    deviation_ratio: float    # as a share of expected
    z_score: float
    direction: str            # "above" | "below"
    severity: str             # "warning" | "serious"
    message: str


def _parse_period(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def detect(
    periods: list[Any],
    values: list[float | None],
    *,
    sensitivity: float = DEFAULT_SENSITIVITY,
) -> list[Anomaly]:
    """Points that depart materially from the fitted trend.

    Returns an empty list when the series is too short — an absence of
    findings, not an error. "Not enough data to judge" and "no anomalies" look
    the same to a caller here, which is acceptable because both mean "show
    nothing".
    """
    pairs = [
        (_parse_period(period), float(value))
        for period, value in zip(periods, values, strict=True)
        if value is not None
    ]
    if len(pairs) < MIN_POINTS:
        return []

    pairs.sort(key=lambda pair: pair[0])
    series = np.array([pair[1] for pair in pairs], dtype=float)
    x = np.arange(len(series), dtype=float)

    slope, intercept = np.polyfit(x, series, 1)
    expected = slope * x + intercept
    residuals = series - expected

    # Median absolute deviation, not standard deviation: the outliers being
    # hunted would otherwise inflate the very threshold meant to catch them.
    # 1.4826 rescales MAD to be comparable to a standard deviation for
    # normally distributed data.
    median_residual = float(np.median(residuals))
    mad = float(np.median(np.abs(residuals - median_residual)))
    spread = mad * 1.4826 if mad > 0 else float(np.std(residuals, ddof=1))

    if spread <= 0:
        # A perfectly straight series has no anomalies by construction.
        return []

    found: list[Anomaly] = []
    for i, (period, actual) in enumerate(pairs):
        z = float((residuals[i] - median_residual) / spread)
        if abs(z) < sensitivity:
            continue

        predicted = float(expected[i])
        deviation = float(actual - predicted)
        ratio = deviation / abs(predicted) if predicted else 0.0
        direction = "above" if deviation > 0 else "below"

        found.append(
            Anomaly(
                period=period.date().isoformat(),
                value=actual,
                expected=predicted,
                deviation=deviation,
                deviation_ratio=ratio,
                z_score=z,
                direction=direction,
                severity="serious" if abs(z) >= sensitivity * 1.5 else "warning",
                message=(
                    f"{period.date().isoformat()} came in {abs(ratio) * 100:.0f}% "
                    f"{direction} the trend "
                    f"({actual:,.0f} against an expected {predicted:,.0f})."
                ),
            )
        )

    return found
