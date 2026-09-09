"""No report or export names a location on the machine the case was made on.

``files.path`` is the absolute path as ingested: the evidence folder for a folder
source, the case's staged folder for an archive row. A report or export is meant to
leave that machine, and ``rel_path`` and ``orig_path`` already say where the file was
within the evidence, so no writer publishes ``files.path``. This runs the real
pipeline on synthetic files and reads every writer's output back as text.

The LAVA export is a folder rather than one file, and part of what it publishes is
inside a SQLite database, so it is read back through its own tables as well as its
manifest and pages.
"""

import csv
import io
import json
import re
import zipfile

from PIL import Image

from gleapp import lava, report
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


def _lava_text(root) -> str:
    """Every string the LAVA export publishes: the manifest, the two pages it writes
    for LAVA's tabs, and every text value in every table of its database."""
    import sqlite3
    from pathlib import Path as _Path

    root = _Path(root)
    parts = [p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(root.rglob("*"))
             if p.is_file() and p.suffix in (".lava", ".html", ".json")]
    db = sqlite3.connect(root / "_lava_artifacts.db")
    try:
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for name in names:
            for row in db.execute(f'SELECT * FROM "{name}"'):
                parts += [v for v in row if isinstance(v, str)]
    finally:
        db.close()
    return "\n".join(parts)


def test_lava_export_carries_no_local_path(tmp_path, monkeypatch):
    from gleapp import hashdb, hashstore, stash
    c = _case(tmp_path, monkeypatch)
    # a hash set records the path it was imported from, and the report names the
    # lists that were checked, so that path is one more way out
    hashes = tmp_path / "known hashes.csv"
    hashes.write_text("md5\n" + "\n".join(
        r["md5"] for r in c.db.iter_files() if r["md5"]) + "\n")
    hashdb.import_hashset(c.db, hashes, name="Op-Paths known", kind="known")
    out = tmp_path / "out"
    out.mkdir()
    try:
        lava.export_lava(c, out / "lava")
    finally:
        c.close()
        hashstore.close()
        stash.close()
    text = _lava_text(out / "lava")
    assert str(tmp_path) not in text
    assert tmp_path.name not in text
    assert "evidence folder" not in text
    # and the evidence-relative path is still there, which is what the report needs
    assert re.search(r"DCIM[\\/]ok\.jpg", text), "the source path was lost entirely"
    # the hash list is named, by its file name and not by where it sat
    assert "Op-Paths known" in text and "known hashes.csv" in text
