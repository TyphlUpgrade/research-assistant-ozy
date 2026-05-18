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


def test_parser_brief_with_drilldown() -> None:
    parser = cli._build_parser()
    ns = parser.parse_args(["brief", "--ticker", "TSLA", "--refresh"])
    assert ns.cmd == "brief"
    assert ns.ticker == "TSLA"
    assert ns.refresh is True


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
