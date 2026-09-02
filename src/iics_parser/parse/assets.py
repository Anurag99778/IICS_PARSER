"""Parsers for the non-graph assets: connections, mapping tasks, mass ingestion."""

from __future__ import annotations

from typing import Dict, List, Optional

from ..extract.package import Asset
from ..model.ir import Connection, FileOperation, Task, TaskParameter


def parse_connection(asset: Asset) -> Optional[Connection]:
    """Read ``connection.json`` from an expanded ``cn_*.Connection`` asset."""
    data = asset.load_json("connection.json")
    if data is None:
        return None
    obj = data[0] if isinstance(data, list) and data else data
    if not isinstance(obj, dict):
        return None

    return Connection(
        name=obj.get("name", asset.name),
        # instanceDisplayName is the user-facing type ("Flat File"); `type` is
        # the internal one. Prefer the display name where present.
        conn_type=obj.get("instanceDisplayName") or obj.get("type", "") or "",
        guid=obj.get("federatedId", "") or asset.guid,
        host=obj.get("host", "") or "",
        database=obj.get("database", "") or "",
        username=obj.get("username", "") or "",
        runtime_environment=str(obj.get("runtimeEnvironmentId", "") or "").lstrip("@"),
    )


def parse_mapping_task(asset: Asset) -> Optional[Task]:
    """Read ``mtTask.json`` from an expanded ``mct_*.MTT`` asset."""
    data = asset.load_json("mtTask.json")
    if data is None:
        return None
    obj = data[0] if isinstance(data, list) and data else data
    if not isinstance(obj, dict):
        return None

    task = Task(
        name=obj.get("name", asset.name),
        task_type="MCT",
        description=obj.get("description", "") or asset.description,
        guid=obj.get("frsGuid", "") or asset.guid,
        parameter_file=obj.get("parameterFileName", "") or "",
        runtime_environment=str(obj.get("runtimeEnvironmentId", "") or "").lstrip("@"),
    )

    for p in obj.get("inOutParameters") or []:
        task.in_out_parameters.append(
            TaskParameter(
                name=p.get("name", ""),
                value=p.get("currentValue", "") or p.get("initialValue", "") or "",
                source="inout",
            )
        )

    # The mapping this task runs, as a GUID reference (@<guid>).
    task._mapping_ref = str(obj.get("mappingId", "") or "").lstrip("@")  # type: ignore[attr-defined]
    return task


def parse_mass_ingestion(asset: Asset) -> Optional[Task]:
    """Read a ``fit_*.MI_TASK.dat`` file (plain JSON despite the extension)."""
    data = asset.load_json(asset.path.name)
    if data is None:
        # MI_TASK assets are files, not directories - load directly.
        try:
            import json
            with asset.path.open(encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None

    task = Task(
        name=data.get("name", asset.name),
        task_type="MI_TASK",
        description=(data.get("description") or asset.description or "").strip(),
        guid=data.get("icsGuid", "") or asset.guid,
        runtime_environment=data.get("agentGroup", "") or "",
        schedule=data.get("schedule", "") or "",
    )

    actions = [a.get("type", "") for a in (data.get("taskActions") or []) if a.get("type")]
    src_opts = data.get("sourceOptions") or {}
    tgt_opts = data.get("targetOptions") or {}
    src_conn = data.get("sourceConnection") or {}
    tgt_conn = data.get("targetConnection") or {}

    task.source = FileOperation(
        connection_name=src_conn.get("name", "") or "",
        connection_type=_conn_type(src_conn),
        directory=src_opts.get("src.download.path", "") or "",
        file_pattern=src_opts.get("src.file.pattern", "") or "",
        archive_directory=src_opts.get("src.archive.dir", "") or "",
    )
    task.target = FileOperation(
        connection_name=tgt_conn.get("name", "") or "",
        connection_type=_conn_type(tgt_conn),
        directory=tgt_opts.get("tgt.download.path", "") or "",
        file_exists_action=_titlecase(tgt_opts.get("fileExistsAction", "")),
        actions=actions,
    )
    return task


def _conn_type(conn: dict) -> str:
    """``local`` is IICS's internal name for a local folder."""
    t = conn.get("type", "") or ""
    return "Local Folder" if t.lower() == "local" else t


def _titlecase(value: str) -> str:
    return value.capitalize() if value.isupper() else value


def build_connection_index(connections: List[Connection]) -> Dict[str, str]:
    """GUID -> connection name, for resolving ``dataAdapter.connectionId``."""
    return {c.guid: c.name for c in connections if c.guid}
