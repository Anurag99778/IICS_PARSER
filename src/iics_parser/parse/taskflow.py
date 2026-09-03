"""Parse ``tf_*.TASKFLOW.xml`` into ordered :class:`Step` objects.

The taskflow is an ActiveVOS-style document. Steps are *not* in execution order
in the file - order comes from the link graph:

* ``<link targetId="...">``                  sequence flow: this step -> next step
* ``<link targetId="..." type="containerLink">``  structural: a container to its
  branch ``<flow>``\\ s, or a branch back to its container. Inside ``<events>``
  it points at a fault-handling branch.

So we index every node by id, start at ``<start>``, and follow sequence links,
recursing into container branches. Numbering: sequential in execution order,
with dotted numbers (``7.1``, ``7.2``) for genuinely concurrent parallel-path
branches. Each step also carries a branch label so conditional and error paths
are identifiable in the output.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..model.ir import IntegrationMeta, Step, TaskParameter

#: IICS service name -> human step type used in the analysis documents.
_STEP_TYPES = {
    "ICSExecuteDataTask": "Mapping Task",
    "ICSExecuteTask": "Mapping Task",
    "DIExecuteServiceTaskIngestionTaskImpl": "Mass Ingestion Task",
    "fileIngestionService": "Mass Ingestion Task",
    "ICSExecuteCommandTask": "Command Task",
    "emailNotificationService": "Notification Task",
    "ICSExecuteLinearTaskflow": "Linear Taskflow",
    "ICSExecuteTaskflow": "Taskflow",
    "restv2service": "Service Step",
}

#: Step checkboxes worth reporting when ticked, with their designer label.
_STEP_OPTIONS = {
    "Wait for Task to Complete": "Wait for task to complete",
    "FailTaskIfAnyScriptFails": "Fail task if any script fails",
    "Has Inout Parameters": "Has in-out parameters",
}

#: Step parameters worth surfacing; everything else is engine plumbing.
_INTERESTING_PARAMS = {
    "Email To": "Email_To",
    "Email Cc": "Email_Cc",
    "Email Bcc": "Email_Bcc",
    "Email Subject": "Email_Subject",
    "Email Body": "Email_Body",
    "Script Name": "Script File Name",
    "Input Arguments": "Input Arguments",
    "Work Directory": "Work Directory",
}

_NS = {"types1": "http://schemas.active-endpoints.com/appmodules/repository/2010/10/avrepository.xsd"}

#: Elements that can appear as a node in the flow graph.
_NODES = {"eventContainer", "container", "assignment", "decision", "flow", "start", "end", "service"}


def parse_taskflow(path: Path, warnings: Optional[List[str]] = None):
    """Return ``(meta, steps)`` parsed from a taskflow XML file."""
    warnings = warnings if warnings is not None else []
    root = ET.parse(path).getroot()
    meta = _parse_meta(root)

    taskflow = _descend(root, "taskflow")
    if taskflow is None:
        warnings.append(f"{path.name}: no <taskflow> element found")
        return meta, []

    meta.taskflow_name = taskflow.get("name", meta.taskflow_name)
    meta.display_name = taskflow.get("displayName", meta.taskflow_name)

    body = _descend(taskflow, "flow")
    if body is None:
        warnings.append(f"{path.name}: taskflow has no <flow> body")
        return meta, []

    graph = _Graph(body)
    steps: List[Step] = []
    start = _descend(body, "start")
    if start is None:
        warnings.append(f"{path.name}: taskflow has no <start> node")
        return meta, []

    counter = _Counter()
    graph.walk_chain(graph.sequence_target(start), steps, branch="", counter=counter)

    # Join points deferred out of branches continue the main flow.
    while graph.deferred:
        graph.walk_chain(graph.deferred.pop(0), steps, branch="", counter=counter)

    # Anything unreachable from <start> still belongs in the analysis.
    for node in graph.step_nodes():
        if id(node) not in graph.visited:
            step = graph.make_step(node, branch="(not reachable from start)")
            if step:
                warnings.append(
                    f"{path.name}: step '{step.title}' is not reachable from the "
                    f"taskflow start - listed at the end"
                )
                steps.append(step)

    _number_unreachable(steps, counter.n)
    return meta, steps


# --------------------------------------------------------------------- graph

class _Counter:
    """Sequential step numbering: 1, 2, 3..."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n}" if self.prefix else str(self.n)


class _BranchCounter(_Counter):
    """Numbering inside branch ``i`` of step/container ``parent``.

    The branch's first step is ``parent.i``; anything chained after it inside
    the same branch becomes ``parent.i.1``, ``parent.i.2``, so numbers stay
    unique across sibling branches.
    """

    def __init__(self, parent: str, index: int):
        super().__init__()
        self.head = f"{parent}.{index}"

    def next(self) -> str:
        self.n += 1
        return self.head if self.n == 1 else f"{self.head}.{self.n - 1}"


class _Graph:
    """Index of taskflow nodes with link-following helpers."""

    def __init__(self, body: ET.Element):
        self.body = body
        self.by_id: Dict[str, ET.Element] = {}
        self.parent: Dict[int, ET.Element] = {}
        self.visited: set = set()
        #: Join nodes deferred out of a branch, in first-encountered order.
        self.deferred: List[str] = []
        for parent in body.iter():
            for child in parent:
                self.parent[id(child)] = parent
        for el in body.iter():
            node_id = el.get("id")
            if node_id and _tag(el) in _NODES:
                self.by_id.setdefault(node_id, el)

        # A node several sequence links converge on is a join: it belongs to
        # the main flow, not to whichever branch happens to reach it first.
        self.in_degree: Dict[str, int] = {}
        for link in body.iter():
            if _tag(link) == "link" and link.get("type") != "containerLink":
                target = link.get("targetId")
                if target:
                    self.in_degree[target] = self.in_degree.get(target, 0) + 1

    def is_join(self, node_id: Optional[str]) -> bool:
        return bool(node_id) and self.in_degree.get(node_id, 0) > 1

    # -- link helpers ----------------------------------------------------

    def _links(self, node: ET.Element) -> List[Tuple[ET.Element, bool]]:
        """Direct links of ``node`` as ``(link, is_error_branch)``.

        A link belongs to the nearest enclosing node, so links inside a child
        node are excluded. ``<events>``/``<catch>`` links are error branches.
        """
        out = []
        for link in node.iter():
            if _tag(link) != "link":
                continue
            owner, in_events = self._owner(link, node)
            if owner is not node:
                continue
            out.append((link, in_events))
        return out

    def _owner(self, link: ET.Element, stop: ET.Element) -> Tuple[Optional[ET.Element], bool]:
        """Nearest node ancestor of ``link`` and whether it sits under events."""
        in_events = False
        cur = self.parent.get(id(link))
        while cur is not None:
            tag = _tag(cur)
            if tag in ("events", "catch"):
                in_events = True
            if tag in _NODES:
                return cur, in_events
            if cur is stop:
                break
            cur = self.parent.get(id(cur))
        return None, in_events

    def sequence_target(self, node: ET.Element) -> Optional[str]:
        """The plain (non-container) link target: the next step."""
        for link, in_events in self._links(node):
            if link.get("type") != "containerLink" and not in_events:
                return link.get("targetId")
        return None

    def branch_targets(self, node: ET.Element) -> List[Tuple[str, bool]]:
        """Container/event branch targets as ``(target_id, is_error)``."""
        out = []
        for link, in_events in self._links(node):
            if link.get("type") == "containerLink":
                target = link.get("targetId")
                # A branch links back to its own container - not a child.
                if target and target != node.get("id"):
                    out.append((target, in_events))
        return out

    # -- traversal -------------------------------------------------------

    def walk_chain(self, node_id: Optional[str], steps: List[Step],
                   branch: str, counter: _Counter, depth: int = 0) -> None:
        """Follow sequence links from ``node_id``, emitting steps in order."""
        guard = 0
        while node_id and guard < 500:
            guard += 1
            # Defer joins reached from inside a branch so they are numbered on
            # the main flow once every incoming branch has been walked.
            if depth > 0 and self.is_join(node_id):
                if node_id not in self.deferred:
                    self.deferred.append(node_id)
                return

            node = self.by_id.get(node_id)
            if node is None or id(node) in self.visited:
                return
            self.visited.add(id(node))
            tag = _tag(node)

            if tag == "container":
                # Parallel/exclusive paths are concurrent: every branch is a
                # dotted sub-step of the container's own number.
                number = counter.next()
                label = _title(node)
                for i, (target, _) in enumerate(self.branch_targets(node), start=1):
                    self.walk_chain(
                        target, steps,
                        branch=f"{label} / path {i}" if label else f"path {i}",
                        counter=_BranchCounter(number, i),
                        depth=depth + 1,
                    )
                node_id = self.sequence_target(node)
                continue

            if tag == "flow":
                # A branch body: its first real node continues this chain.
                inner = self._first_child_node(node)
                if inner is not None:
                    self.walk_chain(inner, steps, branch, counter, depth)
                node_id = self.sequence_target(node)
                continue

            step = self.make_step(node, branch, depth)
            if not step:
                node_id = self.sequence_target(node)
                continue

            step.seq = counter.next()
            steps.append(step)

            branches = self.branch_targets(node)
            errors = [t for t, is_err in branches if is_err]
            normals = [t for t, is_err in branches if not is_err]

            # If the step has no onward sequence link, its normal branch *is*
            # the main chain (an on-success branch), so it keeps the top-level
            # numbering instead of being demoted to a sub-step.
            following = self.sequence_target(node)
            continuation = None
            if not self._is_live(following) and normals:
                continuation, normals = normals[0], normals[1:]

            for i, target in enumerate(errors + normals, start=1):
                label = "on error" if target in errors else f"branch {i}"
                self.walk_chain(
                    target, steps,
                    branch=f"{step.title} / {label}",
                    counter=_BranchCounter(step.seq, i),
                    depth=depth + 1,
                )

            node_id = continuation or following

    def _is_live(self, node_id: Optional[str]) -> bool:
        """True if ``node_id`` leads somewhere other than the taskflow end."""
        if not node_id:
            return False
        node = self.by_id.get(node_id)
        return node is not None and _tag(node) != "end"

    def _first_child_node(self, flow: ET.Element) -> Optional[str]:
        for child in flow:
            if _tag(child) in _NODES and child.get("id"):
                return child.get("id")
        return None

    def step_nodes(self) -> List[ET.Element]:
        return [
            el for el in self.body.iter()
            if _tag(el) in ("eventContainer", "assignment", "decision")
        ]

    # -- step construction -----------------------------------------------

    def make_step(self, node: ET.Element, branch: str, depth: int = 0) -> Optional[Step]:
        tag = _tag(node)
        if tag == "eventContainer":
            service = _child(node, "service")
            if service is None:
                return None
            return _parse_service(service, branch, depth, self._catch_modes(node))
        if tag == "service":
            return _parse_service(node, branch, depth, self._catch_modes(node))
        if tag == "assignment":
            return _parse_assignment(node, branch, depth)
        if tag == "decision":
            return Step(
                seq="", title=_title(node) or "Decision", step_type="Decision",
                branch=branch, depth=depth, parameters=_decision_params(node),
            )
        return None

    def _catch_modes(self, node: ET.Element) -> str:
        """Summarise the step's fault handlers, e.g. ``On error - Suspend``."""
        notes = []
        for catch in node.iter():
            if _tag(catch) != "catch":
                continue
            mode = "Suspend Taskflow" if _descend(catch, "suspend") is not None else "Handled"
            notes.append(f"On {catch.get('name', 'error')} - {mode}")
        return "; ".join(dict.fromkeys(notes))


# ------------------------------------------------------------------ elements

def _parse_service(el: ET.Element, branch: str, depth: int, on_error: str) -> Step:
    service_name = _text(el, "serviceName")
    step = Step(
        seq="", title=_text(el, "title"),
        step_type=_STEP_TYPES.get(service_name, service_name or "Task"),
        service_name=service_name, branch=branch, depth=depth, on_error=on_error,
    )

    service_input = _child(el, "serviceInput")
    if service_input is None:
        return step

    for param in service_input:
        if _tag(param) != "parameter":
            continue
        pname = param.get("name", "")

        if pname == "Task Name":
            value = _param_value(param)
            # Command tasks reuse "Task Name" for the step label, not an asset.
            if value.startswith(("mct_", "fit_", "m_", "tf_")):
                step.task_name = value
            continue

        if pname == "taskField":
            step.parameters.extend(_task_field_params(param))
            continue

        if pname in _STEP_OPTIONS:
            if _param_value(param).strip().lower() == "true":
                step.options.append(_STEP_OPTIONS[pname])
            continue

        if pname in _INTERESTING_PARAMS:
            value = _param_value(param)
            if value:
                step.parameters.append(
                    TaskParameter(name=_INTERESTING_PARAMS[pname], value=value,
                                  source=param.get("source", ""))
                )
    return step


def _task_field_params(param: ET.Element) -> List[TaskParameter]:
    """Command-task script details carried as nested taskField operations."""
    labels = {
        "scriptName": "Script File Name",
        "inputArguments": "Input Arguments",
        "workDir": "Work Directory",
    }
    out: List[TaskParameter] = []
    for op in param:
        if _tag(op) != "operation":
            continue
        to, value = op.get("to", ""), (op.text or "").strip()
        if value and "/" in to and to.rsplit("/", 1)[-1] in labels:
            out.append(TaskParameter(name=labels[to.rsplit("/", 1)[-1]],
                                     value=value, path=to, source=op.get("source", "")))
    return out


def _parse_assignment(el: ET.Element, branch: str, depth: int) -> Step:
    step = Step(seq="", title=_title(el) or "Assignment", step_type="Assignment",
                branch=branch, depth=depth)
    for op in el:
        if _tag(op) != "operation":
            continue
        target = op.get("to", "")
        expr = _child(op, "expression")
        value = (expr.text or "") if expr is not None else (op.text or "")
        if target:
            step.parameters.append(
                TaskParameter(name=target.split(".")[-1], value=_normalise(value),
                              path=target, source=op.get("source", ""))
            )
    return step


def _decision_params(el: ET.Element) -> List[TaskParameter]:
    out = []
    for child in el.iter():
        if _tag(child) == "expression" and child.text:
            out.append(TaskParameter(name="Condition", value=_normalise(child.text)))
    return out


# ---------------------------------------------------------------------- meta

def _parse_meta(root: ET.Element) -> IntegrationMeta:
    meta = IntegrationMeta()
    for tag, attr in {
        "Name": "taskflow_name", "Description": "description",
        "VersionLabel": "version_label", "CreatedBy": "created_by",
        "CreationDate": "created_date", "ModifiedBy": "modified_by",
        "ModificationDate": "modified_date", "PublicationStatus": "publication_status",
    }.items():
        el = root.find(f".//types1:{tag}", _NS)
        if el is not None and el.text:
            setattr(meta, attr, el.text.strip())
    return meta


def _number_unreachable(steps: List[Step], start_at: int) -> None:
    """Give trailing unreachable steps their own numbers after the main flow."""
    n = start_at
    for step in steps:
        if not step.seq:
            n += 1
            step.seq = str(n)


# --------------------------------------------------------------------- utils

def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _child(parent: ET.Element, tag: str) -> Optional[ET.Element]:
    for child in parent:
        if _tag(child) == tag:
            return child
    return None


def _descend(parent: ET.Element, tag: str) -> Optional[ET.Element]:
    for child in parent.iter():
        if _tag(child) == tag and child is not parent:
            return child
    return None


def _text(parent: ET.Element, tag: str) -> str:
    child = _child(parent, tag)
    return (child.text or "").strip() if child is not None else ""


def _title(el: ET.Element) -> str:
    return _text(el, "title")


def _param_value(param: ET.Element) -> str:
    expr = _descend(param, "expression")
    if expr is not None and expr.text:
        return _normalise(expr.text)
    return _normalise(param.text or "")


def _normalise(text: str) -> str:
    """Unescape entities and strip HTML so values read cleanly in a cell."""
    text = unescape(text or "").strip()
    if "<" in text and ">" in text:
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
