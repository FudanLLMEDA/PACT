"""Strict Report, Knowledge, Decision, and anonymous replay stage runners."""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

from .corpus import CorpusSnapshot, CorpusView, GREP_TOOL, GrepLimits
from .action_menu import select_action_menu_entry, validate_action_menu
from .llm import LLMClient, ToolLoopError
from .prompts import (
    KNOWLEDGE_RESEARCH_SYSTEM,
    KNOWLEDGE_RESEARCH_TOOL,
    RECONSIDER_SYSTEM,
    REPLAY_SELECTION_SYSTEM,
    REPORT_ANALYSIS_SYSTEM,
    REPORT_ANALYSIS_TOOL,
    build_action_tools,
    build_analyze_user,
    build_decide_user,
    build_knowledge_research_user,
    build_probe_followup_user,
    build_reconsider_user,
    build_replay_selection_tool,
    build_replay_selection_user,
    build_report_analysis_user,
    build_system_prompt,
    probe_parameter_fingerprint,
    registered_action_targets_by_skill,
)
from .scoring import normalize_score_estimate
from .regarith_tools.agent_evidence import (
    validate_concurrent_proof_decision_packet,
)

logger = logging.getLogger(__name__)

_SHORT_REF_RE = re.compile(
    r"(?:report|knowledge):[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*:[1-9][0-9]*"
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



def _bounded_label_counts(
    labels: Sequence[str], *, limit: int = 12
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        normalized = str(label or "unknown")[:96]
        counts[normalized] = counts.get(normalized, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    visible = dict(ordered[:limit])
    omitted = sum(count for _label, count in ordered[limit:])
    if omitted:
        visible["__other__"] = omitted
    return visible


def project_action_menu_for_decision(menu: Mapping[str, object]) -> dict:
    """Project the complete audit menu to concise selectable LLM input.

    Blocked entries remain durable in the private menu and reports. Decision
    receives only entries that it can actually select plus anonymous aggregate
    refusal counts, so audit volume cannot drown the eligible mechanisms.
    """
    entries = menu.get("entries")
    if not isinstance(entries, list):
        raise ValueError("authorized action menu entries are unavailable")
    eligible = [entry for entry in entries if entry.get("eligible") is True]
    blocked = [entry for entry in entries if entry.get("eligible") is not True]
    mutation_kinds = {"mutation", "physical_recipe", "proof_gated_mutation"}
    mutations = sorted(
        [entry for entry in eligible if entry.get("kind") in mutation_kinds],
        key=lambda entry: (
            -float((entry.get("display") or {}).get("ranking_score") or 0.0),
            str(entry.get("entry_id") or ""),
        ),
    )
    probes = [entry for entry in eligible if entry.get("kind") == "probe"]
    sentinels = [
        entry for entry in eligible if entry.get("kind") == "save_and_exit"
    ]
    if mutations:
        # The execution-window gate has already proved every mutation in this
        # lane launchable. With immutable-incumbent promotion, stopping here
        # cannot improve final legal Fmax and would only authorize premature
        # surrender while an untried mechanism remains.
        selectable_ids = {
            str(entry["entry_id"]) for entry in mutations
        }
        ranked_selectable = mutations
        lane = "mutation"
    else:
        ranked_probes = sorted(
            probes,
            key=lambda entry: (
                -float((entry.get("display") or {}).get("ranking_score") or 0.0),
                str(entry.get("entry_id") or ""),
            ),
        )[:2]
        selectable_ids = {
            str(entry["entry_id"]) for entry in [*ranked_probes, *sentinels]
        }
        ranked_selectable = [*ranked_probes, *sentinels]
        lane = "probe"
    # Preserve score order in the public projection.  The previous set-based
    # projection restored provider order, so a long menu could bury the best
    # current-seed mechanism even though ``mutations`` had already ranked it.
    selectable = list(ranked_selectable)
    deferred = [
        entry for entry in eligible
        if str(entry["entry_id"]) not in selectable_ids
        and not (mutations and entry.get("kind") == "save_and_exit")
    ]

    blocker_codes = []
    for entry in blocked:
        for raw in entry.get("blockers") or []:
            code = str(raw).split(":", 1)[0].strip().upper()
            blocker_codes.append(
                code
                if re.fullmatch(r"[A-Z][A-Z0-9_]{1,95}", code)
                else "UNTYPED_BLOCKER"
            )
    return {
        "schema_version": menu["schema_version"],
        "seed_candidate_id": menu["seed_candidate_id"],
        "seed_artifact_sha256": menu["seed_artifact_sha256"],
        "top_k": menu["top_k"],
        "decision_lane": lane,
        "eligible_entry_count": len(selectable),
        "private_eligible_entry_count": len(eligible),
        "entries": [
            {
                "entry_id": entry["entry_id"],
                "kind": entry["kind"],
                "eligible": True,
                "proof_status": entry["proof_status"],
                "authorization_class": entry["authorization_class"],
                "display": copy.deepcopy(entry["display"]),
                "blockers": [],
            }
            for entry in selectable
        ],
        "deferred_eligible_summary": {
            "entry_count": len(deferred),
            "by_kind": _bounded_label_counts([
                str(entry.get("kind") or "unknown") for entry in deferred
            ]),
            "reason": (
                "PROBE_LANE_DEFERRED_WHILE_MUTATION_AVAILABLE"
                if mutations else "OUTSIDE_BOUNDED_PROBE_LANE"
            ),
            "individual_deferred_entries_withheld": True,
            "deferred_entries_have_no_current_selection_authority": True,
        },
        "blocked_summary": {
            "entry_count": len(blocked),
            "by_kind": _bounded_label_counts([
                str(entry.get("kind") or "unknown") for entry in blocked
            ]),
            "by_authorization_class": _bounded_label_counts([
                str(entry.get("authorization_class") or "unknown")
                for entry in blocked
            ]),
            "by_blocker_code": _bounded_label_counts(blocker_codes),
            "individual_blocked_entries_withheld": True,
            "blocked_entries_have_no_selection_authority": True,
        },
    }


def validate_public_action_menu(value: object) -> dict:
    """Validate the non-executable Action Card projection used by Decision."""
    if not isinstance(value, Mapping):
        raise ValueError("public action menu must be an object")
    menu = copy.deepcopy(dict(value))
    if set(menu) != {
        "schema_version",
        "seed_candidate_id",
        "seed_artifact_sha256",
        "top_k",
        "decision_lane",
        "eligible_entry_count",
        "private_eligible_entry_count",
        "entries",
        "deferred_eligible_summary",
        "blocked_summary",
    }:
        raise ValueError("public action menu schema mismatch")
    if (
        menu.get("schema_version") != "authorized-action-menu-v2"
        or not isinstance(menu.get("seed_candidate_id"), str)
        or not menu["seed_candidate_id"]
        or re.fullmatch(r"[0-9a-f]{64}", str(menu.get("seed_artifact_sha256") or ""))
        is None
        or isinstance(menu.get("top_k"), bool)
        or not isinstance(menu.get("top_k"), int)
        or not 1 <= menu["top_k"] <= 16
        or menu.get("decision_lane") not in {"mutation", "probe"}
        or not isinstance(menu.get("entries"), list)
    ):
        raise ValueError("public action menu identity is invalid")
    expected_entry_fields = {
        "entry_id",
        "kind",
        "eligible",
        "proof_status",
        "authorization_class",
        "display",
        "blockers",
    }
    allowed_kinds = (
        {"mutation", "physical_recipe", "proof_gated_mutation"}
        if menu["decision_lane"] == "mutation"
        else {"probe", "save_and_exit"}
    )
    entry_ids: list[str] = []
    for entry in menu["entries"]:
        if (
            not isinstance(entry, Mapping)
            or set(entry) != expected_entry_fields
            or re.fullmatch(r"[0-9a-f]{64}", str(entry.get("entry_id") or ""))
            is None
            or entry.get("kind") not in allowed_kinds
            or entry.get("eligible") is not True
            or not isinstance(entry.get("proof_status"), str)
            or not isinstance(entry.get("authorization_class"), str)
            or not isinstance(entry.get("display"), Mapping)
            or entry.get("blockers") != []
        ):
            raise ValueError("public action menu entry is invalid")
        entry_ids.append(str(entry["entry_id"]))
    if not entry_ids or len(entry_ids) != len(set(entry_ids)):
        raise ValueError("public action menu entry IDs are invalid")
    for count_field in ("eligible_entry_count", "private_eligible_entry_count"):
        if (
            isinstance(menu.get(count_field), bool)
            or not isinstance(menu.get(count_field), int)
            or menu[count_field] < 0
        ):
            raise ValueError("public action menu counts are invalid")
    if (
        menu["eligible_entry_count"] != len(entry_ids)
        or menu["private_eligible_entry_count"] < len(entry_ids)
    ):
        raise ValueError("public action menu counts do not match entries")
    # Canonical serialization rejects non-JSON values and NaN before an LLM call.
    json.dumps(menu, allow_nan=False, ensure_ascii=True, sort_keys=True)
    return menu


_REGISTERED_OPERATOR_DIRECTION = (
    "operator_rewrite",
    "dsp_srl_recurrence_cut_v1",
)
# Each executable operator template cites its own anonymous preflight count.
OPERATOR_PREFLIGHT_COUNT_FIELDS = {
    "dsp_srl_recurrence_cut_v1": "recurrence_boundary_hypothesis_count",
    "fixed_point_dsp_rebuild_v1": "fixed_point_product_hypothesis_count",
    "dsp_mul_two_operand_v1": "two_operand_product_hypothesis_count",
    "dsp_register_absorb_v1": "register_absorb_hypothesis_count",
    "dsp_pipeline_rebalance_v1": "pipeline_rebalance_hypothesis_count",
    "registered_arithmetic_cone_v1": (
        "registered_arithmetic_eligible_family_count"
    ),
    "constructive_accumulator_rebuild_v1": (
        "constructive_accumulator_eligible_family_count"
    ),
    "memory_write_buffer_forward_v1": (
        "memory_write_buffer_forward_eligible_family_count"
    ),
    "exact_control_to_data_state_equation_v1": (
        "control_to_data_state_equation_hypothesis_count"
    ),
}



def registered_operator_directions() -> tuple[tuple[str, str], ...]:
    """Exact (skill, target) pairs derived from the executable registry."""
    from .skills.operator_rewrite import OPERATOR_REWRITE_REGISTRY

    return tuple(
        ("operator_rewrite", target)
        for target, spec in OPERATOR_REWRITE_REGISTRY.items()
        if spec.execution_status == "executable"
        and target in OPERATOR_PREFLIGHT_COUNT_FIELDS
    )






@dataclass
class AnalyzeResult:
    bottleneck: str = "mixed"
    diagnosis: str = ""
    strategy: list = field(default_factory=list)
    case_verdict: str = ""
    stop_hint: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ReportAnalysisResult:
    bottleneck: str = "unknown"
    diagnosis: str = ""
    bottleneck_evidence_refs: list[str] = field(default_factory=list)
    diagnosis_evidence_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class KnowledgeResearchResult:
    classifications: list[dict] = field(default_factory=list)
    residual_update: str = ""
    report_evidence_refs: list[str] = field(default_factory=list)
    knowledge_evidence_refs: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class StagedResearchResult:
    """Validated Report + Knowledge output with no mutation authority."""

    state: dict
    report_snapshot: CorpusSnapshot
    knowledge_snapshot: CorpusSnapshot
    report_result: ReportAnalysisResult
    knowledge_result: KnowledgeResearchResult
    report_cached: bool = False
    report_transcript: list[dict] = field(default_factory=list)
    knowledge_transcript: list[dict] = field(default_factory=list)
    stage_metrics: dict[str, dict] = field(default_factory=dict)
    before_cost_usd: float = 0.0


@dataclass
class Decision:
    action: str = "run_skill"
    skill: str = ""
    target: str = "auto"
    params: dict = field(default_factory=dict)
    expectation: str = ""
    score_estimate: dict = field(default_factory=dict)
    reflection: str = ""
    stop_reason: str = ""
    stop_rationale: str = ""
    seed_candidate_id: Optional[str] = None
    server: str = "vivado"
    tool: str = ""
    arguments: dict = field(default_factory=dict)
    question: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    report_analysis: dict = field(default_factory=dict)
    knowledge_research: dict = field(default_factory=dict)
    knowledge_comparison: str = ""
    override_knowledge_direction: bool = False
    failure_update: str = ""
    contrastive_decision: dict = field(default_factory=dict)
    operator_judgment: dict = field(default_factory=dict)
    react_transcript: dict = field(default_factory=dict)
    report_snapshot_id: str = ""
    knowledge_snapshot_id: str = ""
    selected_entry_id: str = ""
    action_menu_digest: str = ""
    action_menu_seed_sha256: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ActionReview:
    action: str = "replan"
    assessment: str = ""
    reason: str = ""
    raw: dict = field(default_factory=dict)


class StageRunner:
    @staticmethod
    def _stage_metrics(out: object) -> dict:
        if not isinstance(out, dict):
            out = {}
        usage = out.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return {
            "turns": int(out.get("turns", 0) or 0),
            "elapsed_s": float(out.get("elapsed_s", 0.0) or 0.0),
            "cost_usd": float(out.get("cost_usd", 0.0) or 0.0),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "cached_tokens": int(usage.get("cached_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
        }

    @staticmethod
    def _contains_raw_tcl(node: object) -> bool:
        if isinstance(node, dict):
            return any(
                str(key).lower() in {"command", "tcl", "script", "tcl_ref"}
                or StageRunner._contains_raw_tcl(value)
                for key, value in node.items()
            )
        if isinstance(node, list):
            return any(StageRunner._contains_raw_tcl(item) for item in node)
        if isinstance(node, str):
            lowered = node.lower()
            return any(
                token in lowered
                for token in (
                    "route_design",
                    "place_design",
                    "phys_opt_design",
                    "set_property",
                    "get_cells",
                    "get_nets",
                    "get_sites",
                    "open_checkpoint",
                    "write_checkpoint",
                )
            )
        return False

    def __init__(self, llm: LLMClient, kb, cfg=None, *, cold_isolation: bool = False):
        self.llm = llm
        self.kb = kb
        self.cfg = cfg
        self.cold_isolation = bool(cold_isolation)
        self.uses_staged_react = bool(
            cfg.get("react.enabled", True) if cfg is not None else True
        )
        self.system_prompt = (
            "" if self.uses_staged_react else build_system_prompt(kb, cfg)
        )
        self._action_targets_by_skill = registered_action_targets_by_skill(
            cold_isolation=self.cold_isolation
        )
        self.action_tools = build_action_tools(self._action_targets_by_skill)
        self.knowledge_research_tool = copy.deepcopy(KNOWLEDGE_RESEARCH_TOOL)
        self.last_cost_usd = 0.0
        self._report_cache: dict[str, ReportAnalysisResult] = {}
        self._knowledge_retrieval_cache: dict[str, dict] = {}

    def _named_grep_tool(self, name: str, scope: str) -> dict:
        """Bind the grep schema to the limits the corpus actually enforces.

        Leaving the ceilings out of the schema made the model guess them, and
        every guess above the limit burned a whole tool turn on a rejection.
        """
        tool = copy.deepcopy(GREP_TOOL)
        tool["name"] = name
        tool["description"] = (
            f"Search only the immutable {scope} documents listed for this stage."
        )
        if self.cfg is not None:
            limits = GrepLimits.from_config(
                self.cfg, "report" if scope == "report" else "research"
            )
            properties = tool["parameters"]["properties"]
            properties["max_matches"]["maximum"] = limits.max_matches
            properties["context"]["maximum"] = limits.max_context_lines
            properties["files"]["maxItems"] = limits.max_files
            properties["pattern"]["minLength"] = 1
            properties["pattern"]["maxLength"] = limits.max_pattern_chars
        return tool

    @staticmethod
    def _scope_grep_arguments(
        arguments: dict,
        *,
        scope: str,
        snapshot: CorpusSnapshot,
    ) -> dict:
        scoped = dict(arguments)
        files = scoped.get("files")
        known = {str(item["name"]) for item in snapshot.documents}
        if files is None:
            scoped["files"] = [f"{scope}/{name}" for name in sorted(known)]
            return scoped
        if not isinstance(files, list):
            raise ValueError("grep files must be a list")
        canonical = []
        for item in files:
            if not isinstance(item, str):
                raise ValueError("grep document names must be strings")
            value = item.replace("\\", "/")
            local = value[len(scope) + 1 :] if value.startswith(f"{scope}/") else value
            if local not in known:
                raise ValueError(f"{scope} grep cannot access document: {value}")
            canonical.append(f"{scope}/{local}")
        scoped["files"] = canonical
        return scoped

    @staticmethod
    def _evidence_refs(transcript: list[dict]) -> set[str]:
        refs: set[str] = set()
        for entry in transcript:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            matches = result.get("matches")
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                ref = match.get("evidence_ref")
                if isinstance(ref, str):
                    refs.add(ref)
                for excerpt in match.get("excerpt") or []:
                    if isinstance(excerpt, dict) and isinstance(
                        excerpt.get("evidence_ref"), str
                    ):
                        refs.add(excerpt["evidence_ref"])
        return refs

    @staticmethod
    def _inline_evidence_refs(node: object) -> set[str]:
        refs: set[str] = set()
        if isinstance(node, dict):
            for value in node.values():
                refs.update(StageRunner._inline_evidence_refs(value))
        elif isinstance(node, list):
            for value in node:
                refs.update(StageRunner._inline_evidence_refs(value))
        elif isinstance(node, str):
            refs.update(_SHORT_REF_RE.findall(node))
        return refs

    @staticmethod
    def _structured_citation_lists(
        node: object, path: str = "$"
    ) -> list[tuple[str, object]]:
        result = []
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}"
                if str(key).endswith("evidence_refs"):
                    result.append((child_path, value))
                else:
                    result.extend(
                        StageRunner._structured_citation_lists(value, child_path)
                    )
        elif isinstance(node, list):
            for index, value in enumerate(node):
                result.extend(
                    StageRunner._structured_citation_lists(
                        value, f"{path}[{index}]"
                    )
                )
        return result

    @staticmethod
    def _structured_evidence_refs(node: object) -> set[str]:
        refs = set()
        for _path, value in StageRunner._structured_citation_lists(node):
            if isinstance(value, list):
                refs.update(item for item in value if isinstance(item, str))
        return refs

    @staticmethod
    def _exact_fields(
        value: object,
        *,
        required: set[str],
        optional: set[str] = frozenset(),
        label: str,
    ) -> Optional[str]:
        if not isinstance(value, dict):
            return f"{label} must be an object"
        missing = required - set(value)
        unknown = set(value) - required - optional
        if missing:
            return f"{label} missing fields: {sorted(missing)}"
        if unknown:
            return f"{label} has unknown fields: {sorted(unknown)}"
        return None

    @staticmethod
    def _citation_list_error(value: object, label: str) -> Optional[str]:
        if not isinstance(value, list):
            return f"{label} must be a list"
        if any(not isinstance(item, str) for item in value):
            return f"{label} must contain only strings"
        return None

    @classmethod
    def _validate_score_estimate(cls, value: object) -> Optional[str]:
        error = cls._exact_fields(
            value,
            required={
                "version",
                "gain_vs_global_best_mhz",
                "action_cycle_runtime_s",
                "action_risk",
                "basis",
            },
            label="score_estimate",
        )
        if error:
            return error
        assert isinstance(value, dict)
        if type(value["version"]) is not int or value["version"] != 2:
            return "score_estimate.version must be exactly 2"
        for key in ("gain_vs_global_best_mhz", "action_cycle_runtime_s"):
            quantiles = value[key]
            error = cls._exact_fields(
                quantiles,
                required={"p10", "p50", "p90"},
                label=f"score_estimate.{key}",
            )
            if error:
                return error
            assert isinstance(quantiles, dict)
            if any(
                isinstance(quantiles[name], bool)
                or not isinstance(quantiles[name], (int, float))
                for name in ("p10", "p50", "p90")
            ):
                return f"score_estimate.{key} quantiles must be numbers"
        risk = value["action_risk"]
        error = cls._exact_fields(
            risk,
            required={
                "candidate_regression_probability",
                "no_legal_candidate_probability",
                "timeout_probability",
            },
            label="score_estimate.action_risk",
        )
        if error:
            return error
        assert isinstance(risk, dict)
        if any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in risk.values()
        ):
            return "score_estimate.action_risk values must be numbers"
        if not isinstance(value["basis"], str) or not value["basis"].strip():
            return "score_estimate.basis must be nonempty"
        return None

    @classmethod
    def _validate_terminal_payload(cls, name: str, parsed: dict) -> Optional[str]:
        if name == "complete_report_analysis":
            error = cls._exact_fields(
                parsed,
                required={
                    "bottleneck",
                    "diagnosis",
                    "bottleneck_evidence_refs",
                    "diagnosis_evidence_refs",
                    "unknowns",
                },
                label="report result",
            )
            if error:
                return error
            if parsed.get("bottleneck") not in _BOTTLENECKS:
                return "report bottleneck is invalid"
            if not isinstance(parsed.get("diagnosis"), str) or not parsed["diagnosis"].strip():
                return "report diagnosis must be nonempty"
            for key in (
                "bottleneck_evidence_refs",
                "diagnosis_evidence_refs",
            ):
                error = cls._citation_list_error(
                    parsed.get(key), f"report {key}"
                )
                if error:
                    return error
                if not parsed[key]:
                    return f"report {key} must be nonempty"
            if not isinstance(parsed.get("unknowns"), list) or any(
                not isinstance(item, str) for item in parsed.get("unknowns", [])
            ):
                return "report unknowns must contain only strings"
            return None

        if name == "complete_knowledge_research":
            error = cls._exact_fields(
                parsed,
                required={
                    "classifications",
                    "residual_update",
                    "report_evidence_refs",
                    "knowledge_evidence_refs",
                },
                label="Knowledge result",
            )
            if error:
                return error
            if cls._contains_raw_tcl(parsed):
                return "Knowledge result cannot contain Tcl or scripts"
            if not isinstance(parsed.get("residual_update"), str):
                return "Knowledge residual_update must be a string"
            for key in ("report_evidence_refs", "knowledge_evidence_refs"):
                error = cls._citation_list_error(parsed.get(key), f"Knowledge {key}")
                if error:
                    return error
            classifications = parsed.get("classifications")
            if not isinstance(classifications, list) or len(classifications) > 3:
                return "Knowledge classifications must be a list of at most three items"
            classification_fields = {
                "level",
                "pattern",
                "confidence",
                "live_evidence_refs",
                "knowledge_evidence_refs",
                "applicability",
                "contraindications",
                "missing_evidence",
            }
            for index, item in enumerate(classifications):
                label = f"Knowledge classifications[{index}]"
                error = cls._exact_fields(
                    item, required=classification_fields, label=label
                )
                if error:
                    return error
                assert isinstance(item, dict)
                if item.get("level") not in {"L0", "L1", "L2", "L3", "L4", "L5", "unknown"}:
                    return f"{label}.level is invalid"
                if item.get("confidence") not in {"low", "medium", "high"}:
                    return f"{label}.confidence is invalid"
                for key in ("pattern", "applicability"):
                    if not isinstance(item.get(key), str):
                        return f"{label}.{key} must be a string"
                for key in ("live_evidence_refs", "knowledge_evidence_refs"):
                    error = cls._citation_list_error(item.get(key), f"{label}.{key}")
                    if error:
                        return error
                    if not item.get(key):
                        return f"{label}.{key} must be nonempty"
                for key in ("contraindications", "missing_evidence"):
                    if not isinstance(item.get(key), list) or any(
                        not isinstance(entry, str) for entry in item.get(key, [])
                    ):
                        return f"{label}.{key} must contain only strings"
            return None

        action_schemas = {
            "run_skill": (
                {
                    "skill",
                    "target",
                    "params",
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "score_estimate",
                    "evidence_refs",
                },
                {"reflection"},
            ),
            "probe": (
                {
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
                },
                {"reflection"},
            ),
            "restart_vivado": (
                {
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "evidence_refs",
                },
                {"reflection"},
            ),
            "save_and_exit": (
                {
                    "stop_reason",
                    "stop_rationale",
                    "expectation",
                    "knowledge_comparison",
                    "override_knowledge_direction",
                    "failure_update",
                    "evidence_refs",
                    "direction_status",
                },
                {"reflection"},
            ),
        }
        schema = action_schemas.get(name)
        if schema is None:
            return f"unknown terminal tool {name!r}"
        error = cls._exact_fields(
            parsed,
            required=schema[0],
            optional=schema[1],
            label=f"{name} result",
        )
        if error:
            return error
        error = cls._citation_list_error(
            parsed.get("evidence_refs"), f"{name} evidence_refs"
        )
        if error:
            return error
        if not isinstance(parsed.get("override_knowledge_direction"), bool):
            return "override_knowledge_direction must be a boolean"
        for key in ("expectation", "knowledge_comparison", "failure_update"):
            if not isinstance(parsed.get(key), str) or not parsed[key].strip():
                return f"{key} must be nonempty"
        if "reflection" in parsed and not isinstance(parsed["reflection"], str):
            return "reflection must be a string"
        if name == "run_skill":
            if not isinstance(parsed.get("skill"), str) or not parsed["skill"].strip():
                return "skill must be nonempty"
            if not isinstance(parsed.get("target"), str) or not parsed["target"].strip():
                return "target must be nonempty"
            if not isinstance(parsed.get("params"), dict):
                return "params must be an object"
            if cls._contains_raw_tcl(parsed["params"]):
                return "typed LLM actions cannot provide raw Tcl recipe text"
            return cls._validate_score_estimate(parsed.get("score_estimate"))
        if name == "probe":
            if parsed.get("server") not in {"vivado", "rapidwright"}:
                return "probe server is invalid"
            if not isinstance(parsed.get("tool"), str) or not parsed["tool"].strip():
                return "probe tool must be nonempty"
            if not isinstance(parsed.get("arguments"), dict):
                return "probe arguments must be an object"
            if not isinstance(parsed.get("question"), str) or not parsed["question"].strip():
                return "probe question must be nonempty"
            fingerprint = parsed.get("parameter_fingerprint")
            if (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                or fingerprint
                != probe_parameter_fingerprint(
                    parsed.get("server"),
                    parsed.get("tool"),
                    parsed.get("arguments"),
                    parsed.get("question"),
                )
            ):
                return "probe parameter_fingerprint is invalid"
            return None
        if name == "restart_vivado":
            return None
        if name == "save_and_exit":
            if not isinstance(parsed.get("stop_reason"), str) or not parsed["stop_reason"].strip():
                return "stop_reason must be nonempty"
            if not isinstance(parsed.get("stop_rationale"), str) or not parsed["stop_rationale"].strip():
                return "stop_rationale must be nonempty"
            rows = parsed.get("direction_status")
            if not isinstance(rows, list):
                return "direction_status must be a list"
            required_fields = {
                "skill", "target", "status", "current_seed_eligible",
                "authorization_state", "evidence_complete", "nomination_blockers",
            }
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != required_fields:
                    return f"direction_status[{index}] has invalid fields"
                if row.get("status") not in {"eligible", "exhausted", "ineligible"}:
                    return f"direction_status[{index}].status is invalid"
                if not isinstance(row.get("current_seed_eligible"), bool):
                    return f"direction_status[{index}].current_seed_eligible is invalid"
                if row.get("authorization_state") not in {
                    "live_authorized_executable_now", "hypothesis_only", "blocked",
                }:
                    return f"direction_status[{index}].authorization_state is invalid"
                if not isinstance(row.get("evidence_complete"), bool):
                    return f"direction_status[{index}].evidence_complete is invalid"
                blockers = row.get("nomination_blockers")
                if not isinstance(blockers, list) or any(
                    not isinstance(item, str) for item in blockers
                ):
                    return f"direction_status[{index}].nomination_blockers is invalid"
        return None

    @staticmethod
    def _citation_resolution_error(
        payload: dict,
        observed: set[str],
        report_snapshot: CorpusSnapshot,
        knowledge_snapshot: CorpusSnapshot,
    ) -> Optional[str]:
        for path, value in StageRunner._structured_citation_lists(payload):
            error = StageRunner._citation_list_error(value, path)
            if error:
                return error
            assert isinstance(value, list)
            for ref in value:
                if ref not in observed:
                    return f"{path} cited unobserved evidence {ref!r}"
                if ref.startswith("report:"):
                    if not report_snapshot.resolves_evidence_ref(ref):
                        return f"{path} does not bind to the active report snapshot"
                elif ref.startswith("knowledge:"):
                    if not knowledge_snapshot.resolves_evidence_ref(ref):
                        return f"{path} does not bind to the active taxonomy snapshot"
                else:
                    return f"{path} contains an invalid evidence scope"
        for ref in StageRunner._inline_evidence_refs(payload):
            if ref not in observed:
                return f"prose cited unobserved evidence {ref!r}"
            owner = report_snapshot if ref.startswith("report:") else knowledge_snapshot
            if not owner.resolves_evidence_ref(ref):
                return f"prose citation does not bind to the active snapshot: {ref!r}"
        return None

    @staticmethod
    def _claim_local_knowledge_unions(parsed: dict) -> tuple[list[str], list[str]]:
        """Return canonical aggregate refs from classification-local claims."""
        report_refs: list[str] = []
        knowledge_refs: list[str] = []
        for item in parsed.get("classifications", []):
            report_refs.extend(item.get("live_evidence_refs", []))
            knowledge_refs.extend(item.get("knowledge_evidence_refs", []))
        return (
            list(dict.fromkeys(report_refs)),
            list(dict.fromkeys(knowledge_refs)),
        )

    @staticmethod
    def _claim_local_knowledge_error(parsed: dict) -> Optional[str]:
        local_report_refs = []
        local_knowledge_refs = []
        for index, item in enumerate(parsed["classifications"]):
            live = item["live_evidence_refs"]
            taxonomy = item["knowledge_evidence_refs"]
            if any(not ref.startswith("report:") for ref in live):
                return f"classification {index} has a non-report live ref"
            if any(not ref.startswith("knowledge:") for ref in taxonomy):
                return f"classification {index} has a non-taxonomy knowledge ref"
            if not live or not taxonomy:
                return (
                    f"classification {index} requires its own report "
                    "and taxonomy refs"
                )
            if item["level"] == "unknown" and not item["missing_evidence"]:
                return f"unknown classification {index} requires missing_evidence"
            local_report_refs.extend(live)
            local_knowledge_refs.extend(taxonomy)
        report_refs = parsed["report_evidence_refs"]
        knowledge_refs = parsed["knowledge_evidence_refs"]
        if len(report_refs) != len(set(report_refs)):
            return "Knowledge report_evidence_refs must be deduplicated"
        if set(report_refs) != set(local_report_refs):
            return "Knowledge report_evidence_refs must equal classification-local report refs"
        if len(knowledge_refs) != len(set(knowledge_refs)):
            return "Knowledge knowledge_evidence_refs must be deduplicated"
        if set(knowledge_refs) != set(local_knowledge_refs):
            return (
                "Knowledge knowledge_evidence_refs must equal classification-local taxonomy refs"
            )
        return None




    @staticmethod
    def _verify_snapshot(snapshot: CorpusSnapshot) -> None:
        for item in snapshot.documents:
            snapshot.document_path(str(item["name"]))

    @staticmethod
    def _invalid_staged_decision(
        error: str,
        report_snapshot: CorpusSnapshot,
        knowledge_snapshot: CorpusSnapshot,
        *,
        report_analysis: Optional[dict] = None,
        knowledge_research: Optional[dict] = None,
        report_transcript: Optional[list[dict]] = None,
        knowledge_transcript: Optional[list[dict]] = None,
        decision_transcript: Optional[list[dict]] = None,
        stage_metrics: Optional[dict] = None,
        report_cached: bool = False,
        error_class: str = "reasoning",
    ) -> Decision:
        decision = Decision(
            action="invalid",
            raw={"error": str(error), "error_class": str(error_class)},
        )
        decision.report_snapshot_id = report_snapshot.snapshot_id
        decision.knowledge_snapshot_id = knowledge_snapshot.snapshot_id
        decision.report_analysis = dict(report_analysis or {})
        decision.knowledge_research = dict(knowledge_research or {})
        decision.react_transcript = {
            "report_cached": bool(report_cached),
            "report": list(report_transcript or []),
            "knowledge": list(knowledge_transcript or []),
            "decision": list(decision_transcript or []),
            "stage_metrics": copy.deepcopy(stage_metrics or {}),
            "status": "invalid",
            "error": str(error),
            "error_class": str(error_class),
        }
        return decision

    def _skill_target_compatibility_error(
        self, skill: object, target: object
    ) -> Optional[str]:
        """Reject a target that belongs to a different registered mechanic.

        The public target enum is a flat union, so a cross-mechanic pair is
        representable. Detecting it here lets the terminal repair turns fix it,
        instead of losing a whole reasoning cycle to a capability-layer veto.
        """
        allowed = self._action_targets_by_skill.get(str(skill))
        if allowed is not None and str(target) in allowed:
            return None
        if allowed is None:
            return f"mechanic {skill!r} has no public target for this incumbent"
        return (
            f"mechanic {skill!r} does not implement target {target!r}; "
            f"its registered targets are {allowed}"
        )

    @staticmethod
    def _public_param_error(skill: object, params: object) -> Optional[str]:
        """Reject a public parameter the mechanic's ABI does not accept.

        The capability layer already refuses these, but only after the action
        has consumed a reasoning cycle. Rejecting here lets the terminal repair
        turns drop the key immediately.
        """
        from .prompts import registered_action_public_params

        allowed = registered_action_public_params().get(str(skill))
        if allowed is None or not isinstance(params, dict):
            return None
        unknown = sorted(str(key) for key in params if str(key) not in allowed)
        if not unknown:
            return None
        return (
            f"mechanic {skill!r} rejects public parameters {unknown}; "
            f"it accepts only {allowed} and takes its behaviour from the target"
        )

    def _registered_action_error(self, terminal: str, parsed: dict) -> Optional[str]:
        if terminal == "run_skill":
            if parsed.get("skill") not in self._action_targets_by_skill:
                return "run_skill selected an unregistered mechanic"
            target_schema = self.action_tools[0]["parameters"]["properties"][
                "target"
            ]
            if parsed.get("target") not in target_schema.get("enum", []):
                return "run_skill selected a non-public target mechanic"
            error = self._skill_target_compatibility_error(
                parsed.get("skill"), parsed.get("target")
            )
            if error:
                return error
            error = self._public_param_error(
                parsed.get("skill"), parsed.get("params")
            )
            if error:
                return error
        if terminal == "probe":
            allowed = (
                self.cfg.get("legality.allowed_custom_tools", {})
                if self.cfg is not None
                else {}
            ) or {}
            names = allowed.get(parsed.get("server")) or []
            if parsed.get("tool") not in names:
                return "probe selected an unregistered read-only mechanic"
            probe_schema = self.action_tools[1]["parameters"]["properties"][
                "tool"
            ]
            if parsed.get("tool") not in probe_schema.get("enum", []):
                return "probe selected a non-public read-only mechanic"
            if parsed.get("arguments") != {}:
                return "registered read-only probes require exact empty arguments"
        return None




    @staticmethod
    def _exhausted_probe_error(
        terminal: str, parsed: dict, state: dict
    ) -> Optional[str]:
        """Refuse a read-only observation that already ran on this artifact.

        Registered probes take no arguments and do not mutate the design, so
        repeating one on an unchanged incumbent returns identical data.  Paying
        a full reasoning cycle for it is pure loss, and the loop otherwise
        repeats the same observation until the cost budget is gone.
        """
        if terminal != "probe":
            return None
        entries = state.get("exhausted_probes")
        if not isinstance(entries, list):
            return None
        server = parsed.get("server")
        tool = parsed.get("tool")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("server") == server and entry.get("tool") == tool:
                return (
                    f"probe {server}:{tool} already ran on this unchanged "
                    "incumbent and returns identical data, so it cannot yield "
                    "new evidence. Select a read-only mechanic that is not "
                    "listed in safe_state.exhausted_probes, or select a "
                    "mutating mechanic, or stop."
                )
        return None

    @staticmethod
    def _exhausted_action_error(
        terminal: str, parsed: dict, state: dict
    ) -> Optional[str]:
        """Do not resubmit an exact action already disposed on this artifact."""
        if terminal != "run_skill":
            return None
        entries = state.get("exhausted_actions")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("skill") == parsed.get("skill")
                and entry.get("target") == parsed.get("target")
            ):
                return (
                    "run_skill repeats an action already measured or declined by "
                    "neutral review on this unchanged incumbent; select another "
                    "supported mechanic"
                )
        return None

    @staticmethod
    def _direction_stop_error(parsed: dict, visibility_packet: dict) -> Optional[str]:
        """Bind stop claims to the exact current-seed direction inventory."""
        if parsed.get("direction_status") != visibility_packet.get(
            "direction_status"
        ):
            return (
                "save_and_exit direction_status must exactly copy the "
                "current tool-owned visibility packet"
            )
        if (
            parsed.get("stop_reason") == "no_supported_mutation"
            and int(visibility_packet.get("remaining_eligible_direction_count", 0))
            > 0
        ):
            return (
                "no_supported_mutation is unavailable while an unexhausted "
                "eligible registered direction remains"
            )
        return None




    def staged_research(
        self,
        state: dict,
        report_snapshot: CorpusSnapshot,
        knowledge_snapshot: CorpusSnapshot,
        deadline_monotonic: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
    ) -> StagedResearchResult | Decision:
        """Run citation-bound Report and Knowledge stages without choosing an action."""
        state = copy.deepcopy(state) if isinstance(state, dict) else {}
        concurrent_packet = (
            state.get("concurrent_proof_decision_packet")
            if isinstance(state, dict) else None
        )
        if (
            concurrent_packet is not None
            and validate_concurrent_proof_decision_packet(concurrent_packet) is None
        ):
            return Decision(
                action="invalid",
                raw={"error": "concurrent proof decision packet is malformed"},
            )
        self.knowledge_research_tool = copy.deepcopy(KNOWLEDGE_RESEARCH_TOOL)
        if report_snapshot.scope != "report" or knowledge_snapshot.scope != "knowledge":
            return Decision(action="invalid", raw={"error": "invalid corpus scopes"})
        expected_knowledge_documents = (
            self.kb.llm_corpus_documents()
            if hasattr(self.kb, "llm_corpus_documents")
            else {}
        )
        if {str(item["name"]) for item in knowledge_snapshot.documents} != set(
            expected_knowledge_documents
        ) or set(expected_knowledge_documents) != {"operator_taxonomy.yaml"}:
            return Decision(
                action="invalid",
                raw={"error": "Knowledge stage requires the taxonomy-only corpus"},
            )
        try:
            if any(
                knowledge_snapshot.document_path(name).read_text(encoding="utf-8")
                != text
                for name, text in expected_knowledge_documents.items()
            ):
                return Decision(
                    action="invalid",
                    raw={"error": "Knowledge stage taxonomy content is not approved"},
                )
        except Exception as exc:  # noqa: BLE001 - public boundary fails closed
            return Decision(
                action="invalid",
                raw={"error": f"Knowledge stage taxonomy verification failed: {exc}"},
            )
        before_cost = float(getattr(self.llm, "total_cost", 0.0))
        self.last_cost_usd = 0.0
        report_transcript: list[dict] = []
        knowledge_transcript: list[dict] = []
        decision_transcript: list[dict] = []
        stage_metrics: dict[str, dict] = {}
        report_result: Optional[ReportAnalysisResult] = None
        knowledge_result: Optional[KnowledgeResearchResult] = None
        report_audit: dict = {}
        knowledge_audit: dict = {}
        report_cached = False

        def finish_invalid(error: str, *, error_class: str = "reasoning") -> Decision:
            self.last_cost_usd = max(
                0.0,
                float(getattr(self.llm, "total_cost", before_cost)) - before_cost,
            )
            return self._invalid_staged_decision(
                error,
                report_snapshot,
                knowledge_snapshot,
                report_analysis=(
                    report_result.raw if report_result else report_audit
                ),
                knowledge_research=(
                    knowledge_result.raw if knowledge_result else knowledge_audit
                ),
                report_transcript=report_transcript,
                knowledge_transcript=knowledge_transcript,
                decision_transcript=decision_transcript,
                stage_metrics=stage_metrics,
                report_cached=report_cached,
                error_class=error_class,
            )

        try:
            self._verify_snapshot(report_snapshot)
            self._verify_snapshot(knowledge_snapshot)
        except Exception as exc:  # noqa: BLE001 - immutable boundary fails closed
            return finish_invalid(f"corpus verification failed: {exc}")

        report_result = self._report_cache.get(report_snapshot.snapshot_id)
        report_cached = report_result is not None
        if report_result is None:
            report_view = CorpusView(
                [report_snapshot], GrepLimits.from_config(self.cfg, "report")
            )
            report_tool = self._named_grep_tool("grep_reports", "report")

            def report_handler(name: str, arguments: dict) -> dict:
                if name != "grep_reports":
                    raise ValueError(f"tool is unavailable in Report stage: {name}")
                return report_view.grep(
                    self._scope_grep_arguments(
                        arguments, scope="report", snapshot=report_snapshot
                    )
                )

            def validate_report_terminal(
                name: str, parsed: dict, transcript: list[dict]
            ) -> Optional[str]:
                error = self._validate_terminal_payload(name, parsed)
                if error:
                    return error
                observed = self._evidence_refs(transcript)
                error = self._citation_resolution_error(
                    parsed, observed, report_snapshot, knowledge_snapshot
                )
                if error:
                    return error
                refs = list(parsed["bottleneck_evidence_refs"])
                refs.extend(parsed["diagnosis_evidence_refs"])
                if any(not ref.startswith("report:") for ref in refs):
                    return "Report claims may cite only report evidence"
                return None

            try:
                report_out = self.llm.tool_loop_call(
                    "report_analysis",
                    REPORT_ANALYSIS_SYSTEM,
                    build_report_analysis_user(
                        state, report_snapshot.manifest_for_llm()
                    ),
                    tools=[report_tool, REPORT_ANALYSIS_TOOL],
                    handler=report_handler,
                    terminal_tools={"complete_report_analysis"},
                    required_tool_calls={"grep_reports": 1},
                    deadline_monotonic=deadline_monotonic,
                    max_cost_usd=max_cost_usd,
                    terminal_validator=validate_report_terminal,
                )
            except ToolLoopError as exc:
                report_transcript = list(exc.transcript)
                stage_metrics["report"] = {"cached": False, **exc.metrics}
                return finish_invalid(
                    str(exc),
                    error_class=(
                        "provider_failure" if exc.provider_failure else "reasoning"
                    ),
                )
            report_transcript = list(report_out.get("transcript") or [])
            stage_metrics["report"] = {
                "cached": False,
                **self._stage_metrics(report_out),
            }
            parsed = report_out.get("parsed")
            if not isinstance(parsed, dict):
                return finish_invalid("Report terminal payload must be an object")
            report_audit = copy.deepcopy(parsed)
            terminal_error = self._validate_terminal_payload(
                str(report_out.get("terminal_tool") or ""), parsed
            )
            if terminal_error:
                return finish_invalid(terminal_error)
            observed = self._evidence_refs(report_transcript)
            citation_error = self._citation_resolution_error(
                parsed, observed, report_snapshot, knowledge_snapshot
            )
            if citation_error:
                return finish_invalid(citation_error)
            bottleneck_refs = list(parsed["bottleneck_evidence_refs"])
            diagnosis_refs = list(parsed["diagnosis_evidence_refs"])
            if any(
                not ref.startswith("report:")
                for ref in bottleneck_refs + diagnosis_refs
            ):
                return finish_invalid("Report claims may cite only report evidence")
            evidence_refs = list(dict.fromkeys(bottleneck_refs + diagnosis_refs))
            report_result = ReportAnalysisResult(
                bottleneck=parsed["bottleneck"],
                diagnosis=parsed["diagnosis"],
                bottleneck_evidence_refs=bottleneck_refs,
                diagnosis_evidence_refs=diagnosis_refs,
                evidence_refs=evidence_refs,
                unknowns=list(parsed["unknowns"]),
                raw=copy.deepcopy(parsed),
                transcript=copy.deepcopy(report_transcript),
                metrics=self._stage_metrics(report_out),
            )
            self._report_cache[report_snapshot.snapshot_id] = report_result
        else:
            stage_metrics["report"] = {
                "cached": True,
                **copy.deepcopy(report_result.metrics),
            }

        assert report_result is not None
        if not bool(self.cfg.get("react.knowledge_stage_enabled", False)):
            # The Knowledge stage classified the Report diagnosis against a
            # generic taxonomy. Measured runs spent 269-282s and 33-34 tool
            # turns per hour greping a 202-line immutable document to produce
            # one level label, and no consumer of that label is formal: it does
            # not enter ranking, selection, or parameterization. Skipping it
            # returns that window to action execution, which is what the final
            # measured Fmax is actually made of.
            stage_metrics["knowledge"] = {"skipped": True}
            knowledge_transcript = [{
                "kind": "stage_skipped",
                "reason": "knowledge_stage_disabled",
                "detail": (
                    "Taxonomy classification produced no formal input to "
                    "ranking or selection."
                ),
            }]
            knowledge_result = KnowledgeResearchResult(
                classifications=[],
                residual_update="",
                report_evidence_refs=list(report_result.evidence_refs),
                knowledge_evidence_refs=[],
                raw={
                    "stage": "knowledge_research",
                    "status": "skipped",
                    "classifications": [],
                    "report_evidence_refs": list(report_result.evidence_refs),
                    "knowledge_evidence_refs": [],
                    "residual_update": "",
                },
                transcript=copy.deepcopy(knowledge_transcript),
                metrics={"skipped": True},
            )
            self.last_cost_usd = max(
                0.0,
                float(getattr(self.llm, "total_cost", before_cost)) - before_cost,
            )
            return StagedResearchResult(
                state=copy.deepcopy(state),
                report_snapshot=report_snapshot,
                knowledge_snapshot=knowledge_snapshot,
                report_result=report_result,
                knowledge_result=knowledge_result,
                report_cached=report_cached,
                report_transcript=copy.deepcopy(report_transcript),
                knowledge_transcript=copy.deepcopy(knowledge_transcript),
                stage_metrics=copy.deepcopy(stage_metrics),
                before_cost_usd=before_cost,
            )
        knowledge_view = CorpusView(
            [knowledge_snapshot],
            GrepLimits.from_config(self.cfg, "research"),
        )
        knowledge_grep_tool = self._named_grep_tool("grep_knowledge", "knowledge")
        retrieval_cache_hits = 0
        retrieval_cache_misses = 0

        def research_handler(name: str, arguments: dict) -> dict:
            nonlocal retrieval_cache_hits, retrieval_cache_misses
            if name != "grep_knowledge":
                raise ValueError(f"tool is unavailable in Knowledge stage: {name}")
            scoped = self._scope_grep_arguments(
                arguments, scope="knowledge", snapshot=knowledge_snapshot
            )
            cache_key = json.dumps(
                {
                    "scope": "knowledge",
                    "snapshot_id": knowledge_snapshot.snapshot_id,
                    "arguments": scoped,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            cached = self._knowledge_retrieval_cache.get(cache_key)
            if cached is not None:
                retrieval_cache_hits += 1
                return copy.deepcopy(cached)
            retrieval_cache_misses += 1
            result = knowledge_view.grep(scoped)
            self._knowledge_retrieval_cache[cache_key] = copy.deepcopy(result)
            return result

        def remaining_cost_budget() -> Optional[float]:
            if max_cost_usd is None:
                return None
            spent = max(
                0.0,
                float(getattr(self.llm, "total_cost", before_cost)) - before_cost,
            )
            return max(0.0, float(max_cost_usd) - spent)

        def validate_knowledge_terminal(
            name: str, parsed: dict, transcript: list[dict]
        ) -> Optional[str]:
            error = self._validate_terminal_payload(name, parsed)
            if error:
                return error
            observed = self._evidence_refs(transcript)
            observed.update(report_result.evidence_refs)
            error = self._citation_resolution_error(
                parsed, observed, report_snapshot, knowledge_snapshot
            )
            if error:
                return error
            return self._claim_local_knowledge_error(parsed)

        knowledge_stage_budget_s = float(
            self.cfg.get("react.knowledge_stage_budget_s", 60.0)
            if self.cfg is not None else 60.0
        )
        knowledge_deadline = time.monotonic() + knowledge_stage_budget_s
        if deadline_monotonic is not None:
            knowledge_deadline = min(knowledge_deadline, float(deadline_monotonic))
        knowledge_partial = False

        def preserved_deadline_partial(transcript: list[dict]) -> Optional[dict]:
            """Preserve the latest citation-valid classification synthesis."""
            for entry in reversed(transcript):
                if (
                    not isinstance(entry, dict)
                    or entry.get("tool") != "complete_knowledge_research"
                    or not isinstance(entry.get("arguments"), dict)
                ):
                    continue
                candidate = copy.deepcopy(entry["arguments"])
                if self._validate_terminal_payload(
                    "complete_knowledge_research", candidate
                ):
                    continue
                report_refs, taxonomy_refs = self._claim_local_knowledge_unions(
                    candidate
                )
                candidate["report_evidence_refs"] = report_refs
                candidate["knowledge_evidence_refs"] = taxonomy_refs
                observed = self._evidence_refs(transcript)
                observed.update(report_result.evidence_refs)
                if self._citation_resolution_error(
                    candidate,
                    observed,
                    report_snapshot,
                    knowledge_snapshot,
                ):
                    continue
                if self._claim_local_knowledge_error(candidate):
                    continue
                candidate["residual_update"] = (
                    "typed_partial: Knowledge stage wall budget exhausted after "
                    "preserving citation-validated taxonomy classifications. "
                    f"{candidate['residual_update']}"
                )
                return candidate
            return None

        try:
            knowledge_out = self.llm.tool_loop_call(
                "knowledge_research",
                KNOWLEDGE_RESEARCH_SYSTEM,
                build_knowledge_research_user(
                    report_result.raw,
                    report_result.evidence_refs,
                    knowledge_snapshot.manifest_for_llm(),
                ),
                tools=[knowledge_grep_tool, self.knowledge_research_tool],
                handler=research_handler,
                terminal_tools={"complete_knowledge_research"},
                required_tool_calls={"grep_knowledge": 1},
                deadline_monotonic=knowledge_deadline,
                max_cost_usd=remaining_cost_budget(),
                terminal_validator=validate_knowledge_terminal,
            )
        except ToolLoopError as exc:
            knowledge_transcript = list(exc.transcript)
            budget_exhausted = (
                time.monotonic() >= knowledge_deadline
                or "deadline" in str(exc).lower()
            )
            if not budget_exhausted:
                stage_metrics["knowledge"] = exc.metrics
                return finish_invalid(
                    str(exc),
                    error_class=(
                        "provider_failure" if exc.provider_failure else "reasoning"
                    ),
                )
            knowledge_partial = True
            partial = preserved_deadline_partial(knowledge_transcript)
            knowledge_transcript.append({
                "kind": "typed_partial_evidence",
                "reason": "knowledge_stage_budget_exhausted",
                "budget_s": knowledge_stage_budget_s,
                "silent_empty": False,
                "preserved": partial is not None,
                "preserved_classification_count": (
                    len(partial["classifications"]) if partial else 0
                ),
                "preserved_report_evidence_ref_count": (
                    len(partial["report_evidence_refs"]) if partial else 0
                ),
                "preserved_knowledge_evidence_ref_count": (
                    len(partial["knowledge_evidence_refs"]) if partial else 0
                ),
            })
            if partial is None:
                partial = {
                    "classifications": [],
                    "residual_update": (
                        "typed_partial: Knowledge taxonomy budget exhausted; "
                        "the downstream selector must rely on the validated Report "
                        "result and authorized Action Cards."
                    ),
                    "report_evidence_refs": [],
                    "knowledge_evidence_refs": [],
                }
            knowledge_out = {
                "parsed": partial,
                "terminal_tool": "complete_knowledge_research",
                "transcript": knowledge_transcript,
                **exc.metrics,
            }
            stage_metrics["knowledge"] = {
                **exc.metrics,
                "typed_partial": True,
                "budget_exhausted": True,
                "budget_s": knowledge_stage_budget_s,
                "preserved_evidence": bool(partial["report_evidence_refs"]),
            }
        knowledge_transcript = list(knowledge_out.get("transcript") or [])
        if not knowledge_partial:
            stage_metrics["knowledge"] = self._stage_metrics(knowledge_out)
        stage_metrics["knowledge"].update({
            "retrieval_cache_hits": retrieval_cache_hits,
            "retrieval_cache_misses": retrieval_cache_misses,
        })
        knowledge_parsed = knowledge_out.get("parsed")
        if not isinstance(knowledge_parsed, dict):
            return finish_invalid("Knowledge terminal payload must be an object")
        knowledge_audit = copy.deepcopy(knowledge_parsed)
        terminal_error = self._validate_terminal_payload(
            str(knowledge_out.get("terminal_tool") or ""), knowledge_parsed
        )
        if terminal_error:
            return finish_invalid(terminal_error)
        knowledge_observed = self._evidence_refs(knowledge_transcript)
        knowledge_observed.update(report_result.evidence_refs)
        citation_error = self._citation_resolution_error(
            knowledge_parsed,
            knowledge_observed,
            report_snapshot,
            knowledge_snapshot,
        )
        if citation_error:
            return finish_invalid(citation_error)
        claim_error = self._claim_local_knowledge_error(knowledge_parsed)
        if claim_error:
            return finish_invalid(claim_error)
        knowledge_result = KnowledgeResearchResult(
            classifications=copy.deepcopy(knowledge_parsed["classifications"]),
            residual_update=knowledge_parsed["residual_update"],
            report_evidence_refs=list(knowledge_parsed["report_evidence_refs"]),
            knowledge_evidence_refs=list(
                knowledge_parsed["knowledge_evidence_refs"]
            ),
            raw=copy.deepcopy(knowledge_parsed),
            transcript=copy.deepcopy(knowledge_transcript),
            metrics=self._stage_metrics(knowledge_out),
        )

        self.last_cost_usd = max(
            0.0,
            float(getattr(self.llm, "total_cost", before_cost)) - before_cost,
        )
        return StagedResearchResult(
            state=copy.deepcopy(state),
            report_snapshot=report_snapshot,
            knowledge_snapshot=knowledge_snapshot,
            report_result=report_result,
            knowledge_result=knowledge_result,
            report_cached=report_cached,
            report_transcript=copy.deepcopy(report_transcript),
            knowledge_transcript=copy.deepcopy(knowledge_transcript),
            stage_metrics=copy.deepcopy(stage_metrics),
            before_cost_usd=before_cost,
        )

    def staged_decide(
        self,
        state: dict,
        report_snapshot: CorpusSnapshot,
        knowledge_snapshot: CorpusSnapshot,
        deadline_monotonic: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
    ) -> Decision:
        """Run validated staged research, then select one authorized Action Card."""
        research = self.staged_research(
            state,
            report_snapshot,
            knowledge_snapshot,
            deadline_monotonic=deadline_monotonic,
            max_cost_usd=max_cost_usd,
        )
        if isinstance(research, Decision):
            return research
        return self.select_from_action_menu(
            research,
            deadline_monotonic=deadline_monotonic,
            max_cost_usd=max_cost_usd,
        )

    def select_from_action_menu(
        self,
        research: StagedResearchResult,
        *,
        deadline_monotonic: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
    ) -> Decision:
        """Select exactly one tool-authorized entry using validated research evidence."""
        state = copy.deepcopy(research.state)
        menu = validate_action_menu(state.get("authorized_action_menu"))
        public_menu = project_action_menu_for_decision(menu)
        measured_state = {
            key: value for key, value in state.items()
            if key != "authorized_action_menu"
        }
        measured_state["staged_research"] = {
            "report_analysis": copy.deepcopy(research.report_result.raw),
            "knowledge_research": copy.deepcopy(research.knowledge_result.raw),
            "authority": "reasoning_evidence_only",
        }
        spent = max(
            0.0,
            float(getattr(self.llm, "total_cost", research.before_cost_usd))
            - float(research.before_cost_usd),
        )
        remaining_cost = (
            None
            if max_cost_usd is None
            else max(0.0, float(max_cost_usd) - spent)
        )
        ranked = self.rank_public_action_menu(
            measured_state,
            public_menu,
            deadline_monotonic=deadline_monotonic,
            max_cost_usd=remaining_cost,
        )
        out = ranked["llm_result"]
        selection = select_action_menu_entry(
            menu,
            ranked["selection"],
            current_seed_sha256=menu["seed_artifact_sha256"],
        )
        action = selection["action"]
        kind = action.get("action")
        decision = Decision(
            action=str(kind or "invalid"),
            skill=str(action.get("skill") or ""),
            target=str(action.get("target") or "auto"),
            params=copy.deepcopy(action.get("params") or {}),
            expectation=selection["rationale"],
            reflection=json.dumps(
                selection.get("decision_analysis")
                or {"why_selected": selection["rationale"]},
                sort_keys=True,
            ),
            stop_reason=str(action.get("stop_reason") or ""),
            stop_rationale=selection["rationale"],
            seed_candidate_id=selection["seed_candidate_id"],
            server=str(action.get("server") or "vivado"),
            tool=str(action.get("tool") or ""),
            arguments=copy.deepcopy(action.get("arguments") or {}),
            selected_entry_id=selection["selected_entry_id"],
            action_menu_digest=selection["menu_digest"],
            action_menu_seed_sha256=selection["seed_artifact_sha256"],
            raw={
                **copy.deepcopy(ranked["selection"]),
                "decision_analysis": copy.deepcopy(
                    selection.get("decision_analysis")
                ),
            },
        )
        decision.report_analysis = copy.deepcopy(research.report_result.raw)
        decision.knowledge_research = copy.deepcopy(research.knowledge_result.raw)
        decision.report_snapshot_id = research.report_snapshot.snapshot_id
        decision.knowledge_snapshot_id = research.knowledge_snapshot.snapshot_id
        stage_metrics = copy.deepcopy(research.stage_metrics)
        stage_metrics["action_menu_selection"] = self._stage_metrics(out)
        decision.react_transcript = {
            "report_cached": research.report_cached,
            "report": (
                []
                if research.report_cached
                else copy.deepcopy(research.report_transcript)
            ),
            "knowledge": copy.deepcopy(research.knowledge_transcript),
            "decision": copy.deepcopy(out.get("transcript") or []),
            "stage_metrics": stage_metrics,
            "status": "valid_staged_action_menu_selection",
        }
        self.last_cost_usd = max(
            0.0,
            float(getattr(self.llm, "total_cost", research.before_cost_usd))
            - float(research.before_cost_usd),
        )
        return decision

    def rank_public_action_menu(
        self,
        measured_state: dict,
        public_menu: dict,
        *,
        deadline_monotonic: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
    ) -> dict:
        """Rank non-executable cards for production or offline diagnostics."""
        state = copy.deepcopy(measured_state) if isinstance(measured_state, dict) else {}
        if "authorized_action_menu" in state:
            raise ValueError("public Decision state must not contain a private menu")
        public_menu = validate_public_action_menu(public_menu)
        eligible_ids = [
            str(entry["entry_id"]) for entry in public_menu["entries"]
        ]
        if not eligible_ids:
            raise ValueError("authorized action menu has no decision-lane entry")
        tool = {
            "type": "function",
            "name": "select_action_menu_entry",
            "description": (
                "Compare current-seed mechanism hypotheses and select exactly "
                "one tool-authorized entry. The ID determines all mutation "
                "parameters; the analysis explains the choice but grants no authority."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_entry_id": {
                        "type": "string",
                        "enum": eligible_ids,
                    },
                    "reasoning": {
                        "type": "object",
                        "properties": {
                            "current_wall": {"type": "string", "minLength": 1},
                            "why_selected": {"type": "string", "minLength": 1},
                            "why_not_alternatives": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "expected_next_wall": {"type": "string", "minLength": 1},
                            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                            "confidence": {
                                "type": "string",
                                "enum": ["trusted", "uncertain", "untrusted"],
                            },
                        },
                        "required": [
                            "current_wall", "why_selected", "why_not_alternatives",
                            "expected_next_wall", "risk", "confidence"
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": ["selected_entry_id", "reasoning"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        user = json.dumps({
            "task": (
                "Use the exact current-seed measured state, validated staged research when "
                "present, and Action Cards. Staged research is reasoning evidence only and "
                "grants no mutation authority or parameters. "
                "Maximize the final legal measured DCP Fmax; action coverage, "
                "infrastructure completeness, and a result that merely looks "
                "reasonable are not optimization objectives. "
                "Identify the current timing wall, compare distinct mechanisms, "
                "state why the alternatives are weaker now, predict the next wall, "
                "and select one exact ID. Reason about the parameter regime shown "
                "by the card, but do not invent Tcl, object names, coordinates, or "
                "unlisted numeric parameters. Treat the tool-built ranking_score as "
                "the calibrated default ordering. Deviate only when exact current-seed "
                "evidence identifies a concrete enabling or final-Fmax advantage that "
                "the score omitted; generic diversification is not such evidence. "
                "Compare enabling value, measured same-run feedback, runtime, and "
                "expected final Fmax before choosing any authorized entry."
            ),
            "measured_state": state,
            "authorized_action_menu": public_menu,
        }, sort_keys=True, allow_nan=False)

        def unavailable(name: str, arguments: dict) -> dict:
            del arguments
            raise ValueError(f"unexpected action-menu tool {name!r}")

        out = self.llm.tool_loop_call(
            "action_menu_selection",
            (
                "You reason about competing FPGA optimization mechanisms from one "
                "fresh current-seed profile. Validated staged Report/Knowledge research "
                "may refine the diagnosis but never grants mutation authority; only the "
                "Action Cards are selectable. Compare wall ownership, expected effect, "
                "risk, cost, prior same-run feedback, and likely next wall. Proof, "
                "provenance, exact parameters, execution, measurement, and promotion "
                "remain tool-owned. Historical recipes and benchmark identity provide "
                "no authority. Prefer the action most likely to raise final legal "
                "measured Fmax; do not repeat a weak mechanism merely because its "
                "last delta was positive. Treat a high-value action's remaining "
                "launch margin as consumable: when another measured cycle would make "
                "that action unlaunchable, compare the smaller action's expected gain "
                "against the opportunity cost of losing the larger mechanism, rather "
                "than simply trying cheap actions first. Semantic completeness proves "
                "applicability and mutation authority, not superior QoR by itself. "
                "Treat basin_opportunity_signal and search_role as scope evidence: "
                "a route-dominated low-utilization seed can justify rebuilding a basin, "
                "but pblock containment additionally requires a nonzero current-seed "
                "containment_opportunity_signal derived from measured path dispersion. "
                "Do not turn route delay into congestion by using hard containment on "
                "a spatially compact wall. When dispersed paths and placement headroom "
                "indicate an unresolved basin, a local cleanup cannot substitute for an "
                "authorized basin builder. After a builder promotes, use the freshly "
                "profiled descendant to decide whether locality is resolved and a "
                "path-local or routed refinement now owns the wall. "
                "After a path-local topology mutation promotes on a route-dominated "
                "wall, explicitly compare one bounded routed physical consolidation "
                "before stacking another topology mutation: the parent gain proves "
                "the completed change, while the descendant profile decides whether "
                "routing must settle before another independently discovered edit. "
                "closure_distance_signal is the share of eight official periods "
                "by which this seed misses its official clock. As it approaches "
                "one, an action implementing against the official target does not "
                "converge inside the run and returns no candidate, so prefer the "
                "mechanism that implements under a temporary target derived from "
                "the achieved period and restores the official clock. "
                "Do not choose a lower-ranked local edit merely to diversify when a "
                "higher-ranked card has measured positive lineage feedback and still "
                "covers the freshly measured wall. "
                "A repeated_basin_backend is the same mechanism family/backend that "
                "already produced the direct ancestor; it is not a distinct basin "
                "builder, and ancestor_gain_consumed means its parent gain is already "
                "realized. When an untried authorized basin builder is available, "
                "compare that distinct mechanism before repeating the consumed backend. "
                "A large measured gain from the parent builder describes the completed "
                "parent-to-child transition; it is not a prediction that repeating "
                "the builder has the same marginal value on the child. If the fresh "
                "descendant profile shows collapsed spread or basin opportunity, "
                "prefer the distinct mechanism that owns its residual wall. Repeat a "
                "builder only when the descendant itself still shows an unresolved "
                "basin and already-tried residual-wall mechanisms failed, not merely "
                "because its parent transition was successful. Route dominance alone "
                "does not prove that rerunning the same stochastic backend is more "
                "valuable than a newly justified routed or path-local refinement. "
                "For a single-net fanout action, path incidence is not delay ownership; "
                "discount a widely occurring target unless its current profile also "
                "shows meaningful delay contribution. A timing-driven broad fanout "
                "primitive, including a bounded multi-net replication action, is a "
                "distinct search mechanism: compare its aggregate live "
                "route pressure, fanout strength, and path reach rather than assigning "
                "it the evidence or outcome of one explicit target. "
                "Distinguish LUT pin remapping from path reflow even though both touch "
                "the same live LUT wall. On a shallow, route-dominated wall with high "
                "critical_lut_route_coverage, an authorized lut_pin_swap directly "
                "targets pin-to-pin route delay while preserving the placement basin; "
                "path_local_lut_reflow is the stronger fit when measured logic depth or "
                "a mixed logic/placement wall justifies moving and rerouting the bounded "
                "path. Pin remapping is endgame repair, not a substitute for an unresolved "
                "basin: when absolute WNS remains large relative to the official period, "
                "prefer a justified builder or broader wall owner unless those mechanisms "
                "have already failed on the current lineage. Do not choose reflow merely "
                "because it sounds broader. "
                "Semantic completeness alone is not QoR evidence and does not justify "
                "skipping a better live-wall owner. However, when a proved current-seed "
                "transaction owns the earliest live wall and its card marks the "
                "opportunity as perishable, do not choose a destructive physical remap "
                "first: physical search remains available after the semantic attempt, "
                "while the reverse order can erase the only proved transaction. This is "
                "current action-DAG dominance, not a historical action order. Ranking is "
                "advisory among non-dominated authorized entries, and the selected entry "
                "need not have the largest scalar score when another mechanism better "
                "serves final QoR. "
                "A composed implementation recipe is not automatically broader or "
                "more valuable than a direct action merely because its card lists "
                "more observations or stages. Compare the recipe's concrete added "
                "interventions against live evidence. When a higher-ranked direct "
                "action already owns the same wall, prefer it unless the recipe's "
                "additional pressure, retiming, cleanup, or reroute mechanism has a "
                "distinct current-seed reason to improve final Fmax. "
                "Do not infer proof strength from the internal card kind: a trusted "
                "card whose current-seed proof_status is proved remains proved even "
                "when its executor must independently revalidate before emission. "
                "When proof confidence is equal, compare measured wall ownership, "
                "mechanism effect, runtime, and expected final Fmax rather than "
                "preferring the card that merely looks more final."
            ),
            user,
            tools=[tool],
            handler=unavailable,
            terminal_tools={"select_action_menu_entry"},
            required_tool_calls={},
            max_turns=1,
            tool_choice={"type": "function", "name": "select_action_menu_entry"},
            deadline_monotonic=deadline_monotonic,
            max_cost_usd=max_cost_usd,
        )
        self.last_cost_usd = float(out.get("cost_usd", 0.0) or 0.0)
        if out.get("terminal_tool") != "select_action_menu_entry":
            raise ValueError("action-menu selector used the wrong terminal tool")
        selection = out.get("parsed")
        if (
            not isinstance(selection, Mapping)
            or set(selection) != {"selected_entry_id", "reasoning"}
            or selection.get("selected_entry_id") not in eligible_ids
            or not isinstance(selection.get("reasoning"), Mapping)
        ):
            raise ValueError("action-menu selector returned an invalid public selection")
        return {
            "selection": copy.deepcopy(dict(selection)),
            "public_action_menu": public_menu,
            "llm_result": out,
        }

    def analyze(self, state: dict, case_matches: list[dict]) -> AnalyzeResult:
        out = self.llm.stage_call(
            "analyze", self.system_prompt, build_analyze_user(state, case_matches)
        )
        parsed = out.get("parsed") or {}
        if not isinstance(parsed, dict):
            parsed = {}
        if not parsed:
            logger.warning(
                "[stages] analyze returned no JSON; raw=%r",
                out.get("text", "")[:400],
            )
        strategy = parsed.get("strategy")
        return AnalyzeResult(
            bottleneck=str(parsed.get("bottleneck", "mixed")),
            diagnosis=str(parsed.get("diagnosis", "")),
            strategy=strategy if isinstance(strategy, list) else [],
            case_verdict=str(parsed.get("case_verdict", "")),
            stop_hint=str(parsed.get("stop_hint", "")),
            raw=parsed,
        )

    def decide(
        self,
        state: dict,
        case_matches: list[dict],
        probe_result: Optional[dict] = None,
    ) -> Decision:
        user = (
            build_probe_followup_user(state, probe_result)
            if probe_result is not None
            else build_decide_user(state, case_matches)
        )
        out = self.llm.stage_call("decide", self.system_prompt, user)
        parsed = out.get("parsed")
        if not isinstance(parsed, dict):
            logger.warning(
                "[stages] decide returned no JSON; raw=%r",
                out.get("text", "")[:400],
            )
            return Decision(action="invalid", raw={"error": "no JSON in reply"})
        decision = self._to_decision(parsed)
        if decision.action == "invalid":
            logger.warning(
                "[stages] decide rejected JSON: error=%r parsed=%r",
                decision.raw,
                parsed,
            )
        return decision

    def _to_decision(self, parsed: dict) -> Decision:
        action = str(parsed.get("action", "run_skill")).strip().lower()
        params = parsed.get("params")
        arguments = parsed.get("arguments")
        decision = Decision(
            action=action,
            skill=str(parsed.get("skill", "") or ""),
            target=str(parsed.get("target", "auto") or "auto"),
            params=params if isinstance(params, dict) else {},
            expectation=str(parsed.get("expectation", "") or ""),
            knowledge_comparison=str(parsed.get("knowledge_comparison", "") or ""),
            override_knowledge_direction=(
                parsed.get("override_knowledge_direction")
                if isinstance(parsed.get("override_knowledge_direction"), bool)
                else False
            ),
            failure_update=str(parsed.get("failure_update", "") or ""),
            reflection=str(parsed.get("reflection", "") or ""),
            stop_reason=str(parsed.get("stop_reason", "") or ""),
            stop_rationale=str(parsed.get("stop_rationale", "") or ""),
            seed_candidate_id=(
                str(parsed["seed_candidate_id"])
                if parsed.get("seed_candidate_id") is not None
                else None
            ),
            server=str(parsed.get("server", "vivado") or "vivado"),
            tool=str(parsed.get("tool", "") or ""),
            arguments=arguments if isinstance(arguments, dict) else {},
            question=str(parsed.get("question", "") or ""),
            evidence_refs=(
                list(parsed.get("evidence_refs", []))
                if isinstance(parsed.get("evidence_refs"), list)
                and all(isinstance(item, str) for item in parsed["evidence_refs"])
                else []
            ),
            raw=copy.deepcopy(parsed),
        )
        if decision.action == "run_skill" and decision.skill == "custom":
            decision.action = "probe"
            if not decision.tool:
                decision.tool = decision.target if decision.target != "auto" else ""
            if not decision.arguments and isinstance(
                decision.params.get("arguments"), dict
            ):
                decision.arguments = decision.params["arguments"]
            if decision.params.get("server"):
                decision.server = str(decision.params["server"])
        if decision.action == "run_skill" and self._contains_raw_tcl(decision.params):
            return Decision(
                action="invalid",
                raw={"error": "LLM actions cannot provide raw Tcl recipe text"},
            )
        if (
            decision.action == "run_skill"
            and bool(
                self.cfg.get("score_scheduling.enabled", True)
                if self.cfg is not None
                else True
            )
        ):
            try:
                decision.score_estimate = normalize_score_estimate(
                    parsed.get("score_estimate")
                )
            except (TypeError, ValueError) as exc:
                return Decision(
                    action="invalid",
                    raw={"error": "invalid score_estimate", "detail": str(exc)},
                )
        return decision

    def reconsider_action(
        self,
        *,
        state: dict,
        provisional_action: dict,
        action_proof: dict,
    ) -> ActionReview:
        out = self.llm.stage_call(
            "reconsider",
            RECONSIDER_SYSTEM,
            build_reconsider_user(
                state=state,
                provisional_action=provisional_action,
                action_proof=action_proof,
            ),
        )
        parsed = out.get("parsed")
        if not isinstance(parsed, dict):
            raise ValueError("reconsideration returned no JSON object")
        allowed = {"action", "assessment", "reason"}
        unknown = set(parsed) - allowed
        missing = allowed - set(parsed)
        if missing:
            raise ValueError(f"reconsideration is missing fields: {sorted(missing)}")
        if unknown:
            raise ValueError(f"reconsideration has unknown fields: {sorted(unknown)}")
        action = parsed.get("action")
        if action not in {"confirm", "replan", "save_and_exit"}:
            raise ValueError(f"invalid reconsideration action {action!r}")
        assessment = parsed.get("assessment")
        if not isinstance(assessment, str) or not assessment.strip():
            raise ValueError("reconsideration assessment must be nonempty")
        reason = parsed.get("reason")
        if not isinstance(reason, str):
            raise ValueError("reconsideration reason must be a string")
        if action != "confirm" and not reason.strip():
            raise ValueError(f"{action} reconsideration requires a reason")
        prior = action_proof.get("prior_evidence") or {}
        exact_contradiction = bool(prior.get("contradicting_evidence") or [])
        contract_repair = None
        if action != "confirm" and not exact_contradiction:
            contract_repair = {
                "requested": action,
                "reason": reason[:1000],
                "rule": "unknown effectiveness is not measured contradiction",
            }
            action = "confirm"
            reason = ""
        return ActionReview(
            action=action,
            assessment=assessment[:1000],
            reason=reason[:1000],
            raw={
                **copy.deepcopy(parsed),
                "action": action,
                **({"contract_repair": contract_repair} if contract_repair else {}),
            },
        )

    def to_action_dict(self, decision: Decision) -> dict:
        if decision.action == "probe":
            action = {
                "action": "probe",
                "server": decision.server,
                "tool": decision.tool,
                "arguments": decision.arguments,
            }
            if decision.seed_candidate_id is not None:
                action["seed_candidate_id"] = decision.seed_candidate_id
            return action
        if decision.action == "save_and_exit":
            return {
                "action": "save_and_exit",
                "stop_reason": decision.stop_reason,
                "stop_rationale": decision.stop_rationale,
            }
        if decision.action == "restart_vivado":
            return {"action": "restart_vivado"}
        action = {
            "action": "run_skill",
            "skill": decision.skill,
            "target": decision.target,
            "params": decision.params,
        }
        if decision.seed_candidate_id is not None:
            action["seed_candidate_id"] = decision.seed_candidate_id
        return action


class ReplayStageRunner:
    """One-turn selector over caller-provided anonymous replay inputs only."""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.last_metrics: dict = {}

    def select_action(
        self,
        anonymous_observation: Mapping[str, object] | object,
        eligible_action_ids: Sequence[str],
        *,
        deadline_monotonic: Optional[float] = None,
        max_cost_usd: Optional[float] = None,
    ) -> dict:
        if not isinstance(anonymous_observation, Mapping):
            from .replay import AnonymousObservation

            if not isinstance(anonymous_observation, AnonymousObservation):
                raise TypeError("replay selector requires an anonymous observation")
            anonymous_observation = anonymous_observation.as_dict()
        tool = build_replay_selection_tool(eligible_action_ids)
        user = build_replay_selection_user(
            anonymous_observation, eligible_action_ids
        )

        def unavailable(name: str, arguments: dict) -> dict:
            del arguments
            raise ValueError(f"unexpected replay tool {name!r}")

        out = self.llm.tool_loop_call(
            "replay_selection",
            REPLAY_SELECTION_SYSTEM,
            user,
            tools=[tool],
            handler=unavailable,
            terminal_tools={"select_replay_action"},
            required_tool_calls={},
            max_turns=1,
            tool_choice={"type": "function", "name": "select_replay_action"},
            deadline_monotonic=deadline_monotonic,
            max_cost_usd=max_cost_usd,
        )
        self.last_metrics = StageRunner._stage_metrics(out)
        if out.get("terminal_tool") != "select_replay_action":
            raise ValueError("replay selector used the wrong terminal tool")
        parsed = out.get("parsed")
        if not isinstance(parsed, dict):
            raise ValueError("replay selector returned no object")
        from .replay import POLICY_VERSION, parse_llm_selection

        action_id = parse_llm_selection(parsed, eligible_action_ids)
        return {"version": POLICY_VERSION, "action_id": action_id}

    def run(
        self,
        anonymous_observation: Mapping[str, object] | object,
        eligible_action_ids: Sequence[str],
        **kwargs,
    ) -> dict:
        return self.select_action(
            anonymous_observation, eligible_action_ids, **kwargs
        )
