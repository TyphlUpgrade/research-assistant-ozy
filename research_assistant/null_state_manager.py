"""
NullStateManager — minimal stub satisfying the subset of `StateManager` that
Ozy's `MarketContextBuilder` actually invokes.

Verified surface (2026-05-14, ozymandias/core/market_context.py):
- Line 59:  `state_manager` parameter on `MarketContextBuilder.__init__`
- Line 112: `await state_manager.load_watchlist()` — the only `state_manager.*` call

Verified `WatchlistState` schema (ozymandias/core/state_manager.py:128-131):
- `entries: list[WatchlistEntry] = field(default_factory=list)`
- `last_updated: str = ""`

If `MarketContextBuilder` ever grows new `state_manager.*` calls in a future
Ozy refactor, the import-boundary test (`tests/test_import_boundaries.py`)
catches the new attribute reference via the transitive AST walk and CI fails.

This is the SOLE `state_manager` import allowed in the research repo (named
exception in the import-boundary test's `ALLOWED_NAMED_SYMBOL` set).
"""
from __future__ import annotations

from ozymandias.core.state_manager import WatchlistState


class NullStateManager:
    """Read-only stub: research repo has no execution-side state to load."""

    async def load_watchlist(self) -> WatchlistState:
        return WatchlistState(entries=[], last_updated="")
