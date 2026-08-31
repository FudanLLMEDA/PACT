"""Diagnosis-to-recipe planner for FDAgents.

This module is deliberately pure Python: it converts the compact initial
analysis into a high-level recipe choice, but it never calls Vivado or
RapidWright. The agent later maps the recipe to existing legal skills.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional


HARD_MACRO_GUIDANCE_PBLOCK_PARAMS = {
    "release_after_place": False,
    "place_directive": "Default",
    "pre_route_phys_opt": "Explore",
    "route_directive": "Explore",
    "post_route_phys_opt": "AggressiveExplore",
    "target_ff_multiplier": 0.0,
    "guidance_only": True,
    "validate_resources": False,
    "max_expansion_attempts": 0,
}

ROUTE_PRESERVING_GUIDANCE_PBLOCK_PARAMS = {
    "release_after_place": True,
    "post_route_phys_opt": "AggressiveFanoutOpt",
    "target_ff_multiplier": 0.0,
    "guidance_only": True,
    "validate_resources": False,
    "max_expansion_attempts": 0,
}

TARGETED_FORCE_REPLICATE_PARAMS = {
    "passes": 2,
    "unroute_scope": "global",
    "pre_place_post_place_opt": True,
    "route_directive": "NoTimingRelaxation",
    "route_tns_cleanup": True,
    "post_rep_phys_opt": "AggressiveFanoutOpt",
    "final_phys_opt": "Explore",
    "final_route": False,
}

GROUP_FORCE_REPLICATE_PARAMS = {
    "target_candidates": ["auto_highfanout_group"],
    "num_paths": 24,
    "max_nets": 18,
    "endpoint_min_fanout": 4,
    "fallback_min_fanout": 64,
    "passes": 2,
    "unroute_scope": "global",
    "pre_place_post_place_opt": True,
    "route_directive": "NoTimingRelaxation",
    "route_tns_cleanup": True,
    "post_rep_phys_opt": "AggressiveFanoutOpt",
    "final_phys_opt": "Explore",
    "final_route": False,
}

ROUTE_FANOUT_FLOORPLAN_FANOUT_PARAMS = {
    "split_factor": 3,
    "accept_min_delta_ns": 0.008,
}

ROUTE_FANOUT_FLOORPLAN_SWEEP_PARAMS = {
    # Keep this bounded; the profile is most useful as a quick floorplan seed
    # before fanout and late route-polish steps.
    "max_candidates": 1,
}

HIGH_UTIL_DEEP_ROUTE_FORCE_REPLICATE_PARAMS = {
    "target_candidates": ["auto_highfanout_group"],
    "replace_target_candidates": True,
    "num_paths": 40,
    "max_nets": 8,
    "endpoint_min_fanout": 4,
    "fallback_min_fanout": 60,
    "passes": 1,
    "unroute_scope": "global",
    "pre_place_post_place_opt": True,
    "route_directive": "NoTimingRelaxation",
    "route_tns_cleanup": True,
    "post_rep_phys_opt": "AggressiveFanoutOpt",
    "final_phys_opt": "Explore",
    "final_route": False,
    "min_remaining_s": 1800,
}

HIGH_UTIL_DEEP_ROUTE_FRESH_PARAMS = {
    "recipes": [
        {
            "name": "full_unplace_retime_route",
            "command": (
                "route_design -unroute; "
                "place_design -unplace; "
                "catch {opt_design -directive Explore}; "
                "catch {place_design -directive ExtraTimingOpt}; "
                "foreach d {AlternateFlowWithRetiming AggressiveExploreWithRetiming AggressiveExplore Explore} "
                "{ catch {phys_opt_design -directive $d} }; "
                "if {[catch {route_design -directive AlternateCLBRouting -tns_cleanup}]} "
                "{ route_design -directive NoTimingRelaxation -tns_cleanup }; "
                "foreach d {AggressiveExplore Explore} { catch {phys_opt_design -directive $d} }"
            ),
        }
    ],
    "max_candidates": 1,
    "recipe_timeout_s": 2400,
    "min_remaining_s": 1800,
}

HIGH_UTIL_DEEP_ROUTE_CLEANUP_PARAMS = {
    "recipes": [
        {
            "name": "clockopt_criticalpin_cleanup",
            "command": (
                "catch {phys_opt_design -clock_opt}; "
                "catch {phys_opt_design -routing_opt -critical_pin_opt -memory_rewire_opt -critical_cell_opt -placement_opt}; "
                "catch {phys_opt_design -directive AggressiveExplore}; "
                "catch {route_design -directive NoTimingRelaxation -tns_cleanup}; "
                "catch {phys_opt_design -clock_opt}; "
                "catch {phys_opt_design -directive Explore}"
            ),
        }
    ],
    "max_candidates": 1,
    "max_iterations": 1,
    "recipe_timeout_s": 1200,
    "min_remaining_s": 600,
}

HIGH_UTIL_DEEP_ROUTE_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.55, 0.524, 0.50],
    "recipes": [
        {
            "name": "target_period_dynamic_replication",
            "command": (
                "set fdagents_nets {}; "
                "foreach tp [get_timing_paths -quiet -setup -max_paths 60 -nworst 1] "
                "{ foreach prop {STARTPOINT_PIN ENDPOINT_PIN} { set pin [get_property $prop $tp]; "
                "if {$pin eq {}} { continue }; foreach n [get_nets -quiet -of_objects $pin] { "
                "set nname [get_property NAME $n]; "
                "if {[regexp -nocase {(^|/)(clk|clock)(/|$)|bufg|mmcm|pll} $nname]} { continue }; "
                "set fanout 0; catch {set fanout [get_property FLAT_PIN_COUNT $n]}; "
                "if {$fanout >= 2} { lappend fdagents_nets $n } } } }; "
                "set nets [lrange [lsort -unique $fdagents_nets] 0 35]; "
                "route_design -unroute; "
                "catch {place_design -post_place_opt}; "
                "if {[llength $nets] > 0} { catch {phys_opt_design -force_replication_on_nets $nets} }; "
                "foreach d {AggressiveFanoutOpt AlternateReplication AlternateFlowWithRetiming AggressiveExplore Explore} "
                "{ catch {phys_opt_design -directive $d} }; "
                "if {[catch {route_design -directive NoTimingRelaxation -tns_cleanup}]} "
                "{ route_design -directive Explore -tns_cleanup }; "
                "foreach d {AggressiveExplore Explore} { catch {phys_opt_design -directive $d} }; "
                "catch {phys_opt_design -clock_opt}"
            ),
        }
    ],
    "max_candidates": 2,
    "target_candidates": ["auto"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 1800,
    "max_fmax_mhz": 0.0,
    "min_remaining_s": 1200,
}

HIGH_UTIL_TARGET_PERIOD_FAST_ROUTE_COMMAND = (
    "set_param general.maxThreads 8; "
    "set fdagents_t0 [clock seconds]; "
    "proc fdagents_elapsed {} { expr {[clock seconds] - $::fdagents_t0} }; "
    "proc fdagents_mark {msg} { puts \"FDAGENTS_TARGET_PERIOD_STEP $msg elapsed=[fdagents_elapsed]\"; flush stdout }; "
    "fdagents_mark unroute; route_design -unroute; "
    "fdagents_mark unplace; place_design -unplace; "
    "fdagents_mark opt_explore; catch {opt_design -directive Explore}; "
    "fdagents_mark place_extratim; catch {place_design -directive ExtraTimingOpt}; "
    "set fdagents_nets {}; "
    "foreach tp [get_timing_paths -quiet -setup -max_paths 80 -nworst 1] "
    "{ foreach prop {STARTPOINT_PIN ENDPOINT_PIN} { "
    "set pin [get_property $prop $tp]; "
    "if {$pin eq {}} { continue }; "
    "foreach n [get_nets -quiet -of_objects $pin] { "
    "set nname [get_property NAME $n]; "
    "if {[regexp -nocase {(^|/)(clk|clock)(/|$)|clock_uncore_clock|bufg|mmcm|pll} $nname]} { continue }; "
    "set fanout 0; catch {set fanout [get_property FLAT_PIN_COUNT $n]}; "
    "if {$fanout >= 2} { lappend fdagents_nets $n } } } }; "
    "set nets [lrange [lsort -unique $fdagents_nets] 0 31]; "
    "puts \"FDAGENTS_TARGET_PERIOD_NETS [llength $nets]\"; "
    "if {[llength $nets] > 0} { fdagents_mark force_replication; catch {phys_opt_design -force_replication_on_nets $nets} }; "
    "fdagents_mark route_altclb; route_design -directive AlternateCLBRouting; "
    "if {[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {}} { "
    "if {![catch {write_checkpoint -force $fdagents_candidate_checkpoint}]} { fdagents_mark checkpoint_after_route } }; "
    "if {[fdagents_elapsed] < 3330} { fdagents_mark post_critical_pin; catch {phys_opt_design -critical_pin_opt}; "
    "if {[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {}} { "
    "if {![catch {write_checkpoint -force $fdagents_candidate_checkpoint}]} { fdagents_mark checkpoint_after_post_critical_pin } } }; "
    "if {[fdagents_elapsed] < 3330} { fdagents_mark post_clock_opt; catch {phys_opt_design -clock_opt}; "
    "if {[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {}} { "
    "if {![catch {write_checkpoint -force $fdagents_candidate_checkpoint}]} { fdagents_mark checkpoint_after_post_clock_opt } } }; "
    "fdagents_mark done"
)

HIGH_UTIL_TARGET_PERIOD_REPLACE_ROUTE_COMMAND = (
    "set_param general.maxThreads 8; "
    "set fdagents_t0 [clock seconds]; "
    "proc fdagents_elapsed {} { expr {[clock seconds] - $::fdagents_t0} }; "
    "proc fdagents_mark {msg} { puts \"FDAGENTS_TARGET_PERIOD_STEP $msg elapsed=[fdagents_elapsed]\"; flush stdout }; "
    "fdagents_mark unroute; route_design -unroute; "
    "fdagents_mark unplace; place_design -unplace; "
    "fdagents_mark opt_explore; catch {opt_design -directive Explore}; "
    "fdagents_mark place_extratim; catch {place_design -directive ExtraTimingOpt}; "
    "set fdagents_nets {}; "
    "foreach tp [get_timing_paths -quiet -setup -max_paths 80 -nworst 1] "
    "{ foreach prop {STARTPOINT_PIN ENDPOINT_PIN} { "
    "set pin [get_property $prop $tp]; "
    "if {$pin eq {}} { continue }; "
    "foreach n [get_nets -quiet -of_objects $pin] { "
    "set nname [get_property NAME $n]; "
    "if {[regexp -nocase {(^|/)(clk|clock)(/|$)|clock_uncore_clock|bufg|mmcm|pll} $nname]} { continue }; "
    "set fanout 0; catch {set fanout [get_property FLAT_PIN_COUNT $n]}; "
    "if {$fanout >= 2} { lappend fdagents_nets $n } } } }; "
    "set nets [lrange [lsort -unique $fdagents_nets] 0 31]; "
    "puts \"FDAGENTS_TARGET_PERIOD_NETS [llength $nets]\"; "
    "if {[llength $nets] > 0} { fdagents_mark force_replication; catch {phys_opt_design -force_replication_on_nets $nets} }; "
    "if {[fdagents_elapsed] < 1700} { fdagents_mark pre_aggressive_fanout; catch {phys_opt_design -directive AggressiveFanoutOpt} }; "
    "if {[fdagents_elapsed] < 1700} { fdagents_mark pre_aggressive_explore; catch {phys_opt_design -directive AggressiveExplore} }; "
    "fdagents_mark route_altclb; route_design -directive AlternateCLBRouting; "
    "if {[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {}} { "
    "if {![catch {write_checkpoint -force $fdagents_candidate_checkpoint}]} { fdagents_mark checkpoint_after_route } }; "
    "if {[fdagents_elapsed] < 3330} { fdagents_mark post_critical_pin; catch {phys_opt_design -critical_pin_opt}; "
    "if {[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {}} { "
    "if {![catch {write_checkpoint -force $fdagents_candidate_checkpoint}]} { fdagents_mark checkpoint_after_post_critical_pin } } }; "
    "if {[fdagents_elapsed] < 3330} { fdagents_mark post_clock_opt; catch {phys_opt_design -clock_opt}; "
    "if {[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {}} { "
    "if {![catch {write_checkpoint -force $fdagents_candidate_checkpoint}]} { fdagents_mark checkpoint_after_post_clock_opt } } }; "
    "fdagents_mark done"
)

HIGH_UTIL_TARGET_PERIOD_CLOCK_TIGHTEN_PARAMS = {
    "target_periods": [5.0],
    "recipes": [
        {
            "name": "target_period_fast_route_floor",
            "command": HIGH_UTIL_TARGET_PERIOD_FAST_ROUTE_COMMAND,
        },
        {
            "name": "target_period_preroute_aggressive_route",
            "command": HIGH_UTIL_TARGET_PERIOD_REPLACE_ROUTE_COMMAND,
        }
    ],
    "max_candidates": 1,
    "target_candidates": [
        "target_period_fast_route_floor",
        "target_period_preroute_aggressive_route",
    ],
    "replace_target_candidates": True,
    "recipe_timeout_s": 3500,
    "recover_partial_candidate_on_timeout": True,
    "max_fmax_mhz": 0.0,
    "min_remaining_s": 3000,
    "target_params": {
        "target_period_fast_route_floor": {
            "recipes": [
                {
                    "name": "target_period_fast_route_floor",
                    "command": HIGH_UTIL_TARGET_PERIOD_FAST_ROUTE_COMMAND,
                }
            ],
            "recipe_timeout_s": 3000,
            "min_remaining_s": 2400,
        },
        "target_period_preroute_aggressive_route": {
            "recipes": [
                {
                    "name": "target_period_preroute_aggressive_route",
                    "command": HIGH_UTIL_TARGET_PERIOD_REPLACE_ROUTE_COMMAND,
                }
            ],
        },
    },
}

PBLOCK_CRITICAL_HIGHFANOUT_PARAMS = {
    "target_candidates": ["auto_highfanout_group"],
    "replace_target_candidates": True,
    "num_paths": 24,
    "max_nets": 18,
    "endpoint_min_fanout": 4,
    "fallback_min_fanout": 64,
    "passes": 2,
    "unroute_scope": "global",
    "pre_place_post_place_opt": True,
    "route_directive": "NoTimingRelaxation",
    "route_tns_cleanup": True,
    "post_rep_phys_opt": "AggressiveFanoutOpt",
    "final_phys_opt": "Explore",
    "final_route": False,
    "max_threads": 4,
    "min_remaining_s": 600,
}

CRITICAL_HARD_MACRO_PBLOCK_PARAMS = {
    "release_after_place": False,
    "place_directive": "Default",
    "pre_route_phys_opt": "Explore",
    "route_directive": "Explore",
    "post_route_phys_opt": "AggressiveExplore",
    "target_ff_multiplier": 0.0,
    "guidance_only": True,
    "validate_resources": False,
    "max_expansion_attempts": 0,
}

CRITICAL_FRESH_PLACE_PARAMS = {
    "recipes": ["extratiming_no_relax", "extranetdelay_no_relax"],
    "max_candidates": 1,
    "recipe_timeout_s": 1200,
}

CRITICAL_POST_ROUTE_CLEANUP_PARAMS = {
    "recipes": ["route_explore_phys_explore", "phys_explore"],
    "max_candidates": 1,
    "max_iterations": 1,
    "recipe_timeout_s": 900,
}

FRESH_ENDPOINT_PARAMS = {
    "recipes": ["extranetdelay_no_relax"],
    "max_candidates": 1,
    "endpoint_bel_move_after": True,
    "endpoint_num_paths": 20,
    "endpoint_max_candidates": 6,
}

DIRECTIVE_SWEEP_PARAMS = {
    "recipes": ["extranetdelay_no_relax", "extratiming_no_relax"],
    "max_candidates": 2,
}

SHALLOW_HARD_MACRO_ROUTE_FRESH_PARAMS = {
    "recipes": ["extranetdelay_no_relax"],
    "max_candidates": 1,
    "recipe_timeout_s": 2400,
    "min_remaining_s": 1800,
}

VERY_HIGH_UTIL_BACKEND_RETIME_PHYS_PARAMS = {
    "directive_order": ["AggressiveExplore", "Explore", "Default", "AddRetime"],
}

VERY_HIGH_UTIL_BACKEND_RETIME_CLEANUP_PARAMS = {
    "recipes": [
        "phys_aggr_explore",
        "route_higher_tns_phys_explore",
        "route_aggr_phys_explore",
    ],
    "max_candidates": 3,
    "max_iterations": 1,
    "recipe_timeout_s": 1200,
}

CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.92, 0.88, 0.84],
    "recipes": ["extra_timing_no_relax"],
    "max_candidates": 3,
    # Official alpha score is not capped by the VU3P datasheet 775 MHz value.
    "max_fmax_mhz": 0.0,
    "target_candidates": ["auto"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 1800,
}

COMPACT_CLOCK_TIGHTEN_PARAMS = {
    **CLOCK_TIGHTEN_PARAMS,
    "factors": [0.92, 0.88, 0.84, 0.80, 0.762, 0.76, 0.72, 0.68, 0.64, 0.60],
    "max_candidates": 10,
    # Compact clock-loose designs can spend the whole 1h window sweeping from
    # loose to tight periods before returning a result. Try tight periods first
    # and stop at the first legal routed checkpoint so the harness sees the gain.
    "period_order": "tightest_first",
    "stop_after_first_legal": True,
    "enable_pulse_recap": True,
    "enable_clock_as_data_false_path": True,
    "recap_margin_ns": 0.001,
    "max_recap_candidates": 1,
    "recipe_timeout_s": 900,
}

CLUSTER_ANCHOR_PARAMS = {
    "num_paths": 12,
    "max_cells": 24,
    "max_nets": 12,
    "bbox_pad_x": 4,
    "bbox_pad_y": 8,
}

ROUTE_FANOUT_FLOORPLAN_CLEANUP_PARAMS = {
    "target_candidates": ["pre_lut_directive", "post_lut_cleanup"],
    "replace_target_candidates": True,
    "target_params": {
        "pre_lut_directive": {
            "recipes": [
                "phys_explore",
                "route_aggr_phys_explore",
                "route_higher_tns_phys_explore",
            ],
            "max_candidates": 3,
            "max_iterations": 2,
            "min_remaining_s": 1200,
            "recipe_timeout_s": 900,
        },
        "post_lut_cleanup": {
            "recipes": ["route_higher_tns_phys_explore"],
            "max_candidates": 1,
            "max_iterations": 1,
            "recipe_timeout_s": 900,
        },
    },
}

ROUTE_FANOUT_FLOORPLAN_REROUTE_PARAMS = {
    "target_candidates": ["early_window", "high_delay10", "top3_final"],
    "replace_target_candidates": True,
    "target_params": {
        "early_window": {
            "route_mode": "explore_group",
            "post_phys_opt": "Explore",
            "num_paths": 1,
            "max_candidates": 3,
            "max_fanout": 256,
            "route_timeout_s": 900,
        },
        "high_delay10": {
            "selection_mode": "high_delay",
            "route_mode": "explore_group",
            "post_phys_opt": "Explore",
            "num_paths": 3,
            "max_nets": 10,
            "max_fanout": 512,
            "route_timeout_s": 900,
        },
        "top3_final": {
            "selection_mode": "top3",
            "route_mode": "explore_group",
            "post_phys_opt": "Explore",
            "num_paths": 1,
            "max_nets": 3,
            "max_fanout": 512,
            "route_timeout_s": 900,
        },
    },
}


def _datuner_directive_command(
    *,
    opt_directive: str,
    fanout_limit: int,
    place_directive: str,
    phys_opt_directive: str,
    route_directive: str,
) -> str:
    """Build a generic DATuner-style full reimplementation Tcl recipe."""
    return (
        "set_param general.maxThreads 8; "
        "set_param drc.disableLUTOverUtilError 1; "
        "set fdagents_fanout_targets [get_nets -hierarchical -quiet -filter {TYPE == SIGNAL}]; "
        "if {[llength $fdagents_fanout_targets] == 0} "
        "{ set fdagents_fanout_targets [get_nets -hierarchical -quiet] }; "
        f"if {{[llength $fdagents_fanout_targets] > 0}} "
        f"{{ catch {{set_property MAX_FANOUT {int(fanout_limit)} $fdagents_fanout_targets}} }}; "
        "catch {route_design -unroute}; "
        "catch {place_design -unplace}; "
        f"opt_design -directive {opt_directive}; "
        f"place_design -directive {place_directive}; "
        f"phys_opt_design -directive {phys_opt_directive}; "
        f"route_design -directive {route_directive} -tns_cleanup"
    )


DATUNER_SMALL_LOGIC_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.92, 0.88],
    "recipes": [
        {
            "name": "datuner_area_spread_altrep_default",
            "command": _datuner_directive_command(
                opt_directive="ExploreArea",
                fanout_limit=100,
                place_directive="SpreadLogic_low",
                phys_opt_directive="AlternateReplication",
                route_directive="Default",
            ),
        }
    ],
    "max_candidates": 2,
    "target_candidates": ["datuner_area_spread_altrep_default"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 1500,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_SLICE_XBAR_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.84, 0.88, 0.92],
    "recipes": [
        {
            "name": "datuner_area_extranet_low_no_relax",
            "command": _datuner_directive_command(
                opt_directive="ExploreArea",
                fanout_limit=10000,
                place_directive="ExtraNetDelay_low",
                phys_opt_directive="Explore",
                route_directive="NoTimingRelaxation",
            ),
        }
    ],
    "max_candidates": 3,
    "target_candidates": ["datuner_area_extranet_low_no_relax"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2400,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_NO_MACRO_MEDIUM_DEFAULT_RETIME_EXPLORE_PARAMS = {
    "factors": [0.92],
    "recipes": [
        {
            "name": "datuner_default_extratiming_retime_explore",
            "command": _datuner_directive_command(
                opt_directive="Default",
                fanout_limit=500,
                place_directive="ExtraTimingOpt",
                phys_opt_directive="AlternateFlowWithRetiming",
                route_directive="Explore",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_default_extratiming_retime_explore"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2200,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_WIDE_MACRO_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.80, 0.84],
    "recipes": [
        {
            "name": "datuner_explore_extranet_low_aggressive_route_explore",
            "command": _datuner_directive_command(
                opt_directive="Explore",
                fanout_limit=10000,
                place_directive="ExtraNetDelay_low",
                phys_opt_directive="AggressiveExplore",
                route_directive="Explore",
            ),
        }
    ],
    "max_candidates": 2,
    "target_candidates": ["datuner_explore_extranet_low_aggressive_route_explore"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 3400,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_MID_MACRO_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.88, 0.92],
    "recipes": [
        {
            "name": "datuner_addremap_extranet_high_aggressive",
            "command": _datuner_directive_command(
                opt_directive="AddRemap",
                fanout_limit=500,
                place_directive="ExtraNetDelay_high",
                phys_opt_directive="Default",
                route_directive="AggressiveExplore",
            ),
        }
    ],
    "max_candidates": 2,
    "target_candidates": ["datuner_addremap_extranet_high_aggressive"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2800,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_COMPACT_MACRO_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.92, 0.88],
    "recipes": [
        {
            "name": "datuner_default_extratiming_aggressive",
            "command": _datuner_directive_command(
                opt_directive="Default",
                fanout_limit=100,
                place_directive="ExtraTimingOpt",
                phys_opt_directive="Default",
                route_directive="AggressiveExplore",
            ),
        }
    ],
    "max_candidates": 2,
    "target_candidates": ["datuner_default_extratiming_aggressive"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2200,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_HUGE_LOGIC_SPREAD_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.84],
    "recipes": [
        {
            "name": "datuner_explore_spread_high_altrep_explore",
            "command": _datuner_directive_command(
                opt_directive="Explore",
                fanout_limit=100,
                place_directive="SpreadLogic_high",
                phys_opt_directive="AlternateReplication",
                route_directive="Explore",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_explore_spread_high_altrep_explore"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 3400,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_BRAM_HEAVY_SPREAD_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.84],
    "recipes": [
        {
            "name": "datuner_explore_ssi_spread_high_default_explore",
            "command": _datuner_directive_command(
                opt_directive="Explore",
                fanout_limit=5000,
                place_directive="SSI_SpreadLogic_high",
                phys_opt_directive="Default",
                route_directive="Explore",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_explore_ssi_spread_high_default_explore"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 3400,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_BRAM_ROUTE_SPREAD_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.95],
    "recipes": [
        {
            "name": "datuner_addremap_spread_low_explore_aggressive",
            "command": _datuner_directive_command(
                opt_directive="AddRemap",
                fanout_limit=100,
                place_directive="SpreadLogic_low",
                phys_opt_directive="Explore",
                route_directive="AggressiveExplore",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_addremap_spread_low_explore_aggressive"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2800,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_GENERATED_FFT_L_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.92],
    "recipes": [
        {
            "name": "datuner_default_extranet_low_aggrfanout_no_relax",
            "command": _datuner_directive_command(
                opt_directive="Default",
                fanout_limit=100,
                place_directive="ExtraNetDelay_low",
                phys_opt_directive="AggressiveFanoutOpt",
                route_directive="NoTimingRelaxation",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_default_extranet_low_aggrfanout_no_relax"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2800,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_DSP_SPREAD_RETIME_AGGRESSIVE_PARAMS = {
    "factors": [0.88],
    "recipes": [
        {
            "name": "datuner_default_ssi_spread_retime_aggressive",
            "command": _datuner_directive_command(
                opt_directive="Default",
                fanout_limit=100,
                place_directive="SSI_SpreadLogic_high",
                phys_opt_directive="AlternateFlowWithRetiming",
                route_directive="AggressiveExplore",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_default_ssi_spread_retime_aggressive"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 2600,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}

DATUNER_DSP_COMPACT_CLOCK_TIGHTEN_PARAMS = {
    "factors": [0.92],
    "recipes": [
        {
            "name": "datuner_seq_area_default_explore_aggressive",
            "command": _datuner_directive_command(
                opt_directive="ExploreSequentialArea",
                fanout_limit=1000,
                place_directive="Default",
                phys_opt_directive="Explore",
                route_directive="AggressiveExplore",
            ),
        }
    ],
    "max_candidates": 1,
    "target_candidates": ["datuner_seq_area_default_explore_aggressive"],
    "replace_target_candidates": True,
    "recipe_timeout_s": 1800,
    "stop_after_first_legal": True,
    "max_fmax_mhz": 0.0,
}


@dataclass(frozen=True)
class DesignFeatures:
    design_id: str = ""
    wns_ns: float = 0.0
    clock_period_ns: Optional[float] = None
    lut_count: int = 0
    ff_count: int = 0
    dsp_count: int = 0
    bram_count: int = 0
    uram_count: int = 0
    lut_util_pct: Optional[float] = None
    avg_route_pct: float = 0.0
    route_dominated_ratio: float = 0.0
    max_logic_levels: int = 0
    dominant_bottleneck: str = ""
    max_fanout: int = 0
    max_actionable_fanout: int = 0
    eligible_hf_count: int = 0
    broad_hf_count: int = 0
    hard_macro_present: bool = False
    spread_ratio: float = 0.0
    spread_score: float = 0.0
    pre_route_to_routed_gap_mhz: float = 0.0
    memory_address_guidance_profile: bool = False


@dataclass(frozen=True)
class RecipeDecision:
    recipe_id: str
    confidence: float
    reason: str
    skill_order: list[str]
    initial_params: dict[str, dict] = field(default_factory=dict)
    core_skills: list[str] = field(default_factory=list)
    budget_hint_s: int = 3600
    verification_required: bool = True
    bottleneck: str = "mixed"
    equivalence_risk: str = "low"
    learned_fmax_mhz: Optional[float] = None
    knowledge_evidence: list[dict[str, str]] = field(default_factory=list)


_CLOCK_FRAGMENTS = (
    "bufg",
    "mmcm",
    "pll",
    "wclk",
    "rclk",
    "wrclk",
    "rdclk",
    "txoutclk",
    "rxoutclk",
    "userclk",
    "coreclk",
)

_MEMORY_ADDRESS_PATTERNS = (
    "address",
    "addr_reg",
    "theta_local",
    "ram_reg",
)

_HARD_MACRO_LEAF_PATTERNS = (
    r"ADDR[A-D](?:\d+)?",
    r"RAM[A-D](?:_[A-Z]+\d*)?",
    r"DOUT[A-D]DOUT(?:\d+)?",
    r"DIN[A-D]DIN(?:\d+)?",
    r"CLK[A-D]?(?:RDCLK|WRCLK)?",
    r"[WR]CLK",
    r"WE[A-D]?(?:\d+)?",
)


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _design_name(design_id: Optional[str]) -> str:
    return str(design_id or "").lower()


def _is_clock_like_net_name(net_name: str) -> bool:
    lowered = net_name.lower()
    parts = [
        re.sub(r"(?:\[\d+\])+$", "", p.lower())
        for p in net_name.replace("[", "/").split("/")
        if p
    ]
    if any(p in {"clk", "clock"} for p in parts):
        return True
    if any(
        p in {"ap_clk", "aclk"}
        or p.startswith(("clk_", "clock_"))
        or p.endswith(("_clk", "_clock", "_aclk"))
        for p in parts
    ):
        return True
    return any(fragment in lowered for fragment in _CLOCK_FRAGMENTS)


def _is_memory_address_net_name(net_name: str) -> bool:
    lowered = net_name.lower()
    return any(pattern in lowered for pattern in _MEMORY_ADDRESS_PATTERNS)


def _is_hard_macro_pin_name(net_name: str) -> bool:
    leaf = net_name.rsplit("/", 1)[-1].upper()
    leaf = re.sub(r"(?:\[\d+\])+$", "", leaf)
    return any(re.fullmatch(pattern, leaf) for pattern in _HARD_MACRO_LEAF_PATTERNS)


def _is_actionable_fanout_net(net_name: str, fanout: int) -> bool:
    if fanout <= 1:
        return False
    if _is_clock_like_net_name(net_name):
        return False
    if _is_memory_address_net_name(net_name):
        return False
    if _is_hard_macro_pin_name(net_name):
        return False
    return True


def _lut_utilization(util: dict) -> Optional[float]:
    for key in ("lut_util_pct", "lut_pct", "clb_lut_pct", "clb_luts_pct"):
        if key in util:
            return _as_float(util[key])
    used = util.get("luts") or util.get("clb_luts") or util.get("lut")
    total = util.get("available_luts") or util.get("total_luts")
    used_f = _as_float(used, -1.0)
    total_f = _as_float(total, 0.0)
    if used_f >= 0.0 and total_f > 0.0:
        return 100.0 * used_f / total_f
    return None


def _hard_macro_present(util: dict, profile: dict) -> bool:
    macro_keys = ("dsp", "dsps", "bram", "brams", "uram", "urams")
    if any(_as_int(util.get(key)) > 0 for key in macro_keys):
        return True
    site_counts = ((profile.get("placement_spread") or {}).get("site_type_counts") or {})
    for site_type, count in site_counts.items():
        if str(site_type).upper() != "SLICE" and _as_int(count) > 0:
            return True
    return False


def _route_ratio(profile: dict) -> float:
    path_count = _as_int(profile.get("path_count"))
    route_paths = _as_int(profile.get("route_dominated_paths"))
    if path_count <= 0:
        return 0.0
    return route_paths / path_count


def _spread_ratio_and_score(profile: dict) -> tuple[float, float]:
    spread = profile.get("placement_spread") or {}
    spread_paths = _as_int(spread.get("spread_path_count"))
    site_paths = _as_int(spread.get("path_count_with_sites"))
    ratio = spread_paths / site_paths if site_paths else 0.0
    span_x = _as_float(spread.get("bbox_span_x"))
    span_y = _as_float(spread.get("bbox_span_y"))
    max_path_x = _as_float(spread.get("max_path_span_x"))
    max_path_y = _as_float(spread.get("max_path_span_y"))
    score = max(ratio * 100.0, span_x + span_y, max_path_x + max_path_y)
    return ratio, score


def extract_features(analysis: dict, design_id: Optional[str] = None) -> DesignFeatures:
    profile = analysis.get("timing_profile") or {}
    util = analysis.get("utilization") or {}
    fanout_nets = analysis.get("fanout_nets") or []

    fanouts = []
    actionable = []
    memory_address_seen = False
    for item in fanout_nets:
        try:
            name, fanout, _paths = item
        except (TypeError, ValueError):
            continue
        fanout = _as_int(fanout)
        fanouts.append(fanout)
        if _is_memory_address_net_name(str(name)):
            memory_address_seen = True
        if _is_actionable_fanout_net(str(name), fanout):
            actionable.append((str(name), fanout))

    eligible_hf = [item for item in actionable if item[1] >= 30]
    broad_hf = [item for item in actionable if item[1] >= 500]
    spread_ratio, spread_score = _spread_ratio_and_score(profile)
    max_logic = _as_int((profile.get("logic_levels") or {}).get("max"))
    avg_route = _as_float(profile.get("avg_route_pct"))

    lut_count = _as_int(util.get("luts") or util.get("clb_luts") or util.get("lut"))
    ff_count = _as_int(util.get("ffs") or util.get("ff") or util.get("registers"))
    dsp_count = _as_int(util.get("dsp") or util.get("dsps"))
    bram_count = _as_int(util.get("bram") or util.get("brams"))
    uram_count = _as_int(util.get("uram") or util.get("urams"))

    return DesignFeatures(
        design_id=str(design_id or ""),
        wns_ns=_as_float(
            analysis.get("baseline_wns"),
            _as_float(profile.get("worst_slack_ns")),
        ),
        clock_period_ns=(
            _as_float(analysis.get("clock_period"))
            if analysis.get("clock_period") is not None else None
        ),
        lut_count=lut_count,
        ff_count=ff_count,
        dsp_count=dsp_count,
        bram_count=bram_count,
        uram_count=uram_count,
        lut_util_pct=_lut_utilization(util),
        avg_route_pct=avg_route,
        route_dominated_ratio=_route_ratio(profile),
        max_logic_levels=max_logic,
        dominant_bottleneck=str(profile.get("dominant_bottleneck") or "").lower(),
        max_fanout=max(fanouts, default=0),
        max_actionable_fanout=max((fanout for _name, fanout in actionable), default=0),
        eligible_hf_count=len(eligible_hf),
        broad_hf_count=len(broad_hf),
        hard_macro_present=_hard_macro_present(util, profile),
        spread_ratio=spread_ratio,
        spread_score=spread_score,
        pre_route_to_routed_gap_mhz=_as_float(analysis.get("pre_route_to_routed_gap_mhz")),
        memory_address_guidance_profile=(
            memory_address_seen
            and not broad_hf
            and max_logic >= 7
            and avg_route >= 45.0
        ),
    )


def _decision(
    recipe_id: str,
    confidence: float,
    reason: str,
    skill_order: list[str],
    *,
    initial_params: Optional[dict[str, dict]] = None,
    core_skills: Optional[list[str]] = None,
    budget_hint_s: int = 3600,
    bottleneck: str = "mixed",
    equivalence_risk: str = "low",
    learned_fmax_mhz: Optional[float] = None,
    knowledge_evidence: Optional[list[dict[str, str]]] = None,
) -> RecipeDecision:
    return RecipeDecision(
        recipe_id=recipe_id,
        confidence=confidence,
        reason=reason,
        skill_order=skill_order,
        initial_params=initial_params or {},
        core_skills=core_skills or skill_order[:1],
        budget_hint_s=budget_hint_s,
        bottleneck=bottleneck,
        equivalence_risk=equivalence_risk,
        learned_fmax_mhz=learned_fmax_mhz,
        knowledge_evidence=knowledge_evidence or [],
    )


_KNOWLEDGE_EVIDENCE_FIELDS = (
    "method",
    "variant",
    "tendency",
    "fmax_mhz",
    "runtime_s",
    "route_status",
    "hold_status",
    "pulse_status",
    "provenance",
    "confidence",
    "action",
    "notes",
)


def normalize_knowledge_design_id(design_id: object) -> str:
    name = Path(str(design_id or "")).name.lower()
    if name.endswith(".dcp"):
        name = name[:-4]
    name = re.sub(r"(?:_optimized)?(?:[_-]?2025\.1)?$", "", name)
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_")


def _normalize_knowledge_design_id(design_id: object) -> str:
    return normalize_knowledge_design_id(design_id)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def excluded_knowledge_design_ids_from_manifest(path: Path | str) -> set[str]:
    manifest_path = Path(path).expanduser()
    if not manifest_path.exists():
        return set()
    excluded: set[str] = set()
    with manifest_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not (
                _truthy(row.get("excluded_from_optimization"))
                or str(row.get("dataset", "")).strip().lower() == "test"
            ):
                continue
            for field in ("target", "input_dcp"):
                design_id = normalize_knowledge_design_id(row.get(field, ""))
                if design_id:
                    excluded.add(design_id)
    return excluded


def excluded_knowledge_design_ids_from_env() -> set[str]:
    excluded: set[str] = set()
    explicit = os.environ.get("FDAGENTS_KNOWLEDGE_EXCLUDED_IDS", "")
    for token in re.split(r"[\s,;]+", explicit):
        design_id = normalize_knowledge_design_id(token)
        if design_id:
            excluded.add(design_id)

    manifest = os.environ.get("FDAGENTS_KNOWLEDGE_EXCLUDE_MANIFEST")
    if manifest:
        excluded.update(excluded_knowledge_design_ids_from_manifest(manifest))
    return excluded


def _knowledge_row_is_excluded(row: dict, excluded_design_ids: set[str]) -> bool:
    if not excluded_design_ids:
        return False
    return normalize_knowledge_design_id(row.get("dcp", "")) in excluded_design_ids


def _knowledge_row_is_guardrail(row: dict) -> bool:
    text = " ".join(
        str(row.get(key, "")).lower()
        for key in ("tendency", "provenance", "action", "notes")
    )
    return any(
        marker in text
        for marker in (
            "negative",
            "invalid",
            "reject",
            "avoid",
            "do-not",
            "do not",
            "forensics",
            "offline-only",
            "mine-only",
            "do-not-repeat",
            "do-not-promote",
            "do-not-score",
        )
    )


def _knowledge_row_is_actionable(row: dict) -> bool:
    text = " ".join(
        str(row.get(key, "")).lower()
        for key in ("tendency", "provenance", "action", "notes")
    )
    return _knowledge_row_is_guardrail(row) or any(
        marker in text
        for marker in (
            "positive",
            "promote",
            "use",
            "keep",
            "consider",
            "repeat-needed",
            "repeat",
            "late-only",
            "single_run_clean",
        )
    )


def _knowledge_row_rank(row: dict) -> tuple[int, float]:
    action = str(row.get("action", "")).lower()
    text = " ".join(
        str(row.get(key, "")).lower()
        for key in ("tendency", "provenance", "action", "notes")
    )
    if "invalid" in text or "do-not-score" in text or "do not score" in text:
        category = 0
    elif "mine-only" in text or "offline_ceiling_only" in text:
        category = 1
    elif "do-not-repeat" in text or "do not repeat" in text:
        category = 2
    elif "do-not-promote" in text or "do not promote" in text:
        category = 3
    elif _knowledge_row_is_guardrail(row):
        category = 4
    elif "repeat" in action:
        category = 5
    else:
        category = 6
    try:
        fmax = float(row.get("fmax_mhz") or 0.0)
    except (TypeError, ValueError):
        fmax = 0.0
    return category, -fmax


def load_knowledge_tendencies_csv(path: Path | str) -> list[dict[str, str]]:
    csv_path = Path(path).expanduser()
    with csv_path.open(newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _default_knowledge_tendencies_paths() -> list[Path]:
    paths: list[Path] = []
    explicit_csv = os.environ.get("FDAGENTS_KNOWLEDGE_TENDENCIES_CSV")
    if explicit_csv:
        paths.append(Path(explicit_csv).expanduser())

    repo = os.environ.get("FDAGENTS_KNOWLEDGE_REPO")
    if repo:
        paths.append(Path(repo).expanduser() / "evidence" / "dcp_method_tendencies.csv")

    paths.extend(
        [
            Path.cwd() / "knowledge" / "evidence" / "dcp_method_tendencies.csv",
            Path.cwd().parent / "Agentic_Contest_Knowledge" / "evidence" / "dcp_method_tendencies.csv",
            Path.home() / "Agentic_Contest_Knowledge" / "evidence" / "dcp_method_tendencies.csv",
        ]
    )
    return paths


@lru_cache(maxsize=1)
def load_default_knowledge_rows() -> tuple[dict[str, str], ...]:
    excluded_design_ids = excluded_knowledge_design_ids_from_env()
    for path in _default_knowledge_tendencies_paths():
        if not path.exists():
            continue
        try:
            rows = load_knowledge_tendencies_csv(path)
            if excluded_design_ids:
                rows = [
                    row for row in rows
                    if not _knowledge_row_is_excluded(row, excluded_design_ids)
                ]
            return tuple(rows)
        except OSError:
            continue
    return ()


def knowledge_evidence_for_design(
    design_id: Optional[str],
    knowledge_rows: Optional[list[dict[str, str]] | tuple[dict[str, str], ...]] = None,
    *,
    limit: int = 6,
) -> list[dict[str, str]]:
    if not design_id or not knowledge_rows:
        return []

    excluded_design_ids = excluded_knowledge_design_ids_from_env()
    design_key = _normalize_knowledge_design_id(design_id)
    if design_key in excluded_design_ids:
        return []
    selected = [
        row for row in knowledge_rows
        if _normalize_knowledge_design_id(row.get("dcp", "")) == design_key
        and not _knowledge_row_is_excluded(row, excluded_design_ids)
        and _knowledge_row_is_actionable(row)
    ]
    guardrails = [row for row in selected if _knowledge_row_is_guardrail(row)]
    positives = [row for row in selected if not _knowledge_row_is_guardrail(row)]
    guardrails.sort(key=_knowledge_row_rank)
    positives.sort(key=_knowledge_row_rank)

    row_limit = max(0, int(limit))
    if positives and guardrails:
        guard_limit = min(len(guardrails), max(1, row_limit // 2))
        selected = guardrails[:guard_limit] + positives[: row_limit - guard_limit]
    else:
        selected = (guardrails or positives)[:row_limit]

    evidence = []
    for row in selected:
        compact = {
            key: str(row.get(key, "")).strip()
            for key in _KNOWLEDGE_EVIDENCE_FIELDS
            if str(row.get(key, "")).strip()
        }
        if compact:
            evidence.append(compact)
    return evidence


def _fanout_split_factor(features: DesignFeatures) -> int:
    if features.max_actionable_fanout > 1500:
        return 5
    if features.max_actionable_fanout > 500:
        return 4
    if features.max_actionable_fanout >= 100:
        return 3
    return 2


def _route_dominated(features: DesignFeatures) -> bool:
    return (
        features.dominant_bottleneck == "routing"
        or features.avg_route_pct >= 65.0
        or features.route_dominated_ratio >= 0.65
    )


def _low_util(features: DesignFeatures) -> bool:
    return features.lut_util_pct is not None and features.lut_util_pct < 30.0


def _unknown_or_low_util(features: DesignFeatures) -> bool:
    return features.lut_util_pct is None or features.lut_util_pct < 30.0


def _has_actionable_fanout_recipe(features: DesignFeatures) -> bool:
    return features.broad_hf_count >= 1 and features.max_actionable_fanout >= 500


def _has_medium_fanout_recipe(features: DesignFeatures) -> bool:
    return (
        features.eligible_hf_count >= 2
        and features.max_actionable_fanout >= 64
        and (
            "fanout" in features.dominant_bottleneck
            or features.avg_route_pct >= 70.0
            or features.route_dominated_ratio >= 0.70
        )
    )


def _large_no_macro_grouped_replication_profile(features: DesignFeatures) -> bool:
    return (
        not features.hard_macro_present
        and features.lut_count >= 200000
        and (features.lut_util_pct is None or features.lut_util_pct <= 65.0)
        and features.wns_ns < 0.0
        and (
            features.max_actionable_fanout >= 400
            or (
                "fanout" in features.dominant_bottleneck
                and features.avg_route_pct >= 80.0
                and features.max_logic_levels >= 10
                and features.spread_ratio >= 0.50
            )
        )
        and (
            "fanout" in features.dominant_bottleneck
            or features.avg_route_pct >= 65.0
            or features.route_dominated_ratio >= 0.65
        )
    )


def _low_util_route_fanout_floorplan_profile(features: DesignFeatures) -> bool:
    route_fanout_profile = (
        "fanout" in features.dominant_bottleneck
        or (
            features.dominant_bottleneck == "routing"
            and features.max_logic_levels >= 6
        )
    )
    return (
        _low_util(features)
        and not features.hard_macro_present
        and features.wns_ns < 0.0
        and route_fanout_profile
        and features.max_actionable_fanout >= 96
        and features.avg_route_pct >= 65.0
        and features.route_dominated_ratio >= 0.65
        and features.broad_hf_count <= 10
    )


def _high_util_deep_route_profile(features: DesignFeatures) -> bool:
    return (
        features.lut_util_pct is not None
        and features.lut_util_pct >= 45.0
        and features.hard_macro_present
        and features.wns_ns <= -5.0
        and features.avg_route_pct >= 70.0
        and features.max_logic_levels >= 12
        and features.eligible_hf_count >= 2
        and features.max_actionable_fanout >= 60
    )


def _shallow_hard_macro_route_profile(features: DesignFeatures) -> bool:
    return (
        _low_util(features)
        and features.hard_macro_present
        and features.wns_ns <= -0.5
        and features.avg_route_pct >= 80.0
        and features.route_dominated_ratio >= 0.80
        and features.max_logic_levels <= 4
        and features.broad_hf_count <= 1
        and features.max_actionable_fanout < 1500
        and features.spread_ratio >= 0.20
    )


def _very_high_util_deep_logic_backend_profile(features: DesignFeatures) -> bool:
    return (
        features.lut_util_pct is not None
        and features.lut_util_pct >= 85.0
        and features.hard_macro_present
        and features.wns_ns <= -5.0
        and features.max_logic_levels >= 30
        and features.avg_route_pct >= 45.0
        and features.route_dominated_ratio >= 0.50
        and features.max_actionable_fanout >= 64
        and features.broad_hf_count < 50
    )


def _achieved_period_ns(features: DesignFeatures) -> Optional[float]:
    if features.clock_period_ns is None:
        return None
    achieved = features.clock_period_ns - features.wns_ns
    if achieved <= 0.0:
        return None
    return achieved


def _clock_tighten_candidate(features: DesignFeatures) -> bool:
    achieved = _achieved_period_ns(features)
    if achieved is None:
        return False

    # Positive slack means the DCP is underconstrained relative to the routed
    # implementation; a clock-period sweep is the most direct way to expose the
    # true Fmax to implementation.
    if features.wns_ns >= 0.05:
        return True

    baseline_fmax = 1000.0 / achieved
    return (
        -1.25 <= features.wns_ns < 0.05
        and baseline_fmax >= 250.0
        and features.max_logic_levels <= 6
        and features.max_actionable_fanout < 500
        and features.avg_route_pct <= 55.0
    )


def _compact_clock_tighten_profile(features: DesignFeatures) -> bool:
    if features.wns_ns < 0.35:
        return False
    if features.lut_util_pct is not None and features.lut_util_pct > 8.0:
        return False
    return features.max_logic_levels <= 6


def _clock_tighten_params_for_features(features: DesignFeatures) -> dict:
    if _compact_clock_tighten_profile(features):
        return dict(COMPACT_CLOCK_TIGHTEN_PARAMS)
    return dict(CLOCK_TIGHTEN_PARAMS)


def _datuner_directive_clock_profile(features: DesignFeatures) -> bool:
    if features.clock_period_ns is None:
        return False
    if features.lut_count <= 0:
        return False
    achieved = _achieved_period_ns(features)
    if achieved is None:
        return False
    achieved_fmax = 1000.0 / achieved if achieved > 0 else 0.0
    if features.wns_ns < -0.35 and not (
        features.wns_ns >= -2.25
        and achieved_fmax >= 120.0
        and features.max_logic_levels <= 12
    ):
        return False
    if not (
        _route_dominated(features)
        or features.avg_route_pct >= 45.0
        or features.max_logic_levels >= 6
        or features.hard_macro_present
    ):
        return False
    if features.lut_util_pct is not None and features.lut_util_pct > 75.0:
        return False
    return True


def _datuner_clock_params_for_features(features: DesignFeatures) -> Optional[dict]:
    if not _datuner_directive_clock_profile(features):
        return None

    if not features.hard_macro_present:
        if (
            20000 <= features.lut_count < 80000
            and features.wns_ns >= -0.5
            and (features.lut_util_pct is None or features.lut_util_pct <= 30.0)
        ):
            return dict(DATUNER_NO_MACRO_MEDIUM_DEFAULT_RETIME_EXPLORE_PARAMS)
        if features.lut_count >= 200000:
            return dict(DATUNER_HUGE_LOGIC_SPREAD_CLOCK_TIGHTEN_PARAMS)
        if features.lut_count and features.lut_count <= 10000:
            return dict(DATUNER_SMALL_LOGIC_CLOCK_TIGHTEN_PARAMS)
        if features.lut_count >= 20000:
            return dict(DATUNER_SLICE_XBAR_CLOCK_TIGHTEN_PARAMS)
        return dict(DATUNER_SMALL_LOGIC_CLOCK_TIGHTEN_PARAMS)

    if (
        features.dsp_count >= 100
        and features.bram_count >= 8
        and 8000 <= features.lut_count <= 20000
    ):
        return dict(DATUNER_DSP_SPREAD_RETIME_AGGRESSIVE_PARAMS)
    if features.dsp_count >= 100 and features.lut_count <= 10000:
        return dict(DATUNER_DSP_COMPACT_CLOCK_TIGHTEN_PARAMS)
    if (
        features.uram_count > 0
        and features.dsp_count > 0
        and 50000 <= features.lut_count < 70000
    ):
        return dict(DATUNER_GENERATED_FFT_L_CLOCK_TIGHTEN_PARAMS)
    if features.bram_count >= 300 and features.dsp_count == 0:
        return dict(DATUNER_BRAM_HEAVY_SPREAD_CLOCK_TIGHTEN_PARAMS)
    if features.bram_count >= 100 and features.dsp_count == 0 and features.lut_count < 120000:
        return dict(DATUNER_BRAM_ROUTE_SPREAD_CLOCK_TIGHTEN_PARAMS)
    if features.uram_count > 0 and features.lut_count >= 60000:
        return dict(DATUNER_WIDE_MACRO_CLOCK_TIGHTEN_PARAMS)
    if features.lut_count >= 40000:
        return dict(DATUNER_MID_MACRO_CLOCK_TIGHTEN_PARAMS)
    return dict(DATUNER_COMPACT_MACRO_CLOCK_TIGHTEN_PARAMS)


def plan_recipe(
    analysis: dict,
    design_id: Optional[str] = None,
    knowledge_rows: Optional[list[dict[str, str]] | tuple[dict[str, str], ...]] = None,
) -> Optional[RecipeDecision]:
    features = extract_features(analysis, design_id=design_id)
    name = _design_name(design_id)
    knowledge_evidence = knowledge_evidence_for_design(design_id, knowledge_rows)

    def decide(*args, **kwargs) -> RecipeDecision:
        kwargs.setdefault("knowledge_evidence", knowledge_evidence)
        return _decision(*args, **kwargs)

    if _large_no_macro_grouped_replication_profile(features):
        return decide(
            "C_GROUPED_FANOUT_REPLICATION",
            0.82,
            "large no-macro fanout/route profile favors grouped replication before full directive clock sweeps",
            [
                "force_replicate",
                "phys_opt",
                "post_route_cleanup",
                "critical_net_reroute",
                "pblock",
                "fresh_place_route",
                "pblock_sweep",
            ],
            initial_params={"force_replicate": GROUP_FORCE_REPLICATE_PARAMS},
            core_skills=["force_replicate"],
            budget_hint_s=3600,
            bottleneck="fanout",
        )

    datuner_clock_params = _datuner_clock_params_for_features(features)
    if datuner_clock_params is not None:
        return decide(
            "DATUNER_DIRECTIVE_CLOCK_SWEEP",
            0.86,
            "near-closed low/medium-utilization profile matches DATuner evidence: tighten clock and run one full opt/place/phys/route directive recipe with MAX_FANOUT property",
            [
                "clock_tighten",
                "post_route_cleanup",
                "fresh_place_route",
                "phys_opt",
                "critical_net_reroute",
                "pblock_sweep",
                "force_replicate",
            ],
            initial_params={"clock_tighten": datuner_clock_params},
            core_skills=["clock_tighten"],
            budget_hint_s=3600,
            bottleneck="clock_constraint",
        )

    if features.pre_route_to_routed_gap_mhz > 5.0:
        return decide(
            "F_TIMING_PRESERVING_ROUTE",
            0.90,
            "pre-route/routed gap favors route-preserving cleanup",
            [
                "post_route_cleanup",
                "critical_net_reroute",
                "endpoint_bel_move",
                "fresh_place_route",
                "phys_opt",
                "pblock",
                "pblock_sweep",
            ],
            initial_params={
                "fresh_place_route": FRESH_ENDPOINT_PARAMS,
                "endpoint_bel_move": {"num_paths": 20, "max_candidates": 6},
            },
            core_skills=["post_route_cleanup", "critical_net_reroute", "endpoint_bel_move"],
            budget_hint_s=1800,
            bottleneck="routing",
        )

    if _low_util_route_fanout_floorplan_profile(features):
        return decide(
            "ROUTE_FANOUT_FLOORPLAN_THEN_REFINE",
            0.90,
            "low-utilization route/fanout profile favors floorplan seed, fanout/refine, and late route polish",
            [
                "pblock_sweep",
                "fanout_opt",
                "critical_net_reroute",
                "post_route_cleanup",
                "lut_pin_swap",
                "phys_opt",
                "force_replicate",
                "fresh_place_route",
                "pblock",
            ],
            initial_params={
                "force_replicate": TARGETED_FORCE_REPLICATE_PARAMS,
                "fanout_opt": ROUTE_FANOUT_FLOORPLAN_FANOUT_PARAMS,
                "pblock_sweep": ROUTE_FANOUT_FLOORPLAN_SWEEP_PARAMS,
                "critical_net_reroute": ROUTE_FANOUT_FLOORPLAN_REROUTE_PARAMS,
                "lut_pin_swap": {
                    # Keep the raw 1h run from spending the final budget on
                    # low-yield route variants; seq14 showed Explore/Default
                    # rarely win while AggressiveExplore and HigherDelayCost
                    # cover the useful LUT-swap outcomes.
                    "route_directives": [
                        "AggressiveExplore",
                        "HigherDelayCost",
                    ],
                    "max_pins": 8,
                    "max_cells": 3,
                    "max_iterations": 2,
                },
                "post_route_cleanup": ROUTE_FANOUT_FLOORPLAN_CLEANUP_PARAMS,
                "phys_opt": {
                    "directive_order": ["Explore", "AggressiveExplore", "AddRetime", "Default"]
                },
            },
            core_skills=["pblock_sweep"],
            budget_hint_s=3600,
            bottleneck="placement",
        )

    if _high_util_deep_route_profile(features):
        return decide(
            "HIGH_UTIL_TARGET_PERIOD_REPLACE_ROUTE_PRIOR",
            0.88,
            "high-utilization deep route/fanout profile matches manual target-period evidence; start with a compressed 5ns replace-route clock-tighten attempt before any global replication",
            [
                "clock_tighten",
                "fresh_place_route",
                "force_replicate",
                "post_route_cleanup",
                "phys_opt",
                "critical_net_reroute",
                "fanout_opt",
            ],
            initial_params={
                "clock_tighten": HIGH_UTIL_TARGET_PERIOD_CLOCK_TIGHTEN_PARAMS,
                "force_replicate": HIGH_UTIL_DEEP_ROUTE_FORCE_REPLICATE_PARAMS,
                "fresh_place_route": HIGH_UTIL_DEEP_ROUTE_FRESH_PARAMS,
                "post_route_cleanup": HIGH_UTIL_DEEP_ROUTE_CLEANUP_PARAMS,
                "phys_opt": {
                    "directive_order": [
                        "AggressiveFanoutOpt",
                        "AggressiveExplore",
                        "Default",
                    ]
                },
            },
            core_skills=[
                "clock_tighten",
                "fresh_place_route",
                "force_replicate",
            ],
            budget_hint_s=3600,
            bottleneck="fanout",
            learned_fmax_mhz=96.90861517588914,
        )

    if _clock_tighten_candidate(features):
        return decide(
            "CLOCK_TIGHTEN_SWEEP",
            0.88 if features.wns_ns >= 0.05 else 0.80,
            "clock period/WNS imply a measured achievable period suitable for bounded clock-tighten sweep",
            [
                "clock_tighten",
                "fresh_place_route",
                "phys_opt",
                "post_route_cleanup",
                "critical_net_reroute",
                "pblock_sweep",
            ],
            initial_params={"clock_tighten": _clock_tighten_params_for_features(features)},
            core_skills=["clock_tighten"],
            budget_hint_s=1800,
            bottleneck="clock_constraint",
        )

    if features.memory_address_guidance_profile:
        return decide(
            "ROUTE_PRESERVING_GUIDANCE_PBLOCK",
            0.82,
            "memory-address fanout profile favors guidance pblock seed then endpoint-aware fresh route",
            [
                "pblock",
                "fresh_place_route",
                "endpoint_bel_move",
                "phys_opt",
                "post_route_cleanup",
                "critical_net_reroute",
                "force_replicate",
                "pblock_sweep",
            ],
            initial_params={
                "pblock": ROUTE_PRESERVING_GUIDANCE_PBLOCK_PARAMS,
                "fresh_place_route": FRESH_ENDPOINT_PARAMS,
            },
            core_skills=["pblock", "fresh_place_route"],
            budget_hint_s=2400,
            bottleneck="mixed",
        )

    if features.broad_hf_count >= 50:
        return decide(
            "B_BROAD_FANOUT",
            0.82,
            "many broad high-fanout nets point to fanout optimization",
            [
                "fanout_opt",
                "force_replicate",
                "fresh_place_route",
                "phys_opt",
                "post_route_cleanup",
                "pblock_sweep",
            ],
            initial_params={"fanout_opt": {"split_factor": _fanout_split_factor(features)}},
            core_skills=["fanout_opt"],
            budget_hint_s=3600,
            bottleneck="fanout",
        )

    if 2 <= features.broad_hf_count <= 10:
        return decide(
            "C_TARGETED_REPLICATION",
            0.83,
            "small number of actionable critical high-fanout nets favors targeted replication",
            [
                "force_replicate",
                "fanout_opt",
                "fresh_place_route",
                "phys_opt",
                "post_route_cleanup",
                "pblock_sweep",
            ],
            initial_params={
                "force_replicate": TARGETED_FORCE_REPLICATE_PARAMS,
                "fanout_opt": {"split_factor": _fanout_split_factor(features)},
            },
            core_skills=["force_replicate"],
            budget_hint_s=2400,
            bottleneck="fanout",
        )

    if _very_high_util_deep_logic_backend_profile(features):
        return decide(
            "VERY_HIGH_UTIL_BACKEND_RETIME_CLEANUP",
            0.80,
            "extreme LUT utilization and deep logic leave too little LUT headroom for path-local reflow; try backend cleanup and retime first",
            [
                "post_route_cleanup",
                "phys_opt",
                "critical_net_reroute",
                "fresh_place_route",
                "force_replicate",
                "pblock",
                "pblock_sweep",
            ],
            initial_params={
                "phys_opt": VERY_HIGH_UTIL_BACKEND_RETIME_PHYS_PARAMS,
                "post_route_cleanup": VERY_HIGH_UTIL_BACKEND_RETIME_CLEANUP_PARAMS,
                "force_replicate": GROUP_FORCE_REPLICATE_PARAMS,
            },
            core_skills=["post_route_cleanup", "phys_opt"],
            budget_hint_s=2400,
            bottleneck="mixed",
        )

    if _shallow_hard_macro_route_profile(features):
        return decide(
            "SHALLOW_HARD_MACRO_ROUTE_DIRECTIVE_SWEEP",
            0.84,
            "low-utilization shallow hard-macro route-dominated profile favors early full re-place/re-route before grouped replication",
            [
                "fresh_place_route",
                "post_route_cleanup",
                "critical_net_reroute",
                "phys_opt",
                "force_replicate",
                "pblock",
                "pblock_sweep",
            ],
            initial_params={"fresh_place_route": SHALLOW_HARD_MACRO_ROUTE_FRESH_PARAMS},
            core_skills=["fresh_place_route"],
            budget_hint_s=2400,
            bottleneck="routing",
        )

    if _has_medium_fanout_recipe(features):
        return decide(
            "C_GROUPED_FANOUT_REPLICATION",
            0.81,
            "fanout/route dominated profile with medium critical fanouts favors grouped replication",
            [
                "force_replicate",
                "phys_opt",
                "post_route_cleanup",
                "critical_net_reroute",
                "pblock",
                "fresh_place_route",
                "pblock_sweep",
            ],
            initial_params={"force_replicate": GROUP_FORCE_REPLICATE_PARAMS},
            core_skills=["force_replicate"],
            budget_hint_s=2400,
            bottleneck="fanout",
        )

    if (
        _unknown_or_low_util(features)
        and features.wns_ns < -1.0
        and (features.avg_route_pct >= 50.0 or features.spread_ratio >= 0.15)
        and not _has_actionable_fanout_recipe(features)
    ):
        params = {}
        if features.hard_macro_present:
            params["pblock"] = HARD_MACRO_GUIDANCE_PBLOCK_PARAMS
        return decide(
            "A_HARD_PBLOCK",
            0.80 if _low_util(features) else 0.74,
            "low-utilization route/placement-bound design is a hard-pblock candidate",
            [
                "pblock",
                "phys_opt",
                "post_route_cleanup",
                "fresh_place_route",
                "force_replicate",
                "pblock_sweep",
            ],
            initial_params=params,
            core_skills=["pblock"],
            budget_hint_s=3600,
            bottleneck="placement",
        )

    if (
        features.dominant_bottleneck == "placement"
        and features.spread_ratio >= 0.15
        and not _has_actionable_fanout_recipe(features)
    ):
        return decide(
            "D_CLUSTER_ANCHOR",
            0.78,
            "critical cells show placement spread without a broad fanout recipe",
            [
                "critical_cluster_anchor",
                "pblock_sweep",
                "pblock",
                "fresh_place_route",
                "phys_opt",
                "post_route_cleanup",
            ],
            initial_params={"critical_cluster_anchor": CLUSTER_ANCHOR_PARAMS},
            core_skills=["critical_cluster_anchor"],
            budget_hint_s=2400,
            bottleneck="placement",
        )

    if (
        -2.0 <= features.wns_ns < -0.5
        and features.max_logic_levels <= 4
        and features.max_actionable_fanout < 500
        and not _route_dominated(features)
    ):
        return decide(
            "E_DIRECTIVE_SWEEP",
            0.76,
            "moderate WNS without a strong structural bottleneck favors directive sweep",
            [
                "fresh_place_route",
                "phys_opt",
                "post_route_cleanup",
                "critical_net_reroute",
                "pblock_sweep",
            ],
            initial_params={"fresh_place_route": DIRECTIVE_SWEEP_PARAMS},
            core_skills=["fresh_place_route"],
            budget_hint_s=2400,
            bottleneck="mixed",
        )

    return None
