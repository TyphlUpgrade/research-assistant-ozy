"""
Clean-room Anthropic SDK wrapper for research-mode use.

Why a clean-room implementation instead of lifting `claude_reasoning.py`'s
`_call_claude_raw`: the Ozy version is an instance method on a class taking
`Config` and `PortfolioState`. Research-mode has no portfolio state and a
different config shape, so extracting that method is messier than rewriting
the ~100 lines of SDK glue here.

What we keep from Ozy's approach:
- 4-step defensive JSON parsing (imported from `ozymandias.intelligence.claude_json`)
- Exponential backoff on rate-limit and 5xx errors
- Token-usage tracking + accumulated cost

What we drop:
- Position/PortfolioState context shaping
- Gemini fallback (research-mode is single-vendor; Anthropic only)
- Token-budget guard with chars-per-token estimation
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

log = logging.getLogger(__name__)


# Per-million-token pricing (USD). Update as Anthropic pricing changes.
# Conservative estimates; cost tracking is for telemetry, not billing.
_PRICING: dict[str, tuple[float, float]] = {
    # model: (input_per_mtok, output_per_mtok)
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
}


@dataclass
class CostTracker:
    total_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)
    calls: int = 0

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        prices = _PRICING.get(model, (3.00, 15.00))  # default to Sonnet pricing
        cost = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
        self.total_usd += cost
        self.by_model[model] = self.by_model.get(model, 0.0) + cost
        self.calls += 1
        return cost


@dataclass
class CallResult:
    """Per-call result with text and metadata for trace emission."""
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int


class ClaudeClient:
    """
    Minimal async Anthropic client with retry + cost tracking.

    Usage:
        client = ClaudeClient()
        text = await client.call("Hello", model="claude-sonnet-4-6")
        print(client.cost.total_usd)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "claude-sonnet-4-6",
        max_retries: int = 3,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        )
        self.default_model = default_model
        self.max_retries = max_retries
        self.cost = CostTracker()

    async def call(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        system: Optional[str] = None,
        temperature: float = 1.0,
    ) -> CallResult:
        """
        Single-turn call. Returns CallResult with text + metadata (model,
        tokens, cost, latency). Raises on unrecoverable errors.

        Per-call metadata enables cascade trace emission downstream — the
        orchestrator captures it and writes per-stage events to JSONL.
        """
        chosen_model = model or self.default_model
        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = system

        delay = 1.0
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                start = time.perf_counter()
                response = await self._client.messages.create(**kwargs)
                latency_ms = int((time.perf_counter() - start) * 1000)
                cost = self.cost.record(
                    chosen_model,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
                return CallResult(
                    text=response.content[0].text,
                    model=chosen_model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                )
            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                last_err = e
                if isinstance(e, anthropic.APIStatusError) and e.status_code < 500:
                    raise  # client error, no retry
                log.warning(
                    "ClaudeClient: %s on attempt %d/%d, sleeping %.1fs",
                    type(e).__name__, attempt + 1, self.max_retries + 1, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
            except anthropic.APIConnectionError as e:
                last_err = e
                log.warning(
                    "ClaudeClient: connection error on attempt %d/%d, sleeping %.1fs",
                    attempt + 1, self.max_retries + 1, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2

        assert last_err is not None
        raise last_err
