"""Fail-closed public evidence for read-only Agent registered-arithmetic study.

This module deliberately consumes only the authority-bearing output of the
existing critical-path operator miner.  It publishes bounded anonymous facts;
it does not recover an arithmetic contract, prove a rewrite, or grant mutation
authority.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .common import object_digest


SCHEMA_VERSION = "regarith-agent-evidence-v2"
TARGET = "registered_arithmetic_cone_v1"
AUTHORITY = "exact_incumbent_registered_arithmetic_observation_only"
_MINER_AUTHORITY = "exact_dcp_structural_hypothesis_only"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UNKNOWN = "unknown"
CONCURRENT_PROOF_SCHEMA_VERSION = "concurrent-proof-agent-evidence-v1"
CONCURRENT_PROOF_AUTHORITY = "tool_owned_exact_proof_state_only"
_CONCURRENT_ROW_TYPES = {
    "proof_in_flight",
    "proof_failed_real",
    "proof_failed_misbound",
    "proof_timeout",
}

_COLLECTIONS = (
    ("recurrence_transport", "recurrence_boundary_families", None),
    (
        "fixed_product",
        "fixed_point_families",
        "fixed_point_product_hypothesis_count",
    ),
    (
        "two_operand_product",
        "two_operand_families",
        "two_operand_product_hypothesis_count",
    ),
    ("registered_motif", "repeated_path_families", None),
)
_FAMILY_KINDS = {item[0] for item in _COLLECTIONS}
_PUBLIC_FAMILY_KINDS = _FAMILY_KINDS | {"product_sum"}
_MISSING_OBLIGATIONS = {
    "recurrence_transport": [
        "exact_registered_word_boundaries",
        "control_and_schedule_contract",
        "recurrence_composition_proof",
        "retained_side_consumer_inventory",
    ],
    "fixed_product": [
        "exact_registered_word_boundaries",
        "data_term_and_numeric_loss_contract",
        "control_and_schedule_contract",
        "retained_side_consumer_inventory",
        "exact_arithmetic_proof",
    ],
    "two_operand_product": [
        "exact_registered_word_boundaries",
        "two_operand_term_and_numeric_loss_contract",
        "control_and_schedule_contract",
        "retained_side_consumer_inventory",
        "exact_arithmetic_proof",
    ],
    "registered_motif": [
        "operator_classification",
        "exact_registered_word_boundaries",
        "data_term_and_numeric_loss_contract",
        "control_and_schedule_contract",
        "retained_side_consumer_inventory",
        "exact_arithmetic_proof",
    ],
}


def _phase3_backend_evidence() -> dict[str, Any]:
    """Expose capability status without leaking a prior-run QoR outcome."""
    return {
        "backend": "singleton_wide_dsp_cascade",
        "status": "not_runtime_authorized",
        "authorization_requires": "exact_current_seed_contract_and_proof",
        "default_enabled": False,
    }


def _action_decision_summary(
    siblings: list[dict[str, Any]], counts: Mapping[str, int]
) -> dict[str, Any]:
    """Reorder decision-critical tool facts without adding a recommendation."""
    tool_errors = []
    unsorted_status = []
    for kind in sorted(_PUBLIC_FAMILY_KINDS):
        rows = [row for row in siblings if row.get("family_kind") == kind]
        eligible = sum(row.get("nomination_eligible") is True for row in rows)
        blockers = sorted({
            str(reason)
            for row in rows
            for reason in row.get("nomination_blockers", [])
            if isinstance(reason, str) and reason
        })
        if not rows and counts.get(kind, 0) == 0:
            blockers = ["family_absent"]
        unsorted_status.append({
            "family_kind": kind,
            "family_count": int(counts.get(kind, 0)),
            "displayed_family_count": len(rows),
            "eligible_family_count": eligible,
            "status": "eligible" if eligible else "deferred",
            "deferral_reason_codes": [] if eligible else blockers,
        })
        for row in rows:
            rejection = row.get("typed_rejection")
            if not isinstance(rejection, Mapping):
                continue
            reason = str(rejection.get("reason_code") or "")
            if not reason.endswith("_TOOL_ERROR"):
                continue
            tool_errors.append({
                "family_kind": kind,
                "family_fingerprint": str(row.get("family_fingerprint") or ""),
                "reason_code": reason,
                "exception_class": str(rejection.get("exception_class") or ""),
            })
    direction_status = sorted(
        unsorted_status,
        key=lambda item: (item["status"] != "eligible", item["family_kind"]),
    )
    return {
        "schema_version": 1,
        "authority": "deterministic_reordering_of_packet_tool_facts",
        "backend_regression_caution": _phase3_backend_evidence(),
        "direction_status": direction_status,
        "eligible_family_count": sum(
            row.get("nomination_eligible") is True for row in siblings
        ),
        "mutation_allowed": False,
        "nomination_status": (
            "eligible"
            if any(row.get("nomination_eligible") is True for row in siblings)
            else "unavailable"
        ),
        "tool_errors": sorted(
            tool_errors,
            key=lambda item: (
                item["family_kind"], item["reason_code"],
                item["family_fingerprint"],
            ),
        ),
    }


def registered_arithmetic_capability_schema() -> dict[str, Any]:
    """Return the non-executable capability contract exposed in Phase 4."""
    return {
        "schema_version": 1,
        "target": TARGET,
        "capability_class": "read_only_registered_arithmetic_observation",
        "evidence_document": "registered_arithmetic_decision_packet.json",
        "execution_status": "executable_nomination_gated",
        "default_enabled": False,
        "mutation_allowed": True,
        "nomination_requires": [
            "positive_live_wall_overlap",
            "complete_word_data_control_schedule_contract",
            "exact_proof_binding",
        ],
        "fixed_product_fallback_after_product_sum_rejection": False,
    }


def _bounded_int(value: Any, *, maximum: int) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        return None
    return value


def _family_priority_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """Rank only current-seed evidence; never consume a historical recipe.

    Complete proved transactions first own the earliest live path, then the
    widest recovered transaction, then sampled wall coverage.  Unknown facts
    sort behind known current-seed facts instead of becoming implicit zeros.
    """
    earliest = item.get("earliest_covered_rank")
    scope = item.get("transaction_scope_count")
    return (
        -int(item.get("nomination_eligible") is True),
        int(earliest) if isinstance(earliest, int) else 10**9,
        -int(scope) if isinstance(scope, int) else 0,
        -int(item.get("live_wall_coverage_ppm") or 0),
        str(item.get("family_kind") or ""),
        str(item.get("family_fingerprint") or ""),
    )


def _serial_depth(kind: str, row: Mapping[str, Any]) -> int | str:
    fields = {
        "recurrence_transport": "transition_signature",
        "fixed_product": "cone_cell_count",
        "two_operand_product": "cone_cell_count",
        "registered_motif": "type_signature",
    }
    value = row.get(fields[kind])
    if isinstance(value, list) and len(value) <= 256:
        return len(value)
    bounded = _bounded_int(value, maximum=256)
    return bounded if bounded is not None else _UNKNOWN


def _sibling_support(kind: str, row: Mapping[str, Any]) -> int | str:
    fields = {
        "recurrence_transport": "source_register_count",
        "fixed_product": "occurrence_count",
        "two_operand_product": "occurrence_count",
        "registered_motif": "occurrence_count",
    }
    value = _bounded_int(row.get(fields[kind]), maximum=1_000_000)
    return value if value is not None else _UNKNOWN


def _normalized_family(
    kind: str,
    row: Mapping[str, Any],
    *,
    resolved_path_count: int,
) -> dict[str, Any] | None:
    if (
        row.get("proof_status") != "hypothesis_only"
        or row.get("mutation_allowed") not in (None, False)
        or row.get("authority") not in (None, _MINER_AUTHORITY)
    ):
        return None
    covered = _bounded_int(row.get("critical_path_count"), maximum=16)
    if covered is None or covered > resolved_path_count:
        return None
    coverage_ppm = (
        int(1_000_000 * covered / resolved_path_count)
        if resolved_path_count > 0
        else 0
    )
    path_coverage = row.get("critical_path_coverage")
    earliest_rank = (
        _bounded_int(path_coverage.get("earliest_covered_rank"), maximum=16)
        if isinstance(path_coverage, Mapping) else None
    )
    if earliest_rank == 0:
        earliest_rank = None
    sibling_support = _sibling_support(kind, row)
    normalized = {
        "family_kind": kind,
        "sibling_support_count": sibling_support,
        "control_entropy": _UNKNOWN,
        "term_coverage": _UNKNOWN,
        "serial_depth": _serial_depth(kind, row),
        "live_wall_covered_path_count": covered,
        "live_wall_resolved_path_count": resolved_path_count,
        "live_wall_coverage_ppm": coverage_ppm,
        "earliest_covered_rank": earliest_rank or _UNKNOWN,
        "unresolved_earlier_path_count": (
            earliest_rank - 1 if earliest_rank is not None else _UNKNOWN
        ),
        "transaction_scope_count": sibling_support,
        "expected_replaced_instance_count": _UNKNOWN,
        "missing_obligations": list(_MISSING_OBLIGATIONS[kind]),
        "contract_status": "incomplete",
        "proof_template_id": _UNKNOWN,
        "plan_digest": _UNKNOWN,
        "lowering_status": _UNKNOWN,
        "lowering_backend": _UNKNOWN,
        "observation_priority": (
            "eligible" if covered > 0 else "deprioritized_zero_coverage"
        ),
        "nomination_eligible": False,
        "nomination_blockers": [
            *(["zero_live_wall_overlap"] if covered == 0 else []),
            "exact_contract_not_recovered",
            "proof_not_proved",
        ],
        "proof_status": "hypothesis_only",
        "typed_rejection": None,
    }
    normalized["family_fingerprint"] = object_digest(normalized)
    return normalized


def _path_coverage(cone_cells: list[str], critical_paths: list[list[str]]) -> int:
    cells = {str(item) for item in cone_cells if isinstance(item, str) and item}
    return sum(
        any(
            token == cell or token.startswith(cell + "/")
            for token in path
            for cell in cells
        )
        for path in critical_paths
    )


def screen_recovered_recurrence_transport(
    facts: Any,
    *,
    critical_paths: list[list[str]],
    source_artifact_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind one fresh recurrence family to its dedicated exact backend.

    This path never invokes or falls back to a product-sum lowering. The public
    row contains only anonymous normalized facts; the returned proof binding is
    consumed mechanically by the seed-bound action menu.
    """
    from ..recurrence_action import build_recurrence_family_action_plans
    from .dcp_validation import recurrence_plan_from_facts
    from .lowering import select_plan_lowering

    if (
        not isinstance(facts, dict)
        or facts.get("kind") != "dsp_recurrence_family_contract_facts"
        or facts.get("status") != "success"
        or facts.get("fresh_rediscovery") is not True
        or facts.get("design_sha256") != source_artifact_sha256
        or facts.get("mutation_eligible") is not False
        or not isinstance(critical_paths, list)
        or len(critical_paths) > 16
    ):
        raise ValueError("RECURRENCE_CURRENT_SOURCE_CONTRACT_REQUIRED")
    contract_facts = dict(facts)
    contract_facts.pop("fresh_rediscovery", None)
    contract_facts.pop("runtime_s", None)
    plan = recurrence_plan_from_facts(contract_facts)
    lowering = select_plan_lowering(plan)
    decision = lowering.get("decision") or {}
    if (
        lowering.get("status") != "success"
        or decision.get("status") != "selected"
        or decision.get("backend_id") != "fir_dsp_srl_recurrence_backend"
    ):
        raise ValueError("RECURRENCE_TYPED_LOWERING_NOT_SELECTED")
    action_plans = build_recurrence_family_action_plans(
        facts, max_segment_candidates=1
    )
    if len(action_plans) != 1:
        raise ValueError("RECURRENCE_EXACT_PROOF_UNAVAILABLE")
    authorization = action_plans[0].get("authorization") or {}
    authorization_digest = str(authorization.get("certificate_id") or "")
    if _SHA256_RE.fullmatch(authorization_digest) is None:
        raise ValueError("RECURRENCE_EXACT_PROOF_UNAVAILABLE")
    objects: list[str] = []
    for boundary in facts.get("boundaries") or []:
        if not isinstance(boundary, Mapping):
            continue
        for field in ("source_dsp", "sink_dsp"):
            value = boundary.get(field)
            if isinstance(value, str) and value:
                objects.append(value)
        rewrite = boundary.get("rewrite_plan") or {}
        reinjection = rewrite.get("reinjection_dsp") if isinstance(rewrite, Mapping) else None
        if isinstance(reinjection, str) and reinjection:
            objects.append(reinjection)
        for bit in boundary.get("relay_bits") or []:
            if not isinstance(bit, Mapping):
                continue
            ff = bit.get("ff") or {}
            cell = ff.get("cell") if isinstance(ff, Mapping) else None
            if isinstance(cell, str) and cell:
                objects.append(cell)
    covered = _path_coverage(objects, critical_paths)
    fingerprint = str(facts.get("family_contract_sha256") or "")
    if _SHA256_RE.fullmatch(fingerprint) is None:
        raise ValueError("RECURRENCE_CURRENT_SOURCE_CONTRACT_REQUIRED")
    boundary_count = int(facts.get("boundary_count") or 0)
    row = {
        "family_kind": "recurrence_transport",
        "sibling_support_count": boundary_count,
        "control_entropy": 0,
        "term_coverage": 1_000_000,
        "serial_depth": min(256, boundary_count),
        "live_wall_covered_path_count": covered,
        "live_wall_resolved_path_count": len(critical_paths),
        "live_wall_coverage_ppm": (
            int(1_000_000 * covered / len(critical_paths))
            if critical_paths else 0
        ),
        "earliest_covered_rank": _UNKNOWN,
        "unresolved_earlier_path_count": _UNKNOWN,
        "transaction_scope_count": boundary_count,
        "expected_replaced_instance_count": boundary_count,
        "missing_obligations": [],
        "contract_status": "complete",
        "proof_template_id": "DSP_PREG_SRL_RECURRENCE_CUT_V1",
        "plan_digest": fingerprint,
        "lowering_status": "selected",
        "lowering_backend": "fir_dsp_srl_recurrence_backend",
        "observation_priority": (
            "eligible" if covered else "deprioritized_zero_coverage"
        ),
        "nomination_eligible": covered > 0,
        "nomination_blockers": [] if covered > 0 else ["zero_live_wall_overlap"],
        "proof_status": "proved",
        "family_fingerprint": fingerprint,
        "typed_rejection": None,
    }
    binding = {
        "status": "proved",
        "recovered_plan_digest": fingerprint,
        "authorization_digest": authorization_digest,
        "execution_backend_status": "admitted",
        "execution_backend": "dsp_srl_recurrence_cut_v1",
    }
    return row, binding


def screen_recovered_registered_arithmetic(
    *,
    fixed_point_facts_set: Any,
    product_sum_facts_set: Any,
    critical_paths: list[list[str]],
    source_artifact_sha256: str,
    product_sum_recovery_error: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Recover plans and proofs before exposing nomination eligibility."""
    from ..registered_arithmetic_adapters import adapt_fixed_point_contract_facts
    from .dcp_validation import product_sum_plan_from_facts
    from .lowering import select_plan_lowering
    from .proof import screen_and_prove

    if _SHA256_RE.fullmatch(source_artifact_sha256) is None:
        return []
    if not isinstance(critical_paths, list) or len(critical_paths) > 16:
        return []
    sources = (
        ("fixed_product", fixed_point_facts_set, adapt_fixed_point_contract_facts),
        ("product_sum", product_sum_facts_set, product_sum_plan_from_facts),
    )
    screened = []
    for family_kind, facts_set, adapter in sources:
        if not isinstance(facts_set, Mapping):
            continue
        if facts_set.get("design_sha256") != source_artifact_sha256:
            continue
        candidates = facts_set.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 64:
            continue
        for facts in candidates:
            if not isinstance(facts, Mapping):
                continue
            fallback_fingerprint = str(
                facts.get("region_sha256") or object_digest(dict(facts))
            )
            try:
                plan = adapter(facts)
                proof = screen_and_prove(plan, timeout_ms=5000)
                lowering = select_plan_lowering(plan)
                boundary = facts.get("boundary") or {}
                cone_rows = boundary.get("cone_cells") or []
                cone_cells = [
                    str(item.get("cell"))
                    for item in cone_rows
                    if isinstance(item, Mapping) and isinstance(item.get("cell"), str)
                ]
                cone_cells.extend(
                    str(item.get("cell"))
                    for item in (boundary.get("output_bus") or [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("cell"), str)
                )
                covered = _path_coverage(cone_cells, critical_paths)
                proof_body = proof.get("proof") or {}
                proof_proved = (
                    proof.get("status") == "success"
                    and proof_body.get("status")
                    in {"proved", "proved_for_measurement_probe"}
                )
                lowering_selected = (
                    lowering.get("status") == "success"
                    and (lowering.get("decision") or {}).get("status") == "selected"
                )
                lowering_backend = str(
                    (lowering.get("decision") or {}).get("backend_id") or _UNKNOWN
                )
                integrated_backend = lowering_backend in {
                    "existing_dsp48e2_fixed_point_emitter",
                    "existing_adjacent_pair_wide_dsp_emitter",
                    "registered_product_sum_dcp_bridge",
                }
                accumulation_orders = {
                    getattr(group.accumulation_order, "value", group.accumulation_order)
                    for group in plan.accumulation_groups
                }
                partitioned_residue = (
                    family_kind == "product_sum"
                    and "partitioned_full_precision_then_shift"
                    in accumulation_orders
                )
                missing = list(proof_body.get("missing_obligations") or [])
                if not proof_proved and not missing:
                    missing = ["exact_arithmetic_proof"]
                blockers = [
                    *(["zero_live_wall_overlap"] if covered == 0 else []),
                    *(["missing_contract_obligations"] if missing else []),
                    *(["proof_not_proved"] if not proof_proved else []),
                    *(["lowering_not_selected"] if not lowering_selected else []),
                    *(
                        ["integrated_dcp_backend_unavailable"]
                        if lowering_selected and not integrated_backend
                        else []
                    ),
                    *(
                        ["partitioned_order_residue_not_complete_family"]
                        if partitioned_residue else []
                    ),
                ]
                fingerprint = plan.plan_digest()
                normalized = {
                    "family_kind": family_kind,
                    "sibling_support_count": 1,
                    "control_entropy": 0,
                    "term_coverage": 1_000_000,
                    "serial_depth": min(len(cone_cells), 256),
                    "live_wall_covered_path_count": covered,
                    "live_wall_resolved_path_count": len(critical_paths),
                    "live_wall_coverage_ppm": (
                        int(1_000_000 * covered / len(critical_paths))
                        if critical_paths else 0
                    ),
                    "earliest_covered_rank": _UNKNOWN,
                    "unresolved_earlier_path_count": _UNKNOWN,
                    "transaction_scope_count": 1,
                    "expected_replaced_instance_count": 1,
                    "missing_obligations": missing,
                    "contract_status": "complete" if not missing else "incomplete",
                    "proof_template_id": str(
                        proof_body.get("proof_template_id") or _UNKNOWN
                    ),
                    "plan_digest": fingerprint,
                    "lowering_status": "selected" if lowering_selected else "rejected",
                    "lowering_backend": lowering_backend,
                    "observation_priority": (
                        "eligible" if covered else "deprioritized_zero_coverage"
                    ),
                    "nomination_eligible": not blockers,
                    "nomination_blockers": blockers,
                    "proof_status": "proved" if proof_proved else "rejected",
                    "family_fingerprint": fingerprint,
                    "typed_rejection": (
                        {
                            "reason_code": (
                                "REGARITH_PARTITIONED_PRODUCT_SUM_PRODUCTION_BACKEND_UNSUPPORTED"
                            ),
                            "message": (
                                "partitioned-order residue is not a recovered "
                                "complete-family production action"
                            ),
                        }
                        if partitioned_residue else None
                    ),
                }
            except Exception as exc:  # typed tool failures remain structured
                reason_code = str(getattr(exc, "reason_code", "CONTRACT_RECOVERY_REJECTED"))
                normalized = {
                    "family_kind": family_kind,
                    "sibling_support_count": 1,
                    "control_entropy": _UNKNOWN,
                    "term_coverage": _UNKNOWN,
                    "serial_depth": _UNKNOWN,
                    "live_wall_covered_path_count": 0,
                    "live_wall_resolved_path_count": len(critical_paths),
                    "live_wall_coverage_ppm": 0,
                    "earliest_covered_rank": _UNKNOWN,
                    "unresolved_earlier_path_count": _UNKNOWN,
                    "transaction_scope_count": 1,
                    "expected_replaced_instance_count": _UNKNOWN,
                    "missing_obligations": [reason_code],
                    "contract_status": "rejected",
                    "proof_template_id": _UNKNOWN,
                    "plan_digest": _UNKNOWN,
                    "lowering_status": _UNKNOWN,
                    "lowering_backend": _UNKNOWN,
                    "observation_priority": "deprioritized_zero_coverage",
                    "nomination_eligible": False,
                    "nomination_blockers": [
                        "zero_live_wall_overlap",
                        "missing_contract_obligations",
                        "proof_not_proved",
                    ],
                    "proof_status": "rejected",
                    "family_fingerprint": fallback_fingerprint,
                    "typed_rejection": {
                        "reason_code": reason_code,
                        "exception_class": type(exc).__name__,
                    },
                }
            screened.append(normalized)
    if isinstance(product_sum_recovery_error, Mapping):
        reason_code = str(
            product_sum_recovery_error.get("reason_code")
            or "PRODUCT_SUM_RECOVERY_TOOL_ERROR"
        )
        exception_class = str(
            product_sum_recovery_error.get("exception_class") or "UnknownError"
        )
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", reason_code) is not None
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", exception_class)
            is not None
        ):
            rejection = {
                "reason_code": reason_code,
                "exception_class": exception_class,
            }
            payload = {
                "family_kind": "product_sum",
                "source_artifact_sha256": source_artifact_sha256,
                "typed_rejection": rejection,
            }
            screened.append({
                "family_kind": "product_sum",
                "sibling_support_count": 0,
                "control_entropy": _UNKNOWN,
                "term_coverage": _UNKNOWN,
                "serial_depth": _UNKNOWN,
                "live_wall_covered_path_count": 0,
                "live_wall_resolved_path_count": len(critical_paths),
                "live_wall_coverage_ppm": 0,
                "earliest_covered_rank": _UNKNOWN,
                "unresolved_earlier_path_count": _UNKNOWN,
                "transaction_scope_count": 0,
                "expected_replaced_instance_count": _UNKNOWN,
                "missing_obligations": [reason_code],
                "contract_status": "rejected",
                "proof_template_id": _UNKNOWN,
                "plan_digest": _UNKNOWN,
                "lowering_status": _UNKNOWN,
                "lowering_backend": _UNKNOWN,
                "observation_priority": "deprioritized_zero_coverage",
                "nomination_eligible": False,
                "nomination_blockers": [
                    reason_code,
                    "missing_contract_obligations",
                    "proof_not_proved",
                ],
                "proof_status": "rejected",
                "family_fingerprint": object_digest(payload),
                "typed_rejection": rejection,
            })
    return screened


def build_registered_arithmetic_decision_packet(
    operator_mining_root: Any,
    *,
    expected_design_sha256: str,
    screened_families: Any = None,
) -> dict[str, Any] | None:
    """Build one anonymous decision packet from exact-incumbent miner output."""
    root = operator_mining_root if isinstance(operator_mining_root, Mapping) else {}
    if (
        root.get("status") != "success"
        or root.get("version") != "operator-mining-v2"
        or root.get("authority") != _MINER_AUTHORITY
        or root.get("read_only") is not True
        or root.get("mutation_allowed") is not False
        or root.get("proof_status") != "hypothesis_only"
        or root.get("design_sha256") != expected_design_sha256
        or _SHA256_RE.fullmatch(expected_design_sha256) is None
    ):
        return None
    scope = root.get("scope")
    if not isinstance(scope, Mapping):
        return None
    supplied = _bounded_int(scope.get("critical_paths_supplied"), maximum=16)
    resolved = _bounded_int(scope.get("critical_paths_resolved"), maximum=16)
    if supplied is None or resolved is None or resolved > supplied:
        return None

    counts: dict[str, int] = {"product_sum": 0}
    siblings: list[dict[str, Any]] = []
    details_truncated = False
    for kind, collection_field, count_field in _COLLECTIONS:
        rows = root.get(collection_field)
        if not isinstance(rows, list) or len(rows) > 16:
            return None
        total = len(rows)
        if count_field is not None:
            count = _bounded_int(root.get(count_field), maximum=64)
            if count is None or count < len(rows):
                return None
            total = count
            details_truncated = details_truncated or count > len(rows)
        counts[kind] = total
        for row in rows:
            if not isinstance(row, Mapping):
                return None
            family = _normalized_family(kind, row, resolved_path_count=resolved)
            if family is None:
                return None
            siblings.append(family)

    if screened_families is not None:
        if not isinstance(screened_families, list) or len(screened_families) > 64:
            return None
        screened_kinds = {
            str(item.get("family_kind") or "")
            for item in screened_families
            if isinstance(item, Mapping)
        }
        # Exact recovered rows supersede cheap hypotheses of the same class;
        # keeping both would double-count one family and could make an
        # ineligible hypothesis look like a near-tied exact sibling.
        siblings = [
            item for item in siblings
            if item.get("family_kind") not in screened_kinds
        ]
        for family in screened_families:
            if not isinstance(family, Mapping):
                return None
            row = copy.deepcopy(dict(family))
            siblings.append(row)
            kind = str(row.get("family_kind") or "")
            if kind in counts:
                counts[kind] = sum(
                    item.get("family_kind") == kind for item in screened_families
                    if isinstance(item, Mapping)
                )

    details_truncated = any(
        counts[kind]
        > sum(item.get("family_kind") == kind for item in siblings)
        for kind in counts
    )

    siblings.sort(key=_family_priority_key)
    for index, family in enumerate(siblings, 1):
        family.setdefault("discovery_state", "fresh_in_round")
        family.setdefault("prefetched_seed_sha256", "")
        family.setdefault("prefetch_mutation_authority", False)
        family["family_alias"] = f"registered_family_{index:02d}"

    covered_count = sum(item["live_wall_covered_path_count"] > 0 for item in siblings)
    zero_count = sum(item["live_wall_covered_path_count"] == 0 for item in siblings)
    eligible_count = sum(item.get("nomination_eligible") is True for item in siblings)
    packet = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "action_decision_summary": _action_decision_summary(siblings, counts),
        "target": TARGET,
        "source_artifact_sha256": expected_design_sha256,
        "evidence_status": "available",
        "critical_path_scope": {
            "supplied_path_count": supplied,
            "resolved_path_count": resolved,
            "coverage_metric": "miner_reported_family_path_membership",
        },
        "family_counts": counts,
        "displayed_sibling_count": len(siblings),
        "sibling_details_truncated": details_truncated,
        "positive_live_wall_family_count": covered_count,
        "zero_live_wall_family_count": zero_count,
        "eligible_family_count": eligible_count,
        "observation_status": (
            "eligible" if covered_count else "unavailable_no_live_wall_overlap"
        ),
        "nomination_status": "eligible" if eligible_count else "unavailable",
        "normalized_siblings": siblings,
        "capability": registered_arithmetic_capability_schema(),
        "phase3_backend_evidence": _phase3_backend_evidence(),
        "failed_product_sum_fixed_product_fallback_allowed": False,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "hypothesis_only",
    }
    packet["result_digest"] = object_digest(packet)
    return packet


def refresh_registered_arithmetic_product_sum_slice(
    previous_packet: Any,
    *,
    expected_design_sha256: str,
    screened_product_sum_families: Any,
) -> dict[str, Any] | None:
    """Replace only one failed current-seed family slice after a bounded retry.

    Non-product rows remain bound to the same source packet.  The retry cannot
    import a family, plan, or parameter from another seed or run; all new rows
    must come from the exact current-source cold provider.
    """
    previous = validate_registered_arithmetic_decision_packet(
        previous_packet,
        expected_design_sha256=expected_design_sha256,
    )
    if (
        previous is None
        or not isinstance(screened_product_sum_families, list)
        or len(screened_product_sum_families) > 64
        or any(
            not isinstance(item, Mapping)
            or item.get("family_kind") != "product_sum"
            for item in screened_product_sum_families
        )
    ):
        return None

    siblings = [
        copy.deepcopy(item)
        for item in previous["normalized_siblings"]
        if item.get("family_kind") != "product_sum"
    ]
    siblings.extend(copy.deepcopy(dict(item)) for item in screened_product_sum_families)
    siblings.sort(key=_family_priority_key)
    for index, family in enumerate(siblings, 1):
        family["family_alias"] = f"registered_family_{index:02d}"
        family.setdefault("discovery_state", "fresh_in_round")
        family.setdefault("prefetched_seed_sha256", "")
        family.setdefault("prefetch_mutation_authority", False)

    counts = copy.deepcopy(previous["family_counts"])
    counts["product_sum"] = len(screened_product_sum_families)
    covered_count = sum(
        item["live_wall_covered_path_count"] > 0 for item in siblings
    )
    zero_count = sum(
        item["live_wall_covered_path_count"] == 0 for item in siblings
    )
    eligible_count = sum(
        item.get("nomination_eligible") is True for item in siblings
    )
    packet = {
        key: copy.deepcopy(value)
        for key, value in previous.items()
        if key not in {
            "result_digest", "action_decision_summary", "family_counts",
            "displayed_sibling_count", "sibling_details_truncated",
            "positive_live_wall_family_count", "zero_live_wall_family_count",
            "eligible_family_count", "observation_status", "nomination_status",
            "normalized_siblings",
        }
    }
    packet.update({
        "action_decision_summary": _action_decision_summary(siblings, counts),
        "family_counts": counts,
        "displayed_sibling_count": len(siblings),
        "sibling_details_truncated": any(
            counts[kind]
            > sum(item.get("family_kind") == kind for item in siblings)
            for kind in counts
        ),
        "positive_live_wall_family_count": covered_count,
        "zero_live_wall_family_count": zero_count,
        "eligible_family_count": eligible_count,
        "observation_status": (
            "eligible" if covered_count else "unavailable_no_live_wall_overlap"
        ),
        "nomination_status": "eligible" if eligible_count else "unavailable",
        "normalized_siblings": siblings,
    })
    packet["result_digest"] = object_digest(packet)
    return validate_registered_arithmetic_decision_packet(
        packet,
        expected_design_sha256=expected_design_sha256,
    )


def validate_registered_arithmetic_decision_packet(
    value: Any,
    *,
    expected_design_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Strictly validate an already-public registered-arithmetic packet."""
    packet = copy.deepcopy(value) if isinstance(value, dict) else {}
    expected_fields = {
        "schema_version", "authority", "target", "source_artifact_sha256",
        "evidence_status", "critical_path_scope", "family_counts",
        "displayed_sibling_count", "sibling_details_truncated",
        "positive_live_wall_family_count", "zero_live_wall_family_count",
        "eligible_family_count",
        "observation_status", "nomination_status", "normalized_siblings",
        "capability", "phase3_backend_evidence",
        "action_decision_summary",
        "failed_product_sum_fixed_product_fallback_allowed", "read_only",
        "mutation_allowed", "proof_status", "result_digest",
    }
    source = packet.get("source_artifact_sha256")
    if (
        set(packet) != expected_fields
        or packet.get("schema_version") != SCHEMA_VERSION
        or packet.get("authority") != AUTHORITY
        or packet.get("target") != TARGET
        or not isinstance(source, str)
        or _SHA256_RE.fullmatch(source) is None
        or (expected_design_sha256 is not None and source != expected_design_sha256)
        or packet.get("evidence_status") != "available"
        or packet.get("nomination_status") not in {"eligible", "unavailable"}
        or packet.get("capability") != registered_arithmetic_capability_schema()
        or packet.get("failed_product_sum_fixed_product_fallback_allowed")
        is not False
        or packet.get("read_only") is not True
        or packet.get("mutation_allowed") is not False
        or packet.get("proof_status") != "hypothesis_only"
    ):
        return None
    digest = packet.pop("result_digest", None)
    if not isinstance(digest, str) or object_digest(packet) != digest:
        return None
    packet["result_digest"] = digest

    scope = packet.get("critical_path_scope")
    counts = packet.get("family_counts")
    siblings = packet.get("normalized_siblings")
    if (
        not isinstance(scope, dict)
        or set(scope)
        != {"supplied_path_count", "resolved_path_count", "coverage_metric"}
        or scope.get("coverage_metric") != "miner_reported_family_path_membership"
        or _bounded_int(scope.get("supplied_path_count"), maximum=16) is None
        or _bounded_int(scope.get("resolved_path_count"), maximum=16) is None
        or scope["resolved_path_count"] > scope["supplied_path_count"]
        or not isinstance(counts, dict)
        or set(counts) != _PUBLIC_FAMILY_KINDS
        or any(_bounded_int(item, maximum=64) is None for item in counts.values())
        or not isinstance(siblings, list)
        or len(siblings) > 64
        or packet.get("displayed_sibling_count") != len(siblings)
        or not isinstance(packet.get("sibling_details_truncated"), bool)
    ):
        return None
    for index, family in enumerate(siblings, 1):
        if not isinstance(family, dict):
            return None
        expected_family_fields = {
            "family_kind", "sibling_support_count", "control_entropy",
            "term_coverage", "serial_depth", "live_wall_covered_path_count",
            "live_wall_resolved_path_count", "live_wall_coverage_ppm",
            "missing_obligations", "observation_priority", "nomination_eligible",
            "nomination_blockers", "proof_status", "family_fingerprint",
            "family_alias", "contract_status", "proof_template_id",
            "plan_digest", "lowering_status", "lowering_backend",
            "typed_rejection", "discovery_state", "prefetched_seed_sha256",
            "prefetch_mutation_authority",
            "earliest_covered_rank", "unresolved_earlier_path_count",
            "transaction_scope_count", "expected_replaced_instance_count",
        }
        alias = family.get("family_alias")
        covered = family.get("live_wall_covered_path_count")
        resolved = scope["resolved_path_count"]
        missing = family.get("missing_obligations")
        contract_complete = (
            family.get("contract_status") == "complete" and missing == []
        )
        proof_proved = family.get("proof_status") == "proved"
        typed_rejection = family.get("typed_rejection")
        discovery_state = family.get("discovery_state")
        prefetched_seed = family.get("prefetched_seed_sha256")
        lowering_selected = family.get("lowering_status") == "selected"
        earliest_rank = family.get("earliest_covered_rank")
        unresolved_earlier = family.get("unresolved_earlier_path_count")
        transaction_scope = family.get("transaction_scope_count")
        replaced_instances = family.get("expected_replaced_instance_count")
        integrated_backend = family.get("lowering_backend") in {
            "existing_dsp48e2_fixed_point_emitter",
            "existing_adjacent_pair_wide_dsp_emitter",
            "registered_product_sum_dcp_bridge",
            "fir_dsp_srl_recurrence_backend",
        }
        expected_eligible = bool(
            isinstance(covered, int)
            and not isinstance(covered, bool)
            and covered > 0
            and contract_complete
            and proof_proved
            and lowering_selected
            and integrated_backend
        )
        if (
            set(family) != expected_family_fields
            or family.get("family_kind") not in _PUBLIC_FAMILY_KINDS
            or alias != f"registered_family_{index:02d}"
            or not isinstance(family.get("family_fingerprint"), str)
            or _SHA256_RE.fullmatch(family["family_fingerprint"]) is None
            or (
                family.get("control_entropy") != _UNKNOWN
                and _bounded_int(family.get("control_entropy"), maximum=64) is None
            )
            or (
                family.get("term_coverage") != _UNKNOWN
                and _bounded_int(family.get("term_coverage"), maximum=1_000_000)
                is None
            )
            or _bounded_int(covered, maximum=16) is None
            or covered > resolved
            or family.get("live_wall_resolved_path_count") != resolved
            or family.get("live_wall_coverage_ppm") != (
                int(1_000_000 * covered / resolved) if resolved else 0
            )
            or (
                earliest_rank != _UNKNOWN
                and (
                    _bounded_int(earliest_rank, maximum=16) in (None, 0)
                    or earliest_rank > resolved
                )
            )
            or unresolved_earlier != (
                earliest_rank - 1 if isinstance(earliest_rank, int) else _UNKNOWN
            )
            or (
                transaction_scope != _UNKNOWN
                and _bounded_int(transaction_scope, maximum=1_000_000) is None
            )
            or (
                replaced_instances != _UNKNOWN
                and _bounded_int(replaced_instances, maximum=1_000_000) is None
            )
            or family.get("observation_priority") not in (
                {"eligible", "partial_evidence"}
                if covered else {"deprioritized_zero_coverage"}
            )
            or family.get("nomination_eligible") is not expected_eligible
            or family.get("proof_status")
            not in {"hypothesis_only", "proved", "rejected", "unavailable"}
            or not isinstance(missing, list)
            or any(not isinstance(item, str) or not item for item in missing)
            or not isinstance(family.get("nomination_blockers"), list)
            or bool(family.get("nomination_blockers")) is expected_eligible
            or family.get("contract_status")
            not in {"incomplete", "complete", "rejected", "unavailable"}
            or discovery_state not in {"fresh_in_round", "prefetched_and_bound"}
            or family.get("prefetch_mutation_authority") is not False
            or (
                discovery_state == "prefetched_and_bound"
                and prefetched_seed != source
            )
            or (
                discovery_state == "fresh_in_round"
                and prefetched_seed != ""
            )
            or (
                typed_rejection is not None
                and (
                    not isinstance(typed_rejection, dict)
                    or set(typed_rejection)
                    not in (
                        {"reason_code", "exception_class"},
                        {"reason_code", "message"},
                    )
                    or not isinstance(typed_rejection.get("reason_code"), str)
                    or re.fullmatch(
                        r"[A-Z][A-Z0-9_]{2,127}",
                        typed_rejection["reason_code"],
                    )
                    is None
                    or (
                        "exception_class" in typed_rejection
                        and (
                            not isinstance(
                                typed_rejection.get("exception_class"), str
                            )
                            or re.fullmatch(
                                r"[A-Za-z_][A-Za-z0-9_.]{0,127}",
                                typed_rejection["exception_class"],
                            ) is None
                            or family.get("contract_status")
                            not in {"rejected", "unavailable"}
                            or family.get("proof_status")
                            not in {"rejected", "unavailable"}
                            or typed_rejection["reason_code"] not in missing
                        )
                    )
                    or (
                        "message" in typed_rejection
                        and (
                            not isinstance(typed_rejection.get("message"), str)
                            or not typed_rejection["message"]
                            or family.get("nomination_eligible") is not False
                        )
                    )
                )
            )
            or family.get("lowering_status")
            not in {_UNKNOWN, "selected", "rejected", "unavailable"}
            or not isinstance(family.get("proof_template_id"), str)
            or not isinstance(family.get("plan_digest"), str)
            or (
                family.get("plan_digest") != _UNKNOWN
                and _SHA256_RE.fullmatch(family["plan_digest"]) is None
            )
            or (
                contract_complete
                and family.get("plan_digest") != family.get("family_fingerprint")
            )
            or (
                family.get("sibling_support_count") != _UNKNOWN
                and _bounded_int(
                    family.get("sibling_support_count"), maximum=1_000_000
                ) is None
            )
            or (
                family.get("serial_depth") != _UNKNOWN
                and _bounded_int(family.get("serial_depth"), maximum=256) is None
            )
        ):
            return None
    if siblings != sorted(siblings, key=_family_priority_key):
        return None
    displayed_by_kind = {
        kind: sum(item["family_kind"] == kind for item in siblings)
        for kind in _PUBLIC_FAMILY_KINDS
    }
    positive = sum(item["live_wall_covered_path_count"] > 0 for item in siblings)
    zero = len(siblings) - positive
    eligible = sum(item["nomination_eligible"] is True for item in siblings)
    if (
        packet.get("positive_live_wall_family_count") != positive
        or packet.get("zero_live_wall_family_count") != zero
        or packet.get("eligible_family_count") != eligible
        or packet.get("nomination_status")
        != ("eligible" if eligible else "unavailable")
        or any(
            counts[kind] < displayed_by_kind[kind] for kind in _PUBLIC_FAMILY_KINDS
        )
        or packet.get("sibling_details_truncated")
        is not any(
            counts[kind] > displayed_by_kind[kind]
            for kind in _PUBLIC_FAMILY_KINDS
        )
        or packet.get("observation_status") != (
            "eligible" if positive else "unavailable_no_live_wall_overlap"
        )
        or packet.get("phase3_backend_evidence") != _phase3_backend_evidence()
        or packet.get("action_decision_summary")
        != _action_decision_summary(siblings, counts)
    ):
        return None
    return packet


def build_concurrent_proof_decision_packet(
    rows: Any,
    *,
    source_artifact_sha256: str,
) -> dict[str, Any]:
    """Build a bounded, tool-owned packet from typed scheduler rows."""
    if _SHA256_RE.fullmatch(str(source_artifact_sha256)) is None:
        raise ValueError("concurrent proof packet source must be a SHA-256")
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 8:
        raise ValueError("concurrent proof packet requires one to eight rows")
    normalized = []
    for value in rows:
        row = value.to_dict() if hasattr(value, "to_dict") else copy.deepcopy(value)
        if not isinstance(row, dict) or not _valid_concurrent_proof_row(row):
            raise ValueError("concurrent proof packet contains an invalid typed row")
        normalized.append(copy.deepcopy(row))
    candidate_ids = {row["candidate_id"] for row in normalized}
    if len(candidate_ids) != 1 or normalized[0]["row_type"] != "proof_in_flight":
        raise ValueError("concurrent proof rows must describe one launched candidate")
    if len(normalized) > 1 and normalized[-1]["row_type"] == "proof_in_flight":
        raise ValueError("a terminal concurrent proof row must follow in-flight")
    packet = {
        "schema_version": CONCURRENT_PROOF_SCHEMA_VERSION,
        "authority": CONCURRENT_PROOF_AUTHORITY,
        "source_artifact_sha256": str(source_artifact_sha256),
        "candidate_id": normalized[0]["candidate_id"],
        "current_state": normalized[-1]["row_type"],
        "rows": normalized,
        "mutation_allowed": False,
        "promotion_allowed": False,
        "llm_approval_authority": False,
    }
    packet["result_digest"] = object_digest(packet)
    return packet


def validate_concurrent_proof_decision_packet(
    value: Any,
    *,
    expected_source_artifact_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Validate a public proof-state packet without granting authority."""
    packet = copy.deepcopy(value) if isinstance(value, dict) else {}
    expected_fields = {
        "schema_version",
        "authority",
        "source_artifact_sha256",
        "candidate_id",
        "current_state",
        "rows",
        "mutation_allowed",
        "promotion_allowed",
        "llm_approval_authority",
        "result_digest",
    }
    source = packet.get("source_artifact_sha256")
    rows = packet.get("rows")
    if (
        set(packet) != expected_fields
        or packet.get("schema_version") != CONCURRENT_PROOF_SCHEMA_VERSION
        or packet.get("authority") != CONCURRENT_PROOF_AUTHORITY
        or not isinstance(source, str)
        or _SHA256_RE.fullmatch(source) is None
        or (
            expected_source_artifact_sha256 is not None
            and source != expected_source_artifact_sha256
        )
        or packet.get("mutation_allowed") is not False
        or packet.get("promotion_allowed") is not False
        or packet.get("llm_approval_authority") is not False
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= 8
        or any(not isinstance(row, dict) or not _valid_concurrent_proof_row(row) for row in rows)
        or len({row.get("candidate_id") for row in rows}) != 1
        or packet.get("candidate_id") != rows[0].get("candidate_id")
        or rows[0].get("row_type") != "proof_in_flight"
        or packet.get("current_state") != rows[-1].get("row_type")
    ):
        return None
    digest = packet.pop("result_digest", None)
    if not isinstance(digest, str) or object_digest(packet) != digest:
        return None
    packet["result_digest"] = digest
    return packet


def _valid_concurrent_proof_row(row: dict[str, Any]) -> bool:
    common = {
        "schema",
        "candidate_id",
        "proof_class",
        "row_type",
        "promotion_allowed",
        "llm_approval_authority",
    }
    row_type = row.get("row_type")
    extras = {
        "proof_in_flight": {"parent_candidate_id", "timeout_s", "speculation_depth"},
        "proof_failed_real": {"proof_id", "adjudication"},
        "proof_failed_misbound": {"proof_id", "adjudication"},
        "proof_timeout": {"timeout_s", "solver_result", "hard_timeout"},
    }.get(row_type)
    if (
        row_type not in _CONCURRENT_ROW_TYPES
        or extras is None
        or set(row) != common | extras
        or row.get("schema") != "concurrent_proof_packet_row_v1"
        or not isinstance(row.get("candidate_id"), str)
        or re.fullmatch(r"c[0-9]{6}", row["candidate_id"]) is None
        or not isinstance(row.get("proof_class"), str)
        or row.get("promotion_allowed") is not False
        or row.get("llm_approval_authority") is not False
    ):
        return False
    if row_type == "proof_in_flight":
        return (
            isinstance(row.get("parent_candidate_id"), str)
            and isinstance(row.get("timeout_s"), (int, float))
            and not isinstance(row.get("timeout_s"), bool)
            and float(row["timeout_s"]) > 0.0
            and row.get("speculation_depth") == 1
        )
    if row_type == "proof_timeout":
        return (
            isinstance(row.get("timeout_s"), (int, float))
            and not isinstance(row.get("timeout_s"), bool)
            and float(row["timeout_s"]) >= 0.0
            and row.get("solver_result") == "unknown"
            and row.get("hard_timeout") is True
        )
    adjudication = row.get("adjudication")
    expected_verdict = (
        "failed_real" if row_type == "proof_failed_real" else "failed_misbound"
    )
    return (
        isinstance(row.get("proof_id"), str)
        and bool(row["proof_id"])
        and isinstance(adjudication, dict)
        and adjudication.get("schema") == "counterexample_replay_adjudication_v1"
        and adjudication.get("verdict") == expected_verdict
        and adjudication.get("authority")
        == "mechanical_classification_only_no_promotion_authority"
    )
