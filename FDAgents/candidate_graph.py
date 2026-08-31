"""Pure, serializable candidate-graph mechanics.

The graph stores immutable checkpoint identities and search metadata, but does
not perform filesystem or tool operations.  Its Pareto beam intentionally has
only two objectives: maximize measured Fmax and minimize cumulative lineage
runtime.  Lineage runtime is a search heuristic, not the contest's actual
incremental runtime cost, and it is not a proxy for physical-design diversity.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Iterable, Optional

from .enabling_planner import evaluate_enabling_candidate
from .physical_signature import physical_signature_distance


GRAPH_VERSION = 5
ROOT_CANDIDATE_ID = "c000000"
_CANDIDATE_ID_RE = re.compile(r"^c(\d{6})$")


def candidate_numeric_id(candidate_id: str) -> int:
    match = _CANDIDATE_ID_RE.fullmatch(str(candidate_id))
    if not match:
        raise ValueError(f"invalid candidate ID {candidate_id!r}")
    return int(match.group(1))


def empty_graph(beam_width: int = 3) -> dict:
    """Return an uninitialized graph containing no root node."""
    if isinstance(beam_width, bool) or not isinstance(beam_width, int):
        raise TypeError("beam_width must be an integer")
    if beam_width < 1:
        raise ValueError("beam_width must be at least 1")
    return {
        "version": GRAPH_VERSION,
        "beam_width": beam_width,
        "root_candidate_id": None,
        "current_best_candidate_id": None,
        "beam_candidate_ids": [],
        "next_candidate_sequence": 0,
        "nodes": {},
        "beam_objectives": "max_fmax,min_lineage_runtime",
        "lineage_runtime_is_search_heuristic": True,
        "pending_gc_candidate_ids": [],
        "enabling_pool": _empty_enabling_pool(),
        "beam_retention": {
            "version": 1,
            "physical_diversity_enabled": True,
            "minimum_common_dimensions": 4,
            "method": "greedy_max_min_within_truncated_pareto_front",
            "fallback": "nsga2_crowding",
            "continuation_reserve": "one_recent_round_when_width_allows",
        },
    }


def make_node(
    *,
    candidate_id: str,
    parent_candidate_id: Optional[str],
    depth: int,
    created_round: int,
    created_at: float,
    artifact_path: str,
    artifact_state: str,
    artifact_size_bytes: Optional[int],
    artifact_sha256: str = "",
    search_state: Optional[str] = None,
    action_fingerprint: str = "",
    skill: str = "root",
    target: str = "root",
    resolved_params: Optional[dict] = None,
    summary: str = "",
    measurement: Optional[dict] = None,
    action_runtime_s: float = 0.0,
    validation_runtime_s: float = 0.0,
    cumulative_lineage_runtime_s: float = 0.0,
    promoted_to_global_best: bool = False,
    in_active_beam: bool = False,
    pruned_round: Optional[int] = None,
    physical_signature: Optional[dict] = None,
    evidence: Optional[dict] = None,
    eco_proof: Optional[dict] = None,
) -> dict:
    """Build a node with the complete stable schema and JSON-safe values."""
    candidate_numeric_id(candidate_id)
    if parent_candidate_id is not None:
        candidate_numeric_id(parent_candidate_id)
    if artifact_state not in {
        "materialized", "missing", "deleted", "invalid", "pruned"
    }:
        raise ValueError(f"invalid artifact_state {artifact_state!r}")
    if artifact_state == "invalid":
        artifact_state = "materialized"
        search_state = search_state or "ineligible"
    elif artifact_state == "pruned":
        artifact_state = "materialized"
        search_state = search_state or "pruned"
    if search_state is None:
        search_state = "active" if artifact_state == "materialized" else "ineligible"
    if search_state not in {"active", "pruned", "ineligible"}:
        raise ValueError(f"invalid search_state {search_state!r}")
    measurement = measurement or {}
    return {
        "candidate_id": candidate_id,
        "parent_candidate_id": parent_candidate_id,
        "depth": int(depth),
        "created_round": int(created_round),
        "created_at": float(created_at),
        "artifact_path": str(artifact_path),
        "artifact_state": artifact_state,
        "artifact_size_bytes": (
            int(artifact_size_bytes) if artifact_size_bytes is not None else None
        ),
        "artifact_sha256": str(artifact_sha256),
        "search_state": search_state,
        "artifact_deleted_at": None,
        "artifact_delete_reason": "",
        "action_fingerprint": str(action_fingerprint),
        "skill": str(skill),
        "target": str(target),
        "resolved_params": json.loads(json.dumps(resolved_params or {}, default=str)),
        "summary": str(summary),
        "clock_period_ns": measurement.get("clock_period_ns"),
        "wns_ns": measurement.get("wns_ns"),
        "fmax_mhz": measurement.get("fmax_mhz"),
        "tns_ns": measurement.get("tns_ns"),
        "failing_endpoints": measurement.get("failing_endpoints"),
        "total_endpoints": measurement.get("total_endpoints"),
        "whs_ns": measurement.get("whs_ns"),
        "wpws_ns": measurement.get("wpws_ns"),
        "route_legal": measurement.get("route_legal"),
        "placed_legal": measurement.get("placed_legal"),
        "unplaced_primitives": measurement.get("unplaced_primitives"),
        "hold_legal": measurement.get("hold_legal"),
        "pulse_legal": measurement.get("pulse_legal"),
        "clock_identity_preserved": measurement.get(
            "clock_identity_preserved"
        ),
        "timing_xdc_preserved": measurement.get("timing_xdc_preserved"),
        "constraint_identity_preserved": measurement.get(
            "constraint_identity_preserved"
        ),
        "reference_timing_xdc_sha256": measurement.get(
            "reference_timing_xdc_sha256"
        ),
        "candidate_timing_xdc_sha256": measurement.get(
            "candidate_timing_xdc_sha256"
        ),
        "measurement_complete": bool(measurement.get("measurement_complete", False)),
        "functional_equivalence": "unknown",
        "equivalence_method": None,
        "equivalence_checked_at": None,
        "equivalence_report_path": None,
        "equivalence_root_sha256": None,
        "equivalence_candidate_sha256": None,
        "submission_eligible": False,
        "eligible_for_beam": bool(measurement.get("eligible_for_beam", False)),
        "legality_reason": str(measurement.get("legality_reason", "")),
        "action_runtime_s": float(action_runtime_s),
        "validation_runtime_s": float(validation_runtime_s),
        "cumulative_lineage_runtime_s": float(cumulative_lineage_runtime_s),
        "promoted_to_global_best": bool(promoted_to_global_best),
        "in_active_beam": bool(in_active_beam),
        "pruned_round": int(pruned_round) if pruned_round is not None else None,
        "physical_signature": copy.deepcopy(
            physical_signature
            or {
                "version": 1,
                "status": "legacy_unavailable",
                "artifact_sha256": str(artifact_sha256),
                "dimensions": {},
                "digest_sha256": "",
            }
        ),
        "evidence": copy.deepcopy(evidence),
        "eco_proof": copy.deepcopy(eco_proof),
        "continuation": {
            "version": 1,
            "consumed_round": None,
            "consumed_kind": "",
            "last_protected_round": None,
        },
        "enabling_candidate": None,
    }


def initialize_graph(root_node: dict, beam_width: int = 3) -> dict:
    """Create a graph whose allocated root is ``c000000``."""
    if root_node.get("candidate_id") != ROOT_CANDIDATE_ID:
        raise ValueError(f"root candidate must be {ROOT_CANDIDATE_ID}")
    if root_node.get("parent_candidate_id") is not None:
        raise ValueError("root candidate cannot have a parent")
    graph = empty_graph(beam_width)
    root = copy.deepcopy(root_node)
    root["promoted_to_global_best"] = True
    root["in_active_beam"] = bool(root.get("eligible_for_beam"))
    root["search_state"] = "active" if root["in_active_beam"] else "ineligible"
    root["functional_equivalence"] = "passed"
    root["equivalence_method"] = "identity"
    root["equivalence_root_sha256"] = root.get("artifact_sha256")
    root["equivalence_candidate_sha256"] = root.get("artifact_sha256")
    root["submission_eligible"] = bool(
        root.get("eligible_for_beam") and root.get("placed_legal") is True
    )
    graph.update({
        "root_candidate_id": ROOT_CANDIDATE_ID,
        "current_best_candidate_id": ROOT_CANDIDATE_ID,
        "beam_candidate_ids": (
            [ROOT_CANDIDATE_ID] if root["in_active_beam"] else []
        ),
        "next_candidate_sequence": 1,
        "nodes": {ROOT_CANDIDATE_ID: root},
    })
    return graph


def allocate_candidate_id(graph: dict) -> str:
    """Allocate the next sequential ID, mutating only the sequence counter."""
    sequence = int(graph.get("next_candidate_sequence", 0))
    candidate_id = f"c{sequence:06d}"
    if candidate_id in graph.get("nodes", {}):
        raise ValueError(f"candidate ID already exists: {candidate_id}")
    graph["next_candidate_sequence"] = sequence + 1
    return candidate_id


def add_node(graph: dict, node: dict) -> None:
    """Add one already allocated child node to the graph."""
    candidate_id = str(node.get("candidate_id", ""))
    candidate_numeric_id(candidate_id)
    if candidate_id in graph.get("nodes", {}):
        raise ValueError(f"candidate ID already exists: {candidate_id}")
    parent_id = node.get("parent_candidate_id")
    if parent_id not in graph.get("nodes", {}):
        raise ValueError(f"unknown parent candidate {parent_id!r}")
    expected_depth = int(graph["nodes"][parent_id].get("depth", 0)) + 1
    if int(node.get("depth", -1)) != expected_depth:
        raise ValueError(
            f"candidate {candidate_id} depth must be {expected_depth}"
        )
    graph["nodes"][candidate_id] = copy.deepcopy(node)


def dominates(left: dict, right: dict) -> bool:
    """Return whether ``left`` Pareto-dominates ``right`` on beam objectives."""
    left_fmax = float(left["fmax_mhz"])
    right_fmax = float(right["fmax_mhz"])
    left_runtime = float(left["cumulative_lineage_runtime_s"])
    right_runtime = float(right["cumulative_lineage_runtime_s"])
    no_worse = left_fmax >= right_fmax and left_runtime <= right_runtime
    strictly_better = left_fmax > right_fmax or left_runtime < right_runtime
    return no_worse and strictly_better


def pareto_fronts(nodes: Iterable[dict]) -> list[list[dict]]:
    """Return deterministic nondominated fronts for eligible nodes."""
    remaining = sorted(
        (node for node in nodes if _beam_eligible(node)),
        key=lambda node: candidate_numeric_id(node["candidate_id"]),
    )
    fronts: list[list[dict]] = []
    while remaining:
        front = [
            node
            for node in remaining
            if not any(
                other is not node and dominates(other, node)
                for other in remaining
            )
        ]
        fronts.append(front)
        front_ids = {node["candidate_id"] for node in front}
        remaining = [
            node for node in remaining if node["candidate_id"] not in front_ids
        ]
    return fronts


def crowding_distances(front: Iterable[dict]) -> dict[str, float]:
    """Compute normalized NSGA-II crowding distance for one Pareto front."""
    nodes = list(front)
    distances = {node["candidate_id"]: 0.0 for node in nodes}
    if len(nodes) <= 2:
        return {candidate_id: math.inf for candidate_id in distances}
    objectives = (
        ("fmax_mhz", False),
        ("cumulative_lineage_runtime_s", True),
    )
    for key, minimize in objectives:
        ordered = sorted(
            nodes,
            key=lambda node: (
                float(node[key]) if minimize else -float(node[key]),
                candidate_numeric_id(node["candidate_id"]),
            ),
        )
        values = [float(node[key]) for node in ordered]
        span = max(values) - min(values)
        distances[ordered[0]["candidate_id"]] = math.inf
        distances[ordered[-1]["candidate_id"]] = math.inf
        if span <= 0.0:
            continue
        for index in range(1, len(ordered) - 1):
            candidate_id = ordered[index]["candidate_id"]
            if math.isinf(distances[candidate_id]):
                continue
            distances[candidate_id] += abs(values[index + 1] - values[index - 1]) / span
    return distances


def promote(graph: dict, candidate_id: str) -> None:
    """Pin ``candidate_id`` as global best without deciding whether to promote."""
    if candidate_id not in graph.get("nodes", {}):
        raise ValueError(f"unknown candidate {candidate_id!r}")
    node = graph["nodes"][candidate_id]
    if not _beam_eligible(node):
        raise ValueError(f"candidate {candidate_id} is not beam eligible")
    graph["current_best_candidate_id"] = candidate_id
    node["promoted_to_global_best"] = True
    retire_enabling_candidates_after_promotion(graph, candidate_id)


def _empty_enabling_pool() -> dict:
    return {
        "version": 1,
        "active_candidate_ids": [],
        "retired_candidate_ids": [],
        "default_max_composition_depth": 2,
        "authority": "deterministic_measured_rules_no_llm_judgment",
    }


def is_active_enabling_candidate(graph: dict, candidate_id: str) -> bool:
    pool = graph.get("enabling_pool") or {}
    return str(candidate_id) in pool.get("active_candidate_ids", [])


def retain_enabling_candidate(
    graph: dict,
    candidate_id: str,
    *,
    parent_profile: dict,
    candidate_profile: dict,
    targeted_families: Iterable[str],
    candidate_legal: bool,
    strict_validation_passed: bool,
    promoted: bool = False,
    regression_tolerance_ns: float = 0.01,
    local_improvement_epsilon_ns: float = 0.0005,
    max_composition_depth: int = 2,
    pool_width: int = 4,
) -> dict:
    """Evaluate and, when eligible, retain a non-improving composition seed."""
    if candidate_id not in (graph.get("nodes") or {}):
        raise ValueError(f"unknown candidate {candidate_id!r}")
    if (
        isinstance(max_composition_depth, bool)
        or not isinstance(max_composition_depth, int)
        or max_composition_depth < 1
    ):
        raise ValueError("max_composition_depth must be at least 1")
    if (
        isinstance(pool_width, bool)
        or not isinstance(pool_width, int)
        or pool_width < 1
    ):
        raise ValueError("pool_width must be at least 1")
    node = graph["nodes"][candidate_id]
    parent_id = node.get("parent_candidate_id")
    parent = graph["nodes"].get(parent_id)
    if parent is None:
        raise ValueError("an enabling candidate must have a graph parent")
    if not isinstance(parent_profile, dict) or not isinstance(candidate_profile, dict):
        raise ValueError("parent and candidate residual profiles must be mappings")
    if parent_profile.get("artifact_sha256") != parent.get("artifact_sha256"):
        raise ValueError("parent residual profile is not bound to the parent artifact")
    if candidate_profile.get("artifact_sha256") != node.get("artifact_sha256"):
        raise ValueError("candidate residual profile is not bound to the candidate artifact")

    parent_enabling = parent.get("enabling_candidate") or {}
    parent_composition_depth = (
        int(parent_enabling.get("composition_depth", 0))
        if parent_enabling.get("status") == "active"
        else 0
    )
    composition_depth = parent_composition_depth + 1
    measured_node_legal = bool(
        node.get("artifact_state") == "materialized"
        and node.get("measurement_complete")
        and node.get("route_legal") is True
        and node.get("placed_legal") is True
        and not isinstance(node.get("unplaced_primitives"), bool)
        and node.get("unplaced_primitives") == 0
        and node.get("hold_legal") is True
        and node.get("pulse_legal") is True
    )
    qualification = evaluate_enabling_candidate(
        parent_profile=parent_profile,
        candidate_profile=candidate_profile,
        targeted_families=targeted_families,
        candidate_legal=(candidate_legal is True and measured_node_legal),
        strict_validation_passed=strict_validation_passed,
        promoted=promoted,
        regression_tolerance_ns=regression_tolerance_ns,
        local_improvement_epsilon_ns=local_improvement_epsilon_ns,
    )
    status = "rejected"
    if qualification["qualified"]:
        status = (
            "active" if composition_depth < int(max_composition_depth)
            else "depth_limit_reached"
        )
    record = {
        "version": 1,
        "status": status,
        "composition_depth": composition_depth,
        "max_composition_depth": int(max_composition_depth),
        "substrate_candidate_id": str(parent_id),
        "substrate_artifact_sha256": str(parent.get("artifact_sha256") or ""),
        "qualification": qualification,
        "retired_reason": "",
        "retired_by_candidate_id": None,
    }
    node["enabling_candidate"] = record
    pool = graph.setdefault("enabling_pool", _empty_enabling_pool())
    active = pool.setdefault("active_candidate_ids", [])
    retired = pool.setdefault("retired_candidate_ids", [])
    active[:] = [value for value in active if value != candidate_id]
    if status == "active":
        active.append(candidate_id)
        active.sort(key=candidate_numeric_id)
        while len(active) > int(pool_width):
            retire_enabling_candidate(
                graph, active[0], reason="enabling_pool_capacity", retired_by=None
            )
    elif qualification["qualified"]:
        if candidate_id not in retired:
            retired.append(candidate_id)
    return copy.deepcopy(record)


def retire_enabling_candidate(
    graph: dict,
    candidate_id: str,
    *,
    reason: str,
    retired_by: Optional[str],
) -> bool:
    pool = graph.setdefault("enabling_pool", _empty_enabling_pool())
    active = pool.setdefault("active_candidate_ids", [])
    if candidate_id not in active:
        return False
    active.remove(candidate_id)
    retired = pool.setdefault("retired_candidate_ids", [])
    if candidate_id not in retired:
        retired.append(candidate_id)
        retired.sort(key=candidate_numeric_id)
    node = (graph.get("nodes") or {}).get(candidate_id) or {}
    record = node.get("enabling_candidate") or {}
    record["status"] = "retired"
    record["retired_reason"] = str(reason)
    record["retired_by_candidate_id"] = retired_by
    node["enabling_candidate"] = record
    return True


def _is_ancestor(graph: dict, ancestor_id: str, candidate_id: str) -> bool:
    cursor = candidate_id
    for _ in range(len(graph.get("nodes") or {}) + 1):
        if cursor == ancestor_id:
            return True
        node = (graph.get("nodes") or {}).get(cursor)
        if node is None or node.get("parent_candidate_id") is None:
            return False
        cursor = str(node["parent_candidate_id"])
    return False


def retire_enabling_candidates_after_promotion(
    graph: dict, promoted_candidate_id: str
) -> list[str]:
    """Invalidate every old-incumbent substrate after global-best promotion."""
    pool = graph.setdefault("enabling_pool", _empty_enabling_pool())
    retired = []
    for candidate_id in list(pool.get("active_candidate_ids", [])):
        reason = (
            "composition_promoted"
            if _is_ancestor(graph, candidate_id, promoted_candidate_id)
            else "incumbent_changed"
        )
        if retire_enabling_candidate(
            graph,
            candidate_id,
            reason=reason,
            retired_by=promoted_candidate_id,
        ):
            retired.append(candidate_id)
            if candidate_id != promoted_candidate_id:
                node = (graph.get("nodes") or {}).get(candidate_id) or {}
                node["in_active_beam"] = False
                node["search_state"] = "pruned"
                graph["beam_candidate_ids"] = [
                    value
                    for value in graph.get("beam_candidate_ids", [])
                    if value != candidate_id
                ]
    return retired


def composition_seed_proposals(graph: dict) -> list[dict]:
    """Return bounded, deterministic measured substrate proposals."""
    proposals = []
    for candidate_id in (graph.get("enabling_pool") or {}).get(
        "active_candidate_ids", []
    ):
        node = (graph.get("nodes") or {}).get(candidate_id)
        if node is None or node.get("artifact_state") != "materialized":
            continue
        record = node.get("enabling_candidate") or {}
        if record.get("status") != "active":
            continue
        effects = record.get("qualification", {}).get("target_effects", [])
        improvements = [
            float(item["improvement_ns"])
            for item in effects
            if item.get("improvement_ns") is not None
        ]
        residual_families = record.get("qualification", {}).get(
            "candidate_profile", {}
        ).get("families", {})
        next_action_by_family = {
            "hard_memory": (
                "operator_rewrite", "memory_write_buffer_forward_v1"
            ),
            "constructive_accumulator": (
                "operator_rewrite", "constructive_accumulator_rebuild_v1"
            ),
        }
        eligible_next_actions = []
        for family_id, facts in sorted(
            residual_families.items(),
            key=lambda item: int(item[1].get("best_rank", 10**9)),
        ):
            action = next_action_by_family.get(family_id)
            if action is None or action[1] == node.get("target"):
                continue
            eligible_next_actions.append({
                "skill": action[0],
                "target": action[1],
                "residual_family": family_id,
                "best_rank": facts.get("best_rank"),
                "measured_worst_slack_ns": facts.get("worst_slack_ns"),
                "attempt_authority": "registered_action_gates_still_required",
            })
        proposals.append({
            "seed_candidate_id": candidate_id,
            "composition_depth": record.get("composition_depth"),
            "remaining_depth": (
                int(record.get("max_composition_depth", 2))
                - int(record.get("composition_depth", 0))
            ),
            "targeted_families": record.get("qualification", {}).get(
                "targeted_families", []
            ),
            "best_measured_local_improvement_ns": max(improvements, default=None),
            "eligible_next_actions": eligible_next_actions,
            "optimization_result": False,
        })
    proposals.sort(key=lambda item: (
        int(item["composition_depth"]),
        -float(item["best_measured_local_improvement_ns"] or 0.0),
        candidate_numeric_id(item["seed_candidate_id"]),
    ))
    return proposals


def resume_constraint_refresh_candidate_ids(graph: dict) -> list[str]:
    """Return every selectable legacy node missing immutable-root proof.

    Resume must not validate only the recorded global best.  A stale active
    beam member can become the next seed immediately after the best is rolled
    back, so every materialized selectable node needs the same placement and
    immutable timing-constraint proof before autonomous search continues.
    """
    nodes = graph.get("nodes") or {}
    ordered_ids = dict.fromkeys((
        graph.get("root_candidate_id"),
        *(graph.get("beam_candidate_ids") or []),
        graph.get("current_best_candidate_id"),
        *(
            candidate_id
            for candidate_id, node in nodes.items()
            if node.get("in_active_beam") is True
            or node.get("search_state") == "active"
        ),
    ))
    return [
        str(candidate_id)
        for candidate_id in ordered_ids
        if candidate_id in nodes
        and nodes[candidate_id].get("artifact_state") == "materialized"
        and (
            nodes[candidate_id].get("placed_legal") is not True
            or nodes[candidate_id].get("constraint_identity_preserved") is not True
        )
    ]


def consume_continuation(
    graph: dict, candidate_id: str, *, consumed_round: int, kind: str
) -> bool:
    """Durably consume a candidate's one protected continuation opportunity."""
    if kind not in {"mutation", "probe"}:
        raise ValueError(f"invalid continuation kind {kind!r}")
    node = (graph.get("nodes") or {}).get(candidate_id)
    if node is None:
        raise ValueError(f"unknown candidate {candidate_id!r}")
    continuation = node.setdefault("continuation", _empty_continuation())
    if continuation.get("consumed_round") is not None:
        return False
    continuation["consumed_round"] = int(consumed_round)
    continuation["consumed_kind"] = kind
    return True


def prune_beam(graph: dict, pruned_round: int) -> list[str]:
    """Select a bounded beam, pinning the current global best.

    Nodes removed from the active beam remain in ``nodes``. Their physical
    artifact state remains unchanged; ``search_state='pruned'`` records only
    search ineligibility.
    """
    width = int(graph["beam_width"])
    best_id = graph.get("current_best_candidate_id")
    nodes = graph.get("nodes", {})
    eligible = [
        node
        for node in nodes.values()
        if _beam_eligible(node)
        and not _expired_dominated_continuation(graph, node, int(pruned_round))
    ]
    selected: list[str] = []
    best = nodes.get(best_id)
    if best is not None and _materialized_beam_eligible(best):
        best["search_state"] = "active"
        if best not in eligible:
            eligible.append(best)
        selected.append(best_id)

    # Width one cannot hold both a distinct continuation and the pinned best.
    # For wider beams reserve one bounded slot before quality/diversity ranking.
    if width > len(selected):
        protected = [
            node
            for node in eligible
            if node["candidate_id"] != best_id
            and _continuation_available(graph, node, int(pruned_round))
        ]
        if protected:
            protected.sort(key=lambda node: (
                -int(node.get("created_round", 0)),
                candidate_numeric_id(node["candidate_id"]),
            ))
            selected.append(protected[0]["candidate_id"])
            protected[0].setdefault(
                "continuation", _empty_continuation()
            )["last_protected_round"] = int(pruned_round)

    for front in pareto_fronts(eligible):
        candidates = [node for node in front if node["candidate_id"] not in selected]
        remaining_slots = width - len(selected)
        if remaining_slots <= 0:
            break
        if len(candidates) <= remaining_slots:
            selected.extend(node["candidate_id"] for node in candidates)
            continue
        distances = crowding_distances(front)
        retention = graph.get("beam_retention") or {}
        minimum_common = int(retention.get("minimum_common_dimensions", 4))
        diversity_ready = bool(retention.get("physical_diversity_enabled") and selected)
        chosen = []
        while diversity_ready and candidates and len(chosen) < remaining_slots:
            retained = [graph["nodes"][value] for value in selected + chosen]
            ranked = []
            for node in candidates:
                pair_distances = [
                    physical_signature_distance(
                        node.get("physical_signature") or {},
                        other.get("physical_signature") or {},
                        minimum_common,
                    )
                    for other in retained
                ]
                if not pair_distances or any(value is None for value in pair_distances):
                    diversity_ready = False
                    break
                ranked.append((min(pair_distances), node))
            if not diversity_ready:
                break
            ranked.sort(key=lambda item: (
                -item[0],
                -distances[item[1]["candidate_id"]],
                -float(item[1]["fmax_mhz"]),
                float(item[1]["cumulative_lineage_runtime_s"]),
                candidate_numeric_id(item[1]["candidate_id"]),
            ))
            winner = ranked[0][1]
            chosen.append(winner["candidate_id"])
            candidates = [
                node for node in candidates
                if node["candidate_id"] != winner["candidate_id"]
            ]
        if diversity_ready:
            selected.extend(chosen)
        else:
            candidates.extend(
                graph["nodes"][candidate_id] for candidate_id in chosen
            )
            candidates.sort(
                key=lambda node: (
                    -distances[node["candidate_id"]],
                    -float(node["fmax_mhz"]),
                    float(node["cumulative_lineage_runtime_s"]),
                    candidate_numeric_id(node["candidate_id"]),
                )
            )
            selected.extend(
                node["candidate_id"] for node in candidates[:remaining_slots]
            )
        break

    selected_set = set(selected)
    previously_active = set(graph.get("beam_candidate_ids", []))
    pruned_ids: list[str] = []
    for candidate_id, node in graph.get("nodes", {}).items():
        node["in_active_beam"] = candidate_id in selected_set
        if candidate_id in selected_set:
            node["search_state"] = "active"
        elif not _materialized_beam_eligible(node):
            node["search_state"] = "ineligible"
        elif (
            candidate_id not in selected_set
            and candidate_id != best_id
            and (candidate_id in previously_active or _beam_eligible(node))
        ):
            node["search_state"] = "pruned"
            node["pruned_round"] = int(pruned_round)
            pruned_ids.append(candidate_id)
    graph["beam_candidate_ids"] = selected
    return sorted(pruned_ids, key=candidate_numeric_id)


def _beam_eligible(node: dict) -> bool:
    return (
        _materialized_beam_eligible(node)
        and node.get("search_state", "active") == "active"
    )


def _materialized_beam_eligible(node: dict) -> bool:
    return (
        bool(node.get("eligible_for_beam"))
        and bool(node.get("measurement_complete"))
        and node.get("artifact_state") == "materialized"
        and node.get("fmax_mhz") is not None
        and node.get("cumulative_lineage_runtime_s") is not None
    )


def _empty_continuation() -> dict:
    return {
        "version": 1,
        "consumed_round": None,
        "consumed_kind": "",
        "last_protected_round": None,
    }


def _physical_dimensions_key(node: dict) -> Optional[str]:
    signature = node.get("physical_signature") or {}
    dimensions = signature.get("dimensions") or {}
    if signature.get("status") != "complete" or not dimensions:
        return None
    return json.dumps(dimensions, sort_keys=True, separators=(",", ":"), default=str)


def _continuation_available(graph: dict, node: dict, current_round: int) -> bool:
    continuation = node.setdefault("continuation", _empty_continuation())
    if continuation.get("consumed_round") is not None:
        return False
    created_round = int(node.get("created_round", 0))
    if current_round < created_round or current_round >= created_round + 1:
        return False
    dimensions_key = _physical_dimensions_key(node)
    if dimensions_key is None:
        return False
    numeric_id = candidate_numeric_id(node["candidate_id"])
    for other in (graph.get("nodes") or {}).values():
        if candidate_numeric_id(other["candidate_id"]) >= numeric_id:
            continue
        if not _materialized_beam_eligible(other):
            continue
        if _physical_dimensions_key(other) == dimensions_key:
            return False
    return True


def _expired_dominated_continuation(
    graph: dict, node: dict, current_round: int
) -> bool:
    continuation = node.get("continuation") or {}
    protected_round = continuation.get("last_protected_round")
    if protected_round is None:
        return False
    protection_ended = (
        continuation.get("consumed_round") is not None
        or current_round > int(protected_round)
    )
    if not protection_ended:
        return False
    return any(
        other is not node
        and _materialized_beam_eligible(other)
        and dominates(other, node)
        for other in (graph.get("nodes") or {}).values()
    )


def migrate_graph(raw_graph: dict) -> dict:
    """Migrate serialized v1 graph state without inferring file deletion."""
    graph = copy.deepcopy(raw_graph or {})
    if not graph:
        return graph
    version = int(graph.get("version", 1))
    if version > GRAPH_VERSION:
        raise ValueError(f"unsupported candidate graph version {version}")
    if version == 1:
        for node in (graph.get("nodes") or {}).values():
            old_state = node.get("artifact_state", "missing")
            if old_state == "pruned":
                node["artifact_state"] = "materialized"
                node["search_state"] = "pruned"
            elif old_state == "invalid":
                node["artifact_state"] = "materialized"
                node["search_state"] = "ineligible"
            elif old_state == "materialized":
                node["search_state"] = (
                    "active" if node.get("in_active_beam") else "ineligible"
                )
            else:
                node["search_state"] = "ineligible"
            node.setdefault("artifact_sha256", "")
            node.setdefault("artifact_deleted_at", None)
            node.setdefault("artifact_delete_reason", "")
            node.setdefault("equivalence_method", None)
            node.setdefault("equivalence_checked_at", None)
            node.setdefault("equivalence_report_path", None)
            node.setdefault("equivalence_root_sha256", None)
            node.setdefault("equivalence_candidate_sha256", None)
            node.setdefault("submission_eligible", False)
        graph["version"] = 2
        version = 2
    if version == 2:
        for node in (graph.get("nodes") or {}).values():
            node.setdefault("physical_signature", {
                "version": 1,
                "status": "legacy_unavailable",
                "artifact_sha256": str(node.get("artifact_sha256", "")),
                "dimensions": {},
                "digest_sha256": "",
            })
        graph["version"] = 3
        version = 3
    if version == 3:
        for node in (graph.get("nodes") or {}).values():
            node.setdefault("placed_legal", None)
            node.setdefault("unplaced_primitives", None)
            if node.get("placed_legal") is not True:
                node["submission_eligible"] = False
                node["eligible_for_beam"] = False
                node["in_active_beam"] = False
                node["search_state"] = "ineligible"
        graph["beam_candidate_ids"] = [
            candidate_id
            for candidate_id in graph.get("beam_candidate_ids", [])
            if (graph.get("nodes") or {}).get(candidate_id, {}).get("placed_legal")
            is True
        ]
        graph["version"] = 4
        version = 4
    if version == 4:
        graph.setdefault("enabling_pool", _empty_enabling_pool())
        for node in (graph.get("nodes") or {}).values():
            node.setdefault("enabling_candidate", None)
        graph["version"] = 5
    graph.setdefault("beam_retention", {
        "version": 1,
        "physical_diversity_enabled": True,
        "minimum_common_dimensions": 4,
        "method": "greedy_max_min_within_truncated_pareto_front",
        "fallback": "nsga2_crowding",
        "continuation_reserve": "one_recent_round_when_width_allows",
    })
    graph["beam_retention"].setdefault(
        "continuation_reserve", "one_recent_round_when_width_allows"
    )
    for node in (graph.get("nodes") or {}).values():
        node.setdefault("continuation", _empty_continuation())
        node.setdefault("evidence", None)
        node.setdefault("eco_proof", None)
        node.setdefault("enabling_candidate", None)
    graph.setdefault("enabling_pool", _empty_enabling_pool())
    graph.setdefault("pending_gc_candidate_ids", [])
    return graph


def record_equivalence(
    node: dict,
    *,
    status: str,
    method: str,
    checked_at: float,
    root_sha256: str,
    candidate_sha256: str,
    report_path: Optional[str] = None,
) -> None:
    if status not in {"passed", "failed", "inconclusive"}:
        raise ValueError(f"invalid equivalence status {status!r}")
    node["functional_equivalence"] = status
    node["equivalence_method"] = str(method)
    node["equivalence_checked_at"] = float(checked_at)
    node["equivalence_report_path"] = str(report_path) if report_path else None
    node["equivalence_root_sha256"] = str(root_sha256)
    node["equivalence_candidate_sha256"] = str(candidate_sha256)
    node["submission_eligible"] = bool(
        status == "passed"
        and node.get("artifact_state") == "materialized"
        and node.get("eligible_for_beam")
        and node.get("placed_legal") is True
        and node.get("artifact_sha256") == candidate_sha256
    )


def submission_candidate_id(graph: dict) -> str:
    best_id = graph.get("current_best_candidate_id")
    best = (graph.get("nodes") or {}).get(best_id)
    if (
        best
        and best.get("submission_eligible")
        and best.get("placed_legal") is True
    ):
        return str(best_id)
    root_id = graph.get("root_candidate_id")
    root = (graph.get("nodes") or {}).get(root_id)
    if (
        root
        and root.get("submission_eligible")
        and root.get("placed_legal") is True
    ):
        return str(root_id)
    raise ValueError("candidate graph has no submission-eligible artifact")
