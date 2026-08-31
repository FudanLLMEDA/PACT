"""Mechanically derive proof-gated recurrence-cut mapping candidates."""

from __future__ import annotations

import math
import hashlib
import json
import re
from dataclasses import asdict
from typing import Any

from .operator_proof_current import (
    RecurrenceCutPlan,
    build_recurrence_family_authorization_from_facts,
    build_recurrence_authorization_from_facts,
    recurrence_spec_from_extracted_facts,
    verify_rewrite_authorization,
)


PLACEMENT_MANIFEST_VERSION = 4
FAMILY_PLACEMENT_MANIFEST_VERSION = 2
TOPOLOGY_MANIFEST_VERSION = 2
FAMILY_TOPOLOGY_MANIFEST_VERSION = 2
_SITE_COORD_RE = re.compile(r"_X(?P<x>\d+)Y(?P<y>\d+)$")
_LUT_BEL_RE = re.compile(r"^[A-H]6LUT$")
_FF_BEL_RE = re.compile(r"^[A-H]FF2?$")


def balanced_srl_segment_candidates(
    transport_valid_transactions: int,
    *,
    max_candidates: int = 3,
) -> list[tuple[int, ...]]:
    """Generate balanced legal SRLC32E segmentations without fixed depth/count."""
    if (
        isinstance(transport_valid_transactions, bool)
        or not isinstance(transport_valid_transactions, int)
        or not 1 <= transport_valid_transactions <= 256
    ):
        raise ValueError("transport_valid_transactions must be 1..256")
    if (
        isinstance(max_candidates, bool) or not isinstance(max_candidates, int)
        or not 1 <= max_candidates <= 4
    ):
        raise ValueError("max_candidates must be 1..4")
    minimum = math.ceil(transport_valid_transactions / 32)
    maximum = min(transport_valid_transactions, minimum + max_candidates - 1)
    candidates = []
    for count in range(minimum, maximum + 1):
        quotient, remainder = divmod(transport_valid_transactions, count)
        segments = tuple(
            quotient + (1 if index < remainder else 0)
            for index in range(count)
        )
        if all(1 <= depth <= 32 for depth in segments):
            candidates.append(segments)
    return candidates


def build_recurrence_action_plans(
    facts: dict[str, Any],
    *,
    max_segment_candidates: int = 3,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Build independent abstract plans; no plan grants emitted-DCP authority."""
    spec = recurrence_spec_from_extracted_facts(facts)
    counter_width = max(1, math.ceil(math.log2(spec.transport_valid_transactions + 1)))
    plans = []
    for segments in balanced_srl_segment_candidates(
        spec.transport_valid_transactions,
        max_candidates=max_segment_candidates,
    ):
        plan = RecurrenceCutPlan(
            template="DSP_PREG_SRL_RECURRENCE_CUT_V1",
            state_width=spec.state_width,
            transport_valid_transactions=spec.transport_valid_transactions,
            mask_valid_transactions=spec.transport_valid_transactions,
            counter_width=counter_width,
            srl_segment_depths=segments,
            reset_mask_value=0,
            arithmetic="modular_add",
            advance_enable="ce",
            max_new_dsp_sites=0,
            clock_footprint_policy="preexisting_slice_clock_leaves_only",
        )
        authorization = build_recurrence_authorization_from_facts(
            facts,
            plan=plan,
            timeout_ms=timeout_ms,
        )
        plans.append({
            "plan": plan,
            "authorization": authorization,
            "derived_resources": {
                "state_width": spec.state_width,
                "transport_valid_transactions": spec.transport_valid_transactions,
                "srl_segments_per_bit": len(segments),
                "srlc32e_cells": spec.state_width * len(segments),
                "counter_width": counter_width,
            },
            "authority": "pre_mutation_only",
        })
    return plans


def build_recurrence_family_action_plans(
    facts: dict[str, Any],
    *,
    max_segment_candidates: int = 3,
    timeout_ms: int = 5000,
) -> list[dict[str, Any]]:
    """Derive one common logical plan bound to every recovered family member."""
    width = facts.get("state_width")
    delay = facts.get("transport_valid_transactions")
    count = facts.get("boundary_count")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (width, delay, count)):
        raise ValueError("recurrence family dimensions are invalid")
    if count < 2 or not 2 <= width <= 256 or not 1 <= delay <= 63:
        raise ValueError("recurrence family dimensions are unsupported")
    counter_width = max(1, math.ceil(math.log2(delay + 1)))
    candidates = []
    for segments in balanced_srl_segment_candidates(
        delay, max_candidates=max_segment_candidates
    ):
        plan = RecurrenceCutPlan(
            template="DSP_PREG_SRL_RECURRENCE_CUT_V1",
            state_width=width,
            transport_valid_transactions=delay,
            mask_valid_transactions=delay,
            counter_width=counter_width,
            srl_segment_depths=segments,
            reset_mask_value=0,
            arithmetic="modular_add",
            advance_enable="ce",
            max_new_dsp_sites=0,
            clock_footprint_policy="preexisting_slice_clock_leaves_only",
        )
        authorization = build_recurrence_family_authorization_from_facts(
            facts, plan=plan, timeout_ms=timeout_ms
        )
        candidates.append({
            "plan": plan,
            "authorization": authorization,
            "family_id": facts.get("family_id"),
            "boundary_region_sha256s": list(facts["boundary_region_sha256s"]),
            "derived_resources": {
                "boundary_count": count,
                "state_width": width,
                "transport_valid_transactions": delay,
                "srl_segments_per_bit": len(segments),
                "srlc32e_cells": count * width * len(segments),
                "counter_width": counter_width,
            },
            "authority": "pre_mutation_only",
        })
    return candidates


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _stable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _site_xy(site: str) -> tuple[int, int]:
    match = _SITE_COORD_RE.search(site)
    if match is None:
        raise ValueError(f"site has no canonical X/Y coordinate: {site}")
    return int(match.group("x")), int(match.group("y"))


def _balanced_hop_score(
    source_distance: int,
    reinjection_distance: int,
    segment_index: int,
    segment_count: int,
) -> tuple[int, int]:
    """Rank a layer by its worst evenly divided source-to-reinjection hop."""
    if not 0 <= segment_index < segment_count:
        raise ValueError("recurrence segment index is outside its transport")
    left_hops = segment_index + 1
    right_hops = segment_count - segment_index
    scale = math.lcm(*range(1, segment_count + 1))
    return (
        max(
            source_distance * (scale // left_hops),
            reinjection_distance * (scale // right_hops),
        ),
        source_distance + reinjection_distance,
    )


def _match_adjacent_srl_slots(
    previous_cells: list[dict[str, Any]],
    current_slots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Minimize the worst, then total, same-bit hop between adjacent layers."""
    count = len(previous_cells)
    if (
        count == 0
        or len(current_slots) != count
        or [cell.get("bit") for cell in previous_cells] != list(range(count))
    ):
        raise ValueError("recurrence adjacent SRL layers have incompatible widths")
    ordered = sorted(
        current_slots,
        key=lambda slot: (slot["x"], slot["y"], slot["site"], slot["bel"]),
    )
    previous_xy = [_site_xy(cell["site"]) for cell in previous_cells]
    current_xy = [(slot["x"], slot["y"]) for slot in ordered]
    distances = [
        [abs(left_x - right_x) + abs(left_y - right_y) for right_x, right_y in current_xy]
        for left_x, left_y in previous_xy
    ]

    def has_perfect_matching(threshold: int) -> bool:
        right_owner = [-1] * count

        def augment(left: int, seen: list[bool]) -> bool:
            for right, distance in enumerate(distances[left]):
                if distance > threshold or seen[right]:
                    continue
                seen[right] = True
                if right_owner[right] < 0 or augment(right_owner[right], seen):
                    right_owner[right] = left
                    return True
            return False

        return all(augment(left, [False] * count) for left in range(count))

    thresholds = sorted({distance for row in distances for distance in row})
    low, high = 0, len(thresholds) - 1
    while low < high:
        middle = (low + high) // 2
        if has_perfect_matching(thresholds[middle]):
            high = middle
        else:
            low = middle + 1
    threshold = thresholds[low]

    # Hungarian assignment is deterministic because both layers and all scans
    # are canonically ordered. A blocked edge costs more than any legal matching.
    blocked = threshold * count + 1
    infinity = blocked * (count + 1) + 1
    costs = [
        [distance if distance <= threshold else blocked for distance in row]
        for row in distances
    ]
    left_potential = [0] * (count + 1)
    right_potential = [0] * (count + 1)
    right_match = [0] * (count + 1)
    predecessor = [0] * (count + 1)
    for left in range(1, count + 1):
        right_match[0] = left
        minimum = [infinity] * (count + 1)
        visited = [False] * (count + 1)
        right = 0
        while True:
            visited[right] = True
            active_left = right_match[right]
            delta = infinity
            next_right = 0
            for candidate_right in range(1, count + 1):
                if visited[candidate_right]:
                    continue
                reduced = (
                    costs[active_left - 1][candidate_right - 1]
                    - left_potential[active_left]
                    - right_potential[candidate_right]
                )
                if reduced < minimum[candidate_right]:
                    minimum[candidate_right] = reduced
                    predecessor[candidate_right] = right
                if minimum[candidate_right] < delta:
                    delta = minimum[candidate_right]
                    next_right = candidate_right
            if next_right == 0:
                raise ValueError("recurrence adjacent SRL matching is infeasible")
            for candidate_right in range(count + 1):
                if visited[candidate_right]:
                    left_potential[right_match[candidate_right]] += delta
                    right_potential[candidate_right] -= delta
                else:
                    minimum[candidate_right] -= delta
            right = next_right
            if right_match[right] == 0:
                break
        while True:
            previous_right = predecessor[right]
            right_match[right] = right_match[previous_right]
            right = previous_right
            if right == 0:
                break

    assignment = [-1] * count
    for right in range(1, count + 1):
        if right_match[right] > 0:
            assignment[right_match[right] - 1] = right - 1
    if any(
        right < 0 or distances[left][right] > threshold
        for left, right in enumerate(assignment)
    ):
        raise ValueError("recurrence adjacent SRL matching used a blocked edge")
    return [ordered[right] for right in assignment]


def _candidate_plan(candidate: dict[str, Any]) -> RecurrenceCutPlan:
    if not isinstance(candidate, dict) or candidate.get("authority") != "pre_mutation_only":
        raise ValueError("candidate is not a pre-mutation recurrence action plan")
    raw = candidate.get("plan")
    if isinstance(raw, RecurrenceCutPlan):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("candidate plan is missing")
    normalized = dict(raw)
    depths = normalized.get("srl_segment_depths")
    if isinstance(depths, list):
        normalized["srl_segment_depths"] = tuple(depths)
    try:
        return RecurrenceCutPlan(**normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate plan has an invalid schema") from exc


def _verify_candidate(
    facts: dict[str, Any],
    candidate: dict[str, Any],
    *,
    authorization_region_sha256: str | None = None,
) -> RecurrenceCutPlan:
    plan = _candidate_plan(candidate)
    authorization = candidate.get("authorization")
    if not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(facts.get("design_sha256") or ""),
        expected_region_sha256=str(
            authorization_region_sha256 or facts.get("region_sha256") or ""
        ),
    ):
        raise ValueError("recurrence authorization replay failed")
    if _stable(authorization.get("plan")) != _stable(asdict(plan)):
        raise ValueError("candidate plan differs from its authorization")
    return plan


def _validated_oracle_slots(
    records: list[dict[str, Any]],
    *,
    expected_kind: str,
    facts: dict[str, Any],
    incumbent_regions: set[str],
    require_incumbent_region: bool,
) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not records:
        raise ValueError(f"{expected_kind} records must be a nonempty list")
    expected_keys = {
        "kind", "version", "source_dcp_sha256", "region_sha256", "site", "bel",
        "clock_region", "x", "y", "measured_legal", "oracle",
        "incumbent_clock_region", "preexisting_clock_leaf",
        "source_distance", "reinjection_distance",
    }
    result = []
    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != expected_keys:
            raise ValueError(f"{expected_kind}[{index}] has an unsupported schema")
        if record.get("kind") != expected_kind or record.get("version") != 2:
            raise ValueError(f"{expected_kind}[{index}] has the wrong identity")
        if record.get("source_dcp_sha256") != facts.get("design_sha256") or record.get(
            "region_sha256"
        ) != facts.get("region_sha256"):
            raise ValueError(f"{expected_kind}[{index}] is stale or cross-seed")
        if record.get("oracle") != "vivado_connected_place_cell" or record.get(
            "measured_legal"
        ) is not True:
            raise ValueError(f"{expected_kind}[{index}] is not a successful measured probe")
        site = record.get("site")
        bel = record.get("bel")
        region = record.get("clock_region")
        if not all(isinstance(value, str) and value for value in (site, bel, region)):
            raise ValueError(f"{expected_kind}[{index}] has an invalid physical slot")
        if (
            not isinstance(record.get("incumbent_clock_region"), bool)
            or not isinstance(record.get("preexisting_clock_leaf"), bool)
        ):
            raise ValueError(f"{expected_kind}[{index}] has invalid clock-leaf evidence")
        if any(
            isinstance(record.get(key), bool)
            or not isinstance(record.get(key), int)
            or not 0 <= record[key] <= 100000
            for key in ("source_distance", "reinjection_distance")
        ):
            raise ValueError(f"{expected_kind}[{index}] has invalid anchor distances")
        incumbent = region in incumbent_regions
        if record["incumbent_clock_region"] is not incumbent:
            raise ValueError(
                f"{expected_kind}[{index}] has inconsistent clock-region classification"
            )
        if require_incumbent_region and not incumbent:
            raise ValueError(f"{expected_kind}[{index}] is outside the incumbent regions")
        x, y = _site_xy(site)
        if record.get("x") != x or record.get("y") != y:
            raise ValueError(f"{expected_kind}[{index}] coordinate does not match its site")
        key = (site, bel)
        if key in seen:
            raise ValueError(f"{expected_kind} contains a duplicate site/BEL slot")
        seen.add(key)
        result.append(dict(record))
    return result


def allocate_recurrence_srl_placement(
    facts: dict[str, Any],
    candidate: dict[str, Any],
    *,
    srl_slots: list[dict[str, Any]],
    ce_clone_slots: list[dict[str, Any]],
    authorization_region_sha256: str | None = None,
    namespace_suffix: str = "",
    srl_site_groups: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Allocate measured slots with one identity CE clone per SRL owner site."""
    plan = _verify_candidate(
        facts,
        candidate,
        authorization_region_sha256=authorization_region_sha256,
    )
    rewrite = facts.get("rewrite_plan")
    if not isinstance(rewrite, dict):
        raise ValueError("recurrence facts omit the exact rewrite plan")
    records = rewrite.get("cascade_stages")
    if not isinstance(records, list) or len(records) < 2:
        raise ValueError("rewrite plan omits placed cascade stage records")
    incumbent_regions = {
        item.get("clock_region") for item in records if isinstance(item, dict)
    }
    if None in incumbent_regions or not incumbent_regions:
        raise ValueError("cascade clock-region footprint is incomplete")
    source_xy = _site_xy(str(rewrite.get("source_dsp_site") or ""))
    reinjection_xy = _site_xy(str(records[-1].get("site") or ""))
    data_slots = _validated_oracle_slots(
        srl_slots,
        expected_kind="connected_srlc32e_placement_oracle_slot",
        facts=facts,
        incumbent_regions=incumbent_regions,
        require_incumbent_region=False,
    )
    data_slots = [
        slot for slot in data_slots if slot["preexisting_clock_leaf"] is True
    ]
    if not data_slots:
        raise ValueError("recurrence placement has no measured preexisting clock-leaf SRL slots")
    clone_slots = _validated_oracle_slots(
        ce_clone_slots,
        expected_kind="connected_ce_clone_placement_oracle_slot",
        facts=facts,
        incumbent_regions=incumbent_regions,
        require_incumbent_region=False,
    )

    slots_by_site: dict[str, list[dict[str, Any]]] = {}
    for slot in data_slots:
        slots_by_site.setdefault(slot["site"], []).append(slot)
    for slots in slots_by_site.values():
        slots.sort(key=lambda item: item["bel"])

    relay_bits = facts.get("relay_bits")
    endpoint = facts.get("relay_endpoint_contract")
    if (
        not isinstance(relay_bits, list)
        or len(relay_bits) != plan.state_width
        or not isinstance(endpoint, dict)
        or endpoint.get("primitive") != "FDRE"
        or endpoint.get("init") != 0
    ):
        raise ValueError("recurrence facts omit relay cells for counter reuse")
    counter_ffs = []
    site_owners: dict[str, str] = {}
    for bit in range(plan.counter_width):
        relay = relay_bits[bit]
        ff = relay.get("ff") if isinstance(relay, dict) else None
        if (
            not isinstance(ff, dict)
            or ff.get("type") != "FDRE"
            or not all(isinstance(ff.get(key), str) and ff[key] for key in ("cell", "site", "bel"))
            or _FF_BEL_RE.fullmatch(ff["bel"]) is None
        ):
            raise ValueError("recurrence relay cannot be retained as a zero-init counter FF")
        _site_xy(ff["site"])
        site_owners[ff["site"]] = "retained_counter_logic"
        counter_ffs.append({
            "cell": ff["cell"],
            "bit": bit,
            "site": ff["site"],
            "bel": ff["bel"],
            "reused_relay": True,
        })

    used_slots: set[tuple[str, str]] = set()
    groups = []
    segment_count = len(plan.srl_segment_depths)
    if srl_site_groups is not None:
        expected_group_ids = {
            f"segment_{index:02d}" for index in range(segment_count)
        }
        if (
            not isinstance(srl_site_groups, dict)
            or not srl_site_groups
            or any(
                not isinstance(site, str)
                or group_id not in expected_group_ids
                or site not in slots_by_site
                for site, group_id in srl_site_groups.items()
            )
            or set(srl_site_groups.values()) != expected_group_ids
        ):
            raise ValueError("recurrence global SRL site assignment is invalid")
        slots_by_site = {
            site: slots_by_site[site] for site in sorted(srl_site_groups)
        }
    certificate_id = str(candidate["authorization"]["certificate_id"])
    if namespace_suffix and re.fullmatch(r"[a-z0-9_]{1,16}", namespace_suffix) is None:
        raise ValueError("recurrence namespace suffix is invalid")
    namespace = f"fdagents_recur_{certificate_id[:12]}{namespace_suffix}"
    for segment_index, depth in enumerate(plan.srl_segment_depths):
        group_id = f"segment_{segment_index:02d}"
        target_numerator = segment_index + 1
        target_denominator = segment_count + 1
        target_x = (
            source_xy[0] * (target_denominator - target_numerator)
            + reinjection_xy[0] * target_numerator
        )
        target_y = (
            source_xy[1] * (target_denominator - target_numerator)
            + reinjection_xy[1] * target_numerator
        )
        site_order = sorted(
            (
                site for site in slots_by_site
                if site not in site_owners
                and (
                    srl_site_groups is None
                    or srl_site_groups.get(site) == group_id
                )
            ),
            key=lambda site: (
                *_balanced_hop_score(
                    slots_by_site[site][0]["source_distance"],
                    slots_by_site[site][0]["reinjection_distance"],
                    segment_index,
                    segment_count,
                ),
                -len(slots_by_site[site]),
                site,
            ),
        )
        selected = []
        for site in site_order:
            site_owners[site] = group_id
            for slot in slots_by_site[site]:
                key = (slot["site"], slot["bel"])
                if key in used_slots:
                    continue
                used_slots.add(key)
                selected.append(slot)
                if len(selected) == plan.state_width:
                    break
            if len(selected) == plan.state_width:
                break
        if len(selected) != plan.state_width:
            raise ValueError(f"insufficient site-owner-compatible SRL slots for {group_id}")
        if groups:
            selected = _match_adjacent_srl_slots(groups[-1]["srl_cells"], selected)

        x_sum = sum(slot["x"] for slot in selected)
        y_sum = sum(slot["y"] for slot in selected)
        site_ce_bits = {}
        srl_cells = []
        for bit, slot in enumerate(selected):
            ce_source_bit = site_ce_bits.setdefault(slot["site"], bit)
            srl_cells.append({
                "cell": f"{namespace}_s{segment_index:02d}_b{bit:03d}",
                "bit": bit,
                "site": slot["site"],
                "bel": slot["bel"],
                "depth": depth,
                "address": depth - 1,
                "ce_source_bit": ce_source_bit,
            })
        groups.append({
            "group_id": group_id,
            "segment_index": segment_index,
            "depth": depth,
            "target": {
                "x_numerator": target_x,
                "y_numerator": target_y,
                "denominator": target_denominator,
            },
            "center": {"x_sum": x_sum, "y_sum": y_sum, "count": len(selected)},
            "ce_owner": "identity_lut1_per_srl_site_per_segment",
            "ce_clones": [],
            "srl_cells": srl_cells,
        })

    if srl_site_groups is not None and any(
        site_owners.get(site) != group_id
        for site, group_id in srl_site_groups.items()
    ):
        raise ValueError("recurrence global SRL site assignment was not fully consumed")

    for group in groups:
        cells_by_site: dict[str, list[dict[str, Any]]] = {}
        for cell in group["srl_cells"]:
            cells_by_site.setdefault(cell["site"], []).append(cell)
        for owner_index, (owner_site, owner_cells) in enumerate(cells_by_site.items()):
            source_bits = {int(cell["ce_source_bit"]) for cell in owner_cells}
            if len(source_bits) != 1:
                raise ValueError("SRL owner site has inconsistent CE source bits")
            owner_x, owner_y = _site_xy(owner_site)
            candidates = sorted(
                (
                    slot for slot in clone_slots
                    if site_owners.get(slot["site"]) in {None, "auxiliary_logic"}
                    and (slot["site"], slot["bel"]) not in used_slots
                ),
                key=lambda slot: (
                    abs(slot["x"] - owner_x) + abs(slot["y"] - owner_y),
                    slot["site"],
                    slot["bel"],
                ),
            )
            if not candidates:
                raise ValueError("insufficient measured LUT1 slots for recurrence CE clones")
            slot = candidates[0]
            used_slots.add((slot["site"], slot["bel"]))
            site_owners[slot["site"]] = "auxiliary_logic"
            clone_name = (
                f"{namespace}_s{group['segment_index']:02d}_ce{owner_index:03d}"
            )
            for cell in owner_cells:
                cell["ce_clone_cell"] = clone_name
            group["ce_clones"].append({
                "cell": clone_name,
                "srl_owner_site": owner_site,
                "ce_source_bit": source_bits.pop(),
                "site": slot["site"],
                "bel": slot["bel"],
                "srl_cells": [cell["cell"] for cell in owner_cells],
            })

    support_roles = [
        *(f"counter_next_{bit}" for bit in range(plan.counter_width)),
        "counter_valid",
        *(f"transport_mask_{bit}" for bit in range(plan.state_width)),
    ]
    all_srl_cells = [cell for group in groups for cell in group["srl_cells"]]
    center_x = sum(cell["site"] and _site_xy(cell["site"])[0] for cell in all_srl_cells)
    center_y = sum(cell["site"] and _site_xy(cell["site"])[1] for cell in all_srl_cells)
    center_count = len(all_srl_cells)
    support_candidates = sorted(
        (
            slot for slot in clone_slots
            if site_owners.get(slot["site"]) in {None, "auxiliary_logic"}
            and (slot["site"], slot["bel"]) not in used_slots
        ),
        key=lambda slot: (
            abs(slot["x"] * center_count - center_x)
            + abs(slot["y"] * center_count - center_y),
            slot["site"], slot["bel"],
        ),
    )
    if len(support_candidates) < len(support_roles):
        raise ValueError("insufficient measured LUT slots for recurrence support logic")
    support_logic = []
    for role, slot in zip(support_roles, support_candidates):
        used_slots.add((slot["site"], slot["bel"]))
        site_owners[slot["site"]] = "auxiliary_logic"
        cell_name = f"{namespace}_{role}"
        support_logic.append({
            "cell": cell_name,
            "role": role,
            "site": slot["site"],
            "bel": slot["bel"],
        })

    selected_data_slots = [
        slot for slot in data_slots
        if (slot["site"], slot["bel"]) in used_slots
    ]
    allocated_regions = sorted({
        slot["clock_region"] for slot in selected_data_slots
    })
    manifest = {
        "kind": "recurrence_srl_placement_manifest",
        "version": PLACEMENT_MANIFEST_VERSION,
        "source": {
            "dcp_sha256": facts["design_sha256"],
            "region_sha256": facts["region_sha256"],
        },
        "authorization_id": certificate_id,
        "policy": {
            "site_owner_exclusive": True,
            "ce_routing": "identity_lut1_per_srl_site_per_segment",
            "clock_footprint": "preexisting_slice_clock_leaves_only",
            "slot_oracle": "vivado_connected_place_cell",
        },
        "clock_footprint": {
            "incumbent_regions": sorted(incumbent_regions),
            "allocated_regions": allocated_regions,
            "expanded": False,
            "new_slice_clock_site_count": len({
                slot["site"] for slot in selected_data_slots
                if slot["preexisting_clock_leaf"] is False
            }),
        },
        "site_owners": dict(sorted(site_owners.items())),
        "groups": groups,
        "support_logic": support_logic,
        "counter_ffs": counter_ffs,
        "authority": "pre_emission_only",
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def _allocate_recurrence_family_srl_site_groups(
    boundaries: list[dict[str, Any]],
    plan: RecurrenceCutPlan,
    by_region: dict[str, dict[str, Any]],
    reserved_counter_sites: set[str],
) -> list[dict[str, str]]:
    """Solve global site ownership, then deterministically improve physical locality."""
    import z3

    segment_count = len(plan.srl_segment_depths)
    capacities = []
    distances = []
    for boundary in boundaries:
        rewrite = boundary.get("rewrite_plan")
        stages = rewrite.get("cascade_stages") if isinstance(rewrite, dict) else None
        if not isinstance(stages, list) or len(stages) < 2:
            raise ValueError("recurrence family placement anchors are incomplete")
        incumbent_regions = {
            item.get("clock_region") for item in stages if isinstance(item, dict)
        }
        if None in incumbent_regions or not incumbent_regions:
            raise ValueError("recurrence family clock-region facts are incomplete")
        record = by_region[boundary["region_sha256"]]
        slots = _validated_oracle_slots(
            record.get("srl_slots", []),
            expected_kind="connected_srlc32e_placement_oracle_slot",
            facts=boundary,
            incumbent_regions=incumbent_regions,
            require_incumbent_region=False,
        )
        bels_by_site: dict[str, set[str]] = {}
        distance_by_site: dict[str, tuple[int, int]] = {}
        for slot in slots:
            if (
                slot["preexisting_clock_leaf"] is not True
                or slot["site"] in reserved_counter_sites
            ):
                continue
            bels_by_site.setdefault(slot["site"], set()).add(slot["bel"])
            anchor_distances = (
                slot["source_distance"], slot["reinjection_distance"]
            )
            previous = distance_by_site.setdefault(slot["site"], anchor_distances)
            if previous != anchor_distances:
                raise ValueError("recurrence oracle site has inconsistent anchor distances")
        capacities.append({
            site: len(bels) for site, bels in bels_by_site.items()
        })
        distances.append(distance_by_site)

    all_sites = sorted(set().union(*(set(items) for items in capacities)))
    group_count = len(boundaries) * segment_count
    if not all_sites or group_count < 2:
        raise ValueError("recurrence family has no global SRL site assignment domain")
    group_boundary = [
        boundary_index
        for boundary_index in range(len(boundaries))
        for _segment_index in range(segment_count)
    ]
    owners = [z3.Int(f"recur_site_owner_{index}") for index in range(len(all_sites))]
    allowed_by_site = []
    solver = z3.Solver()
    solver.set(timeout=30000)
    for site, owner in zip(all_sites, owners):
        allowed = [
            group_index
            for group_index, boundary_index in enumerate(group_boundary)
            if site in capacities[boundary_index]
        ]
        allowed_by_site.append(allowed)
        solver.add(z3.Or(owner == -1, *(owner == group for group in allowed)))

    group_capacity_exprs = []
    for group_index, boundary_index in enumerate(group_boundary):
        capacity = z3.Sum(*(
            z3.If(
                owners[site_index] == group_index,
                capacities[boundary_index].get(site, 0),
                0,
            )
            for site_index, site in enumerate(all_sites)
        ))
        group_capacity_exprs.append(capacity)
        solver.add(
            capacity >= plan.state_width,
            capacity <= plan.state_width + 7,
        )
    result = solver.check()
    if result != z3.sat:
        detail = solver.reason_unknown() if result == z3.unknown else "unsatisfiable"
        raise ValueError(f"insufficient globally assignable SRL sites: {detail}")
    model = solver.model()
    assigned = [model.eval(owner).as_long() for owner in owners]

    def site_capacity(site_index: int, group_index: int) -> int:
        return capacities[group_boundary[group_index]].get(all_sites[site_index], 0)

    costs = []
    for site_index, site in enumerate(all_sites):
        site_costs = []
        for group_index, boundary_index in enumerate(group_boundary):
            segment_index = group_index % segment_count
            source_distance, reinjection_distance = distances[boundary_index].get(
                site, (100000, 100000)
            )
            hop_cost, detour_cost = _balanced_hop_score(
                source_distance,
                reinjection_distance,
                segment_index,
                segment_count,
            )
            capacity = site_capacity(site_index, group_index)
            site_costs.append(
                (hop_cost * capacity, detour_cost * capacity, 1)
            )
        costs.append(site_costs)

    group_capacities = [0] * group_count
    for site_index, group_index in enumerate(assigned):
        if group_index >= 0:
            group_capacities[group_index] += site_capacity(site_index, group_index)

    def valid_capacity(value: int) -> bool:
        return plan.state_width <= value <= plan.state_width + 7

    def owner_cost(site_index: int, group_index: int) -> tuple[int, int, int]:
        return (0, 0, 0) if group_index < 0 else costs[site_index][group_index]

    def cost_delta(
        additions: tuple[tuple[int, int, int], ...],
        removals: tuple[tuple[int, int, int], ...],
    ) -> tuple[int, int, int]:
        return tuple(
            sum(cost[index] for cost in additions)
            - sum(cost[index] for cost in removals)
            for index in range(3)
        )

    for _iteration in range(512):
        best = None
        for site_index, current in enumerate(assigned):
            for replacement in [-1, *allowed_by_site[site_index]]:
                if replacement == current:
                    continue
                changed = {}
                if current >= 0:
                    changed[current] = (
                        group_capacities[current]
                        - site_capacity(site_index, current)
                    )
                if replacement >= 0:
                    changed[replacement] = changed.get(
                        replacement, group_capacities[replacement]
                    ) + site_capacity(site_index, replacement)
                if not all(valid_capacity(value) for value in changed.values()):
                    continue
                delta = cost_delta(
                    (owner_cost(site_index, replacement),),
                    (owner_cost(site_index, current),),
                )
                candidate = (
                    *delta, 0, all_sites[site_index], replacement,
                    ("move", site_index, replacement, changed),
                )
                if delta < (0, 0, 0) and (
                    best is None or candidate[:-1] < best[:-1]
                ):
                    best = candidate
        for left in range(len(all_sites)):
            left_owner = assigned[left]
            for right in range(left + 1, len(all_sites)):
                right_owner = assigned[right]
                if left_owner == right_owner:
                    continue
                if (
                    (right_owner >= 0 and right_owner not in allowed_by_site[left])
                    or (left_owner >= 0 and left_owner not in allowed_by_site[right])
                ):
                    continue
                changed = {}
                for site_index, old_owner, new_owner in (
                    (left, left_owner, right_owner),
                    (right, right_owner, left_owner),
                ):
                    if old_owner >= 0:
                        changed[old_owner] = changed.get(
                            old_owner, group_capacities[old_owner]
                        ) - site_capacity(site_index, old_owner)
                    if new_owner >= 0:
                        changed[new_owner] = changed.get(
                            new_owner, group_capacities[new_owner]
                        ) + site_capacity(site_index, new_owner)
                if not all(valid_capacity(value) for value in changed.values()):
                    continue
                delta = cost_delta(
                    (
                        owner_cost(left, right_owner),
                        owner_cost(right, left_owner),
                    ),
                    (
                        owner_cost(left, left_owner),
                        owner_cost(right, right_owner),
                    ),
                )
                candidate = (
                    *delta, 1, all_sites[left], all_sites[right],
                    ("swap", left, right, changed),
                )
                if delta < (0, 0, 0) and (
                    best is None or candidate[:-1] < best[:-1]
                ):
                    best = candidate
        if best is None:
            break
        action = best[-1]
        if action[0] == "move":
            _kind, site_index, replacement, changed = action
            assigned[site_index] = replacement
        else:
            _kind, left, right, changed = action
            assigned[left], assigned[right] = assigned[right], assigned[left]
        for group_index, capacity in changed.items():
            group_capacities[group_index] = capacity

    assignments = [dict() for _boundary in boundaries]
    for site, group_index in zip(all_sites, assigned):
        if group_index < 0:
            continue
        boundary_index = group_boundary[group_index]
        segment_index = group_index % segment_count
        assignments[boundary_index][site] = f"segment_{segment_index:02d}"
    if any(
        not valid_capacity(group_capacities[group_index])
        or not any(
            group_id == f"segment_{group_index % segment_count:02d}"
            for group_id in assignments[group_boundary[group_index]].values()
        )
        for group_index in range(group_count)
    ):
        raise ValueError("recurrence global SRL site assignment is incomplete")
    return assignments


def allocate_recurrence_family_srl_placements(
    family_facts: dict[str, Any],
    candidate: dict[str, Any],
    *,
    oracle_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allocate every family member with one global site/BEL ownership set."""
    family_sha = str(family_facts.get("family_contract_sha256") or "")
    authorization = candidate.get("authorization") or {}
    if not verify_rewrite_authorization(
        authorization,
        expected_dcp_sha256=str(family_facts.get("design_sha256") or ""),
        expected_region_sha256=family_sha,
    ):
        raise ValueError("recurrence family authorization replay failed")
    boundaries = family_facts.get("boundaries")
    if not isinstance(boundaries, list) or len(boundaries) < 2:
        raise ValueError("recurrence family boundaries are incomplete")
    plan = _candidate_plan(candidate)
    reserved_counter_sites = set()
    for boundary in boundaries:
        relay_bits = boundary.get("relay_bits") if isinstance(boundary, dict) else None
        if not isinstance(relay_bits, list) or len(relay_bits) < plan.counter_width:
            raise ValueError("recurrence family relay facts cannot reserve counter sites")
        for relay in relay_bits[:plan.counter_width]:
            ff = relay.get("ff") if isinstance(relay, dict) else None
            site = ff.get("site") if isinstance(ff, dict) else None
            if not isinstance(site, str) or not site:
                raise ValueError("recurrence family retained counter site is invalid")
            _site_xy(site)
            reserved_counter_sites.add(site)
    if not isinstance(oracle_slots, list):
        raise ValueError("recurrence family oracle records must be a list")
    by_region = {}
    for index, record in enumerate(oracle_slots):
        if not isinstance(record, dict) or set(record) != {
            "region_sha256", "srl_slots", "ce_clone_slots",
        }:
            raise ValueError(f"recurrence family oracle record {index} is invalid")
        region = record.get("region_sha256")
        if not isinstance(region, str) or not region or region in by_region:
            raise ValueError("recurrence family oracle regions are invalid or duplicated")
        by_region[region] = record
    if set(by_region) != {boundary.get("region_sha256") for boundary in boundaries}:
        raise ValueError("recurrence family oracle coverage is incomplete")
    assigned_site_groups = _allocate_recurrence_family_srl_site_groups(
        boundaries, plan, by_region, reserved_counter_sites
    )
    used_sites: set[str] = set()
    used_slots: set[tuple[str, str]] = set()
    placements = []
    for index, boundary in enumerate(boundaries):
        record = by_region[boundary["region_sha256"]]
        srl_slots = [
            slot for slot in record.get("srl_slots", [])
            if slot.get("site") not in used_sites
            and slot.get("site") not in reserved_counter_sites
            and slot.get("site") in assigned_site_groups[index]
            and (slot.get("site"), slot.get("bel")) not in used_slots
        ]
        clone_slots = [
            slot for slot in record.get("ce_clone_slots", [])
            if slot.get("site") not in used_sites
            and slot.get("site") not in reserved_counter_sites
            and (slot.get("site"), slot.get("bel")) not in used_slots
        ]
        placement = allocate_recurrence_srl_placement(
            boundary,
            candidate,
            srl_slots=srl_slots,
            ce_clone_slots=clone_slots,
            authorization_region_sha256=family_sha,
            namespace_suffix=f"_b{index:02d}",
            srl_site_groups=assigned_site_groups[index],
        )
        if used_sites & set(placement["site_owners"]):
            raise ValueError("recurrence family placement reuses a retained physical site")
        placements.append({
            "boundary_region_sha256": boundary["region_sha256"],
            "placement": placement,
        })
        used_sites.update(placement["site_owners"])
        for group in placement["groups"]:
            used_slots.update(
                (cell["site"], cell["bel"]) for cell in group["srl_cells"]
            )
            used_slots.update(
                (item["site"], item["bel"]) for item in group["ce_clones"]
            )
        used_slots.update(
            (item["site"], item["bel"]) for item in placement["support_logic"]
        )
    manifest = {
        "kind": "recurrence_family_placement_manifest",
        "version": FAMILY_PLACEMENT_MANIFEST_VERSION,
        "source": {
            "dcp_sha256": family_facts["design_sha256"],
            "region_sha256": family_sha,
        },
        "family_id": family_facts["family_id"],
        "authorization_id": authorization["certificate_id"],
        "boundary_count": len(placements),
        "placements": placements,
        "global_site_owners": sorted(used_sites),
        "authority": "pre_emission_only",
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def _validate_recurrence_placement_structure(
    placement: dict[str, Any],
    plan: RecurrenceCutPlan,
    facts: dict[str, Any],
) -> None:
    """Reject hash-valid manifests that do not describe the exact clone topology."""
    if placement.get("version") != PLACEMENT_MANIFEST_VERSION:
        raise ValueError("placement manifest version does not provide CE clones")
    if placement.get("authority") != "pre_emission_only" or placement.get("policy") != {
        "site_owner_exclusive": True,
        "ce_routing": "identity_lut1_per_srl_site_per_segment",
        "clock_footprint": "preexisting_slice_clock_leaves_only",
        "slot_oracle": "vivado_connected_place_cell",
    }:
        raise ValueError("placement policy is incomplete")
    groups = placement.get("groups")
    if not isinstance(groups, list) or len(groups) != len(plan.srl_segment_depths):
        raise ValueError("placement does not cover every SRL segment")

    relay_bits = facts.get("relay_bits")
    endpoint = facts.get("relay_endpoint_contract")
    if (
        not isinstance(relay_bits, list)
        or len(relay_bits) != plan.state_width
        or not isinstance(endpoint, dict)
        or endpoint.get("primitive") != "FDRE"
        or endpoint.get("init") != 0
    ):
        raise ValueError("placement replay lacks relay facts")
    generated_cells: set[str] = set()
    lut_slots: set[tuple[str, str]] = set()
    expected_site_owners: dict[str, str] = {}
    owner_cells_by_group = []
    for segment_index, (group, depth) in enumerate(
        zip(groups, plan.srl_segment_depths)
    ):
        if not isinstance(group, dict) or set(group) != {
            "group_id", "segment_index", "depth", "target", "center", "ce_owner",
            "ce_clones", "srl_cells",
        }:
            raise ValueError("placement SRL group schema is invalid")
        group_id = f"segment_{segment_index:02d}"
        if (
            group.get("group_id") != group_id
            or group.get("segment_index") != segment_index
            or group.get("depth") != depth
            or group.get("ce_owner")
            != "identity_lut1_per_srl_site_per_segment"
        ):
            raise ValueError("placement SRL group identity is invalid")
        cells = group.get("srl_cells")
        if not isinstance(cells, list) or len(cells) != plan.state_width:
            raise ValueError("placement SRL group width is invalid")
        if [cell.get("bit") for cell in cells if isinstance(cell, dict)] != list(
            range(plan.state_width)
        ):
            raise ValueError("placement SRL bit coverage is invalid")
        cells_by_site: dict[str, list[dict[str, Any]]] = {}
        for cell in cells:
            if not isinstance(cell, dict) or set(cell) != {
                "cell", "bit", "site", "bel", "depth", "address", "ce_source_bit",
                "ce_clone_cell",
            }:
                raise ValueError("placement SRL cell schema is invalid")
            name = cell.get("cell")
            site = cell.get("site")
            bel = cell.get("bel")
            source_bit = cell.get("ce_source_bit")
            if (
                not isinstance(name, str)
                or not name
                or name in generated_cells
                or not isinstance(site, str)
                or _LUT_BEL_RE.fullmatch(str(bel or "")) is None
                or cell.get("depth") != depth
                or cell.get("address") != depth - 1
                or isinstance(source_bit, bool)
                or not isinstance(source_bit, int)
                or not 0 <= source_bit < plan.state_width
                or not isinstance(cell.get("ce_clone_cell"), str)
                or not cell["ce_clone_cell"]
            ):
                raise ValueError("placement SRL cell is invalid")
            _site_xy(site)
            slot = (site, bel)
            if slot in lut_slots:
                raise ValueError("placement reuses an SRL site/BEL")
            owner = expected_site_owners.setdefault(site, group_id)
            if owner != group_id:
                raise ValueError("placement reuses an SRL site across segments")
            generated_cells.add(name)
            lut_slots.add(slot)
            cells_by_site.setdefault(site, []).append(cell)
        owner_cells_by_group.append(cells_by_site)

    for group, cells_by_site in zip(groups, owner_cells_by_group):
        clones = group.get("ce_clones")
        if not isinstance(clones, list) or len(clones) != len(cells_by_site):
            raise ValueError("placement CE clone coverage is invalid")
        clones_by_owner = {}
        for clone in clones:
            if not isinstance(clone, dict) or set(clone) != {
                "cell", "srl_owner_site", "ce_source_bit", "site", "bel", "srl_cells",
            }:
                raise ValueError("placement CE clone schema is invalid")
            name = clone.get("cell")
            owner_site = clone.get("srl_owner_site")
            site = clone.get("site")
            bel = clone.get("bel")
            source_bit = clone.get("ce_source_bit")
            assigned = clone.get("srl_cells")
            if (
                not isinstance(name, str)
                or not name
                or name in generated_cells
                or owner_site not in cells_by_site
                or owner_site in clones_by_owner
                or not isinstance(site, str)
                or _LUT_BEL_RE.fullmatch(str(bel or "")) is None
                or isinstance(source_bit, bool)
                or not isinstance(source_bit, int)
                or not 0 <= source_bit < plan.state_width
                or not isinstance(assigned, list)
            ):
                raise ValueError("placement CE clone is invalid")
            _site_xy(site)
            owner_cells = cells_by_site[owner_site]
            if (
                assigned != [cell["cell"] for cell in owner_cells]
                or {cell["ce_source_bit"] for cell in owner_cells} != {source_bit}
                or {cell["ce_clone_cell"] for cell in owner_cells} != {name}
            ):
                raise ValueError("placement CE clone ownership is inconsistent")
            slot = (site, bel)
            if slot in lut_slots or site in {
                owner_site for owner_site in expected_site_owners
                if expected_site_owners[owner_site].startswith("segment_")
            }:
                raise ValueError("placement CE clone collides with SRL data placement")
            current_owner = expected_site_owners.setdefault(site, "auxiliary_logic")
            if current_owner != "auxiliary_logic":
                raise ValueError("placement CE clone site ownership is invalid")
            generated_cells.add(name)
            lut_slots.add(slot)
            clones_by_owner[owner_site] = clone
        if set(clones_by_owner) != set(cells_by_site):
            raise ValueError("placement CE clone owner coverage is incomplete")

    support = placement.get("support_logic")
    expected_roles = [
        *(f"counter_next_{bit}" for bit in range(plan.counter_width)),
        "counter_valid",
        *(f"transport_mask_{bit}" for bit in range(plan.state_width)),
    ]
    if not isinstance(support, list) or [
        item.get("role") for item in support if isinstance(item, dict)
    ] != expected_roles:
        raise ValueError("placement support-role coverage is invalid")
    support_by_role = {}
    for item in support:
        if not isinstance(item, dict) or set(item) != {"cell", "role", "site", "bel"}:
            raise ValueError("placement support logic schema is invalid")
        name = item.get("cell")
        role = item.get("role")
        site = item.get("site")
        bel = item.get("bel")
        if (
            not isinstance(name, str)
            or not name
            or name in generated_cells
            or role in support_by_role
            or not isinstance(site, str)
            or _LUT_BEL_RE.fullmatch(str(bel or "")) is None
        ):
            raise ValueError("placement support logic is invalid")
        _site_xy(site)
        slot = (site, bel)
        if slot in lut_slots:
            raise ValueError("placement support logic reuses a LUT site/BEL")
        current_owner = expected_site_owners.setdefault(site, "auxiliary_logic")
        if current_owner != "auxiliary_logic":
            raise ValueError("placement support logic collides with SRL data placement")
        generated_cells.add(name)
        lut_slots.add(slot)
        support_by_role[role] = item

    counter_ffs = placement.get("counter_ffs")
    if not isinstance(counter_ffs, list) or [
        item.get("bit") for item in counter_ffs if isinstance(item, dict)
    ] != list(range(plan.counter_width)):
        raise ValueError("placement counter FF coverage is invalid")
    ff_slots = set()
    for item in counter_ffs:
        if not isinstance(item, dict) or set(item) != {
            "cell", "bit", "site", "bel", "reused_relay",
        }:
            raise ValueError("placement counter FF schema is invalid")
        name = item.get("cell")
        bit = item["bit"]
        relay = relay_bits[bit]
        ff = relay.get("ff") if isinstance(relay, dict) else None
        site = item.get("site")
        bel = item.get("bel")
        if (
            not isinstance(name, str)
            or not name
            or name in generated_cells
            or not isinstance(ff, dict)
            or item.get("reused_relay") is not True
            or name != ff.get("cell")
            or site != ff.get("site")
            or bel != ff.get("bel")
            or _FF_BEL_RE.fullmatch(str(bel or "")) is None
            or (site, bel) in ff_slots
        ):
            raise ValueError("placement counter FF is invalid")
        current_owner = expected_site_owners.setdefault(site, "retained_counter_logic")
        if current_owner != "retained_counter_logic":
            raise ValueError("retained counter FF collides with generated placement")
        generated_cells.add(name)
        ff_slots.add((site, bel))

    if placement.get("site_owners") != dict(sorted(expected_site_owners.items())):
        raise ValueError("placement site ownership does not match its cells")
    footprint = placement.get("clock_footprint")
    if (
        not isinstance(footprint, dict)
        or footprint.get("expanded") is not False
        or footprint.get("new_slice_clock_site_count") != 0
    ):
        raise ValueError("placement clock footprint is incomplete")


def build_recurrence_topology_manifest(
    facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
    *,
    authorization_region_sha256: str | None = None,
) -> dict[str, Any]:
    """Describe the exact intended graph while withholding mutation authority."""
    plan = _verify_candidate(
        facts,
        candidate,
        authorization_region_sha256=authorization_region_sha256,
    )
    claimed = placement.get("manifest_sha256") if isinstance(placement, dict) else None
    payload = dict(placement) if isinstance(placement, dict) else {}
    payload.pop("manifest_sha256", None)
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "recurrence_srl_placement_manifest"
        or placement.get("version") != PLACEMENT_MANIFEST_VERSION
        or placement.get("source") != {
            "dcp_sha256": facts.get("design_sha256"),
            "region_sha256": facts.get("region_sha256"),
        }
        or placement.get("authorization_id")
        != candidate["authorization"].get("certificate_id")
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
    ):
        raise ValueError("placement manifest is stale, modified, or cross-plan")
    _validate_recurrence_placement_structure(placement, plan, facts)
    groups = placement.get("groups")

    rewrite = facts.get("rewrite_plan") or {}
    relay_bits = facts.get("relay_bits") or []
    source_preg = rewrite.get("source_preg_control_contract")
    source_preg_valid = False
    if isinstance(source_preg, dict):
        source_preg_payload = dict(source_preg)
        source_preg_digest = source_preg_payload.pop("contract_sha256", None)
        source_preg_valid = (
            source_preg.get("kind") == "source_dsp_preg_control_contract"
            and source_preg.get("current_preg") in {0, 1}
            and source_preg.get("target_preg") == 1
            and source_preg.get("preg_action") in {"enable", "preserve"}
            and source_preg.get("exclusive_external_relay_replacement") is True
            and isinstance(source_preg_digest, str)
            and _digest(source_preg_payload) == source_preg_digest
        )
    arithmetic = rewrite.get("arithmetic_rewrite_contract")
    arithmetic_valid = False
    if isinstance(arithmetic, dict):
        arithmetic_payload = dict(arithmetic)
        arithmetic_digest = arithmetic_payload.pop("contract_sha256", None)
        reinjection = arithmetic.get("reinjection") or {}
        arithmetic_valid = (
            arithmetic.get("kind") == "dsp_recurrence_arithmetic_rewrite_contract"
            and reinjection.get("dsp") == rewrite.get("reinjection_dsp")
            and reinjection.get("operation") == "M_PLUS_PCIN_PLUS_C"
            and reinjection.get("target_muxes") == {"x": 1, "y": 1, "z": 1, "w": 3}
            and reinjection.get("current_creg") in {0, 1}
            and reinjection.get("target_creg") == 0
            and reinjection.get("creg_action") in {"disable", "preserve"}
            and reinjection.get("creg_action")
            == ("disable" if reinjection.get("current_creg") == 1 else "preserve")
            and isinstance(arithmetic_digest, str)
            and _digest(arithmetic_payload) == arithmetic_digest
        )
    missing_contracts = ["connected_temporary_primitive_slot_oracle_replay"]
    if not arithmetic_valid:
        missing_contracts.insert(
            0, "reinjection_dsp_exact_alumode_opmode_inmode_creg_contract"
        )
    if not source_preg_valid:
        missing_contracts.insert(0, "source_dsp_preg_clock_ce_reset_connectivity")
    retained_counter_cells = {
        item["cell"] for item in placement["counter_ffs"]
        if item.get("reused_relay") is True
    }
    manifest = {
        "kind": "dsp_recurrence_cut_topology_manifest",
        "version": TOPOLOGY_MANIFEST_VERSION,
        "source": dict(placement["source"]),
        "authorization_id": placement["authorization_id"],
        "placement_manifest_sha256": claimed,
        "source_dsp": facts.get("source_dsp"),
        "boundary_sink_dsp": facts.get("sink_dsp"),
        "reinjection_dsp": rewrite.get("reinjection_dsp"),
        "controls": dict(facts.get("controls") or {}),
        "transport": {
            "width": plan.state_width,
            "valid_transactions": plan.transport_valid_transactions,
            "segments": [
                {
                    "index": group["segment_index"],
                    "depth": group["depth"],
                    "ce_owner": group["ce_owner"],
                    "ce_clones": [
                        {
                            **clone,
                            "reference": "LUT1",
                            "init": "2'h2",
                            "source_net": facts["controls"]["ce_net"],
                            "sink_pins": [
                                f"{cell}/CE" for cell in clone["srl_cells"]
                            ],
                        }
                        for clone in group["ce_clones"]
                    ],
                    "cells": group["srl_cells"],
                }
                for group in groups
            ],
            "source_pins": [
                f"{facts.get('source_dsp')}/DSP_OUTPUT_INST/P[{bit}]"
                for bit in range(plan.state_width)
            ],
            "reinjection_pins": [
                f"{rewrite.get('reinjection_dsp')}/DSP_C_DATA_INST/C[{bit}]"
                for bit in range(plan.state_width)
            ],
            "sign_extension_pins": [
                f"{rewrite.get('reinjection_dsp')}/DSP_C_DATA_INST/C[{bit}]"
                for bit in range(plan.state_width, int(facts.get("dsp_c_width") or 0))
            ],
        },
        "remove_relay_cells": [
            item.get("ff", {}).get("cell")
            for item in relay_bits
            if isinstance(item, dict)
            and item.get("ff", {}).get("cell") not in retained_counter_cells
        ],
        "reset_mask": {
            "counter_width": plan.counter_width,
            "mask_valid_transactions": plan.mask_valid_transactions,
            "reset_value": plan.reset_mask_value,
            "retained_counter_cells": sorted(retained_counter_cells),
        },
        "source_preg_control_contract": source_preg if source_preg_valid else None,
        "arithmetic_rewrite_contract": arithmetic if arithmetic_valid else None,
        "missing_emission_contracts": missing_contracts,
        "emission_eligible": False,
        "authority": "pre_emission_only",
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def build_recurrence_family_topology_manifest(
    family_facts: dict[str, Any],
    candidate: dict[str, Any],
    placement: dict[str, Any],
) -> dict[str, Any]:
    """Bind every intended boundary topology into one atomic family manifest."""
    claimed = placement.get("manifest_sha256") if isinstance(placement, dict) else None
    payload = dict(placement) if isinstance(placement, dict) else {}
    payload.pop("manifest_sha256", None)
    family_sha = str(family_facts.get("family_contract_sha256") or "")
    if (
        not isinstance(placement, dict)
        or placement.get("kind") != "recurrence_family_placement_manifest"
        or placement.get("version") != FAMILY_PLACEMENT_MANIFEST_VERSION
        or placement.get("source") != {
            "dcp_sha256": family_facts.get("design_sha256"),
            "region_sha256": family_sha,
        }
        or placement.get("authorization_id")
        != candidate.get("authorization", {}).get("certificate_id")
        or not isinstance(claimed, str)
        or _digest(payload) != claimed
    ):
        raise ValueError("recurrence family placement is stale or modified")
    boundaries = {
        item["region_sha256"]: item for item in family_facts.get("boundaries", [])
    }
    records = placement.get("placements")
    if not isinstance(records, list):
        raise ValueError("recurrence family placement members are invalid")
    topology_members = []
    seen_regions = set()
    used_sites = set()
    used_slots = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "boundary_region_sha256", "placement",
        }:
            raise ValueError("recurrence family placement member is invalid")
        region = record.get("boundary_region_sha256")
        if region not in boundaries or region in seen_regions:
            raise ValueError("recurrence family placement has an unknown boundary")
        seen_regions.add(region)
        child = record.get("placement")
        if not isinstance(child, dict):
            raise ValueError("recurrence family child placement is invalid")
        child_sites = set(child.get("site_owners", {}))
        if used_sites & child_sites:
            raise ValueError("recurrence family placement reuses a physical site")
        used_sites.update(child_sites)
        child_slots = {
            (cell["site"], cell["bel"])
            for group in child.get("groups", [])
            for cell in [*group.get("srl_cells", []), *group.get("ce_clones", [])]
        }
        child_slots.update(
            (item["site"], item["bel"])
            for item in child.get("support_logic", [])
        )
        if used_slots & child_slots:
            raise ValueError("recurrence family placement reuses a physical BEL")
        used_slots.update(child_slots)
        topology_members.append({
            "boundary_region_sha256": region,
            "topology": build_recurrence_topology_manifest(
                boundaries[region],
                candidate,
                child,
                authorization_region_sha256=family_sha,
            ),
        })
    if (
        seen_regions != set(boundaries)
        or len(topology_members) != family_facts.get("boundary_count")
        or placement.get("global_site_owners") != sorted(used_sites)
    ):
        raise ValueError("recurrence family topology coverage is incomplete")
    manifest = {
        "kind": "dsp_recurrence_family_topology_manifest",
        "version": FAMILY_TOPOLOGY_MANIFEST_VERSION,
        "source": dict(placement["source"]),
        "family_id": family_facts["family_id"],
        "authorization_id": placement["authorization_id"],
        "placement_manifest_sha256": claimed,
        "members": topology_members,
        "composition_contract_sha256": family_facts["composition_contract"][
            "contract_sha256"
        ],
        "authority": "pre_emission_only",
        "emission_eligible": all(
            member["topology"].get("missing_emission_contracts") == [
                "connected_temporary_primitive_slot_oracle_replay"
            ]
            for member in topology_members
        ),
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest
