"""
Tests for data_loader.py — schema-shape + helpers (no live yfinance calls).

Live yfinance is at the integration boundary (Tier 3 test in plan §How to
test). These offline tests verify the math/classification helpers and the
expected output shapes against synthetic bars.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from research_assistant.data_loader import (
    _classify_absorption,
    _pct_return,
    _volume_5d_trend,
    _volume_ratio_vs_20d,
    _weekly_rsi_14,
    load_headlines,
    load_ticker_data,
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
        "volume_ratio", "weekly_rsi_14", "volume_5d_trend",
        "sector", "earnings_within_days", "daily_signals", "_data_quality",
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
