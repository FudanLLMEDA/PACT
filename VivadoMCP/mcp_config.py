"""Standalone tool-layer config reader for the MCP servers.

The MCP servers run as separate processes and must not depend on the
FDAgents package. This helper reads the same YAML the agent uses
(``FDAGENTS_CONFIG`` env var, falling back to the packaged
``FDAgents/config.yaml``) and exposes one section with defaults.

If PyYAML or the file is unavailable, callers get their hard-coded
fallbacks — the tool layer must keep working standalone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _REPO_ROOT / "FDAgents" / "config.yaml"

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    path_str = os.environ.get("FDAGENTS_CONFIG", "")
    path = Path(path_str).expanduser() if path_str else _DEFAULT_CONFIG
    data: dict = {}
    try:
        import yaml  # type: ignore

        if path.exists():
            with path.open() as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                data = loaded
    except Exception:  # noqa: BLE001 — standalone fallback
        data = {}
    _cache = data
    return data


def tool_config(section: str) -> dict:
    """Return e.g. tool_config('vivado_mcp') -> dict (possibly empty)."""
    value = _load().get(section)
    return value if isinstance(value, dict) else {}


def tool_param(section: str, key: str, default: Any) -> Any:
    value = tool_config(section).get(key, default)
    return default if value is None else value


def reset_cache() -> None:
    global _cache
    _cache = None
