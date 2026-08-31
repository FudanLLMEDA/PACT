"""Minimal host-local scheduler for Agent probe/proof/mutation task graphs."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


_SHA256 = re.compile(r"[0-9a-f]{64}")


class TaskKind(str, Enum):
    PROBE = "probe"
    PROOF = "proof"
    MUTATION = "mutation"
    MEASUREMENT = "measurement"
    VALIDATION = "validation"


class TaskSchedulingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    kind: TaskKind
    seed_artifact_sha256: str
    workspace: Path
    cpu_cores: int
    memory_gib: float
    exclusive_session: str = ""

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.kind, TaskKind):
            raise ValueError("task requires a typed nonempty identity")
        if _SHA256.fullmatch(self.seed_artifact_sha256) is None:
            raise ValueError("task seed must be a SHA-256")
        if isinstance(self.cpu_cores, bool) or not 1 <= self.cpu_cores <= 8:
            raise ValueError("task cpu_cores must be in 1..8")
        if not 0.0 < float(self.memory_gib) <= 32.0:
            raise ValueError("task memory_gib must be in (0, 32]")


class LocalTaskScheduler:
    """Admit isolated siblings under one host-wide CPU/RAM/session cap."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        cpu_limit: int = 8,
        memory_limit_gib: float = 32.0,
    ) -> None:
        if isinstance(cpu_limit, bool) or not 1 <= cpu_limit <= 8:
            raise ValueError("scheduler cpu_limit must be in 1..8")
        if not 0.0 < float(memory_limit_gib) <= 32.0:
            raise ValueError("scheduler memory_limit_gib must be in (0, 32]")
        self.workspace_root = Path(workspace_root).resolve()
        self.cpu_limit = int(cpu_limit)
        self.memory_limit_gib = float(memory_limit_gib)
        self._cpu_used = 0
        self._memory_used = 0.0
        self._sessions: set[str] = set()
        self._active: dict[str, TaskSpec] = {}
        self._condition = asyncio.Condition()

    def _validate(self, spec: TaskSpec) -> None:
        workspace = Path(spec.workspace).resolve()
        if workspace == self.workspace_root or self.workspace_root not in workspace.parents:
            raise TaskSchedulingError("task workspace must be an isolated child directory")
        if spec.cpu_cores > self.cpu_limit or spec.memory_gib > self.memory_limit_gib:
            raise TaskSchedulingError("task resource request exceeds the host cap")

    def _fits(self, spec: TaskSpec) -> bool:
        return (
            self._cpu_used + spec.cpu_cores <= self.cpu_limit
            and self._memory_used + spec.memory_gib <= self.memory_limit_gib
            and (
                not spec.exclusive_session
                or spec.exclusive_session not in self._sessions
            )
        )

    async def _acquire(self, spec: TaskSpec) -> None:
        self._validate(spec)
        async with self._condition:
            if spec.task_id in self._active:
                raise TaskSchedulingError(f"task {spec.task_id!r} is already active")
            await self._condition.wait_for(lambda: self._fits(spec))
            self._cpu_used += spec.cpu_cores
            self._memory_used += float(spec.memory_gib)
            if spec.exclusive_session:
                self._sessions.add(spec.exclusive_session)
            self._active[spec.task_id] = spec

    async def _release(self, spec: TaskSpec) -> None:
        async with self._condition:
            active = self._active.pop(spec.task_id, None)
            if active is None:
                raise TaskSchedulingError(f"task {spec.task_id!r} has no lease")
            self._cpu_used -= spec.cpu_cores
            self._memory_used -= float(spec.memory_gib)
            if spec.exclusive_session:
                self._sessions.remove(spec.exclusive_session)
            self._condition.notify_all()

    async def run(
        self,
        spec: TaskSpec,
        operation: Callable[[], Any | Awaitable[Any]],
    ) -> Any:
        await self._acquire(spec)
        try:
            Path(spec.workspace).resolve().mkdir(parents=True, exist_ok=True)
            value = operation()
            return await value if inspect.isawaitable(value) else value
        finally:
            await self._release(spec)

    def snapshot(self) -> dict[str, Any]:
        return {
            "cpu_limit": self.cpu_limit,
            "memory_limit_gib": self.memory_limit_gib,
            "cpu_used": self._cpu_used,
            "memory_used_gib": self._memory_used,
            "exclusive_sessions": sorted(self._sessions),
            "active_task_ids": sorted(self._active),
        }


__all__ = [
    "LocalTaskScheduler",
    "TaskKind",
    "TaskSchedulingError",
    "TaskSpec",
]
