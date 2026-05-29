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
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
from ozymandias.intelligence.technical_analysis import (
    compute_ema,
    compute_rsi,
    generate_daily_signal_summary,
)

log = logging.getLogger(__name__)


# Default sector ETFs surveyed for world-state (subset of Ozy's full sector map)
DEFAULT_SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")

# Macro reference instruments for Stage 0 world-state
DEFAULT_MACRO_INSTRUMENTS = ("SPY", "QQQ", "^VIX")

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


async def load_ticker_data(
    symbol: str,
    adapter: YFinanceAdapter,
    *,
    sector: Optional[str] = None,
) -> dict[str, Any]:
    """
    Fetch + assemble the per-ticker data dict consumed by Stage 1/2/3 prompts.

    Schema (verified against research-v1.0.0/stage_2_thesis.txt + stage_3_skeptic.txt):
      - price                 : current quote
      - recent_return_5d      : 5-bar pct change
      - return_30d            : 30-bar pct change
      - return_90d            : 90-bar pct change
      - volume_ratio          : today's vol / 20d-avg vol
      - weekly_rsi_14         : RSI(14) on weekly resample
      - volume_5d_trend       : "rising" | "flat" | "declining"
      - sector                : optional sector label (caller-supplied or None)
      - earnings_within_days  : None for v1 (yfinance calendar wiring is v1.x)
      - daily_signals         : full generate_daily_signal_summary output
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

    close = bars["close"]
    volume = bars["volume"]
    daily_signals = generate_daily_signal_summary(symbol, bars)

    return {
        "symbol": symbol,
        "price": getattr(quote, "last", None) or float(close.iloc[-1]),
        "recent_return_5d": _pct_return(close, 5),
        "return_30d": _pct_return(close, 30),
        "return_90d": _pct_return(close, 90),
        "volume_ratio": _volume_ratio_vs_20d(volume),
        "weekly_rsi_14": _weekly_rsi_14(close),
        "volume_5d_trend": _volume_5d_trend(volume),
        "sector": sector,
        "earnings_within_days": None,  # v1.x: wire yfinance calendar
        "daily_signals": daily_signals,
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
    macro_snaps, sector_snaps = await asyncio.gather(
        asyncio.gather(*macro_task), asyncio.gather(*sector_task)
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
        "recent_news_digest": recent_news_digest,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Batch watchlist loader (for /brief)
# ---------------------------------------------------------------------------

async def load_watchlist_data(
    symbols: list[str],
    adapter: YFinanceAdapter,
    *,
    parallel: int = DEFAULT_PARALLEL_FETCH,
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """
    Batch-fetch per-ticker data + headlines for a watchlist.

    Returns:
        (tickers_with_data, headlines_per_ticker)
        Both keyed by uppercase symbol; matches the shape build_brief() expects.
    """
    sem = asyncio.Semaphore(parallel)

    async def _one(sym: str):
        async with sem:
            td = await load_ticker_data(sym, adapter)
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
