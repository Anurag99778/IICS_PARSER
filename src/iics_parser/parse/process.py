"""Parse Application Integration (Process Designer) assets.

An IICS org holds two unrelated families of asset. Data Integration ships
taskflows, mappings and mapping tasks; Application Integration ships processes,
process objects, service connectors and their own connections. They share
nothing but the export envelope - different schemas, different vocabulary,
different designer - so a package of one kind read by a parser written for the
other yields nothing at all.

This module reads the second family into the same :class:`Integration` the
Data Integration parsers produce, so one template, one set of sheets and one
Word document describe either kind. The vocabulary maps across like this:

    process            the integration - what a taskflow is to Data Integration
    flow element       a step, in execution order
    service / create   a mapping: the connection and the object it touches
    assignment         an Expression transformation, one row per operation
    subflow            a step that runs another process
    container          a decision, its branches nested beneath it
    eventContainer     a fault handler
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..model.ir import (
    Connection,
    ExpressionField,
    FieldMapping,
    Integration,
    IntegrationMeta,
    Mapping,
    Step,
    Task,
    TaskParameter,
    Transformation,
    TransformationField,
)

#: Flow element -> the step type an analyst would write in the document.
_STEP_TYPES = {
    "start": "Start",
    "end": "End",
    "assignment": "Assignment",
    "service": "Service Call",
    "create": "Create",
    "update": "Update",
    "delete": "Delete",
    "read": "Read",
    "query": "Query",
    "subflow": "Subprocess",
    "throw": "Throw Fault",
    "wait": "Wait",
    "notification": "Notification",
    "receive": "Receive",
    "reply": "Reply",
    "milestone": "Milestone",
    "jump": "Jump",
    "parallelPaths": "Parallel Paths",
}

#: Elements that carry nested branches rather than doing work themselves.
_CONTAINERS = {"container", "eventContainer", "parallelPaths"}

#: Elements that touch an endpoint, and the element holding their field list.
_ENDPOINT_INPUTS = {
    "create": "createInput",
    "update": "updateInput",
    "delete": "deleteInput",
    "read": "readInput",
    "query": "queryInput",
    "service": "serviceInput",
}

#: Elements that are structural only - never steps in their own right.
_SKIP = {"link", "title", "input", "outputDef", "extData", "appliesTo"}


# ------------------------------------------------------------------- loading

def _localise(root: ET.Element) -> ET.Element:
    """Strip namespaces so the rest of this module can match on plain names.

    Process definitions nest four namespaces and re-declare them per element.
    Matching qualified names would bury every lookup in boilerplate for no
    gain: nothing here is ambiguous once the prefixes are gone.
    """
    for elem in root.iter():
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
        for key in list(elem.attrib):
            if "}" in key:
                elem.attrib[key.split("}", 1)[1]] = elem.attrib.pop(key)
    return root


def _load(path: Path) -> Optional[ET.Element]:
    try:
        return _localise(ET.parse(path).getroot())
    except (ET.ParseError, OSError):
        return None


def _text(elem: Optional[ET.Element], *names: str) -> str:
    """Text of the first named child, or of ``elem`` itself when none given."""
    if elem is None:
        return ""
    if not names:
        return (elem.text or "").strip()
    for name in names:
        child = elem.find(name)
        if child is not None and (child.text or "").strip():
            return child.text.strip()
    return ""


def _item_field(root: ET.Element, name: str) -> str:
    """Read a repository envelope field (``Name``, ``CreatedBy``, ...)."""
    item = root.find("Item")
    return _text(item, name) if item is not None else ""


# --------------------------------------------------------------- connections

def parse_ai_connection(path: Path) -> Optional[Connection]:
    """Read an ``*.AI_CONNECTION.xml`` asset."""
    root = _load(path)
    if root is None:
        return None
    node = root.find(".//connection")
    if node is None:
        return None

    name = node.get("name") or node.get("displayName") or _item_field(root, "Name")
    attrs = {
        a.get("name", ""): a.get("value", "")
        for a in node.findall(".//attribute")
    }
    conn = Connection(
        name=name,
        guid=_item_field(root, "GUID"),
        runtime_environment=_text(node, "agent"),
        username=attrs.get("Username", ""),
        schema=attrs.get("Schema", ""),
    )

    url = next((v for k, v in attrs.items() if "URL" in k and v), "")
    if url:
        conn.host, conn.database = _split_jdbc(url)

    connector = node.find(".//businessConnector")
    if connector is not None:
        conn.conn_type = f"Service Connector: {connector.get('name', '')}".rstrip(": ")
    elif url.startswith("jdbc:"):
        # jdbc:oracle:thin:@... - the vendor is the second segment.
        parts = url.split(":")
        conn.conn_type = f"JDBC ({parts[1].title()})" if len(parts) > 1 else "JDBC"
    else:
        conn.conn_type = "Application Connection"
    return conn


def _split_jdbc(url: str) -> Tuple[str, str]:
    """``jdbc:oracle:thin:@//host:1521/SERVICE`` -> ``(host:1521, SERVICE)``."""
    match = re.search(r"@//([^/]+)/(.+)$", url)
    if match:
        return match.group(1), match.group(2)
    match = re.search(r"@([^:]+):(\d+):(\w+)$", url)       # host:port:SID
    if match:
        return f"{match.group(1)}:{match.group(2)}", match.group(3)
    return url, ""


# ------------------------------------------------------------------- process

def parse_process(path: Path, warnings: List[str]) -> Tuple[
        IntegrationMeta, List[Step], List[Mapping], List[Task]]:
    """Parse one ``*.PROCESS.xml`` into meta, ordered steps and their mappings."""
    root = _load(path)
    if root is None:
        warnings.append(f"{path.name}: could not be read as XML")
        return IntegrationMeta(), [], [], []

    process = root.find(".//process")
    if process is None:
        warnings.append(f"{path.name}: no <process> definition inside")
        return IntegrationMeta(), [], [], []

    meta = _meta(root, process)
    builder = _StepBuilder(warnings)
    flow = process.find("flow")
    if flow is not None:
        builder.walk(flow, depth=0, prefix="")
    else:
        warnings.append(f"{meta.taskflow_name}: process has no flow")

    _record_process_contract(process, builder.steps,
                             builder.mappings, builder.tasks)
    return meta, builder.steps, builder.mappings, builder.tasks


def _meta(root: ET.Element, process: ET.Element) -> IntegrationMeta:
    name = (process.get("name") or _item_field(root, "Name") or "").strip()
    return IntegrationMeta(
        taskflow_name=name,
        display_name=process.get("displayName") or name,
        description=_text(process, "description") or _item_field(root, "Description"),
        created_by=_item_field(root, "CreatedBy"),
        created_date=_item_field(root, "CreationDate"),
        modified_by=_item_field(root, "ModifiedBy"),
        modified_date=_item_field(root, "ModificationDate"),
        version_label=_item_field(root, "VersionLabel"),
        publication_status=_item_field(root, "PublicationStatus"),
        integration_type="Application Integration process",
    )


def _record_process_contract(process: ET.Element, steps: List[Step],
                             mappings: List[Mapping], tasks: List[Task]) -> None:
    """Hang the process's own input and variables on its Start step.

    A process is triggered with a payload, and that payload is the integration's
    source - there is no source connection to name. The Start step is where a
    reader looks for what arrives, so the inbound contract belongs there.

    The temp fields are the process's variable declarations. They are worth
    recording - a migration has to recreate them - but there can be dozens, and
    as step parameters they would push the first real step of the integration
    off the top of the Mapping Details sheet. They go in as a transformation
    instead, which lands them on the field-level sheet where declarations
    belong and leaves sheet one describing the flow.
    """
    start = next((s for s in steps if s.step_type == "Start"), None)
    if start is None:
        return
    for param in process.findall("./input/parameter"):
        start.parameters.append(TaskParameter(
            name=param.get("name", ""),
            value=param.get("type", ""),
            path="Process input",
            source="input",
        ))

    temp = process.findall("./tempFields/field")
    if not temp:
        return
    tx = Transformation(
        name="Process Temp Fields",
        kind="Expression",
        field_count=len(temp),
        expression_fields=[ExpressionField(name=f.get("name", ""),
                                           expression="",
                                           field_type="VARIABLE",
                                           data_type=f.get("type", ""))
                           for f in temp],
    )
    mapping = Mapping(name="Process Temp Fields", transformations=[tx],
                      diagram_expected=False)
    task = Task(name="Process Temp Fields", task_type="Declarations",
                mapping=mapping)
    start.task = task
    start.task_name = task.name
    mappings.append(mapping)
    tasks.append(task)


class _StepBuilder:
    """Walks a process flow and emits steps in execution order.

    Order comes from the ``<link targetId>`` chain rather than document order:
    the designer writes elements out in the order they were created, which is
    not the order they run. Anything the chain does not reach is appended
    afterwards so an unlinked element is never silently dropped.
    """

    def __init__(self, warnings: List[str]) -> None:
        self.warnings = warnings
        self.steps: List[Step] = []
        self.mappings: List[Mapping] = []
        self.tasks: List[Task] = []
        self._names: Dict[str, int] = {}

    # ------------------------------------------------------------- traversal

    def walk(self, flow: ET.Element, depth: int, prefix: str,
             branch: str = "") -> None:
        for index, elem in enumerate(self._ordered(flow), start=1):
            tag = elem.tag
            seq = f"{prefix}{index}" if not prefix else f"{prefix}.{index}"
            title = _text(elem, "title") or _default_title(tag, index)

            if tag in _CONTAINERS:
                self._container(elem, tag, seq, title, depth, branch)
                continue

            step = Step(
                seq=seq,
                title=title,
                step_type=_STEP_TYPES.get(tag, tag.title()),
                depth=depth,
                branch=branch,
            )
            self._attach(step, elem, tag)
            self.steps.append(step)

    def _container(self, elem: ET.Element, tag: str, seq: str, title: str,
                   depth: int, branch: str) -> None:
        """Emit a container, its protected activity and its branches.

        An ``eventContainer`` is not a step of its own: it is an activity - a
        service call, usually - wrapped in the branches that run if it faults.
        The activity sits beside those branches as a plain child, so taking
        only the ``<flow>`` children would drop the very call the container
        exists to protect. Where an activity is found it becomes the step and
        the branches nest beneath it; a container with none is a decision.
        """
        activities = [c for c in elem
                      if c.tag not in _SKIP and c.tag != "flow"]
        branches = elem.findall("flow")

        if activities:
            lead, rest = activities[0], activities[1:]
            step = Step(
                seq=seq,
                title=_text(lead, "title") or title,
                step_type=_STEP_TYPES.get(lead.tag, lead.tag.title()),
                depth=depth,
                branch=branch,
                options=(["Fault handled"] if tag == "eventContainer" and branches
                         else []),
            )
            self._attach(step, lead, lead.tag)
            self.steps.append(step)
            for extra_index, extra in enumerate(rest, start=1):
                self.walk_element(extra, depth + 1, f"{seq}.{extra_index}", branch)
        else:
            self.steps.append(Step(
                seq=seq,
                title=title,
                step_type=("Fault Handler" if tag == "eventContainer"
                           else _container_type(elem)),
                depth=depth,
                branch=branch,
                options=_container_options(elem),
            ))

        # Each branch numbers beneath its own index, so two branches holding a
        # step apiece read as 10.1.1 and 10.2.1 rather than both as 10.1.
        offset = len(activities[1:])
        for b_index, sub in enumerate(branches, start=1):
            label = _branch_label(elem, sub, b_index, fault=(tag == "eventContainer"))
            self.walk(sub, depth + 1, f"{seq}.{offset + b_index}", branch=label)

    def walk_element(self, elem: ET.Element, depth: int, seq: str,
                     branch: str) -> None:
        """Emit one element as a step - used for extras beside an activity."""
        if elem.tag in _CONTAINERS:
            self._container(elem, elem.tag, seq,
                            _text(elem, "title") or elem.tag.title(), depth, branch)
            return
        step = Step(
            seq=seq,
            title=_text(elem, "title") or _default_title(elem.tag, 1),
            step_type=_STEP_TYPES.get(elem.tag, elem.tag.title()),
            depth=depth,
            branch=branch,
        )
        self._attach(step, elem, elem.tag)
        self.steps.append(step)

    def _ordered(self, flow: ET.Element) -> List[ET.Element]:
        """Flow children in execution order, following the link chain."""
        children = [c for c in flow if c.tag not in _SKIP]
        by_id = {c.get("id"): c for c in children if c.get("id")}

        ordered: List[ET.Element] = []
        seen: set = set()
        current = next((c for c in children if c.tag == "start"), None)
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            ordered.append(current)
            link = current.find("link")
            target = link.get("targetId") if link is not None else None
            current = by_id.get(target) if target else None

        ordered.extend(c for c in children if id(c) not in seen)
        return ordered

    # ------------------------------------------------------------ enrichment

    def _attach(self, step: Step, elem: ET.Element, tag: str) -> None:
        """Give a step its parameters and, where it touches data, a mapping."""
        if tag == "assignment":
            self._assignment(step, elem)
        elif tag in _ENDPOINT_INPUTS:
            self._endpoint(step, elem, tag)
        elif tag == "subflow":
            self._subflow(step, elem)
        elif tag == "throw":
            for param in elem.findall("./throwInput/parameter"):
                step.parameters.append(_parameter(param, "Fault"))
            step.on_error = "Raises a fault and ends this path"

    def _assignment(self, step: Step, elem: ET.Element) -> None:
        """An assignment is an Expression transformation: field <- expression."""
        fields = []
        for op in elem.findall("operation"):
            target = op.get("to", "")
            fields.append(ExpressionField(
                name=target,
                expression=_operation_value(op),
                field_type=op.get("source", ""),
            ))
            step.parameters.append(TaskParameter(
                name=target,
                value=_operation_value(op),
                path=f"{step.title} -> {op.get('source', 'assignment')}",
                source=op.get("source", ""),
            ))
        if not fields:
            return
        tx = Transformation(name=step.title, kind="Expression",
                            expression_fields=fields, field_count=len(fields))
        self._bind(step, "Assignment", [tx])

    def _endpoint(self, step: Step, elem: ET.Element, tag: str) -> None:
        """A service call or a create/update/delete against a connection."""
        ref = _text(elem, "serviceName", "entityName", "objectName")
        connection, obj = _split_entity(ref)
        inputs = elem.find(_ENDPOINT_INPUTS[tag])
        params = inputs.findall("parameter") if inputs is not None else []

        tx = Transformation(
            name=obj or step.title,
            kind="Target",
            connection_name=connection,
            object_name=obj,
            write_operations=[_STEP_TYPES.get(tag, tag).upper()],
            field_count=len(params),
            fields=[TransformationField(name=p.get("name", ""), origin=obj,
                                        mapped_from=_parameter_value(p))
                    for p in params],
            field_mappings=[FieldMapping(from_field=_parameter_value(p),
                                         to_field=p.get("name", ""))
                            for p in params],
        )
        transformations = [tx]

        # A call that declares a response shape reads data as well as writes it.
        outputs = elem.findall("./outputDef/field")
        if outputs:
            transformations.append(Transformation(
                name=f"{step.title} (response)",
                kind="Source",
                connection_name=connection,
                object_name=obj,
                field_count=len(outputs),
                fields=[TransformationField(name=f.get("name", ""),
                                            data_type=f.get("type", ""),
                                            origin=obj)
                        for f in outputs],
            ))

        for param in params:
            step.parameters.append(_parameter(param, step.title))
        self._bind(step, _STEP_TYPES.get(tag, tag.title()), transformations)

    def _subflow(self, step: Step, elem: ET.Element) -> None:
        """A step that runs another process, with the arguments it passes."""
        called = _text(elem, "subflowPath") or _text(elem, "title")
        step.task_name = called
        step.service_name = _text(elem, "subflowGUID")
        if _text(elem, "runForEach").lower() == "true":
            step.options.append("Runs once per item")

        for param in elem.findall("./input/parameter"):
            step.parameters.append(_parameter(param, called))

        outputs = elem.findall("./outputDef/field")
        transformations: List[Transformation] = []
        if outputs:
            transformations.append(Transformation(
                name=f"{called} (returns)",
                kind="Source",
                object_name=called,
                field_count=len(outputs),
                fields=[TransformationField(name=f.get("name", ""),
                                            data_type=f.get("type", ""),
                                            origin=called)
                        for f in outputs],
            ))
        if transformations:
            self._bind(step, "Subprocess", transformations, name=called)
            return
        # No declared return shape, so there is nothing to map - but the step
        # does reference a real process, and leaving it unbound would report it
        # as a reference that failed to resolve.
        task = Task(name=called, task_type="Subprocess")
        step.task = task
        self.tasks.append(task)

    def _bind(self, step: Step, task_type: str,
              transformations: Sequence[Transformation],
              name: str = "") -> None:
        """Wrap transformations in the Mapping/Task pair the renderers expect.

        Every sheet reads a step's objects through ``step.task.mapping``. The
        Data Integration side gets that pairing from real MTT and DTEMPLATE
        assets; a process has no equivalent, so one is built here rather than
        teaching every renderer a second shape.
        """
        mapping = Mapping(name=self._unique(name or step.title),
                          transformations=list(transformations),
                          diagram_expected=False)
        sources = [t for t in transformations if t.kind == "Source"]
        targets = [t for t in transformations if t.kind == "Target"]
        mapping.links = [{"from": s.name, "to": t.name}
                         for s in sources for t in targets]

        task = Task(name=step.task_name or mapping.name,
                    task_type=task_type, mapping=mapping)
        step.task = task
        step.task_name = task.name
        self.mappings.append(mapping)
        self.tasks.append(task)

    def _unique(self, name: str) -> str:
        """Keep mapping names distinct - a title can repeat across branches."""
        count = self._names.get(name, 0) + 1
        self._names[name] = count
        return name if count == 1 else f"{name} ({count})"


# ------------------------------------------------------------------- helpers

def _split_entity(ref: str) -> Tuple[str, str]:
    """``app-JDBC-STAGING1-RR:STG_RR_PO`` -> ``(app-JDBC-STAGING1-RR, STG_RR_PO)``."""
    if ":" in ref:
        connection, obj = ref.split(":", 1)
        return connection.strip(), obj.strip()
    return "", ref.strip()


def _parameter_value(param: ET.Element) -> str:
    """The value a parameter passes: a literal, a field path or a formula."""
    expression = param.find("expression")
    if expression is not None and (expression.text or "").strip():
        return expression.text.strip()
    return (param.text or "").strip()


def _parameter(param: ET.Element, path: str) -> TaskParameter:
    return TaskParameter(
        name=param.get("name", ""),
        value=_parameter_value(param),
        path=path,
        source=param.get("source", ""),
    )


def _operation_value(op: ET.Element) -> str:
    expression = op.find("expression")
    if expression is not None and (expression.text or "").strip():
        return expression.text.strip()
    return (op.text or "").strip()


def _container_type(elem: ET.Element) -> str:
    kind = (elem.get("type") or "").lower()
    if kind == "exclusive":
        return "Decision"
    if kind in ("parallel", "parallelpaths"):
        return "Parallel Paths"
    return "Branch" if kind else "Group"


def _container_options(elem: ET.Element) -> List[str]:
    branches = len(elem.findall("flow"))
    return [f"{branches} branch(es)"] if branches else []


def _branch_label(container: ET.Element, flow: ET.Element, index: int,
                  fault: bool = False) -> str:
    """The condition that selects a branch, as the designer shows it.

    The container links to each branch flow and hangs the branch's condition on
    that link, so the label is found by matching link target to flow id. A
    branch with no condition is the path taken when no other matched, which is
    worth saying outright - "Branch 2" tells a reader nothing.
    """
    flow_id = flow.get("id")
    for link in container.findall("link"):
        if link.get("targetId") != flow_id:
            continue
        condition = link.find("condition")
        if condition is None:
            continue
        text = _condition_text(condition)
        if text:
            return text
    return "On fault" if fault else "Otherwise"


def _condition_text(condition: ET.Element) -> str:
    """Flatten a condition into something readable on one line."""
    expression = condition.find("expression")
    if expression is not None and (expression.text or "").strip():
        return expression.text.strip()

    function = condition.find("function")
    if function is not None:
        args = [_field_ref((a.text or "").strip()) for a in function.findall("arg")]
        args = [a for a in args if a]
        name = function.get("name", "")
        # The designer's own vocabulary for the two it uses constantly.
        if name == "set" and args:
            return f"{args[0]} is set"
        if name == "notset" and args:
            return f"{args[0]} is not set"
        return f"{name}({', '.join(args)})" if args else name

    return _field_ref((condition.text or "").strip())


def _field_ref(text: str) -> str:
    """``{$temp.tmp_run_log_obj}`` -> ``tmp_run_log_obj``.

    Field references carry designer syntax that means nothing to a reader of
    the analysis, and the scope prefix repeats on every one of them.
    """
    text = text.strip()
    match = re.fullmatch(r"\{\s*\$?([^}]+?)\s*\}", text)
    if match:
        text = match.group(1)
    return re.sub(r"^\$?(temp|input|output)\.", "", text)


def _default_title(tag: str, index: int) -> str:
    return f"{_STEP_TYPES.get(tag, tag.title())} {index}"
