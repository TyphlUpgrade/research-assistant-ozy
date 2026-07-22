"""
Volatility-scaled time-series momentum — the best-evidenced trend primitive.

Today's trend read is EMA20-vs-EMA50 alignment plus scalar guards. The
quant-literature answer to "measure trend" is time-series momentum
(Moskowitz, Ooi & Pedersen 2012): the SIGN of an asset's own trailing return
says direction, and the return normalized by its own realized volatility says
strength *per unit of risk* — which is what makes two names comparable
(a +8% low-vol grind and a +8% high-vol lurch are different trends). See
FOLLOWUPS #31 for the verified evidence base and the OOS-edge caveat.

Volatility uses the Yang-Zhang (2000) OHLC range estimator, which Baltas-
Kosowski identify as the most efficient in its class — it folds in overnight
gaps (the open-vs-prior-close jump this tool is explicitly gap-blind about),
the intraday open-to-close move, and the Rogers-Satchell high/low range.

Deterministic, O(n), pure pandas/numpy — no new deps. Like `structure.py`
this is a leaf module with NO wiring into data_loader / the composite score;
a later PR decides narrative-only (like `trend_efficiency_10d`) vs ranker
input. Every output is a plain number derived transparently from bars, so it
fits the evidence-anchor contract.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

_TRADING_DAYS = 252


def yang_zhang_vol(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = 20,
    *,
    annualize: bool = True,
) -> Optional[float]:
    """Annualized Yang-Zhang realized volatility over the last `window` bars.

    YZ variance = overnight var + k·open-to-close var + (1-k)·Rogers-Satchell
    var, with k = 0.34 / (1.34 + (n+1)/(n-1)). Needs `window`+1 bars (the extra
    bar supplies the prior close for the first overnight term). Returns a daily
    stdev when ``annualize=False``, else scaled by sqrt(252). None on
    insufficient/degenerate data (non-positive prices, <2 usable bars, or a
    zero/None variance — a flat series has no meaningful vol to normalize by).
    """
    n = window + 1
    if any(s is None for s in (open_, high, low, close)):
        return None
    if len(close) < n or len(open_) < n or len(high) < n or len(low) < n:
        return None
    o = open_.to_numpy(dtype=float)[-n:]
    h = high.to_numpy(dtype=float)[-n:]
    l = low.to_numpy(dtype=float)[-n:]
    c = close.to_numpy(dtype=float)[-n:]
    if not (np.all(np.isfinite([o, h, l, c])) and np.all(o > 0)
            and np.all(h > 0) and np.all(l > 0) and np.all(c > 0)):
        return None

    # Log ratios. Overnight uses prior close, so it starts at bar 1.
    log_oc = np.log(c / o)            # open→close (intraday)
    log_co = np.log(o[1:] / c[:-1])   # prior-close→open (overnight gap)
    rs = (np.log(h / c) * np.log(h / o)) + (np.log(l / c) * np.log(l / o))

    m = window  # number of overnight observations == usable sample size
    if m < 2:
        return None
    # Variances over the last `window` bars (align intraday/RS to overnight).
    oc = log_oc[1:]
    rs_w = rs[1:]
    var_o = np.var(log_co, ddof=1)          # overnight
    var_c = np.var(oc, ddof=1)              # open-to-close
    var_rs = np.mean(rs_w)                  # Rogers-Satchell (mean, not var)
    k = 0.34 / (1.34 + (m + 1) / (m - 1))
    var_yz = var_o + k * var_c + (1.0 - k) * var_rs
    if not math.isfinite(var_yz) or var_yz <= 0:
        return None
    daily = math.sqrt(var_yz)
    return daily * math.sqrt(_TRADING_DAYS) if annualize else daily


def ts_momentum(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    lookback: int = 20,
) -> dict[str, Any]:
    """Volatility-scaled time-series momentum over `lookback` bars.

    Returns:
      - ``momentum_return`` : simple return over the lookback (C_t/C_{t-N}-1)
      - ``realized_vol``    : annualized Yang-Zhang vol
      - ``vol_scaled``      : log-return over the window ÷ the window's vol —
                              a unitless trend-strength-per-risk number, sign =
                              direction, comparable across tickers. None when
                              vol is unavailable/degenerate.
      - ``direction``       : "up" | "down" | "flat" from sign(vol_scaled)
                              (flat only when the return is exactly zero)
      - ``lookback``        : echoed for anchoring

    None-valued fields degrade gracefully (callers fall back to raw returns),
    matching the rest of the ticker_data contract.
    """
    out: dict[str, Any] = {
        "momentum_return": None,
        "realized_vol": None,
        "vol_scaled": None,
        "direction": None,
        "lookback": lookback,
    }
    if close is None or len(close) < lookback + 1:
        return out
    c = close.to_numpy(dtype=float)
    c0, c1 = c[-lookback - 1], c[-1]
    if not (math.isfinite(c0) and math.isfinite(c1) and c0 > 0 and c1 > 0):
        return out

    out["momentum_return"] = round(c1 / c0 - 1.0, 4)
    out["direction"] = "up" if c1 > c0 else "down" if c1 < c0 else "flat"

    vol_annual = yang_zhang_vol(open_, high, low, close, window=lookback)
    if vol_annual is not None:
        out["realized_vol"] = round(vol_annual, 4)
        # Normalize the log-return by the window's (de-annualized) vol:
        # daily_vol * sqrt(lookback) is the expected move over the window.
        daily_vol = vol_annual / math.sqrt(_TRADING_DAYS)
        window_vol = daily_vol * math.sqrt(lookback)
        if window_vol > 0:
            out["vol_scaled"] = round(math.log(c1 / c0) / window_vol, 2)
    return out


def _demo() -> None:
    """Runnable self-check: ``python -m research_assistant.momentum``.

    Builds synthetic OHLC from a close path (modest, constant intraday range
    so vol is finite and > 0), then asserts the sign and ordering of the
    vol-scaled momentum across a clean uptrend / downtrend / chop.
    """
    def ohlc_from_close(closes: list[float]) -> tuple:
        c = pd.Series(closes, dtype=float)
        o = c.shift(1).fillna(c.iloc[0])                # open = prior close
        hi = pd.concat([o, c], axis=1).max(axis=1) * 1.01
        lo = pd.concat([o, c], axis=1).min(axis=1) * 0.99
        return o, hi, lo, c

    up = [100 * (1.01 ** i) for i in range(30)]          # +1%/day compounding
    down = [100 * (0.99 ** i) for i in range(30)]        # -1%/day
    chop = [100 + (2 if i % 2 else -2) for i in range(30)]  # flat oscillation

    m_up = ts_momentum(*ohlc_from_close(up), lookback=20)
    m_down = ts_momentum(*ohlc_from_close(down), lookback=20)
    m_chop = ts_momentum(*ohlc_from_close(chop), lookback=20)

    assert m_up["vol_scaled"] is not None and m_up["vol_scaled"] > 1, m_up
    assert m_down["vol_scaled"] is not None and m_down["vol_scaled"] < -1, m_down
    # Chop nets ~flat → |vol_scaled| small vs a real trend.
    assert abs(m_chop["vol_scaled"]) < abs(m_up["vol_scaled"]), m_chop
    assert m_up["direction"] == "up" and m_down["direction"] == "down"
    assert m_up["realized_vol"] and m_up["realized_vol"] > 0

    # Too short → graceful None, no raise.
    short = ts_momentum(*ohlc_from_close(up[:5]), lookback=20)
    assert short["vol_scaled"] is None and short["momentum_return"] is None

    print(
        "momentum self-check ok:",
        f"up={m_up['vol_scaled']} down={m_down['vol_scaled']} "
        f"chop={m_chop['vol_scaled']} vol_up={m_up['realized_vol']}",
    )


if __name__ == "__main__":
    _demo()
