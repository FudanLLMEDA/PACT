"""Mechanically derive proof-gated DSP input-register absorption candidates.

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
    RegisterAbsorbPlan,
    RegisterAbsorbSpec,
    _digest,
    _stable,
    build_register_absorb_authorization_from_facts,
    register_absorb_spec_from_extracted_facts,
    verify_rewrite_authorization,
)


PLACEMENT_MANIFEST_VERSION = 1
TOPOLOGY_MANIFEST_VERSION = 1
_DSP_SITE_RE = re.compile(r"DSP48E2_X\d+Y\d+")
_SIDE_TARGET_REGS = {"a": "AREG", "b": "BREG"}
_BOUNDARY_FIELDS = {
    "ff_bus", "driver_nets_complete", "dsp_cell", "dsp_site",
    "dsp_side", "clock_pin", "ce_pin", "reset_pin",
}


def derive_register_absorb_plan(spec: RegisterAbsorbSpec) -> RegisterAbsorbPlan:
    """Derive the single mechanical absorption plan implied by a recovered spec."""
    if not isinstance(spec, RegisterAbsorbSpec):
        raise OperatorProofError(
            "register-absorb plan derivation requires a recovered absorb spec"
        )
    target_reg = _SIDE_TARGET_REGS.get(spec.dsp_side)
    if target_reg is None:
        raise OperatorProofError('recovered dsp_side must be "a" or "b"')
    return RegisterAbsorbPlan(
        template="DSP48E2_INPUT_REG_ABSORB_V1",
        dsp_side=spec.dsp_side,
        target_reg=target_reg,
        target_value=1,
        ce_mapped=True,
        reset_mapped=spec.reset_kind == "sync_reset_zero",
    )


def build_register_absorb_action_plans(
    facts: dict[str, Any],
    *,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Build the single proof-gated plan; no plan grants emitted-DCP authority."""
    spec = register_absorb_spec_from_extracted_facts(facts)
    plan = derive_register_absorb_plan(spec)
    authorization = build_register_absorb_authorization_from_facts(
        facts, plan, timeout_ms=timeout_ms
    )
    if (
        not isinstance(authorization, dict)
        or not isinstance(authorization.get("certificate_id"), str)
    ):
        raise OperatorProofError("register-absorb authorization was not produced")
    return [{
        "plan": plan,
        "authorization": authorization,
        "derived_resources": {"dsp48e2_sites": 0},
        "authority": "pre_mutation_only",
    }]


def _candidate_plan(candidate: dict[str, Any]) -> RegisterAbsorbPlan:
    if (
        not isinstance(candidate, dict)
        or candidate.get("authority") != "pre_mutation_only"
    ):
        raise OperatorProofError(
            "candidate is not a pre-mutation register-absorb action plan"
        )
    raw = candidate.get("plan")
    if isinstance(raw, RegisterAbsorbPlan):
        return raw
    if not isinstance(raw, dict):
        raise OperatorProofError("candidate plan is missing")
    try:
        return RegisterAbsorbPlan(**raw)
    except (TypeError, ValueError) as exc:
        raise OperatorProofError("candidate plan has an invalid schema") from exc


def _verify_candidate(
    facts: dict[str, Any], candidate: dict[str, Any]
) -> RegisterAbsorbPlan:
    plan = _candidate_plan(candidate)
    authorization = candidate.get("authorization")
    if not isinstance(authorization, dict) or not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(facts.get("design_sha256") or ""),
        expected_region_sha256=str(facts.get("region_sha256") or ""),
    ):
        raise OperatorProofError("register-absorb authorization replay failed")
    if _stable(authorization.get("plan")) != _stable(asdict(plan)):
        raise OperatorProofError("candidate plan differs from its authorization")
    return plan


def _validated_boundary(
    facts: dict[str, Any], spec: RegisterAbsorbSpec
) -> dict[str, Any]:
    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != _BOUNDARY_FIELDS:
        raise OperatorProofError(
            "register-absorb boundary facts have an unsupported schema"
        )
    ff_bus = boundary.get("ff_bus")
    if (
        not isinstance(ff_bus, list)
        or len(ff_bus) != spec.bus_width
        or any(
            not isinstance(item, dict)
            or set(item) != {"cell", "pin"}
            or not isinstance(item.get("cell"), str)
            or not item["cell"]
            or not isinstance(item.get("pin"), str)
            or not item["pin"]
            for item in ff_bus
        )
    ):
        raise OperatorProofError(
            "register-absorb ff_bus is not a dense fully-named bus"
        )
    if boundary.get("driver_nets_complete") is not True:
        raise OperatorProofError(
            "register-absorb driver net extraction is incomplete"
        )
    dsp_cell = boundary.get("dsp_cell")
    if not isinstance(dsp_cell, str) or not dsp_cell:
        raise OperatorProofError("register-absorb dsp_cell is invalid")
    dsp_site = boundary.get("dsp_site")
    if not isinstance(dsp_site, str) or _DSP_SITE_RE.fullmatch(dsp_site) is None:
        raise OperatorProofError(
            "register-absorb dsp_site is not a DSP48E2 site"
        )
    if boundary.get("dsp_side") != spec.dsp_side:
        raise OperatorProofError(
            "register-absorb boundary side differs from the recovered spec"
        )
    for field in ("clock_pin", "ce_pin"):
        value = boundary.get(field)
        if not isinstance(value, str) or not value:
            raise OperatorProofError(f"register-absorb {field} is invalid")
    reset_pin = boundary.get("reset_pin")
    if reset_pin is not None and (
        not isinstance(reset_pin, str) or not reset_pin
    ):
        raise OperatorProofError("register-absorb reset_pin is invalid")
    if (reset_pin is not None) != (spec.reset_kind == "sync_reset_zero"):
        raise OperatorProofError(
            "register-absorb reset_pin disagrees with the recovered reset kind"
        )
    return boundary


def build_register_absorb_placement_manifest(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
) -> dict[str, Any]:
    """Record the existing DSP site under a replayed authorization; no oracle."""
    spec = register_absorb_spec_from_extracted_facts(facts)
    _verify_candidate(facts, action_candidate)
    boundary = _validated_boundary(facts, spec)
    manifest = {
        "kind": "register_absorb_placement_manifest",
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


def build_register_absorb_topology_manifest(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Describe the exact intended absorption while withholding mutation authority."""
    spec = register_absorb_spec_from_extracted_facts(facts)
    plan = _verify_candidate(facts, action_candidate)
    boundary = _validated_boundary(facts, spec)
    claimed = placement.get("manifest_sha256") if isinstance(placement, dict) else None
    payload = dict(placement) if isinstance(placement, dict) else {}
    payload.pop("manifest_sha256", None)
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "register_absorb_placement_manifest"
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
            "register-absorb placement manifest is stale, modified, or cross-plan"
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
            "register-absorb placement does not record the recovered DSP site"
        )
    manifest = {
        "kind": "register_absorb_topology_manifest",
        "version": TOPOLOGY_MANIFEST_VERSION,
        "source": dict(placement["source"]),
        "authorization_id": placement["authorization_id"],
        "dsp": {
            "site": placements[0]["site"],
            "target_reg": plan.target_reg,
            "target_value": 1,
            "ce_mapped": True,
            "reset_mapped": plan.reset_mapped,
        },
        "bus_width": spec.bus_width,
        "dsp_side": spec.dsp_side,
        "emission_eligible": True,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest
