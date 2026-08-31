"""
Memory state manager for FDAgents.

Tracks optimization state in memory.json under the run directory.
LLM sees only the compact view returned by compact_state_for_llm().
"""

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


BLACKLIST_PARAM_IGNORE_KEYS = {"input_dcp", "target_params"}
PARAM_VARIANT_ACCEPTED_SKILLS = {"force_replicate"}
PBLOCK_FAMILY_SKILLS = {"pblock", "pblock_sweep"}


def _blacklist_params_view(params: Optional[dict]) -> dict:
    if not params:
        return {}
    return {
        str(k): v
        for k, v in params.items()
        if k not in BLACKLIST_PARAM_IGNORE_KEYS and not str(k).startswith("_")
    }


def _blacklist_params_signature(params: Optional[dict]) -> str:
    return json.dumps(
        _blacklist_params_view(params),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _blacklist_hides_candidate(
    entry: dict,
    skill: str,
    target: str,
    current_best_round: Optional[int],
) -> bool:
    if entry.get("skill") != skill:
        return False
    entry_target = entry.get("target")
    if entry_target not in {target, "*"}:
        return False

    severity = entry.get("severity", "hard")
    if severity == "hard":
        return True
    if severity == "cooldown":
        return entry.get("best_round") == current_best_round
    return False


def _is_hard_macro_pblock_sweep_refusal(entry: dict) -> bool:
    if entry.get("skill") != "pblock_sweep":
        return False
    if entry.get("accepted"):
        return False
    text = " ".join(
        str(entry.get(key, ""))
        for key in ("summary", "reason", "message")
    ).lower()
    return "pblock_sweep" in text and "hard macro" in text


def _pblock_has_been_tried(history: list) -> bool:
    return any(
        entry.get("skill") == "pblock"
        or (
            entry.get("skill") == "pblock_sweep"
            and not _is_hard_macro_pblock_sweep_refusal(entry)
        )
        for entry in history
    )


def _pblock_candidate_available(remaining_candidates: dict) -> bool:
    return any(remaining_candidates.get(skill) for skill in PBLOCK_FAMILY_SKILLS)


def _defer_structural_auto_skill(
    skill: str,
    bottleneck: str,
    history: list,
    remaining_candidates: dict,
) -> bool:
    if skill == "cell_replace":
        matching_bottleneck = bottleneck == "placement"
    elif skill == "lut_merge":
        matching_bottleneck = bottleneck == "logic_depth"
    else:
        return False

    if matching_bottleneck:
        return False
    if _pblock_candidate_available(remaining_candidates) and not _pblock_has_been_tried(history):
        return True
    return bool(
        remaining_candidates.get("force_replicate")
        or remaining_candidates.get("per_net_unroute")
    )


@dataclass
class Memory:
    design_id: str
    baseline: dict                    # {wns, fmax, clock_period, bottleneck}
    current_best: dict                # {wns, fmax, dcp_path, round}
    history: list                     # per-round records
    budget: dict                      # {time_elapsed, time_remaining, tokens_spent, tokens_remaining}
    blacklist: list                   # [{skill, target, reason}]
    remaining_candidates: dict        # {skill: [target, ...]}
    deep_calls_used: int = 0
    round: int = 0
    custom_calls_used: int = 0                       # number of custom MCP tool calls executed
    last_custom_responses: list = field(default_factory=list)  # ring buffer of recent custom outputs

    # Per-run budget for custom MCP tool calls. See agent.py deep-mode gating.
    CUSTOM_CALL_BUDGET = 10
    # How many recent custom responses to feed back into the deep-mode LLM state.
    CUSTOM_RESPONSE_WINDOW = 3

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, default=str))

    @classmethod
    def load(cls, path: Path) -> "Memory":
        data = json.loads(path.read_text())
        return cls(**data)

    @classmethod
    def init(
        cls,
        design_id: str,
        baseline_wns: float,
        clock_period: Optional[float],
        bottleneck: str,
        input_dcp: str,
        time_limit_s: float = 3600.0,
        token_budget_usd: float = 1.0,
    ) -> "Memory":
        fmax = _calculate_fmax(baseline_wns, clock_period)
        return cls(
            design_id=design_id,
            baseline={
                "wns": baseline_wns,
                "fmax": fmax,
                "clock_period": clock_period,
                "bottleneck": bottleneck,
            },
            current_best={
                "wns": baseline_wns,
                "fmax": fmax,
                "dcp_path": input_dcp,
                "round": 0,
            },
            history=[],
            budget={
                "start_time": time.time(),
                "time_limit_s": time_limit_s,
                "time_elapsed": 0.0,
                "time_remaining": time_limit_s,
                "tokens_spent": 0.0,
                "tokens_remaining": token_budget_usd,
            },
            blacklist=[],
            remaining_candidates={
                "phys_opt": ["Default", "AggressiveExplore", "AggressiveFanoutOpt", "AddRetime"],
                "fanout_opt": [],   # populated after initial analysis
                "cell_replace": [],
                "critical_net_reroute": ["auto"],
                "critical_cluster_anchor": ["auto"],
                "post_route_cleanup": ["auto"],
                "fresh_place_route": ["auto"],
                "endpoint_bel_move": ["auto"],
                "clock_tighten": ["auto"],
                "pblock_sweep": [],
                "pblock": [],
            },
            deep_calls_used=0,
            round=0,
        )

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def budget_ok(self, deadline: Optional[float] = None) -> bool:
        """True if there is still time and token budget left."""
        now = time.time()
        self.budget["time_elapsed"] = now - self.budget["start_time"]
        if deadline is not None:
            self.budget["time_remaining"] = max(0.0, deadline - now)
        else:
            elapsed = self.budget["time_elapsed"]
            self.budget["time_remaining"] = max(
                0.0, self.budget["time_limit_s"] - elapsed
            )
        time_ok = self.budget["time_remaining"] > 300.0
        token_ok = self.budget["tokens_remaining"] > 0.0
        return time_ok and token_ok

    def record_token_spend(self, cost_usd: float) -> None:
        self.budget["tokens_spent"] += cost_usd
        self.budget["tokens_remaining"] = max(
            0.0, self.budget.get("tokens_remaining", 1.0) - cost_usd
        )

    # ------------------------------------------------------------------
    # Round recording
    # ------------------------------------------------------------------

    def record(
        self,
        skill: str,
        target: str,
        before_wns: float,
        after_wns: float,
        accepted: bool,
        params: dict,
        summary: str = "",
    ) -> None:
        self.round += 1
        self.history.append({
            "round": self.round,
            "skill": skill,
            "target": target,
            "before_wns": before_wns,
            "after_wns": after_wns,
            "delta_wns": after_wns - before_wns,
            "accepted": accepted,
            "params": params,
            "summary": summary,
            "timestamp": time.time(),
        })

    def update_best(self, wns: float, fmax: Optional[float], dcp_path: str) -> None:
        if wns > self.current_best["wns"]:
            self.current_best = {
                "wns": wns,
                "fmax": fmax,
                "dcp_path": dcp_path,
                "round": self.round,
            }

    def blacklist_add(
        self,
        skill: str,
        target: str,
        reason: str,
        *,
        severity: str = "hard",
        params: Optional[dict] = None,
        best_round: Optional[int] = None,
    ) -> None:
        entry = {
            "skill": skill,
            "target": target,
            "reason": reason,
            "severity": severity,
            "best_round": self.current_best.get("round") if best_round is None else best_round,
        }
        if params is not None:
            entry["params"] = _blacklist_params_view(params)
            entry["params_sig"] = _blacklist_params_signature(params)
        self.blacklist.append(entry)

        if severity == "hard":
            # Remove from remaining_candidates if present
            candidates = self.remaining_candidates.get(skill, [])
            if target in candidates:
                candidates.remove(target)

    def record_custom_response(
        self,
        server: str,
        tool: str,
        arguments: dict,
        response: str,
        description: str = "",
    ) -> None:
        """
        Record a custom MCP tool invocation + its (already-truncated) response.

        Pushes onto a ring buffer of length CUSTOM_RESPONSE_WINDOW so the next
        deep-mode LLM state can reason about recent experiments without
        ballooning the history entries.
        """
        self.custom_calls_used += 1
        entry = {
            "round": self.round,
            "server": server,
            "tool": tool,
            "arguments": arguments,
            "description": description,
            "response": response,
        }
        self.last_custom_responses.append(entry)
        if len(self.last_custom_responses) > self.CUSTOM_RESPONSE_WINDOW:
            self.last_custom_responses = self.last_custom_responses[-self.CUSTOM_RESPONSE_WINDOW:]

    def custom_budget_left(self) -> int:
        return max(0, self.CUSTOM_CALL_BUDGET - self.custom_calls_used)

    # ------------------------------------------------------------------
    # LLM-facing compact view (< 2K tokens)
    # ------------------------------------------------------------------

    def compact_state_for_llm(self, include_custom_window: bool = False) -> dict:
        """
        Return a compact dict to pass to LLM. No raw tool output by default.

        When include_custom_window=True (deep-mode calls only), also embed the
        last CUSTOM_RESPONSE_WINDOW custom MCP tool responses so the LLM can
        chain probes.
        """
        recent = self.history[-5:] if len(self.history) >= 5 else self.history
        compact_history = [
            {
                "round": r["round"],
                "skill": r["skill"],
                "target": r["target"],
                "params": _blacklist_params_view(r.get("params", {})),
                "delta_wns": round(r["delta_wns"], 4),
                "accepted": r["accepted"],
            }
            for r in recent
        ]

        accepted = {
            (r.get("skill"), r.get("target"))
            for r in self.history
            if r.get("accepted")
            and r.get("skill") not in PARAM_VARIANT_ACCEPTED_SKILLS
        }
        current_best_round = self.current_best.get("round")
        remaining = {}
        for skill, candidates in self.remaining_candidates.items():
            if _defer_structural_auto_skill(
                skill,
                self.baseline.get("bottleneck", "unknown"),
                self.history,
                self.remaining_candidates,
            ):
                continue
            visible = []
            for target in candidates:
                if (skill, target) in accepted:
                    continue
                if any(
                    _blacklist_hides_candidate(
                        entry,
                        skill,
                        target,
                        current_best_round,
                    )
                    for entry in self.blacklist
                ):
                    continue
                visible.append(target)
            if visible:
                remaining[skill] = visible

        state = {
            "round": self.round,
            "baseline_wns": self.baseline["wns"],
            "current_wns": self.current_best["wns"],
            "fmax_gain_mhz": _fmax_gain(
                self.baseline["wns"],
                self.current_best["wns"],
                self.baseline.get("clock_period"),
            ),
            "bottleneck": self.baseline.get("bottleneck", "unknown"),
            "history": compact_history,
            "remaining": remaining,
            "blacklist": self.blacklist[-10:],
            "budget": {
                "time_s": round(self.budget["time_remaining"], 0),
                "token_usd": round(self.budget["tokens_remaining"], 4),
            },
            "deep_calls_used": self.deep_calls_used,
            "custom_calls_used": self.custom_calls_used,
            "custom_calls_remaining": self.custom_budget_left(),
        }

        if self.baseline.get("timing_profile"):
            state["timing_profile"] = self.baseline["timing_profile"]

        if self.baseline.get("recipe_id"):
            state["recipe"] = {
                "id": self.baseline.get("recipe_id"),
                "confidence": self.baseline.get("recipe_confidence"),
                "reason": self.baseline.get("recipe_reason"),
                "core_skills": self.baseline.get("recipe_core_skills", []),
                "equivalence_risk": self.baseline.get("recipe_equivalence_risk", "low"),
            }
            learned_fmax = self.baseline.get("recipe_learned_fmax_mhz")
            if learned_fmax is not None:
                state["recipe"]["learned_fmax_mhz"] = learned_fmax
            knowledge_evidence = self.baseline.get("recipe_knowledge_evidence")
            if knowledge_evidence:
                state["recipe"]["knowledge_evidence"] = knowledge_evidence

        if include_custom_window and self.last_custom_responses:
            # Truncate each response for token safety; deep-mode input is
            # allowed to be larger but we still don't want one probe to
            # swamp the rest of the state. 12 KB per entry × 3 entries ≈
            # 36 KB ≈ 9 K tokens of probe context, enough for several
            # report_timing paths while still leaving room for raw_tool_output.
            state["recent_custom_calls"] = [
                {
                    "round": e.get("round"),
                    "server": e.get("server"),
                    "tool": e.get("tool"),
                    "arguments": e.get("arguments"),
                    "description": e.get("description"),
                    "response": (e.get("response") or "")[:12000],
                }
                for e in self.last_custom_responses
            ]

        return state


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _calculate_fmax(wns: Optional[float], clock_period: Optional[float]) -> Optional[float]:
    if clock_period is None or clock_period <= 0 or wns is None:
        return None
    achievable = clock_period - wns
    if achievable <= 0:
        return None
    return round(1000.0 / achievable, 3)


def _fmax_gain(
    baseline_wns: Optional[float],
    current_wns: Optional[float],
    clock_period: Optional[float],
) -> Optional[float]:
    base = _calculate_fmax(baseline_wns, clock_period)
    curr = _calculate_fmax(current_wns, clock_period)
    if base is None or curr is None:
        return None
    return round(curr - base, 3)
