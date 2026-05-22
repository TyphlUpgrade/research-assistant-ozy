"""
Tests for cli.py — argparse plumbing + handler dispatch (no live API calls).

End-to-end CLI smoke happens at Tier 3 (live yfinance + Anthropic).
These offline tests verify argparse correctness, handler routing, and
JSON-output mode for programmatic consumption by skills.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from research_assistant import cli


# ---------------------------------------------------------------------------
# Parser plumbing
# ---------------------------------------------------------------------------

def test_parser_requires_subcommand() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_research_subcommand() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["research", "NVDA"])
    assert ns.cmd == "research"
    assert ns.ticker == "NVDA"
    assert ns.json is False
    assert ns.quiet is False


def test_parser_probe_subcommand() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["probe", "IONQ", "What is the catalyst?"])
    assert ns.cmd == "probe"
    assert ns.ticker == "IONQ"
    assert ns.question == "What is the catalyst?"
    assert ns.deep is False


def test_parser_probe_deep_flag() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["probe", "IONQ", "Q?", "--deep"])
    assert ns.deep is True


def test_parser_probe_requires_question() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["probe", "IONQ"])


def test_parser_brief_with_drilldown() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["brief", "--ticker", "TSLA", "--refresh"])
    assert ns.cmd == "brief"
    assert ns.ticker == "TSLA"
    assert ns.refresh is True


def test_parser_brief_default_flags_false() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["brief"])
    assert ns.refresh is False
    assert ns.static_only is False
    assert ns.rediscover is False


def test_parser_brief_static_only_and_rediscover() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["brief", "--refresh", "--static-only", "--rediscover"])
    assert ns.refresh is True
    assert ns.static_only is True
    assert ns.rediscover is True


def test_load_prior_universe_snapshot_missing_file(tmp_path: Path) -> None:
    assert cli._load_prior_universe_snapshot(tmp_path / "absent.json") == []


def test_load_prior_universe_snapshot_old_format_no_field(tmp_path: Path) -> None:
    cache = tmp_path / "old.json"
    cache.write_text('{"date_et": "2026-05-20", "items": []}')
    assert cli._load_prior_universe_snapshot(cache) == []


def test_load_prior_universe_snapshot_returns_persisted_tickers(tmp_path: Path) -> None:
    cache = tmp_path / "today.json"
    cache.write_text('{"discovered_universe": ["AAPL", "NVDA", "TSLA"]}')
    assert cli._load_prior_universe_snapshot(cache) == ["AAPL", "NVDA", "TSLA"]


def test_load_prior_universe_snapshot_corrupt_json_falls_through(tmp_path: Path) -> None:
    cache = tmp_path / "broken.json"
    cache.write_text("{not valid json")
    assert cli._load_prior_universe_snapshot(cache) == []


# ---------------------------------------------------------------------------
# defender-check
# ---------------------------------------------------------------------------

def _write_trace(
    traces_base: Path,
    chain_id: str,
    anchors: list[dict],
    *,
    symbol: str | None = None,
) -> Path:
    """Write a one-event trace JSONL file with the given Stage-2 anchors."""
    dated = traces_base / "2026-05-20"
    dated.mkdir(parents=True, exist_ok=True)
    path = dated / f"{chain_id}.jsonl"
    event = {
        "stage_id": "stage_2_thesis",
        "chain_id": chain_id,
        "symbol": symbol,
        "parsed": {"evidence_anchors": anchors},
    }
    path.write_text(json.dumps(event) + "\n")
    return path


def _append_trace_event(
    traces_base: Path,
    chain_id: str,
    anchors: list[dict],
    *,
    symbol: str | None = None,
    stage_id: str = "stage_2_thesis",
) -> Path:
    dated = traces_base / "2026-05-20"
    dated.mkdir(parents=True, exist_ok=True)
    path = dated / f"{chain_id}.jsonl"
    event = {
        "stage_id": stage_id,
        "chain_id": chain_id,
        "symbol": symbol,
        "parsed": {"evidence_anchors": anchors},
    }
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")
    return path


def test_parser_defender_check_requires_both_args() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["defender-check"])
    with pytest.raises(SystemExit):
        parser.parse_args(["defender-check", "--user-message", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["defender-check", "--chain-id", "y"])


def test_load_anchors_from_chain_reads_stage_2_only(tmp_path: Path) -> None:
    """Other stage events in the trace are ignored — only stage_2_thesis."""
    traces = tmp_path / "traces"
    chain = "20260520T000000-test01"
    _write_trace(traces, chain, [{"claim": "30d +18%", "source": "TICKER_DATA"}])
    anchors = cli._load_anchors_from_chain(traces, chain)
    assert anchors == [{"claim": "30d +18%", "source": "TICKER_DATA"}]


def test_load_anchors_from_chain_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cli._load_anchors_from_chain(tmp_path / "traces", "no-such-chain")


def test_defender_check_returns_false_on_verified_citation(tmp_path: Path, capsys) -> None:
    traces = tmp_path / "traces"
    chain = "20260520T000000-verif1"
    _write_trace(traces, chain, [{"claim": "30-day return +18.05%", "source": "TICKER_DATA"}])
    ns = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "defender-check",
        "--user-message", "I disagree, your +18.05% number is wrong",
        "--chain-id", chain,
    ])
    rc = cli._cmd_defender_check(ns)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "false"


def test_defender_check_returns_true_on_uncorroborated_citation(tmp_path: Path, capsys) -> None:
    traces = tmp_path / "traces"
    chain = "20260520T000000-uncorr"
    _write_trace(traces, chain, [{"claim": "ADX 33 confirms trend", "source": "TICKER_DATA"}])
    ns = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "defender-check",
        "--user-message", "I disagree, the 10-K page 47 shows 18%",
        "--chain-id", chain,
    ])
    rc = cli._cmd_defender_check(ns)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "true"


def test_defender_check_missing_chain_returns_error(tmp_path: Path, capsys) -> None:
    ns = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "defender-check",
        "--user-message", "I disagree",
        "--chain-id", "ghost-chain",
    ])
    rc = cli._cmd_defender_check(ns)
    assert rc == 1
    assert "No trace for chain ghost-chain" in capsys.readouterr().err


def test_load_anchors_rejects_malformed_chain_id(tmp_path: Path) -> None:
    """Defense-in-depth: chain_id must match the canonical token format
    before being passed to rglob."""
    with pytest.raises(ValueError, match="Invalid chain_id format"):
        cli._load_anchors_from_chain(tmp_path / "traces", "../../etc/passwd")
    with pytest.raises(ValueError, match="Invalid chain_id format"):
        cli._load_anchors_from_chain(tmp_path / "traces", "chain*id")
    with pytest.raises(ValueError, match="Invalid chain_id format"):
        cli._load_anchors_from_chain(tmp_path / "traces", "short")


def test_load_anchors_aggregates_multiple_stage_2_events(tmp_path: Path) -> None:
    """A brief chain has multiple Stage-2 events (one per survivor). All
    anchors should be aggregated when no ticker filter is supplied."""
    traces = tmp_path / "traces"
    chain = "20260520T000000-multi1"
    _append_trace_event(traces, chain,
        anchors=[{"claim": "NVDA +24% 30d", "source": "TICKER_DATA"}],
        symbol="NVDA",
    )
    _append_trace_event(traces, chain,
        anchors=[{"claim": "TSLA -5% 5d", "source": "TICKER_DATA"}],
        symbol="TSLA",
    )
    all_anchors = cli._load_anchors_from_chain(traces, chain)
    assert len(all_anchors) == 2
    claims = {a["claim"] for a in all_anchors}
    assert claims == {"NVDA +24% 30d", "TSLA -5% 5d"}


def test_load_anchors_filters_by_ticker(tmp_path: Path) -> None:
    """The --ticker filter scopes to one survivor's anchors only — the fix
    for the architect's HIGH multi-survivor scoping finding."""
    traces = tmp_path / "traces"
    chain = "20260520T000000-multi2"
    _append_trace_event(traces, chain,
        anchors=[{"claim": "NVDA +24% 30d", "source": "TICKER_DATA"}],
        symbol="NVDA",
    )
    _append_trace_event(traces, chain,
        anchors=[{"claim": "TSLA -5% 5d", "source": "TICKER_DATA"}],
        symbol="TSLA",
    )
    nvda_only = cli._load_anchors_from_chain(traces, chain, ticker="NVDA")
    assert len(nvda_only) == 1
    assert nvda_only[0]["claim"] == "NVDA +24% 30d"


def test_load_anchors_ticker_filter_case_insensitive(tmp_path: Path) -> None:
    traces = tmp_path / "traces"
    chain = "20260520T000000-case01"
    _append_trace_event(traces, chain,
        anchors=[{"claim": "x", "source": "y"}],
        symbol="NVDA",
    )
    assert len(cli._load_anchors_from_chain(traces, chain, ticker="nvda")) == 1


def test_load_anchors_ticker_filter_passes_through_legacy_events(tmp_path: Path) -> None:
    """Events without a `symbol` field (older traces) are included so
    single-ticker chains stay backward-compatible."""
    traces = tmp_path / "traces"
    chain = "20260520T000000-legacy"
    _append_trace_event(traces, chain,
        anchors=[{"claim": "legacy event", "source": "x"}],
        symbol=None,
    )
    result = cli._load_anchors_from_chain(traces, chain, ticker="NVDA")
    assert len(result) == 1


def test_load_anchors_skips_corrupt_jsonl_lines(tmp_path: Path) -> None:
    """A truncated/corrupt line in the trace file should not crash the loader."""
    traces = tmp_path / "traces" / "2026-05-20"
    traces.mkdir(parents=True)
    chain = "20260520T000000-corrupt"
    path = traces / f"{chain}.jsonl"
    good_event = {
        "stage_id": "stage_2_thesis",
        "chain_id": chain,
        "symbol": "NVDA",
        "parsed": {"evidence_anchors": [{"claim": "x", "source": "y"}]},
    }
    path.write_text(
        json.dumps(good_event) + "\n"
        + "{not valid json\n"
        + json.dumps(good_event) + "\n"
    )
    result = cli._load_anchors_from_chain(tmp_path / "traces", chain)
    assert len(result) == 2


def test_defender_check_brief_scoping_uses_ticker(tmp_path: Path, capsys) -> None:
    """End-to-end: a TSLA pushback citing "+24% 30d" must NOT verify against
    the NVDA anchor (also "+24% 30d") when --ticker TSLA scopes the corpus."""
    traces = tmp_path / "traces"
    chain = "20260520T000000-scope1"
    _append_trace_event(traces, chain,
        anchors=[{"claim": "NVDA delivered +24% 30d return", "source": "TICKER_DATA"}],
        symbol="NVDA",
    )
    _append_trace_event(traces, chain,
        anchors=[{"claim": "TSLA delivered -8% 5d return", "source": "TICKER_DATA"}],
        symbol="TSLA",
    )
    ns = cli._build_parser().parse_args([
        "--base", str(tmp_path),
        "defender-check",
        "--user-message", "I disagree, your +24% number is wrong",
        "--chain-id", chain,
        "--ticker", "TSLA",
    ])
    rc = cli._cmd_defender_check(ns)
    assert rc == 0
    # +24% appears in NVDA's anchor but TSLA filter scopes it out → fires
    assert capsys.readouterr().out.strip() == "true"


def test_parser_trace_required_chain_id() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["trace", "20260514T093000-deadbeef"])
    assert ns.cmd == "trace"
    assert ns.chain_id == "20260514T093000-deadbeef"


def test_parser_dossier_subcommand() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["dossier", "AAPL"])
    assert ns.cmd == "dossier"
    assert ns.ticker == "AAPL"


def test_parser_global_flags() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["--base", "/tmp/r", "--json", "--quiet", "dossier", "X"])
    assert ns.base == "/tmp/r"
    assert ns.json is True
    assert ns.quiet is True


# ---------------------------------------------------------------------------
# Resolve helpers
# ---------------------------------------------------------------------------

def test_resolve_base_uses_cwd_default() -> None:
    base = cli._resolve_base(None)
    assert base.is_absolute()
    assert base.name == ".research"


def test_resolve_base_uses_arg_when_provided() -> None:
    base = cli._resolve_base("/tmp/custom_research")
    assert base == Path("/tmp/custom_research").resolve()


# ---------------------------------------------------------------------------
# require_api_key
# ---------------------------------------------------------------------------

def test_require_api_key_returns_none_when_unset(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = cli._require_api_key(quiet=False)
    assert result is None
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err


def test_require_api_key_returns_key_when_set(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    assert cli._require_api_key(quiet=True) == "sk-test-fake"


# ---------------------------------------------------------------------------
# dossier handler (no LLM calls — pure I/O)
# ---------------------------------------------------------------------------

def test_dossier_command_missing_returns_error(tmp_path: Path, capsys) -> None:
    args = MagicMock(ticker="NONEXISTENT", base=str(tmp_path), json=False, quiet=True)
    rc = cli._cmd_dossier(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No dossier yet" in err


def test_dossier_command_prints_existing_file(tmp_path: Path, capsys) -> None:
    from research_assistant.dossier_io import Dossier, LedgerEntry, write_dossier_atomic
    d = Dossier(
        symbol="NVDA",
        conviction=0.7,
        state_md="Test state",
        open_questions=["q1"],
        ledger=[LedgerEntry(timestamp="2026-05-14T10:00:00Z", kind="probe", summary="ok")],
    )
    write_dossier_atomic(d, tmp_path)

    args = MagicMock(ticker="NVDA", base=str(tmp_path), json=False, quiet=True)
    rc = cli._cmd_dossier(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "NVDA" in out
    assert "Test state" in out


def test_dossier_command_json_mode(tmp_path: Path, capsys) -> None:
    from research_assistant.dossier_io import Dossier, write_dossier_atomic
    d = Dossier(symbol="TSLA", conviction=0.4, state_md="test")
    write_dossier_atomic(d, tmp_path)

    args = MagicMock(ticker="TSLA", base=str(tmp_path), json=True, quiet=True)
    rc = cli._cmd_dossier(args)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["symbol"] == "TSLA"
    assert parsed["conviction"] == 0.4


# ---------------------------------------------------------------------------
# trace handler (no LLM calls)
# ---------------------------------------------------------------------------

def test_trace_command_missing_chain_returns_error(tmp_path: Path, capsys) -> None:
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    args = MagicMock(chain_id="nonexistent", base=str(tmp_path), json=False, quiet=True)
    rc = cli._cmd_trace(args)
    assert rc == 1


def test_trace_command_renders_existing_chain(tmp_path: Path, capsys) -> None:
    from research_assistant.trace_renderer import append_stage_event

    append_stage_event(
        chain_id="test_xyz",
        stage_id="stage_2_thesis",
        model="sonnet",
        tokens_in=100, tokens_out=50, cost_usd=0.001, latency_ms=500,
        parsed={
            "key_drivers": ["d1"],
            "risks": [],
            "evidence_anchors": [{"claim": "d1", "source": "tc1"}],
        },
        raw_response="{...}",
        traces_base=tmp_path / "traces",
    )
    args = MagicMock(chain_id="test_xyz", base=str(tmp_path), json=False, quiet=True)
    rc = cli._cmd_trace(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "stage_2_thesis" in out
    assert "tc1" in out


# ---------------------------------------------------------------------------
# main() routing
# ---------------------------------------------------------------------------

def test_main_unknown_subcommand_returns_2(capsys) -> None:
    """argparse rejects unknown subcommands before reaching handler dispatch."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["unknowncmd"])
    assert exc.value.code == 2


def test_main_keyboardinterrupt_returns_130(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def boom(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "_cmd_trace", boom)

    rc = cli.main(["--base", str(tmp_path), "trace", "x"])
    assert rc == 130


def test_main_generic_exception_returns_1(monkeypatch, tmp_path: Path) -> None:
    def boom(args):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cli, "_cmd_trace", boom)

    rc = cli.main(["--base", str(tmp_path), "trace", "x"])
    assert rc == 1
