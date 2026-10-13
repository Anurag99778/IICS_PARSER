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
_CLASS_INFO["11"] = "com.informatica.metadata.common.template.tx.tmplsorter.TmplSorter"
_SOURCE, _TARGET, _EXPRESSION, _SORTER = 7, 8, 9, 11

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
                  connection_guid: str = "CONN0001",
                  with_sorter: bool = False) -> Dict:
    """A source -> expression -> target mapping, with a column mapping.

    ``with_sorter`` adds a Sorter carrying ticked advanced options - a
    transformation that is neither an endpoint nor a lookup, and so has no row
    of its own for its settings to be reported on.
    """
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

    graph = {
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
    if with_sorter:
        graph["content"]["transformations"].append({
            "$$ID": 9, "$$class": _SORTER, "name": "srt_file_output",
            "sortEntries": [{"fieldName": "EMPLOYEE_ID", "ascending": "true"}],
            "advancedProperties": [
                {"name": "Case Sensitive", "value": "true"},
                {"name": "Null Treated Low", "value": "true"},
            ],
        })
    return graph


def write_package(path: Path, *, project: str = "Payroll",
                  mapping_name: str = "m_LOAD_EMPLOYEES",
                  task_name: Optional[str] = None,
                  export_name: Optional[str] = None,
                  description: str = "Load employees into the warehouse.",
                  mapping_guid: str = "MAP00001",
                  connection_name: str = "cn_WAREHOUSE",
                  with_sorter: bool = False) -> Path:
    """Write an export package containing one mapping, and optionally its task.

    With ``task_name`` omitted the package is a bare mapping - the case where
    someone exported a single asset out of the IICS UI.
    """
    path = Path(path)
    conn = connection(name=connection_name)
    objects: List[Dict] = [
        _exported(conn["name"], "Connection", "CONN0001", "/SYS"),
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
                     _asset_zip({"connection.json": json.dumps([conn])}))
        pkg.writestr(f"Explore/{project}/{mapping_name}.DTEMPLATE.zip",
                     _asset_zip({
                         "fileRecord.json": json.dumps([
                             {"id": "@3", "type": "IMFOBJECT"},
                             {"id": "@2", "type": "IMAGE"},
                         ]),
                         "bin/@3.bin": json.dumps(
                             mapping_graph(with_sorter=with_sorter)),
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


# ------------------------------------------- Application Integration packages

#: The repository envelope every Application Integration asset is wrapped in.
_AI_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<aetgt:getResponse xmlns:aetgt="http://schemas.active-endpoints.com/appmodules/repository/2010/10/avrepository.xsd"
                   xmlns:types1="http://schemas.active-endpoints.com/appmodules/repository/2010/10/avrepository.xsd">
   <types1:Item>
      <types1:Name>{name}</types1:Name>
      <types1:Description>{description}</types1:Description>
      <types1:VersionLabel>1.0</types1:VersionLabel>
      <types1:CreatedBy>designer@example.com</types1:CreatedBy>
      <types1:CreationDate>2024-01-26T19:23:04Z</types1:CreationDate>
      <types1:ModifiedBy>designer@example.com</types1:ModifiedBy>
      <types1:ModificationDate>2025-02-20T15:41:45Z</types1:ModificationDate>
      <types1:PublicationStatus>published</types1:PublicationStatus>
      <types1:Entry>{body}</types1:Entry>
      <types1:GUID>{guid}</types1:GUID>
   </types1:Item>
</aetgt:getResponse>
"""


def process_xml(name: str = "p_ORDER_OUT",
                subflow: str = "prcs_log",
                service_connection: str = "app-ORACLE-Create-PO",
                service_object: str = "ORACLE_Create_PO",
                target_connection: str = "app-JDBC-STAGING",
                target_object: str = "STG_RESPONSE") -> str:
    """A process in the shape Process Designer exports.

    Covers what the parser has to get right: elements ordered by their link
    chain rather than document order, a service call nested inside the
    eventContainer that handles its faults, a decision with a conditional and
    a default branch, an assignment, and a create carrying column mappings.
    """
    body = f"""<process xmlns="http://schemas.active-endpoints.com/appmodules/screenflow/2010/10/avosScreenflow.xsd"
            GUID="PROC0001" displayName="{name}" name="{name}">
   <input><parameter description="" name="XML_Input" type="xml"/></input>
   <tempFields>
      <field description="" name="tmp_payload" type="string"/>
      <field description="" name="tmp_status" type="string"/>
   </tempFields>
   <flow id="flow-main">
      <start id="n-start"><link id="l0" targetId="n-assign"/></start>
      <create id="n-create">
         <title>crt_{target_object}</title>
         <entityName>{target_connection}:{target_object}</entityName>
         <createInput>
            <parameter name="ID" source="field" updatable="true">temp.tmp_payload</parameter>
            <parameter name="STATUS" source="field" updatable="true">temp.tmp_status</parameter>
         </createInput>
      </create>
      <assignment id="n-assign">
         <title>asgn_payload</title>
         <operation source="formula" to="temp.tmp_payload">
            <expression language="XQuery">util:toXML($input.XML_Input)</expression>
         </operation>
         <operation source="field" to="temp.tmp_status">output.status</operation>
         <link id="l1" targetId="n-event"/>
      </assignment>
      <eventContainer id="n-event">
         <service id="n-service">
            <title>srv_call_oracle</title>
            <serviceName>{service_connection}:{service_object}</serviceName>
            <serviceInput>
               <parameter name="PAYLOAD" source="field">temp.tmp_payload</parameter>
            </serviceInput>
         </service>
         <flow id="flow-fault">
            <subflow id="n-sub">
               <title>{subflow}</title>
               <subflowGUID>SUB00001</subflowGUID>
               <subflowPath>{subflow}</subflowPath>
               <runForEach>false</runForEach>
               <input>
                  <parameter name="in_detail" source="constant" updatable="true">Oracle call failed</parameter>
               </input>
               <outputDef/>
            </subflow>
         </flow>
         <link id="l2" targetId="n-decide"/>
      </eventContainer>
      <container id="n-decide" type="exclusive">
         <title>dec_status</title>
         <flow id="flow-ok">
            <throw id="n-throw">
               <title>Throw 1</title>
               <throwInput>
                  <parameter name="code" source="field" updatable="true">temp.tmp_status</parameter>
               </throwInput>
            </throw>
         </flow>
         <flow id="flow-else"/>
         <link id="l3" targetId="flow-ok" type="containerLink">
            <condition source="formula">
               <function name="set"><arg name="field">{{$temp.tmp_status}}</arg></function>
            </condition>
         </link>
         <link id="l4" targetId="n-create" type="containerLink"/>
      </container>
      <end id="n-end"/>
   </flow>
</process>"""
    return _AI_ENVELOPE.format(name=name, description="Outbound order process.",
                               body=body, guid="PROC0001")


def ai_connection_xml(name: str = "app-JDBC-STAGING",
                      url: str = "jdbc:oracle:thin:@//db.example.internal:1521/STG",
                      username: str = "STAGING1") -> str:
    body = f"""<connection xmlns="http://schemas.informatica.com/socrates/data-services/2014/04/avosConnections.xsd"
            displayName="{name}" label="{name}" name="{name}">
   <description>Staging database</description>
   <agent>AGENT01</agent>
   <attributes>
      <attribute encrypted="false" name="JDBC Connection URL" value="{url}"/>
      <attribute encrypted="false" name="Username" value="{username}"/>
      <attribute encrypted="false" name="Schema" value=""/>
      <attribute encrypted="true" name="Password" value=""/>
   </attributes>
</connection>"""
    return _AI_ENVELOPE.format(name=name, description="Staging database",
                               body=body, guid="CONNAI01")


def write_process_package(path: Path, *, project: str = "Orders",
                          process_name: str = "p_ORDER_OUT",
                          subflow: str = "prcs_log",
                          with_connection: bool = True) -> Path:
    """Write an Application Integration export: a process, its subprocess and
    optionally the connection definition one of them uses."""
    path = Path(path)
    folder = f"/Explore/{project}"
    objects: List[Dict] = [
        _exported(process_name, "PROCESS", "PROC0001", folder,
                  "Outbound order process."),
        _exported(subflow, "PROCESS", "SUB00001", folder, "Shared logger."),
    ]
    if with_connection:
        objects.append(_exported("app-JDBC-STAGING", "AI_CONNECTION",
                                 "CONNAI01", folder))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as pkg:
        pkg.writestr("exportMetadata.v2.json", json.dumps({
            "name": f"{process_name}-1791572657646",
            "sourceOrgId": "ORG1", "sourceOrgName": "ACME_DEV",
            "exportedObjects": objects,
        }))
        pkg.writestr(f"Explore/{project}/{process_name}.PROCESS.xml",
                     process_xml(name=process_name, subflow=subflow))
        # The subprocess calls nothing, so it is unambiguously not the root.
        pkg.writestr(f"Explore/{project}/{subflow}.PROCESS.xml",
                     process_xml(name=subflow, subflow="",
                                 target_object="STG_LOG"))
        if with_connection:
            pkg.writestr(f"Explore/{project}/app-JDBC-STAGING.AI_CONNECTION.xml",
                         ai_connection_xml())
    return path
