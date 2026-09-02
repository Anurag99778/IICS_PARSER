"""End-to-end: an IICS export ZIP in, analysis documents out."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .enrich import ai as ai_enrich
from .enrich.overrides import apply_overrides
from .extract.package import ExportPackage
from .model.ir import Integration
from .parse.build import build_integration
from .render.excel import write_workbook
from .render.word import write_document


@dataclass
class Result:
    """What one package produced."""

    zip_path: Path
    integration: Optional[Integration] = None
    excel: Optional[Path] = None
    word: Optional[Path] = None
    ir_json: Optional[Path] = None
    error: str = ""
    outputs: List[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def name(self) -> str:
        if self.integration:
            return self.integration.meta.taskflow_name or self.zip_path.stem
        return self.zip_path.stem


def process(zip_path: Path, out_dir: Path, *,
            overrides: Optional[Dict] = None,
            use_ai: bool = False,
            template: Optional[Path] = None,
            formats: str = "all",
            dump_ir: bool = False) -> Result:
    """Parse one export package and render the requested documents."""
    zip_path, out_dir = Path(zip_path), Path(out_dir)
    result = Result(zip_path=zip_path)

    try:
        with tempfile.TemporaryDirectory(prefix="iics_") as tmp:
            workdir = Path(tmp) / "package"
            images = out_dir / "images"

            package = ExportPackage.open(zip_path, workdir)
            integration = build_integration(package, image_dir=images)
            result.integration = integration

            # Precedence: overrides beat AI, AI only fills what is still empty.
            if use_ai:
                ai_enrich.enrich(integration)
            apply_overrides(integration, overrides or {})

            stem = integration.meta.taskflow_name or zip_path.stem
            out_dir.mkdir(parents=True, exist_ok=True)

            if formats in ("all", "excel"):
                result.excel = write_workbook(
                    integration, out_dir / f"{stem}_Analysis.xlsx")
                result.outputs.append(result.excel)

            if formats in ("all", "word"):
                result.word = write_document(
                    integration, out_dir / f"{stem}_Analysis.docx", template=template)
                result.outputs.append(result.word)

            if dump_ir:
                result.ir_json = out_dir / f"{stem}_ir.json"
                with result.ir_json.open("w", encoding="utf-8") as fh:
                    json.dump(integration.to_dict(), fh, indent=2, default=str)
                result.outputs.append(result.ir_json)

    except Exception as exc:                       # noqa: BLE001 - one bad zip
        result.error = f"{type(exc).__name__}: {exc}"   # must not stop a batch

    return result


def process_many(zips: List[Path], out_dir: Path, **kwargs) -> List[Result]:
    """Process a batch, isolating each package so one failure cannot stop the rest."""
    return [process(z, out_dir, **kwargs) for z in zips]
