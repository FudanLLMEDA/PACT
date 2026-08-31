"""
MCP client for FDAgents — manages RapidWright and Vivado MCP server sessions.

Extracted from DCPOptimizerBase in dcp_optimizer.py, with LLM code removed.
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .skills.base import parse_timing_summary_static

logger = logging.getLogger(__name__)


class MCPToolError(RuntimeError):
    """Raised when an MCP tool returns an error response (instead of throwing)."""

    def __init__(self, server: str, tool: str, message: str, raw: str = ""):
        self.server = server
        self.tool = tool
        self.raw = raw
        super().__init__(f"[{server}] {tool}: {message}")


def _detect_tool_error(server: str, tool: str, response: str) -> None:
    """
    Inspect a tool response for error markers and raise MCPToolError if found.

    Triggers on:
      - top-level JSON object with an "error" key
      - line starting with "ERROR:" or "ERROR [" (Vivado convention)

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

    # Vivado-style ERROR lines
    for line in response.splitlines():
        line = line.strip()
        if line.startswith("ERROR:") or line.startswith("ERROR ["):
            raise MCPToolError(server, tool, line[:200], raw=response)


class MCPClient:
    """Manages MCP server sessions for RapidWright and Vivado."""

    def __init__(self, run_dir: Path, debug: bool = False):
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.debug = debug
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.exit_stack = AsyncExitStack()
        self.rapidwright_session: Optional[ClientSession] = None
        self.vivado_session: Optional[ClientSession] = None

        self.target_clock: Optional[str] = None

        self._rw_log_file = None
        self._v_log_file = None

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start_servers(self) -> None:
        """Start and connect to both MCP servers."""
        script_dir = Path(__file__).parent.parent.resolve()

        rapidwright_log = self.run_dir / "rapidwright.log"
        rapidwright_mcp_log = self.run_dir / "rapidwright-mcp.log"
        vivado_log = self.run_dir / "vivado.log"
        vivado_journal = self.run_dir / "vivado.jou"
        vivado_mcp_log = self.run_dir / "vivado-mcp.log"

        if self.debug:
            self._rw_log_file = None
            self._v_log_file = None
            logger.info("Debug mode: MCP server output shown in console")
        else:
            self._rw_log_file = open(rapidwright_mcp_log, "w")
            self._v_log_file = open(vivado_mcp_log, "w")
            logger.info(f"Logs in {self.run_dir}: {rapidwright_log.name}, {vivado_log.name}")

        # RapidWright MCP server
        rw_args = [str(script_dir / "RapidWrightMCP" / "server.py")]
        if not self.debug:
            rw_args.extend([
                "--java-log", str(rapidwright_log),
                "--mcp-log", str(rapidwright_mcp_log),
            ])

        env = {**os.environ}
        rw_submodule = script_dir / "RapidWright"
        if rw_submodule.is_dir() and "RAPIDWRIGHT_PATH" not in env:
            env["RAPIDWRIGHT_PATH"] = str(rw_submodule)
            env["CLASSPATH"] = f"{rw_submodule}/bin:{rw_submodule}/jars/*"

        rw_config = {
            "command": sys.executable,
            "args": rw_args,
            "cwd": str(self.run_dir),
            "env": env,
        }

        # Vivado MCP server
        v_args = [str(script_dir / "VivadoMCP" / "vivado_mcp_server.py")]
        if not self.debug:
            v_args.extend([
                "--vivado-log", str(vivado_log),
                "--vivado-journal", str(vivado_journal),
            ])

        v_config = {
            "command": sys.executable,
            "args": v_args,
            "cwd": str(self.run_dir),
            "env": {**os.environ},
        }

        # Start RapidWright
        logger.info("Starting RapidWright MCP server...")
        t0 = time.time()
        rw_params = StdioServerParameters(**rw_config)
        rw_transport = await self.exit_stack.enter_async_context(
            stdio_client(rw_params, errlog=self._rw_log_file)
        )
        rw_read, rw_write = rw_transport
        self.rapidwright_session = await self.exit_stack.enter_async_context(
            ClientSession(rw_read, rw_write)
        )
        await self.rapidwright_session.initialize()
        logger.info(f"RapidWright MCP started in {time.time()-t0:.2f}s")

        # Start Vivado
        logger.info("Starting Vivado MCP server...")
        t0 = time.time()
        v_params = StdioServerParameters(**v_config)
        v_transport = await self.exit_stack.enter_async_context(
            stdio_client(v_params, errlog=self._v_log_file)
        )
        v_read, v_write = v_transport
        self.vivado_session = await self.exit_stack.enter_async_context(
            ClientSession(v_read, v_write)
        )
        await self.vivado_session.initialize()
        logger.info(f"Vivado MCP started in {time.time()-t0:.2f}s")

        logger.info("Both MCP servers connected")

    async def cleanup(self) -> None:
        """Release all resources."""
        await self.exit_stack.aclose()
        if self._rw_log_file:
            self._rw_log_file.close()
        if self._v_log_file:
            self._v_log_file.close()
        logger.info(f"Run directory preserved at: {self.run_dir}")

    # ------------------------------------------------------------------
    # Low-level tool calls
    # ------------------------------------------------------------------

    async def call_rw(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Call a RapidWright MCP tool. Raises MCPToolError on error responses."""
        logger.debug(f"[RW] {tool_name} args={str(arguments)[:200]}")
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                self.rapidwright_session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
            elapsed = time.time() - t0
            logger.info(f"[RW] {tool_name} completed in {elapsed:.2f}s")
            if result.content:
                response = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            else:
                response = "(no output)"
            _detect_tool_error("RW", tool_name, response)
            return response
        except asyncio.TimeoutError:
            logger.error(f"[RW] {tool_name} TIMED OUT after {time.time()-t0:.2f}s")
            raise
        except MCPToolError as e:
            logger.error(f"[RW] {tool_name} returned error: {e}")
            raise
        except Exception as e:
            logger.error(f"[RW] {tool_name} FAILED after {time.time()-t0:.2f}s: {e}")
            raise

    async def call_vivado(self, tool_name: str, arguments: dict, timeout: float = 300.0) -> str:
        """Call a Vivado MCP tool. Raises MCPToolError on error responses."""
        logger.debug(f"[VIV] {tool_name} args={str(arguments)[:200]}")
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                self.vivado_session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
            elapsed = time.time() - t0
            logger.info(f"[VIV] {tool_name} completed in {elapsed:.2f}s")
            if result.content:
                response = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            else:
                response = "(no output)"
            _detect_tool_error("VIV", tool_name, response)
            return response
        except asyncio.TimeoutError:
            logger.error(f"[VIV] {tool_name} TIMED OUT after {time.time()-t0:.2f}s")
            raise
        except MCPToolError as e:
            logger.error(f"[VIV] {tool_name} returned error: {e}")
            raise
        except Exception as e:
            logger.error(f"[VIV] {tool_name} FAILED after {time.time()-t0:.2f}s: {e}")
            raise

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    async def get_clock_period(self) -> Optional[float]:
        """
        Query clock period of the target clock.

        Prefers clk_fpl26contest; falls back to the worst setup path endpoint clock.
        Sets self.target_clock as a side effect.
        """
        tcl_cmd = (
            "set contest_clk [get_clocks -quiet clk_fpl26contest]; "
            "if {$contest_clk ne {}} { "
            "  puts \"CLOCK:clk_fpl26contest\"; "
            "  puts [get_property PERIOD $contest_clk]; "
            "} else { "
            "  set tp [get_timing_paths -max_paths 1 -setup]; "
            "  if {$tp ne {}} { "
            "    set clk [get_property ENDPOINT_CLOCK $tp]; "
            "    if {$clk ne {}} { "
            "      puts \"CLOCK:$clk\"; "
            "      puts [get_property PERIOD [get_clocks $clk]]; "
            "    } "
            "  } "
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

    async def get_wns(self, clock: Optional[str] = None) -> Optional[float]:
        """
        Get WNS for the target clock domain (or overall WNS if no target clock).

        Uses self.target_clock if clock is None.
        """
        target = clock or self.target_clock
        if target:
            tcl_cmd = (
                f"set clk_obj [get_clocks -quiet {{{target}}}]; "
                f"if {{$clk_obj ne {{}}}} {{ "
                f"  set tp [get_timing_paths -max_paths 1 -setup -to $clk_obj]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts NO_TIMING_PATH}} "
                f"}} else {{ "
                f"  set tp [get_timing_paths -max_paths 1 -slack_lesser_than 999]; "
                f"  if {{[llength $tp] > 0}} {{get_property SLACK $tp}} else {{puts NO_TIMING_PATH}} "
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
