"""
Scoreboard — operator-facing verdict→outcome calibration view (FOLLOWUPS #19).

Reads `.research/stage2/<TICKER>.jsonl`, joins each row with forward returns
(5d / 10d / 30d) computed lazily from yfinance and cached at
`.research/stage2_returns/<TICKER>.jsonl`, then emits two stratifications:

1. **Verdict → return distribution** by `skeptic_verdict` bucket. Tells the
   operator whether the brief inline Skeptic's verdicts have any forward-
   return signal — e.g. do WEAKEN-tagged entries actually underperform AGREE?
2. **Conviction decile analysis** — sort all entries by composite_conviction
   (post-Skeptic), bucket into ≤10 quantile groups, median forward return
   per bucket. Monotonic increasing across deciles → composite_conviction
   has predictive signal. Flat → score is decorative.

Operator-facing only. The cascade never reads this output. Preserves the
counter-cyclical backbone (no calibration feedback loop into Skeptic).

Phase scope per FOLLOWUPS #19 audit (2026-06-02 / revised 2026-06-03):

- **Phase 1 (shipped):** brief inline Skeptic verdicts only — the
  AGREE/WEAKEN/STRONG_OBJECTION enum from `stage_2_skeptic_check`.
  Verdict-bucket vocabulary used in the renderer is configurable via
  `render_scoreboard(verdict_order=...)` so Phase 3 can swap in the
  Stage 3 vocabulary without refactoring.

- **Phase 1.5 (shipped 2026-06-03):** candidate-coverage / hit-rate
  surface — detects *under-firing* (tickers that ran but the cascade
  never surfaced at meaningful conviction). Joins the alerts journal
  (`.research/alerts/*.jsonl`, the system's record of "interesting
  candidates") with the Stage 2 journal by `(ticker, asof window)` and
  reports: of top-quartile movers (alerts with forward returns in the
  top quartile of the window), what fraction were surfaced in a brief
  within `lookback_days` of the alert at composite_conviction ≥ X for
  thresholds X ∈ {0.4, 0.5, 0.6}? See `compute_hit_rate` +
  `render_hit_rate`.

- **Phase 2 (deferred):** regime + momentum-gate stratification. Requires
  extending the Stage 2 journal schema additively with `regime` and
  `momentum_gate_state` fields (per its existing SCHEMA CONTRACT —
  additive-only). `orchestrator.py` already has these values at write
  time in `world_state`; Phase 2 just plumbs them into `Stage2Note` +
  `_note_to_row`.

- **Phase 3 (deferred):** anchor-only vs full-data Skeptic comparison.
  Stage 3 `/research` Skeptic verdicts (CONFIRM/TEMPER/CHALLENGE/
  INVALIDATE) live in trace JSONLs, not in the Stage 2 journal. Needs
  a separate trace-event reader + join by `(ticker, chain_id)`.

Cache shape
-----------

Forward returns land in a sidecar at `.research/stage2_returns/<TICKER>.jsonl`
rather than in the Stage 2 journal itself. The reason is **schema shape**,
not security: the Stage 2 journal allows multiple rows per (ticker, asof)
because the operator may re-run `/brief` several times per day. LWW-by-key
collapse would break that "all reads recorded for trajectory analysis"
contract (see `journal/stage2_notes.py:6-9`). A sidecar keyed by
`recorded_at` keeps the journal's append-only multi-row semantics intact
while still supporting LWW collapse on the enrichment cache.

The sidecar is append-only LWW keyed by `recorded_at`. Re-running scoreboard
does not re-fetch already-enriched entries (unless an elapsed horizon was
previously null and has since matured).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from research_assistant.price import BarsBackedPriceAdapter

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


# Mirrors `journal/stage2_notes._MAX_LINE_BYTES`. Defense-in-depth on the
# reader side: oversized lines are skipped with a WARN rather than read
# into memory in full. The Stage 2 writer enforces the same cap at write
# time, so the only path that delivers an oversize row is a corrupted /
# hand-edited / externally-injected file. The sidecar cache files also
# benefit from the same guard.
_MAX_LINE_BYTES = 16 * 1024


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredEntry:
    """A Stage 2 journal row joined with its forward returns.

    `pre_skeptic_conviction` carries the Stage 2 thesis's raw score
    BEFORE the Skeptic applied its discount. Required for the
    discount-magnitude calibration view (FOLLOWUPS #23). Optional
    because the brief journal schema doesn't carry it today — it's
    populated for research entries via the trace reader, and brief
    entries leave it None until the journal schema is additively
    extended.
    """
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
    pre_skeptic_conviction: Optional[float] = None
    source: str = "brief"           # "brief" | "research"


# ---------------------------------------------------------------------------
# Journal + cache I/O
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    """Tolerant JSONL reader — skips bad lines, returns only dicts. Mirrors
    the conventions of `journal/stage2_notes._read_raw` and
    `journal/alerts._read_day_raw`, including the oversize-line guard."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("rb") as f:
        for raw in f:
            if len(raw) > _MAX_LINE_BYTES:
                log.warning(
                    "scoreboard: JSONL line >%dB in %s; skipping",
                    _MAX_LINE_BYTES, path,
                )
                continue
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


def _dedup_latest_per_ticker_asof(rows: list[dict]) -> list[dict]:
    """Collapse multiple cascade re-runs of the same `(ticker, asof)` to a
    single row by keeping the latest by `recorded_at`.

    Background (FOLLOWUPS #19 Phase 1.7, 2026-06-08): the same operator
    can run /brief or /research multiple times against the same
    ticker-date — each run writes a separate journal row carrying its
    own `composite_conviction` and `recorded_at`. The forward-return
    cache is keyed on `(ticker, asof)`, so all those rows JOIN against
    the same return value. Without dedup, decile / verdict
    stratification triple-counts the same outcome.

    Observed empirical impact: pre-fix scoreboard reported decile-10
    (≥0.44 post-Skeptic conviction) → +19.14% median 10d return on
    N=6, where 3 of 6 entries were MRVL on 2026-05-29 (same +40.9%
    return attributed to three distinct journal rows). Post-fix:
    decile-10 collapses to N=5 unique trades, median = -2.62%, 17% of
    bootstrap resamples positive. The "system works at the top" claim
    was a hygiene artifact, not a signal.

    Latest-by-`recorded_at` is the safe choice: it reflects what the
    operator's most recent cascade run said, not an averaging fiction
    across re-runs.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for row in rows:
        ticker = row.get("ticker")
        asof = row.get("asof")
        if not ticker or not asof:
            continue
        key = (str(ticker), str(asof))
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        # Tie-breaker: later recorded_at wins. Missing recorded_at sorts
        # as empty string (loses to anything non-empty).
        cur_ts = row.get("recorded_at") or ""
        prev_ts = existing.get("recorded_at") or ""
        if cur_ts > prev_ts:
            by_key[key] = row
    return list(by_key.values())


def read_all_stage2(base: Path) -> list[dict]:
    """Read every Stage 2 journal row across all tickers under `base/stage2/`.

    Same `(ticker, asof)` rows are deduplicated (latest `recorded_at`
    wins) to prevent same-trade triple-counting in downstream decile /
    verdict analysis. See `_dedup_latest_per_ticker_asof` for the
    forward-return-cache rationale."""
    stage2_dir = base / "stage2"
    if not stage2_dir.exists():
        return []
    rows: list[dict] = []
    for path in sorted(stage2_dir.glob("*.jsonl")):
        rows.extend(_read_jsonl(path))
    return _dedup_latest_per_ticker_asof(rows)


def read_all_research(base: Path) -> list[dict]:
    """Read every research (Stage 3 Skeptic) entry across all tickers,
    projected into the same row shape `enrich_stage2_rows` accepts.

    Sourced from the unified history reader's research projection —
    dossier ledger entries with trace-enrichment populating the numeric
    fields. Entries whose trace file is missing (no `composite_conviction`
    available) are skipped: forward-return calibration requires a
    conviction number to be meaningful.

    Tags each row with `source="research"` so the enrich path can
    propagate that into `ScoredEntry.source` for downstream filtering.

    Same `(ticker, asof)` rows are deduplicated (latest `recorded_at`
    wins) to prevent same-trade triple-counting in downstream decile /
    verdict analysis. See `_dedup_latest_per_ticker_asof`.
    """
    from research_assistant.history import (
        enumerate_tickers,
        read_unified_history,
    )

    rows: list[dict] = []
    for ticker in enumerate_tickers(base):
        for entry in read_unified_history(ticker, base):
            if entry.source != "research":
                continue
            if entry.composite_conviction is None:
                # No trace file (pruned / pre-trace-era / different
                # machine); without the numeric conviction the entry
                # can't participate in return calibration.
                continue
            rows.append({
                "ticker": entry.ticker,
                "asof": entry.asof,
                "recorded_at": entry.recorded_at,
                "composite_conviction": entry.composite_conviction,
                "pre_skeptic_conviction": entry.pre_skeptic_conviction,
                "skeptic_verdict": entry.skeptic_verdict or "UNAVAILABLE",
                "decision_tag": entry.decision_tag or "",
                "source": "research",
            })
    return _dedup_latest_per_ticker_asof(rows)


def _safe_ticker(ticker: object) -> Optional[str]:
    """Validate `ticker` for filesystem-path use. Mirrors the regex from
    `journal/stage2_notes._validate_ticker`. Returns the canonical
    upper-cased ticker, or None if validation fails (a path-traversal
    attempt or unicode lookalike would land here)."""
    # Lazy import — avoids a circular dependency at module import time
    # (journal.stage2_notes does not import scoreboard, but pulling the
    # regex via the canonical source keeps the contract in one place).
    from research_assistant.journal.stage2_notes import _TICKER_RE
    if not isinstance(ticker, str):
        return None
    candidate = ticker.upper()
    if _TICKER_RE.match(candidate):
        return candidate
    return None


def _cache_path(base: Path, ticker: str) -> Path:
    """Compute the sidecar cache path. Caller MUST pass a ticker that
    has already been validated via `_safe_ticker`; this function trusts
    its input. The resolved path is double-checked to live inside
    `base/stage2_returns/` to defend against a regex-bypass."""
    candidate = base / "stage2_returns" / f"{ticker}.jsonl"
    # Resolved-path safety net: even if ticker somehow bypasses the regex,
    # this catches absolute paths and `..` components.
    expected_parent = (base / "stage2_returns").resolve()
    try:
        resolved = candidate.resolve()
        resolved.relative_to(expected_parent)
    except (ValueError, OSError):
        raise ValueError(
            f"_cache_path: resolved path escapes stage2_returns dir: {candidate}"
        )
    return candidate


def read_return_cache(base: Path, ticker: str) -> dict[tuple[str, str], dict]:
    """Read the sidecar return cache for a ticker. Returns a dict keyed by
    `(source, recorded_at)` with the most-recent enrichment row per key
    (LWW — later rows in the file supersede earlier ones).

    Brief and research entries share the same per-ticker cache file but
    must NOT collide on a `recorded_at`-only key: a race between brief
    and research writing the same microsecond timestamp would have
    silently clobbered one side's cached returns. Cached rows without a
    `source` field (written before the schema was tagged) default to
    `"brief"`.
    """
    out: dict[tuple[str, str], dict] = {}
    for row in _read_jsonl(_cache_path(base, ticker)):
        recorded_at = row.get("recorded_at")
        if recorded_at:
            source = row.get("source") or "brief"
            out[(source, recorded_at)] = row
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
    has elapsed by `today` has a non-null return field AND the entry
    price itself was successfully fetched. Horizons still in the future
    are allowed to be None — they'll be filled on a later run.

    A cached entry whose `asof` no longer parses is treated as complete
    (refetching can't recover) but the corruption is logged so an operator
    investigating odd buckets can find the bad row.
    """
    asof_dt = _parse_asof(cached.get("asof", ""))
    if asof_dt is None:
        log.warning(
            "scoreboard: cached row has unparseable asof=%r recorded_at=%r — "
            "treating as complete (cannot refetch); investigate manually",
            cached.get("asof"), cached.get("recorded_at"),
        )
        return True
    # If a previous run failed to get the entry price (and somehow got
    # persisted — shouldn't happen with current code, but be defensive),
    # treat as incomplete so we retry on a future run.
    if cached.get("entry_price") is None:
        return False
    for field_name, days in HORIZONS:
        target = asof_dt + timedelta(days=days)
        if target <= today and cached.get(field_name) is None:
            return False
    return True


async def _enrich_one(
    row: dict, cached: dict[tuple[str, str], dict], adapter, today: date
) -> Optional[tuple[dict, bool]]:
    """Compute the return-cache entry for one Stage 2 row.

    Returns:
        - `(cached_dict, True)` if the cache had a complete-enough row.
        - `(freshly_computed_dict, False)` otherwise (caller persists).
        - None if the row is unparseable (no ticker / asof).

    The explicit `came_from_cache` flag replaces an earlier identity-check
    pattern (`cached_now is enriched`) that was brittle to refactor — a
    defensive copy of the cache dict would have silently caused every
    cache hit to look like a miss and re-append the same row.

    A cached entry is reused when every horizon that's elapsed by `today`
    is non-null AND `entry_price` is non-null. If a horizon was null
    because it hadn't elapsed yet but now has, we re-enrich (the
    matured-horizon case).
    """
    recorded_at = row.get("recorded_at")
    ticker = row.get("ticker")
    asof_str = row.get("asof")
    source = row.get("source") or "brief"
    if not ticker or not asof_str:
        return None
    asof_dt = _parse_asof(asof_str)
    if asof_dt is None:
        return None

    cache_key = (source, recorded_at) if recorded_at else None
    if cache_key and cache_key in cached:
        if _enrichment_complete(cached[cache_key], today):
            return (cached[cache_key], True)
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
        "source": source,
        "entry_price": entry_price,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
    }

    if entry_price is None or entry_price == 0:
        for field_name, _ in HORIZONS:
            enriched[field_name] = None
        return (enriched, False)

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

    return (enriched, False)


async def enrich_stage2_rows(
    rows: list[dict], adapter, base: Path
) -> list[ScoredEntry]:
    """Enrich all Stage 2 rows with forward returns. Bounded concurrency.

    Cache hits return immediately (no fetch). Cache misses fetch and
    persist a new row to the sidecar cache, *unless* `entry_price` came
    back None — failed-fetch rows are not persisted so the next run
    retries.

    Rows with unrecognized tickers (path-traversal attempts, lookalikes,
    or just journal corruption) are dropped with a WARN and a None return
    in the results list.
    """
    today = date.today()
    sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)

    # Validate tickers and pre-load caches once per ticker.
    by_ticker: dict[str, dict[tuple[str, str], dict]] = {}
    for row in rows:
        raw_ticker = row.get("ticker")
        validated = _safe_ticker(raw_ticker)
        if validated is None:
            continue
        if validated not in by_ticker:
            by_ticker[validated] = read_return_cache(base, validated)

    async def _one(row: dict) -> Optional[ScoredEntry]:
        async with sem:
            ticker = _safe_ticker(row.get("ticker"))
            if ticker is None:
                log.warning(
                    "scoreboard: dropping row with invalid ticker=%r recorded_at=%r",
                    row.get("ticker"), row.get("recorded_at"),
                )
                return None
            cache = by_ticker.get(ticker, {})
            recorded_at = row.get("recorded_at", "")
            result = await _enrich_one(row, cache, adapter, today)
            if result is None:
                return None
            enriched, came_from_cache = result
            # Persist on cache miss IFF the fetch actually produced an
            # entry price. Failed-fetch rows (entry_price=None) are not
            # cached so the next run retries them with whatever adapter
            # state is available then.
            if (
                not came_from_cache
                and recorded_at
                and enriched.get("entry_price") is not None
            ):
                append_return_cache(base, ticker, enriched)
                enriched_source = enriched.get("source") or "brief"
                cache[(enriched_source, recorded_at)] = enriched
            # `composite_conviction_pre_skeptic` is the journal canonical
            # name (matches the Stage2Note field). `pre_skeptic_conviction`
            # is the alias used by `read_all_research`'s row projection.
            # Accept either for forward-compat as we converge on one name.
            pre = row.get("composite_conviction_pre_skeptic")
            if pre is None:
                pre = row.get("pre_skeptic_conviction")
            pre_float = float(pre) if isinstance(pre, (int, float)) else None
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
                pre_skeptic_conviction=pre_float,
                source=str(row.get("source") or "brief"),
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
# Discount-magnitude analysis (FOLLOWUPS #23)
# ---------------------------------------------------------------------------

# Discount-magnitude buckets in absolute conviction points.
# `(lo, hi, label)` — hi is exclusive on the upper edge except the last
# bucket which is open-ended. The first bucket captures the case where
# the Skeptic actually RAISED conviction (post > pre); without it those
# rows were silently dropped from calibration. The 0-pt bucket isolates
# AGREE-shaped reads where the Skeptic didn't move the score.
_DISCOUNT_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (-float("inf"), -0.005, "Skeptic raised"),     # post > pre by ≥0.005
    (-0.005,         0.005, "0 pts"),              # essentially no discount
    ( 0.005,         0.05,  "0–5 pts"),
    ( 0.05,          0.10,  "5–10 pts"),
    ( 0.10,          0.20,  "10–20 pts"),
    ( 0.20,          float("inf"), "20+ pts"),
)


def discount_analysis(
    entries: list[ScoredEntry], horizon_field: str
) -> list[dict]:
    """Bucket entries by Skeptic discount magnitude and report median
    forward return per bucket.

    Discount = pre_skeptic_conviction - composite_conviction. Entries
    without a pre_skeptic value (today: all brief entries) are skipped.

    Hypothesis the view tests: does the *magnitude* of the discount have
    forward-return signal? If 0-pt buckets cluster around zero return
    and 20+pt buckets cluster negative, the Skeptic's discount size is
    informative. Flat across buckets → discount magnitude is noise.
    """
    out: list[dict] = []
    eligible = [
        e for e in entries
        if e.pre_skeptic_conviction is not None
        and getattr(e, horizon_field) is not None
    ]
    if not eligible:
        return []
    for lo, hi, label in _DISCOUNT_BUCKETS:
        in_bucket = [
            e for e in eligible
            if lo <= (e.pre_skeptic_conviction - e.composite_conviction) < hi
        ]
        if not in_bucket:
            continue
        returns = [getattr(e, horizon_field) for e in in_bucket]
        out.append({
            "bucket": label,
            "count": len(in_bucket),
            "median_return": _median(returns),
            "p25": _quantile(returns, 0.25),
            "p75": _quantile(returns, 0.75),
            "small_n": len(in_bucket) < _SMALL_N_WARN,
        })
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

# Default verdict-bucket vocabulary for the brief inline Skeptic. Phase 3
# (Stage 3 /research Skeptic comparison) will pass a different tuple
# (`("CONFIRM", "TEMPER", "CHALLENGE", "INVALIDATE")`) — the renderer
# accepts the order as a parameter so it doesn't need to be re-architected
# when that work lands.
DEFAULT_VERDICT_ORDER = ("AGREE", "WEAKEN", "STRONG_OBJECTION", "UNAVAILABLE")

# Stage 3 (research) Skeptic verdict vocabulary. Wider than the brief
# inline Skeptic's 3-level enum — TEMPER and CHALLENGE are the
# distinguishing additions, both representing partial vs. full bull-
# pillar dismantling.
RESEARCH_VERDICT_ORDER = (
    "AGREE", "WEAKEN", "TEMPER", "CHALLENGE", "STRONG_OBJECTION", "UNAVAILABLE",
)


def render_scoreboard(
    entries: list[ScoredEntry],
    horizon_field: str = "return_10d",
    *,
    verdict_order: Iterable[str] = DEFAULT_VERDICT_ORDER,
) -> str:
    """Operator-facing text output for the chosen horizon.

    `verdict_order` controls the display order of known verdict buckets;
    any verdicts present in the data that are NOT in this tuple are
    surfaced at the bottom under an `(unrecognized)` heading. Phase 3
    will pass the Stage 3 verdict vocabulary here instead.

    Pure function over `entries`; easy to snapshot-test against fixtures.
    """
    verdict_order = tuple(verdict_order)
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
    # This section scores tickers the cascade surfaced. The "Hit rate"
    # section below (rendered separately by `render_hit_rate`) covers the
    # complementary axis — tickers that moved but the cascade missed.
    lines.append(
        "ⓘ This view scores tickers we surfaced. The Hit-rate section "
        "below covers Type II error (tickers we missed)."
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
        for verdict in verdict_order:
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
        # Surface unexpected verdict labels at the end. These are buckets
        # present in the data but not in `verdict_order` — Phase 3 will
        # legitimately have multiple vocabularies in play (brief vs
        # /research Skeptic) and the caller chooses which to feature.
        extras = sorted(set(by_verdict) - set(verdict_order))
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


def render_research_scoreboard(
    entries: list[ScoredEntry], horizon_field: str = "return_10d"
) -> str:
    """Operator-facing text output for research-Skeptic entries.

    Renders three sections specific to the `/research` Stage 3 Skeptic:
    - Verdict → forward return (5-level vocabulary)
    - Post-Skeptic conviction decile → forward return
    - Discount magnitude → forward return (FOLLOWUPS #23 — measures
      whether the Skeptic's discount size carries return signal)

    Pure function over `entries`. Caller filters `entries` to
    `source=="research"` before invoking; the function does not
    re-filter.
    """
    lines: list[str] = []
    horizon_label = horizon_field.replace("return_", "")
    lines.append(
        f"# Research-Skeptic scoreboard — verdict→{horizon_label} "
        f"return calibration"
    )
    lines.append("")
    lines.append(f"Total research entries: {len(entries)}")
    if not entries:
        lines.append("")
        lines.append(
            "(no research entries with trace-enriched conviction — "
            "run `/research <TICKER>` to populate, or check that trace "
            "files exist at `.research/traces/<date>/<chain>.jsonl`)"
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
            "forward returns. Stats below are noise-bound."
        )
    elif enriched_count == 0:
        lines.append(
            f"⚠ Zero entries have {horizon_label} forward returns yet."
        )
    lines.append("")

    # Section 1 — verdict stratification (5-level vocabulary)
    lines.append(f"## Verdict → {horizon_label} return")
    lines.append("")
    by_verdict = stratify_by_verdict(entries, horizon_field)
    rendered = False
    for verdict in RESEARCH_VERDICT_ORDER:
        if verdict not in by_verdict:
            continue
        b = by_verdict[verdict]
        if b["enriched_count"] == 0:
            lines.append(
                f"- **{verdict}** — count={b['count']}, "
                f"no enriched {horizon_label} returns yet"
            )
            rendered = True
            continue
        warn = "  ⚠ small-N" if b["small_n"] else ""
        lines.append(
            f"- **{verdict}** — count={b['count']} "
            f"(enriched {b['enriched_count']})  "
            f"median={b['median']:+.2%}  "
            f"p25={b['p25']:+.2%}  p75={b['p75']:+.2%}{warn}"
        )
        rendered = True
    if not rendered:
        lines.append("(no verdicts captured)")
    lines.append("")

    # Section 2 — post-Skeptic conviction deciles
    lines.append(f"## Post-Skeptic conviction decile → {horizon_label} return")
    lines.append("")
    deciles = decile_analysis(entries, horizon_field)
    if not deciles:
        lines.append("(no enriched entries for decile analysis)")
    else:
        lines.append(
            "Monotonic increasing across deciles = post-Skeptic conviction "
            "has predictive signal."
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
    lines.append("")

    # Section 3 — discount magnitude (the novel #23 view)
    lines.append(f"## Discount magnitude → {horizon_label} return")
    lines.append("")
    discounts = discount_analysis(entries, horizon_field)
    if not discounts:
        lines.append(
            "(no entries have both pre- and post-Skeptic convictions yet)"
        )
    else:
        lines.append(
            "Discount = pre_skeptic_conviction − composite_conviction. "
            "If large-discount buckets show worse forward returns than "
            "0-point buckets, the Skeptic's discount magnitude is "
            "informative. Flat across buckets = discount size is noise."
        )
        lines.append("")
        lines.append("| Discount | N | Median return | p25 / p75 |")
        lines.append("|---|---|---|---|")
        for d in discounts:
            warn = " ⚠" if d["small_n"] else ""
            lines.append(
                f"| {d['bucket']} | {d['count']}{warn} | "
                f"{d['median_return']:+.2%} | "
                f"{d['p25']:+.2%} / {d['p75']:+.2%} |"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 1.5 — candidate-coverage / hit-rate
# ---------------------------------------------------------------------------

# The alerts journal uses different forward-return horizons (7d/30d/90d)
# than the Stage 2 scoreboard (5d/10d/30d). These constants pin the alerts
# horizon names so callers don't typo them.
ALERTS_HORIZON_FIELDS = ("return_7d", "return_30d", "return_90d")

# Default conviction thresholds to slice the surface coverage by. At ≥0.4
# the brief survivor floor; ≥0.5 = moderate confidence; ≥0.6 = high
# (anything above 0.6 today is the top end of the observed range — see
# Phase 1 deciles 0.06–0.60).
DEFAULT_CONVICTION_THRESHOLDS: tuple[float, ...] = (0.4, 0.5, 0.6)

# Default top-quantile cutoff for "movers". 0.75 = top quartile. Operator
# can pass a smaller cut (e.g. 0.5 = top half) when the alerts journal is
# sparse and quartile cuts produce only 1-2 movers.
DEFAULT_TOP_QUANTILE = 0.75

# Default lookback window — how many days after an alert fires does the
# brief have to surface the ticker before we count it as "missed"? Three
# days covers same-day + next two trading days; longer windows let stale
# brief reactions count and dilute the signal.
DEFAULT_LOOKBACK_DAYS = 3


def compute_hit_rate(
    alerts: list[dict],
    entries: list[ScoredEntry],
    *,
    horizon_field: str = "return_30d",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    conviction_thresholds: tuple[float, ...] = DEFAULT_CONVICTION_THRESHOLDS,
    top_quantile: float = DEFAULT_TOP_QUANTILE,
) -> dict:
    """Of top-quantile movers in `alerts`, what fraction did the cascade
    surface in `entries` at composite_conviction ≥ threshold, within
    `lookback_days` of the alert?

    Args:
        alerts: rows from `read_alerts_window`. Caller is responsible for
            picking the window; alerts WITHOUT an enriched `horizon_field`
            return are skipped (their horizon hasn't elapsed yet).
        entries: `ScoredEntry` list from `enrich_stage2_rows`.
        horizon_field: which alerts-horizon field to use. Must be one of
            ALERTS_HORIZON_FIELDS.
        lookback_days: brief asof ∈ [alert.asof, alert.asof + lookback_days]
            counts as "the brief surfaced this alert."
        conviction_thresholds: ascending list of thresholds to bucket
            surfaces by.
        top_quantile: 0.75 = top quartile; 0.5 = top half.

    Returns dict with structure:
        {
            "total_alerts": int,                    # all alerts passed in
            "enriched_alerts": int,                 # alerts with non-null horizon
            "movers": int,                          # alerts ≥ cutoff
            "cutoff_return": float | None,          # the cutoff value used
            "by_threshold": [
                {
                    "threshold": float,
                    "surfaced": int,
                    "hit_rate": float,  # surfaced / movers; 0.0 when movers==0
                    "small_n": bool,    # movers < _SMALL_N_WARN
                },
                ...
            ],
            "horizon_field": str,
            "lookback_days": int,
            "top_quantile": float,
        }

    Both AGREE-on-empty and the explicit "0/0 = undefined hit rate" cases
    use 0.0 for `hit_rate`; check `movers` before reading it.
    """
    if horizon_field not in ALERTS_HORIZON_FIELDS:
        raise ValueError(
            f"horizon_field must be one of {ALERTS_HORIZON_FIELDS}, "
            f"got {horizon_field!r}"
        )

    enriched_alerts = [
        a for a in alerts
        if isinstance(a.get(horizon_field), (int, float))
    ]
    base = {
        "total_alerts": len(alerts),
        "enriched_alerts": len(enriched_alerts),
        "movers": 0,
        "cutoff_return": None,
        "by_threshold": [],
        "horizon_field": horizon_field,
        "lookback_days": lookback_days,
        "top_quantile": top_quantile,
    }
    if not enriched_alerts:
        return base

    # Top-quantile cutoff. With small N this is fragile; the renderer
    # surfaces a small-N warning so the operator doesn't over-interpret.
    returns = sorted(a[horizon_field] for a in enriched_alerts)
    cutoff_value = _quantile(returns, top_quantile)
    movers = [
        a for a in enriched_alerts
        if a[horizon_field] >= cutoff_value
    ]
    base["movers"] = len(movers)
    base["cutoff_return"] = cutoff_value

    # Pre-index stage 2 entries by ticker for O(1) lookup.
    entries_by_ticker: dict[str, list[ScoredEntry]] = {}
    for e in entries:
        entries_by_ticker.setdefault(e.ticker, []).append(e)

    by_threshold: list[dict] = []
    for thresh in conviction_thresholds:
        surfaced = 0
        for alert in movers:
            ticker = alert.get("ticker")
            asof_str = alert.get("asof")
            if not ticker or not asof_str:
                continue
            alert_asof = _parse_asof(asof_str)
            if alert_asof is None:
                continue
            window_end = alert_asof + timedelta(days=lookback_days)
            for e in entries_by_ticker.get(ticker, []):
                e_asof = _parse_asof(e.asof)
                if e_asof is None:
                    continue
                if alert_asof <= e_asof <= window_end and e.composite_conviction >= thresh:
                    surfaced += 1
                    break  # one surface per alert is enough
        hit_rate = surfaced / len(movers) if movers else 0.0
        by_threshold.append({
            "threshold": thresh,
            "surfaced": surfaced,
            "hit_rate": hit_rate,
            "small_n": len(movers) < _SMALL_N_WARN,
        })
    base["by_threshold"] = by_threshold
    return base


def render_hit_rate(result: dict) -> str:
    """Operator-facing text output for the hit-rate analysis."""
    lines: list[str] = []
    horizon_label = result["horizon_field"].replace("return_", "")
    pct = int(round(result["top_quantile"] * 100))
    top_label = "quartile" if pct == 75 else "half" if pct == 50 else f"{100 - pct}%"
    lines.append(
        f"## Hit rate — top-{top_label} movers ({horizon_label} horizon, "
        f"alerts journal)"
    )
    lines.append("")
    lines.append(
        f"Alerts in window: {result['total_alerts']} "
        f"(enriched at {horizon_label}: {result['enriched_alerts']})"
    )
    if result["enriched_alerts"] == 0:
        lines.append("")
        lines.append(
            f"(no alerts have enriched {horizon_label} returns yet — "
            f"rerun once alerts older than {horizon_label} accumulate)"
        )
        return "\n".join(lines)

    if result["movers"] == 0:
        lines.append("")
        lines.append("(no movers above the top-quantile cutoff)")
        return "\n".join(lines)

    cutoff = result["cutoff_return"]
    lines.append(
        f"Movers (top {top_label}, {horizon_label} ≥ {cutoff:+.2%}): "
        f"{result['movers']}"
    )
    if result["movers"] < _SMALL_N_WARN:
        lines.append(
            f"⚠ SMALL N: only {result['movers']} movers — hit-rate "
            "percentages below are dominated by single-ticker outcomes."
        )
    lines.append(f"Lookback window: alert.asof + {result['lookback_days']}d.")
    lines.append("")
    lines.append("| Conviction threshold | Surfaced | Hit rate |")
    lines.append("|---|---|---|")
    for b in result["by_threshold"]:
        lines.append(
            f"| ≥{b['threshold']:.2f} | "
            f"{b['surfaced']} / {result['movers']} | "
            f"{b['hit_rate']:.0%} |"
        )
    lines.append("")
    lines.append(
        "Interpretation: 0% hit rate at high thresholds with non-zero at "
        "low thresholds = cascade sees these tickers but expresses "
        "structurally low conviction. 0% across all thresholds = cascade "
        "never surfaced them at all."
    )
    return "\n".join(lines)
