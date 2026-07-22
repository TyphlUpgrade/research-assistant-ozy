"""
Tests for data_loader.py — schema-shape + helpers (no live yfinance calls).

Live yfinance is at the integration boundary (Tier 3 test in plan §How to
test). These offline tests verify the math/classification helpers and the
expected output shapes against synthetic bars.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from research_assistant.data_loader import (
    _classify_absorption,
    _pct_return,
    _volume_5d_trend,
    _volume_ratio_intraday,
    _volume_ratio_vs_20d,
    _weekly_rsi_14,
    load_headlines,
    load_ticker_data,
    load_watchlist_data,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_classify_absorption_age_bands() -> None:
    assert _classify_absorption(0.5) == "fresh_likely_priced_in"
    assert _classify_absorption(1.9) == "fresh_likely_priced_in"
    assert _classify_absorption(2.0) == "recent_partial_absorption"
    assert _classify_absorption(23.5) == "recent_partial_absorption"
    assert _classify_absorption(24.0) == "absorbed"
    assert _classify_absorption(72.0) == "absorbed"
    assert _classify_absorption(24 * 8) == "context_only"


def test_pct_return_handles_short_series() -> None:
    s = pd.Series([100.0, 101.0])
    assert _pct_return(s, 5) is None  # not enough history


def test_pct_return_basic_math() -> None:
    s = pd.Series([100.0] * 5 + [110.0])  # 6 elements; 5-bar lookback
    assert _pct_return(s, 5) == 0.10  # (110 / 100) - 1


def test_pct_return_zero_divisor_safe() -> None:
    s = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 100.0])
    assert _pct_return(s, 5) is None


def test_volume_5d_trend_rising() -> None:
    # Two halves: low avg → high avg → rising
    v = pd.Series([100] * 5 + [200] * 5)
    assert _volume_5d_trend(v) == "rising"


def test_volume_5d_trend_declining() -> None:
    v = pd.Series([200] * 5 + [100] * 5)
    assert _volume_5d_trend(v) == "declining"


def test_volume_5d_trend_flat_default() -> None:
    v = pd.Series([100] * 10)
    assert _volume_5d_trend(v) == "flat"


def test_volume_5d_trend_short_series_returns_flat() -> None:
    v = pd.Series([100] * 3)
    assert _volume_5d_trend(v) == "flat"


def test_volume_ratio_vs_20d_basic() -> None:
    v = pd.Series([100] * 20 + [200])  # 21 elements, today=200, avg20=100
    assert _volume_ratio_vs_20d(v) == 2.0


def test_volume_ratio_short_returns_none() -> None:
    assert _volume_ratio_vs_20d(pd.Series([100] * 5)) is None


def test_volume_ratio_drops_partial_current_session_bar() -> None:
    """Intraday runs: the latest bar (dated today ET) carries partial volume.
    It must be dropped so the ratio compares full days, not a 4%-of-average
    in-progress bar. 21 full days (vol 100) + today's partial (vol 2) → the
    partial bar is dropped, leaving 100/100 = 1.0 (not 2/100 = 0.02)."""
    idx = pd.date_range(end="2026-05-29", periods=22, freq="B")
    v = pd.Series([100.0] * 21 + [2.0], index=idx)  # last bar = 2026-05-29
    # Without the fix this would be ~0.02; with the partial bar dropped it's 1.0.
    assert _volume_ratio_vs_20d(v, asof_date=date(2026, 5, 29)) == 1.0


def test_volume_ratio_keeps_complete_bar_when_last_not_today() -> None:
    """When the latest bar predates the as-of session (e.g. a weekend/after-
    hours run where today's bar hasn't formed), it is a completed bar and is
    used as-is — no trim."""
    idx = pd.date_range(end="2026-05-29", periods=21, freq="B")  # ends Fri 5/29
    v = pd.Series([100.0] * 20 + [150.0], index=idx)
    # As-of the following Monday: last bar (5/29) is complete → use it. 150/100.
    assert _volume_ratio_vs_20d(v, asof_date=date(2026, 6, 1)) == 1.5


# ---------------------------------------------------------------------------
# _volume_ratio_intraday — time-of-day-aware participation
# ---------------------------------------------------------------------------

def _intraday_bars(
    *,
    prior_days: list[date],
    prior_bar_vol: float,
    today: date,
    today_bar_vol: float,
    today_n_bars: int,
) -> pd.Series:
    """Build a 15m-ish intraday volume Series (UTC tz-aware index).

    Prior sessions each get 4 ET bars (09:30/10:30/11:30/12:30); today gets
    `today_n_bars` bars starting at 09:30 ET. June 2026 is EDT (UTC-4), so
    09:30 ET == 13:30 UTC. Volume is constant per bar so cumulative-to-T is
    exactly bar_count * bar_vol — keeps the asserted ratio arithmetic clean.
    """
    et_hours_utc = [13, 14, 15, 16]  # 09:30/10:30/11:30/12:30 ET → +4 = UTC
    stamps: list[pd.Timestamp] = []
    vols: list[float] = []
    for d in prior_days:
        for h in et_hours_utc:
            stamps.append(pd.Timestamp(f"{d.isoformat()}T{h:02d}:30:00", tz="UTC"))
            vols.append(prior_bar_vol)
    for h in et_hours_utc[:today_n_bars]:
        stamps.append(pd.Timestamp(f"{today.isoformat()}T{h:02d}:30:00", tz="UTC"))
        vols.append(today_bar_vol)
    return pd.Series(vols, index=pd.DatetimeIndex(stamps))


def test_volume_ratio_intraday_observes_today() -> None:
    """Today's participation through 11:30 ET (3 bars × 200 = 600) vs the
    prior-session median through the same clock time (3 bars × 100 = 300) →
    2.0. The partial day is COMPARED, not dropped — the whole point."""
    prior = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3),
             date(2026, 6, 4), date(2026, 6, 5)]
    v = _intraday_bars(
        prior_days=prior, prior_bar_vol=100.0,
        today=date(2026, 6, 8), today_bar_vol=200.0, today_n_bars=3,
    )
    assert _volume_ratio_intraday(v, asof_date=date(2026, 6, 8)) == 2.0


def test_volume_ratio_intraday_none_when_no_today_bars() -> None:
    """No bars dated as-of today (weekend / pre-open / stale fetch) → None,
    so the caller falls back to the prior-close ratio."""
    prior = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3),
             date(2026, 6, 4), date(2026, 6, 5)]
    v = _intraday_bars(
        prior_days=prior, prior_bar_vol=100.0,
        today=date(2026, 6, 8), today_bar_vol=200.0, today_n_bars=3,
    )
    # As-of a date with no bars in the series.
    assert _volume_ratio_intraday(v, asof_date=date(2026, 6, 9)) is None


def test_volume_ratio_intraday_none_when_too_few_prior_sessions() -> None:
    """Fewer than min_prior_days (5) prior sessions → None (unstable baseline)."""
    prior = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    v = _intraday_bars(
        prior_days=prior, prior_bar_vol=100.0,
        today=date(2026, 6, 8), today_bar_vol=200.0, today_n_bars=3,
    )
    assert _volume_ratio_intraday(v, asof_date=date(2026, 6, 8)) is None


def test_volume_ratio_intraday_empty_returns_none() -> None:
    assert _volume_ratio_intraday(pd.Series([], dtype=float)) is None


def test_weekly_rsi_14_needs_min_history() -> None:
    """< 75 daily bars → returns None."""
    s = pd.Series([100.0] * 30, index=pd.date_range("2026-01-01", periods=30))
    assert _weekly_rsi_14(s) is None


# ---------------------------------------------------------------------------
# load_ticker_data with mocked adapter
# ---------------------------------------------------------------------------

def _synthetic_bars(n: int = 90, start_price: float = 100.0) -> pd.DataFrame:
    """Generate synthetic daily bars with monotonic price rise + flat volume."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    closes = [start_price + i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "open": closes,
        "high": [c + 1 for c in closes],
        "low":  [c - 1 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


@pytest.mark.asyncio
async def test_load_ticker_data_shape_matches_prompt_contract() -> None:
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=_synthetic_bars(120))
    quote = MagicMock(last=158.5)
    adapter.fetch_quote = AsyncMock(return_value=quote)

    td = await load_ticker_data("NVDA", adapter, sector="Technology")

    # Schema contract per research-v1.0.0/stage_2_thesis.txt + stage_3_skeptic.txt
    required_fields = {
        "symbol", "price", "recent_return_5d", "return_30d", "return_90d",
        "volume_ratio", "volume_ratio_intraday", "volume_ratio_intraday_source",
        "weekly_rsi_14", "volume_5d_trend", "sector", "earnings_within_days",
        "daily_signals", "daily_as_of", "session", "live_quote", "_data_quality",
    }
    assert required_fields.issubset(td.keys()), (
        f"Missing fields: {required_fields - td.keys()}"
    )
    assert td["symbol"] == "NVDA"
    assert td["sector"] == "Technology"
    assert td["_data_quality"] == "ok"
    assert td["recent_return_5d"] is not None  # synthetic data has 5+ bars
    assert td["volume_5d_trend"] in ("rising", "flat", "declining")


@pytest.mark.asyncio
async def test_load_ticker_data_session_split_open_vs_closed() -> None:
    """Open: today's in-progress daily bar is dropped, so daily_as_of is the
    prior completed close and a live_quote block carries today. Closed: today's
    bar is complete and kept, so daily_as_of == today. Same bars both times."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    # Index ends on a known non-holiday weekday so session detection is stable.
    idx = pd.bdate_range(end="2026-06-23", periods=120)  # ... 06-22 (Mon), 06-23 (Tue)
    closes = [100.0 + i * 0.5 for i in range(120)]
    bars = pd.DataFrame({
        "open": closes, "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes], "close": closes,
        "volume": [1_000_000] * 120,
    }, index=idx)
    last_day, prev_day = idx[-1].date(), idx[-2].date()

    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=bars)
    adapter.fetch_quote = AsyncMock(return_value=MagicMock(
        last=158.5, bid=158.4, ask=158.6, volume=400.0, timestamp=None))

    td_open = await load_ticker_data(
        "NVDA", adapter,
        now_et=datetime(2026, 6, 23, 10, 0, tzinfo=et),  # mid-session
    )
    assert td_open["session"] == "regular_hours"
    assert td_open["daily_as_of"] == prev_day.isoformat()  # partial bar dropped
    assert td_open["live_quote"]["last"] == 158.5
    assert td_open["live_quote"]["bid"] == 158.4
    # prior_close = last COMPLETED close (06-22 = 159.0 after dropping 06-23);
    # gap = 158.5/159.0 - 1 = -0.31%.
    assert td_open["live_quote"]["prior_close"] == 159.0
    assert td_open["live_quote"]["gap_vs_prior_close_pct"] == -0.31

    td_closed = await load_ticker_data(
        "NVDA", adapter,
        now_et=datetime(2026, 6, 23, 20, 30, tzinfo=et),  # after close
    )
    assert td_closed["session"] == "closed"
    assert td_closed["daily_as_of"] == last_day.isoformat()  # today's bar kept
    # closed: today's complete bar kept → prior_close = 06-23 close = 159.5.
    assert td_closed["live_quote"]["prior_close"] == 159.5


@pytest.mark.asyncio
async def test_load_ticker_data_profile_path(tmp_path) -> None:
    """Brief path: volume_ratio_intraday comes from the cached profile +
    quote.volume with NO extra fetch (only the daily bars call)."""
    from zoneinfo import ZoneInfo

    from research_assistant.volume_profile import VolumeProfile, write_profile

    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=_synthetic_bars(120))
    adapter.fetch_quote = AsyncMock(return_value=MagicMock(last=158.5, volume=400.0))

    write_profile(tmp_path, VolumeProfile(
        ticker="NVDA", fetched_at=1_750_000_000.0, interval="15m",
        n_sessions=20, bucket_minutes=15,
        typical_cumulative={"09:45": 100.0, "10:00": 200.0},
        typical_full_day=2600.0,
    ))

    td = await load_ticker_data(
        "NVDA", adapter, volume_profile_base=tmp_path,
        now_et=datetime(2026, 6, 16, 10, 5, tzinfo=ZoneInfo("America/New_York")),
    )
    # 400 today vs typical-through-10:00 (200) → 2.0.
    assert td["volume_ratio_intraday"] == 2.0
    assert td["volume_ratio_intraday_source"] == "profile"
    adapter.fetch_bars.assert_called_once()  # daily only — no intraday fetch


@pytest.mark.asyncio
async def test_load_ticker_data_live_path_sets_source(tmp_path) -> None:
    """/research path: include_intraday measures live and tags source 'live'."""
    from zoneinfo import ZoneInfo

    stamps: list[pd.Timestamp] = []
    vols: list[float] = []
    for d in ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]:
        for h in (13, 14, 15):  # 09:30/10:30/11:30 ET (EDT = UTC-4)
            stamps.append(pd.Timestamp(f"{d}T{h:02d}:30:00", tz="UTC"))
            vols.append(100.0)
    for h in (13, 14, 15):
        stamps.append(pd.Timestamp(f"2026-06-15T{h:02d}:30:00", tz="UTC"))
        vols.append(200.0)
    intraday = pd.DataFrame({"volume": vols}, index=pd.DatetimeIndex(stamps))

    def _bars(symbol, interval, period):
        return intraday if interval == "15m" else _synthetic_bars(120)

    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(side_effect=_bars)
    adapter.fetch_quote = AsyncMock(return_value=MagicMock(last=10.0))

    td = await load_ticker_data(
        "X", adapter, include_intraday=True,
        now_et=datetime(2026, 6, 15, 11, 30, tzinfo=ZoneInfo("America/New_York")),
    )
    # today 3 bars × 200 = 600 through 11:30 vs prior median 300 → 2.0.
    assert td["volume_ratio_intraday"] == 2.0
    assert td["volume_ratio_intraday_source"] == "live"


@pytest.mark.asyncio
async def test_load_ticker_data_insufficient_bars_returns_sparse() -> None:
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=_synthetic_bars(3))  # too few
    adapter.fetch_quote = AsyncMock(return_value=MagicMock(last=100.0))

    td = await load_ticker_data("X", adapter)
    assert td["_data_quality"] == "insufficient_bars"
    assert td["symbol"] == "X"
    # Sparse dict must still have symbol + price for downstream graceful handling
    assert td["price"] == 100.0


@pytest.mark.asyncio
async def test_load_ticker_data_falls_back_to_close_when_quote_none() -> None:
    """If yfinance quote fails (e.g. ext-hours), use last close from bars."""
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=_synthetic_bars(60))
    adapter.fetch_quote = AsyncMock(return_value=MagicMock(last=None))

    td = await load_ticker_data("Y", adapter)
    assert td["_data_quality"] == "ok"
    # Synthetic bars start at 100.0 + 0.5 * 59 = 129.5 at index 59
    assert td["price"] == pytest.approx(129.5)


# ---------------------------------------------------------------------------
# load_headlines with mocked adapter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_headlines_annotates_absorption_stage() -> None:
    adapter = MagicMock()
    adapter.fetch_news = AsyncMock(return_value=[
        {"title": "fresh news",   "publisher": "Reuters",  "age_hours": 0.5},
        {"title": "recent news",  "publisher": "Bloomberg", "age_hours": 12.0},
        {"title": "absorbed news", "publisher": "WSJ",       "age_hours": 72.0},
        {"title": "old context",   "publisher": "FT",        "age_hours": 24 * 10},
    ])

    headlines = await load_headlines("AAPL", adapter, max_items=5)
    stages = [h["absorption_stage"] for h in headlines]
    assert stages == [
        "fresh_likely_priced_in",
        "recent_partial_absorption",
        "absorbed",
        "context_only",
    ]


@pytest.mark.asyncio
async def test_load_headlines_handles_empty_result() -> None:
    adapter = MagicMock()
    adapter.fetch_news = AsyncMock(return_value=[])
    headlines = await load_headlines("ZZZ", adapter)
    assert headlines == []


@pytest.mark.asyncio
async def test_load_headlines_handles_none_result() -> None:
    """Some yfinance responses are None for symbols with no news coverage."""
    adapter = MagicMock()
    adapter.fetch_news = AsyncMock(return_value=None)
    headlines = await load_headlines("OBSCURE", adapter)
    assert headlines == []


# ---------------------------------------------------------------------------
# Fear & Greed index
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_world_state_includes_fear_greed(monkeypatch) -> None:
    """The fetched F&G score flows into the world-state input context."""
    import research_assistant.data_loader as dl

    monkeypatch.setattr(
        dl, "fetch_fear_greed",
        AsyncMock(return_value={"score": 27.1, "rating": "fear"}),
    )
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=_synthetic_bars(60))
    adapter.fetch_news = AsyncMock(return_value=[])

    ws_input = await dl.build_world_state_input(
        adapter, macro_instruments=("SPY",), sector_etfs=("XLK",),
    )
    assert ws_input["fear_greed"] == {"score": 27.1, "rating": "fear"}


@pytest.mark.asyncio
async def test_fetch_fear_greed_parses_and_flags_extreme(monkeypatch) -> None:
    """Parses score/rating and flags the contrarian tails (extreme fear/greed)."""
    import httpx

    import research_assistant.data_loader as dl

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"fear_and_greed": {"score": 18.4, "rating": "extreme fear"}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    fg = await dl.fetch_fear_greed()
    assert fg == {"score": 18.4, "rating": "extreme fear", "extreme": True}


@pytest.mark.asyncio
async def test_fetch_fear_greed_degrades_to_none(monkeypatch) -> None:
    """Any network/parse failure → None (never blocks the brief)."""
    import httpx

    import research_assistant.data_loader as dl

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    assert await dl.fetch_fear_greed() is None


@pytest.mark.asyncio
async def test_load_watchlist_data_skips_failed_symbol() -> None:
    """One delisted/bad symbol must not abort the whole brief batch — the
    per-symbol load is guarded and the failure is dropped, not propagated."""
    adapter = MagicMock()

    async def _bars(symbol, *a, **k):
        if symbol == "BAD":
            raise RuntimeError("possibly delisted; no price data found")
        return _synthetic_bars(120)

    adapter.fetch_bars = AsyncMock(side_effect=_bars)
    adapter.fetch_quote = AsyncMock(return_value=MagicMock(last=100.0))
    adapter.fetch_news = AsyncMock(return_value=[])

    tickers, headlines = await load_watchlist_data(["GOOD", "BAD"], adapter)

    assert "GOOD" in tickers
    assert "BAD" not in tickers          # skipped, not fatal
    assert "GOOD" in headlines
