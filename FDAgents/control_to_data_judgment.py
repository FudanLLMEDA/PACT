"""LLM nomination check for the registered control-to-data rewrite.

This stage consumes anonymous state-equation evidence.  It does not extract a
design, emit a mutation, or replace the action's ordinary proof and validation
gates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .llm import LLMClient
from .skills.operator_rewrite import operator_rewrite_catalog


ACTION_TARGET = "exact_control_to_data_state_equation_v1"
DECISIONS = frozenset({"nominate", "refuse", "insufficient_evidence"})
OBLIGATIONS = (
    "exact_boolean_equations",
    "shared_control_input_identity",
    "synchronous_priority_equivalence",
    "clock_and_initial_state_equivalence",
    "complete_side_consumer_closure",
)
_EVIDENCE_FIELDS = {
    "source_primitive",
    "candidate_primitive",
    "source_equations",
    "candidate_equations",
    "claimed_obligations",
}

SYSTEM_PROMPT = """You are reviewing one registered FPGA operator rewrite.
Use only the anonymous state-equation evidence supplied by the caller. Decide
whether the action should be nominated for its normal fresh recovery, proof,
mutation, and validation pipeline. A nomination is not proof and does not
authorize mutation. Refuse when the displayed equations contradict equivalence;
use insufficient_evidence when a required obligation is absent or unsupported.
Return one JSON object only with: decision, reason_codes, missing_obligations,
and concise_reasoning. decision must be nominate, refuse, or
insufficient_evidence. missing_obligations may contain only obligation names
listed in the request."""


def _validated_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _EVIDENCE_FIELDS:
        raise ValueError("control-to-data evidence has an unsupported schema")
    source_primitive = value.get("source_primitive")
    candidate_primitive = value.get("candidate_primitive")
    if source_primitive not in {"FDSE", "FDRE"} or candidate_primitive not in {
        "FDSE",
        "FDRE",
    }:
        raise ValueError("state primitives must be anonymous FDSE/FDRE types")
    equations: dict[str, dict[str, str]] = {}
    for field in ("source_equations", "candidate_equations"):
        raw = value.get(field)
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError(f"{field} must be a nonempty equation object")
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(expression, str)
            or not expression.strip()
            or len(expression) > 256
            for key, expression in raw.items()
        ):
            raise ValueError(f"{field} contains an invalid equation")
        equations[field] = {
            str(key): str(expression).strip()
            for key, expression in sorted(raw.items())
        }
    claimed = value.get("claimed_obligations")
    if (
        not isinstance(claimed, Mapping)
        or set(claimed) != set(OBLIGATIONS)
        or any(type(item) is not bool for item in claimed.values())
    ):
        raise ValueError("claimed_obligations must cover the registered gate")
    return {
        "source_primitive": source_primitive,
        "candidate_primitive": candidate_primitive,
        **equations,
        "claimed_obligations": {
            name: bool(claimed[name]) for name in OBLIGATIONS
        },
    }


def build_judgment_request(evidence: Mapping[str, Any]) -> str:
    """Build the anonymous request and bind it to the normal action registry."""
    normalized = _validated_evidence(evidence)
    spec = operator_rewrite_catalog().get(ACTION_TARGET)
    if not isinstance(spec, dict):
        raise ValueError("control-to-data action is not normally registered")
    return json.dumps(
        {
            "task": "nominate_registered_operator_action",
            "registered_action": {"target": ACTION_TARGET, **spec},
            "anonymous_current_run_evidence": normalized,
            "required_obligations": list(OBLIGATIONS),
            "instruction": (
                "Judge nomination only. Do not invent object names, Tcl, physical "
                "coordinates, parameters, or historical action order. A nomination "
                "does not imply that an execution backend is currently ready."
            ),
        },
        indent=1,
        sort_keys=True,
    )


def _validated_judgment(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("LLM did not return a judgment object")
    decision = value.get("decision")
    reasons = value.get("reason_codes")
    missing = value.get("missing_obligations")
    reasoning = value.get("concise_reasoning")
    if decision not in DECISIONS:
        raise ValueError("LLM returned an unsupported judgment")
    if (
        not isinstance(reasons, list)
        or not reasons
        or any(not isinstance(item, str) or not item for item in reasons)
    ):
        raise ValueError("LLM judgment has invalid reason codes")
    if (
        not isinstance(missing, list)
        or any(item not in OBLIGATIONS for item in missing)
        or len(set(missing)) != len(missing)
    ):
        raise ValueError("LLM judgment has invalid missing obligations")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("LLM judgment has no concise reasoning")
    return {
        "action_target": ACTION_TARGET,
        "decision": decision,
        "reason_codes": list(reasons),
        "missing_obligations": list(missing),
        "concise_reasoning": reasoning.strip(),
        "nomination_is_proof": False,
        "mutation_authorized": False,
    }


def judge_with_llm(llm: LLMClient, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the normal Agent LLM whether fresh evidence merits nomination."""
    result = llm.stage_call(
        "control_to_data_judgment",
        SYSTEM_PROMPT,
        build_judgment_request(evidence),
        effort="low",
        max_output_tokens=1000,
    )
    return _validated_judgment(result.get("parsed"))
