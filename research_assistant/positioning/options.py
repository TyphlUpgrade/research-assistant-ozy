"""
Phase A — Agent B (deterministic options positioning).

Reads yfinance's option chain across all listed expiries and emits a
compressed positioning snapshot: IV term structure, 25-delta put-call skew
on the front month, put/call open-interest ratio, put/call volume ratio,
and a Phase-A approximation of the "unusual single-strike OI" flag (front-
month strike whose OI is > 3x the front-month mean).

The spec's full "single-strike OI vs its own 30d average" flag requires
historical OI tracking we don't have yet — Phase A approximates it
cross-sectionally (within the same expiry, vs mean strike OI) so the
synthesizer still gets a directional signal. A future PR can add the
true historical baseline once OI snapshots are persisted.

Three-state semantics mirror KPISummary:
  - None        → fetch failed / no listed options
  - empty       → fetch succeeded, no expiries (very rare)
  - populated   → render_for_prompt emits the compressed block
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)


# Cap on expiries surveyed. Tickers like SPY list 30+ weekly tenors; the
# substrate block only needs front + a few back tenors to characterise IV
# shape. Saves both fetch time and prompt tokens.
_MAX_EXPIRIES = 6


@dataclass
class ExpiryIV:
    """One expiry's IV summary."""
    expiry: str                          # YYYY-MM-DD
    days_to_expiry: int
    atm_iv: Optional[float] = None       # ATM IV as fraction (0.45 = 45%)
    put_oi: int = 0
    call_oi: int = 0
    put_volume: int = 0
    call_volume: int = 0
    # 25-delta put-call skew: IV(put_25d) - IV(call_25d). Positive = puts
    # are more expensive (downside hedging demand). Approximated by IV at
    # strikes ~10% below / above spot (a workable proxy when broker-grade
    # delta isn't in yfinance's chain).
    skew_25d_proxy: Optional[float] = None
    # Strike whose OI is more than 3x the expiry mean. None when nothing
    # in the chain meets the threshold.
    unusual_oi_strike: Optional[float] = None
    unusual_oi_side: Optional[str] = None     # "put" or "call"
    unusual_oi_multiple: Optional[float] = None


@dataclass
class OptionsPositioning:
    """Compressed options-market positioning surface.

    `expiries` is sorted by days_to_expiry ascending (front first). The
    aggregate ratios use ALL surveyed expiries (capped at _MAX_EXPIRIES).
    """
    symbol: str
    asof: str
    spot: Optional[float] = None
    expiries: list[ExpiryIV] = field(default_factory=list)
    # Aggregate (across all surveyed expiries)
    pc_oi_ratio: Optional[float] = None         # put_OI / call_OI
    pc_volume_ratio: Optional[float] = None     # put_vol / call_vol
    # Front-month convenience surface
    front_atm_iv: Optional[float] = None
    front_skew_25d_proxy: Optional[float] = None
    # Whether ANY expiry had an unusual single-strike OI (Phase A definition)
    unusual_oi_present: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.expiries

    def stage_2_block(self) -> str:
        if self.is_empty:
            return "(no options chain available)"
        lines: list[str] = []

        # Front-month line — IV + skew anchor
        front = self.expiries[0]
        front_parts = [f"front {front.expiry} ({front.days_to_expiry}d)"]
        if self.front_atm_iv is not None:
            front_parts.append(f"ATM IV {_fmt_pct(self.front_atm_iv)}")
        if self.front_skew_25d_proxy is not None:
            front_parts.append(
                f"25d skew {_fmt_pct_signed(self.front_skew_25d_proxy)}"
            )
        lines.append(", ".join(front_parts))

        # IV term-structure line — one cell per surveyed expiry.
        term = []
        for ex in self.expiries:
            iv_cell = (
                _fmt_pct(ex.atm_iv) if ex.atm_iv is not None else "?"
            )
            term.append(f"{ex.expiry}({ex.days_to_expiry}d):{iv_cell}")
        lines.append("term: " + " | ".join(term))

        # Aggregate ratios
        ratio_parts = []
        if self.pc_oi_ratio is not None:
            ratio_parts.append(f"P/C OI {self.pc_oi_ratio:.2f}")
        if self.pc_volume_ratio is not None:
            ratio_parts.append(f"P/C vol {self.pc_volume_ratio:.2f}")
        if ratio_parts:
            lines.append("ratios: " + ", ".join(ratio_parts))

        # Unusual single-strike OI — one line per expiry that has one.
        unusual_lines: list[str] = []
        for ex in self.expiries:
            if ex.unusual_oi_strike is None:
                continue
            mul = (
                f"{ex.unusual_oi_multiple:.1f}x"
                if ex.unusual_oi_multiple is not None
                else "?"
            )
            unusual_lines.append(
                f"unusual: {ex.expiry} {ex.unusual_oi_side} "
                f"strike ${ex.unusual_oi_strike:.0f} ({mul} mean strike OI)"
            )
        if unusual_lines:
            lines.extend(unusual_lines)

        return "\n".join(lines)

    @classmethod
    def render_for_prompt(cls, ops: Optional["OptionsPositioning"]) -> str:
        if ops is None:
            return (
                "(options positioning unavailable — yfinance fetch failed "
                "or no listed options)"
            )
        if ops.is_empty:
            return "(no options chain — ticker has no listed contracts)"
        return ops.stage_2_block()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

async def load_options_positioning(
    symbol: str,
    *,
    asof: Optional[date] = None,
    max_expiries: int = _MAX_EXPIRIES,
) -> Optional[OptionsPositioning]:
    """Fetch + summarise yfinance options chain. Returns None on failure."""
    try:
        return await asyncio.to_thread(
            _download_options_positioning, symbol, asof, max_expiries,
        )
    except Exception as exc:
        log.warning("load_options_positioning failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Yfinance integration (sync; run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _download_options_positioning(
    symbol: str,
    asof: Optional[date],
    max_expiries: int,
) -> OptionsPositioning:
    import yfinance as yf

    asof = asof or date.today()
    ticker = yf.Ticker(symbol)
    out = OptionsPositioning(symbol=symbol.upper(), asof=asof.isoformat())

    # Spot anchor — needed for ATM strike selection.
    try:
        info = ticker.fast_info
        spot = float(
            getattr(info, "last_price", None)
            or getattr(info, "regularMarketPrice", None)
            or 0.0
        )
        if math.isfinite(spot) and spot > 0:
            out.spot = spot
    except Exception as exc:
        log.debug("options: spot fetch failed for %s: %s", symbol, exc)

    # Walk expiries (yfinance lists them as ISO strings, front first)
    try:
        expiries = list(ticker.options or [])
    except Exception as exc:
        log.debug("options: expiries fetch failed for %s: %s", symbol, exc)
        return out

    expiries = expiries[:max_expiries]
    expiry_objs: list[ExpiryIV] = []
    total_put_oi = 0
    total_call_oi = 0
    total_put_vol = 0
    total_call_vol = 0

    for exp in expiries:
        try:
            chain = ticker.option_chain(exp)
        except Exception as exc:
            log.debug(
                "options: chain fetch failed for %s @ %s: %s", symbol, exp, exc,
            )
            continue
        try:
            dte = (datetime.fromisoformat(exp).date() - asof).days
        except ValueError:
            dte = 0
        calls = getattr(chain, "calls", None)
        puts = getattr(chain, "puts", None)
        if calls is None or puts is None:
            continue

        ex = ExpiryIV(expiry=exp, days_to_expiry=max(dte, 0))
        ex.put_oi = _sum_int_col(puts, "openInterest")
        ex.call_oi = _sum_int_col(calls, "openInterest")
        ex.put_volume = _sum_int_col(puts, "volume")
        ex.call_volume = _sum_int_col(calls, "volume")
        total_put_oi += ex.put_oi
        total_call_oi += ex.call_oi
        total_put_vol += ex.put_volume
        total_call_vol += ex.call_volume

        if out.spot is not None:
            ex.atm_iv = _atm_iv(calls, puts, out.spot)
            ex.skew_25d_proxy = _skew_proxy(calls, puts, out.spot)
            strike, side, mul = _unusual_oi(calls, puts)
            if strike is not None:
                ex.unusual_oi_strike = strike
                ex.unusual_oi_side = side
                ex.unusual_oi_multiple = mul
                out.unusual_oi_present = True

        expiry_objs.append(ex)

    out.expiries = sorted(expiry_objs, key=lambda e: e.days_to_expiry)

    if total_call_oi > 0:
        out.pc_oi_ratio = total_put_oi / total_call_oi
    if total_call_vol > 0:
        out.pc_volume_ratio = total_put_vol / total_call_vol
    if out.expiries:
        front = out.expiries[0]
        out.front_atm_iv = front.atm_iv
        out.front_skew_25d_proxy = front.skew_25d_proxy

    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sum_int_col(df, col: str) -> int:
    """Sum an integer column from a yfinance chain DataFrame. NaN → 0."""
    if df is None or col not in df.columns:
        return 0
    try:
        s = df[col].fillna(0).astype(int)
        return int(s.sum())
    except Exception:
        return 0


def _atm_iv(calls, puts, spot: float) -> Optional[float]:
    """Average call+put IV at the strike nearest to spot.

    Yfinance reports IV per row as `impliedVolatility` (fraction). When
    one side is missing for the ATM strike, the other side's IV is used."""
    call_iv = _strike_nearest_iv(calls, spot)
    put_iv = _strike_nearest_iv(puts, spot)
    vals = [v for v in (call_iv, put_iv) if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _strike_nearest_iv(df, spot: float) -> Optional[float]:
    if df is None or df.empty or "strike" not in df.columns or "impliedVolatility" not in df.columns:
        return None
    try:
        idx = (df["strike"] - spot).abs().idxmin()
        iv = float(df.loc[idx, "impliedVolatility"])
        return iv if math.isfinite(iv) and iv > 0 else None
    except Exception:
        return None


def _skew_proxy(calls, puts, spot: float) -> Optional[float]:
    """25-delta skew proxy: IV(put ~10% OTM) - IV(call ~10% OTM).

    Yfinance doesn't return greeks, so a true 25-delta strike pull is
    out of scope. The ~10%-OTM proxy is what institutional quants use
    when greek-grade data isn't available — directionally correct for
    swing horizons even if absolutely shifted vs the true 25-delta surface.
    """
    if spot <= 0:
        return None
    put_strike_target = spot * 0.90
    call_strike_target = spot * 1.10
    put_iv = _iv_at_strike_target(puts, put_strike_target)
    call_iv = _iv_at_strike_target(calls, call_strike_target)
    if put_iv is None or call_iv is None:
        return None
    return put_iv - call_iv


def _iv_at_strike_target(df, strike_target: float) -> Optional[float]:
    if df is None or df.empty or "strike" not in df.columns or "impliedVolatility" not in df.columns:
        return None
    try:
        idx = (df["strike"] - strike_target).abs().idxmin()
        iv = float(df.loc[idx, "impliedVolatility"])
        return iv if math.isfinite(iv) and iv > 0 else None
    except Exception:
        return None


def _unusual_oi(calls, puts) -> tuple[Optional[float], Optional[str], Optional[float]]:
    """Largest strike whose OI exceeds 3x the all-strike mean for that side.

    Returns (strike, side, multiple) or (None, None, None). When both
    sides have an unusual strike, returns the higher multiple.
    """
    cand: list[tuple[float, str, float]] = []
    for df, side in ((calls, "call"), (puts, "put")):
        if df is None or df.empty or "openInterest" not in df.columns:
            continue
        try:
            oi = df["openInterest"].fillna(0).astype(float)
            mean = float(oi.mean())
            if mean <= 0:
                continue
            max_idx = oi.idxmax()
            max_val = float(oi.iloc[oi.values.argmax()])  # robust to MultiIndex
            mul = max_val / mean if mean > 0 else 0.0
            if mul >= 3.0:
                strike = float(df.loc[max_idx, "strike"])
                cand.append((strike, side, mul))
        except Exception:
            continue
    if not cand:
        return None, None, None
    cand.sort(key=lambda t: -t[2])
    strike, side, mul = cand[0]
    return strike, side, mul


def _fmt_pct(fraction: Optional[float]) -> str:
    if fraction is None:
        return "?"
    return f"{fraction * 100:.1f}%"


def _fmt_pct_signed(fraction: Optional[float]) -> str:
    if fraction is None:
        return "?"
    sign = "+" if fraction >= 0 else ""
    return f"{sign}{fraction * 100:.1f}%"


# statistics is imported in case future Phase versions want median/stdev;
# silenced unused import via this re-export so ruff stays clean.
_ = statistics
