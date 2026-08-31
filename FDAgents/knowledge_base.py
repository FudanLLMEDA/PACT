"""Case-based knowledge base.

The playbook that used to be a 380-line ``if``-ladder over hard-coded
resource fingerprints (recipe_planner.py) is now *data*: YAML cases with

  - a soft feature ``signature`` (numeric ranges / booleans / string sets),
  - a suggested plan (skill order + initial params),
  - measured ``evidence`` from real benchmark runs,
  - free-text lessons.

Matching is advisory: cases are scored by signature overlap and the best
ones are shown to the LLM alongside their evidence. The LLM decides; the
match score only ranks what it gets to see. Absorbing thresholds into cases
this way keeps the tuned behavior available without hard-wiring it.
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .config import Config, get_config
from .features import DesignFeatures
logger = logging.getLogger(__name__)

_RAW_TCL_KNOWLEDGE_KEYS = {"command", "tcl", "script", "tcl_ref"}
_PUBLIC_TAXONOMY_PATH = Path(__file__).resolve().parent / "knowledge" / "operator_taxonomy.yaml"
_FORBIDDEN_TAXONOMY_KEYS = {
    "benchmark",
    "case",
    "cases",
    "cell",
    "cells",
    "commit",
    "coordinate",
    "coordinates",
    "dcp",
    "design",
    "digest",
    "fingerprint",
    "hash",
    "net",
    "nets",
    "params",
    "preload",
    "provenance",
    "recipe",
    "sha",
    "site",
    "sites",
    "skill",
    "source",
}


def _validate_public_taxonomy(node: object) -> dict:
    if not isinstance(node, dict):
        raise ValueError("operator taxonomy must be a mapping")
    if not {"schema_version", "authority", "levels"}.issubset(node):
        raise ValueError("operator taxonomy is missing its public contract")
    if node.get("schema_version") != 1:
        raise ValueError("operator taxonomy schema version is unsupported")
    if node.get("authority") != "generic_operator_taxonomy_only":
        raise ValueError("operator taxonomy authority is unsupported")

    def validate_keys(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                parts = {part for part in normalized.replace("-", "_").split("_") if part}
                if parts & _FORBIDDEN_TAXONOMY_KEYS:
                    raise ValueError(
                        f"operator taxonomy contains forbidden public field {key!r}"
                    )
                validate_keys(item)
        elif isinstance(value, list):
            for item in value:
                validate_keys(item)

    validate_keys(node)
    levels = node.get("levels")
    if not isinstance(levels, list) or [item.get("level") for item in levels] != [
        "L0",
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
    ]:
        raise ValueError("operator taxonomy must define the ordered L0-L5 levels")
    directions = node.get("registered_directions")
    expected_directions = (
        {
            "direction_id": "constructive_accumulator_rebuild_attempt",
            "taxonomy_level": "L3",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "constructive_accumulator_eligible_family_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "constructive_accumulator_rebuild_v1",
            "authority": "nomination_gated_execution",
        },
        {
            "direction_id": "memory_write_buffer_forward_attempt",
            "taxonomy_level": "L4",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "memory_write_buffer_forward_eligible_family_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "memory_write_buffer_forward_v1",
            "authority": "nomination_gated_execution",
        },
        {
            "direction_id": "registered_arithmetic_cone_attempt",
            "taxonomy_level": "L4",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "registered_arithmetic_eligible_family_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "registered_arithmetic_cone_v1",
            "authority": "nomination_gated_execution",
        },
        {
            "direction_id": "recurrence_boundary_cut_attempt",
            "taxonomy_level": "L4",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "recurrence_boundary_hypothesis_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "dsp_srl_recurrence_cut_v1",
            "authority": "attempt_direction_only",
        },
        {
            "direction_id": "fixed_point_product_rebuild_attempt",
            "taxonomy_level": "L2",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "fixed_point_product_hypothesis_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "fixed_point_dsp_rebuild_v1",
            "authority": "attempt_direction_only",
        },
        {
            "direction_id": "two_operand_product_rebuild_attempt",
            "taxonomy_level": "L2",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "two_operand_product_hypothesis_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "dsp_mul_two_operand_v1",
            "authority": "attempt_direction_only",
        },
        {
            "direction_id": "register_absorb_attempt",
            "taxonomy_level": "L0",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "register_absorb_hypothesis_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "dsp_register_absorb_v1",
            "authority": "attempt_direction_only",
        },
        {
            "direction_id": "pipeline_rebalance_attempt",
            "taxonomy_level": "L0",
            "evidence_document": "operator_action_preflight.json",
            "evidence_field": "pipeline_rebalance_hypothesis_count",
            "required_minimum": 1,
            "mechanic": "operator_rewrite",
            "target": "dsp_pipeline_rebalance_v1",
            "authority": "attempt_direction_only",
        },
        {
            "direction_id": "recurrence_lineage_worst_source_attempt",
            "taxonomy_level": "L0",
            "evidence_document": "semantic_action_preflight.json",
            "evidence_field": "worst_source_eligible_count",
            "required_minimum": 1,
            "mechanic": "critical_net_reroute",
            "target": "worst_source",
            "authority": "attempt_direction_only",
        },
    )
    if not isinstance(directions, list) or len(directions) != len(
        expected_directions
    ):
        raise ValueError("operator taxonomy registered directions are incomplete")
    for direction, expected_direction in zip(directions, expected_directions):
        if (
            not isinstance(direction, dict)
            or {key: direction.get(key) for key in expected_direction}
            != expected_direction
            or set(direction) != {*expected_direction, "contract"}
            or not isinstance(direction.get("contract"), list)
            or len(direction["contract"]) != 5
            or any(
                not isinstance(item, str) or not item.strip()
                for item in direction["contract"]
            )
        ):
            raise ValueError("operator taxonomy registered direction is invalid")
    return node


def _load_public_taxonomy_document() -> dict[str, str]:
    if not _PUBLIC_TAXONOMY_PATH.is_file():
        raise FileNotFoundError(
            f"packaged operator taxonomy is missing: {_PUBLIC_TAXONOMY_PATH}"
        )
    text = _PUBLIC_TAXONOMY_PATH.read_text(encoding="utf-8")
    taxonomy = yaml.safe_load(text) or {}
    _validate_public_taxonomy(taxonomy)
    _reject_raw_tcl_knowledge(taxonomy, path=str(_PUBLIC_TAXONOMY_PATH))
    return {"operator_taxonomy.yaml": text}


def _reject_raw_tcl_knowledge(node, *, path: str = "cases") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_text = str(key)
            if key_text.lower() in _RAW_TCL_KNOWLEDGE_KEYS:
                raise ValueError(
                    f"raw Tcl knowledge field {key_text!r} is forbidden at {path}"
                )
            _reject_raw_tcl_knowledge(value, path=f"{path}.{key_text}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_raw_tcl_knowledge(value, path=f"{path}[{index}]")


@dataclass
class Case:
    id: str
    description: str = ""
    signature: dict = field(default_factory=dict)
    confidence: float = 0.5
    bottleneck: str = "mixed"
    suggested_skill_order: list = field(default_factory=list)
    initial_params: dict = field(default_factory=dict)
    core_skills: list = field(default_factory=list)
    budget_hint_s: int = 3600
    evidence: list = field(default_factory=list)
    notes: str = ""

    def to_prompt_dict(self, score: float) -> dict:
        d = {
            "case_id": self.id,
            "match_score": round(score, 3),
            "confidence": self.confidence,
            "description": self.description.strip(),
            "bottleneck": self.bottleneck,
            "suggested_skill_order": list(self.suggested_skill_order),
            "core_skills": list(self.core_skills),
        }
        if self.initial_params:
            d["initial_params"] = self.initial_params
        if self.evidence:
            d["evidence"] = self.evidence
        if self.notes.strip():
            d["notes"] = self.notes.strip()
        return d


@dataclass
class CaseMatch:
    case: Case
    score: float


def _range_score(value, spec: dict) -> Optional[float]:
    """1.0 inside [min,max]; decays linearly to 0 within 25% of the span
    (or of |bound| when one-sided) outside; None when value is missing."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    lo = spec.get("min")
    hi = spec.get("max")
    lo = float(lo) if lo is not None else None
    hi = float(hi) if hi is not None else None

    if lo is not None and hi is not None:
        span = max(hi - lo, 1e-9)
        slack = 0.25 * span
        if lo <= value <= hi:
            return 1.0
        dist = (lo - value) if value < lo else (value - hi)
        return max(0.0, 1.0 - dist / max(slack, 1e-9))
    bound = lo if lo is not None else hi
    if bound is None:
        return None
    slack = 0.25 * max(abs(bound), 1.0)
    if lo is not None:
        if value >= lo:
            return 1.0
        return max(0.0, 1.0 - (lo - value) / slack)
    if value <= hi:
        return 1.0
    return max(0.0, 1.0 - (value - hi) / slack)


def score_signature(signature: dict, features: DesignFeatures) -> float:
    """Mean per-key score over the keys the signature specifies."""
    if not signature:
        return 0.0
    fdict = features.to_dict()
    scores: list[float] = []
    for key, spec in signature.items():
        if key == "bottleneck_any":
            wanted = [str(x).lower() for x in (spec or [])]
            got = str(fdict.get("dominant_bottleneck") or "").lower()
            scores.append(1.0 if any(w in got for w in wanted) else 0.0)
            continue
        value = fdict.get(key)
        if isinstance(spec, dict):
            s = _range_score(value, spec)
            if s is not None:
                scores.append(s)
        elif isinstance(spec, bool):
            scores.append(1.0 if bool(value) == spec else 0.0)
        elif isinstance(spec, str):
            scores.append(1.0 if str(value).lower() == spec.lower() else 0.0)
        elif isinstance(spec, (int, float)):
            s = _range_score(value, {"min": spec, "max": spec})
            if s is not None:
                scores.append(s)
    if not scores:
        return 0.0
    base = sum(scores) / len(scores)
    # Specificity weight: a signature that matched on very few keys carries
    # little information — damp it so rich matches outrank trivial ones.
    specificity = min(1.0, len(scores) / 4.0)
    return base * (0.5 + 0.5 * specificity)


class KnowledgeBase:
    def __init__(
        self,
        cases: list[Case],
        skills_guide: dict,
        corpus_documents: Optional[dict[str, str]] = None,
    ):
        self.cases = cases
        self.skills_guide = skills_guide
        self._corpus_documents = dict(corpus_documents or {})
        self._llm_corpus_documents = _load_public_taxonomy_document()

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, cfg: Optional[Config] = None) -> "KnowledgeBase":
        cfg = cfg or get_config()
        paths: list[Path] = [cfg.resolve_path(cfg.require("knowledge.cases_path"))]
        extra = os.environ.get("FDAGENTS_KNOWLEDGE_PATHS", "")
        for raw in extra.split(":"):
            raw = raw.strip()
            if raw:
                paths.append(Path(raw).expanduser())

        cases: list[Case] = []
        seen_ids: set[str] = set()
        corpus_documents: dict[str, str] = {}
        for index, path in enumerate(paths):
            if not path.exists():
                logger.warning("[knowledge] case file missing: %s", path)
                continue
            raw_text = path.read_text(encoding="utf-8")
            corpus_documents[f"cases/{index:02d}_{path.name}"] = raw_text
            raw_cases = yaml.safe_load(raw_text) or []
            if isinstance(raw_cases, dict):
                raw_cases = raw_cases.get("cases", [])
            _reject_raw_tcl_knowledge(raw_cases, path=str(path))
            for raw in raw_cases:
                try:
                    raw = dict(raw)
                    case = Case(**raw)
                except (TypeError, ValueError) as e:
                    raise ValueError(f"bad case entry in {path}: {e}") from e
                if case.id in seen_ids:
                    # Later files override earlier ones (user > packaged).
                    cases = [c for c in cases if c.id != case.id]
                seen_ids.add(case.id)
                cases.append(case)

        guide_path = cfg.resolve_path(cfg.require("knowledge.skills_guide_path"))
        skills_guide: dict = {}
        if guide_path.exists():
            guide_text = guide_path.read_text(encoding="utf-8")
            corpus_documents[f"guides/{guide_path.name}"] = guide_text
            skills_guide = yaml.safe_load(guide_text) or {}
            _reject_raw_tcl_knowledge(skills_guide, path=str(guide_path))
        else:
            logger.warning("[knowledge] skills guide missing: %s", guide_path)

        # Search only the effective case set. Later configured files override
        # duplicate IDs for both matching and retrieval, never just one path.
        corpus_documents = {
            name: text
            for name, text in corpus_documents.items()
            if not name.startswith("cases/")
        }
        corpus_documents["cases/effective_cases.yaml"] = yaml.safe_dump(
            {"cases": [asdict(case) for case in cases]},
            sort_keys=False,
        )

        logger.info("[knowledge] loaded %d cases from %d file(s)", len(cases), len(paths))
        return cls(cases, skills_guide, corpus_documents)

    def corpus_documents(self) -> dict[str, str]:
        """Return legacy/internal sources; this is not an LLM-safe boundary."""
        return dict(self._corpus_documents)

    def llm_corpus_documents(self) -> dict[str, str]:
        """Return only the generic taxonomy approved for the public LLM corpus."""
        return dict(self._llm_corpus_documents)

    # ------------------------------------------------------------------
    def match(
        self,
        features: DesignFeatures,
        min_score: Optional[float] = None,
        limit: Optional[int] = None,
        cfg: Optional[Config] = None,
    ) -> list[CaseMatch]:
        cfg = cfg or get_config()
        if min_score is None:
            min_score = float(cfg.get("knowledge.min_match_score", 0.35))
        if limit is None:
            limit = int(cfg.get("knowledge.max_cases_in_prompt", 5))
        matches = [
            CaseMatch(case, score_signature(case.signature, features))
            for case in self.cases
        ]
        matches = [m for m in matches if m.score >= min_score]
        matches.sort(key=lambda m: (m.score, m.case.confidence), reverse=True)
        return matches[:limit]

    def render_matches(self, matches: list[CaseMatch]) -> list[dict]:
        return [m.case.to_prompt_dict(m.score) for m in matches]

    def target_presets(self, matches: list[CaseMatch]) -> dict[str, dict[str, dict]]:
        """Extract executable semantic-target presets from current case matches."""
        presets: dict[str, dict[str, dict]] = {}

        def add_skill_targets(skill_name: str, raw: object) -> None:
            if not isinstance(raw, dict):
                return
            base = {
                key: copy.deepcopy(value)
                for key, value in raw.items()
                if key not in {
                    "target_candidates",
                    "target_params",
                    "replace_target_candidates",
                }
            }
            target_params = raw.get("target_params") or {}
            if not isinstance(target_params, dict):
                target_params = {}
            candidates = raw.get("target_candidates") or []
            if isinstance(candidates, str):
                candidates = [candidates]
            names = list(candidates) + [
                name for name in target_params if name not in candidates
            ]
            for name in names:
                name = str(name)
                params = copy.deepcopy(base)
                specific = target_params.get(name) or {}
                if isinstance(specific, dict):
                    params.update(
                        copy.deepcopy(
                            {
                                key: value
                                for key, value in specific.items()
                                if key not in {
                                    "target_candidates",
                                    "target_params",
                                    "replace_target_candidates",
                                }
                            }
                        )
                    )
                skill_presets = presets.setdefault(skill_name, {})
                skill_presets.setdefault(name, params)

        for match in matches:
            case = match.case if isinstance(match, CaseMatch) else match
            initial = getattr(case, "initial_params", {})
            if not isinstance(initial, dict):
                continue
            for skill_name, raw in initial.items():
                if skill_name == "clock_tighten_variants":
                    if isinstance(raw, dict):
                        for variant in raw.values():
                            add_skill_targets("clock_tighten", variant)
                    continue
                add_skill_targets(str(skill_name), raw)
        return presets

    def guide_for_skill(self, skill_name: str) -> dict:
        guide = self.skills_guide.get(skill_name)
        return guide if isinstance(guide, dict) else {}
