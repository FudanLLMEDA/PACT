"""Derive bounded action parameters only from one current-seed hypothesis.

The LLM selects a typed regime. Exact numeric parameters are generated here
from current-run features and remain subject to the capability, proof,
provenance, executor-readiness and scheduler gates.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any, Optional

from .action_contracts import HYPOTHESIS_SCHEMA_VERSION, contract_for


SCHEMA_VERSION = "current-seed-action-parameterization-v1"


class ActionParameterizationError(ValueError):
    pass


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")).hexdigest()


def _number(features: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = features.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    value = float(value)
    return value if math.isfinite(value) else float(default)


def _integer(features: Mapping[str, Any], key: str, default: int = 0) -> int:
    return int(round(_number(features, key, float(default))))


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _breadth(features: Mapping[str, Any], regime: str, *, low: int, high: int) -> int:
    path_count = max(1, _integer(features, "path_count", 8))
    route_ratio = _bounded(_number(features, "route_dominated_ratio"), 0.0, 1.0)
    scale = {"local": 0.25, "preserve": 0.25, "gentle": 0.25,
             "guidance": 0.35, "soft": 0.5, "balanced": 0.5,
             "regional": 0.6, "proved": 0.5, "hard": 0.8,
             "aggressive": 1.0, "global": 1.0, "rebuild": 1.0,
             "preserve_constraints": 0.5}.get(regime, 0.5)
    derived = math.ceil(path_count * scale * (0.75 + route_ratio / 2.0))
    return max(low, min(high, derived))


def _candidate_count(regime: str, maximum: int) -> int:
    count = 1 if regime in {"local", "preserve", "gentle", "guidance", "proved"} else 2
    if regime in {"aggressive", "global", "hard", "rebuild"}:
        count = 4
    return max(1, min(maximum, count))


def _route_directives(regime: str) -> list[str]:
    if regime in {"local", "preserve", "gentle", "guidance"}:
        return ["Default"]
    if regime in {"balanced", "regional", "soft", "preserve_constraints"}:
        return ["Default", "Explore"]
    return ["Explore", "AggressiveExplore"]


def _fresh_implementation_intensity(features: Mapping[str, Any]) -> float:
    """Derive basin-search intensity only from the exact current seed.

    Route domination says whether a new implementation basin can address the
    live wall, while placement spread and fanout-wall coverage say how
    disruptive that search should be.  This value is recomputed after every
    promotion; it contains no parent-recipe or historical-order state.
    """
    route_ratio = _bounded(
        _number(features, "route_dominated_ratio"), 0.0, 1.0
    )
    route_share = _bounded(
        _number(features, "avg_route_pct") / 100.0, 0.0, 1.0
    )
    dispersion = max(
        _bounded(_number(features, "spread_ratio"), 0.0, 1.0),
        _bounded(_number(features, "fanout_live_wall_coverage"), 0.0, 1.0),
    )
    return max(route_ratio, route_share, dispersion)


def _fresh_route_directives(
    features: Mapping[str, Any], regime: str
) -> list[str]:
    """Prioritize registered directives by current-seed search intensity."""
    directives = _route_directives(regime)
    route_dominated_ratio = _bounded(
        _number(features, "route_dominated_ratio"), 0.0, 1.0
    )
    avg_route_pct = _bounded(_number(features, "avg_route_pct"), 0.0, 100.0)
    if route_dominated_ratio >= 0.75 and avg_route_pct >= 65.0:
        # Expose the two generic route-owned alternatives.  Their order is
        # still chosen continuously by the exact seed's search intensity.
        directives = ["AggressiveExplore", "HigherDelayCost", *directives]
    desired = _fresh_implementation_intensity(features)
    directive_intensity = {
        "Default": 0.25,
        "Explore": 0.50,
        "HigherDelayCost": 0.50,
        "AggressiveExplore": 1.00,
    }
    original_order = {directive: index for index, directive in enumerate(directives)}
    return sorted(
        directives,
        key=lambda directive: (
            abs(directive_intensity[directive] - desired),
            original_order[directive],
        ),
    )


def _pressure_factors(features: Mapping[str, Any], regime: str) -> list[float]:
    closure = abs(min(0.0, _number(features, "closure_ratio")))
    route_ratio = _bounded(_number(features, "route_dominated_ratio"), 0.0, 1.0)
    base = _bounded(0.02 + closure * 0.08 + route_ratio * 0.03, 0.02, 0.12)
    scale = {"gentle": 0.6, "preserve": 0.6, "balanced": 1.0,
             "preserve_constraints": 1.0, "aggressive": 1.6,
             "rebuild": 1.6}.get(regime, 1.0)
    first = _bounded(1.0 - base * scale, 0.70, 0.98)
    values = [round(first, 4)]
    if regime in {"balanced", "aggressive", "rebuild"}:
        second = _bounded(1.0 - base * scale * 1.5, 0.65, first - 0.01)
        if second < first:
            values.append(round(second, 4))
    # A third rung for the regimes that mean to push.  boom_soc's 96.909 row
    # spends 3,444s on one deep implementation; two rungs of about 1,100s
    # leave the sweep stopping at 0.71 of the achieved period with window to
    # spare, and it measured 73.56.  The ladder already ends itself the moment
    # a rung misses its own target, so the rung that cannot close costs the
    # attempt and nothing that was already won.
    if regime in {"aggressive", "rebuild"} and len(values) == 2:
        third = _bounded(1.0 - base * scale * 2.0, 0.55, values[-1] - 0.01)
        if third < values[-1]:
            values.append(round(third, 4))
    return values


def _fresh_pressure_factors(
    features: Mapping[str, Any], regime: str
) -> list[float]:
    """Prioritize pressure siblings from the re-profiled current seed."""
    factors = _pressure_factors(features, regime)
    if len(factors) < 2:
        return factors
    desired = _fresh_implementation_intensity(features)
    factor_intensity = {
        index: 0.5 + 0.5 * index / float(len(factors) - 1)
        for index in range(len(factors))
    }
    return [
        factor
        for index, factor in sorted(
            enumerate(factors),
            key=lambda item: (
                abs(factor_intensity[item[0]] - desired),
                item[0],
            ),
        )
    ]


def _fanout_window(features: Mapping[str, Any]) -> tuple[int, int]:
    raw = features.get("top_actionable_nets")
    fanouts = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                value = item[1]
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    fanouts.append(value)
    if not fanouts:
        # A design with no actionable fanout net publishes zero here, and a
        # present zero is not the absent case the default was written for, so
        # the window collapsed to its floor of two: fir_systolic then refused
        # to reroute its own worst-source net at fanout 96 against a budget of
        # 2.  Treat a non-positive measurement as absent.
        measured = _integer(features, "max_actionable_fanout", 0)
        maximum = max(2, measured if measured > 0 else 32)
        return max(2, maximum // 4), maximum
    fanouts.sort()
    lower = fanouts[max(0, len(fanouts) // 4 - 1)]
    return max(2, lower), max(lower, fanouts[-1])


def _derive_params(
    skill: str,
    target: str,
    regime: str,
    features: Mapping[str, Any],
    private_bindings: Mapping[str, Any],
    decision_context: Mapping[str, Any],
) -> dict[str, Any]:
    candidates = _candidate_count(regime, 8)
    path_small = _breadth(features, regime, low=4, high=20)
    path_medium = _breadth(features, regime, low=8, high=50)
    min_fanout, max_fanout = _fanout_window(features)
    spread_score = max(0.0, _number(features, "spread_score"))
    lut_util = _bounded(_number(features, "lut_util_pct", 50.0), 0.0, 100.0)

    if skill == "phys_opt":
        return {}
    if skill == "fanout_opt":
        target_fanout = max(2, _integer(features, "target_fanout", max_fanout))
        coverage = _bounded(
            _number(features, "target_live_wall_coverage"), 0.0, 1.0
        )
        delay_contribution = _bounded(
            _number(features, "target_profiled_delay_contribution"), 0.0, 1.0
        )
        # Appearing on many paths is not enough to justify a large split: a
        # ubiquitous control net may still own very little measured delay.
        # Scale disruption only from joint current-seed incidence and delay
        # ownership; no hard eligibility gate or historical outcome is used.
        impact = coverage * delay_contribution
        if target_fanout >= 256 and impact >= 0.50:
            split_factor = 4
        elif target_fanout >= 64 and impact >= 0.20:
            split_factor = 3
        else:
            split_factor = 2
        return {"split_factor": split_factor}
    if skill == "force_replicate":
        fanout_wall_coverage = _bounded(
            _number(features, "fanout_live_wall_coverage"), 0.0, 1.0
        )
        eligible_hf_count = max(1, _integer(features, "eligible_hf_count", 1))
        # Keep a broad action multi-net, but size it from the portion of the
        # current wall it can actually address.  Path sampling breadth is not
        # a replication-count target.
        covered_path_count = max(
            1, int(math.ceil(path_medium * fanout_wall_coverage))
        )
        max_nets = min(16, eligible_hf_count, covered_path_count)
        max_single_net_coverage = _bounded(
            _number(features, "max_single_net_live_wall_coverage"), 0.0, 1.0
        )
        independent_wall_owners = max_nets
        if max_single_net_coverage > 0.0:
            independent_wall_owners = max(
                1,
                int(math.ceil(fanout_wall_coverage / max_single_net_coverage)),
            )
        # The two coverages have different denominators: the numerator is the
        # share of the wall all high-fanout nets hold together, the denominator
        # the share its single largest net holds over every net.  Whenever the
        # dominant net's share exceeds the aggregate high-fanout share the
        # ratio is below one by construction and the count collapses to a
        # single net -- on exactly the designs where replication has the most
        # to do.  vexriscv_re-place_v2 measures 0.503 against 0.875 and
        # replicated one net for a WNS of -0.946 to -0.954, where its recorded
        # 424.4 MHz chain replicates two.  Where the wall carries fanout and
        # more than one net is eligible, replicate at least two.
        if eligible_hf_count >= 2 and fanout_wall_coverage > 0.0:
            independent_wall_owners = max(2, independent_wall_owners)
        independent_wall_owners = min(max_nets, independent_wall_owners)
        max_nets_candidates = sorted(
            {independent_wall_owners, max_nets}
        )
        route_directive = _route_directives(regime)[-1]
        # The skill's own rule: a post-route force replication only acts on a
        # source or control net once the design is unrouted, so net scope is
        # for one net at a time.  Asking for two under net scope returned "no
        # legal force-replication sibling routed successfully" on
        # vexriscv_re-place_v2, where one net alone had at least produced a
        # candidate.
        selective_unroute = (
            regime in {"local", "regional"}
            or fanout_wall_coverage < 0.25
        ) and independent_wall_owners <= 1
        return {
            "num_paths": path_medium,
            "max_nets": max_nets,
            "max_nets_candidates": max_nets_candidates,
            "endpoint_min_fanout": min_fanout,
            "fallback_min_fanout": max_fanout,
            "passes": 2 if max_fanout >= 128 else 1,
            "unroute_scope": "net" if selective_unroute else "global",
            # A broad card may still contain a small number of independent
            # live wall owners. Race one selective-unroute sibling from the
            # same seed so a global repair cannot destroy a good route basin.
            "unroute_scope_candidates": (
                ["net"]
                if selective_unroute
                else ["global", "net"]
            ),
            "route_directive": route_directive,
            "route_directives": [route_directive],
            # The contest objective is worst-path Fmax.  TNS cleanup can trade
            # the exact WNS wall away while improving aggregate slack.
            "route_tns_cleanup": False,
            "pre_place_post_place_opt": (
                regime not in {"local"} and fanout_wall_coverage >= 0.25
            ),
            "post_rep_phys_opt": "AggressiveFanoutOpt",
            "final_phys_opt": "Explore",
            "final_route": regime == "global",
        }
    if skill == "per_net_unroute":
        return {
            # Re-run the exact current-seed discovery breadth that authorized
            # this card. A narrower executor census can falsely refuse every
            # target even though the analyzer just certified candidates.
            "num_paths": max(
                1,
                min(100, _integer(features, "fanout_discovery_num_paths", 50)),
            ),
            "min_fanout": min(256, min_fanout),
            "max_fanout": min(512, max_fanout),
            "replication_passes": 1 if regime == "local" else 2,
        }
    if skill == "cell_replace":
        route_share = _bounded(_number(features, "avg_route_pct"), 0.0, 100.0)
        return {
            "detour_threshold": round(_bounded(1.0 + route_share / 100.0, 1.1, 2.5), 3),
            "num_paths": path_medium,
        }
    if skill == "pblock":
        utilization_target = _bounded(0.55 + lut_util / 300.0, 0.58, 0.82)
        return {
            "utilization_target": round(utilization_target, 3),
            "guidance_only": regime == "guidance",
            "is_soft": regime in {"guidance", "soft"},
            "release_after_place": regime == "guidance",
            "max_pblock_candidates": _candidate_count(regime, 6),
            # Containment strength is not implementation intensity.  Establish
            # one comparable basin under the official Default/Default flow;
            # a freshly profiled descendant may then nominate route or phys-opt
            # refinement as an independent action.
            "place_directive": "Default",
            "route_directive": "Default",
        }
    if skill == "pblock_sweep":
        regional_utilization_target = _bounded(
            0.60 + lut_util / 300.0, 0.60, 0.82
        )
        return {
            # One compact containment candidate plus one independently
            # partitioned sibling is enough to test this expensive mechanism
            # before returning routed feedback to the planner.
            "max_candidates": _candidate_count(regime, 2),
            "allow_hard_macros": bool(features.get("hard_macro_present")),
            "is_soft": regime in {"guidance", "soft"},
            "critical_cluster_paths": path_small,
            "clock_region_capacity_margin": round(
                1.0 / regional_utilization_target, 3
            ),
            "place_directive": "Default",
            "route_directive": "Default",
        }
    if skill == "critical_cluster_anchor":
        scale = max(1, min(12, int(math.ceil(math.sqrt(max(1.0, spread_score))))))
        return {
            "num_paths": path_medium,
            "max_cells": min(96, max(8, path_medium * 3)),
            "max_nets": min(48, max(4, path_medium * 2)),
            "bbox_pad_x": scale,
            "bbox_pad_y": min(24, scale * 2),
        }
    if skill == "lut_merge":
        return {"num_paths": path_small, "max_pins": min(32, path_small * 2)}
    if skill == "lut_pin_swap":
        return {
            "num_paths": path_small,
            "max_pins": min(20, max(4, path_small)),
            "max_cells": min(12, max(2, path_small // 2)),
            "max_iterations": candidates,
            "route_directive": _route_directives(regime)[-1],
            "sweep_candidates": True,
        }
    if skill == "path_local_lut_reflow":
        return {
            "num_paths": path_small,
            "max_cells": min(32, max(6, path_small * 2)),
            "max_candidates": _candidate_count(regime, 4),
            "max_iterations": 1 if regime == "local" else 2,
            "place_directive": "Quick" if regime == "local" else "Default",
            "route_directive": "eco_preserve" if regime == "local" else "Default",
        }
    if skill == "post_route_cleanup":
        # The primitive that opens the wall first, then the broad post-route
        # search every recorded one-hour flow ends with.
        #
        # This used to publish exactly one recipe and one iteration, which put
        # six of the skill's ten recipes out of reach -- including
        # `phys_aggr_explore`, the post-route `phys_opt AggressiveExplore` that
        # closes mini-isp at 413.223, optical-flow at 359.712, vtr_mcml_v2 at
        # 74.388, and ispd16 at 127.291.  ispd16 measured the cost of that:
        # `phys_critical_pin` alone returned +0.000 in 270s of a 2,431s window
        # and the round ended there.
        route_ratio = _number(features, "route_dominated_ratio")
        if regime == "preserve":
            opener = "phys_routing" if route_ratio < 0.5 else "phys_critical_pin"
        elif route_ratio >= 0.5:
            # A route-owned wall needs the directive contrast plus a general
            # physical search. Restrict routing_opt to the preserve regime;
            # using it for balanced cleanup made the stronger action label
            # execute a weak route-preserving backend.
            opener = "route_higher_tns_phys_explore"
        else:
            opener = "phys_explore"
        recipes = [opener, "phys_aggr_explore"]
        return {
            "recipes": recipes,
            "max_candidates": len(recipes),
            "max_iterations": 2,
        }
    if skill == "critical_net_reroute":
        # The target owns anonymous live-net discovery; breadth and the typed
        # routing mechanism still come from this seed and selected regime.
        # worst_source reroutes exactly one net -- the one that owns the wall.
        # Bounding that by a census of other nets is the wrong model and on a
        # design with no actionable nets it forbids every net, so leave the
        # skill's own configured bound in force for that target.
        return {
            "num_paths": 1 if target == "worst_source" else path_medium,
            "max_candidates": 1,
            **({} if target == "worst_source"
               else {"max_fanout": min(1024, max_fanout)}),
            "route_mode": "delay_group" if regime == "local" else "explore_group",
            "post_phys_opt": None if regime == "local" else "Explore",
        }
    if skill == "selective_branch_reroute":
        return {
            "max_branches": _candidate_count(regime, 4),
            "max_candidates": _candidate_count(regime, 6),
            "num_paths": min(64, path_medium),
            "min_detour_ratio": round(_bounded(1.0 + _number(features, "route_dominated_ratio") * 0.8, 1.05, 1.8), 3),
            "route_directives": _route_directives(regime),
        }
    if skill == "equivalent_source_remap":
        return {
            "max_candidates": _candidate_count(regime, 8),
            "min_distance": max(1, min(8, int(math.ceil(spread_score / 20.0)) or 1)),
        }
    if skill == "endpoint_bel_move":
        return {
            "num_paths": path_medium,
            "max_candidates": _candidate_count(regime, 20),
        }
    if skill == "hard_macro_move":
        distance = max(1, min(16, int(math.ceil(spread_score / 10.0)) or 1))
        return {
            "num_paths": path_medium,
            "max_candidates": _candidate_count(regime, 8),
            "max_site_distance": distance,
        }
    if skill == "structure_relocation":
        span = max(1, min(4, int(math.ceil(spread_score / 30.0)) or 1))
        offsets = [[x, 0] for x in range(-span, span + 1) if x]
        return {
            "structure_id": "auto",
            "offsets": offsets[:_candidate_count(regime, 8)],
            "max_candidates": _candidate_count(regime, 8),
            "route_directive": _route_directives(regime)[-1],
        }
    if skill == "fresh_place_route":
        route_directives = _fresh_route_directives(features, regime)
        if (
            bool(decision_context.get("lineage_mechanism_repeat"))
            and len(route_directives) > 1
        ):
            # The first current-profile directive has already produced the
            # direct ancestor.  A repeated basin decision must measure the
            # independent directive contrast instead of deterministically
            # rebuilding the same basin again.  The alternative is still
            # derived from this exact descendant and the fixed allowlist.
            route_directives = route_directives[1:] + route_directives[:1]
        return {
            "strategy_regime": regime,
            # One full place/route sibling is one Agent decision.  A legal
            # candidate must return through central measurement, promotion,
            # exact-descendant profiling and menu arbitration before another
            # fresh basin may consume the remaining wall clock.  Regime still
            # selects a current-seed implementation/directive family; the
            # official clock remains unchanged.  Temporary clock pressure is
            # owned by the separate clock_tighten action so a fresh basin
            # cannot silently relax a failing official constraint.
            "max_candidates": 1,
            "route_directives": route_directives,
            "route_tns_cleanup": True,
        }
    if skill == "clock_tighten":
        # One recipe, ExtraTimingOpt placement into AlternateCLBRouting.
        #
        # Splitting this by occupancy to match each record -- Default
        # placement and AggressiveExplore routing for the sparse designs,
        # as vtr_mcml_v2's row runs and as its record says its ExtraTimingOpt
        # contrast rows regressed -- did not reproduce: vtr_mcml_v2 measured
        # 70.79 that way against 73.51 with ExtraTimingOpt, while boom_soc was
        # unchanged at 73.56.  That record's Default placement comes with a
        # hard pblock ahead of it, and transcribing half of a flow loses.
        return {
            "factors": _pressure_factors(features, regime),
            "max_candidates": _candidate_count(regime, 3),
            "period_order": "conservative_first",
        }
    if skill == "implementation_recipe":
        common = {
            "strategy_regime": regime,
            "max_candidates": _candidate_count(regime, 4),
        }
        if target == "derived_pressure_replace_route_v1":
            return {
                **common,
                "pressure_factors": _pressure_factors(features, regime),
            }
        if target == "full_reimplementation_retime_v1":
            return {
                **common,
                "pressure_factors": _fresh_pressure_factors(features, regime),
                "route_directives": _fresh_route_directives(features, regime),
            }
        if target == "routed_critical_cleanup_v1":
            route_ratio = _bounded(
                _number(features, "route_dominated_ratio"), 0.0, 1.0
            )
            cleanup_recipe = {
                "preserve": (
                    "phys_critical_pin" if route_ratio >= 0.5 else "phys_routing"
                ),
                "balanced": (
                    "route_higher_tns_phys_routing"
                    if route_ratio >= 0.5 else "phys_explore"
                ),
                "rebuild": (
                    "route_aggr_tns_phys_explore"
                    if route_ratio >= 0.5 else "phys_aggr_explore"
                ),
            }[regime]
            return {**common, "cleanup_recipe": cleanup_recipe}
        raise ActionParameterizationError(
            f"unknown implementation_recipe target {target!r}"
        )
    if skill == "operator_rewrite":
        params = {"max_candidates": 1}
        fingerprint = private_bindings.get("family_fingerprint")
        if fingerprint is not None and target in {
            "registered_arithmetic_cone_v1", "dsp_srl_recurrence_cut_v1"
        }:
            params["family_fingerprint"] = fingerprint
        family_ref = private_bindings.get("family_ref")
        if family_ref is not None and target in {
            "proved_arithmetic_compressor_v1",
            "fixed_point_dsp_rebuild_v1",
            "registered_fir_data_cone_rewrite",
            "consumer_driven_registered_arithmetic_rewrite_v1",
            "constructive_accumulator_rebuild_v1",
            "exact_control_to_data_state_equation_v1",
            "ff_srl_boundary_bridge_v1",
            "memory_write_buffer_forward_v1",
            "multilevel_control_distribution_localization_v1",
            "registered_gf2_parity_reassociation_v1",
        }:
            params["family_ref"] = family_ref
        return params
    raise ActionParameterizationError(f"no parameterizer for action skill {skill!r}")


def parameterize_action(
    hypothesis: Mapping[str, Any],
    *,
    regime: str,
    features: Mapping[str, Any],
    private_bindings: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return a seed-bound parameter packet with no standalone authority."""
    if not isinstance(hypothesis, Mapping):
        raise ActionParameterizationError("action hypothesis must be an object")
    required = {
        "schema_version", "run_id", "seed_artifact_sha256", "telemetry_epoch",
        "action_id", "skill", "target", "authorization_status",
        "allowed_parameter_regimes",
    }
    missing = sorted(required - set(hypothesis))
    if missing or hypothesis.get("schema_version") != HYPOTHESIS_SCHEMA_VERSION:
        raise ActionParameterizationError(f"action hypothesis is incomplete: {missing}")
    if hypothesis.get("authorization_status") != "authorized":
        raise ActionParameterizationError("only an authorized hypothesis can be parameterized")
    regimes = hypothesis.get("allowed_parameter_regimes")
    if not isinstance(regimes, list) or regime not in regimes:
        raise ActionParameterizationError(
            f"parameter regime {regime!r} is not allowed; expected {regimes!r}"
        )
    skill = str(hypothesis["skill"])
    target = str(hypothesis["target"])
    contract = contract_for(skill, target)
    if not contract.cold_mutation_allowed:
        raise ActionParameterizationError(contract.unavailable_reason)
    params = _derive_params(
        skill,
        target,
        regime,
        features,
        dict(private_bindings or {}),
        hypothesis,
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(hypothesis["run_id"]),
        "seed_artifact_sha256": str(hypothesis["seed_artifact_sha256"]),
        "telemetry_epoch": int(hypothesis["telemetry_epoch"]),
        "action_hypothesis_id": str(hypothesis["action_id"]),
        "skill": skill,
        "target": target,
        "selected_regime": regime,
        "parameter_policy": list(contract.parameter_policy),
        "resolved_params": copy.deepcopy(params),
        "mutation_authority": False,
    }
    return {**body, "packet_digest": _digest(body)}


def derive_implementation_recipe_params(
    target: str,
    *,
    regime: str,
    features: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the typed recipe fields for a compatibility action card.

    Legacy menus do not carry a current-seed hypothesis packet, but their
    executable recipe fields must still come from the same current-seed
    parameterizer used by the provenance-bound menu.
    """
    contract = contract_for("implementation_recipe", target)
    if regime not in contract.regimes:
        raise ActionParameterizationError(
            f"parameter regime {regime!r} is not allowed for {target!r}"
        )
    return _derive_params(
        "implementation_recipe", target, regime, features, {}, {}
    )


__all__ = [
    "ActionParameterizationError",
    "SCHEMA_VERSION",
    "derive_implementation_recipe_params",
    "parameterize_action",
]
