#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache-2.0

"""
RapidWright MCP Server
Provides AI assistant access to RapidWright FPGA design tools via the Model Context Protocol
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import Tool, TextContent, GetPromptResult, PromptMessage
import mcp.server.stdio


def _configure_checkout_import_path() -> str | None:
    """Make the checkout containing this server importable from any run cwd.

    FDAgents deliberately starts MCP servers in an artifact directory.  Python
    otherwise adds only ``RapidWrightMCP/`` (the script directory) to
    ``sys.path``, which made the lazy product-sum recovery import fail in both
    local and remote scratch-deployment layouts. Resolve the checkout from this
    file; never infer it from cwd or a VM-specific absolute path.
    """
    project_root = Path(__file__).resolve().parent.parent
    if not (project_root / "FDAgents").is_dir():
        return None
    root = str(project_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


_PROJECT_IMPORT_ROOT = _configure_checkout_import_path()


def _tool_param(key: str, default):
    """Read a `rapidwright_mcp:` tunable from the shared FDAgents config.yaml.

    Standalone config reader: honors FDAGENTS_CONFIG, falls back to the
    packaged default file, then to the hard-coded default.
    """
    try:
        import yaml  # type: ignore
        from pathlib import Path

        path_str = os.environ.get("FDAGENTS_CONFIG", "")
        path = (
            Path(path_str).expanduser()
            if path_str
            else Path(__file__).resolve().parent.parent / "FDAgents" / "config.yaml"
        )
        if path.exists():
            with path.open() as f:
                data = yaml.safe_load(f) or {}
            section = data.get("rapidwright_mcp") or {}
            value = section.get(key, default)
            return default if value is None else value
    except Exception:  # noqa: BLE001 — tool layer must work standalone
        pass
    return default

try:
    from . import rapidwright_tools as rw
except ImportError:
    import rapidwright_tools as rw

# Global variable for the Java/stdout log file
_java_log_file = None
_original_stderr_fd = None

# Logger will be configured in main() based on command-line arguments
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create MCP server instance
app = Server("rapidwright-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all available RapidWright tools."""
    return [
        Tool(
            name="initialize_rapidwright",
            description="Initialize the RapidWright environment. Must be called first before using other tools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jvm_max_memory": {
                        "type": "string",
                        "description": "Maximum JVM heap size (default: '4G')",
                        "default": "4G"
                    }
                }
            }
        ),
        Tool(
            name="get_supported_devices",
            description="Get list of all FPGA devices supported by RapidWright, including families and part numbers.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_device_info",
            description="Get detailed information about a specific FPGA device (dimensions, resources, family).",
            inputSchema={
                "type": "object",
                "properties": {
                    "device_name": {
                        "type": "string",
                        "description": "FPGA device name (e.g., 'xcvu3p', 'xcvu9p', 'xcku040')"
                    }
                },
                "required": ["device_name"]
            }
        ),
        Tool(
            name="read_checkpoint",
            description="Read a Vivado Design Checkpoint (.dcp) file for inspection and analysis.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path to the .dcp file"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="write_checkpoint",
            description="Write the current design to a Vivado Design Checkpoint (.dcp) file. If the design contains encrypted IP, an accompanying Tcl script will be generated that is required to load the DCP in Vivado.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path where the .dcp file will be saved"
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "If true, overwrite existing file; if false (default), error if file exists",
                        "default": False
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="insert_registered_product_modules",
            description=(
                "Internal registered-arithmetic backend: insert synthesized "
                "modules from a hash-bound current-DCP bundle."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bundle_path": {"type": "string", "minLength": 1},
                    "module_dcps": {
                        "type": "object",
                        "additionalProperties": {"type": "string", "minLength": 1},
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                },
                "required": ["bundle_path", "module_dcps", "output_dcp"],
            },
        ),
        Tool(
            name="digest_successor_contract_region",
            description=(
                "Read-only route-invariant digest of successor-certificate "
                "boundary nets and emitted hierarchy inventory."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "boundary_net_names": {
                        "type": "array", "minItems": 1, "maxItems": 4096,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "emitted_instance_names": {
                        "type": "array", "maxItems": 256,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                        "default": [],
                    },
                },
                "required": ["boundary_net_names"],
            },
        ),
        Tool(
            name="prove_control_pin_equivalence_groups",
            description=(
                "Internal registered-arithmetic proof: group physical control-pin "
                "nets only when their upstream transition relations are equivalent."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pin_names": {
                        "type": "array", "minItems": 1, "maxItems": 256,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "role": {"type": "string", "default": "CE"},
                },
                "required": ["pin_names"],
            },
        ),
        Tool(
            name="report_approx_timing",
            description="""Estimate critical-path timing using RapidWright's lightweight timing model.

            This is much faster than loading the design in Vivado and is intended for
            screening candidate optimization batches before paying for a full Vivado
            route/timing iteration.

            If clock_period_ns is provided, the tool reports an estimated WNS against
            that period. For contest use, pass the known period of clk_fpl26contest
            from the initial Vivado analysis.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "clock_period_ns": {
                        "type": "number",
                        "description": "Optional contest clock period in nanoseconds for estimated WNS computation"
                    },
                    "include_path": {
                        "type": "boolean",
                        "description": "Include critical-path text in the response (default: true)",
                        "default": True
                    },
                    "max_path_lines": {
                        "type": "integer",
                        "description": "Maximum number of path lines to include (default: 20)",
                        "default": 20
                    }
                }
            }
        ),
        Tool(
            name="get_design_info",
            description="Get statistics about the currently loaded design (cell/net counts, top cell types).",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="search_cells",
            description="Search for cells in the loaded design by name pattern or cell type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Name pattern to match (case-insensitive, optional)"
                    },
                    "cell_type": {
                        "type": "string",
                        "description": "Cell type to filter by (e.g., 'LUT6', 'FDRE', optional)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 100)",
                        "default": 100
                    }
                }
            }
        ),
        Tool(
            name="get_tile_info",
            description="Get information about a specific tile on the FPGA (type, location, sites).",
            inputSchema={
                "type": "object",
                "properties": {
                    "tile_name": {
                        "type": "string",
                        "description": "Tile name to query"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    }
                },
                "required": ["tile_name"]
            }
        ),
        Tool(
            name="search_sites",
            description="Search for sites on an FPGA device by site type (e.g., SLICEL, DSP48E2, RAMB36).",
            inputSchema={
                "type": "object",
                "properties": {
                    "site_type": {
                        "type": "string",
                        "description": "Site type to search for (e.g., 'SLICEL', 'DSP48E2', 'RAMB36')"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 50)",
                        "default": 50
                    }
                }
            }
        ),
        Tool(
            name="optimize_lut_input_cone",
            description="Optimize LUT input cones by combining chained small LUTs into a single larger LUT to reduce logic depth. This is useful for optimizing critical paths.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hierarchical_input_pins": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of hierarchical input pin names to optimize (e.g., ['module/submodule/inst/pin'])"
                    }
                },
                "required": ["hierarchical_input_pins"]
            }
        ),
        Tool(
            name="swap_lut_pins",
            description="""Directly swap/move input pins on a placed LUT cell using RapidWright LUTTools.

            This updates the LUT cell logical-to-physical pin mapping and moves
            the corresponding SitePinInsts. By default it unroutes affected nets
            so a following Vivado route_design can rebuild legal routing.

            Pin tokens may be logical pins (I0..I5), physical BEL pins (A1..A6),
            site pins (A1..H6), or destination alias FASTEST/FASTEST_FREE/AUTO.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "cell_name": {
                        "type": "string",
                        "description": "Hierarchical placed LUT cell name"
                    },
                    "pin_swaps": {
                        "type": "array",
                        "description": "List of [from_pin, to_pin] pairs or objects with from/to fields",
                        "items": {
                            "oneOf": [
                                {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 2,
                                    "maxItems": 2
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "from": {"type": "string"},
                                        "to": {"type": "string"},
                                        "old_pin": {"type": "string"},
                                        "new_pin": {"type": "string"}
                                    }
                                }
                            ]
                        }
                    },
                    "unroute_affected": {
                        "type": "boolean",
                        "description": "Unroute nets touched by the pin move before writing the checkpoint (default: true)",
                        "default": True
                    }
                },
                "required": ["cell_name", "pin_swaps"]
            }
        ),
        Tool(
            name="inspect_lut_pin_swap_candidates",
            description="""Read-only prefilter for LUT input pin swaps.

            Accepts Vivado hierarchical LUT input pin paths such as cell/I0 and
            reports the current physical LUT pin plus the fastest target physical
            pin that swap_lut_pins would use with FASTEST.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "cell_pins": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Vivado hierarchical LUT input pin paths, e.g. ['top/u_lut/I0']"
                    }
                },
                "required": ["cell_pins"]
            }
        ),
        Tool(
            name="analyze_route_connections",
            description=(
                "Read-only sink-specific route analysis. Accepts only inline critical-path "
                "pins or explicit physical-net/sink pairs; reports exact PIP/tree metrics "
                "and issues mutation certificates only when branch preservation is proven."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "maxItems": 64,
                        "items": {
                            "type": "array", "maxItems": 256,
                            "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                        },
                    },
                    "connections": {
                        "type": "array", "maxItems": 32,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "physical_net": {"type": "string", "minLength": 1, "maxLength": 1024},
                                "sink_pin": {"type": "string", "minLength": 1, "maxLength": 1024},
                            },
                            "required": ["physical_net", "sink_pin"],
                        },
                    },
                    "max_connections": {"type": "integer", "minimum": 1, "maximum": 32, "default": 16},
                },
                "anyOf": [
                    {"required": ["critical_paths_data"]},
                    {"required": ["connections"]},
                ],
            },
        ),
        Tool(
            name="operator_mining",
            description=(
                "Read-only structural hypothesis mining on the loaded exact DCP. "
                "Reports repeated critical-path primitive motifs, wide register "
                "transport families, DSP headroom, hard-macro columns, and clock "
                "footprint. Results are hypothesis_only and never authorize mutation."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {
                            "type": "array", "maxItems": 256,
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 1024
                            },
                        },
                        "description": (
                            "Optional inline cell or cell/pin sequences from Vivado "
                            "extract_critical_path_cells/pins; no file path is accepted."
                        ),
                    },
                    "min_family_size": {
                        "type": "integer", "minimum": 2, "maximum": 64, "default": 3
                    },
                    "max_families": {
                        "type": "integer", "minimum": 1, "maximum": 32, "default": 16
                    },
                    "max_motif_cells": {
                        "type": "integer", "minimum": 3, "maximum": 12, "default": 8
                    },
                },
            },
        ),
        Tool(
            name="recover_recurrence_contract",
            description=(
                "Read-only exact recovery for one recurrence hypothesis. Freshly "
                "rediscovers the candidate, derives state width from DSP C/P bit "
                "connectivity, verifies FF controls and side fanout, and emits a "
                "hash-bound extracted_unproved region contract."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {
                            "type": "array", "minItems": 1, "maxItems": 256,
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 1024
                            },
                        },
                    },
                    "candidate_id": {
                        "type": "string", "pattern": "^recurrence:[0-9a-f]{20}$"
                    },
                },
                "required": ["critical_paths_data", "candidate_id"],
            },
        ),
        Tool(
            name="recover_sequential_operator_contract",
            description=(
                "Read-only fresh recovery of one current-DCP sequential write "
                "family. Reports exact bit mapping, controls, data drivers and "
                "explicit proof gaps; it never authorizes mutation."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {
                            "type": "array", "minItems": 1, "maxItems": 256,
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 1024
                            },
                        },
                    },
                    "candidate_id": {
                        "type": "string", "pattern": "^sequential:[0-9a-f]{20}$"
                    },
                },
                "required": ["critical_paths_data", "candidate_id"],
            },
        ),
        Tool(
            name="recover_consumer_driven_operator_contract",
            description=(
                "Read-only consumer-driven semantic recovery for one current "
                "sequential family. Exact object identities remain private."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {
                            "type": "array", "minItems": 1, "maxItems": 64,
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 1024
                            },
                        },
                    },
                    "candidate_id": {
                        "type": "string", "pattern": "^sequential:[0-9a-f]{20}$"
                    },
                },
                "required": ["critical_paths_data", "candidate_id"],
            },
        ),
        Tool(
            name="recover_registered_dead_state_contract",
            description=(
                "Read-only full-design output-observable closure with exact "
                "mutation identities retained in a private manifest."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                },
                "required": ["source_dcp_sha256", "manifest_path"],
            },
        ),
        Tool(
            name="recover_recurrence_family_contract",
            description=(
                "Read-only exact full-design recurrence-family recovery. Scans the "
                "currently loaded DCP without timing names, proves compatible periodic "
                "boundaries and disjoint rewrite windows, and returns one hash-bound "
                "family contract with mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_boundaries": {
                        "type": "integer", "minimum": 2, "maximum": 64, "default": 32
                    },
                },
            },
        ),
        Tool(
            name="propose_recurrence_family_placement_candidates",
            description=(
                "Read-only bounded global placement proposals for every freshly "
                "recovered recurrence-family boundary. Cascade regions remain locality "
                "metadata; Vivado proves connected placement and exact clock-leaf reuse."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_srl_sites": {
                        "type": "integer", "minimum": 1, "maximum": 1024, "default": 32
                    },
                    "max_ce_clone_sites": {
                        "type": "integer", "minimum": 1, "maximum": 128, "default": 16
                    },
                },
            },
        ),
        Tool(
            name="propose_recurrence_placement_candidates",
            description=(
                "Read-only exact-seed recurrence placement proposal. Freshly "
                "rediscovers and recovers the candidate before ranking bounded global "
                "SLICE slots; Vivado remains placement and clock-leaf authority."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "minItems": 1, "maxItems": 16,
                        "items": {
                            "type": "array", "minItems": 1, "maxItems": 256,
                            "items": {
                                "type": "string", "minLength": 1, "maxLength": 1024
                            },
                        },
                    },
                    "candidate_id": {
                        "type": "string", "pattern": "^recurrence:[0-9a-f]{20}$"
                    },
                    "max_srl_sites": {
                        "type": "integer", "minimum": 1, "maximum": 1024, "default": 32
                    },
                    "max_ce_clone_sites": {
                        "type": "integer", "minimum": 1, "maximum": 128, "default": 16
                    },
                },
                "required": ["critical_paths_data", "candidate_id"],
            },
        ),
        Tool(
            name="recover_fixed_point_contract",
            description=(
                "Read-only autonomous fabric fixed-point recovery. Freshly "
                "rediscovers FF-bounded LUT/CARRY cones on the loaded exact DCP, "
                "recovers each constant coefficient symbolically with an exact "
                "bit-vector unsat check, and emits hash-bound "
                "fixed_point_facts_extracted_unproved contracts with "
                "mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 64, "default": 8
                    },
                },
            },
        ),
        Tool(
            name="recover_registered_product_sum_contract",
            description=(
                "Read-only exact recovery of registered product sums, including "
                "independently shifted and partitioned full-precision orders, "
                "from the loaded exact DCP. Returns hash-bound contracts with "
                "mutation_eligible=false and typed rejections."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 64,
                        "default": 16,
                    },
                    "preferred_output_cells": {
                        "type": "array", "maxItems": 16,
                        "items": {"type": "string", "minLength": 1,
                                  "maxLength": 1024},
                        "default": [],
                    },
                    "timeout_ms": {
                        "type": "integer", "minimum": 1, "maximum": 600000,
                        "default": 30000,
                    },
                },
            },
        ),
        Tool(
            name="propose_fixed_point_placement_candidates",
            description=(
                "Read-only exact-seed fixed-point placement proposal. Freshly "
                "re-recovers the hash-bound cone before ranking idle DSP48E2 "
                "sites by Manhattan distance from the cone centroid; Vivado "
                "remains placement and clock authority."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "region_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "max_sites": {
                        "type": "integer", "minimum": 1, "maximum": 256, "default": 64
                    },
                },
                "required": ["region_sha256"],
            },
        ),
        Tool(
            name="recover_two_operand_contract",
            description=(
                "Read-only autonomous fabric two-operand multiply recovery. "
                "Freshly rediscovers FF-bounded LUT/CARRY cones fed by exactly "
                "two registered data buses on the loaded exact DCP, recovers "
                "shift and signedness from concrete probes with an exact "
                "bit-vector unsat check, and emits hash-bound "
                "two_operand_facts_extracted_unproved contracts with "
                "mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 64, "default": 8
                    },
                },
            },
        ),
        Tool(
            name="propose_two_operand_placement_candidates",
            description=(
                "Read-only exact-seed two-operand placement proposal. Freshly "
                "re-recovers the hash-bound cone before ranking idle DSP48E2 "
                "sites by Manhattan distance from the cone centroid; Vivado "
                "remains placement and clock authority."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "region_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "max_sites": {
                        "type": "integer", "minimum": 1, "maximum": 256, "default": 64
                    },
                },
                "required": ["region_sha256"],
            },
        ),
        Tool(
            name="recover_register_absorb_contract",
            description=(
                "Read-only autonomous DSP input register absorption recovery. "
                "Freshly rediscovers fabric FDRE buses that solely drive a "
                "DSP48E2 A or B input whose AREG/BREG is disabled, checks "
                "uniform clock/CE/reset and GND-tied hardened controls, and "
                "emits hash-bound register_absorb_facts_extracted_unproved "
                "contracts with mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 64, "default": 8
                    },
                },
            },
        ),
        Tool(
            name="recover_pipeline_rebalance_contract",
            description=(
                "Read-only autonomous DSP48E2 multiplier pipeline-rebalance "
                "recovery. Freshly rediscovers cells eligible for the "
                "bidirectional AREG/BREG<->MREG retiming move (static "
                "MULTIPLY M-path mode, direct operands, no cascade traffic, "
                "one shared CE and reset net with GND-tied vacated "
                "controls), and emits hash-bound "
                "pipeline_rebalance_facts_extracted_unproved contracts with "
                "mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 64, "default": 8
                    },
                },
            },
        ),
        Tool(
            name="recover_memory_primitive_contracts",
            description=(
                "Read-only autonomous configured BRAM recovery. Enumerates "
                "RAMB18E2/RAMB36E2 cells on the loaded exact DCP and derives "
                "port widths, output registers, write modes, collision "
                "configuration, initialization identity, and exact pin/net "
                "boundaries. No semantic width, mode, latency, or cell-name "
                "parameters are accepted; emitted facts remain unproved and "
                "mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 256,
                        "default": 64,
                    },
                },
            },
        ),
        Tool(
            name="recover_memory_consumer_dependencies",
            description=(
                "Read-only autonomous BRAM consumer recovery. Traces every "
                "configured memory output through its canonical parent net to "
                "all leaf sinks and emits hash-bound producer/consumer edges. "
                "No hierarchy, cell-name, width, mode, or operator parameters "
                "are accepted; facts remain unproved and mutation_eligible=false."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_candidates": {
                        "type": "integer", "minimum": 1, "maximum": 256,
                        "default": 64,
                    },
                },
            },
        ),
        Tool(
            name="find_proven_equivalent_sources",
            description=(
                "Read-only, fail-closed exact-source equivalence proof for placed FDRE/FDSE/"
                "FDCE/FDPE (including _1 forms) and LUT1..LUT6 sources found from inline critical-path pins. "
                "No similarity, current-value, cell-name, or cone inference is used."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "critical_paths_data": {
                        "type": "array", "minItems": 1, "maxItems": 16,
                        "items": {
                            "type": "array", "minItems": 1, "maxItems": 256,
                            "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                        },
                    },
                    "max_candidates": {"type": "integer", "minimum": 1, "maximum": 4, "default": 4},
                    "min_distance": {"type": "integer", "minimum": 1, "maximum": 256, "default": 1},
                },
                "required": ["critical_paths_data"],
            },
        ),
        Tool(
            name="selective_unroute_sink_branches",
            description=(
                "Controlled mutation: remove only analysis-certified sink branches. "
                "Rejects clock/static/fixed/ambiguous or unproven shared-trunk targets, "
                "is single-use per exact checkpoint load, and never whole-net unroutes."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "connections": {
                        "type": "array", "minItems": 1, "maxItems": 4,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "physical_net": {"type": "string", "minLength": 1, "maxLength": 1024},
                                "sink_pin": {"type": "string", "minLength": 1, "maxLength": 1024},
                                "before_net_pip_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                "before_sink_pip_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                "certificate": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            },
                            "required": [
                                "physical_net", "sink_pin", "before_net_pip_digest",
                                "before_sink_pip_digest", "certificate",
                            ],
                        },
                    },
                },
                "required": ["connections"],
            },
        ),
        Tool(
            name="rewire_sink_to_proven_source",
            description=(
                "Internal skill-only mutation accepting only the complete "
                "certificate produced by the exact loaded session."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "certificate": {"type": "object"},
                },
                "required": ["certificate"],
            },
        ),
        Tool(
            name="relocate_structure_cluster",
            description=(
                "Internal skill-only exact-cluster relocation bound to a "
                "framework-owned structure digest."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "structure": {"type": "object"},
                    "expected_digest": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "tile_col_offset": {"type": "integer"},
                    "tile_row_offset": {"type": "integer"},
                    "max_boundary_nets": {
                        "type": "integer", "minimum": 1, "maximum": 256,
                        "default": 16,
                    },
                    "max_boundary_pins": {
                        "type": "integer", "minimum": 1, "maximum": 2048,
                        "default": 128,
                    },
                },
                "required": [
                    "structure", "expected_digest", "tile_col_offset",
                    "tile_row_offset",
                ],
            },
        ),
        Tool(
            name="optimize_fanout",
            description="Optimize high fanout nets by splitting them into multiple driven nets. This reduces fanout by replicating the source driver and can improve timing and routability.",
            inputSchema={
                "type": "object",
                "properties": {
                    "net_name": {
                        "type": "string",
                        "description": "Name of the high fanout net to optimize"
                    },
                    "split_factor": {
                        "type": "integer",
                        "description": "Number of copies to create (k) - net will be split into k parts"
                    }
                },
                "required": ["net_name", "split_factor"]
            }
        ),
        Tool(
            name="analyze_critical_path_spread",
            description="""Calculate Manhattan distances for cells on critical paths.
            
            Takes critical path data from Vivado (cell names from timing report) and uses RapidWright's
            device model to get accurate tile coordinates and calculate Manhattan distances between cells.
            
            Input can be provided either directly as critical_paths_data parameter OR via a JSON file
            specified in input_file parameter (more efficient for large datasets).
            
            Must be called AFTER read_checkpoint.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths_data": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "description": "List of paths, each path is a list of cell names from Vivado timing report"
                    },
                    "input_file": {
                        "type": "string",
                        "description": "Optional: path to JSON file containing critical_paths_data (more efficient)"
                    }
                }
            }
        ),
        Tool(
            name="analyze_fabric_for_pblock",
            description="""Analyze FPGA fabric to find the best contiguous region for a pblock (area constraint).
            
            Identifies regions that:
            1. Have enough resources (SLICEs, DSPs, BRAMs, URAMs) for target utilization
            2. Minimize crossing of delay-heavy columns (URAM, IO, etc.)
            3. Are as contiguous as possible
            
            Use this AFTER getting utilization from Vivado to determine where to place a pblock.
            Requires target resource counts (1.5x current usage from report_utilization_for_pblock).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_lut_count": {
                        "type": "integer",
                        "description": "Required LUTs (1.5x current usage)"
                    },
                    "target_ff_count": {
                        "type": "integer",
                        "description": "Required FFs (1.5x current usage)"
                    },
                    "target_dsp_count": {
                        "type": "integer",
                        "description": "Required DSPs (1.5x current usage, default: 0)"
                    },
                    "target_bram_count": {
                        "type": "integer",
                        "description": "Required BRAMs (1.5x current usage, default: 0)"
                    },
                    "target_uram_count": {
                        "type": "integer",
                        "description": "Required URAMs (1.5x current usage, default: 0)"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    }
                },
                "required": ["target_lut_count", "target_ff_count"]
            }
        ),
        Tool(
            name="analyze_clock_region_pblock_grid",
            description="""Read the loaded part's clock-region grid through RapidWright Device APIs.

            Returns per-clock-region LUT/FF-derived slice capacity, DSP/BRAM/URAM
            capacity, placement occupancy, and separate critical/remainder demand.
            This is read-only and intended for clock-aligned pblock siblings.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_cell_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 256,
                    }
                }
            }
        ),
        Tool(
            name="convert_fabric_region_to_pblock",
            description="""Convert fabric region coordinates to Vivado pblock range strings.
            
            Takes tile column/row coordinates and generates a complete pblock string with all
            site types (SLICE, DSP48E2, RAMB18, RAMB36, URAM288) in proper Vivado format.
            
            Example output: "SLICE_X55Y0:SLICE_X109Y179 DSP48E2_X8Y0:DSP48E2_X13Y71 RAMB18_X4Y0:RAMB18_X7Y71 RAMB36_X4Y0:RAMB36_X7Y35 URAM288_X1Y0:URAM288_X2Y47"
            
            IMPORTANT: Always use detailed site-specific ranges (default) for optimization.
            DO NOT use clock regions (use_clock_regions=True) as they are too coarse.
            
            Must be called AFTER read_checkpoint or with device_name specified.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "col_min": {
                        "type": "integer",
                        "description": "Minimum column coordinate"
                    },
                    "col_max": {
                        "type": "integer",
                        "description": "Maximum column coordinate"
                    },
                    "row_min": {
                        "type": "integer",
                        "description": "Minimum row coordinate"
                    },
                    "row_max": {
                        "type": "integer",
                        "description": "Maximum row coordinate"
                    },
                    "device_name": {
                        "type": "string",
                        "description": "Device name (optional, uses loaded design's device if omitted)"
                    },
                    "use_clock_regions": {
                        "type": "boolean",
                        "description": "If true, use coarse CLOCKREGION ranges (NOT RECOMMENDED for optimization); if false (DEFAULT), generate detailed multi-site-type ranges (SLICE_X, DSP48E2_X, etc.) - REQUIRED for pblock optimization"
                    }
                },
                "required": ["col_min", "col_max", "row_min", "row_max"]
            }
        ),
        Tool(
            name="compare_design_structure",
            description="""Compare structural properties of two design checkpoints for equivalence validation.
            
            This is Phase 1 of design equivalence checking. Performs sanity checks to catch obvious errors:
            - Top-level module name must match
            - I/O port names, directions, and widths must match
            - Device must match
            - Cell counts are reported for context but do not affect PASS/FAIL
            
            Returns PASS/FAIL status with detailed comparison report.
            This should be run BEFORE functional simulation to quickly catch structural errors.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "golden_dcp": {
                        "type": "string",
                        "description": "Path to the golden (reference) DCP file"
                    },
                    "revised_dcp": {
                        "type": "string",
                        "description": "Path to the revised (optimized) DCP file to validate"
                    }
                },
                "required": ["golden_dcp", "revised_dcp"]
            }
        ),
        Tool(
            name="analyze_net_detour",
            description="""Cell-centric analysis of routing detours on critical paths.
            
            For each interior cell on a critical path, computes the detour ratio
            of the incoming net (feeding the cell) and the outgoing net (driven
            by it). A high ratio on either side indicates the cell may benefit
            from re-placement closer to its connections.
            
            Input is a list of pin-path lists as produced by Vivado's
            extract_critical_path_pins:
                ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]
            Consecutive pins from the same cell are an intra-cell pair.
            Consecutive pins from different cells span a net segment.
            
            Input can be provided directly as critical_paths_data or via a JSON file.
            Must be called AFTER read_checkpoint.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths_data": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "description": "List of paths from extract_critical_path_pins. Each path is a list of pin paths like ['src_ff/Q', 'lut/I2', 'lut/O', 'dst_ff/D']."
                    },
                    "detour_threshold": {
                        "type": "number",
                        "description": "Flag cells with max detour ratio above this value (default: 2.0)",
                        "default": 2.0
                    },
                    "input_file": {
                        "type": "string",
                        "description": "Optional: path to JSON file containing critical_paths_data"
                    }
                }
            }
        ),
        Tool(
            name="optimize_cell_placement",
            description="""Re-place cells at the centroid of their connections to reduce routing detours.
            
            For each candidate cell:
            1. Computes the centroid of all connected pin tile locations
            2. Finds the nearest available SLICE site near the centroid
            3. Unplaces the cell and unroutes affected nets
            4. Places the cell at the new site
            
            After running, write the checkpoint and load it in Vivado to re-route
            the unrouted nets with route_design.
            
            Must be called AFTER read_checkpoint.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "cell_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of cell names to re-place"
                    },
                    "max_candidates": {
                        "type": "integer",
                        "description": "Maximum number of cells to process (default: 10)",
                        "default": 10
                    }
                },
                "required": ["cell_names"]
            }
        ),
        Tool(
            name="optimize_critical_path_detours",
            description="""High-level detour-repair recipe for post-pblock or post-route cleanup.

            This tool analyzes critical-path pin paths for routing detours, selects
            the strongest cell candidates, re-places them near the centroid of
            their connections, and can optionally report approximate timing before
            and after the move batch.

            Best used after Vivado extract_critical_path_pins and before a new
            Vivado route_design pass.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "critical_paths_data": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "description": "Critical path pin-path lists from extract_critical_path_pins"
                    },
                    "input_file": {
                        "type": "string",
                        "description": "Optional JSON file containing critical_paths_data"
                    },
                    "detour_threshold": {
                        "type": "number",
                        "description": "Minimum detour ratio required to consider a cell (default: 2.0)",
                        "default": 2.0
                    },
                    "max_candidates": {
                        "type": "integer",
                        "description": "Maximum number of unique cells to move in this batch (default: 5)",
                        "default": 5
                    },
                    "clock_period_ns": {
                        "type": "number",
                        "description": "Optional contest clock period to report approximate WNS/Fmax before and after the move batch"
                    }
                }
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Execute a tool and return the result."""
    try:
        logger.info(f"Tool called: {name} with arguments: {arguments}")
        
        # Route to appropriate handler
        if name == "initialize_rapidwright":
            result = rw.initialize_rapidwright(
                jvm_max_memory=arguments.get("jvm_max_memory", "4G")
            )
        
        elif name == "get_supported_devices":
            result = rw.get_supported_devices()
        
        elif name == "get_device_info":
            result = rw.get_device_info(arguments["device_name"])
        
        elif name == "read_checkpoint":
            result = rw.read_checkpoint(arguments["dcp_path"])
        
        elif name == "write_checkpoint":
            result = rw.write_checkpoint(
                dcp_path=arguments["dcp_path"],
                overwrite=arguments.get("overwrite", False)
            )

        elif name == "report_approx_timing":
            result = rw.report_approx_timing(
                clock_period_ns=arguments.get("clock_period_ns"),
                include_path=arguments.get("include_path", True),
                max_path_lines=arguments.get("max_path_lines", 20)
            )

        elif name == "get_design_info":
            result = rw.get_design_info()
        
        elif name == "search_cells":
            result = rw.search_cells(
                pattern=arguments.get("pattern"),
                cell_type=arguments.get("cell_type"),
                limit=arguments.get("limit", 100)
            )
        
        elif name == "get_tile_info":
            result = rw.get_tile_info(
                tile_name=arguments["tile_name"],
                device_name=arguments.get("device_name")
            )
        
        elif name == "search_sites":
            result = rw.search_sites(
                site_type=arguments.get("site_type"),
                device_name=arguments.get("device_name"),
                limit=arguments.get("limit", 50)
            )
        
        elif name == "optimize_lut_input_cone":
            result = rw.optimize_lut_input_cone(
                hierarchical_input_pins=arguments["hierarchical_input_pins"]
            )

        elif name == "swap_lut_pins":
            result = rw.swap_lut_pins(
                cell_name=arguments["cell_name"],
                pin_swaps=arguments["pin_swaps"],
                unroute_affected=arguments.get("unroute_affected", True)
            )

        elif name == "inspect_lut_pin_swap_candidates":
            result = rw.inspect_lut_pin_swap_candidates(
                cell_pins=arguments["cell_pins"]
            )

        elif name == "analyze_route_connections":
            result = rw.analyze_route_connections(
                connections=arguments.get("connections"),
                critical_paths_data=arguments.get("critical_paths_data"),
                max_connections=arguments.get("max_connections", 16),
            )

        elif name == "operator_mining":
            result = rw.operator_mining(
                critical_paths_data=arguments.get("critical_paths_data"),
                min_family_size=arguments.get("min_family_size", 3),
                max_families=arguments.get("max_families", 16),
                max_motif_cells=arguments.get("max_motif_cells", 8),
            )

        elif name == "recover_recurrence_contract":
            result = rw.recover_recurrence_contract(
                critical_paths_data=arguments["critical_paths_data"],
                candidate_id=arguments["candidate_id"],
            )

        elif name == "recover_sequential_operator_contract":
            result = rw.recover_sequential_operator_contract(
                critical_paths_data=arguments["critical_paths_data"],
                candidate_id=arguments["candidate_id"],
            )

        elif name == "recover_consumer_driven_operator_contract":
            result = rw.recover_consumer_driven_operator_contract(
                critical_paths_data=arguments["critical_paths_data"],
                candidate_id=arguments["candidate_id"],
            )

        elif name == "recover_registered_dead_state_contract":
            result = rw.recover_registered_dead_state_contract(
                source_dcp_sha256=arguments["source_dcp_sha256"],
                manifest_path=arguments["manifest_path"],
            )

        # Backend-only replica recovery is intentionally absent from list_tools:
        # Agent packets receive anonymous summaries rather than object names.
        elif name == "recover_sequential_operator_replicas":
            result = rw.recover_sequential_operator_replicas(
                critical_paths_data=arguments["critical_paths_data"],
                candidate_id=arguments["candidate_id"],
                max_replicas=arguments.get("max_replicas", 64),
            )

        elif name == "insert_registered_product_modules":
            result = rw.insert_registered_product_modules(
                bundle_path=arguments["bundle_path"],
                module_dcps=arguments["module_dcps"],
                output_dcp=arguments["output_dcp"],
            )

        elif name == "digest_successor_contract_region":
            result = rw.digest_successor_contract_region(
                boundary_net_names=arguments["boundary_net_names"],
                emitted_instance_names=arguments.get("emitted_instance_names", []),
            )

        elif name == "prove_control_pin_equivalence_groups":
            result = rw.prove_control_pin_equivalence_groups(
                pin_names=arguments["pin_names"],
                role=arguments.get("role", "CE"),
            )

        elif name == "recover_recurrence_family_contract":
            result = rw.recover_recurrence_family_contract(
                max_boundaries=arguments.get("max_boundaries", 32),
            )

        elif name == "propose_recurrence_family_placement_candidates":
            result = rw.propose_recurrence_family_placement_candidates(
                max_srl_sites=arguments.get("max_srl_sites", 32),
                max_ce_clone_sites=arguments.get("max_ce_clone_sites", 16),
            )

        elif name == "propose_recurrence_placement_candidates":
            result = rw.propose_recurrence_placement_candidates(
                critical_paths_data=arguments["critical_paths_data"],
                candidate_id=arguments["candidate_id"],
                max_srl_sites=arguments.get("max_srl_sites", 32),
                max_ce_clone_sites=arguments.get("max_ce_clone_sites", 16),
            )

        elif name == "recover_fixed_point_contract":
            result = rw.recover_fixed_point_contract(
                max_candidates=arguments.get("max_candidates", 8),
            )

        elif name == "recover_registered_product_sum_contract":
            result = rw.recover_registered_product_sum_contract(
                max_candidates=arguments.get("max_candidates", 16),
                preferred_output_cells=arguments.get(
                    "preferred_output_cells", []
                ),
                timeout_ms=arguments.get("timeout_ms", 30000),
            )

        elif name == "propose_fixed_point_placement_candidates":
            result = rw.propose_fixed_point_placement_candidates(
                region_sha256=arguments["region_sha256"],
                max_sites=arguments.get("max_sites", 64),
            )

        elif name == "recover_two_operand_contract":
            result = rw.recover_two_operand_contract(
                max_candidates=arguments.get("max_candidates", 8),
            )

        elif name == "propose_two_operand_placement_candidates":
            result = rw.propose_two_operand_placement_candidates(
                region_sha256=arguments["region_sha256"],
                max_sites=arguments.get("max_sites", 64),
            )

        elif name == "recover_register_absorb_contract":
            result = rw.recover_register_absorb_contract(
                max_candidates=arguments.get("max_candidates", 8),
            )

        elif name == "recover_pipeline_rebalance_contract":
            result = rw.recover_pipeline_rebalance_contract(
                max_candidates=arguments.get("max_candidates", 8),
            )

        elif name == "recover_memory_primitive_contracts":
            result = rw.recover_memory_primitive_contracts(
                max_candidates=arguments.get("max_candidates", 64),
            )

        elif name == "recover_memory_consumer_dependencies":
            result = rw.recover_memory_consumer_dependencies(
                max_candidates=arguments.get("max_candidates", 64),
            )

        elif name == "find_proven_equivalent_sources":
            result = rw.find_proven_equivalent_sources(
                critical_paths_data=arguments["critical_paths_data"],
                max_candidates=arguments.get("max_candidates", 4),
                min_distance=arguments.get("min_distance", 1),
            )

        elif name == "selective_unroute_sink_branches":
            result = rw.selective_unroute_sink_branches(
                connections=arguments["connections"]
            )

        # Internal skill-only mutation. It accepts only a complete certificate
        # issued by the exact loaded session; the Agent never exposes it as an
        # LLM-selectable probe.
        elif name == "rewire_sink_to_proven_source":
            result = rw.rewire_sink_to_proven_source(
                certificate=arguments["certificate"]
            )

        # Internal skill-only mutation. Catalog registration keeps MCP schema
        # validation aligned; the Agent still does not expose it as an
        # LLM-selectable probe or accept public object paths.
        elif name == "relocate_structure_cluster":
            result = rw.relocate_structure_cluster(
                structure=arguments["structure"],
                expected_digest=arguments["expected_digest"],
                tile_col_offset=arguments["tile_col_offset"],
                tile_row_offset=arguments["tile_row_offset"],
                max_boundary_nets=arguments.get("max_boundary_nets", 16),
                max_boundary_pins=arguments.get("max_boundary_pins", 128),
            )
        
        elif name == "optimize_fanout":
            result = rw.optimize_fanout(
                net_name=arguments["net_name"],
                split_factor=arguments["split_factor"]
            )
        
        elif name == "analyze_critical_path_spread":
            result = rw.analyze_critical_path_spread(
                critical_paths_data=arguments.get("critical_paths_data"),
                input_file=arguments.get("input_file")
            )
        
        elif name == "analyze_fabric_for_pblock":
            result = rw.analyze_fabric_for_pblock(
                target_lut_count=arguments["target_lut_count"],
                target_ff_count=arguments["target_ff_count"],
                target_dsp_count=arguments.get("target_dsp_count", 0),
                target_bram_count=arguments.get("target_bram_count", 0),
                target_uram_count=arguments.get("target_uram_count", 0),
                device_name=arguments.get("device_name")
            )

        elif name == "analyze_clock_region_pblock_grid":
            result = rw.analyze_clock_region_pblock_grid(
                critical_cell_names=arguments.get("critical_cell_names", [])
            )
        
        elif name == "convert_fabric_region_to_pblock":
            result = rw.convert_fabric_region_to_pblock_ranges(
                col_min=arguments["col_min"],
                col_max=arguments["col_max"],
                row_min=arguments["row_min"],
                row_max=arguments["row_max"],
                device_name=arguments.get("device_name"),
                use_clock_regions=arguments.get("use_clock_regions", False)  # Default to detailed site ranges
            )
        
        elif name == "compare_design_structure":
            result = rw.compare_design_structure(
                golden_dcp=arguments["golden_dcp"],
                revised_dcp=arguments["revised_dcp"]
            )
        
        elif name == "analyze_net_detour":
            result = rw.analyze_net_detour(
                critical_paths_data=arguments.get("critical_paths_data"),
                detour_threshold=arguments.get("detour_threshold", float(_tool_param("detour_threshold", 2.0))),
                input_file=arguments.get("input_file")
            )
        
        elif name == "optimize_cell_placement":
            result = rw.optimize_cell_placement(
                cell_names=arguments["cell_names"],
                max_candidates=arguments.get("max_candidates", 10)
            )

        elif name == "optimize_critical_path_detours":
            result = rw.optimize_critical_path_detours(
                critical_paths_data=arguments.get("critical_paths_data"),
                input_file=arguments.get("input_file"),
                detour_threshold=arguments.get("detour_threshold", float(_tool_param("detour_threshold", 2.0))),
                max_candidates=arguments.get("max_candidates", 5),
                clock_period_ns=arguments.get("clock_period_ns"),
            )
        
        else:
            result = {"error": f"Unknown tool: {name}"}
        
        # Return formatted result
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}", exc_info=True)
        return [TextContent(
            type="text", 
            text=json.dumps({"error": str(e), "tool": name}, indent=2)
        )]


@app.list_prompts()
async def list_prompts() -> list[mcp.types.Prompt]:
    """List available prompt templates."""
    return [
        mcp.types.Prompt(
            name="getting_started",
            description="Get started with RapidWright",
            arguments=[]
        ),
        mcp.types.Prompt(
            name="analyze_design",
            description="Analyze a design checkpoint",
            arguments=[
                mcp.types.PromptArgument(
                    name="dcp_path",
                    description="Path to the .dcp file",
                    required=True
                )
            ]
        )
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    """Get a specific prompt template."""
    if name == "getting_started":
        return GetPromptResult(
            description="Getting started with RapidWright",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text="""I want to use RapidWright. Please:
1. Initialize RapidWright
2. Show me what devices are supported
3. Explain what I can do with this server"""
                    )
                )
            ]
        )
    
    elif name == "analyze_design":
        dcp_path = arguments.get("dcp_path") if arguments else "/path/to/design.dcp"
        return GetPromptResult(
            description="Analyze a design checkpoint",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Analyze the design at: {dcp_path}

Tell me:
1. What device it targets
2. Cell and net counts
3. Top cell types used
4. Any interesting statistics"""
                    )
                )
            ]
        )
    
    raise ValueError(f"Unknown prompt: {name}")


async def main():
    """Main entry point for the server."""
    global _java_log_file, _original_stderr_fd
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="RapidWright MCP Server")
    parser.add_argument(
        "--java-log",
        type=str,
        help="Path to log file for Java/JVM output (stdout/stderr)"
    )
    parser.add_argument(
        "--mcp-log",
        type=str,
        help="Path to log file for MCP server logs"
    )
    args = parser.parse_args()
    
    # Configure logging based on whether mcp-log is specified
    if args.mcp_log:
        # Log MCP server messages to a separate file
        mcp_log_file = open(args.mcp_log, 'w', buffering=1)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(mcp_log_file)
            ]
        )
    else:
        # No mcp-log specified - log to stderr (debug mode or standalone usage)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stderr)
            ]
        )
    
    # If java-log is specified, redirect stdout and stderr at the file descriptor level
    # This must be done BEFORE importing rapidwright to capture Java output
    # This ensures JPype/JVM output is captured without breaking MCP protocol
    if args.java_log:
        try:
            _java_log_file = open(args.java_log, 'w', buffering=1)  # Line buffered
            
            # Save original stdout and stderr file descriptors
            original_stdout_fd = os.dup(1)  # dup stdout (fd 1)
            _original_stderr_fd = os.dup(2)  # dup stderr (fd 2)
            
            # Redirect both stdout (fd 1) and stderr (fd 2) to the log file
            # This captures all Java output (progress messages, errors, etc.)
            os.dup2(_java_log_file.fileno(), 1)
            os.dup2(_java_log_file.fileno(), 2)
            
            # Restore Python's stdout and stderr to the saved file descriptors
            # This allows Python logging and MCP protocol to work normally
            sys.stdout = os.fdopen(original_stdout_fd, 'w', buffering=1)
            sys.stderr = os.fdopen(_original_stderr_fd, 'w', buffering=1)
            
            logger.info(f"Java/JVM output (stdout/stderr fds) will be redirected to: {args.java_log}")
        except Exception as e:
            logger.error(f"Failed to redirect stdout/stderr to log file: {e}")
    
    logger.info("Starting RapidWright MCP Server...")
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("Server running on stdio transport")
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )
    
    # Close the log file on exit
    if _java_log_file:
        _java_log_file.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
