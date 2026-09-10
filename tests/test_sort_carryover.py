"""Switching between grid and list keeps the sort, for the sorts both can express.

The grid has a Sort dropdown (path / date / size / skin / faces / cluster); the
list sorts by clicking column headers. They drive the same server query, so
switching view should not silently re-sort the results.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image

from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

APPJS = Path(__file__).resolve().parents[1] / "gleapp/web/static/app.js"


def test_both_views_query_by_the_shared_sort_state():
    js = APPJS.read_text(encoding="utf-8")
    # both views send state.sortCol / state.sortDir - not $("#fsort").value
    fp = js[js.index("function filterParams"):js.index("function filterParams") + 1400]
    assert 'p.set("sort", state.sortCol' in fp and 'p.set("dir", state.sortDir' in fp
    assert '$("#fsort").value' not in fp        # the grid no longer sorts by the raw dropdown value
    # the dropdown maps both ways
    assert "GRID_SORT_TO_LIST" in js and "LIST_SORT_TO_GRID" in js
    assert "reflectGridSort" in js
    # every value the grid Sort dropdown offers has a list-column target
    tpl = (Path(__file__).resolve().parents[1]
           / "gleapp/web/templates/index.html").read_text(encoding="utf-8")
    dd = tpl[tpl.index('<select id="fsort">'):]
    dd = dd[:dd.index("</select>")]
    grid_vals = set(re.findall(r'<option value="(\w+)"', dd))
    mapped = set(re.findall(r"(\w+): \[\"", js.split("GRID_SORT_TO_LIST")[1].split("}")[0]))
    assert grid_vals and grid_vals <= mapped


def test_the_server_orders_by_a_grid_and_a_list_sort_key_alike(tmp_path):
    """Whichever vocabulary the frontend sends, /api/files sorts by size."""
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel

    ev = tmp_path / "ev"
    ev.mkdir()
    for name, wh in [("big", (400, 300)), ("small", (40, 30)), ("mid", (120, 90))]:
        Image.new("RGB", wh, (10, 20, 30)).save(ev / f"{name}.png")
    case = open_case(tmp_path / "c", create=True, examiner="t")
    ingest_sources(case, [Source(kind="folder", path=str(ev), name="ev")])
    process(case, workers=1, keyframes=0, screen=False)
    case.close()

    cl = create_app(None).test_client()
    cl.post("/api/case/open", json={"path": str(tmp_path / "c")})

    def names(qs):
        return [Path(r["rel_path"]).stem
                for r in cl.get("/api/files" + qs).get_json()["files"]]

    assert names("?sort=size&dir=asc") == ["small", "mid", "big"]
    assert names("?sort=size&dir=desc") == ["big", "mid", "small"]
    # the legacy grid vocabulary still works as a fallback
    assert names("?sort=size") == ["small", "mid", "big"]
