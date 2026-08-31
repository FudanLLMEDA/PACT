"""Union dispatcher for current and teammate proof-gated operator actions."""

from __future__ import annotations

from types import MappingProxyType

from . import operator_rewrite_current as _current
from . import operator_rewrite_teammate as _teammate


MAX_CANDIDATES = 1
QOR_PROFILES = tuple(getattr(_teammate, "QOR_PROFILES", ()))
OperatorRewriteTemplate = _teammate.OperatorRewriteTemplate

# Preserve the historical module-level patch points used by the current
# executors and their tests.  The dispatcher copies these bindings into the
# current provider immediately before execution, so monkeypatches and injected
# validators still reach the actual backend rather than stopping at the facade.
run_strict_validation_worker = _current.run_strict_validation_worker
build_compressor_certificate = _teammate.build_compressor_certificate
build_recurrence_family_action_plans = (
    _current.build_recurrence_family_action_plans
)
allocate_recurrence_family_srl_placements = (
    _current.allocate_recurrence_family_srl_placements
)

_CURRENT_PROVIDER_TARGETS = frozenset(
    target
    for target in _current.OPERATOR_REWRITE_REGISTRY
    if _current.operator_rewrite_executor_ready(target)
)

_TEAMMATE_PROVIDER_TARGETS = frozenset(
    target
    for target in _teammate.OPERATOR_REWRITE_REGISTRY
    if _teammate.operator_rewrite_executor_ready(target)
)


def _provider_for_target(target: str) -> str:
    """Resolve one immutable owner with executor-ready current precedence."""
    if target in _CURRENT_PROVIDER_TARGETS:
        return "current"
    if target in _TEAMMATE_PROVIDER_TARGETS:
        return "teammate"
    raise RuntimeError(
        f"operator rewrite target {target!r} has no executor-ready provider"
    )


_ALL_TARGETS = tuple(dict.fromkeys((
    *tuple(_current.OPERATOR_REWRITE_REGISTRY),
    *tuple(_teammate.OPERATOR_REWRITE_REGISTRY),
)))
OPERATOR_REWRITE_OWNERS = MappingProxyType({
    target: _provider_for_target(target) for target in _ALL_TARGETS
})
OPERATOR_REWRITE_PROOF_ABI = MappingProxyType({
    target: (
        _current.PROOF_VERSION
        if provider == "current"
        else _teammate.PROOF_VERSION
    )
    for target, provider in OPERATOR_REWRITE_OWNERS.items()
})

OPERATOR_REWRITE_REGISTRY = MappingProxyType({
    target: (
        _current.OPERATOR_REWRITE_REGISTRY[target]
        if OPERATOR_REWRITE_OWNERS[target] == "current"
        else _teammate.OPERATOR_REWRITE_REGISTRY[target]
    )
    for target in _ALL_TARGETS
})
OPERATOR_REWRITE_TARGETS = tuple(OPERATOR_REWRITE_REGISTRY)


def operator_rewrite_executor_ready(target: str) -> bool:
    value = str(target)
    provider = OPERATOR_REWRITE_OWNERS.get(value)
    if provider == "current":
        return _current.operator_rewrite_executor_ready(value)
    if provider == "teammate":
        return _teammate.operator_rewrite_executor_ready(value)
    return False


def operator_rewrite_catalog() -> dict[str, dict[str, object]]:
    teammate_catalog = _teammate.operator_rewrite_catalog()
    current_catalog = _current.operator_rewrite_catalog()
    catalog: dict[str, dict[str, object]] = {}
    for target, spec in OPERATOR_REWRITE_REGISTRY.items():
        provider = OPERATOR_REWRITE_OWNERS[target]
        source = (
            current_catalog.get(target, {})
            if provider == "current"
            else teammate_catalog.get(target, {})
        )
        catalog[target] = {
            **source,
            "rewrite_kind": spec.rewrite_kind,
            "proof_template": spec.proof_template,
            "execution_model": spec.execution_model,
            "execution_status": (
                spec.execution_status
                if operator_rewrite_executor_ready(target)
                else "blocked_missing_current_seed_backend"
            ),
            "proof_templates": list(
                getattr(spec, "proof_templates", ()) or (spec.proof_template,)
            ),
            "provider": provider,
            "proof_abi": OPERATOR_REWRITE_PROOF_ABI[target],
        }
    return catalog


def operator_rewrite_provider_contract() -> dict[str, object]:
    """Return the fail-closed owner/ABI/readiness contract for audit and tests."""
    targets = {}
    for target in OPERATOR_REWRITE_TARGETS:
        provider = OPERATOR_REWRITE_OWNERS[target]
        targets[target] = {
            "provider": provider,
            "proof_abi": OPERATOR_REWRITE_PROOF_ABI[target],
            "executor_ready": operator_rewrite_executor_ready(target),
            "overlap": (
                target in _current.OPERATOR_REWRITE_REGISTRY
                and target in _teammate.OPERATOR_REWRITE_REGISTRY
            ),
            "current_precedence_applied": (
                target not in _CURRENT_PROVIDER_TARGETS or provider == "current"
            ),
        }
    contract = {
        "schema_version": "operator-rewrite-provider-contract-v1",
        "selection_policy": "executor_ready_current_then_executor_ready_teammate",
        "target_count": len(targets),
        "targets": targets,
    }
    if (
        set(targets) != set(OPERATOR_REWRITE_REGISTRY)
        or any(not row["executor_ready"] for row in targets.values())
        or any(not row["current_precedence_applied"] for row in targets.values())
        or any(
            row["provider"] not in {"current", "teammate"}
            or row["proof_abi"] not in {
                _current.PROOF_VERSION, _teammate.PROOF_VERSION
            }
            for row in targets.values()
        )
    ):
        raise RuntimeError("operator rewrite provider contract is inconsistent")
    return contract


recover_constructive_nomination_contract = (
    _current.recover_constructive_nomination_contract
)
recover_memory_forward_nomination_contract = (
    _current.recover_memory_forward_nomination_contract
)
screen_fixed_point_dsp_contract = _teammate.screen_fixed_point_dsp_contract


class OperatorRewriteSkill:
    """Dispatch one registered target to the owning fail-closed executor."""

    name = "operator_rewrite"

    def __init__(self) -> None:
        self._current = _current.OperatorRewriteSkill()
        self._teammate = _teammate.OperatorRewriteSkill()

    def __getattr__(self, name):
        # The facade must preserve the complete current-provider surface, not
        # only its public execute method.  Existing cold-preflight callers use
        # current provider recovery helpers directly, while newly integrated
        # targets may expose teammate-only helpers.
        if hasattr(self._current, name):
            return getattr(self._current, name)
        if hasattr(self._teammate, name):
            return getattr(self._teammate, name)
        raise AttributeError(name)

    async def execute(self, mcp, target, params, before_wns, clock_period):
        owner = OPERATOR_REWRITE_OWNERS.get(str(target))
        if owner == "current":
            _current.run_strict_validation_worker = run_strict_validation_worker
            _current.build_recurrence_family_action_plans = (
                build_recurrence_family_action_plans
            )
            _current.allocate_recurrence_family_srl_placements = (
                allocate_recurrence_family_srl_placements
            )
            return await self._current.execute(
                mcp, target, params, before_wns, clock_period
            )
        if owner != "teammate":
            # Preserve the public skill contract for an unknown target: return
            # one typed, side-effect-free failure instead of raising through
            # the Agent loop. Registered targets can never reach this branch
            # because the provider contract is constructed fail-closed.
            return await self._current.execute(
                mcp, target, params, before_wns, clock_period
            )
        _teammate.build_compressor_certificate = build_compressor_certificate
        for name, value in self.__dict__.items():
            if name.startswith("_execute_"):
                setattr(self._teammate, name, value)
        return await self._teammate.execute(
            mcp, target, params, before_wns, clock_period
        )

    async def prepare_concurrent_proof(self, **kwargs):
        target = str(kwargs.get("target") or "")
        if OPERATOR_REWRITE_OWNERS.get(target) != "current":
            raise RuntimeError(
                "concurrent proof preparation is not registered for this target"
            )
        return await self._current.prepare_concurrent_proof(**kwargs)


__all__ = [
    "MAX_CANDIDATES",
    "QOR_PROFILES",
    "OPERATOR_REWRITE_REGISTRY",
    "OPERATOR_REWRITE_TARGETS",
    "OPERATOR_REWRITE_OWNERS",
    "OPERATOR_REWRITE_PROOF_ABI",
    "OperatorRewriteSkill",
    "OperatorRewriteTemplate",
    "allocate_recurrence_family_srl_placements",
    "build_compressor_certificate",
    "build_recurrence_family_action_plans",
    "operator_rewrite_catalog",
    "operator_rewrite_executor_ready",
    "operator_rewrite_provider_contract",
    "recover_constructive_nomination_contract",
    "recover_memory_forward_nomination_contract",
    "run_strict_validation_worker",
    "screen_fixed_point_dsp_contract",
]


def __getattr__(name: str):
    """Retain provider constants and helpers used by existing integrations."""
    if hasattr(_current, name):
        return getattr(_current, name)
    if hasattr(_teammate, name):
        return getattr(_teammate, name)
    raise AttributeError(name)
