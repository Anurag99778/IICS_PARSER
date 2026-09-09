"""Build IICS export packages in memory, for tests that need no real export.

The reference Concur package is not in the repository, so the tests that need a
package build one. These are real packages in shape - the same nesting, the
same file names, the same ``$$classInfo`` type table the parser resolves
transformation kinds through - just small enough to read in one screen.
"""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

#: Class numbers are arbitrary in a real export; the parser reads them out of
#: each mapping's own type table, so any consistent numbering works here.
_CLASS_INFO = {
    "7": "com.informatica.metadata.common.template.tx.tmplsource.TmplSource",
    "8": "com.informatica.metadata.common.template.tx.tmpltarget.TmplTarget",
    "9": "com.informatica.metadata.common.template.tx.tmplexpression.TmplExpression",
}
_SOURCE, _TARGET, _EXPRESSION = 7, 8, 9

#: A real .DTEMPLATE carries the designer's preview of the mapping, which the
#: Word document embeds. A 1x1 JPEG stands in for it - small, but a genuine
#: image, so the extract-and-embed path is exercised rather than skipped.
PREVIEW_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


def connection(name: str = "cn_WAREHOUSE", conn_type: str = "Oracle",
               guid: str = "CONN0001") -> Dict:
    return {
        "name": name, "type": conn_type, "instanceDisplayName": conn_type,
        "federatedId": guid, "host": "db.example.internal",
        "database": "WAREHOUSE", "username": "APP_OWNER",
    }


def mapping_graph(source_object: str = "STG_EMPLOYEES",
                  target_object: str = "DW_EMPLOYEES",
                  connection_guid: str = "CONN0001") -> Dict:
    """A source -> expression -> target mapping, with a column mapping."""
    adapter = lambda obj: {                                  # noqa: E731
        "connectionId": f"@{connection_guid}",
        "typeSystem": "Oracle",
        "object": {
            "name": obj, "objectName": obj, "path": obj, "customQuery": "",
            "fields": [
                {"name": "EMPLOYEE_ID", "nativeType": "number",
                 "precision": 10, "scale": 0, "nullable": "false", "key": "true"},
                {"name": "FULL_NAME", "nativeType": "varchar2",
                 "precision": 100, "scale": 0, "nullable": "true"},
            ],
        },
    }
    string_type = {"##SID": "smd:typesystem/string"}

    return {
        "metadata": {"$$classInfo": dict(_CLASS_INFO)},
        "content": {
            "transformations": [
                {"$$ID": 1, "$$class": _SOURCE, "name": "src_employees",
                 "dataAdapter": adapter(source_object),
                 "fields": [{"name": "EMPLOYEE_ID", "platformType": string_type},
                            {"name": "FULL_NAME", "platformType": string_type}]},
                {"$$ID": 2, "$$class": _EXPRESSION, "name": "exp_clean",
                 "fields": [{"name": "o_full_name", "expFieldType": "OUTPUT",
                             "expression": "UPPER(FULL_NAME)",
                             "platformType": string_type}]},
                {"$$ID": 3, "$$class": _TARGET, "name": "tgt_employees",
                 "dataAdapter": adapter(target_object),
                 "manualMappings": {"mappingList": [
                     {"fromFieldName": "o_full_name", "toField": {"##ID": 31}}]},
                 "fields": [{"name": "EMPLOYEE_ID", "$$ID": 30,
                             "platformType": string_type},
                            {"name": "FULL_NAME", "$$ID": 31,
                             "platformType": string_type}]},
            ],
            "links": [
                {"fromTransformation": {"##ID": 1}, "toTransformation": {"##ID": 2}},
                {"fromTransformation": {"##ID": 2}, "toTransformation": {"##ID": 3}},
            ],
        },
    }


def write_package(path: Path, *, project: str = "Payroll",
                  mapping_name: str = "m_LOAD_EMPLOYEES",
                  task_name: Optional[str] = None,
                  export_name: Optional[str] = None,
                  description: str = "Load employees into the warehouse.",
                  mapping_guid: str = "MAP00001") -> Path:
    """Write an export package containing one mapping, and optionally its task.

    With ``task_name`` omitted the package is a bare mapping - the case where
    someone exported a single asset out of the IICS UI.
    """
    path = Path(path)
    objects: List[Dict] = [
        _exported(connection()["name"], "Connection", "CONN0001", "/SYS"),
        _exported(mapping_name, "DTEMPLATE", mapping_guid,
                  f"/Explore/{project}", description),
    ]
    if task_name:
        objects.append(_exported(task_name, "MTT", "TASK0001",
                                 f"/Explore/{project}", description))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as pkg:
        pkg.writestr("exportMetadata.v2.json", json.dumps({
            "name": f"{export_name or task_name or mapping_name}-1787142080634",
            "sourceOrgId": "ORG1", "sourceOrgName": "ACME_DEV",
            "exportedObjects": objects,
        }))
        pkg.writestr("SYS/cn_WAREHOUSE.Connection.zip",
                     _asset_zip({"connection.json": json.dumps([connection()])}))
        pkg.writestr(f"Explore/{project}/{mapping_name}.DTEMPLATE.zip",
                     _asset_zip({
                         "fileRecord.json": json.dumps([
                             {"id": "@3", "type": "IMFOBJECT"},
                             {"id": "@2", "type": "IMAGE"},
                         ]),
                         "bin/@3.bin": json.dumps(mapping_graph()),
                         "bin/@2.bin": PREVIEW_JPEG,
                     }))
        if task_name:
            pkg.writestr(f"Explore/{project}/{task_name}.MTT.zip",
                         _asset_zip({"mtTask.json": json.dumps([{
                             "name": task_name, "frsGuid": "TASK0001",
                             "description": description,
                             "mappingId": f"@{mapping_guid}",
                             "parameterFileName": "employees.param",
                             "inOutParameters": [
                                 {"name": "p_run_date", "currentValue": "SYSDATE"}],
                         }])}))
    return path


def _exported(name: str, obj_type: str, guid: str, folder: str,
              description: str = "") -> Dict:
    return {
        "objectGuid": guid, "objectName": name, "objectType": obj_type,
        "path": folder,
        "metadata": {"additionalInfo": {"description": description or None}},
    }


def _asset_zip(files: Dict[str, str]) -> bytes:
    """An asset's own zip - the packaging IICS uses inside an export."""
    from io import BytesIO
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as inner:
        for name, body in files.items():
            inner.writestr(name, body)
    return buffer.getvalue()
