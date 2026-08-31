"""Immutable LLM corpora and bounded, phase-scoped grep mechanics."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from .artifacts import atomic_write_json, atomic_write_text, sha256_file
from .config import Config


class CorpusError(ValueError):
    """Raised when a corpus or grep request violates its fail-closed contract."""


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _snapshot_digest(scope: str, identity: dict, records: list[dict]) -> str:
    descriptor = json.dumps(
        {"scope": scope, "identity": identity, "documents": records},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return _digest_bytes(descriptor)


def _document_name(value: str) -> str:
    name = str(value).replace("\\", "/")
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CorpusError(f"invalid corpus document name: {value!r}")
    return str(path)


def _validate_python_fallback_pattern(pattern: str) -> None:
    """Reject regex constructs that are unsafe without ripgrep's linear engine."""
    if "(?" in pattern or re.search(r"\\[1-9]", pattern):
        raise CorpusError(
            "Python grep fallback rejects lookaround, special groups, and backreferences"
        )
    if re.search(r"\([^)]*[*+{][^)]*\)\s*[*+{]", pattern):
        raise CorpusError("Python grep fallback rejects nested repetition")


@dataclass(frozen=True)
class EvidenceBinding:
    """Trusted internal binding for one public short evidence reference."""

    scope: str
    document: str
    line: int
    snapshot_id: str
    document_sha256: str


@dataclass(frozen=True)
class CorpusSnapshot:
    """One content-addressed, immutable set of UTF-8 documents."""

    root: Path
    scope: str
    snapshot_id: str
    identity: dict
    documents: tuple[dict, ...]

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def manifest_for_llm(self) -> dict:
        manifest = {
            "scope": self.scope,
            "documents": [
                {
                    "name": item["name"],
                    "lines": item["lines"],
                }
                for item in self.documents
            ],
        }
        epoch = self.identity.get("telemetry_epoch")
        if type(epoch) is int and epoch >= 0:
            manifest["telemetry_epoch"] = epoch
        return manifest

    def evidence_binding(self, ref: object) -> Optional[EvidenceBinding]:
        """Resolve a short citation against this exact immutable snapshot."""
        text = str(ref)
        prefix = f"{self.scope}:"
        if not text.startswith(prefix):
            return None
        try:
            document_name, line_text = text[len(prefix) :].rsplit(":", 1)
            line_number = int(line_text)
        except (TypeError, ValueError):
            return None
        record = next(
            (
                item
                for item in self.documents
                if item["name"] == document_name
            ),
            None,
        )
        if record is None or line_number < 1 or line_number > int(record["lines"]):
            return None
        # Recheck the content-addressed file before issuing an internal binding.
        self.document_path(document_name)
        return EvidenceBinding(
            scope=self.scope,
            document=document_name,
            line=line_number,
            snapshot_id=self.snapshot_id,
            document_sha256=str(record["sha256"]),
        )

    def resolves_evidence_ref(self, ref: object) -> bool:
        """Return whether a short citation binds to this verified snapshot."""
        try:
            return self.evidence_binding(ref) is not None
        except CorpusError:
            return False

    def document_path(self, name: str) -> Path:
        normalized = _document_name(name)
        record = next(
            (item for item in self.documents if item["name"] == normalized), None
        )
        if record is None:
            raise CorpusError(f"document is not in {self.scope} corpus: {normalized}")
        path = self.root / normalized
        if path.is_symlink() or not path.is_file():
            raise CorpusError(f"corpus document is not a regular file: {normalized}")
        if path.resolve().parent != self.root.resolve() and self.root.resolve() not in path.resolve().parents:
            raise CorpusError(f"corpus document escaped its snapshot: {normalized}")
        if path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise CorpusError(f"corpus document changed after publication: {normalized}")
        return path

    @classmethod
    def create(
        cls,
        corpus_root: Path,
        *,
        scope: str,
        documents: dict[str, str],
        identity: Optional[dict] = None,
    ) -> "CorpusSnapshot":
        if scope not in {"report", "knowledge"}:
            raise CorpusError(f"unsupported corpus scope: {scope!r}")
        normalized: dict[str, str] = {}
        records = []
        for raw_name, raw_text in sorted(documents.items()):
            name = _document_name(raw_name)
            if name in normalized:
                raise CorpusError(f"duplicate corpus document name: {name}")
            text = str(raw_text)
            encoded = text.encode("utf-8")
            normalized[name] = text
            records.append({
                "name": name,
                "sha256": _digest_bytes(encoded),
                "bytes": len(encoded),
                "lines": len(text.splitlines()),
            })
        if not records:
            raise CorpusError(f"{scope} corpus must contain at least one document")

        identity = dict(identity or {})
        snapshot_id = _snapshot_digest(scope, identity, records)
        root = Path(corpus_root).resolve() / scope / snapshot_id
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": 1,
            "scope": scope,
            "snapshot_id": snapshot_id,
            "identity": identity,
            "documents": records,
        }
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing != manifest:
                raise CorpusError(f"immutable corpus manifest changed: {snapshot_id}")
        else:
            for name, text in normalized.items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise CorpusError(f"unmanifested corpus file already exists: {name}")
                atomic_write_text(destination, text)
            atomic_write_json(manifest_path, manifest)
        return cls.load(manifest_path, expected_scope=scope)

    @classmethod
    def load(
        cls, manifest_path: Path, *, expected_scope: Optional[str] = None
    ) -> "CorpusSnapshot":
        path = Path(manifest_path).resolve()
        if path.is_symlink() or not path.is_file() or path.name != "manifest.json":
            raise CorpusError(f"invalid corpus manifest path: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        scope = str(payload.get("scope", ""))
        if expected_scope is not None and scope != expected_scope:
            raise CorpusError(
                f"expected {expected_scope} corpus, found {scope or 'unknown'}"
            )
        snapshot_id = str(payload.get("snapshot_id", ""))
        if path.parent.name != snapshot_id or len(snapshot_id) != 64:
            raise CorpusError("corpus snapshot directory does not match manifest ID")
        raw_records = payload.get("documents")
        if payload.get("version") != 1 or not isinstance(raw_records, list) or not raw_records:
            raise CorpusError("unsupported or empty corpus manifest")
        records = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                raise CorpusError("invalid corpus document record")
            try:
                records.append({
                    "name": _document_name(raw.get("name", "")),
                    "sha256": str(raw["sha256"]),
                    "bytes": int(raw["bytes"]),
                    "lines": int(raw["lines"]),
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise CorpusError("invalid corpus document record") from exc
        identity = dict(payload.get("identity") or {})
        if _snapshot_digest(scope, identity, records) != snapshot_id:
            raise CorpusError("corpus manifest does not match its content address")
        seen = set()
        for item in records:
            name = _document_name(item.get("name", ""))
            if name in seen:
                raise CorpusError(f"duplicate corpus manifest document: {name}")
            seen.add(name)
            document = path.parent / name
            if document.is_symlink() or not document.is_file():
                raise CorpusError(f"corpus document missing or symlinked: {name}")
            if sha256_file(document) != item.get("sha256"):
                raise CorpusError(f"corpus document hash mismatch: {name}")
            if document.stat().st_size != item.get("bytes"):
                raise CorpusError(f"corpus document size mismatch: {name}")
            line_count = len(
                document.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            if line_count != item.get("lines"):
                raise CorpusError(f"corpus document line-count mismatch: {name}")
        return cls(
            root=path.parent,
            scope=scope,
            snapshot_id=snapshot_id,
            identity=identity,
            documents=tuple(dict(item) for item in records),
        )


@dataclass(frozen=True)
class GrepLimits:
    max_pattern_chars: int
    max_files: int
    max_matches: int
    max_output_bytes: int
    max_context_lines: int
    max_line_chars: int
    timeout_s: float
    max_calls: int
    max_total_output_bytes: int

    @classmethod
    def from_config(cls, cfg: Config, stage: str) -> "GrepLimits":
        prefix = "react.grep"
        stage_calls = cfg.get(f"{prefix}.max_calls_{stage}_stage")
        if stage_calls is None:
            raise CorpusError(f"unknown grep stage: {stage}")
        return cls(
            max_pattern_chars=int(cfg.require(f"{prefix}.max_pattern_chars")),
            max_files=int(cfg.require(f"{prefix}.max_files")),
            max_matches=int(cfg.require(f"{prefix}.max_matches_per_call")),
            max_output_bytes=int(cfg.require(f"{prefix}.max_output_bytes_per_call")),
            max_context_lines=int(cfg.require(f"{prefix}.max_context_lines")),
            max_line_chars=int(cfg.require(f"{prefix}.max_line_chars")),
            timeout_s=float(cfg.require(f"{prefix}.timeout_s")),
            max_calls=int(stage_calls),
            max_total_output_bytes=int(cfg.require(f"{prefix}.max_total_output_bytes")),
        )


class CorpusView:
    """A fixed allowlist of snapshots with stateful per-stage grep budgets."""

    def __init__(self, snapshots: Iterable[CorpusSnapshot], limits: GrepLimits):
        items = tuple(snapshots)
        if not items:
            raise CorpusError("a corpus view requires at least one snapshot")
        scopes = [item.scope for item in items]
        if len(scopes) != len(set(scopes)):
            raise CorpusError("a corpus view may include only one snapshot per scope")
        self.snapshots = items
        self.limits = limits
        self.calls_used = 0
        self.output_bytes_used = 0
        self.transcript: list[dict] = []
        self._query_numbers: dict[str, int] = {}

    def manifest_for_llm(self) -> list[dict]:
        return [snapshot.manifest_for_llm() for snapshot in self.snapshots]

    def _documents(self) -> dict[str, tuple[CorpusSnapshot, str]]:
        return {
            f"{snapshot.scope}/{item['name']}": (snapshot, item["name"])
            for snapshot in self.snapshots
            for item in snapshot.documents
        }

    def _query_number(self, query_binding: str) -> int:
        number = self._query_numbers.get(query_binding)
        if number is None:
            number = len(self._query_numbers) + 1
            self._query_numbers[query_binding] = number
        return number

    def _encode_cursor(self, query_binding: str, offset: int) -> str:
        return f"page:{self._query_number(query_binding)}:{int(offset)}"

    def _decode_cursor(
        self, cursor: str, query_binding: str, maximum: int
    ) -> int:
        try:
            prefix, query_text, offset_text = str(cursor).split(":", 2)
            query_number = int(query_text)
            offset = int(offset_text)
        except (TypeError, ValueError) as exc:
            raise CorpusError("invalid grep cursor") from exc
        expected_query_number = self._query_numbers.get(query_binding)
        if (
            prefix != "page"
            or expected_query_number is None
            or query_number != expected_query_number
            or offset < 0
            or offset > maximum
        ):
            raise CorpusError("grep cursor does not belong to this bounded query")
        return offset

    def grep(self, arguments: dict) -> dict:
        allowed_arguments = {"pattern", "files", "context", "max_matches", "cursor"}
        unknown = set(arguments) - allowed_arguments
        if unknown:
            raise CorpusError(f"unknown grep argument(s): {sorted(unknown)}")
        if self.calls_used >= self.limits.max_calls:
            raise CorpusError("grep call budget exhausted for this stage")
        if self.output_bytes_used >= self.limits.max_total_output_bytes:
            raise CorpusError("grep output budget exhausted for this stage")

        pattern = str(arguments.get("pattern", ""))
        if not pattern or len(pattern) > self.limits.max_pattern_chars:
            raise CorpusError(
                f"grep pattern must contain 1-{self.limits.max_pattern_chars} characters"
            )
        context = int(arguments.get("context", 0))
        if context < 0 or context > self.limits.max_context_lines:
            raise CorpusError(
                f"grep context must be in [0, {self.limits.max_context_lines}]"
            )
        page_size = int(arguments.get("max_matches", self.limits.max_matches))
        if page_size < 1 or page_size > self.limits.max_matches:
            raise CorpusError(
                f"max_matches must be in [1, {self.limits.max_matches}]"
            )

        documents = self._documents()
        requested_files = arguments.get("files")
        if requested_files is None:
            selected = sorted(documents)
        elif isinstance(requested_files, list):
            selected = sorted({_document_name(str(item)) for item in requested_files})
        else:
            raise CorpusError("grep files must be a list of manifest document names")
        if not selected or len(selected) > self.limits.max_files:
            raise CorpusError(
                f"grep must select 1-{self.limits.max_files} manifest documents"
            )
        missing = [name for name in selected if name not in documents]
        if missing:
            raise CorpusError(f"grep files are outside this corpus view: {missing}")

        query_descriptor = json.dumps(
            {
                "snapshots": [item.snapshot_id for item in self.snapshots],
                "pattern": pattern,
                "files": selected,
                "context": context,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        query_binding = _digest_bytes(query_descriptor)
        maximum_scan = self.limits.max_calls * self.limits.max_matches
        cursor = arguments.get("cursor")
        offset = (
            self._decode_cursor(str(cursor), query_binding, maximum_scan)
            if cursor else 0
        )

        command = [
            "rg",
            "--json",
            "--line-number",
            "--color=never",
            "--max-columns",
            str(self.limits.max_line_chars),
            "--max-columns-preview",
            "--max-count",
            str(maximum_scan + 1),
            "-e",
            pattern,
            "--",
        ]
        local_names = []
        roots: dict[str, Path] = {}
        for public_name in selected:
            snapshot, local_name = documents[public_name]
            # Run one fixed-root process per scope so no caller-controlled path is used.
            roots[public_name] = snapshot.root
            local_names.append((public_name, local_name, snapshot))

        line_cache: dict[str, list[str]] = {}
        verified_text: dict[str, str] = {}
        for public_name in selected:
            snapshot, local_name = documents[public_name]
            text = snapshot.document_path(local_name).read_text(
                encoding="utf-8", errors="replace"
            )
            verified_text[public_name] = text
            line_cache[public_name] = text.splitlines()

        raw_matches = []
        if shutil.which("rg") is None:
            _validate_python_fallback_pattern(pattern)
            try:
                expression = re.compile(pattern)
            except re.error as exc:
                raise CorpusError(f"bounded grep rejected pattern: {exc}") from exc
            deadline = time.monotonic() + self.limits.timeout_s
            for public_name in selected:
                for line_number, line in enumerate(line_cache[public_name], 1):
                    if time.monotonic() > deadline:
                        raise CorpusError("bounded grep failed: Python fallback timed out")
                    # The fallback never evaluates unbounded line content. The
                    # same limit also bounds excerpts returned by the rg path.
                    if expression.search(line[: self.limits.max_line_chars]):
                        raw_matches.append((public_name, line_number))
                        if len(raw_matches) > maximum_scan:
                            break
                if len(raw_matches) > maximum_scan:
                    break
            for public_name in selected:
                snapshot, local_name = documents[public_name]
                current = snapshot.document_path(local_name).read_text(
                    encoding="utf-8", errors="replace"
                )
                if current != verified_text[public_name]:
                    raise CorpusError(
                        f"corpus document changed during grep: {public_name}"
                    )
        else:
            for snapshot in self.snapshots:
                scoped = [
                    (public_name, local_name)
                    for public_name, local_name, owner in local_names
                    if owner is snapshot
                ]
                if not scoped:
                    continue
                try:
                    completed = subprocess.run(
                        command + [local for _public, local in scoped],
                        cwd=snapshot.root,
                        capture_output=True,
                        text=True,
                        timeout=self.limits.timeout_s,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise CorpusError(f"bounded grep failed: {exc}") from exc
                if completed.returncode not in {0, 1}:
                    detail = (completed.stderr or "rg failed").strip()[:300]
                    raise CorpusError(f"bounded grep rejected pattern: {detail}")
                for public_name, local_name in scoped:
                    current = snapshot.document_path(local_name).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if current != verified_text[public_name]:
                        raise CorpusError(
                            f"corpus document changed during grep: {public_name}"
                        )
                public_by_local = {local: public for public, local in scoped}
                for line in completed.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "match":
                        continue
                    data = event.get("data") or {}
                    local = str((data.get("path") or {}).get("text", ""))
                    public = public_by_local.get(local)
                    line_number = data.get("line_number")
                    if public is None or not isinstance(line_number, int):
                        continue
                    raw_matches.append((public, line_number))
        raw_matches.sort(key=lambda item: (item[0], item[1]))

        window = raw_matches[offset : offset + page_size + 1]
        has_more = len(window) > page_size
        window = window[:page_size]
        results = []
        output_limit = min(
            self.limits.max_output_bytes,
            self.limits.max_total_output_bytes - self.output_bytes_used,
        )
        used = 0
        for public_name, line_number in window:
            lines = line_cache[public_name]
            start = max(1, line_number - context)
            end = min(len(lines), line_number + context)
            excerpt = [
                {
                    "line": number,
                    "text": lines[number - 1][: self.limits.max_line_chars],
                    "match": number == line_number,
                }
                for number in range(start, end + 1)
            ]
            scope, local_name = public_name.split("/", 1)
            record = {
                "file": public_name,
                "line": line_number,
                "evidence_ref": f"{scope}:{local_name}:{line_number}",
                "excerpt": [
                    {
                        **item,
                        "evidence_ref": (
                            f"{scope}:{local_name}:{int(item['line'])}"
                        ),
                    }
                    for item in excerpt
                ],
            }
            encoded_size = len(json.dumps(record, separators=(",", ":")).encode("utf-8"))
            if used + encoded_size > output_limit:
                has_more = True
                break
            results.append(record)
            used += encoded_size

        next_offset = offset + len(results)
        response = {
            "scope": [item.scope for item in self.snapshots],
            "pattern": pattern,
            "matches": results,
            "returned": len(results),
            "truncated": bool(has_more),
            "next_cursor": (
                self._encode_cursor(query_binding, next_offset)
                if has_more and results else None
            ),
            "budget": {
                "calls_remaining": self.limits.max_calls - self.calls_used - 1,
                "output_bytes_remaining": max(
                    0,
                    self.limits.max_total_output_bytes
                    - self.output_bytes_used
                    - used,
                ),
            },
        }
        response_bytes = len(json.dumps(response, separators=(",", ":")).encode("utf-8"))
        while results and response_bytes > output_limit:
            results.pop()
            response["matches"] = results
            response["returned"] = len(results)
            response["truncated"] = True
            next_offset = offset + len(results)
            response["next_cursor"] = (
                self._encode_cursor(query_binding, next_offset) if results else None
            )
            response_bytes = len(
                json.dumps(response, separators=(",", ":")).encode("utf-8")
            )
        if response_bytes > output_limit:
            raise CorpusError("grep output limit is too small for response metadata")
        response["budget"]["output_bytes_remaining"] = max(
            0,
            self.limits.max_total_output_bytes
            - self.output_bytes_used
            - response_bytes,
        )
        response_bytes = len(
            json.dumps(response, separators=(",", ":")).encode("utf-8")
        )
        if response_bytes > output_limit:
            raise CorpusError("grep response exceeded its per-call output budget")
        self.calls_used += 1
        self.output_bytes_used += response_bytes
        self.transcript.append({
            "call": self.calls_used,
            "arguments": {
                "pattern": pattern,
                "files": selected,
                "context": context,
                "max_matches": page_size,
                "cursor": cursor,
            },
            "result": response,
        })
        return response


GREP_TOOL = {
    "type": "function",
    "name": "grep",
    "description": (
        "Search only the immutable documents listed in this stage's corpus "
        "manifest. Cite the short returned evidence_ref values exactly; the "
        "framework binds them to immutable source content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "context": {"type": "integer", "minimum": 0},
            "max_matches": {"type": "integer", "minimum": 1},
            "cursor": {
                "type": "string",
                "description": (
                    "Continue the immediately preceding query only. A cursor is "
                    "bound to its exact pattern, files, and context; reuse it "
                    "with identical arguments or omit it."
                ),
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    "strict": False,
}
