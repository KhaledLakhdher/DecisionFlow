"""Contribution analysis: does it name the right cause, and refuse to invent one?

The arithmetic is separated from the I/O precisely so these can be written
without a warehouse. Each fixture plants a *known* structure — one segment
carrying a collapse, or a decline spread evenly — and asserts the module
reaches the conclusion the data actually supports.

The broad-based cases carry the most weight. Naming a culprit is the easy
behaviour to get right and the dangerous one to get wrong: an owner told that
Electronics caused a decline will go and act on Electronics.
"""

from __future__ import annotations

import pytest

from decisionflow.data import contribution
from decisionflow.data.semantics import (
    ColumnSemantics,
    DatasetSemantics,
    SemanticRole,
    SemanticTag,
)


def _rows(pairs: list[tuple[str, float, float]]) -> list[dict[str, object]]:
    """Segments as (name, current, previous)."""
    return [
        {"segment": name, "current_value": current, "previous_value": previous}
        for name, current, previous in pairs
    ]


def _explain(pairs: list[tuple[str, float, float]]) -> contribution.DimensionExplanation:
    return contribution.explain(
        _rows(pairs), dimension="category", measure="revenue", sql="SELECT 1"
    )


# --------------------------------------------------------------------------
# Concentrated change — there is a culprit, and it should be named
# --------------------------------------------------------------------------
def test_identifies_the_segment_carrying_a_collapse() -> None:
    """Electronics loses 800 while everything else barely moves."""
    result = _explain(
        [
            ("electronics", 200.0, 1000.0),
            ("grocery", 500.0, 520.0),
            ("apparel", 300.0, 290.0),
        ]
    )

    assert result.concentrated
    leader = result.leader
    assert leader is not None
    assert leader.segment == "electronics"
    assert leader.delta == pytest.approx(-800.0)


def test_share_exceeds_one_when_other_segments_offset_the_move() -> None:
    """A segment can more than explain a change, and that should be visible."""
    # Net change is -500, but electronics alone fell 800; grocery offset 300.
    result = _explain([("electronics", 200.0, 1000.0), ("grocery", 800.0, 500.0)])

    assert result.total_delta == pytest.approx(-500.0)
    leader = result.leader
    assert leader is not None
    assert leader.share == pytest.approx(1.6)


# --------------------------------------------------------------------------
# Offsetting movement — the net figure hides what happened
# --------------------------------------------------------------------------
def test_detects_when_segments_move_against_each_other() -> None:
    """The case found in real data: one region appears, another collapses.

    Net revenue rose 290, which read alone suggests a quiet month. It was not
    a quiet month.
    """
    result = _explain([("west", 2980.0, 0.0), ("north", 710.0, 3400.0)])

    assert result.total_delta == pytest.approx(290.0)
    assert result.gross_movement == pytest.approx(5670.0)
    assert result.offsetting

    assert result.riser is not None and result.riser.segment == "west"
    assert result.faller is not None and result.faller.segment == "north"


def test_share_is_absurd_precisely_when_offsetting_is_true() -> None:
    """Guards the reason the flag exists.

    2980 / 290 is 1028% — arithmetically correct, and worthless as a sentence.
    `offsetting` is what tells the caller to stop quoting shares.
    """
    result = _explain([("west", 2980.0, 0.0), ("north", 710.0, 3400.0)])

    leader = result.leader
    assert leader is not None
    assert leader.share is not None and abs(leader.share) > 10
    assert result.offsetting


def test_a_clean_concentrated_drop_is_not_offsetting() -> None:
    """Everything moving the same way must keep the share-based phrasing."""
    result = _explain(
        [("electronics", 200.0, 1000.0), ("grocery", 480.0, 520.0)]
    )

    assert result.concentrated
    assert not result.offsetting


def test_gross_movement_exceeds_net_when_segments_cancel() -> None:
    result = _explain([("a", 0.0, 500.0), ("b", 500.0, 0.0)])

    assert result.total_delta == pytest.approx(0.0)
    assert result.gross_movement == pytest.approx(1000.0)
    # Share is undefined against a net change of zero — reported as None rather
    # than as a division that silently produces infinity.
    assert result.leader is not None
    assert result.leader.share is None


# --------------------------------------------------------------------------
# Broad-based change — there is no culprit, and none should be named
# --------------------------------------------------------------------------
def test_an_evenly_spread_decline_is_not_concentrated() -> None:
    """Ten segments each losing 100. No single one explains anything."""
    result = _explain([(f"segment_{index}", 400.0, 500.0) for index in range(10)])

    assert result.total_delta == pytest.approx(-1000.0)
    assert not result.concentrated
    assert result.concentration == pytest.approx(0.1)


def test_two_equal_movers_fall_short_of_the_threshold() -> None:
    """Exactly at the boundary the answer must be 'not concentrated'.

    Both segments collapsed identically. Naming either one as the cause would
    be arbitrary, and the reader would go and act on the arbitrary half.
    """
    result = _explain([("a", 0.0, 500.0), ("b", 0.0, 500.0)])

    assert result.concentration == pytest.approx(0.5)
    assert not result.concentrated


def test_no_movement_at_all_is_not_concentrated() -> None:
    result = _explain([("a", 500.0, 500.0), ("b", 300.0, 300.0)])

    assert result.gross_movement == pytest.approx(0.0)
    assert result.concentration == pytest.approx(0.0)
    assert not result.concentrated


# --------------------------------------------------------------------------
# Ranking and truncation
# --------------------------------------------------------------------------
def test_ranks_by_absolute_movement_so_a_surge_is_not_buried() -> None:
    """The biggest riser is as much the story as the biggest faller."""
    result = _explain(
        [("steady", 100.0, 100.0), ("faller", 50.0, 200.0), ("riser", 900.0, 100.0)]
    )

    assert [segment.segment for segment in result.segments[:2]] == ["riser", "faller"]


def test_statistics_use_every_segment_not_just_the_listed_ones() -> None:
    """Truncating before computing would overstate concentration badly."""
    many = [(f"segment_{index}", 0.0, 100.0) for index in range(40)]
    result = _explain(many)

    assert len(result.segments) == contribution.MAX_SEGMENTS
    assert result.total_delta == pytest.approx(-4000.0)
    # 1/40 of the movement, not 1/8 — the cap is presentational only.
    assert result.concentration == pytest.approx(0.025)
    assert not result.concentrated


def test_best_explanation_prefers_the_most_concentrated_dimension() -> None:
    diffuse = contribution.explain(
        _rows([(f"s{index}", 90.0, 100.0) for index in range(10)]),
        dimension="region",
        measure="revenue",
        sql="SELECT 1",
    )
    sharp = contribution.explain(
        _rows([("electronics", 100.0, 1000.0), ("grocery", 500.0, 495.0)]),
        dimension="category",
        measure="revenue",
        sql="SELECT 2",
    )

    best = contribution.best_explanation([diffuse, sharp])
    assert best is not None
    assert best.dimension == "category"


def test_best_explanation_ignores_dimensions_with_no_movement() -> None:
    """A dimension where nothing changed explains nothing, however tidily."""
    still = contribution.explain(
        _rows([("a", 100.0, 100.0)]), dimension="channel", measure="revenue", sql="x"
    )
    moving = contribution.explain(
        _rows([("a", 0.0, 100.0), ("b", 100.0, 90.0)]),
        dimension="category",
        measure="revenue",
        sql="y",
    )

    best = contribution.best_explanation([still, moving])
    assert best is not None
    assert best.dimension == "category"


def test_best_explanation_is_none_when_nothing_moved_anywhere() -> None:
    still = contribution.explain(
        _rows([("a", 100.0, 100.0)]), dimension="channel", measure="revenue", sql="x"
    )
    assert contribution.best_explanation([still]) is None


# --------------------------------------------------------------------------
# SQL construction
# --------------------------------------------------------------------------
def _semantics(*, with_time: bool = True) -> DatasetSemantics:
    columns = [
        ColumnSemantics(
            column="amount", role=SemanticRole.MEASURE, tags=[SemanticTag.MONETARY]
        ),
        ColumnSemantics(column="category", role=SemanticRole.DIMENSION),
    ]
    if with_time:
        columns.append(
            ColumnSemantics(column="ordered_at", role=SemanticRole.TIME)
        )
    return DatasetSemantics(columns=columns)


def test_contribution_sql_binds_periods_rather_than_interpolating_them() -> None:
    built = contribution.build_contribution_sql(
        _semantics(), schema="clean", table="orders", dimension="category"
    )
    assert built is not None
    sql, measure = built

    assert measure == "amount"
    # Two bound parameters, one per period. These are the only values in the
    # query that are not validated identifiers.
    assert sql.count("?") == 2
    assert "sum(amount)" in sql
    assert "clean.orders" in sql


def test_contribution_sql_counts_records_without_a_revenue_column() -> None:
    semantics = DatasetSemantics(
        columns=[
            ColumnSemantics(column="category", role=SemanticRole.DIMENSION),
            ColumnSemantics(column="ordered_at", role=SemanticRole.TIME),
        ]
    )
    built = contribution.build_contribution_sql(
        semantics, schema="clean", table="orders", dimension="category"
    )
    assert built is not None
    sql, measure = built

    assert measure == "records"
    assert "count(*)" in sql


def test_no_time_column_means_no_change_to_explain() -> None:
    assert (
        contribution.build_contribution_sql(
            _semantics(with_time=False),
            schema="clean",
            table="orders",
            dimension="category",
        )
        is None
    )
    assert (
        contribution.build_period_sql(
            _semantics(with_time=False), schema="clean", table="orders"
        )
        is None
    )


def test_a_hostile_dimension_name_is_rejected_not_quoted() -> None:
    from decisionflow.data.warehouse import WarehouseError

    with pytest.raises(WarehouseError):
        contribution.build_contribution_sql(
            _semantics(),
            schema="clean",
            table="orders",
            dimension="category; DROP TABLE orders --",
        )


def test_an_unsupported_grain_is_rejected() -> None:
    with pytest.raises(ValueError, match="grain"):
        contribution.build_contribution_sql(
            _semantics(),
            schema="clean",
            table="orders",
            dimension="category",
            grain="fortnight",
        )
