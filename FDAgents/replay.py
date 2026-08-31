"""Pure, anonymous one-hour replay policy mechanics.

This module deliberately contains no I/O, clocks, LLM client, candidate graph,
or agent-loop integration.  Callers provide elapsed time, aggregate features,
and mutation outcomes; every operation returns immutable typed state.  In
particular, replay actions name only an immutable-incumbent role.  They never
carry a checkpoint identity or allow the selected action to replace that role.

The public action output is intentionally not a ``ResolvedAction``.  It is a
closed replay template that can be adapted to the capability layer when the
agent owns replay integration.  Keeping that boundary here prevents config,
LLM output, or observations from supplying target or parameter mechanics.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from numbers import Real
from types import MappingProxyType
from typing import Optional


POLICY_ID = "generic_basin_then_semantics_v1"
POLICY_VERSION = 2
SCHEMA_VERSION = 1
SERIALIZATION_SCHEMA_VERSION = SCHEMA_VERSION
IMMUTABLE_INCUMBENT = "immutable_incumbent"
IMMUTABLE_INCUMBENT_INVARIANT = True
EXECUTION_STATUSES = ("not_started", "started", "unknown")
SIBLING_STATUSES = ("legal", "illegal", "unknown")

PHASES = (
    "characterize",
    "basin",
    "semantic_preflight",
    "semantics",
    "closeout",
    "submission",
    "complete",
)

DEFAULT_DEADLINES_S = MappingProxyType({
    "characterize": 300.0,
    "basin": 1200.0,
    "semantics": 2700.0,
    "closeout": 3000.0,
    "submission": 3600.0,
})


class ReplayPolicyError(ValueError):
    """Raised when replay state or a replay selection is invalid."""


# A shorter alias is convenient for callers and keeps failures policy-specific.
PolicyError = ReplayPolicyError


def _finite_number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _require_finite_number(name: str, value: object) -> float:
    result = _finite_number(value)
    if result is None:
        raise ReplayPolicyError(f"{name} must be a finite number")
    return result


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReplayPolicyError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayPolicyError(f"{name} must be a nonnegative integer")
    return value


def _require_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ReplayPolicyError(f"{name} must be boolean")
    return value


def _require_string(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ReplayPolicyError(f"{name} must be a string")
    return value


def _require_exact_fields(
    raw: object,
    expected: frozenset[str],
    name: str,
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise ReplayPolicyError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in raw):
        raise ReplayPolicyError(f"{name} field names must be strings")
    actual = set(raw)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ReplayPolicyError(f"{name} has {' and '.join(details)} field(s)")
    return raw


def _validate_envelope(raw: Mapping[str, object], name: str) -> None:
    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ReplayPolicyError(
            f"{name} schema_version must be {SCHEMA_VERSION}"
        )
    if raw["policy_id"] != POLICY_ID or not isinstance(raw["policy_id"], str):
        raise ReplayPolicyError(f"{name} has the wrong policy ID")
    policy_version = raw["policy_version"]
    if type(policy_version) is not int or policy_version != POLICY_VERSION:
        raise ReplayPolicyError(
            f"{name} policy_version must be {POLICY_VERSION}"
        )


def _validate_object_envelope(
    schema_version: object,
    policy_id: object,
    policy_version: object,
    name: str,
) -> None:
    _validate_envelope(
        {
            "schema_version": schema_version,
            "policy_id": policy_id,
            "policy_version": policy_version,
        },
        name,
    )


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _require_digest(name: str, value: object) -> str:
    digest = _require_string(name, value)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ReplayPolicyError(f"{name} must be a lowercase SHA-256 digest")
    return digest


@dataclass(frozen=True)
class ReplayDeadlines:
    """Absolute elapsed-time phase deadlines for one replay run."""

    characterize: float = DEFAULT_DEADLINES_S["characterize"]
    basin: float = DEFAULT_DEADLINES_S["basin"]
    semantics: float = DEFAULT_DEADLINES_S["semantics"]
    closeout: float = DEFAULT_DEADLINES_S["closeout"]
    submission: float = DEFAULT_DEADLINES_S["submission"]

    def __post_init__(self) -> None:
        values = tuple(
            _require_finite_number(f"deadline {name}", getattr(self, name))
            for name in (
                "characterize",
                "basin",
                "semantics",
                "closeout",
                "submission",
            )
        )
        if values[0] <= 0 or any(
            left >= right for left, right in zip(values, values[1:])
        ):
            raise ReplayPolicyError(
                "replay deadlines must be positive and strictly increasing"
            )
        for name, value in zip(
            ("characterize", "basin", "semantics", "closeout", "submission"),
            values,
        ):
            object.__setattr__(self, name, value)

    def as_dict(self) -> dict[str, float]:
        return {
            "characterize": self.characterize,
            "basin": self.basin,
            "semantics": self.semantics,
            "closeout": self.closeout,
            "submission": self.submission,
        }


@dataclass(frozen=True)
class ReplayThresholds:
    """Generic feature thresholds; none identify a benchmark or target."""

    minimum_feature_count: int = 2
    route_dominated_min_ratio: float = 0.55
    high_util_min_pct: float = 70.0
    deep_logic_min_levels: int = 8
    high_fanout_min_count: int = 1
    clock_fallback_max_closure_ratio: float = -0.05
    semantic_preflight_max_age_s: float = 300.0
    low_gain_max_delta_mhz: float = 0.01
    saturation_minimum_consecutive: int = 2

    def __post_init__(self) -> None:
        minimum_features = _require_positive_int(
            "minimum_feature_count", self.minimum_feature_count
        )
        deep_logic = _require_positive_int(
            "deep_logic_min_levels", self.deep_logic_min_levels
        )
        high_fanout = _require_positive_int(
            "high_fanout_min_count", self.high_fanout_min_count
        )
        saturation_minimum = _require_positive_int(
            "saturation_minimum_consecutive",
            self.saturation_minimum_consecutive,
        )
        route_ratio = _require_finite_number(
            "route_dominated_min_ratio", self.route_dominated_min_ratio
        )
        high_util = _require_finite_number(
            "high_util_min_pct", self.high_util_min_pct
        )
        closure = _require_finite_number(
            "clock_fallback_max_closure_ratio",
            self.clock_fallback_max_closure_ratio,
        )
        preflight_age = _require_finite_number(
            "semantic_preflight_max_age_s",
            self.semantic_preflight_max_age_s,
        )
        low_gain = _require_finite_number(
            "low_gain_max_delta_mhz", self.low_gain_max_delta_mhz
        )
        if not 0.0 <= route_ratio <= 1.0:
            raise ReplayPolicyError("route_dominated_min_ratio must be in [0, 1]")
        if not 0.0 <= high_util <= 100.0:
            raise ReplayPolicyError("high_util_min_pct must be in [0, 100]")
        if preflight_age <= 0.0:
            raise ReplayPolicyError(
                "semantic_preflight_max_age_s must be positive"
            )
        if low_gain < 0.0:
            raise ReplayPolicyError("low_gain_max_delta_mhz must be nonnegative")
        object.__setattr__(self, "minimum_feature_count", minimum_features)
        object.__setattr__(self, "deep_logic_min_levels", deep_logic)
        object.__setattr__(self, "high_fanout_min_count", high_fanout)
        object.__setattr__(
            self, "saturation_minimum_consecutive", saturation_minimum
        )
        object.__setattr__(self, "route_dominated_min_ratio", route_ratio)
        object.__setattr__(self, "high_util_min_pct", high_util)
        object.__setattr__(self, "clock_fallback_max_closure_ratio", closure)
        object.__setattr__(self, "semantic_preflight_max_age_s", preflight_age)
        object.__setattr__(self, "low_gain_max_delta_mhz", low_gain)

    def signature(self) -> tuple[tuple[str, object], ...]:
        return tuple(
            (name, getattr(self, name))
            for name in (
                "minimum_feature_count",
                "route_dominated_min_ratio",
                "high_util_min_pct",
                "deep_logic_min_levels",
                "high_fanout_min_count",
                "clock_fallback_max_closure_ratio",
                "semantic_preflight_max_age_s",
                "low_gain_max_delta_mhz",
                "saturation_minimum_consecutive",
            )
        )

    def as_dict(self) -> dict[str, int | float]:
        return dict(self.signature())


@dataclass(frozen=True)
class ReplaySettings:
    policy_id: str = POLICY_ID
    deadlines: ReplayDeadlines = ReplayDeadlines()
    thresholds: ReplayThresholds = ReplayThresholds()

    def __post_init__(self) -> None:
        if self.policy_id != POLICY_ID:
            raise ReplayPolicyError(
                f"replay policy_id must be exactly {POLICY_ID!r}"
            )

    @classmethod
    def from_config(cls, config: object) -> "ReplaySettings":
        """Build settings from ``Config``, a full mapping, or a replay mapping."""

        def mapping_get(root: Mapping[str, object], path: str, default: object) -> object:
            node: object = root
            parts = path.split(".")
            if "replay" not in root and parts[0] == "replay":
                parts = parts[1:]
            for part in parts:
                if not isinstance(node, Mapping) or part not in node:
                    return default
                node = node[part]
            return node

        def get(path: str, default: object) -> object:
            if isinstance(config, Mapping):
                return mapping_get(config, path, default)
            getter = getattr(config, "get", None)
            if not callable(getter):
                raise TypeError("config must be a Config-like object or mapping")
            return getter(path, default)

        deadlines = ReplayDeadlines(**{
            name: get(f"replay.deadlines_s.{name}", default)
            for name, default in DEFAULT_DEADLINES_S.items()
        })
        threshold_defaults = ReplayThresholds()
        thresholds = ReplayThresholds(**{
            name: get(f"replay.thresholds.{name}", getattr(threshold_defaults, name))
            for name, _value in threshold_defaults.signature()
        })
        return cls(
            policy_id=str(get("replay.policy_id", POLICY_ID)),
            deadlines=deadlines,
            thresholds=thresholds,
        )


DEFAULT_SETTINGS = ReplaySettings()


# Only these anonymous aggregate names can cross the replay observation
# boundary.  Every value is numeric and finite; identities and object lists are
# neither accepted nor recursively inspected.
ANONYMOUS_OBSERVATION_FIELDS = (
    "closure_ratio",
    "lut_util_pct",
    "avg_route_pct",
    "route_dominated_ratio",
    "max_logic_levels",
    "avg_logic_levels",
    "spread_ratio",
    "actionable_fanout_count",
    "broad_fanout_count",
    "dsp_count",
    "bram_count",
    "uram_count",
    "repeated_arithmetic_family_count",
    "repeated_arithmetic_similarity_max",
    "clock_as_data_path_risk_count",
    "recurrence_eligible_count",
    "boundary_link_eligible_count",
    "worst_source_eligible_count",
    "endpoint_move_eligible_count",
)

_COUNT_OBSERVATION_FIELDS = frozenset({
    "max_logic_levels",
    "actionable_fanout_count",
    "broad_fanout_count",
    "dsp_count",
    "bram_count",
    "uram_count",
    "repeated_arithmetic_family_count",
    "clock_as_data_path_risk_count",
    "recurrence_eligible_count",
    "boundary_link_eligible_count",
    "worst_source_eligible_count",
    "endpoint_move_eligible_count",
})
_UNIT_RATIO_FIELDS = frozenset({
    "route_dominated_ratio",
    "spread_ratio",
    "repeated_arithmetic_similarity_max",
})
_PERCENT_FIELDS = frozenset({"lut_util_pct", "avg_route_pct"})
_OBSERVATION_ALIASES = MappingProxyType({
    "closure_ratio": ("closure_ratio",),
    "lut_util_pct": ("lut_util_pct",),
    "avg_route_pct": ("avg_route_pct",),
    "route_dominated_ratio": ("route_dominated_ratio",),
    "max_logic_levels": ("max_logic_levels",),
    "avg_logic_levels": ("avg_logic_levels",),
    "spread_ratio": ("spread_ratio",),
    "actionable_fanout_count": (
        "actionable_fanout_count",
        "eligible_hf_count",
    ),
    "broad_fanout_count": ("broad_fanout_count", "broad_hf_count"),
    "dsp_count": ("dsp_count",),
    "bram_count": ("bram_count",),
    "uram_count": ("uram_count",),
    "repeated_arithmetic_family_count": ("repeated_arithmetic_family_count",),
    "repeated_arithmetic_similarity_max": (
        "repeated_arithmetic_similarity_max",
    ),
    "clock_as_data_path_risk_count": ("clock_as_data_path_risk_count",),
    "recurrence_eligible_count": ("recurrence_eligible_count",),
    "boundary_link_eligible_count": ("boundary_link_eligible_count",),
    "worst_source_eligible_count": ("worst_source_eligible_count",),
    "endpoint_move_eligible_count": ("endpoint_move_eligible_count",),
})


def _sanitize_observation_value(name: str, value: object) -> Optional[int | float]:
    number = _finite_number(value)
    if number is None:
        return None
    if name in _COUNT_OBSERVATION_FIELDS:
        if number < 0.0 or not number.is_integer():
            return None
        return int(number)
    if name in _UNIT_RATIO_FIELDS and not 0.0 <= number <= 1.0:
        return None
    if name in _PERCENT_FIELDS and not 0.0 <= number <= 100.0:
        return None
    if name == "avg_logic_levels" and number < 0.0:
        return None
    return number


def sanitize_observation(raw: Mapping[str, object] | "AnonymousObservation") -> dict:
    """Return only finite, anonymous, allowlisted aggregate features.

    Unknown data is discarded rather than copied or stringified.  This is the
    intentional privacy boundary: identifiers, paths, hashes, object names,
    absolute target periods, Fmax values, and remembered bests cannot affect or
    appear in the returned mapping.
    """

    if isinstance(raw, AnonymousObservation):
        return raw.as_dict()
    if not isinstance(raw, Mapping):
        raise TypeError("replay observation must be a mapping")
    sanitized: dict[str, int | float] = {}
    for name in ANONYMOUS_OBSERVATION_FIELDS:
        for source_name in _OBSERVATION_ALIASES[name]:
            if source_name not in raw:
                continue
            value = _sanitize_observation_value(name, raw[source_name])
            if value is not None:
                sanitized[name] = value
            break
    return sanitized


def validate_anonymous_observation(raw: Mapping[str, object]) -> dict:
    """Validate an already-anonymous observation with no additional keys."""

    if not isinstance(raw, Mapping):
        raise ReplayPolicyError("anonymous observation must be an object")
    unknown = set(raw) - set(ANONYMOUS_OBSERVATION_FIELDS)
    if unknown:
        raise ReplayPolicyError(
            f"anonymous observation has unknown field(s): {sorted(map(str, unknown))}"
        )
    validated: dict[str, int | float] = {}
    for name in ANONYMOUS_OBSERVATION_FIELDS:
        if name not in raw:
            continue
        if name in _COUNT_OBSERVATION_FIELDS and type(raw[name]) is not int:
            raise ReplayPolicyError(
                f"anonymous observation field {name} must be an integer"
            )
        value = _sanitize_observation_value(name, raw[name])
        if value is None:
            raise ReplayPolicyError(
                f"anonymous observation field {name} is not a valid finite aggregate"
            )
        validated[name] = value
    return validated


def anonymous_observation_schema() -> dict:
    """Return the exact JSON-style observation schema as a caller-owned copy."""

    properties: dict[str, dict[str, object]] = {}
    for name in ANONYMOUS_OBSERVATION_FIELDS:
        item: dict[str, object] = {
            "type": "integer" if name in _COUNT_OBSERVATION_FIELDS else "number",
            "finite": True,
        }
        if name in _COUNT_OBSERVATION_FIELDS or name == "avg_logic_levels":
            item["minimum"] = 0
        if name in _UNIT_RATIO_FIELDS:
            item.update({"minimum": 0, "maximum": 1})
        if name in _PERCENT_FIELDS:
            item.update({"minimum": 0, "maximum": 100})
        properties[name] = item
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }


ANONYMOUS_OBSERVATION_SCHEMA = anonymous_observation_schema()


@dataclass(frozen=True)
class AnonymousObservation:
    """Canonical immutable representation of a sanitized observation."""

    items: tuple[tuple[str, int | float], ...]

    def __post_init__(self) -> None:
        raw = dict(self.items)
        validated = validate_anonymous_observation(raw)
        canonical = tuple(
            (name, validated[name])
            for name in ANONYMOUS_OBSERVATION_FIELDS
            if name in validated
        )
        if len(raw) != len(self.items):
            raise ReplayPolicyError("anonymous observation contains duplicate fields")
        object.__setattr__(self, "items", canonical)

    @classmethod
    def from_raw(
        cls, raw: Mapping[str, object] | "AnonymousObservation"
    ) -> "AnonymousObservation":
        if isinstance(raw, cls):
            return raw
        sanitized = sanitize_observation(raw)
        return cls(tuple(sanitized.items()))

    def as_dict(self) -> dict[str, int | float]:
        return dict(self.items)

    def get(self, name: str, default: object = None) -> object:
        return self.as_dict().get(name, default)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"replay template value is not closed data: {type(value).__name__}")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ReplayAction:
    """Closed typed replay template output for later agent integration."""

    action_id: str
    phase: str
    mutation_slot: str
    skill: str
    target: str
    params: Mapping[str, object]
    incumbent_role: str = IMMUTABLE_INCUMBENT
    policy_id: str = POLICY_ID

    def __post_init__(self) -> None:
        if self.policy_id != POLICY_ID:
            raise ReplayPolicyError("replay action has the wrong policy ID")
        if self.phase not in {"basin", "semantics"}:
            raise ReplayPolicyError("replay mutation action has an invalid phase")
        if self.mutation_slot not in {"basin", "semantic"}:
            raise ReplayPolicyError("replay action has an invalid mutation slot")
        if self.incumbent_role != IMMUTABLE_INCUMBENT:
            raise ReplayPolicyError("replay actions must use the immutable incumbent")
        if not self.action_id or not self.skill or not self.target:
            raise ReplayPolicyError("replay action identifiers must be nonempty")
        if not isinstance(self.params, Mapping):
            raise ReplayPolicyError("replay action params must be a mapping")
        object.__setattr__(self, "params", _freeze(self.params))

    @property
    def immutable_incumbent(self) -> bool:
        return True

    @property
    def resolved_params(self) -> dict:
        """A caller-owned params copy; resolution remains an integration step."""

        return self.params_dict()

    def params_dict(self) -> dict:
        return _thaw(self.params)  # type: ignore[return-value]

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "action_id": self.action_id,
            "phase": self.phase,
            "mutation_slot": self.mutation_slot,
            "incumbent_role": self.incumbent_role,
            "skill": self.skill,
            "target": self.target,
            "params": self.params_dict(),
        }

    def to_action_dict(self) -> dict:
        """Return mechanics only, without inventing an incumbent identity."""

        return {
            "action": "run_skill",
            "skill": self.skill,
            "target": self.target,
            "params": self.params_dict(),
        }


def _action(
    action_id: str,
    phase: str,
    mutation_slot: str,
    skill: str,
    target: str,
    params: Optional[Mapping[str, object]] = None,
) -> ReplayAction:
    return ReplayAction(
        action_id=action_id,
        phase=phase,
        mutation_slot=mutation_slot,
        skill=skill,
        target=target,
        params=_freeze(params or {}),  # type: ignore[arg-type]
    )


# Registry order is policy order.  It is also the deterministic no-LLM order.
# No config object is consulted while constructing or returning these entries.
_ACTION_REGISTRY = {
    "basin_route_race": _action(
        "basin_route_race",
        "basin",
        "basin",
        "fresh_place_route",
        "auto",
        {
            "flow_mode": "shared_post_place_route_race",
            "post_place_recipe": "extratiming_explore",
            "route_directives": ["Explore"],
            "post_route_phys_opt_preset": "routing_critical_pin",
            "max_candidates": 1,
        },
    ),
    "basin_physical_explore": _action(
        "basin_physical_explore",
        "basin",
        "basin",
        "phys_opt",
        "AggressiveExplore",
    ),
    "semantic_recurrence_cut": _action(
        "semantic_recurrence_cut",
        "semantics",
        "semantic",
        "operator_rewrite",
        "dsp_srl_recurrence_cut_v1",
        {"max_candidates": 1, "route_timeout_s": 900},
    ),
    "semantic_boundary_link": _action(
        "semantic_boundary_link",
        "semantics",
        "semantic",
        "critical_net_reroute",
        "boundary_link",
    ),
    "semantic_worst_source": _action(
        "semantic_worst_source",
        "semantics",
        "semantic",
        "critical_net_reroute",
        "worst_source",
    ),
    "semantic_endpoint_move": _action(
        "semantic_endpoint_move",
        "semantics",
        "semantic",
        "endpoint_bel_move",
        "auto",
        {"num_paths": 12, "max_candidates": 1, "attempt_timeout_s": 900},
    ),
    "semantic_cleanup_fallback": _action(
        "semantic_cleanup_fallback",
        "semantics",
        "semantic",
        "post_route_cleanup",
        "phys_routing",
        {"max_candidates": 1, "max_iterations": 1},
    ),
}
ACTION_TEMPLATES: Mapping[str, ReplayAction] = MappingProxyType(_ACTION_REGISTRY)
ACTION_IDS = tuple(_ACTION_REGISTRY)


def action_template(action_id: str) -> ReplayAction:
    try:
        return ACTION_TEMPLATES[str(action_id)]
    except KeyError as exc:
        raise ReplayPolicyError(f"unknown replay action_id {action_id!r}") from exc


def action_catalog() -> tuple[dict, ...]:
    """Return caller-owned copies while the code-owned registry stays closed."""

    return tuple(action.to_dict() for action in ACTION_TEMPLATES.values())


ACTION_CATALOG_SIGNATURE = _canonical_digest({
    "policy_id": POLICY_ID,
    "policy_version": POLICY_VERSION,
    "actions": action_catalog(),
})


_DEADLINE_FIELDS = tuple(DEFAULT_DEADLINES_S)
_THRESHOLD_FIELDS = tuple(name for name, _value in ReplayThresholds().signature())
_ENVELOPE_FIELDS = frozenset({"schema_version", "policy_id", "policy_version"})


def _deadlines_from_mapping(raw: object, name: str) -> ReplayDeadlines:
    mapping = _require_exact_fields(raw, frozenset(_DEADLINE_FIELDS), name)
    return ReplayDeadlines(**{field: mapping[field] for field in _DEADLINE_FIELDS})


def _thresholds_from_mapping(raw: object, name: str) -> ReplayThresholds:
    mapping = _require_exact_fields(raw, frozenset(_THRESHOLD_FIELDS), name)
    return ReplayThresholds(**{
        field: mapping[field] for field in _THRESHOLD_FIELDS
    })


def _deadlines_from_signature(
    signature: object,
    name: str,
) -> ReplayDeadlines:
    if not isinstance(signature, tuple):
        raise ReplayPolicyError(f"{name} must be a canonical tuple")
    try:
        raw = dict(signature)
    except (TypeError, ValueError) as exc:
        raise ReplayPolicyError(f"{name} is malformed") from exc
    if len(raw) != len(signature):
        raise ReplayPolicyError(f"{name} contains duplicate fields")
    deadlines = _deadlines_from_mapping(raw, name)
    if signature != tuple(deadlines.as_dict().items()):
        raise ReplayPolicyError(f"{name} is not in canonical field order")
    return deadlines


def _thresholds_from_signature(
    signature: object,
    name: str,
) -> ReplayThresholds:
    if not isinstance(signature, tuple):
        raise ReplayPolicyError(f"{name} must be a canonical tuple")
    try:
        raw = dict(signature)
    except (TypeError, ValueError) as exc:
        raise ReplayPolicyError(f"{name} is malformed") from exc
    if len(raw) != len(signature):
        raise ReplayPolicyError(f"{name} contains duplicate fields")
    thresholds = _thresholds_from_mapping(raw, name)
    if signature != thresholds.signature():
        raise ReplayPolicyError(f"{name} is not in canonical field order")
    return thresholds


def _observation_items_from_serialized(
    raw: object,
    name: str,
) -> tuple[tuple[str, int | float], ...]:
    if not isinstance(raw, Mapping):
        raise ReplayPolicyError(f"{name} must be an object")
    validated = validate_anonymous_observation(raw)
    return tuple(
        (field, validated[field])
        for field in ANONYMOUS_OBSERVATION_FIELDS
        if field in validated
    )


def _validate_observation_items(
    items: object,
    name: str,
) -> AnonymousObservation:
    if not isinstance(items, tuple):
        raise ReplayPolicyError(f"{name} must be a canonical tuple")
    for item in items:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
        ):
            raise ReplayPolicyError(f"{name} is malformed")
    observation = AnonymousObservation(items)
    if observation.items != items:
        raise ReplayPolicyError(f"{name} is not in canonical field order")
    return observation


@dataclass(frozen=True)
class SiblingOutcome:
    """Anonymous authoritative assessment of one produced sibling.

    ``legal`` means centrally measured and legal.  Its delta is therefore the
    sibling's measured Fmax minus the immutable incumbent's measured Fmax.
    ``illegal`` and ``unknown`` are deliberately not assigned synthetic deltas.
    """

    status: str
    delta_fmax_mhz_vs_incumbent: Optional[float] = None
    promoted: bool = False
    schema_version: int = SCHEMA_VERSION
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_object_envelope(
            self.schema_version,
            self.policy_id,
            self.policy_version,
            "sibling outcome",
        )
        if self.status not in SIBLING_STATUSES:
            raise ReplayPolicyError(
                "sibling outcome status must be legal, illegal, or unknown"
            )
        _require_bool("sibling outcome promoted", self.promoted)
        if self.status == "legal":
            if self.delta_fmax_mhz_vs_incumbent is None:
                raise ReplayPolicyError(
                    "a legal sibling requires an incumbent-relative delta"
                )
            delta = _require_finite_number(
                "sibling outcome delta_fmax_mhz_vs_incumbent",
                self.delta_fmax_mhz_vs_incumbent,
            )
            object.__setattr__(self, "delta_fmax_mhz_vs_incumbent", delta)
        elif self.delta_fmax_mhz_vs_incumbent is not None:
            raise ReplayPolicyError(
                "illegal or unknown siblings cannot have a measured legal delta"
            )
        if self.promoted and self.status != "legal":
            raise ReplayPolicyError("only a measured legal sibling can be promoted")
        if self.promoted and self.delta_fmax_mhz_vs_incumbent <= 0.0:
            raise ReplayPolicyError(
                "a promoted sibling must improve on the bound incumbent"
            )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "status": self.status,
            "delta_fmax_mhz_vs_incumbent": (
                self.delta_fmax_mhz_vs_incumbent
            ),
            "promoted": self.promoted,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SiblingOutcome":
        fields = _ENVELOPE_FIELDS | frozenset({
            "status",
            "delta_fmax_mhz_vs_incumbent",
            "promoted",
        })
        mapping = _require_exact_fields(raw, fields, "sibling outcome")
        _validate_envelope(mapping, "sibling outcome")
        return cls(
            status=_require_string("sibling outcome status", mapping["status"]),
            delta_fmax_mhz_vs_incumbent=(
                mapping["delta_fmax_mhz_vs_incumbent"]
            ),
            promoted=_require_bool(
                "sibling outcome promoted", mapping["promoted"]
            ),
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            policy_id=mapping["policy_id"],  # type: ignore[arg-type]
            policy_version=mapping["policy_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ActionCycleOutcome:
    """Terminal result for exactly one authorized action cycle.

    A started action may produce any number of anonymous siblings.  An unknown
    launch is consumed conservatively but cannot carry measurement evidence.
    A durably not-started action carries no siblings and does not consume its
    mutation slot.
    """

    execution_status: str
    siblings: tuple[SiblingOutcome, ...] = ()
    schema_version: int = SCHEMA_VERSION
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_object_envelope(
            self.schema_version,
            self.policy_id,
            self.policy_version,
            "action-cycle outcome",
        )
        if self.execution_status not in EXECUTION_STATUSES:
            raise ReplayPolicyError(
                "execution_status must be not_started, started, or unknown"
            )
        if not isinstance(self.siblings, tuple) or any(
            not isinstance(sibling, SiblingOutcome) for sibling in self.siblings
        ):
            raise ReplayPolicyError(
                "action-cycle siblings must be a tuple of SiblingOutcome values"
            )
        if self.execution_status != "started" and self.siblings:
            raise ReplayPolicyError(
                "not-started or unknown execution cannot carry sibling evidence"
            )

    @property
    def best_legal_delta_fmax_mhz_vs_incumbent(self) -> Optional[float]:
        deltas = [
            float(sibling.delta_fmax_mhz_vs_incumbent)
            for sibling in self.siblings
            if sibling.status == "legal"
            and sibling.delta_fmax_mhz_vs_incumbent is not None
        ]
        return max(deltas) if deltas else None

    @property
    def promoted(self) -> bool:
        return any(sibling.promoted for sibling in self.siblings)

    @property
    def qualifies_for_saturation(self) -> bool:
        return (
            self.execution_status == "started"
            and self.best_legal_delta_fmax_mhz_vs_incumbent is not None
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "execution_status": self.execution_status,
            "siblings": [sibling.to_dict() for sibling in self.siblings],
            "best_legal_delta_fmax_mhz_vs_incumbent": (
                self.best_legal_delta_fmax_mhz_vs_incumbent
            ),
            "promoted": self.promoted,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ActionCycleOutcome":
        fields = _ENVELOPE_FIELDS | frozenset({
            "execution_status",
            "siblings",
            "best_legal_delta_fmax_mhz_vs_incumbent",
            "promoted",
        })
        mapping = _require_exact_fields(raw, fields, "action-cycle outcome")
        _validate_envelope(mapping, "action-cycle outcome")
        raw_siblings = mapping["siblings"]
        if not isinstance(raw_siblings, list):
            raise ReplayPolicyError("action-cycle outcome siblings must be an array")
        outcome = cls(
            execution_status=_require_string(
                "action-cycle execution_status", mapping["execution_status"]
            ),
            siblings=tuple(SiblingOutcome.from_dict(item) for item in raw_siblings),
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            policy_id=mapping["policy_id"],  # type: ignore[arg-type]
            policy_version=mapping["policy_version"],  # type: ignore[arg-type]
        )
        expected_delta = mapping["best_legal_delta_fmax_mhz_vs_incumbent"]
        if expected_delta is not None:
            expected_delta = _require_finite_number(
                "action-cycle best legal incumbent-relative delta",
                expected_delta,
            )
        if expected_delta != outcome.best_legal_delta_fmax_mhz_vs_incumbent:
            raise ReplayPolicyError(
                "action-cycle best legal delta does not match its siblings"
            )
        if (
            _require_bool("action-cycle promoted", mapping["promoted"])
            != outcome.promoted
        ):
            raise ReplayPolicyError(
                "action-cycle promoted flag does not match its siblings"
            )
        return outcome

    # The old spelling remains useful to callers, but it is intentionally just
    # the strict versioned action-cycle shape rather than a coercion layer.
    from_mapping = from_dict


# Compatibility import name for pure callers.  Its constructor and serialized
# representation are the hardened action-cycle contract.
MutationOutcome = ActionCycleOutcome
ActionCycleSiblingOutcome = SiblingOutcome


@dataclass(frozen=True)
class SelectionAuthorization:
    """Identity-free proof that a code-owned action was eligible when selected."""

    action_id: str
    mutation_slot: str
    selected_at_s: float
    observation_items: tuple[tuple[str, int | float], ...]
    basin_attempts: int
    semantic_attempts: int
    authorization_sequence: int
    not_started_cycles: int
    state_signature: str
    deadline_signature: tuple[tuple[str, float], ...]
    threshold_signature: tuple[tuple[str, object], ...]
    action_catalog_signature: str = ACTION_CATALOG_SIGNATURE
    schema_version: int = SCHEMA_VERSION
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_object_envelope(
            self.schema_version,
            self.policy_id,
            self.policy_version,
            "selection authorization",
        )
        action_id = _require_string(
            "selection authorization action_id", self.action_id
        )
        mutation_slot = _require_string(
            "selection authorization mutation_slot", self.mutation_slot
        )
        template = action_template(action_id)
        if template.mutation_slot != mutation_slot:
            raise ReplayPolicyError(
                "selection authorization slot does not match its code-owned action"
            )
        selected_at = _require_finite_number(
            "selection authorization selected_at_s", self.selected_at_s
        )
        if selected_at < 0.0:
            raise ReplayPolicyError(
                "selection authorization selected_at_s must be nonnegative"
            )
        observation = _validate_observation_items(
            self.observation_items,
            "selection authorization observation_items",
        )
        basin_attempts = _require_nonnegative_int(
            "selection authorization basin_attempts", self.basin_attempts
        )
        semantic_attempts = _require_nonnegative_int(
            "selection authorization semantic_attempts", self.semantic_attempts
        )
        if basin_attempts > 1 or semantic_attempts > 1:
            raise ReplayPolicyError(
                "selection authorization mutation counters exceed policy slots"
            )
        sequence = _require_nonnegative_int(
            "selection authorization authorization_sequence",
            self.authorization_sequence,
        )
        not_started = _require_nonnegative_int(
            "selection authorization not_started_cycles",
            self.not_started_cycles,
        )
        if not_started > sequence:
            raise ReplayPolicyError(
                "selection authorization has impossible cycle counters"
            )
        state_signature = _require_digest(
            "selection authorization state_signature", self.state_signature
        )
        catalog_signature = _require_digest(
            "selection authorization action_catalog_signature",
            self.action_catalog_signature,
        )
        if catalog_signature != ACTION_CATALOG_SIGNATURE:
            raise ReplayPolicyError(
                "selection authorization has the wrong action catalog signature"
            )
        deadlines = _deadlines_from_signature(
            self.deadline_signature,
            "selection authorization deadline_signature",
        )
        thresholds = _thresholds_from_signature(
            self.threshold_signature,
            "selection authorization threshold_signature",
        )
        selection_deadline = (
            deadlines.basin if mutation_slot == "basin" else deadlines.semantics
        )
        if selected_at >= selection_deadline:
            raise ReplayPolicyError(
                "selection authorization was issued at or after its deadline"
            )
        eligible_ids = (
            _basin_feature_action_ids(observation, thresholds)
            if mutation_slot == "basin"
            else _semantic_feature_action_ids(observation, thresholds)
        )
        if action_id not in eligible_ids:
            raise ReplayPolicyError(
                "selection authorization action is not eligible for its observation"
            )
        object.__setattr__(self, "selected_at_s", selected_at)
        object.__setattr__(self, "observation_items", observation.items)
        object.__setattr__(self, "basin_attempts", basin_attempts)
        object.__setattr__(self, "semantic_attempts", semantic_attempts)
        object.__setattr__(self, "authorization_sequence", sequence)
        object.__setattr__(self, "not_started_cycles", not_started)
        object.__setattr__(self, "state_signature", state_signature)
        object.__setattr__(self, "action_catalog_signature", catalog_signature)
        object.__setattr__(
            self, "deadline_signature", tuple(deadlines.as_dict().items())
        )
        object.__setattr__(self, "threshold_signature", thresholds.signature())

    @property
    def observation(self) -> AnonymousObservation:
        return AnonymousObservation(self.observation_items)

    @property
    def action(self) -> ReplayAction:
        return action_template(self.action_id)

    @property
    def phase(self) -> str:
        return self.action.phase

    @property
    def skill(self) -> str:
        return self.action.skill

    @property
    def target(self) -> str:
        return self.action.target

    @property
    def params(self) -> Mapping[str, object]:
        return self.action.params

    @property
    def resolved_params(self) -> dict:
        return self.params_dict()

    @property
    def incumbent_role(self) -> str:
        return IMMUTABLE_INCUMBENT

    @property
    def immutable_incumbent(self) -> bool:
        return True

    def params_dict(self) -> dict:
        return self.action.params_dict()

    def to_action_dict(self) -> dict:
        return self.action.to_action_dict()

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "action_catalog_signature": self.action_catalog_signature,
            "action_id": self.action_id,
            "mutation_slot": self.mutation_slot,
            "selected_at_s": self.selected_at_s,
            "observation": self.observation.as_dict(),
            "state_counters": {
                "basin_attempts": self.basin_attempts,
                "semantic_attempts": self.semantic_attempts,
                "authorization_sequence": self.authorization_sequence,
                "not_started_cycles": self.not_started_cycles,
            },
            "state_signature": self.state_signature,
            "deadlines_s": dict(self.deadline_signature),
            "thresholds": dict(self.threshold_signature),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SelectionAuthorization":
        fields = _ENVELOPE_FIELDS | frozenset({
            "action_catalog_signature",
            "action_id",
            "mutation_slot",
            "selected_at_s",
            "observation",
            "state_counters",
            "state_signature",
            "deadlines_s",
            "thresholds",
        })
        mapping = _require_exact_fields(raw, fields, "selection authorization")
        _validate_envelope(mapping, "selection authorization")
        counters = _require_exact_fields(
            mapping["state_counters"],
            frozenset({
                "basin_attempts",
                "semantic_attempts",
                "authorization_sequence",
                "not_started_cycles",
            }),
            "selection authorization state_counters",
        )
        deadlines = _deadlines_from_mapping(
            mapping["deadlines_s"], "selection authorization deadlines_s"
        )
        thresholds = _thresholds_from_mapping(
            mapping["thresholds"], "selection authorization thresholds"
        )
        return cls(
            action_id=_require_string(
                "selection authorization action_id", mapping["action_id"]
            ),
            mutation_slot=_require_string(
                "selection authorization mutation_slot", mapping["mutation_slot"]
            ),
            selected_at_s=mapping["selected_at_s"],  # type: ignore[arg-type]
            observation_items=_observation_items_from_serialized(
                mapping["observation"], "selection authorization observation"
            ),
            basin_attempts=counters["basin_attempts"],  # type: ignore[arg-type]
            semantic_attempts=counters["semantic_attempts"],  # type: ignore[arg-type]
            authorization_sequence=counters["authorization_sequence"],  # type: ignore[arg-type]
            not_started_cycles=counters["not_started_cycles"],  # type: ignore[arg-type]
            state_signature=mapping["state_signature"],  # type: ignore[arg-type]
            deadline_signature=tuple(deadlines.as_dict().items()),
            threshold_signature=thresholds.signature(),
            action_catalog_signature=mapping["action_catalog_signature"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            policy_id=mapping["policy_id"],  # type: ignore[arg-type]
            policy_version=mapping["policy_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class RecordedMutation:
    action_id: str
    mutation_slot: str
    authorization: SelectionAuthorization
    outcome: ActionCycleOutcome
    completed_at_s: float
    schema_version: int = SCHEMA_VERSION
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_object_envelope(
            self.schema_version,
            self.policy_id,
            self.policy_version,
            "recorded mutation",
        )
        if not isinstance(self.authorization, SelectionAuthorization):
            raise ReplayPolicyError(
                "recorded mutation authorization has the wrong type"
            )
        if not isinstance(self.outcome, ActionCycleOutcome):
            raise ReplayPolicyError("recorded mutation outcome has the wrong type")
        template = action_template(self.action_id)
        if template.mutation_slot != self.mutation_slot:
            raise ReplayPolicyError("recorded mutation slot does not match its action")
        if (
            self.authorization.action_id != self.action_id
            or self.authorization.mutation_slot != self.mutation_slot
        ):
            raise ReplayPolicyError(
                "recorded mutation does not match its selection authorization"
            )
        if self.outcome.execution_status == "not_started":
            raise ReplayPolicyError(
                "a durably not-started cycle cannot consume a mutation slot"
            )
        completed_at = _require_finite_number(
            "recorded mutation completed_at_s", self.completed_at_s
        )
        if completed_at < self.authorization.selected_at_s:
            raise ReplayPolicyError(
                "recorded mutation completed before its action was selected"
            )
        object.__setattr__(self, "completed_at_s", completed_at)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "action_id": self.action_id,
            "mutation_slot": self.mutation_slot,
            "authorization": self.authorization.to_dict(),
            "outcome": self.outcome.to_dict(),
            "completed_at_s": self.completed_at_s,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "RecordedMutation":
        fields = _ENVELOPE_FIELDS | frozenset({
            "action_id",
            "mutation_slot",
            "authorization",
            "outcome",
            "completed_at_s",
        })
        mapping = _require_exact_fields(raw, fields, "recorded mutation")
        _validate_envelope(mapping, "recorded mutation")
        return cls(
            action_id=_require_string(
                "recorded mutation action_id", mapping["action_id"]
            ),
            mutation_slot=_require_string(
                "recorded mutation mutation_slot", mapping["mutation_slot"]
            ),
            authorization=SelectionAuthorization.from_dict(
                mapping["authorization"]  # type: ignore[arg-type]
            ),
            outcome=ActionCycleOutcome.from_dict(
                mapping["outcome"]  # type: ignore[arg-type]
            ),
            completed_at_s=mapping["completed_at_s"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            policy_id=mapping["policy_id"],  # type: ignore[arg-type]
            policy_version=mapping["policy_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SemanticPreflight:
    """Feature-bound, expiring authorization snapshot for semantic selection."""

    issued_at_s: float
    expires_at_s: float
    observation_items: tuple[tuple[str, int | float], ...]
    eligible_action_ids: tuple[str, ...]
    basin_attempts: int
    semantic_attempts: int
    threshold_signature: tuple[tuple[str, object], ...]
    schema_version: int = SCHEMA_VERSION
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_object_envelope(
            self.schema_version,
            self.policy_id,
            self.policy_version,
            "semantic preflight",
        )
        issued = _require_finite_number("preflight issued_at_s", self.issued_at_s)
        expires = _require_finite_number("preflight expires_at_s", self.expires_at_s)
        if issued < 0.0 or expires <= issued:
            raise ReplayPolicyError("semantic preflight has invalid validity bounds")
        observation = _validate_observation_items(
            self.observation_items, "semantic preflight observation_items"
        )
        if not isinstance(self.eligible_action_ids, tuple) or any(
            not isinstance(action_id, str) for action_id in self.eligible_action_ids
        ):
            raise ReplayPolicyError(
                "semantic preflight eligible_action_ids must be a tuple of strings"
            )
        basin_attempts = _require_nonnegative_int(
            "semantic preflight basin_attempts", self.basin_attempts
        )
        semantic_attempts = _require_nonnegative_int(
            "semantic preflight semantic_attempts", self.semantic_attempts
        )
        thresholds = _thresholds_from_signature(
            self.threshold_signature,
            "semantic preflight threshold_signature",
        )
        if expires - issued > thresholds.semantic_preflight_max_age_s:
            raise ReplayPolicyError(
                "semantic preflight validity exceeds its configured maximum age"
            )
        object.__setattr__(self, "issued_at_s", issued)
        object.__setattr__(self, "expires_at_s", expires)
        object.__setattr__(self, "observation_items", observation.items)
        object.__setattr__(self, "basin_attempts", basin_attempts)
        object.__setattr__(self, "semantic_attempts", semantic_attempts)
        object.__setattr__(self, "threshold_signature", thresholds.signature())
        if len(set(self.eligible_action_ids)) != len(self.eligible_action_ids):
            raise ReplayPolicyError("semantic preflight has duplicate action IDs")
        for action_id in self.eligible_action_ids:
            if action_template(action_id).mutation_slot != "semantic":
                raise ReplayPolicyError(
                    "semantic preflight contains a non-semantic action"
                )
        if basin_attempts not in {0, 1} or semantic_attempts != 0:
            raise ReplayPolicyError("semantic preflight has invalid mutation counters")
        if self.eligible_action_ids != _semantic_feature_action_ids(
            observation, thresholds
        ):
            raise ReplayPolicyError(
                "semantic preflight actions do not match its anonymous observation"
            )

    @property
    def observation(self) -> AnonymousObservation:
        return AnonymousObservation(self.observation_items)

    @property
    def version(self) -> int:
        return self.policy_version

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "issued_at_s": self.issued_at_s,
            "expires_at_s": self.expires_at_s,
            "observation": self.observation.as_dict(),
            "eligible_action_ids": list(self.eligible_action_ids),
            "basin_attempts": self.basin_attempts,
            "semantic_attempts": self.semantic_attempts,
            "thresholds": dict(self.threshold_signature),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SemanticPreflight":
        fields = _ENVELOPE_FIELDS | frozenset({
            "issued_at_s",
            "expires_at_s",
            "observation",
            "eligible_action_ids",
            "basin_attempts",
            "semantic_attempts",
            "thresholds",
        })
        mapping = _require_exact_fields(raw, fields, "semantic preflight")
        _validate_envelope(mapping, "semantic preflight")
        eligible = mapping["eligible_action_ids"]
        if not isinstance(eligible, list) or any(
            not isinstance(action_id, str) for action_id in eligible
        ):
            raise ReplayPolicyError(
                "semantic preflight eligible_action_ids must be an array of strings"
            )
        thresholds = _thresholds_from_mapping(
            mapping["thresholds"], "semantic preflight thresholds"
        )
        return cls(
            issued_at_s=mapping["issued_at_s"],  # type: ignore[arg-type]
            expires_at_s=mapping["expires_at_s"],  # type: ignore[arg-type]
            observation_items=_observation_items_from_serialized(
                mapping["observation"], "semantic preflight observation"
            ),
            eligible_action_ids=tuple(eligible),
            basin_attempts=mapping["basin_attempts"],  # type: ignore[arg-type]
            semantic_attempts=mapping["semantic_attempts"],  # type: ignore[arg-type]
            threshold_signature=thresholds.signature(),
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            policy_id=mapping["policy_id"],  # type: ignore[arg-type]
            policy_version=mapping["policy_version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ReplayState:
    """Immutable policy state with exactly one slot per mutation class."""

    characterization_complete: bool = False
    basin_mutation: Optional[RecordedMutation] = None
    semantic_preflight: Optional[SemanticPreflight] = None
    semantic_mutation: Optional[RecordedMutation] = None
    authorization_sequence: int = 0
    not_started_cycles: int = 0
    closeout_complete: bool = False
    closeout_completed_at_s: Optional[float] = None
    submission_complete: bool = False
    submission_completed_at_s: Optional[float] = None
    schema_version: int = SCHEMA_VERSION
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION

    def __post_init__(self) -> None:
        _validate_object_envelope(
            self.schema_version,
            self.policy_id,
            self.policy_version,
            "replay state",
        )
        for name in (
            "characterization_complete",
            "closeout_complete",
            "submission_complete",
        ):
            _require_bool(f"replay state {name}", getattr(self, name))
        sequence = _require_nonnegative_int(
            "replay state authorization_sequence", self.authorization_sequence
        )
        not_started = _require_nonnegative_int(
            "replay state not_started_cycles", self.not_started_cycles
        )
        for name in ("basin_mutation", "semantic_mutation"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, RecordedMutation):
                raise ReplayPolicyError(f"replay state {name} has the wrong type")
        if self.semantic_preflight is not None and not isinstance(
            self.semantic_preflight, SemanticPreflight
        ):
            raise ReplayPolicyError(
                "replay state semantic_preflight has the wrong type"
            )
        if (
            self.basin_mutation is not None
            and self.basin_mutation.mutation_slot != "basin"
        ):
            raise ReplayPolicyError("basin slot contains a non-basin mutation")
        if (
            self.semantic_mutation is not None
            and self.semantic_mutation.mutation_slot != "semantic"
        ):
            raise ReplayPolicyError("semantic slot contains a non-semantic mutation")
        if self.semantic_mutation is not None and self.semantic_preflight is not None:
            raise ReplayPolicyError(
                "consumed semantic state cannot retain a semantic preflight"
            )
        if self.semantic_preflight is not None and (
            self.semantic_preflight.basin_attempts != self.basin_attempts
            or self.semantic_preflight.semantic_attempts != self.semantic_attempts
        ):
            raise ReplayPolicyError(
                "semantic preflight counters do not match replay state"
            )
        if sequence != len(self.mutations) + not_started:
            raise ReplayPolicyError(
                "replay state action-cycle counters are inconsistent"
            )
        recorded_sequences = [
            mutation.authorization.authorization_sequence
            for mutation in self.mutations
        ]
        if (
            len(set(recorded_sequences)) != len(recorded_sequences)
            or any(item >= sequence for item in recorded_sequences)
        ):
            raise ReplayPolicyError(
                "replay state contains stale or duplicated mutation authorizations"
            )
        if self.basin_mutation is not None:
            basin_authorization = self.basin_mutation.authorization
            if (
                basin_authorization.basin_attempts != 0
                or basin_authorization.semantic_attempts != 0
            ):
                raise ReplayPolicyError(
                    "basin mutation authorization has impossible counters"
                )
        if self.semantic_mutation is not None:
            semantic_authorization = self.semantic_mutation.authorization
            if semantic_authorization.semantic_attempts != 0 or (
                semantic_authorization.basin_attempts != self.basin_attempts
            ):
                raise ReplayPolicyError(
                    "semantic mutation authorization has impossible counters"
                )
        if self.basin_mutation is not None and self.semantic_mutation is not None:
            if (
                self.semantic_mutation.authorization.authorization_sequence
                <= self.basin_mutation.authorization.authorization_sequence
                or self.semantic_mutation.authorization.selected_at_s
                < self.basin_mutation.completed_at_s
            ):
                raise ReplayPolicyError(
                    "semantic mutation precedes the recorded basin mutation"
                )
        threshold_signatures = {
            mutation.authorization.threshold_signature
            for mutation in self.mutations
        }
        if self.semantic_preflight is not None:
            threshold_signatures.add(self.semantic_preflight.threshold_signature)
        if len(threshold_signatures) > 1:
            raise ReplayPolicyError(
                "replay state mixes incompatible threshold settings"
            )
        if (
            len({
                mutation.authorization.deadline_signature
                for mutation in self.mutations
            })
            > 1
        ):
            raise ReplayPolicyError(
                "replay state mixes incompatible deadline settings"
            )
        closeout_at = self.closeout_completed_at_s
        submission_at = self.submission_completed_at_s
        if self.closeout_complete != (closeout_at is not None):
            raise ReplayPolicyError(
                "replay state closeout flag and acknowledgement time disagree"
            )
        if self.submission_complete != (submission_at is not None):
            raise ReplayPolicyError(
                "replay state submission flag and acknowledgement time disagree"
            )
        if closeout_at is not None:
            closeout_at = _elapsed(closeout_at)
            object.__setattr__(self, "closeout_completed_at_s", closeout_at)
            if any(
                mutation.completed_at_s > closeout_at for mutation in self.mutations
            ):
                raise ReplayPolicyError(
                    "closeout acknowledgement precedes a recorded mutation"
                )
        if self.submission_complete and not self.closeout_complete:
            raise ReplayPolicyError(
                "submission completion requires closeout completion"
            )
        if submission_at is not None:
            submission_at = _elapsed(submission_at)
            object.__setattr__(self, "submission_completed_at_s", submission_at)
            if closeout_at is None or submission_at < closeout_at:
                raise ReplayPolicyError(
                    "submission acknowledgement precedes closeout acknowledgement"
                )
        object.__setattr__(self, "authorization_sequence", sequence)
        object.__setattr__(self, "not_started_cycles", not_started)

    @property
    def immutable_incumbent(self) -> bool:
        return True

    @property
    def basin_attempts(self) -> int:
        return int(self.basin_mutation is not None)

    @property
    def semantic_attempts(self) -> int:
        return int(self.semantic_mutation is not None)

    @property
    def mutations(self) -> tuple[RecordedMutation, ...]:
        return tuple(
            item
            for item in (self.basin_mutation, self.semantic_mutation)
            if item is not None
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "characterization_complete": self.characterization_complete,
            "basin_mutation": (
                None if self.basin_mutation is None else self.basin_mutation.to_dict()
            ),
            "semantic_preflight": (
                None
                if self.semantic_preflight is None
                else self.semantic_preflight.to_dict()
            ),
            "semantic_mutation": (
                None
                if self.semantic_mutation is None
                else self.semantic_mutation.to_dict()
            ),
            "authorization_sequence": self.authorization_sequence,
            "not_started_cycles": self.not_started_cycles,
            "closeout_complete": self.closeout_complete,
            "closeout_completed_at_s": self.closeout_completed_at_s,
            "submission_complete": self.submission_complete,
            "submission_completed_at_s": self.submission_completed_at_s,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ReplayState":
        fields = _ENVELOPE_FIELDS | frozenset({
            "characterization_complete",
            "basin_mutation",
            "semantic_preflight",
            "semantic_mutation",
            "authorization_sequence",
            "not_started_cycles",
            "closeout_complete",
            "closeout_completed_at_s",
            "submission_complete",
            "submission_completed_at_s",
        })
        mapping = _require_exact_fields(raw, fields, "replay state")
        _validate_envelope(mapping, "replay state")
        basin_raw = mapping["basin_mutation"]
        preflight_raw = mapping["semantic_preflight"]
        semantic_raw = mapping["semantic_mutation"]
        return cls(
            characterization_complete=_require_bool(
                "replay state characterization_complete",
                mapping["characterization_complete"],
            ),
            basin_mutation=(
                None
                if basin_raw is None
                else RecordedMutation.from_dict(basin_raw)  # type: ignore[arg-type]
            ),
            semantic_preflight=(
                None
                if preflight_raw is None
                else SemanticPreflight.from_dict(preflight_raw)  # type: ignore[arg-type]
            ),
            semantic_mutation=(
                None
                if semantic_raw is None
                else RecordedMutation.from_dict(semantic_raw)  # type: ignore[arg-type]
            ),
            authorization_sequence=mapping["authorization_sequence"],  # type: ignore[arg-type]
            not_started_cycles=mapping["not_started_cycles"],  # type: ignore[arg-type]
            closeout_complete=_require_bool(
                "replay state closeout_complete", mapping["closeout_complete"]
            ),
            closeout_completed_at_s=mapping["closeout_completed_at_s"],  # type: ignore[arg-type]
            submission_complete=_require_bool(
                "replay state submission_complete",
                mapping["submission_complete"],
            ),
            submission_completed_at_s=mapping["submission_completed_at_s"],  # type: ignore[arg-type]
            schema_version=mapping["schema_version"],  # type: ignore[arg-type]
            policy_id=mapping["policy_id"],  # type: ignore[arg-type]
            policy_version=mapping["policy_version"],  # type: ignore[arg-type]
        )


def initial_state() -> ReplayState:
    return ReplayState()


def replay_state_signature(state: ReplayState) -> str:
    if not isinstance(state, ReplayState):
        raise ReplayPolicyError("state signature requires ReplayState")
    return _canonical_digest(state.to_dict())


def _elapsed(value: object) -> float:
    elapsed = _require_finite_number("elapsed_s", value)
    if elapsed < 0.0:
        raise ReplayPolicyError("elapsed_s must be nonnegative")
    return elapsed


def _feature_count(observation: AnonymousObservation) -> int:
    return len(observation.items)


def _number(observation: AnonymousObservation, name: str) -> Optional[float]:
    value = observation.as_dict().get(name)
    return float(value) if value is not None else None


def _basin_feature_action_ids(
    observation: AnonymousObservation,
    thresholds: ReplayThresholds,
) -> tuple[str, ...]:
    if _feature_count(observation) < thresholds.minimum_feature_count:
        return ()
    route_ratio = _number(observation, "route_dominated_ratio")
    route_pct = _number(observation, "avg_route_pct")
    util_pct = _number(observation, "lut_util_pct")
    route_or_congestion = bool(
        (route_ratio is not None and route_ratio >= thresholds.route_dominated_min_ratio)
        or (route_pct is not None and route_pct >= thresholds.route_dominated_min_ratio * 100.0)
        or (util_pct is not None and util_pct >= thresholds.high_util_min_pct)
    )
    eligible: list[str] = []
    if route_or_congestion:
        eligible.append("basin_route_race")
    eligible.append("basin_physical_explore")
    return tuple(eligible)


def _semantic_feature_action_ids(
    observation: AnonymousObservation,
    thresholds: ReplayThresholds,
) -> tuple[str, ...]:
    if _feature_count(observation) < thresholds.minimum_feature_count:
        return ()
    eligible: list[str] = []
    recurrence_count = _number(observation, "recurrence_eligible_count")
    if recurrence_count is not None and recurrence_count > 0:
        eligible.append("semantic_recurrence_cut")
    boundary_count = _number(observation, "boundary_link_eligible_count")
    if boundary_count == 1:
        eligible.append("semantic_boundary_link")
    route_ratio = _number(observation, "route_dominated_ratio")
    worst_source_count = _number(observation, "worst_source_eligible_count")
    if (
        worst_source_count == 1
        and route_ratio is not None
        and route_ratio >= thresholds.route_dominated_min_ratio
    ):
        eligible.append("semantic_worst_source")
    endpoint_count = _number(observation, "endpoint_move_eligible_count")
    if endpoint_count is not None and endpoint_count > 0:
        eligible.append("semantic_endpoint_move")
    eligible.append("semantic_cleanup_fallback")
    return tuple(eligible)


def semantic_preflight_is_fresh(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation | None,
    elapsed_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> bool:
    preflight = state.semantic_preflight
    if preflight is None or state.semantic_mutation is not None:
        return False
    elapsed = _elapsed(elapsed_s)
    if not preflight.issued_at_s <= elapsed <= preflight.expires_at_s:
        return False
    if elapsed >= settings.deadlines.semantics:
        return False
    current = (
        preflight.observation
        if observation is None
        else AnonymousObservation.from_raw(observation)
    )
    if current.items != preflight.observation_items:
        return False
    if preflight.basin_attempts != state.basin_attempts:
        return False
    if preflight.semantic_attempts != state.semantic_attempts:
        return False
    if preflight.threshold_signature != settings.thresholds.signature():
        return False
    return preflight.eligible_action_ids == _semantic_feature_action_ids(
        current, settings.thresholds
    )


def phase_for(
    state: ReplayState,
    elapsed_s: object,
    observation: Mapping[str, object] | AnonymousObservation | None = None,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> str:
    """Derive the strict phase from immutable state and elapsed time."""

    elapsed = _elapsed(elapsed_s)
    deadlines = settings.deadlines
    if state.submission_complete:
        return "complete"
    if state.closeout_complete:
        return "submission"
    if state.semantic_mutation is not None or elapsed >= deadlines.semantics:
        return "closeout"
    if state.basin_mutation is None and elapsed < deadlines.basin:
        if (
            not state.characterization_complete
            and elapsed < deadlines.characterize
        ):
            return "characterize"
        return "basin"
    if not semantic_preflight_is_fresh(
        state, observation, elapsed, settings
    ):
        return "semantic_preflight"
    return "semantics"


# Descriptive alias for callers that prefer a verb.
determine_phase = phase_for


def mark_characterized(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> ReplayState:
    anonymous = AnonymousObservation.from_raw(observation)
    if _feature_count(anonymous) < settings.thresholds.minimum_feature_count:
        raise ReplayPolicyError(
            "characterization lacks the minimum finite aggregate feature count"
        )
    return replace(state, characterization_complete=True)


def build_semantic_preflight(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation,
    elapsed_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> SemanticPreflight:
    """Create a fresh feature snapshot; this operation does not mutate state."""

    elapsed = _elapsed(elapsed_s)
    anonymous = AnonymousObservation.from_raw(observation)
    if elapsed >= settings.deadlines.semantics:
        raise ReplayPolicyError("semantic deadline has passed")
    current_phase = phase_for(state, elapsed, anonymous, settings)
    if current_phase not in {"semantic_preflight", "semantics"}:
        raise ReplayPolicyError(
            f"semantic preflight is unavailable in phase {current_phase!r}"
        )
    eligible = _semantic_feature_action_ids(anonymous, settings.thresholds)
    if not eligible:
        raise ReplayPolicyError(
            "semantic preflight lacks the minimum finite aggregate feature count"
        )
    return SemanticPreflight(
        issued_at_s=elapsed,
        expires_at_s=min(
            settings.deadlines.semantics,
            elapsed + settings.thresholds.semantic_preflight_max_age_s,
        ),
        observation_items=anonymous.items,
        eligible_action_ids=eligible,
        basin_attempts=state.basin_attempts,
        semantic_attempts=state.semantic_attempts,
        threshold_signature=settings.thresholds.signature(),
    )


def install_semantic_preflight(
    state: ReplayState,
    preflight: SemanticPreflight,
) -> ReplayState:
    if preflight.basin_attempts != state.basin_attempts:
        raise ReplayPolicyError("semantic preflight is stale for basin state")
    if preflight.semantic_attempts != state.semantic_attempts:
        raise ReplayPolicyError("semantic preflight is stale for semantic state")
    if state.semantic_mutation is not None:
        raise ReplayPolicyError("semantic mutation slot has already been consumed")
    return replace(state, semantic_preflight=preflight)


def preflight_semantics(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation,
    elapsed_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> ReplayState:
    return install_semantic_preflight(
        state,
        build_semantic_preflight(state, observation, elapsed_s, settings),
    )


def eligible_actions(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation,
    elapsed_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> tuple[ReplayAction, ...]:
    """Return feature-eligible closed templates in deterministic policy order."""

    anonymous = AnonymousObservation.from_raw(observation)
    phase = phase_for(state, elapsed_s, anonymous, settings)
    if phase == "basin" and state.basin_mutation is None:
        action_ids = _basin_feature_action_ids(anonymous, settings.thresholds)
    elif phase == "semantics" and semantic_preflight_is_fresh(
        state, anonymous, elapsed_s, settings
    ):
        # Freshness recomputes the same pure feature predicate and exact order.
        action_ids = state.semantic_preflight.eligible_action_ids  # type: ignore[union-attr]
    else:
        action_ids = ()
    return tuple(action_template(action_id) for action_id in action_ids)


def eligible_action_ids(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation,
    elapsed_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> tuple[str, ...]:
    return tuple(
        action.action_id
        for action in eligible_actions(state, observation, elapsed_s, settings)
    )


def _normalize_eligible_ids(
    eligible: Iterable[str | ReplayAction],
) -> tuple[str, ...]:
    result: list[str] = []
    for item in eligible:
        action_id = item.action_id if isinstance(item, ReplayAction) else str(item)
        if action_id not in ACTION_TEMPLATES:
            raise ReplayPolicyError(f"unknown eligible action_id {action_id!r}")
        if action_id in result:
            raise ReplayPolicyError(f"duplicate eligible action_id {action_id!r}")
        result.append(action_id)
    return tuple(result)


def llm_selection_schema(eligible: Iterable[str | ReplayAction]) -> dict:
    """Return the strict two-field response schema for an LLM call."""

    action_ids = _normalize_eligible_ids(eligible)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "action_id"],
        "properties": {
            "version": {"const": POLICY_VERSION},
            "action_id": {"type": "string", "enum": list(action_ids)},
        },
    }


def parse_llm_selection(
    payload: Mapping[str, object],
    eligible: Iterable[str | ReplayAction],
) -> str:
    """Accept only ``version`` plus one currently eligible ``action_id``."""

    if not isinstance(payload, Mapping):
        raise ReplayPolicyError("LLM replay selection must be an object")
    if set(payload) != {"version", "action_id"}:
        raise ReplayPolicyError(
            "LLM replay selection must contain exactly version and action_id"
        )
    version = payload.get("version")
    if type(version) is not int or version != POLICY_VERSION:
        raise ReplayPolicyError(
            f"LLM replay selection version must be {POLICY_VERSION}"
        )
    action_id = payload.get("action_id")
    if not isinstance(action_id, str):
        raise ReplayPolicyError("LLM replay action_id must be a string")
    eligible_ids = _normalize_eligible_ids(eligible)
    if action_id not in eligible_ids:
        raise ReplayPolicyError(
            f"LLM replay action_id {action_id!r} is not currently eligible"
        )
    return action_id


# Compatibility spelling for callers that refer to the response as a choice.
parse_llm_choice = parse_llm_selection


def select_from_eligible(
    eligible: Sequence[ReplayAction],
    llm_payload: Optional[Mapping[str, object]] = None,
) -> Optional[ReplayAction]:
    """Select an eligible template; no-LLM mode takes the same first item."""

    ordered = tuple(eligible)
    if not ordered:
        return None
    if llm_payload is None:
        return ordered[0]
    selected_id = parse_llm_selection(llm_payload, ordered)
    return next(action for action in ordered if action.action_id == selected_id)


def select_action(
    state: ReplayState,
    observation: Mapping[str, object] | AnonymousObservation,
    elapsed_s: object,
    llm_payload: Optional[Mapping[str, object]] = None,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> Optional[SelectionAuthorization]:
    selected = select_from_eligible(
        eligible_actions(state, observation, elapsed_s, settings),
        llm_payload,
    )
    if selected is None:
        return None
    return authorize_selection(state, selected, observation, elapsed_s, settings)


def authorize_selection(
    state: ReplayState,
    action: str | ReplayAction,
    observation: Mapping[str, object] | AnonymousObservation,
    elapsed_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> SelectionAuthorization:
    """Bind a currently eligible code-owned action to immutable replay state."""

    template = action_template(action) if isinstance(action, str) else action
    if (
        not isinstance(template, ReplayAction)
        or template.policy_id != POLICY_ID
        or ACTION_TEMPLATES.get(template.action_id) != template
    ):
        raise ReplayPolicyError("selection action is not a code-owned replay template")
    elapsed = _elapsed(elapsed_s)
    anonymous = AnonymousObservation.from_raw(observation)
    current_ids = eligible_action_ids(state, anonymous, elapsed, settings)
    if template.action_id not in current_ids:
        raise ReplayPolicyError(
            f"replay action {template.action_id!r} is not currently eligible"
        )
    return SelectionAuthorization(
        action_id=template.action_id,
        mutation_slot=template.mutation_slot,
        selected_at_s=elapsed,
        observation_items=anonymous.items,
        basin_attempts=state.basin_attempts,
        semantic_attempts=state.semantic_attempts,
        authorization_sequence=state.authorization_sequence,
        not_started_cycles=state.not_started_cycles,
        state_signature=replay_state_signature(state),
        deadline_signature=tuple(settings.deadlines.as_dict().items()),
        threshold_signature=settings.thresholds.signature(),
    )


def record_mutation_outcome(
    state: ReplayState,
    authorization: SelectionAuthorization,
    outcome: ActionCycleOutcome | Mapping[str, object],
    completed_at_s: object,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> ReplayState:
    """Retire one selection without re-evaluating completion-time eligibility."""

    if not isinstance(authorization, SelectionAuthorization):
        raise ReplayPolicyError(
            "recording a mutation requires SelectionAuthorization"
        )
    template = authorization.action
    if authorization.deadline_signature != tuple(
        settings.deadlines.as_dict().items()
    ) or authorization.threshold_signature != settings.thresholds.signature():
        raise ReplayPolicyError(
            "selection authorization was issued for different replay settings"
        )
    expected_counters = (
        state.basin_attempts,
        state.semantic_attempts,
        state.authorization_sequence,
        state.not_started_cycles,
    )
    authorization_counters = (
        authorization.basin_attempts,
        authorization.semantic_attempts,
        authorization.authorization_sequence,
        authorization.not_started_cycles,
    )
    if authorization_counters != expected_counters:
        raise ReplayPolicyError("selection authorization is stale or already used")
    if authorization.state_signature != replay_state_signature(state):
        raise ReplayPolicyError("selection authorization does not match replay state")
    if template.mutation_slot == "basin" and state.basin_mutation is not None:
        raise ReplayPolicyError("basin mutation slot has already been consumed")
    if template.mutation_slot == "semantic" and state.semantic_mutation is not None:
        raise ReplayPolicyError("semantic mutation slot has already been consumed")
    normalized_outcome = (
        outcome
        if isinstance(outcome, ActionCycleOutcome)
        else ActionCycleOutcome.from_dict(outcome)
    )
    completed_at = _elapsed(completed_at_s)
    if completed_at < authorization.selected_at_s:
        raise ReplayPolicyError(
            "action-cycle acknowledgement precedes action selection"
        )
    next_sequence = state.authorization_sequence + 1
    if normalized_outcome.execution_status == "not_started":
        return replace(
            state,
            authorization_sequence=next_sequence,
            not_started_cycles=state.not_started_cycles + 1,
        )
    record = RecordedMutation(
        action_id=template.action_id,
        mutation_slot=template.mutation_slot,
        authorization=authorization,
        outcome=normalized_outcome,
        completed_at_s=completed_at,
    )
    if template.mutation_slot == "basin":
        return replace(
            state,
            basin_mutation=record,
            semantic_preflight=None,
            authorization_sequence=next_sequence,
        )
    return replace(
        state,
        semantic_preflight=None,
        semantic_mutation=record,
        authorization_sequence=next_sequence,
    )


def replay_saturation_state(
    outcomes: Sequence[ActionCycleOutcome | RecordedMutation | Mapping[str, object]],
    *,
    low_gain_max_delta_mhz: float = ReplayThresholds().low_gain_max_delta_mhz,
    minimum_consecutive: int = ReplayThresholds().saturation_minimum_consecutive,
) -> dict:
    """Summarize incumbent-relative evidence once per terminal action cycle.

    A qualified cycle contributes only its best centrally measured legal sibling.
    Launched failures, illegal-only cycles, and unknown execution do not enter
    or advance the qualified measured-cycle stream.  ``not_started`` is not a
    launched attempt and neither consumes nor interrupts mutation evidence.
    """

    threshold = _require_finite_number(
        "low_gain_max_delta_mhz", low_gain_max_delta_mhz
    )
    minimum = _require_positive_int("minimum_consecutive", minimum_consecutive)
    if threshold < 0.0:
        raise ReplayPolicyError("low_gain_max_delta_mhz must be nonnegative")
    measured = 0
    unqualified = 0
    not_started = 0
    consecutive_low_gain_no_promotion = 0
    deltas: list[float] = []
    for item in outcomes:
        if isinstance(item, RecordedMutation):
            outcome = item.outcome
        elif isinstance(item, ActionCycleOutcome):
            outcome = item
        else:
            outcome = ActionCycleOutcome.from_dict(item)
        if outcome.execution_status == "not_started":
            not_started += 1
            continue
        if not outcome.qualifies_for_saturation:
            unqualified += 1
            continue
        measured += 1
        delta = float(outcome.best_legal_delta_fmax_mhz_vs_incumbent)
        deltas.append(delta)
        if delta <= threshold and not outcome.promoted:
            consecutive_low_gain_no_promotion += 1
        else:
            consecutive_low_gain_no_promotion = 0
    return {
        "measured_legal_outcomes": measured,
        "unqualified_attempts": unqualified,
        "not_started_cycles": not_started,
        "consecutive_measured_low_gain_no_promotion": (
            consecutive_low_gain_no_promotion
        ),
        "recent_best_legal_delta_fmax_mhz_vs_incumbent": deltas[-4:],
        "low_gain_max_delta_mhz": threshold,
        "minimum_consecutive": minimum,
        "saturated": consecutive_low_gain_no_promotion >= minimum,
    }


# Descriptive alias matching the policy concept rather than its serialization.
saturation_state = replay_saturation_state


def mark_closeout_complete(
    state: ReplayState,
    elapsed_s: object,
    observation: Mapping[str, object] | AnonymousObservation | None = None,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> ReplayState:
    elapsed = _elapsed(elapsed_s)
    phase = phase_for(state, elapsed, observation, settings)
    if phase != "closeout":
        raise ReplayPolicyError(f"closeout cannot complete in phase {phase!r}")
    return replace(
        state,
        closeout_complete=True,
        closeout_completed_at_s=elapsed,
    )


def mark_submission_complete(
    state: ReplayState,
    elapsed_s: object,
    observation: Mapping[str, object] | AnonymousObservation | None = None,
    settings: ReplaySettings = DEFAULT_SETTINGS,
) -> ReplayState:
    if not state.closeout_complete:
        raise ReplayPolicyError("submission cannot complete before closeout")
    elapsed = _elapsed(elapsed_s)
    if (
        state.closeout_completed_at_s is not None
        and elapsed < state.closeout_completed_at_s
    ):
        raise ReplayPolicyError(
            "submission acknowledgement cannot precede closeout acknowledgement"
        )
    phase = phase_for(state, elapsed, observation, settings)
    if phase != "submission":
        raise ReplayPolicyError(f"submission cannot complete in phase {phase!r}")
    return replace(
        state,
        submission_complete=True,
        submission_completed_at_s=elapsed,
    )


class ReplayPolicy:
    """Convenience facade over the pure replay functions."""

    policy_id = POLICY_ID
    version = POLICY_VERSION
    phases = PHASES
    immutable_incumbent = True

    def __init__(self, config: object = None):
        if config is None:
            self.settings = DEFAULT_SETTINGS
        elif isinstance(config, ReplaySettings):
            self.settings = config
        else:
            self.settings = ReplaySettings.from_config(config)

    def initial_state(self) -> ReplayState:
        return initial_state()

    def sanitize_observation(
        self, raw: Mapping[str, object] | AnonymousObservation
    ) -> dict:
        return sanitize_observation(raw)

    def characterize(
        self,
        state: ReplayState,
        observation: Mapping[str, object] | AnonymousObservation,
    ) -> ReplayState:
        return mark_characterized(state, observation, self.settings)

    def phase(
        self,
        state: ReplayState,
        elapsed_s: object,
        observation: Mapping[str, object] | AnonymousObservation | None = None,
    ) -> str:
        return phase_for(state, elapsed_s, observation, self.settings)

    def preflight(
        self,
        state: ReplayState,
        observation: Mapping[str, object] | AnonymousObservation,
        elapsed_s: object,
    ) -> ReplayState:
        return preflight_semantics(
            state, observation, elapsed_s, self.settings
        )

    def preflight_is_fresh(
        self,
        state: ReplayState,
        observation: Mapping[str, object] | AnonymousObservation | None,
        elapsed_s: object,
    ) -> bool:
        return semantic_preflight_is_fresh(
            state, observation, elapsed_s, self.settings
        )

    def eligible_actions(
        self,
        state: ReplayState,
        observation: Mapping[str, object] | AnonymousObservation,
        elapsed_s: object,
    ) -> tuple[ReplayAction, ...]:
        return eligible_actions(state, observation, elapsed_s, self.settings)

    def eligible_action_ids(
        self,
        state: ReplayState,
        observation: Mapping[str, object] | AnonymousObservation,
        elapsed_s: object,
    ) -> tuple[str, ...]:
        return eligible_action_ids(state, observation, elapsed_s, self.settings)

    def select(
        self,
        state: ReplayState,
        observation: Mapping[str, object] | AnonymousObservation,
        elapsed_s: object,
        llm_payload: Optional[Mapping[str, object]] = None,
    ) -> Optional[SelectionAuthorization]:
        return select_action(
            state,
            observation,
            elapsed_s,
            llm_payload,
            self.settings,
        )

    def record_outcome(
        self,
        state: ReplayState,
        authorization: SelectionAuthorization,
        outcome: ActionCycleOutcome | Mapping[str, object],
        completed_at_s: object,
    ) -> ReplayState:
        return record_mutation_outcome(
            state,
            authorization,
            outcome,
            completed_at_s,
            self.settings,
        )

    def saturation(self, state: ReplayState) -> dict:
        thresholds = self.settings.thresholds
        summary = replay_saturation_state(
            state.mutations,
            low_gain_max_delta_mhz=thresholds.low_gain_max_delta_mhz,
            minimum_consecutive=thresholds.saturation_minimum_consecutive,
        )
        summary["not_started_cycles"] = state.not_started_cycles
        return summary

    def closeout(
        self,
        state: ReplayState,
        elapsed_s: object,
        observation: Mapping[str, object] | AnonymousObservation | None = None,
    ) -> ReplayState:
        return mark_closeout_complete(
            state, elapsed_s, observation, self.settings
        )

    def submit(
        self,
        state: ReplayState,
        elapsed_s: object,
        observation: Mapping[str, object] | AnonymousObservation | None = None,
    ) -> ReplayState:
        return mark_submission_complete(
            state, elapsed_s, observation, self.settings
        )

    def metadata(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "schema_version": SCHEMA_VERSION,
            "phases": list(self.phases),
            "deadlines_s": self.settings.deadlines.as_dict(),
            "thresholds": self.settings.thresholds.as_dict(),
            "action_catalog_signature": ACTION_CATALOG_SIGNATURE,
            "immutable_incumbent": True,
        }
