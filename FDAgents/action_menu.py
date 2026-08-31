"""Deterministic, seed-bound menu of tool-authorized Agent actions.

The menu is a capability boundary, not an LLM evidence claim.  Tools construct
entries and bind every executable parameter.  Decision may select one exact ID
and explain the ranking, but it cannot alter the selected action.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .action_contracts import same_run_feedback_ranking_adjustment
from .regarith_tools.common import object_digest


SCHEMA_VERSION = "authorized-action-menu-v2"
DEFAULT_TOP_K = 4
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTRY_KINDS = {
    "mutation",
    "blocked_family",
    "probe",
    "physical_recipe",
    "proof_gated_mutation",
    "save_and_exit",
}


class ActionMenuError(ValueError):
    """Typed fail-closed menu construction or selection error."""

    def __init__(self, code: str, detail: str = ""):
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ActionMenuError("ACTION_MENU_NON_JSON_VALUE") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ActionMenuError("ACTION_MENU_INVALID_DIGEST", field)
    return value


def _entry_id(
    *,
    seed_artifact_sha256: str,
    action_target: str,
    family_fingerprint: str,
    recovered_plan_digest: str,
    proof_digest: str,
) -> str:
    """Hash exactly the five identity components in the MENU-001 contract."""
    return _digest({
        "seed_artifact_sha256": seed_artifact_sha256,
        "action_target": action_target,
        "family_fingerprint": family_fingerprint,
        "recovered_plan_digest": recovered_plan_digest,
        "proof_digest": proof_digest,
    })


def _make_entry(
    *,
    kind: str,
    eligible: bool,
    seed_artifact_sha256: str,
    action_target: str,
    family_fingerprint: str,
    recovered_plan_digest: str,
    proof_digest: str,
    proof_status: str,
    authorization_class: str,
    action: Mapping[str, Any] | None,
    display: Mapping[str, Any],
    blockers: Sequence[str],
) -> dict[str, Any]:
    if kind not in _ENTRY_KINDS:
        raise ActionMenuError("ACTION_MENU_INVALID_ENTRY_KIND", kind)
    for value, field in (
        (seed_artifact_sha256, "seed_artifact_sha256"),
        (family_fingerprint, "family_fingerprint"),
        (recovered_plan_digest, "recovered_plan_digest"),
        (proof_digest, "proof_digest"),
    ):
        _sha(value, field=field)
    if not isinstance(action_target, str) or not action_target:
        raise ActionMenuError("ACTION_MENU_INVALID_ACTION_TARGET")
    normalized_blockers = list(dict.fromkeys(str(item) for item in blockers if item))
    if eligible and normalized_blockers:
        raise ActionMenuError("ACTION_MENU_ELIGIBLE_ENTRY_HAS_BLOCKERS")
    if eligible and not isinstance(action, Mapping):
        raise ActionMenuError("ACTION_MENU_ELIGIBLE_ENTRY_HAS_NO_ACTION")
    return {
        "entry_id": _entry_id(
            seed_artifact_sha256=seed_artifact_sha256,
            action_target=action_target,
            family_fingerprint=family_fingerprint,
            recovered_plan_digest=recovered_plan_digest,
            proof_digest=proof_digest,
        ),
        "kind": kind,
        "eligible": bool(eligible),
        "seed_artifact_sha256": seed_artifact_sha256,
        "action_target": action_target,
        "family_fingerprint": family_fingerprint,
        "recovered_plan_digest": recovered_plan_digest,
        "proof_digest": proof_digest,
        "proof_status": str(proof_status),
        "authorization_class": str(authorization_class),
        "action": copy.deepcopy(dict(action)) if isinstance(action, Mapping) else None,
        "display": copy.deepcopy(dict(display)),
        "blockers": normalized_blockers,
    }


def _family_rows(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    siblings = packet.get("normalized_siblings")
    if not isinstance(siblings, list):
        return []
    # Packet rows may repeat an anonymous family.  Keep the strongest one while
    # preserving deterministic family ordering and every typed blocker.
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in siblings:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("family_kind") or "unknown")
        fingerprint = str(raw.get("family_fingerprint") or "")
        if _SHA256.fullmatch(fingerprint) is None:
            continue
        key = (kind, fingerprint)
        row = copy.deepcopy(dict(raw))
        prior = grouped.get(key)
        earliest = row.get("earliest_covered_rank")
        scope = row.get("transaction_scope_count")
        rank = (
            int(row.get("nomination_eligible") is True),
            int(row.get("proof_status") == "proved"),
            -int(earliest) if isinstance(earliest, int) else -10**9,
            int(scope) if isinstance(scope, int) else -1,
            int(row.get("live_wall_coverage_ppm") or 0),
        )
        prior_earliest = prior.get("earliest_covered_rank") if prior else None
        prior_scope = prior.get("transaction_scope_count") if prior else None
        prior_rank = (
            int(prior.get("nomination_eligible") is True),
            int(prior.get("proof_status") == "proved"),
            -int(prior_earliest) if isinstance(prior_earliest, int) else -10**9,
            int(prior_scope) if isinstance(prior_scope, int) else -1,
            int(prior.get("live_wall_coverage_ppm") or 0),
        ) if prior else (-1, -1, -10**9, -1, -1)
        if rank > prior_rank:
            grouped[key] = row
    return sorted(grouped.values(), key=lambda row: (
        # Already deep-closed, proved rows outrank broad hypothesis census
        # families even when those generic rows report wider path membership.
        -int(row.get("nomination_eligible") is True),
        (
            int(row["earliest_covered_rank"])
            if isinstance(row.get("earliest_covered_rank"), int) else 10**9
        ),
        -(
            int(row["transaction_scope_count"])
            if isinstance(row.get("transaction_scope_count"), int) else 0
        ),
        -int(row.get("live_wall_coverage_ppm") or 0),
        str(row.get("family_kind") or ""),
        str(row.get("family_fingerprint") or ""),
    ))


def _operator_structural_leverage(row: Mapping[str, Any]) -> dict[str, Any]:
    """Score current-seed structural payoff without historical QoR inputs."""
    covered = max(0, int(row.get("live_wall_covered_path_count") or 0))
    resolved = max(
        covered,
        int(row.get("live_wall_resolved_path_count") or 0),
        1,
    )
    coverage = min(
        1.0,
        max(0.0, float(row.get("live_wall_coverage_ppm") or 0) / 1_000_000.0),
    )
    earliest_raw = row.get("earliest_covered_rank")
    endpoints_raw = row.get("live_wall_endpoint_path_count")
    owns_no_endpoint = (
        not isinstance(endpoints_raw, bool)
        and isinstance(endpoints_raw, int)
        and endpoints_raw <= 0
    )
    earliest = (
        None
        # Owning the earliest wall means terminating the earliest failing path.
        # A family that terminates none of them may still sit on those paths,
        # but it does not remove the wall, and two families with identical
        # coverage and rank were otherwise indistinguishable here.
        if owns_no_endpoint
        else int(earliest_raw)
        if isinstance(earliest_raw, int)
        and not isinstance(earliest_raw, bool)
        and earliest_raw > 0
        else 1 if covered >= resolved and covered > 0 else None
    )
    earliest_ownership = 1.0 / float(earliest) if earliest else 0.0

    def log_scale(value: object, denominator: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return min(1.0, math.log2(max(0.0, float(value)) + 1.0) / denominator)

    serial_depth = log_scale(row.get("serial_depth"), 8.0)
    replaced_scope = log_scale(row.get("expected_replaced_instance_count"), 10.0)
    transaction_scope = log_scale(row.get("transaction_scope_count"), 6.0)
    score = 100.0 * (
        0.35 * earliest_ownership
        + 0.20 * coverage
        + 0.20 * serial_depth
        + 0.20 * replaced_scope
        + 0.05 * transaction_scope
    )
    return {
        "structural_leverage_score": round(score, 6),
        "owns_earliest_live_wall": earliest == 1,
        "earliest_wall_ownership_signal": round(earliest_ownership, 6),
        "sampled_wall_coverage_signal": round(coverage, 6),
        "serial_depth_signal": round(serial_depth, 6),
        "replacement_scope_signal": round(replaced_scope, 6),
        "transaction_scope_signal": round(transaction_scope, 6),
    }


def _auxiliary_entry(
    raw: Mapping[str, Any], *, seed_artifact_sha256: str
) -> dict[str, Any]:
    action = raw.get("action")
    if not isinstance(action, Mapping):
        raise ActionMenuError("ACTION_MENU_AUXILIARY_ACTION_REQUIRED")
    kind = str(raw.get("kind") or "")
    if kind not in {"probe", "physical_recipe", "proof_gated_mutation"}:
        raise ActionMenuError("ACTION_MENU_INVALID_AUXILIARY_KIND", kind)
    if kind == "probe" and action.get("action") != "probe":
        raise ActionMenuError("ACTION_MENU_PROBE_ACTION_MISMATCH")
    if kind in {"physical_recipe", "proof_gated_mutation"} and action.get(
        "action"
    ) != "run_skill":
        raise ActionMenuError("ACTION_MENU_RECIPE_ACTION_MISMATCH")
    target = str(raw.get("action_target") or "")
    if not target:
        raise ActionMenuError("ACTION_MENU_INVALID_ACTION_TARGET")
    legality_digest = _sha(raw.get("legality_digest"), field="legality_digest")
    parameter_digest = _digest(dict(action))
    family = _digest({"auxiliary_kind": kind, "action_target": target})
    display = raw.get("display") if isinstance(raw.get("display"), Mapping) else {}
    if kind == "proof_gated_mutation":
        # A proof-gated mutation is perishable in exactly the way a packet
        # mutation is: the structure it recovers exists on this seed only, and
        # a destructive action taken first can remove it or spend the window it
        # needed.  Score it with the same measured formula so the scheduler's
        # perishability rule reaches this class too, instead of leaving these
        # cards without the facts that rule reads.
        leverage = _proof_gated_structural_leverage(display)
        display = {
            **display,
            **leverage,
            "opportunity_decay_risk": (
                "high" if leverage["owns_earliest_live_wall"] is True else "bounded"
            ),
        }
    return _make_entry(
        kind=kind,
        eligible=True,
        seed_artifact_sha256=seed_artifact_sha256,
        action_target=target,
        family_fingerprint=family,
        recovered_plan_digest=parameter_digest,
        proof_digest=legality_digest,
        proof_status=(
            "executor_proof_required"
            if kind == "proof_gated_mutation"
            else "prevalidated_legality" if kind == "physical_recipe"
            else "read_only"
        ),
        authorization_class=(
            "current_seed_fail_closed_executor_gate"
            if kind == "proof_gated_mutation"
            else "prevalidated_physical_legality" if kind == "physical_recipe"
            else "registered_read_only_probe"
        ),
        action=action,
        display=display,
        blockers=[],
    )


def _proof_gated_structural_leverage(display: Mapping[str, Any]) -> dict[str, Any]:
    """Score a proof-gated card from the live evidence it already publishes.

    These cards carry sampled wall coverage and covered path count but not the
    packet's rank, depth, or replacement-scope columns.  The shared formula
    already scores an absent column as zero, so the unmeasured signals stay
    zero rather than being invented.
    """
    evidence = display.get("live_evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    covered_raw = evidence.get("live_wall_covered_path_count")
    covered = (
        int(covered_raw)
        if not isinstance(covered_raw, bool)
        and isinstance(covered_raw, int)
        and covered_raw > 0
        else 0
    )
    coverage = evidence.get("live_wall_coverage")
    coverage_ppm = None
    resolved = None
    if (
        not isinstance(coverage, bool)
        and isinstance(coverage, (int, float))
        and math.isfinite(float(coverage))
        and 0.0 < float(coverage) <= 1.0
    ):
        coverage_ppm = int(round(float(coverage) * 1_000_000))
        # The card publishes the covered count and the fraction it represents,
        # so the sampled wall it was measured against is their quotient.  The
        # shared formula treats an absent wall size as "covers everything",
        # which would let a card claim the earliest rank it has no evidence
        # for, so without both numbers this card claims nothing.
        if covered:
            resolved = int(round(covered / float(coverage)))
    if resolved is None:
        covered_raw = 0
    return _operator_structural_leverage({
        "live_wall_covered_path_count": covered_raw,
        "live_wall_resolved_path_count": resolved,
        "live_wall_coverage_ppm": coverage_ppm,
        "earliest_covered_rank": evidence.get("earliest_covered_rank"),
        "serial_depth": evidence.get("serial_depth"),
        "expected_replaced_instance_count": evidence.get(
            "expected_replaced_instance_count"
        ),
        "transaction_scope_count": evidence.get("transaction_scope_count"),
    })


def _adaptive_global_limit(
    entries: list[dict[str, Any]], *, configured_top_k: int
) -> int:
    """Apply one family-diverse mutation bound after all families compose.

    Probes and save/stop remain outside the mutation-card bound.  Expanding an
    uncertain menu changes visibility only; it never changes an entry's proof,
    parameters, executor readiness, or mutation authorization.
    """
    candidates = [
        entry for entry in entries
        if entry.get("eligible") is True
        and entry.get("kind") in {
            "mutation", "physical_recipe", "proof_gated_mutation"
        }
    ]
    any_trusted = any(
        (entry.get("display") or {}).get("decision_confidence") == "trusted"
        for entry in candidates
    )
    # An uncertain menu needs enough width to expose fundamentally different
    # implementation mechanisms.  Eight cards was too narrow once operator,
    # routed-physical, placement, path-local, and fresh-implementation actions
    # were composed together: whole mechanism classes disappeared before the
    # decision model could compare them.
    effective_top_k = min(4, configured_top_k) if any_trusted else max(12, configured_top_k)
    effective_top_k = max(1, min(16, effective_top_k))

    def rank(entry: Mapping[str, Any]) -> tuple[float, int, str, str]:
        display = entry.get("display") if isinstance(entry.get("display"), Mapping) else {}
        raw_score = display.get("ranking_score", 0.0)
        score = (
            float(raw_score)
            if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
            else 0.0
        )
        confidence = {"trusted": 2, "uncertain": 1, "untrusted": 0}.get(
            str(display.get("decision_confidence") or "untrusted"), 0
        )
        return (-score, -confidence, str(entry["action_target"]), str(entry["entry_id"]))

    ordered = sorted(candidates, key=rank)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_mechanisms: set[tuple[str, str, str]] = set()

    def family(entry: Mapping[str, Any]) -> str:
        display = entry.get("display") if isinstance(entry.get("display"), Mapping) else {}
        return str(display.get("action_family") or entry.get("family_fingerprint") or "")

    def coverage_bucket(entry: Mapping[str, Any]) -> str:
        """Return generic mechanism coverage owed by an uncertain menu.

        These buckets contain no benchmark identity or executable parameter.
        They only prevent the global score from hiding all locality guidance,
        all fresh implementation, or all path-local logic alternatives.
        """
        display = entry.get("display") if isinstance(entry.get("display"), Mapping) else {}
        action = entry.get("action") if isinstance(entry.get("action"), Mapping) else {}
        skill = str(display.get("skill") or action.get("skill") or "")
        action_family = family(entry)
        if skill == "pblock":
            return "placement_guidance"
        if skill == "pblock_sweep":
            return "placement_sweep"
        if skill == "fresh_place_route":
            return "fresh_place_route"
        if action_family == "fanout_replication" and skill == "phys_opt":
            return "broad_fanout"
        # `phys_opt -directive AggressiveFanoutOpt` is a global directive and
        # was the only card the fanout bucket could hold, so the mechanism that
        # replicates specific certified critical nets and repairs their routing
        # never reserved anything and fell out of top-K.  On
        # vexriscv_re-place_v2 one net owns 87.5% of the live wall, the card
        # survives the window filter and the reserve at 113.61 needing 1,562s
        # of a 2,642s window, and top-K is what removes it -- while the
        # recorded 424.4 MHz chain is a fresh place and route to 411.5 followed
        # by exactly two of these replications.
        if action_family == "fanout_replication" and skill in {
            "force_replicate", "fanout_opt"
        }:
            return "critical_net_replication"
        if action_family in {"path_local_logic", "path_local_lut_physical"}:
            return "path_local_logic"
        return ""

    def skill(entry: Mapping[str, Any]) -> str:
        display = entry.get("display") if isinstance(entry.get("display"), Mapping) else {}
        action = entry.get("action") if isinstance(entry.get("action"), Mapping) else {}
        return str(display.get("skill") or action.get("skill") or "")

    def mechanism(entry: Mapping[str, Any]) -> tuple[str, str, str]:
        """Identify independently useful mechanisms without private params."""
        display = entry.get("display") if isinstance(entry.get("display"), Mapping) else {}
        semantic_identity = str(display.get("expected_mechanism") or "")
        if (
            family(entry) == "operator_rewrite"
            and display.get("family_kind")
        ):
            # Independently recovered current-seed transactions are separate
            # alternatives even when one executor implements all of them.
            semantic_identity = str(entry.get("family_fingerprint") or "")
        return (
            skill(entry),
            family(entry),
            semantic_identity,
        )

    def record_selection(entry: dict[str, Any]) -> None:
        entry_id = str(entry["entry_id"])
        action_mechanism = mechanism(entry)
        selected.append(entry)
        selected_ids.add(entry_id)
        if action_mechanism[0]:
            selected_mechanisms.add(action_mechanism)

    def complete_semantic_mechanism(entry: Mapping[str, Any]) -> bool:
        """Return whether one current-seed semantic mechanism is decision-ready.

        A small live-wall path count must not hide a fully recovered semantic
        bottleneck behind generic physical cards.  This reserves visibility
        only; the Decision model still compares QoR, risk, and runtime and may
        select another authorized mechanism.

        A consumer contract is recovered from the current seed and is complete
        over the cone it owns, so it qualifies on the same terms.  Matching one
        scope literal cut the consumer-driven rewrite out of this pass on
        vtr_mcml_v2: it was eligible, ranked thirteenth against a top_k of
        twelve, and lost its slot to a repeated phys_opt regime.
        """
        if entry.get("kind") == "mutation":
            return True
        if entry.get("kind") != "proof_gated_mutation":
            return False
        display = entry.get("display")
        evidence = (
            display.get("live_evidence") if isinstance(display, Mapping) else None
        )
        return bool(
            isinstance(evidence, Mapping)
            and evidence.get("complete_scope") in _COMPLETE_SEMANTIC_SCOPES
        )

    # Exact current-seed semantic mechanisms are independent alternatives, not
    # variants of one operator skill.  Keep one card for every complete
    # mechanism visible before scalar score filling so a narrow but dominant
    # wall is still compared by the Decision model.
    for entry in ordered:
        if len(selected) >= effective_top_k:
            break
        if not complete_semantic_mechanism(entry):
            continue
        action_mechanism = mechanism(entry)
        if (
            str(entry["entry_id"]) not in selected_ids
            and action_mechanism not in selected_mechanisms
        ):
            record_selection(entry)

    # Reserve one card for each broad, currently authorized search mechanism
    # before ordinary family-diverse score filling.  The strongest live card in
    # each bucket wins; no action or parameters are synthesized here.
    if not any_trusted:
        for bucket in (
            "placement_guidance",
            "placement_sweep",
            "broad_fanout",
            "critical_net_replication",
            "fresh_place_route",
            "path_local_logic",
        ):
            entry = next(
                (item for item in ordered if coverage_bucket(item) == bucket),
                None,
            )
            if entry is None or len(selected) >= effective_top_k:
                continue
            record_selection(entry)

    # Show the strongest card from each independently useful mechanism before
    # adding alternate regimes. One executor can implement several different
    # QoR mechanisms, so skill-name diversity alone is too coarse.
    for entry in ordered:
        if len(selected) >= effective_top_k:
            break
        entry_id = str(entry["entry_id"])
        action_mechanism = mechanism(entry)
        if (
            entry_id in selected_ids
            or not action_mechanism[0]
            or action_mechanism in selected_mechanisms
        ):
            continue
        record_selection(entry)

    # If the menu still has room, fill it by live score.  This preserves useful
    # alternate regimes only after distinct executable mechanics are visible.
    for entry in ordered:
        if len(selected) >= effective_top_k:
            break
        if str(entry["entry_id"]) not in selected_ids:
            record_selection(entry)

    for entry in candidates:
        if entry["entry_id"] in selected_ids:
            continue
        entry["eligible"] = False
        entry["action"] = None
        entry["authorization_class"] = "adaptive_global_ranking_excluded"
        entry["blockers"] = list(dict.fromkeys([
            *entry.get("blockers", []),
            "OUTSIDE_ADAPTIVE_GLOBAL_TOP_K",
        ]))
        entry["display"] = {
            **entry.get("display", {}),
            "ranking_state": "not_displayed_this_epoch",
            "effective_top_k": effective_top_k,
        }
        if entry.get("kind") == "mutation":
            entry["kind"] = "blocked_family"
    return effective_top_k


# Every scope a card builder emits for a complete current-seed semantic
# contract.  A builder that adds a scope without adding it here loses the
# reserved menu slot silently.
_COMPLETE_SEMANTIC_SCOPES = frozenset({
    "current_seed_complete_contract",
    "current_seed_consumer_contract",
})


def build_action_menu(
    *,
    seed_candidate_id: str,
    seed_artifact_sha256: str,
    registered_arithmetic_packet: Mapping[str, Any] | None,
    proof_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    top_k: int = DEFAULT_TOP_K,
    auxiliary_actions: Sequence[Mapping[str, Any]] = (),
    include_save_and_exit: bool = True,
    cold_isolation: bool = False,
    apply_adaptive_limit: bool = True,
) -> dict[str, Any]:
    """Build a deterministic menu from current-seed tool evidence.

    ``proof_bindings`` contains exact same-run proof receipts keyed by family
    fingerprint. Foreign-run and historical packets are never accepted as
    runtime authority.
    """
    seed_sha = _sha(seed_artifact_sha256, field="seed_artifact_sha256")
    if not isinstance(seed_candidate_id, str) or not seed_candidate_id:
        raise ActionMenuError("ACTION_MENU_SEED_CANDIDATE_REQUIRED")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 16:
        raise ActionMenuError("ACTION_MENU_TOP_K_OUT_OF_RANGE")
    packet = registered_arithmetic_packet or {}
    packet_source = packet.get("source_artifact_sha256")
    if packet and packet_source != seed_sha:
        raise ActionMenuError("ACTION_MENU_STALE_PACKET")
    packet_digest = str(packet.get("result_digest") or "")
    bindings = proof_bindings or {}
    entries: list[dict[str, Any]] = []
    rows = _family_rows(packet)
    for family_priority_ordinal, row in enumerate(rows, 1):
        structural_leverage = _operator_structural_leverage(row)
        fingerprint = str(row["family_fingerprint"])
        binding = bindings.get(fingerprint)
        binding = dict(binding) if isinstance(binding, Mapping) else None
        packet_proved = (
            row.get("nomination_eligible") is True
            and row.get("proof_status") == "proved"
            and _SHA256.fullmatch(str(row.get("plan_digest") or "")) is not None
            and _SHA256.fullmatch(packet_digest) is not None
        )
        binding_proved = bool(
            binding
            and binding.get("status") == "proved"
            and _SHA256.fullmatch(str(binding.get("recovered_plan_digest") or ""))
            and _SHA256.fullmatch(str(binding.get("authorization_digest") or ""))
        )
        fixed_template_admitted = bool(
            row.get("family_kind") != "fixed_product"
            or (
                binding
                and binding.get("execution_backend_status") == "admitted"
                and binding.get("execution_backend")
                == "fixed_point_dsp_rebuild_v1"
                and _SHA256.fullmatch(str(
                    binding.get("execution_backend_authorization_digest") or ""
                ))
            )
        )
        eligible = bool(
            row.get("nomination_eligible") is True
            and (binding_proved or packet_proved)
            and fixed_template_admitted
        )
        plan_digest = (
            str(binding["recovered_plan_digest"])
            if binding_proved else str(row.get("plan_digest") or "")
        )
        if _SHA256.fullmatch(plan_digest) is None:
            plan_digest = object_digest({
                "status": "contract_not_recovered",
                "family_fingerprint": fingerprint,
            })
        proof_digest = (
            str(binding["authorization_digest"])
            if binding_proved else object_digest({
                "binding": "validated_registered_arithmetic_packet",
                "packet_result_digest": packet_digest,
                "family_fingerprint": fingerprint,
                "proof_template_id": row.get("proof_template_id"),
                "proof_status": row.get("proof_status"),
                "plan_digest": row.get("plan_digest"),
            })
        )
        blockers = list(row.get("nomination_blockers") or [])
        if not fixed_template_admitted:
            blockers.append("FIXED_POINT_EXECUTION_TEMPLATE_UNADMITTED")
        if not eligible and not blockers:
            blockers.append("EXACT_PROOF_BINDING_UNAVAILABLE")
        recurrence_transport = row.get("family_kind") == "recurrence_transport"
        canonical_target = (
            "dsp_srl_recurrence_cut_v1"
            if recurrence_transport else "registered_arithmetic_cone_v1"
        )
        action_params = {"family_fingerprint": fingerprint}
        entries.append(_make_entry(
            kind="mutation" if eligible else "blocked_family",
            eligible=eligible,
            seed_artifact_sha256=seed_sha,
            action_target=f"operator_rewrite:{canonical_target}",
            family_fingerprint=fingerprint,
            recovered_plan_digest=plan_digest,
            proof_digest=proof_digest,
            proof_status="proved" if eligible else str(row.get("proof_status") or "unavailable"),
            authorization_class=(
                "eager_exact_proof_receipt" if binding_proved
                else "validated_packet_exact_proof_binding" if eligible
                else "not_authorized"
            ),
            action=(
                {
                    "action": "run_skill",
                    "skill": "operator_rewrite",
                    "target": canonical_target,
                    "params": action_params,
                }
                if eligible else None
            ),
            display={
                "action_family": "operator_rewrite",
                "skill": "operator_rewrite",
                "target": canonical_target,
                "decision_confidence": "trusted" if eligible else "untrusted",
                "ranking_score": round(
                    170.0
                    + float(structural_leverage["structural_leverage_score"])
                    - 3.0 * min(family_priority_ordinal - 1, 12),
                    6,
                ),
                "why_now": (
                    "exact current-seed family proof covers the live timing wall; "
                    "compare earliest covered rank, complete transaction scope, "
                    "then sampled coverage"
                ),
                "family_kind": row.get("family_kind"),
                "family_alias": row.get("family_alias"),
                # A family that fails closed records why, but the reason stayed
                # in the preflight artifact.  3d-rendering's highest-ranked
                # real family -- a product_sum at 210.0 covering the whole live
                # wall, above its 195.4 physical top -- reached the planner as
                # UNTYPED_RUNTIMEERROR alone, and naming the actual exception
                # meant reading a file the next round deletes.
                "typed_rejection": copy.deepcopy(row.get("typed_rejection"))
                if isinstance(row.get("typed_rejection"), Mapping) else None,
                "live_wall_coverage_ppm": row.get("live_wall_coverage_ppm"),
                "live_wall_covered_path_count": row.get("live_wall_covered_path_count"),
                "earliest_covered_rank": row.get("earliest_covered_rank"),
                "unresolved_earlier_path_count": row.get(
                    "unresolved_earlier_path_count"
                ),
                "transaction_scope_count": row.get("transaction_scope_count"),
                "expected_replaced_instance_count": row.get(
                    "expected_replaced_instance_count"
                ),
                "serial_depth": row.get("serial_depth"),
                **structural_leverage,
                "opportunity_decay_risk": (
                    "high"
                    if eligible
                    and structural_leverage["owns_earliest_live_wall"] is True
                    else "bounded"
                ),
                "family_priority_ordinal": family_priority_ordinal,
                "family_ranking_policy": (
                    "earliest_rank_then_complete_transaction_scope_then_live_coverage"
                ),
                "expected_mechanism": (
                    "replace the exact recurrence transport family with its "
                    "dedicated proved backend while preserving cycle timing"
                    if recurrence_transport else
                    "replace the exact registered product-sum family with its "
                    "proved production lowering while preserving cycle timing"
                ),
                "expected_wall_effect": (
                    "remove the proved registered arithmetic family from its "
                    "currently covered timing paths"
                ),
                "expected_next_wall": "must be re-profiled after central measurement",
                "parameter_policy": [
                    "exact_current_run_contract",
                    "proof_owned_parameters",
                    "scheduler_owned_timeout",
                ],
                "risk": "high",
                "estimated_runtime_s": 1200.0,
                "resource_request": {
                    "cpu_cores": 8,
                    "memory_gib": 28.0,
                    "exclusive_session": "rapidwright+vivado",
                },
                "destructive_scope": "proved_netlist_mutation",
                "logical_structure_effect": "replaces_proved_semantic_transaction",
                "proof_boundary": "executor_must_revalidate_before_candidate_emission",
                "exact_parameters_private": True,
                "proof_template_id": row.get("proof_template_id"),
            },
            blockers=blockers if not eligible else [],
        ))
    for raw in auxiliary_actions:
        action = raw.get("action") if isinstance(raw, Mapping) else None
        if (
            cold_isolation
            and isinstance(action, Mapping)
            and action.get("action") == "run_skill"
            and action.get("skill") == "semantic_replay"
        ):
            continue
        entries.append(_auxiliary_entry(raw, seed_artifact_sha256=seed_sha))
    effective_top_k = (
        _adaptive_global_limit(entries, configured_top_k=top_k)
        if apply_adaptive_limit else max(1, min(16, int(top_k)))
    )
    if include_save_and_exit:
        target = "agent:save_and_exit"
        family = _digest({"sentinel": target})
        plan = _digest({"action": "save_and_exit"})
        proof = _digest({"authority": "incumbent_preservation"})
        entries.append(_make_entry(
            kind="save_and_exit",
            eligible=True,
            seed_artifact_sha256=seed_sha,
            action_target=target,
            family_fingerprint=family,
            recovered_plan_digest=plan,
            proof_digest=proof,
            proof_status="not_applicable_incumbent_preservation",
            authorization_class="mechanical_incumbent_preservation",
            action={
                "action": "save_and_exit",
                "stop_reason": "menu_selected_save_and_exit",
            },
            display={
                "expected_mechanism": "preserve the immutable incumbent and stop"
            },
            blockers=[],
        ))
    entries.sort(key=lambda item: (
        not item["eligible"],
        item["kind"] == "save_and_exit",
        item["action_target"],
        item["entry_id"],
    ))
    body = {
        "schema_version": SCHEMA_VERSION,
        "authority": "tool_built_seed_bound_action_authorization",
        "seed_candidate_id": seed_candidate_id,
        "seed_artifact_sha256": seed_sha,
        "top_k": effective_top_k,
        "entries": entries,
        "eligible_entry_ids": [item["entry_id"] for item in entries if item["eligible"]],
        "blocked_entry_ids": [item["entry_id"] for item in entries if not item["eligible"]],
        "parameter_authority": "entry_only_no_llm_parameter_authority",
        "promotion_authority": False,
        "replay_derived_actions_forbidden": bool(cold_isolation),
    }
    return {**body, "menu_digest": _digest(body)}


def build_degraded_action_menu(
    *,
    seed_candidate_id: str,
    seed_artifact_sha256: str,
    error_code: str,
    error_detail: str = "",
    top_k: int = DEFAULT_TOP_K,
    auxiliary_actions: Sequence[Mapping[str, Any]] = (),
    cold_isolation: bool = False,
) -> dict[str, Any]:
    """Publish a selectable fail-closed menu when epoch recovery is unavailable."""
    code = str(error_code or "ACTION_MENU_BUILD_FAILED").strip().upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", code) is None:
        code = "ACTION_MENU_BUILD_FAILED"
    fingerprint = object_digest({
        "kind": "degraded_action_menu_error",
        "seed_artifact_sha256": seed_artifact_sha256,
        "error_code": code,
        "error_detail": str(error_detail)[:500],
    })
    packet_body = {
        "source_artifact_sha256": seed_artifact_sha256,
        "normalized_siblings": [{
            "family_kind": "product_sum",
            "family_alias": "degraded_product_sum_builder",
            "family_fingerprint": fingerprint,
            "live_wall_coverage_ppm": 0,
            "live_wall_covered_path_count": 0,
            "earliest_covered_rank": "unknown",
            "unresolved_earlier_path_count": "unknown",
            "transaction_scope_count": "unknown",
            "expected_replaced_instance_count": "unknown",
            "nomination_eligible": False,
            "nomination_blockers": [code],
            "proof_status": "unavailable",
            "proof_template_id": "unknown",
            "plan_digest": "unknown",
        }],
    }
    packet = {**packet_body, "result_digest": object_digest(packet_body)}
    return build_action_menu(
        seed_candidate_id=seed_candidate_id,
        seed_artifact_sha256=seed_artifact_sha256,
        registered_arithmetic_packet=packet,
        top_k=top_k,
        auxiliary_actions=auxiliary_actions,
        cold_isolation=cold_isolation,
    )


def _runtime_rank_charge(seconds: float) -> float:
    """Return the runtime penalty `ranking_score` already applies."""
    return min(30.0, max(0.0, float(seconds)) / 120.0)


def _rescore_for_measured_runtime(
    display: Mapping[str, Any],
    *,
    card_estimate_s: object,
    source: str,
) -> dict[str, Any] | None:
    """Recharge runtime in the score using the better-grounded estimate.

    Only the difference is applied, on the same terms the original formula
    used, so a card whose constant already described its cost is untouched.
    Both a same-run measurement and a calibrated per-skill platform figure
    describe the action better than a family-wide contract constant; a card
    still resting on that constant is left alone.
    """
    if source not in {"same_run_measurement", "platform_configuration"}:
        return None
    measured = display.get("selected_runtime_estimate_s")
    score = display.get("ranking_score")
    for value in (measured, card_estimate_s, score):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
    extra = _runtime_rank_charge(float(measured)) - _runtime_rank_charge(
        float(card_estimate_s)
    )
    if extra <= 0.0:
        return None
    return {
        **display,
        "ranking_score": round(float(score) - extra, 6),
        "ranking_runtime_recharge": round(extra, 6),
    }


def filter_action_menu_for_execution_window(
    value: object,
    *,
    execution_window_s: float,
    runtime_estimates_s: Mapping[str, Any] | None = None,
    central_measurement_estimate_s: float = 0.0,
    candidate_commit_reserve_s: float = 0.0,
    measured_runtimes_s: Mapping[str, Any] | None = None,
    measured_full_implementation_s: float | None = None,
) -> dict[str, Any]:
    """Remove mechanically unlaunchable entries before LLM arbitration.

    Time only decreases within an epoch, so this fail-closed projection may be
    installed as the current menu.  It never makes an entry eligible and keeps
    the original entry identity while removing action authority.
    """
    for value_s, code in (
        (execution_window_s, "ACTION_MENU_INVALID_EXECUTION_WINDOW"),
        (
            central_measurement_estimate_s,
            "ACTION_MENU_INVALID_CENTRAL_MEASUREMENT_ESTIMATE",
        ),
        (
            candidate_commit_reserve_s,
            "ACTION_MENU_INVALID_CANDIDATE_COMMIT_RESERVE",
        ),
    ):
        if (
            isinstance(value_s, bool)
            or not isinstance(value_s, (int, float))
            or not math.isfinite(float(value_s))
            or float(value_s) < 0.0
        ):
            raise ActionMenuError(code)
    menu = validate_action_menu(value)
    changed = False
    for entry in menu["entries"]:
        if entry.get("eligible") is not True:
            continue
        action = entry.get("action")
        requirement = action_execution_window_requirement(
            action=action,
            display=entry.get("display"),
            runtime_estimates_s=runtime_estimates_s,
            central_measurement_estimate_s=central_measurement_estimate_s,
            candidate_commit_reserve_s=candidate_commit_reserve_s,
            measured_runtimes_s=measured_runtimes_s,
            measured_full_implementation_s=measured_full_implementation_s,
        )
        if requirement is None:
            continue
        required = float(requirement["required_execution_window_s"])
        # Publish the exact estimate used by the mechanical gate even when the
        # action fits.  Previously the model saw the Action Card's generic
        # estimate while Python enforced a different platform estimate.
        updated_display = {
            **entry.get("display", {}),
            **requirement,
            "available_execution_window_s": float(execution_window_s),
        }
        # `ranking_score` charges runtime as `min(30, estimate/120)` using the
        # contract's constant.  Where this run has measured that the action
        # costs more, charge the difference on the same terms, so an action
        # that would spend most of the hour is ranked against one that spends
        # a few minutes rather than against its own optimistic constant.
        rescored = _rescore_for_measured_runtime(
            updated_display,
            card_estimate_s=(entry.get("display") or {}).get("estimated_runtime_s"),
            source=str(requirement.get("runtime_estimate_source") or ""),
        )
        if rescored is not None:
            updated_display = rescored
        if updated_display != entry.get("display"):
            entry["display"] = updated_display
            changed = True
        if required <= float(execution_window_s):
            continue
        changed = True
        entry["eligible"] = False
        entry["action"] = None
        entry["authorization_class"] = "execution_window_unavailable"
        entry["blockers"] = list(dict.fromkeys([
            *entry.get("blockers", []),
            "EXECUTION_WINDOW_UNAVAILABLE",
        ]))
    if not changed:
        return menu
    body = {key: value for key, value in menu.items() if key != "menu_digest"}
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if not item["eligible"]
    ]
    projected = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        projected, expected_seed_sha256=projected["seed_artifact_sha256"]
    )


def _unattempted_complete_semantic_mechanism(entry: Mapping[str, Any]) -> bool:
    """Return whether this entry is a semantic contract the run has not tried.

    A complete current-seed contract is the one kind of entry whose value the
    physical cards cannot substitute for, and `attempt_count` says whether this
    run has measured it.
    """
    display = entry.get("display")
    if not isinstance(display, Mapping):
        return False
    evidence = display.get("live_evidence")
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("complete_scope") not in _COMPLETE_SEMANTIC_SCOPES
    ):
        return False
    # An operator card publishes an empty `same_run_feedback` on the round the
    # run has measured nothing, not `{"attempt_count": 0}`.  Reading only the
    # explicit zero meant this never once fired on a first round, which is the
    # only round vtr_mcml_v2's consumer rewrite can run.
    feedback = display.get("same_run_feedback")
    attempts = feedback.get("attempt_count") if isinstance(feedback, Mapping) else None
    return attempts is None or attempts == 0


def reserve_top_ranked_launch_opportunity(
    value: object,
    *,
    execution_window_s: float,
    same_run_feedback_available: bool = True,
) -> dict[str, Any]:
    """Keep a below-rank selection from foreclosing the top-ranked mechanism.

    `ranking_score` is the run's calibrated estimate of expected Fmax gain, and
    arbitration below it is worth keeping: the score cannot see everything, and
    a cheap alternative that fails fast costs the run almost nothing.  What
    makes a deviation expensive is not how far below the top it sits but
    whether it can be undone.  Measured over one round-0 sample per run: a
    59.12-point deviation into an entry that refused in 19 s of a 2,638 s
    window left the top entry to run afterwards and the round still gained;
    a 13.61-point and a 14.50-point deviation each consumed about 2,300 s of an
    about 2,400 s window, returned nothing, and ended their runs, costing
    23.16 MHz on one and the whole round on the other.  Score distance did not
    separate those outcomes; foreclosure did.

    So an entry that is not top-ranked keeps its authority only while the
    window can still hold the top-ranked entry after it.  Both requirements
    already carry their own measurement and commit reserve, which is what a
    run pays when it executes two actions rather than one.

    This is the selection-time form of the reservation that ranking order
    cannot express.  Reordering the menu to launch a perishable mechanism
    first answers a different question -- which action to prefer -- and
    answering it there displaced the physical actions that deliver.  The
    constraint belongs on the choice, not on the preference.

    Before the run has measured anything, `same_run_feedback_available` is
    False and the top-ranked entry is the only one left standing.  That is not
    a stricter version of the rules above but a different argument: the score
    is computed from the seed profile, and on the first round that profile is
    the whole of what anyone knows.  Deviating from it asserts information the
    score omitted, and there is none yet.  It also does not depend on an
    estimate, which is what the two rules above do depend on --
    path_local_lut_reflow declares 835s and spent 2,273s, fresh_place_route
    declares 1,635s and spent 2,309s, and each of those overruns cost a run.
    Every first-round measurement on record agrees: the top entry produced
    +23.16 on boom_soc, +18.06 on fir, +4.22 on vtr_mcml_v2, and the
    120.613 MHz path on ispd16.
    """
    if (
        isinstance(execution_window_s, bool)
        or not isinstance(execution_window_s, (int, float))
        or not math.isfinite(float(execution_window_s))
        or float(execution_window_s) < 0.0
    ):
        raise ActionMenuError("ACTION_MENU_INVALID_EXECUTION_WINDOW")
    menu = validate_action_menu(value)

    def required_s(entry: Mapping[str, Any]) -> float | None:
        display = entry.get("display")
        if not isinstance(display, Mapping):
            return None
        value_s = display.get("required_execution_window_s")
        if isinstance(value_s, bool) or not isinstance(value_s, (int, float)):
            return None
        return float(value_s) if math.isfinite(float(value_s)) else None

    def score(entry: Mapping[str, Any]) -> float:
        display = entry.get("display")
        raw = display.get("ranking_score") if isinstance(display, Mapping) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0.0
        return float(raw) if math.isfinite(float(raw)) else 0.0

    eligible = [entry for entry in menu["entries"] if entry.get("eligible") is True]
    if len(eligible) < 2:
        return menu
    # Forcing a below-rank consumer contract to the front was tried and
    # measured.  vtr_mcml_v2's rewrite is only affordable on round zero -- its
    # `proved_netlist_mutation` scope has the window filter charge it a
    # measured full implementation pass from round 1 -- so it was reserved for
    # there ahead of a clock tighten scoring 24.76 higher.  It ran, and its
    # candidate came back at WNS -13.839 against the seed's -12.885: 65.05 MHz
    # against 69.33, so the score's preference for the clock tighten, which
    # measures +3.18 there, was right.  Rank stands.
    # vexriscv_re-place reaches 449.84 MHz from a clock tighten worth +66.4 and
    # its v2 sibling is the same core re-placed, so v2's card at 119.15 looked
    # like the peaked occupancy term misreading 0.68% utilization.  Reserved
    # for round zero once to measure it: 392.6 MHz against the 397.5 seed,
    # equivalent WNS -0.946 to -0.977, not promoted.  v2 is already a re-placed
    # checkpoint 3.8% from closure where v1 started far looser, and the score
    # was right.  Rank stands.
    # The recorded 424.4 MHz chain for vexriscv_re-place_v2 is a fresh place
    # and route to 411.5 then two critical-net force_replicate, and the
    # replication had never run: the card scores 136.56 on a seed where one net
    # owns 87.5% of the live wall, needs 1,735s, and the reserve withdraws it
    # for foreclosing the top entry.  Reserved for and measured once: it
    # replicated one net under AggressiveExplore and moved WNS from -0.946 to
    # -0.954, not promoted.  Rank stands.
    top = max(eligible, key=score)
    top_required = required_s(top)
    if top_required is None:
        # Without a cost for the mechanism being reserved there is nothing to
        # reserve against; leave arbitration exactly as the window filter left it.
        return menu
    top_score = score(top)
    changed = False
    for entry in eligible:
        if entry["entry_id"] == top["entry_id"]:
            continue
        # Reserve only for a strictly better option.  An entry the score rates
        # as high as the top forecloses nothing by running -- fir's second
        # round offers the same reroute mechanism twice at an identical score,
        # and dropping one of those would shrink the menu to protect a choice
        # the score cannot distinguish from it.
        # An entry the score rates as high as the reserved one forecloses
        # nothing by running -- unless the reserved one is a consumer contract,
        # which has exactly this round to run and is reserved for despite
        # ranking below.  Skipping those left vtr_mcml_v2's clock tighten at
        # 228.67 in front of the rewrite at 203.91 and the round went to it
        # again.
        if score(entry) >= top_score:
            continue
        entry_required = required_s(entry)
        if entry_required is None:
            continue
        # Once the run has its own measurements, the only reason left to
        # withdraw a below-rank entry is that the window cannot hold both --
        # running it would foreclose the better option outright.
        #
        # Withdrawing every below-rank rebuild as well was redundant on the
        # first round, where the rule above already takes the top entry, and
        # harmful afterwards: on vexriscv_re-place_v2 it withheld
        # clock_tighten at 141.6 for all eighteen rounds, and clock_tighten is
        # what took its sibling vexriscv_re-place from 383 to 466 MHz.
        # The first-round argument is about the mechanism, not the clock: the
        # score is computed from the seed profile, and deviating from it
        # asserts information the score omitted.  For a complete semantic
        # contract the run has not attempted, there is still none -- feedback
        # about other families says nothing about this one.  vtr_mcml_v2's
        # consumer-driven rewrite ranked first at 207.11 on its second round
        # with `attempt_count` 0, and the round went to a fanout phys_opt 65
        # points below it that measured +0.010; the same thing happened on the
        # two rounds after that.  Its family is the one the offline record
        # rides from 69.3 to 138.4 MHz, and it costs 300s.
        if (
            same_run_feedback_available
            and not _unattempted_complete_semantic_mechanism(top)
            and entry_required + top_required <= float(execution_window_s)
        ):
            continue
        changed = True
        entry["display"] = {
            **entry["display"],
            "reserved_for_entry_id": str(top["entry_id"]),
            "reserved_for_required_execution_window_s": round(top_required, 3),
        }
        entry["eligible"] = False
        entry["action"] = None
        entry["authorization_class"] = "top_ranked_launch_opportunity_reserved"
        entry["blockers"] = list(dict.fromkeys([
            *entry.get("blockers", []),
            "TOP_RANKED_LAUNCH_OPPORTUNITY_RESERVED",
        ]))
    if not changed:
        return menu
    body = {key: value for key, value in menu.items() if key != "menu_digest"}
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if not item["eligible"]
    ]
    projected = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        projected, expected_seed_sha256=projected["seed_artifact_sha256"]
    )


def _semantic_opportunity_untaken(display: Mapping[str, Any]) -> bool:
    """Return whether this run has yet to attempt this mechanism.

    Perishability is about an opportunity the run has not taken.  Once the
    mechanism has run on this seed or on a direct ancestor, the run has had
    its shot at it, and holding every rebuild behind a repeat of it starves
    the rest of the menu for the remainder of the run.
    """
    feedback = display.get("same_run_feedback")
    if not isinstance(feedback, Mapping):
        return True
    for key in ("attempt_count", "lineage_attempt_count"):
        value = feedback.get(key)
        if (
            not isinstance(value, bool)
            and isinstance(value, int)
            and value > 0
        ):
            return False
    return True


def prioritize_last_launch_opportunity(
    value: object,
    *,
    lookahead_reserve_s: float = 0.0,
) -> dict[str, Any]:
    """Protect proved semantic work that a destructive action can erase.

    This is an action-DAG dominance rule, not a benchmark recipe: an exact
    current-seed transaction that owns the earliest live wall is attempted
    before an action that may destroy its recovery structure.  Route-only
    refinements remain comparable, and a failed/exhausted semantic action no
    longer protects the seed on the next projection.

    A card whose proof runs inside its executor is protected on the same
    terms.  Where the proof is discharged does not change how perishable the
    recovered structure is, and excluding those cards left the whole
    proof-gated class unprotected.

    Protection covers the first attempt only.  A mechanism this run has
    already executed is no longer an opportunity it stands to lose.
    """
    if (
        isinstance(lookahead_reserve_s, bool)
        or not isinstance(lookahead_reserve_s, (int, float))
        or not math.isfinite(float(lookahead_reserve_s))
        or float(lookahead_reserve_s) < 0.0
    ):
        raise ActionMenuError("ACTION_MENU_INVALID_LOOKAHEAD_RESERVE")
    menu = validate_action_menu(value)
    protected: list[dict[str, Any]] = []
    for entry in menu["entries"]:
        action = entry.get("action")
        display = entry.get("display")
        if (
            entry.get("eligible") is True
            and isinstance(action, Mapping)
            and action.get("action") == "run_skill"
            and action.get("skill") == "operator_rewrite"
            and entry.get("proof_status") in {"proved", "executor_proof_required"}
            and isinstance(display, Mapping)
            and display.get("owns_earliest_live_wall") is True
            and display.get("opportunity_decay_risk") == "high"
            and _semantic_opportunity_untaken(display)
        ):
            protected.append(entry)
    if not protected:
        return menu

    protected.sort(key=lambda entry: (
        -float((entry.get("display") or {}).get("structural_leverage_score") or 0.0),
        str(entry.get("entry_id") or ""),
    ))
    protected_ids = [str(entry["entry_id"]) for entry in protected]
    changed = False
    for ordinal, entry in enumerate(protected, 1):
        display = dict(entry.get("display") or {})
        updated = {
            **display,
            "scheduling_class": "perishable_proved_semantic_opportunity",
            "semantic_launch_priority": ordinal,
            "lookahead_profile_reserve_s": float(lookahead_reserve_s),
        }
        if updated != display:
            entry["display"] = updated
            changed = True

    for entry in menu["entries"]:
        if entry.get("eligible") is not True or entry in protected:
            continue
        action = entry.get("action")
        display = entry.get("display")
        if (
            not isinstance(action, Mapping)
            or action.get("action") != "run_skill"
            or not isinstance(display, Mapping)
            or display.get("logical_structure_effect")
            != "may_destroy_current_semantic_opportunity"
        ):
            continue
        entry["eligible"] = False
        entry["action"] = None
        entry["authorization_class"] = "dominated_by_perishable_semantic_opportunity"
        entry["blockers"] = list(dict.fromkeys([
            *entry.get("blockers", []),
            "PERISHABLE_PROVED_SEMANTIC_OPPORTUNITY_FIRST",
        ]))
        entry["display"] = {
            **display,
            "scheduling_state": "deferred_until_semantic_attempt_finishes",
            "dominating_semantic_entry_ids": protected_ids,
        }
        changed = True

    if not changed:
        return menu
    body = {key: item for key, item in menu.items() if key != "menu_digest"}
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if not item["eligible"]
    ]
    projected = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        projected, expected_seed_sha256=projected["seed_artifact_sha256"]
    )


def filter_action_menu_for_permitted_actions(
    value: object,
    *,
    permitted: Iterable[tuple[str, str]],
    blocker_code: str,
) -> dict[str, Any]:
    """Withdraw authority from entries an active lineage rule cannot execute.

    An entry the planner is shown but the executor refuses costs a full
    decision cycle, and a short run of them ends the run outright.  This makes
    the menu agree with the rule instead of discovering it one veto at a time.
    """
    allowed = {(str(skill), str(target)) for skill, target in permitted}
    menu = validate_action_menu(value)
    changed = False
    for entry in menu["entries"]:
        action = entry.get("action")
        if (
            entry.get("eligible") is not True
            or not isinstance(action, Mapping)
            or action.get("action") != "run_skill"
        ):
            continue
        if (str(action.get("skill") or ""), str(action.get("target") or "")) in allowed:
            continue
        entry["eligible"] = False
        entry["action"] = None
        entry["authorization_class"] = "outside_active_lineage_permission"
        entry["blockers"] = list(dict.fromkeys([
            *entry.get("blockers", []), str(blocker_code),
        ]))
        changed = True
    if not changed:
        return menu
    body = {key: item for key, item in menu.items() if key != "menu_digest"}
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if not item["eligible"]
    ]
    projected = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        projected, expected_seed_sha256=projected["seed_artifact_sha256"]
    )


def refresh_action_menu_same_run_feedback(
    value: object,
    *,
    feedback_by_skill: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Refresh exact-seed ranking evidence without changing action authority.

    The menu is assembled before the first mutation on a seed. When a legal
    but weak sibling does not promote, later decisions on that unchanged seed
    must see the new measured feedback without an expensive Vivado re-profile.
    Action IDs, private parameters, eligibility, and proof bindings are fixed.
    """
    menu = validate_action_menu(value)
    changed = False
    for entry in menu["entries"]:
        if entry.get("eligible") is not True:
            continue
        action = entry.get("action")
        display = entry.get("display")
        if (
            not isinstance(action, Mapping)
            or action.get("action") != "run_skill"
            or not isinstance(display, Mapping)
            or "same_run_feedback_ranking_adjustment" not in display
        ):
            continue
        skill = str(action.get("skill") or "")
        fresh = feedback_by_skill.get(skill)
        if not isinstance(fresh, Mapping):
            fresh = {}
        action_family = str(display.get("action_family") or "")
        selected_family_feedback = None
        family_feedback = fresh.get("by_mechanism_family")
        if isinstance(family_feedback, Mapping):
            candidate = family_feedback.get(action_family)
            if isinstance(candidate, Mapping):
                selected_family_feedback = candidate
        # Cross-executor feedback is only valid for composed wrappers around
        # the same full implementation backend. Local mechanism families are
        # intentionally broad taxonomic groups: a failed cell relocation does
        # not measure endpoint BEL moves, and a failed LUT reflow does not
        # measure exact LUT pin automorphisms.
        if (
            selected_family_feedback is None
            and action_family in {"fresh_implementation", "placement_locality"}
        ):
            for skill_feedback in feedback_by_skill.values():
                if not isinstance(skill_feedback, Mapping):
                    continue
                families = skill_feedback.get("by_mechanism_family")
                candidate = (
                    families.get(action_family)
                    if isinstance(families, Mapping) else None
                )
                if isinstance(candidate, Mapping):
                    selected_family_feedback = candidate
                    break
        if selected_family_feedback is not None:
            fresh = selected_family_feedback
        target = str(action.get("target") or "")
        if action_family == "fanout_replication" and (
            skill == "fanout_opt"
            or (skill == "phys_opt" and target == "AggressiveFanoutOpt")
        ):
            fresh = copy.deepcopy(dict(fresh))
            for attempts_key in ("recent_attempts", "recent_lineage_attempts"):
                attempts = fresh.get(attempts_key)
                if isinstance(attempts, list):
                    fresh[attempts_key] = [
                        item for item in attempts
                        if isinstance(item, Mapping)
                        and str(item.get("target") or "") == target
                    ]
            fresh["feedback_scope"] = (
                "exact_fanout_target" if skill == "fanout_opt" else "broad_fanout"
            )
        prior_feedback = display.get("same_run_feedback")
        residual = (
            prior_feedback.get("current_seed_residual")
            if isinstance(prior_feedback, Mapping) else None
        )
        refreshed_feedback = copy.deepcopy(dict(fresh))
        if isinstance(residual, Mapping):
            refreshed_feedback["current_seed_residual"] = copy.deepcopy(
                dict(residual)
            )
        old_adjustment = display.get("same_run_feedback_ranking_adjustment")
        old_score = display.get("ranking_score")
        if (
            isinstance(old_adjustment, bool)
            or not isinstance(old_adjustment, (int, float))
            or not math.isfinite(float(old_adjustment))
            or isinstance(old_score, bool)
            or not isinstance(old_score, (int, float))
            or not math.isfinite(float(old_score))
        ):
            raise ActionMenuError("ACTION_MENU_INVALID_FEEDBACK_RANKING")
        new_adjustment = same_run_feedback_ranking_adjustment(refreshed_feedback)
        lineage_rows = refreshed_feedback.get("recent_lineage_attempts")
        lineage_mechanism_repeat = bool(
            display.get("search_role") in {
                "basin_builder", "repeated_basin_backend"
            }
            and isinstance(lineage_rows, list)
            and any(
                isinstance(row, Mapping)
                and str(row.get("mechanism_family") or "") == action_family
                for row in lineage_rows
            )
        )
        why_now = str(display.get("why_now") or "")
        repeat_note = (
            "; the same mechanism already completed on the direct ancestor, "
            "so that ancestor gain is consumed"
        )
        if lineage_mechanism_repeat and repeat_note not in why_now:
            why_now += repeat_note
        refreshed_display = {
            **display,
            "ranking_score": round(
                float(old_score) - float(old_adjustment) + new_adjustment,
                6,
            ),
            "same_run_feedback": refreshed_feedback,
            "same_run_feedback_ranking_adjustment": new_adjustment,
            "search_role": (
                "repeated_basin_backend"
                if lineage_mechanism_repeat else display.get("search_role")
            ),
            "lineage_mechanism_repeat": lineage_mechanism_repeat,
            "ancestor_gain_consumed": lineage_mechanism_repeat,
            "why_now": why_now,
        }
        if refreshed_display != display:
            entry["display"] = refreshed_display
            changed = True
    if not changed:
        return menu
    body = {key: item for key, item in menu.items() if key != "menu_digest"}
    refreshed = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        refreshed, expected_seed_sha256=refreshed["seed_artifact_sha256"]
    )


def action_cost_key(skill: object, target: object) -> str:
    """Return the identity a measured mutation cost is recorded against."""
    return f"{str(skill or '')}:{str(target or '')}"


# A destructive scope that rebuilds the whole placement pays for one complete
# implementation pass on this design before it can produce a candidate.  Those
# actions therefore bound each other from below: whatever a full pass has
# already cost this run is the least the next one can cost.
REIMPLEMENTING_DESTRUCTIVE_SCOPES = frozenset({
    "full_place_route",
    "full_unplace_unroute",
    "isolated_full_place_route_siblings",
    "proved_netlist_mutation",
    "temporary_clock_full_place_route",
})

# Of those, the scopes whose action does its own mutation work and *then*
# rebuilds.  The rest are the rebuild itself, so their declared estimate
# already covers it.
REWRITE_THEN_REBUILD_SCOPES = frozenset({"proved_netlist_mutation"})


def _measured_runtime_floor_s(
    *,
    skill: str,
    target: object,
    destructive_scope: object,
    card_estimate_s: float | None,
    measured_runtimes_s: Mapping[str, Any] | None,
    measured_full_implementation_s: float | None,
) -> float | None:
    """Return this run's measured lower bound for one action, or None.

    Two sources, in order.  Where this run already ran this exact action, what
    it cost here is the bound.  Otherwise, an action that rebuilds placement
    must pay for one rebuild -- whose measured cost this run already knows --
    on top of its own declared mutation work, because the rebuild is its final
    stage rather than the whole of it.  The caller keeps whichever of this and
    the declared estimate is larger, so a cheap measurement never lowers a
    conservative estimate.
    """
    observed = measured_runtimes_s or {}
    if isinstance(observed, Mapping):
        same_action = observed.get(action_cost_key(skill, target))
        if (
            not isinstance(same_action, bool)
            and isinstance(same_action, (int, float))
            and math.isfinite(float(same_action))
            and float(same_action) > 0.0
        ):
            return float(same_action)
    if (
        str(destructive_scope or "") in REIMPLEMENTING_DESTRUCTIVE_SCOPES
        and not isinstance(measured_full_implementation_s, bool)
        and isinstance(measured_full_implementation_s, (int, float))
        and math.isfinite(float(measured_full_implementation_s))
        and float(measured_full_implementation_s) > 0.0
    ):
        measured_s = float(measured_full_implementation_s)
        own_work_s = 0.0
        if (
            str(destructive_scope or "") in REWRITE_THEN_REBUILD_SCOPES
            and not isinstance(card_estimate_s, bool)
            and isinstance(card_estimate_s, (int, float))
            and math.isfinite(float(card_estimate_s))
            and float(card_estimate_s) > 0.0
        ):
            # A rewrite pays for its own mutation and then for the rebuild, so
            # the two add.  For an action that *is* the rebuild, its estimate
            # already describes the whole thing, and adding a measured rebuild
            # on top double-counts it -- which pushed `fresh_place_route` out
            # of the endgame window it used to fit in.
            own_work_s = float(card_estimate_s)
        return measured_s + own_work_s
    return None


def action_execution_window_requirement(
    *,
    action: object,
    display: object = None,
    runtime_estimates_s: Mapping[str, Any] | None = None,
    central_measurement_estimate_s: float = 0.0,
    candidate_commit_reserve_s: float = 0.0,
    measured_runtimes_s: Mapping[str, Any] | None = None,
    measured_full_implementation_s: float | None = None,
) -> dict[str, Any] | None:
    """Return the mechanical wall required to launch one menu action.

    Platform-configured estimates override the generic Action Card estimate.
    ``min_remaining_s`` remains an independent lower bound.  The estimate also
    reserves one central measurement and enough time to durably publish a
    candidate; none of these scheduling facts grant mutation authority.
    """
    if not isinstance(action, Mapping) or action.get("action") != "run_skill":
        return None
    for value_s, code in (
        (
            central_measurement_estimate_s,
            "ACTION_MENU_INVALID_CENTRAL_MEASUREMENT_ESTIMATE",
        ),
        (
            candidate_commit_reserve_s,
            "ACTION_MENU_INVALID_CANDIDATE_COMMIT_RESERVE",
        ),
    ):
        if (
            isinstance(value_s, bool)
            or not isinstance(value_s, (int, float))
            or not math.isfinite(float(value_s))
            or float(value_s) < 0.0
        ):
            raise ActionMenuError(code)

    params = action.get("params")
    minimum = params.get("min_remaining_s") if isinstance(params, Mapping) else None
    if minimum is not None and (
        isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not math.isfinite(float(minimum))
        or float(minimum) < 0.0
    ):
        raise ActionMenuError("ACTION_MENU_INVALID_MINIMUM_WINDOW")

    skill = str(action.get("skill") or "")
    estimates = runtime_estimates_s or {}
    configured = estimates.get(skill) if isinstance(estimates, Mapping) else None
    card = display if isinstance(display, Mapping) else {}
    card_estimate = card.get("estimated_runtime_s")
    if card_estimate is None:
        card_estimate = card.get("timeout_s")
    estimate = configured if configured is not None else card_estimate
    estimate_source = (
        "platform_configuration"
        if configured is not None
        else "action_card" if card_estimate is not None else "unavailable"
    )
    if estimate is not None and (
        isinstance(estimate, bool)
        or not isinstance(estimate, (int, float))
        or not math.isfinite(float(estimate))
        or float(estimate) < 0.0
    ):
        raise ActionMenuError("ACTION_MENU_INVALID_RUNTIME_ESTIMATE")

    # Both configured and card estimates are constants that know nothing about
    # this design's size.  One measured hour saw a 300s estimate against an
    # 1209s window on a mechanism that then consumed the entire window and
    # produced nothing.  Where this run has already measured the same action,
    # or the cost of one complete implementation pass that this action must
    # also pay, that measurement replaces the constant.
    measured_s = _measured_runtime_floor_s(
        skill=skill,
        target=action.get("target"),
        destructive_scope=card.get("destructive_scope"),
        card_estimate_s=estimate,
        measured_runtimes_s=measured_runtimes_s,
        measured_full_implementation_s=measured_full_implementation_s,
    )
    if measured_s is not None and (estimate is None or measured_s > float(estimate)):
        estimate = measured_s
        estimate_source = "same_run_measurement"

    minimum_s = float(minimum or 0.0)
    estimated_runtime_s = float(estimate or 0.0)
    estimated_completion_s = (
        estimated_runtime_s
        + float(central_measurement_estimate_s)
        + float(candidate_commit_reserve_s)
        if estimate is not None else 0.0
    )
    required = max(minimum_s, estimated_completion_s)
    if required <= 0.0:
        return None
    return {
        "required_execution_window_s": required,
        "minimum_remaining_s": minimum_s,
        "selected_runtime_estimate_s": (
            estimated_runtime_s if estimate is not None else None
        ),
        "runtime_estimate_source": estimate_source,
        "central_measurement_estimate_s": float(central_measurement_estimate_s),
        "candidate_commit_reserve_s": float(candidate_commit_reserve_s),
    }


def filter_action_menu_for_exhausted_entries(
    value: object,
    *,
    exhausted_entry_ids: Sequence[str],
    exhausted_family_entry_ids: Sequence[str] = (),
    exhausted_probe_entry_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Remove same-seed actions that have already executed.

    Entry identity remains visible as typed exhausted evidence, but its action
    payload and launch authority are removed before LLM arbitration.
    """
    menu = validate_action_menu(value)
    exhausted = set(exhausted_entry_ids)
    family_exhausted = set(exhausted_family_entry_ids)
    probe_exhausted = set(exhausted_probe_entry_ids)
    if any(
        not isinstance(item, str) or _SHA256.fullmatch(item) is None
        for item in exhausted | family_exhausted | probe_exhausted
    ):
        raise ActionMenuError("ACTION_MENU_INVALID_EXHAUSTED_ENTRY_ID")
    changed = False
    for entry in menu["entries"]:
        if (
            entry.get("eligible") is not True
            or entry["entry_id"] not in exhausted | family_exhausted | probe_exhausted
        ):
            continue
        if entry.get("kind") == "save_and_exit":
            continue
        if entry.get("kind") == "probe" and entry["entry_id"] not in probe_exhausted:
            continue
        changed = True
        entry["eligible"] = False
        entry["action"] = None
        family_level = entry["entry_id"] in family_exhausted
        probe_level = entry["entry_id"] in probe_exhausted
        entry["authorization_class"] = (
            "same_seed_probe_exhausted"
            if probe_level else
            "same_seed_mechanism_family_exhausted"
            if family_level else "same_seed_action_exhausted"
        )
        entry["blockers"] = [
            "PROBE_ALREADY_OBSERVED_FROM_SEED"
            if probe_level else
            "SAME_SEED_MECHANISM_FAMILY_EXHAUSTED"
            if family_level else "ACTION_ALREADY_EXECUTED_FROM_SEED"
        ]
        entry["display"] = {
            **entry.get("display", {}),
            "execution_state": (
                "probe_exhausted_on_current_seed"
                if probe_level else
                "mechanism_family_exhausted_on_current_seed"
                if family_level else "exhausted_on_current_seed"
            ),
        }
    if not changed:
        return menu
    body = {key: item for key, item in menu.items() if key != "menu_digest"}
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if not item["eligible"]
    ]
    projected = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        projected, expected_seed_sha256=projected["seed_artifact_sha256"]
    )


def project_action_menu_top_k(
    value: object, *, configured_top_k: int | None = None
) -> dict[str, Any]:
    """Apply adaptive top-K after runtime and exhaustion filters.

    The input must be the full source-bound pool.  Calling this only after
    exhausted entries are removed lets the next strongest live mechanisms fill
    the vacated cards instead of leaving stale failures in the decision window.
    """
    menu = validate_action_menu(value)
    top_k = menu["top_k"] if configured_top_k is None else configured_top_k
    effective_top_k = _adaptive_global_limit(
        menu["entries"], configured_top_k=int(top_k)
    )
    body = {key: item for key, item in menu.items() if key != "menu_digest"}
    body["top_k"] = effective_top_k
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in body["entries"] if not item["eligible"]
    ]
    projected = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        projected, expected_seed_sha256=projected["seed_artifact_sha256"]
    )


def build_recovery_action_menu(
    *,
    seed_candidate_id: str,
    seed_artifact_sha256: str,
    registered_arithmetic_packet: Mapping[str, Any] | None,
    proof_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    top_k: int = DEFAULT_TOP_K,
    auxiliary_actions: Sequence[Mapping[str, Any]] = (),
    recovery_error_code: str | None = None,
    recovery_error_detail: str = "",
    cold_isolation: bool = False,
    apply_adaptive_limit: bool = True,
) -> dict[str, Any]:
    """Compose independent family results; degrade only with zero proved entries."""
    complete = build_action_menu(
        seed_candidate_id=seed_candidate_id,
        seed_artifact_sha256=seed_artifact_sha256,
        registered_arithmetic_packet=registered_arithmetic_packet,
        proof_bindings=proof_bindings,
        top_k=top_k,
        auxiliary_actions=auxiliary_actions,
        cold_isolation=cold_isolation,
        apply_adaptive_limit=apply_adaptive_limit,
    )
    proved = [
        item for item in complete["entries"]
        if item.get("kind") == "mutation" and item.get("eligible") is True
    ]
    if recovery_error_code and not proved:
        return {
            "menu": build_degraded_action_menu(
                seed_candidate_id=seed_candidate_id,
                seed_artifact_sha256=seed_artifact_sha256,
                error_code=recovery_error_code,
                error_detail=recovery_error_detail,
                top_k=top_k,
                auxiliary_actions=auxiliary_actions,
                cold_isolation=cold_isolation,
            ),
            "build_state": "degraded",
            "error_code": str(recovery_error_code),
        }
    return {
        "menu": complete,
        "build_state": "partial" if recovery_error_code else "complete",
        "error_code": str(recovery_error_code) if recovery_error_code else None,
    }


def replace_action_menu_family_slice(
    existing_value: object,
    refreshed_value: object,
    *,
    family_kind: str,
) -> dict[str, Any]:
    """Atomically replace one current-seed family without rebuilding telemetry."""
    existing = validate_action_menu(existing_value)
    refreshed = validate_action_menu(
        refreshed_value,
        expected_seed_sha256=existing["seed_artifact_sha256"],
    )
    if (
        not isinstance(family_kind, str)
        or not family_kind
        or existing["seed_candidate_id"] != refreshed["seed_candidate_id"]
        or existing["replay_derived_actions_forbidden"]
        != refreshed["replay_derived_actions_forbidden"]
    ):
        raise ActionMenuError("ACTION_MENU_FAMILY_SLICE_REPLACEMENT_MISMATCH")

    def belongs(entry: Mapping[str, Any]) -> bool:
        display = entry.get("display")
        return bool(
            isinstance(display, Mapping)
            and display.get("family_kind") == family_kind
        )

    entries = [
        copy.deepcopy(item) for item in existing["entries"] if not belongs(item)
    ]
    entries.extend(
        copy.deepcopy(item) for item in refreshed["entries"] if belongs(item)
    )
    entries.sort(key=lambda item: (
        not item["eligible"],
        item["kind"] == "save_and_exit",
        item["action_target"],
        item["entry_id"],
    ))
    body = {
        key: copy.deepcopy(value)
        for key, value in existing.items()
        if key not in {
            "menu_digest", "entries", "eligible_entry_ids", "blocked_entry_ids"
        }
    }
    body["entries"] = entries
    body["eligible_entry_ids"] = [
        item["entry_id"] for item in entries if item["eligible"]
    ]
    body["blocked_entry_ids"] = [
        item["entry_id"] for item in entries if not item["eligible"]
    ]
    merged = {**body, "menu_digest": _digest(body)}
    return validate_action_menu(
        merged,
        expected_seed_sha256=existing["seed_artifact_sha256"],
    )


def validate_action_menu(
    value: object,
    *,
    expected_seed_sha256: str | None = None,
    cold_isolation: bool | None = None,
) -> dict[str, Any]:
    """Strictly replay menu identity, entry hashes, and eligibility indexes."""
    if not isinstance(value, Mapping):
        raise ActionMenuError("ACTION_MENU_REQUIRED")
    menu = copy.deepcopy(dict(value))
    required = {
        "schema_version", "authority", "seed_candidate_id",
        "seed_artifact_sha256", "top_k", "entries", "eligible_entry_ids",
        "blocked_entry_ids", "parameter_authority", "promotion_authority",
        "replay_derived_actions_forbidden",
        "menu_digest",
    }
    if set(menu) != required or menu.get("schema_version") != SCHEMA_VERSION:
        raise ActionMenuError("ACTION_MENU_SCHEMA_MISMATCH")
    seed = _sha(menu.get("seed_artifact_sha256"), field="seed_artifact_sha256")
    if expected_seed_sha256 is not None and seed != expected_seed_sha256:
        raise ActionMenuError("ACTION_MENU_STALE_SEED")
    digest = menu.pop("menu_digest")
    if _sha(digest, field="menu_digest") != _digest(menu):
        raise ActionMenuError("ACTION_MENU_DIGEST_MISMATCH")
    if (
        menu.get("authority") != "tool_built_seed_bound_action_authorization"
        or menu.get("parameter_authority") != "entry_only_no_llm_parameter_authority"
        or menu.get("promotion_authority") is not False
        or not isinstance(menu.get("seed_candidate_id"), str)
        or not menu["seed_candidate_id"]
        or isinstance(menu.get("top_k"), bool)
        or not isinstance(menu.get("top_k"), int)
        or not 1 <= menu["top_k"] <= 16
        or not isinstance(menu.get("entries"), list)
        or not isinstance(menu.get("replay_derived_actions_forbidden"), bool)
    ):
        raise ActionMenuError("ACTION_MENU_SCHEMA_MISMATCH")
    ids: list[str] = []
    eligible_ids: list[str] = []
    blocked_ids: list[str] = []
    entry_fields = {
        "entry_id", "kind", "eligible", "seed_artifact_sha256",
        "action_target", "family_fingerprint", "recovered_plan_digest",
        "proof_digest", "proof_status", "authorization_class", "action",
        "display", "blockers",
    }
    for entry in menu["entries"]:
        if not isinstance(entry, dict) or set(entry) != entry_fields:
            raise ActionMenuError("ACTION_MENU_ENTRY_SCHEMA_MISMATCH")
        if entry.get("kind") not in _ENTRY_KINDS or entry.get("seed_artifact_sha256") != seed:
            raise ActionMenuError("ACTION_MENU_ENTRY_SCHEMA_MISMATCH")
        expected_id = _entry_id(
            seed_artifact_sha256=seed,
            action_target=str(entry.get("action_target") or ""),
            family_fingerprint=_sha(entry.get("family_fingerprint"), field="family_fingerprint"),
            recovered_plan_digest=_sha(entry.get("recovered_plan_digest"), field="recovered_plan_digest"),
            proof_digest=_sha(entry.get("proof_digest"), field="proof_digest"),
        )
        if entry.get("entry_id") != expected_id or expected_id in ids:
            raise ActionMenuError("ACTION_MENU_ENTRY_ID_MISMATCH")
        if not isinstance(entry.get("eligible"), bool):
            raise ActionMenuError("ACTION_MENU_ENTRY_SCHEMA_MISMATCH")
        blockers = entry.get("blockers")
        if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
            raise ActionMenuError("ACTION_MENU_ENTRY_SCHEMA_MISMATCH")
        if entry["eligible"]:
            if blockers or not isinstance(entry.get("action"), dict):
                raise ActionMenuError("ACTION_MENU_ELIGIBILITY_MISMATCH")
            eligible_ids.append(expected_id)
        else:
            if not blockers or entry.get("action") is not None:
                raise ActionMenuError("ACTION_MENU_ELIGIBILITY_MISMATCH")
            blocked_ids.append(expected_id)
        action = entry.get("action")
        if (
            menu["replay_derived_actions_forbidden"]
            and isinstance(action, Mapping)
            and action.get("action") == "run_skill"
            and action.get("skill") == "semantic_replay"
        ):
            raise ActionMenuError("COLD_ISOLATION_REPLAY_ENTRY_FORBIDDEN")
        ids.append(expected_id)
    if (
        cold_isolation is not None
        and menu["replay_derived_actions_forbidden"] is not bool(cold_isolation)
    ):
        raise ActionMenuError("ACTION_MENU_ISOLATION_POLICY_MISMATCH")
    if menu.get("eligible_entry_ids") != eligible_ids or menu.get("blocked_entry_ids") != blocked_ids:
        raise ActionMenuError("ACTION_MENU_INDEX_MISMATCH")
    menu["menu_digest"] = digest
    return menu


def select_action_menu_entry(
    menu: object,
    selection: object,
    *,
    current_seed_sha256: str,
    cold_isolation: bool | None = None,
) -> dict[str, Any]:
    """Resolve one exact ID plus a non-authoritative mechanism analysis."""
    validated = validate_action_menu(
        menu,
        expected_seed_sha256=current_seed_sha256,
        cold_isolation=cold_isolation,
    )
    if not isinstance(selection, Mapping) or set(selection) not in ({
        "selected_entry_id", "rationale"
    }, {
        "selected_entry_id", "reasoning"
    }):
        raise ActionMenuError("ACTION_MENU_SELECTION_SCHEMA_MISMATCH")
    selected_id = selection.get("selected_entry_id")
    if not isinstance(selected_id, str) or _SHA256.fullmatch(selected_id) is None:
        raise ActionMenuError("ACTION_MENU_SELECTED_ID_INVALID")
    decision_analysis = None
    if "reasoning" in selection:
        reasoning = selection.get("reasoning")
        fields = {
            "current_wall", "why_selected", "why_not_alternatives",
            "expected_next_wall", "risk", "confidence",
        }
        if (
            not isinstance(reasoning, Mapping)
            or set(reasoning) != fields
            or any(
                not isinstance(reasoning.get(key), str)
                or not str(reasoning[key]).strip()
                for key in fields - {"why_not_alternatives"}
            )
            or not isinstance(reasoning.get("why_not_alternatives"), list)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in reasoning["why_not_alternatives"]
            )
            or reasoning.get("risk") not in {"low", "medium", "high"}
            or reasoning.get("confidence") not in {
                "trusted", "uncertain", "untrusted"
            }
        ):
            raise ActionMenuError("ACTION_MENU_REASONING_SCHEMA_MISMATCH")
        decision_analysis = copy.deepcopy(dict(reasoning))
        rationale = str(reasoning["why_selected"]).strip()
    else:
        rationale = selection.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ActionMenuError("ACTION_MENU_RATIONALE_REQUIRED")
    matches = [item for item in validated["entries"] if item["entry_id"] == selected_id]
    if len(matches) != 1:
        raise ActionMenuError("ACTION_MENU_SELECTED_ID_NOT_FOUND")
    entry = matches[0]
    if entry["eligible"] is not True:
        raise ActionMenuError("ACTION_MENU_SELECTED_ENTRY_BLOCKED")
    return {
        "menu_digest": validated["menu_digest"],
        "selected_entry_id": selected_id,
        "rationale": rationale.strip(),
        "decision_analysis": decision_analysis,
        "seed_candidate_id": validated["seed_candidate_id"],
        "seed_artifact_sha256": validated["seed_artifact_sha256"],
        "entry": copy.deepcopy(entry),
        "action": copy.deepcopy(entry["action"]),
    }


def bind_shadow_action_menu(menu: object, *, promoted_candidate_sha256: str) -> dict[str, Any]:
    """Return the no-authority binding published by a promoted-seed prefetch."""
    validated = validate_action_menu(
        menu, expected_seed_sha256=promoted_candidate_sha256
    )
    body = {
        "schema_version": "shadow-action-menu-binding-v1",
        "seed_artifact_sha256": promoted_candidate_sha256,
        "menu_digest": validated["menu_digest"],
        "eligible_entry_ids": list(validated["eligible_entry_ids"]),
        "read_only": True,
        "mutation_authority": False,
    }
    return {**body, "binding_digest": _digest(body)}


def invalidate_action_menu(
    menu: object,
    *,
    promoted_candidate_sha256: str,
    finalizing_transaction_id: str | None = None,
    launch_authorization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Invalidate a seed menu while preserving only one terminal receipt.

    A successful mutation may promote before its transaction is archived.  The
    optional exemption binds that already-authorized in-flight transaction; it
    carries no menu entry or mutation authority into the promoted seed.
    """
    validated = validate_action_menu(menu)
    next_seed = _sha(
        promoted_candidate_sha256, field="promoted_candidate_sha256"
    )
    if validated["seed_artifact_sha256"] == next_seed:
        raise ActionMenuError("ACTION_MENU_PROMOTION_DID_NOT_CHANGE_SEED")
    if (finalizing_transaction_id is None) != (launch_authorization is None):
        raise ActionMenuError("ACTION_MENU_INVALIDATION_EXEMPTION_INCOMPLETE")
    exemption = None
    if launch_authorization is not None:
        authorization = copy.deepcopy(dict(launch_authorization))
        transaction_id = str(finalizing_transaction_id or "")
        selected_entry_id = authorization.get("selected_entry_id")
        matches = [
            entry for entry in validated["entries"]
            if entry.get("entry_id") == selected_entry_id
        ]
        if (
            not transaction_id
            or authorization.get("kind") != "action_menu_launch_authorization"
            or authorization.get("seed_candidate_id")
            != validated["seed_candidate_id"]
            or authorization.get("seed_artifact_sha256")
            != validated["seed_artifact_sha256"]
            or authorization.get("menu_digest") != validated["menu_digest"]
            or authorization.get("parameter_authority")
            != validated["parameter_authority"]
            or len(matches) != 1
            or matches[0].get("eligible") is not True
            or matches[0].get("family_fingerprint")
            != authorization.get("family_fingerprint")
            or matches[0].get("recovered_plan_digest")
            != authorization.get("recovered_plan_digest")
            or matches[0].get("proof_digest")
            != authorization.get("proof_digest")
        ):
            raise ActionMenuError("ACTION_MENU_INVALIDATION_EXEMPTION_MISMATCH")
        selected_entry = matches[0]
        exemption = {
            "schema_version": "action-menu-terminal-exemption-v1",
            "transaction_id": transaction_id,
            "seed_candidate_id": validated["seed_candidate_id"],
            "seed_artifact_sha256": validated["seed_artifact_sha256"],
            "menu_digest": validated["menu_digest"],
            "selected_entry_id": str(selected_entry_id),
            "action_target": str(selected_entry["action_target"]),
            "action_digest": object_digest(selected_entry["action"]),
            "launch_authorization_digest": object_digest(authorization),
            "terminal_archival_only": True,
            "mutation_authority": False,
        }
    body = {
        "schema_version": "action-menu-invalidation-v1",
        "invalidated_menu_digest": validated["menu_digest"],
        "invalidated_seed_artifact_sha256": validated["seed_artifact_sha256"],
        "promoted_candidate_sha256": next_seed,
        "invalidated_entry_count": len(validated["entries"]),
        "entries_carried_forward": 0,
        "inflight_terminal_exemption": exemption,
    }
    return {**body, "receipt_digest": _digest(body)}


def validate_action_menu_invalidation(value: object) -> dict[str, Any]:
    """Replay an invalidation receipt and its optional terminal exemption."""
    if not isinstance(value, Mapping):
        raise ActionMenuError("ACTION_MENU_INVALIDATION_REQUIRED")
    receipt = copy.deepcopy(dict(value))
    required = {
        "schema_version", "invalidated_menu_digest",
        "invalidated_seed_artifact_sha256", "promoted_candidate_sha256",
        "invalidated_entry_count", "entries_carried_forward",
        "inflight_terminal_exemption", "receipt_digest",
    }
    if set(receipt) != required:
        raise ActionMenuError("ACTION_MENU_INVALIDATION_SCHEMA_MISMATCH")
    digest = receipt.pop("receipt_digest")
    if (
        receipt.get("schema_version") != "action-menu-invalidation-v1"
        or _sha(digest, field="receipt_digest") != _digest(receipt)
        or _sha(
            receipt.get("invalidated_menu_digest"),
            field="invalidated_menu_digest",
        ) != receipt["invalidated_menu_digest"]
        or _sha(
            receipt.get("invalidated_seed_artifact_sha256"),
            field="invalidated_seed_artifact_sha256",
        ) != receipt["invalidated_seed_artifact_sha256"]
        or _sha(
            receipt.get("promoted_candidate_sha256"),
            field="promoted_candidate_sha256",
        ) != receipt["promoted_candidate_sha256"]
        or receipt["invalidated_seed_artifact_sha256"]
        == receipt["promoted_candidate_sha256"]
        or isinstance(receipt.get("invalidated_entry_count"), bool)
        or not isinstance(receipt.get("invalidated_entry_count"), int)
        or receipt["invalidated_entry_count"] < 0
        or receipt.get("entries_carried_forward") != 0
    ):
        raise ActionMenuError("ACTION_MENU_INVALIDATION_SCHEMA_MISMATCH")
    exemption = receipt.get("inflight_terminal_exemption")
    if exemption is not None:
        fields = {
            "schema_version", "transaction_id", "seed_candidate_id",
            "seed_artifact_sha256", "menu_digest", "selected_entry_id",
            "action_target", "action_digest", "launch_authorization_digest",
            "terminal_archival_only", "mutation_authority",
        }
        if (
            not isinstance(exemption, Mapping)
            or set(exemption) != fields
            or exemption.get("schema_version")
            != "action-menu-terminal-exemption-v1"
            or not isinstance(exemption.get("transaction_id"), str)
            or not exemption["transaction_id"]
            or not isinstance(exemption.get("seed_candidate_id"), str)
            or not exemption["seed_candidate_id"]
            or _sha(
                exemption.get("seed_artifact_sha256"),
                field="seed_artifact_sha256",
            ) != receipt["invalidated_seed_artifact_sha256"]
            or _sha(exemption.get("menu_digest"), field="menu_digest")
            != receipt["invalidated_menu_digest"]
            or _sha(exemption.get("selected_entry_id"), field="selected_entry_id")
            != exemption["selected_entry_id"]
            or not isinstance(exemption.get("action_target"), str)
            or not exemption["action_target"]
            or _sha(exemption.get("action_digest"), field="action_digest")
            != exemption["action_digest"]
            or _sha(
                exemption.get("launch_authorization_digest"),
                field="launch_authorization_digest",
            ) != exemption["launch_authorization_digest"]
            or exemption.get("terminal_archival_only") is not True
            or exemption.get("mutation_authority") is not False
        ):
            raise ActionMenuError("ACTION_MENU_INVALIDATION_EXEMPTION_MISMATCH")
    receipt["receipt_digest"] = digest
    return receipt


__all__ = [
    "filter_action_menu_for_permitted_actions",
    "ActionMenuError",
    "REIMPLEMENTING_DESTRUCTIVE_SCOPES",
    "action_cost_key",
    "DEFAULT_TOP_K",
    "SCHEMA_VERSION",
    "action_execution_window_requirement",
    "bind_shadow_action_menu",
    "build_action_menu",
    "build_degraded_action_menu",
    "build_recovery_action_menu",
    "filter_action_menu_for_exhausted_entries",
    "filter_action_menu_for_execution_window",
    "reserve_top_ranked_launch_opportunity",
    "invalidate_action_menu",
    "prioritize_last_launch_opportunity",
    "project_action_menu_top_k",
    "replace_action_menu_family_slice",
    "refresh_action_menu_same_run_feedback",
    "select_action_menu_entry",
    "validate_action_menu",
    "validate_action_menu_invalidation",
]
