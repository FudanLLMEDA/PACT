"""Typed, fail-closed proof contract for one-cycle BRAM write retiming."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ACTION_CLASS = "memory_write_schedule_retime_v1"
PROOF_TEMPLATE = "BRAM_WRITE_SCHEDULE_RETIME_KINDUCTION_V1"
SCHEMA_VERSION = 1


class MemoryScheduleProofError(ValueError):
    """Raised when a memory retiming contract is incomplete or malformed."""


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(_stable(value), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RegisteredWriteBoundary:
    data_width: int
    address_width: int
    enable_width: int
    latency_delta_cycles: int
    data_registered: bool
    address_registered: bool
    enable_registered: bool
    common_clock: bool
    common_enable: bool
    reset_semantics_equal: bool


@dataclass(frozen=True)
class ExtractedFiniteSchedule:
    state_width: int
    states: tuple[int, ...]
    initial_states: tuple[int, ...]
    transitions: tuple[tuple[int, int], ...]
    write_states: tuple[int, ...]
    read_states: tuple[int, ...]
    observer_states: tuple[int, ...]
    terminal_states: tuple[int, ...]
    read_consumers_complete: bool
    memory_state_has_no_direct_observer: bool
    terminal_consumers_complete: bool
    extraction_sha256: str


@dataclass(frozen=True)
class MemoryWriteScheduleRetimeContract:
    source_dcp_sha256: str
    target_memory_fingerprint: str
    boundary: RegisteredWriteBoundary
    schedule: ExtractedFiniteSchedule
    action_class: str = ACTION_CLASS
    proof_template: str = PROOF_TEMPLATE
    hazard_policy: str = "phase_disjoint_no_read_in_write_or_successor"


def _validate(contract: MemoryWriteScheduleRetimeContract) -> None:
    if contract.action_class != ACTION_CLASS or contract.proof_template != PROOF_TEMPLATE:
        raise MemoryScheduleProofError("memory retiming action/template mismatch")
    if len(contract.source_dcp_sha256) != 64 or len(contract.schedule.extraction_sha256) != 64:
        raise MemoryScheduleProofError("memory retiming hash binding is malformed")
    boundary = contract.boundary
    if not 1 <= boundary.data_width <= 576:
        raise MemoryScheduleProofError("write data width is invalid")
    if not 1 <= boundary.address_width <= 32 or boundary.enable_width < 1:
        raise MemoryScheduleProofError("write address/enable width is invalid")
    if boundary.latency_delta_cycles != 1:
        raise MemoryScheduleProofError("only one-cycle write retiming is supported")
    schedule = contract.schedule
    if not 1 <= schedule.state_width <= 32 or not schedule.states:
        raise MemoryScheduleProofError("finite schedule state space is invalid")
    states = set(schedule.states)
    if len(states) != len(schedule.states):
        raise MemoryScheduleProofError("finite schedule states are not unique")
    if any(state < 0 or state >= 1 << schedule.state_width for state in states):
        raise MemoryScheduleProofError("finite schedule state does not fit width")
    for subset_name in (
        "initial_states", "write_states", "read_states", "observer_states",
        "terminal_states",
    ):
        if not set(getattr(schedule, subset_name)) <= states:
            raise MemoryScheduleProofError(f"{subset_name} escapes extracted state space")
    if not schedule.initial_states or not schedule.transitions:
        raise MemoryScheduleProofError("finite schedule lacks initial states or transitions")
    if any(left not in states or right not in states for left, right in schedule.transitions):
        raise MemoryScheduleProofError("finite schedule transition escapes state space")
    if any(not any(left == state for left, _ in schedule.transitions) for state in states):
        raise MemoryScheduleProofError("finite schedule has a state without a successor")


def _reachable(schedule: ExtractedFiniteSchedule) -> set[int]:
    outgoing: dict[int, set[int]] = {state: set() for state in schedule.states}
    for left, right in schedule.transitions:
        outgoing[left].add(right)
    reached = set(schedule.initial_states)
    frontier = list(schedule.initial_states)
    while frontier:
        state = frontier.pop()
        for successor in outgoing[state]:
            if successor not in reached:
                reached.add(successor)
                frontier.append(successor)
    return reached


def _transition_counterexample(
    schedule: ExtractedFiniteSchedule,
    reachable: set[int],
    *,
    current_forbidden: set[int],
    next_forbidden: set[int],
) -> dict[str, Any] | None:
    import z3

    state = z3.BitVec("memretime_state", schedule.state_width)
    next_state = z3.BitVec("memretime_next_state", schedule.state_width)
    relation = z3.Or(*(
        z3.And(state == left, next_state == right)
        for left, right in schedule.transitions
    ))
    in_reachable = z3.Or(*(state == value for value in sorted(reachable)))
    is_write = z3.Or(*(state == value for value in schedule.write_states))
    current_bad = z3.Or(*(state == value for value in sorted(current_forbidden)))
    next_bad = z3.Or(*(next_state == value for value in sorted(next_forbidden)))
    solver = z3.SolverFor("QF_BV")
    solver.add(relation, in_reachable, is_write, z3.Or(current_bad, next_bad))
    result = solver.check()
    if result == z3.unknown:
        raise MemoryScheduleProofError(
            f"schedule induction returned unknown: {solver.reason_unknown()}"
        )
    if result == z3.unsat:
        return None
    model = solver.model()
    return {
        "state": model.eval(state, model_completion=True).as_long(),
        "next_state": model.eval(next_state, model_completion=True).as_long(),
        "current_forbidden": bool(
            model.eval(current_bad, model_completion=True)
        ),
        "next_forbidden": bool(model.eval(next_bad, model_completion=True)),
    }


def prove_memory_write_schedule_retime(
    contract: MemoryWriteScheduleRetimeContract,
) -> dict[str, Any]:
    """Discharge all explicit obligations or return a typed counterexample."""

    _validate(contract)
    boundary = contract.boundary
    schedule = contract.schedule
    reachable = _reachable(schedule)
    construction = {
        "data_registered": boundary.data_registered,
        "address_registered": boundary.address_registered,
        "enable_registered": boundary.enable_registered,
        "common_clock": boundary.common_clock,
        "common_enable": boundary.common_enable,
        "reset_semantics_equal": boundary.reset_semantics_equal,
        "latency_delta_is_one": boundary.latency_delta_cycles == 1,
    }
    construction_passed = all(construction.values())
    observability = {
        "read_consumers_complete": schedule.read_consumers_complete,
        "memory_state_has_no_direct_observer": schedule.memory_state_has_no_direct_observer,
    }
    observability_passed = all(observability.values())

    hazard_counterexample = _transition_counterexample(
        schedule,
        reachable,
        current_forbidden=set(schedule.read_states),
        next_forbidden=set(schedule.read_states),
    )
    drain_counterexample = _transition_counterexample(
        schedule,
        reachable,
        current_forbidden=set(),
        next_forbidden=set(schedule.observer_states) | set(schedule.terminal_states),
    )
    obligations = [
        {
            "name": "constructive_reregistration",
            "result": "proved" if construction_passed else "failed",
            "facts": construction,
        },
        {
            "name": "one_cycle_raw_hazard_freedom",
            "method": "one-step_k_induction_over_exact_reachable_finite_schedule",
            "result": "proved" if hazard_counterexample is None else "counterexample",
            "counterexample": hazard_counterexample,
        },
        {
            "name": "read_port_observability_closure",
            "result": "proved" if observability_passed else "failed",
            "facts": observability,
        },
        {
            "name": "terminal_write_drain",
            "method": "one-step_k_induction_over_exact_reachable_finite_schedule",
            "result": (
                "proved"
                if drain_counterexample is None and schedule.terminal_consumers_complete
                else "counterexample" if drain_counterexample is not None else "failed"
            ),
            "terminal_consumers_complete": schedule.terminal_consumers_complete,
            "counterexample": drain_counterexample,
        },
    ]
    proved = all(item["result"] == "proved" for item in obligations)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "memory_write_schedule_retime_proof",
        "action_class": ACTION_CLASS,
        "proof_template": PROOF_TEMPLATE,
        "status": "proved" if proved else "rejected",
        "mutation_ready": proved,
        "source_dcp_sha256": contract.source_dcp_sha256,
        "target_memory_fingerprint": contract.target_memory_fingerprint,
        "contract": asdict(contract),
        "reachable_states": sorted(reachable),
        "obligations": obligations,
    }
    payload["proof_id"] = _digest(payload)
    return payload


def load_fixture_contract(path: Path) -> MemoryWriteScheduleRetimeContract:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    netlist = data.get("netlist") or {}
    if (
        netlist.get("memory_primitive") != "RAMB36E2"
        or netlist.get("write_boundary_primitives") != ["FDRE", "FDRE", "FDRE"]
    ):
        raise MemoryScheduleProofError("fixture netlist does not contain the typed boundary")
    raw = data["contract"]
    return MemoryWriteScheduleRetimeContract(
        source_dcp_sha256=raw["source_dcp_sha256"],
        target_memory_fingerprint=raw["target_memory_fingerprint"],
        boundary=RegisteredWriteBoundary(**raw["boundary"]),
        schedule=ExtractedFiniteSchedule(
            **{
                **raw["schedule"],
                "states": tuple(raw["schedule"]["states"]),
                "initial_states": tuple(raw["schedule"]["initial_states"]),
                "transitions": tuple(tuple(item) for item in raw["schedule"]["transitions"]),
                "write_states": tuple(raw["schedule"]["write_states"]),
                "read_states": tuple(raw["schedule"]["read_states"]),
                "observer_states": tuple(raw["schedule"]["observer_states"]),
                "terminal_states": tuple(raw["schedule"]["terminal_states"]),
            }
        ),
    )
