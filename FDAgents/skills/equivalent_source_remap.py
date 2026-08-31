"""Exact-source-proof-gated sink remap with independent routed siblings."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..artifacts import sha256_file
from .base import (
    SkillOutput,
    SkillResult,
    candidate_budget_exhausted,
    open_rapidwright_dcp_in_vivado,
    parse_route_status_static,
)

logger = logging.getLogger(__name__)

PROOF_VERSION = "equivalent-source-v1"
MAX_CANDIDATES = 8
MAX_CRITICAL_PATHS = 16
MAX_MIN_DISTANCE = 256
AUTO_TARGET = "auto_nearest_proven"


def _payload(raw: Any) -> dict:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("MCP tool returned a non-object response")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def _critical_paths(raw: Any) -> list[list[str]]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        raise ValueError("critical pin report did not return an inline path list")
    paths = []
    for path in value[:MAX_CRITICAL_PATHS]:
        if not isinstance(path, list):
            continue
        pins = [pin for pin in path[:64] if isinstance(pin, str) and pin]
        if pins:
            paths.append(pins)
    return paths


def _tcl_quote(value: str) -> str:
    return "{" + str(value).replace("\\", "\\\\").replace("}", "\\}") + "}"


def _route_affected_tcl(net_names: list[str]) -> str:
    if not net_names or len(net_names) > 2:
        raise ValueError("mutation must identify one or two bounded affected nets")
    encoded = " ".join(_tcl_quote(name) for name in net_names)
    return "; ".join([
        f"set fdagents_eq_net_names [list {encoded}]",
        "set fdagents_eq_nets [get_nets -quiet $fdagents_eq_net_names]",
        "if {[llength $fdagents_eq_nets] != [llength $fdagents_eq_net_names]} { error {FDAGENTS equivalent-source affected net missing} }",
        "route_design -preserve -nets $fdagents_eq_nets -directive Default",
    ])


def _candidate_path(run_dir: Path, ordinal: int, certificate_id: str) -> Path:
    return run_dir / f"equivalent_source_{ordinal:02d}_{certificate_id[:12]}.dcp"


class EquivalentSourceRemapSkill:
    name = "equivalent_source_remap"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        started = time.monotonic()
        run_dir = mcp.run_dir
        failed_dcp = run_dir / "equivalent_source_remap_failed.dcp"
        input_dcp = Path(str(params.get("input_dcp") or run_dir / "current_best.dcp"))
        source_sha256 = sha256_file(input_dcp)
        max_candidates = int(params.get("max_candidates", MAX_CANDIDATES))
        min_distance = int(params.get("min_distance", 1))
        num_paths = int(params.get("num_paths", 8))
        contest_clock = str(params.get("_contest_clock", "clk_fpl26contest"))
        attempts = []
        outputs = []

        try:
            if target != AUTO_TARGET and not re.fullmatch(r"[0-9a-f]{64}", target):
                raise ValueError("target must be auto_nearest_proven or a certificate_id")
            if not 1 <= max_candidates <= MAX_CANDIDATES:
                raise ValueError(f"max_candidates must be 1..{MAX_CANDIDATES}")
            if not 1 <= min_distance <= MAX_MIN_DISTANCE:
                raise ValueError(f"min_distance must be 1..{MAX_MIN_DISTANCE}")
            if not 1 <= num_paths <= MAX_CRITICAL_PATHS:
                raise ValueError(f"num_paths must be 1..{MAX_CRITICAL_PATHS}")

            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_vivado(
                "open_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            raw_paths = await mcp.call_vivado(
                "extract_critical_path_pins",
                {"num_paths": num_paths, "clock": contest_clock},
                timeout=600.0,
            )
            paths = _critical_paths(raw_paths)
            if not paths:
                raise ValueError("contest-clock critical pin report returned no paths")

            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            initial = _payload(await mcp.call_rw(
                "find_proven_equivalent_sources",
                {
                    "critical_paths_data": paths,
                    "max_candidates": max_candidates,
                    "min_distance": min_distance,
                },
                timeout=600.0,
            ))
            if initial.get("design_sha256") != source_sha256:
                raise RuntimeError("proof analysis design hash does not match exact seed")
            certificates = list(initial.get("certificates") or [])
            if target == AUTO_TARGET:
                selected_ids = [item["certificate_id"] for item in certificates]
            else:
                selected_ids = [target] if any(
                    item.get("certificate_id") == target for item in certificates
                ) else []
            if not selected_ids:
                raise ValueError("no requested exact-seed proven equivalent source candidate")

            for certificate_id in selected_ids[:max_candidates]:
                if candidate_budget_exhausted(params, completed_attempts=len(outputs)):
                    break
                attempt = {"certificate_id": certificate_id}
                try:
                    # A certificate never crosses sibling state. Reload and regenerate
                    # it from the immutable seed immediately before mutation.
                    await mcp.call_rw(
                        "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
                    )
                    fresh = _payload(await mcp.call_rw(
                        "find_proven_equivalent_sources",
                        {
                            "critical_paths_data": paths,
                            "max_candidates": max_candidates,
                            "min_distance": min_distance,
                        },
                        timeout=600.0,
                    ))
                    certificate = next(
                        (
                            item for item in fresh.get("certificates") or []
                            if item.get("certificate_id") == certificate_id
                        ),
                        None,
                    )
                    if certificate is None:
                        raise RuntimeError(
                            "certificate is absent from fresh exact-seed analysis"
                        )
                    if certificate.get("design_sha256") != source_sha256:
                        raise RuntimeError("fresh certificate source identity mismatch")
                    if not certificate.get("proof_facts", {}).get(
                        "mutation_preconditions", {}
                    ).get("mechanically_unfolded"):
                        raise RuntimeError(
                            "proof succeeded but ECOTools unfolded-netlist precondition failed"
                        )
                    mutation = _payload(await mcp.call_rw(
                        "rewire_sink_to_proven_source",
                        {"certificate": certificate},
                        timeout=600.0,
                    ))
                    if mutation.get("fresh_reproof") is not True:
                        raise RuntimeError("mutation did not attest fresh reproof")
                    route_nets = list(mutation.get("nets_requiring_route") or [])
                    if not route_nets or len(route_nets) > 2:
                        raise RuntimeError("mutation affected-net manifest is invalid")
                    rw_dcp = run_dir / f"equivalent_source_{certificate_id[:12]}_rw.dcp"
                    _payload(await mcp.call_rw(
                        "write_checkpoint",
                        {"dcp_path": str(rw_dcp), "overwrite": True},
                        timeout=600.0,
                    ))
                    if not rw_dcp.is_file():
                        raise RuntimeError("RapidWright scratch checkpoint was not created")
                    await open_rapidwright_dcp_in_vivado(mcp, rw_dcp, timeout=600.0)
                    await mcp.call_vivado(
                        "run_tcl",
                        {"command": _route_affected_tcl(route_nets)},
                        timeout=float(params.get("route_timeout_s", 1200.0)),
                    )
                    route = parse_route_status_static(await mcp.call_vivado(
                        "report_route_status", {}, timeout=180.0
                    ))
                    if not route.get("routed_ok"):
                        raise RuntimeError("affected-net route did not produce a legal design")
                    output = _candidate_path(run_dir, len(outputs) + 1, certificate_id)
                    await mcp.call_vivado(
                        "write_checkpoint",
                        {"dcp_path": str(output.resolve()), "force": True},
                        timeout=600.0,
                    )
                    if not output.is_file():
                        raise RuntimeError("Vivado sibling checkpoint was not created")
                    artifact_sha256 = sha256_file(output)
                    proof = {
                        "proof_version": PROOF_VERSION,
                        "skill": self.name,
                        "source_artifact_sha256": source_sha256,
                        "artifact_sha256": artifact_sha256,
                        "exact_source_equivalence": certificate,
                    }
                    evidence = {
                        "version": 1,
                        "certificate_id": certificate_id,
                        "distance_improvement_tiles": certificate[
                            "distance_improvement_tiles"
                        ],
                        "fresh_reproof": True,
                        "selective_unroute": True,
                        "affected_net_count": len(mutation.get("affected_nets") or []),
                        "routed_net_count": len(route_nets),
                        "acceptance_authority": "Vivado central measurement only",
                    }
                    label = (
                        "equivalent_source_remap "
                        f"distance_gain={certificate['distance_improvement_tiles']}tiles "
                        f"cert={certificate_id[:12]}"
                    )[:120]
                    outputs.append(SkillOutput(output, label, evidence, proof))
                    attempt.update({"status": "legal", **evidence})
                except Exception as exc:  # noqa: BLE001
                    attempt.update({"status": "rejected", "reason": str(exc)[:500]})
                attempts.append(attempt)

            if not outputs:
                return SkillResult.failure(
                    before_wns,
                    "no proof-gated equivalent-source sibling was produced",
                    failed_dcp,
                    details={
                        "proof_version": PROOF_VERSION,
                        "attempts": attempts,
                        "initial_fail_reasons": initial.get("fail_reasons", [])[:64],
                        "acceptance_authority": "Vivado central measurement only",
                    },
                )
            summary = f"equivalent_source_remap candidates={len(outputs)} exact_proof={PROOF_VERSION}"
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=outputs[0].output_dcp,
                summary=summary,
                details={
                    "proof_version": PROOF_VERSION,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "acceptance_authority": "Vivado central measurement only",
                    "eco_proof_does_not_grant_functional_equivalence": True,
                },
                candidates=tuple(outputs),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[equivalent_source_remap] failed: %s", exc)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "acceptance_authority": "Vivado central measurement only",
                },
            )
