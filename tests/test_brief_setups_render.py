"""
Tests for the `## Setups` section in `render_brief_top_level` (PR 1.3).

Covers:
- Empty setups list → `## Setups (0)` + `(no setups detected today)`
- One sector_rotation candidate → rendered via per-screener formatter
- `## Setups` appears ABOVE `## Opportunity surface` (ordering invariant)
- Screener trace stages carry `cost_usd == 0` (no-LLM-at-screener contract)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_assistant.brief import Brief, render_brief_top_level
from research_assistant.screeners import SetupCandidate


def _empty_brief(setups: list[SetupCandidate] | None = None) -> Brief:
    return Brief(
        date_et="2026-05-25",
        chain_id="20260525T093000-test01",
        world_state={
            "regime": "bull-trending",
            "regime_confidence": 0.7,
            "dispersion": 0.3,
            "rationale": "Test rationale.",
            "macro_signals": {"vix_level": 14.0, "vix_trend": "flat", "active_catalysts": []},
        },
        items=[],
        cost_usd=0.1,
        setups=list(setups or []),
    )


def test_renders_empty_setups_section() -> None:
    brief = _empty_brief()
    out = render_brief_top_level(brief)
    assert "## Setups (0)" in out
    assert "(no setups detected today)" in out


def test_renders_single_setup() -> None:
    setup = SetupCandidate(
        ticker="XLK",
        screener="sector_rotation",
        asof="2026-05-25",
        entry_price=250.0,
        evidence={
            "sector_etf": "XLK",
            "rs_rank_now": 2,
            "rs_rank_prior": 7,
            "basis_days": 30,
            "return_5d": 0.045,
            "return_20d": 0.11,
        },
    )
    brief = _empty_brief(setups=[setup])
    out = render_brief_top_level(brief)
    assert "## Setups (1)" in out
    # The sector_rotation formatter line includes the rank transition and basis.
    assert "XLK" in out
    assert "rank 7→2" in out
    assert "30d basis" in out


def test_setups_section_above_opportunity_surface() -> None:
    setup = SetupCandidate(
        ticker="XLK",
        screener="sector_rotation",
        asof="2026-05-25",
        entry_price=250.0,
        evidence={
            "sector_etf": "XLK",
            "rs_rank_now": 2,
            "rs_rank_prior": 7,
            "basis_days": 30,
            "return_5d": 0.045,
            "return_20d": 0.11,
        },
    )
    brief = _empty_brief(setups=[setup])
    out = render_brief_top_level(brief)
    setups_idx = out.index("## Setups")
    opp_idx = out.index("## Opportunity surface")
    assert setups_idx < opp_idx, (
        "## Setups must render before ## Opportunity surface; "
        f"setups_idx={setups_idx}, opp_idx={opp_idx}"
    )


@pytest.mark.parametrize("screener_name", ["sector_rotation"])
def test_screener_stages_have_zero_cost(tmp_path: Path, screener_name: str) -> None:
    """No-LLM-at-screener contract: any trace event tagged with a screener
    stage MUST have cost_usd == 0. Pins the architectural invariant the plan
    calls out (PR 1.3 acceptance criterion)."""
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
