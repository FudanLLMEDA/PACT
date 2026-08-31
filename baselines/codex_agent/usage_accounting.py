from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any


UNKNOWN = "unknown"


@dataclass
class UsageAccounting:
    model: str | int | float
    input_tokens: int | str
    output_tokens: int | str
    total_tokens: int | str
    cost_usd: float | str
    llm_calls: int | str
    agent_elapsed_s: int | float | str
    usage_source: str
    cached_input_tokens: int | str = UNKNOWN
    uncached_input_tokens: int | str = UNKNOWN
    reasoning_output_tokens: int | str = UNKNOWN
    cost_upper_bound_usd: float | str = UNKNOWN

    def as_row(self) -> dict[str, Any]:
        return asdict(self)

    def apply_pricing(self, pricing: dict[str, dict[str, float]]) -> None:
        if not isinstance(self.model, str):
            return
        if not isinstance(self.input_tokens, int) or not isinstance(self.output_tokens, int):
            return
        price = pricing.get(self.model)
        if not price:
            return
        upper_bound = (
            self.input_tokens / 1_000_000.0 * price["input_per_mtok"]
            + self.output_tokens / 1_000_000.0 * price["output_per_mtok"]
        )
        if self.cost_usd != UNKNOWN:
            return
        self.cost_upper_bound_usd = upper_bound

        cached = self.cached_input_tokens
        if not isinstance(cached, int):
            self.cost_usd = upper_bound
            self.usage_source = f"{self.usage_source}+pricing"
            return

        uncached = self.uncached_input_tokens
        if not isinstance(uncached, int):
            uncached = max(self.input_tokens - cached, 0)
            self.uncached_input_tokens = uncached

        cached_price = price.get("cached_input_per_mtok")
        if cached > 0 and cached_price is None:
            self.usage_source = f"{self.usage_source}+pricing_upper_bound"
            return

        self.cost_usd = (
            uncached / 1_000_000.0 * price["input_per_mtok"]
            + cached / 1_000_000.0 * (cached_price if cached_price is not None else price["input_per_mtok"])
            + self.output_tokens / 1_000_000.0 * price["output_per_mtok"]
        )
        self.usage_source = f"{self.usage_source}+cache_pricing" if cached > 0 else f"{self.usage_source}+pricing"


def _int_group(text: str, pattern: str) -> int | str:
    match = re.search(pattern, text, re.S)
    if not match:
        return UNKNOWN
    return int(match.group(1).replace(",", ""))


def _float_group(text: str, pattern: str) -> float | str:
    match = re.search(pattern, text, re.S)
    if not match:
        return UNKNOWN
    return float(match.group(1))


def _str_group(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.S)
    return match.group(1).strip() if match else UNKNOWN


def parse_fdagents_usage(stdout: str) -> UsageAccounting:
    incremental_usage = _parse_fdagents_incremental_usage(stdout)
    if incremental_usage is not None and "LLM tokens:" not in stdout:
        return incremental_usage

    if "returned no usage field" in stdout:
        return UsageAccounting(
            model=_str_group(stdout, r"LLM model:\s+([^\n]+)"),
            input_tokens=UNKNOWN,
            cached_input_tokens=UNKNOWN,
            uncached_input_tokens=UNKNOWN,
            output_tokens=UNKNOWN,
            total_tokens=UNKNOWN,
            cost_usd=UNKNOWN,
            llm_calls=_int_group(stdout, r"LLM calls:\s+(\d+)"),
            agent_elapsed_s=_int_group(stdout, r"Elapsed:\s+(\d+)s"),
            usage_source="fdagents_missing_usage",
        )
    input_tokens = _int_group(stdout, r"LLM tokens:\s+in\s+([\d,]+)")
    output_tokens = _int_group(stdout, r"LLM tokens:.*?out\s+([\d,]+)")
    cached_input_tokens = _int_group(stdout, r"LLM cached input:\s+([\d,]+)")
    uncached_input_tokens: int | str = UNKNOWN
    if isinstance(input_tokens, int) and isinstance(cached_input_tokens, int):
        uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
    total_tokens: int | str = UNKNOWN
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        total_tokens = input_tokens + output_tokens
    return UsageAccounting(
        model=_str_group(stdout, r"LLM model:\s+([^\n]+)"),
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=_float_group(stdout, r"LLM cost:\s+\$([\d.]+)"),
        llm_calls=_int_group(stdout, r"LLM calls:\s+(\d+)"),
        agent_elapsed_s=_int_group(stdout, r"Elapsed:\s+(\d+)s"),
        usage_source="fdagents_summary" if "LLM tokens:" in stdout else "unparsed",
    )


def _parse_fdagents_incremental_usage(stdout: str) -> UsageAccounting | None:
    """Parse per-call FDAgents LLM log lines when the final summary is absent."""
    pattern = re.compile(
        r"\[llm\]\s+call\s+(\d+)\s+.*?"
        r"tokens=in([\d,]+)/cached([\d,]+)/out([\d,]+)\s+"
        r"cost=\$([\d.]+)\s+total=\$([\d.]+)"
    )
    calls = []
    for line in stdout.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        call_no, input_tokens, cached_tokens, output_tokens, call_cost, total_cost = match.groups()
        calls.append(
            (
                int(call_no),
                int(input_tokens.replace(",", "")),
                int(cached_tokens.replace(",", "")),
                int(output_tokens.replace(",", "")),
                float(call_cost),
                float(total_cost),
            )
        )
    if not calls:
        return None

    calls.sort(key=lambda item: item[0])
    input_total = sum(item[1] for item in calls)
    cached_total = sum(item[2] for item in calls)
    output_total = sum(item[3] for item in calls)
    uncached_total = max(input_total - cached_total, 0)
    last_total_cost = calls[-1][5]
    return UsageAccounting(
        model=_str_group(stdout, r"LLM model:\s+([^\n]+)"),
        input_tokens=input_total,
        cached_input_tokens=cached_total,
        uncached_input_tokens=uncached_total,
        output_tokens=output_total,
        total_tokens=input_total + output_total,
        cost_usd=last_total_cost,
        llm_calls=max(item[0] for item in calls),
        agent_elapsed_s=_int_group(stdout, r"Elapsed:\s+(\d+)s"),
        usage_source="fdagents_incremental_llm_log",
    )


def parse_codex_usage(jsonl_stdout: str, stderr: str) -> UsageAccounting:
    model: str | int | float = UNKNOWN
    input_tokens: int | str = UNKNOWN
    cached_input_tokens: int | str = UNKNOWN
    output_tokens: int | str = UNKNOWN
    reasoning_output_tokens: int | str = UNKNOWN
    total_tokens: int | str = UNKNOWN
    cost_usd: float | str = UNKNOWN
    calls: int | str = UNKNOWN

    for line in jsonl_stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event.get("model"), str):
            model = event["model"]
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else event
        if not isinstance(usage, dict):
            continue
        if any(key in usage for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")):
            input_tokens = usage.get("input_tokens", input_tokens)
            cached_input_tokens = usage.get("cached_input_tokens", cached_input_tokens)
            output_tokens = usage.get("output_tokens", output_tokens)
            reasoning_output_tokens = usage.get("reasoning_output_tokens", reasoning_output_tokens)
            total_tokens = usage.get("total_tokens", total_tokens)
            cost_usd = usage.get("cost_usd", cost_usd)
            calls = usage.get("llm_calls", calls)

    if isinstance(input_tokens, int) and isinstance(output_tokens, int) and total_tokens == UNKNOWN:
        total_tokens = input_tokens + output_tokens
    uncached_input_tokens: int | str = UNKNOWN
    if isinstance(input_tokens, int) and isinstance(cached_input_tokens, int):
        uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
    source = "codex_jsonl" if input_tokens != UNKNOWN or output_tokens != UNKNOWN or cost_usd != UNKNOWN else "unparsed"
    return UsageAccounting(
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        uncached_input_tokens=uncached_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        llm_calls=calls,
        agent_elapsed_s=UNKNOWN,
        usage_source=source,
    )
