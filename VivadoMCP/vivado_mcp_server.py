#!/usr/bin/env python3
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
# Portions of this file consist of AI-generated content.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server for Vivado - manages Vivado via pexpect for stdin/stdout control.

Usage:
    python vivado_mcp_server.py [--vivado-path /path/to/vivado]
"""

import argparse
import atexit
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional, Dict, Any

# This file is also launched directly (``python VivadoMCP/vivado_mcp_server.py``).
# In that mode Python adds only ``VivadoMCP`` to sys.path, while several
# framework-owned tools load proof/rendering code from the sibling
# ``FDAgents`` package.  Derive the repository root from this file rather than
# relying on the caller's working directory or PYTHONPATH.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if (_REPOSITORY_ROOT / "FDAgents").is_dir():
    repository_root = str(_REPOSITORY_ROOT)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from VivadoMCP.diagnostics import tcl_completion_diagnostic, tcl_failure_diagnostic
from VivadoMCP.timing_exception_guard import (
    exact_timing_xdc_capture_tcl,
    exact_timing_xdc_restore_tcl,
    inject_timing_exception_source_guard,
    protect_timing_exception_sources_tcl,
)

import pexpect
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Proof-gated ECO tools import FDAgents lazily while this file runs as a script.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from .timing_profile import build_timing_path_profile
    from .pblock_candidates import build_slice_pblock_candidates
    from .structural_analysis import (
        MAX_STRUCTURAL_CELLS,
        build_carry_cloud_tcl,
        build_hard_macro_cascade_tcl,
        parse_carry_cloud_output,
        parse_hard_macro_cascade_output,
        validate_max_cells,
        validate_timeout,
    )
    from .mcp_config import tool_param
    from .placement_oracle import (
        MAX_CE_CLONE_CANDIDATES,
        MAX_DSP_SLOT_CANDIDATES,
        MAX_ORACLE_TIMEOUT_S,
        MAX_PLACE_ATTEMPTS,
        MAX_SRL_CANDIDATES,
        build_connected_dsp_oracle_tcl,
        build_connected_recurrence_oracle_tcl,
        parse_connected_dsp_oracle_output,
        parse_connected_recurrence_oracle_output,
        validate_dsp_oracle_request,
        validate_recurrence_oracle_request,
    )
    from .recurrence_eco import (
        build_recurrence_family_eco_tcl,
        build_recurrence_family_postcheck_tcl,
        build_recurrence_eco_tcl,
        build_recurrence_postcheck_tcl,
    )
    from .fixed_point_eco import (
        build_fixed_point_eco_tcl,
        build_fixed_point_port_signature_tcl,
        build_fixed_point_postcheck_tcl,
        build_pipeline_rebalance_eco_tcl,
        build_pipeline_rebalance_postcheck_tcl,
        build_register_absorb_eco_tcl,
        build_register_absorb_postcheck_tcl,
        build_two_operand_eco_tcl,
        build_two_operand_postcheck_tcl,
        parse_fixed_point_port_signature,
        parse_pipeline_rebalance_control_nets,
        parse_register_absorb_control_nets,
    )
    from .memory_eco import (
        build_memory_repack_eco_tcl,
        build_memory_repack_postcheck_tcl,
    )
    from .banked_predicate_backend import (
        build_script as build_banked_predicate_script,
        load_probe_cache as load_banked_predicate_probe_cache,
        parse_backend_report as parse_banked_predicate_backend_report,
        parse_probe_report as parse_banked_predicate_probe_report,
        write_probe_cache as write_banked_predicate_probe_cache,
    )
    from .banked_ram_operator_backend import (
        build_script as build_banked_ram_operator_script,
        load_probe_cache as load_banked_ram_operator_probe_cache,
        parse_backend_report as parse_banked_ram_operator_backend_report,
        parse_probe_report as parse_banked_ram_operator_probe_report,
        write_probe_cache as write_banked_ram_operator_probe_cache,
    )
    from .commutative_reassociation_backend import (
        build_script as build_commutative_reassociation_script,
        invalidate_probe_cache as invalidate_commutative_reassociation_cache,
        load_contract as load_commutative_reassociation_contract,
        parse_backend_report as parse_commutative_reassociation_backend_report,
        parse_probe_report as parse_commutative_reassociation_probe_report,
        probe_no_report_reason as _commutative_probe_no_report_reason,
    )
    from .registered_dsp_lane_backend import (
        build_script as build_registered_dsp_lane_script,
        parse_backend_report as parse_registered_dsp_lane_backend_report,
        parse_probe_report as parse_registered_dsp_lane_probe_report,
    )
    from .registered_dead_state_backend import (
        build_script as build_registered_dead_state_script,
        parse_backend_report as parse_registered_dead_state_backend_report,
        parse_private_manifest as parse_registered_dead_state_manifest,
    )
    from .control_state_equation_backend import (
        build_script as build_control_state_equation_script,
        parse_backend_report as parse_control_state_equation_backend_report,
        parse_private_manifest as parse_control_state_equation_manifest,
        parse_probe_report as parse_control_state_equation_probe_report,
    )
    from .ff_srl_boundary_bridge_backend import (
        build_script as build_ff_srl_boundary_bridge_script,
        parse_backend_report as parse_ff_srl_boundary_bridge_backend_report,
        parse_private_manifest as parse_ff_srl_boundary_bridge_manifest,
        parse_probe_report as parse_ff_srl_boundary_bridge_probe_report,
    )
    from .memory_forward_backend import (
        build_probe_script as build_memory_forward_probe_script,
        load_private_manifest as load_memory_forward_private_manifest,
        load_probe_report as load_memory_forward_probe_report,
        parse_mutation_result as parse_memory_forward_mutation_result,
        recover_and_write_contract as recover_and_write_memory_forward_contract,
        render_selected_family as render_selected_memory_forward_family,
    )
    from .multilevel_control_backend import (
        build_probe_script as build_multilevel_control_probe_script,
        empty_contract as empty_multilevel_control_contract,
        load_private_manifest as load_multilevel_control_private_manifest,
        load_probe_report as load_multilevel_control_probe_report,
        parse_mutation_result as parse_multilevel_control_mutation_result,
        recover_and_write_contract as recover_and_write_multilevel_control_contract,
        render_selected_family as render_selected_multilevel_control_family,
    )
    from .multilevel_ram_control_backend import (
        build_probe_script as build_multilevel_ram_control_probe_script,
        empty_contract as empty_multilevel_ram_control_contract,
        load_private_manifest as load_multilevel_ram_control_private_manifest,
        load_probe_report as load_multilevel_ram_control_probe_report,
        parse_mutation_result as parse_multilevel_ram_control_mutation_result,
        recover_and_write_contract as recover_and_write_multilevel_ram_control_contract,
        render_selected_family as render_selected_multilevel_ram_control_family,
    )
    from .parity_reassociation_backend import (
        build_probe_script as build_registered_parity_probe_script,
        load_private_manifest as load_registered_parity_private_manifest,
        load_probe_report as load_registered_parity_probe_report,
        parse_mutation_result as parse_registered_parity_mutation_result,
        recover_and_write_contract as recover_and_write_registered_parity_contract,
        render_selected_family as render_selected_registered_parity_family,
    )
except ImportError:
    from timing_profile import build_timing_path_profile
    from pblock_candidates import build_slice_pblock_candidates
    from structural_analysis import (
        MAX_STRUCTURAL_CELLS,
        build_carry_cloud_tcl,
        build_hard_macro_cascade_tcl,
        parse_carry_cloud_output,
        parse_hard_macro_cascade_output,
        validate_max_cells,
        validate_timeout,
    )
    from mcp_config import tool_param
    from placement_oracle import (
        MAX_CE_CLONE_CANDIDATES,
        MAX_DSP_SLOT_CANDIDATES,
        MAX_ORACLE_TIMEOUT_S,
        MAX_PLACE_ATTEMPTS,
        MAX_SRL_CANDIDATES,
        build_connected_dsp_oracle_tcl,
        build_connected_recurrence_oracle_tcl,
        parse_connected_dsp_oracle_output,
        parse_connected_recurrence_oracle_output,
        validate_dsp_oracle_request,
        validate_recurrence_oracle_request,
    )
    from recurrence_eco import (
        build_recurrence_family_eco_tcl,
        build_recurrence_family_postcheck_tcl,
        build_recurrence_eco_tcl,
        build_recurrence_postcheck_tcl,
    )
    from fixed_point_eco import (
        build_fixed_point_eco_tcl,
        build_fixed_point_port_signature_tcl,
        build_fixed_point_postcheck_tcl,
        build_pipeline_rebalance_eco_tcl,
        build_pipeline_rebalance_postcheck_tcl,
        build_register_absorb_eco_tcl,
        build_register_absorb_postcheck_tcl,
        build_two_operand_eco_tcl,
        build_two_operand_postcheck_tcl,
        parse_fixed_point_port_signature,
        parse_pipeline_rebalance_control_nets,
        parse_register_absorb_control_nets,
    )
    from memory_eco import (
        build_memory_repack_eco_tcl,
        build_memory_repack_postcheck_tcl,
    )
    from banked_predicate_backend import (
        build_script as build_banked_predicate_script,
        load_probe_cache as load_banked_predicate_probe_cache,
        parse_backend_report as parse_banked_predicate_backend_report,
        parse_probe_report as parse_banked_predicate_probe_report,
        write_probe_cache as write_banked_predicate_probe_cache,
    )
    from banked_ram_operator_backend import (
        build_script as build_banked_ram_operator_script,
        load_probe_cache as load_banked_ram_operator_probe_cache,
        parse_backend_report as parse_banked_ram_operator_backend_report,
        parse_probe_report as parse_banked_ram_operator_probe_report,
        write_probe_cache as write_banked_ram_operator_probe_cache,
    )
    from commutative_reassociation_backend import (
        build_script as build_commutative_reassociation_script,
        invalidate_probe_cache as invalidate_commutative_reassociation_cache,
        load_contract as load_commutative_reassociation_contract,
        parse_backend_report as parse_commutative_reassociation_backend_report,
        parse_probe_report as parse_commutative_reassociation_probe_report,
        probe_no_report_reason as _commutative_probe_no_report_reason,
    )
    from registered_dsp_lane_backend import (
        build_script as build_registered_dsp_lane_script,
        parse_backend_report as parse_registered_dsp_lane_backend_report,
        parse_probe_report as parse_registered_dsp_lane_probe_report,
    )
    from registered_dead_state_backend import (
        build_script as build_registered_dead_state_script,
        parse_backend_report as parse_registered_dead_state_backend_report,
        parse_private_manifest as parse_registered_dead_state_manifest,
    )
    from control_state_equation_backend import (
        build_script as build_control_state_equation_script,
        parse_backend_report as parse_control_state_equation_backend_report,
        parse_private_manifest as parse_control_state_equation_manifest,
        parse_probe_report as parse_control_state_equation_probe_report,
    )
    from ff_srl_boundary_bridge_backend import (
        build_script as build_ff_srl_boundary_bridge_script,
        parse_backend_report as parse_ff_srl_boundary_bridge_backend_report,
        parse_private_manifest as parse_ff_srl_boundary_bridge_manifest,
        parse_probe_report as parse_ff_srl_boundary_bridge_probe_report,
    )
    from memory_forward_backend import (
        build_probe_script as build_memory_forward_probe_script,
        load_private_manifest as load_memory_forward_private_manifest,
        load_probe_report as load_memory_forward_probe_report,
        parse_mutation_result as parse_memory_forward_mutation_result,
        recover_and_write_contract as recover_and_write_memory_forward_contract,
        render_selected_family as render_selected_memory_forward_family,
    )
    from multilevel_control_backend import (
        build_probe_script as build_multilevel_control_probe_script,
        empty_contract as empty_multilevel_control_contract,
        load_private_manifest as load_multilevel_control_private_manifest,
        load_probe_report as load_multilevel_control_probe_report,
        parse_mutation_result as parse_multilevel_control_mutation_result,
        recover_and_write_contract as recover_and_write_multilevel_control_contract,
        render_selected_family as render_selected_multilevel_control_family,
    )
    from multilevel_ram_control_backend import (
        build_probe_script as build_multilevel_ram_control_probe_script,
        empty_contract as empty_multilevel_ram_control_contract,
        load_private_manifest as load_multilevel_ram_control_private_manifest,
        load_probe_report as load_multilevel_ram_control_probe_report,
        parse_mutation_result as parse_multilevel_ram_control_mutation_result,
        recover_and_write_contract as recover_and_write_multilevel_ram_control_contract,
        render_selected_family as render_selected_multilevel_ram_control_family,
    )
    from parity_reassociation_backend import (
        build_probe_script as build_registered_parity_probe_script,
        load_private_manifest as load_registered_parity_private_manifest,
        load_probe_report as load_registered_parity_probe_report,
        parse_mutation_result as parse_registered_parity_mutation_result,
        recover_and_write_contract as recover_and_write_registered_parity_contract,
        render_selected_family as render_selected_registered_parity_family,
    )

# Tool-layer tunables (FDAgents/config.yaml `vivado_mcp:` section).
PBLOCK_RESOURCE_MULTIPLIER = float(
    tool_param("vivado_mcp", "pblock_resource_multiplier", 1.5)
)
PBLOCK_EXPAND_FACTOR = float(tool_param("vivado_mcp", "pblock_expand_factor", 1.5))
HIGH_FANOUT_MIN_FANOUT = int(tool_param("vivado_mcp", "high_fanout_min_fanout", 100))

_SAFE_CLOCK_NAME_RE = re.compile(r"[A-Za-z0-9_./:+\-\[\]]+")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Vivado Tcl prompt pattern
# Pattern requires newline before prompt to avoid matching prompt in command echoes.
# This prevents the issue where pexpect matches stale prompts in the buffer.
VIVADO_PROMPT = r"\r?\nVivado% "

# Global state
_vivado_process: Optional[pexpect.spawn] = None
_vivado_pid: Optional[int] = None
_vivado_path: Optional[str] = None
_vivado_log_file: Optional[str] = None
_vivado_journal_file: Optional[str] = None
_design_open: bool = False
_command_pending: bool = False  # True if a command timed out and may still be running


def get_vivado_path() -> str:
    """Get Vivado executable path from global setting, VIVADO_EXEC env var, or PATH."""
    global _vivado_path
    if _vivado_path:
        return _vivado_path
    # Check VIVADO_EXEC environment variable
    vivado_exec_env = os.environ.get("VIVADO_EXEC")
    if vivado_exec_env:
        return vivado_exec_env
    # Search in PATH
    vivado = shutil.which("vivado")
    if vivado:
        return vivado
    raise RuntimeError("Vivado not found in PATH. Set VIVADO_EXEC env var, provide --vivado-path, or add Vivado to PATH.")


def build_vivado_spawn_command(vivado_path: str, args: list[str]) -> tuple[str, list[str]]:
    """Return the executable and args used to spawn Vivado.

    Set VIVADO_LOCK_FILE=/path/to/lock to serialize Vivado processes via
    flock while preserving pexpect interaction with the wrapped Vivado process.
    """
    lock_file = os.environ.get("VIVADO_LOCK_FILE")
    if not lock_file:
        return vivado_path, args

    flock_path = shutil.which("flock") or "flock"
    return flock_path, [lock_file, vivado_path, *args]


def terminate_vivado_process_tree(pid: int) -> None:
    """Terminate the wrapper and every Vivado descendant in its process group."""
    if hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            process_group = os.getpgid(pid)
            if process_group != os.getpgrp():
                os.killpg(process_group, signal.SIGKILL)
                return
        except (OSError, ProcessLookupError):
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def cleanup_vivado():
    """Kill Vivado process if running. Called on exit."""
    global _vivado_process, _vivado_pid
    process = _vivado_process
    pid = _vivado_pid
    _vivado_process = None
    _vivado_pid = None
    if pid:
        terminate_vivado_process_tree(pid)
    if process and process.isalive():
        try:
            process.terminate(force=True)
        except Exception:
            pass


def signal_handler(signum, frame):
    """Handle termination signals."""
    cleanup_vivado()
    sys.exit(0)


# Register cleanup handlers
atexit.register(cleanup_vivado)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def start_vivado(log_file: Optional[str] = None, journal_file: Optional[str] = None) -> pexpect.spawn:
    """Start Vivado in Tcl mode and wait for prompt.

    Args:
        log_file: Path to Vivado log file (default: vivado.log in current directory)
        journal_file: Path to Vivado journal file (default: vivado.jou in current directory)
    """
    global _vivado_process, _vivado_pid

    if _vivado_process and _vivado_process.isalive():
        logger.info("Vivado process already running")
        return _vivado_process

    vivado_path = get_vivado_path()
    logger.info(f"Starting Vivado from: {vivado_path}")

    # Build Vivado command arguments
    args = ["-mode", "tcl"]

    # Set log file if specified
    if log_file:
        args.extend(["-log", log_file])
        logger.info(f"Vivado log file: {log_file}")

    # Set journal file if specified
    if journal_file:
        args.extend(["-journal", journal_file])
        logger.info(f"Vivado journal file: {journal_file}")

    # Start Vivado in Tcl mode
    # Use large maxread buffer for handling large outputs
    # Set TERM=dumb to prevent terminal line wrapping and ANSI formatting
    # which can corrupt command echo parsing
    env = os.environ.copy()
    env["TERM"] = "dumb"

    spawn_command, spawn_args = build_vivado_spawn_command(vivado_path, args)
    if spawn_command != vivado_path:
        logger.info(f"Serializing Vivado startup with lock: {spawn_args[0]}")

    try:
        _vivado_process = pexpect.spawn(
            spawn_command,
            args=spawn_args,
            encoding="utf-8",
            timeout=300,  # 5 min default timeout for startup; AWS cold start can be very slow
            maxread=10000000,  # 10MB buffer for large outputs
            searchwindowsize=10000,  # Search window for prompt matching
            env=env,  # Use dumb terminal to prevent line wrapping
            dimensions=(100, 500),  # Set large terminal width to prevent wrapping
        )

        # Get the PID for reliable cleanup
        _vivado_pid = _vivado_process.pid
        logger.info(f"Vivado process started with PID: {_vivado_pid}")

        # Wait for Vivado prompt
        logger.info("Waiting for Vivado prompt...")
        _vivado_process.expect(VIVADO_PROMPT)
    except Exception:
        cleanup_vivado()
        raise
    logger.info("Vivado ready")

    return _vivado_process


def ensure_vivado() -> pexpect.spawn:
    """Ensure Vivado is running, start if needed."""
    global _vivado_process, _vivado_log_file, _vivado_journal_file
    global _design_open, _command_pending
    if _vivado_process is None or not _vivado_process.isalive():
        cleanup_vivado()
        _design_open = False
        _command_pending = False
        return start_vivado(_vivado_log_file, _vivado_journal_file)
    return _vivado_process


def wait_for_prompt(proc: pexpect.spawn, timeout: float) -> str:
    """Wait for Vivado prompt and return captured output."""
    proc.expect(VIVADO_PROMPT, timeout=timeout)
    return proc.before


def sync_after_timeout(proc: pexpect.spawn) -> str:
    """
    After a timeout, wait for the previous command to complete.
    Returns the output from the command that was running.
    """
    global _command_pending
    if not _command_pending:
        return ""

    # Wait indefinitely for the prompt (command to complete)
    # Use a very long timeout (1 hour) as a safety
    try:
        output = wait_for_prompt(proc, timeout=3600)
        _command_pending = False
        return f"[Previous command completed]\n{output}"
    except pexpect.TIMEOUT:
        # Still stuck after 1 hour - Vivado is truly hung
        _command_pending = True
        raise RuntimeError("Vivado appears to be hung. Use restart_vivado to recover.")


def run_tcl_command(command: str, timeout: Optional[float] = None) -> str:
    """
    Run a Tcl command in Vivado and return the output.

    Args:
        command: Tcl command to execute
        timeout: Timeout in seconds (None for default 300s)

    Returns:
        Command output as string
    """
    global _command_pending, _design_open

    proc = ensure_vivado()

    # If a previous command timed out, wait for it to complete first
    if _command_pending:
        sync_output = sync_after_timeout(proc)
        if sync_output:
            # Previous command completed, we can continue
            pass

    # Use provided timeout or default
    effective_timeout = timeout if timeout is not None else 300

    # Log the command (truncate if very long)
    cmd_log = command if len(command) < 200 else command[:200] + "..."
    logger.info(f"Executing Tcl command: {cmd_log}")

    # Send command
    proc.sendline(command)

    try:
        # Wait for prompt and capture output
        proc.expect(VIVADO_PROMPT, timeout=effective_timeout)

        # Get output (everything between command echo and prompt)
        output = proc.before

        # Remove the echoed command from output (first line)
        lines = output.split("\n")
        if lines and command in lines[0]:
            output = "\n".join(lines[1:])

        logger.info(f"Command completed successfully")
        return output.strip()

    except pexpect.TIMEOUT:
        logger.error(f"Command timed out after {effective_timeout}s: {cmd_log}")
        # pexpect cannot cancel an in-flight Tcl command.  Keeping this process
        # alive would make the next tool call wait up to an hour in
        # sync_after_timeout and can consume the entire contest budget.  Kill
        # only this MCP-owned Vivado process tree; ensure_vivado will create a
        # clean process on the next call and the Agent can restore its selected
        # checkpoint through the ordinary recovery path.
        cleanup_vivado()
        _design_open = False
        _command_pending = False
        raise


def run_tcl_script(script: str, timeout: Optional[float] = None) -> str:
    """Run a multiline Tcl script as one interactive Vivado command."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    if len(encoded) > 2048:
        variable = "__fdagents_script_" + hashlib.sha256(
            encoded.encode("ascii")
        ).hexdigest()[:12]
        run_tcl_command(f"set {variable} {{}}", timeout=30)
        try:
            for offset in range(0, len(encoded), 2048):
                chunk = encoded[offset:offset + 2048]
                # Tcl's ``append`` returns the complete accumulated value.  In
                # an interactive Vivado shell that result is echoed to the log,
                # turning an N-byte generated ECO into O(N^2) log traffic.
                # Return only a bounded acknowledgement for every chunk.
                run_tcl_command(
                    f"append {variable} {{{chunk}}}; set __fdagents_chunk_ack 1",
                    timeout=30,
                )
            return run_tcl_command(
                "eval [encoding convertfrom utf-8 "
                f"[binary decode base64 ${variable}]]",
                timeout=timeout,
            )
        finally:
            if not _command_pending:
                try:
                    cleanup_timeout = (
                        min(30.0, float(timeout)) if timeout is not None else 30.0
                    )
                    run_tcl_command(
                        f"unset -nocomplain {variable}", timeout=cleanup_timeout
                    )
                except Exception:
                    logger.warning(
                        "Failed to clear chunked Tcl script variable %s", variable
                    )
            else:
                logger.warning(
                    "Skipped chunked Tcl cleanup while a command is pending: %s",
                    variable,
                )
    command = (
        "eval [encoding convertfrom utf-8 "
        f"[binary decode base64 {{{encoded}}}]]"
    )
    return run_tcl_command(command, timeout=timeout)


def restart_vivado_process() -> str:
    """Kill and restart Vivado process."""
    global _design_open, _command_pending, _vivado_log_file, _vivado_journal_file
    cleanup_vivado()
    _design_open = False
    _command_pending = False
    start_vivado(_vivado_log_file, _vivado_journal_file)
    return "Vivado restarted successfully."


def close_current_design() -> str:
    """Close the current design if one is open."""
    global _design_open
    if _design_open:
        output = run_tcl_command("close_design")
        _design_open = False
        return output
    return "No design was open."


def _open_checkpoint_with_marker(path: Path, *, timeout: float) -> str:
    """Open exactly one checkpoint and require Tcl to reach a success marker."""
    global _design_open
    marker = f"FDAGENTS_EXACT_CHECKPOINT_OPEN_DONE_{secrets.token_hex(16)}"
    output = run_tcl_script(
        f"open_checkpoint {{{path}}}\nputs {{{marker}}}", timeout=timeout
    )
    lines = [line.strip() for line in output.splitlines()]
    if marker not in lines or any(line.startswith("ERROR:") for line in lines):
        raise RuntimeError(
            "checkpoint open did not complete; "
            f"vivado_output={output[-4000:]}"
        )
    _design_open = True
    return output


def _close_design_with_marker(*, timeout: float = 300.0) -> str:
    """Close the loaded design without trusting prompt return as success."""
    global _design_open
    if not _design_open:
        return "No design was open."
    marker = f"FDAGENTS_EXACT_DESIGN_CLOSE_DONE_{secrets.token_hex(16)}"
    output = run_tcl_script(
        f"close_design\nputs {{{marker}}}", timeout=timeout
    )
    lines = [line.strip() for line in output.splitlines()]
    if marker not in lines or any(line.startswith("ERROR:") for line in lines):
        raise RuntimeError(
            "design close did not complete; "
            f"vivado_output={output[-4000:]}"
        )
    _design_open = False
    return output


def _build_setup_timing_report_command(
    num_paths: int,
    clock: Optional[str] = None,
    *,
    nworst: bool = False,
) -> str:
    """Build a setup timing report command, optionally scoped to one clock."""
    cmd = (
        f"report_timing -return_string -max_paths {num_paths} "
        "-delay_type max -sort_by slack"
    )
    if nworst:
        cmd += " -nworst 1"
    if not clock:
        return cmd
    if not isinstance(clock, str) or _SAFE_CLOCK_NAME_RE.fullmatch(clock) is None:
        raise ValueError(
            "clock contains unsafe Tcl characters; allowed characters are "
            "letters, digits, _, -, ., /, :, [, and ]"
        )
    return (
        f"set contest_clock [get_clocks -quiet {{{clock}}}]; "
        "if {[llength $contest_clock] != 1} {"
        "error {clock not found or ambiguous}}; "
        f"{cmd} -to $contest_clock"
    )


def get_critical_high_fanout_nets(
    num_paths: int = 50,
    min_fanout: Optional[int] = None,
    exclude_clocks: bool = True,
    timeout: float = 600.0,
    clock: Optional[str] = None,
) -> str:
    """
    Extract high fanout nets from critical timing paths.

    Analyzes the worst negative slack (WNS) timing paths to identify non-clock
    nets with high fanout that may be candidates for fanout optimization.
    The output can be used with RapidWright's optimize_fanout tool.

    Net names are automatically resolved to their PARENT net names, which is
    required for RapidWright compatibility.
    """
    import re
    from collections import defaultdict

    if min_fanout is None:
        min_fanout = HIGH_FANOUT_MIN_FANOUT

    cmd = _build_setup_timing_report_command(num_paths, clock)

    # Flush buffer before generating timing report
    run_tcl_command("puts {fanout_analysis_start}", timeout=5)

    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return f"Error generating timing report: {str(e)}"

    # Parse the timing report to extract high fanout nets
    # Dictionary to track nets: net_name -> {fanout, path_count}
    net_info = defaultdict(lambda: {
        "fanout": 0,
        "path_count": 0,
        "paths": set(),
        "total_delay_ns": 0.0,
        "max_delay_ns": 0.0,
    })

    # Split report into individual paths
    lines = timing_report.split('\n')
    current_path_id = 0

    # Regex pattern to match net lines with fanout information
    # Example: "net (fo=267, routed)         1.225     4.454    pcie4.../s_axis_cc_tvalid_reg_lower"
    net_pattern = re.compile(
        r'net\s+\(fo=(\d+),\s*(routed|estimated)\)\s+'
        r'(-?\d+(?:\.\d+)?)'
    )

    # Clock net patterns to exclude
    clock_patterns = [
        r'CLK[_\[]',       # CLK_ or CLK[ (clock net naming convention)
        r'[_/]CLK$',       # ends with /CLK or _CLK
        r'CLOCK',          # Contains CLOCK
        r'_clk_',          # Contains _clk_
        r'/C$',            # Clock pin (ends with /C)
        r'BUFG',           # BUFG related
        r'MMCM',           # MMCM related
        r'PLL',            # PLL related
        r'TXOUTCLK',       # GT transceiver clock
        r'RXOUTCLK',       # GT transceiver clock
        r'USERCLK',        # User clock
        r'CORECLK',        # Core clock
    ]
    clock_regex = re.compile('|'.join(clock_patterns), re.IGNORECASE)

    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect new path (usually starts with "Slack" or contains path delimiter)
        if 'Slack' in line and ('ns' in line or 'VIOLATED' in line or 'MET' in line):
            current_path_id += 1

        # Look for net with fanout information
        match = net_pattern.search(line)
        if match:
            fanout = int(match.group(1))
            delay_ns = max(0.0, float(match.group(3)))

            # Only process nets meeting the minimum fanout threshold
            if fanout >= min_fanout:
                net_name = None

                # First try to find it on the current line after the fanout info
                parts = line.split()
                for part in parts:
                    if '/' in part and not part.startswith('(') and not part.endswith(')'):
                        net_name = part
                        break

                # If not found on current line, check next line
                if not net_name and i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if '/' in next_line and not next_line.startswith('net') and not 'Delay' in next_line:
                        parts = next_line.split()
                        for part in parts:
                            if '/' in part:
                                net_name = part
                                break

                if net_name:
                    # Check if this is a clock net
                    is_clock = False
                    if exclude_clocks and clock_regex.search(net_name):
                        is_clock = True

                    if not is_clock:
                        # Update net info
                        if fanout > net_info[net_name]["fanout"]:
                            net_info[net_name]["fanout"] = fanout
                        net_info[net_name]["paths"].add(current_path_id)
                        net_info[net_name]["path_count"] = len(net_info[net_name]["paths"])
                        net_info[net_name]["total_delay_ns"] += delay_ns
                        net_info[net_name]["max_delay_ns"] = max(
                            net_info[net_name]["max_delay_ns"], delay_ns
                        )

        i += 1

    if not net_info:
        return f"No high fanout nets (fanout >= {min_fanout}) found in the {num_paths} most critical paths."

    # Look up parent net names for all extracted nets
    parent_net_map = {}  # original_name -> parent_name

    for net_name in net_info.keys():
        try:
            # First, verify the net exists
            check_cmd = f"get_nets {{{net_name}}}"
            check_result = run_tcl_command(check_cmd, timeout=30.0)
            logger.info(f"[DEBUG] get_nets for '{net_name[-60:]}...': result='{check_result.strip()[:100]}'")

            # If get_nets returns empty or an error, use original name
            if not check_result.strip() or "ERROR" in check_result.upper() or "WARNING" in check_result.upper():
                logger.info(f"Net '{net_name}' not found or has errors, using as-is")
                parent_net_map[net_name] = net_name
                continue

            # Now get the parent property
            parent_cmd = f"get_property PARENT [get_nets {{{net_name}}}]"
            parent_result = run_tcl_command(parent_cmd, timeout=30.0)
            parent_name = parent_result.strip()
            logger.info(f"[DEBUG] PARENT for '{net_name[-60:]}...': result='{parent_name}'")

            # Validate the result - should not be empty, should contain '/' for hierarchical nets,
            # and should not look like a Tcl command or error
            if (parent_name and
                parent_name != net_name and
                '/' in parent_name and
                not parent_name.startswith('get_') and
                not parent_name.startswith('ERROR') and
                not parent_name.startswith('WARNING')):
                parent_net_map[net_name] = parent_name
                logger.info(f"[DEBUG] Using PARENT name: '{parent_name[-80:]}'")
            else:
                # Use original name if parent lookup returned invalid data
                parent_net_map[net_name] = net_name
                logger.info(f"[DEBUG] PARENT invalid, using original: '{net_name[-80:]}'")
        except Exception as e:
            # If lookup fails, keep original name
            logger.warning(f"Parent lookup failed for net '{net_name}': {e}")
            parent_net_map[net_name] = net_name

    # Rebuild net_info with parent net names
    parent_net_info = defaultdict(lambda: {
        "fanout": 0,
        "path_count": 0,
        "paths": set(),
        "total_delay_ns": 0.0,
        "max_delay_ns": 0.0,
    })

    for net_name, info in net_info.items():
        parent_name = parent_net_map[net_name]
        if info["fanout"] > parent_net_info[parent_name]["fanout"]:
            parent_net_info[parent_name]["fanout"] = info["fanout"]
        parent_net_info[parent_name]["paths"].update(info["paths"])
        parent_net_info[parent_name]["path_count"] = len(parent_net_info[parent_name]["paths"])
        parent_net_info[parent_name]["total_delay_ns"] += info["total_delay_ns"]
        parent_net_info[parent_name]["max_delay_ns"] = max(
            parent_net_info[parent_name]["max_delay_ns"], info["max_delay_ns"]
        )

    # Sort nets by path_count, then by fanout
    sorted_nets = sorted(
        parent_net_info.items(),
        key=lambda x: (-x[1]["path_count"], -x[1]["fanout"])
    )

    if not sorted_nets:
        return f"No high fanout nets (fanout >= {min_fanout}) found in the {num_paths} most critical paths."

    # Report the physical driver type before advertising a RapidWright target.
    # FanOutOptimization can replicate only ordinary SLICE FF/LUT sources; a
    # CARRY/DSP/BRAM output may be high fanout but is not compatible with that
    # backend.  This remains evidence for Luna rather than an action gate.
    rw_fanout_types = {
        "FDRE", "FDSE", "FDCE", "FDPE",
        "LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6",
    }
    driver_type_map = {}
    for net_name, _info in sorted_nets:
        try:
            driver_cmd = (
                f"set fdagents_n [get_nets -quiet {{{net_name}}}]; "
                "set fdagents_out [get_pins -quiet -of_objects $fdagents_n "
                "-filter {DIRECTION == OUT}]; "
                "set fdagents_cells [get_cells -quiet -of_objects $fdagents_out]; "
                "if {[llength $fdagents_cells] == 1} { "
                "get_property REF_NAME [lindex $fdagents_cells 0] "
                "} else { puts UNKNOWN }"
            )
            driver_type = run_tcl_command(driver_cmd, timeout=30.0).strip()
            if not driver_type or any(
                marker in driver_type.upper()
                for marker in ("ERROR", "WARNING", "COMMAND:")
            ):
                driver_type = "UNKNOWN"
        except Exception:
            driver_type = "UNKNOWN"
        driver_type_map[net_name] = driver_type

    # Format output
    result_lines = [
        f"=== High Fanout Nets in Critical Paths (Parent Net Names) ===",
        f"Analyzed {num_paths} worst timing paths",
        f"Minimum fanout threshold: {min_fanout}",
        f"Clock nets excluded: {exclude_clocks}",
        f"Note: Net names are resolved to parent nets for RapidWright compatibility",
        f"",
        f"Found {len(sorted_nets)} high fanout nets:",
        f"",
        f"{'Paths':>6}  {'Fanout':>8}  Parent Net Name",
        f"{'-'*6}  {'-'*8}  {'-'*50}",
    ]

    for net_name, info in sorted_nets:
        driver_type = driver_type_map.get(net_name, "UNKNOWN")
        rw_status = (
            "supported"
            if driver_type in rw_fanout_types
            else "unknown"
            if driver_type == "UNKNOWN"
            else "unsupported"
        )
        result_lines.append(
            f"{info['path_count']:>6}  {info['fanout']:>8}  {net_name}  "
            f"DriverType={driver_type} RapidWrightFanout={rw_status} "
            f"TimingTotalDelayNs={info['total_delay_ns']:.6f} "
            f"TimingMaxDelayNs={info['max_delay_ns']:.6f}"
        )

    result_lines.append("")
    result_lines.append("=== Supported Parent Net Names for RapidWright optimize_fanout ===")
    result_lines.append("(Only mechanically compatible FF/LUT-driven parent nets are listed below)")
    result_lines.append(
        "Compatibility is evidence: RapidWright optimize_fanout supports only "
        "FDRE/FDSE/FDCE/FDPE and LUT1..LUT6 drivers."
    )
    result_lines.append("")

    supported_names = [
        net_name
        for net_name, _info in sorted_nets
        if driver_type_map.get(net_name, "UNKNOWN") in rw_fanout_types
    ]
    result_lines.extend(supported_names or ["(none)"])

    return "\n".join(result_lines)


def extract_critical_path_cells(
    num_paths: int = 50,
    output_file: str = None,
    timeout: float = 600.0,
    clock: Optional[str] = None,
) -> str:
    """
    Extract cell names from critical timing paths.

    Parses timing report to get ordered list of cells on each critical path.
    Output is JSON format that can be passed to RapidWright's
    analyze_critical_path_spread.

    For pin-level data (needed by analyze_net_detour), use
    extract_critical_path_pins instead.

    Args:
        num_paths: Number of critical paths to extract
        output_file: Optional path to write JSON output to file instead of returning it
        timeout: Command timeout in seconds
        clock: Optional capture clock name used to limit setup paths

    Returns:
        JSON string with list of paths, or success message if output_file is specified
    """
    import re
    import json

    pin_suffixes = ['/C', '/D', '/Q', '/O', '/CE', '/R', '/S', '/CLR', '/PRE',
                    '/I0', '/I1', '/I2', '/I3', '/I4', '/I5', '/I6']
    # Generate detailed timing report
    cmd = _build_setup_timing_report_command(num_paths, clock, nworst=True)

    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"Error generating timing report: {str(e)}"})

    # Parse paths
    path_sections = re.split(r'Slack \(', timing_report)

    all_paths = []

    for path_idx, path_section in enumerate(path_sections[1:], 1):
        in_data_section = False
        cell_names = []

        for line in path_section.split('\n'):
            stripped = line.strip()

            if stripped.startswith('---'):
                in_data_section = True
                continue
            if not in_data_section:
                continue

            if '/' in line and not stripped.startswith('net (') and not stripped.startswith('net('):
                parts = line.split()
                for part in parts:
                    if '/' in part and not part.startswith('('):
                        cell_path = part
                        for suffix in pin_suffixes:
                            if cell_path.endswith(suffix):
                                cell_path = cell_path[:-len(suffix)]
                                break
                        if cell_path and cell_path not in cell_names:
                            cell_names.append(cell_path)
                        break

        if len(cell_names) >= 2:  # Only include paths with at least 2 cells
            all_paths.append(cell_names)

    # Write to file if specified, otherwise return JSON
    if output_file:
        try:
            import os
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            with open(output_file, 'w') as f:
                json.dump(all_paths, f, indent=2)
            return json.dumps({
                "status": "success",
                "message": f"Extracted {len(all_paths)} critical paths",
                "output_file": output_file,
                "path_count": len(all_paths)
            })
        except Exception as e:
            return json.dumps({"error": f"Error writing to file: {str(e)}"})
    else:
        return json.dumps(all_paths)


def extract_critical_path_pins(
    num_paths: int = 50,
    output_file: str = None,
    timeout: float = 600.0,
    clock: Optional[str] = None,
) -> str:
    """
    Extract pin-level paths from critical timing paths.

    Each path is a flat list of pin references like:
        ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

    Two consecutive pins from the same cell represent the cell's data path.
    Two consecutive pins from different cells represent a connecting net.

    Input pins are extracted from the Prop label (e.g. Prop_LUT6_I2_O -> I2).
    Output/endpoint pins come from the pin path directly (e.g. cell/O, cell/D).

    Args:
        num_paths: Number of critical paths to extract
        output_file: Optional path to write JSON output to file
        timeout: Command timeout in seconds
        clock: Optional capture clock name used to limit setup paths

    Returns:
        JSON string with list of pin paths
    """
    import re
    import json

    cmd = _build_setup_timing_report_command(num_paths, clock, nworst=True)

    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"Error generating timing report: {str(e)}"})

    path_sections = re.split(r'Slack \(', timing_report)

    all_paths = []

    for path_idx, path_section in enumerate(path_sections[1:], 1):
        pins = []
        separator_count = 0
        pending_in_pin = None

        for line in path_section.split('\n'):
            stripped = line.strip()

            if stripped.startswith('---'):
                separator_count += 1
                continue
            # Data path is between the 2nd and 3rd --- separators
            if separator_count < 2:
                continue
            if separator_count >= 3:
                break
            if stripped.startswith('net (') or stripped.startswith('net('):
                continue

            # Check for Prop label (on cell-type line, typically no '/')
            # e.g. Prop_E6LUT_SLICEL_I0_O or Prop_CARRY8_SLICEL_S[4]_CO[7]
            prop_match = re.search(
                r'\(Prop_\S+_(\w+(?:\[\d+\])?)_(\w+(?:\[\d+\])?)\)', line)
            if prop_match:
                in_pin_name = prop_match.group(1)
                if in_pin_name not in ('C', 'CLK'):
                    pending_in_pin = in_pin_name
                else:
                    pending_in_pin = None

            if '/' not in line:
                continue

            # Find the full pin path (e.g. "cell/Q" or "cell/O")
            parts = line.split()
            pin_path = None
            for part in parts:
                if '/' in part and not part.startswith('('):
                    pin_path = part
            if pin_path is None:
                continue

            last_slash = pin_path.rfind('/')
            if last_slash <= 0:
                continue
            cell_path = pin_path[:last_slash]

            if pending_in_pin is not None:
                pins.append(cell_path + '/' + pending_in_pin)
                pins.append(pin_path)
                pending_in_pin = None
            else:
                pins.append(pin_path)

        if len(pins) >= 2:
            all_paths.append(pins)

    if output_file:
        try:
            import os
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            with open(output_file, 'w') as f:
                json.dump(all_paths, f, indent=2)
            return json.dumps({
                "status": "success",
                "message": f"Extracted {len(all_paths)} critical path pin sequences",
                "output_file": output_file,
                "path_count": len(all_paths)
            })
        except Exception as e:
            return json.dumps({"error": f"Error writing to file: {str(e)}"})
    else:
        return json.dumps(all_paths)


def report_timing_path_profile(
    num_paths: int = 50,
    timeout: float = 600.0,
    clock: Optional[str] = None,
) -> str:
    """
    Return a compact JSON profile of the worst setup timing paths.

    This is intentionally read-only. It compresses report_timing into aggregate
    facts the LLM can use without spending tokens on the full report.
    """
    cmd = _build_setup_timing_report_command(num_paths, clock, nworst=True)
    run_tcl_command("puts {timing_path_profile_start}", timeout=5)

    try:
        timing_report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"Error generating timing report: {str(e)}"})

    profile = build_timing_path_profile(timing_report, max_paths=num_paths)
    return json.dumps(profile, sort_keys=True, separators=(",", ":"))


def analyze_carry_clouds(max_cells: int = 128, timeout: float = 300.0) -> str:
    """Return bounded carry-chain and local data-driver cloud topology."""
    try:
        validate_max_cells(max_cells)
        timeout = validate_timeout(timeout)
        output = run_tcl_script(build_carry_cloud_tcl(max_cells), timeout=timeout)
        result = parse_carry_cloud_output(output, max_cells)
    except pexpect.TIMEOUT:
        result = {
            "status": "error",
            "read_only": True,
            "error_code": "VIVADO_TCL_TIMEOUT",
            "error": "carry-cloud analysis exceeded its bounded Tcl deadline",
        }
    except Exception as exc:
        result = {"status": "error", "read_only": True, "error": str(exc)}
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def analyze_hard_macro_cascades(
    max_cells: int = 128, timeout: float = 300.0
) -> str:
    """Return bounded DSP/BRAM/URAM placement, registers, and cascade topology."""
    try:
        validate_max_cells(max_cells)
        timeout = validate_timeout(timeout)
        output = run_tcl_script(build_hard_macro_cascade_tcl(max_cells), timeout=timeout)
        result = parse_hard_macro_cascade_output(output, max_cells)
    except pexpect.TIMEOUT:
        result = {
            "status": "error",
            "read_only": True,
            "error_code": "VIVADO_TCL_TIMEOUT",
            "error": "hard-macro analysis exceeded its bounded Tcl deadline",
        }
    except Exception as exc:
        result = {"status": "error", "read_only": True, "error": str(exc)}
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure_connected_recurrence_slots(dcp_path: str, request: dict) -> str:
    """Transactionally probe connected recurrence primitives and restore the seed."""
    global _design_open, _command_pending
    result = None
    error = None
    restored = False
    try:
        value = validate_recurrence_oracle_request(request)
        source = Path(dcp_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.is_symlink() or "}" in str(source):
            raise ValueError("dcp_path is not a supported regular checkpoint path")
        if _sha256_file(source) != value["source_dcp_sha256"]:
            raise ValueError("source checkpoint SHA-256 mismatch")
        if _design_open:
            close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        output = run_tcl_script(
            build_connected_recurrence_oracle_tcl(value), timeout=value["timeout"]
        )
        try:
            result = parse_connected_recurrence_oracle_output(output, value)
        except Exception as exc:
            raise ValueError(
                f"oracle output validation failed: {exc}; "
                f"vivado_output={str(output)[-2000:]}"
            ) from exc
    except Exception as exc:
        error = str(exc)
    finally:
        try:
            source = Path(dcp_path).expanduser().resolve(strict=True)
            if _command_pending:
                restart_vivado_process()
            elif _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = _sha256_file(source) == request.get("source_dcp_sha256")
        except Exception as restore_exc:
            restored = False
            error = f"{error + '; ' if error else ''}source restore failed: {restore_exc}"
    if error is not None or not restored or result is None:
        return json.dumps({
            "status": "error",
            "read_only_after_restore": bool(restored),
            "srl_slots": [],
            "ce_clone_slots": [],
            "error": error or "source checkpoint restore was not proven",
        }, sort_keys=True, separators=(",", ":"))
    result.update({
        "read_only_after_restore": True,
        "cleanup": {
            "source_checkpoint_reopened": True,
            "temporary_namespace_discarded": True,
        },
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def measure_connected_dsp_slots(dcp_path: str, request: dict) -> str:
    """Transactionally probe connected DSP48E2 slots and restore the seed."""
    global _design_open, _command_pending
    result = None
    error = None
    restored = False
    try:
        value = validate_dsp_oracle_request(request)
        source = Path(dcp_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.is_symlink() or "}" in str(source):
            raise ValueError("dcp_path is not a supported regular checkpoint path")
        if _sha256_file(source) != value["source_dcp_sha256"]:
            raise ValueError("source checkpoint SHA-256 mismatch")
        if _design_open:
            close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        output = run_tcl_script(
            build_connected_dsp_oracle_tcl(value), timeout=value["timeout"]
        )
        try:
            result = parse_connected_dsp_oracle_output(output, value)
        except Exception as exc:
            raise ValueError(
                f"DSP oracle output validation failed: {exc}; "
                f"vivado_output={str(output)[-2000:]}"
            ) from exc
    except Exception as exc:
        error = str(exc)
    finally:
        try:
            source = Path(dcp_path).expanduser().resolve(strict=True)
            if _command_pending:
                restart_vivado_process()
            elif _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = _sha256_file(source) == request.get("source_dcp_sha256")
        except Exception as restore_exc:
            restored = False
            error = f"{error + '; ' if error else ''}source restore failed: {restore_exc}"
    if error is not None or not restored or result is None:
        return json.dumps({
            "status": "error",
            "read_only_after_restore": bool(restored),
            "slots": [],
            "error": error or "source checkpoint restore was not proven",
        }, sort_keys=True, separators=(",", ":"))
    result.update({
        "read_only_after_restore": True,
        "cleanup": {
            "source_checkpoint_reopened": True,
            "temporary_namespace_discarded": True,
        },
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _stable_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _owned_action_path(value: Any, *, name: str, must_exist: bool) -> Path:
    """Resolve a backend artifact while confining it to the MCP run directory."""
    root = Path.cwd().resolve()
    path = Path(str(value)).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{name} must stay inside the optimizer run directory") from exc
    if path == root or path.parent.is_symlink():
        raise ValueError(f"{name} has an unsafe boundary")
    if must_exist and (path.is_symlink() or not path.exists()):
        raise ValueError(f"{name} is absent or symbolic")
    return path


def _tcl_utf8_value(value: str) -> str:
    encoded = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
    return f"[encoding convertfrom utf-8 [binary decode base64 {{{encoded}}}]]"


def _run_isolated_registered_closure_worker(
    *,
    script: Path,
    source: Path,
    target_stem: str,
    output_dir: Path,
    max_stage_depth: int,
    word_lsb: int,
    word_width: int,
    timeout_s: float,
) -> str:
    """Run closure extraction in a killable one-family Vivado process."""
    command = [
        get_vivado_path(),
        "-mode", "batch", "-nojournal", "-nolog",
        "-source", str(script),
        "-tclargs", str(source), target_stem, str(output_dir),
        str(max_stage_depth), str(word_lsb), str(word_width),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        try:
            output, _ = process.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            output = ""
        log_path = output_dir.parent / f"{output_dir.name}.vivado.log"
        log_path.write_text(output or "", encoding="utf-8")
        raise TimeoutError(
            "registered closure isolated Vivado worker exceeded its hard "
            "deadline and was stopped; the next family starts a fresh worker"
        ) from exc
    log_path = output_dir.parent / f"{output_dir.name}.vivado.log"
    log_path.write_text(output or "", encoding="utf-8")
    if process.returncode != 0:
        raise RuntimeError(
            "registered closure isolated Vivado worker failed: "
            + (output or "")[-4000:]
        )
    return output or ""


def extract_registered_closure_facts(request: dict) -> str:
    """Extract an interval-aware multi-stage registered closure and restore source."""
    global _design_open, _command_pending
    required = {
        "dcp_path", "source_dcp_sha256", "target_stem", "output_dir",
        "max_stage_depth", "word_lsb", "word_width", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered closure extraction request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    target_stem = str(request["target_stem"])
    max_stage_depth = request["max_stage_depth"]
    word_lsb = request["word_lsb"]
    word_width = request["word_width"]
    timeout_s = request["timeout_s"]
    output_dir = _owned_action_path(
        request["output_dir"], name="registered closure output", must_exist=False
    )
    if (
        source.is_symlink()
        or not source.is_file()
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
        or not target_stem
        or len(target_stem) > 1024
        or "\x00" in target_stem
        or isinstance(max_stage_depth, bool)
        or not isinstance(max_stage_depth, int)
        or not 1 <= max_stage_depth <= 8
        or isinstance(word_lsb, bool)
        or not isinstance(word_lsb, int)
        or not 0 <= word_lsb <= 1_000_000
        or isinstance(word_width, bool)
        or not isinstance(word_width, int)
        or not 2 <= word_width <= 1024
        or isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not 30 <= float(timeout_s) <= 900
        or (output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()))
    ):
        raise ValueError("registered closure source or output is invalid")
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "extract_registered_closure.tcl"
    )
    # Keep the ordinary MCP Vivado session untouched. The old interactive Tcl
    # path made every per-cell object query 5-40x slower and allowed a timed-out
    # command to hold the worker during cleanup. A one-family batch process is
    # independently killable, naturally restarts between families, and writes
    # the exact same fixed-script facts.
    try:
        output_text = _run_isolated_registered_closure_worker(
            script=script_path,
            source=source,
            target_stem=target_stem,
            output_dir=output_dir,
            max_stage_depth=max_stage_depth,
            word_lsb=word_lsb,
            word_width=word_width,
            timeout_s=float(timeout_s),
        )
    except RuntimeError as exc:
        # These are deterministic shape refusals for one freshly mined live
        # family, not Vivado/MCP failures.  Return them as data so a residual
        # preflight can reject that family without emitting a tool-error tail
        # after an otherwise successful candidate promotion.
        detail = str(exc)
        structural_refusals = (
            (
                "selected registered family does not contain the requested "
                "contiguous interval",
                "REGISTERED_INTERVAL_INCOMPLETE",
            ),
            (
                "selected registered bank is not a whole number of requested words",
                "REGISTERED_BANK_WIDTH_MISMATCH",
            ),
            (
                "observable root is not a whole number of words",
                "OBSERVABLE_ROOT_WORD_WIDTH_MISMATCH",
            ),
        )
        refusal = next(
            ((marker, reason_code) for marker, reason_code in structural_refusals
             if marker in detail),
            None,
        )
        if refusal is None:
            raise
        marker, reason_code = refusal
        if _sha256_file(source) != source_sha256:
            raise RuntimeError(
                "registered closure structural refusal changed the source artifact"
            ) from exc
        return json.dumps({
            "status": "rejected",
            "reason_code": reason_code,
            "message": marker,
            "read_only_after_restore": True,
            "source_checkpoint_reopened": True,
            "shared_vivado_session_untouched": True,
            "worker_mode": "isolated_batch_per_family",
            "word_lsb": word_lsb,
            "word_width": word_width,
        }, sort_keys=True, separators=(",", ":"))
    if "REGISTERED_CLOSURE_EXTRACTED" not in output_text:
        raise RuntimeError(
            "registered closure extractor did not complete; "
            f"vivado_output={output_text[-4000:]}"
        )
    restored = _sha256_file(source) == source_sha256
    if not restored:
        raise RuntimeError("registered closure extraction changed the source artifact")
    try:
        from FDAgents.registered_closure import RegisteredClosure
    except ImportError as exc:
        raise RuntimeError("registered closure runtime is unavailable") from exc
    closure = RegisteredClosure.load(output_dir)
    return json.dumps({
        "status": "success",
        "read_only_after_restore": True,
        "source_checkpoint_reopened": True,
        "shared_vivado_session_untouched": True,
        "worker_mode": "isolated_batch_per_family",
        "node_count": len(closure.records),
        "root_count": len(closure.roots()),
        "max_stage_depth": max_stage_depth,
        "word_lsb": word_lsb,
        "word_width": word_width,
    }, sort_keys=True, separators=(",", ":"))


def synthesize_registered_product_modules(request: dict) -> str:
    """Synthesize generated registered-product RTL out of context."""
    base = {"dcp_path", "source_dcp_sha256", "bundle_path", "work_dir"}
    current_run = base | {"run_id", "run_source_sha256", "clock_name"}
    if (
        not isinstance(request, dict)
        or frozenset(request) not in {frozenset(base), frozenset(current_run)}
    ):
        raise ValueError("registered product synthesis request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    bundle_path = _owned_action_path(
        request["bundle_path"], name="registered product bundle", must_exist=True
    )
    work_dir = _owned_action_path(
        request["work_dir"], name="registered product synthesis", must_exist=False
    )
    if (
        source.is_symlink()
        or not source.is_file()
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
        or work_dir.exists()
    ):
        raise ValueError("registered product synthesis source or work directory is invalid")
    from FDAgents.registered_bridge import (
        render_registered_bridge_verilog,
        render_registered_product_verilog,
        validate_registered_bridge_bundle,
        validate_registered_product_bundle,
    )
    from FDAgents.consumer_rewrite import (
        render_consumer_rewrite_verilog,
        validate_consumer_rewrite_bundle,
    )
    from FDAgents.regarith_tools.production_product_sum import (
        OOC_SYNTHESIS_TOLERANCE_NS,
        authorize_integrated_product_sum_emission,
        execute_ooc_timing_gate,
        render_production_product_sum,
    )
    raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if raw_bundle.get("kind") == "generating_operator_bridge_bundle":
        run_id = str(request.get("run_id") or "")
        run_source_sha256 = str(request.get("run_source_sha256") or "")
        clock_name = str(request.get("clock_name") or "")
        if (
            re.fullmatch(r"nr-[0-9a-f]{32}", run_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", run_source_sha256) is None
            or not clock_name
            or any(token in clock_name for token in "{}\n\r")
        ):
            raise ValueError(
                "current-run registered product synthesis requires run_id and clock"
            )
        bundle = validate_registered_bridge_bundle(raw_bundle)
        render_module = render_registered_bridge_verilog
        # The bridge validator deliberately strips classification-only recovery
        # evidence from its wiring-authority result.  Production IR adaptation
        # must therefore receive the original recovered envelope; it performs
        # its own wiring validation before consulting that read-only metadata.
        emission = render_production_product_sum(
            raw_bundle, source_artifact_sha256=source_sha256
        )
        emitted_modules = {
            row["module_name"]: row for row in emission["modules"]
        }
    elif raw_bundle.get("kind") == "registered_consumer_rewrite_bundle":
        bundle = validate_consumer_rewrite_bundle(raw_bundle)
        render_module = render_consumer_rewrite_verilog
        emission = None
        emitted_modules = {}
    else:
        bundle = validate_registered_product_bundle(raw_bundle)
        render_module = render_registered_product_verilog
        emission = None
        emitted_modules = {}
    work_dir.mkdir()
    module_dcps = {}
    logs = []
    measured_modules = {}
    marker_pattern = re.compile(
        r"REGARITH_OOC_WNS=(?P<wns>-?[0-9.]+)\s+"
        r"REGARITH_OOC_DATA_PATH=(?P<delay>[0-9.]+)\s+"
        r"REGARITH_SOURCE_PERIOD=(?P<period>[0-9.]+)"
    )
    for plan in bundle["plans"]:
        module_name = plan["module_name"]
        rtl = work_dir / f"{module_name}.v"
        output = work_dir / f"{module_name}.dcp"
        script = work_dir / f"{module_name}.tcl"
        log = work_dir / f"{module_name}.log"
        report = work_dir / f"{module_name}.timing.rpt"
        verilog = (
            emitted_modules[module_name]["verilog"]
            if emission is not None else render_module(plan)
        )
        rtl.write_text(verilog, encoding="utf-8")
        script.write_text("\n".join([
            f"open_checkpoint {{{source}}}",
            "set rb_part [get_property PART [current_design]]",
            (
                f"set rb_source_clocks [get_clocks -quiet {{{clock_name}}}]"
                if emission is not None else
                "set rb_source_clocks [get_clocks -quiet]"
            ),
            "if {[llength $rb_source_clocks] != 1} {error {current source clock is not unique}}",
            "set rb_source_period [get_property PERIOD [lindex $rb_source_clocks 0]]",
            "if {![string is double -strict $rb_source_period] || $rb_source_period <= 0.0} {error {current source clock period is invalid}}",
            "close_design",
            f"read_verilog {{{rtl}}}",
            f"synth_design -mode out_of_context -top {module_name} -part $rb_part",
            "create_clock -period $rb_source_period -name regarith_ooc [get_ports clk]",
            "set rb_data_inputs [get_ports -quiet -filter {DIRECTION == IN && NAME != clk}]",
            "if {[llength $rb_data_inputs]} {set_input_delay 0.000 -clock regarith_ooc $rb_data_inputs}",
            "set_output_delay 0.000 -clock regarith_ooc [all_outputs]",
            "opt_design -directive ExploreWithRemap",
            "set rb_path [get_timing_paths -delay_type max -max_paths 1 -nworst 1]",
            "if {[llength $rb_path] != 1} {error {OOC gate found no timed path}}",
            "set rb_wns [get_property SLACK $rb_path]",
            "set rb_delay [get_property DATAPATH_DELAY $rb_path]",
            'puts "REGARITH_OOC_WNS=$rb_wns REGARITH_OOC_DATA_PATH=$rb_delay REGARITH_SOURCE_PERIOD=$rb_source_period"',
            f"report_timing_summary -delay_type min_max -max_paths 50 -file {{{report}}}",
            f"write_checkpoint -force {{{output}}}",
            f"write_edif -force {{{output.with_suffix('.edf')}}}",
            "exit", "",
        ]), encoding="utf-8")
        completed = subprocess.run(
            [get_vivado_path(), "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600.0,
            cwd=str(work_dir),
        )
        log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        text = (completed.stdout or "") + (completed.stderr or "")
        marker = marker_pattern.search(text)
        if (
            completed.returncode != 0
            or not output.is_file()
            or not report.is_file()
            or marker is None
        ):
            raise RuntimeError(
                "registered product module synthesis failed: "
                + ((completed.stderr or completed.stdout or "")[-3000:])
            )
        module_dcps[module_name] = str(output)
        logs.append(str(log))
        if emission is not None:
            measured_modules[module_name] = {
                "setup_wns_ns": float(marker.group("wns")),
                "internal_data_path_ns": float(marker.group("delay")),
                "unconstrained_internal_path_count": 0,
                "ooc_dcp_sha256": _sha256_file(output),
                "timing_report_sha256": _sha256_file(report),
                "source_clock_period_ns": float(marker.group("period")),
            }
    result = {
        "status": "success",
        "module_dcps": module_dcps,
        "module_count": len(module_dcps),
        "logs": logs,
    }
    if emission is not None:
        periods = {
            float(row["source_clock_period_ns"])
            for row in measured_modules.values()
        }
        if len(periods) != 1:
            raise RuntimeError("registered product source clock period changed across modules")
        source_period_ns = periods.pop()
        gate = execute_ooc_timing_gate(
            emission,
            lambda module, _period: measured_modules[module["module_name"]],
            period_ns=source_period_ns,
        )
        authorized = authorize_integrated_product_sum_emission(emission, gate)
        module_rows = [
            {
                "module_name": name,
                "rtl_sha256": _sha256_file(work_dir / f"{name}.v"),
                "ooc_dcp_sha256": _sha256_file(Path(module_dcps[name])),
                "ooc_edif_sha256": _sha256_file(Path(module_dcps[name]).with_suffix(".edf")),
                "timing_report_sha256": measured_modules[name]["timing_report_sha256"],
            }
            for name in sorted(module_dcps)
        ]
        generation_body = {
            "schema_version": "regarith-current-run-generation-binding-v1",
            "status": "passed",
            "run_id": run_id,
            "run_source_sha256": run_source_sha256,
            "source_artifact_sha256": source_sha256,
            "bundle_file_sha256": _sha256_file(bundle_path),
            "bundle_digest": emission["bundle_digest"],
            "emission_result_digest": emission["result_digest"],
            "source_clock_name": clock_name,
            "source_clock_period_ns": source_period_ns,
            "internal_data_path_ns_max": (
                source_period_ns + OOC_SYNTHESIS_TOLERANCE_NS
            ),
            "ooc_gate_digest": gate["gate_digest"],
            "emission_authorization_digest": authorized["authorization_digest"],
            "modules": module_rows,
            "dependencies_created_in_current_run": True,
        }
        generation_binding = {
            **generation_body,
            "binding_digest": _stable_digest(generation_body),
        }
        result.update({
            "emission_result_digest": emission["result_digest"],
            "ooc_gate": gate,
            "authorized_emission": authorized,
            "generation_binding": generation_binding,
        })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _render_registered_product_relative_placement_tcl(manifest: dict) -> list[str]:
    """Render runtime-anchored relative geometry with fail-closed cleanup."""
    from FDAgents.relative_placement import (
        validate_product_sum_relative_placement_manifest,
    )

    checked = validate_product_sum_relative_placement_manifest(manifest)
    rules = checked["geometry_asset"]["modules"]
    lines = [
        "proc rb_pg_grid_distance {site anchor} {",
        "  set st [lindex [get_tiles -quiet -of_objects [get_sites -quiet $site]] 0]",
        "  set at [lindex [get_tiles -quiet -of_objects [get_sites -quiet $anchor]] 0]",
        "  if {$st eq {} || $at eq {}} {return 1000000000}",
        "  set sx [get_property GRID_POINT_X $st]; set sy [get_property GRID_POINT_Y $st]",
        "  set ax [get_property GRID_POINT_X $at]; set ay [get_property GRID_POINT_Y $at]",
        "  return [expr {abs($sx-$ax) + abs($sy-$ay)}]",
        "}",
        "set rb_pg_status applied",
        "set rb_pg_reason {}",
        "set rb_pg_all_cells {}",
        "set rb_pg_pblocks {}",
        "set rb_pg_used [dict create]",
        "if {![info exists rb_pg_seed_occupied]} {error {RELATIVE_PLACEMENT_SEED_OCCUPANCY_UNAVAILABLE}}",
        "set rb_pg_occupied $rb_pg_seed_occupied",
        "if {[catch {",
    ]
    for index, binding in enumerate(checked["runtime_bindings"]):
        name = binding["instance_name"]
        rule = rules[binding["rule_id"]]
        pair_count = int(rule["dsp_pairs"]["pair_count"])
        chain_length = int(rule["compressor_chain"]["length"])
        radius_x = int(rule["register_logic_cluster"]["slice_radius_x"])
        radius_y = int(rule["register_logic_cluster"]["slice_radius_y"])
        prefix = f"rb_pg_{index}"
        lines.extend([
            f"set {prefix}_cells [get_cells -quiet {{{name}/*}}]",
            f"if {{![llength ${prefix}_cells]}} {{error {{RELATIVE_PLACEMENT_CELL_INVENTORY_MISSING}}}}",
            f"lappend rb_pg_all_cells {{*}}${prefix}_cells",
            f"set {prefix}_raw_dsps [filter ${prefix}_cells {{REF_NAME == DSP48E2}}]",
            f"set {prefix}_dsp_groups [dict create]",
            f"foreach rb_pg_dsp ${prefix}_raw_dsps {{",
            "  set rb_pg_dsp_name [get_property NAME $rb_pg_dsp]",
            "  if {[regexp {/product_q_([0-9]+)_reg$} $rb_pg_dsp_name -> rb_pg_term]} {",
            f"    dict set {prefix}_dsp_groups [expr {{int($rb_pg_term)}}] registered $rb_pg_dsp",
            "  } elseif {[regexp {/product_q_([0-9]+)0$} $rb_pg_dsp_name -> rb_pg_term]} {",
            f"    dict set {prefix}_dsp_groups [expr {{int($rb_pg_term)}}] secondary $rb_pg_dsp",
            "  } else {error {RELATIVE_PLACEMENT_DSP_TERM_IDENTITY_UNAVAILABLE}}",
            "}",
            f"set {prefix}_dsps {{}}",
            f"for {{set rb_pg_term 0}} {{$rb_pg_term < {pair_count}}} {{incr rb_pg_term}} {{",
            f"  if {{![dict exists ${prefix}_dsp_groups $rb_pg_term secondary] || ![dict exists ${prefix}_dsp_groups $rb_pg_term registered]}} {{error {{RELATIVE_PLACEMENT_DSP_TERM_PAIR_INCOMPLETE}}}}",
            f"  lappend {prefix}_dsps [dict get ${prefix}_dsp_groups $rb_pg_term secondary] [dict get ${prefix}_dsp_groups $rb_pg_term registered]",
            "}",
            f"set {prefix}_carries [lsort -dictionary [filter ${prefix}_cells {{REF_NAME == CARRY8}}]]",
            f"if {{[llength ${prefix}_dsps] != {pair_count * 2} || [llength ${prefix}_carries] != {chain_length}}} {{error {{RELATIVE_PLACEMENT_CELL_INVENTORY_MISMATCH}}}}",
            f"set {prefix}_outpins [get_pins -quiet {{{name}/y[*]}}]",
            f"set {prefix}_outnets [get_nets -quiet -segments -of_objects ${prefix}_outpins]",
            f"set {prefix}_loads [get_pins -quiet -leaf -of_objects ${prefix}_outnets -filter {{DIRECTION == IN}}]",
            f"set {prefix}_consumers [get_cells -quiet -of_objects ${prefix}_loads]",
            f"set {prefix}_anchors [lsort -unique [get_property LOC ${prefix}_consumers]]",
            f"set {prefix}_anchors [lsearch -all -inline -not -exact ${prefix}_anchors {{}}]",
            f"if {{![llength ${prefix}_anchors]}} {{error {{RELATIVE_PLACEMENT_CONSUMER_ANCHOR_UNAVAILABLE}}}}",
            f"set {prefix}_anchor [lindex ${prefix}_anchors 0]",
            f"set {prefix}_region [lindex [get_clock_regions -quiet -of_objects [get_sites -quiet ${prefix}_anchor]] 0]",
            f"if {{${prefix}_region eq {{}}}} {{error {{RELATIVE_PLACEMENT_CONSUMER_REGION_UNAVAILABLE}}}}",
            f"set {prefix}_pairs {{}}",
            f"if {{{pair_count} == 0}} {{set {prefix}_pairs {{}}}} else {{",
            f"foreach rb_pg_lower [lsort [get_sites -quiet -of_objects ${prefix}_region -filter {{SITE_TYPE == DSP48E2}}]] {{",
            "  if {![regexp {^DSP48E2_X([0-9]+)Y([0-9]+)$} $rb_pg_lower -> rb_pg_x rb_pg_y]} {continue}",
            "  set rb_pg_upper [format {DSP48E2_X%dY%d} $rb_pg_x [expr {$rb_pg_y + 1}]]",
            "  if {![llength [get_sites -quiet $rb_pg_upper]]} {continue}",
            "  if {[dict exists $rb_pg_used $rb_pg_lower] || [dict exists $rb_pg_used $rb_pg_upper]} {continue}",
            "  if {[dict exists $rb_pg_occupied $rb_pg_lower] || [dict exists $rb_pg_occupied $rb_pg_upper] || [llength [get_cells -quiet -of_objects [get_sites -quiet $rb_pg_lower]]] || [llength [get_cells -quiet -of_objects [get_sites -quiet $rb_pg_upper]]]} {continue}",
            f"  set rb_pg_dist [expr {{[rb_pg_grid_distance $rb_pg_lower ${prefix}_anchor] + [rb_pg_grid_distance $rb_pg_upper ${prefix}_anchor]}}]",
            f"  lappend {prefix}_pairs [list $rb_pg_dist $rb_pg_lower $rb_pg_upper]",
            "}",
            "}",
            f"set {prefix}_selected_pairs {{}}",
            f"foreach rb_pg_pair [lsort -integer -index 0 ${prefix}_pairs] {{",
            "  set rb_pg_lower [lindex $rb_pg_pair 1]; set rb_pg_upper [lindex $rb_pg_pair 2]",
            "  if {[dict exists $rb_pg_used $rb_pg_lower] || [dict exists $rb_pg_used $rb_pg_upper]} {continue}",
            f"  lappend {prefix}_selected_pairs $rb_pg_pair",
            "  dict set rb_pg_used $rb_pg_lower 1; dict set rb_pg_used $rb_pg_upper 1",
            f"  if {{[llength ${prefix}_selected_pairs] == {pair_count}}} {{break}}",
            "}",
            f"if {{[llength ${prefix}_selected_pairs] != {pair_count}}} {{error {{RELATIVE_PLACEMENT_DSP_PAIRS_UNAVAILABLE}}}}",
            f"puts \"REGISTERED_PRODUCT_PLACEMENT_PLAN={name}|dsps=${prefix}_dsps|pairs=${prefix}_selected_pairs\"",
            f"set {prefix}_chains {{}}",
            f"foreach rb_pg_lower [lsort [get_sites -quiet -of_objects ${prefix}_region -filter {{SITE_TYPE =~ SLICE*}}]] {{",
            "  if {![regexp {^SLICE_X([0-9]+)Y([0-9]+)$} $rb_pg_lower -> rb_pg_x rb_pg_y]} {continue}",
            "  set rb_pg_sites {}; set rb_pg_ok 1; set rb_pg_dist 0",
            f"  for {{set rb_pg_i 0}} {{$rb_pg_i < {chain_length}}} {{incr rb_pg_i}} {{",
            "    set rb_pg_site [format {SLICE_X%dY%d} $rb_pg_x [expr {$rb_pg_y + $rb_pg_i}]]",
            "    if {![llength [get_sites -quiet $rb_pg_site]] || [dict exists $rb_pg_used $rb_pg_site] || [dict exists $rb_pg_occupied $rb_pg_site] || [llength [get_cells -quiet -of_objects [get_sites -quiet $rb_pg_site]]]} {set rb_pg_ok 0; break}",
            f"    lappend rb_pg_sites $rb_pg_site; incr rb_pg_dist [rb_pg_grid_distance $rb_pg_site ${prefix}_anchor]",
            "  }",
            f"  if {{$rb_pg_ok}} {{lappend {prefix}_chains [list $rb_pg_dist $rb_pg_sites]}}",
            "}",
            f"if {{![llength ${prefix}_chains]}} {{error {{RELATIVE_PLACEMENT_COMPRESSOR_CHAIN_UNAVAILABLE}}}}",
            f"set {prefix}_chain [lindex [lsort -integer -index 0 ${prefix}_chains] 0]",
            f"set {prefix}_chain_sites [lindex ${prefix}_chain 1]",
            f"foreach rb_pg_site ${prefix}_chain_sites {{dict set rb_pg_used $rb_pg_site 1}}",
            f"for {{set rb_pg_i 0}} {{$rb_pg_i < {pair_count}}} {{incr rb_pg_i}} {{",
            f"  set rb_pg_pair [lindex ${prefix}_selected_pairs $rb_pg_i]",
            f"  set_property LOC [lindex $rb_pg_pair 1] [lindex ${prefix}_dsps [expr {{$rb_pg_i * 2}}]]",
            f"  set_property LOC [lindex $rb_pg_pair 2] [lindex ${prefix}_dsps [expr {{$rb_pg_i * 2 + 1}}]]",
            "}",
            f"for {{set rb_pg_i 0}} {{$rb_pg_i < {chain_length}}} {{incr rb_pg_i}} {{",
            f"  set_property LOC [lindex ${prefix}_chain_sites $rb_pg_i] [lindex ${prefix}_carries $rb_pg_i]",
            "}",
            f"set {prefix}_pb [create_pblock rb_relative_geometry_{index}]",
            f"lappend rb_pg_pblocks ${prefix}_pb",
            f"add_cells_to_pblock ${prefix}_pb ${prefix}_cells",
            f"regexp {{^SLICE_X([0-9]+)Y([0-9]+)$}} [lindex ${prefix}_chain_sites 0] -> {prefix}_sx {prefix}_sy",
            f"set {prefix}_xmin [expr {{max(0, ${prefix}_sx - {radius_x})}}]",
            f"set {prefix}_ymin [expr {{max(0, ${prefix}_sy - {radius_y})}}]",
            f"set {prefix}_xmax [expr {{${prefix}_sx + {radius_x}}}]",
            f"set {prefix}_ymax [expr {{${prefix}_sy + {chain_length - 1 + radius_y}}}]",
            f"resize_pblock ${prefix}_pb -add [format {{SLICE_X%dY%d:SLICE_X%dY%d}} ${prefix}_xmin ${prefix}_ymin ${prefix}_xmax ${prefix}_ymax]",
            f"foreach rb_pg_pair ${prefix}_selected_pairs {{",
            f"  resize_pblock ${prefix}_pb -add [format {{%s:%s}} [lindex $rb_pg_pair 1] [lindex $rb_pg_pair 1]]",
            f"  resize_pblock ${prefix}_pb -add [format {{%s:%s}} [lindex $rb_pg_pair 2] [lindex $rb_pg_pair 2]]",
            "}",
        ])
    lines.extend([
        "} rb_pg_error]} {",
        "  set rb_pg_status degraded",
        "  set rb_pg_reason $rb_pg_error",
        "  foreach rb_pg_cell $rb_pg_all_cells {catch {reset_property LOC $rb_pg_cell}; catch {reset_property BEL $rb_pg_cell}}",
        "  foreach rb_pg_pb $rb_pg_pblocks {catch {delete_pblocks $rb_pg_pb}}",
        "  place_design -unplace",
        "}",
        f"puts \"REGISTERED_PRODUCT_PLACEMENT_BINDING=$rb_pg_status|{checked['manifest_digest']}|$rb_pg_reason\"",
    ])
    return lines


def route_registered_product_bundle(request: dict) -> str:
    """Wire, remap, place and route one recovered registered-product bundle."""
    global _design_open, _command_pending
    legacy_fields = {
        "inserted_dcp", "source_dcp", "source_dcp_sha256", "bundle_path",
        "output_dcp", "route_timeout_s", "physical_profile",
    }
    if isinstance(request, dict) and set(request) == legacy_fields:
        # Consumer bundles are authorized by their exact current-seed contract,
        # not by the product-emission receipt used by the newer DSP backend.
        return _legacy_route_registered_product_bundle_v0(request)
    required = {
        "inserted_dcp", "source_dcp", "source_dcp_sha256", "bundle_path",
        "output_dcp", "route_timeout_s", "physical_profile",
        "run_id", "run_source_sha256", "current_run_authorization_path",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered product route request has an unsupported schema")
    inserted = _owned_action_path(
        request["inserted_dcp"], name="registered product inserted DCP", must_exist=True
    )
    source = Path(str(request["source_dcp"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    bundle_path = _owned_action_path(
        request["bundle_path"], name="registered product bundle", must_exist=True
    )
    current_run_authorization_path = _owned_action_path(
        request["current_run_authorization_path"],
        name="current-run generated-module authorization",
        must_exist=True,
    )
    run_id = str(request["run_id"])
    run_source_sha256 = str(request["run_source_sha256"])
    output = _owned_action_path(
        request["output_dcp"], name="registered product output", must_exist=False
    )
    timeout = float(request["route_timeout_s"])
    profile = str(request["physical_profile"])
    directives = {
        "compact_timing": (
            "ExtraTimingOpt", ["catch {phys_opt_design -directive AggressiveExplore} rb_phys_message"],
            "NoTimingRelaxation", [],
        ),
        "net_delay_focus": (
            "ExtraNetDelay_high", ["catch {phys_opt_design -directive AggressiveExplore} rb_phys_message"],
            "NoTimingRelaxation", [],
        ),
        "full_timing": (
            "ExtraTimingOpt", [
                "phys_opt_design -directive AggressiveExplore",
                "phys_opt_design -fanout_opt",
            ], "NoTimingRelaxation", ["phys_opt_design -directive AggressiveExplore"],
        ),
        "semantic_rewrite": (
            "Default", [], "NoTimingRelaxation", [],
        ),
    }
    if (
        source.is_symlink()
        or not source.is_file()
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
        or re.fullmatch(r"nr-[0-9a-f]{32}", run_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", run_source_sha256) is None
        or output.exists()
        or not 1 <= timeout <= 3600
        or profile not in directives
    ):
        raise ValueError("registered product route request is invalid")
    from FDAgents.registered_bridge import (
        render_registered_bridge_connect_tcl,
        render_registered_product_connect_tcl,
        validate_registered_bridge_bundle,
        validate_registered_product_bundle,
    )
    from FDAgents.readable_edif import (
        finalize_readable_edif_sidecar,
        render_readable_edif_sidecar_tcl,
    )
    raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if raw_bundle.get("kind") == "generating_operator_bridge_bundle":
        bundle = validate_registered_bridge_bundle(raw_bundle)
        connect = render_registered_bridge_connect_tcl(bundle)
    else:
        bundle = validate_registered_product_bundle(raw_bundle)
        connect = render_registered_product_connect_tcl(bundle)
    current_run_authorization = json.loads(
        current_run_authorization_path.read_text(encoding="utf-8")
    )
    raw_authorization = dict(current_run_authorization)
    authorization_digest = raw_authorization.pop("authorization_digest", None)
    generation_binding = current_run_authorization.get("generation_binding")
    raw_generation = dict(generation_binding or {})
    generation_digest = raw_generation.pop("binding_digest", None)
    if (
        current_run_authorization.get("schema_version")
        != "regarith-current-run-generated-route-authorization-v1"
        or current_run_authorization.get("status") != "authorized"
        or current_run_authorization.get("mutation_allowed") is not True
        or current_run_authorization.get("authority")
        != "current_run_generated_rtl_ooc_and_exact_insertion_v1"
        or current_run_authorization.get("run_id") != run_id
        or current_run_authorization.get("run_source_sha256") != run_source_sha256
        or current_run_authorization.get("source_artifact_sha256") != source_sha256
        or current_run_authorization.get("bundle_file_sha256") != _sha256_file(bundle_path)
        or current_run_authorization.get("bundle_digest") != _stable_digest(bundle)
        or current_run_authorization.get("inserted_dcp_sha256") != _sha256_file(inserted)
        or not isinstance(authorization_digest, str)
        or _stable_digest(raw_authorization) != authorization_digest
        or not isinstance(generation_binding, dict)
        or generation_binding.get("schema_version")
        != "regarith-current-run-generation-binding-v1"
        or generation_binding.get("run_id") != run_id
        or generation_binding.get("run_source_sha256") != run_source_sha256
        or generation_binding.get("source_artifact_sha256") != source_sha256
        or generation_binding.get("bundle_file_sha256") != _sha256_file(bundle_path)
        or generation_binding.get("bundle_digest") != _stable_digest(bundle)
        or current_run_authorization.get("emission_result_digest")
        != generation_binding.get("emission_result_digest")
        or current_run_authorization.get("emission_authorization_digest")
        != generation_binding.get("emission_authorization_digest")
        or float(current_run_authorization.get("period_ns") or -1.0)
        != float(generation_binding.get("source_clock_period_ns") or -2.0)
        or float(current_run_authorization.get("internal_data_path_ns_max") or -1.0)
        != float(generation_binding.get("internal_data_path_ns_max") or -2.0)
        or generation_binding.get("status") != "passed"
        or generation_binding.get("dependencies_created_in_current_run") is not True
        or not isinstance(generation_digest, str)
        or _stable_digest(raw_generation) != generation_digest
        or not isinstance(current_run_authorization.get("instance_inventory"), list)
    ):
        raise RuntimeError("current-run generated-module authorization is stale or malformed")
    relative_placement = current_run_authorization.get("relative_placement_manifest")
    relative_placement_tcl = _render_registered_product_relative_placement_tcl(
        relative_placement
    )
    authorized_instances = {
        str(row.get("instance_name") or ""): row
        for row in current_run_authorization["instance_inventory"]
        if isinstance(row, dict)
    }
    if set(authorized_instances) != {
        str(instance["name"]) for instance in bundle["instances"]
    }:
        raise RuntimeError("current-run generated-module instance coverage differs")
    if not connect.endswith("exit\n"):
        raise RuntimeError("registered product connect script terminator is invalid")
    connect = connect[:-5]
    place_directive, pre_route_phys, route_directive, post_route_phys = directives[profile]
    topology_checks = []
    generated_class_checks = []
    generated_timing_checks = []
    for instance in bundle["instances"]:
        name = str(instance["name"])
        authorized_row = authorized_instances[name]
        if authorized_row.get("module_name") != str(instance["module_name"]):
            raise RuntimeError("current-run generated-module binding differs")
        # Vivado's -hierarchical option matches leaf names, not a full
        # hierarchical path pattern.  Supplying both -hier and "inst/*"
        # therefore returns an empty collection even when the hierarchy is
        # present.  A path pattern without -hier selects the descendants.
        generated_class_checks.append(
            f"set rb_generated_cells [get_cells -quiet {{{name}/*}}]"
        )
        generated_class_checks.append(
            f"if {{![llength $rb_generated_cells]}} {{error {{{name} generated hierarchy is empty}}}}"
        )
        generated_class_checks.extend([
            "set rb_generated_seq [filter $rb_generated_cells {REF_NAME =~ FD* || REF_NAME == DSP48E2}]",
            f"if {{![llength $rb_generated_seq]}} {{error {{{name} generated module has no registered stage}}}}",
            "set rb_generated_dsps [filter $rb_generated_cells {REF_NAME == DSP48E2}]",
            "foreach rb_generated_dsp $rb_generated_dsps {",
            "  if {[get_property USE_MULT $rb_generated_dsp] ne {MULTIPLY}} {error {generated DSP is not a multiplier}}",
            "  set rb_generated_areg [get_property AREG $rb_generated_dsp]",
            "  set rb_generated_breg [get_property BREG $rb_generated_dsp]",
            "  set rb_generated_preg [get_property PREG $rb_generated_dsp]",
            "  if {$rb_generated_areg < 1 && $rb_generated_breg < 1 && $rb_generated_preg < 1} {error {generated DSP has no registered stage mapping}}",
            "}",
            "foreach rb_generated_net [get_nets -quiet -of_objects [get_pins -quiet -of_objects $rb_generated_dsps -filter {DIRECTION == OUT}]] {",
            "  set rb_generated_drivers [get_pins -quiet -leaf -of_objects $rb_generated_net -filter {DIRECTION == OUT && REF_NAME == DSP48E2}]",
            "  set rb_generated_loads [get_pins -quiet -leaf -of_objects $rb_generated_net -filter {DIRECTION == IN && REF_NAME == DSP48E2}]",
            "  if {[llength $rb_generated_drivers] && [llength $rb_generated_loads]} {",
            "    foreach rb_generated_driver $rb_generated_drivers {",
            "      set rb_generated_refpin [get_property REF_PIN_NAME $rb_generated_driver]",
            "      set rb_generated_source [lindex [get_cells -quiet -of_objects $rb_generated_driver] 0]",
            "      if {![string match {PCOUT*} $rb_generated_refpin] && [get_property PREG $rb_generated_source] < 1} {error {unregistered non-cascade DSP-to-DSP edge}}",
            "    }",
            "  }",
            "}",
            f"puts REGISTERED_PRODUCT_GENERATED_CLASS={name}",
        ])
        generated_timing_checks.extend([
            f"set rb_generated_seq [filter [get_cells -quiet {{{name}/*}}] {{REF_NAME =~ FD* || REF_NAME == DSP48E2}}]",
            "set rb_generated_path [get_timing_paths -quiet -from $rb_generated_seq -to $rb_generated_seq -delay_type max -max_paths 1 -nworst 1]",
            f"if {{[llength $rb_generated_path] != 1}} {{error {{{name} generated timing path is unavailable}}}}",
            "set rb_generated_delay [get_property DATAPATH_DELAY $rb_generated_path]",
            f"if {{$rb_generated_delay > {float(current_run_authorization['internal_data_path_ns_max']):.6f}}} {{error {{{name} generated timing gate failed}}}}",
            f"puts \"REGISTERED_PRODUCT_GENERATED_TIMING={name}|$rb_generated_delay\"",
        ])
        topology_checks.append(
            f"rb_one [get_cells -quiet {{{name}}}] {{{name} inserted instance}}"
        )
        for bit, net in enumerate(instance["output_nets"]):
            pin = f"{name}/y[{bit}]"
            topology_checks.extend([
                f"set rb_check_net [rb_one [get_nets -quiet {{{net}}}] {{{pin} output net}}]",
                "set rb_check_segments [get_nets -quiet -segments $rb_check_net]",
                "set rb_check_drivers [get_pins -quiet -leaf -of_objects $rb_check_segments -filter {DIRECTION == OUT}]",
                f"if {{[llength $rb_check_drivers] != 1 || ![string match {{{name}/*}} [get_property NAME [lindex $rb_check_drivers 0]]]}} {{error {{{pin} is not uniquely driven from the inserted module after sweep}}}}",
            ])
    script = connect + "\n" + "\n".join([
        f"open_checkpoint {{{output}}}",
        "set_param general.maxThreads 8",
        *generated_class_checks,
        "puts REGISTERED_PRODUCT_CURRENT_RUN_GENERATION_VERIFIED",
        "set rb_pg_seed_occupied [dict create]",
        "foreach rb_pg_site [get_sites -quiet -filter {IS_USED == 1}] {dict set rb_pg_seed_occupied $rb_pg_site 1}",
        "opt_design -directive ExploreWithRemap",
        *generated_timing_checks,
        "puts REGISTERED_PRODUCT_CURRENT_RUN_OOC_TIMING_VERIFIED",
        *relative_placement_tcl,
        # The inserted product-sum module changes the arithmetic depth of the
        # cone it replaces, so the incumbent routing of the parent no longer
        # reflects the new logical netlist.  Unroute before placing so the
        # whole design is re-placed and re-routed against the rewritten cone
        # rather than patched around retained routes.
        "route_design -unroute",
        f"place_design -directive {place_directive}",
        *pre_route_phys,
        f"route_design -directive {route_directive}",
        *post_route_phys,
        *topology_checks,
        "puts REGISTERED_PRODUCT_TOPOLOGY_VERIFIED",
        f"write_checkpoint -force {{{output}}}",
        *render_readable_edif_sidecar_tcl(
            output, open_checkpoint=False, close_design=False
        ),
        "puts REGISTERED_PRODUCT_ROUTE_DONE",
        "close_design", "",
    ])
    failure = None
    candidate_sha256 = None
    route_ok = False
    output_text = ""
    try:
        close_current_design()
        argv = "set argv [list " + " ".join(
            _tcl_utf8_value(value) for value in (str(inserted), str(output))
        ) + "]\nset argc [llength $argv]\n"
        output_text = run_tcl_script(argv + script, timeout=timeout + 900.0)
        _design_open = False
        if (
            "REGISTERED_PRODUCT_ROUTE_DONE" not in output_text
            or "REGISTERED_PRODUCT_TOPOLOGY_VERIFIED" not in output_text
            or "REGISTERED_PRODUCT_CURRENT_RUN_GENERATION_VERIFIED" not in output_text
            or "REGISTERED_PRODUCT_CURRENT_RUN_OOC_TIMING_VERIFIED" not in output_text
            or "REGISTERED_PRODUCT_READABLE_EDIF_WRITTEN" not in output_text
            or not output.is_file()
        ):
            error_lines = [
                line.strip() for line in output_text.splitlines()
                if line.strip().startswith(("ERROR:", "Error:", "invalid command name"))
            ]
            error_summary = error_lines[-1] if error_lines else "no explicit Vivado error line"
            raise RuntimeError(
                "registered product route did not produce a checkpoint; "
                f"vivado_error={error_summary}; "
                f"vivado_output={output_text[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        readable_edif = finalize_readable_edif_sidecar(output)
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        route_report = run_tcl_command("report_route_status -return_string", timeout=180)
        errors = re.search(r"# of nets with routing errors[.\s:]+(\d+)", route_report, re.I)
        route_ok = errors is not None and int(errors.group(1)) == 0
        if not route_ok:
            raise RuntimeError("registered product candidate is not fully routed")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _command_pending:
                restart_vivado_process()
            else:
                run_tcl_command("catch {close_design}", timeout=30)
            _design_open = False
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or candidate_sha256 is None:
        output.unlink(missing_ok=True)
        shutil.rmtree(output.parent / f"{output.name}.edf", ignore_errors=True)
        raise RuntimeError(f"registered product route failed: {failure}") from failure
    checks = {
        "current_run_source_and_generation_bound": True,
        "registered_bundle_wiring_verified": True,
        "complete_old_driver_disconnection_verified": True,
        "exact_module_inventory": True,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "current_run_generated_module_structure_verified": True,
        "current_run_ooc_timing_gate_verified": True,
        "readable_edif_sidecar_written": True,
    }
    placement_match = re.search(
        r"REGISTERED_PRODUCT_PLACEMENT_BINDING=(applied|degraded)\|([0-9a-f]{64})\|([^\r\n]*)",
        output_text,
    )
    if (
        placement_match is None
        or placement_match.group(2) != relative_placement.get("manifest_digest")
    ):
        output.unlink(missing_ok=True)
        raise RuntimeError("relative placement binding receipt is absent or stale")
    placement_status = placement_match.group(1)
    placement_reason = placement_match.group(3).strip()
    runtime_plan_rows = sorted(re.findall(
        r"REGISTERED_PRODUCT_PLACEMENT_PLAN=([^\r\n]+)", output_text
    ))
    if placement_status == "applied" and len(runtime_plan_rows) != len(
        relative_placement["runtime_bindings"]
    ):
        output.unlink(missing_ok=True)
        raise RuntimeError("relative placement runtime plan coverage differs")
    placement_receipt_body = {
        "schema_version": "regarith-relative-placement-apply-v1",
        "status": placement_status,
        "mode": (
            "relative_geometry_guided"
            if placement_status == "applied"
            else "unguided_global_placement"
        ),
        "reason_code": placement_reason,
        "manifest_digest": relative_placement["manifest_digest"],
        "geometry_asset_digest": relative_placement["geometry_asset"]["asset_digest"],
        "runtime_binding_count": (
            len(runtime_plan_rows) if placement_status == "applied" else 0
        ),
        "runtime_plan_digest": _stable_digest(
            runtime_plan_rows if placement_status == "applied" else []
        ),
        "apply_time_site_legality_rechecked": placement_status == "applied",
        "absolute_sites_stored_in_certificate": False,
    }
    placement_receipt = {
        **placement_receipt_body,
        "receipt_digest": _stable_digest(placement_receipt_body),
    }
    checks["relative_placement_binding_applied_or_typed_degraded"] = True
    timing_rows = []
    for name, delay in re.findall(
        r"REGISTERED_PRODUCT_GENERATED_TIMING=([^|\r\n]+)\|([0-9.]+)",
        output_text,
    ):
        timing_rows.append({"instance_name": name, "internal_data_path_ns": float(delay)})
    if {row["instance_name"] for row in timing_rows} != set(authorized_instances):
        output.unlink(missing_ok=True)
        raise RuntimeError("current-run generated timing coverage differs")
    generated_gate_body = {
        "schema_version": "regarith-current-run-generated-module-gate-v1",
        "status": "passed",
        "authorization_digest": authorization_digest,
        "run_id": run_id,
        "source_artifact_sha256": source_sha256,
        "generation_binding_digest": generation_digest,
        "period_ns": float(current_run_authorization["period_ns"]),
        "internal_data_path_ns_max": float(
            current_run_authorization["internal_data_path_ns_max"]
        ),
        "timing_rows": sorted(timing_rows, key=lambda row: row["instance_name"]),
        "registered_stage_mapping_passed": True,
        "dsp_edge_class_passed": True,
    }
    generated_gate = {
        **generated_gate_body,
        "gate_digest": _stable_digest(generated_gate_body),
    }
    topology_manifest = {
        "schema_version": "registered-product-emitted-topology-v1",
        "source_dcp_sha256": source_sha256,
        "candidate_dcp_sha256": candidate_sha256,
        "recovered_bundle_digest": _stable_digest(raw_bundle),
        "instances": [
            {
                "name": str(instance["name"]),
                "module_name": str(instance["module_name"]),
                "output_bit_count": len(instance["output_nets"]),
            }
            for instance in bundle["instances"]
        ],
        "module_names": sorted(str(plan["module_name"]) for plan in bundle["plans"]),
        "checks": checks,
        "current_run_generated_module_gate": generated_gate,
        "readable_edif": readable_edif,
    }
    # The receipt already binds the routed candidate SHA independently.  Keep
    # the emitted-topology digest semantic so flag-on/flag-off executions on
    # the same source and recovered bundle compare equal despite ordinary DCP
    # archive/route serialization variance.  Retain the artifact-bound digest
    # as separate evidence rather than conflating it with scheduler semantics.
    semantic_topology_manifest = {
        key: value
        for key, value in topology_manifest.items()
        if key not in {"candidate_dcp_sha256", "readable_edif"}
    }
    emitted_topology_digest = _stable_digest(semantic_topology_manifest)
    emitted_topology_artifact_digest = _stable_digest(topology_manifest)
    return json.dumps({
        "status": "success",
        "candidate": {"dcp_sha256": candidate_sha256},
        "source_checkpoint_reopened": True,
        "candidate_checkpoint_reopened": True,
        "relevant_nets_fully_routed": route_ok,
        "instance_count": len(bundle["instances"]),
        "module_count": len(bundle["plans"]),
        "profile": profile,
        "emitted_topology_digest": emitted_topology_digest,
        "emitted_topology_artifact_digest": emitted_topology_artifact_digest,
        "emitted_topology_manifest": topology_manifest,
        "checks": checks,
        "current_run_generated_module_gate": generated_gate,
        "placement_binding_receipt": placement_receipt,
    }, sort_keys=True, separators=(",", ":"))


def _legacy_registered_closure_extraction_v0(request: dict) -> str:
    """Apply one fixed recurrence ECO transaction and restore the exact source."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "target_stem", "output_dir",
        "max_stage_depth", "word_width",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered closure extraction request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    target_stem = str(request["target_stem"])
    max_stage_depth = request["max_stage_depth"]
    word_width = request["word_width"]
    output_dir = _owned_action_path(
        request["output_dir"], name="registered closure output", must_exist=False
    )
    if (
        source.is_symlink()
        or not source.is_file()
        or _sha256_file(source) != source_sha256
        or not target_stem
        or isinstance(max_stage_depth, bool)
        or not isinstance(max_stage_depth, int)
        or not 1 <= max_stage_depth <= 8
        or isinstance(word_width, bool)
        or not isinstance(word_width, int)
        or not 2 <= word_width <= 1024
        or (output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()))
    ):
        raise ValueError("registered closure source or output is invalid")
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "extract_registered_closure.tcl"
    ).read_text(encoding="utf-8")
    argv = "set argv [list " + " ".join(
        _tcl_utf8_value(value)
        for value in (
            str(source), target_stem, str(output_dir), max_stage_depth,
            word_width,
        )
    ) + "]\n"
    failure = None
    restored = False
    output_text = ""
    try:
        close_current_design()
        output_text = run_tcl_script(argv + script, timeout=1200.0)
        _design_open = False
        if "REGISTERED_CLOSURE_EXTRACTED" not in output_text:
            raise RuntimeError(
                "registered closure extractor did not complete; "
                "completion_diagnostic="
                f"{tcl_completion_diagnostic(output_text)}; "
                f"vivado_output={output_text[-4000:]}"
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _command_pending:
                restart_vivado_process()
            else:
                run_tcl_command("catch {close_design}", timeout=30)
            _design_open = False
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = _sha256_file(source) == source_sha256
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or not restored:
        raise RuntimeError(f"registered closure extraction failed: {failure}") from failure
    try:
        from FDAgents.registered_closure import RegisteredClosure
    except ImportError as exc:
        raise RuntimeError("registered closure runtime is unavailable") from exc
    logger.info("Loading completed registered closure from %s", output_dir)
    closure = RegisteredClosure.load(output_dir)
    logger.info(
        "Loaded registered closure nodes=%d roots=%d",
        len(closure.records), len(closure.roots()),
    )
    return json.dumps({
        "status": "success",
        "read_only_after_restore": True,
        "source_checkpoint_reopened": True,
        "node_count": len(closure.records),
        "root_count": len(closure.roots()),
        "max_stage_depth": max_stage_depth,
        "word_width": word_width,
    }, sort_keys=True, separators=(",", ":"))


def _legacy_synthesize_registered_product_modules_v0(request: dict) -> str:
    """Synthesize trusted generated registered-product RTL out of context."""
    required = {"dcp_path", "source_dcp_sha256", "bundle_path", "work_dir"}
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered product synthesis request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    bundle_path = _owned_action_path(
        request["bundle_path"], name="registered product bundle", must_exist=True
    )
    work_dir = _owned_action_path(
        request["work_dir"], name="registered product synthesis", must_exist=False
    )
    if (
        source.is_symlink()
        or not source.is_file()
        or _sha256_file(source) != source_sha256
        or work_dir.exists()
    ):
        raise ValueError("registered product synthesis source or work directory is invalid")
    from FDAgents.registered_bridge import (
        render_registered_bridge_verilog,
        render_registered_product_verilog,
        validate_registered_bridge_bundle,
        validate_registered_product_bundle,
    )
    from FDAgents.consumer_rewrite import (
        render_consumer_rewrite_verilog,
        validate_consumer_rewrite_bundle,
    )
    raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    if raw_bundle.get("kind") == "generating_operator_bridge_bundle":
        bundle = validate_registered_bridge_bundle(raw_bundle)
        render_module = render_registered_bridge_verilog
    elif raw_bundle.get("kind") == "registered_consumer_rewrite_bundle":
        bundle = validate_consumer_rewrite_bundle(raw_bundle)
        render_module = render_consumer_rewrite_verilog
    else:
        bundle = validate_registered_product_bundle(raw_bundle)
        render_module = render_registered_product_verilog
    work_dir.mkdir()
    module_dcps = {}
    logs = []
    for plan in bundle["plans"]:
        module_name = plan["module_name"]
        rtl = work_dir / f"{module_name}.v"
        output = work_dir / f"{module_name}.dcp"
        script = work_dir / f"{module_name}.tcl"
        log = work_dir / f"{module_name}.log"
        rtl.write_text(render_module(plan), encoding="utf-8")
        script.write_text("\n".join([
            f"open_checkpoint {{{source}}}",
            "set rb_part [get_property PART [current_design]]",
            "close_design",
            f"read_verilog {{{rtl}}}",
            f"synth_design -mode out_of_context -top {module_name} -part $rb_part",
            f"write_checkpoint -force {{{output}}}",
            f"write_edif -force {{{output.with_suffix('.edf')}}}",
            "exit",
            "",
        ]), encoding="utf-8")
        completed = subprocess.run(
            [get_vivado_path(), "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600.0,
            cwd=str(work_dir),
        )
        log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        if completed.returncode != 0 or not output.is_file():
            raise RuntimeError(
                "registered product module synthesis failed: "
                + ((completed.stderr or completed.stdout or "")[-3000:])
            )
        module_dcps[module_name] = str(output)
        logs.append(str(log))
    return json.dumps({
        "status": "success",
        "module_dcps": module_dcps,
        "module_count": len(module_dcps),
        "logs": logs,
    }, sort_keys=True, separators=(",", ":"))


def _legacy_route_registered_product_bundle_v0(request: dict) -> str:
    """Wire, remap, place and route one internally recovered product bundle."""
    global _design_open, _command_pending
    required = {
        "inserted_dcp", "source_dcp", "source_dcp_sha256", "bundle_path",
        "output_dcp", "route_timeout_s", "physical_profile",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered product route request has an unsupported schema")
    inserted = _owned_action_path(
        request["inserted_dcp"], name="registered product inserted DCP", must_exist=True
    )
    source = Path(str(request["source_dcp"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    bundle_path = _owned_action_path(
        request["bundle_path"], name="registered product bundle", must_exist=True
    )
    output = _owned_action_path(
        request["output_dcp"], name="registered product output", must_exist=False
    )
    timeout = float(request["route_timeout_s"])
    profile = str(request["physical_profile"])
    directives = {
        "semantic_rewrite": (
            "Default",
            [],
            "NoTimingRelaxation",
            [],
        ),
        "compact_timing": (
            "ExtraTimingOpt",
            ["catch {phys_opt_design -directive AggressiveExplore} rb_phys_message"],
            "NoTimingRelaxation",
            [],
        ),
        "net_delay_focus": (
            "ExtraNetDelay_high",
            ["catch {phys_opt_design -directive AggressiveExplore} rb_phys_message"],
            "NoTimingRelaxation",
            [],
        ),
        "full_timing": (
            "ExtraTimingOpt",
            [
                "phys_opt_design -directive AggressiveExplore",
                "phys_opt_design -fanout_opt",
            ],
            "NoTimingRelaxation",
            ["phys_opt_design -directive AggressiveExplore"],
        ),
    }
    if (
        source.is_symlink()
        or not source.is_file()
        or _sha256_file(source) != source_sha256
        or output.exists()
        or not 1 <= timeout <= 3600
        or profile not in directives
    ):
        raise ValueError("registered product route request is invalid")
    from FDAgents.registered_bridge import (
        render_registered_bridge_connect_tcl,
        render_registered_product_connect_tcl,
        validate_registered_bridge_bundle,
        validate_registered_product_bundle,
    )
    from FDAgents.consumer_rewrite import (
        render_consumer_rewrite_connect_tcl,
        validate_consumer_rewrite_bundle,
    )
    raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    flattened_connected = False
    native_edif: Optional[Path] = None
    native_edif_dir: Optional[Path] = None
    source_timing_xdc = output.parent / "registered_product_source_timing.private.xdc"
    source_dcp_tcl = _tcl_utf8_value(str(source))
    source_timing_xdc_tcl = _tcl_utf8_value(str(source_timing_xdc))
    if raw_bundle.get("kind") == "generating_operator_bridge_bundle":
        bundle = validate_registered_bridge_bundle(raw_bundle)
        sidecar = inserted.with_suffix(inserted.suffix + ".flat.json")
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValueError("flattened registered bridge provenance is unavailable")
        flat_provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        if flat_provenance != {
            "version": 1,
            "kind": "flattened_connected_generating_operator_bridge",
            "source_dcp_sha256": source_sha256,
            "inserted_dcp_sha256": _sha256_file(inserted),
            "bundle_sha256": _sha256_file(bundle_path),
        }:
            raise ValueError("flattened registered bridge provenance changed")
        # A MacroLevel OOC checkpoint written by RapidWright can carry a
        # PhysDB archive that Vivado can read and route but cannot write back.
        # Extract its single flattened logical EDIF and let Vivado link a
        # native design before implementation.  Vivado parsing is also the
        # authoritative completeness check: a disk-full RapidWright write can
        # otherwise produce a CRC-clean ZIP containing a truncated EDIF.
        with zipfile.ZipFile(inserted) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError("flattened registered bridge archive is corrupt")
            edif_members = [
                item for item in archive.infolist()
                if not item.is_dir() and Path(item.filename).suffix.lower() == ".edf"
            ]
            if len(edif_members) != 1:
                raise ValueError(
                    "flattened registered bridge archive has no unique top EDIF"
                )
            edif_top = Path(edif_members[0].filename).stem
            if not edif_top:
                raise ValueError("flattened registered bridge EDIF has no top name")
            # Vivado selects an EDIF top by both its internal design name and
            # the file basename. Preserve the archive member basename inside
            # a provenance-bound private directory rather than assigning a
            # generic filename or colliding with an action-owned EDIF.
            native_edif_dir = output.parent / (
                ".registered_product_flat_"
                + flat_provenance["inserted_dcp_sha256"][:16]
            )
            if native_edif_dir.is_symlink() or (
                native_edif_dir.exists() and not native_edif_dir.is_dir()
            ):
                raise ValueError("flattened registered bridge EDIF directory is unsafe")
            native_edif_dir.mkdir(exist_ok=True)
            native_edif = native_edif_dir / Path(edif_members[0].filename).name
            if native_edif.is_symlink():
                raise ValueError("flattened registered bridge EDIF cache is unsafe")
            native_edif.unlink(missing_ok=True)
            with archive.open(edif_members[0]) as source_edif, native_edif.open("xb") as sink:
                shutil.copyfileobj(source_edif, sink)
        if native_edif.stat().st_size <= 0:
            raise ValueError("flattened registered bridge EDIF is empty")
        # RapidWright already connected every trusted bundle port before
        # flattening.  Reconnecting vanished module boundary pins would be
        # both impossible and semantically redundant.
        connect = "\n".join([
            "if {$argc != 2} { error {usage: connect.tcl inserted.dcp output.dcp} }",
            f"open_checkpoint {source_dcp_tcl}",
            "set rb_part [get_property PART [current_design]]",
            f"set rb_top {_tcl_utf8_value(edif_top)}",
            "close_design",
            f"read_edif {{{native_edif}}}",
            "link_design -part $rb_part -top $rb_top -mode out_of_context",
            f"read_xdc {source_timing_xdc_tcl}",
            protect_timing_exception_sources_tcl(),
            "puts REGISTERED_BRIDGE_FLAT_WIRING_READY",
            "exit",
            "",
        ])
        flattened_connected = True
    elif raw_bundle.get("kind") == "registered_consumer_rewrite_bundle":
        bundle = validate_consumer_rewrite_bundle(raw_bundle)
        connect = render_consumer_rewrite_connect_tcl(bundle)
    else:
        bundle = validate_registered_product_bundle(raw_bundle)
        connect = render_registered_product_connect_tcl(bundle)
    if not flattened_connected:
        # The connect/cleanup transaction itself can make Vivado rebuild a
        # high-fanout exception source while writing its intermediate
        # checkpoint. Protect exact reported exception sources immediately
        # after the first source open, before any rewiring or cleanup occurs.
        connect = inject_timing_exception_source_guard(connect)
    if not connect.endswith("exit\n"):
        raise RuntimeError("registered product connect script terminator is invalid")
    connect = connect[:-5]
    (
        place_directive, pre_route_phys, route_directive, post_route_phys,
    ) = directives[profile]
    script = exact_timing_xdc_capture_tcl(
        source_dcp_tcl, source_timing_xdc_tcl,
    ) + "\n" + connect + "\n" + "\n".join([
        *([] if flattened_connected else [f"open_checkpoint {{{output}}}"]),
        "set_param general.maxThreads 8",
        # Place and route the newly inserted module without globally unplacing
        # or unrouting the routed parent. A global remap here can also clone
        # unrelated timing-exception endpoints.
        f"place_design -directive {place_directive}",
        *pre_route_phys,
        f"route_design -directive {route_directive}",
        *post_route_phys,
        # Implementation may legitimately clone a high-fanout register and
        # cause Vivado's in-memory exception to expand to replica pins.  The
        # contest contract is the source DCP timing XDC, so reload that exact
        # contract after route and before emitting the candidate checkpoint.
        exact_timing_xdc_restore_tcl(source_timing_xdc_tcl),
        f"write_checkpoint -force {{{output}}}",
        "puts REGISTERED_PRODUCT_ROUTE_DONE",
        "close_design",
        "",
    ])
    failure = None
    candidate_sha256 = None
    route_ok = False
    output_text = ""
    try:
        close_current_design()
        argv = "set argv [list " + " ".join(
            _tcl_utf8_value(value) for value in (str(inserted), str(output))
        ) + "]\nset argc [llength $argv]\n"
        # Generated ECO scripts can be large.  Sending them to the interactive
        # shell as thousands of growing base64 ``append`` commands makes Tcl
        # copy the accumulated string repeatedly before Vivado does any useful
        # work.  The MCP server and Vivado share this owned run directory, so a
        # private source file is both faster and leaves the exact transaction
        # available for backend debugging.
        route_script = output.parent / "registered_product_route.private.tcl"
        route_script.write_text(argv + script, encoding="utf-8")
        output_text = run_tcl_command(
            f"source {{{route_script}}}", timeout=timeout + 900.0
        )
        _design_open = False
        if "REGISTERED_PRODUCT_ROUTE_DONE" not in output_text or not output.is_file():
            raise RuntimeError(
                "registered product route did not produce a checkpoint; "
                f"completion_diagnostic={tcl_failure_diagnostic(output_text)}; "
                f"vivado_output={output_text[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        route_report = run_tcl_command("report_route_status -return_string", timeout=180)
        errors = re.search(r"# of nets with routing errors[.\s:]+(\d+)", route_report, re.I)
        route_ok = errors is not None and int(errors.group(1)) == 0
        if not route_ok:
            raise RuntimeError("registered product candidate is not fully routed")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        if native_edif is not None:
            native_edif.unlink(missing_ok=True)
        if native_edif_dir is not None:
            try:
                native_edif_dir.rmdir()
            except OSError:
                pass
        try:
            if _command_pending:
                restart_vivado_process()
            else:
                run_tcl_command("catch {close_design}", timeout=30)
            _design_open = False
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or candidate_sha256 is None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"registered product route failed: {failure}") from failure
    return json.dumps({
        "status": "success",
        "candidate": {"dcp_sha256": candidate_sha256},
        "source_checkpoint_reopened": True,
        "candidate_checkpoint_reopened": True,
        "relevant_nets_fully_routed": route_ok,
        "instance_count": len(bundle["instances"]),
        "module_count": len(bundle["plans"]),
        "profile": profile,
        "flattened_connected": flattened_connected,
    }, sort_keys=True, separators=(",", ":"))


def apply_proved_arithmetic_compressor_eco(request: dict) -> str:
    """Apply the fixed certificate-bound LUT2/CARRY8 ECO transactionally."""
    global _design_open, _command_pending
    required = {
        "dcp_path", "source_dcp_sha256", "cone_dir", "certificate_path",
        "certificate_id", "authorization_id", "output_dcp", "route_timeout_s",
        "physical_profile",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("arithmetic compressor ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    cone_dir = _owned_action_path(request["cone_dir"], name="compressor cone", must_exist=True)
    certificate_path = _owned_action_path(
        request["certificate_path"], name="compressor certificate", must_exist=True
    )
    output = _owned_action_path(
        request["output_dcp"], name="compressor output", must_exist=False
    )
    if (
        source.is_symlink()
        or not source.is_file()
        or output == source
        or not output.parent.is_dir()
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
    ):
        raise ValueError("arithmetic compressor source or output identity is invalid")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("arithmetic compressor route_timeout_s must be in 1..3600")
    physical_profile = str(request["physical_profile"])
    physical_profiles = {
        "compact_timing": "ExtraTimingOpt",
        "net_delay_focus": "ExtraNetDelay_high",
    }
    if physical_profile not in physical_profiles:
        raise ValueError("arithmetic compressor physical_profile is not registered")
    certificate_id = str(request["certificate_id"])
    authorization_id = str(request["authorization_id"])
    if (
        re.fullmatch(r"[0-9a-f]{64}", certificate_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", authorization_id) is None
    ):
        raise ValueError("arithmetic compressor proof identity is invalid")
    try:
        from FDAgents.compressor_proof import verify_compressor_certificate
    except ImportError as exc:
        raise RuntimeError("compressor proof runtime is unavailable") from exc
    certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    if (
        certificate.get("certificate_id") != certificate_id
        or certificate.get("source_identity", {}).get("dcp_sha256") != source_sha256
        or certificate.get("status") != "proved"
        or certificate.get("mutation_ready") is not True
        or not verify_compressor_certificate(certificate, cone_dir)
    ):
        raise ValueError("arithmetic compressor certificate did not replay")

    work_dir = output.parent / (
        "compressor_eco_" + certificate_id[:12] + "_" + physical_profile
    )
    postcheck_tcl = build_recurrence_postcheck_tcl(facts, candidate, placement)
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_RECURRENCE_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "recurrence ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_RECURRENCE_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "recurrence ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"recurrence ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence ECO candidate identity changed")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_srl_recurrence_cut_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": facts.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": candidate["authorization"]["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": {
            "joint_connected_topology_replayed": True,
            "all_addresses_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": True,
        },
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def _legacy_apply_proved_recurrence_family_eco_v0(request: dict) -> str:
    """Apply a complete freshly recovered recurrence family atomically."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "family_facts", "candidate",
        "placement", "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("recurrence family ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("recurrence family ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("recurrence family ECO output path is unsafe")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or _sha256_file(source) != source_sha256:
        raise ValueError("recurrence family ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 3600:
        raise ValueError("recurrence family ECO route_timeout_s must be in 1..3600")
    family = request["family_facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if (
        not isinstance(family, dict)
        or family.get("kind") != "dsp_recurrence_family_contract_facts"
        or family.get("design_sha256") != source_sha256
        or family.get("fresh_rediscovery") is not True
        or family.get("mutation_eligible") is not False
    ):
        raise ValueError("recurrence family facts are stale or malformed")
    family_payload = dict(family)
    claimed_family_sha = family_payload.pop("family_contract_sha256", None)
    for key in ("family_id", "fresh_rediscovery", "runtime_s"):
        family_payload.pop(key, None)
    if not isinstance(claimed_family_sha, str) or _stable_digest(family_payload) != claimed_family_sha:
        raise ValueError("recurrence family facts digest mismatch")
    try:
        from FDAgents.recurrence_action import build_recurrence_family_topology_manifest
    except ImportError as exc:
        raise RuntimeError("FDAgents recurrence family proof runtime is unavailable") from exc
    topology = build_recurrence_family_topology_manifest(family, candidate, placement)
    if topology.get("emission_eligible") is not True:
        raise ValueError("recurrence family source contracts are incomplete")
    eco_tcl = build_recurrence_family_eco_tcl(
        family, candidate, placement, output_dcp=str(output)
    )
    postcheck_tcl = build_recurrence_family_postcheck_tcl(
        family, candidate, placement
    )
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_RECURRENCE_FAMILY_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "recurrence family ECO did not produce its artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_RECURRENCE_FAMILY_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "recurrence family postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"recurrence family ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence family ECO source changed")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence family ECO candidate identity changed")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_srl_recurrence_cut_v1",
        "source": {"dcp_sha256": source_sha256, "region_sha256": claimed_family_sha},
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": candidate["authorization"]["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": {
            "joint_connected_topology_replayed": True,
            "all_addresses_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": True,
        },
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_proved_memory_repack_eco(request: dict) -> str:
    """Rebuild one authorized BRAM primitive and restore the exact source."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "facts", "consumer_facts",
        "candidate", "micro_action", "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("memory ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    for name, path in (("source", source), ("output parent", output.parent)):
        if any(char in str(path) for char in "{}\n\r"):
            raise ValueError(f"memory ECO {name} path contains unsafe characters")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("memory ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("memory ECO output path is unsafe")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("memory ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or (
        _sha256_file(source) != source_sha256
    ):
        raise ValueError("memory ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("memory ECO route_timeout_s must be in 1..3600")
    facts = request["facts"]
    consumer_facts = request["consumer_facts"]
    candidate = request["candidate"]
    if not isinstance(facts, dict) or facts.get("design_sha256") != source_sha256:
        raise ValueError("memory ECO facts are stale or malformed")
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("authorization"), dict
    ):
        raise ValueError("memory ECO candidate authorization is missing")
    authorization = candidate["authorization"]
    try:
        from FDAgents.memory_action import build_memory_emission_manifests
        from FDAgents.operator_proof import verify_rewrite_authorization
        from FDAgents.semantic_rebuild import validate_semantic_micro_action_ref
    except ImportError as exc:
        raise RuntimeError("FDAgents memory emission runtime is unavailable") from exc
    if authorization.get("rewrite_kind") != "memory_semantic_repack" or not (
        verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_sha256,
            expected_region_sha256=facts.get("region_sha256"),
        )
    ):
        raise ValueError("memory ECO authorization failed proof replay")
    micro_action = validate_semantic_micro_action_ref(
        request["micro_action"], expected_input_sha256=source_sha256
    )
    if (
        micro_action.get("template") != "memory_semantic_repack_v1"
        or micro_action.get("candidate_id") != facts.get("candidate_id")
        or micro_action.get("authorization_id") != authorization.get("certificate_id")
    ):
        raise ValueError("memory ECO micro action does not bind the candidate")
    manifests = build_memory_emission_manifests(facts, consumer_facts, candidate)
    placement = manifests["placement"]
    topology = manifests["topology"]
    eco_tcl = build_memory_repack_eco_tcl(
        facts, authorization, output_dcp=str(output)
    )
    postcheck_tcl = build_memory_repack_postcheck_tcl(
        facts, consumer_facts, authorization
    )
    signature_tcl = build_fixed_point_port_signature_tcl()
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    source_signature = None
    candidate_signature = None
    inventory_replayed = False
    routed_replayed = False
    consumers_replayed = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        source_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_MEMORY_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "memory ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_MEMORY_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "memory ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
        inventory_replayed = "FDAGENTS_MEMORY_INVENTORY_OK" in postcheck_output
        routed_replayed = "FDAGENTS_MEMORY_ROUTED_OK" in postcheck_output
        consumers_replayed = "FDAGENTS_MEMORY_CONSUMERS_OK" in postcheck_output
        if not inventory_replayed or not routed_replayed or not consumers_replayed:
            raise RuntimeError(
                "memory ECO postcheck did not prove inventory, consumers, and routing"
            )
        candidate_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        if candidate_signature != source_signature:
            raise RuntimeError("memory ECO changed the whole-design interface")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"memory ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("memory ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("memory ECO candidate identity changed")
    interface_replayed = candidate_signature is not None and (
        candidate_signature == source_signature
    )
    checks = {
        "memory_primitive_replayed": True,
        "memory_properties_replayed": True,
        "memory_boundary_replayed": inventory_replayed,
        "consumer_dependencies_replayed": consumers_replayed,
        "whole_design_port_contract_replayed": interface_replayed,
        "exact_namespace_inventory": inventory_replayed,
        "closed_top_identity_replayed": interface_replayed,
        "candidate_checkpoint_reopened": candidate_reopened,
        "source_checkpoint_reopened": source_reopened,
        "relevant_nets_fully_routed": routed_replayed,
    }
    if any(value is not True for value in checks.values()):
        output.unlink(missing_ok=True)
        raise RuntimeError("memory ECO checks were not all proved")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "memory_semantic_repack_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": facts.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": checks,
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_semantic_rebuild_atomic_eco(request: dict) -> str:
    """Execute a resolved semantic DAG as one rollback-safe Vivado transaction."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest", "resolutions",
        "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("semantic atomic ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    if any(any(char in str(path) for char in "{}\n\r") for path in (source, output.parent)):
        raise ValueError("semantic atomic ECO path contains unsafe characters")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("semantic atomic ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("semantic atomic ECO output path is unsafe")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("semantic atomic ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or (
        _sha256_file(source) != source_sha256
    ):
        raise ValueError("semantic atomic ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("semantic atomic ECO route_timeout_s must be in 1..3600")
    manifest = request["manifest"]
    resolutions = request["resolutions"]
    if not isinstance(resolutions, list) or not resolutions:
        raise ValueError("semantic atomic ECO resolutions are unavailable")
    try:
        from FDAgents.memory_action import build_memory_emission_manifests
        from FDAgents.semantic_rebuild import build_semantic_rebuild_execution_bundle
    except ImportError as exc:
        raise RuntimeError("FDAgents semantic execution runtime is unavailable") from exc
    bundle = build_semantic_rebuild_execution_bundle(manifest, resolutions)
    if bundle.get("input_dcp_sha256") != source_sha256:
        raise ValueError("semantic execution bundle is bound to another source")
    resolution_by_node = {item["node_id"]: item for item in resolutions}
    ordered = [resolution_by_node[node_id] for node_id in bundle["execution_order"]]
    per_action_timeout = max(1.0, float(timeout) / len(ordered))
    signature_tcl = build_fixed_point_port_signature_tcl()
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    source_signature = None
    candidate_signature = None
    postcheck_status = {}
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        source_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        output.unlink(missing_ok=True)
        for resolution in ordered:
            authorization = resolution["candidate"]["authorization"]
            eco_tcl = build_memory_repack_eco_tcl(
                resolution["facts"], authorization, output_dcp=str(output)
            )
            eco_output = run_tcl_script(eco_tcl, timeout=per_action_timeout)
            if "FDAGENTS_MEMORY_ECO_DONE" not in eco_output or not output.is_file():
                raise RuntimeError(
                    f"semantic micro action did not emit: {resolution['node_id']}"
                )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        for resolution in ordered:
            authorization = resolution["candidate"]["authorization"]
            postcheck_tcl = build_memory_repack_postcheck_tcl(
                resolution["facts"], resolution["consumer_facts"], authorization
            )
            postcheck_output = run_tcl_script(
                postcheck_tcl, timeout=min(per_action_timeout, 900.0)
            )
            status = {
                "inventory": "FDAGENTS_MEMORY_INVENTORY_OK" in postcheck_output,
                "consumers": "FDAGENTS_MEMORY_CONSUMERS_OK" in postcheck_output,
                "routing": "FDAGENTS_MEMORY_ROUTED_OK" in postcheck_output,
                "complete": "FDAGENTS_MEMORY_POSTCHECK_DONE" in postcheck_output,
            }
            if any(value is not True for value in status.values()):
                raise RuntimeError(
                    f"semantic micro action postcheck failed: {resolution['node_id']}"
                )
            postcheck_status[resolution["node_id"]] = status
        candidate_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        if candidate_signature != source_signature:
            raise RuntimeError("semantic atomic ECO changed the whole-design interface")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"semantic atomic ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("semantic atomic ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("semantic atomic ECO candidate identity changed")
    interface_replayed = candidate_signature is not None and (
        candidate_signature == source_signature
    )
    node_attestations = []
    for resolution in ordered:
        facts = resolution["facts"]
        candidate = resolution["candidate"]
        authorization = candidate["authorization"]
        emission = build_memory_emission_manifests(
            facts, resolution["consumer_facts"], candidate
        )
        status = postcheck_status[resolution["node_id"]]
        checks = {
            "memory_primitive_replayed": status["complete"],
            "memory_properties_replayed": status["complete"],
            "memory_boundary_replayed": status["inventory"],
            "consumer_dependencies_replayed": status["consumers"],
            "whole_design_port_contract_replayed": interface_replayed,
            "exact_namespace_inventory": status["inventory"],
            "closed_top_identity_replayed": interface_replayed,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": status["routing"],
        }
        if any(value is not True for value in checks.values()):
            output.unlink(missing_ok=True)
            raise RuntimeError("semantic atomic ECO node checks were not all proved")
        attestation = {
            "kind": "operator_rewrite_emitted_topology_attestation",
            "version": 1,
            "status": "proved",
            "authority": "candidate_measurement_only",
            "template": "memory_semantic_repack_v1",
            "source": {
                "dcp_sha256": source_sha256,
                "region_sha256": facts["region_sha256"],
            },
            "candidate": {"dcp_sha256": candidate_sha256},
            "authorization_id": authorization["certificate_id"],
            "placement_manifest_sha256": emission["placement"]["manifest_sha256"],
            "topology_manifest_sha256": emission["topology"]["manifest_sha256"],
            "checks": checks,
        }
        attestation["attestation_id"] = _stable_digest(attestation)
        node_attestations.append({
            "node_id": resolution["node_id"],
            "attestation": attestation,
        })
    result = {
        "kind": "semantic_rebuild_atomic_execution_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "source": {"dcp_sha256": source_sha256},
        "candidate": {"dcp_sha256": candidate_sha256},
        "semantic_manifest_sha256": manifest["manifest_sha256"],
        "execution_bundle_sha256": bundle["bundle_sha256"],
        "execution_order": bundle["execution_order"],
        "node_attestations": node_attestations,
        "checks": {
            "single_source_transaction": True,
            "all_nodes_postchecked": len(node_attestations) == len(ordered),
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "whole_design_interface_replayed": interface_replayed,
        },
    }
    result["attestation_id"] = _stable_digest(result)
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_proved_fixed_point_eco(request: dict) -> str:
    """Apply one fixed fabric-to-DSP rebuild transaction and restore the source."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "facts", "candidate", "placement",
        "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("fixed-point ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    for name, path in (("source", source), ("output parent", output.parent)):
        if "}" in str(path) or "{" in str(path) or "\n" in str(path) or "\r" in str(path):
            raise ValueError(f"fixed-point ECO {name} path contains unsafe characters")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("fixed-point ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("fixed-point ECO output path is unsafe")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("fixed-point ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("fixed-point ECO source identity is invalid")
    if _sha256_file(source) != source_sha256:
        raise ValueError("fixed-point ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("fixed-point ECO route_timeout_s must be in 1..3600")
    facts = request["facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if not isinstance(facts, dict) or facts.get("design_sha256") != source_sha256:
        raise ValueError("fixed-point ECO facts are stale or malformed")
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("authorization"), dict
    ):
        raise ValueError("fixed-point ECO candidate authorization is missing")
    authorization = candidate["authorization"]
    try:
        from FDAgents.operator_proof import verify_rewrite_authorization
    except ImportError as exc:
        raise RuntimeError("FDAgents operator proof runtime is unavailable") from exc
    if authorization.get("rewrite_kind") != "fabric_to_dsp" or not (
        verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_sha256,
            expected_region_sha256=facts.get("region_sha256"),
        )
    ):
        raise ValueError("fixed-point ECO authorization failed its proof replay")
    try:
        from FDAgents.fixed_point_action import build_fixed_point_topology_manifest
    except ImportError as exc:
        raise RuntimeError("FDAgents fixed-point proof runtime is unavailable") from exc
    topology = build_fixed_point_topology_manifest(facts, candidate, placement)
    if (
        not isinstance(topology, dict)
        or topology.get("kind") != "fixed_point_topology_manifest"
        or topology.get("emission_eligible") is not True
        or not isinstance(topology.get("manifest_sha256"), str)
    ):
        raise ValueError("fixed-point ECO source contracts are incomplete")
    eco_tcl = build_fixed_point_eco_tcl(
        facts, authorization, placement, output_dcp=str(output)
    )
    postcheck_tcl = build_fixed_point_postcheck_tcl(facts, authorization, placement)
    signature_tcl = build_fixed_point_port_signature_tcl()
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    source_signature = None
    candidate_signature = None
    inventory_replayed = False
    routed_replayed = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        source_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_FIXED_POINT_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "fixed-point ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_FIXED_POINT_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "fixed-point ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
        inventory_replayed = "FDAGENTS_FIXED_POINT_INVENTORY_OK" in postcheck_output
        routed_replayed = "FDAGENTS_FIXED_POINT_ROUTED_OK" in postcheck_output
        if not inventory_replayed or not routed_replayed:
            raise RuntimeError(
                "fixed-point ECO postcheck did not prove inventory and routing"
            )
        candidate_signature = parse_fixed_point_port_signature(postcheck_output)
        if candidate_signature["port_signature"] != source_signature["port_signature"]:
            raise RuntimeError("fixed-point ECO changed the whole-design port contract")
        if candidate_signature["top_identity"] != source_signature["top_identity"]:
            raise RuntimeError("fixed-point ECO changed the closed top identity")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"fixed-point ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("fixed-point ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("fixed-point ECO candidate identity changed")
    checks = {
        "fixed_point_dsp_topology_replayed": True,
        "whole_design_port_contract_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["port_signature"]
            == source_signature["port_signature"]
        ),
        "exact_namespace_inventory": inventory_replayed,
        "closed_top_identity_rename_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["top_identity"]
            == source_signature["top_identity"]
        ),
        "candidate_checkpoint_reopened": candidate_reopened,
        "source_checkpoint_reopened": source_reopened,
        "relevant_nets_fully_routed": routed_replayed,
    }
    if any(value is not True for value in checks.values()):
        output.unlink(missing_ok=True)
        raise RuntimeError("fixed-point ECO checks were not all proved")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "fixed_point_dsp_rebuild_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": facts.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": checks,
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_proved_two_operand_eco(request: dict) -> str:
    """Apply one fixed two-operand DSP rebuild transaction and restore the source."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "facts", "candidate", "placement",
        "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("two-operand ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    for name, path in (("source", source), ("output parent", output.parent)):
        if "}" in str(path) or "{" in str(path) or "\n" in str(path) or "\r" in str(path):
            raise ValueError(f"two-operand ECO {name} path contains unsafe characters")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("two-operand ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("two-operand ECO output path is unsafe")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("two-operand ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("two-operand ECO source identity is invalid")
    if _sha256_file(source) != source_sha256:
        raise ValueError("two-operand ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("two-operand ECO route_timeout_s must be in 1..3600")
    facts = request["facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if not isinstance(facts, dict) or facts.get("design_sha256") != source_sha256:
        raise ValueError("two-operand ECO facts are stale or malformed")
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("authorization"), dict
    ):
        raise ValueError("two-operand ECO candidate authorization is missing")
    authorization = candidate["authorization"]
    try:
        from FDAgents.operator_proof import verify_rewrite_authorization
    except ImportError as exc:
        raise RuntimeError("FDAgents operator proof runtime is unavailable") from exc
    if authorization.get("rewrite_kind") != "fabric_to_dsp_two_operand" or not (
        verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_sha256,
            expected_region_sha256=facts.get("region_sha256"),
        )
    ):
        raise ValueError("two-operand ECO authorization failed its proof replay")
    try:
        from FDAgents.two_operand_action import build_two_operand_topology_manifest
    except ImportError as exc:
        raise RuntimeError("FDAgents two-operand proof runtime is unavailable") from exc
    topology = build_two_operand_topology_manifest(facts, candidate, placement)
    if (
        not isinstance(topology, dict)
        or topology.get("kind") != "two_operand_topology_manifest"
        or topology.get("emission_eligible") is not True
        or not isinstance(topology.get("manifest_sha256"), str)
    ):
        raise ValueError("two-operand ECO source contracts are incomplete")
    eco_tcl = build_two_operand_eco_tcl(
        facts, authorization, placement, output_dcp=str(output)
    )
    postcheck_tcl = build_two_operand_postcheck_tcl(facts, authorization, placement)
    signature_tcl = build_fixed_point_port_signature_tcl()
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    source_signature = None
    candidate_signature = None
    inventory_replayed = False
    routed_replayed = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        source_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_TWO_OPERAND_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "two-operand ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_TWO_OPERAND_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "two-operand ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
        inventory_replayed = "FDAGENTS_TWO_OPERAND_INVENTORY_OK" in postcheck_output
        routed_replayed = "FDAGENTS_TWO_OPERAND_ROUTED_OK" in postcheck_output
        if not inventory_replayed or not routed_replayed:
            raise RuntimeError(
                "two-operand ECO postcheck did not prove inventory and routing"
            )
        candidate_signature = parse_fixed_point_port_signature(postcheck_output)
        if candidate_signature["port_signature"] != source_signature["port_signature"]:
            raise RuntimeError("two-operand ECO changed the whole-design port contract")
        if candidate_signature["top_identity"] != source_signature["top_identity"]:
            raise RuntimeError("two-operand ECO changed the closed top identity")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"two-operand ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("two-operand ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("two-operand ECO candidate identity changed")
    checks = {
        "two_operand_dsp_topology_replayed": True,
        "whole_design_port_contract_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["port_signature"]
            == source_signature["port_signature"]
        ),
        "exact_namespace_inventory": inventory_replayed,
        "closed_top_identity_rename_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["top_identity"]
            == source_signature["top_identity"]
        ),
        "candidate_checkpoint_reopened": candidate_reopened,
        "source_checkpoint_reopened": source_reopened,
        "relevant_nets_fully_routed": routed_replayed,
    }
    if any(value is not True for value in checks.values()):
        output.unlink(missing_ok=True)
        raise RuntimeError("two-operand ECO checks were not all proved")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_mul_two_operand_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": facts.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": checks,
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_proved_register_absorb_eco(request: dict) -> str:
    """Absorb one proved fabric FF bus into a placed DSP input register."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "facts", "candidate", "placement",
        "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("register-absorb ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    for name, path in (("source", source), ("output parent", output.parent)):
        if "}" in str(path) or "{" in str(path) or "\n" in str(path) or "\r" in str(path):
            raise ValueError(f"register-absorb ECO {name} path contains unsafe characters")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("register-absorb ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("register-absorb ECO output path is unsafe")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("register-absorb ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("register-absorb ECO source identity is invalid")
    if _sha256_file(source) != source_sha256:
        raise ValueError("register-absorb ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("register-absorb ECO route_timeout_s must be in 1..3600")
    facts = request["facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if not isinstance(facts, dict) or facts.get("design_sha256") != source_sha256:
        raise ValueError("register-absorb ECO facts are stale or malformed")
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("authorization"), dict
    ):
        raise ValueError("register-absorb ECO candidate authorization is missing")
    authorization = candidate["authorization"]
    try:
        from FDAgents.operator_proof import verify_rewrite_authorization
    except ImportError as exc:
        raise RuntimeError("FDAgents operator proof runtime is unavailable") from exc
    if authorization.get("rewrite_kind") != "fabric_ff_to_dsp_input_reg" or not (
        verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_sha256,
            expected_region_sha256=facts.get("region_sha256"),
        )
    ):
        raise ValueError("register-absorb ECO authorization failed its proof replay")
    try:
        from FDAgents.register_absorb_action import (
            build_register_absorb_topology_manifest,
        )
    except ImportError as exc:
        raise RuntimeError(
            "FDAgents register-absorb proof runtime is unavailable"
        ) from exc
    topology = build_register_absorb_topology_manifest(facts, candidate, placement)
    if (
        not isinstance(topology, dict)
        or topology.get("kind") != "register_absorb_topology_manifest"
        or topology.get("emission_eligible") is not True
        or not isinstance(topology.get("manifest_sha256"), str)
    ):
        raise ValueError("register-absorb ECO source contracts are incomplete")
    eco_tcl = build_register_absorb_eco_tcl(
        facts, authorization, placement, output_dcp=str(output)
    )
    signature_tcl = build_fixed_point_port_signature_tcl()
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    source_signature = None
    candidate_signature = None
    inventory_replayed = False
    routed_replayed = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        source_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if (
            "FDAGENTS_REGISTER_ABSORB_ECO_DONE" not in eco_output
            or not output.is_file()
        ):
            raise RuntimeError(
                "register-absorb ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        # The absorbed FDRE bus is gone from the candidate, so the postcheck
        # replays the control/driver net names captured by the ECO stages.
        control_nets = parse_register_absorb_control_nets(eco_output)
        postcheck_tcl = build_register_absorb_postcheck_tcl(
            facts, authorization, placement, control_nets=control_nets
        )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_REGISTER_ABSORB_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "register-absorb ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
        inventory_replayed = (
            "FDAGENTS_REGISTER_ABSORB_INVENTORY_OK" in postcheck_output
        )
        routed_replayed = "FDAGENTS_REGISTER_ABSORB_ROUTED_OK" in postcheck_output
        if not inventory_replayed or not routed_replayed:
            raise RuntimeError(
                "register-absorb ECO postcheck did not prove inventory and routing"
            )
        candidate_signature = parse_fixed_point_port_signature(postcheck_output)
        if candidate_signature["port_signature"] != source_signature["port_signature"]:
            raise RuntimeError(
                "register-absorb ECO changed the whole-design port contract"
            )
        if candidate_signature["top_identity"] != source_signature["top_identity"]:
            raise RuntimeError("register-absorb ECO changed the closed top identity")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"register-absorb ECO transaction failed: {failure}"
        ) from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("register-absorb ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("register-absorb ECO candidate identity changed")
    checks = {
        "register_absorb_topology_replayed": True,
        "whole_design_port_contract_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["port_signature"]
            == source_signature["port_signature"]
        ),
        "exact_namespace_inventory": inventory_replayed,
        "closed_top_identity_rename_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["top_identity"]
            == source_signature["top_identity"]
        ),
        "candidate_checkpoint_reopened": candidate_reopened,
        "source_checkpoint_reopened": source_reopened,
        "relevant_nets_fully_routed": routed_replayed,
    }
    if any(value is not True for value in checks.values()):
        output.unlink(missing_ok=True)
        raise RuntimeError("register-absorb ECO checks were not all proved")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_register_absorb_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": facts.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": checks,
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_proved_pipeline_rebalance_eco(request: dict) -> str:
    """Rebalance one proved MREG register stage inside a placed DSP48E2."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "facts", "candidate", "placement",
        "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("pipeline-rebalance ECO request has an unsupported schema")
    raw_source = Path(str(request["dcp_path"])).expanduser()
    raw_output = Path(str(request["output_dcp"])).expanduser()
    if raw_source.is_symlink():
        raise ValueError("pipeline-rebalance ECO source must not be a symlink")
    if raw_output.is_symlink():
        raise ValueError("pipeline-rebalance ECO output must not be a symlink")
    if raw_output.parent.is_symlink():
        raise ValueError("pipeline-rebalance ECO output parent must not be a symlink")
    source = raw_source.resolve()
    output = raw_output.resolve()
    for name, path in (("source", source), ("output parent", output.parent)):
        if "}" in str(path) or "{" in str(path) or "\n" in str(path) or "\r" in str(path):
            raise ValueError(f"pipeline-rebalance ECO {name} path contains unsafe characters")
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("pipeline-rebalance ECO source is not a regular DCP")
    if output == source or not output.parent.is_dir():
        raise ValueError("pipeline-rebalance ECO output path is unsafe")
    if output.exists() and not output.is_file():
        raise ValueError("pipeline-rebalance ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("pipeline-rebalance ECO source identity is invalid")
    if _sha256_file(source) != source_sha256:
        raise ValueError("pipeline-rebalance ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("pipeline-rebalance ECO route_timeout_s must be in 1..3600")
    facts = request["facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if not isinstance(facts, dict) or facts.get("design_sha256") != source_sha256:
        raise ValueError("pipeline-rebalance ECO facts are stale or malformed")
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("authorization"), dict
    ):
        raise ValueError("pipeline-rebalance ECO candidate authorization is missing")
    authorization = candidate["authorization"]
    try:
        from FDAgents.operator_proof import verify_rewrite_authorization
    except ImportError as exc:
        raise RuntimeError("FDAgents operator proof runtime is unavailable") from exc
    if authorization.get("rewrite_kind") != "dsp_pipeline_rebalance" or not (
        verify_rewrite_authorization(
            authorization,
            expected_dcp_sha256=source_sha256,
            expected_region_sha256=facts.get("region_sha256"),
        )
    ):
        raise ValueError("pipeline-rebalance ECO authorization failed its proof replay")
    try:
        from FDAgents.pipeline_rebalance_action import (
            build_pipeline_rebalance_topology_manifest,
        )
    except ImportError as exc:
        raise RuntimeError(
            "FDAgents pipeline-rebalance proof runtime is unavailable"
        ) from exc
    topology = build_pipeline_rebalance_topology_manifest(facts, candidate, placement)
    if (
        not isinstance(topology, dict)
        or topology.get("kind") != "pipeline_rebalance_topology_manifest"
        or topology.get("emission_eligible") is not True
        or not isinstance(topology.get("manifest_sha256"), str)
    ):
        raise ValueError("pipeline-rebalance ECO source contracts are incomplete")
    eco_tcl = build_pipeline_rebalance_eco_tcl(
        facts, authorization, placement, output_dcp=str(output)
    )
    signature_tcl = build_fixed_point_port_signature_tcl()
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    source_signature = None
    candidate_signature = None
    inventory_replayed = False
    routed_replayed = False
    try:
        close_current_design()
        output_text = run_tcl_script(script, timeout=float(timeout) + 600.0)
        _design_open = True
        source_signature = parse_fixed_point_port_signature(
            run_tcl_script(signature_tcl, timeout=600)
        )
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if (
            "FDAGENTS_PIPELINE_REBALANCE_ECO_DONE" not in eco_output
            or not output.is_file()
        ):
            raise RuntimeError(
                "pipeline-rebalance ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        # The rewired control pins keep no record of the moved nets, so the
        # postcheck replays the net names captured by the ECO stage-1 markers.
        control_nets = parse_pipeline_rebalance_control_nets(eco_output)
        postcheck_tcl = build_pipeline_rebalance_postcheck_tcl(
            facts, authorization, placement,
            candidate_dcp=str(output), control_nets=control_nets,
        )
        candidate_sha256 = _sha256_file(output)
        # The postcheck Tcl reopens the candidate checkpoint itself.
        close_current_design()
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        _design_open = True
        candidate_reopened = (
            "FDAGENTS_PIPELINE_REBALANCE_CANDIDATE_REOPENED" in postcheck_output
        )
        if "FDAGENTS_PIPELINE_REBALANCE_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "pipeline-rebalance ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
        if not candidate_reopened:
            raise RuntimeError(
                "pipeline-rebalance ECO postcheck did not reopen the candidate"
            )
        inventory_replayed = (
            "FDAGENTS_PIPELINE_REBALANCE_INVENTORY_OK" in postcheck_output
        )
        routed_replayed = "FDAGENTS_PIPELINE_REBALANCE_ROUTED_OK" in postcheck_output
        if not inventory_replayed or not routed_replayed:
            raise RuntimeError(
                "pipeline-rebalance ECO postcheck did not prove inventory and routing"
            )
        candidate_signature = parse_fixed_point_port_signature(postcheck_output)
        if candidate_signature["port_signature"] != source_signature["port_signature"]:
            raise RuntimeError(
                "pipeline-rebalance ECO changed the whole-design port contract"
            )
        if candidate_signature["top_identity"] != source_signature["top_identity"]:
            raise RuntimeError("pipeline-rebalance ECO changed the closed top identity")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _command_pending:
                restart_vivado_process()
            else:
                run_tcl_command("catch {close_design}", timeout=30)
            _design_open = False
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"pipeline-rebalance ECO transaction failed: {failure}"
        ) from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("pipeline-rebalance ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("pipeline-rebalance ECO candidate identity changed")
    checks = {
        "rebalance_topology_replayed": True,
        "whole_design_port_contract_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["port_signature"]
            == source_signature["port_signature"]
        ),
        "exact_namespace_inventory": inventory_replayed,
        "closed_top_identity_rename_replayed": (
            candidate_signature is not None
            and source_signature is not None
            and candidate_signature["top_identity"]
            == source_signature["top_identity"]
        ),
        "candidate_checkpoint_reopened": candidate_reopened,
        "source_checkpoint_reopened": source_reopened,
        "relevant_nets_fully_routed": routed_replayed,
    }
    if any(value is not True for value in checks.values()):
        output.unlink(missing_ok=True)
        raise RuntimeError("pipeline-rebalance ECO checks were not all proved")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_pipeline_rebalance_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": certificate["source_identity"]["connectivity_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": checks,
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    observations = {}
    for key, marker in (
        ("placed_wns_ns", "S2_PLACED_WNS"),
        ("routed_wns_ns", "S2_ROUTED_WNS"),
    ):
        match = re.search(
            rf"(?m)^{re.escape(marker)}=([^\s]+)\s*$", output_text
        )
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                value = None
            if value is not None and math.isfinite(value):
                observations[key] = value
    attestation["qor_observation"] = {
        "version": 1,
        "physical_profile": physical_profile,
        "place_directive": physical_profiles[physical_profile],
        "contest_clock": "clk_fpl26contest",
        "placed_wns_ns": observations.get("placed_wns_ns"),
        "routed_wns_ns": observations.get("routed_wns_ns"),
        "role": "advisory_physical_feedback_not_acceptance_authority",
    }
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def attest_proved_recurrence_family_candidate(request: dict) -> str:
    """Recheck an existing family candidate without mutating either checkpoint."""
    global _design_open, _command_pending
    required = {
        "dcp_path", "source_dcp_sha256", "candidate_dcp_path",
        "candidate_dcp_sha256", "family_facts", "candidate", "placement",
        "postcheck_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("recurrence family candidate request has an unsupported schema")

    raw_source = Path(str(request["dcp_path"])).expanduser()
    raw_candidate = Path(str(request["candidate_dcp_path"])).expanduser()
    for name, raw_path in (("source", raw_source), ("candidate", raw_candidate)):
        absolute = raw_path.absolute()
        cursor = absolute
        while True:
            if cursor.is_symlink():
                raise ValueError(
                    f"recurrence family candidate {name} path uses a symlink"
                )
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
    source = raw_source.resolve()
    candidate_dcp = raw_candidate.resolve()
    for name, path in (("source", source), ("candidate", candidate_dcp)):
        if (
            "{" in str(path)
            or "}" in str(path)
            or "\n" in str(path)
            or "\r" in str(path)
        ):
            raise ValueError(f"recurrence family candidate {name} path is unsafe")
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(
                f"recurrence family candidate {name} is not a regular DCP"
            )
    if source == candidate_dcp:
        raise ValueError("recurrence family source and candidate must be distinct")

    source_sha256 = str(request["source_dcp_sha256"])
    candidate_sha256 = str(request["candidate_dcp_sha256"])
    if (
        re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
    ):
        raise ValueError("recurrence family candidate source identity mismatch")
    if (
        re.fullmatch(r"[0-9a-f]{64}", candidate_sha256) is None
        or _sha256_file(candidate_dcp) != candidate_sha256
    ):
        raise ValueError("recurrence family candidate identity mismatch")

    timeout = request["postcheck_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 900
    ):
        raise ValueError("recurrence family postcheck_timeout_s must be in 1..900")

    family = request["family_facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if (
        not isinstance(family, dict)
        or family.get("kind") != "dsp_recurrence_family_contract_facts"
        or family.get("design_sha256") != source_sha256
        or family.get("fresh_rediscovery") is not True
        or family.get("mutation_eligible") is not False
    ):
        raise ValueError("recurrence family candidate facts are stale or malformed")
    authorization = (
        candidate.get("authorization") if isinstance(candidate, dict) else None
    )
    authorization_id = (
        authorization.get("certificate_id")
        if isinstance(authorization, dict)
        else None
    )
    placement_id = (
        placement.get("manifest_sha256")
        if isinstance(placement, dict)
        else None
    )
    if (
        not isinstance(authorization_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", authorization_id) is None
        or not isinstance(placement_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", placement_id) is None
    ):
        raise ValueError("recurrence family candidate authorization is malformed")
    family_payload = dict(family)
    claimed_family_sha = family_payload.pop("family_contract_sha256", None)
    for key in ("family_id", "fresh_rediscovery", "runtime_s"):
        family_payload.pop(key, None)
    if (
        not isinstance(claimed_family_sha, str)
        or _stable_digest(family_payload) != claimed_family_sha
    ):
        raise ValueError("recurrence family candidate facts digest mismatch")
    try:
        from FDAgents.recurrence_action import (
            build_recurrence_family_topology_manifest,
        )
    except ImportError as exc:
        raise RuntimeError("FDAgents recurrence family proof runtime is unavailable") from exc
    topology = build_recurrence_family_topology_manifest(
        family, candidate, placement
    )
    if topology.get("emission_eligible") is not True:
        raise ValueError("recurrence family candidate contracts are incomplete")
    postcheck_tcl = build_recurrence_family_postcheck_tcl(
        family, candidate, placement
    )

    failure = None
    restore_failure = None
    candidate_reopened = False
    source_reopened = False
    try:
        _close_design_with_marker()
        _open_checkpoint_with_marker(candidate_dcp, timeout=600)
        candidate_reopened = True
        postcheck_output = run_tcl_script(postcheck_tcl, timeout=float(timeout))
        if "FDAGENTS_RECURRENCE_FAMILY_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "recurrence family candidate postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _command_pending:
                restart_vivado_process()
            elif _design_open:
                try:
                    _close_design_with_marker()
                except Exception:  # noqa: BLE001
                    restart_vivado_process()
            _open_checkpoint_with_marker(source, timeout=600)
            source_reopened = _sha256_file(source) == source_sha256
            if not source_reopened:
                raise RuntimeError("restored source identity mismatch")
        except Exception as restore_exc:  # noqa: BLE001
            restore_failure = restore_exc

    identity_failures = []
    try:
        if _sha256_file(source) != source_sha256:
            identity_failures.append("source changed")
    except Exception as exc:  # noqa: BLE001
        identity_failures.append(f"source identity check failed: {exc}")
    try:
        if _sha256_file(candidate_dcp) != candidate_sha256:
            identity_failures.append("candidate changed")
    except Exception as exc:  # noqa: BLE001
        identity_failures.append(f"candidate identity check failed: {exc}")

    if failure is not None or restore_failure is not None or identity_failures:
        if restore_failure is not None or identity_failures:
            cleanup_vivado()
            _design_open = False
        reasons = []
        if failure is not None:
            reasons.append(str(failure))
        if restore_failure is not None:
            reasons.append(f"source restore failed: {restore_failure}")
        reasons.extend(identity_failures)
        cause = failure or restore_failure
        raise RuntimeError(
            "recurrence family candidate attestation failed: " + "; ".join(reasons)
        ) from cause

    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_srl_recurrence_cut_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": claimed_family_sha,
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization_id,
        "placement_manifest_sha256": placement_id,
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": {
            "joint_connected_topology_replayed": True,
            "all_addresses_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": True,
        },
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_screened_fixed_point_dsp_eco(request: dict) -> str:
    """Apply the fixed contract-bound DSP48E2 ECO transactionally."""
    global _design_open, _command_pending
    required = {
        "dcp_path", "source_dcp_sha256", "cone_dir", "contract_path",
        "contract_id", "authorization_id", "output_dcp", "route_timeout_s",
        "physical_profile",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("fixed-point DSP ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    source_sha256 = str(request["source_dcp_sha256"])
    cone_dir = _owned_action_path(
        request["cone_dir"], name="fixed-point cone", must_exist=True
    )
    contract_path = _owned_action_path(
        request["contract_path"], name="fixed-point contract", must_exist=True
    )
    output = _owned_action_path(
        request["output_dcp"], name="fixed-point output", must_exist=False
    )
    if (
        source.is_symlink()
        or not source.is_file()
        or output == source
        or not output.parent.is_dir()
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
    ):
        raise ValueError("fixed-point DSP source or output identity is invalid")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("fixed-point DSP route_timeout_s must be in 1..3600")
    physical_profile = str(request["physical_profile"])
    physical_profiles = {
        "compact_timing": "ExtraTimingOpt",
        "net_delay_focus": "ExtraNetDelay_high",
    }
    if physical_profile not in physical_profiles:
        raise ValueError("fixed-point DSP physical_profile is not registered")
    contract_id = str(request["contract_id"])
    authorization_id = str(request["authorization_id"])
    if (
        re.fullmatch(r"[0-9a-f]{64}", contract_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", authorization_id) is None
    ):
        raise ValueError("fixed-point DSP proof identity is invalid")
    try:
        from FDAgents.fixed_point_dsp import verify_fixed_point_contract
    except ImportError as exc:
        raise RuntimeError("fixed-point DSP contract runtime is unavailable") from exc
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if (
        contract.get("certificate_id") != contract_id
        or contract.get("source_identity", {}).get("dcp_sha256") != source_sha256
        or contract.get("mutation_ready_for_measurement_probe") is not True
        or not verify_fixed_point_contract(contract, cone_dir)
    ):
        raise ValueError("fixed-point DSP contract did not replay")

    work_dir = output.parent / (
        "fixed_point_dsp_eco_" + contract_id[:12] + "_" + physical_profile
    )
    if work_dir.exists():
        raise ValueError("fixed-point DSP ECO work directory must be fresh")
    work_dir.mkdir()
    generated_tcl = work_dir / "rewrite.tcl"
    generator = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "generate_fixed_point_dsp_eco.py"
    )
    completed = subprocess.run(
        [
            sys.executable, str(generator), str(cone_dir), str(contract_path),
            str(source), str(work_dir), str(generated_tcl),
            "--prefix", "fdagents_fpdsp_" + contract_id[:8],
            "--physical-profile", physical_profile,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180.0,
    )
    if completed.returncode != 0 or not generated_tcl.is_file():
        raise RuntimeError(
            "fixed-point DSP Tcl generation failed: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    script = generated_tcl.read_text(encoding="utf-8")
    if not script.endswith("exit\n"):
        raise RuntimeError("fixed-point DSP generated Tcl has an unknown terminator")
    script = script[:-5]
    constraint_commands = (
        "create_" + "clock",
        "set_false_" + "path",
        "set_max_" + "delay",
        "set_min_" + "delay",
        "set_clock_" + "groups",
    )
    constraint_pattern = (
        r"(?im)^\s*(?:"
        + "|".join(re.escape(command) for command in constraint_commands)
        + r")\b"
    )
    if re.search(constraint_pattern, script):
        raise RuntimeError("fixed-point DSP generated Tcl changes timing constraints")

    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    route_ok = False
    output_text = ""
    output.unlink(missing_ok=True)
    try:
        close_current_design()
        output_text = run_tcl_script(script, timeout=float(timeout) + 600.0)
        _design_open = True
        generated_candidate = work_dir / "candidate.dcp"
        if generated_candidate.is_file() and not output.exists():
            generated_candidate.replace(output)
        if "FPDSP_ECO_DONE" not in output_text or not output.is_file():
            raise RuntimeError(
                "fixed-point DSP ECO did not produce its completion artifact; "
                f"vivado_output={output_text[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        route_report = run_tcl_command(
            "report_route_status -return_string", timeout=180
        )
        values = {}
        for key, pattern in {
            "routable": r"# of routable nets[.\s:]+(\d+)",
            "routed": r"# of fully routed nets[.\s:]+(\d+)",
            "errors": r"# of nets with routing errors[.\s:]+(\d+)",
        }.items():
            match = re.search(pattern, route_report, re.IGNORECASE)
            values[key] = int(match.group(1)) if match else None
        route_ok = (
            values["routable"] is not None
            and values["routed"] == values["routable"]
            and values["errors"] == 0
        )
        if not route_ok:
            raise RuntimeError("fixed-point DSP candidate is not fully routed")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _command_pending:
                restart_vivado_process()
            else:
                run_tcl_command("catch {close_design}", timeout=30)
            _design_open = False
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"fixed-point DSP ECO transaction failed: {failure}") from failure
    if (
        _sha256_file(source) != source_sha256
        or candidate_sha256 is None
        or _sha256_file(output) != candidate_sha256
    ):
        output.unlink(missing_ok=True)
        raise RuntimeError("fixed-point DSP artifact identity changed")
    topology = contract["topology_manifest"]
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "fixed_point_dsp_rebuild_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": contract["source_identity"]["connectivity_sha256"],
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": authorization_id,
        "placement_manifest_sha256": _stable_digest({
            "physical_profile": physical_profile,
            "target_registers_preserved": True,
            "dsp48e2_count": sum(
                len(item.get("chunks") or [])
                for item in topology.get("product_terms") or []
            ),
        }),
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": {
            "fixed_point_dsp_topology_replayed": "FPDSP_MUTATION_END" in output_text,
            "source_contract_replayed": True,
            "target_register_contract_replayed": "FPDSP_ECO_DONE" in output_text,
            "exact_namespace_inventory": "FPDSP_MUTATION_END" in output_text,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": route_ok,
        },
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    observations = {}
    for key, marker in (
        ("placed_wns_ns", "FPDSP_PLACED_WNS"),
        ("routed_wns_ns", "FPDSP_ROUTED_WNS"),
    ):
        match = re.search(rf"(?m)^{re.escape(marker)}=([^\s]+)\s*$", output_text)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                value = None
            if value is not None and math.isfinite(value):
                observations[key] = value
    attestation["qor_observation"] = {
        "version": 1,
        "physical_profile": physical_profile,
        "place_directive": physical_profiles[physical_profile],
        "contest_clock": "clk_fpl26contest",
        "placed_wns_ns": observations.get("placed_wns_ns"),
        "routed_wns_ns": observations.get("routed_wns_ns"),
        "role": "advisory_physical_feedback_not_acceptance_authority",
    }
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_proved_recurrence_eco(request: dict) -> str:
    """Apply one fixed recurrence ECO transaction and restore the exact source."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "facts", "candidate", "placement",
        "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("recurrence ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    for name, path in (("source", source), ("output parent", output.parent)):
        if "}" in str(path) or "\n" in str(path) or "\r" in str(path):
            raise ValueError(f"recurrence ECO {name} path contains unsafe characters")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("recurrence ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("recurrence ECO output path is unsafe")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("recurrence ECO output path is not a regular file")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("recurrence ECO source identity is invalid")
    if _sha256_file(source) != source_sha256:
        raise ValueError("recurrence ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError("recurrence ECO route_timeout_s must be in 1..3600")
    facts = request["facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    try:
        from FDAgents.recurrence_action import build_recurrence_topology_manifest
    except ImportError as exc:
        raise RuntimeError("FDAgents recurrence proof runtime is unavailable") from exc
    if not isinstance(facts, dict) or facts.get("design_sha256") != source_sha256:
        raise ValueError("recurrence ECO facts are stale or malformed")
    topology = build_recurrence_topology_manifest(facts, candidate, placement)
    if topology.get("missing_emission_contracts") != [
        "connected_temporary_primitive_slot_oracle_replay"
    ]:
        raise ValueError("recurrence ECO source contracts are incomplete")
    eco_tcl = build_recurrence_eco_tcl(
        facts,
        candidate,
        placement,
        output_dcp=str(output),
        run_route=True,
    )
    postcheck_tcl = build_recurrence_postcheck_tcl(facts, candidate, placement)
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_RECURRENCE_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "recurrence ECO did not produce its completion artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_RECURRENCE_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "recurrence ECO postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"recurrence ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence ECO source changed during transaction")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence ECO candidate identity changed")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_srl_recurrence_cut_v1",
        "source": {
            "dcp_sha256": source_sha256,
            "region_sha256": facts.get("region_sha256"),
        },
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": candidate["authorization"]["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": {
            "joint_connected_topology_replayed": True,
            "all_addresses_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": True,
        },
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def apply_proved_recurrence_family_eco(request: dict) -> str:
    """Apply a complete freshly recovered recurrence family atomically."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "family_facts", "candidate",
        "placement", "output_dcp", "route_timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("recurrence family ECO request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    output = Path(str(request["output_dcp"])).expanduser().resolve()
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("recurrence family ECO source is not a regular DCP")
    if output == source or output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("recurrence family ECO output path is unsafe")
    source_sha256 = str(request["source_dcp_sha256"])
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or _sha256_file(source) != source_sha256:
        raise ValueError("recurrence family ECO source identity mismatch")
    timeout = request["route_timeout_s"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 3600:
        raise ValueError("recurrence family ECO route_timeout_s must be in 1..3600")
    family = request["family_facts"]
    candidate = request["candidate"]
    placement = request["placement"]
    if (
        not isinstance(family, dict)
        or family.get("kind") != "dsp_recurrence_family_contract_facts"
        or family.get("design_sha256") != source_sha256
        or family.get("fresh_rediscovery") is not True
        or family.get("mutation_eligible") is not False
    ):
        raise ValueError("recurrence family facts are stale or malformed")
    family_payload = dict(family)
    claimed_family_sha = family_payload.pop("family_contract_sha256", None)
    for key in ("family_id", "fresh_rediscovery", "runtime_s"):
        family_payload.pop(key, None)
    if not isinstance(claimed_family_sha, str) or _stable_digest(family_payload) != claimed_family_sha:
        raise ValueError("recurrence family facts digest mismatch")
    try:
        from FDAgents.recurrence_action import build_recurrence_family_topology_manifest
    except ImportError as exc:
        raise RuntimeError("FDAgents recurrence family proof runtime is unavailable") from exc
    topology = build_recurrence_family_topology_manifest(family, candidate, placement)
    if topology.get("emission_eligible") is not True:
        raise ValueError("recurrence family source contracts are incomplete")
    eco_tcl = build_recurrence_family_eco_tcl(
        family, candidate, placement, output_dcp=str(output)
    )
    postcheck_tcl = build_recurrence_family_postcheck_tcl(
        family, candidate, placement
    )
    failure = None
    candidate_sha256 = None
    candidate_reopened = False
    source_reopened = False
    try:
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
        _design_open = True
        output.unlink(missing_ok=True)
        eco_output = run_tcl_script(eco_tcl, timeout=float(timeout))
        if "FDAGENTS_RECURRENCE_FAMILY_ECO_DONE" not in eco_output or not output.is_file():
            raise RuntimeError(
                "recurrence family ECO did not produce its artifact; "
                f"vivado_output={eco_output[-4000:]}"
            )
        candidate_sha256 = _sha256_file(output)
        close_current_design()
        run_tcl_command(f"open_checkpoint {{{output}}}", timeout=600)
        _design_open = True
        candidate_reopened = True
        postcheck_output = run_tcl_script(
            postcheck_tcl, timeout=min(float(timeout), 900.0)
        )
        if "FDAGENTS_RECURRENCE_FAMILY_POSTCHECK_DONE" not in postcheck_output:
            raise RuntimeError(
                "recurrence family postcheck did not complete; "
                f"vivado_output={postcheck_output[-4000:]}"
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            source_reopened = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"recurrence family ECO transaction failed: {failure}") from failure
    if _sha256_file(source) != source_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence family ECO source changed")
    if candidate_sha256 is None or _sha256_file(output) != candidate_sha256:
        output.unlink(missing_ok=True)
        raise RuntimeError("recurrence family ECO candidate identity changed")
    attestation = {
        "kind": "operator_rewrite_emitted_topology_attestation",
        "version": 1,
        "status": "proved",
        "authority": "candidate_measurement_only",
        "template": "dsp_srl_recurrence_cut_v1",
        "source": {"dcp_sha256": source_sha256, "region_sha256": claimed_family_sha},
        "candidate": {"dcp_sha256": candidate_sha256},
        "authorization_id": candidate["authorization"]["certificate_id"],
        "placement_manifest_sha256": placement["manifest_sha256"],
        "topology_manifest_sha256": topology["manifest_sha256"],
        "checks": {
            "joint_connected_topology_replayed": True,
            "all_addresses_replayed": True,
            "exact_namespace_inventory": True,
            "candidate_checkpoint_reopened": candidate_reopened,
            "source_checkpoint_reopened": source_reopened,
            "relevant_nets_fully_routed": True,
        },
    }
    attestation["attestation_id"] = _stable_digest(attestation)
    return json.dumps(attestation, sort_keys=True, separators=(",", ":"))


def report_utilization_for_pblock(timeout: float = 300.0) -> str:
    """
    Get detailed resource utilization report for pblock sizing.

    Returns utilization of key resources:
    - LUTs, FFs, DSPs, BRAMs, URAMs
    - Formatted for easy parsing and pblock size calculation

    Uses ``get_cells -hier -filter {REF_NAME =~ PATTERN}`` directly instead
    of parsing ``report_utilization`` text output. Two reasons not to use
    PRIMITIVE_GROUP:
      1. On out-of-context designs LUT/FLOP_LATCH group filters have been
         observed to return zero matches even when the design has LUTs
         and FFs (reported in amd_mini-isp, rounds after placement).
      2. PRIMITIVE_GROUP == ARITHMETIC groups **both** DSP48E* and
         CARRY4/CARRY8 into a single bucket, so a naive count was
         reporting 360 "DSPs" on amd_mini-isp (40 DSPs + 320 carries)
         and producing a target that no fabric region could possibly hold.

    REF_NAME glob patterns are stable across Vivado versions and
    in-context / OOC modes, and give clean per-primitive buckets.

    Note on DSPs: ``REF_NAME =~ DSP*`` would also match the sub-primitives
    that Vivado decomposes each DSP48E2 macro into on open_checkpoint
    (DSP_ALU, DSP_A_B_DATA, DSP_C_DATA, DSP_MULTIPLIER, DSP_M_DATA,
    DSP_OUTPUT, DSP_PREADD, DSP_PREADD_DATA — 9 sub-cells per macro).
    We anchor to the macro names explicitly (DSP48E1 / DSP48E2 / DSP58)
    so the count equals the number of DSP *sites* a design needs, not
    the number of sub-primitive leaves.
    """
    cmd = (
        "set counts {}; "
        "lappend counts \"LUT=[llength [get_cells -quiet -hier "
        "-filter {IS_PRIMITIVE && REF_NAME =~ LUT*}]]\"; "
        "lappend counts \"FF=[llength [get_cells -quiet -hier "
        "-filter {IS_PRIMITIVE && (REF_NAME =~ FD* || REF_NAME =~ LD*)}]]\"; "
        "lappend counts \"DSP=[llength [get_cells -quiet -hier "
        "-filter {IS_PRIMITIVE && "
        "(REF_NAME == DSP48E1 || REF_NAME == DSP48E2 || REF_NAME == DSP58)}]]\"; "
        "lappend counts \"BRAM=[llength [get_cells -quiet -hier "
        "-filter {IS_PRIMITIVE && REF_NAME =~ RAMB*}]]\"; "
        "lappend counts \"URAM=[llength [get_cells -quiet -hier "
        "-filter {IS_PRIMITIVE && REF_NAME =~ URAM*}]]\"; "
        "puts [join $counts \" \"]"
    )

    try:
        report = run_tcl_command(cmd, timeout=timeout)
    except Exception as e:
        return f"Error generating utilization report: {str(e)}"

    resources = {"LUT": 0, "FF": 0, "DSP": 0, "BRAM": 0, "URAM": 0}
    for key in resources:
        match = re.search(rf"\b{key}=(\d+)", report)
        if match:
            resources[key] = int(match.group(1))

    # Format output (schema is stable — callers may parse this text)
    result_lines = [
        "=== Design Resource Utilization ===",
        "",
        f"LUTs:  {resources['LUT']:8,}",
        f"FFs:   {resources['FF']:8,}",
        f"DSPs:  {resources['DSP']:8,}",
        f"BRAMs: {resources['BRAM']:8,}",
        f"URAMs: {resources['URAM']:8,}",
        "",
        f"=== {PBLOCK_RESOURCE_MULTIPLIER}x Multiplier (for pblock sizing) ===",
        "",
        f"LUTs:  {int(resources['LUT'] * PBLOCK_RESOURCE_MULTIPLIER):8,}",
        f"FFs:   {int(resources['FF'] * PBLOCK_RESOURCE_MULTIPLIER):8,}",
        f"DSPs:  {int(resources['DSP'] * PBLOCK_RESOURCE_MULTIPLIER):8,}",
        f"BRAMs: {int(resources['BRAM'] * PBLOCK_RESOURCE_MULTIPLIER):8,}",
        f"URAMs: {int(resources['URAM'] * PBLOCK_RESOURCE_MULTIPLIER):8,}",
    ]

    return "\n".join(result_lines)


def suggest_slice_pblock_ranges(
    max_candidates: int = 8,
    timeout: float = 300.0,
) -> str:
    """
    Return slice-only pblock candidates derived from the current placed design.

    This is read-only. It is intended for designs whose placed primitives are
    overwhelmingly SLICE based; if hard macros are present, the caller should
    prefer the RapidWright fabric-region pblock flow.
    """
    run_tcl_command("puts {slice_pblock_candidates_start}", timeout=5)
    tcl = (
        "set slice_sites {}; "
        "set hard_counts [dict create DSP48E2 0 RAMB18 0 RAMB36 0 URAM288 0]; "
        "set dev_x_min 999999; set dev_x_max -1; set dev_y_min 999999; set dev_y_max -1; "
        "foreach s [get_sites -quiet SLICE_X*Y*] { "
        "  set site_name [get_property NAME $s]; "
        "  if {$site_name eq \"\"} {set site_name $s}; "
        "  if {[regexp {^SLICE_X([0-9]+)Y([0-9]+)$} $site_name -> sx sy]} { "
        "    if {$sx < $dev_x_min} {set dev_x_min $sx}; "
        "    if {$sx > $dev_x_max} {set dev_x_max $sx}; "
        "    if {$sy < $dev_y_min} {set dev_y_min $sy}; "
        "    if {$sy > $dev_y_max} {set dev_y_max $sy}; "
        "  } "
        "}; "
        "foreach c [get_cells -hierarchical -quiet -filter {IS_PRIMITIVE && LOC != \"\"}] { "
        "  set loc [get_property LOC $c]; "
        "  if {[regexp {^SLICE_X([0-9]+)Y([0-9]+)$} $loc -> x y]} { "
        "    lappend slice_sites $loc; "
        "  } elseif {[regexp {^(DSP48E2|RAMB18|RAMB36|URAM288)_} $loc -> kind]} { "
        "    dict incr hard_counts $kind; "
        "  } "
        "}; "
        "puts \"SLICE_SITES=[join [lsort -unique $slice_sites] ,]\"; "
        "puts \"HARD_COUNTS=[dict get $hard_counts DSP48E2],[dict get $hard_counts RAMB18],[dict get $hard_counts RAMB36],[dict get $hard_counts URAM288]\"; "
        "puts \"DEVICE_BOUNDS=$dev_x_min,$dev_x_max,$dev_y_min,$dev_y_max\""
    )
    try:
        output = run_tcl_command(tcl, timeout=timeout)
    except Exception as e:
        return json.dumps({"error": f"Error collecting placed sites: {str(e)}"})

    slice_sites = []
    hard_counts = {"DSP48E2": 0, "RAMB18": 0, "RAMB36": 0, "URAM288": 0}
    device_bounds = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("SLICE_SITES="):
            payload = line.split("=", 1)[1].strip()
            slice_sites = [site for site in payload.split(",") if site]
        elif line.startswith("HARD_COUNTS="):
            values = line.split("=", 1)[1].split(",")
            keys = ["DSP48E2", "RAMB18", "RAMB36", "URAM288"]
            for idx, key in enumerate(keys):
                if idx < len(values) and values[idx].strip().isdigit():
                    hard_counts[key] = int(values[idx])
        elif line.startswith("DEVICE_BOUNDS="):
            values = [int(v) for v in line.split("=", 1)[1].split(",") if v]
            if len(values) == 4 and values[1] >= values[0] and values[3] >= values[2]:
                device_bounds = {
                    "x_min": values[0],
                    "x_max": values[1],
                    "y_min": values[2],
                    "y_max": values[3],
                }

    result = build_slice_pblock_candidates(
        slice_sites,
        device_bounds=device_bounds,
        max_candidates=max_candidates,
    )
    result["hard_macro_counts"] = hard_counts
    result["slice_only_recommended"] = sum(hard_counts.values()) == 0
    result["note"] = (
        "Use pblock_sweep early for route-dominated slice-only designs; use "
        "dynamic RapidWright pblock when hard macros are present."
    )
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def validate_pblock_resources(pblock_name: str) -> Dict[str, Any]:
    """
    Validate that a pblock has sufficient resources for the design primitives assigned to it.

    Returns:
        Dictionary with validation results including:
        - is_valid: True if resources are sufficient
        - resource_checks: Dict of resource type -> {required, available, margin}
        - errors: List of resource insufficiency errors
    """
    import re

    # Get pblock properties
    pblock_info = run_tcl_command(f"report_property [get_pblocks {pblock_name}]", timeout=30.0)

    # Parse PRIMITIVE_COUNT (total primitives assigned to pblock)
    primitive_count = 0
    cell_count = 0
    for line in pblock_info.split('\n'):
        if 'PRIMITIVE_COUNT' in line:
            parts = line.split()
            for p in parts:
                try:
                    primitive_count = int(p)
                    break
                except ValueError:
                    continue
        if 'CELL_COUNT' in line:
            parts = line.split()
            for p in parts:
                try:
                    cell_count = int(p)
                    break
                except ValueError:
                    continue

    # Run DRC to check for resource issues (this is the authoritative check)
    # Use file-based output to avoid buffering issues with -return_string
    import tempfile
    import time as time_module

    temp_dir = os.path.dirname(os.path.abspath(__file__))
    drc_file = os.path.join(temp_dir, f"drc_check_{pblock_name}.rpt")

    drc_cmd = f"report_drc -checks {{UTLZ-1 UTLZ-2}} -file {{{drc_file}}}"
    run_tcl_command(drc_cmd, timeout=60.0)

    # Wait for file to be written using Tcl file size check
    drc_result = ""
    for retry in range(10):
        size_result = run_tcl_command(f"file size {{{drc_file}}}", timeout=10.0)
        try:
            file_size = int(size_result.strip())
            if file_size > 0:
                logger.info(f"DRC file ready: {file_size} bytes")
                break
        except ValueError:
            pass
        time_module.sleep(0.3)

    # Read the DRC file
    try:
        with open(drc_file, 'r') as f:
            drc_result = f.read()
        # Clean up temp file
        os.remove(drc_file)
    except Exception as e:
        logger.warning(f"Error reading DRC file: {e}")

    # Parse DRC results for resource errors
    errors = []
    resource_issues = {}

    logger.info(f"DRC result length: {len(drc_result)} chars")

    # Debug: show what we're checking
    utlz1_found = "UTLZ-1" in drc_result
    error_found = "Error" in drc_result
    logger.info(f"DRC content check: 'UTLZ-1' in result={utlz1_found}, 'Error' in result={error_found}")
    if utlz1_found or error_found:
        # Log first 600 chars to understand format
        logger.info(f"DRC result preview: {drc_result[:600]}")

    # First, simple check: if UTLZ-1 appears in the output, we have a hard error
    # The DRC summary table shows: "| UTLZ-1 | Error            |"
    # Also check for "UTLZ-1#" which indicates individual errors like "UTLZ-1#1 Error"
    has_utlz1_error = utlz1_found and error_found
    has_utlz2_warning = "UTLZ-2" in drc_result

    # Log what we found
    logger.info(f"DRC check: has_utlz1_error={has_utlz1_error}, has_utlz2_warning={has_utlz2_warning}")

    # Look for UTLZ-1 errors (hard over-utilization)
    # Format: "LUT6 over-utilized in Pblock ... requires 24377 of such cell types but only 6520 compatible"
    utlz1_pattern = r"(\w+(?:\s+\w+)*?) over-utilized.*?requires (\d+) of such cell types but only (\d+) compatible"
    for match in re.finditer(utlz1_pattern, drc_result, re.IGNORECASE | re.DOTALL):
        resource_type = match.group(1).strip()
        required = int(match.group(2))
        available = int(match.group(3))
        resource_issues[resource_type] = {
            'required': required,
            'available': available,
            'margin': available / required if required > 0 else 999,
            'shortage': required - available
        }
        errors.append(f"{resource_type}: requires {required}, only {available} available (shortage: {required - available})")
        logger.info(f"Found UTLZ-1 error: {resource_type} requires {required}, available {available}")

    # Look for UTLZ-2 warnings (over-utilized but placer might handle)
    # Format: "LUT as Logic over-utilized ... has 31370 LUT as Logic(s) assigned ... only 6520 ... available"
    utlz2_pattern = r"(\w+(?:\s+\w+)*?) over-utilized.*?has (\d+).*?only (\d+).*?available"
    for match in re.finditer(utlz2_pattern, drc_result, re.IGNORECASE | re.DOTALL):
        resource_type = match.group(1).strip()
        assigned = int(match.group(2))
        available = int(match.group(3))
        if resource_type not in resource_issues:  # Don't override UTLZ-1 errors
            resource_issues[resource_type] = {
                'required': assigned,
                'available': available,
                'margin': available / assigned if assigned > 0 else 999,
                'shortage': assigned - available,
                'warning_only': True
            }
            errors.append(f"{resource_type}: {assigned} assigned, only {available} available (may cause issues)")
            logger.info(f"Found UTLZ-2 warning: {resource_type} has {assigned}, available {available}")

    # Fallback: if we detected UTLZ-1 errors but couldn't parse details, add generic error
    if has_utlz1_error and not resource_issues:
        logger.warning("UTLZ-1 error detected but could not parse details")
        errors.append("UTLZ-1 error detected - pblock resources insufficient")
        resource_issues['unknown'] = {'required': 1, 'available': 0, 'margin': 0, 'shortage': 1}

    # is_valid only if there are no UTLZ-1 errors (hard failures)
    hard_errors = [e for e in resource_issues.values() if not e.get('warning_only', False)]
    is_valid = len(hard_errors) == 0 and not has_utlz1_error

    logger.info(f"Pblock validation: is_valid={is_valid}, hard_errors={len(hard_errors)}, total_issues={len(resource_issues)}")

    return {
        'is_valid': is_valid,
        'primitive_count': primitive_count,
        'cell_count': cell_count,
        'resource_issues': resource_issues,
        'errors': errors,
        'drc_output': drc_result[:1000] if len(drc_result) > 1000 else drc_result
    }


def expand_pblock_range(ranges: str, expansion_factor: Optional[float] = None) -> str:
    """
    Expand a pblock range by the given factor (default from config).

    Parses SLICE_X#Y#:SLICE_X#Y# format and expands the range.
    Area scales with the square of the linear factor, so expansion_factor=2.0 gives ~4x area.
    """
    if expansion_factor is None:
        expansion_factor = PBLOCK_EXPAND_FACTOR
    import re

    expanded_parts = []

    logger.info(f"Expanding pblock range by factor {expansion_factor:.2f}x: {ranges}")

    for part in ranges.split():
        # Match pattern like SLICE_X67Y220:SLICE_X80Y272
        match = re.match(r'(\w+)_X(\d+)Y(\d+):(\w+)_X(\d+)Y(\d+)', part)
        if match:
            site_type = match.group(1)
            x_min = int(match.group(2))
            y_min = int(match.group(3))
            x_max = int(match.group(5))
            y_max = int(match.group(6))

            # Calculate expansion
            x_span = x_max - x_min
            y_span = y_max - y_min

            # Expand around the center
            x_center = (x_min + x_max) / 2
            y_center = (y_min + y_max) / 2

            new_x_span = int(x_span * expansion_factor)
            new_y_span = int(y_span * expansion_factor)

            new_x_min = max(0, int(x_center - new_x_span / 2))
            new_x_max = int(x_center + new_x_span / 2)
            new_y_min = max(0, int(y_center - new_y_span / 2))
            new_y_max = int(y_center + new_y_span / 2)

            logger.info(f"  {site_type}: X{x_min}Y{y_min}:X{x_max}Y{y_max} -> X{new_x_min}Y{new_y_min}:X{new_x_max}Y{new_y_max}")
            expanded_parts.append(f"{site_type}_X{new_x_min}Y{new_y_min}:{site_type}_X{new_x_max}Y{new_y_max}")
        else:
            # Keep non-matching parts as-is
            logger.info(f"  Keeping as-is: {part}")
            expanded_parts.append(part)

    result = " ".join(expanded_parts)
    logger.info(f"Expanded pblock range: {result}")
    return result


def create_and_apply_pblock(
    pblock_name: str,
    ranges: str,
    apply_to: str = "current_design",
    is_soft: bool = False,
    timeout: float = 300.0,
    validate_resources: bool = True,
    max_expansion_attempts: int = 3
) -> str:
    """
    Create a pblock and apply it to the design with resource validation.

    Args:
        pblock_name: Name for the pblock (e.g., "pblock_tight")
        ranges: Pblock range specification (e.g., "SLICE_X0Y0:SLICE_X100Y100" or
                "CLOCKREGION_X0Y0:CLOCKREGION_X2Y3")
        apply_to: What to apply pblock to - "current_design" applies to all cells in the design,
                 or provide a cell pattern (e.g., "design_1_wrapper_i/*")
        is_soft: If False, sets IS_SOFT property to 0 (hard constraint)
        validate_resources: If True, validate resources and auto-expand if needed
        max_expansion_attempts: Maximum times to try expanding the pblock

    Returns:
        Status message
    """
    result_lines = []
    current_ranges = ranges

    logger.info(f"Creating pblock '{pblock_name}' with range: {ranges}")
    logger.info(f"validate_resources={validate_resources}, max_expansion_attempts={max_expansion_attempts}")

    for attempt in range(max_expansion_attempts + 1):
        try:
            logger.info(f"Pblock creation attempt {attempt+1}/{max_expansion_attempts+1}")

            # Delete existing pblock if it exists (for retry attempts)
            if attempt > 0:
                try:
                    run_tcl_command(f"delete_pblocks [get_pblocks {pblock_name}]", timeout=10.0)
                    result_lines.append(f"\n=== Retry attempt {attempt} with expanded pblock ===")
                except Exception:
                    pass  # Pblock might not exist

            # Create the pblock
            create_cmd = f"create_pblock {pblock_name}"
            result = run_tcl_command(create_cmd, timeout=30.0)
            result_lines.append(f"Created pblock: {pblock_name}")

            # Add the range to the pblock
            resize_cmd = f"resize_pblock {pblock_name} -add {{{current_ranges}}}"
            result = run_tcl_command(resize_cmd, timeout=30.0)
            result_lines.append(f"Set pblock range: {current_ranges}")

            # Set IS_SOFT property
            soft_value = "1" if is_soft else "0"
            soft_cmd = f"set_property IS_SOFT {soft_value} [get_pblocks {pblock_name}]"
            result = run_tcl_command(soft_cmd, timeout=30.0)
            result_lines.append(f"Set IS_SOFT = {soft_value}")

            # Apply pblock to cells
            if apply_to == "current_design":
                add_cmd = f"add_cells_to_pblock {pblock_name} [get_cells -hierarchical]"
            else:
                add_cmd = f"add_cells_to_pblock {pblock_name} [get_cells {apply_to}]"

            result = run_tcl_command(add_cmd, timeout=timeout)
            result_lines.append(f"Applied pblock to: {apply_to}")

            # Validate resources if requested
            if validate_resources:
                validation = validate_pblock_resources(pblock_name)

                if not validation['is_valid']:
                    result_lines.append(f"\n⚠ Resource validation FAILED:")
                    for error in validation['errors']:
                        result_lines.append(f"  - {error}")

                    if attempt < max_expansion_attempts:
                        # Calculate expansion factor based on worst shortage
                        worst_margin = min(
                            (issue['margin'] for issue in validation['resource_issues'].values()),
                            default=1.0
                        )
                        # Expand by inverse of margin plus some buffer
                        expansion_factor = max(PBLOCK_EXPAND_FACTOR, 1.0 / worst_margin * 1.3)
                        result_lines.append(f"\n  Expanding pblock by factor {expansion_factor:.2f}x...")

                        current_ranges = expand_pblock_range(current_ranges, expansion_factor)
                        continue  # Try again with expanded pblock
                    else:
                        result_lines.append(f"\n  Maximum expansion attempts reached. Consider using a larger region.")
                else:
                    result_lines.append(f"\n✓ Resource validation PASSED")

            # Verify the pblock
            verify_cmd = f"report_property [get_pblocks {pblock_name}]"
            verify_result = run_tcl_command(verify_cmd, timeout=30.0)

            result_lines.extend([
                "",
                "=== Pblock Created Successfully ===",
                f"Name: {pblock_name}",
                f"Range: {current_ranges}",
                f"IS_SOFT: {soft_value}",
                f"Applied to: {apply_to}",
                "",
                "Next steps:",
                "1. Run place_design to re-place with pblock constraint",
                "2. Run route_design to route the newly placed design",
                "3. Check timing with report_timing_summary"
            ])

            return "\n".join(result_lines)

        except Exception as e:
            result_lines.append(f"Error in attempt {attempt}: {str(e)}")
            if attempt >= max_expansion_attempts:
                return f"Error creating/applying pblock: {str(e)}\n" + "\n".join(result_lines)

    return "\n".join(result_lines)


def _banked_predicate_paths(
    request: dict,
    *,
    mutation: bool,
    timeout_max_s: Optional[float] = 4800.0,
) -> tuple[Path, Path, Optional[Path]]:
    required = {
        "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
    }
    if mutation:
        required.add("output_dcp")
    optional = (
        {"probe_report_path"}
        if mutation
        else {"reuse_bound_report"}
    )
    if (
        not isinstance(request, dict)
        or not required.issubset(request)
        or not set(request).issubset(required | optional)
        or (
            "reuse_bound_report" in request
            and not isinstance(request["reuse_bound_report"], bool)
        )
    ):
        raise ValueError("banked predicate request has an unsupported schema")
    source = Path(str(request["dcp_path"])).expanduser().resolve()
    report = Path(str(request["report_path"])).expanduser().resolve()
    output = (
        Path(str(request["output_dcp"])).expanduser().resolve()
        if mutation else None
    )
    paths = [source, report, *([output] if output is not None else [])]
    if any(
        path is None
        or any(char in str(path) for char in ("}", "\r", "\n"))
        for path in paths
    ):
        raise ValueError("banked predicate path is unsafe")
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("banked predicate source is not a regular DCP")
    source_sha256 = str(request["source_dcp_sha256"])
    if (
        re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or _sha256_file(source) != source_sha256
    ):
        raise ValueError("banked predicate source identity mismatch")
    if report.parent.is_symlink() or not report.parent.is_dir():
        raise ValueError("banked predicate report parent is unsafe")
    if output is not None and (
        output == source
        or output.parent.is_symlink()
        or not output.parent.is_dir()
    ):
        raise ValueError("banked predicate output path is unsafe")
    timeout = request["timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) < 1
        or (
            timeout_max_s is not None
            and float(timeout) > float(timeout_max_s)
        )
    ):
        bound = (
            f"1..{float(timeout_max_s):g}"
            if timeout_max_s is not None
            else "a finite number >= 1"
        )
        raise ValueError(f"banked predicate timeout_s must be {bound}")
    return source, report, output


def recover_banked_select_predicate_contract(request: dict) -> str:
    """Run anonymous data/control recovery and restore the exact seed."""
    global _design_open
    source, report, _ = _banked_predicate_paths(request, mutation=False)
    # Cache admission is already fail-closed on source SHA, report SHA, probe
    # ABI, cache digest, and a fresh parser replay.  Try it for every caller so
    # Luna's public read-only probe need not know a framework-private cache
    # parameter.  A miss still performs the full Vivado recovery below.
    cached = load_banked_predicate_probe_cache(
        report, str(request["source_dcp_sha256"])
    )
    if cached is not None:
        cached = dict(cached)
        cached["probe_cache_reused"] = True
        return json.dumps(cached, sort_keys=True, separators=(",", ":"))
    report.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    probe_output = ""
    try:
        close_current_design()
        _design_open = False
        script = build_banked_predicate_script(
            "probe_banked_select_predicate.tcl", (source, report)
        )
        probe_output = run_tcl_script(
            script, timeout=float(request["timeout_s"])
        )
        _design_open = False
        if not report.is_file():
            tail = str(probe_output or "")[-1200:]
            raise RuntimeError(
                "banked predicate probe did not emit its report; "
                f"Vivado tail: {tail}"
            )
        result = parse_banked_predicate_probe_report(report)
        result.pop("_private_contract_digest", None)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None and restored and result is None:
        # The fixed probe intentionally fails closed when the live RAM-control
        # wall does not close into this exact transaction.  Those shape
        # refusals are analyzer data, not Vivado/MCP infrastructure failures.
        # Keep unexpected Tcl/parser failures loud.
        structural_refusals = (
            ("critical timing wall has no RAM write-enable endpoint",
             "NO_LIVE_RAM_WRITE_ENABLE_WALL"),
            ("no BANKED_SELECT_PREDICATE_UPDATE structural candidate found",
             "NO_BANKED_SELECT_PREDICATE_CANDIDATE"),
            ("active comparator has no complete bank-local predicate closure",
             "INCOMPLETE_BANK_LOCAL_PREDICATE_CLOSURE"),
            ("banked predicate specialization needs at least two structurally aligned banks",
             "INSUFFICIENT_ALIGNED_BANKS"),
            ("comparator carry has no LUT source topology",
             "COMPARATOR_LUT_TOPOLOGY_ABSENT"),
            ("comparator has no multi-bank selected-word input",
             "MULTIBANK_SELECTED_WORD_INPUT_ABSENT"),
            ("global predicate has no preserved non-bank-local consumer",
             "GLOBAL_PREDICATE_CONSUMER_ABSENT"),
            ("global predicate carry output is ambiguous",
             "GLOBAL_PREDICATE_DRIVER_AMBIGUOUS"),
        )
        detail = f"{failure}\n{probe_output}"
        refusal = next(
            ((marker, reason_code) for marker, reason_code in structural_refusals
             if marker in detail),
            None,
        )
        if refusal is not None:
            marker, reason_code = refusal
            return json.dumps({
                "status": "rejected",
                "kind": "banked_select_predicate_operator_contract",
                "mutation_eligible": False,
                "read_only_after_restore": True,
                "reason_code": reason_code,
                "message": marker,
            }, sort_keys=True, separators=(",", ":"))
    if failure is not None or result is None or not restored:
        raise RuntimeError(
            f"banked predicate read-only recovery failed: {failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    write_banked_predicate_probe_cache(
        report, str(request["source_dcp_sha256"]), result
    )
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_banked_select_predicate_update(request: dict) -> str:
    """Apply the fresh generic contract, route, reopen, and restore the seed."""
    global _design_open
    source, report, output = _banked_predicate_paths(request, mutation=True)
    assert output is not None
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    tcl_output = ""
    try:
        close_current_design()
        _design_open = False
        script = build_banked_predicate_script(
            "apply_banked_select_predicate_update.tcl",
            (source, output, report),
        )
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if not output.is_file() or not report.is_file():
            raise RuntimeError("banked predicate backend did not emit its candidate")
        result = parse_banked_predicate_backend_report(report)
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"banked predicate ECO transaction failed: {failure or 'source restore failed'}"
        ) from failure
    result.update({
        "source_dcp_sha256": str(request["source_dcp_sha256"]),
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_registered_dsp_lane_contract(request: dict) -> str:
    """Recover anonymous product/control/physical-risk facts without mutation."""
    global _design_open
    source, report, _ = _banked_predicate_paths(request, mutation=False)
    report.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_registered_dsp_lane_script((source, report))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if not report.is_file():
            raise RuntimeError("registered DSP lane probe emitted no report")
        result = parse_registered_dsp_lane_probe_report(report)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        raise RuntimeError(
            "registered DSP lane read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_registered_dsp_lane_rebuild(request: dict) -> str:
    """Recover, absorb, route and reopen one complete DSP product family."""
    global _design_open
    source, report, output = _banked_predicate_paths(request, mutation=True)
    assert output is not None
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_registered_dsp_lane_script((source, output, report))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if not output.is_file() or not report.is_file():
            raise RuntimeError("registered DSP lane backend emitted no routed candidate")
        result = parse_registered_dsp_lane_backend_report(report)
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "registered DSP lane ECO transaction failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "source_dcp_sha256": str(request["source_dcp_sha256"]),
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "registered_control_transaction_preserved": True,
        "primitive_replay_passed": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _control_state_private_path(
    value: object, *, parent: Optional[Path] = None, require_file: bool = False
) -> Path:
    path = Path(str(value)).expanduser().resolve()
    if (
        any(char in str(path) for char in ("}", "\r", "\n"))
        or (parent is not None and path.parent != parent)
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or (require_file and (
            path.is_symlink() or not path.is_file() or path.stat().st_size <= 0
        ))
    ):
        raise ValueError("control-state private path is unsafe")
    return path


def recover_control_to_data_state_equation_contract(request: dict) -> str:
    """Recover anonymous synchronous state equations and retain exact selectors."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "report_path", "manifest_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("control-state probe request has an unsupported schema")
    source, report, _ = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
        )},
        mutation=False,
    )
    manifest = _control_state_private_path(
        request["manifest_path"], parent=report.parent
    )
    report.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_control_state_equation_script((
            source, report, manifest, str(request["source_dcp_sha256"]),
        ))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if (
            not report.is_file() or report.stat().st_size <= 0
            or not manifest.is_file() or manifest.stat().st_size <= 0
        ):
            raise RuntimeError("control-state probe emitted no contract")
        result = parse_control_state_equation_probe_report(
            report,
            manifest_path=manifest,
            expected_source_sha256=str(request["source_dcp_sha256"]),
        )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        report.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            "control-state read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_control_to_data_state_equation(request: dict) -> str:
    """Rewrite one Luna-selected, privately resolved synchronous state family."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest_path", "probe_report_path",
        "family_ref", "output_dcp", "report_path", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("control-state mutation request has an unsupported schema")
    source, report, output = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
            "timeout_s",
        )},
        mutation=True,
    )
    assert output is not None
    family_ref = str(request["family_ref"])
    if re.fullmatch(r"control_state_[1-9][0-9]*", family_ref) is None:
        raise ValueError("control-state family_ref is invalid")
    manifest_path = _control_state_private_path(
        request["manifest_path"], require_file=True
    )
    probe_report_path = _control_state_private_path(
        request["probe_report_path"], parent=manifest_path.parent,
        require_file=True,
    )
    manifest = parse_control_state_equation_manifest(
        manifest_path,
        expected_source_sha256=str(request["source_dcp_sha256"]),
    )
    contract = parse_control_state_equation_probe_report(
        probe_report_path,
        manifest_path=manifest_path,
        expected_source_sha256=str(request["source_dcp_sha256"]),
    )
    if family_ref not in manifest["families"]:
        raise ValueError("control-state family_ref is absent from the current seed")
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_control_state_equation_script((
            source, manifest_path, family_ref, output, report,
            str(request["source_dcp_sha256"]),
        ))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if (
            not output.is_file() or output.stat().st_size <= 0
            or not report.is_file() or report.stat().st_size <= 0
        ):
            raise RuntimeError("control-state backend emitted no routed candidate")
        result = parse_control_state_equation_backend_report(
            report,
            manifest=manifest,
            family_ref=family_ref,
            contract=contract,
        )
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "control-state ECO transaction failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "private_manifest_identity_replayed": True,
        "registered_control_transaction_preserved": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_ff_srl_boundary_bridge_contract(request: dict) -> str:
    """Recover anonymous exact direct-FF transport families read-only."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "report_path", "manifest_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("FF-to-SRL probe request has an unsupported schema")
    source, report, _ = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
        )},
        mutation=False,
    )
    manifest = _control_state_private_path(
        request["manifest_path"], parent=report.parent
    )
    report.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_ff_srl_boundary_bridge_script((
            source, report, manifest, str(request["source_dcp_sha256"]),
        ))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if (
            not report.is_file() or report.stat().st_size <= 0
            or not manifest.is_file() or manifest.stat().st_size <= 0
        ):
            raise RuntimeError("FF-to-SRL probe emitted no contract")
        result = parse_ff_srl_boundary_bridge_probe_report(
            report,
            manifest_path=manifest,
            expected_source_sha256=str(request["source_dcp_sha256"]),
        )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        report.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            "FF-to-SRL read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_memory_write_buffer_forward_contract(request: dict) -> str:
    """Recover supported live BRAM write/read forwarding boundaries read-only."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "report_path", "manifest_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("memory-forward probe request has an unsupported schema")
    source, report, _ = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
        )},
        mutation=False,
    )
    manifest = _control_state_private_path(
        request["manifest_path"], parent=report.parent
    )
    extraction = report.parent / (
        ".memory_forward_extract_" + str(request["source_dcp_sha256"])[:16]
    )
    if extraction.exists() and (extraction.is_symlink() or not extraction.is_dir()):
        raise ValueError("memory-forward extraction root is unsafe")
    extraction.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_memory_forward_probe_script((source, extraction))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        result = recover_and_write_memory_forward_contract(
            extraction,
            source_dcp_sha256=str(request["source_dcp_sha256"]),
            manifest_path=manifest,
            report_path=report,
        )
        if (
            not report.is_file() or report.stat().st_size <= 0
            or not manifest.is_file() or manifest.stat().st_size <= 0
        ):
            raise RuntimeError("memory-forward probe emitted no contract")
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        report.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            "memory-forward read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_memory_write_buffer_forward(request: dict) -> str:
    """Route one Luna-selected same-seed BRAM forwarding family."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest_path", "probe_report_path",
        "family_ref", "output_dcp", "report_path", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("memory-forward mutation request has an unsupported schema")
    source, report, output = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
            "timeout_s",
        )},
        mutation=True,
    )
    assert output is not None
    family_ref = str(request["family_ref"])
    if re.fullmatch(r"memory_forward_[1-9][0-9]*", family_ref) is None:
        raise ValueError("memory-forward family_ref is invalid")
    manifest_path = _control_state_private_path(
        request["manifest_path"], require_file=True
    )
    probe_report_path = _control_state_private_path(
        request["probe_report_path"], parent=manifest_path.parent,
        require_file=True,
    )
    source_sha256 = str(request["source_dcp_sha256"])
    manifest = load_memory_forward_private_manifest(
        manifest_path, expected_source_sha256=source_sha256
    )
    probe = load_memory_forward_probe_report(
        probe_report_path, expected_source_sha256=source_sha256
    )
    if (
        probe.get("private_manifest_digest") != manifest.get("manifest_digest")
        or family_ref not in (manifest.get("families") or {})
        or not any(
            isinstance(item, dict) and item.get("family_ref") == family_ref
            for item in probe.get("families") or []
        )
    ):
        raise ValueError("memory-forward same-seed family handoff changed")
    staging = output.parent / (
        ".memory_forward_apply_" + source_sha256[:16] + "_" + family_ref
    )
    if staging.exists() and (staging.is_symlink() or not staging.is_dir()):
        raise ValueError("memory-forward staging root is unsafe")
    staging.mkdir(parents=True, exist_ok=True)
    candidate = staging / "candidate.dcp"
    result_path = staging / "result.tsv"
    candidate.unlink(missing_ok=True)
    result_path.unlink(missing_ok=True)
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    script, topology, authorization = render_selected_memory_forward_family(
        seed_dcp=source,
        output_dir=staging,
        manifest=manifest,
        family_ref=family_ref,
    )
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if (
            not candidate.is_file() or candidate.stat().st_size <= 0
            or not result_path.is_file() or result_path.stat().st_size <= 0
        ):
            raise RuntimeError("memory-forward backend emitted no routed candidate")
        result = parse_memory_forward_mutation_result(
            result_path,
            expected_source_sha256=source_sha256,
            family_ref=family_ref,
            topology=topology,
        )
        os.replace(candidate, output)
        shutil.copyfile(result_path, report)
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "memory-forward ECO transaction failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "private_family_identity_replayed": True,
        "local_relational_memory_miter_replayed": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
        "authorization": authorization,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_multilevel_control_distribution_contract(request: dict) -> str:
    """Recover one live complete LUT-driven registered control DAG read-only."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "report_path", "manifest_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError(
            "control-localization probe request has an unsupported schema"
        )
    source, report, _ = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
        )},
        mutation=False,
    )
    manifest = _control_state_private_path(
        request["manifest_path"], parent=report.parent
    )
    extraction = report.parent / (
        ".multilevel_control_" + str(request["source_dcp_sha256"])[:16] + ".tsv"
    )
    report.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    cached_extraction = bool(
        extraction.is_file()
        and not extraction.is_symlink()
        and extraction.stat().st_size > 0
    )
    if cached_extraction:
        result = recover_and_write_multilevel_control_contract(
            extraction,
            source_dcp_sha256=str(request["source_dcp_sha256"]),
            manifest_path=manifest,
            report_path=report,
        )
        result["read_only_after_restore"] = True
        result["checkpoint_cache_reused"] = True
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    extraction.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        probe_output = run_tcl_script(
            build_multilevel_control_probe_script((source, extraction)),
            timeout=float(request["timeout_s"]),
        )
        _design_open = False
        if not extraction.is_file() or extraction.stat().st_size <= 0:
            if "no live LUT-driven registered control endpoint found" in probe_output:
                result = empty_multilevel_control_contract(
                    str(request["source_dcp_sha256"])
                )
            else:
                tail = str(probe_output or "")[-1200:]
                raise RuntimeError(
                    "multilevel control probe did not produce its extraction; "
                    f"Vivado tail: {tail}"
                )
        else:
            result = recover_and_write_multilevel_control_contract(
                extraction,
                source_dcp_sha256=str(request["source_dcp_sha256"]),
                manifest_path=manifest,
                report_path=report,
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        report.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            "control-localization read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    result["checkpoint_cache_reused"] = False
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_multilevel_control_distribution_localization(request: dict) -> str:
    """Route one Luna-selected exact multilevel control localization family."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest_path", "probe_report_path",
        "family_ref", "output_dcp", "report_path", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError(
            "control-localization mutation request has an unsupported schema"
        )
    source, report, output = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
            "timeout_s",
        )},
        mutation=True,
        timeout_max_s=None,
    )
    assert output is not None
    family_ref = str(request["family_ref"])
    if re.fullmatch(r"multilevel_control_[1-9][0-9]*", family_ref) is None:
        raise ValueError("control-localization family_ref is invalid")
    source_sha256 = str(request["source_dcp_sha256"])
    manifest_path = _control_state_private_path(
        request["manifest_path"], require_file=True
    )
    probe_report_path = _control_state_private_path(
        request["probe_report_path"], parent=manifest_path.parent,
        require_file=True,
    )
    manifest = load_multilevel_control_private_manifest(
        manifest_path, expected_source_sha256=source_sha256
    )
    probe = load_multilevel_control_probe_report(
        probe_report_path, expected_source_sha256=source_sha256
    )
    if (
        probe.get("private_manifest_digest") != manifest.get("manifest_digest")
        or manifest.get("family_ref") != family_ref
        or not any(
            isinstance(row, dict) and row.get("family_ref") == family_ref
            for row in probe.get("families") or []
        )
    ):
        raise ValueError("control-localization same-seed handoff changed")
    repair_cache = manifest_path.parent / (
        ".multilevel_control_" + source_sha256[:16] + "_repair_sites.txt"
    )
    pre_repair_sites = []
    if (
        repair_cache.is_file()
        and not repair_cache.is_symlink()
        and repair_cache.stat().st_size > 0
    ):
        pre_repair_sites = [
            line.strip()
            for line in repair_cache.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    script, topology, authorization = render_selected_multilevel_control_family(
        seed_dcp=source,
        output_dcp=output,
        result_tsv=report,
        manifest=manifest,
        family_ref=family_ref,
        pre_repair_sites=pre_repair_sites,
        repair_sites_path=repair_cache,
    )
    script = (
        "set ::fdagents_embedded_mutation 1\n"
        + script
        + "\nunset -nocomplain ::fdagents_embedded_mutation\n"
    )
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    public_contract = manifest.get("public_contract") or {}

    def parse_completed_candidate() -> tuple[dict, str]:
        if (
            not output.is_file() or output.stat().st_size <= 0
            or not report.is_file() or report.stat().st_size <= 0
        ):
            raise RuntimeError(
                "control-localization backend emitted no routed candidate"
            )
        parsed = parse_multilevel_control_mutation_result(
            report,
            expected_source_sha256=source_sha256,
            family_ref=family_ref,
            contract_digest=str(public_contract.get("contract_digest") or ""),
            topology=topology,
        )
        return parsed, _sha256_file(output)

    if (
        output.is_file() and not output.is_symlink()
        and output.stat().st_size > 0
        and report.is_file() and not report.is_symlink()
        and report.stat().st_size > 0
    ):
        try:
            result, candidate_sha256 = parse_completed_candidate()
            result.update({
                "candidate_dcp_sha256": candidate_sha256,
                "authorization": authorization,
                "route_checkpoint_cache_reused": True,
                "formal_whole_dcp_equivalence": False,
                "whole_dcp_equivalence_required_before_submission": True,
            })
            return json.dumps(result, sort_keys=True, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001
            # Never destroy an expensive routed checkpoint merely because its
            # delivery/report layer is incomplete.  A trusted verification-only
            # reopen can repair the report, after which the next call reuses the
            # exact candidate.  Silently rerouting here loses the checkpoint and
            # turns a packaging fault into hundreds of seconds of Vivado work.
            raise RuntimeError(
                "control-localization cached routed candidate was preserved; "
                f"repair its report before retrying: {exc}"
            ) from exc
    if output.is_file() and not output.is_symlink() and output.stat().st_size > 0:
        raise RuntimeError(
            "control-localization cached routed candidate was preserved; "
            "its report is missing and must be rebuilt by trusted verification"
        )

    report.unlink(missing_ok=True)

    try:
        close_current_design()
        _design_open = False
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        result, candidate_sha256 = parse_completed_candidate()
    except Exception as exc:  # noqa: BLE001
        failure = exc
        try:
            # A CLI-style Tcl script from an older deployment can close Vivado
            # after it has already written and reopened a complete candidate.
            # Preserve expensive work only when the full routed report replays.
            result, candidate_sha256 = parse_completed_candidate()
            failure = None
            result["completed_artifact_recovered_after_transport_exit"] = True
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        raise RuntimeError(
            "control-localization ECO transaction failed; any emitted "
            "checkpoint was preserved for verification-only resume: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "authorization": authorization,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_multilevel_ram_control_distribution_contract(request: dict) -> str:
    """Recover one complete current-seed LUT-driven RAM-control DAG read-only."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "run_id", "report_path", "manifest_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError(
            "RAM-control localization probe request has an unsupported schema"
        )
    source, report, _ = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
        )},
        mutation=False,
    )
    manifest = _control_state_private_path(
        request["manifest_path"], parent=report.parent
    )
    extraction = report.parent / (
        ".multilevel_ram_control_v1_"
        + str(request["run_id"])
        + "_"
        + str(request["source_dcp_sha256"])[:16]
        + ".tsv"
    )
    report.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    cached_extraction = bool(
        extraction.is_file()
        and not extraction.is_symlink()
        and extraction.stat().st_size > 0
    )
    if cached_extraction:
        result = recover_and_write_multilevel_ram_control_contract(
            extraction,
            source_dcp_sha256=str(request["source_dcp_sha256"]),
            run_id=str(request["run_id"]),
            manifest_path=manifest,
            report_path=report,
        )
        result["read_only_after_restore"] = True
        result["checkpoint_cache_reused"] = True
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    extraction.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        probe_output = run_tcl_script(
            build_multilevel_ram_control_probe_script((source, extraction)),
            timeout=float(request["timeout_s"]),
        )
        _design_open = False
        if not extraction.is_file() or extraction.stat().st_size <= 0:
            if "no complete multi-region LUT-driven RAM-control wall found" in probe_output:
                result = empty_multilevel_ram_control_contract(
                    str(request["source_dcp_sha256"]),
                    run_id=str(request["run_id"]),
                )
            else:
                tail = str(probe_output or "")[-1200:]
                raise RuntimeError(
                    "RAM-control probe did not produce its extraction; "
                    f"Vivado tail: {tail}"
                )
        else:
            result = recover_and_write_multilevel_ram_control_contract(
                extraction,
                source_dcp_sha256=str(request["source_dcp_sha256"]),
                run_id=str(request["run_id"]),
                manifest_path=manifest,
                report_path=report,
            )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        report.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            "RAM-control read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    result["checkpoint_cache_reused"] = False
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_multilevel_ram_control_distribution_localization(request: dict) -> str:
    """Route one proof-authorized exact RAM-control localization family."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "run_id", "manifest_path", "probe_report_path",
        "family_ref", "output_dcp", "report_path", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError(
            "RAM-control mutation request has an unsupported schema"
        )
    source, report, output = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
            "timeout_s",
        )},
        mutation=True,
        timeout_max_s=None,
    )
    assert output is not None
    family_ref = str(request["family_ref"])
    if re.fullmatch(r"multilevel_ram_control_[1-9][0-9]*", family_ref) is None:
        raise ValueError("RAM-control family_ref is invalid")
    source_sha256 = str(request["source_dcp_sha256"])
    run_id = str(request["run_id"])
    manifest_path = _control_state_private_path(
        request["manifest_path"], require_file=True
    )
    probe_report_path = _control_state_private_path(
        request["probe_report_path"], parent=manifest_path.parent,
        require_file=True,
    )
    manifest = load_multilevel_ram_control_private_manifest(
        manifest_path, expected_source_sha256=source_sha256,
        expected_run_id=run_id,
    )
    probe = load_multilevel_ram_control_probe_report(
        probe_report_path, expected_source_sha256=source_sha256,
        expected_run_id=run_id,
    )
    if (
        probe.get("private_manifest_digest") != manifest.get("manifest_digest")
        or manifest.get("family_ref") != family_ref
        or not any(
            isinstance(row, dict) and row.get("family_ref") == family_ref
            for row in probe.get("families") or []
        )
    ):
        raise ValueError("RAM-control same-seed handoff changed")
    repair_cache = manifest_path.parent / (
        ".multilevel_ram_control_v1_"
        + run_id
        + "_"
        + source_sha256[:16]
        + "_repair_sites.txt"
    )
    pre_repair_sites = []
    if (
        repair_cache.is_file()
        and not repair_cache.is_symlink()
        and repair_cache.stat().st_size > 0
    ):
        pre_repair_sites = [
            line.strip()
            for line in repair_cache.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    script, topology, authorization = render_selected_multilevel_ram_control_family(
        seed_dcp=source,
        output_dcp=output,
        result_tsv=report,
        manifest=manifest,
        family_ref=family_ref,
        pre_repair_sites=pre_repair_sites,
        repair_sites_path=repair_cache,
    )
    script = (
        "set ::fdagents_embedded_mutation 1\n"
        + script
        + "\nunset -nocomplain ::fdagents_embedded_mutation\n"
    )
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    public_contract = manifest.get("public_contract") or {}

    def parse_completed_candidate() -> tuple[dict, str]:
        if (
            not output.is_file() or output.stat().st_size <= 0
            or not report.is_file() or report.stat().st_size <= 0
        ):
            raise RuntimeError("RAM-control backend emitted no routed candidate")
        parsed = parse_multilevel_ram_control_mutation_result(
            report,
            expected_source_sha256=source_sha256,
            family_ref=family_ref,
            contract_digest=str(public_contract.get("contract_digest") or ""),
            topology=topology,
            expected_run_id=run_id,
        )
        return parsed, _sha256_file(output)

    if (
        output.is_file() and not output.is_symlink()
        and output.stat().st_size > 0
        and report.is_file() and not report.is_symlink()
        and report.stat().st_size > 0
    ):
        try:
            result, candidate_sha256 = parse_completed_candidate()
            result.update({
                "candidate_dcp_sha256": candidate_sha256,
                "authorization": authorization,
                "route_checkpoint_cache_reused": True,
                "formal_whole_dcp_equivalence": False,
                "whole_dcp_equivalence_required_before_submission": True,
            })
            return json.dumps(result, sort_keys=True, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "RAM-control cached routed candidate was preserved; repair "
                f"its report before retrying: {exc}"
            ) from exc
    if output.is_file() and not output.is_symlink() and output.stat().st_size > 0:
        raise RuntimeError(
            "RAM-control cached routed candidate was preserved; its report "
            "must be rebuilt by trusted verification"
        )
    report.unlink(missing_ok=True)
    try:
        close_current_design()
        _design_open = False
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        result, candidate_sha256 = parse_completed_candidate()
    except Exception as exc:  # noqa: BLE001
        failure = exc
        try:
            result, candidate_sha256 = parse_completed_candidate()
            failure = None
            result["completed_artifact_recovered_after_transport_exit"] = True
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        raise RuntimeError(
            "RAM-control ECO transaction failed; any emitted checkpoint was "
            "preserved for verification-only resume: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "authorization": authorization,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_registered_parity_reassociation_contract(request: dict) -> str:
    """Recover live complete XOR/XNOR closures without mutating the seed."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "report_path", "manifest_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered parity probe request has an unsupported schema")
    source, report, _ = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "report_path", "timeout_s",
        )},
        mutation=False,
    )
    manifest = _control_state_private_path(
        request["manifest_path"], parent=report.parent
    )
    extraction = report.parent / (
        ".registered_parity_" + str(request["source_dcp_sha256"])[:16] + ".tsv"
    )
    report.unlink(missing_ok=True)
    manifest.unlink(missing_ok=True)
    cached_extraction = bool(
        extraction.is_file() and not extraction.is_symlink()
        and extraction.stat().st_size > 0
    )
    if cached_extraction:
        result = recover_and_write_registered_parity_contract(
            extraction,
            source_dcp_sha256=str(request["source_dcp_sha256"]),
            manifest_path=manifest,
            report_path=report,
        )
        result["read_only_after_restore"] = True
        result["checkpoint_cache_reused"] = True
        return json.dumps(result, sort_keys=True, separators=(",", ":"))
    extraction.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        run_tcl_script(
            build_registered_parity_probe_script((source, extraction)),
            timeout=float(request["timeout_s"]),
        )
        _design_open = False
        result = recover_and_write_registered_parity_contract(
            extraction,
            source_dcp_sha256=str(request["source_dcp_sha256"]),
            manifest_path=manifest,
            report_path=report,
        )
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        report.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise RuntimeError(
            "registered parity read-only recovery failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    result["checkpoint_cache_reused"] = False
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_registered_parity_reassociation(request: dict) -> str:
    """Route one Luna-selected registered GF(2) parity family."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest_path", "probe_report_path",
        "family_ref", "implementation_mode", "output_dcp", "report_path",
        "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError(
            "registered parity mutation request has an unsupported schema"
        )
    source, report, output = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
            "timeout_s",
        )},
        mutation=True,
    )
    assert output is not None
    family_ref = str(request["family_ref"])
    if re.fullmatch(r"registered_parity_[1-9][0-9]*", family_ref) is None:
        raise ValueError("registered parity family_ref is invalid")
    implementation_mode = str(request["implementation_mode"])
    if implementation_mode not in {
        "local_preserve", "closure_local_rebasin", "global_quick_rebasin",
    }:
        raise ValueError("registered parity implementation mode is invalid")
    source_sha256 = str(request["source_dcp_sha256"])
    manifest_path = _control_state_private_path(
        request["manifest_path"], require_file=True
    )
    probe_report_path = _control_state_private_path(
        request["probe_report_path"], parent=manifest_path.parent,
        require_file=True,
    )
    manifest = load_registered_parity_private_manifest(
        manifest_path, expected_source_sha256=source_sha256
    )
    probe = load_registered_parity_probe_report(
        probe_report_path, expected_source_sha256=source_sha256
    )
    if (
        probe.get("private_manifest_digest") != manifest.get("manifest_digest")
        or family_ref not in (manifest.get("family_refs") or [])
        or not any(
            isinstance(row, dict) and row.get("family_ref") == family_ref
            for row in probe.get("families") or []
        )
    ):
        raise ValueError("registered parity same-seed handoff changed")
    script, topology, authorization = render_selected_registered_parity_family(
        seed_dcp=source,
        output_dcp=output,
        result_tsv=report,
        manifest=manifest,
        family_ref=family_ref,
        implementation_mode=implementation_mode,
    )
    script = (
        "set ::fdagents_embedded_mutation 1\n" + script
        + "\nunset -nocomplain ::fdagents_embedded_mutation\n"
    )
    public_contract = manifest.get("public_contract") or {}

    def parse_completed_candidate() -> tuple[dict, str]:
        if (
            not output.is_file() or output.stat().st_size <= 0
            or not report.is_file() or report.stat().st_size <= 0
        ):
            raise RuntimeError("registered parity backend emitted no routed candidate")
        parsed = parse_registered_parity_mutation_result(
            report,
            expected_source_sha256=source_sha256,
            family_ref=family_ref,
            contract_digest=str(public_contract.get("contract_digest") or ""),
            topology=topology,
        )
        return parsed, _sha256_file(output)

    if (
        output.is_file() and not output.is_symlink()
        and output.stat().st_size > 0
        and report.is_file() and not report.is_symlink()
        and report.stat().st_size > 0
    ):
        try:
            result, candidate_sha256 = parse_completed_candidate()
            result.update({
                "candidate_dcp_sha256": candidate_sha256,
                "authorization": authorization,
                "route_checkpoint_cache_reused": True,
                "formal_whole_dcp_equivalence": False,
                "whole_dcp_equivalence_required_before_submission": True,
            })
            return json.dumps(result, sort_keys=True, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "registered parity cached routed candidate was preserved; repair "
                f"its report before retrying: {exc}"
            ) from exc
    if output.is_file() and not output.is_symlink() and output.stat().st_size > 0:
        raise RuntimeError(
            "registered parity cached routed candidate was preserved; its report "
            "is missing and must be rebuilt by trusted verification"
        )
    report.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        result, candidate_sha256 = parse_completed_candidate()
    except Exception as exc:  # noqa: BLE001
        failure = exc
        try:
            result, candidate_sha256 = parse_completed_candidate()
            failure = None
            result["completed_artifact_recovered_after_transport_exit"] = True
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        raise RuntimeError(
            "registered parity ECO transaction failed; any emitted checkpoint was "
            "preserved for verification-only resume: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "authorization": authorization,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_ff_srl_boundary_bridge(request: dict) -> str:
    """Rewrite one Luna-selected same-seed direct-FF transport family."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest_path", "probe_report_path",
        "family_ref", "output_dcp", "report_path", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("FF-to-SRL mutation request has an unsupported schema")
    source, report, output = _banked_predicate_paths(
        {key: request[key] for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
            "timeout_s",
        )},
        mutation=True,
    )
    assert output is not None
    family_ref = str(request["family_ref"])
    if re.fullmatch(r"ff_srl_[1-9][0-9]*", family_ref) is None:
        raise ValueError("FF-to-SRL family_ref is invalid")
    manifest_path = _control_state_private_path(
        request["manifest_path"], require_file=True
    )
    probe_report_path = _control_state_private_path(
        request["probe_report_path"], parent=manifest_path.parent,
        require_file=True,
    )
    manifest = parse_ff_srl_boundary_bridge_manifest(
        manifest_path,
        expected_source_sha256=str(request["source_dcp_sha256"]),
    )
    contract = parse_ff_srl_boundary_bridge_probe_report(
        probe_report_path,
        manifest_path=manifest_path,
        expected_source_sha256=str(request["source_dcp_sha256"]),
    )
    if family_ref not in manifest["families"]:
        raise ValueError("FF-to-SRL family_ref is absent from the current seed")
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_ff_srl_boundary_bridge_script((
            source, manifest_path, family_ref, output, report,
            str(request["source_dcp_sha256"]),
        ))
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if (
            not output.is_file() or output.stat().st_size <= 0
            or not report.is_file() or report.stat().st_size <= 0
        ):
            raise RuntimeError("FF-to-SRL backend emitted no routed candidate")
        result = parse_ff_srl_boundary_bridge_backend_report(
            report,
            manifest=manifest,
            family_ref=family_ref,
            contract=contract,
        )
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "FF-to-SRL ECO transaction failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "private_manifest_identity_replayed": True,
        "enabled_cycle_latency_preserved": True,
        "hierarchy_boundary_ff_retained": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_registered_dead_state_elimination(request: dict) -> str:
    """Delete one privately retained exact output-unobservable cell set."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "manifest_path", "output_dcp",
        "report_path", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("registered dead-state request has an unsupported schema")
    source, report, output = _banked_predicate_paths(
        {
            key: request[key]
            for key in (
                "dcp_path", "source_dcp_sha256", "output_dcp",
                "report_path", "timeout_s",
            )
        },
        mutation=True,
    )
    assert output is not None
    manifest_path = Path(str(request["manifest_path"])).expanduser().resolve()
    if any(char in str(manifest_path) for char in ("}", "\r", "\n")):
        raise ValueError("registered dead-state manifest path is unsafe")
    manifest = parse_registered_dead_state_manifest(
        manifest_path,
        expected_source_sha256=str(request["source_dcp_sha256"]),
    )
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_registered_dead_state_script(
            (source, manifest_path, output, report)
        )
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if (
            not output.is_file() or output.stat().st_size <= 0
            or not report.is_file() or report.stat().st_size <= 0
        ):
            raise RuntimeError(
                "registered dead-state backend emitted no routed candidate"
            )
        result = parse_registered_dead_state_backend_report(
            report, manifest=manifest
        )
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "registered dead-state ECO transaction failed: "
            f"{failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "observable_closure_replayed": True,
        "private_manifest_identity_replayed": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def recover_banked_ram_operator_contract(request: dict) -> str:
    """Recover anonymous RAM-data and bank-control operators on the live wall."""
    global _design_open
    source, report, _ = _banked_predicate_paths(request, mutation=False)
    cached = load_banked_ram_operator_probe_cache(
        report, str(request["source_dcp_sha256"])
    )
    if cached is not None:
        cached = dict(cached)
        cached["probe_cache_reused"] = True
        return json.dumps(cached, sort_keys=True, separators=(",", ":"))
    report.unlink(missing_ok=True)
    failure = None
    result = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_banked_ram_operator_script(
            "probe_banked_ram_control_packet.tcl", (source, report)
        )
        run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if not report.is_file():
            raise RuntimeError("banked RAM data/control probe emitted no report")
        result = parse_banked_ram_operator_probe_report(report)
        result.pop("_private_contract_digest", None)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or not restored:
        raise RuntimeError(
            f"banked RAM data/control recovery failed: {failure or 'source restore failed'}"
        ) from failure
    result["read_only_after_restore"] = True
    write_banked_ram_operator_probe_cache(
        report, str(request["source_dcp_sha256"]), result
    )
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def _apply_banked_ram_operator(request: dict, *, backend: str) -> str:
    """Run one framework-owned local data/control backend and restore source."""
    global _design_open
    source, report, output = _banked_predicate_paths(request, mutation=True)
    assert output is not None
    scripts = {
        "registered_control_packet_localization_v1": (
            "apply_banked_ram_control_packet.tcl"
        ),
        "registered_ram_boolean_midpoint_relocation_v1": (
            "apply_registered_ram_boolean_midpoint.tcl"
        ),
    }
    script_name = scripts.get(backend)
    if script_name is None:
        raise ValueError("unsupported banked RAM operator backend")
    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        script_arguments: tuple[Path, ...] = (source, output, report)
        if backend == "registered_control_packet_localization_v1":
            probe_report = Path(
                str(request.get("probe_report_path") or "")
            ).expanduser().resolve()
            if (
                not probe_report.is_file()
                or probe_report.is_symlink()
                or probe_report.parent.is_symlink()
            ):
                raise RuntimeError(
                    "banked control packet mutation probe report is unavailable"
                )
            cached_probe = load_banked_ram_operator_probe_cache(
                probe_report, str(request["source_dcp_sha256"])
            )
            if cached_probe is None:
                raise RuntimeError(
                    "banked control packet mutation requires the exact cached probe"
                )
            script_arguments = (*script_arguments, probe_report)
        close_current_design()
        _design_open = False
        script = build_banked_ram_operator_script(
            script_name, script_arguments
        )
        tcl_output = run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if not output.is_file() or not report.is_file():
            raise RuntimeError(
                "banked RAM backend emitted no routed candidate; "
                f"completion_diagnostic={tcl_failure_diagnostic(tcl_output)}"
            )
        result = parse_banked_ram_operator_backend_report(report, backend)
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"banked RAM ECO transaction failed: {failure or 'source restore failed'}"
        ) from failure
    result.update({
        "source_dcp_sha256": str(request["source_dcp_sha256"]),
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))


def apply_registered_control_packet_localization(request: dict) -> str:
    return _apply_banked_ram_operator(
        request, backend="registered_control_packet_localization_v1"
    )


def apply_registered_ram_boolean_midpoint(request: dict) -> str:
    return _apply_banked_ram_operator(
        request, backend="registered_ram_boolean_midpoint_relocation_v1"
    )


def _commutative_reassociation_artifacts(report: Path) -> tuple[Path, Path]:
    return (
        report.with_name(report.name + ".contract.json"),
        report.with_name(report.name + ".cone"),
    )


def recover_commutative_reassociation_contract(request: dict) -> str:
    """Recover one live, control-closed commutative registered reduction."""
    global _design_open
    source, report, _ = _banked_predicate_paths(request, mutation=False)
    contract_path, cone_dir = _commutative_reassociation_artifacts(report)
    source_sha256 = str(request["source_dcp_sha256"])
    if report.is_file() and contract_path.is_file():
        try:
            parsed = parse_commutative_reassociation_probe_report(report)
            contract = load_commutative_reassociation_contract(contract_path)
            from FDAgents.commutative_reassociation import (
                verify_commutative_reassociation_contract,
            )
            if verify_commutative_reassociation_contract(
                contract, source_dcp_sha256=source_sha256
            ):
                public = dict(parsed["public"])
                public.update({
                    "probe_cache_reused": True,
                    "primitive_replay_passed": True,
                    "clock_ce_reset_transaction_closed": True,
                    "mutation_eligible": True,
                })
                return json.dumps(public, sort_keys=True, separators=(",", ":"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            pass
        invalidate_commutative_reassociation_cache(report)
    else:
        # A lone report/contract/cone is not a reusable cache.  In particular,
        # a report from the previous promoted seed must never be parsed as if
        # it described the current source before a source-bound proof exists.
        invalidate_commutative_reassociation_cache(report)
    failure = None
    restored = False
    parsed = None
    if report.is_file() and not report.is_symlink():
        try:
            parsed = parse_commutative_reassociation_probe_report(report)
            restored = True
        except (OSError, UnicodeError, ValueError):
            report.unlink(missing_ok=True)
    if parsed is None:
        try:
            close_current_design()
            _design_open = False
            script = build_commutative_reassociation_script(
                "probe_commutative_reduction.tcl", (source, report)
            )
            probe_output = run_tcl_script(
                script, timeout=float(request["timeout_s"])
            )
            _design_open = False
            if not report.is_file():
                negative_reason = _commutative_probe_no_report_reason(
                    probe_output
                )
                if negative_reason:
                    raise ValueError(negative_reason)
                raise RuntimeError("commutative reduction probe emitted no report")
            parsed = parse_commutative_reassociation_probe_report(report)
        except Exception as exc:  # noqa: BLE001
            failure = exc
        finally:
            try:
                if _design_open:
                    close_current_design()
                run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
                _design_open = True
                restored = True
            except Exception as restore_exc:  # noqa: BLE001
                if failure is None:
                    failure = restore_exc
    if failure is not None or parsed is None or not restored:
        raise RuntimeError(
            f"commutative reduction recovery failed: {failure or 'source restore failed'}"
        ) from failure

    if cone_dir.exists():
        required = {
            "manifest.tsv", "cells.tsv", "pins.tsv", "boundary.tsv",
            "targets.tsv", "side_fanout.tsv", "static_nets.tsv",
        }
        if not cone_dir.is_dir() or {
            item.name for item in cone_dir.iterdir() if item.is_file()
        } != required:
            raise RuntimeError("commutative reduction partial cone cache is incomplete")
    else:
        extraction = json.loads(extract_register_cone_facts({
            "dcp_path": str(source),
            "source_dcp_sha256": source_sha256,
            "target_stem": parsed["private"]["target_stem"],
            "output_dir": str(cone_dir),
        }))
        if extraction.get("read_only_after_restore") is not True:
            raise RuntimeError("commutative reduction cone extraction did not restore seed")

    from FDAgents.commutative_reassociation import (
        build_commutative_reassociation_contract,
    )
    contract = build_commutative_reassociation_contract(
        cone_dir,
        source_dcp_sha256=source_sha256,
        critical_source_stem=parsed["private"]["critical_source_stem"],
        replay_samples=4096,
    )
    contract_path.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    public = dict(parsed["public"])
    public.update({
        "probe_cache_reused": False,
        "primitive_replay_passed": True,
        "primitive_replay_samples": contract["public"]["primitive_replay_samples"],
        "clock_ce_reset_transaction_closed": True,
        "outside_side_fanout_count": contract["public"]["outside_side_fanout_count"],
        "mutation_eligible": True,
    })
    return json.dumps(public, sort_keys=True, separators=(",", ":"))


def apply_commutative_operand_reassociation(request: dict) -> str:
    """Route one framework-selected complete-bus pair and restore the seed."""
    global _design_open
    required = {
        "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
        "probe_report_path", "contract_path", "candidate_index", "timeout_s",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise ValueError("commutative reassociation request has an unsupported schema")
    source, report, output = _banked_predicate_paths({
        key: request[key]
        for key in (
            "dcp_path", "source_dcp_sha256", "output_dcp", "report_path", "timeout_s"
        )
    }, mutation=True)
    assert output is not None
    probe_report = Path(str(request["probe_report_path"])).expanduser().resolve()
    contract_path = Path(str(request["contract_path"])).expanduser().resolve()
    candidate_index = request["candidate_index"]
    if (
        not probe_report.is_file()
        or probe_report.is_symlink()
        or not contract_path.is_file()
        or contract_path.is_symlink()
        or isinstance(candidate_index, bool)
        or not isinstance(candidate_index, int)
        or candidate_index < 0
    ):
        raise ValueError("commutative reassociation private contract is unavailable")
    parsed = parse_commutative_reassociation_probe_report(probe_report)
    contract = load_commutative_reassociation_contract(contract_path)
    from FDAgents.commutative_reassociation import (
        verify_commutative_reassociation_contract,
    )
    if not verify_commutative_reassociation_contract(
        contract, source_dcp_sha256=str(request["source_dcp_sha256"])
    ):
        raise ValueError("commutative reassociation contract did not replay")
    candidates = list((contract.get("private") or {}).get("candidate_pairs") or [])
    if candidate_index >= len(candidates):
        raise ValueError("commutative reassociation candidate index is out of range")
    selected = candidates[candidate_index]
    private_probe = parsed["private"]
    if (
        selected.get("source_stem") != private_probe["critical_source_stem"]
        or selected.get("peer_stem") not in private_probe["peer_stems"]
        or (contract.get("private") or {}).get("target_stem")
        != private_probe["target_stem"]
    ):
        raise ValueError("commutative reassociation live probe and proof disagree")

    report.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    failure = None
    result = None
    candidate_sha256 = None
    restored = False
    try:
        close_current_design()
        _design_open = False
        script = build_commutative_reassociation_script(
            "apply_commutative_operand_reassociation.tcl",
            (
                source, output, report, private_probe["target_stem"],
                selected["source_stem"], selected["peer_stem"],
            ),
        )
        tcl_output = run_tcl_script(script, timeout=float(request["timeout_s"]))
        _design_open = False
        if not output.is_file() or not report.is_file():
            diagnostic = str(tcl_output or "")[-4000:]
            raise RuntimeError(
                "commutative reassociation emitted no routed candidate; "
                f"tcl_output_tail={diagnostic!r}"
            )
        result = parse_commutative_reassociation_backend_report(report)
        candidate_sha256 = _sha256_file(output)
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        try:
            if _design_open:
                close_current_design()
            run_tcl_command(f"open_checkpoint {{{source}}}", timeout=600)
            _design_open = True
            restored = True
        except Exception as restore_exc:  # noqa: BLE001
            if failure is None:
                failure = restore_exc
    if failure is not None or result is None or candidate_sha256 is None or not restored:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"commutative reassociation transaction failed: {failure or 'source restore failed'}"
        ) from failure
    result.update({
        "candidate_index": candidate_index,
        "source_dcp_sha256": str(request["source_dcp_sha256"]),
        "candidate_dcp_sha256": candidate_sha256,
        "candidate_checkpoint_reopened": True,
        "source_checkpoint_reopened": True,
        "relevant_nets_fully_routed": True,
        "primitive_replay_passed": True,
        "registered_control_transaction_preserved": True,
        "formal_whole_dcp_equivalence": False,
        "whole_dcp_equivalence_required_before_submission": True,
    })
    return json.dumps(result, sort_keys=True, separators=(",", ":"))



_CLOCK_REGION_PBLOCK_RE = re.compile(
    r"^CLOCKREGION_X(\d+)Y(\d+):CLOCKREGION_X(\d+)Y(\d+)$"
)
_SAFE_PBLOCK_CELL_RE = re.compile(r"^[A-Za-z0-9_./\[\]-]+$")


def _clock_region_pblock_box(value: str) -> tuple[int, int, int, int]:
    match = _CLOCK_REGION_PBLOCK_RE.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("multi-region pblocks require one CLOCKREGION rectangle per partition")
    x0, y0, x1, y1 = map(int, match.groups())
    if x0 > x1 or y0 > y1:
        raise ValueError("clock-region pblock bounds are reversed")
    return x0, y0, x1, y1


def create_and_apply_multiregion_pblocks(
    pblock_prefix: str,
    critical_range: str,
    remainder_range: str,
    critical_cell_names: list[str],
    is_soft: bool = False,
    validate_resources: bool = True,
    timeout: float = 300.0,
) -> str:
    """Apply two disjoint, separately validated clock-region pblocks."""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(pblock_prefix)) is None:
        raise ValueError("invalid pblock prefix")
    critical_box = _clock_region_pblock_box(critical_range)
    remainder_box = _clock_region_pblock_box(remainder_range)
    if not (
        critical_box[2] < remainder_box[0]
        or remainder_box[2] < critical_box[0]
        or critical_box[3] < remainder_box[1]
        or remainder_box[3] < critical_box[1]
    ):
        raise ValueError("multi-region pblock rectangles must not overlap")
    names = []
    for value in critical_cell_names:
        name = str(value)
        if not _SAFE_PBLOCK_CELL_RE.fullmatch(name):
            raise ValueError(f"unsafe critical cell name: {name!r}")
        if name not in names:
            names.append(name)
    if not 1 <= len(names) <= 256:
        raise ValueError("critical partition must contain 1..256 unique cells")

    critical_name = f"{pblock_prefix}_critical"
    remainder_name = f"{pblock_prefix}_remainder"
    cell_list = "[list " + " ".join("{" + name + "}" for name in names) + "]"
    selection = (
        f"set fdagents_critical_cells [get_cells -quiet {cell_list}]; "
        "set fdagents_all_cells [get_cells -quiet -hierarchical "
        "-filter {IS_PRIMITIVE}]; "
        # Pblocks must not split dedicated placement shapes. Grow the critical
        # partition through MUXF/CARRY and hard-macro cascade connections.
        "set fdagents_shape_changed 1; "
        "while {$fdagents_shape_changed} { "
        "set fdagents_shape_changed 0; "
        "foreach c $fdagents_critical_cells { "
        "set ref [get_property REF_NAME $c]; set pin_pattern {}; set neighbor_pattern {}; "
        "if {[regexp {^MUXF[78]$} $ref]} { "
        "set pin_pattern {^(I0|I1|O)$}; set neighbor_pattern {^(LUT[1-6]|MUXF[78])$} "
        "} elseif {$ref eq {CARRY8}} { "
        "set pin_pattern {^(CI|CI_TOP|CO\\[[0-7]\\]|DI\\[[0-7]\\]|S\\[[0-7]\\])$}; "
        "set neighbor_pattern {^(LUT[1-6]|CARRY8)$} "
        "} elseif {$ref eq {DSP48E2}} { "
        "set pin_pattern {^(ACIN|ACOUT|BCIN|BCOUT|PCIN|PCOUT|CARRYCASCIN|CARRYCASCOUT|MULTSIGNIN|MULTSIGNOUT)(\\[.*\\])?$}; "
        "set neighbor_pattern {^DSP48E2$} "
        "} elseif {[regexp {^RAMB(18|36)E2$} $ref]} { "
        "set pin_pattern {^CASCADE}; set neighbor_pattern {^RAMB(18|36)E2$} "
        "} elseif {$ref eq {URAM288}} { "
        "set pin_pattern {^CAS_}; set neighbor_pattern {^URAM288$} "
        "} else { continue }; "
        "foreach pin [get_pins -quiet -of_objects $c] { "
        "set leaf {}; catch {set leaf [get_property REF_PIN_NAME $pin]}; "
        "if {$leaf eq {}} { set leaf [lindex [split [get_property NAME $pin] /] end] }; "
        "if {![regexp $pin_pattern $leaf]} { continue }; "
        "foreach neighbor [get_cells -quiet -of_objects [get_nets -quiet -of_objects $pin]] { "
        "if {[lsearch -exact $fdagents_critical_cells $neighbor] >= 0} { continue }; "
        "if {[regexp $neighbor_pattern [get_property REF_NAME $neighbor]]} { "
        "lappend fdagents_critical_cells $neighbor; set fdagents_shape_changed 1 "
        "} } } } }; "
        "set fdagents_critical_cells [lsort -dictionary -unique $fdagents_critical_cells]; "
        "set fdagents_remainder_cells {}; "
        "foreach c $fdagents_all_cells { "
        "if {[lsearch -exact $fdagents_critical_cells $c] < 0} { "
        "lappend fdagents_remainder_cells $c } }; "
    )
    preflight = run_tcl_command(
        selection
        + 'puts "CRITICAL_INPUT=' + str(len(names))
        + ' CRITICAL=[llength $fdagents_critical_cells] '
        + 'REMAINDER=[llength $fdagents_remainder_cells]"',
        timeout=timeout,
    )
    match = re.search(
        r"CRITICAL_INPUT=(\d+)\s+CRITICAL=(\d+)\s+REMAINDER=(\d+)",
        str(preflight),
    )
    if (
        match is None
        or int(match.group(1)) != len(names)
        or int(match.group(2)) < len(names)
        or int(match.group(3)) < 1
    ):
        raise ValueError("multi-region cell partition preflight failed")

    soft_value = "1" if is_soft else "0"
    try:
        command = (
            selection
            + f"create_pblock {critical_name}; "
            + f"resize_pblock {critical_name} -add {{{critical_range}}}; "
            + f"set_property IS_SOFT {soft_value} [get_pblocks {critical_name}]; "
            + f"add_cells_to_pblock {critical_name} $fdagents_critical_cells; "
            + f"create_pblock {remainder_name}; "
            + f"resize_pblock {remainder_name} -add {{{remainder_range}}}; "
            + f"set_property IS_SOFT {soft_value} [get_pblocks {remainder_name}]; "
            + f"add_cells_to_pblock {remainder_name} $fdagents_remainder_cells"
        )
        run_tcl_command(command, timeout=timeout)
        validations = {}
        if validate_resources:
            for name in (critical_name, remainder_name):
                validations[name] = validate_pblock_resources(name)
            invalid = [
                name for name, result in validations.items()
                if not result.get("is_valid", False)
            ]
            if invalid:
                run_tcl_command(
                    f"delete_pblocks [get_pblocks {{{critical_name} {remainder_name}}}]",
                    timeout=30.0,
                )
                errors = {
                    name: validations[name].get("errors", []) for name in invalid
                }
                return f"Error: multi-region resource validation failed: {errors}"
        return (
            f"Created multi-region pblocks: {critical_name}={critical_range}; "
            f"{remainder_name}={remainder_range}; critical_cells={match.group(2)}"
        )
    except Exception:
        try:
            run_tcl_command(
                f"delete_pblocks [get_pblocks -quiet {{{critical_name} {remainder_name}}}]",
                timeout=30.0,
            )
        except Exception:
            pass
        raise


# Create MCP server
server = Server("vivado-mcp")


@server.list_tools()
async def list_tools():
    """List available Vivado tools."""
    return [
        Tool(
            name="open_checkpoint",
            description="Open a Vivado Design Checkpoint (.dcp) file. Closes any currently open design first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path to the .dcp file to open"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="write_checkpoint",
            description="Write the current design to a Vivado Design Checkpoint (.dcp) file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "description": "Path where the .dcp file will be saved"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing file if True (default: False)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["dcp_path"]
            }
        ),
        Tool(
            name="report_route_status",
            description="Get the routing status report for the current design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="report_timing_summary",
            description="Get a timing summary report for the current design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="get_wns",
            description="Get the Worst Negative Slack (WNS) value directly. Returns just the numeric slack value in nanoseconds. Optionally filter by a specific clock domain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "clock": {
                        "type": "string",
                        "description": "Clock name to filter WNS by (e.g., 'clk_fpl26contest'). If omitted, returns overall WNS across all clocks."
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 60)"
                    }
                }
            }
        ),
        Tool(
            name="place_design",
            description="Run placement on the current design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": "Placement directive (e.g., 'Default', 'Explore', 'Quick')"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 3600 for placement)"
                    }
                }
            }
        ),
        Tool(
            name="route_design",
            description="Run routing on the current design.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": "Routing directive (e.g., 'Default', 'Explore', 'Quick')"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 3600 for routing)"
                    }
                }
            }
        ),
        Tool(
            name="run_tcl",
            description="Execute an arbitrary Tcl command in Vivado.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The Tcl command to execute"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="recover_banked_ram_operator_contract",
            description=(
                "Read-only recovery of live registered RAM-to-Boolean data "
                "operators and registered-state-to-bank-control packets. Exact "
                "object names remain private; Luna receives anonymous data/control "
                "contracts and typed measurement candidates."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="recover_commutative_reassociation_contract",
            description=(
                "Read-only recovery of a live registered commutative modular "
                "reduction, including complete operand words, endpoint controls, "
                "primitive replay, and anonymous pair-swap uncertainty."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_commutative_operand_reassociation",
            description=(
                "Framework-only complete-bus reassociation for one privately "
                "indexed candidate of a proved commutative reduction."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "contract_path": {"type": "string", "minLength": 1},
                    "candidate_index": {"type": "integer", "minimum": 0},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "output_dcp", "report_path",
                    "probe_report_path", "contract_path", "candidate_index", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_registered_control_packet_localization",
            description=(
                "Framework-only bank-local replication of one freshly recovered "
                "Boolean control generator; registered state is preserved."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "output_dcp",
                    "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_registered_ram_boolean_midpoint",
            description=(
                "Framework-only relocation of freshly recovered existing "
                "RAM/registered-peer Boolean LUT and endpoint-register pairs."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "output_dcp",
                    "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="recover_banked_select_predicate_contract",
            description=(
                "Read-only recovery of a banked registered-RAM data operator "
                "and its bank-local predicate/control transaction. Exact object "
                "names remain private; the response contains live-wall evidence "
                "and an optional typed measurement action."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_banked_select_predicate_update",
            description=(
                "Framework-only exact-DCP ECO for a freshly recovered "
                "BANKED_SELECT_PREDICATE_UPDATE contract."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "output_dcp",
                    "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="recover_registered_dsp_lane_contract",
            description=(
                "Read-only recovery of a complete anonymous DSP P-to-external-FF "
                "registered product transaction, its independent sibling ceiling, "
                "and newly clocked DSP footprint."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_registered_dsp_lane_rebuild",
            description=(
                "Framework-only complete DSP P-to-external-register transaction "
                "absorption with exact clock/CE/reset-word preservation."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "output_dcp",
                    "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="recover_control_to_data_state_equation_contract",
            description=(
                "Read-only exhaustive recovery of anonymous synchronous-set "
                "state equations, their live timing overlap and minimal care-set "
                "support. Exact cells remain in a framework-private manifest."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "manifest_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path",
                    "manifest_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_control_to_data_state_equation",
            description=(
                "Framework-only exact synchronous state-equation rewrite for "
                "one Luna-selected anonymous family."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "family_ref": {
                        "type": "string", "pattern": "^control_state_[1-9][0-9]*$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "manifest_path",
                    "probe_report_path", "family_ref", "output_dcp",
                    "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="recover_ff_srl_boundary_bridge_contract",
            description=(
                "Read-only recovery of anonymous direct-FF transport families "
                "with exact enabled-cycle and live timing-overlap evidence."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "manifest_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path",
                    "manifest_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="recover_memory_write_buffer_forward_contract",
            description=(
                "Read-only recovery of anonymous live RAMB36E2 write boundaries "
                "with exact adjacent-RAW forwarding and closed read-consumer proof."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "manifest_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path",
                    "manifest_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_memory_write_buffer_forward",
            description=(
                "Framework-only registered write buffering and adjacent-RAW "
                "forwarding for one Luna-selected anonymous BRAM family."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "family_ref": {
                        "type": "string",
                        "pattern": "^memory_forward_[1-9][0-9]*$",
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "manifest_path",
                    "probe_report_path", "family_ref", "output_dcp",
                    "report_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="recover_multilevel_control_distribution_contract",
            description=(
                "Read-only recovery of one anonymous complete LUT-driven "
                "registered control DAG and its live sink distribution."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "manifest_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path",
                    "manifest_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="apply_multilevel_control_distribution_localization",
            description=(
                "Framework-only exact Boolean-DAG cloning near registered "
                "sink regions for one Luna-selected anonymous family."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "family_ref": {
                        "type": "string",
                        "pattern": "^multilevel_control_[1-9][0-9]*$",
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "manifest_path",
                    "probe_report_path", "family_ref", "output_dcp",
                    "report_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="recover_multilevel_ram_control_distribution_contract",
            description=(
                "Read-only recovery of one anonymous complete LUT-driven "
                "RAM-control DAG, exact pin role, and all root-net consumers."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "run_id": {
                        "type": "string", "pattern": "^nr-[0-9a-f]{32}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "manifest_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "run_id", "report_path",
                    "manifest_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="apply_multilevel_ram_control_distribution_localization",
            description=(
                "Framework-only proof-gated cloning of one current-seed "
                "RAM-control DAG near same-role sink regions."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "run_id": {
                        "type": "string", "pattern": "^nr-[0-9a-f]{32}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "family_ref": {
                        "type": "string",
                        "pattern": "^multilevel_ram_control_[1-9][0-9]*$",
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "run_id", "manifest_path",
                    "probe_report_path", "family_ref", "output_dcp",
                    "report_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="recover_registered_parity_reassociation_contract",
            description=(
                "Read-only recovery of anonymous complete XOR/XNOR closures "
                "feeding live registered data endpoints."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "report_path": {"type": "string", "minLength": 1},
                    "manifest_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "report_path",
                    "manifest_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="apply_registered_parity_reassociation",
            description=(
                "Framework-only balanced GF(2) tree emission for one "
                "Luna-selected anonymous registered parity family."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "family_ref": {
                        "type": "string",
                        "pattern": "^registered_parity_[1-9][0-9]*$",
                    },
                    "implementation_mode": {
                        "type": "string",
                        "enum": [
                            "local_preserve", "closure_local_rebasin",
                            "global_quick_rebasin",
                        ],
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "manifest_path",
                    "probe_report_path", "family_ref", "implementation_mode",
                    "output_dcp", "report_path", "timeout_s",
                ],
            },
        ),
        Tool(
            name="apply_ff_srl_boundary_bridge",
            description=(
                "Framework-only exact direct-FF tail replacement for one "
                "Luna-selected anonymous family."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "probe_report_path": {"type": "string", "minLength": 1},
                    "family_ref": {
                        "type": "string", "pattern": "^ff_srl_[1-9][0-9]*$"
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "manifest_path",
                    "probe_report_path", "family_ref", "output_dcp",
                    "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="apply_registered_dead_state_elimination",
            description=(
                "Framework-only deletion of an exact current-DCP registered "
                "state family proven outside the output-observable closure."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"
                    },
                    "manifest_path": {"type": "string", "minLength": 1},
                    "output_dcp": {"type": "string", "minLength": 1},
                    "report_path": {"type": "string", "minLength": 1},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 4800},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "manifest_path",
                    "output_dcp", "report_path", "timeout_s"
                ],
            },
        ),
        Tool(
            name="restart_vivado",
            description="Kill the current Vivado instance and start a fresh one. Use if Vivado is hung or stuck.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="get_critical_high_fanout_nets",
            description="Extract high fanout nets from critical timing paths for optimization. Returns parent net names for RapidWright compatibility.",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of critical paths to analyze (default: 50)"
                    },
                    "min_fanout": {
                        "type": "number",
                        "description": "Minimum fanout threshold to report a net (default: 100)"
                    },
                    "exclude_clocks": {
                        "type": "boolean",
                        "description": "If True, exclude clock nets from results (default: True)"
                    },
                    "clock": {
                        "type": "string",
                        "description": "Optional capture clock name; limits analysis to setup paths ending in this clock domain"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="write_edif",
            description="Write an unencrypted EDIF netlist file. This is required when exporting designs for use with RapidWright, as the EDIF netlist inside DCPs is typically encrypted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "edif_path": {
                        "type": "string",
                        "description": "Path where the .edf or .edif file will be saved"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing file if True (default: False)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["edif_path"]
            }
        ),
        Tool(
            name="extract_critical_path_cells",
            description="""Extract cell names from critical timing paths.

            Parses timing report to get ordered list of cells on each critical path.
            Output is JSON that can be passed to RapidWright's
            analyze_critical_path_spread.

            For pin-level data (needed by analyze_net_detour), use
            extract_critical_path_pins instead.

            Can optionally write to a file for efficient data transfer.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of critical paths to extract (default: 50)"
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Optional: path to write JSON output to file instead of returning it"
                    },
                    "clock": {
                        "type": "string",
                        "description": "Optional capture clock name; limits extraction to setup paths ending in this clock domain"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="extract_critical_path_pins",
            description="""Extract pin-level paths from critical timing paths.

            Each path is a flat list of pin references like:
                ["src_ff/Q", "lut1/I2", "lut1/O", "lut2/I0", "lut2/O", "dst_ff/D"]

            Two consecutive pins from the same cell represent the cell's data path.
            Two consecutive pins from different cells represent a connecting net.
            This format allows RapidWright's analyze_net_detour to resolve nets and
            SitePinInsts via O(1) lookups with no scanning.

            Can optionally write to a file for efficient data transfer.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of critical paths to extract (default: 50)"
                    },
                    "output_file": {
                        "type": "string",
                        "description": "Optional: path to write JSON output to file instead of returning it"
                    },
                    "clock": {
                        "type": "string",
                        "description": "Optional capture clock name; limits extraction to setup paths ending in this clock domain"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="report_timing_path_profile",
            description="""Summarize worst setup timing paths as compact JSON.

            Reports aggregate route/logic delay percentages, logic-level
            histogram, repeated sources/destinations, physical spread of
            critical path sites, and top critical nets.
            Use this before selecting placement, routing, fanout, or LUT-cone
            optimizations.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "num_paths": {
                        "type": "number",
                        "description": "Number of worst timing paths to profile (default: 50)"
                    },
                    "clock": {
                        "type": "string",
                        "description": "Optional capture clock name; limits profiling to setup paths ending in this clock domain"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 600)"
                    }
                }
            }
        ),
        Tool(
            name="report_utilization_for_pblock",
            description="""Get design resource utilization for pblock sizing.

            Returns counts of LUTs, FFs, DSPs, BRAMs, URAMs with both actual usage and
            1.5x multiplied values for pblock size calculation.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="analyze_carry_clouds",
            description=(
                "Read-only bounded analysis of CARRY4/CARRY8 chains and the LUT, "
                "register, and other primitive cloud driving S/DI inputs."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_cells": {
                        "type": "integer", "minimum": 1,
                        "maximum": MAX_STRUCTURAL_CELLS, "default": 128,
                    },
                    "timeout": {
                        "type": "number", "minimum": 1, "maximum": 600,
                        "default": 300,
                    },
                },
            },
        ),
        Tool(
            name="analyze_hard_macro_cascades",
            description=(
                "Read-only bounded analysis of DSP/BRAM/URAM placement, pipeline "
                "register properties, and dedicated cascade connectivity."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_cells": {
                        "type": "integer", "minimum": 1,
                        "maximum": MAX_STRUCTURAL_CELLS, "default": 128,
                    },
                    "timeout": {
                        "type": "number", "minimum": 1, "maximum": 600,
                        "default": 300,
                    },
                },
            },
        ),
        Tool(
            name="measure_connected_recurrence_slots",
            description=(
                "Transactionally place real connected SRLC32E and CE-clone "
                "primitives at bounded candidate BELs, classify cascade-region "
                "membership, prove exact preexisting clock-leaf reuse, then reopen the "
                "exact source DCP."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "region_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "data_pin": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "clock_pin": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "ce_source_pin": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "addresses": {
                        "type": "array", "minItems": 1, "maxItems": 4,
                        "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 0, "maximum": 31},
                    },
                    "incumbent_clock_regions": {
                        "type": "array", "minItems": 1, "maxItems": 16,
                        "uniqueItems": True, "items": {"type": "string"},
                    },
                    "srl_candidates": {
                        "type": "array", "minItems": 1,
                        "maxItems": MAX_SRL_CANDIDATES,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "site": {"type": "string"},
                                "bel": {"type": "string"},
                                "source_distance": {
                                    "type": "integer", "minimum": 0, "maximum": 100000,
                                },
                                "reinjection_distance": {
                                    "type": "integer", "minimum": 0, "maximum": 100000,
                                },
                            },
                            "required": [
                                "site", "bel", "source_distance", "reinjection_distance",
                            ],
                        },
                    },
                    "ce_clone_candidates": {
                        "type": "array", "minItems": 1,
                        "maxItems": MAX_CE_CLONE_CANDIDATES,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "site": {"type": "string"},
                                "bel": {"type": "string"},
                                "source_distance": {
                                    "type": "integer", "minimum": 0, "maximum": 100000,
                                },
                                "reinjection_distance": {
                                    "type": "integer", "minimum": 0, "maximum": 100000,
                                },
                            },
                            "required": [
                                "site", "bel", "source_distance", "reinjection_distance",
                            ],
                        },
                    },
                    "max_place_attempts": {
                        "type": "integer", "minimum": 1,
                        "maximum": MAX_PLACE_ATTEMPTS,
                    },
                    "timeout": {
                        "type": "number", "minimum": 1,
                        "maximum": MAX_ORACLE_TIMEOUT_S,
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "region_sha256", "data_pin",
                    "clock_pin", "ce_source_pin", "addresses",
                    "incumbent_clock_regions", "srl_candidates",
                    "ce_clone_candidates", "max_place_attempts", "timeout",
                ],
            },
        ),
        Tool(
            name="measure_connected_dsp_slots",
            description=(
                "Transactionally place one real clock-connected DSP48E2 at "
                "bounded candidate sites, classify clock-region feasibility, "
                "discard the temporary namespace, then reopen the exact "
                "source DCP."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "region_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "clock_pin": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "candidate_sites": {
                        "type": "array", "minItems": 1,
                        "maxItems": MAX_DSP_SLOT_CANDIDATES,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "site": {
                                    "type": "string",
                                    "pattern": "^DSP48E2_X[0-9]+Y[0-9]+$",
                                },
                                "distance": {
                                    "type": "integer", "minimum": 0, "maximum": 100000,
                                },
                            },
                            "required": ["site", "distance"],
                        },
                    },
                    "timeout": {
                        "type": "number", "minimum": 1,
                        "maximum": MAX_ORACLE_TIMEOUT_S,
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "region_sha256",
                    "clock_pin", "candidate_sites", "timeout",
                ],
            },
        ),
        Tool(
            name="extract_registered_closure_facts",
            description="Internal read-only multi-stage registered closure extraction.",
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "target_stem": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "output_dir": {"type": "string", "minLength": 1},
                    "max_stage_depth": {"type": "integer", "minimum": 1, "maximum": 8},
                    "word_lsb": {"type": "integer", "minimum": 0, "maximum": 1000000},
                    "word_width": {"type": "integer", "minimum": 2, "maximum": 1024},
                    "timeout_s": {"type": "number", "minimum": 30, "maximum": 900},
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "target_stem",
                    "output_dir", "max_stage_depth", "word_lsb", "word_width", "timeout_s",
                ],
            },
        ),
        Tool(
            name="synthesize_registered_product_modules",
            description="Internal OOC synthesis for current-DCP recovered product modules.",
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "bundle_path": {"type": "string", "minLength": 1},
                    "work_dir": {"type": "string", "minLength": 1},
                    "run_id": {"type": "string", "pattern": "^nr-[0-9a-f]{32}$"},
                    "run_source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "clock_name": {"type": "string", "minLength": 1},
                },
                "required": ["dcp_path", "source_dcp_sha256", "bundle_path", "work_dir"],
            },
        ),
        Tool(
            name="route_registered_product_bundle",
            description="Internal wiring and routed implementation for a recovered product bundle.",
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "inserted_dcp": {"type": "string", "minLength": 1},
                    "source_dcp": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "bundle_path": {"type": "string", "minLength": 1},
                    "run_id": {"type": "string", "pattern": "^nr-[0-9a-f]{32}$"},
                    "run_source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "current_run_authorization_path": {"type": "string", "minLength": 1},
                    "output_dcp": {"type": "string", "minLength": 1},
                    "route_timeout_s": {"type": "number", "minimum": 1, "maximum": 3600},
                    "physical_profile": {
                        "type": "string",
                        "enum": ["semantic_rewrite", "compact_timing", "net_delay_focus", "full_timing"],
                    },
                },
                "required": [
                    "inserted_dcp", "source_dcp", "source_dcp_sha256",
                    "bundle_path", "output_dcp",
                    "route_timeout_s", "physical_profile",
                ],
            },
        ),
        Tool(
            name="suggest_slice_pblock_ranges",
            description="""Suggest slice-only pblock ranges from current placement.

            Read-only helper for designs dominated by SLICE primitives. It
            returns compact central windows, occupied SLICE bounding box
            expansions, and hard-macro counts so the agent can decide whether a
            slice-only pblock sweep is appropriate.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_candidates": {
                        "type": "number",
                        "description": "Maximum candidate ranges to return (default: 8)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                }
            }
        ),
        Tool(
            name="create_and_apply_pblock",
            description="""Create a pblock (area constraint) and apply it to the design.

            A pblock restricts placement to a specific region of the FPGA. This can improve timing
            by reducing routing distances for spread-out designs. After applying a pblock, you must
            run place_design and route_design to implement the constraint.

            Range format examples:
            - SLICE_X0Y0:SLICE_X100Y200 (specific slice ranges)
            - CLOCKREGION_X0Y0:CLOCKREGION_X2Y3 (clock region ranges)

            Set is_soft=False for hard constraints that must be met.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "pblock_name": {
                        "type": "string",
                        "description": "Name for the pblock (e.g., 'pblock_tight')"
                    },
                    "ranges": {
                        "type": "string",
                        "description": "Pblock range (e.g., 'SLICE_X0Y0:SLICE_X100Y100' or 'CLOCKREGION_X0Y0:CLOCKREGION_X2Y3')"
                    },
                    "apply_to": {
                        "type": "string",
                        "description": "What to constrain: 'current_design' (all cells) or a cell pattern (default: 'current_design')"
                    },
                    "is_soft": {
                        "type": "boolean",
                        "description": "If false, creates hard constraint (IS_SOFT=0) (default: false)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    },
                    "validate_resources": {
                        "type": "boolean",
                        "description": "Validate pblock resources and auto-expand if needed (default: true)"
                    },
                    "max_expansion_attempts": {
                        "type": "integer",
                        "description": "Maximum resource-validation expansion retries (default: 3)"
                    }
                },
                "required": ["pblock_name", "ranges"]
            }
        ),
        Tool(
            name="create_and_apply_multiregion_pblocks",
            description=(
                "Apply disjoint clock-region pblocks to a current-seed critical "
                "cell partition and its remainder."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pblock_prefix": {
                        "type": "string",
                        "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$",
                    },
                    "critical_range": {
                        "type": "string",
                        "pattern": "^CLOCKREGION_X[0-9]+Y[0-9]+:CLOCKREGION_X[0-9]+Y[0-9]+$",
                    },
                    "remainder_range": {
                        "type": "string",
                        "pattern": "^CLOCKREGION_X[0-9]+Y[0-9]+:CLOCKREGION_X[0-9]+Y[0-9]+$",
                    },
                    "critical_cell_names": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 256,
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "is_soft": {"type": "boolean"},
                    "validate_resources": {"type": "boolean"},
                    "timeout": {"type": "number", "minimum": 1, "maximum": 300},
                },
                "required": [
                    "pblock_prefix",
                    "critical_range",
                    "remainder_range",
                    "critical_cell_names",
                ],
            },
        ),
        Tool(
            name="write_verilog_simulation",
            description="""Export design as a Verilog functional simulation model.

            Generates a Verilog netlist suitable for simulation. This is required for
            functional equivalence checking via simulation. The output netlist can be
            used with xsim or other Verilog simulators.

            Use -mode funcsim for functional simulation (no timing).""",
            inputSchema={
                "type": "object",
                "properties": {
                    "verilog_path": {
                        "type": "string",
                        "description": "Path where the .v file will be saved"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing file if True (default: False)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 300)"
                    }
                },
                "required": ["verilog_path"]
            }
        ),
        Tool(
            name="phys_opt_design",
            description="""Run physical optimization on the current design to improve timing (WNS/TNS).

            Can be run post-place (after place_design) or post-route (after route_design). Performs timing-driven
            optimization on negative-slack paths. The command operates on the in-memory design and can be run
            iteratively for additional improvements.

            Post-place optimizations (default): fanout optimization, placement optimization, LUT restructure,
            critical-cell optimization, DSP/BRAM/URAM register optimization.

            Post-route optimizations (default): placement optimization, routing optimization, LUT restructure,
            critical-cell optimization.

            NOTE: Using specific optimization options disables default optimizations - only specified ones run.
            The directive option is incompatible with specific optimization options.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "directive": {
                        "type": "string",
                        "description": """Physical optimization directive. Only one can be specified at a time, and incompatible with other options:
                        - Default: Run phys_opt_design with default settings
                        - Explore: Multiple passes with replication for very high fanout nets, SLR crossing optimization, and critical path optimization
                        - ExploreWithHoldFix: Multiple passes including hold violation fixing, SLR crossing optimization, and replication for very high fanout nets
                        - ExploreWithAggressiveHoldFix: Multiple passes with aggressive hold violation fixing, SLR crossing optimization, and replication
                        - AggressiveExplore: Similar to Explore but with more aggressive algorithms; includes SLR crossing optimization that may temporarily degrade WNS
                        - AlternateReplication: Use different algorithms for performing critical cell replication
                        - AggressiveFanoutOpt: Use different algorithms for fanout-related optimizations with more aggressive goals
                        - AlternateFlowWithRetiming: Perform more aggressive replication and DSP/BRAM optimization with register retiming enabled
                        - AddRetime: Performs the default phys_opt_design flow and adds register retiming
                        - RuntimeOptimized: Reduced set of optimizations (fanout_opt, critical_cell_opt, placement_opt, bram_enable_opt) for shortest runtime
                        - RQS: Select directive from report_qor_suggestions strategy (requires RQS file)"""
                    },
                    "fanout_opt": {
                        "type": "boolean",
                        "description": "[Note: Cannot be used for post route design, use the optimization from RapidWright instead.] Delay-driven optimization on high-fanout timing critical nets by replicating drivers (not applicable for Versal)"
                    },
                    "placement_opt": {
                        "type": "boolean",
                        "description": "Move cells to reduce delay on timing-critical nets (not applicable for Versal)"
                    },
                    "routing_opt": {
                        "type": "boolean",
                        "description": "Perform routing optimization on timing-critical nets to reduce delay"
                    },
                    "slr_crossing_opt": {
                        "type": "boolean",
                        "description": "Optimize placement of inter-SLR connections (UltraScale/UltraScale+ only)"
                    },
                    "insert_negative_edge_ffs": {
                        "type": "boolean",
                        "description": "Insert negative edge triggered FFs for hold optimization"
                    },
                    "restruct_opt": {
                        "type": "boolean",
                        "description": "Advanced LUT restructure optimization to reduce logic levels and delay on critical signals"
                    },
                    "interconnect_retime": {
                        "type": "boolean",
                        "description": "Perform interconnect retiming by moving/replicating FF or LUT-FF pairs (Versal only)"
                    },
                    "lut_opt": {
                        "type": "boolean",
                        "description": "Perform LUT movement/replication to improve critical path timing (Versal only)"
                    },
                    "casc_opt": {
                        "type": "boolean",
                        "description": "Perform LUT cascade optimization for creating/moving LUT cascades (Versal only)"
                    },
                    "cell_group_opt": {
                        "type": "boolean",
                        "description": "Perform critical cell group optimization"
                    },
                    "equ_drivers_opt": {
                        "type": "boolean",
                        "description": "Rewire load pins to equivalent drivers"
                    },
                    "critical_cell_opt": {
                        "type": "boolean",
                        "description": "Cell-duplication based optimization on timing critical nets (not applicable for Versal)"
                    },
                    "dsp_register_opt": {
                        "type": "boolean",
                        "description": "Move registers between slices and DSP blocks to improve critical path delay"
                    },
                    "bram_register_opt": {
                        "type": "boolean",
                        "description": "Move registers between slices and block RAMs to improve critical path delay"
                    },
                    "uram_register_opt": {
                        "type": "boolean",
                        "description": "Move registers between slices and UltraRAMs to improve critical path delay"
                    },
                    "bram_enable_opt": {
                        "type": "boolean",
                        "description": "Improve timing on critical paths involving power-optimized block RAMs by reversing enable-logic optimization"
                    },
                    "shift_register_opt": {
                        "type": "boolean",
                        "description": "Perform shift register optimization by extracting registers from SRL chains to improve timing"
                    },
                    "hold_fix": {
                        "type": "boolean",
                        "description": "Insert data path delay to fix hold time violations"
                    },
                    "aggressive_hold_fix": {
                        "type": "boolean",
                        "description": "Aggressively insert data path delay to fix hold time violations (considers more violations than standard hold fix)"
                    },
                    "retime": {
                        "type": "boolean",
                        "description": "Re-time registers forward through combinational logic to balance path delays (property-driven)"
                    },
                    "force_replication_on_nets": {
                        "type": "string",
                        "description": "Force replication on specific nets regardless of slack (e.g., net names or Tcl command like '[get_nets -hier *phy_reset*]')"
                    },
                    "critical_pin_opt": {
                        "type": "boolean",
                        "description": "Perform LUT pin-swapping (remap logical to physical pins) to improve critical path timing. Skips cells with LOCK_PINS property."
                    },
                    "clock_opt": {
                        "type": "boolean",
                        "description": "Perform clock skew optimization during post-route optimization by inserting global clock buffers"
                    },
                    "path_groups": {
                        "type": "string",
                        "description": "Perform optimizations on specified path groups only (e.g., 'clk_group1 clk_group2')"
                    },
                    "tns_cleanup": {
                        "type": "boolean",
                        "description": "Total Negative Slack cleanup (use with slr_crossing_opt). Allows some slack degradation if overall WNS doesn't degrade."
                    },
                    "sll_reg_hold_fix": {
                        "type": "boolean",
                        "description": "Perform SLL register hold fix optimization for SLR crossing paths (not applicable for Versal)"
                    },
                    "memory_rewire_opt": {
                        "type": "boolean",
                        "description": "Rewire critical signals to faster pins of BRAM/URAM (Versal only, not for cascaded/ECC memories)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in seconds (default: 3600 for physical optimization)"
                    }
                }
            }
        ),
        Tool(
            name="apply_constructive_accumulator_rebuild",
            description=(
                "Internal skill-only constructive registered accumulator "
                "mutation. Replays current-seed cone proofs and accepts no Tcl "
                "or caller-selected object names."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "cone_dirs": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "certificate_paths": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "authorization_path": {
                        "type": "string", "minLength": 1,
                    },
                    "authorization_id": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "route_timeout_s": {
                        "type": "number", "minimum": 1, "maximum": 3600,
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "cone_dirs",
                    "certificate_paths", "authorization_path",
                    "authorization_id", "output_dcp", "route_timeout_s",
                ],
            },
        ),
        Tool(
            name="apply_screened_fixed_point_dsp_eco",
            description=(
                "Internal skill-only fixed-point DSP mutation. Replays a "
                "repository-generated contract and authorization; callers "
                "cannot provide Tcl or arbitrary object names."
            ),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "cone_dir": {"type": "string", "minLength": 1},
                    "contract_path": {"type": "string", "minLength": 1},
                    "contract_id": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "authorization_id": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "route_timeout_s": {
                        "type": "number", "minimum": 1, "maximum": 3600,
                    },
                    "physical_profile": {
                        "type": "string",
                        "enum": ["compact_timing", "net_delay_focus"],
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "cone_dir",
                    "contract_path", "contract_id", "authorization_id",
                    "output_dcp", "route_timeout_s", "physical_profile",
                ],
            },
        ),
        Tool(
            name="apply_proved_arithmetic_compressor_eco",
            description=(
                "Internal skill-only arithmetic-compressor mutation bound to "
                "an exact source, cone certificate, and authorization."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "cone_dir": {"type": "string", "minLength": 1},
                    "certificate_path": {"type": "string", "minLength": 1},
                    "certificate_id": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "authorization_id": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "output_dcp": {"type": "string", "minLength": 1},
                    "route_timeout_s": {
                        "type": "number", "minimum": 1, "maximum": 3600,
                    },
                    "physical_profile": {
                        "type": "string",
                        "enum": ["compact_timing", "net_delay_focus"],
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "cone_dir",
                    "certificate_path", "certificate_id", "authorization_id",
                    "output_dcp", "route_timeout_s", "physical_profile",
                ],
            },
        ),
        Tool(
            name="apply_proved_recurrence_eco",
            description=(
                "Internal skill-only recurrence mutation using exact live facts, "
                "a framework candidate, and a proved placement."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "facts": {"type": "object"},
                    "candidate": {"type": "object"},
                    "placement": {"type": "object"},
                    "output_dcp": {"type": "string", "minLength": 1},
                    "route_timeout_s": {
                        "type": "number", "minimum": 1, "maximum": 3600,
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "facts", "candidate",
                    "placement", "output_dcp", "route_timeout_s",
                ],
            },
        ),
        Tool(
            name="apply_proved_recurrence_family_eco",
            description=(
                "Internal skill-only atomic recurrence-family mutation using "
                "exact live family facts and a proved placement."
            ),
            inputSchema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "dcp_path": {"type": "string", "minLength": 1},
                    "source_dcp_sha256": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$",
                    },
                    "family_facts": {"type": "object"},
                    "candidate": {"type": "object"},
                    "placement": {"type": "object"},
                    "output_dcp": {"type": "string", "minLength": 1},
                    "route_timeout_s": {
                        "type": "number", "minimum": 1, "maximum": 3600,
                    },
                },
                "required": [
                    "dcp_path", "source_dcp_sha256", "family_facts",
                    "candidate", "placement", "output_dcp", "route_timeout_s",
                ],
            },
        ),
        Tool(
            name="extract_register_cone_facts",
            description=(
                "Internal read-only register-cone extraction used by registered "
                "operator-rewrite skills. The fixed extractor restores the exact "
                "source checkpoint before returning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dcp_path": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "source_dcp_sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "target_stem": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1024,
                    },
                    "output_dir": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
                "required": [
                    "dcp_path",
                    "source_dcp_sha256",
                    "target_stem",
                    "output_dir",
                ],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Handle tool calls."""
    global _design_open

    logger.info(f"Tool called: {name}")

    try:
        if name == "open_checkpoint":
            dcp_path = arguments["dcp_path"]
            timeout = arguments.get("timeout", 300)

            # Close existing design if open
            if _design_open:
                close_current_design()

            # Open the checkpoint
            output = run_tcl_command(f"open_checkpoint {{{dcp_path}}}", timeout=timeout)
            _design_open = True
            return [TextContent(type="text", text=f"Opened checkpoint: {dcp_path}\n\n{output}")]

        elif name == "write_checkpoint":
            dcp_path = arguments["dcp_path"]
            force = arguments.get("force", False)
            timeout = arguments.get("timeout", 300)

            force_flag = " -force" if force else ""
            output = run_tcl_command(f"write_checkpoint{force_flag} {{{dcp_path}}}", timeout=timeout)
            return [TextContent(type="text", text=f"Wrote checkpoint: {dcp_path}\n\n{output}")]

        elif name == "report_route_status":
            timeout = arguments.get("timeout", 300)
            # Run a quick command first to flush any leftover output from previous commands
            run_tcl_command("puts {route_status_start}", timeout=5)
            output = run_tcl_command("report_route_status -return_string", timeout=timeout)
            return [TextContent(type="text", text=output)]

        elif name == "report_timing_summary":
            timeout = arguments.get("timeout", 300)
            # Run a quick command first to flush any leftover output from previous commands
            run_tcl_command("puts {timing_summary_start}", timeout=5)
            output = run_tcl_command("report_timing_summary -return_string", timeout=timeout)
            return [TextContent(type="text", text=output)]

        elif name == "get_wns":
            timeout = arguments.get("timeout", 300)
            clock = arguments.get("clock", None)
            # Flush buffer first
            run_tcl_command("puts {get_wns_start}", timeout=5)
            if clock:
                tcl_cmd = (
                    f"set clk_obj [get_clocks -quiet {{{clock}}}]; "
                    f"if {{$clk_obj ne {{}}}} {{ "
                    f"  set wns_path [get_timing_paths -max_paths 1 -setup -to $clk_obj]; "
                    f"  if {{[llength $wns_path] > 0}} {{get_property SLACK $wns_path}} else {{puts 0.0}} "
                    f"}} else {{puts {{ERROR: clock not found}}}}"
                )
            else:
                tcl_cmd = "set wns_path [get_timing_paths -max_paths 1 -slack_lesser_than 999]; if {[llength $wns_path] > 0} {get_property SLACK $wns_path} else {puts 0.0}"
            output = run_tcl_command(tcl_cmd, timeout=timeout)
            # Clean up the output to just return the number
            wns_value = output.strip().split('\n')[-1].strip()
            return [TextContent(type="text", text=wns_value)]

        elif name == "place_design":
            directive = arguments.get("directive")
            timeout = arguments.get("timeout", 3600)  # 1 hour default for placement

            cmd = "place_design"
            if directive:
                cmd += f" -directive {directive}"

            output = run_tcl_command(cmd, timeout=timeout)
            return [TextContent(type="text", text=f"Placement complete.\n\n{output}")]

        elif name == "route_design":
            directive = arguments.get("directive")
            timeout = arguments.get("timeout", 3600)  # 1 hour default for routing

            cmd = "route_design"
            if directive:
                cmd += f" -directive {directive}"

            output = run_tcl_command(cmd, timeout=timeout)
            return [TextContent(type="text", text=f"Routing complete.\n\n{output}")]

        elif name == "recover_banked_select_predicate_contract":
            output = recover_banked_select_predicate_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_banked_select_predicate_update":
            output = apply_banked_select_predicate_update(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_registered_dsp_lane_contract":
            output = recover_registered_dsp_lane_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_registered_dsp_lane_rebuild":
            output = apply_registered_dsp_lane_rebuild(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_control_to_data_state_equation_contract":
            output = recover_control_to_data_state_equation_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_control_to_data_state_equation":
            output = apply_control_to_data_state_equation(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_ff_srl_boundary_bridge_contract":
            output = recover_ff_srl_boundary_bridge_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_memory_write_buffer_forward_contract":
            output = recover_memory_write_buffer_forward_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_memory_write_buffer_forward":
            output = apply_memory_write_buffer_forward(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_multilevel_control_distribution_contract":
            output = recover_multilevel_control_distribution_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_multilevel_control_distribution_localization":
            output = apply_multilevel_control_distribution_localization(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_multilevel_ram_control_distribution_contract":
            output = recover_multilevel_ram_control_distribution_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_multilevel_ram_control_distribution_localization":
            output = apply_multilevel_ram_control_distribution_localization(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_registered_parity_reassociation_contract":
            output = recover_registered_parity_reassociation_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_registered_parity_reassociation":
            output = apply_registered_parity_reassociation(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_ff_srl_boundary_bridge":
            output = apply_ff_srl_boundary_bridge(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_registered_dead_state_elimination":
            output = apply_registered_dead_state_elimination(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_banked_ram_operator_contract":
            output = recover_banked_ram_operator_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "recover_commutative_reassociation_contract":
            output = recover_commutative_reassociation_contract(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_commutative_operand_reassociation":
            output = apply_commutative_operand_reassociation(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_registered_control_packet_localization":
            output = apply_registered_control_packet_localization(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "apply_registered_ram_boolean_midpoint":
            output = apply_registered_ram_boolean_midpoint(arguments)
            return [TextContent(type="text", text=output)]

        elif name == "run_tcl":
            command = arguments["command"]
            timeout = arguments.get("timeout", 300)
            output = run_tcl_command(command, timeout=timeout)
            return [TextContent(type="text", text=output)]

        elif name == "restart_vivado":
            output = restart_vivado_process()
            return [TextContent(type="text", text=output)]

        elif name == "get_critical_high_fanout_nets":
            num_paths = arguments.get("num_paths", 50)
            min_fanout = arguments.get("min_fanout", HIGH_FANOUT_MIN_FANOUT)
            exclude_clocks = arguments.get("exclude_clocks", True)
            timeout = arguments.get("timeout", 600)
            clock = arguments.get("clock")

            output = get_critical_high_fanout_nets(
                num_paths, min_fanout, exclude_clocks, timeout, clock
            )
            return [TextContent(type="text", text=output)]

        elif name == "write_edif":
            edif_path = arguments["edif_path"]
            force = arguments.get("force", False)
            timeout = arguments.get("timeout", 300)

            force_flag = " -force" if force else ""
            output = run_tcl_command(f"write_edif{force_flag} {{{edif_path}}}", timeout=timeout)
            return [TextContent(type="text", text=f"Wrote EDIF netlist: {edif_path}\n\n{output}")]

        elif name == "extract_critical_path_cells":
            num_paths = arguments.get("num_paths", 50)
            output_file = arguments.get("output_file")
            timeout = arguments.get("timeout", 600)
            clock = arguments.get("clock")

            output = extract_critical_path_cells(num_paths, output_file, timeout, clock)
            return [TextContent(type="text", text=output)]

        elif name == "extract_critical_path_pins":
            num_paths = arguments.get("num_paths", 50)
            output_file = arguments.get("output_file")
            timeout = arguments.get("timeout", 600)
            clock = arguments.get("clock")

            output = extract_critical_path_pins(num_paths, output_file, timeout, clock)
            return [TextContent(type="text", text=output)]

        elif name == "report_timing_path_profile":
            num_paths = arguments.get("num_paths", 50)
            timeout = arguments.get("timeout", 600)
            clock = arguments.get("clock")

            output = report_timing_path_profile(num_paths, timeout, clock)
            return [TextContent(type="text", text=output)]

        elif name == "report_utilization_for_pblock":
            timeout = arguments.get("timeout", 300)
            output = report_utilization_for_pblock(timeout)
            return [TextContent(type="text", text=output)]

        elif name == "analyze_carry_clouds":
            output = analyze_carry_clouds(
                arguments.get("max_cells", 128), arguments.get("timeout", 300)
            )
            return [TextContent(type="text", text=output)]

        elif name == "analyze_hard_macro_cascades":
            output = analyze_hard_macro_cascades(
                arguments.get("max_cells", 128), arguments.get("timeout", 300)
            )
            return [TextContent(type="text", text=output)]

        elif name == "measure_connected_recurrence_slots":
            request = dict(arguments)
            dcp_path = request.pop("dcp_path")
            output = measure_connected_recurrence_slots(dcp_path, request)
            return [TextContent(type="text", text=output)]

        elif name == "measure_connected_dsp_slots":
            request = dict(arguments)
            dcp_path = request.pop("dcp_path")
            output = measure_connected_dsp_slots(dcp_path, request)
            return [TextContent(type="text", text=output)]

        elif name == "extract_registered_closure_facts":
            output = extract_registered_closure_facts(dict(arguments))
            return [TextContent(type="text", text=output)]

        elif name == "synthesize_registered_product_modules":
            output = synthesize_registered_product_modules(dict(arguments))
            return [TextContent(type="text", text=output)]

        elif name == "route_registered_product_bundle":
            output = route_registered_product_bundle(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only mutation. Fixed builder, no caller-provided Tcl.
        elif name == "apply_proved_recurrence_eco":
            output = apply_proved_recurrence_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only mutation. Fixed builder, no caller-provided Tcl.
        elif name == "apply_proved_fixed_point_eco":
            output = apply_proved_fixed_point_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only mutation. Exact facts only, no caller-provided Tcl.
        elif name == "apply_proved_memory_repack_eco":
            output = apply_proved_memory_repack_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal atomic DAG execution. Resolutions are proof objects, never Tcl.
        elif name == "apply_semantic_rebuild_atomic_eco":
            output = apply_semantic_rebuild_atomic_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only mutation. Fixed builder, no caller-provided Tcl.
        elif name == "apply_proved_two_operand_eco":
            output = apply_proved_two_operand_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only mutation. Fixed builder, no caller-provided Tcl.
        elif name == "apply_proved_register_absorb_eco":
            output = apply_proved_register_absorb_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only mutation. Fixed builder, no caller-provided Tcl.
        elif name == "apply_proved_pipeline_rebalance_eco":
            output = apply_proved_pipeline_rebalance_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal skill-only atomic family mutation. Fixed builder, no raw Tcl.
        elif name == "apply_proved_recurrence_family_eco":
            output = apply_proved_recurrence_family_eco(dict(arguments))
            return [TextContent(type="text", text=output)]

        # Internal read-only replay for route/placement-polished family candidates.
        elif name == "attest_proved_recurrence_family_candidate":
            output = attest_proved_recurrence_family_candidate(dict(arguments))
            return [TextContent(type="text", text=output)]

        elif name == "suggest_slice_pblock_ranges":
            max_candidates = arguments.get("max_candidates", 8)
            timeout = arguments.get("timeout", 300)

            output = suggest_slice_pblock_ranges(max_candidates, timeout)
            return [TextContent(type="text", text=output)]

        elif name == "create_and_apply_pblock":
            pblock_name = arguments["pblock_name"]
            ranges = arguments["ranges"]
            apply_to = arguments.get("apply_to", "current_design")
            is_soft = arguments.get("is_soft", False)
            timeout = arguments.get("timeout", 300)
            validate_resources = arguments.get("validate_resources", True)
            max_expansion_attempts = int(arguments.get("max_expansion_attempts", 3))

            output = create_and_apply_pblock(
                pblock_name,
                ranges,
                apply_to,
                is_soft,
                timeout,
                validate_resources,
                max_expansion_attempts,
            )
            return [TextContent(type="text", text=output)]

        elif name == "create_and_apply_multiregion_pblocks":
            output = create_and_apply_multiregion_pblocks(
                arguments["pblock_prefix"],
                arguments["critical_range"],
                arguments["remainder_range"],
                arguments["critical_cell_names"],
                arguments.get("is_soft", False),
                arguments.get("validate_resources", True),
                arguments.get("timeout", 300),
            )
            return [TextContent(type="text", text=output)]

        elif name == "write_verilog_simulation":
            verilog_path = Path(arguments["verilog_path"]).expanduser().resolve()
            force = arguments.get("force", False)
            timeout = arguments.get("timeout", 300)

            if verilog_path.parent.is_symlink() or not verilog_path.parent.is_dir():
                raise ValueError("Verilog simulation output parent is unavailable")
            if verilog_path.exists() and (
                verilog_path.is_symlink() or not verilog_path.is_file()
            ):
                raise ValueError("Verilog simulation output path is unsafe")
            force_flag = " -force" if force else ""
            # Use -mode funcsim for functional simulation
            output = run_tcl_command(f"write_verilog{force_flag} -mode funcsim {{{verilog_path}}}", timeout=timeout)
            if (
                verilog_path.is_symlink()
                or not verilog_path.is_file()
                or verilog_path.stat().st_size <= 0
            ):
                diagnostic = str(output)[-4000:]
                raise RuntimeError(
                    "Vivado write_verilog returned without a non-empty output; "
                    f"tcl_output_tail={diagnostic!r}"
                )
            return [TextContent(type="text", text=f"Wrote Verilog simulation model: {verilog_path}\n\n{output}")]

        elif name == "phys_opt_design":
            timeout = arguments.get("timeout", 3600)  # 1 hour default for physical optimization

            cmd = "phys_opt_design"

            # Directive option (incompatible with other options)
            directive = arguments.get("directive")
            if directive:
                cmd += f" -directive {directive}"
            else:
                # Build command with specific optimization options
                # Boolean flags
                bool_options = [
                    "fanout_opt", "placement_opt", "routing_opt", "slr_crossing_opt",
                    "insert_negative_edge_ffs", "restruct_opt", "interconnect_retime",
                    "lut_opt", "casc_opt", "cell_group_opt", "equ_drivers_opt",
                    "critical_cell_opt", "dsp_register_opt", "bram_register_opt",
                    "uram_register_opt", "bram_enable_opt", "shift_register_opt",
                    "hold_fix", "aggressive_hold_fix", "retime", "critical_pin_opt",
                    "clock_opt", "tns_cleanup", "sll_reg_hold_fix", "memory_rewire_opt"
                ]

                for opt in bool_options:
                    if arguments.get(opt):
                        cmd += f" -{opt}"

                # String options
                force_replication = arguments.get("force_replication_on_nets")
                if force_replication:
                    cmd += f" -force_replication_on_nets {force_replication}"

                path_groups = arguments.get("path_groups")
                if path_groups:
                    cmd += f" -path_groups {{{path_groups}}}"

            output = run_tcl_command(cmd, timeout=timeout)
            return [TextContent(type="text", text=f"Physical optimization complete.\n\n{output}")]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except pexpect.TIMEOUT:
        return [TextContent(
            type="text",
            text=f"Error: Command timed out. Vivado may be stuck. Use restart_vivado to recover."
        )]
    except pexpect.EOF:
        return [TextContent(
            type="text",
            text="Error: Vivado process terminated unexpectedly. Use restart_vivado to restart."
        )]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    """Main entry point."""
    global _vivado_path, _vivado_log_file, _vivado_journal_file

    parser = argparse.ArgumentParser(description="Vivado MCP Server")
    parser.add_argument(
        "--vivado-path",
        type=str,
        help="Path to Vivado executable (default: search in PATH)"
    )
    parser.add_argument(
        "--vivado-log",
        type=str,
        help="Path to Vivado log file (default: vivado.log)"
    )
    parser.add_argument(
        "--vivado-journal",
        type=str,
        help="Path to Vivado journal file (default: vivado.jou)"
    )

    args = parser.parse_args()

    if args.vivado_path:
        _vivado_path = args.vivado_path

    if args.vivado_log:
        _vivado_log_file = args.vivado_log

    if args.vivado_journal:
        _vivado_journal_file = args.vivado_journal

    logger.info("Starting Vivado MCP Server...")

    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Server running on stdio transport")
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
