"""Constructive ROBDD extraction for registered LUT/CARRY accumulator cones.

This tool deliberately does not use SAT or SMT as expression authority.  It
composes every LUT and CARRY8 primitive into one reduced ordered BDD, builds a
word-level candidate in the same canonical manager, and accepts equivalence
only when every output has the identical canonical node.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .compressor_proof import (
    ConeGraph,
    _compressor_schedule,
    _evaluate_target_boolean_batch as _raw_evaluate_target_boolean_batch,
    _parse_init,
    _spec_rows,
    _verify_schedule_structure,
)


SCHEMA_VERSION = "constructive-accumulator-robd-v1"
_INDEXED = re.compile(r"^(?P<stem>.+)\[(?P<index>\d+)\]$")

# Generic authority limits, not benchmark-shape checks.  They bound memory and
# repeated per-output work for every freshly recovered cone.
MAX_CONE_CELLS = 512
MAX_BOUNDARY_BITS = 128
MAX_BOUNDARY_WORDS = 8
MAX_TARGET_BITS = 64
MAX_BDD_NODES_PER_OUTPUT = 10_000_000
MAX_TOTAL_BDD_NODES = 160_000_000


def _evaluate_target_boolean_batch(
    graph: ConeGraph,
    input_nets: list[str],
    assignments: list[tuple[str, ...]],
) -> list[int]:
    """Adapt the extractor to the budget-aware primitive evaluator ABI."""
    return _raw_evaluate_target_boolean_batch(
        graph,
        input_nets,
        assignments,
        deadline_check=lambda _stage: None,
    )


class BDDLimitError(RuntimeError):
    """Raised when a constructive extraction exceeds its declared node cap."""


class BDDManager:
    """Small deterministic reduced ordered BDD manager with complemented logic."""

    def __init__(self, labels: tuple[str, ...], *, max_nodes: int = 5_000_000):
        if len(labels) != len(set(labels)):
            raise ValueError("BDD variable labels are not unique")
        self.labels = labels
        self.max_nodes = int(max_nodes)
        # Terminals are node 0 and node 1. Nonterminals use parallel arrays.
        self.variables = [len(labels), len(labels)]
        self.lows = [0, 1]
        self.highs = [0, 1]
        self.unique: dict[tuple[int, int, int], int] = {}
        self.ite_cache: dict[tuple[int, int, int], int] = {}
        self.not_cache: dict[int, int] = {0: 1, 1: 0}

    @property
    def node_count(self) -> int:
        return len(self.variables)

    def mk(self, variable: int, low: int, high: int) -> int:
        if low == high:
            return low
        key = (int(variable), int(low), int(high))
        prior = self.unique.get(key)
        if prior is not None:
            return prior
        if len(self.variables) >= self.max_nodes:
            raise BDDLimitError(
                f"CACC_ROBDD_NODE_LIMIT_EXCEEDED: ROBDD node cap {self.max_nodes} exceeded"
            )
        node = len(self.variables)
        self.unique[key] = node
        self.variables.append(key[0])
        self.lows.append(key[1])
        self.highs.append(key[2])
        return node

    def variable(self, ordinal: int) -> int:
        return self.mk(ordinal, 0, 1)

    def negate(self, node: int) -> int:
        cached = self.not_cache.get(node)
        if cached is not None:
            return cached
        result = self.ite(node, 0, 1)
        self.not_cache[node] = result
        self.not_cache[result] = node
        return result

    def ite(self, condition: int, yes: int, no: int) -> int:
        if condition == 1:
            return yes
        if condition == 0:
            return no
        if yes == no:
            return yes
        if yes == 1 and no == 0:
            return condition
        key = (condition, yes, no)
        cached = self.ite_cache.get(key)
        if cached is not None:
            return cached
        top = min(
            self.variables[condition], self.variables[yes], self.variables[no]
        )

        def cofactors(node: int) -> tuple[int, int]:
            if self.variables[node] == top:
                return self.lows[node], self.highs[node]
            return node, node

        c0, c1 = cofactors(condition)
        y0, y1 = cofactors(yes)
        n0, n1 = cofactors(no)
        low = self.ite(c0, y0, n0)
        high = self.ite(c1, y1, n1)
        result = self.mk(top, low, high)
        self.ite_cache[key] = result
        return result

    def xor(self, left: int, right: int) -> int:
        if left == right:
            return 0
        return self.ite(left, self.negate(right), right)

    def and_(self, left: int, right: int) -> int:
        return self.ite(left, right, 0)

    def or_(self, left: int, right: int) -> int:
        return self.ite(left, 1, right)

    def digest(self, roots: Iterable[int]) -> str:
        memo: dict[int, bytes] = {
            0: hashlib.sha256(b"BDD0").digest(),
            1: hashlib.sha256(b"BDD1").digest(),
        }

        def visit(node: int) -> bytes:
            prior = memo.get(node)
            if prior is not None:
                return prior
            payload = (
                b"BDDN\0"
                + self.labels[self.variables[node]].encode("utf-8")
                + b"\0"
                + visit(self.lows[node])
                + visit(self.highs[node])
            )
            result = hashlib.sha256(payload).digest()
            memo[node] = result
            return result

        whole = hashlib.sha256()
        for root in roots:
            whole.update(visit(root))
        return whole.hexdigest()

    def counterexample(self, node: int) -> dict[str, bool] | None:
        """Return one assignment making ``node`` true, or None for false."""
        if node == 0:
            return None
        result: dict[str, bool] = {}
        current = node
        while current not in (0, 1):
            label = self.labels[self.variables[current]]
            if self.highs[current] != 0:
                result[label] = True
                current = self.highs[current]
            else:
                result[label] = False
                current = self.lows[current]
        return result if current == 1 else None


class ConeBDDComposer:
    """Demand-driven exact primitive composer for one BDD manager."""

    def __init__(
        self, graph: ConeGraph, manager: BDDManager, boundary: dict[str, int]
    ):
        self.graph = graph
        self.manager = manager
        self.boundary = boundary
        self.drivers = graph.net_driver()
        self.net_cache: dict[str, int] = {
            **{net: int(value) for net, value in graph.static_nets.items()},
            **boundary,
        }
        self.output_cache: dict[tuple[str, str], int] = {}

    def net(self, name: str) -> int:
        cached = self.net_cache.get(name)
        if cached is not None:
            return cached
        source = self.drivers.get(name)
        if source is None:
            raise ValueError(f"constructive cone net has no driver: {name}")
        value = self.output(*source)
        self.net_cache[name] = value
        return value

    def _input(self, cell_name: str, pin_name: str) -> int:
        pin = self.graph.pins[cell_name].get(pin_name)
        return self.net(pin.net) if pin is not None and pin.net else 0

    def output(self, cell_name: str, pin_name: str) -> int:
        key = (cell_name, pin_name)
        cached = self.output_cache.get(key)
        if cached is not None:
            return cached
        cell = self.graph.cells[cell_name]
        match = re.fullmatch(r"LUT([1-6])", cell.ref)
        if match is not None:
            size = int(match.group(1))
            width, table = _parse_init(cell.init)
            if width != 1 << size or pin_name != "O":
                raise ValueError(f"invalid LUT output request: {cell_name}/{pin_name}")
            terms = [self._input(cell_name, f"I{index}") for index in range(size)]
            values = [int(bool((table >> address) & 1)) for address in range(1 << size)]
            for term in terms:
                values = [
                    self.manager.ite(term, values[index + 1], values[index])
                    for index in range(0, len(values), 2)
                ]
            value = values[0]
        elif cell.ref == "LUT6_2":
            width, table = _parse_init(cell.init)
            if width != 64 or pin_name not in {"O5", "O6"}:
                raise ValueError(f"invalid LUT6_2 output request: {cell_name}/{pin_name}")
            size = 5 if pin_name == "O5" else 6
            terms = [self._input(cell_name, f"I{index}") for index in range(size)]
            values = [int(bool((table >> address) & 1)) for address in range(1 << size)]
            for term in terms:
                values = [
                    self.manager.ite(term, values[index + 1], values[index])
                    for index in range(0, len(values), 2)
                ]
            value = values[0]
        elif cell.ref in {"GND", "VCC"}:
            value = int(cell.ref == "VCC")
        elif cell.ref == "CARRY8":
            pin_match = re.fullmatch(r"(O|CO)\[(\d+)\]", pin_name)
            if pin_match is None:
                raise ValueError(f"invalid CARRY8 output request: {cell_name}/{pin_name}")
            lane = int(pin_match.group(2))
            if lane == 0:
                carry = self._input(cell_name, "CI")
            elif lane == 4 and cell.carry_type == "DUAL_CY4":
                carry = self._input(cell_name, "CI_TOP")
            else:
                carry = self.output(cell_name, f"CO[{lane - 1}]")
            select = self._input(cell_name, f"S[{lane}]")
            if pin_match.group(1) == "O":
                value = self.manager.xor(select, carry)
            else:
                data = self._input(cell_name, f"DI[{lane}]")
                value = self.manager.ite(select, carry, data)
        else:
            raise ValueError(f"unsupported constructive primitive {cell.ref}")
        self.output_cache[key] = value
        pin = self.graph.pins[cell_name].get(pin_name)
        if pin is not None and pin.net and pin.net not in self.boundary:
            self.net_cache[pin.net] = value
        return value


class ADDManager:
    """Integer-terminal ordered decision diagrams for compact carry recurrence."""

    def __init__(self, labels: tuple[str, ...], *, max_nodes: int):
        self.labels = labels
        self.max_nodes = max_nodes
        self.variables = [len(labels)]
        self.lows = [0]
        self.highs = [0]
        self.unique: dict[tuple[int, int, int], int] = {}
        self.add_cache: dict[tuple[int, int], int] = {}
        self.map_cache: dict[tuple[str, int], int] = {}

    @staticmethod
    def terminal(value: int) -> int:
        if value < 0:
            raise ValueError("ADD terminals must be nonnegative")
        return -value - 1

    @staticmethod
    def terminal_value(node: int) -> int:
        return -node - 1

    def variable_rank(self, node: int) -> int:
        return len(self.labels) if node < 0 else self.variables[node]

    def mk(self, variable: int, low: int, high: int) -> int:
        if low == high:
            return low
        key = (variable, low, high)
        prior = self.unique.get(key)
        if prior is not None:
            return prior
        if len(self.variables) >= self.max_nodes:
            raise BDDLimitError(f"ADD node cap {self.max_nodes} exceeded")
        node = len(self.variables)
        self.variables.append(variable)
        self.lows.append(low)
        self.highs.append(high)
        self.unique[key] = node
        return node

    def variable(self, ordinal: int) -> int:
        return self.mk(ordinal, self.terminal(0), self.terminal(1))

    def add(self, left: int, right: int) -> int:
        if left == self.terminal(0):
            return right
        if right == self.terminal(0):
            return left
        if left < 0 and right < 0:
            return self.terminal(self.terminal_value(left) + self.terminal_value(right))
        key = (left, right) if left <= right else (right, left)
        prior = self.add_cache.get(key)
        if prior is not None:
            return prior
        top = min(self.variable_rank(left), self.variable_rank(right))

        def cofactors(node: int) -> tuple[int, int]:
            if self.variable_rank(node) == top:
                return self.lows[node], self.highs[node]
            return node, node

        l0, l1 = cofactors(left)
        r0, r1 = cofactors(right)
        result = self.mk(top, self.add(l0, r0), self.add(l1, r1))
        self.add_cache[key] = result
        return result

    def map_values(self, node: int, operation: str) -> int:
        key = (operation, node)
        prior = self.map_cache.get(key)
        if prior is not None:
            return prior
        if node < 0:
            value = self.terminal_value(node)
            mapped = value >> 1 if operation == "half" else value & 1
            result = self.terminal(mapped)
        else:
            result = self.mk(
                self.variables[node],
                self.map_values(self.lows[node], operation),
                self.map_values(self.highs[node], operation),
            )
        self.map_cache[key] = result
        return result

    def to_bdd(self, node: int, manager: BDDManager) -> int:
        cache: dict[int, int] = {}

        def visit(current: int) -> int:
            prior = cache.get(current)
            if prior is not None:
                return prior
            if current < 0:
                value = self.terminal_value(current)
                if value not in (0, 1):
                    raise ValueError("only Boolean ADDs can be converted to BDDs")
                result = value
            else:
                result = manager.mk(
                    self.variables[current],
                    visit(self.lows[current]),
                    visit(self.highs[current]),
                )
            cache[current] = result
            return result

        return visit(node)

@dataclass(frozen=True)
class BoundaryWord:
    role: str
    stem: str
    signed_width: int
    dynamic_lsb: int
    dynamic_width: int
    nets_by_index: dict[int, str]


def _boundary_words(graph: ConeGraph) -> tuple[BoundaryWord, ...]:
    grouped: dict[str, dict[int, str]] = {}
    for net, cell in graph.boundary_nets.items():
        match = _INDEXED.fullmatch(cell)
        if match is None:
            raise ValueError(f"non-word accumulator boundary cell: {cell}")
        grouped.setdefault(match.group("stem"), {})[int(match.group("index"))] = net
    if not grouped:
        raise ValueError("CACC_FRONTIER_EMPTY: accumulator frontier has no words")
    ordered = sorted(grouped.items(), key=lambda row: (min(row[1]), row[0]))
    result = []
    for ordinal, (stem, members) in enumerate(ordered):
        indices = sorted(members)
        if indices != list(range(indices[0], indices[-1] + 1)):
            raise ValueError("accumulator dynamic boundary slice is not contiguous")
        result.append(BoundaryWord(
            role=f"input_{ordinal}",
            stem=stem,
            signed_width=indices[-1] + 1,
            dynamic_lsb=indices[0],
            dynamic_width=len(indices),
            nets_by_index=dict(members),
        ))
    return tuple(result)


def validate_constructive_shape(
    graph: ConeGraph,
    *,
    max_cone_cells: int = MAX_CONE_CELLS,
    max_boundary_bits: int = MAX_BOUNDARY_BITS,
    max_boundary_words: int = MAX_BOUNDARY_WORDS,
    max_target_bits: int = MAX_TARGET_BITS,
) -> tuple[BoundaryWord, ...]:
    """Validate the bounded generic LUT/CARRY registered-cone envelope."""
    words = _boundary_words(graph)
    shape = {
        "cone_cells": len(graph.cells),
        "boundary_bits": len(graph.boundary_nets),
        "boundary_words": len(words),
        "target_bits": len(graph.targets),
    }
    limits = {
        "cone_cells": max_cone_cells,
        "boundary_bits": max_boundary_bits,
        "boundary_words": max_boundary_words,
        "target_bits": max_target_bits,
    }
    exceeded = [name for name, value in shape.items() if not 1 <= value <= limits[name]]
    if exceeded:
        raise ValueError(
            "CACC_RESOURCE_BOUND_EXCEEDED: "
            + ", ".join(
                f"{name}={shape[name]} limit={limits[name]}" for name in exceeded
            )
        )
    if graph.side_fanout:
        raise ValueError("CACC_SIDE_FANOUT_UNCLOSED: accumulator cone has side consumers")
    supported = {
        "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6", "LUT6_2",
        "CARRY8", "GND", "VCC",
    }
    unsupported = sorted({
        cell.ref for cell in graph.cells.values() if cell.ref not in supported
    })
    if unsupported:
        raise ValueError("CACC_PRIMITIVE_UNSUPPORTED: " + ",".join(unsupported))
    _target_source_indices(graph)
    return words


def _target_source_indices(graph: ConeGraph) -> list[int]:
    result = []
    for target in graph.targets:
        match = re.search(r"\[(\d+)\]$", target["d_net"])
        if match is None:
            raise ValueError(f"target D net has no source index: {target['d_net']}")
        result.append(int(match.group(1)))
    if result != list(range(result[0], result[0] + len(result))):
        raise ValueError("target D source indices are not contiguous")
    return result


def _add(manager: BDDManager, left: list[int], right: list[int]) -> list[int]:
    if len(left) != len(right):
        raise ValueError("constructive adder widths differ")
    carry = 0
    result = []
    for a, b in zip(left, right):
        pair_xor = manager.xor(a, b)
        result.append(manager.xor(pair_xor, carry))
        carry = manager.or_(manager.and_(a, b), manager.and_(carry, pair_xor))
    return result


def _sign_extend(bits: list[int], width: int) -> list[int]:
    if not bits or width < len(bits):
        raise ValueError("invalid constructive sign extension")
    return [*bits, *([bits[-1]] * (width - len(bits)))]


def _constant_product(
    manager: BDDManager, bits: list[int], coefficient: int, width: int
) -> list[int]:
    if coefficient <= 0:
        raise ValueError("constructive accumulator currently requires a positive coefficient")
    extended = _sign_extend(bits, width)
    result = [0] * width
    for shift in range(coefficient.bit_length()):
        if (coefficient >> shift) & 1:
            shifted = [0] * shift + extended[: width - shift]
            result = _add(manager, result, shifted)
    return result


def _constant_product_plus(
    manager: BDDManager,
    multiplicand: list[int],
    addend: list[int],
    coefficient: int,
    width: int,
) -> list[int]:
    """Construct ``coefficient*multiplicand + addend`` one weighted column at a time.

    This is the word-level counterpart of a carry-save multiplier.  It avoids
    constructing several complete shifted ripple adders before the final sum;
    only the small integer carry between adjacent weight columns is retained.
    """
    if coefficient <= 0 or not multiplicand or not addend:
        raise ValueError("invalid constructive multiply-add operands")
    coefficient_bits = [
        bit for bit in range(coefficient.bit_length()) if (coefficient >> bit) & 1
    ]
    carry_width = max(3, (len(coefficient_bits) + 2).bit_length() + 1)
    carry = [0] * carry_width
    result = []

    def source(bits: list[int], index: int) -> int:
        if index < 0:
            return 0
        return bits[index] if index < len(bits) else bits[-1]

    def add_one(value: list[int], term: int) -> list[int]:
        propagated = term
        updated = []
        for bit in value:
            updated.append(manager.xor(bit, propagated))
            propagated = manager.and_(bit, propagated)
        return updated

    for column in range(width):
        total = list(carry)
        for coefficient_bit in coefficient_bits:
            total = add_one(total, source(multiplicand, column - coefficient_bit))
        total = add_one(total, source(addend, column))
        result.append(total[0])
        carry = [*total[1:], 0]
    return result


def _constant_product_plus_add(
    manager: BDDManager,
    label_ordinals: dict[str, int],
    multiplicand: BoundaryWord,
    addend: BoundaryWord,
    coefficient: int,
    width: int,
    *,
    max_nodes: int,
) -> list[int]:
    """Build a multiply-add through a compact integer-valued carry ADD."""
    add_manager = ADDManager(manager.labels, max_nodes=max_nodes)
    variables = {
        label: add_manager.variable(ordinal)
        for label, ordinal in label_ordinals.items()
    }

    def source(word: BoundaryWord, index: int) -> int:
        if index < 0:
            return add_manager.terminal(0)
        selected = index if index < word.signed_width else word.signed_width - 1
        net = word.nets_by_index.get(selected)
        if net is None:
            return add_manager.terminal(0)
        return variables[f"{word.role}[{selected}]"]

    coefficient_bits = [
        bit for bit in range(coefficient.bit_length()) if (coefficient >> bit) & 1
    ]
    carry = add_manager.terminal(0)
    result = []
    for column in range(width):
        total = carry
        for coefficient_bit in coefficient_bits:
            total = add_manager.add(total, source(multiplicand, column - coefficient_bit))
        total = add_manager.add(total, source(addend, column))
        result.append(add_manager.to_bdd(add_manager.map_values(total, "parity"), manager))
        carry = add_manager.map_values(total, "half")
    return result


def _python_expected(
    words: tuple[BoundaryWord, ...],
    word_coefficients: dict[str, int],
    values: dict[str, bool],
    *,
    output_lsb: int,
    output_width: int,
) -> int:
    def word_value(word: BoundaryWord) -> int:
        raw = sum(
            int(bool(values.get(net, False))) << index
            for index, net in word.nets_by_index.items()
        )
        if raw & (1 << (word.signed_width - 1)):
            raw -= 1 << word.signed_width
        return raw

    numerator = sum(
        int(word_coefficients[word.role]) * word_value(word) for word in words
    )
    return (numerator >> output_lsb) & ((1 << output_width) - 1)


def _recover_word_coefficients(
    graph: ConeGraph,
    words: tuple[BoundaryWord, ...],
    target_indices: list[int],
) -> dict[str, int]:
    """Recover each signed-word coefficient from exact one-hot cone probes.

    A one-hot bit ``i`` exposes coefficient bit ``output_lsb + k - i`` at
    output bit ``k``.  Combining every available non-sign bit reconstructs all
    coefficient bits that can affect this exact dynamic frontier.  The later
    ROBDD comparison remains the mutation authority and rejects any candidate
    discovery ambiguity or non-linear cone.
    """
    output_lsb = target_indices[0]
    output_width = len(target_indices)
    numerator_width = target_indices[-1] + 1
    all_nets = sorted(graph.boundary_nets)
    assignments = [tuple()]
    probe_nets = []
    for word in words:
        for index, net in sorted(word.nets_by_index.items()):
            if index == word.signed_width - 1:
                continue
            probe_nets.append((word, index, net))
            assignments.append((net,))
    observed = _evaluate_target_boolean_batch(graph, all_nets, assignments)
    if observed[0] != 0:
        raise ValueError(
            "CACC_NONZERO_CONSTANT_UNSUPPORTED: affine constant recovery is ambiguous"
        )
    coefficients: dict[str, int] = {}
    by_role: dict[str, list[tuple[int, int]]] = {word.role: [] for word in words}
    for (word, index, _net), value in zip(probe_nets, observed[1:], strict=True):
        by_role[word.role].append((index, value))
    for word in words:
        constraints: dict[int, int] = {}
        for index, value in by_role[word.role]:
            for output_bit in range(output_width):
                coefficient_bit = output_lsb + output_bit - index
                if not 0 <= coefficient_bit < numerator_width:
                    continue
                bit = (value >> output_bit) & 1
                prior = constraints.setdefault(coefficient_bit, bit)
                if prior != bit:
                    raise ValueError(
                        "CACC_COEFFICIENT_INFERENCE_CONFLICT: one-hot probes disagree"
                    )
        required = {
            output_lsb + output_bit - index
            for index in word.nets_by_index
            for output_bit in range(output_width)
            if 0 <= output_lsb + output_bit - index < numerator_width
        }
        missing = sorted(required - set(constraints))
        if missing:
            raise ValueError(
                "CACC_COEFFICIENT_INFERENCE_AMBIGUOUS: "
                f"{word.role} coefficient bits {missing[:8]} are not observable "
                "from a non-sign dynamic input"
            )
        coefficients[word.role] = sum(bit << index for index, bit in constraints.items())
    return coefficients


def _bit_coefficients(
    words: tuple[BoundaryWord, ...],
    word_coefficients: dict[str, int],
    width: int,
) -> tuple[dict[str, int], dict[str, str]]:
    modulus = 1 << width
    coefficients: dict[str, int] = {}
    source_nets: dict[str, str] = {}
    for word in words:
        coefficient = int(word_coefficients[word.role])
        for index, net in sorted(word.nets_by_index.items()):
            label = f"{word.role}[{index}]"
            weight = coefficient * (1 << index)
            if index == word.signed_width - 1:
                weight = -weight
            coefficients[label] = weight % modulus
            source_nets[label] = net
    return coefficients, source_nets


def _expected_from_compressor_schedule(
    manager: BDDManager,
    label_nodes: dict[str, int],
    coefficients: dict[str, int],
    width: int,
) -> list[int]:
    weighted_sum = {
        "modulus_width": width,
        "constant": 0,
        "coefficients": coefficients,
        "quadratic_coefficients": [],
        "polynomial_degree": 1,
        "zero_coefficient_count": sum(value == 0 for value in coefficients.values()),
    }
    spec = _compressor_schedule(width, 0, coefficients)
    if _verify_schedule_structure(weighted_sum, spec) is None:
        raise ValueError("CACC_LOWERING_STRUCTURE_REJECTED: compressor schedule is invalid")
    rows = _spec_rows(
        spec,
        {},
        lambda label: label_nodes[label],
        const=lambda value: int(bool(value)),
        xor3=lambda a, b, c: manager.xor(manager.xor(a, b), c),
        maj3=lambda a, b, c: manager.or_(
            manager.and_(a, b),
            manager.and_(c, manager.xor(a, b)),
        ),
        invert=manager.negate,
        and2=manager.and_,
    )
    return _add(manager, _add(manager, rows[0], rows[1]), rows[2])


def extract_constructive_accumulator(
    root: Path | str,
    *,
    coefficient: int | None = None,
    random_samples: int = 10_000,
    random_seed: int = 0xF1A26003,
    max_bdd_nodes: int = MAX_BDD_NODES_PER_OUTPUT,
    max_total_bdd_nodes: int = MAX_TOTAL_BDD_NODES,
) -> dict[str, Any]:
    """Recover and prove one bounded affine signed-word accumulator cone."""
    graph = ConeGraph.load(root)
    words = validate_constructive_shape(graph)
    target_indices = _target_source_indices(graph)
    output_lsb = target_indices[0]
    output_width = len(target_indices)
    if coefficient is None:
        word_coefficients = _recover_word_coefficients(graph, words, target_indices)
    else:
        if len(words) != 2:
            raise ValueError(
                "CACC_LEGACY_COEFFICIENT_SHAPE_UNSUPPORTED: explicit coefficient needs two words"
            )
        word_coefficients = {words[0].role: int(coefficient), words[1].role: 1}
    labels_and_nets = sorted(
        (
            (index, word.role, net, f"{word.role}[{index}]")
            for word in words
            for index, net in word.nets_by_index.items()
        ),
        key=lambda row: (row[0], row[1]),
    )
    labels = tuple(row[3] for row in labels_and_nets)
    label_ordinals = {label: ordinal for ordinal, label in enumerate(labels)}
    numerator_width = target_indices[-1] + 1
    actual_digests = []
    expected_digests = []
    mismatched_bits = []
    counterexample = None
    nodes_per_output = []
    for output_bit, (target, source_index) in enumerate(
        zip(graph.targets, target_indices)
    ):
        manager = BDDManager(labels, max_nodes=max_bdd_nodes)
        boundary_nodes = {
            net: manager.variable(ordinal)
            for ordinal, (_index, _role, net, _label) in enumerate(labels_and_nets)
        }
        composer = ConeBDDComposer(graph, manager, boundary_nodes)
        actual = composer.net(target["d_net"])

        try:
            if (
                len(words) == 2
                and word_coefficients[words[1].role] == 1
                and word_coefficients[words[0].role] > 0
            ):
                # Use a compact ADD carry recurrence for the common two-word
                # positive-constant-plus-add form.  The coefficient is freshly
                # recovered from this exact cone.
                expected_word = _constant_product_plus_add(
                    manager,
                    label_ordinals,
                    words[0],
                    words[1],
                    word_coefficients[words[0].role],
                    source_index + 1,
                    max_nodes=max_bdd_nodes,
                )
            else:
                output_coefficients, _source_nets = _bit_coefficients(
                    words, word_coefficients, source_index + 1
                )
                expected_word = _expected_from_compressor_schedule(
                    manager,
                    {
                        label: boundary_nodes[net]
                        for _index, _role, net, label in labels_and_nets
                    },
                    output_coefficients,
                    source_index + 1,
                )
        except BDDLimitError as exc:
            raise BDDLimitError(
                "CACC_ROBDD_NODE_LIMIT_EXCEEDED: "
                f"output bit {output_bit} (source bit {source_index}): {exc}"
            ) from exc
        expected = expected_word[source_index]
        actual_digests.append(manager.digest([actual]))
        expected_digests.append(manager.digest([expected]))
        if actual != expected:
            mismatched_bits.append(output_bit)
            if counterexample is None:
                counterexample = manager.counterexample(manager.xor(actual, expected))
        nodes_per_output.append(manager.node_count)
        if sum(nodes_per_output) > max_total_bdd_nodes:
            raise BDDLimitError(
                "CACC_ROBDD_TOTAL_LIMIT_EXCEEDED: aggregate independent-output "
                f"node count exceeds {max_total_bdd_nodes}"
            )

    rng = random.Random(random_seed)
    assignments: list[tuple[str, ...]] = []
    assignment_maps: list[dict[str, bool]] = []
    all_nets = sorted(graph.boundary_nets)
    for ordinal in range(random_samples):
        if ordinal == 0:
            values = {net: False for net in all_nets}
        elif ordinal == 1:
            values = {net: True for net in all_nets}
        else:
            values = {net: bool(rng.getrandbits(1)) for net in all_nets}
        assignment_maps.append(values)
        assignments.append(tuple(net for net in all_nets if values[net]))
    observed = _evaluate_target_boolean_batch(graph, all_nets, assignments)
    random_mismatches = []
    for ordinal, (values, netlist_value) in enumerate(zip(assignment_maps, observed)):
        expected_value = _python_expected(
            words,
            word_coefficients,
            values,
            output_lsb=output_lsb,
            output_width=output_width,
        )
        if expected_value != netlist_value:
            random_mismatches.append({
                "vector": ordinal,
                "netlist": netlist_value,
                "formula": expected_value,
            })
            if len(random_mismatches) == 8:
                break

    matched = not mismatched_bits
    actual_digest = hashlib.sha256("".join(actual_digests).encode("ascii")).hexdigest()
    expected_digest = hashlib.sha256("".join(expected_digests).encode("ascii")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "proved" if matched else "refuted",
        "source": {
            "dcp_sha256": graph.manifest["dcp_sha256"],
            "target_stem": graph.manifest["target_stem"],
            "connectivity_sha256": graph.connectivity_digest(),
            "cone_cell_count": len(graph.cells),
            "boundary_bit_count": len(graph.boundary_nets),
            "output_width": output_width,
        },
        "authority": {
            "method": "canonical_reduced_ordered_bdd_primitive_composition",
            "sat_or_smt_used_for_extraction": False,
            "all_outputs_canonically_compared": True,
        },
        "variable_order": list(labels),
        "frontier_words": [
            {
                "role": word.role,
                "signed_width": word.signed_width,
                "dynamic_lsb": word.dynamic_lsb,
                "dynamic_width": word.dynamic_width,
            }
            for word in words
        ],
        "derived_expression": {
            "kind": "signed_full_precision_sum_then_slice",
            "coefficient": (
                word_coefficients[words[0].role]
                if len(words) == 2 and word_coefficients[words[1].role] == 1
                else None
            ),
            "word_coefficients": dict(word_coefficients),
            "numerator": "+".join(
                f"signed({word.role})*{word_coefficients[word.role]}" for word in words
            ),
            "numerator_width": numerator_width,
            "slice_lsb": output_lsb,
            "slice_width": output_width,
            "overflow": "modular_slice",
        },
        "canonical_comparison": {
            "matched": matched,
            "mismatched_output_bits": mismatched_bits,
            "counterexample": counterexample,
            "actual_expression_sha256": actual_digest,
            "expected_expression_sha256": expected_digest,
            "per_output_actual_sha256": actual_digests,
            "nodes_per_output": nodes_per_output,
            "max_nodes_for_one_output": max(nodes_per_output),
            "total_nodes_over_independent_outputs": sum(nodes_per_output),
            "resource_limits": {
                "max_nodes_per_output": max_bdd_nodes,
                "max_total_nodes": max_total_bdd_nodes,
            },
        },
        "random_falsification_guard": {
            "samples": random_samples,
            "seed": random_seed,
            "mismatch_count": len(random_mismatches),
            "mismatches": random_mismatches,
            "passed": not random_mismatches,
            "evaluator": "packed_constructive_lut_carry_branch_evaluator",
        },
        "mutation_eligible": matched and not random_mismatches,
    }


def run_siblings(
    roots: list[Path], **kwargs: Any
) -> dict[str, Any]:
    rows = [extract_constructive_accumulator(root, **kwargs) for root in roots]
    expression_digests = {
        row["canonical_comparison"]["actual_expression_sha256"] for row in rows
    }
    all_proved = all(
        row["status"] == "proved"
        and row["random_falsification_guard"]["passed"]
        for row in rows
    )
    sibling_agreement = len(expression_digests) == 1
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "proved" if all_proved and sibling_agreement else "rejected",
        "sibling_count": len(rows),
        "sibling_expression_agreement": sibling_agreement,
        "siblings": rows,
        "mutation_eligible": all_proved and sibling_agreement,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--max-bdd-nodes", type=int, default=MAX_BDD_NODES_PER_OUTPUT)
    args = parser.parse_args()
    result = run_siblings(
        args.roots,
        random_samples=args.samples,
        max_bdd_nodes=args.max_bdd_nodes,
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0 if result["status"] == "proved" else 2


if __name__ == "__main__":
    raise SystemExit(main())
