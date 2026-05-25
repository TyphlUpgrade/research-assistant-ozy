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
  probe <TICKER> <Q>      Focused dossier-scoped question (no Skeptic by default)
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
import re
import sys
from pathlib import Path
from typing import Optional


def _setup_logging(quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_dotenv() -> None:
    """Populate os.environ from a `.env` file in the project root, if present.

    Searches the first ancestor of CWD that looks like a project root
    (contains pyproject.toml or .git/); falls back to CWD itself.
    Existing env vars take precedence (via os.environ.setdefault).

    Parser:
    - Skips blank lines and #-comments
    - Honors shell-style `export KEY=VALUE` by stripping the leading
      `export` keyword
    - Strips exactly one matching outer quote pair on the value
      (preserves nested quotes)
    """
    for parent in (Path.cwd(), *Path.cwd().parents):
        if not ((parent / "pyproject.toml").exists() or (parent / ".git").exists()):
            continue
        candidate = parent / ".env"
        if candidate.is_file():
            for raw in candidate.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export ") or line.startswith("export\t"):
                    line = line[len("export"):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
        return


def _load_prior_universe_snapshot(cache_path: Path) -> list[str]:
    """Return `discovered_universe` from a prior brief cache, or [] when
    the file is missing, unreadable, or pre-dates the field."""
    if not cache_path.exists():
        return []
    try:
        prior = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logging.warning("Prior brief cache unreadable, falling through: %s", exc)
        return []
    return list(prior.get("discovered_universe") or [])


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
    from research_assistant.edgar import (
        EdgarClient,
        load_insider_activity,
        load_institutional_ownership,
    )
    from research_assistant.orchestrator import research_ticker

    base = _resolve_base(args.base)
    symbol = args.ticker.upper()
    adapter = YFinanceAdapter()

    # Load market data + insider activity + institutional ownership in
    # parallel. One shared EdgarClient per command keeps the 5 req/sec
    # rate budget honest (independent clients would let each loader
    # have its own bucket, exceeding SEC's declared ceiling) AND
    # amortizes the 1MB company_tickers.json fetch across all loaders.
    if not args.quiet:
        print(
            f"Loading {symbol} data (yfinance + EDGAR Form 4 + 13F)…",
            file=sys.stderr,
        )
    async with EdgarClient() as edgar:
        ticker_data, headlines, insider_activity, institutional_ownership = await asyncio.gather(
            load_ticker_data(symbol, adapter),
            load_headlines(symbol, adapter, max_items=5),
            load_insider_activity(symbol, client=edgar),
            load_institutional_ownership(symbol, client=edgar),
        )
    if ticker_data.get("_data_quality") != "ok":
        print(
            f"ERROR: insufficient yfinance data for {symbol} "
            f"({ticker_data.get('_data_quality')}). Try a different symbol or retry.",
            file=sys.stderr,
        )
        return 1

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
    try:
        result = await research_ticker(
            symbol,
            world_state=world_state,
            ticker_data=ticker_data,
            headlines=headlines,
            base=base,
            insider_activity=insider_activity,
            institutional_ownership=institutional_ownership,
        )
    except RuntimeError as exc:
        # research_ticker embeds the chain_id in stage-parse failure messages
        # so the user can `python -m research_assistant trace <chain>`.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

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
# probe <TICKER> <QUESTION>
# ---------------------------------------------------------------------------

async def _cmd_probe(args: argparse.Namespace) -> int:
    if _require_api_key(args.quiet) is None:
        return 3

    from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
    from research_assistant.data_loader import (
        build_world_state_input,
        load_headlines,
        load_ticker_data,
    )
    from research_assistant.edgar import (
        EdgarClient,
        load_filing_excerpts,
        load_insider_activity,
        load_institutional_ownership,
    )
    from research_assistant.orchestrator import probe_ticker

    base = _resolve_base(args.base)
    symbol = args.ticker.upper()
    adapter = YFinanceAdapter()

    filing_form = getattr(args, "filing", None)
    if not args.quiet:
        sources = "yfinance + EDGAR Form 4 + 13F"
        if filing_form:
            sources += f" + {filing_form} excerpts"
        print(f"Loading {symbol} data ({sources})…", file=sys.stderr)

    # One shared EdgarClient across all loaders (see /research above for
    # rationale). Filing-excerpt fetch is opt-in via --filing.
    async with EdgarClient() as edgar:
        async def _maybe_excerpts():
            if not filing_form:
                return None
            return await load_filing_excerpts(
                symbol, filing_form, args.question, client=edgar,
            )

        (
            ticker_data, headlines, insider_activity,
            institutional_ownership, filing_excerpts,
        ) = await asyncio.gather(
            load_ticker_data(symbol, adapter),
            load_headlines(symbol, adapter, max_items=5),
            load_insider_activity(symbol, client=edgar),
            load_institutional_ownership(symbol, client=edgar),
            _maybe_excerpts(),
        )
    if ticker_data.get("_data_quality") != "ok":
        print(
            f"ERROR: insufficient yfinance data for {symbol} "
            f"({ticker_data.get('_data_quality')}).",
            file=sys.stderr,
        )
        return 1

    world_state: dict
    today_cache = base / "briefs" / f"{ticker_data.get('_date_et', '')}.json"
    if today_cache.exists():
        try:
            world_state = json.loads(today_cache.read_text()).get("world_state", {})
        except Exception:
            world_state = {}
    else:
        world_state = await build_world_state_input(adapter)

    if not args.quiet:
        print("Running probe (Sonnet)…", file=sys.stderr)

    try:
        result = await probe_ticker(
            symbol,
            args.question,
            world_state=world_state,
            ticker_data=ticker_data,
            headlines=headlines,
            base=base,
            insider_activity=insider_activity,
            institutional_ownership=institutional_ownership,
            filing_excerpts=filing_excerpts,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # probe_ticker embeds the chain_id in the message (see orchestrator);
        # surface it so the user can `python -m research_assistant trace <chain>`.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
    else:
        print(_render_probe_result(result))
    return 0


def _render_probe_result(result) -> str:
    lines = [
        f"## {result.symbol} — Probe (chain: {result.chain_id})",
        "",
        f"**Question:** {result.question}",
        "",
        f"**Answer:** {result.answer}",
        "",
    ]
    if result.evidence_anchors:
        lines.append("**Evidence anchors:**")
        for a in result.evidence_anchors:
            claim = a.get("claim", "")
            source = a.get("source", "")
            lines.append(f"- {claim}  [anchor: {source}]")
        lines.append("")
    if result.closes_questions:
        lines.append("**Closed open questions (removed from dossier):**")
        for q in result.closes_questions:
            lines.append(f"- {q}")
        lines.append("")
    if result.new_open_questions:
        lines.append("**New open questions (appended to dossier):**")
        for q in result.new_open_questions:
            lines.append(f"- {q}")
        lines.append("")
    lines.append(f"**Cost so far this session:** ${result.cost_usd:.4f}")
    lines.append("")
    lines.append(
        f"Dossier updated at `.research/tickers/{result.symbol}.md`. "
        f"Run `python -m research_assistant trace {result.chain_id}` for the trace."
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
        write_brief_cache,
        _deserialize_setup,
    )
    from research_assistant.data_loader import (
        build_world_state_input,
        load_watchlist_data,
    )
    from research_assistant.edgar import load_insider_activities_batch
    from research_assistant.screeners import (
        compute_sector_performance,
        run_screeners_and_journal,
    )
    from research_assistant.universe import discover_universe

    base = _resolve_base(args.base)
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    cache_path = base / "briefs" / f"{today_et}.json"

    brief: Brief
    # Variables threaded into the screener pipeline. On the cache-miss branch
    # these are populated by the build-brief path. On the cache-hit branch
    # they are re-loaded after the cached brief is deserialized (Option α
    # per plan §1F — guaranteed-fresh data on every /brief invocation).
    pipeline_universe: list[str] = []
    pipeline_world_state: dict = {}
    pipeline_ticker_data: dict[str, dict] = {}
    cache_branch: str

    if cache_path.exists() and not args.refresh:
        cache_branch = "hit"
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
            discovered_universe=raw.get("discovered_universe", []),
            # PR 1.3: backward-compatible default for pre-PR-1.3 caches that
            # never wrote `setups`. The pipeline will repopulate this list
            # below; the cached value is only used as a fallback when
            # --skip-screeners is set.
            setups=[_deserialize_setup(s) for s in raw.get("setups", []) or []],
        )

        # Option α: re-load ticker_data + world_state on cache-hit so the
        # screener pipeline always sees fresh data. ~5s budget at 30 tickers.
        if not args.skip_screeners:
            adapter = YFinanceAdapter()
            pipeline_universe = brief.discovered_universe or []
            if pipeline_universe:
                if not args.quiet:
                    print(
                        f"Re-loading ticker data for {len(pipeline_universe)} "
                        "tickers for screener pipeline (cache-hit path)…",
                        file=sys.stderr,
                    )
                news_seed = pipeline_universe[:5]
                world_state_input, (tickers_with_data, _hl) = await asyncio.gather(
                    build_world_state_input(adapter, watchlist_news_for=news_seed),
                    load_watchlist_data(pipeline_universe, adapter),
                )
                pipeline_ticker_data = tickers_with_data
                # Merge raw sector_performance from the world-state input
                # (which has the per-ETF return snapshots) into the brief's
                # world_state dict so screeners that read
                # `world_state["sector_performance"]` get the raw returns,
                # not the LLM-derived bias/strength tags.
                pipeline_world_state = dict(brief.world_state)
                pipeline_world_state["sector_performance"] = (
                    world_state_input.get("sector_performance")
                    or compute_sector_performance(pipeline_ticker_data)
                )
    else:
        cache_branch = "miss"
        adapter = YFinanceAdapter()
        pins = load_watchlist(base)

        # Universe resolution. To keep `--refresh` deterministic within the
        # ET day, reuse the prior cache's `discovered_universe` snapshot when
        # one is available. `--rediscover` and `--static-only` force fresh
        # resolution (overriding any snapshot).
        prior_snapshot: list[str] = []
        if not args.rediscover and not args.static_only:
            prior_snapshot = _load_prior_universe_snapshot(cache_path)

        if args.static_only:
            universe = pins
            source_label = "static watchlist"
        elif prior_snapshot:
            universe = prior_snapshot
            source_label = "snapshot reused from prior cache"
        else:
            universe = await discover_universe(pins=pins, cap=30)
            pins_set = {p.upper() for p in pins}
            pinned_in_universe = sum(1 for t in universe if t in pins_set)
            discovered_count = len(universe) - pinned_in_universe
            source_label = f"{pinned_in_universe} pinned + {discovered_count} discovered"
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
        if not args.quiet:
            print(
                f"Fetching Form 4 insider activity for {len(universe)} tickers "
                f"(~{len(universe) // 5 + 1}s at 5 req/sec)…",
                file=sys.stderr,
            )
        # Shared EdgarClient across the whole batch so the CIK ticker
        # index is fetched once for all 30 tickers.
        from research_assistant.edgar import EdgarClient
        async with EdgarClient() as edgar:
            (tickers_with_data, headlines_per_ticker), insider_activities = await asyncio.gather(
                load_watchlist_data(universe, adapter),
                load_insider_activities_batch(universe, client=edgar),
            )
        brief = await build_brief(
            market_context=market_context,
            universe=universe,
            watchlist_tickers_with_data=tickers_with_data,
            headlines_per_ticker=headlines_per_ticker,
            insider_activities=insider_activities,
            research_base=base,
        )

        # Pipeline inputs for cache-miss. Merge raw sector_performance from
        # the build_world_state_input output into the (LLM-derived) brief
        # world_state so screeners see per-ETF returns.
        pipeline_universe = universe
        pipeline_ticker_data = tickers_with_data
        pipeline_world_state = dict(brief.world_state)
        pipeline_world_state["sector_performance"] = (
            market_context.get("sector_performance")
            or compute_sector_performance(pipeline_ticker_data)
        )

    # PR 1.3 — iter-1 CRITICAL #1 fix: BOTH branches invoke the screener
    # pipeline. The --skip-screeners flag is the operator escape hatch for
    # the cache-hit fast path (skips the ~5s loader re-fetch above too).
    if args.skip_screeners:
        print(
            "[skip-screeners] Setup-finder skipped per --skip-screeners flag; "
            "brief shows last-known setups from cache only.",
            file=sys.stderr,
        )
    else:
        setups = await run_screeners_and_journal(
            world_state=pipeline_world_state,
            universe=pipeline_universe,
            ticker_data=pipeline_ticker_data,
            research_base=base,
            cache_branch=cache_branch,
        )
        brief.setups = list(setups)
        # Re-write the cache so the on-disk brief reflects the freshly
        # computed setups (both branches: cache-miss overwrites the
        # build_brief-written file; cache-hit overwrites the prior day's
        # cache with today's setups).
        write_brief_cache(brief, base)

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
# defender-check
# ---------------------------------------------------------------------------

_CHAIN_ID_RE = re.compile(r"^[A-Za-z0-9T\-]{8,64}$")


def _load_anchors_from_chain(
    traces_base: Path,
    chain_id: str,
    *,
    ticker: Optional[str] = None,
) -> list[dict]:
    """Pull every Stage-2 thesis's evidence_anchors out of the trace JSONL
    for `chain_id`. Returns the flattened list; raises FileNotFoundError if
    the chain has no trace on disk, or ValueError if `chain_id` doesn't match
    the canonical format (defense-in-depth — prevents glob metacharacters
    or path separators from interacting with rglob).

    When `ticker` is supplied, events are filtered to those whose `symbol`
    matches (case-insensitive). Events without a `symbol` field — older
    /research traces written before the field existed — are included so
    single-ticker chains stay backward-compatible.
    """
    if not _CHAIN_ID_RE.match(chain_id):
        raise ValueError(
            f"Invalid chain_id format: {chain_id!r} "
            "(expected 8-64 alphanumeric/'T'/'-' chars)"
        )
    matches = list(traces_base.rglob(f"{chain_id}.jsonl"))
    if not matches:
        raise FileNotFoundError(
            f"No trace for chain {chain_id} under {traces_base}"
        )
    target = ticker.upper() if ticker else None
    anchors: list[dict] = []
    for path in matches:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("stage_id") not in ("stage_2_thesis", "stage_2_probe"):
                continue
            if target is not None:
                event_symbol = event.get("symbol")
                if event_symbol is not None and event_symbol.upper() != target:
                    continue
            parsed = event.get("parsed") or {}
            anchors.extend(parsed.get("evidence_anchors") or [])
    return anchors


def _cmd_defender_check(args: argparse.Namespace) -> int:
    from research_assistant.orchestrator import should_invoke_defender

    base = _resolve_base(args.base)
    traces_base = base / "traces"
    try:
        anchors = _load_anchors_from_chain(
            traces_base, args.chain_id, ticker=args.ticker,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    fire = should_invoke_defender(
        prior_turn_had_recommendation=True,
        user_message=args.user_message,
        prior_evidence_anchors=anchors,
    )
    print("true" if fire else "false")
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
# alerts review (PR 1.3)
# ---------------------------------------------------------------------------

_WINDOW_RE = re.compile(r"^(\d+)d$")


def _parse_window_days(window: str) -> int:
    """Parse `Nd` → N. Raises ValueError on malformed input."""
    m = _WINDOW_RE.match(window.strip())
    if not m:
        raise ValueError(
            f"Invalid --window: {window!r}. Expected format like '7d', '30d', '90d'."
        )
    return int(m.group(1))


def _statistics_median(values: list[float]) -> float:
    """Plain median over a non-empty list. Inline rather than `statistics`
    import for one call-site."""
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _render_alerts_review(
    alerts: list[dict],
    *,
    screener_filter: Optional[str] = None,
) -> str:
    """Per-screener tally rendering for `alerts review`. Reports count,
    hit-rate (% with return_30d > 0), median return_30d. Top/bottom-3 winners
    deferred to PR 3.1 — minimum-viable here per plan §1.3."""
    if screener_filter:
        alerts = [a for a in alerts if a.get("screener") == screener_filter]
    if not alerts:
        return "(no alerts in window)"

    by_screener: dict[str, list[dict]] = {}
    for a in alerts:
        by_screener.setdefault(a.get("screener", "?"), []).append(a)

    lines: list[str] = []
    for name in sorted(by_screener):
        rows = by_screener[name]
        count = len(rows)
        returns_30d = [
            r["return_30d"] for r in rows
            if isinstance(r.get("return_30d"), (int, float))
        ]
        if returns_30d:
            hit_rate = sum(1 for r in returns_30d if r > 0) / len(returns_30d)
            median = _statistics_median(returns_30d)
            lines.append(
                f"- **{name}** — count={count}  "
                f"hit_rate_30d={hit_rate:.0%}  "
                f"median_30d={median:+.2%}  "
                f"(enriched={len(returns_30d)}/{count})"
            )
        else:
            lines.append(
                f"- **{name}** — count={count}  "
                f"(no enriched horizon returns yet)"
            )
    return "\n".join(lines)


def _export_alerts_csv(alerts: list[dict], path: Path) -> None:
    """Flat CSV: one row per alert. Stable column order so operator
    spreadsheets stay readable across runs."""
    import csv
    fieldnames = [
        "asof", "ticker", "screener", "entry_price",
        "return_7d", "return_30d", "return_90d",
        "enriched_at", "created_at",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in alerts:
            writer.writerow({k: row.get(k) for k in fieldnames})


async def _cmd_alerts(args: argparse.Namespace) -> int:
    """Dispatcher for `alerts <subcommand>`. Currently only `review`."""
    from datetime import date, timedelta

    from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
    from research_assistant.journal import enrich_window, read_alerts_window

    if args.alerts_cmd != "review":
        print(f"Unknown alerts subcommand: {args.alerts_cmd}", file=sys.stderr)
        return 2

    try:
        days = _parse_window_days(args.window)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    base = _resolve_base(args.base)
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    alerts = read_alerts_window(base, start_date.isoformat(), end_date.isoformat())

    # Lazy-enrich horizons (plan §B1). Adapter exposes async fetch_price_at.
    if alerts:
        adapter = YFinanceAdapter()
        adapter.research_base = base
        try:
            await enrich_window(alerts, adapter)
        except Exception as exc:
            # Enrichment failures are non-fatal — render what we have.
            logging.warning("alerts review enrichment failed: %s", exc)
        # Re-read so LWW-enrichment rows are folded back in
        alerts = read_alerts_window(base, start_date.isoformat(), end_date.isoformat())

    if args.export_csv:
        _export_alerts_csv(alerts, Path(args.export_csv))
        print(f"Wrote {len(alerts)} rows to {args.export_csv}", file=sys.stderr)

    if args.json:
        print(json.dumps(alerts, indent=2, default=str))
    else:
        header = (
            f"# Alerts review — window {start_date.isoformat()} → "
            f"{end_date.isoformat()} ({days}d)"
        )
        print(header)
        print()
        print(_render_alerts_review(alerts, screener_filter=args.screener))
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

    pp = sub.add_parser(
        "probe",
        help="Focused dossier-scoped question (cheaper than /research)",
    )
    pp.add_argument("ticker", help="Stock symbol — must already have a dossier")
    pp.add_argument("question", help="Focused question to answer against the dossier")
    pp.add_argument(
        "--filing",
        metavar="FORM",
        help="Fetch the latest filing of the given form (e.g. 10-K, 10-Q, 8-K) and "
             "inject paragraphs matching the question keywords. Cited anchors "
             "(edgar:<form>:<acc>:para_N) persist into the trace for Defender.",
    )

    pb = sub.add_parser("brief", help="Morning summary over watchlist + dynamic universe")
    pb.add_argument("--refresh", action="store_true", help="Bypass today's cache and rebuild")
    pb.add_argument("--ticker", help="Drill-down on a specific ticker from the brief")
    pb.add_argument(
        "--static-only",
        action="store_true",
        help="Use only .research/watchlist.txt (skip Yahoo screener discovery)",
    )
    pb.add_argument(
        "--rediscover",
        action="store_true",
        help="Re-run Yahoo screener discovery instead of reusing today's cached snapshot",
    )
    pb.add_argument(
        "--skip-screeners",
        action="store_true",
        help="Skip the setup-finder pipeline on this invocation",
    )

    # alerts review — setup-finder operator surface (PR 1.3)
    pa = sub.add_parser(
        "alerts", help="Operator surface for the setup-finder alert journal",
    )
    pa_sub = pa.add_subparsers(dest="alerts_cmd", required=True)
    pa_review = pa_sub.add_parser(
        "review",
        help="Per-screener tally over a window (default 30d) with horizon returns",
    )
    pa_review.add_argument(
        "--window",
        default="30d",
        help="Window size in days, e.g. 7d, 30d, 90d (default: 30d)",
    )
    pa_review.add_argument(
        "--screener",
        default=None,
        help="Filter to a single screener name (e.g. sector_rotation)",
    )
    pa_review.add_argument(
        "--export-csv",
        default=None,
        help="Optional path to write a flat CSV of the windowed alerts",
    )

    pt = sub.add_parser("trace", help="Render a cascade trace by chain_id")
    pt.add_argument("chain_id", help="Chain ID printed at end of a research/brief result")

    pd = sub.add_parser("dossier", help="Print the current per-ticker dossier")
    pd.add_argument("ticker", help="Stock symbol")

    pdc = sub.add_parser(
        "defender-check",
        help="Decide whether the Defender subagent should fire on user pushback",
    )
    pdc.add_argument("--user-message", required=True, help="The user's pushback text")
    pdc.add_argument(
        "--chain-id",
        required=True,
        help="Chain ID of the prior research result whose anchors should verify citations",
    )
    pdc.add_argument(
        "--ticker",
        default=None,
        help="Scope brief chains to one survivor — verify against only that ticker's "
             "Stage-2 anchors. Omit for /research chains (single survivor).",
    )

    return p


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
    args = _build_parser().parse_args(argv)
    _setup_logging(args.quiet)

    handlers = {
        "research":       lambda a: asyncio.run(_cmd_research(a)),
        "probe":          lambda a: asyncio.run(_cmd_probe(a)),
        "brief":          lambda a: asyncio.run(_cmd_brief(a)),
        "alerts":         lambda a: asyncio.run(_cmd_alerts(a)),
        "trace":          _cmd_trace,
        "dossier":        _cmd_dossier,
        "defender-check": _cmd_defender_check,
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
