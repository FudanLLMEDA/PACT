if {$argc != 2} {
    puts stderr "usage: vivado -mode batch -source signoff_checkpoint.tcl -tclargs INPUT_DCP OUT_DIR"
    exit 2
}

set input_dcp [file normalize [lindex $argv 0]]
set out_dir [file normalize [lindex $argv 1]]
file mkdir $out_dir

open_checkpoint $input_dcp
puts "PACT_REPLAY_OK=1"

set route_report [report_route_status -return_string]
set route_file [open [file join $out_dir route_status.rpt] w]
puts $route_file $route_report
close $route_file

set route_errors -1
regexp {# of nets with routing errors[^\n]*:\s+([0-9]+)} $route_report -> route_errors
puts "PACT_ROUTE_ERRORS=$route_errors"

set timing_report [report_timing_summary -delay_type min_max -report_unconstrained -return_string]
set timing_file [open [file join $out_dir timing_summary.rpt] w]
puts $timing_file $timing_report
close $timing_file

set setup_paths [get_timing_paths -quiet -delay_type max -max_paths 1 -nworst 1]
if {[llength $setup_paths] > 0} {
    puts "PACT_WORST_SETUP_SLACK_NS=[get_property SLACK [lindex $setup_paths 0]]"
} else {
    puts "PACT_WORST_SETUP_SLACK_NS=NA"
}

set hold_paths [get_timing_paths -quiet -delay_type min -max_paths 1 -nworst 1]
if {[llength $hold_paths] > 0} {
    puts "PACT_WORST_HOLD_SLACK_NS=[get_property SLACK [lindex $hold_paths 0]]"
} else {
    puts "PACT_WORST_HOLD_SLACK_NS=NA"
}

set check_report [check_timing -verbose -return_string]
set check_file [open [file join $out_dir check_timing.rpt] w]
puts $check_file $check_report
close $check_file

if {![catch {set pulse_report [report_pulse_width -return_string]} pulse_error]} {
    set pulse_file [open [file join $out_dir pulse_width.rpt] w]
    puts $pulse_file $pulse_report
    close $pulse_file
    puts "PACT_PULSE_WIDTH_REPORT_OK=1"
} else {
    puts "PACT_PULSE_WIDTH_REPORT_OK=0"
    puts "PACT_PULSE_WIDTH_REPORT_ERROR=$pulse_error"
}

close_design

if {$route_errors != 0} {
    puts stderr "PACT_SIGNOFF_FAIL=route_errors"
    exit 3
}

puts "PACT_SIGNOFF_ROUTE_PASS=1"
exit 0
