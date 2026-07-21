"""Tests for research_assistant.structure — swing-pivot market structure."""
from __future__ import annotations

import pandas as pd

from research_assistant.structure import market_structure, swing_pivots


def test_uptrend_hh_hl():
    up = pd.Series([10, 12, 11, 14, 13, 16, 15, 18, 17, 20, 19])
    ms = market_structure(up, up, window=1)
    assert ms["structure"] == "uptrend"
    assert ms["last_swing_high"] > ms["prior_swing_high"]
    assert ms["last_swing_low"] > ms["prior_swing_low"]


def test_downtrend_lh_ll():
    down = pd.Series([20, 18, 19, 16, 17, 14, 15, 12, 13, 10, 11])
    ms = market_structure(down, down, window=1)
    assert ms["structure"] == "downtrend"


def test_range_flat_oscillation():
    flat = pd.Series([10, 12, 10, 12, 10, 12, 10, 12, 10, 12, 10])
    ms = market_structure(flat, flat, window=1)
    assert ms["structure"] == "range"


def test_too_few_bars_returns_no_pivots():
    s = pd.Series([1.0, 2.0, 3.0])
    assert swing_pivots(s, s, window=3) == []


def test_insufficient_pivots_is_range_not_crash():
    # Monotonic ramp: no interior swing high/low → can't decide → range.
    ramp = pd.Series(range(20)).astype(float)
    ms = market_structure(ramp, ramp, window=3)
    assert ms["structure"] == "range"
    assert ms["n_pivots"] == 0


def test_pivots_labelled_and_ordered():
    up = pd.Series([10, 12, 11, 14, 13, 16, 15])
    piv = swing_pivots(up, up, window=1)
    kinds = [p[2] for p in piv]
    assert set(kinds) <= {"H", "L"}
    # index labels are time-ordered
    assert [p[0] for p in piv] == sorted(p[0] for p in piv)
