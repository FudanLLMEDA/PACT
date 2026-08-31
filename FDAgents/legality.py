"""Contest legality guard — the ONLY hard constraint layer.

Everything strategic is the LLM's call. This module blocks exactly what
would break contest rules or corrupt the run:

  1. Unknown skills / malformed actions (can't be executed at all).
  2. `custom` probes invoking forbidden tools or Tcl that changes functional
     behavior or constraint semantics (equivalence & scoring integrity).
  3. Result legality: an accepted checkpoint must be fully routed and keep
     hold / pulse-width clean (either violation scores zero).

Nothing here expresses a strategy preference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .config import Config, get_config


_INTERNAL_ONLY_PROBES = {
    ("rapidwright", "recover_fixed_point_contract"),
    ("rapidwright", "propose_fixed_point_placement_candidates"),
    ("rapidwright", "recover_two_operand_contract"),
    ("rapidwright", "propose_two_operand_placement_candidates"),
    ("rapidwright", "recover_register_absorb_contract"),
    ("rapidwright", "recover_pipeline_rebalance_contract"),
    ("rapidwright", "recover_registered_product_sum_contract"),
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover — convenience
        return self.ok


ALLOW = Verdict(True)


def _forbidden_tcl_patterns(cfg: Config) -> list[re.Pattern]:
    pats = cfg.get("legality.forbidden_tcl_patterns", []) or []
    return [re.compile(p, re.IGNORECASE) for p in pats]


def check_action(
    action: dict,
    *,
    custom_calls_used: int = 0,
    cfg: Optional[Config] = None,
) -> Verdict:
    """Validate an LLM-proposed action against contest rules."""
    cfg = cfg or get_config()
    from .skills import SKILLS, DEEP_ONLY_SKILLS

    act = str(action.get("action", "run_skill"))
    if act == "save_and_exit":
        return ALLOW
    if act == "probe":
        return _check_probe(action, custom_calls_used=custom_calls_used, cfg=cfg)
    if act == "restart_vivado":
        unknown = set(action) - {"action"}
        if unknown:
            return Verdict(
                False,
                f"restart_vivado accepts no public arguments: {sorted(unknown)}",
            )
        return ALLOW
    if act != "run_skill":
        return Verdict(False, f"unknown action kind {act!r}")

    skill = str(action.get("skill", ""))
    if skill not in SKILLS:
        return Verdict(False, f"unknown skill {skill!r}")
    if skill in DEEP_ONLY_SKILLS:
        # `custom` executed as a skill == probe path with the same checks.
        return _check_probe(
            {
                "server": (action.get("params") or {}).get("server", "vivado"),
                "tool": action.get("target"),
                "arguments": (action.get("params") or {}).get("arguments", {}),
            },
            custom_calls_used=custom_calls_used,
            cfg=cfg,
        )

    # Params must be a dict (skills index into it freely).
    params = action.get("params")
    if params is not None and not isinstance(params, dict):
        return Verdict(False, "params must be an object")

    # Only exact commands materialized from internal registries may reach this
    # layer. Public actions cannot introduce even otherwise legal raw Tcl.
    for key, blob in _collect_tcl_fields(params or {}):
        if key != "command" or blob not in _trusted_recipe_commands():
            return Verdict(False, f"raw Tcl public parameter {key!r} is forbidden")
        v = check_tcl(blob, cfg=cfg)
        if not v.ok:
            return v
    return ALLOW


def _check_probe(
    probe: dict, *, custom_calls_used: int, cfg: Config
) -> Verdict:
    # Probe count is strategic, not a contest-legality property.  A global
    # per-run cap used to strand Luna immediately after a topology-changing
    # promotion: the old-seed probes consumed the quota, so the Agent could
    # mine a new family but could not recover its current-seed contract.  The
    # authoritative time and token budgets already bound exploration.  Keep
    # counting calls for audit, but never veto an otherwise safe read-only
    # probe here.
    _ = custom_calls_used

    server = str(probe.get("server") or "").strip().lower()
    allowed_by_server = cfg.get("legality.allowed_custom_tools", {}) or {}
    if server not in allowed_by_server:
        return Verdict(False, f"unknown probe server {server!r}")

    tool = str(probe.get("tool") or "")
    if not tool:
        return Verdict(False, "probe needs a tool name")
    if (server, tool) in _INTERNAL_ONLY_PROBES:
        return Verdict(False, f"tool {tool!r} is an internal-only probe")
    allowed_tools = set(allowed_by_server.get(server) or [])
    if tool not in allowed_tools:
        return Verdict(False, f"tool {tool!r} is not an allowed {server} probe")
    forbidden_tools = set(cfg.get("legality.forbidden_custom_tools", []) or [])
    if tool in forbidden_tools:
        return Verdict(False, f"tool {tool!r} is forbidden")

    arguments = probe.get("arguments") or {}
    if not isinstance(arguments, dict):
        return Verdict(False, "probe arguments must be an object")
    forbidden_path_keys = {
        "input_file", "output_file", "golden_dcp", "revised_dcp",
        "dcp_path", "file_path", "path",
    }

    def validate_arguments(node) -> Optional[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).lower()
                if (
                    normalized in forbidden_path_keys
                    or normalized.endswith("_file")
                    or normalized.endswith("_path")
                ):
                    return f"probe argument {key!r} may access an arbitrary path"
                error = validate_arguments(value)
                if error:
                    return error
        elif isinstance(node, list):
            for value in node:
                error = validate_arguments(value)
                if error:
                    return error
        elif isinstance(node, str):
            if "\x00" in node or "\n" in node or "\r" in node:
                return "probe string arguments cannot contain control characters"
            if server == "vivado" and any(char in node for char in "{};$\\"):
                return "Vivado probe string contains unsafe Tcl metacharacters"
        return None

    argument_error = validate_arguments(arguments)
    if argument_error:
        return Verdict(False, argument_error)
    for blob in _collect_tcl_strings(arguments):
        v = check_tcl(blob, cfg=cfg)
        if not v.ok:
            return v
    return ALLOW


def check_tcl(tcl: str, *, cfg: Optional[Config] = None) -> Verdict:
    """Reject Tcl that would change behavior/constraints or escape the box."""
    cfg = cfg or get_config()
    for pat in _forbidden_tcl_patterns(cfg):
        if pat.search(tcl):
            return Verdict(False, f"forbidden Tcl matched {pat.pattern!r}")
    return ALLOW


_TCL_KEYS = {"command", "tcl", "script"}


def _trusted_recipe_commands() -> set[str]:
    from .skills.clock_tighten import _RECIPE_COMMANDS
    from .skills.fresh_place_route import DEFAULT_RECIPES as fresh
    from .skills.post_route_cleanup import DEFAULT_RECIPES as cleanup

    return {
        *map(str, _RECIPE_COMMANDS.values()),
        *(str(item["command"]) for item in fresh),
        *(str(item["command"]) for item in cleanup),
    }


def _collect_tcl_fields(node) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            normalized = str(key).lower()
            if normalized in _TCL_KEYS or normalized == "tcl_ref":
                found.append((normalized, value if isinstance(value, str) else repr(value)))
            else:
                found.extend(_collect_tcl_fields(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_tcl_fields(item))
    return found


def _collect_tcl_strings(node) -> list[str]:
    """Find raw Tcl strings in params (recipe commands, run_tcl args...)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _TCL_KEYS and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_tcl_strings(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_tcl_strings(item))
    return found


# ---------------------------------------------------------------------------
# Result legality (accept-time checks)
# ---------------------------------------------------------------------------

def check_result(
    *,
    routed_ok: Optional[bool],
    whs: Optional[float] = None,
    wpws: Optional[float] = None,
    cfg: Optional[Config] = None,
) -> Verdict:
    """A candidate checkpoint may only be accepted when contest-legal."""
    cfg = cfg or get_config()
    if cfg.get("legality.require_fully_routed", True) and routed_ok is not True:
        return Verdict(False, f"not fully routed (routed_ok={routed_ok})")
    if cfg.get("legality.require_hold_clean", True):
        if whs is None:
            return Verdict(False, "hold telemetry unavailable (WHS=None)")
        if whs < 0.0:
            return Verdict(False, f"hold violation (WHS={whs})")
    if cfg.get("legality.require_pulse_width_clean", True):
        if wpws is None:
            return Verdict(False, "pulse width telemetry unavailable (WPWS=None)")
        if wpws < 0.0:
            return Verdict(False, f"pulse width violation (WPWS={wpws})")
    return ALLOW
