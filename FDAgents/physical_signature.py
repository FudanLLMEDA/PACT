"""Bounded benchmark-agnostic physical telemetry and distance mechanics."""

from __future__ import annotations

import hashlib
import json
import math
import time


SIGNATURE_VERSION = 1
MAX_PROFILE_BYTES = 65536


def unavailable_signature(status: str, reason: str, artifact_sha256: str) -> dict:
    return {
        "version": SIGNATURE_VERSION,
        "status": str(status),
        "artifact_sha256": str(artifact_sha256),
        "collected_at": time.time(),
        "runtime_s": 0.0,
        "reason": str(reason)[:300],
        "dimensions": {},
        "digest_sha256": "",
    }


def build_physical_signature(
    profile: dict,
    measurement: dict,
    *,
    artifact_sha256: str,
    runtime_s: float = 0.0,
    collected_at: float | None = None,
) -> dict:
    """Build a compact aggregate signature without logical or physical names."""
    placement = profile.get("placement_spread") or {}
    logic = profile.get("logic_levels") or {}
    required_numbers = (
        profile.get("path_count"),
        profile.get("avg_route_pct"),
        logic.get("max"),
        placement.get("path_count_with_sites"),
    )
    try:
        evidence = [float(value) for value in required_numbers]
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate physical profile lacks required evidence") from exc
    if any(not math.isfinite(value) for value in evidence) or evidence[0] <= 0:
        raise ValueError("candidate physical profile has invalid required evidence")
    site_counts = placement.get("site_type_counts") or {}

    def finite(value, default=0.0):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    dimensions = {
        "clock_period_ps": round(finite(measurement.get("clock_period_ns")) * 1000),
        "route_pct_bucket": round(finite(profile.get("avg_route_pct")) / 5) * 5,
        "max_logic_levels": round(finite(logic.get("max"))),
        "avg_logic_levels_x10": round(finite(logic.get("avg")) * 10),
        "spread_x_bucket": round(finite(placement.get("max_path_span_x")) / 5) * 5,
        "spread_y_bucket": round(finite(placement.get("max_path_span_y")) / 5) * 5,
        "spread_ratio_x10": round(
            10
            * finite(placement.get("spread_path_count"))
            / max(1.0, finite(placement.get("path_count_with_sites"), 1.0))
        ),
        "slice_present": int(finite(site_counts.get("SLICE")) > 0),
        "dsp_present": int(finite(site_counts.get("DSP48E2")) > 0),
        "bram_present": int(
            finite(site_counts.get("RAMB18")) + finite(site_counts.get("RAMB36")) > 0
        ),
        "uram_present": int(finite(site_counts.get("URAM288")) > 0),
        "route_error_count": round(finite(measurement.get("nets_with_routing_errors"))),
    }
    payload = {
        "version": SIGNATURE_VERSION,
        "artifact_sha256": str(artifact_sha256),
        "dimensions": dimensions,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return {
        "version": SIGNATURE_VERSION,
        "status": "complete",
        "artifact_sha256": str(artifact_sha256),
        "collected_at": time.time() if collected_at is None else float(collected_at),
        "runtime_s": float(runtime_s),
        "reason": "",
        "dimensions": dimensions,
        "digest_sha256": digest,
    }


async def collect_physical_signature(mcp, measurement: dict, cfg, artifact_sha256: str) -> dict:
    """Collect one bounded profile from the exact currently open canonical DCP."""
    started = time.monotonic()
    try:
        raw = await mcp.call_vivado(
            "report_timing_path_profile",
            {"num_paths": int(cfg.get("analysis.candidate_signature_num_paths", 20))},
            timeout=float(cfg.get("analysis.candidate_signature_timeout_s", 60.0)),
        )
        if len(raw.encode("utf-8")) > MAX_PROFILE_BYTES:
            raise ValueError("candidate physical profile exceeds byte limit")
        profile = json.loads(raw)
        if not isinstance(profile, dict):
            raise ValueError("candidate physical profile is not an object")
        return build_physical_signature(
            profile,
            measurement,
            artifact_sha256=artifact_sha256,
            runtime_s=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001
        result = unavailable_signature("failed", str(exc), artifact_sha256)
        result["runtime_s"] = time.monotonic() - started
        return result


def physical_signature_distance(left: dict, right: dict, minimum_common: int = 4):
    """Return normalized aggregate distance, or None for incomparable evidence."""
    if left.get("status") != "complete" or right.get("status") != "complete":
        return None
    if left.get("version") != SIGNATURE_VERSION or right.get("version") != SIGNATURE_VERSION:
        return None
    left_dims = left.get("dimensions") or {}
    right_dims = right.get("dimensions") or {}
    common = sorted(set(left_dims) & set(right_dims))
    if len(common) < minimum_common:
        return None
    changed = sum(left_dims[key] != right_dims[key] for key in common)
    return changed / len(common)
