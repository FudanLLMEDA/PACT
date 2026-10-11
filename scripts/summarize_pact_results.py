#!/usr/bin/env python3
"""Aggregate new PACT summary.json files, not historical paper results.

Only completed route/timing/replay + structural/simulation-checked runs are
scored. Missing/failed/unvalidated designs remain visible and are NOT silently
counted as a normalized score of 1. Duplicate runs are an error.
"""
import argparse
import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def aggregate(manifest: list[dict], summaries: list[dict]) -> tuple[list[dict], dict]:
    by_name = {}
    names = [Path(r["dcp_relpath"]).name for r in manifest]
    if len(set(names)) != len(names):
        raise ValueError("Manifest checkpoint basenames must be unique")
    for result in summaries:
        if result.get("schema_version") != 1 or result.get("method") != "pact":
            raise ValueError("Unsupported summary schema/method")
        name = Path(result["input_dcp"]).name
        if name not in names:
            raise ValueError(f"Input not in manifest: {name}")
        if name in by_name:
            raise ValueError(f"Duplicate runs for {name}; select one run per design")
        by_name[name] = result
    rows = []
    for design in manifest:
        run = by_name.get(Path(design["dcp_relpath"]).name, {})
        validation = run.get("validation", {})
        checks = validation.get("checks", {})
        eligible = (run.get("status") == "completed" and validation.get("passed") is True
                    and validation.get("functional") == "passed"
                    and all(checks.get(k) == "passed" for k in
                            ("routing", "hold", "pulse_width", "constraints", "checkpoint_reopen"))
                    and checks.get("setup") == "measured")
        original = float(design["orig_fmax_csv_mhz"])
        achieved = run.get("final", {}).get("fmax_mhz")
        if not math.isfinite(original) or original <= 0:
            raise ValueError("Invalid manifest Fmax")
        if eligible and (not isinstance(achieved, (int, float)) or not math.isfinite(achieved) or achieved <= 0):
            raise ValueError(f"Invalid Fmax for completed run: {design['example']}")
        rows.append({"example": design["example"], "split": design["split"],
                     "status": run.get("status", "missing"), "eligible": eligible,
                     "original_fmax_mhz": original, "pact_fmax_mhz": achieved,
                     "measured_baseline_fmax_mhz": run.get("baseline", {}).get("fmax_mhz"),
                     "normalized_fmax": max(1.0, achieved / original) if eligible else None,
                     "wall_time_s": run.get("wall_time_s"),
                     "optimization_wall_time_s": run.get("optimization_wall_time_s"),
                     "cost_usd": run.get("usage", {}).get("cost_usd"),
                     "functional": validation.get("functional", "not_checked"),
                     "formal_equivalence": validation.get("formal_equivalence", "not_checked"),
                     "run_dir": run.get("run_dir", "")})
    report = {"schema_version": 1, "source": "new_runs", "groups": {}}
    for label, split in (("development", "train"), ("held_out", "test"), ("all", None)):
        selected = [r for r in rows if split is None or r["split"] == split]
        scored = [r["normalized_fmax"] for r in selected if r["eligible"]]
        eligible_rows = [r for r in selected if r["eligible"]]
        means = {}
        for key in ("wall_time_s", "optimization_wall_time_s", "cost_usd"):
            values = [r[key] for r in eligible_rows if isinstance(r[key], (int, float))
                      and math.isfinite(r[key]) and r[key] >= 0]
            means[f"mean_{key}"] = sum(values) / len(values) if values else None
            means[f"{key}_count"] = len(values)
        report["groups"][label] = {
            **means,
            "expected": len(selected), "scored": len(scored), "excluded": len(selected) - len(scored),
            "complete": bool(selected) and len(scored) == len(selected),
            "geomean_improvement_pct": 100 * (math.exp(sum(map(math.log, scored)) / len(scored)) - 1) if scored else None,
        }
    return rows, report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_root", type=Path)
    p.add_argument("--manifest", type=Path, default=ROOT / "benchmarks/MANIFEST.csv")
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    with args.manifest.open() as f:
        manifest = list(csv.DictReader(f))
    paths = sorted(args.run_root.rglob("summary.json"))
    summaries = [json.loads(path.read_text()) for path in paths]
    rows, report = aggregate(manifest, summaries)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "results.csv").open("w", newline="") as f:
        fields = list(rows[0]) if rows else ["example", "status"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (args.output_dir / "aggregate.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
