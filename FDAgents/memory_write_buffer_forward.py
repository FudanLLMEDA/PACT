"""Typed local proof contract for one-cycle BRAM write buffering with forwarding.

The proof uses a relational memory invariant rather than global schedule
reachability.  The architectural memory equals the physical BRAM after applying
the one pending write.  A read hit forwards that pending write, including byte
masks, while a miss observes the unmodified physical BRAM value.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any


ACTION_CLASS = "memory_write_buffer_forward_v1"
PROOF_TEMPLATE = "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1"
SCHEMA_VERSION = 1


class MemoryForwardProofError(ValueError):
    """Raised when a forwarding contract is malformed or unsupported."""


@dataclass(frozen=True)
class ForwardedReadPort:
    port: str
    connected_data_bits: tuple[int, ...]
    read_latency_cycles: int
    output_register: int
    consumers_complete: bool
    consumer_refs: tuple[str, ...]


@dataclass(frozen=True)
class MemoryWriteBufferForwardContract:
    source_dcp_sha256: str
    target_memory_fingerprint: str
    data_width: int
    address_width: int
    byte_enable_width: int
    write_enable_register_bits: int
    write_port: str
    write_mode: str
    read_address_collision: str
    common_clock: bool
    write_data_registered: bool
    write_address_registered: bool
    write_enable_registered: bool
    aligned_forward_data_registered: bool
    aligned_hit_registered: bool
    write_data_cones_exclusive: bool
    port_a_read_unused: bool
    read_ports: tuple[ForwardedReadPort, ...]
    action_class: str = ACTION_CLASS
    proof_template: str = PROOF_TEMPLATE


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


def _validate(contract: MemoryWriteBufferForwardContract) -> None:
    if contract.action_class != ACTION_CLASS or contract.proof_template != PROOF_TEMPLATE:
        raise MemoryForwardProofError("memory forwarding action/template mismatch")
    if len(contract.source_dcp_sha256) != 64 or len(contract.target_memory_fingerprint) != 64:
        raise MemoryForwardProofError("memory forwarding hash binding is malformed")
    if not 1 <= contract.data_width <= 576 or not 1 <= contract.address_width <= 32:
        raise MemoryForwardProofError("memory forwarding width is invalid")
    if not 1 <= contract.byte_enable_width <= contract.data_width:
        raise MemoryForwardProofError("memory forwarding byte-enable width is invalid")
    if contract.data_width % contract.byte_enable_width:
        raise MemoryForwardProofError("data width is not divisible by byte-enable width")
    if not 1 <= contract.write_enable_register_bits <= contract.byte_enable_width + 1:
        raise MemoryForwardProofError("write-control register width is invalid")
    if contract.write_port != "A":
        raise MemoryForwardProofError("only port-A buffered writes are supported")
    if contract.write_mode != "READ_FIRST":
        raise MemoryForwardProofError("write port must have deterministic READ_FIRST collision semantics")
    if contract.read_address_collision != "DELAYED_WRITE" or not contract.common_clock:
        raise MemoryForwardProofError("cross-port collision must be common-clock DELAYED_WRITE")
    if not contract.read_ports:
        raise MemoryForwardProofError("memory forwarding contract has no read port")
    for read_port in contract.read_ports:
        if read_port.port != "B":
            raise MemoryForwardProofError("only cross-port B reads are supported")
        if read_port.output_register != 0 or read_port.read_latency_cycles != 1:
            raise MemoryForwardProofError("unsupported read latency configuration")
        if not read_port.consumers_complete:
            raise MemoryForwardProofError("read-port consumer closure is incomplete")
        bits = read_port.connected_data_bits
        if not bits or len(bits) != len(set(bits)):
            raise MemoryForwardProofError("read-port data-bit map is empty or duplicated")
        if min(bits) < 0 or max(bits) >= contract.data_width:
            raise MemoryForwardProofError("read-port data-bit map escapes write data width")


def _expanded_mask(z3: Any, enables: Any, data_width: int, enable_width: int) -> Any:
    lane_width = data_width // enable_width
    result = z3.BitVecVal(0, data_width)
    lane_value = (1 << lane_width) - 1
    for lane in range(enable_width):
        lane_mask = z3.BitVecVal(lane_value << (lane * lane_width), data_width)
        result = result | z3.If(
            z3.Extract(lane, lane, enables) == 1,
            lane_mask,
            z3.BitVecVal(0, data_width),
        )
    return result


def _merge(z3: Any, old: Any, data: Any, mask: Any) -> Any:
    return (old & ~mask) | (data & mask)


def _project(z3: Any, word: Any, bits: tuple[int, ...]) -> Any:
    pieces = [z3.Extract(bit, bit, word) for bit in reversed(bits)]
    return pieces[0] if len(pieces) == 1 else z3.Concat(*pieces)


def estimate_forwarding_cost(contract: MemoryWriteBufferForwardContract) -> dict[str, int]:
    """Return a conservative primitive estimate for the declared local mechanism."""
    _validate(contract)
    forwarded_bits = sum(len(port.connected_data_bits) for port in contract.read_ports)
    comparator_luts = sum(
        math.ceil(contract.address_width / 3) + 1 for _port in contract.read_ports
    )
    write_pipeline_ffs = (
        contract.data_width
        + contract.address_width
        + contract.write_enable_register_bits
    )
    alignment_ffs = sum(
        len(port.connected_data_bits) + 1 for port in contract.read_ports
    )
    return {
        "write_pipeline_ffs": write_pipeline_ffs,
        "forward_alignment_ffs": alignment_ffs,
        "total_ffs": write_pipeline_ffs + alignment_ffs,
        "compare_luts": comparator_luts,
        "read_mux_luts": forwarded_bits,
        "total_luts": comparator_luts + forwarded_bits,
    }


def prove_memory_write_buffer_forward(
    contract: MemoryWriteBufferForwardContract,
) -> dict[str, Any]:
    """Prove the local relational invariant and forwarding read miter."""
    _validate(contract)
    import z3

    addr_sort = z3.BitVecSort(contract.address_width)
    data_sort = z3.BitVecSort(contract.data_width)
    physical = z3.Array("memfwd_physical", addr_sort, data_sort)
    pending_addr = z3.BitVec("memfwd_pending_addr", contract.address_width)
    pending_data = z3.BitVec("memfwd_pending_data", contract.data_width)
    pending_we = z3.BitVec("memfwd_pending_we", contract.byte_enable_width)
    current_addr = z3.BitVec("memfwd_current_addr", contract.address_width)
    current_data = z3.BitVec("memfwd_current_data", contract.data_width)
    current_we = z3.BitVec("memfwd_current_we", contract.byte_enable_width)
    read_addr = z3.BitVec("memfwd_read_addr", contract.address_width)
    read_enable = z3.Bool("memfwd_read_enable")

    pending_mask = _expanded_mask(
        z3, pending_we, contract.data_width, contract.byte_enable_width
    )
    current_mask = _expanded_mask(
        z3, current_we, contract.data_width, contract.byte_enable_width
    )
    pending_active = pending_we != 0
    current_active = current_we != 0

    # Relational invariant at the start of a cycle: architectural memory is
    # physical BRAM state plus the one pending buffered write.
    pending_old = z3.Select(physical, pending_addr)
    architectural = z3.If(
        pending_active,
        z3.Store(
            physical,
            pending_addr,
            _merge(z3, pending_old, pending_data, pending_mask),
        ),
        physical,
    )
    raw_read = z3.Select(physical, read_addr)
    hit = pending_active & (pending_addr == read_addr)
    forwarded_read = z3.If(
        hit,
        _merge(z3, raw_read, pending_data, pending_mask),
        raw_read,
    )
    original_read = z3.Select(architectural, read_addr)

    physical_next = architectural
    architectural_next = z3.If(
        current_active,
        z3.Store(
            architectural,
            current_addr,
            _merge(
                z3,
                z3.Select(architectural, current_addr),
                current_data,
                current_mask,
            ),
        ),
        architectural,
    )
    retimed_relation_next = z3.If(
        current_active,
        z3.Store(
            physical_next,
            current_addr,
            _merge(
                z3,
                z3.Select(physical_next, current_addr),
                current_data,
                current_mask,
            ),
        ),
        physical_next,
    )

    read_mismatches = [
        z3.And(
            read_enable,
            _project(z3, original_read, port.connected_data_bits)
            != _project(z3, forwarded_read, port.connected_data_bits),
        )
        for port in contract.read_ports
    ]
    solver = z3.Solver()
    solver.add(z3.Or(*read_mismatches, architectural_next != retimed_relation_next))
    result = solver.check()
    if result == z3.unknown:
        raise MemoryForwardProofError(f"local forwarding miter returned unknown: {solver.reason_unknown()}")
    counterexample = None
    if result == z3.sat:
        model = solver.model()
        counterexample = {
            "pending_addr": model.eval(pending_addr, model_completion=True).as_long(),
            "pending_data": model.eval(pending_data, model_completion=True).as_long(),
            "pending_we": model.eval(pending_we, model_completion=True).as_long(),
            "current_addr": model.eval(current_addr, model_completion=True).as_long(),
            "current_data": model.eval(current_data, model_completion=True).as_long(),
            "current_we": model.eval(current_we, model_completion=True).as_long(),
            "read_addr": model.eval(read_addr, model_completion=True).as_long(),
            "read_enable": bool(z3.is_true(model.eval(read_enable, model_completion=True))),
        }

    construction = {
        "write_data_registered": contract.write_data_registered,
        "write_address_registered": contract.write_address_registered,
        "write_enable_registered": contract.write_enable_registered,
        "aligned_forward_data_registered": contract.aligned_forward_data_registered,
        "aligned_hit_registered": contract.aligned_hit_registered,
    }
    closure = {
        "write_data_cones_exclusive": contract.write_data_cones_exclusive,
        "port_a_read_unused": contract.port_a_read_unused,
        "read_consumers_complete": all(port.consumers_complete for port in contract.read_ports),
    }
    local_proved = result == z3.unsat
    obligations = [
        {
            "name": "write_pipeline_reregistration_exactness",
            "result": "proved" if all(construction.values()) else "failed",
            "facts": construction,
        },
        {
            "name": "forwarding_hit_miss_enable_collision_equivalence",
            "method": "two_cycle_qf_array_bv_relational_miter",
            "result": "proved" if local_proved else "counterexample",
            "counterexample": counterexample,
        },
        {
            "name": "read_latency_alignment",
            "result": "proved",
            "facts": {
                "primitive_read_latency_cycles": 1,
                "aligned_hit_cycles": 1,
                "aligned_forward_data_cycles": 1,
            },
        },
        {
            "name": "swept_cone_and_consumer_closure",
            "result": "proved" if all(closure.values()) else "failed",
            "facts": closure,
        },
    ]
    proved = all(item["result"] == "proved" for item in obligations)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "memory_write_buffer_forward_proof",
        "action_class": ACTION_CLASS,
        "proof_template": PROOF_TEMPLATE,
        "source_dcp_sha256": contract.source_dcp_sha256,
        "target_memory_fingerprint": contract.target_memory_fingerprint,
        "contract": asdict(contract),
        "invariant": "architectural_memory == apply(pending_write, physical_bram_memory)",
        "collision_semantics": "common_clock_cross_port_read_first_old_data",
        "obligations": obligations,
        "cost_estimate": estimate_forwarding_cost(contract),
        "status": "proved" if proved else "rejected",
        "mutation_ready": proved,
    }
    payload["proof_id"] = _digest(payload)
    return payload


def evaluate_forwarded_read(
    *,
    physical_word: int,
    pending_data: int,
    pending_enable: int,
    pending_address: int,
    read_address: int,
    data_width: int,
    byte_enable_width: int,
) -> int:
    """Concrete reference evaluator used by fixtures and emitter replay guards."""
    lane_width = data_width // byte_enable_width
    mask = 0
    for lane in range(byte_enable_width):
        if (pending_enable >> lane) & 1:
            mask |= ((1 << lane_width) - 1) << (lane * lane_width)
    if pending_enable and pending_address == read_address:
        return (physical_word & ~mask) | (pending_data & mask)
    return physical_word
