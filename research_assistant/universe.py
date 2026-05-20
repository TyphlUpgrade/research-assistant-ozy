"""
Dynamic universe discovery for `/brief`.

Pinned tickers from `.research/watchlist.txt` always lead the result. The
remainder is filled from Ozy's `UniverseFetcher` (Yahoo screeners + S&P 500 /
Nasdaq 100 index constituents). Russell 2000 is intentionally off — gated by
`CascadeConfig.universe_expanded_sources` which we don't pass.

Ozy's fetcher swallows network failures and returns `[]`, so a flaky Yahoo
screener degrades to pins-only without exceptions.

A module-level `UniverseFetcher` singleton honors Ozy's 24h Source B
Wikipedia cache — instantiating fresh each call would defeat it. Tests
bypass the singleton by passing `fetcher=` explicitly.
"""
from __future__ import annotations

import logging
from typing import Optional

from ozymandias.intelligence.universe_fetcher import UniverseFetcher

log = logging.getLogger(__name__)

DEFAULT_CAP = 30

_FETCHER: Optional[UniverseFetcher] = None


def _get_fetcher() -> UniverseFetcher:
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = UniverseFetcher(no_entry_symbols=None, cascade_config=None)
    return _FETCHER


def _reset_fetcher_cache() -> None:
    """Test hook: drop the cached fetcher so the next call re-constructs."""
    global _FETCHER
    _FETCHER = None


async def discover_universe(
    pins: list[str],
    cap: int = DEFAULT_CAP,
    fetcher: Optional[UniverseFetcher] = None,
) -> list[str]:
    """
    Return up to `cap` tickers: pins first (in order), then dynamic discovery
    deduped against pins, truncated to `cap`.

    Pass `fetcher` to inject a custom (typically mocked) UniverseFetcher;
    when None, the module singleton is used so Ozy's 24h Source B cache
    stays warm across briefs in the same process.
    """
    if cap <= 0:
        return []

    pins_upper = [p.upper() for p in pins]
    seen: set[str] = set(pins_upper)
    result: list[str] = list(pins_upper)

    if len(result) >= cap:
        return result[:cap]

    fetcher = fetcher if fetcher is not None else _get_fetcher()
    discovered = await fetcher.get_universe()

    for sym in discovered:
        if len(result) >= cap:
            break
        sym_upper = sym.upper()
        if sym_upper in seen:
            continue
        seen.add(sym_upper)
        result.append(sym_upper)

    log.info(
        "Universe: %d pins + %d discovered = %d total (cap=%d)",
        len(pins_upper), len(result) - len(pins_upper), len(result), cap,
    )
    return result
