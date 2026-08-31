"""Recover and replay fixed-point DSP remap contracts from register cones.

The detector is deliberately identity-free: indexed register buses, truth
tables and the current cone connectivity are its only inputs.  A recovered
contract authorizes a measurement candidate, not final submission; whole-DCP
equivalence remains mandatory after emission.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any, Callable

from .compressor_proof import (
    ConeGraph,
    _carry_chain,
    _downstream_cells,
    _evaluate_boolean_batch,
    _evaluate_target_boolean_batch,
)


CONTRACT_VERSION = 4
TOPOLOGY_VERSION = 4
MAX_DSP_COEFFICIENT_WIDTH = 26  # recovered constant, sign-extended into DSP A[29:0]
DSP_DATA_CHUNK_WIDTH = 17       # unsigned chunks remain positive on B[17:0]
_INDEXED_CELL_RE = re.compile(r"^(?P<stem>.+)\[(?P<index>\d+)\]$")


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(
        _stable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _boundary_buses(graph: ConeGraph) -> list[dict[str, Any]]:
    grouped: dict[str, dict[int, tuple[str, str]]] = {}
    ungrouped = []
    for net, cell in sorted(graph.boundary_nets.items()):
        match = _INDEXED_CELL_RE.fullmatch(cell)
        if match is None:
            ungrouped.append({"cell": cell, "net": net})
            continue
        stem = match.group("stem")
        index = int(match.group("index"))
        if index in grouped.setdefault(stem, {}):
            raise ValueError("fixed-point boundary bus contains a duplicate bit")
        grouped[stem][index] = (net, cell)
    if ungrouped:
        raise ValueError("fixed-point boundary contains non-indexed register bits")
    buses = []
    for stem, members in sorted(grouped.items()):
        indices = sorted(members)
        if len(indices) < 2 or indices != list(range(indices[0], indices[-1] + 1)):
            raise ValueError("fixed-point boundary bus is not a contiguous indexed slice")
        source_lsb = indices[0]
        buses.append({
            "stem": stem,
            "width": len(indices),
            "source_lsb": source_lsb,
            "bits": [
                {
                    "index": index - source_lsb,
                    "source_index": index,
                    "net": members[index][0],
                    "cell": members[index][1],
                }
                for index in indices
            ],
        })
    if not buses:
        raise ValueError("fixed-point boundary has no complete indexed buses")
    return buses


def _sample_words(width: int, rng: random.Random) -> list[int]:
    mask = (1 << width) - 1
    values = {0, 1, 2, 3, mask, mask - 1, 1 << (width - 1)}
    if width > 1:
        values.update({(1 << (width - 1)) - 1, (1 << (width - 1)) + 1})
    for bit in range(width):
        values.add(1 << bit)
        values.add((1 << bit) - 1)
    for _ in range(max(24, 2 * width)):
        values.add(rng.getrandbits(width))
    return sorted(value & mask for value in values)


def _word_assignment(bus: dict[str, Any], value: int) -> tuple[str, ...]:
    return tuple(
        bit["net"] for bit in bus["bits"] if (int(value) >> bit["index"]) & 1
    )


def _collect_sample_table(
    graph: ConeGraph,
    buses: list[dict[str, Any]],
) -> dict[str, Any]:
    seed = int(graph.connectivity_digest()[:16], 16)
    rng = random.Random(seed)
    cases: list[dict[str, int]] = [{}]
    per_bus_values: dict[str, list[int]] = {}
    for bus in buses:
        values = _sample_words(bus["width"], rng)
        per_bus_values[bus["stem"]] = values
        cases.extend({bus["stem"]: value} for value in values)
    for left_index, left in enumerate(buses):
        for right in buses[left_index + 1:]:
            for _ in range(16):
                cases.append({
                    left["stem"]: rng.getrandbits(left["width"]),
                    right["stem"]: rng.getrandbits(right["width"]),
                })
    for _ in range(max(64, 16 * len(buses))):
        cases.append({
            bus["stem"]: rng.getrandbits(bus["width"])
            for bus in buses
        })

    unique: dict[tuple[tuple[str, int], ...], dict[str, int]] = {}
    for case in cases:
        key = tuple(sorted((str(name), int(value)) for name, value in case.items()))
        unique.setdefault(key, case)
    cases = list(unique.values())
    assignments = []
    for case in cases:
        true_nets = []
        for bus in buses:
            true_nets.extend(_word_assignment(bus, case.get(bus["stem"], 0)))
        assignments.append(tuple(sorted(true_nets)))
    output_width = len(graph.targets)
    output_mask = (1 << output_width) - 1
    try:
        chain = _carry_chain(graph)
    except ValueError:
        chain = None
    if chain is not None:
        target_positions = list(chain.get("target_positions") or [])
        target_lsb = int(chain.get("target_lsb", -1))
        if len(target_positions) != len(graph.targets) or target_lsb < 0:
            raise ValueError(
                "fixed-point target slice does not cover the target register bus"
            )
        full_values = _evaluate_boolean_batch(
            graph,
            sorted(graph.boundary_nets),
            assignments,
            chain,
            deadline_check=lambda _stage: None,
        )
        values = [
            (int(value) >> target_lsb) & output_mask for value in full_values
        ]
        chain_width = int(chain["width"])
    else:
        target_lsb = 0
        full_values = _evaluate_target_boolean_batch(
            graph,
            sorted(graph.boundary_nets),
            assignments,
            deadline_check=lambda _stage: None,
        )
        values = [int(value) & output_mask for value in full_values]
        chain_width = output_width
    rows = [
        {
            "inputs": case,
            "output": int(value),
            "source_full_output": int(full_value),
        }
        for case, value, full_value in zip(cases, values, full_values)
    ]
    return {
        "output_width": output_width,
        "source_chain_width": chain_width,
        "source_chain_target_lsb": target_lsb,
        "rows": rows,
        "per_bus_values": per_bus_values,
        "sample_digest": _digest(rows),
    }


def _extend_word(value: int, width: int, target: int, signed: bool) -> int:
    value &= (1 << width) - 1
    if signed and (value >> (width - 1)) & 1:
        value -= 1 << width
    return value & ((1 << target) - 1)


def _passthrough_value(
    value: int,
    width: int,
    output_width: int,
    signed: bool,
    shift: int = 0,
) -> int:
    extended_width = max(width, shift + output_width)
    return (
        _extend_word(value, width, extended_width, signed) >> shift
    ) & ((1 << output_width) - 1)


def _product_value(
    value: int,
    *,
    data_width: int,
    data_signed: bool,
    coefficient: int,
    coefficient_width: int,
    shift: int,
    output_width: int,
) -> int:
    product_width = data_width + coefficient_width
    data = value & ((1 << data_width) - 1)
    if data_signed and (data >> (data_width - 1)) & 1:
        data -= 1 << data_width
    product = (data * coefficient) & ((1 << product_width) - 1)
    return (product >> shift) & ((1 << output_width) - 1)


def _infer_bus_term(
    bus: dict[str, Any],
    samples: list[tuple[int, int]],
    output_width: int,
) -> dict[str, Any] | None:
    mask = (1 << output_width) - 1
    # A signed retained state may be wider than the observable word and still
    # contribute only an arithmetic right-shifted residual.  Restricting the
    # search to ``data_width - output_width`` misclassifies e.g. a 35-bit
    # ``x >>> 4`` contribution to a 32-bit result as ``(2*x) >>> 5``.  Both
    # formulae replay, but the latter needlessly demands a DSP and prevents a
    # heterogeneous product/residual bridge from being represented.  Shifts
    # greater than the source width add no new unsigned behaviour and all have
    # the same signed fill behaviour, so 0..data_width is complete.
    max_passthrough_shift = int(bus["width"])
    for shift in range(max_passthrough_shift + 1):
        for signed in (False, True):
            if all(
                observed == _passthrough_value(
                    value, bus["width"], output_width, signed, shift
                )
                for value, observed in samples
            ):
                return {
                    "kind": "passthrough",
                    "source": bus,
                    "data_width": bus["width"],
                    "data_signed": signed,
                    "shift": shift,
                    "output_width": output_width,
                }

    # The deterministic sample set contains every one-hot input.  For a
    # fixed-product slice, driving ``1 << shift`` exposes the coefficient
    # directly in the output word.  Recover and replay those candidates before
    # asking an SMT solver to search the same finite space.  Besides being much
    # faster, this keeps unsupported closure nodes from consuming 4 * 28
    # independent five-second solver timeouts during live operator mining.
    observed_by_value = {int(value): int(observed) & mask for value, observed in samples}
    direct_candidates = []
    coefficient_width = MAX_DSP_COEFFICIENT_WIDTH
    product_width = bus["width"] + coefficient_width
    for shift in range(product_width - output_width + 1):
        probe = 1 << shift
        if probe >= (1 << int(bus["width"])) or probe not in observed_by_value:
            continue
        raw = observed_by_value[probe]
        coefficient_values = {raw}
        if raw & (1 << (output_width - 1)):
            coefficient_values.add(raw - (1 << output_width))
        for coefficient_value in coefficient_values:
            coefficient_signed = coefficient_value < 0
            if coefficient_signed:
                if not -(1 << (coefficient_width - 1)) <= coefficient_value < 0:
                    continue
            elif not 1 < coefficient_value < (1 << coefficient_width):
                continue
            for data_signed in (True, False):
                if all(
                    observed == _product_value(
                        sample,
                        data_width=bus["width"],
                        data_signed=data_signed,
                        coefficient=coefficient_value,
                        coefficient_width=coefficient_width,
                        shift=shift,
                        output_width=output_width,
                    )
                    for sample, observed in samples
                ):
                    direct_candidates.append((
                        (bus["width"] + DSP_DATA_CHUNK_WIDTH - 1)
                        // DSP_DATA_CHUNK_WIDTH,
                        shift,
                        abs(coefficient_value).bit_length(),
                        abs(coefficient_value),
                        coefficient_signed,
                        coefficient_value,
                        data_signed,
                    ))
    if direct_candidates:
        (
            _, shift, _, _, coefficient_signed, coefficient_value, data_signed
        ) = min(direct_candidates)
        return {
            "kind": "fixed_product_slice",
            "source": bus,
            "data_width": bus["width"],
            "data_signed": bool(data_signed),
            "coefficient": coefficient_value,
            "coefficient_width": coefficient_width,
            "coefficient_signed": bool(coefficient_signed),
            "product_width": product_width,
            "shift": shift,
            "output_width": output_width,
            "overflow": "wrap",
            "recovery_method": "one_hot_coefficient_replay",
        }

    # When every legal shift has its one-hot witness, the direct search is
    # complete for the fixed-product model above.  Falling through to Z3 would
    # only repeat a proven-empty search and is particularly costly while
    # screening many live closure nodes.
    legal_shifts = range(product_width - output_width + 1)
    if all((1 << shift) in observed_by_value for shift in legal_shifts):
        return None

    try:
        import z3
    except ImportError as exc:  # pragma: no cover - environment failure
        raise RuntimeError("z3-solver is required for fixed-point contract recovery") from exc

    coefficient = z3.BitVec("fixed_point_coefficient", coefficient_width)
    candidates = []
    for data_signed in (True, False):
        for coefficient_signed in (False, True):
            for shift in range(product_width - output_width + 1):
                solver = z3.SolverFor("QF_BV")
                solver.set(timeout=5000)
                if coefficient_signed:
                    solver.add(coefficient < z3.BitVecVal(0, coefficient_width))
                else:
                    solver.add(z3.UGT(coefficient, z3.BitVecVal(1, coefficient_width)))
                for value, observed in samples:
                    data = z3.BitVecVal(value, bus["width"])
                    extended_data = (
                        z3.SignExt(coefficient_width, data)
                        if data_signed else z3.ZeroExt(coefficient_width, data)
                    )
                    extended_coefficient = (
                        z3.SignExt(bus["width"], coefficient)
                        if coefficient_signed
                        else z3.ZeroExt(bus["width"], coefficient)
                    )
                    product = extended_data * extended_coefficient
                    sliced = z3.Extract(shift + output_width - 1, shift, product)
                    solver.add(sliced == z3.BitVecVal(observed & mask, output_width))
                if solver.check() != z3.sat:
                    continue
                raw_value = int(
                    solver.model().eval(coefficient, model_completion=True).as_long()
                )
                coefficient_value = (
                    raw_value - (1 << coefficient_width)
                    if coefficient_signed else raw_value
                )
                if (
                    coefficient_signed
                    and not -(1 << (coefficient_width - 1)) <= coefficient_value < 0
                ) or (
                    not coefficient_signed
                    and not 1 < coefficient_value < (1 << coefficient_width)
                ):
                    continue
                if all(
                    observed == _product_value(
                        sample,
                        data_width=bus["width"],
                        data_signed=data_signed,
                        coefficient=coefficient_value,
                        coefficient_width=coefficient_width,
                        shift=shift,
                        output_width=output_width,
                    )
                    for sample, observed in samples
                ):
                    candidates.append((
                        (bus["width"] + DSP_DATA_CHUNK_WIDTH - 1)
                        // DSP_DATA_CHUNK_WIDTH,
                        shift,
                        abs(coefficient_value).bit_length(),
                        abs(coefficient_value),
                        coefficient_signed,
                        coefficient_value,
                        data_signed,
                    ))
    if not candidates:
        return None
    (
        _, shift, _, _, coefficient_signed, coefficient_value, data_signed
    ) = min(candidates)
    return {
        "kind": "fixed_product_slice",
        "source": bus,
        "data_width": bus["width"],
        "data_signed": bool(data_signed),
        "coefficient": coefficient_value,
        "coefficient_width": coefficient_width,
        "coefficient_signed": bool(coefficient_signed),
        "product_width": product_width,
        "shift": shift,
        "output_width": output_width,
        "overflow": "wrap",
    }


def _term_value(term: dict[str, Any], value: int) -> int:
    if term["kind"] == "passthrough":
        return _passthrough_value(
            value,
            term["data_width"],
            term["output_width"],
            term["data_signed"],
            int(term.get("shift", 0)),
        )
    return _product_value(
        value,
        data_width=term["data_width"],
        data_signed=term["data_signed"],
        coefficient=term["coefficient"],
        coefficient_width=term["coefficient_width"],
        shift=term["shift"],
        output_width=term["output_width"],
    )


def _trailing_zero_count(value: int) -> int:
    if value <= 0:
        raise ValueError("trailing-zero count requires a positive integer")
    return (value & -value).bit_length() - 1


def _infer_full_bus_term(
    bus: dict[str, Any],
    samples: list[tuple[int, int]],
    chain_width: int,
) -> dict[str, Any] | None:
    nonzero = [observed for _, observed in samples if observed]
    if not nonzero:
        return None
    destination_lsb = min(_trailing_zero_count(value) for value in nonzero)
    output_width = chain_width - destination_lsb
    normalized = [
        (value, observed >> destination_lsb) for value, observed in samples
    ]
    term = _infer_bus_term(bus, normalized, output_width)
    if term is None:
        return None
    term["destination_lsb"] = destination_lsb
    return term


def _signed_bus_value(term: dict[str, Any], value: int) -> int:
    width = int(term["data_width"])
    value &= (1 << width) - 1
    if term["data_signed"] and value & (1 << (width - 1)):
        value -= 1 << width
    return value


def _full_term_value(
    term: dict[str, Any], value: int, chain_width: int
) -> int:
    return (
        _term_value(term, value) << int(term["destination_lsb"])
    ) & ((1 << chain_width) - 1)


def _group_value(
    group: dict[str, Any],
    terms: list[dict[str, Any]],
    inputs: dict[str, int],
    chain_width: int,
) -> int:
    common_shift = int(group["common_shift"])
    total = 0
    for ordinal in group["term_ordinals"]:
        term = terms[int(ordinal)]
        value = _signed_bus_value(
            term, int(inputs.get(term["source"]["stem"], 0))
        )
        coefficient = int(term.get("coefficient", 1))
        term_shift = int(term.get("shift", 0))
        if term_shift > common_shift:
            raise ValueError("fixed-point group shift precedes a member term")
        total += (value * coefficient) << (common_shift - term_shift)
    output_width = int(group["output_width"])
    destination_lsb = int(group["destination_lsb"])
    normalized = (total >> common_shift) & ((1 << output_width) - 1)
    return (normalized << destination_lsb) & ((1 << chain_width) - 1)


def _interaction_groups(
    table: dict[str, Any], terms: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    chain_width = int(table["source_chain_width"])
    modulus = 1 << chain_width
    zero_rows = [row for row in table["rows"] if not row["inputs"]]
    if len(zero_rows) != 1:
        raise ValueError("fixed-point sample table has no unique full-chain zero row")
    full_constant = int(zero_rows[0]["source_full_output"])
    edges: set[tuple[int, int]] = set()
    for left in range(len(terms)):
        for right in range(left + 1, len(terms)):
            stems = {terms[left]["source"]["stem"], terms[right]["source"]["stem"]}
            pair_rows = [
                row for row in table["rows"] if set(row["inputs"]) == stems
            ]
            if not pair_rows:
                raise ValueError("fixed-point interaction screen has no pair vectors")
            for row in pair_rows:
                predicted = full_constant
                predicted += _full_term_value(
                    terms[left],
                    int(row["inputs"].get(terms[left]["source"]["stem"], 0)),
                    chain_width,
                )
                predicted += _full_term_value(
                    terms[right],
                    int(row["inputs"].get(terms[right]["source"]["stem"], 0)),
                    chain_width,
                )
                if predicted % modulus != int(row["source_full_output"]):
                    edges.add((left, right))
                    break

    remaining = set(range(len(terms)))
    components: list[list[int]] = []
    while remaining:
        component = {min(remaining)}
        changed = True
        while changed:
            changed = False
            for left, right in edges:
                if (left in component) ^ (right in component):
                    component.update((left, right))
                    changed = True
        remaining.difference_update(component)
        components.append(sorted(component))

    groups = []
    for group_index, ordinals in enumerate(components):
        destinations = {int(terms[index]["destination_lsb"]) for index in ordinals}
        widths = {int(terms[index]["output_width"]) for index in ordinals}
        if len(destinations) != 1 or len(widths) != 1:
            raise ValueError(
                "interacting fixed-point terms do not share one quantization boundary"
            )
        groups.append({
            "group_id": f"group_{group_index}",
            "term_ordinals": ordinals,
            "common_shift": max(int(terms[index].get("shift", 0)) for index in ordinals),
            "destination_lsb": next(iter(destinations)),
            "output_width": next(iter(widths)),
            "interaction_screened": len(ordinals) > 1,
        })

    for row in table["rows"]:
        predicted = full_constant + sum(
            _group_value(group, terms, row["inputs"], chain_width)
            for group in groups
        )
        if predicted % modulus != int(row["source_full_output"]):
            raise ValueError(
                "fixed-point quantization groups do not replay current-cone "
                f"samples: groups={groups!r} inputs={row['inputs']!r} "
                f"predicted={predicted % modulus} "
                f"observed={int(row['source_full_output'])}"
            )
    return groups


def _dsp_chunks(term: dict[str, Any], ordinal: int) -> list[dict[str, Any]]:
    chunks = []
    remaining = int(term["data_width"])
    offset = 0
    while remaining:
        width = min(DSP_DATA_CHUNK_WIDTH, remaining)
        chunks.append({
            "chunk_id": f"term{ordinal}_chunk{len(chunks)}",
            "source_lsb": offset,
            "source_width": width,
            "source_signed": bool(term["data_signed"] and remaining <= DSP_DATA_CHUNK_WIDTH),
            "product_signed": bool(
                term.get("coefficient_signed")
                or (term["data_signed"] and remaining <= DSP_DATA_CHUNK_WIDTH)
            ),
            "product_lsb": offset,
            "dsp_primitive": "DSP48E2",
            "areg": 0,
            "breg": 0,
            "mreg": 0,
            "preg": 0,
        })
        offset += width
        remaining -= width
    return chunks


def _reduction_cost(row_count: int, width: int) -> dict[str, int]:
    """Exact Wallace/CSA cell and serial-carry cost for one reduction."""
    rows = max(0, int(row_count))
    bits = max(0, int(width))
    compressor_count = 0
    compressor_stages = 0
    frontier = rows
    while frontier > 2:
        triples, remainder = divmod(frontier, 3)
        compressor_count += triples
        frontier = triples * 2 + remainder
        compressor_stages += 1
    final_adder_count = 1 if frontier == 2 else 0
    carry8_per_adder = (bits + 7) // 8 if bits else 0
    return {
        "row_count": rows,
        "width": bits,
        "csa_compressor_count": compressor_count,
        "csa_lut6_2_count": compressor_count * bits,
        "csa_stages": compressor_stages,
        "adder_count": final_adder_count,
        "lut2_count": final_adder_count * bits,
        "carry8_count": final_adder_count * carry8_per_adder,
        "adder_stages": final_adder_count,
        "serial_carry8_levels": final_adder_count * carry8_per_adder,
    }


def _emitted_topology_cost(
    contract: dict[str, Any], product_terms: list[dict[str, Any]]
) -> dict[str, Any]:
    """Preview the registered backend's emitted primitive topology.

    This is an implementation fact, not a QoR threshold.  It makes an
    otherwise hidden external LUT/CARRY accumulator and the lack of DSP
    pipeline registers visible before Luna spends a mutation/route cycle.
    """
    reductions = []
    product_by_ordinal = {
        int(item["term_ordinal"]): item for item in product_terms
    }

    group_depths = []
    group_carry_levels = []
    group_csa_levels = []
    for group_index, group in enumerate(contract.get("accumulation_groups") or []):
        ordinals = [int(value) for value in group.get("term_ordinals") or []]
        width = int(group["common_shift"]) + int(group["output_width"])
        expanded_row_count = 0
        for ordinal in ordinals:
            term = (contract.get("terms") or [])[ordinal]
            if term.get("kind") == "fixed_product_slice":
                expanded_row_count += len(
                    product_by_ordinal.get(ordinal, {}).get("chunks") or []
                )
            else:
                expanded_row_count += 1
        cost = _reduction_cost(expanded_row_count, width)
        reductions.append({"role": f"quantization_group_{group_index}", **cost})
        group_depths.append(cost["adder_stages"])
        group_carry_levels.append(cost["serial_carry8_levels"])
        group_csa_levels.append(cost["csa_stages"])

    full_row_count = len(contract.get("accumulation_groups") or [])
    if int(contract.get("source_full_constant") or 0):
        full_row_count += 1
    full_cost = _reduction_cost(
        full_row_count, int(contract.get("source_chain_width") or 0)
    )
    reductions.append({"role": "full_chain", **full_cost})
    external_luts = sum(item["lut2_count"] for item in reductions)
    external_csa_luts = sum(item["csa_lut6_2_count"] for item in reductions)
    external_carries = sum(item["carry8_count"] for item in reductions)
    dsp_count = sum(len(item.get("chunks") or []) for item in product_terms)
    deepest_external_stages = (
        max(group_depths, default=0) + full_cost["adder_stages"]
    )
    deepest_external_carry_levels = (
        max(group_carry_levels, default=0)
        + full_cost["serial_carry8_levels"]
    )
    deepest_csa_levels = (
        max(group_csa_levels, default=0) + full_cost["csa_stages"]
    )
    return {
        "backend": "combinational_dsp_plus_flattened_csa_wallace_accumulator",
        "dsp48e2_count": dsp_count,
        "dsp_internal_pipeline_register_count": 0,
        "external_lut2_count": external_luts,
        "external_csa_lut6_2_count": external_csa_luts,
        "external_carry8_count": external_carries,
        "constant_driver_cell_count": 1,
        "estimated_new_cell_count": (
            dsp_count + external_luts + external_csa_luts
            + external_carries + 1
        ),
        "estimated_external_adder_stages_on_deepest_timing_chain": deepest_external_stages,
        "estimated_external_carry8_levels_on_deepest_timing_chain": deepest_external_carry_levels,
        "estimated_csa_lut_levels_on_deepest_timing_chain": deepest_csa_levels,
        "product_chunk_rows_are_flattened_into_quantization_groups": True,
        "uses_external_fabric_accumulation": bool(
            external_luts or external_csa_luts or external_carries
        ),
        "clocked_new_cell_count": 0,
        "clock_footprint_delta_known": True,
        "reduction_breakdown": reductions,
        "interpretation": (
            "topology cost is exact for the flattened CSA emitter shape but is not "
            "a routed timing prediction or an action trigger"
        ),
    }


def build_topology_manifest(contract: dict[str, Any]) -> dict[str, Any]:
    product_terms = []
    for ordinal, term in enumerate(contract.get("terms") or []):
        if term.get("kind") != "fixed_product_slice":
            continue
        product_terms.append({
            "term_ordinal": ordinal,
            "coefficient": term["coefficient"],
            "coefficient_width": term["coefficient_width"],
            "coefficient_signed": term["coefficient_signed"],
            "product_width": term["product_width"],
            "shift": term["shift"],
            "output_width": term["output_width"],
            "chunks": _dsp_chunks(term, ordinal),
        })
    manifest = {
        "kind": "fixed_point_dsp_topology_manifest",
        "version": TOPOLOGY_VERSION,
        "output_width": contract["output_width"],
        "constant": contract["constant"],
        "product_terms": product_terms,
        "passthrough_terms": [
            {
                "term_ordinal": index,
                "data_width": term["data_width"],
                "data_signed": term["data_signed"],
                "shift": int(term.get("shift", 0)),
                "destination_lsb": int(term.get("destination_lsb", 0)),
                "output_width": term["output_width"],
            }
            for index, term in enumerate(contract.get("terms") or [])
            if term.get("kind") == "passthrough"
        ],
        "source_chain_width": contract.get("source_chain_width"),
        "source_chain_target_lsb": contract.get("source_chain_target_lsb"),
        "source_full_constant": contract.get("source_full_constant"),
        "accumulation_groups": contract.get("accumulation_groups"),
        "accumulation": "interaction_screened_quantization_groups_then_modular_add",
        "target_registers_preserved": True,
        "internal_pipeline_registers": 0,
        "emitted_topology_cost": _emitted_topology_cost(
            contract, product_terms
        ),
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def verify_topology_math(contract: dict[str, Any], *, timeout_ms: int = 5000) -> bool:
    try:
        import z3
    except ImportError:  # pragma: no cover - environment failure
        return False
    try:
        terms = contract["terms"]
        groups = contract["accumulation_groups"]
        chain_width = int(contract["source_chain_width"])
        target_lsb = int(contract["source_chain_target_lsb"])
        output_width = int(contract["output_width"])
        if (
            chain_width <= 0
            or target_lsb < 0
            or target_lsb + output_width > chain_width
            or not isinstance(groups, list)
            or not groups
        ):
            return False
        covered = []
        for group in groups:
            ordinals = list(group["term_ordinals"])
            if not ordinals or len(ordinals) != len(set(ordinals)):
                return False
            covered.extend(ordinals)
            common_shift = int(group["common_shift"])
            destination_lsb = int(group["destination_lsb"])
            group_output_width = int(group["output_width"])
            if (
                destination_lsb < 0
                or group_output_width <= 0
                or destination_lsb + group_output_width > chain_width
            ):
                return False
            for ordinal in ordinals:
                term = terms[int(ordinal)]
                if (
                    int(term.get("shift", 0)) > common_shift
                    or int(term["destination_lsb"]) != destination_lsb
                    or int(term["output_width"]) != group_output_width
                ):
                    return False
        if sorted(int(value) for value in covered) != list(range(len(terms))):
            return False
        for ordinal, term in enumerate(terms):
            if term["kind"] == "passthrough":
                width = int(term["data_width"])
                output_width = int(term["output_width"])
                shift = int(term.get("shift", 0))
                source = term.get("source") or {}
                if (
                    width < 2
                    or output_width <= 0
                    or shift < 0
                    or (output_width <= width and shift + output_width > width)
                    or (output_width > width and shift != 0)
                    or len(source.get("bits") or []) != width
                ):
                    return False
                continue
            if term["kind"] != "fixed_product_slice":
                return False
            width = int(term["data_width"])
            product_width = int(term["product_width"])
            coefficient_width = int(term["coefficient_width"])
            coefficient = int(term["coefficient"])
            coefficient_signed = term.get("coefficient_signed")
            output_width = int(term["output_width"])
            shift = int(term["shift"])
            if (
                width < 2
                or coefficient_width != MAX_DSP_COEFFICIENT_WIDTH
                or product_width != width + coefficient_width
                or coefficient_signed not in (False, True)
                or (
                    coefficient_signed is False
                    and not 1 < coefficient < (1 << MAX_DSP_COEFFICIENT_WIDTH)
                )
                or (
                    coefficient_signed is True
                    and not -(1 << (MAX_DSP_COEFFICIENT_WIDTH - 1)) <= coefficient < -1
                )
                or term.get("overflow") != "wrap"
                or output_width <= 0
                or shift < 0
                or shift + output_width > product_width
            ):
                return False
            data = z3.BitVec(f"fixed_point_data_{ordinal}", width)
            reference_operand = (
                z3.SignExt(product_width - width, data)
                if term["data_signed"] else z3.ZeroExt(product_width - width, data)
            )
            rebuilt_operand = z3.BitVecVal(0, product_width)
            for chunk in _dsp_chunks(term, ordinal):
                lsb = chunk["source_lsb"]
                chunk_width = chunk["source_width"]
                raw = z3.Extract(lsb + chunk_width - 1, lsb, data)
                operand = (
                    z3.SignExt(product_width - chunk_width, raw)
                    if chunk["source_signed"] else z3.ZeroExt(product_width - chunk_width, raw)
                )
                rebuilt_operand = rebuilt_operand + (operand << lsb)
            solver = z3.SolverFor("QF_BV")
            solver.set(timeout=timeout_ms)
            # Prove the width/sign-aware operand partition.  Multiplying both
            # equal operands by the same constant preserves equality modulo
            # product_width, so there is no need to bit-blast a much harder
            # variable-by-constant multiplication identity here.
            solver.add(reference_operand != rebuilt_operand)
            if solver.check() != z3.unsat:
                return False
        return True
    except (KeyError, TypeError, ValueError, z3.Z3Exception):
        return False


def build_fixed_point_contract(root: Path | str) -> dict[str, Any]:
    graph = ConeGraph.load(root)
    if graph.side_fanout:
        raise ValueError("fixed-point cone has side fanout outside the replacement region")
    buses = _boundary_buses(graph)
    table = _collect_sample_table(graph, buses)
    output_width = int(table["output_width"])
    chain_width = int(table["source_chain_width"])
    modulus = 1 << chain_width
    output_mask = (1 << output_width) - 1
    baseline_rows = [row for row in table["rows"] if not row["inputs"]]
    if len(baseline_rows) != 1:
        raise ValueError("fixed-point sample table has no unique zero-input row")
    constant = int(baseline_rows[0]["output"]) & output_mask
    full_constant = int(baseline_rows[0]["source_full_output"]) % modulus
    terms = []
    for bus in buses:
        samples = []
        for row in table["rows"]:
            if set(row["inputs"]) <= {bus["stem"]}:
                value = int(row["inputs"].get(bus["stem"], 0))
                contribution = (
                    int(row["source_full_output"]) - full_constant
                ) % modulus
                samples.append((value, contribution))
        term = _infer_full_bus_term(bus, samples, chain_width)
        if term is None:
            raise ValueError(
                "register bus does not match a supported full-chain fixed-point term"
            )
        terms.append(term)
    if not any(term["kind"] == "fixed_product_slice" for term in terms):
        raise ValueError("cone contains no DSP-remappable fixed-point product")
    accumulation_groups = _interaction_groups(table, terms)

    identity = {
        "dcp_sha256": graph.manifest["dcp_sha256"],
        "design": graph.manifest["design"],
        "part": graph.manifest["part"],
        "target_stem": graph.manifest["target_stem"],
        "connectivity_sha256": graph.connectivity_digest(),
    }
    contract: dict[str, Any] = {
        "kind": "register_cone_fixed_point_contract",
        "version": CONTRACT_VERSION,
        "status": "screened_for_measurement_probe",
        "authority": "pre_mutation_measurement_probe_only",
        "source_identity": identity,
        "output_width": output_width,
        "constant": constant,
        "source_chain_width": chain_width,
        "source_chain_target_lsb": int(table["source_chain_target_lsb"]),
        "source_full_constant": full_constant,
        "terms": terms,
        "accumulation_groups": accumulation_groups,
        "target_register_contract_sha256": _digest(graph.targets),
        "side_fanout_count": 0,
        "source_topology_cost": source_topology_summary(graph),
        "screen": {
            "method": "bit_parallel_current_cone_deterministic_edge_power_and_seeded_cross_bus_vectors",
            "vector_count": len(table["rows"]),
            "sample_digest": table["sample_digest"],
            "source_chain_target_lsb": table["source_chain_target_lsb"],
            "source_chain_width": chain_width,
            "interaction_group_count": sum(
                len(group["term_ordinals"]) > 1
                for group in accumulation_groups
            ),
            "mismatch_count": 0,
            "formal_source_equivalence": False,
            "whole_dcp_equivalence_required_before_submission": True,
        },
        "mutation_ready_for_measurement_probe": True,
    }
    contract["topology_manifest"] = build_topology_manifest(contract)
    contract["topology_math_proved"] = verify_topology_math(contract)
    if not contract["topology_math_proved"]:
        raise ValueError("fixed-point DSP chunk topology proof failed")
    contract["certificate_id"] = _digest(contract)
    return contract


def verify_fixed_point_contract(
    contract: dict[str, Any], root: Path | str, *, replay_samples: bool = True
) -> bool:
    try:
        if (
            not isinstance(contract, dict)
            or contract.get("kind") != "register_cone_fixed_point_contract"
            or contract.get("version") != CONTRACT_VERSION
            or contract.get("status") != "screened_for_measurement_probe"
            or contract.get("authority") != "pre_mutation_measurement_probe_only"
            or contract.get("mutation_ready_for_measurement_probe") is not True
            or contract.get("topology_math_proved") is not True
        ):
            return False
        claimed = contract.get("certificate_id")
        payload = dict(contract)
        payload.pop("certificate_id", None)
        if not isinstance(claimed, str) or _digest(payload) != claimed:
            return False
        graph = ConeGraph.load(root)
        identity = contract.get("source_identity") or {}
        if (
            identity.get("dcp_sha256") != graph.manifest.get("dcp_sha256")
            or identity.get("target_stem") != graph.manifest.get("target_stem")
            or identity.get("connectivity_sha256") != graph.connectivity_digest()
            or graph.side_fanout
            or build_topology_manifest(contract) != contract.get("topology_manifest")
            or not verify_topology_math(contract)
        ):
            return False
        if replay_samples:
            replay = build_fixed_point_contract(root)
            return replay == contract
        return True
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return False


def contract_summary(contract: dict[str, Any]) -> dict[str, Any]:
    terms = list(contract.get("terms") or [])
    topology = contract.get("topology_manifest") or {}
    return {
        "status": contract.get("status"),
        "certificate_id": contract.get("certificate_id"),
        "output_width": contract.get("output_width"),
        "term_count": len(terms),
        "fixed_product_count": sum(term.get("kind") == "fixed_product_slice" for term in terms),
        "passthrough_count": sum(term.get("kind") == "passthrough" for term in terms),
        "dsp48e2_count": sum(
            len(item.get("chunks") or []) for item in topology.get("product_terms") or []
        ),
        "sample_vector_count": (contract.get("screen") or {}).get("vector_count"),
        "quantization_group_count": len(contract.get("accumulation_groups") or []),
        "interacting_group_count": sum(
            len(group.get("term_ordinals") or []) > 1
            for group in contract.get("accumulation_groups") or []
        ),
        "emitted_topology_cost": topology.get("emitted_topology_cost") or {},
        "source_topology_cost": contract.get("source_topology_cost") or {},
        "formal_source_equivalence": False,
        "final_equivalence_required": True,
    }


def source_topology_summary(graph: ConeGraph) -> dict[str, Any]:
    """Return source topology in units directly comparable with the emitter."""
    removable = sorted(
        _downstream_cells(graph, {*graph.boundary_nets, *graph.static_nets})
    )
    source_types: dict[str, int] = {}
    for name in removable:
        cell = graph.cells.get(name)
        cell_type = str(cell.ref if cell is not None else "UNKNOWN")
        source_types[cell_type] = source_types.get(cell_type, 0) + 1
    try:
        chain = _carry_chain(graph)
    except ValueError:
        chain = None
    return {
        "removable_cell_count": len(removable),
        "removable_primitive_type_counts": dict(sorted(source_types.items())),
        "source_carry8_count": int(source_types.get("CARRY8", 0)),
        "source_primary_carry_chain_carry8_levels": (
            len(chain.get("cells") or []) if chain is not None else None
        ),
        "source_chain_width": (
            int(chain.get("width") or 0) if chain is not None else len(graph.targets)
        ),
    }


__all__ = [
    "CONTRACT_VERSION",
    "build_fixed_point_contract",
    "build_topology_manifest",
    "contract_summary",
    "source_topology_summary",
    "verify_fixed_point_contract",
    "verify_topology_math",
]
