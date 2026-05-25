"""
Tests for the append-only alert journal (PR 1.1).

Covers:
- `append_alert` dedups on `(asof, ticker, screener)` — second call no-ops
- `append_enriched_alert` always appends; readers apply LWW
- `read_alerts_window` collapses creation + enrichment rows correctly
- `read_alerts_window` globs day-files across the requested range
- Module docstring carries the SCHEMA CONTRACT block (write-time contract test)
- Module docstring documents the single-writer assumption
"""
from __future__ import annotations

import json
from pathlib import Path

from research_assistant.journal import (
    append_alert,
    append_enriched_alert,
    read_alerts_window,
)
from research_assistant.journal import alerts
from research_assistant.screeners import SetupCandidate


def _candidate(
    ticker: str = "NVDA",
    screener: str = "sector_rotation",
    asof: str = "2026-05-23",
    entry_price: float = 945.32,
    evidence: dict | None = None,
) -> SetupCandidate:
    return SetupCandidate(
        ticker=ticker,
        screener=screener,
        asof=asof,
        entry_price=entry_price,
        evidence=evidence if evidence is not None else {"reason": "sector_rs_break"},
    )


def test_append_alert_dedups_same_key(tmp_path: Path) -> None:
    c = _candidate()
    assert append_alert(c, tmp_path) is True
    assert append_alert(c, tmp_path) is False  # dedup hit

    path = tmp_path / "alerts" / "2026-05-23.jsonl"
    assert path.exists()
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_append_enriched_alert_lww(tmp_path: Path) -> None:
    c = _candidate()
    append_alert(c, tmp_path)
    # Two enrichment rows for the same key
    append_enriched_alert(c, tmp_path)
    append_enriched_alert(c, tmp_path)

    path = tmp_path / "alerts" / "2026-05-23.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3  # creation + 2 enrichments

    # Read with LWW collapsing — should return one row, the latest enrichment
    rows = read_alerts_window(tmp_path, "2026-05-23", "2026-05-23")
    assert len(rows) == 1
    assert rows[0]["enriched_at"] is not None

    enrichment_timestamps = [
        json.loads(ln)["enriched_at"]
        for ln in lines
        if json.loads(ln)["enriched_at"] is not None
    ]
    assert rows[0]["enriched_at"] == max(enrichment_timestamps)


def test_read_window_applies_lww(tmp_path: Path) -> None:
    c = _candidate()
    append_alert(c, tmp_path)            # creation row
    append_enriched_alert(c, tmp_path)   # first enrichment
    append_enriched_alert(c, tmp_path)   # second enrichment (winner)

    rows = read_alerts_window(tmp_path, "2026-05-23", "2026-05-23")
    assert len(rows) == 1
    winner = rows[0]
    assert winner["ticker"] == "NVDA"
    assert winner["enriched_at"] is not None

    # The winner must be the latest of the two enrichment rows
    path = tmp_path / "alerts" / "2026-05-23.jsonl"
    all_rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    enrichments = [r for r in all_rows if r["enriched_at"] is not None]
    assert winner["enriched_at"] == max(r["enriched_at"] for r in enrichments)


def test_read_alerts_window_globs_day_files(tmp_path: Path) -> None:
    append_alert(_candidate(asof="2026-05-21", ticker="AAPL"), tmp_path)
    append_alert(_candidate(asof="2026-05-22", ticker="MSFT"), tmp_path)
    append_alert(_candidate(asof="2026-05-23", ticker="NVDA"), tmp_path)

    rows = read_alerts_window(tmp_path, "2026-05-21", "2026-05-23")
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT", "NVDA"]

    # Bounded query — only the middle day
    middle = read_alerts_window(tmp_path, "2026-05-22", "2026-05-22")
    assert [r["ticker"] for r in middle] == ["MSFT"]


def test_schema_contract_docstring() -> None:
    from research_assistant.journal import alerts as alerts_mod
    assert alerts_mod.__doc__ is not None
    assert "SCHEMA CONTRACT" in alerts_mod.__doc__


def test_single_writer_documented() -> None:
    from research_assistant.journal import alerts as alerts_mod
    assert alerts_mod.__doc__ is not None
    assert "single-writer" in alerts_mod.__doc__
