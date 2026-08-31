"""FDAgents — LLM-driven FPGA timing optimization agent (FPL'26 contest).

Architecture (v3):
  - agent.py        thin orchestrator: measure -> ask LLM -> execute -> accept/rollback
  - stages.py       native-reasoning LLM stages (analyze / decide+reflect / probe)
  - knowledge_base  case library with measured evidence (advisory, ranked by match)
  - legality.py     the ONLY hard rules: contest legality, nothing strategic
  - config.yaml     every tunable
  - memory.py       pure bookkeeping, fully surfaced to the LLM

The LLM owns strategy. Python owns mechanics: measurement, checkpoint
handling, rollback, budgets, and contest-legality enforcement.

Canonical graph artifacts are atomically published. Protected Level 1 garbage
collection distinguishes search pruning from physical artifact deletion.

Usage:
    python -m FDAgents.agent input.dcp [--output out.dcp] [--no-llm]
                             [--time-limit 3600] [--model gpt-5.6-luna]
                             [--config my_config.yaml] [--debug]
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import math
import re
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from . import legality
from . import candidate_graph as graph_ops
from .artifacts import (
    atomic_copy_file,
    atomic_write_json,
    canonical_candidate_path,
    cleanup_level1_artifacts,
    sha256_file,
    validate_owned_candidate_path,
)
from .action_proof import (
    append_action_evidence,
    build_action_outcome_record,
    build_action_proof,
    complete_action_proof,
    resolve_registry_path,
    validate_action_proof_completion,
)
from .action_menu import (
    ActionMenuError,
    REIMPLEMENTING_DESTRUCTIVE_SCOPES,
    action_cost_key,
    action_execution_window_requirement,
    build_action_menu,
    build_degraded_action_menu,
    build_recovery_action_menu,
    filter_action_menu_for_exhausted_entries,
    filter_action_menu_for_execution_window,
    reserve_top_ranked_launch_opportunity,
    filter_action_menu_for_permitted_actions,
    prioritize_last_launch_opportunity,
    project_action_menu_top_k,
    replace_action_menu_family_slice,
    refresh_action_menu_same_run_feedback,
    select_action_menu_entry,
    validate_action_menu,
    validate_action_menu_invalidation,
)
from .action_contracts import contract_for
from .action_cards import (
    build_consumer_rewrite_probe_action_card,
    build_current_seed_action_cards,
    build_operator_preflight_action_cards,
    build_operator_probe_action_cards,
)
from .action_parameterization import derive_implementation_recipe_params
from .capabilities import (
    CapabilityError,
    ResolvedAction,
    resolve_replay_action,
    resolve_skill_action,
)
from .bo_advisor import build_bo_advisory
from .config import Config, load_config
from .cold_product_sum import recover_cold_product_sum_families
from .concurrent_proof import (
    ConcurrentProofAction,
    ShadowPrefetchResult,
    configured_proof_timeouts,
    discard_shadow_prefetch,
    finalize_shadow_prefetch,
    run_optional_concurrent_action,
)
from .corpus import CorpusSnapshot
from .features import extract_features
from .knowledge_base import KnowledgeBase
from .isolation import IsolationPolicyError, load_isolation_policy
from .llm import LLMClient, ToolLoopError
from .mcp_client import MCPClient, is_structural_non_applicability
from .memory import Memory
from .measurement import failed_measurement, measurement_legality, measure_candidate
from .offline_decision import (
    OfflineDecisionError,
    write_offline_decision_snapshot,
)
from .no_replay_provenance import (
    NoReplayLedger,
    NoReplayProvenanceError,
    new_run,
    validate_development_allowlist,
    validate_engineering_regression_manifest,
)
from .naming import is_unsuitable_fanout_opt_net_name, is_unsuitable_force_replicate_net_name
from .operator_extract import (
    banked_ram_operator_preflight_facts,
    banked_wall_needs_multilevel_control_recovery,
    build_operator_extraction_profile,
    build_operator_action_preflight,
    build_repeated_arithmetic_motif_report,
    build_semantic_action_preflight,
    homogeneous_repeated_wall_needs_semantic_discriminator,
    repeated_wall_has_memory_endpoint,
    validate_operator_action_preflight,
    validate_operator_mining_root,
    validate_recurrence_boundary_hypotheses,
    validate_repeated_arithmetic_motif_report,
    validate_semantic_action_preflight,
)
from .probe_handoff import (
    capture_probe_response,
    compact_critical_paths_for_operator_mining,
    prepare_probe_arguments,
    retain_recurrence_family_facts,
)
from .replay import (
    ACTION_TEMPLATES,
    POLICY_ID as REPLAY_POLICY_ID,
    ActionCycleOutcome,
    ReplayPolicy,
    ReplayState,
    SelectionAuthorization,
    SiblingOutcome,
)
from .saturation import resolve_strategy_family
from .scoring import project_action_score_envelope
from .parsers import (
    parse_congestion_report,
    parse_high_fanout_capabilities,
    parse_high_fanout_nets,
    parse_high_fanout_timing_evidence,
    parse_utilization_report,
)
from .physical_signature import (
    build_physical_signature,
    collect_physical_signature,
    unavailable_signature,
)
from .prompts import (
    registered_action_targets_by_skill,
    registered_zero_argument_current_design_probes,
)
from .probe_planner import probe_decision_display, probe_fact_already_available
from .operator_proof import (
    OPERATOR_CANDIDATE_PROOF_VERSION as TEAMMATE_OPERATOR_PROOF_VERSION,
    verify_operator_measurement_admission_certificate,
)
from .operator_proof_current import (
    OPERATOR_CANDIDATE_PROOF_VERSION as CURRENT_OPERATOR_PROOF_VERSION,
    fixed_point_contract_set_has_executor_authority,
    verify_operator_post_emission_certificate,
)
from .recurrence_lineage import (
    prepare_recurrence_lineage_outputs,
    recurrence_lineage_context_for_seed,
    validate_full_recurrence_lineage,
    validate_recurrence_lineage_candidate,
)
from .registered_product_sum_provider import build_product_sum_preflight_receipt
from .residual_profile import (
    build_measured_residual_profile,
    capture_live_timing_report,
    metaloop_hypothesis_counts,
    metaloop_live_wall_counts,
)
from .regarith_tools.agent_evidence import (
    build_concurrent_proof_decision_packet,
    build_registered_arithmetic_decision_packet,
    screen_recovered_registered_arithmetic,
    screen_recovered_recurrence_transport,
    refresh_registered_arithmetic_product_sum_slice,
    validate_concurrent_proof_decision_packet,
    validate_registered_arithmetic_decision_packet,
)
from .regarith_tools.common import object_digest
from .skills import SKILLS
from .skills.operator_rewrite import (
    recover_constructive_nomination_contract,
    recover_memory_forward_nomination_contract,
)
from .skills.implementation_recipe import (
    IMPLEMENTATION_RECIPE_TARGETS,
    implementation_recipe_catalog,
)
from .skills.phys_opt import PHYS_OPT_PRESETS, phys_opt_catalog
from .skills.base import (
    REGISTERED_ARITHMETIC_PROOF_MAX_BYTES,
    SkillContractError,
    SkillResult,
    calculate_fmax,
    parse_timing_summary_static,
    validate_skill_outputs,
)
from .skills.critical_net_reroute import (
    _build_boundary_link_discovery_tcl,
    _build_worst_source_discovery_tcl,
    _parse_boundary_link_discovery,
    _parse_discovered_nets,
)
from .skills.endpoint_bel_move import (
    _build_endpoint_safety_evidence_tcl,
    _parse_endpoint_safety_evidence,
)
from .skills.semantic_replay import (
    PROOF_VERSION as SEMANTIC_REPLAY_PROOF_VERSION,
    verify_semantic_replay_proof,
)
from .stages import (
    Decision,
    OPERATOR_PREFLIGHT_COUNT_FIELDS,
    ReplayStageRunner,
    StageRunner,
    registered_operator_directions,
)
from .submission import finalize_submission
from .task_scheduler import LocalTaskScheduler, TaskKind, TaskSpec

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

logger = logging.getLogger("FDAgents")

HIGH_RISK_ECO_SKILLS = frozenset({
    "equivalent_source_remap", "operator_rewrite", "semantic_replay",
})
# Once a full recurrence lineage is proved on a seed, only these two actions
# may run on it.  The menu is projected against the same set, because a card
# the planner is shown but cannot execute costs a whole decision cycle and
# three of them end the run.
RECURRENCE_LINEAGE_PERMITTED_ACTIONS = frozenset({
    ("operator_rewrite", "dsp_srl_recurrence_cut_v1"),
    ("critical_net_reroute", "worst_source"),
})
EQUIVALENT_SOURCE_PROOF_VERSION = "equivalent-source-v1"
# Private execution parameters are sent only to skills that explicitly
# consume them.  Other skills may intentionally reject unknown parameters to
# protect their fixed typed-action contract.
_CANDIDATE_OUTPUT_CAP_SKILLS = frozenset({
    "critical_net_reroute",
    "lut_pin_swap",
})
LLM_PUBLIC_SELECTOR_SCHEMA = 2

# Resume may reuse a public read-only probe record without rebuilding the
# process-local private handoff that its typed mutation consumes.  Keep this
# dependency table complete: every operator target that resolves a private
# bundle must replay the exact-seed producer probe before mutation.
OPERATOR_PRIVATE_HANDOFF_PROBE_BY_TARGET = {
    "proved_arithmetic_compressor_v1": (
        "rapidwright", "recover_sequential_operator_contract"
    ),
    "constructive_accumulator_rebuild_v1": (
        "rapidwright", "recover_sequential_operator_contract"
    ),
    "consumer_driven_registered_arithmetic_rewrite_v1": (
        "rapidwright", "recover_consumer_driven_operator_contract"
    ),
    "fixed_point_dsp_rebuild_v1": (
        "vivado", "screen_fixed_point_dsp_contract"
    ),
    "registered_fir_data_cone_rewrite": (
        "vivado", "screen_fixed_point_dsp_contract"
    ),
    "bank_local_predicate_specialization_v1": (
        "vivado", "recover_banked_select_predicate_contract"
    ),
    "registered_ram_boolean_midpoint_relocation_v1": (
        "vivado", "recover_banked_ram_operator_contract"
    ),
    "registered_control_packet_localization_v1": (
        "vivado", "recover_banked_ram_operator_contract"
    ),
    "commutative_operand_bus_slot_reassociation_v1": (
        "vivado", "recover_commutative_reassociation_contract"
    ),
    "registered_dsp_lane_rebuild_v1": (
        "vivado", "recover_registered_dsp_lane_contract"
    ),
    "registered_dead_state_elimination_v1": (
        "rapidwright", "recover_registered_dead_state_contract"
    ),
    "exact_control_to_data_state_equation_v1": (
        "vivado", "recover_control_to_data_state_equation_contract"
    ),
    "ff_srl_boundary_bridge_v1": (
        "vivado", "recover_ff_srl_boundary_bridge_contract"
    ),
    "memory_write_buffer_forward_v1": (
        "vivado", "recover_memory_write_buffer_forward_contract"
    ),
    "multilevel_control_distribution_localization_v1": (
        "vivado", "recover_multilevel_control_distribution_contract"
    ),
    "registered_gf2_parity_reassociation_v1": (
        "vivado", "recover_registered_parity_reassociation_contract"
    ),
}

# These skills change placement or routing only.  Their parent DCP has the
# same logical object namespace, so a source-bound semantic checkpoint may be
# replayed from that exact parent after the live family is rediscovered.  Do
# not add phys_opt or any logical ECO here: Vivado may replicate or rewrite
# cells in those flows.
_ROUTE_ONLY_CHECKPOINT_SKILLS = frozenset({
    "critical_net_reroute",
    "structure_relocation",
})


def _logical_checkpoint_ancestors(memory: Memory, candidate_id: str) -> list[dict]:
    """Return exact route-only parents eligible for private checkpoint reuse."""
    ancestors: list[dict] = []
    node = memory.candidate_node(candidate_id)
    while node is not None and str(node.get("skill") or "") in (
        _ROUTE_ONLY_CHECKPOINT_SKILLS
    ):
        parent_id = str(node.get("parent_candidate_id") or "")
        if not parent_id:
            break
        parent = memory.candidate_node(parent_id)
        if parent is None:
            break
        if (
            parent.get("artifact_state") != "materialized"
            or not parent.get("artifact_path")
            or not parent.get("artifact_sha256")
        ):
            break
        ancestors.append({
            "candidate_id": parent_id,
            "artifact_path": str(parent["artifact_path"]),
            "artifact_sha256": str(parent["artifact_sha256"]),
            "relation": "route_only_logical_parent",
        })
        node = parent
    return ancestors

_SKILL_FAILURE_DIAGNOSTIC_FIELDS = frozenset({
    "report_available",
    "report_is_object",
    "report_read_error",
    "schema_version",
    "strict_mode",
    "vectors",
    "precheck_vectors",
    "phase1_passed",
    "phase2_passed",
    "phase2_skipped",
    "infrastructure_failure",
    "process_exit_code",
    "error",
})


def _bounded_skill_failure_diagnostic(result: object) -> dict:
    """Keep allowlisted validator facts after action scratch is collected."""
    if not isinstance(result, SkillResult) or not isinstance(result.details, dict):
        return {}
    raw = result.details.get("strict_validator_diagnostic")
    diagnostic = {}
    if isinstance(raw, dict):
        for key in _SKILL_FAILURE_DIAGNOSTIC_FIELDS:
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(value, (bool, int)) or value is None:
                diagnostic[key] = value
            elif isinstance(value, str):
                diagnostic[key] = value[:1000]

    operator = result.details.get("operator_rewrite_diagnostic")
    if isinstance(operator, dict):
        bounded = {}
        for key in (
            "version",
            "target",
            "family_ref",
            "target_width",
            "failure_scope",
            "remaining_family_exploration_allowed",
            "proof_total_budget_ms",
            "certificate_status",
            "mutation_attempted",
            "generating_operator_recovery_status",
            "generating_operator_rejection_reason",
        ):
            value = operator.get(key)
            if isinstance(value, (bool, int)) or value is None:
                bounded[key] = value
            elif isinstance(value, str):
                bounded[key] = value[:200]
        codes = operator.get("blocking_rejection_codes")
        if isinstance(codes, list):
            bounded["blocking_rejection_codes"] = [
                str(code)[:120] for code in codes[:8]
            ]
        attempts = operator.get("attempts")
        if isinstance(attempts, list):
            bounded["attempts"] = []
            for item in attempts[:4]:
                if not isinstance(item, dict):
                    continue
                bounded["attempts"].append({
                    str(key): value
                    for key, value in item.items()
                    if key in {
                        "status",
                        "family_ref",
                        "target_width",
                        "proof_total_budget_ms",
                        "proof_stage",
                        "proof_elapsed_s",
                        "mutation_attempted",
                    }
                    and (
                        isinstance(value, (bool, int, float))
                        or value is None
                        or isinstance(value, str)
                    )
                })
        diagnostic["operator_rewrite"] = bounded
    return diagnostic


def _interrupted_read_only_probe_decision(memory: Memory) -> Optional[Decision]:
    """Recover one persisted probe decision that never produced a result.

    Structured decisions are saved before execution.  If SSH or the runner is
    interrupted after that save, a read-only probe is safe to replay without a
    second priced Luna call.  Mutating actions are deliberately excluded.
    """
    cycles = list(memory.reasoning_cycles or [])
    if not cycles:
        return None
    cycle = cycles[-1]
    if int(cycle.get("round", -1)) != int(memory.round):
        return None
    action = cycle.get("terminal_action") or {}
    if action.get("action") != "probe" or not action.get("tool"):
        return None
    expected_target = f"{action.get('server')}:{action.get('tool')}"
    if any(
        item.get("target") == expected_target
        and int(item.get("round") or -1) > int(cycle.get("round", -1))
        for item in memory.history[-4:]
        if isinstance(item, dict)
    ):
        return None
    return Decision(
        action="probe",
        seed_candidate_id=action.get("seed_candidate_id"),
        server=str(action.get("server") or "vivado"),
        tool=str(action.get("tool") or ""),
        arguments=dict(action.get("arguments") or {}),
        question=str(action.get("question") or ""),
        expectation=str(action.get("expectation") or ""),
        evidence_refs=list(cycle.get("evidence_refs") or []),
        report_analysis=dict(cycle.get("report_analysis") or {}),
        knowledge_research=dict(cycle.get("knowledge_research") or {}),
        knowledge_comparison=str(action.get("knowledge_comparison") or ""),
        override_knowledge_direction=bool(
            action.get("override_knowledge_direction", False)
        ),
        failure_update=str(action.get("failure_update") or ""),
        contrastive_decision=dict(action.get("contrastive_decision") or {}),
        react_transcript=dict(cycle.get("transcript") or {}),
        report_snapshot_id=str(cycle.get("report_snapshot_id") or ""),
        knowledge_snapshot_id=str(cycle.get("knowledge_snapshot_id") or ""),
    )


def _confirmed_read_only_probe_retry_decision(
    memory: Memory, reasoning_round: int
) -> Decision:
    """Replay one exact Luna-selected probe after its backend was repaired.

    This is an explicit Trusted Runner recovery path.  It does not nominate a
    new action: the tool, family and arguments all come from the unique
    persisted reasoning cycle.  Only a read-only probe with a durable failed
    history record may be retried this way.
    """
    requested_round = int(reasoning_round)
    round_cycles = [
        cycle
        for cycle in (memory.reasoning_cycles or [])
        if isinstance(cycle, dict)
        and int(cycle.get("round", -1)) == requested_round
    ]
    replay_candidates = []
    for candidate_cycle in round_cycles:
        persisted = candidate_cycle.get("terminal_action") or {}
        if persisted.get("action") == "probe" and persisted.get("tool"):
            replay_candidates.append((candidate_cycle, persisted, False))
            continue
        transcript = candidate_cycle.get("transcript") or {}
        for item in transcript.get("decision") or []:
            if not isinstance(item, dict) or not item.get("terminal_rejected"):
                continue
            arguments = item.get("arguments")
            result = item.get("result") or {}
            normalized_arguments = (
                {
                    key: value
                    for key, value in arguments.items()
                    if key != "score_estimate"
                }
                if isinstance(arguments, dict)
                else arguments
            )
            schema_only_repair = bool(
                isinstance(normalized_arguments, dict)
                and set(normalized_arguments).issubset({
                    "server",
                    "tool",
                    "arguments",
                    "question",
                    "expectation",
                    "seed_candidate_id",
                })
                and normalized_arguments.get("server")
                in {"vivado", "rapidwright"}
                and isinstance(normalized_arguments.get("tool"), str)
                and bool(normalized_arguments.get("tool"))
                and isinstance(normalized_arguments.get("arguments"), dict)
                and isinstance(normalized_arguments.get("question"), str)
                and "unknown fields: ['score_estimate']"
                in str(result.get("detail") or "")
            )
            if (
                isinstance(normalized_arguments, dict)
                and str(normalized_arguments.get("tool") or "")
                and str(result.get("error") or "") == "terminal payload rejected"
                and (
                    schema_only_repair
                    or StageRunner._validate_terminal_payload(
                        "probe", normalized_arguments
                    ) is None
                )
            ):
                replay_candidates.append((
                    candidate_cycle,
                    {"action": "probe", **copy.deepcopy(normalized_arguments)},
                    True,
                ))
    eligible_candidates = []
    candidates_with_success = []
    for candidate_cycle, candidate_action, rejected_before_execution in replay_candidates:
        candidate_tool = str(candidate_action.get("tool") or "")
        candidate_server = str(candidate_action.get("server") or "vivado")
        expected_target = f"{candidate_server}:{candidate_tool}"
        legality_veto = (
            f"VETO: tool {candidate_tool!r} is not an allowed "
            f"{candidate_server} probe"
        )
        relevant_history = [
            item
            for item in (memory.history or [])
            if isinstance(item, dict)
            and int(item.get("round") or -1) >= requested_round
            and (
                item.get("target") == expected_target
                or (
                    item.get("action_executed") is False
                    and str(item.get("summary") or "") == legality_veto
                )
            )
        ]
        successes = [
            item
            for item in relevant_history
            if item.get("target") == expected_target
            and item.get("action_executed") is True
            and not str(item.get("summary") or "").startswith("FAILED:")
        ]
        failures = [
            item
            for item in relevant_history
            if item.get("action_executed") is False
            or str(item.get("summary") or "").startswith("FAILED:")
        ]
        if successes:
            candidates_with_success.append(candidate_tool)
        elif failures or rejected_before_execution:
            eligible_candidates.append((
                candidate_cycle,
                candidate_action,
                rejected_before_execution,
            ))
    if len(eligible_candidates) != 1:
        if len(replay_candidates) == 1 and candidates_with_success:
            raise ValueError(
                "read-only probe retry already has a later successful result"
            )
        raise ValueError(
            "read-only probe retry requires one unique persisted failed probe call"
        )
    cycle, action, rejected_before_execution = eligible_candidates[0]
    tool = str(action.get("tool") or "")
    if not tool:
        raise ValueError("read-only probe retry cycle has no tool")
    return Decision(
        action="probe",
        seed_candidate_id=action.get("seed_candidate_id"),
        server=str(action.get("server") or "vivado"),
        tool=tool,
        arguments=dict(action.get("arguments") or {}),
        question=str(action.get("question") or ""),
        expectation=str(action.get("expectation") or ""),
        evidence_refs=list(cycle.get("evidence_refs") or []),
        report_analysis=dict(cycle.get("report_analysis") or {}),
        knowledge_research=dict(cycle.get("knowledge_research") or {}),
        knowledge_comparison=str(action.get("knowledge_comparison") or ""),
        override_knowledge_direction=bool(
            action.get("override_knowledge_direction", False)
        ),
        failure_update=str(action.get("failure_update") or ""),
        contrastive_decision=dict(action.get("contrastive_decision") or {}),
        react_transcript=dict(cycle.get("transcript") or {}),
        report_snapshot_id=str(cycle.get("report_snapshot_id") or ""),
        knowledge_snapshot_id=str(cycle.get("knowledge_snapshot_id") or ""),
        raw={
            "trusted_read_only_probe_retry": {
                "reasoning_round": requested_round,
                "authority": "explicit_trusted_runner_backend_fix",
                "rejected_before_execution": rejected_before_execution,
            }
        },
    )


def _resumable_probe_followup(
    memory: Memory, current_seed_candidate_id: str
) -> Optional[dict]:
    """Recover a successful probe whose priced follow-up hit cost closure."""
    cycles = [
        cycle for cycle in (memory.reasoning_cycles or [])
        if isinstance(cycle, dict)
    ]
    if len(cycles) < 2:
        return None
    last = cycles[-1]
    if (
        (last.get("terminal_action") or {}).get("action") != "invalid"
        or "cost budget" not in str(last.get("error") or "").lower()
    ):
        return None
    current_seed = str(current_seed_candidate_id or "")
    # A Trusted Runner retry can execute a Luna-selected probe whose original
    # terminal call was rejected before execution.  That success is durable in
    # last_custom_responses even though there is no earlier terminal_action=
    # probe cycle.  Prefer an exact same-round/same-seed response so the next
    # Luna turn consumes the newest observation instead of an older probe.
    latest_response = next(
        (
            entry for entry in reversed(memory.last_custom_responses or [])
            if isinstance(entry, dict)
            and entry.get("status") == "success"
            and str(entry.get("seed_candidate_id") or "") == current_seed
            and int(entry.get("round") or -1) == int(last.get("round") or -2)
        ),
        None,
    )
    probe_cycle = next(
        (
            cycle for cycle in reversed(cycles[:-1])
            if (cycle.get("terminal_action") or {}).get("action") == "probe"
        ),
        None,
    )
    if probe_cycle is None and latest_response is None:
        return None
    action = (probe_cycle or {}).get("terminal_action") or {}
    server = str(
        (latest_response or {}).get("server") or action.get("server") or ""
    )
    tool = str((latest_response or {}).get("tool") or action.get("tool") or "")
    if not server or not tool:
        return None
    response = latest_response or next(
        (
            entry for entry in reversed(memory.last_custom_responses or [])
            if isinstance(entry, dict)
            and entry.get("status") == "success"
            and str(entry.get("server") or "") == server
            and str(entry.get("tool") or "") == tool
            and str(entry.get("seed_candidate_id") or "")
            == current_seed
        ),
        None,
    )
    if response is None and probe_cycle is not None:
        expected_target = f"{server}:{tool}"
        failed = next(
            (
                row for row in reversed(memory.history or [])
                if isinstance(row, dict)
                and str(row.get("target") or "") == expected_target
                and str(row.get("seed_candidate_id") or "") == current_seed
                and int(row.get("round") or -1) >= int(
                    probe_cycle.get("round") or -2
                )
                and (
                    row.get("action_executed") is False
                    or str(row.get("summary") or "").startswith("FAILED:")
                )
            ),
            None,
        )
        if failed is not None:
            return {
                "status": "error",
                "server": server,
                "tool": tool,
                "arguments": dict(action.get("arguments") or {}),
                "question": str(action.get("question") or ""),
                "seed_candidate_id": current_seed,
                "error": str(failed.get("summary") or "probe failed")[:2000],
                "resumed_after_cost_closure": True,
            }
    if response is None:
        return None
    if any(
        str(row.get("skill") or "") != "custom"
        and int(row.get("round") or -1) > int(response.get("round") or -1)
        for row in (memory.history or [])
        if isinstance(row, dict)
    ):
        return None
    return {
        "status": "success",
        "server": server,
        "tool": tool,
        "arguments": dict(response.get("arguments") or {}),
        "question": str(
            response.get("description") or action.get("question") or ""
        ),
        "seed_candidate_id": current_seed,
        "artifact_sha256": str(response.get("artifact_sha256") or ""),
        "probe_fingerprint": str(response.get("probe_fingerprint") or ""),
        "response": str(response.get("response") or "")[:12000],
        "response_truncated": bool(response.get("response_truncated", False)),
        "resumed_after_cost_closure": True,
    }


def _confirmed_pre_mutation_retry_decision(
    memory: Memory, review_id: str
) -> Decision:
    """Replay one Luna-confirmed action after a pre-mutation backend bug fix.

    This is an explicit Trusted Runner recovery path, not an autonomous policy:
    it accepts only a previously confirmed action whose archived execution
    produced no Candidate and whose backend diagnostic proves mutation never
    started.  The exact immutable seed must still be selectable in the active
    beam; it need not remain global best after unrelated sibling experiments.
    """
    requested = str(review_id or "").strip()
    if not requested:
        raise ValueError("pre-mutation retry requires an exact review id")
    if memory.pending_transaction:
        raise RuntimeError("pre-mutation retry cannot replace a pending transaction")
    derived_review_ids = {
        str(item.get("review_id") or "")
        for item in (memory.decision_reviews or [])
        if isinstance(item, dict)
        and item.get("retry_of_review_id") == requested
    }
    if any(
        item.get("review_id") in derived_review_ids
        and (item.get("execution") or {}).get("action_executed") is True
        for item in (memory.action_transactions or [])
        if isinstance(item, dict)
    ):
        raise RuntimeError("pre-mutation retry review was already executed")
    reviews = [
        item for item in (memory.decision_reviews or [])
        if isinstance(item, dict) and item.get("review_id") == requested
    ]
    transactions = [
        item for item in (memory.action_transactions or [])
        if isinstance(item, dict) and item.get("review_id") == requested
    ]
    if len(reviews) != 1 or len(transactions) != 1:
        raise ValueError("pre-mutation retry review provenance is absent or ambiguous")
    review = reviews[0]
    transaction = transactions[0]
    action = review.get("provisional_action") or {}
    final_review = review.get("final_review") or {}
    diagnostic = (transaction.get("failure_diagnostic") or {}).get(
        "operator_rewrite"
    ) or {}
    transaction_params = {
        str(key): value
        for key, value in dict(transaction.get("params") or {}).items()
        if str(key) != "input_dcp" and not str(key).startswith("_")
    }
    reviewed_params = dict(action.get("params") or {})
    ordinary_pre_mutation_failure = bool(
        transaction.get("terminal_status") == "no_candidates"
        and (transaction.get("execution") or {}).get("action_executed") is True
        and diagnostic.get("mutation_attempted") is False
    )
    recovered_pre_invocation_interruption = bool(
        transaction.get("terminal_status") == "interrupted"
        and str(transaction.get("terminal_reason") or "")
        == "interrupted transaction recovered from executing phase"
        and (transaction.get("outcome_proof") or {}).get("recovered") is True
        and (transaction.get("execution") or {}).get("action_executed") is not True
        and not list(transaction.get("candidates") or [])
    )
    if (
        action.get("action") != "run_skill"
        or final_review.get("action") != "confirm"
        or action.get("skill") != "operator_rewrite"
        or not (
            ordinary_pre_mutation_failure
            or recovered_pre_invocation_interruption
        )
        or transaction.get("skill") != action.get("skill")
        or transaction.get("target") != action.get("target")
        or transaction_params != reviewed_params
    ):
        raise ValueError(
            "pre-mutation retry requires one confirmed, zero-candidate operator "
            "action that provably never started mutation"
        )
    seed_id = str(action.get("seed_candidate_id") or "")
    graph = memory.candidate_graph or {}
    seed = (graph.get("nodes") or {}).get(seed_id) or {}
    if (
        not seed_id
        or seed.get("artifact_state") != "materialized"
        or seed.get("eligible_for_beam") is not True
        or seed.get("in_active_beam") is not True
        or seed_id not in (graph.get("beam_candidate_ids") or [])
        or transaction.get("seed_candidate_id") != seed_id
    ):
        raise ValueError(
            "pre-mutation retry seed is no longer materialized and selectable"
        )
    matching_cycles = [
        cycle for cycle in (memory.reasoning_cycles or [])
        if isinstance(cycle, dict)
        and int(cycle.get("round", -1)) == int(review.get("round", -2))
    ]
    cycle = matching_cycles[-1] if matching_cycles else {}
    return Decision(
        action="run_skill",
        skill=str(action.get("skill") or ""),
        target=str(action.get("target") or "auto"),
        params=dict(action.get("params") or {}),
        seed_candidate_id=seed_id,
        expectation=str(final_review.get("assessment") or ""),
        evidence_refs=list(cycle.get("evidence_refs") or []),
        report_analysis=dict(cycle.get("report_analysis") or {}),
        knowledge_research=dict(cycle.get("knowledge_research") or {}),
        report_snapshot_id=str(cycle.get("report_snapshot_id") or ""),
        knowledge_snapshot_id=str(cycle.get("knowledge_snapshot_id") or ""),
        failure_update=(
            "Backend implementation was repaired before mutation; measure the "
            "same previously confirmed typed action on the unchanged seed."
        ),
        raw={
            "trusted_pre_mutation_retry": {
                "review_id": requested,
                "authority": "explicit_trusted_runner_backend_fix",
            }
        },
    )


def _confirmed_flow_veto_retry_decision(
    memory: Memory, reasoning_round: int
) -> Decision:
    """Replay an exact Luna action rejected by one recognized flow defect.

    This path does not bypass Action Proof or its neutral review. It only
    reconstructs the durable typed proposal after proving that the matching
    backend action never executed and emitted no Candidate.
    """
    requested_round = int(reasoning_round)
    if memory.pending_transaction:
        raise RuntimeError("flow-veto retry cannot replace a pending transaction")
    cycles = [
        cycle for cycle in (memory.reasoning_cycles or [])
        if isinstance(cycle, dict)
        and int(cycle.get("round", -1)) == requested_round
        and (cycle.get("terminal_action") or {}).get("action") == "run_skill"
    ]
    recovered_score_schema_veto = False
    if len(cycles) == 1:
        cycle = cycles[0]
        action = cycle.get("terminal_action") or {}
    elif not cycles:
        # Older staged runs could parse one complete run_skill tool call, then
        # replace it with terminal_action=invalid solely because the advisory
        # score envelope contained misplaced audit fields.  Recover only that
        # exact durable call; it still passes normal Action Proof and neutral
        # reconsideration after resume.
        invalid_cycles = [
            item for item in (memory.reasoning_cycles or [])
            if isinstance(item, dict)
            and int(item.get("round", -1)) == requested_round
            and (item.get("terminal_action") or {}).get("action") == "invalid"
            and str(item.get("error") or "") == "invalid score_estimate"
        ]
        terminal_calls = []
        for item in invalid_cycles:
            for turn in ((item.get("transcript") or {}).get("decision") or []):
                if (
                    isinstance(turn, dict)
                    and turn.get("terminal") is True
                    and turn.get("tool") == "run_skill"
                    and isinstance(turn.get("arguments"), dict)
                ):
                    terminal_calls.append((item, dict(turn["arguments"])))
        if len(terminal_calls) != 1:
            raise ValueError(
                "flow-veto retry action provenance is absent or ambiguous"
            )
        cycle, action = terminal_calls[0]
        action["action"] = "run_skill"
        recovered_score_schema_veto = True
    else:
        raise ValueError("flow-veto retry action provenance is absent or ambiguous")
    skill = str(action.get("skill") or "")
    target = str(action.get("target") or "auto")
    recognized_reason = (
        "invalid score_estimate"
        if recovered_score_schema_veto
        else {
            ("fanout_opt", "auto"): (
                "fanout_opt requires one concrete net from the live supported "
                "fanout inventory"
            ),
        }.get((skill, target))
    )
    if not recognized_reason:
        raise ValueError("flow-veto retry is not a recognized repaired defect")
    if not recovered_score_schema_veto:
        failures = [
            row for row in (memory.history or [])
            if isinstance(row, dict)
            and int(row.get("round", -1)) == requested_round
            and str(row.get("skill") or "") == skill
            and str(row.get("target") or "") == target
            and row.get("action_executed") is False
            and str(row.get("veto_reason") or "") == recognized_reason
        ]
        if len(failures) != 1:
            raise ValueError("flow-veto retry has no unique recognized durable veto")
    if any(
        isinstance(row, dict)
        and int(row.get("round", -1)) > requested_round
        and str(row.get("skill") or "") != "custom"
        and row.get("action_executed") is True
        for row in (memory.history or [])
    ):
        raise ValueError("flow-veto retry was superseded by a later mutation")
    seed_id = str(action.get("seed_candidate_id") or "")
    seed, reason = memory.validate_selectable_seed(seed_id)
    if seed is None:
        raise ValueError(f"flow-veto retry seed is not selectable: {reason}")
    return Decision(
        action="run_skill",
        skill=skill,
        target=target,
        params=dict(action.get("params") or {}),
        seed_candidate_id=seed_id,
        expectation=str(action.get("expectation") or ""),
        # A schema-veto retry deliberately drops only the malformed advisory
        # estimate.  Exact skill/target/seed/params remain unchanged.
        score_estimate=(
            {} if recovered_score_schema_veto
            else dict(action.get("score_estimate") or {})
        ),
        evidence_refs=list(cycle.get("evidence_refs") or []),
        report_analysis=dict(cycle.get("report_analysis") or {}),
        knowledge_research=dict(cycle.get("knowledge_research") or {}),
        report_snapshot_id=str(cycle.get("report_snapshot_id") or ""),
        knowledge_snapshot_id=str(cycle.get("knowledge_snapshot_id") or ""),
        failure_update=str(action.get("failure_update") or ""),
        raw={
            "trusted_flow_veto_retry": {
                "reasoning_round": requested_round,
                "recognized_veto": recognized_reason,
                "recovered_from_terminal_tool_call": recovered_score_schema_veto,
                "authority": "explicit_trusted_runner_flow_fix",
            }
        },
    )


def _structured_decision_failure_kind(decision: Optional[Decision]) -> str:
    """Recover a transport failure kind without inventing a retry threshold.

    Fresh staged decisions carry ``raw.failure_kind``.  A durable same-run
    follow-up can instead retain only the priced terminal-closure policy plus
    its error text.  That policy proves no further provider request can be
    funded, so retrying it locally cannot produce a different Luna answer.
    """
    if decision is None:
        return ""
    raw = getattr(decision, "raw", {}) or {}
    explicit = str(raw.get("failure_kind") or "")
    if explicit:
        return explicit
    trace = getattr(decision, "react_transcript", {}) or {}
    policy = trace.get("decision_budget_policy") or {}
    error = str(raw.get("error") or "").lower()
    if (
        str(policy.get("mode") or "") == "terminal_closure"
        and ("cost budget" in error or "cannot fund" in error)
    ):
        return "cost_budget"
    return ""


def _confirmed_zero_candidate_retry_decision(
    memory: Memory, transaction_id: str
) -> Decision:
    """Replay one deterministic action after a zero-candidate backend fix.

    Unlike ``_confirmed_pre_mutation_retry_decision``, this recovery path is
    allowed after the mutating MCP was invoked: it proves the narrower fact
    that the archived transaction emitted no Candidate, then binds the retry
    to the same materialized seed, canonical params and physical fingerprint.
    When the transaction has an LLM review, the archived review must have
    confirmed exactly that same action; an unreviewed deterministic action
    remains eligible under the original contract.
    """
    requested = str(transaction_id or "").strip()
    if not requested:
        raise ValueError("zero-candidate retry requires an exact transaction id")
    if memory.pending_transaction:
        raise RuntimeError("zero-candidate retry cannot replace a pending transaction")
    derived_review_ids = {
        str(item.get("review_id") or "")
        for item in (getattr(memory, "decision_reviews", None) or [])
        if isinstance(item, dict)
        and item.get("retry_of_transaction_id") == requested
    }
    if any(
        isinstance(item, dict)
        and item.get("review_id") in derived_review_ids
        and (item.get("execution") or {}).get("action_executed") is True
        for item in (memory.action_transactions or [])
    ):
        raise RuntimeError("zero-candidate transaction was already retried")
    transactions = [
        item for item in (memory.action_transactions or [])
        if isinstance(item, dict) and item.get("transaction_id") == requested
    ]
    if len(transactions) != 1:
        raise ValueError("zero-candidate retry transaction is absent or ambiguous")
    transaction = transactions[0]
    outcome = transaction.get("outcome_proof") or {}
    cycle_result = outcome.get("cycle_result") or {}
    candidate_ids = list(transaction.get("candidate_ids") or [])
    emitted_candidates = list(transaction.get("candidates") or [])
    skill_name = str(transaction.get("skill") or "")
    delivery_cap_retry = bool(
        skill_name == "critical_net_reroute"
        and re.fullmatch(
            r"skill output contract violation: skill returned [1-9][0-9]* "
            r"candidates; configured cap is [1-9][0-9]*",
            str(transaction.get("terminal_reason") or ""),
        )
    )
    fixed_parameter_cap_retry = bool(
        skill_name not in _CANDIDATE_OUTPUT_CAP_SKILLS
        and re.fullmatch(
            r"[a-z0-9_]+ (?:preset )?parameters are fixed; "
            r"rejected \['_candidate_output_cap'\]",
            str(transaction.get("terminal_reason") or ""),
        )
    )
    force_replicate_auto_retry = bool(
        skill_name == "force_replicate"
        and str(transaction.get("target") or "") == "auto"
        and str(transaction.get("terminal_reason") or "")
        == "[VIV] run_tcl: ERROR: net not found: auto"
    )
    fanout_hierarchy_delivery_retry = bool(
        skill_name == "fanout_opt"
        and str(transaction.get("target") or "") == "auto"
        and (
            (
                "Couldn't find cell instance"
                in str(transaction.get("terminal_reason") or "")
                and "when attempting to create logical net"
                in str(transaction.get("terminal_reason") or "")
            )
            or "hierarchy_delivery_unavailable"
            in str(transaction.get("terminal_reason") or "")
        )
    )
    fanout_checkpoint_delivery_retry = bool(
        skill_name == "fanout_opt"
        and str(transaction.get("target") or "") == "auto"
        and str(transaction.get("terminal_reason") or "")
        == (
            "[VIV] write_checkpoint: ERROR: [Common 17-49] Internal Data "
            "Exception: HDDMProto::writeMessage failed"
        )
    )
    fanout_zip_checkpoint_delivery_retry = bool(
        skill_name == "fanout_opt"
        and str(transaction.get("target") or "") == "auto"
        and re.fullmatch(
            r"\[VIV\] write_checkpoint: ERROR: \[Common 17-141\] Failed to "
            r"write file content of [^/\\]+\.rdb in zip archive\.",
            str(transaction.get("terminal_reason") or ""),
        )
    )
    fanout_interrupted_delivery_retry = bool(
        skill_name == "fanout_opt"
        and str(transaction.get("target") or "") == "auto"
        and transaction.get("terminal_status") == "interrupted"
        and str(transaction.get("terminal_reason") or "")
        == "interrupted transaction recovered from executing phase"
    )
    critical_cluster_anchor_tcl_delivery_retry = bool(
        skill_name == "critical_cluster_anchor"
        and str(transaction.get("target") or "") == "auto"
        and str(transaction.get("terminal_reason") or "")
        in {
            '[VIV] run_tcl: invalid command name "foreach_in_collection"',
            '[VIV] run_tcl: wrong # args: should be "foreach varList list '
            '?varList list ...? command"',
            '[VIV] run_tcl: ERROR: [Vivado_Tcl 4-2610] place_design -eco '
            'is not supported in non-versal devices.',
        }
    )
    review_id = str(transaction.get("review_id") or "").strip()
    retry_reviews = [
        item for item in (getattr(memory, "decision_reviews", None) or [])
        if isinstance(item, dict) and item.get("review_id") == review_id
    ]
    retry_of_transaction_id = str(
        (retry_reviews[0] if len(retry_reviews) == 1 else {}).get(
            "retry_of_transaction_id"
        ) or ""
    )
    prior_delivery_failures = [
        item for item in (memory.action_transactions or [])
        if isinstance(item, dict)
        and item.get("transaction_id") == retry_of_transaction_id
    ]
    prior_delivery_failure = (
        prior_delivery_failures[0] if len(prior_delivery_failures) == 1 else {}
    )
    false_success_outcomes = list(outcome.get("outcomes") or [])
    force_replicate_tcl_false_success_retry = bool(
        skill_name == "force_replicate"
        and str(transaction.get("target") or "") == "auto"
        and transaction.get("terminal_status") == "committed"
        and (transaction.get("execution") or {}).get("action_executed") is True
        and len(candidate_ids) == 1
        and int(cycle_result.get("candidate_count", -1)) == 1
        and int(cycle_result.get("legal_candidate_count", -1)) == 1
        and len(false_success_outcomes) == 1
        and false_success_outcomes[0].get("legal") is True
        and false_success_outcomes[0].get("promoted") is False
        and float(false_success_outcomes[0].get("delta_fmax_mhz", float("nan")))
        == 0.0
        and prior_delivery_failure.get("skill") == "force_replicate"
        and prior_delivery_failure.get("target") == "auto"
        and prior_delivery_failure.get("seed_candidate_id")
        == transaction.get("seed_candidate_id")
        and prior_delivery_failure.get("action_fingerprint")
        == transaction.get("action_fingerprint")
        and prior_delivery_failure.get("terminal_reason")
        == "[VIV] run_tcl: ERROR: net not found: auto"
    )
    ordinary_zero_candidate_retry = bool(
        transaction.get("terminal_status") in {"no_candidates", "interrupted"}
        and (
            (transaction.get("execution") or {}).get("action_executed") is True
            or fanout_interrupted_delivery_retry
        )
        and not emitted_candidates
        and int(cycle_result.get("candidate_count", -1)) == 0
        and int(cycle_result.get("legal_candidate_count", -1)) == 0
        and (
            skill_name == "operator_rewrite"
            or delivery_cap_retry
            or fixed_parameter_cap_retry
            or force_replicate_auto_retry
            or fanout_hierarchy_delivery_retry
            or fanout_checkpoint_delivery_retry
            or fanout_zip_checkpoint_delivery_retry
            or fanout_interrupted_delivery_retry
            or critical_cluster_anchor_tcl_delivery_retry
        )
    )
    if not (
        ordinary_zero_candidate_retry
        or force_replicate_tcl_false_success_retry
    ):
        raise ValueError(
            "zero-candidate retry requires one deterministic operator action "
            "or one explicitly recognized reviewed physical delivery defect"
        )
    if (
        delivery_cap_retry
        or fixed_parameter_cap_retry
        or force_replicate_auto_retry
        or fanout_hierarchy_delivery_retry
        or fanout_checkpoint_delivery_retry
        or fanout_zip_checkpoint_delivery_retry
        or fanout_interrupted_delivery_retry
        or critical_cluster_anchor_tcl_delivery_retry
        or force_replicate_tcl_false_success_retry
    ) and not review_id:
        raise ValueError(
            "physical delivery retry requires a confirmed review"
        )
    if review_id:
        reviews = [
            item for item in (getattr(memory, "decision_reviews", None) or [])
            if isinstance(item, dict) and item.get("review_id") == review_id
        ]
        if len(reviews) != 1:
            raise ValueError(
                "reviewed zero-candidate retry provenance is absent or ambiguous"
            )
        review = reviews[0]
        action = review.get("provisional_action") or {}
        final_review = review.get("final_review") or {}
        transaction_params = {
            str(key): value
            for key, value in dict(transaction.get("params") or {}).items()
            if str(key) != "input_dcp" and not str(key).startswith("_")
        }
        review_proof_sha = str(
            (review.get("action_proof") or {}).get("proof_sha256") or ""
        )
        # The transaction executes the canonical Action Proof, not the raw
        # provisional Luna payload.  Canonicalization may add a fixed default
        # such as max_candidates=1, so every reviewed retry must compare the
        # archived transaction with the confirmed proof identity.  Falling
        # back to provisional params makes a valid backend-only retry
        # impossible after any ordinary framework repair.
        canonical_reviewed_params = (
            ((review.get("action_proof") or {}).get("action_identity") or {})
            .get("canonical_public_params")
        )
        reviewed_params = (
            dict(canonical_reviewed_params)
            if isinstance(canonical_reviewed_params, dict)
            else dict(action.get("params") or {})
        )
        transaction_proof_sha = str(
            transaction.get("action_proof_sha256") or ""
        )
        if (
            final_review.get("action") != "confirm"
            or action.get("action") != "run_skill"
            or action.get("skill") != transaction.get("skill")
            or action.get("target") != transaction.get("target")
            or action.get("seed_candidate_id")
            != transaction.get("seed_candidate_id")
            or reviewed_params != transaction_params
            or not review_proof_sha
            or review_proof_sha != transaction_proof_sha
        ):
            raise ValueError(
                "reviewed zero-candidate retry does not match its confirmed action"
            )
    # A reserved graph ID is expected even when the backend emitted nothing;
    # it must not have become a graph node or a materialized artifact.
    graph = memory.candidate_graph or {}
    nodes = graph.get("nodes") or {}
    if (
        not force_replicate_tcl_false_success_retry
        and any(candidate_id in nodes for candidate_id in candidate_ids)
    ):
        raise ValueError("zero-candidate retry found a published Candidate")
    if force_replicate_tcl_false_success_retry and any(
        candidate_id not in nodes
        or (nodes[candidate_id] or {}).get("artifact_state") != "deleted"
        or (nodes[candidate_id] or {}).get("search_state") != "pruned"
        or (nodes[candidate_id] or {}).get("in_active_beam") is not False
        or candidate_id in (graph.get("beam_candidate_ids") or [])
        for candidate_id in candidate_ids
    ):
        raise ValueError(
            "false-success retry candidate is not durably pruned and deleted"
        )
    seed_id = str(transaction.get("seed_candidate_id") or "")
    seed = nodes.get(seed_id) or {}
    if (
        not seed_id
        or seed.get("artifact_state") != "materialized"
        or seed.get("eligible_for_beam") is not True
        or seed.get("in_active_beam") is not True
        or seed_id not in (graph.get("beam_candidate_ids") or [])
    ):
        raise ValueError(
            "zero-candidate retry seed is no longer materialized and selectable"
        )
    fingerprint = str(transaction.get("action_fingerprint") or "")
    if not fingerprint:
        raise ValueError("zero-candidate retry has no action fingerprint")
    params = {
        str(key): value
        for key, value in dict(transaction.get("params") or {}).items()
        if str(key) != "input_dcp" and not str(key).startswith("_")
    }
    return Decision(
        action="run_skill",
        skill=str(transaction.get("skill") or ""),
        target=str(transaction.get("target") or "auto"),
        params=params,
        seed_candidate_id=seed_id,
        expectation=(
            "Backend implementation was repaired after a zero-candidate "
            "failure; replay the identical typed action on the unchanged seed."
        ),
        failure_update=(
            "Record the new backend result without treating the repaired "
            "infrastructure failure as QoR evidence."
        ),
        raw={
            "trusted_zero_candidate_retry": {
                "transaction_id": requested,
                "action_fingerprint": fingerprint,
                "seed_candidate_id": seed_id,
                "allow_repaired_private_selector": (
                    fanout_hierarchy_delivery_retry
                    or fanout_interrupted_delivery_retry
                ),
                "authority": "explicit_trusted_runner_backend_fix",
            }
        },
    )


def _archive_probe_superseded_by_explicit_retry(
    memory: Memory,
    probe: Optional[Decision],
    *,
    retry_kind: str,
    retry_id: str,
    recorded_at: float,
) -> None:
    """Retain, but do not execute, a post-failure interrupted probe.

    An explicit Trusted Runner retry repairs an earlier delivery failure.  A
    later probe selected after that false failure is not evidence against the
    repaired action and must not block exact replay.  Preserve its identity in
    the private run manifest so the audit remains complete.
    """
    if probe is None:
        return
    memory.run_manifest.setdefault(
        "interrupted_probes_superseded_by_explicit_retry", []
    ).append({
        "server": probe.server,
        "tool": probe.tool,
        "seed_candidate_id": probe.seed_candidate_id,
        "retry_kind": str(retry_kind),
        "retry_id": str(retry_id),
        "recorded_at": float(recorded_at),
        "reason": "selected after repaired delivery failure",
    })




def validate_candidate_eco_proof(
    *,
    skill: str,
    proof: Optional[dict],
    source_artifact_sha256: str,
    candidate_artifact_sha256: str,
    target: Optional[str] = None,
) -> dict:
    """Candidate-level high-risk gate; this never grants final equivalence."""
    if skill not in HIGH_RISK_ECO_SKILLS:
        return copy.deepcopy(proof) if isinstance(proof, dict) else {}
    if not isinstance(proof, dict):
        raise SkillContractError(f"high-risk skill {skill} output is missing proof")
    if skill == "operator_rewrite":
        proof_version = proof.get("proof_version")
        current_required = {
            "proof_version", "skill", "template", "source_artifact_sha256",
            "artifact_sha256", "rewrite_authorization",
            "post_emission_validation",
        }
        teammate_required = {
            "proof_version", "skill", "template", "source_artifact_sha256",
            "artifact_sha256", "rewrite_authorization",
            "measurement_admission",
        }
        expected_fields = (
            current_required
            if proof_version == CURRENT_OPERATOR_PROOF_VERSION
            else teammate_required
            if proof_version == TEAMMATE_OPERATOR_PROOF_VERSION
            else None
        )
        if expected_fields is None:
            raise SkillContractError("operator rewrite proof version mismatch")
        if set(proof) != expected_fields:
            raise SkillContractError(
                "operator rewrite proof has missing or unsupported fields"
            )
        if proof.get("skill") != skill:
            raise SkillContractError("operator rewrite proof skill mismatch")
        template = proof.get("template")
        allowed_templates = {target}
        if target == "registered_arithmetic_cone_v1":
            from .skills.operator_rewrite import OPERATOR_REWRITE_REGISTRY

            spec = OPERATOR_REWRITE_REGISTRY[target]
            # Candidate proofs use the already-validated concrete backend
            # template while the canonical Action Proof retains the public
            # registered-arithmetic target and nominated family binding.
            allowed_templates = {
                "fixed_point_dsp_rebuild_v1"
                if item in {
                    "DSP48E2_MUL_SHIFT_ACCUM_V1",
                    "DSP48E2_WIDE_CONST_MUL_V1",
                }
                else target
                for item in spec.proof_templates
            }
        if not isinstance(target, str) or template not in allowed_templates:
            raise SkillContractError("operator rewrite proof target mismatch")
        if proof.get("source_artifact_sha256") != source_artifact_sha256:
            raise SkillContractError("operator rewrite proof source identity mismatch")
        if proof.get("artifact_sha256") != candidate_artifact_sha256:
            raise SkillContractError("operator rewrite proof artifact identity mismatch")
        verifier = (
            verify_operator_post_emission_certificate
            if proof_version == CURRENT_OPERATOR_PROOF_VERSION
            else verify_operator_measurement_admission_certificate
        )
        certificate = (
            proof.get("post_emission_validation")
            if proof_version == CURRENT_OPERATOR_PROOF_VERSION
            else proof.get("measurement_admission")
        )
        if not verifier(
            certificate,
            proof.get("rewrite_authorization"),
            expected_template=template,
            expected_source_sha256=source_artifact_sha256,
            expected_candidate_sha256=candidate_artifact_sha256,
        ):
            raise SkillContractError("operator rewrite post-emission proof did not replay")
        return copy.deepcopy(proof)
    if skill == "semantic_replay":
        if proof.get("proof_version") != SEMANTIC_REPLAY_PROOF_VERSION:
            raise SkillContractError("semantic replay proof version mismatch")
        if not verify_semantic_replay_proof(
            proof,
            source_sha256=source_artifact_sha256,
            candidate_sha256=candidate_artifact_sha256,
        ):
            raise SkillContractError("semantic replay proof did not replay")
        return copy.deepcopy(proof)
    required = {
        "proof_version", "skill", "source_artifact_sha256", "artifact_sha256",
        "exact_source_equivalence",
    }
    if set(proof) != required:
        raise SkillContractError("high-risk proof has missing or unsupported fields")
    if proof.get("proof_version") != EQUIVALENT_SOURCE_PROOF_VERSION:
        raise SkillContractError("high-risk proof version mismatch")
    if proof.get("skill") != skill:
        raise SkillContractError("high-risk proof skill mismatch")
    if proof.get("source_artifact_sha256") != source_artifact_sha256:
        raise SkillContractError("high-risk proof source identity mismatch")
    if proof.get("artifact_sha256") != candidate_artifact_sha256:
        raise SkillContractError("high-risk proof artifact identity mismatch")
    certificate = proof.get("exact_source_equivalence")
    if not isinstance(certificate, dict) or certificate.get("status") != "proved":
        raise SkillContractError("exact_source_equivalence status is not proved")
    if certificate.get("proof_version") != EQUIVALENT_SOURCE_PROOF_VERSION:
        raise SkillContractError("equivalent-source certificate version mismatch")
    if certificate.get("proof_type") != "exact_source_equivalence":
        raise SkillContractError("equivalent-source certificate type mismatch")
    if certificate.get("design_sha256") != source_artifact_sha256:
        raise SkillContractError("certificate design identity mismatch")
    return copy.deepcopy(proof)


def validate_action_candidate_eco_proof(
    memory: Memory,
    *,
    run_dir: Path,
    seed_candidate_id: str,
    skill: str,
    proof: Optional[dict],
    evidence: Optional[dict],
    candidate_artifact_sha256: str,
    target: Optional[str] = None,
) -> dict:
    """Validate direct ECO proof or renewed proof rooted at a graph ancestor."""
    seed = memory.candidate_node(seed_candidate_id)
    if seed is None:
        raise SkillContractError(f"candidate proof has unknown seed {seed_candidate_id}")
    seed_has_lineage = validate_full_recurrence_lineage(memory, run_dir, seed)
    lineage_proof = validate_recurrence_lineage_candidate(
        memory,
        run_dir=run_dir,
        seed_candidate_id=seed_candidate_id,
        evidence=evidence,
        proof=proof,
        candidate_sha256=candidate_artifact_sha256,
    )
    if lineage_proof is not None:
        if (skill, target) not in {
            ("operator_rewrite", "dsp_srl_recurrence_cut_v1"),
            ("critical_net_reroute", "worst_source"),
        }:
            raise SkillContractError(
                "recurrence lineage proof is not valid for this action"
            )
        if skill == "operator_rewrite":
            descriptor = (evidence or {}).get("recurrence_family_lineage") or {}
            if (
                descriptor.get("source_candidate_id") != seed_candidate_id
                or descriptor.get("source_artifact_sha256")
                != seed.get("artifact_sha256")
            ):
                raise SkillContractError(
                    "recurrence rewrite lineage is not bound to its immediate seed"
                )
        return lineage_proof
    if seed_has_lineage:
        raise SkillContractError(
            "recurrence lineage seed produced a candidate without renewed proof"
        )
    if (skill, target) in {
        ("critical_net_reroute", "boundary_link"),
        ("endpoint_bel_move", "auto"),
        ("hard_macro_move", "auto"),
        ("semantic_replay", "auto"),
    }:
        if not isinstance(evidence, dict):
            raise SkillContractError(
                f"{skill}/{target} candidate is missing identity-bound evidence"
            )
        if evidence.get("source_dcp_sha256") != seed.get("artifact_sha256"):
            raise SkillContractError(
                f"{skill}/{target} source evidence does not match its graph parent"
            )
        if evidence.get("candidate_dcp_sha256") != candidate_artifact_sha256:
            raise SkillContractError(
                f"{skill}/{target} candidate evidence does not match artifact bytes"
            )
    return validate_candidate_eco_proof(
        skill=skill,
        target=target,
        proof=proof,
        source_artifact_sha256=str(seed.get("artifact_sha256", "")),
        candidate_artifact_sha256=candidate_artifact_sha256,
    )


# ---------------------------------------------------------------------------
# Initial analysis
# ---------------------------------------------------------------------------

async def collect_current_design(
    mcp: MCPClient,
    cfg: Config,
    *,
    require_timing_profile: bool = False,
    central_measurement: Optional[dict] = None,
    capture_reports: bool = False,
    allow_partial: bool = False,
    operator_mining_candidate: Optional[dict] = None,
    recurrence_lineage_active: bool = False,
    retry_failed_action_menu_families: bool = False,
    cold_isolation: bool = False,
    run_id: str = "",
    telemetry_epoch: int = 0,
    same_run_feedback_by_skill: Optional[dict] = None,
) -> dict:
    """Collect read-only telemetry from the currently open Vivado design."""
    report_documents: dict[str, str] = {}
    availability = {
        "timing": False,
        "fanout": False,
        "timing_profile": False,
        "utilization": False,
    }
    if capture_reports and central_measurement is not None:
        report_documents["central_measurement.json"] = json.dumps(
            central_measurement, indent=2, sort_keys=True, default=str
        )
    clock_period = (
        central_measurement.get("clock_period_ns")
        if central_measurement is not None
        else await mcp.get_clock_period()
    )
    if require_timing_profile and clock_period is None:
        raise RuntimeError("clock period telemetry unavailable")

    if central_measurement is None:
        ts_report = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
        if capture_reports:
            report_documents["timing_summary.rpt"] = ts_report
        ts = parse_timing_summary_static(ts_report)
        live_wns = await mcp.get_wns()
        measured_wns = live_wns if live_wns is not None else ts.get("wns")
    else:
        ts = {
            "wns": central_measurement.get("wns_ns"),
            "tns": central_measurement.get("tns_ns"),
            "whs": central_measurement.get("whs_ns"),
            "wpws": central_measurement.get("wpws_ns"),
            "failing_endpoints": central_measurement.get("failing_endpoints"),
        }
        measured_wns = central_measurement.get("wns_ns")
        if capture_reports:
            try:
                ts_report = await mcp.call_vivado(
                    "report_timing_summary", {}, timeout=300.0
                )
                report_documents["timing_summary.rpt"] = ts_report
            except Exception as exc:  # noqa: BLE001
                report_documents["errors/timing_summary.txt"] = str(exc)
    if measured_wns is None:
        raise RuntimeError("timing telemetry did not contain WNS")
    baseline_wns = float(measured_wns)
    availability["timing"] = clock_period is not None

    try:
        fanout_report = await mcp.call_vivado(
            "get_critical_high_fanout_nets",
            {
                "num_paths": int(cfg.get("analysis.fanout_num_paths", 50)),
                "min_fanout": int(cfg.get("analysis.fanout_min_fanout", 20)),
                "exclude_clocks": True,
            },
            timeout=600.0,
        )
        fanout_nets = parse_high_fanout_nets(fanout_report)
        fanout_capabilities = parse_high_fanout_capabilities(fanout_report)
        fanout_timing_evidence = parse_high_fanout_timing_evidence(fanout_report)
        availability["fanout"] = True
        if capture_reports:
            report_documents["critical_high_fanout_nets.rpt"] = fanout_report
    except Exception as exc:  # noqa: BLE001
        if not capture_reports and not allow_partial:
            raise
        fanout_nets = []
        fanout_capabilities = {}
        fanout_timing_evidence = {}
        report_documents["errors/critical_high_fanout_nets.txt"] = str(exc)

    if capture_reports:
        try:
            route_report = await mcp.call_vivado(
                "report_route_status", {}, timeout=120.0
            )
            report_documents["route_status.rpt"] = route_report
        except Exception as exc:  # noqa: BLE001
            report_documents["errors/route_status.txt"] = str(exc)

    timing_profile: dict = {}
    try:
        profile_raw = await mcp.call_vivado(
            "report_timing_path_profile",
            {"num_paths": int(cfg.get("analysis.profile_num_paths", 40))},
            timeout=600.0,
        )
        if capture_reports:
            report_documents["timing_path_profile.json"] = profile_raw
        parsed_profile = json.loads(profile_raw)
        if isinstance(parsed_profile, dict):
            timing_profile = parsed_profile
            if fanout_timing_evidence:
                timing_profile["fanout_parent_net_evidence"] = (
                    fanout_timing_evidence
                )
        availability["timing_profile"] = bool(
            timing_profile and int(timing_profile.get("path_count") or 0) > 0
        )
        if require_timing_profile and (
            not timing_profile or int(timing_profile.get("path_count") or 0) <= 0
        ):
            raise RuntimeError("timing path profile contained no paths")
    except Exception as e:  # noqa: BLE001
        if require_timing_profile:
            raise RuntimeError(f"timing path profile unavailable: {e}") from e
        logger.warning("[agent] Timing path profile unavailable: %s", e)
        if capture_reports:
            report_documents["errors/timing_path_profile.txt"] = str(e)

    utilization: dict = {}
    try:
        util_raw = await mcp.call_vivado(
            "run_tcl",
            {"command": "report_utilization -return_string"},
            timeout=300.0,
        )
        if capture_reports:
            report_documents["utilization.rpt"] = util_raw
        utilization = parse_utilization_report(util_raw)
        availability["utilization"] = bool(utilization)
        if require_timing_profile and not utilization:
            raise RuntimeError("utilization report contained no recognized resources")
    except Exception as e:  # noqa: BLE001
        if require_timing_profile:
            raise RuntimeError(f"utilization report unavailable: {e}") from e
        logger.warning("[agent] Utilization report unavailable: %s", e)
        if capture_reports:
            report_documents["errors/utilization.txt"] = str(e)

    congestion: dict = {}
    try:
        congestion_raw = await mcp.call_vivado(
            "run_tcl",
            {"command": "report_design_analysis -congestion -return_string"},
            timeout=300.0,
        )
        if capture_reports:
            report_documents["congestion.rpt"] = congestion_raw
        congestion = parse_congestion_report(congestion_raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent] Congestion report unavailable: %s", e)
        if capture_reports:
            report_documents["errors/congestion.txt"] = str(e)

    replica_population: dict = {}
    try:
        replica_raw = await mcp.call_vivado(
            "run_tcl",
            {
                "command": (
                    "set approx_replica_cells [concat "
                    "[get_cells -hierarchical -quiet "
                    "-filter {IS_PRIMITIVE && NAME =~ *replica*}] "
                    "[get_cells -hierarchical -quiet "
                    "-filter {IS_PRIMITIVE && NAME =~ *_rep_*}]]; "
                    "puts \"APPROX_REPLICA_NAME_MARKER_COUNT:"
                    "[llength [lsort -unique $approx_replica_cells]]\""
                )
            },
            timeout=300.0,
        )
        if capture_reports:
            report_documents["replica_population.rpt"] = replica_raw
        count_match = re.search(
            r"APPROX_REPLICA_NAME_MARKER_COUNT:\s*(\d+)", replica_raw
        )
        if not count_match:
            raise ValueError("replica marker count missing from Tcl response")
        replica_population = {
            "approximate_name_marker_count": int(count_match.group(1)),
            "name_markers": ["replica", "_rep_"],
            "scope": "primitive_cell_names",
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent] Approximate replica population unavailable: %s", e)
        if capture_reports:
            report_documents["errors/replica_population.txt"] = str(e)

    structural_reports: dict[str, dict] = {}
    for tool, key, report_name in (
        ("analyze_carry_clouds", "carry_clouds", "carry_clouds.json"),
        (
            "analyze_hard_macro_cascades",
            "hard_macro_cascades",
            "hard_macro_cascades.json",
        ),
    ):
        try:
            raw = await mcp.call_vivado(
                tool,
                {"max_cells": 128},
                timeout=300.0,
            )
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("structural analysis did not return an object")
            structural_reports[key] = parsed
            if capture_reports:
                report_documents[report_name] = raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent] %s unavailable: %s", tool, exc)
            structural_reports[key] = {}
            if capture_reports:
                report_documents[f"errors/{key}.txt"] = str(exc)

    analysis = {
        "baseline_wns": baseline_wns,
        "clock_period": clock_period,
        "fanout_nets": fanout_nets,
        "fanout_capabilities": fanout_capabilities,
        "fanout_discovery_num_paths": int(
            cfg.get("analysis.fanout_num_paths", 50)
        ),
        "timing_summary": {
            "wns": baseline_wns,
            "tns": ts.get("tns"),
            "whs": ts.get("whs"),
            "wpws": ts.get("wpws"),
            "failing_endpoints": ts.get("failing_endpoints"),
        },
        "timing_profile": timing_profile,
        "utilization": utilization,
        "congestion": congestion,
        "replica_population": replica_population,
        "carry_clouds": structural_reports["carry_clouds"],
        "hard_macro_cascades": structural_reports["hard_macro_cascades"],
        "_telemetry_availability": availability,
        "_telemetry_complete": bool(
            timing_profile
            and int(timing_profile.get("path_count") or 0) > 0
            and utilization
        ),
    }
    if operator_mining_candidate is not None:
        operator_numeric = None
        try:
            collected_operator_evidence = await collect_operator_mining_evidence(
                mcp,
                cfg,
                incumbent=operator_mining_candidate,
                retry_failed_families=retry_failed_action_menu_families,
                cold_isolation=cold_isolation,
                run_id=run_id,
                telemetry_epoch=telemetry_epoch,
                design_lut_count=(utilization or {}).get("luts"),
                menu_context={
                    "clock_period": clock_period,
                    "baseline_wns": baseline_wns,
                    "timing_summary": analysis["timing_summary"],
                    "route_legal": operator_mining_candidate.get("route_legal"),
                    "telemetry_availability": availability,
                    "timing_profile": timing_profile,
                    "utilization": utilization,
                    "fanout_nets": fanout_nets,
                    "fanout_discovery_num_paths": int(
                        cfg.get("analysis.fanout_num_paths", 50)
                    ),
                    "congestion": congestion,
                    "carry_clouds": structural_reports["carry_clouds"],
                    "hard_macro_cascades": structural_reports[
                        "hard_macro_cascades"
                    ],
                    "actionable_fanout_count": sum(
                        1 for _name, fanout, _paths in fanout_nets
                        if fanout >= 20
                    ),
                    "telemetry_complete": bool(
                        timing_profile
                        and int(timing_profile.get("path_count") or 0) > 0
                        and utilization
                    ),
                    "_same_run_feedback_by_skill": copy.deepcopy(
                        same_run_feedback_by_skill or {}
                    ),
                },
            )
            operator_numeric = collected_operator_evidence.numeric
            public_motif = collected_operator_evidence.public_motif
            registered_arithmetic_packet = collected_operator_evidence.registered_packet
            authorized_action_menu = validate_action_menu(
                collected_operator_evidence.authorized_action_menu
            )
            if operator_numeric:
                analysis["_replay_semantic_evidence"] = operator_numeric
            if public_motif is not None:
                analysis["_repeated_arithmetic_motif_report"] = public_motif
            if registered_arithmetic_packet is not None:
                analysis["_registered_arithmetic_decision_packet"] = (
                    registered_arithmetic_packet
                )
                analysis["_registered_arithmetic_authorizations"] = [
                    {
                        "source_artifact_sha256": registered_arithmetic_packet[
                            "source_artifact_sha256"
                        ],
                        "packet_result_digest": registered_arithmetic_packet[
                            "result_digest"
                        ],
                        "family_fingerprint": row["family_fingerprint"],
                        "plan_digest": row["plan_digest"],
                        "proof_template_id": row["proof_template_id"],
                        "live_wall_coverage_ppm": row[
                            "live_wall_coverage_ppm"
                        ],
                        "missing_obligations": [],
                        "proof_status": "proved",
                    }
                    for row in registered_arithmetic_packet[
                        "normalized_siblings"
                    ]
                    if row["nomination_eligible"] is True
                ]
            analysis["_authorized_action_menu"] = authorized_action_menu
            analysis["_action_menu_timing"] = copy.deepcopy(
                collected_operator_evidence.action_menu_timing
            )
            analysis["_action_menu_build_status"] = copy.deepcopy(
                collected_operator_evidence.action_menu_build_status
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[agent] operator motif evidence unavailable, operator actions "
                "stay unauthorized this epoch: %s",
                exc,
            )
            seed_sha = str(operator_mining_candidate["artifact_sha256"])
            analysis["_authorized_action_menu"] = build_degraded_action_menu(
                seed_candidate_id=str(operator_mining_candidate["candidate_id"]),
                seed_artifact_sha256=seed_sha,
                error_code=_action_menu_failure_code(exc),
                error_detail=str(exc),
                top_k=int(cfg.get("action_menu.top_k", 4)),
                auxiliary_actions=build_epoch_auxiliary_actions(
                    cfg,
                    seed_sha256=seed_sha,
                    run_id=run_id,
                    telemetry_epoch=telemetry_epoch,
                    cold_isolation=cold_isolation,
                    current_evidence={
                        "clock_period": clock_period,
                        "baseline_wns": baseline_wns,
                        "timing_profile": timing_profile,
                        "utilization": utilization,
                        "fanout_nets": fanout_nets,
                        "fanout_discovery_num_paths": int(
                            cfg.get("analysis.fanout_num_paths", 50)
                        ),
                        "congestion": congestion,
                        "carry_clouds": structural_reports["carry_clouds"],
                        "hard_macro_cascades": structural_reports[
                            "hard_macro_cascades"
                        ],
                        "actionable_fanout_count": sum(
                            1 for _name, fanout, _paths in fanout_nets
                            if fanout >= 20
                        ),
                        "telemetry_complete": False,
                        "_same_run_feedback_by_skill": copy.deepcopy(
                            same_run_feedback_by_skill or {}
                        ),
                    },
                ),
                cold_isolation=cold_isolation,
            )
            analysis["_action_menu_build_status"] = {
                "schema_version": "action-menu-build-status-v1",
                "state": "degraded",
                "error_code": _action_menu_failure_code(exc),
                "retry_count": 0,
                "provider_entry": "recover_cold_product_sum_families",
                "seed_artifact_sha256": seed_sha,
            }
            analysis["_action_menu_timing"] = {
                "schema_version": "action-menu-epoch-cost-v1",
                "provider_entry": "recover_cold_product_sum_families",
                "top_k": int(cfg.get("action_menu.top_k", 4)),
            }
        analysis["_operator_action_preflight"] = build_operator_action_preflight(
            operator_numeric
        )
        semantic_numeric: dict[str, int] = {}
        try:
            contest_clock = str(
                cfg.get("legality.contest_clock", "clk_fpl26contest")
            )
            output = await mcp.call_vivado(
                "run_tcl",
                {
                    "command": _build_worst_source_discovery_tcl(
                        256, contest_clock
                    )
                },
                timeout=300.0,
            )
            source_nets = _parse_discovered_nets(str(output))
            semantic_numeric["worst_source_eligible_count"] = int(
                len(source_nets) == 1
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("[agent] staged worst-source preflight unavailable: %s", exc)
        analysis["_semantic_action_preflight"] = build_semantic_action_preflight(
            semantic_numeric,
            recurrence_lineage_active=recurrence_lineage_active,
        )
    if capture_reports:
        report_documents["parsed_telemetry.json"] = json.dumps(
            analysis, indent=2, sort_keys=True, default=str
        )
        analysis["_report_documents"] = report_documents
    return analysis


def publish_report_snapshot(
    run_dir: Path,
    analysis: dict,
    *,
    candidate_id: str,
    artifact_sha256: str,
    telemetry_epoch: int,
) -> tuple[CorpusSnapshot, dict]:
    """Publish an identifier-free report corpus bound to private exact telemetry."""
    if re.fullmatch(r"[0-9a-f]{64}", str(artifact_sha256)) is None:
        raise RuntimeError("report publication requires an exact artifact SHA-256")
    private_analysis = copy.deepcopy(analysis)
    private_analysis.pop("_report_documents", None)
    analysis.pop("_report_documents", None)

    private_dir = Path(run_dir) / "telemetry" / str(artifact_sha256)
    private_dir.mkdir(parents=True, exist_ok=True)
    private_path = private_dir / f"epoch_{int(telemetry_epoch):06d}.json"
    atomic_write_json(private_path, private_analysis)
    private_sha256 = sha256_file(private_path)

    operator_profile = build_operator_extraction_profile(private_analysis)
    measurement_summary = {
        "schema_version": 1,
        "authority": "current_live_measurement_only",
        "timing_summary": operator_profile["timing_summary"],
        "utilization": operator_profile["utilization"],
    }
    documents = {
        "operator_extraction_profile.json": json.dumps(
            operator_profile,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        "measurement_summary.json": json.dumps(
            measurement_summary,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    }
    public_motif = validate_repeated_arithmetic_motif_report(
        private_analysis.get("_repeated_arithmetic_motif_report")
    )
    if public_motif is not None:
        documents["repeated_arithmetic_motif_evidence.json"] = json.dumps(
            public_motif,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    operator_preflight = validate_operator_action_preflight(
        private_analysis.get("_operator_action_preflight")
    )
    if operator_preflight is not None:
        documents["operator_action_preflight.json"] = json.dumps(
            operator_preflight,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    registered_arithmetic_packet = validate_registered_arithmetic_decision_packet(
        private_analysis.get("_registered_arithmetic_decision_packet"),
        expected_design_sha256=str(artifact_sha256),
    )
    if registered_arithmetic_packet is not None:
        documents["registered_arithmetic_decision_packet.json"] = json.dumps(
            registered_arithmetic_packet,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    authorized_action_menu = validate_action_menu(
        private_analysis.get("_authorized_action_menu"),
        expected_seed_sha256=str(artifact_sha256),
    )
    documents["authorized_action_menu.json"] = json.dumps(
        authorized_action_menu,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    concurrent_proof_packet = validate_concurrent_proof_decision_packet(
        private_analysis.get("_concurrent_proof_decision_packet"),
        expected_source_artifact_sha256=str(artifact_sha256),
    )
    if concurrent_proof_packet is not None:
        documents["concurrent_proof_decision_packet.json"] = json.dumps(
            concurrent_proof_packet,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    semantic_preflight = validate_semantic_action_preflight(
        private_analysis.get("_semantic_action_preflight")
    )
    if semantic_preflight is not None:
        documents["semantic_action_preflight.json"] = json.dumps(
            semantic_preflight,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    snapshot = CorpusSnapshot.create(
        Path(run_dir) / "corpora",
        scope="report",
        documents=documents,
        identity={
            "candidate_id": str(candidate_id),
            "artifact_sha256": str(artifact_sha256),
            "telemetry_epoch": int(telemetry_epoch),
        },
    )
    public_manifest = snapshot.manifest_for_llm()
    reference = {
        "snapshot_id": snapshot.snapshot_id,
        "identity": dict(snapshot.identity),
        "documents": copy.deepcopy(public_manifest["documents"]),
        "manifest_path": str(snapshot.manifest_path),
        "private_analysis_path": str(private_path.resolve()),
        "private_analysis_sha256": private_sha256,
        "public_manifest": public_manifest,
    }
    return snapshot, reference


def load_profiled_report_snapshot(
    memory: Memory, candidate: dict
) -> Optional[tuple[CorpusSnapshot, dict, dict]]:
    """Load exact durable report telemetry for an unchanged candidate artifact."""
    candidate_id = str(candidate.get("candidate_id", ""))
    artifact_sha256 = str(candidate.get("artifact_sha256", ""))
    for reference in reversed(memory.report_snapshots):
        identity = reference.get("identity") or {}
        if (
            identity.get("candidate_id") != candidate_id
            or identity.get("artifact_sha256") != artifact_sha256
            or identity.get("telemetry_epoch") is None
        ):
            continue
        try:
            snapshot = CorpusSnapshot.load(
                Path(str(reference["manifest_path"])), expected_scope="report"
            )
            if (
                snapshot.snapshot_id != reference.get("snapshot_id")
                or snapshot.identity != identity
            ):
                raise RuntimeError("persisted report snapshot identity mismatch")
            private_path = Path(str(reference["private_analysis_path"]))
            telemetry_root = (
                Path(str(memory.run_manifest["canonical_run_dir"])).resolve()
                / "telemetry"
            )
            resolved_private = private_path.resolve()
            if (
                private_path.is_symlink()
                or not private_path.is_file()
                or telemetry_root != resolved_private.parent
                and telemetry_root not in resolved_private.parents
            ):
                raise RuntimeError("persisted private telemetry path is invalid")
            if sha256_file(private_path) != reference.get(
                "private_analysis_sha256"
            ):
                raise RuntimeError("persisted private telemetry hash mismatch")
            analysis = json.loads(private_path.read_text(encoding="utf-8"))
            if not isinstance(analysis, dict):
                raise RuntimeError("persisted parsed telemetry is not an object")
            return snapshot, dict(reference), analysis
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[agent] unusable cached report snapshot for %s: %s",
                candidate_id,
                exc,
            )
    return None


def staged_decision_binding_error(
    decision: Decision,
    report_snapshot: Optional[CorpusSnapshot],
    knowledge_snapshot: Optional[CorpusSnapshot],
    memory: Memory,
    selected_seed: dict,
) -> str:
    """Return why staged evidence cannot authorize action on ``selected_seed``."""
    if decision.selected_entry_id:
        telemetry = memory.current_telemetry or {}
        menu = telemetry.get("authorized_action_menu")
        source_sha = str(selected_seed.get("artifact_sha256") or "")
        try:
            selection = select_action_menu_entry(
                menu,
                {
                    "selected_entry_id": decision.selected_entry_id,
                    "rationale": decision.expectation,
                },
                current_seed_sha256=source_sha,
                cold_isolation=bool(
                    (memory.run_manifest.get("isolation") or {}).get(
                        "replay_derived_actions_forbidden"
                    )
                ),
            )
        except ActionMenuError as exc:
            return str(exc)
        if (
            telemetry.get("candidate_id") != selected_seed.get("candidate_id")
            or selection["seed_candidate_id"] != selected_seed.get("candidate_id")
            or decision.action_menu_digest != selection["menu_digest"]
            or decision.action_menu_seed_sha256 != source_sha
        ):
            return "selected action-menu entry is not bound to active telemetry"
        expected = selection["action"]
        actual = {
            "action": decision.action,
            **(
                {
                    "skill": decision.skill,
                    "target": decision.target,
                    "params": decision.params,
                }
                if decision.action == "run_skill" else {}
            ),
            **(
                {
                    "server": decision.server,
                    "tool": decision.tool,
                    "arguments": decision.arguments,
                }
                if decision.action == "probe" else {}
            ),
            **(
                {"stop_reason": decision.stop_reason}
                if decision.action == "save_and_exit" else {}
            ),
        }
        if actual != expected:
            return "selected action-menu parameters changed after exact lookup"
        return ""
    if report_snapshot is None or knowledge_snapshot is None:
        return "staged report or knowledge snapshot is unavailable"
    candidate_id = str(selected_seed.get("candidate_id", ""))
    artifact_sha256 = str(selected_seed.get("artifact_sha256", ""))
    identity = report_snapshot.identity
    if decision.report_snapshot_id != report_snapshot.snapshot_id:
        return "decision report snapshot is not the active report snapshot"
    if identity.get("candidate_id") != candidate_id:
        return (
            f"report candidate {identity.get('candidate_id')!r} does not match "
            f"selected seed {candidate_id!r}"
        )
    if identity.get("artifact_sha256") != artifact_sha256:
        return "report artifact SHA does not match selected seed"
    telemetry = memory.current_telemetry or {}
    telemetry_ref = telemetry.get("report_snapshot") or {}
    telemetry_identity = telemetry_ref.get("identity") or {}
    if (
        telemetry.get("candidate_id") != candidate_id
        or telemetry_ref.get("snapshot_id") != report_snapshot.snapshot_id
        or telemetry_identity != identity
        or telemetry.get("epoch") != identity.get("telemetry_epoch")
    ):
        return "active telemetry is not bound to the selected seed report epoch"
    refs = list(decision.evidence_refs or [])
    report_refs = sorted({ref for ref in refs if ref.startswith("report:")})
    if not report_refs or any(
        not report_snapshot.resolves_evidence_ref(ref) for ref in report_refs
    ):
        return "report evidence does not resolve to the selected seed snapshot"
    knowledge_refs = sorted({ref for ref in refs if ref.startswith("knowledge:")})
    if not knowledge_refs or any(
        not knowledge_snapshot.resolves_evidence_ref(ref)
        for ref in knowledge_refs
    ):
        return "knowledge evidence does not resolve to the active knowledge snapshot"
    if decision.knowledge_snapshot_id != knowledge_snapshot.snapshot_id:
        return "decision knowledge snapshot is not active"
    return ""


def registered_arithmetic_authorization_error(
    memory: Memory,
    selected_seed: dict,
    family_fingerprint: object,
) -> str:
    """Fail closed unless all three nomination gates bind to this exact seed."""
    fingerprint = str(family_fingerprint or "")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        return "registered arithmetic requires a current family fingerprint"
    telemetry = memory.current_telemetry or {}
    source_sha = str(selected_seed.get("artifact_sha256") or "")
    report_identity = (telemetry.get("report_snapshot") or {}).get("identity") or {}
    if (
        telemetry.get("candidate_id") != selected_seed.get("candidate_id")
        or report_identity.get("artifact_sha256") != source_sha
    ):
        return "registered arithmetic telemetry is stale for the selected seed"
    matches = [
        item
        for item in telemetry.get("registered_arithmetic_authorizations") or []
        if isinstance(item, dict)
        and item.get("family_fingerprint") == fingerprint
        and item.get("source_artifact_sha256") == source_sha
    ]
    if len(matches) != 1:
        return "registered arithmetic family is absent, stale, or ambiguous"
    binding = matches[0]
    coverage = binding.get("live_wall_coverage_ppm")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, int)
        or coverage <= 0
    ):
        return "registered arithmetic family has zero live-wall coverage"
    if binding.get("missing_obligations") != []:
        return "registered arithmetic family has incomplete contract obligations"
    if binding.get("proof_status") != "proved":
        return "registered arithmetic family proof is not proved"
    return ""


def bind_unique_registered_arithmetic_params(
    memory: Memory,
    selected_seed: dict,
    params: object,
) -> dict:
    """Bind an omitted canonical family ID when exactly one is authorized.

    The LLM selects the public action class.  The current-seed tool packet owns
    the private canonical family identity.  Auto-binding is allowed only for a
    unique authorization that independently passes the unchanged exact gates;
    explicit fingerprints are preserved and validated by the normal path.
    """
    bound = copy.deepcopy(params) if isinstance(params, dict) else {}
    explicit = bound.get("family_fingerprint")
    if isinstance(explicit, str) and re.fullmatch(r"[0-9a-f]{64}", explicit):
        return bound
    # A malformed prose value is not a family selection and carries no
    # authority.  Discard it before the same unique tool-owned binding used for
    # omission; a well-formed but wrong fingerprint remains explicit and fails
    # the unchanged authorization gate.
    bound.pop("family_fingerprint", None)
    telemetry = memory.current_telemetry or {}
    source_sha = str(selected_seed.get("artifact_sha256") or "")
    report_identity = (telemetry.get("report_snapshot") or {}).get("identity") or {}
    if (
        telemetry.get("candidate_id") != selected_seed.get("candidate_id")
        or report_identity.get("artifact_sha256") != source_sha
    ):
        raise CapabilityError(
            "cannot bind registered arithmetic family from stale telemetry"
        )
    eligible = []
    for item in telemetry.get("registered_arithmetic_authorizations") or []:
        if not isinstance(item, dict):
            continue
        fingerprint = item.get("family_fingerprint")
        if registered_arithmetic_authorization_error(
            memory, selected_seed, fingerprint
        ) == "":
            eligible.append(str(fingerprint))
    if len(eligible) != 1:
        raise CapabilityError(
            "registered arithmetic requires exactly one current-seed authorized "
            "family for tool-owned parameter binding"
        )
    bound["family_fingerprint"] = eligible[0]
    return bound


def build_action_menu_launch_authorization(
    *,
    decision: Decision,
    resolved_action: ResolvedAction,
    report_snapshot: CorpusSnapshot,
    knowledge_snapshot: CorpusSnapshot,
    memory: Memory,
    selected_seed: dict,
) -> Optional[dict]:
    """Bind a supported public operator direction to one exact private launch."""
    if (
        resolved_action.skill,
        resolved_action.canonical_target,
    ) not in registered_operator_directions():
        return None
    if not decision.selected_entry_id:
        raise CapabilityError(
            "staged operator launch requires an Action Menu selection"
        )
    binding_error = staged_decision_binding_error(
        decision,
        report_snapshot,
        knowledge_snapshot,
        memory,
        selected_seed,
    )
    if binding_error:
        raise CapabilityError(binding_error)
    menu = validate_action_menu(
        memory.current_telemetry.get("authorized_action_menu"),
        expected_seed_sha256=str(selected_seed["artifact_sha256"]),
        cold_isolation=bool(
            (memory.run_manifest.get("isolation") or {}).get(
                "replay_derived_actions_forbidden"
            )
        ),
    )
    entry = next(
        item for item in menu["entries"]
        if item["entry_id"] == decision.selected_entry_id
    )
    if (
        resolved_action.skill != entry["action"].get("skill")
        or resolved_action.canonical_target != entry["action"].get("target")
        or any(
            resolved_action.resolved_params.get(key) != value
            for key, value in entry["action"].get("params", {}).items()
        )
    ):
        raise CapabilityError(
            "resolved action differs from selected menu parameter binding"
        )
    return {
        "schema_version": 1,
        "kind": "action_menu_launch_authorization",
        "seed_candidate_id": selected_seed["candidate_id"],
        "seed_artifact_sha256": selected_seed["artifact_sha256"],
        "menu_digest": menu["menu_digest"],
        "selected_entry_id": entry["entry_id"],
        "family_fingerprint": entry["family_fingerprint"],
        "recovered_plan_digest": entry["recovered_plan_digest"],
        "proof_digest": entry["proof_digest"],
        "parameter_authority": menu["parameter_authority"],
    }


def seed_suggested_targets(memory: Memory, analysis: dict) -> None:
    """Informational target hints for the LLM (never gates)."""
    fanout_nets = analysis.get("fanout_nets") or []
    fanout_capabilities = analysis.get("fanout_capabilities") or {}
    fanout_targets = [
        name
        for name, fanout, _paths in fanout_nets
        if fanout >= 20 and not is_unsuitable_fanout_opt_net_name(name)
        and (fanout_capabilities.get(name) or {}).get("rapidwright_fanout")
        == "supported"
    ]
    replicate_targets = [
        name
        for name, fanout, _paths in fanout_nets
        if fanout >= 30 and not is_unsuitable_force_replicate_net_name(name)
    ]
    memory.suggested_targets = {
        "phys_opt": list(PHYS_OPT_PRESETS),
        "implementation_recipe": list(IMPLEMENTATION_RECIPE_TARGETS),
        "fanout_opt": fanout_targets[:10],
        "force_replicate": replicate_targets[:10],
        "post_route_cleanup": ["auto", "pre_lut_directive", "post_lut_cleanup"],
        "critical_net_reroute": [
            "auto", "worst_source", "boundary_link", "early_window",
            "high_delay10", "top3_final"
        ],
        "fresh_place_route": ["auto"],
        "clock_tighten": ["auto"],
        "pblock_sweep": ["auto"],
        "pblock": ["auto"],
        "lut_pin_swap": ["auto"],
        "endpoint_bel_move": ["auto"],
        "hard_macro_move": ["auto"],
        "critical_cluster_anchor": ["auto"],
        "structure_relocation": ["carry_cloud", "hard_macro_cascade"],
        "equivalent_source_remap": ["auto_nearest_proven"],
        "operator_rewrite": [
            "dsp_srl_recurrence_cut_v1", "fixed_point_dsp_rebuild_v1",
            "dsp_mul_two_operand_v1", "dsp_register_absorb_v1",
            "dsp_pipeline_rebalance_v1", "registered_arithmetic_cone_v1",
            "constructive_accumulator_rebuild_v1",
            "memory_write_buffer_forward_v1",
        ],
    }


# ---------------------------------------------------------------------------
# Fallback plan (no LLM / token budget exhausted)
# ---------------------------------------------------------------------------

def build_fallback_plan(matches, memory: Memory, cfg: Config) -> list[dict]:
    """Static action list derived from the best-matching knowledge case."""
    steps: list[dict] = []
    if matches:
        case = matches[0].case
        for skill_name in case.suggested_skill_order:
            if skill_name not in SKILLS:
                continue
            params = dict(case.initial_params.get(skill_name) or {})
            semantic_targets = params.get("target_candidates") or []
            if isinstance(semantic_targets, str):
                semantic_targets = [semantic_targets]
            if not semantic_targets:
                targets = memory.suggested_targets.get(skill_name) or ["auto"]
                semantic_targets = [targets[0]]
            for target in semantic_targets:
                steps.append({
                    "skill": skill_name,
                    "target": str(target),
                    "params": {
                        key: value
                        for key, value in params.items()
                        if key not in {
                            "target_candidates",
                            "target_params",
                            "replace_target_candidates",
                        }
                    },
                })
    if not steps:
        for skill_name in ("phys_opt", "post_route_cleanup", "fresh_place_route", "clock_tighten"):
            steps.append({"skill": skill_name, "target": "auto", "params": {}})
        steps[0]["target"] = "Default"
    return steps


def materialize_candidate_artifact(
    source: Path,
    candidate_dir: Path,
    *,
    expected_sha256: Optional[str] = None,
) -> Path:
    """Copy ``source`` and its source-bound readable-EDIF cache."""
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"candidate source is not a regular file: {source}")
    expected = expected_sha256 or sha256_file(source)
    candidate_dir.mkdir(parents=True, exist_ok=False)
    canonical = candidate_dir / "design.dcp"
    atomic_copy_file(source, canonical, expected_sha256=expected)
    source_edif_cache = Path(str(source) + ".edf")
    if source_edif_cache.is_dir():
        _publish_readable_edif(source_edif_cache, Path(str(canonical) + ".edf"))
    return canonical


def _publish_readable_edif(source: Path, destination: Path) -> None:
    """Publish a candidate-owned readable-EDIF sidecar.

    Action transaction directories are disposable after candidate promotion,
    so a symlink back into the producing action can become dangling while the
    promoted DCP remains live.  Copy the sidecar into the candidate/final
    artifact directory so its lifetime matches the DCP it authenticates.
    """
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def atomic_copy_checkpoint(source: Path, destination: Path) -> None:
    """Publish a DCP together with its readable-EDIF sidecar when present."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy_file(source, destination)
    source_edif = Path(str(source) + ".edf")
    destination_edif = Path(str(destination) + ".edf")
    if not source_edif.is_dir():
        return
    if destination_edif.exists() or destination_edif.is_symlink():
        if destination_edif.is_dir() and not destination_edif.is_symlink():
            shutil.rmtree(destination_edif)
        else:
            destination_edif.unlink()
    _publish_readable_edif(source_edif, destination_edif)


def measurement_from_node(node: dict) -> dict:
    return {
        "clock_period_ns": node.get("clock_period_ns"),
        "wns_ns": node.get("wns_ns"),
        "fmax_mhz": node.get("fmax_mhz"),
        "tns_ns": node.get("tns_ns"),
        "failing_endpoints": node.get("failing_endpoints"),
        "total_endpoints": node.get("total_endpoints"),
        "whs_ns": node.get("whs_ns"),
        "wpws_ns": node.get("wpws_ns"),
        "route_legal": node.get("route_legal"),
        "placed_legal": node.get("placed_legal"),
        "unplaced_primitives": node.get("unplaced_primitives"),
        "hold_legal": node.get("hold_legal"),
        "pulse_legal": node.get("pulse_legal"),
        "measurement_complete": bool(node.get("measurement_complete")),
        "eligible_for_beam": bool(node.get("eligible_for_beam")),
        "legality_reason": node.get("legality_reason", ""),
    }


def apply_measurement_to_node(node: dict, measurement: dict) -> None:
    """Persist a fresh central measurement onto an existing graph node."""
    for key in (
        "clock_period_ns",
        "wns_ns",
        "fmax_mhz",
        "tns_ns",
        "failing_endpoints",
        "total_endpoints",
        "whs_ns",
        "wpws_ns",
        "route_legal",
        "placed_legal",
        "unplaced_primitives",
        "hold_legal",
        "pulse_legal",
        "measurement_complete",
        "eligible_for_beam",
        "legality_reason",
    ):
        node[key] = measurement.get(key)
    artifact_sha256 = str(node.get("artifact_sha256", ""))
    node["submission_eligible"] = bool(
        node.get("eligible_for_beam")
        and node.get("placed_legal") is True
        and node.get("functional_equivalence") == "passed"
        and node.get("equivalence_candidate_sha256") == artifact_sha256
    )
    if not node.get("eligible_for_beam"):
        node["in_active_beam"] = False
        node["search_state"] = "ineligible"
    else:
        node["in_active_beam"] = True
        node["search_state"] = "active"


def _transaction_candidates(transaction: dict) -> list[dict]:
    """Return v2 candidate records, normalizing a persisted v1 transaction."""
    candidates = transaction.get("candidates")
    if isinstance(candidates, list) and candidates:
        return candidates
    candidate_id = transaction.get("candidate_id")
    if not candidate_id:
        return []
    return [{
        "ordinal": 0,
        "candidate_id": str(candidate_id),
        "source_path": transaction.get("source_path", ""),
        "label": transaction.get("result_summary", ""),
        "canonical_path": transaction.get("canonical_path", ""),
        "artifact_size_bytes": transaction.get("artifact_size_bytes"),
        "artifact_sha256": transaction.get("artifact_sha256", ""),
        "measurement": transaction.get("measurement"),
        "validation_runtime_s": transaction.get("validation_runtime_s"),
        "evidence": transaction.get("evidence"),
        "proof": transaction.get("proof"),
    }]


def _transaction_history_limit(cfg: Config) -> int:
    return int(cfg.get("action_review.transaction_history_limit", 64))


def _measurement_terminal_status(measurements: list[dict]) -> tuple[str, str]:
    legality = [measurement_legality(item) for item in measurements]
    if any(value is True for value in legality):
        return "committed", "centrally measured action cycle committed"
    if measurements and all(value is False for value in legality):
        return "illegal_candidates", "all centrally measured candidates were illegal"
    return "measurement_failed", "candidate legality remained unknown after measurement"


def validate_graph_node_artifact(run_dir: Path, node: dict) -> Path:
    """Return one exact regular graph-owned artifact bound to node metadata."""
    candidate_id = str(node.get("candidate_id") or "")
    stored_path = Path(str(node.get("artifact_path") or ""))
    artifact = validate_owned_candidate_path(run_dir, candidate_id, stored_path)
    if artifact.is_symlink() or not artifact.is_file():
        raise RuntimeError(f"candidate {candidate_id} artifact is unavailable")
    digest = sha256_file(artifact)
    if digest != node.get("artifact_sha256"):
        raise RuntimeError(f"candidate {candidate_id} artifact hash changed")
    expected_size = node.get("artifact_size_bytes")
    if expected_size is not None and artifact.stat().st_size != int(expected_size):
        raise RuntimeError(f"candidate {candidate_id} artifact size changed")
    return artifact


def build_replay_observation(analysis: dict) -> dict:
    """Project only observed finite aggregates into the replay policy boundary."""
    projected: dict[str, object] = {}
    availability = analysis.get("_telemetry_availability") or {}
    period = analysis.get("clock_period")
    wns = analysis.get("baseline_wns")
    if isinstance(period, (int, float)) and not isinstance(period, bool) and period > 0:
        if isinstance(wns, (int, float)) and not isinstance(wns, bool):
            projected["closure_ratio"] = float(wns) / float(period)

    features = extract_features(analysis).to_dict()
    if availability.get("utilization") is True:
        for field in ("lut_util_pct", "dsp_count", "bram_count", "uram_count"):
            value = features.get(field)
            if value is not None:
                projected[field] = value
    if availability.get("timing_profile") is True:
        for field in (
            "avg_route_pct",
            "route_dominated_ratio",
            "max_logic_levels",
            "avg_logic_levels",
            "critical_lut_stage_ratio",
            "critical_lut_route_coverage",
            "spread_ratio",
            "fanout_live_wall_coverage",
            "max_single_net_live_wall_coverage",
        ):
            projected[field] = features[field]
    if availability.get("fanout") is True:
        projected["actionable_fanout_count"] = features["eligible_hf_count"]
        projected["broad_fanout_count"] = features["broad_hf_count"]
        projected["fanout_live_wall_coverage"] = features[
            "fanout_live_wall_coverage"
        ]
        projected["max_single_net_live_wall_coverage"] = features[
            "max_single_net_live_wall_coverage"
        ]

    semantic = analysis.get("_replay_semantic_evidence") or {}
    for field in (
        "repeated_arithmetic_family_count",
        "repeated_arithmetic_similarity_max",
        "clock_as_data_path_risk_count",
        "recurrence_eligible_count",
        "boundary_link_eligible_count",
        "worst_source_eligible_count",
        "endpoint_move_eligible_count",
    ):
        if field in semantic:
            projected[field] = semantic[field]
    return ReplayPolicy().sanitize_observation(projected)


def build_replay_execution_metadata(
    policy: ReplayPolicy,
    cfg: Config,
    *,
    use_llm: bool,
    model: Optional[str],
    time_safety_s: float,
    promotion_delta_mhz: float,
    skill_candidate_cap: int,
    candidate_commit_reserve_s: float,
) -> dict:
    """Separate resume compatibility from complete execution provenance."""
    resolved_actions = []
    for action_id, template in ACTION_TEMPLATES.items():
        resolved = resolve_replay_action(template)
        resolved_actions.append({
            "action_id": action_id,
            "skill": resolved.skill,
            "target": resolved.canonical_target,
            "action_fingerprint": resolved.action_fingerprint,
            "concrete_action": resolved.concrete_action,
        })
    config_json = json.dumps(
        cfg.as_dict(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    requested_model = str(model or cfg.require("llm.model")) if use_llm else None
    if use_llm and requested_model and "/" not in requested_model:
        requested_model = f"openai/{requested_model}"
    semantic_contract = {
        "schema_version": 1,
        "policy": policy.metadata(),
        "resolved_actions": resolved_actions,
        "contest_clock": str(
            cfg.get("legality.contest_clock", "clk_fpl26contest")
        ),
        "platform_id": str(cfg.get("platform.id", "unspecified")),
    }
    semantic_json = json.dumps(
        semantic_contract,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    semantic_contract["semantic_contract_sha256"] = hashlib.sha256(
        semantic_json
    ).hexdigest()
    provenance = {
        "schema_version": 1,
        "selector": {
            "mode": "llm" if use_llm else "deterministic",
            "model": requested_model,
            "transport": "openrouter" if use_llm else None,
            "stage": (
                cfg.get("llm.stages.replay_selection", {}) if use_llm else {}
            ),
        },
        "promotion_min_delta_mhz": float(promotion_delta_mhz),
        "max_skill_candidates_per_action": int(skill_candidate_cap),
        "time_safety_s": float(time_safety_s),
        "candidate_commit_reserve_s": float(candidate_commit_reserve_s),
        "config_sha256": hashlib.sha256(config_json).hexdigest(),
    }
    provenance_json = json.dumps(
        provenance,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    provenance["provenance_sha256"] = hashlib.sha256(provenance_json).hexdigest()
    return {
        "semantic_contract": semantic_contract,
        "provenance": provenance,
    }


MAX_CRITICAL_PATHS = 16
# Real contest designs reach ~70 cells per failing path when the clock is far
# from closure, so a 64-cell ceiling silently discarded the whole mining
# corpus for exactly the designs that need it most.  The ceiling only exists to
# bound an external tool payload, so it is set well above observed depth while
# the worst-case payload stays bounded.
MAX_CELLS_PER_CRITICAL_PATH = 256
MAX_CRITICAL_PATH_CELL_NAME = 1024


def validate_critical_path_cells(paths: Any) -> None:
    """Fail closed on out-of-contract critical-path payloads, with diagnosis.

    The bounds guard an external tool response, not design semantics, so a
    rejection reports the violated bound and the observed size instead of one
    opaque message.
    """
    if not isinstance(paths, list):
        raise ValueError(
            "critical-path cells must be a list, got "
            f"{type(paths).__name__}"
        )
    if len(paths) > MAX_CRITICAL_PATHS:
        raise ValueError(
            f"critical-path count {len(paths)} exceeds bound "
            f"{MAX_CRITICAL_PATHS}"
        )
    for index, path in enumerate(paths):
        if not isinstance(path, list):
            raise ValueError(
                f"critical path {index} must be a list, got "
                f"{type(path).__name__}"
            )
        if len(path) > MAX_CELLS_PER_CRITICAL_PATH:
            raise ValueError(
                f"critical path {index} has {len(path)} cells, exceeding bound "
                f"{MAX_CELLS_PER_CRITICAL_PATH}"
            )
        for cell in path:
            if not isinstance(cell, str) or not cell:
                raise ValueError(
                    f"critical path {index} contains a non-string or empty cell"
                )
            if len(cell) > MAX_CRITICAL_PATH_CELL_NAME:
                raise ValueError(
                    f"critical path {index} contains a cell name of "
                    f"{len(cell)} characters, exceeding bound "
                    f"{MAX_CRITICAL_PATH_CELL_NAME}"
                )


@dataclass
class OperatorMiningEvidence:
    """Exact-seed operator evidence and its mandatory authorized Action Menu."""

    numeric: dict[str, object]
    public_motif: Optional[dict]
    registered_packet: Optional[dict]
    authorized_action_menu: dict
    action_menu_timing: dict
    action_menu_build_status: dict


def _action_menu_failure_code(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or "ACTION_MENU_BUILD_FAILED").upper()
    code = re.sub(r"[^A-Z0-9_]+", "_", code).strip("_")
    return code if len(code) >= 3 else "ACTION_MENU_BUILD_FAILED"


def _menu_numeric(mapping: object, key: str, default: float = 0.0) -> float:
    value = mapping.get(key) if isinstance(mapping, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    return float(value)


def operator_census_warrants_deep_recovery(
    numeric: Mapping[str, object],
    public_motif: object,
    recurrence: Sequence[Mapping[str, object]],
) -> tuple[bool, tuple[str, ...]]:
    """Route an exact-seed census to deep recovery only on live operator evidence.

    The census itself is extracted from the current critical paths.  Therefore
    a positive family/hypothesis is already a live-wall intersection; no DCP
    identity, historical action order, or benchmark-tuned score threshold is
    needed for this scheduling decision.
    """
    reasons: list[str] = []
    if recurrence:
        reasons.append("recurrence_family_on_current_critical_paths")
    report = validate_repeated_arithmetic_motif_report(public_motif)
    if report is not None and int(report.get("resolved_sequence_count") or 0) > 0:
        reasons.append("repeated_operator_family_on_current_critical_paths")
    for key in (
        "fixed_point_product_hypothesis_count",
        "two_operand_product_hypothesis_count",
        "register_absorb_hypothesis_count",
        "pipeline_rebalance_hypothesis_count",
        "control_to_data_state_equation_live_hint_count",
        "constructive_accumulator_live_wall_covered_path_count",
        "memory_write_buffer_forward_live_wall_covered_path_count",
    ):
        value = numeric.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) > 0.0
        ):
            reasons.append(key)
    return bool(reasons), tuple(dict.fromkeys(reasons))


def _evidence_gated_phys_opt_targets(context: object) -> tuple[str, ...]:
    """Return a small current-evidence subset of fixed phys-opt presets."""
    if not isinstance(context, dict):
        return ("Default", "Explore")
    profile = context.get("timing_profile") or {}
    utilization = context.get("utilization") or {}
    route_pct = _menu_numeric(profile, "avg_route_pct")
    logic_levels = _menu_numeric(profile, "max_logic_levels")
    fanout_count = int(_menu_numeric(context, "actionable_fanout_count"))
    selected = ["Default", "Explore"]
    if route_pct >= 65.0:
        selected.append("routing_critical_pin")
    elif route_pct > 0.0:
        selected.append("placement_critical_cell")
    if fanout_count > 0:
        selected.extend(("AggressiveFanoutOpt", "equ_drivers"))
    if logic_levels >= 12:
        selected.append("AddRetime")
    return tuple(dict.fromkeys(selected))


def _evidence_gated_recipe_targets(context: object) -> tuple[str, ...]:
    """Nominate generic full-flow recipes only from current design evidence."""
    if not isinstance(context, dict):
        return ()
    profile = context.get("timing_profile") or {}
    utilization = context.get("utilization") or {}
    period = _menu_numeric(context, "clock_period")
    wns = _menu_numeric(context, "baseline_wns")
    closure_ratio = wns / period if period > 0.0 else 0.0
    route_pct = _menu_numeric(profile, "avg_route_pct")
    logic_levels = _menu_numeric(profile, "max_logic_levels")
    lut_util = _menu_numeric(utilization, "lut_util_pct")
    selected = []
    if period > 0.0 and closure_ratio <= -0.20:
        selected.append("derived_pressure_replace_route_v1")
    if route_pct >= 65.0 and (logic_levels >= 12.0 or lut_util >= 45.0):
        selected.append("full_reimplementation_retime_v1")
    if context.get("telemetry_complete") is True:
        selected.append("routed_critical_cleanup_v1")
    return tuple(selected)


def _current_seed_action_features(context: object) -> dict:
    """Project one telemetry context into the shared action parameter inputs."""
    evidence = dict(context) if isinstance(context, Mapping) else {}
    features = extract_features(evidence, design_id="").to_dict()
    profile = evidence.get("timing_profile") or {}
    path_count = int(profile.get("path_count") or 0)
    features.update({
        "path_count": path_count,
        "critical_path_ownership": (
            "current_seed" if path_count > 0 else None
        ),
        "congestion": (
            "current_seed" if evidence.get("congestion") else None
        ),
        "resource_use": (
            "current_seed" if evidence.get("utilization") else None
        ),
        "structure_report": (
            "current_seed"
            if evidence.get("carry_clouds") or evidence.get("hard_macro_cascades")
            else None
        ),
    })
    return features


def build_same_seed_action_feedback(
    memory: Memory, *, seed_artifact_sha256: str
) -> dict[str, dict]:
    """Project measured outcomes bound to this exact seed and current run.

    Parent-seed outcomes are deliberately not carried across promotion. They
    cannot parameterize a descendant and disappear until that descendant has
    its own measured action outcome.
    """
    grouped: dict[str, list[dict]] = {}
    for transaction in memory.action_transactions:
        if not isinstance(transaction, dict):
            continue
        proof = transaction.get("outcome_proof")
        if (
            not isinstance(proof, dict)
            or proof.get("kind") != "action_outcome_proof"
            or proof.get("seed_artifact_sha256") != seed_artifact_sha256
        ):
            continue
        skill = str(proof.get("skill") or transaction.get("skill") or "")
        if not skill:
            continue
        legal_deltas = [
            float(item["delta_fmax_mhz"])
            for item in proof.get("outcomes") or []
            if isinstance(item, dict)
            and item.get("legal") is True
            and isinstance(item.get("delta_fmax_mhz"), (int, float))
            and not isinstance(item.get("delta_fmax_mhz"), bool)
        ]
        grouped.setdefault(skill, []).append({
            "target": str(proof.get("target") or transaction.get("target") or ""),
            "mechanism_family": str(proof.get("mechanism_family") or ""),
            "action_fingerprint": str(proof.get("action_fingerprint") or ""),
            "terminal_status": str(proof.get("terminal_status") or ""),
            "cycle_result": str(proof.get("cycle_result") or ""),
            "best_legal_delta_fmax_mhz": max(legal_deltas) if legal_deltas else None,
            "legal_candidate_count": len(legal_deltas),
        })
    projected = {}
    for skill, attempts in grouped.items():
        by_family: dict[str, list[dict]] = {}
        for attempt in attempts:
            family = str(attempt.get("mechanism_family") or "")
            if family:
                by_family.setdefault(family, []).append(attempt)
        projected[skill] = {
            "binding": "exact_current_seed_measured_outcomes_only",
            "seed_artifact_sha256": seed_artifact_sha256,
            "attempt_count": len(attempts),
            "recent_attempts": attempts[-3:],
            "by_mechanism_family": {
                family: {
                    "binding": "exact_current_seed_measured_outcomes_only",
                    "seed_artifact_sha256": seed_artifact_sha256,
                    "attempt_count": len(family_attempts),
                    "recent_attempts": family_attempts[-3:],
                    "mutation_authority": False,
                }
                for family, family_attempts in by_family.items()
            },
            "mutation_authority": False,
        }
    return projected


_LOCAL_PHYSICAL_FEEDBACK_FAMILIES = {
    "post_route_physical",
    "critical_net_reroute",
    "fanout_replication",
    "local_placement",
    "path_local_logic",
    "path_local_lut_physical",
    "operator_mapping",
    "placement_locality",
}


def build_current_lineage_action_feedback(
    memory: Memory, *, seed_artifact_sha256: str
) -> dict[str, dict]:
    """Add advisory direct-ancestor outcomes to exact-seed ranking feedback.

    The ancestor summary is limited to the current run's direct candidate
    lineage. It carries no discovery facts, parameters, proof, or mutation
    authority; every descendant still needs fresh current-SHA profiling.
    """
    feedback = build_same_seed_action_feedback(
        memory, seed_artifact_sha256=seed_artifact_sha256
    )
    graph = (
        memory.candidate_graph
        if isinstance(memory.candidate_graph, dict) else {}
    )
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
    current_id = str(graph.get("current_best_candidate_id") or "")
    current = nodes.get(current_id) if current_id else None
    if (
        not isinstance(current, dict)
        or str(current.get("artifact_sha256") or "") != seed_artifact_sha256
    ):
        matches = [
            (candidate_id, node)
            for candidate_id, node in nodes.items()
            if isinstance(node, dict)
            and str(node.get("artifact_sha256") or "") == seed_artifact_sha256
        ]
        if len(matches) != 1:
            return feedback
        current_id, current = matches[0]

    lineage_distance_by_sha: dict[str, int] = {}
    cursor = current
    distance = 0
    while isinstance(cursor, dict) and distance <= len(nodes):
        sha = str(cursor.get("artifact_sha256") or "")
        if sha:
            lineage_distance_by_sha.setdefault(sha, distance)
        parent_id = cursor.get("parent_candidate_id")
        if parent_id is None:
            break
        cursor = nodes.get(str(parent_id))
        distance += 1

    lineage_grouped: dict[str, list[dict]] = {}
    attempted_local_families: set[str] = set()
    nearest_positive_path_local: Optional[tuple[int, float]] = None
    nearest_phys_opt_distance: Optional[int] = None
    for transaction in memory.action_transactions:
        proof = (
            transaction.get("outcome_proof")
            if isinstance(transaction, dict) else None
        )
        if (
            not isinstance(proof, dict)
            or proof.get("kind") != "action_outcome_proof"
        ):
            continue
        proof_seed = str(proof.get("seed_artifact_sha256") or "")
        lineage_distance = lineage_distance_by_sha.get(proof_seed)
        if lineage_distance is None:
            continue
        family = str(proof.get("mechanism_family") or "")
        if family in _LOCAL_PHYSICAL_FEEDBACK_FAMILIES:
            attempted_local_families.add(family)
        if lineage_distance == 0:
            continue
        skill = str(proof.get("skill") or transaction.get("skill") or "")
        if not skill:
            continue
        legal_deltas = [
            float(item["delta_fmax_mhz"])
            for item in proof.get("outcomes") or []
            if isinstance(item, dict)
            and item.get("legal") is True
            and isinstance(item.get("delta_fmax_mhz"), (int, float))
            and not isinstance(item.get("delta_fmax_mhz"), bool)
        ]
        if skill == "phys_opt" and (
            nearest_phys_opt_distance is None
            or lineage_distance < nearest_phys_opt_distance
        ):
            nearest_phys_opt_distance = int(lineage_distance)
        if family == "path_local_lut_physical" and legal_deltas:
            best_delta = max(legal_deltas)
            if best_delta > 0.0 and (
                nearest_positive_path_local is None
                or lineage_distance < nearest_positive_path_local[0]
            ):
                nearest_positive_path_local = (
                    int(lineage_distance), float(best_delta)
                )
        lineage_grouped.setdefault(skill, []).append({
            "target": str(proof.get("target") or transaction.get("target") or ""),
            "mechanism_family": family,
            "action_fingerprint": str(proof.get("action_fingerprint") or ""),
            "terminal_status": str(proof.get("terminal_status") or ""),
            "best_legal_delta_fmax_mhz": max(legal_deltas) if legal_deltas else None,
            "legal_candidate_count": len(legal_deltas),
            "lineage_distance": int(lineage_distance),
        })

    for skill, attempts in lineage_grouped.items():
        skill_feedback = feedback.setdefault(
            skill,
            {
                "binding": "exact_seed_plus_direct_ancestor_advisory",
                "seed_artifact_sha256": seed_artifact_sha256,
                "attempt_count": 0,
                "recent_attempts": [],
                "by_mechanism_family": {},
                "mutation_authority": False,
            },
        )
        skill_feedback["lineage_binding"] = (
            "current_run_direct_ancestor_outcomes_advisory_only"
        )
        skill_feedback["lineage_attempt_count"] = len(attempts)
        skill_feedback["recent_lineage_attempts"] = attempts[-3:]
        by_family: dict[str, list[dict]] = {}
        for attempt in attempts:
            family = str(attempt.get("mechanism_family") or "")
            if family:
                by_family.setdefault(family, []).append(attempt)
        family_feedback = skill_feedback.setdefault("by_mechanism_family", {})
        for family, family_attempts in by_family.items():
            item = family_feedback.setdefault(
                family,
                {
                    "binding": "exact_seed_plus_direct_ancestor_advisory",
                    "seed_artifact_sha256": seed_artifact_sha256,
                    "attempt_count": 0,
                    "recent_attempts": [],
                    "mutation_authority": False,
                },
            )
            item["lineage_binding"] = (
                "current_run_direct_ancestor_outcomes_advisory_only"
            )
            item["lineage_attempt_count"] = len(family_attempts)
            item["recent_lineage_attempts"] = family_attempts[-3:]

    if nearest_positive_path_local is not None and (
        nearest_phys_opt_distance is None
        or nearest_positive_path_local[0] < nearest_phys_opt_distance
    ):
        phys_feedback = feedback.setdefault(
            "phys_opt",
            {
                "binding": "exact_seed_plus_direct_ancestor_advisory",
                "seed_artifact_sha256": seed_artifact_sha256,
                "attempt_count": 0,
                "recent_attempts": [],
                "by_mechanism_family": {},
                "mutation_authority": False,
            },
        )
        phys_feedback["post_topology_consolidation_delta_fmax_mhz"] = (
            nearest_positive_path_local[1]
        )
        phys_feedback["post_topology_consolidation_lineage_distance"] = (
            nearest_positive_path_local[0]
        )

    local_family_count = len(attempted_local_families)
    if local_family_count >= 2:
        fresh_feedback = feedback.setdefault(
            "fresh_place_route",
            {
                "binding": "exact_seed_plus_direct_ancestor_advisory",
                "seed_artifact_sha256": seed_artifact_sha256,
                "attempt_count": 0,
                "recent_attempts": [],
                "by_mechanism_family": {},
                "mutation_authority": False,
            },
        )
        fresh_feedback["broad_search_escalation_bonus"] = min(
            45.0, 15.0 * float(local_family_count - 1)
        )
        fresh_feedback["attempted_local_mechanism_family_count"] = (
            local_family_count
        )
    return feedback


def exhausted_same_seed_mechanism_families(
    memory: Memory,
    *,
    seed_artifact_sha256: str,
    max_negative_attempts: int = 2,
) -> set[str]:
    """Return families with enough distinct measured non-positive attempts."""
    if max_negative_attempts < 1:
        return set()
    grouped: dict[str, dict[str, object]] = {}
    for transaction in memory.action_transactions:
        proof = transaction.get("outcome_proof") if isinstance(transaction, dict) else None
        if (
            not isinstance(proof, dict)
            or proof.get("kind") != "action_outcome_proof"
            or proof.get("seed_artifact_sha256") != seed_artifact_sha256
            or (proof.get("execution") or {}).get("action_executed") is not True
        ):
            continue
        family = str(proof.get("mechanism_family") or "")
        fingerprint = str(proof.get("action_fingerprint") or "")
        if not family or not fingerprint:
            continue
        state = grouped.setdefault(family, {"fingerprints": set(), "positive": False})
        state["fingerprints"].add(fingerprint)
        for outcome in proof.get("outcomes") or []:
            if (
                isinstance(outcome, dict)
                and outcome.get("legal") is True
                and isinstance(outcome.get("delta_fmax_mhz"), (int, float))
                and not isinstance(outcome.get("delta_fmax_mhz"), bool)
                and float(outcome["delta_fmax_mhz"]) > 0.0
            ):
                state["positive"] = True
    return {
        family
        for family, state in grouped.items()
        if state["positive"] is False
        and len(state["fingerprints"]) >= max_negative_attempts
    }


def exhausted_run_wide_mechanism_families(
    memory: Memory,
    *,
    max_negative_attempts: int = 2,
    max_spent_seeds: int = 2,
    severe_regression_ratio: float = 0.05,
) -> set[str]:
    """Return families this run has already measured to be a dead end.

    The same-seed rule resets at every promotion, so a family that measures
    nothing keeps being retried from each new incumbent.  vexriscv_re-place_v2
    reached its final seed with 600s left and spent it on four families that
    had already measured zero or worse earlier in the same run.

    One seed is not enough to condemn a family on a flat result:
    post_route_physical measured 0.000 twice on that run's first seed and then
    +1.006 on its third, which is a megahertz this rule must not cost.  So a
    family that has only ever measured nothing needs two distinct seeds to
    spend before it is done.

    Measuring nothing and tearing the design apart are different facts, though.
    path_local_logic merged the same two of eighteen LUTs twice and cost 10.5%
    of Fmax both times, the second with 298s left in the run; every other
    non-positive family in that run measured within 1.6% of flat.  One
    measurement that far below the incumbent is enough.
    """
    if max_negative_attempts < 1 or max_spent_seeds < 1:
        return set()
    grouped: dict[str, dict[str, object]] = {}
    for transaction in memory.action_transactions:
        proof = transaction.get("outcome_proof") if isinstance(transaction, dict) else None
        if (
            not isinstance(proof, dict)
            or proof.get("kind") != "action_outcome_proof"
            or (proof.get("execution") or {}).get("action_executed") is not True
        ):
            continue
        family = str(proof.get("mechanism_family") or "")
        fingerprint = str(proof.get("action_fingerprint") or "")
        seed = str(proof.get("seed_artifact_sha256") or "")
        if not family or not fingerprint or not seed:
            continue
        state = grouped.setdefault(
            family, {"seeds": set(), "positive": False, "best_ratio": None}
        )
        state["seeds"].add(seed)
        for outcome in proof.get("outcomes") or []:
            if (
                not isinstance(outcome, dict)
                or outcome.get("legal") is not True
                or not isinstance(outcome.get("delta_fmax_mhz"), (int, float))
                or isinstance(outcome.get("delta_fmax_mhz"), bool)
            ):
                continue
            delta = float(outcome["delta_fmax_mhz"])
            if delta > 0.0:
                state["positive"] = True
            measured = outcome.get("measured_fmax_mhz")
            if (
                isinstance(measured, (int, float))
                and not isinstance(measured, bool)
                and float(measured) - delta > 0.0
            ):
                ratio = delta / (float(measured) - delta)
                if state["best_ratio"] is None or ratio > state["best_ratio"]:
                    state["best_ratio"] = ratio
    exhausted = set()
    for family, state in grouped.items():
        if state["positive"]:
            continue
        best_ratio = state["best_ratio"]
        if (
            len(state["seeds"]) >= max_spent_seeds
            or (
                best_ratio is not None
                and best_ratio <= -abs(severe_regression_ratio)
            )
        ):
            exhausted.add(family)
    return exhausted


def calibrated_same_run_runtime_estimates(
    memory: Memory,
    configured: Mapping[str, object],
) -> tuple[dict[str, float], float | None]:
    """Calibrate scheduler costs from completed actions in this cold run only.

    Only centrally measured, committed cycles are samples. Quick schema
    refusals and failed launches must not make a later real implementation look
    artificially cheap. The 1.5x observed upper envelope absorbs ordinary VM
    variation while replacing stale order-of-magnitude defaults.
    """
    estimates: dict[str, float] = {}
    for skill, value in configured.items():
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0.0
        ):
            estimates[str(skill)] = float(value)

    action_samples: dict[str, list[float]] = {}
    validation_samples: list[float] = []
    for transaction in memory.action_transactions:
        if (
            not isinstance(transaction, dict)
            or transaction.get("terminal_status") != "committed"
        ):
            continue
        execution = transaction.get("execution")
        proof_execution = (transaction.get("outcome_proof") or {}).get("execution")
        if not isinstance(execution, dict) or execution.get("action_executed") is not True:
            continue
        runtime = execution.get("action_runtime_s")
        if (
            isinstance(runtime, (int, float))
            and not isinstance(runtime, bool)
            and math.isfinite(float(runtime))
            and float(runtime) > 0.0
        ):
            action_samples.setdefault(str(transaction.get("skill") or ""), []).append(
                float(runtime)
            )
        if isinstance(proof_execution, dict):
            validation = proof_execution.get("validation_runtime_s")
            if (
                isinstance(validation, (int, float))
                and not isinstance(validation, bool)
                and math.isfinite(float(validation))
                and float(validation) > 0.0
            ):
                validation_samples.append(float(validation))

    for skill, samples in action_samples.items():
        if skill:
            estimates[skill] = max(60.0, 1.5 * max(samples))
    measurement_estimate = (
        max(15.0, 1.25 * max(validation_samples))
        if validation_samples else None
    )
    return estimates, measurement_estimate


def measured_same_run_profile_reserve(memory: Memory) -> float:
    """Return the observed upper envelope of current-run seed profiling.

    A promoted action does not end at central measurement: the next decision
    requires a fresh profile of the exact descendant.  Reserve that measured
    continuation cost when protecting an expiring action launch.  Only timings
    recorded inside this Memory are consumed; no configured benchmark cost or
    historical run estimate enters scheduling.
    """
    values: list[float] = []
    for telemetry in [memory.current_telemetry, *memory.telemetry_epochs]:
        if not isinstance(telemetry, Mapping):
            continue
        runtime = telemetry.get("profile_runtime_s")
        if (
            isinstance(runtime, bool)
            or not isinstance(runtime, (int, float))
            or not math.isfinite(float(runtime))
            or float(runtime) < 0.0
        ):
            continue
        values.append(float(runtime))
    return max(values, default=0.0)


def highest_ranked_continuation_reserve(
    memory: Memory,
    *,
    excluding_skill: str,
    runtime_estimates_s: Mapping[str, object],
    central_measurement_estimate_s: float,
    candidate_commit_reserve_s: float,
) -> float:
    """Cost of the strongest other launchable current-seed mutation.

    The rank and cost come from the already-built exact-seed menu and current
    run scheduler calibration.  This value is only a wall-clock reservation;
    it grants no mutation authority and carries no action parameters forward.
    """
    menu = memory.current_telemetry.get("authorized_action_menu")
    if not isinstance(menu, Mapping):
        return 0.0
    ranked: list[tuple[float, Mapping[str, object], Mapping[str, object]]] = []
    for entry in menu.get("entries", []):
        if not isinstance(entry, Mapping) or entry.get("eligible") is not True:
            continue
        action = entry.get("action")
        display = entry.get("display")
        if (
            not isinstance(action, Mapping)
            or action.get("action") != "run_skill"
            or str(action.get("skill") or "") == excluding_skill
            or not isinstance(display, Mapping)
        ):
            continue
        raw_score = display.get("ranking_score", 0.0)
        score = (
            float(raw_score)
            if isinstance(raw_score, (int, float))
            and not isinstance(raw_score, bool)
            and math.isfinite(float(raw_score))
            else 0.0
        )
        ranked.append((score, action, display))
    if not ranked:
        return 0.0
    _score, action, display = max(ranked, key=lambda item: item[0])
    requirement = action_execution_window_requirement(
        action=action,
        display=display,
        runtime_estimates_s=runtime_estimates_s,
        central_measurement_estimate_s=central_measurement_estimate_s,
        candidate_commit_reserve_s=candidate_commit_reserve_s,
        measured_runtimes_s=_ACTION_COST_OBSERVED,
        measured_full_implementation_s=_IMPLEMENTATION_COST_OBSERVED.get("worst_s"),
    )
    return float((requirement or {}).get("required_execution_window_s") or 0.0)


def build_epoch_auxiliary_actions(
    cfg: Config,
    *,
    seed_sha256: str,
    run_id: str = "",
    telemetry_epoch: int = 0,
    cold_isolation: bool = False,
    current_evidence: Optional[dict] = None,
) -> list[dict]:
    """Build current-seed hypotheses, parameterizations and probe cards."""
    auxiliary_actions: list[dict] = []
    context = dict(current_evidence or {})
    for server, tools in registered_zero_argument_current_design_probes(cfg).items():
        for tool in tools:
            if probe_fact_already_available(tool, context):
                continue
            auxiliary_actions.append({
                "kind": "probe",
                "action_target": f"{server}:{tool}",
                "legality_digest": object_digest({
                    "authority": "configured_registered_read_only_probe",
                    "seed_artifact_sha256": seed_sha256,
                    "server": server,
                    "tool": tool,
                    "arguments": {},
                }),
                "action": {
                    "action": "probe", "server": server, "tool": tool,
                    "arguments": {},
                },
                "display": probe_decision_display(tool),
            })
    if run_id:
        same_run_feedback_by_skill = context.get("_same_run_feedback_by_skill")
        if not isinstance(same_run_feedback_by_skill, dict):
            same_run_feedback_by_skill = {}
        features = _current_seed_action_features(context)
        suggested_targets = {
            "fanout_opt": [
                str(name)
                for name, fanout, _paths in context.get("fanout_nets") or []
                if int(fanout) >= 2
                and not is_unsuitable_fanout_opt_net_name(str(name))
                and (
                    (context.get("fanout_capabilities") or {}).get(str(name))
                    or {}
                ).get("rapidwright_fanout") == "supported"
            ][:10],
        }
        targets = registered_action_targets_by_skill(
            suggested_targets,
            cold_isolation=cold_isolation,
        )
        cards, _audit = build_current_seed_action_cards(
            run_id=run_id,
            seed_artifact_sha256=seed_sha256,
            telemetry_epoch=telemetry_epoch,
            features=features,
            targets_by_skill=targets,
            cold_isolation=cold_isolation,
            same_run_feedback_by_skill=same_run_feedback_by_skill,
        )
        auxiliary_actions.extend(cards)
        return auxiliary_actions

    # Compatibility path for non-provenance legacy runs. Cold no-replay runs
    # always use the current-seed contract path above.
    catalogs = {
        "phys_opt": phys_opt_catalog(),
        "implementation_recipe": implementation_recipe_catalog(),
    }
    selected_targets = {
        "phys_opt": _evidence_gated_phys_opt_targets(current_evidence),
        "implementation_recipe": _evidence_gated_recipe_targets(current_evidence),
    }
    for skill, targets in selected_targets.items():
        for target in targets:
            metadata = catalogs[skill][target]
            scheduling = metadata.get("scheduling") or {}
            regimes = (
                contract_for(skill, target).regimes
                if skill == "implementation_recipe" else (None,)
            )
            features = (
                _current_seed_action_features(context)
                if skill == "implementation_recipe" else None
            )
            for regime in regimes:
                action_params = {
                    key: copy.deepcopy(value)
                    for key, value in scheduling.items()
                    if key in {"min_remaining_s", "min_candidate_remaining_s"}
                    or key.endswith("_timeout_s")
                }
                if skill == "implementation_recipe":
                    action_params.update(
                        derive_implementation_recipe_params(
                            target,
                            regime=str(regime),
                            features=features or {},
                        )
                    )
                try:
                    resolved = resolve_skill_action(
                        skill,
                        target,
                        action_params,
                        defaults={},
                        target_presets={},
                        cold_isolation=cold_isolation,
                    )
                except (CapabilityError, KeyError, TypeError, ValueError) as exc:
                    logger.info(
                        "[agent] legacy auxiliary action blocked skill=%s "
                        "target=%s regime=%s: %s",
                        skill, target, regime, exc,
                    )
                    continue
                resolved_params = copy.deepcopy(resolved.resolved_params)
                action_target = f"{skill}:{target}"
                if regime is not None:
                    action_target = f"{action_target}:{regime}"
                timeout_s = float(
                    resolved_params.get("recipe_timeout_s")
                    or metadata.get("timeout_s")
                    or 0.0
                )
                auxiliary_actions.append({
                    "kind": "physical_recipe",
                    "action_target": action_target,
                    "legality_digest": object_digest({
                        "authority": "registered_physical_recipe_prevalidation",
                        "seed_artifact_sha256": seed_sha256,
                        "skill": skill,
                        "target": target,
                        "regime": regime,
                        "resolved_action_fingerprint": resolved.action_fingerprint,
                        "params": resolved_params,
                    }),
                    "action": {
                        "action": "run_skill", "skill": skill, "target": target,
                        "params": resolved_params,
                    },
                    "display": {
                        "expected_mechanism": metadata["mechanism_summary"],
                        "backend": metadata.get("backend", skill),
                        "destructive_scope": metadata["destructive_scope"],
                        "parameter_regime": regime,
                        "timeout_s": timeout_s,
                        "required_execution_window_s": float(
                            resolved_params.get("min_remaining_s", 0.0)
                        ),
                    },
                })
    return auxiliary_actions


async def recover_action_menu_product_sum_preflight(
    mcp: MCPClient,
    cfg: Config,
    *,
    incumbent: dict,
    critical_paths: list,
    output_dir: Path,
    top_k: int,
    run_id: str,
    retry_prior_result: Optional[dict] = None,
) -> dict:
    """Invoke the optimized G2 cold provider directly with its bounded budgets."""
    return await recover_cold_product_sum_families(
        mcp,
        input_dcp=Path(str(incumbent["artifact_path"])),
        source_artifact_sha256=str(incumbent["artifact_sha256"]),
        run_id=run_id,
        critical_paths=critical_paths,
        output_dir=output_dir,
        max_families=top_k,
        max_candidates=top_k,
        preflight_budget_s=float(
            cfg.get("analysis.registered_product_sum_preflight_budget_s", 300.0)
        ),
        family_deadline_s=float(
            cfg.get("analysis.registered_product_sum_family_deadline_s", 240.0)
        ),
        graph_candidate_id=str(incumbent["candidate_id"]),
        retry_prior_result=retry_prior_result,
    )


def action_menu_retry_required(
    status: object, *, max_retries: int, current_round: int = 0
) -> bool:
    """Return whether a failed family slice owns one due bounded rebuild."""
    if not isinstance(status, dict) or status.get("state") not in {
        "partial", "degraded"
    }:
        return False
    attempts = status.get("retry_count", 0)
    eligible_round = status.get("retry_not_before_round", 0)
    return (
        isinstance(attempts, int)
        and not isinstance(attempts, bool)
        and 0 <= attempts < max(0, int(max_retries))
        and isinstance(eligible_round, int)
        and not isinstance(eligible_round, bool)
        and int(current_round) >= eligible_round
    )


async def retry_action_menu_product_sum_family_slice(
    mcp: MCPClient,
    cfg: Config,
    *,
    incumbent: dict,
    current_telemetry: Mapping[str, object],
    run_id: str,
    cold_isolation: bool,
) -> dict[str, object]:
    """Retry only failed product-sum closures on one exact current seed."""
    source_sha = str(incumbent.get("artifact_sha256") or "")
    packet = validate_registered_arithmetic_decision_packet(
        current_telemetry.get("registered_arithmetic_decision_packet"),
        expected_design_sha256=source_sha,
    )
    existing_menu = validate_action_menu(
        current_telemetry.get("authorized_action_menu"),
        expected_seed_sha256=source_sha,
        cold_isolation=cold_isolation,
    )
    status = current_telemetry.get("action_menu_build_status")
    retry_count = (
        int(status.get("retry_count", 0))
        if isinstance(status, Mapping) else 0
    )
    if packet is None:
        raise RuntimeError("family-slice retry has no exact-seed decision packet")

    contest_clock = str(cfg.get("legality.contest_clock", "clk_fpl26contest"))
    num_paths = max(1, min(16, int(cfg.get("analysis.profile_num_paths", 16))))
    paths_raw = await mcp.call_vivado(
        "extract_critical_path_cells",
        {"num_paths": num_paths, "clock": contest_clock},
        timeout=600.0,
    )
    paths = json.loads(paths_raw)
    validate_critical_path_cells(paths)
    capture_probe_response(mcp, "extract_critical_path_cells", paths_raw, incumbent)

    preflight_dir = (
        Path(mcp.run_dir) / f"cold_product_sum_preflight_{source_sha[:12]}"
    )
    base_artifact = preflight_dir / "cold_recovery.json"
    if not base_artifact.is_file() or base_artifact.is_symlink():
        raise RuntimeError("family-slice retry has no source-SHA cold cache")
    prior_cold = json.loads(base_artifact.read_text(encoding="utf-8"))
    cached_run_id = str(prior_cold.get("run_id") or "")
    if run_id and cached_run_id != run_id:
        raise RuntimeError("family-slice retry run binding changed")
    if not cached_run_id:
        raise RuntimeError("family-slice retry cache has no run binding")
    retry_dir = preflight_dir / f"retry_{retry_count + 1:03d}"
    if retry_dir.exists() or retry_dir.is_symlink():
        raise RuntimeError("family-slice retry output already exists")

    cold = await recover_action_menu_product_sum_preflight(
        mcp,
        cfg,
        incumbent=incumbent,
        critical_paths=paths,
        output_dir=retry_dir,
        top_k=int(cfg.get("action_menu.top_k", 4)),
        run_id=cached_run_id,
        retry_prior_result=prior_cold,
    )
    atomic_write_json(preflight_dir / "cold_recovery_retry_latest.json", cold)
    atomic_write_json(base_artifact, cold)

    screened_product_sum = [
        dict(item["screened_family"])
        for item in cold.get("candidates", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("screened_family"), Mapping)
    ]
    screened_product_sum.extend(
        dict(item)
        for item in cold.get("unavailable_families", [])
        if isinstance(item, Mapping)
    )
    refreshed_packet = refresh_registered_arithmetic_product_sum_slice(
        packet,
        expected_design_sha256=source_sha,
        screened_product_sum_families=screened_product_sum,
    )
    if refreshed_packet is None:
        raise RuntimeError("family-slice retry produced an invalid decision packet")

    proof_bindings = {
        str(item.get("family_fingerprint") or ""): dict(
            item.get("preflight_receipt") or {}
        )
        for item in cold.get("candidates", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("preflight_receipt"), Mapping)
    }
    recovery_error = None
    if cold.get("preflight_complete") is not True:
        blockers = sorted({
            str(
                (item.get("typed_rejection") or {}).get("reason_code")
                or "COLD_PREFLIGHT_INCOMPLETE"
            )
            for item in cold.get("unavailable_families", [])
            if isinstance(item, Mapping)
        })
        recovery_error = ActionMenuError(
            "ACTION_MENU_PREFLIGHT_INCOMPLETE",
            ",".join(blockers) or "bounded cold recovery did not complete",
        )
    error_code = (
        _action_menu_failure_code(recovery_error)
        if recovery_error is not None else None
    )
    rebuilt = build_recovery_action_menu(
        seed_candidate_id=str(incumbent["candidate_id"]),
        seed_artifact_sha256=source_sha,
        registered_arithmetic_packet=refreshed_packet,
        proof_bindings=proof_bindings,
        top_k=int(cfg.get("action_menu.top_k", 4)),
        recovery_error_code=error_code,
        recovery_error_detail=str(recovery_error or ""),
        cold_isolation=cold_isolation,
        apply_adaptive_limit=False,
    )
    merged_menu = replace_action_menu_family_slice(
        existing_menu,
        rebuilt["menu"],
        family_kind="product_sum",
    )
    return {
        "registered_arithmetic_decision_packet": refreshed_packet,
        "authorized_action_menu": merged_menu,
        "action_menu_build_status": {
            "schema_version": "action-menu-build-status-v1",
            "state": rebuilt["build_state"],
            "error_code": rebuilt["error_code"],
            "retry_count": retry_count + 1,
            "retry_not_before_round": int(current_telemetry.get("source_round") or 0),
            "provider_entry": "recover_cold_product_sum_families",
            "seed_artifact_sha256": source_sha,
            "retry_scope": "failed_product_sum_family_slices_only",
        },
        "action_menu_timing": {
            "schema_version": "action-menu-epoch-cost-v1",
            "provider_entry": "recover_cold_product_sum_families",
            "top_k": int(cfg.get("action_menu.top_k", 4)),
            "retry_scope": "failed_product_sum_family_slices_only",
            **copy.deepcopy(cold.get("timing") or {}),
        },
    }


# A structural refusal is a property of one exact artifact, so it is keyed by
# that artifact's SHA-256 and never shared across seeds. Re-proving the same
# absence on the same bytes buys nothing and spends the optimization window:
# one measured hour spent 384s on six consumer-contract probes of which four
# were repeats of an already refused family.
_SEED_NON_APPLICABILITY: dict[tuple[str, str, str], str] = {}


def remember_structural_non_applicability(
    seed_artifact_sha256: str, tool: str, subject: str, reason: object
) -> bool:
    """Record that a mechanism does not apply to this exact artifact.

    Only a structural refusal is retained. A timeout or backend failure says
    nothing about applicability and must stay retryable.
    """
    if not seed_artifact_sha256 or not tool or not subject:
        return False
    structural = getattr(reason, "structural_non_applicability", None)
    if structural is None:
        structural = is_structural_non_applicability(reason)
    if not structural:
        return False
    _SEED_NON_APPLICABILITY[
        (str(seed_artifact_sha256), str(tool), str(subject))
    ] = str(reason)[:500]
    return True


def recall_structural_non_applicability(
    seed_artifact_sha256: str, tool: str, subject: str
) -> Optional[str]:
    """Return the recorded refusal for this exact artifact, if any."""
    if not seed_artifact_sha256 or not tool or not subject:
        return None
    return _SEED_NON_APPLICABILITY.get(
        (str(seed_artifact_sha256), str(tool), str(subject))
    )


def reset_structural_non_applicability() -> None:
    """Clear the per-process record. Used by tests and by a fresh run."""
    _SEED_NON_APPLICABILITY.clear()


# Probe cost is not a property of the probe name. The same contract recovery
# measured 50s, then 106s, then 352s as the run promoted descendants, because
# the structure it must walk grows with the rebuilt design. A fixed per-name
# estimate therefore spends a late window on evidence that can no longer be
# acted on: one measured hour spent 352s recovering a contract with 1168s left,
# where the mutation that contract authorizes had already cost 913s.
_PROBE_COST_OBSERVED: dict[str, float] = {}
_ACTION_COST_OBSERVED: dict[str, float] = {}
_IMPLEMENTATION_COST_OBSERVED: dict[str, float] = {}
_OPTIMIZATION_BUDGET: dict[str, float] = {}


def note_probe_cost(tool: str, seconds: float) -> None:
    """Record the worst cost this probe has shown in this run."""
    if not tool or not seconds or seconds <= 0:
        return
    _PROBE_COST_OBSERVED[str(tool)] = max(
        _PROBE_COST_OBSERVED.get(str(tool), 0.0), float(seconds)
    )


def note_action_cost(
    skill: str, target: object, seconds: float, *, destructive_scope: object = None
) -> None:
    """Record the worst execution cost this exact mutation has shown.

    Cost is keyed by skill and target together.  One skill spans mechanisms
    whose measured runtimes differ by an order of magnitude, so a per-skill
    record describes none of them.
    """
    if not skill or not seconds or seconds <= 0:
        return
    key = action_cost_key(skill, target)
    _ACTION_COST_OBSERVED[key] = max(
        _ACTION_COST_OBSERVED.get(key, 0.0), float(seconds)
    )
    if str(destructive_scope or "") in REIMPLEMENTING_DESTRUCTIVE_SCOPES:
        _IMPLEMENTATION_COST_OBSERVED["worst_s"] = max(
            _IMPLEMENTATION_COST_OBSERVED.get("worst_s", 0.0), float(seconds)
        )


def measured_action_runtimes_s() -> dict[str, float]:
    """Return this run's measured worst mutation cost per exact action."""
    return dict(_ACTION_COST_OBSERVED)


def measured_full_implementation_s() -> Optional[float]:
    """Return the worst complete implementation pass measured in this run."""
    return _IMPLEMENTATION_COST_OBSERVED.get("worst_s")


def set_optimization_budget(remaining_s: Optional[float]) -> None:
    """Publish the optimization window left, measured by the caller."""
    if remaining_s is None:
        _OPTIMIZATION_BUDGET.pop("remaining_s", None)
        return
    _OPTIMIZATION_BUDGET["remaining_s"] = max(0.0, float(remaining_s))


def reset_probe_budget_state() -> None:
    """Clear the per-process cost record. Used by tests and by a fresh run."""
    _PROBE_COST_OBSERVED.clear()
    _ACTION_COST_OBSERVED.clear()
    _IMPLEMENTATION_COST_OBSERVED.clear()
    _OPTIMIZATION_BUDGET.clear()


def probe_budget_refusal(tool: str, *, authorized_family: str) -> Optional[str]:
    """Return why this probe cannot pay for itself, or None to proceed.

    Recovering a contract is only worth its cost while the window can still
    fit the mutation that contract would authorize. Both sides of that
    comparison come from this run's own measurements, so a first probe is
    never blocked and an unmeasured family never blocks anything.
    """
    remaining = _OPTIMIZATION_BUDGET.get("remaining_s")
    if remaining is None:
        return None
    probe_cost = _PROBE_COST_OBSERVED.get(str(tool))
    action_cost = max(
        (
            seconds
            for key, seconds in _ACTION_COST_OBSERVED.items()
            if key.split(":", 1)[0] == str(authorized_family)
        ),
        default=None,
    )
    if probe_cost is None or action_cost is None:
        return None
    needed = probe_cost + action_cost
    if remaining >= needed:
        return None
    return (
        f"probe {probe_cost:.0f}s plus its authorized {authorized_family} "
        f"mutation {action_cost:.0f}s exceeds the {remaining:.0f}s left"
    )


async def collect_operator_mining_evidence(
    mcp: MCPClient,
    cfg: Config,
    *,
    incumbent: dict,
    retry_failed_families: bool = False,
    cold_isolation: bool = False,
    run_id: str = "",
    telemetry_epoch: int = 0,
    menu_context: Optional[dict] = None,
    design_lut_count: object = None,
) -> OperatorMiningEvidence:
    """Mine one exact incumbent, returning only numeric and public projections."""
    census_started = time.monotonic()
    contest_clock = str(cfg.get("legality.contest_clock", "clk_fpl26contest"))
    action_menu_top_k = int(cfg.get("action_menu.top_k", 4))
    num_paths = max(1, min(16, int(cfg.get("analysis.profile_num_paths", 16))))
    paths_raw = await mcp.call_vivado(
        "extract_critical_path_pins",
        {"num_paths": num_paths, "clock": contest_clock},
        timeout=600.0,
    )
    paths = json.loads(paths_raw)
    validate_critical_path_cells(paths)
    capture_probe_response(
        mcp, "extract_critical_path_pins", paths_raw, incumbent
    )
    mining_paths = compact_critical_paths_for_operator_mining(paths)
    mined_raw = await mcp.call_exact_rapidwright_probe(
        "operator_mining",
        {
            "critical_paths_data": mining_paths,
            "min_family_size": 3,
            "max_families": 16,
            "max_motif_cells": 8,
        },
        candidate_id=str(incumbent["candidate_id"]),
        artifact_path=Path(str(incumbent["artifact_path"])),
        artifact_sha256=str(incumbent["artifact_sha256"]),
        timeout=600.0,
    )
    mined = json.loads(mined_raw)
    validated_root = validate_operator_mining_root(
        mined,
        expected_design_sha256=str(incumbent.get("artifact_sha256") or ""),
    )
    recurrence = validate_recurrence_boundary_hypotheses(
        mined,
        expected_design_sha256=str(incumbent.get("artifact_sha256") or ""),
        max_families=16,
    )
    if validated_root is None or recurrence is None:
        raise ValueError("operator mining was not bound to the exact incumbent")
    public_mining = json.loads(capture_probe_response(
        mcp, "operator_mining", mined_raw, incumbent
    ))

    numeric: dict[str, object] = {
        "recurrence_eligible_count": len(recurrence)
    }
    feedback_by_skill = (
        menu_context.get("_same_run_feedback_by_skill")
        if isinstance(menu_context, dict) else None
    )
    operator_feedback = (
        feedback_by_skill.get("operator_rewrite")
        if isinstance(feedback_by_skill, dict) else None
    )
    if isinstance(operator_feedback, dict):
        family_feedback = operator_feedback.get("by_mechanism_family")
        if isinstance(family_feedback, dict) and isinstance(
            family_feedback.get("operator_rewrite"), dict
        ):
            operator_feedback = family_feedback["operator_rewrite"]
    if not isinstance(operator_feedback, dict):
        operator_feedback = {}
    deep_operator_cards: list[dict[str, object]] = []
    metaloop_report: str | None = None
    cold: Optional[dict] = None
    try:
        metaloop_report = await capture_live_timing_report(
            mcp, clock=contest_clock, top_n=300
        )
        numeric.update(metaloop_hypothesis_counts(metaloop_report, limit=300))
        numeric.update(metaloop_live_wall_counts(metaloop_report, limit=300))
    except Exception as exc:  # anonymous read-only hypotheses fail to zero
        logger.info("[agent] meta-loop action preflight unavailable: %s", exc)
        metaloop_report = None
        numeric["constructive_accumulator_hypothesis_count"] = 0
        numeric["memory_write_buffer_forward_hypothesis_count"] = 0
        numeric["constructive_accumulator_live_wall_covered_path_count"] = 0
        numeric["memory_write_buffer_forward_live_wall_covered_path_count"] = 0
    for prefix, blocker in (
        (
            "constructive_accumulator",
            "CACC_SIBLING_FAMILY_INCOMPLETE",
        ),
        (
            "memory_write_buffer_forward",
            "MEMFWD_CONSUMER_CLOSURE_INCOMPLETE",
        ),
    ):
        shape_count = int(numeric.get(f"{prefix}_hypothesis_count", 0) or 0)
        numeric[f"{prefix}_complete_contract_count"] = 0
        numeric[f"{prefix}_missing_obligation_count"] = int(shape_count > 0)
        numeric[f"{prefix}_nomination_blockers"] = (
            [blocker] if shape_count > 0 else []
        )
    raw_fixed_point = validated_root.get("fixed_point_product_hypothesis_count")
    if (
        not isinstance(raw_fixed_point, bool)
        and isinstance(raw_fixed_point, int)
        and 0 <= raw_fixed_point <= 64
    ):
        numeric["fixed_point_product_hypothesis_count"] = raw_fixed_point
    else:
        numeric["fixed_point_product_hypothesis_count"] = 0
    raw_two_operand = validated_root.get("two_operand_product_hypothesis_count")
    if (
        not isinstance(raw_two_operand, bool)
        and isinstance(raw_two_operand, int)
        and 0 <= raw_two_operand <= 64
    ):
        numeric["two_operand_product_hypothesis_count"] = raw_two_operand
    else:
        numeric["two_operand_product_hypothesis_count"] = 0
    raw_register_absorb = validated_root.get("register_absorb_hypothesis_count")
    if (
        not isinstance(raw_register_absorb, bool)
        and isinstance(raw_register_absorb, int)
        and 0 <= raw_register_absorb <= 64
    ):
        numeric["register_absorb_hypothesis_count"] = raw_register_absorb
    else:
        numeric["register_absorb_hypothesis_count"] = 0
    raw_pipeline_rebalance = validated_root.get(
        "pipeline_rebalance_hypothesis_count"
    )
    if (
        not isinstance(raw_pipeline_rebalance, bool)
        and isinstance(raw_pipeline_rebalance, int)
        and 0 <= raw_pipeline_rebalance <= 64
    ):
        numeric["pipeline_rebalance_hypothesis_count"] = raw_pipeline_rebalance
    else:
        numeric["pipeline_rebalance_hypothesis_count"] = 0
    raw_control_state_hint = validated_root.get(
        "control_to_data_state_equation_live_hint_count"
    )
    if (
        not isinstance(raw_control_state_hint, bool)
        and isinstance(raw_control_state_hint, int)
        and 0 <= raw_control_state_hint <= 64
    ):
        numeric["control_to_data_state_equation_live_hint_count"] = (
            raw_control_state_hint
        )
    else:
        numeric["control_to_data_state_equation_live_hint_count"] = 0
    # Only the exact current-seed recovery below may turn the cheap live hint
    # into a public hypothesis count.
    numeric["control_to_data_state_equation_hypothesis_count"] = 0
    public_motif = build_repeated_arithmetic_motif_report(validated_root)
    if public_motif is not None:
        families = public_motif["sequence_families"]
        numeric["repeated_arithmetic_family_count"] = len(families)
        similarities = [
            float(family[key])
            for family in families
            for key in (
                "nearest_continuous_similarity",
                "population_continuous_similarity",
            )
            if family[key] != "unknown"
        ]
        if similarities:
            numeric["repeated_arithmetic_similarity_max"] = max(similarities)
        numeric["clock_as_data_path_risk_count"] = sum(
            "clock_as_data_path" in family["risk_flags"] for family in families
        )
    deep_recovery, deep_recovery_reasons = (
        operator_census_warrants_deep_recovery(
            numeric, public_motif, recurrence
        )
    )
    numeric["operator_census_deep_recovery_warranted"] = deep_recovery
    numeric["operator_census_deep_recovery_reasons"] = list(
        deep_recovery_reasons
    )
    if not deep_recovery:
        action_menu_timing = {
            "schema_version": "action-menu-epoch-cost-v1",
            "provider_entry": "exact_seed_operator_census",
            "census_elapsed_s": round(time.monotonic() - census_started, 6),
            "deep_recovery_executed": False,
        }
        auxiliary_actions = build_epoch_auxiliary_actions(
            cfg,
            seed_sha256=str(incumbent["artifact_sha256"]),
            run_id=run_id,
            telemetry_epoch=telemetry_epoch,
            cold_isolation=cold_isolation,
            current_evidence=menu_context,
        )
        authorized_action_menu = build_action_menu(
            seed_candidate_id=str(incumbent["candidate_id"]),
            seed_artifact_sha256=str(incumbent["artifact_sha256"]),
            registered_arithmetic_packet=None,
            top_k=action_menu_top_k,
            auxiliary_actions=auxiliary_actions,
            cold_isolation=cold_isolation,
            apply_adaptive_limit=False,
        )
        action_menu_build_status = {
            "schema_version": "action-menu-build-status-v1",
            "state": "complete",
            "error_code": None,
            "retry_count": 0,
            "provider_entry": "exact_seed_operator_census",
            "seed_artifact_sha256": str(incumbent["artifact_sha256"]),
            "deep_recovery_warranted": False,
        }
        return OperatorMiningEvidence(
            numeric,
            public_motif,
            None,
            authorized_action_menu,
            action_menu_timing,
            action_menu_build_status,
        )
    numeric["banked_select_predicate_complete_contract_count"] = 0
    numeric["banked_select_predicate_live_wall_covered_path_count"] = 0
    if run_id:
        sequential_families = (
            (public_mining.get("families") or {}).get("sequential_write") or []
        )
        consumer_candidate_families = sorted(
            (
                item for item in sequential_families
                if isinstance(item, Mapping)
                and isinstance(item.get("family_ref"), str)
                and isinstance(item.get("critical_path_coverage"), Mapping)
                and item["critical_path_coverage"].get("covers_worst_path") is True
            ),
            key=lambda item: (
                int(
                    int(
                        item["critical_path_coverage"].get(
                            "endpoint_path_count"
                        ) or 0
                    ) != 0
                ),
                -int(
                    item["critical_path_coverage"].get("covered_path_count") or 0
                ),
                -int(item.get("member_count") or 0),
                str(item["family_ref"]),
            ),
        )
        for family in consumer_candidate_families[:4]:
            family_ref = str(family["family_ref"])
            seed_sha = str(incumbent["artifact_sha256"])
            cached = recall_structural_non_applicability(
                seed_sha, "recover_consumer_driven_operator_contract", family_ref
            )
            if cached is not None:
                logger.info(
                    "[agent] skipping %s: already not applicable on this exact "
                    "seed (%s)",
                    family_ref,
                    cached,
                )
                continue
            refusal = probe_budget_refusal(
                "recover_consumer_driven_operator_contract",
                authorized_family="operator_rewrite",
            )
            if refusal is not None:
                logger.info(
                    "[agent] deferring %s: %s", family_ref, refusal
                )
                continue
            probe_started = time.monotonic()
            try:
                consumer_arguments = prepare_probe_arguments(
                    mcp,
                    "recover_consumer_driven_operator_contract",
                    {
                        "path_source": "latest_critical_paths",
                        "family_ref": family_ref,
                    },
                    incumbent,
                )
                consumer_raw = await mcp.call_exact_rapidwright_probe(
                    "recover_consumer_driven_operator_contract",
                    consumer_arguments,
                    candidate_id=str(incumbent["candidate_id"]),
                    artifact_path=Path(str(incumbent["artifact_path"])),
                    artifact_sha256=str(incumbent["artifact_sha256"]),
                    timeout=300.0,
                )
                _operator_probe_payload(consumer_raw)
                public_consumer = json.loads(capture_probe_response(
                    mcp,
                    "recover_consumer_driven_operator_contract",
                    consumer_raw,
                    incumbent,
                ))
                consumer_card_refusal = {}
                cards = build_consumer_rewrite_probe_action_card(
                    run_id=run_id,
                    seed_artifact_sha256=str(incumbent["artifact_sha256"]),
                    telemetry_epoch=telemetry_epoch,
                    public_probe=public_consumer,
                    cold_isolation=cold_isolation,
                    same_run_feedback=operator_feedback,
                    refusal=consumer_card_refusal,
                    design_lut_count=design_lut_count,
                )
                if not cards and consumer_card_refusal:
                    # A recovery that succeeds and then admits no card leaves
                    # the mechanism absent from the menu with no trace.
                    logger.info(
                        "[agent] consumer rewrite card refused: %s (unmet=%s)",
                        consumer_card_refusal.get("consumer_rewrite_card_refusal"),
                        consumer_card_refusal.get("consumer_rewrite_card_unmet"),
                    )
                if cards:
                    deep_operator_cards.extend(cards)
                    public_contract = public_consumer.get("public_contract") or {}
                    semantic = public_contract.get("semantic_recovery") or {}
                    shell_nodes = int(
                        semantic.get("primitive_shell_node_count") or 0
                    )
                    producer_nodes = sum(
                        int(value or 0)
                        for value in (
                            public_contract.get("producer_primitive_type_counts")
                            or {}
                        ).values()
                    )
                    contraction = (
                        max(0.0, 1.0 - shell_nodes / producer_nodes)
                        if producer_nodes else 0.0
                    )
                    # A strongly contracting exact contract is already a good
                    # semantic candidate.  Otherwise compare more live owners
                    # instead of routing the first merely legal large shell.
                    if contraction >= 0.5:
                        break
            except Exception as exc:
                # A structural refusal is bound to this exact seed. Remember it
                # so a later epoch does not spend the same probe cost proving
                # the same absence again; a promoted descendant has a different
                # artifact SHA and is probed afresh.
                remember_structural_non_applicability(
                    seed_sha,
                    "recover_consumer_driven_operator_contract",
                    family_ref,
                    exc,
                )
                logger.info(
                    "[agent] live consumer-driven discriminator unavailable "
                    "for %s: %s",
                    family_ref,
                    exc,
                )
            finally:
                note_probe_cost(
                    "recover_consumer_driven_operator_contract",
                    time.monotonic() - probe_started,
                )
    if run_id and int(
        numeric.get("control_to_data_state_equation_live_hint_count", 0) or 0
    ) > 0:
        try:
            control_arguments = prepare_probe_arguments(
                mcp,
                "recover_control_to_data_state_equation_contract",
                {},
                incumbent,
            )
            control_raw = await mcp.call_vivado(
                "recover_control_to_data_state_equation_contract",
                control_arguments,
                timeout=1200.0,
            )
            _operator_probe_payload(control_raw)
            public_control = json.loads(capture_probe_response(
                mcp,
                "recover_control_to_data_state_equation_contract",
                control_raw,
                incumbent,
            ))
            control_families = public_control.get("families") or []
            if isinstance(control_families, list):
                numeric[
                    "control_to_data_state_equation_hypothesis_count"
                ] = len(control_families)
            deep_operator_cards.extend(build_operator_probe_action_cards(
                run_id=run_id,
                seed_artifact_sha256=str(incumbent["artifact_sha256"]),
                telemetry_epoch=telemetry_epoch,
                public_probe=public_control,
                cold_isolation=cold_isolation,
                same_run_feedback=operator_feedback,
                design_lut_count=design_lut_count,
            ))
        except Exception as exc:
            logger.info(
                "[agent] live control-to-data state-equation discriminator "
                "unavailable: %s",
                exc,
            )
            numeric["control_to_data_state_equation_hypothesis_count"] = 0
    if (
        homogeneous_repeated_wall_needs_semantic_discriminator(public_motif)
        or repeated_wall_has_memory_endpoint(public_motif)
    ):
        try:
            probe_arguments = prepare_probe_arguments(
                mcp,
                "recover_banked_ram_operator_contract",
                {},
                incumbent,
            )
            banked_payload = _operator_probe_payload(await mcp.call_vivado(
                "recover_banked_ram_operator_contract",
                probe_arguments,
                timeout=1200.0,
            ))
            numeric.update(banked_ram_operator_preflight_facts(banked_payload))
            if (
                run_id
                and int(numeric.get("banked_ram_control_live_path_count", 0) or 0)
                > 0
            ):
                try:
                    predicate_arguments = prepare_probe_arguments(
                        mcp,
                        "recover_banked_select_predicate_contract",
                        {},
                        incumbent,
                    )
                    public_predicate = _operator_probe_payload(
                        await mcp.call_vivado(
                            "recover_banked_select_predicate_contract",
                            predicate_arguments,
                            timeout=1200.0,
                        )
                    )
                    if (
                        public_predicate.get("status") == "success"
                        and public_predicate.get("kind")
                        == "banked_select_predicate_operator_contract"
                        and public_predicate.get("mutation_eligible") is True
                    ):
                        wall = (
                            (public_predicate.get("contract") or {}).get(
                                "live_wall"
                            ) or {}
                        )
                        numeric[
                            "banked_select_predicate_complete_contract_count"
                        ] = 1
                        numeric[
                            "banked_select_predicate_live_wall_covered_path_count"
                        ] = int(wall.get("covered_path_count") or 0)
                except Exception as exc:
                    logger.info(
                        "[agent] live RAM-control bank-local predicate "
                        "discriminator unavailable: %s",
                        exc,
                    )
            if run_id and banked_wall_needs_multilevel_control_recovery(
                banked_payload
            ):
                try:
                    multilevel_arguments = prepare_probe_arguments(
                        mcp,
                        "recover_multilevel_control_distribution_contract",
                        {},
                        incumbent,
                    )
                    multilevel_raw = await mcp.call_vivado(
                        "recover_multilevel_control_distribution_contract",
                        multilevel_arguments,
                        timeout=1200.0,
                    )
                    _operator_probe_payload(multilevel_raw)
                    public_multilevel = json.loads(capture_probe_response(
                        mcp,
                        "recover_multilevel_control_distribution_contract",
                        multilevel_raw,
                        incumbent,
                    ))
                    deep_operator_cards.extend(build_operator_probe_action_cards(
                        run_id=run_id,
                        seed_artifact_sha256=str(incumbent["artifact_sha256"]),
                        telemetry_epoch=telemetry_epoch,
                        public_probe=public_multilevel,
                        cold_isolation=cold_isolation,
                        same_run_feedback=operator_feedback,
                        design_lut_count=design_lut_count,
                    ))
                    families = public_multilevel.get("families") or []
                    if isinstance(families, list):
                        numeric[
                            "multilevel_control_complete_contract_count"
                        ] = len(families)
                        numeric[
                            "multilevel_control_live_wall_covered_path_count"
                        ] = sum(
                            int(item.get("covered_path_count") or 0)
                            for item in families
                            if isinstance(item, dict)
                        )
                except Exception as exc:
                    logger.info(
                        "[agent] live RAM-control multilevel discriminator "
                        "unavailable: %s",
                        exc,
                    )
        except Exception as exc:  # exact current-seed discriminator fails closed
            logger.info(
                "[agent] homogeneous-wall banked semantic discriminator unavailable: %s",
                exc,
            )
            numeric.update(banked_ram_operator_preflight_facts(None))
    fixed_point_facts_set = None
    product_sum_facts_set = None
    recurrence_screened_family = None
    recurrence_proof_binding = None
    try:
        fixed_point_facts_set = _operator_probe_payload(
            await mcp.call_exact_rapidwright_probe(
                "recover_fixed_point_contract",
                {"max_candidates": 8},
                candidate_id=str(incumbent["candidate_id"]),
                artifact_path=Path(str(incumbent["artifact_path"])),
                artifact_sha256=str(incumbent["artifact_sha256"]),
                timeout=600.0,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[agent] exact registered fixed-point screen unavailable: %s", exc)
    numeric["fixed_point_contract_set_executor_ready"] = (
        fixed_point_contract_set_has_executor_authority(
            fixed_point_facts_set,
            expected_source_sha256=str(incumbent["artifact_sha256"]),
        )
    )
    if recurrence:
        try:
            recurrence_facts = _operator_probe_payload(
                await mcp.call_exact_rapidwright_probe(
                    "recover_recurrence_family_contract",
                    {"max_boundaries": 32},
                    candidate_id=str(incumbent["candidate_id"]),
                    artifact_path=Path(str(incumbent["artifact_path"])),
                    artifact_sha256=str(incumbent["artifact_sha256"]),
                    timeout=600.0,
                )
            )
            (
                recurrence_screened_family,
                recurrence_proof_binding,
            ) = screen_recovered_recurrence_transport(
                recurrence_facts,
                critical_paths=paths,
                source_artifact_sha256=str(incumbent["artifact_sha256"]),
            )
            # The mutation rediscovers the same family on the same artifact,
            # which costs a second five-minute window for an identical payload.
            # Retain this one so it can consume the screen's result instead.
            retain_recurrence_family_facts(
                mcp,
                artifact_sha256=str(incumbent["artifact_sha256"]),
                facts=recurrence_facts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "[agent] exact recurrence-transport screen unavailable: %s", exc
            )
    screened_families = screen_recovered_registered_arithmetic(
        fixed_point_facts_set=fixed_point_facts_set,
        product_sum_facts_set=product_sum_facts_set,
        critical_paths=paths,
        source_artifact_sha256=str(incumbent.get("artifact_sha256") or ""),
        product_sum_recovery_error=None,
    )
    if recurrence_screened_family is not None:
        screened_families.append(recurrence_screened_family)
    # Complete-family production recovery is a distinct cold current-DCP
    # screen.  It does not consume retained bundles and emits only the bounded
    # anonymous rows below into the decision packet; exact wiring remains in
    # private run artifacts and must be re-recovered again at execution.
    cold_recovery_error: BaseException | None = None
    try:
        preflight_dir = (
            Path(mcp.run_dir)
            / f"cold_product_sum_preflight_{str(incumbent['artifact_sha256'])[:12]}"
        )
        base_artifact = preflight_dir / "cold_recovery.json"
        latest_retry_artifact = preflight_dir / "cold_recovery_retry_latest.json"
        prefetched_artifact = (
            latest_retry_artifact
            if latest_retry_artifact.is_file() and not retry_failed_families
            else base_artifact
        )
        if deep_operator_cards:
            # A complete, current-seed semantic contract already dominates the
            # live wall.  Do not spend the execution window recovering an
            # unrelated product-sum menu before exposing that action.
            cold = {
                "kind": "cold_registered_product_sum_facts_set",
                "design_sha256": str(incumbent["artifact_sha256"]),
                "run_id": run_id,
                "fresh_rediscovery": True,
                "read_only": True,
                "mutation_eligible": False,
                "preflight_complete": True,
                "candidates": [],
                "unavailable_families": [],
                "timing": {
                    "elapsed_s": 0.0,
                    "skip_reason": "dominating_semantic_contract_available",
                },
            }
        elif retry_failed_families:
            if not prefetched_artifact.is_file() or prefetched_artifact.is_symlink():
                raise RuntimeError("action-menu retry has no source-SHA family cache")
            prior_cold = json.loads(prefetched_artifact.read_text(encoding="utf-8"))
            retry_dir = preflight_dir / "retry_001"
            cold = await recover_action_menu_product_sum_preflight(
                mcp,
                cfg,
                incumbent=incumbent,
                critical_paths=paths,
                output_dir=retry_dir,
                top_k=action_menu_top_k,
                run_id=run_id,
                retry_prior_result=prior_cold,
            )
            latest_retry_artifact.write_text(
                json.dumps(cold, indent=2, sort_keys=True), encoding="utf-8"
            )
            # Update the aggregate source-SHA view with the merged retry result.
            # The provider reused successful family slices and rebuilt only failed
            # slices; the attempt artifact remains under retry_001 for audit.
            base_artifact.write_text(
                json.dumps(cold, indent=2, sort_keys=True), encoding="utf-8"
            )
        elif prefetched_artifact.is_file() and not prefetched_artifact.is_symlink():
            cold = json.loads(prefetched_artifact.read_text(encoding="utf-8"))
            if (
                not isinstance(cold, dict)
                or cold.get("kind") != "cold_registered_product_sum_facts_set"
                or cold.get("design_sha256") != str(incumbent["artifact_sha256"])
                or cold.get("run_id") != run_id
                or cold.get("fresh_rediscovery") is not True
                or cold.get("read_only") is not True
                or cold.get("mutation_eligible") is not False
                or not isinstance(cold.get("candidates"), list)
            ):
                raise RuntimeError("A5 prefetched cold recovery is stale or malformed")
            for recovered in cold["candidates"]:
                expected_receipt = build_product_sum_preflight_receipt(
                    recovered,
                    expected_family_fingerprint=str(
                        recovered.get("family_fingerprint") or ""
                    ),
                    expected_source_sha256=str(incumbent["artifact_sha256"]),
                    expected_run_id=run_id,
                )
                if recovered.get("preflight_receipt") != expected_receipt:
                    raise RuntimeError("A5 prefetched proof receipt is misbound")
            cold["prefetched_and_bound"] = True
            cold["prefetched_seed_sha256"] = str(incumbent["artifact_sha256"])
        else:
            cold = await recover_action_menu_product_sum_preflight(
                mcp,
                cfg,
                incumbent=incumbent,
                critical_paths=paths,
                output_dir=preflight_dir,
                top_k=action_menu_top_k,
                run_id=run_id,
            )
        for item in cold.get("candidates", []):
            if not isinstance(item, dict) or not isinstance(
                item.get("screened_family"), dict
            ):
                continue
            screened = dict(item["screened_family"])
            if cold.get("prefetched_and_bound") is True:
                screened.update({
                    "discovery_state": "prefetched_and_bound",
                    "prefetched_seed_sha256": str(
                        cold.get("prefetched_seed_sha256") or ""
                    ),
                    "prefetch_mutation_authority": False,
                })
            screened_families.append(screened)
        screened_families.extend(
            dict(item)
            for item in cold.get("unavailable_families", [])
            if isinstance(item, dict)
        )
        if cold.get("preflight_complete") is not True:
            blockers = sorted({
                str(
                    (item.get("typed_rejection") or {}).get("reason_code")
                    or "COLD_PREFLIGHT_INCOMPLETE"
                )
                for item in cold.get("unavailable_families", [])
                if isinstance(item, dict)
            })
            cold_recovery_error = ActionMenuError(
                "ACTION_MENU_PREFLIGHT_INCOMPLETE",
                ",".join(blockers) or "bounded cold recovery did not complete",
            )
    except Exception as exc:  # noqa: BLE001 - cold recovery stays fail-closed
        logger.info("[agent] cold complete-family product-sum screen unavailable: %s", exc)
        cold_recovery_error = exc
    registered_arithmetic_packet = build_registered_arithmetic_decision_packet(
        validated_root,
        expected_design_sha256=str(incumbent.get("artifact_sha256") or ""),
        screened_families=screened_families,
    )
    if registered_arithmetic_packet is not None:
        numeric["registered_arithmetic_eligible_family_count"] = int(
            registered_arithmetic_packet["eligible_family_count"]
        )
    proof_bindings: dict[str, dict] = {}
    proof_bindings = {
        str(item.get("family_fingerprint") or ""): dict(
            item.get("preflight_receipt") or {}
        )
        for item in ((cold or {}).get("candidates") or [])
        if isinstance(item, dict)
        and isinstance(item.get("preflight_receipt"), dict)
    }
    if (
        recurrence_screened_family is not None
        and recurrence_proof_binding is not None
    ):
        proof_bindings[
            str(recurrence_screened_family["family_fingerprint"])
        ] = dict(recurrence_proof_binding)
    auxiliary_actions = build_epoch_auxiliary_actions(
        cfg,
        seed_sha256=str(incumbent["artifact_sha256"]),
        run_id=run_id,
        telemetry_epoch=telemetry_epoch,
        cold_isolation=cold_isolation,
        current_evidence=menu_context,
    )
    menu_started = time.monotonic()
    menu_error_code = (
        _action_menu_failure_code(cold_recovery_error)
        if cold_recovery_error is not None else None
    )
    composed_menu = build_recovery_action_menu(
        seed_candidate_id=str(incumbent["candidate_id"]),
        seed_artifact_sha256=str(incumbent["artifact_sha256"]),
        registered_arithmetic_packet=registered_arithmetic_packet,
        proof_bindings=proof_bindings,
        top_k=action_menu_top_k,
        auxiliary_actions=auxiliary_actions,
        recovery_error_code=menu_error_code,
        recovery_error_detail=(
            str(cold_recovery_error) if cold_recovery_error is not None else ""
        ),
        cold_isolation=cold_isolation,
        apply_adaptive_limit=False,
    )
    authorized_action_menu = composed_menu["menu"]
    menu_build_state = str(composed_menu["build_state"])
    menu_error_code = composed_menu["error_code"]
    action_menu_timing = {
        "schema_version": "action-menu-epoch-cost-v1",
        "provider_entry": "recover_cold_product_sum_families",
        "top_k": action_menu_top_k,
        "census_elapsed_s": (cold or {}).get("timing", {}).get(
            "census_elapsed_s"
        ),
        "family_closure_elapsed_s": (cold or {}).get("timing", {}).get(
            "family_closure_elapsed_s"
        ),
        "exact_proof_elapsed_s": (cold or {}).get("timing", {}).get(
            "exact_proof_elapsed_s"
        ),
        "menu_assembly_elapsed_s": round(
            time.monotonic() - menu_started, 6
        ),
        "total_preflight_elapsed_s": (cold or {}).get("timing", {}).get(
            "elapsed_s"
        ),
        "startup_budget_s": 450.0,
    }
    action_menu_build_status = {
        "schema_version": "action-menu-build-status-v1",
        "state": menu_build_state,
        "error_code": menu_error_code,
        "retry_count": 0,
        "provider_entry": "recover_cold_product_sum_families",
        "seed_artifact_sha256": str(incumbent["artifact_sha256"]),
    }
    upstream_eligible = int(
        numeric.get("registered_arithmetic_eligible_family_count", 0) or 0
    )
    if (
        registered_arithmetic_packet is not None
        and upstream_eligible == 0
        and not deep_operator_cards
        and metaloop_report is not None
    ):
        input_dcp = Path(str(incumbent["artifact_path"]))
        source_sha = str(incumbent["artifact_sha256"])
        preflight_root = (
            Path(mcp.run_dir) / f"metaloop_nomination_{source_sha[:12]}"
        )
        async def _recover_constructive() -> dict:
            return await asyncio.wait_for(
                recover_constructive_nomination_contract(
                    mcp,
                    input_dcp=input_dcp,
                    source_sha256=source_sha,
                    clock=contest_clock,
                    output_dir=preflight_root / "constructive",
                    parent_report=metaloop_report,
                ),
                timeout=600.0,
            )

        async def _recover_memory_forward() -> dict:
            return await asyncio.wait_for(
                recover_memory_forward_nomination_contract(
                    mcp,
                    input_dcp=input_dcp,
                    source_sha256=source_sha,
                    clock=contest_clock,
                    output_dir=preflight_root / "memory_forward",
                    live_wall_covered_path_count=int(
                        numeric.get(
                            "memory_write_buffer_forward_live_wall_covered_path_count",
                            0,
                        )
                        or 0
                    ),
                ),
                timeout=240.0,
            )

        recovery_tasks: list[tuple[str, str, asyncio.Task]] = []
        if int(numeric.get("constructive_accumulator_hypothesis_count", 0) or 0):
            recovery_tasks.append((
                "constructive_accumulator",
                "CACC_SIBLING_FAMILY_INCOMPLETE",
                asyncio.create_task(_recover_constructive()),
            ))
        if int(
            numeric.get("memory_write_buffer_forward_hypothesis_count", 0) or 0
        ):
            recovery_tasks.append((
                "memory_write_buffer_forward",
                "MEMFWD_CONSUMER_CLOSURE_INCOMPLETE",
                asyncio.create_task(_recover_memory_forward()),
            ))
        for prefix, refusal_code, task in recovery_tasks:
            try:
                facts = await task
                for field in (
                    "hypothesis_count", "complete_contract_count",
                    "missing_obligation_count", "live_wall_covered_path_count",
                    "nomination_blockers",
                ):
                    numeric[f"{prefix}_{field}"] = facts[field]
            except Exception as exc:  # exact current-seed gate fails closed
                logger.info(
                    "[agent] %s nomination contract unavailable: %s",
                    prefix,
                    exc,
                )
                numeric[f"{prefix}_complete_contract_count"] = 0
                numeric[f"{prefix}_missing_obligation_count"] = 1
                numeric[f"{prefix}_nomination_blockers"] = [refusal_code]
    if run_id:
        supplemental_operator_cards = list(deep_operator_cards)
        supplemental_operator_cards.extend(build_operator_preflight_action_cards(
            run_id=run_id,
            seed_artifact_sha256=str(incumbent["artifact_sha256"]),
            telemetry_epoch=telemetry_epoch,
            numeric_evidence=numeric,
            cold_isolation=cold_isolation,
            design_lut_count=design_lut_count,
            same_run_feedback=operator_feedback,
            sampled_path_count=num_paths,
        ))
        if supplemental_operator_cards:
            auxiliary_actions = build_epoch_auxiliary_actions(
                cfg,
                seed_sha256=str(incumbent["artifact_sha256"]),
                run_id=run_id,
                telemetry_epoch=telemetry_epoch,
                cold_isolation=cold_isolation,
                current_evidence=menu_context,
            )
            auxiliary_actions.extend(supplemental_operator_cards)
            rebuilt = build_recovery_action_menu(
                seed_candidate_id=str(incumbent["candidate_id"]),
                seed_artifact_sha256=str(incumbent["artifact_sha256"]),
                registered_arithmetic_packet=registered_arithmetic_packet,
                proof_bindings=proof_bindings,
                top_k=action_menu_top_k,
                auxiliary_actions=auxiliary_actions,
                recovery_error_code=menu_error_code,
                recovery_error_detail=(
                    str(cold_recovery_error) if cold_recovery_error is not None else ""
                ),
                cold_isolation=cold_isolation,
                apply_adaptive_limit=False,
            )
            authorized_action_menu = rebuilt["menu"]
            action_menu_timing["supplemental_operator_card_count"] = len(
                supplemental_operator_cards
            )
    return OperatorMiningEvidence(
        numeric,
        public_motif,
        registered_arithmetic_packet,
        authorized_action_menu,
        action_menu_timing,
        action_menu_build_status,
    )


def _operator_probe_payload(raw: object) -> dict:
    """Parse an internal exact operator probe and preserve its typed failure."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("operator probe returned a non-object response")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def _operator_probe_failure(exc: Exception, *, reason_code: str) -> dict[str, str]:
    """Reduce an exact-probe exception to anonymous typed public evidence."""
    exception_class = type(exc).__name__
    raw = getattr(exc, "raw", "")
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            reported = payload.get("error_type")
            if (
                isinstance(reported, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", reported)
            ):
                exception_class = reported
    return {
        "reason_code": reason_code,
        "exception_class": exception_class,
    }


async def collect_replay_semantic_evidence(
    mcp: MCPClient,
    cfg: Config,
    *,
    incumbent: dict,
) -> dict:
    """Collect bounded exact-incumbent preflight facts; failures stay unknown."""
    evidence: dict[str, int | float] = {}
    contest_clock = str(cfg.get("legality.contest_clock", "clk_fpl26contest"))
    max_fanout = 64
    num_paths = max(1, min(16, int(cfg.get("analysis.profile_num_paths", 16))))

    try:
        output = await mcp.call_vivado(
            "run_tcl",
            {
                "command": _build_boundary_link_discovery_tcl(
                    max_fanout=max_fanout,
                    num_paths=num_paths,
                    contest_clock=contest_clock,
                )
            },
            timeout=300.0,
        )
        boundary = _parse_boundary_link_discovery(str(output), max_fanout=max_fanout)
        evidence["boundary_link_eligible_count"] = int(boundary is not None)
    except Exception as exc:  # noqa: BLE001
        logger.info("[replay] boundary-link preflight unavailable: %s", exc)

    try:
        output = await mcp.call_vivado(
            "run_tcl",
            {
                "command": _build_worst_source_discovery_tcl(
                    256, contest_clock
                )
            },
            timeout=300.0,
        )
        source_nets = _parse_discovered_nets(str(output))
        evidence["worst_source_eligible_count"] = int(len(source_nets) == 1)
    except Exception as exc:  # noqa: BLE001
        logger.info("[replay] worst-source preflight unavailable: %s", exc)

    try:
        output = await mcp.call_vivado(
            "run_tcl",
            {
                "command": _build_endpoint_safety_evidence_tcl(
                    num_paths,
                    contest_clock,
                )
            },
            timeout=600.0,
        )
        endpoints = _parse_endpoint_safety_evidence(str(output))
        evidence["endpoint_move_eligible_count"] = min(len(endpoints), 20)
    except Exception as exc:  # noqa: BLE001
        logger.info("[replay] endpoint preflight unavailable: %s", exc)

    try:
        collected_operator_evidence = await collect_operator_mining_evidence(
            mcp,
            cfg,
            incumbent=incumbent,
        )
        operator_evidence = collected_operator_evidence.numeric
        evidence.update(operator_evidence)
    except Exception as exc:  # noqa: BLE001
        logger.info("[replay] operator semantic preflight unavailable: %s", exc)
    return evidence


def _retire_replay_selection_not_started(
    memory: Memory,
    cfg: Config,
    *,
    completed_at_s: float,
) -> None:
    metadata = (memory.replay_control or {}).get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        raise RuntimeError("replay metadata is unavailable")
    control = memory.validate_replay_control(metadata)
    pending = control.get("pending_selection")
    if not isinstance(pending, dict):
        return
    policy = ReplayPolicy(cfg)
    state = ReplayState.from_dict(control["state"])
    authorization = SelectionAuthorization.from_dict(pending["authorization"])
    state = policy.record_outcome(
        state,
        authorization,
        ActionCycleOutcome(execution_status="not_started"),
        completed_at_s,
    )
    memory.set_replay_state(state.to_dict())
    memory.clear_replay_pending_selection()


def _validated_commit_contract(context: dict) -> dict:
    contract = context.get("commit_contract")
    if (
        not isinstance(contract, dict)
        or set(contract) != {"schema_version", "promotion_min_delta_mhz"}
        or contract.get("schema_version") != 1
        or isinstance(contract.get("promotion_min_delta_mhz"), bool)
        or not isinstance(contract.get("promotion_min_delta_mhz"), (int, float))
        or not math.isfinite(float(contract["promotion_min_delta_mhz"]))
        or float(contract["promotion_min_delta_mhz"]) < 0.0
    ):
        raise RuntimeError("action transaction commit contract is malformed")
    return contract


def validate_autonomous_transaction_context(
    memory: Memory,
    transaction: dict,
    cfg: Config,
    *,
    allow_inflight_terminal_invalidation: bool = False,
    commit_result: Optional[dict] = None,
) -> None:
    """Validate durable non-replay launch facts before recovery or archival."""
    context = transaction.get("policy_context")
    if context is None:
        return
    expected_fields = {
        "schema_version",
        "kind",
        "execution_mode",
        "incumbent_binding",
        "resolved_action_fingerprint",
        "commit_contract",
        "launch_authorization",
    }
    if (
        not isinstance(context, dict)
        or set(context) != expected_fields
        or context.get("schema_version") != 1
        or context.get("kind") != "autonomous_action_context"
        or context.get("execution_mode")
        not in {"staged_react", "legacy_llm", "deterministic"}
    ):
        raise RuntimeError("autonomous transaction policy context is malformed")
    _validated_commit_contract(context)
    binding = context.get("incumbent_binding")
    if not isinstance(binding, dict) or set(binding) != {
        "candidate_id",
        "artifact_sha256",
        "fmax_mhz",
    }:
        raise RuntimeError("autonomous incumbent binding is malformed")
    incumbent = memory.candidate_node(str(binding["candidate_id"]))
    if (
        incumbent is None
        or incumbent.get("artifact_sha256") != binding["artifact_sha256"]
        or incumbent.get("fmax_mhz") is None
        or float(incumbent["fmax_mhz"]) != float(binding["fmax_mhz"])
        or str(transaction.get("seed_candidate_id")) != binding["candidate_id"]
        or transaction.get("action_fingerprint")
        != context.get("resolved_action_fingerprint")
    ):
        raise RuntimeError("autonomous transaction launch binding changed")
    run_dir = Path(str(memory.run_manifest.get("canonical_run_dir") or ""))
    validate_graph_node_artifact(run_dir, incumbent)

    authorization = context.get("launch_authorization")
    operator_pair = (
        str(transaction.get("skill")),
        str(transaction.get("target")),
    )
    is_staged_operator = (
        context["execution_mode"] == "staged_react"
        and operator_pair in registered_operator_directions()
    )
    if authorization is None:
        if is_staged_operator:
            raise RuntimeError("staged operator transaction lacks launch authorization")
        return
    if (
        isinstance(authorization, dict)
        and authorization.get("kind") == "action_menu_launch_authorization"
    ):
        menu_auth_fields = {
            "schema_version",
            "kind",
            "seed_candidate_id",
            "seed_artifact_sha256",
            "menu_digest",
            "selected_entry_id",
            "family_fingerprint",
            "recovered_plan_digest",
            "proof_digest",
            "parameter_authority",
        }
        if (
            not is_staged_operator
            or set(authorization) != menu_auth_fields
            or authorization.get("schema_version") != 1
            or authorization.get("seed_candidate_id") != binding["candidate_id"]
            or authorization.get("seed_artifact_sha256") != binding["artifact_sha256"]
        ):
            raise RuntimeError("action-menu operator launch authorization is malformed")
        telemetry = memory.current_telemetry or {}
        if telemetry.get("candidate_id") != binding["candidate_id"]:
            raise RuntimeError("action-menu operator telemetry binding changed")
        try:
            menu = validate_action_menu(
                telemetry.get("authorized_action_menu"),
                expected_seed_sha256=str(binding["artifact_sha256"]),
            )
        except Exception as exc:
            try:
                invalidation = validate_action_menu_invalidation(
                    telemetry.get("action_menu_invalidation")
                )
                exemption = invalidation.get("inflight_terminal_exemption") or {}
                promoted_ids = list(
                    (commit_result or {}).get("promoted_candidate_ids") or []
                )
                best_id = str(
                    memory.candidate_graph.get("current_best_candidate_id") or ""
                )
                best = memory.candidate_node(best_id) or {}
                transaction_candidates = {
                    str(item.get("candidate_id") or "")
                    for item in _transaction_candidates(transaction)
                }
                # The launched action is already pinned by three recorded
                # facts: the menu digest fixes the whole menu, the selected
                # entry id fixes the entry inside it, and the launch
                # authorization digest fixes what was authorized. Rebuilding
                # the action here cannot add a binding, because this branch
                # runs precisely when the menu is no longer available to read.
                # A rebuilt guess also assumed one fixed parameter shape, so a
                # mechanism whose resolved parameters differ could never
                # satisfy the exemption and lost a fully measured candidate.
                if (
                    allow_inflight_terminal_invalidation is not True
                    or transaction.get("phase") != "measured"
                    or exemption.get("transaction_id")
                    != transaction.get("transaction_id")
                    or exemption.get("seed_candidate_id")
                    != binding["candidate_id"]
                    or exemption.get("seed_artifact_sha256")
                    != binding["artifact_sha256"]
                    or exemption.get("menu_digest")
                    != authorization.get("menu_digest")
                    or exemption.get("selected_entry_id")
                    != authorization.get("selected_entry_id")
                    or exemption.get("launch_authorization_digest")
                    != object_digest(authorization)
                    or invalidation.get("promoted_candidate_sha256")
                    != best.get("artifact_sha256")
                    or best_id not in promoted_ids
                    or best_id not in transaction_candidates
                ):
                    raise RuntimeError(
                        "in-flight terminal action-menu exemption mismatch"
                    )
                return
            except Exception as invalidation_exc:
                raise RuntimeError(
                    "action-menu operator launch authorization is no longer durable"
                ) from invalidation_exc
        matches = [
            item for item in menu["entries"]
            if item.get("entry_id") == authorization.get("selected_entry_id")
        ]
        if len(matches) != 1:
            raise RuntimeError("action-menu operator selection binding changed")
        entry = matches[0]
        action = entry.get("action") or {}
        if (
            entry.get("eligible") is not True
            or action.get("skill") != operator_pair[0]
            or action.get("target") != operator_pair[1]
            or any(
                transaction.get("params", {}).get(key) != value
                for key, value in action.get("params", {}).items()
            )
            or menu.get("seed_candidate_id") != binding["candidate_id"]
            or menu.get("menu_digest") != authorization.get("menu_digest")
            or menu.get("parameter_authority")
            != authorization.get("parameter_authority")
            or entry.get("family_fingerprint")
            != authorization.get("family_fingerprint")
            or entry.get("recovered_plan_digest")
            != authorization.get("recovered_plan_digest")
            or entry.get("proof_digest") != authorization.get("proof_digest")
        ):
            raise RuntimeError("action-menu operator launch binding changed")
        return
    if is_staged_operator:
        raise RuntimeError(
            "staged operator launch requires action-menu authorization"
        )
    raise RuntimeError("unsupported autonomous launch authorization")


def validate_replay_pending_transaction(
    memory: Memory,
    transaction: dict,
    cfg: Config,
) -> None:
    """Fail before recovery if a replay transaction drifted from authorization."""
    if not memory.replay_control:
        validate_autonomous_transaction_context(memory, transaction, cfg)
        return
    context = transaction.get("policy_context")
    if not isinstance(context, dict) or set(context) != {
        "schema_version",
        "authorization",
        "incumbent_binding",
        "resolved_action_fingerprint",
        "commit_contract",
    } or context.get("schema_version") != 1:
        raise RuntimeError("replay transaction policy context is malformed")
    _validated_commit_contract(context)
    authorization = SelectionAuthorization.from_dict(context["authorization"])
    binding = context.get("incumbent_binding")
    if not isinstance(binding, dict) or set(binding) != {
        "candidate_id", "artifact_sha256", "fmax_mhz"
    }:
        raise RuntimeError("replay incumbent binding is malformed")
    incumbent = memory.candidate_node(str(binding["candidate_id"]))
    if (
        incumbent is None
        or incumbent.get("artifact_sha256") != binding["artifact_sha256"]
        or incumbent.get("fmax_mhz") is None
        or float(incumbent["fmax_mhz"]) != float(binding["fmax_mhz"])
        or str(transaction.get("seed_candidate_id")) != binding["candidate_id"]
    ):
        raise RuntimeError("replay transaction incumbent binding changed")
    run_dir = Path(str(memory.run_manifest.get("canonical_run_dir") or ""))
    incumbent_path = validate_graph_node_artifact(run_dir, incumbent)
    resolved = resolve_replay_action(authorization.action)
    expected_params = copy.deepcopy(resolved.resolved_params)
    expected_params["input_dcp"] = str(incumbent_path)
    if (
        transaction.get("skill") != resolved.skill
        or transaction.get("target") != resolved.canonical_target
        or transaction.get("action_fingerprint") != resolved.action_fingerprint
        or context.get("resolved_action_fingerprint") != resolved.action_fingerprint
        or json.dumps(
            transaction.get("params") or {},
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != json.dumps(
            expected_params,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    ):
        raise RuntimeError("replay transaction mechanics changed after authorization")


def _finalize_replay_transaction(
    memory: Memory,
    cfg: Config,
    *,
    transaction: dict,
    terminal_status: str,
    commit_result: Optional[dict],
) -> None:
    """Retire one replay authorization from authoritative transaction facts."""
    policy_context = transaction.get("policy_context")
    if not memory.replay_control:
        validate_autonomous_transaction_context(
            memory,
            transaction,
            cfg,
            allow_inflight_terminal_invalidation=True,
            commit_result=commit_result,
        )
        return
    if not isinstance(policy_context, dict):
        raise RuntimeError("replay transaction is missing policy context")
    expected_fields = {
        "schema_version",
        "authorization",
        "incumbent_binding",
        "resolved_action_fingerprint",
        "commit_contract",
    }
    if set(policy_context) != expected_fields or policy_context.get("schema_version") != 1:
        raise RuntimeError("replay transaction policy context is malformed")
    _validated_commit_contract(policy_context)

    policy = ReplayPolicy(cfg)
    metadata = (memory.replay_control or {}).get("metadata")
    if not isinstance(metadata, dict) or not metadata:
        raise RuntimeError("replay metadata is unavailable")
    control = memory.validate_replay_control(metadata)
    pending = control.get("pending_selection")
    if pending is not None and json.dumps(
        pending, sort_keys=True, separators=(",", ":"), allow_nan=False
    ) != json.dumps(
        policy_context, sort_keys=True, separators=(",", ":"), allow_nan=False
    ):
        raise RuntimeError("replay transaction differs from its pending selection")
    authorization = SelectionAuthorization.from_dict(
        policy_context["authorization"]
    )
    existing = control["slot_bindings"].get(authorization.mutation_slot)
    if existing is not None:
        if existing.get("transaction_id") != transaction.get("transaction_id"):
            raise RuntimeError("replay mutation slot is bound to another transaction")

    binding = policy_context["incumbent_binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "candidate_id", "artifact_sha256", "fmax_mhz"
    }:
        raise RuntimeError("replay incumbent binding is malformed")
    incumbent = memory.candidate_node(str(binding["candidate_id"]))
    if (
        incumbent is None
        or incumbent.get("artifact_sha256") != binding["artifact_sha256"]
        or float(incumbent.get("fmax_mhz")) != float(binding["fmax_mhz"])
        or str(transaction.get("seed_candidate_id")) != binding["candidate_id"]
    ):
        raise RuntimeError("replay incumbent binding changed before outcome archival")
    run_dir = Path(str(memory.run_manifest.get("canonical_run_dir") or ""))
    incumbent_path = validate_graph_node_artifact(run_dir, incumbent)

    resolved = resolve_replay_action(authorization.action)
    expected_params = copy.deepcopy(resolved.resolved_params)
    expected_params["input_dcp"] = str(incumbent_path)
    if (
        transaction.get("skill") != resolved.skill
        or transaction.get("target") != resolved.canonical_target
        or transaction.get("action_fingerprint") != resolved.action_fingerprint
        or policy_context.get("resolved_action_fingerprint")
        != resolved.action_fingerprint
        or json.dumps(
            transaction.get("params") or {},
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != json.dumps(
            expected_params,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    ):
        raise RuntimeError("replay transaction mechanics changed after authorization")
    if existing is not None:
        memory.clear_replay_pending_selection()
        return

    execution_started = transaction.get("execution_started")
    phase = str(transaction.get("phase") or "")
    has_candidate_evidence = bool(_transaction_candidates(transaction))
    execution_status = (
        "started" if execution_started is True
        else "unknown"
        if execution_started is False
        and (phase not in {"prepared", "executing"} or has_candidate_evidence)
        else "not_started" if execution_started is False
        else "unknown"
    )
    siblings = []
    if execution_status == "started":
        for item in (commit_result or {}).get("outcomes", []):
            measurement = item.get("measurement") or {}
            legal = measurement_legality(measurement)
            if legal is True and measurement.get("fmax_mhz") is not None:
                siblings.append(SiblingOutcome(
                    status="legal",
                    delta_fmax_mhz_vs_incumbent=(
                        float(measurement["fmax_mhz"]) - float(binding["fmax_mhz"])
                    ),
                    promoted=bool(item.get("promoted")),
                ))
            elif legal is False:
                siblings.append(SiblingOutcome(status="illegal"))
            else:
                siblings.append(SiblingOutcome(status="unknown"))
    outcome = ActionCycleOutcome(
        execution_status=execution_status,
        siblings=tuple(siblings),
    )
    memory.refresh_budget()
    completed_at_s = max(
        float(authorization.selected_at_s),
        float(memory.budget.get("time_elapsed", 0.0)),
    )
    state = ReplayState.from_dict(control["state"])
    state = policy.record_outcome(
        state,
        authorization,
        outcome,
        completed_at_s,
    )
    memory.set_replay_state(state.to_dict())
    if execution_status != "not_started":
        memory.bind_replay_slot(
            authorization.mutation_slot,
            {
                "schema_version": 1,
                "transaction_id": transaction["transaction_id"],
                "terminal_status": terminal_status,
                "completed_at_s": completed_at_s,
                "authorization": authorization.to_dict(),
                "outcome": outcome.to_dict(),
                "incumbent_binding": copy.deepcopy(binding),
                "resolved_action_fingerprint": policy_context[
                    "resolved_action_fingerprint"
                ],
            },
        )
    memory.clear_replay_pending_selection()


def validate_completed_replay_submission(
    memory: Memory,
    *,
    run_dir: Path,
    output_dcp: Path,
) -> dict:
    """Validate a completed replay without rewriting any committed artifact."""
    control = memory.validate_replay_control()
    state = ReplayState.from_dict(control["state"])
    if not state.submission_complete or not state.closeout_complete:
        raise RuntimeError("replay state is not complete")
    if memory.pending_transaction or control.get("pending_selection") is not None:
        raise RuntimeError("completed replay retains pending work")
    submission = control.get("submission")
    if not isinstance(submission, dict) or not submission:
        raise RuntimeError("completed replay lacks submission evidence")
    manifest_path = Path(run_dir) / "submission" / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("completed replay submission manifest is unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("state") not in {
        "committed", "fallback_committed"
    }:
        raise RuntimeError("completed replay manifest is not terminal")
    output_path = Path(output_dcp)
    if output_path.is_symlink() or not output_path.is_file():
        raise RuntimeError("completed replay output is unavailable or symlinked")
    output_sha256 = sha256_file(output_path)
    output_info = manifest.get("output") or {}
    if (
        output_info.get("sha256") != output_sha256
        or submission.get("output_sha256") != output_sha256
        or submission.get("manifest_state") != manifest.get("state")
    ):
        raise RuntimeError("completed replay output identity changed")
    output_candidate_id = submission.get("output_candidate_id")
    output_node = memory.candidate_node(str(output_candidate_id))
    if output_node is None or output_node.get("artifact_sha256") != output_sha256:
        raise RuntimeError("completed replay output candidate binding changed")
    attempted_id = submission.get("attempted_candidate_id")
    if attempted_id != manifest.get("selected_candidate_id"):
        raise RuntimeError("completed replay attempted candidate changed")
    expected_output_id = (
        attempted_id
        if manifest.get("state") == "committed"
        else memory.candidate_graph.get("root_candidate_id")
    )
    if output_candidate_id != expected_output_id:
        raise RuntimeError("completed replay output candidate is ambiguous")
    return manifest


def archive_terminal_action(
    memory: Memory,
    cfg: Config,
    *,
    terminal_status: str,
    terminal_reason: str,
    registry_required: bool,
    commit_result: Optional[dict] = None,
    recovered: bool = False,
) -> dict:
    """Attach one Outcome Proof and archive the existing active transaction."""
    transaction = copy.deepcopy(memory.pending_transaction or {})
    if not transaction:
        raise RuntimeError("cannot archive an absent action transaction")
    action_proof = transaction.get("action_proof") or {}
    outcome_proof = None
    if action_proof:
        outcome_proof = build_action_outcome_record(
            memory=memory,
            action_proof=action_proof,
            transaction=transaction,
            commit_result=commit_result,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
            recovered=recovered,
        )
    registry_required = bool(
        registry_required
        and outcome_proof
        and (outcome_proof.get("execution") or {}).get("action_executed") is True
    )
    _finalize_replay_transaction(
        memory,
        cfg,
        transaction=transaction,
        terminal_status=terminal_status,
        commit_result=commit_result,
    )
    return memory.archive_action_transaction(
        terminal_status=terminal_status,
        terminal_reason=terminal_reason,
        outcome_proof=outcome_proof,
        registry_required=registry_required,
        history_limit=_transaction_history_limit(cfg),
    )


def publish_action_evidence_outbox(
    memory: Memory,
    cfg: Config,
    memory_path: Path,
    registry_path: Optional[Path],
) -> int:
    """Idempotently deliver durable terminal Outcome Proofs to the registry."""
    transaction_count = len(memory.action_transactions)
    memory.prune_action_transactions(_transaction_history_limit(cfg))
    changed = len(memory.action_transactions) != transaction_count
    if registry_path is None:
        if changed:
            memory.save(memory_path)
        return 0
    delivered = 0
    for transaction in list(memory.action_transactions):
        delivery = transaction.get("registry_delivery") or {}
        if not delivery.get("required") or delivery.get("persisted"):
            continue
        transaction_id = str(transaction.get("transaction_id") or "")
        outcome_proof = transaction.get("outcome_proof") or {}
        if not transaction_id or not outcome_proof:
            continue
        try:
            append_action_evidence(
                registry_path,
                outcome_proof,
                max_records=int(cfg.get("action_review.registry_max_records", 512)),
            )
        except Exception as exc:  # noqa: BLE001
            memory.record_action_evidence_delivery(
                transaction_id,
                persisted=False,
                error=str(exc),
            )
            logger.warning(
                "[agent] failed to deliver Outcome Proof %s: %s",
                transaction_id,
                exc,
            )
        else:
            memory.record_action_evidence_delivery(
                transaction_id,
                persisted=True,
            )
            delivered += 1
        changed = True
    transaction_count = len(memory.action_transactions)
    memory.prune_action_transactions(_transaction_history_limit(cfg))
    changed = changed or len(memory.action_transactions) != transaction_count
    if changed:
        memory.save(memory_path)
    return delivered


def commit_failed_action_transaction(
    memory: Memory,
    cfg: Config,
    memory_path: Path,
    *,
    terminal_status: str,
    terminal_reason: str,
    calibration_status: Optional[str],
    registry_path: Optional[Path],
    action_runtime_s: Optional[float] = None,
    recovered: bool = False,
) -> dict:
    """Atomically calibrate and archive a launched action with no candidates."""
    if action_runtime_s is not None:
        memory.pending_transaction["action_runtime_s"] = float(action_runtime_s)
    memory.pending_transaction["result_summary"] = str(terminal_reason)
    if calibration_status is None:
        memory.pending_score_calibration = {}
    else:
        memory.finalize_score_calibration(
            status=calibration_status,
            candidate_count=0,
            legal_candidate_count=0,
            promoted_candidate_ids=[],
            history_limit=int(
                cfg.get("score_scheduling.calibration_history_limit", 24)
            ),
        )
    archived = archive_terminal_action(
        memory,
        cfg,
        terminal_status=terminal_status,
        terminal_reason=terminal_reason,
        registry_required=registry_path is not None,
        recovered=recovered,
    )
    memory.save(memory_path)
    publish_action_evidence_outbox(memory, cfg, memory_path, registry_path)
    return archived


def select_atomic_promotion_candidate(
    candidates: list[dict],
    *,
    incumbent_fmax_mhz: float,
    promotion_delta_mhz: float,
) -> Optional[str]:
    """Choose at most one measured sibling before mutating graph-best state."""
    eligible = []
    for ordinal, item in enumerate(candidates):
        measurement = item.get("measurement") if isinstance(item, dict) else None
        if (
            not isinstance(measurement, dict)
            or measurement.get("eligible_for_beam") is not True
            or isinstance(measurement.get("fmax_mhz"), bool)
            or not isinstance(measurement.get("fmax_mhz"), (int, float))
        ):
            continue
        fmax = float(measurement["fmax_mhz"])
        if fmax <= float(incumbent_fmax_mhz) + float(promotion_delta_mhz):
            continue
        eligible.append((fmax, -ordinal, str(item.get("candidate_id") or "")))
    if not eligible:
        return None
    return max(eligible)[2] or None


def commit_measured_action(
    memory: Memory,
    cfg: Config,
    memory_path: Path,
    promotion_delta: float,
    *,
    recovered: bool = False,
    registry_required: bool = False,
    provenance_ledger: NoReplayLedger | None = None,
) -> dict:
    """Atomically commit every measured sibling to graph and history."""
    transaction = copy.deepcopy(memory.pending_transaction or {})
    if transaction.get("phase") != "measured":
        raise RuntimeError("action transaction is not fully measured")
    candidates = _transaction_candidates(transaction)
    if not candidates:
        raise RuntimeError("measured action transaction has no candidates")

    staged = copy.deepcopy(memory)
    seed_id = str(transaction.get("seed_candidate_id"))
    seed = staged.candidate_node(seed_id)
    if seed is None:
        raise RuntimeError(f"pending transaction has unknown seed {seed_id}")
    created_round = int(transaction.get("created_round", staged.round + 1))
    if created_round != staged.round + 1:
        raise RuntimeError(
            f"pending transaction round {created_round} does not follow {staged.round}"
        )
    staged.consume_candidate_continuation(
        seed_id, kind="mutation", consumed_round=created_round
    )
    action_runtime = float(transaction.get("action_runtime_s", 0.0))
    cycle_best_before = staged.candidate_node(
        staged.candidate_graph["current_best_candidate_id"]
    )
    if cycle_best_before is None or cycle_best_before.get("fmax_mhz") is None:
        raise RuntimeError("action cycle incumbent has no measured Fmax")
    cycle_best_before_fmax = float(cycle_best_before["fmax_mhz"])
    promotion_winner_id = select_atomic_promotion_candidate(
        candidates,
        incumbent_fmax_mhz=cycle_best_before_fmax,
        promotion_delta_mhz=promotion_delta,
    )
    outcomes = []
    for ordinal, item in enumerate(candidates):
        candidate_id = str(item.get("candidate_id"))
        if staged.candidate_node(candidate_id) is not None:
            raise RuntimeError(f"pending candidate already exists in graph: {candidate_id}")
        canonical = Path(str(item.get("canonical_path", "")))
        if not canonical.is_file():
            raise RuntimeError(f"pending canonical candidate is missing: {canonical}")
        digest = sha256_file(canonical)
        expected_digest = item.get("artifact_sha256")
        if expected_digest and digest != expected_digest:
            raise RuntimeError(f"pending canonical candidate hash mismatch: {candidate_id}")
        if provenance_ledger is not None:
            try:
                provenance_ledger.admit_descendant(
                    artifact_path=canonical,
                    artifact_sha256=digest,
                    parent_artifact_sha256=str(seed.get("artifact_sha256") or ""),
                )
            except NoReplayProvenanceError as exc:
                raise RuntimeError(
                    f"no-replay provenance rejected candidate {candidate_id}: {exc}"
                ) from exc
        signature = item.get("physical_signature") or unavailable_signature(
            "legacy_unavailable", "transaction predates physical signatures", digest
        )
        if (
            signature.get("status") == "complete"
            and signature.get("artifact_sha256") != digest
        ):
            raise RuntimeError(
                f"pending physical signature hash mismatch: {candidate_id}"
            )
        measurement = item.get("measurement") or {}
        if not isinstance(measurement, dict) or not measurement:
            raise RuntimeError(f"pending candidate is not measured: {candidate_id}")
        validation_runtime = float(item.get("validation_runtime_s") or 0.0)
        summary = str(item.get("label") or transaction.get("result_summary") or "")
        eco_proof = validate_action_candidate_eco_proof(
            staged,
            run_dir=memory_path.parent,
            seed_candidate_id=seed_id,
            skill=str(transaction.get("skill", "?")),
            target=str(transaction.get("target", "")),
            proof=item.get("proof"),
            evidence=item.get("evidence"),
            candidate_artifact_sha256=digest,
        )
        child = graph_ops.make_node(
            candidate_id=candidate_id,
            parent_candidate_id=seed_id,
            depth=int(seed["depth"]) + 1,
            created_round=created_round,
            created_at=float(item.get("published_at") or transaction.get("created_at", time.time())),
            artifact_path=str(canonical.resolve()),
            artifact_state="materialized",
            artifact_size_bytes=canonical.stat().st_size,
            artifact_sha256=digest,
            search_state=(
                "active" if measurement.get("eligible_for_beam") else "ineligible"
            ),
            action_fingerprint=transaction.get("action_fingerprint", ""),
            skill=transaction.get("skill", "?"),
            target=transaction.get("target", "auto"),
            resolved_params=transaction.get("params") or {},
            summary=summary,
            measurement=measurement,
            action_runtime_s=action_runtime,
            validation_runtime_s=validation_runtime,
            cumulative_lineage_runtime_s=(
                float(seed.get("cumulative_lineage_runtime_s", 0.0))
                + action_runtime
                + validation_runtime
            ),
            physical_signature=signature,
            evidence=item.get("evidence"),
            eco_proof=eco_proof or None,
        )
        staged.record_candidate_node(child)
        promoted = candidate_id == promotion_winner_id
        if promoted:
            if transaction.get("concurrent_proof_required") is True:
                validate_action_proof_completion(
                    transaction.get("action_proof") or {},
                    candidate_id=candidate_id,
                    candidate_artifact_sha256=digest,
                )
            staged.promote_candidate(
                candidate_id, finalizing_transaction=transaction
            )
        enabling_record = None
        enabling_evidence = (item.get("evidence") or {}).get("enabling_candidate")
        if enabling_evidence is not None and not promoted:
            if not isinstance(enabling_evidence, dict):
                raise RuntimeError("enabling_candidate evidence must be a mapping")
            enabling_record = staged.retain_enabling_candidate(
                candidate_id,
                parent_profile=enabling_evidence.get("parent_residual_profile"),
                candidate_profile=enabling_evidence.get("candidate_residual_profile"),
                targeted_families=enabling_evidence.get("targeted_families") or [],
                candidate_legal=measurement_legality(measurement) is True,
                strict_validation_passed=(
                    enabling_evidence.get("strict_validation_passed") is True
                ),
                promoted=False,
                regression_tolerance_ns=cfg.get(
                    "run.enabling_residual_regression_tolerance_ns", 0.01
                ),
                local_improvement_epsilon_ns=cfg.get(
                    "run.enabling_local_improvement_epsilon_ns", 0.0005
                ),
                max_composition_depth=cfg.get(
                    "run.enabling_composition_max_depth", 2
                ),
                pool_width=cfg.get("run.enabling_candidate_pool_width", 4),
            )
        outcomes.append({
            "candidate_id": candidate_id,
            "measurement": measurement,
            "promoted": promoted,
            "global_best_before_fmax_mhz": cycle_best_before_fmax,
            "summary": summary,
            "validation_runtime_s": validation_runtime,
            "enabling_candidate": enabling_record,
        })

    staged.update_candidate_beam(created_round)
    strategy_family = resolve_strategy_family(
        transaction.get("skill", "?"),
        cfg.get("saturation.level1.strategy_family_by_skill", {}) or {},
    )
    for ordinal, outcome in enumerate(outcomes):
        measurement = outcome["measurement"]
        candidate_id = outcome["candidate_id"]
        child = staged.candidate_node(candidate_id)
        candidate_legal = measurement_legality(measurement)
        measured_wns = measurement.get("wns_ns")
        legality_note = (
            ""
            if candidate_legal is True
            else f" | CENTRAL INVALID: {measurement.get('legality_reason', '')}"
            if candidate_legal is False
            else f" | CENTRAL UNKNOWN: {measurement.get('legality_reason', '')}"
        )
        recovery_note = "RECOVERED: " if recovered else ""
        staged.record(
            skill=transaction.get("skill", "?"),
            target=transaction.get("target", "auto"),
            before_wns=float(seed["wns_ns"]),
            after_wns=(
                float(measured_wns) if measured_wns is not None else float(seed["wns_ns"])
            ),
            accepted=outcome["promoted"],
            params=transaction.get("params") or {},
            summary=recovery_note + outcome["summary"] + legality_note,
            expectation=transaction.get("expectation", ""),
            action_fingerprint=transaction.get("action_fingerprint", ""),
            seed_round=seed.get("created_round"),
            seed_candidate_id=seed_id,
            produced_candidate_id=candidate_id,
            candidate_created=True,
            candidate_legal=candidate_legal,
            in_beam=bool(child.get("in_active_beam")),
            action_executed=True,
            strategy_family=strategy_family,
            seed_fmax_mhz=seed.get("fmax_mhz"),
            measured_fmax_mhz=measurement.get("fmax_mhz"),
            global_best_before_fmax_mhz=outcome["global_best_before_fmax_mhz"],
            action_runtime_s=action_runtime,
            validation_runtime_s=outcome["validation_runtime_s"],
            candidate_ordinal=ordinal,
            candidate_count=len(outcomes),
            advance_round=ordinal == 0,
        )
        if candidate_legal is not True:
            staged.record_failure(
                transaction.get("skill", "?"),
                transaction.get("target", "auto"),
                measurement.get("legality_reason") or "central measurement invalid",
            )

    candidate_legality = [
        measurement_legality(outcome["measurement"]) for outcome in outcomes
    ]
    legal_candidate_count = sum(value is True for value in candidate_legality)
    staged.finalize_score_calibration(
        status="recovered" if recovered else "completed",
        candidate_count=len(outcomes),
        legal_candidate_count=legal_candidate_count,
        promoted_candidate_ids=[
            outcome["candidate_id"] for outcome in outcomes if outcome["promoted"]
        ],
        history_limit=int(cfg.get("score_scheduling.calibration_history_limit", 24)),
        candidate_outcomes=[
            {
                "candidate_id": outcome["candidate_id"],
                "legal": measurement_legality(outcome["measurement"]),
                "fmax_mhz": outcome["measurement"].get("fmax_mhz"),
            }
            for outcome in outcomes
        ],
    )
    commit_result = {
        "outcomes": outcomes,
        "promoted_candidate_ids": [
            outcome["candidate_id"] for outcome in outcomes if outcome["promoted"]
        ],
        "best_candidate_id": staged.candidate_graph["current_best_candidate_id"],
    }
    terminal_status, terminal_reason = _measurement_terminal_status(
        [outcome["measurement"] for outcome in outcomes]
    )
    archived = archive_terminal_action(
        staged,
        cfg,
        terminal_status=terminal_status,
        terminal_reason=terminal_reason,
        registry_required=registry_required,
        commit_result=commit_result,
        recovered=recovered,
    )
    staged.save(memory_path)
    memory.__dict__.update(staged.__dict__)
    commit_result["action_transaction"] = archived
    commit_result["outcome_proof"] = archived.get("outcome_proof") or None
    return commit_result


async def complete_candidate_signatures(
    memory: Memory,
    mcp: MCPClient,
    cfg: Config,
    memory_path: Path,
    *,
    deadline_executor,
) -> None:
    """Persist terminal physical evidence for every published sibling."""
    deadline_reached = False
    for item in memory.pending_transaction.get("candidates", []):
        if item.get("physical_signature"):
            continue
        digest = str(item.get("artifact_sha256", ""))
        measurement = item.get("measurement") or {}
        if not measurement.get("measurement_complete"):
            signature = unavailable_signature(
                "central_measurement_incomplete",
                "central measurement incomplete",
                digest,
            )
        elif deadline_reached:
            signature = unavailable_signature(
                "skipped_deadline", "total-run deadline reached", digest
            )
        else:
            canonical = Path(str(item.get("canonical_path", "")))
            try:
                async def collect_exact_signature():
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": str(canonical.resolve())},
                        timeout=600.0,
                    )
                    return await collect_physical_signature(
                        mcp, measurement, cfg, digest
                    )

                signature = (
                    await collect_exact_signature()
                    if deadline_executor is None
                    else await deadline_executor(
                        collect_exact_signature,
                        f"physical signature {item['candidate_id']}",
                    )
                )
            except asyncio.TimeoutError:
                deadline_reached = True
                signature = unavailable_signature(
                    "skipped_deadline", "total-run deadline reached", digest
                )
        item["physical_signature"] = signature
        memory.advance_action_transaction("published")
    memory.save(memory_path)


async def complete_enabling_candidate_profile(
    *, item: dict, mcp: MCPClient, cfg: Config
) -> None:
    """Attach a fresh candidate top-N profile after central measurement."""
    evidence = item.get("evidence")
    envelope = (
        evidence.get("enabling_candidate")
        if isinstance(evidence, dict)
        else None
    )
    if not isinstance(envelope, dict) or envelope.get("candidate_residual_profile"):
        return
    measurement = item.get("measurement") or {}
    if measurement_legality(measurement) is not True:
        evidence["enabling_candidate_profile_error"] = (
            "central candidate measurement was not legal and complete"
        )
        evidence.pop("enabling_candidate", None)
        return
    try:
        request = envelope.get("profile_request") or {}
        top_n = int(request.get("top_n", 300))
        if top_n != 300:
            raise ValueError("enabling profile top_n must be 300")
        report = await capture_live_timing_report(
            mcp,
            clock=str(cfg.get("legality.contest_clock", "clk_fpl26contest")),
            top_n=top_n,
        )
        envelope["candidate_residual_profile"] = build_measured_residual_profile(
            report,
            artifact_sha256=str(item.get("artifact_sha256") or ""),
            global_wns_ns=float(measurement["wns_ns"]),
            fmax_mhz=float(measurement["fmax_mhz"]),
            top_n=top_n,
            source_label=(
                "live_vivado:candidate_top300:"
                + str(item.get("candidate_id") or "unknown")
            ),
        )
        envelope.pop("profile_request", None)
    except Exception as exc:  # profile authority fails closed, candidate remains valid
        evidence["enabling_candidate_profile_error"] = str(exc)[:500]
        evidence.pop("enabling_candidate", None)


async def recover_pending_action(
    memory: Memory,
    mcp: MCPClient,
    cfg: Config,
    memory_path: Path,
    promotion_delta: float,
    deadline_executor=None,
    action_registry_path: Optional[Path] = None,
) -> None:
    transaction = dict(memory.pending_transaction or {})
    if not transaction:
        return
    validate_replay_pending_transaction(memory, transaction, cfg)
    policy_context = transaction.get("policy_context")
    if isinstance(policy_context, dict) and isinstance(
        policy_context.get("commit_contract"), dict
    ):
        promotion_delta = float(
            policy_context["commit_contract"]["promotion_min_delta_mhz"]
        )
    phase = transaction.get("phase")
    seed_id = str(transaction.get("seed_candidate_id"))
    seed = memory.candidate_node(seed_id)
    if seed is None:
        raise RuntimeError(f"pending transaction has unknown seed {seed_id}")
    candidates = _transaction_candidates(transaction)
    existing = [
        item["candidate_id"]
        for item in candidates
        if memory.candidate_node(str(item.get("candidate_id"))) is not None
    ]
    if candidates and len(existing) == len(candidates):
        existing_nodes = [
            memory.candidate_node(candidate_id) for candidate_id in existing
        ]
        if any(
            node is None or str(node.get("parent_candidate_id")) != seed_id
            for node in existing_nodes
        ):
            raise RuntimeError(
                "recovered existing candidate is not a child of the pending seed"
            )
        existing_measurements = [
            measurement_from_node(node) for node in existing_nodes
        ]
        terminal_status, terminal_reason = _measurement_terminal_status(
            existing_measurements
        )
        archive_terminal_action(
            memory,
            cfg,
            terminal_status=terminal_status,
            terminal_reason=(
                "recovered existing candidates: " + terminal_reason
            ),
            registry_required=action_registry_path is not None,
            commit_result={
                "outcomes": [
                    {
                        "candidate_id": candidate_id,
                        "measurement": measurement_from_node(node),
                        "promoted": bool(node.get("promoted_to_global_best")),
                    }
                    for candidate_id, node in zip(existing, existing_nodes)
                ]
            },
            recovered=True,
        )
        memory.save(memory_path)
        publish_action_evidence_outbox(
            memory, cfg, memory_path, action_registry_path
        )
        return
    if existing:
        raise RuntimeError("pending sibling transaction is partially committed")

    if phase in {"prepared", "executing"}:
        transaction_version = int(transaction.get("version", 1))
        if phase == "prepared" and transaction_version < 5:
            continuation = seed.get("continuation") or {}
            if (
                continuation.get("consumed_kind") == "mutation"
                and continuation.get("consumed_round")
                == transaction.get("created_round")
            ):
                continuation["consumed_round"] = None
                continuation["consumed_kind"] = ""
        execution_started = (
            bool(transaction.get("execution_started"))
            if transaction_version >= 5
            else phase == "executing"
        )
        if execution_started:
            memory.consume_candidate_continuation(
                seed_id,
                kind="mutation",
                consumed_round=int(transaction.get("created_round", memory.round + 1)),
            )
        reason = f"interrupted transaction recovered from {phase} phase"
        if execution_started:
            memory.record(
                skill=transaction.get("skill", "?"),
                target=transaction.get("target", "auto"),
                before_wns=float(seed["wns_ns"]),
                after_wns=float(seed["wns_ns"]),
                accepted=False,
                params=transaction.get("params") or {},
                summary=f"INTERRUPTED: {reason}",
                action_fingerprint=transaction.get("action_fingerprint", ""),
                seed_round=seed.get("created_round"),
                seed_candidate_id=seed_id,
                action_executed=True,
            )
            memory.record_failure(
                transaction.get("skill", "?"),
                transaction.get("target", "auto"),
                reason,
            )
        commit_failed_action_transaction(
            memory,
            cfg,
            memory_path,
            terminal_status="interrupted",
            terminal_reason=reason,
            calibration_status="interrupted" if execution_started else None,
            registry_path=(action_registry_path if execution_started else None),
            recovered=True,
        )
        return
    if phase not in {"publishing", "published", "measured"}:
        raise RuntimeError(f"unsupported pending transaction phase {phase!r}")

    if not candidates:
        raise RuntimeError("pending sibling transaction has no candidate records")
    if int(transaction.get("version", 1)) < 2:
        transaction["version"] = 2
        transaction["candidate_ids"] = [item["candidate_id"] for item in candidates]
        transaction["candidates"] = candidates
        memory.pending_transaction = transaction
        memory.save(memory_path)

    if phase == "publishing":
        seed = memory.candidate_node(str(transaction.get("seed_candidate_id")))
        if seed is None:
            raise RuntimeError("pending high-risk proof has no source candidate")
        for item in memory.pending_transaction["candidates"]:
            candidate_id = str(item["candidate_id"])
            expected = canonical_candidate_path(memory_path.parent, candidate_id)
            recorded_path = str(item.get("canonical_path", ""))
            if recorded_path and Path(recorded_path).resolve() != expected:
                raise RuntimeError(
                    f"pending canonical candidate is outside ownership: {candidate_id}"
                )
            canonical = expected
            if expected.is_symlink() or expected.parent.is_symlink():
                raise RuntimeError(
                    f"pending canonical candidate uses a symlink: {candidate_id}"
                )
            proof_source = (
                expected if expected.is_file()
                else Path(str(item.get("source_path", "")))
            )
            if proof_source.is_symlink() or not proof_source.is_file():
                raise RuntimeError(
                    f"pending candidate proof source is unavailable: {candidate_id}"
                )
            validate_action_candidate_eco_proof(
                memory,
                run_dir=memory_path.parent,
                seed_candidate_id=seed_id,
                skill=str(transaction.get("skill", "?")),
                target=str(transaction.get("target", "")),
                proof=item.get("proof"),
                evidence=item.get("evidence"),
                candidate_artifact_sha256=sha256_file(proof_source),
            )
            if not canonical.is_file():
                source = Path(str(item.get("source_path", "")))
                action_dir = Path(str(transaction.get("action_dir", ""))).resolve()
                if source.is_symlink() or not source.is_file():
                    raise RuntimeError(
                        f"pending candidate source is unavailable: {candidate_id}"
                    )
                try:
                    source.resolve().relative_to(action_dir)
                except ValueError as exc:
                    raise RuntimeError(
                        f"pending candidate source is outside action scratch: {candidate_id}"
                    ) from exc
                if expected.parent.exists():
                    if not expected.parent.is_dir():
                        raise RuntimeError(
                            f"pending candidate path is not a directory: {candidate_id}"
                        )
                    stale_temporary_files = list(expected.parent.iterdir())
                    for temporary in stale_temporary_files:
                        if (
                            temporary.is_symlink()
                            or not temporary.is_file()
                            or not temporary.name.startswith(".design.dcp.")
                            or not temporary.name.endswith(".tmp")
                        ):
                            raise RuntimeError(
                                "pending candidate directory contains an unexpected "
                                f"entry: {candidate_id}"
                            )
                    for temporary in stale_temporary_files:
                        temporary.unlink()
                    atomic_copy_file(
                        source,
                        expected,
                        expected_sha256=(
                            item.get("artifact_sha256")
                            or (item.get("evidence") or {}).get(
                                "candidate_dcp_sha256"
                            )
                            or None
                        ),
                    )
                    canonical = expected
                else:
                    canonical = materialize_candidate_artifact(
                        source,
                        expected.parent,
                        expected_sha256=(
                            (item.get("evidence") or {}).get(
                                "candidate_dcp_sha256"
                            ) or None
                        ),
                    )
            digest = sha256_file(canonical)
            validate_action_candidate_eco_proof(
                memory,
                run_dir=memory_path.parent,
                seed_candidate_id=seed_id,
                skill=str(transaction.get("skill", "?")),
                target=str(transaction.get("target", "")),
                proof=item.get("proof"),
                evidence=item.get("evidence"),
                candidate_artifact_sha256=digest,
            )
            expected_digest = item.get("artifact_sha256")
            if expected_digest and digest != expected_digest:
                raise RuntimeError(
                    f"pending canonical candidate hash mismatch: {candidate_id}"
                )
            item.update({
                "canonical_path": str(canonical.resolve()),
                "artifact_size_bytes": canonical.stat().st_size,
                "artifact_sha256": digest,
                "published_at": item.get("published_at", time.time()),
            })
            memory.advance_action_transaction("publishing")
            memory.save(memory_path)
        memory.advance_action_transaction("published")
        memory.save(memory_path)

    if memory.pending_transaction.get("phase") == "published":
        timed_out = False
        for item in memory.pending_transaction["candidates"]:
            if item.get("measurement"):
                continue
            canonical = Path(str(item.get("canonical_path", "")))
            validation_started = time.monotonic()
            if timed_out:
                measurement = failed_measurement(
                    "central measurement skipped after total-run deadline"
                )
            else:
                try:
                    if deadline_executor is None:
                        measurement = await measure_candidate(mcp, canonical, cfg)
                    else:
                        measurement = await deadline_executor(
                            lambda canonical=canonical: measure_candidate(
                                mcp, canonical, cfg
                            ),
                            f"recovered central measurement {item['candidate_id']}",
                        )
                except asyncio.TimeoutError:
                    timed_out = True
                    measurement = failed_measurement(
                        "central measurement timed out at total-run deadline"
                    )
            item["measurement"] = measurement
            memory.validation_count += 1
            await complete_enabling_candidate_profile(
                item=item, mcp=mcp, cfg=cfg
            )
            item["validation_runtime_s"] = time.monotonic() - validation_started
            memory.advance_action_transaction("published")
            memory.save(memory_path)
        await complete_candidate_signatures(
            memory,
            mcp,
            cfg,
            memory_path,
            deadline_executor=deadline_executor,
        )
        memory.advance_action_transaction("measured")
        memory.save(memory_path)

    commit_measured_action(
        memory,
        cfg,
        memory_path,
        promotion_delta,
        recovered=True,
        registry_required=action_registry_path is not None,
    )
    publish_action_evidence_outbox(
        memory,
        cfg,
        memory_path,
        action_registry_path,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run(
    input_dcp: Path,
    output_dcp: Path,
    model: Optional[str] = None,
    time_limit: Optional[float] = None,
    debug: bool = False,
    run_dir: Optional[Path] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    use_llm: bool = True,
    config_path: Optional[str] = None,
    resume: bool = False,
    submission_mode: bool = False,
    equivalence_runner=None,
    rank1_score: Optional[float] = None,
    score_baseline_fmax_mhz: Optional[float] = None,
    replay_policy_id: Optional[str] = None,
    isolation_audit_path: Optional[Path] = None,
    dcp_manifest_path: Optional[Path] = None,
    engineering_regression: bool = False,
) -> int:
    run_started_at = time.time()
    cfg = load_config(config_path)
    isolation_policy = load_isolation_policy(isolation_audit_path)
    cold_isolation = isolation_policy.replay_derived_actions_forbidden
    isolation_artifact = (
        isolation_policy.as_run_artifact()
        if isolation_audit_path is not None
        else None
    )
    requested_time_limit = time_limit
    time_limit = float(time_limit or cfg.get("run.time_limit_s", 3600))
    if not math.isfinite(time_limit) or time_limit <= 0.0:
        raise ValueError("time limit must be a finite positive number")
    replay_enabled = replay_policy_id is not None
    dcp_admission: Optional[dict[str, Any]] = None
    if engineering_regression and not cold_isolation:
        raise IsolationPolicyError(
            "ENGINEERING_REGRESSION_REQUIRES_COLD_ISOLATION"
        )
    if cold_isolation and resume:
        raise IsolationPolicyError("COLD_ISOLATION_RESUME_FORBIDDEN")
    if cold_isolation:
        if dcp_manifest_path is None:
            raise IsolationPolicyError("COLD_ISOLATION_DCP_MANIFEST_REQUIRED")
        try:
            source_sha256 = sha256_file(input_dcp)
            if engineering_regression:
                dcp_admission = validate_engineering_regression_manifest(
                    dcp_manifest_path,
                    source_sha256,
                )
            else:
                dcp_admission = validate_development_allowlist(
                    dcp_manifest_path,
                    source_sha256,
                )
        except NoReplayProvenanceError as exc:
            raise IsolationPolicyError(exc.reason_code, str(exc)) from exc
    if replay_enabled and cold_isolation:
        raise IsolationPolicyError("COLD_ISOLATION_REPLAY_POLICY_FORBIDDEN")
    replay_policy: Optional[ReplayPolicy] = None
    if replay_enabled:
        if replay_policy_id != REPLAY_POLICY_ID:
            raise ValueError(
                f"unknown replay policy {replay_policy_id!r}; expected {REPLAY_POLICY_ID!r}"
            )
        if not submission_mode:
            raise ValueError("replay policy requires strict submission mode")
        if rank1_score is not None or score_baseline_fmax_mhz is not None:
            raise ValueError("replay policy forbids offline score targets")
        replay_policy = ReplayPolicy(cfg)
        replay_deadline = float(replay_policy.settings.deadlines.submission)
        if abs(time_limit - replay_deadline) > 1e-9:
            raise ValueError(
                f"replay policy requires an exact {replay_deadline:g}s time limit"
            )
    time_safety = float(cfg.get("run.time_safety_s", 240))
    # No submission-only floor.  Every result we have measured was measured with
    # the plain run.time_safety_s, and raising it here only shortens the window
    # the optimizer actually gets.  The equivalence proof it was reserving for
    # takes 178s on optical-flow, well inside the ordinary safety margin.
    max_rounds = int(cfg.get("run.max_rounds", 300))
    max_vetoes = int(cfg.get("run.max_consecutive_vetoes", 3))
    beam_width = int(cfg.get("run.candidate_beam_width", 3))
    promotion_delta = float(cfg.get("run.promotion_min_delta_mhz", 0.01))
    skill_candidate_cap = int(cfg.get("run.max_skill_candidates_per_action", 4))
    candidate_commit_reserve = float(cfg.get("run.candidate_commit_reserve_s", 120))
    central_measurement_estimate = float(
        cfg.get("run.central_measurement_estimate_s", 15)
    )
    level1_gc_enabled = bool(cfg.get("artifacts.level1_gc_enabled", True))
    max_seed_switches = int(
        cfg.get("react.max_seed_switches_per_decision", 2)
    )
    replay_metadata: Optional[dict] = None
    replay_provenance: Optional[dict] = None
    if replay_enabled:
        replay_execution = build_replay_execution_metadata(
            replay_policy,
            cfg,
            use_llm=use_llm,
            model=model,
            time_safety_s=time_safety,
            promotion_delta_mhz=promotion_delta,
            skill_candidate_cap=skill_candidate_cap,
            candidate_commit_reserve_s=candidate_commit_reserve,
        )
        replay_metadata = replay_execution["semantic_contract"]
        replay_provenance = replay_execution["provenance"]

    if resume and run_dir is None:
        raise ValueError("--resume requires --run-dir")
    if run_dir is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        run_dir = Path.cwd() / f"fdagents_run-{ts}"
    run_dir = run_dir.expanduser().resolve()
    if resume and not run_dir.is_dir():
        raise FileNotFoundError(f"resume run directory not found: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    task_scheduler = LocalTaskScheduler(
        run_dir,
        cpu_limit=8,
        memory_limit_gib=32.0,
    )
    logger.info("Run directory: %s", run_dir)
    # The run identity is per-run and carries no history: new_run mints a fresh
    # nr-<uuid4> and the ledger records only this run's own descendants.  Gating
    # it on cold isolation left every scored run with run_id "", and the cold
    # registered-product recovery refuses that outright -- its identity check
    # requires nr-[0-9a-f]{32}.  So the registered product-sum operator rewrite
    # was unavailable in every scored run: optical-flow logs "cold
    # complete-family product-sum screen unavailable: cold registered-product
    # source identity is invalid" twice, falls back to clock_tighten, and gains
    # 4.93 MHz where that mechanism is worth 88.00 here.
    no_replay_run = new_run(
        source_artifact_sha256=sha256_file(input_dcp),
        source_path=input_dcp,
        run_root=run_dir,
    )
    provenance_ledger = NoReplayLedger(no_replay_run)
    provenance_ledger.admit_source()
    action_registry_path = resolve_registry_path(
        cfg.get("action_review.registry_path", "")
    )

    memory_path = run_dir / "memory.json"
    candidates_dir = run_dir / "candidates"
    current_best_dcp = run_dir / "current_best.dcp"
    memory: Optional[Memory] = None
    if resume:
        if not memory_path.is_file():
            raise FileNotFoundError(f"resume memory not found: {memory_path}")
        memory = Memory.load(memory_path)
        persisted_rank1 = (
            memory.hard_target.get("rank1_score") if memory.hard_target else None
        )
        if rank1_score is not None and persisted_rank1 != float(rank1_score):
            raise RuntimeError("resume rank-1 score does not match persisted hard target")
        if rank1_score is not None and not memory.hard_target:
            raise RuntimeError("cannot add a hard target to an existing run")
        persisted_score_baseline = (
            memory.hard_target.get("baseline_fmax_mhz")
            if memory.hard_target else None
        )
        if (
            score_baseline_fmax_mhz is not None
            and persisted_score_baseline != float(score_baseline_fmax_mhz)
        ):
            raise RuntimeError(
                "resume scoring baseline does not match persisted hard target"
            )
        if score_baseline_fmax_mhz is not None and not memory.hard_target:
            raise RuntimeError("cannot add a scoring baseline to an existing run")
        persisted_mode = memory.run_manifest.get("execution_mode", "experiment")
        requested_mode = "strict_submission" if submission_mode else "experiment"
        if persisted_mode != requested_mode:
            raise RuntimeError("resume execution mode does not match persisted run")
        persisted_strategy = memory.run_manifest.get("strategy_mode", "autonomous")
        requested_strategy = (
            f"replay:{REPLAY_POLICY_ID}" if replay_enabled else "autonomous"
        )
        if persisted_strategy != requested_strategy:
            raise RuntimeError("resume strategy mode does not match persisted run")
        if (
            isolation_artifact is not None
            and memory.run_manifest.get("isolation") != isolation_artifact
        ):
            raise RuntimeError("resume cold-isolation policy or audit changed")
        if memory.run_manifest.get("packaged_semantic_replay") is not None:
            raise RuntimeError("resume carries a retired packaged replay binding")
        if replay_enabled:
            control = memory.validate_replay_control()
            ReplayState.from_dict(control["state"])
        elif memory.replay_control:
            raise RuntimeError("autonomous resume contains replay control state")
        if submission_mode:
            persisted_limit = float(memory.budget.get("time_limit_s", time_limit))
            if requested_time_limit is not None and float(requested_time_limit) != persisted_limit:
                raise RuntimeError("strict submission resume cannot change time limit")
            if memory.run_manifest.get("output_dcp") != str(output_dcp):
                raise RuntimeError("strict submission resume cannot change output path")
        root_node = memory.candidate_node(graph_ops.ROOT_CANDIDATE_ID)
        if root_node is None:
            raise RuntimeError("resume graph has no immutable root")
        root_artifact = Path(root_node["artifact_path"])
        if not root_artifact.is_file():
            raise FileNotFoundError(f"resume root artifact missing: {root_artifact}")
        if sha256_file(input_dcp) != sha256_file(root_artifact):
            raise RuntimeError("resume input DCP does not match immutable root")
        if replay_enabled:
            replay_state = ReplayState.from_dict(control["state"])
            if replay_state.submission_complete:
                validate_completed_replay_submission(
                    memory,
                    run_dir=run_dir,
                    output_dcp=output_dcp,
                )
                memory.record_replay_provenance(
                    replay_provenance,
                    reason="completed_resume",
                    recorded_at=run_started_at,
                )
                memory.run_manifest["resume_count"] = int(
                    memory.run_manifest.get("resume_count", 0)
                ) + 1
                memory.run_manifest["last_resumed_at"] = run_started_at
                memory.save(memory_path)
                return 0
            control = memory.validate_replay_control(replay_metadata)
            memory.record_replay_provenance(
                replay_provenance,
                reason="resume",
                recorded_at=run_started_at,
            )
        prior_elapsed = float(memory.budget.get("time_elapsed", 0.0))
        memory.budget["start_time"] = (
            float(memory.run_manifest["deadline_at"])
            - float(memory.budget["time_limit_s"])
            if submission_mode and memory.run_manifest.get("deadline_at")
            else run_started_at - prior_elapsed
        )
        if requested_time_limit is not None:
            memory.budget["time_limit_s"] = float(requested_time_limit)
        time_limit = float(memory.budget.get("time_limit_s", time_limit))
        memory.stop = {}
        memory.run_manifest["resume_count"] = int(
            memory.run_manifest.get("resume_count", 0)
        ) + 1
        memory.run_manifest["last_resumed_at"] = run_started_at
    else:
        if memory_path.exists() or (candidates_dir / graph_ops.ROOT_CANDIDATE_ID).exists():
            raise FileExistsError(
                f"run directory already contains FDAgents state; use --resume: {run_dir}"
            )
        candidates_dir.mkdir(parents=True, exist_ok=True)
        root_artifact = materialize_candidate_artifact(
            input_dcp, candidates_dir / graph_ops.ROOT_CANDIDATE_ID
        )
        root_sha256 = sha256_file(root_artifact)
        if provenance_ledger is not None:
            provenance_ledger.admit_descendant(
                artifact_path=root_artifact,
                artifact_sha256=root_sha256,
                parent_artifact_sha256=no_replay_run.source_artifact_sha256,
            )
        atomic_copy_file(
            root_artifact, current_best_dcp, expected_sha256=root_sha256
        )
        if submission_mode:
            atomic_copy_file(
                root_artifact, output_dcp, expected_sha256=root_sha256
            )

    mcp = MCPClient(run_dir, debug=debug)
    kb = None if replay_enabled else KnowledgeBase.load(cfg)
    if cold_isolation and kb is not None and (kb.cases or kb.skills_guide):
        raise IsolationPolicyError(
            "COLD_ISOLATION_HISTORICAL_KNOWLEDGE_FORBIDDEN"
        )
    llm: Optional[LLMClient] = None
    stages: Optional[StageRunner] = None
    replay_stages: Optional[ReplayStageRunner] = None
    report_snapshot: Optional[CorpusSnapshot] = None
    report_snapshot_ref: dict = {}
    knowledge_snapshot: Optional[CorpusSnapshot] = None
    staged_react_enabled = False
    mcp_cleaned = False
    if use_llm:
        llm = LLMClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            cfg=cfg,
            submission_mode=submission_mode,
        )
        if replay_enabled:
            replay_stages = ReplayStageRunner(llm)
        else:
            stages = StageRunner(
                llm,
                kb,
                cfg,
                **({"cold_isolation": True} if cold_isolation else {}),
            )
            staged_react_enabled = bool(
                getattr(stages, "uses_staged_react", False)
            )
        if stages is not None and staged_react_enabled and hasattr(kb, "llm_corpus_documents"):
            knowledge_documents = kb.llm_corpus_documents()
            if knowledge_documents:
                knowledge_snapshot = CorpusSnapshot.create(
                    run_dir / "corpora",
                    scope="knowledge",
                    documents=knowledge_documents,
                    identity={"kind": "configured_fdagents_knowledge"},
                )

    def canonical_best_path() -> Path:
        if memory is not None and memory.candidate_graph:
            # Export what the regression exports.  submission_candidate_id
            # returns the best node only when its equivalence proof completed
            # inside the run, and otherwise falls back to the root -- shipping
            # the untouched input.  Our runs prove equivalence after the fact
            # instead, and it passes, so that fallback discards a candidate
            # that is sound.  Take the same node the measured runs take.
            best_id = memory.candidate_graph.get("current_best_candidate_id")
            node = memory.candidate_node(best_id)
            if node is not None:
                try:
                    return validate_graph_node_artifact(run_dir, node)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "[agent] graph best failed final identity validation: %s",
                        exc,
                    )
        expected_root = (
            str(memory.run_manifest.get("root_sha256") or "")
            if memory is not None else sha256_file(root_artifact)
        )
        if not input_dcp.is_file() or sha256_file(input_dcp) != expected_root:
            raise RuntimeError(
                "neither graph best nor immutable input passed final identity validation"
            )
        return input_dcp

    def export_best() -> None:
        best = canonical_best_path()
        if best.is_file():
            atomic_copy_file(
                best, output_dcp, expected_sha256=sha256_file(best)
            )

    def collect_committed_artifacts(action_dir: Optional[Path] = None) -> None:
        if not level1_gc_enabled or memory is None:
            return
        try:
            cleanup_level1_artifacts(
                memory,
                run_dir=run_dir,
                memory_path=memory_path,
                action_dir=action_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent] artifact GC failed safely: %s", exc)
            memory.artifact_gc.setdefault("failures", []).append({
                "reason": str(exc),
                "timestamp": time.time(),
            })
            memory.artifact_gc["failures"] = memory.artifact_gc["failures"][-20:]
            memory.save(memory_path)

    async def execute_with_run_deadline(factory, label: str):
        budget = memory.time_remaining() - time_safety
        if budget <= 0:
            raise asyncio.TimeoutError
        logger.info("[agent] %s execution deadline: %.0fs", label, budget)
        return await asyncio.wait_for(factory(), timeout=budget)

    async def execute_setup_with_run_deadline(factory, label: str):
        # Setup is bounded by the hard run deadline. The action safety reserve
        # must not prevent local resume/recovery work from reaching its normal
        # deadline-closeout path.
        budget = run_started_at + time_limit - time.time()
        if budget <= 0:
            raise asyncio.TimeoutError
        logger.info("[agent] %s setup deadline: %.0fs", label, budget)
        return await asyncio.wait_for(factory(), timeout=budget)

    try:
        await execute_setup_with_run_deadline(
            mcp.start_servers,
            "MCP server startup",
        )
        if resume:
            if replay_enabled:
                replay_control = memory.validate_replay_control(
                    replay_metadata
                )
                pending_context = memory.pending_transaction.get("policy_context")
                pending_selection = replay_control.get("pending_selection")
                if memory.pending_transaction and not isinstance(pending_context, dict):
                    raise RuntimeError(
                        "replay resume found a transaction without policy context"
                    )
                if memory.pending_transaction and not isinstance(
                    pending_selection, dict
                ):
                    raise RuntimeError(
                        "replay resume found a transaction without pending selection"
                    )
            pending_action_dir = (
                Path(memory.pending_transaction["action_dir"])
                if memory.pending_transaction.get("action_dir") else None
            )
            await recover_pending_action(
                memory,
                mcp,
                cfg,
                memory_path,
                promotion_delta,
                deadline_executor=execute_with_run_deadline,
                action_registry_path=action_registry_path,
            )
            publish_action_evidence_outbox(
                memory, cfg, memory_path, action_registry_path
            )
            if replay_enabled:
                replay_control = memory.validate_replay_control(
                    replay_metadata
                )
                if (
                    replay_control.get("pending_selection") is not None
                    and not memory.pending_transaction
                ):
                    memory.refresh_budget()
                    _retire_replay_selection_not_started(
                        memory,
                        cfg,
                        completed_at_s=float(memory.budget["time_elapsed"]),
                    )
                    memory.save(memory_path)
            if pending_action_dir is not None:
                collect_committed_artifacts(pending_action_dir)
            memory.project_graph_best()
            best_id = memory.candidate_graph["current_best_candidate_id"]
            best_node = memory.candidate_node(best_id)
            best_artifact = Path(best_node["artifact_path"])
            if not best_artifact.is_file():
                raise FileNotFoundError(f"resume best artifact missing: {best_artifact}")
            digest = sha256_file(best_artifact)
            if best_node.get("artifact_sha256") and best_node["artifact_sha256"] != digest:
                raise RuntimeError(f"resume best artifact hash mismatch: {best_id}")
            best_node["artifact_sha256"] = digest
            refresh_ids = dict.fromkeys((
                memory.candidate_graph["root_candidate_id"],
                best_id,
            ))
            if any(
                memory.candidate_node(candidate_id).get("placed_legal") is not True
                for candidate_id in refresh_ids
            ):
                for refresh_id in refresh_ids:
                    refresh_node = memory.candidate_node(refresh_id)
                    if refresh_node.get("placed_legal") is True:
                        continue
                    refresh_artifact = Path(refresh_node["artifact_path"])
                    try:
                        refreshed = await execute_with_run_deadline(
                            lambda refresh_artifact=refresh_artifact: measure_candidate(
                                mcp, refresh_artifact, cfg
                            ),
                            f"resume placement validation for {refresh_id}",
                        )
                    except asyncio.TimeoutError:
                        refreshed = failed_measurement(
                            "resume placement validation timed out"
                        )
                    apply_measurement_to_node(refresh_node, refreshed)
                root_id = memory.candidate_graph["root_candidate_id"]
                root_node = memory.candidate_node(root_id)
                if not root_node.get("eligible_for_beam"):
                    raise RuntimeError(
                        "resume immutable root failed fresh central measurement"
                    )
                refreshed_best = memory.candidate_node(best_id)
                if not refreshed_best.get("eligible_for_beam"):
                    best_id = root_id
                memory.candidate_graph["current_best_candidate_id"] = best_id
                refreshed_ids = [
                    candidate_id
                    for candidate_id in refresh_ids
                    if memory.candidate_node(candidate_id).get("eligible_for_beam")
                ]
                memory.candidate_graph["beam_candidate_ids"] = refreshed_ids[
                    :beam_width
                ]
                for candidate_id, node in memory.candidate_graph["nodes"].items():
                    node["in_active_beam"] = candidate_id in refreshed_ids[:beam_width]
                memory.project_graph_best()
                memory.save(memory_path)
                best_node = memory.candidate_node(best_id)
                best_artifact = Path(best_node["artifact_path"])
                digest = sha256_file(best_artifact)
            atomic_copy_file(
                best_artifact,
                current_best_dcp,
                expected_sha256=str(best_node.get("artifact_sha256") or ""),
            )
            export_best()
            best_measurement = measurement_from_node(best_node)
            try:
                async def reprofile_resumed_best():
                    await mcp.call_vivado(
                        "open_checkpoint",
                        {"dcp_path": str(best_artifact.resolve())},
                        timeout=600.0,
                    )
                    return await collect_current_design(
                        mcp,
                        cfg,
                        require_timing_profile=not staged_react_enabled,
                        central_measurement=best_measurement,
                        capture_reports=False,
                        allow_partial=staged_react_enabled,
                        operator_mining_candidate=(
                            best_node if staged_react_enabled else None
                        ),
                        recurrence_lineage_active=(
                            validate_full_recurrence_lineage(
                                memory, run_dir, best_node
                            )
                            if staged_react_enabled
                            else False
                        ),
                        cold_isolation=cold_isolation,
                        run_id=(no_replay_run.run_id if no_replay_run else ""),
                        telemetry_epoch=int(
                            memory.current_telemetry.get("epoch", -1)
                        ) + 1,
                        same_run_feedback_by_skill=build_current_lineage_action_feedback(
                            memory,
                            seed_artifact_sha256=str(best_node["artifact_sha256"]),
                        ),
                    )

                analysis = await execute_with_run_deadline(
                    reprofile_resumed_best,
                    f"resume telemetry for {best_id}",
                )
                telemetry_refreshed = True
            except asyncio.TimeoutError:
                telemetry_refreshed = False
                prior = memory.current_telemetry or {}
                prior_snapshot = prior.get("report_snapshot") or {}
                prior_identity = prior_snapshot.get("identity") or {}
                prior_exact = (
                    prior.get("candidate_id") == best_id
                    and prior_identity.get("candidate_id") == best_id
                    and prior_identity.get("artifact_sha256") == digest
                )
                analysis = {
                    "baseline_wns": best_measurement.get("wns_ns"),
                    "clock_period": best_measurement.get("clock_period_ns"),
                    "timing_summary": {
                        "wns": best_measurement.get("wns_ns"),
                        "tns": best_measurement.get("tns_ns"),
                        "whs": best_measurement.get("whs_ns"),
                        "wpws": best_measurement.get("wpws_ns"),
                    },
                    "fanout_nets": (prior.get("fanout_nets") or []) if prior_exact else [],
                    "timing_profile": (
                        prior.get("timing_profile") or {}
                    ) if prior_exact else {},
                    "utilization": (prior.get("utilization") or {}) if prior_exact else {},
                    "congestion": (prior.get("congestion") or {}) if prior_exact else {},
                    "replica_population": (
                        prior.get("replica_population") or {}
                    ) if prior_exact else {},
                    "carry_clouds": (
                        prior.get("carry_clouds") or {}
                    ) if prior_exact else {},
                    "hard_macro_cascades": (
                        prior.get("hard_macro_cascades") or {}
                    ) if prior_exact else {},
                    "_telemetry_availability": (
                        prior.get("telemetry_availability") or {}
                    ) if prior_exact else {
                        "timing": True,
                        "fanout": False,
                        "timing_profile": False,
                        "utilization": False,
                    },
                    "_replay_semantic_evidence": (
                        prior.get("replay_semantic_evidence") or {}
                    ) if prior_exact else {},
                    "_telemetry_complete": bool(prior_exact),
                }
                memory.record_telemetry_failure(
                    epoch=int(prior.get("epoch", 0)) + 1,
                    source_round=memory.round,
                    candidate_id=best_id,
                    dcp_path=str(best_artifact.resolve()),
                    reason="resume telemetry skipped at total-run deadline",
                )
                if prior_exact and prior_snapshot.get("manifest_path"):
                    try:
                        loaded_snapshot = CorpusSnapshot.load(
                            Path(prior_snapshot["manifest_path"]),
                            expected_scope="report",
                        )
                        if (
                            loaded_snapshot.snapshot_id != prior_snapshot.get("snapshot_id")
                            or loaded_snapshot.identity.get("candidate_id") != best_id
                            or loaded_snapshot.identity.get("artifact_sha256") != digest
                        ):
                            raise RuntimeError("prior report snapshot identity mismatch")
                        report_snapshot = loaded_snapshot
                        report_snapshot_ref = dict(prior_snapshot)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[agent] prior report snapshot unavailable: %s", exc)
                if staged_react_enabled:
                    cached = load_profiled_report_snapshot(memory, best_node)
                    if cached is None:
                        reason = (
                            "resume exact-seed report collection reached the run "
                            "deadline and no bound snapshot was reusable"
                        )
                        memory.record_stop(
                            source="agent",
                            reason="exact_seed_reprofile_failed",
                            rationale=reason,
                            hard_mechanical=True,
                        )
                        memory.save(memory_path)
                        export_best()
                        return 1
                    report_snapshot, report_snapshot_ref, analysis = cached
                    telemetry_refreshed = False
            except Exception as exc:  # noqa: BLE001
                if not staged_react_enabled:
                    raise
                memory.record_telemetry_failure(
                    epoch=memory.next_telemetry_epoch(),
                    source_round=memory.round,
                    candidate_id=best_id,
                    dcp_path=str(best_artifact.resolve()),
                    reason=f"resume supplemental report collection failed: {exc}",
                )
                cached = load_profiled_report_snapshot(memory, best_node)
                if cached is None:
                    reason = f"resume exact-seed reprofile failed: {exc}"
                    memory.record_stop(
                        source="agent",
                        reason="exact_seed_reprofile_failed",
                        rationale=reason,
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    export_best()
                    return 1
                report_snapshot, report_snapshot_ref, analysis = cached
                telemetry_refreshed = False
            baseline_wns = float(memory.baseline.get("wns") or 0.0)
            clock_period = memory.baseline.get("clock_period")
            features = extract_features(analysis, design_id=input_dcp.stem)
            telemetry_complete = bool(analysis.get("_telemetry_complete"))
            matches = (
                []
                if replay_enabled or staged_react_enabled
                else kb.match(features, cfg=cfg)
                if telemetry_complete
                else []
            )
            target_presets = (
                {} if replay_enabled or staged_react_enabled
                else kb.target_presets(matches)
            )
            rendered_matches = (
                [] if replay_enabled or staged_react_enabled
                else kb.render_matches(matches)
            )
            if not replay_enabled and not staged_react_enabled:
                seed_suggested_targets(memory, analysis)
            if telemetry_refreshed:
                next_epoch = int(memory.current_telemetry.get("epoch", 0)) + 1
                if staged_react_enabled:
                    report_snapshot, report_snapshot_ref = publish_report_snapshot(
                        run_dir,
                        analysis,
                        candidate_id=best_id,
                        artifact_sha256=best_node["artifact_sha256"],
                        telemetry_epoch=next_epoch,
                    )
                memory.record_telemetry_epoch(
                    epoch=next_epoch,
                    source_round=memory.round,
                    candidate_id=best_id,
                    dcp_path=str(best_artifact.resolve()),
                    analysis=analysis,
                    features=features.to_dict(),
                    matched_cases=matches,
                    report_snapshot=report_snapshot_ref,
                )
                if staged_react_enabled and not telemetry_complete:
                    memory.record_telemetry_failure(
                        epoch=next_epoch,
                        source_round=memory.round,
                        candidate_id=best_id,
                        dcp_path=str(best_artifact.resolve()),
                        reason=(
                            "resume report snapshot is exact but rich parsed telemetry "
                            "is incomplete"
                        ),
                    )
            elif staged_react_enabled and report_snapshot is not None:
                memory.record_telemetry_epoch(
                    epoch=int(report_snapshot.identity["telemetry_epoch"]),
                    source_round=memory.round,
                    candidate_id=best_id,
                    dcp_path=str(best_artifact.resolve()),
                    analysis=analysis,
                    features=features.to_dict(),
                    matched_cases=matches,
                    report_snapshot=report_snapshot_ref,
                    append_history=False,
                )
            memory.save(memory_path)
        else:
            # The owned root is graph truth. Initial rich telemetry reuses this
            # central timing result and only adds profile/fanout/utilization data.
            root_validation_started = time.monotonic()
            root_measurement = await execute_setup_with_run_deadline(
                lambda: measure_candidate(mcp, root_artifact, cfg),
                "immutable root measurement",
            )
            root_validation_runtime = time.monotonic() - root_validation_started
            root_state = (
                "active" if root_measurement["eligible_for_beam"] else "ineligible"
            )
            root_node = graph_ops.make_node(
                candidate_id=graph_ops.ROOT_CANDIDATE_ID,
                parent_candidate_id=None,
                depth=0,
                created_round=0,
                created_at=time.time(),
                artifact_path=str(root_artifact.resolve()),
                artifact_state="materialized",
                artifact_size_bytes=root_artifact.stat().st_size,
                artifact_sha256=sha256_file(root_artifact),
                search_state=root_state,
                summary="immutable input root",
                measurement=root_measurement,
                validation_runtime_s=root_validation_runtime,
                cumulative_lineage_runtime_s=0.0,
                promoted_to_global_best=True,
                in_active_beam=bool(root_measurement["eligible_for_beam"]),
            )
            baseline_wns = float(root_measurement.get("wns_ns") or 0.0)
            clock_period = root_measurement.get("clock_period_ns")
            memory = Memory.init(
                design_id=input_dcp.stem,
                baseline_wns=baseline_wns,
                clock_period=clock_period,
                bottleneck="unknown",
                input_dcp=str(root_artifact.resolve()),
                time_limit_s=time_limit,
                token_budget_usd=float(cfg.get("llm.token_budget_usd", 1.0)),
                rank1_score=rank1_score,
                score_baseline_fmax_mhz=score_baseline_fmax_mhz,
            )
            memory.validation_count = 1
            memory.budget["start_time"] = run_started_at
            memory.initialize_candidate_graph(root_node, beam_width)
            memory.run_manifest = {
                "version": 2,
                "canonical_run_dir": str(run_dir),
                "input_dcp": str(input_dcp),
                "output_dcp": str(output_dcp),
                "root_sha256": root_node["artifact_sha256"],
                "created_at": run_started_at,
                "resume_count": 0,
                "execution_mode": (
                    "strict_submission" if submission_mode else "experiment"
                ),
                "strategy_mode": (
                    f"replay:{REPLAY_POLICY_ID}" if replay_enabled else "autonomous"
                ),
                "deadline_at": run_started_at + time_limit,
                "hard_target": dict(memory.hard_target),
            }
            if dcp_admission is not None:
                if engineering_regression:
                    memory.run_manifest["dcp_admission"] = {
                        "execution_scope": "engineering_regression",
                        "not_generalization_evidence": True,
                        "manifest_sha256": dcp_admission["manifest_sha256"],
                        "source_dcp_sha256": dcp_admission[
                            "source_dcp_sha256"
                        ],
                        "matching_slots": list(dcp_admission["matching_slots"]),
                    }
                else:
                    memory.run_manifest["dcp_admission"] = {
                        "execution_scope": "development",
                        "training_role": "development",
                        "source_dcp_sha256": root_node["artifact_sha256"],
                        "lineage_group": dcp_admission["lineage_group"],
                    }
            if no_replay_run is not None and provenance_ledger is not None:
                memory.run_manifest["no_replay_provenance"] = {
                    "schema_version": "no-replay-provenance-v1",
                    "run_id": no_replay_run.run_id,
                    "source_artifact_sha256": no_replay_run.source_artifact_sha256,
                    "source_receipt": provenance_ledger.receipts[
                        str(no_replay_run.source_path)
                    ].as_dict(),
                    "root_receipt": provenance_ledger.receipts[
                        str(root_artifact.resolve())
                    ].as_dict(),
                }
            if isolation_artifact is not None:
                memory.run_manifest["isolation"] = isolation_artifact
            if replay_enabled:
                memory.initialize_replay_control(
                    replay_metadata,
                    replay_policy.initial_state().to_dict(),
                    provenance=replay_provenance,
                    recorded_at=run_started_at,
                )
            memory.refresh_budget()
            memory.save(memory_path)
            if submission_mode:
                export_best()

            if not root_measurement["eligible_for_beam"]:
                logger.error(
                    "[agent] root central measurement is incomplete/illegal: %s",
                    root_measurement.get("legality_reason"),
                )
                memory.record_stop(
                    source="agent",
                    reason="root_measurement_invalid",
                    rationale=root_measurement.get("legality_reason") or "unknown",
                    hard_mechanical=True,
                )
                memory.save(memory_path)
                export_best()
                return 1

            root_profile_started = time.monotonic()
            analysis = await execute_with_run_deadline(
                lambda: collect_current_design(
                    mcp,
                    cfg,
                    central_measurement=root_measurement,
                    capture_reports=False,
                    allow_partial=staged_react_enabled,
                    operator_mining_candidate=(
                        root_node if staged_react_enabled else None
                    ),
                    recurrence_lineage_active=False,
                    cold_isolation=cold_isolation,
                    run_id=(no_replay_run.run_id if no_replay_run else ""),
                    telemetry_epoch=0,
                    same_run_feedback_by_skill=build_current_lineage_action_feedback(
                        memory,
                        seed_artifact_sha256=str(root_node["artifact_sha256"]),
                    ),
                ),
                "immutable root telemetry",
            )
            analysis["_profile_runtime_s"] = max(
                0.0, time.monotonic() - root_profile_started
            )
            root_node = memory.candidate_node(graph_ops.ROOT_CANDIDATE_ID)
            root_node["physical_signature"] = build_physical_signature(
                analysis.get("timing_profile") or {},
                root_measurement,
                artifact_sha256=root_node["artifact_sha256"],
            )
            memory.candidate_graph["beam_retention"] = {
                "version": 1,
                "physical_diversity_enabled": bool(
                    cfg.get("run.candidate_beam_diversity_enabled", True)
                ),
                "minimum_common_dimensions": int(
                    cfg.get(
                        "run.candidate_beam_diversity_min_common_dimensions", 4
                    )
                ),
                "method": "greedy_max_min_within_truncated_pareto_front",
                "fallback": "nsga2_crowding",
            }
            logger.info(
                "[agent] Baseline: WNS=%.3f clock=%s LUTs=%s route%%=%s",
                baseline_wns,
                clock_period,
                (analysis.get("utilization") or {}).get("luts"),
                (analysis.get("timing_profile") or {}).get("avg_route_pct"),
            )
            features = extract_features(analysis, design_id=input_dcp.stem)
            telemetry_complete = bool(analysis.get("_telemetry_complete"))
            matches = (
                []
                if replay_enabled or staged_react_enabled
                else kb.match(features, cfg=cfg)
                if telemetry_complete
                else []
            )
            target_presets = (
                {} if replay_enabled or staged_react_enabled
                else kb.target_presets(matches)
            )
            rendered_matches = (
                [] if replay_enabled or staged_react_enabled
                else kb.render_matches(matches)
            )
            memory.baseline["timing_profile"] = analysis.get("timing_profile") or {}
            memory.baseline["features"] = features.to_dict()
            if not replay_enabled and not staged_react_enabled:
                seed_suggested_targets(memory, analysis)
            if staged_react_enabled:
                report_snapshot, report_snapshot_ref = publish_report_snapshot(
                    run_dir,
                    analysis,
                    candidate_id=graph_ops.ROOT_CANDIDATE_ID,
                    artifact_sha256=root_node["artifact_sha256"],
                    telemetry_epoch=0,
                )
            memory.record_telemetry_epoch(
                epoch=0,
                source_round=0,
                candidate_id=graph_ops.ROOT_CANDIDATE_ID,
                dcp_path=str(root_artifact.resolve()),
                analysis=analysis,
                features=features.to_dict(),
                matched_cases=matches,
                report_snapshot=report_snapshot_ref,
            )
            if staged_react_enabled and not telemetry_complete:
                memory.record_telemetry_failure(
                    epoch=0,
                    source_round=0,
                    candidate_id=graph_ops.ROOT_CANDIDATE_ID,
                    dcp_path=str(root_artifact.resolve()),
                    reason=(
                        "root report snapshot is exact but rich parsed telemetry "
                        "is incomplete"
                    ),
                )
        runtime_estimates = cfg.get("skills.runtime_estimates_s", {}) or {}
        candidate_runtime_estimates = (
            cfg.get("skills.candidate_runtime_estimates_s", {}) or {}
        )
        state_kwargs = {
            "runtime_estimates_s": runtime_estimates,
            "candidate_runtime_estimates_s": candidate_runtime_estimates,
            "max_skill_candidates_per_action": skill_candidate_cap,
            "central_measurement_estimate_s": float(
                central_measurement_estimate
            ),
            "candidate_commit_reserve_s": candidate_commit_reserve,
            "calibration_records_in_llm_state": int(
                cfg.get("score_scheduling.calibration_records_in_llm_state", 8)
            ),
            "time_safety_s": time_safety,
            "platform_id": cfg.get("platform.id", "unspecified"),
            "saturation_config": cfg.get("saturation.level1", {}) or {},
        }

        if (
            not resume
            and stages is not None
            and not bool(getattr(stages, "uses_staged_react", False))
            and memory.tokens_remaining() > 0
        ):
            try:
                plan = stages.analyze(
                    memory.state_for_llm(**state_kwargs),
                    rendered_matches,
                )
                memory.record_token_spend(llm.last_call_cost)
                memory.plan = {
                    "bottleneck": plan.bottleneck,
                    "diagnosis": plan.diagnosis,
                    "strategy": plan.strategy,
                    "case_verdict": plan.case_verdict,
                    "stop_hint": plan.stop_hint,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("[agent] analyze stage failed: %s", exc)

        fallback_plan = build_fallback_plan(matches, memory, cfg)
        fallback_idx = 0
        veto_streak = 0
        pending_probe_result: Optional[dict] = None
        memory.save(memory_path)

        async def open_node(node: dict, label: str) -> None:
            artifact = validate_graph_node_artifact(run_dir, node)
            await execute_with_run_deadline(
                lambda: mcp.call_vivado(
                    "open_checkpoint",
                    {"dcp_path": str(artifact)},
                    timeout=600.0,
                ),
                label,
            )

        async def restart_vivado_at_node(node: dict, label: str) -> dict:
            artifact = validate_graph_node_artifact(run_dir, node)
            restart = getattr(mcp, "restart_vivado", None)
            if not callable(restart):
                raise RuntimeError("MCP client does not support Vivado restart")
            status = await execute_with_run_deadline(
                lambda: restart(
                    restore_checkpoint=artifact,
                    expected_sha256=str(node["artifact_sha256"]),
                    timeout=600.0,
                ),
                label,
            )
            if (
                not isinstance(status, dict)
                or status.get("checkpoint_restored") is not True
                or status.get("checkpoint_path") != str(artifact)
            ):
                raise RuntimeError(
                    "Vivado restart did not prove exact incumbent restoration"
                )
            return status

        async def refresh_replay_semantic_profile() -> dict:
            """Refresh exact graph-best telemetry and action-specific preflight."""
            nonlocal analysis, features, matches, target_presets, rendered_matches
            best_id = str(memory.candidate_graph["current_best_candidate_id"])
            node = memory.candidate_node(best_id)
            if node is None or node.get("eligible_for_beam") is not True:
                raise RuntimeError("replay incumbent is not centrally selectable")
            artifact = validate_graph_node_artifact(run_dir, node)
            await open_node(node, f"replay semantic preflight {best_id}")
            analysis = await execute_with_run_deadline(
                lambda: collect_current_design(
                    mcp,
                    cfg,
                    require_timing_profile=True,
                    central_measurement=measurement_from_node(node),
                    capture_reports=False,
                    allow_partial=False,
                    cold_isolation=cold_isolation,
                ),
                f"replay exact telemetry {best_id}",
            )
            analysis["_replay_semantic_evidence"] = (
                await execute_with_run_deadline(
                    lambda: collect_replay_semantic_evidence(
                        mcp,
                        cfg,
                        incumbent=node,
                    ),
                    f"replay semantic evidence {best_id}",
                )
            )
            features = extract_features(analysis, design_id=input_dcp.stem)
            matches = []
            target_presets = {}
            rendered_matches = []
            epoch = memory.next_telemetry_epoch()
            memory.record_telemetry_epoch(
                epoch=epoch,
                source_round=memory.round,
                candidate_id=best_id,
                dcp_path=str(artifact),
                analysis=analysis,
                features=features.to_dict(),
                matched_cases=[],
                report_snapshot={},
            )
            memory.save(memory_path)
            return build_replay_observation(analysis)

        async def activate_staged_seed_profile(
            node: dict, *, force_refresh: bool = False
        ) -> bool:
            """Activate cached exact telemetry or authoritatively profile one seed."""
            nonlocal analysis, features, matches, target_presets, rendered_matches
            nonlocal report_snapshot, report_snapshot_ref
            candidate_id = str(node["candidate_id"])
            artifact_path = Path(str(node["artifact_path"])).resolve()
            artifact_sha256 = sha256_file(artifact_path)
            if artifact_sha256 != node.get("artifact_sha256"):
                raise RuntimeError(
                    f"exact seed artifact hash mismatch for {candidate_id}"
                )

            cached = None if force_refresh else load_profiled_report_snapshot(memory, node)
            if cached is not None:
                report_snapshot, report_snapshot_ref, analysis = cached
                epoch = int(report_snapshot.identity["telemetry_epoch"])
                append_history = False
                logger.info(
                    "[agent] reusing exact report snapshot %s for %s",
                    report_snapshot.snapshot_id,
                    candidate_id,
                )
            else:
                epoch = memory.next_telemetry_epoch()
                measurement = measurement_from_node(node)
                await open_node(node, f"exact seed reprofile {candidate_id}")
                seed_profile_started = time.monotonic()
                analysis = await execute_with_run_deadline(
                    lambda: collect_current_design(
                        mcp,
                        cfg,
                        central_measurement=measurement,
                        capture_reports=False,
                        allow_partial=True,
                        operator_mining_candidate=node,
                        retry_failed_action_menu_families=force_refresh,
                        recurrence_lineage_active=validate_full_recurrence_lineage(
                            memory, run_dir, node
                        ),
                        cold_isolation=cold_isolation,
                        run_id=(no_replay_run.run_id if no_replay_run else ""),
                        telemetry_epoch=epoch,
                        same_run_feedback_by_skill=build_current_lineage_action_feedback(
                            memory,
                            seed_artifact_sha256=artifact_sha256,
                        ),
                    ),
                    f"exact seed report collection {candidate_id}",
                )
                analysis["_profile_runtime_s"] = max(
                    0.0, time.monotonic() - seed_profile_started
                )
                report_snapshot, report_snapshot_ref = publish_report_snapshot(
                    run_dir,
                    analysis,
                    candidate_id=candidate_id,
                    artifact_sha256=artifact_sha256,
                    telemetry_epoch=epoch,
                )
                append_history = True

            features = extract_features(analysis, design_id=input_dcp.stem)
            telemetry_complete = bool(analysis.get("_telemetry_complete"))
            matches = []
            target_presets = {}
            rendered_matches = []
            memory.record_telemetry_epoch(
                epoch=epoch,
                source_round=memory.round,
                candidate_id=candidate_id,
                dcp_path=str(artifact_path),
                analysis=analysis,
                features=features.to_dict(),
                matched_cases=matches,
                report_snapshot=report_snapshot_ref,
                append_history=append_history,
            )
            if not telemetry_complete and append_history:
                memory.record_telemetry_failure(
                    epoch=epoch,
                    source_round=memory.round,
                    candidate_id=candidate_id,
                    dcp_path=str(artifact_path),
                    reason=(
                        "exact seed report snapshot is authoritative but rich parsed "
                        "telemetry is incomplete"
                    ),
                )
            memory.save(memory_path)
            return not append_history

        def record_degraded_staged_epoch(
            *, node: dict, epoch: int, reason: str, exc: BaseException
        ) -> None:
            """Install a seed-bound menu when promoted-seed profiling fails."""
            seed_sha = str(node["artifact_sha256"])
            menu = build_degraded_action_menu(
                seed_candidate_id=str(node["candidate_id"]),
                seed_artifact_sha256=seed_sha,
                error_code=_action_menu_failure_code(exc),
                error_detail=reason,
                top_k=int(cfg.get("action_menu.top_k", 4)),
                auxiliary_actions=build_epoch_auxiliary_actions(
                    cfg, seed_sha256=seed_sha
                ),
                cold_isolation=cold_isolation,
            )
            measurement = measurement_from_node(node)
            degraded_analysis = {
                "clock_period": measurement.get("clock_period_ns"),
                "baseline_wns": measurement.get("wns_ns"),
                "_telemetry_complete": False,
                "_authorized_action_menu": menu,
                "_action_menu_timing": {
                    "schema_version": "action-menu-epoch-cost-v1",
                    "provider_entry": "recover_cold_product_sum_families",
                    "top_k": int(cfg.get("action_menu.top_k", 4)),
                },
                "_action_menu_build_status": {
                    "schema_version": "action-menu-build-status-v1",
                    "state": "degraded",
                    "error_code": _action_menu_failure_code(exc),
                    "retry_count": 0,
                    "provider_entry": "recover_cold_product_sum_families",
                    "seed_artifact_sha256": seed_sha,
                },
            }
            memory.record_telemetry_epoch(
                epoch=epoch,
                source_round=memory.round,
                candidate_id=str(node["candidate_id"]),
                dcp_path=str(node["artifact_path"]),
                analysis=degraded_analysis,
                features={},
                matched_cases=[],
                report_snapshot={},
            )
            memory.record_telemetry_failure(
                epoch=epoch,
                source_round=memory.round,
                candidate_id=str(node["candidate_id"]),
                dcp_path=str(node["artifact_path"]),
                reason=reason,
            )

        async def restore_known_node(preferred: Optional[dict] = None) -> None:
            node = preferred
            if node is not None:
                current = memory.candidate_node(node.get("candidate_id"))
                if not current or not current.get("in_active_beam"):
                    node = None
            if node is None:
                node = memory.candidate_node(
                    memory.candidate_graph["current_best_candidate_id"]
                )
            if node is not None:
                try:
                    await open_node(node, f"restore {node['candidate_id']}")
                except Exception as exc:  # noqa: BLE001
                    if not callable(getattr(mcp, "restart_vivado", None)):
                        logger.error("[agent] failed to restore known candidate: %s", exc)
                        return
                    try:
                        await execute_with_run_deadline(
                            lambda: restart_vivado_at_node(
                                node,
                                f"restart Vivado and restore {node['candidate_id']}",
                            ),
                            f"restart Vivado and restore {node['candidate_id']}",
                        )
                        logger.warning(
                            "[agent] restarted Vivado and restored candidate %s",
                            node["candidate_id"],
                        )
                    except Exception as restart_exc:  # noqa: BLE001
                        logger.error(
                            "[agent] failed to restore known candidate (%s); "
                            "Vivado restart also failed: %s",
                            exc,
                            restart_exc,
                        )

        # Staged search is wall-clock bounded. Its configured round count is a
        # legacy soft guard and must not discard untried menu mechanisms while
        # usable execution time remains. Legacy/no-LLM paths keep their finite
        # deterministic-plan guard.
        # A provider transport or schema rejection is an infrastructure outcome.
        # It must not be able to retire the run: the reasoning layer never got a
        # chance to produce a decision, so the remaining budget is still usable.
        provider_failure_retries = 0
        max_provider_failure_retries = max(
            0, int(cfg.get("react.provider_failure_retries", 3))
        )
        while memory.round < max_rounds or staged_react_enabled:
            decide_provider_failure = False
            prior_beam = list(memory.candidate_graph.get("beam_candidate_ids", []))
            memory.update_candidate_beam(memory.round)
            if prior_beam != memory.candidate_graph.get("beam_candidate_ids", []):
                memory.save(memory_path)
            remaining = memory.time_remaining()
            # Publish the usable optimization window so read-only evidence
            # collection can refuse work it cannot pay for.
            set_optimization_budget(remaining - time_safety)
            if remaining <= time_safety:
                logger.info("[agent] time safety window reached (%.0fs left)", remaining)
                if not replay_enabled:
                    memory.record_stop(
                        source="agent",
                        reason="hard_deadline_safety_window",
                        rationale=(
                            f"{remaining:.3f}s remained with "
                            f"{time_safety:.3f}s reserved"
                        ),
                        hard_mechanical=True,
                    )
                break

            current_runtime_estimates, observed_measurement_estimate = (
                calibrated_same_run_runtime_estimates(memory, runtime_estimates)
            )
            current_measurement_estimate = (
                observed_measurement_estimate
                if observed_measurement_estimate is not None
                else float(central_measurement_estimate)
            )
            state_kwargs["runtime_estimates_s"] = current_runtime_estimates
            state_kwargs["central_measurement_estimate_s"] = (
                current_measurement_estimate
            )

            if (
                staged_react_enabled
                and action_menu_retry_required(
                    memory.current_telemetry.get("action_menu_build_status"),
                    max_retries=int(cfg.get("action_menu.max_build_retries", 1)),
                    current_round=memory.round,
                )
            ):
                prior_status = dict(
                    memory.current_telemetry.get("action_menu_build_status") or {}
                )
                retry_node = memory.candidate_node(
                    str(memory.current_telemetry.get("candidate_id") or "")
                )
                if retry_node is None:
                    raise RuntimeError("degraded action menu retry has no exact seed node")
                try:
                    await open_node(
                        retry_node,
                        f"family-slice action-menu retry {retry_node['candidate_id']}",
                    )
                    refreshed = await execute_with_run_deadline(
                        lambda: retry_action_menu_product_sum_family_slice(
                            mcp,
                            cfg,
                            incumbent=retry_node,
                            current_telemetry=memory.current_telemetry,
                            run_id=(no_replay_run.run_id if no_replay_run else ""),
                            cold_isolation=cold_isolation,
                        ),
                        "failed product-sum family-slice retry",
                    )
                    memory.current_telemetry.update(refreshed)
                    logger.info(
                        "[agent] refreshed only failed product-sum family slices "
                        "for seed %s",
                        retry_node["candidate_id"],
                    )
                except Exception as exc:  # keep the prior partial menu usable
                    retained_status = dict(
                        memory.current_telemetry.get(
                            "action_menu_build_status"
                        ) or prior_status
                    )
                    retained_status["retry_count"] = int(
                        prior_status.get("retry_count", 0)
                    ) + 1
                    retained_status["retry_error_code"] = (
                        _action_menu_failure_code(exc)
                    )
                    retained_status["retry_scope"] = (
                        "failed_product_sum_family_slices_only"
                    )
                    memory.current_telemetry[
                        "action_menu_build_status"
                    ] = retained_status
                    logger.warning(
                        "[agent] bounded family-slice retry failed; retaining "
                        "the existing seed menu: %s",
                        exc,
                    )
                memory.save(memory_path)

            decision: Optional[Decision] = None
            replay_authorization: Optional[SelectionAuthorization] = None
            replay_policy_context: Optional[dict] = None
            decision_probe_result = pending_probe_result
            pending_probe_result = None
            seed_switches = 0
            if replay_enabled:
                replay_control = memory.validate_replay_control(
                    replay_metadata
                )
                replay_state = ReplayState.from_dict(replay_control["state"])
                memory.refresh_budget()
                replay_elapsed = float(memory.budget["time_elapsed"])
                replay_observation = build_replay_observation(analysis)
                replay_phase = replay_policy.phase(
                    replay_state,
                    replay_elapsed,
                    replay_observation,
                )
                if replay_phase == "characterize":
                    replay_state = replay_policy.characterize(
                        replay_state, replay_observation
                    )
                    memory.set_replay_state(replay_state.to_dict())
                    memory.save(memory_path)
                    continue
                if replay_phase == "semantic_preflight":
                    try:
                        replay_observation = await refresh_replay_semantic_profile()
                        memory.refresh_budget()
                        replay_elapsed = float(memory.budget["time_elapsed"])
                        replay_state = replay_policy.preflight(
                            replay_state,
                            replay_observation,
                            replay_elapsed,
                        )
                    except Exception as exc:  # noqa: BLE001
                        memory.record_stop(
                            source="agent",
                            reason="replay_semantic_preflight_failed",
                            rationale=str(exc)[:500],
                            hard_mechanical=True,
                        )
                        memory.save(memory_path)
                        break
                    memory.set_replay_state(replay_state.to_dict())
                    memory.save(memory_path)
                    continue
                if replay_phase in {"closeout", "submission", "complete"}:
                    break
                if replay_phase not in {"basin", "semantics"}:
                    raise RuntimeError(f"unsupported replay phase {replay_phase!r}")

                eligible_ids = replay_policy.eligible_action_ids(
                    replay_state,
                    replay_observation,
                    replay_elapsed,
                )
                if not eligible_ids:
                    memory.record_stop(
                        source="agent",
                        reason="replay_no_eligible_action",
                        rationale=f"phase {replay_phase} had no eligible action",
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    break
                llm_payload = None
                if replay_stages is not None:
                    if memory.tokens_remaining() <= 0:
                        memory.record_stop(
                            source="agent",
                            reason="replay_token_budget_exhausted",
                            rationale="replay selector has no remaining LLM budget",
                            hard_mechanical=True,
                        )
                        memory.save(memory_path)
                        break
                    llm_cost_before = float(getattr(llm, "total_cost", 0.0))
                    try:
                        llm_payload = replay_stages.select_action(
                            replay_observation,
                            eligible_ids,
                            deadline_monotonic=(
                                time.monotonic()
                                + max(0.0, memory.time_remaining() - time_safety)
                            ),
                            max_cost_usd=memory.tokens_remaining(),
                        )
                    except Exception as exc:  # noqa: BLE001
                        spent = max(
                            0.0,
                            float(getattr(llm, "total_cost", llm_cost_before))
                            - llm_cost_before,
                        )
                        if spent:
                            memory.record_token_spend(spent)
                        memory.record_stop(
                            source="agent",
                            reason="replay_selector_failed",
                            rationale=str(exc)[:500],
                            hard_mechanical=True,
                        )
                        memory.save(memory_path)
                        break
                    memory.record_token_spend(max(
                        0.0,
                        float(getattr(llm, "total_cost", llm_cost_before))
                        - llm_cost_before,
                    ))
                memory.refresh_budget()
                selection_elapsed = float(memory.budget["time_elapsed"])
                if replay_policy.phase(
                    replay_state,
                    selection_elapsed,
                    replay_observation,
                ) != replay_phase:
                    continue
                replay_authorization = replay_policy.select(
                    replay_state,
                    replay_observation,
                    selection_elapsed,
                    llm_payload,
                )
                if replay_authorization is None:
                    raise RuntimeError("replay selection returned no authorization")
                incumbent_id = str(
                    memory.candidate_graph["current_best_candidate_id"]
                )
                incumbent = memory.candidate_node(incumbent_id)
                if (
                    incumbent is None
                    or incumbent.get("eligible_for_beam") is not True
                    or incumbent.get("fmax_mhz") is None
                ):
                    raise RuntimeError("replay incumbent failed exact private binding")
                validate_graph_node_artifact(run_dir, incumbent)
                replay_resolved = resolve_replay_action(
                    replay_authorization.action
                )
                replay_policy_context = {
                    "schema_version": 1,
                    "authorization": replay_authorization.to_dict(),
                    "incumbent_binding": {
                        "candidate_id": incumbent_id,
                        "artifact_sha256": incumbent["artifact_sha256"],
                        "fmax_mhz": float(incumbent["fmax_mhz"]),
                    },
                    "resolved_action_fingerprint": (
                        replay_resolved.action_fingerprint
                    ),
                    "commit_contract": {
                        "schema_version": 1,
                        "promotion_min_delta_mhz": float(promotion_delta),
                    },
                }
                memory.set_replay_pending_selection(replay_policy_context)
                memory.save(memory_path)
                decision = Decision(
                    action="run_skill",
                    skill=replay_authorization.skill,
                    target=replay_authorization.target,
                    params=replay_authorization.params_dict(),
                    expectation=(
                        f"replay policy {REPLAY_POLICY_ID} action "
                        f"{replay_authorization.action_id}"
                    ),
                    seed_candidate_id=incumbent_id,
                )

            while (
                not replay_enabled
                and stages is not None
                and memory.tokens_remaining() > 0
            ):
                llm_cost_before = float(getattr(llm, "total_cost", 0.0))
                try:
                    current_menu = validate_action_menu(
                        memory.current_telemetry.get("authorized_action_menu")
                    )
                    base_menu = memory.current_telemetry.get(
                        "authorized_action_menu_unprojected"
                    )
                    if base_menu is None:
                        base_menu = copy.deepcopy(current_menu)
                        memory.current_telemetry[
                            "authorized_action_menu_unprojected"
                        ] = copy.deepcopy(base_menu)
                    base_menu = refresh_action_menu_same_run_feedback(
                        base_menu,
                        feedback_by_skill=build_current_lineage_action_feedback(
                            memory,
                            seed_artifact_sha256=str(
                                base_menu["seed_artifact_sha256"]
                            ),
                        ),
                    )
                    memory.current_telemetry[
                        "authorized_action_menu_unprojected"
                    ] = copy.deepcopy(base_menu)
                    projected_menu = filter_action_menu_for_execution_window(
                        base_menu,
                        execution_window_s=max(
                            0.0,
                            memory.time_remaining() - time_safety,
                        ),
                        runtime_estimates_s=current_runtime_estimates,
                        central_measurement_estimate_s=(
                            current_measurement_estimate
                        ),
                        candidate_commit_reserve_s=(
                            candidate_commit_reserve
                        ),
                        measured_runtimes_s=_ACTION_COST_OBSERVED,
                        measured_full_implementation_s=(
                            _IMPLEMENTATION_COST_OBSERVED.get("worst_s")
                        ),
                    )
                    # The window filter says which entries can run at all; the
                    # reservation below says which may run *instead of* the
                    # top-ranked mechanism.  It has to come after every filter
                    # that can still withdraw an entry -- see below.
                    menu_seed_node = memory.candidate_node(
                        str(projected_menu["seed_candidate_id"])
                    )
                    if menu_seed_node is not None and (
                        validate_full_recurrence_lineage(
                            memory, run_dir, menu_seed_node
                        )
                    ):
                        projected_menu = (
                            filter_action_menu_for_permitted_actions(
                                projected_menu,
                                permitted=RECURRENCE_LINEAGE_PERMITTED_ACTIONS,
                                blocker_code="RECURRENCE_LINEAGE_RESTRICTED",
                            )
                        )
                    exhausted_entry_ids = []
                    exhausted_family_entry_ids = []
                    exhausted_probe_entry_ids = []
                    same_seed_family_attempts = int(
                        cfg.get(
                            "action_menu.max_same_seed_family_attempts",
                            2,
                        )
                    )
                    exhausted_families = exhausted_same_seed_mechanism_families(
                        memory,
                        seed_artifact_sha256=str(
                            projected_menu["seed_artifact_sha256"]
                        ),
                        max_negative_attempts=same_seed_family_attempts,
                    ) | exhausted_run_wide_mechanism_families(
                        memory,
                        max_negative_attempts=same_seed_family_attempts,
                        max_spent_seeds=int(
                            cfg.get(
                                "action_menu.max_spent_seeds_per_family",
                                2,
                            )
                        ),
                    )
                    for entry in projected_menu.get("entries", []):
                        action = entry.get("action") if isinstance(entry, dict) else None
                        if (
                            entry.get("eligible") is True
                            and isinstance(action, dict)
                            and action.get("action") == "probe"
                            and memory.probe_observation_exhausted(
                                action.get("server"),
                                action.get("tool"),
                                action.get("arguments") or {},
                                str(projected_menu["seed_artifact_sha256"]),
                            )
                        ):
                            exhausted_probe_entry_ids.append(
                                entry["entry_id"]
                            )
                            continue
                        if (
                            entry.get("eligible") is not True
                            or not isinstance(action, dict)
                            or action.get("action") != "run_skill"
                        ):
                            continue
                        try:
                            menu_action = resolve_skill_action(
                                str(action.get("skill") or ""),
                                action.get("target"),
                                action.get("params") or {},
                                defaults=cfg.skill_defaults(
                                    str(action.get("skill") or "")
                                ),
                                target_presets=target_presets,
                                cold_isolation=cold_isolation,
                            )
                        except CapabilityError:
                            continue
                        if memory.has_executed_fingerprint(
                            menu_action.action_fingerprint,
                            str(projected_menu["seed_candidate_id"]),
                        ):
                            exhausted_entry_ids.append(entry["entry_id"])
                        if contract_for(
                            menu_action.skill,
                            menu_action.canonical_target,
                        ).family in exhausted_families:
                            exhausted_family_entry_ids.append(
                                entry["entry_id"]
                            )
                    projected_menu = filter_action_menu_for_exhausted_entries(
                        projected_menu,
                        exhausted_entry_ids=exhausted_entry_ids,
                        exhausted_family_entry_ids=(
                            exhausted_family_entry_ids
                        ),
                        exhausted_probe_entry_ids=(
                            exhausted_probe_entry_ids
                        ),
                    )
                    # Which entries may run instead of the top-ranked
                    # mechanism.  This must follow every filter that can still
                    # withdraw an entry: reserving for one that a later filter
                    # then removes leaves nothing behind.  Placed before the
                    # execution-window filter it emptied fir's post-promotion
                    # menu -- it reserved the reroutes for a post_route_cleanup
                    # that the recurrence-lineage filter deleted one step
                    # later, and the round stopped with 2,000s unspent.  It
                    # stays ahead of top_k so the cut refills from what is
                    # still selectable.
                    projected_menu = reserve_top_ranked_launch_opportunity(
                        projected_menu,
                        execution_window_s=max(
                            0.0,
                            memory.time_remaining() - time_safety,
                        ),
                        # Whether this run has measured any action yet.  Until
                        # it has, the seed profile the score was computed from
                        # is the whole of what is known.
                        same_run_feedback_available=bool(_ACTION_COST_OBSERVED),
                    )
                    projected_menu = project_action_menu_top_k(
                        projected_menu,
                        configured_top_k=int(
                            cfg.get("action_menu.top_k", 4)
                        ),
                    )
                    # Perishability ranked an unattempted proof-gated mechanism
                    # ahead of the physical actions that historically deliver.
                    # On ispd16 that replaced post_route_cleanup, which reached
                    # 120.613 in 1040s of a 2356s window, with an operator
                    # rewrite that consumed a near-identical 2350s window and
                    # returned nothing.  Ranking a mechanism by what it might
                    # lose, rather than by what it delivers, costs whole runs.
                    if projected_menu.get("menu_digest") != current_menu.get(
                        "menu_digest"
                    ):
                        memory.current_telemetry[
                            "authorized_action_menu"
                        ] = projected_menu
                        memory.add_note(
                            "ACTION_MENU_MECHANICAL_FILTER applied before Decision"
                        )
                        memory.save(memory_path)
                    llm_state = (
                        memory.state_for_staged_llm()
                        if bool(getattr(stages, "uses_staged_react", False))
                        else memory.state_for_llm(**state_kwargs)
                    )
                    if bool(getattr(stages, "uses_staged_react", False)):
                        if report_snapshot is None or knowledge_snapshot is None:
                            raise RuntimeError(
                                "staged ReAct requires report and knowledge snapshots"
                            )
                        if cold_isolation:
                            try:
                                snapshot_path, _snapshot = (
                                    write_offline_decision_snapshot(
                                        memory,
                                        llm_state,
                                        run_dir=run_dir,
                                    )
                                )
                                logger.info(
                                    "[agent] captured non-executable Decision snapshot: %s",
                                    snapshot_path,
                                )
                            except (OfflineDecisionError, OSError, ValueError) as exc:
                                logger.warning(
                                    "[agent] offline Decision snapshot skipped: %s",
                                    exc,
                                )
                        decision = stages.staged_decide(
                            llm_state,
                            report_snapshot,
                            knowledge_snapshot,
                            deadline_monotonic=(
                                time.monotonic()
                                + max(
                                    0.0,
                                    memory.time_remaining() - time_safety,
                                )
                            ),
                            max_cost_usd=memory.tokens_remaining(),
                        )
                        memory.record_reasoning_cycle(decision)
                        # A staged decision that is taken and then replaced
                        # leaves no trace of its own: vtr_mcml_v2 selected its
                        # consumer-driven rewrite on round 1, ran the decision
                        # stage again fourteen seconds later with a cached
                        # report, and executed a fanout phys_opt worth +0.010
                        # instead -- with no veto, no binding error and no
                        # reanalysis event to say why.  Name every staged
                        # decision as it is taken so the replacement is
                        # readable from the run log alone.
                        logger.info(
                            "[agent] staged decision %d in round %d: %s/%s",
                            sum(
                                1 for cycle in (memory.reasoning_cycles or [])
                                if cycle.get("round") == memory.round
                            ),
                            memory.round,
                            decision.action or "?",
                            decision.skill or decision.tool or "-",
                        )
                        memory.record_token_spend(
                            max(
                                0.0,
                                float(getattr(llm, "total_cost", llm_cost_before))
                                - llm_cost_before,
                            )
                        )
                    else:
                        decision = stages.decide(
                            llm_state,
                            rendered_matches,
                            probe_result=decision_probe_result,
                        )
                        memory.record_token_spend(llm.last_call_cost)
                    if decision.reflection:
                        memory.add_note(decision.reflection)
                except Exception as exc:  # noqa: BLE001
                    spent_before_failure = max(
                        0.0,
                        float(getattr(llm, "total_cost", llm_cost_before))
                        - llm_cost_before,
                    )
                    if spent_before_failure:
                        memory.record_token_spend(spent_before_failure)
                    if isinstance(exc, ToolLoopError) and exc.provider_failure:
                        decide_provider_failure = True
                    logger.warning("[agent] decide stage failed: %s", exc)
                    decision = None
                    break

                decision_probe_result = None
                if (
                    not bool(getattr(stages, "uses_staged_react", False))
                    or decision.action not in {
                        "run_skill", "probe", "restart_vivado"
                    }
                ):
                    break
                proposed_seed, _seed_reason = memory.validate_selectable_seed(
                    None
                    if decision.action == "restart_vivado"
                    else decision.seed_candidate_id
                )
                if proposed_seed is None:
                    break
                binding_error = staged_decision_binding_error(
                    decision,
                    report_snapshot,
                    knowledge_snapshot,
                    memory,
                    proposed_seed,
                )
                if not binding_error:
                    break
                logger.warning(
                    "[agent] staged decision %s/%s discarded for reanalysis: %s",
                    decision.action or "?",
                    decision.skill or decision.tool or "-",
                    binding_error,
                )
                if seed_switches >= max_seed_switches:
                    memory.record_seed_reanalysis(
                        decision=decision,
                        selected_seed_candidate_id=proposed_seed["candidate_id"],
                        reason=binding_error,
                        status="switch_limit_exceeded",
                    )
                    memory.record_stop(
                        source="agent",
                        reason="staged_seed_switch_limit_exceeded",
                        rationale=(
                            f"more than {max_seed_switches} exact-seed reanalysis "
                            "attempts in one decision cycle"
                        ),
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    decision = None
                    break
                memory.record_seed_reanalysis(
                    decision=decision,
                    selected_seed_candidate_id=proposed_seed["candidate_id"],
                    reason=binding_error,
                    status="reprofile_required",
                )
                memory.save(memory_path)
                seed_switches += 1
                try:
                    await activate_staged_seed_profile(proposed_seed)
                except Exception as exc:  # noqa: BLE001
                    reason = f"exact seed reprofile failed: {exc}"
                    memory.record_telemetry_failure(
                        epoch=memory.next_telemetry_epoch(),
                        source_round=memory.round,
                        candidate_id=proposed_seed["candidate_id"],
                        dcp_path=str(proposed_seed["artifact_path"]),
                        reason=reason,
                    )
                    memory.record_seed_reanalysis(
                        decision=decision,
                        selected_seed_candidate_id=proposed_seed["candidate_id"],
                        reason=reason,
                        status="reprofile_failed_closed",
                    )
                    memory.record_stop(
                        source="agent",
                        reason="exact_seed_reprofile_failed",
                        rationale=reason,
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    await restore_known_node()
                    decision = None
                    break

            if decision is None or decision.action == "invalid":
                if stages is not None:
                    provider_failure = decide_provider_failure or (
                        decision is not None
                        and str((decision.raw or {}).get("error_class", ""))
                        == "provider_failure"
                    )
                    if (
                        provider_failure
                        and memory.tokens_remaining() > 0
                        and memory.time_remaining() > time_safety
                        and provider_failure_retries < max_provider_failure_retries
                    ):
                        provider_failure_retries += 1
                        logger.warning(
                            "[agent] provider failure %d/%d; retrying the decision "
                            "round with %.0fs and $%.4f left",
                            provider_failure_retries,
                            max_provider_failure_retries,
                            memory.time_remaining(),
                            memory.tokens_remaining(),
                        )
                        memory.add_note(
                            "Provider request failed before a decision was "
                            "produced; the round was retried."
                        )
                        memory.save(memory_path)
                        continue
                    reason = (
                        "token_budget_exhausted"
                        if memory.tokens_remaining() <= 0
                        else "llm_provider_unavailable"
                        if provider_failure
                        else "llm_decision_unavailable"
                    )
                    memory.record_stop(
                        source="agent",
                        reason=reason,
                        rationale="LLM mode does not insert Python-selected fallback actions",
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    break
                if fallback_idx >= len(fallback_plan):
                    memory.record_stop(
                        source="agent",
                        reason="no_llm_plan_exhausted",
                        rationale="operator-selected deterministic plan completed",
                        hard_mechanical=False,
                    )
                    break
                step = fallback_plan[fallback_idx]
                fallback_idx += 1
                decision = Decision(
                    action="run_skill",
                    skill=step["skill"],
                    target=step["target"],
                    params=dict(step["params"]),
                    expectation="fallback plan step",
                    seed_candidate_id=memory.candidate_graph.get(
                        "current_best_candidate_id"
                    ),
                )
            if decision.action == "save_and_exit":
                if decision.selected_entry_id:
                    incumbent = memory.candidate_node(
                        memory.candidate_graph["current_best_candidate_id"]
                    )
                    binding_error = staged_decision_binding_error(
                        decision,
                        report_snapshot,
                        knowledge_snapshot,
                        memory,
                        incumbent or {},
                    )
                    if binding_error:
                        memory.record_stop(
                            source="agent",
                            reason="action_menu_selection_invalid",
                            rationale=binding_error,
                            hard_mechanical=True,
                        )
                        memory.save(memory_path)
                        break
                memory.record_stop(
                    source="llm" if stages is not None else "agent",
                    reason=decision.stop_reason or "save_and_exit",
                    rationale=decision.stop_rationale or decision.expectation,
                    hard_mechanical=False,
                )
                break

            resolved_action: Optional[ResolvedAction] = None
            selected_seed: Optional[dict] = None
            action_menu_launch_authorization: Optional[dict] = None
            skip_action = False
            terminate_search = False
            while True:
                selected_seed, seed_reason = memory.validate_selectable_seed(
                    None
                    if decision.action == "restart_vivado"
                    else decision.seed_candidate_id
                )
                if (
                    replay_enabled
                    and selected_seed is not None
                    and selected_seed.get("candidate_id")
                    != memory.candidate_graph.get("current_best_candidate_id")
                ):
                    selected_seed = None
                    seed_reason = "replay seed is not the immutable current incumbent"
                if selected_seed is None:
                    verdict = legality.Verdict(False, seed_reason)
                else:
                    action_dict = (
                        stages.to_action_dict(decision)
                        if stages is not None
                        else {
                            "action": decision.action,
                            "skill": decision.skill,
                            "target": decision.target,
                            "params": decision.params,
                            "seed_candidate_id": decision.seed_candidate_id,
                        }
                    )
                    if decision.action == "run_skill":
                        try:
                            if replay_enabled:
                                if replay_authorization is None:
                                    raise CapabilityError(
                                        "replay action lacks selection authorization"
                                    )
                                resolved_action = resolve_replay_action(
                                    replay_authorization.action
                                )
                                if (
                                    replay_policy_context is None
                                    or resolved_action.action_fingerprint
                                    != replay_policy_context.get(
                                        "resolved_action_fingerprint"
                                    )
                                    or selected_seed.get("artifact_sha256")
                                    != replay_policy_context[
                                        "incumbent_binding"
                                    ]["artifact_sha256"]
                                ):
                                    raise CapabilityError(
                                        "replay action or incumbent binding changed"
                                    )
                            else:
                                if (
                                    decision.skill == "operator_rewrite"
                                    and decision.target
                                    == "registered_arithmetic_cone_v1"
                                ):
                                    decision.params = (
                                        bind_unique_registered_arithmetic_params(
                                            memory,
                                            selected_seed,
                                            decision.params,
                                        )
                                    )
                                resolved_action = resolve_skill_action(
                                    decision.skill,
                                    decision.target,
                                    decision.params,
                                    defaults=cfg.skill_defaults(decision.skill),
                                    target_presets=target_presets,
                                    cold_isolation=cold_isolation,
                                )
                            if (
                                validate_full_recurrence_lineage(
                                    memory, run_dir, selected_seed
                                )
                                and (
                                    resolved_action.skill,
                                    resolved_action.canonical_target,
                                )
                                not in RECURRENCE_LINEAGE_PERMITTED_ACTIONS
                            ):
                                raise CapabilityError(
                                    "active recurrence lineage permits only a "
                                    "proved recurrence rewrite or worst-source polish"
                                )
                            resolved_action.resolved_params["input_dcp"] = str(
                                validate_graph_node_artifact(run_dir, selected_seed)
                            )
                            if (
                                resolved_action.canonical_target
                                == "registered_arithmetic_cone_v1"
                            ):
                                gate_error = registered_arithmetic_authorization_error(
                                    memory,
                                    selected_seed,
                                    resolved_action.resolved_params.get(
                                        "family_fingerprint"
                                    ),
                                )
                                if gate_error:
                                    raise CapabilityError(gate_error)
                            if staged_react_enabled:
                                if report_snapshot is None or knowledge_snapshot is None:
                                    raise CapabilityError(
                                        "staged action lacks active evidence snapshots"
                                    )
                                action_menu_launch_authorization = (
                                    build_action_menu_launch_authorization(
                                        decision=decision,
                                        resolved_action=resolved_action,
                                        report_snapshot=report_snapshot,
                                        knowledge_snapshot=knowledge_snapshot,
                                        memory=memory,
                                        selected_seed=selected_seed,
                                    )
                                )
                            action_dict = {
                                "action": "run_skill",
                                "skill": resolved_action.skill,
                                "target": resolved_action.execution_target,
                                "params": resolved_action.resolved_params,
                                "seed_candidate_id": selected_seed["candidate_id"],
                            }
                            verdict = legality.check_action(
                                action_dict,
                                custom_calls_used=memory.custom_calls_used,
                                cfg=cfg,
                            )
                        except CapabilityError as exc:
                            resolved_action = None
                            verdict = legality.Verdict(False, str(exc))
                    else:
                        verdict = legality.check_action(
                            action_dict,
                            custom_calls_used=memory.custom_calls_used,
                            cfg=cfg,
                        )
                if verdict.ok:
                    veto_streak = 0
                    break

                veto_streak += 1
                # The menu already proved every eligible entry launchable, so a
                # veto means the menu and the legality check disagree about the
                # same action.  Recording it only in memory left the run log
                # silent: fir_systolic spent three selections and 1670s of its
                # window on this and reported nothing an operator could act on.
                logger.warning(
                    "[agent] veto %d/%d on %s/%s: %s",
                    veto_streak,
                    max_vetoes,
                    decision.skill or decision.tool or "?",
                    (
                        resolved_action.canonical_target
                        if resolved_action is not None else decision.target
                    ),
                    verdict.reason,
                )
                best = memory.candidate_node(
                    memory.candidate_graph["current_best_candidate_id"]
                )
                record_seed_id = (
                    selected_seed["candidate_id"]
                    if selected_seed is not None
                    else decision.seed_candidate_id
                )
                memory.record(
                    skill=decision.skill or decision.tool or "?",
                    target=(
                        resolved_action.canonical_target
                        if resolved_action is not None else decision.target
                    ),
                    before_wns=float(best["wns_ns"]),
                    after_wns=float(best["wns_ns"]),
                    accepted=False,
                    params=(
                        resolved_action.resolved_params
                        if resolved_action is not None else decision.params
                    ),
                    summary=f"VETO: {verdict.reason}",
                    veto_reason=verdict.reason,
                    action_fingerprint=(
                        resolved_action.action_fingerprint
                        if resolved_action is not None else ""
                    ),
                    seed_round=(
                        selected_seed.get("created_round")
                        if selected_seed is not None else None
                    ),
                    seed_candidate_id=record_seed_id,
                    action_executed=False,
                    advance_round=False,
                )
                memory.save(memory_path)
                if replay_enabled:
                    memory.refresh_budget()
                    _retire_replay_selection_not_started(
                        memory,
                        cfg,
                        completed_at_s=float(memory.budget["time_elapsed"]),
                    )
                    memory.record_stop(
                        source="agent",
                        reason="replay_action_unexecutable",
                        rationale=verdict.reason,
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    terminate_search = True
                    skip_action = True
                    break
                if veto_streak >= max_vetoes:
                    if stages is None and fallback_idx < len(fallback_plan):
                        step = fallback_plan[fallback_idx]
                        fallback_idx += 1
                        decision = Decision(
                            action="run_skill",
                            skill=step["skill"],
                            target=step["target"],
                            params=dict(step["params"]),
                            expectation="fallback after repeated vetoes",
                            seed_candidate_id=memory.candidate_graph.get(
                                "current_best_candidate_id"
                            ),
                        )
                        resolved_action = None
                        veto_streak = 0
                        continue
                    stop_reason = (
                        "consecutive_unexecutable_llm_decisions"
                        if stages is not None
                        else "consecutive_unexecutable_no_llm_actions"
                    )
                    memory.record_stop(
                        source="agent",
                        reason=stop_reason,
                        rationale=(
                            f"{veto_streak} consecutive "
                            f"{'LLM' if stages is not None else 'no-LLM fallback'} "
                            "actions failed mechanical "
                            "validation; no Python-selected action was inserted"
                        ),
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    terminate_search = True
                skip_action = True
                break
            if skip_action:
                if terminate_search:
                    break
                continue
            if selected_seed is None:
                raise RuntimeError("validated action has no selected seed")

            action_proof = None
            review_id = ""
            registered_packet = (
                memory.current_telemetry.get(
                    "registered_arithmetic_decision_packet"
                )
                or {}
            )
            concurrent_product_sum = any(
                isinstance(row, dict)
                and row.get("family_kind") == "product_sum"
                and row.get("family_fingerprint")
                == str(decision.params.get("family_fingerprint") or "")
                and row.get("nomination_eligible") is True
                for row in registered_packet.get("normalized_siblings", [])
            )
            concurrent_proof_required = bool(
                cfg.get("concurrent_proof.enabled", False)
                and decision.action == "run_skill"
                and decision.target == "registered_arithmetic_cone_v1"
                and concurrent_product_sum
                and callable(
                    getattr(
                        SKILLS.get(decision.skill),
                        "prepare_concurrent_proof",
                        None,
                    )
                )
            )
            if decision.action == "run_skill" and resolved_action is not None:
                provisional_action = {
                    "action": "run_skill",
                    "skill": decision.skill,
                    "target": decision.target,
                    "seed_candidate_id": selected_seed["candidate_id"],
                    "params": copy.deepcopy(decision.params),
                }
                repairs = [
                    copy.deepcopy(item)
                    for item in resolved_action.canonicalization_repairs
                ]
                action_proof = build_action_proof(
                    memory=memory,
                    resolved_action=resolved_action,
                    selected_seed=selected_seed,
                    provisional_action=provisional_action,
                    canonicalization_repairs=repairs,
                    cfg=cfg,
                    registry_path=action_registry_path,
                    concurrent_proof_required=concurrent_proof_required,
                )

            if (
                stages is not None
                and not decision.selected_entry_id
                and action_proof is not None
                and cfg.get("action_review.enabled", True)
            ):
                if memory.tokens_remaining() <= 0:
                    memory.record_stop(
                        source="agent",
                        reason="token_budget_exhausted_before_action_proof_review",
                        rationale=(
                            "the blind proposal is never executed without its "
                            "configured neutral evidence review"
                        ),
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    break
                blind_estimate = copy.deepcopy(decision.score_estimate)
                llm_cost_before = float(getattr(llm, "total_cost", 0.0))
                try:
                    review = stages.reconsider_action(
                        state=(
                            memory.state_for_staged_llm()
                            if bool(getattr(stages, "uses_staged_react", False))
                            else memory.state_for_llm(**state_kwargs)
                        ),
                        provisional_action=provisional_action,
                        action_proof=action_proof,
                    )
                except Exception as exc:  # noqa: BLE001
                    spent = max(
                        0.0,
                        float(getattr(llm, "total_cost", llm_cost_before))
                        - llm_cost_before,
                    )
                    if spent:
                        memory.record_token_spend(spent)
                    memory.record_stop(
                        source="agent",
                        reason="action_proof_review_unavailable",
                        rationale=str(exc)[:500],
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    break
                memory.record_token_spend(
                    max(
                        0.0,
                        float(getattr(llm, "total_cost", llm_cost_before))
                        - llm_cost_before,
                    )
                )
                review_id = memory.record_decision_review(
                    provisional_action=provisional_action,
                    blind_score_estimate=blind_estimate,
                    action_proof=action_proof,
                    review=review,
                )
                memory.save(memory_path)
                if review.action == "save_and_exit":
                    memory.record_stop(
                        source="llm",
                        reason="save_and_exit_after_action_proof_review",
                        rationale=review.reason or review.assessment,
                        hard_mechanical=False,
                    )
                    memory.save(memory_path)
                    break
                if review.action == "replan":
                    memory.add_note(
                        "Action Proof review requested a new blind plan: "
                        + (review.reason or review.assessment)
                    )
                    memory.save(memory_path)
                    continue
                decision.expectation = review.assessment
                if cfg.get("bo.enabled", False):
                    try:
                        shadow_bo = build_bo_advisory(
                            memory=memory,
                            resolved_action=resolved_action,
                            selected_seed=selected_seed,
                            cfg=cfg,
                        )
                    except Exception as exc:  # noqa: BLE001
                        shadow_bo = {
                            "version": 1,
                            "kind": "learned_action_advisory",
                            "status": "unavailable",
                            "shadow_only": True,
                            "reason": str(exc)[:300],
                        }
                    shadow_bo["shadow_only"] = True
                    shadow_bo["shown_to_llm"] = False
                    memory.record_shadow_bo(review_id, shadow_bo)
                    memory.save(memory_path)

            seed_candidate_id = selected_seed["candidate_id"]
            seed_round = int(selected_seed["created_round"])
            before_wns = float(selected_seed["wns_ns"])
            current_period = float(selected_seed["clock_period_ns"])

            if decision.action == "restart_vivado":
                action_started = time.monotonic()
                try:
                    await restart_vivado_at_node(
                        selected_seed,
                        f"public Vivado restart at {seed_candidate_id}",
                    )
                except asyncio.TimeoutError:
                    reason = "run deadline reached during Vivado restart"
                    stop_reason = "run_deadline_during_vivado_restart"
                except Exception as exc:  # noqa: BLE001
                    reason = f"Vivado restart failed: {exc}"
                    stop_reason = "vivado_restart_failed"
                else:
                    memory.record(
                        skill="restart_vivado",
                        target="vivado_session",
                        before_wns=before_wns,
                        after_wns=before_wns,
                        accepted=False,
                        params={},
                        summary=(
                            "RECOVERED: restarted Vivado and restored immutable "
                            "incumbent"
                        ),
                        expectation=decision.expectation,
                        seed_round=seed_round,
                        seed_candidate_id=seed_candidate_id,
                        action_executed=False,
                        candidate_legal=None,
                        strategy_family="process_recovery",
                        action_runtime_s=time.monotonic() - action_started,
                        advance_round=False,
                    )
                    memory.save(memory_path)
                    continue
                memory.record(
                    skill="restart_vivado",
                    target="vivado_session",
                    before_wns=before_wns,
                    after_wns=before_wns,
                    accepted=False,
                    params={},
                    summary=f"FAILED: {reason}",
                    expectation=decision.expectation,
                    seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id,
                    action_executed=False,
                    candidate_legal=None,
                    strategy_family="process_recovery",
                    action_runtime_s=time.monotonic() - action_started,
                    advance_round=False,
                )
                memory.record_failure(
                    "restart_vivado", "vivado_session", reason
                )
                memory.record_stop(
                    source="agent",
                    reason=stop_reason,
                    rationale=reason,
                    hard_mechanical=True,
                )
                memory.save(memory_path)
                break

            if decision.action == "probe":
                target = f"{decision.server}:{decision.tool}"
                seed_sha256 = str(selected_seed.get("artifact_sha256") or "")
                params = {
                    "arguments": decision.arguments,
                    "description": decision.question,
                }
                if memory.probe_observation_exhausted(
                    decision.server, decision.tool, decision.arguments, seed_sha256
                ):
                    # A read-only observation of an unchanged artifact repeats
                    # its own result, so it must not consume probe budget or a
                    # reasoning cycle.
                    reason = (
                        f"DUPLICATE: {target} already observed this exact artifact"
                    )
                    logger.warning("[agent] %s", reason)
                    memory.record(
                        skill="custom", target=target, before_wns=before_wns,
                        after_wns=before_wns, accepted=False, params=params,
                        summary=reason, seed_round=seed_round,
                        seed_candidate_id=seed_candidate_id,
                        advance_round=False,
                    )
                    pending_probe_result = {
                        "status": "error",
                        "server": decision.server,
                        "tool": decision.tool,
                        "question": decision.question,
                        "seed_candidate_id": seed_candidate_id,
                        "artifact_sha256": seed_sha256,
                        "error": reason,
                    }
                    memory.save(memory_path)
                    continue
                try:
                    if decision.server == "vivado":
                        await open_node(selected_seed, f"probe seed {seed_candidate_id}")
                    memory.consume_candidate_continuation(
                        seed_candidate_id,
                        kind="probe",
                        consumed_round=memory.round + 1,
                    )
                    memory.save(memory_path)
                    result = await execute_with_run_deadline(
                        lambda: SKILLS["custom"].execute(
                            mcp,
                            target,
                            params,
                            before_wns,
                            current_period,
                            probe_candidate={
                                "candidate_id": seed_candidate_id,
                                "artifact_path": selected_seed["artifact_path"],
                                "artifact_sha256": seed_sha256,
                            },
                        ),
                        f"probe {target}",
                    )
                except asyncio.TimeoutError:
                    logger.warning("[agent] run deadline reached during probe %s", target)
                    memory.record_stop(
                        source="agent",
                        reason="run_deadline_during_probe",
                        rationale=target,
                        hard_mechanical=True,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    reason = f"probe failed: {exc}"
                    memory.record_failed_probe(
                        server=decision.server,
                        tool=decision.tool,
                        arguments=decision.arguments,
                        error=reason,
                        description=decision.question,
                        seed_candidate_id=seed_candidate_id,
                        artifact_sha256=seed_sha256,
                    )
                    memory.record(
                        skill="custom", target=target, before_wns=before_wns,
                        after_wns=before_wns, accepted=False, params=params,
                        summary=f"FAILED: {reason}", seed_round=seed_round,
                        seed_candidate_id=seed_candidate_id,
                        advance_round=False,
                    )
                    memory.record_failure("custom", target, reason)
                    pending_probe_result = {
                        "status": "error",
                        "server": decision.server,
                        "tool": decision.tool,
                        "question": decision.question,
                        "seed_candidate_id": seed_candidate_id,
                        "artifact_sha256": seed_sha256,
                        "error": reason,
                    }
                    memory.save(memory_path)
                    await restore_known_node(selected_seed)
                    continue
                if not result.success:
                    reason = result.error_msg or result.summary or "probe failed"
                    memory.record_failed_probe(
                        server=decision.server,
                        tool=decision.tool,
                        arguments=decision.arguments,
                        error=reason,
                        description=decision.question,
                        seed_candidate_id=seed_candidate_id,
                        artifact_sha256=seed_sha256,
                    )
                    memory.record(
                        skill="custom", target=target, before_wns=before_wns,
                        after_wns=before_wns, accepted=False, params=params,
                        summary=f"FAILED: {reason}", seed_round=seed_round,
                        seed_candidate_id=seed_candidate_id,
                        advance_round=False,
                    )
                    memory.record_failure("custom", target, reason)
                    pending_probe_result = {
                        "status": "error",
                        "server": decision.server,
                        "tool": decision.tool,
                        "question": decision.question,
                        "seed_candidate_id": seed_candidate_id,
                        "artifact_sha256": seed_sha256,
                        "error": reason,
                    }
                    memory.save(memory_path)
                    await restore_known_node(selected_seed)
                    continue
                if isinstance(result.details, dict):
                    probe_response = str(
                        result.details.get("response") or result.summary or ""
                    )
                    public_observation = result.details.get("public_observation")
                    if not isinstance(public_observation, dict):
                        public_observation = {}
                else:
                    probe_response = str(result.details or result.summary or "")
                    public_observation = {}
                memory.record_custom_response(
                    server=decision.server,
                    tool=decision.tool,
                    arguments=decision.arguments,
                    response=probe_response,
                    description=decision.question,
                    seed_candidate_id=seed_candidate_id,
                    artifact_sha256=seed_sha256,
                )
                memory.record(
                    skill="custom", target=target, before_wns=before_wns,
                    after_wns=result.after_wns, accepted=False, params=params,
                    summary=result.summary, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id,
                    advance_round=False,
                )
                pending_probe_result = {
                    "status": "success",
                    "server": decision.server,
                    "tool": decision.tool,
                    "arguments": decision.arguments,
                    "question": decision.question,
                    "seed_candidate_id": seed_candidate_id,
                    "artifact_sha256": seed_sha256,
                    "response": probe_response[:12000],
                    **public_observation,
                }
                memory.save(memory_path)
                continue

            if resolved_action is None:
                raise RuntimeError("run_skill action reached execution unresolved")
            skill_name = resolved_action.skill
            target = resolved_action.canonical_target
            execution_target = resolved_action.execution_target
            params = resolved_action.resolved_params
            fingerprint = resolved_action.action_fingerprint

            if memory.has_executed_fingerprint(fingerprint, seed_candidate_id):
                memory.record(
                    skill=skill_name, target=target, before_wns=before_wns,
                    after_wns=before_wns, accepted=False, params=params,
                    summary=f"DUPLICATE: already executed from seed {seed_candidate_id}",
                    expectation=decision.expectation,
                    action_fingerprint=fingerprint, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id, action_executed=False,
                    advance_round=False,
                )
                if replay_enabled:
                    memory.refresh_budget()
                    _retire_replay_selection_not_started(
                        memory,
                        cfg,
                        completed_at_s=float(memory.budget["time_elapsed"]),
                    )
                    memory.record_stop(
                        source="agent",
                        reason="replay_action_duplicate",
                        rationale=(
                            f"{skill_name}/{target} already executed from bound incumbent"
                        ),
                        hard_mechanical=True,
                    )
                memory.save(memory_path)
                if replay_enabled:
                    break
                continue

            selected_menu_display: Mapping[str, Any] = {}
            selected_from_menu = bool(decision.selected_entry_id)
            if selected_from_menu:
                current_menu = memory.current_telemetry.get(
                    "authorized_action_menu"
                )
                if isinstance(current_menu, Mapping):
                    selected_entry = next(
                        (
                            item for item in current_menu.get("entries", [])
                            if isinstance(item, Mapping)
                            and item.get("entry_id") == decision.selected_entry_id
                        ),
                        None,
                    )
                    if isinstance(selected_entry, Mapping) and isinstance(
                        selected_entry.get("display"), Mapping
                    ):
                        selected_menu_display = selected_entry["display"]
            requirement = action_execution_window_requirement(
                action={
                    "action": "run_skill",
                    "skill": skill_name,
                    "target": target,
                    "params": params,
                },
                display=selected_menu_display,
                runtime_estimates_s=(
                    current_runtime_estimates if selected_from_menu else None
                ),
                central_measurement_estimate_s=(
                    current_measurement_estimate if selected_from_menu else 0.0
                ),
                candidate_commit_reserve_s=(
                    candidate_commit_reserve if selected_from_menu else 0.0
                ),
                measured_runtimes_s=_ACTION_COST_OBSERVED,
                measured_full_implementation_s=(
                    _IMPLEMENTATION_COST_OBSERVED.get("worst_s")
                ),
            )
            if requirement is not None:
                required_window = float(
                    requirement["required_execution_window_s"]
                )
                execution_window = max(
                    0.0, memory.time_remaining() - time_safety
                )
                if execution_window < required_window:
                    memory.add_note(
                        "UNLAUNCHABLE EXECUTION_WINDOW_UNAVAILABLE "
                        f"{skill_name}/{target}: required {required_window:.0f}s, "
                        f"execution window {execution_window:.0f}s, "
                        "runtime estimate source "
                        f"{requirement['runtime_estimate_source']}"
                    )
                    if replay_enabled:
                        memory.refresh_budget()
                        _retire_replay_selection_not_started(
                            memory,
                            cfg,
                            completed_at_s=float(memory.budget["time_elapsed"]),
                        )
                        memory.record_stop(
                            source="agent",
                            reason="replay_action_window_unavailable",
                            rationale=f"required {required_window:.3f}s",
                            hard_mechanical=True,
                        )
                    memory.save(memory_path)
                    if replay_enabled:
                        break
                    continue

            try:
                await open_node(
                    selected_seed,
                    f"authoritative seed {seed_candidate_id} for {skill_name}/{target}",
                )
            except asyncio.TimeoutError:
                reason = "authoritative seed open timed out"
                memory.record(
                    skill=skill_name, target=target, before_wns=before_wns,
                    after_wns=before_wns, accepted=False, params=params,
                    summary=f"FAILED: {reason}", expectation=decision.expectation,
                    action_fingerprint=fingerprint, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id, action_executed=False,
                )
                memory.record_failure(skill_name, target, reason)
                memory.record_stop(
                    source="agent",
                    reason="run_deadline_opening_seed",
                    rationale=reason,
                    hard_mechanical=True,
                )
                if replay_enabled:
                    memory.refresh_budget()
                    _retire_replay_selection_not_started(
                        memory,
                        cfg,
                        completed_at_s=float(memory.budget["time_elapsed"]),
                    )
                memory.save(memory_path)
                break
            except Exception as exc:  # noqa: BLE001
                reason = f"authoritative seed open failed: {exc}"
                memory.record(
                    skill=skill_name, target=target, before_wns=before_wns,
                    after_wns=before_wns, accepted=False, params=params,
                    summary=f"FAILED: {reason}", expectation=decision.expectation,
                    action_fingerprint=fingerprint, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id, action_executed=False,
                )
                memory.record_failure(skill_name, target, reason)
                if replay_enabled:
                    memory.refresh_budget()
                    _retire_replay_selection_not_started(
                        memory,
                        cfg,
                        completed_at_s=float(memory.budget["time_elapsed"]),
                    )
                    memory.record_stop(
                        source="agent",
                        reason="replay_incumbent_open_failed",
                        rationale=reason,
                        hard_mechanical=True,
                    )
                memory.save(memory_path)
                if replay_enabled:
                    break
                continue

            action_dir = run_dir / "actions" / f"a{memory.round + 1}_{fingerprint[:12]}"
            # Scratch remains transaction-owned until the outcome is durable.
            candidate_id = memory.allocate_candidate_id()
            transaction_policy_context = replay_policy_context
            if not replay_enabled:
                transaction_policy_context = {
                    "schema_version": 1,
                    "kind": "autonomous_action_context",
                    "execution_mode": (
                        "staged_react"
                        if staged_react_enabled
                        else "legacy_llm" if stages is not None else "deterministic"
                    ),
                    "incumbent_binding": {
                        "candidate_id": seed_candidate_id,
                        "artifact_sha256": str(
                            selected_seed.get("artifact_sha256") or ""
                        ),
                        "fmax_mhz": float(selected_seed["fmax_mhz"]),
                    },
                    "resolved_action_fingerprint": fingerprint,
                    "commit_contract": {
                        "schema_version": 1,
                        "promotion_min_delta_mhz": float(promotion_delta),
                    },
                    "launch_authorization": copy.deepcopy(
                        action_menu_launch_authorization
                    ),
                }
            score_projection = {}
            if decision.score_estimate:
                memory.refresh_budget()
                score_projection = project_action_score_envelope(
                    baseline_fmax_mhz=float(memory.baseline["fmax"]),
                    best_fmax_mhz=float(memory.current_best["fmax"]),
                    elapsed_runtime_s=float(memory.budget["time_elapsed"]),
                    llm_cost_usd=float(memory.budget.get("tokens_spent", 0.0)),
                    score_estimate=decision.score_estimate,
                    finalization_reserve_s=time_safety,
                    execution_window_remaining_s=max(
                        0.0, memory.time_remaining() - time_safety
                    ),
                )
            memory.begin_action_transaction(
                candidate_id=candidate_id,
                seed_candidate_id=seed_candidate_id,
                skill=skill_name,
                target=target,
                params=params,
                action_fingerprint=fingerprint,
                action_dir=str(action_dir.resolve()),
                action_proof=action_proof,
                review_id=review_id,
                score_estimate=decision.score_estimate,
                score_projection=score_projection,
                policy_context=transaction_policy_context,
            )
            action_contract = contract_for(skill_name, target)
            mutation_task = TaskSpec(
                task_id=f"mutation:{candidate_id}:{fingerprint[:12]}",
                kind=TaskKind.MUTATION,
                seed_artifact_sha256=str(
                    selected_seed.get("artifact_sha256") or ""
                ),
                workspace=action_dir,
                cpu_cores=action_contract.cpu_cores,
                memory_gib=action_contract.memory_gib,
                exclusive_session=action_contract.exclusive_session,
            )
            memory.pending_transaction["scheduler_task"] = {
                "task_id": mutation_task.task_id,
                "kind": mutation_task.kind.value,
                "seed_artifact_sha256": mutation_task.seed_artifact_sha256,
                "workspace": str(action_dir.resolve()),
                "cpu_cores": mutation_task.cpu_cores,
                "memory_gib": mutation_task.memory_gib,
                "exclusive_session": mutation_task.exclusive_session,
                "host_limit": {"cpu_cores": 8, "memory_gib": 32.0},
            }
            if concurrent_proof_required:
                memory.pending_transaction["concurrent_proof_required"] = True
            memory.save(memory_path)

            execution_state = {
                "skill_invoked": False,
                "shadow_prefetch_task": None,
            }

            async def discard_shadow_task() -> None:
                task = execution_state.get("shadow_prefetch_task")
                execution_state["shadow_prefetch_task"] = None
                if task is None:
                    return
                if not task.done():
                    task.cancel()
                try:
                    shadow_value = await task
                except BaseException:
                    return
                if isinstance(shadow_value, ShadowPrefetchResult):
                    discard_shadow_prefetch(shadow_value)

            async def execute_scoped_skill():
                memory.advance_action_transaction(
                    "executing",
                    execution_started=None,
                    action_started_at=time.time(),
                    expectation=decision.expectation,
                )
                memory.save(memory_path)
                execution_params = dict(params)
                execution_params["_execution_deadline_monotonic"] = (
                    time.monotonic()
                    + max(0.0, memory.time_remaining() - time_safety)
                )
                execution_params["_candidate_commit_reserve_s"] = (
                    candidate_commit_reserve
                )
                # Implementation recipes scale with design size, so a skill
                # choosing between a full re-place and a cheaper route needs
                # the same scale signal the action cards already use.  This
                # reads the utilization the run measured, not a `root_analysis`
                # attribute Memory does not define: guarding that name with
                # hasattr left the scale permanently None and the choice inert.
                #
                # Scope it to the skill that consumes it.  Presets with fixed
                # parameter sets reject any key they do not declare, and while
                # the value was always None that rejection never fired.
                # The ranking charged this action for the cost its contract
                # declares, and the window filter admitted it on that number.
                # An action then granted ten times what it declared is not the
                # action that was ranked: ispd16_example2's operator rewrite
                # declares 300s, was granted the whole 2,776s window, spent it
                # and returned +0.00, where its recorded 120.61 MHz run takes a
                # 435s post-route cleanup first.
                if skill_name == "operator_rewrite":
                    execution_params["_declared_runtime_s"] = float(
                        contract_for(skill_name, target).estimated_runtime_s
                    )
                if skill_name == "clock_tighten":
                    execution_params["_design_lut_count"] = (
                        (memory.current_telemetry or {})
                        .get("utilization", {})
                        .get("luts")
                    )
                if skill_name == "fresh_place_route":
                    execution_params["_continuation_profile_reserve_s"] = (
                        measured_same_run_profile_reserve(memory)
                    )
                    execution_params["_continuation_action_reserve_s"] = (
                        highest_ranked_continuation_reserve(
                            memory,
                            excluding_skill=skill_name,
                            runtime_estimates_s=current_runtime_estimates,
                            central_measurement_estimate_s=(
                                current_measurement_estimate
                            ),
                            candidate_commit_reserve_s=(
                                candidate_commit_reserve
                            ),
                        )
                    )
                if skill_name in _CANDIDATE_OUTPUT_CAP_SKILLS:
                    execution_params["_candidate_output_cap"] = skill_candidate_cap
                candidate_runtime_estimate = candidate_runtime_estimates.get(
                    skill_name
                )
                if (
                    isinstance(candidate_runtime_estimate, (int, float))
                    and not isinstance(candidate_runtime_estimate, bool)
                    and float(candidate_runtime_estimate) > 0.0
                ):
                    execution_params["_candidate_runtime_estimate_s"] = float(
                        candidate_runtime_estimate
                    )
                if no_replay_run is not None and (
                    skill_name == "custom"
                    or (
                        skill_name == "operator_rewrite"
                        and target == "registered_arithmetic_cone_v1"
                    )
                ):
                    execution_params["_run_id"] = no_replay_run.run_id
                    execution_params["_run_source_sha256"] = (
                        no_replay_run.source_artifact_sha256
                    )
                if (
                    (
                        skill_name == "fresh_place_route"
                        and execution_params.get("flow_mode")
                        == "shared_post_place_route_race"
                    )
                    or skill_name in {
                        "critical_net_reroute", "endpoint_bel_move", "hard_macro_move",
                        "selective_branch_reroute", "equivalent_source_remap",
                        "operator_rewrite",
                    }
                ):
                    execution_params["_contest_clock"] = str(
                        cfg.get("legality.contest_clock", "clk_fpl26contest")
                )
                if skill_name in {
                    "critical_net_reroute", "endpoint_bel_move", "hard_macro_move", "semantic_replay",
                }:
                    execution_params["_source_artifact_sha256"] = str(
                        selected_seed.get("artifact_sha256") or ""
                    )
                with mcp.scoped_run_dir(action_dir):
                    memory.pending_transaction["execution_started"] = True
                    memory.mutation_count += 1
                    execution_state["skill_invoked"] = True
                    memory.save(memory_path)

                    async def serial_execution():
                        return await SKILLS[skill_name].execute(
                            mcp,
                            execution_target,
                            execution_params,
                            before_wns,
                            current_period,
                        )

                    prepare = getattr(
                        SKILLS[skill_name], "prepare_concurrent_proof", None
                    )

                    async def prepare_concurrent():
                        action = prepare(
                            mcp=mcp,
                            target=execution_target,
                            params=execution_params,
                            before_wns=before_wns,
                            clock_period=current_period,
                            candidate_id=candidate_id,
                            parent_candidate_id=seed_candidate_id,
                            source_artifact_path=Path(
                                selected_seed["artifact_path"]
                            ).resolve(),
                            source_artifact_sha256=str(
                                selected_seed.get("artifact_sha256") or ""
                            ),
                            action_fingerprint=fingerprint,
                            vm_slot=str(
                                cfg.get("concurrent_proof.vm_slot", "local")
                            ),
                        )
                        if asyncio.iscoroutine(action):
                            action = await action
                        if not isinstance(action, ConcurrentProofAction):
                            raise RuntimeError(
                                "skill returned an invalid concurrent proof action"
                            )
                        return action

                    def record_proof_packet(request, rows) -> None:
                        packet = build_concurrent_proof_decision_packet(
                            rows,
                            source_artifact_sha256=request.source.sha256,
                        )
                        memory.pending_transaction[
                            "concurrent_proof_decision_packet"
                        ] = packet
                        memory.current_telemetry[
                            "concurrent_proof_decision_packet"
                        ] = packet
                        memory.save(memory_path)

                    async def central_candidate_admission(execution):
                        """Measure final Fmax before spending the strict gate."""
                        candidate_path = Path(execution.candidate.path).resolve()
                        admission = await measure_candidate(
                            mcp,
                            candidate_path,
                            cfg,
                            reference_dcp_path=Path(
                                selected_seed["artifact_path"]
                            ).resolve(),
                        )
                        source_fmax = float(selected_seed["fmax_mhz"])
                        candidate_fmax = admission.get("fmax_mhz")
                        admitted = bool(
                            admission.get("eligible_for_beam") is True
                            and isinstance(candidate_fmax, (int, float))
                            and not isinstance(candidate_fmax, bool)
                            and float(candidate_fmax)
                            > source_fmax + float(promotion_delta)
                        )
                        admission_record = {
                            "schema_version": "central-admission-measurement-v1",
                            "source_candidate_id": seed_candidate_id,
                            "source_artifact_sha256": str(
                                selected_seed["artifact_sha256"]
                            ),
                            "candidate_artifact_sha256": execution.candidate.sha256,
                            "source_fmax_mhz": source_fmax,
                            "promotion_min_delta_mhz": float(promotion_delta),
                            "admitted_to_strict_validation": admitted,
                            "measurement": admission,
                        }
                        atomic_write_json(
                            action_dir / "central_admission_measurement.json",
                            admission_record,
                        )
                        if admitted:
                            logger.info(
                                "[agent] central admission %.3f -> %.3f MHz; "
                                "starting strict validation",
                                source_fmax,
                                float(candidate_fmax),
                            )
                            return None
                        measured = (
                            f"{float(candidate_fmax):.3f} MHz"
                            if isinstance(candidate_fmax, (int, float))
                            and not isinstance(candidate_fmax, bool)
                            else "unavailable"
                        )
                        reason = str(
                            admission.get("legality_reason")
                            or (
                                f"central Fmax {measured} did not exceed "
                                f"{source_fmax + float(promotion_delta):.3f} MHz"
                            )
                        )
                        logger.info(
                            "[agent] skipping strict validation after central "
                            "admission rejection: %s",
                            reason,
                        )
                        return SkillResult.failure(
                            before_wns,
                            f"central admission rejected candidate: {reason}",
                            candidate_path,
                            details={
                                "typed_rejection": {
                                    "reason_code": "CENTRAL_FMAX_ADMISSION_REJECTED",
                                    "message": reason,
                                },
                                "central_admission_measurement": admission,
                            },
                        )

                    if not concurrent_proof_required:
                        return await serial_execution()
                    concurrent_result = await run_optional_concurrent_action(
                        enabled=True,
                        serial_execution=serial_execution,
                        prepare_concurrent=prepare_concurrent,
                        proof_timeouts_s=configured_proof_timeouts(cfg),
                        packet_sink=record_proof_packet,
                        admission_measurement=central_candidate_admission,
                    )
                    execution_state["shadow_prefetch_task"] = (
                        concurrent_result.shadow_prefetch_task
                    )
                    if concurrent_result.receipt is not None:
                        completed_proof = complete_action_proof(
                            memory.pending_transaction.get("action_proof") or {},
                            receipt=concurrent_result.receipt,
                        )
                        memory.pending_transaction["action_proof"] = completed_proof
                        memory.pending_transaction["action_proof_sha256"] = (
                            completed_proof["proof_sha256"]
                        )
                        memory.pending_transaction["proof_completion"] = copy.deepcopy(
                            completed_proof["proof_completion"]
                        )
                        memory.current_telemetry.pop(
                            "concurrent_proof_decision_packet", None
                        )
                        memory.save(memory_path)
                    return concurrent_result.value

            action_started = time.monotonic()
            try:
                result = await execute_with_run_deadline(
                    lambda: task_scheduler.run(
                        mutation_task, execute_scoped_skill
                    ),
                    f"{skill_name}/{target}",
                )
            except asyncio.TimeoutError:
                action_runtime = time.monotonic() - action_started
                if not execution_state["skill_invoked"]:
                    reason = "run deadline reached before skill execution started"
                    memory.pending_transaction["execution_started"] = False
                    memory.record_stop(
                        source="agent",
                        reason="run_deadline_before_skill",
                        rationale=reason,
                        hard_mechanical=True,
                    )
                    commit_failed_action_transaction(
                        memory,
                        cfg,
                        memory_path,
                        terminal_status="interrupted",
                        terminal_reason=reason,
                        calibration_status=None,
                        registry_path=None,
                        action_runtime_s=action_runtime,
                    )
                    collect_committed_artifacts(action_dir)
                    break
                reason = "run deadline reached during skill execution"
                # This action did real work on this design before the deadline
                # cut it off, so its elapsed time is a measured lower bound on
                # what it costs here.  Discarding it threw away the single most
                # expensive measurement the run ever makes, and left the next
                # estimate resting on the same constant that mis-sized it.
                note_action_cost(
                    str(decision.skill or ""),
                    decision.target,
                    action_runtime,
                    destructive_scope=(selected_menu_display or {}).get(
                        "destructive_scope"
                    ),
                )
                memory.consume_candidate_continuation(
                    seed_candidate_id,
                    kind="mutation",
                    consumed_round=memory.round + 1,
                )
                memory.record(
                    skill=skill_name, target=target, before_wns=before_wns,
                    after_wns=before_wns, accepted=False, params=params,
                    summary=f"TIMEOUT: {reason}", expectation=decision.expectation,
                    action_fingerprint=fingerprint, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id, action_executed=True,
                    action_runtime_s=action_runtime,
                )
                memory.record_failure(skill_name, target, f"{reason} ({action_runtime:.2f}s)")
                memory.record_stop(
                    source="agent",
                    reason="run_deadline_during_skill",
                    rationale=f"{skill_name}/{target} ran {action_runtime:.3f}s",
                    hard_mechanical=True,
                )
                commit_failed_action_transaction(
                    memory,
                    cfg,
                    memory_path,
                    terminal_status="timed_out",
                    terminal_reason=reason,
                    calibration_status="timeout",
                    registry_path=action_registry_path,
                    action_runtime_s=action_runtime,
                )
                collect_committed_artifacts(action_dir)
                break
            except Exception as exc:  # noqa: BLE001
                action_runtime = time.monotonic() - action_started
                if not execution_state["skill_invoked"]:
                    reason = f"skill launch failed before action execution: {exc}"
                    memory.pending_transaction["execution_started"] = False
                    commit_failed_action_transaction(
                        memory,
                        cfg,
                        memory_path,
                        terminal_status="interrupted",
                        terminal_reason=reason,
                        calibration_status=None,
                        registry_path=None,
                        action_runtime_s=action_runtime,
                    )
                    collect_committed_artifacts(action_dir)
                    await restore_known_node(selected_seed)
                    continue
                reason = f"skill execution failed: {exc}"
                # A failed mutation still paid for everything it did before it
                # failed.  That elapsed time is the same measured evidence a
                # successful one produces, and the run keeps going, so a retry
                # of this family must be sized against it.
                note_action_cost(
                    str(decision.skill or ""),
                    decision.target,
                    action_runtime,
                    destructive_scope=(selected_menu_display or {}).get(
                        "destructive_scope"
                    ),
                )
                memory.consume_candidate_continuation(
                    seed_candidate_id,
                    kind="mutation",
                    consumed_round=memory.round + 1,
                )
                memory.record(
                    skill=skill_name, target=target, before_wns=before_wns,
                    after_wns=before_wns, accepted=False, params=params,
                    summary=f"FAILED: {reason}", expectation=decision.expectation,
                    action_fingerprint=fingerprint, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id, action_executed=True,
                    action_runtime_s=action_runtime,
                )
                memory.record_failure(skill_name, target, reason)
                commit_failed_action_transaction(
                    memory,
                    cfg,
                    memory_path,
                    terminal_status="execution_failed",
                    terminal_reason=reason,
                    calibration_status="skill_failure",
                    registry_path=action_registry_path,
                    action_runtime_s=action_runtime,
                )
                collect_committed_artifacts(action_dir)
                await restore_known_node(selected_seed)
                continue
            memory.consume_candidate_continuation(
                seed_candidate_id,
                kind="mutation",
                consumed_round=memory.round + 1,
            )
            action_runtime = time.monotonic() - action_started
            # A mutation's real cost is what it just cost here, on this design.
            # Read-only evidence collection uses it to decide whether a probe
            # that would authorize the same family can still be acted on, and
            # the menu uses it in place of the constant estimate the next time
            # this action -- or any action that rebuilds placement -- is ranked.
            note_action_cost(
                str(decision.skill or ""),
                decision.target,
                action_runtime,
                destructive_scope=(selected_menu_display or {}).get(
                    "destructive_scope"
                ),
            )
            memory.pending_transaction.update({
                "execution_started": True,
                "action_runtime_s": action_runtime,
            })
            memory.save(memory_path)
            try:
                candidate_outputs = validate_skill_outputs(
                    result,
                    action_dir=action_dir,
                    candidate_cap=skill_candidate_cap,
                    proof_max_bytes=(
                        REGISTERED_ARITHMETIC_PROOF_MAX_BYTES
                        if skill_name == "operator_rewrite"
                        and target == "registered_arithmetic_cone_v1"
                        else 65536
                    ),
                )
            except (SkillContractError, TypeError, ValueError) as exc:
                candidate_outputs = ()
                reason = f"skill output contract violation: {exc}"
            else:
                reason = ""
            if candidate_outputs and skill_name in HIGH_RISK_ECO_SKILLS:
                gated_outputs = []
                gate_failures = []
                for ordinal, output in enumerate(candidate_outputs):
                    try:
                        validate_candidate_eco_proof(
                            skill=skill_name,
                            target=target,
                            proof=output.proof,
                            source_artifact_sha256=str(
                                selected_seed.get("artifact_sha256", "")
                            ),
                            candidate_artifact_sha256=sha256_file(output.output_dcp),
                        )
                        gated_outputs.append(output)
                    except SkillContractError as exc:
                        gate_failures.append(f"candidate {ordinal}: {exc}")
                candidate_outputs = tuple(gated_outputs)
                if gate_failures:
                    reason = "high-risk candidate proof gate: " + "; ".join(gate_failures)
            lineage_timed_out = False
            seed_lineage = None
            if candidate_outputs:
                try:
                    seed_lineage = recurrence_lineage_context_for_seed(
                        memory, run_dir, selected_seed
                    )
                except Exception as exc:  # noqa: BLE001
                    candidate_outputs = ()
                    reason = f"recurrence lineage seed gate: {exc}"
            supported_lineage_action = (
                skill_name == "operator_rewrite"
                and target == "dsp_srl_recurrence_cut_v1"
            ) or (
                skill_name == "critical_net_reroute" and target == "worst_source"
            )
            if candidate_outputs and seed_lineage is not None and not supported_lineage_action:
                candidate_outputs = ()
                reason = (
                    "recurrence lineage seed permits only a renewed worst_source "
                    "route or a new proved recurrence rewrite"
                )
            lineage_action = (
                skill_name == "operator_rewrite"
                and target == "dsp_srl_recurrence_cut_v1"
            ) or (
                seed_lineage is not None
                and skill_name == "critical_net_reroute"
                and target == "worst_source"
            )
            if candidate_outputs and lineage_action:
                try:
                    lineage_budget = (
                        memory.time_remaining()
                        - time_safety
                        - candidate_commit_reserve
                    )
                    if lineage_budget <= 0:
                        raise asyncio.TimeoutError
                    lineage_deadline = time.monotonic() + lineage_budget
                    candidate_outputs = await asyncio.wait_for(
                        prepare_recurrence_lineage_outputs(
                            mcp=mcp,
                            memory=memory,
                            run_dir=run_dir,
                            action_dir=action_dir,
                            selected_seed=selected_seed,
                            skill=skill_name,
                            target=target,
                            params=params,
                            outputs=candidate_outputs,
                            deadline_monotonic=lineage_deadline,
                        ),
                        timeout=lineage_budget,
                    )
                    lineage_result = copy.copy(result)
                    lineage_result.candidates = candidate_outputs
                    candidate_outputs = validate_skill_outputs(
                        lineage_result,
                        action_dir=action_dir,
                        candidate_cap=skill_candidate_cap,
                        proof_max_bytes=(
                            REGISTERED_ARITHMETIC_PROOF_MAX_BYTES
                            if skill_name == "operator_rewrite"
                            and target == "registered_arithmetic_cone_v1"
                            else 65536
                        ),
                    )
                    candidate_outputs = tuple(
                        output
                        for output in candidate_outputs
                        if validate_action_candidate_eco_proof(
                            memory,
                            run_dir=run_dir,
                            seed_candidate_id=seed_candidate_id,
                            skill=skill_name,
                            target=target,
                            proof=output.proof,
                            evidence=output.evidence,
                            candidate_artifact_sha256=sha256_file(
                                output.output_dcp
                            ),
                        )
                        is not None
                    )
                except asyncio.TimeoutError:
                    candidate_outputs = ()
                    lineage_timed_out = True
                    reason = "recurrence lineage gate reached the action deadline"
                    restart = getattr(mcp, "restart_vivado", None)
                    if callable(restart):
                        try:
                            await asyncio.wait_for(
                                restart(
                                    restore_checkpoint=Path(
                                        selected_seed["artifact_path"]
                                    ).resolve(),
                                    expected_sha256=selected_seed.get(
                                        "artifact_sha256"
                                    ),
                                    timeout=600.0,
                                ),
                                timeout=700.0,
                            )
                        except Exception as exc:  # noqa: BLE001
                            reason += f"; Vivado recovery failed: {exc}"
                except Exception as exc:  # noqa: BLE001
                    candidate_outputs = ()
                    reason = f"recurrence lineage gate: {exc}"
                action_runtime = time.monotonic() - action_started
            logger.info("[agent] result: %s", getattr(result, "summary", reason))

            if not candidate_outputs:
                await discard_shadow_task()
                if not reason and isinstance(result, SkillResult):
                    reason = result.error_msg or result.summary or "skill-local failure"
                failure_diagnostic = _bounded_skill_failure_diagnostic(result)
                if failure_diagnostic:
                    memory.pending_transaction["failure_diagnostic"] = (
                        failure_diagnostic
                    )
                memory.record(
                    skill=skill_name, target=target, before_wns=before_wns,
                    after_wns=before_wns, accepted=False, params=params,
                    summary=f"FAILED: {reason}", expectation=decision.expectation,
                    action_fingerprint=fingerprint, seed_round=seed_round,
                    seed_candidate_id=seed_candidate_id, action_executed=True,
                    action_runtime_s=action_runtime,
                )
                memory.record_failure(skill_name, target, reason)
                commit_failed_action_transaction(
                    memory,
                    cfg,
                    memory_path,
                    terminal_status=("timed_out" if lineage_timed_out else "no_candidates"),
                    terminal_reason=reason,
                    calibration_status=(
                        "timeout" if lineage_timed_out else "output_contract_failure"
                    ),
                    registry_path=action_registry_path,
                    action_runtime_s=action_runtime,
                )
                collect_committed_artifacts(action_dir)
                await restore_known_node(selected_seed)
                if lineage_timed_out:
                    memory.record_stop(
                        source="agent",
                        reason="run_deadline_during_recurrence_lineage",
                        rationale=reason,
                        hard_mechanical=True,
                    )
                    memory.save(memory_path)
                    break
                continue

            memory.assign_action_candidates(candidate_outputs)
            memory.advance_action_transaction(
                "publishing",
                action_runtime_s=action_runtime,
                result_summary=result.summary,
            )
            memory.save(memory_path)
            for item in memory.pending_transaction["candidates"]:
                source_path = Path(item["source_path"])
                source_digest = sha256_file(source_path)
                expected_source_digest = (
                    (item.get("evidence") or {}).get("candidate_dcp_sha256")
                    or source_digest
                )
                canonical = materialize_candidate_artifact(
                    source_path,
                    candidates_dir / item["candidate_id"],
                    expected_sha256=expected_source_digest,
                )
                digest = sha256_file(canonical)
                validate_action_candidate_eco_proof(
                    memory,
                    run_dir=run_dir,
                    seed_candidate_id=seed_candidate_id,
                    skill=skill_name,
                    target=target,
                    proof=item.get("proof"),
                    evidence=item.get("evidence"),
                    candidate_artifact_sha256=digest,
                )
                item.update({
                    "canonical_path": str(canonical.resolve()),
                    "artifact_size_bytes": canonical.stat().st_size,
                    "artifact_sha256": digest,
                    "published_at": time.time(),
                })
                memory.advance_action_transaction("publishing")
                memory.save(memory_path)
            memory.advance_action_transaction("published")
            memory.save(memory_path)

            for item in memory.pending_transaction["candidates"]:
                canonical = Path(item["canonical_path"])
                validation_started = time.monotonic()
                try:
                    measurement = await execute_with_run_deadline(
                        lambda canonical=canonical: measure_candidate(mcp, canonical, cfg),
                        f"central measurement {item['candidate_id']}",
                    )
                except asyncio.TimeoutError:
                    measurement = failed_measurement(
                        "central measurement timed out at total-run deadline"
                    )
                item["measurement"] = measurement
                memory.validation_count += 1
                await complete_enabling_candidate_profile(
                    item=item, mcp=mcp, cfg=cfg
                )
                item["validation_runtime_s"] = time.monotonic() - validation_started
                memory.advance_action_transaction("published")
                memory.save(memory_path)
            await complete_candidate_signatures(
                memory,
                mcp,
                cfg,
                memory_path,
                deadline_executor=execute_with_run_deadline,
            )
            memory.advance_action_transaction("measured")
            memory.save(memory_path)

            commit_result = commit_measured_action(
                memory,
                cfg,
                memory_path,
                promotion_delta,
                registry_required=action_registry_path is not None,
                provenance_ledger=provenance_ledger,
            )
            publish_action_evidence_outbox(
                memory, cfg, memory_path, action_registry_path
            )
            promoted_ids = commit_result["promoted_candidate_ids"]
            if promoted_ids:
                best_id = commit_result["best_candidate_id"]
                child = memory.candidate_node(best_id)
                child_measurement = measurement_from_node(child)
                best_path = Path(child["artifact_path"])
                atomic_copy_file(
                    best_path,
                    current_best_dcp,
                    expected_sha256=str(child.get("artifact_sha256") or ""),
                )
                if not submission_mode:
                    atomic_copy_file(
                        best_path,
                        output_dcp,
                        expected_sha256=str(child.get("artifact_sha256") or ""),
                    )
                logger.info(
                    "[agent] PROMOTED siblings %s; best %s at %.3f MHz",
                    promoted_ids,
                    best_id,
                    child["fmax_mhz"],
                )
                shadow_task = execution_state.get("shadow_prefetch_task")
                execution_state["shadow_prefetch_task"] = None
                if shadow_task is not None:
                    try:
                        shadow_value = await shadow_task
                        if not isinstance(shadow_value, ShadowPrefetchResult):
                            raise RuntimeError(
                                "A5 shadow prefetch returned an invalid result"
                            )
                        preflight_root = run_dir / (
                            "cold_product_sum_preflight_"
                            f"{str(child['artifact_sha256'])[:12]}"
                        )
                        shadow_receipt = finalize_shadow_prefetch(
                            shadow_value,
                            validation_passed=True,
                            promotion_occurred=True,
                            promoted_candidate_sha256=str(
                                child["artifact_sha256"]
                            ),
                            published_root=preflight_root,
                        )
                        memory.current_telemetry["shadow_prefetch_receipt"] = (
                            shadow_receipt
                        )
                        logger.info(
                            "[agent] A5 shadow prefetch published for %s in %.3fs",
                            best_id,
                            float(shadow_value.runtime_s),
                        )
                    except Exception as exc:  # optional read-only acceleration
                        memory.current_telemetry["shadow_prefetch_failure"] = {
                            "candidate_id": best_id,
                            "artifact_sha256": str(child["artifact_sha256"]),
                            "reason": str(exc),
                            "mutation_authority": False,
                        }
                        logger.warning(
                            "[agent] A5 shadow prefetch unavailable: %s", exc
                        )
                next_epoch = int(memory.current_telemetry.get("epoch", 0)) + 1
                if memory.time_remaining() <= time_safety:
                    memory.record_telemetry_failure(
                        epoch=next_epoch, source_round=memory.round,
                        candidate_id=best_id, dcp_path=str(best_path),
                        reason="skipped: insufficient time beyond final safety window",
                    )
                else:
                    try:
                        async def reprofile_promoted():
                            await mcp.call_vivado(
                                "open_checkpoint",
                                {"dcp_path": str(best_path.resolve())},
                                timeout=600.0,
                            )
                            return await collect_current_design(
                                mcp, cfg, require_timing_profile=not staged_react_enabled,
                                central_measurement=child_measurement,
                                capture_reports=False,
                                allow_partial=staged_react_enabled,
                                operator_mining_candidate=(
                                    child if staged_react_enabled else None
                                ),
                                recurrence_lineage_active=(
                                    validate_full_recurrence_lineage(
                                        memory, run_dir, child
                                    )
                                    if staged_react_enabled
                                    else False
                                ),
                                cold_isolation=cold_isolation,
                                run_id=(
                                    no_replay_run.run_id if no_replay_run else ""
                                ),
                                telemetry_epoch=next_epoch,
                                same_run_feedback_by_skill=build_current_lineage_action_feedback(
                                    memory,
                                    seed_artifact_sha256=str(child["artifact_sha256"]),
                                ),
                            )

                        promoted_profile_started = time.monotonic()
                        refreshed = await execute_with_run_deadline(
                            reprofile_promoted, f"telemetry epoch {next_epoch}"
                        )
                        refreshed["_profile_runtime_s"] = max(
                            0.0, time.monotonic() - promoted_profile_started
                        )
                        features = extract_features(refreshed, design_id=input_dcp.stem)
                        telemetry_complete = bool(refreshed.get("_telemetry_complete"))
                        matches = (
                            []
                            if replay_enabled or staged_react_enabled
                            else kb.match(features, cfg=cfg)
                            if telemetry_complete
                            else []
                        )
                        target_presets = (
                            {} if replay_enabled or staged_react_enabled
                            else kb.target_presets(matches)
                        )
                        rendered_matches = (
                            [] if replay_enabled or staged_react_enabled
                            else kb.render_matches(matches)
                        )
                        analysis = refreshed
                        if not replay_enabled and not staged_react_enabled:
                            seed_suggested_targets(memory, analysis)
                        if staged_react_enabled:
                            report_snapshot, report_snapshot_ref = publish_report_snapshot(
                                run_dir,
                                analysis,
                                candidate_id=best_id,
                                artifact_sha256=child["artifact_sha256"],
                                telemetry_epoch=next_epoch,
                            )
                        memory.record_telemetry_epoch(
                            epoch=next_epoch, source_round=memory.round,
                            candidate_id=best_id, dcp_path=str(best_path),
                            analysis=analysis, features=features.to_dict(),
                            matched_cases=matches,
                            report_snapshot=report_snapshot_ref,
                        )
                        if staged_react_enabled and not telemetry_complete:
                            memory.record_telemetry_failure(
                                epoch=next_epoch,
                                source_round=memory.round,
                                candidate_id=best_id,
                                dcp_path=str(best_path),
                                reason=(
                                    "report snapshot is exact but rich parsed telemetry "
                                    "is incomplete"
                                ),
                            )
                    except asyncio.TimeoutError:
                        if staged_react_enabled:
                            record_degraded_staged_epoch(
                                node=child,
                                epoch=next_epoch,
                                reason="reprofiling timed out at total-run deadline",
                                exc=asyncio.TimeoutError(
                                    "promoted-seed report collection timed out"
                                ),
                            )
                        else:
                            memory.record_telemetry_failure(
                                epoch=next_epoch, source_round=memory.round,
                                candidate_id=best_id, dcp_path=str(best_path),
                                reason="reprofiling timed out at total-run deadline",
                            )
                    except Exception as exc:  # noqa: BLE001
                        if staged_react_enabled:
                            record_degraded_staged_epoch(
                                node=child,
                                epoch=next_epoch,
                                reason=f"reprofiling failed: {exc}",
                                exc=exc,
                            )
                        else:
                            memory.record_telemetry_failure(
                                epoch=next_epoch, source_round=memory.round,
                                candidate_id=best_id, dcp_path=str(best_path),
                                reason=f"reprofiling failed: {exc}",
                            )
            else:
                await discard_shadow_task()
                all_legal = all(
                    outcome["measurement"].get("eligible_for_beam")
                    for outcome in commit_result["outcomes"]
                )
                await restore_known_node(selected_seed if all_legal else None)
            memory.save(memory_path)
            collect_committed_artifacts(action_dir)
            if terminate_search:
                break

        replay_completion_failed = False
        if replay_enabled:
            replay_control = memory.validate_replay_control(
                replay_metadata
            )
            replay_state = ReplayState.from_dict(replay_control["state"])
            memory.refresh_budget()
            replay_elapsed = float(memory.budget["time_elapsed"])
            replay_observation = build_replay_observation(analysis)
            replay_phase = replay_policy.phase(
                replay_state,
                replay_elapsed,
                replay_observation,
            )
            if not replay_state.closeout_complete and replay_phase == "closeout":
                memory.project_graph_best()
                best_id = str(memory.candidate_graph["current_best_candidate_id"])
                best_node = memory.candidate_node(best_id)
                if best_node is None or best_node.get("eligible_for_beam") is not True:
                    raise RuntimeError("replay closeout graph best is not selectable")
                best_path = Path(str(best_node["artifact_path"])).resolve()
                best_sha256 = sha256_file(best_path)
                if best_sha256 != best_node.get("artifact_sha256"):
                    raise RuntimeError("replay closeout graph best identity changed")
                validate_full_recurrence_lineage(memory, run_dir, best_node)
                closeout_record = {
                    "schema_version": 1,
                    "completed_at_s": replay_elapsed,
                    "selected_candidate_id": best_id,
                    "artifact_sha256": best_sha256,
                    "artifact_size_bytes": best_path.stat().st_size,
                    "central_measurement_complete": bool(
                        best_node.get("measurement_complete")
                    ),
                    "eligible_for_beam": True,
                    "recurrence_lineage_valid": True,
                }
                replay_state = replay_policy.closeout(
                    replay_state,
                    replay_elapsed,
                    replay_observation,
                )
                memory.record_replay_closeout(closeout_record)
                memory.set_replay_state(replay_state.to_dict())
                memory.save(memory_path)
            elif not replay_state.closeout_complete:
                replay_completion_failed = True
                memory.record_stop(
                    source="agent",
                    reason="replay_incomplete_before_closeout",
                    rationale=f"replay stopped in phase {replay_phase}",
                    hard_mechanical=True,
                )
                memory.save(memory_path)

        submission_manifest = None
        if submission_mode:
            if not mcp_cleaned:
                await mcp.cleanup()
                mcp_cleaned = True
            submission_manifest = await finalize_submission(
                memory,
                run_dir=run_dir,
                output_dcp=output_dcp,
                cfg=cfg,
                equivalence_runner=equivalence_runner,
            )
            if replay_enabled and not replay_completion_failed:
                output_path = Path(output_dcp).resolve()
                output_sha256 = sha256_file(output_path)
                output_info = submission_manifest.get("output") or {}
                if (
                    submission_manifest.get("state")
                    not in {"committed", "fallback_committed"}
                    or output_info.get("sha256") != output_sha256
                ):
                    raise RuntimeError(
                        "replay submission manifest is not bound to output bytes"
                    )
                replay_control = memory.validate_replay_control(
                    replay_metadata
                )
                replay_state = ReplayState.from_dict(replay_control["state"])
                memory.refresh_budget()
                replay_elapsed = float(memory.budget["time_elapsed"])
                replay_state = replay_policy.submit(
                    replay_state,
                    replay_elapsed,
                    build_replay_observation(analysis),
                )
                attempted_candidate_id = submission_manifest.get(
                    "selected_candidate_id"
                )
                output_candidate_id = (
                    attempted_candidate_id
                    if submission_manifest["state"] == "committed"
                    else memory.candidate_graph["root_candidate_id"]
                )
                output_node = memory.candidate_node(str(output_candidate_id))
                if (
                    output_node is None
                    or output_node.get("artifact_sha256") != output_sha256
                ):
                    raise RuntimeError(
                        "replay submission output is not bound to its graph candidate"
                    )
                memory.record_replay_submission({
                    "schema_version": 1,
                    "completed_at_s": replay_elapsed,
                    "manifest_state": submission_manifest["state"],
                    "attempted_candidate_id": attempted_candidate_id,
                    "output_candidate_id": output_candidate_id,
                    "optimized_candidate_accepted": (
                        submission_manifest["state"] == "committed"
                    ),
                    "output_sha256": output_sha256,
                    "output_size_bytes": output_path.stat().st_size,
                })
                memory.set_replay_state(replay_state.to_dict())
            memory.save(memory_path)
        if (
            not memory.stop
            and memory.round >= max_rounds
            and not staged_react_enabled
        ):
            memory.record_stop(
                source="agent",
                reason="max_rounds_reached",
                rationale=f"configured maximum of {max_rounds} rounds reached",
                hard_mechanical=True,
            )
        if not submission_mode:
            export_best()
        memory.refresh_budget()
        memory.save(memory_path)

        final_wns = float(memory.current_best["wns"])
        final_fmax = memory.current_best.get("fmax")
        baseline_period = memory.baseline.get("clock_period")
        final_period = memory.current_clock_period()
        gain = memory.fmax_gain_mhz()
        score_state = memory.contest_score_snapshot()
        print("\n" + "=" * 60)
        print("FDAgents Optimization Summary")
        print("=" * 60)
        print(f"  Rounds:          {memory.round}")
        if llm is not None:
            summary = llm.summary()
            print(f"  LLM model:       {summary['model']}")
            print(f"  LLM calls:       {summary['calls']}")
            print(
                f"  LLM tokens:      in {summary['input_tokens']:,} "
                f"(cached {summary['cached_input_tokens']:,}) / out "
                f"{summary['output_tokens']:,} (reasoning "
                f"{summary['reasoning_tokens']:,})"
            )
            print(f"  LLM cost:        ${summary['total_cost_usd']:.4f}")
        baseline_note = (
            f" @ {float(baseline_period):.3f} ns period"
            if baseline_period is not None else ""
        )
        print(f"  Baseline WNS:    {baseline_wns:.3f} ns{baseline_note}")
        frame_changed = (
            baseline_period is not None and final_period is not None
            and abs(float(final_period) - float(baseline_period)) > 1e-6
        )
        if frame_changed:
            print(
                f"  Final WNS:       {final_wns:.3f} ns"
                f" @ {float(final_period):.3f} ns period"
            )
            if final_fmax:
                equivalent_wns = float(baseline_period) - 1000.0 / float(final_fmax)
                print(
                    f"  Equiv. WNS:      {equivalent_wns:.3f} ns @ baseline period"
                    f"  ({equivalent_wns - baseline_wns:+.3f})"
                )
        else:
            print(
                f"  Final WNS:       {final_wns:.3f} ns  "
                f"({final_wns - baseline_wns:+.3f})"
            )
        if final_fmax:
            gain_note = f"  (gain {gain:+.2f} MHz)" if gain is not None else ""
            print(f"  Final Fmax:      {final_fmax:.2f} MHz{gain_note}")
        print(f"  Elapsed:         {memory.budget['time_elapsed']:.0f}s")
        if score_state is not None:
            print(f"  Tracked cost:    ${score_state['llm_cost_usd']:.4f}")
            print(f"  Score factor:    {score_state['penalty_factor']:.6f}")
            print(f"  Projected score: {score_state['score']:.3f}")
            print("  Score validity:  assumes final validation passes")
        print(f"  Output DCP:      {output_dcp}")
        print(f"  Run directory:   {run_dir}")
        print("=" * 60)
        return 1 if replay_completion_failed else 0

    except KeyboardInterrupt:
        logger.info("[agent] interrupted")
        export_best()
        if memory is not None:
            memory.record_stop(
                source="operator",
                reason="keyboard_interrupt",
                hard_mechanical=True,
            )
            memory.refresh_budget()
            memory.save(memory_path)
        return 130
    except Exception:  # noqa: BLE001
        logger.exception("[agent] fatal error")
        export_best()
        if memory is not None:
            memory.record_stop(
                source="agent",
                reason="fatal_error",
                hard_mechanical=True,
            )
            memory.refresh_budget()
            memory.save(memory_path)
        return 1
    finally:
        if not mcp_cleaned:
            try:
                await mcp.cleanup()
            except Exception:  # noqa: BLE001 - the primary failure is already recorded
                logger.exception("[agent] final MCP cleanup failed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def default_output_dcp_path(input_dcp: Path, timestamp: Optional[str] = None) -> Path:
    if timestamp is None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
    return input_dcp.parent / f"{input_dcp.stem}_optimized-{timestamp}.dcp"


def main() -> int:
    parser = argparse.ArgumentParser(description="FDAgents DCP timing optimizer")
    parser.add_argument("input_dcp", type=Path)
    parser.add_argument("--output", type=Path, default=None, help="output DCP path")
    parser.add_argument("--model", default=None, help="LLM model name")
    parser.add_argument("--time-limit", type=float, default=None, help="seconds")
    parser.add_argument(
        "--rank1-score",
        type=float,
        default=None,
        help="published rank-1 score used for the fixed 1.05/0.8 Fmax target",
    )
    parser.add_argument(
        "--score-baseline-fmax-mhz",
        type=float,
        default=None,
        help="official unoptimized Fmax baseline used by the contest score",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="use deterministic policy selection without an LLM",
    )
    parser.add_argument(
        "--replay-policy",
        choices=[REPLAY_POLICY_ID],
        default=None,
        help="run the identity-free one-hour replay policy",
    )
    parser.add_argument("--config", default=None, help="user config YAML")
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument(
        "--isolation-audit",
        type=Path,
        default=None,
        help="launcher-produced protocol/deployment cold-isolation audit",
    )
    parser.add_argument(
        "--resume", action="store_true", help="resume explicit existing --run-dir"
    )
    parser.add_argument(
        "--submission", action="store_true", help="require strict equivalence export"
    )
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--dcp-manifest",
        type=Path,
        help="scope-specific DCP manifest required for cold no-replay runs",
    )
    parser.add_argument(
        "--engineering-regression",
        action="store_true",
        help=(
            "use an explicit no-replay engineering-regression manifest; "
            "results are not unbiased generalization evidence"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    input_dcp = args.input_dcp.expanduser().resolve()
    if not input_dcp.exists():
        print(f"ERROR: input DCP not found: {input_dcp}", file=sys.stderr)
        return 2
    output_dcp = (
        args.output.expanduser().resolve()
        if args.output
        else default_output_dcp_path(input_dcp)
    )

    return asyncio.run(
        run(
            input_dcp=input_dcp,
            output_dcp=output_dcp,
            model=args.model,
            time_limit=args.time_limit,
            debug=args.debug,
            run_dir=args.run_dir,
            use_llm=not args.no_llm,
            config_path=args.config,
            resume=args.resume,
            submission_mode=args.submission,
            rank1_score=args.rank1_score,
            score_baseline_fmax_mhz=args.score_baseline_fmax_mhz,
            replay_policy_id=args.replay_policy,
            isolation_audit_path=args.isolation_audit,
            dcp_manifest_path=args.dcp_manifest,
            engineering_regression=args.engineering_regression,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
