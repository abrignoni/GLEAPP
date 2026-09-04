"""Static location maps for the report: the MVT decoder, the PMTiles tile reader,
the renderer, and the maps embedded in the HTML report. Everything here is offline
and touches only local files."""

import io

import pytest
from PIL import Image

from gleapp import basemaps, mvt, staticmap
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


def _feature(gtype: int, rings) -> bytes:
    g = _geom(gtype, rings)
    return _tag(3, 0) + _uv(gtype) + _tag(4, 2) + _uv(len(g)) + g


def _layer(name: str, gtype: int, rings, extent: int = 4096) -> bytes:
    body = _tag(15, 0) + _uv(2) + _tag(1, 2) + _uv(len(name)) + name.encode()
    body += _tag(5, 0) + _uv(extent)
    feat = _feature(gtype, rings)
    body += _tag(2, 2) + _uv(len(feat)) + feat
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
    _gps_jpeg(ev / "a.jpg", 28.54, -81.37, (200, 40, 40))
    _gps_jpeg(ev / "b.jpg", 28.60, -81.20, (40, 120, 200))
    _gps_jpeg(ev / "c.jpg", 0, 0, (90, 90, 90), gps=False)      # no GPS: gets no map
    c = open_case(tmp_path / "case", create=True, examiner="t")
    assert ingest_sources(c, [Source(name="ev", path=str(ev.parent))]) == 3
    process(c, workers=1, keyframes=2, screen=False)
    basemaps.import_basemap(FIXTURE, name="base")               # raster fixture, z0-1

    html = report.export_html(c, tmp_path / "r.html", maps=True).read_text(encoding="utf-8")
    c.close()
    assert "class='overview'" in html
    assert "2 geolocated file(s)" in html
    assert html.count("class='locmap'") == 2                    # the two GPS files, not c.jpg
    assert "Basemap used in review" in html                     # provenance recorded by the render
    # and maps can be turned off
    c2 = open_case(tmp_path / "case")
    html2 = report.export_html(c2, tmp_path / "r2.html", maps=False).read_text(encoding="utf-8")
    c2.close()
    assert "class='overview'" not in html2 and "class='locmap'" not in html2
