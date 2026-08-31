"""Mechanical adjudication of exact-proof counterexamples on real artifacts.

The adjudicator has no mutation or promotion authority.  It replays one proof
counterexample on the source and speculative candidate artifacts and classifies
the proof failure by the observed traces: a real trace mismatch is a semantic
failure; matching traces mean that the proof/counterexample was misbound.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CounterexampleVerdict(str, Enum):
    """The two fail-closed adjudicator verdicts."""

    REAL_FAILURE = "failed_real"
    MISBOUND_FAILURE = "failed_misbound"


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    """Identity of a real netlist used for counterexample replay."""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("artifact sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CounterexampleReplayRequest:
    """Hash-bound replay inputs supplied after speculative execution joins."""

    candidate_id: str
    proof_id: str
    source: ArtifactBinding
    candidate: ArtifactBinding
    counterexample: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.proof_id.strip():
            raise ValueError("candidate_id and proof_id must be nonempty")
        if not isinstance(self.counterexample, Mapping):
            raise TypeError("counterexample must be a mapping")
        _json_copy(self.counterexample, "counterexample")


@dataclass(frozen=True, slots=True)
class CounterexampleReplayResult:
    """Mechanical verdict and normalized observations from both artifacts."""

    verdict: CounterexampleVerdict
    source_observation: Any
    candidate_observation: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "counterexample_replay_adjudication_v1",
            "verdict": self.verdict.value,
            "source_observation": copy.deepcopy(self.source_observation),
            "candidate_observation": copy.deepcopy(self.candidate_observation),
            "reason": self.reason,
            "authority": "mechanical_classification_only_no_promotion_authority",
        }


ReplayCallback = Callable[[ArtifactBinding, Mapping[str, Any]], Any]


class CounterexampleReplayAdjudicator:
    """Replay a counterexample against both real netlists and compare traces."""

    def adjudicate(
        self,
        request: CounterexampleReplayRequest,
        *,
        replay_source: ReplayCallback,
        replay_candidate: ReplayCallback,
    ) -> CounterexampleReplayResult:
        source_observation = _json_copy(
            replay_source(request.source, copy.deepcopy(dict(request.counterexample))),
            "source replay observation",
        )
        candidate_observation = _json_copy(
            replay_candidate(
                request.candidate, copy.deepcopy(dict(request.counterexample))
            ),
            "candidate replay observation",
        )
        if source_observation != candidate_observation:
            return CounterexampleReplayResult(
                verdict=CounterexampleVerdict.REAL_FAILURE,
                source_observation=source_observation,
                candidate_observation=candidate_observation,
                reason="counterexample reproduced a source/candidate trace mismatch",
            )
        return CounterexampleReplayResult(
            verdict=CounterexampleVerdict.MISBOUND_FAILURE,
            source_observation=source_observation,
            candidate_observation=candidate_observation,
            reason=(
                "real source and candidate traces match; the failed proof or "
                "counterexample binding is inconsistent with the artifacts"
            ),
        )


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-serializable") from exc
