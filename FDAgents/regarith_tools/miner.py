"""Read-only current-DCP critical registered-family miner."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .common import TOOLS_SCHEMA_VERSION, envelope, fail, file_sha256, object_digest


def _default_dcp_loader(path: Path, edif_path: Path | None = None):
    # Import is deliberately delayed so fixture tests need no JVM.  Callers can
    # inject any loader returning either a RapidWright Design or pre-mined facts.
    repo_root = Path(__file__).resolve().parents[2]
    java_home = os.environ.get("REGARITH_JAVA_HOME") or os.environ.get("JAVA_HOME")
    if java_home:
        os.environ["JAVA_HOME"] = str(Path(java_home).expanduser().resolve())
    rapidwright_root = Path(
        os.environ.get("RAPIDWRIGHT_PATH", str(repo_root / "RapidWright"))
    )
    os.environ.setdefault("RAPIDWRIGHT_PATH", str(rapidwright_root))
    os.environ.setdefault(
        "CLASSPATH",
        f"{rapidwright_root / 'bin'}:{rapidwright_root / 'jars' / '*'}",
    )
    import rapidwright  # noqa: F401
    from com.xilinx.rapidwright.design import Design

    if edif_path is None:
        return Design.readCheckpoint(str(path))
    from java.nio.file import Paths

    return Design.readCheckpoint(
        Paths.get(str(path)), Paths.get(str(edif_path))
    )


def _mine_design(design: Any, source_sha: str, critical_paths: list | None) -> Mapping[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    rw_mcp = repo_root / "RapidWrightMCP"
    if str(rw_mcp) not in sys.path:
        sys.path.insert(0, str(rw_mcp))
    from operator_mining import mine_operator_structures

    return mine_operator_structures(
        design,
        critical_paths_data=critical_paths,
        min_family_size=3,
        max_families=16,
        max_motif_cells=8,
        design_sha256=source_sha,
    )


def _anonymous_summary(kind: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind", "source_register_count", "critical_path_count",
        "transition_signature", "sink_bus_width_estimate", "cone_cell_count",
        "carry_primitive_count", "lut_primitive_count", "occurrence_count",
        "sequence_length", "sequence_count", "primitive_sequence",
    }
    facts = {key: raw[key] for key in sorted(raw) if key in allowed}
    family_hash = object_digest({"kind": kind, "facts": facts})
    return {
        "family_id": f"registered-family:{family_hash[:20]}",
        "family_hash": family_hash,
        "family_kind": kind,
        "normalized_facts": facts,
        "proof_status": "hypothesis_only",
    }


def mine_critical_registered_families(
    dcp_path: Path | str,
    *,
    loader: Callable[[Path], Any] | None = None,
    critical_paths: list | None = None,
) -> dict[str, Any]:
    path = Path(dcp_path).expanduser().resolve()
    source_sha = file_sha256(path)
    loaded = (loader or _default_dcp_loader)(path)
    raw = loaded if isinstance(loaded, Mapping) else _mine_design(loaded, source_sha, critical_paths)
    if not isinstance(raw, Mapping):
        fail("MINER_INVALID_BACKEND_RESULT", "loader/miner did not return an object")
    raw_source = raw.get("design_sha256")
    if raw_source not in (None, source_sha):
        fail("MINER_SOURCE_HASH_MISMATCH", "mined facts belong to another artifact")
    families = []
    collections = (
        ("recurrence_transport", raw.get("recurrence_boundary_families", [])),
        ("fixed_product", raw.get("fixed_point_families", [])),
        ("registered_motif", (raw.get("repeated_arithmetic_motif_evidence") or {}).get("sequence_families", [])),
    )
    for kind, rows in collections:
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            fail("MINER_MALFORMED_FAMILY_SET", f"{kind} family set is not an array")
        for row in rows:
            if not isinstance(row, Mapping):
                fail("MINER_MALFORMED_FAMILY", f"{kind} family is not an object")
            families.append(_anonymous_summary(kind, row))
    families.sort(key=lambda item: (item["family_kind"], item["family_hash"]))
    return envelope(
        "critical_registered_family_miner",
        source_artifact_sha256=source_sha,
        candidate_family_id=None,
        candidate_hash=object_digest(families),
        status="success",
        payload={
            "miner_schema_version": TOOLS_SCHEMA_VERSION,
            "family_count": len(families),
            "families": families,
            "normalized_sibling_facts": families,
            "proof_status": "hypothesis_only",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dcp", type=Path)
    args = parser.parse_args()
    try:
        result = mine_critical_registered_families(args.dcp)
    except Exception as exc:
        from .common import rejection_from_exception
        try:
            source_sha = file_sha256(args.dcp)
        except Exception:
            source_sha = ""
        result = rejection_from_exception(
            "critical_registered_family_miner", exc, source_sha=source_sha
        )
    import json
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
