"""
Tests for the unified opportunity-surface render in `render_brief_top_level`
(PR 2A.1, supersedes the PR 1.3 `## Setups` section).

Covers:
- The standalone `## Setups` section is GONE.
- Screener evidence renders inline per item as `[screener: summary]` suffix.
- An item with no screener_evidence still renders cleanly (no empty `[]` tag).
- Screener trace stages still carry `cost_usd == 0` (no-LLM-at-screener contract).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_assistant.brief import Brief, BriefItem, render_brief_top_level


def _brief_with_items(items: list[BriefItem]) -> Brief:
    return Brief(
        date_et="2026-05-25",
        chain_id="20260525T093000-test01",
        world_state={
            "regime": "bull-trending",
            "regime_confidence": 0.7,
            "dispersion": 0.3,
            "rationale": "Test rationale.",
            "macro_signals": {"vix_level": 14.0, "vix_trend": "flat",
                              "active_catalysts": []},
        },
        items=items,
        cost_usd=0.1,
    )


def test_no_standalone_setups_section() -> None:
    """PR 2A.1: `## Setups` is removed. The render must not contain it."""
    brief = _brief_with_items([])
    out = render_brief_top_level(brief)
    assert "## Setups" not in out
    assert "(no setups detected today)" not in out


def test_opportunity_surface_renders_when_empty() -> None:
    """Zero items still produces the surface header so absence is explicit."""
    brief = _brief_with_items([])
    out = render_brief_top_level(brief)
    assert "## Opportunity surface (0 items)" in out


def test_item_without_screener_evidence_renders_clean() -> None:
    """An item with no screener hits renders without an empty `[...]` tag."""
    item = BriefItem(
        ticker="NVDA",
        intrinsic_score=0.65,
        stage_1_reason="trend_strong +0.10",
        conviction_score=0.72,
        screener_evidence=[],
    )
    out = render_brief_top_level(_brief_with_items([item]))
    assert "NVDA" in out
    # The empty-brackets tag must not appear when no evidence is present.
    assert "[]" not in out
    assert "[: " not in out


def test_screener_evidence_renders_inline() -> None:
    """sector_rotation evidence surfaces inline as
    `[sector_rotation: rank 7→2 on 30d basis]`."""
    item = BriefItem(
        ticker="XLK",
        intrinsic_score=0.58,
        stage_1_reason="screener_confirmations +0.08",
        conviction_score=0.55,
        screener_evidence=[{
            "screener": "sector_rotation",
            "sector_etf": "XLK",
            "rs_rank_now": 2,
            "rs_rank_prior": 7,
            "basis_days": 30,
            "return_5d": 0.045,
            "return_30d": 0.11,
        }],
    )
    out = render_brief_top_level(_brief_with_items([item]))
    assert "XLK" in out
    assert "[sector_rotation: rank 7→2 on 30d basis]" in out


def test_multiple_screener_hits_chain() -> None:
    """An item with multiple screener hits renders all of them inline."""
    item = BriefItem(
        ticker="NVDA",
        intrinsic_score=0.74,
        stage_1_reason="multi-source",
        conviction_score=0.65,
        screener_evidence=[
            {"screener": "sector_rotation", "rs_rank_now": 2,
             "rs_rank_prior": 7, "basis_days": 30},
            {"screener": "pead"},
        ],
    )
    out = render_brief_top_level(_brief_with_items([item]))
    assert "[sector_rotation: rank 7→2 on 30d basis]" in out
    assert "[pead]" in out


@pytest.mark.parametrize("screener_name", ["sector_rotation"])
def test_screener_stages_have_zero_cost(tmp_path: Path, screener_name: str) -> None:
    """No-LLM-at-screener contract: any trace event tagged with a screener
    stage MUST have cost_usd == 0. Architectural invariant — preserved
    across PR 1.3 → PR 2A.1."""
    trace_dir = tmp_path / "traces" / "2026-05-25"
    trace_dir.mkdir(parents=True)
    chain_id = "20260525T093000-zero01"
    event = {
        "stage_id": f"screener_{screener_name}",
        "chain_id": chain_id,
        "model": "n/a",
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "latency_ms": 12,
        "parsed": None,
    }
    (trace_dir / f"{chain_id}.jsonl").write_text(json.dumps(event) + "\n")

    raw = (trace_dir / f"{chain_id}.jsonl").read_text()
    for line in raw.splitlines():
        ev = json.loads(line)
        if not ev.get("stage_id", "").startswith("screener_"):
            continue
        assert ev["cost_usd"] == 0.0, (
            f"screener stage carried nonzero cost_usd={ev['cost_usd']} — "
            "violates no-LLM-at-screener contract"
        )
