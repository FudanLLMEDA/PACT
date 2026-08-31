# logicnets_jscl Optimization Strategy Notes

Date: 2026-05-16

## Best Verified Result

- Benchmark: `logicnets_jscl`
- Best DCP: `runs/ma_redeploy_20260513_163244/small_polish/logicnets_jscl_followup1/auto_top1_top3_explore.dcp`
- Verified WNS: `-0.305 ns`
- Clock period: `1.500 ns`
- Fmax: `554.016620 MHz`
- Route status: `28007 / 28007` routable nets fully routed, `0` route errors
- Verification report: `runs/ma_redeploy_20260513_163244/agent_small_mid_new_methods_banach/verify_logicnets_554/verify_results.csv`

Fmax formula used by the local scripts:

```text
Fmax MHz = 1000 / (period_ns - WNS)
```

For this result: `1000 / (1.500 - (-0.305)) = 554.016620 MHz`.

## Effective Optimization Path

The best local path was incremental. The important pattern was to accept only legal routed improvements, then keep polishing the current best rather than restarting from a broad placement search.

| Stage | Representative DCP | WNS | Fmax | What Helped |
| --- | --- | ---: | ---: | --- |
| Original agent-from-zero path | `runs/agent_logicnets_from_zero_current_20260513_143338/out.dcp` | `-0.376` | `533.05 MHz` | pblock and fanout/replication style exploration got a legal baseline above 530 MHz. |
| Route/phys-opt directive sweep | `runs/manual_logicnets_550_explore_20260512_135342/route_directive_sweep_203417/route_HigherDelayCost_physExplore.dcp` | `-0.355` | `539.08 MHz` | `route_design` directive sweep followed by `phys_opt_design -directive Explore`. |
| LUT pin swap pass 1 | `runs/lut_pin_swap_directive_sweep_20260513_105502/...AggressiveExplore.dcp` | `-0.348` | `541.13 MHz` | RapidWright LUT input pin remapping on critical-path LUTs, then reroute/phys-opt. |
| LUT pin swap pass 2 | `runs/lut_pin_swap_directive_sweep2_20260513_111031/lut_pin_swap_iter01_01_layer1_inst_layer1_N43_inst_data_out_131__i_30_I2_A3_AggressiveExplore.dcp` | `-0.322` | `548.85 MHz` | Re-profile after each accepted change; a second critical-path LUT became actionable. |
| Post-route cleanup | `runs/post551_cleanup_sweep_20260513_124703/02_phys_explore.dcp` | `-0.313` | `551.57 MHz` | Small late `phys_opt_design -directive Explore` cleanup preserved legality and improved WNS. |
| Targeted net reroute polish | `runs/ma_redeploy_20260513_163244/small_polish/logicnets_jscl_followup1/auto_top1_top3_explore.dcp` | `-0.305` | `554.02 MHz` | Unroute a tiny set of critical nets, `route_design -directive Explore`, then `phys_opt_design -directive Explore`. |

## What Was Learned

### 1. Keep the flow timing-report driven

The useful choices came from the current timing graph after each accepted DCP:

- extract the top setup path;
- collect nets with large route delay on that path;
- include a small number of adjacent/high-fanout nets when they appear on the same critical cone;
- rerun timing after each accepted transform before choosing the next action.

This avoids benchmark-name-specific logic. The agent should learn "critical path has route-dominated nets with limited local fanout" rather than "logicnets needs command X".

### 2. Small local reroutes beat broad re-placement late in the flow

The late-stage winning polish did not move a large fraction of the design. It selected only three nets from the current critical path:

```text
layer0_inst/layer0_N16_inst/data_out[49]_i_5_n_0
layer0_inst/layer0_N16_inst/data_out[48]_i_15_3
layer0_inst/layer0_N16_inst/data_out[49]_i_13_n_0
```

Then it used:

```tcl
route_design -directive Explore
phys_opt_design -directive Explore
```

The agent-relevant rule is: when WNS is close to the best known state and the critical path is route-delay dominated, try an extremely small unroute/reroute set first. Larger unroute sets sometimes improved intermediate routing freedom but often lost WNS after reroute.

### 3. LUT pin swaps are valuable after placement is already good

RapidWright LUT pin remapping worked because the critical path contained LUT input pins where the fastest physical input was not being used. The safe form is:

- inspect critical-path LUT pins;
- swap only when RapidWright/Vivado can prove the alternate physical pin is legal;
- reroute affected nets;
- accept only if final route status is clean and WNS improves.

The second LUT-swap pass mattered because the first pass changed the critical path. Candidate discovery must be iterative, not a one-shot list from the original DCP.

### 4. Directive sweeps should be cheap and staged

The useful directives were ordinary Vivado commands applied to the current best:

- `phys_opt_design -directive Explore`
- `phys_opt_design -directive AggressiveExplore`
- `route_design -directive HigherDelayCost`
- `route_design -directive MoreGlobalIterations`
- `route_design -directive AggressiveExplore -tns_cleanup`
- `route_design -directive Explore`

The pattern that repeatedly worked was route first, then `phys_opt_design -directive Explore`. For this design, many directives converged to identical WNS values, so the value is in a cheap sweep with fast rejection, not in spending long time on one directive.

### 5. Treat unrouted or partially routed timing as invalid

One later experiment produced apparently excellent timing while the design was not fully routed. It was rejected. Any agent policy must gate acceptance on route legality:

- routable nets equals fully routed nets;
- route errors equals zero;
- no unresolved placement/routing state;
- write and reopen the DCP when possible before final scoring.

## Suggested Agent Skill Abstractions

These are generic skills that can transfer to anonymous benchmarks:

1. `critical_net_reroute`

   Extract worst setup paths, rank nets by route delay and local fanout, unroute a small selected set, reroute with a directive, then run post-route phys-opt. Parameters should include max paths, net selection mode, max nets, route directive, and post phys-opt directive.

2. `iterative_lut_pin_swap`

   Inspect critical-path LUT pins with RapidWright, apply one legal pin swap at a time, reroute, and re-profile. The skill should stop when no actionable pin remains or WNS stops improving.

3. `post_route_cleanup`

   Try short post-route cleanup sequences from the current best. Keep this late in the decision tree because it gives small gains and assumes the design is already near a good routed state.

4. `directive_micro_sweep`

   Run a bounded set of route/phys-opt directive combinations against the same input DCP and accept only legal WNS improvements. Store all rejects with timing and route status for later policy learning.

## Reproducibility Notes

Final winning polish provenance:

- Starting point for follow-up polish: `runs/ma_redeploy_20260513_163244/small_polish/logicnets_jscl_followup1/baseline_copy.dcp`
- Final command log: `runs/ma_redeploy_20260513_163244/small_polish/logicnets_jscl_followup1/auto_top1_top3_explore_commands.txt`
- Selected nets: `runs/ma_redeploy_20260513_163244/small_polish/logicnets_jscl_followup1/auto_top1_top3_explore_selected_nets.tsv`
- Final independent verification: `runs/ma_redeploy_20260513_163244/agent_small_mid_new_methods_banach/verify_logicnets_554/verify_results.csv`

The final result should be described as a verified local result, not a guaranteed contest-harness result, until it is submitted or revalidated through the official harness.
