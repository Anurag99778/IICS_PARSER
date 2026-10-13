"""Two faults a real Parse Report exposed, one in each direction.

* A connection named "Cloud Integration Hub" was reported as not represented
  while it was plainly in the workbook. The audit matched against a set of
  whitespace-split tokens, so a name of several words could never match as one.
  A false alarm - the worst kind, because it teaches a reviewer to ignore the
  report.

* A Sorter's ticked options genuinely never reached the output. Sources and
  targets carry their options on their own row and lookups list theirs with the
  lookup; a transformation that is neither had nowhere to report them. Here the
  audit was right and the renderer was wrong.
"""

from __future__ import annotations

import pytest

from iics_parser import coverage
from iics_parser.extract.package import ExportPackage
from iics_parser.parse.build import build_integration
from iics_parser.render.rows import (connection_detail_rows, field_level_rows,
                                     mapping_detail_rows, object_field_rows)

from . import synthetic


def _rows(integration):
    return (mapping_detail_rows(integration), field_level_rows(integration),
            object_field_rows(integration), connection_detail_rows(integration))


def _text(integration) -> str:
    return "\n".join(str(cell) for rows in _rows(integration)
                     for row in rows for cell in row)


def _build(tmp_path, **kwargs):
    zip_path = synthetic.write_package(tmp_path / "pkg.zip", task_name="mct_X",
                                       **kwargs)
    return build_integration(ExportPackage.open(zip_path, tmp_path / "x"),
                             image_dir=tmp_path / "i")


# ------------------------------------------------ a multi-word connection name

def test_a_connection_named_in_several_words_is_not_reported_missing(tmp_path):
    integration = _build(tmp_path, connection_name="Cloud Integration Hub")

    assert "Cloud Integration Hub" in _text(integration), "precondition"

    lines = coverage.audit(integration, *_rows(integration))
    connections = next(l for l in lines if l.category == "Connections referenced")
    assert connections.complete, connections.missing


def test_a_connection_that_really_is_absent_is_still_reported(tmp_path):
    """The check has to keep failing when it should, or it is worthless."""
    from iics_parser.model.ir import Connection

    integration = _build(tmp_path, connection_name="Cloud Integration Hub")
    integration.connections.append(Connection(name="cn_NEVER_USED_ANYWHERE"))

    lines = coverage.audit(integration, *_rows(integration))
    connections = next(l for l in lines if l.category == "Connections referenced")
    assert not connections.complete
    assert connections.missing == ["cn_NEVER_USED_ANYWHERE"]


# ------------------------------------- options on a shaping transformation

def test_sorter_options_reach_the_sheet(tmp_path):
    """A Sorter is neither an endpoint nor a lookup, so it had no row."""
    integration = _build(tmp_path, with_sorter=True)

    sorter = next(t for t in integration.mappings[0].transformations
                  if t.kind == "Sorter")
    assert sorter.options == ["Case Sensitive", "Null Treated Low"]

    text = _text(integration)
    for option in sorter.options:
        assert option in text, f"{option!r} parsed but never rendered"


def test_every_ticked_option_in_the_package_is_accounted_for(tmp_path):
    integration = _build(tmp_path, with_sorter=True,
                         connection_name="Cloud Integration Hub")

    lines = coverage.audit(integration, *_rows(integration))
    incomplete = [(l.category, l.summary, l.missing)
                  for l in lines if not l.complete]
    assert not incomplete
    assert coverage.overall(lines)["percent"] == 100


def test_a_lookups_options_are_not_reported_twice(tmp_path):
    """Lookups already list their options with the lookup detail."""
    integration = _build(tmp_path)
    mapping = integration.mappings[0]
    lookup = next(t for t in mapping.transformations if t.kind == "Target")
    lookup.kind = "Lookup"
    lookup.options = ["Lookup caching enabled"]

    text = _text(integration)
    assert text.count("Lookup caching enabled") == 1, text
