"""
Tests for trace_renderer.py — visibility-axis surface.

Critical test: visibility regression — a claim in `key_drivers` that has
no matching `evidence_anchors` entry must be flagged with the
[NO ANCHOR — visibility regression] marker.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_assistant.trace_renderer import (
    append_stage_event,
    render_trace,
    _format_stage_event,
)


def _make_event(parsed: dict, chain_id: str = "test_chain_1") -> dict:
    return {
        "stage_id": "stage_2_thesis",
        "chain_id": chain_id,
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-05-14T14:30:22Z",
        "tokens_in": 4000,
        "tokens_out": 400,
        "cost_usd": 0.018,
        "latency_ms": 2300,
        "raw_response_truncated": "{...}",
        "parsed": parsed,
        "error": None,
    }


def test_well_anchored_claims_render_clean() -> None:
    event = _make_event({
        "thesis_text": "NVDA bullish on DC growth.",
        "conviction_score": 0.72,
        "key_drivers": ["Data-center revenue +27% QoQ"],
        "risks": ["China export restrictions"],
        "evidence_anchors": [
            {"claim": "Data-center revenue +27% QoQ", "source": "tool_call_nv001"},
            {"claim": "China export restrictions", "source": "tool_call_nv003"},
        ],
    })
    rendered = _format_stage_event(event)
    assert "[NO ANCHOR" not in rendered, "Well-anchored event should have no visibility regressions"
    assert "tool_call_nv001" in rendered
    assert "tool_call_nv003" in rendered


def test_unanchored_driver_flagged_as_visibility_regression() -> None:
    """
    Visibility regression test: a driver claim that has no anchor entry
    MUST be surfaced with [NO ANCHOR — visibility regression].
    """
    event = _make_event({
        "thesis_text": "TSLA bullish.",
        "conviction_score": 0.6,
        "key_drivers": [
            "Cybertruck production ramping",
            "Some unverified analyst speculation",  # NO matching anchor!
        ],
        "risks": [],
        "evidence_anchors": [
            {"claim": "Cybertruck production ramping", "source": "tool_call_xy123"},
        ],
    })
    rendered = _format_stage_event(event)
    assert "[NO ANCHOR — visibility regression]" in rendered
    assert "Some unverified analyst speculation" in rendered
    # The anchored driver should NOT be flagged
    assert rendered.count("[NO ANCHOR") == 1


def test_unanchored_risk_also_flagged() -> None:
    event = _make_event({
        "thesis_text": "Test.",
        "conviction_score": 0.5,
        "key_drivers": [],
        "risks": ["Generic macro risk"],  # no anchor
        "evidence_anchors": [],
    })
    rendered = _format_stage_event(event)
    assert "[NO ANCHOR" in rendered
    assert "Generic macro risk" in rendered


def test_empty_source_string_treated_as_no_anchor() -> None:
    """A claim with source = '' is as bad as a missing anchor."""
    event = _make_event({
        "thesis_text": "X",
        "conviction_score": 0.5,
        "key_drivers": ["A claim"],
        "risks": [],
        "evidence_anchors": [{"claim": "A claim", "source": ""}],
    })
    rendered = _format_stage_event(event)
    assert "[NO ANCHOR" in rendered


def test_error_event_renders_error_only(tmp_path: Path) -> None:
    event = {
        "stage_id": "stage_3_skeptic",
        "chain_id": "test_chain_err",
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-05-14T14:31:00Z",
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_ms": 1500,
        "raw_response_truncated": "", "parsed": None,
        "error": "JSONDecodeError: Expecting value at line 3 col 5",
    }
    rendered = _format_stage_event(event)
    assert "ERROR" in rendered
    assert "JSONDecodeError" in rendered


def test_append_and_render_roundtrip(tmp_path: Path) -> None:
    """append_stage_event writes JSONL; render_trace reads it back."""
    chain = "20260514T143022-abc123"
    append_stage_event(
        chain_id=chain,
        stage_id="stage_2_thesis",
        model="claude-sonnet-4-6",
        tokens_in=5000, tokens_out=500, cost_usd=0.02, latency_ms=2400,
        parsed={
            "thesis_text": "Test thesis",
            "conviction_score": 0.6,
            "key_drivers": ["d1"],
            "risks": [],
            "evidence_anchors": [{"claim": "d1", "source": "tc1"}],
        },
        raw_response="{...}",
        traces_base=tmp_path,
    )
    rendered = render_trace(chain, tmp_path)
    assert chain in rendered
    assert "stage_2_thesis" in rendered
    assert "tc1" in rendered
    assert "[NO ANCHOR" not in rendered


def test_render_trace_missing_chain_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        render_trace("nonexistent_chain", tmp_path)
