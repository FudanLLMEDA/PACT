#!/usr/bin/env python3
"""Single-machine runner for DATuner Vivado DCP optimization.

Full-parameter search including directives, retiming, fanout limit, force
replication, and clock tightening — the maximum scope DATuner can explore.

Uses datuner's bandit-based space_partition algorithm, evaluations run locally.
"""

import argparse
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VIVADO = os.environ.get("VIVADO_BIN") or shutil.which("vivado") or "vivado"

# ---- Inlined space_partition.py (from Cornell DATuner) -----------------------

def _check_design_point_in_space(design_point, target_space):
    for i in design_point[0]:
        for j in target_space:
            if i[0] == j[1]:
                if j[0] == 'EnumParameter':
                    if i[1] not in j[2]:
                        return False
                    else:
                        break
    return True

def _calculate_conditional_entropy(target_space, dimension, average_qor, global_result):
    conditional_entropy = 0
    values = dimension[2]
    for value in values:
        target_subspace = copy.deepcopy(target_space)
        for i in target_subspace:
            if i[1] == dimension[1]:
                i[2] = [value]
                break
        num_pts, num_good_pts, num_bad_pts = 0, 0, 0
        for design_point in global_result:
            if _check_design_point_in_space(design_point, target_subspace):
                num_pts += 1
                if design_point[-1] <= average_qor:
                    num_good_pts += 1
                else:
                    num_bad_pts += 1
        if num_pts == 0:
            continue
        num_pts, num_good_pts, num_bad_pts = map(float, [num_pts, num_good_pts, num_bad_pts])
        entropy_good_pts, entropy_bad_pts = 0, 0
        if num_good_pts > 0:
            entropy_good_pts = -(num_good_pts/num_pts*math.log(num_good_pts/num_pts))
        if num_bad_pts > 0:
            entropy_bad_pts = -(num_bad_pts/num_pts*math.log(num_bad_pts/num_pts))
        target_subspace_entropy = entropy_good_pts + entropy_bad_pts
        conditional_entropy += num_pts * target_subspace_entropy
    return conditional_entropy

def _select_dimension(target_space, global_result):
    sum_qor = sum(i[-1] for i in global_result)
    average_qor = float(sum_qor) / len(global_result)
    best_conditional_entropy = 1e9
    target_dimension = target_space[0][1]
    for dimension in target_space:
        conditional_entropy = _calculate_conditional_entropy(
            target_space, dimension, average_qor, global_result)
        if conditional_entropy < best_conditional_entropy:
            best_conditional_entropy = conditional_entropy
            target_dimension = dimension[1]
    return target_dimension

def partition_space(subspaces, global_result):
    best_score = -1e9
    target_space_tuple = subspaces[0]
    target_space = subspaces[0][0]
    for space_tuple in subspaces:
        if space_tuple[1] > best_score:
            target_space = space_tuple[0]
            best_score = space_tuple[1]
            target_space_tuple = space_tuple
    dimension_to_partition = _select_dimension(target_space, global_result)
    for dimension in target_space:
        if dimension[1] == dimension_to_partition:
            values = dimension[2]
            break
    for value in values:
        target_space_copy = copy.deepcopy(target_space)
        for dimension in target_space_copy:
            if dimension[1] == dimension_to_partition:
                dimension[2] = [value]
                break
        subspaces.append([target_space_copy, 0, target_space_tuple[2]])
    subspaces.remove(target_space_tuple)

def update_score(t, subspaces, global_result):
    for space_tuple in subspaces:
        num_points, total_qor = 0, 0
        for design_point in global_result:
            if _check_design_point_in_space(design_point, space_tuple[0]):
                num_points += 1
                total_qor += -design_point[1]
        if num_points == 0:
            space_tuple[1] = math.sqrt(2*math.log(t)/space_tuple[2])
        else:
            space_tuple[1] = total_qor/num_points + math.sqrt(2*math.log(t)/space_tuple[2])

def select_space(t, subspaces, global_result):
    update_score(t, subspaces, global_result)
    best_score = -1e9
    best_space_list = subspaces[0] if subspaces else None
    for space_tuple in subspaces:
        if space_tuple[1] > best_score:
            best_space_list = space_tuple
            best_score = space_tuple[1]
    best_space_list[2] += 1
    return best_space_list[0]

# ---- Parameter space -------------------------------------------------------
SPACE = [
    ["EnumParameter", "opt_directive", [
        "Default", "Explore", "ExploreArea",
        "ExploreSequentialArea", "AddRemap",
    ]],
    ["EnumParameter", "fanout_limit", [
        "100", "500", "1000", "5000", "10000",
    ]],
    ["EnumParameter", "place_directive", [
        "Default", "Explore", "WLDrivenBlockPlacement",
        "ExtraNetDelay_high", "ExtraNetDelay_low", "ExtraTimingOpt",
        "SpreadLogic_high", "SpreadLogic_low", "SSI_SpreadLogic_high",
    ]],
    ["EnumParameter", "phys_opt_directive", [
        "Default", "Explore", "AggressiveExplore",
        "AggressiveFanoutOpt", "AlternateFlowWithRetiming",
        "AlternateReplication",
    ]],
    ["EnumParameter", "route_directive", [
        "Default", "Explore", "AggressiveExplore",
        "HigherDelayCost", "NoTimingRelaxation",
        "MoreGlobalIterations", "AdvancedSkewModeling",
    ]],
    ["EnumParameter", "target_factor", [
        "0.80", "0.84", "0.88", "0.92", "0.95",
    ]],
]
# Total: 5×5×9×6×7×5 = 47,250 combinations


def probe_baseline(dcp_path: str) -> dict:
    """Quick Vivado timing analysis on the original DCP. Returns clock info."""
    tcl = """
open_checkpoint ./design.dcp
set clk [get_clocks -quiet clk_fpl26contest]
if {$clk eq {}} { set clk [lindex [get_clocks -quiet] 0] }
if {$clk eq {}} { error "no clock found" }
set cname [get_property NAME $clk]
set period [get_property PERIOD $clk]
set src [get_property SOURCE_PINS $clk]
set port [lindex [get_ports -quiet $src] 0]
report_timing_summary -delay_type max -file timing_summary.rpt
puts "DATUNER_PROBE clock=$cname period=$period port=$port"
"""
    work = Path("/tmp/datuner_probe_%d" % os.getpid())
    work.mkdir(parents=True, exist_ok=True)
    tcl_path = work / "probe.tcl"
    tcl_path.write_text(tcl)
    dcp_link = work / "design.dcp"
    if dcp_link.exists():
        dcp_link.unlink()
    dcp_link.symlink_to(Path(dcp_path).resolve())

    result = subprocess.run(
        [VIVADO, "-mode", "batch", "-source", str(tcl_path)],
        capture_output=True, text=True, timeout=3600, cwd=str(work),
    )
    info = {"clock_name": "clk_fpl26contest", "period_ns": 5.0, "port": "", "wns": 0.0}
    for line in result.stdout.splitlines():
        if line.startswith("DATUNER_PROBE"):
            m = re.search(r"clock=(\S+) period=([\d.]+) port=(\S+)", line)
            if m:
                info["clock_name"] = m.group(1)
                info["period_ns"] = float(m.group(2))
                info["port"] = m.group(3)

    rpt = work / "timing_summary.rpt"
    if rpt.exists():
        text = rpt.read_text()
        m = re.search(r"Worst\s+Slack\s+([\-−]?\d+\.?\d*)\s*ns", text)
        if m:
            info["wns"] = float(m.group(1).replace("−", "-"))

    info["achievable_ns"] = info["period_ns"] - info["wns"]
    return info


def sample_config(space=None, rng=None):
    """Randomly sample one config from the selected DATuner subspace."""
    import random

    space = space if space is not None else SPACE
    rng = rng if rng is not None else random
    cfg = {}
    for param_type, param_name, param_range in space:
        if param_type == "EnumParameter":
            cfg[param_name] = rng.choice(param_range)
        elif param_type == "IntegerParameter":
            cfg[param_name] = rng.randint(param_range[0], param_range[1])
        elif param_type == "FloatParameter":
            cfg[param_name] = rng.uniform(param_range[0], param_range[1])
    return cfg


def write_options_tcl(cfg, target_period_ns, baseline, path="options.tcl",
                     route_only=False, no_clock_tighten=False):
    """Generate options.tcl with all parameters."""
    with open(path, "w") as f:
        for key, val in cfg.items():
            f.write(f'set {key} {{{val}}}\n')
        f.write(f'set target_period_ns {target_period_ns:.3f}\n')
        f.write(f'set clock_name {{{baseline["clock_name"]}}}\n')
        f.write(f'set clock_port {{{baseline["port"]}}}\n')
        f.write(f'set route_only {"1" if route_only else "0"}\n')
        f.write(f'set no_clock_tighten {"1" if no_clock_tighten else "0"}\n')


def parse_timing_summary(rpt_path="timing_summary.rpt"):
    """Return (wns, period_ns, luts, ffs, brams, dsps)."""
    if not os.path.exists(rpt_path):
        return None, None, 0, 0, 0, 0
    text = Path(rpt_path).read_text()
    wns = None
    # Match "Worst Slack  -X.XXXns" in timing summary (handles both ASCII minus and Unicode)
    m = re.search(r"Worst\s+Slack\s+([\-−]?\d+\.?\d*)\s*ns", text)
    if m:
        val = m.group(1).replace("−", "-")
        wns = float(val)
    if wns is None:
        # Fallback: "Slack (VIOLATED) : -X.XXXns"
        m = re.search(r"Slack\s*\(VIOLATED\)\s*:\s*([\-−]?\d+\.?\d*)", text)
        if m:
            val = m.group(1).replace("−", "-")
            wns = float(val)
    period = None
    m = re.search(r"Period\(ns\):\s*(\d+\.?\d*)", text)
    if m:
        period = float(m.group(1))

    def _u(label):
        m = re.search(rf"{re.escape(label)}\s*\|\s*(\d+)", text)
        return int(m.group(1)) if m else 0
    return wns, period, _u("CLB LUTs"), _u("CLB Registers"), _u("Block RAM Tile"), _u("DSPs")


def run_vivado(dcp_path, cfg, target_period_ns, baseline, work_dir, timeout_s=1800,
               route_only=False, no_clock_tighten=False):
    """Run one Vivado eval. Returns (wns, metadata) or (None, error)."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    write_options_tcl(cfg, target_period_ns, baseline, str(work_dir / "options.tcl"),
                     route_only=route_only, no_clock_tighten=no_clock_tighten)

    dcp_abs = Path(dcp_path).resolve()
    dcp_link = work_dir / "design.dcp"
    if dcp_link.exists():
        dcp_link.unlink()
    dcp_link.symlink_to(dcp_abs)

    tcl_src = SCRIPT_DIR / "dcp_flow" / "run_vivado_dcp.tcl"
    run_tcl = work_dir / "run_vivado_dcp.tcl"
    shutil.copy(tcl_src, run_tcl)

    time_log = work_dir / "resource.log"
    try:
        proc = subprocess.run(
            ["/usr/bin/time", "-o", str(time_log),
             "-f", "elapsed_sec:%e\\ncpu_pct:%P\\nmax_rss_kb:%M\\ncpu_sec_user:%U\\ncpu_sec_sys:%S",
             VIVADO, "-mode", "batch", "-source", str(run_tcl)],
            capture_output=True, text=True, timeout=timeout_s, cwd=str(work_dir),
        )
    except subprocess.TimeoutExpired:
        return None, {"error": "timeout"}

    # Parse resource usage from /usr/bin/time
    res = {"error": None}
    if time_log.exists():
        try:
            for line in time_log.read_text().splitlines():
                line = line.strip()
                if line.startswith("elapsed_sec:"):
                    res["elapsed_s"] = float(line.split(":")[1])
                elif line.startswith("cpu_pct:"):
                    res["cpu_pct"] = line.split(":")[1]
                elif line.startswith("max_rss_kb:"):
                    res["max_rss_kb"] = int(line.split(":")[1])
                elif line.startswith("cpu_sec_user:"):
                    res["cpu_sec_user"] = float(line.split(":")[1])
                elif line.startswith("cpu_sec_sys:"):
                    res["cpu_sec_sys"] = float(line.split(":")[1])
        except Exception:
            pass

    if proc.returncode != 0:
        res["error"] = f"exit {proc.returncode}"
        return None, res

    wns, period, luts, ffs, brams, dsps = parse_timing_summary(
        str(work_dir / "timing_summary.rpt"))
    if wns is None:
        res["error"] = "no WNS"
        return None, res

    res.update({"luts": luts, "ffs": ffs, "brams": brams, "dsps": dsps, "period": period})
    return wns, res


def run_datuner_local(dcp_path, budget, output_dir, timeout_s=1800,
                      no_clock_tighten=False, route_only=False):
    """Main bandit search loop."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Probe baseline
    print("[DATuner] Probing baseline DCP...")
    baseline = probe_baseline(dcp_path)
    print(f"[DATuner]   clock={baseline['clock_name']} "
          f"period={baseline['period_ns']:.3f}ns  "
          f"WNS={baseline['wns']:.3f}ns  "
          f"achievable={baseline['achievable_ns']:.3f}ns")
    print()

    global_result = []
    subspaces = [[copy.deepcopy(SPACE), 0, 1]]
    best_qor = float("inf")
    best_cfg = None
    best_wns = None
    best_fmax = None
    best_target_ns = None

    print(f"[DATuner] Budget: {budget}  |  Space: 47,250 combos")
    print(f"[DATuner] DCP: {dcp_path}")
    print(f"[DATuner] Output: {output_dir}")
    print()

    start_time = time.time()

    for iteration in range(1, budget + 1):
        iter_start = time.time()
        selected_space = select_space(iteration, subspaces, global_result)
        cfg = sample_config(selected_space)

        if no_clock_tighten:
            target_period_ns = baseline["period_ns"]
        else:
            target_factor = float(cfg["target_factor"])
            target_period_ns = round(baseline["achievable_ns"] * target_factor, 3)
            if target_period_ns < 0.100:
                target_period_ns = 0.100

        work_dir = output_dir / f"eval_{iteration:04d}"
        work_dir.mkdir(parents=True, exist_ok=True)

        wns, metadata = run_vivado(dcp_path, cfg, target_period_ns, baseline,
                                   str(work_dir), timeout_s,
                                   route_only=route_only,
                                   no_clock_tighten=no_clock_tighten)
        elapsed = time.time() - iter_start

        if wns is None:
            qor = 1e10
            ach_period = None
            fmax_mhz = 0
            status = f"FAIL({metadata.get('error', '?')})"
        else:
            ach_period = target_period_ns - wns
            qor = ach_period  # lower achievable period = higher Fmax
            fmax_mhz = 1000.0 / ach_period if ach_period > 0 else 0
            status = f"WNS={wns:+.3f}ns  Fmax={fmax_mhz:.1f}MHz"

        cfg_list = [[k, v] for k, v in cfg.items()]
        global_result.append([cfg_list, qor])

        res_str = ""
        if wns is not None and metadata:
            rss_gb = metadata.get("max_rss_kb", 0) / (1024 * 1024)
            cpu_s = metadata.get("cpu_sec_user", 0) + metadata.get("cpu_sec_sys", 0)
            res_str = f"cpu={cpu_s:.0f}s rss={rss_gb:.1f}G"

        print(f"[{iteration:4d}/{budget}] {status}  "
              f"tclk={target_period_ns:.3f}ns  "
              f"opt={cfg['opt_directive']}@{cfg['fanout_limit']}  "
              f"place={cfg['place_directive']}  "
              f"phys={cfg['phys_opt_directive']}  "
              f"route={cfg['route_directive']}  "
              f"({elapsed:.0f}s {res_str})")

        if qor < best_qor and wns is not None:
            best_qor = qor
            best_wns = wns
            best_cfg = cfg
            best_fmax = fmax_mhz
            best_target_ns = target_period_ns
            print(f"  >> NEW BEST: Fmax={best_fmax:.2f}MHz  "
                  f"period={ach_period:.3f}ns  "
                  f"WNS={wns:+.3f}ns  target={target_period_ns:.3f}ns")

        # Log
        md = metadata
        with open(output_dir / "global_result.txt", "a") as f:
            f.write(",".join(str(x) for x in (
                [v for pair in cfg_list for v in pair] +
                [target_period_ns, md.get("luts", 0), md.get("ffs", 0),
                 md.get("brams", 0), md.get("dsps", 0), qor]
            )) + "\n")

        if len(global_result) >= 15 and iteration < budget:
            partition_space(subspaces, global_result)

    total_time = time.time() - start_time
    print()
    print(f"[DATuner] Done. {budget} evals in {total_time:.0f}s")
    if best_cfg:
        print(f"[DATuner] Best: Fmax={best_fmax:.2f}MHz  "
              f"period={best_qor:.3f}ns  "
              f"WNS={best_wns:+.3f}ns  target={best_target_ns:.3f}ns")
        print(f"[DATuner] Config: opt={best_cfg['opt_directive']}@"
              f"{best_cfg['fanout_limit']}  "
              f"place={best_cfg['place_directive']}  "
              f"phys={best_cfg['phys_opt_directive']}  "
              f"route={best_cfg['route_directive']}  "
              f"factor={best_cfg['target_factor']}")

    summary = {
        "budget": budget, "total_time_s": total_time,
        "dcp": str(dcp_path), "best_wns": best_wns, "best_fmax": best_fmax,
        "best_config": best_cfg, "baseline": baseline,
        "num_subspaces": len(subspaces),
        "num_ok": sum(1 for r in global_result if r[1] < 1e9),
        "num_fail": sum(1 for r in global_result if r[1] >= 1e9),
        "per_eval_time_s": [round(time.time() - start_time, 1)],
    }
    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[DATuner] Summary: {output_dir / 'summary.json'}")

    return best_wns, best_cfg


def main():
    p = argparse.ArgumentParser(description="DATuner local runner for Vivado DCP")
    p.add_argument("--dcp", dest="dcp_path", required=True)
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--output-dir", dest="output_dir", required=True)
    p.add_argument("--timeout", type=int, dest="timeout_s", default=1800)
    p.add_argument("--no-clock-tighten", action="store_true", help="Skip clock tightening, use original period only")
    p.add_argument("--route-only", action="store_true", help="Skip unplace, only re-route (faster for large DCPs)")
    args = p.parse_args()
    if args.no_clock_tighten:
        # Remove target_factor from search space
        global SPACE
        SPACE = [s for s in SPACE if s[1] != "target_factor"]
    run_datuner_local(args.dcp_path, args.budget, args.output_dir, args.timeout_s,
                      no_clock_tighten=args.no_clock_tighten,
                      route_only=args.route_only)


if __name__ == "__main__":
    main()
