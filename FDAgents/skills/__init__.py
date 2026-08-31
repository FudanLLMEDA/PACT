"""Skill registry for FDAgents."""

from .phys_opt import PhysOptSkill
from .fanout_opt import FanoutOptSkill
from .cell_replace import CellReplaceSkill
from .pblock import PblockSkill
from .pblock_sweep import PblockSweepSkill
from .lut_merge import LutMergeSkill
from .per_net_unroute import PerNetUnrouteSkill
from .force_replicate import ForceReplicateSkill
from .checkpoint_import import CheckpointImportSkill
from .lut_pin_swap import LutPinSwapSkill
from .post_route_cleanup import PostRouteCleanupSkill
from .critical_net_reroute import CriticalNetRerouteSkill
from .critical_cluster_anchor import CriticalClusterAnchorSkill
from .fresh_place_route import FreshPlaceRouteSkill
from .endpoint_bel_move import EndpointBelMoveSkill
from .clock_tighten import ClockTightenSkill
from .path_local_lut_reflow import PathLocalLutReflowSkill
from .custom import CustomSkill

SKILLS = {
    "phys_opt": PhysOptSkill(),
    "fanout_opt": FanoutOptSkill(),
    "cell_replace": CellReplaceSkill(),
    "pblock_sweep": PblockSweepSkill(),
    "pblock": PblockSkill(),
    "lut_merge": LutMergeSkill(),
    "per_net_unroute": PerNetUnrouteSkill(),
    "force_replicate": ForceReplicateSkill(),
    "checkpoint_import": CheckpointImportSkill(),
    "lut_pin_swap": LutPinSwapSkill(),
    "post_route_cleanup": PostRouteCleanupSkill(),
    "critical_net_reroute": CriticalNetRerouteSkill(),
    "critical_cluster_anchor": CriticalClusterAnchorSkill(),
    "fresh_place_route": FreshPlaceRouteSkill(),
    "endpoint_bel_move": EndpointBelMoveSkill(),
    "clock_tighten": ClockTightenSkill(),
    "path_local_lut_reflow": PathLocalLutReflowSkill(),
    "custom": CustomSkill(),
}

# Skills that can only be chosen while deep-analysis mode is active.
# They bypass remaining_candidates (no fixed target list) and are gated
# by agent.py's deep-mode check + a per-run budget in memory.
DEEP_ONLY_SKILLS = {"custom"}

__all__ = [
    "SKILLS",
    "DEEP_ONLY_SKILLS",
    "PhysOptSkill",
    "FanoutOptSkill",
    "CellReplaceSkill",
    "PblockSweepSkill",
    "PblockSkill",
    "LutMergeSkill",
    "PerNetUnrouteSkill",
    "ForceReplicateSkill",
    "CheckpointImportSkill",
    "LutPinSwapSkill",
    "PostRouteCleanupSkill",
    "CriticalNetRerouteSkill",
    "CriticalClusterAnchorSkill",
    "FreshPlaceRouteSkill",
    "EndpointBelMoveSkill",
    "ClockTightenSkill",
    "PathLocalLutReflowSkill",
    "CustomSkill",
]
