"""Net/cell-name classification predicates.

Single authoritative copy of the name heuristics that previously lived in
three diverging variants (agent.py, recipe_planner.py, and inline Tcl).
These filters exist for *safety and signal quality*: clock trees and hard
macro pins are never useful replication/fanout targets and mutating them
risks illegal results.
"""

from __future__ import annotations

import re

_CLOCK_FRAGMENTS = (
    "bufg",
    "mmcm",
    "pll",
    "wclk",
    "rclk",
    "wrclk",
    "rdclk",
    "txoutclk",
    "rxoutclk",
    "userclk",
    "coreclk",
)

# Union of the two previous hard-macro leaf pattern sets (agent.py had the
# DO/DI variants, recipe_planner did not; keep the superset).
_HARD_MACRO_LEAF_PATTERNS = (
    r"ADDR[A-D](?:\d+)?",
    r"RAM[A-D](?:_[A-Z]+\d*)?",
    r"DOUT[A-D]DOUT(?:\d+)?",
    r"DIN[A-D]DIN(?:\d+)?",
    r"DO[A-D]DO(?:\d+)?",
    r"DI[A-D]DI(?:\d+)?",
    r"CLK[A-D]?(?:RDCLK|WRCLK)?",
    r"[WR]CLK",
    r"WE[A-D]?(?:\d+)?",
    r"EN[A-D]?(?:\d+)?",
)

_MEMORY_ADDRESS_PATTERNS = (
    r"(?:^|[/_])addr(?:ess)?\d*(?:$|[/_\[])",
    r"(?:^|[/_])address\d*(?:$|[/_\[])",
    r"(?:^|[/_])addr_reg(?:$|[/_\[])",
)


def is_clock_like_net_name(net_name: str) -> bool:
    """True when the net is (part of) a clock tree and must not be touched."""
    lowered = net_name.lower()
    parts = [
        re.sub(r"(?:\[\d+\])+$", "", p.lower())
        for p in net_name.replace("[", "/").split("/")
        if p
    ]
    if any(p in {"clk", "clock"} for p in parts):
        return True
    if any(
        p in {"ap_clk", "aclk"}
        or p.startswith(("clk_", "clock_"))
        or p.endswith(("_clk", "_clock", "_aclk"))
        for p in parts
    ):
        return True
    return any(fragment in lowered for fragment in _CLOCK_FRAGMENTS)


def is_hard_macro_pin_name(net_name: str) -> bool:
    """True when the leaf looks like a BRAM/URAM/DSP port rather than fabric."""
    leaf = net_name.rsplit("/", 1)[-1].upper()
    leaf = re.sub(r"(?:\[\d+\])+$", "", leaf)
    return any(re.fullmatch(pattern, leaf) for pattern in _HARD_MACRO_LEAF_PATTERNS)


def is_memory_address_net_name(net_name: str) -> bool:
    """True for memory/address plumbing nets (poor replication targets)."""
    lowered = net_name.lower()
    return any(re.search(pattern, lowered) for pattern in _MEMORY_ADDRESS_PATTERNS)


def is_unsuitable_force_replicate_net_name(net_name: str) -> bool:
    return is_clock_like_net_name(net_name) or is_hard_macro_pin_name(net_name)


def is_unsuitable_fanout_opt_net_name(net_name: str) -> bool:
    return is_clock_like_net_name(net_name) or is_memory_address_net_name(net_name)


def is_actionable_fanout_net(net_name: str, fanout: int) -> bool:
    """A net worth considering for replication/splitting at all."""
    if fanout < 2:
        return False
    if is_clock_like_net_name(net_name):
        return False
    if is_hard_macro_pin_name(net_name):
        return False
    return True
