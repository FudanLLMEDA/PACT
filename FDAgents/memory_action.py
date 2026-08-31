"""Derive proof-gated memory repack actions only from recovered DCP facts."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .operator_proof_current import (
    MemoryPrimitiveSpec,
    MemoryRepackPlan,
    OperatorProofError,
    _digest,
    _stable,
    build_memory_repack_authorization_from_facts,
    memory_spec_from_extracted_facts,
    verify_rewrite_authorization,
)


def derive_memory_repack_plan(spec: MemoryPrimitiveSpec) -> MemoryRepackPlan:
    if not isinstance(spec, MemoryPrimitiveSpec):
        raise OperatorProofError("memory plan derivation requires recovered facts")
    return MemoryRepackPlan(
        template="BRAM_MEMORY_SEMANTIC_REPACK_V1",
        primitive=spec.primitive,
        ram_mode=spec.ram_mode,
        semantic_contract_sha256=_digest(asdict(spec)),
    )


def build_memory_action_plans(
    facts: dict[str, Any],
    *,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    spec = memory_spec_from_extracted_facts(facts)
    plan = derive_memory_repack_plan(spec)
    authorization = build_memory_repack_authorization_from_facts(
        facts, plan, timeout_ms=timeout_ms
    )
    if not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=facts["design_sha256"],
        expected_region_sha256=facts["region_sha256"],
        timeout_ms=timeout_ms,
    ):
        raise OperatorProofError("memory authorization did not replay")
    return [{
        "plan": plan,
        "authorization": authorization,
        "derived_resources": {"bram_sites": 1},
        "authority": "pre_mutation_only",
    }]


def _validated_memory_candidate(
    facts: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    if (
        not isinstance(candidate, dict)
        or candidate.get("authority") != "pre_mutation_only"
        or not isinstance(candidate.get("authorization"), dict)
    ):
        raise OperatorProofError("memory candidate is not proof-gated")
    authorization = candidate["authorization"]
    if not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(facts.get("design_sha256") or ""),
        expected_region_sha256=str(facts.get("region_sha256") or ""),
    ):
        raise OperatorProofError("memory candidate authorization replay failed")
    plan = candidate.get("plan")
    raw_plan = asdict(plan) if isinstance(plan, MemoryRepackPlan) else plan
    if not isinstance(raw_plan, dict) or _stable(raw_plan) != _stable(
        authorization.get("plan")
    ):
        raise OperatorProofError("memory candidate plan differs from authorization")
    return authorization


def validate_memory_consumer_facts(
    facts: dict[str, Any], consumer_facts: dict[str, Any]
) -> str:
    if not isinstance(consumer_facts, dict):
        raise OperatorProofError("memory consumer facts must be an object")
    claimed = consumer_facts.get("region_sha256")
    payload = dict(consumer_facts)
    payload.pop("region_sha256", None)
    for key in ("candidate_id", "fresh_rediscovery", "runtime_s"):
        payload.pop(key, None)
    producer = consumer_facts.get("producer")
    if (
        consumer_facts.get("kind") != "memory_consumer_dependency_facts"
        or consumer_facts.get("version") != 1
        or consumer_facts.get("status") != "success"
        or consumer_facts.get("fresh_rediscovery") is not True
        or consumer_facts.get("mutation_eligible") is not False
        or consumer_facts.get("fanout_extraction_complete") is not True
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
        or consumer_facts.get("candidate_id") != "memoryconsumer:" + claimed[:20]
        or not isinstance(producer, dict)
        or producer.get("candidate_id") != facts.get("candidate_id")
        or producer.get("region_sha256") != facts.get("region_sha256")
        or consumer_facts.get("design_sha256") != facts.get("design_sha256")
    ):
        raise OperatorProofError("memory consumer facts do not bind the producer")
    return claimed


def build_memory_emission_manifests(
    facts: dict[str, Any],
    consumer_facts: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Bind physical and topology emission to recovered facts without knobs."""
    memory_spec_from_extracted_facts(facts)
    authorization = _validated_memory_candidate(facts, candidate)
    consumer_region = validate_memory_consumer_facts(facts, consumer_facts)
    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or not isinstance(boundary.get("physical"), dict):
        raise OperatorProofError("memory physical boundary is unavailable")
    physical = boundary["physical"]
    placement = {
        "kind": "memory_repack_placement_manifest",
        "version": 1,
        "source_dcp_sha256": facts["design_sha256"],
        "source_region_sha256": facts["region_sha256"],
        "candidate_id": facts["candidate_id"],
        "authorization_id": authorization["certificate_id"],
        "physical_binding_sha256": _digest(physical),
        "semantic_parameter_policy": "fresh_recovered_facts_only",
        "authority": "emission_contract_only",
        "mutation_eligible": False,
    }
    placement["manifest_sha256"] = _digest(placement)
    topology = {
        "kind": "memory_repack_topology_manifest",
        "version": 1,
        "source_dcp_sha256": facts["design_sha256"],
        "source_region_sha256": facts["region_sha256"],
        "consumer_region_sha256": consumer_region,
        "candidate_id": facts["candidate_id"],
        "authorization_id": authorization["certificate_id"],
        "boundary_sha256": _digest(boundary),
        "property_sha256": facts["spec"]["property_sha256"],
        "dependency_edges_sha256": _digest(consumer_facts["dependency_edges"]),
        "semantic_parameter_policy": "fresh_recovered_facts_only",
        "authority": "emission_contract_only",
        "mutation_eligible": False,
        "emission_eligible": True,
    }
    topology["manifest_sha256"] = _digest(topology)
    return {"placement": placement, "topology": topology}
