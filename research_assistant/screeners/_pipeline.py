"""
Screener pipeline orchestrator.

`run_screeners_and_journal(...)` is the single entry point called from BOTH
`_cmd_brief` branches (cache-hit and cache-miss) — this is the iter-1
CRITICAL fix for the cache-bypass defect (plan §F).

Two layers:
  - `evaluate_all(...)`  — pure dispatcher; iterates registered screeners,
    calls each `evaluate(...)`, returns the flat list of `SetupCandidate`
    (filtering None). Per-screener exceptions logged as `screener_health`
    WARN; that screener emits zero candidates while others continue.
  - `run_screeners_and_journal(...)` — wraps `evaluate_all`, journals each
    candidate via `append_alert`, emits observability breadcrumbs at
    pipeline start + end.

Subsequent PRs (1.2, 2.2, 2.3) wire individual screeners in via
`register_screener(name, evaluate_fn)`. PR 1.1 ships an empty registry;
`evaluate_all` returns `[]`.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from research_assistant.screeners._types import SetupCandidate

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sector-performance derivation helper
# ---------------------------------------------------------------------------

# The 11 XL-series sector ETFs that the sector_rotation screener ranks.
# Kept here (not imported from sector_rotation) to avoid an import cycle —
# this helper is called by `_cmd_brief` BEFORE the screener registry is
# consulted, and sector_rotation already imports from this module.
_SECTOR_ETF_SYMBOLS = (
    "XLK", "XLF", "XLE", "XLY", "XLV",
    "XLI", "XLB", "XLU", "XLP", "XLC", "XLRE",
)


def compute_sector_performance(ticker_data: dict) -> dict:
    """Build `{ETF: {return_5d, return_30d, price}}` from `ticker_data`.

    The sector_rotation screener (PR 1.2) reads
    `world_state["sector_performance"]`, but the brief's `world_state` today
    only carries LLM-derived bias/strength tags (no raw returns). This helper
    bridges the gap: it pulls per-ETF returns out of `ticker_data` (where
    `data_loader._instrument_snapshot` already populated them) so screeners
    can see the raw numbers they need.

    Field-name mapping:
      ticker_data[etf]["return_5d"]   → snapshot["return_5d"]
      ticker_data[etf]["return_30d"]  → snapshot["return_30d"]
      ticker_data[etf]["price"]       → snapshot["price"]  (used as entry_price)

    Skips ETFs missing from `ticker_data` or lacking both return fields;
    returns whatever's available. Empty dict if nothing usable.
    """
    out: dict[str, dict] = {}
    if not isinstance(ticker_data, dict):
        return out
    for etf in _SECTOR_ETF_SYMBOLS:
        td = ticker_data.get(etf) or ticker_data.get(etf.upper())
        if not isinstance(td, dict):
            continue
        r5 = td.get("return_5d")
        r30 = td.get("return_30d")
        if r5 is None and r30 is None:
            continue
        snap: dict = {"symbol": etf}
        if r5 is not None:
            snap["return_5d"] = r5
        if r30 is not None:
            snap["return_30d"] = r30
        price = td.get("price") or td.get("current_price")
        if price is not None:
            snap["price"] = price
        out[etf] = snap
    return out


# ---------------------------------------------------------------------------
# Screener registry
# ---------------------------------------------------------------------------

# Map screener-name → bound `evaluate(...)` callable. Populated by screener
# modules at import time via `register_screener`. PR 1.1 leaves this empty.
_REGISTRY: dict[str, Callable[..., Optional[SetupCandidate]]] = {}

# Set of screener names that are "world-scan" screeners: called once per
# pipeline invocation with the full ticker_data dict, and return
# list[SetupCandidate] (not Optional[SetupCandidate]). sector_rotation is the
# canonical example — it ranks ALL sector ETFs in one pass, not one ticker at
# a time.
_WORLD_SCAN: set[str] = set()


def register_screener(
    name: str,
    evaluate_fn: Callable[..., Optional[SetupCandidate]],
    *,
    world_scan: bool = False,
) -> None:
    """Register a screener's `evaluate(...)` callable. Idempotent — repeated
    registration with the same name overwrites (useful for hot-reload in tests).

    Args:
        world_scan: When True the screener is called ONCE per pipeline run
            with the full `ticker_data` dict and must return
            `list[SetupCandidate]`. Use for screeners that rank a fixed
            universe (e.g. sector_rotation) rather than evaluating one ticker
            at a time.
    """
    _REGISTRY[name] = evaluate_fn
    if world_scan:
        _WORLD_SCAN.add(name)
    else:
        _WORLD_SCAN.discard(name)


def _registered_screeners() -> dict[str, Callable[..., Optional[SetupCandidate]]]:
    """Snapshot of the current registry. Returned as a plain dict so callers
    can iterate without worrying about mutation mid-loop."""
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# Inner dispatcher (no I/O)
# ---------------------------------------------------------------------------

def evaluate_all(
    world_state: dict,
    universe: list[str],
    ticker_data: dict[str, dict],
    earnings_calendar: Optional[dict] = None,
    sector_breadth: Optional[dict] = None,
) -> tuple[list[SetupCandidate], dict[str, dict[str, Any]]]:
    """Run every registered screener over the universe, collecting candidates.

    Returns:
        (candidates, per_screener_metrics) where per_screener_metrics is
        `{name: {"count": N}}` for healthy screeners and
        `{name: {"status": "degraded", "reason": "<ExcClass>"}}` for any
        screener that raised.

    No journal writes here — that's `run_screeners_and_journal`.
    """
    candidates: list[SetupCandidate] = []
    metrics: dict[str, dict[str, Any]] = {}

    for name, evaluate_fn in _registered_screeners().items():
        try:
            hits: list[SetupCandidate] = []
            if name in _WORLD_SCAN:
                # World-scan screeners receive the FULL ticker_data dict and
                # return list[SetupCandidate] in a single invocation.
                results = evaluate_fn(
                    ticker_data=ticker_data,
                    world_state=world_state,
                )
                if results:
                    hits.extend(results)
            else:
                for ticker in universe:
                    td = ticker_data.get(ticker) or ticker_data.get(ticker.upper())
                    if td is None:
                        continue
                    earnings_event = None
                    if earnings_calendar:
                        earnings_event = (
                            earnings_calendar.get(ticker)
                            or earnings_calendar.get(ticker.upper())
                        )
                    sector_rs = None
                    if sector_breadth:
                        sector = td.get("sector") if isinstance(td, dict) else None
                        if sector is not None:
                            sector_rs = sector_breadth.get(sector)
                    result = evaluate_fn(
                        ticker_data=td,
                        world_state=world_state,
                        earnings_event=earnings_event,
                        sector_rs=sector_rs,
                    )
                    if result is not None:
                        hits.append(result)
            candidates.extend(hits)
            metrics[name] = {"count": len(hits)}
        except Exception as exc:
            log.warning("screener_health %s error=%s", name, type(exc).__name__)
            metrics[name] = {"status": "degraded", "reason": type(exc).__name__}
            continue

    return candidates, metrics


# ---------------------------------------------------------------------------
# Orchestrator (journals candidates + emits observability breadcrumbs)
# ---------------------------------------------------------------------------

async def run_screeners_and_journal(
    world_state: dict,
    universe: list[str],
    ticker_data: dict[str, dict],
    earnings_calendar: Optional[dict] = None,
    sector_breadth: Optional[dict] = None,
    research_base: Path = Path(".research"),
    cache_branch: str = "unknown",
) -> list[SetupCandidate]:
    """Run every registered screener, journal each `SetupCandidate`, return
    the flat candidate list.

    Args:
        cache_branch: "hit" | "miss" | "unknown" — observability tag from the
            caller so we can distinguish cache-hit screener runs from
            cache-miss runs in logs (the iter-1 cache-bypass guardrail).

    Per-screener degrade: handled inside `evaluate_all`. A single screener
    raising does NOT prevent journaling of candidates from healthy screeners.

    Pipeline-level hard-fail: journal-write failures bubble up (corrupting
    the tally is worse than crashing the brief).
    """
    # Local import to avoid circular `screeners -> journal -> screeners`.
    from research_assistant.journal import append_alert

    start = time.perf_counter()
    log.info(
        "screeners.pipeline cache_branch=%s universe=%d screeners=%d",
        cache_branch,
        len(universe),
        len(_REGISTRY),
    )

    candidates, per_screener = evaluate_all(
        world_state=world_state,
        universe=universe,
        ticker_data=ticker_data,
        earnings_calendar=earnings_calendar,
        sector_breadth=sector_breadth,
    )

    for candidate in candidates:
        append_alert(candidate, research_base)

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "screeners.pipeline cache_branch=%s alert_count=%d elapsed_ms=%d per_screener=%s",
        cache_branch,
        len(candidates),
        elapsed_ms,
        per_screener,
    )

    return candidates
