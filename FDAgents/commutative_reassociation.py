"""Recover control-closed commutative registered reductions.

The recovery is intentionally name-agnostic.  It consumes one exact
register-cone extraction, groups complete indexed FF words, replays the LUT /
CARRY primitive graph, and authorizes only whole-word permutations whose data
and register-control contracts are identical.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

from .compressor_proof import (
    ConeGraph,
    _evaluate_target_boolean_batch,
)


CONTRACT_VERSION = 1
_INDEXED_CELL = re.compile(r"^(?P<stem>.+)\[(?P<index>\d+)\]$")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _indexed_words(
    rows: list[dict[str, str]], width: int
) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, dict[int, dict[str, str]]] = {}
    for row in rows:
        match = _INDEXED_CELL.fullmatch(str(row.get("cell") or ""))
        if match is None:
            continue
        grouped.setdefault(match.group("stem"), {})[int(match.group("index"))] = row
    result: dict[str, list[dict[str, str]]] = {}
    for stem, bits in grouped.items():
        if sorted(bits) == list(range(width)):
            result[stem] = [bits[index] for index in range(width)]
    return result


def _control_signature(rows: list[dict[str, str]]) -> tuple[str, ...]:
    signatures = {
        (
            str(row.get("clock_net") or ""),
            str(row.get("ce_net") or ""),
            str(row.get("sr_net") or ""),
            str(row.get("init") or ""),
            str(row.get("ref") or ""),
        )
        for row in rows
    }
    if len(signatures) != 1:
        raise ValueError("indexed word has mixed clock/CE/reset/INIT controls")
    return next(iter(signatures))


def build_commutative_reassociation_contract(
    cone_dir: Path | str,
    *,
    source_dcp_sha256: str,
    critical_source_stem: str = "",
    replay_samples: int = 2048,
) -> dict[str, Any]:
    """Build a private exact-source contract and an anonymized public view."""
    cone_dir = Path(cone_dir)
    graph = ConeGraph.load(cone_dir)
    width = len(graph.targets)
    if width < 2:
        raise ValueError("commutative reduction word is narrower than two bits")
    manifest_sha = str(graph.manifest.get("dcp_sha256") or "")
    if manifest_sha != source_dcp_sha256:
        raise ValueError("commutative reduction source identity mismatch")

    target_rows = _rows(cone_dir / "targets.tsv")
    boundary_rows = _rows(cone_dir / "boundary.tsv")
    target_control = _control_signature(target_rows)
    words = _indexed_words(boundary_rows, width)
    if len(words) < 2:
        raise ValueError("fewer than two complete registered operand words recovered")
    for rows in words.values():
        if _control_signature(rows) != target_control:
            raise ValueError("operand and endpoint transaction controls differ")
    if graph.side_fanout:
        raise ValueError("replaceable arithmetic cone has outside side fanout")

    ordered_stems = sorted(words)
    input_nets = sorted(graph.boundary_nets)
    rng = random.Random(0xF1A26)
    assignments: list[tuple[str, ...]] = []
    expected: list[int] = []
    modulus = 1 << width
    for ordinal in range(max(64, int(replay_samples))):
        values = [
            0 if ordinal == 0 else modulus - 1 if ordinal == 1 else rng.randrange(modulus)
            for _ in ordered_stems
        ]
        true_nets: list[str] = []
        for stem, value in zip(ordered_stems, values):
            for bit, row in enumerate(words[stem]):
                if (value >> bit) & 1:
                    true_nets.append(
                        str(row.get("graph_q_net") or row["q_net"])
                    )
        assignments.append(tuple(true_nets))
        expected.append(sum(values) % modulus)
    actual = _evaluate_target_boolean_batch(
        graph,
        input_nets,
        assignments,
        deadline_check=lambda _stage: None,
    )
    if actual != expected:
        first = next(index for index, pair in enumerate(zip(actual, expected)) if pair[0] != pair[1])
        raise ValueError(f"primitive replay contradicts modular sum at sample {first}")

    if critical_source_stem not in words:
        critical_source_stem = ordered_stems[0]
    candidates = [
        {
            "source_stem": critical_source_stem,
            "peer_stem": peer,
            "changed_word_count": 2,
            "changed_net_upper_bound": 2 * width,
        }
        for peer in ordered_stems
        if peer != critical_source_stem
    ]
    private = {
        "target_stem": str(graph.manifest.get("target_stem") or ""),
        "critical_source_stem": critical_source_stem,
        "operand_stems": ordered_stems,
        "candidate_pairs": candidates,
        "control_signature_digest": _digest(target_control),
        "cone_connectivity_digest": graph.connectivity_digest(),
    }
    public = {
        "operator_class": "REGISTERED_COMMUTATIVE_MODULAR_REDUCTION",
        "relation": f"N x W -> W modulo 2^W",
        "operand_count": len(ordered_stems),
        "word_width": width,
        "clock_ce_reset_transaction_closed": True,
        "endpoint_cycle_preserved": True,
        "outside_side_fanout_count": 0,
        "primitive_replay_samples": len(assignments),
        "primitive_replay_passed": True,
        "candidate_pair_count": len(candidates),
        "mutation_scope": "two_complete_operand_buses_changed_net_only",
        "final_equivalence_required": True,
    }
    body = {
        "kind": "commutative_operand_bus_reassociation_contract",
        "version": CONTRACT_VERSION,
        "source_dcp_sha256": source_dcp_sha256,
        "public": public,
        "private": private,
        "proof_status": "exact_primitive_replay_guard_and_control_closed_measurement_probe",
    }
    body["contract_sha256"] = _digest(body)
    return body


def verify_commutative_reassociation_contract(
    contract: dict[str, Any], *, source_dcp_sha256: str
) -> bool:
    if not isinstance(contract, dict):
        return False
    claimed = contract.get("contract_sha256")
    body = dict(contract)
    body.pop("contract_sha256", None)
    return bool(
        contract.get("kind") == "commutative_operand_bus_reassociation_contract"
        and contract.get("version") == CONTRACT_VERSION
        and contract.get("source_dcp_sha256") == source_dcp_sha256
        and claimed == _digest(body)
        and (contract.get("public") or {}).get("primitive_replay_passed") is True
        and (contract.get("public") or {}).get("clock_ce_reset_transaction_closed") is True
    )


__all__ = [
    "CONTRACT_VERSION",
    "build_commutative_reassociation_contract",
    "verify_commutative_reassociation_contract",
]
