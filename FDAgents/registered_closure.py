"""Recover complete registered fixed-point actions from current-DCP word DAGs.

The recovery is deliberately independent of benchmark, hierarchy and cell
names.  Object identities are used only as edges inside the loaded checkpoint;
the emitted arithmetic plan is derived by composing exact Boolean word-cone
evaluators and then replaying the recovered formula over deterministic vectors.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import dataclass
from itertools import product
from pathlib import Path
import random
import re
from typing import Any, Callable, Mapping

from .compressor_proof import ConeGraph, _evaluate_target_boolean_batch
from .fixed_point_dsp import (
    MAX_DSP_COEFFICIENT_WIDTH,
    _group_value,
    _infer_bus_term,
    _infer_full_bus_term,
    _interaction_groups,
    _product_value,
    _sample_words,
    _term_value,
)


_INDEXED = re.compile(
    r"^(?P<stem>.+)\[(?P<index>\d+)\](?P<replica>_replica(?:_\d+)?)?$"
)


def control_aliases_in_closure_namespace(
    alias_facts: Mapping[str, object],
    observed_sink_pins: Mapping[str, str],
) -> dict[str, str]:
    """Translate exact-source RapidWright aliases through shared sink pins.

    Vivado closure TSVs and RapidWright can spell the same routed net
    differently.  The sink pins supplied to the proof are the cross-tool
    identity boundary; both the member and representative must resolve back
    to the complete closure pin set before any alias is installed.
    """
    pin_to_observed: dict[str, str] = {}
    for observed_net, pin_name in observed_sink_pins.items():
        if (
            not isinstance(observed_net, str)
            or not observed_net
            or not isinstance(pin_name, str)
            or not pin_name
            or pin_name in pin_to_observed
        ):
            raise RuntimeError("registered control sink-pin namespace is invalid")
        pin_to_observed[pin_name] = observed_net

    groups = alias_facts.get("groups")
    flat_aliases = alias_facts.get("net_aliases")
    if not isinstance(groups, list) or not isinstance(flat_aliases, dict):
        raise RuntimeError("registered control equivalence proof has no alias groups")

    translated: dict[str, str] = {}
    covered_pins: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise RuntimeError("registered control equivalence group is invalid")
        representative = group.get("representative_net")
        members = group.get("members")
        if not isinstance(representative, str) or not isinstance(members, list):
            raise RuntimeError("registered control equivalence group is incomplete")

        representative_pin = next((
            member.get("pin_name")
            for member in members
            if isinstance(member, dict)
            and member.get("canonical_net") == representative
            and isinstance(member.get("pin_name"), str)
        ), None)
        representative_observed = pin_to_observed.get(representative_pin or "")
        if representative_observed is None:
            raise RuntimeError(
                "registered control representative is outside the closure namespace"
            )

        for member in members:
            if not isinstance(member, dict):
                raise RuntimeError("registered control equivalence member is invalid")
            pin_name = member.get("pin_name")
            canonical_net = member.get("canonical_net")
            observed_net = pin_to_observed.get(
                pin_name if isinstance(pin_name, str) else ""
            )
            if (
                observed_net is None
                or not isinstance(canonical_net, str)
                or flat_aliases.get(canonical_net) != representative
            ):
                raise RuntimeError(
                    "registered control equivalence proof does not cover the closure"
                )
            translated[observed_net] = representative_observed
            covered_pins.add(pin_name)

    if covered_pins != set(pin_to_observed):
        raise RuntimeError(
            "registered control equivalence proof has incomplete sink-pin coverage"
        )
    return translated


@dataclass(frozen=True)
class WordBus:
    stem: str
    width: int
    source_lsb: int
    nets: tuple[str, ...]
    aliases: tuple[tuple[str, ...], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.stem}@{self.source_lsb}:{self.width}"


@dataclass
class RecoveredWordNode:
    """One exact combinational D function between registered word cuts."""

    stem: str
    width: int
    target_lsb: int
    buses: tuple[WordBus, ...]
    controls: tuple[str, ...]
    evaluator: Callable[[Mapping[str, int], Mapping[str, bool]], int]
    active_controls: dict[str, bool]
    control_groups: tuple[tuple[str, ...], ...] = ()
    batch_evaluator: Callable[
        [list[Mapping[str, int]], Mapping[str, bool]], list[int]
    ] | None = None

    def evaluate(self, inputs: Mapping[str, int]) -> int:
        return int(self.evaluator(inputs, self.active_controls)) & (
            (1 << self.width) - 1
        )

    def evaluate_batch(self, inputs: list[Mapping[str, int]]) -> list[int]:
        """Replay several word assignments in one bit-parallel cone pass.

        Synthetic/unit-test nodes may provide only the scalar evaluator; the
        fallback preserves their existing contract.  Nodes recovered from a
        real DCP always install ``batch_evaluator`` below.
        """
        if self.batch_evaluator is None:
            return [self.evaluate(item) for item in inputs]
        mask = (1 << self.width) - 1
        return [
            int(value) & mask
            for value in self.batch_evaluator(inputs, self.active_controls)
        ]


@dataclass(frozen=True)
class WordSliceRef:
    """One current-DCP registered word slice used as a temporal leaf."""

    stem: str
    source_lsb: int
    width: int
    nets: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.stem}@{self.source_lsb}:{self.width}"


@dataclass(frozen=True)
class SemanticWordRef:
    """One logical operand reconstructed from consistent registered slices."""

    stem: str
    source_lsb: int
    width: int
    nets: tuple[str, ...]
    slices: tuple[WordSliceRef, ...]

    @property
    def key(self) -> str:
        return f"{self.stem}@{self.source_lsb}:{self.width}"

    def leaf_values(self, value: int) -> dict[str, int]:
        """Project one logical word value onto every extracted slice."""
        result = {}
        for item in self.slices:
            offset = item.source_lsb - self.source_lsb
            result[item.key] = (int(value) >> offset) & ((1 << item.width) - 1)
        return result


def _merge_semantic_word_slices(
    leaves: Mapping[str, WordSliceRef],
) -> tuple[SemanticWordRef, ...]:
    """Merge overlapping slices only when their physical Q nets agree.

    Synthesis often decomposes one fixed product into overlapping high and low
    rows.  Treating those rows as independent operands hides the generating
    multiply.  The merge is structural rather than name-only: every shared bit
    must identify the same current-DCP net, and the recovered interval must be
    complete.
    """
    by_stem: dict[str, list[WordSliceRef]] = {}
    for item in leaves.values():
        by_stem.setdefault(item.stem, []).append(item)
    result = []
    for stem, items in sorted(by_stem.items()):
        items.sort(key=lambda item: (item.source_lsb, item.width, item.nets))
        start = min(item.source_lsb for item in items)
        stop = max(item.source_lsb + item.width for item in items)
        bits: dict[int, str] = {}
        for item in items:
            for ordinal, net in enumerate(item.nets):
                index = item.source_lsb + ordinal
                prior = bits.get(index)
                if prior is not None and prior != net:
                    raise ValueError(
                        "registered semantic word has inconsistent overlapping nets"
                    )
                bits[index] = net
        if sorted(bits) != list(range(start, stop)):
            raise ValueError("registered semantic word slices do not form one word")
        result.append(SemanticWordRef(
            stem=stem,
            source_lsb=start,
            width=stop - start,
            nets=tuple(bits[index] for index in range(start, stop)),
            slices=tuple(items),
        ))
    return tuple(result)


def _normalize_product_terms(
    terms: list[dict[str, Any]],
) -> tuple[int, list[int], int]:
    """Canonicalize exactly equivalent coefficient/shift representations."""
    if not terms or any(term.get("kind") != "fixed_product_slice" for term in terms):
        raise ValueError("generating operator contains a non-product term")
    common_shift = max(int(term["shift"]) for term in terms)
    coefficients = [
        int(term["coefficient"]) << (common_shift - int(term["shift"]))
        for term in terms
    ]
    coefficient_width = max(
        2, max(abs(value).bit_length() + 1 for value in coefficients)
    )
    if coefficient_width > 27:
        raise ValueError("generating operator coefficient exceeds DSP input contract")
    return common_shift, coefficients, coefficient_width


def _infer_quantized_bus_terms(
    bus: dict[str, Any],
    samples: list[tuple[int, int]],
    output_width: int,
) -> list[dict[str, Any]] | None:
    """Recover one product or a small separately-quantized product sum.

    HLS constant multipliers are sometimes retained as two signed-digit rows
    with a truncation boundary on each row, for example ``31*x`` as
    ``(32*x >>> s) + (-x >>> s)``.  That function differs from an ideal
    ``31*x >>> s`` on carry-sensitive inputs even though every one-hot vector
    exposes coefficient 31.  Preserve the two loss boundaries explicitly;
    the caller's joint partition replay remains the authority for accepting
    the decomposition.
    """
    direct = _infer_bus_term(bus, samples, output_width)
    if direct is not None:
        return [direct]

    width = int(bus["width"])
    mask = (1 << output_width) - 1
    observed_by_value = {
        int(value): int(observed) & mask for value, observed in samples
    }
    product_width = width + MAX_DSP_COEFFICIENT_WIDTH
    candidates: list[tuple[tuple[int, ...], list[dict[str, Any]]]] = []
    for shift in range(product_width - output_width + 1):
        probe = 1 << shift
        if probe >= 1 << width or probe not in observed_by_value:
            continue
        raw = observed_by_value[probe]
        totals = {raw}
        if raw & (1 << (output_width - 1)):
            totals.add(raw - (1 << output_width))
        for total in totals:
            for power in range(MAX_DSP_COEFFICIENT_WIDTH - 1):
                for first in (1 << power, -(1 << power)):
                    second = total - first
                    if (
                        second == 0
                        or not -(1 << (MAX_DSP_COEFFICIENT_WIDTH - 1))
                        <= second
                        < (1 << (MAX_DSP_COEFFICIENT_WIDTH - 1))
                    ):
                        continue
                    for data_signed in (True, False):
                        coefficients = (first, second)
                        if not all(
                            sum(
                                _product_value(
                                    value,
                                    data_width=width,
                                    data_signed=data_signed,
                                    coefficient=coefficient,
                                    coefficient_width=MAX_DSP_COEFFICIENT_WIDTH,
                                    shift=shift,
                                    output_width=output_width,
                                )
                                for coefficient in coefficients
                            ) & mask == observed
                            for value, observed in samples
                        ):
                            continue
                        terms = []
                        for coefficient in coefficients:
                            terms.append({
                                "kind": "fixed_product_slice",
                                "source": dict(bus),
                                "data_width": width,
                                "data_signed": data_signed,
                                "coefficient": coefficient,
                                "coefficient_width": MAX_DSP_COEFFICIENT_WIDTH,
                                "coefficient_signed": coefficient < 0,
                                "product_width": product_width,
                                "shift": shift,
                                "output_width": output_width,
                                "overflow": "wrap",
                                "recovery_method": (
                                    "signed_digit_separate_quantization_replay"
                                ),
                            })
                        rank = (
                            max(abs(value).bit_length() for value in coefficients),
                            sum(abs(value).bit_count() for value in coefficients),
                            sum(abs(value) for value in coefficients),
                            *sorted(coefficients),
                        )
                        candidates.append((rank, terms))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _generating_frontier_rank(plan: Mapping[str, Any]) -> tuple[int, int, int, int]:
    """Prefer a complete word-level operator over an early residual cut.

    A shallow registered frontier can replay exactly while retaining
    ``passthrough`` operands produced by the incumbent LUT/CARRY arithmetic.
    Such a plan is functionally valid but cannot sweep that arithmetic wall.
    Depth is only a tie-breaker after semantic completeness, so this does not
    blindly flatten through registered state.
    """
    terms = [
        term for term in (plan.get("terms") or [])
        if isinstance(term, Mapping)
    ]
    passthrough_count = sum(
        str(term.get("kind") or "") == "passthrough" for term in terms
    )
    product_count = sum(
        str(term.get("kind") or "") == "fixed_product_slice"
        for term in terms
    )
    depth = int((plan.get("recovery") or {}).get("schedule_cut_depth", 0))
    return (
        int(passthrough_count == 0),
        -passthrough_count,
        product_count,
        depth,
    )


def _is_product_only_generating_frontier(plan: Mapping[str, Any]) -> bool:
    terms = [
        term for term in (plan.get("terms") or [])
        if isinstance(term, Mapping)
    ]
    return bool(terms) and all(
        str(term.get("kind") or "") == "fixed_product_slice"
        for term in terms
    )


def _exact_quantization_groups(
    terms: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    output_width: int,
) -> list[dict[str, Any]]:
    """Find the exact small term partition instead of assuming transitivity.

    Quantization interaction is not an equivalence relation: a retained
    residual can interact with two different product paths even when those
    products must be truncated separately.  A connected-component closure of
    pair interactions therefore over-merges.  Generating operators contain at
    most eight terms (Bell(8)=4140 partitions), so deterministic exhaustive
    partition replay is both cheap and exact for the recovered sample table.
    """
    mask = (1 << output_width) - 1
    row_targets = [int(row["source_full_output"]) & mask for row in rows]
    block_values: dict[tuple[int, ...], list[int]] = {}

    def values_for(block: tuple[int, ...]) -> list[int]:
        cached = block_values.get(block)
        if cached is not None:
            return cached
        group = {
            "term_ordinals": block,
            "common_shift": max(terms[index]["shift"] for index in block),
            "destination_lsb": 0,
            "output_width": output_width,
        }
        result = [
            _group_value(group, terms, row["inputs"], output_width)
            for row in rows
        ]
        block_values[block] = result
        return result

    def partitions_with_count(
        block_count: int,
    ) -> list[tuple[tuple[int, ...], ...]]:
        """Generate only one partition cardinality with exact pruning.

        The old implementation materialized every Bell partition and replayed
        the full sample table for all of them before selecting the most split
        valid result.  In registered FIR recovery the all-singleton partition
        is commonly exact, so that work was almost entirely discarded.  This
        generator examines cardinalities in the same preference order and can
        stop as soon as the best cardinality has a valid member.
        """
        found: list[tuple[tuple[int, ...], ...]] = []

        def visit(
            ordinal: int, blocks: tuple[tuple[int, ...], ...]
        ) -> None:
            remaining = len(terms) - ordinal
            if len(blocks) > block_count or len(blocks) + remaining < block_count:
                return
            if ordinal == len(terms):
                if len(blocks) == block_count:
                    found.append(blocks)
                return
            for index in range(len(blocks)):
                updated = list(blocks)
                updated[index] = (*updated[index], ordinal)
                visit(ordinal + 1, tuple(updated))
            visit(ordinal + 1, (*blocks, (ordinal,)))

        visit(0, ())
        return found

    # Preserve the previous selection contract exactly: maximize group count,
    # then the lexicographic block tuple.  The sample set and arithmetic replay
    # are unchanged.
    for block_count in range(len(terms), 0, -1):
        valid: list[tuple[tuple[int, ...], ...]] = []
        for partition in partitions_with_count(block_count):
            vectors = [values_for(block) for block in partition]
            if all(
                sum(vector[row_ordinal] for vector in vectors) & mask == target
                for row_ordinal, target in enumerate(row_targets)
            ):
                valid.append(partition)
        if not valid:
            continue
        selected = max(valid)
        return [
            {
                "group_id": f"group_{group_ordinal}",
                "term_ordinals": list(block),
                "common_shift": max(terms[index]["shift"] for index in block),
                "destination_lsb": 0,
                "output_width": output_width,
                "interaction_screened": len(block) > 1,
            }
            for group_ordinal, block in enumerate(selected)
        ]
    raise ValueError(
        "no quantization partition replays pair and joint closure vectors"
    )


@dataclass
class ClosureRecord:
    node_id: str
    root_stem: str
    slice_index: int
    node_kind: str
    graph: ConeGraph
    _word: RecoveredWordNode | None = None
    _recovery_error: Exception | None = None
    _recovery_attempted: bool = False

    def recover(self) -> RecoveredWordNode | None:
        if not self._recovery_attempted:
            self._recovery_attempted = True
            try:
                self._word = recover_word_node(self.graph)
            except ValueError as exc:
                if str(exc) != "registered closure node has no indexed word boundary":
                    raise
                self._recovery_error = exc
        return self._word


class RegisteredClosure:
    """Interval-aware registered word DAG loaded from one current DCP.

    A register stem may occur more than once as overlapping retained slices.
    The old ``dict[stem, node]`` representation silently selected one of those
    slices.  This loader resolves the smallest producer interval covering each
    consumer bus within the same observable root/slice, which preserves the
    actual DCP bit identities without relying on hierarchy naming conventions.
    """

    def __init__(self, root: Path, records: list[ClosureRecord]):
        self.root = root
        self.records = tuple(records)
        self._by_group_stem: dict[tuple[str, int, str], list[ClosureRecord]] = {}
        self._by_id = {record.node_id: record for record in records}
        self._control_protocol_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._primitive_boundary_record_ids_cache: frozenset[str] | None = None
        self._boundary_ff_rows_cache: dict[str, dict[str, str]] | None = None
        self._proved_control_net_aliases: dict[str, dict[str, str]] = {
            "clock_net": {},
            "ce_net": {},
            "sr_net": {},
        }
        for record in records:
            key = (record.root_stem, record.slice_index, record.graph.manifest["target_stem"])
            self._by_group_stem.setdefault(key, []).append(record)

    def control_sink_pins(self, field: str) -> dict[str, str]:
        """Return one exact sink pin for every observed FF control net."""
        pin_name = {
            "clock_net": "C", "ce_net": "CE", "sr_net": "R"
        }.get(field)
        if pin_name is None:
            raise ValueError("unsupported registered control field")
        result = {}
        for record in self.records:
            path = record.graph.root / "boundary.tsv"
            with path.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
            rows.extend(record.graph.targets)
            for raw in rows:
                if not str(raw.get("ref") or "").upper().startswith("FD"):
                    continue
                net = _one_object_name(raw.get(field), field)
                if net and net not in result:
                    cell = _one_object_name(raw.get("cell"), "cell")
                    result[net] = f"{cell}/{pin_name}"
        return result

    def install_proved_control_net_aliases(
        self,
        field: str,
        aliases: Mapping[str, str],
    ) -> None:
        """Install only hash-bound live-DCP control aliases proved by RW."""
        if field not in self._proved_control_net_aliases:
            raise ValueError("unsupported registered control alias field")
        observed = self.control_sink_pins(field)
        checked = {}
        for source, representative in aliases.items():
            if (
                not isinstance(source, str)
                or not isinstance(representative, str)
                or source not in observed
                or representative not in observed
            ):
                raise ValueError("proved control alias references an unknown net")
            checked[source] = representative
        self._proved_control_net_aliases[field] = checked
        self._boundary_ff_rows_cache = None

    @classmethod
    def load(cls, root: Path | str) -> "RegisteredClosure":
        root = Path(root)
        manifest = root / "closure.tsv"
        if not manifest.is_file():
            raise ValueError("registered closure manifest is missing")
        with manifest.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            fieldnames = set(reader.fieldnames or [])
            rows = list(reader)
        required = {
            "node_id", "root_stem", "slice_index", "target_stem",
            "target_lsb", "target_count", "node_kind",
        }
        if fieldnames != required:
            raise ValueError("registered closure manifest schema is unsupported")
        records = []
        seen = set()
        for row in rows:
            node_id = str(row["node_id"])
            if re.fullmatch(r"node_[0-9]{4,}", node_id) is None or node_id in seen:
                raise ValueError("registered closure node identity is invalid")
            seen.add(node_id)
            node_dir = root / node_id
            graph = ConeGraph.load(node_dir)
            if (
                graph.manifest.get("target_stem") != row["target_stem"]
                or len(graph.targets) != int(row["target_count"])
                or min(
                    int(item.get("source_index", item["index"]))
                    for item in graph.targets
                ) != int(row["target_lsb"])
            ):
                raise ValueError("registered closure node manifest does not match facts")
            records.append(ClosureRecord(
                node_id=node_id,
                root_stem=str(row["root_stem"]),
                slice_index=int(row["slice_index"]),
                node_kind=str(row["node_kind"]),
                graph=graph,
            ))
        return cls(root, records)

    def roots(self) -> tuple[ClosureRecord, ...]:
        return tuple(record for record in self.records if record.node_kind == "root")

    def record(self, node_id: str) -> ClosureRecord:
        try:
            return self._by_id[node_id]
        except KeyError as exc:
            raise ValueError("registered closure node is unknown") from exc

    def resolve_producer(
        self, consumer: ClosureRecord, bus: WordBus
    ) -> ClosureRecord | None:
        candidates = []
        for record in self._by_group_stem.get(
            (consumer.root_stem, consumer.slice_index, bus.stem), []
        ):
            if record.node_id == consumer.node_id:
                continue
            word = record.recover()
            if word is None:
                continue
            start = word.target_lsb
            stop = start + word.width
            if start <= bus.source_lsb and stop >= bus.source_lsb + bus.width:
                candidates.append(record)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (
            item.recover().width,  # type: ignore[union-attr]
            item.recover().target_lsb,  # type: ignore[union-attr]
            item.node_id,
        ))
        return candidates[0]

    def leaf_slices(
        self,
        root: ClosureRecord,
        *,
        max_depth: int | None = None,
        blocked_producer_ids: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, WordSliceRef], dict[str, set[int]]]:
        leaves: dict[str, WordSliceRef] = {}
        depths: dict[str, set[int]] = {}

        def visit(record: ClosureRecord, depth: int, visiting: set[str]) -> None:
            if record.node_id in visiting:
                raise ValueError("registered closure word DAG contains a cycle")
            word = record.recover()
            if word is None:
                raise ValueError("registered closure root is not evaluable")
            visiting.add(record.node_id)
            for bus in word.buses:
                producer = (
                    None
                    if max_depth is not None and depth + 1 >= max_depth
                    else self.resolve_producer(record, bus)
                )
                if producer is not None and producer.node_id in blocked_producer_ids:
                    producer = None
                if producer is not None:
                    visit(producer, depth + 1, visiting)
                    continue
                leaf = WordSliceRef(
                    stem=bus.stem,
                    source_lsb=bus.source_lsb,
                    width=bus.width,
                    nets=bus.nets,
                )
                prior = leaves.get(leaf.key)
                if prior is not None and prior.nets != leaf.nets:
                    raise ValueError("registered closure leaf wiring is inconsistent")
                leaves[leaf.key] = leaf
                depths.setdefault(leaf.key, set()).add(depth + 1)
            visiting.remove(record.node_id)

        visit(root, 0, set())
        return leaves, depths

    def leaf_paths(
        self,
        root: ClosureRecord,
        *,
        max_depth: int,
        blocked_producer_ids: frozenset[str] = frozenset(),
    ) -> dict[str, list[tuple[ClosureRecord, ...]]]:
        """Return target-to-frontier register paths for schedule recovery."""
        paths: dict[str, list[tuple[ClosureRecord, ...]]] = {}

        def visit(
            record: ClosureRecord,
            depth: int,
            path: tuple[ClosureRecord, ...],
            visiting: set[str],
        ) -> None:
            if record.node_id in visiting:
                raise ValueError("registered closure word DAG contains a cycle")
            word = record.recover()
            if word is None:
                raise ValueError("registered closure root is not evaluable")
            visiting.add(record.node_id)
            for bus in word.buses:
                producer = (
                    None
                    if depth + 1 >= max_depth
                    else self.resolve_producer(record, bus)
                )
                if producer is not None and producer.node_id in blocked_producer_ids:
                    producer = None
                if producer is not None:
                    visit(producer, depth + 1, path + (producer,), visiting)
                    continue
                leaf = WordSliceRef(
                    stem=bus.stem,
                    source_lsb=bus.source_lsb,
                    width=bus.width,
                    nets=bus.nets,
                )
                paths.setdefault(leaf.key, []).append(path)
            visiting.remove(record.node_id)

        visit(root, 0, (root,), set())
        return paths

    def direct_product_candidate_records(self) -> tuple[ClosureRecord, ...]:
        """Find registered word producers feeding current observable roots."""
        candidates = {}
        for root in self.roots():
            word = root.recover()
            if word is None:
                continue
            for bus in word.buses:
                if bus.width != word.width:
                    continue
                producer = self.resolve_producer(root, bus)
                if producer is not None and producer.node_kind != "root":
                    candidates[producer.node_id] = producer
        return tuple(candidates[key] for key in sorted(candidates))

    def evaluate(
        self,
        root: ClosureRecord,
        leaf_values: Mapping[str, int],
        *,
        max_depth: int | None = None,
        blocked_producer_ids: frozenset[str] = frozenset(),
    ) -> int:
        visiting: set[str] = set()

        def evaluate_record(record: ClosureRecord, depth: int) -> int:
            if record.node_id in visiting:
                raise ValueError("registered closure word DAG contains a cycle")
            word = record.recover()
            if word is None:
                raise ValueError("registered closure node is not evaluable")
            visiting.add(record.node_id)
            values = {}
            for bus in word.buses:
                producer = (
                    None
                    if max_depth is not None and depth + 1 >= max_depth
                    else self.resolve_producer(record, bus)
                )
                if producer is not None and producer.node_id in blocked_producer_ids:
                    producer = None
                if producer is None:
                    leaf = WordSliceRef(
                        stem=bus.stem,
                        source_lsb=bus.source_lsb,
                        width=bus.width,
                        nets=bus.nets,
                    )
                    raw = int(leaf_values.get(leaf.key, 0))
                    relative_lsb = 0
                else:
                    producer_word = producer.recover()
                    assert producer_word is not None
                    raw = evaluate_record(producer, depth + 1)
                    relative_lsb = bus.source_lsb - producer_word.target_lsb
                if relative_lsb < 0:
                    raise ValueError("registered closure bus precedes producer slice")
                values[bus.key] = (raw >> relative_lsb) & (
                    (1 << bus.width) - 1
                )
            result = word.evaluate(values)
            visiting.remove(record.node_id)
            return result

        return evaluate_record(root, 0)

    def evaluate_batch(
        self,
        root: ClosureRecord,
        leaf_values: list[Mapping[str, int]],
        *,
        max_depth: int | None = None,
        blocked_producer_ids: frozenset[str] = frozenset(),
    ) -> list[int]:
        """Evaluate a complete registered word DAG bit-parallel.

        A batch traverses each recovered closure node once and packs all
        scenarios into the Boolean value carried by every net.  The proof set
        is unchanged; only its execution representation differs from repeated
        scalar recursion.
        """
        if not leaf_values:
            return []
        visiting: set[tuple[str, int]] = set()
        memo: dict[tuple[str, int], list[int]] = {}

        def evaluate_record(record: ClosureRecord, depth: int) -> list[int]:
            cache_key = (record.node_id, depth)
            cached = memo.get(cache_key)
            if cached is not None:
                return cached
            if cache_key in visiting:
                raise ValueError("registered closure word DAG contains a cycle")
            word = record.recover()
            if word is None:
                raise ValueError("registered closure node is not evaluable")
            visiting.add(cache_key)
            values: list[dict[str, int]] = [dict() for _ in leaf_values]
            for bus in word.buses:
                producer = (
                    None
                    if max_depth is not None and depth + 1 >= max_depth
                    else self.resolve_producer(record, bus)
                )
                if producer is not None and producer.node_id in blocked_producer_ids:
                    producer = None
                if producer is None:
                    leaf = WordSliceRef(
                        stem=bus.stem,
                        source_lsb=bus.source_lsb,
                        width=bus.width,
                        nets=bus.nets,
                    )
                    raw_values = [
                        int(item.get(leaf.key, 0)) for item in leaf_values
                    ]
                    relative_lsb = 0
                else:
                    producer_word = producer.recover()
                    assert producer_word is not None
                    raw_values = evaluate_record(producer, depth + 1)
                    relative_lsb = bus.source_lsb - producer_word.target_lsb
                if relative_lsb < 0:
                    raise ValueError("registered closure bus precedes producer slice")
                mask = (1 << bus.width) - 1
                for scenario, raw in enumerate(raw_values):
                    values[scenario][bus.key] = (raw >> relative_lsb) & mask
            result = word.evaluate_batch(values)
            visiting.remove(cache_key)
            memo[cache_key] = result
            return result

        return evaluate_record(root, 0)

    def _logical_control_groups(
        self, record: ClosureRecord
    ) -> tuple[tuple[str, ...], ...]:
        """Return exact logical predicates for one observable lane DAG.

        Controls are merged only when their registered source contract is
        identical.  A shared D net plus clock/edge, CE, reset, primitive and
        INIT proves physical replicas carry one state bit; everything else is
        retained as an independent predicate.
        """
        boundary = _boundary_ff_rows(self)

        def predicate_key(net: str) -> tuple[Any, ...]:
            row = boundary.get(net)
            if row is None or not row.get("d_net"):
                return ("independent", net)
            return (
                "registered_state", row["ref"], row["d_net"],
                row["clock_net"], row["ce_net"], row["sr_net"],
                row["init"],
            )

        root_word = record.recover()
        if root_word is None:
            raise ValueError("registered closure root is not evaluable")
        root_keys = tuple(dict.fromkeys(
            predicate_key(net) for net in root_word.controls
        ))
        grouped: dict[tuple[Any, ...], list[str]] = {
            key: [] for key in root_keys
        }
        for net in boundary:
            key = predicate_key(net)
            if key in grouped:
                grouped[key].append(net)
        for net in root_word.controls:
            key = predicate_key(net)
            if net not in grouped[key]:
                grouped[key].append(net)
        return tuple(
            tuple(sorted(set(grouped[key]))) for key in root_keys
        )

    def _control_protocol_key(
        self, groups: tuple[tuple[str, ...], ...]
    ) -> tuple[Any, ...]:
        boundary = _boundary_ff_rows(self)
        result = []
        for group in groups:
            signatures = set()
            for net in group:
                row = boundary.get(net)
                if row is None or not row.get("d_net"):
                    signatures.add(("independent", net))
                else:
                    signatures.add((
                        "registered_state", row["ref"], row["d_net"],
                        row["clock_net"], row["ce_net"], row["sr_net"],
                        row["init"],
                    ))
            if len(signatures) != 1:
                raise ValueError("logical control group has inconsistent provenance")
            result.append(next(iter(signatures)))
        return tuple(result)

    @staticmethod
    def _attach_control_protocol(
        plan: dict[str, Any],
        *,
        predicate_count: int,
        active_table: int,
        phase_count: int,
        selected_phase: int,
        recovery: str,
    ) -> None:
        plan["controls"] = {
            "kind": "exhaustive_registered_predicate_protocol_v1",
            "predicate_roles": [
                f"predicate_{ordinal}" for ordinal in range(predicate_count)
            ],
            "arithmetic_active_truth_table": active_table,
            "arithmetic_phase_count": active_table.bit_count(),
            "phase_count": phase_count,
            "recovery": recovery,
        }
        plan["recovery"].update({
            "control_predicate_count": predicate_count,
            "arithmetic_active_truth_table": active_table,
            "selected_control_phase": selected_phase,
            "control_phase_recovery": recovery,
        })

    def _apply_control_phase(
        self,
        record: ClosureRecord,
        groups: tuple[tuple[str, ...], ...],
        assignment: tuple[bool, ...],
    ) -> None:
        values = {
            net: bool(value)
            for group, value in zip(groups, assignment)
            for net in group
        }
        for item in self.records:
            if (
                item.root_stem != record.root_stem
                or item.slice_index != record.slice_index
            ):
                continue
            word = item.recover()
            if word is None:
                continue
            word.active_controls = {
                net: values.get(net, bool(word.active_controls.get(net, False)))
                for net in word.controls
            }

    @staticmethod
    def _generating_plan_signature(plan: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            int(plan["data_width"]),
            tuple(int(value) for value in plan["input_widths"]),
            tuple(bool(value) for value in plan["input_signed"]),
            tuple(
                tuple(sorted((str(key), repr(value)) for key, value in term.items()))
                for term in plan["terms"]
            ),
            tuple(
                (
                    tuple(int(value) for value in group["term_ordinals"]),
                    int(group["common_shift"]),
                    int(group["output_width"]),
                )
                for group in plan["groups"]
            ),
        )

    def infer_generating_operator_ir(
        self,
        record: ClosureRecord,
        *,
        module_name: str,
        allow_passthrough_only: bool = False,
        candidate_validator: Callable[
            [dict[str, Any], tuple[SemanticWordRef, ...]], None
        ] | None = None,
    ) -> tuple[dict[str, Any], tuple[SemanticWordRef, ...]]:
        """Jointly recover data semantics and the complete control phase.

        No activity score chooses an anonymous Boolean assignment.  Every
        reachable combination of independently proved predicates is screened;
        arithmetic semantics must replay over data vectors in that phase.  A
        lane with multiple distinct arithmetic modes is rejected rather than
        guessed.  The resulting truth table records every phase exposing the
        single proved operator.
        """
        groups = self._logical_control_groups(record)
        if len(groups) > 8:
            raise ValueError(
                "control protocol exceeds exhaustive predicate recovery"
            )
        protocol_key = self._control_protocol_key(groups)
        cached_protocol = self._control_protocol_cache.get(protocol_key)
        if cached_protocol is not None:
            assignment = tuple(cached_protocol["selected_assignment"])
            self._apply_control_phase(record, groups, assignment)
            try:
                plan, operands = self._infer_generating_operator_for_current_phase(
                    record,
                    module_name=module_name,
                    candidate_validator=candidate_validator,
                    allow_passthrough_only=allow_passthrough_only,
                )
            except ValueError:
                # A shared physical predicate does not prove every consumer is
                # the same operator.  Fall through to a complete phase screen
                # for this lane; disagreement is then explicit, never guessed.
                pass
            else:
                self._attach_control_protocol(
                    plan,
                    predicate_count=len(groups),
                    active_table=int(cached_protocol["active_table"]),
                    phase_count=1 << len(groups),
                    selected_phase=int(cached_protocol["selected_phase"]),
                    recovery="bank_shared_control_protocol_replay",
                )
                return plan, operands
        lane_records = [
            item for item in self.records
            if item.root_stem == record.root_stem
            and item.slice_index == record.slice_index
        ]
        original = {
            item.node_id: dict(item.recover().active_controls)
            for item in lane_records if item.recover() is not None
        }
        recovered: dict[
            tuple[Any, ...],
            list[tuple[int, tuple[bool, ...], dict[str, Any], tuple[SemanticWordRef, ...]]],
        ] = {}
        failures = []
        for phase_index, assignment in enumerate(
            product((False, True), repeat=len(groups))
        ):
            self._apply_control_phase(record, groups, assignment)
            try:
                plan, operands = self._infer_generating_operator_for_current_phase(
                    record,
                    module_name=module_name,
                    candidate_validator=candidate_validator,
                    allow_passthrough_only=allow_passthrough_only,
                )
            except ValueError as exc:
                failures.append(f"phase={phase_index}:{exc}")
                continue
            signature = self._generating_plan_signature(plan)
            recovered.setdefault(signature, []).append((
                phase_index, assignment, plan, operands,
            ))
        if not recovered:
            for item in lane_records:
                word = item.recover()
                if word is not None and item.node_id in original:
                    word.active_controls = original[item.node_id]
            raise ValueError(
                "no control phase exposes one replay-proved generating operator: "
                + "; ".join(failures[-4:])
            )
        if len(recovered) != 1:
            for item in lane_records:
                word = item.recover()
                if word is not None and item.node_id in original:
                    word.active_controls = original[item.node_id]
            raise ValueError(
                "control protocol exposes multiple distinct arithmetic modes"
            )
        phases = next(iter(recovered.values()))
        operand_wiring = {
            tuple((item.source_lsb, item.width, item.nets) for item in operands)
            for _, _, _, operands in phases
        }
        if len(operand_wiring) != 1:
            raise ValueError(
                "control phases expose one formula through different data frontiers"
            )
        phase_index, assignment, plan, operands = phases[0]
        self._apply_control_phase(record, groups, assignment)
        active_table = sum(1 << item[0] for item in phases)
        self._control_protocol_cache[protocol_key] = {
            "active_table": active_table,
            "selected_phase": phase_index,
            "selected_assignment": list(assignment),
        }
        self._attach_control_protocol(
            plan,
            predicate_count=len(groups),
            active_table=active_table,
            phase_count=1 << len(groups),
            selected_phase=phase_index,
            recovery="exhaustive_data_control_joint_replay",
        )
        return plan, operands

    def _infer_generating_operator_for_current_phase(
        self,
        record: ClosureRecord,
        *,
        module_name: str,
        allow_passthrough_only: bool = False,
        candidate_validator: Callable[
            [dict[str, Any], tuple[SemanticWordRef, ...]], None
        ] | None = None,
    ) -> tuple[dict[str, Any], tuple[SemanticWordRef, ...]]:
        """Recover one high-level operator under an explicit control phase."""
        failures = []
        candidates: list[
            tuple[dict[str, Any], tuple[SemanticWordRef, ...]]
        ] = []
        group_size = sum(
            1 for item in self.records
            if item.root_stem == record.root_stem
            and item.slice_index == record.slice_index
        )
        primitive_boundaries = self._primitive_data_boundary_record_ids()
        cut_variants = [frozenset()]
        if primitive_boundaries:
            cut_variants.append(primitive_boundaries)
        for blocked_producer_ids in cut_variants:
            # Rank traceback depths with cheap structural facts before doing
            # any Boolean/vector replay.  HLS arithmetic commonly exposes
            # narrow partial words at shallow depths and a stable bank of
            # complete registered words a few edges later.  Search order is
            # only an acceleration: every admitted plan still passes exact
            # semantic and control replay, and all depths remain fallbacks.
            depth_frontiers = []
            output_width = int(record.recover().width)  # type: ignore[union-attr]
            for max_depth in range(1, group_size + 2):
                try:
                    leaves, _ = self.leaf_slices(
                        record,
                        max_depth=max_depth,
                        blocked_producer_ids=blocked_producer_ids,
                    )
                    merged = _merge_semantic_word_slices(leaves)
                    signature = tuple(
                        (item.stem, item.source_lsb, item.width, item.nets)
                        for item in merged
                    )
                    complete_words = sum(
                        item.source_lsb == 0 and item.width == output_width
                        for item in merged
                    )
                    supported_count = 2 <= len(merged) <= 8
                except ValueError:
                    signature = ()
                    complete_words = 0
                    supported_count = False
                    merged = ()
                depth_frontiers.append({
                    "depth": max_depth,
                    "signature": signature,
                    "operand_count": len(merged),
                    "complete_words": complete_words,
                    "supported_count": supported_count,
                })
            for ordinal, row in enumerate(depth_frontiers[:-1]):
                row["stable_next"] = bool(
                    row["signature"]
                    and row["signature"]
                    == depth_frontiers[ordinal + 1]["signature"]
                )
            if depth_frontiers:
                depth_frontiers[-1]["stable_next"] = False
            ordered_depths = sorted(
                depth_frontiers,
                key=lambda row: (
                    bool(row["supported_count"]),
                    bool(row["operand_count"])
                    and row["complete_words"] == row["operand_count"],
                    int(row["complete_words"]),
                    bool(row["stable_next"]),
                    -int(row["depth"]),
                ),
                reverse=True,
            )
            for frontier in ordered_depths:
                max_depth = int(frontier["depth"])
                try:
                    candidate = self._infer_generating_operator_ir_at_depth(
                        record,
                        module_name=module_name,
                        max_depth=max_depth,
                        blocked_producer_ids=blocked_producer_ids,
                        allow_passthrough_only=allow_passthrough_only,
                    )
                except ValueError as exc:
                    cut_kind = "opaque_primitive" if blocked_producer_ids else "expanded"
                    failures.append(f"{cut_kind}:depth={max_depth}:{exc}")
                    continue
                candidates.append(candidate)
                # A replay-proved product-only frontier no longer retains an
                # incumbent LUT/CARRY arithmetic residual.  Once the emitter's
                # registered control boundary also validates, deeper traceback
                # cannot improve the action's observable data cut and only
                # repeats expensive semantic recovery.
                if (
                    _is_product_only_generating_frontier(candidate[0])
                    and candidate_validator is not None
                ):
                    try:
                        candidate_validator(*candidate)
                    except ValueError as exc:
                        failures.append(
                            f"emitter_boundary:depth={max_depth}:{exc}"
                        )
                    else:
                        return candidate
        if candidates:
            for candidate in sorted(
                candidates,
                key=lambda item: _generating_frontier_rank(item[0]),
                reverse=True,
            ):
                if candidate_validator is None:
                    return candidate
                try:
                    candidate_validator(*candidate)
                except ValueError as exc:
                    depth = int(
                        (candidate[0].get("recovery") or {}).get(
                            "schedule_cut_depth", 0
                        )
                    )
                    failures.append(
                        f"emitter_boundary:depth={depth}:{exc}"
                    )
                    continue
                return candidate
        raise ValueError(
            "no replay-proved generating operator frontier: "
            + "; ".join(failures[-4:])
        )

    def _primitive_data_boundary_record_ids(self) -> frozenset[str]:
        """Return closure nodes whose data graph terminates at a hard macro."""
        cached = self._primitive_boundary_record_ids_cache
        if cached is not None:
            return cached
        result = set()
        for record in self.records:
            path = record.graph.root / "boundary.tsv"
            with path.open("r", encoding="utf-8", newline="") as stream:
                rows = csv.DictReader(stream, delimiter="\t")
                if any(
                    str(row.get("ref") or "").upper().startswith(
                        ("RAMB", "URAM", "DSP")
                    )
                    for row in rows
                ):
                    result.add(record.node_id)
        cached = frozenset(result)
        self._primitive_boundary_record_ids_cache = cached
        return cached

    def _infer_generating_operator_ir_at_depth(
        self,
        record: ClosureRecord,
        *,
        module_name: str,
        max_depth: int,
        blocked_producer_ids: frozenset[str] = frozenset(),
        allow_passthrough_only: bool = False,
    ) -> tuple[dict[str, Any], tuple[SemanticWordRef, ...]]:
        word = record.recover()
        if word is None:
            raise ValueError("generating operator root is not evaluable")
        leaves, _ = self.leaf_slices(
            record,
            max_depth=max_depth,
            blocked_producer_ids=blocked_producer_ids,
        )
        independent = _merge_semantic_word_slices(leaves)
        try:
            aliased = _merge_semantic_data_aliases(
                independent, _boundary_ff_rows(self)
            )
        except ValueError:
            # A complete arithmetic D cone can start directly at a BRAM/DSP
            # output.  Such a raw word has no standalone CE/reset contract and
            # therefore cannot participate in the broad FF-alias pass.  A
            # narrower quantization-group correlation pass below may still
            # prove that it is the exact D source of one registered operand.
            aliased = independent
        variants = [independent]
        if aliased != independent:
            variants.append(aliased)
        failures = []
        recovered_variants = []
        for variant_name, operands in zip(
            ("independent_temporal_states", "same_d_ce_aliases"), variants
        ):
            try:
                plan, recovered_operands = self._infer_generating_operator_for_operands(
                    record,
                    word=word,
                    operands=operands,
                    module_name=module_name,
                    max_depth=max_depth,
                    blocked_producer_ids=blocked_producer_ids,
                    allow_passthrough_only=allow_passthrough_only,
                )
                boundary_rows = _boundary_ff_rows(self)
                for operand in recovered_operands:
                    _operand_wiring_at_cut(
                        self,
                        record,
                        operand,
                        max_depth=max_depth,
                        boundary_rows=boundary_rows,
                        blocked_producer_ids=blocked_producer_ids,
                    )
                plan["recovery"]["operand_alias_model"] = variant_name
                recovered_variants.append((plan, recovered_operands))
            except ValueError as exc:
                failures.append(f"{variant_name}:{exc}")
        if recovered_variants:
            return max(
                recovered_variants,
                key=lambda item: _generating_frontier_rank(item[0]),
            )
        raise ValueError("; ".join(failures))

    def _operand_source_signature(
        self,
        record: ClosureRecord,
        operand: SemanticWordRef,
        *,
        max_depth: int,
        boundary_rows: Mapping[str, dict[str, str]],
        paths: Mapping[str, list[tuple[ClosureRecord, ...]]],
    ) -> tuple[tuple[Any, ...], bool] | None:
        """Return physical sampled-data identity and whether the cut is an FF.

        A hard-macro output has no separate CE/reset contract at this cut.  It
        may be folded only with a registered operand whose D vector resolves
        to the same physical output pins through Vivado net segments; the FF
        representative then supplies the complete schedule contract.
        """
        rows = [boundary_rows.get(net) for net in operand.nets]
        if any(row is None for row in rows):
            return None
        checked = [row for row in rows if row is not None]
        registered = all(str(row.get("ref") or "").startswith("FD") for row in checked)
        primitive_aliases: dict[str, str] = {}
        for row in boundary_rows.values():
            if str(row.get("ref") or "").startswith("FD"):
                continue
            source_id = str(row.get("cell") or "")
            for alias in str(row.get("d_net") or "").split("|"):
                if not alias:
                    continue
                prior = primitive_aliases.get(alias)
                if prior is not None and prior != source_id:
                    raise ValueError("primitive data alias maps to multiple output pins")
                primitive_aliases[alias] = source_id
        if registered:
            try:
                data_nets, _ = _frontier_operand_wiring(
                    operand, boundary_rows
                )
            except ValueError:
                return None
            source_ids = [primitive_aliases.get(net, net) for net in data_nets]
        else:
            if any(str(row.get("ref") or "").startswith("FD") for row in checked):
                return None
            source_ids = [str(row.get("cell") or "") for row in checked]
            if any(not source_id for source_id in source_ids):
                return None
        return (
            (
                int(operand.source_lsb), int(operand.width),
                tuple(source_ids),
            ),
            registered,
        )

    def _fold_correlated_quantization_operands(
        self,
        record: ClosureRecord,
        operands: tuple[SemanticWordRef, ...],
        groups: list[dict[str, Any]],
        *,
        max_depth: int,
        blocked_producer_ids: frozenset[str] = frozenset(),
    ) -> tuple[tuple[SemanticWordRef, ...], int]:
        """Merge same-sample terms only within one proved loss boundary."""
        boundary_rows = _boundary_ff_rows(self)
        paths = self.leaf_paths(
            record,
            max_depth=max_depth,
            blocked_producer_ids=blocked_producer_ids,
        )
        signatures = [
            self._operand_source_signature(
                record,
                operand,
                max_depth=max_depth,
                boundary_rows=boundary_rows,
                paths=paths,
            )
            for operand in operands
        ]
        fold_sets: list[list[int]] = []
        for group in groups:
            buckets: dict[tuple[Any, ...], list[int]] = {}
            for raw_ordinal in group["term_ordinals"]:
                ordinal = int(raw_ordinal)
                signature = signatures[ordinal]
                if signature is not None:
                    buckets.setdefault(signature[0], []).append(ordinal)
            fold_sets.extend(
                members for members in buckets.values() if len(members) > 1
            )
        if not fold_sets:
            return operands, 0
        consumed = set()
        replacements: dict[int, SemanticWordRef] = {}
        for members in fold_sets:
            if consumed.intersection(members):
                raise ValueError("quantization operand folds overlap")
            consumed.update(members)
            registered_members = [
                ordinal for ordinal in members
                if signatures[ordinal] is not None and signatures[ordinal][1]
            ]
            raw_members = [
                ordinal for ordinal in members
                if signatures[ordinal] is not None and not signatures[ordinal][1]
            ]
            if not registered_members or not raw_members:
                # This fold exists only to bridge one primitive data output to
                # an equivalent registered sample.  Registered-only aliases
                # retain their full schedule contract and are handled by the
                # earlier exact D/CE alias pass.
                continue
            representative_ordinal = registered_members[0]
            representative = operands[representative_ordinal]
            slices = []
            seen = set()
            for ordinal in members:
                operand = operands[ordinal]
                if (
                    operand.source_lsb != representative.source_lsb
                    or operand.width != representative.width
                ):
                    raise ValueError("correlated operand widths disagree")
                for item in operand.slices:
                    identity = (
                        item.stem, item.source_lsb, item.width, item.nets
                    )
                    if identity not in seen:
                        seen.add(identity)
                        slices.append(item)
            replacements[min(members)] = SemanticWordRef(
                stem=representative.stem,
                source_lsb=representative.source_lsb,
                width=representative.width,
                nets=representative.nets,
                slices=tuple(slices),
            )
        if not replacements:
            return operands, 0
        folded = []
        # Only suppress members of folds that actually produced a registered
        # representative.  A source-only correlation is useful evidence but
        # is not a legal emitter boundary; retaining those operands is safer
        # than silently deleting them because another bucket did fold.
        replaced_members = set()
        for first in replacements:
            for members in fold_sets:
                if min(members) == first:
                    replaced_members.update(members)
                    break
        for ordinal, operand in enumerate(operands):
            if ordinal in replacements:
                folded.append(replacements[ordinal])
            elif ordinal not in replaced_members:
                folded.append(operand)
        return tuple(folded), len(replacements)

    def _infer_generating_operator_for_operands(
        self,
        record: ClosureRecord,
        *,
        word: RecoveredWordNode,
        operands: tuple[SemanticWordRef, ...],
        module_name: str,
        max_depth: int,
        allow_source_folding: bool = True,
        blocked_producer_ids: frozenset[str] = frozenset(),
        allow_passthrough_only: bool = False,
    ) -> tuple[dict[str, Any], tuple[SemanticWordRef, ...]]:
        if not 2 <= len(operands) <= 8:
            raise ValueError("generating operator has an unsupported operand count")
        operand_probe_values: list[tuple[SemanticWordRef, list[int]]] = []
        replay_assignments: list[Mapping[str, int]] = [{}]
        for operand in operands:
            values = list(_sample_words(
                operand.width, random.Random(operand.width)
            ))
            operand_probe_values.append((operand, values))
            replay_assignments.extend(
                operand.leaf_values(value) for value in values
            )
        replay_outputs = self.evaluate_batch(
            record,
            replay_assignments,
            max_depth=max_depth,
            blocked_producer_ids=blocked_producer_ids,
        )
        if replay_outputs[0] != 0:
            raise ValueError("generating operator has a nonzero affine baseline")
        operand_terms: list[
            tuple[SemanticWordRef, list[dict[str, Any]]]
        ] = []
        sample_rows = [{"inputs": {}, "source_full_output": 0}]
        replay_offset = 1
        for operand, probe_values in operand_probe_values:
            samples = []
            for value in probe_values:
                observed = replay_outputs[replay_offset] & (
                    (1 << word.width) - 1
                )
                replay_offset += 1
                samples.append((value, observed))
                sample_rows.append({
                    "inputs": {operand.key: value},
                    "source_full_output": observed,
                })
            recovered_terms = _infer_quantized_bus_terms({
                "stem": operand.key,
                "width": operand.width,
                "source_lsb": 0,
                "bits": [],
            }, samples, word.width)
            if recovered_terms is None or any(
                term["kind"] not in {"fixed_product_slice", "passthrough"}
                for term in recovered_terms
            ):
                full_term = _infer_full_bus_term({
                    "stem": operand.key,
                    "width": operand.width,
                    "source_lsb": 0,
                    "bits": [],
                }, samples, word.width)
                raise ValueError(
                    "logical operand is not one supported linear arithmetic term: "
                    f"width={operand.width} full_term={full_term!r}"
                )
            operand_terms.append((operand, recovered_terms))
        terms = [
            term
            for _, recovered_terms in operand_terms
            for term in recovered_terms
        ]
        product_terms = [
            term for term in terms if term["kind"] == "fixed_product_slice"
        ]
        if not product_terms and not allow_passthrough_only:
            raise ValueError("generating operator has no fixed-product term")
        normalized_by_operand = []
        for operand, recovered_terms in operand_terms:
            normalized_components = []
            for term in recovered_terms:
                normalized = {
                    "kind": str(term["kind"]),
                    "source": dict(term["source"]),
                    "data_width": int(term["data_width"]),
                    "data_signed": bool(term["data_signed"]),
                    "shift": int(term["shift"]),
                    "output_width": int(term["output_width"]),
                    "destination_lsb": 0,
                }
                if term["kind"] == "fixed_product_slice":
                    coefficient = int(term["coefficient"])
                    normalized.update({
                        "coefficient": coefficient,
                        "coefficient_width": max(
                            2, abs(coefficient).bit_length() + 1
                        ),
                    })
                normalized_components.append(normalized)
            normalized_by_operand.append((operand, normalized_components))
        ordered = sorted(
            normalized_by_operand,
            key=lambda row: (
                tuple(
                    (
                        str(term["kind"]),
                        int(term.get("coefficient", 1)),
                        int(term["shift"]),
                        bool(term["data_signed"]),
                    )
                    for term in row[1]
                ),
                row[0].width, row[0].source_lsb, row[0].key,
            ),
        )
        operands = tuple(row[0] for row in ordered)
        normalized_terms = [
            dict(term) for _, components in ordered for term in components
        ]
        term_operand_ordinals = [
            operand_ordinal
            for operand_ordinal, (_, components) in enumerate(ordered)
            for _ in components
        ]
        # Single-operand samples cannot reveal whether truncation happens
        # before or after two terms are added.  Pair screens recover those
        # quantization interactions; joint rows then replay the complete
        # expression.  This distinguishes floor(a)+floor(b) from floor(a+b)
        # without relying on RTL or hierarchy identity.
        pair_rng = random.Random(0x71A6E)
        replay_rows: list[dict[str, Any]] = []
        replay_leaf_values: list[Mapping[str, int]] = []
        joint_row_ordinals: list[int] = []
        for left in range(len(operands)):
            for right in range(left + 1, len(operands)):
                for _ in range(16):
                    values = {
                        operands[left].key: pair_rng.getrandbits(
                            operands[left].width
                        ),
                        operands[right].key: pair_rng.getrandbits(
                            operands[right].width
                        ),
                    }
                    leaf_values = {}
                    for operand in (operands[left], operands[right]):
                        leaf_values.update(
                            operand.leaf_values(values[operand.key])
                        )
                    replay_rows.append({"inputs": values})
                    replay_leaf_values.append(leaf_values)
        rng = random.Random(0x5E6A17C)
        for _ in range(512):
            values = {
                operand.key: rng.getrandbits(operand.width)
                for operand in operands
            }
            leaf_values = {}
            for operand in operands:
                leaf_values.update(operand.leaf_values(values[operand.key]))
            joint_row_ordinals.append(len(replay_rows))
            replay_rows.append({"inputs": values})
            replay_leaf_values.append(leaf_values)
        replay_results = self.evaluate_batch(
            record,
            replay_leaf_values,
            max_depth=max_depth,
            blocked_producer_ids=blocked_producer_ids,
        )
        for row, observed in zip(replay_rows, replay_results):
            row["source_full_output"] = observed
            sample_rows.append(row)
        joint_vectors = [replay_rows[ordinal] for ordinal in joint_row_ordinals]
        groups = _exact_quantization_groups(
            normalized_terms, sample_rows, word.width
        )
        if allow_source_folding and len(normalized_terms) == len(operands):
            folded_operands, fold_count = self._fold_correlated_quantization_operands(
                record,
                operands,
                groups,
                max_depth=max_depth,
                blocked_producer_ids=blocked_producer_ids,
            )
            if fold_count:
                plan, folded_operands = self._infer_generating_operator_for_operands(
                    record,
                    word=word,
                    operands=folded_operands,
                    module_name=module_name,
                    max_depth=max_depth,
                    allow_source_folding=False,
                    blocked_producer_ids=blocked_producer_ids,
                    allow_passthrough_only=allow_passthrough_only,
                )
                plan["recovery"]["correlated_quantization_operand_fold_count"] = (
                    fold_count
                )
                return plan, folded_operands
        # An unsigned passthrough shifted by at least its complete source width
        # is identically zero.  Keep it through pair/joint source evaluation so
        # interactions with every other operand are still screened, then
        # remove it from the executable IR and loss-boundary inventory.  A
        # zero-width loss slice is not an optimization term and must not reach
        # the production schema.
        retained_term_ordinals = [
            ordinal for ordinal, term in enumerate(normalized_terms)
            if not (
                term["kind"] == "passthrough"
                and not bool(term["data_signed"])
                and int(term["shift"]) >= int(term["data_width"])
            )
        ]
        if not retained_term_ordinals:
            raise ValueError("generating operator has no observable linear term")
        if len(retained_term_ordinals) != len(normalized_terms):
            retained_operands = sorted({
                term_operand_ordinals[ordinal]
                for ordinal in retained_term_ordinals
            })
            operand_remap = {
                old: new for new, old in enumerate(retained_operands)
            }
            term_remap = {
                old: new for new, old in enumerate(retained_term_ordinals)
            }
            normalized_terms = [
                normalized_terms[ordinal]
                for ordinal in retained_term_ordinals
            ]
            term_operand_ordinals = [
                operand_remap[term_operand_ordinals[ordinal]]
                for ordinal in retained_term_ordinals
            ]
            operands = tuple(operands[ordinal] for ordinal in retained_operands)
            groups = [
                {
                    **group,
                    "term_ordinals": [
                        term_remap[ordinal]
                        for ordinal in group["term_ordinals"]
                        if ordinal in term_remap
                    ],
                }
                for group in groups
                if any(ordinal in term_remap for ordinal in group["term_ordinals"])
            ]
        roles = [f"input_{ordinal}" for ordinal in range(len(operands))]
        for term, operand_ordinal in zip(
            normalized_terms, term_operand_ordinals
        ):
            term["source_role"] = roles[operand_ordinal]
        input_signed = []
        for operand_ordinal in range(len(operands)):
            signedness = {
                bool(term["data_signed"])
                for term, ordinal in zip(
                    normalized_terms, term_operand_ordinals
                )
                if ordinal == operand_ordinal
            }
            if len(signedness) != 1:
                raise ValueError(
                    "one semantic operand has inconsistent signed term views"
                )
            input_signed.append(next(iter(signedness)))
        input_widths = [int(operand.width) for operand in operands]
        coefficients = [
            int(term["coefficient"]) for term in normalized_terms
            if term["kind"] == "fixed_product_slice"
        ]
        coefficient_width = max(
            (
                int(term["coefficient_width"]) for term in normalized_terms
                if term["kind"] == "fixed_product_slice"
            ),
            default=0,
        )
        product_shift = max(
            (
                int(term["shift"]) for term in normalized_terms
                if term["kind"] == "fixed_product_slice"
            ),
            default=0,
        )

        mask = (1 << word.width) - 1
        for row in joint_vectors:
            values = row["inputs"]
            predicted = sum(
                _group_value(group, normalized_terms, values, word.width)
                for group in groups
            )
            observed = int(row["source_full_output"])
            if predicted & mask != observed:
                raise ValueError(
                    "normalized generating operator failed joint closure replay: "
                    f"terms={normalized_terms!r} groups={groups!r} "
                    f"predicted={predicted & mask} "
                    f"observed={observed}"
                )
        plan = {
            "version": 1,
            "kind": "generating_operator_ir",
            "operator_class": (
                "separately_quantized_constant_product_sum"
                if product_terms else "registered_linear_sum"
            ),
            "schedule_kind": "semantic_schedule_unbound_v1",
            "module_name": module_name,
            "data_width": word.width,
            "coefficient_width": coefficient_width,
            "product_shift": product_shift,
            "coefficients": coefficients,
            "input_signed": input_signed,
            "input_widths": input_widths,
            "input_roles": roles,
            "terms": [
                {
                    key: value for key, value in term.items()
                    if key != "source"
                }
                for term in normalized_terms
            ],
            "groups": [
                {
                    "term_ordinals": list(group["term_ordinals"]),
                    "common_shift": int(group["common_shift"]),
                    "output_width": int(group["output_width"]),
                }
                for group in groups
            ],
            "quantization": {
                "product_loss_boundary": "interaction_screened_term_groups",
                "accumulation": "group_shift_then_modular_group_sum",
                "output_width": word.width,
                "group_count": len(groups),
            },
            "recovery": {
                "semantic_operand_keys": [item.key for item in operands],
                "semantic_operand_slice_counts": [
                    len(item.slices) for item in operands
                ],
                "schedule_cut_depth": max_depth,
                "replay_vector_count": 512,
                "normalization": (
                    "explicit_product_residual_terms_and_interaction_groups"
                ),
                "external_plan_used": False,
            },
        }
        return plan, operands

    def infer_product_plan(
        self, record: ClosureRecord, *, module_name: str
    ) -> tuple[dict[str, Any], dict[str, WordSliceRef]]:
        # Enumerate schedule cuts rather than flattening through every retained
        # register.  A useful DSP replacement often lives at the first uniform
        # registered frontier; flattening farther can collapse two transaction
        # phases onto the same physical stem and destroy the temporal contract.
        seen_frontiers = set()
        failures = []
        group_size = sum(
            1 for item in self.records
            if item.root_stem == record.root_stem
            and item.slice_index == record.slice_index
        )
        for max_depth in range(1, group_size + 2):
            leaves, _ = self.leaf_slices(record, max_depth=max_depth)
            signature = tuple(sorted(
                (leaf.stem, leaf.source_lsb, leaf.width) for leaf in leaves.values()
            ))
            if signature in seen_frontiers:
                break
            seen_frontiers.add(signature)
            try:
                return self._infer_product_plan_at_depth(
                    record, module_name=module_name, max_depth=max_depth
                )
            except ValueError as exc:
                failures.append(f"depth={max_depth}:{exc}")
        raise ValueError(
            "no schedule-compatible registered product frontier: "
            + "; ".join(failures[-4:])
        )

    def _infer_product_plan_at_depth(
        self,
        record: ClosureRecord,
        *,
        module_name: str,
        max_depth: int,
    ) -> tuple[dict[str, Any], dict[str, WordSliceRef]]:
        word = record.recover()
        if word is None:
            raise ValueError("registered product candidate is not evaluable")
        leaves, depth_sets = self.leaf_slices(record, max_depth=max_depth)
        baseline = self.evaluate(record, {}, max_depth=max_depth)
        if baseline != 0:
            raise ValueError("registered product candidate has a nonzero affine baseline")
        terms = []
        for key, leaf in sorted(leaves.items()):
            samples = []
            for value in _sample_words(leaf.width, random.Random(leaf.width)):
                observed = self.evaluate(
                    record, {key: value}, max_depth=max_depth
                ) & (
                    (1 << word.width) - 1
                )
                samples.append((value, observed))
            bus = {
                "stem": key,
                "width": leaf.width,
                "source_lsb": 0,
                "bits": [],
            }
            term = _infer_bus_term(bus, samples, word.width)
            if term is None or term["kind"] != "fixed_product_slice":
                raise ValueError("composed leaf is not a supported fixed-point product")
            terms.append(term)
        if len(terms) < 2:
            raise ValueError("registered product candidate has fewer than two terms")
        terms.sort(key=lambda term: (
            int(term["coefficient"]),
            bool(term["data_signed"]),
            str(term["source"]["stem"]),
        ))
        shifts = {int(term["shift"]) for term in terms}
        widths = {int(term["data_width"]) for term in terms}
        if len(shifts) != 1 or len(widths) != 1 or next(iter(widths)) != word.width:
            raise ValueError("registered products do not share one word contract")
        rng = random.Random(0xF1A26)
        mask = (1 << word.width) - 1
        for _ in range(256):
            values = {key: rng.getrandbits(leaf.width) for key, leaf in leaves.items()}
            predicted = sum(
                _term_value(term, values[term["source"]["stem"]])
                for term in terms
            ) & mask
            if predicted != self.evaluate(
                record, values, max_depth=max_depth
            ):
                raise ValueError("registered product formula failed composed replay")
        coefficient_width = max(
            2,
            max(abs(int(term["coefficient"])).bit_length() + 1 for term in terms),
        )
        if coefficient_width > 27:
            raise ValueError("registered product coefficient exceeds DSP input contract")
        ordered_keys = [term["source"]["stem"] for term in terms]
        plan = {
            "version": 1,
            "schedule_kind": "registered_product_sum_v1",
            "module_name": module_name,
            "data_width": word.width,
            "coefficient_width": coefficient_width,
            "product_shift": next(iter(shifts)),
            "coefficients": [int(term["coefficient"]) for term in terms],
            "input_signed": [bool(term["data_signed"]) for term in terms],
            "input_roles": [f"input_{index}" for index in range(len(terms))],
            "recovery": {
                "leaf_keys": ordered_keys,
                "leaf_depths": [max(depth_sets[key]) for key in ordered_keys],
                "leaf_depth_ranges": [
                    [min(depth_sets[key]), max(depth_sets[key])] for key in ordered_keys
                ],
                "replay_vector_count": 256,
                "schedule_cut_depth": max_depth,
                "external_plan_used": False,
            },
        }
        return plan, {key: leaves[key] for key in ordered_keys}


def _one_object_name(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1].replace("\\}", "}").replace("\\\\", "\\")
    if not text or any(character in text for character in "\r\n\x00"):
        raise ValueError(f"{label} is absent or ambiguous")
    return text


def _stage_control(
    closure: RegisteredClosure, record: ClosureRecord
) -> dict[str, Any]:
    rows = sorted(record.graph.targets, key=lambda item: int(item["index"]))
    fields = {}
    for key in ("ref", "clock_net", "ce_net", "sr_net", "init"):
        values = {_one_object_name(row.get(key), key) for row in rows}
        if key in closure._proved_control_net_aliases:
            aliases = closure._proved_control_net_aliases[key]
            values = {aliases.get(value, value) for value in values}
        if len(values) != 1:
            raise ValueError(f"registered stage {key} is not uniform")
        fields[key] = next(iter(values))
    primitive = fields["ref"]
    semantics = {
        "FDRE": ("posedge", "sync_clear"),
        "FDRE_1": ("negedge", "sync_clear"),
        "FDCE": ("posedge", "async_clear"),
        "FDCE_1": ("negedge", "async_clear"),
    }.get(primitive)
    if semantics is None:
        raise ValueError("registered stage primitive is not a clear-to-zero FF")
    if fields["init"].lower() not in {"0", "1'b0", "1'h0"}:
        raise ValueError("registered stage has a nonzero initial state")
    return {
        "clock_edge": semantics[0],
        "reset_kind": semantics[1],
        "clock_net": fields["clock_net"],
        "ce_net": fields["ce_net"],
        "reset_net": fields["sr_net"],
    }


def _candidate_schedule(
    closure: RegisteredClosure,
    record: ClosureRecord,
    plan: dict[str, Any],
) -> dict[str, Any]:
    depth = int(plan["recovery"]["schedule_cut_depth"])
    paths = closure.leaf_paths(record, max_depth=depth)
    ordered_keys = list(plan["recovery"]["leaf_keys"])
    product_controls = []
    tail_controls: list[dict[str, Any]] | None = None
    clock_net = None
    clock_edge = None
    for key in ordered_keys:
        key_paths = paths.get(key) or []
        if not key_paths:
            raise ValueError("registered product leaf has no schedule path")
        schedules = []
        for path in key_paths:
            if len(path) != depth:
                raise ValueError("registered product leaf path depth is inconsistent")
            controls = [_stage_control(closure, item) for item in reversed(path)]
            schedules.append(controls)
        first = schedules[0]
        if any(schedule != first for schedule in schedules[1:]):
            raise ValueError("registered product leaf has ambiguous stage controls")
        for control in first:
            if clock_net is None:
                clock_net = control["clock_net"]
                clock_edge = control["clock_edge"]
            if (
                control["clock_net"] != clock_net
                or control["clock_edge"] != clock_edge
            ):
                raise ValueError("registered product schedule crosses clock domains")
        product_controls.append(first[0])
        current_tail = first[1:]
        if tail_controls is None:
            tail_controls = current_tail
        elif current_tail != tail_controls:
            raise ValueError("registered product terms do not share a tail schedule")
    assert tail_controls is not None and clock_net is not None
    return {
        "pipeline_depth": depth,
        "clock_edge": clock_edge,
        "product_reset_kinds": [item["reset_kind"] for item in product_controls],
        "tail_reset_kinds": [item["reset_kind"] for item in tail_controls],
        "clock_net": clock_net,
        "product_ce_nets": [item["ce_net"] for item in product_controls],
        "product_reset_nets": [item["reset_net"] for item in product_controls],
        "tail_ce_nets": [item["ce_net"] for item in tail_controls],
        "tail_reset_nets": [item["reset_net"] for item in tail_controls],
    }


def _boundary_ff_rows(
    closure: RegisteredClosure,
) -> dict[str, dict[str, str]]:
    """Index exact frontier-register facts by Q net across the closure."""
    cached = closure._boundary_ff_rows_cache
    if cached is not None:
        return cached
    fields = (
        "cell", "ref", "d_net", "q_net", "clock_net", "ce_net",
        "sr_net", "init", "graph_q_net",
    )
    result: dict[str, dict[str, str]] = {}
    for record in closure.records:
        path = record.graph.root / "boundary.tsv"
        with path.open("r", encoding="utf-8", newline="") as stream:
            for raw in csv.DictReader(stream, delimiter="\t"):
                row = {key: str(raw.get(key) or "") for key in fields}
                for control_field in ("clock_net", "ce_net", "sr_net"):
                    row[control_field] = closure._proved_control_net_aliases[
                        control_field
                    ].get(row[control_field], row[control_field])
                graph_q_net = row["graph_q_net"] or row["q_net"]
                prior = result.get(graph_q_net)
                if prior is not None and prior != row:
                    raise ValueError(
                        "registered frontier FF facts disagree across closure nodes"
                    )
                result[graph_q_net] = row
    closure._boundary_ff_rows_cache = result
    return result


def _frontier_operand_wiring(
    operand: SemanticWordRef,
    boundary_rows: Mapping[str, dict[str, str]],
) -> tuple[list[str], dict[str, Any]]:
    rows = []
    for net in operand.nets:
        row = boundary_rows.get(net)
        if row is None:
            raise ValueError("semantic operand is not an extracted register frontier")
        rows.append(row)
    uniform = {}
    for key in ("ref", "clock_net", "ce_net", "sr_net", "init"):
        values = {_one_object_name(row.get(key), key) for row in rows}
        if len(values) != 1:
            raise ValueError(f"semantic operand {key} is not uniform")
        uniform[key] = next(iter(values))
    primitive = uniform["ref"]
    semantics = {
        "FDRE": ("posedge", "sync_clear"),
        "FDRE_1": ("negedge", "sync_clear"),
        "FDCE": ("posedge", "async_clear"),
        "FDCE_1": ("negedge", "async_clear"),
    }.get(primitive)
    if semantics is None:
        raise ValueError("semantic operand register primitive is unsupported")
    if uniform["init"].lower() not in {"0", "1'b0", "1'h0"}:
        raise ValueError("semantic operand register has a nonzero initial state")
    return [
        _one_object_name(row.get("d_net"), "semantic operand D net")
        for row in rows
    ], {
        "clock_edge": semantics[0],
        "reset_kind": semantics[1],
        "clock_net": uniform["clock_net"],
        "ce_net": uniform["ce_net"],
        "reset_net": uniform["sr_net"],
    }


def _operand_wiring_at_cut(
    closure: RegisteredClosure,
    root: ClosureRecord,
    operand: SemanticWordRef,
    *,
    max_depth: int,
    boundary_rows: Mapping[str, dict[str, str]],
    blocked_producer_ids: frozenset[str] = frozenset(),
) -> tuple[list[str], dict[str, Any]]:
    """Bind an FF cut or a hard-macro data cut to an exact register schedule."""
    try:
        return _frontier_operand_wiring(operand, boundary_rows)
    except ValueError as register_error:
        rows = [boundary_rows.get(net) for net in operand.nets]
        if any(row is None for row in rows):
            raise register_error
        checked = [row for row in rows if row is not None]
        refs = {str(row.get("ref") or "").upper() for row in checked}
        if not refs or any(
            not ref.startswith(("RAMB", "URAM", "DSP")) for ref in refs
        ):
            raise register_error

        paths = closure.leaf_paths(
            root,
            max_depth=max_depth,
            blocked_producer_ids=blocked_producer_ids,
        )
        terminal_records = []
        for item in operand.slices:
            item_paths = paths.get(item.key) or []
            if not item_paths:
                raise ValueError("primitive operand has no register schedule path")
            terminal_records.extend(path[-1] for path in item_paths)
        controls = [
            _stage_control(closure, record) for record in terminal_records
        ]
        if not controls:
            raise ValueError("primitive operand has no register schedule")
        if all(control == controls[0] for control in controls[1:]):
            return list(operand.nets), controls[0]

        # The raw word may feed both an input-capture register and a derived
        # residual/product register.  Only the exact width-preserving,
        # shift-zero passthrough defines the bridge input schedule; product
        # stage controls are recovered separately by the accumulator contract.
        capture_controls = []
        seen_records = set()
        for terminal in terminal_records:
            if terminal.node_id in seen_records:
                continue
            seen_records.add(terminal.node_id)
            word = terminal.recover()
            if word is None or len(word.buses) != 1:
                continue
            bus = word.buses[0]
            if bus.width != operand.width or word.width != operand.width:
                continue
            samples = []
            for value in _sample_words(
                bus.width, random.Random(bus.width ^ word.width)
            ):
                samples.append((value, word.evaluate({bus.key: value})))
            term = _infer_bus_term({
                "stem": bus.stem,
                "width": bus.width,
                "source_lsb": 0,
                "bits": [],
            }, samples, word.width)
            if (
                term is not None
                and term.get("kind") == "passthrough"
                and int(term.get("shift", -1)) == 0
                and int(term.get("data_width", -1)) == operand.width
                and int(term.get("output_width", -1)) == operand.width
            ):
                capture_controls.append(_stage_control(closure, terminal))
        if (
            capture_controls
            and all(
                control == capture_controls[0]
                for control in capture_controls[1:]
            )
        ):
            return list(operand.nets), capture_controls[0]
        raise ValueError("primitive operand input-capture schedule is ambiguous")


def _merge_semantic_data_aliases(
    operands: tuple[SemanticWordRef, ...],
    boundary_rows: Mapping[str, dict[str, str]],
) -> tuple[SemanticWordRef, ...]:
    """Merge registered aliases that sample the same complete D/CE word.

    Synthesis may retain several register words for different reset shells
    while feeding them from exactly the same D vector on the same clock/CE.
    Treating their Q states as independent during active operator recovery
    creates unreachable counterexamples and hides the generating multiply.
    Every alias slice remains in ``slices`` so closure replay drives all
    occurrences together; one representative supplies the identical D wiring.
    """
    groups: dict[tuple[Any, ...], list[SemanticWordRef]] = {}
    order = []
    for operand in operands:
        d_nets, control = _frontier_operand_wiring(operand, boundary_rows)
        key = (
            operand.source_lsb, operand.width, tuple(d_nets), control["clock_edge"],
            control["clock_net"], control["ce_net"],
        )
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(operand)
    merged = []
    for key in order:
        aliases = groups[key]
        representative = aliases[0]
        slices = []
        seen = set()
        for alias in aliases:
            for item in alias.slices:
                signature = (
                    item.stem, item.source_lsb, item.width, item.nets
                )
                if signature in seen:
                    continue
                seen.add(signature)
                slices.append(item)
        merged.append(SemanticWordRef(
            stem=representative.stem,
            source_lsb=representative.source_lsb,
            width=representative.width,
            nets=representative.nets,
            slices=tuple(slices),
        ))
    return tuple(merged)


def _infer_output_data_gate(record: ClosureRecord) -> tuple[str, str]:
    """Prove one scalar root predicate selects data versus an all-zero write."""
    word = record.recover()
    if word is None or len(word.controls) != 1:
        raise ValueError("generating operator output does not have one scalar data gate")
    gate = word.controls[0]
    active = dict(word.active_controls)
    inactive = dict(active)
    inactive[gate] = not bool(active[gate])
    rng = random.Random(0x6A7E)
    active_nonzero = False
    for _ in range(128):
        values = {bus.key: rng.getrandbits(bus.width) for bus in word.buses}
        observed = int(word.evaluator(values, active))
        suppressed = int(word.evaluator(values, inactive))
        active_nonzero = active_nonzero or observed != 0
        if suppressed != 0:
            raise ValueError("generating operator output predicate is not a zero gate")
    if not active_nonzero:
        raise ValueError("generating operator output gate has no data response")
    return gate, "valid_high" if bool(active[gate]) else "invalid_high"


def _lut_shell_value(row: Mapping[str, str], values: Mapping[str, bool]) -> int:
    match = re.fullmatch(r"LUT([1-6])", str(row.get("shell_ref") or ""))
    init_match = re.fullmatch(
        r"(\d+)'h([0-9a-fA-F]+)", str(row.get("shell_init") or "")
    )
    if match is None or init_match is None or row.get("output_pin") != "O":
        raise ValueError("registered output shell LUT schema is unsupported")
    size = int(match.group(1))
    if int(init_match.group(1)) != 1 << size:
        raise ValueError("registered output shell LUT INIT width is invalid")
    address = 0
    for index in range(size):
        net = str(row.get(f"i{index}_net") or "")
        ref = str(row.get(f"i{index}_driver_ref") or "")
        if ref == "GND":
            value = False
        elif ref == "VCC":
            value = True
        elif net in values:
            value = bool(values[net])
        else:
            raise ValueError("registered output shell has an unresolved LUT input")
        if value:
            address |= 1 << index
    return (int(init_match.group(2), 16) >> address) & 1


def _infer_registered_output_shell(
    closure: RegisteredClosure,
    root: ClosureRecord,
) -> dict[str, Any]:
    """Recover a bypass/hold/zero shell after one registered arithmetic word."""
    path = closure.root / "output_shell.tsv"
    if not path.is_file():
        raise ValueError("registered output shell facts are missing")
    with path.open("r", encoding="utf-8", newline="") as stream:
        all_rows = list(csv.DictReader(stream, delimiter="\t"))
    start = min(
        int(item.get("source_index", item["index"]))
        for item in root.graph.targets
    )
    stop = start + len(root.graph.targets)
    rows = sorted((
        row for row in all_rows
        if row.get("root_stem") == root.root_stem
        and start <= int(row.get("index", -1)) < stop
    ), key=lambda row: int(row["index"]))
    if len(rows) != len(root.graph.targets):
        raise ValueError("registered output shell does not cover the whole word")
    common_dynamic: set[str] | None = None
    for row in rows:
        q_net = str(row.get("root_q_net") or "")
        d_net = str(row.get("root_d_net") or "")
        dynamic = {
            str(row.get(f"i{index}_net") or "")
            for index in range(6)
            if str(row.get(f"i{index}_net") or "")
            and str(row.get(f"i{index}_driver_ref") or "") not in {"GND", "VCC"}
        } - {q_net, d_net}
        common_dynamic = dynamic if common_dynamic is None else common_dynamic & dynamic
    candidates = sorted(common_dynamic or set())
    proved = []
    for gate in candidates:
        for hold in candidates:
            if hold == gate:
                continue
            for active_high in (False, True):
                valid = True
                for row in rows:
                    q_net = str(row["root_q_net"])
                    d_net = str(row["root_d_net"])
                    allowed = {q_net, d_net, gate, hold}
                    for index in range(6):
                        net = str(row.get(f"i{index}_net") or "")
                        ref = str(row.get(f"i{index}_driver_ref") or "")
                        if net and ref not in {"GND", "VCC"} and net not in allowed:
                            valid = False
                    for q_value, d_value, gate_value, hold_value in product(
                        (False, True), repeat=4
                    ):
                        expected = (
                            q_value if hold_value
                            else d_value if gate_value == active_high
                            else False
                        )
                        observed = _lut_shell_value(row, {
                            q_net: q_value,
                            d_net: d_value,
                            gate: gate_value,
                            hold: hold_value,
                        })
                        if observed != int(expected):
                            valid = False
                            break
                    if not valid:
                        break
                if valid:
                    proved.append((gate, hold, active_high))
    if len(proved) != 1:
        raise ValueError(
            "registered output shell semantic pattern is ambiguous: "
            f"candidate_count={len(proved)} candidates={proved!r}"
        )
    gate, hold, active_high = proved[0]
    output_nets = [
        _one_object_name(row.get("output_net"), "registered shell output net")
        for row in rows
    ]
    return {
        "kind": "registered_bypass_hold_zero_gate_v1",
        "gate_net": gate,
        "hold_net": hold,
        "gate_polarity": "valid_high" if active_high else "invalid_high",
        "output_nets": output_nets,
        "bit_count": len(rows),
        "truth_table_replay_count": len(rows) * 16,
    }


def _infer_observable_output_mux_shell(
    closure: RegisteredClosure,
    root: ClosureRecord,
) -> dict[str, Any]:
    """Recover an exact per-bit shell around a registered arithmetic word.

    A hierarchy output may select the recovered word or one independent data
    arm rather than merely data/zero.  The old four-signal pattern matcher
    rejected that topology and forced a D-pin-only replacement, which retained
    the expensive original cone.  Here scalar controls are identified by
    whole-word coverage (including exact replicated-FF aliases), the one
    remaining per-bit signal becomes an alternate data word, and every LUT INIT
    is exhaustively replayed.  No hierarchy or benchmark identity participates.
    """
    path = closure.root / "output_shell.tsv"
    if not path.is_file():
        raise ValueError("registered output shell facts are missing")
    with path.open("r", encoding="utf-8", newline="") as stream:
        all_rows = list(csv.DictReader(stream, delimiter="\t"))
    start = min(
        int(item.get("source_index", item["index"]))
        for item in root.graph.targets
    )
    stop = start + len(root.graph.targets)
    rows = sorted((
        row for row in all_rows
        if row.get("root_stem") == root.root_stem
        and start <= int(row.get("index", -1)) < stop
    ), key=lambda row: int(row["index"]))
    if len(rows) != len(root.graph.targets):
        raise ValueError("observable output shell does not cover the whole word")

    boundary = _boundary_ff_rows(closure)

    def logical_key(net: str, ref: str) -> tuple[Any, ...]:
        item = boundary.get(net)
        if item is None or not item.get("d_net"):
            return ("exact_net", ref, net)
        return (
            "registered_alias", item["ref"], item["d_net"],
            item["clock_net"], item["ce_net"], item["sr_net"], item["init"],
        )

    per_row: list[list[tuple[tuple[Any, ...], str, str]]] = []
    coverage: dict[tuple[Any, ...], dict[int, str]] = {}
    refs: dict[tuple[Any, ...], set[str]] = {}
    for row_ordinal, row in enumerate(rows):
        q_net = str(row.get("root_q_net") or "")
        dynamic = []
        for index in range(6):
            net = str(row.get(f"i{index}_net") or "")
            ref = str(row.get(f"i{index}_driver_ref") or "")
            if not net or ref in {"GND", "VCC"} or net == q_net:
                continue
            key = logical_key(net, ref)
            if row_ordinal in coverage.setdefault(key, {}):
                raise ValueError("observable shell repeats one logical input in a bit")
            coverage[key][row_ordinal] = net
            refs.setdefault(key, set()).add(ref)
            dynamic.append((key, net, ref))
        per_row.append(dynamic)

    control_keys = [
        key for key, members in coverage.items() if len(members) == len(rows)
    ]
    control_keys.sort(key=lambda key: (
        -len(set(coverage[key].values())),
        tuple(sorted(refs[key])),
        repr(key),
    ))
    control_key_set = set(control_keys)
    alternate_nets = []
    alternate_count: int | None = None
    for dynamic in per_row:
        remaining = [net for key, net, _ in dynamic if key not in control_key_set]
        if alternate_count is None:
            alternate_count = len(remaining)
        if len(remaining) != alternate_count or len(remaining) > 1:
            raise ValueError(
                "observable shell does not have one consistent alternate data arm"
            )
        if remaining:
            alternate_nets.append(remaining[0])
    has_alternate = bool(alternate_count)
    if has_alternate and len(set(alternate_nets)) != len(rows):
        raise ValueError("observable shell alternate word reuses one bit net")

    control_roles = [
        f"shell_control_{ordinal}" for ordinal in range(len(control_keys))
    ]
    control_nets = [
        min(coverage[key].values()) for key in control_keys
    ]
    variable_count = 1 + int(has_alternate) + len(control_keys)
    if variable_count > 6:
        raise ValueError("observable shell exceeds one LUT truth-table boundary")
    truth_tables = []
    root_influence = False
    alternate_influence = not has_alternate
    for row_ordinal, row in enumerate(rows):
        table = 0
        q_net = str(row.get("root_q_net") or "")
        alternate_net = alternate_nets[row_ordinal] if has_alternate else ""
        for assignment in product((False, True), repeat=variable_count):
            values = {q_net: assignment[0]}
            cursor = 1
            if has_alternate:
                values[alternate_net] = assignment[cursor]
                cursor += 1
            for key, value in zip(control_keys, assignment[cursor:]):
                for net in set(coverage[key].values()):
                    values[net] = value
            observed = _lut_shell_value(row, values)
            address = sum(
                int(value) << index for index, value in enumerate(assignment)
            )
            table |= int(observed) << address
        truth_tables.append(table)
        other_count = variable_count - 1
        for other in product((False, True), repeat=other_count):
            def lookup(root_value: bool, alternate_value: bool = False) -> int:
                values = [root_value]
                if has_alternate:
                    values.append(alternate_value)
                    values.extend(other[1:])
                else:
                    values.extend(other)
                address = sum(
                    int(value) << index for index, value in enumerate(values)
                )
                return (table >> address) & 1

            if lookup(False, other[0] if has_alternate else False) != lookup(
                True, other[0] if has_alternate else False
            ):
                root_influence = True
            if has_alternate:
                controls = other[1:]
                for root_value in (False, True):
                    low = [root_value, False, *controls]
                    high = [root_value, True, *controls]
                    low_address = sum(
                        int(value) << index for index, value in enumerate(low)
                    )
                    high_address = sum(
                        int(value) << index for index, value in enumerate(high)
                    )
                    if ((table >> low_address) & 1) != ((table >> high_address) & 1):
                        alternate_influence = True
    if not root_influence or not alternate_influence:
        raise ValueError("observable shell does not expose both proved data arms")
    output_nets = [
        _one_object_name(row.get("output_net"), "observable shell output net")
        for row in rows
    ]
    return {
        "kind": "registered_observable_mux_truth_table_v1",
        "control_roles": control_roles,
        "control_nets": control_nets,
        "has_alternate_data": has_alternate,
        "alternate_data_nets": alternate_nets,
        "truth_tables": truth_tables,
        "output_nets": output_nets,
        "bit_count": len(rows),
        "truth_table_replay_count": len(rows) * (1 << variable_count),
    }


def _infer_root_control_shell(record: ClosureRecord) -> dict[str, Any]:
    """Prove the root D function is a per-bit function of data and controls.

    Some operators write the recovered arithmetic directly into an existing
    destination register and retain a more complicated downstream consumer.
    In that topology the safe cut is the destination D net: keep the original
    register and every downstream LUT, and replace only its active arithmetic
    value.  The retained D boundary may mask, invert, or toggle individual
    arithmetic bits under scalar controls.  Recover the complete per-bit truth
    table over ``(active arithmetic bit, controls)`` and reject any phase in
    which an output bit depends on more of the original data cone.
    """
    word = record.recover()
    if word is None:
        raise ValueError("registered root control shell is not evaluable")
    groups = word.control_groups or tuple((net,) for net in word.controls)
    if len(groups) > 5:
        raise ValueError("registered root has too many control predicates")
    controls = tuple(group[0] for group in groups)

    def physical_controls(assignment: tuple[bool, ...]) -> dict[str, bool]:
        return {
            net: bool(value)
            for group, value in zip(groups, assignment)
            for net in group
        }

    active_assignment = []
    for group in groups:
        values = {bool(word.active_controls[net]) for net in group}
        if len(values) != 1:
            raise ValueError("registered control aliases disagree in active phase")
        active_assignment.append(next(iter(values)))
    samples = []
    zero = {bus.key: 0 for bus in word.buses}
    samples.append(zero)
    for bus in word.buses:
        for bit in range(bus.width):
            values = dict(zero)
            values[bus.key] = 1 << bit
            samples.append(values)
    rng = random.Random(0xC017A01)
    for _ in range(192):
        samples.append({
            bus.key: rng.getrandbits(bus.width) for bus in word.buses
        })
    active_outputs = [
        int(word.evaluator(values, word.active_controls)) for values in samples
    ]
    toggles = [
        {bool((value >> bit) & 1) for value in active_outputs}
        for bit in range(word.width)
    ]
    if any(states != {False, True} for states in toggles):
        raise ValueError(
            "registered root control shell has an unexercised arithmetic bit"
        )
    truth_tables = [0] * word.width
    preserved_data_bits = [False] * word.width
    replay_count = 0
    for assignment in product((False, True), repeat=len(controls)):
        control_values = physical_controls(assignment)
        observed_outputs = [
            int(word.evaluator(values, control_values)) for values in samples
        ]
        address = sum(int(value) << index for index, value in enumerate(assignment))
        for bit in range(word.width):
            active_bits = [bool((value >> bit) & 1) for value in active_outputs]
            observed_bits = [bool((value >> bit) & 1) for value in observed_outputs]
            mapping: dict[bool, bool] = {}
            for active_bit, observed_bit in zip(active_bits, observed_bits):
                prior = mapping.get(active_bit)
                if prior is not None and prior != observed_bit:
                    preserved_data_bits[bit] = True
                    break
                mapping[active_bit] = observed_bit
            if preserved_data_bits[bit]:
                continue
            if set(mapping) != {False, True}:
                preserved_data_bits[bit] = True
                continue
            for active_bit, observed_bit in mapping.items():
                table_address = int(active_bit) | (address << 1)
                truth_tables[bit] |= int(observed_bit) << table_address
            replay_count += len(samples)
    active_address = sum(
        int(value) << index for index, value in enumerate(active_assignment)
    )
    if all(preserved_data_bits):
        raise ValueError(
            "registered root control shell preserves every data bit"
        )
    for bit, table in enumerate(truth_tables):
        if preserved_data_bits[bit]:
            continue
        low = (table >> (active_address << 1)) & 1
        high = (table >> (1 | (active_address << 1))) & 1
        if (low, high) != (0, 1):
            raise ValueError(
                "registered root active controls do not expose arithmetic"
            )
    rows = sorted(record.graph.targets, key=lambda item: int(item["index"]))
    if len(rows) != word.width:
        raise ValueError("registered root D boundary does not cover the word")
    return {
        "kind": "preserve_target_register_control_truth_table_v2",
        "control_nets": list(controls),
        "control_roles": [f"control_{index}" for index in range(len(controls))],
        "control_truth_table_includes_data_bit": True,
        "control_truth_tables": truth_tables,
        "control_preserved_data_bits": preserved_data_bits,
        "output_nets": [
            _one_object_name(row.get("d_net"), "registered root D net")
            for row in rows
        ],
        "truth_table_replay_count": replay_count,
    }


def _semantic_accumulator_control(
    closure: RegisteredClosure,
    root: ClosureRecord,
    plan: dict[str, Any],
    *,
    allow_preserved_target_schedule: bool = False,
) -> dict[str, Any]:
    """Recover the registered partial-sum stage subsumed by the DSP bridge."""
    def signature(term: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            str(term["kind"]), int(term["data_width"]),
            bool(term["data_signed"]), int(term["shift"]),
            int(term["output_width"]), int(term.get("coefficient", 1)),
        )

    wanted = [signature(term) for term in plan["terms"]]
    candidates = []
    word = root.recover()
    if word is None:
        raise ValueError("generating operator root is not evaluable")
    direct_producers = []
    for bus in word.buses:
        if bus.width != word.width:
            direct_producers = []
            break
        producer = closure.resolve_producer(root, bus)
        if producer is None:
            direct_producers = []
            break
        direct_producers.append(producer)
    if direct_producers:
        direct_controls = [
            _stage_control(closure, item) for item in direct_producers
        ]
        if all(control == direct_controls[0] for control in direct_controls[1:]):
            # When every complete-word input to the observable root is a
            # registered state with one identical clock/CE/reset contract,
            # that contract is already the exact accumulator schedule.  A
            # recursive semantic search of each producer cannot add schedule
            # evidence and used to repeat the expensive FIR recovery several
            # times per lane.
            return direct_controls[0]
    for bus in word.buses:
        if bus.width != word.width:
            continue
        producer = closure.resolve_producer(root, bus)
        if producer is None:
            continue
        try:
            partial, _ = closure.infer_generating_operator_ir(
                producer, module_name="semantic_partial_sum"
            )
        except ValueError:
            continue
        remaining = list(wanted)
        matched = True
        for item in (signature(term) for term in partial["terms"]):
            try:
                remaining.remove(item)
            except ValueError:
                matched = False
                break
        if not matched or len(remaining) >= len(wanted):
            continue
        candidates.append((len(partial["terms"]), producer))
    if not candidates:
        if allow_preserved_target_schedule:
            # ``preserve_target_register_v1`` leaves the observable register
            # and its priority/control shell in place and replaces only its D
            # function.  The root's recovered stage contract is therefore the
            # exact clock/CE/reset schedule for the bridge accumulator.  This
            # is not valid for a downstream-shell replacement, where a
            # distinct registered partial sum must still be found above.
            return _stage_control(closure, root)
        raise ValueError("generating operator has no registered semantic partial sum")
    _, producer = max(candidates, key=lambda item: (item[0], item[1].node_id))
    return _stage_control(closure, producer)


def _primitive_data_aliases(
    boundary_rows: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    """Map every routed net alias to its opaque primitive output pin.

    A BRAM word is often split across several physical output buses and net
    segments.  Net or hierarchy names therefore cannot establish that two
    registered expressions sampled the same word.  The closure extractor
    records the driving primitive output pin in ``cell`` and all routed aliases
    in ``d_net``; that physical identity is the narrow proof used here.
    """
    aliases: dict[str, str] = {}
    for row in boundary_rows.values():
        if str(row.get("ref") or "").startswith("FD"):
            continue
        source = str(row.get("cell") or "")
        if not source:
            continue
        for alias in str(row.get("d_net") or "").split("|"):
            if not alias:
                continue
            prior = aliases.get(alias)
            if prior is not None and prior != source:
                raise ValueError(
                    "primitive data alias maps to multiple output pins"
                )
            aliases[alias] = source
    return aliases


def _matching_operand_record(
    closure: RegisteredClosure,
    root: ClosureRecord,
    operand: SemanticWordRef,
) -> ClosureRecord | None:
    matches = []
    for record in closure.records:
        if (
            record.root_stem != root.root_stem
            or record.slice_index != root.slice_index
            or record.graph.manifest.get("target_stem") != operand.stem
        ):
            continue
        indices = [
            int(row.get("source_index", row["index"]))
            for row in record.graph.targets
        ]
        if (
            len(indices) == operand.width
            and min(indices) == operand.source_lsb
            and max(indices) == operand.source_lsb + operand.width - 1
        ):
            matches.append(record)
    if len(matches) != 1:
        return None
    return matches[0]


def _exact_source_residual_candidates(
    closure: RegisteredClosure,
    root: ClosureRecord,
    plan: Mapping[str, Any],
    operands: tuple[SemanticWordRef, ...],
) -> list[tuple[int, int, dict[str, Any], ClosureRecord]]:
    """Return structurally eligible residual/product pairs.

    This screen is intentionally cheap.  Every returned pair must still pass
    physical-source identity, product recovery, range, and vector replay in
    ``_fold_exact_source_residual_candidate`` before it can change a plan.
    """
    terms = list(plan.get("terms") or [])
    groups = list(plan.get("groups") or [])
    candidates: list[tuple[int, int, dict[str, Any], ClosureRecord]] = []
    for residual_index, residual in enumerate(terms):
        if residual.get("kind") != "passthrough":
            continue
        matching_groups = [
            group for group in groups
            if residual_index in {
                int(index) for index in group.get("term_ordinals") or []
            }
        ]
        if len(matching_groups) != 1:
            continue
        group = matching_groups[0]
        members = [int(index) for index in group["term_ordinals"]]
        product_indices = [
            index for index in members
            if terms[index].get("kind") == "fixed_product_slice"
        ]
        if len(members) != 2 or len(product_indices) != 1:
            continue
        producer = _matching_operand_record(
            closure, root, operands[residual_index]
        )
        if producer is None or producer.recover() is None:
            continue
        candidates.append((
            residual_index, product_indices[0], group, producer,
        ))
    return candidates


def _fold_exact_source_residual_candidate(
    closure: RegisteredClosure,
    root: ClosureRecord,
    plan: dict[str, Any],
    operands: tuple[SemanticWordRef, ...],
    input_rows: list[tuple[list[str], dict[str, Any]]],
    boundary_rows: Mapping[str, Mapping[str, str]],
    candidate: tuple[int, int, dict[str, Any], ClosureRecord],
    rejections: list[dict[str, Any]] | None = None,
) -> tuple[
    dict[str, Any],
    tuple[SemanticWordRef, ...],
    list[tuple[list[str], dict[str, Any]]],
    dict[str, Any] | None,
]:
    """Prove and fold one retained residual back into its source product.

    This deliberately targets the currently observed incomplete-cut shape.  A
    passthrough may be folded only when (1) it shares one exact quantization
    group with one product, (2) its producing D cone is an exactly recovered
    fixed product of the same bitwise frontier-D word (or, after routed net
    segmentation, the same physical primitive output pins), and (3) that
    product cannot overflow the retained residual word.  No coefficient,
    hierarchy, design identity, lane count, or placement is supplied.
    """
    terms = list(plan.get("terms") or [])
    residual_index, product_index, group, producer = candidate

    def reject(reason: str) -> tuple[
        dict[str, Any],
        tuple[SemanticWordRef, ...],
        list[tuple[list[str], dict[str, Any]]],
        None,
    ]:
        if rejections is not None:
            rejections.append({
                "residual_index": residual_index,
                "product_index": product_index,
                "producer_node_id": producer.node_id,
                "reason": reason,
            })
        return plan, operands, input_rows, None

    residual_word = producer.recover()
    assert residual_word is not None
    product_operand = operands[product_index]
    product_d_nets = list(input_rows[product_index][0])
    if (
        len(product_d_nets) != product_operand.width
        or len(set(product_d_nets)) != len(product_d_nets)
    ):
        return reject("product input does not cover one unique frontier-D word")
    bus_positions: dict[str, list[int]] = {}
    used_positions: list[int] = []
    direct_positions = {
        net: index for index, net in enumerate(product_d_nets)
    }
    direct_match = all(
        net in direct_positions
        for bus in residual_word.buses
        for net in bus.nets
    )
    source_proof = "same_frontier_d_net"
    if direct_match:
        for bus in residual_word.buses:
            positions = [direct_positions[net] for net in bus.nets]
            bus_positions[bus.key] = positions
            used_positions.extend(positions)
        product_sources = product_d_nets
    else:
        aliases = _primitive_data_aliases(boundary_rows)
        product_sources = []
        for net in product_d_nets:
            source = aliases.get(net)
            if source is None:
                return reject(
                    "product input net has neither an exact residual-D "
                    "match nor a primitive-source alias"
                )
            product_sources.append(source)
        if len(set(product_sources)) != len(product_sources):
            return reject(
                "product input does not cover unique primitive source pins"
            )
        source_positions = {
            source: index for index, source in enumerate(product_sources)
        }
        for bus in residual_word.buses:
            positions = []
            for net in bus.nets:
                row = boundary_rows.get(net)
                source = "" if row is None else str(row.get("cell") or "")
                position = source_positions.get(source)
                if position is None:
                    return reject(
                        "residual D cone uses a different primitive source"
                    )
                positions.append(position)
                used_positions.append(position)
            bus_positions[bus.key] = positions
        source_proof = "same_primitive_output_pin"
    if sorted(used_positions) != list(range(product_operand.width)):
        return reject("residual D cone does not cover the complete source word")

    def residual_value(value: int) -> int:
        word_inputs = {}
        for bus in residual_word.buses:
            assembled = 0
            for bit, position in enumerate(bus_positions[bus.key]):
                assembled |= ((int(value) >> position) & 1) << bit
            word_inputs[bus.key] = assembled
        return residual_word.evaluate(word_inputs)

    samples = [
        (value, residual_value(value))
        for value in _sample_words(
            product_operand.width,
            random.Random(0xF01D5EED ^ product_operand.width),
        )
    ]
    recovered = _infer_full_bus_term({
        "stem": "physical_source_word",
        "width": product_operand.width,
        "source_lsb": 0,
        "bits": [],
    }, samples, residual_word.width)
    if (
        recovered is None
        or recovered.get("kind") != "fixed_product_slice"
        or int(recovered.get("destination_lsb", -1)) != 0
        or int(recovered.get("shift", -1)) != 0
        or int(recovered.get("data_width", -1)) != product_operand.width
    ):
        return reject("residual D cone is not one exact unshifted fixed product")

    # The residual is consumed as a signed retained state.  Folding across a
    # wrapping residual would change semantics, so require its exact product
    # range to fit the recovered word before touching the bridge.
    coefficient = int(recovered["coefficient"])
    data_signed = bool(recovered["data_signed"])
    if data_signed:
        low = -(1 << (product_operand.width - 1))
        high = (1 << (product_operand.width - 1)) - 1
        products = (low * coefficient, high * coefficient)
        minimum, maximum = min(products), max(products)
        if not (
            -(1 << (residual_word.width - 1)) <= minimum
            and maximum < (1 << (residual_word.width - 1))
        ):
            return reject("signed residual product can overflow its retained word")
    else:
        maximum = ((1 << product_operand.width) - 1) * coefficient
        if coefficient < 0 or not 0 <= maximum < (1 << residual_word.width):
            return reject("unsigned residual product can overflow its retained word")

    replay_rng = random.Random(0xC105ED)
    replay_values = [
        replay_rng.getrandbits(product_operand.width) for _ in range(512)
    ]
    residual_mask = (1 << residual_word.width) - 1
    if any(
        residual_value(value)
        != (
            _term_value(recovered, value)
            << int(recovered["destination_lsb"])
        ) & residual_mask
        for value in replay_values
    ):
        return reject("residual product failed 512-vector exact replay")

    residual = terms[residual_index]
    product_term = terms[product_index]
    if (
        bool(residual.get("data_signed")) is not data_signed
        or int(residual.get("data_width", -1)) != residual_word.width
        or bool(product_term.get("data_signed")) is not data_signed
        or int(product_term.get("data_width", -1))
        != product_operand.width
    ):
        return reject("residual/product signedness or source width disagrees")
    common_shift = int(group["common_shift"])
    product_shift = int(product_term["shift"])
    residual_shift = int(residual["shift"])
    if product_shift > common_shift or residual_shift > common_shift:
        return reject("residual/product shift exceeds the quantization group")
    combined = (
        int(product_term["coefficient"])
        << (common_shift - product_shift)
    ) + (coefficient << (common_shift - residual_shift))
    coefficient_width = max(2, abs(combined).bit_length() + 1)
    if coefficient_width > 27:
        return reject("combined coefficient exceeds the supported DSP width")

    updated = copy.deepcopy(plan)
    updated_terms = list(updated["terms"])
    updated_product = updated_terms[product_index]
    updated_product["coefficient"] = combined
    updated_product["coefficient_width"] = coefficient_width
    updated_product["shift"] = common_shift

    def remap(index: int) -> int:
        return index - int(index > residual_index)

    updated["terms"] = [
        term for index, term in enumerate(updated_terms)
        if index != residual_index
    ]
    updated_groups = []
    for raw_group in updated["groups"]:
        item = copy.deepcopy(raw_group)
        item["term_ordinals"] = [
            remap(int(index)) for index in item["term_ordinals"]
            if int(index) != residual_index
        ]
        if item["term_ordinals"]:
            updated_groups.append(item)
    updated["groups"] = updated_groups
    for key in ("input_roles", "input_widths", "input_signed"):
        del updated[key][residual_index]
    product_terms = [
        term for term in updated["terms"]
        if term["kind"] == "fixed_product_slice"
    ]
    updated["coefficients"] = [
        int(term["coefficient"]) for term in product_terms
    ]
    updated["coefficient_width"] = max(
        int(term["coefficient_width"]) for term in product_terms
    )
    updated["product_shift"] = max(
        int(term["shift"]) for term in product_terms
    )
    updated.setdefault("recovery", {})[
        "exact_source_residual_fold_count"
    ] = 1
    updated_operands = tuple(
        operand for index, operand in enumerate(operands)
        if index != residual_index
    )
    updated_rows = [
        row for index, row in enumerate(input_rows)
        if index != residual_index
    ]
    return updated, updated_operands, updated_rows, {
        "residual_coefficient": coefficient,
        "combined_coefficient": combined,
        "physical_source_pin_count": len(product_sources),
        "producer_node_id": producer.node_id,
        "source_identity_proof": source_proof,
        "proof": "same_source_exact_product_replay_no_overflow",
    }


def _fold_one_exact_source_residual(
    closure: RegisteredClosure,
    root: ClosureRecord,
    plan: dict[str, Any],
    operands: tuple[SemanticWordRef, ...],
    input_rows: list[tuple[list[str], dict[str, Any]]],
    boundary_rows: Mapping[str, Mapping[str, str]],
) -> tuple[
    dict[str, Any],
    tuple[SemanticWordRef, ...],
    list[tuple[list[str], dict[str, Any]]],
    dict[str, Any] | None,
]:
    """Fold every independently proved source residual in the operator.

    Quantization groups partition the recovered expression, so a successful
    fold is committed one at a time and the candidates are then rediscovered
    against the remapped term/operand indices.  An unproved pair is skipped;
    it never prevents another independent pair from being tested.
    """
    updated_plan = plan
    updated_operands = operands
    updated_rows = input_rows
    proofs: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    rejected: set[tuple[str, str, str, int]] = set()
    while True:
        progress = False
        candidates = _exact_source_residual_candidates(
            closure, root, updated_plan, updated_operands
        )
        for candidate in candidates:
            residual_index, product_index, group, producer = candidate
            key = (
                producer.node_id,
                updated_operands[residual_index].key,
                updated_operands[product_index].key,
                int(group["common_shift"]),
            )
            if key in rejected:
                continue
            candidate_plan, candidate_operands, candidate_rows, proof = (
                _fold_exact_source_residual_candidate(
                    closure,
                    root,
                    updated_plan,
                    updated_operands,
                    updated_rows,
                    boundary_rows,
                    candidate,
                    rejections,
                )
            )
            if proof is None:
                rejected.add(key)
                continue
            updated_plan = candidate_plan
            updated_operands = candidate_operands
            updated_rows = candidate_rows
            proofs.append(proof)
            progress = True
            break
        if not progress:
            break
    if not proofs:
        if rejections:
            rejected_plan = copy.deepcopy(plan)
            rejected_plan.setdefault("recovery", {})[
                "exact_source_residual_fold_rejections"
            ] = rejections
            return rejected_plan, operands, input_rows, None
        return plan, operands, input_rows, None
    updated_plan.setdefault("recovery", {})[
        "exact_source_residual_fold_count"
    ] = len(proofs)
    if rejections:
        updated_plan["recovery"][
            "exact_source_residual_fold_rejections"
        ] = rejections
    aggregate = dict(proofs[0])
    aggregate.update({
        "fold_count": len(proofs),
        "folds": proofs,
        "proof": "independent_same_source_residual_folds",
    })
    return updated_plan, updated_operands, updated_rows, aggregate


def _collapse_observable_alternate_to_registered_q(
    shell: dict[str, Any],
) -> dict[str, Any] | None:
    """Collapse the current zero/alternate/Q shell after state substitution.

    This is intentionally narrow rather than a claim of general sequential
    equivalence.  Every bit must be exactly one of ``0``, ``alternate`` or
    registered ``Q`` in each two-predicate phase, with one alternate phase and
    at least one Q phase.  The caller additionally requires a proved complete
    source-residual fold in the same anonymous bank before enabling it.
    """
    if (
        shell.get("kind") != "registered_observable_mux_truth_table_v1"
        or shell.get("has_alternate_data") is not True
        or len(shell.get("control_roles") or []) != 2
    ):
        return None
    collapsed_tables = []
    for raw_table in shell.get("truth_tables") or []:
        table = int(raw_table)
        phase_kinds = []
        for controls in range(4):
            outputs = []
            for alternate in (0, 1):
                for q_value in (0, 1):
                    address = q_value | (alternate << 1) | (controls << 2)
                    outputs.append((table >> address) & 1)
            if outputs == [0, 1, 0, 1]:
                phase_kinds.append("q")
            elif outputs == [0, 0, 1, 1]:
                phase_kinds.append("alternate")
            elif outputs == [0, 0, 0, 0]:
                phase_kinds.append("zero")
            else:
                return None
        if phase_kinds.count("alternate") != 1 or "q" not in phase_kinds:
            return None
        collapsed = 0
        for controls in range(4):
            for q_value in (0, 1):
                old_address = q_value | (q_value << 1) | (controls << 2)
                new_address = q_value | (controls << 1)
                collapsed |= ((table >> old_address) & 1) << new_address
        collapsed_tables.append(collapsed)
    if not collapsed_tables:
        return None
    result = copy.deepcopy(shell)
    result["has_alternate_data"] = False
    result["alternate_data_nets"] = []
    result["truth_tables"] = collapsed_tables
    result["reachable_state_reduction"] = (
        "alternate_equals_newly_captured_registered_q_v1"
    )
    return result


def build_generating_operator_bridge_bundle(
    closure: RegisteredClosure,
    *,
    fixed_depth: int | None = None,
    root_node_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build a DSP bridge from replay-proved high-level operator semantics."""
    boundary_rows = _boundary_ff_rows(closure)
    recovered = []
    failures = []
    roots = closure.roots()
    if root_node_ids is not None:
        roots = tuple(root for root in roots if root.node_id in root_node_ids)
        missing = root_node_ids - {root.node_id for root in roots}
        if missing:
            raise ValueError(
                "generating operator root selection is absent from the closure"
            )
    for root in roots:
        try:
            try:
                shell = _infer_registered_output_shell(closure, root)
                output_integration = "replace_downstream_shell_v1"
            except ValueError:
                try:
                    shell = _infer_observable_output_mux_shell(closure, root)
                    output_integration = (
                        "replace_observable_shell_truth_table_v1"
                    )
                except ValueError:
                    shell = _infer_root_control_shell(root)
                    output_integration = "preserve_target_register_v1"
            preserved_target_schedule = (
                output_integration == "preserve_target_register_v1"
            )
            if fixed_depth is None:
                plan, operands = closure.infer_generating_operator_ir(
                    root,
                    module_name="generating_operator_candidate",
                    allow_passthrough_only=preserved_target_schedule,
                    candidate_validator=lambda candidate, _operands: (
                        _semantic_accumulator_control(
                            closure,
                            root,
                            candidate,
                            allow_preserved_target_schedule=(
                                preserved_target_schedule
                            ),
                        )
                    ),
                )
            else:
                plan, operands = closure._infer_generating_operator_ir_at_depth(
                    root,
                    module_name="generating_operator_candidate",
                    max_depth=fixed_depth,
                    allow_passthrough_only=preserved_target_schedule,
                )
            if preserved_target_schedule:
                # Joint phase recovery may select a different root-control
                # assignment from the inexpensive phase chosen while loading
                # the cone.  Bind the shell truth table to the exact selected
                # arithmetic phase, never to the loader heuristic.
                shell = _infer_root_control_shell(root)
            accumulator = _semantic_accumulator_control(
                closure,
                root,
                plan,
                allow_preserved_target_schedule=preserved_target_schedule,
            )
            output = _stage_control(closure, root)
            input_rows = [
                _operand_wiring_at_cut(
                    closure,
                    root,
                    operand,
                    max_depth=int(plan["recovery"]["schedule_cut_depth"]),
                    boundary_rows=boundary_rows,
                )
                for operand in operands
            ]
            plan, operands, input_rows, residual_fold = (
                _fold_one_exact_source_residual(
                    closure, root, plan, operands, input_rows, boundary_rows
                )
            )
            clocks = {
                accumulator["clock_net"], output["clock_net"],
                *(control["clock_net"] for _, control in input_rows),
            }
            edges = {
                accumulator["clock_edge"], output["clock_edge"],
                *(control["clock_edge"] for _, control in input_rows),
            }
            if len(clocks) != 1 or edges != {"posedge"}:
                raise ValueError("generating operator bridge crosses clock semantics")
            term_signature = tuple(
                (
                    str(term["kind"]), int(term["data_width"]),
                    bool(term["data_signed"]), int(term["shift"]),
                    int(term["output_width"]),
                    int(term.get("coefficient", 1)),
                    int(term.get("coefficient_width", 0)),
                )
                for term in plan["terms"]
            )
            signature = (
                plan["data_width"], tuple(plan["input_widths"]),
                term_signature,
                tuple(
                    (
                        tuple(group["term_ordinals"]),
                        int(group["common_shift"]),
                        int(group["output_width"]),
                    )
                    for group in plan["groups"]
                ),
                (
                    tuple(plan.get("controls", {}).get("predicate_roles", [])),
                    int(plan.get("controls", {}).get(
                        "arithmetic_active_truth_table", 0
                    )),
                    int(plan.get("controls", {}).get("phase_count", 1)),
                ),
                output_integration,
                (
                    shell["gate_polarity"]
                    if output_integration == "replace_downstream_shell_v1"
                    else (
                        bool(shell["has_alternate_data"]),
                        tuple(shell["control_roles"]),
                        tuple(shell["truth_tables"]),
                    )
                    if output_integration
                    == "replace_observable_shell_truth_table_v1"
                    else (
                        bool(shell.get(
                            "control_truth_table_includes_data_bit", False
                        )),
                        tuple(shell["control_truth_tables"]),
                        tuple(shell.get("control_preserved_data_bits", [])),
                    )
                ),
            )
            recovered.append({
                "root": root,
                "plan": plan,
                "operands": operands,
                "input_rows": input_rows,
                "output_integration": output_integration,
                "shell": shell,
                "accumulator": accumulator,
                "output": output,
                "clock_net": next(iter(clocks)),
                "signature": signature,
                "residual_fold": residual_fold,
            })
        except ValueError as exc:
            failures.append({"node_id": root.node_id, "reason": str(exc)})
    if not recovered:
        summary = "; ".join(
            f"{item['node_id']}:{item['reason']}" for item in failures[:4]
        )
        raise ValueError(
            "no complete generating operator bridge was recovered"
            + (f": {summary}" if summary else "")
        )
    residual_folded_instance_count = sum(
        item["residual_fold"] is not None for item in recovered
    )
    residual_fold_count = sum(
        int(
            (item["plan"].get("recovery") or {}).get(
                "exact_source_residual_fold_count", 0
            )
        )
        for item in recovered
    )
    collapsed_shell_count = 0
    if residual_folded_instance_count:
        collapsed = []
        for item in recovered:
            if item["output_integration"] != (
                "replace_observable_shell_truth_table_v1"
            ):
                collapsed.append(item)
                continue
            shell = _collapse_observable_alternate_to_registered_q(
                item["shell"]
            )
            if shell is None:
                collapsed = []
                break
            updated = dict(item)
            updated["shell"] = shell
            collapsed.append(updated)
        if collapsed:
            recovered = collapsed
            collapsed_shell_count = sum(
                item["output_integration"]
                == "replace_observable_shell_truth_table_v1"
                for item in recovered
            )
            # Signatures include the output shell and must be regenerated after
            # the reachable-state substitution before module family grouping.
            for item in recovered:
                if item["output_integration"] != (
                    "replace_observable_shell_truth_table_v1"
                ):
                    continue
                signature = list(item["signature"])
                signature[-1] = (
                    False,
                    tuple(item["shell"]["control_roles"]),
                    tuple(item["shell"]["truth_tables"]),
                )
                item["signature"] = tuple(signature)
    families: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for item in recovered:
        families.setdefault(item["signature"], []).append(item)
    plans = []
    module_by_signature = {}
    for module_ordinal, (signature, family) in enumerate(sorted(
        families.items(), key=lambda pair: repr(pair[0])
    )):
        module_name = f"fdagents_generating_operator_{module_ordinal}"
        representative = family[0]["plan"]
        module_plan = {
            "version": 1,
            "module_name": module_name,
            "data_width": representative["data_width"],
            "coefficient_width": representative["coefficient_width"],
            "product_shift": representative["product_shift"],
            "coefficients": list(representative["coefficients"]),
            "input_signed": list(representative["input_signed"]),
            "input_widths": list(representative["input_widths"]),
            "input_roles": list(representative["input_roles"]),
            "terms": copy.deepcopy(representative["terms"]),
            "groups": copy.deepcopy(representative["groups"]),
            "controls": copy.deepcopy(representative.get("controls") or {}),
            "output_integration": family[0]["output_integration"],
        }
        if family[0]["output_integration"] == "replace_downstream_shell_v1":
            module_plan.update({
                "output_gate_polarity": family[0]["shell"]["gate_polarity"],
                "output_gate_role": "output_gate",
            })
        elif (
            family[0]["output_integration"]
            == "replace_observable_shell_truth_table_v1"
        ):
            module_plan.update({
                "shell_control_roles": list(
                    family[0]["shell"]["control_roles"]
                ),
                "shell_has_alternate_data": bool(
                    family[0]["shell"]["has_alternate_data"]
                ),
                "shell_truth_tables": list(
                    family[0]["shell"]["truth_tables"]
                ),
            })
        else:
            module_plan.update({
                "control_roles": list(family[0]["shell"]["control_roles"]),
                "control_truth_table_includes_data_bit": bool(
                    family[0]["shell"].get(
                        "control_truth_table_includes_data_bit", False
                    )
                ),
                "control_truth_tables": list(
                    family[0]["shell"]["control_truth_tables"]
                ),
                "control_preserved_data_bits": list(
                    family[0]["shell"].get(
                        "control_preserved_data_bits",
                        [False] * int(representative["data_width"]),
                    )
                ),
            })
        plans.append(module_plan)
        module_by_signature[signature] = module_name
    instances = []
    seen_output_nets = set()
    for ordinal, item in enumerate(sorted(
        recovered,
        key=lambda row: (row["root"].root_stem, row["root"].slice_index)
    )):
        roles = item["plan"]["input_roles"]
        output_nets = list(item["shell"]["output_nets"])
        if (
            len(output_nets) != len(set(output_nets))
            or seen_output_nets.intersection(output_nets)
        ):
            raise ValueError("generating operator roots overlap observable outputs")
        seen_output_nets.update(output_nets)
        instance = {
            "name": f"fdagents_generating_operator_inst_{ordinal}",
            "module_name": module_by_signature[item["signature"]],
            "input_d_nets": {
                role: d_nets
                for role, (d_nets, _) in zip(roles, item["input_rows"])
            },
            "input_ce_nets": {
                role: control["ce_net"]
                for role, (_, control) in zip(roles, item["input_rows"])
            },
            "clock_net": item["clock_net"],
            "product_ce_net": item["accumulator"]["ce_net"],
            "output_nets": output_nets,
            "replaced_target_node_id": item["root"].node_id,
        }
        if item["output_integration"] == "replace_downstream_shell_v1":
            instance.update({
                "output_ce_net": item["output"]["ce_net"],
                "output_reset_net": item["output"]["reset_net"],
                "output_gate_net": item["shell"]["gate_net"],
            })
        elif (
            item["output_integration"]
            == "replace_observable_shell_truth_table_v1"
        ):
            instance.update({
                "output_ce_net": item["output"]["ce_net"],
                "output_reset_net": item["output"]["reset_net"],
                "shell_control_nets": dict(zip(
                    item["shell"]["control_roles"],
                    item["shell"]["control_nets"],
                )),
            })
            if item["shell"]["has_alternate_data"]:
                instance["alternate_data_nets"] = list(
                    item["shell"]["alternate_data_nets"]
                )
        else:
            instance["control_nets"] = dict(zip(
                item["shell"]["control_roles"],
                item["shell"]["control_nets"],
            ))
            if any(item["shell"].get("control_preserved_data_bits", [])):
                instance["preserved_data_nets"] = output_nets
        instances.append(instance)
    representative = recovered[0]["plan"]
    output_shell_kinds = sorted({item["shell"]["kind"] for item in recovered})
    return {
        "version": 1,
        "kind": "generating_operator_bridge_bundle",
        "plans": plans,
        "instances": instances,
        "recovery": {
            "operator_class": representative["operator_class"],
            "quantization": representative["quantization"],
            "selected_signature": [
                list(signature) for signature in sorted(families, key=repr)
            ],
            "selected_instance_count": len(instances),
            "candidate_count": len(recovered),
            "failed_candidates": failures,
            "selection": "max_replay_proved_high_level_semantic_family",
            "backend_input": "generating_operator_ir",
            "output_shell_kind": (
                output_shell_kinds[0] if len(output_shell_kinds) == 1
                else "per_root_replay_proved_output_integration"
            ),
            "schedule_cut_depth": max(
                item["plan"]["recovery"]["schedule_cut_depth"]
                for item in recovered
            ),
            "replay_vector_count": min(
                item["plan"]["recovery"]["replay_vector_count"]
                for item in recovered
            ),
            "external_plan_used": False,
            "exact_source_residual_fold_count": residual_fold_count,
            "reachable_state_shell_collapse_count": collapsed_shell_count,
            "complete_sequential_write_mode": (
                "narrow_observed_structure_v1"
                if collapsed_shell_count else "disabled"
            ),
        },
    }


def merge_generating_operator_bridge_bundles(
    bundles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge replay-proved anonymous operator banks into one atomic action.

    Each input bundle was recovered independently from the current DCP.  The
    merge deliberately ignores hierarchy and benchmark identity.  Each bank
    must independently pass expression and schedule replay, but different
    banks may implement different arithmetic cores.  The merged bundle keeps
    one generated module per distinct full plan and shares only truly
    identical plans.
    """
    if not bundles:
        raise ValueError("generating operator merge requires at least one bank")

    def core_signature(bundle: dict[str, Any], plan: dict[str, Any]) -> tuple[Any, ...]:
        recovery = bundle.get("recovery") or {}
        quantization = recovery.get("quantization") or {}
        raw_terms = plan.get("terms")
        if raw_terms is None:
            raw_terms = [
                {
                    "kind": "fixed_product_slice",
                    "source_role": role,
                    "data_width": int(plan["data_width"]),
                    "data_signed": signed,
                    "shift": int(plan["product_shift"]),
                    "output_width": int(plan["data_width"]),
                    "coefficient": coefficient,
                    "coefficient_width": int(plan["coefficient_width"]),
                }
                for role, signed, coefficient in zip(
                    plan["input_roles"], plan["input_signed"],
                    plan["coefficients"]
                )
            ]
        input_widths = plan.get("input_widths") or [
            int(plan["data_width"])
        ] * len(plan["input_roles"])
        raw_groups = plan.get("groups") or [
            {
                "term_ordinals": [ordinal],
                "common_shift": int(term["shift"]),
                "output_width": int(plan["data_width"]),
            }
            for ordinal, term in enumerate(raw_terms)
        ]
        controls = plan.get("controls") or {}
        term_signature = tuple(
            (
                str(term["kind"]), str(term["source_role"]),
                int(term["data_width"]), bool(term["data_signed"]),
                int(term["shift"]), int(term["output_width"]),
                int(term.get("coefficient", 1)),
                int(term.get("coefficient_width", 0)),
            )
            for term in raw_terms
        )
        return (
            recovery.get("operator_class"),
            tuple(sorted(quantization.items())),
            int(plan["data_width"]),
            tuple(int(value) for value in input_widths),
            tuple(bool(value) for value in plan["input_signed"]),
            tuple(str(value) for value in plan["input_roles"]),
            term_signature,
            tuple(
                (
                    tuple(int(value) for value in group["term_ordinals"]),
                    int(group["common_shift"]), int(group["output_width"]),
                )
                for group in raw_groups
            ),
            (
                tuple(str(value) for value in controls.get("predicate_roles", [])),
                int(controls.get("arithmetic_active_truth_table", 0)),
                int(controls.get("phase_count", 1)),
            ),
            int(recovery["schedule_cut_depth"]),
        )

    accepted: list[dict[str, Any]] = []
    output_banks: list[frozenset[str]] = []
    for bundle in bundles:
        if (
            not isinstance(bundle, dict)
            or bundle.get("kind") != "generating_operator_bridge_bundle"
            or not isinstance(bundle.get("plans"), list)
            or not bundle["plans"]
            or not isinstance(bundle.get("instances"), list)
            or not bundle["instances"]
        ):
            raise ValueError("only complete generating operator banks can be merged")
        plan_by_name = {
            str(plan.get("module_name") or ""): plan
            for plan in bundle["plans"]
            if isinstance(plan, dict)
        }
        if len(plan_by_name) != len(bundle["plans"]) or "" in plan_by_name:
            raise ValueError("generating operator bank has invalid module plans")
        cores = {
            core_signature(bundle, plan) for plan in plan_by_name.values()
        }
        if not cores:
            raise ValueError("one generating operator bank has no arithmetic core")

        outputs = frozenset(
            str(net)
            for instance in bundle["instances"]
            for net in (instance.get("output_nets") or [])
        )
        if not outputs:
            raise ValueError("generating operator bank has no observable outputs")
        duplicate = False
        for prior in output_banks:
            overlap = prior & outputs
            if overlap and prior != outputs:
                raise ValueError("generating operator banks partially overlap outputs")
            if prior == outputs:
                duplicate = True
                break
        if duplicate:
            continue
        output_banks.append(outputs)
        accepted.append(bundle)

    if not accepted:
        raise ValueError("generating operator merge retained no unique bank")

    plans: list[dict[str, Any]] = []
    plan_names: dict[tuple[Any, ...], str] = {}
    instances: list[dict[str, Any]] = []
    seen_instance_outputs: set[str] = set()
    per_bank = []
    for bank_ordinal, bundle in enumerate(accepted):
        local_names: dict[str, str] = {}
        for plan in bundle["plans"]:
            integration = str(
                plan.get("output_integration")
                or "replace_downstream_shell_v1"
            )
            output_signature = (
                (
                    str(plan["output_gate_polarity"]),
                    str(plan["output_gate_role"]),
                )
                if integration == "replace_downstream_shell_v1"
                else (
                    bool(plan["shell_has_alternate_data"]),
                    tuple(str(role) for role in plan["shell_control_roles"]),
                    tuple(int(value) for value in plan["shell_truth_tables"]),
                )
                if integration == "replace_observable_shell_truth_table_v1"
                else (
                    tuple(str(role) for role in plan["control_roles"]),
                    bool(plan.get(
                        "control_truth_table_includes_data_bit", False
                    )),
                    tuple(int(value) for value in plan["control_truth_tables"]),
                    tuple(bool(value) for value in plan.get(
                        "control_preserved_data_bits",
                        [False] * int(plan["data_width"]),
                    )),
                )
            )
            full_signature = core_signature(bundle, plan) + (
                integration, output_signature,
            )
            module_name = plan_names.get(full_signature)
            if module_name is None:
                module_name = f"fdagents_generating_operator_{len(plans)}"
                merged_plan = copy.deepcopy(plan)
                merged_plan["module_name"] = module_name
                plans.append(merged_plan)
                plan_names[full_signature] = module_name
            local_names[str(plan["module_name"])] = module_name

        bank_instance_count = 0
        for instance in bundle["instances"]:
            old_module = str(instance.get("module_name") or "")
            if old_module not in local_names:
                raise ValueError("generating operator instance has no module plan")
            output_nets = [str(net) for net in instance.get("output_nets") or []]
            if (
                not output_nets
                or len(output_nets) != len(set(output_nets))
                or seen_instance_outputs.intersection(output_nets)
            ):
                raise ValueError("generating operator instances overlap outputs")
            seen_instance_outputs.update(output_nets)
            merged_instance = copy.deepcopy(instance)
            merged_instance["name"] = (
                f"fdagents_generating_operator_inst_{len(instances)}"
            )
            merged_instance["module_name"] = local_names[old_module]
            instances.append(merged_instance)
            bank_instance_count += 1
        per_bank.append({
            "bank_ordinal": bank_ordinal,
            "instance_count": bank_instance_count,
            "output_net_count": len(output_banks[bank_ordinal]),
            "output_shell_kind": (
                (bundle.get("recovery") or {}).get("output_shell_kind")
            ),
            "replay_vector_count": int(
                (bundle.get("recovery") or {}).get("replay_vector_count", 0)
            ),
            "operator_class": (
                (bundle.get("recovery") or {}).get("operator_class")
            ),
            "selected_signature": copy.deepcopy(
                (bundle.get("recovery") or {}).get("selected_signature")
            ),
            "exact_source_residual_fold_count": int(
                (bundle.get("recovery") or {}).get(
                    "exact_source_residual_fold_count", 0
                )
            ),
            "reachable_state_shell_collapse_count": int(
                (bundle.get("recovery") or {}).get(
                    "reachable_state_shell_collapse_count", 0
                )
            ),
        })

    recoveries = [bundle["recovery"] for bundle in accepted]
    representative = recoveries[0]
    return {
        "version": 1,
        "kind": "generating_operator_bridge_bundle",
        "plans": plans,
        "instances": instances,
        "recovery": {
            "operator_class": representative["operator_class"],
            "quantization": copy.deepcopy(representative["quantization"]),
            "selected_signature": copy.deepcopy(
                representative["selected_signature"]
            ),
            "selected_instance_count": len(instances),
            "candidate_count": sum(
                int(item.get("candidate_count", 0)) for item in recoveries
            ),
            "failed_candidates": [
                copy.deepcopy(item)
                for recovery in recoveries
                for item in (recovery.get("failed_candidates") or [])
            ],
            "selection": "all_replay_proved_anonymous_structural_replica_banks",
            "backend_input": (
                "generating_operator_ir_multi_bank"
                if len(accepted) > 1
                else str(representative["backend_input"])
            ),
            "output_shell_kind": "per_bank_registered_output_shell",
            "schedule_cut_depth": max(
                int(item["schedule_cut_depth"]) for item in recoveries
            ),
            "replay_vector_count": min(
                int(item["replay_vector_count"]) for item in recoveries
            ),
            "bank_count": len(accepted),
            "per_bank": per_bank,
            "external_plan_used": False,
            "exact_source_residual_fold_count": sum(
                int(item.get("exact_source_residual_fold_count", 0))
                for item in recoveries
            ),
            "reachable_state_shell_collapse_count": sum(
                int(item.get("reachable_state_shell_collapse_count", 0))
                for item in recoveries
            ),
            "complete_sequential_write_mode": (
                "narrow_observed_structure_v1"
                if any(
                    int(item.get("reachable_state_shell_collapse_count", 0))
                    for item in recoveries
                ) else "disabled"
            ),
        },
    }


def namespace_generating_operator_bridge_bundle(
    bundle: dict[str, Any], *, namespace: str
) -> dict[str, Any]:
    """Assign one deterministic, collision-resistant EDIF namespace.

    Incremental DCP rewrites may already contain modules emitted by an earlier
    operator action.  Reusing the historical ``fdagents_generating_operator``
    names makes an otherwise valid second rewrite collide inside RapidWright.
    Namespace allocation belongs to the framework, never the LLM; callers bind
    it to the exact source checkpoint identity before synthesis.
    """
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", namespace) is None:
        raise ValueError("generating operator namespace is not a Verilog identifier")
    if (
        not isinstance(bundle, dict)
        or bundle.get("kind") != "generating_operator_bridge_bundle"
        or not isinstance(bundle.get("plans"), list)
        or not bundle["plans"]
        or not isinstance(bundle.get("instances"), list)
        or not bundle["instances"]
    ):
        raise ValueError("only a complete generating operator bundle can be namespaced")
    updated = copy.deepcopy(bundle)
    renamed_modules: dict[str, str] = {}
    for ordinal, plan in enumerate(updated["plans"]):
        old_name = str(plan.get("module_name") or "")
        if not old_name or old_name in renamed_modules:
            raise ValueError("generating operator module names are not unique")
        new_name = f"{namespace}_module_{ordinal}"
        renamed_modules[old_name] = new_name
        plan["module_name"] = new_name
    for ordinal, instance in enumerate(updated["instances"]):
        old_module = str(instance.get("module_name") or "")
        if old_module not in renamed_modules:
            raise ValueError("generating operator instance references an unknown module")
        instance["name"] = f"{namespace}_inst_{ordinal}"
        instance["module_name"] = renamed_modules[old_module]
    updated.setdefault("recovery", {})["bundle_namespace"] = namespace
    return updated


def _infer_frontier_terms(
    closure: RegisteredClosure,
    record: ClosureRecord,
    *,
    max_depth: int,
) -> tuple[list[dict[str, Any]], dict[str, WordSliceRef]]:
    """Recover an additive word expression over one explicit state frontier."""
    word = record.recover()
    if word is None:
        raise ValueError("registered state node is not evaluable")
    leaves, _ = closure.leaf_slices(record, max_depth=max_depth)
    if not leaves:
        raise ValueError("registered state node has no recoverable frontier")
    if closure.evaluate(record, {}, max_depth=max_depth) != 0:
        raise ValueError("registered state node has a nonzero affine baseline")
    terms = []
    for key, leaf in sorted(leaves.items()):
        samples = []
        for value in _sample_words(leaf.width, random.Random(leaf.width)):
            observed = closure.evaluate(
                record, {key: value}, max_depth=max_depth
            ) & ((1 << word.width) - 1)
            samples.append((value, observed))
        term = _infer_bus_term({
            "stem": key,
            "width": leaf.width,
            "source_lsb": 0,
            "bits": [],
        }, samples, word.width)
        if term is None or term["kind"] not in {
            "fixed_product_slice", "passthrough",
        }:
            raise ValueError("registered state term is not a supported linear slice")
        terms.append(term)
    rng = random.Random(0xCE5DA6)
    mask = (1 << word.width) - 1
    for _ in range(256):
        values = {key: rng.getrandbits(leaf.width) for key, leaf in leaves.items()}
        predicted = sum(
            _term_value(term, values[term["source"]["stem"]])
            for term in terms
        ) & mask
        if predicted != closure.evaluate(record, values, max_depth=max_depth):
            raise ValueError("registered state expression failed independent replay")
    return terms, leaves


def _infer_grouped_frontier_terms(
    closure: RegisteredClosure,
    record: ClosureRecord,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, WordSliceRef]]:
    """Recover quantization groups over independent registered states."""
    word = record.recover()
    if word is None:
        raise ValueError("registered grouped state node is not evaluable")
    leaves, _ = closure.leaf_slices(record, max_depth=1)
    keys = sorted(leaves)
    zero = closure.evaluate(record, {}, max_depth=1)
    if zero != 0:
        raise ValueError("registered grouped state has a nonzero affine baseline")
    rows = [{"inputs": {}, "source_full_output": zero}]
    terms = []
    for key in keys:
        leaf = leaves[key]
        samples = []
        for value in _sample_words(leaf.width, random.Random(leaf.width)):
            observed = closure.evaluate(record, {key: value}, max_depth=1)
            samples.append((value, observed))
            rows.append({"inputs": {key: value}, "source_full_output": observed})
        term = _infer_full_bus_term({
            "stem": key,
            "width": leaf.width,
            "source_lsb": 0,
            "bits": [],
        }, samples, word.width)
        if term is None or term["kind"] not in {
            "fixed_product_slice", "passthrough",
        }:
            raise ValueError("registered grouped term is not a supported linear slice")
        terms.append(term)
    pair_rng = random.Random(0xB2A6C4)
    for left in range(len(keys)):
        for right in range(left + 1, len(keys)):
            for _ in range(12):
                values = {
                    keys[left]: pair_rng.getrandbits(leaves[keys[left]].width),
                    keys[right]: pair_rng.getrandbits(leaves[keys[right]].width),
                }
                rows.append({
                    "inputs": values,
                    "source_full_output": closure.evaluate(
                        record, values, max_depth=1
                    ),
                })
    table = {
        "source_chain_width": word.width,
        "rows": rows,
    }
    groups = _interaction_groups(table, terms)
    rng = random.Random(0xA11CE5)
    mask = (1 << word.width) - 1
    for _ in range(256):
        values = {key: rng.getrandbits(leaf.width) for key, leaf in leaves.items()}
        predicted = sum(
            _group_value(group, terms, values, word.width) for group in groups
        ) & mask
        if predicted != closure.evaluate(record, values, max_depth=1):
            raise ValueError("registered quantization groups failed random replay")
    return terms, groups, leaves


def _normalized_state_term(
    term: Mapping[str, Any],
    *,
    source_role: str,
    source_offset: int = 0,
) -> dict[str, Any]:
    result = {
        "kind": str(term["kind"]),
        "source_role": source_role,
        "source_offset": int(source_offset),
        "data_width": int(term["data_width"]),
        "data_signed": bool(term["data_signed"]),
        "shift": int(term.get("shift", 0)),
        "output_width": int(term["output_width"]),
    }
    if term["kind"] == "fixed_product_slice":
        coefficient = int(term["coefficient"])
        result.update({
            "coefficient": coefficient,
            # RTL literals are signed.  Retain one explicit sign bit even when
            # the recovered Boolean coefficient was unsigned.
            "coefficient_width": max(
                int(term["coefficient_width"]),
                2,
                abs(coefficient).bit_length() + 1,
            ),
        })
    return result


def _term_shape(term: Mapping[str, Any], *, include_source: bool = True) -> tuple[Any, ...]:
    result: tuple[Any, ...] = (
        str(term["kind"]), int(term["source_offset"]),
        int(term["data_width"]), bool(term["data_signed"]),
        int(term["shift"]), int(term["output_width"]),
        int(term.get("coefficient", 1)),
        int(term.get("coefficient_width", 0)),
    )
    if include_source:
        result = (str(term["source_role"]),) + result
    return result


def _infer_branched_product_candidate(
    closure: RegisteredClosure,
    record: ClosureRecord,
) -> dict[str, Any]:
    """Recover a heterogeneous-CE registered arithmetic state DAG.

    The observable state is rebuilt from its direct registered producers.  Each
    producer keeps its own CE/reset contract, so two paths fed by the same word
    are never collapsed merely because their combinational value is identical.
    """
    root_word = record.recover()
    if root_word is None:
        raise ValueError("registered branch root is not evaluable")
    root_terms, root_groups, root_leaves = _infer_grouped_frontier_terms(
        closure, record
    )
    producer_by_root_leaf: dict[str, ClosureRecord] = {}
    for bus in root_word.buses:
        producer = closure.resolve_producer(record, bus)
        if producer is None:
            raise ValueError("registered branch root has a combinational input")
        key = WordSliceRef(
            stem=bus.stem,
            source_lsb=bus.source_lsb,
            width=bus.width,
            nets=bus.nets,
        ).key
        prior = producer_by_root_leaf.get(key)
        if prior is not None and prior.node_id != producer.node_id:
            raise ValueError("registered branch producer is ambiguous")
        producer_by_root_leaf[key] = producer
    if set(producer_by_root_leaf) != set(root_leaves):
        raise ValueError("registered branch frontier does not cover the root")

    raw_branches = []
    root_term_by_leaf = {term["source"]["stem"]: term for term in root_terms}
    root_ordinal_by_leaf = {
        term["source"]["stem"]: ordinal for ordinal, term in enumerate(root_terms)
    }
    if set(root_term_by_leaf) != set(root_leaves):
        raise ValueError("registered branch root terms are not one-to-one")
    seen_producers = set()
    for root_key in sorted(root_leaves):
        producer = producer_by_root_leaf[root_key]
        if producer.node_id in seen_producers:
            raise ValueError("registered branch producer feeds multiple root slices")
        seen_producers.add(producer.node_id)
        branch_terms, input_leaves = _infer_frontier_terms(
            closure, producer, max_depth=1
        )
        if len(branch_terms) != 1 or len(input_leaves) != 1:
            raise ValueError(
                "registered branch must have one recoverable source word"
            )
        input_key, input_leaf = next(iter(input_leaves.items()))
        control = _stage_control(closure, producer)
        producer_word = producer.recover()
        assert producer_word is not None
        root_term = root_term_by_leaf[root_key]
        root_leaf = root_leaves[root_key]
        source_offset = root_leaf.source_lsb - producer_word.target_lsb
        if source_offset < 0 or source_offset + root_leaf.width > producer_word.width:
            raise ValueError("registered branch root slice lies outside producer")
        raw_branches.append({
            "producer": producer,
            "input_key": input_key,
            "input_leaf": input_leaf,
            "control": control,
            "width": producer_word.width,
            "target_lsb": producer_word.target_lsb,
            "branch_term": _normalized_state_term(
                branch_terms[0], source_role="pending"
            ),
            "root_term": _normalized_state_term(
                root_term, source_role="pending", source_offset=source_offset
            ),
            "root_term_ordinal": root_ordinal_by_leaf[root_key],
        })

    # Canonicalize anonymous input roles by the behavior of every state branch
    # they feed.  Names and hierarchy never enter the module signature.
    input_groups: dict[str, list[dict[str, Any]]] = {}
    for branch in raw_branches:
        input_groups.setdefault(branch["input_key"], []).append(branch)
    input_rows = []
    for key, branches in input_groups.items():
        descriptors = sorted((
            int(branch["width"]), int(branch["target_lsb"]),
            str(branch["control"]["reset_kind"]),
            _term_shape(branch["branch_term"], include_source=False),
            _term_shape(branch["root_term"], include_source=False),
        ) for branch in branches)
        input_rows.append((
            (int(branches[0]["input_leaf"].width), tuple(descriptors)),
            key,
            branches[0]["input_leaf"],
        ))
    input_rows.sort(key=lambda item: (item[0], item[1]))
    input_role_by_key = {
        key: f"input_{ordinal}"
        for ordinal, (_, key, _) in enumerate(input_rows)
    }
    inputs = [{
        "role": input_role_by_key[key],
        "width": leaf.width,
    } for _, key, leaf in input_rows]

    for branch in raw_branches:
        branch["branch_term"]["source_role"] = input_role_by_key[branch["input_key"]]
    raw_branches.sort(key=lambda branch: (
        branch["branch_term"]["source_role"],
        int(branch["width"]), int(branch["target_lsb"]),
        _term_shape(branch["branch_term"]),
        _term_shape(branch["root_term"], include_source=False),
    ))
    states = []
    for ordinal, branch in enumerate(raw_branches):
        role = f"state_{ordinal}"
        branch["state_role"] = role
        branch["root_term"]["source_role"] = role
        states.append({
            "role": role,
            "width": int(branch["width"]),
            "target_lsb": int(branch["target_lsb"]),
            "reset_kind": str(branch["control"]["reset_kind"]),
            "terms": [branch["branch_term"]],
        })
    output_terms = sorted(
        (branch["root_term"] for branch in raw_branches),
        key=lambda term: str(term["source_role"]),
    )
    new_ordinal_by_old = {
        int(branch["root_term_ordinal"]): ordinal
        for ordinal, branch in enumerate(raw_branches)
    }
    output_groups = []
    for group in root_groups:
        output_groups.append({
            "term_ordinals": sorted(
                new_ordinal_by_old[int(value)]
                for value in group["term_ordinals"]
            ),
            "common_shift": int(group["common_shift"]),
            "destination_lsb": int(group["destination_lsb"]),
            "output_width": int(group["output_width"]),
        })
    output_groups.sort(key=lambda group: tuple(group["term_ordinals"]))
    output_control = _stage_control(closure, record)
    all_controls = [branch["control"] for branch in raw_branches] + [output_control]
    clock_nets = {item["clock_net"] for item in all_controls}
    clock_edges = {item["clock_edge"] for item in all_controls}
    if len(clock_nets) != 1 or len(clock_edges) != 1:
        raise ValueError("registered branch state DAG crosses clock domains")
    plan = {
        "version": 1,
        "schedule_kind": "registered_branch_state_dag_v1",
        "module_name": "registered_branch_candidate",
        "data_width": root_word.width,
        "pipeline_depth": 2,
        "clock_edge": next(iter(clock_edges)),
        "inputs": inputs,
        "states": states,
        "output_state": {
            "width": root_word.width,
            "reset_kind": output_control["reset_kind"],
            "terms": output_terms,
            "groups": output_groups,
        },
        "recovery": {
            "schedule_cut_depth": 2,
            "replay_vector_count": 512,
            "temporal_branch_count": len(states),
            "external_plan_used": False,
        },
    }
    signature = (
        plan["schedule_kind"], plan["data_width"], plan["clock_edge"],
        tuple((item["width"],) for item in inputs),
        tuple((
            state["width"], state["target_lsb"], state["reset_kind"],
            tuple(_term_shape(term) for term in state["terms"]),
        ) for state in states),
        plan["output_state"]["reset_kind"],
        tuple(_term_shape(term) for term in output_terms),
        tuple((
            tuple(group["term_ordinals"]), group["common_shift"],
            group["destination_lsb"], group["output_width"],
        ) for group in output_groups),
    )
    return {
        "record": record,
        "plan": plan,
        "signature": signature,
        "group": (record.root_stem, record.slice_index),
        "input_leaves": {
            input_role_by_key[key]: leaf for _, key, leaf in input_rows
        },
        "branches": raw_branches,
        "output_control": output_control,
        "clock_net": next(iter(clock_nets)),
    }


def build_registered_product_bundle(
    closure: RegisteredClosure,
) -> dict[str, Any]:
    """Recover the strongest repeated product family without external plans."""
    recovered = []
    failures = []
    for record in closure.direct_product_candidate_records():
        try:
            plan, leaves = closure.infer_product_plan(
                record, module_name="registered_product_candidate"
            )
            schedule = _candidate_schedule(closure, record, plan)
            plan.update({
                key: schedule[key]
                for key in (
                    "pipeline_depth", "clock_edge", "product_reset_kinds",
                    "tail_reset_kinds",
                )
            })
            signature = (
                plan["data_width"], plan["coefficient_width"],
                plan["product_shift"], tuple(plan["coefficients"]),
                tuple(plan["input_signed"]), plan["pipeline_depth"],
                plan["clock_edge"], tuple(plan["product_reset_kinds"]),
                tuple(plan["tail_reset_kinds"]),
            )
            recovered.append({
                "backend_kind": "uniform_product_schedule",
                "record": record,
                "plan": plan,
                "leaves": leaves,
                "schedule": schedule,
                "signature": signature,
                "group": (record.root_stem, record.slice_index),
            })
        except ValueError as exc:
            uniform_reason = str(exc)
            try:
                recovered.append({
                    "backend_kind": "branched_state_dag",
                    **_infer_branched_product_candidate(closure, record),
                })
            except ValueError as branch_exc:
                failures.append({
                    "node_id": record.node_id,
                    "uniform_reason": uniform_reason,
                    "branched_reason": str(branch_exc),
                })
    if not recovered:
        summary = "; ".join(
            f"{item['node_id']}:uniform={item['uniform_reason']};"
            f"branched={item['branched_reason']}"
            for item in failures[:4]
        )
        raise ValueError(
            "no registered fixed-product family was recovered"
            + (f": {summary}" if summary else "")
        )
    by_signature: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for item in recovered:
        by_signature.setdefault(item["signature"], []).append(item)
    signature, family = max(
        by_signature.items(),
        key=lambda pair: (
            len({item["group"] for item in pair[1]}),
            sum(len(item["record"].graph.cells) for item in pair[1]),
        ),
    )
    by_group = {}
    for item in family:
        prior = by_group.get(item["group"])
        if prior is None or len(item["record"].graph.cells) > len(prior["record"].graph.cells):
            by_group[item["group"]] = item
    family = [by_group[key] for key in sorted(by_group, key=lambda value: (value[0], value[1]))]
    module_name = "fdagents_registered_product_0"
    module_plan = dict(family[0]["plan"])
    module_plan["module_name"] = module_name
    instances = []
    for ordinal, item in enumerate(family):
        record = item["record"]
        plan = item["plan"]
        targets = sorted(record.graph.targets, key=lambda row: int(row["index"]))
        common = {
            "name": f"fdagents_registered_product_inst_{ordinal}",
            "module_name": module_name,
            "output_nets": [
                _one_object_name(row["q_net"], "target Q net") for row in targets
            ],
            "replaced_target_node_id": record.node_id,
        }
        if item["backend_kind"] == "uniform_product_schedule":
            leaves = item["leaves"]
            schedule = item["schedule"]
            roles = list(plan["input_roles"])
            leaf_keys = list(plan["recovery"]["leaf_keys"])
            instances.append({
                **common,
                "input_nets": {
                    role: list(leaves[key].nets)
                    for role, key in zip(roles, leaf_keys)
                },
                "clock_net": schedule["clock_net"],
                "product_ce_nets": dict(zip(roles, schedule["product_ce_nets"])),
                "product_reset_nets": dict(zip(roles, schedule["product_reset_nets"])),
                "tail_ce_nets": list(schedule["tail_ce_nets"]),
                "tail_reset_nets": list(schedule["tail_reset_nets"]),
            })
        else:
            instances.append({
                **common,
                "input_nets": {
                    role: list(leaf.nets)
                    for role, leaf in item["input_leaves"].items()
                },
                "clock_net": item["clock_net"],
                "state_ce_nets": {
                    branch["state_role"]: branch["control"]["ce_net"]
                    for branch in item["branches"]
                },
                "state_reset_nets": {
                    branch["state_role"]: branch["control"]["reset_net"]
                    for branch in item["branches"]
                },
                "output_ce_net": item["output_control"]["ce_net"],
                "output_reset_net": item["output_control"]["reset_net"],
            })
    return {
        "version": 2,
        "kind": "registered_product_bundle",
        "plans": [module_plan],
        "instances": instances,
        "recovery": {
            "candidate_count": len(recovered),
            "selected_instance_count": len(instances),
            "selected_signature": list(signature),
            "failed_candidates": failures,
            "backend_kind": family[0]["backend_kind"],
            "external_plan_used": False,
            "selection": "max_cross_slice_support_then_removed_cone_size",
        },
    }


def _word_boundaries(graph: ConeGraph) -> tuple[tuple[WordBus, ...], tuple[str, ...]]:
    grouped: dict[str, dict[int, list[tuple[str, bool]]]] = {}
    controls = []
    for net, cell in sorted(graph.boundary_nets.items()):
        match = _INDEXED.fullmatch(cell)
        if match is None:
            controls.append(net)
            continue
        grouped.setdefault(match.group("stem"), {}).setdefault(
            int(match.group("index")), []
        ).append((net, bool(match.group("replica"))))
    buses = []
    for stem, members in sorted(grouped.items()):
        indices = sorted(members)
        runs: list[list[int]] = []
        for index in indices:
            if not runs or index != runs[-1][-1] + 1:
                runs.append([index])
            else:
                runs[-1].append(index)
        for run in runs:
            # A lone ordinary indexed register may be a scalar state/control
            # bit.  A synthesized ``*_replica`` cell, however, is a physical
            # copy of an indexed data register.  Preserve even a sparse
            # one-bit replica as a word slice so the registered closure can
            # resolve it back to the complete producer word instead of
            # inventing an independent Boolean control phase.
            if (
                len(run) == 1
                and len(members[run[0]]) == 1
                and not members[run[0]][0][1]
            ):
                controls.append(members[run[0]][0][0])
                continue
            aliases = tuple(
                tuple(net for net, _ in members[index]) for index in run
            )
            canonical = tuple(
                next(
                    (net for net, replica in members[index] if not replica),
                    members[index][0][0],
                )
                for index in run
            )
            buses.append(WordBus(
                stem=stem,
                width=len(run),
                source_lsb=run[0],
                nets=canonical,
                aliases=aliases,
            ))
    return tuple(buses), tuple(sorted(controls))


def _evaluate_graph_word(
    graph: ConeGraph,
    buses: tuple[WordBus, ...],
    controls: tuple[str, ...],
    values: Mapping[str, int],
    control_values: Mapping[str, bool],
) -> int:
    true_nets = []
    for bus in buses:
        value = int(values.get(bus.key, values.get(bus.stem, 0)))
        for bit, net in enumerate(bus.nets):
            if not (value >> bit) & 1:
                continue
            true_nets.extend(
                bus.aliases[bit] if bus.aliases else (net,)
            )
    true_nets.extend(
        net for net in controls if bool(control_values.get(net, False))
    )
    result = _evaluate_target_boolean_batch(
        graph,
        sorted(graph.boundary_nets),
        [tuple(sorted(true_nets))],
        deadline_check=lambda _stage: None,
    )
    return int(result[0])


def _evaluate_graph_word_batch(
    graph: ConeGraph,
    buses: tuple[WordBus, ...],
    controls: tuple[str, ...],
    values: list[Mapping[str, int]],
    control_values: Mapping[str, bool],
) -> list[int]:
    """Evaluate all scenarios with one packed pass through an exact cone."""
    assignments: list[tuple[str, ...]] = []
    asserted_controls = tuple(
        net for net in controls if bool(control_values.get(net, False))
    )
    for scenario in values:
        true_nets = list(asserted_controls)
        for bus in buses:
            value = int(scenario.get(bus.key, scenario.get(bus.stem, 0)))
            for bit, net in enumerate(bus.nets):
                if not (value >> bit) & 1:
                    continue
                true_nets.extend(
                    bus.aliases[bit] if bus.aliases else (net,)
                )
        assignments.append(tuple(sorted(true_nets)))
    return _evaluate_target_boolean_batch(
        graph,
        sorted(graph.boundary_nets),
        assignments,
        deadline_check=lambda _stage: None,
    )


def _registered_control_groups(
    graph: ConeGraph, controls: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    """Collapse only FF controls that are provably the same state bit.

    Physical synthesis commonly replicates a scalar enable close to several
    lanes.  The replicas have the same D, clock, CE, reset, primitive and INIT,
    so allowing them to take independent values creates unreachable control
    phases.  That used to make otherwise identical packed arithmetic lanes
    recover different low-bit functions.  Names are not used as evidence: an
    exact sequential-input signature is required, and unresolved controls stay
    singleton groups.
    """
    rows: dict[str, dict[str, str]] = {}
    path = graph.root / "boundary.tsv"
    if path.is_file():
        with path.open("r", encoding="utf-8", newline="") as stream:
            for raw in csv.DictReader(stream, delimiter="\t"):
                graph_q_net = str(
                    raw.get("graph_q_net") or raw.get("q_net") or ""
                )
                if graph_q_net:
                    rows[graph_q_net] = {
                        key: str(raw.get(key) or "")
                        for key in (
                            "ref", "d_net", "clock_net", "ce_net",
                            "sr_net", "init",
                        )
                    }
    grouped: dict[tuple[Any, ...], list[str]] = {}
    for net in controls:
        row = rows.get(net)
        if row is None or not row["d_net"]:
            key = ("unresolved", net)
        else:
            key = (
                "registered_alias", row["ref"], row["d_net"],
                row["clock_net"], row["ce_net"], row["sr_net"], row["init"],
            )
        grouped.setdefault(key, []).append(net)
    return tuple(
        tuple(sorted(grouped[key])) for key in sorted(grouped, key=repr)
    )


def recover_word_node(graph: ConeGraph) -> RecoveredWordNode:
    """Build an exact current-cone evaluator, including data-gate controls."""
    buses, controls = _word_boundaries(graph)
    if not buses:
        raise ValueError("registered closure node has no indexed word boundary")
    target_stem = str(graph.manifest.get("target_stem") or "")
    if not target_stem:
        raise ValueError("registered closure node has no target identity")

    def evaluate(
        values: Mapping[str, int], control_values: Mapping[str, bool]
    ) -> int:
        return _evaluate_graph_word(graph, buses, controls, values, control_values)

    def evaluate_batch(
        values: list[Mapping[str, int]], control_values: Mapping[str, bool]
    ) -> list[int]:
        return _evaluate_graph_word_batch(
            graph, buses, controls, values, control_values
        )

    # Data mux predicates are part of the recovered function rather than a
    # dispatch gate.  Select the assignment exposing the largest deterministic
    # data response, and retain it for a later explicit gate-polarity contract.
    control_groups = _registered_control_groups(graph, controls)
    if len(control_groups) > 8:
        control_candidates = [
            tuple(False for _ in control_groups),
            tuple(True for _ in control_groups),
        ]
    else:
        control_candidates = list(
            product((False, True), repeat=len(control_groups))
        )
    probes: list[dict[str, int]] = [{}]
    for bus in buses:
        probes.extend(
            {bus.key: value} for value in (1, 1 << (bus.width - 1))
        )
    best_controls: dict[str, bool] = {net: False for net in controls}
    best_score = -1
    for candidate in control_candidates:
        assignment = {
            net: bool(value)
            for group, value in zip(control_groups, candidate)
            for net in group
        }
        # Evaluate the baseline and every sparse data probe in one packed
        # Boolean pass.  Replaying the complete cone once per probe made large
        # arithmetic nodes quadratic in practice and could exhaust memory
        # before semantic recovery even began.  Packing changes only the
        # execution representation; the exact same current-cone assignments
        # still determine the selected phase.
        responses = evaluate_batch(probes, assignment)
        baseline = responses[0]
        score = sum(
            (response ^ baseline).bit_count()
            for response in responses[1:]
        )
        if score > best_score:
            best_score = score
            best_controls = assignment
    return RecoveredWordNode(
        stem=target_stem,
        width=len(graph.targets),
        target_lsb=min(
            int(row.get("source_index", row["index"])) for row in graph.targets
        ),
        buses=buses,
        controls=controls,
        evaluator=evaluate,
        active_controls=best_controls,
        control_groups=control_groups,
        batch_evaluator=evaluate_batch,
    )


def composed_leaf_buses(
    root_stem: str, nodes: Mapping[str, RecoveredWordNode]
) -> dict[str, int]:
    leaves: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(stem: str) -> None:
        if stem in visiting:
            raise ValueError("registered closure word DAG contains a cycle")
        node = nodes.get(stem)
        if node is None:
            return
        visiting.add(stem)
        for bus in node.buses:
            child = nodes.get(bus.stem)
            if child is None:
                prior = leaves.get(bus.stem)
                if prior is not None and prior != bus.width:
                    raise ValueError("registered closure leaf width is inconsistent")
                leaves[bus.stem] = bus.width
            else:
                visit(child.stem)
        visiting.remove(stem)

    if root_stem not in nodes:
        raise ValueError("registered closure root is absent")
    visit(root_stem)
    return leaves


def composed_leaf_depths(
    root_stem: str, nodes: Mapping[str, RecoveredWordNode]
) -> dict[str, int]:
    """Return exact register-edge depth for every leaf in the word DAG."""
    depths: dict[str, set[int]] = {}

    def visit(stem: str, depth: int, visiting: set[str]) -> None:
        if stem in visiting:
            raise ValueError("registered closure word DAG contains a cycle")
        node = nodes.get(stem)
        if node is None:
            depths.setdefault(stem, set()).add(depth)
            return
        visiting.add(stem)
        for bus in node.buses:
            visit(bus.stem, depth + 1, visiting)
        visiting.remove(stem)

    visit(root_stem, 0, set())
    result = {}
    for stem, values in depths.items():
        # Arithmetic decompositions may carry low and high partial rows through
        # different registered cuts before reconvergence.  The latest arriving
        # row defines transaction alignment; formula replay below remains the
        # functional authority.
        result[stem] = max(values)
    return result


def composed_leaf_depth_ranges(
    root_stem: str, nodes: Mapping[str, RecoveredWordNode]
) -> dict[str, tuple[int, int]]:
    ranges: dict[str, list[int]] = {}

    def visit(stem: str, depth: int, visiting: set[str]) -> None:
        if stem in visiting:
            raise ValueError("registered closure word DAG contains a cycle")
        node = nodes.get(stem)
        if node is None:
            ranges.setdefault(stem, []).append(depth)
            return
        visiting.add(stem)
        for bus in node.buses:
            visit(bus.stem, depth + 1, visiting)
        visiting.remove(stem)

    visit(root_stem, 0, set())
    return {
        stem: (min(values), max(values)) for stem, values in ranges.items()
    }


def evaluate_composed_word(
    root_stem: str,
    nodes: Mapping[str, RecoveredWordNode],
    leaf_values: Mapping[str, int],
) -> int:
    visiting: set[str] = set()

    def evaluate(stem: str) -> int:
        if stem in visiting:
            raise ValueError("registered closure word DAG contains a cycle")
        node = nodes.get(stem)
        if node is None:
            return int(leaf_values.get(stem, 0))
        visiting.add(stem)
        inputs = {}
        for bus in node.buses:
            child = nodes.get(bus.stem)
            raw = evaluate(bus.stem)
            producer_lsb = child.target_lsb if child is not None else 0
            relative_lsb = bus.source_lsb - producer_lsb
            if relative_lsb < 0:
                raise ValueError("registered closure bus precedes producer slice")
            inputs[bus.key] = (raw >> relative_lsb) & (
                (1 << bus.width) - 1
            )
        result = node.evaluate(inputs)
        visiting.remove(stem)
        return result

    return evaluate(root_stem)


def infer_registered_bridge_plan(
    root_stem: str,
    nodes: Mapping[str, RecoveredWordNode],
    *,
    module_name: str,
    output_gate_polarity: str,
) -> dict:
    """Infer and replay a compact fixed-point plan from the composed word DAG."""
    root = nodes.get(root_stem)
    if root is None:
        raise ValueError("registered bridge root is absent")
    leaves = composed_leaf_buses(root_stem, nodes)
    leaf_depths = composed_leaf_depths(root_stem, nodes)
    leaf_depth_ranges = composed_leaf_depth_ranges(root_stem, nodes)
    baseline = evaluate_composed_word(root_stem, nodes, {})
    terms = []
    for stem, width in sorted(leaves.items()):
        samples = []
        for value in _sample_words(width, random.Random(width)):
            observed = (
                evaluate_composed_word(root_stem, nodes, {stem: value}) - baseline
            ) & ((1 << root.width) - 1)
            samples.append((value, observed))
        bus = {"stem": stem, "width": width, "source_lsb": 0, "bits": []}
        term = _infer_bus_term(bus, samples, root.width)
        if term is None:
            raise ValueError("composed leaf is not a supported fixed-point term")
        terms.append(term)
    product_terms = [term for term in terms if term["kind"] == "fixed_product_slice"]
    if len(product_terms) < 2 or len(product_terms) != len(terms):
        raise ValueError("registered bridge does not close over fixed products")
    shifts = {int(term["shift"]) for term in product_terms}
    widths = {int(term["data_width"]) for term in product_terms}
    if len(shifts) != 1 or len(widths) != 1 or next(iter(widths)) != root.width:
        raise ValueError("registered bridge products do not share one word contract")

    rng = random.Random(0xF1A26)
    mask = (1 << root.width) - 1
    for _ in range(256):
        values = {stem: rng.getrandbits(width) for stem, width in leaves.items()}
        predicted = baseline
        for term in terms:
            predicted += _term_value(term, values[term["source"]["stem"]])
        if predicted & mask != evaluate_composed_word(root_stem, nodes, values):
            raise ValueError("registered bridge formula failed composed replay")

    # The renderer uses signed Verilog products, so both positive and negative
    # coefficients require an explicit sign bit.
    coefficient_width = max(
        2,
        max(
            abs(int(term["coefficient"])).bit_length() + 1
            for term in product_terms
        ),
    )
    if coefficient_width > 27:
        raise ValueError("registered bridge coefficient exceeds DSP input contract")
    return {
        "version": 1,
        "module_name": module_name,
        "data_width": root.width,
        "coefficient_width": coefficient_width,
        "product_shift": next(iter(shifts)),
        "coefficients": [int(term["coefficient"]) for term in product_terms],
        "input_roles": [f"input_{index}" for index in range(len(product_terms))],
        "output_gate_polarity": output_gate_polarity,
        "output_gate_role": "output_gate",
        "recovery": {
            "leaf_stems": [term["source"]["stem"] for term in product_terms],
            "leaf_depths": [
                leaf_depths[term["source"]["stem"]] for term in product_terms
            ],
            "leaf_depth_ranges": [
                list(leaf_depth_ranges[term["source"]["stem"]])
                for term in product_terms
            ],
            "composed_node_count": len(nodes),
            "replay_vector_count": 256,
            "external_plan_used": False,
        },
    }


__all__ = [
    "ClosureRecord",
    "RecoveredWordNode",
    "RegisteredClosure",
    "SemanticWordRef",
    "WordBus",
    "WordSliceRef",
    "build_registered_product_bundle",
    "build_generating_operator_bridge_bundle",
    "merge_generating_operator_bridge_bundles",
    "namespace_generating_operator_bridge_bundle",
    "composed_leaf_buses",
    "composed_leaf_depths",
    "composed_leaf_depth_ranges",
    "evaluate_composed_word",
    "infer_registered_bridge_plan",
    "recover_word_node",
]
