"""Closed-template structural rewrites gated by post-emission proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from ..artifacts import sha256_file
from ..action_menu import build_action_menu, build_degraded_action_menu
from ..cex_replay_adjudicator import ArtifactBinding
from ..cold_product_sum import recover_cold_product_sum_families
from ..concurrent_proof import (
    CandidateProofRequest,
    ConcurrentProofAction,
    ExactProofResult,
    ExactProofStatus,
    ProofClass,
    ShadowPrefetchResult,
    SpeculativeExecutionResult,
)
from ..fixed_point_action_current import (
    allocate_fixed_point_dsp_placement,
    build_fixed_point_action_plans,
)
from ..probe_handoff import resolve_recurrence_family_facts
from ..operator_proof_current import (
    MIN_POST_EMISSION_PRECHECK_VECTORS,
    MIN_POST_EMISSION_VECTORS,
    OPERATOR_CANDIDATE_PROOF_VERSION,
    OPERATOR_TEMPLATE_BINDINGS,
    OperatorProofError,
    build_operator_candidate_proof,
    fixed_point_contract_set_has_executor_authority,
)
from ..registered_product_sum_provider import (
    guard_recovered_product_sum,
    implement_guarded_product_sum,
    load_product_sum_preflight_candidate,
    prove_guarded_product_sum,
    replay_product_sum_counterexample,
    strict_validate_implemented_product_sum,
)
from ..pipeline_rebalance_action_current import (
    build_pipeline_rebalance_action_plans,
    build_pipeline_rebalance_placement_manifest,
    build_pipeline_rebalance_topology_manifest,
)
from ..recurrence_action_current import (
    allocate_recurrence_family_srl_placements,
    build_recurrence_family_action_plans,
)
from ..register_absorb_action_current import (
    build_register_absorb_action_plans,
    build_register_absorb_placement_manifest,
)
from ..registered_arithmetic_adapters import adapt_fixed_point_contract_facts
from ..constructive_accumulator_current import (
    MAX_BDD_NODES_PER_OUTPUT,
    extract_constructive_accumulator,
    validate_constructive_shape,
)
from ..constructive_accumulator_lowering import build_constructive_lowering
from ..compressor_proof import ConeGraph
from ..memory_write_buffer_forward_action import (
    recover_memory_forward_contracts,
    render_memory_forward_eco_tcl,
)
from ..metaloop_action_audit import (
    render_constructive_audit_tcl,
    render_memory_forward_audit_tcl,
)
from ..metaloop_action_proof import (
    build_constructive_accumulator_authorization,
    build_memory_forward_authorization,
)
from ..residual_profile import (
    build_measured_residual_profile,
    capture_live_timing_report,
    constructive_endpoint_stems,
)
from ..regarith_tools.agent_evidence import (
    screen_recovered_registered_arithmetic,
)
from ..regarith_tools.dcp_validation import product_sum_plan_from_facts
from ..regarith_tools.production_product_sum import dispatch_product_sum_backend
from ..regarith_tools.common import object_digest
from ..submission_worker import run_strict_validation_worker
from .semantic_replay import _run_vivado_stage
from ..two_operand_action_current import (
    allocate_two_operand_dsp_placement,
    build_two_operand_action_plans,
)
from .base import SkillOutput, SkillResult, unexpected_execution_params


PROOF_VERSION = OPERATOR_CANDIDATE_PROOF_VERSION
MAX_CANDIDATES = 1
POST_EMISSION_VERIFIER_AVAILABLE = True
RECURRENCE_EXECUTION_BACKEND_AVAILABLE = True
FIXED_POINT_EXECUTION_BACKEND_AVAILABLE = True
TWO_OPERAND_EXECUTION_BACKEND_AVAILABLE = True
REGISTER_ABSORB_EXECUTION_BACKEND_AVAILABLE = True
PIPELINE_REBALANCE_EXECUTION_BACKEND_AVAILABLE = True
CONSTRUCTIVE_ACTION_MAX_BDD_NODES = MAX_BDD_NODES_PER_OUTPUT


async def recover_constructive_nomination_contract(
    mcp,
    *,
    input_dcp: Path,
    source_sha256: str,
    clock: str,
    output_dir: Path,
    parent_report: str | None = None,
) -> dict:
    """Recover the complete current-seed constructive family for nomination."""
    if input_dcp.is_symlink() or not input_dcp.is_file():
        raise ValueError("CACC_INPUT_DCP_UNAVAILABLE")
    if sha256_file(input_dcp) != source_sha256:
        raise ValueError("CACC_SOURCE_ARTIFACT_MISMATCH")
    report = parent_report or await capture_live_timing_report(
        mcp, clock=clock, top_n=300
    )
    stems = constructive_endpoint_stems(report, limit=300)
    extract_script = (
        Path(__file__).resolve().parents[2] / "scripts" / "extract_register_cone.tcl"
    )
    proved: list[dict] = []
    rejections = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for ordinal, stem in enumerate(stems[:12]):
        cone_dir = output_dir / f"cone_{ordinal}"
        try:
            await _run_vivado_stage(
                script=extract_script,
                args=[str(input_dcp), stem, str(cone_dir)],
                cwd=output_dir,
                log_path=output_dir / f"extract_{ordinal}.log",
                timeout_s=240.0,
            )
            graph = ConeGraph.load(cone_dir)
            validate_constructive_shape(graph)
            extraction = await asyncio.to_thread(
                extract_constructive_accumulator,
                cone_dir,
                random_samples=10_000,
                max_bdd_nodes=CONSTRUCTIVE_ACTION_MAX_BDD_NODES,
            )
            expression = extraction["canonical_comparison"][
                "actual_expression_sha256"
            ]
            if (
                extraction.get("mutation_eligible") is not True
                or extraction.get("status") != "proved"
            ):
                raise ValueError("CACC_CONTRACT_PROOF_INCOMPLETE")
            proof_path = cone_dir / "nomination_constructive_proof.json"
            proof_path.write_text(
                json.dumps({
                    "schema_version": extraction["schema_version"],
                    "status": "proved",
                    "sibling_count": 1,
                    "sibling_expression_agreement": True,
                    "siblings": [extraction],
                    "mutation_eligible": True,
                }, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            certificate = await asyncio.to_thread(
                build_constructive_lowering, cone_dir, proof_path
            )
            if certificate.get("mutation_ready") is not True:
                raise ValueError("CACC_LOWERING_NOT_READY")
            semantic_signature = object_digest({
                "expression_sha256": expression,
                "derived_expression": extraction.get("derived_expression"),
                "frontier_words": extraction.get("frontier_words"),
                "weighted_sum": certificate.get("weighted_sum"),
                "compressor_spec": certificate.get("compressor_spec"),
            })
            proved.append({
                "ordinal": ordinal,
                "expression_sha256": expression,
                "semantic_signature": semantic_signature,
            })
            if sum(
                row["semantic_signature"] == semantic_signature for row in proved
            ) >= 3:
                break
        except Exception as exc:  # exact current-seed rejection remains typed
            rejections.append({
                "ordinal": ordinal,
                "reason_code": str(exc).split(":", 1)[0][:120],
            })
    signature_counts: dict[str, int] = {}
    for row in proved:
        signature = str(row["semantic_signature"])
        signature_counts[signature] = signature_counts.get(signature, 0) + 1
    proved_sibling_count = max(signature_counts.values(), default=0)
    complete = proved_sibling_count >= 3
    return {
        "source_artifact_sha256": source_sha256,
        "hypothesis_count": int(bool(stems)),
        "complete_contract_count": int(complete),
        "missing_obligation_count": (
            0 if complete else max(1, 3 - proved_sibling_count)
        ),
        "live_wall_covered_path_count": len(stems),
        "nomination_blockers": [] if complete and stems else [
            "CACC_SIBLING_FAMILY_INCOMPLETE"
            if not complete else "CACC_ZERO_LIVE_WALL_COVERAGE"
        ],
        "proved_sibling_count": proved_sibling_count,
        "typed_rejections": rejections,
    }


async def recover_memory_forward_nomination_contract(
    mcp,
    *,
    input_dcp: Path,
    source_sha256: str,
    clock: str,
    output_dir: Path,
    live_wall_covered_path_count: int,
) -> dict:
    """Recover a complete current-seed forwarding contract for nomination."""
    del clock
    if input_dcp.is_symlink() or not input_dcp.is_file():
        raise ValueError("MEMFWD_INPUT_DCP_UNAVAILABLE")
    if sha256_file(input_dcp) != source_sha256:
        raise ValueError("MEMFWD_SOURCE_ARTIFACT_MISMATCH")
    output_dir.mkdir(parents=True, exist_ok=True)
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts" / "extract_memory_forward_candidates.tcl"
    )
    await _run_vivado_stage(
        script=script,
        args=[str(input_dcp), str(output_dir)],
        cwd=output_dir,
        log_path=output_dir / "recovery.log",
        timeout_s=180.0,
    )
    rejections: list[dict] = []
    recovered = await asyncio.to_thread(
        recover_memory_forward_contracts,
        output_dir,
        source_sha256,
        rejection_log=rejections,
    )
    complete = bool(recovered)
    blocker = "MEMFWD_CONSUMER_CLOSURE_INCOMPLETE"
    if rejections:
        raw = str(rejections[0].get("reason_code") or blocker)
        if raw == "MEMFWD_READ_CONSUMER_CLOSURE_INCOMPLETE":
            raw = blocker
        blocker = raw if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", raw) else blocker
    blockers = []
    if not complete:
        blockers.append(blocker)
    if live_wall_covered_path_count <= 0:
        blockers.append("MEMFWD_ZERO_LIVE_WALL_COVERAGE")
    return {
        "source_artifact_sha256": source_sha256,
        "hypothesis_count": int(live_wall_covered_path_count > 0),
        "complete_contract_count": int(complete),
        "missing_obligation_count": 0 if complete else 1,
        "live_wall_covered_path_count": int(live_wall_covered_path_count),
        "nomination_blockers": blockers,
        "proved_contract_count": len(recovered),
        "typed_rejections": rejections,
    }


@dataclass(frozen=True, slots=True)
class OperatorRewriteTemplate:
    rewrite_kind: str
    proof_template: str
    execution_model: str
    execution_status: str
    proof_templates: tuple[str, ...] = ()


OPERATOR_REWRITE_REGISTRY: Mapping[str, OperatorRewriteTemplate] = MappingProxyType({
    "dsp_srl_recurrence_cut_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_srl_recurrence_cut_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_srl_recurrence_cut_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
    ),
    "fixed_point_dsp_rebuild_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "fixed_point_dsp_rebuild_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "fixed_point_dsp_rebuild_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
    ),
    "dsp_mul_two_operand_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_mul_two_operand_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_mul_two_operand_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
    ),
    "dsp_register_absorb_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_register_absorb_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_register_absorb_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
    ),
    "dsp_pipeline_rebalance_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_pipeline_rebalance_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "dsp_pipeline_rebalance_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
    ),
    "registered_arithmetic_cone_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "registered_arithmetic_cone_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "registered_arithmetic_cone_v1"
        ]["proof_template"],
        proof_templates=tuple(OPERATOR_TEMPLATE_BINDINGS[
            "registered_arithmetic_cone_v1"
        ]["proof_templates"]),
        execution_model="exact_seed_dcp_eco_nomination_gated",
        execution_status="executable",
    ),
    "constructive_accumulator_rebuild_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "constructive_accumulator_rebuild_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "constructive_accumulator_rebuild_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco_family_generic_shape_gated",
        execution_status="executable",
    ),
    "memory_write_buffer_forward_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "memory_write_buffer_forward_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "memory_write_buffer_forward_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco_family_generic_shape_gated",
        execution_status="executable",
    ),
    "exact_control_to_data_state_equation_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "exact_control_to_data_state_equation_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "exact_control_to_data_state_equation_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco_state_equation_gated",
        execution_status="executable",
    ),
})
OPERATOR_REWRITE_TARGETS = tuple(OPERATOR_REWRITE_REGISTRY)

_EXECUTION_PARAMS = {
    "input_dcp",
    "max_candidates",
    "route_timeout_s",
    "min_candidate_remaining_s",
    "_contest_clock",
    "_execution_deadline_monotonic",
    "_candidate_commit_reserve_s",
    "family_fingerprint",
    "_required_family_fingerprint",
    "_run_id",
    "_run_source_sha256",
}

_EXECUTABLE_DISPATCH_TARGETS = frozenset({
    "dsp_srl_recurrence_cut_v1",
    "fixed_point_dsp_rebuild_v1",
    "dsp_mul_two_operand_v1",
    "dsp_register_absorb_v1",
    "dsp_pipeline_rebalance_v1",
    "registered_arithmetic_cone_v1",
    "constructive_accumulator_rebuild_v1",
    "memory_write_buffer_forward_v1",
})


def operator_rewrite_executor_ready(target: str) -> bool:
    """Return whether this tree has a real fail-closed execution dispatch."""
    return str(target) in _EXECUTABLE_DISPATCH_TARGETS


def operator_rewrite_catalog() -> dict[str, dict[str, object]]:
    """Return the public closed-template catalog without implementation mechanics."""
    return {
        target: {
            "rewrite_kind": spec.rewrite_kind,
            "proof_template": spec.proof_template,
            "execution_model": spec.execution_model,
            "execution_status": (
                spec.execution_status
                if operator_rewrite_executor_ready(target)
                else "blocked_missing_current_seed_backend"
            ),
            "proof_templates": list(spec.proof_templates or (spec.proof_template,)),
        }
        for target, spec in OPERATOR_REWRITE_REGISTRY.items()
    }


class OperatorRewriteSkill:
    """Execute one closed rewrite template under exact proof and validation gates."""

    name = "operator_rewrite"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        started = time.monotonic()
        failed_dcp = Path(mcp.run_dir) / "operator_rewrite_failed.dcp"
        spec = OPERATOR_REWRITE_REGISTRY.get(target)
        if spec is None:
            return SkillResult.failure(
                before_wns,
                f"unknown operator_rewrite template {target!r}",
                failed_dcp,
            )
        unknown = unexpected_execution_params(params, _EXECUTION_PARAMS)
        if unknown:
            return SkillResult.failure(
                before_wns,
                f"operator_rewrite rejected parameters {unknown}",
                failed_dcp,
            )
        max_candidates = params.get("max_candidates", MAX_CANDIDATES)
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or max_candidates != MAX_CANDIDATES
        ):
            return SkillResult.failure(
                before_wns,
                f"operator_rewrite max_candidates must be {MAX_CANDIDATES}",
                failed_dcp,
            )

        if target == "dsp_srl_recurrence_cut_v1":
            return await self._execute_recurrence(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "fixed_point_dsp_rebuild_v1":
            return await self._execute_fixed_point(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "dsp_mul_two_operand_v1":
            return await self._execute_two_operand(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "dsp_register_absorb_v1":
            return await self._execute_register_absorb(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "dsp_pipeline_rebalance_v1":
            return await self._execute_pipeline_rebalance(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "registered_arithmetic_cone_v1":
            return await self._execute_registered_arithmetic(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "constructive_accumulator_rebuild_v1":
            return await self._execute_constructive_accumulator(
                mcp, target, params, before_wns, clock_period, started, failed_dcp
            )
        if target == "memory_write_buffer_forward_v1":
            return await self._execute_memory_forward(
                mcp, target, params, before_wns, clock_period, started, failed_dcp
            )
        return SkillResult.failure(
            before_wns,
            "operator_rewrite execution backend is not available",
            failed_dcp,
            details={
                "proof_version": PROOF_VERSION,
                "template": target,
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_status": "blocked_pending_execution_backend",
                "pre_mutation_authorization_is_candidate_authority": False,
                "runtime_s": round(time.monotonic() - started, 6),
            },
        )

    async def prepare_concurrent_proof(
        self,
        *,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
        candidate_id: str,
        parent_candidate_id: str,
        source_artifact_path: Path,
        source_artifact_sha256: str,
        action_fingerprint: str,
        vm_slot: str,
    ) -> ConcurrentProofAction:
        """Prepare the real product-sum proof/implementation split backend."""
        started = time.monotonic()
        if target != "registered_arithmetic_cone_v1":
            raise RuntimeError(
                "concurrent proof is available only for registered arithmetic"
            )
        unknown = unexpected_execution_params(params, _EXECUTION_PARAMS)
        if unknown:
            raise RuntimeError(f"operator_rewrite rejected parameters {unknown}")
        fingerprint = str(params.get("family_fingerprint") or "")
        if len(fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in fingerprint
        ):
            raise RuntimeError(
                "registered arithmetic requires one exact family_fingerprint"
            )
        input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
        source_path = Path(source_artifact_path).resolve()
        if input_dcp != source_path or sha256_file(input_dcp) != source_artifact_sha256:
            raise RuntimeError("STALE_REGISTERED_ARITHMETIC_FAMILY")
        context = await self._recover_registered_arithmetic_context(
            mcp,
            input_dcp=input_dcp,
            source_sha256=source_artifact_sha256,
            fingerprint=fingerprint,
            contest_clock=str(params.get("_contest_clock") or ""),
            run_id=str(params.get("_run_id") or ""),
        )
        selected = context["selected"]
        if selected.get("nomination_eligible") is not True:
            rejection = selected.get("typed_rejection") or {
                "reason_code": "REGISTERED_ARITHMETIC_GATE_REJECTED",
                "message": ",".join(selected.get("nomination_blockers") or []),
            }
            raise RuntimeError(
                str(rejection.get("message") or rejection.get("reason_code"))
            )
        if selected.get("family_kind") != "product_sum":
            raise RuntimeError(
                "concurrent proof provider requires a complete product-sum family"
            )
        cold_matches = [
            item for item in context["cold"].get("candidates", [])
            if item.get("family_fingerprint") == fingerprint
        ]
        if len(cold_matches) != 1:
            raise RuntimeError(
                "REGARITH_COMPLETE_FAMILY_RECOVERY_REQUIRED: nominated product "
                "sum is not a cold complete-family bundle"
            )
        recovered = cold_matches[0]
        guarded = guard_recovered_product_sum(
            recovered,
            expected_family_fingerprint=fingerprint,
            expected_source_sha256=source_artifact_sha256,
            expected_run_id=str(params.get("_run_id") or ""),
        )
        request = CandidateProofRequest(
            candidate_id=candidate_id,
            parent_candidate_id=parent_candidate_id,
            source=ArtifactBinding(source_path, source_artifact_sha256),
            proof_class=ProofClass.REGISTERED_ARITHMETIC_EXACT,
            action_fingerprint=action_fingerprint,
            recovered_plan_digest=guarded["recovered_plan_digest"],
            vm_slot=vm_slot,
        )
        loop = asyncio.get_running_loop()
        action_run_dir = Path(mcp.run_dir).resolve()
        authorization_holder: dict[str, dict] = {}
        route_timeout = float(params.get("route_timeout_s", 1800.0))
        deadline = params.get("_execution_deadline_monotonic")
        reserve = max(
            float(params.get("_candidate_commit_reserve_s", 0.0)),
            float(params.get("min_candidate_remaining_s", 0.0)),
        )
        validation_deadline = (
            float(deadline) - reserve if deadline is not None else None
        )

        def cheap_replay_guard(proof_request: CandidateProofRequest) -> bool:
            return bool(
                proof_request.source.sha256 == source_artifact_sha256
                and proof_request.recovered_plan_digest
                == guarded["recovered_plan_digest"]
                and proof_request.cheap_replay_vectors == 512
                and guarded["cheap_replay_guard"].get("status") == "passed"
                and guarded["cheap_replay_guard"].get("vector_count") == 512
            )

        def exact_proof(_proof_request: CandidateProofRequest) -> ExactProofResult:
            proof_started = time.monotonic()
            authorization = prove_guarded_product_sum(
                guarded, run_id=str(params.get("_run_id") or "")
            )
            authorization_holder["authorization"] = authorization
            return ExactProofResult(
                status=ExactProofStatus.PROVED,
                proof_id=authorization["certificate_id"],
                proof_digest=authorization["certificate_id"],
                recovered_plan_digest=guarded["recovered_plan_digest"],
                runtime_s=time.monotonic() - proof_started,
            )

        def speculative_execution(
            proof_request: CandidateProofRequest,
        ) -> SpeculativeExecutionResult:
            implementation_started = time.monotonic()
            future = asyncio.run_coroutine_threadsafe(
                implement_guarded_product_sum(
                    mcp,
                    guarded,
                    input_dcp=input_dcp,
                    route_timeout_s=route_timeout,
                    run_id=str(params.get("_run_id") or ""),
                    contest_clock=str(params.get("_contest_clock") or ""),
                    run_source_sha256=str(params.get("_run_source_sha256") or ""),
                ),
                loop,
            )
            implementation = future.result()
            checks = implementation.get("checks") or {}
            routed = all(
                checks.get(name) is True
                for name in (
                    "current_run_source_and_generation_bound",
                    "registered_bundle_wiring_verified",
                    "complete_old_driver_disconnection_verified",
                    "exact_module_inventory",
                    "candidate_checkpoint_reopened",
                    "source_checkpoint_reopened",
                    "relevant_nets_fully_routed",
                )
            )
            return SpeculativeExecutionResult(
                candidate_id=proof_request.candidate_id,
                candidate=ArtifactBinding(
                    Path(implementation["candidate_path"]),
                    implementation["candidate_sha256"],
                ),
                isolated_copy=True,
                emitted_topology_digest=implementation[
                    "emitted_topology_digest"
                ],
                routed=routed,
                measurement_complete=routed,
                runtime_s=time.monotonic() - implementation_started,
                payload=implementation,
            )

        def replay(binding: ArtifactBinding, counterexample: Mapping):
            name = (
                "source" if binding.sha256 == source_artifact_sha256
                else "candidate"
            )
            future = asyncio.run_coroutine_threadsafe(
                replay_product_sum_counterexample(
                    source_dcp=input_dcp,
                    artifact=binding,
                    counterexample=counterexample,
                    report_path=(
                        Path(mcp.run_dir)
                        / f"operator_rewrite_counterexample_{name}.json"
                    ),
                    deadline_monotonic=validation_deadline,
                ),
                loop,
            )
            return future.result()

        async def strict_validation(
            execution: SpeculativeExecutionResult,
        ) -> SkillResult:
            authorization = authorization_holder.get("authorization")
            if authorization is None:
                raise RuntimeError("REGARITH_EXACT_PROOF_JOIN_REQUIRED")
            if not isinstance(execution.payload, dict):
                raise RuntimeError("REGARITH_IMPLEMENTATION_REQUIRED")
            return await self._strict_validate_and_build_product_sum_result(
                mcp=mcp,
                target=target,
                params=params,
                before_wns=before_wns,
                started=started,
                input_dcp=input_dcp,
                source_sha256=source_artifact_sha256,
                fingerprint=fingerprint,
                guarded=guarded,
                authorization=authorization,
                implementation=execution.payload,
            )

        async def shadow_prefetch(
            execution: SpeculativeExecutionResult,
        ) -> ShadowPrefetchResult:
            """Prefetch only read-only next-seed facts while strict validation runs."""
            prefetched_started = time.monotonic()
            seed = execution.candidate
            candidate_path = Path(seed.path).resolve()
            if sha256_file(candidate_path) != seed.sha256:
                raise RuntimeError("A5_SHADOW_PREFETCH_SEED_IDENTITY_CHANGED")
            destination = action_run_dir / (
                f"shadow_product_sum_prefetch_{seed.sha256[:12]}"
            )
            recovered = None
            try:
                if recovered is None:
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": str(candidate_path), "timeout": 600},
                        timeout=700.0,
                    )
                    paths = _critical_paths(await mcp.call_vivado(
                        "extract_critical_path_cells",
                        {
                            "num_paths": 16,
                            "clock": str(params.get("_contest_clock") or ""),
                        },
                        timeout=600.0,
                    ))
                    recovered = await recover_cold_product_sum_families(
                        mcp,
                        input_dcp=candidate_path,
                        source_artifact_sha256=seed.sha256,
                        run_id=str(params.get("_run_id") or ""),
                        critical_paths=paths,
                        output_dir=destination,
                        max_families=4,
                        max_candidates=4,
                        preflight_budget_s=300.0,
                        family_deadline_s=240.0,
                    )
            except Exception as exc:  # fail closed but publish the next-seed menu
                destination.mkdir(parents=True, exist_ok=True)
                error_code = str(
                    getattr(exc, "code", "") or "SHADOW_ACTION_MENU_BUILD_FAILED"
                ).upper()
                error_code = re.sub(r"[^A-Z0-9_]+", "_", error_code).strip("_")
                payload = {
                    "schema_version": "shadow-degraded-action-menu-v1",
                    "seed_artifact_sha256": seed.sha256,
                    "error_code": error_code,
                    "error_detail": str(exc)[:500],
                    "read_only": True,
                    "mutation_eligible": False,
                    "provider_entry": "recover_cold_product_sum_families",
                }
                payload_digest = object_digest(payload)
                artifact = destination / "degraded_action_menu_prefetch.json"
                artifact.write_text(
                    json.dumps(
                        {**payload, "payload_digest": payload_digest},
                        indent=2,
                        sort_keys=True,
                    ) + "\n",
                    encoding="utf-8",
                )
                action_menu = build_degraded_action_menu(
                    seed_candidate_id=execution.candidate_id,
                    seed_artifact_sha256=seed.sha256,
                    error_code=error_code,
                    error_detail=str(exc),
                    top_k=4,
                )
                return ShadowPrefetchResult(
                    seed=seed,
                    artifact_path=artifact,
                    artifact_sha256=sha256_file(artifact),
                    payload_digest=payload_digest,
                    discard_root=destination,
                    runtime_s=time.monotonic() - prefetched_started,
                    action_menu=action_menu,
                )
            artifact = destination / "cold_recovery.json"
            if (
                recovered.get("read_only") is not True
                or recovered.get("mutation_eligible") is not False
                or recovered.get("design_sha256") != seed.sha256
                or not artifact.is_file()
            ):
                raise RuntimeError("A5_SHADOW_PREFETCH_AUTHORITY_INVALID")
            proof_bindings = {
                str(item.get("family_fingerprint") or ""): dict(
                    item.get("preflight_receipt") or {}
                )
                for item in recovered.get("candidates", [])
                if isinstance(item, dict)
                and isinstance(item.get("preflight_receipt"), dict)
            }
            action_menu = build_action_menu(
                seed_candidate_id=execution.candidate_id,
                seed_artifact_sha256=seed.sha256,
                registered_arithmetic_packet={
                    "source_artifact_sha256": seed.sha256,
                    "result_digest": str(recovered["result_digest"]),
                    "normalized_siblings": [
                        dict(item["screened_family"])
                        for item in recovered.get("candidates", [])
                        if isinstance(item, dict)
                        and isinstance(item.get("screened_family"), dict)
                    ] + [
                        dict(item)
                        for item in recovered.get("unavailable_families", [])
                        if isinstance(item, dict)
                    ],
                },
                proof_bindings=proof_bindings,
                top_k=4,
            )
            return ShadowPrefetchResult(
                seed=seed,
                artifact_path=artifact,
                artifact_sha256=sha256_file(artifact),
                payload_digest=str(recovered["result_digest"]),
                discard_root=destination,
                runtime_s=time.monotonic() - prefetched_started,
                action_menu=action_menu,
            )

        return ConcurrentProofAction(
            request=request,
            cheap_replay_guard=cheap_replay_guard,
            exact_proof=exact_proof,
            speculative_execution=speculative_execution,
            replay_source=replay,
            replay_candidate=replay,
            strict_validation=strict_validation,
            shadow_prefetch=shadow_prefetch,
        )

    async def _execute_recurrence(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        output = Path(mcp.run_dir) / "operator_rewrite_recurrence.dcp"
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(
                float(params.get("_candidate_commit_reserve_s", 0.0)),
                float(params.get("min_candidate_remaining_s", 0.0)),
            )
            if deadline is not None and float(deadline) - time.monotonic() <= reserve:
                raise TimeoutError("operator_rewrite candidate reserve is exhausted")
            route_timeout = float(params.get("route_timeout_s", 1800.0))
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            facts = resolve_recurrence_family_facts(
                mcp, artifact_sha256=str(source_sha256)
            )
            if facts is None:
                facts = _payload(await mcp.call_rw(
                    "recover_recurrence_family_contract",
                    {"max_boundaries": 32},
                    timeout=600.0,
                ))
            if (
                facts.get("fresh_rediscovery") is not True
                or facts.get("design_sha256") != source_sha256
                or facts.get("proof_status")
                != "family_rewrite_facts_extracted_unproved"
                or facts.get("mutation_eligible") is not False
                or not isinstance(facts.get("boundary_count"), int)
                or facts["boundary_count"] < 2
            ):
                raise RuntimeError("fresh recurrence family has invalid authority")
            required_family = params.get("family_fingerprint")
            recovered_family = facts.get("family_contract_sha256")
            if (
                required_family is not None
                and required_family != recovered_family
            ):
                raise RuntimeError(
                    "fresh recurrence family differs from the selected "
                    "current-seed action-menu binding"
                )
            family_id = facts.get("family_id")
            attempts.append({
                "family_id": family_id,
                "boundary_count": facts["boundary_count"],
                "status": "family_recovered",
            })
            plans = build_recurrence_family_action_plans(
                facts, max_segment_candidates=3
            )
            if not plans:
                raise RuntimeError("no proved recurrence family segmentation was derived")
            # Segment count is what shortens the recurrence path, so it is what
            # raises Fmax; taking the fewest SRL layers optimizes clock-leaf
            # capacity instead.  Probe one segmentation deeper than the minimum
            # alongside it and prefer the deeper one where the measured slots
            # allocate.
            #
            # `measure_connected_recurrence_slots` accepts at most four
            # addresses.  Two plans fit exactly; a family whose depths do not
            # fit that budget keeps the original single-plan behaviour rather
            # than being refused at input validation.
            #
            # Probe the deeper segmentation, but do not prefer it.  A deeper
            # cut shortens each segment and lengthens the routes between them:
            # on fir_systolic the three-segment plan measured 333.222 MHz
            # against 373.552 MHz for the two-segment plan on the same family.
            # Rank by the family's own order and keep the deeper plan as the
            # fallback when the first cannot allocate.
            ranked_plans = [plans[0]] if len(plans) < 2 else [plans[0], plans[1]]
            probe_addresses = sorted({
                int(depth) - 1
                for item in ranked_plans
                for depth in item["plan"].srl_segment_depths
            })
            if len(probe_addresses) > 4:
                ranked_plans = [plans[0]]
                probe_addresses = sorted({
                    int(depth) - 1
                    for depth in plans[0]["plan"].srl_segment_depths
                })
            action_candidate = ranked_plans[0]
            plan = action_candidate["plan"]
            proposals = _payload(await mcp.call_rw(
                "propose_recurrence_family_placement_candidates",
                {
                    "max_srl_sites": 1024,
                    "max_ce_clone_sites": 16,
                },
                timeout=600.0,
            ))
            if (
                proposals.get("fresh_rediscovery") is not True
                or proposals.get("source_dcp_sha256") != source_sha256
                or proposals.get("family_contract_sha256")
                != facts.get("family_contract_sha256")
            ):
                raise RuntimeError("recurrence family placement proposal is stale")
            proposals_by_region = {
                item["region_sha256"]: item["proposal"]
                for item in proposals.get("proposals", [])
            }
            oracle_slots = []
            for boundary in facts["boundaries"]:
                region = boundary["region_sha256"]
                proposal = proposals_by_region.get(region)
                if not isinstance(proposal, dict):
                    raise RuntimeError("recurrence family proposal coverage is incomplete")
                oracle = _payload(await mcp.call_vivado(
                    "measure_connected_recurrence_slots",
                    {
                        "dcp_path": str(input_dcp),
                        "source_dcp_sha256": source_sha256,
                        "region_sha256": region,
                        "data_pin": f"{boundary['relay_bits'][0]['ff']['cell']}/D",
                        "clock_pin": boundary["control_equivalence_groups"]["C"]["members"][0]["pin"],
                        "ce_source_pin": boundary["control_equivalence_groups"]["CE"]["members"][0]["pin"],
                        "addresses": probe_addresses,
                        "incumbent_clock_regions": proposal["incumbent_clock_regions"],
                        "srl_candidates": proposal["srl_candidates"],
                        "ce_clone_candidates": proposal["ce_clone_candidates"],
                        "max_place_attempts": len(proposal["srl_candidates"])
                        + len(proposal["ce_clone_candidates"]),
                        "timeout": 600.0,
                    },
                    timeout=1800.0,
                ))
                if (
                    oracle.get("read_only_after_restore") is not True
                    or oracle.get("cleanup") != {
                        "source_checkpoint_reopened": True,
                        "temporary_namespace_discarded": True,
                    }
                ):
                    raise RuntimeError("recurrence family oracle did not restore the seed")
                oracle_slots.append({
                    "region_sha256": region,
                    "srl_slots": list(oracle.get("srl_slots") or []),
                    "ce_clone_slots": list(oracle.get("ce_clone_slots") or []),
                })
            placement = None
            allocation_refusals: list[str] = []
            for candidate in ranked_plans:
                try:
                    placement = allocate_recurrence_family_srl_placements(
                        facts, candidate, oracle_slots=oracle_slots,
                    )
                except Exception as exc:  # noqa: BLE001 - try the shallower plan
                    allocation_refusals.append(
                        "%d segments: %s"
                        % (len(candidate["plan"].srl_segment_depths), exc)
                    )
                    continue
                action_candidate = candidate
                plan = candidate["plan"]
                break
            if placement is None:
                raise RuntimeError(
                    "no recurrence segmentation allocated against measured slots; "
                    + "; ".join(allocation_refusals[:2])
                )
            json_candidate = dict(action_candidate)
            json_candidate["plan"] = asdict(plan)
            attestation = _payload(await mcp.call_vivado(
                "apply_proved_recurrence_family_eco",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "family_facts": facts,
                    "candidate": json_candidate,
                    "placement": placement,
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                },
                timeout=route_timeout + 1300.0,
            ))
            if not output.is_file():
                raise RuntimeError("recurrence family ECO output checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if attestation.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("recurrence family attestation candidate mismatch")
            validation_deadline = (
                float(deadline) - reserve if deadline is not None else None
            )
            strict_report = await run_strict_validation_worker(
                input_dcp,
                output,
                report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=validation_deadline,
            )
            proof = build_operator_candidate_proof(
                target,
                action_candidate["authorization"],
                candidate_sha256,
                strict_report,
                emitted_topology_attestation=attestation,
            )
            # Import locally so the standalone lineage module does not depend on
            # FDAgents.skills package initialization order.
            from ..recurrence_lineage import (
                RECURRENCE_LINEAGE_KEY,
                write_recurrence_lineage_bundle,
            )

            lineage = write_recurrence_lineage_bundle(
                Path(mcp.run_dir),
                source_sha256=source_sha256,
                family_facts=facts,
                candidate=json_candidate,
                placement=placement,
                proof=proof,
            )
            evidence = {
                "version": 1,
                "family_id": family_id,
                "boundary_count": facts["boundary_count"],
                "state_width": int(plan.state_width),
                "transport_valid_transactions": int(
                    plan.transport_valid_transactions
                ),
                "srl_segment_depths": list(plan.srl_segment_depths),
                "placement_manifest_sha256": placement["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "strict_validation_precheck_vectors": (
                    MIN_POST_EMISSION_PRECHECK_VECTORS
                ),
                RECURRENCE_LINEAGE_KEY: lineage,
                "acceptance_authority": "central measurement and final strict gate",
            }
            attempts[-1]["status"] = "proved_family_candidate"
            skill_output = SkillOutput(
                output,
                (
                    "operator_rewrite recurrence family "
                    f"members={facts['boundary_count']} width={plan.state_width} "
                    f"delay={plan.transport_valid_transactions}"
                )[:120],
                evidence,
                proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=output,
                summary="operator_rewrite recurrence family candidate=1 proof=operator-rewrite-v1",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            output.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )


    async def _execute_fixed_point(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        output = Path(mcp.run_dir) / "operator_rewrite_fixed_point.dcp"
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(
                float(params.get("_candidate_commit_reserve_s", 0.0)),
                float(params.get("min_candidate_remaining_s", 0.0)),
            )
            if deadline is not None and float(deadline) - time.monotonic() <= reserve:
                raise TimeoutError("operator_rewrite candidate reserve is exhausted")
            route_timeout = float(params.get("route_timeout_s", 1800.0))
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            facts_set = _payload(await mcp.call_rw(
                "recover_fixed_point_contract",
                {"max_candidates": 8},
                timeout=600.0,
            ))
            if not fixed_point_contract_set_has_executor_authority(
                facts_set, expected_source_sha256=source_sha256
            ):
                raise RuntimeError("fresh fixed-point contract set has invalid authority")
            candidates = facts_set["candidates"]
            candidate_count = facts_set["candidate_count"]
            attempts.append({
                "candidate_count": candidate_count,
                "status": "contract_set_recovered",
            })
            # Deterministic selection: the first freshly recovered candidate
            # whose mechanical plan authorizes under exact proof replay wins.
            facts = None
            action_candidate = None
            required_fingerprint = str(
                params.get("_required_family_fingerprint") or ""
            )
            for item in candidates:
                try:
                    if (
                        required_fingerprint
                        and adapt_fixed_point_contract_facts(item).plan_digest()
                        != required_fingerprint
                    ):
                        continue
                    plans = build_fixed_point_action_plans(item)
                except OperatorProofError:
                    continue
                if plans:
                    facts = item
                    action_candidate = plans[0]
                    break
            if facts is None or action_candidate is None:
                raise RuntimeError(
                    "no fixed-point candidate authorized a proved rewrite plan"
                )

            plan = action_candidate["plan"]
            candidate_id = facts["candidate_id"]
            boundary = facts.get("boundary")
            if (
                not isinstance(boundary, dict)
                or not isinstance(boundary.get("clock_pin"), str)
                or not boundary["clock_pin"]
                or not isinstance(boundary.get("cone_cells"), list)
            ):
                raise RuntimeError("fixed-point boundary clock contract is unavailable")
            attempts[-1]["candidate_id"] = candidate_id
            proposals = _payload(await mcp.call_rw(
                "propose_fixed_point_placement_candidates",
                {"region_sha256": facts["region_sha256"], "max_sites": 64},
                timeout=600.0,
            ))
            proposed = proposals.get("slots")
            if (
                proposals.get("fresh_rediscovery") is not True
                or proposals.get("source_dcp_sha256") != source_sha256
                or proposals.get("region_sha256") != facts["region_sha256"]
                or not isinstance(proposed, list)
                or not proposed
            ):
                raise RuntimeError("fixed-point placement proposal is stale")
            oracle = _payload(await mcp.call_vivado(
                "measure_connected_dsp_slots",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "region_sha256": facts["region_sha256"],
                    "clock_pin": boundary["clock_pin"],
                    "candidate_sites": [
                        {
                            "site": item["site"],
                            "distance": int(item["distance"]),
                        }
                        for item in proposed[:32]
                    ],
                    "timeout": 600.0,
                },
                timeout=1800.0,
            ))
            if (
                oracle.get("read_only_after_restore") is not True
                or oracle.get("cleanup") != {
                    "source_checkpoint_reopened": True,
                    "temporary_namespace_discarded": True,
                }
            ):
                raise RuntimeError("fixed-point oracle did not restore the seed")
            measured_slots = [
                dict(slot)
                for slot in (oracle.get("slots") or [])
                if isinstance(slot, dict) and slot.get("clock_route_ok") is True
            ]
            placement = allocate_fixed_point_dsp_placement(
                facts,
                action_candidate,
                oracle_slots=measured_slots,
            )
            json_candidate = dict(action_candidate)
            json_candidate["plan"] = asdict(plan)
            attestation = _payload(await mcp.call_vivado(
                "apply_proved_fixed_point_eco",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "facts": facts,
                    "candidate": json_candidate,
                    "placement": placement,
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                },
                timeout=route_timeout + 1300.0,
            ))
            if not output.is_file():
                raise RuntimeError("fixed-point ECO output checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if attestation.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("fixed-point attestation candidate mismatch")
            validation_deadline = (
                float(deadline) - reserve if deadline is not None else None
            )
            strict_report = await run_strict_validation_worker(
                input_dcp,
                output,
                report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=validation_deadline,
            )
            proof = build_operator_candidate_proof(
                target,
                action_candidate["authorization"],
                candidate_sha256,
                strict_report,
                emitted_topology_attestation=attestation,
            )
            spec = facts["spec"]
            evidence = {
                "version": 1,
                "candidate_id": candidate_id,
                "cone_cell_count": len(boundary["cone_cells"]),
                "data_width": int(spec["data_width"]),
                "term_width": int(spec["term_width"]),
                "accumulator_width": int(spec["accumulator_width"]),
                "product_latency": int(spec["product_latency"]),
                "placement_manifest_sha256": placement["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "strict_validation_precheck_vectors": (
                    MIN_POST_EMISSION_PRECHECK_VECTORS
                ),
                "acceptance_authority": "central measurement and final strict gate",
            }
            attempts[-1]["status"] = "proved_fixed_point_candidate"
            skill_output = SkillOutput(
                output,
                (
                    "operator_rewrite fixed point "
                    f"cone={len(boundary['cone_cells'])} "
                    f"data={int(spec['data_width'])} term={int(spec['term_width'])} "
                    f"acc={int(spec['accumulator_width'])} "
                    f"latency={int(spec['product_latency'])}"
                )[:120],
                evidence,
                proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=output,
                summary="operator_rewrite fixed point candidate=1 proof=operator-rewrite-v1",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            output.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )


    async def _recover_registered_arithmetic_context(
        self,
        mcp,
        *,
        input_dcp: Path,
        source_sha256: str,
        fingerprint: str,
        contest_clock: str,
        run_id: str,
    ) -> dict:
        """Recover and bind one exact registered family without mutating it."""
        scoped_run_dir = Path(mcp.run_dir).resolve()
        preflight_name = f"cold_product_sum_preflight_{source_sha256[:12]}"
        preflight_candidates = [
            scoped_run_dir / preflight_name / "cold_recovery.json"
        ]
        # Action-scoped run directories live at run/actions/<action>. A5
        # publishes only a hash-bound read-only preflight into the owning run;
        # exact receipt replay below remains the sole reuse authority.
        if scoped_run_dir.parent.name == "actions":
            preflight_candidates.append(
                scoped_run_dir.parent.parent / preflight_name / "cold_recovery.json"
            )
        preflight_artifact = next(
            (path for path in preflight_candidates if path.is_file()),
            preflight_candidates[0],
        )
        if preflight_artifact.is_file() and not preflight_artifact.is_symlink():
            try:
                recovered = load_product_sum_preflight_candidate(
                    preflight_artifact,
                    expected_family_fingerprint=fingerprint,
                    expected_source_sha256=source_sha256,
                    expected_run_id=run_id,
                )
            except RuntimeError as exc:
                if str(exc) != "REGARITH_PREFLIGHT_FAMILY_NOT_FOUND":
                    raise
            else:
                selected = dict(recovered["screened_family"])
                if selected.get("family_fingerprint") != fingerprint:
                    raise RuntimeError("STALE_REGISTERED_ARITHMETIC_FAMILY")
                return {
                    "selected": selected,
                    "cold": {
                        "kind": "cold_registered_product_sum_facts_set",
                        "design_sha256": source_sha256,
                        "run_id": run_id,
                        "candidates": [recovered],
                        "execution_reused_preflight": True,
                        "preflight_artifact": str(preflight_artifact),
                    },
                    "screened": [selected],
                    "critical_paths": [],
                    "execution_reused_preflight": True,
                }
        await mcp.call_vivado(
            "open_checkpoint",
            {"dcp_path": str(input_dcp), "timeout": 600},
            timeout=700.0,
        )
        paths = _critical_paths(await mcp.call_vivado(
            "extract_critical_path_cells",
            {"num_paths": 16, "clock": contest_clock},
            timeout=600.0,
        ))
        await mcp.call_rw(
            "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
        )
        await mcp.call_rw(
            "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
        )
        fixed_set = _payload(await mcp.call_rw(
            "recover_fixed_point_contract", {"max_candidates": 8}, timeout=600.0
        ))
        try:
            product_sum_set = _payload(await mcp.call_rw(
                "recover_registered_product_sum_contract",
                # The top three exact siblings are sufficient to expose the
                # conventional product-sum family or its typed residue.
                {"max_candidates": 3},
                timeout=600.0,
            ))
        except Exception as exc:  # preserve fixed-point execution availability
            product_sum_set = {
                "status": "rejected",
                "design_sha256": source_sha256,
                "candidates": [],
                "typed_rejection": str(exc),
            }
        screened = screen_recovered_registered_arithmetic(
            fixed_point_facts_set=fixed_set,
            product_sum_facts_set=product_sum_set,
            critical_paths=paths,
            source_artifact_sha256=source_sha256,
        )
        cold = {"candidates": []}
        matches = [
            item for item in screened
            if item.get("family_fingerprint") == fingerprint
        ]
        if not matches:
            cold = await recover_cold_product_sum_families(
                mcp,
                input_dcp=input_dcp,
                source_artifact_sha256=source_sha256,
                run_id=run_id,
                critical_paths=paths,
                output_dir=Path(mcp.run_dir) / "cold_product_sum_execution",
                max_candidates=2,
            )
            screened.extend(
                dict(item["screened_family"])
                for item in cold.get("candidates", [])
                if isinstance(item, dict)
                and isinstance(item.get("screened_family"), dict)
            )
            matches = [
                item for item in screened
                if item.get("family_fingerprint") == fingerprint
            ]
        if len(matches) != 1:
            raise RuntimeError(
                "STALE_REGISTERED_ARITHMETIC_FAMILY: nominated family did not "
                "reproduce on the exact execution seed"
            )
        return {
            "selected": matches[0],
            "cold": cold,
            "screened": screened,
            "critical_paths": paths,
        }

    async def _execute_registered_arithmetic(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Re-screen one nominated exact family, then use its validated backend."""
        fingerprint = str(params.get("family_fingerprint") or "")
        if len(fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in fingerprint
        ):
            return SkillResult.failure(
                before_wns,
                "registered arithmetic requires one exact family_fingerprint",
                failed_dcp,
            )
        input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
        if input_dcp.is_symlink() or not input_dcp.is_file():
            return SkillResult.failure(
                before_wns,
                "registered arithmetic exact input DCP is unavailable",
                failed_dcp,
            )
        source_sha256 = sha256_file(input_dcp)
        try:
            context = await self._recover_registered_arithmetic_context(
                mcp,
                input_dcp=input_dcp,
                source_sha256=source_sha256,
                fingerprint=fingerprint,
                contest_clock=str(params.get("_contest_clock") or ""),
                run_id=str(params.get("_run_id") or ""),
            )
            selected = context["selected"]
            cold = context["cold"]
            if selected.get("nomination_eligible") is not True:
                rejection = selected.get("typed_rejection") or {
                    "reason_code": "REGISTERED_ARITHMETIC_GATE_REJECTED",
                    "message": ",".join(selected.get("nomination_blockers") or []),
                }
                return SkillResult.failure(
                    before_wns,
                    str(rejection.get("message") or rejection.get("reason_code")),
                    failed_dcp,
                    details={"typed_rejection": rejection, "template": target},
                )
            if selected["family_kind"] == "fixed_product":
                delegated = dict(params)
                delegated["_required_family_fingerprint"] = fingerprint
                result = await self._execute_fixed_point(
                    mcp,
                    "fixed_point_dsp_rebuild_v1",
                    delegated,
                    before_wns,
                    started,
                    failed_dcp,
                )
                if isinstance(result.details, dict):
                    result.details["canonical_target"] = target
                    result.details["family_fingerprint"] = fingerprint
                    result.details["proof_template_id"] = selected[
                        "proof_template_id"
                    ]
                return result
            if selected["family_kind"] == "product_sum":
                cold_matches = [
                    item for item in cold.get("candidates", [])
                    if item.get("family_fingerprint") == fingerprint
                ]
                if len(cold_matches) != 1:
                    raise RuntimeError(
                        "REGARITH_COMPLETE_FAMILY_RECOVERY_REQUIRED: nominated "
                        "product sum is not a cold complete-family bundle"
                    )
                recovered = cold_matches[0]
                emission = recovered["emission"]
                if emission.get("result_digest") != fingerprint:
                    raise RuntimeError("STALE_REGISTERED_ARITHMETIC_FAMILY")
                guarded = guard_recovered_product_sum(
                    recovered,
                    expected_family_fingerprint=fingerprint,
                    expected_source_sha256=source_sha256,
                    expected_run_id=str(params.get("_run_id") or ""),
                )
                recovered_plan_digest = guarded["recovered_plan_digest"]
                # Pure CPU proof is intentionally a separate provider stage.
                # G3 may schedule it concurrently with implementation; this
                # sequential Agent path joins it before candidate publication.
                authorization = prove_guarded_product_sum(
                    guarded, run_id=str(params.get("_run_id") or "")
                )
                route_timeout = float(params.get("route_timeout_s", 1800.0))
                implementation = await implement_guarded_product_sum(
                    mcp,
                    guarded,
                    input_dcp=input_dcp,
                    route_timeout_s=route_timeout,
                    run_id=str(params.get("_run_id") or ""),
                    contest_clock=str(params.get("_contest_clock") or ""),
                    run_source_sha256=str(params.get("_run_source_sha256") or ""),
                )
                result = await self._strict_validate_and_build_product_sum_result(
                    mcp=mcp,
                    target=target,
                    params=params,
                    before_wns=before_wns,
                    started=started,
                    input_dcp=input_dcp,
                    source_sha256=source_sha256,
                    fingerprint=fingerprint,
                    guarded=guarded,
                    authorization=authorization,
                    implementation=implementation,
                )
                if isinstance(result.details, dict):
                    result.details["execution_reused_preflight"] = bool(
                        context.get("execution_reused_preflight")
                    )
                    result.details["preflight_receipt_digest"] = str(
                        (recovered.get("preflight_receipt") or {}).get(
                            "receipt_digest"
                        )
                        or ""
                    )
                return result
            raise RuntimeError("REGISTERED_ARITHMETIC_FAMILY_KIND_UNSUPPORTED")
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
            code = (
                reason.split(":", 1)[0]
                if ":" in reason
                else "REGISTERED_ARITHMETIC_EXECUTION_REJECTED"
            )
            return SkillResult.failure(
                before_wns,
                reason,
                failed_dcp,
                details={
                    "typed_rejection": {"reason_code": code, "message": reason},
                    "template": target,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _strict_validate_and_build_product_sum_result(
        self,
        *,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        input_dcp: Path,
        source_sha256: str,
        fingerprint: str,
        guarded: Mapping,
        authorization: dict,
        implementation: dict,
    ) -> SkillResult:
        """Join exact proof, routed topology, and strict real-netlist validation."""
        emission = guarded["emission"]
        recovered_plan_digest = guarded["recovered_plan_digest"]
        output = Path(implementation["candidate_path"])
        candidate_sha256 = implementation["candidate_sha256"]
        emitted_topology_digest = implementation["emitted_topology_digest"]
        authorized = implementation["authorized_emission"]
        checks = dict(implementation["checks"])
        attestation = {
            "kind": "operator_rewrite_emitted_topology_attestation",
            "version": 1,
            "status": "proved",
            "authority": "candidate_measurement_only",
            "template": target,
            "source": {
                "dcp_sha256": source_sha256,
                "region_sha256": emission["bundle_digest"],
            },
            "candidate": {"dcp_sha256": candidate_sha256},
            "authorization_id": authorization["certificate_id"],
            "placement_manifest_sha256": emission[
                "placement_manifest"
            ]["manifest_digest"],
            "topology_manifest_sha256": emitted_topology_digest,
            "checks": checks,
        }
        attestation["attestation_id"] = object_digest(attestation)
        deadline = params.get("_execution_deadline_monotonic")
        reserve = max(
            float(params.get("_candidate_commit_reserve_s", 0.0)),
            float(params.get("min_candidate_remaining_s", 0.0)),
        )
        validation_deadline = (
            float(deadline) - reserve if deadline is not None else None
        )
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        strict_report = await strict_validate_implemented_product_sum(
            source_dcp=input_dcp,
            implementation=implementation,
            report_path=report_path,
            deadline_monotonic=validation_deadline,
        )
        proof = build_operator_candidate_proof(
            target,
            authorization,
            candidate_sha256,
            strict_report,
            emitted_topology_attestation=attestation,
        )
        skill_output = SkillOutput(
            output,
            (
                "registered product sum "
                f"instances={emission['instance_count']} "
                f"modules={emission['module_count']}"
            )[:120],
            {
                "version": 1,
                "family_fingerprint": fingerprint,
                "bundle_digest": emission["bundle_digest"],
                "recovered_plan_digest": recovered_plan_digest,
                "emitted_topology_digest": emitted_topology_digest,
                "instance_count": emission["instance_count"],
                "module_count": emission["module_count"],
                "emission_gate_digest": implementation["emission_gate_digest"],
                "emission_mode": implementation["emission_mode"],
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "acceptance_authority": "central measurement and final strict gate",
            },
            proof,
        )
        return SkillResult(
            success=True,
            before_wns=before_wns,
            after_wns=before_wns,
            delta_wns=0.0,
            is_legal=True,
            output_dcp=output,
            summary=(
                "operator_rewrite registered product-sum candidate=1 "
                "proof=operator-rewrite-v1"
            ),
            details={
                "proof_version": PROOF_VERSION,
                "template": target,
                "family_fingerprint": fingerprint,
                "recovered_plan_digest": recovered_plan_digest,
                "emitted_topology_digest": emitted_topology_digest,
                "followup_discovery_artifact": (
                    "post_measurement_routed_dcp_read_only"
                ),
                "runtime_s": round(time.monotonic() - started, 6),
                "final_submission_authority": False,
            },
            candidates=(skill_output,),
        )

    async def _execute_constructive_accumulator(
        self, mcp, target: str, params: dict, before_wns: float,
        clock_period: Optional[float], started: float, failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        action_dir = Path(mcp.run_dir)
        output_dir = action_dir / "constructive_candidate"
        output = output_dir / "candidate.dcp"
        report_path = action_dir / "constructive_strict_validation.json"
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("CACC_INPUT_DCP_UNAVAILABLE")
            source_sha256 = sha256_file(input_dcp)
            if clock_period is None:
                raise ValueError("CACC_CLOCK_PERIOD_UNAVAILABLE")
            clock = str(params.get("_contest_clock") or "")
            parent_report = await capture_live_timing_report(mcp, clock=clock, top_n=300)
            stems = constructive_endpoint_stems(parent_report, limit=300)
            if not stems:
                raise ValueError("CACC_ANONYMOUS_CLOSED_FAMILY_ABSENT")
            attempts.append({"anonymous_ranked_stem_count": len(stems), "status": "timing_candidates_recovered"})
            extract_script = Path(__file__).resolve().parents[2] / "scripts" / "extract_register_cone.tcl"
            cone_families: dict[str, list[tuple[Path, Path, dict]]] = {}
            for ordinal, stem in enumerate(stems[:12]):
                cone_dir = action_dir / "constructive_recovery" / f"cone_{ordinal}"
                cone_dir.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await _run_vivado_stage(
                        script=extract_script,
                        args=[str(input_dcp), stem, str(cone_dir)],
                        cwd=action_dir,
                        log_path=action_dir / f"constructive_extract_{ordinal}.log",
                        timeout_s=600.0,
                    )
                    graph = ConeGraph.load(cone_dir)
                    validate_constructive_shape(graph)
                    extraction = await asyncio.to_thread(
                        extract_constructive_accumulator, cone_dir,
                        random_samples=10_000,
                        max_bdd_nodes=CONSTRUCTIVE_ACTION_MAX_BDD_NODES,
                    )
                    expression = extraction["canonical_comparison"]["actual_expression_sha256"]
                    if extraction.get("mutation_eligible") is not True or extraction.get("status") != "proved":
                        raise ValueError("CACC_CANONICAL_EXPRESSION_NOT_PROVED")
                    extraction_path = cone_dir / "fresh_constructive_proof.json"
                    extraction_path.write_text(json.dumps({
                        "schema_version": extraction["schema_version"],
                        "status": "proved", "sibling_count": 1,
                        "sibling_expression_agreement": True,
                        "siblings": [extraction], "mutation_eligible": True,
                    }, indent=2, sort_keys=True) + "\n")
                    certificate = await asyncio.to_thread(
                        build_constructive_lowering, cone_dir, extraction_path
                    )
                    certificate_path = cone_dir / "lowering_certificate.json"
                    certificate_path.write_text(json.dumps(certificate, indent=2, sort_keys=True) + "\n")
                    family_key = object_digest({
                        "expression_sha256": expression,
                        "weighted_sum": certificate["weighted_sum"],
                        "compressor_spec": certificate["compressor_spec"],
                    })
                    cone_families.setdefault(family_key, []).append(
                        (cone_dir, certificate_path, certificate)
                    )
                except Exception as exc:
                    attempts.append({
                        "anonymous_candidate_ordinal": ordinal,
                        "status": "typed_shape_rejection",
                        "reason_code": str(exc).split(":", 1)[0][:120],
                    })
            if not cone_families:
                typed = next(
                    (
                        row["reason_code"] for row in attempts
                        if row.get("status") == "typed_shape_rejection"
                        and str(row.get("reason_code", "")).startswith("CACC_")
                    ),
                    "CACC_NO_PROVED_BOUNDED_FAMILY",
                )
                raise ValueError(typed)
            _family_key, cone_rows = min(
                cone_families.items(),
                key=lambda item: (-len(item[1]), item[0]),
            )
            expression = cone_rows[0][2]["constructive_proof"]["expression_sha256"]
            authorization = build_constructive_accumulator_authorization(
                source_dcp_sha256=source_sha256,
                expression_sha256=expression,
                certificates=[row[2] for row in cone_rows],
            )
            attempts.append({
                "family_size": len(cone_rows),
                "expression_sha256": expression,
                "status": "fresh_family_proved",
            })
            from ..constructive_accumulator_eco import build_constructive_accumulator_eco
            specs = [(f"family{index}", row[0], row[1]) for index, row in enumerate(cone_rows)]
            tcl, topology = build_constructive_accumulator_eco(input_dcp, output_dir, specs)
            eco_script = action_dir / "constructive_apply.tcl"
            eco_script.write_text(tcl)
            route_timeout = float(params.get("route_timeout_s", 3600.0))
            await _run_vivado_stage(
                script=eco_script, args=[], cwd=action_dir,
                log_path=action_dir / "constructive_apply.log",
                timeout_s=route_timeout + 1200.0,
            )
            if not output.is_file():
                raise RuntimeError("CACC_CANDIDATE_DCP_MISSING")
            candidate_sha256 = sha256_file(output)
            audit_script = action_dir / "constructive_reopen_audit.tcl"
            audit_script.write_text(render_constructive_audit_tcl(
                candidate=output, source=input_dcp, source_sha256=source_sha256,
                topology=topology,
            ))
            await _run_vivado_stage(
                script=audit_script, args=[], cwd=action_dir,
                log_path=action_dir / "constructive_reopen_audit.log", timeout_s=1200.0,
            )
            attestation = _metaloop_attestation(
                template=target, source_sha256=source_sha256,
                region_sha256=authorization["source"]["region_sha256"],
                candidate_sha256=candidate_sha256,
                authorization_id=authorization["certificate_id"],
                topology_sha256=topology["manifest_sha256"],
                checks={
                    "fresh_seed_bound_recovery": True, "canonical_expression_proved": True,
                    "old_cone_fully_removed": True, "target_driver_closure_replayed": True,
                    "exact_namespace_inventory": True, "candidate_checkpoint_reopened": True,
                    "source_checkpoint_reopened": True, "relevant_nets_fully_routed": True,
                },
            )
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(float(params.get("_candidate_commit_reserve_s", 0.0)), float(params.get("min_candidate_remaining_s", 0.0)))
            strict_report = await run_strict_validation_worker(
                input_dcp, output, report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=(float(deadline) - reserve if deadline is not None else None),
            )
            proof = build_operator_candidate_proof(
                target, authorization, candidate_sha256, strict_report,
                emitted_topology_attestation=attestation,
            )
            parent_profile = build_measured_residual_profile(
                parent_report, artifact_sha256=source_sha256,
                global_wns_ns=before_wns,
                fmax_mhz=1000.0 / (float(clock_period) - float(before_wns)),
                top_n=300, source_label="live_vivado:constructive_parent_top300",
            )
            evidence = {
                "version": 1, "family_kind": "anonymous_closed_lut_carry_accumulator_family",
                "family_size": len(cone_rows), "canonical_expression_sha256": expression,
                "topology_manifest_sha256": topology["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "candidate_dcp_sha256": candidate_sha256,
                "enabling_candidate": {
                    "targeted_families": ["constructive_accumulator"],
                    "parent_residual_profile": parent_profile,
                    "candidate_residual_profile": None,
                    "profile_request": {"top_n": 300},
                    "strict_validation_passed": True,
                },
            }
            return SkillResult(
                success=True, before_wns=before_wns, after_wns=before_wns,
                delta_wns=0.0, is_legal=True, output_dcp=output,
                summary="operator_rewrite constructive accumulator candidate=1",
                details={"proof_version": PROOF_VERSION, "template": target, "attempts": attempts, "runtime_s": round(time.monotonic() - started, 6), "final_submission_authority": False},
                candidates=(SkillOutput(
                    output,
                    f"constructive accumulator family={len(cone_rows)} canonical proof and strict validation passed",
                    evidence,
                    proof,
                ),),
            )
        except Exception as exc:
            output.unlink(missing_ok=True)
            return SkillResult.failure(before_wns, str(exc), failed_dcp, details={
                "typed_rejection": {"reason_code": str(exc).split(":", 1)[0][:120], "message": str(exc)},
                "template": target, "attempts": attempts,
                "runtime_s": round(time.monotonic() - started, 6),
            })

    async def _execute_memory_forward(
        self, mcp, target: str, params: dict, before_wns: float,
        clock_period: Optional[float], started: float, failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        action_dir = Path(mcp.run_dir)
        extraction_dir = action_dir / "memory_forward_recovery"
        output_dir = action_dir / "memory_forward_candidate"
        output = output_dir / "candidate.dcp"
        report_path = action_dir / "memory_forward_strict_validation.json"
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("MEMFWD_INPUT_DCP_UNAVAILABLE")
            source_sha256 = sha256_file(input_dcp)
            if clock_period is None:
                raise ValueError("MEMFWD_CLOCK_PERIOD_UNAVAILABLE")
            clock = str(params.get("_contest_clock") or "")
            parent_report = await capture_live_timing_report(mcp, clock=clock, top_n=300)
            extraction_dir.mkdir(parents=True, exist_ok=True)
            script = Path(__file__).resolve().parents[2] / "scripts" / "extract_memory_forward_candidates.tcl"
            await _run_vivado_stage(
                script=script, args=[str(input_dcp), str(extraction_dir)], cwd=action_dir,
                log_path=action_dir / "memory_forward_recovery.log", timeout_s=900.0,
            )
            recovery_rejections = []
            recovered = await asyncio.to_thread(
                recover_memory_forward_contracts, extraction_dir, source_sha256,
                rejection_log=recovery_rejections,
            )
            attempts.append({
                "proved_contract_count": len(recovered),
                "typed_rejections": recovery_rejections,
                "status": "fresh_contract_recovery_complete",
            })
            if not recovered:
                reason = (
                    recovery_rejections[0]["reason_code"]
                    if recovery_rejections
                    else "MEMFWD_NO_COMPLETE_PROVED_CONTRACT"
                )
                raise ValueError(reason)
            contract, binding, local_proof = recovered[0]
            authorization = build_memory_forward_authorization(local_proof)
            tcl, topology = render_memory_forward_eco_tcl(
                seed_dcp=input_dcp, output_dir=output_dir,
                contract=contract, binding=binding, proof=local_proof,
            )
            eco_script = action_dir / "memory_forward_apply.tcl"
            eco_script.write_text(tcl)
            route_timeout = float(params.get("route_timeout_s", 3600.0))
            await _run_vivado_stage(
                script=eco_script, args=[], cwd=action_dir,
                log_path=action_dir / "memory_forward_apply.log",
                timeout_s=route_timeout + 1200.0,
            )
            if not output.is_file():
                raise RuntimeError("MEMFWD_CANDIDATE_DCP_MISSING")
            candidate_sha256 = sha256_file(output)
            audit_script = action_dir / "memory_forward_reopen_audit.tcl"
            audit_script.write_text(render_memory_forward_audit_tcl(
                candidate=output, source=input_dcp, source_sha256=source_sha256,
                topology=topology,
            ))
            await _run_vivado_stage(
                script=audit_script, args=[], cwd=action_dir,
                log_path=action_dir / "memory_forward_reopen_audit.log", timeout_s=1200.0,
            )
            attestation = _metaloop_attestation(
                template=target, source_sha256=source_sha256,
                region_sha256=authorization["source"]["region_sha256"],
                candidate_sha256=candidate_sha256,
                authorization_id=authorization["certificate_id"],
                topology_sha256=topology["manifest_sha256"],
                checks={
                    "fresh_seed_bound_recovery": True, "read_first_port_contract_replayed": True,
                    "port_symmetric_contract_replayed": True,
                    "write_boundary_replayed": True, "read_consumer_closure_replayed": True,
                    "exact_namespace_inventory": True, "candidate_checkpoint_reopened": True,
                    "source_checkpoint_reopened": True, "relevant_nets_fully_routed": True,
                },
            )
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(float(params.get("_candidate_commit_reserve_s", 0.0)), float(params.get("min_candidate_remaining_s", 0.0)))
            strict_report = await run_strict_validation_worker(
                input_dcp, output, report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=(float(deadline) - reserve if deadline is not None else None),
            )
            proof = build_operator_candidate_proof(
                target, authorization, candidate_sha256, strict_report,
                emitted_topology_attestation=attestation,
            )
            parent_profile = build_measured_residual_profile(
                parent_report, artifact_sha256=source_sha256,
                global_wns_ns=before_wns,
                fmax_mhz=1000.0 / (float(clock_period) - float(before_wns)),
                top_n=300, source_label="live_vivado:memory_forward_parent_top300",
            )
            evidence = {
                "version": 1, "family_kind": "anonymous_read_first_bram_write_boundary",
                "proof_template": local_proof["proof_template"], "proof_id": local_proof["proof_id"],
                "target_memory_fingerprint": contract.target_memory_fingerprint,
                "write_port": contract.write_port,
                "read_port": contract.read_ports[0].port,
                "topology_manifest_sha256": topology["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "candidate_dcp_sha256": candidate_sha256,
                "enabling_candidate": {
                    "targeted_families": ["hard_memory"],
                    "parent_residual_profile": parent_profile,
                    "candidate_residual_profile": None,
                    "profile_request": {"top_n": 300},
                    "strict_validation_passed": True,
                },
            }
            return SkillResult(
                success=True, before_wns=before_wns, after_wns=before_wns,
                delta_wns=0.0, is_legal=True, output_dcp=output,
                summary="operator_rewrite memory write buffer forward candidate=1",
                details={"proof_version": PROOF_VERSION, "template": target, "attempts": attempts, "runtime_s": round(time.monotonic() - started, 6), "final_submission_authority": False},
                candidates=(SkillOutput(output, "memory write buffer forwarding local miter and strict validation passed", evidence, proof),),
            )
        except Exception as exc:
            output.unlink(missing_ok=True)
            return SkillResult.failure(before_wns, str(exc), failed_dcp, details={
                "typed_rejection": {"reason_code": str(exc).split(":", 1)[0][:120], "message": str(exc)},
                "template": target, "attempts": attempts,
                "runtime_s": round(time.monotonic() - started, 6),
            })

    async def _execute_two_operand(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        output = Path(mcp.run_dir) / "operator_rewrite_two_operand.dcp"
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(
                float(params.get("_candidate_commit_reserve_s", 0.0)),
                float(params.get("min_candidate_remaining_s", 0.0)),
            )
            if deadline is not None and float(deadline) - time.monotonic() <= reserve:
                raise TimeoutError("operator_rewrite candidate reserve is exhausted")
            route_timeout = float(params.get("route_timeout_s", 1800.0))
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            facts_set = _payload(await mcp.call_rw(
                "recover_two_operand_contract",
                {"max_candidates": 8},
                timeout=600.0,
            ))
            candidates = facts_set.get("candidates")
            candidate_count = facts_set.get("candidate_count")
            if (
                facts_set.get("kind") != "two_operand_product_contract_facts_set"
                or facts_set.get("version") != 1
                or facts_set.get("fresh_rediscovery") is not True
                or facts_set.get("design_sha256") != source_sha256
                or facts_set.get("proof_status")
                != "two_operand_facts_extracted_unproved"
                or facts_set.get("mutation_eligible") is not False
                or isinstance(candidate_count, bool)
                or not isinstance(candidate_count, int)
                or not isinstance(candidates, list)
                or len(candidates) != candidate_count
                or candidate_count < 1
            ):
                raise RuntimeError("fresh two-operand contract set has invalid authority")
            attempts.append({
                "candidate_count": candidate_count,
                "status": "contract_set_recovered",
            })
            # Deterministic selection: the first freshly recovered candidate
            # whose mechanical plan authorizes under exact proof replay wins.
            facts = None
            action_candidate = None
            for item in candidates:
                try:
                    plans = build_two_operand_action_plans(item)
                except OperatorProofError:
                    continue
                if plans:
                    facts = item
                    action_candidate = plans[0]
                    break
            if facts is None or action_candidate is None:
                raise RuntimeError(
                    "no two-operand candidate authorized a proved rewrite plan"
                )
            plan = action_candidate["plan"]
            candidate_id = facts["candidate_id"]
            boundary = facts.get("boundary")
            if (
                not isinstance(boundary, dict)
                or not isinstance(boundary.get("clock_pin"), str)
                or not boundary["clock_pin"]
                or not isinstance(boundary.get("cone_cells"), list)
            ):
                raise RuntimeError("two-operand boundary clock contract is unavailable")
            attempts[-1]["candidate_id"] = candidate_id
            proposals = _payload(await mcp.call_rw(
                "propose_two_operand_placement_candidates",
                {"region_sha256": facts["region_sha256"], "max_sites": 64},
                timeout=600.0,
            ))
            proposed = proposals.get("slots")
            if (
                proposals.get("fresh_rediscovery") is not True
                or proposals.get("source_dcp_sha256") != source_sha256
                or proposals.get("region_sha256") != facts["region_sha256"]
                or not isinstance(proposed, list)
                or not proposed
            ):
                raise RuntimeError("two-operand placement proposal is stale")
            oracle = _payload(await mcp.call_vivado(
                "measure_connected_dsp_slots",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "region_sha256": facts["region_sha256"],
                    "clock_pin": boundary["clock_pin"],
                    "candidate_sites": [
                        {
                            "site": item["site"],
                            "distance": int(item["distance"]),
                        }
                        for item in proposed[:32]
                    ],
                    "timeout": 600.0,
                },
                timeout=1800.0,
            ))
            if (
                oracle.get("read_only_after_restore") is not True
                or oracle.get("cleanup") != {
                    "source_checkpoint_reopened": True,
                    "temporary_namespace_discarded": True,
                }
            ):
                raise RuntimeError("two-operand oracle did not restore the seed")
            measured_slots = [
                dict(slot)
                for slot in (oracle.get("slots") or [])
                if isinstance(slot, dict) and slot.get("clock_route_ok") is True
            ]
            placement = allocate_two_operand_dsp_placement(
                facts,
                action_candidate,
                oracle_slots=measured_slots,
            )
            json_candidate = dict(action_candidate)
            json_candidate["plan"] = asdict(plan)
            attestation = _payload(await mcp.call_vivado(
                "apply_proved_two_operand_eco",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "facts": facts,
                    "candidate": json_candidate,
                    "placement": placement,
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                },
                timeout=route_timeout + 1300.0,
            ))
            if not output.is_file():
                raise RuntimeError("two-operand ECO output checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if attestation.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("two-operand attestation candidate mismatch")
            validation_deadline = (
                float(deadline) - reserve if deadline is not None else None
            )
            strict_report = await run_strict_validation_worker(
                input_dcp,
                output,
                report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=validation_deadline,
            )
            proof = build_operator_candidate_proof(
                target,
                action_candidate["authorization"],
                candidate_sha256,
                strict_report,
                emitted_topology_attestation=attestation,
            )
            spec = facts["spec"]
            evidence = {
                "version": 1,
                "candidate_id": candidate_id,
                "cone_cell_count": len(boundary["cone_cells"]),
                "a_width": int(spec["a_width"]),
                "b_width": int(spec["b_width"]),
                "term_width": int(spec["term_width"]),
                "accumulator_width": int(spec["accumulator_width"]),
                "product_latency": int(spec["product_latency"]),
                "placement_manifest_sha256": placement["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "strict_validation_precheck_vectors": (
                    MIN_POST_EMISSION_PRECHECK_VECTORS
                ),
                "acceptance_authority": "central measurement and final strict gate",
            }
            attempts[-1]["status"] = "proved_two_operand_candidate"
            skill_output = SkillOutput(
                output,
                (
                    "operator_rewrite two operand "
                    f"cone={len(boundary['cone_cells'])} "
                    f"a={int(spec['a_width'])} b={int(spec['b_width'])} "
                    f"acc={int(spec['accumulator_width'])} "
                    f"latency={int(spec['product_latency'])}"
                )[:120],
                evidence,
                proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=output,
                summary="operator_rewrite two operand candidate=1 proof=operator-rewrite-v1",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            output.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )


    async def _execute_register_absorb(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        output = Path(mcp.run_dir) / "operator_rewrite_register_absorb.dcp"
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(
                float(params.get("_candidate_commit_reserve_s", 0.0)),
                float(params.get("min_candidate_remaining_s", 0.0)),
            )
            if deadline is not None and float(deadline) - time.monotonic() <= reserve:
                raise TimeoutError("operator_rewrite candidate reserve is exhausted")
            route_timeout = float(params.get("route_timeout_s", 1800.0))
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            facts_set = _payload(await mcp.call_rw(
                "recover_register_absorb_contract",
                {"max_candidates": 8},
                timeout=600.0,
            ))
            candidates = facts_set.get("candidates")
            candidate_count = facts_set.get("candidate_count")
            if (
                facts_set.get("kind") != "register_absorb_contract_facts_set"
                or facts_set.get("version") != 1
                or facts_set.get("fresh_rediscovery") is not True
                or facts_set.get("design_sha256") != source_sha256
                or facts_set.get("proof_status")
                != "register_absorb_facts_extracted_unproved"
                or facts_set.get("mutation_eligible") is not False
                or isinstance(candidate_count, bool)
                or not isinstance(candidate_count, int)
                or not isinstance(candidates, list)
                or len(candidates) != candidate_count
                or candidate_count < 1
            ):
                raise RuntimeError(
                    "fresh register-absorb contract set has invalid authority"
                )
            attempts.append({
                "candidate_count": candidate_count,
                "status": "contract_set_recovered",
            })
            # Deterministic selection: the first freshly recovered candidate
            # whose mechanical plan authorizes under exact proof replay wins.
            facts = None
            action_candidate = None
            for item in candidates:
                try:
                    plans = build_register_absorb_action_plans(item)
                except OperatorProofError:
                    continue
                if plans:
                    facts = item
                    action_candidate = plans[0]
                    break
            if facts is None or action_candidate is None:
                raise RuntimeError(
                    "no register-absorb candidate authorized a proved rewrite plan"
                )
            plan = action_candidate["plan"]
            candidate_id = facts["candidate_id"]
            attempts[-1]["candidate_id"] = candidate_id
            # The target DSP is already placed: no placement proposal and no
            # measured oracle. The manifest replays the recovered site under
            # the exact pre-mutation authorization.
            placement = build_register_absorb_placement_manifest(
                facts, action_candidate
            )
            json_candidate = dict(action_candidate)
            json_candidate["plan"] = asdict(plan)
            attestation = _payload(await mcp.call_vivado(
                "apply_proved_register_absorb_eco",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "facts": facts,
                    "candidate": json_candidate,
                    "placement": placement,
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                },
                timeout=route_timeout + 1300.0,
            ))
            if not output.is_file():
                raise RuntimeError("register-absorb ECO output checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if attestation.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("register-absorb attestation candidate mismatch")
            validation_deadline = (
                float(deadline) - reserve if deadline is not None else None
            )
            strict_report = await run_strict_validation_worker(
                input_dcp,
                output,
                report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=validation_deadline,
            )
            proof = build_operator_candidate_proof(
                target,
                action_candidate["authorization"],
                candidate_sha256,
                strict_report,
                emitted_topology_attestation=attestation,
            )
            spec = facts["spec"]
            evidence = {
                "version": 1,
                "candidate_id": candidate_id,
                "bus_width": int(spec["bus_width"]),
                "dsp_side": str(spec["dsp_side"]),
                "reset_kind": str(spec["reset_kind"]),
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "strict_validation_precheck_vectors": (
                    MIN_POST_EMISSION_PRECHECK_VECTORS
                ),
                "placement_manifest_sha256": placement["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "acceptance_authority": "central measurement and final strict gate",
            }
            attempts[-1]["status"] = "proved_register_absorb_candidate"
            skill_output = SkillOutput(
                output,
                (
                    "operator_rewrite register absorb "
                    f"bus={int(spec['bus_width'])} side={str(spec['dsp_side'])} "
                    f"reset={str(spec['reset_kind'])}"
                )[:120],
                evidence,
                proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=output,
                summary="operator_rewrite register absorb candidate=1 proof=operator-rewrite-v1",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            output.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )


    async def _execute_pipeline_rebalance(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        output = Path(mcp.run_dir) / "operator_rewrite_pipeline_rebalance.dcp"
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        try:
            raw_input = Path(str(params.get("input_dcp") or "")).expanduser()
            if raw_input.is_symlink():
                raise ValueError(
                    "operator_rewrite exact input DCP must not be a symlink"
                )
            input_dcp = raw_input.resolve()
            if not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            deadline = params.get("_execution_deadline_monotonic")
            reserve = max(
                float(params.get("_candidate_commit_reserve_s", 0.0)),
                float(params.get("min_candidate_remaining_s", 0.0)),
            )
            if deadline is not None and float(deadline) - time.monotonic() <= reserve:
                raise TimeoutError("operator_rewrite candidate reserve is exhausted")
            route_timeout = float(params.get("route_timeout_s", 1800.0))
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            facts_set = _payload(await mcp.call_rw(
                "recover_pipeline_rebalance_contract",
                {"max_candidates": 8},
                timeout=600.0,
            ))
            candidates = facts_set.get("candidates")
            candidate_count = facts_set.get("candidate_count")
            if (
                facts_set.get("kind") != "pipeline_rebalance_contract_facts_set"
                or facts_set.get("version") != 1
                or facts_set.get("fresh_rediscovery") is not True
                or facts_set.get("design_sha256") != source_sha256
                or facts_set.get("proof_status")
                != "pipeline_rebalance_facts_extracted_unproved"
                or facts_set.get("mutation_eligible") is not False
                or isinstance(candidate_count, bool)
                or not isinstance(candidate_count, int)
                or not isinstance(candidates, list)
                or len(candidates) != candidate_count
                or candidate_count < 1
            ):
                raise RuntimeError(
                    "fresh pipeline-rebalance contract set has invalid authority"
                )
            attempts.append({
                "candidate_count": candidate_count,
                "status": "contract_set_recovered",
            })
            # Deterministic selection: the first freshly recovered candidate
            # whose mechanical plan authorizes under exact proof replay wins.
            facts = None
            action_candidate = None
            for item in candidates:
                try:
                    plans = build_pipeline_rebalance_action_plans(item)
                except OperatorProofError:
                    continue
                if plans:
                    facts = item
                    action_candidate = plans[0]
                    break
            if facts is None or action_candidate is None:
                raise RuntimeError(
                    "no pipeline-rebalance candidate authorized a proved rewrite plan"
                )
            plan = action_candidate["plan"]
            candidate_id = facts["candidate_id"]
            attempts[-1]["candidate_id"] = candidate_id
            # The target DSP is already placed: no placement proposal and no
            # measured oracle. The manifest replays the recovered site under
            # the exact pre-mutation authorization.
            placement = build_pipeline_rebalance_placement_manifest(
                facts, action_candidate
            )
            json_candidate = dict(action_candidate)
            json_candidate["plan"] = asdict(plan)
            topology = build_pipeline_rebalance_topology_manifest(
                facts, json_candidate, placement
            )
            attestation = _payload(await mcp.call_vivado(
                "apply_proved_pipeline_rebalance_eco",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "facts": facts,
                    "candidate": json_candidate,
                    "placement": placement,
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                },
                timeout=route_timeout + 1300.0,
            ))
            if not output.is_file():
                raise RuntimeError("pipeline-rebalance ECO output checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if attestation.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("pipeline-rebalance attestation candidate mismatch")
            if (
                attestation.get("placement_manifest_sha256")
                != placement["manifest_sha256"]
            ):
                raise RuntimeError("pipeline-rebalance attestation placement mismatch")
            if (
                attestation.get("topology_manifest_sha256")
                != topology["manifest_sha256"]
            ):
                raise RuntimeError("pipeline-rebalance attestation topology mismatch")
            validation_deadline = (
                float(deadline) - reserve if deadline is not None else None
            )
            strict_report = await run_strict_validation_worker(
                input_dcp,
                output,
                report_path,
                vectors=MIN_POST_EMISSION_VECTORS,
                precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
                timeout_s=1800.0,
                deadline_monotonic=validation_deadline,
            )
            proof = build_operator_candidate_proof(
                target,
                action_candidate["authorization"],
                candidate_sha256,
                strict_report,
                emitted_topology_attestation=attestation,
            )
            spec = facts["spec"]
            evidence = {
                "version": 1,
                "candidate_id": candidate_id,
                "direction": str(spec["direction"]),
                "a_width": int(spec["a_width"]),
                "b_width": int(spec["b_width"]),
                "reset_kind": str(spec["reset_kind"]),
                "strict_validation_vectors": MIN_POST_EMISSION_VECTORS,
                "strict_validation_precheck_vectors": (
                    MIN_POST_EMISSION_PRECHECK_VECTORS
                ),
                "placement_manifest_sha256": placement["manifest_sha256"],
                "emitted_topology_attestation_id": attestation["attestation_id"],
                "acceptance_authority": "central measurement and final strict gate",
            }
            attempts[-1]["status"] = "proved_pipeline_rebalance_candidate"
            skill_output = SkillOutput(
                output,
                (
                    "operator_rewrite pipeline rebalance "
                    f"dir={str(spec['direction'])} a={int(spec['a_width'])} "
                    f"b={int(spec['b_width'])} reset={str(spec['reset_kind'])}"
                )[:120],
                evidence,
                proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=output,
                summary="operator_rewrite pipeline rebalance candidate=1 proof=operator-rewrite-v1",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            output.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )


def _payload(raw) -> dict:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("MCP tool returned a non-object response")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def _metaloop_attestation(
    *, template: str, source_sha256: str, region_sha256: str,
    candidate_sha256: str, authorization_id: str,
    topology_sha256: str, checks: dict,
) -> dict:
    value = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": template,
        "source": {"dcp_sha256": source_sha256, "region_sha256": region_sha256},
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization_id,
        "placement_manifest_sha256": topology_sha256,
        "topology_manifest_sha256": topology_sha256,
        "checks": checks,
    }
    value["attestation_id"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return value


def _critical_paths(raw) -> list[list[str]]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        raise ValueError("critical path extraction returned a non-list response")
    result = []
    for path in value[:16]:
        if not isinstance(path, list):
            continue
        cells = [item for item in path[:64] if isinstance(item, str) and item]
        if cells:
            result.append(cells)
    return result
