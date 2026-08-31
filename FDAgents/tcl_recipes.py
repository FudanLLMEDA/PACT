"""Reusable Vivado Tcl recipe builders.

These are internal implementations selected by typed code registries. The
legacy ``resolve_tcl_refs`` helper remains for offline migration only;
production knowledge loading rejects Tcl references.

All recipes are equivalence-safe: they only re-place/re-route/replicate,
never re-synthesize or edit logic.
"""

from __future__ import annotations

# Critical-net collection used by several recipes: harvest start/endpoint
# nets of the worst setup paths, excluding clock trees.
_COLLECT_CRITICAL_NETS = (
    "set fdagents_nets {{}}; "
    "foreach tp [get_timing_paths -quiet -setup -max_paths {max_paths} -nworst 1] "
    "{{ foreach prop {{STARTPOINT_PIN ENDPOINT_PIN}} {{ "
    "set pin [get_property $prop $tp]; "
    "if {{$pin eq {{}}}} {{ continue }}; "
    "foreach n [get_nets -quiet -of_objects $pin] {{ "
    "set nname [get_property NAME $n]; "
    "if {{[regexp -nocase {{(^|/)(clk|clock)(/|$)|clock_uncore_clock|bufg|mmcm|pll}} $nname]}} {{ continue }}; "
    "set fanout 0; catch {{set fanout [get_property FLAT_PIN_COUNT $n]}}; "
    "if {{$fanout >= 2}} {{ lappend fdagents_nets $n }} }} }} }}; "
    "set nets [lrange [lsort -unique $fdagents_nets] 0 {net_limit}]; "
)

_TIMED_PROLOGUE = (
    "set_param general.maxThreads 8; "
    "set fdagents_t0 [clock seconds]; "
    "proc fdagents_elapsed {} { expr {[clock seconds] - $::fdagents_t0} }; "
    "proc fdagents_mark {msg} "
    "{ puts \"FDAGENTS_TARGET_PERIOD_STEP $msg elapsed=[fdagents_elapsed]\"; flush stdout }; "
)

_CHECKPOINT_GUARD = (
    "if {{[info exists fdagents_candidate_checkpoint] && $fdagents_candidate_checkpoint ne {{}}}} {{ "
    "if {{![catch {{write_checkpoint -force $fdagents_candidate_checkpoint}}]}} {{ fdagents_mark {mark} }} }}; "
)


def datuner_directive_command(
    *,
    opt_directive: str,
    fanout_limit: int,
    place_directive: str,
    phys_opt_directive: str,
    route_directive: str,
) -> str:
    """DATuner-style full re-implementation with a MAX_FANOUT property."""
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


def _target_period_route_command(*, pre_route_phys_opt: bool) -> str:
    collect = _COLLECT_CRITICAL_NETS.format(max_paths=80, net_limit=31)
    pre_phys = ""
    if pre_route_phys_opt:
        pre_phys = (
            "if {[fdagents_elapsed] < 1700} { fdagents_mark pre_aggressive_fanout; "
            "catch {phys_opt_design -directive AggressiveFanoutOpt} }; "
            "if {[fdagents_elapsed] < 1700} { fdagents_mark pre_aggressive_explore; "
            "catch {phys_opt_design -directive AggressiveExplore} }; "
        )
    return (
        _TIMED_PROLOGUE
        + "fdagents_mark unroute; route_design -unroute; "
        + "fdagents_mark unplace; place_design -unplace; "
        + "fdagents_mark opt_explore; catch {opt_design -directive Explore}; "
        + "fdagents_mark place_extratim; catch {place_design -directive ExtraTimingOpt}; "
        + collect
        + "puts \"FDAGENTS_TARGET_PERIOD_NETS [llength $nets]\"; "
        + "if {[llength $nets] > 0} { fdagents_mark force_replication; "
        + "catch {phys_opt_design -force_replication_on_nets $nets} }; "
        + pre_phys
        + "fdagents_mark route_altclb; route_design -directive AlternateCLBRouting; "
        + _CHECKPOINT_GUARD.format(mark="checkpoint_after_route")
        + "if {[fdagents_elapsed] < 3330} { fdagents_mark post_critical_pin; "
        + "catch {phys_opt_design -critical_pin_opt}; "
        + _CHECKPOINT_GUARD.format(mark="checkpoint_after_post_critical_pin")
        + "}; "
        + "if {[fdagents_elapsed] < 3330} { fdagents_mark post_clock_opt; "
        + "catch {phys_opt_design -clock_opt}; "
        + _CHECKPOINT_GUARD.format(mark="checkpoint_after_post_clock_opt")
        + "}; "
        + "fdagents_mark done"
    )


def high_util_target_period_fast_route_command() -> str:
    return _target_period_route_command(pre_route_phys_opt=False)


def high_util_target_period_replace_route_command() -> str:
    return _target_period_route_command(pre_route_phys_opt=True)


def full_unplace_retime_route_command() -> str:
    return (
        "route_design -unroute; "
        "place_design -unplace; "
        "catch {opt_design -directive Explore}; "
        "catch {place_design -directive ExtraTimingOpt}; "
        "foreach d {AlternateFlowWithRetiming AggressiveExplore Explore} "
        "{ catch {phys_opt_design -directive $d} }; "
        "if {[catch {route_design -directive AlternateCLBRouting -tns_cleanup}]} "
        "{ route_design -directive NoTimingRelaxation -tns_cleanup }; "
        "foreach d {AggressiveExplore Explore} { catch {phys_opt_design -directive $d} }"
    )


def extreme_clock_pressure_command() -> str:
    """Re-implement under the pressure period owned by the typed preset."""
    return (
        "route_design -unroute; "
        "place_design -unplace; "
        "place_design -directive ExtraTimingOpt; "
        "phys_opt_design -directive Explore; "
        "route_design -directive NoTimingRelaxation -tns_cleanup; "
        "phys_opt_design -directive Explore"
    )


def clockopt_criticalpin_cleanup_command() -> str:
    return (
        "catch {phys_opt_design -clock_opt}; "
        "catch {phys_opt_design -routing_opt -critical_pin_opt -memory_rewire_opt "
        "-critical_cell_opt -placement_opt}; "
        "catch {phys_opt_design -directive AggressiveExplore}; "
        "catch {route_design -directive NoTimingRelaxation -tns_cleanup}; "
        "catch {phys_opt_design -clock_opt}; "
        "catch {phys_opt_design -directive Explore}"
    )


def target_period_dynamic_replication_command() -> str:
    collect = _COLLECT_CRITICAL_NETS.format(max_paths=60, net_limit=35)
    # Older variant keeps the original (no clock_uncore exclusion) regex; the
    # unified collector above is a strict superset filter, which is safe.
    return (
        collect
        + "route_design -unroute; "
        + "catch {place_design -post_place_opt}; "
        + "if {[llength $nets] > 0} { catch {phys_opt_design -force_replication_on_nets $nets} }; "
        + "foreach d {AggressiveFanoutOpt AlternateReplication AlternateFlowWithRetiming "
        + "AggressiveExplore Explore} { catch {phys_opt_design -directive $d} }; "
        + "if {[catch {route_design -directive NoTimingRelaxation -tns_cleanup}]} "
        + "{ route_design -directive Explore -tns_cleanup }; "
        + "foreach d {AggressiveExplore Explore} { catch {phys_opt_design -directive $d} }; "
        + "catch {phys_opt_design -clock_opt}"
    )


# Offline migration registry. Production knowledge loading does not call it.
TCL_BUILDERS = {
    "datuner": datuner_directive_command,
    "high_util_target_period_fast_route": high_util_target_period_fast_route_command,
    "high_util_target_period_replace_route": high_util_target_period_replace_route_command,
    "full_unplace_retime_route": full_unplace_retime_route_command,
    "extreme_clock_pressure": extreme_clock_pressure_command,
    "clockopt_criticalpin_cleanup": clockopt_criticalpin_cleanup_command,
    "target_period_dynamic_replication": target_period_dynamic_replication_command,
}


def resolve_tcl_refs(node):
    """Offline compatibility helper for legacy knowledge migrations."""
    if isinstance(node, dict):
        if "tcl_ref" in node:
            name = node["tcl_ref"]
            builder = TCL_BUILDERS.get(name)
            if builder is None:
                raise ValueError(f"unknown tcl_ref: {name!r}")
            kwargs = {k: v for k, v in node.items() if k != "tcl_ref"}
            return builder(**kwargs)
        return {k: resolve_tcl_refs(v) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_tcl_refs(v) for v in node]
    return node
