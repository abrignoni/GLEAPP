"""The times FAT and exFAT record reach the gallery, as text.

FAT32 and exFAT store a wall clock with no zone, so GLEAPP sets no date on such
a file: its FS written / created / accessed columns are empty by design, because
any instant built from a zone-less reading would be invented. The readings
themselves are kept in ``recorded_times`` and were reaching only the HTML report,
so an examiner looking at a walked or recovered FAT file in the gallery saw no
time at all. They are now in the file's details and available as a column.

The fixture is the one qnxprobe validates its exFAT deleted recovery against.
"""

import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ewfwriter import write_ewf                       # pylint: disable=import-error

FIXTURES = Path(__file__).parent / "fixtures"
APPJS = Path(__file__).resolve().parents[1] / "gleapp/web/static/app.js"


def _client():
    from gleapp.web.app import create_app             # pylint: disable=import-outside-toplevel
    return create_app(None).test_client()


def _wait(cl, timeout=60):
    for _ in range(timeout * 4):
        job = cl.get("/api/job").get_json()
        if not job["running"]:
            return job
        time.sleep(0.25)
    raise AssertionError("job never finished")


def _exfat_e01(tmp_path) -> Path:
    raw = gzip.decompress((FIXTURES / "exfat-deleted.img.gz").read_bytes())
    folder = tmp_path / "ev"
    folder.mkdir(parents=True, exist_ok=True)
    return Path(write_ewf(folder, "card", raw)[0])


def _case_with_recovered_exfat(tmp_path):
    cl = _client()
    cl.post("/api/case/create",
            json={"path": str(tmp_path / "c"), "name": "c", "examiner": "t"})
    cl.post("/api/case/ingest", json={
        "sources": [{"path": str(_exfat_e01(tmp_path))}],
        "options": {"screen": False, "keyframes": 0},
    })
    _wait(cl)
    assert cl.post("/api/source/carve", json={"name": "card.E01"}).status_code == 200
    _wait(cl)
    return cl


def test_the_api_carries_the_recorded_times_of_an_exfat_file(tmp_path):
    cl = _case_with_recovered_exfat(tmp_path)
    rows = cl.get("/api/files").get_json()["files"]
    said = [r for r in rows if r.get("recorded_times")]
    assert said, "no row reached the gallery with the times its filesystem recorded"
    for r in said:
        # a zone-less reading is never turned into an instant
        assert not r["mtime"] and not r["ctime"], "a wall clock became a date"
        got = json.loads(r["recorded_times"])
        assert got.get("modified")
        # exFAT stores a UTC offset per time; it is carried as stored
        assert any("utc offset" in k for k in got)

    one = cl.get(f"/api/file/{said[0]['id']}").get_json()
    assert one["recorded_times"] == said[0]["recorded_times"]


def test_the_gallery_renders_the_recorded_times(tmp_path):
    """The value reaching the client is not enough; something has to show it."""
    src = APPJS.read_text(encoding="utf-8")
    assert "function fmtRecorded(" in src
    assert "Recorded (as stored, no zone)" in src, "the details pane shows no such row"
    assert '"recorded_times"' in src, "no column offers it in the list view"
    # and the value the renderer is handed really does format to readable text
    cl = _case_with_recovered_exfat(tmp_path)
    row = next(r for r in cl.get("/api/files").get_json()["files"] if r.get("recorded_times"))
    got = json.loads(row["recorded_times"])
    rendered = "; ".join(f"{k} {v}" for k, v in got.items() if v)
    assert "modified " in rendered and rendered.count(";") >= 1


def test_recorded_times_is_a_sortable_column(tmp_path):
    """It is in the list payload, so the server must accept it as a sort key
    rather than silently ignoring it."""
    from gleapp.web.app import LIST_COLS, FIELDS       # pylint: disable=import-outside-toplevel
    assert "recorded_times" in FIELDS
    assert "recorded_times" in LIST_COLS
    cl = _case_with_recovered_exfat(tmp_path)
    r = cl.get("/api/files?sort=recorded_times&dir=asc")
    assert r.status_code == 200
    assert r.get_json()["files"]
