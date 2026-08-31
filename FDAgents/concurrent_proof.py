"""Depth-one concurrent exact-proof and speculative-execution scheduler.

This standalone core deliberately has no Agent, Vivado, or candidate-graph
dependency.  Integration code supplies a cheap replay guard, exact proof,
isolated-copy execution, and real-netlist counterexample replay callbacks.
Promotion is possible only through :meth:`ConcurrentProofScheduler.authorize_promotion`.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import shutil
import time
import asyncio
from concurrent.futures import Executor, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Union

from .cex_replay_adjudicator import (
    ArtifactBinding,
    CounterexampleReplayAdjudicator,
    CounterexampleReplayRequest,
    CounterexampleReplayResult,
    CounterexampleVerdict,
    ReplayCallback,
)


class ProofClass(str, Enum):
    QF_BV = "qf_bv"
    ROBDD = "robdd"
    SAT = "sat"
    GENERIC_BIT_VECTOR = "generic_bit_vector"
    REGISTERED_ARITHMETIC_EXACT = "registered_arithmetic_exact"
    CONSTRUCTIVE_CONE_EQUALITY = "constructive_cone_equality"
    MEMORY_FORWARD_RELATIONAL = "memory_forward_relational"
    RECURRENCE_TRANSPORT_EXACT = "recurrence_transport_exact"


# Budgets cover proof plus the join data needed to adjudicate a failed proof.
# The enclosing Agent deadline remains authoritative and may be shorter.
DEFAULT_PROOF_TIMEOUTS_S: Mapping[ProofClass, float] = MappingProxyType({
    ProofClass.QF_BV: 180.0,
    ProofClass.ROBDD: 900.0,
    ProofClass.SAT: 300.0,
    ProofClass.GENERIC_BIT_VECTOR: 180.0,
    ProofClass.REGISTERED_ARITHMETIC_EXACT: 180.0,
    ProofClass.CONSTRUCTIVE_CONE_EQUALITY: 900.0,
    ProofClass.MEMORY_FORWARD_RELATIONAL: 300.0,
    ProofClass.RECURRENCE_TRANSPORT_EXACT: 180.0,
})

_PROOF_TIMEOUT_CATEGORY: Mapping[ProofClass, ProofClass] = MappingProxyType({
    ProofClass.QF_BV: ProofClass.QF_BV,
    ProofClass.ROBDD: ProofClass.ROBDD,
    ProofClass.SAT: ProofClass.SAT,
    ProofClass.GENERIC_BIT_VECTOR: ProofClass.QF_BV,
    ProofClass.REGISTERED_ARITHMETIC_EXACT: ProofClass.QF_BV,
    ProofClass.CONSTRUCTIVE_CONE_EQUALITY: ProofClass.ROBDD,
    ProofClass.MEMORY_FORWARD_RELATIONAL: ProofClass.SAT,
    ProofClass.RECURRENCE_TRANSPORT_EXACT: ProofClass.QF_BV,
})

_VM_SLOT_LOCKS: dict[str, Lock] = {}
_VM_SLOT_LOCKS_GUARD = Lock()


class ProofState(str, Enum):
    IN_FLIGHT = "proof_in_flight"
    PROVED = "proved"
    FAILED_REAL = "proof_failed_real"
    FAILED_MISBOUND = "proof_failed_misbound"
    TIMEOUT = "proof_timeout"


class ExactProofStatus(str, Enum):
    PROVED = "proved"
    FAILED = "failed"


class ConcurrentProofError(RuntimeError):
    """Base error for invalid scheduler operations or backend results."""


class SpeculationDepthError(ConcurrentProofError):
    """Raised before a second unproved mutation can start."""


class PromotionGateError(ConcurrentProofError):
    """Raised whenever promotion is attempted without a joined exact proof."""


@dataclass(frozen=True, slots=True)
class CandidateProofRequest:
    candidate_id: str
    parent_candidate_id: str
    source: ArtifactBinding
    proof_class: ProofClass
    action_fingerprint: str
    recovered_plan_digest: str
    vm_slot: str
    cheap_replay_vectors: int = 512

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.parent_candidate_id.strip():
            raise ValueError("candidate and parent candidate IDs must be nonempty")
        if not self.action_fingerprint.strip():
            raise ValueError("action_fingerprint must be nonempty")
        if not _is_sha256(self.recovered_plan_digest):
            raise ValueError("recovered_plan_digest must be a lowercase SHA-256")
        if not self.vm_slot.strip():
            raise ValueError("vm_slot must be nonempty")
        if self.cheap_replay_vectors != 512:
            raise ValueError("concurrent proof requires the 512-vector cheap guard")


@dataclass(frozen=True, slots=True)
class ExactProofResult:
    status: ExactProofStatus
    proof_id: str
    proof_digest: str
    recovered_plan_digest: str
    counterexample: Optional[Mapping[str, Any]] = None
    runtime_s: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.proof_id.strip():
            raise ValueError("proof_id must be nonempty")
        if not _is_sha256(self.proof_digest):
            raise ValueError("proof_digest must be a lowercase SHA-256")
        if not _is_sha256(self.recovered_plan_digest):
            raise ValueError("recovered_plan_digest must be a lowercase SHA-256")
        if self.status is ExactProofStatus.PROVED and self.counterexample is not None:
            raise ValueError("a proved result cannot contain a counterexample")
        if self.status is ExactProofStatus.FAILED and not isinstance(
            self.counterexample, Mapping
        ):
            raise ValueError("a failed exact proof must contain a counterexample")


@dataclass(frozen=True, slots=True)
class SpeculativeExecutionResult:
    candidate_id: str
    candidate: ArtifactBinding
    isolated_copy: bool
    emitted_topology_digest: str
    routed: bool
    measurement_complete: bool
    completed: bool = True
    runtime_s: Optional[float] = None
    payload: Any = None

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id must be nonempty")
        if not _is_sha256(self.emitted_topology_digest):
            raise ValueError("emitted_topology_digest must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ProofInFlightRow:
    candidate_id: str
    parent_candidate_id: str
    proof_class: str
    timeout_s: float
    speculation_depth: int
    row_type: str = field(default="proof_in_flight", init=False)

    def to_dict(self) -> dict[str, Any]:
        return _row_dict(self, promotion_allowed=False)


@dataclass(frozen=True, slots=True)
class ProofFailedRealRow:
    candidate_id: str
    proof_class: str
    proof_id: str
    adjudication: Mapping[str, Any]
    row_type: str = field(default="proof_failed_real", init=False)

    def to_dict(self) -> dict[str, Any]:
        return _row_dict(self, promotion_allowed=False)


@dataclass(frozen=True, slots=True)
class ProofFailedMisboundRow:
    candidate_id: str
    proof_class: str
    proof_id: str
    adjudication: Mapping[str, Any]
    row_type: str = field(default="proof_failed_misbound", init=False)

    def to_dict(self) -> dict[str, Any]:
        return _row_dict(self, promotion_allowed=False)


@dataclass(frozen=True, slots=True)
class ProofTimeoutRow:
    candidate_id: str
    proof_class: str
    timeout_s: float
    solver_result: str = "unknown"
    hard_timeout: bool = True
    row_type: str = field(default="proof_timeout", init=False)

    def to_dict(self) -> dict[str, Any]:
        return _row_dict(self, promotion_allowed=False)


ProofPacketRow = Union[
    ProofInFlightRow,
    ProofFailedRealRow,
    ProofFailedMisboundRow,
    ProofTimeoutRow,
]


@dataclass(frozen=True, slots=True)
class ProofJoinResult:
    candidate_id: str
    state: ProofState
    proof: Optional[ExactProofResult]
    execution: Optional[SpeculativeExecutionResult]
    adjudication: Optional[CounterexampleReplayResult]
    packet_rows: tuple[ProofPacketRow, ...]
    timing: Mapping[str, Optional[float]] = field(default_factory=dict)

    @property
    def promotion_allowed(self) -> bool:
        return (
            self.state is ProofState.PROVED
            and self.execution is not None
            and self.execution.completed
            and self.execution.isolated_copy
        )


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    candidate_id: str
    candidate_sha256: str
    proof_id: str
    proof_digest: str
    recovered_plan_digest: str
    emitted_topology_digest: str
    authority: str = "exact_proof_join_promotion_gate_v1"


@dataclass(slots=True)
class _Record:
    request: CandidateProofRequest
    proof_future: Future
    execution_future: Future
    launched_at: float
    timeout_s: float
    speculation_depth: int
    state: ProofState = ProofState.IN_FLIGHT
    proof: Optional[ExactProofResult] = None
    execution: Optional[SpeculativeExecutionResult] = None
    adjudication: Optional[CounterexampleReplayResult] = None
    rows: list[ProofPacketRow] = field(default_factory=list)
    joined_at: Optional[float] = None


CheapReplayGuard = Callable[[CandidateProofRequest], bool]
ExactProofWorker = Callable[[CandidateProofRequest], ExactProofResult]
SpeculativeExecutionWorker = Callable[
    [CandidateProofRequest], SpeculativeExecutionResult
]


@dataclass(frozen=True, slots=True)
class ShadowPrefetchResult:
    """Read-only next-action discovery bound to one routed candidate SHA."""

    seed: ArtifactBinding
    artifact_path: Path
    artifact_sha256: str
    payload_digest: str
    discard_root: Path
    runtime_s: float
    action_menu: Optional[Mapping[str, Any]] = None
    read_only: bool = True
    mutation_allowed: bool = False

    def __post_init__(self) -> None:
        if not _is_sha256(self.artifact_sha256) or not _is_sha256(self.payload_digest):
            raise ValueError("shadow prefetch digests must be lowercase SHA-256")
        if self.read_only is not True or self.mutation_allowed is not False:
            raise ValueError("shadow prefetch cannot carry mutation authority")
        if self.runtime_s < 0.0:
            raise ValueError("shadow prefetch runtime must be nonnegative")
        if self.action_menu is not None:
            from .action_menu import validate_action_menu

            validate_action_menu(
                self.action_menu, expected_seed_sha256=self.seed.sha256
            )


ShadowPrefetchWorker = Callable[[SpeculativeExecutionResult], Any]
AdmissionMeasurementWorker = Callable[[SpeculativeExecutionResult], Any]


def discard_shadow_prefetch(result: ShadowPrefetchResult) -> None:
    """Remove the complete dedicated shadow directory, never a shared parent."""
    root = Path(result.discard_root).resolve()
    artifact = Path(result.artifact_path).resolve()
    try:
        artifact.relative_to(root)
    except ValueError as exc:
        raise ConcurrentProofError("shadow artifact escapes its discard root") from exc
    if root == root.parent or root.name in {"", ".", ".."}:
        raise ConcurrentProofError("shadow discard root is unsafe")
    if root.exists():
        shutil.rmtree(root)


def finalize_shadow_prefetch(
    result: ShadowPrefetchResult,
    *,
    validation_passed: bool,
    promotion_occurred: bool,
    promoted_candidate_sha256: str,
    published_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Publish a typed receipt only for the strict-validated promoted seed."""
    if not validation_passed or not promotion_occurred:
        discard_shadow_prefetch(result)
        return None
    if result.seed.sha256 != promoted_candidate_sha256:
        discard_shadow_prefetch(result)
        raise ConcurrentProofError("shadow prefetch seed hash differs from promotion")
    artifact = Path(result.artifact_path).resolve()
    if artifact.is_symlink() or not artifact.is_file():
        discard_shadow_prefetch(result)
        raise ConcurrentProofError("shadow prefetch artifact is unavailable")
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != result.artifact_sha256:
        discard_shadow_prefetch(result)
        raise ConcurrentProofError("shadow prefetch artifact digest changed")
    if published_root is not None:
        destination = Path(published_root).resolve()
        source_root = Path(result.discard_root).resolve()
        if (
            destination.exists()
            or destination.is_symlink()
            or not destination.parent.is_dir()
        ):
            discard_shadow_prefetch(result)
            raise ConcurrentProofError("shadow prefetch publication root is not fresh")
        relative_artifact = artifact.relative_to(source_root)
        # Preserve the action-local source because preflight receipts bind the
        # exact bundle paths recorded during discovery. The published copy is
        # only the next-round lookup index; mutation authority still replays
        # the source/guard/proof receipt and bundle bytes.
        shutil.copytree(source_root, destination)
        artifact = destination / relative_artifact
    body = {
        "schema_version": "a5-shadow-prefetch-receipt-v1",
        "status": "prefetched_and_bound",
        "seed_candidate_sha256": result.seed.sha256,
        "artifact_path": str(artifact),
        "artifact_sha256": result.artifact_sha256,
        "payload_digest": result.payload_digest,
        "runtime_s": float(result.runtime_s),
        "read_only": True,
        "mutation_allowed": False,
        "validation_passed": True,
        "promotion_occurred": True,
    }
    if result.action_menu is not None:
        from .action_menu import bind_shadow_action_menu

        body["action_menu_binding"] = bind_shadow_action_menu(
            result.action_menu,
            promoted_candidate_sha256=promoted_candidate_sha256,
        )
    body["receipt_digest"] = hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    return body


@dataclass(frozen=True, slots=True)
class ConcurrentProofAction:
    """Agent-facing split backend supplied by a proof-capable skill."""

    request: CandidateProofRequest
    cheap_replay_guard: CheapReplayGuard
    exact_proof: ExactProofWorker
    speculative_execution: SpeculativeExecutionWorker
    replay_source: ReplayCallback
    replay_candidate: ReplayCallback
    strict_validation: Callable[[SpeculativeExecutionResult], Any]
    shadow_prefetch: Optional[ShadowPrefetchWorker] = None


@dataclass(frozen=True, slots=True)
class ConcurrentActionResult:
    value: Any
    receipt: Optional[PromotionReceipt]
    join: Optional[ProofJoinResult]
    used_concurrent_path: bool
    shadow_prefetch_task: Optional[asyncio.Task] = None


class ConcurrentProofJoinError(ConcurrentProofError):
    """Terminal non-proved join surfaced to Agent transaction handling."""

    def __init__(self, result: ProofJoinResult):
        self.result = result
        super().__init__(f"concurrent proof join ended as {result.state.value}")


class ConcurrentProofScheduler:
    """Run exact proof and isolated-copy execution with a fail-closed join."""

    MAX_SPECULATION_DEPTH = 1

    def __init__(
        self,
        *,
        executor: Optional[Executor] = None,
        proof_timeouts_s: Optional[Mapping[ProofClass, float]] = None,
        adjudicator: Optional[CounterexampleReplayAdjudicator] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._executor = executor or ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="fdagents-proof"
        )
        self._owns_executor = executor is None
        self._timeouts = dict(DEFAULT_PROOF_TIMEOUTS_S)
        if proof_timeouts_s:
            for proof_class, timeout_s in proof_timeouts_s.items():
                timeout = float(timeout_s)
                if timeout <= 0.0:
                    raise ValueError("proof timeouts must be positive")
                self._timeouts[ProofClass(proof_class)] = timeout
        self._adjudicator = adjudicator or CounterexampleReplayAdjudicator()
        self._monotonic = monotonic
        self._records: dict[str, _Record] = {}
        self._lock = RLock()

    def launch(
        self,
        request: CandidateProofRequest,
        *,
        cheap_replay_guard: CheapReplayGuard,
        exact_proof: ExactProofWorker,
        speculative_execution: SpeculativeExecutionWorker,
    ) -> ProofInFlightRow:
        """Pass the cheap guard, then launch proof and execution concurrently."""
        with self._lock:
            if request.candidate_id in self._records:
                raise ConcurrentProofError(
                    f"candidate {request.candidate_id!r} is already scheduled"
                )
            speculation_depth = self._next_speculation_depth(
                request.parent_candidate_id
            )
            if speculation_depth > self.MAX_SPECULATION_DEPTH:
                raise SpeculationDepthError(
                    "speculation depth 1 forbids execution from an unproved parent; "
                    "read-only discovery may continue but a child mutation must wait"
                )
            if cheap_replay_guard(request) is not True:
                raise ConcurrentProofError("cheap replay guard rejected the candidate")
            timeout_s = self._timeouts[request.proof_class]
            launched_at = self._monotonic()
            proof_future = self._executor.submit(exact_proof, request)
            try:
                execution_future = self._executor.submit(
                    self._run_serialized_execution,
                    request,
                    speculative_execution,
                )
            except Exception:
                proof_future.cancel()
                raise
            row = ProofInFlightRow(
                candidate_id=request.candidate_id,
                parent_candidate_id=request.parent_candidate_id,
                proof_class=request.proof_class.value,
                timeout_s=timeout_s,
                speculation_depth=speculation_depth,
            )
            self._records[request.candidate_id] = _Record(
                request=request,
                proof_future=proof_future,
                execution_future=execution_future,
                launched_at=launched_at,
                timeout_s=timeout_s,
                speculation_depth=speculation_depth,
                rows=[row],
            )
            return row

    def state(self, candidate_id: str) -> ProofState:
        with self._lock:
            return self._record(candidate_id).state

    def packet_rows(self, candidate_id: str) -> tuple[ProofPacketRow, ...]:
        with self._lock:
            return tuple(self._record(candidate_id).rows)

    def discovery_seed(self, candidate_id: str) -> ArtifactBinding:
        """Expose an isolated copy for read-only discovery, never mutation.

        This is the enabling-pool overlap hook: discovery can inspect a completed
        speculative implementation while its parent action proof is still in
        flight.  A follow-up speculative mutation remains blocked by ``launch``.
        """
        with self._lock:
            record = self._record(candidate_id)
            if record.state not in {ProofState.IN_FLIGHT, ProofState.PROVED}:
                raise ConcurrentProofError(
                    f"candidate {candidate_id} cannot seed discovery in {record.state.value}"
                )
            if not record.execution_future.done():
                raise ConcurrentProofError("speculative candidate copy is not ready")
            execution = self._validated_execution(
                record, record.execution_future.result()
            )
            record.execution = execution
            return execution.candidate

    def join(
        self,
        candidate_id: str,
        *,
        replay_source: ReplayCallback,
        replay_candidate: ReplayCallback,
        timeout_s: Optional[float] = None,
    ) -> ProofJoinResult:
        """Join both branches into proved, failed-real, failed-misbound, or timeout."""
        with self._lock:
            record = self._record(candidate_id)
            if record.state is not ProofState.IN_FLIGHT:
                return self._join_result(record)
            proof_budget = record.timeout_s if timeout_s is None else min(
                record.timeout_s, float(timeout_s)
            )
            if proof_budget <= 0.0:
                return self._mark_timeout(record, proof_budget)
            joined_started = self._monotonic()
            proof_deadline = min(
                record.launched_at + record.timeout_s,
                joined_started + proof_budget,
            )
            execution_deadline = (
                joined_started + float(timeout_s)
                if timeout_s is not None
                else None
            )

        try:
            proof = record.proof_future.result(
                timeout=self._remaining(proof_deadline)
            )
            proof = self._validated_proof(record, proof)
            execution = record.execution_future.result(
                timeout=(
                    self._remaining(execution_deadline)
                    if execution_deadline is not None
                    else None
                )
            )
            execution = self._validated_execution(record, execution)
        except TimeoutError:
            with self._lock:
                return self._mark_timeout(record, proof_budget)

        with self._lock:
            if record.state is not ProofState.IN_FLIGHT:
                return self._join_result(record)
            record.proof = proof
            record.execution = execution
            record.joined_at = self._monotonic()
            if proof.status is ExactProofStatus.PROVED:
                record.state = ProofState.PROVED
                return self._join_result(record)

            replay_request = CounterexampleReplayRequest(
                candidate_id=record.request.candidate_id,
                proof_id=proof.proof_id,
                source=record.request.source,
                candidate=execution.candidate,
                counterexample=proof.counterexample or {},
            )
            adjudication = self._adjudicator.adjudicate(
                replay_request,
                replay_source=replay_source,
                replay_candidate=replay_candidate,
            )
            record.adjudication = adjudication
            common = {
                "candidate_id": record.request.candidate_id,
                "proof_class": record.request.proof_class.value,
                "proof_id": proof.proof_id,
                "adjudication": adjudication.to_dict(),
            }
            if adjudication.verdict is CounterexampleVerdict.REAL_FAILURE:
                record.state = ProofState.FAILED_REAL
                record.rows.append(ProofFailedRealRow(**common))
            else:
                record.state = ProofState.FAILED_MISBOUND
                record.rows.append(ProofFailedMisboundRow(**common))
            return self._join_result(record)

    def authorize_promotion(
        self, candidate_id: str, *, candidate_sha256: str
    ) -> PromotionReceipt:
        """Return a proof receipt or raise; there is no permissive fallback."""
        with self._lock:
            record = self._record(candidate_id)
            if record.state is not ProofState.PROVED:
                raise PromotionGateError(
                    f"candidate {candidate_id} proof state is {record.state.value}; "
                    "promotion requires proved"
                )
            if record.proof is None or record.execution is None:
                raise PromotionGateError("joined proof/execution evidence is incomplete")
            if not record.execution.completed or not record.execution.isolated_copy:
                raise PromotionGateError("speculative execution was not completed on a copy")
            if record.execution.candidate.sha256 != candidate_sha256:
                raise PromotionGateError("promotion artifact hash differs from proof join")
            return PromotionReceipt(
                candidate_id=candidate_id,
                candidate_sha256=candidate_sha256,
                proof_id=record.proof.proof_id,
                proof_digest=record.proof.proof_digest,
                recovered_plan_digest=record.proof.recovered_plan_digest,
                emitted_topology_digest=(
                    record.execution.emitted_topology_digest
                ),
            )

    def close(self, *, wait: bool = True) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)

    def _next_speculation_depth(self, parent_candidate_id: str) -> int:
        parent = self._records.get(parent_candidate_id)
        if parent is None or parent.state is ProofState.PROVED:
            return 1
        if parent.state is ProofState.IN_FLIGHT:
            return parent.speculation_depth + 1
        raise SpeculationDepthError(
            f"failed or timed-out parent {parent_candidate_id!r} cannot seed execution"
        )

    def _record(self, candidate_id: str) -> _Record:
        try:
            return self._records[candidate_id]
        except KeyError as exc:
            raise ConcurrentProofError(f"unknown candidate {candidate_id!r}") from exc

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0.0:
            raise TimeoutError
        return remaining

    @staticmethod
    def _validated_proof(record: _Record, value: Any) -> ExactProofResult:
        if not isinstance(value, ExactProofResult):
            raise ConcurrentProofError("exact proof worker returned an invalid result")
        if value.recovered_plan_digest != record.request.recovered_plan_digest:
            raise ConcurrentProofError("exact proof recovered-plan digest is misbound")
        return value

    @staticmethod
    def _validated_execution(
        record: _Record, value: Any
    ) -> SpeculativeExecutionResult:
        if not isinstance(value, SpeculativeExecutionResult):
            raise ConcurrentProofError(
                "speculative execution worker returned an invalid result"
            )
        if value.candidate_id != record.request.candidate_id:
            raise ConcurrentProofError("speculative execution candidate ID is misbound")
        if not value.completed or not value.isolated_copy:
            raise ConcurrentProofError(
                "speculative execution must complete on an isolated candidate copy"
            )
        if not value.routed or not value.measurement_complete:
            raise ConcurrentProofError(
                "discovery and join require a post-measurement routed speculative DCP"
            )
        return value

    def _mark_timeout(self, record: _Record, budget: float) -> ProofJoinResult:
        if record.state is ProofState.IN_FLIGHT:
            record.state = ProofState.TIMEOUT
            record.joined_at = self._monotonic()
            record.proof_future.cancel()
            record.execution_future.cancel()
            record.rows.append(ProofTimeoutRow(
                candidate_id=record.request.candidate_id,
                proof_class=record.request.proof_class.value,
                timeout_s=max(0.0, float(budget)),
            ))
        return self._join_result(record)

    @staticmethod
    def _join_result(record: _Record) -> ProofJoinResult:
        proof_runtime = (
            record.proof.runtime_s if record.proof is not None else None
        )
        execution_runtime = (
            record.execution.runtime_s if record.execution is not None else None
        )
        serial_runtime = (
            float(proof_runtime) + float(execution_runtime)
            if proof_runtime is not None and execution_runtime is not None
            else None
        )
        elapsed = (
            max(0.0, record.joined_at - record.launched_at)
            if record.joined_at is not None
            else None
        )
        return ProofJoinResult(
            candidate_id=record.request.candidate_id,
            state=record.state,
            proof=record.proof,
            execution=record.execution,
            adjudication=record.adjudication,
            packet_rows=tuple(record.rows),
            timing={
                "proof_runtime_s": proof_runtime,
                "speculative_execution_runtime_s": execution_runtime,
                "serial_runtime_s": serial_runtime,
                "overlapped_wall_s": elapsed,
                "overlap_saving_s": (
                    max(0.0, serial_runtime - elapsed)
                    if serial_runtime is not None and elapsed is not None
                    else None
                ),
            },
        )

    @staticmethod
    def _run_serialized_execution(
        request: CandidateProofRequest,
        worker: SpeculativeExecutionWorker,
    ) -> SpeculativeExecutionResult:
        with _VM_SLOT_LOCKS_GUARD:
            slot_lock = _VM_SLOT_LOCKS.setdefault(request.vm_slot, Lock())
        with slot_lock:
            return worker(request)


def configured_proof_timeouts(cfg: Any) -> dict[ProofClass, float]:
    """Expand the closed QF_BV/ROBDD/SAT config table to all proof classes."""
    configured = {
        ProofClass.QF_BV: cfg.get(
            "concurrent_proof.timeouts_s.qf_bv", DEFAULT_PROOF_TIMEOUTS_S[ProofClass.QF_BV]
        ),
        ProofClass.ROBDD: cfg.get(
            "concurrent_proof.timeouts_s.robdd", DEFAULT_PROOF_TIMEOUTS_S[ProofClass.ROBDD]
        ),
        ProofClass.SAT: cfg.get(
            "concurrent_proof.timeouts_s.sat", DEFAULT_PROOF_TIMEOUTS_S[ProofClass.SAT]
        ),
    }
    normalized: dict[ProofClass, float] = {}
    for proof_class, value in configured.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{proof_class.value} proof timeout must be numeric")
        timeout = float(value)
        if timeout <= 0.0:
            raise ValueError(f"{proof_class.value} proof timeout must be positive")
        normalized[proof_class] = timeout
    return {
        proof_class: normalized[category]
        for proof_class, category in _PROOF_TIMEOUT_CATEGORY.items()
    }


async def run_optional_concurrent_action(
    *,
    enabled: bool,
    serial_execution: Callable[[], Any],
    prepare_concurrent: Optional[Callable[[], Any]],
    proof_timeouts_s: Optional[Mapping[ProofClass, float]] = None,
    packet_sink: Optional[
        Callable[[CandidateProofRequest, tuple[ProofPacketRow, ...]], None]
    ] = None,
    admission_measurement: Optional[AdmissionMeasurementWorker] = None,
    scheduler_factory: Callable[..., ConcurrentProofScheduler] = ConcurrentProofScheduler,
) -> ConcurrentActionResult:
    """Agent seam: exact off-path passthrough or proof-gated split execution.

    The optional provider is never called while the flag is off.  A provider
    returns ``ConcurrentProofAction``.  A supplied central admission measurement
    runs after a proved join and before the expensive strict validator.  It may
    return a terminal non-candidate result for a legal but non-improving DCP.
    """
    if not enabled or prepare_concurrent is None:
        return ConcurrentActionResult(
            value=await _maybe_await(serial_execution()),
            receipt=None,
            join=None,
            used_concurrent_path=False,
            shadow_prefetch_task=None,
        )

    action = await _maybe_await(prepare_concurrent())
    if not isinstance(action, ConcurrentProofAction):
        raise ConcurrentProofError(
            "concurrent proof provider returned an invalid split action"
        )
    scheduler = scheduler_factory(proof_timeouts_s=proof_timeouts_s)
    try:
        row = scheduler.launch(
            action.request,
            cheap_replay_guard=action.cheap_replay_guard,
            exact_proof=action.exact_proof,
            speculative_execution=action.speculative_execution,
        )
        if packet_sink is not None:
            packet_sink(action.request, (row,))
        join = await asyncio.to_thread(
            scheduler.join,
            action.request.candidate_id,
            replay_source=action.replay_source,
            replay_candidate=action.replay_candidate,
        )
        if packet_sink is not None:
            packet_sink(action.request, join.packet_rows)
        if join.state is not ProofState.PROVED:
            raise ConcurrentProofJoinError(join)
        assert join.execution is not None
        receipt = scheduler.authorize_promotion(
            action.request.candidate_id,
            candidate_sha256=join.execution.candidate.sha256,
        )
        if admission_measurement is not None:
            terminal_value = await _maybe_await(
                admission_measurement(join.execution)
            )
            if terminal_value is not None:
                return ConcurrentActionResult(
                    value=terminal_value,
                    receipt=receipt,
                    join=join,
                    used_concurrent_path=True,
                    shadow_prefetch_task=None,
                )
        shadow_task = (
            asyncio.create_task(_maybe_await(action.shadow_prefetch(join.execution)))
            if action.shadow_prefetch is not None else None
        )
        try:
            value = await _maybe_await(action.strict_validation(join.execution))
        except BaseException:
            if shadow_task is not None:
                if not shadow_task.done():
                    shadow_task.cancel()
                try:
                    shadow_value = await shadow_task
                except BaseException:
                    shadow_value = None
                if isinstance(shadow_value, ShadowPrefetchResult):
                    discard_shadow_prefetch(shadow_value)
            raise
        return ConcurrentActionResult(
            value=value,
            receipt=receipt,
            join=join,
            used_concurrent_path=True,
            shadow_prefetch_task=shadow_task,
        )
    finally:
        scheduler.close(wait=False)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _row_dict(row: Any, *, promotion_allowed: bool) -> dict[str, Any]:
    values = {
        name: copy.deepcopy(getattr(row, name))
        for name in row.__dataclass_fields__
    }
    values.update({
        "schema": "concurrent_proof_packet_row_v1",
        "promotion_allowed": promotion_allowed,
        "llm_approval_authority": False,
    })
    return values


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
