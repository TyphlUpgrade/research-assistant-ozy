"""
Swing-pivot market structure — turns a price series into *structure*.

The cascade today reads trend from pointwise indicators on the latest bar:
EMA alignment (`daily_trend`, `classify_trend_structure`), ADX, MACD, plus the
scalar gap/churn guards (`single_day_share_30d`, `trend_efficiency_10d`). None
of them see the *shape* of the chart — the actual swing highs and lows and
their sequence. This module supplies that missing primitive: fractal swing
pivots → a higher-high/higher-low (HH-HL) vs lower-high/lower-low (LH-LL)
market-structure label, plus the last/prior swing levels (raw material for
support/resistance and breakout detection later).

Deterministic, O(n), no new dependencies (pure pandas/numpy). This is a leaf
module: it computes structure and returns it. It is intentionally NOT wired
into `data_loader` / the composite score yet — a later PR decides whether it
feeds only the Stage 2 narrative (like `trend_efficiency_10d` does today) or
the deterministic ranker. Keeping this PR behavior-neutral mirrors the repo's
`intraday volume profile (PR 1/2 — no behavior change)` pattern.

Anchorability: every output is a plain number or a small enum derived
transparently from the bars, so it slots into the evidence-anchor contract the
cascade is built on — unlike an opaque ML pattern classifier, which is why
this deliberately does swing structure, not template/CNN pattern matching.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd


def swing_pivots(
    high: pd.Series, low: pd.Series, window: int = 3
) -> list[tuple[Any, float, str]]:
    """Fractal swing points over `high`/`low`.

    A swing high is a bar whose high is the strict maximum of the window of
    ``2*window+1`` bars centred on it; a swing low is the symmetric minimum.
    `window=3` ≈ the classic 5-bar Williams fractal widened slightly; larger
    windows yield fewer, more significant pivots.

    Returns a time-ordered list of ``(index_label, price, kind)`` with kind in
    ``{"H", "L"}``. The first and last `window` bars can't be confirmed (no
    room on one side) and are excluded — a pivot needs `window` bars on BOTH
    sides. On a flat plateau the ``argmax/argmin == window`` centre check means
    only a bar that is itself the peak/trough registers, so equal-value shelves
    don't emit duplicate pivots.

    `high` and `low` must share the same index and be numeric. Returns [] when
    there aren't enough bars to confirm a single pivot.
    """
    n = len(high)
    if n < 2 * window + 1 or len(low) != n:
        return []
    hv = high.to_numpy()
    lv = low.to_numpy()
    idx = high.index
    pivots: list[tuple[Any, float, str]] = []
    for i in range(window, n - window):
        seg_h = hv[i - window : i + window + 1]
        seg_l = lv[i - window : i + window + 1]
        if int(seg_h.argmax()) == window:
            pivots.append((idx[i], float(hv[i]), "H"))
        elif int(seg_l.argmin()) == window:
            pivots.append((idx[i], float(lv[i]), "L"))
    return pivots


def market_structure(
    high: pd.Series, low: pd.Series, window: int = 3
) -> dict[str, Optional[float] | str | int]:
    """Classify recent price structure from the last two swing highs and lows.

    - ``uptrend``   : last swing high > prior swing high AND last swing low >
                      prior swing low (HH-HL)
    - ``downtrend`` : last swing high < prior swing high AND last swing low <
                      prior swing low (LH-LL)
    - ``range``     : anything else, or too few pivots to decide

    Returns a dict with the label plus the levels that produced it so a caller
    can anchor evidence and reason about proximity to structure:
    ``structure``, ``last_swing_high``, ``last_swing_low``,
    ``prior_swing_high``, ``prior_swing_low``, ``n_pivots``.
    """
    piv = swing_pivots(high, low, window)
    highs = [p[1] for p in piv if p[2] == "H"]
    lows = [p[1] for p in piv if p[2] == "L"]

    out: dict[str, Optional[float] | str | int] = {
        "structure": "range",
        "last_swing_high": highs[-1] if highs else None,
        "last_swing_low": lows[-1] if lows else None,
        "prior_swing_high": highs[-2] if len(highs) >= 2 else None,
        "prior_swing_low": lows[-2] if len(lows) >= 2 else None,
        "n_pivots": len(piv),
    }
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]
        hl = lows[-1] > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1] < lows[-2]
        if hh and hl:
            out["structure"] = "uptrend"
        elif lh and ll:
            out["structure"] = "downtrend"
    return out


def _demo() -> None:
    """Runnable self-check: ``python -m research_assistant.structure``.

    Uses window=1 (3-bar fractal) on hand-built zigzags so the expected
    pivots are obvious by eye. Asserts the three structure verdicts.
    """
    # Rising zigzag: peaks 12<14<16<18<20, troughs 11<13<15<17 → HH-HL.
    up = pd.Series([10, 12, 11, 14, 13, 16, 15, 18, 17, 20, 19])
    ms_up = market_structure(up, up, window=1)
    assert ms_up["structure"] == "uptrend", ms_up

    # Falling zigzag: mirror of the above → LH-LL.
    down = pd.Series([20, 18, 19, 16, 17, 14, 15, 12, 13, 10, 11])
    ms_down = market_structure(down, down, window=1)
    assert ms_down["structure"] == "downtrend", ms_down

    # Flat oscillation between 10 and 12 → no HH/LL progression → range.
    flat = pd.Series([10, 12, 10, 12, 10, 12, 10, 12, 10, 12, 10])
    ms_flat = market_structure(flat, flat, window=1)
    assert ms_flat["structure"] == "range", ms_flat

    # Too short to confirm any pivot at the default window.
    assert swing_pivots(up.head(3), up.head(3), window=3) == []

    print("structure self-check ok:", ms_up["structure"], ms_down["structure"], ms_flat["structure"])


if __name__ == "__main__":
    _demo()
