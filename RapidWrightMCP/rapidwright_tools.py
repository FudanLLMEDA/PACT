# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache-2.0

"""
RapidWright Tools - Wrapper functions for RapidWright operations
Uses the rapidwright pip package for JPype integration, with RAPIDWRIGHT_PATH
and CLASSPATH pointing to the local RapidWright git submodule for Java classes.
"""
import logging
import os
import re
import shutil
import sys
import time
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Global state
_initialized = False
_current_design = None


_LUT_PIN_TOKEN_RE = re.compile(r"^(?:I[0-5]|A[1-6]|[A-H][1-6])$")
_FASTEST_PIN_ALIASES = {"FASTEST", "FASTEST_FREE", "AUTO"}


def _resolve_vivado_candidate(value: os.PathLike | str | None) -> Optional[str]:
    if not value:
        return None
    path = os.path.abspath(os.path.expanduser(str(value)))
    if os.path.isdir(path):
        path = os.path.join(path, "vivado")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def _prepend_env_path(path: str) -> None:
    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if path in parts:
        return
    os.environ["PATH"] = path if not current else f"{path}{os.pathsep}{current}"


def _default_vivado_candidates() -> list[str]:
    candidates: list[str] = []
    for key in ("VIVADO_EXEC", "VIVADO_BIN"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    xilinx_vivado = os.environ.get("XILINX_VIVADO")
    if xilinx_vivado:
        candidates.append(os.path.join(xilinx_vivado, "bin", "vivado"))
    discovered = shutil.which("vivado")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            "/opt/Xilinx/2025.1/Vivado/bin/vivado",
            "/tools/Xilinx/2025.1/Vivado/bin/vivado",
        ]
    )
    return candidates


def _configure_vivado_lookup(candidates: Optional[list[os.PathLike | str]] = None) -> Dict[str, str]:
    """Set Vivado lookup env vars when a usable executable can be discovered."""
    for candidate in candidates or _default_vivado_candidates():
        vivado_exec = _resolve_vivado_candidate(candidate)
        if vivado_exec is None:
            continue
        vivado_bin = os.path.dirname(vivado_exec)
        os.environ["VIVADO_EXEC"] = vivado_exec
        os.environ["VIVADO_BIN"] = vivado_exec
        _prepend_env_path(vivado_bin)
        return {
            "VIVADO_EXEC": vivado_exec,
            "VIVADO_BIN_DIR": vivado_bin,
        }
    return {}


def _is_built_rapidwright(root: Optional[os.PathLike | str]) -> bool:
    """True when `root` looks like a *built* RapidWright checkout (has jars)."""
    if not root:
        return False
    jars_dir = os.path.join(str(root), "jars")
    try:
        return os.path.isdir(jars_dir) and any(
            name.endswith(".jar") for name in os.listdir(jars_dir)
        )
    except OSError:
        return False


def _configure_repo_local_rapidwright(project_root: Optional[os.PathLike | str] = None) -> Dict[str, str]:
    """Configure Python/Java lookup for RapidWright.

    Prefers a *built* repo-local RapidWright checkout, then a *built* checkout
    named by an existing ``RAPIDWRIGHT_PATH``, and otherwise falls back to the
    jars bundled with the ``rapidwright`` pip package.

    Critically, it never pins ``RAPIDWRIGHT_PATH``/``CLASSPATH`` to a
    non-existent or unbuilt checkout. Doing so shadows the bundled jars and makes
    ``from com.xilinx...`` fail with "Failed to import 'com.xilinx'", which
    silently kills every RapidWright-backed skill. This matches the workspace
    convention that RapidWright is referenced externally and never vendored into
    the repo (so the repo-local ``RapidWright/`` directory is usually absent).
    """
    if project_root is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_local = os.path.join(str(project_root), "RapidWright")
    existing = os.environ.get("RAPIDWRIGHT_PATH")

    if _is_built_rapidwright(repo_local):
        chosen = repo_local
    elif _is_built_rapidwright(existing):
        chosen = existing
    else:
        chosen = None

    vivado_env = _configure_vivado_lookup()

    if chosen is None:
        # Use the pip package's bundled jars. Make sure no stale
        # RAPIDWRIGHT_PATH / CLASSPATH is left behind that would shadow them.
        if existing and not _is_built_rapidwright(existing):
            os.environ.pop("RAPIDWRIGHT_PATH", None)
        os.environ.pop("CLASSPATH", None)
        env = {"RAPIDWRIGHT_PATH": "(bundled rapidwright package jars)"}
        env.update(vivado_env)
        return env

    python_src = os.path.join(chosen, "python", "src")
    os.environ["RAPIDWRIGHT_PATH"] = str(chosen)
    os.environ["CLASSPATH"] = f"{chosen}/bin:{chosen}/jars/*"
    if os.path.isdir(python_src) and python_src not in sys.path:
        sys.path.insert(0, python_src)

    env = {
        "RAPIDWRIGHT_PATH": str(chosen),
        "CLASSPATH": os.environ["CLASSPATH"],
        "PYTHONPATH_ENTRY": python_src,
    }
    env.update(vivado_env)
    return env


def _normalize_lut_pin_swaps(pin_swaps: list) -> list[tuple[str, str]]:
    """
    Normalize user-facing LUT pin swap specs.

    Accepted forms:
      [["I0", "A6"], ["I1", "A5"]]
      [{"from": "I0", "to": "A6"}, {"old_pin": "I1", "new_pin": "FASTEST"}]

    Pin tokens can be logical LUT pins (I0..I5), physical BEL pins (A1..A6),
    or site pins (A1..H6, validated against the target cell later).
    """
    if not isinstance(pin_swaps, list):
        raise ValueError("pin_swaps must be a list")

    normalized: list[tuple[str, str]] = []
    seen_sources: set[str] = set()

    for idx, spec in enumerate(pin_swaps):
        if isinstance(spec, dict):
            old_pin = (
                spec.get("from")
                or spec.get("old_pin")
                or spec.get("source")
                or spec.get("src")
            )
            new_pin = (
                spec.get("to")
                or spec.get("new_pin")
                or spec.get("target")
                or spec.get("dst")
            )
        elif isinstance(spec, (list, tuple)) and len(spec) == 2:
            old_pin, new_pin = spec
        else:
            raise ValueError(
                f"pin_swaps[{idx}] must be a two-item list/tuple or dict"
            )

        if old_pin is None or new_pin is None:
            raise ValueError(f"pin_swaps[{idx}] is missing from/to pins")

        old_token = str(old_pin).strip().upper()
        new_token = str(new_pin).strip().upper()

        if not _LUT_PIN_TOKEN_RE.match(old_token):
            raise ValueError(f"invalid source LUT pin token: {old_pin!r}")
        if (
            new_token not in _FASTEST_PIN_ALIASES
            and not _LUT_PIN_TOKEN_RE.match(new_token)
        ):
            raise ValueError(f"invalid target LUT pin token: {new_pin!r}")
        if old_token in seen_sources:
            raise ValueError(f"duplicate source LUT pin token: {old_token}")

        seen_sources.add(old_token)
        normalized.append((old_token, new_token))

    return normalized


def _java_string_map_to_dict(java_map) -> dict[str, str]:
    result: dict[str, str] = {}
    if java_map is None:
        return result
    for entry in java_map.entrySet():
        result[str(entry.getKey())] = str(entry.getValue())
    return result


def _site_pin_name_for_physical(cell, physical_pin: str) -> str:
    bel_name = str(cell.getBELName())
    if not bel_name:
        raise ValueError("cell has no BEL name")
    return bel_name[0] + physical_pin[1]


def _physical_pin_exists(cell, physical_pin: str) -> bool:
    try:
        return cell.getBEL().getPin(physical_pin) is not None
    except Exception:
        return False


def _preferred_lut_physical_pins(cell) -> list[str]:
    # RapidWright LUTTools searches high-numbered pins first for speed.
    return [
        pin
        for pin in ("A6", "A5", "A4", "A3", "A2", "A1")
        if _physical_pin_exists(cell, pin)
    ]


def _resolve_lut_physical_pin(cell, pin_token: str) -> str:
    pin_token = str(pin_token).strip().upper()
    if pin_token.startswith("I"):
        physical = cell.getPhysicalPinMapping(pin_token)
        if physical is None:
            raise ValueError(
                f"logical pin {pin_token} is not mapped on cell {cell.getName()}"
            )
        physical = str(physical)
    elif pin_token.startswith("A"):
        physical = pin_token
    else:
        site_pin = pin_token
        expected_letter = str(cell.getBELName())[0]
        if site_pin[0] != expected_letter:
            raise ValueError(
                f"site pin {site_pin} does not belong to BEL {cell.getBELName()}"
            )
        physical = "A" + site_pin[1]

    if not _physical_pin_exists(cell, physical):
        raise ValueError(
            f"physical pin {physical} does not exist on BEL {cell.getBELName()}"
        )
    return physical


def _resolve_lut_target_physical_pin(cell, old_physical: str, pin_token: str, lut_tools) -> str:
    pin_token = str(pin_token).strip().upper()
    if pin_token in _FASTEST_PIN_ALIASES:
        fastest_free = lut_tools.getUnmappedPhysicalLUTInputPin(cell)
        if fastest_free is not None:
            return str(fastest_free)
        for candidate in _preferred_lut_physical_pins(cell):
            if candidate != old_physical:
                return candidate
        raise ValueError(f"no alternate physical LUT pin available on {cell.getName()}")
    return _resolve_lut_physical_pin(cell, pin_token)


def _split_lut_cell_pin_path(pin_path: str) -> tuple[str, str]:
    pin_text = str(pin_path).strip()
    if "/" not in pin_text:
        raise ValueError("pin path must be hierarchical cell path plus logical pin, e.g. cell/I0")
    cell_name, logical_pin = pin_text.rsplit("/", 1)
    if not cell_name or not logical_pin:
        raise ValueError("pin path must include both cell name and logical pin")
    logical_pin = logical_pin.upper()
    if not re.match(r"^I[0-5]$", logical_pin):
        raise ValueError(f"logical pin must be I0..I5, got {logical_pin!r}")
    return cell_name, logical_pin


def _empty_lut_pin_swap_candidate(
    pin_path: str,
    cell_name: Optional[str] = None,
    logical_pin: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "pin": pin_path,
        "cell": cell_name,
        "logical_pin": logical_pin,
        "cell_type": None,
        "site": None,
        "bel": None,
        "current_physical_pin": None,
        "current_site_pin": None,
        "fastest_target_physical_pin": None,
        "fastest_target_site_pin": None,
        "is_actionable": False,
        "reason": None,
    }


def _inspect_lut_pin_swap_candidates_for_design(
    cell_pins: list[str],
    design,
    lut_tools,
) -> Dict[str, Any]:
    candidates = []
    invalid = []

    if not isinstance(cell_pins, list):
        return {
            "status": "error",
            "error": "cell_pins must be a list of Vivado pin paths",
            "candidates": [],
            "invalid": [],
        }

    for pin_path in cell_pins:
        pin_text = str(pin_path)
        try:
            cell_name, logical_pin = _split_lut_cell_pin_path(pin_text)
        except ValueError as e:
            invalid.append({"pin": pin_text, "reason": str(e)})
            continue

        cell = design.getCell(cell_name)
        if cell is None:
            invalid.append({
                "pin": pin_text,
                "cell": cell_name,
                "logical_pin": logical_pin,
                "reason": f"Cell '{cell_name}' not found in design",
            })
            continue

        candidate = _empty_lut_pin_swap_candidate(pin_text, cell_name, logical_pin)
        candidate["cell_type"] = str(cell.getType() or "")
        candidate["site"] = str(cell.getSiteName()) if cell.getSiteName() is not None else None
        candidate["bel"] = str(cell.getBELName()) if cell.getBELName() is not None else None

        try:
            bel = cell.getBEL()
            if bel is None or not bel.isLUT():
                candidate["reason"] = f"Cell '{cell_name}' is not placed on a LUT BEL"
                candidates.append(candidate)
                continue
            if not cell.isPlaced() or cell.getSiteInst() is None:
                candidate["reason"] = f"Cell '{cell_name}' is not placed"
                candidates.append(candidate)
                continue

            cell_type = candidate["cell_type"]
            is_routethru = bool(getattr(cell, "isRoutethru", lambda: False)())
            if not cell_type.startswith("LUT") and not is_routethru:
                candidate["reason"] = (
                    f"Cell '{cell_name}' has type {cell_type}; only ordinary LUT "
                    "cells/routethrus are inspectable"
                )
                candidates.append(candidate)
                continue

            current_physical = _resolve_lut_physical_pin(cell, logical_pin)
            current_site = _site_pin_name_for_physical(cell, current_physical)
            target_physical = _resolve_lut_target_physical_pin(
                cell, current_physical, "FASTEST", lut_tools
            )
            target_site = _site_pin_name_for_physical(cell, target_physical)

            candidate["current_physical_pin"] = current_physical
            candidate["current_site_pin"] = current_site
            candidate["fastest_target_physical_pin"] = target_physical
            candidate["fastest_target_site_pin"] = target_site
            candidate["is_actionable"] = target_physical != current_physical
            candidate["reason"] = (
                "fastest target is available"
                if candidate["is_actionable"]
                else "fastest target resolves to the current pin"
            )
        except Exception as e:
            candidate["reason"] = str(e)

        candidates.append(candidate)

    return {
        "status": "success",
        "candidates": candidates,
        "invalid": invalid,
    }


def initialize_rapidwright(jvm_max_memory: str = "4G") -> Dict[str, Any]:
    """
    Initialize the RapidWright environment.
    
    Args:
        jvm_max_memory: Maximum JVM heap size (default: "4G")
        
    Returns:
        Dictionary with initialization status, version, and install path
    """
    global _initialized
    
    if _initialized:
        # Return version and path info even when already initialized
        try:
            import rapidwright
            import os
            from com.xilinx.rapidwright.device import Device
            version = str(Device.RAPIDWRIGHT_VERSION)
            install_path = os.path.dirname(rapidwright.__file__)
            rapidwright_path_env = os.environ.get('RAPIDWRIGHT_PATH')
            classpath = os.environ.get('CLASSPATH')
        except Exception:
            version = 'unknown'
            install_path = 'unknown'
            rapidwright_path_env = None
        
        result = {
            "status": "already_initialized", 
            "message": "RapidWright already initialized",
            "rapidwright_version": version,
            "rapidwright_install_path": install_path
        }
        if rapidwright_path_env:
            result["RAPIDWRIGHT_PATH"] = rapidwright_path_env
        if classpath:
            result["CLASSPATH"] = classpath
        return result
    
    try:
        import os

        # Point at the repo-local RapidWright submodule so we always pick up
        # the expected Python and Java classes for this checkout.
        env_info = _configure_repo_local_rapidwright()
        rw_root = env_info["RAPIDWRIGHT_PATH"]
        logger.info("Using repo-local RapidWright checkout at %s", rw_root)

        # Import rapidwright - this automatically starts the JVM
        import rapidwright
        from com.xilinx.rapidwright.device import Device
        
        _initialized = True
        
        logger.info("RapidWright initialized successfully")
        
        # Test that we can access basic functionality
        device_count = len(Device.getAvailableDevices())
        
        # Get version and install path
        version = str(Device.RAPIDWRIGHT_VERSION)
        install_path = os.path.dirname(rapidwright.__file__)
        rapidwright_path_env = os.environ.get('RAPIDWRIGHT_PATH')
        classpath = os.environ.get('CLASSPATH')

        result = {
            "status": "success",
            "message": "RapidWright initialized successfully",
            "rapidwright_version": version,
            "rapidwright_install_path": install_path,
            "available_devices": device_count
        }
        if rapidwright_path_env:
            result["RAPIDWRIGHT_PATH"] = rapidwright_path_env
        if classpath:
            result["CLASSPATH"] = classpath            
        return result
        
    except Exception as e:
        logger.error(f"Failed to initialize RapidWright: {e}")
        return {
            "status": "error",
            "message": f"Failed to initialize RapidWright: {str(e)}",
            "hint": "Make sure RapidWright is built ('make build-rapidwright'), RAPIDWRIGHT_PATH/CLASSPATH are set, and Java 11+ is installed"
        }


def get_supported_devices() -> Dict[str, Any]:
    """
    Get list of all FPGA devices supported by RapidWright, including families and part numbers.
    
    Returns:
        Dictionary with devices organized as a tree: Series -> FamilyType -> Devices
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.device import PartNameTools
        
        # Get all parts from RapidWright's part database
        all_parts = PartNameTools.getParts()
        
        # Build tree structure: Series -> FamilyType -> Devices (deduplicated)
        # Use sets to avoid duplicates since multiple parts map to the same device
        device_tree_sets = {}
        
        for part in all_parts:
            series_name = str(part.getSeries())
            family_name = str(part.getFamily())
            device_name = str(part.getDevice())
            
            # Initialize series level if needed
            if series_name not in device_tree_sets:
                device_tree_sets[series_name] = {}
            
            # Initialize family level if needed
            if family_name not in device_tree_sets[series_name]:
                device_tree_sets[series_name][family_name] = set()
            
            # Add device to family (set handles deduplication)
            device_tree_sets[series_name][family_name].add(device_name)
        
        # Convert sets to sorted lists for JSON serialization
        device_tree = {}
        total_devices = 0
        
        for series in device_tree_sets:
            device_tree[series] = {}
            for family in device_tree_sets[series]:
                device_tree[series][family] = sorted(device_tree_sets[series][family])
                total_devices += len(device_tree[series][family])
        
        # Calculate summary statistics
        series_count = len(device_tree)
        family_count = sum(len(families) for families in device_tree.values())
        
        return {
            "status": "success",
            "total_devices": total_devices,
            "series_count": series_count,
            "family_count": family_count,
            "device_tree": device_tree
        }
        
    except Exception as e:
        logger.error(f"Error getting supported devices: {e}")
        return {"error": str(e)}


def get_device_info(device_name: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific device.
    
    Args:
        device_name: Name of the device (e.g., 'xcvu3p', 'xcku040')
        
    Returns:
        Dictionary with device information
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.device import Device
        
        device = Device.getDevice(device_name)
        if device is None:
            return {"error": f"Device '{device_name}' not found"}
        
        return {
            "status": "success",
            "name": str(device.getName()),
            "family": str(device.getFamilyType()),
            "series": str(device.getSeries()),
            "architecture": str(device.getArchitecture()),
            "rows": device.getRows(),
            "columns": device.getColumns(),
            "tile_count": device.getAllTiles().size(),
            "site_count": device.getAllSites().length
        }
        
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return {"error": str(e)}


def read_checkpoint(dcp_path: str) -> Dict[str, Any]:
    """
    Read a design checkpoint (DCP) file.
    
    Args:
        dcp_path: Path to the DCP file
        
    Returns:
        Dictionary with load status and basic design info
    """
    global _current_design
    
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.design import Design
        from com.xilinx.rapidwright.tests import CodePerfTracker
        from pathlib import Path
        
        dcp_file = Path(dcp_path).expanduser().resolve()
        if not dcp_file.exists():
            return {"error": f"DCP file not found: {dcp_path}"}
        
        logger.info(f"Loading design from {dcp_file}")
        design = Design.readCheckpoint(str(dcp_file))
        _current_design = design
        
        return {
            "status": "success",
            "message": f"Design loaded successfully from {dcp_file.name}",
            "design_name": str(design.getName()),
            "device": str(design.getDevice().getName()),
            "part_name": str(design.getPartName()),
            "cell_count": design.getCells().size(),
            "net_count": design.getNets().size()
        }
        
    except Exception as e:
        logger.error(f"Error loading design: {e}")
        return {"error": str(e)}


def write_checkpoint(dcp_path: str, overwrite: bool = False) -> Dict[str, Any]:
    """
    Write the current design to a checkpoint (DCP) file.
    
    Args:
        dcp_path: Path where the DCP file will be saved
        overwrite: If True, overwrite existing file; if False, error if file exists
        
    Returns:
        Dictionary with save status, bytes written, and encrypted IP info
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    
    try:
        from com.xilinx.rapidwright.tests import CodePerfTracker
        from pathlib import Path
        import os
        
        output_file = Path(dcp_path).expanduser().resolve()
        
        # Check if file exists and overwrite is not set
        if output_file.exists() and not overwrite:
            return {
                "error": f"File '{output_file}' already exists. Set overwrite=True to replace it."
            }
        
        # Create parent directories if they don't exist
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        design = _current_design
        
        # Check if design contains encrypted cells before writing
        # Note: This method may not be available in all RapidWright versions
        try:
            contains_encrypted_ip = design.getNetlist().isEncrypted()
        except AttributeError:
            # Try alternative method or fall back to False
            try:
                contains_encrypted_ip = design.isNetlistEncrypted()
            except AttributeError:
                contains_encrypted_ip = False
                logger.warning("Could not determine if design contains encrypted IP")
        
        logger.info(f"Writing design checkpoint to {output_file}")
        design.writeCheckpoint(str(output_file))
        
        # Get file size
        bytes_written = output_file.stat().st_size
        
        # Check for accompanying Tcl script (generated for encrypted designs)
        tcl_script_path = str(output_file) + ".tcl"
        tcl_script_exists = os.path.exists(tcl_script_path)
        
        result = {
            "status": "success",
            "message": f"Design checkpoint saved successfully to {output_file.name}",
            "output_file": str(output_file),
            "bytes_written": bytes_written
        }
        
        # Add encrypted IP warning if applicable
        if contains_encrypted_ip:
            result["contains_encrypted_ip"] = True
            result["encrypted_ip_warning"] = (
                "This design contains encrypted IP. RapidWright has generated an "
                "accompanying Tcl script that is required to load this DCP in Vivado."
            )
            if tcl_script_exists:
                result["tcl_script_path"] = tcl_script_path
        
        logger.info(f"Design checkpoint saved: {bytes_written} bytes written")
        return result
        
    except Exception as e:
        logger.error(f"Error writing design checkpoint: {e}")
        return {"error": str(e)}


def report_approx_timing(
    clock_period_ns: Optional[float] = None,
    include_path: bool = True,
    max_path_lines: int = 20,
) -> Dict[str, Any]:
    """
    Report an approximate critical-path timing estimate using RapidWright's timing model.

    Args:
        clock_period_ns: Optional known clock period in nanoseconds. When provided,
            the tool computes an estimated WNS against that period instead of relying
            on RapidWright's internally-derived required time.
        include_path: Include the critical path text in the response.
        max_path_lines: Maximum number of path lines to include when include_path=True.

    Returns:
        Dictionary with approximate delay/slack/Fmax information for the current design.
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        from com.xilinx.rapidwright.timing import TimingManager

        start_time = time.time()
        design = _current_design
        timing_manager = TimingManager(design)
        timing_graph = timing_manager.getTimingGraph()

        critical_path = timing_graph.getMaxDelayPath()
        if critical_path is None:
            timing_graph.buildGraphPaths()
            critical_path = timing_graph.getMaxDelayPath()

        if critical_path is None:
            return {
                "error": (
                    "RapidWright timing graph did not produce a critical path. "
                    "This can happen if the design is incomplete or graph paths "
                    "could not be built."
                )
            }

        path_delay_ps = timing_graph.getPathDelay(critical_path)
        if path_delay_ps is None:
            path_delay_ps = critical_path.getWeight()

        if path_delay_ps is None:
            return {"error": "RapidWright timing graph returned no path delay"}

        path_delay_ps = float(path_delay_ps)
        path_delay_ns = path_delay_ps / 1000.0

        required_time_ps = None
        estimated_slack_ps = None
        timing_basis = "rapidwright_graph"

        if clock_period_ns is not None:
            required_time_ps = float(clock_period_ns) * 1000.0
            estimated_slack_ps = required_time_ps - path_delay_ps
            timing_basis = "provided_clock_period"
        else:
            try:
                required_time_ps = float(timing_graph.getRequiredTime(critical_path))
            except Exception:
                required_time_ps = None
            try:
                slack_value = timing_graph.getSlack(critical_path)
                if slack_value is not None:
                    estimated_slack_ps = float(slack_value)
            except Exception:
                estimated_slack_ps = None

        estimated_fmax_mhz = None
        if path_delay_ns > 0:
            estimated_fmax_mhz = 1000.0 / path_delay_ns

        result: Dict[str, Any] = {
            "status": "success",
            "timing_basis": timing_basis,
            "path_delay_ps": round(path_delay_ps, 3),
            "path_delay_ns": round(path_delay_ns, 3),
            "estimated_fmax_mhz": round(estimated_fmax_mhz, 2) if estimated_fmax_mhz is not None else None,
            "elapsed_seconds": round(time.time() - start_time, 3),
        }

        if required_time_ps is not None:
            result["required_time_ps"] = round(required_time_ps, 3)
            result["required_time_ns"] = round(required_time_ps / 1000.0, 3)

        if estimated_slack_ps is not None:
            result["estimated_wns_ps"] = round(estimated_slack_ps, 3)
            result["estimated_wns_ns"] = round(estimated_slack_ps / 1000.0, 3)

        try:
            worst_slack = timing_graph.getWorstSlack()
            if worst_slack is not None:
                result["graph_worst_slack_ps"] = round(float(worst_slack), 3)
                result["graph_worst_slack_ns"] = round(float(worst_slack) / 1000.0, 3)
        except Exception:
            pass

        try:
            result["critical_path_vertex_count"] = len(list(critical_path.getVertexList()))
        except Exception:
            pass

        if include_path:
            path_lines = str(critical_path).replace(", ", ",\n").splitlines()
            if max_path_lines > 0 and len(path_lines) > max_path_lines:
                result["critical_path"] = path_lines[:max_path_lines]
                result["critical_path_truncated"] = True
                result["critical_path_total_lines"] = len(path_lines)
            else:
                result["critical_path"] = path_lines

        return result

    except Exception as e:
        logger.error(f"Error reporting approximate timing: {e}")
        return {"error": str(e)}


def get_design_info() -> Dict[str, Any]:
    """
    Get information about the currently loaded design.
    
    Returns:
        Dictionary with design statistics
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}
    
    try:
        design = _current_design
        
        # Count cell types
        cell_types = {}
        for cell in design.getCells():
            cell_type = str(cell.getType())
            cell_types[cell_type] = cell_types.get(cell_type, 0) + 1
        
        # Get top 10 most common cell types
        top_types = sorted(cell_types.items(), key=lambda x: x[1], reverse=True)[:10]
        
        return {
            "status": "success",
            "design_name": str(design.getName()),
            "device": str(design.getDevice().getName()),
            "part_name": str(design.getPartName()),
            "cell_count": design.getCells().size(),
            "net_count": design.getNets().size(),
            "top_cell_types": dict(top_types),
            "is_netlist_encrypted": design.getNetlist().hasEncryptedCells()
        }
        
    except Exception as e:
        logger.error(f"Error getting design info: {e}")
        return {"error": str(e)}


def search_cells(pattern: Optional[str] = None, 
                cell_type: Optional[str] = None, 
                limit: int = 100) -> Dict[str, Any]:
    """
    Search for cells in the current design.
    
    Args:
        pattern: Name pattern to match (case-insensitive)
        cell_type: Filter by cell type
        limit: Maximum number of results
        
    Returns:
        Dictionary with matching cells
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}
    
    try:
        design = _current_design
        matching_cells = []
        pattern_lower = pattern.lower() if pattern else None
        
        for cell in design.getCells():
            if len(matching_cells) >= limit:
                break
            
            cell_name = str(cell.getName())
            cell_type_str = str(cell.getType())
            
            # Apply filters
            if pattern_lower and pattern_lower not in cell_name.lower():
                continue
            if cell_type and cell_type != cell_type_str:
                continue
            
            # Get placement info
            placement = "unplaced"
            if cell.isPlaced():
                site = cell.getSite()
                if site:
                    placement = str(site.getName())
            
            matching_cells.append({
                "name": cell_name,
                "type": cell_type_str,
                "placement": placement
            })
        
        return {
            "status": "success",
            "count": len(matching_cells),
            "cells": matching_cells,
            "truncated": len(matching_cells) >= limit
        }
        
    except Exception as e:
        logger.error(f"Error searching cells: {e}")
        return {"error": str(e)}


def get_tile_info(tile_name: str, device_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get information about a specific tile.
    
    Args:
        tile_name: Name of the tile
        device_name: Device name (uses current design's device if not specified)
        
    Returns:
        Dictionary with tile information
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.device import Device
        
        # Get device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}
        
        tile = device.getTile(tile_name)
        if tile is None:
            return {"error": f"Tile '{tile_name}' not found"}
        
        # Get sites in this tile
        sites = []
        if tile.getSites():
            for site in tile.getSites():
                sites.append({
                    "name": str(site.getName()),
                    "type": str(site.getSiteTypeEnum())
                })
        
        return {
            "status": "success",
            "name": str(tile.getName()),
            "type": str(tile.getTileTypeEnum()),
            "row": tile.getRow(),
            "column": tile.getColumn(),
            "site_count": len(sites),
            "sites": sites
        }
        
    except Exception as e:
        logger.error(f"Error getting tile info: {e}")
        return {"error": str(e)}


def search_sites(site_type: Optional[str] = None, 
                device_name: Optional[str] = None, 
                limit: int = 50) -> Dict[str, Any]:
    """
    Search for sites on a device.
    
    Args:
        site_type: Filter by site type (e.g., 'SLICEL', 'DSP48E2')
        device_name: Device name (uses current design's device if not specified)
        limit: Maximum number of results
        
    Returns:
        Dictionary with matching sites
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.device import Device
        
        # Get device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}
        
        matching_sites = []
        
        for site in device.getAllSites():
            if len(matching_sites) >= limit:
                break
            
            site_type_str = str(site.getSiteTypeEnum())
            
            # Filter by site type if specified
            if site_type and site_type not in site_type_str:
                continue
            
            tile = site.getTile()
            matching_sites.append({
                "name": str(site.getName()),
                "type": site_type_str,
                "tile": str(tile.getName()) if tile else "unknown"
            })
        
        return {
            "status": "success",
            "count": len(matching_sites),
            "sites": matching_sites,
            "truncated": len(matching_sites) >= limit
        }
        
    except Exception as e:
        logger.error(f"Error searching sites: {e}")
        return {"error": str(e)}


def optimize_lut_input_cone(hierarchical_input_pins: list[str]) -> Dict[str, Any]:
    """
    Optimize LUT input cones by combining chained small LUTs into a single larger LUT.
    
    This optimization reduces logic depth by replacing series of small LUTs with a single
    larger LUT (up to 6 inputs). This is particularly useful for critical paths where
    the delay through multiple LUT levels can be reduced to a single LUT.
    
    Args:
        hierarchical_input_pins: List of hierarchical input pin names to optimize
                                (e.g., ["module/submodule/inst/pin"])
        
    Returns:
        Dictionary with optimization results
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}
    
    try:
        from com.xilinx.rapidwright.eco import LUTInputConeOpt
        
        design = _current_design
        results = []
        
        logger.info(f"Optimizing {len(hierarchical_input_pins)} LUT input cones")
        
        for pin_name in hierarchical_input_pins:
            try:
                # Get the hierarchical port instance
                port_inst = design.getNetlist().getHierPortInstFromName(pin_name)
                
                if port_inst is None:
                    results.append({
                        "pin": pin_name,
                        "status": "error",
                        "message": f"Pin '{pin_name}' not found in design"
                    })
                    continue
                
                # Attempt optimization
                optimized_cell = LUTInputConeOpt.optimizedLUTInputCone(design, port_inst)
                
                if optimized_cell is None:
                    results.append({
                        "pin": pin_name,
                        "status": "no_optimization",
                        "message": "No optimization possible for this pin (may not be driven by LUTs or only single LUT in path)"
                    })
                else:
                    cell_info = {
                        "name": str(optimized_cell.getName()),
                        "type": str(optimized_cell.getType()),
                        "placement": "unplaced"
                    }
                    
                    if optimized_cell.isPlaced():
                        site = optimized_cell.getSite()
                        if site:
                            cell_info["placement"] = str(site.getName())
                    
                    results.append({
                        "pin": pin_name,
                        "status": "optimized",
                        "message": "LUT input cone successfully optimized",
                        "new_cell": cell_info
                    })
                    
            except Exception as e:
                logger.error(f"Error optimizing pin {pin_name}: {e}")
                results.append({
                    "pin": pin_name,
                    "status": "error",
                    "message": str(e)
                })
        
        # Count successful optimizations
        success_count = sum(1 for r in results if r["status"] == "optimized")
        
        return {
            "status": "success",
            "total_pins": len(hierarchical_input_pins),
            "optimized_count": success_count,
            "results": results
        }
        
    except Exception as e:
        logger.error(f"Error in LUT input cone optimization: {e}")
        return {"error": str(e)}


def inspect_lut_pin_swap_candidates(cell_pins: list[str]) -> Dict[str, Any]:
    """
    Inspect Vivado LUT input pin paths and report fastest-pin swap candidates.

    This is a read-only screening helper. It does not edit pin mappings, site
    pins, nets, or routing; it only resolves the current physical input and the
    fastest target physical input that swap_lut_pins would use for FASTEST.
    """
    if not _initialized:
        return {
            "status": "error",
            "error": "RapidWright not initialized. Call initialize_rapidwright first.",
            "candidates": [],
            "invalid": [],
        }

    if _current_design is None:
        return {
            "status": "error",
            "error": "No design loaded. Use read_checkpoint first.",
            "candidates": [],
            "invalid": [],
        }

    try:
        from com.xilinx.rapidwright.design.tools import LUTTools

        return _inspect_lut_pin_swap_candidates_for_design(
            cell_pins, _current_design, LUTTools
        )
    except Exception as e:
        logger.error("Error inspecting LUT pin swap candidates: %s", e)
        return {
            "status": "error",
            "error": str(e),
            "candidates": [],
            "invalid": [],
        }


def swap_lut_pins(
    cell_name: str,
    pin_swaps: list,
    unroute_affected: bool = True,
) -> Dict[str, Any]:
    """
    Move LUT input site pins and update the cell's logical-to-physical pin map.

    This is a direct RapidWright cell edit around LUTTools.swapMultipleLutPins().
    It preserves logical function by keeping each logical input attached to the
    same net while changing the physical LUT input pin used inside the SLICE.

    Args:
        cell_name: Hierarchical design cell name of a placed LUT cell.
        pin_swaps: List of [from_pin, to_pin] pairs. Pins may be logical
                   (I0..I5), physical (A1..A6), site pins (A1..H6), or target
                   aliases FASTEST/FASTEST_FREE/AUTO for the destination.
        unroute_affected: If true, unroute nets touched by the pin move so a
                          following Vivado route_design rebuilds legal routing.

    Returns:
        Dictionary with per-pin move details and before/after mappings.
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        from com.xilinx.rapidwright.design.tools import LUTTools
        from java.util import HashMap

        normalized_swaps = _normalize_lut_pin_swaps(pin_swaps)
        design = _current_design
        cell = design.getCell(cell_name)
        if cell is None:
            return {"error": f"Cell '{cell_name}' not found in design"}

        bel = cell.getBEL()
        if bel is None or not bel.isLUT():
            return {"error": f"Cell '{cell_name}' is not placed on a LUT BEL"}
        if not cell.isPlaced() or cell.getSiteInst() is None:
            return {"error": f"Cell '{cell_name}' is not placed"}

        cell_type = str(cell.getType() or "")
        if not cell_type.startswith("LUT") and not cell.isRoutethru():
            return {
                "error": (
                    f"Cell '{cell_name}' has type {cell_type}; swap_lut_pins "
                    "only supports ordinary LUT cells/routethrus"
                )
            }

        site_inst = cell.getSiteInst()
        java_swaps = HashMap()
        move_details = []
        affected_nets = {}
        before_mapping = _java_string_map_to_dict(cell.getPinMappingsP2L())
        try:
            before_equation = str(LUTTools.getLUTEquation(cell))
        except Exception:
            before_equation = None

        for old_token, new_token in normalized_swaps:
            old_physical = _resolve_lut_physical_pin(cell, old_token)
            new_physical = _resolve_lut_target_physical_pin(
                cell, old_physical, new_token, LUTTools
            )
            old_site_pin = _site_pin_name_for_physical(cell, old_physical)
            new_site_pin = _site_pin_name_for_physical(cell, new_physical)

            if old_site_pin == new_site_pin:
                move_details.append({
                    "from": old_token,
                    "to": new_token,
                    "status": "skipped",
                    "message": "source and target resolve to the same site pin",
                    "site_pin": old_site_pin,
                })
                continue

            old_spi = site_inst.getSitePinInst(old_site_pin)
            if old_spi is None:
                move_details.append({
                    "from": old_token,
                    "to": new_token,
                    "status": "skipped",
                    "message": f"source site pin {old_site_pin} is not present",
                    "old_physical_pin": old_physical,
                    "new_physical_pin": new_physical,
                })
                continue
            if not old_spi.isLUTInputPin():
                move_details.append({
                    "from": old_token,
                    "to": new_token,
                    "status": "skipped",
                    "message": f"source site pin {old_site_pin} is not a LUT input pin",
                })
                continue

            for spi in (old_spi, site_inst.getSitePinInst(new_site_pin)):
                if spi is None:
                    continue
                net = spi.getNet()
                if net is not None:
                    affected_nets[str(net.getName())] = net

            java_swaps.put(old_spi, new_site_pin)
            move_details.append({
                "from": old_token,
                "to": new_token,
                "status": "queued",
                "old_site_pin": old_site_pin,
                "new_site_pin": new_site_pin,
                "old_physical_pin": old_physical,
                "new_physical_pin": new_physical,
                "logical_pin": str(cell.getLogicalPinMapping(old_physical)),
            })

        if java_swaps.isEmpty():
            return {
                "status": "no_optimization",
                "cell": cell_name,
                "message": "No actionable LUT pin swaps were queued",
                "moves": move_details,
            }

        swap_count = int(LUTTools.swapMultipleLutPins(java_swaps))
        unrouted_nets = []
        if unroute_affected:
            for net_name, net in affected_nets.items():
                try:
                    net.unroute()
                    unrouted_nets.append(net_name)
                except Exception as e:
                    logger.warning("Could not unroute affected net %s: %s", net_name, e)

        after_mapping = _java_string_map_to_dict(cell.getPinMappingsP2L())
        try:
            after_equation = str(LUTTools.getLUTEquation(cell))
        except Exception:
            after_equation = None

        for detail in move_details:
            if detail.get("status") == "queued":
                detail["status"] = "swapped"

        return {
            "status": "success",
            "cell": cell_name,
            "cell_type": cell_type,
            "site": str(cell.getSiteName()),
            "bel": str(cell.getBELName()),
            "requested_swaps": len(normalized_swaps),
            "processed_swaps": swap_count,
            "unroute_affected": unroute_affected,
            "unrouted_nets": sorted(unrouted_nets),
            "before_mapping_p2l": before_mapping,
            "after_mapping_p2l": after_mapping,
            "before_equation": before_equation,
            "after_equation": after_equation,
            "moves": move_details,
        }

    except Exception as e:
        logger.error("Error swapping LUT pins on %s: %s", cell_name, e)
        return {"error": str(e)}


def optimize_fanout(net_name: str, split_factor: int) -> Dict[str, Any]:
    """
    Optimize high fanout nets by splitting them into multiple driven nets.

    This optimization reduces fanout by replicating the source driver and dividing
    the loads among multiple copies. This can improve timing and routability for
    nets with very high fanout.

    Args:
        net_name: Name of the high fanout net to optimize
        split_factor: Number of copies to create (k) - net will be split into k parts

    Returns:
        Dictionary with optimization results
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use load_design first."}

    try:
        from com.xilinx.rapidwright.eco import FanOutOptimization

        design = _current_design

        # Get the net
        net = design.getNet(net_name)
        if net is None:
            return {"error": f"Net '{net_name}' not found in design"}

        # Pre-checks to avoid java.lang.NullPointerException inside
        # FanOutOptimization.cutFanOutOfRoutedNet. The RapidWright ECO API
        # assumes the net has a replicable logic driver and a placed source
        # site pin; global resources (clock enable buffers, global clock
        # buffers, primary IO buffers) and static nets violate these
        # assumptions and crash with NPE deep inside the library.

        # Static nets (VCC/GND) cannot be replicated
        try:
            if net.isStaticNet():
                return {"error": f"Net '{net_name}' is a static net (VCC/GND); cannot optimize fanout"}
        except Exception:
            pass  # isStaticNet may not exist on older RapidWright versions

        source = net.getSource()
        if source is None:
            return {
                "error": (
                    f"Net '{net_name}' has no source site pin — may be a top-level "
                    f"port, an unrouted net, or a net driven by a dedicated resource "
                    f"without an accessible site pin. Fanout optimization requires a "
                    f"placed, replicable driver."
                )
            }

        try:
            site_inst = source.getSiteInst()
        except Exception:
            site_inst = None
        if site_inst is None:
            return {
                "error": f"Net '{net_name}' source has no placed site instance; cannot replicate driver"
            }

        # Reject dedicated resources that aren't replicable as ordinary logic.
        # BUFCE_LEAF and BUFCE are the common culprits for clock-enable nets
        # like 'ccm_inst/CEB1' that previously crashed with NPE.
        try:
            site_type = str(site_inst.getSiteTypeEnum())
        except Exception:
            site_type = ""
        dedicated_markers = (
            "BUFCE", "BUFG", "BUFR", "BUFMR", "BUFGCTRL",
            "IBUF", "OBUF", "IOB", "HPIOB", "HDIOB",
            "MMCM", "PLL", "IBUFDS", "OBUFDS",
        )
        if any(marker in site_type for marker in dedicated_markers):
            return {
                "error": (
                    f"Net '{net_name}' source is a dedicated resource "
                    f"({site_type}); fanout optimization only supports replicable "
                    f"logic drivers (LUT/FF in SLICE sites)."
                )
            }

        original_fanout = net.getFanOut()
        if original_fanout < 2:
            return {"error": f"Net '{net_name}' fanout is {original_fanout}; nothing to split"}
        if split_factor < 2:
            return {"error": f"split_factor must be >= 2, got {split_factor}"}

        logger.info(f"Optimizing net '{net_name}' with fanout {original_fanout} into {split_factor} parts")

        # Perform optimization
        FanOutOptimization.cutFanOutOfRoutedNet(design, net, split_factor)

        # Collect info about the new nets created
        # The optimization creates multiple nets by replicating the source
        new_nets_info = []

        # Try to find the replicated nets (they will have similar names)
        base_name = net_name
        for design_net in design.getNets():
            net_str = str(design_net.getName())
            if base_name in net_str and net_str != net_name:
                new_nets_info.append({
                    "name": net_str,
                    "fanout": design_net.getFanOut()
                })
                if len(new_nets_info) >= split_factor:
                    break

        return {
            "status": "success",
            "net_name": net_name,
            "original_fanout": original_fanout,
            "split_factor": split_factor,
            "new_nets": new_nets_info,
            "message": f"Successfully split net '{net_name}' into {split_factor} parts"
        }

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error in fanout optimization for net '{net_name}':\n{tb}")
        return {"error": f"{type(e).__name__}: {e}"}


def analyze_fabric_for_pblock(
    target_lut_count: int,
    target_ff_count: int,
    target_dsp_count: int = 0,
    target_bram_count: int = 0,
    device_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Analyze the FPGA fabric to find the best contiguous region for a pblock.
    
    Identifies regions that:
    1. Have enough resources (SLICEs, DSPs, BRAMs) for the target utilization
    2. Minimize crossing of delay-heavy columns (URAM, IO, etc.)
    3. Are as contiguous as possible
    
    Args:
        target_lut_count: Required number of LUTs (1.5x current usage)
        target_ff_count: Required number of FFs (1.5x current usage)
        target_dsp_count: Required number of DSPs (1.5x current usage)
        target_bram_count: Required number of BRAMs (1.5x current usage)
        device_name: Device name (uses loaded design's device if omitted)
        
    Returns:
        Dictionary with recommended pblock ranges and analysis
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.device import Device, TileTypeEnum
        
        # Get the device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}
        
        logger.info(f"Analyzing fabric for device: {device.getName()}")
        
        # Get all tiles
        tiles = device.getAllTiles()
        
        # Check for delay-heavy tile types by name pattern
        # These tile types cause routing delays when crossed
        def is_delay_heavy_tile(tile_type_enum) -> bool:
            tile_type_name = str(tile_type_enum.name())
            delay_patterns = ['URAM', 'HPIO', 'HDIO', 'HRIO']
            return any(pattern in tile_type_name for pattern in delay_patterns)
        
        # Map tile columns/rows to resource counts and types
        column_info = {}  # col -> {good_tiles, bad_tiles, resources}
        row_info = {}     # row -> {good_tiles, bad_tiles, resources}
        # Per-tile site counts for later rectangle-accurate counting.
        # The column/row aggregates above are *totals* across the full
        # device axis, so they can't answer "how many DSP sites are in
        # this rectangle" — we need a 2D map.
        tile_sites = {}   # (col, row) -> {"slices": n, "dsps": n, "brams": n}

        min_col, max_col = float('inf'), 0
        min_row, max_row = float('inf'), 0

        for tile in tiles:
            tile_type = tile.getTileTypeEnum()
            col = tile.getColumn()
            row = tile.getRow()

            # Track column/row bounds
            min_col = min(min_col, col)
            max_col = max(max_col, col)
            min_row = min(min_row, row)
            max_row = max(max_row, row)

            # Initialize column/row info
            if col not in column_info:
                column_info[col] = {
                    "good_tiles": 0,
                    "bad_tiles": 0,
                    "slice_sites": 0,
                    "dsp_sites": 0,
                    "bram_sites": 0
                }

            if row not in row_info:
                row_info[row] = {
                    "good_tiles": 0,
                    "bad_tiles": 0,
                    "slice_sites": 0,
                    "dsp_sites": 0,
                    "bram_sites": 0
                }

            # Categorize tile
            is_bad = is_delay_heavy_tile(tile_type)

            if is_bad:
                column_info[col]["bad_tiles"] += 1
                row_info[row]["bad_tiles"] += 1
            else:
                column_info[col]["good_tiles"] += 1
                row_info[row]["good_tiles"] += 1

                # Count resources in this tile
                sites = tile.getSites()
                if sites:
                    slice_n = 0
                    dsp_n = 0
                    bram_n = 0
                    for site in sites:
                        site_type_str = str(site.getSiteTypeEnum())

                        if "SLICE" in site_type_str:
                            slice_n += 1
                        elif "DSP" in site_type_str:
                            dsp_n += 1
                        elif "RAMB" in site_type_str or "BRAM" in site_type_str:
                            bram_n += 1

                    column_info[col]["slice_sites"] += slice_n
                    column_info[col]["dsp_sites"] += dsp_n
                    column_info[col]["bram_sites"] += bram_n
                    row_info[row]["slice_sites"] += slice_n
                    row_info[row]["dsp_sites"] += dsp_n
                    row_info[row]["bram_sites"] += bram_n

                    if slice_n or dsp_n or bram_n:
                        tile_sites[(col, row)] = {
                            "slices": slice_n,
                            "dsps": dsp_n,
                            "brams": bram_n,
                        }
        
        # Find contiguous column ranges with minimal bad columns
        good_columns = []
        for col in sorted(column_info.keys()):
            info = column_info[col]
            # A "good" column has mostly resource tiles, not delay-heavy tiles
            if info["good_tiles"] > info["bad_tiles"] * 2:  # 2:1 ratio
                good_columns.append(col)
        
        # Find the longest contiguous range of good columns
        best_col_range = None
        best_col_resources = {"slices": 0, "dsps": 0, "brams": 0}
        
        current_range = []
        current_resources = {"slices": 0, "dsps": 0, "brams": 0}
        
        for col in good_columns:
            if not current_range or col == current_range[-1] + 1:
                # Continue the range
                current_range.append(col)
                current_resources["slices"] += column_info[col]["slice_sites"]
                current_resources["dsps"] += column_info[col]["dsp_sites"]
                current_resources["brams"] += column_info[col]["bram_sites"]
            else:
                # Gap found - check if current range is better
                if (not best_col_range or 
                    len(current_range) > len(best_col_range) or
                    (len(current_range) == len(best_col_range) and 
                     current_resources["slices"] > best_col_resources["slices"])):
                    best_col_range = current_range
                    best_col_resources = current_resources.copy()
                
                # Start new range
                current_range = [col]
                current_resources = {
                    "slices": column_info[col]["slice_sites"],
                    "dsps": column_info[col]["dsp_sites"],
                    "brams": column_info[col]["bram_sites"]
                }
        
        # Check final range
        if (not best_col_range or 
            len(current_range) > len(best_col_range) or
            (len(current_range) == len(best_col_range) and 
             current_resources["slices"] > best_col_resources["slices"])):
            best_col_range = current_range
            best_col_resources = current_resources.copy()
        
        if not best_col_range:
            return {"error": "No suitable contiguous column range found"}
        
        # Similar analysis for rows
        good_rows = []
        for row in sorted(row_info.keys()):
            info = row_info[row]
            if info["good_tiles"] > info["bad_tiles"] * 2:
                good_rows.append(row)
        
        # Find best row range
        best_row_range = None
        best_row_resources = {"slices": 0, "dsps": 0, "brams": 0}
        
        current_range = []
        current_resources = {"slices": 0, "dsps": 0, "brams": 0}
        
        for row in good_rows:
            if not current_range or row == current_range[-1] + 1:
                current_range.append(row)
                current_resources["slices"] += row_info[row]["slice_sites"]
                current_resources["dsps"] += row_info[row]["dsp_sites"]
                current_resources["brams"] += row_info[row]["bram_sites"]
            else:
                if (not best_row_range or 
                    len(current_range) > len(best_row_range) or
                    (len(current_range) == len(best_row_range) and 
                     current_resources["slices"] > best_row_resources["slices"])):
                    best_row_range = current_range
                    best_row_resources = current_resources.copy()
                
                current_range = [row]
                current_resources = {
                    "slices": row_info[row]["slice_sites"],
                    "dsps": row_info[row]["dsp_sites"],
                    "brams": row_info[row]["bram_sites"]
                }
        
        if (not best_row_range or 
            len(current_range) > len(best_row_range) or
            (len(current_range) == len(best_row_range) and 
             current_resources["slices"] > best_row_resources["slices"])):
            best_row_range = current_range
            best_row_resources = current_resources.copy()
        
        if not best_row_range:
            return {"error": "No suitable contiguous row range found"}
        
        # Calculate center of the best region
        col_center = (best_col_range[0] + best_col_range[-1]) // 2
        row_center = (best_row_range[0] + best_row_range[-1]) // 2
        
        # Estimate required columns/rows for target resources
        # Each SLICE column has ~300 slices, each SLICE has ~4 LUTs and ~8 FFs
        required_slices = max(target_lut_count // 4, target_ff_count // 8)
        
        # Find actual placed cells to determine center of mass
        center_of_mass_col = col_center
        center_of_mass_row = row_center
        
        if _current_design:
            placed_cols = []
            placed_rows = []
            for cell in _current_design.getCells():
                if cell.isPlaced():
                    site = cell.getSite()
                    if site:
                        tile = site.getTile()
                        placed_cols.append(tile.getColumn())
                        placed_rows.append(tile.getRow())
            
            if placed_cols:
                center_of_mass_col = sum(placed_cols) // len(placed_cols)
                center_of_mass_row = sum(placed_rows) // len(placed_rows)
                logger.info(f"Center of mass: col={center_of_mass_col}, row={center_of_mass_row}")
        
        # Find a contiguous range around center of mass that:
        # 1. Has enough resources for target (with margin)
        # 2. Avoids bad columns
        # 3. Is reasonably sized (not the entire device)
        
        # SIMPLIFIED APPROACH: Use fixed reasonable size based on empirical data
        # For logicnets_jscl design (30K LUTs), optimal was 12 SLICE cols × 50 rows
        # This achieved timing closure. Scale based on target LUTs:
        # - Small designs (<20K LUTs): 15-20 columns
        # - Medium designs (20-50K LUTs): 20-30 columns
        # - Large designs (>50K LUTs): 30-40 columns
        
        target_luts = required_slices * 4  # Convert back to LUTs
        
        if target_luts < 20000:
            cols_needed = 20
        elif target_luts < 50000:
            cols_needed = 28
        else:
            cols_needed = 35
        
        rows_needed = 55  # Fixed reasonable height
        
        # Clamp to available fabric size
        cols_needed = min(cols_needed, len(best_col_range) // 2)
        rows_needed = min(rows_needed, len(best_row_range) // 2)
        
        # Grow from center of mass
        col_start_idx = next((i for i, c in enumerate(best_col_range) if c >= center_of_mass_col), len(best_col_range) // 2)
        row_start_idx = next((i for i, r in enumerate(best_row_range) if r >= center_of_mass_row), len(best_row_range) // 2)
        
        # Expand symmetrically from center
        col_left_idx = max(0, col_start_idx - cols_needed // 2)
        col_right_idx = min(len(best_col_range) - 1, col_start_idx + cols_needed // 2)
        row_bottom_idx = max(0, row_start_idx - rows_needed // 2)
        row_top_idx = min(len(best_row_range) - 1, row_start_idx + rows_needed // 2)
        
        final_col_min = best_col_range[col_left_idx]
        final_col_max = best_col_range[col_right_idx]
        final_row_min = best_row_range[row_bottom_idx]
        final_row_max = best_row_range[row_top_idx]

        # ------------------------------------------------------------------
        # Count actual sites inside the chosen rectangle, and widen the
        # region if hard resources (DSP / BRAM) are undershot.
        #
        # The old implementation reported a scalar estimate
        #     est_dsp = best_col_resources["dsps"] * selected_cols / len(best_col_range)
        # which ignored the row restriction AND assumed uniform DSP density
        # across the best column range. On designs like amd_mini-isp the
        # estimate fooled the caller into applying a pblock that contained
        # fewer DSP sites than the design had DSP cells, and Vivado's
        # place_design failed with Place 30-859.
        # ------------------------------------------------------------------
        def count_in_rect(c_min, c_max, r_min, r_max):
            s = d = b = 0
            for (c, r), cnt in tile_sites.items():
                if c_min <= c <= c_max and r_min <= r <= r_max:
                    s += cnt["slices"]
                    d += cnt["dsps"]
                    b += cnt["brams"]
            return s, d, b

        actual_slices, actual_dsps, actual_brams = count_in_rect(
            final_col_min, final_col_max, final_row_min, final_row_max
        )

        # Widen symmetrically while DSP or BRAM is under target. Rows are
        # cheap to widen (no bad-col gating), so exhaust rows first, then
        # fall through to columns.
        max_widen_iters = len(best_col_range) + len(best_row_range)
        for _ in range(max_widen_iters):
            need_dsp = target_dsp_count > 0 and actual_dsps < target_dsp_count
            need_bram = target_bram_count > 0 and actual_brams < target_bram_count
            if not (need_dsp or need_bram):
                break

            widened = False
            # Try growing rows first
            if row_bottom_idx > 0:
                row_bottom_idx -= 1
                final_row_min = best_row_range[row_bottom_idx]
                widened = True
            if row_top_idx < len(best_row_range) - 1:
                row_top_idx += 1
                final_row_max = best_row_range[row_top_idx]
                widened = True
            # Then columns if rows are pinned
            if not widened:
                if col_left_idx > 0:
                    col_left_idx -= 1
                    final_col_min = best_col_range[col_left_idx]
                    widened = True
                if col_right_idx < len(best_col_range) - 1:
                    col_right_idx += 1
                    final_col_max = best_col_range[col_right_idx]
                    widened = True
            if not widened:
                break  # hit fabric boundary on both axes

            actual_slices, actual_dsps, actual_brams = count_in_rect(
                final_col_min, final_col_max, final_row_min, final_row_max
            )

        # Hard failure: no expansion of the good fabric can hold the design
        if target_dsp_count > 0 and actual_dsps < target_dsp_count:
            return {
                "error": (
                    f"No contiguous region of the selected fabric can hold "
                    f"{target_dsp_count} DSPs (best we could fit: {actual_dsps}). "
                    f"The device may have insufficient DSPs or they are all "
                    f"outside the delay-clean fabric region."
                ),
                "target_requirements": {
                    "luts": target_lut_count,
                    "ffs": target_ff_count,
                    "dsps": target_dsp_count,
                    "brams": target_bram_count,
                },
                "best_region_tried": {
                    "col_min": int(final_col_min),
                    "col_max": int(final_col_max),
                    "row_min": int(final_row_min),
                    "row_max": int(final_row_max),
                    "actual_slice_sites": int(actual_slices),
                    "actual_dsp_sites": int(actual_dsps),
                    "actual_bram_sites": int(actual_brams),
                },
            }
        if target_bram_count > 0 and actual_brams < target_bram_count:
            return {
                "error": (
                    f"No contiguous region of the selected fabric can hold "
                    f"{target_bram_count} BRAMs (best we could fit: {actual_brams})."
                ),
                "target_requirements": {
                    "luts": target_lut_count,
                    "ffs": target_ff_count,
                    "dsps": target_dsp_count,
                    "brams": target_bram_count,
                },
                "best_region_tried": {
                    "col_min": int(final_col_min),
                    "col_max": int(final_col_max),
                    "row_min": int(final_row_min),
                    "row_max": int(final_row_max),
                    "actual_slice_sites": int(actual_slices),
                    "actual_dsp_sites": int(actual_dsps),
                    "actual_bram_sites": int(actual_brams),
                },
            }

        # Recompute selected_cols/rows after any widening
        selected_cols = col_right_idx - col_left_idx + 1
        selected_rows = row_top_idx - row_bottom_idx + 1

        # Use actual counts (not projections) in the response
        est_slice_sites = int(actual_slices)
        est_dsp_sites = int(actual_dsps)
        est_bram_sites = int(actual_brams)
        
        return {
            "status": "success",
            "device": str(device.getName()),
            "fabric_bounds": {
                "min_col": int(min_col),
                "max_col": int(max_col),
                "min_row": int(min_row),
                "max_row": int(max_row)
            },
            "recommended_region": {
                "col_min": int(final_col_min),
                "col_max": int(final_col_max),
                "row_min": int(final_row_min),
                "row_max": int(final_row_max),
                "center_col": int(col_center),
                "center_row": int(row_center),
                "center_of_mass_col": int(center_of_mass_col),
                "center_of_mass_row": int(center_of_mass_row),
                "contiguous_columns": selected_cols,
                "contiguous_rows": selected_rows
            },
            "estimated_resources": {
                "slice_sites": est_slice_sites,
                "dsp_sites": est_dsp_sites,
                "bram_sites": est_bram_sites,
                "approx_luts": est_slice_sites * 4,
                "approx_ffs": est_slice_sites * 8
            },
            "target_requirements": {
                "luts": target_lut_count,
                "ffs": target_ff_count,
                "dsps": target_dsp_count,
                "brams": target_bram_count
            },
            "message": f"Found region around center of mass: cols {final_col_min}-{final_col_max}, rows {final_row_min}-{final_row_max}"
        }
        
    except Exception as e:
        logger.error(f"Error analyzing fabric: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def analyze_critical_path_spread(
    critical_paths_data: list = None,
    input_file: str = None,
) -> Dict[str, Any]:
    """
    Calculate Manhattan distances for cells on critical paths.
    
    Takes critical path data from Vivado (list of cell names per path) and uses
    RapidWright's device model to get accurate tile coordinates and calculate distances.
    
    Args:
        critical_paths_data: List of paths, where each path is a list of cell names
        input_file: Optional path to JSON file containing critical_paths_data
        
    Returns:
        Dictionary with spread analysis including max distances per path
        
    Note: Either critical_paths_data or input_file must be provided
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}
    
    # Load data from file if specified
    if input_file:
        try:
            import json
            with open(input_file, 'r') as f:
                critical_paths_data = json.load(f)
        except Exception as e:
            return {"error": f"Error reading input file: {str(e)}"}
    
    if not critical_paths_data:
        return {"error": "No critical path data provided. Specify either critical_paths_data or input_file"}
    
    try:
        design = _current_design
        device = design.getDevice()
        
        logger.info(f"Analyzing {len(critical_paths_data)} critical paths for cell spread")
        
        path_results = []
        all_max_distances = []
        
        for path_idx, cell_names in enumerate(critical_paths_data):
            # Get placements for cells in this path
            cell_locations = []
            
            for cell_name in cell_names:
                try:
                    cell = design.getCell(cell_name)
                    if cell and cell.isPlaced():
                        site = cell.getSite()
                        if site:
                            tile = site.getTile()
                            cell_locations.append({
                                "cell": str(cell.getName()),
                                "type": str(cell.getType()),
                                "tile": str(tile.getName()),
                                "col": tile.getColumn(),
                                "row": tile.getRow()
                            })
                except Exception as e:
                    logger.debug(f"Could not get location for cell {cell_name}: {e}")
                    continue
            
            if len(cell_locations) < 2:
                continue
            
            # Calculate maximum Manhattan distance between SEQUENTIAL cells on this path
            max_distance = 0
            max_pair = None
            
            for i in range(len(cell_locations) - 1):
                loc1 = cell_locations[i]
                loc2 = cell_locations[i + 1]
                distance = abs(loc1["col"] - loc2["col"]) + abs(loc1["row"] - loc2["row"])
                
                if distance > max_distance:
                    max_distance = distance
                    max_pair = (loc1, loc2)
            
            all_max_distances.append(max_distance)
            
            path_results.append({
                "path_num": path_idx + 1,
                "cell_count": len(cell_locations),
                "max_distance": max_distance,
                "max_pair": max_pair
            })
        
        if not all_max_distances:
            return {
                "status": "warning",
                "message": "No cell location data found for paths",
                "paths_analyzed": len(critical_paths_data)
            }
        
        # Calculate statistics
        max_dist = max(all_max_distances)
        avg_dist = sum(all_max_distances) / len(all_max_distances)
        
        # Sort by distance
        path_results.sort(key=lambda x: -x["max_distance"])
        
        return {
            "status": "success",
            "paths_analyzed": len(critical_paths_data),
            "max_distance_found": int(max_dist),
            "avg_max_distance": float(avg_dist),
            "path_distances": [int(d) for d in all_max_distances],
            "worst_paths": path_results[:10]  # Top 10 worst
        }
        
    except Exception as e:
        logger.error(f"Error analyzing critical path spread: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def compare_design_structure(golden_dcp: str, revised_dcp: str) -> Dict[str, Any]:
    """
    Compare structural properties of two design checkpoints.
    
    Performs sanity checks to catch obvious errors:
    - Top-level module name
    - I/O port names, directions, and widths
    - Cell count comparison
    - Clock structure
    
    Args:
        golden_dcp: Path to the golden (reference) DCP file
        revised_dcp: Path to the revised (optimized) DCP file
        
    Returns:
        Dictionary with comparison results including pass/fail status
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.design import Design
        from pathlib import Path
        import json
        
        golden_path = Path(golden_dcp).expanduser().resolve()
        revised_path = Path(revised_dcp).expanduser().resolve()
        
        if not golden_path.exists():
            return {"error": f"Golden DCP not found: {golden_dcp}"}
        if not revised_path.exists():
            return {"error": f"Revised DCP not found: {revised_dcp}"}
        
        logger.info(f"Loading golden design from {golden_path}")
        golden = Design.readCheckpoint(str(golden_path))
        
        logger.info(f"Loading revised design from {revised_path}")
        revised = Design.readCheckpoint(str(revised_path))
        
        issues = []
        checks_passed = 0
        checks_total = 0
        
        # Check 1: Top-level module name
        checks_total += 1
        golden_top = str(golden.getName())
        revised_top = str(revised.getName())
        if golden_top == revised_top:
            checks_passed += 1
        else:
            issues.append(f"Top module name mismatch: '{golden_top}' vs '{revised_top}'")
        
        # Check 2: Device compatibility
        checks_total += 1
        golden_device = str(golden.getDevice().getName())
        revised_device = str(revised.getDevice().getName())
        if golden_device == revised_device:
            checks_passed += 1
        else:
            issues.append(f"Device mismatch: '{golden_device}' vs '{revised_device}'")
        
        # Check 3: I/O ports (names, directions, widths)
        golden_netlist = golden.getNetlist()
        revised_netlist = revised.getNetlist()
        
        golden_top_cell = golden_netlist.getTopCell()
        revised_top_cell = revised_netlist.getTopCell()
        
        # Get port information
        golden_ports = {}
        for port in golden_top_cell.getPorts():
            port_name = str(port.getName())
            port_dir = str(port.getDirection())
            port_width = port.getWidth()
            golden_ports[port_name] = {"direction": port_dir, "width": port_width}
        
        revised_ports = {}
        for port in revised_top_cell.getPorts():
            port_name = str(port.getName())
            port_dir = str(port.getDirection())
            port_width = port.getWidth()
            revised_ports[port_name] = {"direction": port_dir, "width": port_width}
        
        # Compare ports
        checks_total += 1
        port_issues = []
        
        # Check for missing/added ports
        golden_port_names = set(golden_ports.keys())
        revised_port_names = set(revised_ports.keys())
        
        missing_ports = golden_port_names - revised_port_names
        added_ports = revised_port_names - golden_port_names
        
        if missing_ports:
            port_issues.append(f"Missing ports in revised: {', '.join(sorted(missing_ports))}")
        if added_ports:
            port_issues.append(f"Added ports in revised: {', '.join(sorted(added_ports))}")
        
        # Check common ports for direction/width mismatches
        common_ports = golden_port_names & revised_port_names
        for port_name in sorted(common_ports):
            g_info = golden_ports[port_name]
            r_info = revised_ports[port_name]
            
            if g_info["direction"] != r_info["direction"]:
                port_issues.append(
                    f"Port '{port_name}' direction mismatch: "
                    f"{g_info['direction']} vs {r_info['direction']}"
                )
            
            if g_info["width"] != r_info["width"]:
                port_issues.append(
                    f"Port '{port_name}' width mismatch: "
                    f"{g_info['width']} vs {r_info['width']}"
                )
        
        if not port_issues:
            checks_passed += 1
        else:
            issues.extend(port_issues)
        
        # Check 4: Cell count (should increase or stay same, small decreases allowed)
        checks_total += 1
        golden_cell_count = golden.getCells().size()
        revised_cell_count = revised.getCells().size()
        
        cell_change_pct = (revised_cell_count - golden_cell_count) / golden_cell_count * 100
        
        # Allow small decrease (<=3%), up to 50% increase (optimizations can add/remove cells)
        if (revised_cell_count >= golden_cell_count * 0.97 and 
            revised_cell_count <= golden_cell_count * 1.5):
            checks_passed += 1
            # Note small changes as info, not error
            if revised_cell_count < golden_cell_count:
                issues.append(
                    f"INFO: Cell count decreased slightly: {golden_cell_count} -> {revised_cell_count} "
                    f"({abs(cell_change_pct):.2f}% decrease - likely due to optimization)"
                )
            elif revised_cell_count > golden_cell_count:
                issues.append(
                    f"INFO: Cell count increased: {golden_cell_count} -> {revised_cell_count} "
                    f"({cell_change_pct:.2f}% increase - likely due to optimization)"
                )
        else:
            if revised_cell_count < golden_cell_count:
                issues.append(
                    f"Cell count decreased significantly: {golden_cell_count} -> {revised_cell_count} "
                    f"({abs(cell_change_pct):.2f}% decrease - this may indicate logic removal)"
                )
            else:
                issues.append(
                    f"Cell count increased significantly: {golden_cell_count} -> {revised_cell_count} "
                    f"({cell_change_pct:.1f}% increase - this may indicate excessive optimization)"
                )
        
        # Summary - only count real issues (not INFO)
        real_issues = [i for i in issues if not i.startswith("INFO:")]
        all_checks_passed = (checks_passed == checks_total)
        
        result = {
            "status": "success",
            "comparison_result": "PASS" if all_checks_passed else "FAIL",
            "checks_passed": checks_passed,
            "checks_total": checks_total,
            "golden_design": {
                "path": str(golden_path),
                "top_module": golden_top,
                "device": golden_device,
                "cell_count": golden_cell_count,
                "port_count": len(golden_ports)
            },
            "revised_design": {
                "path": str(revised_path),
                "top_module": revised_top,
                "device": revised_device,
                "cell_count": revised_cell_count,
                "port_count": len(revised_ports)
            },
            "issues": issues
        }
        
        # Restore original design if it was loaded
        global _current_design
        if _current_design:
            _current_design = revised  # Keep revised loaded for potential further use
        
        return result
        
    except Exception as e:
        logger.error(f"Error comparing designs: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def _get_cell_physical_nets(design, cell):
    """
    Get all physical (non-static, non-clock) nets connected to a specific cell.

    Uses EDIFHierPortInst.getRoutedSitePinInst() to resolve each logical pin
    to its physical SitePinInst, then reads the Net from that pin.

    Args:
        design: RapidWright Design object
        cell: RapidWright Cell object (must be placed)

    Returns:
        List of Net objects connected to this cell
    """
    net_names = set()
    nets = []

    hier_cell = cell.getEDIFHierCellInst()
    if hier_cell is None:
        return []
    for ehpi in hier_cell.getHierPortInsts():
        spi = ehpi.getRoutedSitePinInst(design)
        if spi is None:
            continue
        net = spi.getNet()
        if net is not None and not net.isStaticNet() and not net.isClockNet():
            name = str(net.getName())
            if name not in net_names:
                net_names.add(name)
                nets.append(net)
    return nets

def _compute_routed_path_length(net, sink_pin):
    """
    Compute the routed path length from a net's source to a specific sink pin.

    Walks backwards from the sink pin node to the source pin node, summing 
    Manhattan tile distances along the way at each PIP.

    Args:
        net: RapidWright Net object (must be routed, i.e. have PIPs)
        sink_pin: RapidWright SitePinInst object for the sink site pin

    Returns:
        Routed path length in tile Manhattan distance units, or -1 if not found
    """
    pips = net.getPIPs()
    if pips is None or pips.size() == 0:
        return -1
    
    # Build a Node map to use as a lookup as we traverse nodes backwards from the
    # sink pin to the source pin.
    node_map = {}
    for pip in pips:
        if pip.isReversed():
            end_node, start_node = pip.getStartNode(), pip.getEndNode()
        else:
            end_node, start_node = pip.getEndNode(), pip.getStartNode()
        if end_node is not None and start_node is not None:
            node_map[end_node] = start_node
    
    src_pin = net.getSource()
    if src_pin is None:
        return -1
    source_node = src_pin.getConnectedNode()
    sink_node = sink_pin.getConnectedNode()
    if source_node is None or sink_node is None:
        return -1    

    # Traverse backwards (sink to source), accumulate Manhattan node (tile-to-tile) distances along the way
    length = 0
    node = sink_node
    while node is not None and node != source_node:
        prev = node_map.get(node)
        if prev is None:
            return -1
        length += node.getTile().getManhattanDistance(prev.getTile())
        node = prev

    return length if node == source_node else -1

def _detour_ratio(net, sink_pin):
    """Return the detour ratio for the provided sink back to the source of the provided net.

        Args:
        net: RapidWright Net object (must be routed, i.e. have PIPs)
        sink_pin: RapidWright SitePinInst object for the sink site pin
    """
    src_pin = net.getSource()
    if src_pin is None or src_pin.getSite() is None:
        return -1
    sink_site = sink_pin.getSite()
    if sink_site is None:
        return -1

    dist = src_pin.getTile().getManhattanDistance(sink_site.getTile())
    if dist == 0:
        return -1
    routed_length = _compute_routed_path_length(net, sink_pin)    
    if routed_length <= 0:
        return -1

    return routed_length / dist

def analyze_net_detour(
    critical_paths_data: list = None,
    detour_threshold: float = 2.0,
    input_file: str = None,
) -> Dict[str, Any]:
    """
    Analysis of routing for large detours on critical paths.

    For each interior cell on a critical path, computes the detour ratio of
    the incoming net (feeding the cell) and the outgoing net (driven by it).
    A high ratio on either side indicates the cell may benefit from
    re-placement closer to its connections.

    Input is a list of pins on the path as produced by Vivado MCP Server's
    extract_critical_path_pins:
        ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

    Args:
        critical_paths_data: List of paths, each a list of pin-path strings
                             from extract_critical_path_pins
        detour_threshold: Flag cells with max detour ratio above this (default: 2.0)
        input_file: Optional JSON file path containing critical_paths_data

    Returns:
        Dictionary with per-cell detour analysis and ranked re-placement candidates
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    if input_file:
        try:
            import json
            with open(input_file, 'r') as f:
                critical_paths_data = json.load(f)
        except Exception as e:
            return {"error": f"Error reading input file: {str(e)}"}

    if not critical_paths_data:
        return {"error": "No critical path data provided. "
                "Specify either critical_paths_data or input_file."}

    try:
        design = _current_design
        netlist = design.getNetlist()

        logger.info(f"Analyzing {len(critical_paths_data)} critical paths for "
                     f"routing detours (threshold={detour_threshold})")

        all_cells = []
        candidates = []
    
        for path_idx, pin_list in enumerate(critical_paths_data):
            if len(pin_list) < 3:
                continue
            cells_on_path = []
            prev_pin = None

            # Find cells on the path
            for pin_name in pin_list:
                pin = netlist.getHierPortInstFromName(pin_name)
                if pin is None:
                    continue
                # If current pin and previous pin point to the same cell 
                if prev_pin is not None and pin.getFullHierarchicalInst().equals(prev_pin.getFullHierarchicalInst()):
                    cells_on_path.append((prev_pin, pin))
                prev_pin = pin
            
            for (in_pin, out_pin) in cells_on_path:
                ratio = -1
                for pin in (in_pin, out_pin):
                    if pin is not None:
                        net = pin.getRoutedPhysicalNet(design)
                        if net is not None and not net.isStaticNet() and not net.isClockNet():
                            spi = pin.getRoutedSitePinInst(design)
                            if spi is not None:
                                if spi.isOutPin():
                                    for sink_spi in net.getSinkPins():
                                        cr = _detour_ratio(net, sink_spi)
                                        if cr > ratio:
                                            ratio = cr
                                else:
                                    cr = _detour_ratio(net, spi)
                                    if cr > ratio:
                                        ratio = cr
                cell_analysis = {
                    "path": path_idx + 1,
                    "cell": str(in_pin.getFullHierarchicalInst().toString()),
                    "max_detour_ratio": round(ratio, 2),
                }
                all_cells.append(cell_analysis)
                if ratio > detour_threshold:
                    candidates.append(cell_analysis)

        candidates.sort(key=lambda x: -x["max_detour_ratio"])
        all_cells.sort(key=lambda x: -x.get("max_detour_ratio", 0))

        return {
            "status": "success",
            "cells_analyzed": len(all_cells),
            "candidates_found": len(candidates),
            "detour_threshold": detour_threshold,
            "candidates": candidates,
            "all_cells": all_cells,
        }

    except Exception as e:
        logger.error(f"Error analyzing net detours: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}                

#


def optimize_cell_placement(
    cell_names: list,
    max_candidates: int = 10,
) -> Dict[str, Any]:
    """
    Re-place cells at the centroid of their connections to reduce routing detours.

    For each candidate cell:
      1. Finds all nets connected to the cell
      2. Collects tile coordinates of every pin on those nets
      3. Computes the centroid using ECOPlacementHelper.getCentroidOfPoints()
      4. Spirals outward from the centroid to find an available SLICE site
      5. Unplaces the cell, unroutes affected nets, and re-places at the new site

    After running, write the checkpoint and load it in Vivado to re-route the
    unrouted nets with route_design.

    Args:
        cell_names: List of cell names to re-place
        max_candidates: Maximum number of cells to process (default: 10)

    Returns:
        Dictionary with per-cell re-placement results
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    try:
        from com.xilinx.rapidwright.design import DesignTools
        from com.xilinx.rapidwright.eco import ECOPlacementHelper
        from com.xilinx.rapidwright.placer.blockplacer import Point
        from com.xilinx.rapidwright.device import SiteTypeEnum
        from java.util import ArrayList, EnumSet, HashMap

        design = _current_design
        device = design.getDevice()
        target_site_types = EnumSet.of(SiteTypeEnum.SLICEL, SiteTypeEnum.SLICEM)

        results = []
        cells_to_process = cell_names[:max_candidates]

        for cell_name in cells_to_process:
            cell = design.getCell(cell_name)
            if cell is None:
                results.append({"cell": cell_name, "status": "error",
                                "message": f"Cell '{cell_name}' not found"})
                continue

            if not cell.isPlaced():
                results.append({"cell": cell_name, "status": "error",
                                "message": "Cell is not placed"})
                continue

            old_site = cell.getSite()
            old_tile = old_site.getTile()
            old_placement = str(old_site.getName())
            old_bel = cell.getBEL()
            is_ff = old_bel.isFF() if old_bel is not None else False
            is_lut = old_bel.isLUT() if old_bel is not None else False

            # Gather clock net before unplacing (needed for FF compatibility)
            clk_net = None
            if is_ff:
                try:
                    clk_wire = cell.getSiteWireNameFromLogicalPin("C")
                    if clk_wire:
                        clk_net = cell.getSiteInst().getNetFromSiteWire(clk_wire)
                except Exception:
                    pass

            connected_nets = _get_cell_physical_nets(design, cell)
            if not connected_nets:
                results.append({"cell": cell_name, "status": "skipped",
                                "message": "No connected nets found"})
                continue

            # Collect all pin tile locations for centroid computation
            points = ArrayList()
            for net in connected_nets:
                for pin in net.getPins():
                    try:
                        t = pin.getTile()
                        if t is not None:
                            points.add(Point(t.getColumn(), t.getRow()))
                    except Exception:
                        continue

            if points.size() < 2:
                results.append({"cell": cell_name, "status": "skipped",
                                "message": "Not enough connection points"})
                continue

            centroid_site = ECOPlacementHelper.getCentroidOfPoints(
                device, points, target_site_types
            )
            if centroid_site is None:
                results.append({"cell": cell_name, "status": "error",
                                "message": "Could not compute centroid site"})
                continue

            # Find a compatible destination before mutating the design. Keep
            # the original BEL (for example B5LUT), rather than mapping every
            # LUT to A6LUT and losing the physical pin interpretation.
            new_site = None
            new_bel = None
            search_limit = 200
            old_bel_name = str(old_bel.getName()) if old_bel is not None else None
            physical_pin_mappings = cell.getPhysicalPinMappings()

            for idx, candidate in enumerate(
                ECOPlacementHelper.spiralOutFrom(centroid_site)
            ):
                if idx >= search_limit:
                    break
                if design.getSiteInstFromSite(candidate) is None:
                    bel = candidate.getBEL(old_bel_name) if old_bel_name else None
                    if bel is not None:
                        new_site = candidate
                        new_bel = bel
                        break

            if new_site is None or new_bel is None:
                results.append({"cell": cell_name, "status": "error",
                                "message": (
                                    f"No available {old_bel_name or 'compatible'} "
                                    "BEL near centroid"
                                )})
                continue

            # Perform the move. fullyUnplaceCell() can throw a
            # NullPointerException for some sparse LUT5 placements
            # with unused site wires. For LUTs, Cell.unplace() is sufficient
            # because all connected physical nets are fully unrouted below
            # and the destination is an otherwise empty site.
            try:
                if is_lut:
                    cell.unplace()
                else:
                    DesignTools.fullyUnplaceCell(cell, None)

                affected_net_names = []
                for net in connected_nets:
                    affected_net_names.append(str(net.getName()))
                    net.unroute()

                placed = design.placeCell(
                    cell,
                    new_site,
                    new_bel,
                    physical_pin_mappings,
                )
                if not placed:
                    raise RuntimeError(
                        f"RapidWright rejected placement at "
                        f"{new_site.getName()}/{new_bel.getName()}"
                    )
                cell.getSiteInst().routeSite()
            except Exception as e:
                results.append({"cell": cell_name, "status": "error",
                                "message": f"Placement failed: {e}"})
                continue

            new_placement = str(new_site.getName())
            move_distance = old_tile.getManhattanDistance(new_site.getTile())
            results.append({
                "cell": cell_name,
                "status": "success",
                "old_site": old_placement,
                "new_site": new_placement,
                "distance_moved": int(move_distance),
                "affected_nets": len(affected_net_names),
                "message": f"Moved from {old_placement} to {new_placement}"
            })

        success_count = sum(1 for r in results if r["status"] == "success")

        return {
            "status": "success",
            "cells_processed": len(cells_to_process),
            "cells_moved": success_count,
            "results": results,
            "message": (f"Re-placed {success_count}/{len(cells_to_process)} cells. "
                        "Affected nets need re-routing in Vivado via route_design.")
        }

    except Exception as e:
        logger.error(f"Error in cell placement optimization: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


def optimize_critical_path_detours(
    critical_paths_data: list = None,
    input_file: str = None,
    detour_threshold: float = 2.0,
    max_candidates: int = 5,
    clock_period_ns: Optional[float] = None,
) -> Dict[str, Any]:
    """
    High-level recipe: analyze critical-path routing detours and immediately
    re-place the strongest candidate cells.

    This wraps:
      1. analyze_net_detour()
      2. optimize_cell_placement()
      3. Optional report_approx_timing() before/after

    Intended usage:
      - Vivado extract_critical_path_pins -> JSON file
      - RapidWright optimize_critical_path_detours(input_file=...)
      - RapidWright write_checkpoint()
      - Vivado open_checkpoint + route_design + report_timing_summary
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}

    if _current_design is None:
        return {"error": "No design loaded. Use read_checkpoint first."}

    detour_analysis = analyze_net_detour(
        critical_paths_data=critical_paths_data,
        detour_threshold=detour_threshold,
        input_file=input_file,
    )
    if "error" in detour_analysis:
        return detour_analysis

    candidates = detour_analysis.get("candidates", [])
    selected_cells = []
    seen = set()
    for candidate in candidates:
        cell_name = candidate.get("cell")
        if not cell_name or cell_name in seen:
            continue
        seen.add(cell_name)
        selected_cells.append(cell_name)
        if len(selected_cells) >= max_candidates:
            break

    result: Dict[str, Any] = {
        "status": "success",
        "detour_threshold": detour_threshold,
        "candidates_found": len(candidates),
        "selected_cells": selected_cells,
    }

    if clock_period_ns is not None:
        before_timing = report_approx_timing(
            clock_period_ns=clock_period_ns,
            include_path=False,
        )
        if "error" not in before_timing:
            result["approx_timing_before"] = before_timing

    if not selected_cells:
        result["message"] = "No detour candidates exceeded the threshold"
        return result

    placement_result = optimize_cell_placement(
        cell_names=selected_cells,
        max_candidates=max_candidates,
    )
    result["placement_result"] = placement_result

    if "error" not in placement_result and clock_period_ns is not None:
        after_timing = report_approx_timing(
            clock_period_ns=clock_period_ns,
            include_path=False,
        )
        if "error" not in after_timing:
            result["approx_timing_after"] = after_timing

    moved_cells = 0
    if "error" not in placement_result:
        moved_cells = placement_result.get("cells_moved", 0)
    result["cells_moved"] = moved_cells
    result["message"] = (
        f"Detour repair processed {len(selected_cells)} selected cells and moved "
        f"{moved_cells}. Re-route in Vivado to measure exact timing."
    )
    return result


def convert_fabric_region_to_pblock_ranges(
    col_min: int,
    col_max: int,
    row_min: int,
    row_max: int,
    device_name: Optional[str] = None,
    use_clock_regions: bool = False
) -> Dict[str, Any]:
    """
    Convert fabric region (column/row coordinates) to Vivado pblock range strings.
    
    Generates a complete pblock string with all site types (SLICE, DSP, BRAM, URAM)
    in the format: "SLICE_X55Y0:SLICE_X109Y179 DSP48E2_X8Y0:DSP48E2_X13Y71 ..."
    
    Args:
        col_min, col_max: Column range (tile coordinates)
        row_min, row_max: Row range (tile coordinates)
        device_name: Device name (uses loaded design's device if omitted)
        use_clock_regions: If True, use CLOCKREGION ranges (simpler but coarser)
        
    Returns:
        Dictionary with pblock range strings suitable for Vivado create_pblock
    """
    if not _initialized:
        return {"error": "RapidWright not initialized. Call initialize_rapidwright first."}
    
    try:
        from com.xilinx.rapidwright.device import Device, SiteTypeEnum
        
        # Get the device
        if device_name:
            device = Device.getDevice(device_name)
        elif _current_design:
            device = _current_design.getDevice()
        else:
            return {"error": "No device specified and no design loaded"}
        
        if use_clock_regions:
            # Use clock region ranges (simpler, coarser granularity)
            cr_x_min = col_min // 60
            cr_x_max = col_max // 60
            cr_y_min = row_min // 60
            cr_y_max = row_max // 60
            
            pblock_range = f"CLOCKREGION_X{cr_x_min}Y{cr_y_min}:CLOCKREGION_X{cr_x_max}Y{cr_y_max}"
            
            return {
                "status": "success",
                "pblock_ranges": pblock_range,
                "format": "CLOCKREGION"
            }
        
        # Use site ranges (finer granularity) - find all site types in region
        # Track min/max coordinates for each site type
        site_bounds = {
            "SLICE": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "DSP48E2": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "RAMB18": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "RAMB36": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
            "URAM288": {"min_x": float('inf'), "max_x": 0, "min_y": float('inf'), "max_y": 0, "count": 0},
        }
        
        # Iterate through all tiles in the region
        for tile in device.getAllTiles():
            col = tile.getColumn()
            row = tile.getRow()
            
            # Check if tile is within our region
            if not (col_min <= col <= col_max and row_min <= row <= row_max):
                continue
            
            # Check all sites in this tile
            sites = tile.getSites()
            if not sites:
                continue
            
            for site in sites:
                site_type = site.getSiteTypeEnum()
                site_type_name = str(site_type.name())
                site_name = str(site.getName())
                
                # Determine site type category based on name patterns
                site_category = None
                
                if site_type_name in ['SLICEL', 'SLICEM']:
                    site_category = "SLICE"
                elif 'DSP48E2' in site_type_name:
                    site_category = "DSP48E2"
                elif site_type_name in ['RAMB18E1', 'RAMB181', 'RAMB180', 'RAMB18_L', 'RAMB18_U', 'RAMBFIFO18']:
                    site_category = "RAMB18"
                elif site_type_name in ['RAMB36', 'RAMB36E1', 'RAMBFIFO36', 'RAMBFIFO36E1']:
                    site_category = "RAMB36"
                elif site_type_name == 'URAM288':
                    site_category = "URAM288"
                
                if site_category and site_category in site_bounds:
                    # Get instance X/Y coordinates from the site
                    try:
                        x = site.getInstanceX()
                        y = site.getInstanceY()
                        
                        bounds = site_bounds[site_category]
                        bounds["min_x"] = min(bounds["min_x"], x)
                        bounds["max_x"] = max(bounds["max_x"], x)
                        bounds["min_y"] = min(bounds["min_y"], y)
                        bounds["max_y"] = max(bounds["max_y"], y)
                        bounds["count"] += 1
                    except:
                        pass
        
        # Build the pblock range string
        pblock_parts = []
        
        for site_type in ["SLICE", "DSP48E2", "RAMB18", "RAMB36", "URAM288"]:
            bounds = site_bounds[site_type]
            if bounds["count"] > 0 and bounds["min_x"] != float('inf'):
                min_x = int(bounds["min_x"])
                max_x = int(bounds["max_x"])
                min_y = int(bounds["min_y"])
                max_y = int(bounds["max_y"])
                
                range_str = f"{site_type}_X{min_x}Y{min_y}:{site_type}_X{max_x}Y{max_y}"
                pblock_parts.append(range_str)
        
        if not pblock_parts:
            return {"error": "No valid sites found in specified region"}
        
        pblock_ranges = " ".join(pblock_parts)
        
        return {
            "status": "success",
            "pblock_ranges": pblock_ranges,
            "format": "SITE",
            "site_counts": {
                "SLICE": site_bounds["SLICE"]["count"],
                "DSP48E2": site_bounds["DSP48E2"]["count"],
                "RAMB18": site_bounds["RAMB18"]["count"],
                "RAMB36": site_bounds["RAMB36"]["count"],
                "URAM288": site_bounds["URAM288"]["count"]
            }
        }
        
    except Exception as e:
        logger.error(f"Error converting fabric region to pblock: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}
