# PACT FPT'26 Paper Artifact

[English](README.md) | [简体中文](README.zh_CN.md)

This repository contains the PACT post-route FPGA checkpoint optimization
agent, the Vivado and RapidWright MCP servers it drives, and the optimization
knowledge base. The 35 benchmark DCPs are included through Git LFS; proprietary
FPGA tools are not included.

## Layout

- `FDAgents/`: agent, typed skills, recipe planner, decision logic, and memory
- `baselines/codex_agent/`: free-form Codex Agent harness and reference results
- `baselines/datuner/`: DATuner checkpoint runner and reference results
- `RapidWrightMCP/`, `VivadoMCP/`: backend MCP servers
- `knowledge/`: optimization knowledge and experimental notes
- `benchmarks/`: 35 evaluation DCPs, manifest, and checksums
- `journals/`: manuscript source retained for artifact preparation
- `tests/`: unit tests that do not require benchmark DCPs
- `runs/`: generated run outputs (ignored by Git)

## Environment

The full artifact requires Python 3, Vivado 2025.1, Java, and a built
RapidWright checkout. Copy `.env.example` to `.env` and set local paths and the
LLM credential there. `.env` and generated DCPs are ignored by Git.

## Run

```bash
python -m pip install -r requirements.txt
python -m FDAgents.agent /path/to/design.dcp --model <model>
```

## Deterministic functional smoke test

Two no-LLM flows exercise both backends and are suitable for an initial
artifact check. Put the following contest checkpoints in one directory:

- `vexriscv_re-place_2025.1.dcp`
- `logicnets_jscl_2025.1.dcp`

Then run:

```bash
DCP_DIR=/path/to/checkpoints \
OUT_ROOT="$PWD/reproduction-smoke" \
PYTHON=/path/to/python \
VIVADO_EXEC=/path/to/vivado \
scripts/run_functional_smoke.sh
```

Pass `vexriscv` or `logicnets` to run only one design. The command fails if an
MCP backend returns an application-level error, no cell is moved in the
VexRiscv flow, Vivado reports any routing error, the output DCP is missing, or
the independently reopened checkpoint fails route signoff. Logs, checksums,
exit codes, signoff timing reports, and output DCPs are written below
`OUT_ROOT`.

Run the lightweight gate regression without Vivado:

```bash
python tests/test_smoke_checks.py
```

For structural and simulation-based comparison of an input and output DCP:

```bash
python validate_dcps.py input.dcp output.dcp \
  --precheck-vectors 50 --vectors 200
```

The full 35-design experiment and benchmark acquisition procedure will be
documented separately before artifact submission.

## License

PACT code authored by the FudanLLMEDA contributors is available under the
[MIT License](LICENSE). Files that retain an AMD copyright header and an
`SPDX-License-Identifier: Apache-2.0` marker remain under the
[Apache License 2.0](LICENSE-APACHE-2.0.txt); the root MIT license does not
relicense those files. The `RapidWright/` submodule and other third-party
dependencies remain subject to their respective upstream licenses. Vivado is
proprietary software and is not distributed in this repository.

## Snapshot

This artifact branch starts from the implementation snapshot dated 2026-06-25.
Its Git history is intentionally squashed to a single sanitized root commit.
Legacy benchmark file names and the `clk_fpl26contest` clock identifier remain
where required for compatibility with the evaluated DCPs.
