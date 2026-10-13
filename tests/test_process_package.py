"""An Application Integration export must document like any other package.

A package of processes used to produce a correctly formatted workbook with
every data sheet empty, and a Parse Report claiming full coverage - the parser
recognised none of the asset types, so nothing was missing because nothing was
found. These tests pin both halves: that the assets are read, and that a
package the parser genuinely cannot read says so instead of looking complete.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iics_parser import pipeline
from iics_parser.coverage import audit
from iics_parser.extract.package import ExportPackage
from iics_parser.parse.build import build_integration
from iics_parser.render import rows

from .synthetic import write_process_package


@pytest.fixture(scope="module")
def integration(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("process")
    pkg = write_process_package(tmp / "p_ORDER_OUT.zip")
    return build_integration(ExportPackage.open(pkg, tmp / "work"))


# ------------------------------------------------------------------ the flow

def test_steps_are_parsed(integration):
    assert integration.steps, "a process package produced no steps"
    assert integration.meta.taskflow_name == "p_ORDER_OUT"
    assert integration.meta.project == "Orders"
    assert integration.meta.source_org == "ACME_DEV"


def test_steps_follow_the_link_chain_not_document_order(integration):
    """``create`` is written before ``assignment`` but runs after it."""
    main = [s for s in integration.steps if not s.seq.startswith("S")]
    titles = [s.title for s in main]
    assert titles.index("asgn_payload") < titles.index("crt_STG_RESPONSE")
    assert main[0].step_type == "Start"


def test_service_inside_an_event_container_is_not_lost(integration):
    """The call an eventContainer protects sits beside its fault branches."""
    service = _step(integration, "srv_call_oracle")
    assert service.step_type == "Service Call"
    assert "Fault handled" in service.options

    target = service.task.mapping.targets[0]
    assert target.connection_name == "app-ORACLE-Create-PO"
    assert target.object_name == "ORACLE_Create_PO"


def test_fault_branch_is_labelled_and_nested(integration):
    sub = _step(integration, "prcs_log", seq_prefix="3.")
    assert sub.step_type == "Subprocess"
    assert sub.branch == "On fault"
    assert sub.depth > 0


def test_decision_branches_read_as_conditions(integration):
    throw = _step(integration, "Throw 1")
    assert throw.branch == "tmp_status is set"
    assert throw.on_error


# ------------------------------------------------------------- the data rows

def test_create_carries_its_column_mappings(integration):
    create = _step(integration, "crt_STG_RESPONSE")
    target = create.task.mapping.targets[0]
    assert target.connection_name == "app-JDBC-STAGING"
    assert target.object_name == "STG_RESPONSE"
    assert {f.name: f.mapped_from for f in target.fields} == {
        "ID": "temp.tmp_payload",
        "STATUS": "temp.tmp_status",
    }


def test_assignment_becomes_an_expression(integration):
    assign = _step(integration, "asgn_payload")
    expr = assign.task.mapping.transformations[0]
    assert expr.kind == "Expression"
    assert {f.name: f.expression for f in expr.expression_fields} == {
        "temp.tmp_payload": "util:toXML($input.XML_Input)",
        "temp.tmp_status": "output.status",
    }


def test_temp_fields_are_declarations_not_step_parameters(integration):
    """Dozens of variables must not push the flow off the top of sheet one."""
    start = integration.steps[0]
    assert [p.name for p in start.parameters] == ["XML_Input"]
    names = [f.name for f in start.task.mapping.transformations[0].expression_fields]
    assert names == ["tmp_payload", "tmp_status"]


def test_connection_definition_is_read(integration):
    conn = integration.connection("app-JDBC-STAGING")
    assert conn.host == "db.example.internal:1521"
    assert conn.database == "STG"
    assert conn.schema_label == "STAGING1"
    assert conn.runtime_environment == "AGENT01"
    assert "Oracle" in conn.conn_type


def test_connections_used_but_not_exported_are_named(integration):
    """Their endpoints are real; their host and credentials are not here."""
    assert any("app-ORACLE-Create-PO" in w and "not exported" in w
               for w in integration.warnings)


def test_every_sheet_has_rows(integration):
    assert rows.mapping_detail_rows(integration)
    assert rows.field_level_rows(integration)
    assert rows.object_field_rows(integration)
    assert rows.connection_detail_rows(integration)


def test_coverage_is_complete_and_counts_something(integration):
    lines = audit(integration,
                  rows.mapping_detail_rows(integration),
                  rows.field_level_rows(integration),
                  rows.object_field_rows(integration),
                  rows.connection_detail_rows(integration))
    counted = [ln for ln in lines if ln.present]
    assert counted, "coverage counted nothing at all"
    incomplete = [f"{ln.category}: {ln.missing}" for ln in counted if not ln.complete]
    assert not incomplete, incomplete


def test_process_mappings_do_not_report_a_missing_diagram(integration):
    """A process export never carries one, so none is absent."""
    lines = audit(integration,
                  rows.mapping_detail_rows(integration),
                  rows.field_level_rows(integration),
                  rows.object_field_rows(integration),
                  rows.connection_detail_rows(integration))
    diagrams = next(ln for ln in lines if ln.category == "Mapping diagrams extracted")
    assert diagrams.present == 0


# ------------------------------------------------- end to end, and the guard

def test_documents_are_written_and_not_empty(tmp_path):
    pkg = write_process_package(tmp_path / "p_ORDER_OUT.zip")
    result = pipeline.process(pkg, tmp_path / "out")
    assert result.excel.exists() and result.word.exists()

    import openpyxl
    book = openpyxl.load_workbook(result.excel)
    for sheet in ("Mapping Details", "Field Values", "Source & Target Fields",
                  "Connection Details"):
        populated = [r for r in book[sheet].iter_rows(min_row=3, values_only=True)
                     if any(c not in (None, "") for c in r)]
        assert populated, f"{sheet} came out empty"


def test_unreadable_package_says_so_rather_than_claiming_coverage(tmp_path):
    """The failure that started this: nothing parsed, nothing reported."""
    import json
    import zipfile

    path = tmp_path / "mystery.zip"
    with zipfile.ZipFile(path, "w") as pkg:
        pkg.writestr("exportMetadata.v2.json", json.dumps({
            "name": "mystery-1791572657646",
            "sourceOrgName": "ACME_DEV",
            "exportedObjects": [
                {"objectGuid": "X1", "objectName": "thing",
                 "objectType": "SOME_FUTURE_TYPE", "path": "/Explore/X"},
            ],
        }))
        pkg.writestr("Explore/X/thing.SOME_FUTURE_TYPE.xml", "<unknown/>")

    package = ExportPackage.open(path, tmp_path / "work")
    assert package.unsupported == {"SOME_FUTURE_TYPE": ["thing"]}
    assert any("Nothing in this package could be parsed" in w
               for w in package.warnings)
    assert any("SOME_FUTURE_TYPE" in w for w in package.warnings)


def _step(integration, title: str, seq_prefix: str = ""):
    for step in integration.steps:
        if step.title == title and step.seq.startswith(seq_prefix):
            return step
    raise AssertionError(
        f"no step titled {title!r}; have "
        f"{[(s.seq, s.title) for s in integration.steps]}"
    )
