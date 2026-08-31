"""Split execution provider for cold registered product-sum actions.

The four public stages deliberately have independent contracts so a scheduler
may run plan proof and implementation concurrently without weakening the final
join.  Only the join of plan authorization, emitted-topology identity, and
strict validation can produce an Agent candidate proof.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .artifacts import sha256_file
from .cex_replay_adjudicator import ArtifactBinding
from .operator_proof_current import (
    STRICT_STIMULUS_PROTOCOL,
    build_registered_product_sum_authorization,
)
from .regarith_tools.common import object_digest
from .submission_worker import run_strict_validation_worker


CHEAP_GUARD_PROTOCOL = "registered_closure_deterministic_replay_v1"
CHEAP_GUARD_VECTORS = 512
_RUN_ID = re.compile(r"nr-[0-9a-f]{32}")


def _checked_run_id(value: Any) -> str:
    run_id = str(value or "")
    if _RUN_ID.fullmatch(run_id) is None:
        raise RuntimeError("REGARITH_CURRENT_RUN_ID_REQUIRED")
    return run_id


def _payload(value: Any) -> dict[str, Any]:
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise RuntimeError("registered product provider returned a non-object")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def guard_recovered_product_sum(
    recovered: Mapping[str, Any],
    *,
    expected_family_fingerprint: str,
    expected_source_sha256: str,
    expected_run_id: str,
) -> dict[str, Any]:
    """Replay the cheap 512-vector guard and bind the recovered plan set."""
    if not isinstance(recovered, Mapping):
        raise RuntimeError("REGARITH_COMPLETE_FAMILY_RECOVERY_REQUIRED")
    emission = recovered.get("emission")
    plan_digests = recovered.get("plan_digests")
    guard = recovered.get("cheap_replay_guard")
    if (
        not isinstance(emission, Mapping)
        or emission.get("result_digest") != expected_family_fingerprint
        or emission.get("source_artifact_sha256") != expected_source_sha256
        or guard
        != {
            "protocol": CHEAP_GUARD_PROTOCOL,
            "vector_count": CHEAP_GUARD_VECTORS,
            "status": "passed",
        }
        or not isinstance(plan_digests, list)
        or plan_digests != list(emission.get("plan_digests") or [])
        or recovered.get("recovered_plan_digest") != object_digest(plan_digests)
        or recovered.get("run_id") != _checked_run_id(expected_run_id)
    ):
        raise RuntimeError("REGARITH_CHEAP_REPLAY_GUARD_REQUIRED")
    bundle_path = Path(str(recovered.get("bundle_path") or "")).resolve()
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise RuntimeError("REGARITH_COMPLETE_FAMILY_RECOVERY_REQUIRED")
    try:
        serialized_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("REGARITH_COMPLETE_FAMILY_RECOVERY_REQUIRED") from exc
    if (
        not isinstance(serialized_bundle, dict)
        or not isinstance(recovered.get("bundle"), Mapping)
        or object_digest(serialized_bundle) != object_digest(dict(recovered["bundle"]))
    ):
        raise RuntimeError("REGARITH_RECOVERED_BUNDLE_BINDING_MISMATCH")
    return {
        "schema_version": "registered-product-provider-guard-v1",
        "status": "passed",
        "source_artifact_sha256": expected_source_sha256,
        "family_fingerprint": expected_family_fingerprint,
        "run_id": expected_run_id,
        "bundle_path": str(bundle_path),
        "bundle_file_sha256": sha256_file(bundle_path),
        "bundle_digest": emission["bundle_digest"],
        "plan_digests": list(plan_digests),
        "recovered_plan_digest": recovered["recovered_plan_digest"],
        "emission": dict(emission),
        "cheap_replay_guard": dict(guard),
    }


def build_product_sum_preflight_receipt(
    recovered: Mapping[str, Any],
    *,
    expected_family_fingerprint: str,
    expected_source_sha256: str,
    expected_run_id: str,
) -> dict[str, Any]:
    """Bind one cold artifact to its cheap guard and exact CPU proof."""
    guarded = guard_recovered_product_sum(
        recovered,
        expected_family_fingerprint=expected_family_fingerprint,
        expected_source_sha256=expected_source_sha256,
        expected_run_id=expected_run_id,
    )
    authorization = prove_guarded_product_sum(guarded, run_id=expected_run_id)
    body = {
        "schema_version": "registered-product-preflight-receipt-v1",
        "status": "proved",
        "source_artifact_sha256": expected_source_sha256,
        "family_fingerprint": expected_family_fingerprint,
        "run_id": _checked_run_id(expected_run_id),
        "bundle_file_sha256": guarded["bundle_file_sha256"],
        "bundle_digest": guarded["bundle_digest"],
        "recovered_plan_digest": guarded["recovered_plan_digest"],
        "guard_digest": object_digest(guarded),
        "authorization_digest": object_digest(authorization),
    }
    return {**body, "receipt_digest": object_digest(body)}


def load_product_sum_preflight_candidate(
    artifact_path: Path | str,
    *,
    expected_family_fingerprint: str,
    expected_source_sha256: str,
    expected_run_id: str,
) -> dict[str, Any]:
    """Load and independently replay a source/guard/proof-bound preflight row."""
    path = Path(artifact_path).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("REGARITH_PREFLIGHT_ARTIFACT_REQUIRED")
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("REGARITH_PREFLIGHT_ARTIFACT_REQUIRED") from exc
    if (
        not isinstance(artifact, dict)
        or artifact.get("kind") != "cold_registered_product_sum_facts_set"
        or artifact.get("design_sha256") != expected_source_sha256
        or artifact.get("run_id") != _checked_run_id(expected_run_id)
        or artifact.get("fresh_rediscovery") is not True
        or artifact.get("read_only") is not True
        or not isinstance(artifact.get("candidates"), list)
    ):
        raise RuntimeError("REGARITH_PREFLIGHT_ARTIFACT_STALE")
    matches = [
        row for row in artifact["candidates"]
        if isinstance(row, dict)
        and row.get("family_fingerprint") == expected_family_fingerprint
    ]
    if len(matches) != 1:
        raise RuntimeError("REGARITH_PREFLIGHT_FAMILY_NOT_FOUND")
    recovered = matches[0]
    expected_receipt = recovered.get("preflight_receipt")
    replayed = build_product_sum_preflight_receipt(
        recovered,
        expected_family_fingerprint=expected_family_fingerprint,
        expected_source_sha256=expected_source_sha256,
        expected_run_id=expected_run_id,
    )
    if not isinstance(expected_receipt, Mapping) or dict(expected_receipt) != replayed:
        raise RuntimeError("REGARITH_PREFLIGHT_RECEIPT_MISMATCH")
    return recovered


def prove_guarded_product_sum(
    guarded: Mapping[str, Any], *, run_id: str
) -> dict[str, Any]:
    """Run only the pure-CPU exact plan proof and return its authorization."""
    if guarded.get("schema_version") != "registered-product-provider-guard-v1":
        raise RuntimeError("REGARITH_CHEAP_REPLAY_GUARD_REQUIRED")
    authorization = build_registered_product_sum_authorization(
        dict(guarded["emission"]), run_id=_checked_run_id(run_id)
    )
    if (
        authorization.get("spec", {}).get("recovered_plan_digest")
        != guarded.get("recovered_plan_digest")
    ):
        raise RuntimeError("REGARITH_RECOVERED_PLAN_BINDING_MISMATCH")
    return authorization


async def implement_guarded_product_sum(
    mcp: Any,
    guarded: Mapping[str, Any],
    *,
    input_dcp: Path,
    route_timeout_s: float,
    run_id: str,
    contest_clock: str,
    run_source_sha256: str,
) -> dict[str, Any]:
    """Generate, insert, and route one exact current-run recovered family."""
    checked_run_id = _checked_run_id(run_id)
    source_sha256 = sha256_file(input_dcp)
    run_root_sha256 = str(run_source_sha256 or "")
    if (
        source_sha256 != guarded.get("source_artifact_sha256")
        or re.fullmatch(r"[0-9a-f]{64}", run_root_sha256) is None
    ):
        raise RuntimeError("STALE_REGISTERED_ARITHMETIC_FAMILY")
    run_dir = Path(mcp.run_dir).resolve()
    bundle_path = Path(str(guarded["bundle_path"])).resolve()
    fingerprint = guarded["family_fingerprint"]
    raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    synthesis_dir = run_dir / "registered_product_synthesis"
    synthesis = _payload(await mcp.call_vivado(
        "synthesize_registered_product_modules",
        {
            "dcp_path": str(input_dcp),
            "source_dcp_sha256": source_sha256,
            "bundle_path": str(bundle_path),
            "work_dir": str(synthesis_dir),
            "run_id": checked_run_id,
            "run_source_sha256": run_root_sha256,
            "clock_name": str(contest_clock or ""),
        },
        timeout=900.0,
    ))
    authorized = synthesis.get("authorized_emission")
    generation_binding = synthesis.get("generation_binding")
    module_dcps = synthesis.get("module_dcps")
    if (
        not isinstance(authorized, dict)
        or authorized.get("mutation_allowed") is not True
        or authorized.get("result_digest") != fingerprint
        or authorized.get("source_artifact_sha256") != source_sha256
        or not isinstance(generation_binding, dict)
        or generation_binding.get("run_id") != checked_run_id
        or generation_binding.get("source_artifact_sha256") != source_sha256
        or generation_binding.get("run_source_sha256") != run_root_sha256
        or generation_binding.get("emission_result_digest") != fingerprint
        or not isinstance(module_dcps, dict)
        or set(module_dcps) != {
            str(plan["module_name"]) for plan in raw_bundle.get("plans") or []
        }
    ):
        raise RuntimeError(
            "REGARITH_CURRENT_RUN_OOC_GATE_REQUIRED: fresh synthesis did not "
            "return a matching mutation-capable envelope"
        )
    inserted = run_dir / "registered_product_inserted.dcp"
    insertion = _payload(await mcp.call_rw(
        "insert_registered_product_modules",
        {
            "bundle_path": str(bundle_path),
            "module_dcps": module_dcps,
            "output_dcp": str(inserted),
        },
        timeout=900.0,
    ))
    if not inserted.is_file() or insertion.get("source_dcp_sha256") != source_sha256:
        raise RuntimeError("registered product module insertion is incomplete")
    relative_placement = (
        authorized.get("placement_manifest") or {}
    ).get("relative_geometry")
    ooc_gate = synthesis.get("ooc_gate")
    if (
        not isinstance(relative_placement, dict)
        or not isinstance(ooc_gate, dict)
        or ooc_gate.get("status") != "passed"
    ):
        raise RuntimeError("REGARITH_CURRENT_RUN_OOC_GATE_REQUIRED")
    route_authorization_body = {
        "schema_version": "regarith-current-run-generated-route-authorization-v1",
        "status": "authorized",
        "run_id": checked_run_id,
        "run_source_sha256": run_root_sha256,
        "source_artifact_sha256": source_sha256,
        "bundle_file_sha256": sha256_file(bundle_path),
        "bundle_digest": guarded["bundle_digest"],
        "emission_result_digest": fingerprint,
        "inserted_dcp_sha256": sha256_file(inserted),
        "generation_binding": dict(generation_binding),
        "emission_authorization_digest": authorized.get("authorization_digest"),
        "period_ns": float(generation_binding["source_clock_period_ns"]),
        "internal_data_path_ns_max": float(
            generation_binding["internal_data_path_ns_max"]
        ),
        "instance_inventory": [
            {
                "instance_name": str(row["name"]),
                "module_name": str(row["module_name"]),
            }
            for row in raw_bundle["instances"]
        ],
        "relative_placement_manifest": relative_placement,
        "mutation_allowed": True,
        "authority": "current_run_generated_rtl_ooc_and_exact_insertion_v1",
    }
    route_authorization = {
        **route_authorization_body,
        "authorization_digest": object_digest(route_authorization_body),
    }
    authorization_path = run_dir / "current_run_route_authorization.json"
    authorization_path.write_text(
        json.dumps(route_authorization, indent=2, sort_keys=True), encoding="utf-8"
    )
    output = run_dir / "operator_rewrite_registered_product.dcp"
    route = _payload(await mcp.call_vivado(
        "route_registered_product_bundle",
        {
            "inserted_dcp": str(inserted),
            "source_dcp": str(input_dcp),
            "source_dcp_sha256": source_sha256,
            "bundle_path": str(bundle_path),
            "output_dcp": str(output),
            "route_timeout_s": route_timeout_s,
            "physical_profile": "full_timing",
            "run_id": checked_run_id,
            "run_source_sha256": run_root_sha256,
            "current_run_authorization_path": str(authorization_path),
        },
        timeout=route_timeout_s + 1300.0,
    ))
    if not output.is_file():
        raise RuntimeError("registered product routed checkpoint is missing")
    candidate_sha256 = sha256_file(output)
    emitted_topology_digest = str(route.get("emitted_topology_digest") or "")
    generated_gate = route.get("current_run_generated_module_gate")
    placement_receipt = route.get("placement_binding_receipt")
    if (
        route.get("candidate") != {"dcp_sha256": candidate_sha256}
        or len(emitted_topology_digest) != 64
        or any(char not in "0123456789abcdef" for char in emitted_topology_digest)
        or not isinstance(generated_gate, dict)
        or generated_gate.get("status") != "passed"
        or generated_gate.get("authorization_digest")
        != route_authorization.get("authorization_digest")
        or not isinstance(placement_receipt, dict)
        or placement_receipt.get("status") not in {"applied", "degraded"}
        or placement_receipt.get("manifest_digest")
        != relative_placement.get("manifest_digest")
    ):
        raise RuntimeError("REGARITH_EMITTED_TOPOLOGY_DIGEST_REQUIRED")
    return {
        "schema_version": "registered-product-provider-implementation-v2",
        "status": "implemented",
        "run_id": checked_run_id,
        "candidate_path": str(output),
        "candidate_sha256": candidate_sha256,
        "emitted_topology_digest": emitted_topology_digest,
        "emitted_topology_artifact_digest": str(
            route.get("emitted_topology_artifact_digest") or ""
        ),
        "checks": dict(route.get("checks") or {}),
        "emission_gate_digest": generated_gate["gate_digest"],
        "emission_mode": "current_run_generated_rtl_ooc_v1",
        "current_run_generated_module_gate": dict(generated_gate),
        "placement_binding_receipt": dict(placement_receipt),
        "authorized_emission": authorized,
        "generation_binding": dict(generation_binding),
    }


async def strict_validate_implemented_product_sum(
    *,
    source_dcp: Path,
    implementation: Mapping[str, Any],
    report_path: Path,
    deadline_monotonic: float | None,
) -> dict[str, Any]:
    """Run strict validation as an independent final join input."""
    if implementation.get("status") != "implemented":
        raise RuntimeError("REGARITH_IMPLEMENTATION_REQUIRED")
    candidate = Path(str(implementation.get("candidate_path") or "")).resolve()
    if sha256_file(candidate) != implementation.get("candidate_sha256"):
        raise RuntimeError("REGARITH_IMPLEMENTATION_IDENTITY_CHANGED")
    return await run_strict_validation_worker(
        source_dcp,
        candidate,
        report_path,
        vectors=1000,
        precheck_vectors=100,
        timeout_s=1800.0,
        deadline_monotonic=deadline_monotonic,
    )


async def replay_product_sum_counterexample(
    *,
    source_dcp: Path,
    artifact: ArtifactBinding,
    counterexample: Mapping[str, Any],
    report_path: Path,
    deadline_monotonic: float | None,
    strict_runner: Callable[..., Awaitable[dict[str, Any]]] = (
        run_strict_validation_worker
    ),
) -> dict[str, Any]:
    """Replay a proof vector through the real strict-validation harness.

    The proof-side counterexample names a vector in the strict validator's
    deterministic LFSR stream.  Replaying through that index preserves the
    validator stimulus format while the returned observation deliberately
    excludes artifact identities, allowing the adjudicator to compare source
    and candidate outcomes mechanically.
    """
    source = Path(source_dcp).resolve()
    candidate = Path(artifact.path).resolve()
    if sha256_file(candidate) != artifact.sha256:
        raise RuntimeError("REGARITH_COUNTEREXAMPLE_ARTIFACT_IDENTITY_CHANGED")
    if counterexample.get("stimulus_protocol") != STRICT_STIMULUS_PROTOCOL:
        raise RuntimeError("REGARITH_COUNTEREXAMPLE_VECTOR_FORMAT_REQUIRED")
    vector_index = counterexample.get("vector_index")
    cycle = counterexample.get("cycle", vector_index)
    if (
        isinstance(vector_index, bool)
        or not isinstance(vector_index, int)
        or vector_index < 0
        or isinstance(cycle, bool)
        or not isinstance(cycle, int)
        or cycle < 0
    ):
        raise RuntimeError("REGARITH_COUNTEREXAMPLE_VECTOR_FORMAT_REQUIRED")
    vectors = max(1000, vector_index + 1)
    report = await strict_runner(
        source,
        candidate,
        Path(report_path),
        vectors=vectors,
        precheck_vectors=min(100, vectors),
        timeout_s=1800.0,
        deadline_monotonic=deadline_monotonic,
    )
    required = {
        "phase1_passed",
        "phase2_passed",
        "phase2_skipped",
        "infrastructure_failure",
        "stimulus_protocol",
        "vectors",
    }
    if (
        not isinstance(report, dict)
        or not required.issubset(report)
        or report.get("stimulus_protocol") != STRICT_STIMULUS_PROTOCOL
        or int(report.get("vectors", -1)) < vector_index + 1
    ):
        raise RuntimeError("REGARITH_COUNTEREXAMPLE_REPLAY_INCOMPLETE")
    return {
        "schema": "strict_validator_counterexample_observation_v1",
        "stimulus_protocol": STRICT_STIMULUS_PROTOCOL,
        "vector_index": vector_index,
        "cycle": cycle,
        "phase1_passed": report["phase1_passed"] is True,
        "phase2_passed": report["phase2_passed"] is True,
        "phase2_skipped": report["phase2_skipped"] is True,
        "infrastructure_failure": report["infrastructure_failure"] is True,
    }


__all__ = [
    "CHEAP_GUARD_PROTOCOL",
    "CHEAP_GUARD_VECTORS",
    "build_product_sum_preflight_receipt",
    "guard_recovered_product_sum",
    "prove_guarded_product_sum",
    "implement_guarded_product_sum",
    "load_product_sum_preflight_candidate",
    "replay_product_sum_counterexample",
    "strict_validate_implemented_product_sum",
]
