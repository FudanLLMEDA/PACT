"""Fixed-template Vivado ECO builder for a proved fixed-point DSP rebuild.

The builders emit closed-form Tcl only: every interpolated value is either a
constant chosen here or a manifest field validated against a strict regex or
integer range first. Cell/net/pin names are always brace-quoted following the
conventions in ``recurrence_eco.py``.

Canonical static DSP48E2 configuration (template DSP48E2_MUL_SHIFT_ACCUM_V1):

* ``USE_MULT MULTIPLY`` with ``AMULTSEL A`` / ``BMULTSEL B`` (no pre-adder).
* ``INMODE 00000`` (direct A2/B2 multiplier operands).
* ``ALUMODE 0000`` (``Z + X + Y + CIN``).
* ``OPMODE 000100101`` (``W=00``, ``Z=010`` selects P feedback for the
  accumulate, ``Y=01``/``X=01`` select the multiplier partial products M).
* ``CARRYINSEL 000`` with ``CARRYIN`` tied to GND.
* ``PREG 1`` always (the P feedback register is the accumulator);
  ``AREG/BREG`` and ``MREG`` are budgeted from the proved product latency:
  latency 1 -> (0, 0), latency 2 -> (1, 0), latency 3 -> (1, 1).
"""

from __future__ import annotations

import base64
import re
from typing import Any


_NAME_RE = re.compile(r"^[A-Za-z0-9_./:+\-\[\]]{1,1024}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DSP_SITE_RE = re.compile(r"^DSP48E2_X\d+Y\d+$")
_CANDIDATE_ID_RE = re.compile(r"^fixedpoint:[0-9a-f]{20}$")
_CONE_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")

_PLACEMENT_MANIFEST_KIND = "fixed_point_placement_manifest"
_PLACEMENT_MANIFEST_VERSION = 1
_FACTS_KIND = "fixed_point_product_contract_facts"
_PLAN_TEMPLATE = "DSP48E2_MUL_SHIFT_ACCUM_V1"

# Fixed canonical DSP48E2 static configuration (see module docstring).
DSP_OPMODE_BITS = 0b000100101  # W=00, Z=010 (P), Y=01 (M), X=01 (M)
DSP_ALUMODE_BITS = 0b0000      # Z + X + Y + CIN
DSP_INMODE_BITS = 0b00000      # A2/B2 direct multiplier operands
DSP_CARRYINSEL_BITS = 0b000    # CARRYIN pin (tied to GND)
DSP_A_WIDTH = 30
DSP_A_MULT_WIDTH = 27
DSP_B_WIDTH = 18
DSP_C_WIDTH = 48
DSP_D_WIDTH = 27
DSP_P_WIDTH = 48
# product_latency -> (AREG/BREG, MREG); PREG is always 1 for the accumulate.
DSP_LATENCY_REGISTERS = {1: (0, 0), 2: (1, 0), 3: (1, 1)}
# String-valued static properties applied verbatim to the new DSP48E2 cell.
DSP_STATIC_STRING_PROPERTIES = (
    ("USE_MULT", "MULTIPLY"),
    ("AMULTSEL", "A"),
    ("BMULTSEL", "B"),
    ("PREADDINSEL", "A"),
    ("USE_SIMD", "ONE48"),
    ("USE_WIDEXOR", "FALSE"),
    ("A_INPUT", "DIRECT"),
    ("B_INPUT", "DIRECT"),
)
# Integer-valued static pipeline properties that never depend on the plan.
DSP_STATIC_INT_PROPERTIES = (
    ("PREG", 1),
    ("ADREG", 0),
    ("DREG", 0),
    ("CREG", 0),
    ("CARRYINREG", 0),
    ("CARRYINSELREG", 0),
    ("OPMODEREG", 0),
    ("ALUMODEREG", 0),
    ("INMODEREG", 0),
)
_DSP_STATIC_GND_CE_RST_PINS = (
    "CEAD", "CEALUMODE", "CEC", "CECARRYIN", "CECTRL", "CEINMODE",
    "RSTA", "RSTALLCARRYIN", "RSTALUMODE", "RSTB", "RSTC", "RSTCTRL",
    "RSTD", "RSTINMODE", "RSTM", "RSTP",
)

PORT_SIGNATURE_MARKER = "FDAGENTS_FIXED_POINT_PORT_SIGNATURE"
TOP_IDENTITY_MARKER = "FDAGENTS_FIXED_POINT_TOP_IDENTITY"
PORT_SIGNATURE_DONE_MARKER = "FDAGENTS_FIXED_POINT_PORT_SIGNATURE_DONE"


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{field} contains unsupported characters")
    return value


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _braced(value: str) -> str:
    if "}" in value or "{" in value or "\n" in value or "\r" in value:
        raise ValueError("Tcl data contains unsupported characters")
    return "{" + value + "}"


def _pins(names: list[str]) -> str:
    return "[get_pins -quiet [list " + " ".join(_braced(name) for name in names) + "]]"


def _int_field(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field} must be an integer in {low}..{high}")
    return value


def _bus_items(
    value: Any,
    field: str,
    expected_length: int,
    *,
    allow_const: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != expected_length:
        raise ValueError(f"{field} does not cover its recovered bus width")
    result = []
    seen_pins = set()
    for index, item in enumerate(value):
        if allow_const and isinstance(item, dict) and set(item) == {"const"}:
            # A hole bit: synthesis constant-folded this bus index away, so
            # the matching DSP A bit is tied to the shared GND net instead.
            const_value = item.get("const")
            if isinstance(const_value, bool) or const_value != 0:
                raise ValueError(f"{field}[{index}] const entry must be 0")
            result.append({"const": 0})
            continue
        if not isinstance(item, dict) or set(item) != {"cell", "pin"}:
            raise ValueError(f"{field}[{index}] has an unsupported schema")
        cell = _name(item.get("cell"), f"{field}[{index}].cell")
        pin = _name(item.get("pin"), f"{field}[{index}].pin")
        if not pin.startswith(cell + "/") or pin in seen_pins:
            raise ValueError(f"{field}[{index}] pin does not belong to its cell")
        seen_pins.add(pin)
        result.append({"cell": cell, "pin": pin})
    return result


def _validated_contract(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Cross-check every manifest in Python before any Tcl is emitted."""
    if not isinstance(facts, dict) or facts.get("kind") != _FACTS_KIND:
        raise ValueError("fixed-point facts manifest kind is invalid")
    spec = facts.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("fixed-point facts spec is missing")
    if not isinstance(authorization, dict) or authorization.get("kind") != (
        "operator_rewrite_authorization"
    ):
        raise ValueError("fixed-point authorization kind is invalid")
    if authorization.get("rewrite_kind") != "fabric_to_dsp":
        raise ValueError("fixed-point authorization rewrite kind is invalid")
    if authorization.get("authority") != "pre_mutation_only":
        raise ValueError("fixed-point authorization authority is invalid")
    certificate_id = authorization.get("certificate_id")
    _identity(certificate_id, "authorization.certificate_id")
    if authorization.get("spec") != spec:
        raise ValueError("fixed-point authorization spec differs from the facts")
    source = authorization.get("source")
    if not isinstance(source, dict) or set(source) != {"dcp_sha256", "region_sha256"}:
        raise ValueError("fixed-point authorization source identity is incomplete")
    _identity(source.get("dcp_sha256"), "authorization.source.dcp_sha256")
    _identity(source.get("region_sha256"), "authorization.source.region_sha256")
    if facts.get("region_sha256") != source["region_sha256"]:
        raise ValueError("fixed-point facts region differs from its authorization")
    if "design_sha256" in facts and facts.get("design_sha256") != source["dcp_sha256"]:
        raise ValueError("fixed-point facts design differs from its authorization")

    plan = authorization.get("plan")
    if not isinstance(plan, dict) or plan.get("template") != _PLAN_TEMPLATE:
        raise ValueError("fixed-point plan template is unsupported")
    data_width = _int_field(spec.get("data_width"), "spec.data_width", 1, DSP_A_MULT_WIDTH)
    coefficient_width = _int_field(
        spec.get("coefficient_width"), "spec.coefficient_width", 1, DSP_B_WIDTH
    )
    product_width = _int_field(spec.get("product_width"), "spec.product_width", 1, 256)
    coefficient = _int_field(
        spec.get("coefficient"), "spec.coefficient", 0, (1 << coefficient_width) - 1
    )
    product_latency = _int_field(
        spec.get("product_latency"), "spec.product_latency", 1, 3
    )
    if product_latency not in DSP_LATENCY_REGISTERS:
        raise ValueError("spec.product_latency has no supported register budget")
    if plan.get("product_latency") != product_latency:
        raise ValueError("plan latency differs from the recovered contract")
    if plan.get("accumulator_width") != spec.get("accumulator_width"):
        raise ValueError("plan accumulator width differs from the recovered contract")
    output_lsb = _int_field(plan.get("output_lsb"), "plan.output_lsb", 0, DSP_P_WIDTH - 1)
    output_width = _int_field(plan.get("output_width"), "plan.output_width", 1, DSP_P_WIDTH)
    if output_lsb + output_width > DSP_P_WIDTH:
        raise ValueError("plan output slice exceeds the DSP P port")
    if output_lsb + output_width > product_width:
        raise ValueError("plan output slice exceeds the recovered product width")
    data_signed = spec.get("data_signed")
    coefficient_signed = spec.get("coefficient_signed")
    if not isinstance(data_signed, bool) or not isinstance(coefficient_signed, bool):
        raise ValueError("spec signedness flags must be booleans")

    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != {
        "input_bus", "output_bus", "cone_cells", "clock_pin", "ce_pin",
    }:
        raise ValueError("fixed-point boundary manifest is incomplete")
    input_bus = _bus_items(
        boundary.get("input_bus"), "boundary.input_bus", data_width,
        allow_const=True,
    )
    # Sign extension replicates the highest A data bit, so signed operands
    # require the MSB input entry to be a real registered cell. A const MSB
    # hole would silently zero the sign; fail closed in Python instead.
    if data_signed and "cell" not in input_bus[-1]:
        raise ValueError(
            "signed fixed-point data requires the MSB input_bus entry to be "
            "a registered cell, not a const hole"
        )
    output_bus = _bus_items(
        boundary.get("output_bus"), "boundary.output_bus", output_width
    )
    cone = boundary.get("cone_cells")
    if not isinstance(cone, list) or not 1 <= len(cone) <= 4096:
        raise ValueError("boundary.cone_cells must contain 1..4096 cells")
    cone_cells = []
    seen_cone = set()
    for index, item in enumerate(cone):
        if not isinstance(item, dict) or set(item) != {"cell", "type"}:
            raise ValueError(f"boundary.cone_cells[{index}] has an unsupported schema")
        cell = _name(item.get("cell"), f"boundary.cone_cells[{index}].cell")
        cone_type = item.get("type")
        if not isinstance(cone_type, str) or _CONE_TYPE_RE.fullmatch(cone_type) is None:
            raise ValueError(f"boundary.cone_cells[{index}].type is invalid")
        if cell in seen_cone:
            raise ValueError("boundary.cone_cells repeats a cell")
        seen_cone.add(cell)
        cone_cells.append({"cell": cell, "type": cone_type})
    boundary_cells = {item["cell"] for item in input_bus if "cell" in item}
    boundary_cells.update(item["cell"] for item in output_bus)
    if boundary_cells & seen_cone:
        raise ValueError("boundary bus cells may not be part of the removed cone")
    clock_pin = _name(boundary.get("clock_pin"), "boundary.clock_pin")
    ce_pin = _name(boundary.get("ce_pin"), "boundary.ce_pin")

    if not isinstance(placement, dict) or placement.get("kind") != (
        _PLACEMENT_MANIFEST_KIND
    ) or placement.get("version") != _PLACEMENT_MANIFEST_VERSION:
        raise ValueError("fixed-point placement manifest kind is invalid")
    if placement.get("authority") != "pre_emission_only":
        raise ValueError("fixed-point placement authority is invalid")
    if placement.get("source") != source:
        raise ValueError("fixed-point placement source differs from its authorization")
    if placement.get("authorization_id") != certificate_id:
        raise ValueError("fixed-point placement authorization identity mismatch")
    _identity(placement.get("manifest_sha256"), "placement.manifest_sha256")
    placement_candidate = placement.get("candidate_id")
    if (
        not isinstance(placement_candidate, str)
        or _CANDIDATE_ID_RE.fullmatch(placement_candidate) is None
    ):
        raise ValueError("fixed-point placement candidate identity is invalid")
    if "candidate_id" in facts and facts.get("candidate_id") != placement_candidate:
        raise ValueError("fixed-point placement candidate differs from the facts")
    records = placement.get("placements")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("fixed-point placement must provide exactly one DSP slot")
    record = records[0]
    if not isinstance(record, dict) or set(record) != {"site", "bel"}:
        raise ValueError("fixed-point placement slot schema is invalid")
    site = record.get("site")
    if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
        raise ValueError("fixed-point placement site is invalid")
    if record.get("bel") != "DSP48E2":
        raise ValueError("fixed-point placement BEL is invalid")

    input_regs, mreg = DSP_LATENCY_REGISTERS[product_latency]
    extended_coefficient = coefficient
    if coefficient_signed and (coefficient >> (coefficient_width - 1)) & 1:
        extended_coefficient |= ((1 << DSP_B_WIDTH) - 1) ^ ((1 << coefficient_width) - 1)
    prefix = f"fdagents_fixedpoint_{certificate_id[:12]}"
    return {
        "prefix": prefix,
        "dsp_cell": f"{prefix}_dsp",
        "gnd_cell": f"{prefix}_gnd",
        "vcc_cell": f"{prefix}_vcc",
        "data_width": data_width,
        "data_signed": data_signed,
        "extended_coefficient": extended_coefficient,
        "output_lsb": output_lsb,
        "output_width": output_width,
        "input_regs": input_regs,
        "mreg": mreg,
        "input_bus": input_bus,
        "output_bus": output_bus,
        "cone_cells": cone_cells,
        "clock_pin": clock_pin,
        "ce_pin": ce_pin,
        "site": site,
    }


def _static_tie_sinks(contract: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the exact GND/VCC leaf-pin inventory of the canonical config."""
    dsp = contract["dsp_cell"]
    gnd_sinks = [f"{contract['gnd_cell']}/G"]
    vcc_sinks = [f"{contract['vcc_cell']}/P"]

    def tie(pin: str, is_one: bool) -> None:
        (vcc_sinks if is_one else gnd_sinks).append(pin)

    for bit in range(9):
        tie(f"{dsp}/OPMODE[{bit}]", bool((DSP_OPMODE_BITS >> bit) & 1))
    for bit in range(4):
        tie(f"{dsp}/ALUMODE[{bit}]", bool((DSP_ALUMODE_BITS >> bit) & 1))
    for bit in range(5):
        tie(f"{dsp}/INMODE[{bit}]", bool((DSP_INMODE_BITS >> bit) & 1))
    for bit in range(3):
        tie(f"{dsp}/CARRYINSEL[{bit}]", bool((DSP_CARRYINSEL_BITS >> bit) & 1))
    tie(f"{dsp}/CARRYIN", False)
    for bit in range(DSP_B_WIDTH):
        tie(f"{dsp}/B[{bit}]", bool((contract["extended_coefficient"] >> bit) & 1))
    for bit in range(DSP_C_WIDTH):
        tie(f"{dsp}/C[{bit}]", False)
    for bit in range(DSP_D_WIDTH):
        tie(f"{dsp}/D[{bit}]", False)
    if not contract["data_signed"]:
        for bit in range(contract["data_width"], DSP_A_WIDTH):
            tie(f"{dsp}/A[{bit}]", False)
    # Const hole entries in the input bus tie their A bit to the same GND net.
    for bit, item in enumerate(contract["input_bus"]):
        if "cell" not in item:
            tie(f"{dsp}/A[{bit}]", False)
    for pin in _DSP_STATIC_GND_CE_RST_PINS:
        tie(f"{dsp}/{pin}", False)
    if contract["input_regs"] == 0:
        gnd_sinks.extend([f"{dsp}/CEA1", f"{dsp}/CEA2", f"{dsp}/CEB1", f"{dsp}/CEB2"])
    else:
        gnd_sinks.extend([f"{dsp}/CEA1", f"{dsp}/CEB1"])
    if contract["mreg"] == 0:
        gnd_sinks.append(f"{dsp}/CEM")
    return gnd_sinks, vcc_sinks


def _ce_sinks(contract: dict[str, Any]) -> list[str]:
    dsp = contract["dsp_cell"]
    sinks = [f"{dsp}/CEP"]
    if contract["input_regs"] == 1:
        sinks.extend([f"{dsp}/CEA2", f"{dsp}/CEB2"])
    if contract["mreg"] == 1:
        sinks.append(f"{dsp}/CEM")
    return sinks


def _dsp_property_pairs(contract: dict[str, Any]) -> list[tuple[str, str]]:
    pairs = [(name, value) for name, value in DSP_STATIC_STRING_PROPERTIES]
    pairs.extend((name, str(value)) for name, value in DSP_STATIC_INT_PROPERTIES)
    input_regs = contract["input_regs"]
    pairs.extend([
        ("AREG", str(input_regs)),
        ("ACASCREG", str(input_regs)),
        ("BREG", str(input_regs)),
        ("BCASCREG", str(input_regs)),
        ("MREG", str(contract["mreg"])),
    ])
    return pairs


def _port_signature_lines(proc_name: str) -> list[str]:
    """Fixed Tcl that emits the whole-design port and top-identity markers."""
    return [
        f"proc {proc_name} {{value}} {{return [binary encode base64 -maxlen 0 \"$value\"]}}",
        f"set fdagents_fp_sig_records [list]",
        "foreach fdagents_fp_sig_name [lsort [get_property NAME [get_ports -quiet *]]] {"
        "set fdagents_fp_sig_port [get_ports -quiet [list $fdagents_fp_sig_name]]; "
        "lappend fdagents_fp_sig_records "
        "\"$fdagents_fp_sig_name:[get_property DIRECTION $fdagents_fp_sig_port]\"}",
        f"puts \"{PORT_SIGNATURE_MARKER}|[{proc_name} [join $fdagents_fp_sig_records ,]]\"",
        f"puts \"{TOP_IDENTITY_MARKER}|[{proc_name} [get_property TOP [current_design]]]\"",
        f"puts {{{PORT_SIGNATURE_DONE_MARKER}}}",
    ]


def build_fixed_point_port_signature_tcl() -> str:
    """Read-only capture of the port contract and closed-top identity."""
    return "\n".join(_port_signature_lines("fdagents_fp_sig_b64"))


def parse_fixed_point_port_signature(output: str) -> dict[str, str]:
    """Decode the exact signature markers; anything else fails closed."""
    signature = None
    top_identity = None
    done = False
    for raw in str(output).splitlines():
        line = raw.strip()
        if line.startswith(PORT_SIGNATURE_MARKER + "|"):
            if signature is not None:
                raise ValueError("port signature marker repeated")
            payload = line.split("|", 1)[1]
            signature = base64.b64decode(
                payload.encode("ascii"), validate=True
            ).decode("utf-8")
        elif line.startswith(TOP_IDENTITY_MARKER + "|"):
            if top_identity is not None:
                raise ValueError("top identity marker repeated")
            payload = line.split("|", 1)[1]
            top_identity = base64.b64decode(
                payload.encode("ascii"), validate=True
            ).decode("utf-8")
        elif line == PORT_SIGNATURE_DONE_MARKER:
            done = True
    if signature is None or top_identity is None or not done:
        raise ValueError("port signature capture did not complete")
    return {"port_signature": signature, "top_identity": top_identity}


def build_fixed_point_eco_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    """Build fixed Tcl from hash-validated manifests; caller owns hash replay."""
    contract = _validated_contract(facts, authorization, placement)
    prefix = contract["prefix"]
    dsp = contract["dsp_cell"]
    lines = [
        "proc fdagents_fp_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"required pin missing: $name\"}; return $value}",
        "proc fdagents_fp_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"required cell missing: $name\"}; return $value}",
        "proc fdagents_fp_pin_net {name} {set pin [fdagents_fp_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"required pin net missing: $name\"}; return $value}",
        "proc fdagents_fp_disconnect_pin {name} {set pin [fdagents_fp_one_pin $name]; set net [get_nets -quiet -of_objects $pin]; if {[llength $net] != 1} {error \"disconnect pin net missing: $name\"}; disconnect_net -net $net -objects $pin}",
        "proc fdagents_fp_ref_prefix {name expected} {set cell [fdagents_fp_one_cell $name]; if {![string match \"${expected}*\" [get_property REF_NAME $cell]]} {error \"unexpected REF_NAME for $name: [get_property REF_NAME $cell]\"}}",
        "puts {FDAGENTS_FIXED_POINT_ECO_STAGE_00_BEGIN}",
        f"set fdagents_fp_clock_net [fdagents_fp_pin_net {_braced(contract['clock_pin'])}]",
        f"set fdagents_fp_ce_net [fdagents_fp_pin_net {_braced(contract['ce_pin'])}]",
        f"if {{[llength [get_cells -quiet {prefix}*]]}} {{error {{fixed-point ECO namespace exists}}}}",
    ]
    # Stage 1: assert the recovered boundary and cone inventory exist.
    for item in contract["cone_cells"]:
        lines.append(
            f"fdagents_fp_ref_prefix {_braced(item['cell'])} {_braced(item['type'])}"
        )
    for item in contract["input_bus"]:
        if "pin" in item:
            lines.append(f"fdagents_fp_one_pin {_braced(item['pin'])}")
    for item in contract["output_bus"]:
        lines.append(f"fdagents_fp_ref_prefix {_braced(item['cell'])} {{FD}}")
        lines.append(f"fdagents_fp_one_pin {_braced(item['pin'])}")
    lines.append("puts {FDAGENTS_FIXED_POINT_ECO_STAGE_01_BOUNDARY_ASSERTED}")

    # Stage 2: create the fixed namespace and configure the canonical DSP.
    lines.extend([
        f"create_cell -reference GND {contract['gnd_cell']}",
        f"create_cell -reference VCC {contract['vcc_cell']}",
        f"create_cell -reference DSP48E2 {dsp}",
        f"create_net {prefix}_gnd_net",
        f"create_net {prefix}_vcc_net",
    ])
    for name, value in _dsp_property_pairs(contract):
        lines.append(f"set_property {name} {value} [get_cells {_braced(dsp)}]")
    lines.append("puts {FDAGENTS_FIXED_POINT_ECO_STAGE_02_CREATED}")

    # Stage 3: static coefficient/control ties and data-bus wiring.
    gnd_sinks, vcc_sinks = _static_tie_sinks(contract)
    lines.extend([
        f"connect_net -net {prefix}_gnd_net -objects {_pins(gnd_sinks)}",
        f"connect_net -net {prefix}_vcc_net -objects {_pins(vcc_sinks)}",
        f"connect_net -hierarchical -net $fdagents_fp_clock_net -objects {_pins([f'{dsp}/CLK'])}",
        f"connect_net -hierarchical -net $fdagents_fp_ce_net -objects {_pins(_ce_sinks(contract))}",
    ])
    data_width = contract["data_width"]
    for bit, item in enumerate(contract["input_bus"]):
        if "cell" not in item:
            continue  # const hole bits are already tied to the GND net
        targets = [f"{dsp}/A[{bit}]"]
        if contract["data_signed"] and bit == data_width - 1:
            targets.extend(
                f"{dsp}/A[{extension}]" for extension in range(data_width, DSP_A_WIDTH)
            )
        lines.append(
            f"connect_net -hierarchical -net [fdagents_fp_pin_net {_braced(item['pin'])}] "
            f"-objects {_pins(targets)}"
        )
    lines.append("puts {FDAGENTS_FIXED_POINT_ECO_STAGE_03_STATIC_TIED}")

    # Stage 4: move every sink register D pin onto the DSP P slice.
    for index, item in enumerate(contract["output_bus"]):
        net = f"{prefix}_p_{index:03d}"
        p_pin = f"{dsp}/P[{contract['output_lsb'] + index}]"
        lines.extend([
            f"fdagents_fp_disconnect_pin {_braced(item['pin'])}",
            f"create_net {net}",
            f"connect_net -hierarchical -net {net} -objects {_pins([p_pin, item['pin']])}",
        ])
    lines.append("puts {FDAGENTS_FIXED_POINT_ECO_STAGE_04_REWIRED}")

    # Stage 5: remove exactly the recovered cone inventory, nothing else.
    for item in contract["cone_cells"]:
        lines.append(f"remove_cell [fdagents_fp_one_cell {_braced(item['cell'])}]")
    first_cone = contract["cone_cells"][0]["cell"]
    lines.extend([
        f"if {{[llength [get_cells -quiet [list {_braced(first_cone)}]]] != 0}} "
        "{error {fixed-point cone removal failed}}",
        "puts {FDAGENTS_FIXED_POINT_ECO_STAGE_05_CONE_REMOVED}",
    ])

    # Stage 6: place the DSP macro on the manifest site (macro cells are
    # placed by site; a bel target fails with Vivado 12-822).
    lines.extend([
        f"place_cell [get_cells {_braced(dsp)}] "
        f"[get_sites {_braced(contract['site'])}]",
        f"if {{[get_property LOC [fdagents_fp_one_cell {_braced(dsp)}]] ne "
        f"{_braced(contract['site'])}}} {{error {{fixed-point DSP placement failed}}}}",
        "puts {FDAGENTS_FIXED_POINT_ECO_STAGE_06_PLACED}",
    ])

    # Stage 7: scoped route of the touched nets only, then fail-closed check.
    lines.extend([
        f"set fdagents_fp_touched [lsort -unique [get_nets -quiet -of_objects "
        f"[get_pins -quiet -of_objects [fdagents_fp_one_cell {_braced(dsp)}]]]]",
        "if {[llength $fdagents_fp_touched] == 0} {error {fixed-point ECO touched no nets}}",
        "route_design -nets $fdagents_fp_touched",
        "set fdagents_fp_unrouted [filter -quiet $fdagents_fp_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_fp_unrouted] != 0} {error \"fixed-point ECO left [llength $fdagents_fp_unrouted] unrouted nets\"}",
        "puts {FDAGENTS_FIXED_POINT_ECO_STAGE_07_ROUTED}",
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_FIXED_POINT_ECO_DONE}",
    ])
    return "\n".join(lines)


def build_fixed_point_postcheck_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
) -> str:
    """Reopen-time assertions for the emitted fixed-point DSP topology."""
    contract = _validated_contract(facts, authorization, placement)
    prefix = contract["prefix"]
    dsp = contract["dsp_cell"]
    lines = [
        "proc fdagents_fpc_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck cell missing: $name\"}; return $value}",
        "proc fdagents_fpc_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck pin missing: $name\"}; return $value}",
        "proc fdagents_fpc_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_fpc_one_pin $name]]; if {[llength $value] == 0} {error \"postcheck pin net segments missing: $name\"}; return $value}",
        "proc fdagents_fpc_same_net {left right} {set left_nets [fdagents_fpc_pin_segments $left]; set right_nets [fdagents_fpc_pin_segments $right]; foreach net $left_nets {if {[lsearch -exact $right_nets $net] >= 0} {return}}; error \"postcheck net mismatch: $left vs $right\"}",
        "proc fdagents_fpc_ref {cell expected} {if {[get_property REF_NAME [fdagents_fpc_one_cell $cell]] ne $expected} {error \"postcheck REF_NAME mismatch: $cell\"}}",
        "proc fdagents_fpc_prop {cell name expected} {set actual [get_property $name [fdagents_fpc_one_cell $cell]]; if {\"$actual\" ne \"$expected\"} {error \"postcheck property mismatch: $cell $name expected=$expected actual=$actual\"}}",
        f"proc fdagents_fpc_static {{pin expected}} {{if {{$expected eq \"GND\"}} {{set source {_braced(contract['gnd_cell'] + '/G')}}} elseif {{$expected eq \"VCC\"}} {{set source {_braced(contract['vcc_cell'] + '/P')}}} else {{error \"postcheck static kind invalid: $expected\"}}; fdagents_fpc_same_net $pin $source}}",
        "set fdagents_fpc_expected [lsort [list "
        + " ".join(_braced(name) for name in (
            contract["gnd_cell"], contract["vcc_cell"], dsp,
        ))
        + "]]",
        f"set fdagents_fpc_actual [lsort [get_property NAME [get_cells -quiet -hierarchical {_braced(prefix + '*')}]]]",
        "if {$fdagents_fpc_actual ne $fdagents_fpc_expected} {error {postcheck namespace inventory mismatch}}",
        "puts {FDAGENTS_FIXED_POINT_INVENTORY_OK}",
        f"fdagents_fpc_ref {_braced(dsp)} DSP48E2",
        f"if {{[get_property LOC [fdagents_fpc_one_cell {_braced(dsp)}]] ne "
        f"{_braced(contract['site'])}}} {{error {{postcheck DSP placement mismatch}}}}",
    ]
    for name, value in _dsp_property_pairs(contract):
        lines.append(f"fdagents_fpc_prop {_braced(dsp)} {name} {_braced(value)}")
    gnd_sinks, vcc_sinks = _static_tie_sinks(contract)
    for pin in gnd_sinks:
        if pin != contract["gnd_cell"] + "/G":
            lines.append(f"fdagents_fpc_static {_braced(pin)} GND")
    for pin in vcc_sinks:
        if pin != contract["vcc_cell"] + "/P":
            lines.append(f"fdagents_fpc_static {_braced(pin)} VCC")
    lines.append(
        f"fdagents_fpc_same_net {_braced(dsp + '/CLK')} {_braced(contract['clock_pin'])}"
    )
    for pin in _ce_sinks(contract):
        lines.append(
            f"fdagents_fpc_same_net {_braced(pin)} {_braced(contract['ce_pin'])}"
        )
    data_width = contract["data_width"]
    for bit, item in enumerate(contract["input_bus"]):
        if "cell" not in item:
            continue  # const hole A bits are asserted via the GND tie list
        lines.append(
            f"fdagents_fpc_same_net {_braced(dsp + f'/A[{bit}]')} {_braced(item['pin'])}"
        )
        if contract["data_signed"] and bit == data_width - 1:
            for extension in range(data_width, DSP_A_WIDTH):
                lines.append(
                    f"fdagents_fpc_same_net {_braced(dsp + f'/A[{extension}]')} "
                    f"{_braced(item['pin'])}"
                )
    for index, item in enumerate(contract["output_bus"]):
        p_pin = f"{dsp}/P[{contract['output_lsb'] + index}]"
        lines.append(
            f"fdagents_fpc_same_net {_braced(p_pin)} {_braced(item['pin'])}"
        )
    for item in contract["cone_cells"]:
        lines.append(
            f"if {{[llength [get_cells -quiet [list {_braced(item['cell'])}]]] != 0}} "
            "{error {postcheck cone cell still exists}}"
        )
    lines.extend([
        "puts {FDAGENTS_FIXED_POINT_CONE_REMOVED_OK}",
        f"set fdagents_fpc_touched [lsort -unique [get_nets -quiet -of_objects "
        f"[get_pins -quiet -of_objects [fdagents_fpc_one_cell {_braced(dsp)}]]]]",
        "set fdagents_fpc_unrouted [filter -quiet $fdagents_fpc_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_fpc_unrouted] != 0} {error {postcheck found unrouted touched nets}}",
        "puts {FDAGENTS_FIXED_POINT_ROUTED_OK}",
    ])
    lines.extend(_port_signature_lines("fdagents_fpc_b64"))
    lines.append("puts {FDAGENTS_FIXED_POINT_POSTCHECK_DONE}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Two-operand multiply template (DSP48E2_MUL_TWO_OPERAND_V1).
#
# Same canonical static DSP48E2 configuration as the fixed-point template
# above (OPMODE/ALUMODE/INMODE/CARRYINSEL, PREG accumulate feedback, and the
# shared latency register budget), except that the B port is wired from the
# recovered ``input_bus_b`` nets per bit instead of being tied to a constant
# coefficient. B-side holes tie to GND and a signed B MSB is replicated
# across B[b_width..17] exactly like the A-side sign extension. Because B now
# carries registered data clocked like A, CEB1/CEB2 follow the identical
# tie/CE budget already used for CEA1/CEA2 (latency 1 ties CEA*/CEB* to GND;
# latency >= 2 drives CEA2/CEB2 from the boundary CE net with CEA1/CEB1 tied
# to GND, matching AREG=BREG=1).
# ---------------------------------------------------------------------------

_TWO_OPERAND_PLACEMENT_MANIFEST_KIND = "two_operand_placement_manifest"
_TWO_OPERAND_PLACEMENT_MANIFEST_VERSION = 1
_TWO_OPERAND_FACTS_KIND = "two_operand_product_contract_facts"
_TWO_OPERAND_PLAN_TEMPLATE = "DSP48E2_MUL_TWO_OPERAND_V1"
_TWO_OPERAND_CANDIDATE_ID_RE = re.compile(r"^twooperand:[0-9a-f]{20}$")


def _validated_two_operand_contract(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Cross-check every two-operand manifest in Python before any Tcl."""
    if not isinstance(facts, dict) or facts.get("kind") != _TWO_OPERAND_FACTS_KIND:
        raise ValueError("two-operand facts manifest kind is invalid")
    spec = facts.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("two-operand facts spec is missing")
    if not isinstance(authorization, dict) or authorization.get("kind") != (
        "operator_rewrite_authorization"
    ):
        raise ValueError("two-operand authorization kind is invalid")
    if authorization.get("rewrite_kind") != "fabric_to_dsp_two_operand":
        raise ValueError("two-operand authorization rewrite kind is invalid")
    if authorization.get("authority") != "pre_mutation_only":
        raise ValueError("two-operand authorization authority is invalid")
    certificate_id = authorization.get("certificate_id")
    _identity(certificate_id, "authorization.certificate_id")
    if authorization.get("spec") != spec:
        raise ValueError("two-operand authorization spec differs from the facts")
    source = authorization.get("source")
    if not isinstance(source, dict) or set(source) != {"dcp_sha256", "region_sha256"}:
        raise ValueError("two-operand authorization source identity is incomplete")
    _identity(source.get("dcp_sha256"), "authorization.source.dcp_sha256")
    _identity(source.get("region_sha256"), "authorization.source.region_sha256")
    if facts.get("region_sha256") != source["region_sha256"]:
        raise ValueError("two-operand facts region differs from its authorization")
    if "design_sha256" in facts and facts.get("design_sha256") != source["dcp_sha256"]:
        raise ValueError("two-operand facts design differs from its authorization")

    plan = authorization.get("plan")
    if not isinstance(plan, dict) or plan.get("template") != _TWO_OPERAND_PLAN_TEMPLATE:
        raise ValueError("two-operand plan template is unsupported")
    a_width = _int_field(spec.get("a_width"), "spec.a_width", 1, DSP_A_MULT_WIDTH)
    b_width = _int_field(spec.get("b_width"), "spec.b_width", 1, DSP_B_WIDTH)
    product_width = _int_field(spec.get("product_width"), "spec.product_width", 1, 256)
    if product_width < a_width + b_width:
        raise ValueError("spec.product_width cannot represent the full product")
    product_latency = _int_field(
        spec.get("product_latency"), "spec.product_latency", 1, 3
    )
    if product_latency not in DSP_LATENCY_REGISTERS:
        raise ValueError("spec.product_latency has no supported register budget")
    if plan.get("product_latency") != product_latency:
        raise ValueError("plan latency differs from the recovered contract")
    if plan.get("accumulator_width") != spec.get("accumulator_width"):
        raise ValueError("plan accumulator width differs from the recovered contract")
    output_lsb = _int_field(plan.get("output_lsb"), "plan.output_lsb", 0, DSP_P_WIDTH - 1)
    output_width = _int_field(plan.get("output_width"), "plan.output_width", 1, DSP_P_WIDTH)
    if output_lsb + output_width > DSP_P_WIDTH:
        raise ValueError("plan output slice exceeds the DSP P port")
    if output_lsb + output_width > product_width:
        raise ValueError("plan output slice exceeds the recovered product width")
    a_signed = spec.get("a_signed")
    b_signed = spec.get("b_signed")
    if not isinstance(a_signed, bool) or not isinstance(b_signed, bool):
        raise ValueError("spec signedness flags must be booleans")
    if plan.get("a_signed") != a_signed or plan.get("b_signed") != b_signed:
        raise ValueError("plan signedness differs from the recovered contract")

    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != {
        "input_bus_a", "input_bus_b", "output_bus", "cone_cells",
        "clock_pin", "ce_pin",
    }:
        raise ValueError("two-operand boundary manifest is incomplete")
    input_bus_a = _bus_items(
        boundary.get("input_bus_a"), "boundary.input_bus_a", a_width,
        allow_const=True,
    )
    input_bus_b = _bus_items(
        boundary.get("input_bus_b"), "boundary.input_bus_b", b_width,
        allow_const=True,
    )
    # Sign extension replicates the highest operand bit, so each signed
    # operand requires its MSB bus entry to be a real registered cell. A
    # const MSB hole would silently zero the sign; fail closed in Python.
    if a_signed and "cell" not in input_bus_a[-1]:
        raise ValueError(
            "signed two-operand data requires the MSB input_bus_a entry to "
            "be a registered cell, not a const hole"
        )
    if b_signed and "cell" not in input_bus_b[-1]:
        raise ValueError(
            "signed two-operand data requires the MSB input_bus_b entry to "
            "be a registered cell, not a const hole"
        )
    output_bus = _bus_items(
        boundary.get("output_bus"), "boundary.output_bus", output_width
    )
    cone = boundary.get("cone_cells")
    if not isinstance(cone, list) or not 1 <= len(cone) <= 4096:
        raise ValueError("boundary.cone_cells must contain 1..4096 cells")
    cone_cells = []
    seen_cone = set()
    for index, item in enumerate(cone):
        if not isinstance(item, dict) or set(item) != {"cell", "type"}:
            raise ValueError(f"boundary.cone_cells[{index}] has an unsupported schema")
        cell = _name(item.get("cell"), f"boundary.cone_cells[{index}].cell")
        cone_type = item.get("type")
        if not isinstance(cone_type, str) or _CONE_TYPE_RE.fullmatch(cone_type) is None:
            raise ValueError(f"boundary.cone_cells[{index}].type is invalid")
        if cell in seen_cone:
            raise ValueError("boundary.cone_cells repeats a cell")
        seen_cone.add(cell)
        cone_cells.append({"cell": cell, "type": cone_type})
    boundary_cells = {item["cell"] for item in input_bus_a if "cell" in item}
    boundary_cells.update(item["cell"] for item in input_bus_b if "cell" in item)
    boundary_cells.update(item["cell"] for item in output_bus)
    if boundary_cells & seen_cone:
        raise ValueError("boundary bus cells may not be part of the removed cone")
    clock_pin = _name(boundary.get("clock_pin"), "boundary.clock_pin")
    ce_pin = _name(boundary.get("ce_pin"), "boundary.ce_pin")

    if not isinstance(placement, dict) or placement.get("kind") != (
        _TWO_OPERAND_PLACEMENT_MANIFEST_KIND
    ) or placement.get("version") != _TWO_OPERAND_PLACEMENT_MANIFEST_VERSION:
        raise ValueError("two-operand placement manifest kind is invalid")
    if placement.get("authority") != "pre_emission_only":
        raise ValueError("two-operand placement authority is invalid")
    if placement.get("source") != source:
        raise ValueError("two-operand placement source differs from its authorization")
    if placement.get("authorization_id") != certificate_id:
        raise ValueError("two-operand placement authorization identity mismatch")
    _identity(placement.get("manifest_sha256"), "placement.manifest_sha256")
    placement_candidate = placement.get("candidate_id")
    if (
        not isinstance(placement_candidate, str)
        or _TWO_OPERAND_CANDIDATE_ID_RE.fullmatch(placement_candidate) is None
    ):
        raise ValueError("two-operand placement candidate identity is invalid")
    if "candidate_id" in facts and facts.get("candidate_id") != placement_candidate:
        raise ValueError("two-operand placement candidate differs from the facts")
    records = placement.get("placements")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("two-operand placement must provide exactly one DSP slot")
    record = records[0]
    if not isinstance(record, dict) or set(record) != {"site", "bel"}:
        raise ValueError("two-operand placement slot schema is invalid")
    site = record.get("site")
    if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
        raise ValueError("two-operand placement site is invalid")
    if record.get("bel") != "DSP48E2":
        raise ValueError("two-operand placement BEL is invalid")

    input_regs, mreg = DSP_LATENCY_REGISTERS[product_latency]
    prefix = f"fdagents_twooperand_{certificate_id[:12]}"
    return {
        "prefix": prefix,
        "dsp_cell": f"{prefix}_dsp",
        "gnd_cell": f"{prefix}_gnd",
        "vcc_cell": f"{prefix}_vcc",
        "a_width": a_width,
        "b_width": b_width,
        "a_signed": a_signed,
        "b_signed": b_signed,
        "output_lsb": output_lsb,
        "output_width": output_width,
        "input_regs": input_regs,
        "mreg": mreg,
        "input_bus_a": input_bus_a,
        "input_bus_b": input_bus_b,
        "output_bus": output_bus,
        "cone_cells": cone_cells,
        "clock_pin": clock_pin,
        "ce_pin": ce_pin,
        "site": site,
    }


def _two_operand_static_tie_sinks(
    contract: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Exact GND/VCC leaf-pin inventory; B is bus-wired, never coefficient-tied.

    Mirrors ``_static_tie_sinks`` bit for bit on the control side. Only the
    hole/extension GND ties of both operand buses replace the fixed-point
    coefficient B ties, and the CEA*/CEB* budget is byte-identical: with
    AREG=BREG=0 every CEA*/CEB* pin ties to GND; with AREG=BREG=1 only
    CEA1/CEB1 tie to GND while CEA2/CEB2 join the boundary CE net.
    """
    dsp = contract["dsp_cell"]
    gnd_sinks = [f"{contract['gnd_cell']}/G"]
    vcc_sinks = [f"{contract['vcc_cell']}/P"]

    def tie(pin: str, is_one: bool) -> None:
        (vcc_sinks if is_one else gnd_sinks).append(pin)

    for bit in range(9):
        tie(f"{dsp}/OPMODE[{bit}]", bool((DSP_OPMODE_BITS >> bit) & 1))
    for bit in range(4):
        tie(f"{dsp}/ALUMODE[{bit}]", bool((DSP_ALUMODE_BITS >> bit) & 1))
    for bit in range(5):
        tie(f"{dsp}/INMODE[{bit}]", bool((DSP_INMODE_BITS >> bit) & 1))
    for bit in range(3):
        tie(f"{dsp}/CARRYINSEL[{bit}]", bool((DSP_CARRYINSEL_BITS >> bit) & 1))
    tie(f"{dsp}/CARRYIN", False)
    for bit in range(DSP_C_WIDTH):
        tie(f"{dsp}/C[{bit}]", False)
    for bit in range(DSP_D_WIDTH):
        tie(f"{dsp}/D[{bit}]", False)
    if not contract["a_signed"]:
        for bit in range(contract["a_width"], DSP_A_WIDTH):
            tie(f"{dsp}/A[{bit}]", False)
    # Const hole entries in either bus tie their operand bit to shared GND.
    for bit, item in enumerate(contract["input_bus_a"]):
        if "cell" not in item:
            tie(f"{dsp}/A[{bit}]", False)
    if not contract["b_signed"]:
        for bit in range(contract["b_width"], DSP_B_WIDTH):
            tie(f"{dsp}/B[{bit}]", False)
    for bit, item in enumerate(contract["input_bus_b"]):
        if "cell" not in item:
            tie(f"{dsp}/B[{bit}]", False)
    for pin in _DSP_STATIC_GND_CE_RST_PINS:
        tie(f"{dsp}/{pin}", False)
    if contract["input_regs"] == 0:
        gnd_sinks.extend([f"{dsp}/CEA1", f"{dsp}/CEA2", f"{dsp}/CEB1", f"{dsp}/CEB2"])
    else:
        gnd_sinks.extend([f"{dsp}/CEA1", f"{dsp}/CEB1"])
    if contract["mreg"] == 0:
        gnd_sinks.append(f"{dsp}/CEM")
    return gnd_sinks, vcc_sinks


def _two_operand_bus_connect_targets(
    contract: dict[str, Any], bus_key: str
) -> list[tuple[dict[str, Any], list[str]]]:
    """Per-bit DSP pin targets for one operand bus, incl. MSB sign extension."""
    dsp = contract["dsp_cell"]
    if bus_key == "input_bus_a":
        port, width, port_width, signed = (
            "A", contract["a_width"], DSP_A_WIDTH, contract["a_signed"]
        )
    else:
        port, width, port_width, signed = (
            "B", contract["b_width"], DSP_B_WIDTH, contract["b_signed"]
        )
    result = []
    for bit, item in enumerate(contract[bus_key]):
        if "cell" not in item:
            continue  # const hole bits are already tied to the GND net
        targets = [f"{dsp}/{port}[{bit}]"]
        if signed and bit == width - 1:
            targets.extend(
                f"{dsp}/{port}[{extension}]"
                for extension in range(width, port_width)
            )
        result.append((item, targets))
    return result


def build_two_operand_eco_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    """Build fixed Tcl from hash-validated manifests; caller owns hash replay."""
    contract = _validated_two_operand_contract(facts, authorization, placement)
    prefix = contract["prefix"]
    dsp = contract["dsp_cell"]
    lines = [
        "proc fdagents_to_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"required pin missing: $name\"}; return $value}",
        "proc fdagents_to_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"required cell missing: $name\"}; return $value}",
        "proc fdagents_to_pin_net {name} {set pin [fdagents_to_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"required pin net missing: $name\"}; return $value}",
        "proc fdagents_to_disconnect_pin {name} {set pin [fdagents_to_one_pin $name]; set net [get_nets -quiet -of_objects $pin]; if {[llength $net] != 1} {error \"disconnect pin net missing: $name\"}; disconnect_net -net $net -objects $pin}",
        "proc fdagents_to_ref_prefix {name expected} {set cell [fdagents_to_one_cell $name]; if {![string match \"${expected}*\" [get_property REF_NAME $cell]]} {error \"unexpected REF_NAME for $name: [get_property REF_NAME $cell]\"}}",
        "puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_00_BEGIN}",
        f"set fdagents_to_clock_net [fdagents_to_pin_net {_braced(contract['clock_pin'])}]",
        f"set fdagents_to_ce_net [fdagents_to_pin_net {_braced(contract['ce_pin'])}]",
        f"if {{[llength [get_cells -quiet {prefix}*]]}} {{error {{two-operand ECO namespace exists}}}}",
    ]
    # Stage 1: assert the recovered boundary and cone inventory exist.
    for item in contract["cone_cells"]:
        lines.append(
            f"fdagents_to_ref_prefix {_braced(item['cell'])} {_braced(item['type'])}"
        )
    for bus_key in ("input_bus_a", "input_bus_b"):
        for item in contract[bus_key]:
            if "pin" in item:
                lines.append(f"fdagents_to_one_pin {_braced(item['pin'])}")
    for item in contract["output_bus"]:
        lines.append(f"fdagents_to_ref_prefix {_braced(item['cell'])} {{FD}}")
        lines.append(f"fdagents_to_one_pin {_braced(item['pin'])}")
    lines.append("puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_01_BOUNDARY_ASSERTED}")

    # Stage 2: create the fixed namespace and configure the canonical DSP.
    lines.extend([
        f"create_cell -reference GND {contract['gnd_cell']}",
        f"create_cell -reference VCC {contract['vcc_cell']}",
        f"create_cell -reference DSP48E2 {dsp}",
        f"create_net {prefix}_gnd_net",
        f"create_net {prefix}_vcc_net",
    ])
    for name, value in _dsp_property_pairs(contract):
        lines.append(f"set_property {name} {value} [get_cells {_braced(dsp)}]")
    lines.append("puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_02_CREATED}")

    # Stage 3: static control ties and per-bit wiring of both operand buses.
    gnd_sinks, vcc_sinks = _two_operand_static_tie_sinks(contract)
    lines.extend([
        f"connect_net -net {prefix}_gnd_net -objects {_pins(gnd_sinks)}",
        f"connect_net -net {prefix}_vcc_net -objects {_pins(vcc_sinks)}",
        f"connect_net -hierarchical -net $fdagents_to_clock_net -objects {_pins([f'{dsp}/CLK'])}",
        f"connect_net -hierarchical -net $fdagents_to_ce_net -objects {_pins(_ce_sinks(contract))}",
    ])
    for bus_key in ("input_bus_a", "input_bus_b"):
        for item, targets in _two_operand_bus_connect_targets(contract, bus_key):
            lines.append(
                f"connect_net -hierarchical -net [fdagents_to_pin_net {_braced(item['pin'])}] "
                f"-objects {_pins(targets)}"
            )
    lines.append("puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_03_STATIC_TIED}")

    # Stage 4: move every sink register D pin onto the DSP P slice.
    for index, item in enumerate(contract["output_bus"]):
        net = f"{prefix}_p_{index:03d}"
        p_pin = f"{dsp}/P[{contract['output_lsb'] + index}]"
        lines.extend([
            f"fdagents_to_disconnect_pin {_braced(item['pin'])}",
            f"create_net {net}",
            f"connect_net -hierarchical -net {net} -objects {_pins([p_pin, item['pin']])}",
        ])
    lines.append("puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_04_REWIRED}")

    # Stage 5: remove exactly the recovered cone inventory, nothing else.
    for item in contract["cone_cells"]:
        lines.append(f"remove_cell [fdagents_to_one_cell {_braced(item['cell'])}]")
    first_cone = contract["cone_cells"][0]["cell"]
    lines.extend([
        f"if {{[llength [get_cells -quiet [list {_braced(first_cone)}]]] != 0}} "
        "{error {two-operand cone removal failed}}",
        "puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_05_CONE_REMOVED}",
    ])

    # Stage 6: place the DSP macro on the manifest site (macro cells are
    # placed by site; a bel target fails with Vivado 12-822).
    lines.extend([
        f"place_cell [get_cells {_braced(dsp)}] "
        f"[get_sites {_braced(contract['site'])}]",
        f"if {{[get_property LOC [fdagents_to_one_cell {_braced(dsp)}]] ne "
        f"{_braced(contract['site'])}}} {{error {{two-operand DSP placement failed}}}}",
        "puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_06_PLACED}",
    ])

    # Stage 7: scoped route of the touched nets only, then fail-closed check.
    lines.extend([
        f"set fdagents_to_touched [lsort -unique [get_nets -quiet -of_objects "
        f"[get_pins -quiet -of_objects [fdagents_to_one_cell {_braced(dsp)}]]]]",
        "if {[llength $fdagents_to_touched] == 0} {error {two-operand ECO touched no nets}}",
        "route_design -nets $fdagents_to_touched",
        "set fdagents_to_unrouted [filter -quiet $fdagents_to_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_to_unrouted] != 0} {error \"two-operand ECO left [llength $fdagents_to_unrouted] unrouted nets\"}",
        "puts {FDAGENTS_TWO_OPERAND_ECO_STAGE_07_ROUTED}",
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_TWO_OPERAND_ECO_DONE}",
    ])
    return "\n".join(lines)


def build_two_operand_postcheck_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
) -> str:
    """Reopen-time assertions for the emitted two-operand DSP topology."""
    contract = _validated_two_operand_contract(facts, authorization, placement)
    prefix = contract["prefix"]
    dsp = contract["dsp_cell"]
    lines = [
        "proc fdagents_toc_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck cell missing: $name\"}; return $value}",
        "proc fdagents_toc_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck pin missing: $name\"}; return $value}",
        "proc fdagents_toc_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_toc_one_pin $name]]; if {[llength $value] == 0} {error \"postcheck pin net segments missing: $name\"}; return $value}",
        "proc fdagents_toc_same_net {left right} {set left_nets [fdagents_toc_pin_segments $left]; set right_nets [fdagents_toc_pin_segments $right]; foreach net $left_nets {if {[lsearch -exact $right_nets $net] >= 0} {return}}; error \"postcheck net mismatch: $left vs $right\"}",
        "proc fdagents_toc_ref {cell expected} {if {[get_property REF_NAME [fdagents_toc_one_cell $cell]] ne $expected} {error \"postcheck REF_NAME mismatch: $cell\"}}",
        "proc fdagents_toc_prop {cell name expected} {set actual [get_property $name [fdagents_toc_one_cell $cell]]; if {\"$actual\" ne \"$expected\"} {error \"postcheck property mismatch: $cell $name expected=$expected actual=$actual\"}}",
        f"proc fdagents_toc_static {{pin expected}} {{if {{$expected eq \"GND\"}} {{set source {_braced(contract['gnd_cell'] + '/G')}}} elseif {{$expected eq \"VCC\"}} {{set source {_braced(contract['vcc_cell'] + '/P')}}} else {{error \"postcheck static kind invalid: $expected\"}}; fdagents_toc_same_net $pin $source}}",
        "set fdagents_toc_expected [lsort [list "
        + " ".join(_braced(name) for name in (
            contract["gnd_cell"], contract["vcc_cell"], dsp,
        ))
        + "]]",
        f"set fdagents_toc_actual [lsort [get_property NAME [get_cells -quiet -hierarchical {_braced(prefix + '*')}]]]",
        "if {$fdagents_toc_actual ne $fdagents_toc_expected} {error {postcheck namespace inventory mismatch}}",
        "puts {FDAGENTS_TWO_OPERAND_INVENTORY_OK}",
        f"fdagents_toc_ref {_braced(dsp)} DSP48E2",
        f"if {{[get_property LOC [fdagents_toc_one_cell {_braced(dsp)}]] ne "
        f"{_braced(contract['site'])}}} {{error {{postcheck DSP placement mismatch}}}}",
    ]
    for name, value in _dsp_property_pairs(contract):
        lines.append(f"fdagents_toc_prop {_braced(dsp)} {name} {_braced(value)}")
    gnd_sinks, vcc_sinks = _two_operand_static_tie_sinks(contract)
    for pin in gnd_sinks:
        if pin != contract["gnd_cell"] + "/G":
            lines.append(f"fdagents_toc_static {_braced(pin)} GND")
    for pin in vcc_sinks:
        if pin != contract["vcc_cell"] + "/P":
            lines.append(f"fdagents_toc_static {_braced(pin)} VCC")
    lines.append(
        f"fdagents_toc_same_net {_braced(dsp + '/CLK')} {_braced(contract['clock_pin'])}"
    )
    for pin in _ce_sinks(contract):
        lines.append(
            f"fdagents_toc_same_net {_braced(pin)} {_braced(contract['ce_pin'])}"
        )
    for bus_key in ("input_bus_a", "input_bus_b"):
        # Const hole operand bits are asserted via the GND tie list above.
        for item, targets in _two_operand_bus_connect_targets(contract, bus_key):
            for target in targets:
                lines.append(
                    f"fdagents_toc_same_net {_braced(target)} {_braced(item['pin'])}"
                )
    for index, item in enumerate(contract["output_bus"]):
        p_pin = f"{dsp}/P[{contract['output_lsb'] + index}]"
        lines.append(
            f"fdagents_toc_same_net {_braced(p_pin)} {_braced(item['pin'])}"
        )
    for item in contract["cone_cells"]:
        lines.append(
            f"if {{[llength [get_cells -quiet [list {_braced(item['cell'])}]]] != 0}} "
            "{error {postcheck cone cell still exists}}"
        )
    lines.extend([
        "puts {FDAGENTS_TWO_OPERAND_CONE_REMOVED_OK}",
        f"set fdagents_toc_touched [lsort -unique [get_nets -quiet -of_objects "
        f"[get_pins -quiet -of_objects [fdagents_toc_one_cell {_braced(dsp)}]]]]",
        "set fdagents_toc_unrouted [filter -quiet $fdagents_toc_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_toc_unrouted] != 0} {error {postcheck found unrouted touched nets}}",
        "puts {FDAGENTS_TWO_OPERAND_ROUTED_OK}",
    ])
    lines.extend(_port_signature_lines("fdagents_toc_b64"))
    lines.append("puts {FDAGENTS_TWO_OPERAND_POSTCHECK_DONE}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Register-absorb template (DSP48E2_INPUT_REG_ABSORB_V1).
#
# Unlike the rebuild templates above, no cells are created and the DSP stays
# on its existing placed site: a proved fabric FDRE bus that solely feeds one
# DSP48E2 operand port is deleted, and the DSP-internal input register
# (AREG/BREG 0 -> 1, with the matching cascade register) absorbs it. The
# boundary CE and the optional synchronous zero reset move onto CEA2/CEB2
# and RSTA/RSTB. Because the FDRE bus (and with it the recorded boundary
# control pins) is removed by the ECO, stages 1/2 capture the control and
# per-bit driver NET names as base64 markers; the handler replays those
# captured names into the postcheck builder for reopen-time name-equality
# assertions against the surviving DSP pins.
# ---------------------------------------------------------------------------

_REGISTER_ABSORB_PLACEMENT_MANIFEST_KIND = "register_absorb_placement_manifest"
_REGISTER_ABSORB_PLACEMENT_MANIFEST_VERSION = 1
_REGISTER_ABSORB_FACTS_KIND = "register_absorb_contract_facts"
_REGISTER_ABSORB_PLAN_TEMPLATE = "DSP48E2_INPUT_REG_ABSORB_V1"
_REGISTER_ABSORB_CANDIDATE_ID_RE = re.compile(r"^regabsorb:[0-9a-f]{20}$")
_REGISTER_ABSORB_BIT_INDEX_RE = re.compile(r"^\d{3}$")
# Reserved cell-name namespace: this template never creates cells, so the
# postcheck asserts the reserved prefix stays empty in the candidate.
_REGISTER_ABSORB_NAMESPACE_PREFIX = "fdagents_regabsorb_"
_REGISTER_ABSORB_SIDE_INFO = {
    "a": {
        "port": "A", "cap": DSP_A_WIDTH, "reg": "AREG", "cascade": "ACASCREG",
        "ce2": "CEA2", "ce1": "CEA1", "rst": "RSTA",
    },
    "b": {
        "port": "B", "cap": DSP_B_WIDTH, "reg": "BREG", "cascade": "BCASCREG",
        "ce2": "CEB2", "ce1": "CEB1", "rst": "RSTB",
    },
}

REGISTER_ABSORB_CLOCK_NET_MARKER = "FDAGENTS_REGISTER_ABSORB_CLOCK_NET"
REGISTER_ABSORB_CE_NET_MARKER = "FDAGENTS_REGISTER_ABSORB_CE_NET"
REGISTER_ABSORB_RESET_NET_MARKER = "FDAGENTS_REGISTER_ABSORB_RESET_NET"
REGISTER_ABSORB_BIT_NET_MARKER = "FDAGENTS_REGISTER_ABSORB_BIT_NET"
REGISTER_ABSORB_CAPTURE_DONE_MARKER = "FDAGENTS_REGISTER_ABSORB_CAPTURE_DONE"


def _validated_register_absorb_contract(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Cross-check every register-absorb manifest in Python before any Tcl."""
    if not isinstance(facts, dict) or facts.get("kind") != _REGISTER_ABSORB_FACTS_KIND:
        raise ValueError("register-absorb facts manifest kind is invalid")
    spec = facts.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("register-absorb facts spec is missing")
    if not isinstance(authorization, dict) or authorization.get("kind") != (
        "operator_rewrite_authorization"
    ):
        raise ValueError("register-absorb authorization kind is invalid")
    if authorization.get("rewrite_kind") != "fabric_ff_to_dsp_input_reg":
        raise ValueError("register-absorb authorization rewrite kind is invalid")
    if authorization.get("authority") != "pre_mutation_only":
        raise ValueError("register-absorb authorization authority is invalid")
    certificate_id = authorization.get("certificate_id")
    _identity(certificate_id, "authorization.certificate_id")
    if authorization.get("spec") != spec:
        raise ValueError("register-absorb authorization spec differs from the facts")
    source = authorization.get("source")
    if not isinstance(source, dict) or set(source) != {"dcp_sha256", "region_sha256"}:
        raise ValueError("register-absorb authorization source identity is incomplete")
    _identity(source.get("dcp_sha256"), "authorization.source.dcp_sha256")
    _identity(source.get("region_sha256"), "authorization.source.region_sha256")
    if facts.get("region_sha256") != source["region_sha256"]:
        raise ValueError("register-absorb facts region differs from its authorization")
    if "design_sha256" in facts and facts.get("design_sha256") != source["dcp_sha256"]:
        raise ValueError("register-absorb facts design differs from its authorization")

    plan = authorization.get("plan")
    if not isinstance(plan, dict) or plan.get("template") != (
        _REGISTER_ABSORB_PLAN_TEMPLATE
    ):
        raise ValueError("register-absorb plan template is unsupported")
    dsp_side = spec.get("dsp_side")
    if dsp_side not in _REGISTER_ABSORB_SIDE_INFO:
        raise ValueError('spec.dsp_side must be "a" or "b"')
    side = _REGISTER_ABSORB_SIDE_INFO[dsp_side]
    bus_width = _int_field(spec.get("bus_width"), "spec.bus_width", 1, side["cap"])
    if spec.get("ce_present") is not True:
        raise ValueError("spec.ce_present must be True for this template")
    if spec.get("init_zero") is not True:
        raise ValueError("spec.init_zero must be True for this template")
    reset_kind = spec.get("reset_kind")
    if reset_kind not in ("none", "sync_reset_zero"):
        raise ValueError('spec.reset_kind must be "none" or "sync_reset_zero"')
    absorb_stages = spec.get("absorb_stages")
    if isinstance(absorb_stages, bool) or absorb_stages != 1:
        raise ValueError("spec.absorb_stages must be exactly 1")
    if plan.get("dsp_side") != dsp_side:
        raise ValueError("plan side differs from the recovered contract")
    if plan.get("target_reg") != side["reg"]:
        raise ValueError("plan target register does not correspond to its side")
    target_value = plan.get("target_value")
    if isinstance(target_value, bool) or target_value != 1:
        raise ValueError("plan.target_value must be exactly 1")
    if plan.get("ce_mapped") is not True:
        raise ValueError("plan.ce_mapped must be True for this template")
    reset_mapped = plan.get("reset_mapped")
    if not isinstance(reset_mapped, bool):
        raise ValueError("plan.reset_mapped must be a boolean")
    if reset_mapped != (reset_kind == "sync_reset_zero"):
        raise ValueError("plan.reset_mapped disagrees with the recovered reset kind")

    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != {
        "ff_bus", "driver_nets_complete", "dsp_cell", "dsp_site", "dsp_side",
        "clock_pin", "ce_pin", "reset_pin",
    }:
        raise ValueError("register-absorb boundary manifest is incomplete")
    ff_bus = _bus_items(boundary.get("ff_bus"), "boundary.ff_bus", bus_width)
    for index, item in enumerate(ff_bus):
        if item["pin"] != item["cell"] + "/Q":
            raise ValueError(f"boundary.ff_bus[{index}] pin must be its FDRE Q pin")
    if boundary.get("driver_nets_complete") is not True:
        raise ValueError("boundary.driver_nets_complete must be True")
    dsp_cell = _name(boundary.get("dsp_cell"), "boundary.dsp_cell")
    dsp_site = boundary.get("dsp_site")
    if not isinstance(dsp_site, str) or _DSP_SITE_RE.fullmatch(dsp_site) is None:
        raise ValueError("boundary.dsp_site is invalid")
    if boundary.get("dsp_side") != dsp_side:
        raise ValueError("boundary side differs from the recovered contract")
    if dsp_cell in {item["cell"] for item in ff_bus}:
        raise ValueError("boundary.dsp_cell may not be part of the absorbed bus")
    first_ff = ff_bus[0]["cell"]
    clock_pin = _name(boundary.get("clock_pin"), "boundary.clock_pin")
    if clock_pin != first_ff + "/C":
        raise ValueError("boundary.clock_pin must be the first absorbed FDRE C pin")
    ce_pin = _name(boundary.get("ce_pin"), "boundary.ce_pin")
    if ce_pin != first_ff + "/CE":
        raise ValueError("boundary.ce_pin must be the first absorbed FDRE CE pin")
    reset_pin = boundary.get("reset_pin")
    if reset_mapped:
        reset_pin = _name(reset_pin, "boundary.reset_pin")
        if reset_pin != first_ff + "/R":
            raise ValueError(
                "boundary.reset_pin must be the first absorbed FDRE R pin"
            )
    elif reset_pin is not None:
        raise ValueError("boundary.reset_pin must be None when no reset is mapped")

    if not isinstance(placement, dict) or placement.get("kind") != (
        _REGISTER_ABSORB_PLACEMENT_MANIFEST_KIND
    ) or placement.get("version") != _REGISTER_ABSORB_PLACEMENT_MANIFEST_VERSION:
        raise ValueError("register-absorb placement manifest kind is invalid")
    if placement.get("authority") != "pre_emission_only":
        raise ValueError("register-absorb placement authority is invalid")
    if placement.get("source") != source:
        raise ValueError(
            "register-absorb placement source differs from its authorization"
        )
    if placement.get("authorization_id") != certificate_id:
        raise ValueError("register-absorb placement authorization identity mismatch")
    _identity(placement.get("manifest_sha256"), "placement.manifest_sha256")
    placement_candidate = placement.get("candidate_id")
    if (
        not isinstance(placement_candidate, str)
        or _REGISTER_ABSORB_CANDIDATE_ID_RE.fullmatch(placement_candidate) is None
    ):
        raise ValueError("register-absorb placement candidate identity is invalid")
    if "candidate_id" in facts and facts.get("candidate_id") != placement_candidate:
        raise ValueError("register-absorb placement candidate differs from the facts")
    records = placement.get("placements")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("register-absorb placement must provide exactly one DSP slot")
    record = records[0]
    if not isinstance(record, dict) or set(record) != {"site", "bel"}:
        raise ValueError("register-absorb placement slot schema is invalid")
    site = record.get("site")
    if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
        raise ValueError("register-absorb placement site is invalid")
    if record.get("bel") != "DSP48E2":
        raise ValueError("register-absorb placement BEL is invalid")
    if site != dsp_site:
        raise ValueError(
            "register-absorb placement site differs from the recovered DSP site"
        )

    return {
        "side": dsp_side,
        "port": side["port"],
        "reg": side["reg"],
        "cascade": side["cascade"],
        "ce2": side["ce2"],
        "ce1": side["ce1"],
        "rst": side["rst"],
        "bus_width": bus_width,
        "reset_mapped": reset_mapped,
        "ff_bus": ff_bus,
        "dsp_cell": dsp_cell,
        "site": dsp_site,
        "clock_pin": clock_pin,
        "ce_pin": ce_pin,
        "reset_pin": reset_pin if reset_mapped else None,
    }


def build_register_absorb_eco_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    """Build fixed Tcl from hash-validated manifests; caller owns hash replay."""
    contract = _validated_register_absorb_contract(facts, authorization, placement)
    dsp = contract["dsp_cell"]
    port = contract["port"]
    reset_mapped = contract["reset_mapped"]
    lines = [
        "proc fdagents_ra_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"required pin missing: $name\"}; return $value}",
        "proc fdagents_ra_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"required cell missing: $name\"}; return $value}",
        "proc fdagents_ra_pin_net {name} {set pin [fdagents_ra_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"required pin net missing: $name\"}; return $value}",
        "proc fdagents_ra_disconnect_pin {name} {set pin [fdagents_ra_one_pin $name]; set net [get_nets -quiet -of_objects $pin]; if {[llength $net] != 1} {error \"disconnect pin net missing: $name\"}; disconnect_net -net $net -objects $pin}",
        "proc fdagents_ra_b64 {value} {return [binary encode base64 -maxlen 0 \"$value\"]}",
        "proc fdagents_ra_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_ra_one_pin $name]]; if {[llength $value] == 0} {error \"required pin net segments missing: $name\"}; return $value}",
        "proc fdagents_ra_same_net {left right} {set left_nets [fdagents_ra_pin_segments $left]; set right_nets [fdagents_ra_pin_segments $right]; foreach net $left_nets {if {[lsearch -exact $right_nets $net] >= 0} {return}}; error \"register-absorb net mismatch: $left vs $right\"}",
        "proc fdagents_ra_gnd_type {name} {set net [fdagents_ra_pin_net $name]; if {[get_property TYPE $net] ne {GND}} {error \"register-absorb pin is not on a GND-type net: $name\"}}",
        "proc fdagents_ra_fdre {name} {set cell [fdagents_ra_one_cell $name]; if {[get_property REF_NAME $cell] ne {FDRE}} {error \"register-absorb cell is not an FDRE: $name\"}; if {\"[get_property INIT $cell]\" ne {1'b0}} {error \"register-absorb FDRE INIT is not zero: $name\"}}",
        "proc fdagents_ra_only_load {qpin sink} {set net [fdagents_ra_pin_net $qpin]; set loads [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == IN}]; if {[llength $loads] != 1} {error \"register-absorb Q fanout is not exactly one pin: $qpin has [llength $loads]\"}; if {[get_property NAME [lindex $loads 0]] ne $sink} {error \"register-absorb Q load is not the expected DSP input: $qpin\"}}",
        "puts {FDAGENTS_REGISTER_ABSORB_ECO_STAGE_00_BEGIN}",
    ]
    # Stage 1: assert the recovered boundary, capture the control net names.
    lines.extend([
        f"set fdagents_ra_clock_net [fdagents_ra_pin_net {_braced(contract['clock_pin'])}]",
        f"set fdagents_ra_ce_net [fdagents_ra_pin_net {_braced(contract['ce_pin'])}]",
    ])
    if reset_mapped:
        lines.append(
            f"set fdagents_ra_reset_net "
            f"[fdagents_ra_pin_net {_braced(contract['reset_pin'])}]"
        )
    lines.extend([
        f"puts \"{REGISTER_ABSORB_CLOCK_NET_MARKER}|"
        "[fdagents_ra_b64 [get_property NAME $fdagents_ra_clock_net]]\"",
        f"puts \"{REGISTER_ABSORB_CE_NET_MARKER}|"
        "[fdagents_ra_b64 [get_property NAME $fdagents_ra_ce_net]]\"",
    ])
    if reset_mapped:
        lines.append(
            f"puts \"{REGISTER_ABSORB_RESET_NET_MARKER}|"
            "[fdagents_ra_b64 [get_property NAME $fdagents_ra_reset_net]]\""
        )
    lines.extend([
        f"if {{[get_property REF_NAME [fdagents_ra_one_cell {_braced(dsp)}]] ne "
        "{DSP48E2}} {error {register-absorb target is not a DSP48E2}}",
        f"if {{[get_property LOC [fdagents_ra_one_cell {_braced(dsp)}]] ne "
        f"{_braced(contract['site'])}}} "
        "{error {register-absorb DSP is not on its recovered site}}",
        f"if {{\"[get_property {contract['reg']} "
        f"[fdagents_ra_one_cell {_braced(dsp)}]]\" ne {{0}}}} "
        f"{{error {{register-absorb {contract['reg']} is not currently 0}}}}",
        f"if {{\"[get_property {contract['cascade']} "
        f"[fdagents_ra_one_cell {_braced(dsp)}]]\" ne {{0}}}} "
        f"{{error {{register-absorb {contract['cascade']} is not currently 0}}}}",
        f"fdagents_ra_same_net {_braced(dsp + '/CLK')} "
        f"{_braced(contract['clock_pin'])}",
        f"fdagents_ra_gnd_type {_braced(dsp + '/' + contract['ce2'])}",
        f"fdagents_ra_gnd_type {_braced(dsp + '/' + contract['ce1'])}",
        f"fdagents_ra_gnd_type {_braced(dsp + '/' + contract['rst'])}",
    ])
    for bit, item in enumerate(contract["ff_bus"]):
        cell = item["cell"]
        dsp_pin = f"{dsp}/{port}[{bit}]"
        lines.append(f"fdagents_ra_fdre {_braced(cell)}")
        # Exact fanout check: the Q net drives exactly this one DSP input bit.
        lines.append(
            f"fdagents_ra_only_load {_braced(item['pin'])} {_braced(dsp_pin)}"
        )
        lines.append(
            f"fdagents_ra_same_net {_braced(cell + '/C')} "
            f"{_braced(contract['clock_pin'])}"
        )
        lines.append(
            f"fdagents_ra_same_net {_braced(cell + '/CE')} "
            f"{_braced(contract['ce_pin'])}"
        )
        if reset_mapped:
            lines.append(
                f"fdagents_ra_same_net {_braced(cell + '/R')} "
                f"{_braced(contract['reset_pin'])}"
            )
        else:
            lines.append(f"fdagents_ra_gnd_type {_braced(cell + '/R')}")
    lines.append("puts {FDAGENTS_REGISTER_ABSORB_ECO_STAGE_01_BOUNDARY_ASSERTED}")

    # Stage 2: move every FF D-pin driver net onto its DSP input bit, then
    # remove exactly the absorbed FDRE inventory and the dead Q nets.
    lines.append("set fdagents_ra_moved [list]")
    for bit, item in enumerate(contract["ff_bus"]):
        cell = item["cell"]
        tag = f"{bit:03d}"
        src = f"fdagents_ra_src_{tag}"
        old = f"fdagents_ra_old_{tag}"
        dsp_pin = f"{dsp}/{port}[{bit}]"
        lines.extend([
            f"set {src} [fdagents_ra_pin_net {_braced(cell + '/D')}]",
            f"set {old} [fdagents_ra_pin_net {_braced(item['pin'])}]",
            f"puts \"{REGISTER_ABSORB_BIT_NET_MARKER}|{tag}|"
            f"[fdagents_ra_b64 [get_property NAME ${src}]]\"",
            f"disconnect_net -net ${old} -objects {_pins([dsp_pin])}",
            f"disconnect_net -net ${src} -objects {_pins([cell + '/D'])}",
            f"disconnect_net -net ${old} -objects {_pins([item['pin']])}",
            f"connect_net -hierarchical -net ${src} -objects {_pins([dsp_pin])}",
            f"lappend fdagents_ra_moved ${src}",
        ])
    lines.append(f"puts {{{REGISTER_ABSORB_CAPTURE_DONE_MARKER}}}")
    for item in contract["ff_bus"]:
        lines.append(f"remove_cell [fdagents_ra_one_cell {_braced(item['cell'])}]")
    first_ff = contract["ff_bus"][0]["cell"]
    lines.append(
        f"if {{[llength [get_cells -quiet [list {_braced(first_ff)}]]] != 0}} "
        "{error {register-absorb FDRE removal failed}}"
    )
    for bit in range(contract["bus_width"]):
        lines.append(f"remove_net $fdagents_ra_old_{bit:03d}")
    lines.append("puts {FDAGENTS_REGISTER_ABSORB_ECO_STAGE_02_REWIRED}")

    # Stage 3: enable the DSP input register pair on the absorbed side.
    lines.extend([
        f"set_property {contract['reg']} 1 [get_cells {_braced(dsp)}]",
        f"set_property {contract['cascade']} 1 [get_cells {_braced(dsp)}]",
        "puts {FDAGENTS_REGISTER_ABSORB_ECO_STAGE_03_REGISTERS_SET}",
    ])

    # Stage 4: move the boundary CE (and mapped reset) onto the DSP controls.
    lines.extend([
        f"fdagents_ra_disconnect_pin {_braced(dsp + '/' + contract['ce2'])}",
        "connect_net -hierarchical -net $fdagents_ra_ce_net -objects "
        + _pins([dsp + "/" + contract["ce2"]]),
    ])
    if reset_mapped:
        lines.extend([
            f"fdagents_ra_disconnect_pin {_braced(dsp + '/' + contract['rst'])}",
            "connect_net -hierarchical -net $fdagents_ra_reset_net -objects "
            + _pins([dsp + "/" + contract["rst"]]),
        ])
    lines.append("puts {FDAGENTS_REGISTER_ABSORB_ECO_STAGE_04_CONTROL_MAPPED}")

    # Stage 5: scoped route of the touched nets only, then fail-closed check.
    lines.extend([
        "set fdagents_ra_touched [lsort -unique [concat [get_nets -quiet "
        "-of_objects [get_pins -quiet -of_objects "
        f"[fdagents_ra_one_cell {_braced(dsp)}]]] $fdagents_ra_moved]]",
        "if {[llength $fdagents_ra_touched] == 0} "
        "{error {register-absorb ECO touched no nets}}",
        "route_design -nets $fdagents_ra_touched",
        "set fdagents_ra_unrouted [filter -quiet $fdagents_ra_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_ra_unrouted] != 0} {error \"register-absorb ECO left [llength $fdagents_ra_unrouted] unrouted nets\"}",
        "puts {FDAGENTS_REGISTER_ABSORB_ECO_STAGE_05_ROUTED}",
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_REGISTER_ABSORB_ECO_DONE}",
    ])
    return "\n".join(lines)


def parse_register_absorb_control_nets(output: str) -> dict[str, Any]:
    """Decode the exact stage-1/2 net-name markers; anything else fails closed."""

    def _decode(payload: str, field: str) -> str:
        value = base64.b64decode(payload.encode("ascii"), validate=True).decode(
            "utf-8"
        )
        return _name(value, field)

    clock_net = None
    ce_net = None
    reset_net = None
    bit_nets: dict[int, str] = {}
    done = False
    for raw in str(output).splitlines():
        line = raw.strip()
        if line.startswith(REGISTER_ABSORB_CLOCK_NET_MARKER + "|"):
            if clock_net is not None:
                raise ValueError("register-absorb clock net marker repeated")
            clock_net = _decode(line.split("|", 1)[1], "captured clock net")
        elif line.startswith(REGISTER_ABSORB_CE_NET_MARKER + "|"):
            if ce_net is not None:
                raise ValueError("register-absorb CE net marker repeated")
            ce_net = _decode(line.split("|", 1)[1], "captured CE net")
        elif line.startswith(REGISTER_ABSORB_RESET_NET_MARKER + "|"):
            if reset_net is not None:
                raise ValueError("register-absorb reset net marker repeated")
            reset_net = _decode(line.split("|", 1)[1], "captured reset net")
        elif line.startswith(REGISTER_ABSORB_BIT_NET_MARKER + "|"):
            parts = line.split("|")
            if len(parts) != 3 or _REGISTER_ABSORB_BIT_INDEX_RE.fullmatch(
                parts[1]
            ) is None:
                raise ValueError("register-absorb bit net marker is malformed")
            index = int(parts[1])
            if index in bit_nets:
                raise ValueError("register-absorb bit net marker repeated")
            bit_nets[index] = _decode(parts[2], f"captured bit net {index}")
        elif line == REGISTER_ABSORB_CAPTURE_DONE_MARKER:
            done = True
    if clock_net is None or ce_net is None or not bit_nets or not done:
        raise ValueError("register-absorb control net capture did not complete")
    if set(bit_nets) != set(range(len(bit_nets))):
        raise ValueError("register-absorb bit net capture is not contiguous")
    return {
        "clock_net": clock_net,
        "ce_net": ce_net,
        "reset_net": reset_net,
        "bit_nets": [bit_nets[index] for index in range(len(bit_nets))],
    }


def build_register_absorb_postcheck_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    control_nets: dict[str, Any],
) -> str:
    """Reopen-time assertions for the absorbed DSP input register topology.

    ``control_nets`` carries the stage-1/2 net names captured by the ECO run
    (the absorbed FDRE bus no longer exists in the candidate, so the boundary
    control pins cannot be replayed directly).
    """
    contract = _validated_register_absorb_contract(facts, authorization, placement)
    if not isinstance(control_nets, dict) or set(control_nets) != {
        "clock_net", "ce_net", "reset_net", "bit_nets",
    }:
        raise ValueError(
            "register-absorb captured control nets have an unsupported schema"
        )
    clock_net = _name(control_nets.get("clock_net"), "control_nets.clock_net")
    ce_net = _name(control_nets.get("ce_net"), "control_nets.ce_net")
    reset_net = control_nets.get("reset_net")
    if contract["reset_mapped"]:
        reset_net = _name(reset_net, "control_nets.reset_net")
    elif reset_net is not None:
        raise ValueError("control_nets.reset_net must be None when no reset is mapped")
    raw_bit_nets = control_nets.get("bit_nets")
    if not isinstance(raw_bit_nets, list) or len(raw_bit_nets) != (
        contract["bus_width"]
    ):
        raise ValueError("control_nets.bit_nets does not cover the absorbed bus")
    bit_nets = [
        _name(item, f"control_nets.bit_nets[{index}]")
        for index, item in enumerate(raw_bit_nets)
    ]
    dsp = contract["dsp_cell"]
    port = contract["port"]
    lines = [
        "proc fdagents_rac_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck cell missing: $name\"}; return $value}",
        "proc fdagents_rac_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck pin missing: $name\"}; return $value}",
        "proc fdagents_rac_pin_net {name} {set pin [fdagents_rac_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"postcheck pin net missing: $name\"}; return $value}",
        "proc fdagents_rac_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_rac_one_pin $name]]; if {[llength $value] == 0} {error \"postcheck pin net segments missing: $name\"}; return $value}",
        "proc fdagents_rac_net_name {pin expected} {foreach net [fdagents_rac_pin_segments $pin] {if {[get_property NAME $net] eq $expected} {return}}; error \"postcheck net name mismatch: $pin expected=$expected\"}",
        "proc fdagents_rac_prop {cell name expected} {set actual [get_property $name [fdagents_rac_one_cell $cell]]; if {\"$actual\" ne \"$expected\"} {error \"postcheck property mismatch: $cell $name expected=$expected actual=$actual\"}}",
        "proc fdagents_rac_gnd_type {name} {set net [fdagents_rac_pin_net $name]; if {[get_property TYPE $net] ne {GND}} {error \"postcheck pin is not on a GND-type net: $name\"}}",
    ]
    # Exact inventory: every absorbed FDRE is gone, no reserved-namespace
    # cells were created (this template never creates any).
    for item in contract["ff_bus"]:
        lines.append(
            f"if {{[llength [get_cells -quiet [list {_braced(item['cell'])}]]] != 0}} "
            "{error {postcheck absorbed FDRE still exists}}"
        )
    lines.extend([
        f"if {{[llength [get_cells -quiet -hierarchical "
        f"{_braced(_REGISTER_ABSORB_NAMESPACE_PREFIX + '*')}]] != 0}} "
        "{error {postcheck namespace inventory mismatch}}",
        "puts {FDAGENTS_REGISTER_ABSORB_INVENTORY_OK}",
        f"fdagents_rac_prop {_braced(dsp)} REF_NAME {{DSP48E2}}",
        f"fdagents_rac_prop {_braced(dsp)} LOC {_braced(contract['site'])}",
        f"fdagents_rac_prop {_braced(dsp)} {contract['reg']} {{1}}",
        f"fdagents_rac_prop {_braced(dsp)} {contract['cascade']} {{1}}",
        f"fdagents_rac_net_name {_braced(dsp + '/CLK')} {_braced(clock_net)}",
        f"fdagents_rac_net_name {_braced(dsp + '/' + contract['ce2'])} "
        f"{_braced(ce_net)}",
        f"fdagents_rac_gnd_type {_braced(dsp + '/' + contract['ce1'])}",
    ])
    if contract["reset_mapped"]:
        lines.append(
            f"fdagents_rac_net_name {_braced(dsp + '/' + contract['rst'])} "
            f"{_braced(reset_net)}"
        )
    else:
        lines.append(f"fdagents_rac_gnd_type {_braced(dsp + '/' + contract['rst'])}")
    for bit, net in enumerate(bit_nets):
        lines.append(
            f"fdagents_rac_net_name {_braced(f'{dsp}/{port}[{bit}]')} {_braced(net)}"
        )
    lines.extend([
        f"set fdagents_rac_touched [lsort -unique [get_nets -quiet -of_objects "
        f"[get_pins -quiet -of_objects [fdagents_rac_one_cell {_braced(dsp)}]]]]",
        "set fdagents_rac_unrouted [filter -quiet $fdagents_rac_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_rac_unrouted] != 0} {error {postcheck found unrouted touched nets}}",
        "puts {FDAGENTS_REGISTER_ABSORB_ROUTED_OK}",
    ])
    lines.extend(_port_signature_lines("fdagents_rac_b64"))
    lines.append("puts {FDAGENTS_REGISTER_ABSORB_POSTCHECK_DONE}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline-rebalance template (DSP48E2_MREG_REBALANCE_V1).
#
# The smallest true Leiserson-Saxe move applied inside one placed DSP48E2:
# a register stage trades between the A2/B2 input registers and the MREG
# multiplier register. The ECO is property-plus-control-pin only: no cells
# are created or removed and the DSP keeps its placement. Forward direction
# sets AREG=0,BREG=0,ACASCREG=0,BCASCREG=0,MREG=1 and moves the shared CE
# net from CEA2+CEB2 onto CEM (vacated pins tie to the captured GND net);
# when a synchronous zero reset is mapped the shared reset net moves from
# RSTA+RSTB onto RSTM likewise. Backward is the exact inverse. Stage 1
# captures the clock/CE/reset/GND net names as base64 markers; the handler
# replays those captured names into the postcheck builder for reopen-time
# name-equality assertions.
# ---------------------------------------------------------------------------

_PIPELINE_REBALANCE_PLACEMENT_MANIFEST_KIND = "pipeline_rebalance_placement_manifest"
_PIPELINE_REBALANCE_PLACEMENT_MANIFEST_VERSION = 1
_PIPELINE_REBALANCE_FACTS_KIND = "pipeline_rebalance_contract_facts"
_PIPELINE_REBALANCE_PLAN_TEMPLATE = "DSP48E2_MREG_REBALANCE_V1"
_PIPELINE_REBALANCE_CANDIDATE_ID_RE = re.compile(r"^rebalance:[0-9a-f]{20}$")
# Reserved cell/net-name namespace: this template never creates cells or
# nets, so the postcheck asserts the reserved prefix stays empty.
_PIPELINE_REBALANCE_NAMESPACE_PREFIX = "fdagents_rebalance_"
# Per-direction contract: authorized set_registers moves, the full applied
# property inventory (ACASCREG/BCASCREG track AREG/BREG even though they are
# not part of set_registers; the applied order keeps every intermediate
# cascade combination legal), the required register preconditions (exact
# inverse of the target), and the control pins the shared CE/reset nets move
# between. The first source pin of each group is the contracted boundary pin.
_PIPELINE_REBALANCE_DIRECTION_INFO = {
    "forward": {
        "set_registers": (("AREG", 0), ("BREG", 0), ("MREG", 1)),
        "source_registers": (
            ("AREG", 1), ("BREG", 1), ("ACASCREG", 1), ("BCASCREG", 1),
            ("MREG", 0),
        ),
        "target_registers": (
            ("ACASCREG", 0), ("AREG", 0), ("BCASCREG", 0), ("BREG", 0),
            ("MREG", 1),
        ),
        "ce_source_pins": ("CEA2", "CEB2"),
        "ce_target_pins": ("CEM",),
        "rst_source_pins": ("RSTA", "RSTB"),
        "rst_target_pins": ("RSTM",),
    },
    "backward": {
        "set_registers": (("AREG", 1), ("BREG", 1), ("MREG", 0)),
        "source_registers": (
            ("AREG", 0), ("BREG", 0), ("ACASCREG", 0), ("BCASCREG", 0),
            ("MREG", 1),
        ),
        "target_registers": (
            ("AREG", 1), ("ACASCREG", 1), ("BREG", 1), ("BCASCREG", 1),
            ("MREG", 0),
        ),
        "ce_source_pins": ("CEM",),
        "ce_target_pins": ("CEA2", "CEB2"),
        "rst_source_pins": ("RSTM",),
        "rst_target_pins": ("RSTA", "RSTB"),
    },
}

PIPELINE_REBALANCE_CLOCK_NET_MARKER = "FDAGENTS_PIPELINE_REBALANCE_CLOCK_NET"
PIPELINE_REBALANCE_CE_NET_MARKER = "FDAGENTS_PIPELINE_REBALANCE_CE_NET"
PIPELINE_REBALANCE_RESET_NET_MARKER = "FDAGENTS_PIPELINE_REBALANCE_RESET_NET"
PIPELINE_REBALANCE_GND_NET_MARKER = "FDAGENTS_PIPELINE_REBALANCE_GND_NET"
PIPELINE_REBALANCE_CAPTURE_DONE_MARKER = "FDAGENTS_PIPELINE_REBALANCE_CAPTURE_DONE"
PIPELINE_REBALANCE_CANDIDATE_REOPENED_MARKER = (
    "FDAGENTS_PIPELINE_REBALANCE_CANDIDATE_REOPENED"
)


def _validated_pipeline_rebalance_contract(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Cross-check every pipeline-rebalance manifest in Python before any Tcl."""
    if not isinstance(facts, dict) or facts.get("kind") != (
        _PIPELINE_REBALANCE_FACTS_KIND
    ):
        raise ValueError("pipeline-rebalance facts manifest kind is invalid")
    spec = facts.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("pipeline-rebalance facts spec is missing")
    if not isinstance(authorization, dict) or authorization.get("kind") != (
        "operator_rewrite_authorization"
    ):
        raise ValueError("pipeline-rebalance authorization kind is invalid")
    if authorization.get("rewrite_kind") != "dsp_pipeline_rebalance":
        raise ValueError("pipeline-rebalance authorization rewrite kind is invalid")
    if authorization.get("authority") != "pre_mutation_only":
        raise ValueError("pipeline-rebalance authorization authority is invalid")
    certificate_id = authorization.get("certificate_id")
    _identity(certificate_id, "authorization.certificate_id")
    if authorization.get("spec") != spec:
        raise ValueError("pipeline-rebalance authorization spec differs from the facts")
    source = authorization.get("source")
    if not isinstance(source, dict) or set(source) != {"dcp_sha256", "region_sha256"}:
        raise ValueError(
            "pipeline-rebalance authorization source identity is incomplete"
        )
    _identity(source.get("dcp_sha256"), "authorization.source.dcp_sha256")
    _identity(source.get("region_sha256"), "authorization.source.region_sha256")
    if facts.get("region_sha256") != source["region_sha256"]:
        raise ValueError("pipeline-rebalance facts region differs from its authorization")
    if "design_sha256" in facts and facts.get("design_sha256") != source["dcp_sha256"]:
        raise ValueError("pipeline-rebalance facts design differs from its authorization")

    plan = authorization.get("plan")
    if not isinstance(plan, dict) or plan.get("template") != (
        _PIPELINE_REBALANCE_PLAN_TEMPLATE
    ):
        raise ValueError("pipeline-rebalance plan template is unsupported")
    direction = spec.get("direction")
    if direction not in _PIPELINE_REBALANCE_DIRECTION_INFO:
        raise ValueError('spec.direction must be "forward" or "backward"')
    info = _PIPELINE_REBALANCE_DIRECTION_INFO[direction]
    a_width = _int_field(spec.get("a_width"), "spec.a_width", 1, DSP_A_WIDTH)
    b_width = _int_field(spec.get("b_width"), "spec.b_width", 1, DSP_B_WIDTH)
    if spec.get("ce_present") is not True:
        raise ValueError("spec.ce_present must be True for this template")
    if spec.get("init_zero") is not True:
        raise ValueError("spec.init_zero must be True for this template")
    reset_kind = spec.get("reset_kind")
    if reset_kind not in ("none", "sync_reset_zero"):
        raise ValueError('spec.reset_kind must be "none" or "sync_reset_zero"')
    if plan.get("direction") != direction:
        raise ValueError("plan direction differs from the recovered contract")
    moves = plan.get("set_registers")
    expected_moves = info["set_registers"]
    if not isinstance(moves, list) or len(moves) != len(expected_moves):
        raise ValueError(
            "plan.set_registers must be the exact register moves for its direction"
        )
    for move, expected in zip(moves, expected_moves):
        if (
            not isinstance(move, (list, tuple))
            or len(move) != 2
            or move[0] != expected[0]
            or isinstance(move[1], bool)
            or move[1] != expected[1]
        ):
            raise ValueError(
                "plan.set_registers must be the exact register moves for "
                "its direction"
            )
    if plan.get("ce_mapped") is not True:
        raise ValueError("plan.ce_mapped must be True for this template")
    reset_mapped = plan.get("reset_mapped")
    if not isinstance(reset_mapped, bool):
        raise ValueError("plan.reset_mapped must be a boolean")
    if reset_mapped != (reset_kind == "sync_reset_zero"):
        raise ValueError("plan.reset_mapped disagrees with the recovered reset kind")

    boundary = facts.get("boundary")
    if not isinstance(boundary, dict) or set(boundary) != {
        "dsp_cell", "dsp_site", "clock_pin", "ce_pin", "reset_pin",
        "a_width", "b_width",
    }:
        raise ValueError("pipeline-rebalance boundary manifest is incomplete")
    dsp_cell = _name(boundary.get("dsp_cell"), "boundary.dsp_cell")
    dsp_site = boundary.get("dsp_site")
    if not isinstance(dsp_site, str) or _DSP_SITE_RE.fullmatch(dsp_site) is None:
        raise ValueError("boundary.dsp_site is invalid")
    clock_pin = _name(boundary.get("clock_pin"), "boundary.clock_pin")
    if clock_pin != dsp_cell + "/CLK":
        raise ValueError("boundary.clock_pin must be the DSP CLK pin")
    ce_pin = _name(boundary.get("ce_pin"), "boundary.ce_pin")
    if ce_pin != f"{dsp_cell}/{info['ce_source_pins'][0]}":
        raise ValueError(
            "boundary.ce_pin must be the active CE source pin for its direction"
        )
    for field, width in (("a_width", a_width), ("b_width", b_width)):
        value = boundary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value != width:
            raise ValueError(f"boundary.{field} differs from the recovered spec")
    reset_pin = boundary.get("reset_pin")
    if reset_mapped:
        reset_pin = _name(reset_pin, "boundary.reset_pin")
        if reset_pin != f"{dsp_cell}/{info['rst_source_pins'][0]}":
            raise ValueError(
                "boundary.reset_pin must be the active reset source pin for "
                "its direction"
            )
    elif reset_pin is not None:
        raise ValueError("boundary.reset_pin must be None when no reset is mapped")

    if not isinstance(placement, dict) or placement.get("kind") != (
        _PIPELINE_REBALANCE_PLACEMENT_MANIFEST_KIND
    ) or placement.get("version") != _PIPELINE_REBALANCE_PLACEMENT_MANIFEST_VERSION:
        raise ValueError("pipeline-rebalance placement manifest kind is invalid")
    if placement.get("authority") != "pre_emission_only":
        raise ValueError("pipeline-rebalance placement authority is invalid")
    if placement.get("source") != source:
        raise ValueError(
            "pipeline-rebalance placement source differs from its authorization"
        )
    if placement.get("authorization_id") != certificate_id:
        raise ValueError("pipeline-rebalance placement authorization identity mismatch")
    _identity(placement.get("manifest_sha256"), "placement.manifest_sha256")
    placement_candidate = placement.get("candidate_id")
    if (
        not isinstance(placement_candidate, str)
        or _PIPELINE_REBALANCE_CANDIDATE_ID_RE.fullmatch(placement_candidate) is None
    ):
        raise ValueError("pipeline-rebalance placement candidate identity is invalid")
    if "candidate_id" in facts and facts.get("candidate_id") != placement_candidate:
        raise ValueError("pipeline-rebalance placement candidate differs from the facts")
    records = placement.get("placements")
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("pipeline-rebalance placement must provide exactly one DSP slot")
    record = records[0]
    if not isinstance(record, dict) or set(record) != {"site", "bel"}:
        raise ValueError("pipeline-rebalance placement slot schema is invalid")
    site = record.get("site")
    if not isinstance(site, str) or _DSP_SITE_RE.fullmatch(site) is None:
        raise ValueError("pipeline-rebalance placement site is invalid")
    if record.get("bel") != "DSP48E2":
        raise ValueError("pipeline-rebalance placement BEL is invalid")
    if site != dsp_site:
        raise ValueError(
            "pipeline-rebalance placement site differs from the recovered DSP site"
        )

    return {
        "direction": direction,
        "dsp_cell": dsp_cell,
        "site": dsp_site,
        "clock_pin": clock_pin,
        "ce_pin": ce_pin,
        "reset_pin": reset_pin if reset_mapped else None,
        "reset_mapped": reset_mapped,
        "a_width": a_width,
        "b_width": b_width,
        "source_registers": info["source_registers"],
        "target_registers": info["target_registers"],
        "ce_source_pins": info["ce_source_pins"],
        "ce_target_pins": info["ce_target_pins"],
        "rst_source_pins": info["rst_source_pins"],
        "rst_target_pins": info["rst_target_pins"],
    }


def build_pipeline_rebalance_eco_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    """Build fixed Tcl from hash-validated manifests; caller owns hash replay."""
    contract = _validated_pipeline_rebalance_contract(facts, authorization, placement)
    dsp = contract["dsp_cell"]
    reset_mapped = contract["reset_mapped"]
    ce_sources = [f"{dsp}/{pin}" for pin in contract["ce_source_pins"]]
    ce_targets = [f"{dsp}/{pin}" for pin in contract["ce_target_pins"]]
    rst_sources = [f"{dsp}/{pin}" for pin in contract["rst_source_pins"]]
    rst_targets = [f"{dsp}/{pin}" for pin in contract["rst_target_pins"]]
    lines = [
        "proc fdagents_pr_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"required pin missing: $name\"}; return $value}",
        "proc fdagents_pr_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"required cell missing: $name\"}; return $value}",
        "proc fdagents_pr_pin_net {name} {set pin [fdagents_pr_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"required pin net missing: $name\"}; return $value}",
        "proc fdagents_pr_disconnect_pin {name} {set pin [fdagents_pr_one_pin $name]; set net [get_nets -quiet -of_objects $pin]; if {[llength $net] != 1} {error \"disconnect pin net missing: $name\"}; disconnect_net -net $net -objects $pin}",
        "proc fdagents_pr_b64 {value} {return [binary encode base64 -maxlen 0 \"$value\"]}",
        "proc fdagents_pr_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_pr_one_pin $name]]; if {[llength $value] == 0} {error \"required pin net segments missing: $name\"}; return $value}",
        "proc fdagents_pr_same_net {left right} {set left_nets [fdagents_pr_pin_segments $left]; set right_nets [fdagents_pr_pin_segments $right]; foreach net $left_nets {if {[lsearch -exact $right_nets $net] >= 0} {return}}; error \"pipeline-rebalance net mismatch: $left vs $right\"}",
        "proc fdagents_pr_gnd_type {name} {set net [fdagents_pr_pin_net $name]; if {[get_property TYPE $net] ne {GND}} {error \"pipeline-rebalance pin is not on a GND-type net: $name\"}; return $net}",
        "puts {FDAGENTS_PIPELINE_REBALANCE_ECO_STAGE_00_BEGIN}",
    ]
    # Stage 1: assert the recovered DSP state, capture the control net names.
    lines.extend([
        f"if {{[get_property REF_NAME [fdagents_pr_one_cell {_braced(dsp)}]] ne "
        "{DSP48E2}} {error {pipeline-rebalance target is not a DSP48E2}}",
        f"if {{[get_property LOC [fdagents_pr_one_cell {_braced(dsp)}]] ne "
        f"{_braced(contract['site'])}}} "
        "{error {pipeline-rebalance DSP is not on its recovered site}}",
    ])
    for name, value in contract["source_registers"]:
        lines.append(
            f"if {{\"[get_property {name} "
            f"[fdagents_pr_one_cell {_braced(dsp)}]]\" ne {{{value}}}}} "
            f"{{error {{pipeline-rebalance {name} is not currently {value}}}}}"
        )
    lines.extend([
        f"set fdagents_pr_clock_net [fdagents_pr_pin_net {_braced(contract['clock_pin'])}]",
        f"puts \"{PIPELINE_REBALANCE_CLOCK_NET_MARKER}|"
        "[fdagents_pr_b64 [get_property NAME $fdagents_pr_clock_net]]\"",
        f"set fdagents_pr_ce_net [fdagents_pr_pin_net {_braced(contract['ce_pin'])}]",
    ])
    # Every CE source pin sits on the ONE shared boundary CE net.
    for pin in ce_sources[1:]:
        lines.append(
            f"fdagents_pr_same_net {_braced(contract['ce_pin'])} {_braced(pin)}"
        )
    lines.append(
        f"puts \"{PIPELINE_REBALANCE_CE_NET_MARKER}|"
        "[fdagents_pr_b64 [get_property NAME $fdagents_pr_ce_net]]\""
    )
    # The target CE pins currently idle on GND-type nets; the first one
    # donates the GND net reused to tie the vacated source pins.
    lines.extend([
        f"set fdagents_pr_gnd_net [fdagents_pr_gnd_type {_braced(ce_targets[0])}]",
        f"puts \"{PIPELINE_REBALANCE_GND_NET_MARKER}|"
        "[fdagents_pr_b64 [get_property NAME $fdagents_pr_gnd_net]]\"",
    ])
    for pin in ce_targets[1:]:
        lines.append(f"fdagents_pr_gnd_type {_braced(pin)}")
    if reset_mapped:
        lines.append(
            f"set fdagents_pr_reset_net "
            f"[fdagents_pr_pin_net {_braced(contract['reset_pin'])}]"
        )
        for pin in rst_sources[1:]:
            lines.append(
                f"fdagents_pr_same_net {_braced(contract['reset_pin'])} "
                f"{_braced(pin)}"
            )
        lines.append(
            f"puts \"{PIPELINE_REBALANCE_RESET_NET_MARKER}|"
            "[fdagents_pr_b64 [get_property NAME $fdagents_pr_reset_net]]\""
        )
        for pin in rst_targets:
            lines.append(f"fdagents_pr_gnd_type {_braced(pin)}")
    else:
        for pin in ("RSTA", "RSTB", "RSTM"):
            lines.append(f"fdagents_pr_gnd_type {_braced(dsp + '/' + pin)}")
    lines.extend([
        f"puts {{{PIPELINE_REBALANCE_CAPTURE_DONE_MARKER}}}",
        "puts {FDAGENTS_PIPELINE_REBALANCE_ECO_STAGE_01_BOUNDARY_ASSERTED}",
    ])

    # Stage 2: apply the full register-property inventory for the direction
    # (cascade registers track their input registers in a legal order).
    for name, value in contract["target_registers"]:
        lines.append(f"set_property {name} {value} [get_cells {_braced(dsp)}]")
    lines.append("puts {FDAGENTS_PIPELINE_REBALANCE_ECO_STAGE_02_PROPERTIES_SET}")

    # Stage 3: move the shared CE (and mapped reset) net onto the target
    # pins; the vacated source pins tie to the captured GND net.
    for pin in ce_targets:
        lines.append(f"fdagents_pr_disconnect_pin {_braced(pin)}")
    lines.extend([
        f"disconnect_net -net $fdagents_pr_ce_net -objects {_pins(ce_sources)}",
        f"connect_net -hierarchical -net $fdagents_pr_ce_net -objects {_pins(ce_targets)}",
        f"connect_net -hierarchical -net $fdagents_pr_gnd_net -objects {_pins(ce_sources)}",
    ])
    if reset_mapped:
        for pin in rst_targets:
            lines.append(f"fdagents_pr_disconnect_pin {_braced(pin)}")
        lines.extend([
            f"disconnect_net -net $fdagents_pr_reset_net -objects {_pins(rst_sources)}",
            f"connect_net -hierarchical -net $fdagents_pr_reset_net -objects {_pins(rst_targets)}",
            f"connect_net -hierarchical -net $fdagents_pr_gnd_net -objects {_pins(rst_sources)}",
        ])
    lines.append("puts {FDAGENTS_PIPELINE_REBALANCE_ECO_STAGE_03_CONTROL_MAPPED}")

    # Stage 4: scoped route of the touched control nets only, fail-closed.
    control_pins = [
        f"{dsp}/{pin}" for pin in ("CEA2", "CEB2", "CEM", "RSTA", "RSTB", "RSTM")
    ]
    touched_extra = "$fdagents_pr_ce_net $fdagents_pr_gnd_net"
    if reset_mapped:
        touched_extra += " $fdagents_pr_reset_net"
    lines.extend([
        "set fdagents_pr_touched [lsort -unique [concat "
        f"[get_nets -quiet -of_objects {_pins(control_pins)}] {touched_extra}]]",
        "if {[llength $fdagents_pr_touched] == 0} "
        "{error {pipeline-rebalance ECO touched no nets}}",
        "route_design -nets $fdagents_pr_touched",
        "set fdagents_pr_unrouted [filter -quiet $fdagents_pr_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_pr_unrouted] != 0} {error \"pipeline-rebalance ECO left [llength $fdagents_pr_unrouted] unrouted nets\"}",
        "puts {FDAGENTS_PIPELINE_REBALANCE_ECO_STAGE_04_ROUTED}",
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_PIPELINE_REBALANCE_ECO_DONE}",
    ])
    return "\n".join(lines)


def parse_pipeline_rebalance_control_nets(output: str) -> dict[str, Any]:
    """Decode the exact stage-1 net-name markers; anything else fails closed."""

    def _decode(payload: str, field: str) -> str:
        value = base64.b64decode(payload.encode("ascii"), validate=True).decode(
            "utf-8"
        )
        return _name(value, field)

    clock_net = None
    ce_net = None
    reset_net = None
    gnd_net = None
    done = False
    for raw in str(output).splitlines():
        line = raw.strip()
        if line.startswith(PIPELINE_REBALANCE_CLOCK_NET_MARKER + "|"):
            if clock_net is not None:
                raise ValueError("pipeline-rebalance clock net marker repeated")
            clock_net = _decode(line.split("|", 1)[1], "captured clock net")
        elif line.startswith(PIPELINE_REBALANCE_CE_NET_MARKER + "|"):
            if ce_net is not None:
                raise ValueError("pipeline-rebalance CE net marker repeated")
            ce_net = _decode(line.split("|", 1)[1], "captured CE net")
        elif line.startswith(PIPELINE_REBALANCE_RESET_NET_MARKER + "|"):
            if reset_net is not None:
                raise ValueError("pipeline-rebalance reset net marker repeated")
            reset_net = _decode(line.split("|", 1)[1], "captured reset net")
        elif line.startswith(PIPELINE_REBALANCE_GND_NET_MARKER + "|"):
            if gnd_net is not None:
                raise ValueError("pipeline-rebalance GND net marker repeated")
            gnd_net = _decode(line.split("|", 1)[1], "captured GND net")
        elif line == PIPELINE_REBALANCE_CAPTURE_DONE_MARKER:
            done = True
    if clock_net is None or ce_net is None or gnd_net is None or not done:
        raise ValueError("pipeline-rebalance control net capture did not complete")
    return {
        "clock_net": clock_net,
        "ce_net": ce_net,
        "reset_net": reset_net,
        "gnd_net": gnd_net,
    }


def build_pipeline_rebalance_postcheck_tcl(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    placement: dict[str, Any],
    *,
    candidate_dcp: str,
    control_nets: dict[str, Any],
) -> str:
    """Reopen-time assertions for the rebalanced DSP register topology.

    ``control_nets`` carries the stage-1 net names captured by the ECO run.
    The emitted Tcl reopens the candidate checkpoint itself (first line) so
    the whole postcheck is a single self-contained replay transaction.
    """
    contract = _validated_pipeline_rebalance_contract(facts, authorization, placement)
    if not isinstance(control_nets, dict) or set(control_nets) != {
        "clock_net", "ce_net", "reset_net", "gnd_net",
    }:
        raise ValueError(
            "pipeline-rebalance captured control nets have an unsupported schema"
        )
    clock_net = _name(control_nets.get("clock_net"), "control_nets.clock_net")
    ce_net = _name(control_nets.get("ce_net"), "control_nets.ce_net")
    _name(control_nets.get("gnd_net"), "control_nets.gnd_net")
    reset_net = control_nets.get("reset_net")
    if contract["reset_mapped"]:
        reset_net = _name(reset_net, "control_nets.reset_net")
    elif reset_net is not None:
        raise ValueError("control_nets.reset_net must be None when no reset is mapped")
    dsp = contract["dsp_cell"]
    lines = [
        f"open_checkpoint {_braced(candidate_dcp)}",
        f"puts {{{PIPELINE_REBALANCE_CANDIDATE_REOPENED_MARKER}}}",
        "proc fdagents_prc_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck cell missing: $name\"}; return $value}",
        "proc fdagents_prc_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck pin missing: $name\"}; return $value}",
        "proc fdagents_prc_pin_net {name} {set pin [fdagents_prc_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"postcheck pin net missing: $name\"}; return $value}",
        "proc fdagents_prc_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_prc_one_pin $name]]; if {[llength $value] == 0} {error \"postcheck pin net segments missing: $name\"}; return $value}",
        "proc fdagents_prc_net_name {pin expected} {foreach net [fdagents_prc_pin_segments $pin] {if {[get_property NAME $net] eq $expected} {return}}; error \"postcheck net name mismatch: $pin expected=$expected\"}",
        "proc fdagents_prc_prop {cell name expected} {set actual [get_property $name [fdagents_prc_one_cell $cell]]; if {\"$actual\" ne \"$expected\"} {error \"postcheck property mismatch: $cell $name expected=$expected actual=$actual\"}}",
        "proc fdagents_prc_gnd_type {name} {set net [fdagents_prc_pin_net $name]; if {[get_property TYPE $net] ne {GND}} {error \"postcheck pin is not on a GND-type net: $name\"}}",
    ]
    # Exact inventory: this template creates nothing, so the reserved
    # namespace stays empty and the contracted DSP cell still exists.
    lines.extend([
        f"if {{[llength [get_cells -quiet -hierarchical "
        f"{_braced(_PIPELINE_REBALANCE_NAMESPACE_PREFIX + '*')}]] != 0}} "
        "{error {postcheck namespace inventory mismatch}}",
        f"if {{[llength [get_nets -quiet -hierarchical "
        f"{_braced(_PIPELINE_REBALANCE_NAMESPACE_PREFIX + '*')}]] != 0}} "
        "{error {postcheck namespace inventory mismatch}}",
        f"fdagents_prc_prop {_braced(dsp)} REF_NAME {{DSP48E2}}",
        f"fdagents_prc_prop {_braced(dsp)} LOC {_braced(contract['site'])}",
        "puts {FDAGENTS_PIPELINE_REBALANCE_INVENTORY_OK}",
    ])
    for name, value in contract["target_registers"]:
        lines.append(f"fdagents_prc_prop {_braced(dsp)} {name} {_braced(str(value))}")
    lines.append(
        f"fdagents_prc_net_name {_braced(dsp + '/CLK')} {_braced(clock_net)}"
    )
    for pin in contract["ce_target_pins"]:
        lines.append(
            f"fdagents_prc_net_name {_braced(dsp + '/' + pin)} {_braced(ce_net)}"
        )
    for pin in contract["ce_source_pins"]:
        lines.append(f"fdagents_prc_gnd_type {_braced(dsp + '/' + pin)}")
    if contract["reset_mapped"]:
        for pin in contract["rst_target_pins"]:
            lines.append(
                f"fdagents_prc_net_name {_braced(dsp + '/' + pin)} "
                f"{_braced(reset_net)}"
            )
        for pin in contract["rst_source_pins"]:
            lines.append(f"fdagents_prc_gnd_type {_braced(dsp + '/' + pin)}")
    else:
        for pin in ("RSTA", "RSTB", "RSTM"):
            lines.append(f"fdagents_prc_gnd_type {_braced(dsp + '/' + pin)}")
    control_pins = [
        f"{dsp}/{pin}" for pin in ("CEA2", "CEB2", "CEM", "RSTA", "RSTB", "RSTM")
    ]
    lines.extend([
        f"set fdagents_prc_touched [lsort -unique "
        f"[get_nets -quiet -of_objects {_pins(control_pins)}]]",
        "set fdagents_prc_unrouted [filter -quiet $fdagents_prc_touched {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_prc_unrouted] != 0} {error {postcheck found unrouted touched nets}}",
        "puts {FDAGENTS_PIPELINE_REBALANCE_ROUTED_OK}",
    ])
    lines.extend(_port_signature_lines("fdagents_prc_b64"))
    lines.append("puts {FDAGENTS_PIPELINE_REBALANCE_POSTCHECK_DONE}")
    return "\n".join(lines)
