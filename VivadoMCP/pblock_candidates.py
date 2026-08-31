"""Helpers for deriving generic pblock candidates."""

from __future__ import annotations

import re
from typing import Any, Iterable


_SLICE_SITE_RE = re.compile(r"^SLICE_X(\d+)Y(\d+)$")
_CLOCK_REGION_RANGE_RE = re.compile(
    r"^CLOCKREGION_X(\d+)Y(\d+):CLOCKREGION_X(\d+)Y(\d+)$"
)


def _clamp_window(start: int, span: int, lower: int, upper: int) -> tuple[int, int]:
    """Clamp an inclusive start/span window into lower..upper bounds."""
    span = max(0, min(int(span), upper - lower))
    start = max(lower, min(int(start), upper - span))
    return start, start + span


def _add_candidate(
    candidates: list[dict[str, Any]],
    seen: set[str],
    *,
    name: str,
    kind: str,
    x_min: int,
    x_max: int,
    y_min: int,
    y_max: int,
    **metadata: Any,
) -> None:
    if x_min > x_max or y_min > y_max:
        return
    range_str = f"SLICE_X{x_min}Y{y_min}:SLICE_X{x_max}Y{y_max}"
    if range_str in seen:
        return
    seen.add(range_str)
    candidate = {
        "name": name,
        "kind": kind,
        "range": range_str,
        "x_min": x_min,
        "x_max": x_max,
        "y_min": y_min,
        "y_max": y_max,
        "span_x": x_max - x_min,
        "span_y": y_max - y_min,
    }
    candidate.update(metadata)
    candidates.append(candidate)


def _count(data: dict[str, Any] | None, *keys: str) -> int:
    if not data:
        return 0
    for key in keys:
        value = data.get(key)
        if value is not None:
            return int(value)
    return 0


def _normalize_requirements(value: dict[str, Any] | None) -> dict[str, int]:
    return {
        "lut": _count(value, "lut", "luts"),
        "ff": _count(value, "ff", "ffs"),
        "dsp": _count(value, "dsp", "dsps"),
        "bram": _count(value, "bram", "brams"),
        "uram": _count(value, "uram", "urams"),
    }


def _clock_region_range(coords: Iterable[tuple[int, int]]) -> str:
    points = list(coords)
    if not points:
        return ""
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        f"CLOCKREGION_X{min(xs)}Y{min(ys)}:"
        f"CLOCKREGION_X{max(xs)}Y{max(ys)}"
    )


def _clock_region_capacity(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    capacity = {"slice_sites": 0, "lut": 0, "ff": 0, "dsp": 0, "bram": 0, "uram": 0}
    for record in records:
        resources = record.get("resources") if isinstance(record, dict) else None
        slices = _count(resources, "slice_sites", "slices")
        capacity["slice_sites"] += slices
        capacity["lut"] += (
            _count(resources, "lut_capacity")
            if resources and "lut_capacity" in resources
            else slices * 4
        )
        capacity["ff"] += (
            _count(resources, "ff_capacity")
            if resources and "ff_capacity" in resources
            else slices * 8
        )
        capacity["dsp"] += _count(resources, "dsp_sites", "dsps", "dsp")
        capacity["bram"] += _count(resources, "bram_sites", "brams", "bram")
        capacity["uram"] += _count(resources, "uram_sites", "urams", "uram")
    return capacity


def _capacity_holds(
    capacity: dict[str, int], requirements: dict[str, int]
) -> bool:
    return all(capacity[key] >= requirements[key] for key in requirements)


def clock_region_ranges_overlap(left: str, right: str) -> bool:
    """Return whether two inclusive CLOCKREGION rectangles overlap."""
    left_match = _CLOCK_REGION_RANGE_RE.fullmatch(str(left).strip())
    right_match = _CLOCK_REGION_RANGE_RE.fullmatch(str(right).strip())
    if left_match is None or right_match is None:
        raise ValueError("clock-region range is malformed")
    lx0, ly0, lx1, ly1 = map(int, left_match.groups())
    rx0, ry0, rx1, ry1 = map(int, right_match.groups())
    return not (lx1 < rx0 or rx1 < lx0 or ly1 < ry0 or ry1 < ly0)


def build_clock_region_pblock_candidates(
    clock_regions: Iterable[dict[str, Any]],
    *,
    critical_requirements: dict[str, Any] | None,
    remainder_requirements: dict[str, Any] | None,
    max_single_candidates: int = 2,
    max_multi_candidates: int = 2,
) -> dict[str, Any]:
    """Build capacity-safe clock-aligned single and two-part siblings.

    The grid is supplied by RapidWright's Device API.  Every emitted rectangle
    is expressed in Vivado's normal ``CLOCKREGION_X..Y..`` pblock range format.
    Two-region candidates are guillotine partitions of that grid, so their
    critical and remainder pblocks are mutually exclusive by construction.
    """
    normalized = []
    seen = set()
    for item in clock_regions:
        if not isinstance(item, dict):
            continue
        x = int(item["x"])
        y = int(item["y"])
        if (x, y) in seen:
            continue
        seen.add((x, y))
        normalized.append({**item, "x": x, "y": y})
    normalized.sort(key=lambda item: (item["y"], item["x"]))
    if not normalized:
        return {"candidates": [], "reason": "empty clock-region grid"}

    critical = _normalize_requirements(critical_requirements)
    remainder = _normalize_requirements(remainder_requirements)
    total = {key: critical[key] + remainder[key] for key in critical}
    xs = sorted({item["x"] for item in normalized})
    ys = sorted({item["y"] for item in normalized})
    by_coord = {(item["x"], item["y"]): item for item in normalized}

    weighted = [
        item for item in normalized if int(item.get("critical_cell_count", 0)) > 0
    ]
    weight_key = "critical_cell_count"
    if not weighted:
        weighted = [
            item for item in normalized if int(item.get("occupied_cell_count", 0)) > 0
        ]
        weight_key = "occupied_cell_count"
    if weighted:
        total_weight = sum(max(0, int(item.get(weight_key, 0))) for item in weighted)
        centroid_x = sum(
            item["x"] * max(0, int(item.get(weight_key, 0))) for item in weighted
        ) / max(1, total_weight)
        centroid_y = sum(
            item["y"] * max(0, int(item.get(weight_key, 0))) for item in weighted
        ) / max(1, total_weight)
        anchor = min(
            weighted,
            key=lambda item: (
                abs(item["x"] - centroid_x) + abs(item["y"] - centroid_y),
                -int(item.get(weight_key, 0)),
                item["y"],
                item["x"],
            ),
        )
        anchor_x, anchor_y = anchor["x"], anchor["y"]
    else:
        anchor_x, anchor_y = xs[len(xs) // 2], ys[len(ys) // 2]

    def rectangle(x0: int, x1: int, y0: int, y1: int) -> list[dict[str, Any]]:
        return [
            by_coord[(x, y)]
            for y in ys
            for x in xs
            if x0 <= x <= x1 and y0 <= y <= y1 and (x, y) in by_coord
        ]

    singles = []
    single_options = []
    for x0 in xs:
        for x1 in xs:
            if x0 > anchor_x or x1 < anchor_x or x0 > x1:
                continue
            for y0 in ys:
                for y1 in ys:
                    if y0 > anchor_y or y1 < anchor_y or y0 > y1:
                        continue
                    records = rectangle(x0, x1, y0, y1)
                    capacity = _clock_region_capacity(records)
                    if _capacity_holds(capacity, total):
                        width = x1 - x0 + 1
                        height = y1 - y0 + 1
                        center_distance = abs((x0 + x1) / 2.0 - anchor_x) + abs(
                            (y0 + y1) / 2.0 - anchor_y
                        )
                        critical_coverage = sum(
                            max(0, int(item.get("critical_cell_count", 0)))
                            for item in records
                        )
                        single_options.append((
                            max(width, height),
                            abs(width - height),
                            len(records),
                            -critical_coverage,
                            center_distance,
                            x0,
                            y0,
                            x1,
                            y1,
                            capacity,
                        ))
    single_options.sort(key=lambda item: item[:9])
    for ordinal, (_, _, _, _, _, x0, y0, x1, y1, capacity) in enumerate(
        single_options[: max(0, int(max_single_candidates))], 1
    ):
        range_text = _clock_region_range([(x0, y0), (x1, y1)])
        singles.append({
            "name": f"clock_aligned_{ordinal}",
            "kind": "clock_region_single",
            "range": range_text,
            "capacity": capacity,
            "requirements": total,
        })

    multi_options = []
    all_x0, all_x1, all_y0, all_y1 = xs[0], xs[-1], ys[0], ys[-1]
    for cut in xs[:-1]:
        sides = (
            (all_x0, cut, all_y0, all_y1),
            (xs[xs.index(cut) + 1], all_x1, all_y0, all_y1),
        )
        critical_side = sides[0] if anchor_x <= cut else sides[1]
        remainder_side = sides[1] if critical_side == sides[0] else sides[0]
        multi_options.append(("vertical", cut, critical_side, remainder_side))
    for cut in ys[:-1]:
        sides = (
            (all_x0, all_x1, all_y0, cut),
            (all_x0, all_x1, ys[ys.index(cut) + 1], all_y1),
        )
        critical_side = sides[0] if anchor_y <= cut else sides[1]
        remainder_side = sides[1] if critical_side == sides[0] else sides[0]
        multi_options.append(("horizontal", cut, critical_side, remainder_side))

    valid_multi = []
    for orientation, cut, critical_box, remainder_box in multi_options:
        critical_records = rectangle(*critical_box)
        remainder_records = rectangle(*remainder_box)
        critical_capacity = _clock_region_capacity(critical_records)
        remainder_capacity = _clock_region_capacity(remainder_records)
        if not _capacity_holds(critical_capacity, critical):
            continue
        if not _capacity_holds(remainder_capacity, remainder):
            continue
        critical_range = _clock_region_range(
            [(critical_box[0], critical_box[2]), (critical_box[1], critical_box[3])]
        )
        remainder_range = _clock_region_range(
            [(remainder_box[0], remainder_box[2]), (remainder_box[1], remainder_box[3])]
        )
        if clock_region_ranges_overlap(critical_range, remainder_range):
            continue
        valid_multi.append((
            len(critical_records),
            orientation,
            cut,
            critical_range,
            remainder_range,
            critical_capacity,
            remainder_capacity,
        ))
    valid_multi.sort(key=lambda item: item[:5])
    multis = []
    for ordinal, item in enumerate(
        valid_multi[: max(0, int(max_multi_candidates))], 1
    ):
        _, orientation, cut, critical_range, remainder_range, critical_capacity, remainder_capacity = item
        multis.append({
            "name": f"clock_multiregion_{orientation}_{cut}_{ordinal}",
            "kind": "clock_region_multi",
            "regions": [
                {
                    "role": "critical",
                    "range": critical_range,
                    "capacity": critical_capacity,
                    "requirements": critical,
                },
                {
                    "role": "remainder",
                    "range": remainder_range,
                    "capacity": remainder_capacity,
                    "requirements": remainder,
                },
            ],
        })

    return {
        "anchor": {"x": anchor_x, "y": anchor_y},
        "critical_requirements": critical,
        "remainder_requirements": remainder,
        "candidates": [*singles, *multis],
    }


def _normalize_fabric_region(region: dict[str, Any]) -> dict[str, int]:
    return {
        "col_min": int(region["col_min"]),
        "col_max": int(region["col_max"]),
        "row_min": int(region["row_min"]),
        "row_max": int(region["row_max"]),
    }


def _normalize_fabric_bounds(
    fabric_bounds: dict[str, Any] | None,
    base_region: dict[str, int],
) -> dict[str, int]:
    if not fabric_bounds:
        return dict(base_region)
    return {
        "col_min": int(
            fabric_bounds.get(
                "min_col",
                fabric_bounds.get("col_min", base_region["col_min"]),
            )
        ),
        "col_max": int(
            fabric_bounds.get(
                "max_col",
                fabric_bounds.get("col_max", base_region["col_max"]),
            )
        ),
        "row_min": int(
            fabric_bounds.get(
                "min_row",
                fabric_bounds.get("row_min", base_region["row_min"]),
            )
        ),
        "row_max": int(
            fabric_bounds.get(
                "max_row",
                fabric_bounds.get("row_max", base_region["row_max"]),
            )
        ),
    }


def _clip_fabric_region(
    region: dict[str, int],
    bounds: dict[str, int],
) -> dict[str, int]:
    return {
        "col_min": max(bounds["col_min"], min(region["col_min"], bounds["col_max"])),
        "col_max": max(bounds["col_min"], min(region["col_max"], bounds["col_max"])),
        "row_min": max(bounds["row_min"], min(region["row_min"], bounds["row_max"])),
        "row_max": max(bounds["row_min"], min(region["row_max"], bounds["row_max"])),
    }


def _fabric_span(region: dict[str, int], axis: str) -> int:
    return max(1, int(region[f"{axis}_max"]) - int(region[f"{axis}_min"]))


def _add_fabric_candidate(
    candidates: list[dict[str, Any]],
    seen: set[tuple[int, int, int, int]],
    *,
    name: str,
    base_region: dict[str, int],
    bounds: dict[str, int],
    region: dict[str, int],
    reasons: list[str],
) -> None:
    clipped = _clip_fabric_region(region, bounds)
    if (
        clipped["col_min"] > clipped["col_max"]
        or clipped["row_min"] > clipped["row_max"]
    ):
        return

    key = (
        clipped["col_min"],
        clipped["col_max"],
        clipped["row_min"],
        clipped["row_max"],
    )
    if key in seen:
        return
    seen.add(key)

    col_span = _fabric_span(clipped, "col")
    row_span = _fabric_span(clipped, "row")
    base_area = _fabric_span(base_region, "col") * _fabric_span(base_region, "row")
    area = col_span * row_span
    candidate = {
        "name": name,
        "region": clipped,
        "span_col": col_span,
        "span_row": row_span,
        "area": area,
        "expansion": area / max(1, base_area),
        "reasons": reasons,
    }
    candidates.append(candidate)


def _score_fabric_candidate(
    candidate: dict[str, Any],
    *,
    base_region: dict[str, int],
    bounds: dict[str, int],
    estimated_resources: dict[str, Any] | None,
    target_requirements: dict[str, Any] | None,
    design_resources: dict[str, Any] | None,
) -> tuple[float, list[str], dict[str, float]]:
    region = candidate["region"]
    device_area = _fabric_span(bounds, "col") * _fabric_span(bounds, "row")
    base_col_span = _fabric_span(base_region, "col")
    base_row_span = _fabric_span(base_region, "row")

    area_ratio = candidate["area"] / max(1, device_area)
    expansion = float(candidate["expansion"])
    left_margin = max(0, base_region["col_min"] - region["col_min"])
    right_margin = max(0, region["col_max"] - base_region["col_max"])
    lower_margin = max(0, base_region["row_min"] - region["row_min"])
    upper_margin = max(0, region["row_max"] - base_region["row_max"])

    real_hard_macros = (
        _count(design_resources, "dsp", "dsps")
        + _count(design_resources, "bram", "brams")
        + _count(design_resources, "uram", "urams")
    )
    estimated_hard_sites = (
        _count(estimated_resources, "dsp_sites", "dsps")
        + _count(estimated_resources, "bram_sites", "brams")
        + _count(estimated_resources, "uram_sites", "urams")
    )
    guidance_mode = (
        target_requirements is not None
        and _count(target_requirements, "ffs") == 0
    )
    macro_or_guidance = bool(real_hard_macros or estimated_hard_sites or guidance_mode)

    score = 100.0
    notes: list[str] = []

    if macro_or_guidance:
        # Macro/guidance pblocks need enough routing slack to seed a different
        # legal placement. This rewards bounded expansion, especially downward
        # and leftward slack, without letting candidates grow into full-device
        # constraints.
        score += min(max(expansion - 1.0, 0.0), 0.35) * 50.0
        score += min(lower_margin / base_row_span, 0.08) * 160.0
        score += min(upper_margin / base_row_span, 0.10) * 110.0
        score += min(left_margin / base_col_span, 0.08) * 140.0
        horizontal_margin = left_margin + right_margin
        if horizontal_margin > 0 and left_margin > 0 and right_margin > 0:
            balance = 1.0 - (
                abs(left_margin - right_margin) / max(1, horizontal_margin)
            )
            score += 12.0 + balance * 8.0
        elif region["col_min"] <= bounds["col_min"] or region["col_max"] >= bounds["col_max"]:
            score -= 14.0
        if candidate["name"] == "recommended":
            score -= 10.0
        if area_ratio < 0.18:
            score -= (0.18 - area_ratio) * 120.0
        elif area_ratio > 0.72:
            score -= (area_ratio - 0.72) * 160.0
        else:
            score += (0.72 - abs(area_ratio - 0.52)) * 10.0
        notes.append("macro_or_guidance")
    else:
        # Slice-only sweeps should stay compact. The separate pblock_sweep path
        # handles trialing multiple slice windows; dynamic pblock should avoid
        # turning into an unconstrained full-device placement.
        score -= max(expansion - 1.0, 0.0) * 30.0
        score += max(0.0, 0.35 - area_ratio) * 15.0
        notes.append("slice_compact")

    metrics = {
        "area_ratio": area_ratio,
        "expansion": expansion,
        "left_margin": float(left_margin),
        "right_margin": float(right_margin),
        "lower_margin": float(lower_margin),
        "upper_margin": float(upper_margin),
    }
    return score, notes, metrics


def build_fabric_pblock_candidates(
    recommended_region: dict[str, Any],
    *,
    fabric_bounds: dict[str, Any] | None = None,
    estimated_resources: dict[str, Any] | None = None,
    target_requirements: dict[str, Any] | None = None,
    design_resources: dict[str, Any] | None = None,
    max_candidates: int = 6,
) -> dict[str, Any]:
    """
    Build and rank generic fabric-region pblock candidates.

    The inputs are the structured values returned by RapidWright
    analyze_fabric_for_pblock plus the real design resource counts parsed from
    Vivado. The ranking is intentionally benchmark-agnostic: it scores bounded
    routing slack, geometry, and whether the design is hard-macro/guidance
    dominated.
    """
    base_region = _normalize_fabric_region(recommended_region)
    bounds = _normalize_fabric_bounds(fabric_bounds, base_region)

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    _add_fabric_candidate(
        candidates,
        seen,
        name="recommended",
        base_region=base_region,
        bounds=bounds,
        region=base_region,
        reasons=["rapidwright_recommended_region"],
    )

    col_span = _fabric_span(base_region, "col")
    row_span = _fabric_span(base_region, "row")
    col_guard = max(8, round(col_span * 0.06))
    row_lower_guard = max(4, round(row_span * 0.04))
    row_upper_guard = max(8, round(row_span * 0.075))

    _add_fabric_candidate(
        candidates,
        seen,
        name="asymmetric_route_guard",
        base_region=base_region,
        bounds=bounds,
        region={
            "col_min": base_region["col_min"] - col_guard,
            "col_max": base_region["col_max"],
            "row_min": base_region["row_min"] - row_lower_guard,
            "row_max": base_region["row_max"] + row_upper_guard,
        },
        reasons=["left_guardband", "vertical_route_guardband"],
    )
    _add_fabric_candidate(
        candidates,
        seen,
        name="row_route_guard",
        base_region=base_region,
        bounds=bounds,
        region={
            "col_min": base_region["col_min"],
            "col_max": base_region["col_max"],
            "row_min": base_region["row_min"] - row_lower_guard,
            "row_max": base_region["row_max"] + row_upper_guard,
        },
        reasons=["vertical_route_guardband"],
    )
    _add_fabric_candidate(
        candidates,
        seen,
        name="balanced_route_guard",
        base_region=base_region,
        bounds=bounds,
        region={
            "col_min": base_region["col_min"] - col_guard,
            "col_max": base_region["col_max"] + col_guard,
            "row_min": base_region["row_min"] - row_lower_guard,
            "row_max": base_region["row_max"] + row_upper_guard,
        },
        reasons=["symmetric_column_guardband", "vertical_route_guardband"],
    )

    device_col_span = _fabric_span(bounds, "col")
    device_row_span = _fabric_span(bounds, "row")
    wide_col_span = max(col_span + col_guard, round(device_col_span * 0.60))
    wide_row_span = max(
        row_span + row_lower_guard + row_upper_guard,
        round(device_row_span * 0.70),
    )
    center_row = int(
        recommended_region.get(
            "center_of_mass_row",
            (base_region["row_min"] + base_region["row_max"]) // 2,
        )
    )
    wide_row_min = center_row - wide_row_span // 2
    _add_fabric_candidate(
        candidates,
        seen,
        name="wide_route_corridor",
        base_region=base_region,
        bounds=bounds,
        region={
            "col_min": base_region["col_max"] - wide_col_span,
            "col_max": base_region["col_max"],
            "row_min": wide_row_min,
            "row_max": wide_row_min + wide_row_span,
        },
        reasons=["broad_macro_corridor"],
    )
    macro_col_span = max(
        col_span + 2 * col_guard,
        round(device_col_span * 0.508),
    )
    macro_row_span = max(
        row_span + row_lower_guard + row_upper_guard,
        round(device_row_span * 0.84),
    )
    base_center_col = (base_region["col_min"] + base_region["col_max"]) // 2
    if "center_of_mass_col" in recommended_region:
        macro_center_col = int(recommended_region["center_of_mass_col"])
    else:
        macro_center_col = base_center_col
    if col_span < round(device_col_span * 0.12):
        macro_center_col += round(device_col_span * 0.06)
    macro_col_min, macro_col_max = _clamp_window(
        macro_center_col - macro_col_span // 2,
        macro_col_span,
        bounds["col_min"],
        bounds["col_max"],
    )
    macro_center_row = center_row
    if col_span < round(device_col_span * 0.12):
        macro_center_row += round(device_row_span * 0.016)
    macro_row_min, macro_row_max = _clamp_window(
        macro_center_row - macro_row_span // 2,
        macro_row_span,
        bounds["row_min"],
        bounds["row_max"],
    )
    _add_fabric_candidate(
        candidates,
        seen,
        name="macro_route_corridor",
        base_region=base_region,
        bounds=bounds,
        region={
            "col_min": macro_col_min,
            "col_max": macro_col_max,
            "row_min": macro_row_min,
            "row_max": macro_row_max,
        },
        reasons=["balanced_hard_macro_route_corridor"],
    )

    for candidate in candidates:
        score, score_notes, metrics = _score_fabric_candidate(
            candidate,
            base_region=base_region,
            bounds=bounds,
            estimated_resources=estimated_resources,
            target_requirements=target_requirements,
            design_resources=design_resources,
        )
        candidate["score"] = round(score, 3)
        candidate["score_notes"] = score_notes
        candidate["metrics"] = metrics

    candidates.sort(
        key=lambda item: (
            -float(item["score"]),
            item["area"],
            item["name"],
        )
    )

    return {
        "recommended_region": base_region,
        "fabric_bounds": bounds,
        "candidates": candidates[: max(1, int(max_candidates))],
    }


def build_slice_pblock_candidates(
    slice_sites: Iterable[str],
    *,
    x_paddings: Iterable[int] = (0,),
    y_paddings: Iterable[int] = (0,),
    device_bounds: dict[str, int] | None = None,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Return deduplicated SLICE pblock ranges around occupied slice sites."""
    points = []
    for site in slice_sites:
        match = _SLICE_SITE_RE.match(str(site).strip())
        if match:
            points.append((int(match.group(1)), int(match.group(2))))

    if not points:
        return {
            "site_count": 0,
            "bbox": None,
            "candidates": [],
            "reason": "no SLICE sites found",
        }

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    bbox = {
        "x_min": min(xs),
        "x_max": max(xs),
        "y_min": min(ys),
        "y_max": max(ys),
    }

    bounds = {
        "x_min": bbox["x_min"],
        "x_max": bbox["x_max"],
        "y_min": bbox["y_min"],
        "y_max": bbox["y_max"],
    }
    if device_bounds:
        bounds.update(device_bounds)

    candidates = []
    seen = set()

    for x_pad, y_pad in zip(x_paddings, y_paddings, strict=False):
        x_min = max(bounds["x_min"], bbox["x_min"] - int(x_pad))
        x_max = min(bounds["x_max"], bbox["x_max"] + int(x_pad))
        y_min = max(bounds["y_min"], bbox["y_min"] - int(y_pad))
        y_max = min(bounds["y_max"], bbox["y_max"] + int(y_pad))
        _add_candidate(
            candidates,
            seen,
            name=f"slice_bbox_xpad{x_pad}_ypad{y_pad}",
            kind="bbox",
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            x_padding=int(x_pad),
            y_padding=int(y_pad),
        )
        if len(candidates) >= max_candidates:
            break

    candidates = candidates[:max_candidates]

    return {
        "site_count": len(points),
        "bbox": bbox,
        "device_bounds": bounds,
        "candidates": candidates,
    }
