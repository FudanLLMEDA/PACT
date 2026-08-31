"""Compose private executable cards from public current-seed hypotheses."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any, MutableMapping

from .action_contracts import (
    build_action_hypothesis,
    contract_for,
    placement_containment_factors,
    placement_containment_opportunity,
)
from .action_parameterization import parameterize_action
from .capabilities import CapabilityError, resolve_skill_action
from .regarith_tools.common import object_digest
from .skills.operator_rewrite import operator_rewrite_executor_ready


_PUBLIC_FIXED_TARGET_SKILLS = {
    "phys_opt",
    "implementation_recipe",
    "critical_net_reroute",
    "selective_branch_reroute",
    "structure_relocation",
}


def _closure_distance(features: Mapping[str, Any]) -> float:
    """Return how far this seed is from its official period, saturating at 8x.

    A seed whose achieved period is a small multiple of the official one can be
    implemented against that official target directly.  One that misses by many
    periods cannot: the router never converges and the action spends the run
    without producing a candidate.  boom_soc misses by 12.21 periods and
    boom_soc_v2 by 7.26, and a direct implementation timed out on both at
    2632.1s and 2358.2s for no gain, while their recorded one-hour paths both
    begin by implementing under a temporary relaxed target and restoring the
    legal clock afterwards.

    Saturating at eight periods keeps designs that are close to closure
    untouched: rosetta_3d sits at 0.88 and amd_mini-isp at 0.44.
    """
    period = features.get("clock_period_ns")
    if period is None:
        period = features.get("clock_period")
    wns = features.get("wns_ns")
    if wns is None:
        wns = features.get("wns")
    for value in (period, wns):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
    if float(period) <= 0.0 or float(wns) >= 0.0:
        return 0.0
    return min(1.0, abs(float(wns)) / float(period) / 8.0)


def _regime_ranking_adjustment(
    skill: str, regime: str, features: Mapping[str, Any]
) -> float:
    """Prefer containment strength supported by this exact seed.

    Pblock hardness and place/route directives are independent choices.  A
    dispersed, low-utilization route wall can support hard containment, while
    a compact descendant should expose guidance first.  This changes public
    ranking only; the ordinary parameterization and executor gates still own
    the mutation.
    """
    closure_distance = _closure_distance(features)
    if skill == "clock_tighten":
        # The temporary-target mechanism exists for a seed that cannot be
        # implemented against its official period.  Nothing else in the score
        # measures that distance, so this card ranked 63 points below a direct
        # implementation on exactly the seeds where the direct one times out.
        #
        # Only this card is raised.  Charging the direct implementation the
        # same distance would be symmetric, but its card carries neither
        # `clock_period` nor `wns`, so that half would silently evaluate to
        # zero and the pair would never reorder.
        return 80.0 * closure_distance
    if skill == "fresh_place_route":
        def unit_feature(name: str, *, scale: float = 1.0) -> float:
            value = features.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return max(0.0, min(1.0, float(value) / scale))

        spread = max(
            unit_feature("spread_ratio"),
            unit_feature("fanout_live_wall_coverage"),
        )
        resource_pressure = unit_feature("lut_util_pct", scale=100.0)
        route_pressure = max(
            unit_feature("route_dominated_ratio"),
            unit_feature("avg_route_pct", scale=100.0),
        )
        # A low-utilization, compact seed first needs an ordinary implementation
        # control.  Increasing live dispersion and resource/route pressure moves
        # the preferred regime continuously toward the disruptive strategies.
        calm = (1.0 - resource_pressure) * (1.0 - spread)
        disruption = max(0.0, min(1.0, route_pressure * (1.0 - calm) + spread))
        regime_level = {
            "preserve_constraints": 0.35,
            "balanced": 0.65,
            "aggressive": 0.95,
        }.get(regime)
        if regime_level is None:
            return 0.0
        return 40.0 * (0.5 - abs(disruption - regime_level))
    if skill not in {"pblock", "pblock_sweep"}:
        return 0.0
    opportunity = placement_containment_opportunity(features)
    if regime == "hard":
        return 40.0 * opportunity - 10.0
    if regime == "soft":
        return 10.0 * opportunity
    if regime == "guidance":
        # Guidance remains available for compact walls, but route pressure by
        # itself must not make placement containment outrank a live structural
        # mechanism when the seed exposes no containment opportunity.
        return 20.0 * opportunity - 10.0
    return 0.0


def _public_target_label(skill: str, target: str) -> str:
    if skill in _PUBLIC_FIXED_TARGET_SKILLS:
        return target
    if target == "auto" or target.startswith("auto_"):
        return target
    return "current_seed_certified_object"


def _feedback_for_mechanism(
    feedback_by_skill: Mapping[str, Mapping[str, Any]],
    *,
    skill: str,
    target: str,
    mechanism_family: str,
) -> dict[str, Any]:
    """Share measured outcomes across skills implementing one mechanism.

    Skill names are executor choices, not independent optimization evidence.
    A weak fanout result from a Vivado primitive must therefore inform a
    RapidWright fanout card, while retaining only current-run advisory facts
    and never transferring target parameters or mutation authority.
    """
    matches: list[Mapping[str, Any]] = []
    for skill_feedback in feedback_by_skill.values():
        if not isinstance(skill_feedback, Mapping):
            continue
        family_feedback = skill_feedback.get("by_mechanism_family")
        candidate = (
            family_feedback.get(mechanism_family)
            if isinstance(family_feedback, Mapping) else None
        )
        if isinstance(candidate, Mapping):
            matches.append(candidate)
    if not matches:
        own = feedback_by_skill.get(skill)
        matches = [own] if isinstance(own, Mapping) else []

    exact_attempts = [
        copy.deepcopy(attempt)
        for item in matches
        for attempt in item.get("recent_attempts", [])
        if isinstance(attempt, Mapping)
    ]
    lineage_attempts = [
        copy.deepcopy(attempt)
        for item in matches
        for attempt in item.get("recent_lineage_attempts", [])
        if isinstance(attempt, Mapping)
    ]
    lineage_attempts.sort(
        key=lambda attempt: -int(attempt.get("lineage_distance") or 0)
    )
    # A broad fanout pass and a split of one particular net are different
    # hypotheses.  Broad success does not validate every explicit target, and
    # one bad explicit target must not suppress the broad primitive.  Retain
    # exact-target outcomes only for these cards; family sharing remains in
    # place for mechanisms whose executor variants are interchangeable.
    if mechanism_family == "fanout_replication" and (
        skill == "fanout_opt"
        or (skill == "phys_opt" and target == "AggressiveFanoutOpt")
    ):
        exact_attempts = [
            item for item in exact_attempts if str(item.get("target") or "") == target
        ]
        lineage_attempts = [
            item for item in lineage_attempts
            if str(item.get("target") or "") == target
        ]
    seed_sha = next(
        (str(item.get("seed_artifact_sha256")) for item in matches
         if item.get("seed_artifact_sha256")),
        "",
    )
    return {
        "binding": "exact_seed_plus_current_lineage_mechanism_advisory",
        "seed_artifact_sha256": seed_sha,
        "attempt_count": len(exact_attempts),
        "recent_attempts": exact_attempts[-6:],
        "lineage_attempt_count": len(lineage_attempts),
        "recent_lineage_attempts": lineage_attempts[-6:],
        "mechanism_family": mechanism_family,
        "feedback_scope": (
            "exact_fanout_target" if skill == "fanout_opt"
            else "broad_fanout" if mechanism_family == "fanout_replication"
            else "mechanism_family"
        ),
        "mutation_authority": False,
    }


def _target_specific_features(
    skill: str,
    target: str,
    features: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one explicit fanout card to only that target's live evidence."""
    scoped = copy.deepcopy(dict(features))
    if skill != "fanout_opt" or target == "auto" or target.startswith("auto_"):
        return scoped
    rows = features.get("top_actionable_nets")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return scoped
    selected = next(
        (
            row for row in rows
            if isinstance(row, Sequence)
            and not isinstance(row, (str, bytes))
            and len(row) >= 3
            and str(row[0]) == target
        ),
        None,
    )
    if selected is None:
        return scoped

    def number(index: int) -> float:
        value = selected[index] if len(selected) > index else 0.0
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else 0.0
        )

    fanout = max(0, int(number(1)))
    fanout_paths = max(0, int(number(2)))
    profile_paths = max(0, int(number(3)))
    sampled_fanout_paths = max(
        1, int(scoped.get("fanout_discovery_num_paths") or 0)
    )
    sampled_timing_paths = max(1, int(scoped.get("path_count") or 0))
    coverage = min(1.0, max(
        fanout_paths / sampled_fanout_paths,
        profile_paths / sampled_timing_paths,
    ))
    top_net_delays = [
        max(0.0, float(row[4]))
        for row in rows
        if isinstance(row, Sequence)
        and not isinstance(row, (str, bytes))
        and len(row) > 4
        and isinstance(row[4], (int, float))
        and not isinstance(row[4], bool)
    ]
    total_profiled_delay = sum(top_net_delays)
    delay_contribution = (
        max(0.0, number(4)) / total_profiled_delay
        if total_profiled_delay > 0.0 else 0.0
    )
    scoped.update({
        "family_max_actionable_fanout": scoped.get("max_actionable_fanout"),
        "family_fanout_live_wall_coverage": scoped.get("fanout_live_wall_coverage"),
        "max_actionable_fanout": fanout,
        "fanout_live_wall_coverage": coverage,
        "target_fanout": fanout,
        "target_critical_path_count": max(fanout_paths, profile_paths),
        "target_live_wall_coverage": coverage,
        "target_total_delay_ns": max(0.0, number(4)),
        "target_max_delay_ns": max(0.0, number(5)),
        "target_profiled_delay_contribution": min(1.0, delay_contribution),
        "fanout_evidence_scope": "exact_current_seed_target",
    })
    return scoped


def build_current_seed_action_cards(
    *,
    run_id: str,
    seed_artifact_sha256: str,
    telemetry_epoch: int,
    features: Mapping[str, Any],
    targets_by_skill: Mapping[str, Sequence[str]],
    cold_isolation: bool,
    same_run_feedback_by_skill: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return private menu cards and the complete public hypothesis audit.

    Operator rewrites are omitted here because their exact proof-bound cards
    are composed by ``action_menu.build_action_menu``. Prohibited replay/import
    entries remain visible in the hypothesis audit but never become cards.
    """
    cards: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    feedback_by_skill = same_run_feedback_by_skill or {}
    for skill in sorted(targets_by_skill):
        if skill == "operator_rewrite":
            continue
        for target in sorted(set(map(str, targets_by_skill[skill]))):
            target_features = _target_specific_features(skill, target, features)
            target_evidence_ready = not (
                skill == "fanout_opt"
                and target != "auto"
                and not target.startswith("auto_")
                and target_features.get("fanout_evidence_scope")
                != "exact_current_seed_target"
            )
            contract_family = contract_for(skill, target).family
            feedback = _feedback_for_mechanism(
                feedback_by_skill,
                skill=skill,
                target=target,
                mechanism_family=contract_family,
            )
            feedback["current_seed_residual"] = {
                key: copy.deepcopy(target_features.get(key))
                for key in (
                    "route_dominated_ratio",
                    "max_logic_levels",
                    "critical_lut_stage_ratio",
                    "critical_lut_route_coverage",
                    "spread_ratio",
                    "max_actionable_fanout",
                    "fanout_live_wall_coverage",
                    "max_single_net_live_wall_coverage",
                    "target_fanout",
                    "target_live_wall_coverage",
                    "target_profiled_delay_contribution",
                )
                if target_features.get(key) is not None
            }
            preliminary = build_action_hypothesis(
                run_id=run_id,
                skill=skill,
                target=target,
                seed_artifact_sha256=seed_artifact_sha256,
                telemetry_epoch=telemetry_epoch,
                features=target_features,
                executor_ready=target_evidence_ready,
                executor_blocker=(
                    "CURRENT_SEED_TARGET_EVIDENCE_UNAVAILABLE"
                    if not target_evidence_ready else ""
                ),
                same_run_feedback=feedback,
            )
            if preliminary["authorization_status"] != "authorized":
                audit.append(preliminary)
                continue
            target_ready = False
            target_blocker = ""
            target_cards = []
            for regime in preliminary["allowed_parameter_regimes"]:
                try:
                    packet = parameterize_action(
                        preliminary,
                        regime=regime,
                        features=target_features,
                    )
                    resolved = resolve_skill_action(
                        skill,
                        target,
                        packet["resolved_params"],
                        defaults={},
                        target_presets={},
                        cold_isolation=cold_isolation,
                    )
                except (CapabilityError, KeyError, TypeError, ValueError) as exc:
                    target_blocker = f"EXECUTOR_READINESS:{type(exc).__name__}:{exc}"
                    continue
                target_ready = True
                regime_adjustment = _regime_ranking_adjustment(
                    skill, regime, target_features
                )
                legality_digest = object_digest({
                    "authority": "current_seed_parameterization_and_capability_resolution",
                    "run_id": run_id,
                    "seed_artifact_sha256": seed_artifact_sha256,
                    "telemetry_epoch": int(telemetry_epoch),
                    "action_hypothesis_id": preliminary["action_id"],
                    "parameterization_digest": packet["packet_digest"],
                    "resolved_action_fingerprint": resolved.action_fingerprint,
                })
                public_live_evidence = copy.deepcopy(preliminary["live_evidence"])
                if skill == "fanout_opt":
                    target_incidence = target_features.get(
                        "target_live_wall_coverage"
                    )
                    target_delay = target_features.get(
                        "target_profiled_delay_contribution"
                    )
                    if isinstance(target_incidence, (int, float)) and not isinstance(
                        target_incidence, bool
                    ):
                        public_live_evidence["target_path_incidence"] = float(
                            target_incidence
                        )
                    if isinstance(target_delay, (int, float)) and not isinstance(
                        target_delay, bool
                    ):
                        public_live_evidence["target_delay_ownership"] = float(
                            target_delay
                        )
                    if (
                        "target_path_incidence" in public_live_evidence
                        and "target_delay_ownership" in public_live_evidence
                    ):
                        public_live_evidence["joint_target_wall_ownership"] = (
                            public_live_evidence["target_path_incidence"]
                            * public_live_evidence["target_delay_ownership"]
                        )
                elif skill == "phys_opt" and target == "AggressiveFanoutOpt":
                    for public_name, feature_name in (
                        ("route_pressure", "avg_route_pct"),
                        ("fanout_path_reach", "fanout_live_wall_coverage"),
                        (
                            "max_single_net_path_reach",
                            "max_single_net_live_wall_coverage",
                        ),
                    ):
                        value = target_features.get(feature_name)
                        if isinstance(value, (int, float)) and not isinstance(
                            value, bool
                        ):
                            public_live_evidence[public_name] = float(value)
                target_cards.append({
                    "kind": "physical_recipe",
                    "action_target": f"{skill}:{target}:{regime}",
                    "legality_digest": legality_digest,
                    "action": {
                        "action": "run_skill",
                        "skill": skill,
                        "target": target,
                        "params": copy.deepcopy(packet["resolved_params"]),
                    },
                    "display": {
                        "action_family": preliminary["family"],
                        "skill": skill,
                        "target": _public_target_label(skill, target),
                        "parameter_regime": regime,
                        "decision_confidence": preliminary["decision_confidence"],
                        "ranking_score": round(
                            float(preliminary["ranking_score"])
                            + regime_adjustment,
                            6,
                        ),
                        "basin_opportunity_signal": preliminary[
                            "basin_opportunity_signal"
                        ],
                        # How far this seed sits from its official period, as
                        # a share of eight periods.  The ranking already uses
                        # it; publishing it lets the planner see why a
                        # temporary-target card leads on a seed that cannot be
                        # implemented against the official clock directly.
                        "closure_distance_signal": round(
                            _closure_distance(target_features), 6
                        ),
                        "containment_opportunity_signal": preliminary[
                            "containment_opportunity_signal"
                        ],
                        # The signal is a product of these four, so one zero
                        # factor zeroes it and the signal alone cannot say
                        # which.  Publishing them makes a zero diagnosable.
                        "containment_opportunity_factors": (
                            placement_containment_factors(target_features)
                        ),
                        "search_role": preliminary["search_role"],
                        "lineage_mechanism_repeat": preliminary[
                            "lineage_mechanism_repeat"
                        ],
                        "ancestor_gain_consumed": preliminary[
                            "ancestor_gain_consumed"
                        ],
                        "scope_ranking_adjustment": preliminary[
                            "scope_ranking_adjustment"
                        ],
                        "regime_ranking_adjustment": round(
                            regime_adjustment, 6
                        ),
                        "applicability_signal": preliminary[
                            "applicability_signal"
                        ],
                        "applicability_basis": preliminary[
                            "applicability_basis"
                        ],
                        "why_now": preliminary["why_now"],
                        "live_evidence": public_live_evidence,
                        "expected_mechanism": preliminary["mechanism"],
                        "expected_wall_effect": preliminary["expected_wall_effect"],
                        "expected_next_wall": preliminary["expected_next_wall"],
                        "parameter_policy": list(preliminary["parameter_policy"]),
                        "risk": preliminary["risk"],
                        "estimated_runtime_s": preliminary["estimated_runtime_s"],
                        "resource_request": copy.deepcopy(
                            preliminary["resource_request"]
                        ),
                        "destructive_scope": preliminary["destructive_scope"],
                        "logical_structure_effect": preliminary[
                            "logical_structure_effect"
                        ],
                        "same_run_feedback": copy.deepcopy(
                            preliminary["same_run_feedback"]
                        ),
                        "same_run_feedback_ranking_adjustment": preliminary[
                            "same_run_feedback_ranking_adjustment"
                        ],
                        "parameterization_digest": packet["packet_digest"],
                        "exact_parameters_private": True,
                    },
                })
            if target_ready:
                audit.append(preliminary)
                cards.extend(target_cards)
            else:
                unavailable = build_action_hypothesis(
                    run_id=run_id,
                    skill=skill,
                    target=target,
                    seed_artifact_sha256=seed_artifact_sha256,
                    telemetry_epoch=telemetry_epoch,
                    features=target_features,
                    executor_ready=False,
                    executor_blocker=target_blocker or "EXECUTOR_NOT_READY",
                    same_run_feedback=feedback,
                )
                audit.append(unavailable)
    return cards, audit


_OPERATOR_COUNT_FIELDS = {
    "fixed_point_dsp_rebuild_v1": "fixed_point_product_hypothesis_count",
    "dsp_mul_two_operand_v1": "two_operand_product_hypothesis_count",
    "dsp_register_absorb_v1": "register_absorb_hypothesis_count",
    "dsp_pipeline_rebalance_v1": "pipeline_rebalance_hypothesis_count",
    "exact_control_to_data_state_equation_v1": (
        "control_to_data_state_equation_hypothesis_count"
    ),
    "bank_local_predicate_specialization_v1": (
        "banked_select_predicate_complete_contract_count"
    ),
    "registered_ram_boolean_midpoint_relocation_v1": (
        "banked_ram_boolean_midpoint_hypothesis_count"
    ),
    "registered_control_packet_localization_v1": (
        "banked_control_packet_hypothesis_count"
    ),
}

_OPERATOR_LIVE_WALL_FIELDS = {
    "bank_local_predicate_specialization_v1": (
        "banked_select_predicate_live_wall_covered_path_count"
    ),
    "registered_ram_boolean_midpoint_relocation_v1": (
        "banked_ram_boolean_midpoint_live_wall_covered_path_count"
    ),
    "registered_control_packet_localization_v1": (
        "banked_control_packet_live_wall_covered_path_count"
    ),
}

_OPERATOR_EXECUTOR_PREFLIGHT_FIELDS = {
    "fixed_point_dsp_rebuild_v1": "fixed_point_contract_set_executor_ready",
}

_FAMILY_PROBE_OPERATOR_TARGETS = frozenset({
    "multilevel_control_distribution_localization_v1",
    "exact_control_to_data_state_equation_v1",
})


def build_operator_probe_action_cards(
    *,
    run_id: str,
    seed_artifact_sha256: str,
    telemetry_epoch: int,
    public_probe: Mapping[str, Any],
    cold_isolation: bool,
    same_run_feedback: Mapping[str, Any] | None = None,
    design_lut_count: object = None,
) -> list[dict[str, Any]]:
    """Turn a complete exact-seed family probe into proof-gated menu cards."""
    common_ready = (
        public_probe.get("status") == "success"
        and public_probe.get("version") == 1
        and public_probe.get("fresh_rediscovery") is True
        and public_probe.get("read_only") is True
        and public_probe.get("read_only_after_restore") is True
        and public_probe.get("mutation_eligible") is True
        and public_probe.get("proof_status")
        == "measurement_probe_only_final_equivalence_required"
    )
    probe_kind = public_probe.get("kind")
    operator_class = public_probe.get("operator_class")
    if not common_ready or (probe_kind, operator_class) not in {
        (
            "multilevel_control_distribution_contract",
            "MULTILEVEL_REGISTERED_CONTROL_DISTRIBUTION",
        ),
        (
            "control_to_data_state_equation_contract",
            "REGISTERED_CONTROL_TO_DATA_STATE_EQUATION",
        ),
    }:
        return []
    families = public_probe.get("families")
    assessment = public_probe.get("action_assessment")
    proof = public_probe.get("proof")
    if not isinstance(assessment, Mapping) or not isinstance(proof, Mapping):
        return []
    required_proof_fields = (
        (
            "complete_boolean_dag_recovered",
            "exact_lut_truth_tables_and_boundary_sources_bound",
            "registered_sink_roles_recovered",
            "exact_names_are_private",
            "does_not_establish_positive_routed_qor",
        )
        if probe_kind == "multilevel_control_distribution_contract"
        else (
            "set_implies_enable_truth_table_exhaustive",
            "minimal_care_set_support_recovered",
            "clock_enable_init_and_observable_cycle_preserved",
            "exact_cell_names_are_private",
            "does_not_establish_positive_routed_qor",
        )
    )
    expected_target = (
        "multilevel_control_distribution_localization_v1"
        if probe_kind == "multilevel_control_distribution_contract"
        else "exact_control_to_data_state_equation_v1"
    )
    if not all(proof.get(field) is True for field in required_proof_fields):
        return []
    actions = assessment.get("candidate_actions")
    if not isinstance(families, list) or not isinstance(actions, list):
        return []
    family_by_ref: dict[str, Mapping[str, Any]] = {}
    for item in families:
        if not isinstance(item, Mapping) or not item.get("family_ref"):
            return []
        family_ref = str(item["family_ref"])
        if family_ref in family_by_ref:
            return []
        covered = item.get(
            "covered_path_count"
            if probe_kind == "multilevel_control_distribution_contract"
            else "top500_path_overlap_count",
            0,
        )
        if (
            not isinstance(covered, int)
            or isinstance(covered, bool)
        ):
            return []
        if covered <= 0:
            continue
        family_by_ref[family_ref] = item
    if not family_by_ref:
        return []
    action_refs: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping):
            return []
        params = action.get("params")
        if (
            action.get("skill") != "operator_rewrite"
            or action.get("target") != expected_target
            or action.get("target") not in _FAMILY_PROBE_OPERATOR_TARGETS
            or not isinstance(params, Mapping)
            or set(params) != {"family_ref"}
        ):
            return []
        family_ref = str(params.get("family_ref") or "")
        if family_ref not in family_by_ref:
            continue
        if family_ref in action_refs:
            return []
        action_refs.add(family_ref)
    if action_refs != set(family_by_ref):
        return []
    cards: list[dict[str, Any]] = []
    for action in actions:
        target = str(action.get("target") or "")
        params = action.get("params")
        if not operator_rewrite_executor_ready(target):
            return []
        family_ref = str(params.get("family_ref") or "")
        family = family_by_ref.get(family_ref)
        if not isinstance(family, Mapping):
            continue
        covered = family.get(
            "covered_path_count"
            if probe_kind == "multilevel_control_distribution_contract"
            else "top500_path_overlap_count",
            0,
        )
        facts = {
            "anonymous_family": family_ref,
            "complete_scope": "current_seed_complete_contract",
            "proof_status": "measurement_probe_final_equivalence_required",
            "live_wall_coverage": min(1.0, covered / 16.0),
            "live_wall_covered_path_count": covered,
            "executor_readiness": "registered_fail_closed_executor",
            "hypothesis_count": len(family_by_ref),
        }
        hypothesis = build_action_hypothesis(
            run_id=run_id,
            skill="operator_rewrite",
            target=target,
            seed_artifact_sha256=seed_artifact_sha256,
            telemetry_epoch=telemetry_epoch,
            features=facts,
            executor_ready=True,
            same_run_feedback=same_run_feedback,
        )
        if hypothesis["authorization_status"] != "authorized":
            return []
        packet = parameterize_action(
            hypothesis,
            regime="proved",
            features=facts,
            private_bindings={"family_ref": family_ref},
        )
        try:
            resolved = resolve_skill_action(
                "operator_rewrite",
                target,
                packet["resolved_params"],
                defaults={},
                target_presets={},
                cold_isolation=cold_isolation,
            )
        except CapabilityError:
            return []
        cards.append({
            "kind": "proof_gated_mutation",
            "action_target": f"operator_rewrite:{target}:{family_ref}",
            "legality_digest": object_digest({
                "authority": "current_seed_complete_family_probe",
                "run_id": run_id,
                "seed_artifact_sha256": seed_artifact_sha256,
                "telemetry_epoch": int(telemetry_epoch),
                "action_hypothesis_id": hypothesis["action_id"],
                "parameterization_digest": packet["packet_digest"],
                "resolved_action_fingerprint": resolved.action_fingerprint,
                "public_probe_digest": object_digest(dict(public_probe)),
            }),
            "action": {
                "action": "run_skill",
                "skill": "operator_rewrite",
                "target": target,
                "params": copy.deepcopy(packet["resolved_params"]),
            },
            "display": {
                "action_family": "operator_rewrite",
                "skill": "operator_rewrite",
                "target": target,
                "parameter_regime": "proved",
                "decision_confidence": hypothesis["decision_confidence"],
                "ranking_score": round(
                    float(hypothesis["ranking_score"])
                    - (
                        min(30.0, scaled_operator_runtime_s(
                            hypothesis["estimated_runtime_s"], design_lut_count
                        ) / 120.0)
                        - min(30.0, float(hypothesis["estimated_runtime_s"]) / 120.0)
                    ),
                    6,
                ),
                "why_now": (
                    "the exact current seed exposes a complete anonymous "
                    f"family covering {covered} live timing paths"
                ),
                "live_evidence": copy.deepcopy(facts),
                "expected_mechanism": hypothesis["mechanism"],
                "expected_wall_effect": hypothesis["expected_wall_effect"],
                "expected_next_wall": hypothesis["expected_next_wall"],
                "parameter_policy": list(hypothesis["parameter_policy"]),
                "risk": hypothesis["risk"],
                "estimated_runtime_s": scaled_operator_runtime_s(
                    hypothesis["estimated_runtime_s"], design_lut_count
                ),
                "resource_request": copy.deepcopy(hypothesis["resource_request"]),
                "destructive_scope": hypothesis["destructive_scope"],
                "same_run_feedback": copy.deepcopy(
                    hypothesis["same_run_feedback"]
                ),
                "same_run_feedback_ranking_adjustment": hypothesis[
                    "same_run_feedback_ranking_adjustment"
                ],
                "proof_boundary": "executor_must_prove_before_candidate_emission",
                "exact_parameters_private": True,
            },
        })
    return cards


def build_consumer_rewrite_probe_action_card(
    *,
    run_id: str,
    seed_artifact_sha256: str,
    telemetry_epoch: int,
    public_probe: Mapping[str, Any],
    cold_isolation: bool,
    same_run_feedback: Mapping[str, Any] | None = None,
    refusal: MutableMapping[str, Any] | None = None,
    design_lut_count: object = None,
) -> list[dict[str, Any]]:
    """Expose a fresh consumer contract as a measured, proof-gated rewrite."""
    assessment = public_probe.get("action_assessment")
    action = assessment.get("candidate_action") if isinstance(assessment, Mapping) else None
    params = action.get("params") if isinstance(action, Mapping) else None
    public = public_probe.get("public_contract")
    if not isinstance(public, Mapping):
        public = public_probe
    family_ref = str(public_probe.get("family_ref") or "")
    target = "consumer_driven_registered_arithmetic_rewrite_v1"
    coverage = public.get("current_wall_coverage") if isinstance(public, Mapping) else None
    semantic_candidates = public.get("semantic_candidates") if isinstance(public, Mapping) else None
    supported = [
        item for item in (semantic_candidates or [])
        if isinstance(item, Mapping)
        and item.get("state") == "supported_for_measurement_probe"
    ]
    # A recovery can succeed and still fail one of eighteen admission checks.
    # Returning an empty list for all of them records a mechanism that simply
    # never appeared, so name the first unmet check instead.
    admission = (
        ("status_not_success", public_probe.get("status") == "success"),
        ("kind_mismatch",
         public_probe.get("kind") == "consumer_driven_operator_contract"),
        ("not_fresh_rediscovery", public_probe.get("fresh_rediscovery") is True),
        ("not_read_only", public_probe.get("read_only") is True),
        ("proof_status_mismatch",
         public_probe.get("proof_status")
         == "live_consumer_contract_extracted_unproved"),
        ("assessment_absent", isinstance(assessment, Mapping)),
        ("assessment_not_executable",
         isinstance(assessment, Mapping)
         and assessment.get("status") == "executable"),
        ("candidate_action_absent", isinstance(action, Mapping)),
        ("action_skill_mismatch",
         isinstance(action, Mapping) and action.get("skill") == "operator_rewrite"),
        ("action_target_mismatch",
         isinstance(action, Mapping) and action.get("target") == target),
        ("action_params_absent", isinstance(params, Mapping)),
        ("family_ref_not_sequential", family_ref.startswith("sequential_")),
        ("coverage_absent", isinstance(coverage, Mapping)),
        ("worst_path_uncovered",
         isinstance(coverage, Mapping)
         and coverage.get("covers_worst_path") is True),
        ("no_covered_path",
         isinstance(coverage, Mapping)
         and int(coverage.get("covered_path_count") or 0) > 0),
        ("no_supported_semantic_candidate", bool(supported)),
        ("executor_not_ready", operator_rewrite_executor_ready(target)),
    )
    unmet = [name for name, ok in admission if not ok]
    if unmet:
        if refusal is not None:
            refusal["consumer_rewrite_card_refusal"] = unmet[0]
            refusal["consumer_rewrite_card_unmet"] = unmet[:6]
            refusal["consumer_rewrite_probe_status"] = str(
                public_probe.get("status") or ""
            )
            refusal["consumer_rewrite_proof_status"] = str(
                public_probe.get("proof_status") or ""
            )
        return []
    covered = int(coverage["covered_path_count"])
    resolved = max(covered, int(coverage.get("ordered_path_count") or 0), 1)
    primitive_counts = public.get("producer_primitive_type_counts") or {}
    producer_nodes = sum(
        int(value or 0) for value in primitive_counts.values()
    ) if isinstance(primitive_counts, Mapping) else 0
    semantic_recovery = public.get("semantic_recovery") or {}
    shell_nodes = int(
        semantic_recovery.get("primitive_shell_node_count") or 0
    ) if isinstance(semantic_recovery, Mapping) else 0
    structural_contraction = (
        max(0.0, min(1.0, 1.0 - shell_nodes / producer_nodes))
        if producer_nodes else 0.0
    )
    facts = {
        "anonymous_family": family_ref,
        "complete_scope": "current_seed_consumer_contract",
        "proof_status": "measurement_probe_final_equivalence_required",
        "live_wall_coverage": min(1.0, covered / resolved),
        "live_wall_coverage_ppm": int(1_000_000 * covered / resolved),
        "live_wall_covered_path_count": covered,
        "executor_readiness": "registered_fail_closed_executor",
        "hypothesis_count": len(supported),
        "pipeline_depth": int(public.get("pipeline_depth") or 0),
        "operator_class": str(supported[0].get("operator_class") or ""),
        "producer_primitive_count": producer_nodes,
        "retained_shell_primitive_count": shell_nodes,
        "structural_contraction_ratio": round(structural_contraction, 6),
    }
    hypothesis = build_action_hypothesis(
        run_id=run_id,
        skill="operator_rewrite",
        target=target,
        seed_artifact_sha256=seed_artifact_sha256,
        telemetry_epoch=telemetry_epoch,
        features=facts,
        executor_ready=True,
        same_run_feedback=same_run_feedback,
    )
    if hypothesis["authorization_status"] != "authorized":
        return []
    packet = parameterize_action(
        hypothesis,
        regime="proved",
        features=facts,
        private_bindings={"family_ref": family_ref},
    )
    try:
        resolved_action = resolve_skill_action(
            "operator_rewrite",
            target,
            packet["resolved_params"],
            defaults={},
            target_presets={},
            cold_isolation=cold_isolation,
        )
    except CapabilityError:
        return []
    return [{
        "kind": "proof_gated_mutation",
        "action_target": f"operator_rewrite:{target}:{family_ref}",
        "legality_digest": object_digest({
            "authority": "current_seed_consumer_measurement_probe",
            "run_id": run_id,
            "seed_artifact_sha256": seed_artifact_sha256,
            "telemetry_epoch": int(telemetry_epoch),
            "action_hypothesis_id": hypothesis["action_id"],
            "parameterization_digest": packet["packet_digest"],
            "resolved_action_fingerprint": resolved_action.action_fingerprint,
            "public_probe_digest": object_digest(dict(public_probe)),
        }),
        "action": {
            "action": "run_skill",
            "skill": "operator_rewrite",
            "target": target,
            "params": copy.deepcopy(packet["resolved_params"]),
        },
        "display": {
            "action_family": "operator_rewrite",
            "skill": "operator_rewrite",
            "target": target,
            "parameter_regime": "proved",
            "decision_confidence": hypothesis["decision_confidence"],
            "ranking_score": hypothesis["ranking_score"],
            "why_now": (
                "fresh consumer recovery identifies a cycle-exact operator "
                f"covering {covered} live timing paths"
            ),
            "live_evidence": copy.deepcopy(facts),
            "expected_mechanism": hypothesis["mechanism"],
            "expected_wall_effect": hypothesis["expected_wall_effect"],
            "expected_next_wall": hypothesis["expected_next_wall"],
            "parameter_policy": list(hypothesis["parameter_policy"]),
            "risk": hypothesis["risk"],
            # Sibling operator builders scale this by design size; leaving it
            # at the contract's static 300s understates a rewrite on a large
            # design several fold, and the execution-window gate then admits an
            # action that cannot finish.
            "estimated_runtime_s": scaled_operator_runtime_s(
                hypothesis["estimated_runtime_s"], design_lut_count
            ),
            "resource_request": copy.deepcopy(hypothesis["resource_request"]),
            "destructive_scope": hypothesis["destructive_scope"],
            "same_run_feedback": copy.deepcopy(hypothesis["same_run_feedback"]),
            "same_run_feedback_ranking_adjustment": hypothesis[
                "same_run_feedback_ranking_adjustment"
            ],
            "proof_boundary": "measurement_candidate_requires_final_equivalence",
            "exact_parameters_private": True,
        },
    }]


def scaled_operator_runtime_s(base_s: float, design_lut_count: object) -> float:
    """Return the estimate unchanged; design size does not predict this cost.

    This scaled a proof-gated rewrite by `243.9 * log2(luts) - 2218`, fitted
    over five points of which three were lower bounds recovered from overruns.
    Fitting a line through censored data as though it were exact biases it
    upward, and the two clean measurements taken since contradict the law's
    sign: vtr_mcml's consumer-driven rewrite completed in 562s and then 435s
    at about 46,000 LUTs, where the law predicts 1,560s and where a 15,117-LUT
    design had measured 958.5s.  Least squares over the completed measurements
    alone slopes downward, which is another way of saying design size explains
    nothing here -- the cost belongs to the transaction.

    Because the law only ever raised an estimate, its whole effect was to
    withhold actions.  vtr_mcml_v2 lost its consumer-driven rewrite to
    EXECUTION_WINDOW_UNAVAILABLE in every round after the first, needing a
    declared 1,692s against about 1,200s remaining, for work its sibling had
    just measured at 562s.

    The contract estimate stands on its own, and `_measured_runtime_floor_s`
    raises it the moment this run measures the action itself.
    """
    del design_lut_count
    return float(base_s)


def build_operator_preflight_action_cards(
    *,
    run_id: str,
    seed_artifact_sha256: str,
    telemetry_epoch: int,
    numeric_evidence: Mapping[str, Any],
    cold_isolation: bool,
    same_run_feedback: Mapping[str, Any] | None = None,
    design_lut_count: object = None,
    sampled_path_count: object = None,
) -> list[dict[str, Any]]:
    """Expose every live operator direction through its fail-closed executor.

    These cards authorize a discovery/proof attempt, not an unproved mutation.
    The operator executor must still recover the complete exact current-seed
    contract and pass its proof before producing a candidate.
    """
    try:
        sampled = float(sampled_path_count)
    except (TypeError, ValueError):
        sampled = 0.0
    if not math.isfinite(sampled) or sampled < 1.0:
        sampled = 16.0
    eligible: dict[str, dict[str, Any]] = {}
    for target, count_field in _OPERATOR_COUNT_FIELDS.items():
        # This target requires a concrete family_ref from its same-run private
        # probe handoff.  It is emitted above by build_operator_probe_action_cards;
        # a count-only card would reach the executor without a selector.
        if target == "exact_control_to_data_state_equation_v1":
            continue
        if not operator_rewrite_executor_ready(target):
            continue
        readiness_field = _OPERATOR_EXECUTOR_PREFLIGHT_FIELDS.get(target)
        if (
            readiness_field is not None
            and numeric_evidence.get(readiness_field) is not True
        ):
            continue
        count = numeric_evidence.get(count_field, 0)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            coverage_field = _OPERATOR_LIVE_WALL_FIELDS.get(target)
            covered = numeric_evidence.get(coverage_field, 0) if coverage_field else 0
            covered = (
                covered
                if isinstance(covered, int)
                and not isinstance(covered, bool)
                and covered > 0
                else 0
            )
            eligible[target] = {
                "anonymous_family": target,
                "complete_scope": (
                    "current_seed_complete_contract"
                    if coverage_field else "execution_preflight_required"
                ),
                "proof_status": (
                    "measurement_probe_final_equivalence_required"
                    if coverage_field else "execution_preflight_required"
                ),
                # Against the sample this run actually took, not a constant.
                # `analysis.profile_num_paths` is 40, so a family covering
                # sixteen critical paths read as complete coverage when it
                # covered forty percent -- and complete coverage is worth a
                # hundred points of applicability.
                "live_wall_coverage": min(1.0, covered / sampled),
                "live_wall_covered_path_count": covered,
                "executor_readiness": "registered_fail_closed_executor",
                "hypothesis_count": count,
            }
    for target, prefix in (
        ("constructive_accumulator_rebuild_v1", "constructive_accumulator"),
        ("memory_write_buffer_forward_v1", "memory_write_buffer_forward"),
    ):
        complete = numeric_evidence.get(f"{prefix}_complete_contract_count", 0)
        missing = numeric_evidence.get(f"{prefix}_missing_obligation_count", 1)
        covered = numeric_evidence.get(f"{prefix}_live_wall_covered_path_count", 0)
        blockers = numeric_evidence.get(f"{prefix}_nomination_blockers", [])
        if (
            isinstance(complete, int) and not isinstance(complete, bool)
            and complete > 0
            and isinstance(missing, int) and not isinstance(missing, bool)
            and missing == 0
            and isinstance(covered, int) and not isinstance(covered, bool)
            and covered > 0
            and blockers == []
        ):
            eligible[target] = {
                "anonymous_family": target,
                "complete_scope": "current_seed_complete_contract",
                "proof_status": "proved",
                "live_wall_coverage": min(1.0, covered / 300.0),
                "executor_readiness": "registered_fail_closed_executor",
                "hypothesis_count": complete,
            }

    cards = []
    semantic_mechanisms = {
        "constructive_accumulator_rebuild_v1": (
            "rebuild a freshly proved repeated registered accumulator family "
            "with an exact depth-reduced compressor"
        ),
        "memory_write_buffer_forward_v1": (
            "forward a freshly proved memory write buffer across its exact "
            "read-first consumer boundary"
        ),
    }
    semantic_runtime_estimates_s = {
        "constructive_accumulator_rebuild_v1": 1200.0,
        "memory_write_buffer_forward_v1": 600.0,
    }
    for target, facts in sorted(eligible.items()):
        hypothesis = build_action_hypothesis(
            run_id=run_id,
            skill="operator_rewrite",
            target=target,
            seed_artifact_sha256=seed_artifact_sha256,
            telemetry_epoch=telemetry_epoch,
            features=facts,
            executor_ready=True,
            same_run_feedback=same_run_feedback,
        )
        if hypothesis["authorization_status"] != "authorized":
            continue
        packet = parameterize_action(
            hypothesis,
            regime="proved",
            features=facts,
        )
        try:
            resolved = resolve_skill_action(
                "operator_rewrite",
                target,
                packet["resolved_params"],
                defaults={},
                target_presets={},
                cold_isolation=cold_isolation,
            )
        except CapabilityError:
            continue
        expected_mechanism = semantic_mechanisms.get(
            target, hypothesis["mechanism"]
        )
        base_runtime_s = float(
            semantic_runtime_estimates_s.get(
                target, hypothesis["estimated_runtime_s"]
            )
        )
        scaled_runtime_s = scaled_operator_runtime_s(
            base_runtime_s, design_lut_count
        )
        # `ranking_score` charged runtime from the contract constant, so a
        # rewrite on a 289,437-LUT design scored as costing what one on a
        # 3,149-LUT design costs.  Charge the difference on the formula's own
        # terms so the score reflects the estimate the card publishes.
        scaled_ranking_score = round(
            float(hypothesis["ranking_score"])
            - (
                min(30.0, scaled_runtime_s / 120.0)
                - min(30.0, base_runtime_s / 120.0)
            ),
            6,
        )
        cards.append({
            "kind": "proof_gated_mutation",
            "action_target": f"operator_rewrite:{target}:execution_preflight",
            "legality_digest": object_digest({
                "authority": "current_seed_operator_discovery_and_proof_attempt",
                "run_id": run_id,
                "seed_artifact_sha256": seed_artifact_sha256,
                "telemetry_epoch": int(telemetry_epoch),
                "action_hypothesis_id": hypothesis["action_id"],
                "parameterization_digest": packet["packet_digest"],
                "resolved_action_fingerprint": resolved.action_fingerprint,
                "numeric_evidence": facts,
            }),
            "action": {
                "action": "run_skill",
                "skill": "operator_rewrite",
                "target": target,
                "params": copy.deepcopy(packet["resolved_params"]),
            },
            "display": {
                "action_family": "operator_rewrite",
                "skill": "operator_rewrite",
                "target": target,
                "parameter_regime": "proved",
                "decision_confidence": "uncertain",
                "ranking_score": scaled_ranking_score,
                "why_now": (
                    f"current seed exposes {facts['hypothesis_count']} anonymous "
                    "families for an exact fail-closed recovery/proof attempt"
                ),
                "live_evidence": copy.deepcopy(facts),
                "expected_mechanism": expected_mechanism,
                "expected_wall_effect": expected_mechanism,
                "expected_next_wall": hypothesis["expected_next_wall"],
                "parameter_policy": list(hypothesis["parameter_policy"]),
                "risk": hypothesis["risk"],
                "estimated_runtime_s": scaled_runtime_s,
                "resource_request": copy.deepcopy(hypothesis["resource_request"]),
                "destructive_scope": hypothesis["destructive_scope"],
                "same_run_feedback": copy.deepcopy(
                    hypothesis["same_run_feedback"]
                ),
                "same_run_feedback_ranking_adjustment": hypothesis[
                    "same_run_feedback_ranking_adjustment"
                ],
                "proof_boundary": "executor_must_prove_before_candidate_emission",
                "exact_parameters_private": True,
            },
        })
    return cards


__all__ = [
    "build_consumer_rewrite_probe_action_card",
    "build_current_seed_action_cards",
    "build_operator_preflight_action_cards",
    "build_operator_probe_action_cards",
]
