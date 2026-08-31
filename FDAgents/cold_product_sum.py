"""Cold current-DCP recovery for complete registered product-sum families.

This is a standalone tool orchestrator, not an Agent policy.  It starts from
anonymous live timing paths, re-mines the exact checkpoint, extracts each
selected sequential closure through fixed repository Tcl, and accepts a bundle
only after complete closure replay and production IR/proof adaptation.  No
retained replay bundle, benchmark name, hierarchy hint, or coordinate is an
input.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifacts import sha256_file
from .probe_handoff import compact_critical_paths_for_operator_mining
from .registered_closure import (
    RegisteredClosure,
    build_generating_operator_bridge_bundle,
    control_aliases_in_closure_namespace,
    merge_generating_operator_bridge_bundles,
    namespace_generating_operator_bridge_bundle,
)
from .regarith_tools.common import object_digest
from .regarith_tools.production_product_sum import render_production_product_sum


SCHEMA_VERSION = "cold-registered-product-sum-v1"
_SEQUENTIAL_ID = re.compile(r"sequential:[0-9a-f]{20}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ColdFamilyDeadlineExceeded(TimeoutError):
    """One exact family exceeded its declared extraction wall."""

    reason_code = "COLD_FAMILY_DEADLINE_EXCEEDED"


class ColdFamilyTypedRefusal(RuntimeError):
    """One current-seed closure is structurally outside the exact contract."""

    def __init__(self, reason_code: str, detail: str):
        self.reason_code = str(reason_code)
        super().__init__(f"{self.reason_code}: {detail}")


_CLOSURE_REFUSAL_MARKERS = (
    (
        "selected registered family does not contain the requested contiguous interval",
        "REGISTERED_INTERVAL_INCOMPLETE",
    ),
    (
        "selected registered bank is not a whole number of requested words",
        "REGISTERED_BANK_WIDTH_MISMATCH",
    ),
    (
        "observable root is not a whole number of words",
        "OBSERVABLE_ROOT_WORD_WIDTH_MISMATCH",
    ),
)


def _remaining(deadline_monotonic: float, cap_s: float, operation: str) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise ColdFamilyDeadlineExceeded(f"{operation} exceeded the family deadline")
    return max(0.1, min(float(cap_s), remaining))


def _payload(value: Any) -> dict[str, Any]:
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise RuntimeError("cold registered-product tool returned a non-object")
    if value.get("status") == "error" or "error" in value:
        raise RuntimeError(str(value.get("error") or value.get("message")))
    return value


def _validate_paths(value: Any) -> list[list[str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("cold registered-product recovery requires 1..16 live paths")
    result = []
    for path in value:
        if not isinstance(path, list) or not 1 <= len(path) <= 256:
            raise ValueError("cold registered-product path shape is invalid")
        row = []
        for token in path:
            if not isinstance(token, str) or not token or len(token) > 1024:
                raise ValueError("cold registered-product path token is invalid")
            row.append(token)
        result.append(row)
    return compact_critical_paths_for_operator_mining(result)


def _indexed_word_interval(
    mapping: Mapping[str, Any], members: Any, *, label: str
) -> tuple[str, int, int]:
    """Recover one exact contiguous register interval without assuming bit zero."""
    minimum = mapping.get("min_index")
    maximum = mapping.get("max_index")
    if (
        mapping.get("status") != "explicit_unique_contiguous"
        or isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum < 0
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not isinstance(members, list)
        or len(members) < 2
        or mapping.get("member_count") != len(members)
        or maximum != minimum + len(members) - 1
    ):
        raise RuntimeError(f"{label} is not one explicit contiguous interval")
    stems = set()
    indices = []
    for item in members:
        name = item.get("cell") if isinstance(item, Mapping) else None
        match = re.fullmatch(r"(.+)\[(\d+)\]", str(name or ""))
        if match is None:
            raise RuntimeError(f"{label} has no indexed cell form")
        stems.add(match.group(1))
        indices.append(int(match.group(2)))
    expected = list(range(minimum, maximum + 1))
    if (
        len(stems) != 1
        or sorted(indices) != expected
        or mapping.get("indices", expected) != expected
    ):
        raise RuntimeError(f"{label} has a gapped, overlapping, or inconsistent interval")
    return next(iter(stems)), minimum, len(members)


def _target_word(
    contract: Mapping[str, Any], source_sha256: str, candidate_id: str
) -> tuple[str, int, int]:
    if (
        contract.get("status") != "success"
        or contract.get("kind") != "sequential_operator_contract_facts"
        or contract.get("fresh_rediscovery") is not True
        or contract.get("read_only") is not True
        or contract.get("design_sha256") != source_sha256
        or contract.get("candidate_id") != candidate_id
        or contract.get("structural_boundary_recovered") is not True
        or contract.get("proof_input_eligible") is not True
        or contract.get("mutation_eligible") is not False
    ):
        raise RuntimeError("fresh sequential contract is incomplete")
    mapping = contract.get("bit_mapping") or {}
    members = contract.get("member_facts")
    selected_stem, selected_lsb, selected_width = _indexed_word_interval(
        mapping, members, label="registered destination bus"
    )
    roots = contract.get("observable_registered_roots")
    if isinstance(roots, list):
        eligible = [
            row for row in roots
            if isinstance(row, Mapping)
            and row.get("complete_reach") is True
            and isinstance(row.get("target_stem"), str)
            and row.get("target_stem")
            and (
                selected_lsb == 0
                or str(row.get("target_stem")) == selected_stem
            )
            and isinstance(row.get("target_width"), int)
            and int(row["target_width"]) >= selected_width
            and int(row["target_width"]) % selected_width == 0
        ]
        if eligible:
            # The RW skeleton only bypasses Vivado's broad all_fanout root
            # search. word_width remains the selected exact contract width, so
            # all observable slices still undergo deep closure and replay.
            root_stem = str(eligible[0]["target_stem"])
            root_lsb = selected_lsb if root_stem == selected_stem else 0
            return root_stem, root_lsb, selected_width
    return selected_stem, selected_lsb, selected_width


def _replica_word(replica: Any) -> tuple[str, int, int, str]:
    if not isinstance(replica, Mapping):
        raise RuntimeError("registered operator replica is not an object")
    family = replica.get("family") or {}
    mapping = family.get("bit_mapping") or {}
    members = replica.get("member_facts")
    candidate_id = str(replica.get("candidate_id") or "")
    if _SEQUENTIAL_ID.fullmatch(candidate_id) is None or (
        isinstance(members, list) and family.get("member_count") != len(members)
    ):
        raise RuntimeError("registered operator replica word is incomplete")
    stem, lsb, width = _indexed_word_interval(
        mapping, members, label="registered operator replica"
    )
    return stem, lsb, width, candidate_id


def _owner(stem: str) -> str:
    owner, separator, _ = stem.rpartition("/")
    return owner if separator else "."


def _ordered_replicas(replicas: list[Any], selected_stem: str) -> list[tuple[int, Any]]:
    """Try one anonymous owner at a time before revisiting the same owner."""
    selected_owner = _owner(selected_stem)
    selected = next(
        (item for item in replicas if isinstance(item, Mapping) and item.get("is_selected") is True),
        {},
    )
    selected_trace = selected.get("data_cone_traceback") or {}

    def distance(item: Any) -> tuple[int, int, int, int, int, int]:
        if not isinstance(item, Mapping):
            return (0, 0, 1, 10**9, 10**9, 10**9)
        trace = item.get("data_cone_traceback") or {}
        primitive_keys = set(selected_trace.get("primitive_type_counts") or {}) | set(
            trace.get("primitive_type_counts") or {}
        )
        boundary_keys = set(selected_trace.get("boundary_type_counts") or {}) | set(
            trace.get("boundary_type_counts") or {}
        )
        return (
            -int(item.get("critical_path_coverage_count") or 0),
            -int(item.get("critical_path_member_count") or 0),
            0 if bool(item.get("cone_signature_match")) else 1,
            sum(abs(
                int((selected_trace.get("primitive_type_counts") or {}).get(key, 0))
                - int((trace.get("primitive_type_counts") or {}).get(key, 0))
            ) for key in primitive_keys),
            sum(abs(
                int((selected_trace.get("boundary_type_counts") or {}).get(key, 0))
                - int((trace.get("boundary_type_counts") or {}).get(key, 0))
            ) for key in boundary_keys),
            abs(
                int(selected_trace.get("total_node_count") or 0)
                - int(trace.get("total_node_count") or 0)
            ),
        )

    buckets: dict[str, list[tuple[int, Any]]] = {}
    invalid = []
    for ordinal, replica in enumerate(replicas):
        try:
            stem, _, _, _ = _replica_word(replica)
        except RuntimeError:
            invalid.append((ordinal, replica))
            continue
        buckets.setdefault(_owner(stem), []).append((ordinal, replica))
    for candidates in buckets.values():
        candidates.sort(key=lambda row: (*distance(row[1]), row[0]))
    owner_order = sorted(
        (name for name in buckets if name != selected_owner),
        key=lambda name: min(distance(row[1]) for row in buckets[name]),
    )
    if selected_owner in buckets:
        owner_order.append(selected_owner)
    ordered = []
    depth = 0
    while any(depth < len(buckets[name]) for name in owner_order):
        for name in owner_order:
            if depth < len(buckets[name]):
                ordered.append(buckets[name][depth])
        depth += 1
    return [*ordered, *invalid]


async def _extract_closure(
    mcp: Any,
    *,
    source: Path,
    source_sha256: str,
    target_stem: str,
    word_lsb: int,
    word_width: int,
    output_dir: Path,
    max_stage_depth: int,
    deadline_monotonic: float,
) -> RegisteredClosure:
    extraction_timeout = _remaining(
        deadline_monotonic, 900.0, "registered closure extraction"
    )
    request = {
        "dcp_path": str(source),
        "source_dcp_sha256": source_sha256,
        "target_stem": target_stem,
        "word_lsb": word_lsb,
        "output_dir": str(output_dir),
        "max_stage_depth": max_stage_depth,
        "word_width": word_width,
        "timeout_s": max(30.0, extraction_timeout),
    }
    try:
        response = _payload(await mcp.call_vivado(
            "extract_registered_closure_facts",
            request,
            # The server kills/restarts a timed-out worker before replying. Keep
            # that recovery tail bounded rather than waiting indefinitely for a
            # Tcl prompt from the failed command.
            timeout=extraction_timeout + 90.0,
        ))
    except (asyncio.TimeoutError, TimeoutError) as exc:
        restart = getattr(mcp, "restart_vivado", None)
        if callable(restart):
            try:
                await asyncio.wait_for(
                    restart(
                        restore_checkpoint=source,
                        expected_sha256=source_sha256,
                        timeout=60.0,
                        restart_request_timeout=30.0,
                    ),
                    timeout=90.0,
                )
            except Exception:  # noqa: BLE001 - deadline evidence is primary
                pass
        raise ColdFamilyDeadlineExceeded(
            "registered closure extraction exceeded its hard deadline; "
            "the Vivado worker was stopped/restarted"
        ) from exc
    except Exception as exc:
        detail = str(exc)
        for marker, reason_code in _CLOSURE_REFUSAL_MARKERS:
            if marker in detail:
                raise ColdFamilyTypedRefusal(reason_code, marker) from exc
        if any(marker in detail for marker in (
            "Timeout exceeded",
            "TIMED OUT",
            "hard deadline",
            "isolated Vivado worker exceeded",
        )):
            raise ColdFamilyDeadlineExceeded(
                "registered closure extraction exceeded its hard deadline; "
                "the Vivado worker was stopped/restarted"
            ) from exc
        raise
    if response.get("status") == "rejected":
        reason_code = str(response.get("reason_code") or "")
        marker = str(response.get("message") or "")
        allowed_refusals = {
            code: text for text, code in _CLOSURE_REFUSAL_MARKERS
        }
        if allowed_refusals.get(reason_code) != marker:
            raise RuntimeError("registered closure returned an unknown refusal")
        raise ColdFamilyTypedRefusal(reason_code, marker)
    if (
        response.get("read_only_after_restore") is not True
        or response.get("source_checkpoint_reopened") is not True
    ):
        raise RuntimeError("registered closure extraction did not restore the source")
    return RegisteredClosure.load(output_dir)


async def _install_control_aliases(
    mcp: Any,
    closure: RegisteredClosure,
    source_sha256: str,
    *,
    deadline_monotonic: float,
) -> None:
    for field, role in (("clock_net", "CLK"), ("ce_net", "CE")):
        sink_pins = closure.control_sink_pins(field)
        if len(sink_pins) <= 1:
            continue
        facts = _payload(await mcp.call_rw(
            "prove_control_pin_equivalence_groups",
            {"pin_names": list(sink_pins.values()), "role": role},
            timeout=_remaining(
                deadline_monotonic, 300.0, f"{role} alias proof"
            ),
        ))
        aliases = facts.get("net_aliases")
        if (
            facts.get("kind") != "proved_control_pin_equivalence_groups"
            or facts.get("design_sha256") != source_sha256
            or facts.get("complete_pin_coverage") is not True
            or not isinstance(aliases, dict)
        ):
            raise RuntimeError(
                f"registered {role} equivalence proof is incomplete"
            )
        closure.install_proved_control_net_aliases(
            field,
            control_aliases_in_closure_namespace(facts, sink_pins),
        )


def _screened_family(
    emission: Mapping[str, Any], *, covered: int, resolved: int,
    sibling_count: int, earliest_covered_rank: int | None,
    fixed_product_count: int, endpoint_path_count: int = 0,
) -> dict[str, Any]:
    fingerprint = str(emission["result_digest"])
    removes_proved_operator_wall = fixed_product_count > 0
    return {
        "family_kind": (
            "product_sum" if removes_proved_operator_wall
            else "registered_linear_sum"
        ),
        "sibling_support_count": sibling_count,
        "control_entropy": 0,
        "term_coverage": 1_000_000,
        "serial_depth": min(256, int(emission["instance_count"])),
        "live_wall_covered_path_count": covered,
        "live_wall_resolved_path_count": resolved,
        # The deep-closure scheduler already trusts this to decide which family
        # to prove first, but it never reached the card, so two families with
        # identical coverage and rank were indistinguishable downstream even
        # when only one of them terminates a failing path.
        "live_wall_endpoint_path_count": max(0, int(endpoint_path_count)),
        "live_wall_coverage_ppm": int(1_000_000 * covered / resolved) if resolved else 0,
        "earliest_covered_rank": (
            earliest_covered_rank
            if isinstance(earliest_covered_rank, int)
            and not isinstance(earliest_covered_rank, bool)
            and 1 <= earliest_covered_rank <= resolved
            else "unknown"
        ),
        "unresolved_earlier_path_count": (
            max(0, earliest_covered_rank - 1)
            if isinstance(earliest_covered_rank, int)
            and not isinstance(earliest_covered_rank, bool)
            and 1 <= earliest_covered_rank <= resolved
            else "unknown"
        ),
        "transaction_scope_count": sibling_count,
        "expected_replaced_instance_count": int(emission["instance_count"]),
        "missing_obligations": (
            [] if removes_proved_operator_wall
            else ["passthrough_only_wall_removal_unproved"]
        ),
        "contract_status": "complete",
        "proof_template_id": str(emission["proof_template_id"]),
        "plan_digest": fingerprint,
        "lowering_status": "selected",
        "lowering_backend": "registered_product_sum_dcp_bridge",
        "observation_priority": (
            "eligible"
            if covered and removes_proved_operator_wall
            else "partial_evidence"
            if covered
            else "deprioritized_zero_coverage"
        ),
        "nomination_eligible": covered > 0 and removes_proved_operator_wall,
        "nomination_blockers": (
            []
            if covered and removes_proved_operator_wall
            else ["passthrough_only_wall_removal_unproved"]
            if covered
            else ["zero_live_wall_overlap"]
        ),
        "proof_status": "proved",
        "family_fingerprint": fingerprint,
        "typed_rejection": None,
    }


def _unavailable_family(
    *,
    candidate_id: str,
    covered: int,
    resolved: int,
    reason_code: str,
    exception_class: str,
    reason_detail: str = "",
) -> dict[str, Any]:
    """Publish bounded census evidence without implying mutation authority."""
    fingerprint = object_digest({
        "candidate_id": candidate_id,
        "covered": covered,
        "resolved": resolved,
        "reason_code": reason_code,
    })
    return {
        "family_kind": "product_sum",
        "sibling_support_count": 0,
        "control_entropy": "unknown",
        "term_coverage": "unknown",
        "serial_depth": "unknown",
        "live_wall_covered_path_count": covered,
        "live_wall_resolved_path_count": resolved,
        "live_wall_coverage_ppm": (
            int(1_000_000 * covered / resolved) if resolved else 0
        ),
        "earliest_covered_rank": "unknown",
        "unresolved_earlier_path_count": "unknown",
        "transaction_scope_count": "unknown",
        "expected_replaced_instance_count": "unknown",
        "missing_obligations": [reason_code],
        "contract_status": "unavailable",
        "proof_template_id": "unknown",
        "plan_digest": "unknown",
        "lowering_status": "unavailable",
        "lowering_backend": "unknown",
        "observation_priority": (
            "deprioritized_zero_coverage" if covered == 0 else "partial_evidence"
        ),
        "nomination_eligible": False,
        "nomination_blockers": [
            reason_code,
            "exact_contract_not_recovered",
            "proof_not_proved",
        ],
        "proof_status": "unavailable",
        "family_fingerprint": fingerprint,
        "typed_rejection": {
            "reason_code": reason_code,
            "exception_class": exception_class,
            # Without this the refusal reaching the planner and the menu is the
            # bare exception class, so a structural non-applicability, a budget
            # shortfall and an infrastructure fault all read identically.
            "reason_detail": str(reason_detail)[:500],
        },
    }


def partition_retry_family_cache(
    prior_result: Mapping[str, Any] | None,
    *,
    source_artifact_sha256: str,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Reuse only proved family slices; failed slices are invalid retry inputs."""
    if prior_result is None:
        return {}, ()
    candidates = prior_result.get("candidates")
    census = prior_result.get("census")
    if (
        prior_result.get("kind") != "cold_registered_product_sum_facts_set"
        or prior_result.get("design_sha256") != source_artifact_sha256
        or not isinstance(candidates, list)
        or not isinstance(census, list)
    ):
        raise ValueError("cold retry cache is stale or malformed")
    successful: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = str(candidate.get("candidate_id") or "")
        screened = candidate.get("screened_family")
        if (
            _SEQUENTIAL_ID.fullmatch(candidate_id) is not None
            and isinstance(screened, Mapping)
            and screened.get("contract_status") == "complete"
            and screened.get("proof_status") == "proved"
            and isinstance(candidate.get("preflight_receipt"), Mapping)
            and isinstance(candidate.get("bundle"), Mapping)
        ):
            successful[candidate_id] = copy.deepcopy(dict(candidate))
    failed = tuple(sorted({
        str(row.get("candidate_id") or "")
        for row in census
        if isinstance(row, Mapping)
        and _SEQUENTIAL_ID.fullmatch(str(row.get("candidate_id") or "")) is not None
        and str(row.get("candidate_id") or "") not in successful
    }))
    return successful, failed


async def recover_cold_product_sum_families(
    mcp: Any,
    *,
    input_dcp: Path | str,
    source_artifact_sha256: str,
    run_id: str,
    critical_paths: list[list[str]],
    output_dir: Path | str,
    max_families: int = 4,
    max_replicas: int = 64,
    max_replica_screens: int = 8,
    max_stage_depth: int = 5,
    max_candidates: int = 2,
    preflight_budget_s: float = 300.0,
    family_deadline_s: float = 240.0,
    graph_candidate_id: str | None = None,
    retry_prior_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Cold-recover the first exact live complete-family production bundle."""
    started = time.monotonic()
    source = Path(input_dcp).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    run_dir = Path(mcp.run_dir).expanduser().resolve()
    paths = _validate_paths(critical_paths)
    if (
        source.is_symlink()
        or not source.is_file()
        or _SHA256.fullmatch(source_artifact_sha256) is None
        or sha256_file(source) != source_artifact_sha256
        or re.fullmatch(r"nr-[0-9a-f]{32}", str(run_id or "")) is None
    ):
        raise ValueError("cold registered-product source identity is invalid")
    try:
        destination.relative_to(run_dir)
    except ValueError as exc:
        raise ValueError("cold registered-product output must stay in the run directory") from exc
    if destination == run_dir or destination.exists() or destination.parent.is_symlink():
        raise ValueError("cold registered-product output directory must be fresh")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            max_families, max_replicas, max_replica_screens,
            max_stage_depth, max_candidates,
        )
    ) or not (
        1 <= max_families <= 16
        and 1 <= max_replicas <= 64
        and 0 <= max_replica_screens <= 16
        and 1 <= max_stage_depth <= 8
        and 1 <= max_candidates <= 4
    ):
        raise ValueError("cold registered-product bounds are invalid")
    if (
        isinstance(preflight_budget_s, bool)
        or not isinstance(preflight_budget_s, (int, float))
        or not 30 <= float(preflight_budget_s) <= 1800
        or isinstance(family_deadline_s, bool)
        or not isinstance(family_deadline_s, (int, float))
        or not 30 <= float(family_deadline_s) <= 900
        or (
            graph_candidate_id is not None
            and (
                not isinstance(graph_candidate_id, str)
                or not graph_candidate_id
                or len(graph_candidate_id) > 128
            )
        )
    ):
        raise ValueError("cold registered-product deadlines are invalid")
    destination.mkdir(parents=False)
    global_deadline = started + float(preflight_budget_s)
    cached_successes, invalidated_failed_ids = partition_retry_family_cache(
        retry_prior_result,
        source_artifact_sha256=source_artifact_sha256,
    )
    reused_successful_ids: list[str] = []

    mining_request = {
        "critical_paths_data": paths,
        "min_family_size": 2,
        "max_families": 16,
        "max_motif_cells": 8,
    }
    if graph_candidate_id is not None and callable(
        getattr(mcp, "call_exact_rapidwright_probe", None)
    ):
        mining = _payload(await mcp.call_exact_rapidwright_probe(
            "operator_mining",
            mining_request,
            candidate_id=graph_candidate_id,
            artifact_path=source,
            artifact_sha256=source_artifact_sha256,
            timeout=_remaining(global_deadline, 600.0, "cold family census"),
        ))
    else:
        await mcp.call_rw(
            "initialize_rapidwright",
            {"jvm_max_memory": "8G"},
            timeout=_remaining(global_deadline, 120.0, "RapidWright initialization"),
        )
        _payload(await mcp.call_rw(
            "read_checkpoint",
            {"dcp_path": str(source)},
            timeout=_remaining(global_deadline, 600.0, "RapidWright checkpoint load"),
        ))
        mining = _payload(await mcp.call_rw(
            "operator_mining",
            mining_request,
            timeout=_remaining(global_deadline, 600.0, "cold family census"),
        ))
    if (
        mining.get("status") != "success"
        or mining.get("read_only") is not True
        or mining.get("design_sha256") != source_artifact_sha256
        or mining.get("mutation_allowed") is not False
    ):
        raise RuntimeError("cold registered-product mining authority is invalid")
    census_elapsed_s = time.monotonic() - started
    families = mining.get("sequential_write_families")
    if not isinstance(families, list):
        raise RuntimeError("cold mining omitted sequential write families")
    # Mining's broad family order favors total sample coverage. Deep closure is
    # much more expensive and must prioritize the family that actually owns the
    # front of the live wall. This is scheduling only; exact closure/replay is
    # still the sole mutation authority.
    def live_wall_rank(family: Any) -> tuple[int, int, int, int, int, str]:
        if not isinstance(family, Mapping):
            return (1, 1, 10**9, 0, 0, "")
        coverage = family.get("critical_path_coverage") or {}
        earliest = coverage.get("earliest_covered_rank")
        return (
            0 if coverage.get("covers_worst_path") is True else 1,
            0 if int(coverage.get("endpoint_path_count") or 0) > 0 else 1,
            int(earliest) if isinstance(earliest, int) else 10**9,
            -int(coverage.get("covered_path_count") or 0),
            -int(family.get("member_count") or 0),
            str(family.get("candidate_id") or ""),
        )

    # Rank the complete current-seed census before applying the bounded deep
    # closure count.  Slicing the miner's coverage-ordered list first can hide
    # a rank-1 complete transaction behind a slightly broader rank-3 sample.
    ranked_families = sorted(families, key=live_wall_rank)[:max_families]

    census = []
    for family in ranked_families:
        if not isinstance(family, Mapping):
            continue
        coverage = family.get("critical_path_coverage") or {}
        census.append({
            "candidate_id": str(family.get("candidate_id") or ""),
            "covered_path_count": int(coverage.get("covered_path_count") or 0),
            "earliest_covered_rank": coverage.get("earliest_covered_rank"),
            "member_count": int(family.get("member_count") or 0),
            "endpoint_path_count": int(coverage.get("endpoint_path_count") or 0),
            "deep_closure_status": "pending",
        })
    candidates = []
    rejections = []
    unavailable_families = []
    exact_proof_elapsed_s = 0.0
    for family_ordinal, family in enumerate(ranked_families):
        candidate_id = str(family.get("candidate_id") or "") if isinstance(family, Mapping) else ""
        if _SEQUENTIAL_ID.fullmatch(candidate_id) is None:
            continue
        coverage = family.get("critical_path_coverage") or {}
        covered = int(coverage.get("covered_path_count") or 0)
        census_row = next(
            (item for item in census if item["candidate_id"] == candidate_id), None
        )
        cached_success = cached_successes.get(candidate_id)
        if cached_success is not None:
            candidates.append(cached_success)
            reused_successful_ids.append(candidate_id)
            if census_row is not None:
                census_row["deep_closure_status"] = "complete_proved_cached"
            if len(candidates) >= max_candidates:
                break
            continue
        if covered <= 0:
            reason = "ZERO_LIVE_WALL_OVERLAP"
            if census_row is not None:
                census_row["deep_closure_status"] = "census_only_zero_coverage"
            unavailable_families.append(_unavailable_family(
                candidate_id=candidate_id,
                covered=0,
                resolved=len(paths),
                reason_code=reason,
                exception_class="PrefilteredFamily",
            ))
            continue
        family_deadline = min(
            global_deadline, time.monotonic() + float(family_deadline_s)
        )
        if family_deadline <= time.monotonic():
            reason = "COLD_PREFLIGHT_BUDGET_EXHAUSTED"
            if census_row is not None:
                census_row["deep_closure_status"] = "unavailable_budget"
            unavailable_families.append(_unavailable_family(
                candidate_id=candidate_id,
                covered=covered,
                resolved=len(paths),
                reason_code=reason,
                exception_class="ColdFamilyDeadlineExceeded",
            ))
            continue
        try:
            sequential = _payload(await mcp.call_rw(
                "recover_sequential_operator_contract",
                {"critical_paths_data": paths, "candidate_id": candidate_id},
                timeout=_remaining(
                    family_deadline, 600.0, "sequential contract recovery"
                ),
            ))
            target_stem, target_lsb, target_width = _target_word(
                sequential, source_artifact_sha256, candidate_id
            )
            closure_dir = destination / f"family_{family_ordinal:02d}"
            closure = await _extract_closure(
                mcp,
                source=source,
                source_sha256=source_artifact_sha256,
                target_stem=target_stem,
                word_lsb=target_lsb,
                word_width=target_width,
                output_dir=closure_dir,
                max_stage_depth=max_stage_depth,
                deadline_monotonic=family_deadline,
            )
            await _install_control_aliases(
                mcp,
                closure,
                source_artifact_sha256,
                deadline_monotonic=family_deadline,
            )
            bundle = build_generating_operator_bridge_bundle(closure)
            _remaining(family_deadline, 900.0, "registered bundle recovery")
            replica_summary = {
                "status": "not_required",
                "recovered_replica_count": 1,
                "accepted_bank_count": int((bundle.get("recovery") or {}).get("bank_count", 1)),
                "screened_replica_count": 0,
                "rejections": [],
            }
            # A preserve-target product-only data cone is already scoped by its
            # complete observable register family.  Shell-replacing plans can
            # represent one of several equivalent observable banks and must be
            # expanded through exact replica recovery before nomination.
            requires_bank_expansion = any(
                str(plan.get("output_integration") or "")
                != "preserve_target_register_v1"
                for plan in (bundle.get("plans") or [])
                if isinstance(plan, Mapping)
            )
            if max_replica_screens > 0 and requires_bank_expansion:
                replica_facts = _payload(await mcp.call_rw(
                    "recover_sequential_operator_replicas",
                    {
                        "critical_paths_data": paths,
                        "candidate_id": candidate_id,
                        "max_replicas": max_replicas,
                    },
                    timeout=_remaining(
                        family_deadline, 900.0, "registered replica census"
                    ),
                ))
                replicas = replica_facts.get("replicas")
                if (
                    replica_facts.get("kind") != "sequential_operator_replica_family_facts"
                    or replica_facts.get("read_only") is not True
                    or replica_facts.get("fresh_rediscovery") is not True
                    or replica_facts.get("mutation_eligible") is not False
                    or not isinstance(replicas, list)
                ):
                    raise RuntimeError("registered operator replica recovery is incomplete")
                bank_bundles = [bundle]
                seen_stems = {(target_stem, target_lsb, target_width)}
                covered_root_stems = {
                    record.root_stem for record in closure.roots()
                }
                screened = 0
                replica_rejections = []
                for replica_ordinal, replica in _ordered_replicas(replicas, target_stem):
                    if screened >= max_replica_screens:
                        break
                    try:
                        peer_stem, peer_lsb, peer_width, _ = _replica_word(replica)
                        peer_identity = (peer_stem, peer_lsb, peer_width)
                        if peer_identity in seen_stems:
                            continue
                        seen_stems.add(peer_identity)
                        if peer_stem in covered_root_stems:
                            continue
                        screened += 1
                        peer_dir = destination / (
                            f"family_{family_ordinal:02d}_replica_{replica_ordinal:02d}"
                        )
                        peer_closure = await _extract_closure(
                            mcp,
                            source=source,
                            source_sha256=source_artifact_sha256,
                            target_stem=peer_stem,
                            word_lsb=peer_lsb,
                            word_width=peer_width,
                            output_dir=peer_dir,
                            max_stage_depth=max_stage_depth,
                            deadline_monotonic=family_deadline,
                        )
                        await _install_control_aliases(
                            mcp,
                            peer_closure,
                            source_artifact_sha256,
                            deadline_monotonic=family_deadline,
                        )
                        peer_bundle = build_generating_operator_bridge_bundle(peer_closure)
                        _remaining(
                            family_deadline, 900.0, "replica bundle recovery"
                        )
                        merged = merge_generating_operator_bridge_bundles(
                            [*bank_bundles, peer_bundle]
                        )
                        prior = len(bank_bundles)
                        bank_count = int(merged["recovery"]["bank_count"])
                        if bank_count == prior:
                            replica_rejections.append("duplicate_observable_bank")
                            continue
                        if bank_count != prior + 1:
                            raise RuntimeError("replica merge changed bank count unexpectedly")
                        bank_bundles.append(peer_bundle)
                        covered_root_stems.update(
                            record.root_stem for record in peer_closure.roots()
                        )
                        bundle = merged
                        break  # one new bank, then the routed seed must be re-profiled
                    except (RuntimeError, ValueError) as exc:
                        replica_rejections.append(type(exc).__name__)
                bundle = merge_generating_operator_bridge_bundles(bank_bundles)
                replica_summary = {
                    "status": "complete",
                    "recovered_replica_count": int(replica_facts.get("replica_count") or 0),
                    "accepted_bank_count": int(bundle["recovery"]["bank_count"]),
                    "screened_replica_count": screened,
                    "rejections": replica_rejections,
                }

            bundle = namespace_generating_operator_bridge_bundle(
                bundle,
                namespace=f"fdagents_ps_{source_artifact_sha256[:16]}",
            )
            replay_vectors = int(
                (bundle.get("recovery") or {}).get("replay_vector_count") or 0
            )
            if replay_vectors != 512:
                raise RuntimeError(
                    "REGARITH_CHEAP_REPLAY_GUARD_REQUIRED: cold complete-family "
                    "recovery must pass the existing 512-vector replay screen"
                )
            emission = render_production_product_sum(
                bundle, source_artifact_sha256=source_artifact_sha256
            )
            plan_digests = list(emission.get("plan_digests") or [])
            if len(plan_digests) != int(emission["instance_count"]):
                raise RuntimeError("cold recovered plan digest coverage is incomplete")
            recovered_plan_digest = object_digest(plan_digests)
            screened_family = _screened_family(
                emission,
                covered=min(covered, len(paths)),
                resolved=len(paths),
                sibling_count=int(emission["instance_count"]),
                earliest_covered_rank=coverage.get("earliest_covered_rank"),
                fixed_product_count=sum(
                    1
                    for plan in bundle.get("plans", [])
                    if isinstance(plan, Mapping)
                    for term in plan.get("terms", [])
                    if isinstance(term, Mapping)
                    and term.get("kind") == "fixed_product_slice"
                ),
                endpoint_path_count=int(
                    coverage.get("endpoint_path_count") or 0
                ),
            )
            duplicate = next(
                (
                    item for item in candidates
                    if item["family_fingerprint"] == emission["result_digest"]
                ),
                None,
            )
            if duplicate is not None:
                if duplicate["bundle_digest"] != emission["bundle_digest"]:
                    raise RuntimeError(
                        "duplicate product-sum fingerprint changed bundle identity"
                    )
                rejections.append({
                    "candidate_id": candidate_id,
                    "reason_code": "DUPLICATE_COLD_FAMILY_FINGERPRINT",
                    "exception_class": "DuplicateCandidate",
                    "message": (
                        "anonymous sequential families converged on the same "
                        "production product-sum plan"
                    ),
                })
                continue
            bundle_path = destination / f"registered_product_bundle_{family_ordinal:02d}.json"
            bundle_path.write_text(
                json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8"
            )
            candidate = {
                "candidate_id": candidate_id,
                "run_id": run_id,
                "family_fingerprint": emission["result_digest"],
                "bundle_digest": emission["bundle_digest"],
                "bundle_path": str(bundle_path),
                "bundle": bundle,
                "emission": emission,
                "plan_digests": plan_digests,
                "recovered_plan_digest": recovered_plan_digest,
                "cheap_replay_guard": {
                    "protocol": "registered_closure_deterministic_replay_v1",
                    "vector_count": replay_vectors,
                    "status": "passed",
                },
                "screened_family": screened_family,
                "replica_discovery": replica_summary,
            }
            from .registered_product_sum_provider import (
                build_product_sum_preflight_receipt,
            )
            proof_started = time.monotonic()
            candidate["preflight_receipt"] = build_product_sum_preflight_receipt(
                candidate,
                expected_family_fingerprint=str(candidate["family_fingerprint"]),
                expected_source_sha256=source_artifact_sha256,
                expected_run_id=run_id,
            )
            exact_proof_elapsed_s += time.monotonic() - proof_started
            candidates.append(candidate)
            if census_row is not None:
                census_row["deep_closure_status"] = "complete_proved"
            if len(candidates) >= max_candidates:
                break
        except Exception as exc:  # each anonymous sibling fails closed independently
            typed = getattr(exc, "reason_code", None)
            # A refusal the planner cannot tell apart from any other refusal is
            # not a typed refusal.  Where the mechanism did not raise a typed
            # code, say so explicitly instead of passing off the exception
            # class as one, and keep the message that names the real cause.
            reason_code = (
                str(typed) if typed else f"untyped_{type(exc).__name__}".lower()
            )
            reason_detail = str(exc)[:500]
            if census_row is not None:
                census_row["deep_closure_status"] = (
                    "unavailable_timeout"
                    if isinstance(exc, ColdFamilyDeadlineExceeded)
                    else "rejected"
                )
            unavailable_families.append(_unavailable_family(
                candidate_id=candidate_id,
                covered=covered,
                resolved=len(paths),
                reason_code=reason_code,
                exception_class=type(exc).__name__,
                reason_detail=reason_detail,
            ))
            rejections.append({
                "candidate_id": candidate_id,
                "reason_code": reason_code,
                "exception_class": type(exc).__name__,
                "message": reason_detail,
            })

    for row in census:
        if row.get("deep_closure_status") != "pending":
            continue
        row["deep_closure_status"] = "census_only_candidate_limit"
        unavailable_families.append(_unavailable_family(
            candidate_id=str(row["candidate_id"]),
            covered=int(row["covered_path_count"]),
            resolved=len(paths),
            reason_code="COLD_FAMILY_CENSUS_ONLY",
            exception_class="CandidateLimit",
        ))

    deep_closure_elapsed_s = time.monotonic() - started - census_elapsed_s
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cold_registered_product_sum_facts_set",
        "status": "success" if candidates else "rejected",
        "design_sha256": source_artifact_sha256,
        "run_id": run_id,
        "fresh_rediscovery": True,
        "read_only": True,
        "mutation_eligible": False,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "census": census,
        "unavailable_families": unavailable_families,
        "rejections": rejections,
        "family_retry_cache": {
            "source_artifact_sha256": source_artifact_sha256,
            "retry": retry_prior_result is not None,
            "reused_successful_candidate_ids": reused_successful_ids,
            "invalidated_failed_candidate_ids": list(invalidated_failed_ids),
            "failure_entries_satisfying_retry": 0,
        },
        "preflight_complete": all(
            row.get("deep_closure_status")
            not in {"pending", "unavailable_budget", "unavailable_timeout"}
            for row in census
        ),
        "timing": {
            "budget_s": float(preflight_budget_s),
            "family_deadline_s": float(family_deadline_s),
            "census_elapsed_s": round(census_elapsed_s, 6),
            "family_closure_elapsed_s": round(
                max(0.0, deep_closure_elapsed_s - exact_proof_elapsed_s), 6
            ),
            "exact_proof_elapsed_s": round(exact_proof_elapsed_s, 6),
            "deep_closure_elapsed_s": round(deep_closure_elapsed_s, 6),
            "elapsed_s": round(time.monotonic() - started, 6),
        },
    }
    public_digest_input = {
        key: value for key, value in result.items() if key not in {"candidates"}
    } | {
        "candidate_fingerprints": [row["family_fingerprint"] for row in candidates]
    }
    result["result_digest"] = object_digest(public_digest_input)
    (destination / "cold_recovery.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result


__all__ = ["SCHEMA_VERSION", "recover_cold_product_sum_families"]
