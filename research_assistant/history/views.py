"""
Operator-facing history views (FOLLOWUPS #22).

Projections over the unified history reader (`history.reader`) that the
`/history` CLI exposes. Three views today:

- `brief <T>` — every brief mention of T, chronological.
- `cohort <T1,T2,...>` — date × ticker grid of verdict + conviction.
- `verdicts` — cross-ticker scan filtered by date / verdict / ticker.

Pure read-only projection. No LLM calls, no live yfinance fetches.
Ticker enumeration uses the on-disk artifacts:
- Journal: `.research/stage2/<T>.jsonl` (one file per ticker that has
  ever surfaced in a brief).
- Ledger: `.research/tickers/<T>.md` (one file per ticker that has
  ever been `/research`'d).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from research_assistant.history.reader import (
    Stage2HistoryEntry,
    read_unified_history,
)


def enumerate_tickers(base: Path) -> list[str]:
    """Return every ticker with at least one history record (journal or
    ledger), sorted alphabetically.

    Uses on-disk file presence rather than walking entry contents so the
    enumeration is O(N tickers) not O(total entries).
    """
    found: set[str] = set()
    journal_dir = base / "stage2"
    if journal_dir.exists():
        for f in journal_dir.glob("*.jsonl"):
            if f.is_file():
                found.add(f.stem.upper())
    tickers_dir = base / "tickers"
    if tickers_dir.exists():
        for f in tickers_dir.glob("*.md"):
            if f.is_file() and not f.name.endswith(".lock"):
                found.add(f.stem.upper())
    return sorted(found)


def _parse_since(since: Optional[str]) -> Optional[date]:
    """Parse a `--since` arg into an absolute date.

    Accepts either an ISO date (`2026-05-29`) or an `Nd` relative form
    (`7d`, `30d`). Returns None when `since` is None (meaning "no
    lower bound").
    """
    if since is None:
        return None
    s = since.strip()
    if s.endswith("d") and s[:-1].isdigit():
        days = int(s[:-1])
        return date.today() - timedelta(days=days)
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(
            f"--since accepts an ISO date or 'Nd' relative form, got {since!r}"
        ) from exc


def _entry_in_window(entry: Stage2HistoryEntry, since: Optional[date]) -> bool:
    if since is None:
        return True
    try:
        return date.fromisoformat(entry.asof) >= since
    except ValueError:
        return False


def filter_history_since(
    entries: list[Stage2HistoryEntry],
    since: Optional[str] = None,
) -> list[Stage2HistoryEntry]:
    """Return entries whose `asof` is ≥ since (or all entries if None)."""
    since_date = _parse_since(since)
    return [e for e in entries if _entry_in_window(e, since_date)]


# ---------------------------------------------------------------------------
# View 1: history brief <T>
# ---------------------------------------------------------------------------

def history_brief(
    ticker: str, base: Path, *, since: Optional[str] = None,
) -> list[Stage2HistoryEntry]:
    """Every brief mention of `ticker`, oldest first."""
    all_entries = read_unified_history(ticker, base)
    brief_entries = [e for e in all_entries if e.source == "brief"]
    return filter_history_since(brief_entries, since)


# ---------------------------------------------------------------------------
# View 2: history cohort <T1,T2,...>
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CohortGrid:
    """Date-ticker grid of history entries. `rows[date][ticker]` is the
    most-recent entry on that day (None if no read happened that day).
    """
    tickers: tuple[str, ...]
    dates: tuple[str, ...]                  # ascending
    rows: dict[str, dict[str, Optional[Stage2HistoryEntry]]]


def build_cohort_grid(
    tickers: list[str], base: Path, *, since: Optional[str] = None,
) -> CohortGrid:
    """Build a date × ticker grid for cross-ticker comparison.

    Tickers preserve caller order (so `cohort MU,MRVL,INTC` renders MU
    in column 1, MRVL in column 2). Dates are sorted ascending. Cell
    value is the LAST entry on that (ticker, date) — when both a brief
    and a research read happened on the same day, the later one wins,
    matching what the operator most likely cares about.
    """
    upper = [t.upper() for t in tickers]
    per_ticker: dict[str, list[Stage2HistoryEntry]] = {}
    all_dates: set[str] = set()
    for ticker in upper:
        entries = filter_history_since(read_unified_history(ticker, base), since)
        per_ticker[ticker] = entries
        for e in entries:
            all_dates.add(e.asof)
    sorted_dates = sorted(all_dates)

    rows: dict[str, dict[str, Optional[Stage2HistoryEntry]]] = {}
    for d in sorted_dates:
        rows[d] = {}
        for ticker in upper:
            day_entries = [e for e in per_ticker[ticker] if e.asof == d]
            if not day_entries:
                rows[d][ticker] = None
                continue
            # Multiple entries same day: pick the one with the latest
            # recorded_at so the operator sees the most-recent read.
            day_entries.sort(key=lambda e: e.recorded_at)
            rows[d][ticker] = day_entries[-1]

    return CohortGrid(
        tickers=tuple(upper), dates=tuple(sorted_dates), rows=rows,
    )


# ---------------------------------------------------------------------------
# View 3: history verdicts [--since] [--verdict] [--ticker]
# ---------------------------------------------------------------------------

def filter_verdicts(
    base: Path,
    *,
    since: Optional[str] = None,
    verdicts: Optional[set[str]] = None,
    tickers: Optional[list[str]] = None,
) -> list[Stage2HistoryEntry]:
    """Cross-ticker scan filtered by date window, verdict word, and
    optional ticker subset. Returns chronologically (oldest first).

    `tickers=None` enumerates every ticker on disk. `verdicts=None`
    keeps all verdict words (including None — i.e. the rare
    pre-prompt-format research entries).
    """
    pool = [t.upper() for t in tickers] if tickers else enumerate_tickers(base)
    verdict_filter = {v.upper() for v in verdicts} if verdicts else None

    collected: list[Stage2HistoryEntry] = []
    for ticker in pool:
        for entry in filter_history_since(
            read_unified_history(ticker, base), since,
        ):
            if verdict_filter is not None:
                if entry.skeptic_verdict is None:
                    continue
                if entry.skeptic_verdict.upper() not in verdict_filter:
                    continue
            collected.append(entry)
    collected.sort(key=lambda e: (e.asof, e.recorded_at, e.ticker))
    return collected


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _fmt_conviction(entry: Stage2HistoryEntry) -> str:
    """Render the conviction column. Shows `pre → post` when both are
    present and differ; otherwise just the available number, or `?`.
    """
    pre = entry.pre_skeptic_conviction
    post = entry.composite_conviction
    if pre is not None and post is not None and abs(pre - post) > 0.005:
        return f"{pre:.2f}→{post:.2f}"
    if post is not None:
        return f"{post:.2f}"
    if pre is not None:
        return f"{pre:.2f}"
    return "?"


def render_history_brief(
    ticker: str, entries: list[Stage2HistoryEntry]
) -> str:
    """Per-ticker brief history table."""
    if not entries:
        return f"# {ticker} — brief history\n\n(no brief reads)"
    header = (
        f"# {ticker} — brief history ({len(entries)} entries, "
        f"{entries[0].asof} → {entries[-1].asof})"
    )
    rows = [header, ""]
    rows.append(
        f"{'date':<11} {'conv':>9} {'verdict':<18} {'tag':<10} thesis_preview"
    )
    rows.append("-" * 100)
    for e in entries:
        conv = _fmt_conviction(e)
        verdict = e.skeptic_verdict or "?"
        tag = e.decision_tag or "?"
        preview = (e.bull_anchor or "")[:50]
        rows.append(f"{e.asof:<11} {conv:>9} {verdict:<18} {tag:<10} {preview}")
    return "\n".join(rows)


def render_cohort_grid(grid: CohortGrid) -> str:
    """Date × ticker ASCII grid. Each cell is `verdict conv` (e.g.
    `CHAL 0.20`). Empty cells render as `-`. Verdict words are
    abbreviated to fit the column.
    """
    if not grid.dates:
        return "# History cohort\n\n(no history)"

    verdict_abbr = {
        "AGREE": "AGRE", "WEAKEN": "WEAK", "TEMPER": "TEMP",
        "CHALLENGE": "CHAL", "STRONG_OBJECTION": "S_OB",
    }

    def cell(entry: Optional[Stage2HistoryEntry]) -> str:
        if entry is None:
            return "-"
        verdict = verdict_abbr.get(entry.skeptic_verdict or "", "?")
        return f"{verdict} {_fmt_conviction(entry)}"

    cell_w = 14
    header = (
        f"# History cohort — {len(grid.tickers)} tickers × "
        f"{len(grid.dates)} dates ({grid.dates[0]} → {grid.dates[-1]})"
    )
    rows = [header, ""]
    rows.append(
        "date         " + "".join(f"  {t:<{cell_w}}" for t in grid.tickers)
    )
    rows.append("-" * (13 + (cell_w + 2) * len(grid.tickers)))
    for d in grid.dates:
        cells = "".join(f"  {cell(grid.rows[d][t]):<{cell_w}}" for t in grid.tickers)
        rows.append(f"{d}  {cells}")
    return "\n".join(rows)


def render_verdicts_table(entries: list[Stage2HistoryEntry]) -> str:
    """Cross-ticker verdict scan table."""
    if not entries:
        return "# History verdicts\n\n(no matching entries)"
    header = f"# History verdicts ({len(entries)} entries)"
    rows = [header, ""]
    rows.append(
        f"{'date':<11} {'ticker':<7} {'source':<9} {'conv':>9} {'verdict':<18}"
    )
    rows.append("-" * 60)
    for e in entries:
        rows.append(
            f"{e.asof:<11} {e.ticker:<7} {e.source:<9} "
            f"{_fmt_conviction(e):>9} {(e.skeptic_verdict or '?'):<18}"
        )
    return "\n".join(rows)
