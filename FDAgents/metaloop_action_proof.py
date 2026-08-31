"""Replayable pre-mutation authorizations for the two meta-loop actions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .compressor_proof import _verify_schedule_structure
from .memory_write_buffer_forward import (
    ForwardedReadPort,
    MemoryWriteBufferForwardContract,
    prove_memory_write_buffer_forward,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def build_constructive_accumulator_authorization(
    *,
    source_dcp_sha256: str,
    expression_sha256: str,
    certificates: list[dict],
) -> dict:
    if not certificates:
        raise ValueError("constructive accumulator family is empty")
    weighted = certificates[0]["weighted_sum"]
    spec = certificates[0]["compressor_spec"]
    sibling_rows = []
    for certificate in certificates:
        if (
            certificate.get("status") != "proved"
            or certificate.get("mutation_ready") is not True
            or certificate.get("source_identity", {}).get("dcp_sha256")
            != source_dcp_sha256
            or certificate.get("constructive_proof", {}).get("expression_sha256")
            != expression_sha256
            or certificate.get("weighted_sum") != weighted
            or certificate.get("compressor_spec") != spec
        ):
            raise ValueError("constructive sibling certificate mismatch")
        sibling_rows.append({
            "region_sha256": certificate["source_identity"]["connectivity_sha256"],
            "target_stem_digest": hashlib.sha256(
                certificate["source_identity"]["target_stem"].encode()
            ).hexdigest(),
            "lowering_certificate_id": certificate["certificate_id"],
        })
    region_sha256 = _digest(sorted(row["region_sha256"] for row in sibling_rows))
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": 1,
        "authority": "pre_mutation_only",
        "rewrite_kind": "constructive_accumulator_rebuild",
        "source": {
            "dcp_sha256": source_dcp_sha256,
            "region_sha256": region_sha256,
        },
        "spec": {
            "family_size": len(certificates),
            "expression_sha256": expression_sha256,
            "weighted_sum": weighted,
            "sibling_rows": sibling_rows,
        },
        "plan": {
            "template": "CONSTRUCTIVE_ACCUMULATOR_GENERIC_V2",
            "compressor_spec": spec,
            "cycle_timing_change": 0,
        },
        "proof": {
            "status": "proved",
            "method": "fresh_canonical_roBDD_plus_structural_column_weight_replay",
        },
    }
    authorization["certificate_id"] = _digest(authorization)
    if not verify_constructive_accumulator_authorization(authorization):
        raise ValueError("constructive authorization did not replay")
    return authorization


def verify_constructive_accumulator_authorization(value: dict) -> bool:
    try:
        if value.get("rewrite_kind") != "constructive_accumulator_rebuild":
            return False
        spec = value["spec"]
        plan = value["plan"]
        if (
            spec["family_size"] < 1
            or len(spec["sibling_rows"]) != spec["family_size"]
            or len({row["region_sha256"] for row in spec["sibling_rows"]})
            != spec["family_size"]
            or plan["template"] != "CONSTRUCTIVE_ACCUMULATOR_GENERIC_V2"
            or plan["cycle_timing_change"] != 0
            or value["proof"] != {
                "status": "proved",
                "method": "fresh_canonical_roBDD_plus_structural_column_weight_replay",
            }
        ):
            return False
        return _verify_schedule_structure(
            spec["weighted_sum"], plan["compressor_spec"]
        ) is not None
    except (KeyError, TypeError, ValueError):
        return False


def build_memory_forward_authorization(proof: dict) -> dict:
    checked_proof = json.loads(json.dumps(proof, sort_keys=True))
    raw_contract = dict(checked_proof.get("contract") or {})
    raw_contract["read_ports"] = tuple(
        ForwardedReadPort(**row) for row in raw_contract.get("read_ports") or []
    )
    contract = MemoryWriteBufferForwardContract(**raw_contract)
    replay = json.loads(json.dumps(
        prove_memory_write_buffer_forward(contract), sort_keys=True
    ))
    if replay != checked_proof or checked_proof.get("mutation_ready") is not True:
        raise ValueError("memory-forward proof did not replay")
    authorization = {
        "kind": "operator_rewrite_authorization",
        "version": 1,
        "authority": "pre_mutation_only",
        "rewrite_kind": "memory_write_buffer_forward",
        "source": {
            "dcp_sha256": contract.source_dcp_sha256,
            "region_sha256": contract.target_memory_fingerprint,
        },
        "spec": asdict(contract),
        "plan": {
            "template": "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1",
            "cycle_timing_change": 0,
            "pending_write_depth": 1,
            "read_forwarding": True,
        },
        "proof": checked_proof,
    }
    authorization["certificate_id"] = _digest(authorization)
    if replay_memory_forward_authorization(authorization) is None:
        raise ValueError("memory-forward authorization did not replay")
    return authorization


def replay_memory_forward_authorization(value: dict) -> dict | None:
    try:
        claimed = value.get("certificate_id")
        unsigned = dict(value)
        unsigned.pop("certificate_id", None)
        if (
            value.get("kind") != "operator_rewrite_authorization"
            or value.get("version") != 1
            or value.get("authority") != "pre_mutation_only"
            or value.get("rewrite_kind") != "memory_write_buffer_forward"
            or not isinstance(claimed, str)
            or _digest(unsigned) != claimed
        ):
            return None
        raw = dict(value["spec"])
        raw["read_ports"] = tuple(
            ForwardedReadPort(**row) for row in raw.get("read_ports") or []
        )
        contract = MemoryWriteBufferForwardContract(**raw)
        replay = json.loads(json.dumps(
            prove_memory_write_buffer_forward(contract), sort_keys=True
        ))
        if (
            value.get("source") != {
                "dcp_sha256": contract.source_dcp_sha256,
                "region_sha256": contract.target_memory_fingerprint,
            }
            or
            value["plan"] != {
                "template": "BRAM_WRITE_BUFFER_FORWARD_LOCAL_MITER_V1",
                "cycle_timing_change": 0,
                "pending_write_depth": 1,
                "read_forwarding": True,
            }
            or replay != json.loads(json.dumps(value["proof"], sort_keys=True))
            or replay.get("mutation_ready") is not True
        ):
            return None
        return replay
    except (KeyError, TypeError, ValueError):
        return None
