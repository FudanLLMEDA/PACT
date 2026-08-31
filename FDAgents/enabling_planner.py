"""Measured, deterministic qualification of composition-enabling candidates.

This module does not decide that a rewrite is good.  It only preserves a legal,
strictly validated, non-promoted checkpoint when measured residual timing shows
that the rewrite changed its declared target family without creating a new wall
below the parent's global WNS.  The resulting record is suitable for a bounded
composition search; it is never an optimization result by itself.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Iterable


PROFILE_SCHEMA = "measured_residual_profile_v1"
QUALIFICATION_SCHEMA = "enabling_candidate_qualification_v1"


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def normalize_residual_profile(raw: dict) -> dict:
    """Validate and normalize a measured top-N residual-family profile."""
    if not isinstance(raw, dict):
        raise ValueError("residual profile must be a mapping")
    if raw.get("schema") != PROFILE_SCHEMA:
        raise ValueError(f"residual profile schema must be {PROFILE_SCHEMA!r}")
    artifact_sha256 = str(raw.get("artifact_sha256") or "")
    if re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None:
        raise ValueError("residual profile artifact_sha256 must be lowercase SHA-256")
    top_n = raw.get("top_n")
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError("residual profile top_n must be a positive integer")
    families = raw.get("families")
    if not isinstance(families, dict) or not families:
        raise ValueError("residual profile families must be a nonempty mapping")
    normalized_families = {}
    for family_id, facts in sorted(families.items()):
        family_id = str(family_id).strip()
        if not family_id or not isinstance(facts, dict):
            raise ValueError("residual profile family rows must be named mappings")
        rank = facts.get("best_rank")
        count = facts.get("count")
        if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= top_n:
            raise ValueError(f"family {family_id!r} best_rank must be in [1, top_n]")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"family {family_id!r} count must be positive")
        normalized_families[family_id] = {
            "best_rank": rank,
            "count": count,
            "worst_slack_ns": _finite_number(
                facts.get("worst_slack_ns"),
                f"families.{family_id}.worst_slack_ns",
            ),
        }
    sources = raw.get("source_artifacts") or []
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(item, str) and item.strip() for item in sources)
    ):
        raise ValueError("residual profile source_artifacts must contain source paths")
    return {
        "schema": PROFILE_SCHEMA,
        "artifact_sha256": artifact_sha256,
        "global_wns_ns": _finite_number(raw.get("global_wns_ns"), "global_wns_ns"),
        "fmax_mhz": _finite_number(raw.get("fmax_mhz"), "fmax_mhz"),
        "top_n": top_n,
        "families": normalized_families,
        "source_artifacts": list(sources),
    }


def evaluate_enabling_candidate(
    *,
    parent_profile: dict,
    candidate_profile: dict,
    targeted_families: Iterable[str],
    candidate_legal: bool,
    strict_validation_passed: bool,
    promoted: bool,
    regression_tolerance_ns: float = 0.01,
    local_improvement_epsilon_ns: float = 0.0005,
) -> dict:
    """Return a machine-owned qualification record with explicit reasons."""
    parent = normalize_residual_profile(parent_profile)
    candidate = normalize_residual_profile(candidate_profile)
    tolerance = _finite_number(regression_tolerance_ns, "regression_tolerance_ns")
    epsilon = _finite_number(local_improvement_epsilon_ns, "local_improvement_epsilon_ns")
    if tolerance < 0.0 or epsilon <= 0.0:
        raise ValueError(
            "regression tolerance must be nonnegative and improvement epsilon positive"
        )
    if parent["top_n"] != candidate["top_n"]:
        raise ValueError("parent and candidate residual profiles must use the same top_n")
    if isinstance(targeted_families, (str, bytes)):
        raise ValueError("targeted_families must be an iterable of family IDs")
    targets = sorted({str(value).strip() for value in targeted_families if str(value).strip()})
    if not targets:
        raise ValueError("targeted_families must contain at least one family")

    reasons = []
    if candidate_legal is not True:
        reasons.append("candidate_not_legal")
    if strict_validation_passed is not True:
        reasons.append("strict_validation_not_passed")
    if promoted:
        reasons.append("candidate_already_promoted")

    target_effects = []
    target_changed = False
    for family_id in targets:
        before = parent["families"].get(family_id)
        after = candidate["families"].get(family_id)
        if before is None:
            target_effects.append({
                "family_id": family_id,
                "status": "absent_from_parent_top_n",
                "improvement_ns": None,
            })
            continue
        if after is None:
            target_changed = True
            target_effects.append({
                "family_id": family_id,
                "status": "left_candidate_top_n",
                "improvement_ns": None,
            })
            continue
        improvement = after["worst_slack_ns"] - before["worst_slack_ns"]
        improved = improvement >= epsilon
        target_changed = target_changed or improved
        target_effects.append({
            "family_id": family_id,
            "status": "locally_improved" if improved else "not_locally_improved",
            "improvement_ns": improvement,
            "parent_worst_slack_ns": before["worst_slack_ns"],
            "candidate_worst_slack_ns": after["worst_slack_ns"],
        })
    if not target_changed:
        reasons.append("target_family_did_not_improve_or_leave_top_n")

    new_wall_limit = parent["global_wns_ns"] - tolerance
    new_blocking_families = []
    for family_id, facts in candidate["families"].items():
        if family_id in parent["families"]:
            continue
        if facts["worst_slack_ns"] < new_wall_limit:
            new_blocking_families.append({
                "family_id": family_id,
                "worst_slack_ns": facts["worst_slack_ns"],
                "limit_ns": new_wall_limit,
            })
    if new_blocking_families:
        reasons.append("new_family_below_parent_global_wns_tolerance")

    return {
        "schema": QUALIFICATION_SCHEMA,
        "qualified": not reasons,
        "reasons": reasons,
        "targeted_families": targets,
        "target_effects": target_effects,
        "new_blocking_families": new_blocking_families,
        "regression_tolerance_ns": tolerance,
        "local_improvement_epsilon_ns": epsilon,
        "parent_profile": copy.deepcopy(parent),
        "candidate_profile": copy.deepcopy(candidate),
        "authority": "deterministic_measured_rules_no_llm_judgment",
        "optimization_result": False,
    }
