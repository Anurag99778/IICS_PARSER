"""Human-supplied values for fields no export package can contain.

Business owner, criticality, environments and similar are organisational facts,
not metadata. They live in a YAML file next to the outputs so a re-run never
overwrites what a person typed:

.. code-block:: yaml

    # overrides.yaml
    defaults:                       # applied to every integration
      business_owner: Finance
      environments: DEV, TEST, UAT, PROD
    integrations:
      tf_HUB_CONCUR_EMPLOYEE_DETAILS_OUTBOUND_FIN_I_HR001:
        criticality: Medium
        technical_owner: Finance Solutions - Concur

Precedence is overrides > AI draft > parsed value, so a human always wins.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..model.ir import Integration, IntegrationMeta

#: Fields an override file is allowed to set.
OVERRIDABLE = {f.name for f in dataclass_fields(IntegrationMeta)}


def load_overrides(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def apply_overrides(integration: Integration, overrides: Dict[str, Any]) -> None:
    """Apply defaults then integration-specific values onto the metadata."""
    if not overrides:
        return
    name = integration.meta.taskflow_name

    for source in (overrides.get("defaults") or {},
                   (overrides.get("integrations") or {}).get(name) or {}):
        for key, value in source.items():
            if key not in OVERRIDABLE:
                integration.warnings.append(
                    f"Override '{key}' is not a known field and was ignored"
                )
                continue
            if value not in (None, ""):
                setattr(integration.meta, key, str(value))


def write_template(integration: Integration, path: Path) -> Path:
    """Write a starter overrides file listing the fields still unfilled."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    meta = integration.meta
    needs = [f for f in sorted(OVERRIDABLE) if not getattr(meta, f, "")]
    body = {
        "defaults": {},
        "integrations": {meta.taskflow_name: {f: "" for f in needs}},
    }
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# Values the export package cannot supply. Anything set here\n"
                 "# wins over parsed and AI-drafted values.\n")
        yaml.safe_dump(body, fh, sort_keys=False, default_flow_style=False)
    return path
