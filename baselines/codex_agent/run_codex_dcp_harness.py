#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock, Thread

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from FDAgents.harness_policy import build_decision_policy_prompt
from FDAgents.llm import _lookup_pricing
from FDAgents.recipe_planner import (
    excluded_knowledge_design_ids_from_manifest,
    normalize_knowledge_design_id,
)
from usage_accounting import UNKNOWN, parse_codex_usage


REQUIRED_NATIVE_MCP_SERVERS = ("vivado", "rapidwright")
DEFAULT_CODEX_PROVIDER = "custom"

FORBIDDEN_CODEX_DELEGATION_PATTERNS = (
    "FDAgents/",
    "FDAgents.agent",
    "FDAgents/agent.py",
    "FDAgents.harness_policy",
    "FDAgents.recipe_planner",
    "run_fdagents_manifest.py",
    "batch_fdagents.py",
    "dcp_optimizer.py",
    "VivadoMCP/vivado_mcp_server.py",
    "vivado_mcp_server.py",
    "RapidWrightMCP/server.py",
)

FIELDS = [
    "target",
    "dataset",
    "status",
    "returncode",
    "input_dcp",
    "codex_input_dcp",
    "output_dcp",
    "run_dir",
    "wall_time_s",
    "agent_elapsed_s",
    "llm_model",
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "cost_usd",
    "cost_upper_bound_usd",
    "usage_source",
    "error",
]


def _read_rows(manifest_path: Path, assigned_host: str | None = None) -> list[dict[str, str]]:
    with manifest_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if assigned_host:
        rows = [row for row in rows if row.get("assigned_host") == assigned_host]
    return rows


def _line_mentions_excluded_design(line: str, excluded_design_ids: set[str]) -> bool:
    if not excluded_design_ids:
        return False
    for token in re.split(r"[^A-Za-z0-9_.\\/-]+", line):
        if normalize_knowledge_design_id(token) in excluded_design_ids:
            return True
    return False


def _sanitize_inventory_text(inventory_text: str, excluded_design_ids: set[str]) -> str:
    if not excluded_design_ids:
        return inventory_text
    kept = [
        line for line in inventory_text.splitlines()
        if not _line_mentions_excluded_design(line, excluded_design_ids)
    ]
    kept.extend(
        [
            "",
            "Evaluation-specific DCP knowledge marked excluded in the manifest was removed.",
        ]
    )
    return "\n".join(kept)


def _write_prompt(
    row: dict[str, str],
    run_dir: Path,
    codex_input_dcp: Path,
    codex_output_dcp: Path,
    inventory_text: str,
    time_limit_s: int | float,
) -> Path:
    prompt = f"""# Codex DCP Harness Task

You are being dispatched as a non-interactive benchmark subagent. If your runtime
environment has optional startup/skill workflows, skip them; the complete policy
for this task is in this prompt.

Input DCP: `{codex_input_dcp}`
Output DCP: `{codex_output_dcp}`
Time limit: `{time_limit_s}` seconds
Offline ceiling MHz: `{row.get('ceiling_mhz', '')}`
95 percent target MHz: `{row.get('target_95_mhz', '')}`

Write the optimized checkpoint only to the output path above.
First, immediately create a safe fallback output by copying the input DCP to the
output path. Replace it later only if you produce a better valid checkpoint.
Treat the time limit as a hard wall-clock budget that includes final verification and your final response.
Plan to stop tool work by `{max(60, int(float(time_limit_s) - 300))}` seconds and
use the remaining time only to ensure the output DCP exists and to finish your
final response. Stop launching broad or long-running Vivado/RapidWright actions
near the end of the budget; if time is low, keep the best currently valid
checkpoint at the output path and finish.
When using MCP tools, do not set tiny timeouts for checkpoint open/write/route
operations; use realistic timeouts so the MCP transport does not close mid-write.
Do not use remembered benchmark, DCP, hierarchy, net, path, or fixed coordinate names.
Use the FDAgents inventory and decision policy below as the only optimization policy.
Do not launch or delegate to FDAgents, dcp_optimizer.py, batch_fdagents.py, or run_fdagents_manifest.py.
Do not read, grep, import, execute, or inspect files under `FDAgents/`; the inventory and decision policy below are the complete FDAgents-derived context available to you.
Do not launch VivadoMCP or RapidWrightMCP server scripts through shell commands; use the Codex-native MCP tools that are already configured.

## FDAgents Inventory

{inventory_text}

## FDAgents Decision Policy

{build_decision_policy_prompt()}

## Required Final Response

Report the output DCP path, important timing observations, and whether the output should be verified.
"""
    path = run_dir / "prompt.md"
    path.write_text(prompt)
    return path


def _base_row(row: dict[str, str], run_dir: Path) -> dict[str, object]:
    codex_work_dir = _codex_work_dir(row, run_dir.parent)
    return {
        "target": row.get("target", ""),
        "dataset": row.get("dataset", ""),
        "status": "",
        "returncode": "",
        "input_dcp": row.get("input_dcp", ""),
        "codex_input_dcp": str((codex_work_dir / "input.dcp").resolve()),
        "output_dcp": str((codex_work_dir / "output.dcp").resolve()),
        "run_dir": str(run_dir),
        "wall_time_s": "",
        "agent_elapsed_s": UNKNOWN,
        "llm_model": UNKNOWN,
        "input_tokens": UNKNOWN,
        "cached_input_tokens": UNKNOWN,
        "uncached_input_tokens": UNKNOWN,
        "output_tokens": UNKNOWN,
        "reasoning_output_tokens": UNKNOWN,
        "total_tokens": UNKNOWN,
        "cost_usd": UNKNOWN,
        "cost_upper_bound_usd": UNKNOWN,
        "usage_source": "unparsed",
        "error": "",
    }


def _run_one(
    row: dict[str, str],
    *,
    run_root: Path,
    inventory_text: str,
    codex_cmd: str,
    codex_model: str | None,
    time_limit_s: int | float,
    timeout_grace_s: int | float,
    dry_run: bool,
    project_root: Path,
    require_usage: bool = False,
) -> dict[str, object]:
    run_dir = run_root / row["target"]
    run_dir.mkdir(parents=True, exist_ok=True)
    codex_work_dir = _codex_work_dir(row, run_root)
    codex_work_dir.mkdir(parents=True, exist_ok=True)
    codex_input_dcp = codex_work_dir / "input.dcp"
    codex_output_dcp = codex_work_dir / "output.dcp"
    prompt_path = _write_prompt(
        row,
        run_dir,
        codex_input_dcp.resolve(),
        codex_output_dcp.resolve(),
        inventory_text,
        time_limit_s,
    )
    result = _base_row(row, run_dir)
    if dry_run:
        result["status"] = "DRY_RUN"
        result["wall_time_s"] = "0.000"
        return result

    _copy_input_dcp_for_codex(row, codex_input_dcp)
    stdout_path = run_dir / "codex.stdout.jsonl"
    stderr_path = run_dir / "codex.stderr.log"
    last_message_path = run_dir / "last_message.md"
    prompt_text = prompt_path.read_text()
    cmd = shlex.split(codex_cmd) + ["exec"]
    cmd.extend(_codex_config_overrides_from_env())
    if codex_model:
        cmd.extend(["--model", codex_model])
    cmd.extend(
        [
            "--json",
            "-C",
            str(project_root),
            "--sandbox",
            "danger-full-access",
            "--skip-git-repo-check",
            "-o",
            str(last_message_path),
            "-",
        ]
    )
    start = time.time()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(run_dir),
            start_new_session=True,
            bufsize=1,
        )

        stdout_thread = Thread(
            target=_tee_stream,
            args=(proc.stdout, stdout_path, stdout_chunks),
            daemon=True,
        )
        stderr_thread = Thread(
            target=_tee_stream,
            args=(proc.stderr, stderr_path, stderr_chunks),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        soft_deadline = time.time() + float(time_limit_s)
        hard_deadline = soft_deadline + float(timeout_grace_s)
        shutdown_requested = False
        while True:
            if proc.poll() is not None:
                break
            contamination = _find_forbidden_process_referencing(run_dir)
            if contamination:
                _terminate_codex_run(proc, run_dir)
                result["status"] = "CONTAMINATED"
                result["returncode"] = -2
                result["error"] = contamination[:500]
                break
            now = time.time()
            if not shutdown_requested and now >= soft_deadline:
                result["status"] = "TIMEOUT"
                result["returncode"] = -1
                if float(timeout_grace_s) > 0:
                    _stop_codex_tool_processes(proc, run_dir)
                    shutdown_requested = True
                    continue
                _terminate_codex_run(proc, run_dir)
                break
            if shutdown_requested:
                _stop_codex_tool_processes(proc, run_dir)
            remaining = (hard_deadline if shutdown_requested else soft_deadline) - now
            if remaining <= 0:
                _terminate_codex_run(proc, run_dir)
                result["status"] = "TIMEOUT"
                result["returncode"] = -1
                break
            time.sleep(min(1.0, remaining))
        if result["status"] == "TIMEOUT":
            _cleanup_codex_run_processes(run_dir)
        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        command_contamination = _find_forbidden_codex_command(stdout)
        if command_contamination:
            result["status"] = "CONTAMINATED"
            result["returncode"] = -2
            result["error"] = command_contamination[:500]
        if result["status"] not in {"TIMEOUT", "CONTAMINATED"}:
            result["returncode"] = proc.returncode
            result["status"] = "OK" if proc.returncode == 0 else "FAILED"
        usage = parse_codex_usage(stdout or "", stderr or "")
        _apply_codex_model_and_pricing(usage, codex_model)
        usage_row = usage.as_row()
        result.update(
            {
                "agent_elapsed_s": usage_row["agent_elapsed_s"],
                "llm_model": usage_row["model"],
                "input_tokens": usage_row["input_tokens"],
                "cached_input_tokens": usage_row["cached_input_tokens"],
                "uncached_input_tokens": usage_row["uncached_input_tokens"],
                "output_tokens": usage_row["output_tokens"],
                "reasoning_output_tokens": usage_row["reasoning_output_tokens"],
                "total_tokens": usage_row["total_tokens"],
                "cost_usd": usage_row["cost_usd"],
                "cost_upper_bound_usd": usage_row["cost_upper_bound_usd"],
                "usage_source": usage_row["usage_source"],
            }
        )
        if require_usage and _missing_required_usage(result):
            result["status"] = "USAGE_MISSING"
            result["error"] = (
                "token/cost usage was not visible in Codex JSON output; "
                "stopping to avoid fabricated accounting"
            )
    except Exception as exc:
        result["status"] = "EXCEPTION"
        result["returncode"] = -1
        result["error"] = str(exc)[:500]
    result["wall_time_s"] = f"{time.time() - start:.3f}"
    return result


def _codex_config_overrides_from_env() -> list[str]:
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base_url:
        return []
    provider = os.environ.get("CODEX_MODEL_PROVIDER", DEFAULT_CODEX_PROVIDER).strip() or DEFAULT_CODEX_PROVIDER
    wire_api = os.environ.get("CODEX_WIRE_API", "responses").strip() or "responses"
    return [
        "-c",
        f"model_provider={json.dumps(provider)}",
        "-c",
        f"model_providers.{provider}.name={json.dumps(provider)}",
        "-c",
        f"model_providers.{provider}.base_url={json.dumps(base_url)}",
        "-c",
        f"model_providers.{provider}.wire_api={json.dumps(wire_api)}",
    ]


def _write_row(writer: csv.DictWriter, f, row: dict[str, object]) -> None:
    writer.writerow(row)
    f.flush()
    os.fsync(f.fileno())


def _missing_required_usage(row: dict[str, object]) -> bool:
    return any(
        row.get(field) in {"", UNKNOWN, None}
        for field in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")
    )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _apply_codex_model_and_pricing(usage, codex_model: str | None) -> None:
    if usage.model == UNKNOWN and codex_model:
        usage.model = codex_model
    if not isinstance(usage.model, str):
        return
    pricing = _lookup_pricing(usage.model)
    if pricing is None:
        return
    converted = {
        "input_per_mtok": pricing["input"] * 1000.0,
        "output_per_mtok": pricing["output"] * 1000.0,
    }
    if "cached_input" in pricing:
        converted["cached_input_per_mtok"] = pricing["cached_input"] * 1000.0
    usage.apply_pricing(
        {usage.model: converted}
    )


def _codex_work_dir(row: dict[str, str], run_root: Path) -> Path:
    target = row.get("target", "")
    input_dcp = row.get("input_dcp", "")
    digest = hashlib.sha256(f"{target}\0{input_dcp}".encode()).hexdigest()[:16]
    return run_root / "codex_cases" / f"case_{digest}"


def _copy_input_dcp_for_codex(row: dict[str, str], codex_input_dcp: Path) -> None:
    source = Path(row.get("input_dcp", "")).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"input DCP not found: {source}")
    codex_input_dcp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, codex_input_dcp)


def _tee_stream(stream, path: Path, chunks: list[str]) -> None:
    if stream is None:
        path.write_text("")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with stream, path.open("w") as f:
        for line in stream:
            chunks.append(line)
            f.write(line)
            f.flush()


def _kill_processes_referencing(path: Path, sig: int) -> None:
    needle = str(path)
    current_pid = os.getpid()
    try:
        current_pgid = os.getpgid(current_pid)
    except ProcessLookupError:
        current_pgid = -1
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,ppid=,pgid=,args="], capture_output=True, text=True, check=False)
    except Exception:
        return
    parent_by_pid: dict[int, int] = {}
    pgid_by_pid: dict[int, int] = {}
    matching_pids: set[int] = set()
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=3)
        if len(parts) < 4:
            continue
        pid_text, ppid_text, pgid_text, command = parts
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
            pgid = int(pgid_text)
        except ValueError:
            continue
        parent_by_pid[pid] = ppid
        pgid_by_pid[pid] = pgid
        if pid != current_pid and needle in command:
            matching_pids.add(pid)

    to_kill = set(matching_pids)
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent_by_pid.items():
            if pid != current_pid and ppid in to_kill and pid not in to_kill:
                to_kill.add(pid)
                changed = True

    for pgid in sorted({pgid_by_pid.get(pid) for pid in to_kill if pgid_by_pid.get(pid) is not None}):
        if pgid == current_pgid:
            continue
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
    for pid in sorted(to_kill):
        if pid == current_pid:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _cleanup_codex_run_processes(run_dir: Path) -> None:
    _kill_processes_referencing(run_dir, signal.SIGTERM)
    time.sleep(0.2)
    _kill_processes_referencing(run_dir, signal.SIGKILL)


def _terminate_codex_run(proc: subprocess.Popen, run_dir: Path) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    _kill_processes_referencing(run_dir, signal.SIGTERM)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _kill_processes_referencing(run_dir, signal.SIGKILL)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    _cleanup_codex_run_processes(run_dir)


def _stop_codex_tool_processes(proc: subprocess.Popen, run_dir: Path) -> None:
    _kill_codex_tool_processes(proc.pid, run_dir, signal.SIGTERM)
    time.sleep(0.2)
    _kill_codex_tool_processes(proc.pid, run_dir, signal.SIGKILL)


def _kill_codex_tool_processes(root_pid: int, run_dir: Path, sig: int) -> None:
    descendants = _process_descendants(root_pid)
    if not descendants:
        return
    current_pid = os.getpid()
    try:
        current_pgid = os.getpgid(current_pid)
    except ProcessLookupError:
        current_pgid = -1
    try:
        root_pgid = os.getpgid(root_pid)
    except ProcessLookupError:
        root_pgid = -1
    tool_pids: set[int] = set()
    for pid, info in descendants.items():
        command = str(info["command"])
        if pid == current_pid or _is_codex_process(command):
            continue
        if _is_tool_process(command) or str(run_dir) in command:
            tool_pids.add(pid)

    changed = True
    while changed:
        changed = False
        for pid, info in descendants.items():
            if info["ppid"] in tool_pids and pid not in tool_pids:
                tool_pids.add(pid)
                changed = True

    for pgid in sorted({int(descendants[pid]["pgid"]) for pid in tool_pids if pid in descendants}):
        if pgid in {current_pgid, root_pgid}:
            continue
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
    for pid in sorted(tool_pids):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _process_descendants(root_pid: int) -> dict[int, dict[str, int | str]]:
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,ppid=,pgid=,args="], capture_output=True, text=True, check=False)
    except Exception:
        return {}
    processes: dict[int, dict[str, int | str]] = {}
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(maxsplit=3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
        except ValueError:
            continue
        processes[pid] = {"ppid": ppid, "pgid": pgid, "command": parts[3]}

    descendants: dict[int, dict[str, int | str]] = {}
    changed = True
    while changed:
        changed = False
        for pid, info in processes.items():
            if pid == root_pid or pid in descendants:
                continue
            if info["ppid"] == root_pid or info["ppid"] in descendants:
                descendants[pid] = info
                changed = True
    return descendants


def _is_codex_process(command: str) -> bool:
    return "codex exec" in command or "/codex.js exec" in command


def _is_tool_process(command: str) -> bool:
    tool_markers = (
        "VivadoMCP/vivado_mcp_server.py",
        "RapidWrightMCP/server.py",
        "/Vivado/bin/vivado",
        "/Vivado/bin/loader",
        "/Vivado/bin/unwrapped/",
        "vivado -mode tcl",
        "vivado -mode batch",
    )
    return any(marker in command for marker in tool_markers)


def _find_forbidden_process_referencing(path: Path) -> str | None:
    needle = str(path)
    current_pid = os.getpid()
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
    except Exception:
        return None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid or needle not in command:
            continue
        if _command_contains_forbidden_delegation(command):
            return f"Forbidden Codex delegation command detected: {command}"
    return None


def _command_contains_forbidden_delegation(command: str) -> bool:
    if _is_read_only_process_inspection(command):
        return False
    for pattern in FORBIDDEN_CODEX_DELEGATION_PATTERNS:
        start = 0
        while True:
            index = command.find(pattern, start)
            if index < 0:
                break
            if pattern == "FDAgents/" and index > 0 and command[index - 1] == "!":
                start = index + len(pattern)
                continue
            return True
    return False


def _is_read_only_process_inspection(command: str) -> bool:
    lowered = command.lower()
    if "ps " not in lowered or (" rg " not in lowered and " grep " not in lowered):
        return False
    launch_markers = (
        "python vivadomcp/",
        "python rapidwrightmcp/",
        "python -m fdagents.agent",
        "run_fdagents_manifest.py",
        "batch_fdagents.py",
        "dcp_optimizer.py",
    )
    return not any(marker in lowered for marker in launch_markers)


def _find_forbidden_codex_command(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        if _command_contains_forbidden_delegation(command):
            return f"Forbidden Codex delegation command detected: {command}"
    return None


def _preflight_codex_native_mcp(codex_cmd: str, required_servers: tuple[str, ...]) -> None:
    proc = subprocess.run(
        shlex.split(codex_cmd) + ["mcp", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"codex mcp list failed: {(proc.stderr or proc.stdout).strip()}")
    configured = _parse_codex_mcp_list_names(proc.stdout)
    missing = [name for name in required_servers if name not in configured]
    if missing:
        raise RuntimeError(
            "missing required Codex MCP servers: "
            + ", ".join(missing)
            + f"; configured: {', '.join(sorted(configured)) or 'none'}"
        )


def _parse_codex_mcp_list_names(output: str) -> set[str]:
    names: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        first = stripped.split(maxsplit=1)[0]
        if first:
            names.add(first)
    return names


def run_codex_manifest(
    *,
    manifest_path: Path,
    run_root: Path,
    inventory_path: Path,
    codex_cmd: str,
    time_limit_s: int | float,
    jobs: int,
    dry_run: bool,
    project_root: Path,
    codex_model: str | None = None,
    timeout_grace_s: int | float = 900.0,
    preflight: bool = True,
    require_native_mcp: bool = False,
    required_mcp_servers: tuple[str, ...] = REQUIRED_NATIVE_MCP_SERVERS,
    assigned_host: str | None = None,
    knowledge_exclude_manifest: Path | None = None,
    require_usage: bool = False,
) -> Path:
    if require_usage and jobs > 1:
        raise ValueError("--require-usage requires --jobs 1 so the run can stop at the first missing usage record")
    if preflight and not dry_run:
        subprocess.run(shlex.split(codex_cmd) + ["exec", "--help"], check=True, capture_output=True, text=True)
        if require_native_mcp:
            _preflight_codex_native_mcp(codex_cmd, required_mcp_servers)

    run_root = run_root.resolve()
    rows = _read_rows(manifest_path, assigned_host=assigned_host)
    run_root.mkdir(parents=True, exist_ok=True)
    exclude_manifest = knowledge_exclude_manifest or manifest_path
    excluded_design_ids = excluded_knowledge_design_ids_from_manifest(exclude_manifest)
    inventory_text = _sanitize_inventory_text(inventory_path.read_text(), excluded_design_ids)
    results_path = run_root / "results.csv"
    lock = Lock()
    with results_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        f.flush()
        os.fsync(f.fileno())
        if jobs <= 1:
            for row in rows:
                row_result = _run_one(
                    row,
                    run_root=run_root,
                    inventory_text=inventory_text,
                    codex_cmd=codex_cmd,
                    codex_model=codex_model,
                    time_limit_s=time_limit_s,
                    timeout_grace_s=timeout_grace_s,
                    dry_run=dry_run,
                    project_root=project_root,
                    require_usage=require_usage,
                )
                _write_row(writer, f, row_result)
                if row_result["status"] == "CONTAMINATED":
                    break
                if require_usage and row_result["status"] == "USAGE_MISSING":
                    break
        else:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = [
                    pool.submit(
                        _run_one,
                        row,
                        run_root=run_root,
                        inventory_text=inventory_text,
                        codex_cmd=codex_cmd,
                        codex_model=codex_model,
                        time_limit_s=time_limit_s,
                        timeout_grace_s=timeout_grace_s,
                        dry_run=dry_run,
                        project_root=project_root,
                        require_usage=require_usage,
                    )
                    for row in rows
                ]
                for future in as_completed(futures):
                    with lock:
                        _write_row(writer, f, future.result())
    return results_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--time-limit", type=float, default=3600.0)
    parser.add_argument("--timeout-grace", type=float, default=900.0)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--codex-cmd", default="codex")
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--assigned-host", default=None)
    parser.add_argument("--knowledge-exclude-manifest", type=Path, default=None)
    parser.add_argument("--require-usage", action="store_true")
    parser.add_argument("--require-native-mcp", action="store_true")
    parser.add_argument(
        "--required-mcp-server",
        action="append",
        default=None,
        help="Required Codex-native MCP server name. Defaults to vivado and rapidwright.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    run_codex_manifest(
        manifest_path=args.manifest,
        run_root=args.run_root,
        inventory_path=args.inventory,
        codex_cmd=args.codex_cmd,
        codex_model=args.codex_model,
        time_limit_s=args.time_limit,
        timeout_grace_s=args.timeout_grace,
        jobs=args.jobs,
        dry_run=args.dry_run,
        project_root=Path.cwd(),
        require_native_mcp=args.require_native_mcp,
        required_mcp_servers=tuple(args.required_mcp_server or REQUIRED_NATIVE_MCP_SERVERS),
        assigned_host=args.assigned_host,
        knowledge_exclude_manifest=args.knowledge_exclude_manifest,
        require_usage=args.require_usage,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
