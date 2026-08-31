"""Multi-sibling registered closure and exact word-boundary extractor."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .common import FACTS_SCHEMA_VERSION, envelope, fail, object_digest, read_json


_SHA_RE = re.compile(r"[0-9a-f]{64}")
_FAMILY_FIELDS = {
    "candidate_family_id", "plan_kind", "words", "nodes", "terms", "losses",
    "accumulation_groups", "controls", "stages", "output_shell",
    "replaceable_old_cone", "retained_side_consumers", "expected_wall_coverage",
}


def _word(raw: Any, path: str) -> dict[str, Any]:
    required = {
        "word_id", "width", "signed", "lsb_index", "cycle_alignment",
        "registered", "register_semantics", "boundary_shape", "endpoints",
    }
    if not isinstance(raw, Mapping) or set(raw) != required:
        fail("CLOSURE_WORD_SCHEMA", "word boundary fields are missing or unknown", path)
    width = raw["width"]
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        fail("CLOSURE_WORD_WIDTH", "word width must be positive", path + ".width")
    endpoints = raw["endpoints"]
    if not isinstance(endpoints, list) or len(endpoints) != width:
        fail("INCOMPLETE_WORD_BOUNDARY", "every logical bit requires an explicit endpoint", path)
    normalized = []
    for ordinal, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, Mapping) or set(endpoint) != {"index", "endpoint_id", "kind"}:
            fail("NONINDEXED_WORD_BOUNDARY", "endpoint requires an explicit logical index", f"{path}.endpoints[{ordinal}]")
        if endpoint["index"] != ordinal or endpoint["kind"] not in {"net", "const_zero", "const_one"}:
            fail("SPARSE_WORD_BOUNDARY_UNRESOLVED", "sparse lanes must be explicit constants at their exact indices", f"{path}.endpoints[{ordinal}]")
        if not isinstance(endpoint["endpoint_id"], str) or not endpoint["endpoint_id"]:
            fail("INCOMPLETE_WORD_BOUNDARY", "endpoint identity is absent", f"{path}.endpoints[{ordinal}]")
        normalized.append(dict(endpoint))
    if raw["boundary_shape"] not in {"contiguous", "sparse_explicit"}:
        fail("UNKNOWN_WORD_BOUNDARY_SHAPE", "boundary shape is unsupported", path)
    result = dict(raw)
    result["endpoints"] = normalized
    return result


def _check_dag(nodes: Any, word_ids: set[str], path: str) -> list[dict[str, Any]]:
    if not isinstance(nodes, list) or not nodes:
        fail("CLOSURE_EMPTY", "closure node set is empty", path)
    normalized = []
    outputs = set()
    edges: dict[str, tuple[str, ...]] = {}
    for index, node in enumerate(nodes):
        node_path = f"{path}[{index}]"
        if not isinstance(node, Mapping) or set(node) != {"node_id", "output_word_id", "input_word_ids", "node_kind"}:
            fail("CLOSURE_NODE_SCHEMA", "closure node schema is invalid", node_path)
        if node["output_word_id"] not in word_ids or not isinstance(node["input_word_ids"], list):
            fail("CLOSURE_UNKNOWN_WORD", "node references an unknown word", node_path)
        if any(item not in word_ids for item in node["input_word_ids"]):
            fail("CLOSURE_UNKNOWN_WORD", "node input references an unknown word", node_path)
        output = str(node["output_word_id"])
        if output in outputs:
            fail("CLOSURE_AMBIGUOUS_PRODUCER", "word has multiple closure producers", node_path)
        outputs.add(output)
        edges[output] = tuple(node["input_word_ids"])
        normalized.append({**node, "input_word_ids": list(node["input_word_ids"])})
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(word: str) -> None:
        if word in visiting:
            fail("CLOSURE_CYCLE", "registered closure contains a cycle", path)
        if word in visited:
            return
        visiting.add(word)
        for child in edges.get(word, ()):
            visit(child)
        visiting.remove(word)
        visited.add(word)
    for output in sorted(edges):
        visit(output)
    return normalized


def _family(raw: Any, index: int) -> dict[str, Any]:
    path = f"facts.families[{index}]"
    if not isinstance(raw, Mapping) or set(raw) != _FAMILY_FIELDS:
        fail("CLOSURE_FAMILY_SCHEMA", "family fields are missing or unknown", path)
    candidate = raw["candidate_family_id"]
    if not isinstance(candidate, str) or not candidate:
        fail("CLOSURE_CANDIDATE_ID", "candidate family identity is absent", path)
    if not isinstance(raw["words"], list) or not raw["words"]:
        fail("CLOSURE_WORDS_EMPTY", "family has no word boundaries", path)
    words = [_word(item, f"{path}.words[{i}]") for i, item in enumerate(raw["words"])]
    word_ids = [item["word_id"] for item in words]
    if len(set(word_ids)) != len(word_ids):
        fail("CLOSURE_DUPLICATE_WORD", "word identities repeat", path)
    retained = raw["retained_side_consumers"]
    if not isinstance(retained, Mapping) or retained.get("complete") is not True:
        fail("INCOMPLETE_SIDE_CONSUMER_COVERAGE", "side-consumer inventory is incomplete", path)
    result = dict(raw)
    result["words"] = words
    result["nodes"] = _check_dag(raw["nodes"], set(word_ids), path + ".nodes")
    result["family_hash"] = object_digest(result)
    return result


def extract_registered_closures(facts: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(facts, Mapping) or set(facts) != {"schema_version", "source_artifact_sha256", "families"}:
        fail("CLOSURE_FACTS_SCHEMA", "top-level closure facts schema is unsupported")
    if facts["schema_version"] != FACTS_SCHEMA_VERSION:
        fail("CLOSURE_FACTS_VERSION", "facts schema version is unsupported")
    source = facts["source_artifact_sha256"]
    if not isinstance(source, str) or _SHA_RE.fullmatch(source) is None:
        fail("CLOSURE_SOURCE_ID", "source artifact SHA-256 is invalid")
    if not isinstance(facts["families"], list) or not facts["families"]:
        fail("CLOSURE_NO_FAMILIES", "no registered sibling families were supplied")
    families = [_family(raw, index) for index, raw in enumerate(facts["families"])]
    for family in families:
        family["source_artifact_sha256"] = source
        family["family_hash"] = object_digest({
            key: value for key, value in family.items() if key != "family_hash"
        })
    families.sort(key=lambda item: (item["candidate_family_id"], item["family_hash"]))
    return envelope(
        "multi_sibling_registered_closure_extractor",
        source_artifact_sha256=source,
        candidate_family_id=None,
        candidate_hash=object_digest(families),
        status="success",
        payload={
            "facts_schema_version": FACTS_SCHEMA_VERSION,
            "sibling_count": len(families),
            "siblings": families,
            "normalized_sibling_facts": [
                {
                    "candidate_family_id": item["candidate_family_id"],
                    "family_hash": item["family_hash"],
                    "plan_kind": item["plan_kind"],
                    "word_count": len(item["words"]),
                    "node_count": len(item["nodes"]),
                }
                for item in families
            ],
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts", type=Path)
    args = parser.parse_args()
    import json
    try:
        result = extract_registered_closures(read_json(args.facts))
    except Exception as exc:
        from .common import rejection_from_exception
        result = rejection_from_exception("multi_sibling_registered_closure_extractor", exc)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
