"""
custom skill — runtime escape hatch for direct MCP tool invocation.

The legality gate permits only exact named read-only tools from the configured
Vivado and RapidWright allowlists. The LLM supplies arguments inline, and Python
returns a truncated response for the next LLM round to reason about. Raw Tcl,
checkpoint I/O, optimization, and other mutation tools are not probe-capable.

Usage (from an LLM probe decision):

    {
      "recommended_action": {
        "skill": "custom",
        "target": "vivado:report_timing_summary",
        "params": {
          "arguments": {},
          "description": "Refresh timing summary telemetry",
          "measure_timing": false
        }
      }
    }

target is ALWAYS "server:tool_name" where server is "vivado" or "rapidwright".
"""

import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

from ..probe_handoff import (
    MissingProbeHandoffError,
    ProbeArgumentContractError,
    capture_probe_response,
    ordered_sequential_family_screen_refs,
    prepare_probe_arguments,
    resolve_sequential_family_handoff,
)
from ..consumer_checkpoint import (
    load_consumer_rewrite_checkpoint,
)
from .base import SkillResult, calculate_fmax

logger = logging.getLogger(__name__)


def _framework_owned_probe_timeout(params: dict) -> float:
    """Use the remaining outer run window instead of a fixed probe cutoff."""
    deadline = params.get("_execution_deadline_monotonic")
    if (
        isinstance(deadline, (int, float))
        and not isinstance(deadline, bool)
        and math.isfinite(float(deadline))
    ):
        return max(1.0, float(deadline) - time.monotonic())
    return 600.0


# Response truncation. We feed the LLM enough of the tool output to actually
# reason about real Vivado/RapidWright reports — a single report_timing path
# is 2-4 KB, and the LLM may ask for -max_paths 10 (20-40 KB). Too-tight a
# limit makes the probe useless: in fdagents_run-20260414_205737 the LLM
# issued three report_timing calls but only saw the header and trailing
# options because the limit was 4 KB.
#
# This is just the CustomSkill's own cap. memory.py imposes a second cap
# when embedding the response into the next deep-mode state (see
# compact_state_for_llm) — keep that one slightly smaller for token safety.
MAX_RESPONSE_CHARS = 11000


def _is_unsupported_consumer_contract_error(exc: Exception) -> bool:
    """Distinguish a proved unsupported live boundary from tool failures."""
    return (
        "no consumer boundary matches a supported registered arithmetic contract"
        in str(exc)
    )


def _is_inconclusive_fixed_point_screen_error(exc: Exception) -> bool:
    """Separate tool/timeout failures from source-bound semantic rejection.

    A missing completion sentinel is not a semantic fact unless the MCP also
    surfaced the terminal Tcl diagnostic. Closure timeouts must remain
    retryable and must never enter the exact-seed negative cache.
    """
    lowered = str(exc or "").lower()
    if "completion_diagnostic=" in lowered:
        diagnostic = lowered.split("completion_diagnostic=", 1)[1]
        diagnostic = diagnostic.split("; vivado_output=", 1)[0].strip()
        if diagnostic and diagnostic != "no vivado completion output":
            return "timeout" in diagnostic
    return any(
        marker in lowered
        for marker in (
            "timeout exceeded",
            "timed out",
            "did not complete",
            "connection closed",
            "broken pipe",
            "mcp server exited",
            "process exited",
        )
    )


def _unsupported_consumer_diagnostics(exc: Exception) -> dict:
    """Recover the anonymous structural census attached to a negative screen."""
    marker = "anonymous_diagnostics="
    message = str(exc)
    if marker not in message:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(message.split(marker, 1)[1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    profiles = []
    for raw in (value.get("owner_boundary_profiles") or [])[:4]:
        if not isinstance(raw, dict):
            continue
        profiles.append({
            "distance": raw.get("distance"),
            "input_group_count": raw.get("input_group_count"),
            "input_widths": list(raw.get("input_widths") or [])[:32],
            "output_group_count": raw.get("output_group_count"),
            "output_widths": list(raw.get("output_widths") or [])[:32],
        })
    recurrence = value.get("selected_owner_recurrence") or {}
    return {
        "eligible_even_input_count": value.get("eligible_even_input_count"),
        "owner_boundary_profiles": profiles,
        "selected_owner_recurrence": {
            "indexed_register_family_count": recurrence.get(
                "indexed_register_family_count"
            ),
            "multi_stage_series_profiles": list(
                recurrence.get("multi_stage_series_profiles") or []
            )[:8],
        },
    }


_UNSUPPORTED_COMMUTATIVE_CONTRACT_ERRORS = {
    "no registered LUT/CARRY reduction found on live paths": (
        "no_live_registered_lut_carry_reduction"
    ),
    "selected reduction endpoint word is incomplete": (
        "incomplete_endpoint_word"
    ),
    "selected endpoint word is not contiguous": (
        "noncontiguous_endpoint_word"
    ),
    "selected reduction has fewer than two complete operand words": (
        "insufficient_complete_operand_words"
    ),
    "fewer than two complete registered operand words recovered": (
        "insufficient_complete_operand_words"
    ),
    "operand and endpoint transaction controls differ": (
        "operand_endpoint_control_mismatch"
    ),
    "replaceable arithmetic cone has outside side fanout": (
        "outside_side_fanout"
    ),
}


def _unsupported_commutative_contract_reason(exc: Exception) -> str:
    """Classify exact semantic non-eligibility without hiding tool failures."""
    message = str(exc)
    for text, reason in _UNSUPPORTED_COMMUTATIVE_CONTRACT_ERRORS.items():
        if text in message:
            return reason
    return ""


def _compact_operator_judgment_chain(value: object) -> dict:
    """Keep Luna's decision links without repeating the full probe payload."""
    if not isinstance(value, dict):
        return {}
    steps = []
    for raw in value.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        discriminator = raw.get("next_discriminator") or {}
        steps.append({
            "condition": raw.get("condition"),
            "state": raw.get("state"),
            "scope": raw.get("scope"),
            "missing_evidence": list(raw.get("missing_evidence") or [])[:3],
            "next_discriminator": {
                key: discriminator.get(key)
                for key in ("question", "source", "event", "decision_owner")
                if discriminator.get(key) is not None
            },
        })
    return {
        "version": value.get("version"),
        "authority": value.get("authority"),
        "effect_chain": value.get("effect_chain") or [],
        "steps": steps,
        "threshold_semantics": value.get("threshold_semantics"),
    }


def _semantic_screen_candidate(
    family_ref: str,
    screen: dict,
    *,
    nominated_family_ref: str,
) -> dict:
    """Build one name-free sibling comparison row for Luna and audit."""
    summary = screen.get("summary") or {}
    recovery = summary.get("generating_operator_recovery") or {}
    fir_eligible = bool(summary.get("registered_fir_action_eligible"))
    action_eligible = bool(summary.get("registered_action_eligible"))
    complete_generating = bool(
        recovery.get("status") == "recovered" and action_eligible
    )
    action = None
    if action_eligible:
        action = {
            "skill": "operator_rewrite",
            "target": (
                "registered_fir_data_cone_rewrite"
                if fir_eligible else "fixed_point_dsp_rebuild_v1"
            ),
            "params": {"family_ref": family_ref},
        }
    return {
        "family_ref": family_ref,
        "nomination_source": (
            "luna_selected"
            if family_ref == nominated_family_ref else "framework_live_sibling"
        ),
        "generating_operator_recovery": recovery,
        "complete_generating_operator_recovered": complete_generating,
        "selection_tier": (
            "fir_generating_operator"
            if fir_eligible and complete_generating
            else "generating_operator"
            if complete_generating
            else "alternate_local_fixed_point"
            if action_eligible
            else "rejected"
        ),
        "candidate_action": action,
        "semantic_operator_class": (
            (summary.get("proved_semantic_signature") or {}).get(
                "operator_class"
            )
        ),
        "term_count": summary.get("term_count"),
        "fixed_product_count": summary.get("fixed_product_count"),
        "passthrough_count": summary.get("passthrough_count"),
        "selected_instance_count": summary.get("selected_instance_count"),
        "dsp48e2_count": summary.get("dsp48e2_count"),
        "schedule": (
            (summary.get("proved_semantic_signature") or {}).get("schedule")
            or {}
        ),
        "source_topology_cost": summary.get("source_topology_cost") or {},
        "emitted_topology_cost": summary.get("emitted_topology_cost") or {},
        "routed_qor_still_unknown": True,
    }


def _compact_json_value(value: object, depth: int = 0) -> object:
    """Bound nested evidence while preserving its JSON type and disposition."""
    if depth >= 5:
        return "..."
    if isinstance(value, dict):
        return {
            str(key): _compact_json_value(item, depth + 1)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, (list, tuple)):
        return [_compact_json_value(item, depth + 1) for item in value[:6]]
    if isinstance(value, str) and len(value) > 320:
        return value[:317] + "..."
    return value


def _compact_operator_mining_families(value: object) -> object:
    """Keep every anonymous family row while dropping response bulk."""
    if not isinstance(value, dict):
        return _compact_json_value(value)
    result = {}
    for kind, raw_rows in value.items():
        rows = []
        for raw in raw_rows or []:
            if not isinstance(raw, dict):
                continue
            coverage = raw.get("critical_path_coverage") or {}
            rows.append({
                key: raw.get(key)
                for key in (
                    "family_ref", "kind", "member_count",
                    "critical_member_count", "critical_path_count",
                    "occurrence_count", "source_register_count",
                    "column_span", "transition_signature", "proof_status",
                    "mutation_eligible",
                )
                if raw.get(key) is not None
            } | ({
                "critical_path_coverage": {
                    key: coverage.get(key)
                    for key in (
                        "covered_path_count", "earliest_covered_rank",
                        "covers_worst_path", "endpoint_path_count",
                        "top5_covered_count", "top10_covered_count",
                        "all_path_coverage_ratio",
                    )
                    if coverage.get(key) is not None
                }
            } if isinstance(coverage, dict) and coverage else {}))
        result[str(kind)] = rows
    return result


def _bounded_probe_response(response: str) -> tuple[str, bool]:
    """Bound a probe response without corrupting a machine-readable JSON list."""
    text = response or ""
    if len(text) <= MAX_RESPONSE_CHARS:
        return text, False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text[:MAX_RESPONSE_CHARS], True
    if isinstance(parsed, dict):
        preferred = (
            "status", "kind", "read_only_after_restore", "contract",
            "action_assessment", "family_ref",
            # Cross-family semantic screens are useful only when the bounded
            # payload retains the comparison itself.  Previously the large
            # selected-family topology summary consumed the full response and
            # silently dropped these rows, so Luna still saw only the first
            # timing-ranked fallback after an expensive sibling walk.
            "nominated_family_ref", "selected_family_changed",
            "selection_disposition", "semantic_candidate_assessments",
            "generating_operator_recovery",
            # Action-bearing discovery rows must survive before descriptive
            # judgment prose.  A real 16-family operator_mining response used
            # to spend the whole bound on operator_judgment_chain and omit the
            # `families` object, leaving Luna with an instruction to choose a
            # sequential_N ref that was not actually visible.
            "family_counts", "family_selection_semantics",
            "consumer_driven_family_comparison", "families",
            "template_compatibility", "recover_contract_with",
            "recover_sequential_contract_with",
            "recover_consumer_driven_contract_with",
            # A recovered sequential contract is useful only if Luna can see
            # the typed follow-up and its exact local authority.  Keep these
            # compact action-bearing fields ahead of large descriptive cone
            # and closure summaries so an 11 KB response cannot silently
            # erase every executable candidate.
            "registered_backend_follow_up", "operator_action_contract",
            "producer_registered_width", "producer_primitive_type_counts",
            "current_wall_coverage", "consumer_input_word_widths",
            "observable_output_word_widths",
            "producer_to_consumer_contraction", "control_protocol",
            "pipeline_depth", "pipeline_depth_basis", "semantic_candidates",
            "missing_proof", "predicted_surviving_wall",
            "structural_boundary_recovered", "proof_input_eligible",
            "proof_input_scope", "recovery_issue_count",
            "target_width", "contract_status", "registered_bank_count",
            "selected_instance_count", "module_count", "replica_discovery",
            "term_count", "fixed_product_count", "negative_product_count",
            "passthrough_count", "dsp48e2_count", "sample_vector_count",
            "quantization_group_count", "interacting_group_count",
            "proved_semantic_signature", "source_topology_cost",
            "emitted_topology_cost", "topology_counterfactual",
            "action_assessment", "operator_judgment_chain",
            "mutation_eligible", "limitations",
        )
        ordered = [*preferred, *(key for key in parsed if key not in preferred)]
        retained: dict[str, object] = {"response_structurally_truncated": True}
        omitted = []
        for key in ordered:
            if key not in parsed:
                continue
            compacted_value = (
                _compact_operator_mining_families(parsed[key])
                if key == "families"
                else _compact_json_value(parsed[key])
            )
            candidate = {
                **retained,
                key: compacted_value,
            }
            encoded = json.dumps(
                candidate, ensure_ascii=False, separators=(",", ":")
            )
            if len(encoded) <= MAX_RESPONSE_CHARS:
                retained = candidate
            else:
                omitted.append(key)
        retained["omitted_top_level_fields"] = omitted
        encoded = json.dumps(
            retained, ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded) > MAX_RESPONSE_CHARS:
            retained.pop("omitted_top_level_fields", None)
            encoded = json.dumps(
                retained, ensure_ascii=False, separators=(",", ":")
            )
        return encoded, True
    if not isinstance(parsed, list):
        return text[:MAX_RESPONSE_CHARS], True

    retained = []
    encoded = "[]"
    for item in parsed:
        candidate = json.dumps(
            [*retained, item],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(candidate) > MAX_RESPONSE_CHARS:
            break
        retained.append(item)
        encoded = candidate
    return encoded, len(retained) < len(parsed)


class CustomSkill:
    """Pass through a named MCP probe after the agent's legality gate."""

    name = "custom"

    async def execute(
        self,
        mcp,
        target: str,                    # "vivado:tool_name" or "rapidwright:tool_name"
        params: dict,                   # {arguments: {...}, description: "...", measure_timing: bool}
        before_wns: float,
        clock_period: Optional[float],
        probe_candidate: Optional[dict] = None,
    ) -> SkillResult:
        run_dir = mcp.run_dir
        output_dcp = run_dir / "current_best.dcp"  # custom calls don't produce their own DCP

        server, _, tool_name = target.partition(":")
        server = server.strip().lower()
        tool_name = tool_name.strip()

        if server not in ("vivado", "rapidwright", "rw", "v"):
            return SkillResult.failure(
                before_wns,
                f"custom target must be 'vivado:tool' or 'rapidwright:tool', got {target!r}",
                output_dcp,
            )
        if not tool_name:
            return SkillResult.failure(
                before_wns,
                f"custom target missing tool name, got {target!r}",
                output_dcp,
            )

        arguments = params.get("arguments", {}) or {}
        if not isinstance(arguments, dict):
            return SkillResult.failure(
                before_wns,
                f"custom params.arguments must be a dict, got {type(arguments).__name__}",
                output_dcp,
            )
        description = str(params.get("description", ""))[:200]
        measure_timing = bool(params.get("measure_timing", False))
        probe_timeout = _framework_owned_probe_timeout(params)

        logger.info(
            f"[custom] {server}:{tool_name} args={str(arguments)[:200]} "
            f"measure_timing={measure_timing} desc={description!r}"
        )

        auto_prerequisite = False
        screened_candidates = []
        try:
            tool_arguments = prepare_probe_arguments(
                mcp, tool_name, arguments, probe_candidate
            )
        except MissingProbeHandoffError as exc:
            if tool_name != "operator_mining":
                return SkillResult.failure(
                    before_wns,
                    f"{server}:{tool_name} missing probe handoff: {exc}",
                    output_dcp,
                )
            logger.info(
                "[custom] operator_mining is missing its same-seed path "
                "handoff; running the read-only extraction prerequisite"
            )
            try:
                extracted = await mcp.call_vivado(
                    "extract_critical_path_cells",
                    {"clock": "clk_fpl26contest", "num_paths": 40},
                    timeout=probe_timeout,
                )
                capture_probe_response(
                    mcp,
                    "extract_critical_path_cells",
                    extracted or "",
                    probe_candidate,
                )
                tool_arguments = prepare_probe_arguments(
                    mcp, tool_name, arguments, probe_candidate
                )
                auto_prerequisite = True
            except Exception as prerequisite_exc:  # noqa: BLE001
                logger.warning(
                    "[custom] operator_mining prerequisite failed: %s",
                    prerequisite_exc,
                )
                return SkillResult.failure(
                    before_wns,
                    "rapidwright:operator_mining automatic read-only path "
                    f"extraction failed: {prerequisite_exc}",
                    output_dcp,
                )
        except ProbeArgumentContractError as exc:
            logger.warning(
                "[custom] %s:%s rejected before MCP: %s",
                server,
                tool_name,
                exc,
            )
            return SkillResult.failure(
                before_wns,
                f"{server}:{tool_name} argument contract rejected before MCP: {exc}",
                output_dcp,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[custom] %s:%s handoff preparation rejected before MCP: %s",
                server,
                tool_name,
                exc,
            )
            return SkillResult.failure(
                before_wns,
                f"{server}:{tool_name} handoff preparation rejected before MCP: {exc}",
                output_dcp,
            )

        try:
            if tool_name == "screen_fixed_point_dsp_contract":
                if server not in ("vivado", "v"):
                    raise ValueError(
                        "screen_fixed_point_dsp_contract is a Vivado-side "
                        "framework probe"
                    )
                from .operator_rewrite import screen_fixed_point_dsp_contract

                nominated_family_ref = str(
                    tool_arguments.get("family_ref") or ""
                )
                candidate_refs = ordered_sequential_family_screen_refs(
                    mcp,
                    artifact_sha256=str(
                        (probe_candidate or {}).get("artifact_sha256") or ""
                    ),
                    selected_family_ref=nominated_family_ref,
                    candidate_id=str(
                        (probe_candidate or {}).get("candidate_id") or ""
                    ),
                )
                screens_by_ref = {}
                for candidate_ref in candidate_refs:
                    try:
                        candidate_screen = await screen_fixed_point_dsp_contract(
                            mcp,
                            input_dcp=Path(run_dir) / "current_best.dcp",
                            family_ref=candidate_ref,
                            cone_dir=(
                                Path(run_dir)
                                / f"operator_fixed_point_screen_{candidate_ref}"
                            ),
                            # Cross-family comparison is handled here so each
                            # family retains an independent proof and rejection.
                            selected_family_only=True,
                        )
                    except (RuntimeError, ValueError) as exc:
                        inconclusive = _is_inconclusive_fixed_point_screen_error(
                            exc
                        )
                        screened_candidates.append({
                            "family_ref": candidate_ref,
                            "nomination_source": (
                                "luna_selected"
                                if candidate_ref == nominated_family_ref
                                else "framework_live_sibling"
                            ),
                            "generating_operator_recovery": {
                                "status": (
                                    "inconclusive_infrastructure"
                                    if inconclusive else "rejected"
                                ),
                                "reason": str(exc)[:500],
                                "failure_scope": "selected_family_only",
                                "remaining_family_exploration_allowed": True,
                            },
                            "complete_generating_operator_recovered": False,
                            "selection_tier": (
                                "inconclusive_infrastructure"
                                if inconclusive else "rejected"
                            ),
                            "candidate_action": None,
                            "routed_qor_still_unknown": True,
                        })
                        continue
                    screens_by_ref[candidate_ref] = candidate_screen
                    candidate_row = _semantic_screen_candidate(
                        candidate_ref,
                        candidate_screen,
                        nominated_family_ref=nominated_family_ref,
                    )
                    screened_candidates.append(candidate_row)
                    # A locally complete operator does not establish that it is
                    # the complete live timing population.  Keep screening the
                    # other current-seed endpoint owners so Luna can compare
                    # independently proved local actions instead of inheriting
                    # whichever family happened to be discovered first.

                viable_rows = [
                    row for row in screened_candidates
                    if row.get("candidate_action") is not None
                ]
                if not viable_rows:
                    # A read-only screen may be informative while leaving all
                    # mutations ineligible.  Return that evidence to Luna; fail
                    # only when every sibling probe itself raised.
                    observed_rows = [
                        row for row in screened_candidates
                        if row.get("family_ref") in screens_by_ref
                    ]
                    if observed_rows:
                        viable_rows = observed_rows
                    else:
                        inconclusive_rows = [
                            row for row in screened_candidates
                            if row.get("selection_tier")
                            == "inconclusive_infrastructure"
                        ]
                        if inconclusive_rows:
                            reasons = [
                                str(
                                    (row.get("generating_operator_recovery") or {})
                                    .get("reason")
                                    or "inconclusive fixed-point screen"
                                )
                                for row in inconclusive_rows
                            ]
                            raise RuntimeError(
                                "endpoint-owner fixed-point screen set is "
                                "inconclusive because one or more read-only "
                                "extractors did not complete; do not cache this "
                                "as a semantic negative: "
                                + "; ".join(reasons)[:1000]
                            )
                        reasons = [
                            str(
                                (row.get("generating_operator_recovery") or {}).get(
                                    "reason"
                                )
                                or "semantic screen rejected"
                            )
                            for row in screened_candidates
                        ]
                        raise ValueError(
                            "all endpoint-owner fixed-point screens in the "
                            "current screen set rejected; upstream consumer-"
                            "driven families and other operator classes remain "
                            "eligible for independent recovery: "
                            + "; ".join(reasons)[:1000]
                        )
                complete_rows = [
                    row for row in viable_rows
                    if row.get("complete_generating_operator_recovered")
                ]
                if complete_rows:
                    chosen_row = next(
                        (
                            row for row in complete_rows
                            if row.get("family_ref") == nominated_family_ref
                        ),
                        complete_rows[0],
                    )
                    selection_disposition = (
                        "luna_must_choose_among_complete_live_candidates"
                    )
                else:
                    # A local fixed-point fallback is an experiment candidate,
                    # not a semantic winner.  Keep the nominated row as the
                    # compatibility summary while requiring Luna to compare
                    # every retained sibling assessment explicitly.
                    chosen_row = next(
                        (
                            row for row in viable_rows
                            if row.get("family_ref") == nominated_family_ref
                        ),
                        viable_rows[0],
                    )
                    selection_disposition = (
                        "luna_must_choose_among_alternate_local_measurements"
                    )
                family_ref = str(chosen_row["family_ref"])
                screen = screens_by_ref[family_ref]
                summary = screen["summary"]
                terms = list(screen["contract"].get("terms") or [])
                fir_action_eligible = bool(
                    summary.get("registered_fir_action_eligible")
                )
                registered_action_eligible = bool(
                    summary.get("registered_action_eligible")
                )
                response = json.dumps({
                    "status": "success",
                    "kind": "fixed_point_dsp_contract_screen",
                    "read_only_after_restore": True,
                    "family_ref": family_ref,
                    "nominated_family_ref": nominated_family_ref,
                    "selected_family_changed": (
                        bool(complete_rows)
                        and family_ref != nominated_family_ref
                    ),
                    "selection_disposition": selection_disposition,
                    "framework_default_is_not_recommendation": True,
                    "family_selection_semantics": (
                        "Every current-seed endpoint-owner family is screened "
                        "independently. The scalar family_ref preserves response "
                        "compatibility and is not a recommendation; Luna must compare "
                        "all semantic_candidate_assessments using live evidence. "
                        "Rejected siblings do not blacklist the family."
                    ),
                    "semantic_candidate_assessments": screened_candidates,
                    "generating_operator_recovery": summary.get(
                        "generating_operator_recovery"
                    ) or {},
                    "target_width": screen["target_width"],
                    "contract_status": summary.get("status"),
                    "term_count": summary.get("term_count"),
                    "fixed_product_count": summary.get("fixed_product_count"),
                    "negative_product_count": sum(
                        item.get("kind") == "fixed_product_slice"
                        and int(item.get("coefficient", 0)) < 0
                        for item in terms
                    ),
                    "passthrough_count": summary.get("passthrough_count"),
                    "dsp48e2_count": summary.get("dsp48e2_count"),
                    "sample_vector_count": summary.get("sample_vector_count"),
                    "quantization_group_count": summary.get(
                        "quantization_group_count"
                    ),
                    "interacting_group_count": summary.get(
                        "interacting_group_count"
                    ),
                    "registered_bank_count": summary.get(
                        "registered_bank_count"
                    ),
                    "module_count": summary.get("module_count"),
                    "selected_instance_count": summary.get(
                        "selected_instance_count"
                    ),
                    "replica_discovery": summary.get(
                        "replica_discovery"
                    ) or {},
                    "proved_semantic_signature": summary.get(
                        "proved_semantic_signature"
                    ) or {},
                    "source_topology_cost": summary.get(
                        "source_topology_cost"
                    ) or {},
                    "emitted_topology_cost": summary.get(
                        "emitted_topology_cost"
                    ) or {},
                    "topology_counterfactual": summary.get(
                        "topology_counterfactual"
                    ) or {},
                    "operator_judgment_chain": screen.get(
                        "operator_judgment_chain"
                    ) and _compact_operator_judgment_chain(
                        screen.get("operator_judgment_chain")
                    ) or {},
                    "mutation_eligible": registered_action_eligible,
                    "action_assessment": {
                        "action_type": (
                            "typed_local_operator_rewrite_measurement"
                            if registered_action_eligible
                            else "local_operator_measurement_probe"
                        ),
                        "authority": (
                            "exact_generating_contract_then_disposable_candidate_measurement"
                            if chosen_row.get(
                                "complete_generating_operator_recovered"
                            )
                            else "alternate_local_contract_measurement_only"
                            if registered_action_eligible
                            else "disposable_candidate_measurement_only"
                        ),
                        "selection_status": (
                            "executable" if registered_action_eligible
                            else "exploration_candidate"
                        ),
                        "registered_backend_available": True,
                        "candidate_action": {
                            "skill": "operator_rewrite",
                            "target": (
                                "registered_fir_data_cone_rewrite"
                                if fir_action_eligible
                                else "fixed_point_dsp_rebuild_v1"
                            ),
                            "params": {"family_ref": family_ref},
                        },
                        "complete_operator_rewrite_eligible": (
                            bool(chosen_row.get(
                                "complete_generating_operator_recovered"
                            ))
                        ),
                        "topology_counterfactual": summary.get(
                            "topology_counterfactual"
                        ) or {},
                        "generating_operator_closure": screen.get(
                            "generating_operator_closure"
                        ) or {},
                        "does_not_establish": (
                            [
                                "whole_dcp_post_mutation_equivalence",
                                "routed_fmax_improvement",
                            ]
                            if chosen_row.get(
                                "complete_generating_operator_recovered"
                            )
                            else [
                                "complete_generating_operator_scope",
                                "formal_source_equivalence",
                                "schedule_or_protocol_equivalence",
                                "routed_fmax_improvement",
                            ]
                        ),
                    },
                    "limitations": [
                        "screen recovers the same registered-product backend "
                        "used by the action but does not mutate or route it",
                        "whole-DCP equivalence and trusted Fmax remain required",
                    ],
                }, sort_keys=True, separators=(",", ":"))
            elif server in ("vivado", "v"):
                response = await mcp.call_vivado(
                    tool_name, tool_arguments, timeout=probe_timeout
                )
            else:
                candidate = probe_candidate or {}
                await mcp.ensure_rapidwright_probe(
                    tool_name,
                    tool_arguments,
                    candidate_id=candidate.get("candidate_id"),
                    artifact_path=candidate.get("artifact_path"),
                    artifact_sha256=candidate.get("artifact_sha256"),
                    timeout=probe_timeout,
                )
                response = await mcp.call_rw(
                    tool_name, tool_arguments, timeout=probe_timeout
                )
        except Exception as e:
            # A deterministic, read-only semantic screen that proves no live
            # family is currently supported is evidence, not an execution
            # failure.  Return it as a successful negative observation so the
            # exact-seed probe checkpoint can reuse it after resume.  Transient
            # MCP/tool failures still fail normally and are never cached.
            if (
                tool_name == "recover_consumer_driven_operator_contract"
                and _is_unsupported_consumer_contract_error(e)
            ):
                try:
                    family_ref = str(arguments.get("family_ref") or "")
                    artifact_sha256 = str(
                        (probe_candidate or {}).get("artifact_sha256") or ""
                    ).lower()
                    source_dcp = Path(str(
                        (probe_candidate or {}).get("artifact_path")
                        or (Path(run_dir) / "current_best.dcp")
                    ))
                    live = resolve_sequential_family_handoff(
                        mcp,
                        artifact_sha256=artifact_sha256,
                        family_ref=family_ref,
                    )
                    checkpoint_scope = "exact_current_run_live_family"
                    try:
                        checkpoint_run_id = str(params.get("_run_id") or "")
                        checkpoint_run_source = str(
                            params.get("_run_source_sha256") or ""
                        ).lower()
                        checkpoint = load_consumer_rewrite_checkpoint(
                            source_dcp,
                            run_id=checkpoint_run_id,
                            run_source_sha256=checkpoint_run_source,
                            source_sha256=artifact_sha256,
                            candidate_id=live["candidate_id"],
                        )
                    except ValueError:
                        checkpoint = None
                        if checkpoint is None:
                            family = live.get("family") or {}
                            bit_mapping = family.get("bit_mapping") or {}
                            coverage = family.get("critical_path_coverage") or {}
                            diagnostics = _unsupported_consumer_diagnostics(e)
                            profiles = diagnostics.get(
                                "owner_boundary_profiles"
                            ) or []
                            response = json.dumps({
                                "status": "no_supported_contract",
                                "kind": "consumer_driven_operator_contract",
                                "version": 2,
                                "fresh_rediscovery": True,
                                "read_only": True,
                                "candidate_id": live["candidate_id"],
                                "design_sha256": artifact_sha256,
                                "public_contract": {
                                    "producer_registered_width": int(
                                        bit_mapping.get("member_count") or 0
                                    ),
                                    "current_wall_coverage": {
                                        key: coverage.get(key)
                                        for key in (
                                            "covered_path_count",
                                            "ordered_path_count",
                                            "covers_worst_path",
                                            "top5_covered_count",
                                            "top10_covered_count",
                                        )
                                    },
                                    "consumer_input_word_widths": sorted({
                                        int(width)
                                        for profile in profiles
                                        for width in (
                                            profile.get("input_widths") or []
                                        )
                                        if isinstance(width, int)
                                    }),
                                    "observable_output_word_widths": sorted({
                                        int(width)
                                        for profile in profiles
                                        for width in (
                                            profile.get("output_widths") or []
                                        )
                                        if isinstance(width, int)
                                    }),
                                    "producer_to_consumer_contraction": {
                                        "status": (
                                            "no_supported_registered_arithmetic_boundary"
                                        ),
                                        **diagnostics,
                                    },
                                    "semantic_candidates": [],
                                    "missing_proof": [
                                        "supported observable arithmetic boundary",
                                        "complete generating-operator ownership",
                                        "width loss and transaction schedule",
                                    ],
                                    "predicted_surviving_wall": (
                                        "the current logic-depth wall remains live; "
                                        "this negative screen rejects only the selected "
                                        "consumer-contraction hypothesis"
                                    ),
                                },
                                "action_assessment": {
                                    "status": "no_supported_contract",
                                    "authority": (
                                        "exact_live_boundary_negative_screen"
                                    ),
                                    "candidate_action": None,
                                    "not_a_hard_trigger": True,
                                    "luna_owns_final_selection": True,
                                },
                                "mutation_eligible": False,
                                "proof_status": (
                                    "negative_live_consumer_boundary_screen"
                                ),
                            }, sort_keys=True, separators=(",", ":"))
                            public_response = capture_probe_response(
                                mcp, tool_name, response, probe_candidate
                            )
                            truncated, was_truncated = (
                                _bounded_probe_response(public_response)
                            )
                            logger.info(
                                "[custom] retained exact-seed negative consumer "
                                "screen for live family %s",
                                family_ref,
                            )
                            return SkillResult(
                                success=True,
                                before_wns=before_wns,
                                after_wns=before_wns,
                                delta_wns=0.0,
                                is_legal=True,
                                output_dcp=output_dcp,
                                summary=(
                                    "custom:rapidwright:"
                                    "recover_consumer_driven_operator_contract "
                                    "negative live-boundary screen"
                                ),
                                details=truncated,
                                details_truncated=was_truncated,
                            )
                    family = live.get("family") or {}
                    bit_mapping = family.get("bit_mapping") or {}
                    coverage = family.get("critical_path_coverage") or {}
                    public_contract = dict(checkpoint["public_contract"])
                    public_contract.update({
                        "producer_registered_width": int(
                            bit_mapping.get("member_count") or 0
                        ),
                        "current_wall_coverage": {
                            key: coverage.get(key)
                            for key in (
                                "covered_path_count", "ordered_path_count",
                                "covers_worst_path", "top5_covered_count",
                                "top10_covered_count",
                            )
                        },
                        "producer_to_consumer_contraction": {
                            "registered_state_bits": int(
                                bit_mapping.get("member_count") or 0
                            ),
                            "observable_output_bits": sum(
                                public_contract.get(
                                    "observable_output_word_widths"
                                ) or []
                            ),
                            "checkpoint_scope": checkpoint_scope,
                        },
                    })
                    response = json.dumps({
                        "status": "success",
                        "kind": "consumer_driven_operator_contract",
                        "version": 2,
                        "fresh_rediscovery": True,
                        "read_only": True,
                        "candidate_id": live["candidate_id"],
                        "design_sha256": artifact_sha256,
                        "contract_sha256": checkpoint["contract_sha256"],
                        "public_contract": public_contract,
                        "private_contract": {
                            "bundle": checkpoint["bundle"],
                        },
                        "action_assessment": {
                            "status": "executable",
                            "authority": (
                                "current_run_checkpoint_plus_live_family"
                            ),
                            "candidate_action": {
                                "skill": "operator_rewrite",
                                "target": (
                                    "consumer_driven_registered_arithmetic_rewrite_v1"
                                ),
                                "params": {"family_ref": family_ref},
                            },
                            "not_a_hard_trigger": True,
                            "luna_owns_final_selection": True,
                        },
                        "mutation_eligible": True,
                        "proof_status": (
                            "current_run_checkpoint_revalidated_on_live_family"
                        ),
                        "checkpoint_scope": checkpoint_scope,
                    }, sort_keys=True, separators=(",", ":"))
                    logger.info(
                        "[custom] resumed exact-current-run consumer proof checkpoint "
                        "for live family %s after backend probe rejection: %s",
                        family_ref,
                        e,
                    )
                except Exception:
                    logger.error(f"[custom] {server}:{tool_name} failed: {e}")
                    return SkillResult.failure(
                        before_wns,
                        f"{server}:{tool_name} raised: {e}",
                        output_dcp,
                    )
            elif (
                tool_name == "recover_commutative_reassociation_contract"
                and _unsupported_commutative_contract_reason(e)
            ):
                reason = _unsupported_commutative_contract_reason(e)
                logger.info(
                    "[custom] retained exact-seed negative commutative screen: %s",
                    reason,
                )
                response = json.dumps({
                    "status": "no_supported_contract",
                    "kind": "commutative_reassociation_contract",
                    "version": 1,
                    "fresh_rediscovery": True,
                    "read_only": True,
                    "contract_condition_failure": reason,
                    "mutation_eligible": False,
                    "proof_status": "negative_live_commutative_boundary_screen",
                    "action_assessment": {
                        "status": "no_supported_contract",
                        "candidate_action": None,
                        "not_a_hard_trigger": True,
                        "luna_owns_final_selection": True,
                        "remaining_family_exploration_allowed": True,
                        "physical_alternative_allowed": True,
                    },
                    "limitations": [
                        "the rejection is exact-seed and scoped to the live "
                        "commutative operand-reassociation hypothesis",
                        "it does not reject a different complete operator family "
                        "or a physical implementation alternative",
                    ],
                }, sort_keys=True, separators=(",", ":"))
            elif (
                tool_name == "screen_fixed_point_dsp_contract"
                and screened_candidates
                and all(
                    row.get("selection_tier") == "rejected"
                    for row in screened_candidates
                    if isinstance(row, dict)
                )
            ):
                logger.info(
                    "[custom] retained deterministic negative semantic screen: %s",
                    e,
                )
                response = json.dumps({
                    "status": "no_supported_contract",
                    "kind": "fixed_point_dsp_contract_screen",
                    "read_only_after_restore": True,
                    "nominated_family_ref": str(
                        tool_arguments.get("family_ref") or ""
                    ),
                    "selected_family_changed": False,
                    "selection_disposition": (
                        "no_endpoint_owner_fixed_point_family_supported"
                    ),
                    "semantic_candidate_assessments": screened_candidates,
                    "mutation_eligible": False,
                    "action_assessment": {
                        "selection_status": "no_supported_contract",
                        "candidate_action": None,
                        "remaining_family_exploration_allowed": True,
                        "consumer_driven_recovery_remains_allowed": True,
                        "remaining_operator_classes": [
                            "consumer_driven_registered_arithmetic",
                            "bounded_boolean_arithmetic_compressor",
                        ],
                        "does_not_blacklist_future_live_state": True,
                    },
                    "limitations": [
                        "the rejection is scoped to the endpoint-owner fixed-point screen set",
                        "it does not reject an upstream high-coverage producer or consumer-driven contract",
                        "negative result is exact-seed and implementation-ABI bound",
                        "a new Candidate or recovery ABI requires a fresh screen",
                    ],
                }, sort_keys=True, separators=(",", ":"))
            else:
                logger.error(f"[custom] {server}:{tool_name} failed: {e}")
                return SkillResult.failure(
                    before_wns,
                    f"{server}:{tool_name} raised: {e}",
                    output_dcp,
                )

        try:
            public_response = capture_probe_response(
                mcp, tool_name, response or "", probe_candidate
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            retained_note = ""
            if tool_name in {
                "operator_mining",
                "recover_recurrence_contract",
                "recover_sequential_operator_contract",
                "recover_consumer_driven_operator_contract",
            }:
                retained_note = "; trusted critical-path handoff remains intact"
            logger.error(
                "[custom] %s returned a non-JSON/invalid response after MCP%s: %s",
                tool_name,
                retained_note,
                exc,
            )
            return SkillResult.failure(
                before_wns,
                f"{server}:{tool_name} returned a non-JSON/invalid response after MCP"
                f"{retained_note}: {exc}",
                output_dcp,
            )
        if auto_prerequisite:
            parsed_public = json.loads(public_response)
            if isinstance(parsed_public, dict):
                parsed_public["framework_auto_prerequisite"] = (
                    "extract_critical_path_cells"
                )
                public_response = json.dumps(
                    parsed_public, ensure_ascii=False, separators=(",", ":")
                )
        truncated, was_truncated = _bounded_probe_response(public_response)

        # WNS measurement is opt-in — many custom calls are read-only probes
        # (report_timing, search_cells, get_design_info) and re-querying WNS
        # just to report delta=0 wastes Vivado time.
        after_wns = before_wns
        if measure_timing:
            try:
                measured = await mcp.get_wns()
                if measured is not None:
                    after_wns = measured
            except Exception as e:
                logger.warning(f"[custom] could not measure WNS after: {e}")

        delta = after_wns - before_wns

        head = truncated.replace("\n", " ")[:140]
        summary = (
            f"custom:{server}:{tool_name} delta_wns={delta:+.3f} → {head}"
            + (" …(truncated)" if was_truncated else "")
        )[:200]

        return SkillResult(
            success=True,
            before_wns=before_wns,
            after_wns=after_wns,
            delta_wns=delta,
            is_legal=True,   # can't verify legality from a generic tool call
            output_dcp=output_dcp,
            summary=summary,
            details=truncated,   # full truncated response for the next deep round
            details_truncated=was_truncated,
        )
