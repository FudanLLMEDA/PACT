"""Strict result checks shared by the deterministic artifact smoke flows."""

from __future__ import annotations

import json
import re
from typing import Any, Optional


class MCPToolPayloadError(RuntimeError):
    """A tool call completed at the transport layer but returned an error payload."""


def check_mcp_tool_payload(text: str, tool_name: str) -> Optional[Any]:
    """Raise when a JSON MCP response reports an application-level failure."""
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return payload

    error = payload.get("error")
    status = str(payload.get("status", "")).strip().lower()
    if error:
        raise MCPToolPayloadError(f"{tool_name} returned error: {error}")
    if status in {"error", "fail", "failed", "failure"}:
        message = payload.get("message") or payload
        raise MCPToolPayloadError(f"{tool_name} returned status={status}: {message}")
    return payload


def route_error_count(route_report: str) -> int:
    """Return Vivado's routing-error count, failing closed if it is absent."""
    match = re.search(
        r"# of nets with routing errors[.\s:]+(\d+)",
        route_report,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError("Could not parse '# of nets with routing errors' from route report")
    return int(match.group(1))


def require_route_clean(route_report: str) -> None:
    """Raise unless Vivado explicitly reports zero routing errors."""
    errors = route_error_count(route_report)
    if errors != 0:
        raise RuntimeError(f"Candidate has {errors} routing error(s)")
