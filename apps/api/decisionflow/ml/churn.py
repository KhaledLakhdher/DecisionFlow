"""Repeat-purchase (churn) risk from transaction history.

Built on RFM — recency, frequency, monetary value — because that is what a
transaction table can actually support. A CSV of orders contains no support
tickets, no logins, no contract dates; inventing engagement features from it
would be fabrication.

The label is defined, not given. No upload says "this customer churned", so
churn is derived: a customer is treated as lapsed when their most recent
purchase is older than a cutoff based on the observed gap between purchases.
That definition is returned alongside the predictions, because a churn number
whose definition is hidden cannot be argued with — and it should be arguable.

Logistic regression rather than gradient boosting. The lift on a few hundred
customers is negligible, and coefficients give a per-customer reason
("last purchase 94 days ago") that a tree ensemble cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

# Below this, a fitted model is noise with a decimal point.
MIN_CUSTOMERS = 20
# Both classes must be present for a classifier to be meaningful.
MIN_PER_CLASS = 3

FEATURE_LABELS = {
    "recency_days": "days since last purchase",
    "frequency": "number of purchases",
    "monetary": "total spend",
    "tenure_days": "days as a customer",
}


class InsufficientDataError(Exception):
    """Not enough history to model churn honestly."""


@dataclass(slots=True)
class CustomerRisk:
    customer: str
    risk: float               # probability of being lapsed
    band: str                 # "high" | "medium" | "low"
    recency_days: float
    frequency: int
    monetary: float
    reason: str


@dataclass(slots=True)
class ChurnModel:
    definition: str
    cutoff_days: float
    customers: int
    lapsed: int
    accuracy: float | None
    # Signed coefficient per feature, so the direction of each driver is
    # visible rather than just its magnitude.
    drivers: dict[str, float] = field(default_factory=dict)
    at_risk: list[CustomerRisk] = field(default_factory=list)


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def build_features(
    rows: list[dict[str, Any]],
    *,
    customer_key: str,
    date_key: str,
    value_key: str | None,
) -> tuple[list[str], np.ndarray, datetime]:
    """Aggregate transactions into per-customer RFM features."""
    by_customer: dict[str, list[tuple[datetime, float]]] = {}

    for row in rows:
        customer = row.get(customer_key)
        raw_date = row.get(date_key)
        if customer is None or raw_date is None:
            continue
        try:
            when = _parse(raw_date)
        except (ValueError, TypeError):
            continue
        amount = 0.0
        if value_key is not None and row.get(value_key) is not None:
            try:
                amount = float(row[value_key])
            except (TypeError, ValueError):
                amount = 0.0
        by_customer.setdefault(str(customer), []).append((when, amount))

    if not by_customer:
        raise InsufficientDataError("No usable customer transactions were found.")

    # "Now" is the last date in the data, not the wall clock. A file exported
    # six months ago would otherwise show every customer as churned.
    as_of = max(when for history in by_customer.values() for when, _ in history)

    customers: list[str] = []
    features: list[list[float]] = []
    for customer, history in by_customer.items():
        dates = [when for when, _ in history]
        first, last = min(dates), max(dates)
        customers.append(customer)
        features.append(
            [
                (as_of - last).days,
                len(history),
                sum(amount for _, amount in history),
                (last - first).days,
            ]
        )

    return customers, np.array(features, dtype=float), as_of


def _cutoff_days(recency: np.ndarray) -> float:
    """Where to draw the line between active and lapsed.

    The 70th percentile of observed recency, floored at 30 days. Derived from
    the data rather than hard-coded, because "90 days" means something very
    different for weekly groceries than for annual software renewals.

    Rounded to whole days: a percentile lands on values like 50.9999, and
    reporting the threshold as a float lets the stored number and the sentence
    describing it disagree by one.
    """
    return float(round(max(30.0, float(np.percentile(recency, 70)))))


def fit(
    rows: list[dict[str, Any]],
    *,
    customer_key: str,
    date_key: str,
    value_key: str | None = None,
    top_n: int = 10,
) -> ChurnModel:
    """Fit a churn model and score every customer."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    customers, features, as_of = build_features(
        rows, customer_key=customer_key, date_key=date_key, value_key=value_key
    )

    if len(customers) < MIN_CUSTOMERS:
        raise InsufficientDataError(
            f"Churn modelling needs at least {MIN_CUSTOMERS} customers; "
            f"this dataset has {len(customers)}."
        )

    recency = features[:, 0]
    cutoff = _cutoff_days(recency)
    labels = (recency > cutoff).astype(int)

    if int(labels.sum()) < MIN_PER_CLASS or int((1 - labels).sum()) < MIN_PER_CLASS:
        raise InsufficientDataError(
            "Customer activity is too uniform to separate lapsed from active "
            "accounts — every customer last purchased at a similar time."
        )

    # Recency defines the label, so including it as a predictor would let the
    # model rediscover the cutoff and report a meaningless ~100% accuracy.
    # The honest question is what *else* predicts lapsing.
    predictors = features[:, 1:]
    names = ["frequency", "monetary", "tenure_days"]

    scaler = StandardScaler()
    scaled = scaler.fit_transform(predictors)

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(scaled, labels)

    probabilities = model.predict_proba(scaled)[:, 1]
    accuracy = float(model.score(scaled, labels))
    drivers = {
        name: float(coefficient)
        for name, coefficient in zip(names, model.coef_[0], strict=True)
    }

    ranked = sorted(
        range(len(customers)), key=lambda i: probabilities[i], reverse=True
    )[:top_n]

    at_risk = [
        CustomerRisk(
            customer=customers[i],
            risk=float(probabilities[i]),
            band="high"
            if probabilities[i] >= 0.7
            else "medium"
            if probabilities[i] >= 0.4
            else "low",
            recency_days=float(features[i, 0]),
            frequency=int(features[i, 1]),
            monetary=float(features[i, 2]),
            reason=(
                f"Last purchased {int(features[i, 0])} days ago, "
                f"{int(features[i, 1])} purchase(s) totalling {features[i, 2]:,.0f}."
            ),
        )
        for i in ranked
    ]

    return ChurnModel(
        definition=(
            f"A customer is treated as lapsed when their last purchase is more "
            f"than {cutoff:.0f} days before {as_of.date().isoformat()}, the most "
            f"recent date in the data. That threshold is the 70th percentile of "
            f"observed gaps, not a fixed rule."
        ),
        cutoff_days=cutoff,
        customers=len(customers),
        lapsed=int(labels.sum()),
        # In-sample accuracy on a small dataset — reported for transparency,
        # not as evidence of generalisation. Cross-validation on a few dozen
        # customers would be equally unreliable and more work to explain.
        accuracy=accuracy,
        drivers=drivers,
        at_risk=at_risk,
    )
