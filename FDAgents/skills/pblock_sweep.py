"""
pblock_sweep skill — try a small set of generic slice pblock candidates.

The learned pattern is not tied to benchmark names: for slice-only,
route-dominated designs, a compact central fabric window or bbox-derived
pblock plus fresh place/route can materially improve WNS. The MCP tool
supplies candidates from the current placement and device bounds; this skill
evaluates them and keeps the best legal checkpoint.
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


def _candidate_range(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        value = item.get("range") or item.get("ranges")
        return str(value).strip() if value else ""
    return ""


def _parse_candidate_ranges(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_candidate_range(item) for item in value if _candidate_range(item)]
    if isinstance(value, dict):
        if "candidates" in value:
            return _parse_candidate_ranges(value.get("candidates"))
        return [_candidate_range(value)] if _candidate_range(value) else []

    text = str(value).strip()
    if not text:
        return []
    if text[0] in "[{":
        parsed = json.loads(text)
        return _parse_candidate_ranges(parsed)

    if ";" in text:
        separator = ";"
    elif "\n" in text:
        separator = "\n"
    elif "," in text and text.count("SLICE_X") > 2:
        separator = ","
    else:
        return [text]
    return [part.strip() for part in text.split(separator) if part.strip()]


def _dedupe_keep_order(values: list[str], limit: int) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


class PblockSweepSkill:
    """Sweep candidate pblock ranges, then keep the best legal result."""

    name = "pblock_sweep"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        output_dcp = run_dir / "pblock_sweep_best.dcp"
        current_dcp = params.get("input_dcp") or str(run_dir / "current_best.dcp")
        max_candidates = int(params.get("max_candidates", 4))
        place_directive = str(params.get("place_directive", "Default"))
        route_directive = str(params.get("route_directive", "Default"))
        allow_hard_macros = bool(params.get("allow_hard_macros", False))
        is_soft = bool(params.get("is_soft", False))

        logger.info(
            "[pblock_sweep] target=%r max_candidates=%d place=%s route=%s",
            target,
            max_candidates,
            place_directive,
            route_directive,
        )

        try:
            ranges = await self._resolve_ranges(
                mcp,
                target,
                params,
                max_candidates=max_candidates,
                allow_hard_macros=allow_hard_macros,
            )
            if not ranges:
                return SkillResult.failure(before_wns, "no pblock ranges to sweep", output_dcp)

            best_wns = None
            best_range = None
            legal_count = 0
            trial_details = []

            for idx, pblock_range in enumerate(ranges):
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(current_dcp)},
                    timeout=600.0,
                )
                await mcp.call_vivado(
                    "run_tcl",
                    {"command": "place_design -unplace"},
                    timeout=300.0,
                )
                await mcp.call_vivado(
                    "create_and_apply_pblock",
                    {
                        "pblock_name": f"pblock_sweep_{idx}",
                        "ranges": pblock_range,
                        "apply_to": "current_design",
                        "is_soft": is_soft,
                    },
                    timeout=300.0,
                )
                await mcp.call_vivado(
                    "place_design",
                    {"directive": place_directive},
                    timeout=3600.0,
                )
                await mcp.call_vivado(
                    "route_design",
                    {"directive": route_directive},
                    timeout=3600.0,
                )

                route_report = await mcp.call_vivado(
                    "report_route_status",
                    {"show_errors": True},
                    timeout=120.0,
                )
                route = parse_route_status_static(route_report)
                is_legal = bool(route.get("routed_ok"))
                after_wns = await mcp.get_wns()
                if after_wns is None:
                    ts = await mcp.call_vivado(
                        "report_timing_summary", {}, timeout=300.0
                    )
                    parsed = parse_timing_summary_static(ts)
                    after_wns = parsed.get("wns")
                if after_wns is None:
                    after_wns = before_wns

                trial_details.append(
                    {
                        "range": pblock_range,
                        "wns": after_wns,
                        "legal": is_legal,
                    }
                )

                if is_legal:
                    legal_count += 1
                if is_legal and (best_wns is None or after_wns > best_wns):
                    best_wns = after_wns
                    best_range = pblock_range
                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(output_dcp.resolve()), "force": True},
                        timeout=600.0,
                    )

            if best_wns is None or best_range is None:
                return SkillResult.failure(
                    before_wns,
                    "no legal pblock_sweep candidate routed successfully",
                    output_dcp,
                )

            delta = best_wns - before_wns
            if output_dcp.exists():
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(output_dcp.resolve())},
                    timeout=600.0,
                )

            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(best_wns, clock_period)
            short_range = best_range.split(" ")[0]
            if fmax_before and fmax_after:
                summary = (
                    f"pblock_sweep:{short_range} {legal_count}/{len(ranges)} legal "
                    f"wns {before_wns:.3f}→{best_wns:.3f} "
                    f"fmax {fmax_before:.1f}→{fmax_after:.1f} MHz"
                )
            else:
                summary = (
                    f"pblock_sweep:{short_range} {legal_count}/{len(ranges)} legal "
                    f"wns {before_wns:.3f}→{best_wns:.3f} delta={delta:+.3f}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=best_wns,
                delta_wns=delta,
                is_legal=True,
                output_dcp=output_dcp if output_dcp.exists() else Path(current_dcp),
                summary=summary,
                details=json.dumps(
                    {"best_range": best_range, "trials": trial_details},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

        except Exception as e:
            logger.error("[pblock_sweep] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)

    async def _resolve_ranges(
        self,
        mcp,
        target: str,
        params: dict,
        *,
        max_candidates: int,
        allow_hard_macros: bool,
    ) -> list[str]:
        target_text = str(target or "").strip()
        explicit = params.get("candidate_ranges")
        if explicit is not None:
            return _dedupe_keep_order(_parse_candidate_ranges(explicit), max_candidates)
        if target_text and target_text.lower() != "auto":
            return _dedupe_keep_order(_parse_candidate_ranges(target_text), max_candidates)

        raw = await mcp.call_vivado(
            "suggest_slice_pblock_ranges",
            {"max_candidates": max_candidates},
            timeout=300.0,
        )
        data = json.loads(raw) if isinstance(raw, str) else raw
        if data.get("error"):
            raise RuntimeError(str(data["error"]))

        hard_macro_counts = data.get("hard_macro_counts", {})
        hard_macro_total = sum(int(v) for v in hard_macro_counts.values())
        if hard_macro_total and not allow_hard_macros:
            raise RuntimeError(
                "slice-only pblock_sweep refused because hard macros are present; "
                "use pblock/auto or set allow_hard_macros for an explicit experiment"
            )
        if not data.get("slice_only_recommended", True) and not allow_hard_macros:
            raise RuntimeError(
                "slice-only pblock_sweep refused because hard macros are present"
            )

        return _dedupe_keep_order(
            _parse_candidate_ranges(data.get("candidates", [])),
            max_candidates,
        )
