"""The version is written in two files, so something has to keep them equal.

gleapp/__init__.py holds the version the app reports and the release workflow checks the
tag against; pyproject.toml holds the version the wheel is built with. setuptools cannot
read the second from the first with a dynamic ``attr``: resolving ``gleapp`` imports the
root launcher gleapp.py instead of the package, the same shadowing that makes pylint
report false import errors for every test here, and the build fails outright. So the two
are written out and compared.

Both are read as text, never imported, which is what packaging/build.py does with the same
literal and what keeps this file free of that shadowing.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# vYYYY.N.P, the shape the LEAPP family tags, where N is a release series rather than a
# month. The optional suffix leaves room for a pre-release such as 2026.5.0-dev.
VERSION_SHAPE = re.compile(r"^\d{4}\.\d+\.\d+(-[0-9A-Za-z.]+)?$")


def package_version() -> str:
    """__version__ from gleapp/__init__.py, read the way packaging/build.py reads it."""
    text = (REPO / "gleapp" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    assert found, "no __version__ in gleapp/__init__.py"
    return found.group(1)


def pyproject_version() -> str:
    """The [project] version, read without tomllib, which Python 3.10 does not have."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(r"^\[project\]$(.*?)^\[", text, re.M | re.S)
    assert section, "no [project] section in pyproject.toml"
    found = re.search(r'^version = "([^"]+)"', section.group(1), re.M)
    assert found, "no literal version in [project]; a dynamic version needs this test rewritten"
    return found.group(1)


def test_pyproject_and_package_agree_on_the_version():
    assert pyproject_version() == package_version()


def test_the_version_has_the_shape_the_release_tag_expects():
    # packaging/build.py reads this literal and .github/workflows/release.yml requires the
    # tag to equal "v" + that value, so a version off this shape cannot be released.
    assert VERSION_SHAPE.match(package_version()), package_version()
