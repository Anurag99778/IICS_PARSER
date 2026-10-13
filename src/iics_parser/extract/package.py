"""Unpack an IICS export ZIP and index its assets.

An export package looks like::

    ContentsofExportPackage_<name>.csv
    exportMetadata.v2.json
    Explore/<Project>/tf_*.TASKFLOW.xml
    Explore/<Project>/m_*.DTEMPLATE.zip      -> bin/@3.bin  (mapping graph)
                                                bin/@2.bin  (preview JPEG)
    Explore/<Project>/mct_*.MTT.zip          -> mtTask.json
    Explore/<Project>/fit_*.MI_TASK.dat      (plain JSON)
    SYS/cn_*.Connection.zip                  -> connection.json

Asset zips are expanded in place so the rest of the pipeline only ever sees
directories.
"""

from __future__ import annotations

import csv
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Asset:
    """One object inside the export package."""

    name: str
    obj_type: str          # TASKFLOW / DTEMPLATE / MTT / MI_TASK / Connection / ...
    path: Path             # file for .xml/.dat, directory for expanded zips
    guid: str = ""
    obj_path: str = ""     # IICS folder, e.g. /Explore/Concur
    description: str = ""

    def file(self, *names: str) -> Optional[Path]:
        """Return the first existing named file inside an expanded asset."""
        for n in names:
            p = self.path / n if self.path.is_dir() else self.path.parent / n
            if p.exists():
                return p
        return None

    def load_json(self, *names: str):
        p = self.file(*names)
        if p is None:
            return None
        with p.open(encoding="utf-8-sig") as fh:
            return json.load(fh)


class ExportPackage:
    """An extracted IICS export package with its assets indexed by type."""

    def __init__(self, root: Path):
        self.root = root
        self.assets: List[Asset] = []
        self.metadata: Dict = {}
        self.warnings: List[str] = []
        self.unsupported: Dict[str, List[str]] = {}
        self._index()

    # ------------------------------------------------------------------ load

    @classmethod
    def open(cls, zip_path: Path, workdir: Path) -> "ExportPackage":
        """Extract ``zip_path`` into ``workdir`` and index it."""
        zip_path = Path(zip_path)
        workdir = Path(workdir)
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, workdir)

        # Expand nested asset zips in place: foo.DTEMPLATE.zip -> foo.DTEMPLATE/
        for nested in sorted(workdir.rglob("*.zip")):
            target = nested.with_suffix("")
            try:
                with zipfile.ZipFile(nested) as zf:
                    target.mkdir(parents=True, exist_ok=True)
                    _safe_extract(zf, target)
                nested.unlink()
            except zipfile.BadZipFile:
                continue

        return cls(workdir)

    # ----------------------------------------------------------------- index

    def _index(self) -> None:
        meta_file = self.root / "exportMetadata.v2.json"
        if meta_file.exists():
            with meta_file.open(encoding="utf-8-sig") as fh:
                self.metadata = json.load(fh)

        # GUID/description lookup from package metadata
        info: Dict[str, Dict] = {}
        for obj in self.metadata.get("exportedObjects", []) or []:
            info[obj.get("objectName", "")] = obj

        seen = set()
        for path in sorted(self.root.rglob("*")):
            name, obj_type = _classify(path)
            if not obj_type or path in seen:
                continue
            seen.add(path)
            meta = info.get(name, {})
            self.assets.append(
                Asset(
                    name=name,
                    obj_type=obj_type,
                    path=path,
                    guid=meta.get("objectGuid", ""),
                    obj_path=meta.get("path", ""),
                    description=(meta.get("metadata", {}) or {})
                    .get("additionalInfo", {})
                    .get("description")
                    or "",
                )
            )

        # A package with no taskflow is normal - IICS exports single assets as
        # readily as whole orchestrations. build.py documents what is there and
        # says what it could not establish, so nothing is warned about here.

        self._audit_unrecognised()

    def _audit_unrecognised(self) -> None:
        """Name exported objects whose type this parser cannot read.

        Silently skipping an unknown suffix is how a package of the wrong
        product family produced a workbook of empty sheets that still claimed
        full coverage: nothing was parsed, so nothing was missing. The export
        metadata lists what the package is *supposed* to contain, so compare
        against that rather than against what we happened to recognise.
        """
        indexed = {a.name for a in self.assets}
        unknown: Dict[str, List[str]] = {}
        for obj in self.listed_objects:
            name, obj_type = obj.get("objectName", ""), obj.get("objectType", "")
            if not name or obj_type in _CONTAINER_TYPES or name in indexed:
                continue
            unknown.setdefault(obj_type, []).append(name)

        self.unsupported = unknown
        for obj_type in sorted(unknown):
            names = sorted(unknown[obj_type])
            shown = ", ".join(names[:4]) + (" ..." if len(names) > 4 else "")
            self.warnings.append(
                f"{len(names)} {obj_type} object(s) could not be read and are "
                f"absent from this analysis: {shown}"
            )

        if unknown and not any(a.obj_type in _PARSEABLE_TYPES for a in self.assets):
            self.warnings.insert(0, (
                "Nothing in this package could be parsed. It holds "
                + ", ".join(f"{len(v)} {k}" for k, v in sorted(unknown.items()))
                + " - these are Application Integration (Process Designer) "
                "assets, and this parser reads Data Integration assets "
                "(taskflows, mappings, mapping tasks). The documents below are "
                "empty for that reason, not because the integration is empty."
            ))

    @property
    def listed_objects(self) -> List[Dict[str, str]]:
        """Objects the package says it contains, from metadata or the CSV."""
        objs = self.metadata.get("exportedObjects") or []
        if objs:
            return objs
        return [{"objectName": r.get("objectName", ""),
                 "objectType": r.get("objectType", "")}
                for r in self.contents_csv]

    # ---------------------------------------------------------------- access

    def by_type(self, obj_type: str) -> List[Asset]:
        return [a for a in self.assets if a.obj_type == obj_type]

    def by_name(self, name: str) -> Optional[Asset]:
        for a in self.assets:
            if a.name == name:
                return a
        return None

    @property
    def contents_csv(self) -> List[Dict[str, str]]:
        for p in self.root.glob("ContentsofExportPackage*.csv"):
            with p.open(encoding="utf-8-sig", newline="") as fh:
                return list(csv.DictReader(fh))
        return []

    @property
    def project_name(self) -> str:
        for row in self.contents_csv:
            if row.get("objectType") == "Project":
                return row.get("objectName", "")
        tf = self.by_type("TASKFLOW")
        if tf and tf[0].obj_path:
            return tf[0].obj_path.rstrip("/").split("/")[-1]
        return ""


# --------------------------------------------------------------------- utils

#: Suffix -> object type. Order matters: longest/most specific first.
_SUFFIXES = [
    (".TASKFLOW.xml", "TASKFLOW"),
    (".PROCESS_OBJECT.xml", "PROCESS_OBJECT"),
    (".PROCESS.xml", "PROCESS"),
    (".AI_CONNECTION.xml", "AI_CONNECTION"),
    (".AI_SERVICE_CONNECTOR.xml", "AI_SERVICE_CONNECTOR"),
    (".MI_TASK.dat", "MI_TASK"),
    (".Project.json", "Project"),
    (".DTEMPLATE", "DTEMPLATE"),
    (".MTT", "MTT"),
    (".Connection", "Connection"),
    (".HSCHEMA", "HSCHEMA"),
    (".AgentGroup", "AgentGroup"),
]


#: Object types that are containers, not assets - never "unsupported".
_CONTAINER_TYPES = {"Folder", "Project"}

#: Types that carry the content the analysis is built from.
_PARSEABLE_TYPES = {"TASKFLOW", "DTEMPLATE", "MTT", "MI_TASK", "PROCESS"}


def _classify(path: Path) -> tuple:
    """Return ``(asset_name, object_type)`` for a package path, or ``("", "")``."""
    n = path.name
    for suffix, obj_type in _SUFFIXES:
        if n.endswith(suffix):
            # Directory assets must be directories; file assets must be files.
            is_dir_asset = "." not in suffix[1:]
            if is_dir_asset and not path.is_dir():
                continue
            if not is_dir_asset and not path.is_file():
                continue
            return n[: -len(suffix)], obj_type
    return "", ""


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract guarding against path traversal (``../``) entries."""
    dest = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"Unsafe path in archive: {member.filename}")
    zf.extractall(dest)
