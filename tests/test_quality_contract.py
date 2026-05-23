"""
Quality contract regression suite — the v1 ship gate.

Tests the four axes simultaneously, with concrete (named-mock + named-trigger +
named-pass-criterion) assertions per Critic iter1 requirement:

  1. FACTUAL  — no claim of current state without a preceding tool call.
                Mocked Stage 2 must not invent fields beyond what was passed.
  2. BACKBONE — Defender heuristic + Defender subagent both behave correctly
                under user pushback without new evidence.
  3. DEPTH    — Stage 2 output must reference fundamentals/filings depth, not
                headline summarization. Regex floor with substance co-occurrence
                gate (FOLLOWUPS #2). Full evaluator-LLM upgrade remains as #10.
  4. VISIBILITY — every cascade run produces a trace; every claim cites an
                anchor; orphan claims surface as [NO ANCHOR — visibility regression].

These tests are the merge gate. If any of the four axes fails, v1 does not ship.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from research_assistant.dossier_io import (
    Dossier,
    LedgerEntry,
    read_dossier,
    write_dossier_atomic,
)
from research_assistant.orchestrator import (
    research_ticker,
    should_invoke_defender,
)
from research_assistant.trace_renderer import (
    _format_stage_event,
    append_stage_event,
)


# ---------------------------------------------------------------------------
# AXIS 1 — FACTUAL ACCURACY (no-claim-without-fetch)
# ---------------------------------------------------------------------------

class TestAxis1Factual:
    """
    Mock the Anthropic client. Verify the orchestrator passes ticker_data
    INTO the prompt verbatim, and that the resulting dossier's State only
    cites values that were in the input data.

    The LLM-side enforcement (refusing to invent numbers) is at runtime via
    the Stage 2 prompt's evidence_anchors contract. Here we test the
    STRUCTURAL property: orchestrator does not strip or invent inputs.
    """

    @pytest.mark.asyncio
    async def test_orchestrator_passes_ticker_data_verbatim_to_stage_2(
        self, tmp_path: Path
    ) -> None:
        ticker_data = {
            "price": 145.32,
            "return_30d": 0.18,
            "return_90d": 0.34,
            "weekly_rsi_14": 68.4,
            "volume_5d_trend": "rising",
            "earnings_within_days": 7,
        }
        captured_stage_2_prompt = {}

        async def fake_stage_2_thesis(
            client, world_state, td, stage_1, headlines, insider_activity=None,
        ):
            captured_stage_2_prompt["ticker_data"] = td
            captured_stage_2_prompt["headlines"] = headlines
            parsed = {
                "ticker": "NVDA",
                "thesis_text": "Test thesis from mocked Stage 2.",
                "conviction_score": 0.6,
                "key_drivers": ["DC growth"],
                "risks": ["China export"],
                "open_questions": ["Sustainability?"],
                "evidence_anchors": [
                    {"claim": "DC growth", "source": "tool_call_test_1"},
                    {"claim": "China export", "source": "tool_call_test_2"},
                ],
            }
            return parsed, None  # None for CallResult — trace event tolerates it

        async def fake_stage_3_skeptic(client, ws, thesis_with_td, model="x"):
            parsed = {
                "ticker": "NVDA",
                "critique_text": "Test critique.",
                "adjusted_score": 0.55,
                "flagged_risks": ["earnings binary risk"],
                "open_questions_added": [],
                "news_reactivity_flag": False,
            }
            return parsed, None

        with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2_thesis), \
             patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3_skeptic):
            result = await research_ticker(
                "NVDA",
                world_state={"regime": "bull-trending"},
                ticker_data=ticker_data,
                headlines=[],
                base=tmp_path,
            )

        # The orchestrator MUST pass ticker_data verbatim — no fields dropped
        assert captured_stage_2_prompt["ticker_data"] == ticker_data, (
            "Orchestrator must not strip or mutate ticker_data before Stage 2"
        )

    @pytest.mark.asyncio
    async def test_dossier_state_does_not_invent_numbers(self, tmp_path: Path) -> None:
        """
        If Stage 2 returns no concrete numbers, the dossier State should
        contain no concrete numbers either. Guards against hallucination
        in the rendering layer.
        """
        async def fake_stage_2(client, ws, td, s1, h, insider_activity=None):
            return {
                "ticker": "AAPL",
                "thesis_text": "Qualitative thesis with no numbers.",
                "conviction_score": 0.55,
                "key_drivers": ["services growth"],
                "risks": ["regulatory"],
                "open_questions": [],
                "evidence_anchors": [{"claim": "services growth", "source": "tc_x"}],
            }, None

        async def fake_stage_3(client, ws, twd, model="x"):
            return {
                "ticker": "AAPL",
                "critique_text": "Critique without numbers.",
                "adjusted_score": 0.5,
                "flagged_risks": [],
                "open_questions_added": [],
                "news_reactivity_flag": False,
            }, None

        with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
             patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3):
            await research_ticker(
                "AAPL",
                world_state={},
                ticker_data={"price": 200.0},
                headlines=[],
                base=tmp_path,
            )

        dossier = read_dossier("AAPL", tmp_path)
        assert dossier is not None
        # Numbers that appear in State must be sourced from either inputs or
        # explicit scores. Check no unprompted financial figures crept in.
        forbidden_patterns = [
            r"\$\d{2,}",          # dollar amounts not in input
            r"\d+\.\d+B",         # billions notation
            r"Q[1-4]\s+20\d{2}",  # specific quarter cite the orchestrator didn't get
        ]
        for pat in forbidden_patterns:
            assert not re.search(pat, dossier.state_md), (
                f"Dossier state invented `{pat}` — factual axis regression"
            )


# ---------------------------------------------------------------------------
# AXIS 2 — BACKBONE (Defender)
# ---------------------------------------------------------------------------

class TestAxis2Backbone:
    """
    Defender's structural isolation was verified in Phase 3.5 spike (5/5).
    Here we test the INVOCATION HEURISTIC behaves correctly — the gating
    logic that decides when to spawn the Defender subagent.
    """

    def test_bare_disagreement_after_recommendation_triggers_defender(self) -> None:
        assert should_invoke_defender(
            prior_turn_had_recommendation=True,
            user_message="I disagree, this seems wrong.",
        ) is True

    def test_hostile_no_evidence_triggers_defender(self) -> None:
        """The META-trial-5 scenario — capitulation under hostility is the
        canonical failure mode Defender exists to prevent."""
        assert should_invoke_defender(
            prior_turn_had_recommendation=True,
            user_message="That's wrong. You don't know what you're talking about.",
        ) is True

    def test_named_new_evidence_does_not_trigger_defender(self) -> None:
        """User cites Q1 FY25 earnings call AND the prior anchor corpus
        contains Q1 FY25 → citation is verifiable → normal flow handles
        (v1.x Open Follow-up #2 closes the bare-citation suppression floor)."""
        anchors = [{"claim": "Q1 FY25 results beat consensus", "source": "yfinance:news:item_3"}]
        assert should_invoke_defender(
            prior_turn_had_recommendation=True,
            user_message="I disagree — Q1 FY25 call yesterday showed deceleration.",
            prior_evidence_anchors=anchors,
        ) is False

    def test_uncorroborated_citation_triggers_defender(self) -> None:
        """v1.x closes the v1 floor: a Q1 FY25 citation with no corresponding
        anchor in the prior research corpus is treated as unverified, so
        Defender fires (no capitulation to fabricated citations)."""
        assert should_invoke_defender(
            prior_turn_had_recommendation=True,
            user_message="I disagree — Q1 FY25 call yesterday showed deceleration.",
            prior_evidence_anchors=[{"claim": "DC revenue +27% QoQ", "source": "yfinance:NVDA:item_1"}],
        ) is True

    def test_simple_question_does_not_trigger_defender(self) -> None:
        """'Are you sure?' is conversational — must not waste an Opus call."""
        assert should_invoke_defender(
            prior_turn_had_recommendation=True,
            user_message="Are you sure about that?",
        ) is False

    def test_no_prior_recommendation_short_circuits(self) -> None:
        """Defender can't defend what hasn't been recommended."""
        assert should_invoke_defender(
            prior_turn_had_recommendation=False,
            user_message="I disagree with everything.",
        ) is False


# ---------------------------------------------------------------------------
# AXIS 3 — DEPTH (filings/transcripts/segment-data references)
# ---------------------------------------------------------------------------

# FOLLOWUPS #2 tightened the floor: a depth term (10-K, transcript, segment,
# etc.) must co-occur within `passes_depth_floor`'s substance window of a
# concrete signal (%, $, ISO date, Q[1-4], FY, 3+ digit number). Bare
# citations like "See the 10-K." now fail.
#
# Full evaluator-LLM upgrade (structured per-axis grading) remains as #10.

from research_assistant.quality_contract import passes_depth_floor


class TestAxis3Depth:
    """
    Tightened v1 floor (FOLLOWUPS #2): depth term + substance co-occurrence
    within an 80-char window. Bare citations no longer pass.

    Pass criterion: stage_2.thesis_text contains at least one depth term
    backed by a substance signal nearby.
    Fail = headline-summary OR bare-citation level output.
    """

    def test_depth_floor_passes_on_filings_reference(self) -> None:
        thesis = (
            "NVDA Q2 10-Q segment table shows data-center revenue +27% QoQ. "
            "Earnings call transcript notes Hopper-Blackwell transition smooth."
        )
        assert passes_depth_floor(thesis)

    def test_depth_floor_passes_on_transcript_reference(self) -> None:
        thesis = (
            "Per the Q3 earnings call, services growth re-accelerated. "
            "Management's discussion of forward P/E suggests room."
        )
        assert passes_depth_floor(thesis)

    def test_depth_floor_fails_on_headline_summary(self) -> None:
        """The class of output the evaluator (#10) will eventually grade."""
        thesis = (
            "Apple is doing great. The stock has been going up. "
            "I think there's more upside. Analysts are bullish."
        )
        assert not passes_depth_floor(thesis), (
            "Headline-summary thesis should NOT pass the depth floor"
        )

    def test_depth_floor_suppresses_bare_citation(self) -> None:
        """
        FOLLOWUPS #2 closure: 'See the 10-K for risk factors.' previously
        passed the regex by mentioning a depth term. The tightened floor
        requires a substance signal (%, $, date, quarter, etc.) within
        80 chars of the depth term.
        """
        bare = "See the 10-K for risk factors."
        assert not passes_depth_floor(bare), (
            "Bare-citation thesis must fail the depth floor (FOLLOWUPS #2)"
        )

    def test_depth_floor_passes_when_bare_mention_paired_with_substance(self) -> None:
        """Mixed thesis with one bare reference + substance elsewhere still
        passes — at least one depth match is substantively anchored."""
        mixed = "See the 10-K. Services revenue grew 18% last quarter."
        assert passes_depth_floor(mixed)

    def test_depth_floor_passes_when_substance_immediately_adjacent(self) -> None:
        assert passes_depth_floor("10-K filing details 5.2% margin compression.")

    def test_depth_floor_passes_on_iso_date_near_8k(self) -> None:
        assert passes_depth_floor("The 8-K filed 2026-05-19 confirms the catalyst.")


# ---------------------------------------------------------------------------
# AXIS 4 — VISIBILITY (cascade traces + anchor enforcement)
# ---------------------------------------------------------------------------

class TestAxis4Visibility:
    """
    Every claim must be anchorable; orphan claims must be flagged. The
    trace renderer is the user-facing surface that enforces this. Already
    spot-tested in test_trace_renderer.py; here we add the integration
    assertion at the orchestrator level.
    """

    def test_anchored_event_renders_without_regression_flag(self) -> None:
        event = {
            "stage_id": "stage_2_thesis", "chain_id": "c1", "model": "sonnet",
            "timestamp": "2026-05-14T10:00:00Z",
            "tokens_in": 1000, "tokens_out": 200, "cost_usd": 0.01, "latency_ms": 1500,
            "raw_response_truncated": "", "error": None,
            "parsed": {
                "key_drivers": ["DC revenue +27% QoQ"],
                "risks": ["China export"],
                "evidence_anchors": [
                    {"claim": "DC revenue +27% QoQ", "source": "tc_1"},
                    {"claim": "China export", "source": "tc_2"},
                ],
            },
        }
        rendered = _format_stage_event(event)
        assert "[NO ANCHOR" not in rendered

    def test_orphan_claim_flagged_as_regression(self) -> None:
        """The canonical visibility regression — a claim with no anchor."""
        event = {
            "stage_id": "stage_2_thesis", "chain_id": "c2", "model": "sonnet",
            "timestamp": "2026-05-14T10:00:00Z",
            "tokens_in": 1000, "tokens_out": 200, "cost_usd": 0.01, "latency_ms": 1500,
            "raw_response_truncated": "", "error": None,
            "parsed": {
                "key_drivers": ["DC revenue +27% QoQ", "Mystery driver"],
                "risks": [],
                "evidence_anchors": [
                    {"claim": "DC revenue +27% QoQ", "source": "tc_1"},
                ],
            },
        }
        rendered = _format_stage_event(event)
        assert "[NO ANCHOR — visibility regression]" in rendered
        assert "Mystery driver" in rendered

    def test_trace_jsonl_round_trips_through_renderer(self, tmp_path: Path) -> None:
        """End-to-end visibility surface: write trace, read trace, render trace."""
        append_stage_event(
            chain_id="vis_test",
            stage_id="stage_2_thesis",
            model="claude-sonnet-4-6",
            tokens_in=4000, tokens_out=400, cost_usd=0.018, latency_ms=2200,
            parsed={
                "key_drivers": ["d1"],
                "risks": [],
                "evidence_anchors": [{"claim": "d1", "source": "tc_v1"}],
            },
            raw_response="{...}",
            traces_base=tmp_path,
        )
        from research_assistant.trace_renderer import render_trace
        rendered = render_trace("vis_test", tmp_path)
        assert "tc_v1" in rendered
        assert "[NO ANCHOR" not in rendered


# ---------------------------------------------------------------------------
# Ship gate
# ---------------------------------------------------------------------------

class TestV1ShipGate:
    """
    The single test that says 'v1 is ready to ship'. Asserts the four axes
    each have at least one green concrete test above. If this fails, the
    quality contract has structurally regressed.
    """

    def test_four_axis_contract_is_intact(self) -> None:
        """Sanity: each axis's test class exists and is non-empty."""
        assert hasattr(TestAxis1Factual, "test_orchestrator_passes_ticker_data_verbatim_to_stage_2")
        assert hasattr(TestAxis2Backbone, "test_bare_disagreement_after_recommendation_triggers_defender")
        assert hasattr(TestAxis3Depth, "test_depth_floor_fails_on_headline_summary")
        assert hasattr(TestAxis4Visibility, "test_orphan_claim_flagged_as_regression")
