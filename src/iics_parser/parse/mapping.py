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
from typing import Dict, List, Optional

from ..extract.package import Asset
from ..model.ir import ExpressionField, LookupCondition, Mapping, Transformation

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


def parse_mapping(asset: Asset, connections: Dict[str, str],
                  image_dir: Optional[Path] = None,
                  warnings: Optional[List[str]] = None) -> Mapping:
    """Build a :class:`Mapping` from an expanded ``.DTEMPLATE`` directory.

    ``connections`` maps a connection GUID to its display name.
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
                          connections: Dict[str, str], mapping_name: str,
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
        tx.connection_name = connections.get(guid, "")
        tx.connection_type = adapter.get("typeSystem", "") or ""

        obj = adapter.get("object") or {}
        tx.object_name = obj.get("objectName") or obj.get("name") or ""
        tx.custom_query = obj.get("customQuery", "") or ""
        tx.field_count = len(obj.get("fields") or [])

        read = adapter.get("readOptions") or {}
        tx.filter_condition = read.get("filterCondition", "") or ""
        tx.advanced_filter = read.get("advancedFilterCondition", "") or ""
        tx.sort_fields = [
            s.get("fieldName", "") for s in (read.get("sortFields") or []) if s.get("fieldName")
        ]

        write = adapter.get("writeOptions") or {}
        tx.write_operations = list(write.get("operations") or [])
        tx.truncate_target = str(write.get("truncate", "")).lower() == "true"

    # Pre/Post SQL and other advanced properties
    for prop in raw.get("advancedProperties") or []:
        pname, pvalue = prop.get("name", ""), prop.get("value", "")
        if not pvalue:
            continue
        if pname == "Pre SQL":
            tx.pre_sql = pvalue
        elif pname == "Post SQL":
            tx.post_sql = pvalue

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

    if not tx.update_columns:
        tx.update_columns = [
            c.get("name", c) if isinstance(c, dict) else str(c)
            for c in (raw.get("updateColumns") or [])
        ]

    return tx


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

    chains: List[str] = []
    for start in starts:
        for path in _walk(start, adjacency, set()):
            if len(path) > 1:
                chains.append("->".join(path))

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


def _walk(node: str, adjacency: Dict[str, List[str]], seen: set) -> List[List[str]]:
    """All simple paths from ``node`` to leaf nodes."""
    if node in seen:
        return [[node]]
    seen = seen | {node}
    children = adjacency.get(node, [])
    if not children:
        return [[node]]
    paths = []
    for child in children:
        for tail in _walk(child, adjacency, seen):
            paths.append([node] + tail)
    return paths
