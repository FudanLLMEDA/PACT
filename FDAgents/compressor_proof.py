"""Proof tools for register-boundary carry-compressor rewrites.

The module consumes facts emitted by ``scripts/extract_register_cone.tcl``.
It does not mutate a design.  A rewrite is considered ready only when the
complete final CARRY8 chain is proved equivalent to a modular weighted sum of
the extracted register-boundary bits.
"""

from __future__ import annotations

import csv
from collections import deque
import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
CERTIFICATE_VERSION = 6
_VECTOR_PIN_RE = re.compile(r"^(?P<name>[A-Z_]+)\[(?P<index>\d+)\]$")
_LUT_RE = re.compile(r"^LUT(?P<size>[1-6])$")


class CompressorProofTimeout(TimeoutError):
    """The complete proof budget expired before mutation authority existed."""

    def __init__(self, stage: str, elapsed_s: float, budget_ms: int):
        self.stage = str(stage)
        self.elapsed_s = float(elapsed_s)
        self.budget_ms = int(budget_ms)
        super().__init__(
            f"compressor proof total deadline exceeded at {self.stage} "
            f"after {self.elapsed_s:.3f}s (budget={self.budget_ms}ms)"
        )


def _require_proof_budget(
    deadline_monotonic: float | None,
    *,
    started_monotonic: float,
    budget_ms: int,
    stage: str,
) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise CompressorProofTimeout(
            stage,
            time.monotonic() - started_monotonic,
            budget_ms,
        )


def _remaining_solver_timeout_ms(
    deadline_monotonic: float,
    *,
    started_monotonic: float,
    budget_ms: int,
    stage: str,
) -> int:
    _require_proof_budget(
        deadline_monotonic,
        started_monotonic=started_monotonic,
        budget_ms=budget_ms,
        stage=stage,
    )
    remaining = int((deadline_monotonic - time.monotonic()) * 1000)
    return max(1, min(int(budget_ms), remaining))

_FINAL_ADDER_SPEC = {
    "kind": "LUT6_2_CSA3_PLUS_LUT2_XOR_DI_PLUS_CARRY8_SINGLE_CY8",
    "compressor_primitive": "LUT6_2",
    "compressor_init": "64'h96969696E8E8E8E8",
    "compressor_sum_output": "O6",
    "compressor_carry_output": "O5",
    "final_xor_primitive": "LUT2",
    "final_xor_init": "4'h6",
    "carry_primitive": "CARRY8",
    "carry_type": "SINGLE_CY8",
    "s_source": "LUT2.O",
    "di_source": "CSA_SUM",
    "shifted_operand": "CSA_CARRY_PREVIOUS_COLUMN",
    "initial_ci": 0,
    "ci_top": 0,
    "sum_output": "O",
    "carry_output": "CO",
}


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


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _parse_init(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?i)(\d+)'([hbd])([0-9a-f_xz]+)", value.strip())
    if match is None:
        raise ValueError(f"unsupported INIT literal {value!r}")
    width = int(match.group(1))
    radix = {"h": 16, "b": 2, "d": 10}[match.group(2).lower()]
    digits = match.group(3).replace("_", "").lower()
    if "x" in digits or "z" in digits:
        raise ValueError(f"unknown bit in INIT literal {value!r}")
    return width, int(digits, radix)


@dataclass(frozen=True)
class Cell:
    name: str
    ref: str
    init: str
    carry_type: str
    loc: str
    bel: str


@dataclass(frozen=True)
class Pin:
    cell: str
    name: str
    direction: str
    net: str


@dataclass
class ConeGraph:
    root: Path
    manifest: dict[str, str]
    cells: dict[str, Cell]
    pins: dict[str, dict[str, Pin]]
    boundary_nets: dict[str, str]
    static_nets: dict[str, bool]
    targets: list[dict[str, str]]
    side_fanout: list[dict[str, str]]

    @classmethod
    def load(cls, root: Path | str) -> "ConeGraph":
        root = Path(root)
        manifest = {row["key"]: row["value"] for row in _read_tsv(root / "manifest.tsv")}
        if int(manifest.get("schema_version", -1)) not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError("register-cone schema version mismatch")
        cells = {
            row["cell"]: Cell(
                name=row["cell"],
                ref=row["ref"].upper(),
                init=row["init"],
                carry_type=row["carry_type"].upper(),
                loc=row["loc"],
                bel=row["bel"],
            )
            for row in _read_tsv(root / "cells.tsv")
        }
        pins: dict[str, dict[str, Pin]] = {name: {} for name in cells}
        for row in _read_tsv(root / "pins.tsv"):
            if row["cell"] not in cells:
                raise ValueError(f"pin references unknown cone cell {row['cell']}")
            pin = Pin(row["cell"], row["ref_pin"], row["direction"], row["net"])
            pins[row["cell"]][row["ref_pin"]] = pin
        boundary_nets = {}
        for row in _read_tsv(root / "boundary.tsv"):
            graph_q_net = row.get("graph_q_net") or row["q_net"]
            if not graph_q_net:
                raise ValueError(f"boundary FF {row['cell']} has no Q net")
            if graph_q_net in boundary_nets:
                raise ValueError(
                    f"boundary net {graph_q_net} has multiple FF sources"
                )
            boundary_nets[graph_q_net] = row["cell"]
        targets = sorted(
            _read_tsv(root / "targets.tsv"), key=lambda row: int(row["index"])
        )
        static_nets = {
            row["net"]: row["value"] == "1"
            for row in _read_tsv(root / "static_nets.tsv")
        }
        target_indices = [int(row["index"]) for row in targets]
        if not target_indices:
            raise ValueError("target register bus is empty")
        expected = list(
            range(target_indices[0], target_indices[0] + len(target_indices))
        )
        if target_indices != expected:
            raise ValueError("target register indices are not contiguous")
        # Intermediate HLS words are commonly retained as slices such as
        # word[5:50].  Connectivity and arithmetic recovery operate on the
        # ordered slice, so normalize only the local ordinal while retaining
        # the DCP bit identity for audit and later wiring.
        target_index_lsb = target_indices[0]
        targets = [
            {
                **row,
                "source_index": str(index),
                "index": str(index - target_index_lsb),
            }
            for row, index in zip(targets, target_indices)
        ]
        return cls(
            root=root,
            manifest=manifest,
            cells=cells,
            pins=pins,
            boundary_nets=boundary_nets,
            static_nets=static_nets,
            targets=targets,
            side_fanout=_read_tsv(root / "side_fanout.tsv"),
        )

    def connectivity_digest(self) -> str:
        return _digest({
            "cells": [self.cells[name].__dict__ for name in sorted(self.cells)],
            "pins": [
                self.pins[cell][pin].__dict__
                for cell in sorted(self.pins)
                for pin in sorted(self.pins[cell])
            ],
            "boundary_nets": self.boundary_nets,
            "static_nets": self.static_nets,
            "targets": self.targets,
            "side_fanout": self.side_fanout,
        })

    def net_driver(self) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        for cell, pins in self.pins.items():
            for pin in pins.values():
                if pin.direction != "OUT" or not pin.net:
                    continue
                if pin.net in result:
                    raise ValueError(f"cone net {pin.net} has multiple drivers")
                result[pin.net] = (cell, pin.name)
        return result


def _pin_index(name: str, base: str) -> int | None:
    match = _VECTOR_PIN_RE.fullmatch(name)
    if match is None or match.group("name") != base:
        return None
    return int(match.group("index"))


def _cell_input_nets(graph: ConeGraph, cell: Cell) -> list[str]:
    return [
        pin.net
        for pin in graph.pins[cell.name].values()
        if pin.direction == "IN" and pin.net
    ]


def _evaluation_order(
    graph: ConeGraph, protected_nets: set[str]
) -> tuple[str, ...]:
    """Return a cached dependency order for one exact boundary view.

    The old evaluator repeatedly scanned every pending cell until another
    logic level became ready.  Registered semantic replay invokes that path
    hundreds of times on cones with thousands of cells, making an otherwise
    linear Boolean pass dominate the hour.  A Kahn order preserves the exact
    primitive semantics while making each evaluation one pass.
    """
    cache = getattr(graph, "_fdagents_evaluation_order_cache", None)
    if cache is None:
        cache = {}
        setattr(graph, "_fdagents_evaluation_order_cache", cache)
    key = frozenset(protected_nets)
    cached = cache.get(key)
    if cached is not None:
        return cached

    drivers = graph.net_driver()
    dependencies: dict[str, set[str]] = {}
    consumers: dict[str, list[str]] = {name: [] for name in graph.cells}
    for name, cell in graph.cells.items():
        required: set[str] = set()
        for net in _cell_input_nets(graph, cell):
            if net in protected_nets or net in graph.static_nets:
                continue
            source = drivers.get(net)
            if source is None:
                raise ValueError(
                    f"cone has an unextracted input net for {name}: {net}"
                )
            producer = source[0]
            if producer != name:
                required.add(producer)
        dependencies[name] = required
        for producer in required:
            consumers.setdefault(producer, []).append(name)

    ready = deque(
        name for name in graph.cells if not dependencies.get(name)
    )
    ordered: list[str] = []
    while ready:
        name = ready.popleft()
        ordered.append(name)
        for consumer in consumers.get(name, []):
            remaining = dependencies[consumer]
            remaining.discard(name)
            if not remaining:
                ready.append(consumer)
    if len(ordered) != len(graph.cells):
        unresolved = [
            name for name in graph.cells if dependencies.get(name)
        ][:8]
        raise ValueError(
            f"cone is cyclic or has unresolved dependencies: {unresolved}"
        )
    result = tuple(ordered)
    cache[key] = result
    return result


def _evaluate(
    graph: ConeGraph,
    boundary: dict[str, Any],
    *,
    xor: Callable[[Any, Any], Any],
    choose: Callable[[Any, Any, Any], Any],
    invert: Callable[[Any], Any],
    static_value: Callable[[bool], Any] = lambda value: value,
    deadline_check: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], dict[tuple[str, str], Any]]:
    nets = {
        **{net: static_value(bool(value)) for net, value in graph.static_nets.items()},
        **boundary,
    }
    protected_nets = set(boundary)
    outputs: dict[tuple[str, str], Any] = {}
    pending = {
        name: graph.cells[name]
        for name in _evaluation_order(graph, protected_nets)
    }
    while pending:
        if deadline_check is not None:
            deadline_check("graph_evaluation")
        progressed = False
        for name, cell in list(pending.items()):
            if deadline_check is not None:
                deadline_check("graph_evaluation")
            required = _cell_input_nets(graph, cell)
            if any(net not in nets for net in required):
                continue
            pin_map = graph.pins[name]
            produced: dict[str, Any]
            match = _LUT_RE.fullmatch(cell.ref)
            if match is not None:
                size = int(match.group("size"))
                width, init = _parse_init(cell.init)
                if width != 1 << size:
                    raise ValueError(f"{name} INIT width does not match {cell.ref}")
                terms = [nets[pin_map[f"I{i}"].net] for i in range(size)]

                def table_value(inputs: list[Any], table: int) -> Any:
                    values: list[Any] = [
                        static_value(bool((table >> address) & 1))
                        for address in range(1 << len(inputs))
                    ]
                    for term in inputs:
                        values = [
                            choose(term, values[index + 1], values[index])
                            for index in range(0, len(values), 2)
                        ]
                    return values[0]

                produced = {"O": table_value(terms, init)}
            elif cell.ref == "LUT6_2":
                width, init = _parse_init(cell.init)
                if width != 64:
                    raise ValueError(f"{name} INIT width does not match LUT6_2")
                terms = [nets[pin_map[f"I{i}"].net] for i in range(6)]

                def lut_value(size: int, table: int) -> Any:
                    values: list[Any] = [
                        static_value(bool((table >> address) & 1))
                        for address in range(1 << size)
                    ]
                    for term in terms[:size]:
                        values = [
                            choose(term, values[index + 1], values[index])
                            for index in range(0, len(values), 2)
                        ]
                    return values[0]

                produced = {
                    "O5": lut_value(5, init & ((1 << 32) - 1)),
                    "O6": lut_value(6, init),
                }
            elif cell.ref in {"GND", "VCC"}:
                produced = {
                    "G" if cell.ref == "GND" else "P": static_value(
                        cell.ref == "VCC"
                    )
                }
            elif cell.ref in {"MUXF7", "MUXF8", "MUXF9"}:
                produced = {
                    "O": choose(
                        nets[pin_map["S"].net],
                        nets[pin_map["I1"].net],
                        nets[pin_map["I0"].net],
                    )
                }
            elif cell.ref == "CARRY8":
                # Vivado leaves physically unused upper lanes open. They cannot
                # influence any extracted sink; model their primitive tie-off as 0.
                di = [
                    nets[pin_map[f"DI[{i}]"].net]
                    if pin_map[f"DI[{i}]"].net else static_value(False)
                    for i in range(8)
                ]
                s = [
                    nets[pin_map[f"S[{i}]"].net]
                    if pin_map[f"S[{i}]"].net else static_value(False)
                    for i in range(8)
                ]
                ci = nets[pin_map["CI"].net]
                ci_top = nets[pin_map["CI_TOP"].net]
                produced = {}
                carry = ci
                for index in range(8):
                    if index == 4 and cell.carry_type == "DUAL_CY4":
                        carry = ci_top
                    produced[f"O[{index}]"] = xor(s[index], carry)
                    carry = choose(s[index], carry, di[index])
                    produced[f"CO[{index}]"] = carry
            else:
                raise ValueError(f"unsupported cone primitive {cell.ref} ({name})")

            for pin_name, value in produced.items():
                outputs[(name, pin_name)] = value
                pin = pin_map.get(pin_name)
                if pin is not None and pin.net and pin.net not in protected_nets:
                    nets[pin.net] = value
            pending.pop(name)
            progressed = True
        if not progressed:
            missing = {
                name: sorted(net for net in _cell_input_nets(graph, cell) if net not in nets)
                for name, cell in list(pending.items())[:8]
            }
            raise ValueError(f"cone is cyclic or has unextracted inputs: {missing}")
    return nets, outputs


def evaluate_concrete(
    graph: ConeGraph,
    boundary_values: dict[str, bool],
    *,
    deadline_check: Callable[[str], None] | None = None,
) -> tuple[dict[str, bool], dict[tuple[str, str], bool]]:
    missing = set(graph.boundary_nets) - set(boundary_values)
    if missing:
        raise ValueError(f"missing boundary values: {sorted(missing)[:8]}")
    return _evaluate(
        graph,
        boundary_values,
        xor=lambda left, right: bool(left) ^ bool(right),
        choose=lambda condition, yes, no: yes if bool(condition) else no,
        invert=lambda value: not bool(value),
        deadline_check=deadline_check,
    )


def _evaluate_boolean_batch(
    graph: ConeGraph,
    input_nets: list[str],
    assignments: list[tuple[str, ...]],
    chain: dict[str, Any],
    *,
    deadline_check: Callable[[str], None],
) -> list[int]:
    """Evaluate many sparse Boolean assignments in one bit-parallel graph pass."""
    if not assignments:
        return []
    scenario_count = len(assignments)
    mask = (1 << scenario_count) - 1
    packed = {net: 0 for net in input_nets}
    for scenario, true_nets in enumerate(assignments):
        deadline_check("batch_assignment_pack")
        bit = 1 << scenario
        for net in true_nets:
            if net not in packed:
                raise ValueError(f"batch assignment names unknown input net {net}")
            packed[net] |= bit
    _, outputs = _evaluate(
        graph,
        packed,
        xor=lambda left, right: left ^ right,
        choose=lambda condition, yes, no: (condition & yes) | ((~condition) & no),
        invert=lambda value: (~value) & mask,
        static_value=lambda value: mask if value else 0,
        deadline_check=deadline_check,
    )
    values = [0] * scenario_count
    for offset, cell in enumerate(chain["cells"]):
        for index in range(8):
            position = offset * 8 + index
            if position >= chain["width"]:
                continue
            packed_output = int(outputs[(cell, f"O[{index}]")])
            for scenario in range(scenario_count):
                if (packed_output >> scenario) & 1:
                    values[scenario] |= 1 << position
        deadline_check("batch_output_unpack")
    return values


def _evaluate_target_boolean_batch(
    graph: ConeGraph,
    input_nets: list[str],
    assignments: list[tuple[str, ...]],
    *,
    deadline_check: Callable[[str], None],
) -> list[int]:
    """Evaluate an arbitrary ordered registered D slice bit-parallel.

    Unlike ``_evaluate_boolean_batch`` this does not assume that every target
    bit is a direct output of one final CARRY8 chain.  Multi-stage operator
    recovery encounters product, truncation, gate and partial-word registers
    whose D buses are still exact Boolean functions but have a mixed LUT/carry
    output topology.
    """
    if not assignments:
        return []
    scenario_count = len(assignments)
    mask = (1 << scenario_count) - 1
    packed = {net: 0 for net in input_nets}
    for scenario, true_nets in enumerate(assignments):
        deadline_check("batch_assignment_pack")
        bit = 1 << scenario
        for net in true_nets:
            if net not in packed:
                raise ValueError(f"batch assignment names unknown input net {net}")
            packed[net] |= bit
    nets, _ = _evaluate(
        graph,
        packed,
        xor=lambda left, right: left ^ right,
        choose=lambda condition, yes, no: (condition & yes) | ((~condition) & no),
        invert=lambda value: (~value) & mask,
        static_value=lambda value: mask if value else 0,
        deadline_check=deadline_check,
    )
    values = [0] * scenario_count
    for position, target in enumerate(graph.targets):
        net = target["d_net"]
        if net not in nets:
            raise ValueError(f"target D net is absent from evaluated cone: {net}")
        packed_output = int(nets[net])
        for scenario in range(scenario_count):
            if (packed_output >> scenario) & 1:
                values[scenario] |= 1 << position
        deadline_check("batch_target_output_unpack")
    return values


def _carry_chain(graph: ConeGraph) -> dict[str, Any]:
    driver = graph.net_driver()
    carries = {name for name, cell in graph.cells.items() if cell.ref == "CARRY8"}
    predecessor: dict[str, str] = {}
    successor: dict[str, str] = {}
    for name in carries:
        ci = graph.pins[name].get("CI")
        source = driver.get(ci.net if ci else "")
        if source and source[0] in carries and source[1] == "CO[7]":
            predecessor[name] = source[0]
            successor[source[0]] = name

    target_drivers = {
        driver.get(row["d_net"])
        for row in graph.targets
        if driver.get(row["d_net"]) is not None
    }
    scores: list[tuple[int, str]] = []
    for name in carries:
        score = sum(1 for source in target_drivers if source and source[0] == name)
        if score:
            scores.append((score, name))
    if not scores:
        raise ValueError("no CARRY8 output drives the target register bus")
    current = max(scores)[1]
    while current in predecessor:
        current = predecessor[current]
    chain = []
    while current:
        chain.append(current)
        current = successor.get(current, "")
    positions = {
        (cell, f"O[{index}]"): offset * 8 + index
        for offset, cell in enumerate(chain)
        for index in range(8)
    }
    target_positions = []
    for row in graph.targets:
        source = driver.get(row["d_net"])
        if source not in positions:
            raise ValueError(f"target {row['cell']} is not driven by the selected final carry chain")
        target_positions.append(positions[source])
    expected = list(range(target_positions[0], target_positions[0] + len(target_positions)))
    if target_positions != expected:
        raise ValueError("target register bus is not a contiguous final-carry slice")
    used_width = target_positions[-1] + 1
    return {
        "cells": chain,
        "width": used_width,
        "physical_capacity": len(chain) * 8,
        "target_lsb": target_positions[0],
        "target_positions": target_positions,
    }


def _chain_integer(outputs: dict[tuple[str, str], Any], chain: dict[str, Any]) -> int:
    value = 0
    for offset, cell in enumerate(chain["cells"]):
        for index in range(8):
            position = offset * 8 + index
            if position < chain["width"]:
                value |= int(bool(outputs[(cell, f"O[{index}]")])) << position
    return value


def _compressor_schedule(
    width: int,
    constant: int,
    coefficients: dict[str, int],
    source_priorities: dict[str, int] | None = None,
    *,
    deadline_check: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    modulus = 1 << width
    signed_terms: dict[int, list[tuple[str, int]]] = {1: [], -1: []}
    for source, coefficient in sorted(coefficients.items()):
        if deadline_check is not None:
            deadline_check("compressor_schedule")
        signed = coefficient if coefficient < (modulus >> 1) else coefficient - modulus
        sign = -1 if signed < 0 else 1
        value = abs(signed)
        column = 0
        while value:
            if value & 1:
                digit = 2 - (value & 3)
                signed_terms[sign * digit].append((source, column))
                value -= digit
            value >>= 1
            column += 1

    packed_rows: list[dict[str, Any]] = []
    for sign in (1, -1):
        rows: list[dict[int, str]] = []
        for source, column in sorted(signed_terms[sign], key=lambda item: (item[1], item[0])):
            if deadline_check is not None:
                deadline_check("compressor_schedule")
            if column >= width:
                continue
            row = next((candidate for candidate in rows if column not in candidate), None)
            if row is None:
                row = {}
                rows.append(row)
            row[column] = source
        for row in rows:
            packed_rows.append({"sign": sign, "bits": row})

    columns: list[list[str]] = [[] for _ in range(width)]
    for bit in range(width):
        if (constant >> bit) & 1:
            columns[bit].append("CONST1")
    inverted_tokens = set()
    row_manifest = []
    for row_index, row in enumerate(packed_rows):
        if deadline_check is not None:
            deadline_check("compressor_schedule")
        bits = row["bits"]
        sign = row["sign"]
        manifested = []
        for bit in range(width):
            source = bits.get(bit)
            if sign > 0:
                token = source
            elif source is None:
                token = "CONST1"
            else:
                token = f"NOT({source})"
                inverted_tokens.add(token)
            if token is not None:
                columns[bit].append(token)
                manifested.append({"column": bit, "token": token})
        if sign < 0:
            columns[0].append("CONST1")
        row_manifest.append({
            "row": row_index,
            "sign": sign,
            "two_complement_correction": sign < 0,
            "bits": manifested,
        })
    initial_heights = [len(column) for column in columns]
    priorities = dict(source_priorities or {})

    def token_priority(token: str) -> int:
        if token in ("CONST0", "CONST1"):
            return 0
        if token in priorities:
            return priorities[token]
        match = _NOT_TOKEN_RE.fullmatch(token)
        if match is not None:
            return token_priority(match.group("source")) + 1
        return 0

    for column in columns:
        column.sort(key=lambda token: (token_priority(token), token))
    operations = []
    ordinal = 0
    stage = 0
    # Stop at three carry-save rows.  Reducing a sparse three-high column to
    # two rows can launch a half-adder carry that advances only one column per
    # stage through an otherwise two-high word, making depth grow with width.
    # The proved final topology below compresses all three rows locally in one
    # LUT6_2 layer and then uses the ordinary two-row CARRY8 adder.
    final_row_count = 3
    while any(len(column) > final_row_count for column in columns):
        next_columns: list[list[str]] = [[] for _ in range(width)]
        for bit in range(width):
            current = sorted(
                columns[bit], key=lambda token: (token_priority(token), token)
            )
            # Compress only tokens from the current stage.  Carries generated
            # by the lower column live in ``next_columns`` and are consumed in
            # the next stage, preserving a real acyclic compressor tree.
            while len(current) >= 3:
                # With exactly three tokens, use a half adder on the two
                # earliest inputs.  This leaves the latest-arriving token on
                # the final row instead of forcing it through another LUT.
                input_count = 2 if len(current) == 3 else 3
                inputs = [current.pop(0) for _ in range(input_count)]
                sum_name = f"csa_{ordinal}_sum"
                carry_name = f"csa_{ordinal}_carry"
                ordinal += 1
                output_priority = 1 + max(token_priority(token) for token in inputs)
                priorities[sum_name] = output_priority
                priorities[carry_name] = output_priority
                next_columns[bit].append(sum_name)
                if bit + 1 < width:
                    next_columns[bit + 1].append(carry_name)
                operations.append({
                    "stage": stage,
                    "column": bit,
                    "input_count": input_count,
                    "inputs": inputs,
                    "sum": sum_name,
                    "carry": carry_name if bit + 1 < width else None,
                    "primitive": "LUT6_2",
                    "init": "64'h96969696E8E8E8E8",
                    "i5_tie": 1,
                    "sum_output": "O6",
                    "carry_output": "O5",
                })
            next_columns[bit].extend(current)
        columns = next_columns
        stage += 1
    rows = [[] for _ in range(final_row_count)]
    for column in columns:
        for row_index in range(final_row_count):
            rows[row_index].append(
                column[row_index] if len(column) > row_index else "CONST0"
            )
    return {
        "width": width,
        "signed_digit_rows": row_manifest,
        "positive_row_count": sum(row["sign"] > 0 for row in packed_rows),
        "negative_row_count": sum(row["sign"] < 0 for row in packed_rows),
        "inverted_token_count": len(inverted_tokens),
        "initial_column_heights": initial_heights,
        "max_source_priority": max(source_priorities.values(), default=0)
        if source_priorities else 0,
        "max_final_priority": max(
            (token_priority(token) for row in rows for token in row), default=0
        ),
        "stage_count": stage,
        "compressor_count": len(operations),
        "compressors": operations,
        "final_row_count": final_row_count,
        "final_rows": rows,
        "final_adder": dict(_FINAL_ADDER_SPEC),
    }


def _downstream_cells(
    graph: ConeGraph, stop_nets: set[str]
) -> set[str]:
    drivers = graph.net_driver()
    result: set[str] = set()

    def visit(net: str) -> None:
        if not net or net in stop_nets:
            return
        source = drivers.get(net)
        if source is None or source[0] in result:
            return
        cell = source[0]
        result.add(cell)
        for pin in graph.pins[cell].values():
            if pin.direction == "IN":
                visit(pin.net)

    for target in graph.targets:
        visit(target["d_net"])
    return result


def _reverse_depth_frontier(
    graph: ConeGraph, depth: int
) -> tuple[set[str], set[str]]:
    """Return the target-side region and its complete input-net frontier."""
    if depth <= 0:
        raise ValueError("frontier depth must be positive")
    drivers = graph.net_driver()
    layer = {
        source[0]
        for target in graph.targets
        if (source := drivers.get(target["d_net"])) is not None
    }
    region: set[str] = set()
    for _ in range(depth):
        if not layer:
            break
        region.update(layer)
        next_layer: set[str] = set()
        for cell in layer:
            for net in _cell_input_nets(graph, graph.cells[cell]):
                if net in graph.static_nets:
                    continue
                source = drivers.get(net)
                if source is not None and source[0] not in region:
                    next_layer.add(source[0])
        layer = next_layer

    frontier = set()
    for cell in region:
        for net in _cell_input_nets(graph, graph.cells[cell]):
            if net in graph.static_nets:
                continue
            source = drivers.get(net)
            if source is None or source[0] not in region:
                frontier.add(net)
    actual_region = _downstream_cells(graph, {*frontier, *graph.static_nets})
    if actual_region != region:
        raise ValueError("reverse-depth frontier does not exactly bound its target region")
    return region, frontier


def _source_logic_priorities(
    graph: ConeGraph,
    source_nets: set[str],
    *,
    deadline_check: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Estimate source arrival order by upstream combinational logic depth."""
    drivers = graph.net_driver()
    memo: dict[str, int] = {}
    active: set[str] = set()

    def depth(net: str) -> int:
        if deadline_check is not None:
            deadline_check("source_logic_priorities")
        if not net or net in graph.boundary_nets or net in graph.static_nets:
            return 0
        if net in memo:
            return memo[net]
        if net in active:
            raise ValueError(f"combinational cycle while ranking source net {net}")
        source = drivers.get(net)
        if source is None:
            return 0
        active.add(net)
        cell = source[0]
        input_depths = [
            depth(pin.net)
            for pin in graph.pins[cell].values()
            if pin.direction == "IN" and pin.net
        ]
        active.remove(net)
        memo[net] = 1 + max(input_depths, default=0)
        return memo[net]

    return {net: depth(net) for net in source_nets}


_NOT_TOKEN_RE = re.compile(r"^NOT\((?P<source>.*)\)$")


def _spec_symbol_names(weighted_sum: dict[str, Any]) -> list[str]:
    names = set(weighted_sum["coefficients"])
    for row in weighted_sum["quadratic_coefficients"]:
        names.add(row["left"])
        names.add(row["right"])
    return sorted(names)


def _spec_rows(
    spec: dict[str, Any],
    quad_lookup: dict[str, tuple[str, str]],
    base: Callable[[str], Any],
    *,
    const: Callable[[bool], Any],
    xor3: Callable[[Any, Any, Any], Any],
    maj3: Callable[[Any, Any, Any], Any],
    invert: Callable[[Any], Any],
    and2: Callable[[Any, Any], Any],
) -> list[list[Any]]:
    """Evaluate the emitted CSA schedule down to its final operand rows."""
    produced: dict[str, Any] = {}

    def resolve(token: str) -> Any:
        if token == "CONST0":
            return const(False)
        if token == "CONST1":
            return const(True)
        if token in produced:
            return produced[token]
        match = _NOT_TOKEN_RE.fullmatch(token)
        if match is not None:
            return invert(resolve(match.group("source")))
        if token in quad_lookup:
            left, right = quad_lookup[token]
            return and2(base(left), base(right))
        return base(token)

    for operation in spec["compressors"]:
        inputs = [resolve(token) for token in operation["inputs"]]
        if len(inputs) == 2:
            inputs.append(const(False))
        first, second, third = inputs
        produced[operation["sum"]] = xor3(first, second, third)
        if operation["carry"] is not None:
            produced[operation["carry"]] = maj3(first, second, third)
    return [[resolve(token) for token in row] for row in spec["final_rows"]]


def _emitted_final_adder_bits(
    rows: list[list[Any]],
    *,
    const: Callable[[bool], Any],
    xor2: Callable[[Any, Any], Any],
    xor3: Callable[[Any, Any, Any], Any],
    maj3: Callable[[Any, Any, Any], Any],
    mux: Callable[[Any, Any, Any], Any],
) -> list[Any]:
    """Evaluate the exact emitted ternary-CSA plus LUT2/CARRY8 topology.

    A LUT6_2 first computes the three-row parity and majority for each column.
    The majority is shifted to the next column, so a LUT2 computes the select
    for adding ``parity[i]`` and ``majority[i-1]``.  CARRY8 receives
    ``DI = parity[i]`` and implements ``O = S XOR CI`` and
    ``CO = S ? CI : DI``.  The topology therefore adds all three rows modulo
    the word width without a width-linear carry-save reduction wave.
    """
    if len(rows) != 3 or len({len(row) for row in rows}) != 1:
        raise ValueError("emitted final adder requires exactly three equal-width rows")
    carry = const(False)
    shifted_majority = const(False)
    outputs = []
    for row0, row1, row2 in zip(rows[0], rows[1], rows[2]):
        compressed_sum = xor3(row0, row1, row2)
        select = xor2(compressed_sum, shifted_majority)
        outputs.append(xor2(select, carry))
        carry = mux(select, carry, compressed_sum)
        shifted_majority = maj3(row0, row1, row2)
    return outputs


def _verify_emitted_final_adder_recurrence_z3(solver_timeout_ms: int) -> str:
    """Prove the width-independent recurrence used by the emitted final adder.

    A whole-word miter introduces three unconstrained bits per output column and
    makes the solver cost grow sharply with the operator width.  The emitted
    circuit is a repeated one-bit recurrence, so two local identities are
    sufficient: the LUT6_2 compressor preserves a three-bit column, and the
    LUT2/CARRY8 lane preserves its two operands plus incoming carry.  Chaining
    those identities proves every word width modulo 2**width; the discarded
    top carry is exactly the modular overflow.
    """
    import z3

    row0, row1, row2 = z3.Bools(
        "emitted_local_row0 emitted_local_row1 emitted_local_row2"
    )
    shifted_majority, carry_in = z3.Bools(
        "emitted_local_shifted_majority emitted_local_carry_in"
    )
    compressed_sum = z3.Xor(row0, z3.Xor(row1, row2))
    majority = z3.Or(
        z3.And(row0, row1),
        z3.And(row0, row2),
        z3.And(row1, row2),
    )
    select = z3.Xor(compressed_sum, shifted_majority)
    output = z3.Xor(select, carry_in)
    carry_out = z3.If(select, carry_in, compressed_sum)
    as_int = lambda bit: z3.If(bit, z3.IntVal(1), z3.IntVal(0))

    solver = z3.Solver()
    solver.set(timeout=int(solver_timeout_ms))
    solver.add(z3.Or(
        as_int(row0) + as_int(row1) + as_int(row2)
        != as_int(compressed_sum) + 2 * as_int(majority),
        as_int(compressed_sum) + as_int(shifted_majority) + as_int(carry_in)
        != as_int(output) + 2 * as_int(carry_out),
    ))
    result = solver.check()
    if result == z3.unsat:
        return "proved"
    if result == z3.sat:
        return "failed"
    return f"unknown:{solver.reason_unknown()}"


def _quad_lookup(weighted_sum: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {
        row["token"]: (row["left"], row["right"])
        for row in weighted_sum["quadratic_coefficients"]
    }


def _schedule_initial_columns(
    weighted_sum: dict[str, Any], spec: dict[str, Any]
) -> list[list[str]] | None:
    """Reconstruct and validate the token columns consumed by the CSA schedule."""
    width = int(spec["width"])
    if width != int(weighted_sum["modulus_width"]):
        return None
    columns: list[list[str]] = [[] for _ in range(width)]
    constant = int(weighted_sum["constant"])
    for bit in range(width):
        if (constant >> bit) & 1:
            columns[bit].append("CONST1")

    positive_count = 0
    negative_count = 0
    inverted_tokens: set[str] = set()
    for ordinal, row in enumerate(spec["signed_digit_rows"]):
        if int(row["row"]) != ordinal or int(row["sign"]) not in (-1, 1):
            return None
        sign = int(row["sign"])
        correction = bool(row["two_complement_correction"])
        if correction != (sign < 0):
            return None
        positive_count += sign > 0
        negative_count += sign < 0
        seen_columns: set[int] = set()
        for item in row["bits"]:
            column = int(item["column"])
            token = str(item["token"])
            if column < 0 or column >= width or column in seen_columns:
                return None
            seen_columns.add(column)
            columns[column].append(token)
            if _NOT_TOKEN_RE.fullmatch(token):
                inverted_tokens.add(token)
        if sign < 0:
            if seen_columns != set(range(width)):
                return None
            columns[0].append("CONST1")

    if positive_count != int(spec["positive_row_count"]):
        return None
    if negative_count != int(spec["negative_row_count"]):
        return None
    if len(inverted_tokens) != int(spec["inverted_token_count"]):
        return None
    if [len(column) for column in columns] != spec["initial_column_heights"]:
        return None
    return columns


def _verify_schedule_structure(
    weighted_sum: dict[str, Any], spec: dict[str, Any]
) -> list[list[str]] | None:
    """Check that every emitted compressor preserves its source column weight."""
    columns = _schedule_initial_columns(weighted_sum, spec)
    if columns is None:
        return None
    width = int(spec["width"])
    produced: set[str] = set()
    operations = spec["compressors"]
    operation_index = 0
    stage_count = int(spec["stage_count"])
    for stage in range(stage_count):
        next_columns: list[list[str]] = [[] for _ in range(width)]
        while (
            operation_index < len(operations)
            and int(operations[operation_index]["stage"]) == stage
        ):
            operation = operations[operation_index]
            operation_index += 1
            column = int(operation["column"])
            inputs = [str(token) for token in operation["inputs"]]
            sum_name = str(operation["sum"])
            carry_name = operation["carry"]
            if (
                column < 0
                or column >= width
                or len(inputs) not in (2, 3)
                or int(operation["input_count"]) != len(inputs)
                or operation["primitive"] != "LUT6_2"
                or operation["init"].lower() != "64'h96969696e8e8e8e8"
                or int(operation["i5_tie"]) != 1
                or operation["sum_output"] != "O6"
                or operation["carry_output"] != "O5"
                or not sum_name
                or sum_name in produced
            ):
                return None
            available = columns[column]
            for token in inputs:
                if token not in available:
                    return None
                available.remove(token)
            produced.add(sum_name)
            next_columns[column].append(sum_name)
            if column + 1 < width:
                if not carry_name or str(carry_name) in produced:
                    return None
                produced.add(str(carry_name))
                next_columns[column + 1].append(str(carry_name))
            elif carry_name is not None:
                return None
        for column, remaining in enumerate(columns):
            next_columns[column].extend(remaining)
        columns = next_columns

    if operation_index != len(operations):
        return None
    if len(operations) != int(spec["compressor_count"]):
        return None
    final_row_count = int(spec["final_row_count"])
    if (
        final_row_count != 3
        or spec["final_adder"] != _FINAL_ADDER_SPEC
        or len(spec["final_rows"]) != final_row_count
    ):
        return None
    expected_rows = [[] for _ in range(final_row_count)]
    for column in columns:
        if len(column) > final_row_count:
            return None
        for row_index in range(final_row_count):
            expected_rows[row_index].append(
                column[row_index] if len(column) > row_index else "CONST0"
            )
    if any(len(row) != width for row in spec["final_rows"]):
        return None
    for column in range(width):
        expected = sorted(row[column] for row in expected_rows)
        actual = sorted(row[column] for row in spec["final_rows"])
        if expected != actual:
            return None
    return _schedule_initial_columns(weighted_sum, spec)


def _verify_spec_z3(
    weighted_sum: dict[str, Any],
    spec: dict[str, Any],
    solver_timeout_ms: int,
    *,
    deadline_check: Callable[[str], None] | None = None,
    remaining_timeout_ms: Callable[[str], int] | None = None,
) -> str:
    """Prove the actual emitted LUT/CSA/CARRY8 topology equals the polynomial."""
    import z3

    width = int(spec["width"])
    if deadline_check is not None:
        deadline_check("emitted_topology_structure")
    initial_columns = _verify_schedule_structure(weighted_sum, spec)
    if initial_columns is None:
        return "failed"
    names = _spec_symbol_names(weighted_sum)
    symbols = {name: z3.Bool(f"spec_{index}") for index, name in enumerate(names)}
    quad_lookup = _quad_lookup(weighted_sum)

    def resolve(token: str) -> Any:
        if token == "CONST1":
            return z3.BoolVal(True)
        match = _NOT_TOKEN_RE.fullmatch(token)
        if match is not None:
            return z3.Not(resolve(match.group("source")))
        if token in quad_lookup:
            left, right = quad_lookup[token]
            return z3.And(symbols[left], symbols[right])
        if token not in symbols:
            raise ValueError(f"unknown compressor token: {token}")
        return symbols[token]

    spec_value = z3.BitVecVal(0, width)
    for column, tokens in enumerate(initial_columns):
        for token in tokens:
            spec_value = spec_value + z3.If(
                resolve(token),
                z3.BitVecVal(1 << column, width),
                z3.BitVecVal(0, width),
            )
    polynomial = z3.BitVecVal(int(weighted_sum["constant"]), width)
    for name, coefficient in weighted_sum["coefficients"].items():
        polynomial = polynomial + z3.If(
            symbols[name],
            z3.BitVecVal(int(coefficient), width),
            z3.BitVecVal(0, width),
        )
    for row in weighted_sum["quadratic_coefficients"]:
        polynomial = polynomial + z3.If(
            z3.And(symbols[row["left"]], symbols[row["right"]]),
            z3.BitVecVal(int(row["coefficient"]), width),
            z3.BitVecVal(0, width),
        )
    row_solver = z3.Solver()
    row_solver.set(
        timeout=(
            remaining_timeout_ms("emitted_topology_rows")
            if remaining_timeout_ms is not None else int(solver_timeout_ms)
        )
    )
    row_solver.add(spec_value != polynomial)
    row_result = row_solver.check()
    if row_result != z3.unsat:
        if row_result == z3.sat:
            return "failed"
        return f"unknown:{row_solver.reason_unknown()}"
    if deadline_check is not None:
        deadline_check("emitted_topology_recurrence")

    # The final topology is a repeated one-bit recurrence.  Prove its local
    # compressor and carry invariant once instead of building a 3*width input
    # whole-word SAT miter.  This is exact for every width and avoids the live
    # 31-bit timeout that previously appeared as EMITTED_TOPOLOGY_NOT_PROVED.
    emitted_result = _verify_emitted_final_adder_recurrence_z3(
        remaining_timeout_ms("emitted_topology_recurrence")
        if remaining_timeout_ms is not None else solver_timeout_ms
    )
    return emitted_result


def _spec_concrete_value(
    weighted_sum: dict[str, Any], spec: dict[str, Any], values: dict[str, bool]
) -> int:
    width = int(spec["width"])
    rows = _spec_rows(
        spec,
        _quad_lookup(weighted_sum),
        lambda name: bool(values[name]),
        const=bool,
        xor3=lambda a, b, c: bool(a) ^ bool(b) ^ bool(c),
        maj3=lambda a, b, c: (int(a) + int(b) + int(c)) >= 2,
        invert=lambda a: not a,
        and2=lambda a, b: bool(a) and bool(b),
    )
    output_bits = _emitted_final_adder_bits(
        rows,
        const=bool,
        xor2=lambda a, b: bool(a) ^ bool(b),
        xor3=lambda a, b, c: bool(a) ^ bool(b) ^ bool(c),
        maj3=lambda a, b, c: (int(a) + int(b) + int(c)) >= 2,
        mux=lambda select, when_true, when_false: (
            bool(when_true) if bool(select) else bool(when_false)
        ),
    )
    value = 0
    for index, bit in enumerate(output_bits):
        value |= int(bool(bit)) << index
    return value % (1 << width)


def _polynomial_concrete_value(
    weighted_sum: dict[str, Any], values: dict[str, bool]
) -> int:
    width = int(weighted_sum["modulus_width"])
    total = int(weighted_sum["constant"])
    for name, coefficient in weighted_sum["coefficients"].items():
        if values[name]:
            total += int(coefficient)
    for row in weighted_sum["quadratic_coefficients"]:
        if values[row["left"]] and values[row["right"]]:
            total += int(row["coefficient"])
    return total % (1 << width)


def verify_certificate_vectors(
    root: Path | str,
    certificate: dict[str, Any],
    *,
    samples: int = 64,
    seed: int = 0,
) -> dict[str, Any]:
    """End-to-end miter: extracted netlist vs polynomial vs emitted spec."""
    import random

    if not verify_compressor_certificate(certificate, root):
        raise ValueError("certificate hash/source binding verification failed")
    spec = certificate.get("compressor_spec")
    if spec is None:
        raise ValueError("certificate has no compressor spec")
    graph = ConeGraph.load(root)
    chain = _carry_chain(graph)
    claimed_chain = certificate["cone"]["final_carry_chain"]
    if chain["cells"] != claimed_chain["cells"] or chain["width"] != claimed_chain["width"]:
        raise ValueError("final carry chain no longer matches certificate")
    weighted_sum = certificate["weighted_sum"]
    boundary_nets = sorted({
        *graph.boundary_nets,
        *certificate["cone"]["compositional_cut"]["nets"],
    })
    rng = random.Random(seed)
    vectors = [
        {net: False for net in boundary_nets},
        {net: True for net in boundary_nets},
    ]
    for _ in range(max(0, int(samples) - len(vectors))):
        vectors.append({net: rng.random() < 0.5 for net in boundary_nets})
    mismatches = []
    for ordinal, values in enumerate(vectors):
        _, outputs = evaluate_concrete(graph, values)
        netlist_value = _chain_integer(outputs, chain)
        polynomial_value = _polynomial_concrete_value(weighted_sum, values)
        spec_value = _spec_concrete_value(weighted_sum, spec, values)
        if not netlist_value == polynomial_value == spec_value:
            mismatches.append({
                "vector": ordinal,
                "netlist": netlist_value,
                "polynomial": polynomial_value,
                "spec": spec_value,
            })
    return {
        "samples": len(vectors),
        "boundary_bits": len(boundary_nets),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:8],
        "passed": not mismatches,
    }


def compressor_mutation_rejection_codes(certificate: dict[str, Any]) -> list[str]:
    """Return correctness blockers without treating estimated QoR as authority."""
    proof = certificate.get("proof") or {}
    cone = certificate.get("cone") or {}
    spec = certificate.get("compressor_spec")
    codes: list[str] = []
    if certificate.get("status") != "proved":
        codes.append("SOURCE_FUNCTION_NOT_PROVED")
    if int(cone.get("side_fanout_count") or 0) != 0:
        codes.append("REPLACEABLE_CONE_HAS_SIDE_FANOUT")
    if proof.get("emitted_topology_solver_result") != "proved":
        codes.append("EMITTED_TOPOLOGY_NOT_PROVED")
    if not isinstance(spec, dict):
        codes.append("COMPRESSOR_SPEC_MISSING")
    elif spec.get("final_adder") != _FINAL_ADDER_SPEC:
        codes.append("FINAL_ADDER_SCHEMA_MISMATCH")
    return codes


def compressor_certificate_summary(certificate: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded, object-name-free summary suitable for persistent Memory."""
    proof = certificate.get("proof") or {}
    cone = certificate.get("cone") or {}
    cost = certificate.get("implementation_cost") or {}
    spec = certificate.get("compressor_spec") or {}
    final_chain = cone.get("final_carry_chain") or {}
    readiness = certificate.get("readiness") or {}
    failure = proof.get("failure") or {}
    existing_luts = cost.get("existing_lut_count")
    proposed_luts = cost.get("proposed_lut_count_upper_bound")
    existing_carries = cost.get("existing_carry8_count")
    arithmetic_width = spec.get("width")
    observable_width = len(final_chain.get("target_positions") or [])
    emitted_carries = (
        (int(arithmetic_width) + 7) // 8
        if isinstance(arithmetic_width, int) and arithmetic_width > 0 else None
    )
    lut_growth_ratio = (
        round(float(proposed_luts) / float(existing_luts), 4)
        if isinstance(proposed_luts, int)
        and isinstance(existing_luts, int)
        and existing_luts > 0 else None
    )
    return {
        "certificate_version": certificate.get("version"),
        "status": certificate.get("status"),
        "mutation_ready": certificate.get("mutation_ready") is True,
        "blocking_rejection_codes": list(
            readiness.get("blocking_rejection_codes")
            or compressor_mutation_rejection_codes(certificate)
        ),
        "advisory_warning_codes": list(readiness.get("advisory_warning_codes") or []),
        "source_solver_result": proof.get("solver_result"),
        "source_solver_failure_reason": failure.get("reason"),
        "emitted_topology_solver_result": proof.get(
            "emitted_topology_solver_result"
        ),
        "side_fanout_count": int(cone.get("side_fanout_count") or 0),
        "compressor_spec_present": isinstance(
            certificate.get("compressor_spec"), dict
        ),
        "replaceable_cell_count": cost.get("replaceable_cell_count"),
        "existing_lut_count": cost.get("existing_lut_count"),
        "existing_carry8_count": existing_carries,
        "proposed_lut_count_upper_bound": cost.get(
            "proposed_lut_count_upper_bound"
        ),
        "cost_growth_guide": cost.get("growth_limit"),
        "cost_guide_met": cost.get("acceptable"),
        "cost_role": cost.get("role"),
        "qor_advisory": {
            "arithmetic_width": arithmetic_width,
            "observable_output_width": observable_width,
            "compressor_count": spec.get("compressor_count"),
            "compressor_stage_count": spec.get("stage_count"),
            "final_row_count": spec.get("final_row_count"),
            "existing_carry8_count": existing_carries,
            "emitted_carry8_count": emitted_carries,
            "emitted_minus_existing_carry8_count": (
                emitted_carries - int(existing_carries)
                if emitted_carries is not None
                and isinstance(existing_carries, int) else None
            ),
            "proposed_to_existing_lut_ratio": lut_growth_ratio,
            "arithmetic_minus_observable_width": (
                int(arithmetic_width) - observable_width
                if isinstance(arithmetic_width, int) else None
            ),
            "role": (
                "descriptive_soft_evidence_for_luna_and_audit; "
                "never_mutation_or_acceptance_authority"
            ),
        },
    }


def build_compressor_certificate(
    root: Path | str,
    *,
    solver_timeout_ms: int = 120_000,
    frontier_depth: int | None = None,
    cut_rewire_outputs: bool = False,
) -> dict[str, Any]:
    if (
        isinstance(solver_timeout_ms, bool)
        or not isinstance(solver_timeout_ms, int)
        or solver_timeout_ms < 1
    ):
        raise ValueError("solver_timeout_ms must be a positive integer")
    proof_started = time.monotonic()
    proof_deadline = proof_started + solver_timeout_ms / 1000.0

    def deadline_check(stage: str) -> None:
        _require_proof_budget(
            proof_deadline,
            started_monotonic=proof_started,
            budget_ms=solver_timeout_ms,
            stage=stage,
        )

    def remaining_timeout_ms(stage: str) -> int:
        return _remaining_solver_timeout_ms(
            proof_deadline,
            started_monotonic=proof_started,
            budget_ms=solver_timeout_ms,
            stage=stage,
        )

    graph = ConeGraph.load(root)
    deadline_check("cone_load")
    chain = _carry_chain(graph)
    width = int(chain["width"])
    modulus = 1 << width
    final_carries = set(chain["cells"])
    if frontier_depth is None:
        cut_kind = "all_nonfinal_carry_outputs"
        cut_set = {
            pin.net
            for name, cell in graph.cells.items()
            if cell.ref == "CARRY8" and name not in final_carries
            for pin_name, pin in graph.pins[name].items()
            if _pin_index(pin_name, "O") is not None and pin.net
        }
        if cut_rewire_outputs:
            cut_kind += "_plus_rewire_outputs"
            cut_set.update({
                pin.net
                for name in graph.cells
                if name.endswith("_rewire")
                for pin in graph.pins[name].values()
                if pin.direction == "OUT" and pin.net
            })
        cut_nets = sorted(cut_set)
        proof_nets = sorted({
            *cut_nets,
            *(
                net
                for net in graph.boundary_nets
                if net in {
                    input_net
                    for cell in _downstream_cells(
                        graph, {*cut_nets, *graph.boundary_nets, *graph.static_nets}
                    )
                    for input_net in _cell_input_nets(graph, graph.cells[cell])
                }
            ),
        })
        expected_region = None
    else:
        cut_kind = "reverse_logic_depth_frontier"
        expected_region, frontier = _reverse_depth_frontier(graph, frontier_depth)
        cut_nets = sorted(frontier)
        proof_nets = cut_nets
    input_nets = sorted({*graph.boundary_nets, *proof_nets})
    linear_assignments = [()] + [(net,) for net in proof_nets]
    linear_values = _evaluate_boolean_batch(
        graph,
        input_nets,
        linear_assignments,
        chain,
        deadline_check=deadline_check,
    )
    constant = linear_values[0]
    coefficients = {
        net: (linear_values[index] - constant) % modulus
        for index, net in enumerate(proof_nets, start=1)
    }

    try:
        import z3
    except ImportError as exc:  # pragma: no cover - environment failure
        raise RuntimeError("z3-solver is required for compressor equivalence proof") from exc
    symbols = {net: z3.Bool(f"b_{index}") for index, net in enumerate(proof_nets)}
    symbolic_boundary: dict[str, Any] = {
        net: z3.BoolVal(False) for net in graph.boundary_nets
    }
    symbolic_boundary.update(symbols)
    _, symbolic_outputs = _evaluate(
        graph,
        symbolic_boundary,
        xor=z3.Xor,
        choose=z3.If,
        invert=z3.Not,
        deadline_check=deadline_check,
    )
    actual = z3.BitVecVal(0, width)
    for offset, cell in enumerate(chain["cells"]):
        for index in range(8):
            position = offset * 8 + index
            if position >= width:
                continue
            bit = symbolic_outputs[(cell, f"O[{index}]")]
            actual = actual | z3.If(
                bit, z3.BitVecVal(1 << position, width), z3.BitVecVal(0, width)
            )
    proposed = z3.BitVecVal(constant, width)
    for net in proof_nets:
        proposed = proposed + z3.If(
            symbols[net],
            z3.BitVecVal(coefficients[net], width),
            z3.BitVecVal(0, width),
        )
    # Whole-word QF_BV used to receive the complete remaining deadline.  When
    # it returned unknown, the advertised bitwise fallback reached bit zero
    # with no budget left.  Prove the independent output obligations first and
    # give each remaining bit a fair share of the live total budget.
    bitwise_fallback_used = True
    bitwise_failure_bit: int | None = None

    def check(candidate: Any) -> tuple[Any, Any]:
        nonlocal bitwise_fallback_used, bitwise_failure_bit
        bitwise_failure_bit = None
        last_solver = None
        for bit in range(width):
            stage = f"source_function_bit_{bit}"
            remaining = remaining_timeout_ms(stage)
            obligations_left = width - bit
            per_bit_timeout_ms = max(1, remaining // obligations_left)
            per_bit = z3.SolverFor("QF_BV")
            per_bit.set(timeout=per_bit_timeout_ms)
            per_bit.add(
                z3.Extract(bit, bit, actual) != z3.Extract(bit, bit, candidate)
            )
            bit_result = per_bit.check()
            last_solver = per_bit
            if bit_result != z3.unsat:
                bitwise_failure_bit = bit
                return bit_result, per_bit
        return z3.unsat, last_solver

    result, solver = check(proposed)
    quadratic: dict[tuple[str, str], int] = {}
    proof_degree = 1
    if result == z3.sat:
        # Multiplier cones are quadratic over register-boundary bits. Recover
        # exact pair terms by finite differences, then prove the conjecture.
        pair_assignments: list[tuple[str, str]] = []
        for left_index, left in enumerate(proof_nets):
            for right in proof_nets[left_index + 1:]:
                pair_assignments.append((left, right))
        pair_values = _evaluate_boolean_batch(
            graph,
            input_nets,
            pair_assignments,
            chain,
            deadline_check=deadline_check,
        )
        for (left, right), pair_value in zip(pair_assignments, pair_values):
            deadline_check("quadratic_finite_difference")
            coefficient = (
                pair_value - constant - coefficients[left] - coefficients[right]
            ) % modulus
            if coefficient:
                quadratic[(left, right)] = coefficient
        quadratic_proposed = proposed
        for (left, right), coefficient in quadratic.items():
            quadratic_proposed = quadratic_proposed + z3.If(
                z3.And(symbols[left], symbols[right]),
                z3.BitVecVal(coefficient, width),
                z3.BitVecVal(0, width),
            )
        result, solver = check(quadratic_proposed)
        proof_degree = 2
    proof_status = "proved" if result == z3.unsat else "failed"
    failure = None
    if result == z3.sat:
        model = solver.model()
        failure = {
            "reason": "weighted-sum conjecture has a counterexample",
            "true_boundary_nets": [net for net in proof_nets if z3.is_true(model.eval(symbols[net], model_completion=True))],
        }
    elif result == z3.unknown:
        failure = {"reason": f"solver returned unknown: {solver.reason_unknown()}"}

    nonzero = {net: value for net, value in coefficients.items() if value}
    compressor_coefficients = dict(nonzero)
    quadratic_rows = []
    for (left, right), coefficient in sorted(quadratic.items()):
        token = f"AND({left},{right})"
        compressor_coefficients[token] = coefficient
        quadratic_rows.append({
            "left": left,
            "right": right,
            "coefficient": coefficient,
            "token": token,
            "primitive": "LUT2",
            "init": "4'h8",
        })
    source_identity = {
        "dcp_sha256": graph.manifest["dcp_sha256"],
        "design": graph.manifest["design"],
        "part": graph.manifest["part"],
        "target_stem": graph.manifest["target_stem"],
        "connectivity_sha256": graph.connectivity_digest(),
    }
    weighted_sum = {
        "modulus_width": width,
        "constant": constant,
        "coefficients": nonzero,
        "quadratic_coefficients": quadratic_rows,
        "polynomial_degree": proof_degree,
        "zero_coefficient_count": len(coefficients) - len(nonzero),
    }
    source_priorities = _source_logic_priorities(
        graph,
        set(nonzero),
        deadline_check=deadline_check,
    )
    for row in quadratic_rows:
        source_priorities[row["token"]] = 1 + max(
            source_priorities.get(row["left"], 0),
            source_priorities.get(row["right"], 0),
        )
    compressor_spec = (
        _compressor_schedule(
            width,
            constant,
            compressor_coefficients,
            source_priorities,
            deadline_check=deadline_check,
        )
        if proof_status == "proved" else None
    )
    spec_result = None
    if compressor_spec is not None:
        spec_result = _verify_spec_z3(
            weighted_sum,
            compressor_spec,
            solver_timeout_ms,
            deadline_check=deadline_check,
            remaining_timeout_ms=remaining_timeout_ms,
        )
    downstream = _downstream_cells(
        graph, {*proof_nets, *graph.static_nets}
    )
    if expected_region is not None and downstream != expected_region:
        raise ValueError("proved frontier and replaceable region disagree")
    existing_luts = sum(graph.cells[name].ref.startswith("LUT") for name in downstream)
    existing_carries = sum(graph.cells[name].ref == "CARRY8" for name in downstream)
    proposed_luts = None
    cost_acceptable = False
    if compressor_spec is not None:
        proposed_luts = (
            compressor_spec["compressor_count"]
            + compressor_spec["inverted_token_count"]
            + 2 * width
            + len(quadratic_rows)
        )
        cost_acceptable = proposed_luts <= max(existing_luts * 2, existing_luts + 64)
    certificate: dict[str, Any] = {
        "kind": "register_cone_compressor_certificate",
        "version": CERTIFICATE_VERSION,
        "status": proof_status,
        "source_identity": source_identity,
        "cone": {
            "cell_count": len(graph.cells),
            "boundary_bit_count": len(proof_nets),
            "side_fanout_count": len(graph.side_fanout),
            "final_carry_chain": chain,
            "compositional_cut": {
                "kind": cut_kind,
                "reverse_logic_depth": frontier_depth,
                "net_count": len(cut_nets),
                "nets": cut_nets,
                "retained_upstream_logic": True,
            },
        },
        "weighted_sum": weighted_sum,
        "proof": {
            "method": f"z3_exact_structural_miter_against_degree_{proof_degree}_integer_polynomial",
            "source_function_check_mode": "bitwise_first_fair_share",
            "solver_result": str(result),
            "bitwise_fallback_used": bitwise_fallback_used,
            "bitwise_failure_bit": bitwise_failure_bit,
            "spec_solver_result": spec_result,
            "emitted_topology": (
                "LUT6_2_CSA3_PLUS_LUT2_XOR_DI_PLUS_CARRY8_SINGLE_CY8"
            ),
            "emitted_topology_solver_result": spec_result,
            "failure": failure,
            "total_budget_ms": solver_timeout_ms,
            "elapsed_s": round(time.monotonic() - proof_started, 6),
        },
        "implementation_cost": {
            "replaceable_cell_count": len(downstream),
            "existing_lut_count": existing_luts,
            "existing_carry8_count": existing_carries,
            "proposed_lut_count_upper_bound": proposed_luts,
            "growth_limit": max(existing_luts * 2, existing_luts + 64),
            "acceptable": cost_acceptable,
            "role": "advisory_qor_estimate_not_mutation_authority",
        },
    }
    if compressor_spec is not None:
        certificate["compressor_spec"] = compressor_spec
    blocking_rejection_codes = compressor_mutation_rejection_codes(certificate)
    advisory_warning_codes = []
    if not cost_acceptable:
        advisory_warning_codes.append("IMPLEMENTATION_COST_ABOVE_QOR_GUIDE")
    if compressor_spec is not None:
        emitted_carries = (int(compressor_spec["width"]) + 7) // 8
        if emitted_carries >= existing_carries:
            advisory_warning_codes.append(
                "FINAL_CARRY_NOT_SHORTER_THAN_REPLACED_CHAIN"
            )
        if proposed_luts is not None and proposed_luts > existing_luts:
            advisory_warning_codes.append(
                "PROPOSED_LUT_COUNT_EXCEEDS_REPLACED_LUT_COUNT"
            )
        if int(compressor_spec["width"]) > len(chain.get("target_positions") or []):
            advisory_warning_codes.append(
                "ARITHMETIC_WIDTH_EXCEEDS_OBSERVABLE_OUTPUT_WIDTH"
            )
    certificate["readiness"] = {
        "version": 1,
        "blocking_rejection_codes": blocking_rejection_codes,
        "advisory_warning_codes": advisory_warning_codes,
        "correctness_checks_remain_authoritative": True,
        "implementation_cost_is_advisory": True,
    }
    certificate["mutation_ready"] = not blocking_rejection_codes
    payload = dict(certificate)
    certificate["certificate_id"] = _digest(payload)
    return certificate


def verify_compressor_certificate(certificate: dict[str, Any], root: Path | str) -> bool:
    if certificate.get("kind") != "register_cone_compressor_certificate":
        return False
    if certificate.get("version") != CERTIFICATE_VERSION:
        return False
    claimed = str(certificate.get("certificate_id") or "")
    payload = dict(certificate)
    payload.pop("certificate_id", None)
    if not claimed or _digest(payload) != claimed:
        return False
    graph = ConeGraph.load(root)
    identity = certificate.get("source_identity") or {}
    return (
        identity.get("dcp_sha256") == graph.manifest.get("dcp_sha256")
        and identity.get("target_stem") == graph.manifest.get("target_stem")
        and identity.get("connectivity_sha256") == graph.connectivity_digest()
    )
