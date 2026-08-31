"""Exact physical translation of analysis-derived carry and macro structures."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    SkillOutput,
    SkillResult,
    calculate_fmax,
    candidate_budget_exhausted,
    open_rapidwright_dcp_in_vivado,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)

STRUCTURE_RELOCATION_TARGETS = ("carry_cloud", "hard_macro_cascade")
STRUCTURE_ROUTE_DIRECTIVES = ("Default", "Explore", "NoTimingRelaxation")
MAX_OFFSET = 32
MAX_OFFSET_CANDIDATES = 8
MAX_CANDIDATES = 4
# Must not exceed the MCP tool's own schema maximum for its Tcl timeout.
_STRUCTURAL_ANALYSIS_TIMEOUT_S = 600.0
_STRUCTURAL_PREFLIGHT_MAX_CELLS = 128
_STRUCTURAL_MAX_CELLS = 512


def _payload(raw: Any) -> dict:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("MCP tool returned a non-object response")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def normalize_offsets(raw: Any) -> list[tuple[int, int]]:
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_OFFSET_CANDIDATES:
        raise ValueError(f"offsets must contain 1..{MAX_OFFSET_CANDIDATES} [column,row] pairs")
    result = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"offsets[{index}] must be [tile_col_offset,tile_row_offset]")
        col, row = item
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (col, row)):
            raise ValueError(f"offsets[{index}] values must be integers")
        if abs(col) > MAX_OFFSET or abs(row) > MAX_OFFSET:
            raise ValueError(f"offsets[{index}] values must be in -{MAX_OFFSET}..{MAX_OFFSET}")
        if col == 0 and row == 0:
            raise ValueError(f"offsets[{index}] must be nonzero")
        pair = (col, row)
        if pair in seen:
            raise ValueError(f"offsets[{index}] is duplicated")
        seen.add(pair)
        result.append(pair)
    return result


def _select_structure(analysis: dict, structure_type: str, structure_id: str) -> dict:
    structures = analysis.get("structures")
    if not isinstance(structures, list):
        raise RuntimeError("Vivado structure analysis did not return structures")
    eligible = [
        item for item in structures
        if isinstance(item, dict)
        and item.get("structure_type") == structure_type
        and item.get("relocation_eligible") is True
        and item.get("members_truncated") is False
    ]
    if structure_id == "auto":
        if not eligible:
            raise RuntimeError("no complete relocation-eligible structure found")
        eligible.sort(
            key=lambda item: (
                -int(item.get("length", 0)),
                str(item.get("structure_id", "")),
            )
        )
        return eligible[0]
    matches = [item for item in eligible if item.get("structure_id") == structure_id]
    if len(matches) != 1:
        raise RuntimeError("requested structure_id is absent, stale, or ineligible")
    return matches[0]


def _internal_structure_payload(structure: dict) -> dict:
    payload = {
        "structure_type": structure.get("structure_type"),
        "cells": structure.get("cells"),
        "sites": structure.get("sites"),
        "edges": structure.get("edges"),
    }
    if not isinstance(structure.get("content_digest"), str):
        raise RuntimeError("structure analysis omitted its content digest")
    return payload


def _tcl_quote(value: str) -> str:
    return "{" + str(value).replace("\\", "\\\\").replace("}", "\\}") + "}"


def _build_affected_route_tcl(net_names: list[str], directive: str) -> str:
    if directive not in STRUCTURE_ROUTE_DIRECTIVES:
        raise ValueError("route directive is not allowlisted")
    if not net_names or len(net_names) > 32:
        raise ValueError("affected net list must contain 1..32 names")
    names = " ".join(_tcl_quote(name) for name in net_names)
    return "; ".join((
        f"set fdagents_relocation_net_names [list {names}]",
        "set fdagents_relocation_nets [get_nets -quiet $fdagents_relocation_net_names]",
        "if {[llength $fdagents_relocation_nets] != [llength $fdagents_relocation_net_names]} { error {FDAGENTS relocation net missing} }",
        f"route_design -preserve -nets $fdagents_relocation_nets -directive {directive}",
    ))


def _hold_clean(timing: dict) -> bool:
    whs = timing.get("whs")
    ths = timing.get("ths")
    return (
        isinstance(whs, (int, float))
        and isinstance(ths, (int, float))
        and whs >= 0.0
        and ths >= 0.0
    )


class StructureRelocationSkill:
    name = "structure_relocation"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        started_all = time.monotonic()
        run_dir = mcp.run_dir
        failed_dcp = run_dir / "structure_relocation_failed.dcp"
        input_dcp = str(params.get("input_dcp") or run_dir / "current_best.dcp")
        attempts = []
        candidates = []
        budget_stopped = False
        unstarted = []

        try:
            if target not in STRUCTURE_RELOCATION_TARGETS:
                raise ValueError("target must be carry_cloud or hard_macro_cascade")
            structure_id = str(params.get("structure_id", "auto"))
            if structure_id != "auto" and not re.fullmatch(
                rf"{re.escape(target)}:[0-9a-f]{{20}}", structure_id
            ):
                raise ValueError("structure_id does not match the typed target")
            offsets = normalize_offsets(params.get("offsets"))
            max_candidates = params.get("max_candidates", MAX_CANDIDATES)
            if (
                isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
                or not 1 <= max_candidates <= MAX_CANDIDATES
            ):
                raise ValueError(f"max_candidates must be 1..{MAX_CANDIDATES}")
            directive = params.get("route_directive", "Default")
            if directive not in STRUCTURE_ROUTE_DIRECTIVES:
                raise ValueError("route_directive is not allowlisted")

            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            analysis_tool = (
                "analyze_carry_clouds"
                if target == "carry_cloud"
                else "analyze_hard_macro_cascades"
            )
            count_field = (
                "carry_cell_count" if target == "carry_cloud" else "hard_macro_count"
            )
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
            )
            analysis = _payload(await mcp.call_vivado(
                analysis_tool,
                {"max_cells": _STRUCTURAL_PREFLIGHT_MAX_CELLS},
                timeout=300.0,
            ))
            try:
                structure = _select_structure(analysis, target, structure_id)
            except RuntimeError:
                total = analysis.get(count_field)
                if (
                    isinstance(total, int)
                    and not isinstance(total, bool)
                    and _STRUCTURAL_PREFLIGHT_MAX_CELLS < total <= _STRUCTURAL_MAX_CELLS
                ):
                    analysis = _payload(await mcp.call_vivado(
                        analysis_tool,
                        {"max_cells": total, "timeout": _STRUCTURAL_ANALYSIS_TIMEOUT_S},
                        timeout=_STRUCTURAL_ANALYSIS_TIMEOUT_S + 60.0,
                    ))
                structure = _select_structure(analysis, target, structure_id)
            digest = str(structure["content_digest"])
            internal_structure = _internal_structure_payload(structure)

            for offset_index, (col, row) in enumerate(offsets):
                if len(candidates) >= max_candidates:
                    unstarted = [f"offset=({x},{y})" for x, y in offsets[offset_index:]]
                    break
                if candidate_budget_exhausted(
                    params, completed_attempts=len(attempts)
                ):
                    budget_stopped = True
                    unstarted = [f"offset=({x},{y})" for x, y in offsets[offset_index:]]
                    break
                started = time.monotonic()
                label = (
                    f"structure_relocation type={target} offset=({col},{row}) "
                    f"route={directive}"
                )[:120]
                record = {
                    "label": label,
                    "offset": [col, row],
                    "status": "error",
                }
                try:
                    await mcp.call_rw(
                        "read_checkpoint", {"dcp_path": input_dcp}, timeout=600.0
                    )
                    mutation = _payload(await mcp.call_rw(
                        "relocate_structure_cluster",
                        {
                            "structure": internal_structure,
                            "expected_digest": digest,
                            "tile_col_offset": col,
                            "tile_row_offset": row,
                            "max_boundary_nets": 16,
                            "max_boundary_pins": 128,
                        },
                        timeout=600.0,
                    ))
                    rw_scratch = run_dir / f"structure_relocation_{offset_index + 1:02d}_rw.dcp"
                    _payload(await mcp.call_rw(
                        "write_checkpoint",
                        {"dcp_path": str(rw_scratch), "overwrite": True},
                        timeout=600.0,
                    ))
                    if not rw_scratch.is_file():
                        raise RuntimeError("RapidWright scratch checkpoint was not created")

                    await open_rapidwright_dcp_in_vivado(
                        mcp, rw_scratch, timeout=600.0
                    )
                    affected = mutation.get("affected_nets_requiring_route") or []
                    if affected:
                        await mcp.call_vivado(
                            "run_tcl",
                            {"command": _build_affected_route_tcl(affected, directive)},
                            timeout=1200.0,
                        )
                    route = parse_route_status_static(await mcp.call_vivado(
                        "report_route_status", {}, timeout=180.0
                    ))
                    timing = parse_timing_summary_static(await mcp.call_vivado(
                        "report_timing_summary", {}, timeout=300.0
                    ))
                    if not route.get("routed_ok"):
                        raise RuntimeError("skill-local route precheck failed")
                    if not _hold_clean(timing):
                        raise RuntimeError("skill-local hold precheck failed or was unavailable")

                    identity = hashlib.sha256(
                        f"{digest}|{col}|{row}|{directive}".encode("utf-8")
                    ).hexdigest()[:12]
                    candidate_dcp = run_dir / (
                        f"structure_relocation_{offset_index + 1:02d}_{identity}.dcp"
                    )
                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(candidate_dcp.resolve()), "force": True},
                        timeout=600.0,
                    )
                    if not candidate_dcp.is_file():
                        raise RuntimeError("Vivado candidate checkpoint was not created")
                    candidates.append(SkillOutput(candidate_dcp, label))
                    record.update({
                        "status": "legal",
                        "structure_id": structure.get("structure_id"),
                        "content_digest": digest,
                        "route_status": route,
                        "hold_precheck": {
                            "whs": timing.get("whs"),
                            "ths": timing.get("ths"),
                            "clean": True,
                        },
                        "local_vivado_wns": timing.get("wns"),
                        "relocation": {
                            "moved_sites": mutation.get("moved_sites", []),
                            "before_placement_digest": mutation.get("before_placement_digest"),
                            "after_placement_digest": mutation.get("after_placement_digest"),
                            "affected_boundary_nets": mutation.get("affected_boundary_nets", []),
                            "unrouted_pin_count": mutation.get("unrouted_pin_count"),
                            "unrouted_net_count": mutation.get("unrouted_net_count"),
                            "internal_route_preservation": mutation.get("internal_route_preservation", {}),
                        },
                    })
                except Exception as exc:
                    record["error"] = type(exc).__name__
                record["runtime_s"] = round(time.monotonic() - started, 6)
                attempts.append(record)

            details = {
                "attempts": attempts,
                "budget_stopped": budget_stopped,
                "unstarted": unstarted,
                "exact_cluster_definition": (
                    "complete carry_cloud or hard_macro_cascade analysis set; "
                    "arbitrary cell clusters are forbidden"
                ),
                "acceptance_authority": "Vivado central measurement only",
                "runtime_s": round(time.monotonic() - started_all, 6),
            }
            if not candidates:
                return SkillResult.failure(
                    before_wns,
                    "no route- and hold-clean exact structure relocation candidate",
                    failed_dcp,
                    details=details,
                )
            local_values = [
                item.get("local_vivado_wns") for item in attempts
                if isinstance(item.get("local_vivado_wns"), (int, float))
            ]
            after_wns = max(local_values) if local_values else before_wns
            fmax_before = calculate_fmax(before_wns, clock_period)
            fmax_after = calculate_fmax(after_wns, clock_period)
            summary = (
                f"structure_relocation {target} candidates={len(candidates)} "
                f"local_wns={before_wns:.3f}->{after_wns:.3f}"
            )
            if fmax_before is not None and fmax_after is not None:
                summary += f" local_fmax={fmax_before:.1f}->{fmax_after:.1f}MHz"
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=after_wns - before_wns,
                is_legal=True,
                output_dcp=candidates[0].output_dcp,
                summary=summary[:200],
                details=details,
                candidates=tuple(candidates),
            )
        except Exception as exc:
            logger.error("[structure_relocation] failed: %s", exc)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "attempts": attempts,
                    "budget_stopped": budget_stopped,
                    "unstarted": unstarted,
                    "exact_cluster_definition": (
                        "complete carry_cloud or hard_macro_cascade analysis set; "
                        "arbitrary cell clusters are forbidden"
                    ),
                    "acceptance_authority": "Vivado central measurement only",
                    "runtime_s": round(time.monotonic() - started_all, 6),
                },
            )
