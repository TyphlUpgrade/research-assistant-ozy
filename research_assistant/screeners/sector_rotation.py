"""
Sector-rotation screener (PR 1.2).

Fires when an XL-series ETF moves from bottom-half to top-quartile relative-
strength rank over a 5d→30d return basis (plan §2 PR 1.2 acceptance).

Concretely:
  - rs_rank_now   = rank by return_5d  (1 = strongest)
  - rs_rank_prior = rank by return_30d (1 = strongest; 30d ≈ plan's 20d basis)
  - Fire condition: rs_rank_prior > N/2  (was in bottom half)
                AND rs_rank_now  ≤ ceil(N/4) (now in top quartile)
  For N=11: rank_prior > 5 AND rank_now ≤ 3.

Input (world_state["sector_performance"]):
  dict[etf_symbol -> snapshot] where each snapshot has at minimum:
    "return_5d"  : float   (5-bar pct change)
    "return_30d" : float   (30-bar pct change, used as prior-window proxy)
    "price"      : float   (current price; used as entry_price)

Per-screener degrade: if world_state["sector_performance"] is None or empty,
returns [] and logs WARN "screener_health sector_rotation degraded=missing_sector_data".

No I/O inside evaluate() — all data must be pre-fetched by the caller.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from research_assistant.screeners._types import SetupCandidate, register_formatter
from research_assistant.screeners._pipeline import register_screener

log = logging.getLogger(__name__)

_SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLY", "XLV", "XLI", "XLB", "XLU", "XLP", "XLC", "XLRE"]


def evaluate(
    ticker_data: dict,
    world_state: dict,
    earnings_event: Optional[dict] = None,  # unused — protocol uniformity
    sector_rs: Optional[dict] = None,        # unused — protocol uniformity
) -> list[SetupCandidate]:
    """Evaluate all sector ETFs for a bottom-half→top-quartile RS rotation.

    Called once per pipeline run (world-scan screener). Returns a list of
    SetupCandidate — one per qualifying sector ETF.

    Args:
        ticker_data: Full ticker_data dict (keyed by symbol). Used to look up
            per-ETF price when world_state["sector_performance"] lacks it.
        world_state: Must contain "sector_performance" key with per-ETF
            snapshots (return_5d, return_30d, price).
        earnings_event: Unused; kept for protocol uniformity.
        sector_rs: Unused; kept for protocol uniformity.
    """
    sector_perf: Any = world_state.get("sector_performance") if world_state else None
    if not sector_perf:
        log.warning("screener_health sector_rotation degraded=missing_sector_data")
        return []

    # Collect ETFs that have both return fields present.
    etf_data: list[tuple[str, float, float]] = []  # (symbol, return_5d, return_30d)
    for etf in _SECTOR_ETFS:
        snap = sector_perf.get(etf)
        if not snap:
            continue
        r5 = snap.get("return_5d")
        r30 = snap.get("return_30d")
        if r5 is None or r30 is None:
            continue
        etf_data.append((etf, float(r5), float(r30)))

    if not etf_data:
        log.warning("screener_health sector_rotation degraded=missing_sector_data")
        return []

    n = len(etf_data)
    top_quartile_cutoff = math.ceil(n / 4)   # rank_now ≤ this to be top quartile
    bottom_half_cutoff = n / 2               # rank_prior > this to be bottom half

    # Rank by return_5d descending → rs_rank_now (1 = best).
    sorted_by_5d = sorted(etf_data, key=lambda x: x[1], reverse=True)
    rank_now: dict[str, int] = {sym: i + 1 for i, (sym, _, _) in enumerate(sorted_by_5d)}

    # Rank by return_30d descending → rs_rank_prior (1 = best).
    sorted_by_30d = sorted(etf_data, key=lambda x: x[2], reverse=True)
    rank_prior: dict[str, int] = {sym: i + 1 for i, (sym, _, _) in enumerate(sorted_by_30d)}

    asof = world_state.get("asof") if isinstance(world_state, dict) else None
    if not asof:
        asof = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    candidates: list[SetupCandidate] = []

    for etf, r5, r30 in etf_data:
        rn = rank_now[etf]
        rp = rank_prior[etf]

        # Fire condition: was in bottom half on 30d basis, now in top quartile on 5d basis.
        if not (rp > bottom_half_cutoff and rn <= top_quartile_cutoff):
            continue

        # Look up entry price: prefer sector_performance snapshot, fall back to ticker_data.
        snap = sector_perf.get(etf, {})
        entry_price: Optional[float] = snap.get("price")
        if entry_price is None:
            td = ticker_data.get(etf) if isinstance(ticker_data, dict) else None
            if isinstance(td, dict):
                entry_price = td.get("current_price") or td.get("price")

        if entry_price is None:
            log.warning(
                "screener_health sector_rotation skipped=missing_price ticker=%s", etf
            )
            continue

        candidates.append(
            SetupCandidate(
                ticker=etf,
                screener="sector_rotation",
                asof=asof,
                entry_price=float(entry_price),
                evidence={
                    "sector_etf": etf,
                    "rs_rank_now": rn,
                    "rs_rank_prior": rp,
                    "basis_days": 30,
                    "return_5d": r5,
                    "return_30d": r30,
                },
            )
        )

    return candidates


# ---------------------------------------------------------------------------
# Per-screener formatter
# ---------------------------------------------------------------------------

def _render_line(c: SetupCandidate) -> str:
    e = c.evidence
    return (
        f"- **{c.ticker}** (sector_rotation) — "
        f"rank {e['rs_rank_prior']}→{e['rs_rank_now']} on {e['basis_days']}d basis; "
        f"5d {e['return_5d']:+.1%} / 30d {e['return_30d']:+.1%}"
    )


register_formatter("sector_rotation", _render_line)
register_screener("sector_rotation", evaluate, world_scan=True)
