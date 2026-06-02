"""
Scoreboard — operator-facing verdict→outcome calibration view (FOLLOWUPS #19).

Reads `.research/stage2/<TICKER>.jsonl`, joins each row with forward returns
(5d / 10d / 30d) computed lazily from yfinance and cached at
`.research/stage2_returns/<TICKER>.jsonl`, then emits two stratifications:

1. **Verdict → return distribution** by `skeptic_verdict` bucket
   (AGREE / WEAKEN / STRONG_OBJECTION / UNAVAILABLE). Tells the operator
   whether the brief inline Skeptic's verdicts have any forward-return
   signal — e.g. do WEAKEN-tagged entries actually underperform AGREE?
2. **Conviction decile analysis** — sort all entries by composite_conviction
   (post-Skeptic), bucket into ≤10 quantile groups, median forward return
   per bucket. Monotonic increasing across deciles → composite_conviction
   has predictive signal. Flat → score is decorative.

Operator-facing only. The cascade never reads this output. Preserves the
counter-cyclical backbone (no calibration feedback loop into Skeptic).

Phase 1 scope (per FOLLOWUPS #19 audit, 2026-06-02): brief inline Skeptic
verdicts only (`stage_2_skeptic_check`). Stage 3 `/research` Skeptic
verdicts (CONFIRM/TEMPER/CHALLENGE/INVALIDATE) live in trace JSONLs and
require trace-event joining — deferred to Phase 3. Regime stratification
and momentum-gate-state stratification require extending the Stage 2
journal schema — deferred to Phase 2.

The sidecar cache is append-only LWW keyed by `recorded_at`:
re-running scoreboard does not re-fetch already-enriched entries.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Swing-horizon forward-return windows. Days-to-weeks aligns with the
# product's mission (see project_mission_swing_trading.md). Alerts journal
# uses 7/30/90d — those are setup-finder horizons, not swing horizons.
HORIZONS: tuple[tuple[str, int], ...] = (
    ("return_5d", 5),
    ("return_10d", 10),
    ("return_30d", 30),
)

_ENRICH_CONCURRENCY = 5

# Below this enriched-count per bucket, render a small-N warning rather
# than let the operator anchor on noise-bound medians.
_SMALL_N_WARN = 5


# ---------------------------------------------------------------------------
# Price adapter shim
# ---------------------------------------------------------------------------
#
# Scoreboard's enrichment talks to a duck-typed protocol:
#
#     async fetch_price_at(symbol: str, target_date: date) -> float | None
#
# Tests pass canned-price stubs. The real ozymandias `YFinanceAdapter` exposes
# `fetch_bars(symbol, interval, period)` returning a pandas DataFrame; the
# `BarsBackedPriceAdapter` wrapper below adapts that surface to the protocol
# and caches the bars DataFrame per symbol so a multi-horizon enrichment
# fans out to ONE network fetch per ticker (not one per horizon).
#
# Note: the existing alerts-journal enrichment (journal/outcomes.py) calls
# `fetch_price_at` directly on the raw YFinanceAdapter and silently fails
# (the method doesn't exist on the real adapter). That's why
# `.research/alerts/*.jsonl` show all-null `return_*` fields — separate bug,
# not in scoreboard's scope to fix.

class BarsBackedPriceAdapter:
    """Wrap a yfinance-style `fetch_bars` adapter to expose the
    `fetch_price_at(symbol, target_date)` protocol scoreboard uses.

    One DataFrame cache per symbol over the lifetime of the wrapper —
    enrichments for the same ticker across multiple horizons (and across
    multiple journal rows on the same ticker) share a single network
    fetch. Concurrent fetches for the same symbol coalesce via a per-symbol
    asyncio Event (the first task fetches, others wait then read the cache).
    """

    # 6mo of 1d bars comfortably covers up to a 30d forward horizon on an
    # asof date that's ~5 months old. The Stage 2 journal in practice
    # starts late May 2026, so 6mo is plenty; widen if older asof dates
    # appear.
    _BARS_INTERVAL = "1d"
    _BARS_PERIOD = "6mo"

    def __init__(self, inner) -> None:
        self.inner = inner
        self._bars_cache: dict[str, object] = {}  # symbol → DataFrame|None
        self._inflight: dict[str, asyncio.Event] = {}

    async def _bars(self, symbol: str):
        if symbol in self._bars_cache:
            return self._bars_cache[symbol]
        # Coalesce concurrent fetches for the same symbol.
        evt = self._inflight.get(symbol)
        if evt is not None:
            await evt.wait()
            return self._bars_cache.get(symbol)
        evt = asyncio.Event()
        self._inflight[symbol] = evt
        try:
            df = await self.inner.fetch_bars(
                symbol, self._BARS_INTERVAL, self._BARS_PERIOD,
            )
        except Exception as exc:
            log.warning("BarsBackedPriceAdapter: fetch_bars failed symbol=%s err=%s",
                        symbol, exc)
            df = None
        self._bars_cache[symbol] = df
        evt.set()
        self._inflight.pop(symbol, None)
        return df

    async def fetch_price_at(
        self, symbol: str, target_date: date
    ) -> Optional[float]:
        """Return the close on `target_date` if a bar exists for that date,
        otherwise the close on the latest trading day strictly before it.
        Returns None when the symbol has no bars in the window, or when the
        target predates the available history.
        """
        df = await self._bars(symbol)
        if df is None:
            return None
        # yfinance DataFrames typically have a DatetimeIndex; tolerate
        # variations (some adapters return tz-aware vs naive). All we need
        # is a way to compare each bar's date with `target_date`.
        try:
            empty = df.empty
        except AttributeError:
            return None
        if empty:
            return None
        try:
            idx_dates = df.index.date  # numpy array of date objects
        except AttributeError:
            return None
        try:
            close_col = df["close"]
        except (KeyError, TypeError):
            return None
        # Bars on or before target, take the most recent.
        last_close: Optional[float] = None
        for bar_date, close in zip(idx_dates, close_col):
            if bar_date <= target_date:
                last_close = close
            else:
                break  # bars are date-ordered ascending in yfinance output
        if last_close is None:
            return None
        try:
            return float(last_close)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredEntry:
    """A Stage 2 journal row joined with its forward returns."""
    ticker: str
    asof: str
    recorded_at: str
    composite_conviction: float
    skeptic_verdict: str
    decision_tag: str
    entry_price: Optional[float]
    return_5d: Optional[float]
    return_10d: Optional[float]
    return_30d: Optional[float]


# ---------------------------------------------------------------------------
# Journal + cache I/O
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    """Tolerant JSONL reader — skips bad lines, returns only dicts. Mirrors
    the conventions of `journal/stage2_notes._read_raw` and
    `journal/alerts._read_day_raw`."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("rb") as f:
        for raw in f:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def read_all_stage2(base: Path) -> list[dict]:
    """Read every Stage 2 journal row across all tickers under `base/stage2/`."""
    stage2_dir = base / "stage2"
    if not stage2_dir.exists():
        return []
    rows: list[dict] = []
    for path in sorted(stage2_dir.glob("*.jsonl")):
        rows.extend(_read_jsonl(path))
    return rows


def _cache_path(base: Path, ticker: str) -> Path:
    return base / "stage2_returns" / f"{ticker}.jsonl"


def read_return_cache(base: Path, ticker: str) -> dict[str, dict]:
    """Read the sidecar return cache for a ticker. Returns a dict keyed by
    `recorded_at` with the most-recent enrichment row per key (LWW — later
    rows in the file supersede earlier ones)."""
    out: dict[str, dict] = {}
    for row in _read_jsonl(_cache_path(base, ticker)):
        key = row.get("recorded_at")
        if key:
            out[key] = row
    return out


def append_return_cache(base: Path, ticker: str, entry: dict) -> None:
    """Append a single return-cache row for `ticker`. Caller ensures the
    entry has at least `recorded_at`."""
    path = _cache_path(base, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _parse_asof(asof: str) -> Optional[date]:
    try:
        return datetime.strptime(asof, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _enrichment_complete(cached: dict, today: date) -> bool:
    """A cached entry is 'complete enough to reuse' if every horizon that
    has elapsed by `today` has a non-null return field. Horizons still in
    the future are allowed to be None — they'll be filled on a later run.
    """
    asof_dt = _parse_asof(cached.get("asof", ""))
    if asof_dt is None:
        return True  # can't recompute anyway, treat as complete
    for field_name, days in HORIZONS:
        target = asof_dt + timedelta(days=days)
        if target <= today and cached.get(field_name) is None:
            return False
    return True


async def _enrich_one(
    row: dict, cached: dict[str, dict], adapter, today: date
) -> Optional[dict]:
    """Compute the return-cache entry for one Stage 2 row.

    Returns:
        - The cached enrichment if present AND complete for elapsed horizons.
        - A freshly-computed enrichment otherwise (caller persists it).
        - None if the row is unparseable (no ticker / asof).

    A cached entry is reused when every horizon that's elapsed by `today`
    is non-null. If a horizon was null because it hadn't elapsed yet, but
    now has, we re-enrich (this is the 'fill in matured horizons' case).
    """
    recorded_at = row.get("recorded_at")
    ticker = row.get("ticker")
    asof_str = row.get("asof")
    if not ticker or not asof_str:
        return None
    asof_dt = _parse_asof(asof_str)
    if asof_dt is None:
        return None

    if recorded_at and recorded_at in cached:
        if _enrichment_complete(cached[recorded_at], today):
            return cached[recorded_at]
        # Cached entry exists but some elapsed horizons are still null —
        # this is the "horizon matured since last run" case. Fall through
        # to re-fetch.

    try:
        entry_price = await adapter.fetch_price_at(ticker, asof_dt)
    except Exception as exc:
        log.warning(
            "scoreboard: entry-price fetch failed ticker=%s asof=%s err=%s",
            ticker, asof_str, exc,
        )
        entry_price = None

    enriched: dict = {
        "schema_version": 1,
        "recorded_at": recorded_at,
        "ticker": ticker,
        "asof": asof_str,
        "entry_price": entry_price,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }

    if entry_price is None or entry_price == 0:
        for field_name, _ in HORIZONS:
            enriched[field_name] = None
        return enriched

    for field_name, days in HORIZONS:
        target = asof_dt + timedelta(days=days)
        if target > today:
            enriched[field_name] = None
            continue
        try:
            price = await adapter.fetch_price_at(ticker, target)
        except Exception as exc:
            log.warning(
                "scoreboard: horizon fetch failed ticker=%s asof=%s h=%d err=%s",
                ticker, asof_str, days, exc,
            )
            price = None
        if price is None:
            enriched[field_name] = None
        else:
            enriched[field_name] = round((price / entry_price) - 1.0, 4)

    return enriched


async def enrich_stage2_rows(
    rows: list[dict], adapter, base: Path
) -> list[ScoredEntry]:
    """Enrich all Stage 2 rows with forward returns. Bounded concurrency.

    Cache hits return immediately (no fetch). Cache misses fetch and
    persist a new row to the sidecar cache.
    """
    today = date.today()
    sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    by_ticker: dict[str, dict[str, dict]] = {}
    for row in rows:
        t = row.get("ticker")
        if t and t not in by_ticker:
            by_ticker[t] = read_return_cache(base, t)

    async def _one(row: dict) -> Optional[ScoredEntry]:
        async with sem:
            ticker = row.get("ticker")
            if not ticker:
                return None
            cache = by_ticker.get(ticker, {})
            recorded_at = row.get("recorded_at", "")
            enriched = await _enrich_one(row, cache, adapter, today)
            if enriched is None:
                return None
            cached_now = cache.get(recorded_at)
            # Only persist when we actually got an entry price. A failed
            # fetch (entry_price=None) means the symbol was unfetchable on
            # this run; caching that would freeze the row in a broken state
            # and prevent the next run from retrying. Let it stay
            # un-cached so a future run gets a fresh shot.
            should_persist = (
                recorded_at
                and enriched.get("entry_price") is not None
                and (cached_now is None or cached_now is not enriched)
            )
            if should_persist:
                append_return_cache(base, ticker, enriched)
                cache[recorded_at] = enriched
            return ScoredEntry(
                ticker=ticker,
                asof=row.get("asof", ""),
                recorded_at=recorded_at,
                composite_conviction=float(row.get("composite_conviction") or 0.0),
                skeptic_verdict=str(row.get("skeptic_verdict") or "UNAVAILABLE"),
                decision_tag=str(row.get("decision_tag") or ""),
                entry_price=enriched.get("entry_price"),
                return_5d=enriched.get("return_5d"),
                return_10d=enriched.get("return_10d"),
                return_30d=enriched.get("return_30d"),
            )

    results = await asyncio.gather(*[_one(r) for r in rows])
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _quantile(values: list[float], q: float) -> float:
    """Approximate quantile via linear interpolation. Caller ensures
    `values` is non-empty; for an empty list returns nan."""
    if not values:
        return float("nan")
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def stratify_by_verdict(
    entries: list[ScoredEntry], horizon_field: str
) -> dict[str, dict]:
    """Group by `skeptic_verdict`; compute count, enriched_count, median +
    p25/p75 over the horizon field for entries with non-null returns."""
    buckets: dict[str, list[ScoredEntry]] = {}
    for e in entries:
        buckets.setdefault(e.skeptic_verdict, []).append(e)
    out: dict[str, dict] = {}
    for verdict, items in buckets.items():
        returns = [
            getattr(e, horizon_field) for e in items
            if getattr(e, horizon_field) is not None
        ]
        out[verdict] = {
            "count": len(items),
            "enriched_count": len(returns),
            "median": _median(returns) if returns else None,
            "p25": _quantile(returns, 0.25) if returns else None,
            "p75": _quantile(returns, 0.75) if returns else None,
            "small_n": len(returns) < _SMALL_N_WARN,
        }
    return out


def decile_analysis(
    entries: list[ScoredEntry], horizon_field: str
) -> list[dict]:
    """Sort by composite_conviction, bucket into ≤10 quantile groups,
    compute median forward return per bucket. Returns ascending-conviction
    list of `{bucket, conviction_lo, conviction_hi, count, median_return,
    small_n}`. Empty buckets are omitted (occurs only when N < n_buckets;
    not reachable given the bucketing formula below)."""
    enriched = [e for e in entries if getattr(e, horizon_field) is not None]
    if not enriched:
        return []
    sorted_e = sorted(enriched, key=lambda e: e.composite_conviction)
    n = len(sorted_e)
    n_buckets = min(10, n)
    buckets: list[list[ScoredEntry]] = [[] for _ in range(n_buckets)]
    for i, e in enumerate(sorted_e):
        idx = min(i * n_buckets // n, n_buckets - 1)
        buckets[idx].append(e)
    out: list[dict] = []
    for i, items in enumerate(buckets):
        if not items:
            continue
        convs = [e.composite_conviction for e in items]
        returns = [getattr(e, horizon_field) for e in items]
        out.append({
            "bucket": i + 1,
            "conviction_lo": min(convs),
            "conviction_hi": max(convs),
            "count": len(items),
            "median_return": _median(returns),
            "small_n": len(items) < _SMALL_N_WARN,
        })
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

_VERDICT_ORDER = ("AGREE", "WEAKEN", "STRONG_OBJECTION", "UNAVAILABLE")


def render_scoreboard(
    entries: list[ScoredEntry], horizon_field: str = "return_10d"
) -> str:
    """Operator-facing text output for the chosen horizon.

    Pure function over `entries`; easy to snapshot-test against fixtures.
    """
    lines: list[str] = []
    horizon_label = horizon_field.replace("return_", "")
    lines.append(f"# Scoreboard — verdict→{horizon_label} return calibration")
    lines.append("")
    lines.append(f"Total Stage 2 entries: {len(entries)}")
    if not entries:
        lines.append("")
        lines.append(
            "(no Stage 2 journal data — run `/brief` and `/research <TICKER>` "
            "to start journaling)"
        )
        return "\n".join(lines)

    enriched_count = sum(
        1 for e in entries if getattr(e, horizon_field) is not None
    )
    lines.append(
        f"Enriched ({horizon_label} horizon): {enriched_count} / {len(entries)}"
    )
    if 0 < enriched_count < _SMALL_N_WARN:
        lines.append(
            f"⚠ SMALL N: only {enriched_count} entries have {horizon_label} "
            "forward returns. Stats below are noise-bound — rerun once journal "
            "has matured."
        )
    elif enriched_count == 0:
        lines.append(
            f"⚠ Zero entries have {horizon_label} forward returns yet (horizon "
            "hasn't elapsed for any journaled date). Rerun in "
            f"{horizon_field.replace('return_', '').replace('d', '')}+ days."
        )
    lines.append("")

    # Verdict stratification
    lines.append(f"## Verdict → {horizon_label} return")
    lines.append("")
    by_verdict = stratify_by_verdict(entries, horizon_field)
    if not by_verdict:
        lines.append("(no verdicts captured)")
    else:
        rendered_any = False
        for verdict in _VERDICT_ORDER:
            if verdict not in by_verdict:
                continue
            b = by_verdict[verdict]
            if b["enriched_count"] == 0:
                lines.append(
                    f"- **{verdict}** — count={b['count']}, "
                    f"no enriched {horizon_label} returns yet"
                )
                rendered_any = True
                continue
            warn = "  ⚠ small-N" if b["small_n"] else ""
            lines.append(
                f"- **{verdict}** — count={b['count']} "
                f"(enriched {b['enriched_count']})  "
                f"median={b['median']:+.2%}  "
                f"p25={b['p25']:+.2%}  p75={b['p75']:+.2%}{warn}"
            )
            rendered_any = True
        # Surface unexpected verdict labels at the end (e.g. future enum additions).
        extras = sorted(set(by_verdict) - set(_VERDICT_ORDER))
        for verdict in extras:
            b = by_verdict[verdict]
            warn = "  ⚠ small-N" if b["small_n"] else ""
            median_repr = (
                f"{b['median']:+.2%}" if b["median"] is not None else "n/a"
            )
            lines.append(
                f"- **{verdict}** (unrecognized) — count={b['count']} "
                f"(enriched {b['enriched_count']})  median={median_repr}{warn}"
            )
            rendered_any = True
        if not rendered_any:
            lines.append("(no verdicts captured)")
    lines.append("")

    # Conviction decile analysis
    lines.append(f"## Conviction decile → {horizon_label} return")
    lines.append("")
    deciles = decile_analysis(entries, horizon_field)
    if not deciles:
        lines.append("(no enriched entries for decile analysis)")
    else:
        lines.append(
            "Monotonic increasing across deciles = composite_conviction has "
            "predictive signal. Flat = score is decorative."
        )
        lines.append("")
        lines.append("| Decile | Conv. range | N | Median return |")
        lines.append("|---|---|---|---|")
        for d in deciles:
            warn = " ⚠" if d["small_n"] else ""
            lines.append(
                f"| {d['bucket']} | "
                f"{d['conviction_lo']:.2f}–{d['conviction_hi']:.2f} | "
                f"{d['count']}{warn} | {d['median_return']:+.2%} |"
            )

    return "\n".join(lines)
