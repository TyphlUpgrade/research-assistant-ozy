"""
Price-adapter shim — adapts yfinance-style `fetch_bars` adapters to the
`fetch_price_at(symbol, target_date)` protocol used by forward-return
enrichment paths.

Why this module exists
----------------------

Two journals enrich rows with forward returns from yfinance:

- `journal/outcomes.py::enrich_alert_with_returns` (alerts journal)
- `scoreboard.py::enrich_stage2_rows` (Stage 2 journal)

Both call `adapter.fetch_price_at(symbol, target_date)`. The real
`ozymandias.data.adapters.yfinance_adapter.YFinanceAdapter` does NOT
expose that method — it exposes `fetch_bars(symbol, interval, period)`
returning a pandas DataFrame. Without this shim the alerts enrichment
silently fails in production (every `return_*` field in
`.research/alerts/*.jsonl` is null).

This module is the single place that translates between the two
contracts, so the bug doesn't get fixed twice with two diverged shims.
Both callers (`cli.py::_cmd_scoreboard`, `cli.py::_cmd_alerts`) wrap the
raw adapter with `BarsBackedPriceAdapter(YFinanceAdapter())` before
handing it to the enrichment path.

Cache behavior
--------------

One DataFrame per symbol over the lifetime of the wrapper. Concurrent
fetches for the same symbol coalesce via a per-symbol `asyncio.Event`
so two horizon lookups (or two journal rows) don't double-fetch the
same ticker. The Event bookkeeping is wrapped in `try/finally` so a
`CancelledError` thrown by the inner adapter does NOT leave waiters
deadlocked on a never-set Event.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)


class BarsBackedPriceAdapter:
    """Wrap a `fetch_bars(symbol, interval, period)` adapter to expose the
    `fetch_price_at(symbol, target_date)` protocol used by the forward-
    return enrichment paths.

    Caching is per-symbol; multi-horizon lookups for one ticker fan into
    a single network fetch. Concurrent fetches for the same symbol
    coalesce via an in-flight `asyncio.Event`.

    The wrapper is intentionally `__slots__`-free so callers may attach
    arbitrary attributes (e.g. `adapter.research_base = base`, which
    `journal.outcomes.enrich_window` reads).
    """

    # 6 months of 1d bars covers every horizon (max 30d) for any asof
    # date within ~5 months of today. Stage 2 journal in practice starts
    # late May 2026, so 6mo is plenty for v1. Widen via subclassing or
    # constructor injection if asof dates older than ~5 months appear
    # (would surface as silently-null `entry_price` rows that DO NOT
    # cache, so the symptom is "row keeps re-trying every run" — visible
    # in stage2_returns/<TICKER>.jsonl having no entry for that
    # recorded_at).
    _BARS_INTERVAL = "1d"
    _BARS_PERIOD = "6mo"

    def __init__(self, inner) -> None:
        self.inner = inner
        # symbol → DataFrame|None ; None signals "fetch attempted and failed"
        self._bars_cache: dict[str, object] = {}
        # symbol → Event held while a fetch is in flight
        self._inflight: dict[str, asyncio.Event] = {}

    async def _bars(self, symbol: str):
        """Fetch (or read cached) bars for `symbol`. Concurrent callers
        for the same symbol coalesce on the in-flight Event."""
        if symbol in self._bars_cache:
            return self._bars_cache[symbol]
        # Another task is already fetching this symbol — wait for it.
        evt = self._inflight.get(symbol)
        if evt is not None:
            await evt.wait()
            return self._bars_cache.get(symbol)
        # We're the first; claim the in-flight slot and fetch.
        evt = asyncio.Event()
        self._inflight[symbol] = evt
        try:
            try:
                df = await self.inner.fetch_bars(
                    symbol, self._BARS_INTERVAL, self._BARS_PERIOD,
                )
            except Exception as exc:
                log.warning(
                    "BarsBackedPriceAdapter: fetch_bars failed symbol=%s err=%s",
                    symbol, exc,
                )
                df = None
            self._bars_cache[symbol] = df
            return df
        finally:
            # Always release waiters and clear the slot — even on
            # CancelledError. If we don't, a single cancelled fetch
            # deadlocks every subsequent caller for this symbol.
            evt.set()
            self._inflight.pop(symbol, None)

    async def fetch_price_at(
        self, symbol: str, target_date: date
    ) -> Optional[float]:
        """Return the close on `target_date` if a bar exists for that
        date, otherwise the close on the most recent trading day before
        it. None when the symbol has no bars in the window OR when the
        target predates the available history.
        """
        df = await self._bars(symbol)
        if df is None:
            return None
        try:
            empty = df.empty
        except AttributeError:
            return None
        if empty:
            return None
        # yfinance DataFrames typically have a UTC-tz-aware DatetimeIndex
        # after the ozymandias adapter's `tz_convert("UTC")`. For daily
        # bars timestamped at exchange-open (09:30 ET / 13:30 UTC) the
        # UTC `.date()` returns the same trading day. We compare bar
        # date to `target_date` (a naive `datetime.date`); if intraday
        # bars or extended-hours bars ever land here, the comparison
        # may be off by one — this is a known limitation for now and is
        # only an issue if `interval` becomes something other than "1d".
        try:
            idx_dates = df.index.date
        except AttributeError:
            return None
        try:
            close_col = df["close"]
        except (KeyError, TypeError):
            return None
        # Bars are date-ordered ascending in yfinance output; take the
        # last one whose date is on or before target.
        last_close: Optional[float] = None
        for bar_date, close in zip(idx_dates, close_col):
            if bar_date <= target_date:
                last_close = close
            else:
                break
        if last_close is None:
            return None
        try:
            value = float(last_close)
        except (TypeError, ValueError):
            return None
        # numpy NaN converts to Python float NaN cleanly — guard against
        # silently propagating a NaN close (corporate action bar, etc.).
        if value != value:  # NaN check
            return None
        return value
