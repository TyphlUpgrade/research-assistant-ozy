"""
Dynamic universe discovery for `/brief`.

Pinned tickers from `.research/watchlist.txt` always lead the result. The
remainder is filled from Ozy's `UniverseFetcher` (Yahoo screeners + S&P 500 /
Nasdaq 100 index constituents). Russell 2000 is intentionally off — gated by
`CascadeConfig.universe_expanded_sources` which we don't pass.

Ozy's fetcher swallows network failures and returns `[]`, so a flaky Yahoo
screener degrades to pins-only without exceptions.
"""
from __future__ import annotations

import logging

from ozymandias.intelligence.universe_fetcher import UniverseFetcher

log = logging.getLogger(__name__)

DEFAULT_CAP = 30


async def discover_universe(pins: list[str], cap: int = DEFAULT_CAP) -> list[str]:
    """
    Return up to `cap` tickers: pins first (in order), then dynamic discovery
    deduped against pins, truncated to `cap`.
    """
    if cap <= 0:
        return []

    pins_upper = [p.upper() for p in pins]
    seen: set[str] = set(pins_upper)
    result: list[str] = list(pins_upper)

    if len(result) >= cap:
        return result[:cap]

    fetcher = UniverseFetcher(no_entry_symbols=None, cascade_config=None)
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
