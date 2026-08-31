#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _read_header(path: Path) -> list[str]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration as exc:
            raise ValueError(f"empty input: {path}") from exc


def merge_results(inputs: list[Path], output: Path) -> Path:
    if not inputs:
        raise ValueError("at least one input CSV is required")

    header = _read_header(inputs[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=header)
        writer.writeheader()
        for path in inputs:
            current_header = _read_header(path)
            if current_header != header:
                raise ValueError(f"header mismatch: {path}")
            with path.open(newline="") as in_f:
                reader = csv.DictReader(in_f)
                for row in reader:
                    writer.writerow(row)
        out_f.flush()
        os.fsync(out_f.fileno())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    merge_results(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
