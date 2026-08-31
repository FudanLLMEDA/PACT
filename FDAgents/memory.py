"""Run state for FDAgents.

Memory is pure bookkeeping: rounds, best checkpoint, budgets, failed
attempts, and probe ring buffers. It contains no policy; private replay and
transaction bindings are deliberately excluded from LLM state projections.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import candidate_graph as graph_ops
from .artifacts import atomic_write_json
from .saturation import level1_saturation_state, resolve_strategy_family
from .scoring import break_even_action_gain_mhz, leaderboard_target, score_snapshot
from .physical_signature import physical_signature_distance
from .skills.base import calculate_fmax



_LOGGER = logging.getLogger("FDAgents")

class CandidateSelectionError(ValueError):
    """Raised when an explicit/default graph seed cannot be selected safely."""


def _compact_probe_value(value, depth: int = 0):
    """Bound retained probe evidence without character-slicing JSON."""
    if depth >= 5:
        return "..."
    if isinstance(value, dict):
        return {
            str(key): _compact_probe_value(item, depth + 1)
            for key, item in list(value.items())[:16]
        }
    if isinstance(value, (list, tuple)):
        return [_compact_probe_value(item, depth + 1) for item in value[:3]]
    if isinstance(value, str) and len(value) > 240:
        return value[:237] + "..."
    return value


def compact_probe_response(tool: str, response: object) -> dict:
    """Keep a typed, bounded probe disposition for later decision turns."""
    original = response if isinstance(response, str) else json.dumps(response)
    try:
        parsed = json.loads(original) if isinstance(original, str) else response
    except (TypeError, json.JSONDecodeError):
        return {
            "status": "non_json",
            "response_prefix": str(original or "")[:400],
            "original_chars": len(str(original or "")),
        }
    if not isinstance(parsed, dict):
        return {
            "status": "structured",
            "value": _compact_probe_value(parsed),
            "original_chars": len(str(original or "")),
        }
    return {
        "status": str(parsed.get("status") or "structured"),
        "facts": _compact_probe_value(parsed),
        "tool": str(tool),
        "original_chars": len(str(original or "")),
    }


_REPLAY_CONTROL_FIELDS = frozenset({
    "schema_version",
    "metadata",
    "provenance_epochs",
    "state",
    "pending_selection",
    "slot_bindings",
    "closeout",
    "submission",
})
_REPLAY_SLOTS = frozenset({"basin", "semantic"})
_MAX_REPLAY_CONTROL_BYTES = 1024 * 1024
_MAX_POLICY_CONTEXT_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 32


def _copy_strict_json_object(
    value: object,
    *,
    field_name: str,
    max_bytes: int,
) -> dict:
    """Validate and copy a bounded object containing only exact JSON types."""
    if type(value) is not dict:
        raise ValueError(f"{field_name} must be a JSON object")

    def validate(node: object, depth: int = 0) -> None:
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{field_name} exceeds maximum JSON nesting")
        if type(node) is dict:
            for key, item in node.items():
                if type(key) is not str:
                    raise ValueError(f"{field_name} JSON object keys must be strings")
                validate(item, depth + 1)
            return
        if type(node) is list:
            for item in node:
                validate(item, depth + 1)
            return
        if node is None or type(node) in {bool, int, float, str}:
            return
        raise ValueError(f"{field_name} contains a non-JSON value")

    validate(value)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"{field_name} is not strict JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds {max_bytes} encoded bytes")
    return copy.deepcopy(value)


def _same_json_value(left: object, right: object) -> bool:
    """Compare validated JSON values without bool/int or int/float coercion."""
    return json.dumps(
        left,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) == json.dumps(
        right,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _copy_policy_context(value: object) -> Optional[dict]:
    if value is None:
        return None
    return _copy_strict_json_object(
        value,
        field_name="action transaction policy_context",
        max_bytes=_MAX_POLICY_CONTEXT_BYTES,
    )


@dataclass
class Memory:
    design_id: str
    baseline: dict                    # {wns, fmax, clock_period, bottleneck, ...}
    current_best: dict                # {wns, fmax, dcp_path, round}
    history: list                     # per-round records
    budget: dict                      # {start_time, time_limit_s, time_elapsed, time_remaining, tokens_spent, tokens_remaining}
    failed_attempts: list             # informational: what failed and why
    suggested_targets: dict           # {skill: [target, ...]} — hints, never gates
    notes: list = field(default_factory=list)   # lessons/reflections carried across rounds
    round: int = 0
    custom_calls_used: int = 0
    probe_count: int = 0
    mutation_count: int = 0
    validation_count: int = 0
    last_custom_responses: list = field(default_factory=list)
    plan: dict = field(default_factory=dict)    # analyze-stage output (living plan)
    current_telemetry: dict = field(default_factory=dict)
    telemetry_epochs: list = field(default_factory=list)
    telemetry_failures: list = field(default_factory=list)
    candidate_graph: dict = field(default_factory=dict)
    stop: dict = field(default_factory=dict)
    artifact_gc: dict = field(default_factory=dict)
    pending_transaction: dict = field(default_factory=dict)
    action_transactions: list = field(default_factory=list)
    replay_control: dict = field(default_factory=dict)
    run_manifest: dict = field(default_factory=dict)
    score_calibration_history: list = field(default_factory=list)
    pending_score_calibration: dict = field(default_factory=dict)
    hard_target: dict = field(default_factory=dict)
    report_snapshots: list = field(default_factory=list)
    reasoning_cycles: list = field(default_factory=list)
    decision_reviews: list = field(default_factory=list)
    exhausted_probes: dict = field(default_factory=dict)

    # How many recent probe responses ride along in the LLM state.
    CUSTOM_RESPONSE_WINDOW = 3
    TELEMETRY_HISTORY_WINDOW = 32
    EXHAUSTED_PROBE_WINDOW = 32

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save(self, path: Path) -> None:
        atomic_write_json(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> "Memory":
        data = json.loads(path.read_text())
        memory = cls(**data)
        memory.candidate_graph = graph_ops.migrate_graph(memory.candidate_graph)
        memory.replay_control = memory._validated_replay_control(allow_empty=True)
        memory._validate_loaded_action_transaction()
        return memory

    @classmethod
    def init(
        cls,
        design_id: str,
        baseline_wns: float,
        clock_period: Optional[float],
        bottleneck: str,
        input_dcp: str,
        time_limit_s: float = 3600.0,
        token_budget_usd: float = 1.0,
        rank1_score: Optional[float] = None,
        score_baseline_fmax_mhz: Optional[float] = None,
    ) -> "Memory":
        fmax = calculate_fmax(baseline_wns, clock_period)
        target_baseline = (
            score_baseline_fmax_mhz
            if score_baseline_fmax_mhz is not None
            else fmax
        )
        target = (
            leaderboard_target(
                baseline_fmax_mhz=target_baseline,
                rank1_score=rank1_score,
            )
            if rank1_score is not None and target_baseline is not None
            else {}
        )
        return cls(
            design_id=design_id,
            baseline={
                "wns": baseline_wns,
                "fmax": fmax,
                "clock_period": clock_period,
                "bottleneck": bottleneck,
            },
            current_best={
                "wns": baseline_wns,
                "fmax": fmax,
                "dcp_path": input_dcp,
                "round": 0,
            },
            history=[],
            budget={
                "start_time": time.time(),
                "time_limit_s": time_limit_s,
                "time_elapsed": 0.0,
                "time_remaining": time_limit_s,
                "tokens_spent": 0.0,
                "tokens_remaining": token_budget_usd,
            },
            failed_attempts=[],
            suggested_targets={},
            hard_target=target,
        )

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------
    def refresh_budget(self) -> None:
        now = time.time()
        self.budget["time_elapsed"] = now - self.budget["start_time"]
        self.budget["time_remaining"] = max(
            0.0, self.budget["time_limit_s"] - self.budget["time_elapsed"]
        )

    def time_remaining(self) -> float:
        self.refresh_budget()
        return float(self.budget["time_remaining"])

    def tokens_remaining(self) -> float:
        return float(self.budget.get("tokens_remaining", 0.0))

    def record_token_spend(self, cost_usd: float) -> None:
        self.budget["tokens_spent"] = self.budget.get("tokens_spent", 0.0) + cost_usd
        self.budget["tokens_remaining"] = max(
            0.0, self.budget.get("tokens_remaining", 0.0) - cost_usd
        )

    def record_stop(
        self,
        *,
        source: str,
        reason: str,
        rationale: str = "",
        hard_mechanical: bool,
    ) -> None:
        """Persist the first terminal reason without turning evidence into policy."""
        if self.stop:
            return
        self.stop = {
            "source": str(source),
            "reason": str(reason),
            "rationale": str(rationale),
            "hard_mechanical": bool(hard_mechanical),
            "round": self.round,
            "timestamp": time.time(),
        }

    # ------------------------------------------------------------------
    # Private replay bookkeeping
    # ------------------------------------------------------------------
    def _validated_replay_control(self, *, allow_empty: bool = False) -> dict:
        control = _copy_strict_json_object(
            self.replay_control,
            field_name="replay_control",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        if not control:
            if allow_empty:
                return {}
            raise ValueError("replay_control is not initialized")
        if set(control) != _REPLAY_CONTROL_FIELDS:
            missing = sorted(_REPLAY_CONTROL_FIELDS - set(control))
            unknown = sorted(set(control) - _REPLAY_CONTROL_FIELDS)
            raise ValueError(
                f"replay_control fields are invalid; missing={missing}, unknown={unknown}"
            )
        if type(control["schema_version"]) is not int or control["schema_version"] != 2:
            raise ValueError("replay_control schema_version must be 2")
        if type(control["metadata"]) is not dict:
            raise ValueError("replay_control metadata must be a JSON object")
        epochs = control["provenance_epochs"]
        if type(epochs) is not list or not epochs or len(epochs) > 64:
            raise ValueError(
                "replay_control provenance_epochs must contain 1..64 records"
            )
        for index, epoch in enumerate(epochs):
            if type(epoch) is not dict or set(epoch) != {
                "epoch", "reason", "recorded_at", "provenance"
            }:
                raise ValueError(f"replay provenance epoch {index} is malformed")
            if epoch["epoch"] != index:
                raise ValueError("replay provenance epoch ordinals are not contiguous")
            if epoch["reason"] not in {"initial", "resume", "completed_resume"}:
                raise ValueError("replay provenance epoch reason is invalid")
            if (
                isinstance(epoch["recorded_at"], bool)
                or not isinstance(epoch["recorded_at"], (int, float))
                or type(epoch["provenance"]) is not dict
                or not epoch["provenance"]
            ):
                raise ValueError(f"replay provenance epoch {index} has invalid data")
        if type(control["state"]) is not dict:
            raise ValueError("replay_control state must be a JSON object")
        pending = control["pending_selection"]
        if pending is not None and type(pending) is not dict:
            raise ValueError(
                "replay_control pending_selection must be null or a JSON object"
            )
        bindings = control["slot_bindings"]
        if type(bindings) is not dict:
            raise ValueError("replay_control slot_bindings must be a JSON object")
        if set(bindings) - _REPLAY_SLOTS:
            raise ValueError("replay_control contains an unknown mutation slot")
        if any(type(record) is not dict or not record for record in bindings.values()):
            raise ValueError("replay_control slot bindings must be nonempty objects")
        for field_name in ("closeout", "submission"):
            if type(control[field_name]) is not dict:
                raise ValueError(
                    f"replay_control {field_name} evidence must be a JSON object"
                )
        if control["submission"] and not control["closeout"]:
            raise ValueError("replay submission evidence requires closeout evidence")
        return control

    def _require_replay_control(self) -> dict:
        if not self.replay_control:
            raise RuntimeError("replay_control is not initialized")
        return self._validated_replay_control()

    def initialize_replay_control(
        self,
        metadata: dict,
        state: dict,
        *,
        provenance: dict,
        recorded_at: Optional[float] = None,
    ) -> None:
        if type(self.replay_control) is not dict:
            raise ValueError("replay_control must be an empty JSON object")
        if self.replay_control:
            raise RuntimeError("replay_control is already initialized")
        metadata_copy = _copy_strict_json_object(
            metadata,
            field_name="replay policy metadata",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        state_copy = _copy_strict_json_object(
            state,
            field_name="replay state",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        provenance_copy = _copy_strict_json_object(
            provenance,
            field_name="replay execution provenance",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        if not provenance_copy:
            raise ValueError("replay execution provenance must be nonempty")
        self.replay_control = {
            "schema_version": 2,
            "metadata": metadata_copy,
            "provenance_epochs": [{
                "epoch": 0,
                "reason": "initial",
                "recorded_at": float(time.time() if recorded_at is None else recorded_at),
                "provenance": provenance_copy,
            }],
            "state": state_copy,
            "pending_selection": None,
            "slot_bindings": {},
            "closeout": {},
            "submission": {},
        }

    def validate_replay_control(self, metadata: Optional[dict] = None) -> dict:
        control = self._validated_replay_control()
        if metadata is not None:
            expected_metadata = _copy_strict_json_object(
                metadata,
                field_name="replay policy metadata",
                max_bytes=_MAX_REPLAY_CONTROL_BYTES,
            )
            if not _same_json_value(control["metadata"], expected_metadata):
                raise ValueError(
                    "replay semantic contract drifted from durable metadata"
                )
        return control

    def record_replay_provenance(
        self,
        provenance: dict,
        *,
        reason: str,
        recorded_at: Optional[float] = None,
    ) -> None:
        if reason not in {"resume", "completed_resume"}:
            raise ValueError("replay provenance reason is invalid")
        provenance_copy = _copy_strict_json_object(
            provenance,
            field_name="replay execution provenance",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        if not provenance_copy:
            raise ValueError("replay execution provenance must be nonempty")
        control = self._require_replay_control()
        epochs = control["provenance_epochs"]
        if len(epochs) >= 64:
            raise RuntimeError("replay provenance epoch limit reached")
        epochs.append({
            "epoch": len(epochs),
            "reason": reason,
            "recorded_at": float(time.time() if recorded_at is None else recorded_at),
            "provenance": provenance_copy,
        })
        self.replay_control = control

    def set_replay_state(self, state: dict) -> None:
        state_copy = _copy_strict_json_object(
            state,
            field_name="replay state",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        control = self._require_replay_control()
        control["state"] = state_copy
        self.replay_control = control

    def set_replay_pending_selection(self, selection: dict) -> None:
        selection_copy = _copy_strict_json_object(
            selection,
            field_name="replay pending selection",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        control = self._require_replay_control()
        control["pending_selection"] = selection_copy
        self.replay_control = control

    def clear_replay_pending_selection(self) -> None:
        control = self._require_replay_control()
        control["pending_selection"] = None
        self.replay_control = control

    def bind_replay_slot(self, slot: str, record: dict) -> None:
        if slot not in _REPLAY_SLOTS:
            raise ValueError("replay mutation slot must be 'basin' or 'semantic'")
        record_copy = _copy_strict_json_object(
            record,
            field_name=f"replay {slot} slot binding",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        if not record_copy:
            raise ValueError("replay slot binding must be nonempty")
        control = self._require_replay_control()
        bindings = control["slot_bindings"]
        if slot in bindings:
            if not _same_json_value(bindings[slot], record_copy):
                raise RuntimeError(f"replay {slot} slot is already bound differently")
            return
        bindings[slot] = record_copy
        self.replay_control = control

    def record_replay_closeout(self, record: dict) -> None:
        record_copy = _copy_strict_json_object(
            record,
            field_name="replay closeout evidence",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        if not record_copy:
            raise ValueError("replay closeout evidence must be nonempty")
        control = self._require_replay_control()
        if control["closeout"]:
            if not _same_json_value(control["closeout"], record_copy):
                raise RuntimeError("replay closeout is already recorded differently")
            return
        control["closeout"] = record_copy
        self.replay_control = control

    def record_replay_submission(self, record: dict) -> None:
        record_copy = _copy_strict_json_object(
            record,
            field_name="replay submission evidence",
            max_bytes=_MAX_REPLAY_CONTROL_BYTES,
        )
        if not record_copy:
            raise ValueError("replay submission evidence must be nonempty")
        control = self._require_replay_control()
        if not control["closeout"]:
            raise RuntimeError("replay submission requires a recorded closeout")
        if control["submission"]:
            if not _same_json_value(control["submission"], record_copy):
                raise RuntimeError("replay submission is already recorded differently")
            return
        control["submission"] = record_copy
        self.replay_control = control

    def _validate_loaded_action_transaction(self) -> None:
        transaction = self.pending_transaction
        if type(transaction) is not dict:
            raise ValueError("pending action transaction must be a JSON object")
        if not transaction or transaction.get("version") != 6:
            return
        if type(transaction["version"]) is not int:
            raise ValueError("pending action transaction version must be an integer")
        if "policy_context" not in transaction:
            raise ValueError("version-6 action transaction lacks policy_context")
        transaction["policy_context"] = _copy_policy_context(
            transaction["policy_context"]
        )

    def begin_action_transaction(
        self,
        *,
        candidate_id: str,
        seed_candidate_id: str,
        skill: str,
        target: str,
        params: dict,
        action_fingerprint: str,
        action_dir: str,
        action_proof: Optional[dict] = None,
        review_id: Optional[str] = None,
        score_estimate: Optional[dict] = None,
        score_projection: Optional[dict] = None,
        policy_context: Optional[dict] = None,
    ) -> None:
        if self.pending_transaction:
            raise RuntimeError("an action transaction is already pending")
        policy_context_copy = _copy_policy_context(policy_context)
        self.pending_transaction = {
            "version": 6,
            "transaction_id": f"t{candidate_id[1:]}",
            "phase": "prepared",
            "candidate_id": candidate_id,
            "candidate_ids": [candidate_id],
            "candidates": [],
            "seed_candidate_id": seed_candidate_id,
            "created_round": self.round + 1,
            "skill": str(skill),
            "target": str(target),
            "params": dict(params),
            "action_fingerprint": str(action_fingerprint),
            "action_dir": str(action_dir),
            "created_at": time.time(),
            "execution_started": False,
            "action_proof": copy.deepcopy(action_proof or {}),
            "action_proof_sha256": str(
                (action_proof or {}).get("proof_sha256") or ""
            ),
            "review_id": str(review_id or ""),
            "score_estimate": dict(score_estimate or {}),
            "score_projection": dict(score_projection or {}),
            "policy_context": policy_context_copy,
        }
        if score_estimate:
            self.refresh_budget()
            self.pending_score_calibration = {
                "version": 1,
                "transaction_id": self.pending_transaction["transaction_id"],
                "round": self.round + 1,
                "seed_candidate_id": seed_candidate_id,
                "skill": str(skill),
                "target": str(target),
                "estimate": dict(score_estimate),
                "projection": dict(score_projection or {}),
                "started_elapsed_runtime_s": float(self.budget["time_elapsed"]),
                "global_best_before_fmax_mhz": float(self.current_best["fmax"]),
                "status": "pending",
            }

    def assign_action_candidates(self, outputs: tuple) -> None:
        """Durably assign ordered graph IDs after bounded output validation."""
        if not self.pending_transaction:
            raise RuntimeError("no action transaction is pending")
        if self.pending_transaction.get("phase") != "executing":
            raise RuntimeError("action candidates can only be assigned after execution")
        if not outputs:
            raise ValueError("an action transaction requires at least one candidate")
        candidate_ids = list(self.pending_transaction.get("candidate_ids") or [])
        if not candidate_ids:
            candidate_ids.append(self.allocate_candidate_id())
        while len(candidate_ids) < len(outputs):
            candidate_ids.append(self.allocate_candidate_id())
        if len(candidate_ids) != len(outputs):
            raise RuntimeError("transaction reserved more candidate IDs than outputs")
        self.pending_transaction["candidate_ids"] = candidate_ids
        self.pending_transaction["candidates"] = [
            {
                "ordinal": ordinal,
                "candidate_id": candidate_ids[ordinal],
                "source_path": str(output.output_dcp),
                "label": output.label,
                "evidence": output.evidence,
                "proof": output.proof,
            }
            for ordinal, output in enumerate(outputs)
        ]
        self.advance_action_transaction("publishing")

    def advance_action_transaction(self, phase: str, **facts) -> None:
        order = {
            "prepared": 0,
            "executing": 1,
            "publishing": 2,
            "published": 3,
            "measured": 4,
        }
        if not self.pending_transaction:
            raise RuntimeError("no action transaction is pending")
        current = self.pending_transaction.get("phase")
        if phase not in order or current not in order or order[phase] < order[current]:
            raise ValueError(f"invalid transaction transition {current!r} -> {phase!r}")
        if "policy_context" in facts:
            replacement = _copy_policy_context(facts.pop("policy_context"))
            if not _same_json_value(
                self.pending_transaction.get("policy_context"), replacement
            ):
                raise RuntimeError("action transaction policy_context is immutable")
        self.pending_transaction.update(facts)
        self.pending_transaction["phase"] = phase
        self.pending_transaction["updated_at"] = time.time()

    def clear_action_transaction(self) -> None:
        self.pending_transaction = {}

    def prune_action_transactions(self, history_limit: int) -> None:
        """Bound terminal history without dropping an undelivered outbox entry."""
        history_start = max(
            0,
            len(self.action_transactions) - max(1, int(history_limit)),
        )
        self.action_transactions = [
            item
            for index, item in enumerate(self.action_transactions)
            if index >= history_start
            or (
                (item.get("registry_delivery") or {}).get("required")
                and not (item.get("registry_delivery") or {}).get("persisted")
            )
        ]

    def archive_action_transaction(
        self,
        *,
        terminal_status: str,
        terminal_reason: str,
        outcome_proof: Optional[dict],
        registry_required: bool,
        history_limit: int,
    ) -> dict:
        """Move the active transaction into its immutable terminal archive."""
        allowed = {
            "committed",
            "illegal_candidates",
            "no_candidates",
            "execution_failed",
            "measurement_failed",
            "timed_out",
            "interrupted",
        }
        if terminal_status not in allowed:
            raise ValueError(f"unknown terminal action status {terminal_status!r}")
        if not self.pending_transaction:
            raise RuntimeError("no action transaction is pending")
        transaction = copy.deepcopy(self.pending_transaction)
        proof = copy.deepcopy(outcome_proof or {})
        completed_at = time.time()
        execution_started = transaction.get("execution_started")
        if int(transaction.get("version", 1)) >= 5:
            execution_started = (
                execution_started if isinstance(execution_started, bool) else None
            )
        elif not isinstance(execution_started, bool):
            execution_started = transaction.get("phase") != "prepared"
        archived = {
            "version": 1,
            "transaction_id": str(transaction.get("transaction_id") or ""),
            "phase": terminal_status,
            "terminal_status": terminal_status,
            "terminal_reason": str(terminal_reason),
            "created_at": transaction.get("created_at"),
            "completed_at": completed_at,
            "created_round": transaction.get("created_round"),
            "seed_candidate_id": transaction.get("seed_candidate_id"),
            "candidate_ids": list(transaction.get("candidate_ids") or []),
            "skill": transaction.get("skill"),
            "target": transaction.get("target"),
            "params": copy.deepcopy(transaction.get("params") or {}),
            "action_fingerprint": transaction.get("action_fingerprint"),
            "action_proof_sha256": str(
                transaction.get("action_proof_sha256")
                or (transaction.get("action_proof") or {}).get("proof_sha256")
                or ""
            ),
            "review_id": str(transaction.get("review_id") or ""),
            "execution": {
                "action_executed": execution_started,
                "action_started_at": transaction.get("action_started_at"),
                "action_runtime_s": transaction.get("action_runtime_s"),
                "result_summary": transaction.get("result_summary"),
            },
            "failure_diagnostic": copy.deepcopy(
                transaction.get("failure_diagnostic") or {}
            ),
            "outcome_proof": proof,
            "outcome_proof_status": (
                "available" if proof else "unavailable_legacy_transaction"
            ),
            "registry_delivery": {
                "required": bool(registry_required and proof),
                "persisted": False,
                "record_id": proof.get("record_id"),
                "attempts": 0,
                "last_error": "",
            },
        }
        if "policy_context" in transaction:
            archived["policy_context"] = _copy_policy_context(
                transaction["policy_context"]
            )
        self.action_transactions.append(archived)
        self.prune_action_transactions(history_limit)
        self.pending_transaction = {}
        return archived

    def record_action_evidence_delivery(
        self,
        transaction_id: str,
        *,
        persisted: bool,
        error: str = "",
    ) -> None:
        for transaction in reversed(self.action_transactions):
            if transaction.get("transaction_id") != transaction_id:
                continue
            delivery = transaction.setdefault("registry_delivery", {})
            delivery["attempts"] = int(delivery.get("attempts", 0)) + 1
            delivery["persisted"] = bool(persisted)
            delivery["last_error"] = str(error)[:500]
            delivery["last_attempt_at"] = time.time()
            if persisted:
                delivery["persisted_at"] = time.time()
            return
        raise KeyError(f"unknown terminal action transaction {transaction_id!r}")

    def finalize_score_calibration(
        self,
        *,
        status: str,
        candidate_count: int,
        legal_candidate_count: int,
        promoted_candidate_ids: list[str],
        history_limit: int,
        candidate_outcomes: Optional[list[dict]] = None,
    ) -> None:
        pending = self.pending_score_calibration
        if not pending:
            return
        self.refresh_budget()
        estimate = pending["estimate"]
        actual_gain = max(
            0.0,
            float(self.current_best["fmax"])
            - float(pending["global_best_before_fmax_mhz"]),
        )
        runtime = max(
            0.0,
            float(self.budget["time_elapsed"])
            - float(pending["started_elapsed_runtime_s"]),
        )
        gain_quantiles = estimate["gain_vs_global_best_mhz"]
        runtime_quantiles = estimate["action_cycle_runtime_s"]
        record = dict(pending)
        record["actual"] = {
            "status": str(status),
            "candidate_count": int(candidate_count),
            "legal_candidate_count": int(legal_candidate_count),
            "promoted_candidate_ids": list(promoted_candidate_ids),
            "incremental_global_best_gain_mhz": actual_gain,
            "action_cycle_runtime_s": runtime,
        }
        record["errors"] = {
            "gain_p50_error_mhz": actual_gain - float(gain_quantiles["p50"]),
            "runtime_p50_error_s": runtime - float(runtime_quantiles["p50"]),
        }
        record["coverage"] = {
            "gain_inside_p10_p90": (
                float(gain_quantiles["p10"]) <= actual_gain
                <= float(gain_quantiles["p90"])
            ),
            "runtime_inside_p10_p90": (
                float(runtime_quantiles["p10"]) <= runtime
                <= float(runtime_quantiles["p90"])
            ),
        }
        outcomes = list(candidate_outcomes or [])
        legal = [item for item in outcomes if item.get("legal") is True]
        global_before = float(pending["global_best_before_fmax_mhz"])
        regressions = sum(
            _fmax is not None and _fmax < global_before
            for item in legal
            if (_fmax := item.get("fmax_mhz")) is not None
        )
        observed_risk = {
            "candidate_regression": bool(legal) and regressions == len(legal),
            "candidate_regression_rate": (
                regressions / len(legal) if legal else None
            ),
            "no_legal_candidate": (
                False
                if legal
                else True
                if not outcomes or all(item.get("legal") is False for item in outcomes)
                else None
            ),
            "timeout": "timeout" in str(status).lower(),
            "candidate_outcome_count": len(outcomes),
        }
        record["observed_action_risk"] = observed_risk
        predicted_risk = estimate.get("action_risk") or {}
        risk_fields = {
            "candidate_regression_probability",
            "no_legal_candidate_probability",
            "timeout_probability",
        }
        if risk_fields <= predicted_risk.keys():
            risk_errors = {
                "candidate_regression_probability_error": (
                    float(observed_risk["candidate_regression"])
                    - float(predicted_risk["candidate_regression_probability"])
                ),
                "timeout_probability_error": (
                    float(observed_risk["timeout"])
                    - float(predicted_risk["timeout_probability"])
                ),
            }
            if observed_risk["no_legal_candidate"] is not None:
                risk_errors["no_legal_candidate_probability_error"] = (
                    float(observed_risk["no_legal_candidate"])
                    - float(predicted_risk["no_legal_candidate_probability"])
                )
            record["risk_errors"] = risk_errors
        record["completed_at"] = time.time()
        record.pop("status", None)
        self.score_calibration_history.append(record)
        self.score_calibration_history = self.score_calibration_history[-history_limit:]
        self.pending_score_calibration = {}

    # ------------------------------------------------------------------
    # Round recording
    # ------------------------------------------------------------------
    def record(
        self,
        skill: str,
        target: str,
        before_wns: float,
        after_wns: float,
        accepted: bool,
        params: dict,
        summary: str = "",
        expectation: str = "",
        veto_reason: str = "",
        action_fingerprint: str = "",
        seed_round: Optional[int] = None,
        action_executed: bool = False,
        seed_candidate_id: Optional[str] = None,
        produced_candidate_id: Optional[str] = None,
        candidate_created: bool = False,
        candidate_legal: Optional[bool] = False,
        in_beam: bool = False,
        strategy_family: str = "",
        seed_fmax_mhz: Optional[float] = None,
        measured_fmax_mhz: Optional[float] = None,
        global_best_before_fmax_mhz: Optional[float] = None,
        action_runtime_s: Optional[float] = None,
        validation_runtime_s: Optional[float] = None,
        candidate_ordinal: Optional[int] = None,
        candidate_count: Optional[int] = None,
        advance_round: bool = True,
    ) -> None:
        if advance_round:
            self.round += 1
        elif not action_executed:
            # A selected action that is recorded without executing and without
            # advancing the round sends the round loop straight back to the
            # decision stage, and several of those branches say nothing at all.
            # vtr_mcml_v2 selected its consumer-driven rewrite on round 1,
            # never reached the authoritative-seed open, and was re-deciding
            # eleven seconds later -- with no veto, no binding error and no
            # line in the run log naming what dropped it.
            _LOGGER.warning(
                "[memory] %s/%s selected but not executed, round held: %s",
                skill or "?", target or "-", summary or "no summary recorded",
            )
        delta_fmax_mhz = (
            float(measured_fmax_mhz) - float(seed_fmax_mhz)
            if measured_fmax_mhz is not None and seed_fmax_mhz is not None
            else None
        )
        delta_vs_global_best_mhz = (
            float(measured_fmax_mhz) - float(global_best_before_fmax_mhz)
            if measured_fmax_mhz is not None
            and global_best_before_fmax_mhz is not None
            else None
        )
        self.history.append({
            "round": self.round,
            "skill": skill,
            "target": target,
            "before_wns": before_wns,
            "after_wns": after_wns,
            "delta_wns": after_wns - before_wns,
            "accepted": accepted,
            "params": params,
            "summary": summary,
            "expectation": expectation,
            "veto_reason": veto_reason,
            "action_fingerprint": action_fingerprint,
            "seed_round": seed_round,
            "action_executed": action_executed,
            "seed_candidate_id": seed_candidate_id,
            "produced_candidate_id": produced_candidate_id,
            "candidate_created": bool(candidate_created),
            "candidate_legal": (
                candidate_legal if isinstance(candidate_legal, bool) else None
            ),
            "in_beam": bool(in_beam),
            "strategy_family": strategy_family or resolve_strategy_family(skill),
            "seed_fmax_mhz": seed_fmax_mhz,
            "measured_fmax_mhz": measured_fmax_mhz,
            "delta_fmax_mhz": delta_fmax_mhz,
            "global_best_before_fmax_mhz": global_best_before_fmax_mhz,
            "delta_vs_global_best_mhz": delta_vs_global_best_mhz,
            "action_runtime_s": action_runtime_s,
            "validation_runtime_s": validation_runtime_s,
            "candidate_ordinal": candidate_ordinal,
            "candidate_count": candidate_count,
            "timestamp": time.time(),
        })

    def has_executed_fingerprint(
        self, fingerprint: str, seed_candidate_id: Optional[str] = None
    ) -> bool:
        """Whether this physical action already ran from the selected seed."""
        if seed_candidate_id is not None:
            return any(
                row.get("action_fingerprint") == fingerprint
                and row.get("seed_candidate_id") == seed_candidate_id
                and bool(row.get("action_executed"))
                for row in self.history
            )
        # Compatibility for memories created before the candidate graph.
        seed_round = int(self.current_best.get("round", 0))
        return any(
            row.get("action_fingerprint") == fingerprint
            and row.get("seed_round") == seed_round
            and bool(row.get("action_executed"))
            for row in self.history
        )

    # ------------------------------------------------------------------
    # Candidate graph
    # ------------------------------------------------------------------
    def initialize_candidate_graph(self, root_node: dict, beam_width: int) -> None:
        self.candidate_graph = graph_ops.initialize_graph(root_node, beam_width)
        self.project_graph_best()

    def candidate_node(self, candidate_id: str) -> Optional[dict]:
        return (self.candidate_graph.get("nodes") or {}).get(candidate_id)

    def validate_selectable_seed(
        self, seed_candidate_id: Optional[str] = None
    ) -> tuple[Optional[dict], str]:
        """Resolve omitted seed to global best and validate active artifact state."""
        if not self.candidate_graph:
            return None, "candidate graph is not initialized"
        candidate_id = seed_candidate_id or self.candidate_graph.get(
            "current_best_candidate_id"
        )
        node = self.candidate_node(str(candidate_id)) if candidate_id else None
        if node is None:
            return None, f"unknown seed_candidate_id {candidate_id!r}"
        if node.get("artifact_state") != "materialized":
            return None, (
                f"seed candidate {candidate_id} is {node.get('artifact_state', 'invalid')}"
            )
        if not node.get("eligible_for_beam"):
            return None, f"seed candidate {candidate_id} is not beam eligible"
        in_beam = bool(
            node.get("in_active_beam")
            and candidate_id in self.candidate_graph.get("beam_candidate_ids", [])
        )
        in_enabling_pool = graph_ops.is_active_enabling_candidate(
            self.candidate_graph, str(candidate_id)
        )
        if not in_beam and not in_enabling_pool:
            return None, (
                f"seed candidate {candidate_id} is neither in the active beam "
                "nor the active enabling pool"
            )
        artifact = Path(str(node.get("artifact_path", "")))
        if not artifact.is_file():
            node["artifact_state"] = "missing"
            node["search_state"] = "ineligible"
            node["eligible_for_beam"] = False
            node["in_active_beam"] = False
            self.candidate_graph["beam_candidate_ids"] = [
                value
                for value in self.candidate_graph.get("beam_candidate_ids", [])
                if value != candidate_id
            ]
            graph_ops.retire_enabling_candidate(
                self.candidate_graph,
                str(candidate_id),
                reason="artifact_missing",
                retired_by=None,
            )
            return None, f"seed candidate {candidate_id} artifact is missing"
        return node, ""

    def select_candidate(self, seed_candidate_id: Optional[str] = None) -> dict:
        node, reason = self.validate_selectable_seed(seed_candidate_id)
        if node is None:
            raise CandidateSelectionError(reason)
        return node

    def allocate_candidate_id(self) -> str:
        return graph_ops.allocate_candidate_id(self.candidate_graph)

    def record_candidate_node(self, node: dict) -> None:
        graph_ops.add_node(self.candidate_graph, node)

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        finalizing_transaction: Optional[dict] = None,
    ) -> None:
        prior_menu = self.current_telemetry.get("authorized_action_menu")
        invalidation = None
        if prior_menu:
            from .action_menu import invalidate_action_menu

            node = self.candidate_node(candidate_id) or {}
            kwargs = {}
            if finalizing_transaction is not None:
                authorization = (
                    (finalizing_transaction.get("policy_context") or {}).get(
                        "launch_authorization"
                    )
                )
                if (
                    isinstance(authorization, dict)
                    and authorization.get("kind")
                    == "action_menu_launch_authorization"
                ):
                    pending_id = str(
                        (self.pending_transaction or {}).get("transaction_id") or ""
                    )
                    transaction_id = str(
                        finalizing_transaction.get("transaction_id") or ""
                    )
                    if (
                        not pending_id
                        or transaction_id != pending_id
                        or finalizing_transaction.get("phase") != "measured"
                    ):
                        raise ValueError(
                            "promotion terminal menu exemption is not the active "
                            "measured transaction"
                        )
                    kwargs = {
                        "finalizing_transaction_id": transaction_id,
                        "launch_authorization": authorization,
                    }
            invalidation = invalidate_action_menu(
                prior_menu,
                promoted_candidate_sha256=str(node.get("artifact_sha256") or ""),
                **kwargs,
            )
        graph_ops.promote(self.candidate_graph, candidate_id)
        self.project_graph_best()
        if invalidation is not None:
            self.current_telemetry["action_menu_invalidation"] = invalidation
            self.current_telemetry.pop("authorized_action_menu", None)
            self.current_telemetry.pop("authorized_action_menu_unprojected", None)

    def retain_enabling_candidate(self, candidate_id: str, **evidence) -> dict:
        return graph_ops.retain_enabling_candidate(
            self.candidate_graph, candidate_id, **evidence
        )

    def composition_seed_proposals(self) -> list[dict]:
        return graph_ops.composition_seed_proposals(self.candidate_graph)

    def update_candidate_beam(self, pruned_round: int) -> list[str]:
        return graph_ops.prune_beam(self.candidate_graph, pruned_round)

    def consume_candidate_continuation(
        self, candidate_id: str, *, kind: str, consumed_round: Optional[int] = None
    ) -> bool:
        return graph_ops.consume_continuation(
            self.candidate_graph,
            candidate_id,
            consumed_round=self.round if consumed_round is None else consumed_round,
            kind=kind,
        )

    def project_graph_best(self) -> None:
        """Project graph truth into the legacy ``current_best`` dictionary."""
        if not self.candidate_graph:
            return
        candidate_id = self.candidate_graph.get("current_best_candidate_id")
        node = self.candidate_node(candidate_id)
        if node is None:
            return
        self.current_best = {
            "candidate_id": candidate_id,
            "wns": node.get("wns_ns"),
            "fmax": node.get("fmax_mhz"),
            "dcp_path": node.get("artifact_path"),
            "round": node.get("created_round", 0),
            "clock_period": node.get("clock_period_ns"),
        }

    def record_failure(self, skill: str, target: str, reason: str) -> None:
        self.failed_attempts.append({
            "round": self.round,
            "skill": skill,
            "target": target,
            "reason": reason[:300],
        })

    def record_telemetry_epoch(
        self,
        *,
        epoch: int,
        source_round: int,
        dcp_path: str,
        analysis: dict,
        features: dict,
        matched_cases: list,
        candidate_id: Optional[str] = None,
        collected_at: Optional[float] = None,
        report_snapshot: Optional[dict] = None,
        append_history: bool = True,
    ) -> None:
        """Replace current telemetry and append one bounded compact snapshot."""
        collected_at = time.time() if collected_at is None else collected_at
        normalized_matches = []
        for match in matched_cases:
            if isinstance(match, dict):
                case_id = match.get("case_id") or match.get("id")
                score = match.get("match_score", match.get("score"))
            else:
                case = getattr(match, "case", None)
                case_id = getattr(case, "id", None)
                score = getattr(match, "score", None)
            if case_id is not None:
                normalized_matches.append({
                    "case_id": str(case_id),
                    "match_score": round(float(score or 0.0), 3),
                })

        timing_summary = dict(analysis.get("timing_summary") or {})
        timing_profile = dict(analysis.get("timing_profile") or {})
        utilization = dict(analysis.get("utilization") or {})
        congestion = dict(analysis.get("congestion") or {})
        replica_population = dict(analysis.get("replica_population") or {})
        carry_clouds = dict(analysis.get("carry_clouds") or {})
        hard_macro_cascades = dict(analysis.get("hard_macro_cascades") or {})
        telemetry_availability = dict(
            analysis.get("_telemetry_availability") or {}
        )
        replay_semantic_evidence = dict(
            analysis.get("_replay_semantic_evidence") or {}
        )
        registered_arithmetic_authorizations = list(
            analysis.get("_registered_arithmetic_authorizations") or []
        )
        if len(registered_arithmetic_authorizations) > 64 or any(
            not isinstance(item, dict)
            for item in registered_arithmetic_authorizations
        ):
            registered_arithmetic_authorizations = []
        from .action_menu import validate_action_menu

        authorized_action_menu = validate_action_menu(
            analysis.get("_authorized_action_menu")
        )
        registered_arithmetic_packet = analysis.get(
            "_registered_arithmetic_decision_packet"
        )
        registered_arithmetic_packet = (
            copy.deepcopy(registered_arithmetic_packet)
            if isinstance(registered_arithmetic_packet, dict) else None
        )
        concurrent_proof_packet = analysis.get("_concurrent_proof_decision_packet")
        concurrent_proof_packet = (
            copy.deepcopy(concurrent_proof_packet)
            if isinstance(concurrent_proof_packet, dict) else None
        )
        action_menu_timing = analysis.get("_action_menu_timing")
        if not isinstance(action_menu_timing, dict):
            raise ValueError("telemetry epoch requires action-menu timing")
        action_menu_timing = copy.deepcopy(action_menu_timing)
        action_menu_build_status = analysis.get("_action_menu_build_status")
        if not isinstance(action_menu_build_status, dict):
            raise ValueError("telemetry epoch requires action-menu build status")
        action_menu_build_status = copy.deepcopy(action_menu_build_status)
        if action_menu_build_status is not None:
            action_menu_build_status["retry_not_before_round"] = (
                int(source_round) + 1
                if action_menu_build_status.get("state") == "partial"
                else int(source_round)
            )
        profile_runtime_s = analysis.get("_profile_runtime_s")
        if (
            isinstance(profile_runtime_s, bool)
            or not isinstance(profile_runtime_s, (int, float))
            or not math.isfinite(float(profile_runtime_s))
            or float(profile_runtime_s) < 0.0
        ):
            profile_runtime_s = None
        fanout_nets = list(analysis.get("fanout_nets") or [])
        current = {
            "epoch": int(epoch),
            "source_round": int(source_round),
            "candidate_id": candidate_id,
            "dcp_path": str(dcp_path),
            "collected_at": float(collected_at),
            "clock_period": analysis.get("clock_period"),
            "wns": analysis.get("baseline_wns"),
            "timing_summary": timing_summary,
            "fanout_nets": fanout_nets,
            "timing_profile": timing_profile,
            "design_features": dict(features or {}),
            "utilization": utilization,
            "congestion": congestion,
            "replica_population": replica_population,
            "carry_clouds": carry_clouds,
            "hard_macro_cascades": hard_macro_cascades,
            "telemetry_availability": telemetry_availability,
            "replay_semantic_evidence": replay_semantic_evidence,
            "registered_arithmetic_authorizations": (
                registered_arithmetic_authorizations
            ),
            "registered_arithmetic_decision_packet": registered_arithmetic_packet,
            "concurrent_proof_decision_packet": concurrent_proof_packet,
            "authorized_action_menu": authorized_action_menu,
            "action_menu_timing": action_menu_timing,
            "action_menu_build_status": action_menu_build_status,
            "profile_runtime_s": (
                float(profile_runtime_s) if profile_runtime_s is not None else None
            ),
            "matched_cases": normalized_matches,
            "report_snapshot": dict(report_snapshot or {}),
        }
        self.current_telemetry = current

        profile_summary = {
            key: timing_profile.get(key)
            for key in (
                "path_count",
                "worst_slack_ns",
                "avg_route_pct",
                "route_dominated_paths",
                "dominant_bottleneck",
                "logic_levels",
                "placement_spread",
            )
            if timing_profile.get(key) is not None
        }
        feature_summary = {
            key: features.get(key)
            for key in (
                "wns_ns",
                "clock_period_ns",
                "closure_ratio",
                "lut_util_pct",
                "hard_macro_present",
                "avg_route_pct",
                "route_dominated_ratio",
                "max_logic_levels",
                "critical_lut_stage_ratio",
                "critical_lut_route_coverage",
                "dominant_bottleneck",
                "spread_ratio",
                "max_fanout",
                "max_actionable_fanout",
                "fanout_live_wall_coverage",
                "max_single_net_live_wall_coverage",
                "eligible_hf_count",
            )
            if features.get(key) is not None
        }
        carry_summary = {
            key: carry_clouds.get(key)
            for key in (
                "status", "read_only", "carry_cell_count", "analyzed_cell_count",
                "truncated", "chain_count", "max_chain_length", "cloud_summary",
            )
            if carry_clouds.get(key) is not None
        }
        macro_summary = {
            key: hard_macro_cascades.get(key)
            for key in (
                "status", "read_only", "hard_macro_count", "analyzed_macro_count",
                "truncated", "family_counts", "cascade_count", "max_cascade_length",
            )
            if hard_macro_cascades.get(key) is not None
        }
        if append_history:
            self.telemetry_epochs.append({
                "epoch": int(epoch),
                "source_round": int(source_round),
                "candidate_id": candidate_id,
                "dcp_path": str(dcp_path),
                "collected_at": float(collected_at),
                "clock_period": analysis.get("clock_period"),
                "timing_summary": timing_summary,
                "timing_profile": profile_summary,
                "design_features": feature_summary,
                "utilization": utilization,
                "congestion": congestion,
                "replica_population": replica_population,
                "carry_clouds": carry_summary,
                "hard_macro_cascades": macro_summary,
                "matched_cases": normalized_matches,
                "profile_runtime_s": (
                    float(profile_runtime_s)
                    if profile_runtime_s is not None else None
                ),
                "report_snapshot": dict(report_snapshot or {}),
            })
            self.telemetry_epochs = self.telemetry_epochs[
                -self.TELEMETRY_HISTORY_WINDOW:
            ]
        if report_snapshot:
            self.record_report_snapshot(report_snapshot)

    def next_telemetry_epoch(self) -> int:
        epochs = [
            int(item.get("epoch", -1))
            for item in self.telemetry_epochs
            if item.get("epoch") is not None
        ]
        if self.current_telemetry.get("epoch") is not None:
            epochs.append(int(self.current_telemetry["epoch"]))
        return max(epochs, default=-1) + 1

    def record_report_snapshot(self, report_snapshot: dict) -> None:
        snapshot_id = report_snapshot.get("snapshot_id")
        self.report_snapshots = [
            item
            for item in self.report_snapshots
            if item.get("snapshot_id") != snapshot_id
        ]
        self.report_snapshots.append(dict(report_snapshot))
        self.report_snapshots = self.report_snapshots[-self.TELEMETRY_HISTORY_WINDOW:]

    def record_telemetry_failure(
        self,
        *,
        epoch: int,
        source_round: int,
        dcp_path: str,
        reason: str,
        candidate_id: Optional[str] = None,
        collected_at: Optional[float] = None,
    ) -> None:
        self.telemetry_failures.append({
            "epoch": int(epoch),
            "source_round": int(source_round),
            "candidate_id": candidate_id,
            "dcp_path": str(dcp_path),
            "timestamp": time.time() if collected_at is None else collected_at,
            "reason": str(reason)[:300],
        })
        self.telemetry_failures = self.telemetry_failures[-self.TELEMETRY_HISTORY_WINDOW:]

    def add_note(self, note: str) -> None:
        note = note.strip()
        if note:
            self.notes.append(note[:500])
            self.notes = self.notes[-20:]

    def record_reasoning_cycle(self, decision) -> None:
        """Persist staged grep evidence and the terminal typed action."""
        trace = getattr(decision, "react_transcript", None) or {}
        report_analysis = getattr(decision, "report_analysis", None) or {}
        if not trace and not report_analysis:
            return

        def redact(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    lowered = str(key).lower()
                    if any(word in lowered for word in ("secret", "token", "password", "api_key")):
                        result[str(key)] = "<redacted>"
                    elif lowered in {"input_dcp", "output_dcp", "dcp_path", "artifact_path"}:
                        result[str(key)] = "<mechanically-owned-path>"
                    else:
                        result[str(key)] = redact(item)
                return result
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        terminal_action = {
            "action": getattr(decision, "action", "invalid"),
            "skill": getattr(decision, "skill", ""),
            "target": getattr(decision, "target", ""),
            "seed_candidate_id": getattr(decision, "seed_candidate_id", None),
            "params": redact(getattr(decision, "params", {}) or {}),
            "expectation": getattr(decision, "expectation", ""),
            "score_estimate": redact(
                getattr(decision, "score_estimate", {}) or {}
            ),
            "server": getattr(decision, "server", ""),
            "tool": getattr(decision, "tool", ""),
            "arguments": redact(getattr(decision, "arguments", {}) or {}),
            "question": getattr(decision, "question", ""),
            "stop_reason": getattr(decision, "stop_reason", ""),
            "stop_rationale": getattr(decision, "stop_rationale", ""),
            "selected_entry_id": getattr(decision, "selected_entry_id", ""),
            "action_menu_digest": getattr(decision, "action_menu_digest", ""),
            "action_menu_seed_sha256": getattr(
                decision, "action_menu_seed_sha256", ""
            ),
        }
        self.reasoning_cycles.append({
            "round": self.round,
            "timestamp": time.time(),
            "report_snapshot_id": getattr(decision, "report_snapshot_id", ""),
            "knowledge_snapshot_id": getattr(
                decision, "knowledge_snapshot_id", ""
            ),
            "report_analysis": dict(report_analysis),
            "evidence_refs": list(getattr(decision, "evidence_refs", []) or []),
            "terminal_action": terminal_action,
            "error": redact((getattr(decision, "raw", {}) or {}).get("error")),
            "transcript": trace,
        })
        self.reasoning_cycles = self.reasoning_cycles[-32:]

    def record_decision_review(
        self,
        *,
        provisional_action: dict,
        blind_score_estimate: dict,
        action_proof: dict,
        review,
    ) -> str:
        """Persist neutral final review without consuming an action round."""
        review_id = (
            f"r{len(self.decision_reviews) + 1:06d}-"
            f"{str(action_proof.get('proof_sha256') or '')[:12]}"
        )
        event = {
            "version": 2,
            "review_id": review_id,
            "round": self.round,
            "timestamp": time.time(),
            "provisional_action": provisional_action,
            "blind_score_estimate": blind_score_estimate,
            "action_proof": action_proof,
            "shadow_bo_advisory": {},
            "final_review": {
                "action": getattr(review, "action", "invalid"),
                "assessment": getattr(review, "assessment", ""),
                "reason": getattr(review, "reason", ""),
            },
        }
        self.decision_reviews.append(event)
        self.decision_reviews = self.decision_reviews[-32:]
        return review_id

    def record_shadow_bo(self, review_id: str, advisory: dict) -> None:
        """Attach predictor output after the LLM decision is irrevocably fixed."""
        for event in reversed(self.decision_reviews):
            if event.get("review_id") == review_id:
                event["shadow_bo_advisory"] = dict(advisory or {})
                event["shadow_bo_recorded_at"] = time.time()
                return
        raise KeyError(f"unknown decision review {review_id}")

    def record_seed_reanalysis(
        self,
        *,
        decision,
        selected_seed_candidate_id: str,
        reason: str,
        status: str,
    ) -> None:
        """Attach the mechanical disposition of a stale-report action."""
        event = {
            "status": str(status),
            "reason": str(reason)[:300],
            "action_discarded": True,
            "selected_seed_candidate_id": str(selected_seed_candidate_id),
            "report_snapshot_id": getattr(decision, "report_snapshot_id", ""),
            "timestamp": time.time(),
        }
        if self.reasoning_cycles:
            cycle = self.reasoning_cycles[-1]
            if cycle.get("report_snapshot_id") == event["report_snapshot_id"]:
                cycle["reanalysis"] = event
                return
        self.reasoning_cycles.append({
            "round": self.round,
            "timestamp": event["timestamp"],
            "event": "seed_bound_reanalysis",
            "reanalysis": event,
        })
        self.reasoning_cycles = self.reasoning_cycles[-32:]

    def update_best(
        self,
        wns: float,
        fmax: Optional[float],
        dcp_path: str,
        clock_period: Optional[float] = None,
    ) -> None:
        if self.candidate_graph:
            # Once initialized, graph promotion is the sole best-selection path.
            self.project_graph_best()
            return
        entry = {
            "wns": wns,
            "fmax": fmax,
            "dcp_path": dcp_path,
            "round": self.round,
            "clock_period": (
                clock_period
                if clock_period is not None
                else self.current_clock_period()
            ),
        }
        prev_fmax = self.current_best.get("fmax")
        if prev_fmax is not None and fmax is not None:
            # Fmax is period-independent — the correct cross-clock criterion.
            if fmax > prev_fmax:
                self.current_best = entry
        elif wns > self.current_best["wns"]:
            self.current_best = entry

    # ------------------------------------------------------------------
    # Probe (custom MCP call) bookkeeping
    # ------------------------------------------------------------------
    def record_custom_response(
        self,
        server: str,
        tool: str,
        arguments: dict,
        response: str,
        description: str = "",
        seed_candidate_id: Optional[str] = None,
        artifact_sha256: Optional[str] = None,
    ) -> None:
        self.custom_calls_used += 1
        self.probe_count += 1
        self.last_custom_responses.append({
            "round": self.round,
            "server": server,
            "tool": tool,
            "arguments": arguments,
            "description": description,
            "seed_candidate_id": seed_candidate_id,
            "artifact_sha256": artifact_sha256,
            "status": "success",
            "response": response,
        })
        if len(self.last_custom_responses) > self.CUSTOM_RESPONSE_WINDOW:
            self.last_custom_responses = self.last_custom_responses[
                -self.CUSTOM_RESPONSE_WINDOW:
            ]
        self._record_exhausted_probe(server, tool, arguments, artifact_sha256)

    def record_failed_probe(
        self,
        *,
        server: str,
        tool: str,
        arguments: dict,
        error: str,
        description: str = "",
        seed_candidate_id: Optional[str] = None,
        artifact_sha256: Optional[str] = None,
    ) -> None:
        """Retire one exact failed observation on one unchanged artifact.

        A schema error or deterministic tool refusal cannot become informative
        merely by asking for the identical read-only observation again.
        """
        self.custom_calls_used += 1
        self.probe_count += 1
        # Failed probes are disposition evidence, not observations. Keep the
        # response ring free of stale/partial payloads while still retiring the
        # exact deterministic request below.
        self._record_exhausted_probe(server, tool, arguments, artifact_sha256)

    # ------------------------------------------------------------------
    # Exhausted read-only probes
    # ------------------------------------------------------------------
    @staticmethod
    def probe_observation_key(
        server: object,
        tool: object,
        arguments: object,
        artifact_sha256: object,
    ) -> str:
        """Identity of one read-only observation of one exact artifact.

        A read-only probe of an unchanged artifact returns the same data, so
        repeating this key cannot yield new information.  Arguments are hashed
        rather than stored so no design identifier enters the ledger.
        """
        try:
            argument_json = json.dumps(
                arguments if isinstance(arguments, dict) else {},
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            argument_json = "{}"
        payload = "\u0000".join([
            str(server or ""),
            str(tool or ""),
            argument_json,
            str(artifact_sha256 or ""),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _record_exhausted_probe(
        self,
        server: object,
        tool: object,
        arguments: object,
        artifact_sha256: object,
    ) -> None:
        key = self.probe_observation_key(server, tool, arguments, artifact_sha256)
        if key in self.exhausted_probes:
            return
        self.exhausted_probes[key] = {
            "server": str(server or ""),
            "tool": str(tool or ""),
            "artifact_sha256": str(artifact_sha256 or ""),
            "round": int(self.round),
        }
        if len(self.exhausted_probes) > self.EXHAUSTED_PROBE_WINDOW:
            for stale in list(self.exhausted_probes)[
                : len(self.exhausted_probes) - self.EXHAUSTED_PROBE_WINDOW
            ]:
                self.exhausted_probes.pop(stale, None)

    def probe_observation_exhausted(
        self,
        server: object,
        tool: object,
        arguments: object,
        artifact_sha256: object,
    ) -> bool:
        """Whether this exact observation of this exact artifact already ran."""
        return (
            self.probe_observation_key(server, tool, arguments, artifact_sha256)
            in self.exhausted_probes
        )

    def exhausted_probe_summary(
        self, artifact_sha256: Optional[str] = None
    ) -> list[dict]:
        """Bounded, identifier-free observations that cannot repay their cost.

        Scoped to one exact artifact: once a candidate is promoted the design
        changed, so the same observation becomes informative again.
        """
        scope = str(artifact_sha256 or "")
        return [
            {
                "server": entry["server"],
                "tool": entry["tool"],
                "first_observed_round": entry["round"],
            }
            for entry in list(self.exhausted_probes.values())[
                -self.EXHAUSTED_PROBE_WINDOW:
            ]
            if str(entry.get("artifact_sha256") or "") == scope
        ]

    def current_incumbent_artifact_sha256(self) -> str:
        """SHA-256 of the artifact the next decision will observe."""
        candidate_id = self.candidate_graph.get("current_best_candidate_id")
        node = self.candidate_node(str(candidate_id)) if candidate_id else None
        if not isinstance(node, dict):
            return ""
        return str(node.get("artifact_sha256") or "")

    # ------------------------------------------------------------------
    # LLM-facing state
    # ------------------------------------------------------------------
    def current_clock_period(self) -> Optional[float]:
        """Clock period of the current best checkpoint (clock_tighten changes it)."""
        period = self.current_best.get("clock_period")
        if period is None:
            period = self.baseline.get("clock_period")
        try:
            return float(period) if period is not None else None
        except (TypeError, ValueError):
            return None

    def fmax_gain_mhz(self) -> Optional[float]:
        f0 = self.baseline.get("fmax") or calculate_fmax(
            self.baseline.get("wns"), self.baseline.get("clock_period")
        )
        f1 = self.current_best.get("fmax") or calculate_fmax(
            self.current_best.get("wns"), self.current_clock_period()
        )
        if f0 is None or f1 is None:
            return None
        return round(f1 - f0, 3)

    def contest_score_snapshot(self) -> Optional[dict]:
        """Projected stop-now score, assuming final validation later passes."""
        self.refresh_budget()
        baseline_fmax = self.baseline.get("fmax")
        if baseline_fmax is None:
            baseline_fmax = calculate_fmax(
                self.baseline.get("wns"), self.baseline.get("clock_period")
            )
        best_fmax = self.current_best.get("fmax")
        if best_fmax is None:
            best_fmax = calculate_fmax(
                self.current_best.get("wns"), self.current_clock_period()
            )
        if baseline_fmax is None or best_fmax is None:
            return None

        snapshot = score_snapshot(
            baseline_fmax_mhz=baseline_fmax,
            best_fmax_mhz=best_fmax,
            runtime_s=self.budget.get("time_elapsed", 0.0),
            llm_cost_usd=self.budget.get("tokens_spent", 0.0),
        )
        snapshot.pop("validation_passed", None)
        best_node = None
        if self.candidate_graph:
            best_node = self.candidate_node(
                self.candidate_graph.get("current_best_candidate_id")
            )
        snapshot.update({
            "kind": "projected_if_current_best_passes_final_validation",
            "projection_assumes_final_validation": True,
            "final_validation_status": "not_checked_by_score_projection",
            "functional_equivalence_status": (
                (best_node or {}).get("functional_equivalence", "unknown")
            ),
            "contest_runtime_is_global_wall_time": True,
        })
        return snapshot

    def hard_target_snapshot(self) -> Optional[dict]:
        """Return immutable leaderboard conversion plus current measured progress."""
        if not self.hard_target:
            return None
        current = self.current_best.get("fmax")
        if current is None:
            current = calculate_fmax(
                self.current_best.get("wns"), self.current_clock_period()
            )
        if current is None:
            return None
        target = dict(self.hard_target)
        required = float(target["target_fmax_mhz"])
        current = float(current)
        target.update({
            "current_best_fmax_mhz": current,
            "gap_to_target_fmax_mhz": max(0.0, required - current),
            "margin_over_target_fmax_mhz": current - required,
            "target_reached": current > required,
        })
        return target

    def measured_action_feedback(self, *, max_cycles_per_family: int = 6) -> dict:
        """Summarize authoritative action-cycle gain/runtime across seed changes."""
        batches: dict[tuple, list[dict]] = {}
        for row in self.history:
            if not row.get("candidate_created") or row.get("measured_fmax_mhz") is None:
                continue
            key = (
                row.get("round"),
                row.get("strategy_family") or row.get("skill"),
                row.get("seed_candidate_id"),
            )
            batches.setdefault(key, []).append(row)

        families: dict[str, list[dict]] = {}
        for (_round, family, seed_id), outcomes in batches.items():
            legal = [row for row in outcomes if row.get("candidate_legal")]
            measured = legal or outcomes
            winner = max(
                measured,
                key=lambda row: float(row.get("measured_fmax_mhz") or float("-inf")),
            )
            global_before = winner.get("global_best_before_fmax_mhz")
            measured_fmax = float(winner["measured_fmax_mhz"])
            retained_gain = (
                max(0.0, measured_fmax - float(global_before))
                if global_before is not None and legal else 0.0
            )
            runtime = max(
                float(row.get("action_runtime_s") or 0.0) for row in outcomes
            )
            cycle = {
                "round": winner.get("round"),
                "seed_candidate_id": seed_id,
                "skill": winner.get("skill"),
                "target": winner.get("target"),
                "params": winner.get("params") or {},
                "candidate_count": len(outcomes),
                "legal_candidate_count": len(legal),
                "best_measured_fmax_mhz": measured_fmax,
                "retained_gain_vs_global_best_mhz": retained_gain,
                "action_cycle_runtime_s": runtime,
                "retained_mhz_per_1000s": (
                    retained_gain * 1000.0 / runtime if runtime > 0.0 else None
                ),
            }
            families.setdefault(str(family), []).append(cycle)

        target = self.hard_target_snapshot()
        summarized = {}
        for family, cycles in sorted(families.items()):
            cycles.sort(key=lambda item: int(item.get("round") or 0))
            recent = cycles[-max(1, int(max_cycles_per_family)):]
            gains = [item["retained_gain_vs_global_best_mhz"] for item in recent]
            summarized[family] = {
                "cycles_total": len(cycles),
                "recent_cycles": recent,
                "last_retained_gain_mhz": gains[-1],
                "best_retained_gain_mhz": max(gains),
                "strictly_declining_last_three": (
                    len(gains) >= 3 and gains[-3] > gains[-2] > gains[-1]
                ),
                "hard_target_gap_mhz": (
                    target["gap_to_target_fmax_mhz"] if target else None
                ),
            }
        return {
            "version": 1,
            "kind": "authoritative_outer_action_feedback",
            "informational_only": True,
            "sibling_outcomes_are_one_action_cycle": True,
            "families": summarized,
        }

    def state_for_llm(
        self,
        *,
        history_window: int = 8,
        runtime_estimates_s: Optional[dict] = None,
        candidate_runtime_estimates_s: Optional[dict] = None,
        max_skill_candidates_per_action: int = 1,
        central_measurement_estimate_s: float = 0.0,
        candidate_commit_reserve_s: float = 0.0,
        calibration_records_in_llm_state: int = 8,
        time_safety_s: float = 0.0,
        platform_id: str = "unspecified",
        saturation_config: Optional[dict] = None,
    ) -> dict:
        """Everything the LLM needs to decide, nothing gated."""
        self.refresh_budget()
        recent_rounds = []
        for row in reversed(self.history):
            row_round = row.get("round")
            if row_round not in recent_rounds:
                recent_rounds.append(row_round)
            if len(recent_rounds) >= history_window:
                break
        selected_rounds = set(recent_rounds)
        recent = [
            row for row in self.history if row.get("round") in selected_rounds
        ]

        def compact(value, depth: int = 0):
            if depth >= 4:
                return "..."
            if isinstance(value, dict):
                return {
                    str(key): compact(item, depth + 1)
                    for key, item in list(value.items())[:12]
                }
            if isinstance(value, (list, tuple)):
                return [compact(item, depth + 1) for item in value[:6]]
            if isinstance(value, str) and len(value) > 160:
                return value[:157] + "..."
            return value

        def compact_params(value: dict) -> dict:
            # Artifact paths are Python-owned mechanics, not LLM action inputs.
            return compact({
                key: item
                for key, item in value.items()
                if key not in {"input_dcp", "output_dcp", "dcp_path"}
            })

        def compact_profile(value: dict) -> dict:
            keys = (
                "path_count",
                "worst_slack_ns",
                "avg_slack_ns",
                "avg_route_pct",
                "avg_logic_pct",
                "dominant_bottleneck",
                "route_dominated_paths",
                "logic_levels",
                "logic_histogram",
                "top_sources",
                "top_destinations",
                "top_nets",
                "path_samples",
                "placement_spread",
            )
            return {key: compact(value[key]) for key in keys if key in value}

        def compact_features(value: dict) -> dict:
            return {
                str(key): compact(item)
                for key, item in list(value.items())[:32]
            }

        compact_history = [
            {
                "round": r["round"],
                "skill": r["skill"],
                "target": r["target"],
                "delta_wns": round(r["delta_wns"], 4),
                "accepted": r["accepted"],
                "params": compact_params(r.get("params") or {}),
                "summary": (r.get("summary") or "")[:220],
                **(
                    {"action_fingerprint": r["action_fingerprint"]}
                    if r.get("action_fingerprint") else {}
                ),
                **(
                    {"seed_round": r["seed_round"]}
                    if r.get("seed_round") is not None else {}
                ),
                "seed_candidate_id": r.get("seed_candidate_id"),
                "produced_candidate_id": r.get("produced_candidate_id"),
                "candidate_created": bool(r.get("candidate_created", False)),
                "candidate_legal": (
                    r.get("candidate_legal")
                    if isinstance(r.get("candidate_legal"), bool)
                    else None
                ),
                "in_beam": bool(r.get("in_beam", False)),
                "strategy_family": r.get("strategy_family") or r["skill"],
                "delta_fmax_mhz": r.get("delta_fmax_mhz"),
                "action_runtime_s": r.get("action_runtime_s"),
                "validation_runtime_s": r.get("validation_runtime_s"),
                "candidate_ordinal": r.get("candidate_ordinal"),
                "candidate_count": r.get("candidate_count"),
                **({"expectation": r["expectation"][:160]} if r.get("expectation") else {}),
                **({"veto_reason": r["veto_reason"][:160]} if r.get("veto_reason") else {}),
            }
            for r in recent
        ]

        state = {
            "design_id": self.design_id,
            "round": self.round,
            "clock_period_ns": self.baseline.get("clock_period"),
            "current_clock_period_ns": self.current_clock_period(),
            "baseline_wns": self.baseline.get("wns"),
            "baseline_fmax_mhz": self.baseline.get("fmax"),
            "current_wns": self.current_best.get("wns"),
            "current_fmax_mhz": self.current_best.get("fmax"),
            "fmax_gain_mhz": self.fmax_gain_mhz(),
            "bottleneck": (
                (self.current_telemetry.get("design_features") or {}).get(
                    "dominant_bottleneck"
                )
                or self.plan.get("bottleneck")
                or self.baseline.get("bottleneck", "unknown")
            ),
            "history": compact_history,
            "rounds_total": self.round,
            "outcomes_total": len(self.history),
            "failed_attempts": self.failed_attempts[-10:],
            "suggested_targets": {
                k: v[:10] for k, v in self.suggested_targets.items() if v
            },
            "notes": self.notes[-8:],
            "budget": {
                "time_elapsed_s": self.budget["time_elapsed"],
                "time_remaining_s": round(self.budget["time_remaining"], 0),
                "time_limit_s": self.budget.get("time_limit_s"),
                "token_remaining_usd": round(self.budget.get("tokens_remaining", 0.0), 4),
            },
            "probe_calls_used": self.custom_calls_used,
            "task_counts": {
                "probe": self.probe_count,
                "mutation": self.mutation_count,
                "validation": self.validation_count,
            },
            "telemetry_epoch": self.current_telemetry.get("epoch"),
            "telemetry_candidate_id": self.current_telemetry.get("candidate_id"),
        }
        concurrent_packet = self.current_telemetry.get(
            "concurrent_proof_decision_packet"
        )
        if isinstance(concurrent_packet, dict):
            state["concurrent_proof_decision_packet"] = copy.deepcopy(
                concurrent_packet
            )
        execution_window = max(
            0.0, float(self.budget["time_remaining"]) - float(time_safety_s)
        )
        state["execution_platform"] = {
            "id": str(platform_id),
            "source": "configuration",
            "dynamically_verified": False,
        }
        state["run_contract"] = {
            "strategy_decision_owner": "llm",
            "saturation_is_informational": True,
            "score_hurdles_are_informational": True,
            "configured_runtime_fit_is_informational": True,
            "hard_mechanical_deadline_enforced": True,
            "time_limit_s": self.budget.get("time_limit_s"),
            "time_safety_s": float(time_safety_s),
            "execution_window_remaining_s": execution_window,
            "score_projection_can_veto_actions": False,
        }
        completed = self.score_calibration_history
        state["score_calibration"] = {
            "informational_only": True,
            "automatically_adjusts_estimates": False,
            "completed_records": len(completed),
            "recent": completed[-int(calibration_records_in_llm_state):],
        }
        feedback = self.measured_action_feedback()
        feedback["families"] = {
            family: {
                **summary,
                "recent_cycles": [
                    {**cycle, "params": compact_params(cycle.get("params") or {})}
                    for cycle in summary["recent_cycles"]
                ],
            }
            for family, summary in feedback["families"].items()
        }
        state["measured_action_feedback"] = feedback
        score_state = self.contest_score_snapshot()
        if score_state is not None:
            hard_target = self.hard_target_snapshot()
            if hard_target is not None:
                state["hard_target"] = hard_target
                score_state["hard_target_score"] = hard_target["target_score"]
                score_state["actual_score_gap_to_hard_target"] = (
                    hard_target["target_score"] - score_state["score"]
                )
                score_state["actual_penalty_factor_does_not_redefine_hard_target"] = True
            if runtime_estimates_s:
                hurdles = {}
                for skill, runtime_s in sorted(runtime_estimates_s.items()):
                    try:
                        result = break_even_action_gain_mhz(
                            baseline_fmax_mhz=score_state["baseline_fmax_mhz"],
                            best_fmax_mhz=score_state["best_fmax_mhz"],
                            elapsed_runtime_s=score_state["elapsed_runtime_s"],
                            llm_cost_usd=score_state["llm_cost_usd"],
                            additional_runtime_s=runtime_s,
                            additional_llm_cost_usd=0.0,
                        )
                    except (TypeError, ValueError):
                        continue
                    hurdles[str(skill)] = {
                        "estimated_skill_runtime_s": result["additional_runtime_s"],
                        "break_even_additional_gain_mhz": result[
                            "break_even_additional_gain_mhz"
                        ],
                        "tie_possible": result["tie_possible"],
                        "positive_score_possible": result["positive_score_possible"],
                        "configured_skill_runtime_fits_window": (
                            result["additional_runtime_s"] <= execution_window
                        ),
                        "configured_skill_runtime_deadline_slack_s": (
                            execution_window - result["additional_runtime_s"]
                        ),
                    }
                score_state["skill_runtime_only_break_even_hurdles"] = hurdles
                score_state["hurdle_assumption"] = (
                    "lower bound using configured skill runtime only and $0 future "
                    "LLM cost; excludes validation, telemetry, export, and other wall time"
                )
            state["contest_score"] = score_state
            if candidate_runtime_estimates_s:
                skills = {}
                for skill, candidate_runtime in sorted(
                    candidate_runtime_estimates_s.items()
                ):
                    options = []
                    for count in range(1, int(max_skill_candidates_per_action) + 1):
                        additional = count * (
                            float(candidate_runtime)
                            + float(central_measurement_estimate_s)
                        )
                        hurdle = break_even_action_gain_mhz(
                            baseline_fmax_mhz=score_state["baseline_fmax_mhz"],
                            best_fmax_mhz=score_state["best_fmax_mhz"],
                            elapsed_runtime_s=score_state["elapsed_runtime_s"],
                            llm_cost_usd=score_state["llm_cost_usd"],
                            additional_runtime_s=additional,
                            additional_llm_cost_usd=0.0,
                        )
                        required = additional + float(candidate_commit_reserve_s)
                        options.append({
                            "candidate_count": count,
                            "estimated_additional_wall_s": additional,
                            "required_window_with_commit_reserve_s": required,
                            "fits_current_execution_window": required <= execution_window,
                            "break_even_additional_gain_mhz": hurdle[
                                "break_even_additional_gain_mhz"
                            ],
                        })
                    skills[str(skill)] = {
                        "independent_ordered_siblings": True,
                        "estimated_candidate_runtime_s": float(candidate_runtime),
                        "options": options,
                    }
                state["candidate_batch_budget"] = {
                    "policy_owner": "llm",
                    "execution_mode": "serial_single_vivado",
                    "hard_output_cap": int(max_skill_candidates_per_action),
                    "candidate_commit_reserve_s": float(candidate_commit_reserve_s),
                    "estimates_are_informational": True,
                    "python_selects_candidate_count": False,
                    "skills": skills,
                }
        saturation_cfg = saturation_config or {}
        if saturation_cfg.get("enabled", False):
            state["saturation_state"] = level1_saturation_state(
                self.history,
                (self.candidate_graph.get("nodes") or {}),
                platform_id=platform_id,
                low_gain_max_delta_mhz=saturation_cfg.get(
                    "low_gain_max_delta_mhz", 0.01
                ),
                minimum_consecutive=saturation_cfg.get("minimum_consecutive", 2),
                family_by_skill=saturation_cfg.get("strategy_family_by_skill") or {},
                max_scopes=saturation_cfg.get("max_scopes_in_llm_state", 12),
            )
        if self.plan:
            state["plan"] = self.plan
        if self.candidate_graph:
            active_ids = self.candidate_graph.get("beam_candidate_ids", [])
            composition_proposals = self.composition_seed_proposals()
            display_ids = list(dict.fromkeys(
                list(active_ids)
                + [item["seed_candidate_id"] for item in composition_proposals]
            ))
            best_node = self.candidate_node(
                self.candidate_graph.get("current_best_candidate_id")
            )
            compact_candidates = []
            for candidate_id in display_ids:
                node = self.candidate_node(candidate_id)
                if node is None:
                    continue
                signature = node.get("physical_signature") or {}
                dimensions = signature.get("dimensions") or {}
                ancestry = []
                ancestor_id = node.get("parent_candidate_id")
                while ancestor_id is not None and len(ancestry) < 3:
                    ancestor = self.candidate_node(str(ancestor_id))
                    if ancestor is None:
                        break
                    ancestry.append({
                        "candidate_id": str(ancestor_id),
                        "skill": str(ancestor.get("skill", ""))[:32],
                        "target": str(ancestor.get("target", ""))[:48],
                    })
                    ancestor_id = ancestor.get("parent_candidate_id")
                compact_candidates.append({
                    "candidate_id": candidate_id,
                    "parent_candidate_id": node.get("parent_candidate_id"),
                    "depth": node.get("depth"),
                    "fmax_mhz": node.get("fmax_mhz"),
                    "clock_period_ns": node.get("clock_period_ns"),
                    "wns_ns": node.get("wns_ns"),
                    "whs_ns": node.get("whs_ns"),
                    "wpws_ns": node.get("wpws_ns"),
                    "cumulative_lineage_runtime_s": node.get(
                        "cumulative_lineage_runtime_s"
                    ),
                    "last_skill": node.get("skill"),
                    "last_target": node.get("target"),
                    "promoted_to_global_best": bool(
                        node.get("promoted_to_global_best")
                    ),
                    "physical_signature_status": signature.get("status"),
                    "physical_signature_digest": (
                        str(signature.get("digest_sha256"))[:16] or None
                    ),
                    "physical_signature_dimensions": {
                        key: dimensions[key]
                        for key in (
                            "clock_period_ps",
                            "route_pct_bucket",
                            "max_logic_levels",
                            "avg_logic_levels_x10",
                            "spread_x_bucket",
                            "spread_y_bucket",
                            "spread_ratio_x10",
                            "dsp_present",
                            "bram_present",
                            "uram_present",
                        )
                        if key in dimensions
                    },
                    "ancestry": ancestry,
                    "continuation": compact(node.get("continuation") or {}),
                    "physical_diversity_to_global_best": (
                        physical_signature_distance(
                            signature,
                            (best_node or {}).get("physical_signature") or {},
                            int(
                                (self.candidate_graph.get("beam_retention") or {}).get(
                                    "minimum_common_dimensions", 4
                                )
                            ),
                        )
                        if best_node is not None else None
                    ),
                })
            state["candidate_search"] = {
                "default_seed_candidate_id": self.candidate_graph.get(
                    "current_best_candidate_id"
                ),
                "selectable_candidate_ids": list(dict.fromkeys(
                    list(active_ids)
                    + [item["seed_candidate_id"] for item in composition_proposals]
                )),
                "seed_selection": (
                    "seed_candidate_id is optional; omitted defaults to current global "
                    "best; active beam candidates and measured enabling-pool "
                    "proposals are selectable"
                ),
                "beam_width": self.candidate_graph.get("beam_width"),
                "beam_objectives": "max_fmax,min_lineage_runtime",
                "beam_retention": self.candidate_graph.get("beam_retention"),
                "seed_selection_owner": "llm",
                "python_automatically_rotates_seeds": False,
                "no_llm_seed_policy": "global_best_only",
                "lineage_runtime_is_search_heuristic": True,
                "candidates": compact_candidates,
                "enabling_composition": {
                    "authority": "deterministic_measured_rules_no_llm_judgment",
                    "optimization_result": False,
                    "proposals": composition_proposals,
                },
            }
        baseline_profile = self.baseline.get("timing_profile") or {}
        baseline_features = self.baseline.get("features") or {}
        current_profile = (
            self.current_telemetry.get("timing_profile") or baseline_profile
        )
        current_features = (
            self.current_telemetry.get("design_features") or baseline_features
        )
        if current_profile:
            state["timing_profile"] = compact_profile(current_profile)
        if current_features:
            state["design_features"] = compact_features(current_features)
        if baseline_profile:
            state["baseline_timing_profile"] = compact_profile(baseline_profile)
        if baseline_features:
            state["baseline_design_features"] = compact_features(baseline_features)
        if self.current_telemetry:
            state["timing_summary"] = compact(
                self.current_telemetry.get("timing_summary") or {}
            )
            state["fanout_nets"] = compact(
                (self.current_telemetry.get("fanout_nets") or [])[:12]
            )
            state["utilization"] = compact(
                self.current_telemetry.get("utilization") or {}
            )
            state["congestion"] = compact(
                self.current_telemetry.get("congestion") or {}
            )
            state["replica_population"] = compact(
                self.current_telemetry.get("replica_population") or {}
            )
            state["carry_clouds"] = compact(
                {
                    key: value
                    for key, value in (
                        self.current_telemetry.get("carry_clouds") or {}
                    ).items()
                    if key in {
                        "status", "read_only", "carry_cell_count",
                        "analyzed_cell_count", "truncated", "chain_count",
                        "max_chain_length", "cloud_summary",
                    }
                }
            )
            state["hard_macro_cascades"] = compact(
                {
                    key: value
                    for key, value in (
                        self.current_telemetry.get("hard_macro_cascades") or {}
                    ).items()
                    if key in {
                        "status", "read_only", "hard_macro_count",
                        "analyzed_macro_count", "truncated", "family_counts",
                        "cascade_count", "max_cascade_length",
                    }
                }
            )
            state["current_matched_cases"] = compact(
                self.current_telemetry.get("matched_cases") or []
            )
        if self.telemetry_failures:
            state["telemetry_failures"] = [
                {key: value for key, value in failure.items() if key != "dcp_path"}
                for failure in self.telemetry_failures[-5:]
            ]
        if self.last_custom_responses:
            state["recent_probes"] = [
                {
                    "round": e.get("round"),
                    "server": e.get("server"),
                    "tool": e.get("tool"),
                    "arguments": e.get("arguments"),
                    "description": (e.get("description") or "")[:200],
                    "seed_candidate_id": e.get("seed_candidate_id"),
                    "response": (e.get("response") or "")[:12000],
                }
                for e in self.last_custom_responses
            ]
        return state

    def state_for_staged_llm(self) -> dict:
        """Positive-allowlist projection for Report, Knowledge, and Decision."""
        self.refresh_budget()
        features = self.current_telemetry.get("design_features") or {}
        bottleneck = str(features.get("dominant_bottleneck") or "unknown")
        if bottleneck not in {
            "routing",
            "placement",
            "logic_depth",
            "fanout",
            "clock_constraint",
            "mixed",
            "unknown",
        }:
            bottleneck = "unknown"
        incumbent_sha = self.current_incumbent_artifact_sha256()
        exhausted_actions = []
        seen_actions = set()
        incumbent_id = str(self.current_best.get("candidate_id") or "")
        nodes = (self.candidate_graph or {}).get("nodes") or {}
        ancestor_ids = {incumbent_id}
        cursor = incumbent_id
        for _ in range(len(nodes) + 1):
            node = nodes.get(cursor)
            if not isinstance(node, dict):
                break
            parent = node.get("parent_candidate_id")
            if parent is None or str(parent) in ancestor_ids:
                break
            cursor = str(parent)
            ancestor_ids.add(cursor)
        lineage_actions: dict[tuple[str, str], dict] = {}
        for outcome in self.history:
            key = (
                str(outcome.get("skill") or ""),
                str(outcome.get("target") or ""),
            )
            if (
                outcome.get("action_executed") is not True
                or str(outcome.get("produced_candidate_id") or "") not in ancestor_ids
                or not all(key)
            ):
                continue
            summary = lineage_actions.setdefault(key, {
                "skill": key[0],
                "target": key[1],
                "measurements": 0,
                "accepted_measurements": 0,
            })
            summary["measurements"] += 1
            if outcome.get("accepted") is True:
                summary["accepted_measurements"] += 1
        for outcome in reversed(self.history):
            key = (
                str(outcome.get("skill") or ""),
                str(outcome.get("target") or ""),
            )
            if (
                outcome.get("action_executed") is not True
                or str(outcome.get("seed_candidate_id") or "") != incumbent_id
                or not all(key)
                or key in seen_actions
            ):
                continue
            seen_actions.add(key)
            exhausted_actions.append({
                "skill": key[0],
                "target": key[1],
                "disposition": "measured",
            })
        for event in reversed(self.decision_reviews):
            review = event.get("final_review") or {}
            proof = event.get("action_proof") or {}
            source = proof.get("source_identity") or {}
            action = event.get("provisional_action") or {}
            key = (str(action.get("skill") or ""), str(action.get("target") or ""))
            if (
                review.get("action") != "replan"
                or source.get("seed_artifact_sha256") != incumbent_sha
                or not all(key)
                or key in seen_actions
            ):
                continue
            seen_actions.add(key)
            exhausted_actions.append({
                "skill": key[0],
                "target": key[1],
                "disposition": "neutral_replan",
            })
        state = {
            "round": int(self.round),
            "current_clock_period_ns": self.current_clock_period(),
            "current_wns": self.current_best.get("wns"),
            "current_fmax_mhz": self.current_best.get("fmax"),
            "fmax_gain_mhz": self.fmax_gain_mhz(),
            "bottleneck": bottleneck,
            "telemetry_epoch": self.current_telemetry.get("epoch"),
            "exhausted_probes": self.exhausted_probe_summary(
                incumbent_sha
            ),
            "exhausted_actions": exhausted_actions[:16],
            "measured_actions_on_current_lineage": list(lineage_actions.values())[-16:],
            "suggested_targets": {
                skill: [
                    str(target)
                    for target in targets[:10]
                    if isinstance(target, str) and target and len(target) <= 1024
                ]
                for skill, targets in self.suggested_targets.items()
                if skill in {"fanout_opt", "force_replicate"}
                and isinstance(targets, list)
            },
            "budget": {
                "time_elapsed_s": self.budget.get("time_elapsed"),
                "time_remaining_s": self.budget.get("time_remaining"),
                "time_limit_s": self.budget.get("time_limit_s"),
                "token_remaining_usd": self.budget.get("tokens_remaining"),
            },
            "task_counts": {
                "probe": self.probe_count,
                "mutation": self.mutation_count,
                "validation": self.validation_count,
            },
            "run_contract": {
                "immutable_incumbent_retained": True,
                "hard_mechanical_deadline_enforced": True,
                "central_measurement_is_authority": True,
                "proof_gates_remain_authoritative": True,
            },
        }
        concurrent_packet = self.current_telemetry.get(
            "concurrent_proof_decision_packet"
        )
        if isinstance(concurrent_packet, dict):
            state["concurrent_proof_decision_packet"] = copy.deepcopy(
                concurrent_packet
            )
        from .action_menu import validate_action_menu

        action_menu = validate_action_menu(
            self.current_telemetry.get("authorized_action_menu")
        )
        state["authorized_action_menu"] = copy.deepcopy(action_menu)
        menu_timing = self.current_telemetry.get("action_menu_timing")
        if isinstance(menu_timing, dict):
            state["action_menu_epoch_cost"] = copy.deepcopy(menu_timing)
        return state
