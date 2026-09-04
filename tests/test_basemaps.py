"""Offline basemaps: a PMTiles file served by byte range, a raster MBTiles served tile by
tile with the row flipped, import with a recorded hash, and the case remembering which
file its maps were drawn on."""

import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from gleapp import basemaps
from gleapp.case import open_case
from gleapp.cli import main as cli_main

FIXTURE = Path(__file__).parent / "fixtures" / "tiny.pmtiles"
COLORS = {"nw": (200, 40, 40), "ne": (40, 200, 40), "sw": (40, 40, 200), "se": (200, 200, 40)}


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    """Basemaps live in the app data folder; keep every test in its own."""
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, "PNG")
    return buf.getvalue()


def _mbtiles(path: Path, *, fmt="png") -> Path:
    """Five synthetic tiles in TMS row order: at zoom 1 the TMS row 1 is the northern half."""
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE metadata(name TEXT, value TEXT);"
        "CREATE TABLE tiles(zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);")
    c.executemany("INSERT INTO metadata VALUES(?,?)", [
        ("name", "tiny synthetic"), ("format", fmt), ("bounds", "-180,-85.0511,180,85.0511"),
        ("minzoom", "0"), ("maxzoom", "1"), ("attribution", "GLEAPP test fixture")])
    blob = _png if fmt == "png" else (lambda col: b"\x1a\x00pbf")
    c.executemany("INSERT INTO tiles VALUES(?,?,?,?)", [
        (0, 0, 0, blob((40, 40, 40))),
        (1, 0, 1, blob(COLORS["nw"])), (1, 1, 1, blob(COLORS["ne"])),
        (1, 0, 0, blob(COLORS["sw"])), (1, 1, 0, blob(COLORS["se"]))])
    c.commit()
    c.close()
    return path


def test_pmtiles_header_is_read_from_the_bytes():
    h = basemaps.read_pmtiles_header(FIXTURE)
    assert h["spec_version"] == 3 and h["tile_type"] == "png"
    assert (h["min_zoom"], h["max_zoom"]) == (0, 1)
    assert h["addressed_tiles"] == 5 and h["clustered"] is True
    assert h["bounds"] == pytest.approx([-180.0, -85.0511, 180.0, 85.0511])
    assert h["internal_compression"] == "gzip" and h["metadata"]["name"] == "tiny synthetic"
    assert basemaps.inspect(FIXTURE)["format"] == "pmtiles"


def test_inspect_refuses_what_it_cannot_serve(tmp_path):
    with pytest.raises(ValueError, match="vector MBTiles"):
        basemaps.inspect(_mbtiles(tmp_path / "vec.mbtiles", fmt="pbf"))
    other = tmp_path / "x.bin"
    other.write_bytes(b"not a map at all")
    with pytest.raises(ValueError, match="neither"):
        basemaps.inspect(other)
    assert basemaps.inspect(_mbtiles(tmp_path / "ok.mbtiles"))["format"] == "mbtiles"


def test_mbtiles_tiles_are_served_the_xyz_way(tmp_path):
    p = _mbtiles(tmp_path / "t.mbtiles")
    for (x, y), corner in {(0, 0): "nw", (1, 0): "ne", (0, 1): "sw", (1, 1): "se"}.items():
        data, mime = basemaps.mbtiles_tile(p, 1, x, y)
        assert mime == "image/png"
        assert Image.open(io.BytesIO(data)).getpixel((5, 5)) == COLORS[corner], corner
    assert basemaps.mbtiles_tile(p, 2, 0, 0) is None


def test_import_copies_hashes_and_activates():
    import hashlib
    seen = []
    rec = basemaps.import_basemap(FIXTURE, progress=lambda d, t: seen.append((d, t)))
    assert rec["name"] == "tiny" and rec["format"] == "pmtiles"
    assert rec["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert Path(rec["path"]).read_bytes() == FIXTURE.read_bytes()
    assert seen and seen[-1] == (FIXTURE.stat().st_size, FIXTURE.stat().st_size)
    assert rec["attribution"] == "GLEAPP test fixture"                # its own metadata, no OSM guess
    assert basemaps.get_active() == "tiny"                            # the first import is active
    listed = basemaps.list_basemaps()
    assert [(b["name"], b["active"], b["present"]) for b in listed] == [("tiny", True, True)]
    again = basemaps.import_basemap(FIXTURE)                          # a second copy gets a new name
    assert again["name"] == "tiny-2" and basemaps.get_active() == "tiny"
    assert basemaps.remove_basemap("tiny") and basemaps.get_active() is None
    assert not basemaps.remove_basemap("tiny")
    assert [b["name"] for b in basemaps.list_basemaps()] == ["tiny-2"]


def test_styles_point_only_at_the_local_server(tmp_path):
    basemaps.import_basemap(FIXTURE, name="vec")
    basemaps.import_basemap(_mbtiles(tmp_path / "r.mbtiles"), name="ras")
    s = basemaps.style("vec", flavor="dark")
    assert s["sources"]["basemap"]["url"] == "pmtiles:///basemap/vec.pmtiles"
    assert s["glyphs"].startswith("/static/maps/fonts/") and s["sprite"] == "/static/maps/sprites/v4/dark"
    assert len(s["layers"]) > 50 and all(l.get("source", "basemap") == "basemap" for l in s["layers"])
    assert "http" not in json.dumps(s)                                # nothing leaves the machine
    r = basemaps.style("ras")
    assert r["sources"]["basemap"]["tiles"] == ["/basemap/ras/{z}/{x}/{y}"]
    assert r["sources"]["basemap"]["attribution"] == "GLEAPP test fixture"
    assert [l["type"] for l in r["layers"]] == ["background", "raster"]
    with pytest.raises(ValueError):
        basemaps.style("nope")


def _client(case_dir):
    from gleapp.web.app import create_app
    app = create_app(str(case_dir))
    app.config["TESTING"] = True
    return app.test_client(), app.config["STATE"]


def test_web_serves_pmtiles_by_range_and_records_the_basemap_used(tmp_path):
    case = open_case(tmp_path / "case", create=True, examiner="t")
    case.close()
    client, state = _client(tmp_path / "case")
    try:
        assert client.get("/api/basemaps").get_json() == {"active": None, "basemaps": []}
        assert client.get("/api/basemaps/style").status_code == 404
        r = client.post("/api/basemaps/import", json={"path": str(FIXTURE), "name": "tiny"})
        assert r.status_code == 200
        import time
        for _ in range(100):
            if not client.get("/api/job").get_json()["running"]:
                break
            time.sleep(0.05)
        j = client.get("/api/job").get_json()
        assert j["stage"] == "done" and j["stats"] == {"basemap": "tiny"}, j
        lst = client.get("/api/basemaps").get_json()
        assert lst["active"] == "tiny" and lst["basemaps"][0]["sha256"]

        whole = FIXTURE.read_bytes()
        r = client.get("/basemap/tiny.pmtiles", headers={"Range": "bytes=0-126"})
        assert r.status_code == 206 and r.data == whole[:127]           # the header, as pmtiles.js reads it
        r.close()
        r = client.get("/basemap/tiny.pmtiles", headers={"Range": "bytes=127-200"})
        assert r.status_code == 206 and r.data == whole[127:201]
        r.close()
        assert client.get("/basemap/other.pmtiles").status_code == 404
        assert client.get("/basemap/../case.gleapp").status_code in (404, 400)

        style = client.get("/api/basemaps/style?flavor=light").get_json()
        assert style["sprite"].endswith("/light") and style["sources"]["basemap"]["url"].startswith("pmtiles:///basemap/")
        assert client.get("/static/maps/pmtiles.js").status_code == 200
        assert client.get("/static/maps/sprites/v4/dark.json").status_code == 200
        assert client.get("/static/maps/fonts/Noto%20Sans%20Regular/0-255.pbf").status_code == 200

        used = client.post("/api/basemaps/used", json={}).get_json()
        assert used["ok"] and used["name"] == "tiny"
        db = state["case"].db
        assert db.get_meta("basemap_name") == "tiny" and db.get_meta("basemap_sha256") == used["sha256"]
        from gleapp import report
        html_out = report.export_html(state["case"], tmp_path / "r.html", "")
        assert used["sha256"] in html_out.read_text(encoding="utf-8")
        payload = json.loads(report.export_json(state["case"], tmp_path / "r.json", "")
                             .read_text(encoding="utf-8"))
        assert payload["basemap"]["sha256"] == used["sha256"]

        assert client.post("/api/basemaps/remove", json={"name": "tiny"}).get_json()["active"] is None
        assert client.get("/basemap/tiny.pmtiles").status_code == 404
    finally:
        state["close_current"]()


def test_web_serves_raster_mbtiles_tiles(tmp_path):
    basemaps.import_basemap(_mbtiles(tmp_path / "r.mbtiles"), name="ras")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    case.close()
    client, state = _client(tmp_path / "case")
    try:
        r = client.get("/basemap/ras/1/1/0")
        assert r.status_code == 200 and r.mimetype == "image/png"
        assert Image.open(io.BytesIO(r.data)).getpixel((5, 5)) == COLORS["ne"]
        assert client.get("/basemap/ras/3/0/0").status_code == 404
        assert client.get("/basemap/ras.mbtiles").status_code == 404     # the file itself is not served
        style = client.get("/api/basemaps/style").get_json()
        assert style["sources"]["basemap"]["type"] == "raster"
    finally:
        state["close_current"]()


def test_cli_lists_imports_and_removes(capsys):
    assert cli_main(["maps", "list"]) == 0
    assert "No basemaps imported" in capsys.readouterr().out
    assert cli_main(["maps", "import", str(FIXTURE), "--name", "cli"]) == 0
    out = capsys.readouterr().out
    assert "Imported cli (pmtiles, zoom 0-1)" in out
    assert cli_main(["maps", "list"]) == 0
    assert "* cli: pmtiles" in capsys.readouterr().out
    # a bounding box west of Greenwich starts with a minus sign, which argparse reads
    # as an option unless the value is attached with '='; the help says so
    assert cli_main(["maps", "extract", "--bbox=-77.12,38.79,-76.90,38.99", "--out", "dc.pmtiles"]) == 0
    assert "pmtiles extract https://build.protomaps.com/YYYYMMDD.pmtiles dc.pmtiles --bbox=-77.12,38.79,-76.90,38.99" in capsys.readouterr().out
    assert cli_main(["maps", "remove", "cli"]) == 0
    assert cli_main(["maps", "remove", "cli"]) == 2                    # ValueError -> exit 2
