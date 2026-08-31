# DATuner baseline

[English](README.md) | [简体中文](README.zh_CN.md)

This directory contains the DATuner-style baseline used in the PACT
evaluation. It adapts the dynamic space-partitioning and multi-armed-bandit
search described by Xu et al., “A Parallel Bandit-Based Approach for
Autotuning FPGA Compilation,” FPGA 2017, to start from Vivado design
checkpoints.

The runner explores a 47,250-point space comprising Vivado optimization,
placement, physical-optimization and routing directives, a fanout limit, and
a clock-target factor. Each trial starts from the unchanged input DCP.

## Requirements

- Python 3.10 or newer
- AMD Vivado 2025.1 and a valid license
- `/usr/bin/time`
- The DCPs materialized from Git LFS under `benchmarks/`

Set `VIVADO_BIN` if `vivado` is not available on `PATH`.

## Single-design run

```bash
VIVADO_BIN=/path/to/vivado \
python baselines/datuner/run_local.py \
  --dcp benchmarks/logicnets_jscl_2025.1.dcp \
  --budget 15 \
  --timeout 1200 \
  --output-dir runs/datuner/logicnets
```

The original experiment used the per-design 8-, 12-, or 15-trial budgets in
`trial_counts.csv`. The per-trial timeout was 1,200 seconds. DATuner was not
subject to PACT's one-hour per-design wall-clock cap.

## Full benchmark batch

Preview the 35 commands without running Vivado:

```bash
python baselines/datuner/run_batch.py --dry-run
```

Run sequentially:

```bash
VIVADO_BIN=/path/to/vivado \
python baselines/datuner/run_batch.py \
  --output-dir runs/datuner
```

Use `--jobs N` only when the machine has enough Vivado licenses, memory, and
CPU capacity for `N` simultaneous implementation runs.

Each design directory contains `global_result.txt`, `summary.json`, a batch
console log, and per-trial Vivado reports. `reference_results.csv` contains the
DATuner Fmax and wall-clock values used by the paper.

Recompute the paper's aggregate DATuner result:

```bash
python baselines/datuner/summarize_results.py
```

As in the paper's comparison, a method that does not beat the unchanged input
checkpoint is assigned a normalized Fmax of 1.0. The expected geometric-mean
improvements are +18.39% on the 27-design development set, +4.80% on the
8-design held-out set, and +15.14% overall.

## Attribution

The baseline search is based on:

> C. Xu, G. Liu, R. Zhao, S. Yang, G. Luo, and Z. Zhang, “A Parallel
> Bandit-Based Approach for Autotuning FPGA Compilation,” FPGA 2017,
> pp. 157–166. DOI: 10.1145/3020078.3021747.

PACT's checkpoint runner and Vivado parameterization are artifact-specific
adaptations.
