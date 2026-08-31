source options.tcl

# Open design
open_checkpoint ./design.dcp

# Clock tightening (skip if no_clock_tighten=1)
if {$no_clock_tighten eq "0"} {
  set clk [get_clocks -quiet $clock_name]
  if {$clk eq {}} { set clk [lindex [get_clocks -quiet] 0] }
  set cname [get_property NAME $clk]
  set src [get_property SOURCE_PINS $clk]
  set port [lindex [get_ports -quiet $src] 0]
  if {$port ne {}} {
    create_clock -period $target_period_ns -name $cname $port
  } else {
    create_clock -period $target_period_ns -name $cname -objects $src
  }
}

# Allow over-utilization for congested designs
set_param drc.disableLUTOverUtilError 1

# Guide placement-time high-fanout replication for synthesized DCP nets.
if {[catch {get_nets -hierarchical -quiet -filter {TYPE == SIGNAL}} fanout_targets]} {
  set fanout_targets [get_nets -hierarchical -quiet]
}
if {[llength $fanout_targets] == 0} {
  set fanout_targets [get_nets -hierarchical -quiet]
}
if {[llength $fanout_targets] > 0} {
  catch {set_property MAX_FANOUT $fanout_limit $fanout_targets}
}

# Reset placement/routing
catch {route_design -unroute}
if {$route_only eq "0"} { catch {place_design -unplace} }

# Build args
set opt_args [list -directive $opt_directive]
set place_args [list -directive $place_directive]
set phys_args [list -directive $phys_opt_directive]
set route_args [list -directive $route_directive -tns_cleanup]

# Run implementation
opt_design {*}$opt_args
if {$route_only eq "0"} { place_design {*}$place_args }
phys_opt_design {*}$phys_args
route_design {*}$route_args

# Reports
report_timing_summary -delay_type max -file timing_summary.rpt
report_utilization -file utilization.rpt
