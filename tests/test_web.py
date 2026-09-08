"""The web front end: upload a package, get the documents back.

These exercise the app the way a browser does - a real multipart upload of the
reference package, then following the download links it hands back.
"""

from __future__ import annotations

import importlib
import io
import zipfile
from pathlib import Path

import pytest

SAMPLE = (Path(__file__).resolve().parent.parent / "samples"
          / "tf_HUB_CONCUR_EMPLOYEE_DETAILS_OUTBOUND_FIN_I_HR001.zip")

pytest.importorskip("flask")
pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample package not present")

PREFIX = "/parserIICS"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A test client whose jobs land in a throwaway directory."""
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("URL_PREFIX", PREFIX)
    from iics_parser.web import app as web
    importlib.reload(web)                 # env is read at import time
    web.app.config.update(TESTING=True)
    return web.app.test_client()


def _upload(client, name="export.zip", data=None, **extra):
    payload = {"package": (io.BytesIO(data if data is not None else SAMPLE.read_bytes()), name)}
    payload.update(extra)
    return client.post(f"{PREFIX}/", data=payload, content_type="multipart/form-data")


def test_the_form_is_served_under_the_prefix(client):
    page = client.get(f"{PREFIX}/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Export package" in body
    # Links must carry the prefix or they point outside the app.
    assert f'action="{PREFIX}/"' in body or f'"{PREFIX}' in body


def test_health_check_answers(client):
    assert client.get(f"{PREFIX}/health").get_json()["status"] == "ok"


def test_upload_produces_both_documents(client):
    page = _upload(client)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "_Analysis.xlsx" in body
    assert "_Analysis.docx" in body
    assert "100% coverage" in body


def test_generated_files_download(client):
    import re
    body = _upload(client).get_data(as_text=True)
    links = re.findall(r'href="([^"]*/download/[^"]+)"', body)
    assert len(links) == 2, body[:400]

    for link in links:
        got = client.get(link)
        assert got.status_code == 200
        assert len(got.data) > 5000
        assert got.headers["Content-Disposition"].startswith("attachment")


def test_everything_downloads_as_one_zip(client):
    import re
    body = _upload(client).get_data(as_text=True)
    bundle = re.search(r'href="([^"]*/bundle/[^"]+\.zip)"', body).group(1)

    got = client.get(bundle)
    assert got.status_code == 200
    with zipfile.ZipFile(io.BytesIO(got.data)) as zf:
        names = zf.namelist()
    assert any(n.endswith(".xlsx") for n in names)
    assert any(n.endswith(".docx") for n in names)


def test_a_bad_upload_is_reported_not_crashed(client):
    """A wrong file is a message to the user, never a stack trace."""
    page = _upload(client, name="notes.txt", data=b"this is not a zip")
    assert page.status_code == 200
    assert "Not a .zip file" in page.get_data(as_text=True)

    page = _upload(client, name="broken.zip", data=b"PK\x03\x04 truncated")
    assert "not a readable ZIP archive" in page.get_data(as_text=True)


def test_uploading_nothing_asks_for_a_file(client):
    page = client.post(f"{PREFIX}/", data={}, content_type="multipart/form-data")
    assert page.status_code == 400
    assert "Choose a .zip export package" in page.get_data(as_text=True)


def test_download_path_cannot_escape_the_job_directory(client):
    """Job ids come from the URL, so they must not be able to address the disk."""
    assert client.get(f"{PREFIX}/download/..%2f..%2fetc/passwd").status_code == 404
    assert client.get(f"{PREFIX}/download/nosuchjob/x.xlsx").status_code == 404


def test_a_proxy_that_strips_the_prefix_still_works(client):
    """Some reverse proxies forward the path already stripped."""
    page = client.get("/")
    assert page.status_code == 200
    # Links are still generated with the prefix, so they route back correctly.
    assert PREFIX in page.get_data(as_text=True)
