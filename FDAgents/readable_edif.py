"""Collision-safe readable-EDIF sidecars for routed checkpoint attestations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def readable_edif_sidecar_paths(dcp_path: Path | str) -> dict[str, Path]:
    """Return RapidWright's sidecar paths beside one transaction-owned DCP."""
    dcp = Path(dcp_path).expanduser().resolve()
    sidecar_dir = dcp.parent / f"{dcp.name}.edf"
    return {
        "dcp": dcp,
        "sidecar_dir": sidecar_dir,
        "edif": sidecar_dir / f"{dcp.stem}.edf",
        "md5": sidecar_dir / f"{dcp.name}.md5",
    }


def _braced(path: Path) -> str:
    value = str(path)
    if any(token in value for token in ("}", "\n", "\r")):
        raise ValueError("readable-EDIF path contains unsafe Tcl characters")
    return "{" + value + "}"


def render_readable_edif_sidecar_tcl(
    dcp_path: Path | str,
    *,
    open_checkpoint: bool,
    close_design: bool,
) -> list[str]:
    """Render isolated, force-overwriting EDIF generation commands.

    The sidecar directory is deleted and recreated within the caller's unique
    transaction/invocation directory.  Thus stale or partially generated R4
    outputs cannot collide, while no output outside that isolation scope is
    touched.
    """
    paths = readable_edif_sidecar_paths(dcp_path)
    commands = []
    if open_checkpoint:
        commands.append(f"open_checkpoint {_braced(paths['dcp'])}")
    commands.extend([
        f"file delete -force {_braced(paths['sidecar_dir'])}",
        f"file mkdir {_braced(paths['sidecar_dir'])}",
        f"write_edif -force {_braced(paths['edif'])}",
        "puts REGISTERED_PRODUCT_READABLE_EDIF_WRITTEN",
    ])
    if close_design:
        commands.append("close_design")
    return commands


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_readable_edif_sidecar(dcp_path: Path | str) -> dict[str, Any]:
    """Bind one generated EDIF to exact DCP bytes and return an audit row."""
    paths = readable_edif_sidecar_paths(dcp_path)
    dcp = paths["dcp"]
    edif = paths["edif"]
    if (
        dcp.is_symlink()
        or not dcp.is_file()
        or edif.is_symlink()
        or not edif.is_file()
        or edif.stat().st_size <= 0
    ):
        raise RuntimeError("registered product readable-EDIF sidecar is incomplete")
    dcp_md5 = _md5_file(dcp)
    paths["md5"].write_text(dcp_md5 + "\n", encoding="ascii")
    return {
        "schema_version": "registered-product-readable-edif-v1",
        "status": "ready",
        "isolation": "transaction_or_invocation_unique_directory",
        "dcp_md5": dcp_md5,
        "edif_sha256": hashlib.sha256(edif.read_bytes()).hexdigest(),
        "edif_size_bytes": edif.stat().st_size,
    }


__all__ = [
    "finalize_readable_edif_sidecar",
    "readable_edif_sidecar_paths",
    "render_readable_edif_sidecar_tcl",
]
