"""No report or export names a location on the machine the case was made on.

``files.path`` is the absolute path as ingested: the evidence folder for a folder
source, the case's staged folder for an archive row. A report or export is meant to
leave that machine, and ``rel_path`` and ``orig_path`` already say where the file was
within the evidence, so no writer publishes ``files.path``. This runs the real
pipeline on synthetic files and reads every writer's output back as text.
"""

import csv
import io
import json
import re
import zipfile

from PIL import Image

from gleapp import report
from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

# every field the report dialog offers that could carry a path, plus the error column
FIELDS = ["name", "disk_name", "path", "orig_path", "source", "md5", "error"]


def _case(tmp_path, monkeypatch):
    """A folder-source case under folders whose names hold a space, with one decodable
    image and one that stores an error."""
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path / "cfg"))
    ev = tmp_path / "evidence folder" / "DCIM"
    ev.mkdir(parents=True)
    Image.new("RGB", (64, 48), (200, 40, 40)).save(ev / "ok.jpg", "JPEG")
    (ev / "bad.jpg").write_bytes(b"not an image at all")
    c = open_case(tmp_path / "case folder", create=True, examiner="t")
    assert ingest_sources(c, [Source(name="ev", path=str(ev.parent))]) == 2
    process(c, workers=1, keyframes=2, screen=False)
    return c


def test_exports_carry_no_local_path(tmp_path, monkeypatch):
    from gleapp import hashstore, stash
    c = _case(tmp_path, monkeypatch)
    out = tmp_path / "out"
    out.mkdir()
    try:
        made = {
            "html": report.export_html(c, out / "r.html", fields=FIELDS),
            "csv": report.export_csv(c, out / "r.csv"),
            "json": report.export_json(c, out / "r.json"),
            "md5": report.export_md5(c, out / "md5.csv"),
            "kmz": report.export_kml(c, out / "geo.kmz"),
        }
    finally:
        c.close()
        hashstore.close()
        stash.close()
    texts = {k: p.read_text(encoding="utf-8") for k, p in made.items() if k != "kmz"}
    with zipfile.ZipFile(made["kmz"]) as zf:
        texts["kmz"] = "".join(zf.read(n).decode("utf-8", "replace")
                               for n in zf.namelist() if n.endswith(".kml"))
    # the case folder's own name is the default case name and titles the report, so
    # only its parents and the evidence folder are checked for
    for name, text in texts.items():
        assert str(tmp_path) not in text, name
        assert tmp_path.name not in text, name
        assert "evidence folder" not in text, name

    # the relative paths survive: rel_path is stored with the platform's separator
    rows = list(csv.DictReader(io.StringIO(texts["csv"])))
    assert sorted(r["file_path"].replace("\\", "/") for r in rows) == [
        "DCIM/bad.jpg", "DCIM/ok.jpg"]
    files = json.loads(texts["json"])["files"]
    assert len(files) == 2 and all("path" not in f and f["rel_path"] for f in files), files
    assert re.search(r"DCIM[\\/]ok\.jpg", texts["html"]), "Path field lost the source path"
    assert [f for f in files if f["error"]][0]["error"].startswith("UnidentifiedImageError")
