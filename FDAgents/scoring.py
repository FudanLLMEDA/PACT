"""Pure FPL'26 contest-score mechanics.

These helpers calculate score thresholds from measured Fmax, global wall time,
and tracked LLM cost. They do not estimate action gains or choose a strategy.
"""

from __future__ import annotations

import math


_QUANTILES = ("p10", "p50", "p90")
LEADERBOARD_TARGET_MULTIPLIER = 1.05
LEADERBOARD_REFERENCE_FACTOR = 0.8


def _finite_number(name: str, value: float, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def contest_penalty_factor(*, runtime_s: float, llm_cost_usd: float) -> float:
    """Return the multiplicative runtime/cost factor in the contest formula."""
    runtime = _finite_number("runtime_s", runtime_s, nonnegative=True)
    cost = _finite_number("llm_cost_usd", llm_cost_usd, nonnegative=True)
    return 1.0 - 0.1 * cost - 0.1 * runtime / 3600.0


def contest_score(
    *,
    delta_fmax_mhz: float,
    runtime_s: float,
    llm_cost_usd: float,
    validation_passed: bool = True,
) -> float:
    """Calculate the official nonnegative per-benchmark score."""
    gain = _finite_number("delta_fmax_mhz", delta_fmax_mhz)
    if not isinstance(validation_passed, bool):
        raise TypeError("validation_passed must be a bool")
    factor = contest_penalty_factor(
        runtime_s=runtime_s,
        llm_cost_usd=llm_cost_usd,
    )
    if not validation_passed:
        return 0.0
    raw_score = gain * factor
    return max(0.0, raw_score)


def leaderboard_target(*, baseline_fmax_mhz: float, rank1_score: float) -> dict:
    """Convert a published rank-1 score into the fixed development Fmax gate."""
    baseline = _finite_number("baseline_fmax_mhz", baseline_fmax_mhz)
    leader = _finite_number("rank1_score", rank1_score, nonnegative=True)
    target_score = leader * LEADERBOARD_TARGET_MULTIPLIER
    target_delta = target_score / LEADERBOARD_REFERENCE_FACTOR
    return {
        "version": 1,
        "metric": "fmax_mhz",
        "rank1_score": leader,
        "score_multiplier": LEADERBOARD_TARGET_MULTIPLIER,
        "target_score": target_score,
        "reference_factor": LEADERBOARD_REFERENCE_FACTOR,
        "baseline_fmax_mhz": baseline,
        "target_delta_fmax_mhz": target_delta,
        "target_fmax_mhz": baseline + target_delta,
        "derivation": "baseline_fmax_mhz + rank1_score * 1.05 / 0.8",
        "immutable_conversion": True,
        "comparison": "strictly_greater_than",
    }


def score_snapshot(
    *,
    baseline_fmax_mhz: float,
    best_fmax_mhz: float,
    runtime_s: float,
    llm_cost_usd: float,
    validation_passed: bool = True,
) -> dict:
    """Return all mechanical inputs and outputs for a score calculation."""
    baseline = _finite_number("baseline_fmax_mhz", baseline_fmax_mhz)
    best = _finite_number("best_fmax_mhz", best_fmax_mhz)
    runtime = _finite_number("runtime_s", runtime_s, nonnegative=True)
    cost = _finite_number("llm_cost_usd", llm_cost_usd, nonnegative=True)
    if not isinstance(validation_passed, bool):
        raise TypeError("validation_passed must be a bool")
    gain = best - baseline
    factor = contest_penalty_factor(runtime_s=runtime, llm_cost_usd=cost)
    raw_score = gain * factor
    return {
        "baseline_fmax_mhz": baseline,
        "best_fmax_mhz": best,
        "delta_fmax_mhz": gain,
        "elapsed_runtime_s": runtime,
        "llm_cost_usd": cost,
        "penalty_factor": factor,
        "raw_score": raw_score,
        "score": contest_score(
            delta_fmax_mhz=gain,
            runtime_s=runtime,
            llm_cost_usd=cost,
            validation_passed=validation_passed,
        ),
        "validation_passed": validation_passed,
    }


def break_even_action_gain_mhz(
    *,
    baseline_fmax_mhz: float,
    best_fmax_mhz: float,
    elapsed_runtime_s: float,
    llm_cost_usd: float,
    additional_runtime_s: float,
    additional_llm_cost_usd: float,
) -> dict:
    """Calculate the additional Fmax needed to tie the stop-now score.

    ``additional_runtime_s`` is total additional wall time through stopping,
    not merely skill execution time. The result is a hurdle, not a prediction.
    """
    baseline = _finite_number("baseline_fmax_mhz", baseline_fmax_mhz)
    best = _finite_number("best_fmax_mhz", best_fmax_mhz)
    elapsed = _finite_number(
        "elapsed_runtime_s", elapsed_runtime_s, nonnegative=True
    )
    cost = _finite_number("llm_cost_usd", llm_cost_usd, nonnegative=True)
    extra_runtime = _finite_number(
        "additional_runtime_s", additional_runtime_s, nonnegative=True
    )
    extra_cost = _finite_number(
        "additional_llm_cost_usd", additional_llm_cost_usd, nonnegative=True
    )

    current_gain = best - baseline
    stop_score = contest_score(
        delta_fmax_mhz=current_gain,
        runtime_s=elapsed,
        llm_cost_usd=cost,
    )
    future_runtime = elapsed + extra_runtime
    future_cost = cost + extra_cost
    future_factor = contest_penalty_factor(
        runtime_s=future_runtime,
        llm_cost_usd=future_cost,
    )

    if stop_score == 0.0:
        required_total_gain = current_gain
        required_additional_gain = 0.0
        tie_possible = True
    elif future_factor <= 0.0:
        required_total_gain = None
        required_additional_gain = None
        tie_possible = False
    else:
        required_total_gain = stop_score / future_factor
        required_additional_gain = max(0.0, required_total_gain - current_gain)
        tie_possible = True

    return {
        "stop_now_score": stop_score,
        "current_total_gain_mhz": current_gain,
        "additional_runtime_s": extra_runtime,
        "additional_llm_cost_usd": extra_cost,
        "projected_runtime_s": future_runtime,
        "projected_llm_cost_usd": future_cost,
        "projected_penalty_factor": future_factor,
        "tie_possible": tie_possible,
        "positive_score_possible": future_factor > 0.0,
        "break_even_total_gain_mhz": required_total_gain,
        "break_even_additional_gain_mhz": required_additional_gain,
        "break_even_best_fmax_mhz": (
            baseline + required_total_gain
            if required_total_gain is not None
            else None
        ),
    }


def normalize_score_estimate(value: dict) -> dict:
    """Validate one structured LLM-owned gain/runtime estimate."""
    if not isinstance(value, dict):
        raise TypeError("score_estimate must be an object")
    legacy = {"gain_vs_global_best_mhz", "action_cycle_runtime_s", "basis"}
    version2 = legacy | {"version", "action_risk"}
    keys = frozenset(value)
    if keys not in {frozenset(legacy), frozenset(version2)}:
        raise ValueError(
            f"score_estimate keys must be {sorted(version2)} "
            f"(or legacy {sorted(legacy)})"
        )
    version = 1 if keys == frozenset(legacy) else value.get("version")
    if version not in {1, 2}:
        raise ValueError("score_estimate.version must be 2")
    normalized = {"version": int(version)}
    # A reversible trial can regress, so a negative gain quantile is an honest
    # estimate and must stay expressible. Runtime remains nonnegative.
    for field, nonnegative in (
        ("gain_vs_global_best_mhz", False),
        ("action_cycle_runtime_s", True),
    ):
        quantiles = value.get(field)
        if not isinstance(quantiles, dict) or set(quantiles) != set(_QUANTILES):
            raise ValueError(f"{field} must contain exactly p10, p50, p90")
        values = [
            _finite_number(f"{field}.{key}", quantiles[key], nonnegative=nonnegative)
            for key in _QUANTILES
        ]
        if values != sorted(values):
            raise ValueError(f"{field} quantiles must be ordered")
        normalized[field] = dict(zip(_QUANTILES, values))
    basis = value.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("score_estimate.basis must be nonempty")
    normalized["basis"] = basis.strip()[:500]
    if version == 2:
        risk = value.get("action_risk")
        expected_risks = {
            "candidate_regression_probability",
            "no_legal_candidate_probability",
            "timeout_probability",
        }
        if not isinstance(risk, dict) or set(risk) != expected_risks:
            raise ValueError(
                f"action_risk must contain exactly {sorted(expected_risks)}"
            )
        normalized_risk = {}
        for field in sorted(expected_risks):
            if isinstance(risk[field], bool):
                raise TypeError(f"action_risk.{field} must be a probability")
            probability = _finite_number(f"action_risk.{field}", risk[field])
            if not 0.0 <= probability <= 1.0:
                raise ValueError(f"action_risk.{field} must be in [0, 1]")
            normalized_risk[field] = probability
        normalized["action_risk"] = normalized_risk
    else:
        normalized["action_risk"] = {"status": "legacy_unavailable"}
    return normalized


def project_action_score_envelope(
    *,
    baseline_fmax_mhz: float,
    best_fmax_mhz: float,
    elapsed_runtime_s: float,
    llm_cost_usd: float,
    score_estimate: dict,
    finalization_reserve_s: float,
    execution_window_remaining_s: float,
) -> dict:
    """Mechanically project official score scenarios from an LLM estimate."""
    estimate = normalize_score_estimate(score_estimate)
    baseline = _finite_number("baseline_fmax_mhz", baseline_fmax_mhz)
    best = _finite_number("best_fmax_mhz", best_fmax_mhz)
    elapsed = _finite_number("elapsed_runtime_s", elapsed_runtime_s, nonnegative=True)
    cost = _finite_number("llm_cost_usd", llm_cost_usd, nonnegative=True)
    reserve = _finite_number(
        "finalization_reserve_s", finalization_reserve_s, nonnegative=True
    )
    window = _finite_number(
        "execution_window_remaining_s", execution_window_remaining_s,
        nonnegative=True,
    )
    current_gain = best - baseline
    stop_score = contest_score(
        delta_fmax_mhz=current_gain, runtime_s=elapsed, llm_cost_usd=cost
    )
    stop_reserved = contest_score(
        delta_fmax_mhz=current_gain,
        runtime_s=elapsed + reserve,
        llm_cost_usd=cost,
    )
    scenarios = {
        "pessimistic": ("p10", "p90"),
        "central": ("p50", "p50"),
        "optimistic": ("p90", "p10"),
    }
    projected = {}
    for name, (gain_key, runtime_key) in scenarios.items():
        gain = estimate["gain_vs_global_best_mhz"][gain_key]
        runtime = estimate["action_cycle_runtime_s"][runtime_key]
        score = contest_score(
            delta_fmax_mhz=current_gain + gain,
            runtime_s=elapsed + runtime,
            llm_cost_usd=cost,
        )
        reserved_score = contest_score(
            delta_fmax_mhz=current_gain + gain,
            runtime_s=elapsed + runtime + reserve,
            llm_cost_usd=cost,
        )
        hurdle = break_even_action_gain_mhz(
            baseline_fmax_mhz=baseline,
            best_fmax_mhz=best,
            elapsed_runtime_s=elapsed,
            llm_cost_usd=cost,
            additional_runtime_s=runtime,
            additional_llm_cost_usd=0.0,
        )
        required = hurdle["break_even_additional_gain_mhz"]
        projected[name] = {
            "gain_quantile": gain_key,
            "runtime_quantile": runtime_key,
            "additional_gain_mhz": gain,
            "additional_runtime_s": runtime,
            "projected_score": score,
            "score_delta_vs_stop_now": score - stop_score,
            "break_even_additional_gain_mhz": required,
            "gain_margin_over_break_even_mhz": (
                gain - required if required is not None else None
            ),
            "fits_execution_window": runtime <= window,
            "reserve_charged_projected_score": reserved_score,
            "reserve_charged_score_delta": reserved_score - stop_reserved,
        }
    return {
        "version": 1,
        "kind": "mechanical_projection_of_llm_estimate",
        "informational_only": True,
        "conditional_on_final_validation": True,
        "score_if_final_validation_fails": 0.0,
        "stop_now_score": stop_score,
        "finalization_reserve_s": reserve,
        "execution_window_remaining_s": window,
        "scenarios": projected,
        "llm_action_risk": estimate["action_risk"],
        "action_risk_is_informational_only": True,
    }
