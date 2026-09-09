"""Assemble parsed assets into a single :class:`Integration`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from ..extract.package import ExportPackage
from ..model.ir import Integration, Step, Task
from . import assets as asset_parsers
from .mapping import parse_mapping
from .taskflow import parse_taskflow


def build_integration(package: ExportPackage,
                      image_dir: Optional[Path] = None) -> Integration:
    """Parse every asset in ``package`` and wire the references together."""
    integration = Integration()
    integration.warnings.extend(package.warnings)
    warnings = integration.warnings

    # 1. Connections first - mappings resolve their adapters against these.
    for asset in package.by_type("Connection"):
        conn = asset_parsers.parse_connection(asset)
        if conn:
            integration.connections.append(conn)
        else:
            warnings.append(f"{asset.name}: could not read connection.json")
    conn_index = asset_parsers.build_connection_index(integration.connections)

    # 2. Mappings (the transformation graphs).
    mappings_by_guid: Dict[str, "object"] = {}
    mappings_by_name: Dict[str, "object"] = {}
    for asset in package.by_type("DTEMPLATE"):
        mapping = parse_mapping(asset, conn_index, image_dir, warnings)
        integration.mappings.append(mapping)
        if mapping.guid:
            mappings_by_guid[mapping.guid] = mapping
        mappings_by_name[mapping.name] = mapping

    # 3. Tasks, linked to their mapping.
    tasks_by_name: Dict[str, Task] = {}
    for asset in package.by_type("MTT"):
        task = asset_parsers.parse_mapping_task(asset)
        if not task:
            warnings.append(f"{asset.name}: could not read mtTask.json")
            continue
        ref = getattr(task, "_mapping_ref", "")
        task.mapping = mappings_by_guid.get(ref) or mappings_by_name.get(
            _mapping_name_for(task.name)
        )
        if task.mapping is None and ref:
            warnings.append(
                f"{task.name}: referenced mapping {ref} not present in package"
            )
        integration.tasks.append(task)
        tasks_by_name[task.name] = task

    for asset in package.by_type("MI_TASK"):
        task = asset_parsers.parse_mass_ingestion(asset)
        if not task:
            warnings.append(f"{asset.name}: could not read MI_TASK payload")
            continue
        integration.tasks.append(task)
        tasks_by_name[task.name] = task

    # 4. Taskflow - gives execution order and step-level parameters.
    taskflows = package.by_type("TASKFLOW")
    if len(taskflows) > 1:
        warnings.append(
            "Package contains %d taskflows (%s); only '%s' was analysed - "
            "export the others separately"
            % (len(taskflows), ", ".join(t.name for t in taskflows), taskflows[0].name)
        )
    if taskflows:
        meta, steps = parse_taskflow(taskflows[0].path, warnings)
        integration.meta = meta
        integration.steps = steps
        for step in steps:
            if step.task_name:
                step.task = tasks_by_name.get(step.task_name)
                if step.task is None:
                    warnings.append(
                        f"Step '{step.title}': task {step.task_name} not in package"
                    )

    integration.meta.project = package.project_name
    integration.meta.folder = package.project_name
    integration.meta.source_org = package.metadata.get("sourceOrgName", "")
    if not integration.meta.taskflow_name and taskflows:
        integration.meta.taskflow_name = taskflows[0].name

    # 5. A package with no taskflow still has to be documented.
    if not integration.steps:
        _document_without_a_taskflow(integration, warnings)
        if not integration.meta.taskflow_name:
            integration.meta.taskflow_name = _package_name(package, integration)

    _derive_defaults(integration)
    return integration


def _document_without_a_taskflow(integration: Integration,
                                 warnings: List[str]) -> None:
    """Give a package with no taskflow something to document.

    IICS exports a single asset as readily as a whole orchestration, so a
    package is often just one mapping, or a mapping task and the mapping it
    runs, with nothing to sequence them. Every renderer works from steps, so
    without this the workbook and the document come out empty - which is
    exactly what a single-mapping export used to produce.

    The assets are listed in name order. That is not execution order, and the
    Parse Report says so rather than leaving a reader to assume otherwise.
    """
    step_types = {"MCT": "Mapping Task", "MI_TASK": "Mass Ingestion Task"}
    steps: List[Step] = []

    for task in sorted(integration.tasks, key=lambda t: t.name):
        steps.append(Step(
            seq="", title=task.name,
            step_type=step_types.get(task.task_type, task.task_type or "Task"),
            task_name=task.name, task=task,
        ))

    # A mapping exported on its own has no task to run it. Wrap it so the
    # renderers see the shape they see everywhere else; the wrapper is
    # scaffolding, not an asset, so it is deliberately not added to
    # integration.tasks - the Parse Report should still say zero tasks.
    already = {id(t.mapping) for t in integration.tasks if t.mapping}
    for mapping in sorted(integration.mappings, key=lambda m: m.name):
        if id(mapping) in already:
            continue
        steps.append(Step(
            seq="", title=mapping.name, step_type="Mapping",
            task=Task(name=mapping.name, task_type="MAPPING", guid=mapping.guid,
                      description=mapping.description, mapping=mapping),
        ))

    if not steps:
        return

    for number, step in enumerate(steps, start=1):
        step.seq = str(number)
    integration.steps = steps

    warnings.append(
        f"This package contains no taskflow, so there is no execution order to "
        f"report. The {len(steps)} asset(s) it does contain are documented "
        f"below in name order - do not read that as the order they run in."
    )


def _package_name(package: ExportPackage, integration: Integration) -> str:
    """What to call a package that has no taskflow to take a name from.

    IICS stamps the export with the name of whatever was selected, suffixed
    with a timestamp, so that is the most faithful answer available.
    """
    exported = str(package.metadata.get("name", "") or "")
    exported = re.sub(r"-\d{10,}$", "", exported)
    if exported:
        return exported
    if len(integration.tasks) == 1:
        return integration.tasks[0].name
    if len(integration.mappings) == 1:
        return integration.mappings[0].name
    return package.project_name or ""


def _mapping_name_for(task_name: str) -> str:
    """``mct_FOO`` conventionally runs mapping ``m_FOO`` - a naming fallback."""
    return "m_" + task_name[4:] if task_name.startswith("mct_") else task_name


def _derive_defaults(integration: Integration) -> None:
    """Fill metadata that is safely derivable from the package itself."""
    meta = integration.meta

    # Only the taskflow's own description describes the integration. A step's
    # description describes that step, so it is left for AI or a human rather
    # than passed off as the integration's business purpose.
    #
    # The exception is a package that is a single asset: there the asset's
    # description is not one step's description among many, it is the whole of
    # what this package does.
    if not meta.description and len(integration.steps) == 1:
        only = integration.steps[0]
        described = only.task.description if only.task else ""
        meta.description = described or (
            only.task.mapping.description if only.task and only.task.mapping else ""
        )

    if not meta.display_name:
        meta.display_name = meta.taskflow_name

    # Error handling is stated by the taskflow's fault handlers.
    modes = {s.on_error for s in integration.steps if s.on_error}
    if modes and not meta.error_handling:
        meta.error_handling = "\n".join(sorted(modes))

    if not meta.current_status:
        meta.current_status = (
            "Active" if meta.publication_status == "published" else meta.publication_status
        )
