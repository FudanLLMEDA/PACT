"""Exact LUT/CARRY graph replay used by DCP semantic recovery probes."""

from __future__ import annotations

from collections import defaultdict
import csv
from pathlib import Path
import re
from typing import Mapping


_INIT = re.compile(r"(\d+)'h([0-9a-fA-F]+)\Z")
_INDEX = re.compile(r"(?:O|CO)\[(\d+)\]\Z")


class PrimitiveGraph:
    """Combinational graph with sequential Q pins treated as state inputs."""

    def __init__(self, path: Path | str):
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        self.pins: dict[str, dict[str, str]] = defaultdict(dict)
        self.refs: dict[str, str] = {}
        self.inits: dict[str, str] = {}
        self.sequential: set[str] = set()
        self.drivers: dict[str, tuple[str, str]] = {}
        self.targets: list[tuple[str, str]] = []
        self.net_keys: set[str] = set()
        for row in rows:
            if row.get("kind") == "target":
                self.targets.append((row["target"], row["net_key"]))
                continue
            if row.get("kind") != "pin":
                continue
            cell = row["cell"]
            pin = row["ref_pin"]
            net = row["net_key"]
            self.pins[cell][pin] = net
            self.refs[cell] = row["ref"]
            self.inits[cell] = row["init"]
            if row["sequential"] == "1":
                self.sequential.add(cell)
            if net:
                self.net_keys.add(net)
            if row["direction"] == "OUT" and net:
                previous = self.drivers.setdefault(net, (cell, pin))
                if previous != (cell, pin):
                    raise ValueError(f"net has multiple primitive drivers: {net}")

    def key_for_alias(self, alias: str) -> str:
        matches = [key for key in self.net_keys if alias in key.split(",")]
        if len(matches) != 1:
            raise ValueError(f"net alias is missing or ambiguous: {alias}")
        return matches[0]

    def q_net(self, cell: str) -> str:
        net = self.pins.get(cell, {}).get("Q", "")
        if cell not in self.sequential or not net:
            raise ValueError(f"sequential Q net is unavailable: {cell}")
        return net

    def indexed_register_groups(self) -> dict[str, dict[int, str]]:
        groups: dict[str, dict[int, str]] = defaultdict(dict)
        for cell in self.sequential:
            match = re.fullmatch(r"(.+)_reg\[(\d+)\]", cell)
            if match:
                groups[match.group(1)][int(match.group(2))] = cell
        return dict(groups)

    def evaluate(
        self,
        nets: Mapping[str, int],
        *,
        state: Mapping[str, int] | None = None,
        overrides: Mapping[str, int] | None = None,
    ) -> dict[str, int]:
        state = state or {}
        overrides = overrides or {}
        cache: dict[str, int] = {}

        def value(net: str) -> int:
            if net in overrides:
                return int(bool(overrides[net]))
            if net in cache:
                return cache[net]
            if "<const0>" in net or "GND" in net:
                cache[net] = 0
                return 0
            if "<const1>" in net or "VCC" in net:
                cache[net] = 1
                return 1
            driver = self.drivers.get(net)
            if driver is None:
                cache[net] = int(bool(overrides.get(net, 0)))
                return cache[net]
            cell, pin = driver
            if cell in self.sequential:
                cache[net] = int(bool(state.get(cell, 0)))
                return cache[net]
            ref = self.refs[cell]

            def source(name: str) -> int:
                return value(self.pins[cell].get(name, ""))

            if ref.startswith("LUT"):
                match = _INIT.fullmatch(self.inits[cell])
                if match is None or int(match.group(1)) != 1 << int(ref[3:]):
                    raise ValueError(f"invalid LUT INIT: {cell}")
                address = sum(
                    source(f"I{index}") << index for index in range(int(ref[3:]))
                )
                result = (int(match.group(2), 16) >> address) & 1
            elif ref == "MUXF7":
                result = source("I1") if source("S") else source("I0")
            elif ref == "CARRY8":
                match = _INDEX.fullmatch(pin)
                if match is None:
                    raise ValueError(f"unsupported CARRY8 output: {cell}/{pin}")
                output_index = int(match.group(1))
                carry = source("CI")
                result = 0
                for index in range(output_index + 1):
                    select = source(f"S[{index}]")
                    data = source(f"DI[{index}]")
                    carry_out = carry if select else data
                    if index == output_index:
                        result = select ^ carry if pin.startswith("O[") else carry_out
                    carry = carry_out
            else:
                raise ValueError(f"unsupported primitive in semantic cone: {ref}")
            cache[net] = result
            return result

        return {name: value(net) for name, net in nets.items()}


__all__ = ["PrimitiveGraph"]
