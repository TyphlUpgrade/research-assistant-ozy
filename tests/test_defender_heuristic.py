"""
Test the should_invoke_defender 3-condition AND heuristic
(Critic iter1 #11 requirement).

Conditions:
  1. Prior turn produced a Recommendation
  2. User message contains a disagreement marker
  3. User message contains NO evidence marker

All three must be true for Defender to fire.
"""
from __future__ import annotations

from research_assistant.orchestrator import should_invoke_defender


def test_fires_on_bare_disagreement_after_recommendation() -> None:
    """The canonical case the heuristic exists for."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, this is the wrong call.",
    ) is True


def test_does_not_fire_without_prior_recommendation() -> None:
    """Condition 1 fails — there's nothing to defend."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=False,
        user_message="I disagree, this is the wrong call.",
    ) is False


def test_does_not_fire_on_neutral_followup() -> None:
    """Condition 2 fails — no disagreement, just a question."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="What about the China segment?",
    ) is False


def test_does_not_fire_when_user_supplies_evidence_date() -> None:
    """Condition 3 fails — ISO date marker = new evidence, normal handling."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree — per the 2026-04-15 10-Q, the segment shrank.",
    ) is False


def test_does_not_fire_when_user_supplies_evidence_quarter() -> None:
    """Condition 3 fails — Q4 FY24 marker = new evidence."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I don't think that's right, the Q1 FY25 call showed deceleration.",
    ) is False


def test_does_not_fire_when_user_cites_filing() -> None:
    """Condition 3 fails — '10-K' marker = new evidence."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="You're wrong — the 10-K page 47 says otherwise.",
    ) is False


def test_does_not_fire_when_user_cites_percentage() -> None:
    """Condition 3 fails — numeric/percentage marker = new evidence."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="I disagree, revenue is down 18% not up.",
    ) is False


def test_fires_on_hostile_disagreement_without_evidence() -> None:
    """The META trial 5 scenario — hostile tone, zero evidence."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="That's wrong. You don't know what you're talking about.",
    ) is True


def test_fires_on_explicit_overconfident_pushback() -> None:
    """'too bullish' / 'overly bullish' patterns."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="That's too bullish for the current environment.",
    ) is True


def test_does_not_fire_on_simple_question() -> None:
    """A common follow-up question — must not waste an Opus Defender call."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="Are you sure about that?",
    ) is False


def test_fires_on_negation_followed_by_subjective_pushback() -> None:
    """'No, I' pattern is canonical disagreement-without-evidence."""
    assert should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message="No, I think you're being way too optimistic here.",
    ) is True
