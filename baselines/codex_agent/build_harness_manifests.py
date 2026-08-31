#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


FIELDS = [
    "target",
    "input_dcp",
    "dataset",
    "group",
    "excluded_from_optimization",
    "ceiling_mhz",
    "target_95_mhz",
    "assigned_host",
    "status",
]


def _dataset(row: dict[str, str]) -> str:
    excluded = str(row.get("excluded_from_optimization", "")).strip().lower()
    return "test" if excluded == "true" else "train"


def build_manifests(source_csv: Path, output_dir: Path) -> None:
    with source_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))

    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for row in rows:
        target = row.get("target", "").strip()
        if not target:
            raise ValueError("manifest row missing target")
        if target in seen:
            raise ValueError(f"duplicate target {target}")
        seen.add(target)
        out = {field: row.get(field, "") for field in FIELDS}
        out["dataset"] = _dataset(row)
        normalized.append(out)

    output_dir.mkdir(parents=True, exist_ok=True)
    splits = {
        "all.csv": normalized,
        "train.csv": [row for row in normalized if row["dataset"] == "train"],
        "test.csv": [row for row in normalized if row["dataset"] == "test"],
    }
    for name, split_rows in splits.items():
        with (output_dir / name).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(split_rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    build_manifests(args.source, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
