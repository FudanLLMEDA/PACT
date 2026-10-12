# AE runtime reference and timeout guidance

## Reference measurements (2026-10-12)

Runtime depends on CPU performance, allocated resources, host load, storage and
LLM latency. Matching the core count alone does not match the paper machine's
performance. Reviewers using a slower or shared machine may need larger time
budgets to exercise the complete flow.

The measurements below are **single AE diagnostic runs, not paper runtime
results or guaranteed completion times**. They used Vivado 2025.1, GPT-5.5 via
the supplied gateway, and Ubuntu 22.04 containers allocated 8 pinned logical
CPUs and 32 GiB RAM on Intel Xeon Gold 6148 hosts. The paper specifies an Intel
Core i7-12700 with an eight-core/32 GB resource limit. These environments are
not performance-equivalent; the observed differences do not isolate CPU speed
from other sources of runtime variation.

| DCP | Optimization phase | Additional finalization / validation | Total wall time | Outcome |
| --- | ---: | ---: | ---: | --- |
| AMD Mini-ISP | 345 s (5.7 min) | 207 s (3.5 min) | 552 s (9.2 min) | Final route/timing, structural and sampled functional checks passed |
| Double-Clock FFT-L | 442 s (7.4 min) | 441 s (7.3 min) | 883 s (14.7 min) | Final route/timing, structural and sampled functional checks passed |
| Stress FFT-CORDIC-M | 2,499 s (41.7 min) | 1,378 s (23.0 min) | 3,877 s (64.6 min) | Final route/timing, structural and sampled functional checks passed |
| BOOM SoC | 3,856 s (64.3 min) | 2,391 s (39.8 min) | 6,247 s (104.1 min) | Failed run: optimization tool timeout and final functional-validation timeout; no validated optimized output |

Optimization and total times come from `optimization_wall_time_s` and
`wall_time_s` in each `summary.json`. Additional time is their difference, not a
separately measured simulation duration; it also includes final report calls,
checkpoint handling and cleanup. Values are rounded independently. Every run
requested `--time-limit 3600 --validation-timeout 1800`. In-flight operations
and timeout recovery can exceed the nominal optimization budget.

For scheduling, these observations suggest allowing roughly 10–15 minutes for
the two smaller examples and over an hour for Stress FFT-CORDIC-M on a similar
host, with additional headroom for variation. **BOOM's 104-minute failed run is
not a successful runtime estimate or a sufficient budget recommendation.**

The candidate produced after that tool timeout was subsequently verified
independently: restoring the original 1.569 ns clock and reopening it measured
**89.654 MHz**, with route/hold/pulse/minimum-period checks passed. Structural
and sampled functional verification failed at **32 GiB** because xelab was
killed by the container memory limit, then **passed at 64 GiB in 2,426 seconds
(40.4 minutes)**: 4/4 structural checks, a 50-cycle precheck and a 200-cycle full
comparison, all with zero functional or protocol mismatches. The independent
constraint/reopen/timing gate took another 372 seconds (6.2 minutes).

This is post-budget candidate recovery and independent verification, **not** a
successful end-to-end one-hour/32 GB run. Do not add the failed-run and retry
times and report that sum as a fresh optimization measurement. Allow at least
64 GiB for reproducing this particular standalone BOOM functional check on a
similar host; 64 GiB was sufficient in this run, not a universal guarantee.
The original failed run and sanitized final evidence are retained in
[`ae_evidence/`](ae_evidence/).

## Which timeout to adjust

- `--time-limit`: optimization budget (paper setting: 3,600 seconds). Candidate
  checks consume this budget; the deadline is checked between actions.
- `--validation-timeout`: separate final structural/simulation subprocess
  allowance (default: 1,800 seconds). Independent Vivado report calls and other
  finalization work are not all covered by this limit.
- Individual skills also have tool/recipe timeouts. Increasing `--time-limit`
  does **not** automatically increase them. Use `--recipe-timeout-scale` to
  scale explicitly configured `recipe_timeout_s` values; default `1` preserves
  the existing limits. For example, scale `2` changes the selected
  `target_period_fast_route_floor` recipe from 3,000 to 6,000 seconds. It does not
  change the optimization deadline, action selection, or every backend timeout.
- `--elaboration-timeout`: xelab's inner timeout (default: 3,600 seconds), also
  available on `validate_dcps.py`. The successful BOOM retry allowed 5,400
  seconds for elaboration and 7,200 seconds for the whole independent check.
  Keep the outer final-validation allowance larger than the elaboration limit
  to leave room for netlist export, compilation and simulation.

An example of a **longer-budget diagnostic**, not a paper-budget replication:

```bash
python -m FDAgents.agent benchmarks/boom_soc_2025.1.dcp \
  --output runs/boom-extended/output.dcp \
  --run-dir runs/boom-extended --model "$FDAGENTS_MODEL" \
  --time-limit 7200 --recipe-timeout-scale 2 \
  --validation-timeout 7200 --elaboration-timeout 5400
```

This extended-budget end-to-end command has not yet been verified to produce
a successful BOOM output; it exposes the relevant limits without source edits.
The measured success above was independent validation of a recovered candidate.
Use a fresh run directory; keep the original failed run and
logs. Do not disable routing, hold, pulse-width, constraint-restoration or
functional checks to make a run pass.

Report the hardware, resource allocation, tool/model versions, all timeout
settings, actual optimization/total times and final validation status. Keep
extended-budget runs separate from the paper's one-hour comparison: they can
demonstrate executability, but do not establish the paper's time-efficiency
claim. A timeout alone is neither a functional mismatch nor a validation pass.

To verify an existing candidate without re-running optimization, first restore
its original timing constraints and complete the checkpoint gates described in
[the follow-up](AE_FOLLOWUP_2026-10-12.md). Then, on a host with sufficient RAM:

```bash
python validate_dcps.py original.dcp normalized.dcp \
  --precheck-vectors 50 --vectors 200 --elaboration-timeout 5400 \
  --work-dir runs/boom-functional --report-json runs/boom-functional.json
```

The validator distinguishes compiler/elaborator failure (including a killed
tool) from observed output mismatches. A killed process is not automatically
proof of OOM; check the host/container memory events. It is never a pass.

## Measurement provenance

The retained source summaries are under `/data/qlli/fdagents/work/`:

| Host | Relative summary path |
| --- | --- |
| server-120 | `pact-ae-full-mini-20261012/PACT/runs/full-mini/summary.json` |
| server-120 | `pact-ae-full-fft-20261012/PACT/runs/full-fft/summary.json` |
| server-118 | `pact-ae-full-stress-20261012/PACT/runs/full-stress/summary.json` |
| server-120 | `pact-ae-full-boom-fix2-20261012/PACT/runs/full-boom/summary.json` |
| server-120 | `pact-ae-v1-20261012/boom-candidate-check-64g-20261012/functional/functional.json` |

These are maintainer-side evidence paths, not files reviewers must have on their
machines. The measurements document the AE maintenance work incorporated into v1.0.4,
not a rerun of the entire paper suite. The older v1.0.3 archive is unchanged.
