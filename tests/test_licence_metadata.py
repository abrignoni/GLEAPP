"""The licence the wheel declares, and the licence texts behind it.

pyproject.toml's ``license`` is an SPDX expression, and the core metadata spec says
it applies to the distribution archive that carries it, so it has to cover what the
wheel bundles as well as GLEAPP's own code. ``license-files`` names the texts, which
setuptools copies into the wheel's .dist-info and lists as License-File. Both are
written by hand, so a vendored file or a model added later with a licence of its own
would leave them short, and the build would say nothing. These tests tie both
fields, the vendored-file manifest and the notices to the licence texts in the tree.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10, where pytest itself depends on tomli
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import make_notices  # noqa: E402  pylint: disable=wrong-import-position

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

# A phrase from each licence's own text. They are specific on purpose: the OFL grants
# its permission in the same opening words as MIT, so MIT is recognised by its notice
# condition instead, and BSD-3-Clause by the clause that makes it three.
MARKERS = {
    "MIT": ("The above copyright notice and this permission notice shall be included "
            "in all copies or substantial portions of the Software"),
    "Apache-1.1": "The Apache Software License, Version 1.1",
    "Apache-2.0": "Apache License Version 2.0, January 2004",
    "BSD-3-Clause": ("may be used to endorse or promote products derived from this "
                     "software without specific prior written permission"),
    "CC0-1.0": "Creative Commons 0 (CC0)",
    "OFL-1.1": "SIL Open Font License, Version 1.1",
}

# Every licence text in the tree and what it grants. A new licence file fails the
# first test until it is read and added here.
LICENCES = {
    "LICENSE": {"MIT"},
    "gleapp/vendor/LICENSE-qnxprobe": {"MIT"},
    "gleapp/vendor/LICENSE-ewfprobe": {"MIT"},
    "gleapp/vendor/LICENSE-mediacarve": {"MIT"},
    # Apache 1.1 with Impacket's names in it; Fedora's python-impacket package labels
    # the same text Apache-1.1.
    "gleapp/vendor/LICENSE-impacket": {"Apache-1.1"},
    "gleapp/models/LICENSE-sface": {"Apache-2.0"},
    "gleapp/models/LICENSE-yunet": {"MIT"},
    # MapLibre and the mapbox-gl-js and d3-color code in it, plus glfx.js under MIT
    "gleapp/web/static/maps/LICENSE-maplibre-gl.txt": {"BSD-3-Clause", "MIT"},
    "gleapp/web/static/maps/LICENSE-pmtiles.txt": {"BSD-3-Clause"},
    # the style code, the style design, and the icons the sprites derive from
    "gleapp/web/static/maps/LICENSE-protomaps-basemaps.txt": {"BSD-3-Clause", "CC0-1.0",
                                                              "MIT"},
    "gleapp/web/static/maps/fonts/OFL.txt": {"OFL-1.1"},
}

# Licences a text grants to files GLEAPP does not ship. Impacket's LICENSE has
# sections for other parts of Impacket; the one a marker finds is the MIT section for
# examples/kintercept.py, and gleapp/vendor/impacket_ese.py holds only ese.py and
# structure.py.
NOT_SHIPPED = {
    "gleapp/vendor/LICENSE-impacket": {"MIT"},
}

# Written by tools/make_notices.py when someone runs it for the /notices page;
# git-ignored, never shipped from the source tree.
GENERATED = {"gleapp/web/static/NOTICES.txt"}


def _licence_texts_in_tree() -> set[str]:
    found = {"LICENSE"}
    for path in (ROOT / "gleapp").rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if (path.is_file() and "__pycache__" not in path.parts and rel not in GENERATED
                and re.match(r"(LICEN[CS]E|COPYING|NOTICE|OFL)", path.name, re.IGNORECASE)):
            found.add(rel)
    return found


def _flat(rel: str) -> str:
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split())


def test_every_licence_text_in_the_tree_is_classified_from_its_own_words():
    assert _licence_texts_in_tree() == set(LICENCES)
    problems = []
    for rel, claimed in sorted(LICENCES.items()):
        text = _flat(rel)
        found = {spdx for spdx, marker in MARKERS.items() if marker in text}
        for spdx in sorted(claimed - found):
            problems.append(f"{rel}: listed as {spdx}, but its text does not say so")
        for spdx in sorted(found - claimed - NOT_SHIPPED.get(rel, set())):
            problems.append(f"{rel}: its text grants {spdx}, which LICENCES leaves out")
    assert not problems, "\n".join(problems)


def test_license_files_names_exactly_the_licence_texts():
    named = set()
    for pattern in PYPROJECT["project"]["license-files"]:
        hits = {p.relative_to(ROOT).as_posix() for p in ROOT.glob(pattern) if p.is_file()}
        assert hits, f"license-files pattern {pattern!r} matches nothing"
        named |= hits
    assert named == set(LICENCES)


def test_the_expression_names_every_licence_those_texts_grant():
    expression = PYPROJECT["project"]["license"]
    assert isinstance(expression, str), "license must be an SPDX string, not a table"
    terms = expression.split(" AND ")
    # a plain conjunction is all this reads; OR, WITH or brackets need a real parser
    assert all(re.fullmatch(r"[A-Za-z0-9.+-]+", term) for term in terms), expression
    assert len(terms) == len(set(terms)), f"an identifier repeats in {expression}"
    assert set(terms) == set().union(*LICENCES.values())


def test_the_vendored_manifest_names_the_licence_its_file_carries():
    manifest = json.loads((ROOT / "gleapp" / "vendor" / "vendored.json")
                          .read_text(encoding="utf-8"))
    for entry in manifest["vendored"]:
        rel = entry["licence_file"]
        assert rel in LICENCES, f"{entry['name']}: {rel} is not a known licence text"
        assert set(entry["licence"].split(" AND ")) == LICENCES[rel], (
            f"{entry['name']}: vendored.json says {entry['licence']}, "
            f"{rel} grants {' AND '.join(sorted(LICENCES[rel]))}")


def test_every_licence_text_reaches_the_notices(monkeypatch):
    monkeypatch.setattr(make_notices.md, "distributions", lambda: [])
    text, _unknown = make_notices.build_notices(only=set(), root=ROOT)
    assert {rel for _label, rel in make_notices.REPO_NOTICES} == set(LICENCES)
    for rel in sorted(LICENCES):
        body = (ROOT / rel).read_text(encoding="utf-8").strip()
        assert body in text, f"the text of {rel} is not in the built notices"


def test_the_notices_skip_the_distribution_pyproject_builds():
    assert make_notices.OWN_DISTRIBUTION == PYPROJECT["project"]["name"]
