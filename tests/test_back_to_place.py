"""'Back to all' returns to where the examiner was, not the top of page 1.

A search (find similar, find matching faces) or a group view replaces the gallery.
The frontend remembers the page, the focused file and the scroll position when it
leaves, and restores them when it comes back. These pin the wiring in app.js; the
behavior itself was checked in a browser against a paged case.
"""

import re
from pathlib import Path

APPJS = Path(__file__).resolve().parents[1] / "gleapp/web/static/app.js"


def _body(js, header):
    start = js.index(header)
    return js[start:js.index("\n}", start)]


def test_every_entry_point_remembers_the_place_before_leaving():
    js = APPJS.read_text(encoding="utf-8")
    assert "function rememberPlace" in js and "function backToPlace" in js
    # the two searches
    assert "rememberPlace(id);" in _body(js, "async function showSimilar")
    assert "rememberPlace();" in _body(js, "async function showFaceMatches")
    # the group views, from the details pane and from the context menu
    assert len(re.findall(r"rememberPlace\(f\.id\);\s*state\.(?:vstack|stack) = ", js)) == 2
    assert len(re.findall(r"rememberPlace\(f0\.id\);\s*state\.(?:vstack|stack) = ", js)) == 2


def test_only_the_first_hop_is_remembered():
    js = APPJS.read_text(encoding="utf-8")
    body = _body(js, "function rememberPlace")
    # already inside a search or group: a second search keeps the original spot
    assert "if (state.similarOf || state.vstack || state.stack) return;" in body
    for key in ("page: state.page", "top:", "left:"):
        assert key in body


def test_back_restores_and_a_fresh_filter_forgets():
    js = APPJS.read_text(encoding="utf-8")
    assert '$("#simBack").onclick = backToPlace;' in js
    back = _body(js, "function backToPlace")
    assert "state.page = b.page" in back and "load(b ? { place: b } : {})" in back
    # load() puts the scroll and the cursor back when it is handed a place
    load = _body(js, "async function load")
    assert "opts.place" in load and "setFocus(opts.place.focus)" in load
    # a filter change starts over, so a stale place is never restored
    assert "state.back = null" in _body(js, "function reload")
    # dropping the group chip returns too, rather than resetting to page 1
    assert "return backToPlace();" in js
