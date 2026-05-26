"""
Regression tests for PR 2A.6: screener-only tickers must surface on the
opportunity surface, not just in the alert journal.

Bug history: sector_rotation emitted XLV / XLU SetupCandidates that the
journal layer wrote to `.research/alerts/<date>.jsonl` faithfully, but the
brief render dropped them — `_stage_1_composite` only ranked tickers in
`watchlist_tickers_with_data`, and `_attach_screener_evidence` only updated
existing items. ETFs (which aren't in the watchlist) had no slot.

Coverage:
- `build_brief` appends a stub BriefItem for a screener-only ticker
  (cache-miss path)
- The stub item carries the screener evidence and renders it inline
- `_screener_only_stub_items` skips tickers that ARE already items
  (no double-counting when a screener fires on a watchlist ticker)
- `_append_cache_hit_screener_stubs` mirrors the same behaviour for the
  cli cache-hit path
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_assistant.brief import (
    Brief,
    BriefItem,
    _screener_only_stub_items,
    _synthesize_screener_ticker_data,
    build_brief,
    render_brief_top_level,
)
from research_assistant.screeners import SetupCandidate


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------

def test_synthesize_sector_etf_pulls_from_world_state() -> None:
    """`_synthesize_screener_ticker_data` for a sector ETF must hydrate
    return_5d / return_30d / price from `world_state["sector_performance"]`
    so the composite scorer has something to chew on (instead of all 0s).
    """
    world_state = {
        "sector_performance": {
            "XLV": {
                "symbol": "XLV",
                "return_5d": 0.033,
                "return_30d": 0.0175,
                "price": 149.88,
            },
        },
    }
    data = _synthesize_screener_ticker_data("XLV", world_state)
    assert data["symbol"] == "XLV"
    assert data["recent_return_5d"] == pytest.approx(0.033)
    assert data["return_30d"] == pytest.approx(0.0175)
    assert data["price"] == pytest.approx(149.88)


def test_synthesize_unknown_ticker_returns_symbol_only() -> None:
    """Non-sector-ETF screener-emitted tickers degrade to symbol-only —
    composite scoring still works via `_finite()` defaults (no crash)."""
    data = _synthesize_screener_ticker_data("XYZ", {"sector_performance": {}})
    assert data == {"symbol": "XYZ"}


def test_stub_items_skip_tickers_already_present() -> None:
    """When a screener fires on a ticker that's ALREADY a BriefItem (e.g.
    a watchlist stock that also hits sector_rotation by virtue of being
    XL-series — hypothetical), no stub is added for it. The existing
    item's `screener_evidence` is the source of truth, set upstream by
    `_stage_1_composite`."""
    existing = [
        BriefItem(
            ticker="NVDA",
            intrinsic_score=0.55,
            stage_1_reason="trend_strong +0.10",
            screener_evidence=[{"screener": "sector_rotation", "rs_rank_now": 2}],
        ),
    ]
    nvda_alert = SetupCandidate(
        ticker="NVDA",
        screener="sector_rotation",
        asof="2026-05-26",
        entry_price=500.0,
        evidence={"sector_etf": "NVDA", "rs_rank_now": 2, "rs_rank_prior": 7},
    )
    stubs = _screener_only_stub_items(
        screener_alerts=[nvda_alert],
        existing_items=existing,
        world_state={"regime": "bull-trending"},
        chain_id="test-chain",
    )
    assert stubs == [], "NVDA already exists as an item; no stub should be added"


def test_stub_items_inject_screener_only_ticker() -> None:
    """A sector ETF with a sector_rotation alert and no matching watchlist
    item must produce a stub BriefItem with the screener evidence inline.
    This is the direct regression for the original bug."""
    xlv_alert = SetupCandidate(
        ticker="XLV",
        screener="sector_rotation",
        asof="2026-05-26",
        entry_price=149.88,
        evidence={
            "sector_etf": "XLV",
            "rs_rank_now": 2,
            "rs_rank_prior": 7,
            "basis_days": 30,
            "return_5d": 0.033,
            "return_30d": 0.0175,
        },
    )
    world_state = {
        "regime": "bull-trending",
        "sector_performance": {
            "XLV": {
                "symbol": "XLV",
                "return_5d": 0.033,
                "return_30d": 0.0175,
                "price": 149.88,
            },
        },
    }
    stubs = _screener_only_stub_items(
        screener_alerts=[xlv_alert],
        existing_items=[],
        world_state=world_state,
        chain_id="test-chain",
    )
    assert len(stubs) == 1
    stub = stubs[0]
    assert stub.ticker == "XLV"
    assert stub.chain_id == "test-chain"
    assert stub.stage_2_note is None, "Stub items intentionally skip Stage 2"
    # Composite must give a non-zero, meaningful score (baseline 0.30 ×
    # bull-trending 1.10 + screener bonus 0.08 = ~0.41). The exact value
    # is brittle (depends on weight tuning), but it must lift off 0.
    assert stub.intrinsic_score > 0.30
    # Evidence dict carries `screener` key + the alert's evidence fields
    assert len(stub.screener_evidence) == 1
    ev = stub.screener_evidence[0]
    assert ev["screener"] == "sector_rotation"
    assert ev["rs_rank_now"] == 2
    assert ev["rs_rank_prior"] == 7


def test_stub_items_dedupe_multi_alert_same_ticker() -> None:
    """Two alerts on the same screener-only ticker collapse into a single
    BriefItem with both evidence dicts. Defensive — sector_rotation only
    emits one alert per ETF today, but future screeners could double-fire."""
    alerts = [
        SetupCandidate(
            ticker="XLU",
            screener="sector_rotation",
            asof="2026-05-26",
            entry_price=45.35,
            evidence={"sector_etf": "XLU", "rs_rank_now": 1, "rs_rank_prior": 11, "basis_days": 30},
        ),
        SetupCandidate(
            ticker="XLU",
            screener="pead",  # hypothetical second screener
            asof="2026-05-26",
            entry_price=45.35,
            evidence={"surprise_pct": 0.08},
        ),
    ]
    stubs = _screener_only_stub_items(
        screener_alerts=alerts,
        existing_items=[],
        world_state={"regime": "bull-trending"},
        chain_id="c",
    )
    assert len(stubs) == 1
    assert stubs[0].ticker == "XLU"
    screener_names = {ev["screener"] for ev in stubs[0].screener_evidence}
    assert screener_names == {"sector_rotation", "pead"}


# ---------------------------------------------------------------------------
# build_brief integration (mocks Stage 0 + Stage 2 so the test is offline)
# ---------------------------------------------------------------------------

class _StubClient:
    """Minimal ClaudeClient stand-in. Tests that exercise build_brief mock
    out _stage_0_world_state + _stage_2_for_survivor so the actual LLM is
    never called — but build_brief still reads `client.cost.total_usd` at
    the end for the cost field."""
    def __init__(self) -> None:
        self.cost = SimpleNamespace(total_usd=0.0)


def test_build_brief_surfaces_screener_only_ticker(
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end: a sector_rotation alert on XLV (not in the watchlist)
    must produce a BriefItem on the surface. Pre-PR-2A.6 this was the
    failure mode the operator caught — XLV journaled to alerts/<date>.jsonl
    but never reached the brief render.
    """
    world_state = {
        "regime": "bull-trending",
        "regime_confidence": 0.7,
        "dispersion": 0.3,
        "rationale": "Test bull regime.",
        "macro_signals": {"vix_level": 14.0, "vix_trend": "flat", "active_catalysts": []},
        "sector_performance": {
            "XLV": {"symbol": "XLV", "return_5d": 0.033, "return_30d": 0.0175, "price": 149.88},
        },
    }
    # One watchlist stock so the existing Stage 1 path stays exercised.
    watchlist_data = {
        "NVDA": {
            "symbol": "NVDA",
            "price": 500.0,
            "return_30d": 0.05,
            "recent_return_5d": 0.01,
            "weekly_rsi_14": 55.0,
            "volume_ratio": 1.1,
            "sector": "Technology",
        },
    }
    xlv_alert = SetupCandidate(
        ticker="XLV",
        screener="sector_rotation",
        asof="2026-05-26",
        entry_price=149.88,
        evidence={
            "sector_etf": "XLV", "rs_rank_now": 2, "rs_rank_prior": 7,
            "basis_days": 30, "return_5d": 0.033, "return_30d": 0.0175,
        },
    )

    async def _fake_stage_0(client, context):
        return world_state

    async def _fake_stage_2(*args, **kwargs):
        # Returning None still lets build_brief assemble a BriefItem for the
        # survivor — just without a Stage2Note. That's fine for this test:
        # we're verifying the XLV stub-injection path, not Stage 2 contents.
        return None

    monkeypatch.setattr(
        "research_assistant.brief._stage_0_world_state", _fake_stage_0,
    )
    monkeypatch.setattr(
        "research_assistant.brief._stage_2_for_survivor", _fake_stage_2,
    )

    brief = asyncio.run(build_brief(
        market_context={"foo": "bar"},
        universe=["NVDA"],
        watchlist_tickers_with_data=watchlist_data,
        headlines_per_ticker={"NVDA": []},
        research_base=tmp_path,
        client=_StubClient(),
        insider_activities={},
        screener_alerts=[xlv_alert],
    ))

    tickers = [item.ticker for item in brief.items]
    assert "XLV" in tickers, (
        f"XLV (screener-only) must surface as a BriefItem; got: {tickers}. "
        "Regression for the original PR 2A.6 bug — sector_rotation alerts "
        "were journaled but invisible in the brief."
    )
    xlv_item = next(i for i in brief.items if i.ticker == "XLV")
    assert xlv_item.stage_2_note is None
    assert any(
        ev.get("screener") == "sector_rotation"
        for ev in xlv_item.screener_evidence
    )

    rendered = render_brief_top_level(brief)
    assert "XLV" in rendered
    assert "[sector_rotation: rank 7→2 on 30d basis]" in rendered, (
        "Stub item must render the inline screener evidence tag so the "
        "operator sees the rotation flag without `/research`."
    )
