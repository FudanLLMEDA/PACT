"""Observational saturation evidence derived from authoritative candidates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def resolve_strategy_family(
    skill: str,
    family_by_skill: Mapping[str, str] | None = None,
) -> str:
    """Resolve an explicit family, defaulting deterministically to the skill."""
    canonical_skill = str(skill or "unknown")
    configured = (family_by_skill or {}).get(canonical_skill)
    return str(configured) if configured else canonical_skill


def _finite_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def level1_saturation_state(
    history: Sequence[Mapping[str, object]],
    nodes: Mapping[str, Mapping[str, object]],
    *,
    platform_id: str,
    low_gain_max_delta_mhz: float,
    minimum_consecutive: int,
    family_by_skill: Mapping[str, str] | None = None,
    max_scopes: int = 12,
) -> dict:
    """Summarize measured evidence without selecting, vetoing, or stopping."""
    if isinstance(low_gain_max_delta_mhz, bool):
        raise TypeError("low_gain_max_delta_mhz must be a number")
    if isinstance(minimum_consecutive, bool):
        raise TypeError("minimum_consecutive must be an integer")
    if isinstance(max_scopes, bool):
        raise TypeError("max_scopes must be an integer")
    threshold = float(low_gain_max_delta_mhz)
    minimum = int(minimum_consecutive)
    scopes_limit = int(max_scopes)
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("low_gain_max_delta_mhz must be finite and nonnegative")
    if minimum < 1:
        raise ValueError("minimum_consecutive must be at least 1")
    if scopes_limit < 1:
        raise ValueError("max_scopes must be at least 1")

    scopes: dict[tuple[str, str], dict] = {}
    for index, row in enumerate(history):
        seed_id = row.get("seed_candidate_id")
        if seed_id is None:
            continue
        family = str(
            row.get("strategy_family")
            or resolve_strategy_family(str(row.get("skill") or "unknown"), family_by_skill)
        )
        key = (family, str(seed_id))
        scope = scopes.setdefault(key, {
            "strategy_family": family,
            "seed_candidate_id": str(seed_id),
            "measured_outcomes": 0,
            "unmeasured_executed_attempts": 0,
            "consecutive_measured_no_promotion": 0,
            "consecutive_measured_low_gain": 0,
            "consecutive_measured_low_gain_no_promotion": 0,
            "recent_seed_delta_fmax_mhz": [],
            "recent_action_runtime_s": [],
            "recent_validation_runtime_s": [],
            "last_history_index": index,
        })
        scope["last_history_index"] = index

        child_id = row.get("produced_candidate_id")
        child = nodes.get(str(child_id)) if child_id is not None else None
        parent = nodes.get(str(seed_id))
        qualifies = bool(
            row.get("action_executed")
            and row.get("candidate_created")
            and row.get("candidate_legal")
            and child is not None
            and parent is not None
            and child.get("measurement_complete")
            and child.get("eligible_for_beam")
        )
        measured_fmax = _finite_float(row.get("measured_fmax_mhz"))
        if measured_fmax is None and child is not None:
            measured_fmax = _finite_float(child.get("fmax_mhz"))
        seed_fmax = _finite_float(row.get("seed_fmax_mhz"))
        if seed_fmax is None and parent is not None:
            seed_fmax = _finite_float(parent.get("fmax_mhz"))
        qualifies = bool(qualifies and measured_fmax is not None and seed_fmax is not None)
        if not qualifies:
            if row.get("action_executed"):
                scope["unmeasured_executed_attempts"] += 1
            continue

        delta = measured_fmax - seed_fmax
        promoted = bool(row.get("accepted"))
        low_gain = delta <= threshold
        scope["measured_outcomes"] += 1
        scope["consecutive_measured_no_promotion"] = (
            0 if promoted else scope["consecutive_measured_no_promotion"] + 1
        )
        scope["consecutive_measured_low_gain"] = (
            scope["consecutive_measured_low_gain"] + 1 if low_gain else 0
        )
        scope["consecutive_measured_low_gain_no_promotion"] = (
            scope["consecutive_measured_low_gain_no_promotion"] + 1
            if low_gain and not promoted
            else 0
        )
        scope["last_seed_delta_fmax_mhz"] = delta
        global_delta = _finite_float(row.get("delta_vs_global_best_mhz"))
        if global_delta is not None:
            scope["last_delta_vs_global_best_mhz"] = global_delta
        scope["recent_seed_delta_fmax_mhz"] = (
            scope["recent_seed_delta_fmax_mhz"] + [delta]
        )[-4:]
        for row_key, scope_key in (
            ("action_runtime_s", "recent_action_runtime_s"),
            ("validation_runtime_s", "recent_validation_runtime_s"),
        ):
            runtime = _finite_float(row.get(row_key))
            if runtime is not None:
                scope[scope_key] = (scope[scope_key] + [runtime])[-4:]

    ordered = sorted(
        scopes.values(),
        key=lambda scope: (
            -int(scope["last_history_index"]),
            scope["strategy_family"],
            scope["seed_candidate_id"],
        ),
    )
    for scope in ordered:
        scope["level1_evidence_met"] = bool(
            scope["consecutive_measured_low_gain_no_promotion"] >= minimum
        )
        scope.pop("last_history_index", None)
    visible = ordered[:scopes_limit]

    return {
        "level": 1,
        "platform_id": str(platform_id),
        "informational_only": True,
        "hard_veto": False,
        "decision_owner": "llm",
        "scope_key": ["strategy_family", "seed_candidate_id"],
        "qualification": "centrally measured legal candidate outcomes only",
        "consecutive_definition": (
            "consecutive within each scope's measured outcome stream; other "
            "strategy families or seeds do not reset it"
        ),
        "thresholds": {
            "low_gain_max_delta_mhz": threshold,
            "minimum_consecutive": minimum,
        },
        "strategy_family_by_skill": dict(family_by_skill or {}),
        "default_strategy_family_rule": "canonical skill name",
        "scopes": visible,
        "scopes_omitted": max(0, len(ordered) - len(visible)),
    }
