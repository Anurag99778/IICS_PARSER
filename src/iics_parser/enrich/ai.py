"""Optional AI drafting for the narrative fields of the analysis document.

Everything structural comes from the export package. A handful of fields are
editorial - the business-purpose paragraph, a criticality call, the monitoring
and recovery notes - and those are what this module drafts, from a factual
summary of what the parser already found.

It is strictly optional. With no API key (or no ``anthropic`` package) the
pipeline runs unchanged and those fields stay as ``[to be confirmed]``.
Precedence is always overrides > AI draft > parsed value, so a human's entry is
never overwritten, and every AI-drafted field is recorded in
``Integration.warnings`` so reviewers can see what was not read off the export.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from ..model.ir import Integration

MODEL = "claude-opus-5"

#: Fields the model may draft, with the guidance shown to it.
DRAFTABLE = {
    "description": "One or two sentences on what this integration does, in business terms.",
    "integration_type": "Classification, e.g. 'Batch taskflow orchestrating mapping tasks with file delivery'.",
    "integration_pattern": "One of: Sync, Async, Batch, File, or a short combination.",
    "trigger_type": "One of: Schedule, Event, Ad Hoc - or 'Unknown' if the package gives no evidence.",
    "criticality": "High, Medium or Low, with no explanation.",
    "business_process_name": "The business process this supports, in a few words.",
    "monitoring": "How this integration is monitored and who is alerted, from its notification steps.",
    "failure_recovery": "How a failed run is recovered, from the error handling actually present.",
    "schedule_note": "What is known about scheduling. Say so plainly if the package shows none.",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "string"} for k in DRAFTABLE},
        "uncertain": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Field names you could not ground in the supplied facts.",
        },
    },
    "required": list(DRAFTABLE) + ["uncertain"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are documenting an Informatica IICS integration that is being migrated to "
    "Oracle Integration Cloud. You are given facts extracted from the integration's "
    "own export package. Fill in the requested documentation fields.\n\n"
    "Ground every answer in the supplied facts. Do not invent owners, team names, "
    "schedules, ticket numbers or environments - if the facts do not support a "
    "field, return an empty string for it and list its name in 'uncertain'. "
    "Being incomplete is correct; guessing is not."
)


def is_available() -> bool:
    """True if the SDK is installed and a credential is configured."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def enrich(integration: Integration, model: str = MODEL) -> Dict[str, str]:
    """Draft the narrative fields, applying them only where still empty.

    Returns the fields that were applied. Any failure is reported as a warning
    on the integration and leaves the document unchanged.
    """
    try:
        import anthropic
    except ImportError:
        integration.warnings.append(
            "AI enrichment skipped: the 'anthropic' package is not installed "
            "(pip install 'iics-parser[ai]')."
        )
        return {}

    facts = build_facts(integration)
    wanted = [f for f in DRAFTABLE if not getattr(integration.meta, f, "")]
    if not wanted:
        return {}

    prompt = (
        "Facts extracted from the export package:\n\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Fill in these documentation fields:\n"
        + "\n".join(f"- {f}: {DRAFTABLE[f]}" for f in wanted)
        + "\n\nReturn every field in the schema; use an empty string for any field "
          "the facts do not support, and name it in 'uncertain'."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        if response.stop_reason == "refusal":
            integration.warnings.append("AI enrichment declined by the model; fields left blank.")
            return {}
        text = next(b.text for b in response.content if b.type == "text")
        drafted = json.loads(text)
    except Exception as exc:                      # noqa: BLE001 - never fail the run
        integration.warnings.append(f"AI enrichment failed ({type(exc).__name__}: {exc}).")
        return {}

    uncertain = set(drafted.get("uncertain") or [])
    applied: Dict[str, str] = {}
    for field in wanted:
        value = (drafted.get(field) or "").strip()
        if value and field not in uncertain:
            setattr(integration.meta, field, value)
            applied[field] = value

    if applied:
        integration.warnings.append(
            "AI-drafted (verify before publishing): " + ", ".join(sorted(applied))
        )
    if uncertain:
        integration.warnings.append(
            "AI could not ground these fields; they need a human: "
            + ", ".join(sorted(uncertain))
        )
    return applied


def build_facts(integration: Integration) -> Dict:
    """A compact, factual summary of the integration for the model to work from."""
    meta = integration.meta
    steps: List[Dict] = []
    for step in integration.steps:
        entry = {
            "seq": step.seq,
            "name": step.title,
            "type": step.step_type,
            "task": step.task_name,
        }
        if step.task and step.task.description:
            entry["description"] = step.task.description
        if step.on_error:
            entry["on_error"] = step.on_error
        if step.step_type == "Notification Task":
            entry["notification"] = {
                p.name: p.value for p in step.parameters if p.name.startswith("Email")
            }
        mapping = step.task.mapping if step.task and step.task.mapping else None
        if mapping:
            entry["sources"] = [
                f"{t.connection_display}: {t.object_name}" for t in mapping.sources
            ]
            entry["targets"] = [
                f"{t.connection_display}: {t.object_name}" for t in mapping.targets
            ]
        steps.append(entry)

    return {
        "taskflow": meta.taskflow_name,
        "project": meta.project,
        "source_org": meta.source_org,
        "taskflow_description": meta.description,
        "created": f"{meta.created_date} by {meta.created_by}",
        "last_modified": f"{meta.modified_date} by {meta.modified_by}",
        "connections": [
            {"name": c.name, "type": c.conn_type, "host": c.host, "database": c.database}
            for c in integration.connections
        ],
        "steps": steps,
        "transformation_kinds": sorted({
            tx.kind for m in integration.mappings for tx in m.transformations
        }),
    }
