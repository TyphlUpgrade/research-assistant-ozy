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

- **Phase 1.5 (deferred):** candidate-coverage / hit-rate surface — the
  surface that detects *under-firing* (tickers that ran but the cascade
  never surfaced). Phase 1's verdict-stratification + decile-analysis
  score the verdicts that exist; neither catches the Type II error that
  motivated #19 in the first place (MRVL/DELL on 2026-06-02, both ran
  sharply higher after the cascade tempered them). An operator reading
  Phase 1 output should NOT conclude "the cascade is calibrated" without
  the hit-rate view. See `render_scoreboard` output for the explicit
  caveat in the rendered text.

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


def read_all_stage2(base: Path) -> list[dict]:
    """Read every Stage 2 journal row across all tickers under `base/stage2/`."""
    stage2_dir = base / "stage2"
    if not stage2_dir.exists():
        return []
    rows: list[dict] = []
    for path in sorted(stage2_dir.glob("*.jsonl")):
        rows.extend(_read_jsonl(path))
    return rows


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
    row: dict, cached: dict[str, dict], adapter, today: date
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
    if not ticker or not asof_str:
        return None
    asof_dt = _parse_asof(asof_str)
    if asof_dt is None:
        return None

    if recorded_at and recorded_at in cached:
        if _enrichment_complete(cached[recorded_at], today):
            return (cached[recorded_at], True)
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
    by_ticker: dict[str, dict[str, dict]] = {}
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

# Default verdict-bucket vocabulary for the brief inline Skeptic. Phase 3
# (Stage 3 /research Skeptic comparison) will pass a different tuple
# (`("CONFIRM", "TEMPER", "CHALLENGE", "INVALIDATE")`) — the renderer
# accepts the order as a parameter so it doesn't need to be re-architected
# when that work lands.
DEFAULT_VERDICT_ORDER = ("AGREE", "WEAKEN", "STRONG_OBJECTION", "UNAVAILABLE")


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
    # The under-firing caveat. Phase 1 measures whether the verdicts and
    # convictions the cascade emitted predict returns on the tickers it
    # surfaced — it does NOT measure tickers the cascade declined to
    # surface that subsequently moved. Reading this scoreboard as "the
    # cascade is well-calibrated" is unsafe without the Phase 1.5
    # hit-rate surface (deferred, FOLLOWUPS #19).
    lines.append(
        "ⓘ This view scores tickers we surfaced; it does NOT detect tickers "
        "we missed (Type II error). See FOLLOWUPS #19 Phase 1.5."
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
