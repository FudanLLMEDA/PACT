# FDAgents Skill And Prompt Inventory

## FDAgents Prompt Source

- Source: `FDAgents/SKILL.md`
- Characters: `25822`
- Forbidden hint matches: `0`

## Registered Skills

- `cell_replace`: Re-place critical-path cells to reduce wire detour.
- `checkpoint_import`: Open and adopt a local known-good DCP.
- `clock_tighten`: Sweep tightened clock periods with a bounded fresh place/route recipe.
- `critical_cluster_anchor`: Timing-driven soft bbox anchor around current critical SLICE cells.
- `critical_net_reroute`: Sweep small critical net windows with route-preserving Vivado ECO.
- `custom` deep-only: Direct pass-through to any MCP tool. Deep-mode only.
- `endpoint_bel_move`: Try same-site FF->FF2 moves for endpoint registers on critical paths.
- `fanout_opt`: Split high-fanout nets using RapidWright, then re-route in Vivado.
- `force_replicate`: Global unroute + double force replication + route recovery.
- `fresh_place_route`: Try bounded full place/route recipes from current best DCP.
- `lut_merge`: Merge LUT input cones on critical paths via RapidWright ECO.
- `lut_pin_swap`: Swap critical LUT inputs onto faster physical pins.
- `path_local_lut_reflow`: Sweep bounded path-local LUT reflow candidates.
- `pblock`: Constrain design to a pblock, then re-place and re-route.
- `pblock_sweep`: Sweep candidate pblock ranges, then keep the best legal result.
- `per_net_unroute`: Per-net unroute + double force_replicate + route. Vivado-only.
- `phys_opt`: Runs phys_opt_design with the given directive.
- `post_route_cleanup`: Sweep generic post-route cleanup Tcl recipes from current best DCP.

## Decision Mechanism

- `FDAgents/decision.py`: `quick_filter`, rollback, same-skill cooldown, deep-analysis trigger.
- `FDAgents/recipe_planner.py`: `RecipePlanner` profile-to-recipe mapping, confidence, core skills, initial params.
- `FDAgents/agent.py`: LLM action validation, recipe-core gating, blacklist checks, time-budget checks, fallback picker.
- `FDAgents/memory.py`: compact state exposed to the LLM and harness policy.

## No-Cheat Constraint

- Harness prompts must not add remembered design, DCP, hierarchy, net, path, or coordinate names.
- Codex and FDAgents outputs must be verified by reopening DCPs and checking route, setup-derived Fmax, hold, and pulse.
