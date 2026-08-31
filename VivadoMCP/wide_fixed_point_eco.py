"""Two-DSP48E2 ECO for a proved signed wide constant product slice."""

from __future__ import annotations

import re
from typing import Any

from FDAgents.operator_proof import (
    _digest,
    fixed_point_spec_from_extracted_facts,
    verify_rewrite_authorization,
)
from .fixed_point_eco import (
    _braced,
    _bus_items,
    _identity,
    _int_field,
    _name,
    _pins,
)


TEMPLATE = "DSP48E2_WIDE_CONST_MUL_V1"
LOWER_OPMODE = 0b000000101
UPPER_OPMODE = 0b001010101
SPLIT_BIT = 17
_SITE_RE = re.compile(r"DSP48E2_X(\d+)Y(\d+)")

_STRING_PROPERTIES = (
    ("USE_MULT", "MULTIPLY"),
    ("USE_SIMD", "ONE48"),
    ("USE_WIDEXOR", "FALSE"),
    ("AMULTSEL", "A"),
    ("BMULTSEL", "B"),
    ("PREADDINSEL", "A"),
    ("B_INPUT", "DIRECT"),
)
_ZERO_REGISTER_PROPERTIES = (
    "AREG", "ACASCREG", "BREG", "BCASCREG", "MREG", "PREG", "ADREG",
    "CREG", "DREG", "CARRYINREG", "CARRYINSELREG", "OPMODEREG",
    "ALUMODEREG", "INMODEREG",
)
_SCALAR_GND_PINS = (
    "CARRYCASCIN", "CARRYIN", "MULTSIGNIN", "CEA1", "CEA2", "CEAD",
    "CEALUMODE", "CEB1", "CEB2", "CEC", "CECARRYIN", "CECTRL", "CED",
    "CEINMODE", "CEM", "CEP", "CLK", "RSTA", "RSTALLCARRYIN",
    "RSTALUMODE", "RSTB", "RSTC", "RSTCTRL", "RSTD", "RSTINMODE",
    "RSTM", "RSTP",
)


def _validated_contract(
    facts: dict[str, Any], authorization: dict[str, Any], placement: dict[str, Any]
) -> dict[str, Any]:
    spec_obj = fixed_point_spec_from_extracted_facts(facts)
    if "accumulate" not in facts.get("spec", {}):
        raise ValueError("wide fixed-point facts do not bind accumulate semantics")
    if spec_obj.accumulate:
        raise ValueError("wide fixed-point product may not contain accumulator feedback")
    if not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=facts["design_sha256"],
        expected_region_sha256=facts["region_sha256"],
    ):
        raise ValueError("wide fixed-point authorization replay failed")
    plan = authorization.get("plan")
    if not isinstance(plan, dict) or plan.get("template") != TEMPLATE:
        raise ValueError("wide fixed-point plan template is unsupported")
    if authorization.get("spec") != facts.get("spec"):
        raise ValueError("wide fixed-point authorization spec differs from facts")

    data_width = _int_field(spec_obj.data_width, "spec.data_width", 28, 35)
    coefficient_width = _int_field(
        spec_obj.coefficient_width, "spec.coefficient_width", 1, 27
    )
    coefficient = _int_field(
        spec_obj.coefficient,
        "spec.coefficient",
        0,
        (1 << coefficient_width) - 1,
    )
    output_lsb = _int_field(plan.get("output_lsb"), "plan.output_lsb", 0, 64)
    output_width = _int_field(plan.get("output_width"), "plan.output_width", 1, 64)
    if output_lsb + output_width > spec_obj.product_width:
        raise ValueError("wide fixed-point output slice exceeds the proved product")
    if output_lsb + output_width > SPLIT_BIT + 48:
        raise ValueError("wide fixed-point output slice exceeds the DSP pair")
    if plan.get("operand_signed") != spec_obj.data_signed or plan.get(
        "coefficient_signed"
    ) != spec_obj.coefficient_signed:
        raise ValueError("wide fixed-point plan signedness differs from facts")
    if plan.get("product_latency") != 1:
        raise ValueError("wide fixed-point template preserves one external sink register")

    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != {
        "input_bus", "output_bus", "cone_cells", "clock_pin", "ce_pin",
    }:
        raise ValueError("wide fixed-point boundary manifest is incomplete")
    input_bus = _bus_items(
        boundary.get("input_bus"), "boundary.input_bus", data_width, allow_const=True
    )
    if spec_obj.data_signed and "cell" not in input_bus[-1]:
        raise ValueError("wide signed data requires a real MSB input register")
    output_bus = _bus_items(
        boundary.get("output_bus"), "boundary.output_bus", output_width
    )
    cone = boundary.get("cone_cells")
    if not isinstance(cone, list) or not 1 <= len(cone) <= 4096:
        raise ValueError("wide fixed-point cone cell inventory is invalid")
    cone_cells = []
    seen = set()
    for index, item in enumerate(cone):
        if not isinstance(item, dict) or set(item) != {"cell", "type"}:
            raise ValueError(f"boundary.cone_cells[{index}] schema is invalid")
        cell = _name(item.get("cell"), f"boundary.cone_cells[{index}].cell")
        cell_type = _name(item.get("type"), f"boundary.cone_cells[{index}].type")
        if cell in seen or not re.fullmatch(r"(?:LUT[1-6]|CARRY[48])", cell_type):
            raise ValueError("wide fixed-point cone inventory is invalid")
        seen.add(cell)
        cone_cells.append({"cell": cell, "type": cell_type})

    if not isinstance(placement, dict) or placement.get("kind") != (
        "fixed_point_placement_manifest"
    ) or placement.get("version") != 1:
        raise ValueError("wide fixed-point placement manifest kind is invalid")
    claimed = placement.get("manifest_sha256")
    payload = dict(placement)
    payload.pop("manifest_sha256", None)
    _identity(claimed, "placement.manifest_sha256")
    if _digest(payload) != claimed:
        raise ValueError("wide fixed-point placement digest mismatch")
    source = {
        "dcp_sha256": facts["design_sha256"],
        "region_sha256": facts["region_sha256"],
    }
    if (
        placement.get("source") != source
        or placement.get("candidate_id") != facts.get("candidate_id")
        or placement.get("authorization_id") != authorization.get("certificate_id")
        or placement.get("authority") != "pre_emission_only"
    ):
        raise ValueError("wide fixed-point placement is stale or cross-plan")
    records = placement.get("placements")
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError("wide fixed-point placement requires two DSP sites")
    sites = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"site", "bel"}:
            raise ValueError("wide fixed-point placement slot schema is invalid")
        match = _SITE_RE.fullmatch(str(record.get("site")))
        if match is None or record.get("bel") != "DSP48E2":
            raise ValueError("wide fixed-point placement slot is invalid")
        sites.append(record["site"])
    lower_site = _SITE_RE.fullmatch(sites[0])
    upper_site = _SITE_RE.fullmatch(sites[1])
    if lower_site.group(1) != upper_site.group(1) or int(
        upper_site.group(2)
    ) != int(lower_site.group(2)) + 1:
        raise ValueError("wide fixed-point sites are not an ordered cascade pair")

    extended_coefficient = coefficient
    if spec_obj.coefficient_signed and coefficient & (1 << (coefficient_width - 1)):
        extended_coefficient |= ((1 << 27) - 1) ^ ((1 << coefficient_width) - 1)
    certificate_id = authorization["certificate_id"]
    prefix = f"fdagents_widefixed_{certificate_id[:12]}"
    return {
        "prefix": prefix,
        "lower": prefix + "_lower",
        "upper": prefix + "_upper",
        "gnd": prefix + "_gnd",
        "vcc": prefix + "_vcc",
        "input_bus": input_bus,
        "output_bus": output_bus,
        "cone_cells": cone_cells,
        "data_width": data_width,
        "data_signed": spec_obj.data_signed,
        "coefficient_signed": spec_obj.coefficient_signed,
        "coefficient": extended_coefficient,
        "output_lsb": output_lsb,
        "sites": sites,
    }


def _property_pairs(role: str) -> list[tuple[str, str]]:
    pairs = list(_STRING_PROPERTIES)
    pairs.append(("A_INPUT", "DIRECT" if role == "lower" else "CASCADE"))
    pairs.extend((name, "0") for name in _ZERO_REGISTER_PROPERTIES)
    return pairs


def _static_sinks(contract: dict[str, Any]) -> tuple[list[str], list[str]]:
    gnd = [contract["gnd"] + "/G"]
    vcc = [contract["vcc"] + "/P"]

    def tie(pin: str, value: bool) -> None:
        (vcc if value else gnd).append(pin)

    for role, opmode in (("lower", LOWER_OPMODE), ("upper", UPPER_OPMODE)):
        dsp = contract[role]
        for bit in range(9):
            tie(f"{dsp}/OPMODE[{bit}]", bool(opmode & (1 << bit)))
        for width, port in ((4, "ALUMODE"), (5, "INMODE"), (3, "CARRYINSEL")):
            for bit in range(width):
                tie(f"{dsp}/{port}[{bit}]", False)
        for pin in _SCALAR_GND_PINS:
            tie(f"{dsp}/{pin}", False)
        for bit in range(48):
            tie(f"{dsp}/C[{bit}]", False)
        # Upper D[25:26] conflict with the hard A-cascade path on this device.
        # D is semantically unused for INMODE=00000/AMULTSEL=A, so leave the
        # entire upper D port open as synthesis does after physical pruning.
        if role == "lower":
            for bit in range(27):
                tie(f"{dsp}/D[{bit}]", False)
        for bit in range(18):
            tie(f"{dsp}/BCIN[{bit}]", False)
        if role == "lower":
            for bit in range(30):
                tie(f"{dsp}/ACIN[{bit}]", False)
            for bit in range(48):
                tie(f"{dsp}/PCIN[{bit}]", False)
            for bit in range(27):
                tie(f"{dsp}/A[{bit}]", bool(contract["coefficient"] & (1 << bit)))
            for bit in range(27, 30):
                tie(
                    f"{dsp}/A[{bit}]",
                    contract["coefficient_signed"]
                    and bool(contract["coefficient"] & (1 << 26)),
                )
            tie(f"{dsp}/B[17]", False)
        # With A_INPUT=CASCADE the upper direct A port is physically replaced
        # by ACIN; leave those dead logical pins open rather than routing GND.
    for bit, item in enumerate(contract["input_bus"]):
        if "cell" in item:
            continue
        target = f"{contract['lower']}/B[{bit}]" if bit < SPLIT_BIT else (
            f"{contract['upper']}/B[{bit - SPLIT_BIT}]"
        )
        tie(target, False)
    upper_data_width = contract["data_width"] - SPLIT_BIT
    extension_value = contract["data_signed"]
    for bit in range(upper_data_width, 18):
        if not extension_value:
            tie(f"{contract['upper']}/B[{bit}]", False)
    return gnd, vcc


def _output_pin(contract: dict[str, Any], index: int) -> str:
    product_bit = contract["output_lsb"] + index
    if product_bit < SPLIT_BIT:
        return f"{contract['lower']}/P[{product_bit}]"
    return f"{contract['upper']}/P[{product_bit - SPLIT_BIT}]"


def build_wide_fixed_point_eco_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    contract = _validated_contract(facts, authorization, placement)
    prefix = contract["prefix"]
    lower, upper = contract["lower"], contract["upper"]
    lines = [
        "proc fdagents_wf_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"required pin missing: $name\"}; return $value}",
        "proc fdagents_wf_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"required cell missing: $name\"}; return $value}",
        "proc fdagents_wf_pin_net {name} {set value [get_nets -quiet -of_objects [fdagents_wf_one_pin $name]]; if {[llength $value] != 1} {error \"required pin net missing: $name\"}; return $value}",
        "proc fdagents_wf_disconnect {name} {set pin [fdagents_wf_one_pin $name]; set net [get_nets -quiet -of_objects $pin]; if {[llength $net] != 1} {error \"disconnect pin net missing: $name\"}; disconnect_net -net $net -objects $pin}",
        f"if {{[llength [get_cells -quiet {_braced(prefix + '*')}]]}} {{error {{wide fixed-point namespace exists}}}}",
    ]
    for item in contract["cone_cells"]:
        lines.append(
            f"if {{[get_property REF_NAME [fdagents_wf_one_cell {_braced(item['cell'])}]] ne {_braced(item['type'])}}} {{error {{wide cone type mismatch}}}}"
        )
    for item in contract["input_bus"]:
        if "pin" in item:
            lines.append(f"fdagents_wf_one_pin {_braced(item['pin'])}")
    for item in contract["output_bus"]:
        lines.append(f"fdagents_wf_one_pin {_braced(item['pin'])}")
    lines.extend([
        f"create_cell -reference GND {contract['gnd']}",
        f"create_cell -reference VCC {contract['vcc']}",
        f"create_cell -reference DSP48E2 {lower}",
        f"create_cell -reference DSP48E2 {upper}",
        f"create_net {prefix}_gnd_net",
        f"create_net {prefix}_vcc_net",
    ])
    for role in ("lower", "upper"):
        for name, value in _property_pairs(role):
            lines.append(
                f"set_property {name} {value} [get_cells {_braced(contract[role])}]"
            )
    gnd, vcc = _static_sinks(contract)
    lines.append(f"connect_net -net {prefix}_gnd_net -objects {_pins(gnd)}")
    lines.append(f"connect_net -net {prefix}_vcc_net -objects {_pins(vcc)}")
    for bit in range(30):
        net = f"{prefix}_ac_{bit:02d}"
        lines.extend([
            f"create_net {net}",
            f"connect_net -net {net} -objects {_pins([f'{lower}/ACOUT[{bit}]', f'{upper}/ACIN[{bit}]'])}",
        ])
    for bit in range(48):
        net = f"{prefix}_pc_{bit:02d}"
        lines.extend([
            f"create_net {net}",
            f"connect_net -net {net} -objects {_pins([f'{lower}/PCOUT[{bit}]', f'{upper}/PCIN[{bit}]'])}",
        ])
    for bit, item in enumerate(contract["input_bus"]):
        if "pin" not in item:
            continue
        target = f"{lower}/B[{bit}]" if bit < SPLIT_BIT else f"{upper}/B[{bit - SPLIT_BIT}]"
        targets = [target]
        if contract["data_signed"] and bit == contract["data_width"] - 1:
            targets.extend(
                f"{upper}/B[{extension}]"
                for extension in range(contract["data_width"] - SPLIT_BIT, 18)
            )
        lines.append(
            f"connect_net -hierarchical -net [fdagents_wf_pin_net {_braced(item['pin'])}] -objects {_pins(targets)}"
        )
    lines.append("set fdagents_wf_old_outputs [list]")
    for item in contract["output_bus"]:
        lines.append(
            f"lappend fdagents_wf_old_outputs [fdagents_wf_pin_net {_braced(item['pin'])}]"
        )
    lines.append(
        "route_design -unroute -nets [lsort -unique $fdagents_wf_old_outputs]"
    )
    for index, item in enumerate(contract["output_bus"]):
        net = f"{prefix}_p_{index:03d}"
        lines.extend([
            f"fdagents_wf_disconnect {_braced(item['pin'])}",
            f"create_net {net}",
            f"connect_net -hierarchical -net {net} -objects {_pins([_output_pin(contract, index), item['pin']])}",
        ])
    for item in contract["cone_cells"]:
        lines.append(f"remove_cell [fdagents_wf_one_cell {_braced(item['cell'])}]")
    for role, site in zip(("lower", "upper"), contract["sites"]):
        lines.append(
            f"place_cell [get_cells {_braced(contract[role])}] [get_sites {_braced(site)}]"
        )
    lines.extend([
        f"set fdagents_wf_touched [lsort -unique [get_nets -quiet -of_objects [get_pins -quiet -of_objects [get_cells [list {_braced(lower)} {_braced(upper)}]]]]]",
        f"set fdagents_wf_touched [lsearch -all -inline -not -exact $fdagents_wf_touched {_braced(prefix + '_gnd_net')}]",
        f"set fdagents_wf_touched [lsearch -all -inline -not -exact $fdagents_wf_touched {_braced(prefix + '_vcc_net')}]",
        "if {[llength $fdagents_wf_touched] == 0} {error {wide fixed-point ECO touched no nets}}",
        "route_design",
        "set fdagents_wf_unrouted [filter -quiet $fdagents_wf_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_wf_unrouted] != 0} {error {wide fixed-point ECO left unrouted nets}}",
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_WIDE_FIXED_POINT_ECO_DONE}",
    ])
    return "\n".join(lines)


def build_wide_fixed_point_postcheck_tcl(
    facts: dict[str, Any], authorization: dict[str, Any], placement: dict[str, Any]
) -> str:
    contract = _validated_contract(facts, authorization, placement)
    lower, upper = contract["lower"], contract["upper"]
    lines = [
        "proc fdagents_wfc_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck cell missing: $name\"}; return $value}",
        "proc fdagents_wfc_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck pin missing: $name\"}; return $value}",
        "proc fdagents_wfc_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_wfc_one_pin $name]]; if {[llength $value] == 0} {error \"postcheck pin net missing: $name\"}; return $value}",
        "proc fdagents_wfc_same {left right} {set a [fdagents_wfc_segments $left]; set b [fdagents_wfc_segments $right]; foreach n $a {if {[lsearch -exact $b $n] >= 0} {return}}; error \"postcheck net mismatch: $left $right\"}",
    ]
    for role, site in zip(("lower", "upper"), contract["sites"]):
        dsp = contract[role]
        lines.extend([
            f"if {{[get_property REF_NAME [fdagents_wfc_one_cell {_braced(dsp)}]] ne {{DSP48E2}}}} {{error {{postcheck DSP type mismatch}}}}",
            f"if {{[get_property LOC [fdagents_wfc_one_cell {_braced(dsp)}]] ne {_braced(site)}}} {{error {{postcheck DSP placement mismatch}}}}",
        ])
        for name, value in _property_pairs(role):
            lines.append(
                f"if {{[get_property {name} [fdagents_wfc_one_cell {_braced(dsp)}]] ne {_braced(value)}}} {{error {{postcheck DSP property mismatch}}}}"
            )
    for bit in range(30):
        lines.append(
            f"fdagents_wfc_same {_braced(f'{lower}/ACOUT[{bit}]')} {_braced(f'{upper}/ACIN[{bit}]')}"
        )
    for bit in range(48):
        lines.append(
            f"fdagents_wfc_same {_braced(f'{lower}/PCOUT[{bit}]')} {_braced(f'{upper}/PCIN[{bit}]')}"
        )
    for index, item in enumerate(contract["output_bus"]):
        lines.append(
            f"fdagents_wfc_same {_braced(_output_pin(contract, index))} {_braced(item['pin'])}"
        )
    for item in contract["cone_cells"]:
        lines.append(
            f"if {{[llength [get_cells -quiet [list {_braced(item['cell'])}]]] != 0}} {{error {{postcheck cone cell still exists}}}}"
        )
    lines.extend([
        f"set fdagents_wfc_touched [lsort -unique [get_nets -quiet -of_objects [get_pins -quiet -of_objects [get_cells [list {_braced(lower)} {_braced(upper)}]]]]]",
        f"set fdagents_wfc_touched [lsearch -all -inline -not -exact $fdagents_wfc_touched {_braced(contract['prefix'] + '_gnd_net')}]",
        f"set fdagents_wfc_touched [lsearch -all -inline -not -exact $fdagents_wfc_touched {_braced(contract['prefix'] + '_vcc_net')}]",
        "set fdagents_wfc_unrouted [filter -quiet $fdagents_wfc_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_wfc_unrouted] != 0} {error {postcheck found unrouted wide fixed-point nets}}",
        "puts {FDAGENTS_WIDE_FIXED_POINT_POSTCHECK_DONE}",
    ])
    return "\n".join(lines)
