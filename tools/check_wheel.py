#!/usr/bin/env python3
"""Build the wheel and confirm it carries every tracked file under gleapp/.

The wheel takes its data files from [tool.setuptools.package-data] in pyproject.toml,
and a pattern that misses a file fails open: the build succeeds and the file is simply
not in it. The CI jobs install the package editable, which reads the checkout, and
packaging/gleapp.spec copies its data folders whole, so neither would notice.

    python3 tools/check_wheel.py

The wheel is built from ``git archive HEAD`` in a temporary folder, so it holds what is
committed and nothing else a working tree carries, and the checkout is left alone. pip
builds it in an isolated environment, fetching the setuptools that pyproject.toml's
build-system asks for from the package index.

A tracked file missing from the wheel, or a file in the wheel that is not tracked under
gleapp/, exits 1. A wheel that could not be built or read exits 2. That second result
says nothing was compared, which is different from a finding about pyproject.toml.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "gleapp"


class CouldNotCheck(Exception):
    """The wheel could not be built or read, so nothing was compared."""


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, check=False)
    except OSError as exc:
        raise CouldNotCheck(f"could not run {cmd[0]}: {exc}") from exc


def _text(raw):
    return raw.decode("utf-8", "replace")


def tracked_files():
    """Every file under gleapp/ in HEAD. Read NUL-delimited, because the glyph
    folders under web/static/maps have spaces in their names."""
    res = _run(["git", "-C", REPO, "ls-tree", "-r", "-z", "--name-only", "HEAD",
                "--", PACKAGE])
    if res.returncode != 0:
        raise CouldNotCheck("git ls-tree failed:\n" + _text(res.stderr))
    files = {p for p in _text(res.stdout).split("\0") if p}
    if not files:
        raise CouldNotCheck(f"HEAD has no files under {PACKAGE}/")
    return files


def build_wheel(workdir):
    """Build the wheel from HEAD inside workdir and return its path."""
    archive = os.path.join(workdir, "head.zip")
    src = os.path.join(workdir, "src")
    dist = os.path.join(workdir, "dist")
    res = _run(["git", "-C", REPO, "archive", "--format=zip", "-o", archive, "HEAD"])
    if res.returncode != 0:
        raise CouldNotCheck("git archive failed:\n" + _text(res.stderr))
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(src)
    res = _run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                "--disable-pip-version-check", "-w", dist, src])
    if res.returncode != 0:
        tail = "\n".join(_text(res.stdout + res.stderr).splitlines()[-30:])
        raise CouldNotCheck(f"pip could not build the wheel (exit {res.returncode}):\n{tail}")
    wheels = sorted(f for f in os.listdir(dist) if f.endswith(".whl"))
    if len(wheels) != 1:
        raise CouldNotCheck(f"expected one wheel in {dist}, found {wheels}")
    return os.path.join(dist, wheels[0])


def wheel_contents(wheel):
    """The files the wheel installs outside its .dist-info, and what wrote it."""
    try:
        with zipfile.ZipFile(wheel) as whl:
            names = {n for n in whl.namelist() if not n.endswith("/")}
            meta = {n for n in names if n.split("/", 1)[0].endswith(".dist-info")}
            generator = "an unrecorded tool"
            for name in meta:
                if name.endswith(".dist-info/WHEEL"):
                    found = re.search(r"^Generator: (.+)$", _text(whl.read(name)), re.M)
                    if found:
                        generator = found.group(1).strip()
    except (OSError, zipfile.BadZipFile) as exc:
        raise CouldNotCheck(f"could not read {wheel}: {exc}") from exc
    return names - meta, generator


def _report(title, paths):
    folders = defaultdict(list)
    for path in sorted(paths):
        folders[posixpath.dirname(path)].append(posixpath.basename(path))
    print(f"\n{title}\n")
    for folder, names in sorted(folders.items()):
        shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        print(f"  {folder}/  {len(names)}: {shown}")


def main():
    print(f"Building the wheel from HEAD of {REPO} ...")
    try:
        tracked = tracked_files()
        with tempfile.TemporaryDirectory(prefix="gleapp-wheel-",
                                         ignore_cleanup_errors=True) as work:
            wheel = build_wheel(work)
            carried, generator = wheel_contents(wheel)
            label = (f"{os.path.basename(wheel)} "
                     f"({os.path.getsize(wheel) / 1e6:.1f} MB, written by {generator})")
    except CouldNotCheck as exc:
        print(f"\nCould not check the wheel: {exc}")
        return 2

    missing = tracked - carried
    unexpected = carried - tracked
    if missing:
        _report(f"Tracked files under {PACKAGE}/ that {label} does not carry "
                f"({len(missing)}):", missing)
        print("\nEach needs a pattern in [tool.setuptools.package-data] in pyproject.toml.")
    if unexpected:
        _report(f"Files in {label} that are not tracked under {PACKAGE}/ "
                f"({len(unexpected)}):", unexpected)
    if missing or unexpected:
        return 1

    data = sum(1 for p in tracked if not p.endswith(".py"))
    print(f"{label} carries all {len(tracked)} tracked files under {PACKAGE}/, "
          f"{data} of them data files, and nothing else.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
