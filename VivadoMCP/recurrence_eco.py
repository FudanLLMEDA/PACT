"""Fixed-template Vivado ECO builder for a proved DSP recurrence cut."""

from __future__ import annotations

import re
from typing import Any


_NAME_RE = re.compile(r"^[A-Za-z0-9_./:+\-\[\]]{1,1024}$")
_SITE_RE = re.compile(r"^SLICE_X\d+Y\d+$")
_LUT_BEL_RE = re.compile(r"^[A-H]6LUT$")
_FF_BEL_RE = re.compile(r"^[A-H]FF2?$")
_PLACEMENT_MANIFEST_VERSION = 4


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{field} contains unsupported characters")
    return value


def _braced(value: str) -> str:
    if "}" in value or "\n" in value or "\r" in value:
        raise ValueError("Tcl data contains unsupported characters")
    return "{" + value + "}"


def _pins(names: list[str]) -> str:
    return "[get_pins -quiet [list " + " ".join(_braced(name) for name in names) + "]]"


def _counter_next_init(bit: int, width: int, threshold: int) -> int:
    value = 0
    mask = (1 << width) - 1
    for assignment in range(64):
        current = assignment & mask
        following = current if current >= threshold else current + 1
        value |= ((following >> bit) & 1) << assignment
    return value


def _counter_valid_init(width: int, threshold: int) -> int:
    mask = (1 << width) - 1
    return sum(
        1 << assignment
        for assignment in range(64)
        if (assignment & mask) >= threshold
    )


def _placement_prefix(candidate: dict[str, Any], groups: list[dict[str, Any]]) -> str:
    authorization = candidate.get("authorization") or {}
    namespace = _name(str(authorization.get("certificate_id", ""))[:12], "certificate_id")
    base = f"fdagents_recur_{namespace}"
    first_group = next(
        (group for group in groups if group.get("segment_index") == 0), None
    )
    first_cell = next(
        (
            cell for cell in (first_group or {}).get("srl_cells", [])
            if cell.get("bit") == 0
        ),
        None,
    )
    marker = "_s00_b000"
    cell_name = _name((first_cell or {}).get("cell"), "first SRL cell")
    if not cell_name.endswith(marker):
        raise ValueError("placement SRL namespace is malformed")
    prefix = cell_name[:-len(marker)]
    if re.fullmatch(re.escape(base) + r"(?:_[a-z0-9_]{1,16})?", prefix) is None:
        raise ValueError("placement namespace differs from its authorization")
    return prefix


def _validated_ce_clone_assignments(
    groups: list[dict[str, Any]],
    state_width: int,
    depths: tuple[int, ...],
) -> list[dict[str, Any]]:
    """Validate the exact one-clone-per-segment/SRL-site ownership graph."""
    assignments = []
    seen_srl_cells = set()
    seen_clone_cells = set()
    for segment_index, (group, depth) in enumerate(zip(groups, depths)):
        if (
            not isinstance(group, dict)
            or group.get("segment_index") != segment_index
            or group.get("depth") != depth
            or group.get("ce_owner")
            != "identity_lut1_per_srl_site_per_segment"
        ):
            raise ValueError("placement CE clone group identity is invalid")
        cells = group.get("srl_cells")
        clones = group.get("ce_clones")
        if (
            not isinstance(cells, list)
            or [cell.get("bit") for cell in cells if isinstance(cell, dict)]
            != list(range(state_width))
            or not isinstance(clones, list)
        ):
            raise ValueError("placement CE clone group coverage is invalid")
        cells_by_site: dict[str, list[dict[str, Any]]] = {}
        for cell in cells:
            if not isinstance(cell, dict):
                raise ValueError("placement SRL cell is invalid")
            cell_name = _name(cell.get("cell"), "srl.cell")
            site = cell.get("site")
            source_bit = cell.get("ce_source_bit")
            clone_cell = cell.get("ce_clone_cell")
            if (
                cell_name in seen_srl_cells
                or not isinstance(site, str)
                or isinstance(source_bit, bool)
                or not isinstance(source_bit, int)
                or not 0 <= source_bit < state_width
                or not isinstance(clone_cell, str)
                or not clone_cell
            ):
                raise ValueError("placement SRL CE ownership is invalid")
            seen_srl_cells.add(cell_name)
            cells_by_site.setdefault(site, []).append(cell)
        if len(clones) != len(cells_by_site):
            raise ValueError("placement does not provide one CE clone per SRL site")
        seen_owner_sites = set()
        for clone in clones:
            if not isinstance(clone, dict) or set(clone) != {
                "cell", "srl_owner_site", "ce_source_bit", "site", "bel", "srl_cells",
            }:
                raise ValueError("placement CE clone schema is invalid")
            clone_name = _name(clone.get("cell"), "ce_clone.cell")
            owner_site = clone.get("srl_owner_site")
            source_bit = clone.get("ce_source_bit")
            assigned = clone.get("srl_cells")
            if (
                clone_name in seen_clone_cells
                or owner_site not in cells_by_site
                or owner_site in seen_owner_sites
                or isinstance(source_bit, bool)
                or not isinstance(source_bit, int)
                or not 0 <= source_bit < state_width
                or not isinstance(assigned, list)
            ):
                raise ValueError("placement CE clone assignment is invalid")
            owner_cells = cells_by_site[owner_site]
            if (
                assigned != [cell["cell"] for cell in owner_cells]
                or {cell["ce_source_bit"] for cell in owner_cells} != {source_bit}
                or {cell["ce_clone_cell"] for cell in owner_cells} != {clone_name}
            ):
                raise ValueError("placement CE clone assignment differs from its SRLs")
            seen_clone_cells.add(clone_name)
            seen_owner_sites.add(owner_site)
            assignments.append(clone)
        if seen_owner_sites != set(cells_by_site):
            raise ValueError("placement CE clone owner coverage is incomplete")
    return assignments


def _compact_creation_commands(lines: list[str]) -> list[str]:
    cells: dict[str, list[str]] = {}
    nets = []
    init_groups: dict[str, list[str]] = {}
    remaining = []
    insertion = None
    for line in lines:
        cell_match = re.fullmatch(r"create_cell -reference (\S+) (\S+)", line)
        net_match = re.fullmatch(r"create_net (\S+)", line)
        init_match = re.fullmatch(
            r"set_property INIT (\S+) \[get_cells (\S+)\]", line
        )
        if cell_match:
            insertion = len(remaining) if insertion is None else insertion
            cells.setdefault(cell_match.group(1), []).append(cell_match.group(2))
        elif net_match:
            insertion = len(remaining) if insertion is None else insertion
            nets.append(net_match.group(1))
        elif init_match:
            insertion = len(remaining) if insertion is None else insertion
            init_groups.setdefault(init_match.group(1), []).append(init_match.group(2))
        else:
            remaining.append(line)
    if insertion is None:
        return remaining
    batched = []
    for reference, names in cells.items():
        batched.append(
            f"create_cell -reference {reference} "
            + "[list " + " ".join(_braced(name) for name in names) + "]"
        )
    if nets:
        batched.append(
            "create_net [list " + " ".join(_braced(name) for name in nets) + "]"
        )
    for value, names in init_groups.items():
        batched.append(
            f"set_property INIT {value} [get_cells [list "
            + " ".join(_braced(name) for name in names) + "]]"
        )
    return [*remaining[:insertion], *batched, *remaining[insertion:]]


def build_recurrence_eco_tcl(
    facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
    run_route: bool = True,
    stop_after: str | None = None,
) -> str:
    """Build fixed Tcl from hash-validated manifests; caller owns hash replay."""
    plan = candidate.get("plan")
    if hasattr(plan, "state_width"):
        state_width = int(plan.state_width)
        counter_width = int(plan.counter_width)
        threshold = int(plan.mask_valid_transactions)
        depths = tuple(plan.srl_segment_depths)
    elif isinstance(plan, dict):
        state_width = int(plan["state_width"])
        counter_width = int(plan["counter_width"])
        threshold = int(plan["mask_valid_transactions"])
        depths = tuple(int(item) for item in plan["srl_segment_depths"])
    else:
        raise ValueError("recurrence candidate plan is missing")
    if not 2 <= state_width <= 256 or not 1 <= counter_width <= 6:
        raise ValueError("ECO template supports width 2..256 and counter width 1..6")
    if sum(depths) != int(facts["rewrite_plan"]["transport_valid_transactions"]):
        raise ValueError("SRL depths differ from recovered transport delay")
    groups = placement.get("groups")
    support = placement.get("support_logic")
    counter_ffs = placement.get("counter_ffs")
    if (
        placement.get("kind") != "recurrence_srl_placement_manifest"
        or placement.get("version") != _PLACEMENT_MANIFEST_VERSION
    ):
        raise ValueError("placement manifest does not provide versioned CE clones")
    if not isinstance(groups, list) or len(groups) != len(depths):
        raise ValueError("placement groups do not cover every segment")
    if not isinstance(support, list) or not isinstance(counter_ffs, list):
        raise ValueError("placement omits recurrence support logic")
    ce_clones = _validated_ce_clone_assignments(groups, state_width, depths)
    expected_support_roles = [
        *(f"counter_next_{bit}" for bit in range(counter_width)),
        "counter_valid",
        *(f"transport_mask_{bit}" for bit in range(state_width)),
    ]
    if [item.get("role") for item in support if isinstance(item, dict)] != expected_support_roles:
        raise ValueError("placement support-role coverage is invalid")
    if [item.get("bit") for item in counter_ffs if isinstance(item, dict)] != list(
        range(counter_width)
    ):
        raise ValueError("placement counter FF coverage is invalid")
    if any(item.get("reused_relay") is not True for item in counter_ffs):
        raise ValueError("placement counter FFs must reuse proved relay cells")
    if stop_after not in {None, "cells", "new_nets", "internal", "creation", "placement", "rewire", "relay_removal"}:
        raise ValueError("stop_after is invalid")

    source_dsp = _name(facts["source_dsp"], "source_dsp")
    boundary_dsp = _name(facts["sink_dsp"], "boundary_dsp")
    rewrite = facts["rewrite_plan"]
    reinjection_dsp = _name(rewrite["reinjection_dsp"], "reinjection_dsp")
    relay_bits = facts["relay_bits"]
    if len(relay_bits) != state_width:
        raise ValueError("relay bit manifest is incomplete")
    arithmetic = rewrite.get("arithmetic_rewrite_contract") or {}
    reinjection = arithmetic.get("reinjection") or {}
    target_opmode = int(reinjection.get("target_opmode", -1))
    if target_opmode != 405:
        raise ValueError("unsupported or unproved reinjection OPMODE")
    current_creg = reinjection.get("current_creg")
    target_creg = reinjection.get("target_creg")
    creg_action = reinjection.get("creg_action")
    if (
        current_creg not in {0, 1}
        or target_creg != 0
        or creg_action not in {"disable", "preserve"}
        or creg_action != ("disable" if current_creg == 1 else "preserve")
    ):
        raise ValueError("reinjection CREG contract is incomplete")
    preg = rewrite.get("source_preg_control_contract") or {}
    if preg.get("target_preg") != 1 or preg.get("preg_action") not in {"enable", "preserve"}:
        raise ValueError("source PREG contract is incomplete")

    clock_pin = _name(
        facts["control_equivalence_groups"]["C"]["members"][0]["pin"],
        "clock_pin",
    )
    ce_pin = _name(
        facts["control_equivalence_groups"]["CE"]["members"][0]["pin"],
        "ce_pin",
    )
    reset_pin = _name(
        facts["control_equivalence_groups"]["R"]["members"][0]["pin"],
        "reset_pin",
    )
    prefix = _placement_prefix(candidate, groups)

    clock_sinks = []
    ce_sinks = []
    reset_sinks = []
    gnd_sinks = [f"{prefix}_gnd/G"]
    vcc_sinks = [f"{prefix}_vcc/P"]
    lines = [
        "proc fdagents_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"required pin missing: $name\"}; return $value}",
        "proc fdagents_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"required cell missing: $name\"}; return $value}",
        "proc fdagents_pin_net {name} {set pin [fdagents_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"required pin net missing: $name\"}; return $value}",
        "proc fdagents_disconnect_pin {name} {set pin [fdagents_one_pin $name]; set net [get_nets -quiet -of_objects $pin]; if {[llength $net] != 1} {error \"disconnect pin net missing: $name\"}; disconnect_net -net $net -objects $pin}",
        f"set fdagents_clock_net [fdagents_pin_net {_braced(clock_pin)}]",
        f"set fdagents_ce_net [fdagents_pin_net {_braced(ce_pin)}]",
        f"set fdagents_reset_net [fdagents_pin_net {_braced(reset_pin)}]",
        f"if {{[llength [get_cells -quiet {prefix}*]]}} {{error {{recurrence ECO namespace exists}}}}",
        f"create_cell -reference GND {prefix}_gnd",
        f"create_cell -reference VCC {prefix}_vcc",
        f"create_net {prefix}_gnd_net",
        f"create_net {prefix}_vcc_net",
    ]

    if preg["preg_action"] == "enable":
        lines.extend([
            f"set fdagents_source_dsp [fdagents_one_cell {_braced(source_dsp)}]",
            "if {[get_property PREG $fdagents_source_dsp] != 0} {error {source PREG changed}}",
            "set_property PREG 1 $fdagents_source_dsp",
            "if {[get_property PREG $fdagents_source_dsp] != 1} {error {source PREG enable failed}}",
            f"fdagents_disconnect_pin {_braced(source_dsp + '/CLK')}",
            f"connect_net -hierarchical -net $fdagents_clock_net -objects [fdagents_one_pin {_braced(source_dsp + '/CLK')}]",
            f"fdagents_disconnect_pin {_braced(source_dsp + '/CEP')}",
            f"connect_net -hierarchical -net $fdagents_ce_net -objects [fdagents_one_pin {_braced(source_dsp + '/CEP')}]",
            f"fdagents_disconnect_pin {_braced(source_dsp + '/RSTP')}",
            f"connect_net -hierarchical -net $fdagents_reset_net -objects [fdagents_one_pin {_braced(source_dsp + '/RSTP')}]",
        ])

    lines.extend([
        f"set fdagents_reinjection_dsp [fdagents_one_cell {_braced(reinjection_dsp)}]",
        f"if {{[get_property CREG $fdagents_reinjection_dsp] != {current_creg}}} {{error {{reinjection CREG changed}}}}",
    ])
    if creg_action == "disable":
        lines.append("set_property CREG 0 $fdagents_reinjection_dsp")
    lines.append(
        "if {[get_property CREG $fdagents_reinjection_dsp] != 0} {error {reinjection CREG disable failed}}"
    )

    for group, depth in zip(groups, depths):
        for cell in group["srl_cells"]:
            cell_name = _name(cell["cell"], "srl.cell")
            lines.extend([
                f"create_cell -reference SRLC32E {cell_name}",
                f"set_property INIT 32'h00000000 [get_cells {cell_name}]",
            ])
            clock_sinks.append(f"{cell_name}/CLK")
            for address_bit in range(5):
                static = "vcc" if ((depth - 1) >> address_bit) & 1 else "gnd"
                (vcc_sinks if static == "vcc" else gnd_sinks).append(
                    f"{cell_name}/A[{address_bit}]"
                )
    for clone in ce_clones:
        cell = _name(clone["cell"], "CE clone")
        net = _name(f"{cell}_out", "CE clone output net")
        sinks = [f"{cell}/O", *(f"{name}/CE" for name in clone["srl_cells"])]
        lines.extend([
            f"create_cell -reference LUT1 {cell}",
            f"set_property INIT 2'h2 [get_cells {cell}]",
            f"create_net {net}",
            f"connect_net -net {net} -objects {_pins(sinks)}",
        ])
        ce_sinks.append(f"{cell}/I0")
    support_by_role = {item["role"]: item for item in support}
    group_cells = [
        {int(cell["bit"]): cell["cell"] for cell in group["srl_cells"]}
        for group in groups
    ]
    final_q_nets = {}
    for bit in range(state_width):
        source_pin = _name(f"{relay_bits[bit]['ff']['cell']}/D", "relay D pin")
        first_cell = group_cells[0][bit]
        lines.append(
            f"connect_net -hierarchical -net [fdagents_pin_net {_braced(source_pin)}] "
            f"-objects [get_pins {first_cell}/D]"
        )
        for segment_index, cells in enumerate(group_cells):
            cell_name = cells[bit]
            net_name = f"{prefix}_s{segment_index:02d}_b{bit:03d}_q"
            net_pins = [f"{cell_name}/Q"]
            if segment_index + 1 < len(group_cells):
                net_pins.append(f"{group_cells[segment_index + 1][bit]}/D")
            else:
                final_q_nets[bit] = net_name
                net_pins.append(
                    f"{support_by_role[f'transport_mask_{bit}']['cell']}/I0"
                )
            lines.extend([
                f"create_net {net_name}",
                f"connect_net -net {net_name} -objects {_pins(net_pins)}",
            ])

    for bit in range(counter_width):
        item = support_by_role[f"counter_next_{bit}"]
        cell = _name(item["cell"], "counter LUT")
        init = _counter_next_init(bit, counter_width, threshold)
        lines.extend([
            f"create_cell -reference LUT6 {cell}",
            f"set_property INIT 64'h{init:016x} [get_cells {cell}]",
        ])
    valid_item = support_by_role["counter_valid"]
    valid_cell = _name(valid_item["cell"], "valid LUT")
    valid_init = _counter_valid_init(counter_width, threshold)
    lines.extend([
        f"create_cell -reference LUT6 {valid_cell}",
        f"set_property INIT 64'h{valid_init:016x} [get_cells {valid_cell}]",
        f"create_net {prefix}_counter_valid_net",
    ])

    ff_by_bit = {int(item["bit"]): item for item in counter_ffs}
    counter_q_nets = {}
    for bit in range(counter_width):
        ff_item = ff_by_bit[bit]
        ff = _name(ff_item["cell"], "counter FF")
        lut = _name(support_by_role[f"counter_next_{bit}"]["cell"], "counter LUT")
        q_net = f"{prefix}_counter_q_{bit}"
        d_net = f"{prefix}_counter_d_{bit}"
        counter_q_nets[bit] = q_net
        lines.extend([
            f"fdagents_disconnect_pin {_braced(ff + '/D')}",
            f"fdagents_disconnect_pin {_braced(ff + '/Q')}",
            f"create_net {q_net}",
            f"create_net {d_net}",
            f"connect_net -hierarchical -net {d_net} -objects {_pins([f'{lut}/O', f'{ff}/D'])}",
        ])
    for input_bit in range(counter_width):
        q_pins = [f"{ff_by_bit[input_bit]['cell']}/Q", f"{valid_cell}/I{input_bit}"]
        q_pins.extend(
            f"{support_by_role[f'counter_next_{bit}']['cell']}/I{input_bit}"
            for bit in range(counter_width)
        )
        lines.append(
            f"connect_net -hierarchical -net {counter_q_nets[input_bit]} -objects {_pins(q_pins)}"
        )
    for input_bit in range(counter_width, 6):
        gnd_sinks.append(f"{valid_cell}/I{input_bit}")
        gnd_sinks.extend(
            f"{support_by_role[f'counter_next_{bit}']['cell']}/I{input_bit}"
            for bit in range(counter_width)
        )

    mask_nets = {}
    for bit in range(state_width):
        item = support_by_role[f"transport_mask_{bit}"]
        cell = _name(item["cell"], "mask LUT")
        net = f"{prefix}_masked_{bit}"
        mask_nets[bit] = net
        lines.extend([
            f"create_cell -reference LUT2 {cell}",
            f"set_property INIT 4'h8 [get_cells {cell}]",
            f"create_net {net}",
            f"connect_net -net {net} -objects [get_pins {cell}/O]",
        ])

    valid_sinks = [f"{valid_cell}/O"] + [
        f"{support_by_role[f'transport_mask_{bit}']['cell']}/I1"
        for bit in range(state_width)
    ]
    lines = _compact_creation_commands(lines)
    if stop_after in {"cells", "new_nets"}:
        filtered = []
        for line in lines:
            stripped = line.lstrip()
            is_connect = stripped.startswith("connect_net ")
            if stop_after == "cells" and is_connect:
                continue
            if stop_after == "new_nets" and is_connect and "-hierarchical" in line:
                continue
            filtered.append(line)
        filtered.append(
            "puts {FDAGENTS_RECURRENCE_ECO_CELLS_DONE}"
            if stop_after == "cells"
            else "puts {FDAGENTS_RECURRENCE_ECO_NEW_NETS_DONE}"
        )
        return "\n".join(filtered)
    if stop_after == "internal":
        lines.append("puts {FDAGENTS_RECURRENCE_ECO_INTERNAL_DONE}")
        return "\n".join(lines)
    lines.extend([
        f"connect_net -hierarchical -net $fdagents_clock_net -objects {_pins(clock_sinks)}",
        f"connect_net -hierarchical -net $fdagents_ce_net -objects {_pins(ce_sinks)}",
        f"connect_net -net {prefix}_gnd_net -objects {_pins(gnd_sinks)}",
        f"connect_net -net {prefix}_vcc_net -objects {_pins(vcc_sinks)}",
        f"connect_net -net {prefix}_counter_valid_net -objects {_pins(valid_sinks)}",
    ])
    if reset_sinks:
        lines.append(
            f"connect_net -hierarchical -net $fdagents_reset_net -objects {_pins(reset_sinks)}"
        )
    placement_pairs = []
    for group in groups:
        for cell in group["srl_cells"]:
            if _SITE_RE.fullmatch(cell["site"]) is None or _LUT_BEL_RE.fullmatch(cell["bel"]) is None:
                raise ValueError("SRL placement is invalid")
            placement_pairs.append((cell["cell"], cell["site"] + "/" + cell["bel"]))
    for item in ce_clones:
        if _SITE_RE.fullmatch(item["site"]) is None or _LUT_BEL_RE.fullmatch(item["bel"]) is None:
            raise ValueError("CE clone placement is invalid")
        placement_pairs.append((item["cell"], item["site"] + "/" + item["bel"]))
    for item in support:
        if _SITE_RE.fullmatch(item["site"]) is None or _LUT_BEL_RE.fullmatch(item["bel"]) is None:
            raise ValueError("support LUT placement is invalid")
        placement_pairs.append((item["cell"], item["site"] + "/" + item["bel"]))
    for item in counter_ffs:
        if _SITE_RE.fullmatch(item["site"]) is None or _FF_BEL_RE.fullmatch(item["bel"]) is None:
            raise ValueError("counter FF placement is invalid")
        if item.get("reused_relay") is not True:
            placement_pairs.append((item["cell"], item["site"] + "/" + item["bel"]))
    if (
        len({cell for cell, _ in placement_pairs}) != len(placement_pairs)
        or len({bel for _, bel in placement_pairs}) != len(placement_pairs)
    ):
        raise ValueError("placement reuses a generated cell or physical BEL")
    if stop_after == "creation":
        lines.append("puts {FDAGENTS_RECURRENCE_ECO_CREATION_DONE}")
        return "\n".join(lines)
    lines.append("set fdagents_place_args [list]")
    for cell, bel in placement_pairs:
        lines.append(
            "lappend fdagents_place_args "
            f"[get_cells {_braced(cell)}] [get_bels {_braced(bel)}]"
        )
    lines.extend([
        f"if {{[llength $fdagents_place_args] != {2 * len(placement_pairs)}}} {{error {{placement manifest mismatch}}}}",
        "place_cell {*}$fdagents_place_args",
    ])
    if stop_after == "placement":
        lines.append("puts {FDAGENTS_RECURRENCE_ECO_PLACEMENT_DONE}")
        return "\n".join(lines)

    dsp_width = int(facts["dsp_c_width"])
    for bit in range(dsp_width):
        boundary_pin = _name(f"{boundary_dsp}/C[{bit}]", "boundary C pin")
        lines.extend([
            f"fdagents_disconnect_pin {_braced(boundary_pin)}",
            f"connect_net -hierarchical -net {prefix}_gnd_net -objects [fdagents_one_pin {_braced(boundary_pin)}]",
        ])
        reinjection_pin = _name(f"{reinjection_dsp}/C[{bit}]", "reinjection C pin")
        source_bit = min(bit, state_width - 1)
        lines.extend([
            f"fdagents_disconnect_pin {_braced(reinjection_pin)}",
            f"connect_net -hierarchical -net {mask_nets[source_bit]} -objects [fdagents_one_pin {_braced(reinjection_pin)}]",
        ])

    for bit in range(9):
        pin = _name(f"{reinjection_dsp}/OPMODE[{bit}]", "reinjection OPMODE pin")
        static = "vcc" if (target_opmode >> bit) & 1 else "gnd"
        lines.extend([
            f"fdagents_disconnect_pin {_braced(pin)}",
            f"connect_net -hierarchical -net {prefix}_{static}_net -objects [fdagents_one_pin {_braced(pin)}]",
        ])
    if stop_after == "rewire":
        lines.append("puts {FDAGENTS_RECURRENCE_ECO_REWIRE_DONE}")
        return "\n".join(lines)

    retained_relays = {item["cell"] for item in counter_ffs}
    removed_relays = []
    for item in relay_bits:
        relay = _name(item["ff"]["cell"], "relay cell")
        if relay in retained_relays:
            continue
        removed_relays.append(relay)
        lines.append(f"remove_cell [fdagents_one_cell {_braced(relay)}]")
    if stop_after == "relay_removal":
        lines.append("puts {FDAGENTS_RECURRENCE_ECO_RELAY_REMOVAL_DONE}")
        return "\n".join(lines)
    if removed_relays:
        lines.append(
            f"if {{[llength [get_cells -quiet [list {_braced(removed_relays[0])}]]] != 0}} "
            "{error {relay removal failed}}"
        )
    if run_route:
        lines.extend([
            "puts {FDAGENTS_RECURRENCE_ECO_ROUTE_START}",
            "route_design -preserve -directive Quick",
            "set fdagents_unrouted [get_nets -quiet -hierarchical -filter {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
            "if {[llength $fdagents_unrouted] != 0} {error \"recurrence ECO left [llength $fdagents_unrouted] unrouted nets\"}",
        ])
    lines.extend([
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_RECURRENCE_ECO_DONE}",
    ])
    return "\n".join(lines)


def build_recurrence_postcheck_tcl(
    facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
) -> str:
    """Build fresh-checkpoint assertions for the emitted recurrence topology."""
    plan = candidate.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("recurrence postcheck requires a JSON plan")
    state_width = int(plan["state_width"])
    depths = tuple(int(item) for item in plan["srl_segment_depths"])
    groups = placement.get("groups")
    support = placement.get("support_logic")
    counter_ffs = placement.get("counter_ffs")
    if (
        placement.get("kind") != "recurrence_srl_placement_manifest"
        or placement.get("version") != _PLACEMENT_MANIFEST_VERSION
    ):
        raise ValueError("postcheck placement does not provide versioned CE clones")
    if not isinstance(groups, list) or len(groups) != len(depths):
        raise ValueError("recurrence postcheck placement groups are incomplete")
    if not isinstance(support, list) or not isinstance(counter_ffs, list):
        raise ValueError("recurrence postcheck support placement is incomplete")
    ce_clones = _validated_ce_clone_assignments(groups, state_width, depths)
    prefix = _placement_prefix(candidate, groups)
    rewrite = facts["rewrite_plan"]
    source_dsp = _name(facts["source_dsp"], "source_dsp")
    boundary_dsp = _name(facts["sink_dsp"], "boundary_dsp")
    reinjection_dsp = _name(rewrite["reinjection_dsp"], "reinjection_dsp")
    support_by_role = {item["role"]: item for item in support}
    group_cells = [
        {int(cell["bit"]): cell for cell in group["srl_cells"]}
        for group in groups
    ]
    expected_cells = [f"{prefix}_gnd", f"{prefix}_vcc"]
    expected_cells.extend(
        cell["cell"] for group in groups for cell in group["srl_cells"]
    )
    expected_cells.extend(item["cell"] for item in ce_clones)
    expected_cells.extend(item["cell"] for item in support)
    expected_cells.extend(
        item["cell"] for item in counter_ffs
        if item.get("reused_relay") is not True
    )
    lines = [
        "proc fdagents_pc_one_cell {name} {set value [get_cells -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck cell missing: $name\"}; return $value}",
        "proc fdagents_pc_one_pin {name} {set value [get_pins -quiet [list $name]]; if {[llength $value] != 1} {error \"postcheck pin missing: $name\"}; return $value}",
        "proc fdagents_pc_pin_net {name} {set pin [fdagents_pc_one_pin $name]; set value [get_nets -quiet -of_objects $pin]; if {[llength $value] != 1} {error \"postcheck pin net missing: $name\"}; return $value}",
        "proc fdagents_pc_pin_segments {name} {set value [get_nets -quiet -segments -of_objects [fdagents_pc_one_pin $name]]; if {[llength $value] == 0} {error \"postcheck pin net segments missing: $name\"}; return $value}",
        "proc fdagents_pc_same_net {left right} {set left_nets [fdagents_pc_pin_segments $left]; set right_nets [fdagents_pc_pin_segments $right]; foreach net $left_nets {if {[lsearch -exact $right_nets $net] >= 0} {return}}; error \"postcheck net mismatch: $left vs $right left=$left_nets right=$right_nets\"}",
        "proc fdagents_pc_named_net {pin expected} {set actual [fdagents_pc_pin_segments $pin]; set nets [get_nets -quiet -hierarchical [list $expected]]; if {[llength $nets] > 0} {foreach expected_net $nets {set connected 0; foreach segment [get_nets -quiet -segments $expected_net] {if {[lsearch -exact $actual $segment] >= 0} {set connected 1; break}}; if {!$connected} {error \"postcheck named net mismatch: $pin expected=$expected_net\"}}; return}; set source_pins [get_pins -quiet -hierarchical [list $expected]]; if {[llength $source_pins] != 1} {error \"postcheck named net or driver pin missing: $expected\"}; set expected_segments [get_nets -quiet -segments -of_objects $source_pins]; foreach segment $expected_segments {if {[lsearch -exact $actual $segment] >= 0} {return}}; error \"postcheck driver net mismatch: $pin expected=$expected\"}",
        "proc fdagents_pc_ref {cell expected} {if {[get_property REF_NAME [fdagents_pc_one_cell $cell]] ne $expected} {error \"postcheck REF_NAME mismatch: $cell\"}}",
        "proc fdagents_pc_init {cell expected} {set actual [string tolower [get_property INIT [fdagents_pc_one_cell $cell]]]; if {$actual ne [string tolower $expected]} {error \"postcheck INIT mismatch: $cell expected=$expected actual=$actual\"}}",
        "proc fdagents_pc_exact_net_pins {pin expected} {set actual [list]; foreach net [fdagents_pc_pin_segments $pin] {foreach item [get_pins -quiet -leaf -of_objects $net] {lappend actual [get_property NAME $item]}}; set actual [lsort -unique $actual]; set expected [lsort -unique $expected]; if {$actual ne $expected} {error \"postcheck net leaf-pin inventory mismatch: $pin expected=$expected actual=$actual\"}}",
        "proc fdagents_pc_place {cell site bel} {set value [fdagents_pc_one_cell $cell]; set actual_loc [get_property LOC $value]; set actual_bel [get_property BEL $value]; if {$actual_loc ne $site || ![string match \"*.$bel\" $actual_bel]} {error \"postcheck placement mismatch: $cell expected=$site/$bel actual=$actual_loc/$actual_bel\"}}",
        f"proc fdagents_pc_static {{pin expected}} {{if {{$expected eq \"GND\"}} {{set source {_braced(prefix + '_gnd/G')}}} elseif {{$expected eq \"VCC\"}} {{set source {_braced(prefix + '_vcc/P')}}} else {{error \"postcheck static kind invalid: $expected\"}}; fdagents_pc_same_net $pin $source}}",
        "set fdagents_pc_expected [lsort [list "
        + " ".join(_braced(name) for name in expected_cells)
        + "]]",
        f"set fdagents_pc_actual [lsort [get_property NAME [get_cells -quiet -hierarchical {_braced(prefix + '*')}]]]",
        "if {$fdagents_pc_actual ne $fdagents_pc_expected} {error {postcheck namespace inventory mismatch}}",
    ]
    clock_net = _name(facts["controls"]["clock_net"], "clock_net")
    for group_index, (cells, depth) in enumerate(zip(group_cells, depths)):
        for bit in range(state_width):
            item = cells[bit]
            cell = _name(item["cell"], "srl cell")
            lines.extend([
                f"fdagents_pc_ref {_braced(cell)} SRLC32E",
                f"fdagents_pc_place {_braced(cell)} {_braced(item['site'])} {_braced(item['bel'])}",
                f"fdagents_pc_named_net {_braced(cell + '/CLK')} {_braced(clock_net)}",
            ])
            for address_bit in range(5):
                expected = "VCC" if ((depth - 1) >> address_bit) & 1 else "GND"
                lines.append(
                    f"fdagents_pc_static {_braced(cell + f'/A[{address_bit}]')} {expected}"
                )
            if group_index == 0:
                source_pin = f"{source_dsp}/DSP_OUTPUT_INST/P[{bit}]"
            else:
                source_pin = f"{group_cells[group_index - 1][bit]['cell']}/Q"
            lines.append(
                f"fdagents_pc_same_net {_braced(cell + '/D')} {_braced(source_pin)}"
            )
            if group_index + 1 == len(group_cells):
                mask_pin = f"{support_by_role[f'transport_mask_{bit}']['cell']}/I0"
                lines.append(
                    f"fdagents_pc_same_net {_braced(cell + '/Q')} {_braced(mask_pin)}"
                )
    ce_net = _name(facts["controls"]["ce_net"], "ce_net")
    for item in ce_clones:
        cell = _name(item["cell"], "CE clone")
        expected_pins = [f"{cell}/O", *(f"{name}/CE" for name in item["srl_cells"])]
        lines.extend([
            f"fdagents_pc_ref {_braced(cell)} LUT1",
            f"fdagents_pc_place {_braced(cell)} {_braced(item['site'])} {_braced(item['bel'])}",
            f"fdagents_pc_init {_braced(cell)} {{2'h2}}",
            f"fdagents_pc_named_net {_braced(cell + '/I0')} {_braced(ce_net)}",
            f"fdagents_pc_exact_net_pins {_braced(cell + '/O')} [list "
            + " ".join(_braced(pin) for pin in expected_pins)
            + "]",
        ])
    for item in support:
        role = item["role"]
        reference = "LUT2" if role.startswith("transport_mask_") else "LUT6"
        lines.extend([
            f"fdagents_pc_ref {_braced(item['cell'])} {reference}",
            f"fdagents_pc_place {_braced(item['cell'])} {_braced(item['site'])} {_braced(item['bel'])}",
        ])
    for item in counter_ffs:
        bit = int(item["bit"])
        next_lut = support_by_role[f"counter_next_{bit}"]["cell"]
        q_pins = [
            f"{item['cell']}/Q",
            f"{support_by_role['counter_valid']['cell']}/I{bit}",
            *(
                f"{support_by_role[f'counter_next_{output_bit}']['cell']}/I{bit}"
                for output_bit in range(len(counter_ffs))
            ),
        ]
        lines.extend([
            f"fdagents_pc_ref {_braced(item['cell'])} FDRE",
            f"fdagents_pc_place {_braced(item['cell'])} {_braced(item['site'])} {_braced(item['bel'])}",
            f"fdagents_pc_init {_braced(item['cell'])} {{1'b0}}",
            f"fdagents_pc_named_net {_braced(item['cell'] + '/C')} {_braced(clock_net)}",
            f"fdagents_pc_named_net {_braced(item['cell'] + '/CE')} {_braced(facts['controls']['ce_net'])}",
            f"fdagents_pc_named_net {_braced(item['cell'] + '/R')} {_braced(facts['controls']['reset_net'])}",
            f"fdagents_pc_same_net {_braced(item['cell'] + '/D')} {_braced(next_lut + '/O')}",
            f"fdagents_pc_exact_net_pins {_braced(item['cell'] + '/Q')} [list "
            + " ".join(_braced(pin) for pin in q_pins)
            + "]",
        ])
    dsp_width = int(facts["dsp_c_width"])
    for bit in range(dsp_width):
        lines.append(
            f"fdagents_pc_static {_braced(boundary_dsp + f'/C[{bit}]')} GND"
        )
        source_bit = min(bit, state_width - 1)
        mask_output = f"{support_by_role[f'transport_mask_{source_bit}']['cell']}/O"
        lines.append(
            f"fdagents_pc_same_net {_braced(reinjection_dsp + f'/C[{bit}]')} {_braced(mask_output)}"
        )
    target_opmode = int(
        rewrite["arithmetic_rewrite_contract"]["reinjection"]["target_opmode"]
    )
    for bit in range(9):
        expected = "VCC" if (target_opmode >> bit) & 1 else "GND"
        lines.append(
            f"fdagents_pc_static {_braced(reinjection_dsp + f'/OPMODE[{bit}]')} {expected}"
        )
    retained_relays = {item["cell"] for item in counter_ffs}
    for relay in facts["relay_bits"]:
        if relay["ff"]["cell"] in retained_relays:
            continue
        lines.append(
            f"if {{[llength [get_cells -quiet [list {_braced(relay['ff']['cell'])}]]] != 0}} {{error {{postcheck relay still exists}}}}"
        )
    lines.extend([
        f"if {{[get_property PREG [fdagents_pc_one_cell {_braced(source_dsp)}]] != 1}} {{error {{postcheck source PREG mismatch}}}}",
        f"if {{[get_property CREG [fdagents_pc_one_cell {_braced(reinjection_dsp)}]] != 0}} {{error {{postcheck reinjection CREG mismatch}}}}",
        "set fdagents_pc_unrouted [get_nets -quiet -hierarchical -filter {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_pc_unrouted] != 0} {error {postcheck found unrouted nets}}",
        "puts {FDAGENTS_RECURRENCE_POSTCHECK_DONE}",
    ])
    return "\n".join(lines)


def build_recurrence_family_eco_tcl(
    family_facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
    *,
    output_dcp: str,
) -> str:
    """Emit every freshly recovered family boundary in one Vivado transaction."""
    boundaries = {
        item["region_sha256"]: item for item in family_facts.get("boundaries", [])
    }
    records = placement.get("placements")
    if not isinstance(records, list) or len(records) != len(boundaries) or len(records) < 2:
        raise ValueError("recurrence family placement coverage is incomplete")
    scripts = []
    seen = set()
    for record in records:
        region = record.get("boundary_region_sha256")
        if region not in boundaries or region in seen:
            raise ValueError("recurrence family placement boundary is invalid")
        seen.add(region)
        scripts.append(build_recurrence_eco_tcl(
            boundaries[region],
            candidate,
            record["placement"],
            output_dcp=output_dcp,
            run_route=False,
            stop_after="relay_removal",
        ))
    scripts.extend([
        "puts {FDAGENTS_RECURRENCE_FAMILY_ROUTE_START}",
        "route_design -eco -preserve",
        "set fdagents_family_unrouted [get_nets -quiet -hierarchical -filter {(ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIALLY_ROUTED) && TYPE != GND && TYPE != VCC}]",
        "if {[llength $fdagents_family_unrouted] != 0} {error \"recurrence family ECO left [llength $fdagents_family_unrouted] unrouted nets\"}",
        f"write_checkpoint -force {_braced(output_dcp)}",
        "puts {FDAGENTS_RECURRENCE_FAMILY_ECO_DONE}",
    ])
    return "\n".join(scripts)


def build_recurrence_family_postcheck_tcl(
    family_facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
) -> str:
    """Reopen-time checks for every member of an emitted recurrence family."""
    boundaries = {
        item["region_sha256"]: item for item in family_facts.get("boundaries", [])
    }
    scripts = []
    seen = set()
    for record in placement.get("placements", []):
        region = record.get("boundary_region_sha256")
        if region not in boundaries or region in seen:
            raise ValueError("recurrence family postcheck boundary is invalid")
        seen.add(region)
        scripts.append(build_recurrence_postcheck_tcl(
            boundaries[region], candidate, record["placement"]
        ))
    if seen != set(boundaries):
        raise ValueError("recurrence family postcheck coverage is incomplete")
    scripts.append("puts {FDAGENTS_RECURRENCE_FAMILY_POSTCHECK_DONE}")
    return "\n".join(scripts)
