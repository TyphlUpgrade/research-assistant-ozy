"""
Research-mode market context wrapper.

Thin shim around Ozy's `MarketContextBuilder` that:
1. Injects `NullStateManager` so the builder can construct without Ozy's
   execution-state machinery.
2. Calls `builder.build(...)` to assemble world-state + technicals.
3. Drops execution-specific output keys (`pdt_trades_remaining`,
   `active_strategies`) before returning.

The returned dict matches spec §169 minus the dropped keys:
  - spy_trend, spy_rsi, qqq_trend, market_breadth
  - sector_performance, macro_news
  - watchlist_news (empty in research-mode v1 since NullStateManager returns
    an empty watchlist; opportunity surface comes from `.research/watchlist.txt`
    handled separately by `/brief`)
  - sector_dispersion, trading_session
"""
from __future__ import annotations

from typing import Any, Optional

from ozymandias.core.market_context import MarketContextBuilder

from research_assistant.null_state_manager import NullStateManager


_EXECUTION_KEYS_TO_DROP = frozenset({
    "pdt_trades_remaining",
    "active_strategies",
})


async def build_research_context(
    market_indicators: dict[str, Any],
    daily_indicators: dict[str, Any],
    data_adapter: Any,
    recommendation_outcomes: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Assemble world-state + technicals for research use.

    Args:
        market_indicators: pre-computed indicators for SPY/QQQ etc.
        daily_indicators: per-ticker daily TA snapshots
        data_adapter: a YFinanceAdapter instance (or compatible)
        recommendation_outcomes: optional prior-outcomes dict for context

    Returns:
        Dict matching spec §169 with execution-specific keys removed.
    """
    state_manager = NullStateManager()
    builder = MarketContextBuilder()

    raw = await builder.build(
        acct=None,
        pdt_remaining=0,
        market_context_indicators=market_indicators,
        daily_indicators=daily_indicators,
        recommendation_outcomes=recommendation_outcomes or {},
        state_manager=state_manager,
        data_adapter=data_adapter,
    )

    # Strip execution-specific keys (research is read-only, no trading state)
    return {k: v for k, v in raw.items() if k not in _EXECUTION_KEYS_TO_DROP}
