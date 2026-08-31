"""
pblock skill — constrain design to a tighter pblock region and re-place/route.

Dynamic range flow (when target is empty):
  1. Vivado report_utilization_for_pblock  → real LUT/FF/DSP/BRAM counts
  2. RapidWright analyze_fabric_for_pblock  → recommended col/row region for 1.5×
     (enforces DSP/BRAM hard constraints and returns an error if impossible)
  3. RapidWright convert_fabric_region_to_pblock → "SLICE_X..Y.. DSP.. BRAM.." string

If target is non-empty the string is used directly as the pblock range.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from VivadoMCP.pblock_candidates import build_fabric_pblock_candidates

from .base import SkillResult, parse_timing_summary_static, parse_route_status_static, calculate_fmax

logger = logging.getLogger(__name__)


def _parse_utilization(report: str) -> dict:
    """
    Parse the text report from Vivado report_utilization_for_pblock.

    The report has a stable two-block layout:

        === Design Resource Utilization ===

        LUTs:    12,345
        FFs:     23,456
        DSPs:       12
        BRAMs:       8
        URAMs:       0

        === 1.5x Multiplier (for pblock sizing) ===

        LUTs:    18,517
        FFs:     35,184
        ...

    We take the *first* block (real counts) — the pblock-validation path
    needs real counts, not the 1.5x inflated numbers.
    """
    head = report.split("1.5x Multiplier", 1)[0]

    def _num(key: str) -> int:
        m = re.search(rf"{key}:\s*([\d,]+)", head)
        return int(m.group(1).replace(",", "")) if m else 0

    return {
        "lut": _num("LUTs"),
        "ff": _num("FFs"),
        "dsp": _num("DSPs"),
        "bram": _num("BRAMs"),
        "uram": _num("URAMs"),
    }


# Multiplier applied before handing off to analyze_fabric_for_pblock.
# 1.5x matches the multiplier the Vivado tool's text block prints.
RESOURCE_MULTIPLIER = 1.5


def _is_guidance_pblock_place_recoverable(message: str) -> bool:
    msg = str(message)
    return (
        "UTLZ-1" in msg
        or "over-utilized in Pblock" in msg
        or "Resource utilization" in msg
    )


class PblockSkill:
    """Constrain design to a pblock, then re-place and re-route."""

    name = "pblock"

    async def execute(
        self,
        mcp,
        target: str,                # pblock range string or empty for dynamic sizing
        params: dict,               # {input_dcp: str, utilization_target: float}
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        input_dcp = params.get("input_dcp")
        util_target = float(params.get("utilization_target", 0.7))
        place_directive = str(params.get("place_directive", "Default"))
        route_directive = str(params.get("route_directive", "Default"))
        pre_route_phys_opt = str(params.get("pre_route_phys_opt") or "").strip()
        release_after_place = bool(params.get("release_after_place", False))
        second_place_after_release = bool(
            params.get("second_place_after_release", release_after_place)
        )
        post_route_phys_opt = str(params.get("post_route_phys_opt") or "").strip()
        resource_multiplier = float(
            params.get("resource_multiplier", RESOURCE_MULTIPLIER)
        )
        target_ff_multiplier = float(
            params.get("target_ff_multiplier", resource_multiplier)
        )
        guidance_only = bool(params.get("guidance_only", False))
        validate_resources = bool(params.get("validate_resources", not guidance_only))
        max_expansion_attempts = int(
            params.get("max_expansion_attempts", 0 if guidance_only else 3)
        )
        pblock_candidate_ranking = bool(
            params.get("pblock_candidate_ranking", True)
        )
        max_pblock_candidates = int(params.get("max_pblock_candidates", 6))
        output_dcp = run_dir / "pblock_opt.dcp"

        logger.info(f"[pblock] target_range={target!r} utilization_target={util_target}")

        try:
            pblock_ranges = target.strip() if target.strip() else None

            if not pblock_ranges:
                pblock_ranges = await self._compute_dynamic_range(
                    mcp,
                    input_dcp or str(run_dir / "current_best.dcp"),
                    resource_multiplier=resource_multiplier,
                    target_ff_multiplier=target_ff_multiplier,
                    hard_resource_floor=not guidance_only,
                    pblock_candidate_ranking=pblock_candidate_ranking,
                    max_pblock_candidates=max_pblock_candidates,
                )
                if pblock_ranges is None:
                    return SkillResult.failure(
                        before_wns,
                        "dynamic pblock range computation returned no usable region",
                        output_dcp,
                    )
                logger.info(f"[pblock] dynamic range: {pblock_ranges[:200]}")

            # Unplace design
            await mcp.call_vivado(
                "run_tcl",
                {"command": "place_design -unplace"},
                timeout=300.0,
            )

            # Create and apply pblock
            await mcp.call_vivado(
                "create_and_apply_pblock",
                {
                    "pblock_name": "pblock_opt",
                    "ranges": pblock_ranges,
                    "apply_to": "current_design",
                    "is_soft": False,
                    "validate_resources": validate_resources,
                    "max_expansion_attempts": max_expansion_attempts,
                },
                timeout=300.0,
            )

            # A guidance-only pblock is deliberately allowed to be too small:
            # it nudges the first placer pass, is deleted, and the final route
            # gate decides whether the resulting seed is useful.
            try:
                await mcp.call_vivado(
                    "place_design",
                    {"directive": place_directive},
                    timeout=3600.0,
                )
            except Exception as place_error:
                if (
                    guidance_only
                    and release_after_place
                    and _is_guidance_pblock_place_recoverable(str(place_error))
                ):
                    logger.warning(
                        "[pblock] guidance-only first place hit recoverable "
                        "pblock resource DRC; releasing pblock and continuing: %s",
                        place_error,
                    )
                else:
                    raise

            if release_after_place:
                await mcp.call_vivado(
                    "run_tcl",
                    {"command": "delete_pblocks [get_pblocks pblock_opt]"},
                    timeout=300.0,
                )
                if second_place_after_release:
                    await mcp.call_vivado(
                        "place_design",
                        {"directive": place_directive},
                        timeout=3600.0,
                    )

            if pre_route_phys_opt:
                await mcp.call_vivado(
                    "phys_opt_design",
                    {"directive": pre_route_phys_opt},
                    timeout=3600.0,
                )

            # Route
            await mcp.call_vivado(
                "route_design",
                {"directive": route_directive},
                timeout=3600.0,
            )

            if post_route_phys_opt:
                await mcp.call_vivado(
                    "phys_opt_design",
                    {"directive": post_route_phys_opt},
                    timeout=3600.0,
                )

            # Check route status
            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok"))

            # Measure WNS
            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            delta = after_wns - before_wns

            # Write output DCP
            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )

            # Shorten range for the summary (can be several hundred chars)
            short_range = pblock_ranges.split(" ")[0] if " " in pblock_ranges else pblock_ranges
            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"pblock:{short_range} "
                    f"wns {before_wns:.3f}→{after_wns:.3f} fmax {fmax_b:.1f}→{fmax_a:.1f} MHz"
                )
            else:
                summary = f"pblock:{short_range} wns {before_wns:.3f}→{after_wns:.3f} delta={delta:+.3f}"

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=is_legal,
                output_dcp=output_dcp,
                summary=summary,
            )

        except Exception as e:
            logger.error(f"[pblock] failed: {e}")
            return SkillResult.failure(before_wns, str(e), output_dcp)

    # ------------------------------------------------------------------
    # Dynamic range computation (Vivado util → RW fabric → RW convert)
    # ------------------------------------------------------------------

    async def _compute_dynamic_range(
        self,
        mcp,
        dcp_to_open: str,
        *,
        resource_multiplier: float = RESOURCE_MULTIPLIER,
        target_ff_multiplier: float = RESOURCE_MULTIPLIER,
        hard_resource_floor: bool = True,
        pblock_candidate_ranking: bool = True,
        max_pblock_candidates: int = 6,
    ) -> Optional[str]:
        """
        Chain the three MCP tools that together turn 'current design utilization'
        into a concrete pblock range string. Returns None on unrecoverable error
        (the skill turns that into a SkillResult.failure with the underlying msg).
        """
        # Step 1: Vivado utilization report (parser is PRIMITIVE_GROUP-based,
        # so FF on OOC designs is now reliable).
        util_report = await mcp.call_vivado(
            "report_utilization_for_pblock", {}, timeout=300.0
        )
        real = _parse_utilization(util_report)
        if real["lut"] == 0 and real["ff"] == 0:
            logger.error(f"[pblock] got zero LUTs and zero FFs: {util_report[:300]!r}")
            return None
        targets = {k: int(v * resource_multiplier) for k, v in real.items()}
        targets["ff"] = int(real["ff"] * target_ff_multiplier)
        logger.info(
            f"[pblock] real counts: lut={real['lut']} ff={real['ff']} "
            f"dsp={real['dsp']} bram={real['bram']} uram={real['uram']} | "
            f"scaled targets: lut={targets['lut']} ff={targets['ff']} "
            f"dsp={targets['dsp']} bram={targets['bram']}"
        )
        hard_dsp_target = real["dsp"] if hard_resource_floor else 0
        hard_bram_target = real["bram"] if hard_resource_floor else 0
        if not hard_resource_floor:
            logger.info(
                "[pblock] guidance_only: disabling DSP/BRAM hard floors "
                "during fabric-region sizing"
            )

        # Step 2: RapidWright — load design and ask for a recommended fabric region.
        # analyze_fabric_for_pblock now enforces DSP/BRAM counts and returns
        # a structured error on shortfall, so we don't need to re-validate.
        await mcp.call_rw(
            "initialize_rapidwright",
            {"jvm_max_memory": "8G"},
            timeout=120.0,
        )
        await mcp.call_rw(
            "read_checkpoint",
            {"dcp_path": dcp_to_open},
            timeout=600.0,
        )
        fabric_raw = await mcp.call_rw(
            "analyze_fabric_for_pblock",
            {
                "target_lut_count": targets["lut"],
                "target_ff_count": targets["ff"],
                "target_dsp_count": hard_dsp_target,
                "target_bram_count": hard_bram_target,
            },
            timeout=300.0,
        )
        fabric = json.loads(fabric_raw) if isinstance(fabric_raw, str) else fabric_raw
        if "error" in fabric:
            logger.error(f"[pblock] analyze_fabric_for_pblock error: {fabric['error']}")
            return None
        region = fabric.get("recommended_region")
        if not region:
            logger.error(f"[pblock] analyze_fabric_for_pblock missing recommended_region: {fabric_raw[:300]!r}")
            return None
        est = fabric.get("estimated_resources", {})
        logger.info(
            f"[pblock] recommended region: "
            f"cols {region['col_min']}-{region['col_max']} "
            f"rows {region['row_min']}-{region['row_max']} | "
            f"actual sites: slices={est.get('slice_sites', '?')} "
            f"dsp={est.get('dsp_sites', '?')} bram={est.get('bram_sites', '?')}"
        )

        if pblock_candidate_ranking:
            candidate_result = build_fabric_pblock_candidates(
                region,
                fabric_bounds=fabric.get("fabric_bounds"),
                estimated_resources=est,
                target_requirements=fabric.get("target_requirements"),
                design_resources=real,
                max_candidates=max_pblock_candidates,
            )
            candidates = candidate_result["candidates"]
            logger.info(
                "[pblock] ranked fabric candidates: %s",
                [
                    {
                        "name": item["name"],
                        "score": item["score"],
                        "region": item["region"],
                    }
                    for item in candidates
                ],
            )
        else:
            candidates = [{"name": "recommended", "region": region, "score": 0.0}]

        # Step 3: RapidWright — convert ranked fabric regions into
        # SLICE/DSP/BRAM ranges. Conversion is cheap, so fall back to the next
        # ranked region if a wider candidate lands on an empty site interval.
        last_convert_raw = None
        for candidate in candidates:
            candidate_region = candidate["region"]
            convert_raw = await mcp.call_rw(
                "convert_fabric_region_to_pblock",
                {
                    "col_min": candidate_region["col_min"],
                    "col_max": candidate_region["col_max"],
                    "row_min": candidate_region["row_min"],
                    "row_max": candidate_region["row_max"],
                    "use_clock_regions": False,
                },
                timeout=120.0,
            )
            last_convert_raw = convert_raw
            convert = json.loads(convert_raw) if isinstance(convert_raw, str) else convert_raw
            pblock_ranges = convert.get("pblock_ranges")
            if pblock_ranges:
                logger.info(
                    "[pblock] selected ranked candidate %s score=%s region=%s",
                    candidate.get("name"),
                    candidate.get("score"),
                    candidate_region,
                )
                return pblock_ranges

            logger.warning(
                "[pblock] ranked candidate %s did not convert to pblock ranges: %r",
                candidate.get("name"),
                convert_raw,
            )

        logger.error(
            "[pblock] convert_fabric_region_to_pblock missing pblock_ranges: %r",
            str(last_convert_raw)[:300],
        )
        return None
