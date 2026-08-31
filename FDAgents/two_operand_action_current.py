"""Mechanically derive proof-gated two-operand DSP rebuild candidates."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .operator_proof_current import (
    OperatorProofError,
    TwoOperandDspPlan,
    TwoOperandProductSpec,
    _digest,
    _stable,
    build_two_operand_authorization_from_facts,
    two_operand_spec_from_extracted_facts,
    verify_rewrite_authorization,
)


PLACEMENT_MANIFEST_VERSION = 1
TOPOLOGY_MANIFEST_VERSION = 1
_DSP_SITE_RE = re.compile(r"DSP48E2_X\d+Y\d+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ORACLE_SLOT_FIELDS = {
    "site", "bel", "source_dcp_sha256", "region_sha256",
    "clock_route_ok", "distance",
}


def derive_two_operand_plan(spec: TwoOperandProductSpec) -> TwoOperandDspPlan:
    """Derive the single mechanical DSP plan implied by a recovered spec."""
    if not isinstance(spec, TwoOperandProductSpec):
        raise OperatorProofError(
            "two-operand plan derivation requires a recovered product spec"
        )
    return TwoOperandDspPlan(
        template="DSP48E2_MUL_TWO_OPERAND_V1",
        a_signed=spec.a_signed,
        b_signed=spec.b_signed,
        output_lsb=spec.shift,
        output_width=spec.term_width,
        output_signed=spec.term_signed,
        accumulator_width=spec.accumulator_width,
        product_latency=spec.product_latency,
    )


def build_two_operand_action_plans(
    facts: dict[str, Any],
    *,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Build the single proof-gated plan; no plan grants emitted-DCP authority."""
    spec = two_operand_spec_from_extracted_facts(facts)
    plan = derive_two_operand_plan(spec)
    authorization = build_two_operand_authorization_from_facts(
        facts, plan, timeout_ms=timeout_ms
    )
    if (
        not isinstance(authorization, dict)
        or not isinstance(authorization.get("certificate_id"), str)
    ):
        raise OperatorProofError("two-operand authorization was not produced")
    return [{
        "plan": plan,
        "authorization": authorization,
        "derived_resources": {"dsp48e2_sites": 1},
        "authority": "pre_mutation_only",
    }]


def _candidate_plan(candidate: dict[str, Any]) -> TwoOperandDspPlan:
    if (
        not isinstance(candidate, dict)
        or candidate.get("authority") != "pre_mutation_only"
    ):
        raise OperatorProofError(
            "candidate is not a pre-mutation two-operand action plan"
        )
    raw = candidate.get("plan")
    if isinstance(raw, TwoOperandDspPlan):
        return raw
    if not isinstance(raw, dict):
        raise OperatorProofError("candidate plan is missing")
    try:
        return TwoOperandDspPlan(**raw)
    except (TypeError, ValueError) as exc:
        raise OperatorProofError("candidate plan has an invalid schema") from exc


def _verify_candidate(
    facts: dict[str, Any], candidate: dict[str, Any]
) -> TwoOperandDspPlan:
    plan = _candidate_plan(candidate)
    authorization = candidate.get("authorization")
    if not isinstance(authorization, dict) or not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(facts.get("design_sha256") or ""),
        expected_region_sha256=str(facts.get("region_sha256") or ""),
    ):
        raise OperatorProofError("two-operand authorization replay failed")
    if _stable(authorization.get("plan")) != _stable(asdict(plan)):
        raise OperatorProofError("candidate plan differs from its authorization")
    return plan


def _validated_oracle_slots(
    records: list[dict[str, Any]],
    *,
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not records:
        raise OperatorProofError(
            "two-operand oracle slots must be a nonempty list"
        )
    result = []
    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _ORACLE_SLOT_FIELDS:
            raise OperatorProofError(
                f"two-operand oracle slot [{index}] has an unsupported schema"
            )
        site = record.get("site")
        if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
            raise OperatorProofError(
                f"two-operand oracle slot [{index}] site is not a DSP48E2 site"
            )
        if record.get("bel") != "DSP48E2":
            raise OperatorProofError(
                f"two-operand oracle slot [{index}] BEL is not DSP48E2"
            )
        for field in ("source_dcp_sha256", "region_sha256"):
            value = record.get(field)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise OperatorProofError(
                    f"two-operand oracle slot [{index}] {field} is invalid"
                )
        if record["source_dcp_sha256"] != facts.get("design_sha256") or record[
            "region_sha256"
        ] != facts.get("region_sha256"):
            raise OperatorProofError(
                f"two-operand oracle slot [{index}] is stale or cross-seed"
            )
        if record.get("clock_route_ok") is not True:
            raise OperatorProofError(
                f"two-operand oracle slot [{index}] clock route is not measured"
            )
        distance = record.get("distance")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, int)
            or distance < 0
        ):
            raise OperatorProofError(
                f"two-operand oracle slot [{index}] distance is invalid"
            )
        key = (site, record["bel"])
        if key in seen:
            raise OperatorProofError(
                "two-operand oracle slots contain a duplicate site/BEL"
            )
        seen.add(key)
        result.append(dict(record))
    return result


def allocate_two_operand_dsp_placement(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
    *,
    oracle_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allocate the closest measured DSP48E2 slot under a replayed authorization."""
    two_operand_spec_from_extracted_facts(facts)
    _verify_candidate(facts, action_candidate)
    slots = _validated_oracle_slots(oracle_slots, facts=facts)
    selected = min(slots, key=lambda slot: (slot["distance"], slot["site"]))
    manifest = {
        "kind": "two_operand_placement_manifest",
        "version": PLACEMENT_MANIFEST_VERSION,
        "source": {
            "dcp_sha256": facts["design_sha256"],
            "region_sha256": facts["region_sha256"],
        },
        "candidate_id": facts["candidate_id"],
        "authorization_id": action_candidate["authorization"]["certificate_id"],
        "placements": [{"site": selected["site"], "bel": selected["bel"]}],
        "authority": "pre_emission_only",
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def build_two_operand_topology_manifest(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Describe the exact intended DSP graph while withholding mutation authority."""
    spec = two_operand_spec_from_extracted_facts(facts)
    plan = _verify_candidate(facts, action_candidate)
    claimed = placement.get("manifest_sha256") if isinstance(placement, dict) else None
    payload = dict(placement) if isinstance(placement, dict) else {}
    payload.pop("manifest_sha256", None)
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "two_operand_placement_manifest"
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
            "two-operand placement manifest is stale, modified, or cross-plan"
        )
    placements = placement.get("placements")
    if (
        not isinstance(placements, list)
        or len(placements) != 1
        or not isinstance(placements[0], dict)
        or set(placements[0]) != {"site", "bel"}
        or placements[0].get("bel") != "DSP48E2"
        or not isinstance(placements[0].get("site"), str)
        or _DSP_SITE_RE.fullmatch(placements[0]["site"]) is None
    ):
        raise OperatorProofError(
            "two-operand placement does not describe exactly one DSP48E2 site"
        )
    manifest = {
        "kind": "two_operand_topology_manifest",
        "version": TOPOLOGY_MANIFEST_VERSION,
        "source": dict(placement["source"]),
        "authorization_id": placement["authorization_id"],
        "dsp": {
            "site": placements[0]["site"],
            "opmode_contract": {
                "multiply": True,
                "accumulate": spec.accumulator_width > 0,
                "output_lsb": plan.output_lsb,
                "output_width": plan.output_width,
            },
            "a_width": spec.a_width,
            "b_width": spec.b_width,
            "product_latency": spec.product_latency,
        },
        "emission_eligible": True,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest
