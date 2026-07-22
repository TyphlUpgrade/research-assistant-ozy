"""FOLLOWUPS #31 Layer-2: the trend-recognition fields are first-class
synthesizer anchors, and the corpus labels stay in sync with the prompt
allow-list (the one real coupling in the synthesizer's source contract)."""
from __future__ import annotations

from research_assistant.prompts import PROMPTS_DIR
from research_assistant.synthesizer import build_anchor_corpus

_TREND_LABELS = (
    "TICKER_DATA:market_structure",
    "TICKER_DATA:ts_momentum",
    "TICKER_DATA:directional_change",
)


def _ticker_data_with_trend() -> dict:
    return {
        "symbol": "MU",
        "market_structure": {"structure": "downtrend", "last_swing_high": 1035.5,
                             "last_swing_low": 804.0, "n_pivots": 9},
        "ts_momentum": {"momentum_return": -0.08, "realized_vol": 1.21,
                        "vol_scaled": -0.24, "direction": "down", "lookback": 20},
        "directional_change": {"mode": "up", "theta": 0.05, "n_events": 15},
    }


def test_corpus_exposes_trend_anchors_as_context_rich_blobs():
    corpus = build_anchor_corpus(
        ticker="MU", world_state={}, ticker_data=_ticker_data_with_trend(),
        headlines=[],
    )
    for label in _TREND_LABELS:
        assert label in corpus, f"{label} missing from corpus"
        # Context-rich (header text present), not a bare number — this is what
        # lets the role-bound verifier read a framing (cf. removed scalars).
        assert len(corpus[label]) > 40


def test_trend_anchors_absent_when_fields_missing():
    corpus = build_anchor_corpus(
        ticker="MU", world_state={}, ticker_data={"symbol": "MU"}, headlines=[],
    )
    for label in _TREND_LABELS:
        assert label not in corpus


def test_corpus_labels_match_prompt_allowlist_no_drift():
    """Each trend anchor the corpus can emit MUST be documented in the
    synthesizer prompt's allow-list, or the verifier rejects every flag that
    cites it. Guards the corpus<->prompt coupling."""
    prompt = (PROMPTS_DIR / "stage_1_7_synthesizer.txt").read_text()
    for label in _TREND_LABELS:
        assert label in prompt, f"{label} in corpus but not in prompt allow-list"
