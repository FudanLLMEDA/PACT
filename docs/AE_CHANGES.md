# FPT'26 AE reusable-artifact corrections (v1.0.3)

This change is on the v1 maintenance line. It does not change v2/main,
or rewrite published tags. The release metadata records publication links and
checksums separately from the local validation record below.

## Review-item mapping

| Concern | Correction | Evidence / limitation |
| --- | --- | --- |
| Outdated main README | Complete input/install/run/verification/aggregation instructions | Root READMEs; executable batch dry-run |
| DATuner 1,200 s timeout text | v1.0.1 already changed runner defaults and READMEs to 3,600 s per trial | Preserve CSV trial budgets and historical reference results; refresh the archive |
| Missing Codex setup | Installation, exact native MCP registration, gateway/provider/model configuration | Baseline READMEs; registration is not an end-to-end startup test |
| No PACT machine-readable result / aggregation | Atomic versioned summary and manifest-based CSV/JSON aggregation | Failed/missing/unvalidated inputs and duplicate runs handled explicitly |
| Modified clock in exported DCP | Restore original timing XDC before candidate scoring and final export; compare clock snapshots after reopening | Requires real Vivado regression before publication |
| Routing-only skill checks | Common candidate route/hold/pulse/replay gate; default final structural/simulation gate | No claim of per-transition simulation or formal equivalence |

## Intentional behavioral changes

Skill-local `is_legal` still describes routing, not full validation. The agent's
common gate determines whether an improving candidate can replace current best.
Original timing exceptions are restored as well as clocks, so exploratory
false paths cannot leak into scoring/export. Functional comparison is a final
export gate, not an assertion about every intermediate candidate.

A skipped functional simulation (e.g. unsupported/encrypted IP) no longer exits
successfully. It remains documented evidence of an incomplete comparison.
A failed final gate does not publish the requested output; internal DCPs remain
for debugging. An explicitly skipped functional check produces a separately
labelled output excluded from validated aggregates.

Old reference CSVs are unchanged. Stricter checks and additional validation work
may change results and runtimes; they do not retrospectively certify old outputs.

## Publication checklist

1. Run unit tests, batch dry-runs, and real Vivado tests under 2025.1. Include a
   clock-tightening candidate, a rejected hold/pulse candidate, reopen checks,
   and structural/simulation comparison. Record exact command/tool versions.
2. Use the reviewers' six-design subset first, then the full suite if reporting
   full-suite new results. Do not substitute reference data for missing runs.
3. Create the new v1 release from the tested commit; leave prior tags intact.
4. Build the complete archive from that commit, include the pinned RapidWright
   submodule and materialized DCPs, and verify `benchmarks/SHA256SUMS`.
   Do not package `.env`, API keys, local Codex configuration or private logs.
5. Add an archive manifest with commit, dependency versions and archive SHA-256.
   Update the existing Figshare article using its authenticated owner workflow;
   GitHub release publication does not update Figshare automatically.
6. Download the published archive afresh, verify its checksum/content and smoke
   test it. Only then reply to reviewers with exact release/archive versions
   and the tested versus untested scope.

## Local validation on 2026-10-11

- `python -m pytest -q tests`: 58 passed (isolated Python test environment).
- PACT batch dry-run: 35 commands; repeated `--design` subset selection checked.
- Codex harness dry-run: 35 prompts and 35 `DRY_RUN` result rows.
- DATuner batch dry-run and both baseline reference aggregators passed; historical
  overall improvements remain +15.14% and +9.78%, respectively.
- Python compilation, CLI help and `git diff --check` passed.
- No Vivado executable was found in this development environment. Clock restoration,
  actual replay and functional simulation have **not** been validated with real DCPs here.
- For release packaging, all 35 benchmark DCPs were materialized and checked against
  `benchmarks/SHA256SUMS`; the pinned RapidWright submodule and both nested submodules
  were fetched. The ZIP contains actual DCPs and source, not LFS pointers.
- `scripts/package_artifact.py` packages tracked files only and generates a per-file
  integrity manifest plus ZIP SHA-256. The Figshare publisher creates a new record,
  retains the previous record, verifies uploaded checksums and public availability,
  and reads its API token only from the environment. Publication is separate from
  the unit-test and dry-run claims above.
