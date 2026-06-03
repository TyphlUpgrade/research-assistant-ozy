"""
Tests for `research_assistant/price.py` — `BarsBackedPriceAdapter`, the
shim that wraps yfinance's `fetch_bars(symbol, interval, period)` into the
`fetch_price_at(symbol, target_date)` protocol used by forward-return
enrichment paths.

Covers:
- Returns the close at the exact target date
- Falls back to the most recent prior bar on weekends/holidays
- Returns None when the target predates available history
- Caches one DataFrame per symbol (multiple horizon lookups → one fetch)
- Coalesces concurrent fetches for the same symbol via the in-flight Event
- Releases the in-flight Event on a cancelled fetch so waiters aren't
  permanently deadlocked (HIGH severity finding from 2026-06-03 review)
- Returns None for unknown symbols / fetch exceptions
- Returns None for NaN closes (corporate-action bars, etc.)
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from research_assistant.price import BarsBackedPriceAdapter


class _StubBarsInner:
    """Fake `fetch_bars`-style adapter. `bars_by_symbol` maps a symbol to a
    list of (date, close) pairs in ascending date order. Returns a pandas
    DataFrame with the same shape the real ozymandias adapter produces."""

    def __init__(self, bars_by_symbol: dict[str, list[tuple[date, float]]]):
        self.bars_by_symbol = bars_by_symbol
        self.fetch_count = 0

    async def fetch_bars(self, symbol: str, interval: str, period: str):
        self.fetch_count += 1
        bars = self.bars_by_symbol.get(symbol)
        import pandas as pd
        if bars is None:
            return pd.DataFrame()
        idx = pd.to_datetime([b[0] for b in bars])
        return pd.DataFrame(
            {"close": [b[1] for b in bars]},
            index=idx,
        )


def test_returns_close_on_target():
    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),
            (date(2026, 5, 2), 101.0),
            (date(2026, 5, 3), 102.0),
        ],
    })
    shim = BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 2)))
    assert got == pytest.approx(101.0)


def test_falls_back_to_last_bar_before_target():
    """Weekend / holiday case: target has no bar; use most recent prior."""
    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),  # Friday
            (date(2026, 5, 4), 105.0),  # Monday (skip weekend)
        ],
    })
    shim = BarsBackedPriceAdapter(inner)
    # Sunday 2026-05-03 — no bar exists; expect the Friday close.
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 3)))
    assert got == pytest.approx(100.0)


def test_returns_none_before_history():
    inner = _StubBarsInner({
        "FOO": [(date(2026, 5, 10), 100.0)],
    })
    shim = BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 1)))
    assert got is None


def test_caches_per_symbol_sequential():
    """Multiple sequential price lookups for the same symbol → one fetch_bars call."""
    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),
            (date(2026, 5, 5), 110.0),
            (date(2026, 5, 10), 120.0),
        ],
    })
    shim = BarsBackedPriceAdapter(inner)

    async def _go():
        a = await shim.fetch_price_at("FOO", date(2026, 5, 1))
        b = await shim.fetch_price_at("FOO", date(2026, 5, 5))
        c = await shim.fetch_price_at("FOO", date(2026, 5, 10))
        return a, b, c

    a, b, c = asyncio.run(_go())
    assert (a, b, c) == (pytest.approx(100.0), pytest.approx(110.0), pytest.approx(120.0))
    assert inner.fetch_count == 1


def test_coalesces_concurrent_fetches():
    """Concurrent lookups for the same symbol while a fetch is in flight
    must wait on the in-flight Event and share the result — not each
    trigger their own fetch."""

    class _SlowInner:
        def __init__(self):
            self.fetch_count = 0
            self.gate = asyncio.Event()

        async def fetch_bars(self, symbol, interval, period):
            self.fetch_count += 1
            await self.gate.wait()
            import pandas as pd
            return pd.DataFrame(
                {"close": [42.0]},
                index=pd.to_datetime([date(2026, 5, 1)]),
            )

    inner = _SlowInner()
    shim = BarsBackedPriceAdapter(inner)

    async def _go():
        # Schedule N concurrent lookups; they should all pile up on the
        # same in-flight Event because only one task runs fetch_bars.
        tasks = [
            asyncio.create_task(shim.fetch_price_at("FOO", date(2026, 5, 1)))
            for _ in range(5)
        ]
        # Let coroutines reach the await before we release the gate.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        inner.gate.set()
        results = await asyncio.gather(*tasks)
        return results

    results = asyncio.run(_go())
    assert all(r == pytest.approx(42.0) for r in results)
    # Exactly one underlying fetch despite 5 concurrent callers.
    assert inner.fetch_count == 1


def test_event_released_on_cancelled_fetch():
    """If the first fetcher is cancelled, the in-flight Event must still
    be set so subsequent callers don't deadlock waiting on a dead Event.

    This is the HIGH-severity finding from the 2026-06-03 code review:
    `CancelledError` does not inherit from `Exception` and would have
    bypassed the original `except Exception` cleanup, leaving the Event
    permanently unset.
    """

    class _CancellableInner:
        def __init__(self):
            self.fetch_count = 0
            self.gate = asyncio.Event()
            self.second_call_releases = False

        async def fetch_bars(self, symbol, interval, period):
            self.fetch_count += 1
            if self.fetch_count == 1:
                # First call hangs forever — the test cancels it.
                await asyncio.Event().wait()
            # Subsequent calls resolve immediately.
            import pandas as pd
            return pd.DataFrame(
                {"close": [99.0]},
                index=pd.to_datetime([date(2026, 5, 1)]),
            )

    inner = _CancellableInner()
    shim = BarsBackedPriceAdapter(inner)

    async def _go():
        # Task A starts the (hanging) fetch.
        task_a = asyncio.create_task(
            shim.fetch_price_at("FOO", date(2026, 5, 1))
        )
        # Yield so task_a enters `_bars` and registers the in-flight Event.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Cancel task_a — this is the failure path the bug fix addresses.
        task_a.cancel()
        try:
            await task_a
        except asyncio.CancelledError:
            pass
        # Task B should now proceed: the Event must have been set in
        # `_bars`'s finally clause; the in-flight slot must have been
        # cleared so B is the new fetcher (with the cache still empty).
        task_b = asyncio.create_task(
            shim.fetch_price_at("FOO", date(2026, 5, 1))
        )
        # Give B reasonable time. If the bug exists this hangs forever;
        # bound the wait so a failure surfaces as a TimeoutError.
        return await asyncio.wait_for(task_b, timeout=2.0)

    got = asyncio.run(_go())
    assert got == pytest.approx(99.0)
    # First call was cancelled mid-fetch; second call completed the fetch.
    assert inner.fetch_count == 2


def test_unknown_symbol_returns_none():
    inner = _StubBarsInner({})
    shim = BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("MISSING", date(2026, 5, 1)))
    assert got is None


def test_fetch_exception_returns_none():
    class _BadInner:
        async def fetch_bars(self, symbol, interval, period):
            raise RuntimeError("network down")

    shim = BarsBackedPriceAdapter(_BadInner())
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 1)))
    assert got is None


def test_nan_close_returns_none():
    """Corporate-action bars / split-adjusted nulls sometimes surface as
    NaN; the shim should treat NaN like a missing price rather than
    propagating it (which would later coerce to nan returns and contaminate
    downstream stats)."""
    import math

    inner = _StubBarsInner({
        "FOO": [
            (date(2026, 5, 1), 100.0),
            (date(2026, 5, 2), math.nan),
        ],
    })
    shim = BarsBackedPriceAdapter(inner)
    got = asyncio.run(shim.fetch_price_at("FOO", date(2026, 5, 2)))
    assert got is None


def test_research_base_attribute_passthrough():
    """The wrapper must accept attribute assignment from callers — the
    `enrich_window` path in `journal/outcomes.py` does
    `adapter.research_base = base` to thread the data directory through.
    A `__slots__`-d class would reject this; the test pins the
    attribute-assignable contract."""
    inner = _StubBarsInner({})
    shim = BarsBackedPriceAdapter(inner)
    from pathlib import Path
    shim.research_base = Path("/tmp/whatever")
    assert getattr(shim, "research_base") == Path("/tmp/whatever")
