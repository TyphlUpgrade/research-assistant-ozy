"""
Single CLI entrypoint for research_assistant.

Used by CC skills as the canonical Python invocation — skill markdown
files tell Claude to call `python -m research_assistant <subcommand>`
and parse the output. Avoids each skill inventing its own Python
invocation incantation.

Also usable directly from a terminal for dev work, and as the
integration point for a v2 Discord bot (the bot calls the same CLI).

Subcommands:
  research <TICKER>       Full single-ticker DD (Stage 2 + Stage 3 + dossier write)
  brief                   Morning summary (Stage 0 + 1 + parallel Stage 2 over watchlist)
  trace <CHAIN_ID>        Render a cascade trace as human-readable text
  dossier <TICKER>        Print the current per-ticker dossier markdown

Common flags:
  --json                  Emit JSON instead of human-readable text (machine-readable)
  --base PATH             Override the .research/ data directory (default: ./.research)
  --quiet                 Suppress non-essential stderr output

Exit codes:
  0   Success
  1   Generic error (parse fail, missing data, etc.) — stderr explains
  2   Usage error (missing args, unknown subcommand)
  3   Missing API key for an LLM-invoking subcommand
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional


def _setup_logging(quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _require_api_key(quiet: bool) -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set. Export it before invoking this command.",
            file=sys.stderr,
        )
        return None
    return key


def _resolve_base(arg_base: Optional[str]) -> Path:
    if arg_base:
        return Path(arg_base).resolve()
    # Default: .research/ in the cwd (typically the research-assistant repo)
    return (Path.cwd() / ".research").resolve()


# ---------------------------------------------------------------------------
# research <TICKER>
# ---------------------------------------------------------------------------

async def _cmd_research(args: argparse.Namespace) -> int:
    if _require_api_key(args.quiet) is None:
        return 3

    from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
    from research_assistant.data_loader import (
        build_world_state_input,
        load_headlines,
        load_ticker_data,
    )
    from research_assistant.orchestrator import research_ticker

    base = _resolve_base(args.base)
    symbol = args.ticker.upper()
    adapter = YFinanceAdapter()

    # Load market data
    if not args.quiet:
        print(f"Loading {symbol} data via yfinance…", file=sys.stderr)
    ticker_data = await load_ticker_data(symbol, adapter)
    if ticker_data.get("_data_quality") != "ok":
        print(
            f"ERROR: insufficient yfinance data for {symbol} "
            f"({ticker_data.get('_data_quality')}). Try a different symbol or retry.",
            file=sys.stderr,
        )
        return 1
    headlines = await load_headlines(symbol, adapter, max_items=5)

    # World state — by default, build a fresh small one for /research
    # (cheaper than full Stage 0; brief.py handles full Stage 0).
    # If a today's brief cache exists, reuse its world_state for free.
    world_state: dict
    today_cache = base / "briefs" / f"{ticker_data.get('_date_et', '')}.json"
    if today_cache.exists():
        try:
            world_state = json.loads(today_cache.read_text()).get("world_state", {})
        except Exception:
            world_state = {}
    else:
        world_state = await build_world_state_input(adapter)

    # Run the mini-cascade
    if not args.quiet:
        print("Running Stage 2 (thesis) + Stage 3 (Skeptic)…", file=sys.stderr)
    result = await research_ticker(
        symbol,
        world_state=world_state,
        ticker_data=ticker_data,
        headlines=headlines,
        base=base,
    )

    if args.json:
        print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    else:
        print(_render_research_result(result))
    return 0


def _render_research_result(result) -> str:
    """Human-readable rendering matching the skill spec output format."""
    lines = [
        f"## {result.symbol} — Research result (chain: {result.chain_id})",
        "",
        f"**Thesis (Sonnet):** {result.thesis_text}",
        "",
        f"**Conviction:** {result.conviction_score:.2f} → {result.adjusted_score:.2f} "
        f"(post-Skeptic adjustment)",
        "",
    ]
    anchors_by_claim = {
        a.get("claim", "").strip().lower(): a.get("source", "")
        for a in (result.evidence_anchors or [])
    }

    def _anchor_for(claim: str) -> str:
        s = anchors_by_claim.get(claim.strip().lower())
        if s:
            return f"[anchor: {s}]"
        # Fuzzy: if any anchor claim contains/contained-in this claim
        for ac, src in anchors_by_claim.items():
            if ac and (ac in claim.lower() or claim.lower() in ac):
                return f"[anchor: {src}]"
        return "[NO ANCHOR — visibility regression]"

    if result.key_drivers:
        lines.append("**Key drivers:**")
        for d in result.key_drivers:
            lines.append(f"- {d}  {_anchor_for(d)}")
        lines.append("")
    if result.risks:
        lines.append("**Risks (named):**")
        for r in result.risks:
            lines.append(f"- {r}  {_anchor_for(r)}")
        lines.append("")
    if result.critique_text:
        lines.append(f"**Skeptic critique:** {result.critique_text}")
        lines.append("")
    if result.flagged_risks:
        lines.append("**Flagged additional risks:**")
        for r in result.flagged_risks:
            lines.append(f"- {r}")
        lines.append("")
    if result.open_questions:
        lines.append("**Open questions to probe:**")
        for q in result.open_questions:
            lines.append(f"- {q}")
        lines.append("")
    lines.append(f"**News reactivity flag:** {result.news_reactivity_flag}")
    lines.append(f"**Cost so far this session:** ${result.cost_usd:.4f}")
    lines.append("")
    lines.append(
        f"Dossier appended at `.research/tickers/{result.symbol}.md`. "
        f"Run `python -m research_assistant trace {result.chain_id}` for the full cascade trace."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------

async def _cmd_brief(args: argparse.Namespace) -> int:
    if _require_api_key(args.quiet) is None:
        return 3

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
    from research_assistant.brief import (
        Brief,
        BriefItem,
        build_brief,
        load_watchlist,
        render_brief_drill_down,
        render_brief_top_level,
    )
    from research_assistant.data_loader import (
        build_world_state_input,
        load_watchlist_data,
    )
    from research_assistant.universe import discover_universe

    base = _resolve_base(args.base)
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    cache_path = base / "briefs" / f"{today_et}.json"

    brief: Brief

    if cache_path.exists() and not args.refresh:
        if not args.quiet:
            print(f"Using cached brief for {today_et} (ET). "
                  f"Pass --refresh to rebuild.", file=sys.stderr)
        raw = json.loads(cache_path.read_text())
        brief = Brief(
            date_et=raw["date_et"],
            chain_id=raw["chain_id"],
            world_state=raw["world_state"],
            items=[BriefItem(**i) for i in raw["items"]],
            cost_usd=raw.get("cost_usd", 0.0),
        )
    else:
        adapter = YFinanceAdapter()
        pins = load_watchlist(base)
        if args.static_only:
            universe = pins
            source_label = "static watchlist"
        else:
            universe = await discover_universe(pins=pins, cap=30)
            source_label = f"{len(pins)} pinned + {len(universe) - len(pins)} discovered"
        if not universe:
            print(
                f"ERROR: universe is empty. Add tickers to {base}/watchlist.txt "
                "one-per-line, or drop --static-only so Yahoo screeners can fill in.",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            print(f"Building brief over {len(universe)} tickers ({source_label})…",
                  file=sys.stderr)

        # Market context for Stage 0 input — keep headlines anchored to pinned
        # names (or the first 5 universe entries when no pins exist) to bound
        # the Stage 0 news cost.
        news_seed = pins[:5] if pins else universe[:5]
        market_context = await build_world_state_input(
            adapter, watchlist_news_for=news_seed,
        )
        tickers_with_data, headlines_per_ticker = await load_watchlist_data(
            universe, adapter
        )
        brief = await build_brief(
            market_context=market_context,
            watchlist_tickers_with_data=tickers_with_data,
            headlines_per_ticker=headlines_per_ticker,
            research_base=base,
        )

    if args.ticker:
        # Drill-down view
        text = render_brief_drill_down(brief, args.ticker)
        if args.json:
            # JSON of just the drilled item
            item = next((i for i in brief.items if i.ticker == args.ticker.upper()), None)
            print(json.dumps(
                dataclasses.asdict(item) if item else {"error": f"no item {args.ticker}"},
                indent=2, default=str,
            ))
        else:
            print(text)
    else:
        # Top-level view
        if args.json:
            print(json.dumps({
                "date_et": brief.date_et,
                "chain_id": brief.chain_id,
                "world_state": brief.world_state,
                "items": [dataclasses.asdict(i) for i in brief.items],
                "cost_usd": brief.cost_usd,
            }, indent=2, default=str))
        else:
            print(render_brief_top_level(brief))
    return 0


# ---------------------------------------------------------------------------
# trace <CHAIN_ID>
# ---------------------------------------------------------------------------

def _cmd_trace(args: argparse.Namespace) -> int:
    from research_assistant.trace_renderer import render_trace

    base = _resolve_base(args.base)
    traces_base = base / "traces"
    try:
        rendered = render_trace(args.chain_id, traces_base)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        # List 5 most recent chains for user convenience
        recent = sorted(traces_base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        if recent:
            print("\nMost recent chains:", file=sys.stderr)
            for f in recent:
                print(f"  {f.stem}", file=sys.stderr)
        return 1
    print(rendered)
    return 0


# ---------------------------------------------------------------------------
# dossier <TICKER>
# ---------------------------------------------------------------------------

def _cmd_dossier(args: argparse.Namespace) -> int:
    from research_assistant.dossier_io import read_dossier

    base = _resolve_base(args.base)
    dossier = read_dossier(args.ticker, base)
    if dossier is None:
        print(
            f"No dossier yet for {args.ticker.upper()}. "
            f"Run `python -m research_assistant research {args.ticker}` first.",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(dataclasses.asdict(dossier), indent=2, default=str))
    else:
        path = base / "tickers" / f"{args.ticker.upper()}.md"
        if path.exists():
            print(path.read_text())
        else:
            # Dossier object exists but file doesn't (shouldn't normally happen);
            # render minimal view from the in-memory dossier.
            print(f"# {dossier.symbol}")
            print(f"\n## State\n{dossier.state_md}")
    return 0


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="research_assistant",
        description="Stock Research Assistant CLI — surfaces invoked by CC skills and dev terminals.",
    )
    # Top-level flags that apply to all subcommands
    p.add_argument("--base", help="Override .research/ data directory (default: ./.research)")
    p.add_argument("--quiet", action="store_true", help="Suppress non-essential stderr output")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("research", help="Single-ticker full DD")
    pr.add_argument("ticker", help="Stock symbol, e.g. NVDA")

    pb = sub.add_parser("brief", help="Morning summary over watchlist + dynamic universe")
    pb.add_argument("--refresh", action="store_true", help="Bypass today's cache and rebuild")
    pb.add_argument("--ticker", help="Drill-down on a specific ticker from the brief")
    pb.add_argument(
        "--static-only",
        action="store_true",
        help="Use only .research/watchlist.txt (skip Yahoo screener discovery)",
    )

    pt = sub.add_parser("trace", help="Render a cascade trace by chain_id")
    pt.add_argument("chain_id", help="Chain ID printed at end of a research/brief result")

    pd = sub.add_parser("dossier", help="Print the current per-ticker dossier")
    pd.add_argument("ticker", help="Stock symbol")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.quiet)

    handlers = {
        "research": lambda a: asyncio.run(_cmd_research(a)),
        "brief":    lambda a: asyncio.run(_cmd_brief(a)),
        "trace":    _cmd_trace,
        "dossier":  _cmd_dossier,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        print(f"Unknown subcommand: {args.cmd}", file=sys.stderr)
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.exception("Unhandled error")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
