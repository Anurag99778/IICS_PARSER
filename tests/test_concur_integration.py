"""End-to-end tests against the Concur employee-outbound export package.

This package is the reference integration: it exercises mapping tasks, mass
ingestion, command tasks, notifications, an assignment, parallel paths, a
decision, unconnected lookups, a router, a hierarchy parser and pre-SQL. If a
change to support some *other* integration breaks any assertion here, that is
the signal to look again.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from iics_parser.extract.package import ExportPackage
from iics_parser.parse.build import build_integration
from iics_parser.parse.mapping import flow_string
from iics_parser.pipeline import process
from iics_parser.render.rows import field_level_rows, mapping_detail_rows

SAMPLE = (Path(__file__).resolve().parent.parent / "samples"
          / "tf_HUB_CONCUR_EMPLOYEE_DETAILS_OUTBOUND_FIN_I_HR001.zip")

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample package not present")


@pytest.fixture(scope="module")
def integration(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("pkg")
    package = ExportPackage.open(SAMPLE, workdir / "extracted")
    return build_integration(package, image_dir=workdir / "images")


# ------------------------------------------------------------------ package

def test_every_asset_is_recognised(tmp_path):
    package = ExportPackage.open(SAMPLE, tmp_path / "x")
    kinds = {a.obj_type for a in package.assets}
    assert {"TASKFLOW", "DTEMPLATE", "MTT", "MI_TASK", "Connection"} <= kinds
    assert len(package.by_type("DTEMPLATE")) == 9
    assert len(package.by_type("MTT")) == 9
    assert len(package.by_type("MI_TASK")) == 4
    assert len(package.by_type("Connection")) == 7
    assert package.project_name == "Concur"


def test_parses_without_warnings(integration):
    """A clean package should need no human review."""
    assert integration.warnings == []


# --------------------------------------------------------------- taskflow

def test_steps_are_in_execution_order(integration):
    """Order comes from the link graph, not document order.

    ``ast_ntf_params`` sits near the end of the XML but runs second, so this
    also guards against a regression back to document order.
    """
    order = [s.title for s in integration.steps]
    expected_head = [
        "Get Param Values",
        "ast_ntf_params",
        "Get PM End dated Details from ERP ESS",
        "Unzip ESS",
        "Generate FileLIST",
        "Parse XML To STG",
    ]
    assert order[:6] == expected_head
    # The parallel pair runs after Parse XML To STG and before Update Approver.
    assert order.index("Load TRX 760 Data To LND") > order.index("Parse XML To STG")
    assert order.index("Update Approver Details in STG") > order.index("Load TRX 710 data to LND")


def test_parallel_branches_get_dotted_numbers(integration):
    by_title = {s.title: s for s in integration.steps}
    assert by_title["Load TRX 760 Data To LND"].seq == "7.1"
    assert by_title["Load TRX 710 data to LND"].seq == "7.2"
    assert by_title["Archive Files"].seq.endswith(".1")
    assert by_title["Archive ESS"].seq.endswith(".2")


def test_step_numbers_are_unique(integration):
    seqs = [s.seq for s in integration.steps]
    assert len(seqs) == len(set(seqs)), f"duplicate step numbers: {seqs}"


def test_step_types_are_classified(integration):
    by_title = {s.title: s.step_type for s in integration.steps}
    assert by_title["Get Param Values"] == "Mapping Task"
    assert by_title["Unzip ESS"] == "Mass Ingestion Task"
    assert by_title["Generate FileLIST"] == "Command Task"
    assert by_title["Success Notification"] == "Notification Task"
    assert by_title["ast_ntf_params"] == "Assignment"


def test_error_handling_is_captured(integration):
    step = next(s for s in integration.steps if s.title == "Get Param Values")
    assert "Suspend Taskflow" in step.on_error


def test_command_task_script_is_captured(integration):
    step = next(s for s in integration.steps if s.title == "Generate FileLIST")
    params = {p.name: p.value for p in step.parameters}
    assert params["Script File Name"] == r"D:\ps-scripts\file_list_generator.bat"
    assert "CONCUR__PM_END_DATED_DETAILS_LIST.txt" in params["Input Arguments"]


def test_assignment_expressions_are_captured(integration):
    step = next(s for s in integration.steps if s.title == "ast_ntf_params")
    params = {p.name: p.value for p in step.parameters}
    assert params["tmp_org_id"] == "util:getOrganizationId()"
    assert params["tmp_timestamp"] == "fn:current-dateTime()"


def test_notification_body_html_is_stripped(integration):
    step = next(s for s in integration.steps if s.title == "Success Notification")
    body = next(p.value for p in step.parameters if p.name == "Email_Body")
    assert "<p>" not in body and "&lt;" not in body
    assert "Environment :" in body


# ---------------------------------------------------------------- mappings

def test_tasks_are_linked_to_their_mappings(integration):
    for step in integration.steps:
        if step.task and step.task.task_type == "MCT":
            assert step.task.mapping is not None, f"{step.task.name} has no mapping"


def test_transformation_kinds_resolve_from_class_table(integration):
    kinds = {tx.kind for m in integration.mappings for tx in m.transformations}
    assert {"Source", "Target", "Expression", "Lookup", "Router",
            "Aggregator", "Sorter", "Filter"} <= kinds
    # Nothing should fall through to the unsupported path in this package.
    assert not [tx for m in integration.mappings
                for tx in m.transformations if tx.unsupported]


def test_expression_fields_are_extracted(integration):
    mapping = next(m for m in integration.mappings
                   if m.name == "m_LND_STG_CONCUR_LOAD_TRX_710_OUTBOUND_PROJECTS_DATA")
    exprs = {f.name: f.expression
             for tx in mapping.expressions for f in tx.expression_fields}
    assert exprs["O_TRX710"] == "710"
    assert exprs["O_approval_type"] == "'EXP'"
    assert exprs["v_project_unit"] == ":LKP.lkp_project_unit(PROJECT_NUMBER)"
    assert exprs["o_proj_unit"].startswith("IIF(NOT ISNULL(v_project_unit)")


def test_connection_types_use_user_facing_names(integration):
    types = {c.name: c.conn_type for c in integration.connections}
    assert types["cn_STAGING1"] == "Oracle"
    assert types["cn_LOCAL_FF"] == "Flat File"          # not the internal "CSVFile"
    assert types["cn_MSSQL_AIR_JobRun"] == "SQL Server"
    assert types["cn_SFTP_Concur_TEST"] == "Advanced SFTP V2"


def test_source_and_target_connections_resolve(integration):
    mapping = next(m for m in integration.mappings
                   if m.name == "m_LND_STG_CONCUR_LOAD_TRX_710_OUTBOUND_PROJECTS_DATA")
    source = mapping.sources[0]
    assert source.connection_display == "cn_STAGING1 (Oracle)"
    assert source.object_name == "LND_LKP_PROJECTS_TOPIC"


def test_unconnected_lookup_detail(integration):
    mapping = next(m for m in integration.mappings
                   if m.name == "m_LND_STG_CONCUR_LOAD_TRX_710_OUTBOUND_PROJECTS_DATA")
    lookup = mapping.lookups[0]
    assert lookup.name == "lkp_project_unit"
    assert lookup.lookup_unconnected is True
    assert lookup.object_name == "LND_ERP_CONCUR_PROJECT_TASK"
    assert str(lookup.lookup_conditions[0]) == "PROJECT_NUMBER = in_projects_number"


def test_pre_sql_is_captured(integration):
    mapping = next(m for m in integration.mappings
                   if m.name == "m_ERP_LND_CONCUR_STG_TRX_760_EMPLOYEE_OUTBOUND")
    target = next(t for t in mapping.targets if t.pre_sql)
    assert "DELETE FROM LND_CONCUR_PM_FUTURE_ENDDATE" in target.pre_sql


def test_flow_string_matches_hand_written_analysis(integration):
    """These strings were written by hand in the reference analysis."""
    parse_xml = next(m for m in integration.mappings
                     if m.name == "m_FF_LND_PARSE_ERP_CONCUR_PM_END_DATED_DETAILS")
    assert flow_string(parse_xml) == (
        "src_ERP_ESS_CONCUR_PM_FILE_LIST->HierarchyParser->exp_data_conv->"
        "tgt_LND_ERP_CONCUR_PM_ENDDATE_DATA"
    )

    trx710 = next(m for m in integration.mappings
                  if m.name == "m_LND_STG_CONCUR_LOAD_TRX_710_OUTBOUND_PROJECTS_DATA")
    lines = flow_string(trx710).splitlines()
    assert lines[0] == ("src_LND_LKP_PROJECTS_TOPIC->exp_trx710_conv->"
                        "tgt_MOR_EMPLOYEE_OUTBOUND_TRX_710")
    assert lines[1] == "Unconnected Lookup: lkp_project_unit"


def test_router_produces_a_chain_per_target(integration):
    mapping = next(m for m in integration.mappings
                   if m.name == "m_ERP_LND_CONCUR_STG_TRX_760_EMPLOYEE_OUTBOUND")
    chains = flow_string(mapping).splitlines()
    assert any(c.endswith("tgt_MOR_EMPLOYEE_OUTBOUND_TRX_760") for c in chains)
    assert any(c.endswith("tgt_FUTURE_EMPLOYEE_OUTBOUND_TRX_760") for c in chains)


def test_mass_ingestion_details(integration):
    task = next(t for t in integration.tasks
                if t.name == "fit_LOCAL_FF_CONCUR_SFTP_EMPLOYEE_DETAILS")
    assert task.target.connection_type == "Advanced SFTP V2"
    assert task.target.connection_name == "cn_SFTP_Concur_TEST"
    assert task.target.directory == "/in"
    assert task.target.actions == ["PGPEncrypt"]
    assert task.target.file_exists_action == "Overwrite"


def test_in_out_parameters(integration):
    task = next(t for t in integration.tasks
                if t.name == "mct_DUMMY_CONCUR_EMPLOYEE_OUTBOUND_GET_PARAM")
    params = {p.name: p.value for p in task.in_out_parameters}
    assert params["p_Env"] == "MORTENSON_DEV2"
    assert "concuradmin@mortenson.com" in params["p_Email"]
    assert task.parameter_file == "Concur_Employee_Outbound_Details.param"


def test_mapping_previews_are_extracted(integration):
    with_preview = [m for m in integration.mappings if m.preview_image]
    assert len(with_preview) == 9
    assert all(Path(m.preview_image).exists() for m in with_preview)


# ------------------------------------------------------------------- rows

def test_mapping_detail_rows_shape(integration):
    rows = mapping_detail_rows(integration)
    assert rows and all(len(r) == 15 for r in rows)
    # Every step appears at least once.
    seqs = {r[0] for r in rows if r[0]}
    assert seqs == {s.seq for s in integration.steps}


def test_router_target_gets_its_own_row(integration):
    rows = mapping_detail_rows(integration)
    targets = [r[12] for r in rows]
    assert "MOR_EMPLOYEE_OUTBOUND_TRX_760" in targets
    assert "LND_CONCUR_PM_FUTURE_ENDDATE" in targets


def test_field_level_rows_contain_expressions(integration):
    rows = field_level_rows(integration)
    pairs = {(r[5], r[6]) for r in rows}
    assert ("O_TRX710", "710") in pairs
    assert ("o_pm_end_date", "SUBSTR(project_manager_end_date,1,10)") in pairs
    assert ("tmp_org_id", "util:getOrganizationId()") in pairs


# -------------------------------------------------------------- end to end

def test_pipeline_writes_both_documents(tmp_path):
    result = process(SAMPLE, tmp_path, dump_ir=True)
    assert result.ok, result.error
    assert result.excel.exists() and result.excel.stat().st_size > 5000
    assert result.word.exists() and result.word.stat().st_size > 5000
    assert result.ir_json.exists()


def test_pipeline_reports_a_bad_zip_without_raising(tmp_path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip file")
    result = process(broken, tmp_path / "out")
    assert not result.ok and result.error


def test_overrides_win_over_parsed_values(tmp_path):
    result = process(SAMPLE, tmp_path, overrides={
        "defaults": {"business_owner": "Finance"},
        "integrations": {
            "tf_HUB_CONCUR_EMPLOYEE_DETAILS_OUTBOUND_FIN_I_HR001": {
                "criticality": "Medium", "project": "OverriddenProject",
            }
        },
    })
    assert result.integration.meta.business_owner == "Finance"
    assert result.integration.meta.criticality == "Medium"
    assert result.integration.meta.project == "OverriddenProject"


def test_unknown_override_key_is_reported(tmp_path):
    result = process(SAMPLE, tmp_path, overrides={"defaults": {"nonsense_field": "x"}})
    assert any("nonsense_field" in w for w in result.integration.warnings)


def test_source_filters_appear_on_their_own_row(integration):
    """Each source's read filter belongs to that source's leg, not the step."""
    rows = mapping_detail_rows(integration)
    by_source = {r[9]: r[10] for r in rows if r[9]}
    assert "LOGINID IS NOT NULL" in by_source["MOR_EMPLOYEE_OUTBOUND_TRX_305"]
    assert "EMPLOYEE_ID IS NOT NULL" in by_source["MOR_EMPLOYEE_OUTBOUND_TRX_710"]
    # A source with no filter of its own must not inherit a sibling's.
    assert "LOGINID" not in by_source["MOR_EMPLOYEE_OUTBOUND_TRX_760"]


def test_shaping_transformations_are_reported(integration):
    """An Aggregator or Sorter changes the output and must be visible."""
    rows = mapping_detail_rows(integration)
    lnd_to_ff = [r for r in rows if r[4] == "m_LND_TO_FF_CONCUR_EMPLOYYE_OUTBOUND"]
    assert any("Aggregator" in r[10] and "Sorter" in r[10] for r in lnd_to_ff)


def test_target_field_mappings_are_captured(integration):
    """Column-level lineage: which incoming field lands in which column."""
    mapping = next(m for m in integration.mappings
                   if m.name == "m_LND_STG_CONCUR_LOAD_TRX_710_OUTBOUND_PROJECTS_DATA")
    target = next(t for t in mapping.targets if t.field_mappings)
    pairs = {fm.from_field: fm.to_field for fm in target.field_mappings}
    assert pairs["O_TRX710"] == "TRX_TYPE"
    assert pairs["o_proj_unit"] == "SEGMENT_1"
    assert pairs["PROJECT_NUMBER"] == "SEGMENT_3"


def test_field_mappings_reach_the_field_level_sheet(integration):
    rows = field_level_rows(integration)
    pairs = {(r[5], r[6]) for r in rows}
    assert ("O_TRX710", "→ TRX_TYPE") in pairs
    assert ("o_proj_unit", "→ SEGMENT_1") in pairs


def test_encryption_and_retention_detail_is_captured(integration):
    """PGP keys and source-file retention matter for a migration."""
    sftp = next(t for t in integration.tasks
                if t.name == "fit_LOCAL_FF_CONCUR_SFTP_EMPLOYEE_DETAILS")
    assert "0x92241FDB9AFF10B5" in sftp.target.action_detail
    unzip = next(t for t in integration.tasks
                 if t.name == "fit_ERP_EXTRACT_CONCUR_LOCAL_FF_UNZIP_PM_DETAILS")
    assert unzip.source.after_pickup == "Archive"
