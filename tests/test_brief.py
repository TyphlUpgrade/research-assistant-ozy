"""
Tests for brief.py — watchlist loading + render functions (offline-testable).

Builder integration test is end-to-end and requires API; covered separately
in the quality-contract regression suite (Phase 7) with mocked Anthropic.
"""
from __future__ import annotations

from pathlib import Path

from research_assistant.brief import (
    Brief,
    BriefItem,
    load_watchlist,
    render_brief_drill_down,
    render_brief_top_level,
)
from research_assistant.orchestrator import Stage2Note, compute_composite_conviction


# ---------------------------------------------------------------------------
# Watchlist loader
# ---------------------------------------------------------------------------

def test_load_watchlist_normalizes_and_skips_comments(tmp_path: Path) -> None:
    wl = tmp_path / "watchlist.txt"
    wl.write_text(
        "# Mega-cap tech\n"
        "AAPL\n"
        "  nvda  \n"           # whitespace + lowercase → trimmed + uppercased
        "\n"                   # blank line skipped
        "# comment in the middle\n"
        "tsla\n"
    )
    tickers = load_watchlist(tmp_path)
    assert tickers == ["AAPL", "NVDA", "TSLA"]


def test_load_watchlist_missing_returns_empty(tmp_path: Path) -> None:
    tickers = load_watchlist(tmp_path)
    assert tickers == []


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------

def _make_note(
    ticker: str,
    *,
    observation: tuple[str, ...],
    bull_anchor: str,
    bear_anchor: str,
    what_would_change: tuple[str, ...],
    conviction: dict[str, float],
    decision_tag: str,
) -> Stage2Note:
    return Stage2Note(
        ticker=ticker,
        observation=observation,
        bull_anchor=bull_anchor,
        bear_anchor=bear_anchor,
        what_would_change=what_would_change,
        conviction=conviction,
        composite_conviction=compute_composite_conviction(conviction),
        decision_tag=decision_tag,
    )


def _sample_brief() -> Brief:
    nvda_note = _make_note(
        "NVDA",
        observation=(
            "NVDA up 27% on 30d basis with weekly RSI 65",
            "Form 4 net flow last 90d: -$3.5M / 1 sale / 0 buys",
        ),
        bull_anchor="DC revenue +27% QoQ aligns with bull-trending regime",
        bear_anchor="Weekly RSI 65 + recent 5d return +12% — chase risk",
        what_would_change=(
            "RSI breaks below 55 on weekly close",
            "Form 4 cluster buy: ≥2 distinct buyers in next 30d",
            "Q4 earnings miss revenue by 5%+",
        ),
        conviction={
            "technical": 0.55, "fundamental": 0.70, "catalyst": 0.40, "regime": 0.75,
        },
        decision_tag="WATCH",
    )
    meta_note = _make_note(
        "META",
        observation=("META at 5-day high; volume ratio 1.4x average",),
        bull_anchor="Reels ad revenue mix shifting positive",
        bear_anchor="EU DMA enforcement risk near-term",
        what_would_change=(
            "Reality Labs losses decelerate >10% YoY in Q3",
            "EU DMA fine announced >$2B",
        ),
        conviction={
            "technical": 0.60, "fundamental": 0.55, "catalyst": 0.50, "regime": 0.65,
        },
        decision_tag="PROBE",
    )
    return Brief(
        date_et="2026-05-14",
        chain_id="20260514T093000-deadbeef",
        world_state={
            "regime": "bull-trending",
            "regime_confidence": 0.72,
            "dispersion": 0.41,
            "rationale": "Tech leadership broadening into industrials.",
            "macro_signals": {
                "vix_level": 14.2,
                "vix_trend": "flat",
                "active_catalysts": ["FOMC Wed"],
            },
        },
        items=[
            BriefItem(
                ticker="NVDA",
                intrinsic_score=0.82,
                stage_1_reason="DC growth + bull regime aligned",
                stage_2_note=nvda_note,
                conviction_score=nvda_note.composite_conviction,
                chain_id="20260514T093000-deadbeef",
            ),
            BriefItem(
                ticker="META",
                intrinsic_score=0.65,
                stage_1_reason="Reels monetization signal",
                stage_2_note=meta_note,
                conviction_score=meta_note.composite_conviction,
                chain_id="20260514T093000-deadbeef",
            ),
        ],
        cost_usd=0.42,
    )


def test_top_level_render_includes_regime_and_items() -> None:
    brief = _sample_brief()
    rendered = render_brief_top_level(brief)
    assert "Morning Brief — 2026-05-14 (ET)" in rendered
    assert "bull-trending" in rendered
    assert "NVDA" in rendered
    assert "META" in rendered
    # Composite conviction for NVDA = geomean(0.55,0.70,0.40,0.75) ≈ 0.583
    nvda_note = brief.items[0].stage_2_note
    assert nvda_note is not None
    assert f"conviction {nvda_note.composite_conviction:.2f}" in rendered
    assert "WATCH" in rendered
    assert "PROBE" in rendered
    assert "FOMC" in rendered
    assert "20260514T093000-deadbeef" in rendered


def test_top_level_render_surfaces_bull_and_bear_anchors() -> None:
    """PR 2A.2: anchors visible in the top-level scan (not just drill-down)."""
    brief = _sample_brief()
    rendered = render_brief_top_level(brief)
    assert "DC revenue +27% QoQ aligns with bull-trending regime" in rendered
    assert "Weekly RSI 65 + recent 5d return +12% — chase risk" in rendered


def test_top_level_render_includes_what_would_change_triggers() -> None:
    brief = _sample_brief()
    rendered = render_brief_top_level(brief)
    assert "RSI breaks below 55 on weekly close" in rendered
    assert "Form 4 cluster buy: ≥2 distinct buyers in next 30d" in rendered


def test_drill_down_shows_observation_and_conviction_breakdown() -> None:
    brief = _sample_brief()
    rendered = render_brief_drill_down(brief, "NVDA")
    assert "NVDA" in rendered
    assert "**Decision:** WATCH" in rendered
    assert "Tech 0.55" in rendered
    assert "Fund 0.70" in rendered
    assert "Catalyst 0.40" in rendered
    assert "Regime 0.75" in rendered
    assert "DC revenue +27% QoQ aligns with bull-trending regime" in rendered
    assert "Form 4 net flow last 90d" in rendered


def test_drill_down_unknown_ticker_friendly_error() -> None:
    brief = _sample_brief()
    rendered = render_brief_drill_down(brief, "XYZ")
    assert "No brief item for XYZ" in rendered
    assert "['NVDA', 'META']" in rendered


def test_drill_down_lowercase_input() -> None:
    """User typed `/brief nvda` — should still work."""
    brief = _sample_brief()
    rendered = render_brief_drill_down(brief, "nvda")
    assert "NVDA" in rendered
    assert "**Decision:** WATCH" in rendered


def test_drill_down_no_stage_2_note_degrades_gracefully() -> None:
    """An item without a Stage2Note (e.g. parse failure) renders the Stage 1 reason
    instead of crashing."""
    brief = _sample_brief()
    brief.items[0].stage_2_note = None
    brief.items[0].conviction_score = None
    rendered = render_brief_drill_down(brief, "NVDA")
    assert "Stage 2 note not generated" in rendered
    assert "DC growth + bull regime aligned" in rendered
