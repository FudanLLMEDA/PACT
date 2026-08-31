"""Positive-allowlist prompt and typed-tool generation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Optional

from .config import Config, get_config
from .knowledge_base import KnowledgeBase
from .regarith_tools.agent_evidence import (
    validate_concurrent_proof_decision_packet,
)
from .replay import (
    llm_selection_schema,
    validate_anonymous_observation,
)


_BOTTLENECKS = {
    "routing",
    "placement",
    "logic_depth",
    "fanout",
    "clock_constraint",
    "mixed",
    "unknown",
}
_EVIDENCE_REF_RE = re.compile(
    r"^(?:report|knowledge):[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*:[1-9][0-9]*$"
)
_SAFE_STATE_NUMBERS = (
    "round",
    "rounds_total",
    "outcomes_total",
    "current_clock_period_ns",
    "current_wns",
    "current_fmax_mhz",
    "fmax_gain_mhz",
    "probe_calls_used",
    "telemetry_epoch",
)
_SAFE_BUDGET_NUMBERS = (
    "time_elapsed_s",
    "time_remaining_s",
    "time_limit_s",
    "token_remaining_usd",
)
_SAFE_RUN_CONTRACT_FIELDS = (
    "saturation_is_informational",
    "score_hurdles_are_informational",
    "configured_runtime_fit_is_informational",
    "hard_mechanical_deadline_enforced",
    "time_limit_s",
    "time_safety_s",
    "execution_window_remaining_s",
    "score_projection_can_veto_actions",
)
_SAFE_FEEDBACK_NUMBERS = (
    "elapsed_s",
    "runtime_s",
    "wns_ns",
    "fmax_mhz",
    "delta_wns_ns",
    "delta_fmax_mhz",
    "avg_route_pct",
    "avg_logic_pct",
    "logic_levels",
    "fanout",
    "structure_count",
    "structure_member_count",
    "analyzed_structure_member_count",
    "relocation_eligible_count",
)
_SAFE_FEEDBACK_BOOLEANS = (
    "success",
    "legal",
    "fully_routed",
    "timed_out",
    "measurement_complete",
    "analysis_truncated",
)
_SAFE_FEEDBACK_STATUSES = {
    "error",
    "success",
    "failed",
    "rejected",
    "timeout",
    "unknown",
}
# Only names already registered by the mechanics layer can be emitted in a
# Decision prompt. Configuration may remove entries, but cannot add new ones.
_REGISTERED_GENERIC_PROBES = {
    "vivado": {
        "report_route_status",
        "report_timing_summary",
        "get_wns",
        "get_critical_high_fanout_nets",
        "extract_critical_path_cells",
        "extract_critical_path_pins",
        "report_timing_path_profile",
        "report_utilization_for_pblock",
        "analyze_carry_clouds",
        "analyze_hard_macro_cascades",
        "screen_fixed_point_dsp_contract",
        "recover_banked_select_predicate_contract",
        "recover_banked_ram_operator_contract",
        "recover_commutative_reassociation_contract",
        "recover_registered_dsp_lane_contract",
        "recover_control_to_data_state_equation_contract",
        "recover_ff_srl_boundary_bridge_contract",
        "recover_memory_write_buffer_forward_contract",
        "recover_multilevel_control_distribution_contract",
        "recover_registered_parity_reassociation_contract",
        "suggest_slice_pblock_ranges",
    },
    "rapidwright": {
        "get_supported_devices",
        "get_device_info",
        "report_approx_timing",
        "get_design_info",
        "search_cells",
        "get_tile_info",
        "search_sites",
        "inspect_lut_pin_swap_candidates",
        "analyze_critical_path_spread",
        "analyze_fabric_for_pblock",
        "convert_fabric_region_to_pblock",
        "analyze_net_detour",
        "analyze_route_connections",
        "operator_mining",
        "recover_recurrence_contract",
        "recover_sequential_operator_contract",
        "recover_consumer_driven_operator_contract",
        "recover_registered_dead_state_contract",
        "find_proven_equivalent_sources",
    },
}
_ZERO_ARGUMENT_GENERIC_PROBES = {
    "vivado": {
        "report_route_status",
        "report_timing_summary",
        "get_wns",
        "get_critical_high_fanout_nets",
        "extract_critical_path_cells",
        "extract_critical_path_pins",
        "report_timing_path_profile",
        "analyze_carry_clouds",
        "analyze_hard_macro_cascades",
    },
    "rapidwright": {
        "get_supported_devices",
        "get_design_info",
        "report_approx_timing",
    },
}

_CURRENT_DESIGN_ZERO_ARGUMENT_PROBES = {
    "vivado": set(_ZERO_ARGUMENT_GENERIC_PROBES["vivado"]),
    "rapidwright": {
        "get_design_info",
        "report_approx_timing",
    },
}


def registered_read_only_probes(cfg: Optional[Config] = None) -> dict[str, list[str]]:
    """Return the configured subset of public, mechanically registered probes."""
    cfg = cfg or get_config()
    configured = cfg.get("legality.allowed_custom_tools", {}) or {}
    result = {}
    for server, registered in _REGISTERED_GENERIC_PROBES.items():
        enabled = configured.get(server)
        if not isinstance(enabled, list):
            continue
        selected = sorted(
            item
            for item in enabled
            if isinstance(item, str)
            and item in registered
        )
        if selected:
            result[server] = selected
    return result


def registered_zero_argument_current_design_probes(
    cfg: Optional[Config] = None,
) -> dict[str, list[str]]:
    """Return menu-safe probes that observe the already-open current design.

    Parameterized probes require a typed same-run handoff and therefore cannot
    be published as empty-argument Action Cards. Platform inventory calls such
    as ``get_supported_devices`` are intentionally excluded because they do not
    reduce uncertainty about the current timing wall.
    """
    registered = registered_read_only_probes(cfg)
    return {
        server: [
            tool for tool in tools
            if tool in _CURRENT_DESIGN_ZERO_ARGUMENT_PROBES.get(server, set())
        ]
        for server, tools in registered.items()
        if any(
            tool in _CURRENT_DESIGN_ZERO_ARGUMENT_PROBES.get(server, set())
            for tool in tools
        )
    }


def probe_parameter_fingerprint(
    server: object,
    tool: object,
    arguments: object,
    question: object,
) -> str:
    """Bind one typed probe recommendation without fuzzy text matching.

    The question is the recommendation's typed intent parameter.  Including it
    distinguishes two recommendations that intentionally invoke the same
    zero-argument server/tool pair while retaining exact runtime arguments.
    """
    if (
        server not in {"vivado", "rapidwright"}
        or not isinstance(tool, str)
        or not tool
        or not isinstance(arguments, dict)
        or not isinstance(question, str)
        or not question
    ):
        raise ValueError("probe fingerprint inputs are malformed")
    try:
        encoded = json.dumps(
            {
                "arguments": arguments,
                "question": question,
                "server": server,
                "tool": tool,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("probe arguments are not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _finite_number(value: object) -> Optional[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return value


def _safe_evidence_refs(value: object, *, scope: Optional[str] = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("evidence refs must be a list")
    result = []
    for item in value:
        if not isinstance(item, str) or _EVIDENCE_REF_RE.fullmatch(item) is None:
            raise ValueError(f"invalid short evidence ref {item!r}")
        if scope is not None and not item.startswith(f"{scope}:"):
            raise ValueError(f"evidence ref {item!r} has the wrong scope")
        result.append(item)
    return result


def _safe_manifest(value: object, *, scope: str) -> dict:
    result: dict = {"scope": scope, "documents": []}
    if not isinstance(value, dict) or value.get("scope") != scope:
        return result
    epoch = value.get("telemetry_epoch")
    if type(epoch) is int and epoch >= 0:
        result["telemetry_epoch"] = epoch
    documents = value.get("documents")
    if not isinstance(documents, list):
        return result
    for item in documents:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        lines = item.get("lines")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 256
            or name.startswith("/")
            or ".." in name.replace("\\", "/").split("/")
            or type(lines) is not int
            or lines < 0
        ):
            continue
        result["documents"].append({"name": name, "lines": lines})
    return result


def _safe_state(state: object, *, report_only: bool = False) -> dict:
    if not isinstance(state, dict):
        return {}
    allowed_numbers = (
        ("round", "current_clock_period_ns", "current_wns", "current_fmax_mhz", "telemetry_epoch")
        if report_only
        else _SAFE_STATE_NUMBERS
    )
    result = {}
    for key in allowed_numbers:
        number = _finite_number(state.get(key))
        if number is not None:
            result[key] = number
    bottleneck = state.get("bottleneck")
    if not report_only and bottleneck in _BOTTLENECKS:
        result["bottleneck"] = bottleneck
    concurrent_packet = validate_concurrent_proof_decision_packet(
        state.get("concurrent_proof_decision_packet")
    )
    if concurrent_packet is not None:
        result["concurrent_proof_decision_packet"] = concurrent_packet
    if report_only:
        return result
    budget = state.get("budget")
    if isinstance(budget, dict):
        safe_budget = {}
        for key in _SAFE_BUDGET_NUMBERS:
            number = _finite_number(budget.get(key))
            if number is not None:
                safe_budget[key] = number
        if safe_budget:
            result["budget"] = safe_budget
    exhausted = state.get("exhausted_probes")
    if isinstance(exhausted, list):
        safe_exhausted = []
        for entry in exhausted[:32]:
            if not isinstance(entry, dict):
                continue
            server = entry.get("server")
            tool = entry.get("tool")
            if not isinstance(server, str) or not isinstance(tool, str):
                continue
            if server not in _REGISTERED_GENERIC_PROBES:
                continue
            if tool not in _REGISTERED_GENERIC_PROBES[server]:
                continue
            item = {"server": server, "tool": tool}
            number = _finite_number(entry.get("first_observed_round"))
            if number is not None:
                item["first_observed_round"] = number
            safe_exhausted.append(item)
        if safe_exhausted:
            result["exhausted_probes"] = safe_exhausted
    exhausted_actions = state.get("exhausted_actions")
    if isinstance(exhausted_actions, list):
        safe_actions = []
        registered = registered_action_targets_by_skill(
            state.get("suggested_targets")
        )
        for entry in exhausted_actions[:16]:
            if not isinstance(entry, dict):
                continue
            skill = entry.get("skill")
            target = entry.get("target")
            disposition = entry.get("disposition")
            if (
                isinstance(skill, str)
                and isinstance(target, str)
                and target in registered.get(skill, [])
                and disposition in {"measured", "neutral_replan"}
            ):
                safe_actions.append({
                    "skill": skill,
                    "target": target,
                    "disposition": disposition,
                })
        if safe_actions:
            result["exhausted_actions"] = safe_actions
    lineage_actions = state.get("measured_actions_on_current_lineage")
    if isinstance(lineage_actions, list):
        safe_lineage_actions = []
        registered = registered_action_targets_by_skill(
            state.get("suggested_targets")
        )
        for entry in lineage_actions[:16]:
            if not isinstance(entry, dict):
                continue
            skill = entry.get("skill")
            target = entry.get("target")
            measurements = entry.get("measurements")
            accepted = entry.get("accepted_measurements")
            if (
                isinstance(skill, str)
                and isinstance(target, str)
                and target in registered.get(skill, [])
                and type(measurements) is int
                and type(accepted) is int
                and 1 <= measurements <= 1000
                and 0 <= accepted <= measurements
            ):
                safe_lineage_actions.append({
                    "skill": skill,
                    "target": target,
                    "measurements": measurements,
                    "accepted_measurements": accepted,
                })
        if safe_lineage_actions:
            result["measured_actions_on_current_lineage"] = safe_lineage_actions
    visibility = state.get("operator_direction_visibility_packet")
    if isinstance(visibility, dict):
        rows = visibility.get("direction_status")
        safe_rows = []
        registered = registered_action_targets_by_skill(
            state.get("suggested_targets")
        )
        if isinstance(rows, list):
            for row in rows[:32]:
                if not isinstance(row, dict):
                    continue
                skill = row.get("skill")
                target = row.get("target")
                status = row.get("status")
                blockers = row.get("nomination_blockers")
                if (
                    isinstance(skill, str)
                    and isinstance(target, str)
                    and target in registered.get(skill, [])
                    and status in {"eligible", "exhausted", "ineligible"}
                    and isinstance(blockers, list)
                    and all(isinstance(item, str) for item in blockers)
                ):
                    safe_rows.append({
                        "skill": skill,
                        "target": target,
                        "status": status,
                        "current_seed_eligible": (
                            row.get("current_seed_eligible") is True
                        ),
                        "authorization_state": (
                            row.get("authorization_state")
                            if row.get("authorization_state") in {
                                "live_authorized_executable_now",
                                "hypothesis_only",
                                "blocked",
                            }
                            else "blocked"
                        ),
                        "evidence_complete": row.get("evidence_complete") is True,
                        "nomination_blockers": blockers,
                    })
        if safe_rows:
            result["operator_direction_visibility_packet"] = {
                "authority": "tool_owned_current_seed_direction_visibility",
                "direction_status": safe_rows,
                "remaining_eligible_direction_count": sum(
                    row["status"] == "eligible" for row in safe_rows
                ),
                "no_supported_mutation_permitted": not any(
                    row["status"] == "eligible" for row in safe_rows
                ),
            }
    contract = state.get("run_contract")
    if isinstance(contract, dict):
        safe_contract = {}
        for key in _SAFE_RUN_CONTRACT_FIELDS:
            value = contract.get(key)
            if isinstance(value, bool):
                safe_contract[key] = value
            else:
                number = _finite_number(value)
                if number is not None:
                    safe_contract[key] = number
        if safe_contract:
            result["run_contract"] = safe_contract
    return result


def _safe_probe_feedback(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _SAFE_FEEDBACK_NUMBERS:
        number = _finite_number(value.get(key))
        if number is not None:
            result[key] = number
    for key in _SAFE_FEEDBACK_BOOLEANS:
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    status = value.get("status")
    if status in _SAFE_FEEDBACK_STATUSES:
        result["status"] = status
    server = value.get("server")
    if server in {"vivado", "rapidwright"}:
        result["server"] = server
    structure_type = value.get("structure_type")
    if structure_type in {"carry_cloud", "hard_macro_cascade"}:
        result["structure_type"] = structure_type
    return result


def _safe_report_analysis(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    bottleneck = value.get("bottleneck")
    unknowns = value.get("unknowns")
    bottleneck_refs = _safe_evidence_refs(
        value.get("bottleneck_evidence_refs"), scope="report"
    )
    diagnosis_refs = _safe_evidence_refs(
        value.get("diagnosis_evidence_refs"), scope="report"
    )
    return {
        "bottleneck": bottleneck if bottleneck in _BOTTLENECKS else "unknown",
        "bottleneck_evidence_refs": bottleneck_refs,
        "diagnosis_evidence_refs": diagnosis_refs,
        "evidence_refs": list(dict.fromkeys(bottleneck_refs + diagnosis_refs)),
        "unknown_count": len(unknowns) if isinstance(unknowns, list) else 0,
    }


def _safe_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]






CONTEST_BRIEF = """\
You are the Decision Agent in an autonomous timing-optimization loop.
Choose only from the registered generic mechanics supplied below. Every trial
is measured through trusted legality, functional-equivalence, timing, and
retention gates. A failed or rejected trial must leave the immutable incumbent
available. The wall-clock deadline and remaining LLM cost are mechanical limits.

Use current measured evidence rather than remembered identities or outcomes.
Treat estimates as uncertain judgment, never as framework-generated strategy.
Do not emit raw backend commands or physical object names. Plain text cannot
authorize execution; only one validated terminal function call can do so.
"""

DECISION_GUIDANCE = """\
Decision craft (advice, not rules):
- Compare the current wall, the Knowledge Agent's cited classification, and
  the expected mechanism before selecting a registered mechanic.
- Prior outcomes are observations, not a fixed sequence, threshold, or veto.
- A read-only probe is appropriate when its answer can distinguish competing
  classifications or expose a missing proof obligation.
- Retain the immutable incumbent while any trial is measured.
- After a retained change, reclassify the residual wall instead of transferring
  the parent classification unchanged.
- Stop when the remaining mechanical budget cannot support a useful measured
  cycle or when current evidence supports preserving the incumbent.
"""

STAGE_SCHEMAS = {
    "analyze": """\
Task `analyze`: return one JSON object with bottleneck, diagnosis, strategy,
and stop_hint. Strategy entries may use only registered mechanic names and
registered targets. Do not use remembered examples or identities.""",
    "decide": """\
Task `decide`: return one JSON object selecting run_skill, probe,
restart_vivado, or save_and_exit.
A run_skill reply includes skill, target, params, expectation, and the existing
structured score_estimate contract. A stop reply includes stop_reason and
stop_rationale. Do not emit physical object names or backend commands.""",
    "probe": """\
Task `probe`: return one JSON object selecting an allowlisted read-only server
and tool with typed arguments and a question.""",
}


REPORT_ANALYSIS_SYSTEM = """\
You are the Report Agent. Analyze only the immutable report corpus exposed by
grep_reports. No mutation mechanic, target objective, prior outcome library, or
other advisory source is available. Do not guess report facts. Bind bottleneck
and diagnosis independently to exact displayed short evidence_ref values. Do
not use one result-level citation pool. Finish with
complete_report_analysis. Plain text cannot complete this stage.
"""

REPORT_ANALYSIS_TOOL = {
    "type": "function",
    "name": "complete_report_analysis",
    "description": "Finish the report-only diagnosis with displayed evidence.",
    "parameters": {
        "type": "object",
        "properties": {
            "bottleneck": {"type": "string", "enum": sorted(_BOTTLENECKS)},
            "diagnosis": {"type": "string"},
            "bottleneck_evidence_refs": {
                "type": "array", "items": {"type": "string"}
            },
            "diagnosis_evidence_refs": {
                "type": "array", "items": {"type": "string"}
            },
            "unknowns": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "bottleneck",
            "diagnosis",
            "bottleneck_evidence_refs",
            "diagnosis_evidence_refs",
            "unknowns",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}


KNOWLEDGE_RESEARCH_SYSTEM = """\
You are the independent Knowledge Agent. Interpret the frozen, citation-validated
Report result using only the generic operator taxonomy exposed by grep_knowledge.
You do not select, authorize, recommend, or parameterize actions or probes; the
Action Menu is the sole execution authority downstream.

Use only report evidence_ref values already present in the frozen Report result
as live evidence. Use grep_knowledge only to retrieve taxonomy vocabulary needed
to interpret that diagnosis. Prefer one focused taxonomy lookup and finish as
soon as the classification is supported; do not search the report corpus again.
Return at most three claim-local classifications plus a concise residual_update.
Every classification must cite at least one displayed report ref and one displayed
taxonomy ref. The two top-level evidence lists must be the exact deduplicated
unions of the claim-local lists. Finish with complete_knowledge_research; plain
text cannot complete this stage.
"""

KNOWLEDGE_RESEARCH_TOOL = {
    "type": "function",
    "name": "complete_knowledge_research",
    "description": "Freeze the cited taxonomy interpretation for Action Menu selection.",
    "parameters": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "level": {
                            "type": "string",
                            "enum": ["L0", "L1", "L2", "L3", "L4", "L5", "unknown"],
                        },
                        "pattern": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "live_evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "knowledge_evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "applicability": {"type": "string"},
                        "contraindications": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "missing_evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "level",
                        "pattern",
                        "confidence",
                        "live_evidence_refs",
                        "knowledge_evidence_refs",
                        "applicability",
                        "contraindications",
                        "missing_evidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "residual_update": {"type": "string"},
            "report_evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "knowledge_evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "classifications",
            "residual_update",
            "report_evidence_refs",
            "knowledge_evidence_refs",
        ],
        "additionalProperties": False,
    },
    "strict": True,
}


def registered_action_skills(
    targets_by_skill: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[str]:
    """Public mechanic names Decision may execute."""
    targets = targets_by_skill or registered_action_targets_by_skill()
    return sorted(targets)


def registered_action_targets_by_skill(
    suggested_targets: Optional[Mapping[str, object]] = None,
    *,
    cold_isolation: bool = False,
) -> dict[str, list[str]]:
    """Public target names each mechanic actually implements.

    The flat union is only safe as a schema enum; a target that belongs to a
    different mechanic is rejected by the capability layer at execution time,
    which costs a whole reasoning cycle. Callers use this mapping to reject the
    mismatch while the model can still repair it.
    """
    from .skills import SKILLS
    from .skills.equivalent_source_remap import AUTO_TARGET
    from .skills.implementation_recipe import IMPLEMENTATION_RECIPE_TARGETS
    from .skills.operator_rewrite import OPERATOR_REWRITE_REGISTRY
    from .skills.phys_opt import PHYS_OPT_PRESETS
    from .skills.structure_relocation import STRUCTURE_RELOCATION_TARGETS

    explicit: dict[str, set[str]] = {
        "implementation_recipe": set(map(str, IMPLEMENTATION_RECIPE_TARGETS)),
        "fresh_place_route": {"auto"},
        "pblock": {"auto"},
        "per_net_unroute": {"auto"},
        "hard_macro_move": {"auto"},
        "phys_opt": set(map(str, PHYS_OPT_PRESETS)),
        "critical_net_reroute": {"boundary_link", "worst_source"},
        "selective_branch_reroute": {"auto_high_detour"},
        "structure_relocation": set(map(str, STRUCTURE_RELOCATION_TARGETS)),
        "equivalent_source_remap": {str(AUTO_TARGET)},
        "operator_rewrite": set(map(str, OPERATOR_REWRITE_REGISTRY)),
        "semantic_replay": {"auto"},
    }
    unavailable = {"checkpoint_import"}
    dynamic = {"fanout_opt", "force_replicate"}
    suggestions = suggested_targets if isinstance(suggested_targets, Mapping) else {}
    result: dict[str, list[str]] = {}
    for skill in SKILLS:
        if skill == "custom" or skill in unavailable:
            continue
        if cold_isolation and skill == "semantic_replay":
            continue
        # A mechanic that publishes an explicit target list is contracted to
        # exactly that list. Only a mechanic that derives its own target from
        # the live design accepts "auto"; publishing both would let Knowledge
        # name a target Decision is not allowed to execute.
        published = explicit.get(skill)
        if published:
            result[skill] = sorted(published)
            continue
        if skill in dynamic:
            values = suggestions.get(skill)
            safe_values = (
                {
                    str(item)
                    for item in values
                    if isinstance(item, str) and item and len(item) <= 1024
                }
                if isinstance(values, Sequence) and not isinstance(values, str)
                else set()
            )
            if skill == "force_replicate":
                safe_values.add("auto_highfanout_group")
            if safe_values:
                result[skill] = sorted(safe_values)
            continue
        result[skill] = ["auto"]
    return result


def registered_action_targets(
    targets_by_skill: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[str]:
    """Public target names Decision may execute."""
    targets = targets_by_skill or registered_action_targets_by_skill()
    return sorted({str(target) for values in targets.values() for target in values})






def _terminal_action_tools(
    targets_by_skill: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[dict]:
    registered_skills = registered_action_skills(targets_by_skill)
    registered_targets = registered_action_targets(targets_by_skill)
    registered_probes = sorted({
        name
        for names in registered_read_only_probes().values()
        for name in names
    })
    quantiles = {
        "type": "object",
        "properties": {
            "p10": {"type": "number"},
            "p50": {"type": "number"},
            "p90": {"type": "number"},
        },
        "required": ["p10", "p50", "p90"],
        "additionalProperties": False,
    }
    risk = {
        "type": "object",
        "properties": {
            "candidate_regression_probability": {"type": "number"},
            "no_legal_candidate_probability": {"type": "number"},
            "timeout_probability": {"type": "number"},
        },
        "required": [
            "candidate_regression_probability",
            "no_legal_candidate_probability",
            "timeout_probability",
        ],
        "additionalProperties": False,
    }
    score_estimate = {
        "type": "object",
        "properties": {
            "version": {"type": "integer", "enum": [2]},
            "gain_vs_global_best_mhz": quantiles,
            "action_cycle_runtime_s": quantiles,
            "action_risk": risk,
            "basis": {"type": "string"},
        },
        "required": [
            "version",
            "gain_vs_global_best_mhz",
            "action_cycle_runtime_s",
            "action_risk",
            "basis",
        ],
        "additionalProperties": False,
    }
    shared = {
        "expectation": {"type": "string"},
        "knowledge_comparison": {"type": "string"},
        "override_knowledge_direction": {"type": "boolean"},
        "failure_update": {"type": "string"},
        "reflection": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    }
    return [
        {
            "type": "function",
            "name": "run_skill",
            "description": "Select one registered generic mutation mechanic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "enum": registered_skills},
                    "target": {"type": "string", "enum": registered_targets},
                    "params": {"type": "object"},
                    "score_estimate": score_estimate,
                    **shared,
                },
                "required": [
                    "skill",
                    "target",
                    "params",
                    "score_estimate",
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
            "strict": False,
        },
        {
            "type": "function",
            "name": "probe",
            "description": "Select one registered read-only probe.",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "enum": ["vivado", "rapidwright"]},
                    "tool": {"type": "string", "enum": registered_probes},
                    "arguments": {"type": "object"},
                    "parameter_fingerprint": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                        "description": (
                            "Copy the exact tool-owned fingerprint from the "
                            "selected frozen Knowledge probe."
                        ),
                    },
                    "question": {"type": "string"},
                    **shared,
                },
                "required": [
                    "server",
                    "tool",
                    "arguments",
                    "parameter_fingerprint",
                    "question",
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
            "strict": False,
        },
        {
            "type": "function",
            "name": "restart_vivado",
            "description": (
                "Recover the Vivado process and restore the immutable current "
                "incumbent. This produces no candidate and accepts no checkpoint, "
                "path, Tcl, server, tool, timeout, or backend argument fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {**shared},
                "required": [
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
            "strict": False,
        },
        {
            "type": "function",
            "name": "save_and_exit",
            "description": (
                "Stop and preserve the immutable incumbent. Copy every row from "
                "operator_direction_visibility_packet.direction_status exactly; "
                "no_supported_mutation is legal only when no eligible row remains."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stop_reason": {"type": "string"},
                    "stop_rationale": {"type": "string"},
                    "direction_status": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "skill": {"type": "string", "enum": registered_skills},
                                "target": {"type": "string", "enum": registered_targets},
                                "status": {
                                    "type": "string",
                                    "enum": ["eligible", "exhausted", "ineligible"],
                                },
                                "current_seed_eligible": {"type": "boolean"},
                                "authorization_state": {
                                    "type": "string",
                                    "enum": [
                                        "live_authorized_executable_now",
                                        "hypothesis_only",
                                        "blocked",
                                    ],
                                },
                                "evidence_complete": {"type": "boolean"},
                                "nomination_blockers": {
                                    "type": "array", "items": {"type": "string"}
                                },
                            },
                            "required": [
                                "skill", "target", "status",
                                "current_seed_eligible", "authorization_state",
                                "evidence_complete",
                                "nomination_blockers",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    **shared,
                },
                "required": [
                    "stop_reason",
                    "stop_rationale",
                    "direction_status",
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
            "strict": False,
        },
    ]


def build_action_tools(
    targets_by_skill: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[dict]:
    """Return a fresh baseline terminal schema with no calibration extension."""
    return _terminal_action_tools(targets_by_skill)


ACTION_TOOLS = build_action_tools()


RECONSIDER_SYSTEM = """\
Review one registered mechanic using only the anonymous measured summary in the
payload. Confirm exactly that mechanic, request a fresh plan, or stop while
preserving the incumbent. This review precedes a reversible measurement; it is
not a demand to prove future benefit. When confirm_required is true, the action
must be confirm: unknown effectiveness, absent learned prediction, or imperfect
bottleneck attribution are not measured contradictions. Replan is available
only when exact_measured_contradiction is true. Return one strict JSON object
with action, assessment, and reason.
"""


def build_reconsider_user(
    *,
    state: dict,
    provisional_action: dict,
    action_proof: dict,
) -> str:
    from .skills import SKILLS

    skill = provisional_action.get("skill")
    safe_action = {
        "action": provisional_action.get("action")
        if provisional_action.get("action") in {"run_skill", "probe", "save_and_exit"}
        else "run_skill"
    }
    if isinstance(skill, str) and skill in SKILLS:
        safe_action["registered_mechanic"] = skill
    proof_fields = (
        "contains_learned_prediction",
        "conditions_complete",
        "measurement_complete",
        "legal",
        "fully_routed",
        "prior_attempt_count",
        "observed_runtime_s",
        "retained_gain_mhz",
    )
    safe_proof = {}
    if isinstance(action_proof, dict):
        for key in proof_fields:
            value = action_proof.get(key)
            if isinstance(value, bool):
                safe_proof[key] = value
            else:
                number = _finite_number(value)
                if number is not None:
                    safe_proof[key] = number
        prior = action_proof.get("prior_evidence") or {}
        contradicting = prior.get("contradicting_evidence") or []
        exact_measured_contradiction = bool(contradicting)
        safe_proof["exact_measured_contradiction"] = exact_measured_contradiction
        safe_proof["confirm_required"] = not exact_measured_contradiction
    return json.dumps(
        {
            "task": "anonymous_action_review",
            "current_state": _safe_state(state),
            "provisional_action": safe_action,
            "measured_summary": safe_proof,
            "response_schema": {
                "action": "confirm|replan|save_and_exit",
                "assessment": "measurement-grounded assessment without requiring predicted benefit",
                "reason": "required for replan or save_and_exit",
            },
        },
        indent=1,
        sort_keys=True,
    )


def build_report_analysis_user(state: dict, report_manifest: dict) -> str:
    return json.dumps(
        {
            "task": "report_analysis",
            "safe_telemetry": _safe_state(state, report_only=True),
            "report_corpus": _safe_manifest(report_manifest, scope="report"),
            "instruction": (
                "Use grep_reports, then complete the report result with exact "
                "short refs that were displayed. For registered arithmetic, "
                "inspect action_decision_summary in "
                "registered_arithmetic_decision_packet.json before retrieving "
                "individual sibling details; it deterministically surfaces "
                "eligibility blockers, typed tool errors, direction deferrals, "
                "and the recorded backend regression caution."
                " If concurrent_proof_decision_packet is present in safe_telemetry, "
                "treat its typed state as tool-owned: the LLM cannot approve an "
                "in-flight, failed, misbound, or timed-out proof."
                " For constructive-accumulator and memory-forward actions, inspect "
                "metaloop_action_decision_summary in operator_action_preflight.json; "
                "these independent action classes do not use the generic registered-"
                "arithmetic eligible-family count or its singleton-DSP regression."
            ),
        },
        indent=1,
        sort_keys=True,
    )


def build_knowledge_research_user(
    report_analysis: dict,
    report_evidence_refs: list[str],
    knowledge_manifest: dict,
) -> str:
    report = _safe_report_analysis(report_analysis)
    report["evidence_refs"] = _safe_evidence_refs(
        report_evidence_refs, scope="report"
    )
    return json.dumps(
        {
            "task": "taxonomy_classification",
            "frozen_sanitized_report_result": report,
            "taxonomy_corpus": _safe_manifest(knowledge_manifest, scope="knowledge"),
            "instruction": (
                "Use only the report refs already present in frozen_sanitized_report_result "
                "as live evidence. Call grep_knowledge with a focused taxonomy query, then "
                "finish promptly. Do not inspect report documents, propose mechanics, "
                "recommend probes, or infer mutation authority. Return at most three "
                "claim-local classifications whose top-level report_evidence_refs and "
                "knowledge_evidence_refs are exact deduplicated unions."
            ),
        },
        indent=1,
        sort_keys=True,
    )


def registered_action_public_params() -> dict[str, list[str]]:
    """Public parameter names a mechanic accepts, where the ABI is exact.

    A fixed-preset mechanic carries all backend behaviour in its target, so any
    other key is rejected by the capability layer after a full reasoning cycle
    has already been spent. Only mechanics with a precisely known contract are
    listed; an absent entry means no public parameter has been published.
    """
    return {
        "phys_opt": ["min_remaining_s"],
        "operator_rewrite": [
            "family_fingerprint", "family_ref", "qor_profiles",
            "implementation_mode",
        ],
        "semantic_replay": [],
        "critical_net_reroute": [],
        "per_net_unroute": [
            "max_fanout",
            "min_fanout",
            "num_paths",
            "replication_passes",
            "route_timeout_s",
        ],
        "hard_macro_move": [
            "attempt_timeout_s",
            "max_candidates",
            "max_site_distance",
            "min_candidate_remaining_s",
            "num_paths",
        ],
        "implementation_recipe": [
            "min_candidate_remaining_s",
            "min_remaining_s",
        ],
    }


def _safe_mechanics_catalog(
    cfg: Config,
    targets_by_skill: Optional[Mapping[str, Sequence[str]]] = None,
) -> dict:
    from .skills.operator_rewrite import OPERATOR_REWRITE_REGISTRY

    registered = targets_by_skill or registered_action_targets_by_skill()
    targets: dict[str, object] = {
        str(skill): list(values) for skill, values in registered.items()
    }
    # Operator templates additionally publish their execution status so a
    # classify-only template cannot look like an available mutation.
    targets["operator_rewrite"] = {
        name: {"execution_status": "executable"}
        for name, spec in OPERATOR_REWRITE_REGISTRY.items()
        if spec.execution_status == "executable"
    }
    return {
        "mutation_skills": sorted(targets),
        "registered_targets": targets,
        "public_params": registered_action_public_params(),
        "read_only_probes": registered_read_only_probes(cfg),
    }


def build_research_system_prompt(
    cfg: Optional[Config] = None,
    targets_by_skill: Optional[Mapping[str, Sequence[str]]] = None,
) -> str:
    cfg = cfg or get_config()
    mechanics = _safe_mechanics_catalog(cfg, targets_by_skill)
    return "\n\n".join([
        CONTEST_BRIEF,
        DECISION_GUIDANCE,
        "Registered generic mechanics: " + json.dumps(mechanics, sort_keys=True),
        (
            'Operator staged runtime: {"fixed_full_cycle_estimate_s": null}. '
            "Read-only operator probes are continuous live evidence for Luna, "
            "never a hard trigger; their exact current-seed private handoffs bind "
            "the later typed action. mutation_eligible=false on a read-only probe "
            "does not veto a separately proof-gated typed action. Do not treat the "
            "one-hour contest cap as a fixed runtime estimate for every operator "
            "rewrite; discovery and proof may reject before mutation."
        ),
        (
            "Operator rewrite rule: select only templates marked executable. "
            "Structure relocation accepts complete registered structures; arbitrary "
            "cell clusters are not public mechanics. Repeated-arithmetic evidence is "
            "mechanically collected from the exact incumbent and appears only as a "
            "validated, identifier-free, hypothesis-only report. A positive "
            "operator preflight authorizes only an attempt; fresh private recovery "
            "and all rewrite proof gates remain mandatory."
        ),
        (
            "The frozen Knowledge result is validated and advisory. Choose one terminal "
            "run_skill, probe, restart_vivado, or save_and_exit call. A mutation must "
            "exactly match one "
            "supported direction; a probe must exactly match one recommended probe. "
            "restart_vivado is operational recovery: it cannot choose a checkpoint, "
            "never replays the failed command, and Python restores the exact immutable "
            "current incumbent. It is neither a mutation nor an evidence probe. "
            "Repeat exactly that item's report and taxonomy refs as action evidence. "
            "Overrides cannot authorize execution. measured_actions_on_current_lineage "
            "is advisory history rather than an execution veto; explain the current "
            "residual or novelty before repeating a listed method. State a failure update. "
            "Leave run_skill params empty unless public_params publishes a key for "
            "that exact mechanic; the target already carries the backend behaviour."
        ),
    ])


def build_system_prompt(kb: KnowledgeBase, cfg: Optional[Config] = None) -> str:
    del kb
    return "\n\n".join([
        build_research_system_prompt(cfg),
        "Legacy JSON stage protocols:",
        STAGE_SCHEMAS["analyze"],
        STAGE_SCHEMAS["decide"],
        STAGE_SCHEMAS["probe"],
        "Return exactly one JSON object.",
    ])


def build_analyze_user(state: dict, case_matches: list[dict]) -> str:
    del case_matches
    return json.dumps(
        {"task": "analyze", "safe_state": _safe_state(state)},
        indent=1,
        sort_keys=True,
    )


def build_decide_user(state: dict, case_matches: list[dict]) -> str:
    del case_matches
    return json.dumps(
        {"task": "decide", "safe_state": _safe_state(state)},
        indent=1,
        sort_keys=True,
    )


def build_probe_followup_user(state: dict, probe_result: dict) -> str:
    return json.dumps(
        {
            "task": "decide",
            "safe_state": _safe_state(state),
            "anonymous_probe_feedback": _safe_probe_feedback(probe_result),
        },
        indent=1,
        sort_keys=True,
    )


REPLAY_SELECTION_SYSTEM = """\
Select exactly one action from the caller-supplied eligible enum using only the
anonymous numeric observation. Do not request other state or tools. Finish with
select_replay_action. Plain text cannot complete this one-turn stage.
"""


def _validated_replay_inputs(
    anonymous_observation: Mapping[str, object],
    eligible_action_ids: Sequence[str],
) -> tuple[dict[str, int | float], tuple[str, ...]]:
    observation = validate_anonymous_observation(anonymous_observation)
    if isinstance(eligible_action_ids, (str, bytes)) or not isinstance(
        eligible_action_ids, Sequence
    ):
        raise TypeError("eligible replay action IDs must be a sequence")
    action_ids = tuple(eligible_action_ids)
    if not action_ids:
        raise ValueError("eligible replay action IDs must not be empty")
    llm_selection_schema(action_ids)
    return observation, action_ids


def build_replay_selection_tool(eligible_action_ids: Sequence[str]) -> dict:
    _observation, action_ids = _validated_replay_inputs({}, eligible_action_ids)
    parameters = llm_selection_schema(action_ids)
    return {
        "type": "function",
        "name": "select_replay_action",
        "description": "Select one exact eligible replay action ID.",
        "parameters": parameters,
        "strict": True,
    }


def build_replay_selection_user(
    anonymous_observation: Mapping[str, object],
    eligible_action_ids: Sequence[str],
) -> str:
    observation, action_ids = _validated_replay_inputs(
        anonymous_observation, eligible_action_ids
    )
    return json.dumps(
        {
            "task": "select_eligible_replay_action",
            "anonymous_observation": observation,
            "eligible_action_ids": list(action_ids),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
