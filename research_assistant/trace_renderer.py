"""
Cascade trace renderer (visibility-axis surface).

Reads `.research/traces/<date>/<chain_id>.jsonl` and emits a human-readable
per-stage summary with evidence anchors surfaced inline.

Each line of the JSONL is one StageEvent (mirrors Ozy's cascade trace schema):
  - stage_id        : "stage_2_thesis" | "stage_3_skeptic" | ...
  - chain_id        : the parent chain UUID
  - model           : "claude-sonnet-4-6" etc.
  - timestamp       : ISO 8601 UTC
  - tokens_in / tokens_out : int
  - cost_usd        : float
  - latency_ms      : int
  - raw_response_truncated : string (first ~200 chars)
  - parsed          : dict (the parsed JSON output of the stage)
  - error           : string | null

The visibility regression test (`tests/test_quality_contract.py`) injects a
synthetic claim without an anchor in the trace and asserts the renderer
flags it explicitly with `[NO ANCHOR — visibility regression]`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


_NO_ANCHOR_MARKER = "[NO ANCHOR — visibility regression]"


def _format_stage_event(event: dict) -> str:
    """Format one StageEvent as a human-readable block."""
    lines = []
    lines.append(f"## {event.get('stage_id', '<unknown>')} ({event.get('model', '?')})")
    lines.append(f"- chain_id: `{event.get('chain_id', '?')}`")
    lines.append(f"- timestamp: {event.get('timestamp', '?')}")
    lines.append(
        f"- tokens: in={event.get('tokens_in', 0)} out={event.get('tokens_out', 0)} "
        f"cost=${event.get('cost_usd', 0.0):.4f} latency={event.get('latency_ms', 0)}ms"
    )

    err = event.get("error")
    if err:
        lines.append(f"- **ERROR:** {err}")
        return "\n".join(lines) + "\n"

    parsed = event.get("parsed", {})

    # Surface evidence anchors inline (per-claim citations from Stage 2)
    anchors = parsed.get("evidence_anchors")
    if anchors:
        lines.append("- evidence anchors (per-claim citations):")
        for a in anchors:
            claim = a.get("claim", "<no claim>")
            source = a.get("source", _NO_ANCHOR_MARKER)
            if not source or source.strip() == "":
                source = _NO_ANCHOR_MARKER
            lines.append(f"    - `{claim}` ← {source}")

    # Flag any claim in the stage output without a matching anchor (visibility regression).
    # Strict exact-match (after case+whitespace normalization) — fuzzy bidirectional
    # substring was too lax and let paraphrased anchors slip through. The Stage 2 prompt
    # now requires verbatim claim text in anchors, so exact match is the right contract.
    drivers = parsed.get("key_drivers", []) or []
    risks = parsed.get("risks", []) or []
    anchor_claims = {
        a.get("claim", "").strip().lower()
        for a in (anchors or [])
        if a.get("claim") and a.get("source")  # empty source = no anchor
    }
    for claim_set, label in [(drivers, "driver"), (risks, "risk")]:
        for c in claim_set:
            c_norm = (c or "").strip().lower()
            if c_norm and c_norm not in anchor_claims:
                lines.append(f"- ⚠ {label}: `{c}` {_NO_ANCHOR_MARKER}")

    # Other parsed fields, lightly rendered
    for k, v in parsed.items():
        if k in ("evidence_anchors", "key_drivers", "risks"):
            continue
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "…"
        lines.append(f"- {k}: {v}")

    return "\n".join(lines) + "\n"


def render_trace(chain_id: str, traces_base: Path) -> str:
    """
    Find and render the trace for a given chain_id. Searches all
    date-subdirs of `traces_base` (typically `.research/traces/`).

    Returns a multi-section markdown string. Raises FileNotFoundError if no
    matching trace file exists.
    """
    matches = list(traces_base.rglob(f"*{chain_id}*.jsonl"))
    if not matches:
        raise FileNotFoundError(f"No trace file found for chain_id={chain_id} under {traces_base}")
    if len(matches) > 1:
        # Multiple matches: pick the most recent
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    path = matches[0]
    blocks = [f"# Cascade trace — chain `{chain_id}`", f"_(source: `{path}`)_", ""]
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                blocks.append(f"## [unparseable line]\n```\n{line[:200]}\n```\n")
                continue
            blocks.append(_format_stage_event(event))
    return "\n".join(blocks)


def append_stage_event(
    chain_id: str,
    stage_id: str,
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    parsed: Optional[dict],
    raw_response: Optional[str],
    traces_base: Path,
    error: Optional[str] = None,
    symbol: Optional[str] = None,
) -> Path:
    """Append one StageEvent to the chain's JSONL. Returns the file path.

    `symbol` scopes the event to a specific ticker so multi-survivor brief
    chains (which share one chain_id across N Stage-2 events) can be
    filtered downstream (e.g., Defender citation verification picks only
    the survivor under disagreement).
    """
    now = datetime.now(timezone.utc)
    date_dir = traces_base / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    path = date_dir / f"{chain_id}.jsonl"

    event = {
        "stage_id": stage_id,
        "chain_id": chain_id,
        "model": model,
        "timestamp": now.isoformat(),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "raw_response_truncated": (raw_response or "")[:200],
        "parsed": parsed,
        "error": error,
        "symbol": symbol,
    }
    with path.open("a") as f:
        f.write(json.dumps(event) + "\n")
    return path
