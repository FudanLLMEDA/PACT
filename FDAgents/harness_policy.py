from __future__ import annotations

from FDAgents.skills import DEEP_ONLY_SKILLS, SKILLS


def build_decision_policy_prompt() -> str:
    skills = "\n".join(f"- {name}" for name in sorted(SKILLS))
    deep_only = ", ".join(sorted(DEEP_ONLY_SKILLS)) or "none"
    return f"""# FDAgents Decision Policy

You are selecting the next optimization action for an FPGA DCP run.

Return one JSON object with exactly these top-level fields:
{{"skill": "<registered skill>", "target": "<candidate>", "params": {{}}, "reason": "<short>"}}

Registered skills:
{skills}

Decision policy:
- If budget exhausted, return no mutating action in the harness result.
- If recipe core skills remain available, choose one of the recipe core skills before any non-core skill.
- Obey remaining candidates. Do not invent net, cell, path, hierarchy, design, DCP, or coordinate names.
- Obey blacklist entries. A hard blacklist blocks the candidate; a soft blacklist blocks only the same skill, target, and params.
- Apply same-skill cooldown: if the recent same skill repeatedly failed with negligible delta, move to a different available skill.
- Apply fallback when the preferred action is unavailable: scan recipe core skills first, then suggested skill order, then registered safe defaults.
- Use deep-mode custom only when deep mode is active and custom calls remain; deep-only skills are: {deep_only}.
- Prefer bounded actions late in the time budget: phys_opt and post_route_cleanup before broad pblock or full reimplementation.
- After a route-local or structural improvement, consider post_route_cleanup before giving up.
"""
