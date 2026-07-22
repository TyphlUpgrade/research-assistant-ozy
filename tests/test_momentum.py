"""Tests for research_assistant.momentum — vol-scaled time-series momentum."""
from __future__ import annotations

import pandas as pd

from research_assistant.momentum import ts_momentum, yang_zhang_vol


def _ohlc(closes):
    c = pd.Series(closes, dtype=float)
    o = c.shift(1).fillna(c.iloc[0])
    hi = pd.concat([o, c], axis=1).max(axis=1) * 1.01
    lo = pd.concat([o, c], axis=1).min(axis=1) * 0.99
    return o, hi, lo, c


UP = [100 * (1.01 ** i) for i in range(40)]
DOWN = [100 * (0.99 ** i) for i in range(40)]
CHOP = [100 + (2 if i % 2 else -2) for i in range(40)]


def test_uptrend_positive_vol_scaled():
    m = ts_momentum(*_ohlc(UP), lookback=20)
    assert m["direction"] == "up"
    assert m["momentum_return"] > 0
    assert m["vol_scaled"] is not None and m["vol_scaled"] > 1


def test_downtrend_negative_vol_scaled():
    m = ts_momentum(*_ohlc(DOWN), lookback=20)
    assert m["direction"] == "down"
    assert m["vol_scaled"] is not None and m["vol_scaled"] < -1


def test_chop_weaker_than_trend():
    trend = ts_momentum(*_ohlc(UP), lookback=20)["vol_scaled"]
    chop = ts_momentum(*_ohlc(CHOP), lookback=20)["vol_scaled"]
    assert abs(chop) < abs(trend)


def test_vol_scaling_makes_moves_comparable():
    # Same +20% net move, different realized vol → different vol_scaled.
    # Low-vol grind vs a jumpy path that ends at the same close.
    smooth = [100 * (1.00919 ** i) for i in range(21)]          # ~+20% smooth
    jumpy = [100, 130, 95, 140, 90, 145, 92, 150, 88, 152, 90,
             155, 92, 150, 95, 148, 100, 145, 105, 140, 120]     # ~+20% jumpy
    vs_smooth = ts_momentum(*_ohlc(smooth), lookback=20)["vol_scaled"]
    vs_jumpy = ts_momentum(*_ohlc(jumpy), lookback=20)["vol_scaled"]
    assert vs_smooth > vs_jumpy  # same return, less risk → stronger trend signal


def test_yang_zhang_vol_positive_and_sane():
    o, h, l, c = _ohlc(UP)
    v = yang_zhang_vol(o, h, l, c, window=20)
    assert v is not None and 0 < v < 5  # annualized fraction, not absurd


def test_insufficient_bars_returns_none_not_crash():
    m = ts_momentum(*_ohlc(UP[:5]), lookback=20)
    assert m["vol_scaled"] is None
    assert m["momentum_return"] is None
    assert yang_zhang_vol(*_ohlc(UP[:5]), window=20) is None


def test_degenerate_prices_return_none():
    bad = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0])
    assert yang_zhang_vol(bad, bad, bad, bad, window=3) is None
