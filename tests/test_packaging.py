"""Guard the gap between "works from a checkout" and "works when installed".

Running from the source tree finds every file on disk. ``pip install`` copies
only the Python modules plus whatever ``package-data`` declares - so a template
or asset nobody listed is silently missing from the installed package, and the
failure surfaces as a 500 in a container rather than as a test failure here.

That has happened once: the web front end's ``index.html`` was not declared,
so the image built and started, answered its health check, and returned a
TemplateNotFound for every page. This test fails instead.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:                                     # pragma: no cover - 3.9/3.10 only
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "iics_parser"


def _declared_patterns() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        config = tomllib.load(fh)
    return config["tool"]["setuptools"]["package-data"]


def _is_declared(relative: Path, patterns: dict) -> bool:
    """True if some package's glob covers this file.

    ``package-data`` keys are package names and the globs are relative to that
    package, so a file matches when its path under the package matches a glob.
    """
    for package, globs in patterns.items():
        prefix = Path(*package.split(".")[1:])           # drop "iics_parser"
        try:
            within = relative.relative_to(prefix) if prefix.parts else relative
        except ValueError:
            continue
        if any(fnmatch.fnmatch(str(within), glob) for glob in globs):
            return True
    return False


def test_every_non_python_file_is_declared_as_package_data():
    patterns = _declared_patterns()
    missing = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*"))
        if path.is_file() and path.suffix != ".py"
        and "__pycache__" not in path.parts
        and not _is_declared(path.relative_to(SRC), patterns)
    ]
    assert not missing, (
        "these files ship in a checkout but not in an installed package - "
        f"add them to [tool.setuptools.package-data]: {missing}"
    )


def test_the_web_template_is_where_flask_will_look_for_it():
    """The specific file whose absence took the container down."""
    assert (SRC / "web" / "templates" / "index.html").is_file()


def test_every_package_with_data_is_a_real_package():
    """A package-data key that names nothing is a typo that silently does nothing."""
    for package in _declared_patterns():
        directory = SRC.joinpath(*package.split(".")[1:])
        assert (directory / "__init__.py").is_file(), (
            f"package-data names '{package}', which is not an importable package"
        )
