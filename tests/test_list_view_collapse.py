"""The details list view always shows every row; the grid may collapse duplicates.

An examiner triaging by metadata needs to see every file, including exact and
visual duplicates - a photo that turns up in two places is two rows. Collapsing
is a grid-only convenience.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

APPJS = Path(__file__).resolve().parents[1] / "gleapp/web/static/app.js"


def _jpg(colour) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (48, 36), colour).save(b, "JPEG")
    return b.getvalue()


def _case_with_a_duplicate(tmp_path):
    ev = tmp_path / "ev"
    (ev / "a").mkdir(parents=True)
    (ev / "b").mkdir(parents=True)
    same = _jpg((70, 90, 110))
    (ev / "a" / "photo.jpg").write_bytes(same)
    (ev / "b" / "photo.jpg").write_bytes(same)          # identical bytes, two paths
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    process(case, workers=1, keyframes=0, screen=False)
    case.close()
    return tmp_path / "case"


def test_the_grid_collapses_but_the_list_does_not(tmp_path):
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel

    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(_case_with_a_duplicate(tmp_path))})

    # grid, collapse on: the two identical photos are one row
    assert cl.get("/api/files?dupes=collapse").get_json()["total"] == 1
    # list view sends no dupes= param: both rows
    every = cl.get("/api/files").get_json()
    assert every["total"] == 2
    assert sorted(r["rel_path"].replace("\\", "/") for r in every["files"]) == \
        ["a/photo.jpg", "b/photo.jpg"]


def test_the_frontend_never_asks_the_list_view_to_collapse():
    js = APPJS.read_text(encoding="utf-8")
    # the collapse param is gated on not being in list view
    assert 'state.view !== "list"' in js
    # and the checkbox is disabled while the list is open
    assert "fc.disabled = list" in js
