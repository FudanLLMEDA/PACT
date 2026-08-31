#!/usr/bin/env python3
"""Summarize Codex Agent reference results using the paper's scoring rule."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path


DEFAULT_RESULTS = Path(__file__).resolve().with_name("reference_results.csv")


def normalized_score(row: dict[str, str]) -> float:
    original = float(row["original_fmax_mhz"])
    achieved = float(row["codex_fmax_mhz"])
    if original <= 0:
        raise ValueError(f"original Fmax must be positive for {row['example']}")
    return max(1.0, achieved / original)


def geometric_mean(values: list[float]) -> float:
    if not values:
        raise ValueError("cannot compute a geometric mean of an empty set")
    return math.exp(sum(math.log(value) for value in values) / len(values))


def improvement(rows: list[dict[str, str]], split: str | None) -> tuple[int, float]:
    selected = [
        normalized_score(row)
        for row in rows
        if split is None or row["split"] == split
    ]
    return len(selected), 100.0 * (geometric_mean(selected) - 1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()

    with args.results.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 35:
        raise ValueError(f"expected 35 result rows, found {len(rows)}")

    for label, split in (("development", "train"), ("held-out", "test"), ("all", None)):
        count, value = improvement(rows, split)
        print(f"{label}: n={count}, geometric-mean improvement={value:+.2f}%")
    mean_time = statistics.fmean(float(row["codex_time_s"]) for row in rows)
    mean_cost = statistics.fmean(float(row["codex_cost_usd"]) for row in rows)
    print(f"mean runtime: {mean_time:.0f}s")
    print(f"mean token cost: ${mean_cost:.2f} per DCP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
