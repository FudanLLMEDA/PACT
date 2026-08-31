# RapidWright ECO Optimization Knowledge Base

Last updated: 2026-05-06

Purpose: this file is the condensed source-scan memory for future agents. Read this before reopening large parts of `RapidWright/`.

## How To Read This File

Use the first half as the fast index, then jump into the appendices when you need implementation detail.

- If you need to write a new RapidWright optimization script, read `TL;DR`, `Required Normalization`, `Placement And ECO`, `Routing And Timing APIs`, and `Minimal Java Skeleton`.
- If you need to understand a DCP object graph, read `Core Object Model`, then `Appendix A`.
- If you need to move cells safely, read `Placement And ECO`, then `Appendix C`.
- If you need to rip up or reroute only part of a net, read `Routing And Timing APIs`, then `Appendix B`.
- If you need to wire this into the checkpoint agent, read `Repository Integration`, then `Appendix E`.
- If future context is tight, feed the target agent only the relevant appendix plus the top `TL;DR`.

This document intentionally duplicates some points between index and appendix. The index is optimized for navigation; appendices are optimized for agents that need enough detail to act without rescanning the repository.

## TL;DR

- Local RapidWright checkout is a submodule at `RapidWright/`, currently around `v2025.2.1`.
- Many core classes are not in `RapidWright/src`: `Design`, `Cell`, `Net`, `SiteInst`, `SitePinInst`, `Device`, `Tile`, `Node`, `PIP` come from `RapidWright/jars/rapidwright-api-lib-2025.2.1.jar`. Use `javap` or the javadoc jar for those APIs.
- Vivado is the scoring/timing authority. RapidWright is best used for fast structural analysis, controlled ECO edits, route-tree inspection, and DCP generation.
- For routed contest DCPs, do not use raw `Cell.unplace()` or broad `net.unroute()` casually. Prefer `DesignTools.fullyUnplaceCell`, `DesignTools.unroutePins`, deferred pin removals, and `PartialRouter` when using RW to repair routing.
- Always normalize/check designs after RW mutation: `DesignTools.makePhysNetNamesConsistent`, `DesignTools.createMissingSitePinInsts`, `DesignTools.updatePinsIsRouted`, then `ReportRouteStatus`.
- Contest timing target is `clk_fpl26contest`, not necessarily overall WNS from `report_timing_summary`.
- Legal transformations must preserve cycle behavior. Replication, relocation, pblock-driven re-place, LUT pin swaps, and retiming are plausible. Adding pipeline latency is disqualifying.

## Source Topology

Important directories:

- `RapidWright/src/com/xilinx/rapidwright/design`: helpers, module APIs, `DesignTools`, `NetTools`, pblocks, LUT tools.
- `RapidWright/src/com/xilinx/rapidwright/edif`: logical netlist source.
- `RapidWright/src/com/xilinx/rapidwright/eco`: ECO helpers, fanout optimization, LUT insertion.
- `RapidWright/src/com/xilinx/rapidwright/rwroute`: RWRoute, PartialRouter, timing-driven routing structures.
- `RapidWright/src/com/xilinx/rapidwright/timing`: timing graph/model.
- `RapidWright/src/com/xilinx/rapidwright/interchange`: FPGA interchange read/write.
- `RapidWright/src/com/xilinx/rapidwright/examples`: minimal runnable patterns.
- `RapidWright/test/src`: behavior examples and edge cases.
- `RapidWrightMCP/rapidwright_tools.py`: Python JPype wrapper used by the contest agent.
- `VivadoMCP/vivado_mcp_server.py`: Vivado Tcl wrapper.
- `FDAgents/`: newer skill-based optimization agent framework.

Core jar note:

```bash
find RapidWright/jars -maxdepth 1 -name 'rapidwright-api-lib*'
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.Design
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.Net
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.Cell
```

## Build And Run

Build:

```bash
cd RapidWright
./gradlew compileJava
```

Run registered apps:

```bash
RapidWright/bin/rapidwright --list-apps
RapidWright/bin/rapidwright ReportRouteStatus input.dcp
RapidWright/bin/rapidwright RWRoute input.dcp output.dcp --nonTimingDriven
RapidWright/bin/rapidwright com.xilinx.rapidwright.rwroute.TimingAndWirelengthReport input.dcp
```

Relevant files:

- `RapidWright/build.gradle`: main class is `com.xilinx.rapidwright.MainEntrypoint`; source dir is `src`, classes go to `bin`.
- `RapidWright/bin/rapidwright`: checks `build/libs/main.jar`, then dispatches either registered app names or full class names.
- `RapidWright/src/com/xilinx/rapidwright/MainEntrypoint.java`: registered apps include `RWRoute`, `PartialRouter`, `ReportRouteStatus`, `ReportTimingExample`, interchange tools, examples.

Python JPype setup:

```bash
export RAPIDWRIGHT_PATH=/path/to/pact-artifact/RapidWright
export CLASSPATH="$RAPIDWRIGHT_PATH/bin:$RAPIDWRIGHT_PATH/jars/*"
JAVA_TOOL_OPTIONS="-Xmx32G" python3 my_opt.py
```

```python
import rapidwright
from com.xilinx.rapidwright.design import Design

d = Design.readCheckpoint("in.dcp")
d.writeCheckpoint("out.dcp")
```

`RapidWright/python/src/rapidwright/rapidwright.py` starts the JVM, requires `CLASSPATH` if `RAPIDWRIGHT_PATH` is set, and blocks Java `System.exit` calls through `FileTools.blockSystemExitCalls()`.

## Core Object Model

Physical + logical layers:

- `Design`: top-level physical/logical container. Key APIs: `readCheckpoint`, `writeCheckpoint`, `getDevice`, `getNetlist`, `getCell`, `getNet`, `getSiteInst`, `getCells`, `getNets`, `getSiteInsts`, `createCell`, `createAndPlaceCell`, `placeCell`, `createNet`, `unrouteDesign`, `routeSites`.
- `EDIFNetlist`: logical netlist. Key APIs: `getTopCell`, `getTopHierCellInst`, `getHierCellInstFromName`, `getHierPortInstFromName`, `getHierNetFromName`, `getPhysicalPins`, `getPhysicalNetFromPin`, `getParentNetMap`, `resetParentNetMap`.
- `EDIFHierPortInst`: useful bridge from Vivado-style hierarchical pin names to physical objects. Key APIs include `getPhysicalCell(design)`, `getRoutedPhysicalNet(design)`, `getRoutedSitePinInst(design)`, `getRoutedBELPin(design)`.
- `Cell`: physical cell pointing back to logical `EDIFCellInst`/`EDIFHierCellInst`. Key APIs: `getName`, `getType`, `isPlaced`, `getSite`, `getTile`, `getBEL`, `getSiteInst`, `getEDIFHierCellInst`, `getPhysicalPinMapping`, `getLogicalPinMapping`, `getPhysicalPinMappings`, `unplace`, `isLocked`.
- `Net`: physical net. Key APIs: `getPins`, `getSinkPins`, `getSource`, `getAlternateSource`, `getPIPs`, `setPIPs`, `addPIP`, `removePIP`, `connect(Cell, pin)`, `removePin`, `unroute`, `unroutePin`, `isClockNet`, `isStaticNet`, `getLogicalHierNet`.
- `SiteInst`: configured site. Holds cells, site pins, site PIPs, and site-wire to net mapping. Key APIs: `getCell`, `getCells`, `getSitePinInst`, `getNetFromSiteWire`, `routeIntraSiteNet`, `unrouteIntraSiteNet`, `routeSite`, `unrouteSite`.
- `SitePinInst`: physical net pin at a site boundary. Key APIs: `isOutPin`, `isRouted`, `setRouted`, `getNet`, `getTile`, `getBELPin`, `getConnectedNode`, `getRouteNode`.
- `Device`: static device database. Key APIs: `Device.getDevice`, `getTile`, `getSite`, `getNode`, `getPIP`, `getAllTiles`, `getAllSites`, `getClockRegion`, `getSLR`.
- `Tile`, `Site`, `BEL`, `BELPin`, `Node`, `PIP`: architecture objects. `Tile.getManhattanDistance`, `PIP.getStartNode`, `PIP.getEndNode`, and `Node` downhill/uphill traversal are the common routing hooks.

Name mapping pointers:

- Logical lookup implementation is in `RapidWright/src/com/xilinx/rapidwright/edif/EDIFNetlist.java`, especially hierarchical object lookup and physical pin maps.
- `RapidWright/src/com/xilinx/rapidwright/edif/EDIFHierPortInst.java` maps logical hierarchical pins to routed physical objects.
- `DesignTools.getPortInstsFromSitePinInst` maps physical site pins back toward logical port insts.
- `DesignTools.makePhysNetNamesConsistent` canonicalizes physical net names against logical parent nets.

## DCP, EDIF, Interchange

Checkpoint:

- Use `Design.readCheckpoint(String/Path)` and `design.writeCheckpoint(String/Path)`.
- Overloads support `CodePerfTracker`, external EDIF path, and skip-place/route style reads.
- `Design.getPartNameFromDCP` and `Design.replaceEDIFinDCP` are useful utility APIs.
- If encrypted or external EDIF appears, prefer explicit read paths and keep the companion Tcl files.

EDIF:

- `EDIFTools.readEdifFile`, `writeEDIFFile`, `readEdifFromZipFile`, `loadEDIFFile`.
- Binary EDIF cache uses `.bedf` through `BinaryEDIFWriter` / `BinaryEDIFReader`.
- After structural logical edits, consider `EDIFNetlist.resetParentNetMap()`.
- `EDIFTools.connectPortInstsThruHier` can connect through hierarchy, but shared cell definitions may require uniqueification. If module instances exist, uniqueify may require flattening first.

Interchange:

- `Interchange.readInterchangeDesign(root)` finds `.netlist`, `.phys`, `.xdc` with the same root.
- `Interchange.writeDesignToInterchange(design, root)` writes logical, physical, and constraints where present.
- CLI tools: `DcpToInterchange`, `PhysicalNetlistToDcp`, `EdifToLogicalNetlist`, `LogicalNetlistToEdif`.
- Physical netlist route branches encode `PIP`, `BEL_PIN`, `SITE_PIN`, `SITE_PIP`.

Useful commands:

```bash
RapidWright/bin/rapidwright DcpToInterchange input.dcp
RapidWright/bin/rapidwright PhysicalNetlistToDcp input.netlist input.phys input.xdc output.dcp
```

## Required Normalization After Loading Or Editing

For placed/routed DCPs, use this pattern before route analysis and after physical edits:

```java
DesignTools.makePhysNetNamesConsistent(design);
DesignTools.createMissingSitePinInsts(design);
DesignTools.updatePinsIsRouted(design);
```

Then validate route status:

```java
ReportRouteStatusResult rrs = ReportRouteStatus.reportRouteStatus(design);
System.out.println(rrs.toString("RW Route Status"));
```

Why:

- Some routed DCPs do not have complete `SitePinInst` objects until rebuilt.
- `SitePinInst.isRouted()` is cached state; direct PIP mutations do not make it trustworthy.
- `ReportRouteStatus` uses `SitePinInst.isRouted()` plus node conflict detection, so update first.

Source pointers:

- `DesignTools.createMissingSitePinInsts`: `RapidWright/src/com/xilinx/rapidwright/design/DesignTools.java:2233`, `:2511`
- `DesignTools.makePhysNetNamesConsistent`: `DesignTools.java:3369`
- `DesignTools.updatePinsIsRouted`: `DesignTools.java:4652`, `:4666`, `:4721`
- `ReportRouteStatus.reportRouteStatus`: `RapidWright/src/com/xilinx/rapidwright/util/ReportRouteStatus.java:52`

## Routing And Timing APIs

### Net/PIP/Node Inspection

Core public APIs from jar:

- `Net.getPins()`, `getSinkPins()`, `getSource()`, `getPIPs()`, `getCopyOfPIPs()`, `hasPIPs()`, `getFanOut()`
- `Net.setPIPs(List/Set)`, `addPIP`, `removePIP`, `unroute`, `unroutePin`
- `PIP.getStartNode()`, `getEndNode()`, `isReversed()`, `isRouteThru()`, `isBidirectional()`, `isPIPFixed()`, `setIsPIPFixed()`, `isGapArc()`
- `Node.getAllDownhillNodes/PIPs`, `getAllUphillNodes/PIPs`, `getAllWiresInNode`, `getSitePin`, `getIntentCode`

PIP direction rule: if `pip.isReversed()`, actual start/end are swapped. Source examples handle this in `NetTools`, `DesignTools`, and `ReportRouteStatus`.

Source pointers:

- `NetTools.getNodeTrees(Net)`: route-tree reconstruction, `RapidWright/src/com/xilinx/rapidwright/design/NetTools.java:183`
- `NetTools.getNetTreeString`: textual tree report, `NetTools.java:272`
- `DesignTools.getConnectionPIPs(SitePinInst)`: sink-to-source PIP list, `DesignTools.java:3942`
- `DesignTools.findRoutingPath`: arbitrary route path search, `DesignTools.java:809`

### Selective Unroute

Use these in order of preference:

- `DesignTools.unroutePins(net, pins)`: remove only necessary branches to selected sink/source pins.
- `Net.unroutePin(spi)`: public API for one pin.
- `Net.removePin(spi, preserveOtherRoutes=true)`: remove pin while preserving unrelated route where possible.
- `Net.unroute()`: clears whole net, broad and often too disruptive.
- `Design.unrouteDesign()`: broadest.

Source pointers:

- `DesignTools.unroutePins`: `DesignTools.java:1144`
- `DesignTools.unrouteSourcePin`: `DesignTools.java:1178`
- `DesignTools.getTrimmablePIPsFromPins`: `DesignTools.java:1247`
- `DesignTools.removeConnectedRouting`: `DesignTools.java:1099`

### RWRoute vs PartialRouter

`RWRoute`:

- Full router in `RapidWright/src/com/xilinx/rapidwright/rwroute/RWRoute.java`.
- CLI: `RapidWright/bin/rapidwright RWRoute input.dcp output.dcp [options]`.
- Full routing flow preprocesses, categorizes nets, creates `NetWrapper`/`Connection`, routes, and writes PIPs back.
- It unroutes target signal/static/clock nets during full routing; treat it as a full re-router, not a preserve-by-default incremental router.
- Main hooks: `routeDesignFullTimingDriven`, `routeDesignFullNonTimingDriven`, `routeDesignWithUserDefinedArguments`, `evaluateCostAndPush`, `setPIPsOfNets`, `checkPIPsUsage`.

`PartialRouter`:

- Incremental/preserve-oriented router in `RapidWright/src/com/xilinx/rapidwright/rwroute/PartialRouter.java`.
- It preserves already routed pins/nets and routes target unrouted pins; `softPreserve` can release blocking preserved routing.
- Main hooks: `getUnroutedPins`, `routeDesignPartialNonTimingDriven`, `routeDesignPartialTimingDriven`, `preserveNet`, `unpreserveNet`.
- For contest ECO repair, prefer `PartialRouter` over full `RWRoute`.

RWRouteConfig options to remember:

- `--maxIterations`
- `--timingDriven` / `--nonTimingDriven`
- `--wirelengthWeight`
- `--timingWeight`
- `--timingMultiplier`
- `--criticalityExponent`
- `--minRerouteCriticality`
- `--reroutePercentage`
- `--boundingBoxExtensionX/Y`
- `--fixBoundingBox`
- `--useUTurnNodes`
- `--maskNodesCrossRCLK`
- `--lutPinSwapping`
- `--lutRoutethru`
- `--pblock`
- `--verbose`

### Timing Graph

Useful classes:

- `TimingManager`: builds model/graph; computes arrival/required times and critical path reports.
- `TimingGraph`: path-level APIs such as `getMaxDelayPath`, `getPathDelay`, `getWorstSlack`, `getCriticalTimingEdgesInOrder`, `getTimingPath`.
- `TimingEdge`: per-edge `getLogicDelay`, `getNetDelay`, `getIntraSiteDelay`, `getDelay`, `getNet`.
- `DelayEstimatorBase`: node delay estimator used by timing-driven routing.
- `TimingAndWirelengthReport`: runnable template for post-route wirelength and approximate critical path reporting.

Important caveat: RW timing is useful for ranking and feature extraction, but Vivado timing is the scoreboard.

Source pointers:

- `RapidWright/src/com/xilinx/rapidwright/timing/TimingManager.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingGraph.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingEdge.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/delayestimator/DelayEstimatorBase.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/TimingAndWirelengthReport.java`

### Congestion, Wirelength, Illegal States

- `ReportRouteStatus` detects unrouted pins and resource conflicts by used end nodes.
- `RouteNode.isOverUsed`, `Connection.isCongested`, and `RouteNodeGraph.updatePresentCongestionCosts` expose RWRoute congestion internals.
- `RouteNode.getLength(node, routingGraph)` and `RouterHelper.addNodeTypeLengthToMap` can drive wirelength/intent-code reports.
- `Net.hasGapRouting` and `PIP.isGapArc` can identify gap routing; `NetTools.getNodeTrees` ignores gaps/islands but exposes multiply-driven trees.

Useful hooks:

- Build per-net route-tree features: fanout, PIP count, branch depth, shared branch ratio, gap/island, multiply-driven flag.
- Build per-sink features from `DesignTools.getConnectionPIPs`.
- Build hot-resource map from `ReportRouteStatus` conflict logic plus node intent codes.
- Combine `TimingGraph` critical edges with physical PIP/tree data for sink-level scoring.

## Placement And ECO

### Safe Cell Move Pattern

Avoid raw `Cell.unplace()` for routed designs. Use:

```java
Map<Net, Set<SitePinInst>> deferred = new HashMap<>();

DesignTools.fullyUnplaceCell(cell, deferred);
design.placeCell(cell, newSite, newBEL, cell.getPhysicalPinMappings());
cell.getSiteInst().routeSite();
DesignTools.batchRemoveSitePins(deferred, true);
```

Then selectively unroute/re-route affected nets and update route status.

Why:

- `fullyUnplaceCell` scans BEL pins, site wires, RBELs, site PIPs, routethrus, shared CLK/CE/SR, LUT5/LUT6 A6 pins, dual-output alternate sources.
- Deferred removals let many edits be batched while preserving unrelated branches.

Source pointers:

- `DesignTools.fullyUnplaceCell`: `RapidWright/src/com/xilinx/rapidwright/design/DesignTools.java:1489`
- helper details: `DesignTools.java:1353`, `:1419`, `:1436`, `:1453`, `:1464`
- `DesignTools.batchRemoveSitePins`: `DesignTools.java:1575`
- `DesignTools.placeCell`: `DesignTools.java:2201`

### ECO Helpers

`ECOTools`:

- `disconnectNet`: logical + physical disconnect with optional deferred removals.
- `connectNet`: reconnect through hierarchy and create physical site pins/site routing.
- `createExitSitePinInst`: create/reuse a site pin and route intra-site, including LUT input remapping.
- `removeCell`, `createCell`, `createAndPlaceInlineCellOnInputPin`, `refactorCell`.

`ECOPlacementHelper`:

- `getUnusedFlop`: finds compatible empty FF and checks CLK/CE/SR compatibility and pin-bounce constraints.
- `getUnusedLUT`: finds compatible empty LUT; same-letter 5LUT/6LUT constraints matter.
- `getCentroidOfNet`, `getCentroidOfPoints`: placement target heuristic.
- `spiralOutFrom`: local search around a site/tile, optionally pblock constrained.

Source pointers:

- `RapidWright/src/com/xilinx/rapidwright/eco/ECOTools.java`
- `RapidWright/src/com/xilinx/rapidwright/eco/ECOPlacementHelper.java`

### Replication, LUT Swaps, Relocation

Fanout replication:

- `FanOutOptimization.cutFanOutOfRoutedNet` supports FF drivers (`FDRE/FDSE/FDCE/FDPE`) and `LUT1..LUT6`.
- It clusters sinks with KMeans, moves original driver, creates copies, disconnects/reconnects sinks.
- Source: `RapidWright/src/com/xilinx/rapidwright/eco/FanOutOptimization.java`.

LUT pin swap:

- `LUTTools.swapMultipleLutPins`
- `LUTTools.swapLutPinsFromPIPs`
- Updates logical/physical mapping, SPI names, and intra-site routing.
- SRL pin swap is not supported.
- Source: `RapidWright/src/com/xilinx/rapidwright/design/tools/LUTTools.java`.

Inline LUT1:

- `LUT1InsertionTool.insertLUT1`
- `ECOTools.createAndPlaceInlineCellOnInputPin`
- Can insert buffer/inverter/routethru near a sink. Use carefully: it changes logic structure but can preserve cycle behavior if function is equivalent.

Hierarchy/SLR relocation:

- `RelocationTools.relocate` moves hierarchy-relative `SiteInst`/PIP placements if target sites are not mixed with outside cells.
- Boundary nets are selectively/full unrouted depending on source/sink relation and static/clock status.
- Source: `RapidWright/src/com/xilinx/rapidwright/design/tools/RelocationTools.java`.

Macro/module placement:

- `ModuleInst.place(site, skipIncompatible, allowOverlap)` places relative sites/PIPs from an anchor.
- `ModuleInst.unplace` clears module placement and relevant module PIPs.
- `BlockPlacer2` is annealing-style legalizer for module placement experiments.

### What To Unroute After Placement Mutation

- Moving/deleting a cell: unroute that cell's affected site routing. If it is the last user of shared CLK/CE/SR, remove corresponding SPI and inter-site branch.
- Reconnecting sinks: prefer `ECOTools.disconnectNet(..., deferred)`, `ECOTools.connectNet(..., deferred)`, then `DesignTools.batchRemoveSitePins(deferred, true)`.
- Only changing some sinks: use `DesignTools.unroutePins` or `Net.removePin(spi, true)`.
- If removing an SPI and planning to reuse the same SPI name, also remove it from the `SiteInst`.
- Static nets and clocks need special handling; do not treat them like ordinary signal nets.

## Contest Repository Integration

There are two optimization loop styles:

1. `dcp_optimizer.py`: original ReAct/tool-calling loop.
2. `FDAgents/`: newer compact-state + skill framework. Better fit for durable recipe development.

Common workflow:

1. Vivado opens DCP and reports target clock timing.
2. RapidWright reads the same DCP for structural analysis/ECO.
3. RW writes a modified DCP.
4. Vivado opens/routes/physopts/measures.
5. Only legal better checkpoints are promoted to output.

Important files:

- `dcp_optimizer.py`: server lifecycle, clock period, target WNS, best checkpoint promotion.
- `RapidWrightMCP/rapidwright_tools.py`: JPype wrapper.
- `VivadoMCP/vivado_mcp_server.py`: arbitrary Tcl, timing path extraction, pblock tools, phys_opt wrappers.
- `FDAgents/agent.py`: skill loop, accept/reject, rollback to `current_best.dcp`.
- `FDAgents/skills/`: recipe modules (`phys_opt`, `fanout_opt`, `cell_replace`, `pblock`, `lut_merge`, `per_net_unroute`, `custom`).
- `scripts/extract_dcp_info.py`: DCP metadata/inventory extraction.

Reusable RapidWrightMCP functions:

- `initialize_rapidwright`
- `read_checkpoint`
- `write_checkpoint`
- `report_approx_timing`
- `analyze_net_detour`
- `optimize_cell_placement`
- `optimize_critical_path_detours`
- pblock fabric search/convert helpers
- fanout and LUT-cone utilities

Known fragile spots:

- Vivado timing/route parsing is regex/text driven.
- Vivado MCP uses `pexpect`; timeouts can leave pending command state.
- `optimize_cell_placement` currently calls `net.unroute()` on connected nets, which is often too broad.
- Some pblock search code uses fixed empirical dimensions.
- `compare_design_structure` is a sanity check, not a proof of equivalence.

## Contest Constraints That Shape RW Work

Scoring:

- Benchmark score is driven by Fmax improvement minus OpenRouter cost and runtime penalty.
- 1 hour wall-clock per benchmark. The last/best output DCP at timeout is what matters.
- Hidden benchmarks exist; do not overfit only public names.

Runtime environment:

- AWS `m7a.2xlarge`: 8 vCPU, 32 GB RAM.
- Vivado 2025.1.
- Large designs spend most time in Vivado open/place/route; batch RW ECOs before Vivado measure when possible.

Timing target:

- Optimize clock `clk_fpl26contest`.
- Overall `report_timing_summary` WNS can be wrong on multi-clock designs.
- Query target WNS with Tcl similar to:

```tcl
set tp [get_timing_paths -max_paths 1 -setup -to [get_clocks clk_fpl26contest]]
get_property SLACK $tp
```

Legality:

- Fully placed/routed.
- Zero routing errors.
- Hold and pulse width constraints met.
- Functional equivalence.
- Latency-preserving only. Replication/relocation/pblock re-place/LUT pin swap/retiming are plausible. Adding pipeline registers that change cycle behavior is not.

Validation:

- `validate_dcps.py` does RW structure sanity and Vivado funcsim/xsim random-vector comparison.
- Encrypted/SIP cases may skip parts of validation locally, so local pass is not always full proof.

## Proven And Useful Recipes

### 1. Critical Sink Rip-Up + Partial Reroute

Identify:

- Use Vivado critical paths or RW `TimingGraph.getCriticalTimingEdgesInOrder`.
- Map critical timing edges to nets/sinks.

Optimize:

- `DesignTools.unroutePins(net, criticalSinkPins)`.
- `PartialRouter.routeDesignPartialTimingDriven(design, pinsToRoute, softPreserve=true)` or non-timing-driven for speed.

Measure:

- RW `ReportRouteStatus` first, then Vivado route/timing.

Why useful: minimal perturbation compared with `route_design -unroute` or `net.unroute()`.

### 2. Critical Path Detour Cell Re-Placement

Identify:

- Vivado `extract_critical_path_pins`.
- RW `analyze_net_detour`: compare routed path length to source-sink Manhattan distance.

Optimize:

- Move selected LUT/FF near centroid of connected pins.
- Use `fullyUnplaceCell`, compatible BEL search, `placeCell`, `routeSite`, deferred pin removals.
- Unroute only affected branches if possible.

Existing wrapper:

- `RapidWrightMCP/rapidwright_tools.py`: `analyze_net_detour`, `optimize_cell_placement`, `optimize_critical_path_detours`.

Current weakness: wrapper often unroutes whole connected nets; improve with `DesignTools.unroutePins`.

### 3. High Fanout Driver Replication

Identify:

- Vivado high fanout report and critical path source repetition.
- RW net fanout and sink clustering.

Optimize:

- Vivado: `phys_opt_design -force_replication_on_nets` often works well.
- RW: `FanOutOptimization.cutFanOutOfRoutedNet`.

Measure:

- Re-route in Vivado. Watch for over-replication cliffs.

### 4. Pblock Topology Re-Place

Known strong result:

- `docs/vexriscv_optimization_recipe.md` records `vexriscv_re-place` from about 310 MHz to about 479 MHz.
- Key step was a full-height narrow vertical pblock, not a small critical-path rectangle.

Generalize:

- Derive critical path bounding boxes.
- Try vertical/horizontal strips around DSP/CARRY/BRAM columns.
- Use Vivado resource validation before place.

Avoid:

- Too-small hard pblocks.
- Global constraints that leave router no freedom.

### 5. LUT Input Pin Swap

Identify:

- Critical LUT input has worse physical route than equivalent input.
- PIPs/SitePins reveal mismatch.

Optimize:

- `LUTTools.swapMultipleLutPins` or `swapLutPinsFromPIPs`.

Measure:

- Check logical function and route status; then Vivado timing.

### 6. Inline LUT1 Buffer/Inverter

Identify:

- A long high-fanout or awkward sink connection where a local buffer may help.

Optimize:

- `ECOTools.createAndPlaceInlineCellOnInputPin` or `LUT1InsertionTool`.

Caution:

- Must preserve logic and latency. Treat as a controlled ECO and validate.

### 7. Hierarchy Or SLR Relocation

Identify:

- A hierarchy has bad placement topology and relatively clean boundary nets.

Optimize:

- `RelocationTools.relocate(design, hier, dx, dy)`.

Caution:

- Target `SiteInst`s must not be mixed with outside hierarchy cells.
- Static/clock/boundary nets need selective/full unroute based on tool rules.

### 8. Repeated Implementation Stamping

Use when many replicated hierarchies should share a good physical implementation.

Tools:

- `DesignTools.stampPlacement`
- `DesignTools.copyImplementation`
- `copySiteRouting`

## Pitfalls

- Searching only `RapidWright/src` misses core APIs from `rapidwright-api-lib`.
- Raw `Cell.unplace()` is unsafe for routed designs.
- `net.unroute()` is often too broad and can destroy a good solution.
- `RWRoute` full routing does not preserve existing routing by default.
- `PartialRouter` is the right place for RW incremental routing experiments.
- `SitePinInst.isRouted()` must be recomputed after manual PIP edits.
- `ReportRouteStatus` is only as good as updated pins plus physical route tree consistency.
- RW approximate timing can rank candidates but does not replace Vivado.
- Overall WNS can be the wrong clock; always query `clk_fpl26contest`.
- Logical netlist edits may require `EDIFNetlist.resetParentNetMap`.
- EDIF hierarchy edits on shared definitions may require uniqueification or flattening.
- Static nets and clocks have special routing/source behavior.
- Pblock gains can disappear after routing; always measure post-route.
- Vivado `phys_opt -force_replication_on_nets` cannot be used in post-route mode without unroute.

## Minimal Java Skeleton

```java
import com.xilinx.rapidwright.design.*;
import com.xilinx.rapidwright.rwroute.PartialRouter;
import com.xilinx.rapidwright.tests.CodePerfTracker;
import com.xilinx.rapidwright.util.ReportRouteStatus;
import com.xilinx.rapidwright.util.ReportRouteStatusResult;

import java.util.*;

public class FplRwOptimize {
    public static void main(String[] args) {
        if (args.length != 2) {
            System.out.println("USAGE: <input.dcp> <output.dcp>");
            return;
        }

        CodePerfTracker t = new CodePerfTracker("FPL RW Optimize", true);
        t.start("Read DCP");
        Design d = Design.readCheckpoint(args[0], CodePerfTracker.SILENT);

        t.stop().start("Normalize");
        DesignTools.makePhysNetNamesConsistent(d);
        DesignTools.createMissingSitePinInsts(d);
        DesignTools.updatePinsIsRouted(d);

        t.stop().start("Analyze/Optimize");
        // Inspect d.getCells(), d.getNets(), net.getPIPs(), net.getSinkPins().
        // Prefer DesignTools.unroutePins(net, pins) over net.unroute().
        // Prefer DesignTools.fullyUnplaceCell(cell, deferred) over cell.unplace().

        t.stop().start("Repair routing");
        PartialRouter.routeDesignPartialNonTimingDriven(d, null, false);

        t.stop().start("Route status");
        DesignTools.updatePinsIsRouted(d);
        ReportRouteStatusResult rrs = ReportRouteStatus.reportRouteStatus(d);
        System.out.println(rrs.toString("Post-RW Route Status"));

        t.stop().start("Write DCP");
        d.writeCheckpoint(args[1], t);
        t.stop().printSummary();
    }
}
```

## Minimal Python Skeleton

```python
import os

os.environ["RAPIDWRIGHT_PATH"] = "/path/to/pact-artifact/RapidWright"
os.environ["CLASSPATH"] = os.environ["RAPIDWRIGHT_PATH"] + "/bin:" + os.environ["RAPIDWRIGHT_PATH"] + "/jars/*"

import rapidwright
from com.xilinx.rapidwright.design import Design, DesignTools
from com.xilinx.rapidwright.util import ReportRouteStatus

d = Design.readCheckpoint("in.dcp")
DesignTools.makePhysNetNamesConsistent(d)
DesignTools.createMissingSitePinInsts(d)
DesignTools.updatePinsIsRouted(d)

# Inspect or mutate here.

DesignTools.updatePinsIsRouted(d)
rrs = ReportRouteStatus.reportRouteStatus(d)
print(rrs.toString("Post-RW Route Status"))
d.writeCheckpoint("out.dcp")
```

## High-Value Source Index

Core/model:

- `RapidWright/jars/rapidwright-api-lib-2025.2.1.jar`: `Design`, `Cell`, `Net`, `SiteInst`, `SitePinInst`, `Device`, `Tile`, `Node`, `PIP`.
- `RapidWright/src/com/xilinx/rapidwright/design/DesignTools.java`
- `RapidWright/src/com/xilinx/rapidwright/design/NetTools.java`
- `RapidWright/src/com/xilinx/rapidwright/edif/EDIFNetlist.java`
- `RapidWright/src/com/xilinx/rapidwright/edif/EDIFTools.java`
- `RapidWright/src/com/xilinx/rapidwright/edif/EDIFHierPortInst.java`

Routing/timing:

- `RapidWright/src/com/xilinx/rapidwright/rwroute/RWRoute.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/RWRouteConfig.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/PartialRouter.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/Connection.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/NetWrapper.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/RouteNode.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/RouteNodeGraph.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/RouterHelper.java`
- `RapidWright/src/com/xilinx/rapidwright/rwroute/TimingAndWirelengthReport.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingManager.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingGraph.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingEdge.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/delayestimator/DelayEstimatorBase.java`
- `RapidWright/src/com/xilinx/rapidwright/util/ReportRouteStatus.java`
- `RapidWright/src/com/xilinx/rapidwright/util/VivadoTools.java`

ECO/placement:

- `RapidWright/src/com/xilinx/rapidwright/eco/ECOTools.java`
- `RapidWright/src/com/xilinx/rapidwright/eco/ECOPlacementHelper.java`
- `RapidWright/src/com/xilinx/rapidwright/eco/FanOutOptimization.java`
- `RapidWright/src/com/xilinx/rapidwright/eco/LUT1InsertionTool.java`
- `RapidWright/src/com/xilinx/rapidwright/design/tools/LUTTools.java`
- `RapidWright/src/com/xilinx/rapidwright/design/tools/RelocationTools.java`
- `RapidWright/src/com/xilinx/rapidwright/design/ModuleInst.java`
- `RapidWright/src/com/xilinx/rapidwright/placer/blockplacer/BlockPlacer2.java`

Checkpoint/interchange/tools:

- `RapidWright/src/com/xilinx/rapidwright/interchange/Interchange.java`
- `RapidWright/src/com/xilinx/rapidwright/interchange/DcpToInterchange.java`
- `RapidWright/src/com/xilinx/rapidwright/interchange/PhysicalNetlistToDcp.java`
- `RapidWright/src/com/xilinx/rapidwright/interchange/PhysNetlistReader.java`
- `RapidWright/src/com/xilinx/rapidwright/interchange/PhysNetlistWriter.java`
- `RapidWright/src/com/xilinx/rapidwright/interchange/LogNetlistReader.java`
- `RapidWright/src/com/xilinx/rapidwright/interchange/LogNetlistWriter.java`
- `RapidWright/src/com/xilinx/rapidwright/util/FileTools.java`
- `RapidWright/src/com/xilinx/rapidwright/tests/CodePerfTracker.java`

Examples/tests:

- `RapidWright/src/com/xilinx/rapidwright/examples/CustomRouting.java`
- `RapidWright/src/com/xilinx/rapidwright/examples/CountRoutedNets.java`
- `RapidWright/src/com/xilinx/rapidwright/examples/ReportTimingExample.java`
- `RapidWright/src/com/xilinx/rapidwright/examples/UpdateRoutingUsingSATRouter.java`
- `RapidWright/src/com/xilinx/rapidwright/examples/RunSATRouterExample.java`
- `RapidWright/test/src/com/xilinx/rapidwright/design/TestDesignTools.java`
- `RapidWright/test/src/com/xilinx/rapidwright/design/TestNet.java`
- `RapidWright/test/src/com/xilinx/rapidwright/eco/TestECOTools.java`
- `RapidWright/test/src/com/xilinx/rapidwright/rwroute/TestRWRoute.java`

Contest repo:

- `RapidWrightMCP/rapidwright_tools.py`
- `VivadoMCP/vivado_mcp_server.py`
- `dcp_optimizer.py`
- `FDAgents/agent.py`
- `FDAgents/skills/`
- `scripts/extract_dcp_info.py`
- `validate_dcps.py`
- `docs/optimization_example.md`
- `docs/vexriscv_optimization_recipe.md`
- `docs/benchmarks.md`
- `docs/score.md`
- `docs/runtime.md`

## Suggested Future Agent Task Split

Use these as independent work packages:

- `validation-guardrails`: one legality gate for route/hold/pulse/target-clock WNS/function validation and best-DCP promotion.
- `vivado-recipes`: stabilize directive matrices, VexRiscv-style pblock/force-replication/per-net-unroute recipes.
- `rapidwright-eco`: implement safer leaf-level clone, single-sink reconnect, selective unroute, BEL/site compatibility checks.
- `routing-analysis`: route-tree feature extraction, sink-level detour, congestion map, critical edge to sink mapping.
- `pblock-topology`: critical-path bbox, vertical strips, DSP/BRAM/CARRY column aware pblocks.
- `benchmark-mining`: classify benchmarks by timing bottleneck and pick recipe families automatically.
- `experiment-db`: normalize run logs, timing, route status, token/cost, and recipe parameters for regression.

---

## Appendix A: Detailed Core Model And API Catalog

This appendix is for future agents that need more than a high-level object map. It combines local source scan, `javap` of `rapidwright-api-lib-2025.2.1.jar`, and the useful cross-layer methods found in EDIF source files.

### A.1 Why Core Classes Are Split Between Source And Jar

The checkout under `RapidWright/src` does not contain all source for the public API. The Gradle build depends on `com.xilinx.rapidwright:rapidwright-api-lib:2025.2.1`; that jar provides many central classes. This is why `rg Design.java RapidWright/src` does not find `Design.java`.

Practical rule:

- Search `RapidWright/src` first for tools/helpers/examples.
- Use `javap` for public API surface of jar-only classes.
- Use `RapidWright/jars/rapidwright-api-lib-2025.2.1-javadoc.jar` if you need docs for jar-only classes.
- Do not assume missing source means the class is unavailable. If Python JPype imports it, it is usually in the jar.

Commands:

```bash
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.Design
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.Net
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.Cell
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.SiteInst
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.design.SitePinInst
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.device.Device
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.device.Tile
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.device.Node
javap -classpath 'RapidWright/bin:RapidWright/jars/*' -public com.xilinx.rapidwright.device.PIP
```

### A.2 Design

`Design` is the top-level object that ties together physical state, logical EDIF, device database, XDC constraints, modules, nets, cells, sites, and partition pins.

High-use read/write APIs:

- `Design.readCheckpoint(Path/String)`
- `Design.readCheckpoint(Path/String, CodePerfTracker)`
- `Design.readCheckpoint(dcp, edif, CodePerfTracker)`
- `Design.readCheckpoint(Path/String, boolean)`
- `design.writeCheckpoint(Path/String)`
- `design.writeCheckpoint(Path/String, CodePerfTracker)`
- `design.updateDesignWithCheckpointPlaceAndRoute(Path/String)`
- `Design.getPartNameFromDCP(Path/String)`
- `Design.replaceEDIFinDCP(...)`

High-use queries:

- `getName()`
- `getPart()`
- `getPartName()`
- `getSeries()`
- `getDevice()`
- `getNetlist()`
- `getTopEDIFCell()`
- `getCell(String)`
- `getCells()`
- `getNet(String)`
- `getNets()`
- `getSiteInst(String)`
- `getSiteInstFromSite(Site)`
- `getSiteInstFromSiteName(String)`
- `getSiteInsts()`
- `isSiteUsed(Site)`
- `getGndNet()`
- `getVccNet()`
- `getStaticNet(NetType)`

High-use mutation:

- `createCell(String, EDIFCell/Unisim/EDIFCellInst)`
- `createAndPlaceCell(...)`
- `createNet(String)`
- `createNet(EDIFHierNet)`
- `addCell(Cell)`
- `removeCell(String/Cell)`
- `addNet(Net)`
- `removeNet(String/Net)`
- `placeCell(Cell, Site, BEL)`
- `placeCell(Cell, Site, BEL, Map<String,String>)`
- `placeCell(Cell, Site, BEL, String[])`
- `createSiteInst(...)`
- `routeSites()`
- `unrouteSites()`
- `unrouteDesign()`
- `unplaceDesign()`
- `flattenDesign()`

Tracking APIs:

- `setTrackNetChanges(boolean)`
- `setTrackSiteInstChanges(boolean)`
- `getModifiedNets()`
- `getModifiedSiteInsts()`
- `clearTrackedChanges()`
- `setCopyingOriginalNetsRouting(boolean)`
- `getOriginalNetRouting()`
- `setCopyingOriginalSiteInsts(boolean)`
- `getOriginalSiteInsts()`

Contest use:

- Use `readCheckpoint`/`writeCheckpoint` as the stable DCP bridge.
- Use `getCell`, `getNet`, `getNetlist().getHierPortInstFromName(...)` to bridge Vivado timing names to RW objects.
- Use `setTrackNetChanges` if you want cheap bookkeeping of mutated nets, but still explicitly maintain your own affected-net set.
- Avoid `unrouteDesign` unless doing full reroute experiments.

### A.3 Net

`Net` is the physical routed net. It is not just the EDIF net. It owns site pins and PIPs and may refer back to a logical `EDIFHierNet`.

Important read APIs:

- `getName()`
- `getType()`
- `getPins()`
- `getSinkPins()`
- `getSource()`
- `getAlternateSource()`
- `getAlternateSources()`
- `getSourceTile()`
- `getPIPs()`
- `getCopyOfPIPs()`
- `hasPIPs()`
- `getFanOut()`
- `getSiteInsts()`
- `getLogicalNet()`
- `getLogicalHierNet()`
- `isStaticNet()`
- `isVCCNet()`
- `isGNDNet()`
- `isClockNet()`
- `hasGapRouting()`

Important mutation APIs:

- `setPIPs(List<PIP>)`
- `setPIPs(Set<PIP>)`
- `addPIP(PIP)`
- `removePIP(PIP)`
- `setPins(List<SitePinInst>)`
- `addPin(SitePinInst)`
- `addPin(SitePinInst, boolean)`
- `removePin(SitePinInst)`
- `removePin(SitePinInst, boolean preserveOtherRoutes)`
- `unroutePin(SitePinInst)`
- `unroute()`
- `connect(Cell, String logicalPin)`
- `createPin(String, SiteInst)`
- `setSource(SitePinInst)`
- `removeSource()`
- `replaceSource(SitePinInst)`
- `lockRouting()`
- `unlockRouting()`

Contest use:

- For analysis, `getPIPs`, `getSource`, and `getSinkPins` are enough to build detour/line length features.
- For mutation, prefer `DesignTools.unroutePins` before `net.unroute`.
- For sink-level rewiring, combine logical EDIF updates through `ECOTools` with physical pin updates.
- For high fanout, `getFanOut` is a cheap filter, but use Vivado timing and actual sink locations for ranking.

### A.4 Cell

`Cell` is physical placement plus a pointer to logical instance.

Important read APIs:

- `getName()`
- `getType()`
- `isPlaced()`
- `getSite()`
- `getTile()`
- `getBEL()`
- `getBELName()`
- `getSiteInst()`
- `getSiteName()`
- `getEDIFCellInst()`
- `getEDIFHierCellInst()`
- `getParentCell()`
- `getParentHierarchicalInstName()`
- `getPhysicalPinMapping(String logicalPin)`
- `getLogicalPinMapping(String physicalPin)`
- `getPhysicalPinMappings()`
- `getAllPhysicalPinMappings(String logicalPin)`
- `getPinMappingsP2L()`
- `getPinMappingsL2P()`
- `getCorrespondingSitePinName(String logicalPin)`
- `getAllCorrespondingSitePinNames(String logicalPin)`
- `getBELPin(EDIFPortInst/EDIFHierPortInst)`
- `getSitePinFromLogicalPin(String, List<String>)`
- `getAllSitePinsFromLogicalPin(...)`
- `isLocked()`
- `isBELFixed()`
- `isSiteFixed()`
- `isRoutethru()`

Important mutation APIs:

- `addPinMapping(physical, logical)`
- `removePinMapping(physical)`
- `fixPin(String)`
- `unFixPin(String)`
- `setLocked(boolean)`
- `setBELFixed(boolean)`
- `setSiteFixed(boolean)`
- `fixCell(boolean)`
- `setType(String)`
- `unplace()`
- `connectStaticSourceToPin/ToPins`
- `unrouteLogicalPinInSite(String)`

Contest use:

- For routed designs, `Cell.unplace()` alone is too weak; use `DesignTools.fullyUnplaceCell`.
- Preserve `getPhysicalPinMappings()` when moving a cell unless you intentionally swap pins.
- Check `cell.getBEL().isLUT()` / `isFF()` when choosing legal destination BELs.
- For LUTs, use `LUTTools` for pin swaps instead of manually editing mapping.

### A.5 SiteInst And SitePinInst

`SiteInst` represents one configured site. It owns cells, site pins, site PIPs, and site-wire routing.

`SiteInst` high-use APIs:

- `getName()`
- `getSite()`
- `getTile()`
- `getSiteTypeEnum()`
- `getBEL(String)`
- `getBELPin(String belName, String pinName)`
- `getCell(String/BEL)`
- `getCells()`
- `getSitePinInst(String)`
- `getSitePinInsts()`
- `getSitePinInstMap()`
- `getConnectedNets()`
- `getNetFromSiteWire(String)`
- `getSiteWiresFromNet(Net)`
- `getSiteWireToNetMap()`
- `getUsedSitePIPs()`
- `hasUsedSitePIPs()`
- `routeIntraSiteNet(Net, BELPin, BELPin)`
- `unrouteIntraSiteNet(BELPin, BELPin)`
- `routeSite()`
- `unrouteSite()`
- `isEmpty()`

`SitePinInst` high-use APIs:

- `getName()`
- `getSitePinName()`
- `isOutPin()`
- `getSiteInst()`
- `getSite()`
- `getTile()`
- `getBELPin()`
- `getConnectedNode()`
- `getRouteNode()`
- `getSiteWireName()`
- `getNet()`
- `setNet(Net)`
- `isRouted()`
- `setRouted(boolean)`
- `movePin(String)`
- `detachSiteInst()`

Contest use:

- `SitePinInst` is the boundary between site routing and inter-site routing. A missing or stale SPI is a common source of broken RW edits.
- Always update pin routed status after PIP mutations.
- `SiteInst.routeSite()` is a coarse but useful repair after placement. Use exact `routeIntraSiteNet` for surgical site edits.
- If `Net.removePin(spi)` is followed by reusing same pin name, remove it from `SiteInst` too or stale mapping may remain.

### A.6 Device, Site, Tile, BEL, Node, PIP

`Device` is the static architecture database:

- `Device.getDevice(String/Part)`
- `getTile(String/int/row,col)`
- `getSite(String)`
- `getNode(String)`
- `getPIP(String)`
- `getWire(String)`
- `getBEL(SiteTypeEnum, String)`
- `getAllTiles()`
- `getAllSites()`
- `getAllCompatibleSites(SiteTypeEnum)`
- `getAllSitesOfType(SiteTypeEnum)`
- `getClockRegion(...)`
- `getSLR(...)`
- `getAvailableDevices()`

`Tile`:

- `getName()`
- `getRow()`
- `getColumn()`
- `getTileXCoordinate()`
- `getTileYCoordinate()`
- `getTileNeighbor(dx, dy)`
- `getTileXYNeighbor(dx, dy)`
- `getSites()`
- `getPIPs()`
- `getPIPs(wireName/wireIndex)`
- `getBackwardPIPs(...)`
- `getManhattanDistance(Tile)`
- `getWireName(int)`
- `getWireIndex(String)`
- `getWireIntentCode(int)`
- `getClockRegion()`
- `getSLR()`

`Site`:

- `getName()`
- `getTile()`
- `getSiteTypeEnum()`
- `getAlternateSiteTypeEnums()`
- `getBEL(String)`
- `getBELs()`
- `getInstanceX()`
- `getInstanceY()`
- `getRpmX()`
- `getRpmY()`
- `isCompatibleSiteType(...)`
- `getNeighborSite(dx, dy)`
- `getConnectedNode(pinName/index)`
- `getBELPin(sitePinName)` or `getBELPin(belName, pinName)`
- `getSitePIPs()`
- `getClockRegion()`

`BEL`:

- `getName()`
- `getBELClass()`
- `getPins()`
- `getPin(String/int)`
- `getBELType()`
- `isLUT()`
- `isFF()`
- `isCarry()`
- `isStaticSource()`
- `isGndSource()`
- `isVccSource()`
- `canInvert()`
- `getHighestInputIndex()`

`Node`:

- `Node.getNode(Tile, wireIndex/wireName)`
- `Node.getNode(String, Device)`
- `getTile()`
- `getWireIndex()`
- `getAllWiresInNode()`
- `getAllDownhillNodes()`
- `getAllUphillNodes()`
- `getAllDownhillPIPs()`
- `getAllUphillPIPs()`
- `getSitePin()`
- `isTiedToGnd()`
- `isTiedToVcc()`
- `isTied()`
- `getIntentCode()`
- `hasIntentCode(IntentCode)`

`PIP`:

- constructors from `Tile`, `Device`, string form.
- `getTile()`
- `getStartWireIndex()`
- `getEndWireIndex()`
- `getStartWireName()`
- `getEndWireName()`
- `getStartNode()`
- `getEndNode()`
- `isReversed()`
- `setIsReversed(boolean)`
- `isRouteThru()`
- `isBidirectional()`
- `isStub()`
- `isGapArc()`
- `isPIPFixed()`
- `setIsPIPFixed(boolean)`
- `getDelayIndex()`
- `PIP.getArbitraryPIP(Node, Node)`

Contest use:

- `Tile.getManhattanDistance` is the cheap geometric feature for placement and detour.
- `Node.getIntentCode` lets you classify route resources and hot spots.
- `PIP.getStartNode/getEndNode` is the route-tree edge. Respect `isReversed`.
- Use `Site.isCompatibleSiteType` and BEL type checks before placement.

### A.7 EDIF Logical Netlist Details

`EDIFNetlist`:

- Top and library queries: `getTopCell`, `getTopCellInst`, `getTopHierCellInst`, `getWorkLibrary`, `getHDIPrimitivesLibrary`, `getLibraries`.
- Hierarchical lookup: `getCellInstFromHierName`, `getHierCellInstFromName`, `getHierPortInstFromName`, `getHierNetFromName`, `getHierObject`.
- Physical net mapping: `getPhysicalPins(Net/String/EDIFHierNet)`, `getPhysicalNetFromPin`, `getPhysicalVccPins`, `getPhysicalGndPins`.
- Parent/alias mapping: `getParentNet`, `getParentNetName`, `getParentNetMap`, `getParentNetMapNames`, `resetParentNetMap`, `getNetAliases`.
- Descendant queries: `getAllLeafDescendants`, `getAllLeafCellInstances`, `getAllLeafHierCellInstances`.
- Macro/static helpers: `identifyNetType`, `expandMacroUnisims`, `collapseMacroUnisims`.
- Export/cache: `exportEDIF`, `readBinaryEDIF`, `writeBinaryEDIF`.

`EDIFHierCellInst`:

- `getFullHierarchicalInstName`
- `getCellType`
- `getCellName`
- `getParent`
- `getChild(String/EDIFCellInst)`
- `getNet(String)`
- `getPortInst(String)`
- `getHierPortInsts`
- `isDescendantOf`
- `getCommonAncestor`
- `isUniquified`
- `ensureAncestorsAreUniquified`

`EDIFHierPortInst`:

- `getFullHierarchicalInstName`
- `getFullHierarchicalInst`
- `getHierarchicalNet`
- `getHierarchicalNetName`
- `isInput`
- `isOutput`
- `getRoutedSitePinInst(Design)`
- `getAllRoutedSitePinInsts(Design)`
- `getPhysicalCell(Design)`
- `getRoutedBELPin(Design)`
- `getRoutedPhysicalNet(Design)`
- `getPortInParent`
- `getInternalNet`

`EDIFNet`:

- `getPortInsts`
- `getSourcePortInsts(boolean includeTopLevel)`
- `getPortInst(EDIFCellInst, pinName)`
- `createPortInst(...)`
- `removePortInst(...)`
- `isVCC`
- `isGND`
- `getPhysStaticSourceType`
- `isInternalToParent`
- `rename`

Contest use:

- Vivado critical path pins usually map through `EDIFNetlist.getHierPortInstFromName`.
- From an `EDIFHierPortInst`, use `getRoutedPhysicalNet(design)` and `getRoutedSitePinInst(design)` to jump to physical route.
- After adding/removing logical nets/cells, reset parent net maps and re-normalize physical pins.
- Avoid ad hoc string splitting of hierarchy names unless no API exists; RapidWright already handles separators and escaped names.

---

## Appendix B: Detailed Routing, Timing, And Route-Tree Map

This appendix turns the routing subagent scan into a more actionable development guide.

### B.1 Route State Is Not A Single Enum

There is no simple design-level `RouteStatus` enum for all nets. The class at `RapidWright/src/com/xilinx/rapidwright/util/RouteStatus.java` is a comparison bean used by `CompareRouteStatusReports`, not the main status authority.

The status sources are:

- `SitePinInst.isRouted()` for per-pin cached route state.
- `Net.getPIPs()` for physical PIP list.
- `ReportRouteStatus.reportRouteStatus(Design)` for RW route legality summary.
- Vivado `report_route_status` for final tool authority.

Implication:

- After manual PIP edits, `SitePinInst.isRouted()` can be stale.
- Always run `DesignTools.updatePinsIsRouted(design)` before `ReportRouteStatus`.
- For contest scoring, still use Vivado route/timing reports.

### B.2 Route Tree Reconstruction

Primary source:

- `RapidWright/src/com/xilinx/rapidwright/design/NetTools.java`

Important methods:

- `NetTools.getNodeTrees(Net)`
- `NetTools.getNetTreeString(Net)`
- `NetTools.isGlobalClock(Net)`
- `NetTools.findClockRootVRoute(...)`

What `getNodeTrees` does:

1. Reads `net.getPIPs()`.
2. Builds node adjacency using PIP start/end nodes.
3. Respects `pip.isReversed()` when deciding actual direction.
4. Starts from output pin or tied VCC/GND node.
5. Ignores gap/island artifacts where appropriate.
6. Marks multiply-driven/cyclic tree states.

When to use:

- Detect unexpected multi-driver routes.
- Build fanout tree depth features.
- Convert PIP list into branch structure.
- Compare route-tree shape before and after an ECO.

### B.3 Single-Sink Path Extraction

Primary sources:

- `DesignTools.getConnectionPIPs(SitePinInst)`: sink-to-source PIP path.
- `RouterHelper.getPIPsFromNodes(...)`: rebuild PIPs from a node path.
- `RouterHelper.getNodesOfNet(Net)`: get route nodes used by a net.

Use case:

- Given a critical sink pin, find only the route segment that feeds it.
- Compute per-sink PIP count, node types, route length, and detour.
- Selectively unroute just this branch.

Algorithm sketch:

```text
pin_name from Vivado timing
  -> EDIFNetlist.getHierPortInstFromName(pin_name)
  -> EDIFHierPortInst.getRoutedSitePinInst(design)
  -> SitePinInst sink
  -> sink.getNet()
  -> DesignTools.getConnectionPIPs(sink)
```

### B.4 Selective Unroute Strategy

Prefer the narrowest operation that matches the change:

| Intent | API | Scope | Risk |
|---|---|---:|---|
| Remove one sink route | `DesignTools.unroutePins(net, pins)` | selected branch | low |
| Remove one pin | `Net.unroutePin(spi)` | one pin branch | medium |
| Remove pin and preserve route | `Net.removePin(spi, true)` | pin + trimmable branch | medium |
| Remove all routing on net | `Net.unroute()` | whole net | high |
| Remove all design routing | `Design.unrouteDesign()` | all nets | very high |

`DesignTools.unroutePins` is the contest default for custom ECO because it tries to preserve unrelated routing.

Source pointers:

- `DesignTools.unroutePins(Net, Collection<SitePinInst>)`
- `DesignTools.unrouteSourcePin(SitePinInst)`
- `DesignTools.unrouteSourcePins(List<SitePinInst>)`
- `DesignTools.getTrimmablePIPsFromPins(...)`
- `DesignTools.removeConnectedRouting(Net, Node)`

### B.5 RWRoute Full Routing Flow

Primary class:

- `RapidWright/src/com/xilinx/rapidwright/rwroute/RWRoute.java`

Mental model:

```text
Design
  -> RWRoute.preprocess()
  -> categorize nets
  -> create NetWrapper per net
  -> create Connection per sink
  -> initialize RouteNodeGraph
  -> iterative rip-up/reroute over connections
  -> setPIPsOfNets()
  -> checkPIPsUsage()
  -> write DCP
```

Important methods:

- `preprocess`
- `initialize`
- `determineRoutingTargets`
- `categorizeNets`
- `createNetWrapperAndConnections`
- `route`
- `routeIndirectConnectionsIteratively`
- `evaluateCostAndPush`
- `updateCostFactors`
- `setPIPsOfNets`
- `checkPIPsUsage`
- `routeDesignFullTimingDriven`
- `routeDesignFullNonTimingDriven`
- `routeDesignWithUserDefinedArguments`

Key data structures:

- `NetWrapper`: net-level wrapper, source/alternate source, fanout, HPWL.
- `Connection`: one source-to-sink route target, criticality, bounding box, timing edges, route nodes.
- `RouteNode`: routed resource with occupancy/cost/parent.
- `RouteNodeGraph`: owns route node factory, preserve map, excluded tiles, congestion costs.
- `RouteNodeGraphTimingDriven`: delay-estimator backed graph with RCLK masking.

Important warning:

- Full `RWRoute` is not a preserve-first incremental repair. It unroutes target nets and writes fresh PIPs.
- In previous contest experiments, RWRoute QoR did not match Vivado timing QoR. Use it as an analysis/experiment tool, not default final router.

### B.6 PartialRouter Incremental Routing Flow

Primary class:

- `RapidWright/src/com/xilinx/rapidwright/rwroute/PartialRouter.java`

Mental model:

```text
Existing routed design
  -> choose pinsToRoute, or null for all currently unrouted pins
  -> preserve already routed pins/nets
  -> route only targets
  -> optionally softPreserve: release blocking preserved nets if needed
  -> write modified DCP
```

Important methods:

- `preprocess(Design)`
- `getUnroutedPins(Design)`
- `routeDesignWithUserDefinedArguments(...)`
- `routeDesignPartialNonTimingDriven(Design, pinsToRoute)`
- `routeDesignPartialNonTimingDriven(Design, pinsToRoute, softPreserve)`
- `routeDesignPartialTimingDriven(Design, pinsToRoute, softPreserve)`
- `preserveNet`
- `unpreserveNet`
- `handleUnroutableConnection`

Contest default:

- If you changed a few cells/nets in RW, mark affected sink pins unrouted and use `PartialRouter`.
- If `PartialRouter` cannot resolve conflicts, try `softPreserve=true` to release blocking preserved routes.
- Still pass result to Vivado for final route/timing authority.

### B.7 RWRouteConfig Knobs

Common command shape:

```bash
RapidWright/bin/rapidwright RWRoute input.dcp output.dcp --maxIterations 100 --nonTimingDriven
RapidWright/bin/rapidwright PartialRouter input.dcp output.dcp --fixBoundingBox --useUTurnNodes --verbose
```

Important options:

- `--maxIterations`: runtime/quality bound.
- `--timingDriven` / `--nonTimingDriven`: turn timing model on/off.
- `--wirelengthWeight`: faster/shorter wire tradeoff.
- `--timingWeight`: timing-vs-runtime tradeoff.
- `--timingMultiplier`: experimental adaptive timing weight.
- `--criticalityExponent`: sharpens criticality.
- `--minRerouteCriticality`: controls critical connection reroute set.
- `--reroutePercentage`: max percentage of critical connections to reroute.
- `--presentCongestionMultiplier`: current overuse cost growth.
- `--historicalCongestionFactor`: long-term congestion penalty.
- `--boundingBoxExtensionX/Y`
- `--enlargeBoundingBox`
- `--fixBoundingBox`
- `--useUTurnNodes`
- `--maskNodesCrossRCLK`
- `--lutPinSwapping`
- `--lutRoutethru`
- `--pblock`
- `--verbose`

For contest:

- `--nonTimingDriven` can be useful for quick legality repair.
- `--timingDriven` can rank critical connections but may not correlate perfectly with Vivado.
- `--fixBoundingBox` and `--useUTurnNodes` are often used by partial routes for routability.

### B.8 TimingManager And TimingGraph

Primary classes:

- `RapidWright/src/com/xilinx/rapidwright/timing/TimingManager.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingGraph.java`
- `RapidWright/src/com/xilinx/rapidwright/timing/TimingEdge.java`

`TimingManager`:

- Builds timing model and graph.
- Parses XDC-like timing requirement.
- Computes arrival/required times.
- Calculates connection criticality.
- Prints critical path info.

`TimingGraph`:

- `build(boolean isPartial, Collection<Net> targetNets)`
- `addNetDelayEdges(Net)`
- `getMaxDelay()`
- `getPathDelay(GraphPath)`
- `getWorstSlack()`
- `getMaxDelayPath()`
- `getCriticalTimingEdgesInOrder(...)`
- `getTimingEdgeOfPath(...)`
- `getTimingPath(src, sink)`
- `getTimingEdgeConnectionMap()`
- `setTimingEdgesOfConnections(...)`

`TimingEdge`:

- `getLogicDelay()`
- `getNetDelay()`
- `getIntraSiteDelay()`
- `getDelay()`
- `setRouteDelay(short)`
- `getSrc()`
- `getDst()`
- `getNet()`
- `delaysInfo()`

Useful route/timing join:

```text
TimingGraph critical edge
  -> TimingEdge.getNet()
  -> TimingGraph.getTimingEdgeConnectionMap()
  -> Connection
  -> Connection.getSink()
  -> SitePinInst
  -> DesignTools.getConnectionPIPs(sink)
```

This is the right path for sink-level timing-aware route analysis.

### B.9 DelayEstimatorBase

Primary class:

- `RapidWright/src/com/xilinx/rapidwright/timing/delayestimator/DelayEstimatorBase.java`

Useful APIs/behavior:

- `getDelayOf(Node)` gives node-group delay.
- Long-to-long extra delay handled separately.
- If U-turn nodes are disabled, some nodes get a large sentinel delay.

Contest use:

- Good for relative route-cost estimates.
- Do not treat as final timing; Vivado timing wins.

### B.10 TimingAndWirelengthReport

Primary class:

- `RapidWright/src/com/xilinx/rapidwright/rwroute/TimingAndWirelengthReport.java`

What it does:

- Reads DCP.
- Normalizes physical net names and missing SPIs.
- Builds `TimingManager`.
- Builds `RouteNodeGraphTimingDriven`.
- Iterates wire nets.
- Uses `RouterHelper.getNodesOfNet(net)` for used nodes.
- Uses `RouteNode.getLength(node, routingGraph)` for wirelength.
- Uses `RouterHelper.getSourceToSinkINTNodeDelays(net, estimator)` for source-to-sink delays.
- Prints critical path and wirelength.

Run:

```bash
RapidWright/bin/rapidwright com.xilinx.rapidwright.rwroute.TimingAndWirelengthReport input.dcp
```

Contest use:

- Template for writing your own wirelength/detour report.
- Copy its setup sequence when timing graph edge association fails.

### B.11 Route Legality And Conflict Checks

RapidWright route status:

- `ReportRouteStatus.reportRouteStatus(Design)` computes logical, routable, fully routed, unrouted, some-unrouted, and conflict counts.
- It records used end nodes for each PIP to detect cross-net conflicts.
- It does not validate logical netlist correctness or full Vivado placement legality.

Vivado route status:

- `VivadoTools.reportRouteStatus(Design)` writes a temporary DCP and calls Vivado.
- `VivadoTools.reportRouteStatus(design, netName)` can query single-net status.
- This is heavier but closer to final truth.

Contest gate:

1. RW `ReportRouteStatus` as cheap preflight.
2. Vivado `report_route_status`.
3. Vivado hold/pulse/timing.
4. Functional validation if design is a candidate for final.

### B.12 Routing Hooks Worth Implementing

Critical sink local reroute:

- Input: Vivado critical path pin names.
- Map to `SitePinInst`.
- `DesignTools.unroutePins(net, selectedSinks)`.
- `PartialRouter.routeDesignPartialTimingDriven(design, selectedSinks, true)`.
- Vivado route/timing.

Detour detector:

- For each critical sink, get connection PIPs.
- Sum tile Manhattan distance over PIP node hops.
- Divide by source-sink tile Manhattan distance.
- Flag high ratio and high Vivado net delay.

Congestion heat map:

- Iterate all nets and PIPs.
- Map actual end nodes to users.
- Count nodes used by multiple nets.
- Aggregate by tile, clock region, intent code.

Route-tree sharing feature:

- `NetTools.getNodeTrees`.
- Count trunk length, branch count, max depth, number of sinks reached.
- Useful for fanout replication and sink clustering.

Timing edge physical annotation:

- Use `TimingGraph` critical edges.
- Join to net and sink.
- Attach placement distance, PIP count, node intent histogram.

RWRoute cost-function fork:

- Start from `RWRoute.evaluateCostAndPush`.
- Private `getNodeCost` limits fine-grained extension; likely fork rather than subclass.
- Add biases for avoiding hot tiles, avoiding SLR/Laguna detours, or preferring known good columns.

---

## Appendix C: Detailed Placement And ECO Cookbook

This appendix is for implementing new physical optimization recipes.

### C.1 Safe Move Checklist

Before moving a cell:

- Confirm `cell != null`.
- Confirm `cell.isPlaced()`.
- Record `oldSite`, `oldBEL`, `oldTile`, `cell.getPhysicalPinMappings()`.
- Record connected physical nets before unplacing.
- Decide whether this is LUT, FF, carry, DSP, BRAM, or special primitive.
- Avoid clocks/static nets unless recipe is specifically designed for them.

Move:

```java
Map<Net, Set<SitePinInst>> deferred = new HashMap<>();
DesignTools.fullyUnplaceCell(cell, deferred);
design.placeCell(cell, newSite, newBEL, cell.getPhysicalPinMappings());
cell.getSiteInst().routeSite();
DesignTools.batchRemoveSitePins(deferred, true);
```

After move:

- Unroute affected sink branches, not whole nets if possible.
- `DesignTools.createMissingSitePinInsts(design)`.
- `DesignTools.updatePinsIsRouted(design)`.
- `ReportRouteStatus.reportRouteStatus(design)`.
- Write DCP and use Vivado `route_design`/timing.

### C.2 Why `fullyUnplaceCell` Matters

`DesignTools.fullyUnplaceCell` handles the site details that raw `Cell.unplace` does not:

- Shared control pins: CLK, CE, SR/RST.
- Whether another cell still uses a site wire.
- RBEL and SitePIP traversal.
- Routethru cells tied to the cell being removed.
- LUT5/LUT6 shared A6 static pin.
- Alternate source for dual-output cases.
- Site pin removal and optional deferred removal map.

If a recipe moves an already-routed LUT/FF and uses raw `Cell.unplace`, expect pin mapping failures or stale site routing in Vivado.

### C.3 Deferred Site Pin Removal

Use deferred removals when multiple edits may touch the same net:

```java
Map<Net, Set<SitePinInst>> deferred = new HashMap<>();

DesignTools.fullyUnplaceCell(cellA, deferred);
DesignTools.fullyUnplaceCell(cellB, deferred);

// reconnect or place cells

DesignTools.batchRemoveSitePins(deferred, true);
```

Why:

- Avoid repeated expensive branch trimming.
- Avoid deleting a route branch that a later edit reuses.
- Preserve unrelated routes where possible.

### C.4 Destination Site/BEL Search

For LUT/FF ECO, prefer `ECOPlacementHelper`:

- `ECOPlacementHelper.getCentroidOfPoints(device, points, targetSiteTypes)`
- `ECOPlacementHelper.getCentroidOfNet(net, targetSiteTypes)`
- `ECOPlacementHelper.spiralOutFrom(site)`
- `ECOPlacementHelper.spiralOutFrom(site, pblock)`
- `ECOPlacementHelper.getUnusedFlop(siteInst, clkNet)`
- `ECOPlacementHelper.getUnusedLUT(siteInst)`

FF-specific concerns:

- FF BEL must be free.
- Shared CLK/CE/SR must be compatible.
- Bypass input and pin-bounce constraints can block placement.
- Helper tends to use `AFF..HFF` and avoid FF2 variants.

LUT-specific concerns:

- 5LUT/6LUT sharing matters.
- Static-source usage can reserve LUT resources.
- Companion LUT pin mapping may constrain legal swaps.

### C.5 ECOTools Disconnect/Reconnect

`ECOTools.disconnectNet`:

- Updates logical EDIF connections.
- Updates physical site pin and site routing.
- Can use deferred removals.
- Has a property to skip intra-site unroute when preserving site routing for reuse.

`ECOTools.connectNet`:

- Handles hierarchy crossing.
- Creates needed physical site pins.
- Routes intra-site connection.
- May remap LUT input pins if conflict exists.

`ECOTools.createExitSitePinInst`:

- Creates or reuses a site pin for a primitive pin.
- Routes the primitive BEL pin to the site pin.
- Handles LUT input remapping.

Use these instead of manually editing both EDIF and physical pins unless the recipe is very narrow.

### C.6 FanOutOptimization

Primary method:

- `FanOutOptimization.cutFanOutOfRoutedNet(design, net, splitByCount, onlyUseEmptySites)`

Supported drivers:

- FF: `FDRE`, `FDSE`, `FDCE`, `FDPE`
- LUT: `LUT1` through `LUT6`

Flow:

1. Collect sinks of high fanout net.
2. For LUT replication, include source pins of nets driving the LUT so placement accounts for inputs.
3. KMeans clusters sink/source points.
4. Find centroid for each cluster.
5. Find valid placement near centroid.
6. Move original driver for first cluster.
7. Create driver copies for later clusters.
8. Connect copied inputs and control pins.
9. Disconnect/reconnect sink groups.

Contest use:

- Good for control nets and replicated register sources.
- Watch for over-replication causing congestion.
- Vivado `phys_opt_design -force_replication_on_nets` may be stronger for final QoR; RW version is useful for deterministic custom strategies.

### C.7 LUT Pin Swap

Primary source:

- `RapidWright/src/com/xilinx/rapidwright/design/tools/LUTTools.java`

Useful methods:

- `swapMultipleLutPins`
- `swapLutPinsFromPIPs`
- `configureLUT`
- `getLUTEquation`

What swap does:

- Updates logical-to-physical mapping.
- Updates physical-to-logical mapping.
- Updates SPI names when necessary.
- Updates intra-site routing.

Use cases:

- Critical route enters a slow or inconvenient LUT input.
- Post-placement physical pin mapping does not match desired PIP route.
- A direct/shorter site pin alternative exists.

Cautions:

- SRL pin swap is not supported.
- Dual LUTs in same slice can constrain each other.
- Always validate function and timing.

### C.8 Inline LUT1

Tools:

- `LUT1InsertionTool.insertLUT1`
- `LUT1InsertionTool.getFreeLUTLocation`
- `ECOTools.createAndPlaceInlineCellOnInputPin`

Pattern:

```text
original net -> inserted LUT1 input
inserted LUT1 output -> original sink
```

Valid uses:

- Buffer-like replication near a sink.
- Inverter insertion when logically intended.
- Creating a physical anchor/routethru-like element.

Contest warning:

- Logic must remain equivalent and latency unchanged.
- Treat as higher-risk than movement or replication.

### C.9 RelocationTools

Primary source:

- `RapidWright/src/com/xilinx/rapidwright/design/tools/RelocationTools.java`

What it can do:

- Relocate a hierarchy by tile offsets.
- Move associated `SiteInst`s.
- Translate PIPs where compatible.
- Handle boundary nets with selective/full unroute rules.

Constraints:

- Destination site instances must not mix hierarchy-internal and external cells.
- Some PIPs may not exist at target tile.
- Static nets and clock nets need special handling.
- Boundary source/sink relation determines whether selective branch unroute is enough or full net unroute is needed.

Use cases:

- Moving a repeated module to a better clock region.
- Testing SLR offsets.
- Reusing relative placement of a known-good block.

### C.10 ModuleInst And BlockPlacer

`ModuleInst.place(site, skipIncompatible, allowOverlap)`:

- Places a module relative to its anchor.
- Checks compatible sites.
- Can forbid overlap.
- Handles special RAMB18/RAMB36 mutual exclusion.

`ModuleInst.unplace`:

- Clears instance placement.
- Clears module PIPs.
- Static net PIPs inserted by the module are removed carefully.

`BlockPlacer2`:

- Annealing-style placement for module-level objects.
- Has initial placement, range-limited moves, accept/undo, and push-away overlap handling.

Contest use:

- More relevant for replicated/hard IP/module-level moves than single LUT/FF timing ECO.
- Could be useful for hidden benchmarks with repeated accelerators or shells.

### C.11 Placement/ECO Recipe Templates

Single critical cell move:

```text
Vivado critical path -> pin list
RW map pins -> interior cell
collect connected nets -> centroid
find compatible site/BEL near centroid
fullyUnplaceCell + placeCell + routeSite
selective unroute affected sinks
PartialRouter or Vivado route
measure target clock WNS
```

High fanout replication:

```text
Vivado high fanout critical source
RW find physical net
cluster sinks by tile/clock region
replicate FF/LUT driver
reconnect sink groups
route new branches
measure Vivado timing
```

LUT pin swap:

```text
critical LUT input has high net delay
enumerate equivalent LUT inputs
check existing site/BEL pin mapping
swap with LUTTools
routeSite/update route status
measure Vivado timing
```

SLR/hierarchy move:

```text
identify hierarchy with bad placement
check mixed site-inst constraint
try tile offset with RelocationTools
repair boundary nets
route and measure
```

### C.12 ECO Pitfalls

- Moving FFs across incompatible clock/control sets will break placement or route.
- Moving LUTs without preserving pin mappings can silently change routeability or function.
- Removing pins without cleaning `SiteInst` can leave stale site pins.
- Whole-net unroute can destroy a currently good route tree for unrelated sinks.
- Static source routing may be implicit and not represented like normal nets.
- Dual-output LUT/SLICE alternatives need explicit handling.
- `routeSite()` repairs intra-site connectivity but does not solve inter-site routing.
- `DesignTools.createMissingSitePinInsts` should run after major route/placement edits.

---

## Appendix D: Detailed Checkpoint, Interchange, EDIF, And Tooling Map

### D.1 Build System

Files:

- `RapidWright/README.md`
- `RapidWright/build.gradle`
- `RapidWright/common.gradle`
- `RapidWright/bin/rapidwright`
- `RapidWright/scripts/invoke_rapidwright.sh`

Build:

```bash
cd RapidWright
./gradlew compileJava
```

Gradle facts:

- Main app: `com.xilinx.rapidwright.MainEntrypoint`.
- Standalone app: `com.xilinx.rapidwright.StandaloneEntrypoint`.
- Source dir: `src`.
- Test dir: `test/src`.
- Class output: `bin`.
- Jars copied into `jars`.
- Main launcher jar: `build/libs/main.jar`.

Launcher facts:

- `bin/rapidwright --list-apps` enumerates registered `MainEntrypoint` apps.
- If first arg starts with `com.xilinx.rapidwright.`, launcher runs that class directly.
- Otherwise it dispatches app name through `MainEntrypoint`.

### D.2 MainEntrypoint App Inventory

High-value apps for contest:

- `ReportRouteStatus`: cheap route legality summary.
- `ReportTimingExample`: RW timing model demo.
- `TimingAndWirelengthReport`: not always registered, run by full class name.
- `RWRoute`: full router.
- `PartialRouter`: incremental router.
- `DcpToInterchange`: DCP to `.netlist/.phys/.xdc`.
- `PhysicalNetlistToDcp`: interchange back to DCP.
- `DesignComparator`: compare design structures.
- `DesignImplementationDiff`: implementation diff.
- `CompareRouteStatusReports`: compare detailed Vivado route status reports.
- `RelocationTools`: relocation helper CLI/query.
- `CopyImplementation`: copy placement/routing implementation.
- `LUTTools`: LUT inspection/modification utilities.
- `NetlistClockDetection`: clock detection.

Examples worth reading:

- `CustomRouting`: create design, route, custom PIP route, lock route.
- `CountRoutedNets`: route status by pin.
- `ReportTimingExample`: TimingManager/TimingGraph minimal usage.
- `UpdateRoutingUsingSATRouter`: unroute non-clock/static nets and SAT route within pblock.
- `RunSATRouterExample`: SAT router example.
- `RelocateHierarchy`: hierarchy relocation demo.
- `StampPlacement`: repeated placement stamping.
- `DecomposeLUT`: logic/LUT edit example.
- `ExampleNetlistCreation`: EDIF netlist creation.

### D.3 DCP Read/Write Path

`Design.readCheckpoint` high-level behavior:

- Reads DCP zip.
- Reads EDIF from DCP if present.
- Can use external EDIF if supplied.
- May auto-generate readable EDIF through Vivado depending on settings.
- Reads placement/routing physical content.
- Handles encrypted cells via companion Tcl where applicable.

Relevant toggles:

- `Design.isAutoGenerateReadableEdif`
- `Design.setAutoGenerateReadableEdif`
- `Design.readEdifAndXdefInParallel`
- `Design.setReadEdifAndXdefInParallel`

`Design.writeCheckpoint`:

- Writes physical checkpoint.
- Can use external EDIF file for detached netlist paths.
- May generate companion Tcl for encrypted designs.

Practical advice:

- Use absolute output paths for contest pipeline.
- Preserve companion `.tcl` if generated.
- Write best known legal DCP frequently enough to survive timeout.

### D.4 EDIF Load/Export

Primary source:

- `RapidWright/src/com/xilinx/rapidwright/edif/EDIFTools.java`
- `RapidWright/src/com/xilinx/rapidwright/edif/EDIFNetlist.java`

Important functions:

- `EDIFTools.loadEDIFStream`
- `EDIFTools.loadEDIFFile`
- `EDIFTools.readEdifFile`
- `EDIFTools.writeEDIFFile`
- `EDIFTools.readEdifFromZipFile`
- `EDIFTools.connectPortInstsThruHier`
- `EDIFTools.uniqueifyNetlist`
- `EDIFNetlist.exportEDIF`
- `EDIFNetlist.readBinaryEDIF`
- `EDIFNetlist.writeBinaryEDIF`

Binary EDIF:

- `.bedf` uses Kryo + Zstd.
- Controlled by EDIF binary caching settings.

Important gotchas:

- Shared EDIF cell definitions mean editing one instance can edit all instances unless uniquified.
- `uniqueifyNetlist` may reject designs with `ModuleInst` unless flattened.
- After logical edits, parent/physical maps can be stale.

### D.5 Interchange Read/Write Path

Primary source:

- `RapidWright/src/com/xilinx/rapidwright/interchange/Interchange.java`
- `LogNetlistReader/Writer.java`
- `PhysNetlistReader/Writer.java`

Formats:

- `.netlist`: logical netlist.
- `.phys`: physical netlist.
- `.xdc`: constraints.
- Usually gzip Cap'n Proto, not text.

Read:

- `Interchange.readInterchangeDesign(root)`
- Finds same-root logical/physical/xdc.
- Builds `Design`, netlist, placement, routing, constraints.

Write:

- `Interchange.writeDesignToInterchange(design, rootName)`
- Writes `.netlist`.
- Writes `.phys` if physical content exists.
- Writes `.xdc` if constraints exist.

CLI:

```bash
RapidWright/bin/rapidwright DcpToInterchange input.dcp
RapidWright/bin/rapidwright PhysicalNetlistToDcp input.netlist input.phys input.xdc output.dcp
RapidWright/bin/rapidwright EdifToLogicalNetlist input.edf output.netlist
RapidWright/bin/rapidwright LogicalNetlistToEdif input.netlist output.edf
```

Contest use:

- Interchange is useful for serialization experiments and debugging route branches.
- DCP remains the required bridge to Vivado.

### D.6 Utility Tools

`FileTools`:

- `getRapidWrightPath`
- Zip/DCP stream helpers.
- `runCommand`
- `execCommandGetOutput`
- Vivado version/path helpers.
- `blockSystemExitCalls`
- gzip helpers.

`CodePerfTracker`:

- `start`
- `stop`
- `printSummary`
- GC and OS memory tracking.

`VivadoTools`:

- `reportRouteStatus(Design)`
- `reportRouteStatus(Design, netName)`
- `routeDesignAndGetStatus`
- `placeAndRouteDesignAndGetStatus`
- DCP/workdir overloads.

Use `VivadoTools` for Java-driven local debugging; the contest agent mostly uses `VivadoMCP`.

---

## Appendix E: Detailed Contest Repo Integration

### E.1 Two Agent Loops

Original:

- `dcp_optimizer.py`
- Tool-calling/ReAct style.
- Starts RapidWright MCP and Vivado MCP.
- LLM iterates with tools.
- Tracks best WNS and best legal WNS.

Newer:

- `FDAgents/`
- Compact-state, skill-based agent.
- LLM picks from recipe/skill menu.
- Skills are Python modules with explicit flows.
- Easier to stabilize and benchmark.

Shared principle:

- Vivado owns measurement and final legality.
- RapidWright owns structural analysis and deterministic ECO.

### E.2 `dcp_optimizer.py` Map

Important areas:

- MCP lifecycle: starts Vivado and RW servers.
- Clock period query: target `clk_fpl26contest`.
- WNS query: clock-specific WNS, not overall summary.
- Tool-call wrapper: dispatches `vivado_*` and `rapidwright_*`.
- Best checkpoint promotion: only legal improvements get copied to output.
- Initial analysis: RW init, Vivado open/timing, high fanout, critical spread.
- Test mode: deterministic pipelines for fanout, LogicNets pblock, VexRiscv re-place.

Key behavior:

- `best_wns` can track non-legal progress.
- `best_legal_wns` is what should be promoted.
- Hold and pulse checks matter.
- Route status must be fully legal before output promotion.

### E.3 `RapidWrightMCP/rapidwright_tools.py`

Core wrapper state:

- `_initialized`
- `_current_design`

Initialization:

- Sets `RAPIDWRIGHT_PATH` to repo-local `RapidWright`.
- Sets `CLASSPATH` to `RapidWright/bin:RapidWright/jars/*`.
- Imports `rapidwright`, which starts JVM.

Basic tools:

- `initialize_rapidwright`
- `get_supported_devices`
- `get_device_info`
- `read_checkpoint`
- `write_checkpoint`
- `get_design_info`
- `search_cells`
- `search_nets`

Timing/analysis tools:

- `report_approx_timing`
- critical path spread / placement analysis helpers.
- pblock search helpers.
- high fanout analysis.

Optimization tools:

- fanout split.
- LUT cone merge.
- pblock fabric search/convert.
- `analyze_net_detour`
- `optimize_cell_placement`
- `optimize_critical_path_detours`

Known weaknesses:

- Some tools return JSON-like dicts but catch broad exceptions.
- `optimize_cell_placement` currently picks simplistic BEL names (`AFF`, `A6LUT`) and uses empty-site-only logic.
- It calls `net.unroute()` on all connected nets, which is more disruptive than necessary.
- Approx timing should not drive final decisions alone.

Improvement targets:

- Replace whole-net unroute in cell move with per-sink `DesignTools.unroutePins`.
- Add BEL/site compatibility helper using `ECOPlacementHelper`.
- Add affected-net/pin reporting.
- Add route status preflight before writing.

### E.4 `VivadoMCP/vivado_mcp_server.py`

Core role:

- Long-running Vivado Tcl process through pexpect.
- Arbitrary Tcl command wrapper.
- Timing, pblock, phys_opt, route, checkpoint tools.

High-value capabilities:

- `run_tcl_command`: escape hatch for any Tcl.
- open/write checkpoint.
- `report_timing_summary`.
- target clock WNS query.
- critical path cells.
- critical path pins.
- high fanout extraction.
- pblock creation/application/resource validation.
- `phys_opt_design` directive wrappers and force replication.
- route status and route design.

Fragility:

- Text/prompt parsing.
- Timeouts can leave pending command state.
- Some Vivado errors only appear in textual output.
- Use defensive Tcl and short commands where possible.

### E.5 FDAgents Map

Files:

- `FDAgents/agent.py`
- `FDAgents/mcp_client.py`
- `FDAgents/memory.py`
- `FDAgents/decision.py`
- `FDAgents/llm.py`
- `FDAgents/skills/*.py`

Skills:

- `phys_opt`: Vivado physical optimization directives.
- `fanout_opt`: fanout replication/splitting.
- `cell_replace`: cell relocation/replacement.
- `pblock`: pblock topology experiments.
- `lut_merge`: LUT input cone merge.
- `per_net_unroute`: selective net unroute / force replication style flows.
- `custom`: escape hatch for arbitrary tool use.

Policy:

- Compact state for LLM to reduce token pressure.
- Accept only legal improvements above threshold.
- Reject and roll back to `current_best.dcp`.
- Log actions and outcomes for batch analysis.

Why this matters:

- It is the right home for deterministic recipes discovered by development agents.
- Runtime/cost pressure means LLM should choose among known recipes rather than invent Tcl/RW from scratch every time.

### E.6 Contest Docs Facts To Keep In Mind

Scoring:

- Score driven by Fmax improvement minus proportional OpenRouter cost and runtime penalty.
- Bad/invalid output gets zero for that benchmark.
- The best DCP must already be written when timeout happens.

Runtime:

- AWS `m7a.2xlarge`
- 8 vCPU
- 32 GB RAM
- Vivado 2025.1

Benchmarks:

- Device target: `xcvu3p-ffvc1517-2-e`.
- Public designs include VexRiscv, Rosetta, LogicNets, FINN, BOOM, ISPD, etc.
- Target clock: `clk_fpl26contest`.

Validation:

- Must be fully routed.
- No routing errors.
- Hold met.
- Pulse width met.
- Functional equivalence.

### E.7 VexRiscv Recipe Lessons

Document:

- `docs/vexriscv_optimization_recipe.md`

Key result:

- Public VexRiscv recipe improved about 310 MHz to about 479 MHz.

Winning moves:

- Vivado pipeline with selected directives.
- Force replication on specific critical `MUL_LOW` nets.
- Full-height vertical pblock around critical region, not a small rectangle.
- Iterative double force-replication on current top-critical nets.
- Per-net unroute instead of global unroute in later stages.

Negative results:

- Tiny pblock around critical cells did not work.
- Single cell move often moved bottleneck or worsened route.
- RWRoute replacement routing underperformed Vivado for that design.
- Over-replication cliff exists.
- Placement-stage gains can disappear after route.

General lesson:

- Topology constraints that guide Vivado placer/router can outperform manual single-cell moves.
- Timing bottleneck shifts after each successful optimization; recipes must re-measure and adapt.

### E.8 Future Development Modules

Recommended modules:

- `validation-guardrails`: central legal/better promotion gate.
- `vivado-recipes`: deterministic Tcl recipes with measured outcomes.
- `rapidwright-eco`: safer RW cell/EDIF/sink-level mutation library.
- `routing-analysis`: route tree and timing-edge feature extraction.
- `pblock-topology`: path/column/SLR-aware pblock generation.
- `benchmark-mining`: classify benchmarks and choose recipe families.
- `experiment-db`: persistent outcome store for recipes and runs.

---

## Appendix F: Raw Subagent Scan Digest

This appendix preserves the essential output of the six exploratory agents in a compact but less polished form. Use this when the summary above is too abstract.

### F.1 Core Model Agent Digest

Scope scanned:

- `RapidWright/src/com/xilinx/rapidwright/design`
- `RapidWright/src/com/xilinx/rapidwright/device`
- `RapidWright/src/com/xilinx/rapidwright/edif`
- jar APIs via `rapidwright-api-lib`

Findings:

- `Design/Cell/Net/SiteInst/SitePinInst` and `Device/Tile/BEL/Node/PIP` are jar APIs.
- `Design` owns `Device`, `EDIFNetlist`, physical cells/nets/sites.
- `Device` maps part names to architecture tiles/sites/nodes/PIPs.
- `Cell` ties physical placement to logical EDIF instance.
- `Net` owns physical pins and PIPs, with optional logical net association.
- `SiteInst` owns configured site state.
- `SitePinInst` bridges site routing and inter-site routing.
- `EDIFNetlist` owns hierarchy and maps logical pins to physical pins/nets.

Key lookup APIs:

- `Design.getCell/getNet/getSiteInst/getSiteInstFromSiteName/getSiteInstFromSite/isSiteUsed`
- `Device.getTile/getSite/getNode/getPIP/getWire/getBEL`
- `EDIFNetlist.getCellInstFromHierName`
- `EDIFNetlist.getHierCellInstFromName`
- `EDIFNetlist.getNetFromHierName`
- `EDIFNetlist.getHierNetFromName`
- `EDIFNetlist.getHierPortInstFromName`
- `EDIFHierPortInst.getPhysicalCell`
- `EDIFHierPortInst.getRoutedPhysicalNet`
- `EDIFHierPortInst.getRoutedSitePinInst`
- `DesignTools.getPortInstsFromSitePinInst`

Key mutation APIs:

- `Design.createCell/createAndPlaceCell/placeCell/createSiteInst/createNet`
- `Net.connect/createPin/addPin/removePin/setSource/removeSource`
- `Net.unroute/addPIP/removePIP/lockRouting/unlockRouting`
- `SiteInst.routeIntraSiteNet/unrouteIntraSiteNet`
- `EDIFCell.createChildCellInst/createPort/createNet`
- `EDIFNet.createPortInst/removePortInst/addPortInst`
- `EDIFTools.connectPortInstsThruHier`
- `LUTTools.configureLUT/getLUTEquation/swapLutPinsFromPIPs`

Gotchas:

- Reset `EDIFNetlist` parent maps after logical edits.
- Missing `SitePinInst`s are common on loaded DCPs.
- `SitePinInst.isRouted` is stale after manual PIP changes.
- EDIF name conflicts throw; use unique-name helpers.
- Static net physical pins have special handling.
- `ModuleInst` complicates uniqueification.

### F.2 Routing Agent Digest

Scope scanned:

- `router`
- `rwroute`
- `timing`
- route-related `DesignTools`/`NetTools`

Findings:

- `Net` PIPs/pins are jar APIs; route helper logic is in source.
- `DesignTools.unroutePins` is the key selective unroute API.
- `NetTools.getNodeTrees` reconstructs PIP route trees.
- `RWRoute` is full routing, not preserve-first.
- `PartialRouter` is the incremental/preserve-oriented path.
- `TimingManager` and `TimingGraph` can connect timing edges to physical nets/connections.
- `ReportRouteStatus` is a cheap RW legality summary.

High-value route APIs:

- `Net.getPIPs/getPins/getSinkPins/getSource`
- `Net.setPIPs/addPIP/removePIP/unroute/unroutePin`
- `DesignTools.unroutePins`
- `DesignTools.getConnectionPIPs`
- `DesignTools.updatePinsIsRouted`
- `RouterHelper.getNodesOfNet`
- `RouterHelper.getPIPsFromNodes`
- `PartialRouter.routeDesignPartialTimingDriven`
- `PartialRouter.routeDesignPartialNonTimingDriven`

Route hooks:

- Critical sink rip-up/reroute.
- Route-tree feature extraction.
- Congestion heat map from end-node conflicts.
- Timing-aware sink scoring.
- Wirelength/detour analysis.
- Incremental conflict healing via `softPreserve`.

### F.3 Placement/ECO Agent Digest

Scope scanned:

- `placer`
- `eco`
- `DesignTools`
- placement examples/tests

Findings:

- Raw `Cell.unplace()` is insufficient for routed designs.
- `DesignTools.fullyUnplaceCell` is the safe primitive for cell moves.
- Deferred removals plus `batchRemoveSitePins` preserve unrelated routes.
- `ECOTools` can disconnect/reconnect logical and physical nets together.
- `ECOPlacementHelper` has centroid and local site search helpers.
- `FanOutOptimization` implements KMeans sink clustering and FF/LUT replication.
- `LUTTools` handles safe LUT pin swaps.
- `RelocationTools` handles hierarchy/tile-offset relocation with boundary net logic.

High-value ECO APIs:

- `DesignTools.fullyUnplaceCell`
- `DesignTools.fullyRemoveCell`
- `DesignTools.unroutePins`
- `DesignTools.unrouteCellPinSiteRouting`
- `DesignTools.batchRemoveSitePins`
- `ECOTools.disconnectNet`
- `ECOTools.connectNet`
- `ECOTools.createExitSitePinInst`
- `ECOTools.createAndPlaceInlineCellOnInputPin`
- `ECOPlacementHelper.getUnusedFlop/getUnusedLUT/getCentroidOfNet/spiralOutFrom`
- `FanOutOptimization.cutFanOutOfRoutedNet`
- `LUTTools.swapMultipleLutPins`
- `RelocationTools.relocate`

Recipe suggestions:

- Single cell move.
- High fanout driver replication.
- LUT input pin swap.
- Inline LUT1 buffer/inverter.
- Remove unused logic cone.
- Hierarchy/SLR relocation.
- Module-level re-legalize.
- Copy/stamp implementation.

### F.4 Checkpoint/Runtime Agent Digest

Scope scanned:

- `interchange`
- `util`
- `bin`
- `scripts`
- `python`
- Gradle entry points

Findings:

- Reliable build: `cd RapidWright && ./gradlew compileJava`.
- Reliable app launch: `RapidWright/bin/rapidwright <AppName>`.
- Python JPype requires `RAPIDWRIGHT_PATH` and `CLASSPATH` when using local checkout.
- `Design` checkpoint APIs are jar APIs.
- Interchange read/write source is available locally.
- `ReportRouteStatus`, `ReportTimingExample`, `TimingAndWirelengthReport`, `CodePerfTracker`, and `FileTools` are primary debug tools.

Important CLI:

- `ReportRouteStatus <design.dcp>`
- `RWRoute <input.dcp|input.phys> <output.dcp>`
- `PartialRouter <input.dcp> <output.dcp>`
- `DcpToInterchange <input.dcp>`
- `PhysicalNetlistToDcp <input.netlist> <input.phys> <input.xdc> <output.dcp>`

Troubleshooting:

- If `bin/rapidwright` says not compiled, run `./gradlew compileJava`.
- If Python JVM fails, check `CLASSPATH`.
- If route status is suspicious, run `DesignTools.updatePinsIsRouted`.
- If interchange read fails, check same-root `.netlist/.phys/.xdc`.

### F.5 Contest Integration Agent Digest

Scope scanned:

- `RapidWrightMCP`
- `VivadoMCP`
- `dcp_optimizer.py`
- `FDAgents`
- docs and scripts

Findings:

- Main loops: original `dcp_optimizer.py` and newer `FDAgents`.
- Vivado is timing/routing authority.
- RW is for analysis/ECO.
- Best legal DCP must be promoted continuously.
- `clk_fpl26contest` is the only target clock that matters for score.
- Large benchmark runtime is dominated by Vivado.
- Existing recipes include fanout split, pblock, cell move, LUT merge, per-net unroute, phys_opt.
- VexRiscv pblock/force-replication recipe is a key known win.

Weak spots:

- Text parsing of Vivado reports.
- pexpect timeout state.
- broad `net.unroute()` in current cell placement wrapper.
- pblock heuristics are hardcoded.
- structure compare is not functional proof.

Future tasks:

- validation guardrail.
- Vivado recipe library.
- safer RapidWright ECO library.
- pblock topology generator.
- benchmark mining.
- experiment database.

### F.6 Examples/Tests Agent Digest

Scope scanned:

- `RapidWright/src/com/xilinx/rapidwright/examples`
- `RapidWright/test/src`
- runnable app entries

Most useful examples:

- `CustomRouting`: `Design.createAndPlaceCell`, `Net.connect`, `Router.routeDesign`, `DesignTools.findRoutingPath`, `net.getPIPs().addAll`, `net.lockRouting`.
- `CountRoutedNets`: `DesignTools.makePhysNetNamesConsistent`, `createMissingSitePinInsts`, `updatePinsIsRouted`, iterate `net.getPins`.
- `ReportTimingExample`: `TimingManager`, `TimingGraph.getMaxDelayPath`, `prettyPrintPathDelays`.
- `UpdateRoutingUsingSATRouter`: broad unroute then SAT route inside pblock.
- `RunSATRouterExample`: SAT router setup.
- `RelocateHierarchy`: hierarchy relocation.
- `StampPlacement`: copy placement pattern.
- `DecomposeLUT`: LUT/EDIF edit pattern.
- `ExampleNetlistCreation`: new logical netlist pattern.

Most useful tests:

- `TestDesignTools`: placement/unroute helper behavior.
- `TestNet`: `removePin(..., preserveOtherRoutes)` and route preservation behavior.
- `TestECOTools`: disconnect/connect/deferred removal behavior.
- `TestRWRoute`: RWRoute invocation patterns.
- `TestReportRouteStatus`: route status expectations.

Minimal agent instruction:

- When writing a new RW script, copy the normalization and route-status pattern from `CountRoutedNets`.
- When writing custom route PIPs, copy the path replacement pattern from `CustomRouting`, but prefer selective changes in real DCPs.
- When moving cells, inspect `TestDesignTools` and use `fullyUnplaceCell`.

---

## Appendix G: Task-Oriented Playbooks

### G.1 I Need To Move One Critical LUT/FF

Read:

- `Appendix A.4`
- `Appendix C.1`
- `Appendix C.2`
- `Appendix C.4`

Steps:

1. Get critical path pins from Vivado.
2. Convert pin names with `EDIFNetlist.getHierPortInstFromName`.
3. For each interior cell, call `getPhysicalCell(design)`.
4. Filter to placed LUT/FF, skip locked/special cells first.
5. Collect connected nets before moving.
6. Compute centroid from connected net pins.
7. Search compatible site/BEL with `ECOPlacementHelper`.
8. `fullyUnplaceCell`.
9. `placeCell` with preserved pin mappings.
10. `routeSite`.
11. Selectively unroute affected sink branches.
12. `updatePinsIsRouted` and `ReportRouteStatus`.
13. Vivado `route_design` and target WNS.

Stop if:

- Cell is not LUT/FF.
- Destination site has incompatible shared controls.
- Route status shows conflicts after local repair.
- Vivado worsens WNS significantly.

### G.2 I Need To Analyze A Critical Net's Detour

Read:

- `Appendix B.2`
- `Appendix B.3`
- `Appendix B.12`

Steps:

1. Get source/sink pin from Vivado timing.
2. Map to `EDIFHierPortInst`.
3. Get `SitePinInst` sink and `Net`.
4. Extract connection PIPs with `DesignTools.getConnectionPIPs`.
5. Walk PIPs with reversed-direction handling.
6. Sum tile-to-tile Manhattan hops.
7. Divide by source-sink tile Manhattan distance.
8. Record PIP count, intent-code histogram, SLR crossing, clock region crossing.
9. Rank high-detour, high-Vivado-net-delay sinks.

### G.3 I Need To Replicate A High Fanout Driver

Read:

- `Appendix C.6`
- `Appendix E.7`

Steps:

1. Use Vivado high fanout and timing to identify source net.
2. Confirm driver cell type is supported FF/LUT.
3. Cluster sinks by tile/clock region.
4. Try Vivado `phys_opt_design -force_replication_on_nets` first for baseline.
5. If custom placement is needed, use `FanOutOptimization.cutFanOutOfRoutedNet`.
6. Route/measure after each replication count.
7. Watch for over-replication cliffs.

### G.4 I Need To Repair Routing After RW ECO

Read:

- `Appendix B.4`
- `Appendix B.6`
- `Appendix B.11`

Steps:

1. Maintain a set of affected nets and sink pins during ECO.
2. Use `DesignTools.unroutePins` for changed sinks.
3. Normalize missing SPIs.
4. Call `PartialRouter.routeDesignPartialNonTimingDriven` for legality-first repair.
5. If failed due to blocking preserved nets, try timing-driven partial with `softPreserve=true`.
6. Run RW route status.
7. Write DCP.
8. Let Vivado route and measure.

### G.5 I Need To Add A New FDAgents Skill

Read:

- `Appendix E.1`
- `Appendix E.5`
- `Appendix E.8`

Skill design:

- Inputs should be small: target nets/cells/pblock params/directive.
- Skill should write its exact Tcl/RW operations to memory/log.
- Skill should return legality, WNS delta, route status, affected nets, and output DCP.
- Skill should support rollback by not mutating `current_best.dcp` until accepted.

Good first skills:

- `critical_sink_partial_reroute`
- `critical_path_bbox_pblock`
- `rw_selective_cell_move`
- `lut_pin_swap`
- `route_detour_report`
- `fanout_replication_sweep`

### G.6 I Need To Debug A Broken DCP

Read:

- `Appendix D.6`
- `Appendix B.11`
- `Appendix C.12`

Checklist:

1. Can RapidWright read it?
2. Can Vivado open it?
3. Does `DesignTools.createMissingSitePinInsts` throw?
4. Does `ReportRouteStatus` show unrouted pins or conflicts?
5. Does Vivado `report_route_status` agree?
6. Is the failing net static/clock/signal?
7. Are site pins stale?
8. Did a moved cell lose pin mapping?
9. Did a whole-net unroute accidentally affect unrelated sinks?
10. Did logical EDIF maps need reset?

### G.7 I Need To Keep Context Small For A Future Agent

Give the future agent:

- Top `TL;DR`.
- The one relevant appendix.
- The file path index for source files.
- The exact DCP/benchmark/timing report being worked on.
- The latest Vivado target-clock WNS and route status.

Do not give:

- Full RapidWright source.
- All appendices unless necessary.
- Long Vivado logs without the few critical lines.
