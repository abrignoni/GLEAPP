"""The third-party notices the build has to carry.

The licences here mostly ask for their notice to travel with a binary copy, so the
failure worth catching is a notices file that quietly drops something. Two shapes
get their own test: a package that ships no licence text at all, which must be
described rather than skipped, and a package nothing is known about, which must stop
the build rather than produce a notice with a hole in it.
"""

from __future__ import annotations

import sys
from email.message import Message
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import make_notices  # noqa: E402  pylint: disable=wrong-import-position

ROOT = Path(__file__).resolve().parent.parent


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
    for label in ("qnxprobe", "ewfprobe", "mediacarve", "SFace"):
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
