"""Design feature extraction.

Converts the raw initial-analysis dict (timing profile, utilization, fanout
nets) into a flat, mostly *normalized* feature record. Normalized features
(``closure_ratio`` instead of absolute WNS, utilization percentages instead
of raw LUT counts) are what the knowledge base matches on and what the LLM
sees — absolute per-benchmark thresholds are deliberately avoided.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .naming import is_actionable_fanout_net, is_memory_address_net_name


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DesignFeatures:
    design_id: str = ""

    # --- timing ---
    wns_ns: float = 0.0
    clock_period_ns: Optional[float] = None
    # wns / clock_period: 0 = met, -0.1 = failing by 10% of the period.
    closure_ratio: Optional[float] = None
    baseline_fmax_mhz: Optional[float] = None
    achieved_period_ns: Optional[float] = None
    pre_route_to_routed_gap_mhz: float = 0.0

    # --- utilization ---
    lut_count: int = 0
    ff_count: int = 0
    dsp_count: int = 0
    bram_count: int = 0
    uram_count: int = 0
    lut_util_pct: Optional[float] = None
    hard_macro_present: bool = False

    # --- critical-path profile ---
    avg_route_pct: float = 0.0
    route_dominated_ratio: float = 0.0
    max_logic_levels: int = 0
    avg_logic_levels: float = 0.0
    critical_lut_stage_ratio: float = 0.0
    critical_lut_route_coverage: float = 0.0
    dominant_bottleneck: str = ""
    spread_ratio: float = 0.0
    spread_score: float = 0.0

    # --- fanout landscape ---
    max_fanout: int = 0
    max_actionable_fanout: int = 0
    fanout_live_wall_coverage: float = 0.0
    max_single_net_live_wall_coverage: float = 0.0
    eligible_hf_count: int = 0     # actionable nets with fanout >= 30
    broad_hf_count: int = 0        # actionable nets with fanout >= 500
    memory_address_profile: bool = False
    fanout_discovery_num_paths: int = 0

    # Private current-seed target evidence.  Rows are
    # (name, fanout, fanout_report_path_count, timing_profile_path_count,
    #  total_delay_ns, max_delay_ns).  Object names never enter the public
    # Decision display; they bind an executable card to its freshly observed
    # target inside the same run.
    top_actionable_nets: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["top_actionable_nets"] = [list(x) for x in self.top_actionable_nets]
        return d


def _lut_utilization(util: dict) -> Optional[float]:
    for key in ("lut_util_pct", "clb_lut_util_pct", "lut_utilization"):
        if util.get(key) is not None:
            return _as_float(util.get(key))
    return None


def _hard_macro_present(util: dict, profile: dict, structural: dict) -> bool:
    if _as_int(util.get("dsp") or util.get("dsps")) > 0:
        return True
    if _as_int(util.get("bram") or util.get("brams")) > 0:
        return True
    if _as_int(util.get("uram") or util.get("urams")) > 0:
        return True
    site_counts = (profile.get("placement_spread") or {}).get("site_type_counts") or {}
    if any(
        _as_int(count) > 0 and str(site).upper() != "SLICE"
        for site, count in site_counts.items()
    ):
        return True
    return _as_int(structural.get("hard_macro_count")) > 0


def _route_ratio(profile: dict) -> float:
    path_count = _as_int(profile.get("path_count"))
    if path_count <= 0:
        return 0.0
    return _as_int(profile.get("route_dominated_paths")) / path_count


def _critical_lut_stage_ratio(profile: dict) -> float:
    """Return the share of sampled live-wall primitives that are LUT stages."""
    histogram = profile.get("logic_histogram") or {}
    if not isinstance(histogram, dict):
        return 0.0
    total = 0
    lut_total = 0
    for primitive, raw_count in histogram.items():
        count = max(0, _as_int(raw_count))
        total += count
        if str(primitive).upper().startswith("LUT"):
            lut_total += count
    return lut_total / total if total > 0 else 0.0


def _spread_ratio_and_score(profile: dict) -> tuple[float, float]:
    spread = profile.get("placement_spread") or {}
    with_sites = _as_int(spread.get("path_count_with_sites"))
    spread_paths = _as_int(spread.get("spread_path_count"))
    ratio = spread_paths / with_sites if with_sites > 0 else 0.0
    score = _as_float(spread.get("max_path_span_x")) + _as_float(
        spread.get("max_path_span_y")
    )
    return ratio, score


def extract_features(analysis: dict, design_id: Optional[str] = None) -> DesignFeatures:
    profile = analysis.get("timing_profile") or {}
    util = analysis.get("utilization") or {}
    hard_macro_cascades = analysis.get("hard_macro_cascades") or {}
    fanout_nets = analysis.get("fanout_nets") or []
    # A resumed staged run reloads selector-free parsed telemetry from its
    # immutable report snapshot.  The public fanout rows intentionally no
    # longer contain net names, so they cannot be reclassified with the name
    # filters below.  Preserve the already computed, candidate-bound numeric
    # summary separately instead of either exposing selectors or silently
    # turning the fanout landscape into zeros.
    trusted_feature_summary = analysis.get("_trusted_feature_summary") or {}
    fanout_timing_evidence = profile.get("fanout_parent_net_evidence") or {}

    fanouts: list[int] = []
    actionable: list[tuple[str, int, int]] = []
    actionable_path_counts: list[int] = []
    memory_address_seen = False
    for item in fanout_nets:
        try:
            name, fanout, paths = item
        except (TypeError, ValueError):
            continue
        fanout = _as_int(fanout)
        fanouts.append(fanout)
        if is_memory_address_net_name(str(name)):
            memory_address_seen = True
        if is_actionable_fanout_net(str(name), fanout):
            path_count = max(0, _as_int(paths))
            actionable.append((str(name), fanout, path_count))
            actionable_path_counts.append(path_count)

    actionable.sort(key=lambda x: (-x[1], -x[2], x[0]))
    eligible_hf = [x for x in actionable if x[1] >= 30]
    broad_hf = [x for x in actionable if x[1] >= 500]
    spread_ratio, spread_score = _spread_ratio_and_score(profile)

    logic_levels = profile.get("logic_levels") or {}
    max_logic = _as_int(logic_levels.get("max"))
    avg_logic = _as_float(logic_levels.get("avg"))
    avg_route = _as_float(profile.get("avg_route_pct"))
    critical_lut_stage_ratio = _critical_lut_stage_ratio(profile)

    wns = _as_float(analysis.get("baseline_wns"), _as_float(profile.get("worst_slack_ns")))
    clock_period = (
        _as_float(analysis.get("clock_period"))
        if analysis.get("clock_period") is not None
        else None
    )

    closure_ratio = None
    baseline_fmax = None
    achieved_period = None
    if clock_period and clock_period > 0:
        closure_ratio = wns / clock_period
        achieved = clock_period - wns
        if achieved > 0:
            achieved_period = achieved
            baseline_fmax = 1000.0 / achieved

    def restored_int(name: str, fallback: int) -> int:
        value = trusted_feature_summary.get(name)
        return fallback if value is None else _as_int(value, fallback)

    def restored_bool(name: str, fallback: bool) -> bool:
        value = trusted_feature_summary.get(name)
        return fallback if value is None else bool(value)

    def restored_float(name: str, fallback: float) -> float:
        value = trusted_feature_summary.get(name)
        return fallback if value is None else _as_float(value, fallback)

    sampled_fanout_path_count = max(
        0,
        _as_int(analysis.get("fanout_discovery_num_paths")),
    )
    sampled_timing_path_count = max(0, _as_int(profile.get("path_count")))
    actionable_names = {name for name, _fanout, _paths in actionable}
    path_samples = profile.get("path_samples") or []
    profiled_union_count = sum(
        1
        for item in path_samples
        if isinstance(item, dict)
        and str(item.get("top_net") or "") in actionable_names
    )
    fanout_report_single_coverage = (
        min(
            1.0,
            max(actionable_path_counts, default=0) / sampled_fanout_path_count,
        )
        if sampled_fanout_path_count > 0
        else 0.0
    )
    # The per-net path counts can overlap heavily; summing them made aggregate
    # coverage stick at 1.0 after a successful broad fanout pass.  Use the
    # exact union visible in current timing samples, or the conservative
    # maximum single-net coverage when path identities are unavailable.
    fanout_wall_coverage = (
        min(1.0, profiled_union_count / sampled_timing_path_count)
        if path_samples and sampled_timing_path_count > 0
        else fanout_report_single_coverage
    )
    top_net_path_counts = [
        max(0, _as_int(item.get("path_count")))
        for item in (profile.get("top_nets") or [])
        if isinstance(item, dict)
        and str(item.get("name") or "") in actionable_names
    ]
    max_single_net_wall_coverage = max(
        fanout_report_single_coverage,
        (
            min(1.0, max(top_net_path_counts, default=0) / sampled_timing_path_count)
            if sampled_timing_path_count > 0 else 0.0
        ),
    )

    return DesignFeatures(
        design_id=str(design_id or ""),
        wns_ns=wns,
        clock_period_ns=clock_period,
        closure_ratio=closure_ratio,
        baseline_fmax_mhz=baseline_fmax,
        achieved_period_ns=achieved_period,
        pre_route_to_routed_gap_mhz=_as_float(analysis.get("pre_route_to_routed_gap_mhz")),
        lut_count=_as_int(util.get("luts") or util.get("clb_luts") or util.get("lut")),
        ff_count=_as_int(util.get("ffs") or util.get("ff") or util.get("registers")),
        dsp_count=_as_int(util.get("dsp") or util.get("dsps")),
        bram_count=_as_int(util.get("bram") or util.get("brams")),
        uram_count=_as_int(util.get("uram") or util.get("urams")),
        lut_util_pct=_lut_utilization(util),
        hard_macro_present=_hard_macro_present(util, profile, hard_macro_cascades),
        avg_route_pct=avg_route,
        route_dominated_ratio=_route_ratio(profile),
        max_logic_levels=max_logic,
        avg_logic_levels=avg_logic,
        critical_lut_stage_ratio=critical_lut_stage_ratio,
        critical_lut_route_coverage=(
            critical_lut_stage_ratio * min(1.0, max(0.0, avg_route / 100.0))
        ),
        dominant_bottleneck=str(profile.get("dominant_bottleneck") or "").lower(),
        spread_ratio=spread_ratio,
        spread_score=spread_score,
        max_fanout=restored_int("max_fanout", max(fanouts, default=0)),
        max_actionable_fanout=restored_int(
            "max_actionable_fanout",
            max((f for _n, f, _paths in actionable), default=0),
        ),
        fanout_live_wall_coverage=restored_float(
            "fanout_live_wall_coverage", fanout_wall_coverage
        ),
        max_single_net_live_wall_coverage=restored_float(
            "max_single_net_live_wall_coverage", max_single_net_wall_coverage
        ),
        eligible_hf_count=restored_int("eligible_hf_count", len(eligible_hf)),
        broad_hf_count=restored_int("broad_hf_count", len(broad_hf)),
        memory_address_profile=restored_bool(
            "memory_address_profile",
            (
                memory_address_seen
                and not broad_hf
                and max_logic >= 7
                and avg_route >= 45.0
            ),
        ),
        fanout_discovery_num_paths=_as_int(
            analysis.get("fanout_discovery_num_paths")
        ),
        top_actionable_nets=tuple(
            (
                name,
                fanout,
                fanout_paths,
                max(0, _as_int(target_timing.get("path_count"))),
                max(0.0, _as_float(target_timing.get("total_delay_ns"))),
                max(0.0, _as_float(target_timing.get("max_delay_ns"))),
            )
            for name, fanout, fanout_paths in actionable[:64]
            for top_net in [next(
                (
                    item for item in (profile.get("top_nets") or [])
                    if isinstance(item, dict) and str(item.get("name") or "") == name
                ),
                {},
            )]
            for target_timing in [
                (
                    fanout_timing_evidence.get(name)
                    if isinstance(fanout_timing_evidence, dict)
                    and isinstance(fanout_timing_evidence.get(name), dict)
                    else top_net
                )
            ]
        ),
    )
