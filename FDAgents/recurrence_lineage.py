"""Durable proof lineage for route-polished recurrence-family candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .artifacts import (
    atomic_copy_file,
    atomic_write_text,
    sha256_file,
    validate_owned_candidate_path,
)
from .operator_proof_current import (
    MIN_POST_EMISSION_PRECHECK_VECTORS,
    MIN_POST_EMISSION_VECTORS,
    OPERATOR_CANDIDATE_PROOF_VERSION,
    build_operator_candidate_proof,
    verify_operator_post_emission_certificate,
)
from .skills.base import SkillContractError, SkillOutput
from .submission_worker import run_strict_validation_worker


RECURRENCE_TEMPLATE = "dsp_srl_recurrence_cut_v1"
RECURRENCE_LINEAGE_KEY = "recurrence_family_lineage"
RECURRENCE_LINEAGE_BUNDLE_NAME = "operator_rewrite_recurrence_lineage.json"
RECURRENCE_LINEAGE_STORE_DIR = "proof_lineage"
MAX_RECURRENCE_LINEAGE_BUNDLE_BYTES = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_ID_RE = re.compile(r"^c[0-9]{6}$")
_BUNDLE_FIELDS = {
    "kind",
    "version",
    "template",
    "source_artifact_sha256",
    "family_facts",
    "candidate",
    "placement",
}
_DESCRIPTOR_FIELDS = {
    "kind",
    "version",
    "template",
    "bundle_sha256",
    "source_candidate_id",
    "source_artifact_sha256",
    "family_contract_sha256",
    "placement_manifest_sha256",
    "authorization_id",
    "emitted_topology_attestation_id",
    "operator_candidate_certificate_id",
}
_OPERATOR_PROOF_FIELDS = {
    "proof_version",
    "skill",
    "template",
    "source_artifact_sha256",
    "artifact_sha256",
    "rewrite_authorization",
    "post_emission_validation",
}


@dataclass(frozen=True, slots=True)
class RecurrenceLineageContext:
    """Validated replay inputs and the graph-owned authorization source."""

    descriptor: dict[str, Any]
    bundle: dict[str, Any]
    source_candidate_id: str
    source_path: Path
    source_artifact_sha256: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SkillContractError(f"recurrence lineage {field} is not a SHA-256 digest")
    return value


def _payload(raw: Any, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise SkillContractError(f"recurrence lineage {field} is not JSON") from exc
    if not isinstance(value, dict):
        raise SkillContractError(f"recurrence lineage {field} is not an object")
    if value.get("status") == "error" or "error" in value:
        raise SkillContractError(
            f"recurrence lineage {field} failed: "
            f"{value.get('error') or value.get('message')}"
        )
    return value


def _read_bundle(path: Path, expected_sha256: str) -> dict[str, Any]:
    bundle_path = Path(path)
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise SkillContractError("recurrence lineage bundle is unavailable or unsafe")
    descriptor = os.open(
        bundle_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 0
            or file_stat.st_size > MAX_RECURRENCE_LINEAGE_BUNDLE_BYTES
        ):
            raise SkillContractError(
                "recurrence lineage bundle is unavailable or unsafe"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            encoded = stream.read(MAX_RECURRENCE_LINEAGE_BUNDLE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise SkillContractError("recurrence lineage bundle identity mismatch")
    try:
        value = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SkillContractError("recurrence lineage bundle is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise SkillContractError("recurrence lineage bundle is not an object")
    return value


def _descriptor_from_evidence(
    evidence: Optional[dict[str, Any]],
    *,
    required: bool,
) -> Optional[dict[str, Any]]:
    if not isinstance(evidence, dict) or RECURRENCE_LINEAGE_KEY not in evidence:
        if required:
            raise SkillContractError("recurrence lineage descriptor is missing")
        return None
    descriptor = evidence.get(RECURRENCE_LINEAGE_KEY)
    if not isinstance(descriptor, dict):
        raise SkillContractError("recurrence lineage descriptor is malformed")
    if set(descriptor) != _DESCRIPTOR_FIELDS:
        raise SkillContractError(
            "recurrence lineage descriptor has missing or unsupported fields"
        )
    if (
        descriptor.get("kind") != "recurrence_family_candidate_lineage"
        or descriptor.get("version") != 1
        or descriptor.get("template") != RECURRENCE_TEMPLATE
    ):
        raise SkillContractError("recurrence lineage descriptor version mismatch")
    for field in (
        "bundle_sha256",
        "source_artifact_sha256",
        "family_contract_sha256",
        "placement_manifest_sha256",
        "authorization_id",
        "emitted_topology_attestation_id",
        "operator_candidate_certificate_id",
    ):
        _require_digest(descriptor.get(field), field)
    source_id = descriptor.get("source_candidate_id")
    if source_id is not None and (
        not isinstance(source_id, str) or _CANDIDATE_ID_RE.fullmatch(source_id) is None
    ):
        raise SkillContractError("recurrence lineage source candidate ID is invalid")
    return copy.deepcopy(descriptor)


def _validate_operator_proof(
    proof: Optional[dict[str, Any]],
    *,
    source_sha256: str,
    candidate_sha256: str,
) -> dict[str, Any]:
    if not isinstance(proof, dict) or set(proof) != _OPERATOR_PROOF_FIELDS:
        raise SkillContractError("recurrence lineage operator proof is malformed")
    if (
        proof.get("proof_version") != OPERATOR_CANDIDATE_PROOF_VERSION
        or proof.get("skill") != "operator_rewrite"
        or proof.get("template") != RECURRENCE_TEMPLATE
        or proof.get("source_artifact_sha256") != source_sha256
        or proof.get("artifact_sha256") != candidate_sha256
    ):
        raise SkillContractError("recurrence lineage operator proof identity mismatch")
    if not verify_operator_post_emission_certificate(
        proof.get("post_emission_validation"),
        proof.get("rewrite_authorization"),
        expected_template=RECURRENCE_TEMPLATE,
        expected_source_sha256=source_sha256,
        expected_candidate_sha256=candidate_sha256,
    ):
        raise SkillContractError("recurrence lineage operator proof did not replay")
    return copy.deepcopy(proof)


def _validate_bundle_binding(
    bundle: dict[str, Any],
    descriptor: dict[str, Any],
    proof: Optional[dict[str, Any]],
    *,
    source_sha256: str,
    candidate_sha256: str,
) -> None:
    if set(bundle) != _BUNDLE_FIELDS or (
        bundle.get("kind") != "dsp_recurrence_family_lineage_bundle"
        or bundle.get("version") != 1
        or bundle.get("template") != RECURRENCE_TEMPLATE
        or bundle.get("source_artifact_sha256") != source_sha256
    ):
        raise SkillContractError("recurrence lineage bundle schema mismatch")
    validated_proof = _validate_operator_proof(
        proof,
        source_sha256=source_sha256,
        candidate_sha256=candidate_sha256,
    )
    family = bundle.get("family_facts")
    candidate = bundle.get("candidate")
    placement = bundle.get("placement")
    if not isinstance(family, dict) or not isinstance(candidate, dict) or not isinstance(
        placement, dict
    ):
        raise SkillContractError("recurrence lineage replay inputs are malformed")
    if (
        family.get("kind") != "dsp_recurrence_family_contract_facts"
        or family.get("version") != 1
        or family.get("status") != "success"
        or family.get("proof_status")
        != "family_rewrite_facts_extracted_unproved"
        or family.get("fresh_rediscovery") is not True
        or family.get("mutation_eligible") is not False
        or family.get("design_sha256") != source_sha256
    ):
        raise SkillContractError("recurrence lineage family facts are stale")
    family_payload = dict(family)
    family_sha256 = family_payload.pop("family_contract_sha256", None)
    for key in ("family_id", "fresh_rediscovery", "runtime_s"):
        family_payload.pop(key, None)
    if (
        _require_digest(family_sha256, "family contract")
        != descriptor["family_contract_sha256"]
        or _digest(family_payload) != family_sha256
    ):
        raise SkillContractError("recurrence lineage family contract mismatch")

    authorization = candidate.get("authorization")
    if (
        candidate.get("authority") != "pre_mutation_only"
        or not isinstance(authorization, dict)
        or authorization != validated_proof.get("rewrite_authorization")
        or authorization.get("source")
        != {"dcp_sha256": source_sha256, "region_sha256": family_sha256}
        or candidate.get("plan") != authorization.get("plan")
        or candidate.get("family_id") != family.get("family_id")
        or candidate.get("boundary_region_sha256s")
        != family.get("boundary_region_sha256s")
    ):
        raise SkillContractError("recurrence lineage action candidate mismatch")
    authorization_id = _require_digest(
        authorization.get("certificate_id"), "authorization"
    )

    placement_payload = dict(placement)
    placement_sha256 = placement_payload.pop("manifest_sha256", None)
    if (
        placement.get("kind") != "recurrence_family_placement_manifest"
        or placement.get("source")
        != {"dcp_sha256": source_sha256, "region_sha256": family_sha256}
        or placement.get("family_id") != family.get("family_id")
        or placement.get("authorization_id") != authorization_id
        or _require_digest(placement_sha256, "placement manifest")
        != descriptor["placement_manifest_sha256"]
        or _digest(placement_payload) != placement_sha256
    ):
        raise SkillContractError("recurrence lineage placement manifest mismatch")

    certificate = validated_proof["post_emission_validation"]
    emitted = certificate["emitted_topology"]
    if (
        descriptor.get("source_artifact_sha256") != source_sha256
        or descriptor.get("authorization_id") != authorization_id
        or descriptor.get("emitted_topology_attestation_id")
        != emitted.get("attestation_id")
        or descriptor.get("operator_candidate_certificate_id")
        != certificate.get("certificate_id")
        or emitted.get("placement_manifest_sha256") != placement_sha256
    ):
        raise SkillContractError("recurrence lineage descriptor proof binding mismatch")


def _descriptor_for_proof(
    *,
    bundle_sha256: str,
    source_candidate_id: Optional[str],
    source_sha256: str,
    family_sha256: str,
    placement_sha256: str,
    proof: dict[str, Any],
) -> dict[str, Any]:
    certificate = proof.get("post_emission_validation") or {}
    emitted = certificate.get("emitted_topology") or {}
    return {
        "kind": "recurrence_family_candidate_lineage",
        "version": 1,
        "template": RECURRENCE_TEMPLATE,
        "bundle_sha256": bundle_sha256,
        "source_candidate_id": source_candidate_id,
        "source_artifact_sha256": source_sha256,
        "family_contract_sha256": family_sha256,
        "placement_manifest_sha256": placement_sha256,
        "authorization_id": str(
            (proof.get("rewrite_authorization") or {}).get("certificate_id") or ""
        ),
        "emitted_topology_attestation_id": str(
            emitted.get("attestation_id") or ""
        ),
        "operator_candidate_certificate_id": str(
            certificate.get("certificate_id") or ""
        ),
    }


def write_recurrence_lineage_bundle(
    action_dir: Path,
    *,
    source_sha256: str,
    family_facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
    proof: dict[str, Any],
) -> dict[str, Any]:
    """Write trusted replay inputs and return a bounded staging descriptor."""
    action_root = Path(action_dir)
    if action_root.is_symlink() or not action_root.is_dir():
        raise SkillContractError("recurrence lineage action directory is unsafe")
    bundle = json.loads(_canonical_json({
        "kind": "dsp_recurrence_family_lineage_bundle",
        "version": 1,
        "template": RECURRENCE_TEMPLATE,
        "source_artifact_sha256": source_sha256,
        "family_facts": family_facts,
        "candidate": candidate,
        "placement": placement,
    }))
    encoded = _canonical_json(bundle)
    if len(encoded.encode("ascii")) > MAX_RECURRENCE_LINEAGE_BUNDLE_BYTES:
        raise SkillContractError("recurrence lineage bundle exceeds its size bound")
    bundle_path = action_root.resolve() / RECURRENCE_LINEAGE_BUNDLE_NAME
    atomic_write_text(bundle_path, encoded)
    bundle_sha256 = sha256_file(bundle_path)
    descriptor = _descriptor_for_proof(
        bundle_sha256=bundle_sha256,
        source_candidate_id=None,
        source_sha256=source_sha256,
        family_sha256=str(family_facts.get("family_contract_sha256") or ""),
        placement_sha256=str(placement.get("manifest_sha256") or ""),
        proof=proof,
    )
    _descriptor_from_evidence({RECURRENCE_LINEAGE_KEY: descriptor}, required=True)
    _validate_bundle_binding(
        bundle,
        descriptor,
        proof,
        source_sha256=source_sha256,
        candidate_sha256=str(proof.get("artifact_sha256") or ""),
    )
    return descriptor


def _lineage_store(run_dir: Path, *, create: bool) -> Path:
    run_root = Path(run_dir)
    if run_root.is_symlink() or not run_root.is_dir():
        raise SkillContractError("recurrence lineage run directory is unsafe")
    store = run_root.resolve() / RECURRENCE_LINEAGE_STORE_DIR
    if store.exists():
        if store.is_symlink() or not store.is_dir():
            raise SkillContractError("recurrence lineage store is unsafe")
    elif create:
        store.mkdir()
    else:
        raise SkillContractError("recurrence lineage store is unavailable")
    return store


def _persist_operator_output(
    output: SkillOutput,
    *,
    action_dir: Path,
    run_dir: Path,
    source_candidate_id: str,
    source_sha256: str,
) -> SkillOutput:
    evidence = copy.deepcopy(output.evidence or {})
    descriptor = _descriptor_from_evidence(evidence, required=True)
    if descriptor["source_candidate_id"] is not None:
        raise SkillContractError("operator lineage descriptor was pre-bound by the skill")
    if descriptor["source_artifact_sha256"] != source_sha256:
        raise SkillContractError("operator lineage source does not match the graph seed")
    bundle_path = Path(action_dir).resolve() / RECURRENCE_LINEAGE_BUNDLE_NAME
    bundle = _read_bundle(bundle_path, descriptor["bundle_sha256"])
    _validate_bundle_binding(
        bundle,
        descriptor,
        output.proof,
        source_sha256=source_sha256,
        candidate_sha256=sha256_file(output.output_dcp),
    )
    store = _lineage_store(run_dir, create=True)
    destination = store / f"{descriptor['bundle_sha256']}.json"
    if destination.exists():
        _read_bundle(destination, descriptor["bundle_sha256"])
    else:
        atomic_copy_file(bundle_path, destination)
        _read_bundle(destination, descriptor["bundle_sha256"])
    descriptor["source_candidate_id"] = source_candidate_id
    evidence[RECURRENCE_LINEAGE_KEY] = descriptor
    return SkillOutput(output.output_dcp, output.label, evidence, output.proof)


def _source_is_ancestor(memory, source_candidate_id: str, descendant_id: str) -> bool:
    cursor: Optional[str] = descendant_id
    limit = len((memory.candidate_graph or {}).get("nodes") or {}) + 1
    for _ in range(limit):
        if cursor == source_candidate_id:
            return True
        node = memory.candidate_node(str(cursor)) if cursor is not None else None
        if node is None:
            return False
        parent = node.get("parent_candidate_id")
        cursor = str(parent) if parent is not None else None
    raise SkillContractError("recurrence lineage graph ancestry contains a cycle")


def validate_recurrence_lineage_candidate(
    memory,
    *,
    run_dir: Path,
    seed_candidate_id: str,
    evidence: Optional[dict[str, Any]],
    proof: Optional[dict[str, Any]],
    candidate_sha256: str,
) -> Optional[dict[str, Any]]:
    """Replay one persisted lineage proof, returning it or ``None``."""
    descriptor = _descriptor_from_evidence(evidence, required=False)
    if descriptor is None:
        return None
    source_id = descriptor.get("source_candidate_id")
    if not isinstance(source_id, str):
        raise SkillContractError("recurrence lineage source candidate is unbound")
    if not _source_is_ancestor(memory, source_id, seed_candidate_id):
        raise SkillContractError("recurrence lineage source is not a graph ancestor")
    source_node = memory.candidate_node(source_id)
    if source_node is None:
        raise SkillContractError("recurrence lineage source candidate is unknown")
    source_sha256 = descriptor["source_artifact_sha256"]
    if source_node.get("artifact_sha256") != source_sha256:
        raise SkillContractError("recurrence lineage graph source identity mismatch")
    try:
        source_path = validate_owned_candidate_path(
            run_dir,
            source_id,
            Path(str(source_node.get("artifact_path") or "")),
        )
    except ValueError as exc:
        raise SkillContractError(str(exc)) from exc
    if source_path.is_symlink() or not source_path.is_file():
        raise SkillContractError("recurrence lineage source artifact is unavailable")
    if sha256_file(source_path) != source_sha256:
        raise SkillContractError("recurrence lineage source artifact changed")
    store = _lineage_store(run_dir, create=False)
    bundle = _read_bundle(
        store / f"{descriptor['bundle_sha256']}.json",
        descriptor["bundle_sha256"],
    )
    _validate_bundle_binding(
        bundle,
        descriptor,
        proof,
        source_sha256=source_sha256,
        candidate_sha256=candidate_sha256,
    )
    return copy.deepcopy(proof)


def validate_full_recurrence_lineage(memory, run_dir: Path, node: dict) -> bool:
    """Replay every required recurrence proof from graph root through ``node``."""
    cursor: Optional[str] = str(node.get("candidate_id") or "")
    reverse_path = []
    limit = len((memory.candidate_graph or {}).get("nodes") or {}) + 1
    for _ in range(limit):
        if not cursor:
            break
        current = memory.candidate_node(cursor)
        if current is None:
            raise SkillContractError("recurrence lineage graph ancestor is missing")
        reverse_path.append(current)
        parent = current.get("parent_candidate_id")
        cursor = str(parent) if parent is not None else None
    if cursor is not None:
        raise SkillContractError("recurrence lineage graph ancestry contains a cycle")

    lineage_active = False
    for current in reversed(reverse_path):
        candidate_id = str(current.get("candidate_id") or "")
        action = (str(current.get("skill") or ""), str(current.get("target") or ""))
        is_recurrence_rewrite = action == ("operator_rewrite", RECURRENCE_TEMPLATE)
        descriptor = _descriptor_from_evidence(
            current.get("evidence"), required=False
        )
        if is_recurrence_rewrite and descriptor is None:
            raise SkillContractError(
                "recurrence rewrite graph node is missing required lineage"
            )
        if lineage_active and descriptor is None:
            raise SkillContractError(
                "recurrence lineage descendant dropped its required proof"
            )
        if descriptor is None:
            continue
        if action not in {
            ("operator_rewrite", RECURRENCE_TEMPLATE),
            ("critical_net_reroute", "worst_source"),
        }:
            raise SkillContractError(
                "recurrence lineage descriptor is attached to an unsupported action"
            )
        if is_recurrence_rewrite:
            parent_id = current.get("parent_candidate_id")
            parent = memory.candidate_node(str(parent_id)) if parent_id else None
            if (
                parent is None
                or descriptor.get("source_candidate_id") != parent_id
                or descriptor.get("source_artifact_sha256")
                != parent.get("artifact_sha256")
            ):
                raise SkillContractError(
                    "recurrence rewrite lineage is not bound to its immediate seed"
                )
        validate_recurrence_lineage_candidate(
            memory,
            run_dir=run_dir,
            seed_candidate_id=candidate_id,
            evidence=current.get("evidence"),
            proof=current.get("eco_proof"),
            candidate_sha256=str(current.get("artifact_sha256") or ""),
        )
        lineage_active = True
    return lineage_active


def recurrence_lineage_context_for_seed(
    memory,
    run_dir: Path,
    seed: dict,
) -> Optional[RecurrenceLineageContext]:
    """Resolve the newest valid recurrence lineage in one seed's ancestry."""
    cursor: Optional[str] = str(seed.get("candidate_id") or "")
    limit = len((memory.candidate_graph or {}).get("nodes") or {}) + 1
    for _ in range(limit):
        if not cursor:
            break
        node = memory.candidate_node(cursor)
        if node is None:
            raise SkillContractError("recurrence lineage graph ancestor is missing")
        descriptor = _descriptor_from_evidence(node.get("evidence"), required=False)
        is_recurrence_rewrite = (
            node.get("skill") == "operator_rewrite"
            and node.get("target") == RECURRENCE_TEMPLATE
        )
        if is_recurrence_rewrite and descriptor is None:
            raise SkillContractError(
                "recurrence rewrite graph node is missing required lineage"
            )
        if descriptor is not None:
            validate_recurrence_lineage_candidate(
                memory,
                run_dir=run_dir,
                seed_candidate_id=cursor,
                evidence=node.get("evidence"),
                proof=node.get("eco_proof"),
                candidate_sha256=str(node.get("artifact_sha256") or ""),
            )
            source_id = str(descriptor["source_candidate_id"])
            source_node = memory.candidate_node(source_id)
            source_path = validate_owned_candidate_path(
                run_dir,
                source_id,
                Path(str(source_node.get("artifact_path") or "")),
            )
            store = _lineage_store(run_dir, create=False)
            bundle = _read_bundle(
                store / f"{descriptor['bundle_sha256']}.json",
                descriptor["bundle_sha256"],
            )
            return RecurrenceLineageContext(
                descriptor=descriptor,
                bundle=bundle,
                source_candidate_id=source_id,
                source_path=source_path,
                source_artifact_sha256=descriptor["source_artifact_sha256"],
            )
        proof = node.get("eco_proof") or {}
        if (
            isinstance(proof, dict)
            and proof.get("skill") == "operator_rewrite"
            and proof.get("template") == RECURRENCE_TEMPLATE
        ):
            raise SkillContractError(
                "recurrence family proof exists but its replay bundle is unavailable"
            )
        parent = node.get("parent_candidate_id")
        cursor = str(parent) if parent is not None else None
    if cursor is not None:
        raise SkillContractError("recurrence lineage graph ancestry contains a cycle")
    return None


async def prepare_recurrence_lineage_outputs(
    *,
    mcp,
    memory,
    run_dir: Path,
    action_dir: Path,
    selected_seed: dict,
    skill: str,
    target: str,
    params: dict[str, Any],
    outputs: tuple[SkillOutput, ...],
    deadline_monotonic: Optional[float],
) -> tuple[SkillOutput, ...]:
    """Persist initial lineage or renew it for a route-polished candidate."""
    if skill == "operator_rewrite" and target == RECURRENCE_TEMPLATE:
        source_id = str(selected_seed.get("candidate_id") or "")
        source_sha256 = str(selected_seed.get("artifact_sha256") or "")
        if _CANDIDATE_ID_RE.fullmatch(source_id) is None:
            raise SkillContractError("operator lineage source candidate ID is invalid")
        _require_digest(source_sha256, "operator graph source")
        return tuple(
            _persist_operator_output(
                output,
                action_dir=action_dir,
                run_dir=run_dir,
                source_candidate_id=source_id,
                source_sha256=source_sha256,
            )
            for output in outputs
        )

    if skill != "critical_net_reroute" or target != "worst_source":
        return outputs
    # Pin what the action *selects*: exactly the one net that owns the wall,
    # under no target pressure, producing one candidate.  What the action then
    # does to the proved structure is not pre-judged here -- the attestation
    # and strict validation below check the emitted topology and function
    # against the source artifact, which is evidence rather than a guess.
    # Locking `post_phys_opt` to None as well made this unsatisfiable: no
    # regime derives that combination, so every worst_source candidate on a
    # recurrence lineage was discarded before it was ever measured.  fir threw
    # away a 402.4 MHz candidate that way, against a 373.55 MHz incumbent.
    locked = {
        "selection_mode": "worst_source",
        "route_mode": "explore_group",
        "pressure_path_count": 0,
        "pressure_value_ns": None,
        "max_candidates": 1,
    }
    # An absent key means the skill's own default applies, so compare against
    # that rather than against None.  `pressure_path_count` defaults to exactly
    # the value locked here, and reading a missing key as None counted the
    # default as a violation.
    skill_defaults = {
        "selection_mode": "window",
        "route_mode": "delay_group",
        "pressure_path_count": 0,
        "pressure_value_ns": None,
        "max_candidates": None,
    }
    if any(
        params.get(key, skill_defaults[key]) != value
        for key, value in locked.items()
    ):
        raise SkillContractError("worst_source lineage action is not physically locked")
    if len(outputs) != 1:
        raise SkillContractError("worst_source lineage renewal requires one candidate")
    context = recurrence_lineage_context_for_seed(memory, run_dir, selected_seed)
    if context is None:
        return outputs

    renewed = []
    for ordinal, output in enumerate(outputs):
        candidate_sha256 = sha256_file(output.output_dcp)
        attestation = _payload(
            await mcp.call_vivado(
                "attest_proved_recurrence_family_candidate",
                {
                    "dcp_path": str(context.source_path),
                    "source_dcp_sha256": context.source_artifact_sha256,
                    "candidate_dcp_path": str(output.output_dcp),
                    "candidate_dcp_sha256": candidate_sha256,
                    "family_facts": context.bundle["family_facts"],
                    "candidate": context.bundle["candidate"],
                    "placement": context.bundle["placement"],
                    "postcheck_timeout_s": 600,
                },
                timeout=1900.0,
            ),
            field="candidate attestation",
        )
        report_path = Path(action_dir).resolve() / (
            f"critical_net_reroute_recurrence_validation_{ordinal + 1:02d}.json"
        )
        strict_report = await run_strict_validation_worker(
            context.source_path,
            output.output_dcp,
            report_path,
            vectors=MIN_POST_EMISSION_VECTORS,
            precheck_vectors=MIN_POST_EMISSION_PRECHECK_VECTORS,
            timeout_s=1800.0,
            deadline_monotonic=deadline_monotonic,
        )
        proof = build_operator_candidate_proof(
            RECURRENCE_TEMPLATE,
            context.bundle["candidate"]["authorization"],
            candidate_sha256,
            strict_report,
            emitted_topology_attestation=attestation,
        )
        descriptor = _descriptor_for_proof(
            bundle_sha256=context.descriptor["bundle_sha256"],
            source_candidate_id=context.source_candidate_id,
            source_sha256=context.source_artifact_sha256,
            family_sha256=context.descriptor["family_contract_sha256"],
            placement_sha256=context.descriptor["placement_manifest_sha256"],
            proof=proof,
        )
        evidence = copy.deepcopy(output.evidence or {})
        if RECURRENCE_LINEAGE_KEY in evidence:
            raise SkillContractError("route skill attempted to supply recurrence lineage")
        evidence[RECURRENCE_LINEAGE_KEY] = descriptor
        _validate_bundle_binding(
            context.bundle,
            descriptor,
            proof,
            source_sha256=context.source_artifact_sha256,
            candidate_sha256=candidate_sha256,
        )
        renewed.append(SkillOutput(output.output_dcp, output.label, evidence, proof))
    return tuple(renewed)
