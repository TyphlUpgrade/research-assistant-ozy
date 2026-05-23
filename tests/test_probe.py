"""
Tests for the `/probe` skill (FOLLOWUPS #2): focused dossier-scoped query.

Covers:
- Missing dossier raises FileNotFoundError (probe is cold-start against a
  SAVED dossier, not a way to create one)
- Successful probe appends a kind="probe" ledger entry citing the chain_id
- Successful probe appends a kind="probe" observation row
- closes_questions drops verbatim Open Questions from the dossier
- new_open_questions appends to the dossier's Open Questions
- `deep=True` runs Stage 3 Skeptic and populates critique_text
- `deep=False` (default) does NOT run Skeptic
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    read_dossier,
    write_dossier_atomic,
)
from research_assistant.observations import read_observations
from research_assistant.orchestrator import _render, probe_ticker


def _seed_dossier(base: Path, symbol: str = "IONQ") -> Dossier:
    """Write a minimal dossier so /probe has something to read."""
    d = Dossier(
        symbol=symbol,
        conviction=0.42,
        state_md="Mock state for tests.",
        open_questions=[
            "What is IonQ's revenue run-rate?",
            "When is the next earnings date?",
        ],
        ledger=[
            LedgerEntry(
                timestamp="2026-05-22T19:00:00+00:00",
                kind="thesis",
                summary="seed thesis",
                evidence_anchor="20260522T190000-aaaaaa",
            ),
        ],
    )
    write_dossier_atomic(d, base)
    return d


def _fake_probe_response(
    *,
    answer: str = "Mock answer.",
    anchors: list[dict] | None = None,
    closes: list[str] | None = None,
    new_qs: list[str] | None = None,
):
    parsed = {
        "ticker": "IONQ",
        "answer": answer,
        "evidence_anchors": anchors or [
            {"claim": "Mock answer.", "source": "TICKER_DATA:daily_signals"},
        ],
        "closes_questions": closes or [],
        "new_open_questions": new_qs or [],
    }

    async def fake(client, ws, td, h, dc, q, insider_activity=None, institutional_ownership=None, filing_excerpts=None):
        return parsed, None

    return fake


@pytest.mark.asyncio
async def test_probe_raises_when_no_dossier(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No dossier found for IONQ"):
        await probe_ticker(
            "IONQ",
            "test question",
            world_state={"regime": "bull-trending"},
            ticker_data={"price": 30.0},
            headlines=[],
            base=tmp_path,
        )


@pytest.mark.asyncio
async def test_probe_appends_ledger_entry(tmp_path: Path) -> None:
    _seed_dossier(tmp_path)
    fake = _fake_probe_response(answer="The Trump quantum policy is the catalyst.")
    with patch("research_assistant.orchestrator._stage_2_probe", fake):
        result = await probe_ticker(
            "IONQ",
            "What is the catalyst?",
            world_state={"regime": "bull-trending"},
            ticker_data={"price": 30.0},
            headlines=[],
            base=tmp_path,
        )

    updated = read_dossier("IONQ", tmp_path)
    assert updated is not None
    probe_entries = [e for e in updated.ledger if e.kind == "probe"]
    assert len(probe_entries) == 1
    assert probe_entries[0].evidence_anchor == result.chain_id
    assert "What is the catalyst?" in probe_entries[0].summary
    assert "Trump quantum policy" in probe_entries[0].summary


@pytest.mark.asyncio
async def test_probe_appends_observation(tmp_path: Path) -> None:
    _seed_dossier(tmp_path)
    fake = _fake_probe_response(answer="Answer A.")
    with patch("research_assistant.orchestrator._stage_2_probe", fake):
        result = await probe_ticker(
            "IONQ",
            "Q",
            world_state={"regime": "bull-trending"},
            ticker_data={"price": 30.0},
            headlines=[],
            base=tmp_path,
        )

    obs = read_observations("IONQ", tmp_path)
    assert len(obs) == 1
    assert obs[0].kind == "probe"
    assert obs[0].symbol == "IONQ"
    assert obs[0].chain_id == result.chain_id
    assert obs[0].thesis == "Answer A."
    assert obs[0].regime == "bull-trending"


@pytest.mark.asyncio
async def test_probe_closes_open_questions(tmp_path: Path) -> None:
    _seed_dossier(tmp_path)
    fake = _fake_probe_response(
        answer="Revenue is $40M / quarter per the latest filing.",
        closes=["What is IonQ's revenue run-rate?"],
    )
    with patch("research_assistant.orchestrator._stage_2_probe", fake):
        await probe_ticker(
            "IONQ",
            "Revenue?",
            world_state={},
            ticker_data={"price": 30.0},
            headlines=[],
            base=tmp_path,
        )

    updated = read_dossier("IONQ", tmp_path)
    assert updated is not None
    assert "What is IonQ's revenue run-rate?" not in updated.open_questions
    assert "When is the next earnings date?" in updated.open_questions  # unchanged


@pytest.mark.asyncio
async def test_probe_appends_new_open_questions(tmp_path: Path) -> None:
    _seed_dossier(tmp_path)
    fake = _fake_probe_response(
        answer="Partial answer; data gap remains.",
        new_qs=["What is the exact CHIPS Act allocation for IonQ?"],
    )
    with patch("research_assistant.orchestrator._stage_2_probe", fake):
        await probe_ticker(
            "IONQ",
            "Federal funding?",
            world_state={},
            ticker_data={"price": 30.0},
            headlines=[],
            base=tmp_path,
        )

    updated = read_dossier("IONQ", tmp_path)
    assert updated is not None
    assert "What is the exact CHIPS Act allocation for IonQ?" in updated.open_questions


@pytest.mark.asyncio
async def test_probe_does_not_call_skeptic(tmp_path: Path) -> None:
    """Per v1 scope (post-remediation 2026-05-22): /probe is thin-and-fast and
    NEVER invokes the Skeptic — for adversarial pressure, use /research."""
    _seed_dossier(tmp_path)
    fake = _fake_probe_response(answer="Probe answer.")
    skeptic_calls = []

    async def fake_skeptic(client, ws, twd, model="x"):
        skeptic_calls.append(twd)
        return {}, None

    with patch("research_assistant.orchestrator._stage_2_probe", fake), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_skeptic):
        await probe_ticker(
            "IONQ",
            "Q",
            world_state={},
            ticker_data={"price": 30.0},
            headlines=[],
            base=tmp_path,
        )

    assert skeptic_calls == []


def test_render_does_not_recursively_substitute() -> None:
    """Persistent dossier content (e.g. an Open Question that happens to
    contain `{focused_question}`) must NOT splice into the next probe's
    prompt. Pins the post-remediation single-pass regex behavior."""
    out = _render(
        "Q: {focused_question}\nDOSSIER:\n{dossier_context}",
        dossier_context="Open Q: 'what about {focused_question}?'",
        focused_question="this turn's question",
    )
    # The `{focused_question}` inside dossier_context is preserved verbatim,
    # not re-interpolated to the actual question text.
    assert "Open Q: 'what about {focused_question}?'" in out
    assert "Q: this turn's question" in out


def test_render_preserves_json_examples() -> None:
    """JSON schema blocks in prompt bodies (e.g. `{"k": "v"}`) must NOT
    be touched unless the key is an actual sub. Backward-compat regression
    guard for the str.replace → regex refactor."""
    template = 'OUTPUT JSON: { "ticker": "<symbol>", "answer": "..." }'
    out = _render(template, ticker_json='unused')
    assert out == template
