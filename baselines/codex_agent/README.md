# Codex Agent baseline

[English](README.md) | [简体中文](README.zh_CN.md)

This directory contains the free-form Codex Agent harness used as a baseline
in the PACT evaluation. The harness gives Codex the same FDAgents action
inventory and decision-policy text, but Codex decides how to invoke the native
Vivado and RapidWright MCP servers and how to manage its checkpoint trials.
It is prohibited from delegating back to PACT or reading the FDAgents source.

The scripts were recovered from the experiment snapshot at Git commit
`b5fb2e3`. Packaging changes are limited to repository-relative imports and a
provider-neutral name for optional OpenAI-compatible API endpoints.

## Contents

- `run_codex_dcp_harness.py`: run one or more manifest entries with Codex.
- `verify_harness_outputs.py`: independently reopen outputs in Vivado and
  check route, setup-derived Fmax, hold, and pulse-width timing.
- `usage_accounting.py`: extract token and cost records from Codex JSONL.
- `build_harness_manifests.py`: split a source manifest into train/test/all.
- `merge_harness_results.py`: merge result shards with header validation.
- `compare_harness_results.py`: compare verified Codex and PACT result tables.
- `manifest.csv`: the 35 benchmark inputs and the development/held-out split.
- `inventory.md`: the action inventory provided to the free-form agent.
- `reference_results.csv`: the validation-clean results reported in the paper.

## Requirements

- Python 3.10 or newer
- A Codex CLI supporting `codex exec --json`
- An API credential accepted by that Codex configuration
- AMD Vivado 2025.1 and a valid license
- The repository's Vivado and RapidWright native MCP servers configured in
  Codex with the names `vivado` and `rapidwright`
- The 35 Git LFS DCPs materialized under `benchmarks/`

The paper used model `gpt-5.5`, reasoning effort `xhigh`, and a one-hour
wall-clock limit per design. Configure the same reasoning effort in the Codex
configuration before reproducing the reported experiment.

## Installation and MCP registration

Run these commands from the **v1 artifact root**, with its Python environment
activated, dependencies installed, RapidWright built and Vivado on `PATH`:

```bash
npm install -g @openai/codex
codex --version
PACT_ROOT="$(pwd)"
PACT_PYTHON="$(python -c 'import sys; print(sys.executable)')"
export RAPIDWRIGHT_PATH="${RAPIDWRIGHT_PATH:-$PACT_ROOT/RapidWright}"
export CLASSPATH="$RAPIDWRIGHT_PATH/bin:$RAPIDWRIGHT_PATH/jars/*"

codex mcp add vivado -- \
  "$PACT_PYTHON" "$PACT_ROOT/VivadoMCP/vivado_mcp_server.py" \
  --vivado-path "$(command -v vivado)"
codex mcp add rapidwright \
  --env "RAPIDWRIGHT_PATH=$RAPIDWRIGHT_PATH" \
  --env "CLASSPATH=$CLASSPATH" \
  -- "$PACT_PYTHON" "$PACT_ROOT/RapidWrightMCP/server.py"
codex mcp list
```

Use a supported Node.js/npm installation for the selected Codex CLI release.
Record the installed CLI version; installing the current package does not pin
the historical experiment's CLI. The names must be exactly `vivado` and
`rapidwright`. If these names already exist, inspect their paths before
replacing them. Registration checks configuration, not server startup: run the
root no-LLM smoke test to exercise the backends and inspect the real baseline
run logs as well.

### Credentials, reasoning and gateway model

For the Vercel AI Gateway configuration supplied during AE:

```bash
export OPENAI_API_KEY="<your-evaluation-key>"
export OPENAI_BASE_URL="https://ai-gateway.vercel.sh/v1"
export CODEX_MODEL_PROVIDER="custom"
export CODEX_WIRE_API="responses"
```

Merge the following into `~/.codex/config.toml` (do not overwrite existing
configuration or duplicate table headers). Place `model_reasoning_effort`
before any section headers; `env_key` is an environment variable name, **not**
the secret itself:

```toml
model_reasoning_effort = "xhigh"

[model_providers.custom]
name = "Vercel AI Gateway"
base_url = "https://ai-gateway.vercel.sh/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

For this gateway, use **`--codex-model openai/gpt-5.5`** in the full-run command
below instead of `gpt-5.5`. For direct OpenAI access, use your configured OpenAI
provider and its model ID; unset the custom gateway variables. Export shell
variables explicitly: the Codex harness does not load the root `.env` for you.
Never put evaluation keys into commands committed to the repository.

## Dry run

This writes all 35 prompts and a result table without starting Codex or
Vivado:

```bash
python baselines/codex_agent/run_codex_dcp_harness.py \
  --manifest baselines/codex_agent/manifest.csv \
  --inventory baselines/codex_agent/inventory.md \
  --run-root runs/codex_agent-dry \
  --dry-run
```

## Full run

First check that `codex mcp list` reports both required servers. Then run:

```bash
python baselines/codex_agent/run_codex_dcp_harness.py \
  --manifest baselines/codex_agent/manifest.csv \
  --inventory baselines/codex_agent/inventory.md \
  --run-root runs/codex_agent \
  --codex-model gpt-5.5 \
  --time-limit 3600 \
  --timeout-grace 900 \
  --require-native-mcp \
  --require-usage
```

Set `OPENAI_BASE_URL` and `CODEX_MODEL_PROVIDER` only when using an
OpenAI-compatible endpoint instead of the provider already configured in
Codex. Use `--jobs N` only when the machine has enough Vivado licenses, memory,
and CPU capacity for `N` simultaneous runs.

The harness invokes Codex with `danger-full-access` because Vivado,
RapidWright, and the output checkpoint must be accessible. Run it in a
disposable artifact VM and do not expose unrelated files or credentials to
that VM.

## Independent verification

```bash
python baselines/codex_agent/verify_harness_outputs.py \
  --results runs/codex_agent/results.csv \
  --output runs/codex_agent/verified.csv \
  --vivado /path/to/vivado
```

The verifier does not trust the agent's final message. It reopens each output
DCP, produces new route and min/max timing reports, and classifies missing,
unrouted, hold-failing, pulse-failing, and route/timing-verified outputs. This baseline verifier does
not perform functional simulation or prove formal equivalence.

## Reference result

```bash
python baselines/codex_agent/summarize_results.py
```

As in the paper, normalized Fmax is floored at 1.0 when a method does not beat
the unchanged input. The expected geometric-mean improvements are +12.70% on
the 27-design development set, +0.47% on the 8-design held-out set, and +9.78%
overall. The reported mean runtime is 1,524 seconds and mean token cost is
$3.81 per DCP.

The original two-machine launcher is intentionally not included: it embedded
experiment-host paths and network topology. `run_codex_dcp_harness.py` is the
portable execution core and already supports sequential or local parallel
operation.
