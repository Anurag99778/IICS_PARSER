"""Parsers for the non-graph assets: connections, mapping tasks, mass ingestion."""

from __future__ import annotations

from typing import Dict, List, Optional

from ..extract.package import Asset
from ..model.ir import Connection, FileOperation, Task, TaskParameter


#: Internal connector names -> the label people use in the analysis documents.
_CONNECTION_TYPE_ALIASES = {
    "csvfile": "Flat File",
    "flatfile": "Flat File",
    "sqlserver": "SQL Server",
    "local": "Local Folder",
}


def connection_type_label(raw: str) -> str:
    """Normalise a connector type to its user-facing name."""
    return _CONNECTION_TYPE_ALIASES.get((raw or "").strip().lower(), raw or "")


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
        # instanceDisplayName is the user-facing type where IICS sets one
        # ("Advanced SFTP V2"); otherwise fall back to the internal type.
        conn_type=connection_type_label(
            obj.get("instanceDisplayName") or obj.get("type", "") or ""
        ),
        guid=obj.get("federatedId", "") or asset.guid,
        host=obj.get("host", "") or "",
        database=obj.get("database", "") or "",
        schema=obj.get("schema", "") or "",
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

    if str(obj.get("enableCrossSchemaPushdown", "")).lower() == "true":
        task.options.append("Cross-schema pushdown enabled")
    if str(obj.get("enableParallelRun", "")).lower() == "true":
        task.options.append("Parallel run enabled")

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

    raw_actions = data.get("taskActions") or []
    actions = [a.get("type", "") for a in raw_actions if a.get("type")]
    # Every property a file operation carries, whatever the action type - a
    # rename suffix, a PGP key, a compression format. Reading them generically
    # means an action this parser has never seen still reports its settings
    # instead of appearing as a bare verb.
    detail = [
        f"{action.get('type', 'Action')} · {key}: {value}"
        for action in raw_actions
        for key, value in sorted((action.get("properties") or {}).items())
        if value not in (None, "")
    ]
    src_opts = data.get("sourceOptions") or {}
    tgt_opts = data.get("targetOptions") or {}
    src_conn = data.get("sourceConnection") or {}
    tgt_conn = data.get("targetConnection") or {}

    if str(src_opts.get("filePatternFilter", "")).lower() == "true":
        task.options.append("File pattern filter enabled")
    if str(src_opts.get("fileStability", "")).lower() == "true":
        task.options.append("File stability check enabled")
    if str(data.get("allowConcurrency", "")).lower() == "true":
        task.options.append("Concurrent execution allowed")

    task.source = FileOperation(
        connection_name=src_conn.get("name", "") or "",
        connection_type=_conn_type(src_conn),
        directory=src_opts.get("src.download.path", "") or "",
        file_pattern=src_opts.get("src.file.pattern", "") or "",
        file_pattern_type=_titlecase(src_opts.get("src.file.pattern.type", "")),
        batch_size=str(src_opts.get("batchSize", "") or ""),
        archive_directory=src_opts.get("src.archive.dir", "") or "",
        after_pickup=_titlecase(src_opts.get("src.file.delete", "")),
    )
    task.target = FileOperation(
        connection_name=tgt_conn.get("name", "") or "",
        connection_type=_conn_type(tgt_conn),
        directory=tgt_opts.get("tgt.download.path", "") or "",
        file_exists_action=_titlecase(tgt_opts.get("fileExistsAction", "")),
        actions=actions,
        action_properties=detail,
    )
    return task


def _conn_type(conn: dict) -> str:
    return connection_type_label(conn.get("type", "") or "")


def _titlecase(value: str) -> str:
    return value.capitalize() if value.isupper() else value


def build_connection_index(connections: List[Connection]) -> Dict[str, Connection]:
    """GUID -> connection, for resolving ``dataAdapter.connectionId``."""
    return {c.guid: c for c in connections if c.guid}
