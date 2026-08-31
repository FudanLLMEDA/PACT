"""Deterministic Phase-6 failure-owner diagnosis and experiment proposals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


SCHEMA = "failure_owner_diagnosis_v1"

_PROPOSALS = {
    "mining_extraction_tool": {
        "hypothesis": "The target family is absent or malformed because the miner does not recover its complete measured closure.",
        "experiment_class": "read_only_mining_fixture",
        "stop_condition": "Stop when the frozen fixture yields a complete normalized family or a stable typed abstention.",
    },
    "recovery_proof_tool": {
        "hypothesis": "The selected family is correct, but one explicit data, control, schedule, or proof obligation prevents exact authority.",
        "experiment_class": "standalone_contract_or_proof_discriminator",
        "stop_condition": "Stop on exact proof, a bounded counterexample, or the predeclared solver budget; do not mutate on unknown.",
    },
    "lowering_emitter_tool": {
        "hypothesis": "The exact rewrite regresses because the emitted topology introduces a deeper serial arithmetic wall than the measured parent topology.",
        "experiment_class": "ooc_topology_and_timing_discriminator",
        "stop_condition": "Stop unless inventory and OOC timing remove the observed serial topology before an integrated trial.",
    },
    "selection_wall_coverage": {
        "hypothesis": "The rewrite targeted a legal exact family that did not cover the parent's measured top wall and therefore could not produce global gain.",
        "experiment_class": "read_only_wall_coverage_rebaseline",
        "stop_condition": "Stop unless the selected family has positive measured top-N wall coverage on the exact current seed.",
    },
    "physical_shared_net_backend": {
        "hypothesis": "The local physical edit perturbs shared or nonincident routing, so any local placement hypothesis is dominated by uncontrolled global route changes.",
        "experiment_class": "route_boundary_audit_and_bounded_backend",
        "stop_condition": "Stop on any nonincident static-route change or after the predeclared bounded destination set is exhausted.",
    },
    "skill_presentation_or_llm_policy": {
        "hypothesis": "Complete sibling evidence was available, but decision ordering caused selection of a measurably weaker family.",
        "experiment_class": "unchanged_gate_decision_packet_replay",
        "stop_condition": "Stop after one complete decision-chain replay with unchanged mechanical authority gates.",
    },
    "integration_control_flow_bug": {
        "hypothesis": "A typed rejection incorrectly changes action authority through fallback or exception handling.",
        "experiment_class": "fail_closed_integration_regression",
        "stop_condition": "Stop when the exact rejection remains visible and emits no alternate mutation.",
    },
    "current_seed_refresh_resume_bug": {
        "hypothesis": "Promotion left parent-scoped semantic evidence selectable on the new incumbent.",
        "experiment_class": "promotion_resume_invalidation_regression",
        "stop_condition": "Stop when every parent substrate is historical and the next packet binds only to the promoted SHA.",
    },
    "skill_memory_soft_cooldown": {
        "hypothesis": "The decision loop cannot recognize an unchanged rejected proposal on the same artifact.",
        "experiment_class": "artifact_scoped_rejection_memory_regression",
        "stop_condition": "Stop when the repeated fingerprint is visible and cannot execute unchanged.",
    },
}


def classify_failure(observation: dict) -> dict:
    """Classify structured evidence using an explicit, stable precedence chain."""
    if not isinstance(observation, dict):
        raise ValueError("observation must be a mapping")
    evidence = []
    if observation.get("family_present") is False or observation.get("family_malformed") is True:
        owner = "mining_extraction_tool"
        evidence.append("family_absent_or_malformed")
    elif observation.get("stale_target_after_promotion") is True:
        owner = "current_seed_refresh_resume_bug"
        evidence.append("stale_target_after_promotion")
    elif observation.get("fallback_action_executed_after_typed_rejection") is True:
        owner = "integration_control_flow_bug"
        evidence.append("typed_rejection_changed_action_authority")
    elif observation.get("supported_direction_rejected_by_gate") is True:
        owner = "integration_control_flow_bug"
        evidence.append("supported_direction_rejected_by_inconsistent_gate")
    elif observation.get("post_emission_proof_serialization_mismatch") is True:
        owner = "integration_control_flow_bug"
        evidence.append("post_emission_proof_changed_shape_at_publication_boundary")
    elif observation.get("semantic_proof_identifier_rejected_as_secret") is True:
        owner = "integration_control_flow_bug"
        evidence.append("semantic_signal_identifier_collided_with_secret_field_policy")
    elif observation.get("signoff_command_invalid") is True:
        owner = "integration_control_flow_bug"
        evidence.append("final_signoff_command_used_incompatible_timing_switches")
    elif observation.get("repeated_unchanged_rejected_proposal") is True:
        owner = "skill_memory_soft_cooldown"
        evidence.append("unchanged_rejected_fingerprint_repeated")
    elif observation.get("proof_result") in {"unknown", "timeout", "inconclusive"}:
        owner = "recovery_proof_tool"
        evidence.append(f"proof_result={observation.get('proof_result')}")
    elif observation.get("contract_complete") is False:
        owner = "recovery_proof_tool"
        evidence.append("data_control_or_schedule_contract_incomplete")
    elif (
        int(observation.get("nonincident_static_route_changes") or 0) > 0
        or observation.get("boundary_audit_passed") is False
    ):
        owner = "physical_shared_net_backend"
        evidence.append(
            f"nonincident_static_route_changes={int(observation.get('nonincident_static_route_changes') or 0)}"
        )
    elif observation.get("selected_family_parent_top_n_coverage") is False:
        owner = "selection_wall_coverage"
        evidence.append("selected_family_absent_from_parent_top_n_wall")
    elif observation.get("emitted_topology_became_wall") is True or observation.get("emitted_topology_correct") is False:
        owner = "lowering_emitter_tool"
        evidence.append(str(observation.get("residual_wall_classification") or "emitted_topology_failure"))
    elif (
        observation.get("sibling_evidence_complete") is True
        and observation.get("weaker_sibling_selected") is True
    ):
        owner = "skill_presentation_or_llm_policy"
        evidence.append("complete_evidence_but_weaker_sibling_selected")
    elif observation.get("clock_or_placement_footprint_dominates") is True:
        owner = "physical_shared_net_backend"
        evidence.append("clock_or_placement_footprint_dominates")
    else:
        raise ValueError("structured observation does not match a known Phase-6 owner")

    proposal = _PROPOSALS[owner]
    return {
        "schema": SCHEMA,
        "case_id": str(observation.get("case_id") or "unidentified"),
        "owner": owner,
        "evidence": evidence,
        "falsifiable_hypothesis": proposal["hypothesis"],
        "experiment_class": proposal["experiment_class"],
        "stop_condition": proposal["stop_condition"],
        "automation_authority": "classification_and_proposal_only",
        "mutation_authority": False,
    }


def proposal_markdown(result: dict) -> str:
    """Render a concise worker-ready proposal from a diagnosis record."""
    return "\n".join([
        f"# Failure proposal: {result['case_id']}",
        "",
        f"- **Owner:** `{result['owner']}`",
        f"- **Falsifiable hypothesis:** {result['falsifiable_hypothesis']}",
        f"- **Experiment class:** `{result['experiment_class']}`",
        f"- **Stop condition:** {result['stop_condition']}",
        "- **Mutation authority:** none; complete the discriminator first.",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation_json", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    with args.observation_json.open() as stream:
        observation = json.load(stream)
    result = classify_failure(observation)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(encoded)
    else:
        print(encoded, end="")
    if args.markdown_out:
        args.markdown_out.write_text(proposal_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
