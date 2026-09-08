"""A small web front end: upload an IICS export ZIP, download the analysis.

The whole application is the pipeline the CLI already drives, so anything the
command line produces the browser produces identically. Nothing is stored
permanently - each upload becomes a job directory that a sweeper removes once
it is older than ``JOB_TTL_HOURS``.

It is designed to be served under a path prefix (``/parserIICS`` by default) so
it can sit alongside whatever else the host already serves. The prefix works
both ways round: when a reverse proxy passes the full path through, and when it
strips the prefix before forwarding.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Optional

from flask import (Flask, abort, redirect, render_template, request,
                   send_file, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from .. import coverage
from ..enrich import ai as ai_enrich
from ..enrich.overrides import load_overrides
from ..pipeline import Result, process

#: Where job directories live. A volume in the container image.
WORK_DIR = Path(os.environ.get("WORK_DIR", "/data/jobs"))

#: How long a finished job stays downloadable before the sweeper removes it.
JOB_TTL_HOURS = float(os.environ.get("JOB_TTL_HOURS", "6"))

#: Largest upload accepted, in megabytes. Export packages are usually a few MB;
#: the ceiling is here so a stray huge file cannot fill the disk.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

URL_PREFIX = os.environ.get("URL_PREFIX", "/parserIICS").rstrip("/")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    @app.get("/health")
    def health():
        return {"status": "ok", "ai": ai_enrich.is_available()}

    @app.get("/")
    def index():
        return render_template("index.html", results=None,
                               ai_available=ai_enrich.is_available(),
                               ai_model=ai_enrich.model_name(),
                               max_mb=MAX_UPLOAD_MB)

    @app.post("/")
    def upload():
        uploads = [f for f in request.files.getlist("package") if f and f.filename]
        if not uploads:
            return render_template("index.html", results=None, error="Choose a .zip export package first.",
                                   ai_available=ai_enrich.is_available(),
                                   ai_model=ai_enrich.model_name(),
                                   max_mb=MAX_UPLOAD_MB), 400

        _sweep()
        job = uuid.uuid4().hex[:12]
        job_dir = WORK_DIR / job
        (job_dir / "in").mkdir(parents=True, exist_ok=True)
        out_dir = job_dir / "out"

        use_ai = bool(request.form.get("use_ai")) and ai_enrich.is_available()
        overrides = _read_overrides(job_dir, request.files.get("overrides"))

        views = []
        for upload_file in uploads:
            name = secure_filename(upload_file.filename or "package.zip")
            if not name.lower().endswith(".zip"):
                views.append({"name": name, "error": "Not a .zip file."})
                continue
            saved = job_dir / "in" / name
            upload_file.save(saved)
            if not zipfile.is_zipfile(saved):
                views.append({"name": name, "error": "This file is not a readable ZIP archive."})
                continue
            views.append(_view(job, process(saved, out_dir, overrides=overrides,
                                            use_ai=use_ai)))

        return render_template("index.html", results=views, job=job,
                               any_ok=any(v.get("files") for v in views),
                               ai_available=ai_enrich.is_available(),
                               ai_model=ai_enrich.model_name(),
                               max_mb=MAX_UPLOAD_MB)

    @app.get("/download/<job>/<path:filename>")
    def download(job: str, filename: str):
        out_dir = _job_out(job)
        if out_dir is None:
            abort(404)
        return send_from_directory(out_dir, filename, as_attachment=True)

    @app.get("/bundle/<job>.zip")
    def download_all(job: str):
        out_dir = _job_out(job)
        if out_dir is None:
            abort(404)
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(out_dir.iterdir()):
                if path.is_file():
                    zf.write(path, path.name)
        buffer.seek(0)
        return send_file(buffer, mimetype="application/zip", as_attachment=True,
                         download_name=f"IICS_Analysis_{job}.zip")

    @app.errorhandler(413)
    def too_large(_):
        return render_template(
            "index.html", results=None,
            error=f"That upload is larger than the {MAX_UPLOAD_MB} MB limit.",
            ai_available=ai_enrich.is_available(),
            ai_model=ai_enrich.model_name(), max_mb=MAX_UPLOAD_MB), 413

    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, URL_PREFIX)
    return app


# ----------------------------------------------------------------- internals

def _view(job: str, result: Result) -> dict:
    """What the results page needs to know about one processed package."""
    if not result.ok:
        return {"name": result.zip_path.name, "error": result.error}

    integration = result.integration
    lines = coverage.audit(integration, *_rows(integration))
    totals = coverage.overall(lines)

    return {
        "name": result.name,
        "files": [
            {"label": "Excel analysis", "filename": p.name,
             "url": url_for("download", job=job, filename=p.name)}
            for p in (result.excel, result.word) if p
        ],
        "steps": len(integration.steps),
        "mappings": len(integration.mappings),
        "connections": len(integration.connections),
        "coverage": totals["percent"],
        "incomplete": [f"{l.category}: {l.summary}" for l in lines if not l.complete],
        "warnings": list(integration.warnings),
    }


def _rows(integration):
    from ..render.rows import (field_level_rows, mapping_detail_rows,
                               object_field_rows)
    return (mapping_detail_rows(integration), field_level_rows(integration),
            object_field_rows(integration))


def _read_overrides(job_dir: Path, upload_file) -> dict:
    """An optional overrides.yaml uploaded alongside the package."""
    if not upload_file or not upload_file.filename:
        return {}
    path = job_dir / "overrides.yaml"
    upload_file.save(path)
    return load_overrides(str(path)) or {}


def _job_out(job: str) -> Optional[Path]:
    """The output directory of ``job``, or None if the name is not a job id."""
    if not job.isalnum():
        return None
    out_dir = (WORK_DIR / job / "out").resolve()
    if not str(out_dir).startswith(str(WORK_DIR.resolve())) or not out_dir.is_dir():
        return None
    return out_dir


def _sweep() -> None:
    """Delete job directories past their time to live.

    Nothing uploaded here is meant to be kept: the documents are downloaded and
    the export packages can contain connection detail that should not sit on
    disk indefinitely.
    """
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    for path in WORK_DIR.glob("*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


class _PrefixMiddleware:
    """Serve the app under ``prefix`` however the proxy in front behaves.

    A proxy that forwards ``/parserIICS/upload`` untouched and one that strips
    the prefix and forwards ``/upload`` both end up here, and in either case
    generated links carry the prefix so they point back at the right place.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "")
            if path.startswith(self.prefix):
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.app(environ, start_response)


app = create_app()
