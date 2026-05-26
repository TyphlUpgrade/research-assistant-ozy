"""
Tests for the append-only Stage 2 note journal (PR 2A.4).

Covers:
- `append_stage2_note` creates the per-ticker file on first write
- `append_stage2_note` appends on subsequent writes (no dedup)
- `read_stage2_history` returns most-recent first
- `read_stage2_history` respects limit
- `read_stage2_history` returns [] when no file exists
- `read_stage2_full_history` returns chronological (oldest-first)
- Module docstring carries the SCHEMA CONTRACT block
- Module docstring documents the single-writer assumption
"""
from __future__ import annotations

import json
from pathlib import Path

from research_assistant.journal import (
    append_stage2_note,
    read_stage2_full_history,
    read_stage2_history,
)
from research_assistant.journal import stage2_notes as stage2_mod
from research_assistant.orchestrator import (
    Stage2Note,
    compute_composite_conviction,
)


def _note(
    ticker: str = "NVDA",
    bull_anchor: str = "Trend acceleration on AI capex tailwind",
    bear_anchor: str = "Weekly RSI 65 + 5d return +12% — chase risk",
    technical: float = 0.55,
    fundamental: float = 0.70,
    catalyst: float = 0.40,
    regime: float = 0.75,
    decision_tag: str = "WATCH",
    skeptic_verdict: str = "AGREE",
) -> Stage2Note:
    conviction = {
        "technical": technical,
        "fundamental": fundamental,
        "catalyst": catalyst,
        "regime": regime,
    }
    return Stage2Note(
        ticker=ticker,
        observation=(),
        bull_anchor=bull_anchor,
        bear_anchor=bear_anchor,
        what_would_change=(),
        conviction=conviction,
        composite_conviction=compute_composite_conviction(conviction),
        decision_tag=decision_tag,
        skeptic_verdict=skeptic_verdict,
    )


def test_append_stage2_note_creates_file(tmp_path: Path) -> None:
    note = _note()
    append_stage2_note(note, tmp_path)

    path = tmp_path / "stage2" / "NVDA.jsonl"
    assert path.exists()
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["ticker"] == "NVDA"
    assert row["bull_anchor"] == note.bull_anchor
    assert row["bear_anchor"] == note.bear_anchor
    assert row["decision_tag"] == "WATCH"
    assert row["skeptic_verdict"] == "AGREE"
    assert row["composite_conviction"] == note.composite_conviction
    assert row["schema_version"] == 1


def test_append_stage2_note_appends_subsequent_writes(tmp_path: Path) -> None:
    """No dedup at write time — operator may re-run brief multiple times
    per day with different Stage 2 outputs."""
    append_stage2_note(_note(bull_anchor="first read"), tmp_path)
    append_stage2_note(_note(bull_anchor="second read"), tmp_path)

    path = tmp_path / "stage2" / "NVDA.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    rows = [json.loads(ln) for ln in lines]
    assert rows[0]["bull_anchor"] == "first read"
    assert rows[1]["bull_anchor"] == "second read"


def test_read_stage2_history_returns_most_recent_first(tmp_path: Path) -> None:
    append_stage2_note(_note(bull_anchor="oldest"), tmp_path)
    append_stage2_note(_note(bull_anchor="middle"), tmp_path)
    append_stage2_note(_note(bull_anchor="newest"), tmp_path)

    history = read_stage2_history("NVDA", tmp_path, limit=3)
    assert len(history) == 3
    assert [h["bull_anchor"] for h in history] == ["newest", "middle", "oldest"]


def test_read_stage2_history_respects_limit(tmp_path: Path) -> None:
    for i in range(10):
        append_stage2_note(_note(bull_anchor=f"read-{i}"), tmp_path)

    history = read_stage2_history("NVDA", tmp_path, limit=5)
    assert len(history) == 5
    # Most-recent first: read-9 .. read-5
    assert [h["bull_anchor"] for h in history] == [
        "read-9", "read-8", "read-7", "read-6", "read-5",
    ]


def test_read_stage2_history_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_stage2_history("DOESNOTEXIST", tmp_path, limit=5) == []


def test_read_stage2_full_history_returns_chronological(tmp_path: Path) -> None:
    append_stage2_note(_note(bull_anchor="first"), tmp_path)
    append_stage2_note(_note(bull_anchor="second"), tmp_path)
    append_stage2_note(_note(bull_anchor="third"), tmp_path)

    full = read_stage2_full_history("NVDA", tmp_path)
    assert [h["bull_anchor"] for h in full] == ["first", "second", "third"]


def test_schema_contract_docstring() -> None:
    assert stage2_mod.__doc__ is not None
    assert "SCHEMA CONTRACT" in stage2_mod.__doc__


def test_single_writer_documented() -> None:
    assert stage2_mod.__doc__ is not None
    assert "single-writer" in stage2_mod.__doc__
