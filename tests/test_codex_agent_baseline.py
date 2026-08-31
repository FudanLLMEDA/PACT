"""Pure-Python checks for the artifact Codex Agent baseline."""

import csv
import importlib.util
import tempfile
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "baselines" / "codex_agent"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HARNESS = load_module("codex_harness", BASELINE / "run_codex_dcp_harness.py")
SUMMARY = load_module("codex_summary", BASELINE / "summarize_results.py")


def test_manifest_matches_all_benchmarks():
    with (BASELINE / "manifest.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    with (ROOT / "benchmarks" / "MANIFEST.csv").open(
        newline="", encoding="utf-8"
    ) as stream:
        benchmark_rows = list(csv.DictReader(stream))
    assert len(rows) == len(benchmark_rows) == 35
    assert {row["input_dcp"] for row in rows} == {
        row["dcp_relpath"] for row in benchmark_rows
    }
    assert Counter(row["dataset"] for row in rows) == Counter(
        {"train": 27, "test": 8}
    )
    assert all(
        (row["dataset"] == "test") == (row["excluded_from_optimization"] == "true")
        for row in rows
    )
    with (BASELINE / "reference_results.csv").open(
        newline="", encoding="utf-8"
    ) as stream:
        reference_rows = list(csv.DictReader(stream))
    assert {row["example"] for row in reference_rows} == {
        row["example"] for row in benchmark_rows
    }
    assert Counter(row["split"] for row in reference_rows) == Counter(
        {"train": 27, "test": 8}
    )


def test_dry_run_writes_35_anonymous_prompts():
    with tempfile.TemporaryDirectory() as temporary:
        run_root = Path(temporary) / "runs"
        result_path = HARNESS.run_codex_manifest(
            manifest_path=BASELINE / "manifest.csv",
            run_root=run_root,
            inventory_path=BASELINE / "inventory.md",
            codex_cmd="codex",
            time_limit_s=3600,
            jobs=1,
            dry_run=True,
            project_root=ROOT,
        )
        with result_path.open(newline="", encoding="utf-8") as stream:
            results = list(csv.DictReader(stream))
        assert len(results) == 35
        assert {row["status"] for row in results} == {"DRY_RUN"}
        assert len(list(run_root.glob("*/prompt.md"))) == 35


def test_paper_scoring_floors_regressions():
    row = {
        "example": "example",
        "original_fmax_mhz": "100.0",
        "codex_fmax_mhz": "90.0",
    }
    assert SUMMARY.normalized_score(row) == 1.0


def test_reference_metrics():
    with (BASELINE / "reference_results.csv").open(
        newline="", encoding="utf-8"
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert round(SUMMARY.improvement(rows, "train")[1], 2) == 12.70
    assert round(SUMMARY.improvement(rows, "test")[1], 2) == 0.47
    assert round(SUMMARY.improvement(rows, None)[1], 2) == 9.78


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
