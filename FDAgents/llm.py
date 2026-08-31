"""LLM client with native reasoning.

Wraps the OpenAI Responses API (streamed) with:
  - native reasoning (``reasoning={"effort": ..., "summary": "auto"}``),
  - per-stage effort/token budgets from config,
  - automatic OpenRouter mode: when ``OPENROUTER_API_KEY`` is set (the
    contest harness always sets it) the client targets
    ``https://openrouter.ai/api/v1`` and prefixes the model with ``openai/``
    when needed — the contest requires OpenRouter and nothing else,
  - robust JSON extraction from the answer text,
  - cumulative token/cost accounting (reasoning tokens are billed as output).

Every call is stateless: ``store=False``, full context resent each time so a
crashed run can resume from memory.json alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Callable, Optional

from .config import Config, get_config

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def extract_json(text: str) -> Optional[dict]:
    """Best-effort extraction of the first JSON object from model text."""
    if not text:
        return None
    text = text.strip()
    # Fast path: the whole message is JSON.
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Fenced block.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # First balanced {...} span.
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class LLMError(RuntimeError):
    pass


class ToolLoopError(LLMError):
    """Tool-loop failure that retains its completed structured audit trail."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        transcript: list[dict],
        calls_by_name: dict[str, int],
        usage: dict,
        cost_usd: float,
        elapsed_s: float = 0.0,
        turns: int = 0,
        provider_failure: bool = False,
    ):
        super().__init__(message)
        self.stage = str(stage)
        self.transcript = list(transcript)
        self.calls_by_name = dict(calls_by_name)
        self.usage = dict(usage)
        self.cost_usd = float(cost_usd)
        self.elapsed_s = float(elapsed_s)
        self.turns = int(turns)
        # A provider transport, schema, or availability failure is an
        # infrastructure outcome, not a reasoning outcome. Callers must be able
        # to distinguish it so one rejected request cannot be mistaken for an
        # exhausted stage and discard the whole run.
        self.provider_failure = bool(provider_failure)

    @property
    def metrics(self) -> dict:
        return {
            "turns": self.turns,
            "elapsed_s": self.elapsed_s,
            "cost_usd": self.cost_usd,
            "input_tokens": int(self.usage.get("input_tokens", 0) or 0),
            "cached_tokens": int(self.usage.get("cached_tokens", 0) or 0),
            "output_tokens": int(self.usage.get("output_tokens", 0) or 0),
            "reasoning_tokens": int(self.usage.get("reasoning_tokens", 0) or 0),
        }


class TransientLLMError(LLMError):
    """Provider-reported transient failure without a typed HTTP exception."""


_TRANSIENT_PROVIDER_CODES = frozenset(
    {
        "server_error",
        "internal_server_error",
        "server_is_overloaded",
        "service_unavailable_error",
        "service_unavailable",
        "overloaded",
        "overloaded_error",
        "rate_limit_exceeded",
        "temporarily_unavailable",
        "timeout",
    }
)


def _provider_error_codes(exc: Exception) -> set:
    """Collect the provider's own error identifiers from a failure body.

    An overload reported inside an HTTP 200 stream reaches the SDK as a bare
    ``APIError`` with no status code, so status alone cannot classify it. The
    body still carries the provider's verdict as ``code``/``type``.
    """
    codes: set = set()
    body = getattr(exc, "body", None)
    if body is None:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - body need not be JSON
                body = None
    sources = []
    if isinstance(body, Mapping):
        sources.append(body)
        error = body.get("error")
        if isinstance(error, Mapping):
            sources.append(error)
    for source in sources:
        for key in ("code", "type"):
            value = source.get(key)
            if isinstance(value, str):
                codes.add(value.strip().lower())
    return codes


def _is_transient_api_error(exc: Exception) -> bool:
    """Return whether an OpenAI-compatible request is safe to retry."""
    if isinstance(exc, TransientLLMError):
        return True
    try:
        from httpx import TransportError
    except ImportError:  # pragma: no cover - OpenAI installs httpx
        TransportError = ()
    if isinstance(exc, TransportError):
        return True
    # Classify by the provider's own error code before anything status-based.
    # A `server_is_overloaded` delivered inside a 200 stream arrives as a bare
    # APIError, which no status test can catch; treating it as fatal ended a
    # run with 2669s of its budget and 99% of its cost allowance unspent.
    if _provider_error_codes(exc) & _TRANSIENT_PROVIDER_CODES:
        return True
    try:
        from openai import APIConnectionError, APIStatusError
    except ImportError:  # pragma: no cover - LLMClient already checks this
        return False

    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, APIStatusError):
        status = int(exc.status_code)
        return status in {408, 409, 429} or 500 <= status <= 599
    return False


def _api_error_detail(exc: Exception) -> str:
    """Render a provider failure with its response body.

    A bare status line is not diagnosable. OpenAI-compatible errors carry the
    offending ``param`` path in the body, which is what identifies a malformed
    input item during a stateless tool-loop continuation.
    """
    parts = [str(exc)]
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    body = getattr(exc, "body", None)
    if body is None:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - fall back to raw text
                body = getattr(response, "text", None)
    if body is not None:
        try:
            rendered = json.dumps(body, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(body)
        parts.append(f"body={rendered[:2000]}")
    return " | ".join(part for part in parts if part)


class LLMClient:
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        cfg: Optional[Config] = None,
        submission_mode: bool = False,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError("openai package required: pip install openai") from e

        self.cfg = cfg or get_config()
        model = model or self.cfg.require("llm.model")

        openrouter_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if submission_mode:
            if api_key is not None or base_url is not None:
                raise LLMError(
                    "submission mode forbids explicit API credentials or base URLs"
                )
            if not openrouter_key:
                raise LLMError("submission mode requires OPENROUTER_API_KEY")
            resolved_base = OPENROUTER_BASE_URL
            resolved_key = openrouter_key
        else:
            resolved_key = (
                api_key
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("LLM_API_KEY")
                or openrouter_key
            )
            resolved_base = (
                base_url
                or os.environ.get("OPENAI_BASE_URL")
                or os.environ.get("LLM_BASE_URL")
            )
            # Contest harness credentials override legacy environment variables.
            # Explicit constructor arguments remain available for local testing.
            if openrouter_key and api_key is None and base_url is None:
                resolved_base = OPENROUTER_BASE_URL
                resolved_key = openrouter_key
        if resolved_base and "openrouter.ai" in resolved_base and "/" not in model:
            model = f"openai/{model}"

        self.model = model
        self.base_url = resolved_base
        self.client = OpenAI(
            api_key=resolved_key,
            base_url=resolved_base,
            timeout=float(self.cfg.get("llm.request_timeout_s", 300)),
            max_retries=0,
        )

        self.call_count = 0
        self.total_cost = 0.0
        self.last_call_cost = 0.0
        self.total_input_tokens = 0
        self.total_cached_input_tokens = 0
        self.total_output_tokens = 0
        self.total_reasoning_tokens = 0
        self._last_tool_retry_attempts = 0
        self._last_tool_retry_reservation_cost = 0.0
        self._last_tool_success_reservation_cost = 0.0

        if self._pricing() is None:
            logger.warning(
                "[llm] no pricing entry for model %r; cost falls back to 0 "
                "unless the provider reports usage cost.",
                model,
            )

    # ------------------------------------------------------------------
    def _pricing(self) -> Optional[dict]:
        pricing: dict = self.cfg.get("llm.pricing", {}) or {}
        if self.model in pricing:
            return pricing[self.model]
        # Longest-prefix match tolerates provider prefixes/suffixes.
        matches = [
            (k, v)
            for k, v in pricing.items()
            if self.model.startswith(k) or self.model.endswith(k) or k in self.model
        ]
        if not matches:
            return None
        return max(matches, key=lambda kv: len(kv[0]))[1]

    def _stage_cfg(self, stage: str) -> dict:
        stages = self.cfg.get("llm.stages", {}) or {}
        entry = stages.get(stage) or {}
        return {
            "effort": entry.get("effort", "medium"),
            "max_output_tokens": int(entry.get("max_output_tokens", 4000)),
        }

    def _stage_max_tool_turns(self, stage: str) -> int:
        """Tool-turn ceiling for one stage, falling back to the shared ceiling.

        Research stages re-grep the same frozen corpus every reasoning cycle, so
        an unbounded shared ceiling let a single stage consume a quarter of the
        run's whole LLM budget.
        """
        default = int(self.cfg.get("react.max_tool_turns", 12))
        overrides = self.cfg.get("react.stage_max_tool_turns", {}) or {}
        value = overrides.get(stage) if isinstance(overrides, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return default
        return min(default, value)

    # ------------------------------------------------------------------
    def stage_call(
        self,
        stage: str,
        system: str,
        user: str,
        *,
        effort: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> dict:
        """Run one reasoning call; returns
        {parsed, text, reasoning_summary, usage, cost_usd}."""
        scfg = self._stage_cfg(stage)
        effort = effort or scfg["effort"]
        max_out = max_output_tokens or scfg["max_output_tokens"]

        retry = self.cfg.get("llm.retry", {}) or {}
        attempts = int(retry.get("attempts", 6))
        delay = float(retry.get("initial_delay_s", 5))
        max_delay = float(retry.get("max_delay_s", 60))

        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._one_call(stage, system, user, effort, max_out)
            except Exception as e:  # noqa: BLE001 - classify provider errors below
                last_err = e
                if not _is_transient_api_error(e):
                    raise LLMError(
                        f"{stage} call failed with non-retryable "
                        f"{type(e).__name__}: {e}"
                    ) from e
                logger.warning(
                    "[llm] %s call attempt %d/%d failed: %s",
                    stage, attempt, attempts, e,
                )
                if attempt < attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, max_delay)
        raise LLMError(
            f"{stage} call failed after {attempts} attempts: {last_err}"
        ) from last_err

    def tool_loop_call(
        self,
        stage: str,
        system: str,
        user: str,
        *,
        tools: list[dict],
        handler: Callable[[str, dict], dict],
        terminal_tools: set[str],
        required_tool_calls: Optional[dict[str, int]] = None,
        effort: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        max_turns: Optional[int] = None,
        tool_choice: Optional[Any] = None,
        deadline_monotonic: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
        terminal_validator: Optional[
            Callable[[str, dict, list[dict]], Optional[str]]
        ] = None,
    ) -> dict:
        """Run a stateless Responses function-tool loop to one terminal call.

        Every provider response is appended to locally held input. Tool output
        is produced only by ``handler``; terminal calls are returned to the
        caller for schema validation and never executed as EDA operations here.
        """
        started_monotonic = time.monotonic()
        scfg = self._stage_cfg(stage)
        effort = effort or scfg["effort"]
        max_out = max_output_tokens or scfg["max_output_tokens"]
        max_turns = int(
            max_turns
            or self._stage_max_tool_turns(stage)
        )
        repair_turns = (
            int(self.cfg.get("react.terminal_repair_turns", 2))
            if terminal_validator is not None
            else 0
        )
        total_turns = max_turns + repair_turns
        tool_choice = tool_choice or str(self.cfg.get("react.tool_choice", "auto"))
        required = {
            str(name): max(0, int(count))
            for name, count in (required_tool_calls or {}).items()
        }
        calls_by_name: dict[str, int] = {}
        transcript: list[dict] = []
        input_items: list[Any] = [{"role": "user", "content": user}]
        reasoning_parts: list[str] = []
        text_parts: list[str] = []
        aggregate_usage = {
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
        stage_cost = 0.0
        completed_turns = 0

        def audit_error(
            message: str, *, provider_failure: bool = False
        ) -> ToolLoopError:
            return ToolLoopError(
                message,
                stage=stage,
                transcript=transcript,
                calls_by_name=calls_by_name,
                usage=aggregate_usage,
                cost_usd=stage_cost,
                elapsed_s=time.monotonic() - started_monotonic,
                turns=completed_turns,
                provider_failure=provider_failure,
            )

        for turn in range(1, total_turns + 1):
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise audit_error(f"{stage} reached its run deadline")
            if max_cost_usd is not None and stage_cost >= float(max_cost_usd):
                raise audit_error(f"{stage} exhausted its LLM cost budget")
            missing_before_turn = {
                required_name: count - calls_by_name.get(required_name, 0)
                for required_name, count in required.items()
                if calls_by_name.get(required_name, 0) < count
            }
            turn_tools = tools
            turn_tool_choice = tool_choice
            missing_call_count = sum(missing_before_turn.values())
            if (
                missing_before_turn
                and turn >= max(1, max_turns - missing_call_count)
            ):
                required_names = set(missing_before_turn)
                required_definitions = [
                    tool
                    for tool in tools
                    if str(tool.get("name", "")) in required_names
                ]
                if required_definitions:
                    turn_tools = required_definitions
                    if len(required_names) == 1:
                        required_name = next(iter(required_names))
                        turn_tool_choice = {
                            "type": "function",
                            "name": required_name,
                        }
                        instruction = f"Call {required_name} now."
                    else:
                        turn_tool_choice = "required"
                        instruction = "Call one remaining required evidence tool now."
                    input_items.append({
                        "role": "user",
                        "content": (
                            "Required evidence calls must complete before the "
                            "terminal synthesis turn. " + instruction
                        ),
                    })
            if turn == max_turns - 1 and not missing_before_turn:
                input_items.append({
                    "role": "user",
                    "content": (
                        "The tool-turn budget is nearly exhausted. Synthesize the "
                        "evidence already collected and prepare the terminal call."
                    ),
                })
            if turn >= max_turns and not missing_before_turn:
                terminal_definitions = [
                    tool
                    for tool in tools
                    if str(tool.get("name", "")) in terminal_tools
                ]
                if terminal_definitions:
                    turn_tools = terminal_definitions
                if len(terminal_tools) == 1:
                    terminal_name = next(iter(terminal_tools))
                    turn_tool_choice = {
                        "type": "function",
                        "name": terminal_name,
                    }
                    instruction = f"Call {terminal_name} now."
                else:
                    turn_tool_choice = "required"
                    instruction = "Choose and call one terminal function now."
                input_items.append({
                    "role": "user",
                    "content": (
                        "This is the final protocol turn. "
                        f"{instruction} Use only evidence already displayed."
                    ),
                })
            self._last_tool_retry_reservation_cost = 0.0
            self._last_tool_success_reservation_cost = 0.0
            try:
                response = self._tool_response_with_retry(
                    stage,
                    system,
                    input_items,
                    turn_tools,
                    effort,
                    max_out,
                    turn_tool_choice,
                    deadline_monotonic,
                    (
                        max(0.0, float(max_cost_usd) - stage_cost)
                        if max_cost_usd is not None else None
                    ),
                )
            except LLMError as exc:
                # Retry reservations are permanently charged by the request
                # helper even when no provider response is available.
                stage_cost += self._last_tool_retry_reservation_cost
                # The request helper already exhausted its own retries, so this
                # is a provider-side outcome rather than a reasoning outcome.
                raise audit_error(str(exc), provider_failure=True) from exc
            stage_cost += self._last_tool_retry_reservation_cost
            completed_turns = turn
            usage = self._usage_from_response(response)
            for key in aggregate_usage:
                aggregate_usage[key] += int(usage.get(key, 0))
            usage_is_authoritative = bool(
                usage.get("provider_cost_usd") is not None
                or int(usage.get("input_tokens", 0)) > 0
                or int(usage.get("output_tokens", 0)) > 0
            )
            if (
                max_cost_usd is not None
                and not usage_is_authoritative
                and self._last_tool_success_reservation_cost > 0.0
            ):
                success_cost = self._last_tool_success_reservation_cost
                self.call_count += 1
                self.total_cost += success_cost
                self.last_call_cost = success_cost
                stage_cost += success_cost
                logger.warning(
                    "[llm] %s response omitted usage; charged worst-case $%.6f",
                    stage,
                    success_cost,
                )
            else:
                stage_cost += self._record_usage(usage)
            if max_cost_usd is not None and stage_cost > float(max_cost_usd) + 1e-12:
                raise audit_error(f"{stage} exceeded its LLM cost budget")
            output_items = list(getattr(response, "output", None) or [])
            response_text = self._response_text(response, output_items)
            if response_text:
                text_parts.append(response_text)
            reasoning_parts.extend(self._reasoning_summaries(output_items))

            # Stateless continuation replays the provider's own output items.
            # This is the documented Responses pattern for ``store=False`` and is
            # what preserves reasoning continuity across tool turns: reasoning
            # items carry ``encrypted_content`` (requested via ``include``) and
            # must be handed back verbatim so the model keeps its chain of
            # thought instead of re-deriving it from tool history every turn.
            input_items.extend(output_items)

            function_calls = [
                item
                for item in output_items
                if getattr(item, "type", None) == "function_call"
                or (isinstance(item, dict) and item.get("type") == "function_call")
            ]
            if not function_calls:
                transcript.append({
                    "turn": turn,
                    "kind": "protocol_error",
                    "error": "response contained no function call",
                })
                input_items.append({
                    "role": "user",
                    "content": (
                        "The stage is not complete. Call an allowed function tool; "
                        "plain text cannot terminate this stage."
                    ),
                })
                continue

            for item in function_calls:
                name = str(
                    getattr(item, "name", None)
                    or (item.get("name") if isinstance(item, dict) else "")
                )
                call_id = str(
                    getattr(item, "call_id", None)
                    or (item.get("call_id") if isinstance(item, dict) else "")
                )
                raw_arguments = (
                    getattr(item, "arguments", None)
                    if not isinstance(item, dict)
                    else item.get("arguments")
                )
                try:
                    arguments = (
                        json.loads(raw_arguments)
                        if isinstance(raw_arguments, str)
                        else dict(raw_arguments or {})
                    )
                    if not isinstance(arguments, dict):
                        raise TypeError("function arguments are not an object")
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    result = {"ok": False, "error": f"invalid function arguments: {exc}"}
                    transcript.append({
                        "turn": turn,
                        "tool": name,
                        "arguments": raw_arguments,
                        "result": result,
                    })
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    })
                    continue

                if name in terminal_tools:
                    missing = {
                        required_name: count - calls_by_name.get(required_name, 0)
                        for required_name, count in required.items()
                        if calls_by_name.get(required_name, 0) < count
                    }
                    if missing:
                        result = {
                            "ok": False,
                            "error": "required evidence tools have not been used",
                            "missing_calls": missing,
                        }
                        transcript.append({
                            "turn": turn,
                            "tool": name,
                            "arguments": arguments,
                            "result": result,
                        })
                        input_items.append({
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result),
                        })
                        continue
                    terminal_error = (
                        terminal_validator(name, arguments, transcript)
                        if terminal_validator is not None
                        else None
                    )
                    if terminal_error:
                        result = {
                            "ok": False,
                            "error": str(terminal_error)[:1000],
                        }
                        transcript.append({
                            "turn": turn,
                            "tool": name,
                            "arguments": arguments,
                            "result": result,
                        })
                        input_items.append({
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result),
                        })
                        continue
                    calls_by_name[name] = calls_by_name.get(name, 0) + 1
                    transcript.append({
                        "turn": turn,
                        "tool": name,
                        "arguments": arguments,
                        "terminal": True,
                    })
                    logger.info(
                        "[llm] %s tool loop completed in %d turn(s), %.1fs, "
                        "in=%d(cached=%d) out=%d(reasoning=%d), cost=$%.4f",
                        stage,
                        turn,
                        time.monotonic() - started_monotonic,
                        aggregate_usage["input_tokens"],
                        aggregate_usage["cached_tokens"],
                        aggregate_usage["output_tokens"],
                        aggregate_usage["reasoning_tokens"],
                        stage_cost,
                    )
                    return {
                        "parsed": arguments,
                        "terminal_tool": name,
                        "text": "\n".join(text_parts),
                        "reasoning_summary": "\n".join(reasoning_parts),
                        "usage": aggregate_usage,
                        "cost_usd": stage_cost,
                        "elapsed_s": time.monotonic() - started_monotonic,
                        "turns": turn,
                        "transcript": transcript,
                        "calls_by_name": calls_by_name,
                    }

                try:
                    result = handler(name, arguments)
                    if not isinstance(result, dict):
                        raise TypeError("tool handler must return an object")
                except Exception as exc:  # noqa: BLE001 - tool errors return to model
                    result = {"ok": False, "error": str(exc)[:500]}
                if result.get("ok") is not False:
                    calls_by_name[name] = calls_by_name.get(name, 0) + 1
                transcript.append({
                    "turn": turn,
                    "tool": name,
                    "arguments": arguments,
                    "result": result,
                })
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, default=str),
                })
        raise audit_error(
            f"{stage} did not call a valid terminal tool within {total_turns} turns"
        )

    def _tool_response_with_retry(
        self,
        stage: str,
        system: str,
        input_items: list[Any],
        tools: list[dict],
        effort: str,
        max_out: int,
        tool_choice: Any,
        deadline_monotonic: Optional[float],
        remaining_cost_usd: Optional[float],
    ):
        retry = self.cfg.get("llm.retry", {}) or {}
        attempts = max(1, int(retry.get("attempts", 6)))
        delay = float(retry.get("initial_delay_s", 5))
        max_delay = float(retry.get("max_delay_s", 60))
        last_err: Optional[Exception] = None
        bounded_max_out = int(max_out)
        failed_attempt_reservation = 0.0
        configured_attempts = attempts
        self._last_tool_retry_attempts = attempts
        self._last_tool_retry_reservation_cost = 0.0
        self._last_tool_success_reservation_cost = 0.0
        if remaining_cost_usd is not None:
            pricing = self._pricing()
            if not pricing:
                raise LLMError(
                    f"{stage} cannot enforce a cost cap without model pricing"
                )
            input_rate = max(
                float(pricing.get("input", 0.0)),
                float(pricing.get("cached_input", 0.0)),
            ) / 1000.0
            output_rate = float(pricing.get("output", 0.0)) / 1000.0
            if output_rate <= 0.0:
                raise LLMError(f"{stage} has no positive output-token price")
            # UTF-8 bytes are a conservative upper bound on BPE token count.
            input_bytes = (
                len(system.encode("utf-8"))
                + len(json.dumps(input_items, default=str).encode("utf-8"))
                + len(json.dumps(tools, default=str).encode("utf-8"))
                + 1024  # request framing and provider-added tool separators
            )
            # First reduce retry capacity so one complete synthesis turn remains
            # possible. Only shrink that one turn when the full output bound is
            # itself unaffordable. Every permitted attempt remains reserved.
            selected_attempts = None
            for candidate_attempts in range(attempts, 0, -1):
                available_for_output = (
                    float(remaining_cost_usd)
                    - input_bytes * input_rate * candidate_attempts
                )
                candidate_max_out = int(
                    max(0.0, available_for_output)
                    / output_rate
                    / candidate_attempts
                )
                if candidate_max_out >= bounded_max_out:
                    selected_attempts = candidate_attempts
                    break
            if selected_attempts is None:
                selected_attempts = 1
                available_for_output = (
                    float(remaining_cost_usd) - input_bytes * input_rate
                )
                bounded_max_out = min(
                    bounded_max_out,
                    int(max(0.0, available_for_output) / output_rate),
                )
            attempts = selected_attempts
            self._last_tool_retry_attempts = attempts
            if bounded_max_out < 64:
                raise LLMError(
                    f"{stage} remaining cost budget cannot fund another bounded turn"
                )
            if attempts < configured_attempts:
                logger.info(
                    "[llm] %s cost cap reduces transient attempts %d -> %d "
                    "with max_output_tokens=%d",
                    stage,
                    configured_attempts,
                    attempts,
                    bounded_max_out,
                )
            failed_attempt_reservation = (
                input_bytes * input_rate + bounded_max_out * output_rate
            )
            self._last_tool_success_reservation_cost = failed_attempt_reservation
        for attempt in range(1, attempts + 1):
            try:
                request_timeout = float(
                    self.cfg.get("llm.request_timeout_s", 300)
                )
                if deadline_monotonic is not None:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise LLMError(f"{stage} reached its run deadline")
                    request_timeout = min(request_timeout, remaining)
                stream = self.client.responses.create(
                    model=self.model,
                    instructions=system,
                    input=input_items,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=False,
                    reasoning={"effort": effort, "summary": "auto"},
                    # ``store=False`` keeps no provider-side state, so reasoning
                    # continuity across tool turns depends on replaying reasoning
                    # items. They are only replayable when the provider returns
                    # their encrypted content.
                    include=["reasoning.encrypted_content"],
                    max_output_tokens=bounded_max_out,
                    store=False,
                    stream=True,
                    timeout=request_timeout,
                )
                completed_response = None
                completed_output_items = []
                for event in stream:
                    event_type = getattr(event, "type", "")
                    if event_type == "response.output_item.done":
                        item = getattr(event, "item", None)
                        if item is not None:
                            completed_output_items.append(item)
                    elif event_type in ("response.completed", "response.incomplete"):
                        completed_response = getattr(event, "response", None)
                    elif event_type == "response.failed":
                        failed_response = getattr(event, "response", None)
                        raise LLMError(
                            f"{stage} streamed response failed: "
                            f"{getattr(failed_response, 'error', None)}"
                        )
                if completed_response is None:
                    raise LLMError(
                        f"{stage} streamed response ended without a final response"
                    )
                if not list(getattr(completed_response, "output", None) or []):
                    if hasattr(completed_response, "model_copy"):
                        completed_response = completed_response.model_copy(
                            update={"output": completed_output_items}
                        )
                    else:
                        completed_response.output = completed_output_items
                return completed_response
            except Exception as exc:  # noqa: BLE001 - classify provider errors below
                last_err = exc
                if not _is_transient_api_error(exc):
                    detail = _api_error_detail(exc)
                    # A non-retryable provider rejection ends the stage and can
                    # end the run. Log the provider's own error body here: the
                    # status line alone is not diagnosable, and the body carries
                    # the offending `param` path.
                    logger.error(
                        "[llm] %s tool turn rejected (non-retryable %s): %s",
                        stage,
                        type(exc).__name__,
                        detail,
                    )
                    raise LLMError(
                        f"{stage} tool turn failed with non-retryable "
                        f"{type(exc).__name__}: {detail}"
                    ) from exc
                if failed_attempt_reservation > 0.0:
                    # No usage object is available for a disconnected request.
                    # Permanently debit its worst-case bounded cost so later
                    # tool turns cannot reuse the same reservation.
                    self._last_tool_retry_reservation_cost += (
                        failed_attempt_reservation
                    )
                    self.total_cost += failed_attempt_reservation
                    self.last_call_cost = failed_attempt_reservation
                logger.warning(
                    "[llm] %s tool turn attempt %d/%d failed: %s",
                    stage,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    sleep_for = delay
                    if deadline_monotonic is not None:
                        sleep_for = min(
                            sleep_for,
                            max(0.0, deadline_monotonic - time.monotonic()),
                        )
                    if sleep_for <= 0:
                        break
                    time.sleep(sleep_for)
                    delay = min(delay * 2, max_delay)
        raise LLMError(
            f"{stage} tool turn failed after {attempts} attempts: {last_err}"
        ) from last_err

    @staticmethod
    def _usage_from_response(response) -> dict:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if isinstance(usage, dict):
            input_details = usage.get("input_tokens_details") or {}
            output_details = usage.get("output_tokens_details") or {}
            result = {
                "input_tokens": usage.get("input_tokens", 0) or 0,
                "cached_tokens": input_details.get("cached_tokens", 0) or 0,
                "output_tokens": usage.get("output_tokens", 0) or 0,
                "reasoning_tokens": output_details.get("reasoning_tokens", 0) or 0,
            }
            if usage.get("cost") is not None:
                result["provider_cost_usd"] = usage["cost"]
            return result
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        result = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "cached_tokens": (
                getattr(input_details, "cached_tokens", 0) or 0
                if input_details is not None else 0
            ),
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "reasoning_tokens": (
                getattr(output_details, "reasoning_tokens", 0) or 0
                if output_details is not None else 0
            ),
        }
        provider_cost = getattr(usage, "cost", None)
        if provider_cost is not None:
            result["provider_cost_usd"] = provider_cost
        return result

    @staticmethod
    def _response_text(response, output_items: list[Any]) -> str:
        direct = getattr(response, "output_text", None)
        if direct:
            return str(direct)
        parts = []
        for item in output_items:
            item_type = getattr(item, "type", None)
            content = getattr(item, "content", None)
            if isinstance(item, dict):
                item_type = item.get("type")
                content = item.get("content")
            if item_type != "message":
                continue
            for entry in content or []:
                text = (
                    entry.get("text") if isinstance(entry, dict)
                    else getattr(entry, "text", None)
                )
                if text:
                    parts.append(str(text))
        return "\n".join(parts)

    @staticmethod
    def _reasoning_summaries(output_items: list[Any]) -> list[str]:
        parts = []
        for item in output_items:
            item_type = getattr(item, "type", None)
            summary = getattr(item, "summary", None)
            if isinstance(item, dict):
                item_type = item.get("type")
                summary = item.get("summary")
            if item_type != "reasoning":
                continue
            for entry in summary or []:
                text = (
                    entry.get("text") if isinstance(entry, dict)
                    else getattr(entry, "text", None)
                )
                if text:
                    parts.append(str(text))
        return parts

    # ------------------------------------------------------------------
    def _one_call(
        self, stage: str, system: str, user: str, effort: str, max_out: int
    ) -> dict:
        t0 = time.time()
        stream = self.client.responses.create(
            model=self.model,
            instructions=system,
            input=[{"role": "user", "content": user}],
            reasoning={"effort": effort, "summary": "auto"},
            max_output_tokens=max_out,
            store=False,
            stream=True,
        )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict = {}
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                text_parts.append(getattr(event, "delta", "") or "")
            elif etype == "response.output_item.done":
                item = getattr(event, "item", None)
                item_type = getattr(item, "type", "")
                if item_type == "reasoning":
                    for s in getattr(item, "summary", None) or []:
                        stext = getattr(s, "text", "") or ""
                        if stext:
                            reasoning_parts.append(stext)
            elif etype in ("response.completed", "response.incomplete"):
                resp = getattr(event, "response", None)
                u = getattr(resp, "usage", None)
                if u is not None:
                    details_out = getattr(u, "output_tokens_details", None)
                    details_in = getattr(u, "input_tokens_details", None)
                    usage = {
                        "input_tokens": getattr(u, "input_tokens", 0) or 0,
                        "cached_tokens": getattr(details_in, "cached_tokens", 0) or 0
                        if details_in is not None
                        else 0,
                        "output_tokens": getattr(u, "output_tokens", 0) or 0,
                        "reasoning_tokens": getattr(details_out, "reasoning_tokens", 0)
                        or 0
                        if details_out is not None
                        else 0,
                    }
                    provider_cost = getattr(u, "cost", None)
                    if provider_cost is not None:
                        usage["provider_cost_usd"] = provider_cost
            elif etype == "response.failed":
                resp = getattr(event, "response", None)
                err = getattr(resp, "error", None)
                code = (
                    err.get("code") or err.get("type")
                    if isinstance(err, dict)
                    else getattr(err, "code", None) or getattr(err, "type", None)
                )
                error_type = (
                    TransientLLMError
                    if str(code).strip().lower() in _TRANSIENT_PROVIDER_CODES
                    else LLMError
                )
                raise error_type(f"response.failed: {err}")

        text = "".join(text_parts)
        cost = self._record_usage(usage)
        elapsed = time.time() - t0
        parsed = extract_json(text)
        logger.info(
            "[llm] %s: %.1fs in=%s(cached=%s) out=%s(reasoning=%s) cost=$%.4f parsed=%s",
            stage,
            elapsed,
            usage.get("input_tokens"),
            usage.get("cached_tokens"),
            usage.get("output_tokens"),
            usage.get("reasoning_tokens"),
            cost,
            parsed is not None,
        )
        return {
            "parsed": parsed,
            "text": text,
            "reasoning_summary": "\n".join(reasoning_parts),
            "usage": usage,
            "cost_usd": cost,
            "elapsed_s": elapsed,
            "turns": 1,
        }

    # ------------------------------------------------------------------
    def _record_usage(self, usage: dict) -> float:
        input_tokens = int(usage.get("input_tokens", 0))
        cached = int(usage.get("cached_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        reasoning_tokens = int(usage.get("reasoning_tokens", 0))

        self.call_count += 1
        self.total_input_tokens += input_tokens
        self.total_cached_input_tokens += cached
        self.total_output_tokens += output_tokens
        self.total_reasoning_tokens += reasoning_tokens

        provider_cost = usage.get("provider_cost_usd")
        pricing = self._pricing() or {}
        in_rate = float(pricing.get("input", 0.0)) / 1000.0
        cached_rate = float(pricing.get("cached_input", pricing.get("input", 0.0))) / 1000.0
        out_rate = float(pricing.get("output", 0.0)) / 1000.0
        uncached = max(0, input_tokens - cached)
        cost = (
            float(provider_cost)
            if provider_cost is not None
            else uncached * in_rate + cached * cached_rate + output_tokens * out_rate
        )
        self.last_call_cost = cost
        self.total_cost += cost
        return cost

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "calls": self.call_count,
            "input_tokens": self.total_input_tokens,
            "cached_input_tokens": self.total_cached_input_tokens,
            "output_tokens": self.total_output_tokens,
            "reasoning_tokens": self.total_reasoning_tokens,
            "total_cost_usd": round(self.total_cost, 6),
        }
