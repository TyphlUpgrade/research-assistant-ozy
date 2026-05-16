---
name: trace
description: Render the cascade trace for a given chain_id as human-readable terminal output. Surfaces per-stage events (Stage 2, Stage 3, optional Defender), evidence-anchor citations per claim, and flags claims without anchors as visibility regressions. Usage `/trace <chain_id>` — typically the chain_id printed at the end of a `/research` result.
---

When the user runs `/trace <chain_id>`:

1. **Parse the chain_id** from the user's message. Format is typically `YYYYMMDDTHHMMSS-XXXXXX` (timestamp + 6-hex suffix).

2. **Invoke `research_assistant.trace_renderer.render_trace(chain_id, traces_base=Path(".research/traces"))`** to read the JSONL file and produce the rendered markdown.

3. **Display the output directly** in the terminal. Each stage block looks like:

   ```
   ## stage_2_thesis (claude-sonnet-4-6)
   - chain_id: `20260514T143022-abc123`
   - timestamp: 2026-05-14T14:30:22Z
   - tokens: in=4823 out=412 cost=$0.0203 latency=2451ms
   - evidence anchors (per-claim citations):
       - `Data-center revenue +27% QoQ` ← tool_call_nv001
       - `H100→H200 transition smooth` ← tool_call_nv002
   - thesis_text: NVDA's Q2 segment results show data-center revenue accelerating…
   - conviction_score: 0.72
   ```

4. **Visibility regression detection**: any driver or risk in the parsed output that lacks a matching evidence_anchor is flagged inline with `⚠ ... [NO ANCHOR — visibility regression]`. This is the visibility-axis test of the quality contract — the user sees IMMEDIATELY when the assistant has made a claim it can't anchor.

5. **Error handling**: if no trace file exists for the cited chain_id, return "No trace found for chain_id `<id>`. Available recent chains: <list>" and list the 5 most recent chain_ids from `.research/traces/`.

## Quality contract enforcement

The `/trace` command is the visibility-axis enforcement surface. Every Recommendation produces a trace; every trace must render cleanly; any anchorless claim must be flagged. If `/trace` shows clean output with all anchors present, the visibility floor is met. If `/trace` shows `[NO ANCHOR — visibility regression]` flags, the v1 quality contract is broken for that chain.
