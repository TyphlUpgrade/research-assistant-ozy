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
