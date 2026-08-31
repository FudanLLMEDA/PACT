"""Hash-bound orchestration references for generic semantic rebuild actions.

The manifest deliberately contains no functional parameters. Widths, constants,
latencies, slices, and memory modes remain inside freshly recovered facts and
their replayable authorizations; orchestration can only reference those objects.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .operator_proof_current import (
    OPERATOR_TEMPLATE_BINDINGS,
    _digest,
    verify_rewrite_authorization,
)


MICRO_ACTION_REF_VERSION = 1
SEMANTIC_REBUILD_MANIFEST_VERSION = 1
SEMANTIC_EXECUTION_BUNDLE_VERSION = 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_ID_RE = re.compile(r"[a-z][a-z0-9_-]*:[0-9a-f]{20,64}")
_NODE_ID_RE = re.compile(r"micro:[0-9a-f]{24}")
_COUPLING_MODES = {"independent", "atomic_chain_member"}
_MICRO_FIELDS = {
    "kind",
    "version",
    "node_id",
    "template",
    "candidate_id",
    "source",
    "authorization_id",
    "depends_on",
    "coupling",
    "semantic_parameter_policy",
    "authority",
    "mutation_eligible",
}
_MANIFEST_FIELDS = {
    "kind",
    "version",
    "input_dcp_sha256",
    "actions",
    "execution_order",
    "semantic_parameter_policy",
    "external_semantic_parameters",
    "authority",
    "mutation_eligible",
    "manifest_sha256",
}


class SemanticRebuildError(ValueError):
    """Raised when a semantic action reference or bundle is not fail-closed."""


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SemanticRebuildError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _authorization_template(authorization: dict[str, Any]) -> str:
    plan = authorization.get("plan")
    rewrite_kind = authorization.get("rewrite_kind")
    proof_template = plan.get("template") if isinstance(plan, dict) else None
    matches = [
        template
        for template, binding in OPERATOR_TEMPLATE_BINDINGS.items()
        if binding["rewrite_kind"] == rewrite_kind
        and binding["proof_template"] == proof_template
    ]
    if len(matches) != 1:
        raise SemanticRebuildError(
            "authorization does not identify exactly one registered template"
        )
    return matches[0]


def _normalized_dependencies(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise SemanticRebuildError("depends_on must be a sequence of micro node IDs")
    result = []
    for dependency in value:
        if not isinstance(dependency, str) or _NODE_ID_RE.fullmatch(dependency) is None:
            raise SemanticRebuildError("depends_on contains an invalid micro node ID")
        if dependency in result:
            raise SemanticRebuildError("depends_on contains a duplicate micro node ID")
        result.append(dependency)
    return sorted(result)


def _micro_identity_payload(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": action["kind"],
        "version": action["version"],
        "template": action["template"],
        "candidate_id": action["candidate_id"],
        "source": dict(action["source"]),
        "authorization_id": action["authorization_id"],
        "semantic_parameter_policy": action["semantic_parameter_policy"],
        "authority": action["authority"],
        "mutation_eligible": action["mutation_eligible"],
    }


def build_semantic_micro_action_ref(
    facts: dict[str, Any],
    authorization: dict[str, Any],
    *,
    depends_on: Iterable[str] = (),
    coupling: str = "independent",
) -> dict[str, Any]:
    """Reference one freshly recovered authorization without copying its spec."""
    if not isinstance(facts, dict) or not isinstance(authorization, dict):
        raise SemanticRebuildError("facts and authorization must be objects")
    source_dcp = _require_sha256(facts.get("design_sha256"), "facts design_sha256")
    source_region = _require_sha256(
        facts.get("region_sha256"), "facts region_sha256"
    )
    candidate_id = facts.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise SemanticRebuildError("facts candidate_id is invalid")
    if facts.get("fresh_rediscovery") is not True:
        raise SemanticRebuildError("facts are not a fresh DCP rediscovery")
    if facts.get("mutation_eligible") is not False:
        raise SemanticRebuildError("facts unexpectedly grant mutation authority")
    authorization_id = _require_sha256(
        authorization.get("certificate_id"), "authorization certificate_id"
    )
    if not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=source_dcp,
        expected_region_sha256=source_region,
    ):
        raise SemanticRebuildError("authorization does not replay against facts")
    if coupling not in _COUPLING_MODES:
        raise SemanticRebuildError("coupling is not a registered orchestration mode")
    action = {
        "kind": "semantic_micro_action_ref",
        "version": MICRO_ACTION_REF_VERSION,
        "template": _authorization_template(authorization),
        "candidate_id": candidate_id,
        "source": {
            "dcp_sha256": source_dcp,
            "region_sha256": source_region,
        },
        "authorization_id": authorization_id,
        "depends_on": _normalized_dependencies(list(depends_on)),
        "coupling": coupling,
        "semantic_parameter_policy": "fresh_recovered_facts_only",
        "authority": "orchestration_reference_only",
        "mutation_eligible": False,
    }
    action["node_id"] = "micro:" + _digest(_micro_identity_payload(action))[:24]
    return action


def validate_semantic_micro_action_ref(
    value: Any,
    *,
    expected_input_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the closed reference schema without needing private facts."""
    if not isinstance(value, dict) or set(value) != _MICRO_FIELDS:
        raise SemanticRebuildError("micro action reference has an unsupported schema")
    if (
        value.get("kind") != "semantic_micro_action_ref"
        or value.get("version") != MICRO_ACTION_REF_VERSION
        or value.get("semantic_parameter_policy") != "fresh_recovered_facts_only"
        or value.get("authority") != "orchestration_reference_only"
        or value.get("mutation_eligible") is not False
    ):
        raise SemanticRebuildError("micro action reference authority is invalid")
    template = value.get("template")
    if template not in OPERATOR_TEMPLATE_BINDINGS:
        raise SemanticRebuildError("micro action template is not registered")
    candidate_id = value.get("candidate_id")
    if not isinstance(candidate_id, str) or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        raise SemanticRebuildError("micro action candidate_id is invalid")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"dcp_sha256", "region_sha256"}:
        raise SemanticRebuildError("micro action source has an unsupported schema")
    source_dcp = _require_sha256(source.get("dcp_sha256"), "micro source DCP")
    _require_sha256(source.get("region_sha256"), "micro source region")
    if expected_input_sha256 is not None and source_dcp != _require_sha256(
        expected_input_sha256, "expected_input_sha256"
    ):
        raise SemanticRebuildError("micro action is bound to a different input DCP")
    _require_sha256(value.get("authorization_id"), "micro authorization_id")
    dependencies = _normalized_dependencies(value.get("depends_on"))
    if value.get("coupling") not in _COUPLING_MODES:
        raise SemanticRebuildError("micro action coupling mode is invalid")
    expected_node_id = "micro:" + _digest(_micro_identity_payload(value))[:24]
    if value.get("node_id") != expected_node_id:
        raise SemanticRebuildError("micro action identity digest does not replay")
    normalized = dict(value)
    normalized["source"] = dict(source)
    normalized["depends_on"] = dependencies
    return normalized


def _topological_order(actions: list[dict[str, Any]]) -> list[str]:
    by_id = {action["node_id"]: action for action in actions}
    if len(by_id) != len(actions):
        raise SemanticRebuildError("semantic manifest contains duplicate node IDs")
    for action in actions:
        if action["node_id"] in action["depends_on"]:
            raise SemanticRebuildError("semantic micro action depends on itself")
        missing = sorted(set(action["depends_on"]) - set(by_id))
        if missing:
            raise SemanticRebuildError(
                f"semantic micro action has missing dependencies: {missing}"
            )
    pending = {node_id: set(action["depends_on"]) for node_id, action in by_id.items()}
    order = []
    while pending:
        ready = sorted(node_id for node_id, deps in pending.items() if not deps)
        if not ready:
            raise SemanticRebuildError("semantic action dependency graph contains a cycle")
        order.extend(ready)
        for node_id in ready:
            pending.pop(node_id)
        for dependencies in pending.values():
            dependencies.difference_update(ready)
    return order


def build_semantic_rebuild_manifest(
    input_dcp_sha256: str,
    actions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic DAG that cannot carry external semantic knobs."""
    source = _require_sha256(input_dcp_sha256, "input_dcp_sha256")
    normalized = [
        validate_semantic_micro_action_ref(action, expected_input_sha256=source)
        for action in actions
    ]
    if not normalized:
        raise SemanticRebuildError("semantic rebuild manifest requires an action")
    normalized.sort(key=lambda action: action["node_id"])
    order = _topological_order(normalized)
    manifest = {
        "kind": "semantic_rebuild_manifest",
        "version": SEMANTIC_REBUILD_MANIFEST_VERSION,
        "input_dcp_sha256": source,
        "actions": normalized,
        "execution_order": order,
        "semantic_parameter_policy": "fresh_recovered_facts_only",
        "external_semantic_parameters": {},
        "authority": "orchestration_only",
        "mutation_eligible": False,
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def validate_semantic_rebuild_manifest(value: Any) -> dict[str, Any]:
    """Replay manifest identity, seed binding, and deterministic DAG ordering."""
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise SemanticRebuildError("semantic rebuild manifest has an unsupported schema")
    if (
        value.get("kind") != "semantic_rebuild_manifest"
        or value.get("version") != SEMANTIC_REBUILD_MANIFEST_VERSION
        or value.get("semantic_parameter_policy") != "fresh_recovered_facts_only"
        or value.get("external_semantic_parameters") != {}
        or value.get("authority") != "orchestration_only"
        or value.get("mutation_eligible") is not False
    ):
        raise SemanticRebuildError("semantic rebuild manifest authority is invalid")
    source = _require_sha256(value.get("input_dcp_sha256"), "input_dcp_sha256")
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise SemanticRebuildError("semantic rebuild manifest actions are unavailable")
    actions = [
        validate_semantic_micro_action_ref(action, expected_input_sha256=source)
        for action in raw_actions
    ]
    if actions != sorted(actions, key=lambda action: action["node_id"]):
        raise SemanticRebuildError("semantic rebuild actions are not canonical")
    order = _topological_order(actions)
    if value.get("execution_order") != order:
        raise SemanticRebuildError("semantic rebuild execution order is stale")
    payload = dict(value)
    claimed = payload.pop("manifest_sha256", None)
    _require_sha256(claimed, "manifest_sha256")
    if _digest(payload) != claimed:
        raise SemanticRebuildError("semantic rebuild manifest digest does not replay")
    normalized = dict(value)
    normalized["actions"] = actions
    normalized["execution_order"] = order
    return normalized


def _dependency_ancestors(
    node_id: str, by_id: dict[str, dict[str, Any]]
) -> set[str]:
    result = set()
    pending = list(by_id[node_id]["depends_on"])
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(by_id[dependency]["depends_on"])
    return result


def build_semantic_rebuild_execution_bundle(
    manifest: dict[str, Any],
    resolutions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a closed DAG to run-owned memory proofs without serializing specs."""
    validated = validate_semantic_rebuild_manifest(manifest)
    actions = {action["node_id"]: action for action in validated["actions"]}
    raw_resolutions = list(resolutions)
    if len(raw_resolutions) != len(actions):
        raise SemanticRebuildError("execution resolution coverage is incomplete")
    by_node = {}
    cells = {}
    sites = {}
    nodes = []
    for resolution in raw_resolutions:
        if not isinstance(resolution, dict) or set(resolution) != {
            "node_id", "facts", "consumer_facts", "candidate"
        }:
            raise SemanticRebuildError("execution resolution has an unsupported schema")
        node_id = resolution.get("node_id")
        if node_id not in actions or node_id in by_node:
            raise SemanticRebuildError("execution resolution node is absent or duplicated")
        action = actions[node_id]
        if action["template"] != "memory_semantic_repack_v1":
            raise SemanticRebuildError("execution bundle has no registered micro emitter")
        facts = resolution["facts"]
        candidate = resolution["candidate"]
        if not isinstance(facts, dict) or not isinstance(candidate, dict):
            raise SemanticRebuildError("execution resolution proof objects are unavailable")
        authorization = candidate.get("authorization")
        if (
            facts.get("candidate_id") != action["candidate_id"]
            or facts.get("design_sha256") != action["source"]["dcp_sha256"]
            or facts.get("region_sha256") != action["source"]["region_sha256"]
            or not isinstance(authorization, dict)
            or authorization.get("certificate_id") != action["authorization_id"]
        ):
            raise SemanticRebuildError("execution resolution differs from its micro action")
        try:
            from .memory_action import build_memory_emission_manifests

            emission = build_memory_emission_manifests(
                facts, resolution["consumer_facts"], candidate
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticRebuildError(
                f"execution resolution memory proof failed: {exc}"
            ) from exc
        boundary = facts.get("boundary") or {}
        physical = boundary.get("physical") or {}
        cell = boundary.get("cell")
        site = physical.get("site")
        if not isinstance(cell, str) or not cell or not isinstance(site, str) or not site:
            raise SemanticRebuildError("execution resolution physical identity is absent")
        if cell in cells or site in sites:
            raise SemanticRebuildError("execution resolutions overlap a memory cell or site")
        cells[cell] = node_id
        sites[site] = node_id
        by_node[node_id] = resolution
        nodes.append({
            "node_id": node_id,
            "candidate_id": action["candidate_id"],
            "source_region_sha256": action["source"]["region_sha256"],
            "authorization_id": action["authorization_id"],
            "consumer_region_sha256": resolution["consumer_facts"]["region_sha256"],
            "placement_manifest_sha256": emission["placement"]["manifest_sha256"],
            "topology_manifest_sha256": emission["topology"]["manifest_sha256"],
        })
    cross_edges = []
    for producer_node, resolution in by_node.items():
        for edge in resolution["consumer_facts"]["dependency_edges"]:
            consumer_node = cells.get(edge.get("consumer_cell"))
            if consumer_node is None or consumer_node == producer_node:
                continue
            if producer_node not in _dependency_ancestors(consumer_node, actions):
                raise SemanticRebuildError(
                    "memory-to-memory edge is missing its DAG dependency"
                )
            if (
                actions[producer_node]["coupling"] != "atomic_chain_member"
                or actions[consumer_node]["coupling"] != "atomic_chain_member"
            ):
                raise SemanticRebuildError(
                    "memory-to-memory dependency is not atomically coupled"
                )
            cross_edges.append({
                "producer_node_id": producer_node,
                "consumer_node_id": consumer_node,
                "net_sha256": _digest(edge["net"]),
            })
    nodes.sort(key=lambda item: validated["execution_order"].index(item["node_id"]))
    cross_edges.sort(key=lambda item: (
        item["producer_node_id"], item["consumer_node_id"], item["net_sha256"]
    ))
    bundle = {
        "kind": "semantic_rebuild_execution_bundle",
        "version": SEMANTIC_EXECUTION_BUNDLE_VERSION,
        "input_dcp_sha256": validated["input_dcp_sha256"],
        "semantic_manifest_sha256": validated["manifest_sha256"],
        "nodes": nodes,
        "execution_order": list(validated["execution_order"]),
        "cross_action_edges": cross_edges,
        "transaction_policy": "single_source_atomic_commit",
        "rollback_policy": "delete_candidate_and_restore_source",
        "semantic_parameter_policy": "fresh_recovered_facts_only",
        "external_semantic_parameters": {},
        "authority": "pre_emission_orchestration_only",
        "mutation_eligible": False,
    }
    bundle["bundle_sha256"] = _digest(bundle)
    return bundle
