"""
Tests for the outcome tracker (PR 1.1).

Covers:
- Basic return math (entry → +7d/+30d/+90d prices)
- None prices from yfinance are excluded (None, not 0.0 sentinel)
- `enrich_window` respects the `_ENRICH_CONCURRENCY` semaphore (≤5 in-flight)
- `enrich_window` summary correctly counts rate-limit vs other failures
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from research_assistant.journal.outcomes import (
    _ENRICH_CONCURRENCY,
    enrich_alert_with_returns,
    enrich_window,
)


def _today_iso() -> str:
    return date.today().isoformat()


def _past_asof(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _alert(
    ticker: str = "NVDA",
    asof: str | None = None,
    entry_price: float = 100.0,
    screener: str = "sector_rotation",
) -> dict:
    return {
        "schema_version": 1,
        "ticker": ticker,
        "screener": screener,
        "asof": asof if asof is not None else _past_asof(120),
        "entry_price": entry_price,
        "evidence": {},
        "created_at": "2026-05-23T00:00:00+00:00",
        "return_7d": None,
        "return_30d": None,
        "return_90d": None,
        "enriched_at": None,
    }


class _PriceMapAdapter:
    """Maps (ticker, offset_days_from_asof) -> price. Async fetch_price_at."""

    def __init__(self, asof: date, prices: dict[tuple[str, int], float | None], research_base: Path):
        self._asof = asof
        self._prices = prices
        self.research_base = research_base

    async def fetch_price_at(self, ticker: str, target: date):
        offset = (target - self._asof).days
        return self._prices.get((ticker, offset))


@pytest.mark.asyncio
async def test_enrich_alert_basic_math(tmp_path: Path) -> None:
    asof = _past_asof(120)
    asof_date = date.fromisoformat(asof)
    adapter = _PriceMapAdapter(
        asof=asof_date,
        prices={("NVDA", 7): 110.0, ("NVDA", 30): 120.0, ("NVDA", 90): 130.0},
        research_base=tmp_path,
    )
    alert = _alert(asof=asof, entry_price=100.0)
    enriched = await enrich_alert_with_returns(alert, adapter)
    assert enriched["return_7d"] == pytest.approx(0.10)
    assert enriched["return_30d"] == pytest.approx(0.20)
    assert enriched["return_90d"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_none_price_excluded_from_stats(tmp_path: Path) -> None:
    asof = _past_asof(120)
    asof_date = date.fromisoformat(asof)
    adapter = _PriceMapAdapter(
        asof=asof_date,
        prices={("NVDA", 7): 110.0, ("NVDA", 30): None, ("NVDA", 90): 130.0},
        research_base=tmp_path,
    )
    alert = _alert(asof=asof, entry_price=100.0)
    enriched = await enrich_alert_with_returns(alert, adapter)
    assert enriched["return_7d"] == pytest.approx(0.10)
    assert enriched["return_30d"] is None  # NOT 0.0 / sentinel
    assert enriched["return_90d"] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_enrich_window_respects_concurrency_cap(tmp_path: Path) -> None:
    asof = _past_asof(120)
    asof_date = date.fromisoformat(asof)
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    class _CountingAdapter:
        def __init__(self):
            self.research_base = tmp_path

        async def fetch_price_at(self, ticker: str, target: date):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                if in_flight > peak:
                    peak = in_flight
            try:
                # Yield to scheduler so concurrent tasks can interleave.
                await asyncio.sleep(0.01)
                return 105.0
            finally:
                async with lock:
                    in_flight -= 1

    alerts = [_alert(ticker=f"T{i:02d}", asof=asof) for i in range(50)]
    summary = await enrich_window(alerts, _CountingAdapter())
    assert peak <= _ENRICH_CONCURRENCY, f"peak={peak} exceeded cap={_ENRICH_CONCURRENCY}"
    assert summary["enriched"] == 50


@pytest.mark.asyncio
async def test_enrich_window_reports_rate_limit_failures(tmp_path: Path) -> None:
    asof = _past_asof(120)

    class _FlakyAdapter:
        def __init__(self):
            self.research_base = tmp_path
            self._calls: dict[str, int] = {}

        async def fetch_price_at(self, ticker: str, target: date):
            # Ticker numbers 0..9 always raise rate-limit; rest succeed.
            n = int(ticker[1:])
            if n < 10:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return 110.0

    alerts = [_alert(ticker=f"T{i:02d}", asof=asof) for i in range(50)]
    summary = await enrich_window(alerts, _FlakyAdapter())
    assert summary["failed_rate_limit"] == 10
    assert summary["failed_other"] == 0
    assert summary["enriched"] == 40
