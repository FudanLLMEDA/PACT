"""Render a recovered product-sum bundle and execute its mandatory OOC gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from .production_product_sum import (
    authorize_integrated_product_sum_emission,
    execute_ooc_timing_gate,
    render_production_product_sum,
)


_MARKER = re.compile(
    r"REGARITH_OOC_WNS=(?P<wns>-?[0-9.]+)\s+"
    r"REGARITH_OOC_DATA_PATH=(?P<delay>[0-9.]+)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    *, bundle_path: Path, source_sha256: str, output_dir: Path,
    vivado: str, part: str, period_ns: float,
) -> dict:
    if output_dir.exists():
        raise ValueError("output directory must be fresh")
    output_dir.mkdir(parents=False)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    emission = render_production_product_sum(
        bundle, source_artifact_sha256=source_sha256
    )
    (output_dir / "emission.json").write_text(
        json.dumps(emission, indent=2, sort_keys=True), encoding="utf-8"
    )

    def runner(module: dict, period: float) -> dict:
        name = module["module_name"]
        rtl = output_dir / f"{name}.v"
        dcp = output_dir / f"{name}.dcp"
        report = output_dir / f"{name}.timing.rpt"
        script = output_dir / f"{name}.tcl"
        log = output_dir / f"{name}.log"
        rtl.write_text(module["verilog"], encoding="ascii")
        script.write_text("\n".join([
            f"read_verilog {{{rtl}}}",
            f"synth_design -mode out_of_context -top {name} -part {part}",
            f"create_clock -period {period:.6f} -name regarith_ooc [get_ports clk]",
            "set rb_data_inputs [get_ports -quiet -filter {NAME != clk}]",
            "if {[llength $rb_data_inputs]} {set_input_delay 0.000 -clock regarith_ooc $rb_data_inputs}",
            "set_output_delay 0.000 -clock regarith_ooc [all_outputs]",
            "opt_design -directive ExploreWithRemap",
            "set rb_path [get_timing_paths -delay_type max -max_paths 1 -nworst 1]",
            "if {[llength $rb_path] != 1} {error {OOC gate found no timed path}}",
            "set rb_wns [get_property SLACK $rb_path]",
            "set rb_delay [get_property DATAPATH_DELAY $rb_path]",
            'puts "REGARITH_OOC_WNS=$rb_wns REGARITH_OOC_DATA_PATH=$rb_delay"',
            f"report_timing_summary -delay_type min_max -max_paths 50 -file {{{report}}}",
            f"write_checkpoint -force {{{dcp}}}",
            "exit", "",
        ]), encoding="utf-8")
        completed = subprocess.run(
            [vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)],
            check=False, capture_output=True, text=True, timeout=900,
            cwd=str(output_dir),
        )
        text = (completed.stdout or "") + (completed.stderr or "")
        log.write_text(text, encoding="utf-8")
        marker = _MARKER.search(text)
        if completed.returncode != 0 or marker is None or not dcp.is_file() or not report.is_file():
            raise RuntimeError(f"OOC synthesis failed for {name}: {text[-2000:]}")
        return {
            "setup_wns_ns": float(marker.group("wns")),
            "internal_data_path_ns": float(marker.group("delay")),
            "unconstrained_internal_path_count": 0,
            "ooc_dcp_sha256": _sha256(dcp),
            "timing_report_sha256": _sha256(report),
        }

    gate = execute_ooc_timing_gate(emission, runner, period_ns=period_ns)
    authorized = authorize_integrated_product_sum_emission(emission, gate)
    (output_dir / "ooc_gate.json").write_text(
        json.dumps(gate, indent=2, sort_keys=True), encoding="utf-8"
    )
    (output_dir / "authorized_emission.json").write_text(
        json.dumps(authorized, indent=2, sort_keys=True), encoding="utf-8"
    )
    summary = {
        "source_artifact_sha256": source_sha256,
        "bundle_kind": emission["bundle_kind"],
        "bundle_digest": emission["bundle_digest"],
        "plan_digests": emission["plan_digests"],
        "emission_result_digest": emission["result_digest"],
        "ooc_gate_digest": gate["gate_digest"],
        "authorization_digest": authorized["authorization_digest"],
        "module_count": emission["module_count"],
        "instance_count": emission["instance_count"],
        "mutation_allowed": authorized["mutation_allowed"],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("source_sha256")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--vivado", default="vivado")
    parser.add_argument("--part", required=True)
    parser.add_argument("--period-ns", type=float, default=2.0)
    args = parser.parse_args()
    result = run(
        bundle_path=args.bundle.resolve(), source_sha256=args.source_sha256,
        output_dir=args.output_dir.resolve(), vivado=args.vivado,
        part=args.part, period_ns=args.period_ns,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
