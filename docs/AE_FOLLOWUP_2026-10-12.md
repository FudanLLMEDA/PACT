# AE execution follow-up (2026-10-12; updated 2026-10-13 for v1.0.4)

The reviewer could run PACT but did not obtain validated final outputs. Their
four run logs have not yet been supplied here, so this note does not assign a
root cause to each failure or claim that the paper's results now replicate.

## Confirmed defects and changes

- DATuner passed relative `-source` and resource-log paths after switching the
  subprocess working directory. Resolve output/trial directories before building
  those paths. This is a runner defect, not merely an evaluator environment issue.
- The Vivado bridge sent multiline Tcl directly to a REPL and returned at its
  first prompt. With real `tclsh` and a PTY, a clock snapshot was returned to the
  *next* request; the original snapshot parser saw no clocks. Source the complete
  script with one physical input line and unique begin/end markers instead.
  Preserve that response boundary across timeouts, including errors completing
  after the timeout. Do not flatten newlines in Tcl comments or string literals.
- Query actual Vivado project state before checkpoint reopening; do not trust a
  Python flag that raw Tcl or a timed-out command can leave stale. A checkpoint
  reopen closes the previous project before loading the new DCP.
- Treat the server's `Error:` responses and MCP `isError` flag as errors. Forward
  the requested backend timeout, with five seconds of client response grace.
- The unconstrained `mcp>=1.0.0` dependency installed MCP 2.3.0 here, whose Server
  lacks the v1 `list_tools` decorator used by both servers. Constrain all three
  dependency lists to `mcp>=1.0.0,<2`; the tested version is 1.30.0.

## Executed checks

- Actual streaming gateway requests using `https://gproxy.lin.pub/codex/v1` and
  `gpt-5.5`: PACT `analyze_timing` and `decide_next_action` both returned JSON.
  These used synthetic timing summaries, not an FPGA checkpoint. Credentials
  were supplied interactively and were not written into source or result files.
- Real Tcl interpreter/PTY regressions cover multiline output, fake prompts
  inside report output, long scripts, large responses, syntax/tool errors,
  timeout recovery, completion-before-prompt timeouts and EOF cleanup.
- An actual MCP stdio server subprocess is exercised against a Tcl-only test
  backend. This tests MCP startup, request/response handling and error recovery;
  it is explicitly not Vivado, routing, timing or functional-equivalence evidence.
- A project-state test uses Tcl stubs to reproduce a stale Python state flag;
  it does not prove behavior on a real Vivado checkpoint.

Run the transport checks with:

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q tests/test_vivado_transport.py
python -m pytest -q tests
```

`tclsh` is needed for the real interpreter/PTY checks; those tests explicitly
skip when it is absent. The source requires the MCP 1.x API.

Final packaging regression: **90 tests passed**, including real Tcl/PTY and MCP
stdio tests plus recipe-budget and infrastructure-failure classification tests.
`pip check`, Python compilation and `git diff --check` also passed.

## Remote validation on the available lab servers

The user identified `server-120` and `server-118`. Tests use registered fd-slot
containers with 8 pinned logical CPUs, 32 GiB memory, host networking for the
node-locked license, Ubuntu 22.04 and Vivado 2025.1. All run data lives under
`/data/qlli/fdagents/work/`, not the system disk. Other users' tasks are untouched.
The RapidWright source pinned by this artifact (`f63afef5`) was built separately;
we did not substitute the other project's modified optimizer classes.

Real preflight exposed command-echo contamination while repairing the transport:
the actual clock was 1.570 ns but an echoed `-max_paths 1` was parsed as 1.0 ns.
Vivado scripts now use `source -notrace`; clock period parsing requires a dedicated
`PERIOD_NS:` marker instead of accepting the first numeric token. Repeated real
checkpoint gates report 1.570 ns, WNS -1.686 ns and the expected 307.125 MHz baseline.

A one-round AMD Mini-ISP diagnostic completed the actual gateway/LLM, RapidWright,
Vivado, rollback and final structural/simulation flow. Its candidate regressed and
was correctly rejected; the original 307.125 MHz output was validated. This was
an infrastructure diagnostic, not an optimization-gain claim.

Full-budget tests use the four designs recommended earlier in the AE discussion,
not an assertion about the unidentified four reviewer logs. Each gets a
3,600-second optimization budget and a separate final-validation allowance.
Inputs are copied to a dedicated run directory before execution. Initial setup
attempts with an expired temporary tunnel or overlapping input staging remain
recorded separately; they are not successful measurements. Final runs use a
persistent SSH tunnel to the same gateway and frozen, per-run inputs.

Results obtained so far (measured baseline -> final, MHz):

| Design | Baseline | Final | Final verification |
| --- | ---: | ---: | --- |
| AMD Mini-ISP | 307.125 | 392.619 | Routing/hold/pulse/reopen and structural/sampled simulation passed |
| Double-Clock FFT-L | 442.478 | 508.647 | Routing/hold/pulse/reopen and structural/sampled simulation passed |
| BOOM SoC | 48.237 | 89.654 (recovered candidate) | Independent original-constraint gate and 64 GiB structural/sampled simulation passed; not a successful one-hour end-to-end run |
| Stress FFT-CORDIC-M | 343.879 | 369.276 | Routing/hold/pulse/reopen and structural/sampled simulation passed |

See [AE runtime reference](AE_RUNTIME.md) for measured optimization and total
times, machine details, and the distinction between longer-budget diagnostics
and the paper's one-hour comparison.

The DATuner relative-path smoke test also completed one real Vivado trial with
exit code 0 (`num_ok=1`, `num_fail=0`). This verifies the runner path, not its full
published trial budget or independent functional signoff.

## BOOM recovery and verification evidence

The 3,600-second optimization run failed on a 3,000-second recipe timeout; its
original-input fallback later failed the final functional-check timeout. The
backend completed a candidate after the timeout. That original run's
`summary.json` remains `validation_failed`; it has not been relabelled successful.

The recovered candidate was separately normalized to the input's original
1.569 ns clock, written and reopened. Measured WNS is -9.585 ns, giving
89.653936 MHz; all 273,660 routable nets are routed, with zero routing errors,
WHS +0.010 ns and zero failing hold/pulse endpoints. Minimum-period slack is
0.000 ns. The normalized DCP SHA-256 is
`e14f34da747fdf259dc67722c07a7cd4f96ea6115b9ff894a30bf12144643a2b`.

Standalone verification at 32 GiB passed structural checks but xelab was killed.
The container reached its 34,359,738,368-byte limit and recorded an OOM kill.
The retry used the **same DCP hash**, eight pinned logical CPUs and 64 GiB RAM,
with a 5,400-second elaboration allowance and 7,200-second overall verification
allowance. It completed in 2,426 seconds on 2026-10-12: structural 4/4, 50-cycle
precheck and 200-cycle full simulation passed, with zero functional/protocol
mismatches. This is sampled evidence, not formal equivalence.

The timeout/reopen fix was also exercised independently on real Vivado: a
pending long command was interrupted by restarting the backend before reopening
a saved checkpoint; the old PID disappeared and restored clock/WNS matched.
No validation criterion was relaxed. v1.0.4 exposes `--recipe-timeout-scale`
and `--elaboration-timeout`, records them in summaries, and classifies
compiler/elaborator termination as infrastructure failure rather than an
observed functional mismatch. Defaults remain unchanged.

Sanitized measured fields and the failed-run evidence are in
[`ae_evidence/`](ae_evidence/). They predate the final packaging-only changes and
new runtime/reporting controls; the final release also runs unit tests and
35-design dry-runs, not another full FPGA sweep.

## Remaining scope limitations

- BOOM is a recovered, independently validated output, not a demonstrated
  one-command one-hour/32 GB success. Its 89.654 MHz is below the paper's 91.9 MHz.
- Three other designs completed the optimization/final-validation flow; these
  four examples are not a fresh 35-design replication or a recreation of the
  unidentified four reviewer runs.
- DATuner has one real-trial runner smoke check here, not a new full baseline
  campaign. Codex setup/dry-run evidence is not a new full Codex campaign.
- Release/upload/public-download verification is separate from these tests.
  See release metadata for the actual published commit, ZIP hash and DOI.
