"""
lut_merge skill — combine chained small LUTs on critical paths into one LUT6.

Wraps RapidWright's optimize_lut_input_cone MCP tool. Route-dominated
critical paths can still contain 3-5 LUT levels, including BRAM control
enables driven through mixed LUT3/LUT4/LUT5 chains. Collapsing a 3-LUT
chain into a single LUT6 removes 2 LUT-propagation delays AND the
inter-LUT routing between them — both of which compound.

Flow (mirrors cell_replace but with optimize_lut_input_cone in place of
optimize_cell_placement):

  1. Vivado extract_critical_path_pins → JSON pin list per path
  2. Python: pick destination pins of the N worst paths where the
     endpoint is a plausible LUT-cone sink (FF data/set/reset/ce,
     BRAM control, or explicit LUT input). Skip DSP internal pins.
  3. RapidWright read_checkpoint → optimize_lut_input_cone(pins)
  4. Write checkpoint → Vivado open → route_design → measure WNS.

`target` semantics:
  "auto"  — auto-pick pins from critical paths (default)
  "cell_name"       — LLM-specified single cell; we'll synthesize pins
                      ending in /D, /I0..I5 for that cell
  "pin1,pin2,..."   — LLM-specified hierarchical pin list (comma separated)
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    open_rapidwright_dcp_in_vivado,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


# Pin-name patterns that are valid destinations for a LUT-input cone.
# FF data/set/reset/clock-enable inputs, LUT inputs, and BRAM control pins.
# We deliberately skip DSP internal sub-primitive pins (DSP_*_INST/...)
# because the merge operates on LUT chains, not DSP macros.
_LUT_CONE_SINK_RE = re.compile(
    r"/(D|S|R|CE|I[0-9]|ENBWREN|ENARDEN|WEA|WEBWE|REGCEAREGCE|REGCEB)$"
)
_DSP_SUBPRIM_RE = re.compile(r"/DSP_[A-Z_]+_INST/")


def _pick_pins_from_paths(
    critical_paths: list,
    max_pins: int = 20,
    num_paths: int = 5,
) -> list[str]:
    """
    From the top N worst critical paths, collect LUT-cone sink pins.

    Preference order:
      1. Endpoint pins of each path (the last pin, which is the FF/BRAM input)
      2. Any LUT input pin along the path

    Deduplicates; caps total to max_pins.
    """
    collected: list[str] = []
    seen: set[str] = set()

    for path in critical_paths[:num_paths]:
        if not isinstance(path, list) or not path:
            continue

        # 1) Endpoint pin (last entry in the path)
        endpoint = path[-1]
        if (
            isinstance(endpoint, str)
            and _LUT_CONE_SINK_RE.search(endpoint)
            and not _DSP_SUBPRIM_RE.search(endpoint)
            and endpoint not in seen
        ):
            collected.append(endpoint)
            seen.add(endpoint)

        # 2) Mid-path LUT input pins
        for pin in path:
            if not isinstance(pin, str):
                continue
            if pin in seen:
                continue
            if _DSP_SUBPRIM_RE.search(pin):
                continue
            if re.search(r"/I[0-9]$", pin):
                collected.append(pin)
                seen.add(pin)

        if len(collected) >= max_pins:
            break

    return collected[:max_pins]


# Minimum number of LUTs in a pin's fanin cone (up to LUT_FANIN_DEPTH levels
# back) before the pin is considered a worthwhile lut_merge target. A pin
# whose fanin has only 1 LUT (or none) cannot be collapsed — the RW tool
# will instead replicate the driver, which is what produced the observed
# regressions on several low/medium-utilization route-bound profiles.
MIN_LUTS_IN_FANIN = 2
LUT_FANIN_DEPTH = 6


@dataclass(frozen=True)
class FaninLutCheck:
    """Typed, fail-closed result from the mutation precondition check."""

    kept_pins: tuple[str, ...]
    counts: dict[str, int]
    rejection: Optional[str] = None


async def _filter_pins_by_fanin_luts(
    mcp, pins: list[str]
) -> FaninLutCheck:
    """
    For each pin, ask Vivado how many LUT primitives sit in its fanin cone
    up to LUT_FANIN_DEPTH levels back. Keep only pins with
    >= MIN_LUTS_IN_FANIN.

    A failed Tcl check is a typed rejection.  It must never pass unchecked
    pins to the mutating RapidWright optimizer.
    """
    if not pins:
        return FaninLutCheck((), {})

    # Wrap each pin in literal braces so Tcl doesn't interpret brackets /
    # slashes inside bus indexing (e.g. "sdpram[3]/pipe_3_tvalid/I3").
    pinlist_tcl = " ".join("{" + p + "}" for p in pins)
    cmd = (
        f"set pins [list {pinlist_tcl}]; "
        "set out {}; "
        "foreach p $pins { "
        "  set pin [get_pins -quiet $p]; "
        "  if {[llength $pin] == 0} { lappend out \"$p=-1\"; continue }; "
        f"  set cone [all_fanin -flat -levels {LUT_FANIN_DEPTH} -to $pin]; "
        "  set luts [get_cells -quiet -of_objects $cone "
        "            -filter {IS_PRIMITIVE && REF_NAME =~ LUT*}]; "
        "  lappend out \"$p=[llength $luts]\"; "
        "}; "
        "puts [join $out \"\\n\"]"
    )

    try:
        resp = await mcp.call_vivado("run_tcl", {"command": cmd}, timeout=120.0)
    except Exception as e:
        rejection = f"fanin LUT precondition check failed: {e}"
        logger.warning(f"[lut_merge] {rejection}; rejecting action")
        return FaninLutCheck((), {}, rejection)

    counts: dict[str, int] = {}
    for line in (resp or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        pin_name, _, value = line.rpartition("=")
        try:
            counts[pin_name] = int(value)
        except ValueError:
            continue

    kept = [p for p in pins if counts.get(p, -1) >= MIN_LUTS_IN_FANIN]
    return FaninLutCheck(tuple(kept), counts)


class LutMergeSkill:
    """
    Merge LUT input cones on critical paths via RapidWright ECO.

    Best used when Logic Levels >= 3 on the critical path; a single-LUT
    path has nothing to merge and will return with 0 optimizations.
    """

    name = "lut_merge"

    async def execute(
        self,
        mcp,
        target: str,                # "auto" or pin list or cell name
        params: dict,               # {input_dcp, num_paths, max_pins}
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        input_dcp = params.get("input_dcp")
        num_paths = int(params.get("num_paths", 10))
        max_pins = int(params.get("max_pins", 20))
        output_dcp = run_dir / "lut_merge.dcp"

        logger.info(f"[lut_merge] target={target!r} num_paths={num_paths} max_pins={max_pins}")

        try:
            # --------------------------------------------------------------
            # Step 1: Figure out which pins to target
            # --------------------------------------------------------------
            target_pins: list[str] = []
            target = (target or "auto").strip()

            if target == "auto":
                pins_file = run_dir / "critical_path_pins.json"
                await mcp.call_vivado(
                    "extract_critical_path_pins",
                    {"num_paths": num_paths, "output_file": str(pins_file)},
                    timeout=600.0,
                )
                if not pins_file.exists():
                    return SkillResult.failure(
                        before_wns, "extract_critical_path_pins produced no file", output_dcp
                    )
                critical_paths = json.loads(pins_file.read_text())
                target_pins = _pick_pins_from_paths(
                    critical_paths, max_pins=max_pins, num_paths=num_paths
                )
                if not target_pins:
                    return SkillResult.failure(
                        before_wns,
                        "no valid LUT-cone sink pins found on critical paths "
                        "(may be a DSP-only path — nothing to merge)",
                        output_dcp,
                    )
            elif "/" in target and "," not in target and not re.search(r"/[A-Z]", target.split("/")[-1]):
                # Looks like a plain cell name ("module/cell"), no pin suffix
                # → synthesize common fanin sink pins
                target_pins = [f"{target}/{p}" for p in ("D", "I0", "I1", "I2", "I3", "I4", "I5")]
            else:
                # Treat as a comma-separated list of hierarchical pins
                target_pins = [p.strip() for p in target.split(",") if p.strip()]

            logger.info(f"[lut_merge] targeting {len(target_pins)} pins: {target_pins[:5]}{'…' if len(target_pins) > 5 else ''}")

            # --------------------------------------------------------------
            # Step 1b: Gate each pin by its fanin LUT depth.
            # optimize_lut_input_cone only *collapses* LUT chains; on a pin
            # whose fanin is a single LUT (or a DSP/FF direct driver) it
            # *replicates* the driver instead, which produced the −0.443 /
            # −0.480 / −0.155 ns regressions observed in the batch run.
            # --------------------------------------------------------------
            fanin_check = await _filter_pins_by_fanin_luts(mcp, target_pins)
            if fanin_check.rejection is not None:
                return SkillResult.failure(
                    before_wns,
                    fanin_check.rejection,
                    output_dcp,
                    details={
                        "typed_rejection": {
                            "reason_code": "FANIN_CHECK_FAILED",
                            "message": fanin_check.rejection,
                        }
                    },
                )
            kept_pins = list(fanin_check.kept_pins)
            fanin_counts = fanin_check.counts
            if fanin_counts:
                # Log the per-pin counts for the first few; helpful for debug
                preview = [(p, fanin_counts.get(p, -1)) for p in target_pins[:5]]
                logger.info(
                    f"[lut_merge] fanin LUT counts (≥{MIN_LUTS_IN_FANIN} kept): {preview}"
                )
            if not kept_pins:
                return SkillResult.failure(
                    before_wns,
                    f"no target pin has ≥{MIN_LUTS_IN_FANIN} LUTs in its "
                    f"{LUT_FANIN_DEPTH}-level fanin cone; this design's "
                    f"critical path has insufficient LUT depth for cone "
                    f"merging (max observed: {max(fanin_counts.values(), default=0)})",
                    output_dcp,
                )
            if len(kept_pins) < len(target_pins):
                logger.info(
                    f"[lut_merge] fanin filter kept {len(kept_pins)}/"
                    f"{len(target_pins)} pins"
                )
            target_pins = kept_pins

            # --------------------------------------------------------------
            # Step 2: RapidWright — load design and run the cone optimizer
            # --------------------------------------------------------------
            await mcp.call_rw(
                "initialize_rapidwright",
                {"jvm_max_memory": "8G"},
                timeout=120.0,
            )
            dcp_to_open = input_dcp or str(run_dir / "current_best.dcp")
            await mcp.call_rw(
                "read_checkpoint",
                {"dcp_path": dcp_to_open},
                timeout=600.0,
            )

            merge_result = await mcp.call_rw(
                "optimize_lut_input_cone",
                {"hierarchical_input_pins": target_pins},
                timeout=600.0,
            )

            merge_json = json.loads(merge_result) if isinstance(merge_result, str) else merge_result
            if "error" in merge_json:
                return SkillResult.failure(
                    before_wns, f"optimize_lut_input_cone: {merge_json['error']}", output_dcp
                )

            optimized_count = int(merge_json.get("optimized_count", 0))
            total = int(merge_json.get("total_pins", len(target_pins)))
            optimized_cells = [
                str((item.get("new_cell") or {}).get("name") or "")
                for item in (merge_json.get("results") or [])
                if isinstance(item, dict) and item.get("status") == "optimized"
            ]
            if (
                len(optimized_cells) != optimized_count
                or any(not name or "}" in name for name in optimized_cells)
            ):
                return SkillResult.failure(
                    before_wns,
                    "RapidWright omitted a valid identity for an optimized LUT",
                    output_dcp,
                    details={
                        "typed_rejection": {
                            "reason_code": "LUT_MERGE_OPTIMIZED_CELL_IDENTITY_INVALID",
                            "message": "optimized LUT identities are incomplete",
                        }
                    },
                )
            logger.info(f"[lut_merge] merged {optimized_count}/{total} cones")

            if optimized_count == 0:
                # Not an error — the tool correctly reported nothing to merge.
                # Let the agent loop blacklist this (skill, target) pair.
                return SkillResult.failure(
                    before_wns,
                    f"no LUT cones were mergeable (0/{total} pins optimized); "
                    f"likely means critical path has logic levels <= 1 or is "
                    f"not a LUT chain (e.g. DSP-dominated)",
                    output_dcp,
                )

            # --------------------------------------------------------------
            # Step 3: Write RW DCP, re-route in Vivado, measure WNS
            # --------------------------------------------------------------
            rw_dcp = run_dir / "lut_merge_rw.dcp"
            await mcp.call_rw(
                "write_checkpoint",
                {"dcp_path": str(rw_dcp), "overwrite": True},
                timeout=600.0,
            )
            if not rw_dcp.exists():
                return SkillResult.failure(before_wns, "RapidWright DCP not created", output_dcp)

            await open_rapidwright_dcp_in_vivado(mcp, rw_dcp, timeout=600.0)

            # Merged LUT may be unplaced; run place_design first if so.
            # Cheap no-op if everything is already placed.
            try:
                await mcp.call_vivado(
                    "place_design",
                    {"directive": "Default"},
                    timeout=1800.0,
                )
            except Exception as e:
                # Continuing into route_design after placement rejected a LUT6
                # mapping produced deterministic F6LUT/A6 routing failures.
                # Placement is a cheap mandatory admission gate, not a warning.
                return SkillResult.failure(
                    before_wns,
                    f"lut_merge placement rejected the merged LUT mapping: {e}",
                    output_dcp,
                    details={
                        "typed_rejection": {
                            "reason_code": "LUT_MERGE_PLACEMENT_LEGALITY_REJECTED",
                            "message": str(e)[:500],
                        }
                    },
                )

            cell_list = " ".join("{" + name + "}" for name in optimized_cells)
            placement_check = await mcp.call_vivado(
                "run_tcl",
                {
                    "command": (
                        f"set fdagents_cells [get_cells -quiet [list {cell_list}]]; "
                        f"if {{[llength $fdagents_cells] != {optimized_count}}} "
                        "{error {merged LUT cell inventory changed}}; "
                        "set fdagents_unplaced [filter $fdagents_cells "
                        "{STATUS == UNPLACED}]; "
                        'puts "FDAGENTS_LUT_MERGE_UNPLACED=[llength '
                        '$fdagents_unplaced]"'
                    )
                },
                timeout=120.0,
            )
            unplaced_match = re.search(
                r"FDAGENTS_LUT_MERGE_UNPLACED=(\d+)", str(placement_check)
            )
            if unplaced_match is None or int(unplaced_match.group(1)) != 0:
                return SkillResult.failure(
                    before_wns,
                    "lut_merge left an optimized LUT unplaced; route was not started",
                    output_dcp,
                    details={
                        "typed_rejection": {
                            "reason_code": "LUT_MERGE_UNPLACED_OPTIMIZED_CELL",
                            "message": str(placement_check)[:500],
                        }
                    },
                )

            await mcp.call_vivado(
                "route_design",
                {"directive": "Default"},
                timeout=3600.0,
            )

            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok"))

            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            delta = after_wns - before_wns

            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )

            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"lut_merge merged={optimized_count}/{total} "
                    f"wns {before_wns:.3f}→{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}→{fmax_a:.1f} MHz"
                )
            else:
                summary = (
                    f"lut_merge merged={optimized_count}/{total} "
                    f"wns {before_wns:.3f}→{after_wns:.3f} delta={delta:+.3f}"
                )

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
            logger.error(f"[lut_merge] failed: {e}")
            return SkillResult.failure(before_wns, str(e), output_dcp)
