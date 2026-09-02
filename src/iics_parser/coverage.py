"""Prove how much of a package reached the output.

The warnings list says what the parser *knows* it struggled with. That is not
the same as completeness: a whole category could be missed without any single
step failing, and nobody would see it.

So after rendering, every object found in the package is checked against the
rows actually produced. Anything present but unrepresented is named. The point
is that a reviewer opening an integration they have never seen can tell, from
the workbook alone, whether the analysis is complete - without trusting the
tool's own opinion of itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from .model.ir import Integration


@dataclass
class CoverageLine:
    """One category of object: how many existed, how many reached the output."""

    category: str
    present: int
    covered: int
    missing: List[str] = field(default_factory=list)
    note: str = ""

    @property
    def complete(self) -> bool:
        return self.present == self.covered

    @property
    def summary(self) -> str:
        if self.present == 0:
            return "none in package"
        if self.complete:
            return f"{self.covered}/{self.present} — complete"
        return f"{self.covered}/{self.present} — {self.present - self.covered} not in output"


def audit(integration: Integration,
          detail_rows: Sequence[Sequence[str]],
          field_rows: Sequence[Sequence[str]]) -> List[CoverageLine]:
    """Compare what the package holds against what the sheets show."""
    detail_text = _text_of(detail_rows)
    field_text = _text_of(field_rows)
    both = detail_text | field_text

    lines: List[CoverageLine] = []

    # 1. Taskflow steps - each must own a row.
    step_seqs = {r[0] for r in detail_rows if r and r[0]}
    missing_steps = [f"{s.seq} {s.title}" for s in integration.steps
                     if s.seq not in step_seqs]
    lines.append(CoverageLine("Taskflow steps", len(integration.steps),
                              len(integration.steps) - len(missing_steps), missing_steps))

    # 2. Tasks referenced by the taskflow must resolve to an asset.
    referenced = [s for s in integration.steps if s.task_name]
    unresolved = [f"{s.seq} {s.title} → {s.task_name}" for s in referenced if s.task is None]
    lines.append(CoverageLine("Task references resolved", len(referenced),
                              len(referenced) - len(unresolved), unresolved))

    # 3. Mappings - an unused one is either a dead asset or a link we missed.
    used = {s.task.mapping.name for s in integration.steps
            if s.task and s.task.mapping}
    orphan_mappings = [m.name for m in integration.mappings if m.name not in used]
    lines.append(CoverageLine(
        "Mappings reached by a step", len(integration.mappings),
        len(integration.mappings) - len(orphan_mappings), orphan_mappings,
        note="an unreached mapping is exported but never called - verify it is dead"))

    # 4. Sources and targets must be named somewhere in the sheets.
    endpoints, missing_endpoints = [], []
    for mapping in integration.mappings:
        for tx in mapping.sources + mapping.targets:
            label = tx.object_name or tx.name
            endpoints.append(label)
            if label and label not in both and (tx.custom_query or "") not in both:
                missing_endpoints.append(f"{mapping.name} · {tx.kind} {label}")
    lines.append(CoverageLine("Source/target objects", len(endpoints),
                              len(endpoints) - len(missing_endpoints), missing_endpoints))

    # 5. Every computed field should appear on the field-level sheet.
    exprs, missing_exprs = [], []
    for mapping in integration.mappings:
        for tx in mapping.transformations:
            for f in tx.expression_fields:
                exprs.append(f.name)
                if f.name not in field_text:
                    missing_exprs.append(f"{mapping.name} · {tx.name} · {f.name}")
    lines.append(CoverageLine("Expression / variable fields", len(exprs),
                              len(exprs) - len(missing_exprs), missing_exprs))

    # 6. Column lineage.
    pairs, missing_pairs = [], []
    for mapping in integration.mappings:
        for tx in mapping.targets:
            for fm in tx.field_mappings:
                pairs.append(str(fm))
                if fm.from_field not in field_text or fm.to_field not in field_text:
                    missing_pairs.append(f"{mapping.name} · {fm}")
    lines.append(CoverageLine("Target column mappings", len(pairs),
                              len(pairs) - len(missing_pairs), missing_pairs))

    # 7. Connections. One never mentioned is exported but unused by this flow.
    unused_conns = [c.name for c in integration.connections if c.name not in both]
    lines.append(CoverageLine(
        "Connections referenced", len(integration.connections),
        len(integration.connections) - len(unused_conns), unused_conns,
        note="an unreferenced connection may belong to a step that failed to parse"))

    # 8. Mapping diagrams for the Word document.
    with_preview = [m for m in integration.mappings if m.preview_image]
    lines.append(CoverageLine("Mapping diagrams extracted", len(integration.mappings),
                              len(with_preview),
                              [m.name for m in integration.mappings if not m.preview_image]))

    # 9. Transformations that shape data but carry no condition of their own -
    #    easy to lose, so confirm each is named somewhere.
    shaping, missing_shaping = [], []
    for mapping in integration.mappings:
        for tx in mapping.transformations:
            if tx.kind in ("Source", "Target"):
                continue
            shaping.append(tx.name)
            if tx.name not in both:
                missing_shaping.append(f"{mapping.name} · {tx.kind} {tx.name}")
    lines.append(CoverageLine("Intermediate transformations", len(shaping),
                              len(shaping) - len(missing_shaping), missing_shaping))

    return lines


def overall(lines: Sequence[CoverageLine]) -> Dict[str, int]:
    present = sum(l.present for l in lines)
    covered = sum(l.covered for l in lines)
    return {
        "present": present,
        "covered": covered,
        "percent": round(100 * covered / present) if present else 100,
        "incomplete_categories": sum(1 for l in lines if not l.complete),
    }


def _text_of(rows: Sequence[Sequence[str]]) -> set:
    """Every distinct token appearing in a set of rows, for containment checks."""
    out = set()
    for row in rows:
        for cell in row:
            if not cell:
                continue
            text = str(cell)
            out.add(text)
            for line in text.splitlines():
                out.add(line.strip())
                # Cells combine several facts; index the pieces too.
                for part in line.replace("->", " ").replace("→", " ").split():
                    out.add(part.strip(" ,;:·()"))
    return out
