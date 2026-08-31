"""Isolated subprocess worker for packaged BO QoR/runtime predictors."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator

from .predictor_package import validate_model_package


@contextmanager
def _isolated_imports(root: Path, module_name: str) -> Iterator[None]:
    root = root.resolve()
    local_names = {path.stem for path in root.glob("*.py")}
    original_path = list(sys.path)
    preserved = {name: sys.modules[name] for name in local_names if name in sys.modules}
    for name in preserved:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(root))
    try:
        yield
    finally:
        for name, module in list(sys.modules.items()):
            origin = getattr(module, "__file__", None)
            if name in local_names or (
                origin and Path(str(origin)).resolve().is_relative_to(root)
            ):
                sys.modules.pop(name, None)
        sys.modules.update(preserved)
        sys.path[:] = original_path


def _load_module(root: Path, filename: str, module_name: str) -> ModuleType:
    module_path = root / filename
    with _isolated_imports(root, module_name):
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import predictor helper {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


def _qor_prediction(root: Path, row: dict) -> tuple[dict, dict]:
    import joblib

    validation = validate_model_package(root, expected_kind="qor_ranker")
    helper = _load_module(
        root,
        "train_optimized_delta_models.py",
        "_fdagents_bo_qor_helper",
    )
    payload = joblib.load(root / "best_optimized_top5_ranker.joblib")
    result = helper.predict_payload(payload, [row])
    scope = bool(result["scope_match"][0])
    prediction = {
        "scope_match": scope,
        "pred_delta_fmax_mhz": float(result["pred_delta"][0]) if scope else None,
        "pred_result_fmax_mhz": float(result["pred_result"][0]) if scope else None,
        "improved_route_clean_probability": (
            float(result["improved_probability"][0]) if scope else None
        ),
        "uncertainty_mhz": float(result["rf_uncertainty"][0]) if scope else None,
    }
    return prediction, validation.compact_provenance()


def _runtime_prediction(root: Path, row: dict) -> tuple[dict, dict]:
    import joblib

    validation = validate_model_package(root, expected_kind="runtime_dispatcher")
    helper = _load_module(
        root,
        "train_unified_time_models.py",
        "_fdagents_bo_runtime_helper",
    )
    payload = joblib.load(root / "best_unified_time_predictor.joblib")
    result = helper.predict_unified_payload(payload, [row])
    scope = bool(result["scope_match"][0])
    prediction = {
        "scope_match": scope,
        "p50_s": float(result["prediction"][0]) if scope else None,
        "p10_s": float(result["p10"][0]) if scope else None,
        "p90_s": float(result["p90"][0]) if scope else None,
        "expert": str(result["expert"][0]),
        "mode": str(result["mode"][0]),
        "confidence": str(result["confidence"][0]),
    }
    return prediction, validation.compact_provenance()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qor-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    request = json.load(sys.stdin)
    row = request.get("row")
    if not isinstance(row, dict):
        raise ValueError("worker request requires one row object")
    qor, qor_package = _qor_prediction(args.qor_root.resolve(), row)
    response = {"qor": qor, "qor_package": qor_package}
    if args.runtime_root is not None:
        runtime, runtime_package = _runtime_prediction(
            args.runtime_root.resolve(), row
        )
        response.update({"runtime": runtime, "runtime_package": runtime_package})
    json.dump(response, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
