"""The third-party notices the build has to carry.

The licences here mostly ask for their notice to travel with a binary copy, so the
failure worth catching is a notices file that quietly drops something. Three shapes
get their own test: a package that ships no licence text at all, which must be
described rather than skipped, a package nothing is known about, which must stop
the build rather than produce a notice with a hole in it, and a model the build
copies out of gleapp/models with no licence entry, which is how YuNet shipped.
"""

from __future__ import annotations

import hashlib
import sys
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import make_notices  # noqa: E402  pylint: disable=wrong-import-position

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "gleapp" / "models"


def _metadata(name: str, **fields) -> Message:
    m = Message()
    m["Name"] = name
    for key, value in fields.items():
        m[key.replace("_", "-")] = value
    return m


class _FakeDist:
    """Enough of importlib.metadata.Distribution for the collector."""

    def __init__(self, name, version="1.0", files=(), base=None, **fields):
        self.metadata = _metadata(name, **fields)
        self.version = version
        self._files = list(files)
        self._base = base

    @property
    def files(self):
        return self._files

    def locate_file(self, path):
        return (self._base / str(path)) if self._base else Path(str(path))


@pytest.fixture(name="only_fakes")
def _only_fakes(monkeypatch):
    """Replace the installed set, so a test describes exactly what it built."""
    def use(dists):
        monkeypatch.setattr(make_notices.md, "distributions", lambda: list(dists))
    return use


def test_the_notices_open_with_gleapps_own_licence(only_fakes):
    only_fakes([])
    text, unknown = make_notices.build_notices(only=set(), root=ROOT)
    assert unknown == []
    assert "MIT License" in text
    assert "charpy4n6" in text
    # the vendored readers travel too, and Impacket's Apache 1.1 asks by name
    assert "SecureAuth Corporation" in text
    for label in ("qnxprobe", "ewfprobe", "mediacarve", "SFace", "YuNet"):
        assert label in text, f"{label} notice missing"


def test_a_package_that_ships_no_licence_text_is_described_not_dropped(only_fakes):
    only_fakes([_FakeDist("proxy_tools", "0.1.0", License="MIT")])
    text, unknown = make_notices.build_notices(only={"proxy-tools"}, root=ROOT)
    assert unknown == []
    assert "proxy_tools 0.1.0" in text
    assert "Declared licence: MIT" in text
    assert "ships no licence text of its own" in text


def test_a_package_nothing_is_known_about_is_reported_as_a_gap(only_fakes):
    # the control: the same package with a declaration is not a gap
    only_fakes([_FakeDist("mystery", "2.0", License="BSD-3-Clause")])
    _text, known = make_notices.build_notices(only={"mystery"}, root=ROOT)
    assert known == []

    only_fakes([_FakeDist("mystery", "2.0")])
    text, unknown = make_notices.build_notices(only={"mystery"}, root=ROOT)
    assert unknown == ["mystery"], "a package with no text and no declaration must be a gap"
    assert "not stated by the package" in text


def test_a_licence_text_beside_the_code_is_collected_too(tmp_path, only_fakes):
    """OpenCV keeps its 179 KB third-party notice next to its code, not in dist-info.

    Searching only dist-info would drop the notice covering every library OpenCV
    bundles, which is the largest single block in the file.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "LICENSE-3RD-PARTY.txt").write_text("ffmpeg and friends",
                                                           encoding="utf-8")
    dist = _FakeDist("demo-pkg", "3.0", files=["pkg/LICENSE-3RD-PARTY.txt"],
                     base=tmp_path, License="Apache-2.0")
    only_fakes([dist])
    text, unknown = make_notices.build_notices(only={"demo-pkg"}, root=ROOT)
    assert unknown == []
    assert "ffmpeg and friends" in text
    assert "LICENSE-3RD-PARTY.txt" in text


def test_build_only_packages_are_not_credited(only_fakes):
    """pillow-heif is installed to write a test fixture and never shipped."""
    only_fakes([_FakeDist("pillow-heif", "1.7.0", License="BSD-3-Clause"),
                _FakeDist("pytest", "9.0", License="MIT")])
    entries = make_notices.installed_entries()
    assert entries == [], "build-only packages must not appear in the notices"


def test_gleapps_own_distribution_is_not_described_a_second_time(tmp_path, only_fakes):
    """REPO_NOTICES already carries GLEAPP's LICENSE and every text its wheel ships.

    Every CI job and the frozen build install GLEAPP editable, so its own
    distribution is among the installed ones, and its dist-info holds the texts
    pyproject.toml's license-files names. Described as a package it printed GLEAPP's
    LICENSE a second time, and with the vendored and map texts in license-files it
    would have repeated each of those too.
    """
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    dist_licence = tmp_path / "gleapp_forensics-0.1.0.dist-info" / "licenses" / "LICENSE"
    dist_licence.parent.mkdir(parents=True)
    dist_licence.write_text(licence, encoding="utf-8")
    own = _FakeDist(make_notices.OWN_DISTRIBUTION, "0.1.0",
                    files=["gleapp_forensics-0.1.0.dist-info/licenses/LICENSE"],
                    base=tmp_path, License_Expression="MIT AND Apache-1.1")
    only_fakes([own])
    assert make_notices.installed_entries() == []
    text, unknown = make_notices.build_notices(only={make_notices.OWN_DISTRIBUTION}, root=ROOT)
    assert unknown == []
    assert f"{make_notices.OWN_DISTRIBUTION} 0.1.0" not in text
    assert text.count(licence.strip()) == 1


def test_the_webview2_terms_travel_with_the_binaries_that_need_them(only_fakes):
    """pywebview ships Microsoft's WebView2 assemblies and none of their terms.

    Those DLLs are byte-identical to the NuGet package Microsoft.Web.WebView2
    1.0.3856.49, whose licence is BSD-3-Clause, and its second clause asks for the
    notice in a binary distribution. pywebview carries only its own BSD licence, so
    the text has to come from this repo or it does not ship at all.
    """
    only_fakes([_FakeDist("pywebview", "6.2.1", License="BSD 3-Clause License")])
    text, unknown = make_notices.build_notices(only={"pywebview"}, root=ROOT)
    assert unknown == []
    assert "Microsoft.Web.WebView2.Core.dll" in text
    assert "1.0.3856.49" in text
    # the licence text itself, not merely a mention of it
    assert "Copyright (C) Microsoft Corporation. All rights reserved." in text
    assert "Redistributions in binary form must reproduce the above" in text
    # the Runtime is a different thing and is not bundled; say so rather than imply it
    assert "NOT bundled" in text


def test_the_webview2_licence_text_is_the_one_microsoft_ships():
    """Kept verbatim: an edited licence is not the licence."""
    text = (ROOT / "packaging" / "licenses" / "LICENSE-webview2.txt").read_text(encoding="utf-8")
    assert text.startswith("Copyright (C) Microsoft Corporation. All rights reserved.")
    for clause in ("Redistributions of source code must retain",
                   "Redistributions in binary form must reproduce",
                   "may not be used to endorse or promote products"):
        assert clause in text, f"missing BSD-3-Clause term: {clause}"
    assert "\r" not in text, "must be LF, like every tracked text file"


def _looks_like_licence(name: str) -> bool:
    # the file names make_notices.licence_texts() treats as licence texts
    return name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))


def test_every_bundled_model_carries_its_licence_into_the_notices(only_fakes):
    """packaging/gleapp.spec copies all of gleapp/models into every build.

    The YuNet model travelled that way with no licence text anywhere in the build,
    beside the SFace model, which had one. So each model file needs a REPO_NOTICES
    entry whose label names it, and the text of that entry's file has to reach the
    built notices. build_notices() skips an entry whose file is missing without a
    word, which is why this reads the notices and not only the list.
    """
    only_fakes([])
    text, _unknown = make_notices.build_notices(only=set(), root=ROOT)
    # dotfiles are the desktop's (.DS_Store), not models
    files = sorted(p for p in MODELS.iterdir() if p.is_file() and not p.name.startswith("."))
    models = [p for p in files if not _looks_like_licence(p.name)]
    assert models, "no model found in gleapp/models, so nothing was checked"

    listed = {rel for _label, rel in make_notices.REPO_NOTICES}
    problems = []
    for lic in (p.relative_to(ROOT).as_posix() for p in files if _looks_like_licence(p.name)):
        if lic not in listed:
            problems.append(f"{lic} is not listed in make_notices.REPO_NOTICES")
    for model in models:
        rel = model.relative_to(ROOT).as_posix()
        owners = [lic for label, lic in make_notices.REPO_NOTICES if f"({rel})" in label]
        if not owners:
            problems.append(f"{rel} has no licence entry in tools/make_notices.py")
        for lic in owners:
            path = ROOT / lic
            body = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            if not body:
                problems.append(f"{rel}: its licence file {lic} is missing or empty")
            elif body not in text:
                problems.append(f"{rel}: the text of {lic} is not in the built notices")
    assert not problems, "\n".join(problems)


# The licence files OpenCV Zoo keeps beside each model, models/face_detection_yunet/
# LICENSE and models/face_recognition_sface/LICENSE in github.com/opencv/opencv_zoo,
# copied byte for byte and compared at commit 47534e27. YuNet's has no final newline
# upstream, so an editor that adds one has edited the licence.
UPSTREAM_MODEL_LICENCES = {
    "LICENSE-yunet": (1085, "c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958",
                      "Copyright (c) 2020 Shiqi Yu"),
    "LICENSE-sface": (11358, "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
                      "Version 2.0, January 2004"),
}


@pytest.mark.parametrize("name", sorted(UPSTREAM_MODEL_LICENCES))
def test_the_model_licences_are_opencv_zoos_text_unchanged(name):
    """Kept verbatim: an edited licence is not the licence."""
    size, sha256, line = UPSTREAM_MODEL_LICENCES[name]
    path = MODELS / name
    assert path.is_file(), f"{name} is missing from gleapp/models"
    data = path.read_bytes()
    assert line.encode("ascii") in data, f"{name} does not carry {line!r}"
    assert len(data) == size, f"{name} is {len(data)} bytes, the upstream file {size}"
    assert hashlib.sha256(data).hexdigest() == sha256, f"{name} differs from the upstream bytes"


def test_the_notices_route_says_what_to_do_when_no_file_is_built():
    """A source checkout has no notices file, and that is not a missing feature."""
    from gleapp.web.app import create_app  # pylint: disable=import-outside-toplevel
    client = create_app(None).test_client()
    resp = client.get("/notices")
    if resp.status_code == 200:                 # a build, or someone ran the tool
        assert b"THIRD-PARTY NOTICES" in resp.data
        return
    assert resp.status_code == 404
    assert b"tools/make_notices.py" in resp.data
