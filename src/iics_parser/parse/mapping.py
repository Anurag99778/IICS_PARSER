"""Parse a ``m_*.DTEMPLATE`` asset into a :class:`Mapping`.

The real mapping graph lives in a binary-named attachment (``bin/@3.bin``)
which is actually JSON::

    {"content": {"transformations": [...], "links": [...]},
     "metadata": {"$$classInfo": {"7": "...tmplsource.TmplSource", ...}}}

``$$classInfo`` is a self-describing type table: every transformation's
``$$class`` number resolves to a fully-qualified Informatica class name. We
resolve transformation kinds through it rather than hard-coding numbers, so
transformation types this parser has never seen still name themselves
correctly (and get flagged rather than dropped).

``bin/@2.bin`` is the mapping preview JPEG shown in Informatica's designer -
we keep it to embed in the Word document.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..extract.package import Asset
from ..model.ir import (Connection, ExpressionField, FieldMapping, LookupCondition,
                        Mapping, Transformation, TransformationField)

#: Structural booleans worth reporting when ticked, with their designer label.
#: Only the enabled state is recorded - an unticked box is the default.
_OPTION_LABELS = {
    "createTarget": "Create target at runtime",
    "inputSorted": "Input is sorted",
    "targetFieldsOrdered": "Target fields ordered",
    "useLabels": "Use labels",
    "useSequenceFields": "Use sequence fields",
    "generateFilenamePort": "Generate filename port",
}
_READ_OPTION_LABELS = {
    "selectDistinct": "Select distinct",
    "queryAll": "Query all",
    "descending": "Sort descending",
}
_WRITE_OPTION_LABELS = {
    "truncate": "Truncate target",
    "bulkApi": "Use bulk API",
    "setFieldsToNull": "Set fields to null",
    "useExactSrcNames": "Use exact source names",
    "handleSpecialChars": "Handle special characters",
    "handleDecimalRoundOff": "Handle decimal round-off",
    "useErrorFile": "Write error file",
    "useSuccessFile": "Write success file",
}

#: Informatica class-name fragment -> canonical transformation kind.
_KIND_BY_CLASS = {
    "tmplsource.TmplSource": "Source",
    "tmpltarget.TmplTarget": "Target",
    "tmplexpression.TmplExpression": "Expression",
    "tmpllookup.TmplLookup": "Lookup",
    "tmplfilter.TmplFilter": "Filter",
    "tmplrouter.TmplRouter": "Router",
    "tmplaggregator.TmplAggregator": "Aggregator",
    "tmplsorter.TmplSorter": "Sorter",
    "tmpljoiner.TmplJoiner": "Joiner",
    "tmplunion.TmplUnion": "Union",
    "tmplnormalizer.TmplNormalizer": "Normalizer",
    "tmplsequence.TmplSequence": "Sequence",
    "tmplsql.TmplSql": "SQL",
    "tmplrank.TmplRank": "Rank",
    "tmpljava.TmplJava": "Java",
    "tmplwebservice.TmplWebService": "Web Service",
    "tmplhierarchyparser.TmplX2r": "Hierarchy Parser",
    "tmplx2r.TmplX2r": "Hierarchy Parser",
    "tmplr2x.TmplR2x": "Hierarchy Builder",
    "tmplvelocity.TmplVelocity": "Structure Parser",
    "tmplupdatestrategy.TmplUpdateStrategy": "Update Strategy",
    "tmpltransaction.TmplTransaction": "Transaction Control",
    "tmplmacro.TmplMacro": "Macro",
    "tmpldeduplicate.TmplDeduplicate": "Deduplicate",
    "tmplcleanse.TmplCleanse": "Cleanse",
    "tmplparse.TmplParse": "Parse",
    "tmplverifier.TmplVerifier": "Verifier",
    "tmplaccessviolation.TmplAccessViolation": "Access Violation",
}


def parse_mapping(asset: Asset, connections: Dict[str, "Connection"],
                  image_dir: Optional[Path] = None,
                  warnings: Optional[List[str]] = None) -> Mapping:
    """Build a :class:`Mapping` from an expanded ``.DTEMPLATE`` directory.

    ``connections`` maps a connection GUID to its :class:`Connection`.
    """
    warnings = warnings if warnings is not None else []
    mapping = Mapping(name=asset.name, description=asset.description, guid=asset.guid)

    graph = _load_graph(asset)
    if graph is None:
        warnings.append(f"{asset.name}: no mapping graph found (bin/@3.bin missing)")
        return mapping

    content = graph.get("content", {})
    class_info = (graph.get("metadata", {}) or {}).get("$$classInfo", {}) or {}

    by_id: Dict[int, Transformation] = {}
    for raw in content.get("transformations", []) or []:
        tx = _parse_transformation(raw, class_info, connections, asset.name, warnings)
        mapping.transformations.append(tx)
        if "$$ID" in raw:
            by_id[raw["$$ID"]] = tx

    for raw in content.get("links", []) or []:
        src = by_id.get((raw.get("fromTransformation") or {}).get("##ID"))
        dst = by_id.get((raw.get("toTransformation") or {}).get("##ID"))
        if src and dst:
            mapping.links.append({"from": src.name, "to": dst.name})

    mapping.preview_image = _extract_preview(asset, image_dir)
    return mapping


# --------------------------------------------------------------------- graph

def _load_graph(asset: Asset) -> Optional[dict]:
    """Find and load the mapping-graph attachment.

    Attachment ids are assigned by Informatica, so locate the IMFOBJECT entry
    via ``fileRecord.json`` rather than assuming ``@3``.
    """
    bin_dir = asset.path / "bin"
    if not bin_dir.is_dir():
        return None

    candidates: List[Path] = []
    records = asset.load_json("fileRecord.json") or []
    for rec in records if isinstance(records, list) else []:
        if rec.get("type") == "IMFOBJECT":
            p = bin_dir / f"{rec.get('id', '')}.bin"
            if p.exists():
                candidates.append(p)

    candidates.extend(sorted(p for p in bin_dir.glob("*.bin") if p not in candidates))

    for path in candidates:
        try:
            with path.open(encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and "content" in data:
            return data
    return None


def _extract_preview(asset: Asset, image_dir: Optional[Path]) -> str:
    """Copy out the mapping preview JPEG, returning its path."""
    if image_dir is None:
        return ""
    records = asset.load_json("fileRecord.json") or []
    for rec in records if isinstance(records, list) else []:
        if rec.get("type") == "IMAGE":
            src = asset.path / "bin" / f"{rec.get('id', '')}.bin"
            if src.exists():
                image_dir.mkdir(parents=True, exist_ok=True)
                dst = image_dir / f"{asset.name}_preview.jpeg"
                shutil.copyfile(src, dst)
                return str(dst)
    return ""


# ------------------------------------------------------------ transformation

def _parse_transformation(raw: dict, class_info: Dict[str, str],
                          connections: Dict[str, Connection], mapping_name: str,
                          warnings: List[str]) -> Transformation:
    class_name = class_info.get(str(raw.get("$$class")), "")
    kind, unsupported = _resolve_kind(class_name)
    if unsupported and class_name:
        warnings.append(
            f"{mapping_name}: transformation '{raw.get('name')}' has unrecognised "
            f"type '{class_name.split('.')[-1]}' - reported as '{kind}', please review"
        )

    tx = Transformation(name=raw.get("name", ""), kind=kind, unsupported=unsupported)

    adapter = raw.get("dataAdapter") or {}
    if adapter:
        guid = str(adapter.get("connectionId", "")).split("@")[-1]
        conn = connections.get(guid)
        tx.connection_name = conn.name if conn else ""
        # The connection asset carries the user-facing type; typeSystem is the
        # adapter's internal name and is only a fallback.
        tx.connection_type = (conn.conn_type if conn else "") or adapter.get("typeSystem", "") or ""

        obj = adapter.get("object") or {}
        tx.object_name = obj.get("objectName") or obj.get("name") or ""
        tx.db_schema = obj.get("dbSchema", "") or ""
        tx.custom_query = obj.get("customQuery", "") or ""
        tx.field_count = len(obj.get("fields") or [])
        tx.fields = _object_fields(obj, raw)
        tx.file_format = _file_format(obj.get("fileAttrs") or {})
        tx.dynamic_file_name = _is_on(adapter.get("useDynamicFileName"))

        read = adapter.get("readOptions") or {}
        tx.filter_condition = read.get("filterCondition", "") or ""
        tx.advanced_filter = read.get("advancedFilterCondition", "") or ""
        tx.user_defined_join = read.get("userDefinedJoin", "") or ""
        tx.row_limit = _positive(read.get("rowLimit"))
        # A source's read-time ordering. Direction matters as much as the key -
        # a report sorted the wrong way is wrong.
        tx.sort_fields = [
            _sort_key(s.get("fieldName"), _is_on(s.get("sortDescending")))
            for s in (read.get("sortFields") or []) if s.get("fieldName")
        ]

        write = adapter.get("writeOptions") or {}
        tx.write_operations = list(write.get("operations") or [])
        tx.truncate_target = _is_on(write.get("truncate"))
        tx.update_strategy = write.get("updateStrategyExpression", "") or ""

        tx.options.extend(_ticked(read, _READ_OPTION_LABELS))
        tx.options.extend(_ticked(write, _WRITE_OPTION_LABELS))
        if tx.dynamic_file_name:
            tx.options.append("Dynamic file name")

    # A Sorter keeps its keys on the transformation itself, not on an adapter.
    tx.sort_fields.extend(
        _sort_key(e.get("fieldName"), not _is_on(e.get("ascending")))
        for e in (raw.get("sortEntries") or []) if e.get("fieldName")
    )
    if raw.get("advancedSort"):
        tx.sort_fields.append(f"Advanced: {raw['advancedSort']}")

    # An Aggregator's group-by list decides what one output row means.
    tx.group_by_fields = [
        f.get("fieldName", "")
        for f in ((raw.get("groupByFieldsList") or {}).get("fields") or [])
        if f.get("fieldName")
    ]

    # Pre/Post SQL, and any advanced option the designer shows as a checkbox.
    for prop in raw.get("advancedProperties") or []:
        pname, pvalue = prop.get("name", ""), prop.get("value", "")
        if not pvalue:
            continue
        if pname == "Pre SQL":
            tx.pre_sql = pvalue
        elif pname == "Post SQL":
            tx.post_sql = pvalue
        elif _is_on(pvalue):
            tx.options.append(pname)

    # Structural checkboxes that live on the transformation itself.
    tx.options.extend(_ticked(raw, _OPTION_LABELS))

    # Expression fields
    for f in raw.get("fields") or []:
        expr = f.get("expression")
        if expr in (None, ""):
            continue
        ftype = f.get("expFieldType", "")
        if ftype == "INPUT":
            continue
        tx.expression_fields.append(
            ExpressionField(
                name=f.get("name", ""),
                expression=str(expr),
                field_type=ftype,
                data_type=_type_name(f.get("platformType")),
                precision=f.get("precision"),
                scale=f.get("scale"),
            )
        )

    # Lookup detail
    for cond in raw.get("lookupConditions") or []:
        tx.lookup_conditions.append(
            LookupCondition(
                left=cond.get("leftOperand", ""),
                operator=cond.get("operator", "="),
                right=cond.get("rightOperand", ""),
            )
        )
    if kind == "Lookup":
        tx.lookup_unconnected = str(raw.get("unconnected", "")).lower() == "true"
        tx.lookup_return_field = raw.get("returnPortName", "") or ""
        tx.lookup_multiple_match = raw.get("multipleMatchPolicy", "") or ""

    # Router / Filter group conditions
    for group in raw.get("groups") or []:
        cond = group.get("filterCondition") or group.get("condition")
        if cond:
            label = group.get("name", "")
            tx.group_expressions.append(f"{label}: {cond}" if label else str(cond))
    if kind == "Filter" and raw.get("filterCondition"):
        tx.group_expressions.append(str(raw["filterCondition"]))

    # Column-level lineage: which incoming field feeds which target column.
    tx.field_mappings = _field_mappings(raw)
    wired = {fm.to_field: fm.from_field for fm in tx.field_mappings}
    for column in tx.fields:
        column.mapped_from = wired.get(column.name, "")

    if not tx.update_columns:
        tx.update_columns = [
            c.get("name", c) if isinstance(c, dict) else str(c)
            for c in (raw.get("updateColumns") or [])
        ]

    return tx


def _object_fields(obj: dict, raw: dict) -> List[TransformationField]:
    """The Fields grid of a source or target.

    Column detail is split across two places: the adapter field carries
    precision, scale, the native database type and the originating object; the
    transformation field carries the platform type the designer displays. They
    are joined by name.
    """
    platform_types = {
        f.get("name"): _type_name(f.get("platformType"))
        for f in (raw.get("fields") or []) if f.get("name")
    }

    out: List[TransformationField] = []
    for f in obj.get("fields") or []:
        name = f.get("name") or f.get("nativeName") or ""
        if not name:
            continue
        origin = ""
        for prop in f.get("properties") or []:
            if prop.get("name") == "parentObject":
                origin = prop.get("value", "") or ""
        out.append(TransformationField(
            name=name,
            data_type=platform_types.get(name, ""),
            precision=_as_int(f.get("precision")),
            scale=_as_int(f.get("scale")),
            native_type=f.get("nativeType", "") or "",
            nullable=_is_on(f.get("nullable")),
            is_key=_is_on(f.get("key")),
            origin=origin or obj.get("objectName", "") or "",
        ))
    return out


def _sort_key(name, descending: bool) -> str:
    """``EMPLOYEE_ID (Ascending)`` - how the designer labels a sort key."""
    return f"{name} ({'Descending' if descending else 'Ascending'})"


def _positive(value) -> str:
    """A numeric setting, kept only when it is actually in force."""
    number = _as_int(value)
    return str(number) if number else ""


def _file_format(attrs: dict) -> str:
    """Summarise a flat file's layout, which decides how it must be rebuilt."""
    if not attrs:
        return ""
    parts = []
    delimiter = attrs.get("delimiter")
    if delimiter:
        parts.append(f"Delimiter: {delimiter}")
    if attrs.get("textQualifier"):
        parts.append(f"Text qualifier: {attrs['textQualifier']}")
    if _is_on(attrs.get("firstDataRowAsHeader")):
        parts.append("First row is a header")
    header_line = _as_int(attrs.get("headerLineNo"))
    first_row = _as_int(attrs.get("firstDataRow"))
    if header_line:
        parts.append(f"Header line: {header_line}")
    if first_row:
        parts.append(f"First data row: {first_row}")
    if attrs.get("escapeChar"):
        parts.append(f"Escape character: {attrs['escapeChar']}")
    return "; ".join(parts)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_on(value) -> bool:
    """True for a ticked checkbox, however the export spells it."""
    return str(value).strip().lower() == "true" or value is True


def _ticked(source: dict, labels: Dict[str, str]) -> List[str]:
    """The designer labels of every option in ``source`` that is switched on."""
    return [label for key, label in labels.items() if _is_on(source.get(key))]


def _field_mappings(raw: dict) -> List[FieldMapping]:
    """Read a target's manual field mappings.

    Each entry names its incoming field but references the target column by id,
    so build an id -> name index over the transformation and resolve through it.
    """
    entries = ((raw.get("manualMappings") or {}).get("mappingList")) or []
    if not entries:
        return []

    index: Dict[int, str] = {}
    stack = [raw]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "$$ID" in node and "name" in node:
                index[node["$$ID"]] = node["name"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)

    out = []
    for entry in entries:
        source = entry.get("fromFieldName") or ", ".join(entry.get("fromFieldNames") or [])
        target = index.get((entry.get("toField") or {}).get("##ID"), "")
        if source and target:
            out.append(FieldMapping(from_field=source, to_field=target))
    return out


def _resolve_kind(class_name: str) -> tuple:
    """Map an Informatica class name to a transformation kind.

    Returns ``(kind, unsupported)``. Unknown types fall back to a humanised
    class name so they still appear in the output instead of vanishing.
    """
    if not class_name:
        return "Unknown", True
    tail = class_name.rsplit("template.tx.", 1)[-1]
    for fragment, kind in _KIND_BY_CLASS.items():
        if tail.endswith(fragment) or class_name.endswith(fragment):
            return kind, False
    leaf = class_name.split(".")[-1]
    if leaf.startswith("Tmpl"):
        leaf = leaf[4:]
    return (leaf or "Unknown"), True


def _type_name(platform_type) -> str:
    """Extract ``string`` from a ``smd:...typesystem/string`` reference."""
    if isinstance(platform_type, dict):
        sid = platform_type.get("##SID", "")
        if "/" in sid:
            return sid.rsplit("/", 1)[-1]
    return ""


def flow_string(mapping: Mapping) -> str:
    """Render the mapping graph as ``src->exp->tgt`` chains.

    Walks each source to its reachable targets. Unconnected lookups have no
    links so they are listed separately, matching how the analysis documents
    describe them.
    """
    if not mapping.transformations:
        return ""

    adjacency: Dict[str, List[str]] = {}
    has_incoming = set()
    for link in mapping.links:
        adjacency.setdefault(link["from"], []).append(link["to"])
        has_incoming.add(link["to"])

    starts = [
        t.name for t in mapping.transformations
        if t.name not in has_incoming and (t.kind == "Source" or t.name in adjacency)
    ]

    # The cap is on the mapping as a whole, not per source - a wide graph has
    # many sources, each of which could otherwise spend the full budget.
    chains: List[str] = []
    truncated = False
    for start in starts:
        if len(chains) >= _MAX_PATHS:
            truncated = True
            break
        for path in _walk(start, adjacency, set()):
            if len(path) > 1:
                chains.append("->".join(path))
            if len(chains) >= _MAX_PATHS:
                truncated = True
                break
    if truncated:
        chains.append(f"… flow truncated at {_MAX_PATHS} paths — see the mapping diagram")

    unconnected = [t.name for t in mapping.lookups if t.lookup_unconnected]
    if unconnected:
        chains.append("Unconnected Lookup: " + ", ".join(unconnected))

    if not chains:
        chains = [
            t.name for t in mapping.transformations
            if t.kind in ("Source", "Target")
        ]
        return " / ".join(chains)

    return "\n".join(chains)


#: Guards for pathological mapping graphs. A wide graph can hold combinatorially
#: many source-to-target paths, and a long chain would overflow the interpreter
#: stack if walked recursively - neither should take the run down.
_MAX_PATHS = 200
_MAX_PATH_LENGTH = 400


def _walk(node: str, adjacency: Dict[str, List[str]], seen: set) -> List[List[str]]:
    """Simple paths from ``node`` to leaf nodes, iteratively and bounded.

    Written as an explicit stack rather than recursion so a long mapping chain
    cannot raise ``RecursionError``, and capped so a wide graph cannot generate
    paths without limit.
    """
    paths: List[List[str]] = []
    stack: List[Tuple[str, List[str], frozenset]] = [(node, [], frozenset(seen))]

    while stack:
        current, prefix, visited = stack.pop()
        path = prefix + [current]

        if current in visited or len(path) >= _MAX_PATH_LENGTH:
            paths.append(path)
            continue

        children = adjacency.get(current, [])
        if not children:
            paths.append(path)
            continue

        if len(paths) + len(stack) >= _MAX_PATHS:
            paths.append(path + ["…"])
            continue

        onward = visited | {current}
        for child in reversed(children):
            stack.append((child, path, onward))

    return paths[:_MAX_PATHS]
