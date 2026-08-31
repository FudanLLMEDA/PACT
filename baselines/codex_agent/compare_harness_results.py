#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


FIELDS = [
    "target",
    "dataset",
    "ceiling_mhz",
    "target_95_mhz",
    "codex_fmax_mhz",
    "codex_ratio",
    "codex_status",
    "codex_wall_time_s",
    "codex_agent_elapsed_s",
    "codex_input_tokens",
    "codex_cached_input_tokens",
    "codex_uncached_input_tokens",
    "codex_output_tokens",
    "codex_reasoning_output_tokens",
    "codex_total_tokens",
    "codex_cost_usd",
    "codex_cost_upper_bound_usd",
    "codex_usage_source",
    "fdagents_fmax_mhz",
    "fdagents_ratio",
    "fdagents_status",
    "fdagents_wall_time_s",
    "fdagents_agent_elapsed_s",
    "fdagents_input_tokens",
    "fdagents_cached_input_tokens",
    "fdagents_uncached_input_tokens",
    "fdagents_output_tokens",
    "fdagents_reasoning_output_tokens",
    "fdagents_total_tokens",
    "fdagents_cost_usd",
    "fdagents_cost_upper_bound_usd",
    "fdagents_usage_source",
    "verify_time_s",
    "winner",
    "notes",
]


def _read_by_target(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["target"]: row for row in csv.DictReader(f)}


def _float(value: str) -> float | None:
    try:
        if value == "" or value == "unknown":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(fmax: str, ceiling: str) -> str:
    f = _float(fmax)
    c = _float(ceiling)
    if f is None or c is None or c <= 0:
        return ""
    return f"{(f / c) * 100.0:.2f}%"


def _sum_numeric(*values: str) -> str:
    nums = [_float(v) for v in values]
    nums = [v for v in nums if v is not None]
    if not nums:
        return ""
    return f"{sum(nums):.3f}"


def _accounted_time_s(row: dict[str, str], *, cap_s: float = 3600.0) -> str:
    """Time charged to the agent budget; exclude timeout grace and verification."""
    for key in ("agent_elapsed_s", "wall_time_s"):
        value = _float(row.get(key, ""))
        if value is not None:
            return f"{min(value, cap_s):.3f}"
    return ""


def _winner(codex: dict[str, str], fdagents: dict[str, str]) -> str:
    cf = _float(codex.get("verified_fmax_mhz", ""))
    ff = _float(fdagents.get("verified_fmax_mhz", ""))
    if cf is None and ff is None:
        return "no_result"
    if cf is not None and ff is None:
        return "codex"
    if cf is None and ff is not None:
        return "fdagents"
    if cf == ff:
        return "tie"
    return "codex" if cf > ff else "fdagents"


def build_comparison(
    manifest_csv: Path,
    codex_verified_csv: Path,
    fdagents_verified_csv: Path,
    output_csv: Path,
    output_md: Path,
) -> None:
    with manifest_csv.open(newline="") as f:
        manifest_rows = list(csv.DictReader(f))
    codex = _read_by_target(codex_verified_csv)
    fdagents = _read_by_target(fdagents_verified_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    for manifest in manifest_rows:
        target = manifest["target"]
        c = codex.get(target, {})
        fa = fdagents.get(target, {})
        row = {
            "target": target,
            "dataset": manifest.get("dataset", ""),
            "ceiling_mhz": manifest.get("ceiling_mhz", ""),
            "target_95_mhz": manifest.get("target_95_mhz", ""),
            "codex_fmax_mhz": c.get("verified_fmax_mhz", ""),
            "codex_ratio": _ratio(c.get("verified_fmax_mhz", ""), manifest.get("ceiling_mhz", "")),
            "codex_status": c.get("verify_status", "not_run"),
            "codex_wall_time_s": _accounted_time_s(c),
            "codex_agent_elapsed_s": c.get("agent_elapsed_s", ""),
            "codex_input_tokens": c.get("input_tokens", ""),
            "codex_cached_input_tokens": c.get("cached_input_tokens", ""),
            "codex_uncached_input_tokens": c.get("uncached_input_tokens", ""),
            "codex_output_tokens": c.get("output_tokens", ""),
            "codex_reasoning_output_tokens": c.get("reasoning_output_tokens", ""),
            "codex_total_tokens": c.get("total_tokens", ""),
            "codex_cost_usd": c.get("cost_usd", ""),
            "codex_cost_upper_bound_usd": c.get("cost_upper_bound_usd", ""),
            "codex_usage_source": c.get("usage_source", ""),
            "fdagents_fmax_mhz": fa.get("verified_fmax_mhz", ""),
            "fdagents_ratio": _ratio(fa.get("verified_fmax_mhz", ""), manifest.get("ceiling_mhz", "")),
            "fdagents_status": fa.get("verify_status", "not_run"),
            "fdagents_wall_time_s": _accounted_time_s(fa),
            "fdagents_agent_elapsed_s": fa.get("agent_elapsed_s", ""),
            "fdagents_input_tokens": fa.get("input_tokens", ""),
            "fdagents_cached_input_tokens": fa.get("cached_input_tokens", ""),
            "fdagents_uncached_input_tokens": fa.get("uncached_input_tokens", ""),
            "fdagents_output_tokens": fa.get("output_tokens", ""),
            "fdagents_reasoning_output_tokens": fa.get("reasoning_output_tokens", ""),
            "fdagents_total_tokens": fa.get("total_tokens", ""),
            "fdagents_cost_usd": fa.get("cost_usd", ""),
            "fdagents_cost_upper_bound_usd": fa.get("cost_upper_bound_usd", ""),
            "fdagents_usage_source": fa.get("usage_source", ""),
            "verify_time_s": _sum_numeric(c.get("verify_time_s", ""), fa.get("verify_time_s", "")),
            "winner": _winner(c, fa),
            "notes": "",
        }
        rows.append(row)

    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    md = ["# Harness Comparison", "", "| Target | Dataset | Codex MHz | FDAgents MHz | Winner |", "|---|---|---:|---:|---|"]
    for row in rows:
        md.append(
            f"| `{row['target']}` | {row['dataset']} | {row['codex_fmax_mhz']} | "
            f"{row['fdagents_fmax_mhz']} | {row['winner']} |"
        )
    output_md.write_text("\n".join(md) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--codex-verified", type=Path, required=True)
    parser.add_argument("--fdagents-verified", "--fdagents-test-verified", dest="fdagents_verified", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)
    build_comparison(
        args.manifest,
        args.codex_verified,
        args.fdagents_verified,
        args.output_csv,
        args.output_md,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
