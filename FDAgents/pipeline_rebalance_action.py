"""Mechanically derive proof-gated DSP48E2 MREG pipeline-rebalance candidates.

The target DSP48E2 is already placed; there is no placement oracle. The
"placement" manifest records the existing site recovered in the hash-bound
boundary facts, and every manifest replays the pre-mutation authorization.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .operator_proof_current import (
    OperatorProofError,
    PipelineRebalancePlan,
    PipelineRebalanceSpec,
    _PIPELINE_REBALANCE_SET_REGISTERS,
    _digest,
    _stable,
    build_pipeline_rebalance_authorization_from_facts,
    pipeline_rebalance_spec_from_extracted_facts,
    verify_rewrite_authorization,
)


PLACEMENT_MANIFEST_VERSION = 1
TOPOLOGY_MANIFEST_VERSION = 1
_DSP_SITE_RE = re.compile(r"DSP48E2_X\d+Y\d+")
_DIRECTION_ACTIVE_CE_PINS = {"forward": "CEA2", "backward": "CEM"}
_BOUNDARY_FIELDS = {
    "dsp_cell", "dsp_site", "clock_pin", "ce_pin", "reset_pin",
    "a_width", "b_width",
}


def derive_pipeline_rebalance_plan(
    spec: PipelineRebalanceSpec,
) -> PipelineRebalancePlan:
    """Derive the single mechanical rebalance plan implied by a recovered spec."""
    if not isinstance(spec, PipelineRebalanceSpec):
        raise OperatorProofError(
            "pipeline-rebalance plan derivation requires a recovered"
            " rebalance spec"
        )
    set_registers = _PIPELINE_REBALANCE_SET_REGISTERS.get(spec.direction)
    if set_registers is None:
        raise OperatorProofError(
            'recovered direction must be "forward" or "backward"'
        )
    return PipelineRebalancePlan(
        template="DSP48E2_MREG_REBALANCE_V1",
        direction=spec.direction,
        set_registers=set_registers,
        ce_mapped=True,
        reset_mapped=spec.reset_kind == "sync_reset_zero",
    )


def build_pipeline_rebalance_action_plans(
    facts: dict[str, Any],
    *,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Build the single proof-gated plan; no plan grants emitted-DCP authority."""
    spec = pipeline_rebalance_spec_from_extracted_facts(facts)
    plan = derive_pipeline_rebalance_plan(spec)
    authorization = build_pipeline_rebalance_authorization_from_facts(
        facts, plan, timeout_ms=timeout_ms
    )
    if (
        not isinstance(authorization, dict)
        or not isinstance(authorization.get("certificate_id"), str)
    ):
        raise OperatorProofError(
            "pipeline-rebalance authorization was not produced"
        )
    return [{
        "plan": plan,
        "authorization": authorization,
        "derived_resources": {"dsp48e2_sites": 0},
        "authority": "pre_mutation_only",
    }]


def _normalized_set_registers(raw: Any) -> tuple:
    if not isinstance(raw, (list, tuple)):
        raise OperatorProofError(
            "candidate plan set_registers must be a sequence of register moves"
        )
    moves = []
    for move in raw:
        if not isinstance(move, (list, tuple)) or len(move) != 2:
            raise OperatorProofError(
                "candidate plan set_registers entries must be 2-item moves"
            )
        moves.append((move[0], move[1]))
    return tuple(moves)


def _candidate_plan(candidate: dict[str, Any]) -> PipelineRebalancePlan:
    if (
        not isinstance(candidate, dict)
        or candidate.get("authority") != "pre_mutation_only"
    ):
        raise OperatorProofError(
            "candidate is not a pre-mutation pipeline-rebalance action plan"
        )
    raw = candidate.get("plan")
    if isinstance(raw, PipelineRebalancePlan):
        return raw
    if not isinstance(raw, dict):
        raise OperatorProofError("candidate plan is missing")
    try:
        raw = dict(raw)
        raw["set_registers"] = _normalized_set_registers(
            raw.get("set_registers")
        )
        return PipelineRebalancePlan(**raw)
    except (TypeError, ValueError) as exc:
        raise OperatorProofError("candidate plan has an invalid schema") from exc


def _verify_candidate(
    facts: dict[str, Any], candidate: dict[str, Any]
) -> PipelineRebalancePlan:
    plan = _candidate_plan(candidate)
    authorization = candidate.get("authorization")
    if not isinstance(authorization, dict) or not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(facts.get("design_sha256") or ""),
        expected_region_sha256=str(facts.get("region_sha256") or ""),
    ):
        raise OperatorProofError(
            "pipeline-rebalance authorization replay failed"
        )
    if _stable(authorization.get("plan")) != _stable(asdict(plan)):
        raise OperatorProofError("candidate plan differs from its authorization")
    return plan


def _validated_boundary(
    facts: dict[str, Any], spec: PipelineRebalanceSpec
) -> dict[str, Any]:
    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != _BOUNDARY_FIELDS:
        raise OperatorProofError(
            "pipeline-rebalance boundary facts have an unsupported schema"
        )
    dsp_cell = boundary.get("dsp_cell")
    if not isinstance(dsp_cell, str) or not dsp_cell:
        raise OperatorProofError("pipeline-rebalance dsp_cell is invalid")
    dsp_site = boundary.get("dsp_site")
    if not isinstance(dsp_site, str) or _DSP_SITE_RE.fullmatch(dsp_site) is None:
        raise OperatorProofError(
            "pipeline-rebalance dsp_site is not a DSP48E2 site"
        )
    if boundary.get("clock_pin") != f"{dsp_cell}/CLK":
        raise OperatorProofError(
            "pipeline-rebalance clock_pin is not the DSP CLK pin"
        )
    active_ce = _DIRECTION_ACTIVE_CE_PINS.get(spec.direction)
    if active_ce is None or boundary.get("ce_pin") != f"{dsp_cell}/{active_ce}":
        raise OperatorProofError(
            "pipeline-rebalance ce_pin is not the active CE source pin"
            " for the recovered direction"
        )
    for field, width in (("a_width", spec.a_width), ("b_width", spec.b_width)):
        value = boundary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value != width:
            raise OperatorProofError(
                f"pipeline-rebalance boundary {field} differs from the"
                " recovered spec"
            )
    reset_pin = boundary.get("reset_pin")
    if reset_pin is not None and (
        not isinstance(reset_pin, str) or not reset_pin
    ):
        raise OperatorProofError("pipeline-rebalance reset_pin is invalid")
    if (reset_pin is not None) != (spec.reset_kind == "sync_reset_zero"):
        raise OperatorProofError(
            "pipeline-rebalance reset_pin disagrees with the recovered"
            " reset kind"
        )
    return boundary


def build_pipeline_rebalance_placement_manifest(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
) -> dict[str, Any]:
    """Record the existing DSP site under a replayed authorization; no oracle."""
    spec = pipeline_rebalance_spec_from_extracted_facts(facts)
    _verify_candidate(facts, action_candidate)
    boundary = _validated_boundary(facts, spec)
    manifest = {
        "kind": "pipeline_rebalance_placement_manifest",
        "version": PLACEMENT_MANIFEST_VERSION,
        "source": {
            "dcp_sha256": facts["design_sha256"],
            "region_sha256": facts["region_sha256"],
        },
        "candidate_id": facts["candidate_id"],
        "authorization_id": action_candidate["authorization"]["certificate_id"],
        "placements": [{"site": boundary["dsp_site"], "bel": "DSP48E2"}],
        "authority": "pre_emission_only",
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def build_pipeline_rebalance_topology_manifest(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Describe the exact intended rebalance while withholding mutation authority."""
    spec = pipeline_rebalance_spec_from_extracted_facts(facts)
    plan = _verify_candidate(facts, action_candidate)
    boundary = _validated_boundary(facts, spec)
    claimed = placement.get("manifest_sha256") if isinstance(placement, dict) else None
    payload = dict(placement) if isinstance(placement, dict) else {}
    payload.pop("manifest_sha256", None)
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "pipeline_rebalance_placement_manifest"
        or placement.get("version") != PLACEMENT_MANIFEST_VERSION
        or placement.get("authority") != "pre_emission_only"
        or placement.get("source") != {
            "dcp_sha256": facts.get("design_sha256"),
            "region_sha256": facts.get("region_sha256"),
        }
        or placement.get("candidate_id") != facts.get("candidate_id")
        or placement.get("authorization_id")
        != action_candidate["authorization"].get("certificate_id")
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
    ):
        raise OperatorProofError(
            "pipeline-rebalance placement manifest is stale, modified,"
            " or cross-plan"
        )
    placements = placement.get("placements")
    if (
        not isinstance(placements, list)
        or len(placements) != 1
        or not isinstance(placements[0], dict)
        or set(placements[0]) != {"site", "bel"}
        or placements[0].get("bel") != "DSP48E2"
        or placements[0].get("site") != boundary["dsp_site"]
    ):
        raise OperatorProofError(
            "pipeline-rebalance placement does not record the recovered DSP site"
        )
    manifest = {
        "kind": "pipeline_rebalance_topology_manifest",
        "version": TOPOLOGY_MANIFEST_VERSION,
        "source": dict(placement["source"]),
        "authorization_id": placement["authorization_id"],
        "dsp": {
            "site": placements[0]["site"],
            "direction": plan.direction,
            "set_registers": [
                [name, value] for name, value in plan.set_registers
            ],
            "ce_mapped": True,
            "reset_mapped": plan.reset_mapped,
        },
        "a_width": spec.a_width,
        "b_width": spec.b_width,
        "emission_eligible": True,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest
