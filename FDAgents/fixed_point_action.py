"""Mechanically derive proof-gated fixed-point DSP rebuild candidates."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .operator_proof_current import (
    FixedPointDspPlan,
    FixedPointProductSpec,
    OperatorProofError,
    _digest,
    _stable,
    build_fixed_point_authorization_from_facts,
    fixed_point_spec_from_extracted_facts,
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


def derive_fixed_point_plan(spec: FixedPointProductSpec) -> FixedPointDspPlan:
    """Derive the single mechanical DSP plan implied by a recovered spec."""
    if not isinstance(spec, FixedPointProductSpec):
        raise OperatorProofError(
            "fixed-point plan derivation requires a recovered product spec"
        )
    return FixedPointDspPlan(
        template=(
            "DSP48E2_WIDE_CONST_MUL_V1"
            if spec.data_width > 27 and spec.accumulate is False
            else "DSP48E2_MUL_SHIFT_ACCUM_V1"
        ),
        operand_signed=spec.data_signed,
        coefficient_signed=spec.coefficient_signed,
        output_lsb=spec.shift,
        output_width=spec.term_width,
        output_signed=spec.term_signed,
        accumulator_width=spec.accumulator_width,
        product_latency=spec.product_latency,
    )


def build_fixed_point_action_plans(
    facts: dict[str, Any],
    *,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Build the single proof-gated plan; no plan grants emitted-DCP authority."""
    spec = fixed_point_spec_from_extracted_facts(facts)
    plan = derive_fixed_point_plan(spec)
    authorization = build_fixed_point_authorization_from_facts(
        facts, plan, timeout_ms=timeout_ms
    )
    if (
        not isinstance(authorization, dict)
        or not isinstance(authorization.get("certificate_id"), str)
    ):
        raise OperatorProofError("fixed-point authorization was not produced")
    return [{
        "plan": plan,
        "authorization": authorization,
        "derived_resources": {
            "dsp48e2_sites": 2 if plan.template == "DSP48E2_WIDE_CONST_MUL_V1" else 1
        },
        "authority": "pre_mutation_only",
    }]


def _candidate_plan(candidate: dict[str, Any]) -> FixedPointDspPlan:
    if (
        not isinstance(candidate, dict)
        or candidate.get("authority") != "pre_mutation_only"
    ):
        raise OperatorProofError(
            "candidate is not a pre-mutation fixed-point action plan"
        )
    raw = candidate.get("plan")
    if isinstance(raw, FixedPointDspPlan):
        return raw
    if not isinstance(raw, dict):
        raise OperatorProofError("candidate plan is missing")
    try:
        return FixedPointDspPlan(**raw)
    except (TypeError, ValueError) as exc:
        raise OperatorProofError("candidate plan has an invalid schema") from exc


def _verify_candidate(
    facts: dict[str, Any], candidate: dict[str, Any]
) -> FixedPointDspPlan:
    plan = _candidate_plan(candidate)
    authorization = candidate.get("authorization")
    if not isinstance(authorization, dict) or not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(facts.get("design_sha256") or ""),
        expected_region_sha256=str(facts.get("region_sha256") or ""),
    ):
        raise OperatorProofError("fixed-point authorization replay failed")
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
            "fixed-point oracle slots must be a nonempty list"
        )
    result = []
    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _ORACLE_SLOT_FIELDS:
            raise OperatorProofError(
                f"fixed-point oracle slot [{index}] has an unsupported schema"
            )
        site = record.get("site")
        if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
            raise OperatorProofError(
                f"fixed-point oracle slot [{index}] site is not a DSP48E2 site"
            )
        if record.get("bel") != "DSP48E2":
            raise OperatorProofError(
                f"fixed-point oracle slot [{index}] BEL is not DSP48E2"
            )
        for field in ("source_dcp_sha256", "region_sha256"):
            value = record.get(field)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise OperatorProofError(
                    f"fixed-point oracle slot [{index}] {field} is invalid"
                )
        if record["source_dcp_sha256"] != facts.get("design_sha256") or record[
            "region_sha256"
        ] != facts.get("region_sha256"):
            raise OperatorProofError(
                f"fixed-point oracle slot [{index}] is stale or cross-seed"
            )
        if record.get("clock_route_ok") is not True:
            raise OperatorProofError(
                f"fixed-point oracle slot [{index}] clock route is not measured"
            )
        distance = record.get("distance")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, int)
            or distance < 0
        ):
            raise OperatorProofError(
                f"fixed-point oracle slot [{index}] distance is invalid"
            )
        key = (site, record["bel"])
        if key in seen:
            raise OperatorProofError(
                "fixed-point oracle slots contain a duplicate site/BEL"
            )
        seen.add(key)
        result.append(dict(record))
    return result


def allocate_fixed_point_dsp_placement(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
    *,
    oracle_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allocate the closest measured DSP48E2 slot under a replayed authorization."""
    fixed_point_spec_from_extracted_facts(facts)
    plan = _verify_candidate(facts, action_candidate)
    slots = _validated_oracle_slots(oracle_slots, facts=facts)
    if plan.template == "DSP48E2_WIDE_CONST_MUL_V1":
        by_site = {slot["site"]: slot for slot in slots}
        pairs = []
        for lower in slots:
            match = re.fullmatch(r"DSP48E2_X(\d+)Y(\d+)", lower["site"])
            upper = by_site.get(
                f"DSP48E2_X{match.group(1)}Y{int(match.group(2)) + 1}"
            )
            if upper is not None:
                pairs.append((lower, upper))
        if not pairs:
            raise OperatorProofError(
                "wide fixed-point placement requires an adjacent DSP cascade pair"
            )
        lower, upper = min(
            pairs,
            key=lambda pair: (
                pair[0]["distance"] + pair[1]["distance"],
                pair[0]["site"],
            ),
        )
        selected_placements = [
            {"site": lower["site"], "bel": lower["bel"]},
            {"site": upper["site"], "bel": upper["bel"]},
        ]
    else:
        selected = min(slots, key=lambda slot: (slot["distance"], slot["site"]))
        selected_placements = [{"site": selected["site"], "bel": selected["bel"]}]
    manifest = {
        "kind": "fixed_point_placement_manifest",
        "version": PLACEMENT_MANIFEST_VERSION,
        "source": {
            "dcp_sha256": facts["design_sha256"],
            "region_sha256": facts["region_sha256"],
        },
        "candidate_id": facts["candidate_id"],
        "authorization_id": action_candidate["authorization"]["certificate_id"],
        "placements": selected_placements,
        "authority": "pre_emission_only",
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def build_fixed_point_topology_manifest(
    facts: dict[str, Any],
    action_candidate: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Describe the exact intended DSP graph while withholding mutation authority."""
    spec = fixed_point_spec_from_extracted_facts(facts)
    plan = _verify_candidate(facts, action_candidate)
    claimed = placement.get("manifest_sha256") if isinstance(placement, dict) else None
    payload = dict(placement) if isinstance(placement, dict) else {}
    payload.pop("manifest_sha256", None)
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "fixed_point_placement_manifest"
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
            "fixed-point placement manifest is stale, modified, or cross-plan"
        )
    placements = placement.get("placements")
    expected_placements = 2 if plan.template == "DSP48E2_WIDE_CONST_MUL_V1" else 1
    if (
        not isinstance(placements, list)
        or len(placements) != expected_placements
        or any(
            not isinstance(item, dict)
            or set(item) != {"site", "bel"}
            or item.get("bel") != "DSP48E2"
            or not isinstance(item.get("site"), str)
            or _DSP_SITE_RE.fullmatch(item["site"]) is None
            for item in placements
        )
    ):
        raise OperatorProofError(
            "fixed-point placement has the wrong DSP48E2 site inventory"
        )
    if expected_placements == 2:
        lower = re.fullmatch(r"DSP48E2_X(\d+)Y(\d+)", placements[0]["site"])
        upper = re.fullmatch(r"DSP48E2_X(\d+)Y(\d+)", placements[1]["site"])
        if lower.group(1) != upper.group(1) or int(upper.group(2)) != int(lower.group(2)) + 1:
            raise OperatorProofError("wide fixed-point DSP sites are not an ordered cascade pair")
    dsp_manifest = {
        "opmode_contract": {
            "multiply": True,
            "accumulate": plan.template == "DSP48E2_MUL_SHIFT_ACCUM_V1",
            "output_lsb": plan.output_lsb,
            "output_width": plan.output_width,
        },
        "coefficient": spec.coefficient,
        "product_latency": spec.product_latency,
    }
    if expected_placements == 2:
        dsp_manifest.update({
            "sites": [item["site"] for item in placements],
            "template": plan.template,
        })
    else:
        dsp_manifest["site"] = placements[0]["site"]
    manifest = {
        "kind": "fixed_point_topology_manifest",
        "version": TOPOLOGY_MANIFEST_VERSION,
        "source": dict(placement["source"]),
        "authorization_id": placement["authorization_id"],
        "dsp": dsp_manifest,
        "emission_eligible": True,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest
