"""
LLM client for FDAgents — OpenAI Responses API, stateless (no history).

Every call is independent. The compact state dict from memory.py is the
only context the LLM sees (< 3K tokens input per call).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .memory import Memory

logger = logging.getLogger(__name__)

# Pricing table — values are USD per 1K tokens.
# The proxy/API doesn't return a cost field, so we compute locally from usage.
PRICING: dict[str, dict[str, float]] = {
    # ----- Anthropic (via OpenAI-compatible proxy) -----
    # Confirmed 2026-04-14: Claude Opus 4.6 is cheaper than Opus 4/4.5 ($15/$75)
    "claude-opus-4-6":   {"input": 0.005,  "output": 0.025},
    "claude-opus-4-5":   {"input": 0.015,  "output": 0.075},
    "claude-opus-4":     {"input": 0.015,  "output": 0.075},
    # ----- OpenAI -----
    # User-provided 2026-06-04 GPT-5.5 standard API pricing is
    # $5 uncached input / $0.50 cached input / $30 output per 1M tokens;
    # this table stores USD per 1K tokens.
    "gpt-5.5":           {"input": 0.005,  "cached_input": 0.0005, "output": 0.030},
    "gpt-5.5-pro":       {"input": 0.030,  "output": 0.180},
    # Confirmed 2026-04-14: GPT-5.4 "most capable" tier
    "gpt-5.4":           {"input": 0.0025, "output": 0.015},
    "gpt-4o":            {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini":       {"input": 0.00015, "output": 0.00060},
    # ----- Free -----
    "qwen/qwen3.6-plus:free": {"input": 0.0, "output": 0.0},
}


def _lookup_pricing(model: str) -> Optional[dict]:
    """
    Find pricing entry for a model.

    1. Exact key match.
    2. Prefix match (longest wins) — handles tagged variants like
       'claude-opus-4-6[1m]' or 'gpt-5.4-20260101'.
    3. None if nothing matches (caller decides how to react).
    """
    if model in PRICING:
        return PRICING[model]
    matches = [(k, v) for k, v in PRICING.items() if model.startswith(k)]
    if matches:
        matches.sort(key=lambda kv: -len(kv[0]))
        return matches[0][1]
    return None


def _parse_json_or_extract(text: str) -> Optional[dict]:
    """
    Try to parse text as JSON. If that fails, try to extract the first
    balanced JSON object from within prose and parse that.

    Handles these shapes:
      - pure JSON: '{"skill": "phys_opt"}'
      - JSON inside prose: 'Looking at this path... {"skill": "phys_opt"} …'
      - JSON wrapped in markdown prose without code fences
    Returns None on unrecoverable failure (caller falls back to error response).
    """
    if not text:
        return None

    # Direct parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first '{' and walk forward tracking brace depth, skipping
    # over string contents so braces inside strings don't confuse us.
    start = text.find("{")
    while start != -1:
        depth = 0
        i = start
        in_string = False
        escape = False
        while i < len(text):
            c = text[i]
            if escape:
                escape = False
            elif c == "\\" and in_string:
                escape = True
            elif c == '"':
                in_string = not in_string
            elif not in_string:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        # Found a balanced object
                        candidate = text[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break  # Try next '{' after this one
            i += 1
        # Find next candidate start after this failed block
        start = text.find("{", start + 1)

    return None


class LLMClient:
    """
    Stateless LLM wrapper using the OpenAI Responses API.

    Each call is completely independent — no conversation history is retained
    server-side (store=False) and none is sent client-side.
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        skill_md_path: Optional[Path] = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("openai package required: pip install openai") from e

        self.model = model
        self.client = OpenAI(
            api_key=api_key
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("LLM_API_KEY"),
            base_url=base_url
                or os.environ.get("OPENAI_BASE_URL")
                or os.environ.get("LLM_BASE_URL"),
        )

        # Load SKILL.md as the fixed system instructions (cached by the API)
        if skill_md_path is None:
            skill_md_path = Path(__file__).parent / "SKILL.md"
        self.instructions = skill_md_path.read_text() if skill_md_path.exists() else ""

        self.call_count = 0
        self.total_cost = 0.0
        self.last_call_cost = 0.0
        self.total_input_tokens = 0
        self.total_cached_input_tokens = 0
        self.total_uncached_input_tokens = 0
        self.total_output_tokens = 0

        # Warn once if the model doesn't map to a known price entry
        if _lookup_pricing(model) is None:
            logger.warning(
                f"[llm] no pricing entry for model {model!r}; "
                "cost tracking will report $0. Add it to PRICING in llm.py."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(self, task: str, memory: "Memory", extra: dict | None = None) -> dict:
        """
        Normal-mode call: compact state only, no raw tool output.

        Returns parsed JSON dict from the LLM.
        """
        state = memory.compact_state_for_llm()
        state["task"] = task
        if extra:
            state.update(extra)
        return self._call(state, max_output_tokens=512)

    def call_deep(self, task: str, memory: "Memory", raw_output: str, extra: dict | None = None) -> dict:
        """
        Deep-analysis call: passes truncated raw tool output to LLM.

        Use only when should_deep_analyze() returns True.
        Truncates raw_output to ~8K tokens (32K chars) before sending.
        When extra is provided, its keys are merged into the compact state —
        agent.py uses this to pass the custom-tool catalog and the recent
        custom-response window for mid-deep-session chaining.
        """
        state = memory.compact_state_for_llm(include_custom_window=True)
        state["task"] = task
        state["raw_tool_output"] = raw_output[:32000]
        state["_deep_mode"] = True
        if extra:
            state.update(extra)
        return self._call(state, max_output_tokens=1024)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call(self, payload: dict, max_output_tokens: int = 512) -> dict:
        t0 = time.time()
        self.call_count += 1

        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=self.instructions,
                input=[{"role": "user", "content": json.dumps(payload, default=str)}],
                store=False,        # no server-side history
                stream=True,        # proxy requires streaming Responses calls
                max_output_tokens=max_output_tokens,
            )
        except Exception as e:
            logger.error(f"[llm] API call {self.call_count} failed: {e}")
            raise

        elapsed = time.time() - t0
        raw_text, usage = self._extract_response_text_and_usage(response)

        # Track tokens + cost from usage (proxy doesn't return a cost field)
        input_tokens = self._usage_int(usage, "input_tokens")
        output_tokens = self._usage_int(usage, "output_tokens")
        cached_input_tokens = self._cached_input_tokens(usage)
        uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
        cost = self._compute_cost(input_tokens, output_tokens, cached_input_tokens)

        self.last_call_cost = cost
        self.total_cost += cost
        self.total_input_tokens += input_tokens
        self.total_cached_input_tokens = (
            getattr(self, "total_cached_input_tokens", 0) + cached_input_tokens
        )
        self.total_uncached_input_tokens = (
            getattr(self, "total_uncached_input_tokens", 0) + uncached_input_tokens
        )
        self.total_output_tokens += output_tokens

        logger.info(
            f"[llm] call {self.call_count} task={payload.get('task')} "
            f"elapsed={elapsed:.2f}s "
            f"tokens=in{input_tokens}/cached{cached_input_tokens}/out{output_tokens} "
            f"cost=${cost:.4f} total=${self.total_cost:.4f}"
        )

        if usage is None:
            logger.warning(
                f"[llm] call {self.call_count} returned no usage field; "
                "cost tracking shows $0 for this call"
            )

        # Parse response
        raw_text = raw_text.strip()

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            raw_text = "\n".join(lines[1:-1]) if len(lines) > 2 else raw_text

        parsed = _parse_json_or_extract(raw_text)
        if parsed is not None:
            return parsed

        logger.warning(f"[llm] response not valid JSON, raw: {raw_text[:200]}")
        return {"error": "invalid_json", "raw": raw_text[:500]}

    def _extract_response_text_and_usage(self, response: Any) -> tuple[str, Any]:
        """Return output text and usage from streamed or non-streamed responses."""
        if not hasattr(response, "__iter__") or isinstance(response, (str, bytes)):
            return getattr(response, "output_text", "") or "", getattr(response, "usage", None)

        text_deltas: list[str] = []
        done_text = ""
        completed_response = None
        try:
            for event in response:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    text_deltas.append(getattr(event, "delta", "") or "")
                elif event_type == "response.output_text.done":
                    done_text = getattr(event, "text", "") or done_text
                elif event_type == "response.completed":
                    completed_response = getattr(event, "response", None)
                elif event_type in {"response.failed", "response.error"}:
                    error = getattr(event, "error", None)
                    raise RuntimeError(f"streaming response failed: {error or event}")
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        if completed_response is not None:
            output_text = getattr(completed_response, "output_text", "") or ""
            if output_text:
                return output_text, getattr(completed_response, "usage", None)
            return (
                done_text or "".join(text_deltas),
                getattr(completed_response, "usage", None),
            )

        return done_text or "".join(text_deltas), None

    @staticmethod
    def _usage_int(usage: Any, key: str) -> int:
        if usage is None:
            return 0
        if isinstance(usage, dict):
            value = usage.get(key, 0)
        else:
            value = getattr(usage, key, 0)
        return value if isinstance(value, int) else 0

    @classmethod
    def _cached_input_tokens(cls, usage: Any) -> int:
        if usage is None:
            return 0
        direct = cls._usage_int(usage, "cached_input_tokens")
        if direct:
            return direct
        details = usage.get("input_tokens_details") if isinstance(usage, dict) else getattr(usage, "input_tokens_details", None)
        if details is None:
            details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(usage, "prompt_tokens_details", None)
        if details is None:
            return 0
        if isinstance(details, dict):
            value = details.get("cached_tokens", details.get("cached_input_tokens", 0))
        else:
            value = getattr(details, "cached_tokens", getattr(details, "cached_input_tokens", 0))
        return value if isinstance(value, int) else 0

    def _compute_cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        pricing = _lookup_pricing(self.model)
        if pricing is None:
            return 0.0
        cached_input_tokens = max(0, min(cached_input_tokens, input_tokens))
        uncached_input_tokens = input_tokens - cached_input_tokens
        cached_input_price = pricing.get("cached_input", pricing["input"])
        return (
            uncached_input_tokens * pricing["input"]
            + cached_input_tokens * cached_input_price
            + output_tokens * pricing["output"]
        ) / 1000.0
