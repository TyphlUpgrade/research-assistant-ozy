"""Tests for `research_assistant.history.views` (FOLLOWUPS #22)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    write_dossier_atomic,
)
from research_assistant.history import (
    build_cohort_grid,
    enumerate_tickers,
    filter_verdicts,
    history_brief,
    render_cohort_grid,
    render_history_brief,
    render_verdicts_table,
)
from research_assistant.history.trace_reader import _chain_id_to_trace_path
from research_assistant.history.views import _parse_since
from research_assistant.journal import append_stage2_note
from research_assistant.orchestrator import Stage2Note


def _journal_note(ticker: str, *, verdict: str = "AGREE", composite: float = 0.4) -> Stage2Note:
    return Stage2Note(
        ticker=ticker,
        observation=(),
        bull_anchor="bull",
        bear_anchor="bear",
        what_would_change=(),
        conviction={"technical": 0.5, "fundamental": 0.5, "catalyst": 0.5, "regime": 0.5},
        composite_conviction=composite,
        decision_tag="RESEARCH",
        skeptic_verdict=verdict,
    )


def _seed_ledger(
    base: Path, ticker: str, chain_id: str, timestamp: str,
    *, verdict_word: str = "CHALLENGE",
) -> None:
    dossier = Dossier(
        symbol=ticker,
        ledger=[
            LedgerEntry(
                timestamp=timestamp, kind="thesis",
                summary=f"{ticker} thesis at {timestamp}", evidence_anchor=chain_id,
            ),
            LedgerEntry(
                timestamp=timestamp, kind="skeptic",
                summary=f"Verdict: {verdict_word}. critique body",
                evidence_anchor=chain_id,
            ),
        ],
    )
    write_dossier_atomic(dossier, base)


def _seed_trace(
    traces_base: Path, chain_id: str,
    *, pre: float | None = None, post: float | None = None,
) -> None:
    path = _chain_id_to_trace_path(chain_id, traces_base)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    events = []
    if pre is not None:
        events.append({"stage_id": "stage_2_thesis", "parsed": {"conviction_score": pre}})
    if post is not None:
        events.append({"stage_id": "stage_3_skeptic", "parsed": {"adjusted_score": post}})
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------

class TestParseSince:
    def test_none_returns_none(self):
        assert _parse_since(None) is None

    def test_iso_date(self):
        assert _parse_since("2026-05-29") == date(2026, 5, 29)

    def test_relative_days(self):
        result = _parse_since("7d")
        assert result == date.today() - timedelta(days=7)

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            _parse_since("not-a-date")


# ---------------------------------------------------------------------------
# enumerate_tickers
# ---------------------------------------------------------------------------

class TestEnumerateTickers:
    def test_empty_base(self, tmp_path: Path):
        assert enumerate_tickers(tmp_path) == []

    def test_unions_journal_and_ledger_sources(self, tmp_path: Path):
        # Journal-only ticker
        append_stage2_note(_journal_note("AAPL"), tmp_path)
        # Ledger-only ticker
        _seed_ledger(
            tmp_path, "MSFT", "20260601T120000-msft01",
            "2026-06-01T12:00:00+00:00",
        )
        # Both journal and ledger
        append_stage2_note(_journal_note("GOOGL"), tmp_path)
        _seed_ledger(
            tmp_path, "GOOGL", "20260601T120000-googl1",
            "2026-06-01T12:00:00+00:00",
        )

        tickers = enumerate_tickers(tmp_path)
        assert tickers == ["AAPL", "GOOGL", "MSFT"]

    def test_skips_lock_files(self, tmp_path: Path):
        # The dossier system creates `.lock` files next to .md files
        # under per-symbol fcntl. They should NOT be enumerated as tickers.
        (tmp_path / "tickers").mkdir(parents=True)
        (tmp_path / "tickers" / "AAPL.md.lock").write_text("")
        assert enumerate_tickers(tmp_path) == []


# ---------------------------------------------------------------------------
# history_brief
# ---------------------------------------------------------------------------

class TestHistoryBrief:
    def test_filters_to_brief_source(self, tmp_path: Path):
        append_stage2_note(_journal_note("AAPL", composite=0.42), tmp_path)
        _seed_ledger(
            tmp_path, "AAPL", "20260601T120000-rsrc01",
            "2026-06-01T12:00:00+00:00",
        )

        entries = history_brief("AAPL", tmp_path)
        assert len(entries) == 1
        assert entries[0].source == "brief"

    def test_since_filter_applied(self, tmp_path: Path):
        # Today's journal entry survives `--since 1d`
        append_stage2_note(_journal_note("AAPL"), tmp_path)
        entries = history_brief("AAPL", tmp_path, since="1d")
        assert len(entries) == 1

    def test_empty_returns_empty_list(self, tmp_path: Path):
        assert history_brief("NOPE", tmp_path) == []


# ---------------------------------------------------------------------------
# build_cohort_grid
# ---------------------------------------------------------------------------

class TestCohortGrid:
    def test_two_tickers_two_dates(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "MU", "20260601T120000-aaaaa1",
            "2026-06-01T12:00:00+00:00", verdict_word="CHALLENGE",
        )
        _seed_trace(tmp_path / "traces", "20260601T120000-aaaaa1", pre=0.48, post=0.22)
        _seed_ledger(
            tmp_path, "MRVL", "20260602T120000-bbbbb1",
            "2026-06-02T12:00:00+00:00", verdict_word="WEAKEN",
        )
        _seed_trace(tmp_path / "traces", "20260602T120000-bbbbb1", pre=0.50, post=0.42)

        grid = build_cohort_grid(["MU", "MRVL"], tmp_path)

        assert grid.tickers == ("MU", "MRVL")
        # Ticker order in columns preserves caller order
        assert "MU" in grid.rows[grid.dates[0]]
        # Verify cell content for MU on its date
        mu_entry = grid.rows["2026-06-01"]["MU"]
        assert mu_entry is not None
        assert mu_entry.skeptic_verdict == "CHALLENGE"
        # MRVL has nothing on the MU date
        assert grid.rows["2026-06-01"]["MRVL"] is None

    def test_same_day_multiple_reads_picks_latest_recorded_at(self, tmp_path: Path):
        # Two reads on the same day; the later one (by recorded_at) wins
        # the cell. Reflects what the operator most likely cares about.
        _seed_ledger(
            tmp_path, "MU", "20260601T100000-aaaa10",
            "2026-06-01T10:00:00+00:00", verdict_word="WEAKEN",
        )
        # Second chain on same day, written later
        d2 = Dossier(symbol="MU", ledger=[])
        # Recover existing ledger first then append (per-symbol lock not
        # critical here since tests are single-threaded)
        from research_assistant.dossier_io import read_dossier
        existing = read_dossier("MU", tmp_path)
        assert existing is not None
        existing.ledger.append(LedgerEntry(
            timestamp="2026-06-01T14:00:00+00:00", kind="thesis",
            summary="late thesis", evidence_anchor="20260601T140000-aaaa20",
        ))
        existing.ledger.append(LedgerEntry(
            timestamp="2026-06-01T14:00:00+00:00", kind="skeptic",
            summary="Verdict: CHALLENGE. late critique",
            evidence_anchor="20260601T140000-aaaa20",
        ))
        write_dossier_atomic(existing, tmp_path)

        grid = build_cohort_grid(["MU"], tmp_path)
        cell = grid.rows["2026-06-01"]["MU"]
        assert cell is not None
        # The later 14:00 read wins the cell
        assert cell.skeptic_verdict == "CHALLENGE"

    def test_empty_cohort_renders_no_history(self, tmp_path: Path):
        grid = build_cohort_grid(["NOPE"], tmp_path)
        out = render_cohort_grid(grid)
        assert "no history" in out


# ---------------------------------------------------------------------------
# filter_verdicts (cross-ticker scan)
# ---------------------------------------------------------------------------

class TestFilterVerdicts:
    def test_unfiltered_returns_all_history(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "MU", "20260601T120000-aaaaa1",
            "2026-06-01T12:00:00+00:00", verdict_word="CHALLENGE",
        )
        _seed_ledger(
            tmp_path, "MRVL", "20260602T120000-bbbbb1",
            "2026-06-02T12:00:00+00:00", verdict_word="WEAKEN",
        )

        entries = filter_verdicts(tmp_path)
        assert len(entries) == 2
        assert {e.ticker for e in entries} == {"MU", "MRVL"}

    def test_verdict_filter(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "MU", "20260601T120000-aaaaa1",
            "2026-06-01T12:00:00+00:00", verdict_word="CHALLENGE",
        )
        _seed_ledger(
            tmp_path, "MRVL", "20260602T120000-bbbbb1",
            "2026-06-02T12:00:00+00:00", verdict_word="WEAKEN",
        )

        challenge_only = filter_verdicts(tmp_path, verdicts={"CHALLENGE"})
        assert len(challenge_only) == 1
        assert challenge_only[0].ticker == "MU"

    def test_ticker_filter(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "MU", "20260601T120000-aaaaa1",
            "2026-06-01T12:00:00+00:00",
        )
        _seed_ledger(
            tmp_path, "MRVL", "20260602T120000-bbbbb1",
            "2026-06-02T12:00:00+00:00",
        )

        mu_only = filter_verdicts(tmp_path, tickers=["MU"])
        assert len(mu_only) == 1
        assert mu_only[0].ticker == "MU"

    def test_sort_order_is_chronological(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "MU", "20260603T120000-aaaaa1",
            "2026-06-03T12:00:00+00:00",
        )
        _seed_ledger(
            tmp_path, "MRVL", "20260601T120000-bbbbb1",
            "2026-06-01T12:00:00+00:00",
        )

        entries = filter_verdicts(tmp_path)
        assert [e.asof for e in entries] == ["2026-06-01", "2026-06-03"]


# ---------------------------------------------------------------------------
# Renderers (smoke tests — verify they don't crash and include key content)
# ---------------------------------------------------------------------------

class TestRenderers:
    def test_render_history_brief_includes_header_and_rows(self, tmp_path: Path):
        append_stage2_note(_journal_note("AAPL", verdict="WEAKEN"), tmp_path)
        entries = history_brief("AAPL", tmp_path)
        out = render_history_brief("AAPL", entries)
        assert "AAPL — brief history" in out
        assert "WEAKEN" in out

    def test_render_cohort_grid_includes_tickers_and_dates(self, tmp_path: Path):
        _seed_ledger(
            tmp_path, "MU", "20260601T120000-aaaaa1",
            "2026-06-01T12:00:00+00:00", verdict_word="CHALLENGE",
        )
        grid = build_cohort_grid(["MU"], tmp_path)
        out = render_cohort_grid(grid)
        assert "MU" in out
        assert "2026-06-01" in out
        assert "CHAL" in out  # Verdict abbreviation

    def test_render_verdicts_table_handles_empty(self):
        out = render_verdicts_table([])
        assert "no matching entries" in out
