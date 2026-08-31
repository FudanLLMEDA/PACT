#!/usr/bin/env python3
"""Run the checkpoint-based DATuner baseline over the artifact manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks" / "MANIFEST.csv"
DEFAULT_BUDGETS = SCRIPT_DIR / "trial_counts.csv"


def design_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug:
        raise ValueError(f"cannot derive output name from {name!r}")
    return slug


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 35:
        raise ValueError(f"expected 35 manifest rows, found {len(rows)}")
    return rows


def load_budgets(path: Path) -> dict[str, int]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    budgets = {row["design"]: int(row["datuner_budget"]) for row in rows}
    if len(rows) != 35 or len(budgets) != 35:
        raise ValueError(
            f"expected 35 unique budget rows, found {len(rows)} rows "
            f"and {len(budgets)} names"
        )
    return budgets


def build_jobs(
    manifest: Path,
    budget_file: Path,
    output_dir: Path,
    only: set[str],
    timeout_s: int,
) -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    budgets = load_budgets(budget_file)
    for row in load_manifest(manifest):
        name = row["example"]
        if only and name not in only:
            continue
        dcp = REPO_ROOT / row["dcp_relpath"]
        if not dcp.is_file():
            raise FileNotFoundError(
                f"missing DCP {dcp}; run 'git lfs pull' before the batch"
            )
        if name not in budgets:
            raise KeyError(f"no DATuner trial budget recorded for {name}")
        budget = budgets[name]
        jobs.append(
            {
                "name": name,
                "dcp": dcp,
                "budget": budget,
                "timeout_s": timeout_s,
                "output_dir": output_dir / design_slug(name),
            }
        )
    return jobs


def command_for(job: dict[str, object]) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(SCRIPT_DIR / "run_local.py"),
        "--dcp",
        str(job["dcp"]),
        "--budget",
        str(job["budget"]),
        "--timeout",
        str(job["timeout_s"]),
        "--output-dir",
        str(job["output_dir"]),
    ]


def run_one(job: dict[str, object]) -> dict[str, object]:
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    command = command_for(job)
    console = output_dir / "batch_console.log"
    started = time.time()
    maximum_runtime = int(job["budget"]) * int(job["timeout_s"]) + 3600
    with console.open("w", encoding="utf-8") as stream:
        try:
            completed = subprocess.run(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                timeout=maximum_runtime,
                check=False,
            )
            returncode = completed.returncode
            error = None
        except subprocess.TimeoutExpired:
            returncode = 124
            error = f"batch timeout after {maximum_runtime}s"

    summary_path = output_dir / "summary.json"
    summary = None
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return {
        "name": job["name"],
        "dcp": str(job["dcp"]),
        "budget": job["budget"],
        "elapsed_s": round(time.time() - started, 3),
        "returncode": returncode,
        "error": error,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--budgets", type=Path, default=DEFAULT_BUDGETS)
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "runs" / "datuner"
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    selected = set(args.only)
    jobs = build_jobs(
        args.manifest.resolve(),
        args.budgets.resolve(),
        args.output_dir.resolve(),
        selected,
        args.timeout,
    )

    print(f"DATuner designs: {len(jobs)}")
    for job in jobs:
        print(
            f"{job['name']}: budget={job['budget']} "
            f"dcp={job['dcp']} output={job['output_dir']}"
        )
    if args.dry_run:
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_one, job): job for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                f"[{len(results)}/{len(jobs)}] {result['name']}: "
                f"exit={result['returncode']} elapsed={result['elapsed_s']}s"
            )

    results.sort(key=lambda item: str(item["name"]))
    result_path = args.output_dir / "batch_results.json"
    result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    failures = sum(result["returncode"] != 0 for result in results)
    print(f"Results: {result_path}")
    print(f"Completed: {len(results) - failures}, failed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
