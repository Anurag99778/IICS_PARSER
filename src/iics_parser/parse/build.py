"""Assemble parsed assets into a single :class:`Integration`."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from ..extract.package import ExportPackage
from ..model.ir import Integration, Task
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

    _derive_defaults(integration)
    return integration


def _mapping_name_for(task_name: str) -> str:
    """``mct_FOO`` conventionally runs mapping ``m_FOO`` - a naming fallback."""
    return "m_" + task_name[4:] if task_name.startswith("mct_") else task_name


def _derive_defaults(integration: Integration) -> None:
    """Fill metadata that is safely derivable from the package itself."""
    meta = integration.meta

    # Only the taskflow's own description describes the integration. A step's
    # description describes that step, so it is left for AI or a human rather
    # than passed off as the integration's business purpose.

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
