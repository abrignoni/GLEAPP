"""Static location maps for the report: the MVT decoder, the PMTiles tile reader,
the renderer, and the maps embedded in the HTML report. Everything here is offline
and touches only local files."""

import base64
import io
import json
import re

import pytest
from PIL import Image

from gleapp import basemaps, mvt, staticmap

# The label tests reach into the module on purpose: where a label was placed, and
# with what font, have no public surface, and asserting on the finished image alone
# cannot tell a label at the middle of a road from one at its end.
# pylint: disable=protected-access
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "tiny.pmtiles"


# ---- a minimal MVT encoder, only for the tests ----------------------------

def _uv(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _uv((field << 3) | wire)


def _zz(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def _geom(gtype: int, rings) -> bytes:
    """Command stream for point/line/polygon rings given in tile units."""
    out = bytearray()
    cx = cy = 0
    for ring in rings:
        out += _uv((1 << 3) | 1)                    # MoveTo, count 1
        out += _uv(_zz(ring[0][0] - cx)) + _uv(_zz(ring[0][1] - cy))
        cx, cy = ring[0]
        rest = ring[1:]
        if rest:
            out += _uv((len(rest) << 3) | 2)        # LineTo count
            for x, y in rest:
                out += _uv(_zz(x - cx)) + _uv(_zz(y - cy))
                cx, cy = x, y
        if gtype == mvt.POLYGON:
            out += _uv((1 << 3) | 7)                # ClosePath
    return bytes(out)


def _feature(gtype: int, rings, tag_indices=()) -> bytes:
    g = _geom(gtype, rings)
    out = _tag(3, 0) + _uv(gtype)
    if tag_indices:
        packed = b"".join(_uv(i) for i in tag_indices)
        out += _tag(2, 2) + _uv(len(packed)) + packed
    return out + _tag(4, 2) + _uv(len(g)) + g


def _string_value(text: str) -> bytes:
    return _tag(1, 2) + _uv(len(text.encode())) + text.encode()


def _int_value(n: int) -> bytes:
    return _tag(5, 0) + _uv(n)                      # uint64


def _layer(name: str, gtype: int, rings, extent: int = 4096, attrs=None) -> bytes:
    """One layer holding one feature. ``attrs`` is a dict written into the layer's
    key and value tables and referenced by the feature, the way a real tile does."""
    body = _tag(15, 0) + _uv(2) + _tag(1, 2) + _uv(len(name)) + name.encode()
    body += _tag(5, 0) + _uv(extent)
    indices = []
    keys_values = b""
    for i, (key, value) in enumerate((attrs or {}).items()):
        keys_values += _tag(3, 2) + _uv(len(key.encode())) + key.encode()
        encoded = _string_value(value) if isinstance(value, str) else _int_value(value)
        keys_values += _tag(4, 2) + _uv(len(encoded)) + encoded
        indices += [i, i]
    feat = _feature(gtype, rings, indices)
    body += _tag(2, 2) + _uv(len(feat)) + feat + keys_values
    return body


def _tile(*layers: bytes) -> bytes:
    return b"".join(_tag(3, 2) + _uv(len(la)) + la for la in layers)


def _full_water(extent: int = 4096) -> bytes:
    ring = [(0, 0), (extent, 0), (extent, extent), (0, extent)]
    return _tile(_layer("water", mvt.POLYGON, [ring], extent))


# ---- MVT decoder ----------------------------------------------------------

def test_mvt_decode_roundtrips_geometry():
    ring = [(10, 10), (100, 10), (100, 100), (10, 100)]
    road = [(0, 2048), (4096, 2048)]
    raw = _tile(_layer("water", mvt.POLYGON, [ring]),
                _layer("roads", mvt.LINESTRING, [road]))
    layers = mvt.decode(raw)
    assert set(layers) == {"water", "roads"}
    assert layers["water"]["extent"] == 4096
    wf = layers["water"]["features"][0]
    assert wf["type"] == mvt.POLYGON and wf["paths"][0][:2] == [(10, 10), (100, 10)]
    rf = layers["roads"]["features"][0]
    assert rf["type"] == mvt.LINESTRING and rf["paths"][0] == [(0, 2048), (4096, 2048)]


def test_mvt_decode_survives_garbage():
    assert mvt.decode(b"\xff\xff\x00not a real tile") == {} or isinstance(
        mvt.decode(b"\xff\xff\x00not a real tile"), dict)


# ---- PMTiles tile reader (against the committed raster fixture) ------------

def test_pmtiles_tile_reads_from_the_directory():
    h = basemaps.read_pmtiles_header(FIXTURE)
    assert (h["min_zoom"], h["max_zoom"]) == (0, 1)
    z0 = basemaps.pmtiles_tile(FIXTURE, 0, 0, 0)
    assert z0 and z0[:8] == b"\x89PNG\r\n\x1a\n"          # the fixture stores PNG tiles
    # all four z1 tiles exist in the fixture
    for x in (0, 1):
        for y in (0, 1):
            assert basemaps.pmtiles_tile(FIXTURE, 1, x, y) is not None
    assert basemaps.pmtiles_tile(FIXTURE, 2, 0, 0) is None    # beyond max zoom
    assert basemaps.pmtiles_tile(FIXTURE, 1, 9, 9) is None    # out of range x/y


# ---- renderer -------------------------------------------------------------

def _reddish(im: Image.Image) -> int:
    return sum(c for c, (r, g, b) in (im.getcolors(1 << 20) or []) if r > 170 and g < 110 and b < 110)


def test_render_raster_produces_an_image_with_a_marker():
    rec = {"path": str(FIXTURE), "format": basemaps.FORMAT_PMTILES,
           "tile_type": "png", "min_zoom": 0, "max_zoom": 1}
    png = staticmap.render(rec, [(10.0, 20.0)], width=200, height=140, zoom=1)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert im.size == (200, 140)
    assert _reddish(im) > 0                                    # the marker is on it


def test_render_return_points_gives_the_markers_own_pixel():
    """The clickable overlay in the report needs to know exactly where a marker
    landed - not an approximation, the same pixel the marker itself was drawn at."""
    rec = {"path": str(FIXTURE), "format": basemaps.FORMAT_PMTILES,
           "tile_type": "png", "min_zoom": 0, "max_zoom": 1}
    png, px = staticmap.render(rec, [(10.0, 20.0), (-5.0, 30.0)], width=200, height=140,
                               zoom=1, return_points=True)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert len(px) == 2
    for x, y in px:
        assert 0 <= x <= 200 and 0 <= y <= 140
        x, y = round(x), round(y)
        # sample a small box around the reported point: the marker (a filled red
        # circle, radius 7) must actually be centred there, not merely nearby
        box = im.crop((max(0, x - 3), max(0, y - 3), x + 3, y + 3))
        assert _reddish(box) > 0


def test_render_vector_draws_the_style_and_a_marker(monkeypatch):
    tile = _full_water()
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda p, z, x, y: tile)
    rec = {"path": "x.pmtiles", "format": basemaps.FORMAT_PMTILES,
           "tile_type": "mvt", "min_zoom": 0, "max_zoom": 2}
    jpg = staticmap.render(rec, [(0.0, 0.0)], width=256, height=256, zoom=1, fmt="jpeg")
    im = Image.open(io.BytesIO(jpg)).convert("RGB")
    wr, wg, wb = staticmap.FLAVORS["light"]["water"]
    watery = sum(c for c, (r, g, b) in (im.getcolors(1 << 20) or [])
                 if abs(r - wr) < 40 and abs(g - wg) < 40 and abs(b - wb) < 40)
    assert watery > 256 * 256 * 0.4                           # most of the frame is water
    assert _reddish(im) > 0                                    # marker drawn on top


def test_render_without_points_does_not_crash():
    rec = {"path": str(FIXTURE), "format": basemaps.FORMAT_PMTILES,
           "tile_type": "png", "min_zoom": 0, "max_zoom": 1}
    assert staticmap.render(rec, [], width=80, height=60)[:8] == b"\x89PNG\r\n\x1a\n"


# ---- the maps embedded in the HTML report ---------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    from gleapp import hashstore, stash
    hashstore.close(); stash.close()
    yield
    hashstore.close(); stash.close()


def _gps_jpeg(path, lat, lon, color, *, gps=True):
    import piexif
    Image.new("RGB", (64, 48), color).save(path, "JPEG")
    if not gps:
        return

    def dms(v):
        v = abs(v); d = int(v); m = int((v - d) * 60); s = round(((v - d) * 60 - m) * 60 * 1e4)
        return ((d, 1), (m, 1), (s, 10000))
    g = {piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
         piexif.GPSIFD.GPSLatitude: dms(lat),
         piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
         piexif.GPSIFD.GPSLongitude: dms(lon)}
    piexif.insert(piexif.dump({"GPS": g}), str(path))


def test_report_embeds_overview_and_per_file_maps(tmp_path):
    from gleapp import report
    ev = tmp_path / "ev" / "DCIM"
    ev.mkdir(parents=True)
    # far enough apart that even this fixture's low max_zoom (1) keeps them as two
    # distinct markers rather than clustering them - see test_staticmap.py's own
    # test for what happens when files DO share a spot
    _gps_jpeg(ev / "a.jpg", 28.54, -81.37, (200, 40, 40))
    _gps_jpeg(ev / "b.jpg", 47.60, -122.30, (40, 120, 200))
    _gps_jpeg(ev / "c.jpg", 0, 0, (90, 90, 90), gps=False)      # no GPS: gets no map
    c = open_case(tmp_path / "case", create=True, examiner="t")
    assert ingest_sources(c, [Source(name="ev", path=str(ev.parent))]) == 3
    process(c, workers=1, keyframes=2, screen=False)
    basemaps.import_basemap(FIXTURE, name="base")               # raster fixture, z0-1

    html = report.export_html(c, tmp_path / "r.html", maps=True).read_text(encoding="utf-8")
    c.close()
    assert "class='overview'" in html
    assert "2 geolocated file(s)" in html
    assert html.count("class='locmap openable'") == 2            # the two GPS files, not c.jpg
    assert "Basemap used in review: base" in html                # provenance recorded by the render
    import hashlib
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() not in html   # name only, no hash

    # the overview map is a clickable SVG (an <image> plus a transparent, linked
    # <circle> on each marker) rather than a plain <img>, so a click on either
    # marker jumps to that file's card
    assert "<svg class='ovsvg'" in html and html.count("<image href=") == 1
    assert html.count("fill='transparent'") == 2                 # one hit-circle per GPS file
    assert html.count("href='#file-") == 4                       # 2 on the map + 2 in the jump list
    assert "class='ovlinkscap'" in html and "a.jpg" in html and "b.jpg" in html
    # and maps can be turned off
    c2 = open_case(tmp_path / "case")
    html2 = report.export_html(c2, tmp_path / "r2.html", maps=False).read_text(encoding="utf-8")
    c2.close()
    assert "class='overview'" not in html2 and "class='locmap openable'" not in html2


# ---- what the report does with a file the basemap cannot draw --------------
# A point outside the basemap still renders: the tiles come back empty and the result
# is the background colour with a pin on it, which reads as a location with nothing
# around it. The LAVA export skips such a file and counts it; these pin that the HTML
# report does the same, that the overview is framed on the files it could draw, and
# that the note accounts for the cards left without a map.

WEST = (28.54, -81.38)          # lon < 0: tile column 0 at the fixture's top zoom
EAST = (59.33, 18.07)           # lon > 0: tile column 1


def _half_world(monkeypatch):
    """Serve only the western half of the world, so WEST is covered and EAST is not."""
    real = basemaps.pmtiles_tile
    monkeypatch.setattr(basemaps, "pmtiles_tile",
                        lambda path, z, x, y: None if x else real(path, z, x, y))


def _geo_case(tmp_path, points, name="case"):
    """A case holding one geolocated file per (lat, lon), with the fixture imported."""
    ev = tmp_path / name / "DCIM"
    ev.mkdir(parents=True)
    for i, (lat, lon) in enumerate(points):
        _gps_jpeg(ev / f"f{i}.jpg", lat, lon, (40 + 30 * i, 120, 200))
    c = open_case(tmp_path / f"{name}-case", create=True, examiner="t")
    ingest_sources(c, [Source(name="ev", path=str(ev.parent))])
    process(c, workers=1, keyframes=2, screen=False)
    basemaps.import_basemap(FIXTURE, name=name)
    return c


def _report(tmp_path, points, name="case") -> str:
    from gleapp import report
    c = _geo_case(tmp_path, points, name)
    try:
        return report.export_html(c, tmp_path / f"{name}.html", maps=True).read_text(
            encoding="utf-8")
    finally:
        c.close()


def _note(doc: str) -> str:
    return re.search(r"<div class='ovnote'>(.*?)</div>", doc, re.S).group(1)


def test_a_point_the_basemap_cannot_draw_renders_as_a_blank_square(monkeypatch):
    """The premise the skip rests on, measured rather than asserted.

    The same figures come off a real regional basemap: a Stockholm point on an Orlando
    extract drew a 360x240 JPEG that was 91.9% one colour, against 1.2% for a point
    inside it.
    """
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda *a, **k: None)
    rec = {"path": str(FIXTURE), "format": basemaps.FORMAT_PMTILES,
           "tile_type": "png", "min_zoom": 0, "max_zoom": 1}
    assert staticmap.covers(rec, EAST[1], EAST[0]) is False
    im = Image.open(io.BytesIO(staticmap.render(
        rec, [(EAST[1], EAST[0])], width=360, height=240, fmt="jpeg"))).convert("RGB")
    assert max(c for c, _ in im.getcolors(1 << 20)) > 0.85 * 360 * 240
    assert _reddish(im) > 0, "the blank square carries a pin, which is what misreads"


def test_the_report_draws_no_locator_for_a_file_outside_the_basemap(tmp_path,
                                                                    monkeypatch):
    _half_world(monkeypatch)
    doc = _report(tmp_path, [WEST, EAST])
    assert doc.count("class='locmap openable'") == 1, "the uncovered file still got a locator"
    assert "2 geolocated file(s): 1 drawn on the imported offline basemap" in _note(doc)
    assert "1 outside the basemap" in _note(doc)


def test_the_report_overview_frames_only_the_files_it_drew(tmp_path, monkeypatch):
    """One far away file the basemap cannot show would otherwise zoom the overview out
    until the files it can show are a single dot."""
    _half_world(monkeypatch)
    doc = _report(tmp_path, [WEST, EAST])
    drawn = re.search(r"class='overview'.*?<image href='data:image/png;base64,([^']+)'",
                      doc, re.S).group(1)
    rec = {"path": str(FIXTURE), "format": basemaps.FORMAT_PMTILES,
           "tile_type": "png", "min_zoom": 0, "max_zoom": 1}

    def px(raw: bytes) -> bytes:
        return Image.open(io.BytesIO(raw)).convert("RGB").tobytes()

    covered = staticmap.render(rec, [(WEST[1], WEST[0])], width=900, height=540,
                               fmt="png")
    both = staticmap.render(rec, [(WEST[1], WEST[0]), (EAST[1], EAST[0])],
                            width=900, height=540, fmt="png")
    assert px(covered) != px(both), "the two framings draw the same image"  # control
    assert px(base64.b64decode(drawn)) == px(covered)


def test_the_locations_note_accounts_for_every_geolocated_file(tmp_path, monkeypatch):
    """Each geolocated file lands in exactly one bucket, so the note explains every
    card that has no map instead of leaving the reader to guess."""
    from gleapp import report
    _half_world(monkeypatch)
    c = _geo_case(tmp_path, [WEST, WEST, EAST])
    try:
        rows = report._rows(c)
        _, per, tally, _, _ = report._render_report_maps(c, rows, flavor="light", cap=400)
        assert sum(tally.values()) == 3 and tally["drawn"] == len(per) == 2
        assert tally["outside the basemap"] == 1
        _, capped, tally, _, _ = report._render_report_maps(c, rows, flavor="light", cap=1)
        assert sum(tally.values()) == 3 and tally["over the cap"] == 2
        assert tally["drawn"] == len(capped)
    finally:
        c.close()


def test_nothing_covered_leaves_the_note_without_a_map(tmp_path, monkeypatch):
    """No coverage means no overview, and a line saying so rather than no section.

    An image framed on points the basemap cannot draw would be an empty background,
    and a reader given no section at all cannot tell that from maps being turned off.
    """
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda *a, **k: None)
    doc = _report(tmp_path, [WEST, EAST])
    assert "class='overview'" in doc and "class='locmap openable'" not in doc
    assert "<img" not in _note(doc) and "data:image/png" not in doc
    assert "2 geolocated file(s): 0 drawn" in _note(doc)
    assert "2 outside the basemap" in _note(doc)
    assert "no overview map" in _note(doc).lower()


def test_the_html_report_and_the_lava_export_map_the_same_files(tmp_path, monkeypatch):
    """The two reports are built from one case, so they must not disagree about which
    file the basemap can place. They are separate code paths, and the HTML one used to
    draw a locator for every geolocated file whatever the basemap held."""
    import sqlite3
    from gleapp import lava, report
    _half_world(monkeypatch)
    c = _geo_case(tmp_path, [WEST, EAST])
    try:
        doc = report.export_html(c, tmp_path / "r.html", maps=True).read_text(
            encoding="utf-8")
        lava.export_lava(c, tmp_path / "lava")
    finally:
        c.close()
    html_mapped = {
        re.search(r"<summary>(.*?)</summary>", card).group(1)
        for card in doc.split("<div class='card' id=")[1:] if "class='locmap openable'" in card}

    manifest = json.loads((tmp_path / "lava" / "_lava_data.lava").read_text(
        encoding="utf-8"))
    db = sqlite3.connect(tmp_path / "lava" / manifest["lava_db_name"])
    try:
        lava_mapped = {name for name, ref in
                       db.execute("SELECT file_name, map FROM media_locations") if ref}
    finally:
        db.close()
    assert html_mapped == lava_mapped == {"f0.jpg"}


def test_cluster_svg_markers_fans_out_files_sharing_a_point():
    """Two files whose markers land on (near enough) the same pixel become one
    numbered cluster with a collapsed petal per file, Google-Earth-pin-stack
    style; a file on its own keeps the plain always-clickable marker."""
    import math
    from gleapp import report
    geo_rows = [{"id": 1, "orig_name": "a.jpg"}, {"id": 2, "orig_name": "b.jpg"},
                {"id": 3, "orig_name": "solo.jpg"}]
    marker_px = {1: (100.0, 100.0), 2: (102.0, 101.0),     # 2px apart: one cluster
                 3: (500.0, 300.0)}                        # far away: its own marker
    svg = report._cluster_svg_markers(geo_rows, marker_px)  # pylint: disable=protected-access

    assert "<a href='#file-3'><circle cx='500.0' cy='300.0' r='12' fill='transparent'>" in svg

    assert "class='cluster'" in svg and "class='clusterdot'" in svg
    assert ">2<" in svg                                     # the count badge
    assert svg.count("class='petal'") == 2
    assert "href='#file-1'" in svg and "href='#file-2'" in svg

    # each petal is its own group, scaled from zero at the shared centre (cx,cy) -
    # a line to the number and the dot at the end of it collapse to that one point
    # together, which is what a click-a-name test can't see but a real hover can
    m = re.search(
        r"<g class='petal' style='transform-origin:([\d.]+)px ([\d.]+)px'>"
        r"<line class='spoke' x1='([\d.]+)' y1='([\d.]+)' x2='([\d.]+)' y2='([\d.]+)'>"
        r"</line><a href='#file-1'><circle class='petaldot' cx='([\d.]+)' cy='([\d.]+)' r='9'>",
        svg)
    assert m
    ox, oy, x1, y1, x2, y2, dcx, dcy = (float(g) for g in m.groups())
    # the transform-origin and the line's start must be the SAME shared centre -
    # that's what makes the group collapse to one point rather than fly off
    assert (ox, oy) == (100.0, 100.0) == (x1, y1)
    assert (x2, y2) == (dcx, dcy)                            # the line reaches the dot it points at

    # moving from the centre to a petal, or from one petal to another, crosses
    # empty SVG space either way - only an always-on zone spanning the whole fan
    # (not just a line to one petal) keeps the cluster hovered while doing that
    m = re.search(r"<circle class='clusterzone' cx='([\d.]+)' cy='([\d.]+)' r='([\d.]+)' "
                  r"fill='transparent'>", svg)
    assert m
    zcx, zcy, zr = (float(g) for g in m.groups())
    assert (zcx, zcy) == (100.0, 100.0)
    petal_dist = math.hypot(dcx - zcx, dcy - zcy)
    assert zr >= petal_dist + 9                             # covers the petal's own radius too
    assert svg.index("clusterzone") < svg.index("clusterdot")   # painted first, so under it


# ---- labels ----------------------------------------------------------------
# A locator that shows only shapes says where something is relative to nothing. The
# names are in the tile already; these pin that they are read and drawn.

def test_mvt_decode_exposes_feature_attributes():
    """The key and value tables can follow the features that index them, so they are
    resolved once the layer is read rather than as each feature is parsed."""
    raw = _tile(_layer("roads", mvt.LINESTRING, [[(0, 2048), (4096, 2048)]],
                       attrs={"name": "Rosalind Avenue", "min_zoom": 12}))
    feat = mvt.decode(raw)["roads"]["features"][0]
    assert feat["tags"] == {"name": "Rosalind Avenue", "min_zoom": 12}
    assert feat["paths"][0] == [(0, 2048), (4096, 2048)]       # geometry still there
    assert "tag_indices" not in feat, "the raw indices leaked into the result"


def test_a_feature_with_no_attributes_still_decodes():
    raw = _tile(_layer("water", mvt.POLYGON, [[(0, 0), (100, 0), (100, 100), (0, 100)]]))
    feat = mvt.decode(raw)["water"]["features"][0]
    assert feat["tags"] == {}


def test_a_road_is_labelled_at_its_middle_not_at_a_vertex():
    """A two-point line has no middle vertex, so taking one puts the label at an end.

    On a real map that is the tile boundary, where the label is clipped or sits on
    the next road along.
    """
    assert staticmap._path_midpoint([(0, 0), (100, 0)]) == (50.0, 0.0)
    # detail bunched at one end must not drag the label there
    bunched = [(0, 0), (1, 0), (2, 0), (3, 0), (103, 0)]
    x, _ = staticmap._path_midpoint(bunched)
    assert 50 <= x <= 53, x
    assert staticmap._path_midpoint([(7, 9), (7, 9)]) == (7, 9)     # zero length

    # and the label pass has to use it: a two-point road spanning the tile places
    # its label at the tile's middle, not at the vertex the end of the line sits on
    raw = _tile(_layer("roads", mvt.LINESTRING, [[(0, 2048), (4096, 2048)]],
                       attrs={"name": "Rosalind Avenue", "min_zoom": 0}))
    candidates = staticmap._label_points(mvt.decode(raw), 0.0, 0.0, 15)
    assert len(candidates) == 1
    _, _, x, y, text = candidates[0]
    scale = staticmap.TILE * staticmap._SS / 4096
    assert text == "Rosalind Avenue"
    assert x == 2048 * scale, "the label is not at the middle of the road"
    assert y == 2048 * scale


def _render_road(monkeypatch, name, *, zoom=15, min_zoom=12, labels=True):
    tile = _tile(_layer("roads", mvt.LINESTRING, [[(0, 2048), (4096, 2048)]],
                        attrs={"name": name, "min_zoom": min_zoom}))
    monkeypatch.setattr(basemaps, "pmtiles_tile", lambda p, z, x, y: tile)
    rec = {"path": "x.pmtiles", "format": basemaps.FORMAT_PMTILES,
           "tile_type": "mvt", "min_zoom": 0, "max_zoom": 18}
    # wide enough that a tile's midpoint is inside the frame: at 0,0 four tiles meet
    # at the centre, so on a small canvas every candidate falls off the edge
    return staticmap.render(rec, [(0.0, 0.0)], width=700, height=500, zoom=zoom,
                            fmt="png", labels=labels)


def _ink(png: bytes) -> int:
    """Distinct colours in the image: text with a halo adds many, a plain road few."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    return len(im.getcolors(maxcolors=1_000_000) or [])


def test_a_named_road_is_labelled(monkeypatch):
    with_label = _ink(_render_road(monkeypatch, "Rosalind Avenue"))
    without = _ink(_render_road(monkeypatch, "Rosalind Avenue", labels=False))
    assert with_label > without, "labels=True drew no more than labels=False"


def test_a_feature_the_basemap_hides_at_this_zoom_is_not_labelled(monkeypatch):
    """Every Protomaps feature carries ``min_zoom``. Honouring it means the tileset
    decides what appears, rather than a rule invented in this renderer."""
    shown = _ink(_render_road(monkeypatch, "Rosalind Avenue", zoom=15, min_zoom=12))
    hidden = _ink(_render_road(monkeypatch, "Rosalind Avenue", zoom=10, min_zoom=12))
    plain = _ink(_render_road(monkeypatch, "Rosalind Avenue", zoom=10, min_zoom=12,
                              labels=False))
    assert shown > hidden
    assert hidden == plain, "a feature hidden at this zoom was labelled anyway"


def test_labels_are_skipped_when_no_font_is_available(monkeypatch):
    """Pillow's scalable default is used rather than a vendored font. A build without
    it draws no labels instead of failing, so the map is still produced."""
    monkeypatch.setattr(staticmap, "_font", lambda size: None)
    with_font_gone = _ink(_render_road(monkeypatch, "Rosalind Avenue"))
    plain = _ink(_render_road(monkeypatch, "Rosalind Avenue", labels=False))
    assert with_font_gone == plain
