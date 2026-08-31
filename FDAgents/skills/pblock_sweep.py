"""
pblock_sweep skill — try a small set of generic slice pblock candidates.

The learned pattern is not tied to benchmark names: compact central/bbox
windows remain the baseline siblings.  When RapidWright Device analysis is
available, the same public ``auto`` target also races clock-region-aligned
single pblocks and a disjoint two-region partition (critical cluster plus
remainder).  The skill evaluates them and keeps the best legal checkpoint.
"""

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Optional

from VivadoMCP.pblock_candidates import build_clock_region_pblock_candidates

from .base import (
    SkillOutput,
    SkillResult,
    candidate_budget_exhausted,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)
from .pblock import PblockSkill, _parse_utilization

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


def _candidate_description(candidate: dict[str, Any]) -> str:
    if candidate.get("kind") == "clock_region_multi":
        regions = candidate.get("regions", [])
        by_role = {item.get("role"): item.get("range") for item in regions}
        return f"critical={by_role.get('critical')}|remainder={by_role.get('remainder')}"
    return "range=" + str(candidate.get("range") or "")


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

        attempts: list[dict] = []
        candidate_feedback: list[dict] = []
        candidate_outputs: list[SkillOutput] = []
        budget_stopped = False
        unstarted: list[str] = []

        try:
            candidates = await self._resolve_candidates(
                mcp,
                target,
                params,
                current_dcp=str(current_dcp),
                max_candidates=max_candidates,
                allow_hard_macros=allow_hard_macros,
            )
            if not candidates:
                return SkillResult.failure(
                    before_wns,
                    "no pblock ranges to sweep",
                    output_dcp,
                    details={
                        "attempts": attempts,
                        "candidates": candidate_feedback,
                        "budget_stopped": budget_stopped,
                        "unstarted": unstarted,
                    },
                )

            best_wns = None
            best_description = None
            legal_count = 0
            labels = [
                f"pblock_sweep variant={idx:02d} {_candidate_description(candidate)}"[:120]
                for idx, candidate in enumerate(candidates, 1)
            ]

            for idx, candidate in enumerate(candidates):
                if candidate_budget_exhausted(
                    params, completed_attempts=len(attempts)
                ):
                    budget_stopped = True
                    unstarted = labels[idx:]
                    break
                started = time.monotonic()
                label = labels[idx]
                description = _candidate_description(candidate)
                candidate_dcp = run_dir / f"pblock_sweep_{idx + 1:02d}.dcp"
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
                if candidate.get("kind") == "clock_region_multi":
                    regions = {
                        item["role"]: item for item in candidate.get("regions", [])
                    }
                    apply_result = await mcp.call_vivado(
                        "create_and_apply_multiregion_pblocks",
                        {
                            "pblock_prefix": f"pblock_sweep_{idx}",
                            "critical_range": regions["critical"]["range"],
                            "remainder_range": regions["remainder"]["range"],
                            "critical_cell_names": candidate["critical_cell_names"],
                            "is_soft": is_soft,
                            "validate_resources": True,
                        },
                        timeout=300.0,
                    )
                    if str(apply_result).lower().startswith("error"):
                        feedback = {
                            "label": label,
                            "candidate_kind": "clock_region_multi",
                            "status": "rejected",
                            "reason": str(apply_result)[:500],
                            "runtime_s": time.monotonic() - started,
                        }
                        attempts.append(feedback)
                        continue
                else:
                    await mcp.call_vivado(
                        "create_and_apply_pblock",
                        {
                            "pblock_name": f"pblock_sweep_{idx}",
                            "ranges": candidate["range"],
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

                runtime_s = time.monotonic() - started
                feedback = {
                    "label": label,
                    "candidate_kind": candidate.get("kind", "single"),
                    "status": "legal" if is_legal else "illegal",
                    "local_metrics": {
                        "wns": after_wns,
                        "delta_wns": after_wns - before_wns,
                    },
                    "runtime_s": runtime_s,
                }
                attempts.append(feedback)

                if is_legal:
                    legal_count += 1
                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                        timeout=600.0,
                    )
                    candidate_outputs.append(SkillOutput(candidate_dcp, label))
                    candidate_feedback.append(feedback)
                if is_legal and (best_wns is None or after_wns > best_wns):
                    best_wns = after_wns
                    best_description = description
                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(output_dcp.resolve()), "force": True},
                        timeout=600.0,
                    )

            if best_wns is None or best_description is None:
                error = "no legal pblock_sweep candidate routed successfully"
                return SkillResult(
                    success=False,
                    before_wns=before_wns,
                    after_wns=before_wns,
                    delta_wns=0.0,
                    is_legal=False,
                    output_dcp=output_dcp,
                    summary=f"FAILED: {error}",
                    error_msg=error,
                    details={
                        "attempts": attempts,
                        "candidates": candidate_feedback,
                        "budget_stopped": budget_stopped,
                        "unstarted": unstarted,
                    },
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
            short_range = best_description.split(" ")[0]
            if fmax_before and fmax_after:
                summary = (
                    f"pblock_sweep:{short_range} {legal_count}/{len(candidates)} legal "
                    f"wns {before_wns:.3f}→{best_wns:.3f} "
                    f"fmax {fmax_before:.1f}→{fmax_after:.1f} MHz"
                )
            else:
                summary = (
                    f"pblock_sweep:{short_range} {legal_count}/{len(candidates)} legal "
                    f"wns {before_wns:.3f}→{best_wns:.3f} delta={delta:+.3f}"
                )
            if budget_stopped:
                summary += f" budget_stop {len(attempts)}/{len(candidates)}"

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=best_wns,
                delta_wns=delta,
                is_legal=True,
                output_dcp=output_dcp if output_dcp.exists() else Path(current_dcp),
                summary=summary,
                details={
                    "attempts": attempts,
                    "candidates": candidate_feedback,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                },
                candidates=tuple(candidate_outputs),
            )

        except Exception as e:
            logger.error("[pblock_sweep] failed: %s", e)
            return SkillResult.failure(
                before_wns,
                str(e),
                output_dcp,
                details={
                    "attempts": attempts,
                    "candidates": candidate_feedback,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                },
            )

    async def _resolve_ranges(
        self,
        mcp,
        target: str,
        params: dict,
        *,
        current_dcp: str,
        max_candidates: int,
        allow_hard_macros: bool,
    ) -> list[str]:
        """Backward-compatible range-only view used by older local callers."""
        candidates = await self._resolve_candidates(
            mcp,
            target,
            params,
            current_dcp=str(params.get("input_dcp") or mcp.run_dir / "current_best.dcp"),
            max_candidates=max_candidates,
            allow_hard_macros=allow_hard_macros,
        )
        return [
            candidate["range"] for candidate in candidates
            if candidate.get("kind") != "clock_region_multi"
        ]

    async def _resolve_candidates(
        self,
        mcp,
        target: str,
        params: dict,
        *,
        current_dcp: str,
        max_candidates: int,
        allow_hard_macros: bool,
    ) -> list[dict[str, Any]]:
        target_text = str(target or "").strip()
        explicit = params.get("candidate_ranges")
        if explicit is not None:
            return [
                {"name": f"explicit_{idx}", "kind": "single", "range": value}
                for idx, value in enumerate(
                    _dedupe_keep_order(_parse_candidate_ranges(explicit), max_candidates), 1
                )
            ]
        if target_text and target_text.lower() != "auto":
            return [
                {"name": f"explicit_{idx}", "kind": "single", "range": value}
                for idx, value in enumerate(
                    _dedupe_keep_order(_parse_candidate_ranges(target_text), max_candidates), 1
                )
            ]

        # ``auto`` owns the legal resource shape.  A SLICE-only range cannot
        # contain a design's DSP/BRAM/URAM sites, so discover that inexpensive
        # fact before running the much heavier slice-candidate census.  Route
        # hard-macro designs through the existing generic fabric-region flow;
        # this is the mixed-resource implementation of the same Luna-selected
        # pblock action, not a policy veto or a strategy substitution.
        util_report = await mcp.call_vivado(
            "report_utilization_for_pblock", {}, timeout=300.0
        )
        utilization = _parse_utilization(util_report)
        if any(utilization.get(key, 0) > 0 for key in ("dsp", "bram", "uram")):
            mixed_range = await PblockSkill()._compute_dynamic_range(
                mcp,
                current_dcp,
                max_pblock_candidates=max_candidates,
            )
            return [mixed_range] if mixed_range else []

        async def current_bbox_fallback() -> list[dict[str, Any]]:
            """Return only the exact occupied bbox derived from this seed."""
            raw = await mcp.call_vivado(
                "suggest_slice_pblock_ranges",
                {"max_candidates": 1},
                timeout=300.0,
            )
            data = json.loads(raw) if isinstance(raw, str) else raw
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            ranges = _dedupe_keep_order(
                _parse_candidate_ranges(data.get("candidates", [])), 1
            )
            return [
                {"name": "current_occupied_bbox", "kind": "single", "range": value}
                for value in ranges
            ]

        # The capacity-aware clock grid is the normal SLICE-only discovery
        # path.  The exact occupied bbox is only a compatibility fallback; no
        # fixed center ratio, padding, or historical window enters the menu.
        if not hasattr(mcp, "call_rw"):
            return await current_bbox_fallback()
        try:
            pins_raw = await mcp.call_vivado(
                "extract_critical_path_pins",
                {"num_paths": int(params.get("critical_cluster_paths", 3))},
                timeout=600.0,
            )
            paths = json.loads(pins_raw) if isinstance(pins_raw, str) else pins_raw
            critical_cells = []
            seen_cells = set()
            for path in paths if isinstance(paths, list) else []:
                for pin in path if isinstance(path, list) else []:
                    cell = str(pin).rsplit("/", 1)[0] if "/" in str(pin) else ""
                    if cell and cell not in seen_cells:
                        seen_cells.add(cell)
                        critical_cells.append(cell)
                    if len(critical_cells) >= 256:
                        break
                if len(critical_cells) >= 256:
                    break
            if not critical_cells:
                return await current_bbox_fallback()

            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": current_dcp}, timeout=600.0
            )
            grid_raw = await mcp.call_rw(
                "analyze_clock_region_pblock_grid",
                {"critical_cell_names": critical_cells},
                timeout=300.0,
            )
            grid = json.loads(grid_raw) if isinstance(grid_raw, str) else grid_raw
            if not isinstance(grid, dict) or grid.get("error"):
                return await current_bbox_fallback()
            matched_cells = list(grid.get("matched_critical_cell_names") or [])
            if not matched_cells:
                return await current_bbox_fallback()

            margin = float(params.get("clock_region_capacity_margin", 1.15))
            margin = max(1.0, min(margin, 2.0))

            def scaled(value: Any) -> dict[str, int]:
                source = value if isinstance(value, dict) else {}
                return {
                    key: int(math.ceil(int(source.get(key, 0)) * margin))
                    for key in ("lut", "ff", "dsp", "bram", "uram")
                }

            generated = build_clock_region_pblock_candidates(
                grid.get("clock_regions", []),
                critical_requirements=scaled(grid.get("critical_requirements")),
                remainder_requirements=scaled(grid.get("remainder_requirements")),
                max_single_candidates=2,
                max_multi_candidates=2,
            ).get("candidates", [])
            for candidate in generated:
                if candidate.get("kind") == "clock_region_multi":
                    candidate["critical_cell_names"] = matched_cells

            singles = [
                item for item in generated if item.get("kind") == "clock_region_single"
            ]
            multis = [
                item for item in generated if item.get("kind") == "clock_region_multi"
            ]
            siblings = []
            if singles:
                siblings.append(singles[0])
            if multis:
                siblings.append(multis[0])
            siblings.extend(singles[1:])
            siblings.extend(multis[1:])
            if not siblings:
                return await current_bbox_fallback()
            return siblings[:max_candidates]
        except Exception as exc:
            logger.warning(
                "[pblock_sweep] clock-region sibling discovery unavailable: %s", exc
            )
            return await current_bbox_fallback()
