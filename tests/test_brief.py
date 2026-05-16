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

def _sample_brief() -> Brief:
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
                thesis_text="NVDA's DC segment growth +27% QoQ aligns with bull regime.",
                conviction_score=0.72,
                key_drivers=["DC revenue +27% QoQ", "H100→H200 transition smooth"],
                risks=["China export restriction"],
                open_questions=["Can DC growth sustain through CY27?"],
                evidence_anchors=[
                    {"claim": "DC revenue +27% QoQ", "source": "tool_call_nv001"},
                    {"claim": "H100→H200 transition smooth", "source": "tool_call_nv002"},
                ],
                chain_id="20260514T093000-deadbeef",
            ),
            BriefItem(
                ticker="META",
                intrinsic_score=0.65,
                stage_1_reason="Reels monetization signal",
                thesis_text="META Reels ad rev mix improving; Reality Labs losses moderating.",
                conviction_score=0.58,
                key_drivers=["Reels monetization improving"],
                risks=["EU antitrust"],
                open_questions=[],
                evidence_anchors=[
                    {"claim": "Reels monetization improving", "source": "tool_call_me301"},
                ],
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
    assert "conviction 0.72" in rendered
    assert "FOMC" in rendered
    assert "20260514T093000-deadbeef" in rendered


def test_top_level_render_clean_when_anchors_present() -> None:
    """Top-level scannable doesn't need anchor citations — those are for drill-down."""
    brief = _sample_brief()
    rendered = render_brief_top_level(brief)
    # Top-level scannable; no anchor markers expected here
    assert "[NO ANCHOR" not in rendered


def test_drill_down_shows_anchor_citations() -> None:
    brief = _sample_brief()
    rendered = render_brief_drill_down(brief, "NVDA")
    assert "NVDA" in rendered
    assert "tool_call_nv001" in rendered
    assert "tool_call_nv002" in rendered
    assert "DC revenue +27% QoQ" in rendered


def test_drill_down_flags_missing_anchor_as_visibility_regression() -> None:
    brief = _sample_brief()
    # Mutate: NVDA has a key driver without a matching anchor
    brief.items[0].key_drivers.append("Unanchored claim about future guidance")
    rendered = render_brief_drill_down(brief, "NVDA")
    assert "[NO ANCHOR — visibility regression]" in rendered
    assert "Unanchored claim" in rendered


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
    assert "tool_call_nv001" in rendered
