"""Write the OIC modernisation analysis document.

The supplied ``.docx`` is used as the template: opening it inherits its styles,
page setup, headers and footers, so generated documents keep house branding.
The body is then rebuilt section by section from the IR.

Fields that cannot be derived from an export package (business owner,
criticality, and so on) are filled from the overrides file or, failing that,
left as a visible ``[to be confirmed]`` placeholder - never silently invented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from ..model.ir import Integration, Task
from ..parse.mapping import flow_string

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "analysis_template.docx"

PLACEHOLDER = "[to be confirmed]"

_KEY_VALUE_STYLE = "Grid Table 1 Light"
_TABLE_STYLE = "Table Grid"
_ACCENT_STYLE = "Grid Table 4 Accent 1"


def write_document(integration: Integration, path: Path,
                   template: Optional[Path] = None) -> Path:
    """Render ``integration`` to a .docx analysis document at ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    template = Path(template) if template else TEMPLATE
    doc = Document(str(template)) if template.exists() else Document()
    _clear_body(doc)

    meta = integration.meta
    _build(doc, integration, meta)

    doc.save(path)
    return path


# ------------------------------------------------------------------ sections

def _build(doc: Document, integration: Integration, meta) -> None:
    name = meta.display_name or meta.taskflow_name

    _para(doc, "Document Details:", bold=True)
    _kv_table(doc, [
        ("Document Type", "Process Document"),
        ("IICS Architecture", "Informatica Intelligent Cloud Services"),
        ("OIC Architecture", "Oracle Integration Cloud"),
        ("Integration Name", name),
        ("Business Process", _or_placeholder(meta.business_process, "IICS to OIC Migration")),
        ("Business Process Name", _or_placeholder(meta.business_process_name)),
        ("Upstream App Owner", _or_placeholder(meta.upstream_app_owner)),
        ("Downstream App Owner", _or_placeholder(meta.downstream_app_owner)),
    ], style=_KEY_VALUE_STYLE)

    _para(doc, "")
    _para(doc, "Version History:", bold=True)
    _grid(
        doc,
        ["Version", "Create / Modified Date", "Version Scope", "Owner/Changed by"],
        [[meta.version_label or "1.0", _date(meta.modified_date or meta.created_date),
          "1", meta.modified_by or meta.created_by or PLACEHOLDER]],
        style=_ACCENT_STYLE,
    )

    _para(doc,
          "Validity: The information submitted in this document will be valid "
          "for a period of 30 days from the Response Date",
          style="Body Text")

    doc.add_heading("IICS 'AS-IS' Status", level=1)
    doc.add_heading("Integration Purpose/Objective:", level=1)
    _para(doc, "Analysis and migration of all IICS (Informatica Intelligent Cloud "
               "Services) Integrations to OIC (Oracle Integration Cloud).")

    doc.add_heading("AS IS Analysis", level=2)

    doc.add_heading("Integration Identification", level=3)
    _kv_table(doc, [
        ("Attribute", "Details"),
        ("Integration Name / ID", name),
        ("Folder / Project", meta.project or PLACEHOLDER),
        ("Integration Type (Mapping / Taskflow / API / Process)",
         _or_placeholder(meta.integration_type, _derive_integration_type(integration))),
        ("Trigger Type (Schedule, Event or Ad Hoc)", _or_placeholder(meta.trigger_type)),
        ("Integration Pattern (Sync, Async, Batch or File)",
         _or_placeholder(meta.integration_pattern, _derive_pattern(integration))),
        ("Description / Business Purpose", _or_placeholder(meta.description)),
        ("Business Owner", _or_placeholder(meta.business_owner)),
        ("Technical Owner", _or_placeholder(meta.technical_owner)),
        ("Current Status", _or_placeholder(meta.current_status)),
        ("Criticality (High / Medium / Low)", _or_placeholder(meta.criticality)),
        ("Environment(s)", _or_placeholder(meta.environments)),
        ("Last Reviewed Date", _or_placeholder(meta.last_reviewed)),
    ], style=_TABLE_STYLE, header=True)

    doc.add_heading("Business process supported", level=3)
    _para(doc, _or_placeholder(meta.business_process_name, meta.project))

    doc.add_heading("Upstream Dependency", level=3)
    _grid(doc,
          ["#", "Task Name", "Source Asset", "Connection / Protocol",
           "Source Object / File / API", "Dependency Type", "Notes"],
          _upstream_rows(integration))

    doc.add_heading("Down Stream Dependency", level=3)
    _grid(doc,
          ["#", "Target System / Application", "Task Name", "Connection / Protocol",
           "Target Object / File / API", "Dependency Type", "Notes / Description"],
          _downstream_rows(integration))

    doc.add_heading("Source-to-Target Data Flow", level=3)
    _data_flow(doc, integration)

    doc.add_heading("Mapping / Transformation", level=3)
    _mapping_section(doc, integration)

    doc.add_heading("Connections and Security", level=3)
    _grid(doc,
          ["Connection", "Type", "Host / Endpoint", "Database / Path",
           "Schema / User", "Used by"],
          _connection_rows(integration))

    doc.add_heading("Schedule and Trigger", level=3)
    _para(doc, _or_placeholder(meta.schedule_note))

    doc.add_heading("Parameter/Configurations", level=3)
    _parameter_section(doc, integration)

    doc.add_heading("File / API / Database", level=3)
    _para(doc, _or_placeholder(meta.integration_type,
                               _derive_integration_type(integration)))

    doc.add_heading("Operational and Error Handling", level=3)
    for line in (meta.error_handling or PLACEHOLDER).splitlines():
        _para(doc, line)

    doc.add_heading("Monitoring and Alert", level=3)
    _monitoring_section(doc, integration)

    doc.add_heading("Failure Recovery Mechanism", level=3)
    for line in (meta.failure_recovery or PLACEHOLDER).splitlines():
        _para(doc, line)

    if integration.warnings:
        doc.add_heading("Parser Notes - Items Needing Review", level=3)
        for warning in integration.warnings:
            _para(doc, warning, bullet=True)


# --------------------------------------------------------------- row builders

def _upstream_rows(integration: Integration) -> List[List[str]]:
    """One row per thing this integration reads from.

    ``Source Asset`` is the asset the read happens through - the mapping for a
    mapping task, the connection for a file transfer - which is distinct from
    the task that runs it.
    """
    rows: List[List[str]] = []
    for task in _ordered_tasks(integration):
        if task.task_type == "MI_TASK":
            src = task.source
            if not src:
                continue
            protocol = [f"Source: {src.connection_display}"] if src.connection_display else []
            if src.archive_directory:
                protocol.append(f"Archive Directory: {src.archive_directory}")
            obj = []
            if src.directory:
                obj.append(f"Source Directory: {src.directory}")
            if src.pattern_label:
                obj.append(f"File Pattern: {src.pattern_label}")
            notes = [task.description] if task.description else []
            if src.after_pickup:
                notes.append(f"Source file after pickup: {src.after_pickup}")
            rows.append([str(len(rows) + 1), task.name,
                         src.connection_name or "Mass Ingestion",
                         "\n".join(protocol), "\n".join(obj), "File",
                         "\n".join(notes) or "-"])
            continue

        mapping = task.mapping
        if not mapping:
            continue
        for source in mapping.sources:
            obj = (f"Query: {source.custom_query}" if source.custom_query
                   else _qualified(source))
            rows.append([str(len(rows) + 1), task.name, mapping.name,
                         source.connection_display, obj or source.name,
                         "Source", task.description or "-"])
        for lookup in mapping.lookups:
            rows.append([str(len(rows) + 1), task.name, mapping.name,
                         lookup.connection_display, _qualified(lookup) or lookup.name,
                         "Lookup",
                         "Unconnected lookup" if lookup.lookup_unconnected else "Lookup"])
    return rows or [["1", PLACEHOLDER, "", "", "", "", ""]]


def _downstream_rows(integration: Integration) -> List[List[str]]:
    """One row per thing this integration writes to.

    ``Target System / Application`` is the platform on the receiving end - the
    connection type, which is what a migration plan is organised around.
    """
    rows: List[List[str]] = []
    for task in _ordered_tasks(integration):
        if task.task_type == "MI_TASK":
            tgt = task.target
            if not tgt:
                continue
            protocol = []
            if tgt.connection_type:
                protocol.append(f"Connection Type: {tgt.connection_type}")
            if tgt.connection_name:
                protocol.append(f"Connection: {tgt.connection_name}")
            notes = [task.description] if task.description else []
            if tgt.actions:
                notes.append("File operations: " + ", ".join(tgt.actions))
            notes.extend(tgt.action_properties)
            rows.append([
                str(len(rows) + 1), tgt.connection_type or "File", task.name,
                "\n".join(protocol),
                f"Target Directory: {tgt.directory}" if tgt.directory else "",
                f"If file exists: {tgt.file_exists_action}" if tgt.file_exists_action else "File",
                "\n".join(notes) or "-",
            ])
            continue

        mapping = task.mapping
        if not mapping:
            continue
        for target in mapping.targets:
            notes = [task.description] if task.description else []
            if target.write_operations:
                notes.append("Operation: " + ", ".join(target.write_operations))
            if target.update_strategy:
                notes.append(f"Update Strategy: {target.update_strategy}")
            if target.pre_sql:
                notes.append(f"Pre SQL: {target.pre_sql}")
            if target.post_sql:
                notes.append(f"Post SQL: {target.post_sql}")
            if target.file_format:
                notes.append(f"File format: {target.file_format}")
            if target.options:
                notes.append("Options: " + ", ".join(target.options))
            rows.append([
                str(len(rows) + 1), target.connection_type or "-", task.name,
                target.connection_display, _qualified(target) or target.name,
                "Target", "\n".join(notes) or "-",
            ])
    return rows or [["1", PLACEHOLDER, "", "", "", "", ""]]


def _qualified(tx) -> str:
    """``STAGING1.MOR_EMPLOYEES`` - the object, qualified by schema or folder."""
    name = tx.object_path or tx.object_name
    if name and tx.db_schema:
        return f"{tx.db_schema}.{name}"
    return name


def _connection_rows(integration: Integration) -> List[List[str]]:
    usage = {}
    for mapping in integration.mappings:
        for tx in mapping.transformations:
            if tx.connection_name:
                usage.setdefault(tx.connection_name, set()).add(mapping.name)
    for task in integration.tasks:
        for side in (task.source, task.target):
            if side and side.connection_name:
                usage.setdefault(side.connection_name, set()).add(task.name)

    rows = []
    for conn in integration.connections:
        rows.append([
            conn.name, conn.conn_type, conn.host or "-", conn.database or "-",
            conn.schema_label or "-",
            "\n".join(sorted(usage.get(conn.name, []))) or "-",
        ])
    return rows or [[PLACEHOLDER, "", "", "", "", ""]]


def _ordered_tasks(integration: Integration) -> List[Task]:
    """Tasks in taskflow order, then any not referenced by the taskflow."""
    seen, ordered = set(), []
    for step in integration.steps:
        if step.task and id(step.task) not in seen:
            seen.add(id(step.task))
            ordered.append(step.task)
    for task in integration.tasks:
        if id(task) not in seen:
            ordered.append(task)
    return ordered


# -------------------------------------------------------------- content bits

def _data_flow(doc: Document, integration: Integration) -> None:
    """Embed the mapping preview images Informatica ships in the package."""
    added = 0
    for step in integration.steps:
        mapping = step.task.mapping if step.task and step.task.mapping else None
        if not mapping or not mapping.preview_image:
            continue
        image = Path(mapping.preview_image)
        if not image.exists():
            continue
        _para(doc, f"{step.seq}. {step.title} - {mapping.name}", bold=True)
        try:
            doc.add_picture(str(image), width=Inches(6.2))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            added += 1
        except Exception:
            _para(doc, f"[preview image could not be embedded: {image.name}]")
    if not added:
        _para(doc, "No mapping preview images were present in the export package.")


def _mapping_section(doc: Document, integration: Integration) -> None:
    seen = set()
    for step in integration.steps:
        mapping = step.task.mapping if step.task and step.task.mapping else None
        if not mapping or mapping.name in seen:
            continue
        seen.add(mapping.name)
        label = f"{step.title}:"
        if mapping.description:
            label += f" {mapping.description}"
        _para(doc, label, bold=True)
        for line in flow_string(mapping).splitlines():
            _para(doc, line)
        for tx in mapping.transformations:
            if tx.expression_fields:
                _para(doc, f"{tx.name} ({tx.kind}):", italic=True)
                for f in tx.expression_fields:
                    _para(doc, f"{f.name} = {f.expression}", bullet=True)
    if not seen:
        _para(doc, "No mappings were present in the export package.")


def _parameter_section(doc: Document, integration: Integration) -> None:
    found = False
    for step in integration.steps:
        task = step.task
        if task and (task.in_out_parameters or task.parameter_file):
            found = True
            _para(doc, f"{step.title}: {task.name}", bold=True)
            if task.parameter_file:
                _para(doc, "Parameter File Name")
                _para(doc, task.parameter_file)
            for p in task.in_out_parameters:
                _para(doc, f"{p.name} = {p.value}", bullet=True)
        if step.step_type == "Assignment" and step.parameters:
            found = True
            _para(doc, f"{step.title} (Assignment)", bold=True)
            for p in step.parameters:
                _para(doc, f"{p.name} = {p.value}", bullet=True)
    if not found:
        _para(doc, PLACEHOLDER)


def _monitoring_section(doc: Document, integration: Integration) -> None:
    recipients = []
    for step in integration.steps:
        if step.step_type != "Notification Task":
            continue
        to = next((p.value for p in step.parameters if p.name == "Email_To"), "")
        subject = next((p.value for p in step.parameters if p.name == "Email_Subject"), "")
        recipients.append((step.seq, step.title, to, subject))

    if recipients:
        _grid(doc, ["Step", "Notification", "Recipients", "Subject"],
              [[s, t, to, subj] for s, t, to, subj in recipients])
    else:
        _para(doc, integration.meta.monitoring or PLACEHOLDER)


# ----------------------------------------------------------------- derivation

def _derive_pattern(integration: Integration) -> str:
    """Batch/file classification from the step types actually present."""
    types = {s.step_type for s in integration.steps}
    if "Mass Ingestion Task" in types:
        return "Batch / File"
    if "Mapping Task" in types:
        return "Batch"
    return ""


def _derive_integration_type(integration: Integration) -> str:
    kinds = {
        tx.kind for m in integration.mappings for tx in m.transformations
    }
    parts = ["Taskflow orchestrating mapping tasks"]
    if "Mass Ingestion Task" in {s.step_type for s in integration.steps}:
        parts.append("file ingestion")
    if kinds & {"Hierarchy Parser", "Hierarchy Builder"}:
        parts.append("hierarchical (XML/JSON) parsing")
    return ", ".join(parts)


# --------------------------------------------------------------------- writer

def _clear_body(doc: Document) -> None:
    """Remove template content, keeping styles, section and page setup."""
    body = doc.element.body
    for child in list(body):
        if child.tag.endswith("}sectPr"):
            continue
        body.remove(child)


def _para(doc: Document, text: str, *, bold: bool = False, italic: bool = False,
          bullet: bool = False, style: Optional[str] = None):
    p = doc.add_paragraph(style=_safe_style(doc, style or ("List Bullet" if bullet else None)))
    if bullet and p.style.name not in ("List Bullet", "List Paragraph"):
        text = f"• {text}"
    run = p.add_run(text)
    run.bold = bold
    run.italic = italic
    return p


def _safe_style(doc: Document, name: Optional[str]) -> Optional[str]:
    """Use a style only if the template defines it."""
    if not name:
        return None
    try:
        doc.styles[name]
        return name
    except KeyError:
        return None


def _kv_table(doc: Document, pairs: Sequence, style: str, header: bool = False):
    table = doc.add_table(rows=0, cols=2)
    _apply_style(doc, table, style)
    for i, (key, value) in enumerate(pairs):
        cells = table.add_row().cells
        _set_cell(cells[0], str(key), bold=True)
        _set_cell(cells[1], str(value), bold=header and i == 0)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    doc.add_paragraph()
    return table


def _grid(doc: Document, headers: Sequence[str], rows: Iterable[Sequence[str]],
          style: str = _TABLE_STYLE):
    table = doc.add_table(rows=1, cols=len(headers))
    _apply_style(doc, table, style)
    for cell, text in zip(table.rows[0].cells, headers):
        _set_cell(cell, text, bold=True)
    for values in rows:
        cells = table.add_row().cells
        for cell, text in zip(cells, values):
            _set_cell(cell, str(text or ""))
    doc.add_paragraph()
    return table


def _apply_style(doc: Document, table, style: str) -> None:
    try:
        table.style = doc.styles[style]
    except KeyError:
        try:
            table.style = doc.styles[_TABLE_STYLE]
        except KeyError:
            pass


def _set_cell(cell, text: str, bold: bool = False) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    lines = str(text).split("\n")
    for i, line in enumerate(lines):
        if i:
            paragraph = cell.add_paragraph()
        run = paragraph.add_run(line)
        run.bold = bold
        run.font.size = Pt(9)
        if line == PLACEHOLDER:
            run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            run.italic = True


def _or_placeholder(*values: str) -> str:
    for value in values:
        if value:
            return value
    return PLACEHOLDER


def _date(raw: str) -> str:
    return raw.split("T")[0] if raw else PLACEHOLDER
