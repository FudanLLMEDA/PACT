"""Coordinate-free placement rules for registered product-sum emitters.

The certificate stores only relative geometry.  Absolute sites may appear only
in a runtime apply receipt after an actual seed has supplied consumer anchors
and a legality oracle has checked every selected site.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .regarith_tools.common import object_digest
from .registered_bridge import validate_registered_bridge_bundle


SCHEMA_VERSION = "regarith-relative-placement-v1"
_SITE = re.compile(r"(?P<kind>DSP48E2|SLICE)_X(?P<x>\d+)Y(?P<y>\d+)")


class RelativePlacementError(RuntimeError):
    pass


def _semantic_key(plan: Mapping[str, Any]) -> str:
    return object_digest({
        str(key): value for key, value in plan.items() if key != "module_name"
    })


def build_product_sum_relative_placement_manifest(
    bundle_value: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract deterministic emitter rules without an absolute coordinate."""
    try:
        bundle = validate_registered_bridge_bundle(dict(bundle_value))
    except (TypeError, ValueError) as exc:
        raise RelativePlacementError("RELATIVE_PLACEMENT_BUNDLE_INVALID") from exc
    modules: dict[str, dict[str, Any]] = {}
    module_rule_ids: dict[str, str] = {}
    for plan in bundle["plans"]:
        width = int(plan["data_width"])
        terms = list(plan["terms"])
        product_terms = [
            term for term in terms
            if term["kind"] == "fixed_product_slice"
        ]
        semantic_key = _semantic_key(plan)
        rule = {
            "semantic_key": semantic_key,
            "dsp_pairs": {
                "pair_count": len(product_terms),
                "relation": "same_column_consecutive_y",
                "ordering": "term_index_secondary_then_registered",
            },
            "compressor_chain": {
                "length": (width + 7) // 8,
                "relation": "same_column_consecutive_y",
                "ordering": "canonical_cell_name_vertical",
            },
            "register_logic_cluster": {
                "relation": "bounded_window_around_dsp_and_compressor_anchor",
                "slice_radius_x": 4,
                "slice_radius_y": 8,
            },
        }
        rule_id = object_digest(rule)
        modules[rule_id] = rule
        module_rule_ids[str(plan["module_name"])] = rule_id
    asset_body = {
        "schema_version": SCHEMA_VERSION,
        "kind": "registered_product_sum_relative_geometry_asset",
        "anchor_policy": "nearest_legal_region_to_retained_output_consumers",
        "site_legality": "runtime_type_occupancy_and_relative_rule_recheck",
        "degraded_fallback": "unguided_global_placement_typed_receipt",
        "modules": {key: modules[key] for key in sorted(modules)},
        "absolute_coordinates": [],
    }
    asset = {**asset_body, "asset_digest": object_digest(asset_body)}
    bindings = sorted(
        ({
            "instance_name": str(instance["name"]),
            "rule_id": module_rule_ids[str(instance["module_name"])],
        } for instance in bundle["instances"]),
        key=lambda row: row["instance_name"],
    )
    body = {
        "schema_version": SCHEMA_VERSION,
        "kind": "registered_product_sum_relative_placement_manifest",
        "geometry_asset": asset,
        "runtime_bindings": bindings,
        "authority": "pre_route_guidance_only",
    }
    return validate_product_sum_relative_placement_manifest({
        **body, "manifest_digest": object_digest(body)
    })


def validate_product_sum_relative_placement_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RelativePlacementError("RELATIVE_PLACEMENT_MANIFEST_REQUIRED")
    manifest = copy.deepcopy(dict(value))
    digest = manifest.pop("manifest_digest", None)
    asset = manifest.get("geometry_asset")
    bindings = manifest.get("runtime_bindings")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("kind")
        != "registered_product_sum_relative_placement_manifest"
        or manifest.get("authority") != "pre_route_guidance_only"
        or not isinstance(asset, dict)
        or not isinstance(bindings, list)
        or not bindings
        or digest != object_digest(manifest)
    ):
        raise RelativePlacementError("RELATIVE_PLACEMENT_MANIFEST_INVALID")
    asset_digest = asset.pop("asset_digest", None)
    if (
        asset.get("schema_version") != SCHEMA_VERSION
        or asset.get("kind")
        != "registered_product_sum_relative_geometry_asset"
        or asset.get("absolute_coordinates") != []
        or not isinstance(asset.get("modules"), dict)
        or not asset["modules"]
        or asset_digest != object_digest(asset)
    ):
        raise RelativePlacementError("RELATIVE_PLACEMENT_ASSET_INVALID")
    asset["asset_digest"] = asset_digest
    names: set[str] = set()
    for binding in bindings:
        if (
            not isinstance(binding, dict)
            or set(binding) != {"instance_name", "rule_id"}
            or not isinstance(binding["instance_name"], str)
            or not binding["instance_name"]
            or binding["instance_name"] in names
            or binding["rule_id"] not in asset["modules"]
        ):
            raise RelativePlacementError("RELATIVE_PLACEMENT_BINDING_INVALID")
        names.add(binding["instance_name"])
    encoded = str({**manifest, "geometry_asset": asset})
    if "DSP48E2_X" in encoded or "SLICE_X" in encoded:
        raise RelativePlacementError("RELATIVE_PLACEMENT_ABSOLUTE_SITE_FORBIDDEN")
    manifest["geometry_asset"] = asset
    manifest["manifest_digest"] = digest
    return manifest


def geometry_asset_from_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(
        validate_product_sum_relative_placement_manifest(value)["geometry_asset"]
    )


def allocate_relative_geometry(
    manifest_value: Mapping[str, Any],
    *,
    cell_inventory: Mapping[str, Mapping[str, list[str]]],
    oracle_slots: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pure deterministic apply-time legality model used by runners/tests."""
    manifest = validate_product_sum_relative_placement_manifest(manifest_value)
    available: dict[str, dict[str, Any]] = {}
    for raw in oracle_slots:
        slot = dict(raw)
        match = _SITE.fullmatch(str(slot.get("site") or ""))
        if (
            match is None
            or slot.get("legal") is not True
            or slot.get("occupied") not in {True, False}
            or isinstance(slot.get("distance"), bool)
            or not isinstance(slot.get("distance"), (int, float))
            or float(slot["distance"]) < 0
        ):
            continue
        slot.update({
            "kind": match.group("kind"),
            "x": int(match.group("x")),
            "y": int(match.group("y")),
        })
        if not slot["occupied"]:
            available[slot["site"]] = slot
    placements: list[dict[str, str]] = []
    used: set[str] = set()
    try:
        for binding in manifest["runtime_bindings"]:
            name = binding["instance_name"]
            cells = cell_inventory.get(name)
            if not isinstance(cells, Mapping):
                raise RelativePlacementError("RELATIVE_PLACEMENT_CELL_INVENTORY_MISSING")
            rule = manifest["geometry_asset"]["modules"][binding["rule_id"]]
            dsp_cells = sorted(cells.get("DSP48E2") or [])
            carry_cells = sorted(cells.get("CARRY8") or [])
            pair_count = int(rule["dsp_pairs"]["pair_count"])
            chain_length = int(rule["compressor_chain"]["length"])
            if len(dsp_cells) != pair_count * 2 or len(carry_cells) != chain_length:
                raise RelativePlacementError("RELATIVE_PLACEMENT_CELL_INVENTORY_MISMATCH")
            selected_pairs = []
            if pair_count:
                pair_candidates = []
                for lower in available.values():
                    if lower["kind"] != "DSP48E2" or lower["site"] in used:
                        continue
                    upper = available.get(
                        f"DSP48E2_X{lower['x']}Y{lower['y'] + 1}"
                    )
                    if upper is not None and upper["site"] not in used:
                        pair_candidates.append((
                            float(lower["distance"]) + float(upper["distance"]),
                            lower["site"], upper["site"],
                        ))
                for candidate in sorted(pair_candidates):
                    _, lower, upper = candidate
                    if lower in used or upper in used:
                        continue
                    selected_pairs.append(candidate)
                    used.update((lower, upper))
                    if len(selected_pairs) == pair_count:
                        break
            for ordinal, (_, lower, upper) in enumerate(selected_pairs):
                placements.extend((
                    {"cell": dsp_cells[2 * ordinal], "site": lower},
                    {"cell": dsp_cells[2 * ordinal + 1], "site": upper},
                ))
            if sum(row["cell"] in dsp_cells for row in placements) < len(dsp_cells):
                raise RelativePlacementError("RELATIVE_PLACEMENT_DSP_PAIRS_UNAVAILABLE")
            chains = []
            for lower in available.values():
                if lower["kind"] != "SLICE" or lower["site"] in used:
                    continue
                sites = [f"SLICE_X{lower['x']}Y{lower['y'] + index}" for index in range(chain_length)]
                if all(site in available and site not in used for site in sites):
                    chains.append((sum(float(available[site]["distance"]) for site in sites), sites))
            if not chains:
                raise RelativePlacementError("RELATIVE_PLACEMENT_COMPRESSOR_CHAIN_UNAVAILABLE")
            _, sites = min(chains, key=lambda row: (row[0], row[1]))
            used.update(sites)
            placements.extend(
                {"cell": cell, "site": site}
                for cell, site in zip(carry_cells, sites, strict=True)
            )
    except RelativePlacementError as exc:
        body = {
            "schema_version": SCHEMA_VERSION,
            "status": "degraded",
            "mode": "unguided_global_placement",
            "reason_code": str(exc),
            "manifest_digest": manifest["manifest_digest"],
            "placements": [],
        }
        return {**body, "receipt_digest": object_digest(body)}
    body = {
        "schema_version": SCHEMA_VERSION,
        "status": "applied",
        "mode": "relative_geometry_guided",
        "reason_code": "",
        "manifest_digest": manifest["manifest_digest"],
        "placements": placements,
    }
    return {**body, "receipt_digest": object_digest(body)}


__all__ = [
    "RelativePlacementError",
    "allocate_relative_geometry",
    "build_product_sum_relative_placement_manifest",
    "geometry_asset_from_manifest",
    "validate_product_sum_relative_placement_manifest",
]
