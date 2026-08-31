"""
custom skill — runtime escape hatch for direct MCP tool invocation.

Only usable when the agent is in deep-analysis mode (see decision.should_deep_analyze
and agent.py). The LLM picks any one tool from either the Vivado or RapidWright
MCP server, supplies the arguments inline, and Python forwards the call verbatim
and returns a truncated response for the next LLM round to reason about.

Usage (from LLM, inside a deep_analysis reply):

    {
      "recommended_action": {
        "skill": "custom",
        "target": "vivado:run_tcl",
        "params": {
          "arguments": {"command": "report_timing -max_paths 1 -from ..."},
          "description": "Query the source side of path 1 to confirm FDRE location",
          "measure_timing": false
        }
      }
    }

target is ALWAYS "server:tool_name" where server is "vivado" or "rapidwright".
"""

import logging
from pathlib import Path
from typing import Optional

from .base import SkillResult, calculate_fmax

logger = logging.getLogger(__name__)


# Response truncation. We feed the LLM enough of the tool output to actually
# reason about real Vivado/RapidWright reports — a single report_timing path
# is 2-4 KB, and the LLM may ask for -max_paths 10 (20-40 KB). Too-tight a
# limit makes the probe useless: in fdagents_run-20260414_205737 the LLM
# issued three report_timing calls but only saw the header and trailing
# options because the limit was 4 KB.
#
# This is just the CustomSkill's own cap. memory.py imposes a second cap
# when embedding the response into the next deep-mode state (see
# compact_state_for_llm) — keep that one slightly smaller for token safety.
MAX_RESPONSE_CHARS = 16000


class CustomSkill:
    """Direct pass-through to any MCP tool. Deep-mode only."""

    name = "custom"

    async def execute(
        self,
        mcp,
        target: str,                    # "vivado:tool_name" or "rapidwright:tool_name"
        params: dict,                   # {arguments: {...}, description: "...", measure_timing: bool}
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        output_dcp = run_dir / "current_best.dcp"  # custom calls don't produce their own DCP

        server, _, tool_name = target.partition(":")
        server = server.strip().lower()
        tool_name = tool_name.strip()

        if server not in ("vivado", "rapidwright", "rw", "v"):
            return SkillResult.failure(
                before_wns,
                f"custom target must be 'vivado:tool' or 'rapidwright:tool', got {target!r}",
                output_dcp,
            )
        if not tool_name:
            return SkillResult.failure(
                before_wns,
                f"custom target missing tool name, got {target!r}",
                output_dcp,
            )

        arguments = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            return SkillResult.failure(
                before_wns,
                f"custom params.arguments must be a dict, got {type(arguments).__name__}",
                output_dcp,
            )
        description = str(params.get("description", ""))[:200]
        measure_timing = bool(params.get("measure_timing", False))

        logger.info(
            f"[custom] {server}:{tool_name} args={str(arguments)[:200]} "
            f"measure_timing={measure_timing} desc={description!r}"
        )

        try:
            if server in ("vivado", "v"):
                response = await mcp.call_vivado(tool_name, arguments, timeout=600.0)
            else:
                response = await mcp.call_rw(tool_name, arguments, timeout=600.0)
        except Exception as e:
            logger.error(f"[custom] {server}:{tool_name} failed: {e}")
            return SkillResult.failure(before_wns, f"{server}:{tool_name} raised: {e}", output_dcp)

        truncated = (response or "")[:MAX_RESPONSE_CHARS]
        was_truncated = len(response or "") > MAX_RESPONSE_CHARS

        # WNS measurement is opt-in — many custom calls are read-only probes
        # (report_timing, search_cells, get_design_info) and re-querying WNS
        # just to report delta=0 wastes Vivado time.
        after_wns = before_wns
        if measure_timing:
            try:
                measured = await mcp.get_wns()
                if measured is not None:
                    after_wns = measured
            except Exception as e:
                logger.warning(f"[custom] could not measure WNS after: {e}")

        delta = after_wns - before_wns

        head = truncated.replace("\n", " ")[:140]
        summary = (
            f"custom:{server}:{tool_name} delta_wns={delta:+.3f} → {head}"
            + (" …(truncated)" if was_truncated else "")
        )[:200]

        return SkillResult(
            success=True,
            before_wns=before_wns,
            after_wns=after_wns,
            delta_wns=delta,
            is_legal=True,   # can't verify legality from a generic tool call
            output_dcp=output_dcp,
            summary=summary,
            details=truncated,   # full truncated response for the next deep round
        )
