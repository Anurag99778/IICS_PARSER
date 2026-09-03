"""Intermediate representation for a parsed IICS integration.

Every parser writes into these objects and every renderer reads out of them, so
the Excel and Word outputs can never drift apart. The IR is plain dataclasses
with ``to_dict`` so it can be dumped to JSON, diffed between runs and asserted
on in tests without going near a spreadsheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


def _clean(value: Any) -> Any:
    """Drop empty values so serialized IR stays readable."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


@dataclass
class Connection:
    """A connection asset from ``/SYS`` (Oracle, Flat File, SFTP, ...)."""

    name: str
    conn_type: str = ""
    guid: str = ""
    host: str = ""
    database: str = ""
    username: str = ""
    runtime_environment: str = ""

    @property
    def display(self) -> str:
        """``cn_STAGING1 (Oracle)`` - how connections appear in the analysis."""
        return f"{self.name} ({self.conn_type})" if self.conn_type else self.name


@dataclass
class ExpressionField:
    """One output/variable field of an Expression transformation."""

    name: str
    expression: str
    field_type: str = ""          # OUTPUT / VARIABLE / INPUT
    data_type: str = ""
    precision: Optional[int] = None
    scale: Optional[int] = None


@dataclass
class TransformationField:
    """One column of a source or target object, as the designer's Fields grid
    shows it: name, type, precision, scale and the object it originates from."""

    name: str
    data_type: str = ""          # platform type - "string", "date/time", ...
    precision: Optional[int] = None
    scale: Optional[int] = None
    native_type: str = ""        # the database type, e.g. nvarchar
    nullable: bool = True
    is_key: bool = False
    origin: str = ""             # the object this column comes from
    mapped_from: str = ""        # for targets: the incoming field wired to it


@dataclass
class FieldMapping:
    """One incoming field wired to one target column.

    This is the column-level lineage - ``o_proj_unit -> SEGMENT_1`` - that says
    which computed value lands in which column of the target object.
    """

    from_field: str
    to_field: str

    def __str__(self) -> str:
        return f"{self.from_field} → {self.to_field}"


@dataclass
class LookupCondition:
    left: str
    operator: str
    right: str

    def __str__(self) -> str:
        return f"{self.left} {self.operator} {self.right}"


@dataclass
class Transformation:
    """A node in the mapping graph.

    ``kind`` is resolved from the mapping's own ``$$classInfo`` type table
    (Source / Target / Expression / Lookup / Router / Aggregator / ...), never
    from hard-coded class numbers, so unseen transformation types still name
    themselves correctly.
    """

    name: str
    kind: str
    connection_name: str = ""
    connection_type: str = ""
    object_name: str = ""
    custom_query: str = ""
    filter_condition: str = ""
    advanced_filter: str = ""
    sort_fields: List[str] = field(default_factory=list)
    lookup_conditions: List[LookupCondition] = field(default_factory=list)
    lookup_unconnected: bool = False
    lookup_return_field: str = ""
    lookup_multiple_match: str = ""
    expression_fields: List[ExpressionField] = field(default_factory=list)
    pre_sql: str = ""
    post_sql: str = ""
    write_operations: List[str] = field(default_factory=list)
    truncate_target: bool = False
    update_columns: List[str] = field(default_factory=list)
    field_count: int = 0
    fields: List[TransformationField] = field(default_factory=list)
    field_mappings: List[FieldMapping] = field(default_factory=list)
    #: Options ticked in the IICS designer, e.g. "Forward Rejected Rows".
    #: Only enabled ones are kept - an unticked box is the default and would
    #: bury the meaningful settings in noise.
    options: List[str] = field(default_factory=list)
    group_expressions: List[str] = field(default_factory=list)   # Router/Filter conditions
    unsupported: bool = False    # parsed but kind unknown to this version

    @property
    def connection_display(self) -> str:
        if not self.connection_name:
            return ""
        return (
            f"{self.connection_name} ({self.connection_type})"
            if self.connection_type
            else self.connection_name
        )


@dataclass
class Mapping:
    """A ``m_*`` mapping asset - the transformation graph behind a task."""

    name: str
    description: str = ""
    guid: str = ""
    transformations: List[Transformation] = field(default_factory=list)
    links: List[Dict[str, str]] = field(default_factory=list)   # {from, to}
    preview_image: str = ""   # path to the extracted mapping preview JPEG

    def by_kind(self, kind: str) -> List[Transformation]:
        return [t for t in self.transformations if t.kind == kind]

    @property
    def sources(self) -> List[Transformation]:
        return self.by_kind("Source")

    @property
    def targets(self) -> List[Transformation]:
        return self.by_kind("Target")

    @property
    def lookups(self) -> List[Transformation]:
        return self.by_kind("Lookup")

    @property
    def expressions(self) -> List[Transformation]:
        return self.by_kind("Expression")


@dataclass
class TaskParameter:
    """An in-out parameter, assignment target or notification field."""

    name: str
    value: str = ""
    path: str = ""
    source: str = ""    # constant / formula / field


@dataclass
class FileOperation:
    """Source or target side of a Mass Ingestion (``fit_*``) task."""

    connection_name: str = ""
    connection_type: str = ""
    directory: str = ""
    file_pattern: str = ""
    archive_directory: str = ""
    file_exists_action: str = ""
    after_pickup: str = ""       # what happens to the source file: KEEP/ARCHIVE/DELETE
    actions: List[str] = field(default_factory=list)
    action_detail: str = ""      # e.g. the PGP key an encrypt step uses

    @property
    def connection_display(self) -> str:
        if self.connection_name and self.connection_type:
            return f"{self.connection_name} ({self.connection_type})"
        return self.connection_name or self.connection_type


@dataclass
class Task:
    """A referenced task asset: mapping task (MTT) or mass ingestion (MI_TASK)."""

    name: str
    task_type: str = ""          # MCT / MI_TASK / command / notification
    description: str = ""
    guid: str = ""
    mapping: Optional[Mapping] = None
    parameter_file: str = ""
    in_out_parameters: List[TaskParameter] = field(default_factory=list)
    runtime_environment: str = ""
    source: Optional[FileOperation] = None
    target: Optional[FileOperation] = None
    schedule: str = ""
    options: List[str] = field(default_factory=list)   # ticked task settings


@dataclass
class Step:
    """One step in the taskflow, in execution order."""

    seq: str                     # "1", "7.1", "11.2" - display sequence
    title: str                   # taskflow step name
    step_type: str               # Mapping Task / Mass Ingestion Task / ...
    service_name: str = ""       # raw IICS service name
    task_name: str = ""          # mct_* / fit_*
    task: Optional[Task] = None
    parameters: List[TaskParameter] = field(default_factory=list)
    branch: str = ""             # parallel path / decision branch label
    on_error: str = ""
    depth: int = 0
    options: List[str] = field(default_factory=list)   # ticked step settings

    @property
    def mapping_name(self) -> str:
        return self.task.mapping.name if self.task and self.task.mapping else ""


@dataclass
class IntegrationMeta:
    """Package-level metadata plus human-supplied fields."""

    taskflow_name: str = ""
    display_name: str = ""
    project: str = ""
    folder: str = ""
    description: str = ""
    source_org: str = ""
    created_by: str = ""
    created_date: str = ""
    modified_by: str = ""
    modified_date: str = ""
    version_label: str = ""
    publication_status: str = ""
    # fields that need human/AI input - never invented silently
    business_owner: str = ""
    technical_owner: str = ""
    business_process: str = ""
    business_process_name: str = ""
    criticality: str = ""
    integration_type: str = ""
    trigger_type: str = ""
    integration_pattern: str = ""
    environments: str = ""
    current_status: str = ""
    upstream_app_owner: str = ""
    downstream_app_owner: str = ""
    last_reviewed: str = ""
    schedule_note: str = ""
    error_handling: str = ""
    monitoring: str = ""
    failure_recovery: str = ""


@dataclass
class Integration:
    """Everything parsed from one IICS export package."""

    meta: IntegrationMeta = field(default_factory=IntegrationMeta)
    steps: List[Step] = field(default_factory=list)
    connections: List[Connection] = field(default_factory=list)
    mappings: List[Mapping] = field(default_factory=list)
    tasks: List[Task] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def connection(self, name: str) -> Optional[Connection]:
        for c in self.connections:
            if c.name == name:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return _clean(asdict(self))
