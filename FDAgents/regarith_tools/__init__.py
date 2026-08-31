"""Standalone registered-arithmetic extraction, proof, and lowering tools.

Exports are lazy so each ``python -m FDAgents.regarith_tools.<tool>`` CLI starts
without pre-importing the module it is about to execute.
"""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "build_concurrent_proof_decision_packet": (
        "agent_evidence", "build_concurrent_proof_decision_packet"
    ),
    "build_registered_arithmetic_decision_packet": (
        "agent_evidence", "build_registered_arithmetic_decision_packet"
    ),
    "registered_arithmetic_capability_schema": (
        "agent_evidence", "registered_arithmetic_capability_schema"
    ),
    "validate_registered_arithmetic_decision_packet": (
        "agent_evidence", "validate_registered_arithmetic_decision_packet"
    ),
    "validate_concurrent_proof_decision_packet": (
        "agent_evidence", "validate_concurrent_proof_decision_packet"
    ),
    "mine_critical_registered_families": ("miner", "mine_critical_registered_families"),
    "extract_registered_closures": ("closure", "extract_registered_closures"),
    "recover_data_terms_and_losses": ("data_recovery", "recover_data_terms_and_losses"),
    "recover_control_and_schedule": ("schedule_recovery", "recover_control_and_schedule"),
    "plan_from_family_facts": ("serializer", "plan_from_family_facts"),
    "bind_registered_plan": ("serializer", "bind_registered_plan"),
    "screen_and_prove": ("proof", "screen_and_prove"),
    "select_plan_lowering": ("lowering", "select_plan_lowering"),
    "check_emitted_topology": ("topology", "check_emitted_topology"),
    "build_vivado_measurement_command": ("route_measure", "build_vivado_measurement_command"),
    "adapt_generating_bundle_to_ir": (
        "production_product_sum", "adapt_generating_bundle_to_ir"
    ),
    "dispatch_product_sum_backend": (
        "production_product_sum", "dispatch_product_sum_backend"
    ),
    "execute_ooc_timing_gate": (
        "production_product_sum", "execute_ooc_timing_gate"
    ),
}


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(f"{__name__}.{module_name}"), attribute)


__all__ = sorted(_EXPORTS)
