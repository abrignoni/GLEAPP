"""c2pa-python is an optional extra, and no build of GLEAPP carries it.

Reading Content Credentials needs c2pa-python, whose native library is compiled from
c2pa-rs and its Rust dependencies. Upstream publishes no lockfile or third-party
notice for that library (checked for c2pa-python 0.38.0 / c2pa-rs 0.91.0 on
2026-10-06), so a frozen build could not carry a notice that is provably complete.
It is therefore an extra (``pip install -e ".[provenance]"``), not a requirement,
and a build that happens to have it installed must not pick it up half-way. The
measurements are in "C2PA reading is optional" in .claude/rules/gleapp-cross-platform.md.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def test_c2pa_python_is_an_extra_pinned_below_the_next_minor():
    """0.92.0 removes the trust settings 0.91 deprecated (trust.trust_anchors and
    trust.user_anchors), and the reader's JSON and state rules have changed between
    minors, so an upgrade is a decision, not something pip makes."""
    extra = re.search(r'^provenance = \[(.*)\]$', _text("pyproject.toml"), re.M)
    assert extra, "pyproject.toml has no [provenance] extra"
    assert extra.group(1) == '"c2pa-python>=0.38,<0.39"'


def test_the_requirements_do_not_carry_it():
    assert "c2pa" not in _text("requirements.txt").lower()


def test_the_frozen_build_excludes_it():
    """PyInstaller follows an import even inside a function, so a build environment
    with the extra installed would collect the c2pa package without the native
    library it loads through ctypes, and its notice would be empty."""
    spec = _text("packaging", "gleapp.spec")
    excludes = re.search(r"excludes=\[(.*?)\],\n", spec, re.S).group(1)
    assert '"c2pa"' in excludes


def test_the_code_never_names_the_distribution_to_importlib_metadata():
    """PyInstaller copies the dist-info of a distribution the code names to
    importlib.metadata, whether or not the package is excluded: a build beside the
    extra carried c2pa_python-0.38.0.dist-info, a RECORD of files it did not hold
    (PyInstaller 6.22.2). The version is read from the package instead."""
    code = _text("gleapp", "provenance.py")
    assert not re.search(r"^\s*(from importlib import metadata|import importlib\.metadata"
                         r"|from importlib\.metadata import)", code, re.M)
    assert not re.search(r"\bmetadata\.(version|distribution|metadata|files|requires)\(", code)


def test_ci_runs_the_suite_with_the_library_and_without_it():
    """The Linux and Windows jobs install the extra, so the signed-fixture tests run;
    the runtime-contract matrix (3.10 to 3.14) does not, so the 'not installed'
    path runs as an examiner without it would meet it."""
    workflows = ROOT / ".github" / "workflows"
    for name in ("test_cases.yml", "windows_smoke.yml"):
        assert '".[dev,provenance]"' in (workflows / name).read_text(encoding="utf-8"), name
    assert "provenance" not in (workflows / "python_runtime_contract.yml").read_text(
        encoding="utf-8")
