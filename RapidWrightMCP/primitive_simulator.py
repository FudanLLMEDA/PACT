"""Cycle simulator for restricted LUT/CARRY/FD/SRL operator contracts."""

from __future__ import annotations

import re
from typing import Any


_BUS_BIT_RE = re.compile(r"^(?P<base>.*)\[(?P<index>\d+)\]$")
_LUT_RE = re.compile(r"LUT(?P<inputs>[1-6])$")
_LITERAL_RE = re.compile(
    r"(?:(?P<width>\d+)'[sS]?(?P<base>[bBdDhH]))?(?P<digits>[0-9a-fA-F]+)"
)


class PrimitiveSimulationError(ValueError):
    """Raised when the extracted primitive graph cannot be simulated exactly."""


def _parse_literal(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raw = str(value).strip().replace("_", "")
    if raw.lower() in {"false", "true"}:
        return int(raw.lower() == "true")
    match = _LITERAL_RE.fullmatch(raw)
    if match is None:
        raise PrimitiveSimulationError(f"unsupported primitive literal: {value}")
    base = {None: 10, "b": 2, "d": 10, "h": 16}[t := (
        match.group("base").lower() if match.group("base") else None
    )]
    del t
    return int(match.group("digits"), base)


def _constant_net(name: str) -> int | None:
    if name.endswith("/<const0>") or name in {"<const0>", "GLOBAL_LOGIC0"}:
        return 0
    if name.endswith("/<const1>") or name in {"<const1>", "GLOBAL_LOGIC1"}:
        return 1
    return None


def _pin_map(cell: dict[str, Any]) -> dict[str, str]:
    return {
        str(pin["pin"]): str(pin["canonical_net"])
        for pin in cell.get("pins", [])
        if pin.get("canonical_net") is not None
    }


def _input_value(pins: dict[str, str], values: dict[str, int], pin: str, default=None):
    net = pins.get(pin)
    if net is None:
        return default
    return values.get(net, default)


class PrimitiveContractSimulator:
    """Execute one extracted hierarchical operator at primitive granularity."""

    def __init__(self, contract: dict[str, Any]):
        if contract.get("kind") != "hierarchical_operator_contract":
            raise PrimitiveSimulationError("input is not a hierarchical operator contract")
        self.cells = list(contract["operator"]["cells"])
        self.boundary = list(contract["boundary"]["ports"])
        self.combinational = []
        self.fdres = []
        self.srls = []
        self.state: dict[str, int] = {}
        for cell in self.cells:
            cell_type = str(cell["type"]).upper()
            properties = cell.get("functional_properties", {})
            if cell_type == "FDRE":
                self.fdres.append(cell)
                self.state[cell["name"]] = _parse_literal(properties.get("INIT"), 0) & 1
            elif cell_type in {"SRL16E", "SRLC32E"}:
                self.srls.append(cell)
                width = 16 if cell_type == "SRL16E" else 32
                self.state[cell["name"]] = _parse_literal(
                    properties.get("INIT"), 0
                ) & ((1 << width) - 1)
            elif _LUT_RE.fullmatch(cell_type) or cell_type in {
                "CARRY8", "GND", "VCC", "MUXF7", "MUXF8", "MUXF9",
            }:
                self.combinational.append(cell)
            else:
                raise PrimitiveSimulationError(
                    f"unsupported operator primitive: {cell_type} ({cell['name']})"
                )
        self.input_ports = [
            port for port in self.boundary
            if port["direction"] == "input" and port.get("connected") is not False
        ]
        self.output_ports = [
            port for port in self.boundary
            if port["direction"] == "output" and port.get("connected") is not False
        ]

    def reset(self) -> None:
        for cell in self.fdres + self.srls:
            properties = cell.get("functional_properties", {})
            width = 1 if cell["type"] == "FDRE" else (16 if cell["type"] == "SRL16E" else 32)
            self.state[cell["name"]] = _parse_literal(
                properties.get("INIT"), 0
            ) & ((1 << width) - 1)

    def _boundary_inputs(self, inputs: dict[str, int]) -> dict[str, int]:
        values = {}
        for port in self.input_ports:
            net = port.get("canonical_net")
            if net is None:
                continue
            name = str(port["port"])
            match = _BUS_BIT_RE.fullmatch(name)
            if match is None:
                if name in inputs:
                    values[net] = int(inputs[name]) & 1
                elif "clk" in name.lower():
                    values[net] = 0
                continue
            base = match.group("base")
            if base in inputs:
                values[net] = (int(inputs[base]) >> int(match.group("index"))) & 1
            elif name in inputs:
                values[net] = int(inputs[name]) & 1
        return values

    def _drive_constants_and_state(self, values: dict[str, int]) -> None:
        for cell in self.cells:
            pins = _pin_map(cell)
            for net in pins.values():
                constant = _constant_net(net)
                if constant is not None:
                    values[net] = constant
        for cell in self.fdres:
            q_net = _pin_map(cell).get("Q")
            if q_net is not None:
                values[q_net] = self.state[cell["name"]]
        for cell in self.srls:
            pins = _pin_map(cell)
            address_width = 4 if cell["type"] == "SRL16E" else 5
            address = 0
            for index in range(address_width):
                value = _input_value(
                    pins, values, f"A[{index}]",
                    _input_value(pins, values, f"A{index}"),
                )
                if value is None:
                    break
                address |= value << index
            else:
                q_net = pins.get("Q")
                if q_net is not None:
                    values[q_net] = (self.state[cell["name"]] >> address) & 1
                q31_net = pins.get("Q31")
                if q31_net is not None:
                    values[q31_net] = (self.state[cell["name"]] >> 31) & 1

    @staticmethod
    def _assign(values: dict[str, int], net: str | None, value: int, cell: str) -> None:
        if net is None:
            return
        bit = value & 1
        previous = values.get(net)
        if previous is not None and previous != bit:
            raise PrimitiveSimulationError(
                f"multiple logical values drive {net} while evaluating {cell}"
            )
        values[net] = bit

    def _evaluate_cell(self, cell: dict[str, Any], values: dict[str, int]) -> bool:
        cell_type = str(cell["type"]).upper()
        pins = _pin_map(cell)
        if cell_type == "GND":
            self._assign(values, pins.get("G"), 0, cell["name"])
            return True
        if cell_type == "VCC":
            self._assign(values, pins.get("P"), 1, cell["name"])
            return True
        lut_match = _LUT_RE.fullmatch(cell_type)
        if lut_match is not None:
            width = int(lut_match.group("inputs"))
            inputs = [_input_value(pins, values, f"I{index}") for index in range(width)]
            if any(value is None for value in inputs):
                return False
            index = sum(int(bit) << offset for offset, bit in enumerate(inputs))
            init = _parse_literal(cell.get("functional_properties", {}).get("INIT"))
            self._assign(values, pins.get("O"), (init >> index) & 1, cell["name"])
            return True
        if cell_type.startswith("MUXF"):
            select = _input_value(pins, values, "S")
            left = _input_value(pins, values, "I0")
            right = _input_value(pins, values, "I1")
            if None in {select, left, right}:
                return False
            self._assign(values, pins.get("O"), right if select else left, cell["name"])
            return True
        if cell_type == "CARRY8":
            connected_outputs = [
                int(match.group(1))
                for pin_name in pins
                if (match := re.fullmatch(r"(?:O|CO)\[(\d+)\]", pin_name))
            ]
            if not connected_outputs:
                raise PrimitiveSimulationError(
                    f"CARRY8 has no connected outputs: {cell['name']}"
                )
            active_width = max(connected_outputs) + 1
            select = [
                _input_value(pins, values, f"S[{index}]")
                for index in range(active_width)
            ]
            data = [
                _input_value(pins, values, f"DI[{index}]")
                for index in range(active_width)
            ]
            carry = values.get(pins["CI"]) if "CI" in pins else 0
            carry_top = values.get(pins["CI_TOP"]) if "CI_TOP" in pins else 0
            if carry is None or any(value is None for value in select + data):
                return False
            carry_type = str(
                cell.get("functional_properties", {}).get("CARRY_TYPE", "SINGLE_CY8")
            ).upper()
            for index in range(active_width):
                if index == 4 and carry_type == "DUAL_CY4":
                    carry = carry_top
                self._assign(
                    values, pins.get(f"O[{index}]"), select[index] ^ carry, cell["name"]
                )
                carry = carry if select[index] else data[index]
                self._assign(
                    values, pins.get(f"CO[{index}]"), carry, cell["name"]
                )
            return True
        return False

    def _settle(self, inputs: dict[str, int]) -> dict[str, int]:
        values = self._boundary_inputs(inputs)
        self._drive_constants_and_state(values)
        pending = list(self.combinational)
        while pending:
            remaining = []
            progressed = False
            for cell in pending:
                if self._evaluate_cell(cell, values):
                    progressed = True
                else:
                    remaining.append(cell)
            if not progressed:
                unresolved = sorted(
                    remaining,
                    key=lambda cell: sum(
                        pin.get("direction") == "input"
                        and pin.get("canonical_net") not in values
                        for pin in cell.get("pins", [])
                    ),
                )
                samples = []
                for cell in unresolved[:8]:
                    unknown = [
                        f"{pin['pin']}={pin['canonical_net']}"
                        for pin in cell.get("pins", [])
                        if pin.get("direction") == "input"
                        and pin.get("canonical_net") not in values
                    ]
                    samples.append(
                        f"{cell['name']}:{cell['type']} unknown[{','.join(unknown)}]"
                    )
                raise PrimitiveSimulationError(
                    "combinational graph did not settle; "
                    f"{len(remaining)} cells unresolved: {'; '.join(samples)}"
                )
            pending = remaining
        return values

    @staticmethod
    def _control(cell, pins, values, name: str, default: int) -> int:
        value = _input_value(pins, values, name, default)
        inverted = _parse_literal(
            cell.get("functional_properties", {}).get(f"IS_{name}_INVERTED"), 0
        )
        return int(value) ^ (inverted & 1)

    def _clock_edge(self, values: dict[str, int]) -> None:
        next_state = dict(self.state)
        for cell in self.fdres:
            pins = _pin_map(cell)
            data = _input_value(pins, values, "D")
            if data is None:
                raise PrimitiveSimulationError(f"FDRE D is unresolved: {cell['name']}")
            reset = self._control(cell, pins, values, "R", 0)
            enable = self._control(cell, pins, values, "CE", 1)
            if reset:
                next_state[cell["name"]] = 0
            elif enable:
                next_state[cell["name"]] = data
        for cell in self.srls:
            pins = _pin_map(cell)
            data = _input_value(pins, values, "D")
            if data is None:
                raise PrimitiveSimulationError(f"SRL D is unresolved: {cell['name']}")
            enable = self._control(cell, pins, values, "CE", 1)
            if enable:
                width = 16 if cell["type"] == "SRL16E" else 32
                next_state[cell["name"]] = (
                    (self.state[cell["name"]] << 1) | data
                ) & ((1 << width) - 1)
        self.state = next_state

    def _outputs(self, values: dict[str, int]) -> dict[str, int]:
        result = {}
        widths = {}
        for port in self.output_ports:
            name = str(port["port"])
            net = port.get("canonical_net")
            if net not in values:
                raise PrimitiveSimulationError(f"operator output is unresolved: {name}")
            match = _BUS_BIT_RE.fullmatch(name)
            if match is None:
                result[name] = values[net]
                continue
            base = match.group("base")
            index = int(match.group("index"))
            result[base] = result.get(base, 0) | (values[net] << index)
            widths[base] = max(widths.get(base, 0), index + 1)
        result["_widths"] = widths
        return result

    def step(self, inputs: dict[str, int]) -> dict[str, int]:
        before = self._settle(inputs)
        self._clock_edge(before)
        after = self._settle(inputs)
        return self._outputs(after)
