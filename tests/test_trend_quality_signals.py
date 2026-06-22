"""Trend-quality guards: single_day_share_30d + trend_efficiency_10d.

These exist so the Stage-3 momentum-continuation gate can't be fooled by a
single gap day plus chop (a lagging-ADX false positive — the MRVL case).
"""
import pandas as pd

from research_assistant.data_loader import _single_day_share, _trend_efficiency


def test_one_gap_then_chop_is_flagged():
    # One +40% gap, then 15 sessions of flat oscillation (the MRVL shape).
    gap = pd.Series(
        [100] * 16 + [140] + [139, 141, 140, 138, 141, 139, 140, 141, 139, 140, 141, 139, 140, 141]
    )
    share = _single_day_share(gap, 30)
    eff = _trend_efficiency(gap, 10)
    assert share is not None and share >= 0.6, share          # one day IS the move
    assert eff is not None and eff <= 0.30, eff               # post-gap churn


def test_steady_trend_is_not_flagged():
    ramp = pd.Series([100 * (1.01 ** i) for i in range(35)])  # +1%/day, broad-based
    assert _single_day_share(ramp, 30) < 0.6
    assert _trend_efficiency(ramp, 10) >= 0.9                 # clean directional


def test_null_safe_on_short_or_flat_history():
    assert _single_day_share(pd.Series([100, 101, 102]), 30) is None   # too short
    assert _trend_efficiency(pd.Series([100, 101, 102]), 10) is None
    flat = pd.Series([100.0] * 35)
    assert _single_day_share(flat, 30) is None                # net < 5% → undefined
    assert _trend_efficiency(flat, 10) is None                # zero path → None


if __name__ == "__main__":
    test_one_gap_then_chop_is_flagged()
    test_steady_trend_is_not_flagged()
    test_null_safe_on_short_or_flat_history()
    print("trend-quality signal checks OK")
