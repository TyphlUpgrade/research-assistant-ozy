"""
Data loader — bridges Ozy's yfinance_adapter + TA primitives to the shapes
research_assistant orchestrator + brief expect.

Three responsibilities:
  1. Per-ticker `ticker_data` dict for Stage 2/3 prompts
  2. Per-ticker `headlines` list with `absorption_stage` annotation
  3. World-state input dict for Stage 0 (SPY/QQQ/VIX/sector data)

Design choice — bypassing Ozy's MarketContextBuilder for v1:
  MarketContextBuilder.build() expects pre-computed indicator dicts in Ozy's
  medium-loop format (signals.trend_structure, long_score/short_score, etc.)
  + a full Config object + context_symbols/sector_map at construction. For
  v1, we assemble the Stage 0 input directly here — simpler, fewer moving
  parts, no execution-config coupling. The `market_context.py` wrapper
  remains as a seam for future richer-context wiring.

Concurrency: asyncio.Semaphore-bounded (default 5) per Critic iter1 #17
to avoid bursting yfinance + Anthropic rate limits.

This module is the SINGLE integration point with live yfinance. All other
research_assistant modules consume its output as plain dicts. Replace it
(or mock it) to swap data sources.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from ozymandias.core.market_hours import Session, get_current_session
from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
from ozymandias.intelligence.technical_analysis import (
    compute_ema,
    compute_rsi,
    generate_daily_signal_summary,
)

from research_assistant.volume_profile import (
    PROFILE_REFRESH_CAP,
    intraday_ratio_from_profile,
    is_stale,
    load_profile,
    refresh_profile,
)

log = logging.getLogger(__name__)


# Default sector ETFs surveyed for world-state (subset of Ozy's full sector map)
DEFAULT_SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")

# Macro reference instruments for Stage 0 world-state.
#
# 2026-06-08 (FOLLOWUPS #25): swapped `^VIX` → `^VIX9D`. Yahoo's `^VIX`
# endpoint intermittently returns "possibly delisted; no price data
# found" under concurrent load — observed across all 12 /research runs
# on 2026-06-08. `^VIX9D` (9-day VIX) is the closest liquid sibling
# (tracks `^VIX` within ~1 vol point in calm regimes, prints 1-3 points
# higher in stress), reads consistently from yfinance, and serves the
# same Stage-0 regime function. Display label in brief.py remains "VIX"
# — operators are reading the level/trend, not the precise tenor.
DEFAULT_MACRO_INSTRUMENTS = ("SPY", "QQQ", "^VIX9D")

# Concurrency cap (Critic iter1 #17)
DEFAULT_PARALLEL_FETCH = 5


# ---------------------------------------------------------------------------
# Per-ticker data
# ---------------------------------------------------------------------------

def _pct_return(series: pd.Series, lookback_bars: int) -> Optional[float]:
    """Percent change between current close and `lookback_bars` ago. None if insufficient history."""
    if series is None or len(series) <= lookback_bars:
        return None
    try:
        prev = float(series.iloc[-lookback_bars - 1])
        curr = float(series.iloc[-1])
        if prev == 0:
            return None
        return round((curr / prev) - 1.0, 4)
    except (IndexError, ValueError, TypeError):
        return None


def _single_day_share(close: pd.Series, lookback_bars: int = 30) -> Optional[float]:
    """Fraction of the lookback-window net move concentrated in its single
    biggest day.

    ~1.0 (or >1) means one gap day IS the move — chop/digestion around a
    one-off event, NOT a broad-based trend (ADX reads high off the gap and
    lags). Low values mean participation is spread across many sessions.
    None when the net move is ~flat (the ratio is undefined / not meaningful).
    """
    if close is None or len(close) < lookback_bars + 1:
        return None
    try:
        window = close.iloc[-(lookback_bars + 1):]
        net = abs(float(window.iloc[-1]) / float(window.iloc[0]) - 1.0)
        if net < 0.05:  # net under 5% — "share of move" isn't meaningful
            return None
        biggest = float(window.pct_change().dropna().abs().max())
        return round(biggest / net, 2)
    except (IndexError, ValueError, TypeError, ZeroDivisionError):
        return None


def _trend_efficiency(close: pd.Series, lookback_bars: int = 10) -> Optional[float]:
    """Kaufman efficiency ratio over the recent window: |net move| / sum of
    per-bar absolute moves. 1.0 = a clean directional trend; near 0 = wide
    oscillation with no net progress (the 'flat chop at the highs' shape).
    Complements _single_day_share: that flags the gap, this flags the churn
    after it."""
    if close is None or len(close) < lookback_bars + 1:
        return None
    try:
        window = close.iloc[-(lookback_bars + 1):]
        net = abs(float(window.iloc[-1]) - float(window.iloc[0]))
        path = float(window.diff().abs().sum())
        if path == 0:
            return None
        return round(net / path, 2)
    except (IndexError, ValueError, TypeError, ZeroDivisionError):
        return None


def _volume_5d_trend(volume: pd.Series) -> str:
    """Classify 5-day rolling-avg volume slope as rising / flat / declining."""
    if volume is None or len(volume) < 10:
        return "flat"
    try:
        rolling = volume.rolling(5).mean().dropna()
        if len(rolling) < 5:
            return "flat"
        first = float(rolling.iloc[-5])
        last = float(rolling.iloc[-1])
        if first <= 0:
            return "flat"
        ratio = last / first
        if ratio > 1.10:
            return "rising"
        if ratio < 0.90:
            return "declining"
        return "flat"
    except (IndexError, ValueError, TypeError, ZeroDivisionError):
        return "flat"


def _weekly_rsi_14(daily_close: pd.Series) -> Optional[float]:
    """RSI(14) on weekly-resampled closes. Needs ~15 weeks of daily history."""
    if daily_close is None or len(daily_close) < 75:  # ~15 weeks of trading days
        return None
    try:
        weekly = daily_close.resample("W").last().dropna()
        if len(weekly) < 15:
            return None
        weekly_df = pd.DataFrame({"close": weekly})
        rsi = compute_rsi(weekly_df, length=14)
        last = rsi.iloc[-1]
        return None if pd.isna(last) else round(float(last), 2)
    except Exception as exc:
        log.debug("weekly_rsi_14 failed: %s", exc)
        return None


def _volume_ratio_vs_20d(
    volume: pd.Series, *, asof_date: Optional[date] = None
) -> Optional[float]:
    """Most-recent COMPLETED daily bar's volume / prior-20-bar average.

    When the cascade runs intraday (e.g. the morning brief, right after the
    open), yfinance's latest daily bar is the in-progress session carrying
    only partial accumulated volume — dividing that by full-day averages
    produced a spuriously tiny ratio (~0.04 at the open) that corrupted the
    volume signal across every ticker. When the latest bar is dated the
    current ET session we drop it and use the last completed bar, keeping this
    a full-day-vs-full-day comparison. Tradeoff: after the close (today's bar
    is complete) the ratio lags by one session — acceptable for a daily
    participation signal, and far better than the partial-bar artifact.

    `asof_date` overrides "today" (ET) for deterministic testing.
    """
    if volume is None or len(volume) < 21:
        return None
    try:
        last_ts = volume.index[-1]
        last_date = last_ts.date() if hasattr(last_ts, "date") else None
        today_et = asof_date or datetime.now(ZoneInfo("America/New_York")).date()
        if last_date is not None and last_date == today_et:
            volume = volume.iloc[:-1]  # drop the in-progress partial bar
        if len(volume) < 21:
            return None
        avg20 = float(volume.iloc[-21:-1].mean())
        today = float(volume.iloc[-1])
        if avg20 <= 0:
            return None
        return round(today / avg20, 3)
    except (IndexError, ValueError, TypeError):
        return None


# US regular session boundaries (ET). yfinance intraday defaults exclude
# pre/post, but we filter defensively so a stray extended-hours bar can't
# contaminate the time-of-day profile.
_ET = ZoneInfo("America/New_York")
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)


def _volume_ratio_intraday(
    volume: pd.Series,
    *,
    asof_date: Optional[date] = None,
    min_prior_days: int = 5,
) -> Optional[float]:
    """Time-of-day-aware intraday participation ratio.

    Compares today's cumulative volume *through the latest bar's clock time*
    against the typical (median) cumulative volume through that same clock
    time across the prior sessions in the window:

        ratio = sum(today vol through T)  /  median_d( sum(day d vol through T) )

    Unlike `_volume_ratio_vs_20d` — which drops today's partial bar and
    compares the prior completed session to the prior-20d average, leaving it
    structurally BLIND to today — this is an apples-to-apples partial-day-vs-
    partial-day measure that actually observes today's session. It's the
    number that matters on a catalyst day, when volume front-loads into the
    open and a same-clock-time comparison (rather than a forward projection)
    avoids the U-curve extrapolation error.

    Expects an intraday `volume` Series with a tz-aware (UTC) DatetimeIndex
    (e.g. 15m bars over ~1mo). Uses the MEDIAN of prior sessions so a single
    prior catalyst day doesn't inflate the baseline. Comparing by clock time
    (not bar count) makes it robust to half-days and DST.

    Returns None when: no intraday bars, today has no bars (weekend / pre-open
    / stale fetch), or fewer than `min_prior_days` prior sessions exist for a
    stable baseline — callers degrade gracefully to `volume_ratio`.
    """
    if volume is None or len(volume) == 0:
        return None
    try:
        idx = volume.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        et_idx = idx.tz_convert(_ET)
        ser = pd.Series(
            pd.to_numeric(volume.values, errors="coerce"), index=et_idx
        ).dropna()
        if ser.empty:
            return None
        # Regular hours only.
        times = [ts.time() for ts in ser.index]
        ser = ser[[_SESSION_OPEN <= t <= _SESSION_CLOSE for t in times]]
        if ser.empty:
            return None
        et_dates = pd.Index([ts.date() for ts in ser.index])
        today = asof_date or datetime.now(_ET).date()
        # Object-Index equality yields a numpy bool array directly.
        today_mask = et_dates == today
        if not today_mask.any():
            return None
        # Cutoff = the latest clock time observed today; today's cumulative is
        # simply the sum of all of today's in-session bars.
        cutoff = max(ts.time() for ts in ser.index[today_mask])
        today_cum = float(ser[today_mask].sum())
        if today_cum <= 0:
            return None
        prior: list[float] = []
        for d in sorted({dd for dd in et_dates[~today_mask]}):
            day_mask = et_dates == d
            day_ser = ser[day_mask]
            cum = float(
                day_ser[[ts.time() <= cutoff for ts in day_ser.index]].sum()
            )
            if cum > 0:
                prior.append(cum)
        if len(prior) < min_prior_days:
            return None
        typical = float(pd.Series(prior).median())
        if typical <= 0:
            return None
        return round(today_cum / typical, 3)
    except (IndexError, ValueError, TypeError, AttributeError):
        return None


async def load_ticker_data(
    symbol: str,
    adapter: YFinanceAdapter,
    *,
    sector: Optional[str] = None,
    include_intraday: bool = False,
    volume_profile_base: Optional[Path] = None,
    now_et: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Fetch + assemble the per-ticker data dict consumed by Stage 1/2/3 prompts.

    Schema (verified against research-v1.0.0/stage_2_thesis.txt + stage_3_skeptic.txt):
      - price                 : current quote
      - recent_return_5d      : 5-bar pct change
      - return_30d            : 30-bar pct change
      - return_90d            : 90-bar pct change
      - volume_ratio          : prior COMPLETED session's vol / prior-20d avg.
                                Intraday-stale BY DESIGN — drops today's partial
                                bar, so it does not observe the current session.
                                Scoring (composite.py) + scoreboard calibration
                                key on this; keep its semantics stable.
      - volume_ratio_intraday : time-of-day-aware participation (today through
                                now vs typical-through-now). Two source paths,
                                None if neither available. The catalyst-day-
                                accurate companion to volume_ratio. Narrative-
                                only for now (not wired into scoring).
      - volume_ratio_intraday_source : "live" | "profile" | None — which path
                                produced the figure (transparency / dossier
                                color / debugging).
      - weekly_rsi_14         : RSI(14) on weekly resample
      - volume_5d_trend       : "rising" | "flat" | "declining"
      - single_day_share_30d  : biggest single day's |move| / |30d net move|.
                                ~1 → one gap IS the move (chop, not a trend).
      - trend_efficiency_10d  : Kaufman efficiency ratio (|net| / path). 1 →
                                clean trend; near 0 → wide oscillation / churn.
      - sector                : optional sector label (caller-supplied or None)
      - earnings_within_days  : None here (this fetch has no calendar); the CLI
                                backfills it from load_earnings_calendar.
      - daily_signals         : full generate_daily_signal_summary output
      - daily_as_of           : ISO date of the completed close the daily signals
                                describe. During regular hours the in-progress
                                partial bar is dropped, so this is the prior close.
      - session               : market session at fetch time (regular_hours /
                                pre_market / post_market / closed)
      - live_quote            : full current quote (last/bid/ask/volume/timestamp)
                                plus prior_close + gap_vs_prior_close_pct so the
                                live-vs-last-close move is a real number, not a
                                guess — today's state, distinct from the dated
                                daily block

    Two ways to populate volume_ratio_intraday:
      - `include_intraday=True` (single-ticker /research & /probe): one extra
        `fetch_bars(15m, 1mo)` measures today's real intraday shape vs prior
        sessions (source "live"). Bounded cost.
      - `volume_profile_base` set (brief universe): today's accumulated volume
        from the already-fetched quote ÷ cached time-of-day profile (source
        "profile"). Zero extra fetch — `now_et` overrides the wall clock for
        deterministic testing.
    `include_intraday` takes precedence when both are set.
    """
    bars = await adapter.fetch_bars(symbol, interval="1d", period="3mo")
    quote = await adapter.fetch_quote(symbol)

    if bars is None or len(bars) < 5:
        log.warning("Insufficient bars for %s — returning sparse ticker_data", symbol)
        return {
            "symbol": symbol,
            "price": getattr(quote, "last", None),
            "sector": sector,
            "_data_quality": "insufficient_bars",
        }

    # Market-session split. During regular hours yfinance's latest daily bar is
    # the in-progress partial session; drop it so the daily indicators are
    # honestly "as of the last completed close". Today's live state is carried
    # separately by `live_quote`, so the two reach the model as distinct dated
    # blocks rather than a stale partial masquerading as current. After the
    # close (and pre/post) the latest bar is complete, so it stands.
    session = get_current_session(now_et)
    asof = now_et or datetime.now(_ET)
    if session == Session.REGULAR_HOURS and len(bars):
        last_ts = bars.index[-1]
        last_date = last_ts.date() if hasattr(last_ts, "date") else None
        if last_date is not None and last_date == asof.date():
            bars = bars.iloc[:-1]

    close = bars["close"]
    volume = bars["volume"]
    daily_signals = generate_daily_signal_summary(symbol, bars)
    _last_bar = bars.index[-1]
    daily_as_of = _last_bar.date().isoformat() if hasattr(_last_bar, "date") else None

    # Full live quote — nothing thrown away. Carries prior_close + the computed
    # gap so the live-vs-last-close question is answerable with a real number,
    # not a guess. `close.iloc[-1]` is the last COMPLETED session's close
    # (today's partial was dropped above during regular hours), so the gap reads
    # as today's intraday move during the session / the after-hours move once
    # the session has closed.
    _ts = getattr(quote, "timestamp", None)
    _last = getattr(quote, "last", None)
    _prior_close = float(close.iloc[-1]) if len(close) else None
    _gap_pct = (
        round((_last / _prior_close - 1) * 100, 2)
        if _last and _prior_close else None
    )
    live_quote = {
        "last": _last,
        "prior_close": _prior_close,
        "gap_vs_prior_close_pct": _gap_pct,
        "bid": getattr(quote, "bid", None),
        "ask": getattr(quote, "ask", None),
        "volume": getattr(quote, "volume", None),
        "timestamp": _ts.isoformat() if hasattr(_ts, "isoformat") else _ts,
        "session": session.value,
    }

    # Time-of-day-aware intraday participation. Both paths degrade to None on
    # any failure so callers fall back to the prior-close `volume_ratio`.
    volume_ratio_intraday: Optional[float] = None
    volume_ratio_intraday_source: Optional[str] = None
    if include_intraday:
        # Live path (single-ticker DD): measure today's real intraday shape.
        try:
            intraday_bars = await adapter.fetch_bars(
                symbol, interval="15m", period="1mo"
            )
            if intraday_bars is not None and "volume" in intraday_bars:
                volume_ratio_intraday = _volume_ratio_intraday(
                    intraday_bars["volume"],
                    asof_date=(now_et or datetime.now(_ET)).date(),
                )
                if volume_ratio_intraday is not None:
                    volume_ratio_intraday_source = "live"
        except Exception as exc:
            log.warning("intraday volume fetch failed for %s: %s", symbol, exc)
    elif volume_profile_base is not None:
        # Profile path (brief universe): today's accumulated volume from the
        # already-fetched quote ÷ cached typical-through-now. No extra fetch.
        try:
            today_vol = getattr(quote, "volume", None)
            ratio = intraday_ratio_from_profile(
                load_profile(volume_profile_base, symbol),
                float(today_vol) if today_vol is not None else None,
                now_et or datetime.now(_ET),
            )
            if ratio is not None:
                volume_ratio_intraday = ratio
                volume_ratio_intraday_source = "profile"
        except Exception as exc:
            log.warning("profile intraday ratio failed for %s: %s", symbol, exc)

    return {
        "symbol": symbol,
        "price": getattr(quote, "last", None) or float(close.iloc[-1]),
        "recent_return_5d": _pct_return(close, 5),
        "return_30d": _pct_return(close, 30),
        "return_90d": _pct_return(close, 90),
        # Trend-quality guards: distinguish a real trend from one gap + chop so
        # the momentum-continuation gate isn't fooled by a lagging ADX.
        "single_day_share_30d": _single_day_share(close, 30),
        "trend_efficiency_10d": _trend_efficiency(close, 10),
        "volume_ratio": _volume_ratio_vs_20d(volume),
        "volume_ratio_intraday": volume_ratio_intraday,
        "volume_ratio_intraday_source": volume_ratio_intraday_source,
        "weekly_rsi_14": _weekly_rsi_14(close),
        "volume_5d_trend": _volume_5d_trend(volume),
        "sector": sector,
        "earnings_within_days": None,  # v1.x: wire yfinance calendar
        "daily_signals": daily_signals,
        "daily_as_of": daily_as_of,  # date of the completed close the signals describe
        "session": session.value,
        "live_quote": live_quote,
        "_data_quality": "ok",
    }


# ---------------------------------------------------------------------------
# Headlines with absorption_stage
# ---------------------------------------------------------------------------

def _classify_absorption(age_hours: float) -> str:
    """
    Map news age to absorption stage per research-v1.0.0 prompts:
      < 2h     : market hasn't priced it in vs has already priced it in — chase risk
      2-24h    : partial absorption — supporting evidence only
      1-7d     : absorbed — safe to cite
      > 7d     : context only — background
    """
    if age_hours < 2:
        return "fresh_likely_priced_in"
    if age_hours < 24:
        return "recent_partial_absorption"
    if age_hours < 24 * 7:
        return "absorbed"
    return "context_only"


async def load_headlines(
    symbol: str,
    adapter: YFinanceAdapter,
    *,
    max_items: int = 5,
    max_age_hours: int = 24 * 14,
) -> list[dict[str, Any]]:
    """Return at most `max_items` headlines with absorption_stage annotated."""
    raw = await adapter.fetch_news(symbol, max_items=max_items, max_age_hours=max_age_hours)
    headlines: list[dict[str, Any]] = []
    for item in (raw or [])[:max_items]:
        age = float(item.get("age_hours", 0.0))
        headlines.append({
            "title": item.get("title", ""),
            "publisher": item.get("publisher", ""),
            "age_hours": age,
            "absorption_stage": _classify_absorption(age),
        })
    return headlines


# ---------------------------------------------------------------------------
# World-state input (Stage 0 context)
# ---------------------------------------------------------------------------

async def _instrument_snapshot(symbol: str, adapter: YFinanceAdapter) -> dict[str, Any]:
    """One macro/sector instrument's compressed snapshot for Stage 0 context_json."""
    try:
        bars = await adapter.fetch_bars(symbol, interval="1d", period="3mo")
        if bars is None or len(bars) < 20:
            return {"symbol": symbol, "_data_quality": "insufficient_bars"}
        close = bars["close"]
        rsi = compute_rsi(bars, length=14)
        ema20 = compute_ema(close, 20)
        last_close = float(close.iloc[-1])
        last_ema20 = float(ema20.iloc[-1])
        last_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
        return {
            "symbol": symbol,
            "price": last_close,
            "rsi_14d": round(last_rsi, 2) if last_rsi is not None else None,
            "price_vs_ema20": "above" if last_close >= last_ema20 else "below",
            "return_5d": _pct_return(close, 5),
            "return_30d": _pct_return(close, 30),
        }
    except Exception as exc:
        log.warning("instrument snapshot failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "_data_quality": f"error: {exc}"}


_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


async def fetch_fear_greed() -> Optional[dict[str, Any]]:
    """CNN Fear & Greed index — current composite market sentiment (0–100).

    Returns {"score": float, "rating": str} or None on any failure. The
    endpoint is unofficial, so this degrades to None (never blocks the brief);
    the brief's per-ET-date JSON cache means it's fetched ~once per build, so no
    separate TTL is needed here. The 7 sub-components VIX/RSI/dispersion don't
    capture (breadth, put/call, safe-haven, junk spreads) are what it adds.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                _FEAR_GREED_URL,
                headers={"User-Agent": "Mozilla/5.0 (research-assistant)"},
            )
            r.raise_for_status()
            fg = r.json().get("fear_and_greed", {})
        score = fg.get("score")
        if score is None:
            return None
        rating = fg.get("rating")
        # Context, not signal: the daily level is redundant with VIX/dispersion/
        # rotation. Only the tails carry a contrarian edge, so we flag those.
        return {
            "score": round(float(score), 1),
            "rating": rating,
            "extreme": rating in ("extreme fear", "extreme greed"),
        }
    except Exception as exc:
        log.warning("fear/greed fetch failed: %s", exc)
        return None


async def build_world_state_input(
    adapter: YFinanceAdapter,
    *,
    macro_instruments: tuple[str, ...] = DEFAULT_MACRO_INSTRUMENTS,
    sector_etfs: tuple[str, ...] = DEFAULT_SECTOR_ETFS,
    watchlist_news_for: Optional[list[str]] = None,
    parallel: int = DEFAULT_PARALLEL_FETCH,
) -> dict[str, Any]:
    """
    Build the Stage 0 `context_json` payload directly (without going through
    MarketContextBuilder).

    Output shape consumed by research-v1.0.0/world_state.txt:
      - macro_instruments    : dict[symbol -> snapshot] for SPY/QQQ/^VIX
      - sector_performance   : dict[symbol -> snapshot] for sector ETFs
      - recent_news_digest   : list of headlines from macro + watchlist instruments
      - timestamp_utc        : when this was assembled
    """
    sem = asyncio.Semaphore(parallel)

    async def _with_sem(coro):
        async with sem:
            return await coro

    macro_task = [_with_sem(_instrument_snapshot(s, adapter)) for s in macro_instruments]
    sector_task = [_with_sem(_instrument_snapshot(s, adapter)) for s in sector_etfs]
    macro_snaps, sector_snaps, fear_greed = await asyncio.gather(
        asyncio.gather(*macro_task), asyncio.gather(*sector_task), fetch_fear_greed()
    )

    # Macro news from SPY/QQQ + optionally watchlist names
    news_targets = list(macro_instruments)
    if watchlist_news_for:
        news_targets.extend(watchlist_news_for)
    news_tasks = [_with_sem(load_headlines(s, adapter, max_items=3)) for s in news_targets]
    all_news_lists = await asyncio.gather(*news_tasks)

    recent_news_digest: list[dict[str, Any]] = []
    for sym, items in zip(news_targets, all_news_lists):
        for item in items:
            recent_news_digest.append({**item, "instrument": sym})

    return {
        "macro_instruments": {snap.get("symbol"): snap for snap in macro_snaps},
        "sector_performance": {snap.get("symbol"): snap for snap in sector_snaps},
        "fear_greed": fear_greed,  # CNN composite sentiment (None if fetch failed)
        "recent_news_digest": recent_news_digest,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Batch watchlist loader (for /brief)
# ---------------------------------------------------------------------------

async def _refresh_volume_profiles(
    symbols: list[str],
    adapter: YFinanceAdapter,
    *,
    base: Path,
    force: bool = False,
    cap: int = PROFILE_REFRESH_CAP,
) -> None:
    """Warm the intraday volume-profile cache for the brief path.

    Lazy: only missing/stale profiles are refreshed, and at most `cap` per run
    (one intraday fetch each) so a cold universe warms over a few runs instead
    of one latency cliff. `force=True` (operator `--refresh-profiles`) refreshes
    the whole universe, ignoring the cap — intended for an after-close cron.
    Skips are logged (no silent caps)."""
    refreshed = 0
    skipped = 0
    for sym in symbols:
        prof = load_profile(base, sym)
        if not (force or prof is None or is_stale(prof)):
            continue
        if not force and refreshed >= cap:
            skipped += 1
            continue
        if await refresh_profile(sym, adapter, base=base) is not None:
            refreshed += 1
    if refreshed or skipped:
        log.info(
            "volume profiles: refreshed %d, skipped %d stale (cap %d, force=%s)",
            refreshed, skipped, cap, force,
        )


async def load_watchlist_data(
    symbols: list[str],
    adapter: YFinanceAdapter,
    *,
    parallel: int = DEFAULT_PARALLEL_FETCH,
    volume_profile_base: Optional[Path] = None,
    refresh_profiles: bool = False,
    now_et: Optional[datetime] = None,
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """
    Batch-fetch per-ticker data + headlines for a watchlist.

    When `volume_profile_base` is set, each ticker gets a profile-based
    `volume_ratio_intraday` (today's accumulated volume ÷ cached profile, zero
    extra fetch) and the profile cache is warmed first (lazy, capped — see
    `_refresh_volume_profiles`). `refresh_profiles=True` forces a full refresh.

    Returns:
        (tickers_with_data, headlines_per_ticker)
        Both keyed by uppercase symbol; matches the shape build_brief() expects.
    """
    if volume_profile_base is not None:
        await _refresh_volume_profiles(
            symbols, adapter, base=volume_profile_base, force=refresh_profiles,
        )

    sem = asyncio.Semaphore(parallel)

    async def _one(sym: str):
        async with sem:
            td = await load_ticker_data(
                sym, adapter,
                volume_profile_base=volume_profile_base,
                now_et=now_et,
            )
            hl = await load_headlines(sym, adapter)
            return sym.upper(), td, hl

    results = await asyncio.gather(*[_one(s) for s in symbols])
    tickers: dict[str, dict] = {}
    headlines: dict[str, list[dict]] = {}
    for sym, td, hl in results:
        tickers[sym] = td
        headlines[sym] = hl
    return tickers, headlines


# ---------------------------------------------------------------------------
# CLI smoke entry (helps Tier 1 testing per "How to test")
# ---------------------------------------------------------------------------

async def _smoke_main(symbol: str) -> None:
    """Print loaded ticker_data + headlines for one symbol. Useful for quick verification."""
    import json
    adapter = YFinanceAdapter()
    td = await load_ticker_data(symbol, adapter)
    hl = await load_headlines(symbol, adapter)
    print(json.dumps({"ticker_data": td, "headlines": hl}, indent=2, default=str))


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m research_assistant.data_loader <SYMBOL>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_smoke_main(sys.argv[1]))
