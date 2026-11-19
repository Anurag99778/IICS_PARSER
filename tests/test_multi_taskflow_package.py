"""Every taskflow in a package has to be documented, not just the first.

Exporting a folder rather than a single asset gives a package with several
taskflows. Only the first was analysed, so the steps of the others never
existed - and because the sheets are built from steps, every mapping those
taskflows called went unreached and every expression, column mapping and sort
key inside them went unreported. A package of four taskflows came out at 53%
coverage with nothing obviously wrong on the face of it.
"""

from __future__ import annotations

import pytest

from iics_parser import pipeline
from iics_parser.coverage import audit
from iics_parser.extract.package import ExportPackage
from iics_parser.parse.build import build_integration
from iics_parser.render import rows

from .synthetic import write_multi_taskflow_package

FLOWS = ["tf_ALPHA", "tf_BETA", "tf_GAMMA"]


@pytest.fixture(scope="module")
def integration(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("multi")
    pkg = write_multi_taskflow_package(tmp / "multi.zip", flows=FLOWS)
    return build_integration(ExportPackage.open(pkg, tmp / "work"))


def test_every_taskflow_contributes_steps(integration):
    assert len(integration.steps) == len(FLOWS)


def test_every_mapping_is_reached_by_a_step(integration):
    """The failure this fixes: mappings stranded by an unparsed taskflow."""
    reached = {s.task.mapping.name for s in integration.steps
               if s.task and s.task.mapping}
    assert reached == {f"m_{f[3:]}" for f in FLOWS}


def test_later_taskflows_are_sequenced_and_labelled(integration):
    """Each extra taskflow continues the numbering under its own name."""
    extra = [s for s in integration.steps if s.seq.startswith("S")]
    assert len(extra) == len(FLOWS) - 1
    assert [s.branch for s in extra] == ["tf_BETA", "tf_GAMMA"]
    assert all(s.depth > 0 for s in extra)


def test_the_exported_taskflow_leads(integration):
    """The package names the asset it was exported for; that one comes first."""
    assert not integration.steps[0].seq.startswith("S")
    assert integration.meta.taskflow_name == "tf_ALPHA"


def test_the_extra_taskflows_are_named_in_the_report(integration):
    note = next((w for w in integration.warnings if "taskflows" in w), "")
    assert "tf_BETA" in note and "tf_GAMMA" in note


def test_a_single_taskflow_package_gains_no_prefix_and_no_note(tmp_path):
    pkg = write_multi_taskflow_package(tmp_path / "one.zip", flows=["tf_ONLY"])
    built = build_integration(ExportPackage.open(pkg, tmp_path / "work"))
    assert built.steps and not any(s.seq.startswith("S") for s in built.steps)
    assert not any("taskflows" in w for w in built.warnings)


# ------------------------------------------------------- what the gap cost

def test_detail_of_later_taskflows_reaches_the_sheets(integration):
    """Not just the step - the mapping's innards have to come through too."""
    detail = "\n".join(str(c) for row in rows.mapping_detail_rows(integration)
                       for c in row)
    fields = "\n".join(str(c) for row in rows.object_field_rows(integration)
                       for c in row)
    for flow in FLOWS:
        assert f"m_{flow[3:]}" in detail
        assert f"STG_{flow[3:]}" in fields
        assert f"DW_{flow[3:]}" in fields


def test_sort_keys_of_later_taskflows_are_reported(integration):
    """Sort keys live on transformations inside the stranded mappings."""
    lines = _audit(integration)
    sort = next(ln for ln in lines if ln.category == "Sort / group-by keys")
    assert sort.present > 0, "fixture exercises no sort keys"
    assert sort.complete, sort.missing


def test_no_mapping_is_left_unreached(integration):
    lines = _audit(integration)
    reached = next(ln for ln in lines
                   if ln.category == "Mappings reached by a step")
    assert reached.complete, reached.missing


def test_documents_carry_every_taskflow(tmp_path):
    pkg = write_multi_taskflow_package(tmp_path / "multi.zip", flows=FLOWS)
    result = pipeline.process(pkg, tmp_path / "out")

    import openpyxl
    book = openpyxl.load_workbook(result.excel)
    text = "\n".join(
        str(c) for sheet in book.worksheets
        for row in sheet.iter_rows(values_only=True) for c in row if c
    )
    for flow in FLOWS:
        assert f"m_{flow[3:]}" in text, f"{flow}'s mapping missing from workbook"


def _audit(integration):
    return audit(integration,
                 rows.mapping_detail_rows(integration),
                 rows.field_level_rows(integration),
                 rows.object_field_rows(integration),
                 rows.connection_detail_rows(integration))
