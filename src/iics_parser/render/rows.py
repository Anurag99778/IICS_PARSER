"""Turn the IR into the flat rows the analysis documents are made of.

Keeping this separate from the writers means the Excel sheets, the Word tables
and any future renderer all describe the integration the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..model.ir import Integration, Mapping, Step, Transformation
from ..parse.mapping import flow_string

MAPPING_DETAIL_COLUMNS = [
    "S. No", "Taskflow", "TYPE", "SubTask", "Mapping Name",
    "Input fields", "Values", "Path",
    "Source Connection/ Connection Type", "Source Object",
    "Lookup/ Filter (if Any)",
    "Target Connection/ Connection Type", "Target Object",
    "Notes", "Flow",
]

FIELD_LEVEL_COLUMNS = [
    "S. No", "Taskflow", "TYPE", "SubTask", "Mapping Name",
    "Input fields", "Values", "Path", "Notes",
]


@dataclass
class ObjectPair:
    """One source -> target leg of a step."""

    source_connection: str = ""
    source_object: str = ""
    lookup_filter: str = ""
    target_connection: str = ""
    target_object: str = ""
    notes: str = ""


@dataclass
class ParamRow:
    name: str = ""
    value: str = ""
    path: str = ""


# ---------------------------------------------------------------- sheet one

def mapping_detail_rows(integration: Integration) -> List[List[str]]:
    """Rows for the 'Mapping Details' sheet, one per source/target leg."""
    rows: List[List[str]] = []
    for step in integration.steps:
        pairs = _object_pairs(step)
        params = _param_rows(step)
        mapping = step.task.mapping if step.task and step.task.mapping else None
        flow = flow_string(mapping) if mapping else ""
        notes = _step_notes(step)

        count = max(len(pairs), len(params), 1)
        for i in range(count):
            pair = pairs[i] if i < len(pairs) else ObjectPair()
            param = params[i] if i < len(params) else ParamRow()
            rows.append([
                step.seq if i == 0 else "",
                step.title if i == 0 else "",
                step.step_type if i == 0 else "",
                step.task_name if i == 0 else "",
                step.mapping_name if i == 0 else "",
                param.name,
                param.value,
                param.path,
                pair.source_connection,
                pair.source_object,
                pair.lookup_filter,
                pair.target_connection,
                pair.target_object,
                _join(notes if i == 0 else "", pair.notes),
                flow if i == 0 else "",
            ])
    return rows


def _object_pairs(step: Step) -> List[ObjectPair]:
    """Source/target legs for a step.

    Mapping tasks pair each source with the targets reachable from it in the
    mapping graph, so a Router feeding two targets produces two rows - matching
    how the analysis is written by hand.
    """
    task = step.task
    if task is None:
        return []

    if task.task_type == "MI_TASK":
        src, tgt = task.source, task.target
        if not src and not tgt:
            return []
        notes = []
        if src and src.file_pattern:
            notes.append(f"File pattern: {src.file_pattern}")
        if src and src.archive_directory:
            notes.append(f"Archive directory: {src.archive_directory}")
        if tgt and tgt.file_exists_action:
            notes.append(f"If file exists: {tgt.file_exists_action}")
        if tgt and tgt.actions:
            notes.append("Action: " + ", ".join(tgt.actions))
        return [ObjectPair(
            source_connection=src.connection_display if src else "",
            source_object=src.directory if src else "",
            target_connection=tgt.connection_display if tgt else "",
            target_object=tgt.directory if tgt else "",
            notes="\n".join(notes),
        )]

    mapping = task.mapping
    if mapping is None:
        return []

    # Mapping-wide detail (lookups, router/filter steps) belongs on the step's
    # first row; a source's own filter belongs on that source's row.
    mapping_note = _lookup_filter_note(mapping)
    reach = _reachable_targets(mapping)
    pairs: List[ObjectPair] = []

    for source in mapping.sources:
        source_note = _source_filter_note(source)
        lookup_cell = _join(mapping_note if not pairs else "", source_note)
        targets = reach.get(source.name) or []
        if not targets:
            pairs.append(ObjectPair(
                source_connection=source.connection_display,
                source_object=_object_label(source),
                lookup_filter=lookup_cell,
                notes=_transform_notes(source),
            ))
            continue
        for j, target in enumerate(targets):
            pairs.append(ObjectPair(
                source_connection=source.connection_display if j == 0 else "",
                source_object=_object_label(source) if j == 0 else "",
                lookup_filter=lookup_cell if j == 0 else "",
                target_connection=target.connection_display,
                target_object=_object_label(target),
                notes=_join(_transform_notes(source) if j == 0 else "",
                            _transform_notes(target)),
            ))

    # Targets not fed by any source (rare, but never drop them silently).
    covered = {t.name for ts in reach.values() for t in ts}
    for target in mapping.targets:
        if target.name not in covered:
            pairs.append(ObjectPair(
                target_connection=target.connection_display,
                target_object=_object_label(target),
                notes=_transform_notes(target),
            ))

    if not pairs and lookup_note:
        pairs.append(ObjectPair(lookup_filter=lookup_note))
    return pairs


def _reachable_targets(mapping: Mapping) -> Dict[str, List[Transformation]]:
    """Targets reachable from each source, following the mapping links."""
    by_name = {t.name: t for t in mapping.transformations}
    adjacency: Dict[str, List[str]] = {}
    for link in mapping.links:
        adjacency.setdefault(link["from"], []).append(link["to"])

    out: Dict[str, List[Transformation]] = {}
    for source in mapping.sources:
        found: List[Transformation] = []
        seen = set()
        stack = list(adjacency.get(source.name, []))
        while stack:
            name = stack.pop(0)
            if name in seen:
                continue
            seen.add(name)
            node = by_name.get(name)
            if node is None:
                continue
            if node.kind == "Target":
                found.append(node)
            stack.extend(adjacency.get(name, []))
        out[source.name] = found
    return out


def _object_label(tx: Transformation) -> str:
    """Object name, or the custom query when the source is a SQL override."""
    if tx.custom_query:
        return f"Query: {tx.custom_query}"
    return tx.object_name or tx.name


def _source_filter_note(source: Transformation) -> str:
    """A source's own read-time filter and ordering."""
    parts = []
    if source.filter_condition:
        parts.append(f"Filter: {source.filter_condition}")
    if source.advanced_filter:
        parts.append(f"Filter: {source.advanced_filter}")
    if source.sort_fields:
        parts.append("Sorted by: " + ", ".join(source.sort_fields))
    return "\n".join(parts)


def _lookup_filter_note(mapping: Mapping) -> str:
    """Mapping-wide lookups, filter steps and router conditions.

    Source-attached filters are excluded - they are reported on their own row
    by :func:`_source_filter_note` so each leg reads independently.
    """
    parts: List[str] = []
    for lkp in mapping.lookups:
        detail = [f"Lookup: {lkp.name}"]
        if lkp.connection_display:
            detail.append(f"Connection: {lkp.connection_display}")
        if lkp.object_name:
            detail.append(f"Lookup Object: {lkp.object_name}")
        if lkp.lookup_conditions:
            detail.append("Condition: " + " AND ".join(str(c) for c in lkp.lookup_conditions))
        if lkp.lookup_return_field:
            detail.append(f"Return: {lkp.lookup_return_field}")
        if lkp.lookup_multiple_match:
            detail.append(f"Multiple Match: {lkp.lookup_multiple_match}")
        if lkp.lookup_unconnected:
            detail.append("Unconnected")
        parts.append("\n".join(detail))

    for tx in mapping.transformations:
        if tx.kind == "Source":
            continue                       # reported on that source's own row
        if tx.filter_condition:
            parts.append(f"Filter ({tx.name}): {tx.filter_condition}")
        if tx.advanced_filter:
            parts.append(f"Filter ({tx.name}): {tx.advanced_filter}")
        for cond in tx.group_expressions:
            parts.append(f"{tx.kind} ({tx.name}): {cond}")

    # Transformations that shape the data without conditions of their own still
    # belong in the analysis - an Aggregator or Sorter changes the output.
    shaping = [tx.name for tx in mapping.transformations
               if tx.kind in ("Aggregator", "Sorter", "Joiner", "Union",
                              "Normalizer", "Rank", "Deduplicate")]
    if shaping:
        parts.append("Transformations: " + ", ".join(shaping))
    return "\n".join(parts)


def _transform_notes(tx: Transformation) -> str:
    parts = []
    if tx.pre_sql:
        parts.append(f"Pre SQL: {tx.pre_sql}")
    if tx.post_sql:
        parts.append(f"Post SQL: {tx.post_sql}")
    if tx.write_operations:
        parts.append("Operation: " + ", ".join(tx.write_operations))
    if tx.truncate_target:
        parts.append("Truncate target: Yes")
    if tx.update_columns:
        parts.append("Update Columns: " + ", ".join(tx.update_columns))
    if tx.sort_fields:
        parts.append("Sorted by: " + ", ".join(tx.sort_fields))
    return "\n".join(parts)


def _step_notes(step: Step) -> str:
    parts = []
    if step.task and step.task.description:
        parts.append(step.task.description.strip())
    if step.task and step.task.parameter_file:
        parts.append(f"Parameter file: {step.task.parameter_file}")
    if step.branch:
        parts.append(f"Path: {step.branch}")
    if step.on_error:
        parts.append(step.on_error)
    return "\n".join(dict.fromkeys(p for p in parts if p))


def _param_rows(step: Step) -> List[ParamRow]:
    """Step parameters plus the task's in-out parameters."""
    rows = [ParamRow(p.name, p.value, p.path) for p in step.parameters]
    if step.task:
        for p in step.task.in_out_parameters:
            rows.append(ParamRow(
                p.name, p.value,
                f"{step.task_name} -> Inout -> {p.name}",
            ))
    return rows


# ---------------------------------------------------------------- sheet two

def field_level_rows(integration: Integration) -> List[List[str]]:
    """Rows for the 'Field level mapping' sheet: expressions and parameters."""
    rows: List[List[str]] = []
    for step in integration.steps:
        entries: List[Tuple[str, str, str, str]] = []

        for p in _param_rows(step):
            entries.append((p.name, p.value, p.path, ""))

        mapping = step.task.mapping if step.task and step.task.mapping else None
        if mapping:
            for tx in mapping.transformations:
                for f in tx.expression_fields:
                    note = tx.name if tx.kind == "Expression" else f"{tx.kind}: {tx.name}"
                    if f.field_type == "VARIABLE":
                        note = _join(note, "Variable field")
                    entries.append((f.name, f.expression, "", note))

        if not entries:
            continue

        for i, (name, value, path, note) in enumerate(entries):
            rows.append([
                step.seq if i == 0 else "",
                step.title if i == 0 else "",
                step.step_type if i == 0 else "",
                step.task_name if i == 0 else "",
                step.mapping_name if i == 0 else "",
                name, value, path, note,
            ])
    return rows


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)
