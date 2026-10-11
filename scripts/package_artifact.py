#!/usr/bin/env python3
"""Package tracked v1 source, recursive submodules and materialized LFS inputs.

No .git history, untracked files, build outputs or local credentials are copied.
Requires a clean source tree; emits a ZIP, internal file manifest and SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def tracked_files(root: Path, prefix: Path = Path()):
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, revision, stage = metadata.decode().split()
        name = Path(raw_name.decode())
        if stage != "0":
            raise ValueError(f"Unmerged file: {prefix / name}")
        if mode == "160000":
            child = root / name
            actual = git(child, "rev-parse", "HEAD").decode().strip()
            if actual != revision:
                raise ValueError(f"Submodule revision mismatch: {prefix / name}")
            yield from tracked_files(child, prefix / name)
        else:
            yield root / name, prefix / name, int(mode, 8)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if git(ROOT, "status", "--porcelain", "--untracked-files=normal"):
        raise SystemExit("Commit the intended changes and use a clean source worktree before packaging")
    if not args.version.startswith("v1.") or any(c not in "v0123456789." for c in args.version):
        raise SystemExit("Expected a v1.x.y release version")
    revision = git(ROOT, "rev-parse", "HEAD").decode().strip()
    timestamp = int(git(ROOT, "show", "-s", "--format=%ct", "HEAD"))
    date = time.gmtime(timestamp)[:6]
    name = f"PACT-FPT2026-{args.version}"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f"{name}.zip"
    if archive.exists():
        raise SystemExit(f"Refusing to overwrite {archive}")
    manifest = {"version": args.version, "commit": revision,
                "contains": "tracked source, recursive pinned submodule source, 35 original benchmark DCPs",
                "excludes": "Vivado, licenses, prebuilt Java/Python environments, .git, credentials, run outputs",
                "submodules": git(ROOT, "submodule", "status", "--recursive").decode().splitlines(), "files": []}
    benchmark_count = 0
    with zipfile.ZipFile(archive, "w", allowZip64=True) as out:
        for source, relative, mode in sorted(tracked_files(ROOT), key=lambda row: str(row[1])):
            if relative.name in {".env", "config.toml", ".git"}:
                raise ValueError(f"Unexpected private file in tracked source: {relative}")
            content = source.readlink().as_posix().encode() if stat.S_ISLNK(mode) else None
            if content is not None:
                first = content[:128]
            else:
                with source.open("rb") as stream:
                    first = stream.read(128)
            if first.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise ValueError(f"LFS pointer not materialized: {relative}")
            info = zipfile.ZipInfo(f"{name}/{relative.as_posix()}", date_time=date)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_STORED if relative.suffix.lower() in {".dcp", ".zip", ".jar", ".png", ".pdf"} else zipfile.ZIP_DEFLATED
            h = hashlib.sha256()
            size = 0
            with out.open(info, "w", force_zip64=True) as target:
                if content is not None:
                    target.write(content)
                    h.update(content)
                    size = len(content)
                else:
                    with source.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            target.write(chunk)
                            h.update(chunk)
                            size += len(chunk)
            manifest["files"].append({"path": relative.as_posix(), "sha256": h.hexdigest(), "size": size})
            if relative.parts[0] == "benchmarks" and relative.suffix == ".dcp":
                benchmark_count += 1
        if benchmark_count != 35:
            raise ValueError(f"Expected 35 benchmark checkpoints, found {benchmark_count}")
        info = zipfile.ZipInfo(f"{name}/ARTIFACT_MANIFEST.json", date_time=date)
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        out.writestr(info, json.dumps(manifest, indent=2) + "\n")
    if archive.stat().st_size >= 2 * 1024**3:
        raise ValueError("ZIP exceeds GitHub's 2 GiB per-asset limit")
    with zipfile.ZipFile(archive) as check:
        if check.testzip() is not None:
            raise ValueError("ZIP CRC verification failed")
    checksum = digest(archive)
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size,
                      "sha256": checksum, "files": len(manifest["files"]),
                      "benchmarks": benchmark_count, "commit": revision}, indent=2))


if __name__ == "__main__":
    main()
