"""Helpers for deriving generic pblock candidates."""

from __future__ import annotations

import re
from typing import Any, Iterable


_SLICE_SITE_RE = re.compile(r"^SLICE_X(\d+)Y(\d+)$")


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


def _add_center_window_candidates(
    candidates: list[dict[str, Any]],
    seen: set[str],
    bbox: dict[str, int],
    bounds: dict[str, int],
) -> None:
    """
    Add generic compact fabric windows in addition to occupied-bbox ranges.

    Some slice-only designs improve when the placer is forced into a fresh
    central window instead of preserving the current occupied bbox. These
    windows are derived only from device bounds plus the current bbox center.
    """
    device_span_x = bounds["x_max"] - bounds["x_min"]
    device_span_y = bounds["y_max"] - bounds["y_min"]
    bbox_span_x = bbox["x_max"] - bbox["x_min"]
    bbox_span_y = bbox["y_max"] - bbox["y_min"]
    if device_span_x <= 0 or device_span_y <= 0:
        return

    has_room_to_rewindow = (
        device_span_x > bbox_span_x * 1.25
        or device_span_y > bbox_span_y * 1.25
    )
    if not has_room_to_rewindow:
        return

    window_span_y = max(16, round(device_span_y * 0.65))
    bbox_center_x = round((bbox["x_min"] + bbox["x_max"]) / 2)

    x_bias = round(device_span_x * 0.02)
    lower_y_min, lower_y_max = _clamp_window(
        round(bounds["y_min"] + device_span_y * 0.20),
        window_span_y,
        bounds["y_min"],
        bounds["y_max"],
    )
    device_center_y = (bounds["y_min"] + bounds["y_max"]) // 2
    mid_y_min, mid_y_max = _clamp_window(
        device_center_y - window_span_y // 2,
        window_span_y,
        bounds["y_min"],
        bounds["y_max"],
    )

    for fraction, label in ((0.335, "x34"), (0.40, "x40")):
        window_span_x = max(8, round(device_span_x * fraction))
        x_min, x_max = _clamp_window(
            bbox_center_x + x_bias - window_span_x // 2,
            window_span_x,
            bounds["x_min"],
            bounds["x_max"],
        )
        _add_candidate(
            candidates,
            seen,
            name=f"slice_center_window_{label}_y65_lower",
            kind="center_window",
            x_min=x_min,
            x_max=x_max,
            y_min=lower_y_min,
            y_max=lower_y_max,
            x_fraction=fraction,
            y_fraction=0.65,
            y_anchor="lower",
            x_center_bias=x_bias,
        )

    window_span_x = max(8, round(device_span_x * 0.335))
    x_min, x_max = _clamp_window(
        bbox_center_x - window_span_x // 2,
        window_span_x,
        bounds["x_min"],
        bounds["x_max"],
    )
    _add_candidate(
        candidates,
        seen,
        name="slice_center_window_x34_y65_mid",
        kind="center_window",
        x_min=x_min,
        x_max=x_max,
        y_min=mid_y_min,
        y_max=mid_y_max,
        x_fraction=0.335,
        y_fraction=0.65,
        y_anchor="center",
        x_center_bias=0,
    )


def build_slice_pblock_candidates(
    slice_sites: Iterable[str],
    *,
    x_paddings: Iterable[int] = (0, 4, 8, 12),
    y_paddings: Iterable[int] = (0, 16, 32, 48),
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
    _add_center_window_candidates(candidates, seen, bbox, bounds)

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
