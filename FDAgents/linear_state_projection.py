"""Exact read-only recovery for registered GF(2)-linear observations.

The extractor composes LUT truth tables from a registered output boundary back
to sequential/top-port startpoints.  It proves an affine observation matrix;
it does not yet authorize projection through mux/storage state or emit a
mutation.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_LUT = re.compile(r"LUT([1-6])(?:_2)?$")
_INDEX = re.compile(r"^(?P<stem>.+)\[(?P<index>\d+)\](?:_reg(?:_\d+)?)?$")
_INPUT_BOUNDARY_PRIMITIVES = {"IBUF", "IBUFCTRL", "INBUF"}
_INDEX_TOKEN = re.compile(r"\[\d+\]")


class LinearObservationError(ValueError):
    """Raised when the registered observation is not exactly affine GF(2)."""


@dataclass(frozen=True, slots=True)
class AffineExpression:
    constant: int
    variables: frozenset[str]

    def xor(self, other: "AffineExpression") -> "AffineExpression":
        return AffineExpression(
            self.constant ^ other.constant,
            self.variables.symmetric_difference(other.variables),
        )


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _parse_init(value: str) -> int:
    text = str(value or "").strip().lower().replace("_", "")
    match = re.fullmatch(r"\d+'h([0-9a-f]+)", text)
    if match is None:
        match = re.fullmatch(r"0x([0-9a-f]+)", text)
    if match is None:
        raise LinearObservationError(f"unsupported LUT INIT {value!r}")
    return int(match.group(1), 16)


def _local_affine_coefficients(init: int, width: int) -> tuple[int, list[int]]:
    constant = init & 1
    coefficients = [((init >> (1 << index)) & 1) ^ constant for index in range(width)]
    for address in range(1 << width):
        expected = constant
        for index, coefficient in enumerate(coefficients):
            if coefficient and (address >> index) & 1:
                expected ^= 1
        if ((init >> address) & 1) != expected:
            raise LinearObservationError("LUT truth table is not affine over GF(2)")
    return constant, coefficients


def _gf2_rank(rows: list[int]) -> int:
    basis: dict[int, int] = {}
    for raw in rows:
        value = int(raw)
        while value:
            pivot = value.bit_length() - 1
            if pivot not in basis:
                basis[pivot] = value
                break
            value ^= basis[pivot]
    return len(basis)


def _private_indexed_groups(
    boundary_names: dict[str, str], variables: list[str]
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[int, tuple[str, str]]] = {}
    for net in variables:
        name = boundary_names.get(net, "")
        match = _INDEX.fullmatch(name)
        if match is None:
            continue
        grouped.setdefault(match.group("stem"), {})[
            int(match.group("index"))
        ] = (name, net)
    rows = []
    for stem, members in grouped.items():
        ordered = sorted(members)
        if ordered == list(range(ordered[0], ordered[-1] + 1)):
            rows.append({
                "stem": stem,
                "width": len(ordered),
                "index_lsb": ordered[0],
                "index_msb": ordered[-1],
                "cells": [members[index][0] for index in ordered],
                "nets": [members[index][1] for index in ordered],
            })
    rows.sort(key=lambda row: (-row["width"], row["index_lsb"], row["stem"]))
    for ordinal, row in enumerate(rows, 1):
        row["family_ref"] = f"gf2_boundary_{ordinal}"
    return rows


def _anonymous_owner_shape(name: str) -> str:
    """Return a stable structural shape without exposing instance indices."""

    return _INDEX_TOKEN.sub("[*]", str(name or ""))


def summarize_live_registered_owners(
    owner_template_path: Path | str,
    timing_census_path: Path | str,
) -> dict[str, Any]:
    """Align live timing endpoints with registered observation-cone owners.

    The observation-cone owner inventory contains registered *startpoints*.
    A live wall may instead terminate at the next registered stage.  This
    summary makes that coverage gap explicit and nominates one live endpoint
    owner for a focused follow-up probe.  Exact names remain private.
    """

    with Path(owner_template_path).open("r", encoding="utf-8", newline="") as stream:
        owners = [
            row for row in csv.DictReader(stream, delimiter="\t")
            if row.get("kind") == "family" and row.get("exact_stem_private")
        ]
    with Path(timing_census_path).open("r", encoding="utf-8", newline="") as stream:
        paths = [
            row for row in csv.DictReader(stream, delimiter="\t")
            if row.get("kind") == "path" and row.get("end_stem")
        ]
    if not owners:
        raise LinearObservationError("registered owner template inventory is empty")
    if not paths:
        raise LinearObservationError("live timing endpoint census is empty")

    owner_by_stem = {row["exact_stem_private"]: row for row in owners}
    exact_matches = sum(row["end_stem"] in owner_by_stem for row in paths)
    by_shape: dict[str, dict[str, Any]] = {}
    for row in paths:
        stem = row["end_stem"]
        shape = _anonymous_owner_shape(stem)
        item = by_shape.setdefault(shape, {
            "stems": set(), "paths": [], "representative_cell": row.get("end_cell", ""),
        })
        item["stems"].add(stem)
        item["paths"].append(row)

    ranked = sorted(
        by_shape.items(),
        key=lambda item: (
            -len(item[1]["paths"]),
            -sum(str(row.get("end_pin", "")).rsplit("/", 1)[-1] == "D" for row in item[1]["paths"]),
            item[0],
        ),
    )
    public_shapes = []
    private_shapes = []
    nominated_private: dict[str, Any] | None = None
    for ordinal, (shape, item) in enumerate(ranked, 1):
        rows = item["paths"]
        data_count = sum(str(row.get("end_pin", "")).rsplit("/", 1)[-1] == "D" for row in rows)
        control_count = len(rows) - data_count
        matched_widths = sorted({
            int(owner_by_stem[stem]["width"])
            for stem in item["stems"] if stem in owner_by_stem
            and str(owner_by_stem[stem].get("width", "")).isdigit()
        })
        ref = f"live_owner_shape_{ordinal}"
        public_shapes.append({
            "owner_ref": ref,
            "observed_path_count": len(rows),
            "unique_owner_instances": len(item["stems"]),
            "setup_data_path_count": data_count,
            "control_path_count": control_count,
            "worst_slack_ns": min(float(row["slack_ns"]) for row in rows),
            "observation_template_widths": matched_widths,
            "exact_template_match": bool(matched_widths),
        })
        private_row = {
            "owner_ref": ref,
            "structural_shape": shape,
            "exact_stems": sorted(item["stems"]),
            "representative_cell": item["representative_cell"],
            "path_ordinals": [int(row["ordinal"]) for row in rows],
        }
        private_shapes.append(private_row)
        if nominated_private is None and data_count and item["representative_cell"]:
            nominated_private = private_row

    if nominated_private is None:
        raise LinearObservationError("live timing census has no registered data endpoint")
    private = {
        "owner_shapes": private_shapes,
        "nominated_owner_ref": nominated_private["owner_ref"],
        "nominated_representative_cell": nominated_private["representative_cell"],
    }
    public = {
        "kind": "live_registered_owner_alignment",
        "schema_version": 1,
        "status": "focused_probe_required",
        "authority": "classification_and_followup_probe_only",
        "mutation_eligible": False,
        "timing_path_count": len(paths),
        "exact_observation_template_path_count": exact_matches,
        "uncovered_live_endpoint_path_count": len(paths) - exact_matches,
        "live_owner_shapes": public_shapes,
        "nominated_owner_ref": nominated_private["owner_ref"],
        "required_before_mutation": [
            "complete registered owner word recovery",
            "exact data mux and storage recurrence",
            "clock CE reset init and valid-ready arbitration closure",
            "dynamic-slice occupancy and side-consumer closure",
            "invertible state-basis and transformed next-state proof",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


def write_live_owner_probe_manifest(recovered: dict[str, Any], path: Path | str) -> None:
    private = recovered.get("private_manifest") or {}
    owner_ref = str(private.get("nominated_owner_ref") or "")
    cell = str(private.get("nominated_representative_cell") or "")
    if not re.fullmatch(r"live_owner_shape_\d+", owner_ref) or not cell:
        raise LinearObservationError("nominated live owner is absent")
    if any(character in cell for character in "\t\r\n"):
        raise LinearObservationError("unsafe live owner representative cell")
    Path(path).write_text(
        "owner_ref\trepresentative_cell\n" + f"{owner_ref}\t{cell}\n",
        encoding="utf-8",
    )


def _parse_ref_census(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in filter(None, str(value or "").split(",")):
        ref, separator, count = item.rpartition(":")
        if not separator or not ref or not count.isdigit():
            raise LinearObservationError("invalid owner bit primitive census")
        result[ref] = int(count)
    return result


def recover_live_registered_owner_contract(path: Path | str) -> dict[str, Any]:
    """Recover an anonymous data/control shape from one live owner probe."""

    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    summary = {
        row["key"]: row["value"] for row in rows if row.get("kind") == "summary"
    }
    required = {
        "index_lsb", "index_msb", "contiguous", "control_class_count",
        "cone_cell_count", "startpoint_count",
    }
    if not required.issubset(summary) or summary["contiguous"] != "1":
        raise LinearObservationError("live registered owner summary is incomplete")
    lsb, msb = int(summary["index_lsb"]), int(summary["index_msb"])
    width = msb - lsb + 1
    bit_rows = {
        int(row["index"]): row for row in rows
        if row.get("kind") == "bit_cone_signature"
    }
    if sorted(bit_rows) != list(range(lsb, msb + 1)):
        raise LinearObservationError("live registered owner bit inventory is incomplete")

    global_sources = []
    source_by_private_key: dict[tuple[str, str], str] = {}
    start_rows = [row for row in rows if row.get("kind") == "start_family_width"]
    start_rows.sort(key=lambda row: (-int(row["value"]), row["ref"], row["stem_private"]))
    for ordinal, row in enumerate(start_rows, 1):
        source_ref = f"source_word_{ordinal}"
        key = (row["stem_private"], row["ref"])
        source_by_private_key[key] = source_ref
        global_sources.append({
            "source_ref": source_ref,
            "observed_width": int(row["value"]),
            "primitive": row["ref"],
        })

    per_bit_sources: dict[int, list[dict[str, Any]]] = {
        index: [] for index in range(lsb, msb + 1)
    }
    for row in rows:
        kind = row.get("kind")
        if kind not in {"bit_start_family", "bit_start_nonindexed"}:
            continue
        index = int(row["index"])
        if index not in per_bit_sources:
            raise LinearObservationError("owner bit source index is outside the word")
        if kind == "bit_start_family":
            key = (row["stem_private"], row["ref"])
            source_ref = source_by_private_key.get(key)
            if source_ref is None:
                # A source absent from the whole-word startpoint summary means
                # the focused probe is internally inconsistent.
                raise LinearObservationError("owner bit source family is unindexed")
            per_bit_sources[index].append({
                "kind": "indexed", "source_ref": source_ref,
                "ref": row["ref"], "count": int(row["value"]),
                "private_stem": row["stem_private"],
            })
        else:
            per_bit_sources[index].append({
                "kind": "nonindexed", "source_ref": None,
                "ref": row["ref"], "count": int(row["value"]),
                "private_stem": "",
            })

    bit_private = []
    shape_keys: dict[int, tuple[Any, ...]] = {}
    for index in range(lsb, msb + 1):
        refs = _parse_ref_census(bit_rows[index]["value"])
        sources = per_bit_sources[index]
        source_identity = tuple(sorted(
            (item["kind"], item["ref"], item["private_stem"])
            for item in sources
        ))
        shape_keys[index] = (tuple(sorted(refs)), source_identity)
        bit_private.append({
            "index": index, "primitive_census": refs, "sources": sources,
        })

    raw_regions: list[tuple[int, int, tuple[Any, ...]]] = []
    region_lsb = lsb
    previous_key = shape_keys[lsb]
    for index in range(lsb + 1, msb + 1):
        if shape_keys[index] != previous_key:
            raw_regions.append((region_lsb, index - 1, previous_key))
            region_lsb, previous_key = index, shape_keys[index]
    raw_regions.append((region_lsb, msb, previous_key))
    dominant = max(raw_regions, key=lambda row: row[1] - row[0] + 1)
    public_regions = []
    private_regions = []
    for ordinal, (region_start, region_end, key) in enumerate(raw_regions, 1):
        sources = per_bit_sources[region_start]
        public_regions.append({
            "region_ref": f"bit_region_{ordinal}",
            "index_lsb": region_start,
            "index_msb": region_end,
            "width": region_end - region_start + 1,
            "candidate_role": (
                "selected_storage_data_candidate" if (region_start, region_end, key) == dominant
                else "transaction_sideband_candidate"
            ),
            "primitive_classes": list(key[0]),
            "indexed_source_words": sorted({
                item["source_ref"] for item in sources if item["source_ref"]
            }),
            "has_top_port_boundary": any(
                item["kind"] == "nonindexed" and item["ref"] == "PORT"
                for item in sources
            ),
        })
        private_regions.append({
            "region_ref": f"bit_region_{ordinal}",
            "indices": list(range(region_start, region_end + 1)),
            "shape_key": key,
        })

    private = {
        "summary": summary,
        "control_classes": [
            {"count": int(row["value"]), "signature": row["stem_private"]}
            for row in rows if row.get("kind") == "control_class"
        ],
        "global_sources": [
            {
                **public,
                "private_stem": row["stem_private"],
            }
            for public, row in zip(global_sources, start_rows)
        ],
        "bits": bit_private,
        "regions": private_regions,
    }
    public = {
        "kind": "live_registered_selected_storage_contract",
        "schema_version": 1,
        "status": "control_data_contract_incomplete",
        "authority": "nomination_and_followup_proof_only",
        "mutation_eligible": False,
        "word_width": width,
        "control_class_count": int(summary["control_class_count"]),
        "cone_cell_count": int(summary["cone_cell_count"]),
        "startpoint_count": int(summary["startpoint_count"]),
        "registered_source_words": global_sources,
        "bit_regions": public_regions,
        "candidate_family": "linear_consumer_state_basis_projection_v1",
        "required_before_mutation": [
            "exact observable projection matrix at the transaction boundary",
            "proof that select storage arbitration and sideband do not inspect eliminated data dimensions",
            "complete CE reset init valid-ready latency and side-consumer replay",
            "dynamic-slice occupancy preservation",
            "invertible transformed next-state proof with direct control-visible coordinates",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


def _decode_compact_pins(value: str) -> dict[str, tuple[int, str]]:
    result = {}
    for item in filter(None, str(value or "").split(",")):
        role, separator, tail = item.partition("=")
        identifier, colon, direction = tail.partition(":")
        if not separator or not colon or not role or not identifier.isdigit():
            raise LinearObservationError("invalid compact GF2 pin record")
        result[role] = (int(identifier), direction)
    return result


def _truth_support(table: int, width: int, *, fixed: dict[int, int] | None = None) -> set[int]:
    fixed = fixed or {}
    support = set()
    for variable in range(width):
        if variable in fixed:
            continue
        others = [index for index in range(width) if index != variable and index not in fixed]
        for bits in range(1 << len(others)):
            address = sum(int(value) << index for index, value in fixed.items())
            for ordinal, index in enumerate(others):
                address |= ((bits >> ordinal) & 1) << index
            if ((table >> address) & 1) != ((table >> (address | (1 << variable))) & 1):
                support.add(variable)
                break
    return support


def _simple_mux_roles(table: int, width: int) -> tuple[int, set[int]] | None:
    """Return select and data-variable roles for a mux/gate truth table."""

    choices = []
    for select in range(width):
        low = _truth_support(table, width, fixed={select: 0})
        high = _truth_support(table, width, fixed={select: 1})
        if len(low) <= 1 and len(high) <= 1:
            data = low | high
            if data and select not in data:
                choices.append((select, data))
    if not choices:
        return None
    choices.sort(key=lambda item: (-len(item[1]), item[0], tuple(sorted(item[1]))))
    return choices[0]


def recover_compact_selected_storage_roles(path: Path | str) -> dict[str, Any]:
    """Classify exact affine and mux roles in a compact registered cone."""

    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    producers: dict[int, tuple[str, dict[str, str], str]] = {}
    cells: dict[str, dict[str, Any]] = {}
    boundaries: dict[int, dict[str, str]] = {}
    ports: dict[int, dict[str, str]] = {}
    targets = []
    for row in rows:
        kind = row.get("kind")
        pins = _decode_compact_pins(row.get("pins", ""))
        if kind == "cell":
            cell_key = row["id"]
            cells[cell_key] = {**row, "decoded_pins": pins}
            for role, (identifier, direction) in pins.items():
                if direction == "OUT":
                    if identifier in producers:
                        raise LinearObservationError("compact GF2 net has multiple drivers")
                    producers[identifier] = ("cell", row, role)
        elif kind == "boundary":
            for role, (identifier, direction) in pins.items():
                if direction == "OUT":
                    producers[identifier] = ("boundary", row, role)
                    boundaries[identifier] = row
        elif kind == "port":
            identifier = int(row["id"])
            producers[identifier] = ("port", row, "PORT")
            ports[identifier] = row
        elif kind == "target":
            targets.append((int(row["target_index"]), int(row["id"])))
    targets.sort()
    if not targets or [row[0] for row in targets] != list(range(len(targets))):
        raise LinearObservationError("compact GF2 target inventory is incomplete")

    data_leaves: set[int] = set()
    control_leaves: set[int] = set()
    visited: set[tuple[int, str]] = set()
    affine_cells: set[str] = set()
    mux_cells: set[str] = set()
    control_cells: set[str] = set()
    unresolved: dict[str, dict[str, str]] = {}

    def walk(identifier: int, context: str) -> None:
        key = (identifier, context)
        if key in visited:
            return
        visited.add(key)
        producer = producers.get(identifier)
        if producer is None:
            unresolved[f"net_{identifier}"] = {"ref": "NO_DRIVER", "init": ""}
            return
        kind, row, output_role = producer
        if kind in {"boundary", "port"}:
            (data_leaves if context == "data" else control_leaves).add(identifier)
            return
        cell_key = row["id"]
        ref = row["ref"].upper()
        match = re.fullmatch(r"LUT([1-6])(?:_2)?", ref)
        if match is None:
            unresolved[cell_key] = {"ref": ref, "init": row["init"]}
            return
        width = int(match.group(1))
        if ref.endswith("_2") and output_role == "O5":
            width = min(width, 5)
        table = _parse_init(row["init"])
        if ref.endswith("_2") and output_role == "O5":
            table &= (1 << 32) - 1
        pins = cells[cell_key]["decoded_pins"]
        sensitive = _truth_support(table, width)

        def input_net(index: int) -> int:
            record = pins.get(f"I{index}")
            if record is None or record[1] != "IN":
                raise LinearObservationError("compact GF2 LUT input is absent")
            return record[0]

        if context == "control":
            control_cells.add(cell_key)
            for index in sensitive:
                walk(input_net(index), "control")
            return
        try:
            _constant, coefficients = _local_affine_coefficients(table, width)
        except LinearObservationError:
            coefficients = []
        if coefficients:
            affine_cells.add(cell_key)
            for index, coefficient in enumerate(coefficients):
                if coefficient:
                    walk(input_net(index), "data")
            return
        mux = _simple_mux_roles(table, width)
        if mux is None:
            unresolved[cell_key] = {"ref": ref, "init": row["init"]}
            return
        select, data_inputs = mux
        mux_cells.add(cell_key)
        walk(input_net(select), "control")
        for index in data_inputs:
            walk(input_net(index), "data")

    for _index, identifier in targets:
        walk(identifier, "data")

    overlap = data_leaves & control_leaves

    def boundary_shapes(identifiers: set[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[str, dict[str, Any]] = {}
        private_rows = []
        for identifier in sorted(identifiers):
            row = boundaries.get(identifier) or ports.get(identifier)
            name = row.get("private_name", "") if row else ""
            match = _INDEX.fullmatch(name)
            stem = match.group("stem") if match else name
            index = int(match.group("index")) if match else None
            shape = _anonymous_owner_shape(stem)
            item = grouped.setdefault(shape, {"owners": set(), "indices": set(), "refs": set()})
            item["owners"].add(stem)
            if index is not None:
                item["indices"].add(index)
            if row:
                item["refs"].add(row.get("ref", ""))
            private_rows.append({"net_id": identifier, "name": name, "stem": stem, "index": index})
        public_rows = []
        shape_refs = {}
        for ordinal, (shape, item) in enumerate(sorted(
            grouped.items(), key=lambda pair: (-len(pair[1]["indices"]), -len(pair[1]["owners"]), pair[0])
        ), 1):
            indices = sorted(item["indices"])
            shape_refs[shape] = f"boundary_shape_{ordinal}"
            public_rows.append({
                "boundary_ref": f"boundary_shape_{ordinal}",
                "owner_instance_count": len(item["owners"]),
                "observed_index_count": len(indices),
                "index_lsb": indices[0] if indices else None,
                "index_msb": indices[-1] if indices else None,
                "primitive_classes": sorted(filter(None, item["refs"])),
            })
        for row in private_rows:
            row["shape"] = _anonymous_owner_shape(row["stem"])
            row["boundary_ref"] = shape_refs[row["shape"]]
        return public_rows, private_rows

    data_public, data_private = boundary_shapes(data_leaves)
    control_public, control_private = boundary_shapes(control_leaves)
    rectangular_scores = {
        row["boundary_ref"]: row["owner_instance_count"] * row["observed_index_count"]
        for row in data_public
        if row["owner_instance_count"] > 0 and row["observed_index_count"] > 0
    }
    best_rectangle = max(rectangular_scores.values(), default=0)
    candidate_storage_refs = sorted(
        ref for ref, score in rectangular_scores.items() if score == best_rectangle
    )
    candidate_storage_nets = {
        row["net_id"] for row in data_private
        if row["boundary_ref"] in candidate_storage_refs
    }

    def unresolved_touches_candidate(cell_key: str) -> bool:
        pending_nets = [
            identifier for identifier, direction in cells[cell_key]["decoded_pins"].values()
            if direction == "IN"
        ]
        seen_nets = set()
        while pending_nets:
            identifier = pending_nets.pop()
            if identifier in seen_nets:
                continue
            seen_nets.add(identifier)
            if identifier in candidate_storage_nets:
                return True
            producer = producers.get(identifier)
            if producer is None or producer[0] != "cell":
                continue
            upstream_row = producer[1]
            pending_nets.extend(
                net for net, direction in cells[upstream_row["id"]]["decoded_pins"].values()
                if direction == "IN"
            )
        return False

    unresolved_payload = {
        key: value for key, value in unresolved.items()
        if unresolved_touches_candidate(key)
    }
    preserved_opaque = {
        key: value for key, value in unresolved.items()
        if key not in unresolved_payload
    }

    candidate_rows_by_net = {
        row["net_id"]: row for row in data_private
        if row["boundary_ref"] in candidate_storage_refs
    }
    expected_candidate_owners = {
        row["stem"] for row in candidate_rows_by_net.values()
    }
    support_cache: dict[int, frozenset[int]] = {}

    def candidate_support(identifier: int) -> frozenset[int]:
        cached = support_cache.get(identifier)
        if cached is not None:
            return cached
        if identifier in candidate_storage_nets:
            value = frozenset({identifier})
            support_cache[identifier] = value
            return value
        producer = producers.get(identifier)
        if producer is None or producer[0] in {"boundary", "port"}:
            value = frozenset()
            support_cache[identifier] = value
            return value
        row, output_role = producer[1], producer[2]
        cell_key = row["id"]
        if cell_key in preserved_opaque:
            value = frozenset()
            support_cache[identifier] = value
            return value
        if cell_key in unresolved_payload:
            value = frozenset()
            support_cache[identifier] = value
            return value
        ref = row["ref"].upper()
        match = re.fullmatch(r"LUT([1-6])(?:_2)?", ref)
        if match is None:
            value = frozenset()
            support_cache[identifier] = value
            return value
        width = int(match.group(1))
        if ref.endswith("_2") and output_role == "O5":
            width = min(width, 5)
        table = _parse_init(row["init"])
        if ref.endswith("_2") and output_role == "O5":
            table &= (1 << 32) - 1
        pins = cells[cell_key]["decoded_pins"]

        def support_input(index: int) -> frozenset[int]:
            pin = pins.get(f"I{index}")
            if pin is None:
                raise LinearObservationError("compact GF2 support input is absent")
            return candidate_support(pin[0])

        try:
            _constant, coefficients = _local_affine_coefficients(table, width)
        except LinearObservationError:
            coefficients = []
        if coefficients:
            combined: set[int] = set()
            for index, coefficient in enumerate(coefficients):
                if coefficient:
                    combined.symmetric_difference_update(support_input(index))
            value = frozenset(combined)
        else:
            mux = _simple_mux_roles(table, width)
            if mux is None:
                value = frozenset()
            else:
                _select, data_inputs = mux
                combined = set()
                for index in data_inputs:
                    combined.update(support_input(index))
                value = frozenset(combined)
        support_cache[identifier] = value
        return value

    owner_rows: dict[str, list[set[int]]] = {}
    owner_refs: dict[str, str] = {}
    for target_index, identifier in targets:
        for net in candidate_support(identifier):
            boundary = candidate_rows_by_net[net]
            owner_rows.setdefault(
                boundary["stem"], [set() for _ in range(len(targets))]
            )[target_index].add(int(boundary["index"]))
            owner_refs[boundary["stem"]] = boundary["boundary_ref"]
    projection_signatures: dict[tuple[tuple[int, ...], ...], list[str]] = {}
    for owner, matrix in owner_rows.items():
        signature = tuple(tuple(sorted(row)) for row in matrix)
        projection_signatures.setdefault(signature, []).append(owner)
    matrix_complete = set(owner_rows) == expected_candidate_owners
    dominant_signature: tuple[tuple[int, ...], ...] = tuple()
    if projection_signatures:
        dominant_signature = max(
            projection_signatures,
            key=lambda signature: (len(projection_signatures[signature]), signature),
        )
    present_indices_by_owner: dict[str, set[int]] = {}
    candidate_shapes = {
        row["shape"] for row in data_private
        if row["boundary_ref"] in candidate_storage_refs
    }
    for row in boundaries.values():
        name = row.get("private_name", "")
        match = _INDEX.fullmatch(name)
        if match is None:
            continue
        stem = match.group("stem")
        if _anonymous_owner_shape(stem) in candidate_shapes:
            present_indices_by_owner.setdefault(stem, set()).add(int(match.group("index")))
    elision_proofs = {}
    variants_compatible = bool(dominant_signature)
    for owner, matrix in owner_rows.items():
        signature = tuple(tuple(sorted(row)) for row in matrix)
        missing = sorted({
            index for canonical, actual in zip(dominant_signature, signature)
            for index in set(canonical) - set(actual)
        })
        extra = sorted({
            index for canonical, actual in zip(dominant_signature, signature)
            for index in set(actual) - set(canonical)
        })
        missing_but_present = sorted(set(missing) & present_indices_by_owner.get(owner, set()))
        compatible = not extra and not missing_but_present
        variants_compatible &= compatible
        elision_proofs[owner] = {
            "missing_indices": missing,
            "extra_indices": extra,
            "missing_but_present_indices": missing_but_present,
            "compatible_constant_elision": compatible,
        }
    matrix_consistent = matrix_complete and variants_compatible
    projection_rank = None
    projection_supports: list[int] = []
    active_input_indices: list[int] = []
    if matrix_consistent:
        signature = dominant_signature
        projection_supports = [len(row) for row in signature]
        active_input_indices = sorted({index for row in signature for index in row})
        matrix_rows = [sum(1 << index for index in row) for row in signature]
        projection_rank = _gf2_rank(matrix_rows)

    private = {
        "data_boundaries": data_private,
        "control_boundaries": control_private,
        "overlap_net_ids": sorted(overlap),
        "candidate_storage_boundary_refs": candidate_storage_refs,
        "unresolved_payload_cells": unresolved_payload,
        "preserved_opaque_observable_roots": preserved_opaque,
        "owner_projection_matrices": {
            owner: [sorted(row) for row in matrix]
            for owner, matrix in owner_rows.items()
        },
        "constant_elision_proofs": elision_proofs,
    }
    public = {
        "kind": "compact_selected_storage_role_contract",
        "schema_version": 1,
        "status": "proved_roles" if not unresolved_payload and not overlap else "role_closure_incomplete",
        "authority": "projection_nomination_only",
        "mutation_eligible": False,
        "target_width": len(targets),
        "affine_data_cell_count": len(affine_cells),
        "exact_mux_or_gate_cell_count": len(mux_cells),
        "control_cell_count": len(control_cells),
        "unresolved_data_cell_count": len(unresolved_payload),
        "preserved_opaque_observable_root_count": len(preserved_opaque),
        "data_boundary_count": len(data_leaves),
        "control_boundary_count": len(control_leaves),
        "data_control_overlap_count": len(overlap),
        "data_boundary_shapes": data_public,
        "control_boundary_shapes": control_public,
        "candidate_storage_boundary_refs": candidate_storage_refs,
        "candidate_storage_owner_count": len(owner_rows),
        "candidate_storage_expected_owner_count": len(expected_candidate_owners),
        "projection_owner_coverage_complete": matrix_complete,
        "distinct_projection_matrix_count": len(projection_signatures),
        "projection_matrix_consistent": matrix_consistent,
        "constant_elided_variant_owner_count": sum(
            bool(proof["missing_indices"]) for proof in elision_proofs.values()
        ),
        "constant_elision_proved": variants_compatible,
        "projection_rank": projection_rank,
        "projection_row_support_min": min(projection_supports) if projection_supports else None,
        "projection_row_support_max": max(projection_supports) if projection_supports else None,
        "projection_active_input_count": len(active_input_indices),
        "projection_active_index_lsb": active_input_indices[0] if active_input_indices else None,
        "projection_active_index_msb": active_input_indices[-1] if active_input_indices else None,
        "required_before_mutation": [
            "zero unresolved candidate-storage data cells and zero data-control boundary overlap",
            "preserve opaque nonlinear observable cones proven disjoint from candidate storage",
            "transaction-word grouping and observable matrix rank",
            "all side consumers and dynamic-slice occupancy",
            "sequential CE reset valid-ready arbitration and latency replay",
            "invertible state-basis next-state proof",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


def write_candidate_storage_manifest(recovered: dict[str, Any], path: Path | str) -> None:
    private = recovered.get("private_manifest") or {}
    selected = set(private.get("candidate_storage_boundary_refs") or [])
    rows = [
        row for row in (private.get("data_boundaries") or [])
        if row.get("boundary_ref") in selected
    ]
    if not selected or not rows:
        raise LinearObservationError("candidate storage boundary inventory is empty")
    rows.sort(key=lambda row: (row["boundary_ref"], row["stem"], int(row["index"])))
    lines = ["boundary_ref\tindex\tcell_private\n"]
    for row in rows:
        name = str(row.get("name") or "")
        if not name or any(character in name for character in "\t\r\n"):
            raise LinearObservationError("unsafe candidate storage cell name")
        lines.append(f"{row['boundary_ref']}\t{int(row['index'])}\t{name}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def write_representative_storage_word_manifest(
    recovered: dict[str, Any], path: Path | str, *, boundary_ref: str | None = None
) -> None:
    private = recovered.get("private_manifest") or {}
    selected = set(private.get("candidate_storage_boundary_refs") or [])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in private.get("data_boundaries") or []:
        if row.get("boundary_ref") in selected and (
            boundary_ref is None or row.get("boundary_ref") == boundary_ref
        ):
            grouped.setdefault(row["stem"], []).append(row)
    if not grouped:
        raise LinearObservationError("candidate storage owner inventory is empty")
    complete = []
    for stem, rows in grouped.items():
        rows.sort(key=lambda row: int(row["index"]))
        indices = [int(row["index"]) for row in rows]
        if indices == list(range(indices[0], indices[-1] + 1)):
            complete.append((len(rows), stem, rows))
    if not complete:
        raise LinearObservationError("candidate storage has no complete owner word")
    _width, _stem, rows = sorted(complete, key=lambda item: (-item[0], item[1]))[0]
    lines = ["family_ref\ttarget_index\ttarget_cell\n"]
    for ordinal, row in enumerate(rows):
        name = str(row.get("name") or "")
        if not name or any(character in name for character in "\t\r\n"):
            raise LinearObservationError("unsafe representative storage cell name")
        lines.append(f"gf2_boundary_1\t{ordinal}\t{name}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def recover_registered_gf2_observation(path: Path | str) -> dict[str, Any]:
    csv.field_size_limit(8 * 1024 * 1024)
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    targets = sorted(
        (row for row in rows if row.get("kind") == "target"),
        key=lambda row: int(row["target_index"]),
    )
    if not targets or [int(row["target_index"]) for row in targets] != list(range(len(targets))):
        raise LinearObservationError("registered output target word is incomplete")

    cells: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") != "pin" or not row.get("cell"):
            continue
        cell = cells.setdefault(row["cell"], {
            "ref": row["ref"],
            "init": row.get("init", ""),
            "sequential": row.get("sequential") == "1",
            "pins": {},
        })
        cell["pins"][row["ref_pin"]] = {
            "direction": row["direction"], "net": row["net_key"],
        }

    producers: dict[str, tuple[str, str]] = {}
    boundary_names: dict[str, str] = {}
    for cell_name, cell in cells.items():
        for pin_name, pin in cell["pins"].items():
            net = pin["net"]
            if not net or pin["direction"] != "OUT":
                continue
            if net in producers and producers[net] != (cell_name, pin_name):
                raise LinearObservationError("observation net has multiple primitive drivers")
            producers[net] = (cell_name, pin_name)
            if cell["sequential"] or cell["ref"] in _INPUT_BOUNDARY_PRIMITIVES:
                boundary_names[net] = cell_name
    for row in rows:
        if row.get("kind") == "startpoint" and row.get("net_key"):
            boundary_names.setdefault(row["net_key"], row.get("cell") or "<port>")

    cache: dict[str, AffineExpression] = {}
    active: set[str] = set()

    def expression(net: str) -> AffineExpression:
        if net in cache:
            return cache[net]
        if net in boundary_names or net not in producers:
            value = AffineExpression(0, frozenset({net}))
            cache[net] = value
            return value
        if net in active:
            raise LinearObservationError("combinational cycle in observation cone")
        active.add(net)
        cell_name, output_pin = producers[net]
        cell = cells[cell_name]
        match = _LUT.fullmatch(cell["ref"])
        if match is None:
            raise LinearObservationError(
                f"unsupported observation primitive {cell['ref']}"
            )
        width = int(match.group(1))
        if cell["ref"].endswith("_2") and output_pin == "O5":
            width = min(width, 5)
        init = _parse_init(cell["init"])
        if cell["ref"].endswith("_2") and output_pin == "O5":
            init &= (1 << 32) - 1
        constant, coefficients = _local_affine_coefficients(init, width)
        value = AffineExpression(constant, frozenset())
        for index, coefficient in enumerate(coefficients):
            if not coefficient:
                continue
            pin = cell["pins"].get(f"I{index}")
            if pin is None or not pin["net"]:
                raise LinearObservationError("affine LUT input is unconnected")
            value = value.xor(expression(pin["net"]))
        active.remove(net)
        cache[net] = value
        return value

    expressions = [expression(row["net_key"]) for row in targets]
    variables = sorted(set().union(*(item.variables for item in expressions)))
    positions = {name: index for index, name in enumerate(variables)}
    matrix_rows = [
        sum(1 << positions[name] for name in item.variables)
        for item in expressions
    ]
    constants = sum(item.constant << index for index, item in enumerate(expressions))
    private_families = _private_indexed_groups(boundary_names, variables)
    private = {
        "target_count": len(targets),
        "boundary_variables": variables,
        "matrix_rows_hex": [hex(value) for value in matrix_rows],
        "constant_vector_hex": hex(constants),
        "boundary_cell_names": {
            name: boundary_names.get(name, "<port>") for name in variables
        },
        "registered_boundary_families": private_families,
    }
    public = {
        "kind": "registered_gf2_observation_contract",
        "schema_version": 1,
        "status": "proved_read_only",
        "authority": "classification_and_followup_probe_only",
        "mutation_eligible": False,
        "observable_width": len(targets),
        "boundary_dimension": len(variables),
        "matrix_rank": _gf2_rank(matrix_rows),
        "affine_constant_nonzero": bool(constants),
        "row_support_min": min(len(item.variables) for item in expressions),
        "row_support_max": max(len(item.variables) for item in expressions),
        "row_support_total": sum(len(item.variables) for item in expressions),
        "registered_boundary_word_shapes": [
            {
                "family_ref": row["family_ref"],
                "width": row["width"],
                "index_lsb": row["index_lsb"],
                "index_msb": row["index_msb"],
            }
            for row in private_families
        ],
        "required_before_mutation": [
            "payload word grouping and selected/storage owner closure",
            "proof that mux, CE/reset, valid/ready, arbitration, and side consumers are payload independent",
            "unchanged dynamic-slice occupancy and registered latency",
            "invertible basis and transformed sequential next-state proof",
            "emitted topology and clock-footprint evidence",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


def write_registered_boundary_probe_manifest(
    recovered: dict[str, Any], path: Path | str,
    *, family_refs: set[str] | None = None,
) -> None:
    private = recovered.get("private_manifest") or {}
    families = private.get("registered_boundary_families") or []
    if not families:
        raise LinearObservationError("registered boundary family inventory is empty")
    lines = ["family_ref\ttarget_index\ttarget_cell\n"]
    for family in families:
        if family_refs is not None and family.get("family_ref") not in family_refs:
            continue
        cells = family.get("cells") or []
        if not cells or len(cells) != int(family.get("width", -1)):
            raise LinearObservationError("registered boundary family is incomplete")
        for index, cell in enumerate(cells):
            if "\t" in cell or "\n" in cell or "\r" in cell:
                raise LinearObservationError("unsafe registered boundary cell name")
            lines.append(f"{family['family_ref']}\t{index}\t{cell}\n")
    if len(lines) == 1:
        raise LinearObservationError("selected registered boundary family inventory is empty")
    Path(path).write_text("".join(lines), encoding="utf-8")


def _conditional_affine_profile(
    table: int,
    width: int,
    data_positions: list[int],
    control_positions: list[int],
) -> tuple[int, dict[int, int]]:
    """Recover control-indexed affine coefficients for selected LUT inputs.

    Returned truth-table masks are indexed by ``control_positions`` order.
    This is exact exhaustive recovery (at most six LUT inputs), not a mux
    heuristic.  A coefficient mask bit says whether the corresponding data
    input participates under that control assignment.
    """

    if set(data_positions) & set(control_positions):
        raise LinearObservationError("conditional affine LUT roles overlap")
    if sorted(data_positions + control_positions) != list(range(width)):
        raise LinearObservationError("conditional affine LUT roles are incomplete")
    constant_mask = 0
    coefficient_masks = {position: 0 for position in data_positions}
    for control_value in range(1 << len(control_positions)):
        local_table = 0
        for data_value in range(1 << len(data_positions)):
            address = 0
            for ordinal, position in enumerate(control_positions):
                address |= ((control_value >> ordinal) & 1) << position
            for ordinal, position in enumerate(data_positions):
                address |= ((data_value >> ordinal) & 1) << position
            local_table |= ((table >> address) & 1) << data_value
        constant, coefficients = _local_affine_coefficients(
            local_table, len(data_positions)
        )
        constant_mask |= constant << control_value
        for ordinal, coefficient in enumerate(coefficients):
            coefficient_masks[data_positions[ordinal]] |= coefficient << control_value
    return constant_mask, coefficient_masks


def recover_conditional_affine_storage_write(path: Path | str) -> dict[str, Any]:
    """Recover a registered word write that is affine in repeated data words.

    Wide indexed boundary families are inferred from live connectivity.  LUT
    inputs from the widest repeated families are data; all other inputs are
    controls.  For every control assignment the local LUT must remain affine
    in the data inputs.  Exact hierarchy is kept only in the private manifest.
    """

    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    targets: list[tuple[int, int]] = []
    cells_by_output: dict[int, dict[str, Any]] = {}
    boundaries: dict[int, dict[str, Any]] = {}
    indexed: dict[str, dict[int, int]] = {}
    for row in rows:
        pins = _decode_compact_pins(row.get("pins", ""))
        if row.get("kind") == "target":
            targets.append((int(row["target_index"]), int(row["id"])))
        elif row.get("kind") == "cell":
            outputs = [net for net, direction in pins.values() if direction == "OUT"]
            if len(outputs) != 1:
                raise LinearObservationError("storage-write LUT must have one output")
            cells_by_output[outputs[0]] = {**row, "decoded_pins": pins}
        elif row.get("kind") == "boundary":
            outputs = [net for net, direction in pins.values() if direction == "OUT"]
            if len(outputs) != 1:
                raise LinearObservationError("storage-write boundary must have one output")
            net = outputs[0]
            boundaries[net] = {**row, "decoded_pins": pins}
            match = _INDEX.fullmatch(row.get("private_name", ""))
            if match is not None:
                indexed.setdefault(match.group("stem"), {})[
                    int(match.group("index"))
                ] = net
    targets.sort()
    if not targets or [index for index, _net in targets] != list(range(len(targets))):
        raise LinearObservationError("conditional storage-write targets are incomplete")
    if not indexed:
        raise LinearObservationError("conditional storage-write has no indexed boundary words")

    # Width, rather than a design-specific numeric cutoff, identifies the
    # transaction words.  Short indexed request/mask vectors remain controls.
    widest = max(len(members) for members in indexed.values())
    data_stems = sorted(stem for stem, members in indexed.items() if len(members) == widest)
    data_nets = {
        net: (stem, index)
        for stem in data_stems
        for index, net in indexed[stem].items()
    }
    stem_refs = {stem: f"source_word_{ordinal}" for ordinal, stem in enumerate(data_stems, 1)}

    profiles: dict[int, dict[str, Any]] = {}
    unsupported: list[int] = []
    for target_index, target_net in targets:
        cell = cells_by_output.get(target_net)
        if cell is None:
            # A direct register-to-register write is affine identity and is
            # recorded separately from the conditional-LUT family.
            boundary = boundaries.get(target_net)
            if boundary is not None and target_net in data_nets:
                stem, source_index = data_nets[target_net]
                profiles[target_index] = {
                    "kind": "direct",
                    "control_nets": [],
                    "constant_mask_hex": "0x0",
                    "coefficients": {stem_refs[stem]: "0x1"},
                    "source_indices": {stem_refs[stem]: source_index},
                }
                continue
            unsupported.append(target_index)
            continue
        match = _LUT.fullmatch(cell["ref"].upper())
        if match is None:
            unsupported.append(target_index)
            continue
        width = int(match.group(1))
        pins = cell["decoded_pins"]
        input_nets: dict[int, int] = {}
        for position in range(width):
            record = pins.get(f"I{position}")
            if record is None or record[1] != "IN":
                raise LinearObservationError("conditional storage-write LUT input is absent")
            input_nets[position] = record[0]
        data_positions = sorted(
            position for position, net in input_nets.items() if net in data_nets
        )
        control_positions = [position for position in range(width) if position not in data_positions]
        table = _parse_init(cell["init"])
        try:
            constant_mask, coefficient_masks = _conditional_affine_profile(
                table, width, data_positions, control_positions
            )
        except LinearObservationError:
            unsupported.append(target_index)
            continue
        control_nets = [input_nets[position] for position in control_positions]
        coefficients: dict[str, str] = {}
        source_indices: dict[str, int] = {}
        for position in data_positions:
            stem, source_index = data_nets[input_nets[position]]
            ref = stem_refs[stem]
            if ref in coefficients:
                unsupported.append(target_index)
                break
            coefficients[ref] = hex(coefficient_masks[position])
            source_indices[ref] = source_index
        else:
            profiles[target_index] = {
                "kind": "conditional_affine" if data_positions else "control_only",
                "control_nets": control_nets,
                "constant_mask_hex": hex(constant_mask),
                "coefficients": coefficients,
                "source_indices": source_indices,
                "private_cell_id": cell["id"],
            }

    conditional = [row for row in profiles.values() if row["kind"] == "conditional_affine"]
    control_only = [row for row in profiles.values() if row["kind"] == "control_only"]
    coefficient_function_signatures = {
        tuple(sorted(row["coefficients"].items())) for row in conditional
    }
    physical_control_skeletons = {
        tuple(row["control_nets"]) for row in conditional
    }
    # A synthesis replica is a logical predicate alias only when the source D
    # and every state-control pin are identical.  The compact exporter keeps D
    # specifically for this certificate; old caches without D stay unproved.
    control_signatures: dict[int, tuple[Any, ...]] = {}
    all_control_nets = sorted({
        net for row in profiles.values() for net in row.get("control_nets", [])
    })
    for net in all_control_nets:
        boundary = boundaries.get(net)
        if boundary is None:
            continue
        pins = boundary["decoded_pins"]
        d_pin = pins.get("D")
        if d_pin is None or d_pin[1] != "IN":
            continue
        control_signatures[net] = (
            boundary.get("ref", ""), boundary.get("init", ""), d_pin[0],
            *(
                pins.get(role, (None, ""))[0]
                for role in ("C", "CE", "R", "S", "CLR", "PRE")
            ),
        )
    distinct_state_signatures = sorted(set(control_signatures.values()), key=repr)
    signature_refs = {
        signature: f"control_predicate_{ordinal}"
        for ordinal, signature in enumerate(distinct_state_signatures, 1)
    }
    control_classes = {
        str(net): signature_refs[signature]
        for net, signature in control_signatures.items()
    }
    logical_control_skeletons = {
        tuple(control_classes.get(str(net), f"unproved_net_{net}") for net in row["control_nets"])
        for row in conditional
    }
    source_index_aligned = all(
        len(set(row["source_indices"].values())) <= 1 for row in conditional
    )
    covered_source_indices = sorted({
        index for row in conditional for index in row["source_indices"].values()
    })
    private = {
        "data_stems": data_stems,
        "source_word_members": {
            stem_refs[stem]: {str(index): net for index, net in sorted(indexed[stem].items())}
            for stem in data_stems
        },
        "target_profiles": {str(index): row for index, row in sorted(profiles.items())},
        "unsupported_target_indices": unsupported,
        "control_classes": control_classes,
        "control_probe_targets": [
            {
                "physical_net_id": net,
                "cell_private": boundaries[net].get("private_name", ""),
                "state_signature": {
                    "ref": boundaries[net].get("ref", ""),
                    "init": boundaries[net].get("init", ""),
                    "pins": {
                        role: boundaries[net]["decoded_pins"][role][0]
                        for role in ("C", "CE", "R", "S", "CLR", "PRE")
                        if role in boundaries[net]["decoded_pins"]
                    },
                },
            }
            for net in all_control_nets if net in boundaries
        ],
    }
    proved = (
        not unsupported
        and bool(conditional)
        and len(coefficient_function_signatures) == 1
        and source_index_aligned
    )
    public = {
        "kind": "conditional_affine_storage_write_contract",
        "schema_version": 1,
        "status": "proved_conditional_affine" if proved else "write_factoring_incomplete",
        "authority": "projection_closure_proof_only",
        "mutation_eligible": False,
        "target_width": len(targets),
        "source_word_count": len(data_stems),
        "source_word_width": widest,
        "conditional_affine_target_count": len(conditional),
        "control_only_target_count": len(control_only),
        "direct_target_count": sum(row["kind"] == "direct" for row in profiles.values()),
        "unsupported_target_count": len(unsupported),
        "common_coefficient_function": len(coefficient_function_signatures) == 1,
        "physical_control_skeleton_count": len(physical_control_skeletons),
        "logical_control_skeleton_count": len(logical_control_skeletons),
        "control_state_identity_available": len(control_signatures) == len(all_control_nets),
        "control_replica_equivalence_proved": len(logical_control_skeletons) == 1,
        "source_word_indices_aligned": source_index_aligned,
        "covered_source_index_count": len(covered_source_indices),
        "control_input_count": len(conditional[0]["control_nets"]) if conditional else None,
        "required_before_mutation": [
            "replicated control-predicate equivalence and projection row-space closure",
            "complete source and destination CE reset init schedule replay",
            "all side consumers remain inside transformed data closure",
            "in-place emitted topology and immutable clock-XDC measurement",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


def write_conditional_control_probe_manifest(
    recovered: dict[str, Any], path: Path | str
) -> None:
    targets = (recovered.get("private_manifest") or {}).get("control_probe_targets") or []
    if not targets:
        raise LinearObservationError("conditional control probe inventory is empty")
    lines = ["family_ref\ttarget_index\ttarget_cell\n"]
    for ordinal, row in enumerate(sorted(targets, key=lambda item: int(item["physical_net_id"]))):
        cell = str(row.get("cell_private") or "")
        if not cell or any(character in cell for character in "\t\r\n"):
            raise LinearObservationError("unsafe conditional control target cell")
        lines.append(f"conditional_control\t{ordinal}\t{cell}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def recover_conditional_control_equivalence(
    path: Path | str,
    storage_write: dict[str, Any],
) -> dict[str, Any]:
    """Prove logical predicate aliases from exact D cones and FF schedules."""

    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    producers: dict[int, tuple[str, dict[str, Any], str]] = {}
    cells: dict[str, dict[str, Any]] = {}
    targets: dict[str, int] = {}
    for row in rows:
        pins = _decode_compact_pins(row.get("pins", ""))
        kind = row.get("kind")
        if kind == "cell":
            cells[row["id"]] = {**row, "decoded_pins": pins}
            for role, (net, direction) in pins.items():
                if direction == "OUT":
                    producers[net] = ("cell", row, role)
        elif kind in {"boundary", "port"}:
            if kind == "port":
                producers[int(row["id"])] = ("port", row, "PORT")
            else:
                for role, (net, direction) in pins.items():
                    if direction == "OUT":
                        producers[net] = ("boundary", row, role)
        elif kind == "target":
            targets[row.get("private_name", "")] = int(row["id"])
    if not targets:
        raise LinearObservationError("conditional control target inventory is empty")

    cache: dict[int, Any] = {}
    active: set[int] = set()

    def canonical(net: int) -> Any:
        if net in cache:
            return cache[net]
        producer = producers.get(net)
        if producer is None:
            value = ("undriven", net)
            cache[net] = value
            return value
        kind, row, output_role = producer
        if kind in {"boundary", "port"}:
            # Net IDs are driver-keyed by the compact extractor, so one ID is
            # one exact sequential/top-port predicate across all target cones.
            value = (kind, net)
            cache[net] = value
            return value
        if net in active:
            raise LinearObservationError("cycle in conditional control D cone")
        active.add(net)
        cell = cells[row["id"]]
        match = _LUT.fullmatch(cell["ref"].upper())
        if match is None:
            raise LinearObservationError(
                f"unsupported conditional control primitive {cell['ref']}"
            )
        width = int(match.group(1))
        if cell["ref"].upper().endswith("_2") and output_role == "O5":
            width = min(width, 5)
        table = _parse_init(cell["init"])
        if cell["ref"].upper().endswith("_2") and output_role == "O5":
            table &= (1 << 32) - 1
        inputs = []
        for position in range(width):
            pin = cell["decoded_pins"].get(f"I{position}")
            if pin is None or pin[1] != "IN":
                raise LinearObservationError("conditional control LUT input is absent")
            inputs.append(canonical(pin[0]))
        active.remove(net)
        value = ("lut", width, table, tuple(inputs))
        cache[net] = value
        return value

    probe_targets = (storage_write.get("private_manifest") or {}).get("control_probe_targets") or []
    recovered: list[tuple[int, Any, dict[str, Any], str]] = []
    missing = []
    for row in probe_targets:
        cell = str(row.get("cell_private") or "")
        if cell not in targets:
            missing.append(cell)
            continue
        recovered.append((
            int(row["physical_net_id"]),
            canonical(targets[cell]),
            row.get("state_signature") or {},
            cell,
        ))
    if missing or len(recovered) != len(probe_targets):
        raise LinearObservationError("conditional control probe target coverage is incomplete")
    groups: dict[str, list[tuple[int, str]]] = {}
    signatures: dict[int, str] = {}
    raw_signatures: dict[str, Any] = {}
    for physical_net, d_function, state_signature, cell in recovered:
        raw = {"d_function": d_function, "state_signature": state_signature}
        digest = _digest(raw)
        raw_signatures.setdefault(digest, raw)
        groups.setdefault(digest, []).append((physical_net, cell))
    ranked = sorted(groups.items(), key=lambda item: (repr(raw_signatures[item[0]]), item[0]))
    class_members = {}
    for ordinal, (digest, members) in enumerate(ranked, 1):
        ref = f"logical_control_predicate_{ordinal}"
        class_members[ref] = [physical_net for physical_net, _cell in members]
        for physical_net, _cell in members:
            signatures[physical_net] = ref
    private = {
        "physical_net_classes": {str(net): ref for net, ref in sorted(signatures.items())},
        "class_member_cells": {
            ref: [cell for net, cell in members]
            for ref, (_digest_value, members) in zip(class_members, ranked)
        },
        "class_signatures": raw_signatures,
    }
    public = {
        "kind": "conditional_control_predicate_equivalence_contract",
        "schema_version": 1,
        "status": "proved_exact_d_and_schedule_equivalence",
        "authority": "projection_closure_proof_only",
        "mutation_eligible": False,
        "physical_predicate_count": len(recovered),
        "logical_predicate_count": len(groups),
        "replicated_predicate_count": len(recovered) - len(groups),
        "target_coverage_complete": True,
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


def _reduce_gf2_with_basis(value: int, basis: dict[int, int]) -> int:
    reduced = int(value)
    while reduced:
        pivot = reduced.bit_length() - 1
        if pivot not in basis:
            break
        reduced ^= basis[pivot]
    return reduced


def prove_conditional_affine_projection_closure(
    projection: dict[str, Any],
    storage_write: dict[str, Any],
    control_equivalence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove the minimal extra GF(2) coordinates needed across a live write."""

    matrices = (projection.get("private_manifest") or {}).get("owner_projection_matrices") or {}
    if not matrices:
        raise LinearObservationError("projection owner matrices are absent")
    counts: dict[tuple[tuple[int, ...], ...], int] = {}
    for matrix in matrices.values():
        signature = tuple(tuple(int(index) for index in row) for row in matrix)
        counts[signature] = counts.get(signature, 0) + 1
    canonical = max(counts, key=lambda item: (counts[item], item))
    matrix_rows = [sum(1 << index for index in row) for row in canonical]
    basis: dict[int, int] = {}
    for row in matrix_rows:
        reduced = _reduce_gf2_with_basis(row, basis)
        if reduced:
            basis[reduced.bit_length() - 1] = reduced

    write_private = storage_write.get("private_manifest") or {}
    equivalence_classes = (
        (control_equivalence or {}).get("private_manifest") or {}
    ).get("physical_net_classes") or write_private.get("control_classes") or {}
    profiles = {
        int(index): row for index, row in (write_private.get("target_profiles") or {}).items()
    }
    if not profiles:
        raise LinearObservationError("conditional storage-write profiles are absent")
    source_refs = sorted({
        ref for row in profiles.values() for ref in row.get("source_indices", {})
    })
    residual_rows: list[int] = []
    closed_rows = 0
    row_records = []
    for output_index, support in enumerate(canonical):
        mapped_by_source: dict[str, int] = {}
        for source_ref in source_refs:
            mapped = 0
            skeletons = set()
            for target_index in support:
                profile = profiles.get(target_index)
                if profile is None:
                    raise LinearObservationError("projection references an absent write target")
                if source_ref not in profile.get("source_indices", {}):
                    continue
                mapped ^= 1 << int(profile["source_indices"][source_ref])
                skeletons.add((
                    tuple(
                        equivalence_classes.get(str(net), f"unproved_net_{net}")
                        for net in (profile.get("control_nets") or [])
                    ),
                    str((profile.get("coefficients") or {}).get(source_ref, "0x0")),
                ))
            if len(skeletons) > 1:
                raise LinearObservationError("projection row has inconsistent conditional coefficients")
            if any(
                ref.startswith("unproved_net_")
                for controls, _coefficient in skeletons for ref in controls
            ):
                raise LinearObservationError("replicated storage-write controls are not proved equivalent")
            mapped_by_source[source_ref] = mapped
        distinct_mapped = set(mapped_by_source.values())
        if len(distinct_mapped) > 1:
            raise LinearObservationError("source words require different projected coordinates")
        mapped = next(iter(distinct_mapped), 0)
        residual = _reduce_gf2_with_basis(mapped, basis)
        if residual:
            residual_rows.append(residual)
        else:
            closed_rows += 1
        row_records.append({
            "output_index": output_index,
            "mapped_support_hex": hex(mapped),
            "residual_hex": hex(residual),
        })
    residual_basis: dict[int, int] = {}
    for row in residual_rows:
        reduced = _reduce_gf2_with_basis(row, residual_basis)
        if reduced:
            residual_basis[reduced.bit_length() - 1] = reduced
    extra_rows = sorted(residual_basis.values(), key=lambda row: row.bit_length())
    # One-step closure is not sufficient for a sequential rewrite.  Iterate
    # the recovered data-coordinate transition until the invariant subspace
    # stabilizes.  If it expands to the full word, compression requires a
    # separate reachable-state/source-generator proof and must not mutate.
    def transition_image(value: int) -> int:
        mapped = 0
        for target_index in range(max(profiles) + 1):
            if not ((value >> target_index) & 1):
                continue
            profile = profiles[target_index]
            indices = set(profile.get("source_indices", {}).values())
            if len(indices) > 1:
                raise LinearObservationError("sequential write maps source words differently")
            if indices:
                mapped ^= 1 << int(next(iter(indices)))
        return mapped

    invariant_basis: dict[int, int] = {}
    generators: list[int] = []
    for row in matrix_rows:
        reduced = _reduce_gf2_with_basis(row, invariant_basis)
        if reduced:
            invariant_basis[reduced.bit_length() - 1] = reduced
            generators.append(row)
    cursor = 0
    closure_rounds = 0
    frontier_end = len(generators)
    while cursor < len(generators):
        while cursor < frontier_end:
            image = transition_image(generators[cursor])
            cursor += 1
            reduced = _reduce_gf2_with_basis(image, invariant_basis)
            if reduced:
                invariant_basis[reduced.bit_length() - 1] = reduced
                generators.append(image)
        closure_rounds += 1
        frontier_end = len(generators)
    transitive_rank = len(invariant_basis)
    target_width = int((storage_write.get("public_contract") or {}).get("target_width") or 0)
    compressive = bool(target_width) and transitive_rank < target_width
    private = {
        "canonical_projection_rows": [list(row) for row in canonical],
        "write_row_records": row_records,
        "supplemental_coordinate_rows_hex": [hex(row) for row in extra_rows],
        "invariant_basis_rows_hex": [hex(row) for row in generators],
    }
    public = {
        "kind": "conditional_affine_projection_closure_contract",
        "schema_version": 1,
        "status": (
            "proved_invariant_compressive_basis"
            if compressive else "sequential_basis_not_compressive"
        ),
        "authority": "emitter_candidate_only" if compressive else "read_only_counterevidence",
        "mutation_eligible": False,
        "projection_width": len(canonical),
        "projection_rank": _gf2_rank(matrix_rows),
        "directly_closed_projection_rows": closed_rows,
        "boundary_projection_rows": len(canonical) - closed_rows,
        "supplemental_coordinate_rank": len(extra_rows),
        "one_step_candidate_width": len(canonical) + len(extra_rows),
        "transitive_closure_rank": transitive_rank,
        "closed_state_width": transitive_rank,
        "sequential_closure_rounds": closure_rounds,
        "state_compression_dimension": max(0, target_width - transitive_rank),
        "source_word_count": len(source_refs),
        "required_before_mutation": [
            (
                "derive supplemental-coordinate next-state equations"
                if compressive else
                "prove reset-reachable source-generator subspace before any state compression"
            ),
            "replay destination CE reset init and control-only write terms",
            "emit all transformed owners and preserve control-visible state",
            "route reopen and measure immutable target clock",
        ],
        "exact_names_public": False,
    }
    public["private_manifest_sha256"] = _digest(private)
    return {"public_contract": public, "private_manifest": private}


__all__ = [
    "AffineExpression", "LinearObservationError",
    "recover_registered_gf2_observation", "write_registered_boundary_probe_manifest",
    "summarize_live_registered_owners", "write_live_owner_probe_manifest",
    "recover_live_registered_owner_contract",
    "recover_compact_selected_storage_roles",
    "write_candidate_storage_manifest",
    "write_representative_storage_word_manifest",
    "recover_conditional_affine_storage_write",
    "write_conditional_control_probe_manifest",
    "recover_conditional_control_equivalence",
    "prove_conditional_affine_projection_closure",
]
