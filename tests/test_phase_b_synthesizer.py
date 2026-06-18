"""
Phase B (FOLLOWUPS #28) Stage 1.7 Synthesizer unit tests.

Coverage:
  - SynthesisFlag schema validation (v2 — no thesis_implication, etc.)
  - SynthesisOutput render three-state contract
  - parse_synthesis_output rejects internally-inconsistent payloads
  - build_anchor_corpus resolves every Phase A substrate source
  - verify_flag drops flags whose anchors are missing from corpus
  - verify_flag passes only when BOTH judges return SUPPORTS
  - failed verification drops the flag with NO retry (one synthesizer call)
  - axes_agreed=true short-circuits verification
  - Stage 2 prompt slot {synthesis_flags_block} present + renders
  - Orchestrator wiring: enable_synthesizer=False → no synthesizer call
  - Telemetry: stage_1_7_synthesizer event emitted with parsed metadata
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from research_assistant.deep_reads import InsiderBehaviorDetail
from research_assistant.fundamentals import EarningsCalendar, KPISummary
from research_assistant.fundamentals.kpis import AnnualLine
from research_assistant.positioning import AnalystRevisions, OptionsPositioning
from research_assistant.positioning.options import ExpiryIV
from research_assistant.synthesizer import (
    SEVERITY_VALUES,
    SynthesisFlag,
    SynthesisOutput,
    _FORBIDDEN_FLAG_KEYS,
    build_anchor_corpus,
    parse_flag,
    parse_synthesis_output,
    synthesize_research,
    verify_flag,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_parse_synthesis_output_accepts_axes_agreed_empty():
    payload = {
        "ticker": "MU", "schema_version": 2,
        "axes_agreed": True, "flags": [],
    }
    out = parse_synthesis_output(payload, default_ticker="MU")
    assert out.axes_agreed is True
    assert out.flags == []


def test_parse_synthesis_output_accepts_multi_flag():
    payload = {
        "ticker": "MU", "schema_version": 2, "axes_agreed": False,
        "flags": [
            {
                "severity": "HIGH",
                "observation_type": "headline_vs_decomposition",
                "shallow_source": "yfinance:fetch_news:MU:item_1",
                "shallow_observation": "Headline frames as routine.",
                "deep_source": "edgar:form4:behavior_detail",
                "deep_observation": "Behavior detail shows discretionary distribution.",
            },
        ],
    }
    out = parse_synthesis_output(payload, default_ticker="MU")
    assert len(out.flags) == 1
    assert out.flags[0].severity == "HIGH"


def test_parse_synthesis_output_rejects_thesis_implication():
    """v2 schema is evidence-only; reject any directive-shaped field."""
    payload = {
        "ticker": "MU", "schema_version": 2, "axes_agreed": False,
        "flags": [{
            "severity": "HIGH",
            "observation_type": "x",
            "shallow_source": "a", "shallow_observation": "b",
            "deep_source": "c", "deep_observation": "d",
            "thesis_implication": "BUY",
        }],
    }
    with pytest.raises(ValueError, match="forbidden directive keys"):
        parse_synthesis_output(payload, default_ticker="MU")


def test_parse_flag_rejects_all_forbidden_keys():
    """Each forbidden key independently triggers rejection."""
    for forbidden in _FORBIDDEN_FLAG_KEYS:
        base = {
            "severity": "HIGH",
            "observation_type": "x",
            "shallow_source": "a", "shallow_observation": "b",
            "deep_source": "c", "deep_observation": "d",
            forbidden: "any value",
        }
        with pytest.raises(ValueError, match="forbidden"):
            parse_flag(base)


def test_parse_synthesis_output_rejects_inconsistent_axes_agreed():
    """axes_agreed=True + non-empty flags is internally inconsistent."""
    payload = {
        "ticker": "MU", "schema_version": 2, "axes_agreed": True,
        "flags": [{
            "severity": "HIGH", "observation_type": "x",
            "shallow_source": "a", "shallow_observation": "b",
            "deep_source": "c", "deep_observation": "d",
        }],
    }
    with pytest.raises(ValueError, match="internally inconsistent"):
        parse_synthesis_output(payload, default_ticker="MU")


def test_parse_flag_rejects_invalid_severity():
    payload = {
        "severity": "CRITICAL", "observation_type": "x",
        "shallow_source": "a", "shallow_observation": "b",
        "deep_source": "c", "deep_observation": "d",
    }
    with pytest.raises(ValueError, match="severity must be one of"):
        parse_flag(payload)


def test_parse_flag_severity_uppercased():
    """Case-insensitive severity acceptance — uppercase is canonical."""
    for sev_in in ("high", "Medium", "LOW"):
        flag = parse_flag({
            "severity": sev_in, "observation_type": "x",
            "shallow_source": "a", "shallow_observation": "b",
            "deep_source": "c", "deep_observation": "d",
        })
        assert flag.severity in SEVERITY_VALUES
        assert flag.severity == sev_in.upper()


# ---------------------------------------------------------------------------
# Render contract
# ---------------------------------------------------------------------------

def test_render_for_prompt_none_returns_unavailable():
    assert "unavailable" in SynthesisOutput.render_for_prompt(None).lower()


def test_render_for_prompt_axes_agreed_empty():
    out = SynthesisOutput(
        ticker="MU", schema_version=2, axes_agreed=True, flags=[],
    )
    rendered = SynthesisOutput.render_for_prompt(out)
    assert "no cross-source divergences surfaced" in rendered.lower()


def test_render_for_prompt_verified_empty_distinct_from_axes_agreed():
    """Verified-empty (synthesizer thought there were divergences but
    all flags failed verification) renders distinctly from axes_agreed."""
    out = SynthesisOutput(
        ticker="MU", schema_version=2, axes_agreed=False, flags=[],
    )
    rendered = SynthesisOutput.render_for_prompt(out)
    assert "no verified flags" in rendered.lower()
    assert "axes agreed" not in rendered.lower()


def test_render_for_prompt_populated_flags():
    flag = SynthesisFlag(
        severity="HIGH", observation_type="headline_vs_decomposition",
        shallow_source="yfinance:fetch_news:MU:item_1",
        shallow_observation="Headline frames as routine.",
        deep_source="edgar:form4:behavior_detail",
        deep_observation="Detail shows discretionary $70M+.",
    )
    out = SynthesisOutput(
        ticker="MU", schema_version=2, axes_agreed=False, flags=[flag],
    )
    rendered = SynthesisOutput.render_for_prompt(out)
    assert "[HIGH] headline_vs_decomposition" in rendered
    assert "shallow (yfinance:fetch_news:MU:item_1)" in rendered
    assert "deep    (edgar:form4:behavior_detail)" in rendered


# ---------------------------------------------------------------------------
# Anchor corpus
# ---------------------------------------------------------------------------

def test_build_anchor_corpus_resolves_phase_a_substrate():
    """Every Phase A source label must resolve to non-empty text when
    its dataclass input is populated."""
    kpi = KPISummary(symbol="MU", asof="2026-06-09",
                    revenue=[AnnualLine(2025, 1e9)])
    cal = EarningsCalendar(symbol="MU", asof="2026-06-09",
                           next_earnings_date="2026-08-15")
    ops = OptionsPositioning(
        symbol="MU", asof="2026-06-09",
        expiries=[ExpiryIV(expiry="2026-06-20", days_to_expiry=11,
                           atm_iv=0.45, put_oi=100, call_oi=80,
                           put_volume=10, call_volume=8)],
    )
    rev = AnalystRevisions(symbol="MU", asof="2026-06-09", buy=5)
    detail = InsiderBehaviorDetail(
        symbol="MU", window_days=90,
        window_start="2026-03-11", window_end="2026-06-09",
    )
    corpus = build_anchor_corpus(
        ticker="MU",
        world_state={"regime": "neutral"},
        ticker_data={"symbol": "MU", "price": 100, "daily_signals": {"a": 1}},
        headlines=[{"title": "H1", "publisher": "P", "age_hours": 1, "absorption_stage": "absorbed"}],
        kpi_summary=kpi,
        earnings_calendar=cal,
        options_positioning=ops,
        analyst_revisions=rev,
        insider_detail=detail,
    )
    # Every label the synthesizer prompt teaches must resolve
    expected = [
        "world_state",
        "TICKER_DATA", "TICKER_DATA:daily_signals",
        "yfinance:fetch_news:MU:item_1",
        "yfinance:fetch_bars:MU", "yfinance:fetch_quote:MU",
        "edgar:form4:behavior_detail",
        "yfinance:financials:annual", "yfinance:financials:balance",
        "yfinance:financials:multiples",
        "yfinance:calendar:next", "yfinance:calendar:last",
        "yfinance:options:aggregate",
        "yfinance:options:2026-06-20",
        "yfinance:recommendations:aggregate",
    ]
    for label in expected:
        assert label in corpus, f"missing corpus entry: {label}"
        assert corpus[label], f"empty corpus entry: {label}"


def test_build_anchor_corpus_omits_bare_scalar_subkeys():
    """Scalar TICKER_DATA sub-keys (price, recent_return_5d, …) are NO
    LONGER valid anchors: rendered as bare numbers they can't support the
    framing claims the synthesizer attaches, so the role-bound verifier
    always rejected them. Only the rich daily_signals blob survives; the
    raw values still live inside the full TICKER_DATA blob."""
    corpus = build_anchor_corpus(
        ticker="MU",
        world_state={},
        ticker_data={
            "symbol": "MU", "price": 100, "recent_return_5d": 16.98,
            "return_30d": 12.0, "volume_ratio": 1.1,
            "daily_signals": {"adx": 19.5},
        },
        headlines=[],
    )
    for removed in (
        "TICKER_DATA:price", "TICKER_DATA:recent_return_5d",
        "TICKER_DATA:return_30d", "TICKER_DATA:return_90d",
        "TICKER_DATA:volume_ratio", "TICKER_DATA:volume_5d_trend",
        "TICKER_DATA:weekly_rsi_14",
    ):
        assert removed not in corpus, f"scalar sub-key should be gone: {removed}"
    # The rich blob and full snapshot remain.
    assert "TICKER_DATA:daily_signals" in corpus
    assert "TICKER_DATA" in corpus
    # The removed values are still reachable inside the full blob.
    assert "16.98" in corpus["TICKER_DATA"]


def test_news_anchor_marked_metadata_only():
    """News anchors render with an explicit metadata-only prefix so the
    blind re-extractor reliably reports no article body exists."""
    corpus = build_anchor_corpus(
        ticker="MU",
        world_state={},
        ticker_data={"symbol": "MU"},
        headlines=[{"title": "CEO buys", "publisher": "P",
                    "age_hours": 1, "absorption_stage": "fresh"}],
    )
    entry = corpus["yfinance:fetch_news:MU:item_1"]
    assert entry.startswith("HEADLINE METADATA ONLY")
    assert "CEO buys" in entry


def test_build_anchor_corpus_omits_unloaded_sources():
    """Sources whose input is None must NOT appear in the corpus —
    citation verification then fails for those labels (cheaper than
    rendering placeholder text + judging it as 'supports')."""
    corpus = build_anchor_corpus(
        ticker="X",
        world_state={},
        ticker_data={"symbol": "X"},
        headlines=[],
    )
    assert "yfinance:financials:annual" not in corpus
    assert "yfinance:options:aggregate" not in corpus
    assert "edgar:13f:aggregate" not in corpus


# ---------------------------------------------------------------------------
# verify_flag — role-bound verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_flag_rejects_unresolved_shallow_anchor():
    """Flag citing a phantom anchor fails verification without any
    LLM call (cheaper than burning Haiku on missing-corpus citations)."""
    flag = SynthesisFlag(
        severity="HIGH", observation_type="x",
        shallow_source="phantom:anchor:nope",
        shallow_observation="a",
        deep_source="world_state",
        deep_observation="b",
    )
    corpus = {"world_state": "regime data"}
    fake_client = AsyncMock()
    passed, cost = await verify_flag(fake_client, flag, corpus, ticker="X")
    assert passed is False
    assert cost == 0.0
    assert "unresolved anchor" in flag.verifier_reasoning


@pytest.mark.asyncio
async def test_verify_flag_passes_only_when_both_judges_support():
    """Mock the two Haiku calls per anchor (extract + judge). Pass case:
    both judges return SUPPORTS."""
    flag = SynthesisFlag(
        severity="HIGH", observation_type="x",
        shallow_source="A", shallow_observation="obs_shallow",
        deep_source="B", deep_observation="obs_deep",
    )
    corpus = {"A": "text_a", "B": "text_b"}

    # Sequence of CallResults (4 calls per flag: 2 anchors × extract+judge)
    extract = type("R", (), {
        "text": "Independent reading of source.",
        "cost_usd": 0.001, "input_tokens": 100, "output_tokens": 50,
        "model": "haiku", "latency_ms": 100,
    })()
    judge_supports = type("R", (), {
        "text": '{"verdict": "SUPPORTS", "reasoning": "aligned"}',
        "cost_usd": 0.001, "input_tokens": 100, "output_tokens": 50,
        "model": "haiku", "latency_ms": 100,
    })()
    fake_client = AsyncMock()
    fake_client.call = AsyncMock(side_effect=[
        extract, judge_supports,  # shallow anchor
        extract, judge_supports,  # deep anchor
    ])
    passed, cost = await verify_flag(fake_client, flag, corpus, ticker="X")
    assert passed is True
    assert cost == pytest.approx(0.004)
    assert flag.verifier_shallow_verdict == "SUPPORTS"
    assert flag.verifier_deep_verdict == "SUPPORTS"


@pytest.mark.asyncio
async def test_verify_flag_fails_when_judge_contradicts():
    flag = SynthesisFlag(
        severity="HIGH", observation_type="x",
        shallow_source="A", shallow_observation="obs",
        deep_source="B", deep_observation="obs",
    )
    corpus = {"A": "a", "B": "b"}
    extract = type("R", (), {
        "text": "Reading.", "cost_usd": 0.001, "input_tokens": 50, "output_tokens": 20,
        "model": "haiku", "latency_ms": 50,
    })()
    judge_contradicts = type("R", (), {
        "text": '{"verdict": "CONTRADICTS", "reasoning": "diverges"}',
        "cost_usd": 0.001, "input_tokens": 50, "output_tokens": 20,
        "model": "haiku", "latency_ms": 50,
    })()
    fake_client = AsyncMock()
    fake_client.call = AsyncMock(side_effect=[
        extract, judge_contradicts,  # shallow → CONTRADICTS
        extract, type("R", (), {
            "text": '{"verdict": "SUPPORTS", "reasoning": "ok"}',
            "cost_usd": 0.001, "input_tokens": 50, "output_tokens": 20,
            "model": "haiku", "latency_ms": 50,
        })(),
    ])
    passed, cost = await verify_flag(fake_client, flag, corpus, ticker="X")
    assert passed is False
    assert flag.verifier_shallow_verdict == "CONTRADICTS"
    assert flag.verifier_deep_verdict == "SUPPORTS"


@pytest.mark.asyncio
async def test_verify_flag_fails_on_judge_parse_failure():
    """Judge returning non-JSON triggers UNCLEAR verdict → fail."""
    flag = SynthesisFlag(
        severity="HIGH", observation_type="x",
        shallow_source="A", shallow_observation="obs",
        deep_source="B", deep_observation="obs",
    )
    corpus = {"A": "a", "B": "b"}
    extract = type("R", (), {
        "text": "Reading.", "cost_usd": 0.001, "input_tokens": 50, "output_tokens": 20,
        "model": "haiku", "latency_ms": 50,
    })()
    judge_garbage = type("R", (), {
        "text": "not json at all",
        "cost_usd": 0.001, "input_tokens": 50, "output_tokens": 20,
        "model": "haiku", "latency_ms": 50,
    })()
    fake_client = AsyncMock()
    fake_client.call = AsyncMock(side_effect=[
        extract, judge_garbage,
        extract, judge_garbage,
    ])
    passed, _ = await verify_flag(fake_client, flag, corpus, ticker="X")
    assert passed is False
    assert flag.verifier_shallow_verdict == "UNCLEAR"


@pytest.mark.asyncio
async def test_synthesize_research_drops_failed_flag_without_retry():
    """A flag that fails verification is dropped with NO second synthesizer
    call. The old per-flag retry re-ran the whole synthesizer (the dominant
    cost on early runs); the contract fix makes that futile, so it's gone."""
    flag = SynthesisFlag(
        severity="HIGH", observation_type="x",
        shallow_source="world_state", shallow_observation="a",
        deep_source="TICKER_DATA:daily_signals", deep_observation="b",
    )
    output = SynthesisOutput(
        ticker="X", schema_version=2, axes_agreed=False, flags=[flag],
    )
    synth_mock = AsyncMock(return_value=(output, None))
    verify_mock = AsyncMock(return_value=(False, 0.005))
    with patch("research_assistant.synthesizer._stage_1_7_synthesizer", synth_mock), \
         patch("research_assistant.synthesizer.verify_flag", verify_mock):
        result = await synthesize_research(
            AsyncMock(),
            ticker="X",
            world_state={"regime": "neutral"},
            ticker_data={"symbol": "X", "daily_signals": {"adx": 19.5}},
            headlines=[],
        )
    assert synth_mock.await_count == 1, "retry must NOT re-run the synthesizer"
    assert result is not None
    assert result.flags == []
    assert result.flags_pre_verification == 1
    assert result.flags_dropped_verification == 1


# ---------------------------------------------------------------------------
# Stage 2 prompt slot wiring
# ---------------------------------------------------------------------------

def test_stage_2_thesis_prompt_carries_synthesis_flags_slot():
    from research_assistant.prompts import load_prompt
    template = load_prompt("stage_2_thesis")
    assert "{synthesis_flags_block}" in template


def test_stage_2_thesis_renders_synthesis_flags_with_none_input():
    """Flag-off path: synthesis_output=None → unavailable sentinel
    fills the slot. No `{synthesis_flags_block}` placeholder leak."""
    from research_assistant.deep_reads import InsiderBehaviorDetail
    from research_assistant.edgar import (
        InsiderActivitySummary,
        InstitutionalOwnership,
    )
    from research_assistant.prompts import load_prompt, render
    template = load_prompt("stage_2_thesis")
    out = render(
        template,
        ticker_json="{}", stage_1_json="{}", headlines_json="[]",
        insider_activity_block=InsiderActivitySummary.render_for_prompt(None),
        institutional_ownership_block=InstitutionalOwnership.render_for_prompt(None),
        insider_detail_block=InsiderBehaviorDetail.render_for_prompt(None),
        fundamentals_block=KPISummary.render_for_prompt(None),
        earnings_calendar_block=EarningsCalendar.render_for_prompt(None),
        options_positioning_block=OptionsPositioning.render_for_prompt(None),
        analyst_revisions_block=AnalystRevisions.render_for_prompt(None),
        synthesis_flags_block=SynthesisOutput.render_for_prompt(None),
    )
    assert "{synthesis_flags_block}" not in out
    assert "synthesis flags unavailable" in out.lower()


# ---------------------------------------------------------------------------
# Orchestrator integration: enable_synthesizer=False → no synthesizer call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_research_ticker_synthesizer_off_does_not_call_synthesize(
    tmp_path: Path,
) -> None:
    """Default behavior (enable_synthesizer=False) must NOT invoke the
    synthesizer — preserves no-cost-regression guarantee for users who
    haven't opted into the panopticon."""
    from research_assistant.orchestrator import research_ticker

    captured: dict = {}

    async def fake_stage_2(client, ws, td, s1, h, **kwargs):
        captured.update(kwargs)
        return (
            {
                "ticker": "MU", "thesis_text": "ok", "conviction_score": 0.5,
                "key_drivers": ["d"], "risks": ["r"], "open_questions": [],
                "evidence_anchors": [
                    {"claim": "d", "source": "no_fetch"},
                    {"claim": "r", "source": "no_fetch"},
                ],
            },
            type("M", (), {
                "model": "x", "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "latency_ms": 0, "text": "",
            })(),
        )

    async def fake_stage_3(client, ws, twd, model="x"):
        return (
            {"adjusted_score": 0.4, "critique_text": "", "flagged_risks": []},
            type("M", (), {
                "model": "x", "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "latency_ms": 0, "text": "",
            })(),
        )

    synth_mock = AsyncMock()
    with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3), \
         patch("research_assistant.orchestrator.synthesize_research", synth_mock):
        await research_ticker(
            "MU",
            world_state={"regime": "neutral"},
            ticker_data={"symbol": "MU", "price": 100},
            headlines=[],
            base=tmp_path,
            enable_synthesizer=False,
        )
    synth_mock.assert_not_called()
    assert captured["synthesis_output"] is None


@pytest.mark.asyncio
async def test_research_ticker_synthesizer_on_invokes_synthesize_and_emits_trace(
    tmp_path: Path,
) -> None:
    """enable_synthesizer=True must invoke synthesize_research AND emit a
    stage_1_7_synthesizer trace event with axes_agreed metadata."""
    from research_assistant.orchestrator import research_ticker

    fake_synth_output = SynthesisOutput(
        ticker="MU", schema_version=2, axes_agreed=True, flags=[],
        synthesizer_cost_usd=0.04, verifier_cost_usd=0.0,
        flags_pre_verification=0, flags_dropped_verification=0,
    )

    async def fake_synth(*args, **kwargs):
        return fake_synth_output

    captured: dict = {}

    async def fake_stage_2(client, ws, td, s1, h, **kwargs):
        captured.update(kwargs)
        return (
            {
                "ticker": "MU", "thesis_text": "ok", "conviction_score": 0.5,
                "key_drivers": ["d"], "risks": ["r"], "open_questions": [],
                "evidence_anchors": [
                    {"claim": "d", "source": "no_fetch"},
                    {"claim": "r", "source": "no_fetch"},
                ],
            },
            type("M", (), {
                "model": "x", "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "latency_ms": 0, "text": "",
            })(),
        )

    async def fake_stage_3(client, ws, twd, model="x"):
        return (
            {"adjusted_score": 0.4, "critique_text": "", "flagged_risks": []},
            type("M", (), {
                "model": "x", "input_tokens": 0, "output_tokens": 0,
                "cost_usd": 0.0, "latency_ms": 0, "text": "",
            })(),
        )

    with patch("research_assistant.orchestrator._stage_2_thesis", fake_stage_2), \
         patch("research_assistant.orchestrator._stage_3_skeptic", fake_stage_3), \
         patch("research_assistant.orchestrator.synthesize_research", fake_synth):
        result = await research_ticker(
            "MU",
            world_state={"regime": "neutral"},
            ticker_data={"symbol": "MU", "price": 100},
            headlines=[],
            base=tmp_path,
            enable_synthesizer=True,
        )

    assert captured["synthesis_output"] is fake_synth_output

    # Trace event must include the synthesizer telemetry
    trace_dir = tmp_path / "traces"
    trace_files = list(trace_dir.rglob("*.jsonl"))
    assert trace_files, "expected at least one trace JSONL file"
    found_synth_event = False
    for trace_file in trace_files:
        for line in trace_file.read_text().splitlines():
            event = json.loads(line)
            if event.get("stage_id") == "stage_1_7_synthesizer":
                found_synth_event = True
                parsed = event.get("parsed") or {}
                assert parsed.get("axes_agreed") is True
                assert parsed.get("flags_kept") == 0
                assert parsed.get("flags_pre_verification") == 0
                break
    assert found_synth_event, "stage_1_7_synthesizer trace event missing"


# ---------------------------------------------------------------------------
# Module-level sanity: forbidden keys cover the spec's directive shapes
# ---------------------------------------------------------------------------

def test_forbidden_flag_keys_includes_spec_named_directives():
    """Spec v2 (after critic M1 + operator pushback) names
    thesis_implication as the directive-leak risk. Ensure the parser
    catches it AND the broader category of verdict-shape fields."""
    for required_forbidden in (
        "thesis_implication", "verdict", "recommendation", "action",
    ):
        assert required_forbidden in _FORBIDDEN_FLAG_KEYS
