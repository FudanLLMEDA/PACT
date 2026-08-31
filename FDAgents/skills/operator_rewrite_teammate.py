"""Closed-template structural rewrites gated by post-emission proof."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from ..artifacts import sha256_file
from ..operator_proof import (
    OPERATOR_CANDIDATE_PROOF_VERSION,
    OPERATOR_TEMPLATE_BINDINGS,
    build_arithmetic_compressor_authorization,
    build_banked_predicate_authorization,
    build_banked_predicate_emitted_attestation,
    build_banked_ram_operator_authorization,
    build_banked_ram_operator_emitted_attestation,
    build_commutative_reassociation_authorization,
    build_commutative_reassociation_emitted_attestation,
    build_consumer_rewrite_authorization,
    build_fixed_point_contract_authorization,
    build_operator_candidate_proof,
    build_registered_product_authorization,
    build_registered_product_emitted_attestation,
    build_registered_dsp_lane_authorization,
    build_registered_dsp_lane_emitted_attestation,
    build_registered_dead_state_authorization,
    build_registered_dead_state_emitted_attestation,
    build_control_state_equation_authorization,
    build_control_state_equation_emitted_attestation,
    build_ff_srl_boundary_bridge_authorization,
    build_ff_srl_boundary_bridge_emitted_attestation,
    build_memory_forward_emitted_attestation,
    build_multilevel_control_localization_authorization,
    build_multilevel_control_localization_emitted_attestation,
    build_registered_parity_authorization,
    build_registered_parity_emitted_attestation,
)
from ..compressor_proof import (
    ConeGraph,
    CompressorProofTimeout,
    build_compressor_certificate,
    compressor_certificate_summary,
)
from ..constructive_accumulator import discover_constructive_accumulator
from ..constructive_accumulator_lowering import build_constructive_lowering
from ..fixed_point_dsp import (
    build_fixed_point_contract,
    contract_summary as fixed_point_contract_summary,
    source_topology_summary,
)
from ..probe_handoff import (
    resolve_consumer_rewrite_handoff,
    resolve_control_state_handoff,
    resolve_ff_srl_boundary_bridge_handoff,
    resolve_memory_forward_handoff,
    resolve_multilevel_control_localization_handoff,
    resolve_registered_parity_handoff,
    resolve_registered_dead_state_handoff,
    resolve_sequential_family_handoff,
)
from ..metaloop_action_proof import (
    build_constructive_accumulator_authorization,
)
from ..registered_closure import (
    RegisteredClosure,
    build_generating_operator_bridge_bundle,
    build_registered_product_bundle,
    merge_generating_operator_bridge_bundles,
    namespace_generating_operator_bridge_bundle,
)
from ..recurrence_action import (
    allocate_recurrence_family_srl_placements,
    build_recurrence_family_action_plans,
)
from .base import SkillOutput, SkillResult, unexpected_execution_params


PROOF_VERSION = OPERATOR_CANDIDATE_PROOF_VERSION
MAX_CANDIDATES = 2
QOR_PROFILES = ("compact_timing", "net_delay_focus")
POST_EMISSION_VERIFIER_AVAILABLE = True
RECURRENCE_EXECUTION_BACKEND_AVAILABLE = True
FIXED_POINT_EXECUTION_BACKEND_AVAILABLE = True
ARITHMETIC_COMPRESSOR_EXECUTION_BACKEND_AVAILABLE = True
_REGISTERED_SCREEN_CACHE_VERSION = 2
_REGISTERED_FIR_BUNDLE_CACHE_VERSION = 1
# A shallow cut may still expose registered residuals computed by the old
# LUT/CARRY arithmetic.  Two additional stages let semantic recovery reach the
# complete word-level operand boundary; every selected frontier still requires
# exact data/control replay, so depth alone never authorizes a rewrite.
_REGISTERED_PRODUCT_MAX_STAGE_DEPTH = 6
_REGISTERED_FIR_TARGET = "registered_fir_data_cone_rewrite"
_CONSUMER_DRIVEN_TARGET = "consumer_driven_registered_arithmetic_rewrite_v1"
_BANKED_PREDICATE_TARGET = "bank_local_predicate_specialization_v1"
_RAM_BOOLEAN_MIDPOINT_TARGET = "registered_ram_boolean_midpoint_relocation_v1"
_CONTROL_PACKET_TARGET = "registered_control_packet_localization_v1"
_COMMUTATIVE_REASSOCIATION_TARGET = (
    "commutative_operand_bus_slot_reassociation_v1"
)
_REGISTERED_DSP_LANE_TARGET = "registered_dsp_lane_rebuild_v1"
_REGISTERED_DEAD_STATE_TARGET = "registered_dead_state_elimination_v1"
_CONTROL_STATE_EQUATION_TARGET = "exact_control_to_data_state_equation_v1"
_FF_SRL_BOUNDARY_BRIDGE_TARGET = "ff_srl_boundary_bridge_v1"
_MEMORY_FORWARD_TARGET = "memory_write_buffer_forward_v1"
_MULTILEVEL_CONTROL_TARGET = "multilevel_control_distribution_localization_v1"
_REGISTERED_PARITY_TARGET = "registered_gf2_parity_reassociation_v1"
_CONSTRUCTIVE_ACCUMULATOR_TARGET = "constructive_accumulator_rebuild_v1"


@dataclass(frozen=True, slots=True)
class OperatorRewriteTemplate:
    rewrite_kind: str
    proof_template: str
    execution_model: str
    execution_status: str
    action_type: str


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
        action_type="complete_operator_rewrite",
    ),
    "proved_arithmetic_compressor_v1": OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            "proved_arithmetic_compressor_v1"
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            "proved_arithmetic_compressor_v1"
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
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
        action_type="local_operator_measurement_probe",
    ),
    _REGISTERED_FIR_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_FIR_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_FIR_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _CONSUMER_DRIVEN_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _CONSUMER_DRIVEN_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _CONSUMER_DRIVEN_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _BANKED_PREDICATE_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _BANKED_PREDICATE_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _BANKED_PREDICATE_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _RAM_BOOLEAN_MIDPOINT_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _RAM_BOOLEAN_MIDPOINT_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _RAM_BOOLEAN_MIDPOINT_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _CONTROL_PACKET_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _CONTROL_PACKET_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _CONTROL_PACKET_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _COMMUTATIVE_REASSOCIATION_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _COMMUTATIVE_REASSOCIATION_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _COMMUTATIVE_REASSOCIATION_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _REGISTERED_DSP_LANE_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_DSP_LANE_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_DSP_LANE_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _REGISTERED_DEAD_STATE_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_DEAD_STATE_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_DEAD_STATE_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _CONTROL_STATE_EQUATION_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _CONTROL_STATE_EQUATION_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _CONTROL_STATE_EQUATION_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _FF_SRL_BOUNDARY_BRIDGE_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _FF_SRL_BOUNDARY_BRIDGE_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _FF_SRL_BOUNDARY_BRIDGE_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _MEMORY_FORWARD_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _MEMORY_FORWARD_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _MEMORY_FORWARD_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _MULTILEVEL_CONTROL_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _MULTILEVEL_CONTROL_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _MULTILEVEL_CONTROL_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _REGISTERED_PARITY_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_PARITY_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _REGISTERED_PARITY_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
    _CONSTRUCTIVE_ACCUMULATOR_TARGET: OperatorRewriteTemplate(
        rewrite_kind=OPERATOR_TEMPLATE_BINDINGS[
            _CONSTRUCTIVE_ACCUMULATOR_TARGET
        ]["rewrite_kind"],
        proof_template=OPERATOR_TEMPLATE_BINDINGS[
            _CONSTRUCTIVE_ACCUMULATOR_TARGET
        ]["proof_template"],
        execution_model="exact_seed_dcp_eco",
        execution_status="executable",
        action_type="local_operator_measurement_probe",
    ),
})
OPERATOR_REWRITE_TARGETS = tuple(OPERATOR_REWRITE_REGISTRY)

_EXECUTABLE_DISPATCH_TARGETS = frozenset({
    "dsp_srl_recurrence_cut_v1",
    "proved_arithmetic_compressor_v1",
    _CONSTRUCTIVE_ACCUMULATOR_TARGET,
    "fixed_point_dsp_rebuild_v1",
    _REGISTERED_FIR_TARGET,
    _CONSUMER_DRIVEN_TARGET,
    _BANKED_PREDICATE_TARGET,
    _RAM_BOOLEAN_MIDPOINT_TARGET,
    _CONTROL_PACKET_TARGET,
    _COMMUTATIVE_REASSOCIATION_TARGET,
    _REGISTERED_DSP_LANE_TARGET,
    _CONTROL_STATE_EQUATION_TARGET,
    _FF_SRL_BOUNDARY_BRIDGE_TARGET,
    _MEMORY_FORWARD_TARGET,
    _MULTILEVEL_CONTROL_TARGET,
    _REGISTERED_PARITY_TARGET,
    _REGISTERED_DEAD_STATE_TARGET,
})


def operator_rewrite_executor_ready(target: str) -> bool:
    """Return whether the teammate provider has an explicit execute branch."""
    return str(target) in _EXECUTABLE_DISPATCH_TARGETS

# Knowledge-derived compatibility contracts are visible to the Decision Agent
# for classification, but are intentionally absent from the executable
# registry until a generic exact-contract extractor, emitter, and post-emission
# proof backend all exist.  This prevents a pattern name from masquerading as
# an available mutation.
_BACKLOG_TEMPLATE_CATALOG: Mapping[str, dict[str, object]] = MappingProxyType({
    "fixed_point_lut_to_dsp_remap_v1": {
        "rewrite_kind": "fixed_point_resource_substitution",
        "proof_template": "FIXED_POINT_LUT_TO_DSP_V1",
        "execution_model": "recovered_operator_or_source_reemission",
        "execution_status": "backend_not_registered",
        "selection_status": "classify_or_probe_only",
        "semantic_level": "L1-L2",
        "knowledge_pattern": "REPEATED_ARITHMETIC_CONE",
        "compatibility_contract": [
            "complete word width and signedness",
            "shift, truncation, saturation/wrap, and accumulation order",
            "unchanged registered schedule and interface latency",
            "whole repeated-family wall coverage",
            "OOC identity and clock contract preserved",
        ],
        "contraindications": [
            "LUT-only resemblance without a proved word/loss boundary",
            "schedule, II, latency, rounding, saturation, or wrap drift",
            "DSP or clock-footprint pressure without measured headroom",
            "source or exact netlist re-emission backend unavailable",
        ],
    },
    "observable_slice_width_reduction_v1": {
        "rewrite_kind": "observable_slice_width_reduction",
        "proof_template": "OBSERVABLE_SLICE_WIDTH_REDUCTION_V1",
        "execution_model": "recovered_operator_or_source_reemission",
        "execution_status": "backend_not_registered",
        "selection_status": "classify_or_probe_only",
        "semantic_level": "L2",
        "knowledge_pattern": "LOW_SLICE_WIDE_PRODUCT",
        "compatibility_contract": [
            "complete composite producer and consumer bit-slice mapping",
            "all discarded bits proven unobservable across modes and cycles",
            "signedness, carries, rounding, saturation, and wrap preserved",
            "registered schedule and interface contract unchanged",
        ],
        "contraindications": [
            "leaf pin roles used as composite bit weights",
            "hidden quotient, root, status, carry, or readback consumers",
            "memory inference mode or cycle schedule changes",
        ],
    },
    "registered_reduction_rebuild_v1": {
        "rewrite_kind": "many_to_few_registered_reduction_rebuild",
        "proof_template": "REGISTERED_REDUCTION_BOUNDARY_V1",
        "execution_model": "recovered_operator_or_source_reemission",
        "execution_status": "backend_not_registered",
        "selection_status": "classify_or_probe_only",
        "semantic_level": "L3",
        "knowledge_pattern": "MANY_TO_FEW_REGISTERED_REDUCTION",
        "compatibility_contract": [
            "one exact replicated tile and its complete reduction boundary",
            "replicas limited to current wall-covered owners",
            "all cross-replica controls, side consumers, and ordering closed",
            "transactional merge with unchanged clock and registered latency",
        ],
        "contraindications": [
            "wide union boundary has higher entropy than the local cones",
            "mixed-resource wall is treated as one operator",
            "replica scope copied from another design or stale checkpoint",
        ],
    },
})

_EXECUTION_PARAMS = {
    "input_dcp",
    "max_candidates",
    "route_timeout_s",
    "min_candidate_remaining_s",
    "_contest_clock",
    "_execution_deadline_monotonic",
    "_declared_runtime_s",
    "_candidate_commit_reserve_s",
    "family_ref",
    "qor_profiles",
}
_TARGET_EXECUTION_PARAMS = {
    _REGISTERED_PARITY_TARGET: {"implementation_mode"},
}


def _framework_owned_execution_deadline(params: dict) -> float | None:
    deadline = params.get("_execution_deadline_monotonic")
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        return float(deadline)
    return None


def _framework_owned_commit_reserve(params: dict) -> float:
    values = [0.0]
    for key in ("_candidate_commit_reserve_s", "min_candidate_remaining_s"):
        value = params.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return max(values)


def _framework_owned_route_timeout(params: dict) -> float:
    """Give routing the remaining framework-owned action window.

    Luna chooses the semantic action, not an arbitrary short scheduler cutoff.
    The outer Agent deadline remains authoritative and leaves the framework's
    normal finalization window outside this action deadline.  The typed Vivado
    route interfaces accept at most 3600 seconds, so a larger outer window is
    represented by that schema maximum instead of being forwarded as an
    invalid request.
    """
    deadline = _framework_owned_execution_deadline(params)
    if deadline is not None and math.isfinite(deadline):
        window = min(3600.0, max(1.0, deadline - time.monotonic()))
        # The ranking charged this action for the cost its contract declares
        # and the window filter admitted it on that number, so it does not get
        # to spend the round instead.  Three times the declaration leaves ample
        # room for estimate error: ispd16_example2's rewrite declares 300s, was
        # granted its whole 2,776s window, spent it and returned +0.00, and its
        # recorded 120.61 MHz run takes a 435s post-route cleanup first.
        declared = params.get("_declared_runtime_s")
        if (
            isinstance(declared, (int, float))
            and not isinstance(declared, bool)
            and math.isfinite(float(declared))
            and float(declared) > 0.0
        ):
            return max(1.0, min(window, 3.0 * float(declared)))
        return window
    fallback = params.get("route_timeout_s", 3600.0)
    if (
        isinstance(fallback, (int, float))
        and not isinstance(fallback, bool)
        and math.isfinite(float(fallback))
        and float(fallback) >= 1.0
    ):
        return min(3600.0, float(fallback))
    return 3600.0


def _cached_commutative_route_facts(
    output: Path,
    report: Path,
    *,
    source_sha256: str,
    candidate_index: int,
) -> dict | None:
    """Reuse only a complete framework-owned routed pair checkpoint."""
    if (
        not output.is_file()
        or output.is_symlink()
        or not report.is_file()
        or report.is_symlink()
    ):
        return None
    try:
        values = {}
        for raw in report.read_text(encoding="utf-8").splitlines()[1:]:
            key, value = raw.split("\t", 1)
            values[key] = value
        if (
            values.get("contract")
            != "COMMUTATIVE_OPERAND_BUS_SLOT_REASSOCIATION"
            or values.get("backend")
            != "commutative_operand_bus_slot_reassociation_v1"
            or values.get("input_period_ns") != values.get("output_period_ns")
            or int(values.get("route_errors", "-1")) != 0
            or int(values.get("latency_delta_cycles", "-1")) != 0
        ):
            return None
        return {
            "contract": values["contract"],
            "backend": values["backend"],
            "word_width": int(values["word_width"]),
            "changed_net_count": int(values["changed_net_count"]),
            "moved_pin_count": int(values["moved_pin_count"]),
            "latency_delta_cycles": 0,
            "period_ns": float(values["output_period_ns"]),
            "input_wns_ns": float(values["input_wns_ns"]),
            "output_wns_ns": float(values["output_wns_ns"]),
            "route_errors": 0,
            "candidate_index": candidate_index,
            "source_dcp_sha256": source_sha256,
            "candidate_dcp_sha256": sha256_file(output),
            "candidate_checkpoint_reopened": True,
            "source_checkpoint_reopened": True,
            "relevant_nets_fully_routed": True,
            "primitive_replay_passed": True,
            "registered_control_transaction_preserved": True,
            "formal_whole_dcp_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
            "route_checkpoint_cache_reused": True,
        }
    except (KeyError, OSError, UnicodeError, ValueError):
        return None


def operator_rewrite_catalog() -> dict[str, dict[str, object]]:
    """Return the public closed-template catalog without implementation mechanics."""
    executable = {}
    for target, spec in OPERATOR_REWRITE_REGISTRY.items():
        if target == _CONSTRUCTIVE_ACCUMULATOR_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": (
                    "exploratory_after_fresh_constructive_accumulator_proof"
                ),
                "scope": "one_current_dcp_anonymous_registered_accumulator_family",
                "semantic_level": "L2-L3",
                "knowledge_pattern": "CONSTRUCTIVE_REGISTERED_ACCUMULATOR",
                "requires_public_family_ref": "one current sequential_N",
                "compatibility_contract": [
                    "complete registered output word and closed LUT/CARRY fanin cone",
                    "two signed frontier words with recovered constant coefficient and slice",
                    "exact canonical Boolean composition for every output bit",
                    "clock, CE, reset/init, target register and visible cycle unchanged",
                    "all selected siblings independently replay the same expression and lowering",
                ],
                "decision_evidence_chain": [
                    "current live wall ownership of the registered family",
                    "fresh coefficient, signedness, slice and side-fanout recovery",
                    "constructive source and emitted-column proof",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "open side fanout, sparse target word or unsupported primitive",
                    "ambiguous coefficient, operand order, signedness or loss boundary",
                    "canonical expression disagreement across selected siblings",
                    "a smaller fixed-point DSP or physical action better matches the live wall",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _REGISTERED_PARITY_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": (
                    "executable_after_live_complete_gf2_closure_recovery"
                ),
                "scope": "one_current_dcp_anonymous_registered_parity_family",
                "semantic_level": "L2-L3",
                "knowledge_pattern": "REGISTERED_GF2_PARITY_REDUCTION",
                "requires_probe": (
                    "vivado.recover_registered_parity_reassociation_contract"
                ),
                "compatibility_contract": [
                    "every recursively consumed LUT is an exact XOR or XNOR truth table",
                    "every boundary-net multiplicity is recovered before GF(2) cancellation",
                    "one current live registered data endpoint is selected privately",
                    "the original root and every non-selected consumer remain intact",
                    "destination register controls and observable cycle are unchanged",
                ],
                "decision_evidence_chain": [
                    "current endpoint path rank and parity closure depth",
                    "old closure size versus balanced-tree leaf count and levels",
                    "actual duplicate-cancellation opportunity and sibling-consumer count",
                    "routed measurement followed by immediate wall refresh",
                ],
                "implementation_modes": {
                    "local_preserve": (
                        "keep existing placed slice logic fixed when the proved "
                        "semantic tree is the local owner and the current physical "
                        "basin should remain stable"
                    ),
                    "closure_local_rebasin": (
                        "derive the occupied sites of the recovered private parity "
                        "closure and explicitly unplace only movable leaf cells in "
                        "those sites before Quick placement from the existing basin"
                    ),
                    "global_quick_rebasin": (
                        "release placed slice logic for Quick placement after the "
                        "same proved rewrite when live paths are strongly route-"
                        "dominated or a local semantic candidate left the physical "
                        "wall intact and sufficient time remains"
                    ),
                },
                "implementation_mode_decision_owner": "luna",
                "contraindications": [
                    "an open non-parity cell inside the purported closure",
                    "unaccounted sibling input or ambiguous boundary multiplicity",
                    "no registered endpoint in the current live timing sample",
                    "crossing a sequential or control boundary",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _MULTILEVEL_CONTROL_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": (
                    "executable_after_live_complete_control_dag_recovery"
                ),
                "scope": "one_current_dcp_anonymous_registered_control_family",
                "semantic_level": "L2-L3",
                "knowledge_pattern": "MULTILEVEL_REGISTERED_CONTROL_DISTRIBUTION",
                "requires_probe": (
                    "vivado.recover_multilevel_control_distribution_contract"
                ),
                "compatibility_contract": [
                    "complete acyclic LUT truth-table DAG to registered control sinks",
                    "all boundary sources and LUT side inputs recovered exactly",
                    "registered sink pin roles and cycle remain unchanged",
                    "the original generator and all non-selected consumers remain intact",
                    "private same-seed family selector",
                ],
                "decision_evidence_chain": [
                    "current top-path coverage and earliest covered rank",
                    "Boolean logic depth, sink count and clock-region distribution",
                    "whether the wall is control distribution rather than data arithmetic",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "incomplete or cyclic LUT closure",
                    "unresolved boundary source, sink role or cycle",
                    "no live timing overlap unless Luna deliberately explores",
                    "a large sink population without current wall evidence",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _MEMORY_FORWARD_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_memory_boundary_recovery",
                "scope": "one_current_dcp_anonymous_supported_bram_family",
                "semantic_level": "L3-L4",
                "knowledge_pattern": "MEMORY_WRITE_BUFFER_FORWARD",
                "requires_probe": (
                    "vivado.recover_memory_write_buffer_forward_contract"
                ),
                "compatibility_contract": [
                    "common-clock RAMB36E2 port-A write and port-B registered read",
                    "READ_FIRST write mode with DELAYED_WRITE cross-port collision semantics",
                    "complete registered write data, address and enable boundary",
                    "complete one-cycle registered read-consumer boundary",
                    "adjacent RAW hit/miss and byte-mask relational miter",
                    "private same-seed family selector",
                ],
                "decision_evidence_chain": [
                    "current write-side and read-side worst slack plus path rank",
                    "width-dependent pending-write, comparator and forwarding-mux cost",
                    "whether the present wall is serial write logic or read consumption",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "unsupported write/collision mode or independent clocks",
                    "incomplete read-consumer or write-driver closure",
                    "unresolved byte enables, output register latency or address alignment",
                    "a predicted gain without current write/read wall evidence",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _FF_SRL_BOUNDARY_BRIDGE_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_transport_recovery",
                "scope": "one_current_dcp_anonymous_direct_ff_family",
                "semantic_level": "L1",
                "knowledge_pattern": "DIRECT_FF_CHAIN_TO_SRL",
                "requires_probe": "vivado.recover_ff_srl_boundary_bridge_contract",
                "compatibility_contract": [
                    "maximal direct three-to-33-stage FDRE transport chain",
                    "identical clock polarity, CE, static-zero reset and zero INIT",
                    "no intermediate tap or side fanout",
                    "one observable final consumer boundary",
                    "retain the first hierarchy-boundary FF",
                    "preserve exact enabled-cycle latency with SRLC32E address length minus two",
                ],
                "decision_evidence_chain": [
                    "current top-500 path and endpoint overlap by anonymous family",
                    "chain population, length and constant-high versus dynamic CE mode",
                    "dynamic-CE write-enable wall risk versus FF transport removal",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "intermediate tap, side fanout or nonmaximal chain",
                    "asynchronous or nonzero reset history",
                    "unresolved INIT, clock edge or enabled-cycle behavior",
                    "no live timing overlap unless Luna deliberately spends exploration budget",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _CONTROL_STATE_EQUATION_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_truth_table_recovery",
                "scope": "one_current_dcp_anonymous_synchronous_state_family",
                "semantic_level": "L1-L2",
                "knowledge_pattern": "CONTROL_TO_DATA_STATE_EQUATION",
                "requires_probe": (
                    "vivado.recover_control_to_data_state_equation_contract"
                ),
                "compatibility_contract": [
                    "synchronous set and clock-enable each have one LUT driver",
                    "the exhaustive LUT truth table proves set implies enable",
                    "set is recoverable from a minimal support on the enable care set",
                    "clock, enable, init and observable state cycle remain unchanged",
                    "the current seed privately resolves every control_state_N family",
                    "before/after control-set diversity is measured from live clock, CE and synchronous control nets",
                ],
                "decision_evidence_chain": [
                    "live path-internal and endpoint overlap by anonymous family",
                    "state population and recovered support width",
                    "control-set fragmentation relief even when direct path overlap is weak",
                    "exact source and replacement registered state equations",
                    "cheap truth-table discrimination before a long physical basin when live control arcs overlap",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "set does not imply enable for every LUT input assignment",
                    "the set factor is ambiguous on the enable care set",
                    "clock, enable, init or state-cycle preservation is unresolved",
                    "only a name or one observed Boolean assignment suggests equivalence",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _REGISTERED_DEAD_STATE_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_observable_closure",
                "scope": "one_current_dcp_complete_output_unobservable_population",
                "semantic_level": "L4",
                "knowledge_pattern": "ZERO_CONSUMER_PRODUCER_FAMILY",
                "requires_probe": (
                    "rapidwright.recover_registered_dead_state_contract"
                ),
                "compatibility_contract": [
                    "all top-level observable output drivers anchor one full primitive fanin closure",
                    "registered data, control, reset and clock dependencies are traversed",
                    "only primitives outside that closure enter the private mutation manifest",
                    "expanded hard-macro components are deleted only as complete live parent macros",
                    "partial hard-macro parents and unbound physical implementation cells are preserved",
                ],
                "decision_evidence_chain": [
                    "current full-design observable/dead primitive partition",
                    "dead registered-state and hard-macro population by anonymous type",
                    "private exact-cell manifest bound to the current seed",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "the design has no resolvable top-level output drivers",
                    "a primitive driver or hard-macro parent is missing from the physical graph",
                    "only names or apparent unused ports suggest dead state without graph closure",
                    "the current closure contains no dead registered producer",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _REGISTERED_DSP_LANE_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_complete_product_transaction_recovery",
                "scope": "one_live_anonymous_dsp_product_register_family",
                "semantic_level": "L2-L3",
                "knowledge_pattern": "LOW_FANOUT_REGISTERED_DSP_ARRAY",
                "requires_probe": "vivado.recover_registered_dsp_lane_contract",
                "compatibility_contract": [
                    "one complete contiguous DSP P-to-external-FF word per lane",
                    "PREG is currently disabled and PCOUT plus output side consumers are closed",
                    "clock, CE, synchronous reset priority, reset word and observable cycle are recovered",
                    "the selected homogeneous family materially owns the current setup wall",
                    "reset-one bits are preserved by one history state per lane plus sparse correction",
                    "independent sibling ceiling measured before mutation",
                ],
                "decision_evidence_chain": [
                    "live wall path ownership and recovered lane/word dimensions",
                    "complete arithmetic-output and control/reset transaction",
                    "independent sibling rank and setup ceiling",
                    "newly clocked DSP count, clock-region span and control fanout risk",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "mixed DSP modes, incomplete P-bit mapping or unresolved PCOUT/side consumers",
                    "reset behavior cannot be reproduced by PREG plus bounded correction",
                    "an independent sibling wall leaves little material ceiling",
                    "new clock leaves or clock-region expansion dominate expected data-path benefit",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _COMMUTATIVE_REASSOCIATION_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_value_and_control_closure",
                "scope": "one_live_registered_commutative_modular_reduction",
                "semantic_level": "L2-L4",
                "knowledge_pattern": "CONTROL_CLOSED_COMMUTATIVE_OPERAND_REASSOCIATION",
                "requires_probe": "vivado.recover_commutative_reassociation_contract",
                "compatibility_contract": [
                    "all complete operand words and every endpoint bit are recovered",
                    "the exact modulo relation is replayed over the primitive cone",
                    "clock, CE, reset, INIT and observable transaction cycle match",
                    "the arithmetic cone has no outside data or control side fanout",
                    "only two complete operand buses change attachment",
                ],
                "decision_evidence_chain": [
                    "current registered LUT/CARRY wall and critical owner",
                    "commutative modular relation with word width and operand count",
                    "complete data plus control/state closure",
                    "high target uncertainty until changed-net-only route measurement",
                    "post-action wall refresh and failed-pair feedback",
                ],
                "contraindications": [
                    "mixed signed extension, coefficients, saturation or early truncation",
                    "different cycles, CE/reset domains, valid epochs or control ownership",
                    "partial-bit swap or open side fanout",
                    "treating geometric distance alone as a reliable QoR prediction",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target in {_RAM_BOOLEAN_MIDPOINT_TARGET, _CONTROL_PACKET_TARGET}:
            is_data = target == _RAM_BOOLEAN_MIDPOINT_TARGET
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "executable_after_live_banked_ram_data_control_contract",
                "scope": (
                    "live_ram_registered_boolean_family"
                    if is_data else "live_banked_ram_control_packet"
                ),
                "semantic_level": "L2-L4",
                "knowledge_pattern": "BANKED_RAM_DATA_CONTROL_OPERATOR",
                "requires_probe": "vivado.recover_banked_ram_operator_contract",
                "compatibility_contract": (
                    [
                        "RAM output and registered peer feed one exact Boolean LUT",
                        "the LUT directly drives an endpoint register",
                        "endpoint clock, CE, reset/set, INIT and cycle are recovered",
                        "only existing LUT/register placement changes",
                    ] if is_data else [
                        "registered-state or static inputs feed one exact Boolean generator",
                        "generator sinks are complete bank-local RAM control consumers",
                        "registered state is preserved and only the generator is cloned",
                        "sink ownership is partitioned from current physical regions",
                    ]
                ),
                "decision_evidence_chain": [
                    "current live path rank and wall ownership",
                    "exact data or control truth-table contract",
                    "registered control and zero-cycle preservation",
                    "routed measurement followed by immediate wall refresh",
                ],
                "contraindications": [
                    "ambiguous or multi-driver Boolean input",
                    "open endpoint control or RAM pin ownership",
                    "candidate family does not overlap the current live wall",
                    "a sibling data/control family is predicted to remain at the same WNS",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _BANKED_PREDICATE_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": (
                    "executable_after_live_banked_data_control_contract"
                ),
                "scope": "one_live_anonymous_banked_select_predicate_family",
                "semantic_level": "L2-L4",
                "knowledge_pattern": "BANKED_SELECT_PREDICATE_UPDATE",
                "requires_probe": "vivado.recover_banked_select_predicate_contract",
                "compatibility_contract": [
                    "registered RAM bank words feed one global selector",
                    "the selected word enters one exact Boolean/comparison predicate",
                    "bank-local write control already owns the corresponding address decode",
                    "all alternate/init truth-table cofactors and global predicate consumers remain unchanged",
                    "per-bank source mapping is recovered from live connectivity, not names or fixed dimensions",
                ],
                "decision_evidence_chain": [
                    "live selector/predicate/write wall ownership",
                    "registered bank-word and selected-input mapping",
                    "bank-local address/control role and alternate cofactors",
                    "global registered/observable consumer preservation",
                    "routed measurement and post-action wall",
                ],
                "contraindications": [
                    "selected-word input has zero or multiple bank-local registered sources",
                    "the local write predicate pin is ambiguous or not in the LUT support",
                    "global predicate has no separately preserved consumer",
                    "banked family does not overlap the current timing wall",
                ],
                "not_a_hard_trigger": True,
                "decision_owner": "luna",
            }
            continue
        if target == _CONSUMER_DRIVEN_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": (
                    "executable_only_after_live_consumer_contract_probe"
                ),
                "scope": "one_live_anonymous_registered_consumer_boundary",
                "semantic_level": "L2-L4",
                "knowledge_pattern": "CONSUMER_DRIVEN_ARITHMETIC_REDUCTION",
                "requires_public_family_ref": "one current sequential_N",
                "compatibility_contract": [
                    "wide registered producer materially owns the current timing wall",
                    "complete observable input/output word dimensions and loss boundary",
                    "high-level arithmetic class supported by live boundary evidence",
                    "clock, CE, reset/init, II, latency and output cycle preserved",
                    "framework privately derives all object names and wiring",
                ],
                "decision_evidence_chain": [
                    "live_wall_ownership",
                    "producer_to_consumer_dimensional_contraction",
                    "competing_high_level_semantic_classes",
                    "registered_control_and_latency_contract",
                    "routed_measurement_and_post_action_reprofile",
                ],
                "contraindications": [
                    "only a primitive pattern or hierarchy name suggests semantics",
                    "observable bits, predicates, modes, or side consumers are unresolved",
                    "latency, enable, reset, bubble, or output cycle is ambiguous",
                    "current exact family contract is absent or stale",
                ],
                "namespace_owner": "framework live-contract-derived namespace",
            }
            continue
        if target == _REGISTERED_FIR_TARGET:
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "exploratory_local_measurement_after_fresh_contract",
                "scope": "one_live_registered_product_sum_data_cone",
                "semantic_level": "L2-L3",
                "knowledge_pattern": "REGISTERED_FIR_OR_CONV_DATA_CONE",
                "requires_public_family_ref": "one current sequential_N",
                "compatibility_contract": [
                    "current timing wall ends in a complete indexed registered word",
                    "replay recovers only fixed-product terms and their exact per-term quantization",
                    "clock, CE, reset/init, observable latency, and state/control cone stay unchanged",
                    "the new DSP data cone replaces every proved old arithmetic driver at that word boundary",
                ],
                "decision_evidence_chain": [
                    "live_registered_wall_ownership",
                    "product_only_semantic_frontier",
                    "exact_data_and_control_replay",
                    "old_data_cone_reachability_and_sweep",
                    "routed_measurement_and_next_wall",
                ],
                "contraindications": [
                    "passthrough or unresolved accumulator terms remain at the selected frontier",
                    "tap/sample alignment, truncation, wrap, signedness, CE, reset, or latency is unresolved",
                    "the selected family does not own the current routed timing wall",
                    "the action would replace BRAM state, address/control FSM, or protocol outputs",
                ],
                "namespace_owner": "framework exact-source-derived namespace",
            }
            continue
        if target == "fixed_point_dsp_rebuild_v1":
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "exploratory_local_measurement_after_fresh_contract",
                "scope": "one_live_family_seed_with_anonymous_compatible_bank_expansion",
                "semantic_level": "L1-L2",
                "knowledge_pattern": "REPEATED_ARITHMETIC_CONE",
                "requires_public_family_ref": "one current sequential_N",
                "replica_expansion": (
                    "The framework may merge independently replay-proved compatible "
                    "banks from the current DCP. Discovery excludes design, hierarchy, "
                    "cell, placement, and timing-rank identity; Luna never names peers "
                    "or requests an instance/DSP count."
                ),
                "compatibility_contract": [
                    "current complete indexed destination register bus",
                    "complete LUT/CARRY cone with no side consumers",
                    "current-cone fixed-product slice and modular accumulation screen",
                    "positive DSP48E2 coefficient and exact signed chunk decomposition",
                    "target registers, controls, latency, clock, and timing constraints preserved",
                ],
                "correctness_authority": (
                    "The deterministic source screen admits only a measurement probe; "
                    "independent whole-DCP equivalence remains mandatory before submission."
                ),
                "decision_evidence_chain": [
                    "wall_and_replica_scope",
                    "arithmetic_and_loss_contract",
                    "schedule_control_and_observable_contract",
                    "source_vs_emitted_topology_payoff",
                    "semantic_vs_physical_alternative",
                ],
                "contraindications": [
                    "non-indexed or incomplete register boundary",
                    "nonseparable predicate, saturation, rounding, or hidden side consumer",
                    "coefficient cannot use the positive DSP48E2 multiplier contract",
                    "emitted topology, route, clock identity, or final equivalence failure",
                ],
            }
            continue
        if target == "proved_arithmetic_compressor_v1":
            executable[target] = {
                "rewrite_kind": spec.rewrite_kind,
                "proof_template": spec.proof_template,
                "execution_model": spec.execution_model,
                "execution_status": spec.execution_status,
                "action_type": spec.action_type,
                "selection_status": "exploratory_local_measurement_after_fresh_contract",
                "scope": "one_live_family_seed_with_backend_specific_scope_recovery",
                "semantic_level": "L1-L2",
                "knowledge_pattern": "REPEATED_ARITHMETIC_CONE",
                "requires_public_family_ref": "one current sequential_N",
                "family_choice_evidence": (
                    "Compare current worst-path/endpoint ownership and top-wall "
                    "coverage; discovery order and proof eligibility are not QoR rank."
                ),
                "decision_evidence_chain": [
                    "wall_and_replica_scope",
                    "arithmetic_and_loss_contract",
                    "schedule_control_and_observable_contract",
                    "source_vs_emitted_topology_payoff",
                    "semantic_vs_physical_alternative",
                ],
                "qor_profiles": {
                    "registered": list(QOR_PROFILES),
                    "role": (
                        "Luna-selected abstract physical hypotheses; the framework "
                        "owns the concrete Vivado directives and central measurement"
                    ),
                    "bounded_siblings": [1, MAX_CANDIDATES],
                    "selection_is_soft": True,
                },
                "execution_budget_contract": {
                    "fixed_full_cycle_estimate_s": None,
                    "staged": [
                        "fresh contract recovery",
                        "exact cone extraction and proof",
                        "proof-gated ECO and route",
                        "post-emission validation",
                    ],
                    "early_proof_rejection_is_terminal_without_mutation": True,
                    "hard_authority": "live global execution deadline",
                    "decision_semantics": (
                        "Do not treat the one-hour contest cap as a required "
                        "operator_rewrite cycle. Luna may first choose the "
                        "read-only probes, then compare the proof-gated backend "
                        "against alternatives using the actual remaining window."
                    ),
                },
                "compatibility_contract": [
                    "current-seed contiguous indexed registered destination bus",
                    "fresh complete CARRY8/LUT cone and side-consumer closure",
                    "clock, CE, reset, INIT, signedness, and loss boundary preserved",
                    "v6 emitted LUT6_2/LUT2/CARRY8 topology proof",
                ],
                "contraindications": [
                    "stale or absent current family_ref",
                    "unsupported primitive, ambiguous driver, sparse bus, or side fanout",
                    "polynomial degree above two or emitted-topology counterexample",
                ],
            }
            continue
        executable[target] = {
            "rewrite_kind": spec.rewrite_kind,
            "proof_template": spec.proof_template,
            "execution_model": spec.execution_model,
            "execution_status": spec.execution_status,
            "action_type": spec.action_type,
            "selection_status": "executable_after_fresh_exact_contract",
            "semantic_level": "L3-L4",
            "knowledge_pattern": "SELF_RECURRENCE_DENSE_FAMILY",
            "compatibility_contract": [
                "fresh exact recurrence family contract from the current seed",
                "state width, bit order, modular arithmetic, and side fanout",
                "clock, CE, reset, bubbles, and observable latency",
                "balanced SRL transport within the incumbent clock footprint",
            ],
            "contraindications": [
                "name-normalized recurrence hypothesis without exact recovery",
                "single boundary or incomplete family closure",
                "placement oracle cannot restore the immutable seed",
            ],
        }
    return {
        **executable,
        **{name: dict(spec) for name, spec in _BACKLOG_TEMPLATE_CATALOG.items()},
    }


def _require_registered_fir_data_cone_bundle(bundle: dict) -> None:
    """Enforce semantic type safety for the local FIR/conv data-cone action.

    This is not a QoR trigger.  Luna chooses when the action is worth trying;
    the backend only ensures that a typed FIR action cannot silently degrade
    into a shallow residual rewrite or take ownership of state/control logic.
    """
    if (
        not isinstance(bundle, dict)
        or bundle.get("kind") != "generating_operator_bridge_bundle"
        or not isinstance(bundle.get("plans"), list)
        or not bundle["plans"]
        or not isinstance(bundle.get("instances"), list)
        or not bundle["instances"]
    ):
        raise ValueError("registered FIR data cone was not recovered")
    for plan in bundle["plans"]:
        terms = plan.get("terms") if isinstance(plan, dict) else None
        if (
            not isinstance(terms, list)
            or len(terms) < 2
            or any(
                not isinstance(term, dict)
                or term.get("kind") != "fixed_product_slice"
                for term in terms
            )
        ):
            raise ValueError(
                "registered FIR data cone requires a product-only semantic frontier"
            )
        if plan.get("output_integration") != "preserve_target_register_v1":
            raise ValueError(
                "registered FIR data cone must preserve the target register control shell"
            )
    recovery = bundle.get("recovery") or {}
    if (
        recovery.get("operator_class")
        != "separately_quantized_constant_product_sum"
        or recovery.get("external_plan_used") is not False
    ):
        raise ValueError("registered FIR data cone semantic replay is incomplete")


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
        allowed_params = _EXECUTION_PARAMS | _TARGET_EXECUTION_PARAMS.get(target, set())
        unknown = unexpected_execution_params(params, allowed_params)
        if unknown:
            return SkillResult.failure(
                before_wns,
                f"operator_rewrite rejected parameters {unknown}",
                failed_dcp,
            )
        max_candidates = params.get("max_candidates", MAX_CANDIDATES)
        target_cap = MAX_CANDIDATES if target == "proved_arithmetic_compressor_v1" else 1
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= target_cap
        ):
            return SkillResult.failure(
                before_wns,
                f"operator_rewrite max_candidates must be in 1..{target_cap}",
                failed_dcp,
            )
        if target == "proved_arithmetic_compressor_v1":
            profiles = params.get("qor_profiles", list(QOR_PROFILES[:max_candidates]))
            if (
                not isinstance(profiles, list)
                or len(profiles) != max_candidates
                or any(not isinstance(item, str) for item in profiles)
                or len(set(profiles)) != len(profiles)
                or any(item not in QOR_PROFILES for item in profiles)
            ):
                return SkillResult.failure(
                    before_wns,
                    "operator_rewrite qor_profiles must contain max_candidates "
                    "unique registered profiles",
                    failed_dcp,
                )
            params = {**params, "qor_profiles": list(profiles)}
        elif "qor_profiles" in params:
            return SkillResult.failure(
                before_wns,
                "operator_rewrite recurrence target does not accept qor_profiles",
                failed_dcp,
            )

        if target == "dsp_srl_recurrence_cut_v1":
            return await self._execute_recurrence(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == "proved_arithmetic_compressor_v1":
            return await self._execute_arithmetic_compressor(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _CONSTRUCTIVE_ACCUMULATOR_TARGET:
            return await self._execute_constructive_accumulator(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target in {"fixed_point_dsp_rebuild_v1", _REGISTERED_FIR_TARGET}:
            return await self._execute_fixed_point_dsp(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _CONSUMER_DRIVEN_TARGET:
            return await self._execute_consumer_driven(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _BANKED_PREDICATE_TARGET:
            return await self._execute_banked_predicate(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target in {_RAM_BOOLEAN_MIDPOINT_TARGET, _CONTROL_PACKET_TARGET}:
            return await self._execute_banked_ram_operator(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _COMMUTATIVE_REASSOCIATION_TARGET:
            return await self._execute_commutative_reassociation(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _REGISTERED_DSP_LANE_TARGET:
            return await self._execute_registered_dsp_lane(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _CONTROL_STATE_EQUATION_TARGET:
            return await self._execute_control_state_equation(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _FF_SRL_BOUNDARY_BRIDGE_TARGET:
            return await self._execute_ff_srl_boundary_bridge(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _MEMORY_FORWARD_TARGET:
            return await self._execute_memory_write_buffer_forward(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _MULTILEVEL_CONTROL_TARGET:
            return await self._execute_multilevel_control_localization(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _REGISTERED_PARITY_TARGET:
            return await self._execute_registered_parity_reassociation(
                mcp, target, params, before_wns, started, failed_dcp
            )
        if target == _REGISTERED_DEAD_STATE_TARGET:
            return await self._execute_registered_dead_state(
                mcp, target, params, before_wns, started, failed_dcp
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

    async def _execute_control_state_equation(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one Luna-selected exact synchronous state-equation family."""
        output = Path(mcp.run_dir) / "operator_rewrite_control_state_equation.dcp"
        route_report = Path(mcp.run_dir) / "control_state_equation_backend.tsv"
        attempts = []
        public_contract = None
        family_ref = str(params.get("family_ref") or "")
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_control_state_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            public_contract = handoff["public_contract"]
            selected = handoff["selected_family"]
            authorization = build_control_state_equation_authorization(
                source_sha256, public_contract, family_ref
            )
            attempts.append({
                "status": "same_seed_state_equation_replayed",
                "mutation_attempted": True,
                "operator_class": "REGISTERED_CONTROL_TO_DATA_STATE_EQUATION",
                "family_ref": family_ref,
                "state_count": selected.get("state_count"),
                "top500_endpoint_count": selected.get("top500_endpoint_count"),
                "top500_path_overlap_count": selected.get(
                    "top500_path_overlap_count"
                ),
                "worst_path_overlap": selected.get("worst_path_overlap"),
                "support_width": selected.get("support_width"),
                "control_set_structure": selected.get("control_set_structure"),
            })
            route_timeout = _framework_owned_route_timeout(params)
            route_facts = _payload(await mcp.call_vivado(
                "apply_control_to_data_state_equation",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "manifest_path": str(handoff["manifest_path"]),
                    "probe_report_path": str(handoff["probe_report_path"]),
                    "family_ref": family_ref,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("control-state routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("control-state candidate identity changed")
            attestation = build_control_state_equation_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "input_wns_ns": route_facts.get("input_wns_ns"),
                "output_wns_ns": route_facts.get("output_wns_ns"),
                "output_whs_ns": route_facts.get("output_whs_ns"),
                "resource_delta": route_facts.get("resource_delta"),
            })
            evidence = {
                "version": 1,
                "action_type": "exact_control_to_data_state_equation",
                "operator_class": "REGISTERED_CONTROL_TO_DATA_STATE_EQUATION",
                "family_ref": family_ref,
                "state_count": selected.get("state_count"),
                "top500_endpoint_count": selected.get("top500_endpoint_count"),
                "top500_path_overlap_count": selected.get(
                    "top500_path_overlap_count"
                ),
                "worst_path_overlap": selected.get("worst_path_overlap"),
                "support_width": selected.get("support_width"),
                "control_set_structure": selected.get("control_set_structure"),
                "truth_table_replayed": True,
                "care_set_support_replayed": True,
                "clock_enable_init_and_observable_cycle_preserved": True,
                "exact_cell_identity_framework_private": True,
                "clock_identity_preserved": True,
                "resource_delta": route_facts.get("resource_delta") or {},
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "exact registered control-to-data state equation rewritten and routed",
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
                summary=(
                    "operator_rewrite control-state candidate=1 "
                    "proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": {
                        "operator_class": public_contract.get("operator_class"),
                        "family_ref": family_ref,
                        "state_count": selected.get("state_count"),
                        "top500_endpoint_count": selected.get(
                            "top500_endpoint_count"
                        ),
                        "top500_path_overlap_count": selected.get(
                            "top500_path_overlap_count"
                        ),
                        "worst_path_overlap": selected.get(
                            "worst_path_overlap"
                        ),
                        "support_width": selected.get("support_width"),
                        "control_set_structure": selected.get(
                            "control_set_structure"
                        ),
                    },
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "output_whs_ns", "family_ref", "selected_state_count",
                            "top500_endpoint_count", "support_width",
                            "top500_path_overlap_count", "worst_path_overlap",
                            "control_set_structure",
                            "resource_delta", "pulse_violations",
                        )
                    },
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "failure_scope": "exact_control_to_data_state_equation",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_ff_srl_boundary_bridge(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one Luna-selected exact direct-FF transport family."""
        output = Path(mcp.run_dir) / "operator_rewrite_ff_srl_boundary_bridge.dcp"
        route_report = Path(mcp.run_dir) / "ff_srl_boundary_bridge_backend.tsv"
        attempts = []
        public_contract = None
        family_ref = str(params.get("family_ref") or "")
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_ff_srl_boundary_bridge_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            public_contract = handoff["public_contract"]
            selected = handoff["selected_family"]
            authorization = build_ff_srl_boundary_bridge_authorization(
                source_sha256, public_contract, family_ref
            )
            attempts.append({
                "status": "same_seed_direct_ff_family_replayed",
                "mutation_attempted": True,
                "operator_class": "DIRECT_FF_CHAIN_TO_SRL_BOUNDARY_BRIDGE",
                "family_ref": family_ref,
                "chain_count": selected.get("chain_count"),
                "chain_length": selected.get("chain_length"),
                "ce_mode": selected.get("ce_mode"),
                "top500_path_overlap_count": selected.get(
                    "top500_path_overlap_count"
                ),
                "top500_endpoint_count": selected.get("top500_endpoint_count"),
                "worst_path_overlap": selected.get("worst_path_overlap"),
                "risk": selected.get("risk"),
            })
            route_timeout = _framework_owned_route_timeout(params)
            route_facts = _payload(await mcp.call_vivado(
                "apply_ff_srl_boundary_bridge",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "manifest_path": str(handoff["manifest_path"]),
                    "probe_report_path": str(handoff["probe_report_path"]),
                    "family_ref": family_ref,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("FF-to-SRL routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("FF-to-SRL candidate identity changed")
            attestation = build_ff_srl_boundary_bridge_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "input_wns_ns": route_facts.get("input_wns_ns"),
                "output_wns_ns": route_facts.get("output_wns_ns"),
                "output_whs_ns": route_facts.get("output_whs_ns"),
                "resource_delta": route_facts.get("resource_delta"),
            })
            evidence = {
                "version": 1,
                "action_type": "ff_srl_boundary_bridge",
                "operator_class": "DIRECT_FF_CHAIN_TO_SRL_BOUNDARY_BRIDGE",
                "family_ref": family_ref,
                "chain_count": selected.get("chain_count"),
                "chain_length": selected.get("chain_length"),
                "ce_mode": selected.get("ce_mode"),
                "top500_path_overlap_count": selected.get(
                    "top500_path_overlap_count"
                ),
                "top500_endpoint_count": selected.get("top500_endpoint_count"),
                "worst_path_overlap": selected.get("worst_path_overlap"),
                "enabled_cycle_latency_preserved": True,
                "hierarchy_boundary_ff_retained": True,
                "exact_cell_identity_framework_private": True,
                "clock_identity_preserved": True,
                "risk": selected.get("risk"),
                "resource_delta": route_facts.get("resource_delta") or {},
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "exact direct-FF transport family replaced by an SRL boundary bridge",
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
                summary="operator_rewrite FF-to-SRL candidate=1 proof=measurement-probe",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": {
                        "operator_class": public_contract.get("operator_class"),
                        "family_ref": family_ref,
                        "chain_count": selected.get("chain_count"),
                        "chain_length": selected.get("chain_length"),
                        "ce_mode": selected.get("ce_mode"),
                        "top500_path_overlap_count": selected.get(
                            "top500_path_overlap_count"
                        ),
                        "top500_endpoint_count": selected.get(
                            "top500_endpoint_count"
                        ),
                        "worst_path_overlap": selected.get("worst_path_overlap"),
                        "risk": selected.get("risk"),
                    },
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "output_whs_ns", "family_ref", "selected_chain_count",
                            "selected_chain_length", "ce_mode",
                            "top500_path_overlap_count", "top500_endpoint_count",
                            "worst_path_overlap", "resource_delta",
                            "pulse_violations",
                        )
                    },
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "failure_scope": "ff_srl_boundary_bridge",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_memory_write_buffer_forward(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one Luna-selected supported same-seed BRAM family."""
        output = Path(mcp.run_dir) / "operator_rewrite_memory_forward.dcp"
        route_report = Path(mcp.run_dir) / "memory_forward_backend.tsv"
        attempts = []
        public_contract = None
        family_ref = str(params.get("family_ref") or "")
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_memory_forward_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            public_contract = handoff["public_contract"]
            selected = handoff["selected_family"]
            attempts.append({
                "status": "same_seed_memory_family_replayed",
                "mutation_attempted": True,
                "operator_class": "MEMORY_WRITE_BUFFER_FORWARD",
                "family_ref": family_ref,
                "data_width": selected.get("data_width"),
                "address_width": selected.get("address_width"),
                "top_path_rank": selected.get("top_path_rank"),
                "write_worst_slack_ns": selected.get("write_worst_slack_ns"),
                "read_worst_slack_ns": selected.get("read_worst_slack_ns"),
                "expected_resource_overhead": selected.get(
                    "expected_resource_overhead"
                ),
                "risk": selected.get("risk"),
            })
            route_timeout = _framework_owned_route_timeout(params)
            route_facts = _payload(await mcp.call_vivado(
                "apply_memory_write_buffer_forward",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "manifest_path": str(handoff["manifest_path"]),
                    "probe_report_path": str(handoff["probe_report_path"]),
                    "family_ref": family_ref,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if route_facts.get("route_checkpoint_cache_reused") is True:
                attempts[-1]["route_checkpoint_cache_reused"] = True
            authorization = route_facts.pop("authorization", None)
            if not isinstance(authorization, dict):
                raise RuntimeError("memory-forward authorization is missing")
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("memory-forward routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("memory-forward candidate identity changed")
            attestation = build_memory_forward_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "input_wns_ns": route_facts.get("input_wns_ns"),
                "output_wns_ns": route_facts.get("output_wns_ns"),
                "output_whs_ns": route_facts.get("output_whs_ns"),
                "resource_delta": route_facts.get("resource_delta"),
            })
            evidence = {
                "version": 1,
                "action_type": "memory_write_buffer_forward",
                "operator_class": "MEMORY_WRITE_BUFFER_FORWARD",
                "family_ref": family_ref,
                "data_width": selected.get("data_width"),
                "address_width": selected.get("address_width"),
                "byte_enable_width": selected.get("byte_enable_width"),
                "write_mode": selected.get("write_mode"),
                "collision_mode": selected.get("collision_mode"),
                "top_path_rank": selected.get("top_path_rank"),
                "write_worst_slack_ns": selected.get("write_worst_slack_ns"),
                "read_worst_slack_ns": selected.get("read_worst_slack_ns"),
                "adjacent_raw_relational_miter_replayed": True,
                "exact_memory_identity_framework_private": True,
                "clock_identity_preserved": True,
                "risk": selected.get("risk"),
                "resource_delta": route_facts.get("resource_delta") or {},
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "one exact BRAM write boundary buffered with registered RAW forwarding",
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
                summary="operator_rewrite memory-forward candidate=1 proof=measurement-probe",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": {
                        "operator_class": public_contract.get("operator_class"),
                        "family_ref": family_ref,
                        "data_width": selected.get("data_width"),
                        "address_width": selected.get("address_width"),
                        "top_path_rank": selected.get("top_path_rank"),
                        "write_worst_slack_ns": selected.get(
                            "write_worst_slack_ns"
                        ),
                        "read_worst_slack_ns": selected.get(
                            "read_worst_slack_ns"
                        ),
                        "expected_resource_overhead": selected.get(
                            "expected_resource_overhead"
                        ),
                        "risk": selected.get("risk"),
                    },
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "output_whs_ns", "family_ref", "new_fdres",
                            "new_luts", "resource_delta", "route_errors",
                            "pulse_violations",
                        )
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "failure_scope": "memory_write_buffer_forward",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_multilevel_control_localization(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one Luna-selected complete registered control DAG family."""
        output = Path(mcp.run_dir) / "operator_rewrite_multilevel_control.dcp"
        route_report = Path(mcp.run_dir) / "multilevel_control_backend.tsv"
        attempts = []
        public_contract = None
        family_ref = str(params.get("family_ref") or "")
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_multilevel_control_localization_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            public_contract = handoff["public_contract"]
            selected = handoff["selected_family"]
            proof_input = public_contract.get("public_contract") or {}
            authorization = build_multilevel_control_localization_authorization(
                source_sha256, proof_input, family_ref
            )
            attempts.append({
                "status": "same_seed_control_family_replayed",
                "mutation_attempted": True,
                "operator_class": (
                    "MULTILEVEL_REGISTERED_CONTROL_DISTRIBUTION"
                ),
                "family_ref": family_ref,
                "logic_depth": selected.get("logic_depth"),
                "closure_cell_count": selected.get("closure_cell_count"),
                "sink_count": selected.get("sink_count"),
                "clock_region_count": selected.get("clock_region_count"),
                "covered_path_count": selected.get("covered_path_count"),
                "earliest_path_rank": selected.get("earliest_path_rank"),
            })
            route_timeout = _framework_owned_route_timeout(params)
            route_facts = _payload(await mcp.call_vivado(
                "apply_multilevel_control_distribution_localization",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "manifest_path": str(handoff["manifest_path"]),
                    "probe_report_path": str(handoff["probe_report_path"]),
                    "family_ref": family_ref,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": route_timeout,
                },
                # The outer Agent deadline is authoritative.  This small RPC
                # margin lets the MCP return structured Tcl timeout evidence
                # without imposing another fixed action cutoff.
                timeout=route_timeout + 60.0,
            ))
            if route_facts.get("route_checkpoint_cache_reused") is True:
                attempts[-1]["route_checkpoint_cache_reused"] = True
            returned_authorization = route_facts.pop("authorization", None)
            if returned_authorization != authorization:
                raise RuntimeError(
                    "control-localization authorization replay changed"
                )
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError(
                    "control-localization routed checkpoint is missing"
                )
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("control-localization candidate identity changed")
            attestation = build_multilevel_control_localization_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "input_wns_ns": route_facts.get("input_wns_ns"),
                "output_wns_ns": route_facts.get("output_wns_ns"),
                "output_whs_ns": route_facts.get("output_whs_ns"),
                "resource_delta": route_facts.get("resource_delta"),
            })
            evidence = {
                "version": 1,
                "action_type": "multilevel_control_distribution_localization",
                "operator_class": (
                    "MULTILEVEL_REGISTERED_CONTROL_DISTRIBUTION"
                ),
                "family_ref": family_ref,
                "logic_depth": selected.get("logic_depth"),
                "closure_cell_count": selected.get("closure_cell_count"),
                "sink_count": selected.get("sink_count"),
                "clock_region_count": selected.get("clock_region_count"),
                "sink_role_counts": selected.get("sink_role_counts"),
                "covered_path_count": selected.get("covered_path_count"),
                "earliest_path_rank": selected.get("earliest_path_rank"),
                "complete_boolean_dag_replayed": True,
                "registered_sink_role_and_cycle_preserved": True,
                "original_generator_and_nonselected_consumers_preserved": True,
                "exact_object_identity_framework_private": True,
                "clock_identity_preserved": True,
                "resource_delta": route_facts.get("resource_delta") or {},
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "complete multilevel registered control DAG localized near sink regions",
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
                summary=(
                    "operator_rewrite multilevel-control candidate=1 "
                    "proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": {
                        "operator_class": public_contract.get("operator_class"),
                        "family_ref": family_ref,
                        "logic_depth": selected.get("logic_depth"),
                        "closure_cell_count": selected.get("closure_cell_count"),
                        "sink_count": selected.get("sink_count"),
                        "clock_region_count": selected.get("clock_region_count"),
                        "covered_path_count": selected.get("covered_path_count"),
                        "earliest_path_rank": selected.get("earliest_path_rank"),
                    },
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "output_whs_ns", "family_ref", "closure_cell_count",
                            "localized_sink_count", "clone_group_count",
                            "new_lut_count", "resource_delta", "route_errors",
                            "pulse_violations", "route_checkpoint_cache_reused",
                        )
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "failure_scope": "multilevel_control_distribution_localization",
                        "remaining_family_exploration_allowed": True,
                        "emitted_checkpoint_preserved_for_resume": (
                            output.is_file() and output.stat().st_size > 0
                        ),
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_registered_parity_reassociation(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one Luna-selected exact registered GF(2) parity family."""
        family_ref = str(params.get("family_ref") or "")
        implementation_mode = str(
            params.get("implementation_mode") or "local_preserve"
        )
        if re.fullmatch(r"registered_parity_[1-9][0-9]*", family_ref) is None:
            return SkillResult.failure(
                before_wns, "registered parity family_ref is invalid", failed_dcp
            )
        if implementation_mode not in {
            "local_preserve", "closure_local_rebasin", "global_quick_rebasin"
        }:
            return SkillResult.failure(
                before_wns,
                "registered parity implementation mode is invalid",
                failed_dcp,
            )
        output = Path(mcp.run_dir) / (
            f"operator_rewrite_{family_ref}_{implementation_mode}.dcp"
        )
        route_report = Path(mcp.run_dir) / (
            f"registered_parity_backend_{family_ref}_{implementation_mode}.tsv"
        )
        attempts = []
        public_contract = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_registered_parity_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            public_contract = handoff["public_contract"]
            selected = handoff["selected_family"]
            proof_input = public_contract.get("public_contract") or {}
            authorization = build_registered_parity_authorization(
                source_sha256, proof_input, family_ref, implementation_mode
            )
            attempts.append({
                "status": "same_seed_parity_family_replayed",
                "mutation_attempted": True,
                "operator_class": "REGISTERED_GF2_PARITY_REDUCTION",
                "family_ref": family_ref,
                "implementation_mode": implementation_mode,
                "logic_depth": selected.get("logic_depth"),
                "closure_cell_count": selected.get("closure_cell_count"),
                "raw_leaf_occurrence_count": selected.get(
                    "raw_leaf_occurrence_count"
                ),
                "odd_leaf_count": selected.get("odd_leaf_count"),
                "canceled_leaf_occurrence_count": selected.get(
                    "canceled_leaf_occurrence_count"
                ),
                "registered_endpoint_count": selected.get(
                    "registered_endpoint_count"
                ),
                "earliest_live_path_rank": selected.get(
                    "earliest_live_path_rank"
                ),
            })
            route_timeout = _framework_owned_route_timeout(params)
            route_facts = _payload(await mcp.call_vivado(
                "apply_registered_parity_reassociation",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "manifest_path": str(handoff["manifest_path"]),
                    "probe_report_path": str(handoff["probe_report_path"]),
                    "family_ref": family_ref,
                    "implementation_mode": implementation_mode,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if route_facts.get("route_checkpoint_cache_reused") is True:
                attempts[-1]["route_checkpoint_cache_reused"] = True
            returned_authorization = route_facts.pop("authorization", None)
            if returned_authorization != authorization:
                raise RuntimeError("registered parity authorization replay changed")
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("registered parity routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("registered parity candidate identity changed")
            attestation = build_registered_parity_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "input_wns_ns": route_facts.get("input_wns_ns"),
                "output_wns_ns": route_facts.get("output_wns_ns"),
                "output_whs_ns": route_facts.get("output_whs_ns"),
                "resource_delta": route_facts.get("resource_delta"),
            })
            evidence = {
                "version": 1,
                "action_type": "registered_gf2_parity_reassociation",
                "operator_class": "REGISTERED_GF2_PARITY_REDUCTION",
                "family_ref": family_ref,
                "implementation_mode": implementation_mode,
                "logic_depth": selected.get("logic_depth"),
                "closure_cell_count": selected.get("closure_cell_count"),
                "raw_leaf_occurrence_count": selected.get(
                    "raw_leaf_occurrence_count"
                ),
                "odd_leaf_count": selected.get("odd_leaf_count"),
                "canceled_leaf_occurrence_count": selected.get(
                    "canceled_leaf_occurrence_count"
                ),
                "registered_endpoint_count": selected.get(
                    "registered_endpoint_count"
                ),
                "complete_gf2_expression_replayed": True,
                "balanced_lut6_tree_emitted": True,
                "destination_register_control_and_cycle_preserved": True,
                "original_root_and_nonselected_consumers_preserved": True,
                "exact_object_identity_framework_private": True,
                "clock_identity_preserved": True,
                "resource_delta": route_facts.get("resource_delta") or {},
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "exact registered GF(2) closure balanced for one live endpoint",
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
                summary=(
                    "operator_rewrite registered-parity candidate=1 "
                    "proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": {
                        "operator_class": public_contract.get("operator_class"),
                        "family_ref": family_ref,
                        "implementation_mode": implementation_mode,
                        "logic_depth": selected.get("logic_depth"),
                        "closure_cell_count": selected.get(
                            "closure_cell_count"
                        ),
                        "odd_leaf_count": selected.get("odd_leaf_count"),
                        "canceled_leaf_occurrence_count": selected.get(
                            "canceled_leaf_occurrence_count"
                        ),
                        "registered_endpoint_count": selected.get(
                            "registered_endpoint_count"
                        ),
                        "earliest_live_path_rank": selected.get(
                            "earliest_live_path_rank"
                        ),
                    },
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "output_whs_ns", "family_ref", "odd_leaf_count",
                            "implementation_mode", "rebasin_cell_count",
                            "rebasin_site_count",
                            "new_lut_count", "old_closure_cell_count",
                            "registered_endpoint_count",
                            "preserved_sibling_count", "resource_delta",
                            "route_errors", "pulse_violations",
                            "route_checkpoint_cache_reused",
                        )
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "implementation_mode": implementation_mode,
                        "failure_scope": "registered_gf2_parity_reassociation",
                        "remaining_family_exploration_allowed": True,
                        "emitted_checkpoint_preserved_for_resume": (
                            output.is_file() and output.stat().st_size > 0
                        ),
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_registered_dead_state(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one complete same-seed output-unobservable state deletion."""
        output = Path(mcp.run_dir) / "operator_rewrite_registered_dead_state.dcp"
        route_report = Path(mcp.run_dir) / "registered_dead_state_backend.tsv"
        attempts = []
        public_contract = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_registered_dead_state_handoff(
                mcp, artifact_sha256=source_sha256
            )
            public_contract = handoff["public_contract"]
            manifest_path = Path(handoff["manifest_path"])
            authorization = build_registered_dead_state_authorization(
                source_sha256, public_contract
            )
            summary = public_contract.get("summary") or {}
            attempts.append({
                "status": "same_seed_observable_closure_replayed",
                "mutation_attempted": True,
                "operator_class": "REGISTERED_OBSERVABLE_DEAD_STATE_FAMILY",
                "dead_registered_state_count": summary.get(
                    "dead_registered_state_count"
                ),
                "mutation_target_cell_count": summary.get(
                    "mutation_target_cell_count"
                ),
            })
            route_timeout = _framework_owned_route_timeout(params)
            route_facts = _payload(await mcp.call_vivado(
                "apply_registered_dead_state_elimination",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "manifest_path": str(manifest_path),
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("registered dead-state routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("registered dead-state candidate identity changed")
            attestation = build_registered_dead_state_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "selected_dsp_count": route_facts.get("selected_dsp_count"),
                "selected_ff_count": route_facts.get("selected_ff_count"),
                "resource_delta": route_facts.get("resource_delta"),
            })
            evidence = {
                "version": 1,
                "action_type": "registered_dead_state_elimination",
                "operator_class": "REGISTERED_OBSERVABLE_DEAD_STATE_FAMILY",
                "physical_cell_count": summary.get("physical_cell_count"),
                "observable_physical_cell_count": summary.get(
                    "observable_physical_cell_count"
                ),
                "dead_physical_cell_count": summary.get(
                    "dead_physical_cell_count"
                ),
                "dead_registered_state_count": summary.get(
                    "dead_registered_state_count"
                ),
                "mutation_target_cell_count": summary.get(
                    "mutation_target_cell_count"
                ),
                "selected_dsp_count": route_facts.get("selected_dsp_count"),
                "selected_ff_count": route_facts.get("selected_ff_count"),
                "resource_delta": route_facts.get("resource_delta") or {},
                "complete_output_observable_closure_replayed": True,
                "exact_cell_identity_framework_private": True,
                "clock_identity_preserved": True,
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "output-unobservable registered state removed and routed",
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
                summary=(
                    "operator_rewrite dead-state candidate=1 "
                    "proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": {
                        "operator_class": public_contract.get("operator_class"),
                        "summary": {
                            key: summary.get(key)
                            for key in (
                                "physical_cell_count",
                                "observable_physical_cell_count",
                                "dead_physical_cell_count",
                                "dead_registered_state_count",
                                "total_dsp_count", "dead_dsp_count",
                                "partial_dead_dsp_parent_count",
                                "mutation_target_cell_count",
                                "mutation_eligible", "dead_type_counts",
                            )
                        },
                    },
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "mutation_target_cell_count", "selected_dsp_count",
                            "selected_ff_count", "selected_lut_count",
                            "selected_carry_count", "resource_delta", "worst_path",
                        )
                    },
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "failure_scope": "registered_observable_dead_state",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_registered_dsp_lane(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Route one complete anonymous DSP-product sequential transaction."""
        output = Path(mcp.run_dir) / "operator_rewrite_registered_dsp_lane.dcp"
        route_report = Path(mcp.run_dir) / "registered_dsp_lane_backend.tsv"
        attempts = []
        public_contract = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            route_timeout = _framework_owned_route_timeout(params)
            attempts.append({
                "status": "fresh_complete_product_transaction_recovery_started",
                "mutation_attempted": True,
                "operator_class": "REGISTERED_DSP_PRODUCT_TRANSACTION",
            })
            route_facts = _payload(await mcp.call_vivado(
                "apply_registered_dsp_lane_rebuild",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if not output.is_file():
                raise RuntimeError("registered DSP lane routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("registered DSP lane candidate identity changed")
            public_contract = {
                key: route_facts.get(key)
                for key in (
                    "operator_class", "lane_count", "word_width",
                    "complete_p_to_ff_mapping_recovered",
                    "clock_ce_reset_word_recovered",
                    "pcout_and_side_consumers_closed",
                    "observable_cycle_preserved", "live_wall_path_count",
                    "sibling_present", "sibling_rank", "sibling_slack_ns",
                    "new_clocked_dsp_count", "clock_region_count",
                    "selected_dsp_already_on_recovered_clock_count",
                    "newly_activated_dsp_clock_site_count",
                    "removed_external_ff_count",
                    "retained_reset_history_ff_count",
                    "reset_correction_lut_count", "resource_delta",
                    "clock_edge_inverted",
                )
            }
            authorization = build_registered_dsp_lane_authorization(
                source_sha256, public_contract
            )
            attestation = build_registered_dsp_lane_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1].update({
                "status": "routed_measurement_candidate",
                "lane_count": route_facts.get("lane_count"),
                "word_width": route_facts.get("word_width"),
                "live_wall_path_count": route_facts.get("live_wall_path_count"),
                "sibling_rank": route_facts.get("sibling_rank"),
                "sibling_present": route_facts.get("sibling_present"),
                "new_clocked_dsp_count": route_facts.get("new_clocked_dsp_count"),
                "selected_dsp_already_on_recovered_clock_count": route_facts.get(
                    "selected_dsp_already_on_recovered_clock_count"
                ),
                "newly_activated_dsp_clock_site_count": route_facts.get(
                    "newly_activated_dsp_clock_site_count"
                ),
                "clock_region_count": route_facts.get("clock_region_count"),
            })
            evidence = {
                "version": 1,
                "action_type": "registered_dsp_lane_rebuild",
                "operator_class": "REGISTERED_DSP_PRODUCT_TRANSACTION",
                "lane_count": route_facts.get("lane_count"),
                "word_width": route_facts.get("word_width"),
                "live_wall_path_count": route_facts.get("live_wall_path_count"),
                "sibling_rank": route_facts.get("sibling_rank"),
                "sibling_slack_ns": route_facts.get("sibling_slack_ns"),
                "new_clocked_dsp_count": route_facts.get("new_clocked_dsp_count"),
                "selected_dsp_already_on_recovered_clock_count": route_facts.get(
                    "selected_dsp_already_on_recovered_clock_count"
                ),
                "newly_activated_dsp_clock_site_count": route_facts.get(
                    "newly_activated_dsp_clock_site_count"
                ),
                "clock_region_count": route_facts.get("clock_region_count"),
                "complete_data_and_control_transaction_replayed": True,
                "clock_identity_preserved": True,
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "complete DSP product-register absorption routed candidate",
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
                summary="operator_rewrite registered DSP lane candidate=1 proof=measurement-probe",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "lane_count", "word_width", "live_wall_path_count",
                            "sibling_rank", "sibling_slack_ns",
                            "sibling_present",
                            "new_clocked_dsp_count", "clock_region_count",
                            "selected_dsp_already_on_recovered_clock_count",
                            "newly_activated_dsp_clock_site_count",
                            "removed_external_ff_count",
                            "retained_reset_history_ff_count",
                            "reset_correction_lut_count", "resource_delta",
                            "worst_path",
                        )
                    },
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "failure_scope": "registered_dsp_product_transaction",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_banked_predicate(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Recover and route one anonymous banked data/control specialization."""
        output = Path(mcp.run_dir) / "operator_rewrite_banked_predicate.dcp"
        # The read-only Luna probe already recovered the exact same-seed
        # contract.  The MCP accepts this report only when its source SHA,
        # report digest, and probe ABI cache all replay exactly; otherwise it
        # falls back to a fresh recovery here.
        # Probe artifacts belong to the stable server run root.  During skill
        # execution ``mcp.run_dir`` points at the per-action directory; using
        # it here silently misses the exact same-seed probe cache and repeats
        # the expensive Vivado recovery.
        probe_root = Path(
            getattr(mcp, "_server_run_dir", None) or mcp.run_dir
        )
        probe_report = probe_root / "banked_select_predicate_probe.tsv"
        route_report = Path(mcp.run_dir) / "banked_predicate_backend.tsv"
        attempts = []
        public_contract = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            route_timeout = _framework_owned_route_timeout(params)
            recovered = _payload(await mcp.call_vivado(
                "recover_banked_select_predicate_contract",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "report_path": str(probe_report),
                    "timeout_s": min(900.0, route_timeout),
                },
                timeout=min(1200.0, route_timeout + 300.0),
            ))
            if (
                recovered.get("status") != "success"
                or recovered.get("structural_boundary_recovered") is not True
                or recovered.get("mutation_eligible") is not True
            ):
                raise ValueError("banked predicate data/control contract did not close")
            public_contract = recovered.get("contract")
            authorization = build_banked_predicate_authorization(
                source_sha256, public_contract
            )
            attempts.append({
                "status": "fresh_data_control_contract_recovered",
                "probe_cache_reused": bool(recovered.get("probe_cache_reused")),
                "mutation_attempted": False,
                "operator_class": "BANKED_SELECT_PREDICATE_UPDATE",
                "bank_count": (public_contract.get("control_operator") or {}).get(
                    "bank_count"
                ),
                "word_width": (public_contract.get("data_operator") or {}).get(
                    "word_width"
                ),
            })
            attempts[-1]["mutation_attempted"] = True
            route_facts = _payload(await mcp.call_vivado(
                "apply_banked_select_predicate_update",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if not output.is_file():
                raise RuntimeError("banked predicate routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("banked predicate candidate identity changed")
            attestation = build_banked_predicate_emitted_attestation(
                authorization, candidate_sha256, route_facts
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1]["status"] = "routed_measurement_candidate"
            evidence = {
                "version": 1,
                "action_type": "bank_local_predicate_specialization",
                "operator_class": "BANKED_SELECT_PREDICATE_UPDATE",
                "bank_count": route_facts.get("bank_count"),
                "word_width": route_facts.get("word_width"),
                "comparator_lut_count": route_facts.get(
                    "comparator_lut_count"
                ),
                "selected_word_input_count": route_facts.get(
                    "selected_word_input_count"
                ),
                "global_consumer_preserved": True,
                "alternate_init_cofactors_preserved": True,
                "clock_ce_reset_state_unchanged": True,
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "bank-local compare-before-select routed candidate",
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
                summary=(
                    "operator_rewrite banked predicate candidate=1 "
                    "proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "bank_count", "word_width", "comparator_lut_count",
                            "selected_word_input_count", "period_ns",
                            "input_wns_ns", "output_wns_ns", "worst_path",
                        )
                    },
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "failure_scope": "banked_select_predicate_family",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_banked_ram_operator(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Recover both RAM data/control semantics, then route Luna's target."""
        data_target = target == _RAM_BOOLEAN_MIDPOINT_TARGET
        output = Path(mcp.run_dir) / (
            "operator_rewrite_ram_boolean_midpoint.dcp"
            if data_target else "operator_rewrite_control_packet.dcp"
        )
        probe_root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
        probe_report = probe_root / "banked_ram_operator_probe.tsv"
        route_report = Path(mcp.run_dir) / "banked_ram_operator_backend.tsv"
        attempts = []
        public_contract = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            route_timeout = _framework_owned_route_timeout(params)
            recovered = _payload(await mcp.call_vivado(
                "recover_banked_ram_operator_contract",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "report_path": str(probe_report),
                    "timeout_s": min(900.0, route_timeout),
                },
                timeout=min(1200.0, route_timeout + 300.0),
            ))
            if (
                recovered.get("status") != "success"
                or recovered.get("structural_boundary_recovered") is not True
                or recovered.get("mutation_eligible") is not True
            ):
                raise ValueError("banked RAM data/control contract did not close")
            public_contract = recovered.get("contract")
            candidate_actions = (
                (recovered.get("action_assessment") or {}).get("candidate_actions")
                or []
            )
            if not any(
                isinstance(item, dict) and item.get("target") == target
                for item in candidate_actions
            ):
                raise ValueError(
                    "selected banked RAM backend is unsupported by the current live contract"
                )
            authorization = build_banked_ram_operator_authorization(
                source_sha256, public_contract, target
            )
            attempts.append({
                "status": "fresh_banked_ram_data_control_contract_recovered",
                "probe_cache_reused": bool(recovered.get("probe_cache_reused")),
                "mutation_attempted": True,
                "operator_class": "BANKED_RAM_DATA_CONTROL_OPERATOR",
                "selected_target": target,
                "data_operator_count": (
                    (public_contract.get("data_operator") or {}).get("operator_count")
                ),
                "control_packet_count": (
                    (public_contract.get("control_operator") or {}).get("packet_count")
                ),
            })
            mcp_tool = (
                "apply_registered_ram_boolean_midpoint"
                if data_target else "apply_registered_control_packet_localization"
            )
            route_facts = _payload(await mcp.call_vivado(
                mcp_tool,
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "output_dcp": str(output),
                    "report_path": str(route_report),
                    "probe_report_path": str(probe_report),
                    "timeout_s": min(4800.0, route_timeout + 900.0),
                },
                timeout=min(5100.0, route_timeout + 1200.0),
            ))
            if not output.is_file():
                raise RuntimeError("banked RAM routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                raise RuntimeError("banked RAM candidate identity changed")
            attestation = build_banked_ram_operator_emitted_attestation(
                authorization, candidate_sha256, route_facts, target
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1]["status"] = "routed_measurement_candidate"
            evidence = {
                "version": 1,
                "action_type": target,
                "operator_class": "BANKED_RAM_DATA_CONTROL_OPERATOR",
                "data_operator_count": (
                    (public_contract.get("data_operator") or {}).get("operator_count")
                ),
                "control_packet_count": (
                    (public_contract.get("control_operator") or {}).get("packet_count")
                ),
                "exact_boolean_contract_preserved": True,
                "registered_state_and_control_preserved": True,
                "architectural_latency_delta_cycles": 0,
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "banked RAM data/control routed measurement candidate",
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
                summary=(
                    f"operator_rewrite {target} candidate=1 proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "route_facts": {
                        key: route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "candidate_count", "moved_operator_count",
                            "registered_source_count", "localized_sink_count",
                            "covered_path_count", "earliest_path_rank",
                        )
                    },
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "failure_scope": "banked_ram_data_control_operator",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_commutative_reassociation(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Recover value/control semantics, then measure private pair candidates."""
        probe_root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
        probe_report = probe_root / "commutative_reassociation_probe.tsv"
        contract_path = probe_report.with_name(probe_report.name + ".contract.json")
        attempts: list[dict[str, object]] = []
        public_contract = None
        best: tuple[float, Path, dict, dict] | None = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            route_timeout = _framework_owned_route_timeout(params)
            deadline = _framework_owned_execution_deadline(params)
            reserve = _framework_owned_commit_reserve(params)
            recovered = _payload(await mcp.call_vivado(
                "recover_commutative_reassociation_contract",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "report_path": str(probe_report),
                    "timeout_s": min(900.0, route_timeout),
                },
                timeout=min(1800.0, route_timeout + 600.0),
            ))
            if (
                recovered.get("status") != "success"
                or recovered.get("mutation_eligible") is not True
                or recovered.get("primitive_replay_passed") is not True
                or recovered.get("clock_ce_reset_transaction_closed") is not True
            ):
                raise ValueError("commutative value/control contract did not close")
            public_contract = {
                key: value
                for key, value in recovered.items()
                if key not in {"_private_contract_digest"}
            }
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            authorization = build_commutative_reassociation_authorization(
                source_sha256, contract
            )
            candidate_count = recovered.get("candidate_pair_count")
            if (
                isinstance(candidate_count, bool)
                or not isinstance(candidate_count, int)
                or candidate_count < 1
            ):
                raise ValueError("commutative pair inventory is empty")

            for candidate_index in range(candidate_count):
                if deadline is not None:
                    remaining = float(deadline) - time.monotonic() - reserve
                    if remaining <= 1.0:
                        attempts.append({
                            "candidate_ordinal": candidate_index + 1,
                            "status": "not_started_budget_reserve",
                        })
                        break
                    candidate_timeout = min(4800.0, route_timeout, remaining)
                else:
                    candidate_timeout = min(4800.0, route_timeout)
                output = Path(mcp.run_dir) / (
                    f"operator_rewrite_commutative_{candidate_index + 1:02d}.dcp"
                )
                route_report = Path(mcp.run_dir) / (
                    f"commutative_reassociation_{candidate_index + 1:02d}.tsv"
                )
                attempt: dict[str, object] = {
                    "candidate_ordinal": candidate_index + 1,
                    "status": "started",
                    "mutation_attempted": True,
                }
                attempts.append(attempt)
                try:
                    route_facts = _cached_commutative_route_facts(
                        output,
                        route_report,
                        source_sha256=source_sha256,
                        candidate_index=candidate_index,
                    )
                    if route_facts is None:
                        route_facts = _payload(await mcp.call_vivado(
                            "apply_commutative_operand_reassociation",
                            {
                                "dcp_path": str(input_dcp),
                                "source_dcp_sha256": source_sha256,
                                "output_dcp": str(output),
                                "report_path": str(route_report),
                                "probe_report_path": str(probe_report),
                                "contract_path": str(contract_path),
                                "candidate_index": candidate_index,
                                "timeout_s": candidate_timeout,
                            },
                            timeout=min(5100.0, candidate_timeout + 300.0),
                        ))
                    else:
                        attempt["route_checkpoint_cache_reused"] = True
                    if not output.is_file():
                        raise RuntimeError("commutative routed checkpoint is missing")
                    candidate_sha256 = sha256_file(output)
                    if route_facts.get("candidate_dcp_sha256") != candidate_sha256:
                        raise RuntimeError("commutative candidate identity changed")
                    attestation = build_commutative_reassociation_emitted_attestation(
                        authorization, candidate_sha256, route_facts
                    )
                    proof = build_operator_candidate_proof(
                        target,
                        authorization,
                        candidate_sha256,
                        emitted_topology_attestation=attestation,
                    )
                    output_wns = float(route_facts["output_wns_ns"])
                    attempt.update({
                        "status": "routed_measurement_candidate",
                        "output_wns_ns": output_wns,
                        "delta_wns_ns": output_wns - before_wns,
                        "changed_net_count": route_facts.get("changed_net_count"),
                        "moved_pin_count": route_facts.get("moved_pin_count"),
                    })
                    if best is None or output_wns > best[0]:
                        best = (output_wns, output, route_facts, proof)
                    # A pair's physical value is not predictable from symmetry or
                    # Manhattan distance.  Continue while the framework deadline
                    # permits and retain the best routed pair, including negative
                    # evidence for the next decision round.
                except Exception as exc:  # noqa: BLE001
                    attempt.update({
                        "status": "candidate_failed",
                        "reason": str(exc),
                    })

            if best is None:
                raise RuntimeError("no commutative reassociation candidate routed")
            best_wns, best_output, best_route_facts, best_proof = best
            evidence = {
                "version": 1,
                "action_type": target,
                "operator_class": "REGISTERED_COMMUTATIVE_MODULAR_REDUCTION",
                "word_width": recovered.get("word_width"),
                "operand_count": recovered.get("operand_count"),
                "primitive_replay_passed": True,
                "clock_ce_reset_transaction_closed": True,
                "complete_operand_buses_only": True,
                "architectural_latency_delta_cycles": 0,
                "candidate_identity_private": True,
                "formal_whole_dcp_equivalence": False,
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                best_output,
                "commutative complete-bus routed measurement candidate",
                evidence,
                best_proof,
            )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=best_output,
                summary=(
                    "operator_rewrite commutative reassociation "
                    f"measured={sum(item.get('status') == 'routed_measurement_candidate' for item in attempts)}"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "selected_route_facts": {
                        key: best_route_facts.get(key)
                        for key in (
                            "period_ns", "input_wns_ns", "output_wns_ns",
                            "changed_net_count", "moved_pin_count",
                            "latency_delta_cycles",
                        )
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=(skill_output,),
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "failure_scope": "commutative_value_control_or_pair_route",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted")) for item in attempts
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_consumer_driven(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Execute only the private bundle produced by the current live probe."""
        attempts = []
        output = Path(mcp.run_dir) / "operator_rewrite_consumer_driven.dcp"
        family_ref = str(params.get("family_ref") or "")
        public_contract = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_consumer_rewrite_handoff(
                mcp,
                artifact_sha256=source_sha256,
                family_ref=family_ref,
            )
            bundle = handoff["bundle"]
            public_contract = handoff["public_contract"]
            contract_sha256 = str(handoff.get("contract_sha256") or "")
            authorization = build_consumer_rewrite_authorization(
                source_sha256, contract_sha256, bundle
            )
            bundle_path = Path(mcp.run_dir) / "consumer_rewrite_bundle.private.json"
            bundle_path.write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )
            attempts.append({
                "status": "live_consumer_contract_resolved",
                "family_ref": family_ref,
                "mutation_attempted": False,
                "action_type": "consumer_driven_registered_arithmetic_rewrite",
            })
            route_timeout = _framework_owned_route_timeout(params)
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            synthesis_dir = Path(mcp.run_dir) / "consumer_rewrite_synthesis"
            synthesis = _payload(await mcp.call_vivado(
                "synthesize_registered_product_modules",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "bundle_path": str(bundle_path),
                    "work_dir": str(synthesis_dir),
                },
                timeout=900.0,
            ))
            module_dcps = synthesis.get("module_dcps")
            if not isinstance(module_dcps, dict) or not module_dcps:
                raise RuntimeError("consumer rewrite synthesis returned no modules")
            inserted = Path(mcp.run_dir) / "consumer_rewrite_inserted.dcp"
            attempts[-1]["mutation_attempted"] = True
            insertion = _payload(await mcp.call_rw(
                "insert_registered_product_modules",
                {
                    "bundle_path": str(bundle_path),
                    "module_dcps": module_dcps,
                    "output_dcp": str(inserted),
                },
                timeout=900.0,
            ))
            if insertion.get("source_dcp_sha256") != source_sha256:
                raise RuntimeError("consumer rewrite insertion source identity changed")
            route_facts = _payload(await mcp.call_vivado(
                "route_registered_product_bundle",
                {
                    "inserted_dcp": str(inserted),
                    "source_dcp": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "bundle_path": str(bundle_path),
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                    "physical_profile": "semantic_rewrite",
                },
                timeout=route_timeout + 1200.0,
            ))
            if not output.is_file():
                raise RuntimeError("consumer rewrite routed checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("consumer rewrite route candidate identity changed")
            attestation = build_registered_product_emitted_attestation(
                authorization,
                candidate_sha256,
                route_facts,
                template=target,
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1]["status"] = "routed_measurement_candidate"
            evidence = {
                "version": 1,
                "action_type": "consumer_driven_registered_arithmetic_rewrite",
                "family_ref": family_ref,
                "producer_registered_width": public_contract.get(
                    "producer_registered_width"
                ),
                "observable_output_word_widths": public_contract.get(
                    "observable_output_word_widths"
                ) or [],
                "pipeline_depth": public_contract.get("pipeline_depth"),
                "source_contract_authority": "measurement_probe_only",
                "formal_source_equivalence": False,
                "acceptance_authority": "central_measurement_only",
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                "consumer-driven registered arithmetic rewrite routed candidate",
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
                summary=(
                    "operator_rewrite consumer-driven candidate=1 "
                    "proof=measurement-probe"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "public_contract": public_contract,
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
                    "public_contract": public_contract,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "failure_scope": "selected_family_only",
                        "remaining_family_exploration_allowed": True,
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_fixed_point_dsp(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        attempts = []
        family_ref = ""
        target_width = None
        backend_summary = None
        generating_recovery = {
            "status": "not_attempted",
            "failure_scope": "selected_family_only",
            "remaining_family_exploration_allowed": True,
        }
        fir_data_cone = target == _REGISTERED_FIR_TARGET
        output = Path(mcp.run_dir) / (
            "operator_rewrite_registered_fir_data_cone.dcp"
            if fir_data_cone else "operator_rewrite_fixed_point_dsp.dcp"
        )
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            family_ref = str(params.get("family_ref") or "")
            cached = _find_registered_checkpoint(
                input_dcp,
                source_sha256=source_sha256,
                family_ref=family_ref,
            )
            try:
                handoff = resolve_sequential_family_handoff(
                    mcp, artifact_sha256=source_sha256, family_ref=family_ref
                )
            except ValueError:
                if cached is None:
                    raise
                handoff = {}
            route_timeout = _framework_owned_route_timeout(params)
            target_stem = str(cached[1]["target_stem"]) if cached else ""
            # Closure facts are restartable, but mutation always needs a live
            # RapidWright design loaded from the exact authoritative seed.
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            if handoff:
                sequential = _payload(await mcp.call_rw(
                    "recover_sequential_operator_contract",
                    {
                        "critical_paths_data": handoff["critical_paths_data"],
                        "candidate_id": handoff["candidate_id"],
                    },
                    timeout=600.0,
                ))
                target_stem, target_width = _compressor_target_stem(
                    sequential, source_sha256, handoff["candidate_id"]
                )
                # A persisted complete closure is keyed to its expanded
                # observable output bank, while the live handoff names the
                # selected 32-bit entry word.  The exact source and trusted
                # candidate ID bind these two views; comparing their stems
                # would discard a valid restart checkpoint.
                if (
                    cached is not None
                    and cached[1].get("candidate_id")
                    != handoff["candidate_id"]
                ):
                    cached = None
            attempts.append({
                "family_ref": family_ref,
                "target_width": target_width,
                "status": (
                    "fresh_contract_recovered" if handoff
                    else "persistent_contract_resumed"
                ),
                "mutation_attempted": False,
                "action_type": "registered_product_closure_rewrite",
            })
            if cached is not None:
                closure, _, closure_dir = cached
                checkpoint_dir = closure_dir
                attempts[-1]["screen_cache_reused"] = True
                attempts[-1]["screen_cache_scope"] = (
                    "persistent_checkpoint" if handoff
                    else "persistent_checkpoint_resume"
                )
            else:
                checkpoint_dir = _registered_checkpoint_dir(
                    input_dcp,
                    source_sha256=source_sha256,
                    candidate_id=handoff["candidate_id"],
                    target_stem=target_stem,
                )
                closure_dir = Path(mcp.run_dir) / "operator_registered_closure"
                if closure_dir.exists() and (
                    closure_dir.is_symlink() or not closure_dir.is_dir()
                ):
                    raise RuntimeError(
                        "operator registered closure path is not a resumable directory"
                    )
                extraction = await _extract_registered_closure_with_resume(
                    mcp,
                    {
                        "dcp_path": str(input_dcp),
                        "source_dcp_sha256": source_sha256,
                        "target_stem": target_stem,
                        "output_dir": str(closure_dir),
                        "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
                        "word_width": target_width,
                    },
                )
                if (
                    extraction.get("read_only_after_restore") is not True
                    or extraction.get("source_checkpoint_reopened") is not True
                ):
                    raise RuntimeError(
                        "registered closure extraction did not restore the seed"
                    )
                closure = RegisteredClosure.load(closure_dir)
                _write_registered_screen_cache(
                    closure_dir,
                    source_sha256=source_sha256,
                    family_ref=family_ref,
                    candidate_id=handoff["candidate_id"],
                    target_stem=target_stem,
                    closure=closure,
                )
                _publish_registered_checkpoint(closure_dir, checkpoint_dir)
                attempts[-1]["screen_cache_reused"] = False
                attempts[-1]["screen_cache_scope"] = "fresh_published"
            # The closure cache stores structural extraction, not mutable
            # alias state.  Restore the exact-source CE relation before
            # rebuilding the primary bank or proving peer banks.
            control_alias_proof = await _install_registered_ce_aliases(
                mcp, closure, source_sha256
            )
            if fir_data_cone:
                try:
                    bundle = _load_registered_fir_bundle_cache(
                        checkpoint_dir,
                        source_sha256=source_sha256,
                        closure=closure,
                    )
                    if bundle is None:
                        bundle = build_generating_operator_bridge_bundle(closure)
                        _write_registered_fir_bundle_cache(
                            checkpoint_dir,
                            source_sha256=source_sha256,
                            closure=closure,
                            bundle=bundle,
                        )
                    _require_registered_fir_data_cone_bundle(bundle)
                except ValueError as exc:
                    generating_recovery = {
                        "status": "rejected",
                        "reason": str(exc)[:500],
                        "failure_scope": "selected_family_only",
                        "remaining_family_exploration_allowed": True,
                        "alternate_contract_used": False,
                    }
                    raise ValueError(
                        "registered FIR generating operator rejected: "
                        f"{exc}"
                    ) from exc
                generating_recovery = {
                    "status": "recovered",
                    "failure_scope": "selected_family_only",
                    "remaining_family_exploration_allowed": True,
                    "alternate_contract_used": False,
                }
            else:
                try:
                    bundle = build_generating_operator_bridge_bundle(closure)
                except ValueError as exc:
                    generating_recovery = {
                        "status": "rejected",
                        "reason": str(exc)[:500],
                        "failure_scope": "selected_family_only",
                        "remaining_family_exploration_allowed": True,
                        "alternate_contract_used": True,
                        "alternate_contract": "registered_product_bundle",
                    }
                    bundle = build_registered_product_bundle(closure)
                else:
                    generating_recovery = {
                        "status": "recovered",
                        "failure_scope": "selected_family_only",
                        "remaining_family_exploration_allowed": True,
                        "alternate_contract_used": False,
                    }
            generating_bridge = (
                bundle.get("kind") == "generating_operator_bridge_bundle"
            )
            accepted_closure_stats = [{
                "node_count": len(closure.records),
                "root_count": len(closure.roots()),
            }]
            covered_root_stems = {
                record.root_stem for record in closure.roots()
            }
            replica_discovery = {
                "status": "not_available_without_fresh_handoff",
                "recovered_replica_count": 1,
                "accepted_bank_count": 1,
                "rejections": [],
            }
            # A registered FIR data-cone target is a generating bridge just
            # like the generic fixed-point target.  Keep the proof-driven
            # replica expansion available to both: each peer is independently
            # recovered, replayed and merged before it can be emitted.  The
            # previous target-specific exclusion made a full action silently
            # discard replicas already proved by the read-only recovery.
            if generating_bridge and handoff:
                replica_facts = _payload(await mcp.call_rw(
                    "recover_sequential_operator_replicas",
                    {
                        "critical_paths_data": handoff["critical_paths_data"],
                        "candidate_id": handoff["candidate_id"],
                        "max_replicas": 64,
                    },
                    timeout=900.0,
                ))
                if (
                    replica_facts.get("kind")
                    != "sequential_operator_replica_family_facts"
                    or replica_facts.get("read_only") is not True
                    or replica_facts.get("fresh_rediscovery") is not True
                    or replica_facts.get("mutation_eligible") is not False
                    or not isinstance(replica_facts.get("replicas"), list)
                ):
                    raise RuntimeError(
                        "registered operator replica recovery is incomplete"
                    )
                # Persist the read-only discovery before any expensive Vivado
                # closure screen.  A timeout or interrupted proof can then be
                # audited and resumed without losing which anonymous live
                # candidates were considered.  This artifact is framework
                # audit state and is never exposed to the LLM or Knowledge.
                (Path(mcp.run_dir) / "registered_replica_candidates.json").write_text(
                    json.dumps(replica_facts, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                bank_bundles = [bundle]
                seen_target_stems = {target_stem}
                rejections = []
                ordered_replicas = _order_replica_screen_candidates(
                    replica_facts["replicas"],
                    selected_target_stem=target_stem,
                    cached_candidate_ids=_registered_checkpoint_candidate_ids(
                        input_dcp, source_sha256=source_sha256
                    ),
                )
                for replica_ordinal, replica in ordered_replicas:
                    try:
                        peer_stem, peer_width, peer_candidate_id = (
                            _replica_target_stem(replica)
                        )
                        if peer_stem in seen_target_stems:
                            continue
                        seen_target_stems.add(peer_stem)
                        if peer_stem in covered_root_stems:
                            rejections.append({
                                "status": "already_covered_by_observable_bank",
                            })
                            continue
                        peer_cached = _find_registered_checkpoint_by_candidate(
                            input_dcp,
                            source_sha256=source_sha256,
                            candidate_id=peer_candidate_id,
                        )
                        if peer_cached is not None:
                            peer_closure = peer_cached[0]
                            peer_cache_scope = "persistent_checkpoint_resume"
                        else:
                            peer_dir = Path(mcp.run_dir) / (
                                "operator_registered_closure_replica_"
                                f"{replica_ordinal:02d}"
                            )
                            extraction = await _extract_registered_closure_with_resume(
                                mcp,
                                {
                                    "dcp_path": str(input_dcp),
                                    "source_dcp_sha256": source_sha256,
                                    "target_stem": peer_stem,
                                    "output_dir": str(peer_dir),
                                    "max_stage_depth": (
                                        _REGISTERED_PRODUCT_MAX_STAGE_DEPTH
                                    ),
                                    "word_width": peer_width,
                                },
                            )
                            if (
                                extraction.get("read_only_after_restore") is not True
                                or extraction.get("source_checkpoint_reopened") is not True
                            ):
                                raise RuntimeError(
                                    "replica closure extraction did not restore the seed"
                                )
                            peer_closure = RegisteredClosure.load(peer_dir)
                            _write_registered_screen_cache(
                                peer_dir,
                                source_sha256=source_sha256,
                                family_ref=(
                                    "replica_of:" + family_ref
                                ),
                                candidate_id=peer_candidate_id,
                                target_stem=peer_stem,
                                closure=peer_closure,
                                replica_of_family_ref=family_ref,
                            )
                            peer_checkpoint = _registered_checkpoint_dir(
                                input_dcp,
                                source_sha256=source_sha256,
                                candidate_id=peer_candidate_id,
                                target_stem=peer_stem,
                            )
                            _publish_registered_checkpoint(
                                peer_dir, peer_checkpoint
                            )
                            peer_cache_scope = "fresh_published"
                        await _install_registered_ce_aliases(
                            mcp, peer_closure, source_sha256
                        )
                        peer_bundle = build_generating_operator_bridge_bundle(
                            peer_closure
                        )
                        merged = merge_generating_operator_bridge_bundles(
                            [*bank_bundles, peer_bundle]
                        )
                        prior_bank_count = len(bank_bundles)
                        merged_bank_count = int(
                            merged["recovery"]["bank_count"]
                        )
                        if merged_bank_count == prior_bank_count:
                            rejections.append({
                                "status": "duplicate_observable_bank",
                                "cache_scope": peer_cache_scope,
                            })
                            continue
                        if merged_bank_count != prior_bank_count + 1:
                            raise RuntimeError(
                                "replica bundle merge changed bank count unexpectedly"
                            )
                        bank_bundles.append(peer_bundle)
                        covered_root_stems.update(
                            record.root_stem for record in peer_closure.roots()
                        )
                        accepted_closure_stats.append({
                            "node_count": len(peer_closure.records),
                            "root_count": len(peer_closure.roots()),
                        })
                        bundle = merged
                        # Keep screening the remaining anonymous live replicas.
                        # Each peer still has to pass its own fresh closure,
                        # replay and merge proof; stopping after the first proved
                        # peer can leave the selected generating operator only
                        # partially reconstructed and make the next semantic
                        # layer unreachable.
                    except (RuntimeError, ValueError) as replica_exc:
                        rejections.append({
                            "status": "semantic_replica_rejected",
                            "reason": str(replica_exc)[:500],
                        })
                bundle = merge_generating_operator_bridge_bundles(bank_bundles)
                replica_discovery = {
                    "status": "complete",
                    "recovered_replica_count": int(
                        replica_facts.get("replica_count", 0)
                    ),
                    "exact_cone_replica_count": int(
                        replica_facts.get("exact_cone_replica_count", 0)
                    ),
                    "semantic_screen_candidate_count": int(
                        replica_facts.get(
                            "semantic_screen_candidate_count",
                            replica_facts.get("replica_count", 0),
                        )
                    ),
                    "shape_match_count_before_cone_signature": int(
                        replica_facts.get(
                            "shape_match_count_before_cone_signature", 0
                        )
                    ),
                    "accepted_bank_count": int(
                        bundle["recovery"]["bank_count"]
                    ),
                    "expansion_policy": (
                        "all_semantically_compatible_live_banks"
                    ),
                    "rejections": rejections,
                    "identity_features_excluded": list(
                        replica_facts.get("excluded_identity_features") or []
                    ),
                }
            if fir_data_cone:
                bundle = namespace_generating_operator_bridge_bundle(
                    bundle,
                    namespace=(
                        "fdagents_registered_fir_data_cone_"
                        + source_sha256[:12]
                    ),
                )
            if target_width is None:
                target_width = int(bundle["plans"][0]["data_width"])
                attempts[-1]["target_width"] = target_width
            bundle_path = Path(mcp.run_dir) / (
                "generating_operator_bridge_bundle.json"
                if generating_bridge else "registered_product_bundle.json"
            )
            bundle_path.write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )
            if generating_bridge:
                (Path(mcp.run_dir) / "replica_discovery.json").write_text(
                    json.dumps(
                        replica_discovery, indent=2, sort_keys=True
                    ),
                    encoding="utf-8",
                )
            authorization = build_registered_product_authorization(
                source_sha256, bundle
            )
            plan = bundle["plans"][0]
            if generating_bridge:
                term_count = len(plan["terms"])
                temporal_branch_count = len(plan["input_roles"])
                schedule_kind = "generating_operator_bridge_v2"
                pipeline_depth = 4
                schedule_cut_depth = bundle["recovery"]["schedule_cut_depth"]
                replay_vectors = bundle["recovery"]["replay_vector_count"]
            elif plan["schedule_kind"] == "registered_branch_state_dag_v1":
                term_count = sum(
                    len(state["terms"]) for state in plan["states"]
                ) + len(plan["output_state"]["terms"])
                temporal_branch_count = len(plan["states"])
                schedule_kind = plan["schedule_kind"]
                pipeline_depth = plan["pipeline_depth"]
                schedule_cut_depth = plan["recovery"]["schedule_cut_depth"]
                replay_vectors = plan["recovery"]["replay_vector_count"]
            else:
                term_count = len(plan["coefficients"])
                temporal_branch_count = len(plan["coefficients"])
                schedule_kind = plan["schedule_kind"]
                pipeline_depth = plan["pipeline_depth"]
                schedule_cut_depth = plan["recovery"]["schedule_cut_depth"]
                replay_vectors = plan["recovery"]["replay_vector_count"]
            backend_summary = {
                "generating_operator_recovery": generating_recovery,
                "schedule_kind": schedule_kind,
                "closure_node_count": sum(
                    item["node_count"] for item in accepted_closure_stats
                ),
                "closure_root_count": sum(
                    item["root_count"] for item in accepted_closure_stats
                ),
                "selected_instance_count": len(bundle["instances"]),
                "module_count": len(bundle["plans"]),
                "registered_bank_count": int(
                    bundle.get("recovery", {}).get("bank_count", 1)
                ),
                "replica_discovery": replica_discovery,
                "data_width": plan["data_width"],
                "term_count": term_count,
                "temporal_branch_count": temporal_branch_count,
                "pipeline_depth": pipeline_depth,
                "schedule_cut_depth": schedule_cut_depth,
                "composed_replay_vectors": replay_vectors,
                "semantic_operator_class": (
                    bundle.get("recovery", {}).get("operator_class")
                ),
                "external_plan_used": False,
            }
            attempts[-1].update({
                "status": (
                    "generating_operator_bundle_recovered"
                    if generating_recovery.get("status") == "recovered"
                    else "alternate_registered_product_bundle_recovered"
                ),
                "selected_instance_count": len(bundle["instances"]),
                "closure_node_count": backend_summary["closure_node_count"],
                "registered_bank_count": backend_summary[
                    "registered_bank_count"
                ],
            })
            synthesis_dir = Path(mcp.run_dir) / "registered_product_synthesis"
            synthesis = _payload(await mcp.call_vivado(
                "synthesize_registered_product_modules",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "bundle_path": str(bundle_path),
                    "work_dir": str(synthesis_dir),
                },
                timeout=900.0,
            ))
            module_dcps = synthesis.get("module_dcps")
            if not isinstance(module_dcps, dict) or not module_dcps:
                raise RuntimeError("registered product synthesis returned no modules")
            inserted_dcp = Path(mcp.run_dir) / "registered_product_inserted.dcp"
            attempts[-1]["mutation_attempted"] = True
            insertion = _payload(await mcp.call_rw(
                "insert_registered_product_modules",
                {
                    "bundle_path": str(bundle_path),
                    "module_dcps": module_dcps,
                    "output_dcp": str(inserted_dcp),
                },
                timeout=900.0,
            ))
            if insertion.get("source_dcp_sha256") != source_sha256:
                raise RuntimeError("registered product insertion source identity changed")
            route_facts = _payload(await mcp.call_vivado(
                "route_registered_product_bundle",
                {
                    "inserted_dcp": str(inserted_dcp),
                    "source_dcp": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "bundle_path": str(bundle_path),
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                    "physical_profile": "compact_timing",
                },
                timeout=route_timeout + 1200.0,
            ))
            if not output.is_file():
                raise RuntimeError("registered product output checkpoint is missing")
            candidate_sha256 = sha256_file(output)
            if route_facts.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError("registered product route candidate identity changed")
            attestation = build_registered_product_emitted_attestation(
                authorization,
                candidate_sha256,
                route_facts,
                template=target,
            )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            evidence = {
                "version": 1,
                "action_type": "registered_product_closure_rewrite",
                "family_ref": family_ref,
                "target_width": target_width,
                "backend_summary": backend_summary,
                "source_contract_authority": "measurement_probe_only",
                "complete_operator_rewrite_eligible": False,
                "formal_source_equivalence": False,
                "acceptance_authority": "central_measurement_only",
                "final_equivalence_required": True,
            }
            attempts[-1]["status"] = "measurement_candidate"
            recovery_note = (
                "generating-operator"
                if generating_recovery.get("status") == "recovered"
                else "alternate-fixed-point"
            )
            skill_output = SkillOutput(
                output,
                (
                    f"operator_rewrite {recovery_note} "
                    f"instances={len(bundle['instances'])} "
                    f"terms={term_count} depth={pipeline_depth}"
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
                summary="operator_rewrite registered product candidate=1 proof=measurement-probe",
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "registered_product_backend_summary": backend_summary,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                    "action_type": "registered_product_closure_rewrite",
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
                    "registered_product_backend_summary": backend_summary,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "target_width": target_width,
                        "failure_scope": "selected_family_only",
                        "remaining_family_exploration_allowed": True,
                        "generating_operator_recovery_status": (
                            generating_recovery.get("status")
                        ),
                        "generating_operator_rejection_reason": (
                            generating_recovery.get("reason")
                        ),
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_constructive_accumulator(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        """Recover, prove and route one live constructive accumulator family."""
        attempts: list[dict] = []
        family_ref = str(params.get("family_ref") or "")
        output = Path(mcp.run_dir) / "operator_rewrite_constructive_accumulator.dcp"
        accepted: list[dict] = []
        target_width = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            handoff = resolve_sequential_family_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            route_timeout = _framework_owned_route_timeout(params)
            deadline = _framework_owned_execution_deadline(params)
            reserve = _framework_owned_commit_reserve(params)

            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            contract = _payload(await mcp.call_rw(
                "recover_sequential_operator_contract",
                {
                    "critical_paths_data": handoff["critical_paths_data"],
                    "candidate_id": handoff["candidate_id"],
                },
                timeout=600.0,
            ))
            target_stem, target_width = _compressor_target_stem(
                contract, source_sha256, handoff["candidate_id"]
            )

            async def recover_one(
                stem: str,
                *,
                width: int,
                ordinal: int,
                role: str,
            ) -> dict:
                cone_dir = Path(mcp.run_dir) / (
                    f"constructive_accumulator_cone_{ordinal:02d}"
                )
                extraction_path = cone_dir / "constructive_proof.json"
                certificate_path = cone_dir / "lowering_certificate.json"
                cache_reused = False
                proof_wrapper = None
                if extraction_path.is_file() and certificate_path.is_file():
                    try:
                        proof_wrapper = json.loads(
                            extraction_path.read_text(encoding="utf-8")
                        )
                        certificate = json.loads(
                            certificate_path.read_text(encoding="utf-8")
                        )
                        if (
                            proof_wrapper.get("mutation_eligible") is not True
                            or certificate.get("mutation_ready") is not True
                            or certificate.get("source_identity", {}).get(
                                "dcp_sha256"
                            ) != source_sha256
                        ):
                            raise ValueError("stale constructive cache")
                        cache_reused = True
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                        proof_wrapper = None
                if proof_wrapper is None:
                    if cone_dir.exists():
                        raise RuntimeError(
                            "constructive cone cache is incomplete for this run"
                        )
                    extraction = _payload(await mcp.call_vivado(
                        "extract_register_cone_facts",
                        {
                            "dcp_path": str(input_dcp),
                            "source_dcp_sha256": source_sha256,
                            "target_stem": stem,
                            "output_dir": str(cone_dir),
                        },
                        timeout=900.0,
                    ))
                    if (
                        extraction.get("read_only_after_restore") is not True
                        or extraction.get("target_width") != width
                    ):
                        raise RuntimeError(
                            "constructive cone extraction did not restore the exact seed"
                        )
                    proof = discover_constructive_accumulator(
                        cone_dir, max_bdd_nodes=10_000_000
                    )
                    proof_wrapper = {
                        "schema_version": proof["schema_version"],
                        "status": "proved",
                        "sibling_count": 1,
                        "sibling_expression_agreement": True,
                        "recovered_expression": {
                            key: proof["derived_expression"][key]
                            for key in (
                                "coefficient", "operand_order",
                                "slice_lsb", "slice_width",
                            )
                        },
                        "siblings": [proof],
                        "mutation_eligible": True,
                    }
                    extraction_path.write_text(
                        json.dumps(proof_wrapper, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    certificate = build_constructive_lowering(
                        cone_dir, extraction_path
                    )
                    certificate_path.write_text(
                        json.dumps(certificate, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                proof = proof_wrapper["siblings"][0]
                row = {
                    "ordinal": ordinal,
                    "role": role,
                    "cone_dir": cone_dir,
                    "certificate_path": certificate_path,
                    "certificate": certificate,
                    "expression_sha256": proof["canonical_comparison"][
                        "actual_expression_sha256"
                    ],
                    "expression": dict(proof["derived_expression"]),
                    "cache_reused": cache_reused,
                }
                attempts.append({
                    "ordinal": ordinal,
                    "role": role,
                    "status": "constructive_contract_proved",
                    "cache_reused": cache_reused,
                    "coefficient": row["expression"]["coefficient"],
                    "operand_order": row["expression"]["operand_order"],
                    "slice_lsb": row["expression"]["slice_lsb"],
                    "slice_width": row["expression"]["slice_width"],
                    "mutation_attempted": False,
                })
                return row

            selected = await recover_one(
                target_stem,
                width=target_width,
                ordinal=0,
                role="selected_live_family",
            )
            accepted.append(selected)
            replica_facts = _payload(await mcp.call_rw(
                "recover_sequential_operator_replicas",
                {
                    "critical_paths_data": handoff["critical_paths_data"],
                    "candidate_id": handoff["candidate_id"],
                    "max_replicas": 64,
                },
                timeout=900.0,
            ))
            replicas = replica_facts.get("replicas")
            if (
                replica_facts.get("kind")
                != "sequential_operator_replica_family_facts"
                or replica_facts.get("read_only") is not True
                or replica_facts.get("fresh_rediscovery") is not True
                or not isinstance(replicas, list)
            ):
                raise RuntimeError(
                    "constructive accumulator replica discovery is incomplete"
                )
            seen_stems = {target_stem}
            for replica_ordinal, replica in enumerate(replicas, start=1):
                if deadline is not None and float(deadline) - time.monotonic() <= reserve:
                    attempts.append({
                        "role": "replica_screen",
                        "status": "stopped_at_framework_commit_reserve",
                    })
                    break
                try:
                    peer_stem, peer_width, _peer_candidate_id = (
                        _replica_target_stem(replica)
                    )
                    if peer_stem in seen_stems:
                        continue
                    seen_stems.add(peer_stem)
                    peer = await recover_one(
                        peer_stem,
                        width=peer_width,
                        ordinal=replica_ordinal,
                        role="live_replica_candidate",
                    )
                    if (
                        peer["expression_sha256"] != selected["expression_sha256"]
                        or peer["certificate"]["weighted_sum"]
                        != selected["certificate"]["weighted_sum"]
                        or peer["certificate"]["compressor_spec"]
                        != selected["certificate"]["compressor_spec"]
                    ):
                        attempts[-1]["status"] = "constructive_replica_rejected"
                        attempts[-1]["reason"] = "expression_or_lowering_disagrees"
                        continue
                    attempts[-1]["status"] = "constructive_replica_accepted"
                    accepted.append(peer)
                except (RuntimeError, ValueError) as exc:
                    attempts.append({
                        "ordinal": replica_ordinal,
                        "role": "live_replica_candidate",
                        "status": "constructive_replica_rejected",
                        "reason": str(exc)[:500],
                        "mutation_attempted": False,
                    })

            certificates = [item["certificate"] for item in accepted]
            authorization = build_constructive_accumulator_authorization(
                source_dcp_sha256=source_sha256,
                expression_sha256=selected["expression_sha256"],
                certificates=certificates,
            )
            authorization_path = Path(mcp.run_dir) / (
                "constructive_accumulator_authorization.json"
            )
            authorization_path.write_text(
                json.dumps(authorization, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            attempts.append({
                "status": "mutation_started",
                "accepted_family_size": len(accepted),
                "mutation_attempted": True,
            })
            attestation = _payload(await mcp.call_vivado(
                "apply_constructive_accumulator_rebuild",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "cone_dirs": [str(item["cone_dir"]) for item in accepted],
                    "certificate_paths": [
                        str(item["certificate_path"]) for item in accepted
                    ],
                    "authorization_path": str(authorization_path),
                    "authorization_id": authorization["certificate_id"],
                    "output_dcp": str(output),
                    "route_timeout_s": route_timeout,
                },
                timeout=route_timeout + 1200.0,
            ))
            qor_observation = attestation.pop("qor_observation", None)
            if not output.is_file():
                raise RuntimeError(
                    "constructive accumulator routed checkpoint is missing"
                )
            candidate_sha256 = sha256_file(output)
            if attestation.get("candidate") != {"dcp_sha256": candidate_sha256}:
                raise RuntimeError(
                    "constructive accumulator candidate identity changed"
                )
            proof = build_operator_candidate_proof(
                target,
                authorization,
                candidate_sha256,
                emitted_topology_attestation=attestation,
            )
            attempts[-1]["status"] = "routed_measurement_candidate"
            expression = selected["expression"]
            evidence = {
                "version": 1,
                "action_type": "constructive_accumulator_rebuild",
                "family_ref": family_ref,
                "accepted_family_size": len(accepted),
                "coefficient": expression["coefficient"],
                "operand_order": expression["operand_order"],
                "slice_lsb": expression["slice_lsb"],
                "slice_width": expression["slice_width"],
                "compressor_count_per_family": selected["certificate"][
                    "compressor_spec"
                ]["compressor_count"],
                "qor_observation": qor_observation,
                "acceptance_authority": "central_measurement_only",
                "final_equivalence_required": True,
            }
            skill_output = SkillOutput(
                output,
                (
                    "operator_rewrite constructive accumulator "
                    f"families={len(accepted)} width={expression['slice_width']}"
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
                summary=(
                    "operator_rewrite constructive accumulator "
                    f"candidate=1 family={len(accepted)}"
                ),
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
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "target_width": target_width,
                        "failure_scope": "selected_family_and_proved_live_replicas",
                        "remaining_family_exploration_allowed": True,
                        "accepted_family_size": len(accepted),
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts if isinstance(item, dict)
                        ),
                    },
                    "runtime_s": round(time.monotonic() - started, 6),
                },
            )

    async def _execute_arithmetic_compressor(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        started: float,
        failed_dcp: Path,
    ) -> SkillResult:
        report_path = Path(mcp.run_dir) / "operator_rewrite_validation.json"
        attempts = []
        outputs = []
        certificate_summary = None
        family_ref = ""
        target_width = None
        proof_budget_ms = None
        try:
            input_dcp = Path(str(params.get("input_dcp") or "")).resolve()
            if input_dcp.is_symlink() or not input_dcp.is_file():
                raise ValueError("operator_rewrite exact input DCP is unavailable")
            source_sha256 = sha256_file(input_dcp)
            family_ref = str(params.get("family_ref") or "")
            handoff = resolve_sequential_family_handoff(
                mcp, artifact_sha256=source_sha256, family_ref=family_ref
            )
            route_timeout = _framework_owned_route_timeout(params)
            deadline = _framework_owned_execution_deadline(params)
            reserve = _framework_owned_commit_reserve(params)

            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
            contract = _payload(await mcp.call_rw(
                "recover_sequential_operator_contract",
                {
                    "critical_paths_data": handoff["critical_paths_data"],
                    "candidate_id": handoff["candidate_id"],
                },
                timeout=600.0,
            ))
            target_stem, target_width = _compressor_target_stem(
                contract, source_sha256, handoff["candidate_id"]
            )
            attempts.append({
                "family_ref": family_ref,
                "target_width": target_width,
                "status": "fresh_contract_recovered",
            })

            cone_dir = Path(mcp.run_dir) / "operator_compressor_cone"
            if cone_dir.exists():
                raise RuntimeError("operator compressor cone directory already exists")
            extraction = _payload(await mcp.call_vivado(
                "extract_register_cone_facts",
                {
                    "dcp_path": str(input_dcp),
                    "source_dcp_sha256": source_sha256,
                    "target_stem": target_stem,
                    "output_dir": str(cone_dir),
                },
                timeout=900.0,
            ))
            if (
                extraction.get("read_only_after_restore") is not True
                or extraction.get("target_width") != target_width
            ):
                raise RuntimeError("register-cone extraction did not restore the exact seed")

            proof_budget_ms = 3_600_000
            if deadline is not None:
                proof_budget_ms = max(
                    1,
                    int(
                        (float(deadline) - time.monotonic() - reserve)
                        * 1000
                    ),
                )
            attempts[-1]["proof_total_budget_ms"] = proof_budget_ms
            try:
                certificate = build_compressor_certificate(
                    cone_dir,
                    solver_timeout_ms=proof_budget_ms,
                )
            except CompressorProofTimeout as exc:
                attempts[-1].update({
                    "status": "proof_total_deadline_exceeded",
                    "proof_stage": exc.stage,
                    "proof_elapsed_s": round(exc.elapsed_s, 6),
                    "mutation_attempted": False,
                })
                raise
            certificate_summary = compressor_certificate_summary(certificate)
            certificate_path = Path(mcp.run_dir) / "compressor_certificate.json"
            certificate_path.write_text(
                json.dumps(certificate, indent=2, sort_keys=True), encoding="utf-8"
            )
            attempts[-1]["certificate_status"] = certificate_summary["status"]
            attempts[-1]["blocking_rejection_codes"] = certificate_summary[
                "blocking_rejection_codes"
            ]
            attempts[-1]["advisory_warning_codes"] = certificate_summary[
                "advisory_warning_codes"
            ]
            attempts[-1]["mutation_attempted"] = False
            if certificate.get("status") != "proved" or not certificate.get(
                "mutation_ready"
            ):
                codes = certificate_summary["blocking_rejection_codes"]
                if not codes:
                    codes = ["UNKNOWN_CERTIFICATE_READINESS_FAILURE"]
                raise RuntimeError(
                    "arithmetic cone is not mutation-ready: " + ",".join(codes)
                )
            authorization = build_arithmetic_compressor_authorization(certificate)
            profiles = list(params["qor_profiles"])
            for ordinal, profile in enumerate(profiles, start=1):
                attempt = {
                    "family_ref": family_ref,
                    "target_width": target_width,
                    "qor_profile": profile,
                    "candidate_ordinal": ordinal,
                    "status": "started",
                    "mutation_attempted": False,
                }
                attempts.append(attempt)
                output = Path(mcp.run_dir) / (
                    f"operator_rewrite_compressor_{ordinal:02d}_{profile}.dcp"
                )
                try:
                    if (
                        deadline is not None
                        and float(deadline) - time.monotonic() <= reserve
                    ):
                        attempt["status"] = "not_started_budget_reserve"
                        break
                    attempt["mutation_attempted"] = True
                    attestation = _payload(await mcp.call_vivado(
                        "apply_proved_arithmetic_compressor_eco",
                        {
                            "dcp_path": str(input_dcp),
                            "source_dcp_sha256": source_sha256,
                            "cone_dir": str(cone_dir),
                            "certificate_path": str(certificate_path),
                            "certificate_id": certificate["certificate_id"],
                            "authorization_id": authorization["certificate_id"],
                            "output_dcp": str(output),
                            "route_timeout_s": route_timeout,
                            "physical_profile": profile,
                        },
                        timeout=route_timeout + 1200.0,
                    ))
                    qor_observation = attestation.pop("qor_observation", None)
                    if not output.is_file():
                        raise RuntimeError(
                            "arithmetic compressor output checkpoint is missing"
                        )
                    candidate_sha256 = sha256_file(output)
                    if attestation.get("candidate") != {
                        "dcp_sha256": candidate_sha256
                    }:
                        raise RuntimeError(
                            "arithmetic compressor attestation candidate mismatch"
                        )

                    proof = build_operator_candidate_proof(
                        target,
                        authorization,
                        candidate_sha256,
                        emitted_topology_attestation=attestation,
                    )
                    evidence = {
                        "version": 2,
                        "family_ref": family_ref,
                        "target_width": target_width,
                        "qor_profile": profile,
                        "qor_observation": qor_observation,
                        "qor_advisory": certificate_summary.get("qor_advisory"),
                        "compressor_count": certificate["compressor_spec"][
                            "compressor_count"
                        ],
                        "polynomial_degree": certificate["weighted_sum"][
                            "polynomial_degree"
                        ],
                        "emitted_topology": certificate["proof"]["emitted_topology"],
                        "acceptance_authority": "central_measurement_only",
                        "final_equivalence_required": True,
                    }
                    label = (
                        "operator_rewrite arithmetic compressor "
                        f"profile={profile} width={target_width} "
                        f"degree={evidence['polynomial_degree']}"
                    )[:120]
                    outputs.append(SkillOutput(output, label, evidence, proof))
                    attempt.update({
                        "status": "measurement_candidate",
                        "qor_observation": qor_observation,
                    })
                except Exception as exc:  # noqa: BLE001
                    output.unlink(missing_ok=True)
                    attempt.update({
                        "status": "rejected",
                        "reason": str(exc)[:500],
                    })
                    continue

            if not outputs:
                reasons = [
                    str(item.get("reason"))
                    for item in attempts
                    if item.get("reason")
                ]
                raise RuntimeError(
                    "no proof-gated arithmetic-compressor sibling was produced"
                    + (": " + reasons[-1] if reasons else "")
                )
            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=before_wns,
                delta_wns=0.0,
                is_legal=True,
                output_dcp=outputs[0].output_dcp,
                summary=(
                    "operator_rewrite arithmetic compressor "
                    f"candidates={len(outputs)} proof=v4"
                ),
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "compressor_certificate_summary": certificate_summary,
                    "runtime_s": round(time.monotonic() - started, 6),
                    "final_submission_authority": False,
                },
                candidates=tuple(outputs),
            )
        except Exception as exc:  # noqa: BLE001
            validator_diagnostic = _validator_diagnostic(report_path)
            for output in outputs:
                output.output_dcp.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "compressor_certificate_summary": certificate_summary,
                    "operator_rewrite_diagnostic": {
                        "version": 1,
                        "target": target,
                        "family_ref": family_ref,
                        "target_width": target_width,
                        "failure_scope": "selected_family_only",
                        "remaining_family_exploration_allowed": True,
                        "proof_total_budget_ms": proof_budget_ms,
                        "certificate_status": (
                            certificate_summary.get("status")
                            if isinstance(certificate_summary, dict) else None
                        ),
                        "blocking_rejection_codes": (
                            list(certificate_summary.get("blocking_rejection_codes") or [])
                            if isinstance(certificate_summary, dict) else []
                        ),
                        "mutation_attempted": any(
                            bool(item.get("mutation_attempted"))
                            for item in attempts
                            if isinstance(item, dict)
                        ),
                        "attempts": [
                            {
                                key: item.get(key)
                                for key in (
                                    "status",
                                    "family_ref",
                                    "target_width",
                                    "proof_total_budget_ms",
                                    "proof_stage",
                                    "proof_elapsed_s",
                                    "mutation_attempted",
                                )
                                if key in item
                            }
                            for item in attempts[:4]
                            if isinstance(item, dict)
                        ],
                    },
                    "strict_validator_diagnostic": validator_diagnostic,
                    "runtime_s": round(time.monotonic() - started, 6),
                },
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
            route_timeout = _framework_owned_route_timeout(params)
            await mcp.call_rw(
                "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
            )
            await mcp.call_rw(
                "read_checkpoint", {"dcp_path": str(input_dcp)}, timeout=600.0
            )
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
            # Minimize SRL layers first so scarce measured clock-leaf capacity
            # remains available for physically compact family placement.
            action_candidate = plans[0]
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
                        "addresses": sorted({
                            int(depth) - 1 for depth in plan.srl_segment_depths
                        }),
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
            placement = allocate_recurrence_family_srl_placements(
                facts,
                action_candidate,
                oracle_slots=oracle_slots,
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
            proof = build_operator_candidate_proof(
                target,
                action_candidate["authorization"],
                candidate_sha256,
                emitted_topology_attestation=attestation,
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
                "acceptance_authority": "central_measurement_only",
                "final_equivalence_required": True,
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
            validator_diagnostic = _validator_diagnostic(report_path)
            output.unlink(missing_ok=True)
            return SkillResult.failure(
                before_wns,
                str(exc),
                failed_dcp,
                details={
                    "proof_version": PROOF_VERSION,
                    "template": target,
                    "attempts": attempts,
                    "strict_validator_diagnostic": validator_diagnostic,
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


def _registered_closure_partial_is_resumable(output_dir: Path) -> bool:
    """Return whether a timed-out Tcl extraction left useful exact-source state."""
    directory = Path(output_dir)
    closure = directory / "closure.tsv"
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or closure.is_symlink()
        or not closure.is_file()
        or closure.stat().st_size <= 0
    ):
        return False
    return any(
        child.is_dir()
        and not child.is_symlink()
        and (child / "manifest.tsv").is_file()
        for child in directory.glob("node_*")
    )


_REGISTERED_CLOSURE_REJECTION_NAME = "registered_closure_rejection.json"


def _registered_closure_rejection(output_dir: Path, request: dict) -> str | None:
    """Return a matching scoped extraction rejection, if one was cached.

    A structurally incomplete live family is not a global operator negative,
    but retrying the same Tcl walk on every Luna round wastes the expensive
    Vivado call.  The marker is deliberately bound to the exact source and
    extraction arguments so a new seed or a different family can never inherit
    the rejection.
    """
    marker = Path(output_dir) / _REGISTERED_CLOSURE_REJECTION_NAME
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expected = {
        "source_dcp_sha256": str(request.get("source_dcp_sha256") or ""),
        "target_stem": str(request.get("target_stem") or ""),
        "max_stage_depth": request.get("max_stage_depth"),
        "word_width": request.get("word_width"),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return None
    reason = str(payload.get("reason") or "scoped registered closure rejection")
    return reason[:1000]


def _cache_registered_closure_rejection(
    output_dir: Path, request: dict, reason: str
) -> None:
    """Persist only deterministic structural rejection diagnostics."""
    lowered = str(reason or "").lower()
    if not any(
        marker in lowered
        for marker in (
            "closure target family is empty",
            "selected registered family is incomplete",
            "observable root is not a whole number of words",
        )
    ):
        return
    directory = Path(output_dir)
    if directory.is_symlink() or not directory.is_dir():
        return
    payload = {
        "version": 1,
        "source_dcp_sha256": str(request.get("source_dcp_sha256") or ""),
        "target_stem": str(request.get("target_stem") or ""),
        "max_stage_depth": request.get("max_stage_depth"),
        "word_width": request.get("word_width"),
        "reason": str(reason)[:1000],
    }
    marker = directory / _REGISTERED_CLOSURE_REJECTION_NAME
    temporary = directory / ("." + marker.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, marker)
    except OSError:
        temporary.unlink(missing_ok=True)


async def _extract_registered_closure_with_resume(
    mcp, request: dict, *, timeout: float = 1300.0
) -> dict:
    """Continue once from exact-source node checkpoints after a timeout."""
    output_dir = Path(str(request["output_dir"]))
    cached_rejection = _registered_closure_rejection(output_dir, request)
    if cached_rejection is not None:
        raise RuntimeError(
            "cached scoped registered closure rejection: " + cached_rejection
        )
    if not output_dir.exists():
        _seed_registered_closure_from_shallower_checkpoint(
            Path(str(request["dcp_path"])),
            source_sha256=str(request["source_dcp_sha256"]),
            target_stem=str(request["target_stem"]),
            destination=output_dir,
        )
    for attempt in range(2):
        try:
            response = _payload(await mcp.call_vivado(
                "extract_registered_closure_facts", request, timeout=timeout
            ))
            if response.get("status") == "rejected":
                reason = str(response.get("message") or "")
                _cache_registered_closure_rejection(output_dir, request, reason)
                raise RuntimeError(
                    "scoped registered closure rejection: " + reason
                )
            return response
        except Exception as exc:  # noqa: BLE001
            _cache_registered_closure_rejection(output_dir, request, str(exc))
            if (
                attempt == 0
                and "Timeout exceeded" in str(exc)
                and _registered_closure_partial_is_resumable(output_dir)
            ):
                continue
            raise
    raise AssertionError("registered closure resume loop exhausted")


def _validator_diagnostic(report_path: Path) -> dict:
    """Retain bounded validation failure facts after action scratch is GC'd."""
    path = Path(report_path)
    if path.is_symlink() or not path.is_file():
        return {"report_available": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "report_available": True,
            "report_read_error": f"{type(exc).__name__}: {exc}"[:500],
        }
    if not isinstance(payload, dict):
        return {"report_available": True, "report_is_object": False}
    keys = (
        "schema_version", "strict_mode", "vectors", "precheck_vectors",
        "phase1_passed", "phase2_passed", "phase2_skipped",
        "infrastructure_failure", "process_exit_code", "error",
    )
    result = {"report_available": True, "report_is_object": True}
    result.update({key: payload.get(key) for key in keys if key in payload})
    return result


def _compressor_target_stem(
    contract: dict, source_sha256: str, candidate_id: str
) -> tuple[str, int]:
    """Derive a complete indexed destination bus from fresh trusted facts."""
    if (
        contract.get("status") != "success"
        or contract.get("kind") != "sequential_operator_contract_facts"
        or contract.get("fresh_rediscovery") is not True
        or contract.get("read_only") is not True
        or contract.get("design_sha256") != source_sha256
        or contract.get("candidate_id") != candidate_id
        or contract.get("structural_boundary_recovered") is not True
        or contract.get("proof_input_eligible") is not True
        or contract.get("mutation_eligible") is not False
    ):
        raise RuntimeError("fresh sequential contract is incomplete")
    mapping = contract.get("bit_mapping") or {}
    members = contract.get("member_facts")
    if (
        mapping.get("status") != "explicit_unique_contiguous"
        or mapping.get("min_index") != 0
        or not isinstance(members, list)
        or len(members) < 2
        or mapping.get("member_count") != len(members)
    ):
        raise RuntimeError("compressor destination bus is not contiguous from bit zero")
    stems = set()
    indices = []
    for item in members:
        name = item.get("cell") if isinstance(item, dict) else None
        match = re.fullmatch(r"(.+)\[(\d+)\]", str(name or ""))
        if match is None:
            raise RuntimeError("compressor destination member has no indexed cell form")
        stems.add(match.group(1))
        indices.append(int(match.group(2)))
    if len(stems) != 1 or sorted(indices) != list(range(len(members))):
        raise RuntimeError("compressor destination members do not form one complete word")
    return next(iter(stems)), len(members)


def _replica_target_stem(replica: dict) -> tuple[str, int, str]:
    """Derive one exact indexed word from trusted anonymous replica facts."""
    if not isinstance(replica, dict):
        raise RuntimeError("registered operator replica is not an object")
    family = replica.get("family") or {}
    mapping = family.get("bit_mapping") or {}
    members = replica.get("member_facts")
    candidate_id = str(replica.get("candidate_id") or "")
    if (
        not candidate_id.startswith("sequential:")
        or mapping.get("status") != "explicit_unique_contiguous"
        or mapping.get("min_index") != 0
        or not isinstance(members, list)
        or len(members) < 2
        or mapping.get("member_count") != len(members)
        or family.get("member_count") != len(members)
    ):
        raise RuntimeError("registered operator replica word is incomplete")
    stems = set()
    indices = []
    for item in members:
        name = item.get("cell") if isinstance(item, dict) else None
        match = re.fullmatch(r"(.+)\[(\d+)\]", str(name or ""))
        if match is None:
            raise RuntimeError("registered operator replica has no indexed cell form")
        stems.add(match.group(1))
        indices.append(int(match.group(2)))
    if len(stems) != 1 or sorted(indices) != list(range(len(members))):
        raise RuntimeError("registered operator replica is not one complete word")
    return next(iter(stems)), len(members), candidate_id


def _replica_owner_key(target_stem: str) -> str:
    """Return the anonymous structural owner used only to schedule screens.

    Replica equivalence still comes exclusively from the recovered topology and
    replay proof.  The hierarchy owner is not an acceptance feature; it merely
    prevents an expensive closure walk from screening every word in one owner
    before trying the first word in another owner.
    """
    owner, separator, _ = target_stem.rpartition("/")
    return owner if separator else "."


def _order_replica_screen_candidates(
    replicas: list,
    *,
    selected_target_stem: str,
    cached_candidate_ids: set[str] | None = None,
) -> list[tuple[int, object]]:
    """Try one candidate per anonymous owner before revisiting an owner.

    This is a generic scheduling policy for replicated banks.  It does not
    inspect benchmark names, owner labels, placement coordinates, or expected
    resource counts, and it never bypasses the normal closure/replay proof.
    Invalid facts remain in the tail so the existing diagnostic path records
    their concrete rejection.
    """
    selected_owner = _replica_owner_key(selected_target_stem)
    cached_candidate_ids = cached_candidate_ids or set()
    selected_replica = next((
        item for item in replicas
        if isinstance(item, dict) and item.get("is_selected") is True
    ), None)
    selected_trace = (
        selected_replica.get("data_cone_traceback")
        if isinstance(selected_replica, dict) else {}
    ) or {}

    def trace_distance(replica: object) -> tuple[int, int, int, int, int, int]:
        """Soft anonymous distance used only to schedule expensive proofs."""
        if not isinstance(replica, dict):
            return (0, 0, 1, 10**9, 10**9, 10**9)
        trace = replica.get("data_cone_traceback") or {}
        primitive_keys = set(selected_trace.get("primitive_type_counts") or {})
        primitive_keys.update(trace.get("primitive_type_counts") or {})
        primitive_delta = sum(abs(
            int((selected_trace.get("primitive_type_counts") or {}).get(key, 0))
            - int((trace.get("primitive_type_counts") or {}).get(key, 0))
        ) for key in primitive_keys)
        boundary_keys = set(selected_trace.get("boundary_type_counts") or {})
        boundary_keys.update(trace.get("boundary_type_counts") or {})
        boundary_delta = sum(abs(
            int((selected_trace.get("boundary_type_counts") or {}).get(key, 0))
            - int((trace.get("boundary_type_counts") or {}).get(key, 0))
        ) for key in boundary_keys)
        node_delta = abs(
            int(selected_trace.get("total_node_count") or 0)
            - int(trace.get("total_node_count") or 0)
        )
        return (
            -int(replica.get("critical_path_coverage_count") or 0),
            -int(replica.get("critical_path_member_count") or 0),
            0 if bool(replica.get("cone_signature_match")) else 1,
            primitive_delta,
            boundary_delta,
            node_delta,
        )
    buckets: dict[str, list[tuple[int, object]]] = {}
    invalid: list[tuple[int, object]] = []
    for ordinal, replica in enumerate(replicas):
        try:
            peer_stem, _, _ = _replica_target_stem(replica)
        except RuntimeError:
            invalid.append((ordinal, replica))
            continue
        buckets.setdefault(_replica_owner_key(peer_stem), []).append(
            (ordinal, replica)
        )

    # A saved closure is cheaper to screen, but cache preference must remain
    # local to each anonymous owner.  Globally moving all cached candidates to
    # the front makes one already explored bank monopolize the walk and delays
    # semantically equivalent banks with a different LUT decomposition.
    for candidates in buckets.values():
        candidates.sort(key=lambda item: (
            0
            if _replica_target_stem(item[1])[2] in cached_candidate_ids
            else 1,
            *trace_distance(item[1]),
            item[0],
        ))

    owner_order = sorted(
        (owner for owner in buckets if owner != selected_owner),
        key=lambda owner: min(
            trace_distance(item[1]) for item in buckets[owner]
        ),
    )
    if selected_owner in buckets:
        owner_order.append(selected_owner)
    ordered: list[tuple[int, object]] = []
    depth = 0
    while True:
        emitted = False
        for owner in owner_order:
            candidates = buckets[owner]
            if depth < len(candidates):
                ordered.append(candidates[depth])
                emitted = True
        if not emitted:
            break
        depth += 1
    return [*ordered, *invalid]


def _write_registered_screen_cache(
    closure_dir: Path,
    *,
    source_sha256: str,
    family_ref: str,
    candidate_id: str,
    target_stem: str,
    closure: RegisteredClosure,
    replica_of_family_ref: str | None = None,
) -> None:
    """Mark a fully recovered read-only screen for the matching live action."""
    payload = {
        "version": _REGISTERED_SCREEN_CACHE_VERSION,
        "source_dcp_sha256": source_sha256,
        "family_ref": family_ref,
        "candidate_id": candidate_id,
        "target_stem": target_stem,
        "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
        "node_count": len(closure.records),
        "root_count": len(closure.roots()),
    }
    if replica_of_family_ref:
        payload["replica_of_family_ref"] = replica_of_family_ref
    (closure_dir / "registered_screen_cache.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _registered_fir_bundle_digest(bundle: dict) -> str:
    payload = json.dumps(
        bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _write_registered_fir_bundle_cache(
    checkpoint_dir: Path,
    *,
    source_sha256: str,
    closure: RegisteredClosure,
    bundle: dict,
) -> None:
    """Persist a private, exact-source FIR proof result across Agent runs."""
    _require_registered_fir_data_cone_bundle(bundle)
    root_ids = {record.node_id for record in closure.roots()}
    replaced_ids = {
        str(instance.get("replaced_target_node_id") or "")
        for instance in bundle.get("instances", [])
        if isinstance(instance, dict)
    }
    if not root_ids or replaced_ids != root_ids:
        raise ValueError("registered FIR bundle does not cover the cached roots")
    if checkpoint_dir.is_symlink() or not checkpoint_dir.is_dir():
        raise ValueError("registered FIR checkpoint directory is unavailable")
    bundle_path = checkpoint_dir / "registered_fir_bundle.private.json"
    metadata_path = checkpoint_dir / "registered_fir_bundle_cache.json"
    bundle_path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
    )
    metadata = {
        "version": _REGISTERED_FIR_BUNDLE_CACHE_VERSION,
        "source_dcp_sha256": source_sha256,
        "screen_cache_version": _REGISTERED_SCREEN_CACHE_VERSION,
        "node_count": len(closure.records),
        "root_count": len(closure.roots()),
        "root_node_ids": sorted(root_ids),
        "bundle_sha256": _registered_fir_bundle_digest(bundle),
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )


def _load_registered_fir_bundle_cache(
    checkpoint_dir: Path,
    *,
    source_sha256: str,
    closure: RegisteredClosure,
) -> dict | None:
    """Load only a source-bound private FIR bundle matching this closure."""
    bundle_path = checkpoint_dir / "registered_fir_bundle.private.json"
    metadata_path = checkpoint_dir / "registered_fir_bundle_cache.json"
    if not bundle_path.is_file() or not metadata_path.is_file():
        return None
    if checkpoint_dir.is_symlink() or bundle_path.is_symlink() or metadata_path.is_symlink():
        raise ValueError("registered FIR bundle cache path is not immutable")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    root_ids = {record.node_id for record in closure.roots()}
    expected = {
        "version": _REGISTERED_FIR_BUNDLE_CACHE_VERSION,
        "source_dcp_sha256": source_sha256,
        "screen_cache_version": _REGISTERED_SCREEN_CACHE_VERSION,
        "node_count": len(closure.records),
        "root_count": len(closure.roots()),
        "root_node_ids": sorted(root_ids),
    }
    if not isinstance(metadata, dict) or any(
        metadata.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("registered FIR bundle cache provenance changed")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if metadata.get("bundle_sha256") != _registered_fir_bundle_digest(bundle):
        raise ValueError("registered FIR bundle cache payload changed")
    _require_registered_fir_data_cone_bundle(bundle)
    replaced_ids = {
        str(instance.get("replaced_target_node_id") or "")
        for instance in bundle.get("instances", [])
        if isinstance(instance, dict)
    }
    if replaced_ids != root_ids:
        raise ValueError("registered FIR bundle cache root coverage changed")
    return bundle


def _registered_checkpoint_source_dir(
    input_dcp: Path, *, source_sha256: str
) -> Path:
    """Return the exact-source namespace for persistent registered facts."""
    configured = os.environ.get("FPL26_CHECKPOINT_CACHE_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        resolved = input_dcp.resolve()
        # A promoted candidate is stored below ``run/candidates``.  Its source
        # DCP is the same artifact as the screen seed, but looking only for a
        # repository ``FDAgents`` directory walks past the run-level checkpoint
        # the preceding screen published, so the same closure gets proved
        # again.  Prefer the nearest namespace that already holds checkpoints,
        # and fall back to the deployed repository layout for direct seed
        # paths.  Identity stays source-bound either way -- the leaf is still
        # source_sha256[:16] -- so a wrong root can only miss, never mismatch.
        # From C-YuLong on fplcontest-luna (891fdda), ported to this file name.
        checkpoint_root = next((
            parent for parent in (resolved.parent, *resolved.parents)
            if (parent / ".fdagents_checkpoints").is_dir()
        ), None)
        if checkpoint_root is not None:
            root = checkpoint_root / ".fdagents_checkpoints" / "registered_closure"
        else:
            repository_root = next((
                parent for parent in (resolved.parent, *resolved.parents)
                if (parent / "FDAgents").is_dir()
            ), resolved.parent)
            root = repository_root / ".fdagents_checkpoints" / "registered_closure"
    return root / source_sha256[:16]


def _seed_registered_closure_from_shallower_checkpoint(
    input_dcp: Path,
    *,
    source_sha256: str,
    target_stem: str,
    destination: Path,
) -> int | None:
    """Copy the deepest compatible partial closure before extending it.

    Increasing the semantic traceback depth must not discard hours of exact
    current-DCP extraction.  The Tcl extractor validates each copied node and
    reuses it only when source hash, target stem and width still match; it then
    appends the newly reachable stages in ``destination``.  The old immutable
    checkpoint remains untouched.
    """
    if destination.exists():
        return None
    source_dir = _registered_checkpoint_source_dir(
        input_dcp, source_sha256=source_sha256
    )
    if source_dir.is_symlink() or not source_dir.is_dir():
        return None
    compatible: list[tuple[int, Path]] = []
    for cache_path in source_dir.glob("*/registered_screen_cache.json"):
        if cache_path.is_symlink() or not cache_path.is_file():
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        depth = payload.get("max_stage_depth")
        if (
            payload.get("version") == _REGISTERED_SCREEN_CACHE_VERSION
            and payload.get("source_dcp_sha256") == source_sha256
            and payload.get("target_stem") == target_stem
            and isinstance(depth, int)
            and 0 < depth < _REGISTERED_PRODUCT_MAX_STAGE_DEPTH
            and _registered_closure_partial_is_resumable(cache_path.parent)
        ):
            compatible.append((depth, cache_path.parent))
    if not compatible:
        return None
    depth, source = max(compatible, key=lambda item: (item[0], str(item[1])))
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False)
    return depth


def _registered_checkpoint_candidate_ids(
    input_dcp: Path, *, source_sha256: str
) -> set[str]:
    """List complete exact-source screen identities without loading closures."""
    source_dir = _registered_checkpoint_source_dir(
        input_dcp, source_sha256=source_sha256
    )
    if source_dir.is_symlink() or not source_dir.is_dir():
        return set()
    candidate_ids = set()
    for cache_path in source_dir.glob("*/registered_screen_cache.json"):
        if cache_path.is_symlink() or not cache_path.is_file():
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        candidate_id = str(payload.get("candidate_id") or "")
        if (
            payload.get("version") == _REGISTERED_SCREEN_CACHE_VERSION
            and payload.get("source_dcp_sha256") == source_sha256
            and payload.get("max_stage_depth")
            == _REGISTERED_PRODUCT_MAX_STAGE_DEPTH
            and candidate_id.startswith("sequential:")
        ):
            candidate_ids.add(candidate_id)
    return candidate_ids


def _registered_checkpoint_dir(
    input_dcp: Path,
    *,
    source_sha256: str,
    candidate_id: str,
    target_stem: str,
) -> Path:
    """Return the cross-run checkpoint path for one exact structural screen."""
    identity = json.dumps({
        "version": _REGISTERED_SCREEN_CACHE_VERSION,
        "source_dcp_sha256": source_sha256,
        "target_stem": target_stem,
        "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
    }, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _registered_checkpoint_source_dir(
        input_dcp, source_sha256=source_sha256
    ) / key


def _publish_registered_checkpoint(source_dir: Path, checkpoint_dir: Path) -> None:
    """Atomically publish immutable closure facts for later Agent runs."""
    if checkpoint_dir.exists():
        return
    checkpoint_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_dir.with_name(
        checkpoint_dir.name + f".tmp-{uuid.uuid4().hex}"
    )
    try:
        shutil.copytree(source_dir, temporary, symlinks=False)
        try:
            temporary.replace(checkpoint_dir)
        except FileExistsError:
            shutil.rmtree(temporary)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _load_registered_screen_cache(
    closure_dir: Path,
    *,
    source_sha256: str,
    family_ref: str,
    candidate_id: str,
    target_stem: str,
    enforce_family_ref: bool = True,
    enforce_candidate_id: bool = True,
) -> RegisteredClosure:
    """Reuse screen facts only when their exact live provenance still matches."""
    cache_path = closure_dir / "registered_screen_cache.json"
    if closure_dir.is_symlink() or cache_path.is_symlink() or not cache_path.is_file():
        raise ValueError("registered screen cache is unavailable")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    expected = {
        "version": _REGISTERED_SCREEN_CACHE_VERSION,
        "source_dcp_sha256": source_sha256,
        "target_stem": target_stem,
        "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
    }
    if enforce_family_ref:
        expected["family_ref"] = family_ref
    if enforce_candidate_id:
        expected["candidate_id"] = candidate_id
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("registered screen cache provenance changed")
    closure = RegisteredClosure.load(closure_dir)
    if (
        payload.get("node_count") != len(closure.records)
        or payload.get("root_count") != len(closure.roots())
        or not closure.roots()
        or any(
            record.graph.manifest.get("dcp_sha256") != source_sha256
            for record in closure.records
        )
    ):
        raise ValueError("registered screen cache facts are incomplete or stale")
    return closure


def _find_registered_checkpoint(
    input_dcp: Path, *, source_sha256: str, family_ref: str
) -> tuple[RegisteredClosure, dict, Path] | None:
    """Find an exact-source/family closure without a process-local handoff.

    This is the restart path.  Duplicate immutable copies are accepted only
    when they describe the same target word; conflicting semantic targets are
    rejected instead of guessed.
    """
    source_dir = _registered_checkpoint_source_dir(
        input_dcp, source_sha256=source_sha256
    )
    if source_dir.is_symlink() or not source_dir.is_dir():
        return None
    matches = []
    for cache_path in sorted(source_dir.glob("*/registered_screen_cache.json")):
        if cache_path.is_symlink() or not cache_path.is_file():
            continue
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        expected = {
            "version": _REGISTERED_SCREEN_CACHE_VERSION,
            "source_dcp_sha256": source_sha256,
            "family_ref": family_ref,
            "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
        }
        if not isinstance(payload, dict) or any(
            payload.get(key) != value for key, value in expected.items()
        ):
            continue
        target_stem = str(payload.get("target_stem") or "")
        candidate_id = str(payload.get("candidate_id") or "")
        if not target_stem or not candidate_id:
            raise ValueError("registered checkpoint semantic identity is incomplete")
        checkpoint_dir = cache_path.parent
        closure = _load_registered_screen_cache(
            checkpoint_dir,
            source_sha256=source_sha256,
            family_ref=family_ref,
            candidate_id=candidate_id,
            target_stem=target_stem,
        )
        matches.append((closure, payload, checkpoint_dir))
    if not matches:
        return None
    target_stems = {str(item[1]["target_stem"]) for item in matches}
    if len(target_stems) != 1:
        raise ValueError("registered checkpoints disagree on the semantic target")
    return matches[0]


def _find_registered_checkpoint_by_target(
    input_dcp: Path, *, source_sha256: str, target_stem: str
) -> tuple[RegisteredClosure, dict, Path] | None:
    """Find an immutable closure for one exact anonymous registered word."""
    source_dir = _registered_checkpoint_source_dir(
        input_dcp, source_sha256=source_sha256
    )
    if source_dir.is_symlink() or not source_dir.is_dir():
        return None
    matches = []
    for cache_path in sorted(source_dir.glob("*/registered_screen_cache.json")):
        if cache_path.is_symlink() or not cache_path.is_file():
            continue
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        expected = {
            "version": _REGISTERED_SCREEN_CACHE_VERSION,
            "source_dcp_sha256": source_sha256,
            "target_stem": target_stem,
            "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
        }
        if not isinstance(payload, dict) or any(
            payload.get(key) != value for key, value in expected.items()
        ):
            continue
        family_ref = str(payload.get("family_ref") or "")
        candidate_id = str(payload.get("candidate_id") or "")
        if not family_ref or not candidate_id:
            raise ValueError("registered checkpoint semantic identity is incomplete")
        checkpoint_dir = cache_path.parent
        closure = _load_registered_screen_cache(
            checkpoint_dir,
            source_sha256=source_sha256,
            family_ref=family_ref,
            candidate_id=candidate_id,
            target_stem=target_stem,
            enforce_family_ref=False,
            enforce_candidate_id=False,
        )
        matches.append((closure, payload, checkpoint_dir))
    if len(matches) > 1:
        raise ValueError("registered target has multiple immutable checkpoints")
    return matches[0] if matches else None


def _find_registered_checkpoint_by_candidate(
    input_dcp: Path, *, source_sha256: str, candidate_id: str
) -> tuple[RegisteredClosure, dict, Path] | None:
    """Find one closure by its trusted anonymous family identity."""
    source_dir = _registered_checkpoint_source_dir(
        input_dcp, source_sha256=source_sha256
    )
    if source_dir.is_symlink() or not source_dir.is_dir():
        return None
    matches = []
    for cache_path in sorted(source_dir.glob("*/registered_screen_cache.json")):
        if cache_path.is_symlink() or not cache_path.is_file():
            continue
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        expected = {
            "version": _REGISTERED_SCREEN_CACHE_VERSION,
            "source_dcp_sha256": source_sha256,
            "candidate_id": candidate_id,
            "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
        }
        if not isinstance(payload, dict) or any(
            payload.get(key) != value for key, value in expected.items()
        ):
            continue
        family_ref = str(payload.get("family_ref") or "")
        target_stem = str(payload.get("target_stem") or "")
        if not family_ref or not target_stem:
            raise ValueError("registered checkpoint semantic identity is incomplete")
        checkpoint_dir = cache_path.parent
        closure = _load_registered_screen_cache(
            checkpoint_dir,
            source_sha256=source_sha256,
            family_ref=family_ref,
            candidate_id=candidate_id,
            target_stem=target_stem,
            enforce_family_ref=False,
        )
        matches.append((closure, payload, checkpoint_dir))
    target_stems = {str(item[1]["target_stem"]) for item in matches}
    if len(target_stems) > 1:
        raise ValueError("registered candidate has conflicting checkpoints")
    return matches[0] if matches else None


def _fixed_point_screen_judgment_chain(
    *,
    sequential: dict,
    summary: dict,
    resource_census: dict,
) -> dict:
    """Translate one local screen into the reusable Knowledge decision chain."""
    coverage = (sequential.get("family") or {}).get(
        "critical_path_coverage"
    ) or {}
    scope_census = sequential.get("operator_scope_census") or {}
    ordered = int(coverage.get("ordered_path_count") or 0)
    covered = int(coverage.get("covered_path_count") or 0)
    selected_scope = (
        "whole_wall" if ordered and covered == ordered
        else "partial_wall" if covered else "unresolved"
    )
    idle_dsp = resource_census.get("idle_dsp_estimate")
    resource_state = "unresolved"
    if isinstance(idle_dsp, int):
        resource_state = "supported" if idle_dsp > 0 else "contradicted"
    topology = summary.get("emitted_topology_cost") or {}
    source = summary.get("source_topology_cost") or {}
    counterfactual = summary.get("topology_counterfactual") or {}
    controls = {
        str(role): {
            key: item.get(key)
            for key in (
                "status", "member_count", "missing_member_count",
                "distinct_net_count", "distinct_polarity_count",
            )
            if key in item
        }
        for role, item in (sequential.get("controls") or {}).items()
        if isinstance(item, dict)
    }
    def step(
        condition: str,
        state: str,
        scope: str,
        evidence: dict,
        missing: list[str],
        next_discriminator: dict,
    ) -> dict:
        return {
            "condition": condition,
            "state": state,
            "scope": scope,
            "evidence": evidence,
            "missing_evidence": missing,
            "next_discriminator": next_discriminator,
        }

    return {
        "version": 2,
        "authority": "local_measurement_screen_only_not_mutation_recommendation",
        "selected_family_scope": "local_endpoint_family",
        "effect_chain": [
            "evidence_normalized",
            "pattern_present",
            "complete_generating_operator_recovered",
            "semantic_and_schedule_contract_closed",
            "hierarchy_and_constraint_owner_boundary_preserved",
            "candidate_mapping_realized",
            "current_wall_materially_covered",
            "counterfactual_next_wall_attributed",
            "trusted_signoff_measured",
        ],
        "threshold_semantics": (
            "all counts, coverage, and topology deltas are continuous evidence "
            "for Luna rather than fixed dispatch thresholds"
        ),
        "steps": [
            step(
                "evidence_normalization",
                "unresolved",
                "current_checkpoint_and_timing_sample",
                {
                    "ordered_path_count": ordered,
                    "sequential_contract_fresh_rediscovery": sequential.get(
                        "fresh_rediscovery"
                    ),
                    "route_stage_status": "not_observed_by_local_arithmetic_screen",
                    "timing_band_censoring_status": "not_observed_without_slack_vector",
                },
                [
                    "trusted_fully_routed_status",
                    "complete_slack_vector_and_top_n_censoring_check",
                    "registered_observation_tail_normalization",
                    "transformed_timing_leaf_to_logical_parent_and_site_collapse",
                ],
                {
                    "source": "latest live timing and route reports",
                    "question": (
                        "normalize the current wall before using this local "
                        "screen as evidence about global operator payoff"
                    ),
                },
            ),
            step(
                "dense_repeated_timing_wall",
                "supported" if covered else "unresolved",
                selected_scope,
                {"selected_endpoint_owner_coverage": coverage},
                ["motif_to_complete_operator_replica_mapping"],
                {"source": "current operator_mining wall evidence"},
            ),
            step(
                "complete_generating_operator_scope",
                "unresolved",
                selected_scope,
                {
                    "operator_scope_census": scope_census,
                    "local_screen_does_not_widen_scope": True,
                },
                [
                    "complete_replica_template_and_count",
                    "all_wall_owner_families_and_observable_boundary",
                ],
                {
                    "question": (
                        "widen only from mechanically linked peer families; do "
                        "not promote this endpoint family into the source operator"
                    )
                },
            ),
            step(
                "resource_carrier_mismatch",
                resource_state,
                "design_resource_census",
                {
                    "idle_dsp_estimate": idle_dsp,
                    "screened_dsp48e2_count": summary.get("dsp48e2_count"),
                    "emitted_clocked_new_cell_count": topology.get(
                        "clocked_new_cell_count"
                    ),
                },
                ["routed_macro_locality_and_clock_footprint_cost"],
                {"question": "compare carrier headroom with emitted physical cost"},
            ),
            step(
                "arithmetic_loss_contract",
                "unresolved",
                "local_endpoint_family",
                {
                    "fixed_product_count": summary.get("fixed_product_count"),
                    "passthrough_count": summary.get("passthrough_count"),
                    "quantization_group_count": summary.get(
                        "quantization_group_count"
                    ),
                    "sample_vector_count": summary.get("sample_vector_count"),
                    "local_sample_replay_passed": True,
                    "formal_source_equivalence": False,
                },
                [
                    "complete_operator_term_and_replica_coverage",
                    "formal_or_source_bound_arithmetic_equivalence",
                    "all_observable_loss_boundaries",
                ],
                {
                    "question": (
                        "treat the recovered terms as a local hypothesis until "
                        "the full generating operator is recovered"
                    )
                },
            ),
            step(
                "schedule_control_observable_contract",
                "unresolved",
                "registered_endpoint_boundary",
                {
                    "structural_boundary_recovered": sequential.get(
                        "structural_boundary_recovered"
                    ),
                    "controls": controls,
                },
                [
                    "registered_observable_closure",
                    "latency_initiation_interval_bubbles_and_protocol",
                    "complete_schedule_equivalence",
                ],
                {"question": "recover the full schedule/control shell before re-emission"},
            ),
            step(
                "hierarchy_constraint_owner_boundary",
                "unresolved",
                "local_registered_endpoint_boundary",
                {
                    "hierarchy_boundary": sequential.get("hierarchy_boundary") or {},
                    "structural_boundary_recovered": sequential.get(
                        "structural_boundary_recovered"
                    ),
                    "local_screen_does_not_prove_owner_preservation": True,
                },
                [
                    "complete_real_edif_or_hierarchy_owner_shell",
                    "constraint_owned_cell_families",
                    "launch_and_endpoint_route_ownership",
                    "pre_action_clock_sink_and_sequential_replica_sets",
                ],
                {
                    "question": (
                        "preserve the real owner shell and all constraint-owned "
                        "sequential families before any emitted mapping"
                    )
                },
            ),
            step(
                "emitted_topology_payoff",
                "unresolved",
                "registered_backend_preview",
                {
                    "source_topology_cost": source,
                    "emitted_topology_cost": topology,
                    "topology_counterfactual": counterfactual,
                    "preview_is_not_routed_timing": True,
                },
                [
                    "placed_and_routed_timing",
                    "post_rewrite_wall_profile",
                    "whole_operator_cell_and_clock_delta",
                ],
                {
                    "question": (
                        "decide whether the DSP substitution is undone by its "
                        "external fabric accumulator before selecting mutation"
                    )
                },
            ),
            step(
                "mapping_realization_and_wall_overlap",
                "unresolved",
                "candidate_backend_on_current_wall",
                {
                    "selected_endpoint_owner_coverage": coverage,
                    "operator_scope_census": scope_census,
                    "topology_preview_available": bool(topology),
                    "preview_is_not_realized_mapping": True,
                },
                [
                    "candidate_mapping_realized_on_current_design",
                    "mapped_owner_boundary_preserved",
                    "material_complete_operator_wall_overlap",
                    "exact_post_mapping_clock_sink_and_replica_delta",
                ],
                {
                    "question": (
                        "do not spend global route budget merely because local "
                        "arithmetic fits; require a realized mapping that owns the wall"
                    )
                },
            ),
            step(
                "counterfactual_post_rewrite_wall",
                "unresolved",
                "current_timing_band",
                {
                    "selected_covered_path_ranks": scope_census.get(
                        "selected_covered_path_ranks"
                    ) or [],
                    "uncovered_path_ranks": scope_census.get(
                        "uncovered_path_ranks"
                    ) or [],
                    "anonymous_peer_endpoint_owner_family_count": scope_census.get(
                        "peer_family_count"
                    ),
                },
                [
                    "alternate_arms_of_same_endpoint_or_operator",
                    "uncovered_sibling_lanes_or_replicas",
                    "next_independent_timing_family_and_slack_bound",
                ],
                {
                    "decision_owner": "luna",
                    "question": (
                        "predict the earliest surviving timing family and use it "
                        "to cap expected global benefit"
                    ),
                },
            ),
            step(
                "semantic_vs_physical_alternative",
                "unresolved",
                "decision",
                {
                    "semantic_path": "exploratory_local_measurement_candidate",
                    "physical_alternatives_remain_eligible": True,
                },
                ["same_seed_physical_basin_response"],
                {
                    "decision_owner": "luna",
                    "question": (
                        "compare this local semantic exploration with a physical "
                        "action and with widening operator recovery"
                    ),
                },
            ),
            step(
                "post_action_failure_attribution",
                "unresolved",
                "future_measurement",
                {
                    "failure_classes": [
                        "semantic_contract_failure",
                        "emitter_topology_failure",
                        "physical_basin_failure",
                    ]
                },
                ["live_mutation_and_reprofile_result"],
                {
                    "event": "post_action_reprofile",
                    "question": (
                        "do not interpret a slow external accumulator or bad route "
                        "as evidence that the recovered operator semantics are false"
                    ),
                },
            ),
        ],
    }


def _topology_counterfactual(*, sequential: dict, summary: dict) -> dict:
    """Compare source and emitted serial topology without predicting Fmax."""
    source = summary.get("source_topology_cost") or {}
    emitted = summary.get("emitted_topology_cost") or {}
    source_levels = source.get("source_primary_carry_chain_carry8_levels")
    emitted_levels = emitted.get(
        "estimated_external_carry8_levels_on_deepest_timing_chain"
    )
    if isinstance(source_levels, int) and isinstance(emitted_levels, int):
        if emitted_levels < source_levels:
            status = "structurally_positive"
        elif emitted_levels > source_levels:
            status = "structurally_negative"
        else:
            status = "serial_carry_depth_equal_other_costs_unresolved"
        serial_delta = emitted_levels - source_levels
    else:
        status = "unresolved"
        serial_delta = None
    closure = sequential.get("generating_operator_closure") or {}
    uncovered = list(closure.get("uncovered_path_ranks") or [])
    scope_census = sequential.get("operator_scope_census") or {}
    if not uncovered:
        uncovered = list(scope_census.get("uncovered_path_ranks") or [])
    return {
        "version": 1,
        "status": status,
        "authority": "pre_mutation_structural_evidence_not_fmax_prediction",
        "source": source,
        "emitted": emitted,
        "serial_carry8_level_delta": serial_delta,
        "selected_wall_covered_path_count": scope_census.get(
            "selected_covered_path_count"
        ),
        "ordered_path_count": scope_census.get("ordered_path_count"),
        "uncovered_path_count": len(uncovered),
        "earliest_predicted_surviving_path_rank": (
            min(uncovered) if uncovered else None
        ),
        "complete_generating_operator_contract_recovered": bool(
            closure.get("complete_generating_operator_contract_recovered")
        ),
        "interpretation": (
            "structural comparison is mandatory evidence for Luna; it neither "
            "authorizes mutation nor substitutes for routed timing"
        ),
    }


def _fixed_point_local_emitter_blocker(
    *, sequential: dict, summary: dict
) -> dict:
    """Reject the known-wrong combinational backend architecture.

    This is a mechanical topology contradiction rather than a QoR threshold:
    an unregistered emitter with a deeper serial carry chain cannot realize
    the claimed registered-wall removal mechanism.
    """
    counterfactual = summary.get("topology_counterfactual") or {}
    emitted = summary.get("emitted_topology_cost") or {}
    closure = sequential.get("generating_operator_closure") or {}
    scope = sequential.get("operator_scope_census") or {}
    reasons: list[str] = []
    if counterfactual.get("status") == "structurally_negative":
        reasons.append("emitted_serial_carry_depth_exceeds_source")
    if int(emitted.get("dsp_internal_pipeline_register_count") or 0) == 0:
        reasons.append("no_dsp_pipeline_or_registered_schedule_cut")
    if emitted.get("uses_external_fabric_accumulation") is True:
        reasons.append("dsp_output_rebuilds_external_fabric_accumulator")
    if closure.get("complete_generating_operator_contract_recovered") is not True:
        reasons.append("complete_multi_family_operator_closure_missing")
    if int(scope.get("uncovered_path_count") or 0) > 0:
        reasons.append("sampled_timing_wall_remains_uncovered")
    blocked = bool(reasons)
    return {
        "version": 1,
        "blocked": blocked,
        "backend": "combinational_local_fixed_point_dsp_emitter",
        "reason_codes": reasons,
        "reason": (
            "fixed-point local emitter rejected before mutation: "
            + ", ".join(reasons)
            if blocked
            else "fixed-point local emitter topology has no mechanical blocker"
        ),
        "required_backend": "registered_multi_family_fixed_point_bridge",
        "required_mechanism": [
            "complete_repeated_family_and_observable_shell_coverage",
            "schedule_compatible_dsp_internal_registers_or_cascade",
            "registered_accumulation_boundary",
            "no_deeper_external_serial_carry_chain",
        ],
        "authority": "generated_topology_contradiction_not_fmax_prediction",
    }


def _public_closure_summary(value: dict) -> dict:
    """Remove internal family identities while retaining closure evidence."""
    closure = value if isinstance(value, dict) else {}
    groups = []
    for item in closure.get("wall_groups") or []:
        if not isinstance(item, dict):
            continue
        groups.append({
            key: item.get(key)
            for key in (
                "group_ref", "covered_path_ranks", "covered_path_count",
                "family_count", "evidence_relation",
            )
            if key in item
        } | {
            "endpoint_owner_family_count": len(
                item.get("endpoint_owner_candidate_ids") or []
            ),
            "non_endpoint_support_family_count": len(
                item.get("non_endpoint_support_candidate_ids") or []
            ),
        })
    return {
        key: closure.get(key)
        for key in (
            "version", "status", "authority", "selected_family_scope",
            "selected_group_ref",
            "complete_generating_operator_contract_recovered",
            "family_count", "wall_group_count", "ordered_path_count",
            "all_family_covered_path_count",
            "all_family_covered_path_ranks", "uncovered_path_count",
            "uncovered_path_ranks", "closed_obligations",
            "open_obligations", "interpretation",
        )
        if key in closure
    } | {"wall_groups": groups}


def _control_aliases_in_closure_namespace(
    alias_facts: Mapping[str, object],
    observed_sink_pins: Mapping[str, str],
) -> dict[str, str]:
    """Translate RapidWright net aliases through exact shared sink pins.

    Vivado closure TSVs and RapidWright can use different canonical strings
    for the same routed control net.  The proof is requested with exact sink
    pins, so those pins are the cross-tool identity boundary.  Never install
    RapidWright net strings directly into a closure indexed by Vivado names.
    """
    pin_to_observed: dict[str, str] = {}
    for observed_net, pin_name in observed_sink_pins.items():
        if (
            not isinstance(observed_net, str)
            or not observed_net
            or not isinstance(pin_name, str)
            or not pin_name
            or pin_name in pin_to_observed
        ):
            raise RuntimeError("registered control sink-pin namespace is invalid")
        pin_to_observed[pin_name] = observed_net

    groups = alias_facts.get("groups")
    flat_aliases = alias_facts.get("net_aliases")
    if not isinstance(groups, list) or not isinstance(flat_aliases, dict):
        raise RuntimeError("registered control equivalence proof has no alias groups")

    translated: dict[str, str] = {}
    covered_pins: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise RuntimeError("registered control equivalence group is invalid")
        representative = group.get("representative_net")
        members = group.get("members")
        if not isinstance(representative, str) or not isinstance(members, list):
            raise RuntimeError("registered control equivalence group is incomplete")

        representative_pin = None
        for member in members:
            if (
                isinstance(member, dict)
                and member.get("canonical_net") == representative
                and isinstance(member.get("pin_name"), str)
            ):
                representative_pin = member["pin_name"]
                break
        representative_observed = pin_to_observed.get(representative_pin or "")
        if representative_observed is None:
            raise RuntimeError(
                "registered control representative is outside the closure namespace"
            )

        for member in members:
            if not isinstance(member, dict):
                raise RuntimeError("registered control equivalence member is invalid")
            pin_name = member.get("pin_name")
            canonical_net = member.get("canonical_net")
            observed_net = pin_to_observed.get(
                pin_name if isinstance(pin_name, str) else ""
            )
            if (
                observed_net is None
                or not isinstance(canonical_net, str)
                or flat_aliases.get(canonical_net) != representative
            ):
                raise RuntimeError(
                    "registered control equivalence proof does not cover the closure"
                )
            translated[observed_net] = representative_observed
            covered_pins.add(pin_name)

    if covered_pins != set(pin_to_observed):
        raise RuntimeError(
            "registered control equivalence proof has incomplete sink-pin coverage"
        )
    return translated


async def _install_registered_ce_aliases(
    mcp, closure: RegisteredClosure, source_sha256: str
) -> dict:
    """Install exact-source CE aliases required by one recovered closure."""
    ce_sink_pins = closure.control_sink_pins("ce_net")
    proof = {
        "status": "not_needed",
        "unique_net_count": len(ce_sink_pins),
        "group_count": len(ce_sink_pins),
        "aliased_net_count": 0,
    }
    if len(ce_sink_pins) <= 1:
        return proof
    alias_facts = _payload(await mcp.call_rw(
        "prove_control_pin_equivalence_groups",
        {"pin_names": list(ce_sink_pins.values()), "role": "CE"},
        timeout=300.0,
    ))
    aliases = alias_facts.get("net_aliases")
    if (
        alias_facts.get("status") != "success"
        or alias_facts.get("kind")
        != "proved_control_pin_equivalence_groups"
        or alias_facts.get("design_sha256") != source_sha256
        or alias_facts.get("complete_pin_coverage") is not True
        or not isinstance(aliases, dict)
    ):
        raise RuntimeError("registered CE equivalence proof is incomplete")
    closure.install_proved_control_net_aliases(
        "ce_net", _control_aliases_in_closure_namespace(alias_facts, ce_sink_pins)
    )
    return {
        "status": "proved",
        "unique_net_count": int(alias_facts.get("unique_net_count") or 0),
        "group_count": int(alias_facts.get("group_count") or 0),
        "aliased_net_count": int(alias_facts.get("aliased_net_count") or 0),
        "method": "exact_source_transition_relation",
    }


async def screen_fixed_point_dsp_contract(
    mcp,
    *,
    input_dcp: Path,
    family_ref: str,
    cone_dir: Path,
    selected_family_only: bool = False,
) -> dict:
    """Recover the registered-product backend that the live action will use."""
    source = Path(input_dcp).resolve()
    destination = Path(cone_dir).resolve()
    if source.is_symlink() or not source.is_file():
        raise ValueError("fixed-point screen input DCP is unavailable")
    source_sha256 = sha256_file(source)
    cached = _find_registered_checkpoint(
        source, source_sha256=source_sha256, family_ref=family_ref
    )
    try:
        handoff = resolve_sequential_family_handoff(
            mcp, artifact_sha256=source_sha256, family_ref=family_ref
        )
    except ValueError:
        if cached is None:
            raise
        handoff = {}

    sequential = {
        "fresh_rediscovery": False,
        "family": {},
        "controls": {},
        "data_cone_traceback": {},
        "generating_operator_closure": {},
        "operator_action_contract": {},
    }
    target_stem = str(cached[1]["target_stem"]) if cached else ""
    target_width = None
    if handoff:
        await mcp.call_rw(
            "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
        )
        await mcp.call_rw(
            "read_checkpoint", {"dcp_path": str(source)}, timeout=600.0
        )
        sequential = _payload(await mcp.call_rw(
            "recover_sequential_operator_contract",
            {
                "critical_paths_data": handoff["critical_paths_data"],
                "candidate_id": handoff["candidate_id"],
            },
            timeout=600.0,
        ))
        target_stem, target_width = _compressor_target_stem(
            sequential, source_sha256, handoff["candidate_id"]
        )
        if (
            cached is not None
            and cached[1].get("candidate_id") != handoff["candidate_id"]
        ):
            cached = None

    if cached is not None:
        closure, _, checkpoint_dir = cached
        cache_scope = (
            "persistent_checkpoint" if handoff
            else "persistent_checkpoint_resume"
        )
    else:
        checkpoint_dir = _registered_checkpoint_dir(
            source,
            source_sha256=source_sha256,
            candidate_id=handoff["candidate_id"],
            target_stem=target_stem,
        )
        if destination.exists():
            run_dir = Path(mcp.run_dir).resolve()
            if (
                destination.parent != run_dir
                or not destination.name.startswith("operator_fixed_point_screen_")
                or destination.is_symlink()
                or not destination.is_dir()
            ):
                raise RuntimeError("screen closure path is not resumable")
        extraction = await _extract_registered_closure_with_resume(
            mcp,
            {
                "dcp_path": str(source),
                "source_dcp_sha256": source_sha256,
                "target_stem": target_stem,
                "output_dir": str(destination),
                "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
                "word_width": target_width,
            },
        )
        if (
            extraction.get("read_only_after_restore") is not True
            or extraction.get("source_checkpoint_reopened") is not True
        ):
            raise RuntimeError(
                "registered-product closure extraction did not restore the seed"
            )
        closure = RegisteredClosure.load(destination)
        _write_registered_screen_cache(
            destination,
            source_sha256=source_sha256,
            family_ref=family_ref,
            candidate_id=handoff["candidate_id"],
            target_stem=target_stem,
            closure=closure,
        )
        _publish_registered_checkpoint(destination, checkpoint_dir)
        cache_scope = "fresh_published"

    # The registered data graph is stable across physical basins, but phys_opt
    # may split one logical CE into several equivalent source replicas.  The
    # semantic recovery must compare their proved transition relation rather
    # than their post-route net strings.  This is an internal exact-DCP proof;
    # object names never enter the LLM response.
    if len(closure.control_sink_pins("ce_net")) > 1 and not handoff:
        await mcp.call_rw(
            "initialize_rapidwright", {"jvm_max_memory": "8G"}, timeout=120.0
        )
        await mcp.call_rw(
            "read_checkpoint", {"dcp_path": str(source)}, timeout=600.0
        )
    control_alias_proof = await _install_registered_ce_aliases(
        mcp, closure, source_sha256
    )
    bundle = _load_registered_fir_bundle_cache(
        checkpoint_dir,
        source_sha256=source_sha256,
        closure=closure,
    )
    generating_recovery = {
        "status": "recovered" if bundle is not None else "not_attempted",
        "failure_scope": "selected_family_only",
        "remaining_family_exploration_allowed": True,
        "alternate_contract_used": False,
    }
    if bundle is None:
        try:
            bundle = build_generating_operator_bridge_bundle(closure)
        except ValueError as exc:
            generating_recovery = {
                "status": "rejected",
                "reason": str(exc)[:500],
                "failure_scope": "selected_family_only",
                "remaining_family_exploration_allowed": True,
                "alternate_contract_used": True,
                "alternate_contract": "registered_product_bundle",
            }
            bundle = build_registered_product_bundle(closure)
        else:
            generating_recovery = {
                "status": "recovered",
                "failure_scope": "selected_family_only",
                "remaining_family_exploration_allowed": True,
                "alternate_contract_used": False,
            }
        if bundle.get("kind") == "generating_operator_bridge_bundle":
            try:
                _write_registered_fir_bundle_cache(
                    checkpoint_dir,
                    source_sha256=source_sha256,
                    closure=closure,
                    bundle=bundle,
                )
            except ValueError:
                # Non-FIR generating bridges retain the existing screen path.
                pass
    generating_bridge = bundle.get("kind") == "generating_operator_bridge_bundle"
    replica_discovery = {
        "status": (
            "selected_family_only"
            if selected_family_only
            else "not_applicable_to_local_backend"
        ),
        "recovered_replica_count": 1,
        "accepted_bank_count": 1,
        "rejections": [],
    }
    if generating_bridge and handoff and not selected_family_only:
        replica_facts = _payload(await mcp.call_rw(
            "recover_sequential_operator_replicas",
            {
                "critical_paths_data": handoff["critical_paths_data"],
                "candidate_id": handoff["candidate_id"],
                "max_replicas": 64,
            },
            timeout=900.0,
        ))
        if (
            replica_facts.get("kind")
            != "sequential_operator_replica_family_facts"
            or replica_facts.get("read_only") is not True
            or replica_facts.get("fresh_rediscovery") is not True
            or replica_facts.get("mutation_eligible") is not False
            or not isinstance(replica_facts.get("replicas"), list)
        ):
            raise RuntimeError("registered operator replica recovery is incomplete")
        bank_bundles = [bundle]
        seen_target_stems = {target_stem}
        covered_root_stems = {
            record.root_stem for record in closure.roots()
        }
        rejections = []
        ordered_replicas = _order_replica_screen_candidates(
            replica_facts["replicas"],
            selected_target_stem=target_stem,
            cached_candidate_ids=_registered_checkpoint_candidate_ids(
                source, source_sha256=source_sha256
            ),
        )
        for replica_ordinal, replica in ordered_replicas:
            try:
                peer_stem, peer_width, peer_candidate_id = _replica_target_stem(
                    replica
                )
                if peer_stem in seen_target_stems:
                    continue
                seen_target_stems.add(peer_stem)
                if peer_stem in covered_root_stems:
                    rejections.append({
                        "status": "already_covered_by_observable_bank",
                    })
                    continue
                peer_cached = _find_registered_checkpoint_by_candidate(
                    source,
                    source_sha256=source_sha256,
                    candidate_id=peer_candidate_id,
                )
                if peer_cached is not None:
                    peer_closure = peer_cached[0]
                    peer_cache_scope = "persistent_checkpoint_resume"
                else:
                    peer_dir = Path(mcp.run_dir) / (
                        "operator_fixed_point_screen_replica_"
                        f"{replica_ordinal:02d}"
                    )
                    extraction = await _extract_registered_closure_with_resume(
                        mcp,
                        {
                            "dcp_path": str(source),
                            "source_dcp_sha256": source_sha256,
                            "target_stem": peer_stem,
                            "output_dir": str(peer_dir),
                            "max_stage_depth": _REGISTERED_PRODUCT_MAX_STAGE_DEPTH,
                            "word_width": peer_width,
                        },
                    )
                    if (
                        extraction.get("read_only_after_restore") is not True
                        or extraction.get("source_checkpoint_reopened") is not True
                    ):
                        raise RuntimeError(
                            "replica closure extraction did not restore the seed"
                        )
                    peer_closure = RegisteredClosure.load(peer_dir)
                    _write_registered_screen_cache(
                        peer_dir,
                        source_sha256=source_sha256,
                        family_ref="replica_of:" + family_ref,
                        candidate_id=peer_candidate_id,
                        target_stem=peer_stem,
                        closure=peer_closure,
                        replica_of_family_ref=family_ref,
                    )
                    peer_checkpoint = _registered_checkpoint_dir(
                        source,
                        source_sha256=source_sha256,
                        candidate_id=peer_candidate_id,
                        target_stem=peer_stem,
                    )
                    _publish_registered_checkpoint(peer_dir, peer_checkpoint)
                    peer_cache_scope = "fresh_published"
                # A peer closure is independently replay-proved, but it also
                # needs its own source-bound CE alias map.  Reusing only the
                # primary map leaves equivalent control nets non-uniform.
                await _install_registered_ce_aliases(
                    mcp, peer_closure, source_sha256
                )
                peer_bundle = build_generating_operator_bridge_bundle(
                    peer_closure
                )
                merged = merge_generating_operator_bridge_bundles(
                    [*bank_bundles, peer_bundle]
                )
                prior_bank_count = len(bank_bundles)
                merged_bank_count = int(merged["recovery"]["bank_count"])
                if merged_bank_count == prior_bank_count:
                    rejections.append({
                        "status": "duplicate_observable_bank",
                        "cache_scope": peer_cache_scope,
                    })
                    continue
                if merged_bank_count != prior_bank_count + 1:
                    raise RuntimeError(
                        "replica bundle merge changed bank count unexpectedly"
                    )
                bank_bundles.append(peer_bundle)
                covered_root_stems.update(
                    record.root_stem for record in peer_closure.roots()
                )
                bundle = merged
            except (RuntimeError, ValueError) as replica_exc:
                rejections.append({
                    "status": "semantic_replica_rejected",
                    "reason": str(replica_exc)[:500],
                })
        bundle = merge_generating_operator_bridge_bundles(bank_bundles)
        replica_discovery = {
            "status": "complete",
            "recovered_replica_count": int(
                replica_facts.get("replica_count", 0)
            ),
            "shape_match_count_before_cone_signature": int(
                replica_facts.get(
                    "shape_match_count_before_cone_signature", 0
                )
            ),
            "accepted_bank_count": int(bundle["recovery"]["bank_count"]),
            "expansion_policy": "all_semantically_compatible_live_banks",
            "rejections": rejections,
            "identity_features_excluded": list(
                replica_facts.get("excluded_identity_features") or []
            ),
        }
    plan = bundle["plans"][0]
    registered_action_eligible = bool(
        isinstance(bundle.get("plans"), list)
        and bundle["plans"]
        and isinstance(bundle.get("instances"), list)
        and bundle["instances"]
        and (bundle.get("recovery") or {}).get("external_plan_used") is False
    )
    registered_fir_action_eligible = False
    try:
        _require_registered_fir_data_cone_bundle(bundle)
    except ValueError:
        pass
    else:
        registered_fir_action_eligible = True
    if target_width is None:
        target_width = int(plan["data_width"])
    instance_count = len(bundle["instances"])
    if generating_bridge:
        all_terms = list(plan["terms"])
        fixed_terms = [
            term for term in all_terms
            if term["kind"] == "fixed_product_slice"
        ]
        term_count = len(all_terms)
        fixed_product_count = len(fixed_terms)
        passthrough_count = term_count - fixed_product_count
        quantization_group_count = term_count
        interacting_group_count = 0
        contract_terms = [
            {
                "kind": term["kind"],
                "coefficient": term.get("coefficient", 1),
                "input_signed": term["data_signed"],
            }
            for term in all_terms
        ]
        schedule_kind = "generating_operator_bridge_v2"
        pipeline_depth = 4
        schedule_cut_depth = bundle["recovery"]["schedule_cut_depth"]
        replay_vector_count = bundle["recovery"]["replay_vector_count"]
    elif plan["schedule_kind"] == "registered_branch_state_dag_v1":
        all_terms = [
            term for state in plan["states"] for term in state["terms"]
        ] + list(plan["output_state"]["terms"])
        fixed_terms = [
            term for term in all_terms
            if term["kind"] == "fixed_product_slice"
        ]
        term_count = len(all_terms)
        fixed_product_count = len(fixed_terms)
        passthrough_count = term_count - fixed_product_count
        quantization_group_count = len(plan["output_state"]["groups"])
        interacting_group_count = sum(
            len(group["term_ordinals"]) > 1
            for group in plan["output_state"]["groups"]
        )
        contract_terms = [{
            "kind": "fixed_product_slice",
            "coefficient": term["coefficient"],
            "input_signed": term["data_signed"],
        } for term in fixed_terms]
        schedule_kind = plan["schedule_kind"]
        pipeline_depth = plan["pipeline_depth"]
        schedule_cut_depth = plan["recovery"]["schedule_cut_depth"]
        replay_vector_count = plan["recovery"]["replay_vector_count"]
    else:
        term_count = len(plan["coefficients"])
        fixed_product_count = term_count
        passthrough_count = 0
        quantization_group_count = 1
        interacting_group_count = 1
        contract_terms = [
            {
                "kind": "fixed_product_slice",
                "coefficient": coefficient,
                "input_signed": signed,
            }
            for coefficient, signed in zip(
                plan["coefficients"], plan["input_signed"]
            )
        ]
        schedule_kind = plan["schedule_kind"]
        pipeline_depth = plan["pipeline_depth"]
        schedule_cut_depth = plan["recovery"]["schedule_cut_depth"]
        replay_vector_count = plan["recovery"]["replay_vector_count"]
    if fixed_product_count and passthrough_count:
        semantic_operator_class = "registered_fixed_point_multiply_accumulate"
    elif fixed_product_count > 1:
        semantic_operator_class = "registered_fixed_point_product_sum"
    elif fixed_product_count == 1:
        semantic_operator_class = "registered_fixed_point_multiply"
    else:
        semantic_operator_class = "registered_add_or_transport"
    semantic_signature = {
        "version": 1,
        "authority": "closure_replay_proved_local_semantics",
        "operator_class": semantic_operator_class,
        "primitive_operators": {
            "constant_multiply_term_count": fixed_product_count,
            "passthrough_or_accumulator_term_count": passthrough_count,
            "additive_term_count": term_count,
            "joint_quantization_group_count": quantization_group_count,
            "interacting_quantization_group_count": interacting_group_count,
        },
        "schedule": {
            "kind": schedule_kind,
            "pipeline_depth": pipeline_depth,
            "independent_state_count": (
                len(plan["states"])
                if schedule_kind == "registered_branch_state_dag_v1"
                else term_count
            ),
            "heterogeneous_clock_enables_preserved": (
                schedule_kind == "registered_branch_state_dag_v1"
            ),
        },
        "proof_basis": [
            "complete_boolean_closure_replay",
            "word_level_fixed_product_inference",
            "joint_shift_and_quantization_replay",
            "registered_control_schedule_recovery",
        ],
        "scope": "live_seed_family_plus_anonymous_replay_proved_banks",
        "routed_qor_still_unknown": True,
    }
    # Count the physical DSP multiplier tiles implied by operand widths.  A
    # logical product is not necessarily one DSP48E2: for example a signed
    # 32x26 product tiles most cheaply as two 27x18-oriented DSPs.  The old
    # one-term/one-DSP estimate under-reported the measured v7 experiment by 2x.
    instances_by_module: dict[str, int] = {}
    for instance in bundle.get("instances") or []:
        module_name = str(instance.get("module_name") or "")
        instances_by_module[module_name] = (
            instances_by_module.get(module_name, 0) + 1
        )

    def multiplier_tiles(term: dict) -> int:
        if term.get("kind") != "fixed_product_slice":
            return 0
        data_width = int(term.get("data_width") or 0)
        coefficient_width = int(term.get("coefficient_width") or 0)
        if data_width <= 0 or coefficient_width <= 0:
            return 0
        orient_a = ((data_width + 26) // 27) * (
            (coefficient_width + 17) // 18
        )
        orient_b = ((data_width + 17) // 18) * (
            (coefficient_width + 26) // 27
        )
        return min(orient_a, orient_b)

    dsp_count = 0
    for module_plan in bundle.get("plans") or []:
        module_name = str(module_plan.get("module_name") or "")
        terms = []
        for state in module_plan.get("states") or []:
            terms.extend(state.get("terms") or [])
        terms.extend((module_plan.get("output_state") or {}).get("terms") or [])
        dsp_count += instances_by_module.get(module_name, 0) * sum(
            multiplier_tiles(term) for term in terms
        )
    contract = {
        "kind": "registered_product_closure_screen",
        "version": 1,
        "terms": contract_terms,
    }
    source_topology = {
        "backend_input": "current_dcp_registered_closure",
        "closure_node_count": len(closure.records),
        "closure_root_count": len(closure.roots()),
        "selected_instance_count": instance_count,
        "registered_bank_count": int(
            bundle.get("recovery", {}).get("bank_count", 1)
        ),
        "module_count": len(bundle["plans"]),
        "replica_discovery": replica_discovery,
        "checkpoint_scope": cache_scope,
        "control_alias_proof": control_alias_proof,
        "traceback_status": (
            sequential.get("data_cone_traceback") or {}
        ).get("status"),
    }
    emitted_topology = {
        "backend": schedule_kind,
        "dsp48e2_count": dsp_count,
        "dsp48e2_count_status": (
            "deterministic_emitter_plan_pending_synthesis_confirmation"
        ),
        "selected_instance_count": instance_count,
        "term_count_per_instance": term_count,
        "data_width": plan["data_width"],
        "pipeline_depth": pipeline_depth,
        "schedule_cut_depth": schedule_cut_depth,
        "dsp_product_registers_recovered": True,
        "tail_register_schedule_recovered": True,
        "external_plan_used": False,
        "mapping_and_routed_timing_unknown_until_action": True,
    }
    topology_counterfactual = {
        "version": 2,
        "status": (
            "typed_registered_fir_rewrite_ready"
            if registered_fir_action_eligible
            else "typed_registered_operator_rewrite_ready"
            if registered_action_eligible
            else "registered_schedule_candidate_recovered"
        ),
        "authority": "pre_mutation_live_backend_evidence_not_fmax_prediction",
        "source": source_topology,
        "emitted": emitted_topology,
        "complete_generating_operator_contract_recovered": (
            generating_bridge and registered_action_eligible
        ),
        "interpretation": (
            "the actual registered backend recovered a schedule-preserving "
            "measurement candidate; mapping and routed QoR remain unknown"
        ),
    }
    summary = {
        "status": (
            "generating_operator_bundle_recovered"
            if generating_bridge
            else "alternate_registered_product_bundle_recovered"
        ),
        "generating_operator_recovery": generating_recovery,
        "term_count": term_count,
        "fixed_product_count": fixed_product_count,
        "passthrough_count": passthrough_count,
        "dsp48e2_count": dsp_count,
        "sample_vector_count": replay_vector_count,
        "quantization_group_count": quantization_group_count,
        "interacting_group_count": interacting_group_count,
        "proved_semantic_signature": semantic_signature,
        "source_topology_cost": source_topology,
        "emitted_topology_cost": emitted_topology,
        "topology_counterfactual": topology_counterfactual,
        "registered_bank_count": source_topology["registered_bank_count"],
        "module_count": source_topology["module_count"],
        "selected_instance_count": instance_count,
        "replica_discovery": replica_discovery,
        "registered_fir_action_eligible": registered_fir_action_eligible,
        "registered_action_eligible": registered_action_eligible,
    }
    judgment_chain = _fixed_point_screen_judgment_chain(
        sequential=sequential,
        summary=summary,
        resource_census=handoff.get("resource_census") or {},
    )
    public_closure = _public_closure_summary(
        sequential.get("generating_operator_closure") or {}
    )
    if generating_bridge and registered_action_eligible:
        # The mining union may cover only part of the sampled whole-DCP wall,
        # while the backend has nevertheless proved every root and control in
        # the selected registered family.  Do not misreport the former as an
        # incomplete typed action.  This carries no routed-QoR claim.
        public_closure = {
            **public_closure,
            "status": "complete_selected_registered_family",
            "authority": "exact_current_dcp_closure_replay",
            "selected_family_scope": "selected_live_registered_family",
            "complete_generating_operator_contract_recovered": True,
            "family_count": len(closure.roots()),
            "closed_obligations": [
                "complete_selected_family_data_roots",
                "fixed_point_loss_boundaries",
                "registered_control_schedule",
                "observable_target_register_write",
            ],
            "open_obligations": [
                "post_mutation_mapping_and_routed_fmax"
            ],
        }
    return {
        "source_sha256": source_sha256,
        "target_width": target_width,
        "contract": contract,
        "registered_product_bundle": bundle,
        "summary": summary,
        "generating_operator_closure": public_closure,
        "operator_action_contract": (
            {
                "action_type": (
                    "registered_fir_data_cone_rewrite"
                    if registered_fir_action_eligible
                    else "fixed_point_dsp_rebuild_v1"
                ),
                "scope": "selected_live_registered_family",
                "authority": (
                    "exact_current_dcp_generating_operator_replay"
                    if generating_bridge
                    else "exact_alternate_local_fixed_point_replay"
                ),
                "complete_operator_rewrite_eligible": bool(generating_bridge),
                "reason": (
                    "complete generating operator recovered; routed QoR pending"
                    if generating_bridge
                    else (
                        "selected generating operator was rejected; an alternate "
                        "local fixed-point contract exists but sibling-family "
                        "exploration remains open"
                    )
                ),
            }
            if registered_action_eligible
            else sequential.get("operator_action_contract") or {}
        ),
        "operator_judgment_chain": judgment_chain,
    }


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
