"""
Test the NullStateManager contract.

The critical assertion (Critic iter2 BLOCKER 1): the stub returns a real
WatchlistState constructed with the actual field names (entries=, last_updated=)
that match Ozy's state_manager.py:128-131 — not the iter2-draft's wrong
(symbols=, updated_at=) which would TypeError at first call.
"""
from __future__ import annotations

import asyncio

from ozymandias.core.state_manager import WatchlistState

from research_assistant.null_state_manager import NullStateManager


def test_load_watchlist_returns_real_watchlist_state() -> None:
    """
    The stub's load_watchlist must return a WatchlistState instance,
    not raise TypeError on construction. This guards the Critic-flagged
    schema mismatch that would have shipped if we'd kept iter2's draft.
    """
    nsm = NullStateManager()
    result = asyncio.run(nsm.load_watchlist())
    assert isinstance(result, WatchlistState)
    # Field-shape contract per state_manager.py:128-131
    assert hasattr(result, "entries")
    assert hasattr(result, "last_updated")
    assert isinstance(result.entries, list)
    assert result.entries == []
    assert isinstance(result.last_updated, str)
    assert result.last_updated == ""


def test_no_other_state_manager_methods_used() -> None:
    """
    Sanity: NullStateManager exposes ONLY load_watchlist. If a future
    MarketContextBuilder refactor adds another state_manager.* call,
    that call will AttributeError here — surfacing the drift cleanly.
    """
    nsm = NullStateManager()
    public_methods = [
        name for name in dir(nsm)
        if not name.startswith("_") and callable(getattr(nsm, name))
    ]
    assert public_methods == ["load_watchlist"], (
        f"NullStateManager has unexpected methods: {public_methods}. "
        f"Spec says only load_watchlist is needed; extra methods suggest "
        f"either scope creep or a missing import-boundary test."
    )
