"""
lut_pin_swap skill — direct LUT input pin remapping via RapidWright.

This skill targets route-dominated LUT input delay without changing latency or
logical function. It asks RapidWright's LUTTools to move a logical LUT input to
a different physical input pin, unroutes the affected nets, then lets Vivado
route and time the result.

Target semantics:
  "auto"              — pick LUT input pins from current critical paths
  "cell/I2"           — move that logical pin to FASTEST
  "cell:I2->A6"       — explicit cell and one swap
  params["pin_swaps"] — explicit swaps for params["cell_name"] or target cell
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from .base import (
    SkillOutput,
    SkillResult,
    candidate_budget_exhausted,
    calculate_fmax,
    open_rapidwright_dcp_in_vivado,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


_LUT_INPUT_PIN_RE = re.compile(r"/I[0-5]$")
_DSP_SUBPRIM_RE = re.compile(r"/DSP_[A-Z_]+_INST/")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _pick_lut_input_pins_from_paths(
    critical_paths: list,
    max_pins: int = 8,
    num_paths: int = 5,
) -> list[str]:
    """Collect unique LUT input pins from the worst timing paths."""
    pins: list[str] = []
    seen: set[str] = set()

    for path in critical_paths[:num_paths]:
        if not isinstance(path, list):
            continue
        for pin in path:
            if not isinstance(pin, str):
                continue
            if pin in seen or _DSP_SUBPRIM_RE.search(pin):
                continue
            if not _LUT_INPUT_PIN_RE.search(pin):
                continue
            pins.append(pin)
            seen.add(pin)
            if len(pins) >= max_pins:
                return pins

    return pins


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("_", value).strip("_") or "auto"


def _route_directives_from_params(params: dict) -> list[str]:
    raw = params.get("route_directives")
    if raw is None:
        raw = params.get("route_directive", "Default")

    if isinstance(raw, str):
        candidates = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(item).strip() for item in raw]
    else:
        candidates = [str(raw).strip()]

    directives: list[str] = []
    for directive in candidates:
        if not directive:
            continue
        if directive not in directives:
            directives.append(directive)
    return directives or ["Default"]


def _parse_explicit_target(target: str, params: dict) -> dict[str, list[list[str]]]:
    """Return {cell_name: [[from_pin, to_pin], ...]} for non-auto targets."""
    target = (target or "").strip()
    explicit_swaps = params.get("pin_swaps")
    cell_name = params.get("cell_name")

    if explicit_swaps:
        if cell_name:
            return {str(cell_name): explicit_swaps}
        if target and "/" in target and "->" not in target and ":" not in target:
            maybe_cell, maybe_pin = target.rsplit("/", 1)
            if re.fullmatch(r"I[0-5]", maybe_pin):
                return {maybe_cell: explicit_swaps}
        if target:
            return {target: explicit_swaps}
        raise ValueError("pin_swaps requires cell_name or a cell target")

    if "->" in target:
        lhs, rhs = target.split("->", 1)
        lhs = lhs.strip()
        rhs = rhs.strip()
        if ":" in lhs:
            cell, old_pin = lhs.rsplit(":", 1)
        else:
            cell, old_pin = lhs.rsplit("/", 1)
        return {cell: [[old_pin, rhs]]}

    if "/" in target:
        cell, maybe_pin = target.rsplit("/", 1)
        if re.fullmatch(r"I[0-5]", maybe_pin):
            return {cell: [[maybe_pin, "FASTEST"]]}

    raise ValueError(
        "explicit lut_pin_swap target must be 'auto', 'cell/Ix', "
        "'cell:Ix->Ay', or provide params.pin_swaps"
    )


def _build_raw_iteration_swaps(
    target_pins: list[str],
    max_cells: int,
    preferred_pin: str,
    accepted_swap_keys: set[tuple[str, str]],
) -> dict[str, list[list[str]]]:
    iteration_swaps: dict[str, list[list[str]]] = {}
    for pin in target_pins:
        cell, logical_pin = pin.rsplit("/", 1)
        if cell in iteration_swaps:
            continue
        if (cell, logical_pin) in accepted_swap_keys:
            continue
        iteration_swaps[cell] = [[logical_pin, preferred_pin]]
        if len(iteration_swaps) >= max_cells:
            break
    return iteration_swaps


def _build_prefiltered_iteration_swaps(
    inspect_result: dict,
    max_cells: int,
    preferred_pin: str,
    accepted_swap_keys: set[tuple[str, str]],
) -> Optional[dict[str, list[list[str]]]]:
    """Build swaps from RW inspect result.

    Returns None when the inspect response is not usable and callers should
    fall back to raw critical pins. Returns an empty dict when RW successfully
    inspected the pins and found no actionable candidate.
    """
    if not isinstance(inspect_result, dict):
        return None
    if inspect_result.get("status") != "success":
        return None
    candidates = inspect_result.get("candidates")
    if not isinstance(candidates, list):
        return None

    use_inspected_fastest = str(preferred_pin).upper() == "FASTEST"
    iteration_swaps: dict[str, list[list[str]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if not candidate.get("is_actionable"):
            continue
        cell = str(candidate.get("cell") or "")
        logical_pin = str(candidate.get("logical_pin") or "").upper()
        if not cell or not re.fullmatch(r"I[0-5]", logical_pin):
            continue
        if cell in iteration_swaps:
            continue
        if (cell, logical_pin) in accepted_swap_keys:
            continue

        target_pin = (
            candidate.get("fastest_target_physical_pin")
            if use_inspected_fastest
            else preferred_pin
        )
        if not target_pin:
            continue
        iteration_swaps[cell] = [[logical_pin, str(target_pin)]]
        if len(iteration_swaps) >= max_cells:
            break

    return iteration_swaps


class LutPinSwapSkill:
    """Swap critical LUT inputs onto faster physical pins."""

    name = "lut_pin_swap"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        run_dir = mcp.run_dir
        input_dcp = params.get("input_dcp")
        num_paths = int(params.get("num_paths", 5))
        max_pins = int(params.get("max_pins", 6))
        max_cells = int(params.get("max_cells", 4))
        route_directive = params.get("route_directive", "Default")
        route_directives = _route_directives_from_params(params)
        max_iterations = max(1, min(int(params.get("max_iterations", 2)), 5))
        accept_min_delta = float(params.get("accept_min_delta", 0.001))
        preferred_pin = params.get("preferred_pin", "FASTEST")
        safe_target = _safe_name(target or "auto")
        output_dcp = run_dir / f"lut_pin_swap_{safe_target}.dcp"

        logger.info(
            "[lut_pin_swap] target=%r num_paths=%d max_pins=%d max_cells=%d",
            target,
            num_paths,
            max_pins,
            max_cells,
        )

        try:
            target = (target or "auto").strip()
            swaps_by_cell: dict[str, list[list[str]]] = {}

            if target != "auto":
                swaps_by_cell = _parse_explicit_target(target, params)

            if target != "auto" and not swaps_by_cell:
                return SkillResult.failure(
                    before_wns,
                    "no swaps selected",
                    output_dcp,
                    details={
                        "attempts": [],
                        "candidates": [],
                        "budget_stopped": False,
                        "unstarted": [],
                    },
                )

            await mcp.call_rw(
                "initialize_rapidwright",
                {"jvm_max_memory": "8G"},
                timeout=120.0,
            )
            dcp_to_open = input_dcp or str(run_dir / "current_best.dcp")

            if target == "auto" and bool(params.get("sweep_candidates", True)):
                attempts = []
                feedback_attempts: list[dict] = []
                candidate_feedback: list[dict] = []
                candidate_outputs: list[SkillOutput] = []
                accepted = []
                accepted_swap_keys: set[tuple[str, str]] = set()
                current_dcp = str(dcp_to_open)
                current_wns = before_wns
                best_seen = None
                budget_stopped = False
                unstarted: list[str] = []
                stop_sweep = False

                for iteration in range(1, max_iterations + 1):
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": current_dcp},
                        timeout=600.0,
                    )

                    pins_file = run_dir / f"critical_path_pins_lut_pin_swap_iter{iteration}.json"
                    await mcp.call_vivado(
                        "extract_critical_path_pins",
                        {"num_paths": num_paths, "output_file": str(pins_file)},
                        timeout=600.0,
                    )
                    if not pins_file.exists():
                        return SkillResult.failure(
                            before_wns,
                            "extract_critical_path_pins produced no file",
                            output_dcp,
                            details={
                                "attempts": feedback_attempts,
                                "candidates": candidate_feedback,
                                "budget_stopped": budget_stopped,
                                "unstarted": unstarted,
                                "ancestry": "iterations after 1 descend from the prior local winner",
                            },
                        )

                    critical_paths = json.loads(pins_file.read_text())
                    target_pins = _pick_lut_input_pins_from_paths(
                        critical_paths,
                        max_pins=max_pins,
                        num_paths=num_paths,
                    )
                    if not target_pins:
                        if attempts:
                            break
                        return SkillResult.failure(
                            before_wns,
                            "no LUT input pins found on critical paths",
                            output_dcp,
                            details={
                                "attempts": feedback_attempts,
                                "candidates": candidate_feedback,
                                "budget_stopped": budget_stopped,
                                "unstarted": unstarted,
                                "ancestry": "iterations after 1 descend from the prior local winner",
                            },
                        )

                    prefilter_result = None
                    try:
                        await mcp.call_rw(
                            "read_checkpoint",
                            {"dcp_path": current_dcp},
                            timeout=600.0,
                        )
                        raw_prefilter = await mcp.call_rw(
                            "inspect_lut_pin_swap_candidates",
                            {"cell_pins": target_pins},
                            timeout=300.0,
                        )
                        prefilter_result = (
                            json.loads(raw_prefilter)
                            if isinstance(raw_prefilter, str)
                            else raw_prefilter
                        )
                    except Exception as e:
                        prefilter_result = {
                            "status": "error",
                            "error": str(e),
                        }

                    iteration_swaps = _build_prefiltered_iteration_swaps(
                        prefilter_result,
                        max_cells=max_cells,
                        preferred_pin=preferred_pin,
                        accepted_swap_keys=accepted_swap_keys,
                    )
                    if iteration_swaps is None:
                        attempts.append(
                            {
                                "iteration": iteration,
                                "prefilter_status": "fallback",
                                "prefilter_result": prefilter_result,
                            }
                        )
                        iteration_swaps = _build_raw_iteration_swaps(
                            target_pins,
                            max_cells=max_cells,
                            preferred_pin=preferred_pin,
                            accepted_swap_keys=accepted_swap_keys,
                        )
                    elif not iteration_swaps:
                        attempts.append(
                            {
                                "iteration": iteration,
                                "prefilter_status": "no_actionable_candidates",
                                "prefilter_result": prefilter_result,
                            }
                        )
                    if not iteration_swaps:
                        break

                    iteration_best = None
                    for idx, (cell_name, pin_swaps) in enumerate(iteration_swaps.items(), 1):
                        pin_identity = "-".join(str(item) for item in pin_swaps[0])
                        base_label = (
                            f"lut_pin_swap iteration={iteration:02d} "
                            f"cell={_safe_name(cell_name)} pin={_safe_name(pin_identity)}"
                        )[:120]
                        if candidate_budget_exhausted(
                            params, completed_attempts=len(feedback_attempts)
                        ):
                            budget_stopped = True
                            unstarted = [base_label, "later LUT swap variants"]
                            stop_sweep = True
                            break
                        cell_started = time.monotonic()
                        await mcp.call_rw(
                            "read_checkpoint",
                            {"dcp_path": current_dcp},
                            timeout=600.0,
                        )

                        raw = await mcp.call_rw(
                            "swap_lut_pins",
                            {
                                "cell_name": cell_name,
                                "pin_swaps": pin_swaps,
                                "unroute_affected": True,
                            },
                            timeout=300.0,
                        )
                        rw_result = json.loads(raw) if isinstance(raw, str) else raw
                        processed = 0
                        if rw_result.get("status") == "success":
                            processed = int(rw_result.get("processed_swaps", 0) or 0)

                        base_attempt = {
                            "iteration": iteration,
                            "cell": cell_name,
                            "pin_swaps": pin_swaps,
                            "processed_swaps": processed,
                            "rw_result": rw_result,
                        }
                        if processed == 0:
                            attempts.append(base_attempt)
                            feedback_attempts.append(
                                {
                                    "label": base_label,
                                    "status": "skipped",
                                    "local_metrics": {"processed_swaps": 0},
                                    "runtime_s": time.monotonic() - cell_started,
                                }
                            )
                            continue

                        pin_tag = "_".join(str(p) for p in pin_swaps[0])
                        identity_digest = hashlib.sha1(
                            f"{cell_name}|{pin_tag}".encode("utf-8")
                        ).hexdigest()[:10]
                        tag = (
                            f"iter{iteration:02d}_{idx:02d}_"
                            f"{_safe_name(cell_name)[:40]}_"
                            f"{_safe_name(pin_tag)}_{identity_digest}"
                        )
                        rw_dcp = run_dir / f"lut_pin_swap_{tag}_rw.dcp"

                        await mcp.call_rw(
                            "write_checkpoint",
                            {"dcp_path": str(rw_dcp), "overwrite": True},
                            timeout=600.0,
                        )
                        if not rw_dcp.exists():
                            failed = dict(base_attempt)
                            failed["error"] = "RapidWright DCP not created"
                            attempts.append(failed)
                            continue

                        for directive in route_directives:
                            label = (
                                f"lut_pin_swap iteration={iteration:02d} "
                                f"route={_safe_name(directive)} "
                                f"cell={_safe_name(cell_name)} "
                                f"pin={_safe_name(pin_identity)}"
                            )[:120]
                            if candidate_budget_exhausted(
                                params, completed_attempts=len(feedback_attempts)
                            ):
                                budget_stopped = True
                                unstarted = [label, "later LUT swap variants"]
                                stop_sweep = True
                                break
                            started = time.monotonic()
                            directive_tag = _safe_name(directive)
                            candidate_dcp = run_dir / f"lut_pin_swap_{tag}_{directive_tag}.dcp"
                            attempt = dict(base_attempt)
                            attempt["route_directive"] = directive
                            attempt["rw_dcp"] = str(rw_dcp)

                            try:
                                await open_rapidwright_dcp_in_vivado(
                                    mcp, rw_dcp, timeout=600.0
                                )
                                await mcp.call_vivado(
                                    "route_design",
                                    {"directive": directive},
                                    timeout=3600.0,
                                )

                                route_report = await mcp.call_vivado(
                                    "report_route_status", {}, timeout=120.0
                                )
                                route = parse_route_status_static(route_report)
                                is_legal = bool(route.get("routed_ok"))

                                after_wns = await mcp.get_wns()
                                if after_wns is None:
                                    ts = await mcp.call_vivado(
                                        "report_timing_summary", {}, timeout=300.0
                                    )
                                    parsed = parse_timing_summary_static(ts)
                                    after_wns = parsed.get("wns") or current_wns

                                await mcp.call_vivado(
                                    "write_checkpoint",
                                    {
                                        "dcp_path": str(candidate_dcp.resolve()),
                                        "force": True,
                                    },
                                    timeout=600.0,
                                )

                                attempt.update(
                                    {
                                        "after_wns": after_wns,
                                        "delta_wns": after_wns - current_wns,
                                        "total_delta_wns": after_wns - before_wns,
                                        "is_legal": is_legal,
                                        "route": route,
                                        "output_dcp": str(candidate_dcp),
                                    }
                                )
                            except Exception as exc:
                                attempt.update({"error": str(exc), "is_legal": False})
                            attempts.append(attempt)

                            feedback = {
                                "label": label,
                                "status": (
                                    "legal" if attempt.get("is_legal") else
                                    "error" if "error" in attempt else "illegal"
                                ),
                                "local_metrics": {
                                    "wns": attempt.get("after_wns"),
                                    "delta_wns": attempt.get("total_delta_wns"),
                                    "processed_swaps": processed,
                                },
                                "runtime_s": time.monotonic() - started,
                            }
                            feedback_attempts.append(feedback)

                            if iteration == 1 and attempt.get("is_legal"):
                                candidate_outputs.append(
                                    SkillOutput(candidate_dcp, label)
                                )
                                candidate_feedback.append(feedback)
                            if attempt.get("is_legal") and (
                                iteration_best is None
                                or attempt["after_wns"] > iteration_best["after_wns"]
                            ):
                                iteration_best = attempt
                            if attempt.get("is_legal") and (
                                best_seen is None
                                or attempt["after_wns"] > best_seen["after_wns"]
                            ):
                                best_seen = attempt

                        if stop_sweep:
                            break

                    if stop_sweep:
                        break
                    if iteration_best is None:
                        break

                    iteration_delta = iteration_best["after_wns"] - current_wns
                    if iteration_delta <= accept_min_delta:
                        break

                    accepted.append(iteration_best)
                    accepted_pin = iteration_best["pin_swaps"][0][0]
                    accepted_swap_keys.add((iteration_best["cell"], accepted_pin))
                    current_dcp = iteration_best["output_dcp"]
                    current_wns = iteration_best["after_wns"]

                if best_seen is None:
                    error = "RapidWright processed 0 legal LUT pin swap candidates"
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
                            "attempts": feedback_attempts,
                            "candidates": candidate_feedback,
                            "budget_stopped": budget_stopped,
                            "unstarted": unstarted,
                            "ancestry": "iterations after 1 descend from the prior local winner",
                        },
                    )

                final_attempt = accepted[-1] if accepted else best_seen
                after_wns = final_attempt["after_wns"]
                delta = after_wns - before_wns
                best_cell = str(final_attempt["cell"]).split("/")[-1]
                output_dcp = Path(final_attempt["output_dcp"])
                await mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(output_dcp.resolve())},
                    timeout=600.0,
                )
                fmax_b = calculate_fmax(before_wns, clock_period)
                fmax_a = calculate_fmax(after_wns, clock_period)
                prefix = "greedy" if accepted else "sweep"
                if fmax_b and fmax_a:
                    summary = (
                        f"lut_pin_swap {prefix} best={best_cell} "
                        f"iters={len(accepted)} wns {before_wns:.3f}->{after_wns:.3f} "
                        f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                    )
                else:
                    summary = (
                        f"lut_pin_swap {prefix} best={best_cell} "
                        f"iters={len(accepted)} wns {before_wns:.3f}->{after_wns:.3f} "
                        f"delta={delta:+.3f}"
                    )
                if budget_stopped:
                    summary += f" budget_stop {len(feedback_attempts)} attempts"

                output_cap = int(
                    params.get("_candidate_output_cap", len(candidate_outputs) or 1)
                )
                if output_cap < 1:
                    raise ValueError("_candidate_output_cap must be at least 1")
                candidate_count_before_cap = len(candidate_outputs)
                if candidate_count_before_cap > output_cap:
                    ranked_outputs = sorted(
                        enumerate(zip(candidate_outputs, candidate_feedback)),
                        key=lambda item: (
                            -float(
                                item[1][1].get("local_metrics", {}).get("wns")
                                if item[1][1].get("local_metrics", {}).get("wns")
                                is not None
                                else float("-inf")
                            ),
                            item[0],
                        ),
                    )[:output_cap]
                    candidate_outputs = [item[1][0] for item in ranked_outputs]
                    candidate_feedback = [item[1][1] for item in ranked_outputs]

                return SkillResult(
                    success=True,
                    before_wns=before_wns,
                    after_wns=after_wns,
                    delta_wns=delta,
                    is_legal=True,
                    output_dcp=output_dcp,
                    summary=summary,
                    details={
                        "attempts": feedback_attempts,
                        "candidates": candidate_feedback,
                        "candidate_output_count_before_cap": (
                            candidate_count_before_cap
                        ),
                        "candidate_output_cap": output_cap,
                        "budget_stopped": budget_stopped,
                        "unstarted": unstarted,
                        "ancestry": "iterations after 1 descend from the prior local winner",
                    },
                    candidates=tuple(candidate_outputs),
                )

            serial_started = time.monotonic()
            await mcp.call_rw(
                "read_checkpoint",
                {"dcp_path": dcp_to_open},
                timeout=600.0,
            )

            rw_results = []
            processed = 0
            for cell_name, pin_swaps in swaps_by_cell.items():
                raw = await mcp.call_rw(
                    "swap_lut_pins",
                    {
                        "cell_name": cell_name,
                        "pin_swaps": pin_swaps,
                        "unroute_affected": True,
                    },
                    timeout=300.0,
                )
                result = json.loads(raw) if isinstance(raw, str) else raw
                rw_results.append(result)
                if result.get("status") == "success":
                    processed += int(result.get("processed_swaps", 0) or 0)

            if processed == 0:
                return SkillResult.failure(
                    before_wns,
                    "RapidWright processed 0 LUT pin swaps: "
                    + json.dumps(rw_results)[:300],
                    output_dcp,
                    details={
                        "attempts": [{
                            "label": "lut_pin_swap explicit serial edit",
                            "status": "error",
                            "local_metrics": {"processed_swaps": 0},
                            "runtime_s": time.monotonic() - serial_started,
                        }],
                        "candidates": [],
                        "budget_stopped": False,
                        "unstarted": [],
                        "ancestry": "explicit multi-cell edits form one serial output",
                    },
                )

            rw_dcp = run_dir / "lut_pin_swap_rw.dcp"
            await mcp.call_rw(
                "write_checkpoint",
                {"dcp_path": str(rw_dcp), "overwrite": True},
                timeout=600.0,
            )
            if not rw_dcp.exists():
                return SkillResult.failure(
                    before_wns,
                    "RapidWright DCP not created",
                    output_dcp,
                    details={
                        "attempts": [{
                            "label": "lut_pin_swap explicit serial edit",
                            "status": "error",
                            "local_metrics": {"processed_swaps": processed},
                            "runtime_s": time.monotonic() - serial_started,
                        }],
                        "candidates": [],
                        "budget_stopped": False,
                        "unstarted": [],
                        "ancestry": "explicit multi-cell edits form one serial output",
                    },
                )

            await open_rapidwright_dcp_in_vivado(mcp, rw_dcp, timeout=600.0)
            await mcp.call_vivado(
                "route_design",
                {"directive": route_directive},
                timeout=3600.0,
            )

            route_report = await mcp.call_vivado(
                "report_route_status", {}, timeout=120.0
            )
            route = parse_route_status_static(route_report)
            is_legal = bool(route.get("routed_ok"))

            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado(
                    "report_timing_summary", {}, timeout=300.0
                )
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            delta = after_wns - before_wns
            await mcp.call_vivado(
                "write_checkpoint",
                {"dcp_path": str(output_dcp.resolve()), "force": True},
                timeout=600.0,
            )

            first_cells = ",".join(c.split("/")[-1] for c in list(swaps_by_cell)[:3])
            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"lut_pin_swap cells=[{first_cells}] swaps={processed} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"lut_pin_swap cells=[{first_cells}] swaps={processed} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} delta={delta:+.3f}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=is_legal,
                output_dcp=output_dcp,
                summary=summary,
                details={
                    "attempts": [{
                        "label": (
                            "lut_pin_swap explicit "
                            f"cells={len(swaps_by_cell)} "
                            f"route={_safe_name(str(route_directive))}"
                        )[:120],
                        "status": "legal" if is_legal else "illegal",
                        "local_metrics": {
                            "wns": after_wns,
                            "delta_wns": delta,
                            "processed_swaps": processed,
                        },
                        "runtime_s": time.monotonic() - serial_started,
                    }],
                    "candidates": [],
                    "budget_stopped": False,
                    "unstarted": [],
                    "ancestry": "explicit multi-cell edits form one serial output",
                },
            )

        except Exception as e:
            logger.error("[lut_pin_swap] failed: %s", e)
            return SkillResult.failure(
                before_wns,
                str(e),
                output_dcp,
                details={
                    "attempts": [],
                    "candidates": [],
                    "budget_stopped": False,
                    "unstarted": [],
                },
            )
