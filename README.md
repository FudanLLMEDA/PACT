# PACT Artifact

[English](README.md) | [简体中文](README.zh_CN.md)

PACT (Post-route Agentic Checkpoint Tuning) optimizes implemented AMD FPGA
design checkpoints through validation-gated Vivado and RapidWright actions.
This repository contains the PACT agent, its two MCP backend servers, the
optimization knowledge base, validation tooling, and paper artifact material.
It is the sanitized open-source artifact for our top-five FPL'26 final
submission.

## Artifact Scope

The public FPL'26 contest checkpoints used by the quick workflow are downloaded
by `make setup`; the artifact also retains Git LFS pointers for the
redistributable evaluation set. Vivado, Java, private evaluation outputs,
credentials, optimized checkpoints, and generated run directories are not
redistributed. The repository supports
source inspection, a lightweight source check, and an end-to-end optimization
run on a public checkpoint.

The release vendors the agent from `FudanLLMEDA/FDAgents` commit
`7691f3a99a304667b68f81d67bd6b41cb59d64ad` and the contest integration from
commit `1d6bef8d909e0ea751c398ea2a45248b37c1c679`. Private development branches,
credentials, deployment infrastructure, hidden-benchmark artifacts, raw runs,
and internal results are intentionally excluded.

The complete 35-design result package used for the paper is not present in this
snapshot. In particular, this snapshot does not contain all 35 input DCPs, raw
per-design results, DATuner and Codex baseline runners, or the aggregation and
plotting scripts. Consequently, the headline aggregate results cannot yet be
recomputed from this repository alone. Do not claim the Results Replicated
badge for this snapshot; add the complete result package and the full workflow
described in [Reproducing the paper results](#reproducing-the-paper-results)
first.

## Repository Layout

- `FDAgents/`: PACT agent, typed skills, recipe planner, decision logic, and
  within-run case memory
- `VivadoMCP/`: Vivado MCP backend
- `RapidWrightMCP/`: RapidWright MCP backend
- `knowledge/`: optimization knowledge and experimental notes
- `docs/`: supplementary optimization examples
- `baselines/`: paper baseline harnesses and reference tables
- `benchmarks/`: public/reproducible checkpoint manifest and Git LFS pointers
- `journals/`: manuscript source retained for artifact evaluation
- `validate_dcps.py`: structural and randomized-simulation comparison of two
  DCPs
- `runs/`: generated run outputs (ignored by Git)

## Hardware and Software Requirements

The reference artifact machine was measured on 2026-07-30 with the following
configuration:

- Microsoft Azure x86-64 VM with 8 logical CPUs (4 cores, 2 threads per core)
  from an AMD EPYC 9V45 96-Core Processor host
- 31 GiB RAM and no swap
- Ubuntu 24.04.4 LTS with Linux kernel `6.8.0-1059-azure`
- 495 GiB root filesystem; the reference PACT workspace and contest workspace
  occupied approximately 15 GiB before new run outputs
- AMD Vivado 2025.1, 64-bit, SW build 6140274, IP build 6138677, SharedData
  build 6139179, with a valid license for `xcvu3p-ffvc1517-2-e`
- Python 3.13.14 (installed by the pinned `uv` environment)
- RapidWright Python package 2026.1.0; the source checkout used by this
  repository is pinned at Git commit
  `f63afef5d34ad71e2544f0c1565c5286d16139fa`
- OpenJDK 11.0.16.1 (the Temurin runtime bundled with Vivado)
- MCP 1.28.1 exactly; the remaining Python dependency set is locked in
  `uv.lock`

For a single-design functional run, allocate at least 8 CPU threads, 32 GiB
RAM, and 25 GiB free disk space. A full 35-design rerun should use at least the
reference machine's 495 GiB filesystem or an equivalently sized external
workspace because concurrent Vivado runs, checkpoints, and logs accumulate
quickly. The public checkpoint archive itself is approximately 525 MB.

No physical FPGA board, accelerator, or GPU is required. Vivado is proprietary
software and is not included. Internet access is required during setup and,
for the LLM-guided workflow, for an OpenAI-compatible Responses API. A single
optimization may take up to one hour and can temporarily use several gigabytes
of disk space.

## Installation

Clone the artifact with its pinned RapidWright submodule:

```bash
git clone --recurse-submodules \
  https://github.com/FudanLLMEDA/PACT.git
cd PACT
```

Alternatively, unpack the archive downloaded from the artifact DOI and enter
its top-level directory. When Git submodule metadata is unavailable in an
archival source package, `make setup` fetches the same pinned RapidWright commit
directly from its upstream repository.

Run the self-contained setup target:

```bash
make setup VIVADO_EXEC=/path/to/Vivado/2025.1/bin/vivado
```

`make setup` installs pinned `uv`, Python 3.13.14 and the locked dependencies,
builds the pinned RapidWright checkout, and downloads the FPL'26 contest
checkpoint archive v1.2.0. If the
automatic download is unavailable, download
`fpl26_contest_benchmarks_v1.2.0.tar.gz` from the
[FPL'26 optimization contest release](https://github.com/Xilinx/fpl26_optimization_contest/releases/tag/v1.2.0)
and place it in the repository root before rerunning `make setup`.

Copy the environment template and set paths appropriate for the local machine:

```bash
cp .env.example .env
```

At minimum, set `VIVADO_EXEC` and `RAPIDWRIGHT_PATH`. Submission-compatible
runs use `OPENROUTER_API_KEY`. Direct non-submission runs may instead use
`OPENAI_API_KEY` and an optional `OPENAI_BASE_URL`. Never commit `.env`.

## Quick Validation

The following check compiles and imports the public Python sources without
Vivado, RapidWright, a DCP, or an API key:

```bash
make python-env
make check
```

This is a source/package sanity check, not a reproduction of the paper's FPGA
timing results.

## End-to-end Functional Workflow

### Deterministic run without an LLM

Run PACT's rule-based path on one public checkpoint:

```bash
make run_test \
  DCP=fpl26_contest_benchmarks/vexriscv_re-place_2025.1.dcp
```

This invokes:

```bash
python -m FDAgents.agent INPUT.dcp --no-llm --time-limit 3600
```

### LLM-guided PACT run

For the contest-compatible submission path, set `OPENROUTER_API_KEY` and run:

```bash
make run_optimizer \
  DCP=fpl26_contest_benchmarks/logicnets_jscl_2025.1.dcp
```

To use another OpenAI-compatible Responses endpoint or make output locations
explicit, invoke the underlying CLI directly:

```bash
python -m FDAgents.agent INPUT.dcp \
  --model MODEL_NAME \
  --time-limit 3600 \
  --run-dir runs/example \
  --output runs/example/optimized.dcp
```

At completion, PACT prints the baseline and final WNS/Fmax, elapsed time, token
usage and estimated cost (for an LLM run), output DCP path, and run directory.
The run directory also contains the current best checkpoint, `memory.json`, and
backend logs. A timing improvement is design- and tool-run-dependent; success
means that the command completes and emits a reopenable routed output DCP, not
that every run must improve timing.

## Comparing Two Checkpoints

After an optimization, run the separate comparison utility:

```bash
make validate \
  GOLDEN=/path/to/original.dcp \
  REVISED=/path/to/optimized.dcp \
  VECTORS=1000
```

The utility checks structural compatibility and uses randomized XSim vectors
when simulation is supported. It writes `validation_report.json` under its
temporary work directory and reports `PASSED`, `FAILED`, or
`INFRASTRUCTURE FAILURE`. This randomized comparison is not a formal
equivalence proof.

To check the validation infrastructure against an unchanged public DCP:

```bash
make validate_demo
```

## Reproducing the Paper Results

The paper reports these primary results over 35 UltraScale+ DCPs:

- Figure 1: per-design validation-clean Fmax normalized to the original DCP
- Figure 2: quality/runtime and quality/token-cost tradeoffs
- Table II: geometric-mean Fmax improvement for PACT, Scripted, Vivado
  `phys_opt`, DATuner, and the Codex Agent on the 27-design development set,
  8-design held-out set, and all 35 designs
- aggregate claims: PACT +22.30% Fmax, DATuner +15.14%, Codex Agent +9.78%,
  6.4x paired runtime speedup over DATuner, and $0.16 average token cost per DCP

A complete Results Replicated package must archive the following alongside this
code before the DOI is minted:

1. A 35-row benchmark manifest with source, split, target part, DCP filename,
   SHA-256 checksum, target clock, and baseline Fmax.
2. All redistributable DCPs plus acquisition or deterministic generation
   instructions for every omitted DCP.
3. Exact PACT, Scripted, `phys_opt`, DATuner, and Codex commands and
   configurations.
4. Raw logs and one machine-readable per-design table containing validation
   status, original/final Fmax, runtime, tool calls/actions, token counts, and
   cost for every method.
5. A common signoff script that checks checkpoint reopening, full routing, zero
   route errors, setup/hold/pulse-width/min-period timing, and preservation of
   primary I/O and clock definitions.
6. Aggregation and plotting scripts that regenerate the two paper figures and
   Table II from the raw table.

Until those files are added, evaluators can assess the implementation and the
single-design functional workflow but cannot independently recompute the
paper's aggregate numbers.

## Reproducibility Notes

- Use the original target clock and timing exceptions; do not relax
  constraints when comparing Fmax.
- PACT's LLM output is nondeterministic. Record the model identifier, API
  endpoint, date, complete run directory, and token accounting for every run.
- Generated checkpoints, logs, `.env`, `runs/`, and downloaded DCPs are ignored
  by Git. Include required raw results explicitly in the archival release before
  minting its DOI.

## License

PACT code authored by the FudanLLMEDA contributors is available under the
[MIT License](LICENSE). Files that retain an AMD copyright header and an
`SPDX-License-Identifier: Apache-2.0` marker remain under the
[Apache License 2.0](LICENSE-APACHE-2.0.txt); the root MIT license does not
relicense those files. The `RapidWright/` submodule and other third-party
dependencies remain subject to their respective upstream licenses. Vivado is
proprietary software and is not distributed in this repository.

## Snapshot

The original artifact root is a sanitized 2026-06-25 snapshot. This release
updates it to the final-submission implementation dated 2026-08-11 without
importing the private development repository's branches, logs, deployment
files, run outputs, or credentials. Legacy benchmark filenames and the
`clk_fpl26contest` clock identifier remain where required for compatibility
with the evaluated DCPs.
