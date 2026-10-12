# AE measured evidence

These JSON files contain selected fields from real Vivado 2025.1 runs on
2026-10-12. Runtime-specific filesystem paths are omitted; metrics and statuses
are not rewritten. See `../AE_FOLLOWUP_2026-10-12.md` for provenance and limits.

- `mini.json`, `fft.json`, `stress.json`: completed optimization and final checks.
- `boom-original-run.json`: the failed end-to-end run, retained as a failure.
- `boom-candidate-gate.json`: independent original-constraint timing/routing gate;
  its 32 GiB functional failure is not the final retry result.
- `boom-functional-32g.json`: original failed report plus an explicit OOM
  postmortem; the old validator did not classify that kill correctly.
- `boom-functional-64g.json`, `boom-functional-64g-run.json`: the subsequent
  passing structural/sampled-functional checks and normalized candidate hash.

Do not aggregate the BOOM recovery as a completed one-hour run. Do not substitute
these diagnostic measurements for the paper's historical 35-design results.
The ZIP includes original benchmark inputs and this evidence, not all generated
candidate DCPs or private/raw gateway logs.
