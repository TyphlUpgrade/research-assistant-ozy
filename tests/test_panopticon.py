"""Phase C panopticon harness — offline (no network, no LLM).

Builds synthetic `stage_1_7_synthesizer` trace fixtures and exercises
collection, the degraded-exclusion rule, the decoy falsifiability arm, the
PASS/FAIL/INCOMPLETE gate, and the grading-sheet round-trip.
"""
import json
from pathlib import Path

from research_assistant import panopticon as pan


def _flag(obs_type="margin_divergence", sev="HIGH"):
    return {
        "severity": sev,
        "observation_type": obs_type,
        "shallow_source": "yfinance:rev",
        "shallow_observation": "revenue accelerating",
        "deep_source": "edgar:10q:ar",
        "deep_observation": "receivables growing faster than revenue",
        "verifier_shallow_verdict": "SUPPORTS",
        "verifier_deep_verdict": "SUPPORTS",
        "verifier_reasoning": "both anchors check out",
    }


def _event(chain, symbol, kept=(), dropped=(), degraded=False, ts="2026-06-01T12:00:00+00:00"):
    return {
        "stage_id": "stage_1_7_synthesizer",
        "chain_id": chain,
        "timestamp": ts,
        "symbol": symbol,
        "parsed": {
            "axes_agreed": not kept,
            "flags_kept": len(kept),
            "panopticon_degraded": degraded,
            "flags": {"kept": list(kept), "dropped": list(dropped)},
        },
    }


def _write(traces_base: Path, *events):
    d = traces_base / "2026-06-01"
    d.mkdir(parents=True, exist_ok=True)
    for ev in events:
        with (d / f"{ev['chain_id']}.jsonl").open("a") as f:
            # also drop a non-synth event + a junk line to prove they're skipped
            f.write(json.dumps({"stage_id": "stage_2_thesis", "parsed": {}}) + "\n")
            f.write("{not json\n")
            f.write(json.dumps(ev) + "\n")


def test_collect_excludes_degraded_and_counts(tmp_path):
    tb = tmp_path / "traces"
    _write(
        tb,
        _event("c1", "MU", kept=[_flag(), _flag("insider_divergence")], dropped=[_flag()]),
        _event("c2", "NVDA", kept=[_flag()], degraded=True),  # excluded by Gate 3
    )
    records, stats = pan.collect_flag_records(tb)
    assert len(records) == 2                      # NVDA degraded flag dropped
    assert stats["n_events"] == 1
    assert stats["n_degraded_excluded"] == 1
    assert stats["n_dropped"] == 1
    assert stats["tickers_seen"] == {"MU"}        # degraded ticker not counted


def test_decoy_false_positive_fails_gate(tmp_path):
    tb = tmp_path / "traces"
    decoys = frozenset({"AAPL", "MSFT", "COST"})
    _write(
        tb,
        _event("c1", "MU", kept=[_flag()]),
        _event("c2", "AAPL", kept=[_flag()]),      # flag on a control = false positive
        _event("c3", "MSFT", kept=[]),             # clean control
        _event("c4", "COST", kept=[]),             # clean control
    )
    records, stats = pan.collect_flag_records(tb, decoys=decoys)
    gate = pan.compute_gate(records, stats, decoys=decoys)
    assert stats["decoys_present"] == ["AAPL", "COST", "MSFT"]
    assert gate.decoy_fp_count == 1
    assert abs(gate.decoy_fp_rate - 1 / 3) < 1e-9
    assert gate.status == "FAIL"                   # 33% > 10% bound


def test_clean_decoys_incomplete_then_pass(tmp_path):
    tb = tmp_path / "traces"
    decoys = frozenset({"MSFT", "COST"})
    _write(
        tb,
        _event("c1", "MU", kept=[_flag()]),
        _event("c2", "PLUG", kept=[_flag("going_concern")]),
        _event("c3", "MSFT", kept=[]),
        _event("c4", "COST", kept=[]),
    )
    records, stats = pan.collect_flag_records(tb, decoys=decoys)

    # No grades yet → INCOMPLETE (decoys clean, but precision unknown).
    gate = pan.compute_gate(records, stats, decoys=decoys)
    assert gate.decoy_fp_rate == 0.0
    assert gate.status == "INCOMPLETE"

    # Hand-grade both real flags CORRECT → PASS.
    pan.apply_grades(records, {r.row_id: "CORRECT" for r in records})
    gate2 = pan.compute_gate(records, stats, decoys=decoys)
    assert gate2.precision == 1.0
    assert gate2.status == "PASS"


def test_low_precision_fails(tmp_path):
    tb = tmp_path / "traces"
    _write(tb, _event("c1", "MU", kept=[_flag(), _flag("a"), _flag("b"), _flag("c")]))
    records, stats = pan.collect_flag_records(tb)
    grades = {r.row_id: g for r, g in zip(records, ["CORRECT", "WRONG", "WRONG", "WRONG"])}
    pan.apply_grades(records, grades)
    gate = pan.compute_gate(records, stats)
    assert gate.precision == 0.25
    assert gate.status == "FAIL"


def test_grading_sheet_roundtrip(tmp_path):
    tb = tmp_path / "traces"
    _write(tb, _event("chainAAA1234567", "MU", kept=[_flag()]))
    records, _ = pan.collect_flag_records(tb)
    sheet = pan.render_grading_sheet(records)
    rid = records[0].row_id
    assert rid in sheet and sheet.rstrip().endswith("|  |")   # blank GRADE cell
    # Simulate a human filling that row's GRADE cell, then read it back.
    filled = "\n".join(
        line[:-3] + " CORRECT |" if line.startswith(f"| {rid} ") and line.endswith("|  |")
        else line
        for line in sheet.splitlines()
    )
    grades = pan.parse_graded_sheet(filled)
    assert grades.get(rid) == "CORRECT"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
