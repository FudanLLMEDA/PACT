# PACT FPT'26 Paper Artifact — v1 maintenance line

[English](README.md) | [简体中文](README.zh_CN.md)

This is the **FPT'26 paper artifact (v1)**, not the FPL'26 contest artifact
on the v2/main line. Use the exact v1 release supplied with the AE archive.
The benchmark manifest contains 35 original post-route DCPs (27 development,
8 held-out). Vivado and its license are not distributed.

## 1. Obtain and check the inputs

From a Git checkout of the supplied v1 release:

```bash
git submodule update --init --recursive
git lfs install
git lfs pull
(cd benchmarks && sha256sum -c SHA256SUMS)
```

From the complete AE archive, run the checksum command too. A GitHub source
ZIP is not a substitute for the complete archive: it may omit submodule
contents or contain Git LFS pointers instead of DCPs. See
[benchmark documentation](benchmarks/README.md).

## 2. Environment

Use Linux, Python 3.10+, Vivado **2025.1** with a valid license, Java 11+,
and a built RapidWright checkout. Run commands below from the artifact root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
source /path/to/Vivado/2025.1/settings64.sh
export VIVADO_EXEC="$(command -v vivado)"
make build-rapidwright
export RAPIDWRIGHT_PATH="$PWD/RapidWright"
export CLASSPATH="$RAPIDWRIGHT_PATH/bin:$RAPIDWRIGHT_PATH/jars/*"
cp .env.example .env  # once; do not overwrite an existing configuration
```

Fill in local paths and the credential in `.env`, or export the variables in
the shell. Never commit credentials. For the AE gateway configuration:

```bash
export OPENAI_BASE_URL="https://ai-gateway.vercel.sh/v1"
export OPENAI_API_KEY="<your-evaluation-key>"
export FDAGENTS_MODEL="openai/gpt-5.5"
```

A direct OpenAI endpoint uses its own model ID (`gpt-5.5`) instead. Do not
mix an endpoint's model identifiers with another provider's. Record tool and
package versions with each experiment (`vivado -version`, `java -version`,
`python -m pip freeze`; also `codex --version` for the baseline).

## 3. No-LLM smoke test

```bash
DCP_DIR="$PWD/benchmarks" PYTHON="$(command -v python)" \
  OUT_ROOT="$PWD/runs/smoke" scripts/run_functional_smoke.sh
```

This runs VexRiscv and LogicNets and checks backend operation and route replay.
**The smoke test is not full functional/timing signoff.** Its timing reports
are evidence, not an assertion that hold, pulse-width or equivalence passed.

## 4. Run PACT

One design, with the paper's 3,600-second optimization budget:

```bash
python -m FDAgents.agent benchmarks/amd_mini-isp_2025.1.dcp \
  --output runs/pact/mini-isp/output.dcp \
  --run-dir runs/pact/mini-isp --model "$FDAGENTS_MODEL" --time-limit 3600
```

Use fresh output/run paths. Preview all 35 commands, then run them sequentially:

```bash
python scripts/run_pact_batch.py --model "$FDAGENTS_MODEL" --dry-run
python scripts/run_pact_batch.py --model "$FDAGENTS_MODEL" --run-root runs/pact-full
```

Select a subset with repeated `--design` options using exact names from
`benchmarks/MANIFEST.csv`, e.g. `--design "AMD Mini-ISP" --design "BOOM SoC"`.
`--no-llm` on the single-design runner is a diagnostic mode, not the paper's
LLM experiment. Known-benchmark replay remains opt-in, not a default.

### Output and validation contract

- `summary.json`: schema version, input hash, configuration, trial decisions,
  baseline/final metrics, validation states, elapsed time and LLM token/cost totals.
  Written incrementally and on handled failure/interruption; an OS kill can
  leave `status: running`, which is **not** a completed result.
- `memory.json`: internal decision history (not the stable aggregation API).
- `constraints/`: input timing XDC and clock names/periods/waveforms.
- `validation/round_*/`: reports for promising candidates; `validation/final/`:
  reopened final-checkpoint reports and structural/simulation evidence.
- `current_best.dcp`, skill DCPs and `final_candidate.dcp`: **internal candidates**,
  not certified outputs. Failed final checks leave evidence here, not an output
  at the requested export path.

Before accepting an improving candidate, the common gate restores the input's
exported timing constraints (including original clocks and timing exceptions),
writes and reopens a checkpoint, and requires complete routing, available setup
measurement, nonnegative hold and pulse-width slack. WNS is remeasured under
those constraints, not trusted from the skill's exploratory clock target.
Fmax is the setup-derived estimate `1000 / (original_period_ns - WNS_ns)`,
not a guarantee of operation at that frequency on every clock domain.
Negative setup slack is permitted: the task is improving Fmax of timing-violating
inputs, not claiming closure at the original clock frequency.

Final export repeats that gate and, by default, runs the independent structural
and 200-vector simulation comparison against the input (50-vector precheck).
A skipped simulation, missing report or timeout is not a pass. This is **not a
formal equivalence proof**, nor is functional simulation performed on every
intermediate transition. `--skip-functional-check` is an explicitly unvalidated
export mode; those runs are excluded from validated aggregation.

The optimization deadline is checked between actions; in-flight tool work may
finish after it. Candidate gates consume optimization time. Final signoff is
additional work; `--validation-timeout` (default 1,800 seconds) bounds the final
structural/simulation subprocess, not the separate Vivado report calls.
`optimization_wall_time_s` and total `wall_time_s` are recorded separately.
The stricter gates can change accepted candidates and runtime relative to the
historical implementation; **the paper's numbers have not been regenerated by
this maintenance change**. See [AE changes](docs/AE_CHANGES.md).

## 5. Aggregate new PACT runs

```bash
python scripts/summarize_pact_results.py runs/pact-full \
  --output-dir runs/pact-full-aggregate
```

This produces per-design `results.csv` and `aggregate.json` for development,
held-out and overall sets. Scoring uses `max(1, final_Fmax / manifest_original_Fmax)`
and a geometric mean. Runtime/cost means include explicit sample counts.
The freshly measured baseline is also retained in the CSV.
Missing, failed and unvalidated runs remain visible and are excluded from the
mean; coverage counts and `complete` prevent a subset mean being mistaken for
35-design replication. Duplicate runs are rejected, not cherry-picked.
These are **new-run results**, not reconstructed historical reference results.

## 6. DATuner baseline

Follow [DATuner setup and commands](baselines/datuner/README.md).
Use `trial_counts.csv` for per-design trial budgets. The corrected timeout is
**3,600 seconds per Vivado trial**, not 1,200 seconds and not a one-hour total
budget for DATuner. The CSV does not contain timeout settings.

```bash
python baselines/datuner/run_batch.py --dry-run
python baselines/datuner/run_batch.py --output-dir runs/datuner
python baselines/datuner/summarize_results.py
```

The last command summarizes the **historical reference CSV**, not the run above.

## 7. Codex Agent baseline

First complete [Codex installation, MCP registration, credentials and model
configuration](baselines/codex_agent/README.md#installation-and-mcp-registration).
Registration alone does not prove that either MCP server starts successfully.
The guide includes dry-run, full-run, independent verification and reference
aggregation commands. Use the same inputs and subset as PACT/DATuner.

## 8. Tests and independent functional comparison

```bash
python -m pip install pytest
python -m pytest -q tests
python validate_dcps.py input.dcp output.dcp \
  --precheck-vectors 50 --vectors 200 --report-json runs/comparison.json
```

Unit tests do not require Vivado; actual optimization and DCP verification do.
Simulation is sampled functional evidence, not formal equivalence certification.

## Layout

`FDAgents/` contains the agent and typed skills; `VivadoMCP/` and
`RapidWrightMCP/` the backend servers; `scripts/` the smoke/batch/aggregation
entry points; `baselines/` baseline runners and reference CSVs; `knowledge/`
optimization notes; `journals/` manuscript source; `benchmarks/` original inputs.

## License

PACT code authored by the FudanLLMEDA contributors is available under the
[MIT License](LICENSE). Files that retain an AMD copyright header and an
`SPDX-License-Identifier: Apache-2.0` marker remain under the
[Apache License 2.0](LICENSE-APACHE-2.0.txt); the root MIT license does not
relicense those files. The `RapidWright/` submodule and other third-party
dependencies remain subject to their respective upstream licenses. Vivado is
proprietary software and is not distributed in this repository.


## Version provenance

The v1 line starts from the sanitized 2026-06-25 implementation snapshot.
Maintenance commits follow it without rewriting the existing v1 or v2 tags.
Historical benchmark file names and `clk_fpl26contest` remain for compatibility.
