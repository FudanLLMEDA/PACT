"""Current-seed action contracts shared by analysis, Decision and execution.

The contract registry describes optimization mechanisms; it does not authorize
mutation.  Authorization still requires an exact current-run seed binding,
executor readiness and any action-specific proof or frozen target certificate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence


SCHEMA_VERSION = "current-seed-action-contract-v1"
HYPOTHESIS_SCHEMA_VERSION = "current-seed-action-hypothesis-v1"


@dataclass(frozen=True, slots=True)
class ActionContract:
    family: str
    mechanism: str
    required_facts: tuple[str, ...]
    parameter_policy: tuple[str, ...]
    regimes: tuple[str, ...]
    risk: str
    estimated_runtime_s: float
    destructive_scope: str
    cpu_cores: int = 1
    memory_gib: float = 1.0
    exclusive_session: str = "vivado"
    cold_mutation_allowed: bool = True
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        if not self.family or not self.mechanism:
            raise ValueError("action contract family and mechanism are required")
        if self.risk not in {"low", "medium", "high"}:
            raise ValueError("action contract risk must be low, medium, or high")
        if not math.isfinite(self.estimated_runtime_s) or self.estimated_runtime_s <= 0:
            raise ValueError("estimated_runtime_s must be finite and positive")
        if not 1 <= self.cpu_cores <= 8:
            raise ValueError("action contract cpu_cores must be in 1..8")
        if not math.isfinite(self.memory_gib) or not 0 < self.memory_gib <= 32:
            raise ValueError("action contract memory_gib must be in (0, 32]")
        if self.cold_mutation_allowed and self.unavailable_reason:
            raise ValueError("available action contract cannot have unavailable_reason")
        if not self.cold_mutation_allowed and not self.unavailable_reason:
            raise ValueError("unavailable action contract requires a reason")


def _contract(
    family: str,
    mechanism: str,
    required_facts: Sequence[str],
    parameter_policy: Sequence[str],
    regimes: Sequence[str],
    risk: str,
    estimated_runtime_s: float,
    destructive_scope: str,
    *,
    cpu_cores: int = 1,
    memory_gib: float = 1.0,
    exclusive_session: str = "vivado",
    cold_mutation_allowed: bool = True,
    unavailable_reason: str = "",
) -> ActionContract:
    return ActionContract(
        family=family,
        mechanism=mechanism,
        required_facts=tuple(required_facts),
        parameter_policy=tuple(parameter_policy),
        regimes=tuple(regimes),
        risk=risk,
        estimated_runtime_s=float(estimated_runtime_s),
        destructive_scope=destructive_scope,
        cpu_cores=int(cpu_cores),
        memory_gib=float(memory_gib),
        exclusive_session=exclusive_session,
        cold_mutation_allowed=bool(cold_mutation_allowed),
        unavailable_reason=unavailable_reason,
    )


_SKILL_CONTRACTS: Mapping[str, ActionContract] = MappingProxyType({
    "phys_opt": _contract(
        "post_route_physical", "one typed routed physical-optimization primitive",
        ("route_share", "logic_depth", "fanout", "resource_use"),
        ("fixed_safe_backend_primitive", "scheduler_owned_timeout"),
        ("preserve", "balanced", "aggressive"), "medium", 600,
        "routed_physical_optimization", cpu_cores=8, memory_gib=24,
    ),
    "fanout_opt": _contract(
        "fanout_replication", "split one freshly certified critical fanout net",
        ("fanout", "critical_path_ownership"),
        ("current_seed_target_certificate", "current_seed_derived_split"),
        ("local", "balanced"), "medium", 1200, "rapidwright_netlist_eco",
        cpu_cores=8, memory_gib=24, exclusive_session="rapidwright+vivado",
    ),
    "force_replicate": _contract(
        "fanout_replication", "replicate current critical nets and repair routing",
        ("fanout", "critical_path_ownership", "route_share"),
        ("current_seed_target_certificate", "llm_selected_scope", "scheduler_owned_timeout"),
        ("local", "regional", "global"), "high", 1500, "selective_or_full_unroute",
        cpu_cores=8, memory_gib=24,
    ),
    "per_net_unroute": _contract(
        "critical_net_reroute", "surgically unroute and repair one live critical net",
        ("fanout", "critical_path_ownership", "route_share"),
        ("current_seed_target_certificate", "current_seed_derived_breadth"),
        ("local",), "medium", 900, "single_net_unroute", cpu_cores=8, memory_gib=24,
    ),
    "cell_replace": _contract(
        "local_placement", "replace route-detour cells on current critical paths",
        ("route_share", "critical_path_ownership"),
        ("current_seed_target_certificate", "current_seed_derived_breadth"),
        ("local", "balanced"), "medium", 1200, "rapidwright_cell_eco",
        cpu_cores=8, memory_gib=24, exclusive_session="rapidwright+vivado",
    ),
    "pblock": _contract(
        "placement_locality", "apply current-design locality guidance or containment",
        ("placement_spread", "congestion", "resource_use"),
        ("current_seed_region_discovery", "llm_selected_scope", "resource_feasibility"),
        ("guidance", "soft", "hard"), "high", 2400, "full_place_route",
        cpu_cores=8, memory_gib=28,
    ),
    "pblock_sweep": _contract(
        "placement_locality", "measure isolated current-design locality alternatives",
        ("placement_spread", "congestion", "resource_use"),
        ("current_seed_region_discovery", "resource_feasibility", "scheduler_owned_siblings"),
        ("guidance", "soft", "hard"), "high", 3000, "isolated_full_place_route_siblings",
        cpu_cores=8, memory_gib=28,
    ),
    "critical_cluster_anchor": _contract(
        "placement_locality", "anchor the live critical cluster around its current bbox",
        ("placement_spread", "critical_path_ownership"),
        ("current_seed_cluster_discovery", "current_seed_derived_scale"),
        ("local", "balanced"), "medium", 1200, "critical_cluster_place_route",
        cpu_cores=8, memory_gib=24,
    ),
    "lut_merge": _contract(
        "path_local_logic", "merge a freshly certified critical LUT topology",
        ("logic_depth", "critical_path_ownership"),
        ("current_seed_target_certificate", "semantic_readiness_gate"),
        ("local",), "high", 1500, "rapidwright_logic_eco",
        cpu_cores=8, memory_gib=24, exclusive_session="rapidwright+vivado",
    ),
    "lut_pin_swap": _contract(
        "path_local_lut_physical", "reassign legal commutative LUT pins on the live wall",
        ("critical_lut_route_coverage", "critical_path_ownership"),
        ("current_seed_swap_certificate", "current_seed_derived_breadth"),
        ("local", "balanced"), "medium", 1200, "rapidwright_lut_pin_eco",
        cpu_cores=8, memory_gib=24, exclusive_session="rapidwright+vivado",
    ),
    "path_local_lut_reflow": _contract(
        "path_local_lut_physical", "re-place and reroute a bounded current critical LUT path",
        ("critical_lut_route_coverage", "critical_path_ownership"),
        ("current_seed_path_certificate", "llm_selected_intensity"),
        ("local", "balanced"), "medium", 1200, "path_local_place_route",
        cpu_cores=8, memory_gib=24,
    ),
    "post_route_cleanup": _contract(
        "post_route_physical", "preserve the incumbent while applying one cleanup mechanism",
        ("route_share", "critical_path_ownership"),
        ("one_mechanism_per_task", "scheduler_owned_timeout"),
        ("preserve", "balanced"), "low", 900, "routed_cleanup",
        cpu_cores=8, memory_gib=20,
    ),
    "critical_net_reroute": _contract(
        "critical_net_reroute", "reroute a freshly frozen worst-source or boundary link",
        ("route_share", "critical_path_ownership"),
        ("current_seed_target_certificate", "current_seed_derived_window"),
        ("local", "balanced"), "medium", 1200, "critical_net_reroute",
        cpu_cores=8, memory_gib=24,
    ),
    "selective_branch_reroute": _contract(
        "critical_net_reroute", "reroute current high-detour branches only",
        ("route_share", "critical_path_ownership"),
        ("current_seed_branch_certificate", "current_seed_derived_breadth"),
        ("local", "balanced"), "medium", 1200, "rapidwright_branch_eco",
        cpu_cores=8, memory_gib=24, exclusive_session="rapidwright+vivado",
    ),
    "equivalent_source_remap": _contract(
        "critical_net_reroute", "remap to a currently proved equivalent source",
        ("route_share", "critical_path_ownership", "proof_status"),
        ("current_seed_equivalence_certificate", "current_seed_derived_distance"),
        ("local",), "medium", 1200, "rapidwright_source_remap",
        cpu_cores=8, memory_gib=24, exclusive_session="rapidwright+vivado",
    ),
    "endpoint_bel_move": _contract(
        "local_placement", "move a current critical endpoint to a certified legal BEL",
        ("placement_spread", "critical_path_ownership"),
        ("current_seed_target_certificate", "current_seed_derived_breadth"),
        ("local", "balanced"), "medium", 1200, "endpoint_bel_eco",
        cpu_cores=8, memory_gib=24,
    ),
    "hard_macro_move": _contract(
        "local_placement", "explore legal sites for a current critical hard macro",
        ("hard_macro", "placement_spread", "critical_path_ownership"),
        ("current_seed_target_certificate", "current_seed_derived_distance"),
        ("local", "balanced"), "high", 1800, "hard_macro_placement_eco",
        cpu_cores=8, memory_gib=28,
    ),
    "structure_relocation": _contract(
        "local_placement", "relocate a freshly analyzed carry or hard-macro structure",
        ("structure_report", "critical_path_ownership"),
        ("current_seed_structure_certificate", "current_seed_legal_offsets"),
        ("local", "balanced"), "high", 1800, "rapidwright_structure_eco",
        cpu_cores=8, memory_gib=28, exclusive_session="rapidwright+vivado",
    ),
    "fresh_place_route": _contract(
        "fresh_implementation", "search a new placement and routing basin",
        ("route_share", "logic_depth", "placement_spread", "resource_use"),
        ("llm_selected_strategy", "current_seed_derived_implementation_intensity",
         "official_clock_preserved", "one_candidate_per_decision"),
        ("preserve_constraints", "balanced", "aggressive"), "high", 3000,
        "full_unplace_unroute", cpu_cores=8, memory_gib=28,
    ),
    "clock_tighten": _contract(
        "fresh_implementation", "implement under pressure derived from current achieved period",
        ("clock_period", "wns", "route_share", "logic_depth"),
        ("llm_selected_regime", "current_seed_derived_periods", "scheduler_owned_timeout"),
        ("gentle", "balanced", "aggressive"), "high", 2400,
        "temporary_clock_full_place_route", cpu_cores=8, memory_gib=28,
    ),
    "implementation_recipe": _contract(
        "fresh_implementation", "compile a current-seed implementation task plan",
        ("clock_period", "wns", "route_share", "logic_depth", "placement_spread", "fanout"),
        ("llm_selected_strategy", "current_seed_parameterization", "task_graph_compilation"),
        ("preserve", "balanced", "rebuild"), "high", 3000,
        "dynamic_task_graph", cpu_cores=8, memory_gib=28,
    ),
    "operator_rewrite": _contract(
        "operator_rewrite", "apply one exact current-seed semantic rewrite",
        ("anonymous_family", "complete_scope", "proof_status", "live_wall_coverage", "executor_readiness"),
        ("exact_current_run_contract", "proof_owned_parameters", "scheduler_owned_timeout"),
        ("proved",), "high", 300, "proved_netlist_mutation",
        cpu_cores=8, memory_gib=28, exclusive_session="rapidwright+vivado",
    ),
    "custom": _contract(
        "probe", "typed read-only diagnostic probe",
        ("explicit_missing_fact",), ("registered_read_only_tool",), ("probe",),
        "low", 300, "read_only", cpu_cores=1, memory_gib=2,
        exclusive_session="read_only_probe", cold_mutation_allowed=False,
        unavailable_reason="CUSTOM_MUTATION_REQUIRES_REGISTERED_ACTION",
    ),
    "checkpoint_import": _contract(
        "prohibited", "offline checkpoint comparison only",
        (), (), (), "high", 60, "foreign_artifact_import",
        cold_mutation_allowed=False,
        unavailable_reason="NO_REPLAY_FOREIGN_CHECKPOINT_IMPORT_FORBIDDEN",
    ),
    "semantic_replay": _contract(
        "prohibited", "offline historical replay audit only",
        (), (), (), "high", 60, "historical_replay",
        cold_mutation_allowed=False,
        unavailable_reason="NO_REPLAY_SEMANTIC_REPLAY_FORBIDDEN",
    ),
})


_PHYS_OPT_TARGETS: Mapping[str, tuple[str, str, str]] = MappingProxyType({
    "Default": ("general routed physical cleanup", "route_share", "preserve"),
    "Explore": ("explore general routed physical cleanup", "route_share", "balanced"),
    "AggressiveExplore": ("aggressively explore routed physical cleanup", "route_share", "aggressive"),
    "AggressiveFanoutOpt": ("repair current fanout-owned paths", "fanout", "aggressive"),
    "AddRetime": ("retime current deep sequential paths", "logic_depth", "balanced"),
    "equ_drivers": ("merge currently equivalent routed drivers", "proof_status", "preserve"),
    "routing_critical_pin": ("repair routing and input-pin delay on the live wall", "route_share", "local"),
    "placement_critical_cell": ("move current timing-critical cells", "placement_spread", "local"),
    "clock_opt": ("repair a current clock-owned wall", "clock_path_ownership", "preserve"),
})


def contract_for(skill: str, target: str = "auto") -> ActionContract:
    """Return the complete mechanism contract for one registered action."""
    try:
        base = _SKILL_CONTRACTS[str(skill)]
    except KeyError as exc:
        raise KeyError(f"unregistered action contract for skill {skill!r}") from exc
    if skill == "implementation_recipe":
        try:
            family, mechanism, required_facts, parameter_policy, scope = {
                "derived_pressure_replace_route_v1": (
                    "fresh_implementation",
                    (
                        "re-place and re-route under current-seed-derived temporary "
                        "clock pressure with critical-net replication and alternate "
                        "CLB routing"
                    ),
                    ("clock_period", "wns", "route_share", "logic_depth"),
                    (
                        "current_seed_derived_periods",
                        "critical_net_replication",
                        "alternate_clb_routing",
                    ),
                    "full_unplace_unroute",
                ),
                "full_reimplementation_retime_v1": (
                    "fresh_implementation",
                    (
                        "fully re-place and re-route with Explore, ExtraTimingOpt, "
                        "bounded retiming, and an alternate-route fallback"
                    ),
                    ("route_share", "logic_depth", "placement_spread", "resource_use"),
                    (
                        "current_seed_parameterization",
                        "bounded_retiming",
                        "alternate_route_fallback",
                    ),
                    "full_unplace_unroute",
                ),
                "routed_critical_cleanup_v1": (
                    "post_route_physical",
                    (
                        "retain the routed incumbent while applying clock, critical-pin, "
                        "critical-cell, and placement cleanup with one bounded reroute"
                    ),
                    ("route_share", "critical_path_ownership"),
                    (
                        "current_seed_cleanup_selection",
                        "one_bounded_reroute",
                    ),
                    "routed_cleanup_with_bounded_reroute",
                ),
            }[str(target)]
        except KeyError as exc:
            raise KeyError(
                f"unregistered implementation_recipe contract for target {target!r}"
            ) from exc
        return ActionContract(
            family=family,
            mechanism=mechanism,
            required_facts=required_facts,
            parameter_policy=parameter_policy,
            regimes=base.regimes,
            risk=base.risk,
            estimated_runtime_s=base.estimated_runtime_s,
            destructive_scope=scope,
            cpu_cores=base.cpu_cores,
            memory_gib=base.memory_gib,
            exclusive_session=base.exclusive_session,
        )
    if skill != "phys_opt":
        return base
    try:
        mechanism, fact, default_regime = _PHYS_OPT_TARGETS[str(target)]
    except KeyError as exc:
        raise KeyError(f"unregistered phys_opt contract for target {target!r}") from exc
    return ActionContract(
        family=(
            "fanout_replication" if target == "AggressiveFanoutOpt"
            else "local_placement" if target == "placement_critical_cell"
            else base.family
        ),
        mechanism=mechanism,
        required_facts=(fact,),
        parameter_policy=base.parameter_policy,
        regimes=(default_regime,),
        risk=("high" if target in {"AggressiveExplore", "AddRetime"} else base.risk),
        estimated_runtime_s=base.estimated_runtime_s,
        destructive_scope=base.destructive_scope,
        cpu_cores=base.cpu_cores,
        memory_gib=base.memory_gib,
        exclusive_session=base.exclusive_session,
    )


def registered_contract_skills() -> tuple[str, ...]:
    return tuple(sorted(_SKILL_CONTRACTS))


def validate_contract_coverage(skills: Sequence[str]) -> None:
    missing = sorted(set(map(str, skills)) - set(_SKILL_CONTRACTS))
    extra = sorted(set(_SKILL_CONTRACTS) - set(map(str, skills)))
    if missing or extra:
        raise ValueError(f"action contract coverage mismatch missing={missing} extra={extra}")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _feature_value(features: Mapping[str, Any], fact: str) -> Any:
    aliases = {
        "clock_period": "clock_period_ns",
        "wns": "wns_ns",
        "route_share": "avg_route_pct",
        "logic_depth": "max_logic_levels",
        "placement_spread": "spread_ratio",
        "fanout": "max_actionable_fanout",
        "resource_use": "lut_util_pct",
        "hard_macro": "hard_macro_present",
        "dsp_use": "dsp_count",
        "bram_use": "bram_count",
    }
    key = aliases.get(fact, fact)
    return features.get(key)


def _fact_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, (str, Sequence, Mapping)):
        return bool(value)
    return False


def _feature_number(features: Mapping[str, Any], key: str) -> float:
    value = features.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) else 0.0


# Independent-lineage priors from the anonymised effective-operator catalog
# (`effective_operator_paths/catalog.json`, dcp and benchmark names omitted,
# designs carried as D01-D14).  A mechanism observed to work on four unrelated
# lineages is the strongest generalisation evidence this project holds; one
# observed on a single lineage is an n=1 fit and earns nothing here.
#
# The catalog's own family index:
#   F01 consumer_driven_observable_state_reduction        4  strong
#   F02 directed_ring_owner_with_control_closure          4  strong
#   F03-F09                                               1  conditional
_INDEPENDENT_LINEAGE_COUNT: Mapping[str, int] = MappingProxyType({
    # F01 -- observable quotient/root and divider slices, GF(2) projection,
    # observable-closure trimming.
    "consumer_driven_registered_arithmetic_rewrite_v1": 4,
    # F02 -- directed ring owners closed over their global control.
    "multilevel_control_distribution_localization_v1": 4,
    # F03/F04/F05 -- one lineage each.
    "fixed_point_dsp_rebuild_v1": 1,
    "registered_arithmetic_cone_v1": 1,
    "dsp_srl_recurrence_cut_v1": 1,
})


# Targets whose contract declares exactly the cone it covers, so the ranking
# score's `coverage` term already prices their partial reach.
_CONE_SCOPED_CONTRACT_TARGETS = frozenset({
    "consumer_driven_registered_arithmetic_rewrite_v1",
})


def independent_lineage_credit(target: object, applicability: float = 1.0) -> float:
    """Return the ranking credit a mechanism's cross-design evidence earns.

    Scored on the same hundred-point scale as applicability and multiplied by
    it, because the two answer different halves of one question: the lineage
    count says the mechanism carries to designs unrelated to this one, and the
    seed's own applicability says whether it bites here.  Evidence that it
    generalises is not evidence that it applies.

    Added rather than multiplied, it substituted for seed support: on
    corescore an operator rewrite with an applicability of about 0.28 rose
    from 105.98 to 206 and displaced the pblock that had just measured
    +76.29 MHz there.  Scaled, that same card earns 28 and stays under it,
    while vtr_mcml's consumer-driven rewrite -- applicability about 1.0, base
    186.4 -- keeps the full credit and the 131.30 MHz it produced.

    Scaling is right only where the contract claims more scope than it covers,
    which is corescore's case: a `current_seed_complete_contract` that applies
    to 28% of the live wall.  A consumer-driven contract declares the cone it
    owns and nothing else -- it publishes `current_seed_consumer_contract` and
    its own `live_wall_coverage` -- and the hundred points of `coverage` in the
    ranking score already charge for that share.  Scaling there bills the same
    partial cone twice: on vtr_mcml_v2 it left the rewrite at 120.33, under a
    LUT merge that scored 174.44 and failed in 21s with no mergeable cone, and
    under two more ECOs that measured +0.010 and +0.000, while its family is
    the one the offline record rides from 69.3 to 138.4 MHz.  It costs 300s of
    a 2,900s window and was never once selected in five rounds.
    """
    name = str(target or "")
    count = _INDEPENDENT_LINEAGE_COUNT.get(name, 1)
    lineage = max(0.0, min(1.0, (count - 1) / 3.0))
    if name in _CONE_SCOPED_CONTRACT_TARGETS:
        return 100.0 * lineage
    return 100.0 * lineage * max(0.0, min(1.0, float(applicability)))


def placement_basin_opportunity(features: Mapping[str, Any]) -> float:
    """Measure current-seed room for a global locality builder.

    Route domination, low fabric utilization, and dispersed critical paths
    jointly indicate that local routed polish is unlikely to substitute for a
    new placement basin.  The signal is recomputed from every exact seed and
    contains no benchmark identity, historical region, or action-order state.
    """
    route_pressure = max(
        min(1.0, abs(_feature_number(features, "avg_route_pct")) / 100.0),
        min(1.0, max(0.0, _feature_number(features, "route_dominated_ratio"))),
    )
    utilization = min(
        1.0, max(0.0, _feature_number(features, "lut_util_pct")) / 80.0
    )
    # An empty device is where a new basin has the most room to be different,
    # so this is the plain complement.  Peaking it at moderate occupancy was
    # aimed at 3d-rendering, where the plain reading put fresh_place_route 15
    # points above the bank-local predicate rewrite its recorded run took first
    # for +70.5 MHz; but the peak also collapsed it on
    # rosetta_digit-recognition, whose recorded 419.11 MHz comes from
    # fresh_place_route alone on round 0 -- +50.74 MHz in one action -- and
    # which reached only 374.53 twice with the peak in place.  Occupancy is not
    # what separates them: both sit near 5% and both cross about 110 tiles.
    # Two readings of the same room, and a design has whichever is larger.
    # The peak says moderate occupancy has the most room to re-implement, which
    # is what boom_soc needs at 72% -- the plain complement drops its clock
    # tighten 48.5 points off the top of its menu.  But at 0.6% occupancy the
    # peak reads an almost-empty device as having no room at all, and that cost
    # vexriscv_re-place its clock tighten: scored 130.45 with a -24.18 scope it
    # never ran, where the mechanism is worth +66.4 MHz there and the run fell
    # from 463.39 to 417.01.  Taking the maximum can only raise the signal, so
    # no design loses a clock tighten it already had.
    # An almost-empty device has room for a new basin -- unless the wall is
    # owned by fanout, in which case the fix is the fanout and not the
    # placement.  Sparse occupancy alone put clock_tighten ahead of the fanout
    # repair on amd_mini-isp, which opens with phys_opt AggressiveFanoutOpt for
    # +68.3 MHz (307.1 -> 375.4) and closes at 443.07; leading with the basin
    # instead ends at 424.81 and then spends four consecutive phys_opt actions
    # for +0.000 each.  vexriscv_re-place is equally sparse and needs the
    # rescue -- without it the peak reads its 0.6% occupancy as no room at all
    # and the run falls from 463.4 to 417.01 -- but its wall is not the fanout.
    # Scale the rescue by the wall the fanout does not own, so the two separate
    # on who owns the wall rather than on how empty the device is.
    fanout_wall_coverage = min(1.0, max(0.0, _feature_number(
        features, "fanout_live_wall_coverage"
    )))
    sparse_rescue = (1.0 - utilization) * (1.0 - fanout_wall_coverage)
    placement_headroom = max(
        4.0 * utilization * (1.0 - utilization), sparse_rescue
    )
    # What separates them is who owns the wall.  One net owns the whole of
    # 3d-rendering's live wall, so the fix is that net; digit-recognition's
    # heaviest net owns 0.3 of a wall spread over 116 tiles at three logic
    # levels, so the fix is the placement.  Containment already reads this
    # fact, and a basin is the mechanism it argues for or against most
    # directly.
    single_net_ownership = min(1.0, max(0.0, _feature_number(
        features, "max_single_net_live_wall_coverage"
    )))
    dispersion = min(
        1.0, 3.0 * max(0.0, _feature_number(features, "spread_ratio"))
    )
    logic_depth = min(
        1.0, max(0.0, _feature_number(features, "max_logic_levels")) / 24.0
    )
    # A compact sampled path can still pay a large locality tax.  Requiring it
    # to cross the coarse device-span threshold made the basin signal collapse
    # to zero precisely on low-utilization, route-dominated designs.  Preserve
    # explicit dispersion, but also admit the route share left after a smooth
    # logic-depth discount as independent current-seed basin evidence.
    route_locality_tax = route_pressure * max(0.0, 1.0 - 0.5 * logic_depth)
    locality_evidence = max(dispersion, route_locality_tax)
    return min(1.0, route_pressure * placement_headroom * locality_evidence)


def local_polish_discount_signal(features: Mapping[str, Any]) -> float:
    """Measure how much a local polish is worth less than re-implementing.

    Third of the three questions the one basin signal used to answer.  The
    peaked occupancy term belongs to `placement_basin_opportunity`, which asks
    how much room a pressure re-implementation has -- boom_soc closes at 73.56
    MHz from a clock tighten that needs it.  The discount asks something else:
    the emptier the device, the more a different implementation could differ
    from this one, so occupancy enters as the plain complement.

    Reading the peaked form here cost ispd16_example2 its round.  Its
    post-route cleanup, which its recorded 120.61 MHz run takes first, fell
    from 186.10 to 175.17 and under a LUT pin swap at 182.35 that spent the
    rest of the window.
    """
    route_pressure = max(
        min(1.0, abs(_feature_number(features, "avg_route_pct")) / 100.0),
        min(1.0, max(0.0, _feature_number(features, "route_dominated_ratio"))),
    )
    utilization = min(
        1.0, max(0.0, _feature_number(features, "lut_util_pct")) / 80.0
    )
    dispersion = min(
        1.0, 3.0 * max(0.0, _feature_number(features, "spread_ratio"))
    )
    logic_depth = min(
        1.0, max(0.0, _feature_number(features, "max_logic_levels")) / 24.0
    )
    locality_evidence = max(
        dispersion, route_pressure * max(0.0, 1.0 - 0.5 * logic_depth)
    )
    return min(1.0, route_pressure * (1.0 - utilization) * locality_evidence)


def placement_rebuild_opportunity(features: Mapping[str, Any]) -> float:
    """Measure how different a fresh implementation could be from this one.

    Distinct from `placement_basin_opportunity`, which answers how much a local
    polish should be discounted.  The two were one signal, and raising it to
    let rosetta_digit-recognition rebuild also deepened the local-refinement
    discount on ispd16_example2 until an operator rewrite outranked the
    post-route cleanup its recorded 120.61 MHz run takes first, and that run
    ended at 107.64 with no gain at all.

    An empty device is where a rebuild has the most room to land somewhere
    else, so occupancy enters as the plain complement.  But a wall one net owns
    is not a placement problem: one net owns the whole of 3d-rendering's live
    wall and its recorded run rewrites that net for +70.5 MHz, while
    digit-recognition's heaviest net owns 0.3 of a wall spread over 116 tiles
    at three logic levels and a fresh place and route alone is worth +50.74.
    """
    route_pressure = max(
        min(1.0, abs(_feature_number(features, "avg_route_pct")) / 100.0),
        min(1.0, max(0.0, _feature_number(features, "route_dominated_ratio"))),
    )
    utilization = min(
        1.0, max(0.0, _feature_number(features, "lut_util_pct")) / 80.0
    )
    dispersion = min(
        1.0, 3.0 * max(0.0, _feature_number(features, "spread_ratio"))
    )
    logic_depth = min(
        1.0, max(0.0, _feature_number(features, "max_logic_levels")) / 24.0
    )
    single_net_ownership = min(1.0, max(0.0, _feature_number(
        features, "max_single_net_live_wall_coverage"
    )))
    # Route pressure enters once, as the alternative to explicit dispersion
    # when the critical paths are compact but still pay a routing tax.  It is
    # not a second discount: for a rebuild, a routing-bound implementation is
    # the reason to re-implement, not a reason to hold back.  Multiplying by it
    # again left finn_radioml's fresh place and route at 154.93, 4.6 points
    # under a floorplan that measures +35.6 MHz where the rebuild measures
    # +63.2 and is what its recorded 348.31 MHz run takes on round 0.
    locality_evidence = max(
        dispersion, route_pressure * max(0.0, 1.0 - 0.5 * logic_depth)
    )
    return min(1.0, (1.0 - utilization) * locality_evidence
               * (1.0 - single_net_ownership))


def placement_containment_opportunity(features: Mapping[str, Any]) -> float:
    """Measure how much smaller this design could be packed.

    The recorded one-hour flows that win with a floorplan are whole-design
    floorplans: corescore at 416.146, vtr_mcml_v2 at 74.388, mini-isp at
    413.223, digit-recognition at 427.350, finn's Recipe_A, optical-flow at
    359.712, vexriscv's narrow vertical box.  What they exploit is a design
    spread over far more fabric than its resources need -- packing it smaller
    shortens every net, and at low occupancy it cannot congest.

    Gating that on whether sampled critical paths are dispersed measured the
    wrong property.  Across every retained decision snapshot this signal read
    0 on five of seven designs, zeroed by three different factors, and the
    pblock card scored 41-70 against menu tops of 139-236 -- including on
    corescore, whose one-hour row is a hard pblock and whose run gained 2.27
    of a 79.14 MHz target.

    Occupancy is the property that decides whether a design can be packed
    smaller, so it is what this measures.  A wall owned by a single net is
    still discounted: confinement cannot move a fanout wall.
    """
    utilization = min(
        1.0, max(0.0, _feature_number(features, "lut_util_pct")) / 80.0
    )
    single_net_ownership = min(
        1.0,
        max(0.0, _feature_number(features, "max_single_net_live_wall_coverage")),
    )
    # Containment has an optimum occupancy, and it is not the emptiest device.
    # A full one has nowhere to pack into; a nearly empty one is already local,
    # so confining it trades very little wire length for real congestion risk.
    # Measured: corescore at 25.4% occupancy gained 76.29 MHz from a hard
    # pblock, while logicnets at 7.8% took pblock_sweep on its first round and
    # finished at 431.41 against the 531.91 its physical sequence had reached.
    # Scaled so the peak, at half the usable fabric, is 1.0.
    return min(
        1.0,
        4.0 * utilization * (1.0 - utilization)
        * (1.0 - 0.5 * single_net_ownership),
    )


def placement_containment_factors(features: Mapping[str, Any]) -> dict[str, float]:
    """Return the four factors whose product is the containment opportunity.

    The opportunity is a product, so one zero factor zeroes it, and the signal
    alone cannot say which.  Publish the measured path span alongside, because
    a zero dispersion carries two unrelated meanings: no path resolved to a
    site at all, or every path resolved and none reached the spread threshold.
    corescore_500_mod is the second case -- forty paths resolve and span seven
    by six tiles against a thirty by fifty threshold -- so its zero is a
    compact wall that confinement cannot improve, not a failed measurement.
    """
    return {
        "route_pressure": round(max(
            min(1.0, abs(_feature_number(features, "avg_route_pct")) / 100.0),
            min(1.0, max(0.0, _feature_number(features, "route_dominated_ratio"))),
        ), 6),
        "utilization_headroom": round(1.0 - min(
            1.0, max(0.0, _feature_number(features, "lut_util_pct")) / 80.0
        ), 6),
        "dispersion": round(min(
            1.0, max(0.0, _feature_number(features, "spread_ratio"))
        ), 6),
        "measured_path_span": round(
            max(0.0, _feature_number(features, "spread_score")), 6
        ),
        "single_net_ownership": round(min(
            1.0,
            max(0.0, _feature_number(features, "max_single_net_live_wall_coverage")),
        ), 6),
    }


def same_run_feedback_ranking_adjustment(feedback: Mapping[str, Any]) -> float:
    """Score current-run measured feedback without granting mutation authority.

    Exact-seed outcomes remain the strongest signal. Direct-ancestor outcomes
    are a softer search-diversity signal only: they cannot authorize or
    parameterize a mutation on the current descendant.
    """
    raw_broad_bonus = feedback.get("broad_search_escalation_bonus")
    broad_bonus = (
        max(0.0, min(45.0, float(raw_broad_bonus)))
        if (
            isinstance(raw_broad_bonus, (int, float))
            and not isinstance(raw_broad_bonus, bool)
            and math.isfinite(float(raw_broad_bonus))
        )
        else 0.0
    )
    raw_consolidation_delta = feedback.get(
        "post_topology_consolidation_delta_fmax_mhz"
    )
    consolidation_bonus = (
        min(30.0, 15.0 + 5.0 * math.sqrt(float(raw_consolidation_delta)))
        if (
            isinstance(raw_consolidation_delta, (int, float))
            and not isinstance(raw_consolidation_delta, bool)
            and math.isfinite(float(raw_consolidation_delta))
            and float(raw_consolidation_delta) > 0.0
        )
        else 0.0
    )

    attempts = feedback.get("recent_attempts")
    if isinstance(attempts, list) and attempts:
        deltas = []
        for attempt in attempts:
            value = (
                attempt.get("best_legal_delta_fmax_mhz")
                if isinstance(attempt, Mapping) else None
            )
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ):
                deltas.append(float(value))
        if not deltas:
            return -30.0
        best = max(deltas)
        if best <= 0.0:
            return -60.0
        if best < 0.25:
            return -35.0
        return min(15.0, best)

    if consolidation_bonus:
        return consolidation_bonus

    lineage_attempts = feedback.get("recent_lineage_attempts")
    if isinstance(lineage_attempts, list) and lineage_attempts:
        latest_delta = None
        latest_family = ""
        for attempt in reversed(lineage_attempts):
            value = (
                attempt.get("best_legal_delta_fmax_mhz")
                if isinstance(attempt, Mapping) else None
            )
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ):
                latest_delta = float(value)
                latest_family = str(attempt.get("mechanism_family") or "")
                break
        if latest_delta is None:
            return -30.0
        if feedback.get("feedback_scope") == "broad_fanout":
            residual = feedback.get("current_seed_residual")
            coverage = (
                _feature_number(residual, "fanout_live_wall_coverage")
                if isinstance(residual, Mapping) else 0.0
            )
            if coverage <= 0.10:
                return -60.0
            if coverage <= 0.25:
                return -45.0
            if coverage <= 0.50:
                return -25.0
            return -10.0
        # A positive direct-parent result is a continuation signal only for a
        # builder whose next card explores an independently derived basin or
        # locality candidate. One-shot cleanup and local repair mechanisms
        # should yield to re-analysis after promotion instead of repeating
        # merely because their enabling pass was positive.
        if latest_delta > 0.0 and latest_family in {
            "fresh_implementation", "placement_locality"
        }:
            # The parent's gain has already been realized; it cannot predict
            # the marginal value of repeating the backend on its descendant.
            # Force one freshly justified residual-wall mechanism ahead of a
            # stochastic rebuild repeat. The separate broad-search escalation
            # restores fresh priority after local mechanism families fail.
            # Distinct failed residual-wall families restore the value of a
            # broad search.  This bonus was previously computed by the Agent
            # but made unreachable by this early return, so repeated fresh
            # work stayed suppressed until nearly every local action ran.
            return -75.0 + broad_bonus
        if latest_delta > 0.0 and latest_family == "path_local_lut_physical":
            residual = feedback.get("current_seed_residual")
            lut_route_coverage = (
                min(
                    1.0,
                    max(
                        0.0,
                        _feature_number(residual, "critical_lut_route_coverage"),
                    ),
                )
                if isinstance(residual, Mapping) else 0.0
            )
            # A legal pin/reflow change can expose a different LUT on the new
            # worst path.  Continue only in proportion to coverage freshly
            # re-measured on that exact descendant.
            # Scale a measured positive continuation continuously: the gain
            # establishes that the mechanism can move WNS, while freshly
            # measured LUT-route coverage says how much independent wall is
            # still available on the descendant. This lets a newly discovered
            # pin candidate compete with unrelated local edits without fixing
            # an action order or carrying parent objects forward.
            return (
                min(15.0, 5.0 * math.sqrt(latest_delta))
                + 25.0 * lut_route_coverage
            )
        if latest_delta <= 0.0:
            return -45.0
        if latest_delta < 1.0:
            return -35.0
        return -30.0

    if broad_bonus:
        return broad_bonus
    return 0.0


def _mechanism_applicability_signal(
    *,
    skill: str,
    family: str,
    route_signal: float,
    logic_signal: float,
    spread_signal: float,
    fanout_signal: float,
    lut_physical_signal: float,
    single_net_route_signal: float,
) -> tuple[float, str]:
    """Rank mechanisms from live structure without granting authority.

    Clock-pressure reimplementation is most useful when the current placement
    basin is already spatially coherent. A wall spread across the device calls
    for a new placement basin first; treating clock pressure and fresh P&R as
    identical made the shorter static estimate win regardless of applicability.
    """
    if skill == "clock_tighten":
        signal = max(route_signal, logic_signal) * (1.0 - 0.5 * spread_signal)
        return signal, "clock_pressure_discounted_by_live_placement_spread"
    if family == "path_local_lut_physical":
        if skill == "path_local_lut_reflow":
            # Reflow is a bounded placement/logic intervention.  A high LUT
            # route share alone does not make it equivalent to a pin swap:
            # on a shallow route-owned wall, moving the whole path adds more
            # disruption than remapping the live LUT inputs in place.  Keep
            # reflow available for mixed/deeper walls, but rank it from both
            # the live LUT route coverage and the observed logic depth.
            signal = 0.5 * lut_physical_signal + 0.5 * logic_signal
            return signal, "live_lut_route_coverage_blended_with_logic_depth"
        return lut_physical_signal, "live_lut_stage_share_times_route_delay_share"
    if skill in {"per_net_unroute", "critical_net_reroute"}:
        return (
            single_net_route_signal,
            "live_route_share_times_max_single_net_wall_coverage",
        )
    # Thresholded placement spread is deliberately conservative: a compact
    # critical path can stay below the device-span cutoff while still paying a
    # large routing tax from poor relative locality.  Treat the route share
    # left after discounting logic depth as independent current-seed locality
    # evidence.  This exposes containment/anchor actions without importing a
    # historical region or making them executable without their ordinary
    # current-design parameterization and feasibility proof.
    locality_signal = max(
        spread_signal,
        route_signal * max(0.0, 1.0 - 0.5 * logic_signal),
    )
    signal = {
        "post_route_physical": route_signal,
        "critical_net_reroute": route_signal,
        # A wall one net owns is the definitive case for replicating that net,
        # and it is the same evidence per_net_unroute and critical_net_reroute
        # already receive.  vexriscv_re-place_v2 measures a single net owning
        # 87.5% of its live wall and its recorded path to 424.4 runs a
        # critical-net force replication twice, yet this family saw only the
        # fanout census and scored 0.434.  Taken as a maximum, so a design
        # without a single-net wall is unaffected.
        "fanout_replication": max(fanout_signal, single_net_route_signal),
        "placement_locality": locality_signal,
        "local_placement": max(spread_signal, route_signal),
        "path_local_logic": logic_signal,
        "fresh_implementation": max(route_signal, logic_signal, spread_signal),
        "operator_mapping": logic_signal,
    }.get(family, 0.0)
    basis = (
        "live_spread_or_route_tax_locality_signal"
        if family == "placement_locality"
        else "live_structural_family_signal"
    )
    return signal, basis


def build_action_hypothesis(
    *,
    run_id: str,
    skill: str,
    target: str,
    seed_artifact_sha256: str,
    telemetry_epoch: int,
    features: Mapping[str, Any],
    executor_ready: bool,
    executor_blocker: str = "",
    same_run_feedback: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build one public current-seed mechanism hypothesis.

    This object contains no mutation parameters and grants no mutation
    authority. It is the reasoning input from which a private parameterization
    packet may later be constructed.
    """
    if not isinstance(run_id, str) or not run_id.startswith("nr-") or len(run_id) < 8:
        raise ValueError("action hypothesis requires one current no-replay run_id")
    contract = contract_for(skill, target)
    evidence = {
        fact: copy.deepcopy(_feature_value(features, fact))
        for fact in contract.required_facts
        if _fact_present(_feature_value(features, fact))
    }
    missing = [fact for fact in contract.required_facts if fact not in evidence]
    if not contract.cold_mutation_allowed:
        authorization = "blocked"
        blockers = [contract.unavailable_reason]
    elif not executor_ready:
        authorization = "blocked"
        blockers = [executor_blocker or "EXECUTOR_NOT_READY"]
    elif missing:
        authorization = "probe_only"
        blockers = [f"MISSING_CURRENT_SEED_FACT:{fact}" for fact in missing]
    else:
        authorization = "authorized"
        blockers = []

    coverage = len(evidence) / max(1, len(contract.required_facts))
    if authorization == "authorized" and coverage >= 1.0:
        confidence = (
            "trusted"
            if skill == "operator_rewrite" and features.get("proof_status") == "proved"
            else "uncertain"
        )
    else:
        confidence = "untrusted"
    route_signal = min(1.0, abs(_feature_number(features, "avg_route_pct")) / 100.0)
    logic_signal = min(1.0, abs(_feature_number(features, "max_logic_levels")) / 24.0)
    spread_signal = min(1.0, abs(_feature_number(features, "spread_ratio")))
    fanout_strength = min(
        1.0,
        math.log2(max(1.0, _feature_number(features, "max_actionable_fanout"))) / 10.0,
    )
    fanout_wall_coverage = min(
        1.0, max(0.0, _feature_number(features, "fanout_live_wall_coverage"))
    )
    fanout_signal = fanout_strength * fanout_wall_coverage
    broad_fanout = (
        skill == "force_replicate"
        or (skill == "phys_opt" and target == "AggressiveFanoutOpt")
    )
    if broad_fanout:
        # The timing-driven broad primitive is not committed to the first
        # reported net. It can choose among the current seed's fanout-owned
        # paths, so rank its search opportunity from three independent live
        # facts: route pressure, fanout strength, and observed path reach. The
        # geometric mean requires all three while avoiding the single-target
        # error of treating path incidence alone as delay ownership.
        # The aggregate primitive must be justified by aggregate current-wall
        # ownership.  A single net can occur on every sampled path while
        # contributing essentially no delay; borrowing that incidence here
        # made broad fanout look dominant even when the measured aggregate
        # fanout wall coverage was zero.
        broad_wall_coverage = fanout_wall_coverage
        fanout_signal = (
            route_signal * fanout_strength * broad_wall_coverage
        ) ** (1.0 / 3.0)
    elif skill == "fanout_opt":
        target_coverage = min(
            1.0, max(0.0, _feature_number(features, "target_live_wall_coverage"))
        )
        target_delay = min(
            1.0,
            max(0.0, _feature_number(features, "target_profiled_delay_contribution")),
        )
        # Path incidence is not delay ownership. A control or enable net can
        # occur on every sampled path while contributing little measured delay;
        # splitting that single net is then a weak and potentially disruptive
        # hypothesis. Require both current-seed facts continuously instead of
        # allowing either one to dominate the applicability score.
        target_owned_wall = target_coverage * target_delay
        fanout_signal = min(
            1.0,
            math.log2(max(1.0, _feature_number(features, "target_fanout"))) / 10.0,
        ) * target_owned_wall
    lut_physical_signal = min(
        1.0, max(0.0, _feature_number(features, "critical_lut_route_coverage"))
    )
    single_net_route_signal = route_signal * min(
        1.0,
        max(0.0, _feature_number(features, "max_single_net_live_wall_coverage")),
    )
    if contract.family == "operator_rewrite":
        signal_by_family = _feature_number(features, "live_wall_coverage")
        applicability_basis = "exact_current_seed_live_wall_coverage"
        if "structural_contraction_ratio" in features:
            contraction = min(
                1.0,
                max(
                    0.0,
                    _feature_number(features, "structural_contraction_ratio"),
                ),
            )
            signal_by_family *= max(0.05, contraction)
            applicability_basis += "_times_live_structural_contraction"
    else:
        signal_by_family, applicability_basis = _mechanism_applicability_signal(
            skill=skill,
            family=contract.family,
            route_signal=route_signal,
            logic_signal=logic_signal,
            spread_signal=spread_signal,
            fanout_signal=fanout_signal,
            lut_physical_signal=lut_physical_signal,
            single_net_route_signal=single_net_route_signal,
        )
        if skill == "fanout_opt":
            applicability_basis = (
                "exact_target_fanout_times_path_incidence_and_delay_ownership"
            )
        elif broad_fanout:
            applicability_basis = (
                "broad_timing_driven_geomean_of_route_fanout_and_path_reach"
            )
    feedback = copy.deepcopy(dict(same_run_feedback or {}))
    feedback_adjustment = same_run_feedback_ranking_adjustment(feedback)
    basin_opportunity = placement_basin_opportunity(features)
    rebuild_opportunity = placement_rebuild_opportunity(features)
    polish_discount = local_polish_discount_signal(features)
    containment_opportunity = placement_containment_opportunity(features)
    placement_basin_builder = (
        contract.family == "placement_locality"
        and skill in {"pblock", "pblock_sweep"}
    )
    placement_local_refinement = (
        contract.family == "placement_locality"
        and skill == "critical_cluster_anchor"
    )
    if placement_basin_builder:
        # A region-constrained implementation needs measured containment
        # opportunity, not merely evidence that a fresh placement basin could
        # help. Route locality without dispersion authorizes fresh P&R but
        # should not make a pblock outrank a directly covered wall mechanism.
        signal_by_family = min(signal_by_family, containment_opportunity)
    search_role = (
        "basin_builder"
        if placement_basin_builder or contract.family == "fresh_implementation"
        else "local_refinement"
        if placement_local_refinement or contract.family in {
            "post_route_physical",
            "critical_net_reroute",
            "local_placement",
            "path_local_logic",
            "path_local_lut_physical",
        }
        else "structural_mechanism"
    )
    lineage_rows = feedback.get("recent_lineage_attempts")
    lineage_mechanism_repeat = bool(
        search_role == "basin_builder"
        and isinstance(lineage_rows, list)
        and any(
            isinstance(row, Mapping)
            and str(row.get("mechanism_family") or "") == contract.family
            for row in lineage_rows
        )
    )
    if lineage_mechanism_repeat:
        search_role = "repeated_basin_backend"
    scope_adjustment = 0.0
    if placement_basin_builder:
        # Route locality alone supports rebuilding a basin, but hard placement
        # containment additionally requires current-seed spatial dispersion.
        scope_adjustment = 60.0 * containment_opportunity - 25.0
        hard_macro_present = bool(features.get("hard_macro_present"))
        # Match the builder backend to facts of the exact current seed.  A
        # SLICE-only design can use isolated native-SLICE siblings, while a
        # mixed-resource design benefits from the single capacity-aware fabric
        # region flow.  This is a scope-fit signal, not benchmark knowledge.
        if skill == "pblock_sweep" and not hard_macro_present:
            scope_adjustment += 6.0
        elif skill == "pblock" and hard_macro_present:
            scope_adjustment += 6.0
    elif contract.family == "fresh_implementation":
        # Only a mechanism that unplaces the design is searching for a
        # different basin.  clock_tighten re-implements under a tighter target
        # on the basin it already has, so the rebuild signal's occupancy and
        # wall-ownership terms do not describe it: boom_soc's live wall is
        # owned by a single net, which zeroed the rebuild signal and dropped
        # clock_tighten 48.5 points off the top of its menu -- the mechanism
        # that measures 73.56 MHz there, where the phys_opt that replaced it
        # measured 51.32.
        scope_adjustment = 60.0 * (
            rebuild_opportunity
            if "unplace" in contract.destructive_scope
            else basin_opportunity
        ) - 25.0
    elif search_role == "local_refinement":
        residual_fraction = (
            1.0 - lut_physical_signal
            if contract.family == "path_local_lut_physical"
            else 1.0
        )
        scope_adjustment = -50.0 * polish_discount * residual_fraction
    ranking_score = round(
        100.0 * coverage
        + 100.0 * signal_by_family
        + independent_lineage_credit(target, signal_by_family)
        - min(30.0, contract.estimated_runtime_s / 120.0)
        - {"low": 0.0, "medium": 5.0, "high": 10.0}[contract.risk]
        + feedback_adjustment
        + scope_adjustment,
        6,
    )
    action_id_body = {
        "schema_version": HYPOTHESIS_SCHEMA_VERSION,
        "run_id": run_id,
        "seed_artifact_sha256": str(seed_artifact_sha256),
        "telemetry_epoch": int(telemetry_epoch),
        "skill": str(skill),
        "target": str(target),
        "family": contract.family,
    }
    body = {
        **action_id_body,
        "action_id": _canonical_digest(action_id_body),
        "authorization_status": authorization,
        "decision_confidence": confidence,
        "ranking_score": ranking_score,
        "applicability_signal": round(signal_by_family, 6),
        "applicability_basis": applicability_basis,
        "independent_lineage_credit": round(
            independent_lineage_credit(target, signal_by_family), 6
        ),
        "basin_opportunity_signal": round(basin_opportunity, 6),
        "containment_opportunity_signal": round(containment_opportunity, 6),
        "search_role": search_role,
        "lineage_mechanism_repeat": lineage_mechanism_repeat,
        "ancestor_gain_consumed": lineage_mechanism_repeat,
        "scope_ranking_adjustment": round(scope_adjustment, 6),
        "mechanism": contract.mechanism,
        "why_now": (
            f"current seed supplies {', '.join(evidence)}"
            + (
                "; the same mechanism already completed on the direct ancestor, "
                "so that ancestor gain is consumed"
                if lineage_mechanism_repeat else ""
            )
            if evidence else "current seed lacks the evidence needed to rank this mechanism"
        ),
        "live_evidence": evidence,
        "missing_evidence": missing,
        "expected_wall_effect": contract.mechanism,
        "expected_next_wall": "must be re-profiled after central measurement",
        "parameter_policy": list(contract.parameter_policy),
        "allowed_parameter_regimes": list(contract.regimes),
        "risk": contract.risk,
        "estimated_runtime_s": contract.estimated_runtime_s,
        "resource_request": {
            "cpu_cores": contract.cpu_cores,
            "memory_gib": contract.memory_gib,
            "exclusive_session": contract.exclusive_session,
        },
        "destructive_scope": contract.destructive_scope,
        "logical_structure_effect": (
            "may_destroy_current_semantic_opportunity"
            if contract.destructive_scope in {
                "routed_physical_optimization",
                "rapidwright_netlist_eco",
                "rapidwright_logic_eco",
                "selective_or_full_unroute",
                "full_place_route",
                "isolated_full_place_route_siblings",
                "full_unplace_unroute",
                "temporary_clock_full_place_route",
                "dynamic_task_graph",
                "proved_netlist_mutation",
            }
            else "preserves_current_logical_structure"
        ),
        "same_run_feedback": feedback,
        "same_run_feedback_ranking_adjustment": feedback_adjustment,
        "blockers": blockers,
        "mutation_authority": False,
    }
    return body


__all__ = [
    "ActionContract",
    "HYPOTHESIS_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_action_hypothesis",
    "contract_for",
    "placement_basin_opportunity",
    "registered_contract_skills",
    "validate_contract_coverage",
]
