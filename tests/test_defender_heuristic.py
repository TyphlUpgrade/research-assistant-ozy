"""
Test the should_invoke_defender heuristic with v1.x citation verification
(Open Follow-up #2).

Conditions for Defender to fire (all must hold):
  1. Prior turn produced a Recommendation
  2. User message expresses disagreement
  3. User message has no evidence markers OR its strong citation tokens
     (dates, quarters, %, $, bps) do not all resolve against the prior
     anchor corpus.

When `prior_evidence_anchors` is omitted (or empty), no citation can be
verified, so any disagreement-with-citation fires Defender too — closing
the v1 "fake citation suppresses Defender" floor.
"""
from __future__ import annotations

from research_assistant.orchestrator import should_invoke_defender


# ---------------------------------------------------------------------------
# Bare disagreement & negative paths (unchanged in v1.x)
# ---------------------------------------------------------------------------

def test_fires_on_bare_disagreement_after_recommendation() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, this is the wrong call.",
    ) is True


def test_does_not_fire_without_prior_recommendation() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=False,
        user_message="I disagree, this is the wrong call.",
    ) is False


def test_does_not_fire_on_neutral_followup() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="What about the China segment?",
    ) is False


def test_does_not_fire_on_simple_question() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="Are you sure about that?",
    ) is False


def test_fires_on_hostile_disagreement_without_evidence() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="That's wrong. You don't know what you're talking about.",
    ) is True


def test_fires_on_explicit_overconfident_pushback() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="That's too bullish for the current environment.",
    ) is True


def test_fires_on_negation_followed_by_subjective_pushback() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="No, I think you're being way too optimistic here.",
    ) is True


# ---------------------------------------------------------------------------
# Uncorroborated citations — v1.x closes the floor
# ---------------------------------------------------------------------------

def test_fires_on_uncorroborated_date_citation() -> None:
    """ISO date present but no corpus to verify against — citation unverified."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — per the 2026-04-15 10-Q, the segment shrank.",
    ) is True


def test_fires_on_uncorroborated_quarter_citation() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I don't think that's right, the Q1 FY25 call showed deceleration.",
    ) is True


def test_fires_on_uncorroborated_filing_citation() -> None:
    """Document-type-only citation ('10-K page 47') has no strong tokens,
    so it can't be checked even with a corpus — defaults to unverified."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="You're wrong — the 10-K page 47 says otherwise.",
        prior_evidence_anchors=[{"claim": "DC revenue +27% QoQ", "source": "yfinance"}],
    ) is True


def test_fires_on_uncorroborated_percentage_citation() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, revenue is down 18% not up.",
    ) is True


# ---------------------------------------------------------------------------
# Verified citations — the corpus contains the cited token, no Defender
# ---------------------------------------------------------------------------

def test_does_not_fire_when_date_resolves_in_corpus() -> None:
    anchors = [{"claim": "Per the 2026-04-15 10-Q, segment grew 12%", "source": "yfinance:RIG:item_0"}]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — per the 2026-04-15 10-Q, the segment shrank.",
        prior_evidence_anchors=anchors,
    ) is False


def test_does_not_fire_when_quarter_resolves_in_corpus() -> None:
    anchors = [{"claim": "Q1 FY25 results showed margin expansion", "source": "headlines"}]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I don't think that's right, the Q1 FY25 call showed deceleration.",
        prior_evidence_anchors=anchors,
    ) is False


def test_does_not_fire_when_percentage_resolves_in_corpus() -> None:
    anchors = [{"claim": "30-day return +18.05% with price above EMA20", "source": "TICKER_DATA:daily_signals"}]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, your +18.05% number overstates the move.",
        prior_evidence_anchors=anchors,
    ) is False


def test_fires_when_some_tokens_resolve_but_others_do_not() -> None:
    """All strong tokens must resolve — partial match is unverified."""
    anchors = [{"claim": "Revenue grew 18% on the year", "source": "headlines"}]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        # 18% would resolve, but $2.5B does not appear in the corpus
        user_message="I disagree, revenue was $2.5B and 18% growth claim is wrong.",
        prior_evidence_anchors=anchors,
    ) is True


def test_fires_when_corpus_empty_even_with_strong_token() -> None:
    """Empty corpus = no anchor to verify against = unverified."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, your 18% is wrong.",
        prior_evidence_anchors=[],
    ) is True


# ---------------------------------------------------------------------------
# Adversarial cases: substring-containment false positives must be rejected
# (the bug the code-reviewer + architect both caught in 99d1cb9)
# ---------------------------------------------------------------------------

def test_fires_when_user_token_is_digit_prefix_of_corpus_value() -> None:
    """'18%' must NOT resolve against a corpus containing '118%'."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, revenue is down 18%.",
        prior_evidence_anchors=[{"claim": "YoY growth of 118%", "source": "headlines"}],
    ) is True


def test_fires_when_user_percentage_is_decimal_prefix_of_corpus_value() -> None:
    """'18.05%' must NOT resolve against a corpus containing '+118.05%'."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, your +18.05% number is wrong.",
        prior_evidence_anchors=[{"claim": "30-day return +118.05% with price above EMA", "source": "TICKER_DATA"}],
    ) is True


def test_fires_when_user_dollar_is_prefix_of_corpus_value() -> None:
    """'$5' must NOT resolve against a corpus containing '$500M'."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, revenue was $5.",
        prior_evidence_anchors=[{"claim": "Quarterly revenue $500M", "source": "headlines"}],
    ) is True


def test_fires_when_user_bps_is_prefix_of_corpus_value() -> None:
    """'200 bps' must NOT resolve against a corpus containing '1200 bps'."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, spread widened 200 bps.",
        prior_evidence_anchors=[{"claim": "Spread widened 1200 bps YoY", "source": "headlines"}],
    ) is True


def test_resolves_when_percentage_matches_with_boundary() -> None:
    """The previous bug-fix's defensive boundary must NOT block real matches."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, the +18% is too low.",
        prior_evidence_anchors=[{"claim": "30-day return +18% with price above EMA20", "source": "TICKER_DATA"}],
    ) is False


# ---------------------------------------------------------------------------
# Quarter-form coverage: 3Q24 / 1H25 / FY2025 must also be extracted as
# strong tokens (not silently dropped per the round-2 code-reviewer note)
# ---------------------------------------------------------------------------

def test_fires_on_uncorroborated_alt_quarter_form() -> None:
    """'3Q24' alone with empty corpus is unverified — Defender fires."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, 3Q24 was the inflection.",
        prior_evidence_anchors=[],
    ) is True


def test_does_not_fire_when_alt_quarter_resolves_in_corpus() -> None:
    anchors = [{"claim": "3Q24 results posted margin expansion", "source": "headlines"}]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, 3Q24 was the inflection.",
        prior_evidence_anchors=anchors,
    ) is False


def test_fires_on_half_year_form_uncorroborated() -> None:
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, 1H25 guidance was lifted.",
        prior_evidence_anchors=[],
    ) is True


def test_does_not_fire_on_fy_form_resolved() -> None:
    anchors = [{"claim": "FY2025 outlook raised by 8%", "source": "headlines"}]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, FY2025 guide is fine.",
        prior_evidence_anchors=anchors,
    ) is False


# ---------------------------------------------------------------------------
# FOLLOWUPS #2 — Defender hardening against EDGAR-typed anchor strings
# ---------------------------------------------------------------------------
# After #1 (EDGAR client) and #3 (Form 4 wiring), Stage 2 and /probe begin
# producing anchors with sources like edgar:form4:aggregate. The Defender
# heuristic's _flatten_anchors_to_corpus concatenates all dict values
# (claim + source) into the verification corpus, so user pushback citing a
# specific dollar / share figure from the insider summary must resolve
# against that corpus rather than firing Defender unnecessarily.

def test_does_not_fire_when_insider_dollars_resolve_in_edgar_anchor() -> None:
    """Stage 2 cited an insider net flow of -$42M via edgar:form4:aggregate.
    User pushback referencing the exact figure must NOT fire Defender —
    the claim is already corpus-verified."""
    anchors = [{
        "claim": "insider net flow last 90d: -$42.0M / 4 sales / 0 buys",
        "source": "edgar:form4:aggregate",
    }]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — the -$42.0M insider flow is the dominant signal.",
        prior_evidence_anchors=anchors,
    ) is False


def test_fires_when_insider_dollars_do_not_resolve_in_edgar_anchor() -> None:
    """User invents a $200M figure that's NOT in the prior anchor corpus
    (which only contains -$42M). Defender must fire — fake citation floor
    still holds against typed-source anchors."""
    anchors = [{
        "claim": "insider net flow last 90d: -$42.0M / 4 sales / 0 buys",
        "source": "edgar:form4:aggregate",
    }]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — the $200M insider flow contradicts your read.",
        prior_evidence_anchors=anchors,
    ) is True


def test_does_not_fire_when_filing_paragraph_anchor_carries_substance() -> None:
    """Anchor source edgar:8-K:<acc>:para_17 paired with the substantive
    claim — pushback citing the resolved figure stays inside the corpus."""
    anchors = [{
        "claim": "Q3 2026 segment revenue grew 27%",
        "source": "edgar:8-K:0001234567-26-000045:para_17",
    }]
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, 27% segment growth is not durable.",
        prior_evidence_anchors=anchors,
    ) is False
