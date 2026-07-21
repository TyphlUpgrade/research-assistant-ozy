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


# --- directional-change (Guillaume/Olsen) ------------------------------------
from research_assistant.structure import (
    directional_change_state,
    directional_changes,
)


def test_dc_detects_downturn_at_peak():
    path = pd.Series([100, 105, 110, 120, 118, 112, 105])  # peak 120 then -12.5%
    dc = directional_changes(path, theta=0.10)
    downs = [e for e in dc if e["kind"] == "DC_down"]
    assert downs and downs[0]["ext_price"] == 120


def test_dc_confirm_after_extreme_no_lookahead():
    # confirm_index must be strictly later than the extreme it reverses.
    path = pd.Series([100, 110, 120, 118, 112, 105])
    dc = directional_changes(path, theta=0.10)
    e = dc[0]
    assert e["confirm_index"] > e["ext_index"]


def test_dc_state_mode_and_monotonic_rise():
    down_path = pd.Series([100, 105, 110, 120, 118, 112, 105])
    assert directional_change_state(down_path, theta=0.10)["mode"] == "down"
    mono = pd.Series([100, 101, 102, 103, 104, 105])
    assert directional_change_state(mono, theta=0.03)["mode"] == "up"


def test_dc_no_move_no_events():
    # Never retraces theta → no confirmed events, mode None.
    flat = pd.Series([100, 100.5, 100.2, 100.4, 100.1])
    st = directional_change_state(flat, theta=0.05)
    assert st["n_events"] == 0 and st["mode"] is None


def test_dc_theta_scale_adaptive():
    # A wiggly path yields more events at a tighter theta than a looser one.
    path = pd.Series([100, 103, 99, 104, 98, 105, 97, 106])
    tight = len(directional_changes(path, theta=0.02))
    loose = len(directional_changes(path, theta=0.08))
    assert tight >= loose
