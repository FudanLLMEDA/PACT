"""Neutral, identifier-free projection of live operator-extraction telemetry.

The ordinary analysis payload is intentionally rich: it contains names and
physical details needed by internal optimization consumers.  This module is a
separate, allowlist-only public projection.  It never recursively sanitizes or
copies an input object, so a newly added internal identifier cannot
accidentally become public evidence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Callable


_UNKNOWN = "unknown"
_AUTHORITY = "continuous_live_evidence_only"
_MOTIF_AUTHORITY = "generic_structural_hypothesis_only"
_RAW_OPERATOR_MINING_AUTHORITY = "exact_dcp_structural_hypothesis_only"
_RAW_OPERATOR_MINING_VERSION = "operator-mining-v2"
_RAW_MOTIF_VERSION = "operator-mining-motif-v2"
_OPERATOR_PREFLIGHT_AUTHORITY = "exact_incumbent_anonymous_hypothesis_only"
_SEMANTIC_PREFLIGHT_AUTHORITY = "exact_incumbent_anonymous_action_preflight"
_MOTIF_SIMILARITY_METRIC = (
    "one_minus_levenshtein_distance_normalized_by_max_sequence_length"
)
_PUBLIC_LOGIC_FAMILIES = frozenset({
    "unknown",
    "lut",
    "lut1",
    "lut2",
    "lut3",
    "lut4",
    "lut5",
    "lut6",
    "carry4",
    "carry8",
    "dsp",
    "bram",
    "uram",
    "distributed_ram",
    "mux",
    "register_or_latch",
    "shift_register",
})

_DIRECTION_COUNT_FIELDS = {
    "dsp_srl_recurrence_cut_v1": "recurrence_boundary_hypothesis_count",
    "fixed_point_dsp_rebuild_v1": "fixed_point_product_hypothesis_count",
    "dsp_mul_two_operand_v1": "two_operand_product_hypothesis_count",
    "dsp_register_absorb_v1": "register_absorb_hypothesis_count",
    "dsp_pipeline_rebalance_v1": "pipeline_rebalance_hypothesis_count",
    "registered_arithmetic_cone_v1": "registered_arithmetic_eligible_family_count",
    "constructive_accumulator_rebuild_v1": (
        "constructive_accumulator_eligible_family_count"
    ),
    "memory_write_buffer_forward_v1": (
        "memory_write_buffer_forward_eligible_family_count"
    ),
    "exact_control_to_data_state_equation_v1": (
        "control_to_data_state_equation_hypothesis_count"
    ),
}

_BANKED_RAM_OPERATOR_TARGETS = frozenset({
    "registered_ram_boolean_midpoint_relocation_v1",
    "registered_control_packet_localization_v1",
})


def homogeneous_repeated_wall_needs_semantic_discriminator(value: Any) -> bool:
    """Return whether one live motif dominates enough to justify deep recovery.

    This is a read-only scheduling predicate, not mutation eligibility. It uses
    only the anonymous exact-seed motif census and deliberately requires one
    family to own at least three quarters of a nontrivial sampled wall.
    """
    report = validate_repeated_arithmetic_motif_report(value)
    if report is None:
        return False
    resolved = report["resolved_sequence_count"]
    families = report["sequence_families"]
    if resolved < 8 or not families:
        return False
    dominant = max(families, key=lambda item: item["sequence_count"])
    return bool(
        dominant["sequence_count"] / resolved >= 0.75
        and dominant["nearest_continuous_similarity"] != _UNKNOWN
        and float(dominant["nearest_continuous_similarity"]) >= 0.95
        and dominant["risk_flags"] == []
    )


def repeated_wall_has_memory_endpoint(value: Any) -> bool:
    """Return whether current live motif paths terminate at a memory primitive."""
    report = validate_repeated_arithmetic_motif_report(value)
    if report is None:
        return False
    return any(
        family["primitive_family_sequence"][-1]
        in {"bram", "uram", "distributed_ram"}
        for family in report["sequence_families"]
    )


def banked_ram_operator_preflight_facts(value: Any) -> dict[str, int]:
    """Project a fresh deep contract into anonymous menu-preflight counts."""
    result = {
        "banked_ram_control_live_path_count": 0,
        "banked_ram_boolean_midpoint_hypothesis_count": 0,
        "banked_control_packet_hypothesis_count": 0,
        "banked_ram_boolean_midpoint_live_wall_covered_path_count": 0,
        "banked_control_packet_live_wall_covered_path_count": 0,
    }
    payload = _as_mapping(value)
    if (
        payload.get("status") in {"success", "no_supported_contract"}
        and payload.get("kind") == "banked_ram_data_control_operator_contract"
        and payload.get("version") == 1
        and payload.get("fresh_rediscovery") is True
        and payload.get("read_only") is True
    ):
        live_wall = _as_mapping(_as_mapping(payload.get("contract")).get("live_wall"))
        sample_count = live_wall.get("sample_count", 0)
        raw_control_paths = live_wall.get("ram_control_path_count", 0)
        if (
            isinstance(sample_count, int)
            and not isinstance(sample_count, bool)
            and 0 <= sample_count <= 500
            and isinstance(raw_control_paths, int)
            and not isinstance(raw_control_paths, bool)
            and 0 <= raw_control_paths <= sample_count
        ):
            result["banked_ram_control_live_path_count"] = raw_control_paths
    if not (
        payload.get("status") == "success"
        and payload.get("kind") == "banked_ram_data_control_operator_contract"
        and payload.get("version") == 1
        and payload.get("fresh_rediscovery") is True
        and payload.get("read_only") is True
        and payload.get("structural_boundary_recovered") is True
        and payload.get("mutation_eligible") is True
        and payload.get("proof_status")
        == "measurement_probe_only_final_equivalence_required"
    ):
        return result
    actions = _as_mapping(payload.get("action_assessment")).get(
        "candidate_actions"
    )
    if not isinstance(actions, list):
        return result
    fields = {
        "registered_ram_boolean_midpoint_relocation_v1": (
            "banked_ram_boolean_midpoint_hypothesis_count",
            "banked_ram_boolean_midpoint_live_wall_covered_path_count",
        ),
        "registered_control_packet_localization_v1": (
            "banked_control_packet_hypothesis_count",
            "banked_control_packet_live_wall_covered_path_count",
        ),
    }
    for raw in actions:
        action = _as_mapping(raw)
        target = action.get("target")
        if (
            action.get("skill") != "operator_rewrite"
            or target not in _BANKED_RAM_OPERATOR_TARGETS
            or action.get("params") != {}
        ):
            continue
        count_field, coverage_field = fields[str(target)]
        evidence = _as_mapping(action.get("evidence"))
        raw_coverage = evidence.get("covered_path_count", 1)
        coverage = (
            raw_coverage
            if isinstance(raw_coverage, int)
            and not isinstance(raw_coverage, bool)
            and 1 <= raw_coverage <= 300
            else 1
        )
        result[count_field] = 1
        result[coverage_field] = max(result[coverage_field], coverage)
    return result


def banked_wall_needs_multilevel_control_recovery(value: Any) -> bool:
    """Return whether a live RAM-control wall escaped the local packet screen."""
    facts = banked_ram_operator_preflight_facts(value)
    return bool(
        facts["banked_ram_control_live_path_count"] > 0
        and facts["banked_control_packet_hypothesis_count"] == 0
    )


def build_operator_action_preflight(
    semantic_evidence: Any,
) -> dict[str, Any]:
    """Project exact-incumbent nomination gates without private identities.

    Constructive and memory shape matches are census evidence only.  They gain
    nomination authority only when the current seed also supplies a complete
    recovered family contract, zero missing obligations, and positive live-wall
    coverage.  This mirrors the registered-arithmetic nomination contract.
    """
    evidence = _as_mapping(semantic_evidence)
    count_fields = {
        "recurrence_boundary_hypothesis_count": (
            evidence.get("recurrence_eligible_count"), 16
        ),
        "fixed_point_product_hypothesis_count": (
            evidence.get("fixed_point_product_hypothesis_count", 0), 64
        ),
        "two_operand_product_hypothesis_count": (
            evidence.get("two_operand_product_hypothesis_count", 0), 64
        ),
        "register_absorb_hypothesis_count": (
            evidence.get("register_absorb_hypothesis_count", 0), 64
        ),
        "pipeline_rebalance_hypothesis_count": (
            evidence.get("pipeline_rebalance_hypothesis_count", 0), 64
        ),
        "registered_arithmetic_eligible_family_count": (
            evidence.get("registered_arithmetic_eligible_family_count", 0), 64
        ),
        "control_to_data_state_equation_hypothesis_count": (
            evidence.get("control_to_data_state_equation_hypothesis_count", 0),
            64,
        ),
    }
    valid = all(
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= maximum
        for value, maximum in count_fields.values()
    )

    family_specs = (
        (
            "constructive_accumulator_rebuild_v1",
            "constructive_accumulator",
            "CACC_SIBLING_FAMILY_INCOMPLETE",
            "CACC_ZERO_LIVE_WALL_COVERAGE",
        ),
        (
            "memory_write_buffer_forward_v1",
            "memory_write_buffer_forward",
            "MEMFWD_CONSUMER_CLOSURE_INCOMPLETE",
            "MEMFWD_ZERO_LIVE_WALL_COVERAGE",
        ),
    )
    family_values: dict[str, dict[str, Any]] = {}
    for target, prefix, incomplete_code, zero_wall_code in family_specs:
        shape = evidence.get(f"{prefix}_hypothesis_count", 0)
        complete = evidence.get(f"{prefix}_complete_contract_count", 0)
        missing = evidence.get(
            f"{prefix}_missing_obligation_count", 0 if complete else int(bool(shape))
        )
        covered = evidence.get(f"{prefix}_live_wall_covered_path_count", shape)
        raw_blockers = evidence.get(f"{prefix}_nomination_blockers")
        blockers = list(raw_blockers) if isinstance(raw_blockers, list) else []
        family_valid = all(
            not isinstance(value, bool)
            and isinstance(value, int)
            and 0 <= value <= maximum
            for value, maximum in (
                (shape, 16), (complete, 16), (missing, 256), (covered, 300)
            )
        ) and all(
            isinstance(item, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", item) is not None
            for item in blockers
        ) and complete <= shape
        valid = valid and family_valid
        if family_valid:
            if shape and (complete < 1 or missing > 0) and incomplete_code not in blockers:
                blockers.append(incomplete_code)
            if shape and covered == 0 and zero_wall_code not in blockers:
                blockers.append(zero_wall_code)
            eligible = bool(
                shape > 0 and complete > 0 and missing == 0 and covered > 0
                and not blockers
            )
            family_values[target] = {
                "hypothesis_count": shape,
                "complete_recovered_contract_count": complete,
                "missing_obligation_count": missing,
                "live_wall_covered_path_count": covered,
                "live_wall_overlap": covered > 0,
                "nomination_eligible": eligible,
                "nomination_blockers": blockers,
                "attempt_status": (
                    "eligible_for_reversible_measurement"
                    if eligible else "visible_but_ineligible"
                ),
                "requires_registered_arithmetic_eligible_family": False,
                "unrelated_singleton_dsp_regression_is_veto": False,
            }
        else:
            family_values[target] = {}

    if valid:
        status = "available"
        counts = {name: value for name, (value, _maximum) in count_fields.items()}
        metaloop_summary = family_values
    else:
        status = "unknown"
        counts = {name: _UNKNOWN for name in count_fields}
        metaloop_summary = {
            target: {
                "hypothesis_count": _UNKNOWN,
                "complete_recovered_contract_count": _UNKNOWN,
                "missing_obligation_count": _UNKNOWN,
                "live_wall_covered_path_count": _UNKNOWN,
                "live_wall_overlap": _UNKNOWN,
                "nomination_eligible": _UNKNOWN,
                "nomination_blockers": _UNKNOWN,
                "attempt_status": _UNKNOWN,
                "requires_registered_arithmetic_eligible_family": False,
                "unrelated_singleton_dsp_regression_is_veto": False,
            }
            for target, _prefix, _incomplete, _zero in family_specs
        }
    cacc = metaloop_summary["constructive_accumulator_rebuild_v1"]
    memfwd = metaloop_summary["memory_write_buffer_forward_v1"]
    attempt_status = (
        _UNKNOWN
        if status == "unknown"
        else "eligible"
        if any(value >= 1 for value in counts.values())
        or cacc["nomination_eligible"] is True
        or memfwd["nomination_eligible"] is True
        else "unavailable"
    )
    return {
        "schema_version": 12,
        "authority": _OPERATOR_PREFLIGHT_AUTHORITY,
        "evidence_status": status,
        **counts,
        "constructive_accumulator_hypothesis_count": cacc["hypothesis_count"],
        "constructive_accumulator_eligible_family_count": (
            int(cacc["nomination_eligible"] is True)
            if status == "available" else _UNKNOWN
        ),
        "memory_write_buffer_forward_hypothesis_count": memfwd["hypothesis_count"],
        "memory_write_buffer_forward_eligible_family_count": (
            int(memfwd["nomination_eligible"] is True)
            if status == "available" else _UNKNOWN
        ),
        "metaloop_action_decision_summary": metaloop_summary,
        "registered_attempt_status": attempt_status,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "nomination_preflight_only",
    }


def validate_operator_action_preflight(value: Any) -> dict[str, Any] | None:
    """Validate the complete anonymous nomination-preflight schema."""
    report = _as_mapping(value)
    fields = {
        "schema_version",
        "authority",
        "evidence_status",
        "recurrence_boundary_hypothesis_count",
        "fixed_point_product_hypothesis_count",
        "two_operand_product_hypothesis_count",
        "register_absorb_hypothesis_count",
        "pipeline_rebalance_hypothesis_count",
        "registered_arithmetic_eligible_family_count",
        "control_to_data_state_equation_hypothesis_count",
        "constructive_accumulator_hypothesis_count",
        "constructive_accumulator_eligible_family_count",
        "memory_write_buffer_forward_hypothesis_count",
        "memory_write_buffer_forward_eligible_family_count",
        "metaloop_action_decision_summary",
        "registered_attempt_status",
        "read_only",
        "mutation_allowed",
        "proof_status",
    }
    if (
        set(report) != fields
        or report.get("schema_version") != 12
        or report.get("authority") != _OPERATOR_PREFLIGHT_AUTHORITY
        or report.get("evidence_status") not in {"available", "unknown"}
        or report.get("read_only") is not True
        or report.get("mutation_allowed") is not False
        or report.get("proof_status") != "nomination_preflight_only"
    ):
        return None
    if report["evidence_status"] == "unknown":
        expected = build_operator_action_preflight(None)
        return expected if dict(report) == expected else None
    summary = report.get("metaloop_action_decision_summary")
    if not isinstance(summary, Mapping):
        return None
    cacc = _as_mapping(summary.get("constructive_accumulator_rebuild_v1"))
    memfwd = _as_mapping(summary.get("memory_write_buffer_forward_v1"))
    source = {
        "recurrence_eligible_count": report.get(
            "recurrence_boundary_hypothesis_count"
        ),
        "fixed_point_product_hypothesis_count": report.get(
            "fixed_point_product_hypothesis_count"
        ),
        "two_operand_product_hypothesis_count": report.get(
            "two_operand_product_hypothesis_count"
        ),
        "register_absorb_hypothesis_count": report.get(
            "register_absorb_hypothesis_count"
        ),
        "pipeline_rebalance_hypothesis_count": report.get(
            "pipeline_rebalance_hypothesis_count"
        ),
        "registered_arithmetic_eligible_family_count": report.get(
            "registered_arithmetic_eligible_family_count"
        ),
        "control_to_data_state_equation_hypothesis_count": report.get(
            "control_to_data_state_equation_hypothesis_count"
        ),
        "constructive_accumulator_hypothesis_count": cacc.get(
            "hypothesis_count"
        ),
        "constructive_accumulator_complete_contract_count": cacc.get(
            "complete_recovered_contract_count"
        ),
        "constructive_accumulator_missing_obligation_count": cacc.get(
            "missing_obligation_count"
        ),
        "constructive_accumulator_live_wall_covered_path_count": cacc.get(
            "live_wall_covered_path_count"
        ),
        "constructive_accumulator_nomination_blockers": cacc.get(
            "nomination_blockers"
        ),
        "memory_write_buffer_forward_hypothesis_count": memfwd.get(
            "hypothesis_count"
        ),
        "memory_write_buffer_forward_complete_contract_count": memfwd.get(
            "complete_recovered_contract_count"
        ),
        "memory_write_buffer_forward_missing_obligation_count": memfwd.get(
            "missing_obligation_count"
        ),
        "memory_write_buffer_forward_live_wall_covered_path_count": memfwd.get(
            "live_wall_covered_path_count"
        ),
        "memory_write_buffer_forward_nomination_blockers": memfwd.get(
            "nomination_blockers"
        ),
    }
    expected = build_operator_action_preflight(source)
    return expected if dict(report) == expected else None


def build_operator_direction_visibility_packet(
    operator_preflight: Any,
    registered_arithmetic_packet: Any,
    exhausted_actions: Any = None,
) -> dict[str, Any]:
    """Keep current-seed registered direction status visible across failures."""
    preflight = validate_operator_action_preflight(operator_preflight)
    exhausted = {
        (str(item.get("skill") or ""), str(item.get("target") or ""))
        for item in (exhausted_actions if isinstance(exhausted_actions, list) else [])
        if isinstance(item, Mapping)
        and item.get("disposition") in {"measured", "neutral_replan"}
    }
    registered_packet = (
        registered_arithmetic_packet
        if isinstance(registered_arithmetic_packet, Mapping)
        else {}
    )
    registered_summary = _as_mapping(
        registered_packet.get("action_decision_summary")
    )
    family_status = {
        str(item.get("family_kind")): item
        for item in registered_summary.get("direction_status", [])
        if isinstance(item, Mapping) and isinstance(item.get("family_kind"), str)
    }
    product_sum_status = _as_mapping(family_status.get("product_sum"))
    fixed_product_status = _as_mapping(family_status.get("fixed_product"))
    recurrence_status = _as_mapping(family_status.get("recurrence_transport"))
    rows = []
    for target, count_field in _DIRECTION_COUNT_FIELDS.items():
        count = preflight.get(count_field, 0) if preflight is not None else 0
        eligible = bool(isinstance(count, int) and not isinstance(count, bool) and count > 0)
        blockers: list[str] = []
        evidence_complete = preflight is not None
        live_authorized = False
        if target in {
            "constructive_accumulator_rebuild_v1",
            "memory_write_buffer_forward_v1",
        }:
            summary = _as_mapping(
                preflight.get("metaloop_action_decision_summary", {}).get(target)
                if preflight is not None else None
            )
            eligible = eligible and summary.get("nomination_eligible") is True
            blockers = [
                str(item) for item in summary.get("nomination_blockers", [])
                if isinstance(item, str) and item
            ]
            evidence_complete = evidence_complete and all(
                field in summary for field in (
                    "complete_recovered_contract_count",
                    "missing_obligation_count",
                    "live_wall_covered_path_count",
                    "nomination_eligible",
                    "nomination_blockers",
                )
            )
            live_authorized = eligible
        elif target == "registered_arithmetic_cone_v1":
            packet_eligible = (
                registered_packet.get("nomination_status") == "eligible"
                and int(registered_packet.get("eligible_family_count", 0) or 0) > 0
                and product_sum_status.get("status") == "eligible"
            )
            eligible = eligible and packet_eligible
            blockers = [
                str(item)
                for item in product_sum_status.get("deferral_reason_codes", [])
                if isinstance(item, str) and item
            ]
            evidence_complete = evidence_complete and packet_eligible
            live_authorized = eligible
            if not packet_eligible and not blockers:
                blockers = ["REGISTERED_ARITHMETIC_PACKET_UNAVAILABLE"]
        elif target == "fixed_point_dsp_rebuild_v1":
            packet_eligible = (
                fixed_product_status.get("status") == "eligible"
                and int(fixed_product_status.get("eligible_family_count", 0) or 0) > 0
            )
            eligible = eligible and packet_eligible
            blockers = [
                str(item)
                for item in fixed_product_status.get("deferral_reason_codes", [])
                if isinstance(item, str) and item
            ]
            evidence_complete = evidence_complete and bool(fixed_product_status)
            live_authorized = eligible
            if not packet_eligible and not blockers:
                blockers = ["FIXED_PRODUCT_LIVE_AUTHORIZATION_UNAVAILABLE"]
        elif target == "dsp_srl_recurrence_cut_v1" and recurrence_status:
            packet_eligible = (
                recurrence_status.get("status") == "eligible"
                and int(recurrence_status.get("eligible_family_count", 0) or 0) > 0
            )
            eligible = eligible and packet_eligible
            blockers = [
                str(item)
                for item in recurrence_status.get("deferral_reason_codes", [])
                if isinstance(item, str) and item
            ]
            evidence_complete = evidence_complete and bool(recurrence_status)
            live_authorized = eligible
        if not eligible and not blockers:
            blockers = ["NO_CURRENT_SEED_ELIGIBLE_FAMILY"]
        exhausted_here = ("operator_rewrite", target) in exhausted
        status = "exhausted" if eligible and exhausted_here else (
            "eligible" if eligible else "ineligible"
        )
        rows.append({
            "skill": "operator_rewrite",
            "target": target,
            "status": status,
            "current_seed_eligible": eligible,
            "authorization_state": (
                "live_authorized_executable_now"
                if live_authorized
                else "hypothesis_only"
                if isinstance(count, int) and not isinstance(count, bool) and count > 0
                else "blocked"
            ),
            "evidence_complete": bool(evidence_complete),
            "nomination_blockers": [] if eligible else blockers,
        })
    rows.sort(key=lambda item: (
        {"eligible": 0, "exhausted": 1, "ineligible": 2}[item["status"]],
        item["authorization_state"] != "live_authorized_executable_now",
        item["target"],
    ))
    remaining = sum(item["status"] == "eligible" for item in rows)
    return {
        "schema_version": 1,
        "authority": "tool_owned_current_seed_direction_visibility",
        "direction_status": rows,
        "remaining_eligible_direction_count": remaining,
        "no_supported_mutation_permitted": remaining == 0,
        "mutation_allowed": False,
    }


def build_semantic_action_preflight(
    semantic_evidence: Any,
    *,
    recurrence_lineage_active: Any,
) -> dict[str, Any]:
    """Project exact-incumbent route eligibility and private lineage state."""
    evidence = _as_mapping(semantic_evidence)
    count = evidence.get("worst_source_eligible_count")
    if isinstance(count, bool) or not isinstance(count, int) or count not in {0, 1}:
        status = "unknown"
        count = _UNKNOWN
    else:
        status = "available"
    lineage = (
        recurrence_lineage_active
        if isinstance(recurrence_lineage_active, bool)
        else _UNKNOWN
    )
    attempt_status = (
        _UNKNOWN
        if status == "unknown" or lineage == _UNKNOWN
        else "eligible" if count == 1 and lineage is True else "unavailable"
    )
    return {
        "schema_version": 3,
        "authority": _SEMANTIC_PREFLIGHT_AUTHORITY,
        "evidence_status": status,
        "worst_source_eligible_count": count,
        "recurrence_lineage_active": lineage,
        "registered_attempt_status": attempt_status,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "selection_preflight_only",
    }


def validate_semantic_action_preflight(value: Any) -> dict[str, Any] | None:
    """Validate the complete anonymous lineage-safe route preflight schema."""
    report = _as_mapping(value)
    fields = {
        "schema_version",
        "authority",
        "evidence_status",
        "worst_source_eligible_count",
        "recurrence_lineage_active",
        "registered_attempt_status",
        "read_only",
        "mutation_allowed",
        "proof_status",
    }
    if (
        set(report) != fields
        or report.get("schema_version") != 3
        or report.get("authority") != _SEMANTIC_PREFLIGHT_AUTHORITY
        or report.get("evidence_status") not in {"available", "unknown"}
        or report.get("read_only") is not True
        or report.get("mutation_allowed") is not False
        or report.get("proof_status") != "selection_preflight_only"
    ):
        return None
    count = report.get("worst_source_eligible_count")
    if report["evidence_status"] == "available":
        if isinstance(count, bool) or not isinstance(count, int) or count not in {0, 1}:
            return None
    elif count != _UNKNOWN:
        return None
    lineage = report.get("recurrence_lineage_active")
    if not isinstance(lineage, bool) and lineage != _UNKNOWN:
        return None
    expected_attempt_status = (
        _UNKNOWN
        if report["evidence_status"] == "unknown" or lineage == _UNKNOWN
        else "eligible" if count == 1 and lineage is True else "unavailable"
    )
    if report.get("registered_attempt_status") != expected_attempt_status:
        return None
    return {
        "schema_version": 3,
        "authority": _SEMANTIC_PREFLIGHT_AUTHORITY,
        "evidence_status": report["evidence_status"],
        "worst_source_eligible_count": count,
        "recurrence_lineage_active": lineage,
        "registered_attempt_status": expected_attempt_status,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "selection_preflight_only",
    }


def build_operator_extraction_profile(analysis: Any) -> dict[str, Any]:
    """Build schema-v2 neutral evidence from the current live analysis schema.

    Missing or malformed scalar evidence is represented by ``"unknown"``.
    Collections retain only explicitly selected counts, finite measurements,
    booleans, controlled family buckets, and aliases allocated afresh for this
    call.  In particular, no arbitrary input key or identifier is copied.
    """
    analysis_map = _as_mapping(analysis)
    timing_summary = _as_mapping(analysis_map.get("timing_summary"))
    timing_profile = _as_mapping(analysis_map.get("timing_profile"))
    utilization = _as_mapping(analysis_map.get("utilization"))
    congestion = _as_mapping(analysis_map.get("congestion"))
    replica_population = _as_mapping(analysis_map.get("replica_population"))
    carry_clouds = _as_mapping(analysis_map.get("carry_clouds"))
    hard_macro_cascades = _as_mapping(
        analysis_map.get("hard_macro_cascades")
    )

    path_samples = _mapping_records(timing_profile.get("path_samples"))
    top_sources = _mapping_records(timing_profile.get("top_sources"))
    top_destinations = _mapping_records(timing_profile.get("top_destinations"))
    top_nets = _mapping_records(timing_profile.get("top_nets"))
    fanout_records, fanout_collection_available = _read_fanout_records(
        analysis_map.get("fanout_nets")
    )

    source_aliases = _ordinal_aliases(
        [item.get("source") for item in path_samples]
        + [item.get("name") for item in top_sources],
        "source",
    )
    destination_aliases = _ordinal_aliases(
        [item.get("destination") for item in path_samples]
        + [item.get("name") for item in top_destinations],
        "destination",
    )
    net_aliases = _ordinal_aliases(
        [item.get("top_net") for item in path_samples]
        + [item.get("name") for item in top_nets]
        + [item.get("name") for item in fanout_records],
        "net",
    )

    returned_path_count = _first_count(
        timing_profile,
        "returned_path_count",
        "path_count",
    )
    path_count = _first_count(
        timing_profile,
        "path_count",
        "returned_path_count",
    )
    requested_path_count = _count(timing_profile.get("requested_path_count"))
    sample_may_be_censored = _timing_censoring(
        timing_profile.get("sample_may_be_censored"),
        requested_path_count,
        returned_path_count,
        path_count,
    )

    (
        slack_vector,
        indexed_slacks,
        raw_slack_count,
        unknown_slack_count,
        slack_vector_complete,
        slack_vector_was_provided,
    ) = _slack_evidence(timing_profile, path_samples, returned_path_count)

    histogram_sequence = _sequence_or_none(
        timing_profile.get("path_logic_histograms")
    )
    histogram_sequence_was_provided = histogram_sequence is not None
    if histogram_sequence is None:
        histogram_sequence = [item.get("logic_histogram") for item in path_samples]

    observation_count = max(
        len(indexed_slacks),
        len(histogram_sequence),
        len(path_samples),
    )
    path_observations = []
    path_histogram_counts: list[tuple[dict[str, int], bool, bool | str]] = []
    for index in range(observation_count):
        sample = path_samples[index] if index < len(path_samples) else {}
        histogram_value: Any = None
        if index < len(histogram_sequence):
            histogram_value = histogram_sequence[index]
        elif not histogram_sequence_was_provided:
            histogram_value = sample.get("logic_histogram")
        family_counts, counts_available, counts_complete = _family_counts(
            histogram_value,
            _logic_family,
        )
        path_histogram_counts.append(
            (family_counts, counts_available, counts_complete)
        )
        path_observations.append({
            "path_alias": _ordinal_alias("path", index),
            "slack_ns": (
                indexed_slacks[index]
                if index < len(indexed_slacks)
                else _UNKNOWN
            ),
            "source_alias": _lookup_alias(
                source_aliases,
                sample.get("source"),
            ),
            "destination_alias": _lookup_alias(
                destination_aliases,
                sample.get("destination"),
            ),
            "top_net_alias": _lookup_alias(
                net_aliases,
                sample.get("top_net"),
            ),
            "route_pct": _continuous(sample.get("route_pct")),
            "logic_levels": _count(sample.get("logic_levels")),
            "span_x": _count(sample.get("span_x")),
            "span_y": _count(sample.get("span_y")),
            "cross_type_span_unavailable": _boolean(
                sample.get("cross_type_span_unavailable")
            ),
            "logic_family_counts": family_counts,
            "logic_family_counts_available": counts_available,
            "logic_family_counts_complete": counts_complete,
        })

    aggregate_histogram = timing_profile.get("logic_histogram")
    (
        logic_family_counts,
        logic_family_counts_available,
        logic_family_counts_complete,
    ) = _family_counts(aggregate_histogram, _logic_family)
    if not logic_family_counts_available:
        derived_counts: dict[str, int] = {}
        derived_complete: bool | str = True
        saw_path_histogram = False
        for counts, available, complete in path_histogram_counts:
            if not available:
                continue
            saw_path_histogram = True
            for family, count in counts.items():
                derived_counts[family] = derived_counts.get(family, 0) + count
            if complete is not True:
                derived_complete = False
        if saw_path_histogram:
            logic_family_counts = dict(sorted(derived_counts.items()))
            logic_family_counts_available = True
            logic_family_counts_complete = derived_complete

    top_source_evidence = [
        {
            "source_alias": _lookup_alias(source_aliases, item.get("name")),
            "path_count": _count(item.get("path_count")),
        }
        for item in top_sources
    ]
    top_source_evidence.sort(key=lambda item: _alias_sort_key(item["source_alias"]))

    top_destination_evidence = [
        {
            "destination_alias": _lookup_alias(
                destination_aliases,
                item.get("name"),
            ),
            "path_count": _count(item.get("path_count")),
        }
        for item in top_destinations
    ]
    top_destination_evidence.sort(
        key=lambda item: _alias_sort_key(item["destination_alias"])
    )

    top_net_evidence = [
        {
            "net_alias": _lookup_alias(net_aliases, item.get("name")),
            "fanout_count": _count(item.get("fanout")),
            "path_count": _count(item.get("path_count")),
            "occurrence_count": _count(item.get("occurrences")),
            "total_delay_ns": _continuous(item.get("total_delay_ns")),
            "max_delay_ns": _continuous(item.get("max_delay_ns")),
        }
        for item in top_nets
    ]
    top_net_evidence.sort(key=lambda item: _alias_sort_key(item["net_alias"]))

    fanout_evidence = [
        {
            "net_alias": _lookup_alias(net_aliases, item.get("name")),
            "fanout_count": _count(item.get("fanout")),
            "critical_path_count": _count(item.get("path_count")),
        }
        for item in fanout_records
    ]
    fanout_evidence.sort(key=lambda item: _alias_sort_key(item["net_alias"]))

    placement_spread = _placement_spread_summary(
        timing_profile.get("placement_spread")
    )
    timing_profile_available = _has_any_key(
        timing_profile,
        (
            "path_count",
            "returned_path_count",
            "requested_path_count",
            "slack_vector_ns",
            "worst_slack_ns",
            "avg_route_pct",
            "logic_histogram",
            "path_samples",
        ),
    )

    return {
        "schema_version": 2,
        "authority": _AUTHORITY,
        "live_telemetry_complete": _boolean(
            analysis_map.get("_telemetry_complete")
        ),
        "baseline_wns": _continuous(analysis_map.get("baseline_wns")),
        "clock_period": _continuous(analysis_map.get("clock_period")),
        "timing_summary": {
            "evidence_available": _has_any_key(
                timing_summary,
                ("wns", "tns", "whs", "wpws", "failing_endpoints"),
            ),
            "wns": _first_continuous(
                timing_summary,
                ("wns",),
                fallback=analysis_map.get("baseline_wns"),
            ),
            "tns": _continuous(timing_summary.get("tns")),
            "whs": _continuous(timing_summary.get("whs")),
            "wpws": _continuous(timing_summary.get("wpws")),
            "failing_endpoint_count": _count(
                timing_summary.get("failing_endpoints")
            ),
        },
        "timing_profile": {
            "evidence_available": timing_profile_available,
            "path_count": path_count,
            "requested_path_count": requested_path_count,
            "returned_path_count": returned_path_count,
            "sample_may_be_censored": sample_may_be_censored,
            "slack_vector_ns": slack_vector,
            "reported_slack_value_count": raw_slack_count,
            "finite_slack_value_count": len(slack_vector),
            "unknown_slack_value_count": unknown_slack_count,
            "slack_vector_complete": slack_vector_complete,
            "slack_vector_was_provided": slack_vector_was_provided,
            "worst_slack_ns": _continuous(timing_profile.get("worst_slack_ns")),
            "avg_slack_ns": _continuous(timing_profile.get("avg_slack_ns")),
            "avg_route_pct": _continuous(timing_profile.get("avg_route_pct")),
            "avg_logic_pct": _continuous(timing_profile.get("avg_logic_pct")),
            "logic_levels": {
                "avg": _continuous(
                    _as_mapping(timing_profile.get("logic_levels")).get("avg")
                ),
                "max": _count(
                    _as_mapping(timing_profile.get("logic_levels")).get("max")
                ),
            },
            "logic_family_counts": logic_family_counts,
            "logic_family_counts_available": logic_family_counts_available,
            "logic_family_counts_complete": logic_family_counts_complete,
            "top_sources": top_source_evidence,
            "top_destinations": top_destination_evidence,
            "top_nets": top_net_evidence,
            "path_observations": path_observations,
            "placement_spread": placement_spread,
        },
        "fanout_nets": {
            "evidence_available": fanout_collection_available,
            "returned_net_count": len(fanout_evidence),
            # The current telemetry does not retain the query's requested count.
            "sample_may_be_censored": _UNKNOWN,
            "observations": fanout_evidence,
        },
        "utilization": _utilization_summary(utilization),
        "congestion": _congestion_summary(congestion),
        "replica_population": {
            "evidence_available": "approximate_name_marker_count"
            in replica_population,
            "approximate_name_marker_count": _count(
                replica_population.get("approximate_name_marker_count")
            ),
        },
        "carry_clouds": _carry_summary(carry_clouds),
        "hard_macro_cascades": _hard_macro_summary(hard_macro_cascades),
    }


def validate_operator_mining_root(
    value: Any,
    *,
    expected_design_sha256: str | None = None,
) -> Mapping[str, Any] | None:
    """Validate the authority-bearing private envelope without exposing it."""
    root = _as_mapping(value)
    design_sha256 = root.get("design_sha256")
    if (
        root.get("status") != "success"
        or root.get("version") != _RAW_OPERATOR_MINING_VERSION
        or root.get("authority") != _RAW_OPERATOR_MINING_AUTHORITY
        or root.get("read_only") is not True
        or root.get("mutation_allowed") is not False
        or root.get("proof_status") != "hypothesis_only"
        or not isinstance(design_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", design_sha256) is None
        or (
            expected_design_sha256 is not None
            and design_sha256 != expected_design_sha256
        )
    ):
        return None
    return root


def validate_recurrence_boundary_hypotheses(
    operator_mining_root: Any,
    *,
    expected_design_sha256: str,
    max_families: int = 16,
) -> tuple[Mapping[str, Any], ...] | None:
    """Validate private recurrence hypotheses before they influence selection."""
    root = validate_operator_mining_root(
        operator_mining_root,
        expected_design_sha256=expected_design_sha256,
    )
    if root is None or isinstance(max_families, bool) or not 1 <= max_families <= 32:
        return None
    raw_families = root.get("recurrence_boundary_families")
    if not isinstance(raw_families, list) or len(raw_families) > max_families:
        return None
    fields = {
        "candidate_id",
        "kind",
        "source_register_stem",
        "source_register_count",
        "source_columns",
        "sink_dsp",
        "critical_path_count",
        "transition_signature",
        "member_examples",
        "authority",
        "read_only",
        "mutation_allowed",
        "proof_status",
    }
    validated = []
    for family in raw_families:
        if not isinstance(family, Mapping) or set(family) != fields:
            return None
        source_count = family.get("source_register_count")
        path_count = family.get("critical_path_count")
        columns = family.get("source_columns")
        transition = family.get("transition_signature")
        examples = family.get("member_examples")
        if (
            re.fullmatch(
                r"recurrence:[0-9a-f]{20}", str(family.get("candidate_id") or "")
            ) is None
            or family.get("kind") != "recurrence_boundary_hypothesis"
            or family.get("authority") != _RAW_OPERATOR_MINING_AUTHORITY
            or family.get("read_only") is not True
            or family.get("mutation_allowed") is not False
            or family.get("proof_status") != "hypothesis_only"
            or not isinstance(family.get("source_register_stem"), str)
            or not 1 <= len(family["source_register_stem"]) <= 1024
            or not isinstance(family.get("sink_dsp"), str)
            or not 1 <= len(family["sink_dsp"]) <= 1024
            or isinstance(source_count, bool)
            or not isinstance(source_count, int)
            or not 2 <= source_count <= 1_000_000
            or isinstance(path_count, bool)
            or not isinstance(path_count, int)
            or not 1 <= path_count <= 16
            or not isinstance(columns, list)
            or len(columns) > 32
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or not -1_000_000 <= item <= 1_000_000
                for item in columns
            )
            or not isinstance(transition, list)
            or not 2 <= len(transition) <= 4
            or any(
                not isinstance(item, str) or not 1 <= len(item) <= 128
                for item in transition
            )
            or not isinstance(examples, list)
            or not 1 <= len(examples) <= 2
            or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or any(
                    not isinstance(item, str) or not 1 <= len(item) <= 1024
                    for item in pair
                )
                for pair in examples
            )
        ):
            return None
        validated.append(family)
    return tuple(validated)


def _normalized_sequence_edit_distance(
    left: tuple[str, ...], right: tuple[str, ...]
) -> float:
    if left == right:
        return 0.0
    scale = max(len(left), len(right))
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, 1):
        current = [left_index]
        for right_index, right_item in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1]
                + (0 if left_item == right_item else 1),
            ))
        previous = current
    return previous[-1] / scale


def _canonical_public_motif_families(
    population: Mapping[tuple[str, ...], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    resolved_count = sum(int(item["sequence_count"]) for item in population.values())
    families = []
    for sequence, facts in population.items():
        sequence_count = int(facts["sequence_count"])
        weighted_distances = []
        if sequence_count > 1:
            weighted_distances.append((0.0, sequence_count - 1))
        for peer, peer_facts in population.items():
            if peer == sequence:
                continue
            weighted_distances.append((
                _normalized_sequence_edit_distance(sequence, peer),
                int(peer_facts["sequence_count"]),
            ))
        if weighted_distances:
            nearest_distance: float | str = round(
                min(distance for distance, weight in weighted_distances if weight > 0),
                6,
            )
            population_distance: float | str = round(
                sum(distance * weight for distance, weight in weighted_distances)
                / max(resolved_count - 1, 1),
                6,
            )
            nearest_similarity: float | str = round(1.0 - nearest_distance, 6)
            population_similarity: float | str = round(
                1.0 - population_distance, 6
            )
        else:
            nearest_distance = _UNKNOWN
            population_distance = _UNKNOWN
            nearest_similarity = _UNKNOWN
            population_similarity = _UNKNOWN
        carry_count = sum(item in {"carry4", "carry8"} for item in sequence)
        dsp_count = sum(item == "dsp" for item in sequence)
        lut_count = sum(
            item == "lut" or re.fullmatch(r"lut[1-6]", item) is not None
            for item in sequence
        )
        families.append({
            "primitive_family_sequence": list(sequence),
            "sequence_length": len(sequence),
            "sequence_count": sequence_count,
            "peer_sequence_count": max(resolved_count - 1, 0),
            "nearest_normalized_edit_distance": nearest_distance,
            "population_normalized_edit_distance": population_distance,
            "nearest_continuous_similarity": nearest_similarity,
            "population_continuous_similarity": population_similarity,
            "dedicated_arithmetic_primitive_count": carry_count + dsp_count,
            "carry_primitive_count": carry_count,
            "dsp_primitive_count": dsp_count,
            "lut_primitive_count": lut_count,
            "risk_flags": (
                ["clock_as_data_path"] if facts["clock_as_data_path"] else []
            ),
            "proof_status": "hypothesis_only",
        })
    families.sort(key=lambda item: (
        -item["sequence_count"],
        -item["sequence_length"],
        item["primitive_family_sequence"],
    ))
    return families


def build_repeated_arithmetic_motif_report(
    operator_mining_root: Any,
) -> dict[str, Any] | None:
    """Project only bounded, controlled motif evidence from a private RW root."""
    root = validate_operator_mining_root(operator_mining_root)
    if root is None:
        return None
    raw = _as_mapping(root.get("repeated_arithmetic_motif_evidence"))
    if (
        raw.get("version") != _RAW_MOTIF_VERSION
        or raw.get("authority") != _RAW_OPERATOR_MINING_AUTHORITY
        or raw.get("read_only") is not True
        or raw.get("mutation_allowed") is not False
        or raw.get("proof_status") != "hypothesis_only"
        or raw.get("similarity_metric") != _MOTIF_SIMILARITY_METRIC
    ):
        return None
    raw_families = raw.get("sequence_families")
    if not isinstance(raw_families, list) or len(raw_families) > 16:
        return None

    population: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw_family in raw_families:
        if not isinstance(raw_family, Mapping):
            return None
        raw_sequence = raw_family.get("primitive_sequence")
        if (
            not isinstance(raw_sequence, list)
            or not 1 <= len(raw_sequence) <= 64
            or any(
                not isinstance(item, str) or not 1 <= len(item) <= 128
                for item in raw_sequence
            )
            or raw_family.get("authority") != _RAW_OPERATOR_MINING_AUTHORITY
            or raw_family.get("read_only") is not True
            or raw_family.get("mutation_allowed") is not False
            or raw_family.get("proof_status") != "hypothesis_only"
        ):
            return None
        sequence = tuple(_logic_family(item) for item in raw_sequence)
        sequence_count = _bounded_count(
            raw_family.get("sequence_count"), maximum=16
        )
        if sequence_count is None or sequence_count < 1:
            return None
        if any(
            _bounded_ratio_or_unknown(raw_family.get(field)) is None
            for field in (
                "nearest_normalized_edit_distance",
                "population_normalized_edit_distance",
                "nearest_continuous_similarity",
                "population_continuous_similarity",
            )
        ):
            return None
        risk_flags = raw_family.get("risk_flags")
        if risk_flags not in ([], ["clock_as_data_path"]):
            return None
        merged = population.setdefault(sequence, {
            "sequence_count": 0,
            "clock_as_data_path": False,
        })
        merged["sequence_count"] += sequence_count
        merged["clock_as_data_path"] = bool(
            merged["clock_as_data_path"] or risk_flags
        )
    resolved_count = sum(item["sequence_count"] for item in population.values())
    if resolved_count > 16:
        return None
    families = _canonical_public_motif_families(population)
    return {
        "schema_version": 2,
        "authority": _MOTIF_AUTHORITY,
        "resolved_sequence_count": resolved_count,
        "unique_sequence_count": len(families),
        "similarity_metric": _MOTIF_SIMILARITY_METRIC,
        "sequence_families": families,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "hypothesis_only",
    }


def validate_repeated_arithmetic_motif_report(value: Any) -> dict | None:
    """Rebuild an already-public motif report across the corpus boundary."""
    report = _as_mapping(value)
    top_fields = {
        "schema_version",
        "authority",
        "resolved_sequence_count",
        "unique_sequence_count",
        "similarity_metric",
        "sequence_families",
        "read_only",
        "mutation_allowed",
        "proof_status",
    }
    if (
        set(report) != top_fields
        or report.get("schema_version") != 2
        or report.get("authority") != _MOTIF_AUTHORITY
        or report.get("similarity_metric") != _MOTIF_SIMILARITY_METRIC
        or report.get("read_only") is not True
        or report.get("mutation_allowed") is not False
        or report.get("proof_status") != "hypothesis_only"
        or isinstance(report.get("resolved_sequence_count"), bool)
        or not isinstance(report.get("resolved_sequence_count"), int)
        or isinstance(report.get("unique_sequence_count"), bool)
        or not isinstance(report.get("unique_sequence_count"), int)
    ):
        return None
    raw_families = report.get("sequence_families")
    if not isinstance(raw_families, list) or len(raw_families) > 16:
        return None
    family_fields = {
        "primitive_family_sequence",
        "sequence_length",
        "sequence_count",
        "peer_sequence_count",
        "nearest_normalized_edit_distance",
        "population_normalized_edit_distance",
        "nearest_continuous_similarity",
        "population_continuous_similarity",
        "dedicated_arithmetic_primitive_count",
        "carry_primitive_count",
        "dsp_primitive_count",
        "lut_primitive_count",
        "risk_flags",
        "proof_status",
    }
    population: dict[tuple[str, ...], dict[str, Any]] = {}
    submitted_by_sequence: dict[tuple[str, ...], dict[str, Any]] = {}
    for family in raw_families:
        if not isinstance(family, Mapping) or set(family) != family_fields:
            return None
        sequence = family.get("primitive_family_sequence")
        if (
            not isinstance(sequence, list)
            or not 1 <= len(sequence) <= 64
            or any(item not in _PUBLIC_LOGIC_FAMILIES for item in sequence)
        ):
            return None
        counts = {}
        for field in (
            "sequence_length",
            "sequence_count",
            "peer_sequence_count",
            "dedicated_arithmetic_primitive_count",
            "carry_primitive_count",
            "dsp_primitive_count",
            "lut_primitive_count",
        ):
            item = family.get(field)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return None
            counts[field] = item
        ratios = {}
        for field in (
            "nearest_normalized_edit_distance",
            "population_normalized_edit_distance",
            "nearest_continuous_similarity",
            "population_continuous_similarity",
        ):
            item = family.get(field)
            if item == _UNKNOWN:
                ratios[field] = _UNKNOWN
            elif (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                or not 0.0 <= float(item) <= 1.0
            ):
                return None
            else:
                ratios[field] = float(item)
        risk_flags = family.get("risk_flags")
        if risk_flags not in ([], ["clock_as_data_path"]):
            return None
        carry_count = sum(item in {"carry4", "carry8"} for item in sequence)
        dsp_count = sum(item == "dsp" for item in sequence)
        lut_count = sum(
            item == "lut" or re.fullmatch(r"lut[1-6]", item) is not None
            for item in sequence
        )
        if (
            counts["sequence_length"] != len(sequence)
            or counts["sequence_count"] < 1
            or counts["carry_primitive_count"] != carry_count
            or counts["dsp_primitive_count"] != dsp_count
            or counts["lut_primitive_count"] != lut_count
            or counts["dedicated_arithmetic_primitive_count"]
            != carry_count + dsp_count
            or family.get("proof_status") != "hypothesis_only"
        ):
            return None
        sequence_key = tuple(sequence)
        if sequence_key in population:
            return None
        submitted = {
            "primitive_family_sequence": list(sequence),
            "sequence_length": len(sequence),
            "sequence_count": counts["sequence_count"],
            "peer_sequence_count": counts["peer_sequence_count"],
            **ratios,
            "dedicated_arithmetic_primitive_count": carry_count + dsp_count,
            "carry_primitive_count": carry_count,
            "dsp_primitive_count": dsp_count,
            "lut_primitive_count": lut_count,
            "risk_flags": list(risk_flags),
            "proof_status": "hypothesis_only",
        }
        population[sequence_key] = {
            "sequence_count": counts["sequence_count"],
            "clock_as_data_path": bool(risk_flags),
        }
        submitted_by_sequence[sequence_key] = submitted
    rebuilt = _canonical_public_motif_families(population)
    resolved = sum(item["sequence_count"] for item in rebuilt)
    if (
        resolved > 16
        or report.get("resolved_sequence_count") != resolved
        or report.get("unique_sequence_count") != len(rebuilt)
        or any(
            submitted_by_sequence[tuple(item["primitive_family_sequence"])] != item
            for item in rebuilt
        )
    ):
        return None
    return {
        "schema_version": 2,
        "authority": _MOTIF_AUTHORITY,
        "resolved_sequence_count": resolved,
        "unique_sequence_count": len(rebuilt),
        "similarity_metric": _MOTIF_SIMILARITY_METRIC,
        "sequence_families": rebuilt,
        "read_only": True,
        "mutation_allowed": False,
        "proof_status": "hypothesis_only",
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _sequence_or_none(value: Any) -> list[Any] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return list(value)


def _continuous(value: Any) -> float | str:
    if value is None or isinstance(value, bool):
        return _UNKNOWN
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return _UNKNOWN
    return number if math.isfinite(number) else _UNKNOWN


def _count(value: Any) -> int | str:
    if value is None or isinstance(value, bool):
        return _UNKNOWN
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return _UNKNOWN
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return _UNKNOWN
    return int(number)


def _bounded_count(value: Any, *, maximum: int) -> int | None:
    count = _count(value)
    if count == _UNKNOWN or count > maximum:
        return None
    return count


def _bounded_ratio_or_unknown(value: Any) -> float | str | None:
    if value is None:
        return _UNKNOWN
    result = _continuous(value)
    if result == _UNKNOWN or not 0.0 <= result <= 1.0:
        return None
    return result


def _boolean(value: Any) -> bool | str:
    return value if isinstance(value, bool) else _UNKNOWN


def _first_count(source: Mapping[str, Any], *keys: str) -> int | str:
    for key in keys:
        if key in source:
            value = _count(source.get(key))
            if value != _UNKNOWN:
                return value
    return _UNKNOWN


def _first_continuous(
    source: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    fallback: Any = None,
) -> float | str:
    for key in keys:
        if key in source:
            value = _continuous(source.get(key))
            if value != _UNKNOWN:
                return value
    return _continuous(fallback)


def _has_any_key(source: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in source for key in keys)


def _raw_name(value: Any) -> str | None:
    return value if isinstance(value, str) and bool(value.strip()) else None


def _ordinal_aliases(values: list[Any], prefix: str) -> dict[str, str]:
    aliases = {}
    for value in values:
        name = _raw_name(value)
        if name is not None and name not in aliases:
            aliases[name] = _ordinal_alias(prefix, len(aliases))
    return aliases


def _ordinal_alias(prefix: str, index: int) -> str:
    return f"{prefix}_{index + 1:04d}"


def _lookup_alias(aliases: Mapping[str, str], value: Any) -> str:
    name = _raw_name(value)
    return aliases.get(name, _UNKNOWN) if name is not None else _UNKNOWN


def _alias_sort_key(alias: Any) -> tuple[bool, str]:
    return alias == _UNKNOWN, str(alias)


def _read_fanout_records(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, (list, tuple)):
        return [], False
    records = []
    for item in value:
        if isinstance(item, Mapping):
            records.append({
                "name": item.get("name"),
                "fanout": item.get("fanout"),
                "path_count": item.get("path_count", item.get("paths")),
            })
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            records.append({
                "name": item[0],
                "fanout": item[1],
                "path_count": item[2],
            })
    return records, True


def _timing_censoring(
    provided: Any,
    requested_path_count: int | str,
    returned_path_count: int | str,
    path_count: int | str,
) -> bool | str:
    signals = []
    provided_value = _boolean(provided)
    if provided_value != _UNKNOWN:
        signals.append(provided_value)
    if requested_path_count != _UNKNOWN and returned_path_count != _UNKNOWN:
        if requested_path_count == 0:
            signals.append(returned_path_count > 0)
        else:
            signals.append(returned_path_count >= requested_path_count)
    if returned_path_count != _UNKNOWN and path_count != _UNKNOWN:
        if returned_path_count != path_count:
            signals.append(True)
    if any(signal is True for signal in signals):
        return True
    if signals:
        return False
    return _UNKNOWN


def _slack_evidence(
    timing_profile: Mapping[str, Any],
    path_samples: list[Mapping[str, Any]],
    returned_path_count: int | str,
) -> tuple[list[float], list[float | str], int, int | str, bool | str, bool]:
    raw_vector = _sequence_or_none(timing_profile.get("slack_vector_ns"))
    vector_was_provided = raw_vector is not None
    if raw_vector is None:
        raw_vector = [sample.get("slack_ns") for sample in path_samples]

    finite_vector = []
    indexed_vector: list[float | str] = []
    for raw_value in raw_vector:
        value = _continuous(raw_value)
        indexed_vector.append(value)
        if value != _UNKNOWN:
            finite_vector.append(value)

    if returned_path_count == _UNKNOWN:
        unknown_count: int | str = _UNKNOWN
        complete: bool | str = _UNKNOWN
    else:
        unknown_count = max(returned_path_count - len(finite_vector), 0)
        complete = (
            vector_was_provided
            and len(raw_vector) == returned_path_count
            and len(finite_vector) == returned_path_count
        )
    return (
        finite_vector,
        indexed_vector,
        len(raw_vector),
        unknown_count,
        complete,
        vector_was_provided,
    )


def _family_counts(
    value: Any,
    classifier: Callable[[Any], str],
) -> tuple[dict[str, int], bool, bool | str]:
    if not isinstance(value, Mapping):
        return {}, False, _UNKNOWN
    result: dict[str, int] = {}
    complete = True
    for raw_family, raw_count in value.items():
        count = _count(raw_count)
        if count == _UNKNOWN:
            complete = False
            continue
        family = classifier(raw_family)
        result[family] = result.get(family, 0) + count
    return dict(sorted(result.items())), bool(value), complete


def _logic_family(value: Any) -> str:
    token = str(value).strip().upper() if isinstance(value, str) else ""
    if re.fullmatch(r"LUT[1-6]", token):
        return token.lower()
    if token == "LUT":
        return "lut"
    if token in {"CARRY4", "CARRY8"}:
        return token.lower()
    if token in {
        "DSP",
        "DSP_ALU",
        "DSP_A_B_DATA",
        "DSP_C_DATA",
        "DSP_M_DATA",
        "DSP_MULTIPLIER",
        "DSP_OUTPUT",
    } or re.fullmatch(r"DSP(?:48E[12]|58)", token):
        return "dsp"
    if token == "BRAM" or re.fullmatch(r"RAMB(?:18|36)(?:E[12])?", token):
        return "bram"
    if token in {"URAM", "URAM288"}:
        return "uram"
    if re.fullmatch(
        r"(?:RAM(?:16|32|64|128|256|512)[A-Z0-9_]*|RAM[SD][A-Z0-9_]*)",
        token,
    ):
        return "distributed_ram"
    if re.fullmatch(r"MUXF[5-9]", token):
        return "mux"
    if re.fullmatch(r"(?:FD|LD)[A-Z0-9_]*", token):
        return "register_or_latch"
    if re.fullmatch(r"SRL[A-Z0-9_]*", token):
        return "shift_register"
    return _UNKNOWN


def _site_family(value: Any) -> str:
    token = str(value).strip().upper() if isinstance(value, str) else ""
    if token == "SLICE":
        return "slice"
    if re.fullmatch(r"DSP(?:48E[12]|58)", token):
        return "dsp"
    if re.fullmatch(r"RAMB(?:18|36)(?:E[12])?", token):
        return "bram"
    if token == "URAM288":
        return "uram"
    return _UNKNOWN


def _macro_family(value: Any) -> str:
    token = str(value).strip().upper() if isinstance(value, str) else ""
    if token == "DSP":
        return "dsp"
    if token == "BRAM":
        return "bram"
    if token == "URAM":
        return "uram"
    return _UNKNOWN


def _placement_spread_summary(value: Any) -> dict[str, Any]:
    spread = _as_mapping(value)
    family_counts, counts_available, counts_complete = _family_counts(
        spread.get("site_type_counts"),
        _site_family,
    )
    return {
        "evidence_available": _has_any_key(
            spread,
            (
                "path_count_with_sites",
                "site_type_counts",
                "bbox_span_x",
                "bbox_span_y",
                "avg_path_span_x",
                "avg_path_span_y",
                "max_path_span_x",
                "max_path_span_y",
            ),
        ),
        "path_count_with_sites": _count(spread.get("path_count_with_sites")),
        "site_family_counts": family_counts,
        "site_family_counts_available": counts_available,
        "site_family_counts_complete": counts_complete,
        "bbox_span_x": _count(spread.get("bbox_span_x")),
        "bbox_span_y": _count(spread.get("bbox_span_y")),
        "cross_type_span_unavailable": _boolean(
            spread.get("cross_type_span_unavailable")
        ),
        "avg_path_span_x": _continuous(spread.get("avg_path_span_x")),
        "avg_path_span_y": _continuous(spread.get("avg_path_span_y")),
        "max_path_span_x": _count(spread.get("max_path_span_x")),
        "max_path_span_y": _count(spread.get("max_path_span_y")),
    }


def _utilization_summary(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_available": _has_any_key(
            source,
            (
                "luts",
                "clb_luts",
                "lut",
                "ffs",
                "ff",
                "registers",
                "dsp",
                "dsps",
                "bram",
                "brams",
                "uram",
                "urams",
                "lut_util_pct",
                "clb_lut_util_pct",
                "lut_utilization",
            ),
        ),
        "luts": _first_count(source, "luts", "clb_luts", "lut"),
        "ffs": _first_count(source, "ffs", "ff", "registers"),
        "dsp": _first_count(source, "dsp", "dsps"),
        "bram": _first_count(source, "bram", "brams"),
        "uram": _first_count(source, "uram", "urams"),
        "lut_util_pct": _first_continuous(
            source,
            ("lut_util_pct", "clb_lut_util_pct", "lut_utilization"),
        ),
    }


def _congestion_summary(source: Mapping[str, Any]) -> dict[str, Any]:
    placer = _as_mapping(source.get("placer"))
    router = _as_mapping(source.get("router"))
    return {
        "evidence_available": bool(placer or router),
        "placer": _congestion_bucket(placer),
        "router": _congestion_bucket(router),
    }


def _congestion_bucket(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_available": _has_any_key(
            source,
            ("window_count", "max_level", "no_windows_above_level"),
        ),
        "window_count": _count(source.get("window_count")),
        "max_level": _count(source.get("max_level")),
        "no_windows_above_level": _count(
            source.get("no_windows_above_level")
        ),
    }


def _structure_censoring(
    source: Mapping[str, Any],
    total_count: int | str,
    analyzed_count: int | str,
    *,
    extra_flag_keys: tuple[str, ...],
) -> bool | str:
    signals = []
    truncated = _boolean(source.get("truncated"))
    if truncated != _UNKNOWN:
        signals.append(truncated)
    for key in extra_flag_keys:
        flag = _boolean(source.get(key))
        if flag != _UNKNOWN:
            signals.append(flag)
    if total_count != _UNKNOWN and analyzed_count != _UNKNOWN:
        signals.append(analyzed_count < total_count)
    if source.get("status") not in (None, "success"):
        signals.append(True)
    parse_errors = source.get("parse_errors")
    if isinstance(parse_errors, (list, tuple)) and parse_errors:
        signals.append(True)
    structures = source.get("structures")
    if isinstance(structures, (list, tuple)):
        for structure in structures:
            if isinstance(structure, Mapping) and structure.get("members_truncated") is True:
                signals.append(True)
                break
    if any(signal is True for signal in signals):
        return True
    if signals:
        return False
    return _UNKNOWN


def _carry_summary(source: Mapping[str, Any]) -> dict[str, Any]:
    total_count = _count(source.get("carry_cell_count"))
    analyzed_count = _count(source.get("analyzed_cell_count"))
    cloud = _as_mapping(source.get("cloud_summary"))
    structures = source.get("structures")
    structure_count: int | str = (
        len(structures) if isinstance(structures, (list, tuple)) else _UNKNOWN
    )
    return {
        "evidence_available": _has_any_key(
            source,
            (
                "carry_cell_count",
                "analyzed_cell_count",
                "chain_count",
                "max_chain_length",
                "cloud_summary",
                "structures",
            ),
        ),
        "carry_cell_count": total_count,
        "analyzed_cell_count": analyzed_count,
        "structure_count": structure_count,
        "chain_count": _count(source.get("chain_count")),
        "max_chain_length": _count(source.get("max_chain_length")),
        "driver_family_counts": {
            "lut": _count(cloud.get("lut_driver_count")),
            "register_or_latch": _count(
                cloud.get("ff_or_latch_driver_count")
            ),
            "other": _count(cloud.get("other_driver_count")),
        },
        "sample_may_be_censored": _structure_censoring(
            source,
            total_count,
            analyzed_count,
            extra_flag_keys=("carry_edges_truncated",),
        ),
    }


def _hard_macro_summary(source: Mapping[str, Any]) -> dict[str, Any]:
    total_count = _count(source.get("hard_macro_count"))
    analyzed_count = _count(source.get("analyzed_macro_count"))
    family_counts, counts_available, counts_complete = _family_counts(
        source.get("family_counts"),
        _macro_family,
    )
    structures = source.get("structures")
    structure_count: int | str = (
        len(structures) if isinstance(structures, (list, tuple)) else _UNKNOWN
    )
    return {
        "evidence_available": _has_any_key(
            source,
            (
                "hard_macro_count",
                "analyzed_macro_count",
                "family_counts",
                "cascade_count",
                "max_cascade_length",
                "structures",
            ),
        ),
        "hard_macro_count": total_count,
        "analyzed_macro_count": analyzed_count,
        "structure_count": structure_count,
        "family_counts": family_counts,
        "family_counts_available": counts_available,
        "family_counts_complete": counts_complete,
        "cascade_count": _count(source.get("cascade_count")),
        "max_cascade_length": _count(source.get("max_cascade_length")),
        "sample_may_be_censored": _structure_censoring(
            source,
            total_count,
            analyzed_count,
            extra_flag_keys=("cascade_edges_truncated",),
        ),
    }
