"""
Outcome tracker — computes 7d/30d/90d forward returns for journaled alerts.

Enrichment is lazy: `alerts review` (PR 1.3) calls `enrich_window` which fans
out across a `_ENRICH_CONCURRENCY = 5` semaphore-bounded `asyncio.gather`
(matches the `brief._STAGE_2_CONCURRENCY` pattern, scoped locally because
this layer is yfinance-bound rather than Anthropic-bound).

A horizon return is None when:
  - the horizon hasn't elapsed yet (asof + Xd > today), OR
  - yfinance returns None price (delisting, holiday, missing bar) — we do NOT
    write a sentinel like 0.0, since None means "unknown" while 0.0 means
    "no movement".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from research_assistant.screeners import SetupCandidate

log = logging.getLogger(__name__)


# Concurrency cap for parallel yfinance enrichment lookups.
# Mirrors `brief._STAGE_2_CONCURRENCY = 5` but kept local since this layer is
# yfinance-rate-limit-bound, not Anthropic-bound.
_ENRICH_CONCURRENCY = 5

_HORIZONS = (("return_7d", 7), ("return_30d", 30), ("return_90d", 90))


def _parse_asof(asof: str) -> date:
    return datetime.strptime(asof, "%Y-%m-%d").date()


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Heuristic classifier for yfinance rate-limit errors. Matches anything
    whose exception class or message mentions rate-limit / 429 — broad on
    purpose so we don't silently miscount transient throttling as 'other'."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "ratelimit" in msg.replace(" ", "").replace("-", ""):
        return True
    if "429" in msg:
        return True
    if "too many requests" in msg:
        return True
    return False


async def enrich_alert_with_returns(alert: dict, adapter) -> dict:
    """Fill in `return_7d`, `return_30d`, `return_90d` for one alert.

    `adapter` must expose `async fetch_price_at(symbol, target_date) -> float | None`.
    Returns the alert dict with horizon fields populated (or left None if the
    horizon hasn't elapsed / yfinance returned None).
    """
    asof_dt = _parse_asof(alert["asof"])
    entry = alert.get("entry_price")
    if entry is None or entry == 0:
        return alert
    today = date.today()

    enriched = dict(alert)
    for field_name, days in _HORIZONS:
        target = asof_dt + timedelta(days=days)
        if target > today:
            enriched[field_name] = None
            continue
        price = await adapter.fetch_price_at(alert["ticker"], target)
        if price is None:
            enriched[field_name] = None
            continue
        enriched[field_name] = round((price / entry) - 1.0, 4)
    return enriched


def _row_to_candidate(row: dict) -> SetupCandidate:
    return SetupCandidate(
        ticker=row["ticker"],
        screener=row["screener"],
        asof=row["asof"],
        entry_price=row["entry_price"],
        evidence=row.get("evidence", {}),
        return_7d=row.get("return_7d"),
        return_30d=row.get("return_30d"),
        return_90d=row.get("return_90d"),
    )


async def enrich_window(alerts: list[dict], adapter) -> dict[str, int]:
    """Parallel-enrich `alerts` under a `_ENRICH_CONCURRENCY` semaphore.

    For each successfully-enriched alert, writes a new row via
    `append_enriched_alert` (creating an LWW-superseding row that the next
    `read_alerts_window` call will collapse).

    Returns summary `{"enriched": N, "failed_rate_limit": M, "failed_other": K}`.

    `adapter` must expose `async fetch_price_at(symbol, target_date)`. Caller
    threads through the same yfinance adapter used by `data_loader`.

    `base` for the enrichment writes is taken from `adapter.research_base` if
    present, else `.research`. Callers that need a custom base must set
    `adapter.research_base = Path(...)` (tests use `tmp_path`).
    """
    from research_assistant.journal.alerts import append_enriched_alert

    base: Path = getattr(adapter, "research_base", Path(".research"))
    sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)
    summary = {"enriched": 0, "failed_rate_limit": 0, "failed_other": 0}

    async def _one(alert: dict) -> tuple[str, Optional[dict]]:
        async with sem:
            try:
                enriched = await enrich_alert_with_returns(alert, adapter)
                return ("ok", enriched)
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    log.warning(
                        "enrich_window rate_limit ticker=%s asof=%s err=%s",
                        alert.get("ticker"), alert.get("asof"), exc,
                    )
                    return ("rate_limit", None)
                log.warning(
                    "enrich_window error ticker=%s asof=%s err=%s",
                    alert.get("ticker"), alert.get("asof"), exc,
                )
                return ("other", None)

    results = await asyncio.gather(*[_one(a) for a in alerts])
    for status, enriched in results:
        if status == "ok" and enriched is not None:
            append_enriched_alert(_row_to_candidate(enriched), base)
            summary["enriched"] += 1
        elif status == "rate_limit":
            summary["failed_rate_limit"] += 1
        else:
            summary["failed_other"] += 1
    return summary
