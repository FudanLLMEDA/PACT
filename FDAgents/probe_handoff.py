"""Trusted, seed-bound handoffs between read-only operator probes.

Large path arrays are useful to deterministic mining but harmful in an LLM
prompt. This module keeps the full machine-readable payload inside the live
MCP client and returns only bounded, name-free summaries to the model.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from pathlib import Path
from typing import Any, Optional

from .artifacts import atomic_write_json


LATEST_CRITICAL_PATHS = "latest_critical_paths"
MAX_HANDOFF_PATHS = 64
MAX_PATH_ITEMS = 64
MAX_RAW_PATH_ITEMS = 4096
# The deterministic miner retains every bounded family.  Luna only needs the
# highest-coverage representatives plus stable refs for the next exact probe;
# keeping this small avoids turning a 40-path machine handoff into a large or
# syntactically truncated prompt payload.
MAX_PUBLIC_FAMILIES_PER_KIND = 4


class ProbeArgumentContractError(ValueError):
    """A typed probe argument is invalid before any MCP I/O occurs."""


class MissingProbeHandoffError(ValueError):
    """A read-only probe prerequisite has not yet been retained for this seed."""


_OPERATOR_MINING_INTEGER_RANGES = {
    "min_family_size": (2, 64),
    "max_families": (1, 32),
    "max_motif_cells": (3, 12),
}

_TRUSTED_PATH_HANDOFF_TOOLS = frozenset({
    "analyze_route_connections",
    "inspect_lut_pin_swap_candidates",
    "operator_mining",
    "recover_recurrence_contract",
    "recover_sequential_operator_contract",
    "recover_consumer_driven_operator_contract",
})

# Exact-probe reuse is intentionally durable across Agent resumes, but a
# semantic result must not survive a change in the implementation that
# produced it.  Keep this as a cheap, explicit ABI (no source-tree hashing or
# startup sweep).  Bump only the affected tool when its recovery semantics
# change.  The value participates in the private probe fingerprint and is
# never exposed as a Luna action argument.
_PROBE_IMPLEMENTATION_ABI = {
    "inspect_lut_pin_swap_candidates": 1,
    "operator_mining": 9,
    "recover_recurrence_contract": 1,
    "recover_sequential_operator_contract": 2,
    "recover_consumer_driven_operator_contract": 3,
    "screen_fixed_point_dsp_contract": 3,
    "recover_banked_select_predicate_contract": 1,
    "recover_banked_ram_operator_contract": 2,
    "recover_commutative_reassociation_contract": 1,
    "recover_registered_dsp_lane_contract": 3,
    "recover_registered_dead_state_contract": 1,
    "recover_control_to_data_state_equation_contract": 3,
    "recover_ff_srl_boundary_bridge_contract": 1,
    "recover_memory_write_buffer_forward_contract": 1,
    "recover_multilevel_control_distribution_contract": 1,
    "recover_multilevel_ram_control_distribution_contract": 1,
    "recover_registered_parity_reassociation_contract": 1,
}


def _canonical_server(server: str) -> str:
    value = str(server or "").strip().lower()
    return {
        "v": "vivado",
        "rw": "rapidwright",
    }.get(value, value)


def canonicalize_probe_arguments(tool_name: str, arguments: dict) -> dict:
    """Return the public, deterministic arguments used for probe identity.

    This normalizes only framework-owned aliases and defaults.  It never
    invents a target or silently clamps an invalid numeric value.
    """
    if not isinstance(arguments, dict):
        raise ProbeArgumentContractError("probe arguments must be an object")
    tool = str(tool_name or "").strip()
    canonical = dict(arguments)

    # The Vivado MCP schema calls this field ``clock``.  Older Luna replies
    # used the descriptive alias ``clock_name``; accepting it without moving
    # the value caused the server to ignore the requested contest clock.
    if tool in {"extract_critical_path_cells", "extract_critical_path_pins"}:
        alias = canonical.pop("clock_name", None)
        if alias is not None:
            if "clock" in canonical and canonical["clock"] != alias:
                raise ProbeArgumentContractError(
                    "probe clock and clock_name values conflict"
                )
            canonical["clock"] = alias

    if tool in _TRUSTED_PATH_HANDOFF_TOOLS:
        uses_explicit_connections = bool(
            tool == "analyze_route_connections"
            and canonical.get("connections") is not None
        )
        if not uses_explicit_connections:
            source = canonical.get("path_source", LATEST_CRITICAL_PATHS)
            if source != LATEST_CRITICAL_PATHS:
                raise ProbeArgumentContractError(
                    f"{tool} path_source must be {LATEST_CRITICAL_PATHS!r}; "
                    f"got {source!r}"
                )
            canonical["path_source"] = LATEST_CRITICAL_PATHS

    if tool == "operator_mining":
        _validate_operator_mining_arguments(canonical)
        canonical.setdefault("min_family_size", 3)
        canonical.setdefault("max_families", 16)
        canonical.setdefault("max_motif_cells", 8)
    elif tool == "inspect_lut_pin_swap_candidates":
        if set(canonical) != {"path_source"}:
            raise ProbeArgumentContractError(
                "inspect_lut_pin_swap_candidates accepts only the framework-owned "
                "path_source handoff"
            )
    elif tool == "screen_fixed_point_dsp_contract":
        if set(canonical) != {"family_ref"}:
            raise ProbeArgumentContractError(
                "screen_fixed_point_dsp_contract accepts only family_ref"
            )
        family_ref = canonical.get("family_ref")
        if not isinstance(family_ref, str) or not re.fullmatch(
            r"sequential_[1-9][0-9]*", family_ref
        ):
            raise ProbeArgumentContractError(
                "screen_fixed_point_dsp_contract family_ref must be a current "
                "sequential_N reference"
            )
    elif tool == "recover_consumer_driven_operator_contract":
        allowed = {"family_ref", "path_source"}
        if set(canonical) != allowed:
            raise ProbeArgumentContractError(
                "recover_consumer_driven_operator_contract accepts only "
                "family_ref and path_source"
            )
        family_ref = canonical.get("family_ref")
        if not isinstance(family_ref, str) or not re.fullmatch(
            r"sequential_[1-9][0-9]*", family_ref
        ):
            raise ProbeArgumentContractError(
                "recover_consumer_driven_operator_contract family_ref must be "
                "a current sequential_N reference"
            )
    elif tool == "analyze_route_connections":
        value = canonical.get("max_connections", 16)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
            raise ProbeArgumentContractError(
                "analyze_route_connections max_connections must be an integer "
                f"in 1..32; got {value!r}"
            )
        canonical["max_connections"] = value
    elif tool in {
        "recover_banked_select_predicate_contract",
        "recover_banked_ram_operator_contract",
        "recover_commutative_reassociation_contract",
        "recover_registered_dsp_lane_contract",
        "recover_registered_dead_state_contract",
        "recover_control_to_data_state_equation_contract",
        "recover_ff_srl_boundary_bridge_contract",
        "recover_memory_write_buffer_forward_contract",
        "recover_multilevel_control_distribution_contract",
        "recover_multilevel_ram_control_distribution_contract",
        "recover_registered_parity_reassociation_contract",
    }:
        if canonical:
            raise ProbeArgumentContractError(
                f"{tool} accepts no public arguments"
            )

    return canonical


def probe_request_fingerprint(
    *,
    seed_candidate_id: str,
    artifact_sha256: str,
    server: str,
    tool: str,
    arguments: dict,
) -> tuple[dict, str]:
    """Bind one read-only request to its exact immutable seed and semantics."""
    seed = str(seed_candidate_id or "")
    digest = str(artifact_sha256 or "").lower()
    if not seed or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ProbeArgumentContractError(
            "probe fingerprint requires an exact seed and artifact SHA256"
        )
    canonical_arguments = canonicalize_probe_arguments(tool, arguments)
    payload = {
        "version": 2,
        "seed_candidate_id": seed,
        "artifact_sha256": digest,
        "server": _canonical_server(server),
        "tool": str(tool or "").strip(),
        "arguments": canonical_arguments,
        "implementation_abi": _PROBE_IMPLEMENTATION_ABI.get(
            str(tool or "").strip(), 1
        ),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return canonical_arguments, hashlib.sha256(encoded).hexdigest()


def _validate_operator_mining_arguments(arguments: dict) -> None:
    for name, (low, high) in _OPERATOR_MINING_INTEGER_RANGES.items():
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProbeArgumentContractError(
                f"operator_mining {name} must be an integer in {low}..{high}; "
                f"got {type(value).__name__}"
            )
        if not low <= value <= high:
            raise ProbeArgumentContractError(
                f"operator_mining {name} must be an integer in {low}..{high}; "
                f"got {value}"
            )

    if "path_source" in arguments:
        value = arguments["path_source"]
        if value != LATEST_CRITICAL_PATHS:
            raise ProbeArgumentContractError(
                "operator_mining path_source must be "
                f"{LATEST_CRITICAL_PATHS!r}; got {value!r}"
            )


def _identity(probe_candidate: Optional[dict]) -> tuple[str, str]:
    candidate = probe_candidate or {}
    candidate_id = str(candidate.get("candidate_id") or "")
    artifact_sha256 = str(candidate.get("artifact_sha256") or "").lower()
    if not candidate_id or len(artifact_sha256) != 64:
        raise ValueError("trusted probe handoff requires exact seed identity")
    return candidate_id, artifact_sha256


def _store(mcp) -> dict[tuple[str, str], dict[str, Any]]:
    value = getattr(mcp, "_trusted_probe_handoffs", None)
    if value is None:
        value = _load_persisted_store(mcp)
        setattr(mcp, "_trusted_probe_handoffs", value)
    if not isinstance(value, dict):
        raise RuntimeError("trusted probe handoff store has invalid state")
    return value


def _persisted_store_path(mcp) -> Optional[Path]:
    root = getattr(mcp, "_server_run_dir", None) or getattr(mcp, "run_dir", None)
    if root is None:
        return None
    return Path(root) / ".trusted_probe_handoffs.json"


def _load_persisted_store(mcp) -> dict[tuple[str, str], dict[str, Any]]:
    path = _persisted_store_path(mcp)
    if path is None or not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        candidate_id = str(record.get("candidate_id") or "")
        artifact_sha256 = str(record.get("artifact_sha256") or "").lower()
        state = record.get("state")
        if (
            not candidate_id
            or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
            or not isinstance(state, dict)
        ):
            continue
        result[(candidate_id, artifact_sha256)] = state
    return result


def _persist_store(mcp) -> None:
    path = _persisted_store_path(mcp)
    if path is None:
        return
    records = [
        {
            "candidate_id": candidate_id,
            "artifact_sha256": artifact_sha256,
            "state": state,
        }
        for (candidate_id, artifact_sha256), state in sorted(_store(mcp).items())
        if isinstance(state, dict)
    ]
    atomic_write_json(path, {"version": 1, "records": records})
    try:
        path.chmod(0o600)
    except OSError:
        pass


def trusted_probe_handoff_available(
    mcp, *, candidate_id: str, artifact_sha256: str
) -> bool:
    state = _store(mcp).get((str(candidate_id), str(artifact_sha256).lower()))
    return bool(
        isinstance(state, dict)
        and state.get("critical_paths")
        and state.get("sequential_family_refs")
    )


def _registered_dead_state_manifest_path(mcp, artifact_sha256: str) -> Path:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("registered dead-state handoff requires exact seed identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("registered dead-state private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory / f"registered_dead_state_{digest}.tsv"


def resolve_registered_dead_state_handoff(
    mcp, *, artifact_sha256: str
) -> dict[str, Any]:
    """Resolve one same-seed public closure and its private mutation manifest."""
    digest = str(artifact_sha256 or "").lower()
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("registered_dead_state_contract")
        if isinstance(contract, dict):
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "registered dead-state contract is absent, stale, or ambiguous for the current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    summary = contract.get("summary") or {}
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or summary.get("mutation_eligible") is not True
        or re.fullmatch(
            r"[0-9a-f]{64}", str(contract.get("contract_digest") or "")
        ) is None
    ):
        raise ValueError("registered dead-state contract does not authorize a mutation probe")
    manifest = _registered_dead_state_manifest_path(mcp, digest)
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size <= 0:
        raise ValueError("registered dead-state private mutation manifest is unavailable")
    return {"public_contract": contract, "manifest_path": manifest}


# The recurrence family recovery is a pure function of the loaded design and
# costs about five minutes on a large DCP.  Bump when its semantics change so a
# resumed run cannot consume a payload the current code would not produce.
_RECURRENCE_FAMILY_FACTS_ABI = 1


def _recurrence_family_facts_path(mcp, artifact_sha256: str) -> Path:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("recurrence family handoff requires exact seed identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("recurrence family private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory / f"recurrence_family_{digest}.json"


def retain_recurrence_family_facts(
    mcp, *, artifact_sha256: str, facts: Any
) -> bool:
    """Retain one seed-bound family recovery for the mutation that follows.

    The screen already paid for this recovery on this exact artifact, so the
    mutation can consume the same payload instead of spending a second window
    rediscovering the identical family.  Retention is keyed by the design the
    payload describes, so a different seed never resolves it.
    """
    if not isinstance(facts, dict):
        return False
    digest = str(artifact_sha256 or "").lower()
    if (
        facts.get("design_sha256") != digest
        or facts.get("fresh_rediscovery") is not True
    ):
        return False
    try:
        atomic_write_json(
            _recurrence_family_facts_path(mcp, digest),
            {
                "abi": _RECURRENCE_FAMILY_FACTS_ABI,
                "design_sha256": digest,
                "facts": facts,
            },
        )
    except (OSError, ValueError, TypeError):
        return False
    return True


def resolve_recurrence_family_facts(
    mcp, *, artifact_sha256: str
) -> Optional[dict[str, Any]]:
    """Return the retained family recovery for this exact seed, or None."""
    digest = str(artifact_sha256 or "").lower()
    try:
        path = _recurrence_family_facts_path(mcp, digest)
    except ValueError:
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(record, dict)
        or record.get("abi") != _RECURRENCE_FAMILY_FACTS_ABI
        or record.get("design_sha256") != digest
    ):
        return None
    facts = record.get("facts")
    if not isinstance(facts, dict) or facts.get("design_sha256") != digest:
        return None
    return facts


def _control_state_paths(mcp, artifact_sha256: str) -> tuple[Path, Path]:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("control-state handoff requires exact seed identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("control-state private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stem = f"control_state_{digest}"
    return directory / f"{stem}.tsv", directory / f"{stem}_probe.tsv"


def resolve_control_state_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Resolve one anonymous state family to its exact same-seed private selector."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"control_state_[1-9][0-9]*", ref) is None:
        raise ValueError("control-state family_ref is invalid")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("control_state_equation_contract")
        if isinstance(contract, dict):
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "control-state contract is absent, stale, or ambiguous for the current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    selected = next(
        (
            item for item in contract.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == ref
        ),
        None,
    )
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or not isinstance(selected, dict)
        or not isinstance(selected.get("state_count"), int)
        or selected["state_count"] < 1
    ):
        raise ValueError("control-state contract does not authorize this family")
    manifest, report = _control_state_paths(mcp, digest)
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        for path in (manifest, report)
    ):
        raise ValueError("control-state private probe artifacts are unavailable")
    return {
        "public_contract": contract,
        "selected_family": selected,
        "manifest_path": manifest,
        "probe_report_path": report,
    }


def _ff_srl_paths(mcp, artifact_sha256: str) -> tuple[Path, Path]:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("FF-to-SRL handoff requires exact seed identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("FF-to-SRL private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stem = f"ff_srl_{digest}"
    return directory / f"{stem}.tsv", directory / f"{stem}_probe.tsv"


def resolve_ff_srl_boundary_bridge_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Resolve one anonymous direct-FF family to its same-seed private selector."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"ff_srl_[1-9][0-9]*", ref) is None:
        raise ValueError("FF-to-SRL family_ref is invalid")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("ff_srl_boundary_bridge_contract")
        if isinstance(contract, dict):
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "FF-to-SRL contract is absent, stale, or ambiguous for the current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    selected = next(
        (
            item for item in contract.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == ref
        ),
        None,
    )
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or not isinstance(selected, dict)
        or not isinstance(selected.get("chain_count"), int)
        or selected["chain_count"] < 1
    ):
        raise ValueError("FF-to-SRL contract does not authorize this family")
    manifest, report = _ff_srl_paths(mcp, digest)
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        for path in (manifest, report)
    ):
        raise ValueError("FF-to-SRL private probe artifacts are unavailable")
    return {
        "public_contract": contract,
        "selected_family": selected,
        "manifest_path": manifest,
        "probe_report_path": report,
    }


def _memory_forward_paths(mcp, artifact_sha256: str) -> tuple[Path, Path]:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("memory-forward handoff requires exact seed identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("memory-forward private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stem = f"memory_forward_{digest}"
    return directory / f"{stem}.json", directory / f"{stem}_probe.json"


def resolve_memory_forward_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Resolve one anonymous memory family to its same-seed private contract."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"memory_forward_[1-9][0-9]*", ref) is None:
        raise ValueError("memory-forward family_ref is invalid")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("memory_write_buffer_forward_contract")
        if isinstance(contract, dict):
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "memory-forward contract is absent, stale, or ambiguous for the current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    selected = next(
        (
            item for item in contract.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == ref
        ),
        None,
    )
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or not isinstance(selected, dict)
        or not isinstance(selected.get("data_width"), int)
        or selected["data_width"] < 1
    ):
        raise ValueError("memory-forward contract does not authorize this family")
    manifest, report = _memory_forward_paths(mcp, digest)
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        for path in (manifest, report)
    ):
        raise ValueError("memory-forward private probe artifacts are unavailable")
    return {
        "public_contract": contract,
        "selected_family": selected,
        "manifest_path": manifest,
        "probe_report_path": report,
    }


def _multilevel_control_paths(mcp, artifact_sha256: str) -> tuple[Path, Path]:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("control-localization handoff requires exact seed identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("control-localization private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stem = f"multilevel_control_{digest}"
    return directory / f"{stem}.json", directory / f"{stem}_probe.json"


def resolve_multilevel_control_localization_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Resolve one anonymous control DAG to its same-seed private closure."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"multilevel_control_[1-9][0-9]*", ref) is None:
        raise ValueError("control-localization family_ref is invalid")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("multilevel_control_distribution_contract")
        if isinstance(contract, dict):
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "control-localization contract is absent, stale, or ambiguous for "
            "the current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    selected = next(
        (
            item for item in contract.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == ref
        ),
        None,
    )
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or not isinstance(selected, dict)
        or not isinstance(selected.get("closure_cell_count"), int)
        or selected["closure_cell_count"] < 1
        or not isinstance(selected.get("sink_count"), int)
        or selected["sink_count"] < 1
    ):
        raise ValueError("control-localization contract does not authorize this family")
    manifest, report = _multilevel_control_paths(mcp, digest)
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        for path in (manifest, report)
    ):
        raise ValueError("control-localization private probe artifacts are unavailable")
    return {
        "public_contract": contract,
        "selected_family": selected,
        "manifest_path": manifest,
        "probe_report_path": report,
    }


def _multilevel_ram_control_paths(
    mcp, artifact_sha256: str, run_id: str
) -> tuple[Path, Path]:
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("RAM-control handoff requires exact seed identity")
    if re.fullmatch(r"nr-[0-9a-f]{32}", str(run_id or "")) is None:
        raise ValueError("RAM-control handoff requires exact run identity")
    root = Path(getattr(mcp, "_server_run_dir", None) or mcp.run_dir)
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("RAM-control private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stem = f"multilevel_ram_control_{run_id}_{digest}"
    return directory / f"{stem}.json", directory / f"{stem}_probe.json"


def resolve_multilevel_ram_control_localization_handoff(
    mcp, *, artifact_sha256: str, run_id: str, family_ref: str
) -> dict[str, Any]:
    """Resolve one anonymous RAM-control DAG to its same-seed private closure."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"multilevel_ram_control_[1-9][0-9]*", ref) is None:
        raise ValueError("RAM-control family_ref is invalid")
    if re.fullmatch(r"nr-[0-9a-f]{32}", str(run_id or "")) is None:
        raise ValueError("RAM-control run_id is invalid")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("multilevel_ram_control_distribution_contract")
        if isinstance(contract, dict) and contract.get("run_id") == run_id:
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "RAM-control contract is absent, stale, or ambiguous for the current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    selected = next(
        (
            item for item in contract.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == ref
        ),
        None,
    )
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or not isinstance(selected, dict)
        or not isinstance(selected.get("closure_cell_count"), int)
        or selected["closure_cell_count"] < 1
        or not isinstance(selected.get("sink_count"), int)
        or selected["sink_count"] < 2
        or not isinstance(selected.get("clock_region_count"), int)
        or selected["clock_region_count"] < 2
    ):
        raise ValueError("RAM-control contract does not authorize this family")
    manifest, report = _multilevel_ram_control_paths(mcp, digest, run_id)
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        for path in (manifest, report)
    ):
        raise ValueError("RAM-control private probe artifacts are unavailable")
    return {
        "public_contract": contract,
        "selected_family": selected,
        "manifest_path": manifest,
        "probe_report_path": report,
    }


def _registered_parity_paths(
    mcp, artifact_sha256: str
) -> tuple[Path, Path]:
    root = Path(mcp.run_dir).resolve()
    digest = str(artifact_sha256 or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("registered parity artifact identity is invalid")
    directory = root / ".trusted_operator_manifests"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise ValueError("registered parity private manifest root is unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stem = f"registered_parity_{digest}"
    return directory / f"{stem}.json", directory / f"{stem}_probe.json"


def resolve_registered_parity_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Resolve an anonymous parity family to its private same-seed closure."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"registered_parity_[1-9][0-9]*", ref) is None:
        raise ValueError("registered parity family_ref is invalid")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        contract = state.get("registered_parity_reassociation_contract")
        if isinstance(contract, dict):
            matches.append(contract)
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "registered parity contract is absent, stale, or ambiguous for the "
            "current seed"
        )
    contract = json.loads(json.dumps(next(iter(unique.values()))))
    selected = next(
        (
            item for item in contract.get("families") or []
            if isinstance(item, dict) and item.get("family_ref") == ref
        ),
        None,
    )
    if (
        contract.get("status") != "success"
        or contract.get("private_mutation_manifest_retained") is not True
        or not isinstance(selected, dict)
        or not isinstance(selected.get("odd_leaf_count"), int)
        or selected["odd_leaf_count"] < 2
        or not isinstance(selected.get("registered_endpoint_count"), int)
        or selected["registered_endpoint_count"] < 1
    ):
        raise ValueError("registered parity contract does not authorize this family")
    manifest, report = _registered_parity_paths(mcp, digest)
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        for path in (manifest, report)
    ):
        raise ValueError("registered parity private probe artifacts are unavailable")
    return {
        "public_contract": contract,
        "selected_family": selected,
        "manifest_path": manifest,
        "probe_report_path": report,
    }


def resolve_sequential_family_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Resolve one short public family ref to current-seed trusted probe data.

    The returned object stays inside the framework.  Cell names, path arrays,
    candidate IDs and digests are never copied into the LLM action payload.
    """
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if len(digest) != 64 or not ref.startswith("sequential_"):
        raise ValueError("operator rewrite requires a current sequential family_ref")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        candidate_id = (state.get("sequential_family_refs") or {}).get(ref)
        paths = state.get("critical_paths")
        if isinstance(candidate_id, str) and candidate_id and paths is not None:
            mining = state.get("operator_mining") or {}
            matches.append({
                "candidate_id": candidate_id,
                "critical_paths_data": _validate_paths(paths),
                "resource_census": dict(mining.get("resource_census") or {}),
                "family": next((
                    json.loads(json.dumps(item))
                    for item in mining.get("sequential_write_families") or []
                    if isinstance(item, dict)
                    and item.get("candidate_id") == candidate_id
                ), {}),
            })
    unique = {
        hashlib.sha256(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "sequential family_ref is absent, stale, or ambiguous for the current seed"
        )
    return json.loads(json.dumps(next(iter(unique.values()))))


def resolve_consumer_rewrite_handoff(
    mcp, *, artifact_sha256: str, family_ref: str
) -> dict[str, Any]:
    """Return one private live consumer bundle for framework execution only."""
    digest = str(artifact_sha256 or "").lower()
    ref = str(family_ref or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None or re.fullmatch(
        r"sequential_[1-9][0-9]*", ref
    ) is None:
        raise ValueError("consumer rewrite requires a current sequential family_ref")
    matches = []
    for (_, stored_sha256), state in _store(mcp).items():
        if stored_sha256 != digest or not isinstance(state, dict):
            continue
        candidate_id = (state.get("sequential_family_refs") or {}).get(ref)
        retained = state.get("consumer_contracts") or {}
        contracts = list(retained.values()) if isinstance(retained, dict) else []
        legacy = state.get("consumer_contract")
        if isinstance(legacy, dict):
            contracts.append(legacy)
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            private = contract.get("private_contract") or {}
            bundle = private.get("bundle")
            if (
                isinstance(candidate_id, str)
                and contract.get("candidate_id") == candidate_id
                and contract.get("design_sha256") == digest
                and contract.get("fresh_rediscovery") is True
                and (contract.get("action_assessment") or {}).get("status")
                == "executable"
                and isinstance(bundle, dict)
            ):
                matches.append({
                    "candidate_id": candidate_id,
                    "contract_sha256": contract.get("contract_sha256"),
                    "bundle": bundle,
                    "public_contract": contract.get("public_contract") or {},
                })
    unique = {
        hashlib.sha256(json.dumps(
            item, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(): item
        for item in matches
    }
    if len(unique) != 1:
        raise ValueError(
            "consumer rewrite contract is absent, stale, ambiguous, or not executable"
        )
    return json.loads(json.dumps(next(iter(unique.values()))))


def ordered_sequential_family_screen_refs(
    mcp,
    *,
    artifact_sha256: str,
    selected_family_ref: str,
    candidate_id: str = "",
) -> list[str]:
    """Return live endpoint-owner families in semantic-screen order.

    ``sequential_N`` numbering follows timing/discovery order and is therefore
    not a stable semantic priority.  The selected family is screened first,
    followed by other complete registered words that actually own sampled
    endpoints.  Register-support families with no endpoint ownership are not
    promoted merely because they are adjacent in the ranked mining payload.

    This function returns only short current-seed refs.  Object names and
    candidate identifiers remain in the trusted handoff store.
    """
    digest = str(artifact_sha256 or "").lower()
    selected = str(selected_family_ref or "")
    if re.fullmatch(r"sequential_[1-9][0-9]*", selected) is None:
        raise ValueError("semantic screen requires a current sequential_N ref")

    store = _store(mcp)
    current_key = (str(candidate_id or ""), digest)
    current_state = store.get(current_key) if current_key[0] else None
    matching_states = (
        [current_state]
        if isinstance(current_state, dict)
        else [
            state
            for (_, stored_sha256), state in store.items()
            if stored_sha256 == digest and isinstance(state, dict)
        ]
    )
    # Direct programmatic and cache-resume callers may not have a live mining
    # handoff.  Preserve their selected-family behavior without inventing peers.
    if not matching_states:
        return [selected]
    if len(matching_states) != 1:
        raise ValueError("current sequential family screen state is ambiguous")

    state = matching_states[0]
    refs = state.get("sequential_family_refs") or {}
    if refs.get(selected) is None:
        raise ValueError("selected sequential family is stale for current seed")
    by_candidate = {
        candidate_id: family_ref
        for family_ref, candidate_id in refs.items()
        if isinstance(family_ref, str) and isinstance(candidate_id, str)
    }
    ordered = [selected]
    families = (
        (state.get("operator_mining") or {}).get("sequential_write_families")
        or []
    )
    for family in families:
        if not isinstance(family, dict):
            continue
        family_ref = by_candidate.get(family.get("candidate_id"))
        if not family_ref or family_ref in ordered:
            continue
        mapping = family.get("bit_mapping") or {}
        coverage = family.get("critical_path_coverage") or {}
        endpoint_count = coverage.get("endpoint_path_count")
        owns_endpoint = (
            int(endpoint_count) > 0
            if isinstance(endpoint_count, int) and not isinstance(endpoint_count, bool)
            else endpoint_count is None
        )
        complete_word = (
            mapping.get("status") == "explicit_unique_contiguous"
            and mapping.get("min_index") == 0
        )
        if owns_endpoint and complete_word:
            ordered.append(family_ref)
    return ordered


def _compress_path(path: list[str]) -> list[str]:
    """Retain endpoints and representative interior cells on deep paths."""
    if len(path) <= MAX_PATH_ITEMS:
        return list(path)
    endpoint_items = 16
    head = path[:endpoint_items]
    tail = path[-endpoint_items:]
    interior = path[endpoint_items:-endpoint_items]
    slots = MAX_PATH_ITEMS - len(head) - len(tail)
    sampled = [
        interior[(index * len(interior)) // slots]
        for index in range(slots)
    ]
    return head + sampled + tail


def compact_critical_paths_for_operator_mining(value: Any) -> list[list[str]]:
    """Validate and deterministically fit live paths to the miner ABI.

    Vivado can return critical paths deeper than the RapidWright
    ``operator_mining`` limit of 64 items.  Keep this normalization in one
    framework-owned seam so direct Agent and standalone cold-provider calls
    cannot accidentally send the unbounded extraction payload.
    """
    return _validate_paths(value)


def _validate_paths(value: Any) -> list[list[str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("critical path extraction returned no path array")
    if len(value) > MAX_HANDOFF_PATHS:
        raise ValueError(
            f"critical path handoff exceeds {MAX_HANDOFF_PATHS} paths"
        )
    result: list[list[str]] = []
    for index, path in enumerate(value):
        if not isinstance(path, list) or not path or len(path) > MAX_RAW_PATH_ITEMS:
            raise ValueError(
                f"critical path {index} must contain 1..{MAX_RAW_PATH_ITEMS} items"
            )
        if any(
            not isinstance(item, str) or not item or len(item) > 1024
            for item in path
        ):
            raise ValueError(f"critical path {index} contains an invalid item")
        result.append(_compress_path(path))
    return result


def prepare_probe_arguments(
    mcp,
    tool_name: str,
    arguments: dict,
    probe_candidate: Optional[dict],
) -> dict:
    """Inject exact prior probe data without asking the LLM to reproduce it."""
    prepared = canonicalize_probe_arguments(tool_name, arguments or {})
    if tool_name == "recover_registered_parity_reassociation_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("registered parity probe candidate is unavailable")
        manifest, report = _registered_parity_paths(mcp, artifact_sha256)
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "report_path": str(report),
            "manifest_path": str(manifest),
            "timeout_s": 900.0,
        }
    if tool_name == "recover_multilevel_control_distribution_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("control-localization probe candidate is unavailable")
        manifest, report = _multilevel_control_paths(mcp, artifact_sha256)
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "report_path": str(report),
            "manifest_path": str(manifest),
            "timeout_s": 900.0,
        }
    if tool_name == "recover_multilevel_ram_control_distribution_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("RAM-control probe candidate is unavailable")
        run_id = str(candidate.get("run_id") or "")
        manifest, report = _multilevel_ram_control_paths(
            mcp, artifact_sha256, run_id
        )
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "run_id": run_id,
            "report_path": str(report),
            "manifest_path": str(manifest),
            "timeout_s": 900.0,
        }
    if tool_name == "recover_memory_write_buffer_forward_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("memory-forward probe candidate is unavailable")
        manifest, report = _memory_forward_paths(mcp, artifact_sha256)
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "report_path": str(report),
            "manifest_path": str(manifest),
            "timeout_s": 900.0,
        }
    if tool_name == "recover_ff_srl_boundary_bridge_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("FF-to-SRL probe candidate is unavailable")
        manifest, report = _ff_srl_paths(mcp, artifact_sha256)
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "report_path": str(report),
            "manifest_path": str(manifest),
            "timeout_s": 900.0,
        }
    if tool_name == "recover_control_to_data_state_equation_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("control-state probe candidate is unavailable")
        manifest, report = _control_state_paths(mcp, artifact_sha256)
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "report_path": str(report),
            "manifest_path": str(manifest),
            "timeout_s": 900.0,
        }
    if tool_name in {
        "recover_banked_select_predicate_contract",
        "recover_banked_ram_operator_contract",
        "recover_commutative_reassociation_contract",
        "recover_registered_dsp_lane_contract",
    }:
        _, artifact_sha256 = _identity(probe_candidate)
        candidate = probe_candidate or {}
        artifact = Path(str(candidate.get("artifact_path") or "")).resolve()
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("semantic operator probe candidate is unavailable")
        report_name = {
            "recover_banked_select_predicate_contract": "banked_select_predicate_probe.tsv",
            "recover_banked_ram_operator_contract": "banked_ram_operator_probe.tsv",
            "recover_commutative_reassociation_contract": "commutative_reassociation_probe.tsv",
            "recover_registered_dsp_lane_contract": "registered_dsp_lane_probe.tsv",
        }[tool_name]
        return {
            "dcp_path": str(artifact),
            "source_dcp_sha256": artifact_sha256,
            "report_path": str(
                Path(
                    getattr(mcp, "_server_run_dir", None) or mcp.run_dir
                )
                / report_name
            ),
            "timeout_s": 900.0,
        }
    if tool_name == "recover_registered_dead_state_contract":
        _, artifact_sha256 = _identity(probe_candidate)
        return {
            "source_dcp_sha256": artifact_sha256,
            "manifest_path": str(
                _registered_dead_state_manifest_path(mcp, artifact_sha256)
            ),
        }
    if tool_name not in _TRUSTED_PATH_HANDOFF_TOOLS:
        return prepared

    key = _identity(probe_candidate)
    state = _store(mcp).get(key) or {}
    if tool_name == "analyze_route_connections" and prepared.get(
        "connections"
    ) is not None:
        # Explicit physical-net/sink pairs are already a complete typed input.
        # Do not mix them with a second, implicit path-derived request.
        prepared.pop("path_source", None)
        return prepared
    requested_source = prepared.pop("path_source", LATEST_CRITICAL_PATHS)
    if requested_source != LATEST_CRITICAL_PATHS:
        raise ValueError(
            f"unsupported critical path handoff {requested_source!r}"
        )
    paths = state.get("critical_paths")
    if paths is None:
        # Keep direct programmatic callers compatible, but the staged Agent is
        # prompted to use the trusted handoff and never copy the array.
        paths = prepared.get("critical_paths_data")
    if paths is None:
        raise MissingProbeHandoffError(
            "current seed has no retained critical-path extraction"
        )
    paths = _validate_paths(paths)
    if tool_name == "inspect_lut_pin_swap_candidates":
        # The public Luna decision is intentionally name-free.  Resolve the
        # concrete LUT inputs mechanically from the retained exact-seed paths,
        # using the same bounded ordering as the mutating lut_pin_swap skill.
        cell_pins = []
        seen = set()
        for path in paths[:5]:
            for pin in path:
                if (
                    pin not in seen
                    and "/DSP_" not in pin
                    and re.search(r"/I[0-5]$", pin)
                ):
                    cell_pins.append(pin)
                    seen.add(pin)
                    if len(cell_pins) >= 8:
                        break
            if len(cell_pins) >= 8:
                break
        return {"cell_pins": cell_pins}
    prepared["critical_paths_data"] = paths

    if tool_name == "analyze_route_connections":
        return prepared
    if tool_name == "recover_recurrence_contract":
        family_ref = prepared.pop("family_ref", None)
        family_refs = state.get("recurrence_family_refs") or {}
        if family_ref is not None:
            candidate_id = family_refs.get(str(family_ref))
            if not candidate_id:
                raise ValueError(
                    f"unknown recurrence family_ref {family_ref!r} for this seed"
                )
            prepared["candidate_id"] = candidate_id
        elif family_refs and prepared.get("candidate_id") not in set(
            family_refs.values()
        ):
            raise ValueError(
                "recover_recurrence_contract requires a current family_ref"
            )
    elif tool_name in {
        "recover_sequential_operator_contract",
        "recover_consumer_driven_operator_contract",
    }:
        family_ref = prepared.pop("family_ref", None)
        family_refs = state.get("sequential_family_refs") or {}
        if family_ref is not None:
            candidate_id = family_refs.get(str(family_ref))
            if not candidate_id:
                raise ValueError(
                    f"unknown sequential family_ref {family_ref!r} for this seed"
                )
            prepared["candidate_id"] = candidate_id
        elif family_refs and prepared.get("candidate_id") not in set(
            family_refs.values()
        ):
            raise ValueError(
                f"{tool_name} requires a current family_ref"
            )
    return prepared


def _critical_path_summary(
    paths: list[list[str]], *, deep_paths_compacted: bool = False
) -> dict:
    lengths = [len(path) for path in paths]
    return {
        "status": "success",
        "handoff": LATEST_CRITICAL_PATHS,
        "path_count": len(paths),
        "path_item_count": sum(lengths),
        "path_length_items": {
            "min": min(lengths),
            "median": float(statistics.median(lengths)),
            "max": max(lengths),
        },
        "framework_retained_complete_paths": not deep_paths_compacted,
        "deep_paths_compacted": deep_paths_compacted,
        "llm_payload_contains_object_names": False,
        "next_probe": {
            "server": "rapidwright",
            "tool": "operator_mining",
            "arguments": {"path_source": LATEST_CRITICAL_PATHS},
        },
    }


def _family_summary(item: dict, family_ref: str, fields: tuple[str, ...]) -> dict:
    result = {"family_ref": family_ref}
    for field in fields:
        if field in item:
            result[field] = item[field]
    return result


def _public_bit_mapping(value: Any) -> dict:
    item = value if isinstance(value, dict) else {}
    return {
        key: item.get(key)
        for key in (
            "status", "member_count", "min_index", "max_index"
        )
        if key in item
    }


def _public_physical_footprint(value: Any) -> dict:
    item = value if isinstance(value, dict) else {}
    return {
        key: item.get(key)
        for key in (
            "placed_member_count", "tile_x_span", "clock_region_count"
        )
        if key in item
    }


def _public_path_wall_coverage(value: Any) -> dict:
    item = value if isinstance(value, dict) else {}
    return {
        key: item.get(key)
        for key in (
            "ordered_path_count", "covered_path_count", "covered_path_ranks",
            "earliest_covered_rank", "covers_worst_path",
            "endpoint_path_count", "endpoint_path_ranks",
            "covers_worst_endpoint", "top5_covered_count",
            "top10_covered_count", "all_path_coverage_ratio",
            "rank_semantics",
        )
        if key in item
    }


def _public_operator_scope_census(
    value: Any, family_refs: dict[str, str]
) -> dict:
    item = value if isinstance(value, dict) else {}
    by_candidate = {
        candidate_id: family_ref
        for family_ref, candidate_id in family_refs.items()
    }
    peers = []
    for peer in item.get("peer_families") or []:
        if not isinstance(peer, dict):
            continue
        candidate_id = peer.get("candidate_id")
        public = {
            key: peer.get(key)
            for key in (
                "evidence_scope", "covered_path_count",
                "additional_path_count", "additional_path_ranks",
                "overlap_path_count", "covers_worst_path",
            )
            if key in peer
        }
        if candidate_id in by_candidate:
            public["family_ref"] = by_candidate[candidate_id]
        peers.append(public)
    return {
        key: item.get(key)
        for key in (
            "selected_scope", "complete_generating_operator_contract_recovered",
            "ordered_path_count", "selected_covered_path_count",
            "selected_covered_path_ranks", "uncovered_path_count",
            "uncovered_path_ranks", "peer_family_count", "interpretation",
        )
        if key in item
    } | {"peer_families": peers}


def _public_judgment_chain(
    value: Any, family_refs: Optional[dict[str, str]] = None
) -> dict:
    """Remove trusted identities while preserving the complete reasoning ABI."""
    refs = family_refs or {}
    by_candidate = {candidate_id: ref for ref, candidate_id in refs.items()}
    private_keys = {
        "candidate_id", "design_sha256", "contract_sha256",
        "members", "critical_members", "member_examples",
        "cell", "net", "site", "path", "representative_net",
    }

    def scrub(node):
        if isinstance(node, list):
            return [scrub(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {}
        for key, item in node.items():
            if key == "candidate_id":
                if item in by_candidate:
                    result["family_ref"] = by_candidate[item]
                continue
            if key in private_keys:
                continue
            result[str(key)] = scrub(item)
        return result

    return scrub(value) if isinstance(value, dict) else {}


def _public_generating_operator_closure(value: Any) -> dict:
    """Expose wall closure facts without leaking internal family identities."""
    closure = value if isinstance(value, dict) else {}
    groups = []
    for item in closure.get("wall_groups") or []:
        if not isinstance(item, dict):
            continue
        groups.append({
            key: item.get(key)
            for key in (
                "group_ref", "covered_path_ranks", "covered_path_count",
                "family_count", "evidence_relation",
            )
            if key in item
        } | {
            "endpoint_owner_family_count": len(
                item.get("endpoint_owner_candidate_ids") or []
            ),
            "non_endpoint_support_family_count": len(
                item.get("non_endpoint_support_candidate_ids") or []
            ),
        })
    relations = [
        {
            key: item.get(key)
            for key in (
                "left_group_ref", "right_group_ref", "relation",
                "overlap_path_ranks",
            )
            if key in item
        }
        for item in closure.get("cross_group_relations") or []
        if isinstance(item, dict)
    ]
    return {
        key: closure.get(key)
        for key in (
            "version", "status", "authority", "selected_family_scope",
            "selected_group_ref",
            "complete_generating_operator_contract_recovered",
            "family_count", "wall_group_count", "ordered_path_count",
            "all_family_covered_path_count",
            "all_family_covered_path_ranks", "uncovered_path_count",
            "uncovered_path_ranks", "closed_obligations",
            "open_obligations", "interpretation",
        )
        if key in closure
    } | {
        "wall_groups": groups,
        "cross_group_relations": relations,
    }


def _operator_mining_summary(
    payload: dict,
) -> tuple[dict, dict[str, str], dict[str, str]]:
    recurrence_refs: dict[str, str] = {}
    sequential_refs: dict[str, str] = {}
    motifs = []
    for index, item in enumerate(
        (payload.get("repeated_path_families") or [])[
            :MAX_PUBLIC_FAMILIES_PER_KIND
        ],
        start=1,
    ):
        family = _family_summary(
            item,
            f"motif_{index}",
            (
                "kind", "type_signature", "occurrence_count",
                "critical_path_count", "critical_path_coverage", "proof_status",
            ),
        )
        family.update({
            "family_ref_scope": "repeated_path_observation_only",
            "recover_recurrence_contract_eligible": False,
        })
        motifs.append(family)
    recurrences = []
    for index, item in enumerate(
        (payload.get("recurrence_boundary_families") or [])[
            :MAX_PUBLIC_FAMILIES_PER_KIND
        ],
        start=1,
    ):
        family_ref = f"recurrence_{index}"
        candidate_id = item.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            recurrence_refs[family_ref] = candidate_id
        public_recurrence = _family_summary(
            item,
            family_ref,
            (
                "kind", "source_register_count", "critical_path_count",
                "transition_signature", "proof_status",
            ),
        )
        public_recurrence.update({
            "family_ref_scope": "recurrence_boundary_contract",
            "recover_recurrence_contract_eligible": bool(candidate_id),
            "next_probe": {
                "server": "rapidwright",
                "tool": "recover_recurrence_contract",
                "arguments": {
                    "path_source": LATEST_CRITICAL_PATHS,
                    "family_ref": family_ref,
                },
            } if candidate_id else None,
        })
        recurrences.append(public_recurrence)
    transports = [
        _family_summary(
            item,
            f"transport_{index}",
            (
                "kind", "cell_type", "member_count", "critical_member_count",
                "column_span", "proof_status",
            ),
        )
        for index, item in enumerate(
            (payload.get("register_transport_families") or [])[
                :MAX_PUBLIC_FAMILIES_PER_KIND
            ],
            start=1,
        )
    ]
    sequential = []
    sequential_index = []
    for index, item in enumerate(
        payload.get("sequential_write_families") or [], start=1
    ):
        family_ref = f"sequential_{index}"
        candidate_id = item.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            sequential_refs[family_ref] = candidate_id
        index_family = _family_summary(
            item,
            family_ref,
            (
                "kind", "member_count", "critical_member_count",
                "critical_path_count", "proof_status",
            ),
        )
        index_family["critical_path_coverage"] = _public_path_wall_coverage(
            item.get("critical_path_coverage")
        )
        sequential_index.append(index_family)
        if index > MAX_PUBLIC_FAMILIES_PER_KIND:
            continue
        public_family = _family_summary(
            item,
            family_ref,
            (
                "kind", "grouping", "cell_type", "member_count",
                "critical_member_count", "critical_path_count",
                "proof_status", "mutation_eligible",
            ),
        )
        public_family["bit_mapping"] = _public_bit_mapping(
            item.get("bit_mapping")
        )
        public_family["physical_footprint"] = _public_physical_footprint(
            item.get("physical_footprint")
        )
        public_family["critical_path_coverage"] = _public_path_wall_coverage(
            item.get("critical_path_coverage")
        )
        sequential.append(public_family)
    clock_nets = list((payload.get("clock_footprint") or {}).get("clock_nets") or [])
    summary = {
        "status": payload.get("status", "success"),
        "version": payload.get("version"),
        "scope": payload.get("scope") or {},
        "resource_census": payload.get("resource_census") or {},
        "clock_footprint_summary": {
            "clock_net_count": len(clock_nets),
            "max_sink_site_count": max(
                (int(item.get("sink_site_count") or 0) for item in clock_nets),
                default=0,
            ),
            "max_hard_macro_sink_site_count": max(
                (
                    int(item.get("hard_macro_sink_site_count") or 0)
                    for item in clock_nets
                ),
                default=0,
            ),
        },
        "family_counts": {
            "repeated_path": len(payload.get("repeated_path_families") or []),
            "recurrence_boundary": len(
                payload.get("recurrence_boundary_families") or []
            ),
            "register_transport": len(
                payload.get("register_transport_families") or []
            ),
            "sequential_write": len(
                payload.get("sequential_write_families") or []
            ),
            "shown_per_kind": MAX_PUBLIC_FAMILIES_PER_KIND,
        },
        "framework_retained_complete_mining_payload": True,
        "public_family_lists_are_ranked_prefixes": True,
        "family_selection_semantics": (
            "Ranking is a compact discovery order, not a mutation priority. "
            "Compare critical_path_coverage, especially broad internal wall "
            "coverage versus endpoint ownership, before choosing a family_ref. "
            "Endpoint ownership says where sampled paths terminate; it does not "
            "prove that the endpoint family generated the upstream arithmetic "
            "wall or offers the largest removable cone. A broad non-endpoint "
            "family can be producer state feeding a narrower consumer shell. "
            "Use read-only contracts to compare those roles rather than treating "
            "endpoint count as mutation priority. Detailed rows are a ranked "
            "prefix, while sequential_write_index compactly exposes every valid "
            "current-seed sequential ref. Do not rerun mining with narrower "
            "bounds merely to obtain a short ref."
        ),
        "typed_family_ref_contracts": {
            "motif_N": (
                "observation-only repeated path shape; never pass to "
                "recover_recurrence_contract"
            ),
            "recurrence_N": (
                "exact current-seed recurrence boundary accepted by "
                "recover_recurrence_contract when its row marks eligibility"
            ),
            "sequential_N": (
                "exact current-seed sequential family accepted by sequential "
                "and consumer-driven contract probes"
            ),
        },
        "consumer_driven_family_comparison": {
            "question": (
                "Does a broadly covered internal sequential family feed a "
                "smaller observable registered consumer boundary, while a "
                "different family merely owns some path endpoints?"
            ),
            "compare": [
                "covered_path_count and covers_worst_path",
                "endpoint_path_count without assuming endpoint causality",
                "registered producer width and primitive composition",
                "observable consumer word dimensions and contraction",
                "clock, enable, reset/init and pipeline latency",
            ],
            "selection_semantics": (
                "This is a soft discriminator, not a trigger. Probe the live "
                "family whose producer/consumer role is unresolved and let Luna "
                "select only after comparing the returned evidence."
            ),
        },
        "families": {
            "repeated_path": motifs,
            "recurrence_boundary": recurrences,
            "register_transport": transports,
            "sequential_write": sequential,
            "sequential_write_index": sequential_index,
        },
        "template_compatibility": payload.get("template_compatibility") or [],
        "operator_judgment_chain": _public_judgment_chain(
            payload.get("operator_judgment_chain")
        ),
        "proof_status": payload.get("proof_status"),
        "limitations": payload.get("limitations") or [],
        "llm_payload_contains_object_names": False,
        "recover_contract_with": {
            "tool": "recover_recurrence_contract",
            "arguments": {
                "path_source": LATEST_CRITICAL_PATHS,
                "family_ref": "one recurrence_N listed above",
            },
        },
        "recover_sequential_contract_with": {
            "tool": "recover_sequential_operator_contract",
            "arguments": {
                "path_source": LATEST_CRITICAL_PATHS,
                "family_ref": "one sequential_N listed above",
            },
        },
        "recover_consumer_driven_contract_with": {
            "tool": "recover_consumer_driven_operator_contract",
            "arguments": {
                "path_source": LATEST_CRITICAL_PATHS,
                "family_ref": "one sequential_N listed above",
            },
            "authority": "read_only_soft_discriminator",
        },
    }
    return summary, recurrence_refs, sequential_refs


def _recurrence_contract_summary(payload: dict) -> dict:
    plan = payload.get("rewrite_plan") or {}
    return {
        "status": payload.get("status"),
        "kind": payload.get("kind"),
        "fresh_rediscovery": payload.get("fresh_rediscovery"),
        "proof_status": payload.get("proof_status"),
        "boundary_extraction_complete": payload.get(
            "boundary_extraction_complete"
        ),
        "proof_input_eligible": payload.get("proof_input_eligible"),
        "mutation_eligible": payload.get("mutation_eligible"),
        "state_width": payload.get("state_width"),
        "dsp_c_width": payload.get("dsp_c_width"),
        "extension": payload.get("extension"),
        "controls_semantics": payload.get("controls_semantics"),
        "relay_endpoint_contract": payload.get("relay_endpoint_contract"),
        "transport_valid_transactions": (
            payload.get("transport_valid_transactions")
            if "transport_valid_transactions" in payload
            else plan.get("transport_valid_transactions")
        ),
        "missing_rewrite_facts": payload.get("missing_rewrite_facts") or [],
        "llm_payload_contains_object_names": False,
    }


def _sequential_contract_summary(
    payload: dict,
    family_ref: Optional[str],
    family_refs: Optional[dict[str, str]] = None,
) -> dict:
    controls = {}
    for role, item in (payload.get("controls") or {}).items():
        if not isinstance(item, dict):
            continue
        controls[str(role)] = {
            key: item.get(key)
            for key in (
                "status", "member_count", "missing_member_count",
                "distinct_net_count", "distinct_polarity_count",
            )
        }
    cone = payload.get("data_cone_traceback") or {}
    public_cone = {
        key: cone.get(key)
        for key in (
            "status", "traced_member_count", "untraced_member_count",
            "total_node_count", "max_depth", "primitive_type_counts",
            "boundary_type_counts", "distinct_cone_signature_count",
            "largest_repeated_signature_member_count",
        )
    }
    recovery_issue_count = len(payload.get("recovery_issues") or [])
    action_contract = payload.get("operator_action_contract") or {}
    coverage = (payload.get("family") or {}).get(
        "critical_path_coverage"
    ) or {}
    observed_family_role = str(
        (payload.get("family") or {}).get("evidence_scope") or ""
    )
    backend_available = bool(
        isinstance(family_ref, str)
        and family_ref.startswith("sequential_")
        and payload.get("fresh_rediscovery") is True
        and payload.get("structural_boundary_recovered") is True
        and payload.get("proof_input_eligible") is True
        and recovery_issue_count == 0
    )
    if backend_available:
        discriminator_options = [
            {
                "hypothesis": "banked_ram_boolean_and_control_localization",
                "server": "vivado",
                "tool": "recover_banked_ram_operator_contract",
                "arguments": {},
                "question": (
                    "whether the current wall is owned by a repeated registered "
                    "RAM/peer Boolean data operator, a registered-state Boolean "
                    "packet feeding bank-local RAM controls, or a near-tied mixture"
                ),
                "use_when_live_evidence": (
                    "RAM-to-register data paths and RAM address/enable/write-control "
                    "paths coexist; recover both high-level data and control "
                    "transactions before choosing a local backend"
                ),
                "authority": "read_only_soft_discriminator",
            },
            {
                "hypothesis": "banked_data_control_transaction",
                "server": "vivado",
                "tool": "recover_banked_select_predicate_contract",
                "arguments": {},
                "question": (
                    "whether registered RAM words participate in a select-then-"
                    "predicate transaction whose result scatters back through "
                    "bank-local write controls"
                ),
                "use_when_live_evidence": (
                    "a live registered-data/control path reaches a RAM write pin; "
                    "recover both the selected-word data operator and the local "
                    "enable/init/alternate-control operator before treating the "
                    "visible fanout carrier as the root cause"
                ),
                "authority": "read_only_soft_discriminator",
            },
            {
                "hypothesis": "consumer_boundary_contraction",
                "server": "rapidwright",
                "tool": "recover_consumer_driven_operator_contract",
                "arguments": {
                    "path_source": LATEST_CRITICAL_PATHS,
                    "family_ref": family_ref,
                },
                "question": (
                    "whether a broad internal registered producer is understood "
                    "only at a narrower or specialized observable consumer boundary"
                ),
                "use_when_live_evidence": (
                    "wall coverage and downstream observation remain unresolved; "
                    "compare producer/consumer dimensions, control, and latency"
                ),
                "authority": "read_only_soft_discriminator",
            },
            {
                "hypothesis": "local_fixed_point_product",
                "server": "vivado",
                "tool": "screen_fixed_point_dsp_contract",
                "arguments": {"family_ref": family_ref},
                "question": (
                    "whether the selected local D-cone itself closes as a product, "
                    "shift, wrap, or accumulation contract"
                ),
                "use_when_live_evidence": (
                    "local word-level product terms, loss boundaries, signedness, "
                    "and register schedule are already plausible"
                ),
                "authority": "read_only_soft_discriminator",
            },
            {
                "hypothesis": "generic_boolean_arithmetic_compressor",
                "candidate_target": "proved_arithmetic_compressor_v1",
                "question": (
                    "whether a bounded exact Boolean arithmetic cone is the useful "
                    "rewrite boundary without a proved higher-level product"
                ),
                "use_when_live_evidence": (
                    "the cone is complete and replayable but product or consumer "
                    "semantics remain unsupported"
                ),
                "authority": "local_operator_measurement_probe",
            },
            {
                "hypothesis": "constructive_registered_accumulator",
                "candidate_target": "constructive_accumulator_rebuild_v1",
                "question": (
                    "whether the complete registered LUT/CARRY cone is exactly a "
                    "constant-product-plus-add slice with a constructive emitted "
                    "compressor lowering"
                ),
                "use_when_live_evidence": (
                    "the current wall ends in a complete indexed register word, "
                    "two signed frontier words are plausible, and fixed-point DSP "
                    "or generic Boolean labels do not yet explain the full cone"
                ),
                "authority": "local_operator_measurement_probe",
            },
        ]
        backend_follow_up = {
            "status": "exploration_candidates_available",
            "skill": "operator_rewrite",
            "action_type": "local_operator_measurement_probe",
            "scope": "selected_registered_family_only",
            "observed_family_role": observed_family_role,
            "causal_role_evidence": {
                "covered_path_count": coverage.get("covered_path_count"),
                "endpoint_path_count": coverage.get("endpoint_path_count"),
                "covers_worst_path": coverage.get("covers_worst_path"),
                "covers_worst_endpoint": coverage.get(
                    "covers_worst_endpoint"
                ),
                "interpretation": (
                    "internal support, endpoint ownership, and generating-"
                    "operator ownership are distinct; compare a broad internal "
                    "producer against narrower endpoint families before "
                    "treating a visible carrier as the root cause"
                ),
            },
            "decision_owner": "luna",
            "discriminator_options": discriminator_options,
            "selection_rule": (
                "select the discriminator whose question best matches current "
                "live wall, boundary, data, control, and latency evidence; no "
                "option is a default or hard trigger"
            ),
            "candidate_actions": [
                {
                    "target": "registered_fir_data_cone_rewrite",
                    "params": {"family_ref": family_ref},
                    "mechanism": (
                        "recover a product-only registered FIR/conv data cone, "
                        "preserve its state and control shell, and replace the "
                        "proved LUT/CARRY arithmetic drivers with DSP arithmetic"
                    ),
                    "additional_proof_needed": (
                        "live product-only semantic frontier and exact data/control "
                        "replay followed by routed measurement"
                    ),
                    "authority": "local_operator_measurement_probe",
                    "selection_status": "exploration_only",
                },
                {
                    "target": "fixed_point_dsp_rebuild_v1",
                    "params": {"family_ref": family_ref},
                    "mechanism": (
                        "recover a fresh current-cone fixed-product/shift/wrap "
                        "contract and emit a DSP48E2-backed measurement candidate"
                    ),
                    "additional_proof_needed": (
                        "deterministic current-cone contract screen followed by "
                        "independent whole-DCP equivalence"
                    ),
                    "authority": "local_operator_measurement_probe",
                    "selection_status": "exploration_only",
                },
                {
                    "target": "proved_arithmetic_compressor_v1",
                    "params": {"family_ref": family_ref},
                    "mechanism": (
                        "rebuild the bounded Boolean arithmetic cone as an exact "
                        "LUT/CARRY compressor"
                    ),
                    "additional_proof_needed": (
                        "exact source-function and emitted-topology proof"
                    ),
                    "authority": "local_operator_measurement_probe",
                    "selection_status": "exploration_only",
                },
                {
                    "target": "constructive_accumulator_rebuild_v1",
                    "params": {"family_ref": family_ref},
                    "mechanism": (
                        "infer and canonically prove a signed constant-product-"
                        "plus-add slice, then replace all independently agreeing "
                        "live siblings with one constructive compressor lowering"
                    ),
                    "additional_proof_needed": (
                        "fresh complete cone extraction, exact per-bit canonical "
                        "comparison, emitted column proof and routed measurement"
                    ),
                    "authority": "local_operator_measurement_probe",
                    "selection_status": "exploration_only",
                },
            ],
            "not_a_hard_trigger": True,
            "semantics": (
                "This read-only registered-family contract exposes competing "
                "semantic questions, not a complete generating operator, forced "
                "action, or positive QoR claim. Luna chooses a discriminator from "
                "live evidence, then separately decides whether its typed candidate "
                "deserves a routed measurement. Each backend re-recovers the "
                "current contract before mutation."
            ),
        }
    else:
        unavailable_reasons = []
        if not isinstance(family_ref, str) or not family_ref.startswith(
            "sequential_"
        ):
            unavailable_reasons.append("current_family_ref_unresolved")
        if payload.get("fresh_rediscovery") is not True:
            unavailable_reasons.append("not_freshly_rediscovered")
        if payload.get("structural_boundary_recovered") is not True:
            unavailable_reasons.append("structural_boundary_not_recovered")
        if payload.get("proof_input_eligible") is not True:
            unavailable_reasons.append("proof_input_not_eligible")
        if recovery_issue_count:
            unavailable_reasons.append("recovery_issues_present")
        backend_follow_up = {
            "status": "unavailable",
            "reasons": unavailable_reasons,
            "not_a_hard_trigger": True,
        }
    return {
        "status": payload.get("status"),
        "kind": payload.get("kind"),
        "version": payload.get("version"),
        "fresh_rediscovery": payload.get("fresh_rediscovery"),
        "read_only": payload.get("read_only"),
        "bit_mapping": _public_bit_mapping(payload.get("bit_mapping")),
        "controls": controls,
        "driver_shape": payload.get("driver_shape") or {},
        "consumer_shape": payload.get("consumer_shape") or {},
        "hierarchy_boundary": payload.get("hierarchy_boundary") or {},
        "data_cone_traceback": public_cone,
        "semantic_hypotheses": payload.get("semantic_hypotheses") or {},
        "operator_scope_census": _public_operator_scope_census(
            payload.get("operator_scope_census"), family_refs or {}
        ),
        "generating_operator_closure": _public_generating_operator_closure(
            payload.get("generating_operator_closure")
        ),
        "operator_judgment_chain": _public_judgment_chain(
            payload.get("operator_judgment_chain"), family_refs or {}
        ),
        "physical_footprint": _public_physical_footprint(
            (payload.get("family") or {}).get("physical_footprint")
        ),
        "critical_path_coverage": _public_path_wall_coverage(coverage),
        "observed_family_role": observed_family_role,
        "qor_precheck": {
            "proof_input_eligible_is_not_qor_evidence": True,
            "measured_before_mutation": [
                "critical_path_and_endpoint_coverage",
                "registered_destination_footprint",
                "data_cone_depth_and_primitive_composition",
            ],
            "still_unknown_until_implementation": [
                "final_carry_width_reduction",
                "generated_cell_locality",
                "placed_timing",
                "routed_timing",
            ],
        },
        "classifications": payload.get("classifications") or [],
        "structural_boundary_recovered": payload.get(
            "structural_boundary_recovered"
        ),
        "proof_status": payload.get("proof_status"),
        "proof_input_eligible": payload.get("proof_input_eligible"),
        "proof_input_scope": payload.get("proof_input_scope"),
        "mutation_eligible": payload.get("mutation_eligible"),
        "operator_action_contract": {
            key: action_contract.get(key)
            for key in (
                "action_type", "scope", "authority",
                "complete_operator_rewrite_eligible", "reason",
            )
            if key in action_contract
        },
        "recovery_issue_count": recovery_issue_count,
        "global_proof_obligations": payload.get("global_proof_obligations") or [],
        "registered_backend_follow_up": backend_follow_up,
        "llm_payload_contains_object_names": False,
    }


def _consumer_contract_summary(
    payload: dict, family_ref: Optional[str]
) -> dict[str, Any]:
    public = payload.get("public_contract") or {}
    semantic = public.get("semantic_recovery") or {}
    assessment = payload.get("action_assessment") or {}
    action = assessment.get("candidate_action")
    if isinstance(action, dict):
        action = json.loads(json.dumps(action))
        params = action.setdefault("params", {})
        params["family_ref"] = family_ref
    return {
        "status": payload.get("status"),
        "kind": payload.get("kind"),
        "version": payload.get("version"),
        "fresh_rediscovery": payload.get("fresh_rediscovery"),
        "read_only": payload.get("read_only"),
        "family_ref": family_ref,
        "producer_registered_width": public.get("producer_registered_width"),
        "producer_primitive_type_counts": public.get(
            "producer_primitive_type_counts"
        ) or {},
        "current_wall_coverage": public.get("current_wall_coverage") or {},
        "consumer_input_word_widths": public.get(
            "consumer_input_word_widths"
        ) or [],
        "observable_output_word_widths": public.get(
            "observable_output_word_widths"
        ) or [],
        "producer_to_consumer_contraction": public.get(
            "producer_to_consumer_contraction"
        ) or {},
        "control_protocol": public.get("control_protocol") or {},
        "pipeline_depth": public.get("pipeline_depth"),
        "pipeline_depth_basis": public.get("pipeline_depth_basis"),
        "semantic_candidates": public.get("semantic_candidates") or [],
        "semantic_recovery": {
            key: semantic.get(key)
            for key in (
                "operator_class",
                "rewrite_cut",
                "complete_owner_output_shell_recovered",
                "primitive_shell_node_count",
                "primitive_shell_external_count",
            )
            if key in semantic
        },
        "current_seed_realization": public.get(
            "current_seed_realization"
        ) or {},
        "missing_proof": public.get("missing_proof") or [],
        "predicted_surviving_wall": public.get("predicted_surviving_wall"),
        "action_assessment": {
            "status": assessment.get("status"),
            "authority": assessment.get("authority"),
            "candidate_action": action,
            "not_a_hard_trigger": assessment.get("not_a_hard_trigger"),
            "luna_owns_final_selection": assessment.get(
                "luna_owns_final_selection"
            ),
        },
        "mutation_eligible": payload.get("mutation_eligible"),
        "proof_status": payload.get("proof_status"),
        "llm_payload_contains_object_names": False,
    }


def capture_probe_response(
    mcp,
    tool_name: str,
    response: str,
    probe_candidate: Optional[dict],
) -> str:
    """Retain full trusted payload and return a compact LLM-safe response."""
    if tool_name not in {
        "extract_critical_path_cells",
        "extract_critical_path_pins",
        "operator_mining",
        "recover_recurrence_contract",
        "recover_sequential_operator_contract",
        "recover_consumer_driven_operator_contract",
        "recover_registered_dead_state_contract",
        "recover_control_to_data_state_equation_contract",
        "recover_ff_srl_boundary_bridge_contract",
        "recover_memory_write_buffer_forward_contract",
        "recover_multilevel_control_distribution_contract",
        "recover_multilevel_ram_control_distribution_contract",
        "recover_registered_parity_reassociation_contract",
    }:
        return response
    key = _identity(probe_candidate)
    state = _store(mcp).setdefault(key, {})
    parsed = json.loads(response)
    if tool_name in {"extract_critical_path_cells", "extract_critical_path_pins"}:
        deep_paths_compacted = bool(
            isinstance(parsed, list)
            and any(
                isinstance(path, list) and len(path) > MAX_PATH_ITEMS
                for path in parsed
            )
        )
        paths = _validate_paths(parsed)
        state["critical_paths"] = paths
        public = _critical_path_summary(
            paths, deep_paths_compacted=deep_paths_compacted
        )
    elif tool_name == "operator_mining":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        state["operator_mining"] = parsed
        public, recurrence_refs, sequential_refs = _operator_mining_summary(parsed)
        state["recurrence_family_refs"] = recurrence_refs
        state["sequential_family_refs"] = sequential_refs
    elif tool_name == "recover_recurrence_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        state["recurrence_contract"] = parsed
        public = _recurrence_contract_summary(parsed)
    elif tool_name == "recover_sequential_operator_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        state["sequential_contract"] = parsed
        candidate_id = parsed.get("candidate_id")
        family_refs = state.get("sequential_family_refs") or {}
        matching_refs = sorted(
            str(ref)
            for ref, stored_candidate_id in family_refs.items()
            if stored_candidate_id == candidate_id
        )
        family_ref = matching_refs[0] if len(matching_refs) == 1 else None
        public = _sequential_contract_summary(
            parsed, family_ref, family_refs
        )
    elif tool_name == "recover_consumer_driven_operator_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        candidate_id = parsed.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            retained = state.setdefault("consumer_contracts", {})
            if not isinstance(retained, dict):
                raise ValueError("consumer contract cache has invalid state")
            retained[candidate_id] = parsed
        # Retain the legacy latest-result slot for old readers while the
        # candidate-keyed map prevents a later sibling negative from erasing
        # an earlier executable family contract.
        state["consumer_contract"] = parsed
        family_refs = state.get("sequential_family_refs") or {}
        matching_refs = sorted(
            str(ref)
            for ref, stored_candidate_id in family_refs.items()
            if stored_candidate_id == candidate_id
        )
        family_ref = matching_refs[0] if len(matching_refs) == 1 else None
        public = _consumer_contract_summary(parsed, family_ref)
    elif tool_name == "recover_registered_parity_reassociation_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        families = parsed.get("families") or []
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if (
            not isinstance(families, list)
            or not isinstance(summary, dict) or not isinstance(proof, dict)
        ):
            raise ValueError("registered parity response is malformed")
        state["registered_parity_reassociation_contract"] = parsed
        public_families = [
            {
                key: item.get(key)
                for key in (
                    "family_ref", "logic_depth", "closure_cell_count",
                    "raw_leaf_occurrence_count", "odd_leaf_count",
                    "canceled_leaf_occurrence_count",
                    "registered_endpoint_count",
                    "live_registered_endpoint_count",
                    "earliest_live_path_rank", "output_polarity",
                )
            }
            for item in families if isinstance(item, dict)
        ]
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "read_only_after_restore": parsed.get("read_only_after_restore"),
            "operator_class": parsed.get("operator_class"),
            "families": public_families,
            "summary": {
                key: summary.get(key)
                for key in ("family_count", "mutation_eligible")
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "complete_xor_xnor_closures_recovered",
                    "all_boundary_multiplicities_replayed_in_gf2",
                    "registered_data_endpoints_recovered",
                    "exact_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidates_available" if public_families
                    else "no_live_registered_parity_closure"
                ),
                "candidate_actions": [
                    {
                        "skill": "operator_rewrite",
                        "target": "registered_gf2_parity_reassociation_v1",
                        "params": {"family_ref": item.get("family_ref")},
                    }
                    for item in public_families
                ],
                "expected_mechanism": (
                    "cancel only even GF(2) leaf multiplicities and replace one "
                    "live registered endpoint with a balanced LUT6 XOR tree"
                ),
                "failure_update": (
                    "distinguish incomplete closure, sibling-consumer change, "
                    "route failure, and a routed parity regression"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_family_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": (
                "measurement_probe_only_final_equivalence_required"
                if public_families else "negative_read_only_boundary"
            ),
            "llm_payload_contains_object_names": False,
        }
    elif tool_name == "recover_multilevel_control_distribution_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        families = parsed.get("families") or []
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if (
            not isinstance(families, list)
            or not isinstance(summary, dict) or not isinstance(proof, dict)
        ):
            raise ValueError("control-localization response is malformed")
        state["multilevel_control_distribution_contract"] = parsed
        public_families = [
            {
                key: item.get(key)
                for key in (
                    "family_ref", "logic_depth", "closure_cell_count",
                    "boundary_source_count", "sink_count",
                    "clock_region_count", "sink_role_counts",
                    "covered_path_count", "earliest_path_rank",
                )
            }
            for item in families if isinstance(item, dict)
        ]
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "read_only_after_restore": parsed.get("read_only_after_restore"),
            "operator_class": parsed.get("operator_class"),
            "families": public_families,
            "summary": {
                key: summary.get(key)
                for key in ("family_count", "mutation_eligible")
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "complete_boolean_dag_recovered",
                    "exact_lut_truth_tables_and_boundary_sources_bound",
                    "registered_sink_roles_recovered",
                    "exact_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidates_available" if public_families
                    else "no_complete_multilevel_control_wall"
                ),
                "candidate_actions": [
                    {
                        "skill": "operator_rewrite",
                        "target": (
                            "multilevel_control_distribution_localization_v1"
                        ),
                        "params": {"family_ref": item.get("family_ref")},
                    }
                    for item in public_families
                ],
                "expected_mechanism": (
                    "clone the complete same-cycle Boolean control DAG near "
                    "registered sink regions while preserving the original "
                    "generator and every non-selected consumer"
                ),
                "failure_update": (
                    "distinguish incomplete Boolean closure, route failure, and "
                    "a routed localization regression before refreshing the wall"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_family_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": (
                "measurement_probe_only_final_equivalence_required"
                if public_families else "negative_read_only_boundary"
            ),
            "llm_payload_contains_object_names": False,
        }
    elif tool_name == "recover_multilevel_ram_control_distribution_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        families = parsed.get("families") or []
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if (
            not isinstance(families, list)
            or not isinstance(summary, dict)
            or not isinstance(proof, dict)
        ):
            raise ValueError("RAM-control response is malformed")
        state["multilevel_ram_control_distribution_contract"] = parsed
        public_families = [
            {
                key: item.get(key)
                for key in (
                    "family_ref", "logic_depth", "closure_cell_count",
                    "boundary_source_count", "sink_count",
                    "clock_region_count", "sink_role",
                    "ram_primitive_counts", "nonselected_consumer_count",
                    "sampled_path_count", "covered_path_count",
                    "earliest_path_rank", "live_timing_rank_per_mille",
                    "timing_eligibility_gate",
                )
            }
            for item in families if isinstance(item, dict)
        ]
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "read_only_after_restore": parsed.get("read_only_after_restore"),
            "operator_class": parsed.get("operator_class"),
            "families": public_families,
            "summary": {
                key: summary.get(key)
                for key in (
                    "family_count", "mutation_eligible", "refusal_reason",
                )
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "complete_boolean_dag_recovered",
                    "exact_lut_truth_tables_and_boundary_sources_bound",
                    "ram_primitive_property_and_pin_role_bound",
                    "nonselected_consumer_closure_recovered",
                    "zero_cycle_combinational_schedule_bound",
                    "exact_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidates_available" if public_families
                    else str(
                        summary.get("refusal_reason")
                        or "no_complete_multilevel_ram_control_wall"
                    )
                ),
                "candidate_actions": [
                    {
                        "skill": "operator_rewrite",
                        "target": (
                            "multilevel_ram_control_distribution_localization_v1"
                        ),
                        "params": {"family_ref": item.get("family_ref")},
                    }
                    for item in public_families
                ],
                "expected_mechanism": (
                    "clone the complete same-cycle RAM-control Boolean DAG "
                    "near same-role sink regions while preserving RAM identity, "
                    "cycle timing, the original generator, and all non-selected "
                    "consumers"
                ),
                "failure_update": (
                    "distinguish incomplete closure, RAM identity change, route "
                    "failure, and a routed localization regression"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_family_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": (
                "measurement_probe_only_final_equivalence_required"
                if public_families else "negative_read_only_boundary"
            ),
            "llm_payload_contains_object_names": False,
        }
    elif tool_name == "recover_memory_write_buffer_forward_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        families = parsed.get("families") or []
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if (
            not isinstance(families, list)
            or not isinstance(summary, dict) or not isinstance(proof, dict)
        ):
            raise ValueError("memory-forward response is malformed")
        state["memory_write_buffer_forward_contract"] = parsed
        public_families = [
            {
                key: item.get(key)
                for key in (
                    "family_ref", "data_width", "address_width",
                    "byte_enable_width", "write_mode", "collision_mode",
                    "common_clock", "read_port", "read_latency_cycles",
                    "top_path_rank", "write_worst_slack_ns",
                    "read_worst_slack_ns", "expected_resource_overhead", "risk",
                )
            }
            for item in families if isinstance(item, dict)
        ]
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "read_only_after_restore": parsed.get("read_only_after_restore"),
            "operator_class": parsed.get("operator_class"),
            "families": public_families,
            "summary": {
                key: summary.get(key)
                for key in (
                    "family_count", "shape_rejection_count", "mutation_eligible",
                )
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "two_cycle_relational_memory_miter_replayed",
                    "adjacent_raw_hit_and_miss_covered",
                    "write_and_read_boundaries_closed",
                    "exact_memory_and_pin_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidates_available" if public_families
                    else "no_supported_live_write_boundary"
                ),
                "candidate_actions": [
                    {
                        "skill": "operator_rewrite",
                        "target": "memory_write_buffer_forward_v1",
                        "params": {"family_ref": item.get("family_ref")},
                    }
                    for item in public_families
                ],
                "expected_mechanism": (
                    "delay one BRAM write and forward the pending store at the "
                    "closed registered read boundary on exact address/control hits"
                ),
                "failure_update": (
                    "distinguish unsupported memory semantics from a new read-side "
                    "comparator or forwarding-mux timing wall"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_family_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": (
                "measurement_probe_only_final_equivalence_required"
                if public_families else "negative_read_only_boundary"
            ),
            "llm_payload_contains_object_names": False,
        }
    elif tool_name == "recover_ff_srl_boundary_bridge_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        families = parsed.get("families") or []
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if (
            not isinstance(families, list)
            or not isinstance(summary, dict) or not isinstance(proof, dict)
        ):
            raise ValueError("FF-to-SRL response is malformed")
        state["ff_srl_boundary_bridge_contract"] = parsed
        public_families = [
            {
                key: item.get(key)
                for key in (
                    "family_ref", "chain_count", "chain_length", "ce_mode",
                    "top500_path_overlap_count", "top500_endpoint_count",
                    "worst_path_overlap", "retained_boundary_ff_per_chain",
                    "replacement_primitive", "enabled_cycle_latency", "risk",
                )
            }
            for item in families if isinstance(item, dict)
        ]
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "read_only_after_restore": parsed.get("read_only_after_restore"),
            "operator_class": parsed.get("operator_class"),
            "clock": parsed.get("clock"),
            "families": public_families,
            "summary": {
                key: summary.get(key)
                for key in (
                    "family_count", "chain_count", "timing_wall_family_count",
                    "mutation_eligible",
                )
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "maximal_direct_chain_recovered",
                    "clock_ce_reset_init_uniform",
                    "intermediate_side_fanout_absent",
                    "enabled_cycle_latency_preserved",
                    "exact_cell_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidates_available" if public_families
                    else "no_exact_direct_ff_chain_observed"
                ),
                "candidate_actions": [
                    {
                        "skill": "operator_rewrite",
                        "target": "ff_srl_boundary_bridge_v1",
                        "params": {"family_ref": item.get("family_ref")},
                    }
                    for item in public_families
                ],
                "expected_mechanism": (
                    "replace a transport-only FF tail by SRLC32E while retaining "
                    "the hierarchy-boundary FF and exact enabled-cycle latency"
                ),
                "failure_update": (
                    "if SRL clock-to-Q or dynamic write-enable routing takes over, "
                    "lower the family and refresh the live wall"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_family_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": (
                "measurement_probe_only_final_equivalence_required"
                if public_families else "negative_read_only_boundary"
            ),
            "llm_payload_contains_object_names": False,
        }
    elif tool_name == "recover_control_to_data_state_equation_contract":
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        families = parsed.get("families") or []
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if (
            not isinstance(families, list)
            or not isinstance(summary, dict) or not isinstance(proof, dict)
        ):
            raise ValueError("control-state response is malformed")
        state["control_state_equation_contract"] = parsed
        public_families = [
            {
                key: item.get(key)
                for key in (
                    "family_ref", "state_count", "top500_endpoint_count",
                    "top500_path_overlap_count", "worst_path_overlap",
                    "support_width", "equation", "replacement_equation",
                    "state_primitive_before", "state_primitive_after",
                    "replacement_lut_inputs", "control_set_structure",
                )
            }
            for item in families if isinstance(item, dict)
        ]
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "read_only_after_restore": parsed.get("read_only_after_restore"),
            "operator_class": parsed.get("operator_class"),
            "clock": parsed.get("clock"),
            "families": public_families,
            "summary": {
                key: summary.get(key)
                for key in (
                    "family_count", "state_count", "top500_endpoint_count",
                    "top500_path_overlap_count", "mutation_eligible",
                )
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "set_implies_enable_truth_table_exhaustive",
                    "minimal_care_set_support_recovered",
                    "clock_enable_init_and_observable_cycle_preserved",
                    "exact_cell_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidates_available" if public_families
                    else "no_exact_state_equation_observed"
                ),
                "candidate_actions": [
                    {
                        "skill": "operator_rewrite",
                        "target": "exact_control_to_data_state_equation_v1",
                        "params": {"family_ref": item.get("family_ref")},
                    }
                    for item in public_families
                ],
                "expected_mechanism": (
                    "move an exactly proved synchronous set predicate into the "
                    "registered data equation; compare live control-set reduction "
                    "as packing/global-routing evidence and claim direct path "
                    "shortening only when sampled path overlap exists"
                ),
                "failure_update": (
                    "separate proof/selector replay failure from routed physical "
                    "regression, then refresh the live timing wall"
                ),
                "not_a_hard_trigger": True,
                "luna_owns_family_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": (
                "measurement_probe_only_final_equivalence_required"
                if public_families else "negative_read_only_boundary"
            ),
            "llm_payload_contains_object_names": False,
        }
    else:
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            return response
        summary = parsed.get("summary") or {}
        proof = parsed.get("proof") or {}
        if not isinstance(summary, dict) or not isinstance(proof, dict):
            raise ValueError("registered dead-state response is malformed")
        state["registered_dead_state_contract"] = parsed
        public = {
            "status": parsed.get("status"),
            "kind": parsed.get("kind"),
            "version": parsed.get("version"),
            "read_only": parsed.get("read_only"),
            "fresh_rediscovery": parsed.get("fresh_rediscovery"),
            "operator_class": parsed.get("operator_class"),
            "summary": {
                key: summary.get(key)
                for key in (
                    "physical_cell_count", "observable_physical_cell_count",
                    "dead_physical_cell_count", "top_output_port_bit_count",
                    "top_output_driver_anchor_count",
                    "preserved_unbound_physical_cell_count",
                    "nonphysical_primitive_driver_count", "total_dsp_count",
                    "dead_dsp_count", "partial_dead_dsp_parent_count",
                    "dead_registered_state_count", "mutation_target_cell_count",
                    "mutation_eligible", "dead_type_counts",
                )
            },
            "proof": {
                key: proof.get(key)
                for key in (
                    "output_anchored_transitive_primitive_fanin",
                    "sequential_data_control_and_clock_inputs_traversed",
                    "all_physical_primitives_partitioned",
                    "expanded_dsp_components_grouped_by_live_parent",
                    "partial_dsp_parents_preserved",
                    "exact_dead_cell_names_are_private",
                    "does_not_establish_positive_routed_qor",
                )
            },
            "action_assessment": {
                "status": (
                    "measurement_candidate_available"
                    if summary.get("mutation_eligible") is True
                    else "no_dead_registered_state_observed"
                ),
                "authority": "current_dcp_output_observable_graph_closure",
                "candidate_action": (
                    {
                        "skill": "operator_rewrite",
                        "target": "registered_dead_state_elimination_v1",
                        "params": {},
                    }
                    if summary.get("mutation_eligible") is True else None
                ),
                "expected_mechanism": (
                    "remove a complete registered producer population proven "
                    "outside the output-observable primitive fanin closure"
                ),
                "failure_update": (
                    "distinguish graph-closure or manifest replay failure from "
                    "a routed physical regression, then refresh the live wall"
                ),
                "does_not_establish": [
                    "positive routed QoR",
                    "whole-DCP functional equivalence",
                ],
                "not_a_hard_trigger": True,
                "luna_owns_final_selection": True,
            },
            "mutation_eligible": summary.get("mutation_eligible"),
            "proof_status": "measurement_probe_only_final_equivalence_required",
            "llm_payload_contains_object_names": False,
        }
    _persist_store(mcp)
    return json.dumps(public, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "LATEST_CRITICAL_PATHS",
    "MAX_HANDOFF_PATHS",
    "MissingProbeHandoffError",
    "ProbeArgumentContractError",
    "canonicalize_probe_arguments",
    "capture_probe_response",
    "prepare_probe_arguments",
    "probe_request_fingerprint",
    "resolve_sequential_family_handoff",
    "resolve_consumer_rewrite_handoff",
    "resolve_control_state_handoff",
    "resolve_ff_srl_boundary_bridge_handoff",
    "resolve_memory_forward_handoff",
    "resolve_multilevel_control_localization_handoff",
    "resolve_multilevel_ram_control_localization_handoff",
    "resolve_registered_parity_handoff",
    "resolve_registered_dead_state_handoff",
    "trusted_probe_handoff_available",
]
