"""
Setup-finder data contracts.

`SetupCandidate` is the uniform output of every screener — a frozen dataclass
journaled to `.research/alerts/<date>.jsonl` and rendered into the `/brief`
opportunity surface. Per-screener fields live in `evidence` so adding new
screeners doesn't churn the dataclass (plan §3.2, Option D1).

`Screener` is the pure-function protocol every screener implements. No I/O
inside `evaluate(...)` — loaders run upstream and pass already-fetched data in.

`render_setup_line(candidate)` dispatches to a per-screener formatter
registered via `register_formatter(name, fn)`. PR 1.1 ships only the registry;
each screener module (PR 1.2, 2.2, 2.3) registers its own formatter at import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol


@dataclass(frozen=True)
class SetupCandidate:
    """One screener hit, post-evaluation. Journaled by `append_alert`."""
    ticker: str
    screener: str                            # "sector_rotation" | "pead" | "pre_catalyst"
    asof: str                                # ISO date (ET trading day)
    entry_price: float
    evidence: dict[str, Any]
    return_7d: Optional[float] = None
    return_30d: Optional[float] = None
    return_90d: Optional[float] = None
    enriched_at: Optional[str] = None


class Screener(Protocol):
    """Pure-function screener. No I/O — loaders run upstream."""

    name: str

    def evaluate(
        self,
        ticker_data: dict,
        world_state: dict,
        earnings_event: Optional[dict] = None,
        sector_rs: Optional[dict] = None,
    ) -> Optional[SetupCandidate]:
        ...


# ---------------------------------------------------------------------------
# Per-screener formatter registry
# ---------------------------------------------------------------------------

_FORMATTERS: dict[str, Callable[[SetupCandidate], str]] = {}


def register_formatter(name: str, fn: Callable[[SetupCandidate], str]) -> None:
    """Register a per-screener line formatter. Idempotent re-registration is
    allowed (test setup may import a module twice)."""
    _FORMATTERS[name] = fn


def render_setup_line(candidate: SetupCandidate) -> str:
    """Render one `## Setups` line for a candidate. Falls back to a minimal
    `ticker (screener)` line if no formatter is registered — keeps the brief
    renderer from crashing on screeners introduced after a hot-reload."""
    fn = _FORMATTERS.get(candidate.screener)
    if fn is None:
        return f"- **{candidate.ticker}** ({candidate.screener}) @ {candidate.entry_price}"
    return fn(candidate)
