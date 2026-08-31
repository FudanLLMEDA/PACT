"""
MCP client for FDAgents — manages RapidWright and Vivado MCP server sessions.

Extracted from DCPOptimizerBase in dcp_optimizer.py, with LLM code removed.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from typing import Optional

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .artifacts import sha256_file, validate_owned_candidate_path
from .skills.base import parse_timing_summary_static

logger = logging.getLogger(__name__)


_CLOSED_MCP_TRANSPORT_ERRORS = (
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
)


def _is_closed_mcp_transport_error(exc: BaseException) -> bool:
    """Return true when every exception leaf only reports a closed transport.

    AnyIO may wrap a stdio reader's ``BrokenResourceError`` in one or more
    ``BaseExceptionGroup`` instances while an ``AsyncExitStack`` is closing.
    At final cleanup that means the peer has already closed the resource, not
    that candidate export or validation failed.  Do not use message matching,
    and do not suppress a mixed group containing any other failure.
    """
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        return all(_is_closed_mcp_transport_error(child) for child in nested)
    return isinstance(exc, _CLOSED_MCP_TRANSPORT_ERRORS)


def _prepend_pythonpath(env: dict[str, str], directory: Path) -> dict[str, str]:
    """Return an env where an exact project root is importable by MCP children.

    MCP servers run with the per-run artifact directory as their cwd, so the
    parent Agent's import-time ``sys.path`` is not inherited.  Bind the child
    explicitly to the checkout that contains the server script instead of
    depending on the launch cwd or a machine-specific site-package install.
    """
    result = dict(env)
    root = str(Path(directory).expanduser().resolve())
    existing = [
        item for item in result.get("PYTHONPATH", "").split(os.pathsep)
        if item and item != root
    ]
    result["PYTHONPATH"] = os.pathsep.join([root, *existing])
    return result


def _usable_rapidwright_checkout(path: Path) -> bool:
    """Return true only for a checkout that can supply Java classes.

    Git worktrees may contain an uninitialized, empty ``RapidWright`` gitlink.
    Treating that directory as a runtime shadows the pip/shared classpath with
    ``bin:jars/*`` entries that contain no RapidWright classes.
    """
    root = Path(path).expanduser()
    return (
        root.is_dir()
        and (root / "jars").is_dir()
        and any((root / "jars").glob("*.jar"))
    )


# These tools never consult the current design. They still need RapidWright
# initialized, but loading a candidate checkpoint would be unnecessary.
RAPIDWRIGHT_DEVICE_ONLY_PROBE_TOOLS = frozenset({
    "get_supported_devices",
    "get_device_info",
})

_RAPIDWRIGHT_IDENTITY_PRESERVING_TOOLS = frozenset({
    *RAPIDWRIGHT_DEVICE_ONLY_PROBE_TOOLS,
    "report_approx_timing",
    "get_design_info",
    "search_cells",
    "get_tile_info",
    "search_sites",
    "inspect_lut_pin_swap_candidates",
    "analyze_critical_path_spread",
    "analyze_fabric_for_pblock",
    "convert_fabric_region_to_pblock",
    "analyze_net_detour",
    "analyze_route_connections",
    "operator_mining",
    "recover_recurrence_contract",
    "recover_recurrence_family_contract",
    "recover_sequential_operator_contract",
    "recover_sequential_operator_replicas",
    "prove_control_pin_equivalence_groups",
    "recover_registered_product_sum_contract",
    "propose_recurrence_placement_candidates",
    "find_proven_equivalent_sources",
    "write_checkpoint",
})


class MCPToolError(RuntimeError):
    """Raised when an MCP tool returns an error response (instead of throwing)."""

    def __init__(self, server: str, tool: str, message: str, raw: str = ""):
        self.server = server
        self.tool = tool
        self.raw = raw
        self.message = message
        super().__init__(f"[{server}] {tool}: {message}")

    @property
    def structural_non_applicability(self) -> bool:
        """Whether the tool refused because the structure does not qualify.

        A structural refusal is a normal analysis outcome: the current seed
        simply does not carry the shape this mechanism needs. Reporting it as a
        tool error pollutes feedback and pushes the planner away from a whole
        mechanism family that may apply to a later descendant.
        """
        return is_structural_non_applicability(self.message)


# A refusal states that the required structure is absent. A failure carries a
# backend diagnostic: the tool tried to act and the attempt broke.
_NON_APPLICABILITY_MARKERS = (
    "no consumer boundary matches",
    "has no hierarchy owner",
    "anonymous_diagnostics=",
    "not_applicable",
)
_BACKEND_FAILURE_MARKERS = (
    "vivado_error=",
    "error: [",
    "traceback",
)


def is_structural_non_applicability(message: object) -> bool:
    """Classify a tool refusal as `not_applicable` rather than an error."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _BACKEND_FAILURE_MARKERS):
        return False
    if text.startswith("no "):
        return True
    return any(marker in text for marker in _NON_APPLICABILITY_MARKERS)


def _detect_tool_error(server: str, tool: str, response: str) -> None:
    """
    Inspect a tool response for error markers and raise MCPToolError if found.

    Triggers on:
      - top-level JSON object with an "error" key
      - leading Vivado/Tcl/server error lines

    Does NOT trigger on empty output — some tools legitimately return nothing.
    """
    if not response:
        return

    stripped = response.lstrip()

    # JSON error object: {"error": "..."} or {"status": "error", "message": "..."}
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and ("error" in obj or obj.get("status") == "error"):
            err = obj.get("error", obj.get("message", obj))
            msg = err if isinstance(err, str) else json.dumps(err)
            raise MCPToolError(server, tool, msg, raw=response)

    # Vivado-style errors, Tcl interpreter failures, and server wrappers.
    for line in response.splitlines():
        line = line.strip()
        lowered = line.lower()
        if (
            line.startswith(("ERROR:", "ERROR [", "Error:", "Error executing tool:"))
            or lowered.startswith("invalid command name")
        ):
            raise MCPToolError(server, tool, line[:200], raw=response)


def _extract_tool_response(server: str, tool: str, result: object) -> str:
    """Normalize an MCP 1.28.1 tool result and fail closed on protocol errors.

    Low-level MCP input/output validation failures are returned as
    ``CallToolResult(isError=True)`` rather than raised by ``call_tool``.  Do
    not let those responses fall through to downstream JSON parsers.  Prefer
    ordinary non-empty text blocks for compatibility with the repository's
    existing servers, then accept protocol-native structured content when no
    text was supplied.
    """
    content = getattr(result, "content", None)
    if not isinstance(content, (list, tuple)):
        raise MCPToolError(
            server,
            tool,
            "MCP returned a malformed result without a content list",
        )

    text_blocks = []
    saw_text_block = False
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            saw_text_block = True
            if text:
                text_blocks.append(text)
    response = "\n".join(text_blocks)

    if not response:
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        if structured is not None:
            if not isinstance(structured, dict):
                raise MCPToolError(
                    server,
                    tool,
                    "MCP returned non-object structured content",
                )
            try:
                response = json.dumps(structured, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise MCPToolError(
                    server,
                    tool,
                    "MCP returned non-serializable structured content",
                ) from exc

    if getattr(result, "isError", False):
        message = response.strip() or "MCP tool reported an error without content"
        raise MCPToolError(server, tool, message[:1000], raw=response)

    if response:
        _detect_tool_error(server, tool, response)
        return response
    if saw_text_block:
        # Some Tcl commands legitimately return one empty TextContent block.
        return ""
    if content:
        raise MCPToolError(
            server,
            tool,
            "MCP returned no usable text or structured content",
        )
    return "(no output)"


class MCPClient:
    """Manages MCP server sessions for RapidWright and Vivado."""

    def __init__(self, run_dir: Path, debug: bool = False):
        self.run_dir = Path(run_dir).expanduser().resolve()
        self._server_run_dir = self.run_dir
        self.debug = debug
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self._rw_exit_stack = AsyncExitStack()
        self._vivado_exit_stack = AsyncExitStack()
        self.rapidwright_session: Optional[ClientSession] = None
        self.vivado_session: Optional[ClientSession] = None
        self._vivado_server_config: Optional[dict] = None

        self.target_clock: Optional[str] = None

        self._rw_log_file = None
        self._v_log_file = None
        self._run_dir_scope_active = False
        self._rw_initialized = False
        self._rw_design_identity: Optional[tuple[str, str]] = None
        self._rw_probe_lock = asyncio.Lock()
        self._vivado_lock = asyncio.Lock()

    @contextmanager
    def scoped_run_dir(self, path: Path):
        """Temporarily use an action scratch directory.

        This synchronous scope mutates shared client state and is intentionally
        sequential and non-concurrent. Nested or overlapping scopes are rejected.
        """
        if self._run_dir_scope_active:
            raise RuntimeError("scoped_run_dir is sequential and non-concurrent")

        scoped = Path(path).expanduser().resolve()
        scoped.mkdir(parents=True, exist_ok=True)
        previous = self.run_dir
        self._run_dir_scope_active = True
        self.run_dir = scoped
        try:
            yield scoped
        finally:
            self.run_dir = previous
            self._run_dir_scope_active = False

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start_servers(self) -> None:
        """Start and connect to both MCP servers."""
        script_dir = Path(__file__).parent.parent.resolve()

        rapidwright_log = self._server_run_dir / "rapidwright.log"
        rapidwright_mcp_log = self._server_run_dir / "rapidwright-mcp.log"
        vivado_log = self._server_run_dir / "vivado.log"
        vivado_journal = self._server_run_dir / "vivado.jou"
        vivado_mcp_log = self._server_run_dir / "vivado-mcp.log"

        if self.debug:
            self._rw_log_file = None
            self._v_log_file = None
            logger.info("Debug mode: MCP server output shown in console")
        else:
            self._rw_log_file = open(rapidwright_mcp_log, "w")
            self._v_log_file = open(vivado_mcp_log, "w")
            logger.info(
                "Logs in %s: %s, %s",
                self._server_run_dir,
                rapidwright_log.name,
                vivado_log.name,
            )

        # RapidWright MCP server
        rw_args = [str(script_dir / "RapidWrightMCP" / "server.py")]
        if not self.debug:
            rw_args.extend([
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rapidwright_mcp_log),
            ])

        env = _prepend_pythonpath({**os.environ}, script_dir)
        rw_submodule = script_dir / "RapidWright"
        if _usable_rapidwright_checkout(rw_submodule) and "RAPIDWRIGHT_PATH" not in env:
            env["RAPIDWRIGHT_PATH"] = str(rw_submodule)
            env["CLASSPATH"] = f"{rw_submodule}/bin:{rw_submodule}/jars/*"

        rw_config = {
            "command": sys.executable,
            "args": rw_args,
            "cwd": str(self._server_run_dir),
            "env": env,
        }

        # Vivado MCP server
        v_args = [str(script_dir / "VivadoMCP" / "vivado_mcp_server.py")]
        if not self.debug:
            v_args.extend([
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal),
            ])

        self._vivado_server_config = {
            "command": sys.executable,
            "args": v_args,
            "cwd": str(self._server_run_dir),
            "env": {**os.environ},
        }

        # Start RapidWright
        logger.info("Starting RapidWright MCP server...")
        t0 = time.time()
        rw_params = StdioServerParameters(**rw_config)
        rw_transport = await self._rw_exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=self._rw_log_file)
        )
        rw_read, rw_write = rw_transport
        self.rapidwright_session = await self._rw_exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        await self.rapidwright_session.initialize()
        logger.info(f"RapidWright MCP started in {time.time()-t0:.2f}s")

        # Start Vivado
        await self._start_vivado_server()

        logger.info("Both MCP servers connected")

    async def cleanup(self) -> None:
        """Release all resources."""
        cleanup_errors = []
        for label, stack in (
            ("Vivado", self._vivado_exit_stack),
            ("RapidWright", self._rw_exit_stack),
        ):
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001 - cleanup remains best effort
                if _is_closed_mcp_transport_error(exc):
                    logger.info(
                        "%s MCP transport was already closed during cleanup",
                        label,
                    )
                else:
                    cleanup_errors.append((label, exc))
                    logger.error("Failed to close %s MCP resources: %s", label, exc)
        self.vivado_session = None
        self.rapidwright_session = None
        self._rw_initialized = False
        self._rw_design_identity = None
        for label, log_file in (
            ("RapidWright", self._rw_log_file),
            ("Vivado", self._v_log_file),
        ):
            if log_file:
                try:
                    log_file.close()
                except Exception as exc:  # noqa: BLE001 - cleanup remains best effort
                    cleanup_errors.append((f"{label} log", exc))
                    logger.error("Failed to close %s MCP log: %s", label, exc)
        self._rw_log_file = None
        self._v_log_file = None
        if cleanup_errors:
            logger.error("MCP cleanup completed with %d error(s)", len(cleanup_errors))
        logger.info(f"Run directory preserved at: {self.run_dir}")
        if cleanup_errors:
            labels = ", ".join(label for label, _ in cleanup_errors)
            raise RuntimeError(f"MCP cleanup failed for: {labels}") from cleanup_errors[0][1]

    async def _start_vivado_server(self) -> None:
        """Create a fresh Vivado MCP transport and session."""
        if self._vivado_server_config is None:
            raise RuntimeError("Vivado MCP server configuration is unavailable")
        logger.info("Starting Vivado MCP server...")
        t0 = time.time()
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(**self._vivado_server_config)
            transport = await stack.enter_async_context(
                stdio_client(params, errlog=self._v_log_file)
            )
            read_stream, write_stream = transport
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        self._vivado_exit_stack = stack
        self.vivado_session = session
        logger.info("Vivado MCP started in %.2fs", time.time() - t0)

    async def _reconnect_vivado_server(self) -> None:
        """Replace a failed Vivado MCP transport without touching RapidWright."""
        old_stack = self._vivado_exit_stack
        self.vivado_session = None
        self._vivado_exit_stack = AsyncExitStack()
        try:
            await old_stack.aclose()
        except Exception as exc:  # noqa: BLE001 - broken transports can fail close
            logger.warning("Failed to cleanly close Vivado MCP transport: %s", exc)
        await self._start_vivado_server()

    async def restart_vivado(
        self,
        *,
        restore_checkpoint: Optional[Path] = None,
        expected_sha256: Optional[str] = None,
        timeout: float = 600.0,
        restart_request_timeout: float = 60.0,
    ) -> dict:
        """Restart Vivado and optionally restore one prevalidated checkpoint.

        The failed command is never replayed. If the MCP transport itself is
        unavailable, only the Vivado MCP server is recreated; RapidWright stays
        connected.
        """
        checkpoint: Optional[Path] = None
        if restore_checkpoint is not None:
            checkpoint = Path(restore_checkpoint).expanduser().resolve()
            if not checkpoint.is_file():
                raise FileNotFoundError(f"Vivado restore checkpoint missing: {checkpoint}")
            if expected_sha256 is not None:
                expected = str(expected_sha256).lower()
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError("Vivado restore checkpoint SHA256 is invalid")
                if sha256_file(checkpoint) != expected:
                    raise ValueError("Vivado restore checkpoint SHA256 mismatch")
        elif expected_sha256 is not None:
            raise ValueError("expected_sha256 requires restore_checkpoint")
        if timeout <= 0.0 or restart_request_timeout <= 0.0:
            raise ValueError("Vivado restart timeouts must be positive")

        async with self._vivado_lock:
            self.target_clock = None
            reconnected = False
            try:
                await self._call_vivado_unlocked(
                    "restart_vivado",
                    {},
                    timeout=min(timeout, restart_request_timeout),
                )
            except MCPToolError:
                # The server was reachable and returned a deterministic restart
                # failure; replacing the transport would only hide that error.
                raise
            except Exception as exc:  # noqa: BLE001 - transport may be gone
                logger.warning(
                    "Vivado restart tool was unreachable; recreating MCP transport: %s",
                    exc,
                )
                await self._reconnect_vivado_server()
                reconnected = True

            if checkpoint is not None:
                await self._call_vivado_unlocked(
                    "open_checkpoint",
                    {"dcp_path": str(checkpoint)},
                    timeout=timeout,
                )
            return {
                "status": "restarted",
                "mcp_reconnected": reconnected,
                "checkpoint_restored": checkpoint is not None,
                "checkpoint_path": str(checkpoint) if checkpoint is not None else None,
            }

    # ------------------------------------------------------------------
    # Low-level tool calls
    # ------------------------------------------------------------------

    async def call_rw(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Serialize one ordinary RapidWright MCP tool call."""
        async with self._rw_probe_lock:
            return await self._call_rw_unlocked(tool_name, arguments, timeout)

    async def _call_rw_unlocked(
        self, tool_name: str, arguments: dict, timeout: float = 300.0
    ) -> str:
        """Call RapidWright while the caller owns ``_rw_probe_lock``."""
        if (
            tool_name != "initialize_rapidwright"
            and tool_name not in _RAPIDWRIGHT_IDENTITY_PRESERVING_TOOLS
        ):
            # A direct checkpoint load or mutating tool makes any prior probe
            # binding untrustworthy. ensure_rapidwright_probe will reload it.
            self._rw_design_identity = None
        logger.debug(f"[RW] {tool_name} args={str(arguments)[:200]}")
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                self.rapidwright_session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
            elapsed = time.time() - t0
            logger.info(f"[RW] {tool_name} completed in {elapsed:.2f}s")
            return _extract_tool_response("RW", tool_name, result)
        except asyncio.TimeoutError:
            logger.error(f"[RW] {tool_name} TIMED OUT after {time.time()-t0:.2f}s")
            raise
        except MCPToolError as e:
            if e.structural_non_applicability:
                # `not_applicable`, not `infrastructure_error`.
                logger.info(f"[RW] {tool_name} not applicable: {e}")
            else:
                logger.error(f"[RW] {tool_name} returned error: {e}")
            raise
        except Exception as e:
            logger.error(f"[RW] {tool_name} FAILED after {time.time()-t0:.2f}s: {e}")
            raise

    async def call_vivado(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Call a Vivado MCP tool. Raises MCPToolError on error responses."""
        async with self._vivado_lock:
            try:
                return await self._call_vivado_unlocked(tool_name, arguments, timeout)
            except Exception as exc:  # noqa: BLE001 - annotate one specific shape
                if not _is_closed_mcp_transport_error(exc):
                    raise
                # Say what a closed transport means, so the recovery action is
                # legible rather than inferred from a bare transport error.
                # Annotate here and not in _call_vivado_unlocked: restart_vivado
                # probes through that method directly and tells a closed
                # transport apart from a deterministic MCPToolError to decide
                # whether to rebuild the transport.  Converting the type there
                # would send it down the re-raise branch and strand it.
                raise MCPToolError(
                    "VIV",
                    tool_name,
                    "Vivado MCP transport is closed: the process is gone and "
                    "its in-memory design state with it. This command was not "
                    "replayed. Call restart_vivado to recover the process and "
                    "restore the current incumbent before any further mutation.",
                ) from exc

    async def _call_vivado_unlocked(
        self, tool_name: str, arguments: dict, timeout: float = 300.0
    ) -> str:
        """Call Vivado while the caller owns ``_vivado_lock``."""
        if self.vivado_session is None:
            raise RuntimeError("Vivado MCP session is not connected")
        logger.debug(f"[VIV] {tool_name} args={str(arguments)[:200]}")
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
            elapsed = time.time() - t0
            logger.info(f"[VIV] {tool_name} completed in {elapsed:.2f}s")
            return _extract_tool_response("VIV", tool_name, result)
        except asyncio.TimeoutError:
            logger.error(f"[VIV] {tool_name} TIMED OUT after {time.time()-t0:.2f}s")
            raise
        except MCPToolError as e:
            if e.structural_non_applicability:
                # `not_applicable`, not `infrastructure_error`.
                logger.info(f"[VIV] {tool_name} not applicable: {e}")
            else:
                logger.error(f"[VIV] {tool_name} returned error: {e}")
            raise
        except Exception as e:
            logger.error(f"[VIV] {tool_name} FAILED after {time.time()-t0:.2f}s: {e}")
            raise

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_rw_status(tool: str, response: str, accepted: set[str]) -> None:
        try:
            payload = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MCPToolError(
                "RW", tool, "RapidWright returned a non-JSON synchronization response",
                raw=response,
            ) from exc
        status = payload.get("status") if isinstance(payload, dict) else None
        if status not in accepted:
            raise MCPToolError(
                "RW", tool, f"unexpected synchronization status {status!r}", raw=response
            )

    async def _ensure_rapidwright_initialized(self, timeout: float) -> None:
        if self._rw_initialized:
            return
        self._rw_design_identity = None
        try:
            response = await self._call_rw_unlocked(
                "initialize_rapidwright", {}, timeout=timeout
            )
            self._require_rw_status(
                "initialize_rapidwright", response, {"success", "already_initialized"}
            )
        except Exception:
            self._rw_initialized = False
            self._rw_design_identity = None
            raise
        self._rw_initialized = True

    async def ensure_rapidwright_probe(
        self,
        tool_name: str,
        arguments: dict,
        *,
        candidate_id: Optional[str] = None,
        artifact_path: Optional[Path] = None,
        artifact_sha256: Optional[str] = None,
        timeout: float = 600.0,
    ) -> dict:
        """Synchronize a read-only probe to one exact graph-owned candidate.

        Pure device probes initialize RapidWright but deliberately do not load a
        DCP. Every other probe requires agent-supplied candidate identity; LLM
        arguments are never used to choose the checkpoint path.
        """
        if not isinstance(arguments, dict):
            raise TypeError("RapidWright probe arguments must be a dict")

        async with self._rw_probe_lock:
            return await self._ensure_rapidwright_probe_unlocked(
                tool_name,
                candidate_id=candidate_id,
                artifact_path=artifact_path,
                artifact_sha256=artifact_sha256,
                timeout=timeout,
            )

    async def _ensure_rapidwright_probe_unlocked(
        self,
        tool_name: str,
        *,
        candidate_id: Optional[str],
        artifact_path: Optional[Path],
        artifact_sha256: Optional[str],
        timeout: float,
    ) -> dict:
        await self._ensure_rapidwright_initialized(timeout)
        if tool_name in RAPIDWRIGHT_DEVICE_ONLY_PROBE_TOOLS:
            return {
                "status": "ready",
                "design_required": False,
                "reason": "pure_device_probe",
            }

        if not candidate_id or artifact_path is None or not artifact_sha256:
            self._rw_design_identity = None
            raise ValueError(
                "design-level RapidWright probe requires candidate ID, artifact path, "
                "and artifact SHA256"
            )
        digest = str(artifact_sha256).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            self._rw_design_identity = None
            raise ValueError("candidate artifact SHA256 is missing or invalid")

        try:
            canonical = validate_owned_candidate_path(
                self.run_dir, str(candidate_id), Path(artifact_path)
            )
            actual_digest = sha256_file(canonical)
        except Exception:
            self._rw_design_identity = None
            raise
        if actual_digest != digest:
            self._rw_design_identity = None
            raise ValueError(
                f"candidate {candidate_id} artifact SHA256 mismatch before RapidWright probe"
            )

        identity = (str(candidate_id), digest)
        if self._rw_design_identity == identity:
            return {
                "status": "reused",
                "design_required": True,
                "candidate_id": identity[0],
                "artifact_sha256": identity[1],
            }

        self._rw_design_identity = None
        try:
            response = await self._call_rw_unlocked(
                "read_checkpoint", {"dcp_path": str(canonical)}, timeout=timeout
            )
            self._require_rw_status("read_checkpoint", response, {"success"})
            payload = json.loads(response)
            reported = payload.get("design_sha256")
            # A missing field and a differing digest demand opposite fixes: the
            # first means the server predates the identity contract, the second
            # means the server resolved the path to different bytes. Collapsing
            # them into one message hides which one happened.
            if reported is None:
                raise MCPToolError(
                    "RW",
                    "read_checkpoint",
                    "server response omits design_sha256; the RapidWrightMCP "
                    "checkout predates the design-identity contract and cannot "
                    "prove which checkpoint it loaded",
                    raw=response,
                )
            if str(reported).lower() != digest:
                raise MCPToolError(
                    "RW",
                    "read_checkpoint",
                    "server loaded a checkpoint with a different SHA256 "
                    f"(expected {digest}, server reported {reported}); "
                    f"{canonical} may resolve to different bytes for the server",
                    raw=response,
                )
        except Exception:
            self._rw_design_identity = None
            raise
        self._rw_design_identity = identity
        return {
            "status": "loaded",
            "design_required": True,
            "candidate_id": identity[0],
            "artifact_sha256": identity[1],
        }

    async def call_exact_rapidwright_probe(
        self,
        tool_name: str,
        arguments: dict,
        *,
        candidate_id: Optional[str] = None,
        artifact_path: Optional[Path] = None,
        artifact_sha256: Optional[str] = None,
        timeout: float = 600.0,
    ) -> str:
        """Load/reuse one exact candidate and run its probe under one lock."""
        if not isinstance(arguments, dict):
            raise TypeError("RapidWright probe arguments must be a dict")
        async with self._rw_probe_lock:
            await self._ensure_rapidwright_probe_unlocked(
                tool_name,
                candidate_id=candidate_id,
                artifact_path=artifact_path,
                artifact_sha256=artifact_sha256,
                timeout=timeout,
            )
            return await self._call_rw_unlocked(tool_name, arguments, timeout)

    async def get_clock_period(
        self,
        clock_name: str = "clk_fpl26contest",
        *,
        allow_fallback: bool = True,
    ) -> Optional[float]:
        """
        Query clock period of the target clock.

        Prefers ``clock_name``; falls back to the worst setup path endpoint clock.
        Sets self.target_clock as a side effect.
        """
        fallback_tcl = (
            "  set tp [get_timing_paths -max_paths 1 -setup]; "
            "  if {$tp ne {}} { "
            "    set clk [get_property ENDPOINT_CLOCK $tp]; "
            "    if {$clk ne {}} { "
            "      puts \"CLOCK:$clk\"; "
            "      puts [get_property PERIOD [get_clocks $clk]]; "
            "    } "
            "  } "
            if allow_fallback else
            f"  puts \"CLOCK_NOT_FOUND:{clock_name}\"; "
        )
        tcl_cmd = (
            f"set contest_clk [get_clocks -quiet {{{clock_name}}}]; "
            "if {$contest_clk ne {}} { "
            f"  puts \"CLOCK:{clock_name}\"; "
            "  puts [get_property PERIOD $contest_clk]; "
            "} else { "
            + fallback_tcl +
            "}"
        )
        try:
            result = await self.call_vivado("run_tcl", {"command": tcl_cmd})
            clock_name = None
            for token in result.strip().split():
                if token.startswith("CLOCK:"):
                    clock_name = token[len("CLOCK:"):]
                    continue
                if token.startswith(("ERROR", "WARNING")):
                    continue
                try:
                    period = float(token)
                    if period > 0:
                        self.target_clock = clock_name
                        logger.info(f"Target clock: {clock_name}, period: {period:.3f} ns")
                        return period
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get clock period: {e}")
        logger.warning("Could not determine clock period")
        return None

    async def get_wns(
        self,
        clock: Optional[str] = None,
        *,
        allow_fallback: bool = True,
    ) -> Optional[float]:
        """
        Get WNS for the target clock domain (or overall WNS if no target clock).

        Uses self.target_clock if clock is None.
        """
        target = clock or self.target_clock
        if target:
            missing_clock_tcl = (
                "  set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                "  if {[llength $tp] > 0} {get_property SLACK $tp} else {puts NO_TIMING_PATH} "
                if allow_fallback else
                f"  puts CLOCK_NOT_FOUND:{target}; "
            )
            tcl_cmd = (
                f"set clk_obj [get_clocks -quiet {{{target}}}]; "
                f"if {{$clk_obj ne {{}}}} {{ "
                f"  set tp [get_timing_paths -max_paths 1 -setup -to $clk_obj]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts NO_TIMING_PATH}} "
                f"}} else {{ "
                + missing_clock_tcl +
                f"}}"
            )
        else:
            tcl_cmd = (
                "set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                "if {[llength $tp] > 0} {get_property SLACK $tp} else {puts NO_TIMING_PATH}"
            )
        try:
            result = await self.call_vivado("run_tcl", {"command": tcl_cmd})
            for token in result.strip().split("\n"):
                token = token.strip()
                if not token or token.startswith(("ERROR", "WARNING")):
                    continue
                try:
                    wns = float(token)
                    logger.info(f"WNS ({target or 'overall'}): {wns:.3f} ns")
                    return wns
                except ValueError:
                    continue
        except Exception as e:
            logger.warning(f"Failed to get WNS: {e}")
        if not allow_fallback:
            return None
        try:
            ts = await self.call_vivado("report_timing_summary", {}, timeout=300.0)
            parsed = parse_timing_summary_static(ts)
            wns = parsed.get("wns")
            if wns is not None:
                logger.info(f"WNS ({target or 'overall'} via timing summary): {wns:.3f} ns")
                return wns
        except Exception as e:
            logger.warning(f"Failed to get WNS from timing summary: {e}")
        return None
