"""Pure-Python checks for the artifact DATuner batch wrapper."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "baselines" / "datuner" / "run_batch.py"
SPEC = importlib.util.spec_from_file_location("datuner_run_batch", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

SUMMARY_PATH = ROOT / "baselines" / "datuner" / "summarize_results.py"
SUMMARY_SPEC = importlib.util.spec_from_file_location(
    "datuner_summarize_results", SUMMARY_PATH
)
SUMMARY = importlib.util.module_from_spec(SUMMARY_SPEC)
assert SUMMARY_SPEC.loader is not None
SUMMARY_SPEC.loader.exec_module(SUMMARY)


def test_recorded_budgets():
    budgets = MODULE.load_budgets(
        ROOT / "baselines" / "datuner" / "trial_counts.csv"
    )
    assert len(budgets) == 35
    assert budgets["BOOM SoC"] == 8
    assert budgets["LogicNets JSCL"] == 12
    assert budgets["VexRiscv Re-place"] == 15


def test_design_slug():
    assert MODULE.design_slug("LogicNets JSCL") == "logicnets_jscl"
    assert MODULE.design_slug("Double-Clock FFT-L") == "double_clock_fft_l"


def test_paper_scoring_floors_regressions():
    row = {
        "example": "example",
        "original_fmax_mhz": "100.0",
        "datuner_fmax_mhz": "90.0",
    }
    assert SUMMARY.normalized_score(row) == 1.0


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
