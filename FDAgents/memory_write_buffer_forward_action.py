"""Fresh contract recovery and generic ECO emission for write buffering."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .memory_write_buffer_forward import (
    ForwardedReadPort,
    MemoryWriteBufferForwardContract,
    prove_memory_write_buffer_forward,
)


BACKEND_SCHEMA = "memory-write-buffer-forward-action-v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _pin_index(value: str, stem: str) -> int:
    match = re.fullmatch(re.escape(stem) + r"\[(\d+)\]", value)
    if match is None:
        raise ValueError(f"unsupported memory pin {value!r}")
    return int(match.group(1))


@dataclass(frozen=True)
class MemoryForwardBinding:
    memory: str
    clock_net: str
    reset_net: str
    data_pins: tuple[str, ...]
    data_source_nets: tuple[str, ...]
    address_pins: tuple[str, ...]
    address_source_nets: tuple[str, ...]
    read_address_pins: tuple[str, ...]
    read_address_nets: tuple[str, ...]
    wea_pins: tuple[str, ...]
    wea_source_net: str
    port_enable_source_net: str
    read_enable_net: str
    read_output_pins: tuple[str, ...]
    read_raw_nets: tuple[str, ...]
    read_consumer_pins: tuple[str, ...]

    def fingerprint(self) -> str:
        return _digest(asdict(self))


def recover_memory_forward_contracts(
    extraction: Path,
    source_dcp_sha256: str,
    *,
    rejection_log: list[dict[str, Any]] | None = None,
) -> list[tuple[MemoryWriteBufferForwardContract, MemoryForwardBinding, dict]]:
    memories = _rows(extraction / "memories.tsv")
    inputs = _rows(extraction / "input_drivers.tsv")
    consumers = _rows(extraction / "read_consumers.tsv")
    results = []
    for candidate_ordinal, config in enumerate(memories):
        memory = config["memory"]
        local_inputs = [row for row in inputs if row["memory"] == memory]
        local_consumers = [row for row in consumers if row["memory"] == memory]
        data = sorted(
            (row for row in local_inputs if row["ref_pin"].startswith("DINADIN[")),
            key=lambda row: _pin_index(row["ref_pin"], "DINADIN"),
        )
        addresses = sorted(
            (
                row for row in local_inputs
                if row["ref_pin"].startswith("ADDRARDADDR[")
                and row["driver_refs"] != "VCC"
            ),
            key=lambda row: _pin_index(row["ref_pin"], "ADDRARDADDR"),
        )
        wea = sorted(
            (row for row in local_inputs if row["ref_pin"].startswith("WEA[")),
            key=lambda row: _pin_index(row["ref_pin"], "WEA"),
        )
        port_enable = [row for row in local_inputs if row["ref_pin"] == "ENARDEN"]
        read_enable = [row for row in local_inputs if row["ref_pin"] == "ENBWREN"]
        read_rows = sorted(
            (
                row for row in local_consumers
                if row["ref_pin"].startswith("DOUTBDOUT[")
            ),
            key=lambda row: _pin_index(row["ref_pin"], "DOUTBDOUT"),
        )
        rejection = None
        try:
            data_bits = [_pin_index(row["ref_pin"], "DINADIN") for row in data]
            read_bits = [_pin_index(row["ref_pin"], "DOUTBDOUT") for row in read_rows]
            wea_nets = {row["net"] for row in wea}
            resets = {row["sink_reset_net"] for row in read_rows}
            sink_clocks = {row["sink_clock_net"] for row in read_rows}
            if not data or data_bits != list(range(len(data))):
                raise ValueError("MEMFWD_DATA_BOUNDARY_UNSUPPORTED")
            if read_bits != data_bits or len(read_rows) != len(data):
                raise ValueError("MEMFWD_READ_CONSUMER_CLOSURE_INCOMPLETE")
            if any(row["sink_ref"] != "FDRE" for row in read_rows):
                raise ValueError("MEMFWD_READ_CONSUMER_SHAPE_UNSUPPORTED")
            if any(int(row["sink_count"]) != 1 for row in data):
                raise ValueError("MEMFWD_WRITE_DATA_CONE_NOT_EXCLUSIVE")
            if len(wea_nets) != 1 or not wea:
                raise ValueError("MEMFWD_BYTE_ENABLE_SHAPE_UNSUPPORTED")
            if len(port_enable) != 1 or len(read_enable) != 1:
                raise ValueError("MEMFWD_PORT_ENABLE_BOUNDARY_INCOMPLETE")
            if len(resets) != 1 or "" in resets:
                raise ValueError("MEMFWD_RESET_CONTRACT_UNSUPPORTED")
            if sink_clocks != {config["clock_a_net"]}:
                raise ValueError("MEMFWD_CONSUMER_CLOCK_MISMATCH")
            if not 1 <= len(addresses) <= 16:
                raise ValueError("MEMFWD_ADDRESS_SHAPE_UNSUPPORTED")
            read_address_pins = tuple(
                row["ref_pin"].replace("ADDRARDADDR", "ADDRBWRADDR")
                for row in addresses
            )
            input_by_pin = {row["ref_pin"]: row for row in local_inputs}
            if any(pin not in input_by_pin for pin in read_address_pins):
                raise ValueError("MEMFWD_READ_ADDRESS_BOUNDARY_INCOMPLETE")
            binding = MemoryForwardBinding(
                memory=memory,
                clock_net=config["clock_a_net"],
                reset_net=next(iter(resets)),
                data_pins=tuple(row["ref_pin"] for row in data),
                data_source_nets=tuple(row["net"] for row in data),
                address_pins=tuple(row["ref_pin"] for row in addresses),
                address_source_nets=tuple(row["net"] for row in addresses),
                read_address_pins=read_address_pins,
                read_address_nets=tuple(input_by_pin[pin]["net"] for pin in read_address_pins),
                wea_pins=tuple(row["ref_pin"] for row in wea),
                wea_source_net=next(iter(wea_nets)),
                port_enable_source_net=port_enable[0]["net"],
                read_enable_net=read_enable[0]["net"],
                read_output_pins=tuple(row["ref_pin"] for row in read_rows),
                read_raw_nets=tuple(row["net"] for row in read_rows),
                read_consumer_pins=tuple(row["sink_pin"] for row in read_rows),
            )
            contract = MemoryWriteBufferForwardContract(
                source_dcp_sha256=source_dcp_sha256,
                target_memory_fingerprint=binding.fingerprint(),
                data_width=len(data),
                address_width=len(addresses),
                byte_enable_width=len(wea),
                write_enable_register_bits=2,
                write_port="A",
                write_mode=config["write_mode_a"],
                read_address_collision=config["collision"],
                common_clock=config["same_clock"] == "1",
                write_data_registered=True,
                write_address_registered=True,
                write_enable_registered=True,
                aligned_forward_data_registered=True,
                aligned_hit_registered=True,
                write_data_cones_exclusive=True,
                port_a_read_unused=not any(
                    row["ref_pin"].startswith("DOUTADOUT[")
                    for row in local_consumers
                ),
                read_ports=(ForwardedReadPort(
                    port="B",
                    connected_data_bits=tuple(read_bits),
                    read_latency_cycles=1 + int(config["dob_reg"]),
                    output_register=int(config["dob_reg"]),
                    consumers_complete=True,
                    consumer_refs=("FDRE",),
                ),),
            )
            proof = prove_memory_write_buffer_forward(contract)
            if proof.get("mutation_ready") is not True:
                raise ValueError("MEMFWD_LOCAL_PROOF_REJECTED")
            results.append((contract, binding, proof))
        except Exception as exc:  # fail one shape without changing action class
            rejection = str(exc)
        if rejection:
            if rejection_log is not None:
                rejection_log.append({
                    "candidate_ordinal": candidate_ordinal,
                    "reason_code": rejection.split(":", 1)[0][:120],
                })
            continue
    return results


def _q(value: str) -> str:
    if not value or any(char in value for char in "{}\n\r"):
        raise ValueError("unsafe Tcl binding")
    return "{" + value + "}"


def _lut_init(input_count: int, predicate) -> str:
    width = 1 << input_count
    value = sum(int(bool(predicate(address))) << address for address in range(width))
    digits = max(1, width // 4)
    return f"{width}'h{value:0{digits}X}"


def render_memory_forward_eco_tcl(
    *,
    seed_dcp: Path,
    output_dir: Path,
    contract: MemoryWriteBufferForwardContract,
    binding: MemoryForwardBinding,
    proof: dict,
) -> tuple[str, dict]:
    if (
        proof.get("status") != "proved"
        or proof.get("proof_template") != "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1"
        or proof.get("source_dcp_sha256") != contract.source_dcp_sha256
        or binding.fingerprint() != contract.target_memory_fingerprint
    ):
        raise ValueError("memory-forward proof or binding is stale")
    prefix = "fdagents_memfwd"
    lines = [
        f"set seed_dcp {_q(str(seed_dcp.resolve()))}",
        f"set output_dir {_q(str(output_dir.resolve()))}",
        f"set expected_sha {_q(contract.source_dcp_sha256)}",
        f"set memory_name {_q(binding.memory)}",
        "file mkdir $output_dir",
        "set actual_sha [lindex [split [exec sha256sum -- $seed_dcp]] 0]",
        "if {$actual_sha ne $expected_sha} { error {memory-forward seed SHA mismatch} }",
        "open_checkpoint $seed_dcp",
        "proc prop {object name} { if {$object eq \"\"} {return \"\"}; if {[catch {get_property -quiet $name $object} value]} {return \"\"}; regsub -all {\\t|\\r|\\n} $value { } value; return $value }",
        "proc timing_snapshot {} { set clock [lindex [get_clocks -quiet clk_fpl26contest] 0]; if {$clock eq \"\"} {error {clk_fpl26contest missing}}; set setup [lindex [get_timing_paths -quiet -setup -max_paths 1 -sort_by slack -to $clock] 0]; if {$setup eq \"\"} {error {clk_fpl26contest has no setup path}}; set hold [lindex [get_timing_paths -quiet -hold -max_paths 1 -sort_by slack -to $clock] 0]; return [dict create period [prop $clock PERIOD] waveform [prop $clock WAVEFORM] source [prop $clock SOURCE_PINS] wns [prop $setup SLACK] whs [expr {$hold eq \"\" ? \"\" : [prop $hold SLACK]}]] }",
        "set input_timing [timing_snapshot]",
        "set input_fdre_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME == FDRE}]]",
        "set input_lut_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ LUT*}]]",
        "set input_dsp_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME == DSP48E2}]]",
        "set input_carry_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME == CARRY8}]]",
        "proc direct_net {pin} { set nets [get_nets -quiet -of_objects $pin]; if {[llength $nets] != 1} {error \"expected one direct net on $pin\"}; return [lindex $nets 0] }",
        "proc make_fdre {name dnet qnet clk reset vcc} { create_cell -reference FDRE $name; set_property INIT 1'b0 [get_cells $name]; create_net $qnet; connect_net -hierarchical -net $dnet -objects [get_pins $name/D]; connect_net -hierarchical -net [get_nets $qnet] -objects [get_pins $name/Q]; connect_net -hierarchical -net $clk -objects [get_pins $name/C]; connect_net -hierarchical -net $reset -objects [get_pins $name/R]; connect_net -hierarchical -net $vcc -objects [get_pins $name/CE]; return [get_nets $qnet] }",
        "set memory [get_cells -quiet $memory_name]",
        "if {[llength $memory] != 1 || [get_property REF_NAME $memory] ne {RAMB36E2}} {error {memory-forward target binding failed}}",
        "if {[get_property WRITE_MODE_A $memory] ne {READ_FIRST} || [get_property RDADDR_COLLISION_HWCONFIG $memory] ne {DELAYED_WRITE} || [get_property DOA_REG $memory] != 0 || [get_property DOB_REG $memory] != 0} {error {memory-forward primitive contract changed}}",
        f"set clk [get_nets {_q(binding.clock_net)}]",
        f"set reset [get_nets {_q(binding.reset_net)}]",
        f"create_cell -reference VCC {prefix}_vcc",
        f"create_net {prefix}_vcc_net",
        f"connect_net -hierarchical -net [get_nets {prefix}_vcc_net] -objects [get_pins {prefix}_vcc/P]",
        f"set vcc [get_nets {prefix}_vcc_net]",
        f"set new_cells [list [get_cells {prefix}_vcc]]",
        "set aligned_data_nets {}",
        "set pending_addr_nets {}",
        "set read_addr_nets {}",
    ]
    new_names = [f"{prefix}_vcc"]
    for bit, (pin, old_net) in enumerate(zip(binding.data_pins, binding.data_source_nets)):
        wcell = f"{prefix}_wdata_{bit}"
        fcell = f"{prefix}_fdata_{bit}"
        new_names.extend([wcell, fcell])
        lines.extend([
            f"set bram_pin [get_pins $memory_name/{pin}]",
            f"if {{[direct_net $bram_pin] ne {_q(old_net)}}} {{error {{memory-forward data boundary changed}}}}",
            "disconnect_net -net [direct_net $bram_pin] -objects $bram_pin",
            f"set pending_net [make_fdre {wcell} [get_nets {_q(old_net)}] {wcell}_q $clk $reset $vcc]",
            "connect_net -hierarchical -net $pending_net -objects $bram_pin",
            f"set aligned_net [make_fdre {fcell} $pending_net {fcell}_q $clk $reset $vcc]",
            "lappend aligned_data_nets $aligned_net",
            f"lappend new_cells [get_cells {wcell}] [get_cells {fcell}]",
        ])
    for bit, (pin, old_net, read_pin, read_net) in enumerate(zip(
        binding.address_pins, binding.address_source_nets,
        binding.read_address_pins, binding.read_address_nets,
    )):
        cell = f"{prefix}_waddr_{bit}"
        new_names.append(cell)
        lines.extend([
            f"set write_pin [get_pins $memory_name/{pin}]",
            f"if {{[direct_net $write_pin] ne {_q(old_net)}}} {{error {{memory-forward address boundary changed}}}}",
            "disconnect_net -net [direct_net $write_pin] -objects $write_pin",
            f"set pending_net [make_fdre {cell} [get_nets {_q(old_net)}] {cell}_q $clk $reset $vcc]",
            "connect_net -hierarchical -net $pending_net -objects $write_pin",
            "lappend pending_addr_nets $pending_net",
            f"if {{[direct_net [get_pins $memory_name/{read_pin}]] ne {_q(read_net)}}} {{error {{memory-forward read address changed}}}}",
            f"lappend read_addr_nets [get_nets {_q(read_net)}]",
            f"lappend new_cells [get_cells {cell}]",
        ])
    for name, pin, old_net in (
        (f"{prefix}_en", "ENARDEN", binding.port_enable_source_net),
        (f"{prefix}_we", binding.wea_pins[0], binding.wea_source_net),
    ):
        new_names.append(name)
        lines.extend([
            f"set control_pin [get_pins $memory_name/{pin}]",
            f"if {{[direct_net $control_pin] ne {_q(old_net)}}} {{error {{memory-forward control boundary changed}}}}",
            f"set {name}_q [make_fdre {name} [get_nets {_q(old_net)}] {name}_q $clk $reset $vcc]",
            f"lappend new_cells [get_cells {name}]",
        ])
    # EN has one pin; WEA may have several tied pins and must be rewired together.
    lines.extend([
        "disconnect_net -net [get_nets " + _q(binding.port_enable_source_net) + "] -objects [get_pins $memory_name/ENARDEN]",
        f"connect_net -hierarchical -net ${prefix}_en_q -objects [get_pins $memory_name/ENARDEN]",
    ])
    for pin in binding.wea_pins:
        lines.extend([
            f"if {{[direct_net [get_pins $memory_name/{pin}]] ne {_q(binding.wea_source_net)}}} {{error {{memory-forward WEA lanes changed}}}}",
            f"disconnect_net -net [get_nets {_q(binding.wea_source_net)}] -objects [get_pins $memory_name/{pin}]",
            f"connect_net -hierarchical -net ${prefix}_we_q -objects [get_pins $memory_name/{pin}]",
        ])
    # Compare every address pair. All but the final bit use shared LUT6 groups;
    # the final pair includes write/read controls exactly as in the proved shape.
    normal_bits = max(0, contract.address_width - 1)
    eq_nets = []
    group = 0
    for start in range(0, normal_bits, 3):
        count = min(3, normal_bits - start)
        cell = f"{prefix}_eq_{group}"
        net = f"{cell}_n"
        init = _lut_init(2 * count, lambda address, count=count: all(
            ((address >> (2 * lane)) & 1) == ((address >> (2 * lane + 1)) & 1)
            for lane in range(count)
        ))
        new_names.append(cell)
        eq_nets.append(net)
        lines.extend([
            f"create_cell -reference LUT{2 * count} {cell}",
            f"set_property INIT {init} [get_cells {cell}]",
            f"create_net {net}",
        ])
        for lane in range(count):
            bit = start + lane
            lines.extend([
                f"connect_net -hierarchical -net [lindex $pending_addr_nets {bit}] -objects [get_pins {cell}/I{2 * lane}]",
                f"connect_net -hierarchical -net [lindex $read_addr_nets {bit}] -objects [get_pins {cell}/I{2 * lane + 1}]",
            ])
        lines.extend([
            f"connect_net -hierarchical -net [get_nets {net}] -objects [get_pins {cell}/O]",
            f"lappend new_cells [get_cells {cell}]",
        ])
        group += 1
    last = contract.address_width - 1
    ctrl = f"{prefix}_eq_ctrl"
    ctrl_net = f"{ctrl}_n"
    ctrl_init = _lut_init(5, lambda address: (
        ((address >> 0) & 1) == ((address >> 1) & 1)
        and all((address >> bit) & 1 for bit in (2, 3, 4))
    ))
    new_names.append(ctrl)
    eq_nets.append(ctrl_net)
    lines.extend([
        f"create_cell -reference LUT5 {ctrl}",
        f"set_property INIT {ctrl_init} [get_cells {ctrl}]",
        f"create_net {ctrl_net}",
        f"connect_net -hierarchical -net [lindex $pending_addr_nets {last}] -objects [get_pins {ctrl}/I0]",
        f"connect_net -hierarchical -net [lindex $read_addr_nets {last}] -objects [get_pins {ctrl}/I1]",
        f"connect_net -hierarchical -net ${prefix}_en_q -objects [get_pins {ctrl}/I2]",
        f"connect_net -hierarchical -net ${prefix}_we_q -objects [get_pins {ctrl}/I3]",
        f"if {{[direct_net [get_pins $memory_name/ENBWREN]] ne {_q(binding.read_enable_net)}}} {{error {{memory-forward read enable changed}}}}",
        f"connect_net -hierarchical -net [get_nets {_q(binding.read_enable_net)}] -objects [get_pins {ctrl}/I4]",
        f"connect_net -hierarchical -net [get_nets {ctrl_net}] -objects [get_pins {ctrl}/O]",
        f"lappend new_cells [get_cells {ctrl}]",
    ])
    if not 1 <= len(eq_nets) <= 6:
        raise ValueError("MEMFWD_ADDRESS_COMPARE_TOPOLOGY_UNSUPPORTED")
    hit_and = f"{prefix}_hit_and"
    hit_d = f"{prefix}_hit_d"
    hit = f"{prefix}_hit"
    new_names.extend([hit_and, hit])
    and_init = _lut_init(len(eq_nets), lambda address: address == (1 << len(eq_nets)) - 1)
    lines.extend([
        f"create_cell -reference LUT{len(eq_nets)} {hit_and}",
        f"set_property INIT {and_init} [get_cells {hit_and}]",
        f"create_net {hit_d}",
    ])
    for index, net in enumerate(eq_nets):
        lines.append(f"connect_net -hierarchical -net [get_nets {net}] -objects [get_pins {hit_and}/I{index}]")
    lines.extend([
        f"connect_net -hierarchical -net [get_nets {hit_d}] -objects [get_pins {hit_and}/O]",
        f"set hit_net [make_fdre {hit} [get_nets {hit_d}] {hit}_q $clk $reset $vcc]",
        f"lappend new_cells [get_cells {hit_and}] [get_cells {hit}]",
    ])
    for bit, (out_pin, raw_net, sink_pin) in enumerate(zip(
        binding.read_output_pins, binding.read_raw_nets, binding.read_consumer_pins,
    )):
        cell = f"{prefix}_mux_{bit}"
        net = f"{cell}_n"
        new_names.append(cell)
        lines.extend([
            f"if {{[direct_net [get_pins $memory_name/{out_pin}]] ne {_q(raw_net)}}} {{error {{memory-forward read output changed}}}}",
            f"set consumer [get_pins {_q(sink_pin)}]",
            "if {[llength $consumer] != 1} {error {memory-forward consumer closure changed}}",
            "disconnect_net -net [direct_net $consumer] -objects $consumer",
            f"create_cell -reference LUT3 {cell}",
            f"set_property INIT 8'hCA [get_cells {cell}]",
            f"create_net {net}",
            f"connect_net -hierarchical -net [get_nets {_q(raw_net)}] -objects [get_pins {cell}/I0]",
            f"connect_net -hierarchical -net [lindex $aligned_data_nets {bit}] -objects [get_pins {cell}/I1]",
            f"connect_net -hierarchical -net $hit_net -objects [get_pins {cell}/I2]",
            f"connect_net -hierarchical -net [get_nets {net}] -objects [list [get_pins {cell}/O] $consumer]",
            f"lappend new_cells [get_cells {cell}]",
        ])
    expected_fdres = 2 * contract.data_width + contract.address_width + 3
    expected_luts = len(eq_nets) + 1 + contract.data_width
    lines.extend([
        "set new_cells [lsort -unique $new_cells]",
        f"if {{[llength $new_cells] != {len(new_names)}}} {{error {{memory-forward inventory mismatch}}}}",
        f"set new_fdres [get_cells -quiet {prefix}_* -filter {{REF_NAME == FDRE}}]",
        f"set new_luts [get_cells -quiet {prefix}_* -filter {{REF_NAME =~ LUT*}}]",
        f"if {{[llength $new_fdres] != {expected_fdres} || [llength $new_luts] != {expected_luts}}} {{error {{memory-forward topology mismatch}}}}",
        "set_property DONT_TOUCH true $new_cells",
        f"set preserved_leaf [get_cells -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == LEAF && NAME !~ {prefix}_*}}]",
        f"set preserved_macro [get_cells -hierarchical -filter {{IS_PRIMITIVE == 1 && LOC != \"\" && PRIMITIVE_LEVEL == MACRO && NAME !~ {prefix}_*}}]",
        "set_property IS_LOC_FIXED true $preserved_leaf",
        "set_property IS_BEL_FIXED true $preserved_leaf",
        "set_property IS_LOC_FIXED true $preserved_macro",
        "set_property IS_BEL_FIXED true $preserved_macro",
        "set pblocks [get_pblocks -quiet pb_extreme]",
        "if {[llength $pblocks] == 1} {add_cells_to_pblock [lindex $pblocks 0] $new_cells}",
        "place_design -directive ExtraTimingOpt",
        "route_design -directive NoTimingRelaxation",
        "phys_opt_design -directive AggressiveExplore",
        "route_design -directive NoTimingRelaxation",
        "report_timing_summary -delay_type min_max -max_paths 100 -file [file join $output_dir timing_summary.rpt]",
        "report_timing -delay_type max -max_paths 300 -input_pins -file [file join $output_dir timing_paths.rpt]",
        "report_route_status -file [file join $output_dir route_status.rpt]",
        "report_drc -file [file join $output_dir drc.rpt]",
        "set candidate_path [file join $output_dir candidate.dcp]",
        "write_checkpoint -force $candidate_path",
        "close_design",
        "open_checkpoint $candidate_path",
        "set output_timing [timing_snapshot]",
        "if {[dict get $output_timing period] ne [dict get $input_timing period] || [dict get $output_timing waveform] ne [dict get $input_timing waveform] || [dict get $output_timing source] ne [dict get $input_timing source]} {error {memory-forward changed contest clock identity}}",
        "set route_status [report_route_status -return_string]",
        "set route_errors 0",
        "if {[regexp -nocase {# of nets with routing errors[^0-9]*([0-9]+)} $route_status -> value]} {set route_errors $value}",
        "if {$route_errors != 0} {error {memory-forward candidate is not fully routed}}",
        "set pulse_text [report_pulse_width -return_string]",
        "set pulse_violations [regexp -all -nocase {VIOLATED} $pulse_text]",
        "set output_fdre_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME == FDRE}]]",
        "set output_lut_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME =~ LUT*}]]",
        "set output_dsp_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME == DSP48E2}]]",
        "set output_carry_count [llength [get_cells -quiet -hierarchical -filter {REF_NAME == CARRY8}]]",
        "set fp [open [file join $output_dir result.tsv] w]",
        "puts $fp \"metric\\tvalue\"",
        "puts $fp \"contract\\tBRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1\"",
        "puts $fp \"backend\\tmemory_write_buffer_forward_v1\"",
        "puts $fp \"source_dcp_sha256\\t$expected_sha\"",
        f"puts $fp \"target_memory_fingerprint\\t{contract.target_memory_fingerprint}\"",
        "puts $fp \"input_period_ns\\t[dict get $input_timing period]\"",
        "puts $fp \"input_waveform\\t[dict get $input_timing waveform]\"",
        "puts $fp \"input_clock_source\\t[dict get $input_timing source]\"",
        "puts $fp \"input_wns_ns\\t[dict get $input_timing wns]\"",
        "puts $fp \"output_period_ns\\t[dict get $output_timing period]\"",
        "puts $fp \"output_waveform\\t[dict get $output_timing waveform]\"",
        "puts $fp \"output_clock_source\\t[dict get $output_timing source]\"",
        "puts $fp \"output_wns_ns\\t[dict get $output_timing wns]\"",
        "puts $fp \"output_whs_ns\\t[dict get $output_timing whs]\"",
        "puts $fp \"input_fdre_count\\t$input_fdre_count\"",
        "puts $fp \"output_fdre_count\\t$output_fdre_count\"",
        "puts $fp \"input_lut_count\\t$input_lut_count\"",
        "puts $fp \"output_lut_count\\t$output_lut_count\"",
        "puts $fp \"input_dsp_count\\t$input_dsp_count\"",
        "puts $fp \"output_dsp_count\\t$output_dsp_count\"",
        "puts $fp \"input_carry_count\\t$input_carry_count\"",
        "puts $fp \"output_carry_count\\t$output_carry_count\"",
        f"puts $fp \"new_cells\\t{len(new_names)}\"",
        f"puts $fp \"new_fdres\\t{expected_fdres}\"",
        f"puts $fp \"new_luts\\t{expected_luts}\"",
        "puts $fp \"route_errors\\t$route_errors\"",
        "puts $fp \"pulse_violations\\t$pulse_violations\"",
        "puts $fp \"candidate_checkpoint_reopened\\t1\"",
        "close $fp",
        "close_design",
        "exit",
        "",
    ])
    topology = {
        "schema": BACKEND_SCHEMA,
        "source_sha256": contract.source_dcp_sha256,
        "target_memory_fingerprint": contract.target_memory_fingerprint,
        "memory": binding.memory,
        "write_pins": list(binding.data_pins),
        "read_consumer_pins": list(binding.read_consumer_pins),
        "new_cells": new_names,
        "new_fdres": expected_fdres,
        "new_luts": expected_luts,
        "write_data_bits": contract.data_width,
        "write_address_bits": contract.address_width,
        "read_mux_bits": contract.data_width,
        "old_direct_write_cone_sinks": 0,
    }
    topology["manifest_sha256"] = _digest(topology)
    return "\n".join(lines), topology
