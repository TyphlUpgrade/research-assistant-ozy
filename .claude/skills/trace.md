---
name: trace
description: Render the cascade trace for a given chain_id as human-readable terminal output. Surfaces per-stage events (Stage 2, Stage 3, optional Defender), evidence-anchor citations per claim, flags claims without anchors as visibility regressions. Usage `/trace <chain_id>` — typically the chain_id printed at end of a `/research` or `/brief` result.
---

When the user runs `/trace <chain_id>` or says "show me the trace for X" / "what was the reasoning on X":

## Invocation

```bash
python -m research_assistant trace <CHAIN_ID>
```

The CLI's stdout is the rendered trace — markdown formatted per-stage events with anchors inline. Show it directly.

If the chain isn't found, the CLI exits 1 and lists the 5 most recent chains on stderr to help the user. Surface those alternatives.

## Output format

Each cascade stage appears as a section:

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

## Visibility regression detection

The CLI automatically flags any `key_drivers` or `risks` claim without a matching `evidence_anchors` entry with `⚠ ... [NO ANCHOR — visibility regression]`. If you see those flags, surface them prominently — they are exactly the visibility-axis failures the quality contract exists to catch.

## Quality contract surface

`/trace` is the user-facing visibility-axis enforcement: every Recommendation should produce a trace; every trace should render cleanly; orphan-claim flags mean the assistant made a claim it can't anchor. This is how the user sees and audits the assistant's reasoning chain.
