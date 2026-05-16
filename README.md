# Stock Research Assistant

A CC-native stock research assistant that reuses curated Ozymandias v3 intelligence modules as an imported Python library.

## Status

v1 in development. See `.omc/plans/2026-05-14-research-assistant.md` in the Ozy repo for the implementation plan.

## Quality contract (v1 hard floor)

1. **Factual accuracy** — no claim of current state without a preceding tool call
2. **Backbone** — Defender subagent fires on user pushback without new evidence
3. **Depth** — outputs surface what 5 min of Googling wouldn't
4. **Visibility** — every claim cites an evidence anchor; cascade traces user-readable

All four must ship in v1 or v1 doesn't ship.

## Surfaces (v1)

- `/research <TICKER>` — single-ticker full DD with bias defense
- `/brief` — on-demand morning market summary
- `/trace <chain_id>` — human-readable cascade trace renderer

`/portfolio` deferred to v1.1.

## Install

Requires the Ozymandias v3 repo at a known path. Editable install:

```
pip install -e /path/to/ozy-bot-v3
pip install -e .
```

(Details in v1 build phases — see plan.)
