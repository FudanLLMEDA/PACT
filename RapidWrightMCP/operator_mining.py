"""Bounded, read-only structural hypothesis mining for loaded DCP designs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional


MAX_PATHS = 16
# Real failing paths reach ~70 cells when the clock is far from closure. A
# 64-cell ceiling rejected the whole payload for exactly those designs, which
# silently disabled all operator evidence for them.
MAX_PATH_CELLS = 256
MAX_FAMILIES = 32
MAX_MOTIF_CELLS = 12
_CLOCK_INFRA_TYPES = {"INBUF", "IBUF", "IBUFCTRL", "BUFG", "BUFGCE"}
_CLOCK_AS_DATA_TYPE_PREFIXES = (
    "INBUF", "IBUFCTRL", "BUFG", "BUFR", "BUFH", "MMCM", "PLL",
)
_DISPLAY_NAME_LIMIT = 192
_HYPOTHESIS_AUTHORITY = "exact_dcp_structural_hypothesis_only"


def _as_list(value) -> list:
    return [] if value is None else list(value)


def _safe_call(obj, method: str, default=None):
    try:
        value = getattr(obj, method)()
        return default if value is None else value
    except Exception:
        return default


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _location(cell) -> tuple[Optional[int], Optional[int], Optional[str]]:
    try:
        if not bool(cell.isPlaced()):
            return None, None, None
        site = cell.getSite()
        if site is None:
            return None, None, None
        site_name = str(site.getName())
        tile = site.getTile()
        x = _safe_call(tile, "getTileXCoordinate")
        y = _safe_call(tile, "getTileYCoordinate")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            match = re.search(r"_X(-?\d+)Y(-?\d+)", str(tile.getName()))
            if match is None:
                match = re.search(r"_X(-?\d+)Y(-?\d+)", site_name)
            if match is not None:
                x, y = int(match.group(1)), int(match.group(2))
        return (
            int(x) if isinstance(x, (int, float)) else None,
            int(y) if isinstance(y, (int, float)) else None,
            site_name,
        )
    except Exception:
        return None, None, None


def _normalized_stem(name: str) -> str:
    value = re.sub(r"\[\s*\d+\s*\]", "[]", str(name))
    value = re.sub(r"(?<![A-Za-z])\d+(?![A-Za-z])", "#", value)
    value = re.sub(r"(?<=[_.$/])\d+(?=[_.$/]|$)", "#", value)
    return value


def _hard_macro_class(cell_type: str) -> Optional[str]:
    value = str(cell_type).upper()
    if value.startswith("DSP"):
        return "dsp"
    if value.startswith("RAMB"):
        return "bram"
    if value.startswith("URAM"):
        return "uram"
    return None


def _resolve_path_cell(design, token: str):
    candidate = str(token)
    while candidate:
        try:
            cell = design.getCell(candidate)
        except Exception:
            cell = None
        if cell is not None:
            return cell
        if "/" not in candidate:
            break
        candidate = candidate.rsplit("/", 1)[0]
    return None


def _normalized_sequence_edit_distance(
    left: tuple[str, ...], right: tuple[str, ...]
) -> float:
    """Return Levenshtein distance normalized by the longer sequence."""
    if left == right:
        return 0.0
    scale = max(len(left), len(right))
    if scale == 0:
        return 0.0
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, 1):
        current = [left_index]
        for right_index, right_item in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1]
                + (0 if left_item == right_item else 1),
            ))
        previous = current
    return previous[-1] / scale


def _ordered_path_motif_evidence(resolved_paths: list) -> dict[str, Any]:
    """Summarize resolved type sequences without exposing design identifiers."""
    sequences = [
        tuple(item["type"] for item in path) for _path_index, path in resolved_paths
    ]
    sequence_counts: dict[tuple[str, ...], int] = {}
    for sequence in sequences:
        sequence_counts[sequence] = sequence_counts.get(sequence, 0) + 1

    families = []
    for sequence, sequence_count in sequence_counts.items():
        skipped_reference = False
        distances = []
        for peer in sequences:
            if not skipped_reference and peer == sequence:
                skipped_reference = True
                continue
            distances.append(_normalized_sequence_edit_distance(sequence, peer))

        nearest_distance = min(distances) if distances else None
        population_distance = (
            sum(distances) / len(distances) if distances else None
        )
        carry_count = sum(item.startswith("CARRY") for item in sequence)
        dsp_count = sum(item.startswith("DSP") for item in sequence)
        clock_as_data = any(
            item.startswith(prefix)
            for item in sequence
            for prefix in _CLOCK_AS_DATA_TYPE_PREFIXES
        )
        families.append({
            "primitive_sequence": list(sequence),
            "sequence_length": len(sequence),
            "sequence_count": sequence_count,
            "peer_sequence_count": len(distances),
            "nearest_normalized_edit_distance": (
                round(nearest_distance, 6) if nearest_distance is not None else None
            ),
            "population_normalized_edit_distance": (
                round(population_distance, 6)
                if population_distance is not None else None
            ),
            "nearest_continuous_similarity": (
                round(1.0 - nearest_distance, 6)
                if nearest_distance is not None else None
            ),
            "population_continuous_similarity": (
                round(1.0 - population_distance, 6)
                if population_distance is not None else None
            ),
            "dedicated_arithmetic_primitive_count": carry_count + dsp_count,
            "carry_primitive_count": carry_count,
            "dsp_primitive_count": dsp_count,
            "lut_primitive_count": sum(
                item.startswith("LUT") for item in sequence
            ),
            "risk_flags": ["clock_as_data_path"] if clock_as_data else [],
            "authority": _HYPOTHESIS_AUTHORITY,
            "read_only": True,
            "mutation_allowed": False,
            "proof_status": "hypothesis_only",
        })
    families.sort(key=lambda item: (
        -item["sequence_count"], -item["sequence_length"],
        item["primitive_sequence"],
    ))
    return {
        "version": "operator-mining-motif-v2",
        "authority": _HYPOTHESIS_AUTHORITY,
        "resolved_sequence_count": len(sequences),
        "unique_sequence_count": len(families),
        "similarity_metric": (
            "one_minus_levenshtein_distance_normalized_by_max_sequence_length"
        ),
        "sequence_families": families,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "hypothesis_only",
    }


def _candidate_id(kind: str, payload: Any) -> str:
    return f"{kind}:{_digest(payload)[:20]}"


def _judgment_step(
    condition: str,
    *,
    state: str,
    scope: str,
    evidence: dict[str, Any],
    missing_evidence: list[str],
    next_discriminator: dict[str, Any],
) -> dict[str, Any]:
    """Build one Knowledge-style, non-dispatching judgment step.

    The tri-state result describes what the current probe established.  It is
    deliberately not a trigger: continuous coverage/resource facts remain
    visible so Luna can compare semantic and physical alternatives.
    """
    if state not in {"supported", "contradicted", "unresolved"}:
        raise ValueError(f"invalid operator judgment state {state!r}")
    return {
        "condition": condition,
        "state": state,
        "scope": scope,
        "evidence": evidence,
        "missing_evidence": missing_evidence,
        "next_discriminator": next_discriminator,
    }


def _display_name(value: str) -> str:
    text = str(value)
    if len(text) <= _DISPLAY_NAME_LIMIT:
        return text
    return text[:160] + "...#" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def _recurrence_source_stem(name: str) -> str:
    """Preserve the stage index while normalizing one relay bank's bit/suffix."""
    value = re.sub(r"\[(\d+)\](?=_psdsp(?:_\d+)?$)", "[]", str(name))
    return re.sub(r"_psdsp(?:_\d+)?$", "_psdsp*", value)


def _pipeline_rebalance_operand_token(token: str) -> Optional[tuple[str, int]]:
    """Normalize exact DSP A/B boundary and Vivado leaf operand tokens."""
    token_upper = str(token).upper()
    match = re.search(r"/(A|B)\[(\d+)\]$", token_upper)
    if match is None:
        match = re.search(
            r"/DSP_A_B_DATA_INST/([AB])2_DATA\[(\d+)\]$", token_upper
        )
    if match is None:
        return None
    return match.group(1).lower(), int(match.group(2))


def _validate_inputs(
    critical_paths_data,
    min_family_size: int,
    max_families: int,
    max_motif_cells: int,
) -> None:
    if critical_paths_data is not None:
        if not isinstance(critical_paths_data, list) or len(critical_paths_data) > MAX_PATHS:
            raise ValueError(f"critical_paths_data must contain at most {MAX_PATHS} paths")
        for index, path in enumerate(critical_paths_data):
            if (
                not isinstance(path, list)
                or len(path) > MAX_PATH_CELLS
                or any(
                    not isinstance(item, str) or not item or len(item) > 1024
                    for item in path
                )
            ):
                raise ValueError(
                    f"critical_paths_data[{index}] must contain at most "
                    f"{MAX_PATH_CELLS} nonempty cell or pin names"
                )
    for name, value, low, high in (
        ("min_family_size", min_family_size, 2, 64),
        ("max_families", max_families, 1, MAX_FAMILIES),
        ("max_motif_cells", max_motif_cells, 3, MAX_MOTIF_CELLS),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in {low}..{high}")


def mine_operator_structures(
    design,
    *,
    critical_paths_data=None,
    min_family_size: int = 3,
    max_families: int = 16,
    max_motif_cells: int = 8,
    design_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Mine structural candidates without treating similarity as a proof."""
    _validate_inputs(
        critical_paths_data, min_family_size, max_families, max_motif_cells
    )
    cells = sorted(_as_list(design.getCells()), key=lambda cell: str(cell.getName()))
    records = []
    used_types: dict[str, int] = {}
    macro_columns: dict[str, set[int]] = {"dsp": set(), "bram": set(), "uram": set()}
    macro_sites: dict[str, set[str]] = {"dsp": set(), "bram": set(), "uram": set()}
    for cell in cells:
        name = str(cell.getName())
        cell_type = str(cell.getType()).upper()
        x, y, site = _location(cell)
        macro_class = _hard_macro_class(cell_type)
        if macro_class is not None and x is not None:
            macro_columns[macro_class].add(x)
        if macro_class is not None and site is not None:
            macro_sites[macro_class].add(site)
        used_types[cell_type] = used_types.get(cell_type, 0) + 1
        records.append({
            "name": name,
            "type": cell_type,
            "stem": _normalized_stem(name),
            "x": x,
            "y": y,
            "site": site,
        })

    dsp_site_count = 0
    try:
        for site in _as_list(design.getDevice().getAllSites()):
            if str(site.getSiteTypeEnum()).upper().startswith("DSP"):
                dsp_site_count += 1
    except Exception:
        pass
    logical_dsp_primitive_count = sum(
        count for cell_type, count in used_types.items()
        if _hard_macro_class(cell_type) == "dsp"
    )
    used_dsp_sites = len(macro_sites["dsp"])
    idle_dsp_estimate = max(0, dsp_site_count - used_dsp_sites) if dsp_site_count else None

    resolved_paths = []
    unresolved_tokens = 0
    critical_names = set()
    for path_index, path in enumerate(critical_paths_data or []):
        resolved = []
        for token in path:
            cell = _resolve_path_cell(design, token)
            if cell is None:
                unresolved_tokens += 1
                continue
            name = str(cell.getName())
            if resolved and resolved[-1]["name"] == name:
                continue
            item = {"name": name, "type": str(cell.getType()).upper()}
            resolved.append(item)
            critical_names.add(name)
        if resolved:
            resolved_paths.append((path_index, resolved))

    # This is deliberately only a cheap scheduling hint.  An FDSE occurring on
    # the exact current timing sample can justify the standalone Vivado
    # state-equation recovery, but it does not prove that S implies CE or grant
    # mutation authority.  The recovery tool re-derives the complete truth
    # table and all exact selectors from the same SHA-bound checkpoint.
    control_state_live_cells = {
        item["name"]
        for _path_index, path in resolved_paths
        for item in path
        if item["type"] == "FDSE"
    }

    occurrences: dict[tuple[str, ...], list[dict]] = {}
    for path_index, path in resolved_paths:
        for width in range(3, min(max_motif_cells, len(path)) + 1):
            for start in range(len(path) - width + 1):
                window = path[start:start + width]
                signature = tuple(item["type"] for item in window)
                if any(item in _CLOCK_INFRA_TYPES for item in signature):
                    continue
                occurrences.setdefault(signature, []).append({
                    "path": path_index,
                    "start": start,
                    "members": [item["name"] for item in window],
                })

    motif_families = []
    # Missing device-capacity data is unknown, not positive headroom evidence.
    dsp_headroom = idle_dsp_estimate is not None and idle_dsp_estimate > 0
    for signature, members in occurrences.items():
        if len(members) < min_family_size:
            continue
        types = set(signature)
        has_dsp = any(_hard_macro_class(item) == "dsp" for item in types)
        has_ff = any(re.fullmatch(r"FD[A-Z0-9_]+", item) for item in types)
        has_lut = any(item.startswith("LUT") for item in types)
        has_carry = any(item.startswith("CARRY") for item in types)
        if has_dsp and has_ff:
            kind = "hard_macro_recurrence_motif"
        elif has_carry and dsp_headroom:
            kind = "carry_arithmetic_motif"
        elif has_carry:
            kind = "fabric_arithmetic_motif"
        elif has_lut and has_ff:
            kind = "repeated_lut_register_motif"
        else:
            kind = "repeated_path_motif"
        payload = {"signature": list(signature), "occurrences": members}
        covered_path_ranks = sorted({int(item["path"]) + 1 for item in members})
        motif_families.append({
            "candidate_id": _candidate_id("motif", payload),
            "kind": kind,
            "evidence_scope": "wall_motif_family",
            "type_signature": list(signature),
            "occurrence_count": len(members),
            "critical_path_count": len({item["path"] for item in members}),
            "critical_path_coverage": {
                "ordered_path_count": len(resolved_paths),
                "covered_path_count": len(covered_path_ranks),
                "covered_path_ranks": covered_path_ranks,
                "covers_worst_path": 1 in covered_path_ranks,
                "all_path_coverage_ratio": (
                    round(len(covered_path_ranks) / len(resolved_paths), 4)
                    if resolved_paths else 0.0
                ),
                "rank_semantics": "rank_1_is_worst_in_current_live_path_sample",
            },
            "member_examples": [
                [_display_name(name) for name in item["members"]]
                for item in members[:2]
            ],
            "proof_status": "hypothesis_only",
        })
    motif_families.sort(key=lambda item: (
        -item["critical_path_count"], -item["occurrence_count"],
        -len(item["type_signature"]), item["candidate_id"],
    ))

    recurrence_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for path_index, path in resolved_paths:
        for index in range(len(path) - 1):
            source, sink = path[index], path[index + 1]
            if not re.fullmatch(r"FD[A-Z0-9_]+", source["type"]):
                continue
            if sink["type"] != "DSP_C_DATA":
                continue
            source_stem = _recurrence_source_stem(source["name"])
            sink_dsp = sink["name"].rsplit("/", 1)[0]
            key = (source_stem, sink_dsp)
            group = recurrence_groups.setdefault(key, {
                "paths": set(), "examples": [], "signature": [],
            })
            group["paths"].add(path_index)
            if len(group["examples"]) < 2:
                group["examples"].append([source["name"], sink["name"]])
            group["signature"] = [item["type"] for item in path[index:index + 4]]

    recurrence_families = []
    for (source_stem, sink_dsp), group in recurrence_groups.items():
        source_members = [
            item for item in records
            if item["type"].startswith("FD")
            and _recurrence_source_stem(item["name"]) == source_stem
        ]
        if len(source_members) < min_family_size:
            continue
        columns = sorted({
            item["x"] for item in source_members if item["x"] is not None
        })
        payload = {
            "source_stem": source_stem,
            "sink_dsp": sink_dsp,
            "source_members": [item["name"] for item in source_members],
        }
        recurrence_families.append({
            "candidate_id": _candidate_id("recurrence", payload),
            "kind": "recurrence_boundary_hypothesis",
            "source_register_stem": _display_name(source_stem),
            "source_register_count": len(source_members),
            "source_columns": columns[:32],
            "sink_dsp": _display_name(sink_dsp),
            "critical_path_count": len(group["paths"]),
            "transition_signature": group["signature"],
            "member_examples": [
                [_display_name(name) for name in pair]
                for pair in group["examples"]
            ],
            "authority": _HYPOTHESIS_AUTHORITY,
            "read_only": True,
            "mutation_allowed": False,
            "proof_status": "hypothesis_only",
        })
    recurrence_families.sort(key=lambda item: (
        -item["critical_path_count"], -item["source_register_count"],
        item["candidate_id"],
    ))

    register_groups: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        if re.fullmatch(r"FD[A-Z0-9_]+", record["type"]):
            register_groups.setdefault((record["stem"], record["type"]), []).append(record)
    transport_families = []
    all_macro_columns = sorted(set().union(*macro_columns.values()))
    for (stem, cell_type), members in register_groups.items():
        columns = sorted({item["x"] for item in members if item["x"] is not None})
        if len(members) < min_family_size or len(columns) < 2:
            continue
        span = columns[-1] - columns[0]
        if span < 2:
            continue
        payload = {
            "stem": stem, "type": cell_type,
            "members": [item["name"] for item in members],
        }
        transport_families.append({
            "candidate_id": _candidate_id("transport", payload),
            "kind": "register_transport_hypothesis",
            "normalized_stem": _display_name(stem),
            "cell_type": cell_type,
            "member_count": len(members),
            "critical_member_count": sum(
                item["name"] in critical_names for item in members
            ),
            "column_span": span,
            "occupied_columns": columns[:32],
            "near_hard_macro_columns": [
                column for column in all_macro_columns
                if any(abs(column - member_column) <= 2 for member_column in columns)
            ][:16],
            "member_examples": [
                _display_name(item["name"]) for item in members[:4]
            ],
            "proof_status": "hypothesis_only",
        })
    transport_families.sort(key=lambda item: (
        -item["critical_member_count"], -item["member_count"],
        -item["column_span"], item["candidate_id"],
    ))

    # Cheap structural pre-scan for fabric fixed-point product hypotheses:
    # FF-bounded LUT/CARRY runs on the supplied critical paths that end in a
    # register bus while idle DSP sites remain. Authority stays hypothesis
    # only; exact recovery happens in recover_fixed_point_product_contract.
    fixed_point_groups: dict[tuple, dict[str, Any]] = {}
    two_operand_groups: dict[str, dict[str, Any]] = {}
    for path_index, path in resolved_paths:
        index = 0
        while index < len(path) - 1:
            if not re.fullmatch(r"FD[A-Z0-9_]+", path[index]["type"]):
                index += 1
                continue
            cursor = index + 1
            run = []
            while cursor < len(path) and path[cursor]["type"].startswith(
                ("LUT", "CARRY")
            ):
                run.append(path[cursor])
                cursor += 1
            if (
                run
                and dsp_headroom
                and cursor < len(path)
                and re.fullmatch(r"FD[A-Z0-9_]+", path[cursor]["type"])
                and any(item["type"].startswith("CARRY") for item in run)
            ):
                key = (
                    tuple(item["type"] for item in run),
                    _normalized_stem(path[cursor]["name"]),
                )
                group = fixed_point_groups.setdefault(
                    key, {"paths": set(), "count": 0}
                )
                group["paths"].add(path_index)
                group["count"] += 1
                signature, sink_stem = key
                two_group = two_operand_groups.setdefault(sink_stem, {
                    "paths": set(), "count": 0,
                    "source_stems": set(), "signature": (),
                })
                two_group["paths"].add(path_index)
                two_group["count"] += 1
                two_group["source_stems"].add(
                    _normalized_stem(path[index]["name"])
                )
                if len(signature) > len(two_group["signature"]):
                    two_group["signature"] = signature
            index = cursor if run else index + 1

    fixed_point_families = []
    for (signature, sink_stem), group in fixed_point_groups.items():
        bus_width = sum(
            len(members)
            for (stem, _cell_type), members in register_groups.items()
            if stem == sink_stem
        )
        payload = {"signature": list(signature), "sink_stem": sink_stem}
        fixed_point_families.append({
            "candidate_id": _candidate_id("fixedpoint", payload),
            "kind": "fixed_point_product_hypothesis",
            "cone_cell_count": len(signature),
            "carry_primitive_count": sum(
                item.startswith("CARRY") for item in signature
            ),
            "lut_primitive_count": sum(
                item.startswith("LUT") for item in signature
            ),
            "sink_bus_width_estimate": bus_width,
            "occurrence_count": group["count"],
            "critical_path_count": len(group["paths"]),
            "authority": _HYPOTHESIS_AUTHORITY,
            "read_only": True,
            "mutation_allowed": False,
            "proof_status": "hypothesis_only",
        })
    fixed_point_families.sort(key=lambda item: (
        -item["critical_path_count"], -item["occurrence_count"],
        -item["cone_cell_count"], item["candidate_id"],
    ))

    # Cheap structural pre-scan for two-operand product hypotheses: the same
    # FF-bounded LUT/CARRY runs, grouped per sink bus stem and requiring at
    # least TWO distinct registered source-bus stems (variable x variable).
    # Authority stays hypothesis only; exact recovery happens in
    # recover_two_operand_contract.
    def _stem_register_width(stem: str) -> int:
        return sum(
            len(members)
            for (member_stem, _cell_type), members in register_groups.items()
            if member_stem == stem
        )

    two_operand_families = []
    for sink_stem, group in two_operand_groups.items():
        source_stems = sorted(group["source_stems"] - {sink_stem})
        if len(source_stems) < 2:
            continue
        signature = group["signature"]
        payload = {
            "signature": list(signature),
            "sink_stem": sink_stem,
            "source_stems": source_stems,
        }
        two_operand_families.append({
            "candidate_id": _candidate_id("twooperand", payload),
            "kind": "two_operand_product_hypothesis",
            "cone_cell_count": len(signature),
            "carry_primitive_count": sum(
                item.startswith("CARRY") for item in signature
            ),
            "lut_primitive_count": sum(
                item.startswith("LUT") for item in signature
            ),
            "input_bus_count": len(source_stems),
            "input_bus_width_estimates": sorted(
                (_stem_register_width(stem) for stem in source_stems),
                reverse=True,
            )[:2],
            "sink_bus_width_estimate": _stem_register_width(sink_stem),
            "occurrence_count": group["count"],
            "critical_path_count": len(group["paths"]),
            "authority": _HYPOTHESIS_AUTHORITY,
            "read_only": True,
            "mutation_allowed": False,
            "proof_status": "hypothesis_only",
        })
    two_operand_families.sort(key=lambda item: (
        -item["critical_path_count"], -item["occurrence_count"],
        -item["cone_cell_count"], item["candidate_id"],
    ))

    # Cheap structural pre-scan for DSP input register-absorb hypotheses: a
    # critical-path FF that directly drives a DSP hard macro through an A/B
    # input pin token, with the FF's normalized bus stem sizing the bus.
    # Authority stays hypothesis only; exact recovery happens in
    # recover_register_absorb_contract.
    register_absorb_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path_index, path in enumerate(critical_paths_data or []):
        previous = None
        for token in path:
            cell = _resolve_path_cell(design, token)
            if cell is None:
                continue
            name = str(cell.getName())
            cell_type = str(cell.getType()).upper()
            side_match = re.search(r"/(A|B)\[\d+\]$", str(token).upper())
            if (
                previous is not None
                and re.fullmatch(r"FD[A-Z0-9_]+", previous[1])
                and _hard_macro_class(cell_type) == "dsp"
                and side_match is not None
            ):
                key = (
                    _normalized_stem(previous[0]),
                    name,
                    side_match.group(1).lower(),
                )
                group = register_absorb_groups.setdefault(
                    key, {"paths": set(), "count": 0}
                )
                group["paths"].add(path_index)
                group["count"] += 1
            previous = (name, cell_type)

    register_absorb_families = []
    for (source_stem, _dsp_name, side), group in register_absorb_groups.items():
        bus_width = sum(
            len(members)
            for (stem, cell_type), members in register_groups.items()
            if stem == source_stem and cell_type == "FDRE"
        )
        if bus_width < 2:
            continue
        payload = {
            "source_stem": source_stem, "dsp": _dsp_name, "side": side,
        }
        register_absorb_families.append({
            "candidate_id": _candidate_id("regabsorb", payload),
            "kind": "register_absorb_hypothesis",
            "dsp_side": side,
            "bus_width": bus_width,
            "occurrence_count": group["count"],
            "critical_path_count": len(group["paths"]),
            "authority": _HYPOTHESIS_AUTHORITY,
            "read_only": True,
            "mutation_allowed": False,
            "proof_status": "hypothesis_only",
        })
    register_absorb_families.sort(key=lambda item: (
        -item["critical_path_count"], -item["occurrence_count"],
        -item["bus_width"], item["candidate_id"],
    ))

    # Cheap structural pre-scan for DSP48E2 MREG pipeline-rebalance
    # hypotheses: critical paths traversing a DSP48E2. Entering through an
    # A/B operand pin token suggests the input-routing leg is critical (the
    # backward move MREG -> AREG/BREG re-cuts it); a P output token suggests
    # the multiplier/ALU leg is critical (the forward move AREG/BREG -> MREG
    # re-cuts it). Observed A/B indices size the operand-width estimates.
    # Authority stays hypothesis only; exact recovery happens in
    # recover_pipeline_rebalance_contract.
    pipeline_rebalance_groups: dict[tuple[str, str], dict[str, Any]] = {}
    pipeline_rebalance_widths: dict[str, dict[str, int]] = {}
    for path_index, path in enumerate(critical_paths_data or []):
        previous = None
        for token in path:
            cell = _resolve_path_cell(design, token)
            if cell is None:
                continue
            name = str(cell.getName())
            cell_type = str(cell.getType()).upper()
            # Vivado-written DCPs place the DSP48E2 as a site macro whose
            # physical cells are the DSP_* internal leaf primitives; fold
            # those back onto the macro boundary name so both forms group
            # under one DSP. A leaf-only exact DSP48E2 match found zero
            # DSPs on real designs.
            dsp_name = None
            if cell_type == "DSP48E2":
                dsp_name = name
            elif cell_type.startswith("DSP_") and "/" in name:
                dsp_name = name.rsplit("/", 1)[0]
            if dsp_name is not None:
                token_upper = str(token).upper()
                operand = _pipeline_rebalance_operand_token(token_upper)
                output = re.search(r"/P\[\d+\]$", token_upper)
                if operand is not None:
                    widths = pipeline_rebalance_widths.setdefault(
                        dsp_name, {"a": 0, "b": 0}
                    )
                    side, bit_index = operand
                    widths[side] = max(widths[side], bit_index + 1)
                direction = None
                if operand is not None and previous is not None \
                        and previous[0] != dsp_name:
                    direction = "backward"
                elif output is not None:
                    direction = "forward"
                if direction is not None:
                    group = pipeline_rebalance_groups.setdefault(
                        (dsp_name, direction), {"paths": set(), "count": 0}
                    )
                    group["paths"].add(path_index)
                    group["count"] += 1
            previous = (dsp_name if dsp_name is not None else name, cell_type)

    pipeline_rebalance_families = []
    for (dsp_name, direction), group in pipeline_rebalance_groups.items():
        widths = pipeline_rebalance_widths.get(dsp_name, {"a": 0, "b": 0})
        payload = {"dsp": dsp_name, "direction": direction}
        pipeline_rebalance_families.append({
            "candidate_id": _candidate_id("rebalance", payload),
            "kind": "pipeline_rebalance_hypothesis",
            "direction": direction,
            "a_width": widths["a"],
            "b_width": widths["b"],
            "occurrence_count": group["count"],
            "critical_path_count": len(group["paths"]),
            "authority": _HYPOTHESIS_AUTHORITY,
            "read_only": True,
            "mutation_allowed": False,
            "proof_status": "hypothesis_only",
        })
    pipeline_rebalance_families.sort(key=lambda item: (
        -item["critical_path_count"], -item["occurrence_count"],
        -item["a_width"], -item["b_width"], item["candidate_id"],
    ))

    clock_nets = []
    for net in _as_list(design.getNets()):
        try:
            if not bool(net.isClockNet()):
                continue
        except Exception:
            continue
        sites_by_region: dict[str, set[str]] = {}
        hard_macro_sites = set()
        for pin in _as_list(net.getSinkPins()):
            try:
                site_inst = pin.getSiteInst()
                site = site_inst.getSite() if site_inst is not None else None
                if site is None:
                    continue
                site_name = str(site.getName())
                region = _safe_call(site.getTile(), "getClockRegion")
                region_name = str(region) if region is not None else "unknown"
                sites_by_region.setdefault(region_name, set()).add(site_name)
                if any(
                    _hard_macro_class(str(cell.getType())) is not None
                    for cell in _as_list(site_inst.getCells())
                ):
                    hard_macro_sites.add(site_name)
            except Exception:
                continue
        all_sink_sites = set().union(*sites_by_region.values()) if sites_by_region else set()
        clock_nets.append({
            "clock_net": str(net.getName()),
            "sink_site_count": len(all_sink_sites),
            "hard_macro_sink_site_count": len(hard_macro_sites),
            "region_sink_sites": {
                key: len(value) for key, value in sorted(sites_by_region.items())
            },
        })
    clock_nets.sort(key=lambda item: (-item["sink_site_count"], item["clock_net"]))

    # Registered-product cold recovery needs the anonymous endpoint-owner
    # families that were historically exposed by operator-mining-v1.  Keep
    # this as read-only hypothesis evidence; exact closure replay remains the
    # only authority for selecting a production bundle.
    sequential_families = []
    if critical_paths_data:
        try:
            from .sequential_contract import mine_sequential_write_families
        except ImportError:
            from sequential_contract import mine_sequential_write_families
        sequential_families = mine_sequential_write_families(
            design,
            critical_paths_data=critical_paths_data,
            max_families=max_families,
            design_sha256=design_sha256,
        )

    result = {
        "status": "success",
        "read_only": True,
        "mutation_allowed": False,
        "version": "operator-mining-v2",
        "authority": _HYPOTHESIS_AUTHORITY,
        "scope": {
            "critical_paths_supplied": len(critical_paths_data or []),
            "critical_paths_resolved": len(resolved_paths),
            "unresolved_path_tokens": unresolved_tokens,
            "placed_cell_count": sum(item["site"] is not None for item in records),
        },
        "resource_census": {
            "used_dsp_sites": used_dsp_sites,
            "logical_dsp_primitive_cells": logical_dsp_primitive_count,
            "device_dsp_sites": dsp_site_count if dsp_site_count else None,
            "idle_dsp_estimate": idle_dsp_estimate,
            "used_hard_macros": {
                kind: len(macro_sites[kind]) for kind in ("dsp", "bram", "uram")
            },
            "hard_macro_columns": {
                kind: sorted(columns)[:64] for kind, columns in macro_columns.items()
            },
        },
        "clock_footprint": {"clock_nets": clock_nets[:16]},
        "repeated_arithmetic_motif_evidence": _ordered_path_motif_evidence(
            resolved_paths
        ),
        "repeated_path_families": motif_families[:max_families],
        "recurrence_boundary_families": recurrence_families[:max_families],
        "register_transport_families": transport_families[:max_families],
        "sequential_write_families": sequential_families[:max_families],
        "fixed_point_product_hypothesis_count": len(fixed_point_families),
        "fixed_point_families": fixed_point_families[:max_families],
        "two_operand_product_hypothesis_count": len(two_operand_families),
        "two_operand_families": two_operand_families[:max_families],
        "register_absorb_hypothesis_count": len(register_absorb_families),
        "register_absorb_families": register_absorb_families[:max_families],
        "pipeline_rebalance_hypothesis_count": len(pipeline_rebalance_families),
        "pipeline_rebalance_families": (
            pipeline_rebalance_families[:max_families]
        ),
        "control_to_data_state_equation_live_hint_count": min(
            len(control_state_live_cells), 64
        ),
        "proof_status": "hypothesis_only",
        "limitations": [
            "path motifs compare primitive-type sequences, not complete fan-in graph isomorphism",
            "continuous sequence similarity and primitive counts do not prove arithmetic semantics",
            "clock_as_data_path is a risk flag, not authorization to alter clocks or constraints",
            "register families use normalized names and placement span, not proven recurrence connectivity",
            "fixed-point families are FF-bounded LUT/CARRY run counts, not proven product cones",
            "two-operand families are multi-source-bus FF-bounded run counts, not proven variable products",
            "register-absorb families are FF-to-DSP input adjacency counts, not proven absorbable register buses",
            "pipeline-rebalance families are critical-path DSP traversal counts, not proven retiming-eligible register configurations",
            "live FDSE cells only schedule exact state-equation recovery; they do not prove set-implies-enable semantics",
            "idle DSP and clock footprint are resource facts, not timing-benefit predictions",
            "a fresh hash-bound bit-vector and emitted-topology proof is required before mutation",
        ],
    }
    if design_sha256 is not None:
        result["design_sha256"] = design_sha256
    return result
