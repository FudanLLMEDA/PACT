#!/usr/bin/env python3
"""Sequential PACT runner; use --design repeatedly to select a subset."""
import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, default=ROOT / "benchmarks/MANIFEST.csv")
    p.add_argument("--run-root", type=Path, default=ROOT / "runs/pact")
    p.add_argument("--design", action="append")
    p.add_argument("--model", required=True)
    p.add_argument("--time-limit", type=float, default=3600)
    p.add_argument("--validation-timeout", type=float, default=1800)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    with args.manifest.open() as f:
        rows = list(csv.DictReader(f))
    names = {r["example"] for r in rows}
    if args.design and set(args.design) - names:
        p.error(f"Unknown designs: {sorted(set(args.design) - names)}")
    failures = 0
    for row in rows:
        if args.design and row["example"] not in args.design:
            continue
        dcp = ROOT / row["dcp_relpath"]
        run = args.run_root.resolve() / dcp.stem
        command = [sys.executable, "-m", "FDAgents.agent", str(dcp), "--output", str(run / "output.dcp"),
                   "--run-dir", str(run), "--model", args.model, "--time-limit", str(args.time_limit),
                   "--validation-timeout", str(args.validation_timeout)]
        print(json.dumps({"design": row["example"], "command": command}), flush=True)
        if not args.dry_run:
            if run.exists():
                p.error(f"Run directory already exists; use a new --run-root: {run}")
            run.mkdir(parents=True)
            with (run / "console.log").open("w") as log:
                rc = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT).returncode
            (run / "exit_code.txt").write_text(f"{rc}\n")
            failures += rc != 0
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
