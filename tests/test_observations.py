"""
Tests for the per-ticker observations stream (FOLLOWUPS #1, write phase).

Covers:
- append creates the ticker directory if missing
- multiple appends accumulate (no overwrite)
- read_observations round-trips the schema oldest-first
- limit param returns the most-recent N
- read on a fresh base returns []
- invalid kind raises ValueError
- unknown JSON keys are ignored on read (forward-compat)
- orchestrator.research_ticker emits a kind="research" event after dossier write
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_assistant.observations import (
    Observation,
    append_observation,
    now_iso,
    read_observations,
)
from research_assistant.orchestrator import research_ticker


def _obs(symbol: str = "NVDA", **kw) -> Observation:
    defaults = dict(
        ts=now_iso(),
        kind="research",
        symbol=symbol,
        chain_id="20260101T000000-abc123",
        thesis="thesis",
        conviction=0.5,
        regime="bull-trending",
        drivers=["d1"],
        risks=["r1"],
        flagged_risks=["fr1"],
        open_questions=["q1"],
        anchors=["a1"],
    )
    defaults.update(kw)
    return Observation(**defaults)


def test_append_creates_ticker_dir(tmp_path: Path) -> None:
    append_observation(_obs(), tmp_path)
    expected = tmp_path / "tickers" / "NVDA" / "observations.jsonl"
    assert expected.exists()
    assert expected.read_text().count("\n") == 1


def test_appends_accumulate(tmp_path: Path) -> None:
    append_observation(_obs(thesis="t1"), tmp_path)
    append_observation(_obs(thesis="t2"), tmp_path)
    append_observation(_obs(thesis="t3"), tmp_path)
    events = read_observations("NVDA", tmp_path)
    assert [e.thesis for e in events] == ["t1", "t2", "t3"]


def test_round_trip_preserves_fields(tmp_path: Path) -> None:
    original = _obs(
        thesis="full thesis text",
        drivers=["d1", "d2"],
        risks=["r1"],
        flagged_risks=["fr1", "fr2"],
        open_questions=["q1", "q2", "q3"],
        anchors=["a1"],
        conviction=0.42,
    )
    append_observation(original, tmp_path)
    [loaded] = read_observations("NVDA", tmp_path)
    assert loaded == original


def test_limit_returns_most_recent(tmp_path: Path) -> None:
    for i in range(5):
        append_observation(_obs(thesis=f"t{i}"), tmp_path)
    recent = read_observations("NVDA", tmp_path, limit=2)
    assert [e.thesis for e in recent] == ["t3", "t4"]


def test_limit_larger_than_history_returns_all(tmp_path: Path) -> None:
    append_observation(_obs(thesis="only"), tmp_path)
    events = read_observations("NVDA", tmp_path, limit=10)
    assert len(events) == 1


def test_read_on_fresh_base_returns_empty(tmp_path: Path) -> None:
    assert read_observations("NVDA", tmp_path) == []


def test_invalid_kind_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown observation kind"):
        append_observation(_obs(kind="bogus"), tmp_path)


def test_unknown_keys_ignored_on_read(tmp_path: Path) -> None:
    # Simulate a future schema version writing an event with extra keys.
    path = tmp_path / "tickers" / "NVDA" / "observations.jsonl"
    path.parent.mkdir(parents=True)
    payload = {
        "ts": now_iso(),
        "kind": "research",
        "symbol": "NVDA",
        "chain_id": "x",
        "thesis": "t",
        "conviction": 0.5,
        "regime": None,
        "drivers": [],
        "risks": [],
        "flagged_risks": [],
        "open_questions": [],
        "anchors": [],
        "future_field_we_dont_know_about": {"nested": True},
    }
    path.write_text(json.dumps(payload) + "\n")
    [loaded] = read_observations("NVDA", tmp_path)
    assert loaded.thesis == "t"


def test_separate_tickers_have_separate_files(tmp_path: Path) -> None:
    append_observation(_obs(symbol="NVDA", thesis="nvda thesis"), tmp_path)
    append_observation(_obs(symbol="IONQ", thesis="ionq thesis"), tmp_path)
    nvda = read_observations("NVDA", tmp_path)
    ionq = read_observations("IONQ", tmp_path)
    assert [e.thesis for e in nvda] == ["nvda thesis"]
    assert [e.thesis for e in ionq] == ["ionq thesis"]


@pytest.mark.asyncio
async def test_research_ticker_emits_research_observation(tmp_path: Path) -> None:
    """research_ticker must append a kind="research" event after the dossier
    write — pins FOLLOWUPS #1 brief→/research integration."""
    async def fake_stage_2(client, ws, td, s1, h):
        return {
            "ticker": "AAPL",
            "thesis_text": "Mocked Stage 2 thesis.",
            "conviction_score": 0.6,
            "key_drivers": ["services growth"],
            "risks": ["regulatory"],
            "open_questions": ["margin trajectory?"],
            "evidence_anchors": [{"claim": "services growth", "source": "tc_x"}],
        }, None

    async def fake_stage_3(client, ws, twd, model="x"):
        return {
            "ticker": "AAPL",
            "critique_text": "Mocked critique.",
            "adjusted_score": 0.48,
            "flagged_risks": ["valuation regime"],
            "open_questions_added": ["forward multiple?"],
            "news_reactivity_flag": False,
        }, None

    with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3):
        await research_ticker(
            "AAPL",
            world_state={"regime": "bull-trending"},
            ticker_data={"price": 200.0},
            headlines=[],
            base=tmp_path,
        )

    events = read_observations("AAPL", tmp_path)
    assert len(events) == 1
    [obs] = events
    assert obs.kind == "research"
    assert obs.symbol == "AAPL"
    assert obs.thesis == "Mocked Stage 2 thesis."
    assert obs.conviction == pytest.approx(0.48)
    assert obs.regime == "bull-trending"
    assert obs.drivers == ["services growth"]
    assert obs.flagged_risks == ["valuation regime"]
    assert "margin trajectory?" in obs.open_questions
    assert "forward multiple?" in obs.open_questions
    assert obs.anchors == [{"claim": "services growth", "source": "tc_x"}]
