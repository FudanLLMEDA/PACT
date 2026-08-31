"""
Base types and shared utility functions for FDAgents skills.

parse_timing_summary_static and parse_route_status_static are copied verbatim
from dcp_optimizer.py (lines 51 and 142).
"""

import re
import time
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..artifacts import sha256_file


class SkillContractError(ValueError):
    """Raised when a skill returns artifacts outside the bounded contract."""


@dataclass(frozen=True, slots=True)
class SkillOutput:
    """One skill-produced DCP awaiting canonical central measurement."""

    output_dcp: Path
    label: str = ""
    evidence: Optional[dict[str, Any]] = None
    proof: Optional[dict[str, Any]] = None


_MAX_EVIDENCE_BYTES = 32768
_MAX_PROOF_BYTES = 65536
REGISTERED_ARITHMETIC_PROOF_MAX_BYTES = 524288
_FORBIDDEN_EVIDENCE_KEYS = {
    "command", "tcl", "script", "raw_tcl", "secret", "token", "password",
    "api_key", "dcp_path", "file_path", "input_path", "output_path",
}


def _validate_bounded_json(value: Any, *, field: str, max_bytes: int) -> dict:
    if not isinstance(value, dict):
        raise SkillContractError(f"candidate {field} must be an object")

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 16:
            raise SkillContractError(f"candidate {field} exceeds maximum nesting")
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise SkillContractError(f"candidate {field} keys must be strings")
                normalized = key.lower()
                if (
                    normalized in _FORBIDDEN_EVIDENCE_KEYS
                    or normalized.endswith("_secret")
                    or normalized.endswith("_token")
                    or normalized.endswith("_password")
                    or normalized.endswith("_path")
                ):
                    raise SkillContractError(
                        f"candidate {field} contains forbidden field {key!r}"
                    )
                walk(item, depth + 1)
        elif isinstance(node, (list, tuple)):
            if len(node) > 512:
                raise SkillContractError(f"candidate {field} list exceeds bound")
            for item in node:
                walk(item, depth + 1)
        elif node is None or isinstance(node, (bool, int, str)):
            return
        elif isinstance(node, float) and math.isfinite(node):
            return
        else:
            raise SkillContractError(f"candidate {field} is not JSON-safe")

    walk(value)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise SkillContractError(f"candidate {field} is not JSON-safe") from exc
    if len(encoded) > max_bytes:
        raise SkillContractError(
            f"candidate {field} exceeds {max_bytes} encoded bytes"
        )
    return json.loads(encoded)


@dataclass
class SkillResult:
    success: bool
    before_wns: float
    after_wns: float
    delta_wns: float        # after - before, positive = improvement
    is_legal: bool          # route_status shows no routing errors
    output_dcp: Path
    summary: str            # compact summary for LLM (<200 chars)
    error_msg: str = ""
    details: Optional[dict[str, Any] | str] = None
    details_truncated: bool = False
    candidates: tuple[SkillOutput, ...] = ()

    def candidate_outputs(self) -> tuple[SkillOutput, ...]:
        """Normalize repository-native singleton and explicit bounded outputs."""
        if self.candidates:
            return self.candidates
        if self.success and self.is_legal and self.output_dcp:
            return (SkillOutput(Path(self.output_dcp), self.summary),)
        return ()

    @classmethod
    def failure(
        cls,
        before_wns: float,
        error_msg: str,
        output_dcp: Path,
        *,
        details: Optional[dict[str, Any] | str] = None,
    ) -> "SkillResult":
        return cls(
            success=False,
            before_wns=before_wns,
            after_wns=before_wns,
            delta_wns=0.0,
            is_legal=False,
            output_dcp=output_dcp,
            summary=f"FAILED: {error_msg[:150]}",
            error_msg=error_msg,
            details=details,
        )


def unexpected_execution_params(
    params: Mapping[str, Any] | dict,
    accepted: Iterable[str],
) -> list[str]:
    """Return the keys a skill accepts no parameter authority for.

    A skill with a fixed parameter set refuses unknown keys so the decision
    layer cannot smuggle parameters past the Action Card.  That guarantee is
    about keys the decision layer can produce.

    A leading underscore marks a key the framework injects on its own behalf:
    execution deadlines, commit reserves, run identity, measured design scale.
    Those never come from a menu selection, so a skill that does not read one
    should ignore it rather than refuse the action.  Enumerating them per
    skill instead made every new piece of framework context an outage across
    every skill that had not been updated to expect it -- adding a measured
    design scale failed boom_soc's first action with `phys_opt preset
    parameters are fixed; rejected ['_design_lut_count']`.
    """
    allowed = set(accepted)
    return sorted(
        str(key) for key in params
        if not str(key).startswith("_") and str(key) not in allowed
    )


def candidate_budget_exhausted(
    params: dict,
    *,
    completed_attempts: int,
) -> bool:
    """Stop before another candidate while preserving a completed prefix."""
    output_cap = params.get("_candidate_output_cap")
    if output_cap is not None:
        if isinstance(output_cap, bool) or not isinstance(output_cap, int):
            raise TypeError("_candidate_output_cap must be an integer")
        if output_cap < 1:
            raise ValueError("_candidate_output_cap must be at least 1")
        if completed_attempts >= output_cap:
            return True
    if completed_attempts < 1:
        return False
    deadline = params.get("_execution_deadline_monotonic")
    if deadline is None:
        return False
    reserve = max(
        float(params.get("_candidate_commit_reserve_s", 0.0)),
        float(params.get("min_candidate_remaining_s", 0.0)),
    )
    candidate_runtime = float(params.get("_candidate_runtime_estimate_s", 0.0))
    if candidate_runtime > 0.0:
        reserve = max(
            reserve,
            candidate_runtime
            + float(params.get("_candidate_commit_reserve_s", 0.0)),
        )
    return float(deadline) - time.monotonic() <= reserve


def continuation_opportunity_would_be_lost(
    params: dict,
    *,
    best_delta_wns: float,
    observed_candidate_runtime_s: float,
    remaining_candidates: int,
) -> dict[str, float | int | str] | None:
    """Stop a sibling sweep only when it would consume a measured continuation.

    A first positive sibling is not by itself a reason to stop.  The stop is
    authorized only when current-run wall-clock evidence shows that attempting
    another sibling would leave too little time to commit/re-profile the legal
    winner and launch the strongest remaining same-seed mechanism.  All inputs
    are scheduler facts from this run; no benchmark identity or historical
    recipe outcome is accepted here.
    """
    if best_delta_wns <= 0.0 or remaining_candidates <= 0:
        return None
    deadline = params.get("_execution_deadline_monotonic")
    if deadline is None:
        return None
    values = {
        "observed_candidate_runtime_s": observed_candidate_runtime_s,
        "candidate_commit_reserve_s": params.get(
            "_candidate_commit_reserve_s", 0.0
        ),
        "continuation_profile_reserve_s": params.get(
            "_continuation_profile_reserve_s", 0.0
        ),
        "continuation_action_reserve_s": params.get(
            "_continuation_action_reserve_s", 0.0
        ),
    }
    normalized: dict[str, float] = {}
    for key, raw in values.items():
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or float(raw) < 0.0
        ):
            raise ValueError(f"{key} must be a finite non-negative number")
        normalized[key] = float(raw)
    if normalized["continuation_action_reserve_s"] <= 0.0:
        return None
    next_sibling_cost = max(
        normalized["observed_candidate_runtime_s"],
        float(params.get("_candidate_runtime_estimate_s", 0.0) or 0.0),
    )
    continuation_reserve = (
        normalized["candidate_commit_reserve_s"]
        + normalized["continuation_profile_reserve_s"]
        + normalized["continuation_action_reserve_s"]
    )
    available = max(0.0, float(deadline) - time.monotonic())
    if available > next_sibling_cost + continuation_reserve:
        return None
    return {
        "reason": "next_sibling_would_consume_measured_continuation",
        "best_delta_wns": float(best_delta_wns),
        "remaining_candidates": int(remaining_candidates),
        "available_s": round(available, 6),
        "next_sibling_cost_s": round(next_sibling_cost, 6),
        "continuation_reserve_s": round(continuation_reserve, 6),
        **{key: round(value, 6) for key, value in normalized.items()},
    }


def validate_skill_outputs(
    result: SkillResult,
    *,
    action_dir: Path,
    candidate_cap: int,
    proof_max_bytes: int = _MAX_PROOF_BYTES,
) -> tuple[SkillOutput, ...]:
    """Validate exact output type, bound, uniqueness, and scratch ownership."""
    if not isinstance(result, SkillResult):
        raise SkillContractError(
            f"skill returned {type(result).__name__}, expected SkillResult"
        )
    if isinstance(candidate_cap, bool) or not isinstance(candidate_cap, int):
        raise TypeError("candidate_cap must be an integer")
    if candidate_cap < 1:
        raise ValueError("candidate_cap must be at least 1")
    if (
        isinstance(proof_max_bytes, bool)
        or not isinstance(proof_max_bytes, int)
        or not 1 <= proof_max_bytes <= REGISTERED_ARITHMETIC_PROOF_MAX_BYTES
    ):
        raise ValueError("proof_max_bytes is outside the bounded proof envelope")
    if not isinstance(result.candidates, tuple):
        raise SkillContractError("SkillResult.candidates must be a tuple")
    if not result.success and result.candidates:
        raise SkillContractError("failed SkillResult cannot publish candidates")
    if not result.is_legal and result.candidates:
        raise SkillContractError("skill-locally illegal SkillResult cannot publish candidates")

    outputs = result.candidate_outputs()
    if len(outputs) > candidate_cap:
        raise SkillContractError(
            f"skill returned {len(outputs)} candidates; configured cap is {candidate_cap}"
        )
    action_root = Path(action_dir).resolve()
    seen: set[Path] = set()
    validated = []
    for ordinal, output in enumerate(outputs):
        if not isinstance(output, SkillOutput):
            raise SkillContractError(f"candidate {ordinal} is not a SkillOutput")
        if not isinstance(output.output_dcp, Path):
            raise SkillContractError(f"candidate {ordinal} output_dcp must be a Path")
        if not isinstance(output.label, str) or len(output.label) > 120:
            raise SkillContractError(
                f"candidate {ordinal} label must be a string of at most 120 characters"
            )
        source = output.output_dcp
        if source.is_symlink() or not source.is_file():
            raise SkillContractError(
                f"candidate {ordinal} is missing or not a regular file, or is symlinked"
            )
        resolved = source.resolve()
        try:
            resolved.relative_to(action_root)
        except ValueError as exc:
            raise SkillContractError(
                f"candidate {ordinal} is outside action scratch"
            ) from exc
        if resolved in seen:
            raise SkillContractError(
                f"candidate {ordinal} duplicates an earlier output path"
            )
        seen.add(resolved)
        evidence = (
            _validate_bounded_json(
                output.evidence, field="evidence", max_bytes=_MAX_EVIDENCE_BYTES
            )
            if output.evidence is not None else None
        )
        if isinstance(evidence, dict) and "candidate_dcp_sha256" in evidence:
            expected_sha256 = evidence["candidate_dcp_sha256"]
            if (
                not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or sha256_file(resolved) != expected_sha256
            ):
                raise SkillContractError(
                    f"candidate {ordinal} evidence is not bound to its DCP bytes"
                )
        proof = (
            _validate_bounded_json(
                output.proof, field="proof", max_bytes=proof_max_bytes
            )
            if output.proof is not None else None
        )
        validated.append(SkillOutput(resolved, output.label, evidence, proof))
    return tuple(validated)


def parse_timing_summary_static(timing_report: str) -> dict:
    """
    Parse timing summary report to extract WNS, TNS, and failing endpoints.

    Copied from dcp_optimizer.py:51.
    """
    result = {
        "wns": None,
        "tns": None,
        "failing_endpoints": None,
        "total_endpoints": None,
        "whs": None,
        "ths": None,
        "ths_failing_endpoints": None,
        "ths_total_endpoints": None,
        "wpws": None,
        "tpws": None,
        "tpws_failing_endpoints": None,
        "tpws_total_endpoints": None,
        "min_period_required_ns": None,
        "min_period_actual_ns": None,
        "min_period_slack_ns": None,
    }

    lines = timing_report.split("\n")

    def attach_min_period_checks() -> None:
        checks = []
        for item in lines:
            if not item.strip().startswith("Min Period"):
                continue
            nums = re.findall(r"[-+]?\d+\.\d+", item)
            if len(nums) < 3:
                continue
            required, actual, slack = (float(nums[0]), float(nums[1]), float(nums[2]))
            checks.append((required, actual, slack))
        if not checks:
            return
        required, actual, slack = max(checks, key=lambda row: row[0])
        result["min_period_required_ns"] = required
        result["min_period_actual_ns"] = actual
        result["min_period_slack_ns"] = slack

    header_idx = -1
    for i, line in enumerate(lines):
        if "WNS(ns)" in line and "TNS(ns)" in line:
            header_idx = i
            header_line = line
            break

    if header_idx == -1:
        return result
    has_hold = "WHS(ns)" in header_line and "THS(ns)" in header_line
    has_pulse = "WPWS(ns)" in header_line and "TPWS(ns)" in header_line

    data_idx = header_idx + 2
    if data_idx >= len(lines):
        return result

    data_line = lines[data_idx].strip()
    if not data_line:
        return result

    parts = data_line.split()
    if not parts:
        return result

    def parse_float(token: str):
        if token in {"NA", "N/A"}:
            return None
        return float(token)

    def parse_int(token: str):
        if token in {"NA", "N/A"}:
            return None
        return int(token)

    try:
        if len(parts) >= 1:
            result["wns"] = parse_float(parts[0])
        if len(parts) >= 2:
            result["tns"] = parse_float(parts[1])
        if len(parts) >= 3:
            result["failing_endpoints"] = parse_int(parts[2])
        if len(parts) >= 4:
            result["total_endpoints"] = parse_int(parts[3])
        offset = 4
        if has_hold:
            if len(parts) >= offset + 1:
                result["whs"] = parse_float(parts[offset])
            if len(parts) >= offset + 2:
                result["ths"] = parse_float(parts[offset + 1])
            if len(parts) >= offset + 3:
                result["ths_failing_endpoints"] = parse_int(parts[offset + 2])
            if len(parts) >= offset + 4:
                result["ths_total_endpoints"] = parse_int(parts[offset + 3])
            offset += 4
        if has_pulse:
            if len(parts) >= offset + 1:
                result["wpws"] = parse_float(parts[offset])
            if len(parts) >= offset + 2:
                result["tpws"] = parse_float(parts[offset + 1])
            if len(parts) >= offset + 3:
                result["tpws_failing_endpoints"] = parse_int(parts[offset + 2])
            if len(parts) >= offset + 4:
                result["tpws_total_endpoints"] = parse_int(parts[offset + 3])
    except (ValueError, IndexError):
        pass

    attach_min_period_checks()
    return result


def parse_route_status_static(route_report: str) -> dict:
    """
    Parse Vivado report_route_status output.

    Copied from dcp_optimizer.py:142.
    """
    result = {
        "routable_nets": None,
        "fully_routed_nets": None,
        "nets_with_routing_errors": None,
        "routed_ok": None,
    }

    patterns = {
        "routable_nets": r"# of routable nets[.\s:]+(\d+)",
        "fully_routed_nets": r"# of fully routed nets[.\s:]+(\d+)",
        "nets_with_routing_errors": r"# of nets with routing errors[.\s:]+(\d+)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, route_report, re.IGNORECASE)
        if match:
            result[key] = int(match.group(1))

    if (
        result["routable_nets"] is not None
        and result["fully_routed_nets"] is not None
        and result["nets_with_routing_errors"] is not None
    ):
        result["routed_ok"] = (
            result["fully_routed_nets"] == result["routable_nets"]
            and result["nets_with_routing_errors"] == 0
        )

    return result


def calculate_fmax(wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
    """
    Calculate achievable Fmax in MHz.

    Copied from dcp_optimizer.py:363 (DCPOptimizerBase.calculate_fmax).
    """
    if clock_period is None or clock_period <= 0 or wns is None:
        return None
    achievable = clock_period - wns
    if achievable <= 0:
        return None
    return 1000.0 / achievable


async def open_rapidwright_dcp_in_vivado(
    mcp,
    dcp_path: Path,
    *,
    timeout: float = 600.0,
) -> None:
    """Open a RapidWright DCP using its Tcl sidecar when one was emitted."""
    dcp_path = Path(dcp_path)
    sidecars = (
        Path(str(dcp_path) + ".tcl"),  # RapidWright: foo.dcp.tcl
        dcp_path.with_suffix(".tcl"),  # Legacy: foo.tcl
    )
    for sidecar in sidecars:
        if sidecar.is_file():
            await mcp.call_vivado(
                "run_tcl",
                {"command": f"source {{{sidecar.resolve()}}}"},
                timeout=timeout,
            )
            return
    await mcp.call_vivado(
        "open_checkpoint",
        {"dcp_path": str(dcp_path.resolve())},
        timeout=timeout,
    )
