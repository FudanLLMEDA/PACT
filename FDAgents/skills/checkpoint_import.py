"""
checkpoint_import skill -- import a known-good checkpoint into the run.

This is intentionally conservative: it opens the checkpoint in Vivado,
checks route legality/timing, then copies the source DCP into the run
directory. The main agent acceptance logic still decides whether the imported
checkpoint improves over current_best.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from .base import (
    SkillResult,
    calculate_fmax,
    parse_route_status_static,
    parse_timing_summary_static,
)

logger = logging.getLogger(__name__)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "checkpoint"


class CheckpointImportSkill:
    """Open and adopt a local known-good DCP."""

    name = "checkpoint_import"

    async def execute(
        self,
        mcp,
        target: str,
        params: dict,
        before_wns: float,
        clock_period: Optional[float],
    ) -> SkillResult:
        source = Path(target).expanduser()
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()
        else:
            source = source.resolve()

        output_dcp = mcp.run_dir / f"checkpoint_import_{_safe_name(source.stem)}.dcp"
        logger.info("[checkpoint_import] source=%s", source)

        if not source.exists():
            return SkillResult.failure(before_wns, f"checkpoint not found: {source}", output_dcp)

        try:
            await mcp.call_vivado(
                "open_checkpoint",
                {"dcp_path": str(source)},
                timeout=600.0,
            )

            rs = await mcp.call_vivado("report_route_status", {}, timeout=120.0)
            route = parse_route_status_static(rs)
            is_legal = bool(route.get("routed_ok", True))

            after_wns = await mcp.get_wns()
            if after_wns is None:
                ts = await mcp.call_vivado("report_timing_summary", {}, timeout=300.0)
                parsed = parse_timing_summary_static(ts)
                after_wns = parsed.get("wns") or before_wns

            shutil.copy(source, output_dcp)

            delta = after_wns - before_wns
            fmax_b = calculate_fmax(before_wns, clock_period)
            fmax_a = calculate_fmax(after_wns, clock_period)
            if fmax_b and fmax_a:
                summary = (
                    f"checkpoint_import:{source.name} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} "
                    f"fmax {fmax_b:.1f}->{fmax_a:.1f} MHz delta={delta:+.3f}"
                )
            else:
                summary = (
                    f"checkpoint_import:{source.name} "
                    f"wns {before_wns:.3f}->{after_wns:.3f} delta={delta:+.3f}"
                )

            return SkillResult(
                success=True,
                before_wns=before_wns,
                after_wns=after_wns,
                delta_wns=delta,
                is_legal=is_legal,
                output_dcp=output_dcp,
                summary=summary,
            )

        except Exception as e:
            logger.error("[checkpoint_import] failed: %s", e)
            return SkillResult.failure(before_wns, str(e), output_dcp)
