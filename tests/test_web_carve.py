"""Carving an E01 is reachable from the gallery, two ways.

The backend has carved an acquisition for a while (``archive.carve_source``),
but only the command line could ask for it. An examiner works in the launcher
and the review gallery, so the choice has to be there: a checkbox on the
launcher to carve during the first ingest, and a button in the Source panel to
carve later.
"""

import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ewfwriter import write_ewf                        # pylint: disable=import-error
from fatwriter import build_fat32                      # pylint: disable=import-error

TEMPLATE = Path(__file__).resolve().parents[1] / "gleapp/web/templates/index.html"
APPJS = Path(__file__).resolve().parents[1] / "gleapp/web/static/app.js"


def _jpeg(colour) -> bytes:
    from PIL import Image                              # pylint: disable=import-outside-toplevel
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), colour).save(buf, "JPEG")
    return buf.getvalue()


def _e01_with_free_space_media(tmp_path) -> Path:
    """A FAT32 volume with two live images, and a third JPEG planted in a run of
    clusters the filesystem never allocated — the deleted-media case carving is
    for."""
    vol = bytearray(build_fat32([
        ("LIVE1", "JPG", _jpeg((200, 40, 40)), (2023, 6, 1, 12, 0, 0)),
        ("LIVE2", "JPG", _jpeg((40, 160, 60)), (2022, 1, 2, 3, 4, 6)),
    ]))
    planted = _jpeg((10, 20, 240))
    at = len(vol) - 64 * 1024              # deep in the unallocated tail
    vol[at:at + len(planted)] = planted
    folder = tmp_path / "ev"
    folder.mkdir(parents=True, exist_ok=True)
    return Path(write_ewf(folder, "acq", bytes(vol))[0])


def _client():
    from gleapp.web.app import create_app              # pylint: disable=import-outside-toplevel
    return create_app(None).test_client()


def _wait(cl, timeout=60):
    for _ in range(timeout * 4):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            return job
        time.sleep(0.25)
    raise AssertionError("job never finished")


def _origins(case_dir):
    from gleapp.case import open_case                  # pylint: disable=import-outside-toplevel
    case = open_case(case_dir)
    try:
        return [r["origin"] for r in case.db.iter_files()]
    finally:
        case.close()


def _audit_actions(cl):
    return [e["action"] for e in cl.get("/api/audit").get_json()]


# ---- the launcher checkbox ------------------------------------------------

def test_the_launcher_template_and_script_carry_the_carve_option():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="optCarve"' in html
    js = APPJS.read_text(encoding="utf-8")
    assert re.search(r"carve:\s*\$\(\"#optCarve\"\)\.checked", js)


def test_ingesting_an_e01_with_carve_ticked_walks_then_carves(tmp_path):
    image = _e01_with_free_space_media(tmp_path)
    cl = _client()
    assert cl.post("/api/case/create",
                   json={"path": str(tmp_path / "c"), "name": "c",
                         "examiner": "t"}).status_code == 200
    r = cl.post("/api/case/ingest", json={
        "sources": [{"path": str(image)}],
        "options": {"screen": False, "keyframes": 0, "carve": True},
    })
    assert r.status_code == 200, r.get_json()
    job = _wait(cl)
    assert job["stage"] == "done", job
    assert "carve-source" in _audit_actions(cl)

    origins = _origins(tmp_path / "c")
    assert "walk" in origins, "the live files should still be walked"
    assert "carve" in origins, "the planted free-space JPEG should be carved"


def test_ingesting_without_the_option_only_walks(tmp_path):
    image = _e01_with_free_space_media(tmp_path)
    cl = _client()
    cl.post("/api/case/create",
            json={"path": str(tmp_path / "c"), "name": "c", "examiner": "t"})
    cl.post("/api/case/ingest", json={
        "sources": [{"path": str(image)}],
        "options": {"screen": False, "keyframes": 0},
    })
    assert _wait(cl)["stage"] == "done"
    assert set(_origins(tmp_path / "c")) == {"walk"}
    assert "carve-source" not in _audit_actions(cl)


# ---- the Source-panel button --------------------------------------------

def test_source_carve_endpoint_adds_and_processes_carved_rows(tmp_path):
    image = _e01_with_free_space_media(tmp_path)
    cl = _client()
    cl.post("/api/case/create",
            json={"path": str(tmp_path / "c"), "name": "c", "examiner": "t"})
    cl.post("/api/case/ingest", json={
        "sources": [{"path": str(image)}],
        "options": {"screen": False, "keyframes": 0},
    })
    _wait(cl)
    assert set(_origins(tmp_path / "c")) == {"walk"}

    r = cl.post("/api/source/carve", json={"name": "acq.E01"})
    assert r.status_code == 200, r.get_json()
    job = _wait(cl)
    assert job["stage"] == "done", job
    assert "carve-source" in _audit_actions(cl)

    from gleapp.case import open_case                  # pylint: disable=import-outside-toplevel
    case = open_case(tmp_path / "c")
    try:
        carved = [r for r in case.db.iter_files() if r["origin"] == "carve"]
        assert carved, "carving the source should have added rows"
        # they went through processing too, not just registration
        assert all(r["md5"] for r in carved)
    finally:
        case.close()

    sset = cl.get("/api/sources").get_json()
    acq = {s["name"]: s for s in sset}["acq.E01"]
    assert acq["carved"] == len(carved) and acq["walked"] > 0


def test_the_how_recovered_filter_narrows_to_walked_or_carved(tmp_path):
    image = _e01_with_free_space_media(tmp_path)
    cl = _client()
    cl.post("/api/case/create",
            json={"path": str(tmp_path / "c"), "name": "c", "examiner": "t"})
    cl.post("/api/case/ingest", json={
        "sources": [{"path": str(image)}],
        "options": {"screen": False, "keyframes": 0, "carve": True},
    })
    assert _wait(cl)["stage"] == "done"

    def total(qs):
        return cl.get("/api/files" + qs).get_json()["total"]

    everything = total("")
    walked = total("?origin=walk")
    carved = total("?origin=carve")
    assert walked >= 1 and carved >= 1
    assert walked + carved == everything
    # a bogus value is ignored rather than filtering to nothing
    assert total("?origin=nonsense") == everything

    # the "How recovered" control lives in the Carving section, which is hidden
    # until an acquisition is present
    template = TEMPLATE.read_text(encoding="utf-8")
    assert '<details class="fsec" data-sec="carve" hidden>' in template
    assert 'id="forigin"' in template
    js = APPJS.read_text(encoding="utf-8")
    assert 'p.set("origin"' in js
    assert 's.format === "ewf" || s.format === "raw"' in js


def test_source_carve_refuses_a_non_acquisition(tmp_path):
    from PIL import Image                              # pylint: disable=import-outside-toplevel
    import zipfile
    z = tmp_path / "extract.zip"
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(buf, "JPEG")
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.jpg", buf.getvalue())
    cl = _client()
    cl.post("/api/case/create",
            json={"path": str(tmp_path / "c"), "name": "c", "examiner": "t"})
    cl.post("/api/case/ingest", json={
        "sources": [{"path": str(z)}], "options": {"screen": False, "keyframes": 0},
    })
    _wait(cl)
    r = cl.post("/api/source/carve", json={"name": "extract.zip"})
    assert r.status_code == 400
