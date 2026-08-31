"""Production lowering for freshly recovered registered product-sum families.

Every executable fact comes from the current source DCP.  The recovered bundle
is adapted to exact registered-operator IR, proved, rendered as fresh RTL, and
must pass a same-run OOC timing gate before integrated mutation is authorized.
No packaged checkpoint, historical topology, or prior-run timing fact is
accepted by this backend.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..registered_arithmetic_ir import (
    SCHEMA_VERSION,
    AccumulationOrder,
    ClockEnableSemantics,
    ClockIdentity,
    ControlSemantics,
    CycleAlignment,
    ExpectedWallCoverage,
    ModularAccumulationGroup,
    NumericLossBoundary,
    OutputShell,
    PlanKind,
    QuantizedProductTerm,
    RegisterStage,
    RegisterStageSchedule,
    RegisteredInputWord,
    RegisteredOperatorPlan,
    ReplaceableOldCone,
    ResetSetSemantics,
    RetainedSideConsumers,
    WordBoundary,
)
from ..registered_bridge import (
    render_registered_bridge_connect_tcl,
    render_registered_bridge_insert_jython,
    render_registered_bridge_verilog,
    validate_registered_bridge_bundle,
)
from ..relative_placement import (
    build_product_sum_relative_placement_manifest,
)
from .common import fail, object_digest, plan_input
from .proof import screen_and_prove


BACKEND_ID = "registered_complete_family_product_sum_v1"
BACKEND_SCHEMA_VERSION = "regarith-production-product-sum-v1"
OOC_GATE_SCHEMA_VERSION = "regarith-ooc-timing-gate-v1"
DEFAULT_OOC_PERIOD_NS = 2.0
OOC_SYNTHESIS_TOLERANCE_NS = 0.2
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _integer(value: Any, label: str, *, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", f"{label} is invalid")
    return value


def _runtime_endpoint_list(value: Any, width: int, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) != width
        or any(not isinstance(item, str) or not item for item in value)
    ):
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", f"{label} is incomplete")
    return tuple(value)


def _classify_bundle(bundle: Mapping[str, Any]) -> str:
    recovery = bundle.get("recovery")
    instances = bundle.get("instances")
    plans = bundle.get("plans")
    if not isinstance(recovery, Mapping) or not isinstance(instances, list) or not instances:
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "recovery/instance inventory is absent")
    if not isinstance(plans, list) or not plans:
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "module plan inventory is absent")
    operator_class = str(recovery.get("operator_class") or "")
    if operator_class not in {
        "separately_quantized_constant_product_sum",
        "registered_linear_sum",
    }:
        fail(
            "PRODUCTION_PRODUCT_SUM_CLASS_UNSUPPORTED",
            f"unsupported recovered operator class {operator_class or '<missing>'}",
        )
    bank_count = recovery.get("bank_count")
    if isinstance(bank_count, int) and not isinstance(bank_count, bool) and bank_count > 1:
        return "complete_registered_bank"
    if recovery.get("complete_sequential_write_mode") == "disabled":
        return "fir_like_registered_data_cone"
    return "complete_registered_bank"


def _plan_for_instance(
    *,
    source_sha256: str,
    plan: Mapping[str, Any],
    instance: Mapping[str, Any],
    instance_index: int,
    bundle_digest: str,
) -> RegisteredOperatorPlan:
    width = _integer(plan.get("data_width"), "data_width", low=1)
    roles = plan.get("input_roles")
    widths = plan.get("input_widths")
    signedness = plan.get("input_signed")
    raw_terms = plan.get("terms")
    raw_groups = plan.get("groups")
    if (
        not isinstance(roles, list)
        or not isinstance(widths, list)
        or not isinstance(signedness, list)
        or not isinstance(raw_terms, list)
        or not isinstance(raw_groups, list)
        or not roles
        or len(roles) != len(widths) != len(signedness)
    ):
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "plan operands are incomplete")
    if len(roles) != len(widths) or len(roles) != len(signedness):
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "plan operand vectors disagree")
    if any(not isinstance(item, bool) for item in signedness):
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "operand signedness is unresolved")
    input_d = instance.get("input_d_nets")
    input_ce = instance.get("input_ce_nets")
    if not isinstance(input_d, Mapping) or not isinstance(input_ce, Mapping):
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "registered input stage mapping is absent")

    words: list[WordBoundary] = []
    registered: list[RegisteredInputWord] = []
    role_to_word: dict[str, WordBoundary] = {}
    controls: list[ControlSemantics] = []
    clock = str(instance.get("clock_net") or "")
    if not clock:
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "instance clock is absent")
    for ordinal, (role, raw_width, signed) in enumerate(zip(roles, widths, signedness, strict=True)):
        if not isinstance(role, str) or not role:
            fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "input role is invalid")
        input_width = _integer(raw_width, f"{role}.width", low=1)
        endpoints = _runtime_endpoint_list(input_d.get(role), input_width, f"{role}.input_d_nets")
        word = WordBoundary(
            f"instance_{instance_index}_input_{ordinal}", input_width, signed, 0,
            endpoints, 0,
        )
        words.append(word)
        registered.append(RegisteredInputWord(word, "recovered_incumbent_D_CE_boundary"))
        role_to_word[role] = word
        ce = str(input_ce.get(role) or "")
        if not ce:
            fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", f"{role} CE is absent")
        controls.append(ControlSemantics(
            f"instance_{instance_index}_input_control_{ordinal}",
            ClockIdentity(clock, "recovered_net_identity", "posedge"),
            ClockEnableSemantics(ce, "high", "sample", "hold", "recovered_D_CE_mapping"),
            ResetSetSemantics("none", None, "high", None, "not_applicable", "recovered_init_zero"),
            0, "hold", "none", "none", "preserved", "none",
        ))

    term_to_group: dict[int, tuple[int, Mapping[str, Any]]] = {}
    for group_index, group in enumerate(raw_groups):
        if not isinstance(group, Mapping):
            fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "quantization group is malformed")
        ordinals = group.get("term_ordinals")
        if not isinstance(ordinals, list) or not ordinals:
            fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "quantization group is empty")
        for ordinal in ordinals:
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or not 0 <= ordinal < len(raw_terms)
                or ordinal in term_to_group
            ):
                fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "term/group coverage is invalid")
            term_to_group[ordinal] = (group_index, group)
    if set(term_to_group) != set(range(len(raw_terms))):
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "groups do not cover every term exactly once")

    terms: list[QuantizedProductTerm] = []
    losses: list[NumericLossBoundary] = []
    group_terms: dict[int, list[str]] = defaultdict(list)
    for ordinal, raw_term in enumerate(raw_terms):
        if (
            not isinstance(raw_term, Mapping)
            or raw_term.get("kind") not in {"fixed_product_slice", "passthrough"}
        ):
            fail(
                "PRODUCTION_PRODUCT_SUM_TERM_UNSUPPORTED",
                "production backend requires linear arithmetic terms",
            )
        source_role = str(raw_term.get("source_role") or "")
        if source_role not in role_to_word:
            fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "term source role is unknown")
        group_index, group = term_to_group[ordinal]
        shift = _integer(group.get("common_shift"), "group.common_shift")
        output_width = _integer(group.get("output_width"), "group.output_width", low=1)
        data_width = _integer(raw_term.get("data_width"), "term.data_width", low=1)
        if raw_term["kind"] == "fixed_product_slice":
            coefficient_width = _integer(
                raw_term.get("coefficient_width"),
                "term.coefficient_width",
                low=1,
            )
            coefficient = raw_term.get("coefficient")
            if isinstance(coefficient, bool) or not isinstance(coefficient, int):
                fail(
                    "PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE",
                    "term coefficient is invalid",
                )
            product_width = data_width + coefficient_width
        else:
            # A replay-proved passthrough is the exact linear term 1*x.  Keep
            # its native width in the semantic contract; the RTL backend uses
            # a register rather than manufacturing a multiply-by-one DSP.
            coefficient = 1
            coefficient_width = 2
            product_width = data_width + coefficient_width
        extension = (
            "sign_extend" if shift + output_width > product_width else "none"
        )
        term_id = f"instance_{instance_index}_term_{ordinal}"
        loss = NumericLossBoundary(
            f"instance_{instance_index}_loss_{ordinal}", term_id,
            product_width, output_width, shift, extension, "modular_wrap", "none",
            "discard_low_bits" if shift else "none", 0,
        )
        losses.append(loss)
        terms.append(QuantizedProductTerm(
            term_id, role_to_word[source_role].word_id, data_width,
            bool(raw_term.get("data_signed")), coefficient, coefficient_width,
            coefficient < 0, product_width, output_width, True,
            loss.boundary_id, 1,
        ))
        group_terms[group_index].append(term_id)

    groups = []
    for group_index, raw_group in enumerate(raw_groups):
        ids = tuple(group_terms[group_index])
        group_loss = NumericLossBoundary(
            f"instance_{instance_index}_group_loss_{group_index}",
            f"instance_{instance_index}_group_{group_index}",
            width, width, 0, "none", "modular_wrap", "none", "none", 0,
        )
        losses.append(group_loss)
        groups.append(ModularAccumulationGroup(
            f"instance_{instance_index}_group_{group_index}", ids, width, width,
            (
                AccumulationOrder.AFTER_TERM_LOSS
                if len(ids) == 1
                else AccumulationOrder.BEFORE_GROUP_LOSS
            ),
            "modular_wrap", group_loss.boundary_id,
            ("REGARITH_PRODUCT_SUM_V1:recovered_group_replay",),
        ))

    outputs = _runtime_endpoint_list(instance.get("output_nets"), width, "output_nets")
    output_word = WordBoundary(
        f"instance_{instance_index}_output", width, True, 0, outputs, 4,
    )
    words.append(output_word)
    product_ce = str(instance.get("product_ce_net") or "")
    if not product_ce:
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "product CE is absent")
    product_control = ControlSemantics(
        f"instance_{instance_index}_product_control",
        ClockIdentity(clock, "recovered_net_identity", "posedge"),
        ClockEnableSemantics(product_ce, "high", "update", "hold", "recovered_product_CE"),
        ResetSetSemantics("none", None, "high", None, "not_applicable", "recovered_init_zero"),
        0, "hold", "none", "none", "preserved", "none",
    )
    controls.append(product_control)
    stages = (
        RegisterStage(
            f"instance_{instance_index}_input_stage", 1,
            tuple(word.word_id for word in role_to_word.values()),
            tuple(word.word_id for word in role_to_word.values()),
            controls[0].control_id, "registered_input_mapping",
            ("recovered_D_CE_boundary",),
        ),
        RegisterStage(
            f"instance_{instance_index}_product_stage", 2,
            tuple(word.word_id for word in role_to_word.values()),
            (output_word.word_id,), product_control.control_id,
            "REGARITH_PRODUCT_SUM_V1",
            ("registered_full_width_products", "separate_loss_boundaries"),
        ),
        RegisterStage(
            f"instance_{instance_index}_accumulation_stage", 3,
            tuple(word.word_id for word in role_to_word.values()),
            (output_word.word_id,), product_control.control_id,
            "registered_modular_accumulation", ("wrap_width_preserved",),
        ),
        RegisterStage(
            f"instance_{instance_index}_preserved_output_stage", 4,
            (output_word.word_id,), (output_word.word_id,), product_control.control_id,
            "preserve_target_register_v1", ("observable_write_shell_preserved",),
        ),
    )
    candidate_id = f"production-product-sum:{bundle_digest[:16]}:{instance_index}"
    return RegisteredOperatorPlan(
        SCHEMA_VERSION, PlanKind.QUANTIZED_PRODUCT_SUM, source_sha256, candidate_id,
        tuple(words), tuple(registered), tuple(terms), tuple(losses), tuple(groups),
        tuple(controls),
        RegisterStageSchedule(
            tuple(CycleAlignment(word.word_id, 0, "incumbent_registered_D") for word in role_to_word.values()),
            (CycleAlignment(output_word.word_id, 4, "preserved_observable_write"),),
            stages,
        ),
        OutputShell(
            (output_word.word_id,), "preserve_target_register_v1", (),
            ("output_control_and_cycle_preserved",),
        ),
        ReplaceableOldCone(
            (str(instance.get("replaced_target_node_id") or f"instance_{instance_index}"),),
            "closed", ("old_output_driver_disconnected",),
        ),
        RetainedSideConsumers((), "complete", ("no_unaccounted_side_consumers",)),
        ExpectedWallCoverage("runtime_recovered_complete_family", 1, 1_000_000, bundle_digest),
    ).validate()


def adapt_generating_bundle_to_ir(
    bundle_value: Mapping[str, Any], *, source_artifact_sha256: str
) -> dict[str, Any]:
    """Validate a freshly recovered bundle and bind every instance to the IR."""
    if _SHA256.fullmatch(source_artifact_sha256) is None:
        fail("PRODUCTION_SOURCE_IDENTITY_INVALID", "source identity must be a lowercase SHA-256")
    try:
        bundle = validate_registered_bridge_bundle(dict(bundle_value))
    except (TypeError, ValueError) as exc:
        fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", str(exc))
    bundle_digest = object_digest(bundle)
    # The bridge validator intentionally returns only execution-authority
    # fields.  Classification metadata remains read-only evidence in the raw
    # recovered bundle and is never used for wiring.
    kind = _classify_bundle(bundle_value)
    plans_by_name = {str(row["module_name"]): row for row in bundle["plans"]}
    plans = []
    for index, instance in enumerate(bundle["instances"]):
        plan = plans_by_name.get(str(instance.get("module_name") or ""))
        if plan is None:
            fail("PRODUCTION_BUNDLE_CONTRACT_INCOMPLETE", "instance module has no recovered plan")
        plans.append(_plan_for_instance(
            source_sha256=source_artifact_sha256,
            plan=plan,
            instance=instance,
            instance_index=index,
            bundle_digest=bundle_digest,
        ))
    proofs = [screen_and_prove(plan) for plan in plans]
    rejected = [
        proof for proof in proofs
        if proof.get("status") != "success"
        or (proof.get("proof") or {}).get("status") != "proved"
    ]
    if rejected:
        reasons = [
            reason.get("reason_code", "PROOF_OBLIGATION_FAILED")
            for proof in rejected
            for reason in (proof.get("failure_reasons") or [])
        ]
        fail(
            "REGARITH_COMPLETE_FAMILY_PROOF_FAILED",
            "one or more recovered instance plans failed exact proof: "
            + ",".join(reasons or ["PROOF_OBLIGATION_FAILED"]),
        )
    result = {
        "schema_version": BACKEND_SCHEMA_VERSION,
        "backend_id": BACKEND_ID,
        "source_artifact_sha256": source_artifact_sha256,
        "bundle_kind": kind,
        "bundle_digest": bundle_digest,
        "instance_count": len(bundle["instances"]),
        "module_count": len(bundle["plans"]),
        "plan_digests": [plan.plan_digest() for plan in plans],
        "plans": [json.loads(plan.to_canonical_json()) for plan in plans],
        "proof_result_digests": [proof["result_digest"] for proof in proofs],
        "proofs": proofs,
        "proof_template_id": "REGARITH_COMPLETE_FAMILY_PRODUCT_SUM_V1",
        "mutation_allowed": False,
    }
    result["result_digest"] = object_digest(result)
    return result


def render_production_product_sum(
    bundle_value: Mapping[str, Any], *, source_artifact_sha256: str
) -> dict[str, Any]:
    """Render fresh RTL from an exact current-DCP IR adaptation."""
    adapted = adapt_generating_bundle_to_ir(
        bundle_value, source_artifact_sha256=source_artifact_sha256
    )
    bundle = validate_registered_bridge_bundle(dict(bundle_value))
    modules = []
    for raw_plan in bundle["plans"]:
        verilog = render_registered_bridge_verilog(raw_plan)
        modules.append({
            "module_name": raw_plan["module_name"],
            "verilog": verilog,
            "verilog_sha256": _sha256_text(verilog),
        })
    placement_manifest = {
        "kind": "runtime_global_implementation_manifest",
        "source_artifact_sha256": source_artifact_sha256,
        "bundle_digest": adapted["bundle_digest"],
        "coordinate_constraints": [],
        "placement_authority": "runtime_consumer_anchored_relative_geometry",
        "relative_geometry": build_product_sum_relative_placement_manifest(bundle),
        "opt_directive": "ExploreWithRemap",
        "place_directive": "ExtraTimingOpt",
        "pre_route_phys_directive": "AggressiveExplore",
        "route_directive": "NoTimingRelaxation",
    }
    placement_manifest["manifest_digest"] = object_digest(placement_manifest)
    result = {
        **{key: value for key, value in adapted.items() if key != "plans"},
        "modules": modules,
        "ir_plans": adapted["plans"],
        "placement_manifest": placement_manifest,
        "insert_jython": render_registered_bridge_insert_jython(bundle),
        "connect_tcl": render_registered_bridge_connect_tcl(bundle),
        "ooc_gate_required": True,
        "mutation_allowed": False,
    }
    result["result_digest"] = object_digest(result)
    return result


def build_ooc_timing_gate_request(
    emission: Mapping[str, Any], *, period_ns: float = DEFAULT_OOC_PERIOD_NS
) -> dict[str, Any]:
    if (
        not isinstance(period_ns, (int, float))
        or isinstance(period_ns, bool)
        or not 0.5 <= float(period_ns) <= 10.0
    ):
        fail("OOC_GATE_PERIOD_INVALID", "OOC period is outside 0.5..10.0 ns")
    modules = emission.get("modules") if isinstance(emission, Mapping) else None
    if not isinstance(modules, list) or not modules:
        fail("OOC_GATE_EMISSION_INVALID", "emission has no modules")
    request = {
        "schema_version": OOC_GATE_SCHEMA_VERSION,
        "backend_id": BACKEND_ID,
        "emission_result_digest": emission.get("result_digest"),
        "period_ns": float(period_ns),
        "required_module_sha256": {
            str(row["module_name"]): str(row["verilog_sha256"]) for row in modules
        },
        "requirements": {
            # OOC synthesis is an estimate before physical optimization.  A
            # small tool-noise tolerance is applied to the period recovered
            # from the current source DCP; no prior-run timing enters the gate.
            "setup_wns_ns_min": -OOC_SYNTHESIS_TOLERANCE_NS,
            "internal_data_path_ns_max": (
                float(period_ns) + OOC_SYNTHESIS_TOLERANCE_NS
            ),
            "unconstrained_internal_path_count": 0,
        },
        "mutation_allowed": False,
    }
    request["request_digest"] = object_digest(request)
    return request


def render_ooc_synth_tcl(
    *, module_name: str, rtl_path: str, output_dcp: str, report_path: str,
    part: str, period_ns: float = DEFAULT_OOC_PERIOD_NS,
) -> str:
    """Render one identity-free OOC synthesis/timing script.

    The runner owns paths and the part recovered from the current checkpoint;
    no benchmark identity or placement coordinate enters this script.
    """
    if not module_name or any(token in module_name for token in "{}\n\r"):
        fail("OOC_GATE_MODULE_INVALID", "module name is unsafe")
    if not part or any(token in part for token in "{}\n\r"):
        fail("OOC_GATE_PART_INVALID", "runtime part is unsafe")
    for value in (rtl_path, output_dcp, report_path):
        if not value or any(token in value for token in "{}\n\r"):
            fail("OOC_GATE_PATH_INVALID", "OOC runner path is unsafe")
    return "\n".join([
        f"read_verilog {{{rtl_path}}}",
        f"synth_design -mode out_of_context -top {module_name} -part {part}",
        f"create_clock -period {float(period_ns):.6f} -name regarith_ooc [get_ports clk]",
        "set_input_delay 0.000 -clock regarith_ooc [all_inputs]",
        "set_output_delay 0.000 -clock regarith_ooc [all_outputs]",
        "opt_design -directive ExploreWithRemap",
        f"report_timing_summary -delay_type min_max -max_paths 50 -file {{{report_path}}}",
        f"write_checkpoint -force {{{output_dcp}}}",
        "exit", "",
    ])


def execute_ooc_timing_gate(
    emission: Mapping[str, Any],
    runner: Callable[[dict[str, Any], float], Mapping[str, Any]],
    *,
    period_ns: float = DEFAULT_OOC_PERIOD_NS,
) -> dict[str, Any]:
    """Execute and validate the mandatory gate through a tool-owned callback."""
    request = build_ooc_timing_gate_request(emission, period_ns=period_ns)
    rows = []
    for module in emission["modules"]:
        measured = runner(dict(module), float(period_ns))
        rows.append({
            "module_name": module["module_name"],
            "verilog_sha256": module["verilog_sha256"],
            "setup_wns_ns": measured.get("setup_wns_ns"),
            "internal_data_path_ns": measured.get("internal_data_path_ns"),
            "unconstrained_internal_path_count": measured.get(
                "unconstrained_internal_path_count"
            ),
            "ooc_dcp_sha256": measured.get("ooc_dcp_sha256"),
            "timing_report_sha256": measured.get("timing_report_sha256"),
        })
    raw = {
        "schema_version": OOC_GATE_SCHEMA_VERSION,
        "request_digest": request["request_digest"],
        "status": "passed",
        "modules": rows,
    }
    return validate_ooc_timing_gate(request, raw)


def validate_ooc_timing_gate(
    request: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a typed pass record or fail closed before integrated P&R."""
    if request.get("schema_version") != OOC_GATE_SCHEMA_VERSION:
        fail("OOC_GATE_REQUEST_INVALID", "OOC gate request schema is unsupported")
    raw_request = dict(request)
    claimed_request = raw_request.pop("request_digest", None)
    if not isinstance(claimed_request, str) or object_digest(raw_request) != claimed_request:
        fail("OOC_GATE_REQUEST_INVALID", "OOC gate request digest mismatch")
    if (
        not isinstance(result, Mapping)
        or result.get("schema_version") != OOC_GATE_SCHEMA_VERSION
        or result.get("request_digest") != claimed_request
        or result.get("status") not in {"passed", "failed"}
    ):
        fail("OOC_GATE_RESULT_INVALID", "OOC gate result is absent, stale, or malformed")
    modules = result.get("modules")
    required = request["required_module_sha256"]
    if not isinstance(modules, list) or len(modules) != len(required):
        fail("OOC_GATE_RESULT_INVALID", "OOC result does not cover every module")
    observed = {}
    failures = []
    for row in modules:
        if not isinstance(row, Mapping):
            fail("OOC_GATE_RESULT_INVALID", "OOC module row is malformed")
        name = str(row.get("module_name") or "")
        if name not in required or name in observed or row.get("verilog_sha256") != required[name]:
            fail("OOC_GATE_RESULT_INVALID", "OOC module identity is stale or duplicated")
        wns = row.get("setup_wns_ns")
        delay = row.get("internal_data_path_ns")
        unconstrained = row.get("unconstrained_internal_path_count")
        ooc_sha = row.get("ooc_dcp_sha256")
        report_sha = row.get("timing_report_sha256")
        if (
            isinstance(wns, bool) or not isinstance(wns, (int, float))
            or isinstance(delay, bool) or not isinstance(delay, (int, float))
            or isinstance(unconstrained, bool) or not isinstance(unconstrained, int)
            or not isinstance(ooc_sha, str) or _SHA256.fullmatch(ooc_sha) is None
            or not isinstance(report_sha, str) or _SHA256.fullmatch(report_sha) is None
        ):
            fail("OOC_GATE_RESULT_INVALID", "OOC timing metrics are incomplete")
        if float(wns) < request["requirements"]["setup_wns_ns_min"]:
            failures.append(f"{name}:setup_wns_ns={wns}")
        if float(delay) > request["requirements"]["internal_data_path_ns_max"]:
            failures.append(f"{name}:internal_data_path_ns={delay}")
        if unconstrained != 0:
            failures.append(f"{name}:unconstrained_internal_path_count={unconstrained}")
        observed[name] = dict(row)
    if set(observed) != set(required):
        fail("OOC_GATE_RESULT_INVALID", "OOC result module coverage differs")
    if result.get("status") != "passed" or failures:
        fail(
            "REGARITH_OOC_TIMING_GATE_FAILED",
            "integrated P&R forbidden: " + (", ".join(failures) or "runner reported failure"),
        )
    passed = {
        "schema_version": OOC_GATE_SCHEMA_VERSION,
        "status": "passed",
        "request_digest": claimed_request,
        "module_count": len(observed),
        "period_ns": request["period_ns"],
        "module_result_digests": {
            name: object_digest(observed[name]) for name in sorted(observed)
        },
        "integrated_mutation_authorized": True,
    }
    passed["gate_digest"] = object_digest(passed)
    return passed


def authorize_integrated_product_sum_emission(
    emission: Mapping[str, Any], gate: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind the only mutation-capable envelope to a passing OOC gate."""
    request = build_ooc_timing_gate_request(
        emission, period_ns=float(gate.get("period_ns", DEFAULT_OOC_PERIOD_NS))
    )
    # A caller supplies the already validated pass record, not raw timing rows.
    if (
        gate.get("schema_version") != OOC_GATE_SCHEMA_VERSION
        or gate.get("status") != "passed"
        or gate.get("request_digest") != request["request_digest"]
        or gate.get("integrated_mutation_authorized") is not True
    ):
        fail("REGARITH_OOC_TIMING_GATE_REQUIRED", "a matching passing OOC gate is mandatory")
    raw_gate = dict(gate)
    claimed_gate = raw_gate.pop("gate_digest", None)
    if not isinstance(claimed_gate, str) or object_digest(raw_gate) != claimed_gate:
        fail("REGARITH_OOC_TIMING_GATE_REQUIRED", "OOC pass record digest mismatch")
    authorized = dict(emission)
    authorized.update({
        "ooc_gate_digest": claimed_gate,
        "mutation_allowed": True,
        "authority": "integrated_candidate_measurement_only",
    })
    authorized["authorization_digest"] = object_digest(authorized)
    return authorized


def dispatch_product_sum_backend(
    plan_value: Any,
    *,
    recovered_bundle: Mapping[str, Any] | None = None,
    source_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Typed dispatch used by ``registered_arithmetic_cone_v1``."""
    plan = plan_input(plan_value)
    if PlanKind(plan.plan_kind) != PlanKind.QUANTIZED_PRODUCT_SUM:
        fail("REGARITH_PRODUCT_SUM_PLAN_KIND_UNSUPPORTED", "backend accepts only product-sum plans")
    orders = {
        group.accumulation_order.value
        if isinstance(group.accumulation_order, AccumulationOrder)
        else str(group.accumulation_order)
        for group in plan.accumulation_groups
    }
    if AccumulationOrder.PARTITIONED_FULL_PRECISION_THEN_SHIFT.value in orders:
        fail(
            "REGARITH_PARTITIONED_PRODUCT_SUM_PRODUCTION_BACKEND_UNSUPPORTED",
            "partitioned-order residue is not a complete-family production action",
        )
    if recovered_bundle is None:
        fail(
            "REGARITH_COMPLETE_FAMILY_RECOVERY_REQUIRED",
            "product-sum dispatch requires a freshly recovered complete-family bundle",
        )
    source_sha = source_artifact_sha256 or plan.source_artifact_sha256
    emission = render_production_product_sum(
        recovered_bundle, source_artifact_sha256=source_sha
    )
    if plan.source_artifact_sha256 != source_sha:
        fail("REGARITH_PRODUCT_SUM_SOURCE_MISMATCH", "plan and bundle source identities differ")
    return emission


__all__ = [
    "BACKEND_ID", "BACKEND_SCHEMA_VERSION", "OOC_GATE_SCHEMA_VERSION",
    "DEFAULT_OOC_PERIOD_NS", "OOC_SYNTHESIS_TOLERANCE_NS",
    "adapt_generating_bundle_to_ir",
    "render_production_product_sum", "build_ooc_timing_gate_request",
    "render_ooc_synth_tcl", "execute_ooc_timing_gate",
    "validate_ooc_timing_gate", "authorize_integrated_product_sum_emission",
    "dispatch_product_sum_backend",
]
