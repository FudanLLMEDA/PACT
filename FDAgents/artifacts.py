"""Atomic file publication and artifact hashing primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path


_ACTION_DIR_RE = re.compile(r"^a\d+_[0-9a-f]{12}$")
_CANDIDATE_ID_RE = re.compile(r"^c\d{6}$")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically publish UTF-8 text without truncating the prior file."""
    destination = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically publish formatted JSON without truncating the prior state."""
    atomic_write_text(Path(path), json.dumps(payload, indent=2, default=str))


def atomic_copy_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Copy and hash one no-follow source descriptor before atomic publish."""
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.is_symlink() or not source_path.is_file():
        raise FileNotFoundError(f"source is not a regular file: {source_path}")
    if expected_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_sha256)
    ):
        raise ValueError("expected source SHA-256 is invalid")
    source_descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    source_stat = os.fstat(source_descriptor)
    if not stat.S_ISREG(source_stat.st_mode):
        os.close(source_descriptor)
        raise FileNotFoundError(f"source is not a regular file: {source_path}")
    descriptor = None
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        with os.fdopen(source_descriptor, "rb") as source_stream, os.fdopen(
            descriptor, "wb"
        ) as destination_stream:
            while chunk := source_stream.read(1024 * 1024):
                digest.update(chunk)
                destination_stream.write(chunk)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
            os.fchmod(destination_stream.fileno(), stat.S_IMODE(source_stat.st_mode))
        copied_sha256 = digest.hexdigest()
        if expected_sha256 is not None and copied_sha256 != expected_sha256:
            raise RuntimeError("source identity changed before atomic copy")
        os.replace(temporary, destination_path)
        _fsync_directory(destination_path.parent)
        if sha256_file(destination_path) != copied_sha256:
            raise RuntimeError("published copy identity mismatch")
    finally:
        try:
            os.close(source_descriptor)
        except OSError:
            pass
        try:
            if descriptor is not None:
                os.close(descriptor)
        except OSError:
            pass
        if temporary is not None and temporary.exists():
            temporary.unlink()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for one regular file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_candidate_path(run_dir: Path, candidate_id: str) -> Path:
    if not re.fullmatch(r"c\d{6}", str(candidate_id)):
        raise ValueError(f"invalid candidate ID {candidate_id!r}")
    return Path(run_dir).resolve() / "candidates" / str(candidate_id) / "design.dcp"


def validate_owned_candidate_path(
    run_dir: Path,
    candidate_id: str,
    stored_path: Path,
) -> Path:
    """Return the exact graph-owned path or fail before any deletion."""
    expected = canonical_candidate_path(run_dir, candidate_id)
    candidate_dir = expected.parent
    if candidate_dir.is_symlink() or expected.is_symlink():
        raise ValueError(f"candidate {candidate_id} uses a symlinked artifact path")
    if Path(stored_path).resolve() != expected:
        raise ValueError(
            f"candidate {candidate_id} artifact path is outside graph ownership"
        )
    return expected


def protected_candidate_ids(memory) -> set[str]:
    graph = memory.candidate_graph or {}
    nodes = graph.get("nodes") or {}
    protected = {
        str(graph.get("root_candidate_id")),
        str(graph.get("current_best_candidate_id")),
        *(str(value) for value in graph.get("beam_candidate_ids", [])),
        *(
            str(value)
            for value in (graph.get("enabling_pool") or {}).get(
                "active_candidate_ids", []
            )
        ),
    }
    transaction = getattr(memory, "pending_transaction", {}) or {}
    for key in ("candidate_id", "seed_candidate_id"):
        if transaction.get(key):
            protected.add(str(transaction[key]))
    protected.update(str(value) for value in transaction.get("candidate_ids", []))
    protected.update(
        str(item.get("candidate_id"))
        for item in transaction.get("candidates", [])
        if item.get("candidate_id")
    )
    replay_control = getattr(memory, "replay_control", {}) or {}
    for record in (replay_control.get("slot_bindings") or {}).values():
        binding = (record or {}).get("incumbent_binding") or {}
        if binding.get("candidate_id"):
            protected.add(str(binding["candidate_id"]))
    closeout = replay_control.get("closeout") or {}
    if closeout.get("selected_candidate_id"):
        protected.add(str(closeout["selected_candidate_id"]))
    submission = replay_control.get("submission") or {}
    for key in ("attempted_candidate_id", "output_candidate_id"):
        if submission.get(key):
            protected.add(str(submission[key]))
    descriptor_fields = {
        "kind", "version", "template", "bundle_sha256",
        "source_candidate_id", "source_artifact_sha256",
        "family_contract_sha256", "placement_manifest_sha256",
        "authorization_id", "emitted_topology_attestation_id",
        "operator_candidate_certificate_id",
    }
    for candidate_id, node in nodes.items():
        evidence = node.get("evidence") or {}
        descriptor = evidence.get("recurrence_family_lineage") or {}
        proof = node.get("eco_proof") or {}
        source_id = descriptor.get("source_candidate_id")
        source_node = nodes.get(source_id) if isinstance(source_id, str) else None
        cursor = candidate_id
        source_is_ancestor = False
        for _ in range(len(nodes) + 1):
            if cursor == source_id:
                source_is_ancestor = True
                break
            ancestor = nodes.get(cursor)
            if ancestor is None or ancestor.get("parent_candidate_id") is None:
                break
            cursor = str(ancestor["parent_candidate_id"])
        if (
            isinstance(descriptor, dict)
            and set(descriptor) == descriptor_fields
            and descriptor.get("kind") == "recurrence_family_candidate_lineage"
            and descriptor.get("version") == 1
            and proof.get("skill") == "operator_rewrite"
            and proof.get("template") == "dsp_srl_recurrence_cut_v1"
            and proof.get("source_artifact_sha256")
            == descriptor.get("source_artifact_sha256")
            and proof.get("artifact_sha256") == node.get("artifact_sha256")
            and isinstance(source_id, str)
            and _CANDIDATE_ID_RE.fullmatch(source_id) is not None
            and isinstance(source_node, dict)
            and source_node.get("artifact_sha256")
            == descriptor.get("source_artifact_sha256")
            and source_is_ancestor
        ):
            protected.add(source_id)
    protected.discard("None")
    return protected


def _tree_size_without_following_links(path: Path) -> int:
    total = 0
    for root, _directories, files in os.walk(path, followlinks=False):
        for filename in files:
            try:
                total += (Path(root) / filename).stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def delete_owned_action_tree(run_dir: Path, action_dir: Path) -> int:
    """Delete one exact action scratch tree after its result is durable."""
    actions_root = Path(run_dir).resolve() / "actions"
    candidate = Path(action_dir)
    if not _ACTION_DIR_RE.fullmatch(candidate.name):
        raise ValueError(f"invalid action directory name {candidate.name!r}")
    if candidate.is_symlink() or candidate.resolve().parent != actions_root:
        raise ValueError("action directory is outside run ownership")
    if not candidate.exists():
        return 0
    reclaimed = _tree_size_without_following_links(candidate)
    shutil.rmtree(candidate)
    return reclaimed


def _gc_state(memory) -> dict:
    state = memory.artifact_gc
    state.setdefault("runs", 0)
    state.setdefault("candidate_artifacts_deleted", 0)
    state.setdefault("action_trees_deleted", 0)
    state.setdefault("reclaimed_bytes", 0)
    state.setdefault("failures", [])
    return state


def cleanup_level1_artifacts(
    memory,
    *,
    run_dir: Path,
    memory_path: Path,
    action_dir: Path | None = None,
) -> dict:
    """Collect committed scratch and unselectable graph-owned candidates."""
    state = _gc_state(memory)
    state["runs"] += 1
    if action_dir is not None:
        reclaimed = delete_owned_action_tree(run_dir, action_dir)
        if reclaimed or not Path(action_dir).exists():
            state["action_trees_deleted"] += 1
            state["reclaimed_bytes"] += reclaimed

    graph = memory.candidate_graph or {}
    nodes = graph.get("nodes") or {}
    protected = protected_candidate_ids(memory)
    pending = list(dict.fromkeys(graph.get("pending_gc_candidate_ids", [])))
    new_pending = []
    for candidate_id, node in nodes.items():
        if candidate_id in protected or node.get("artifact_state") != "materialized":
            continue
        if node.get("search_state") not in {"pruned", "ineligible"}:
            continue
        try:
            owned = validate_owned_candidate_path(
                run_dir, candidate_id, Path(str(node.get("artifact_path", "")))
            )
        except ValueError as exc:
            state["failures"].append({
                "candidate_id": candidate_id,
                "reason": str(exc),
                "timestamp": time.time(),
            })
            continue
        if not owned.is_file():
            node["artifact_state"] = "missing"
            continue
        if candidate_id not in pending:
            new_pending.append(candidate_id)

    pending.extend(new_pending)
    graph["pending_gc_candidate_ids"] = pending
    memory.save(memory_path)

    for candidate_id in list(pending):
        node = nodes.get(candidate_id)
        if node is None or candidate_id in protected:
            pending.remove(candidate_id)
            continue
        try:
            owned = validate_owned_candidate_path(
                run_dir, candidate_id, Path(str(node.get("artifact_path", "")))
            )
            reclaimed = owned.stat().st_size if owned.is_file() else 0
            if owned.is_file():
                owned.unlink()
            try:
                owned.parent.rmdir()
            except OSError:
                pass
            node["artifact_state"] = "deleted"
            node["artifact_deleted_at"] = time.time()
            node["artifact_delete_reason"] = "level1_unselectable_candidate_gc"
            state["candidate_artifacts_deleted"] += 1
            state["reclaimed_bytes"] += reclaimed
            pending.remove(candidate_id)
        except (OSError, ValueError) as exc:
            state["failures"].append({
                "candidate_id": candidate_id,
                "reason": str(exc),
                "timestamp": time.time(),
            })
    state["failures"] = state["failures"][-20:]
    state["last_run_at"] = time.time()
    graph["pending_gc_candidate_ids"] = pending
    memory.save(memory_path)
    return dict(state)
