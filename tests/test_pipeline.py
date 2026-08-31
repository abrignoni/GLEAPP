import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gleapp import dedupe, hashdb
from gleapp.case import open_case, parse_source_spec
from gleapp.hashing import crypto_hashes, hamming, perceptual_hashes
from gleapp.ingest import classify, scan
from gleapp.pipeline import ingest_sources, process
from gleapp.similar import find_similar

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    """Keep tests out of the real %APPDATA%\\GLEAPP recent-cases list."""
    monkeypatch.setenv("GLEAPP_CONFIG_DIR",
                       str(tmp_path_factory.mktemp("gleapp-cfg")))


@pytest.fixture(scope="session")
def evidence(tmp_path_factory):
    ev = tmp_path_factory.mktemp("evidence")
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_sample_evidence.py"), str(ev)],
        check=True, capture_output=True,
    )
    return ev


@pytest.fixture()
def case(tmp_path, evidence):
    c = open_case(tmp_path / "case", create=True, examiner="tester")
    sources, meta = parse_source_spec(evidence / "ingest.json")
    ingest_sources(c, sources)
    process(c, workers=2, keyframes=4, screen=True)
    yield c
    c.close()


def test_classify():
    assert classify(".JPG") == "image"
    assert classify(".mkv") == "video"
    assert classify(".txt") == "other"


def test_crypto_hashes(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello world")
    h = crypto_hashes(p)
    assert h["md5"] == "5eb63bbbe01eeed093cb22bb8f5acdc3"
    assert set(h) == {"md5", "sha1", "sha256"}


def test_scan_finds_media(evidence):
    found = list(scan(evidence))
    kinds = {d.kind for d in found}
    assert "image" in kinds
    assert len(found) > 5


def test_source_spec_json(evidence):
    sources, meta = parse_source_spec(evidence / "ingest.json")
    assert len(sources) == 2
    assert meta["case"] == "Operation Sample"
    assert {s.name for s in sources} == {"USB-1", "Laptop"}


def test_source_spec_folder(evidence):
    sources, meta = parse_source_spec(evidence / "usb1")
    assert len(sources) == 1
    assert Path(sources[0].path).name == "usb1"


def test_pipeline_hashes_and_thumbs(case):
    rows = case.db.iter_files("kind='image'")
    assert rows
    for r in rows:
        assert r["md5"] and r["sha256"]
        assert r["phash"]
        assert r["thumb"]
        assert (case.thumb_dir / r["thumb"]).exists()


def test_exact_duplicate_stacking(case):
    redundant = case.db.stats()["redundant_duplicates"]
    assert redundant >= 4  # the 4 identical random PNGs + sunset copy


def test_near_duplicate_cluster(case):
    # photo_a and its heavy re-edit are the same picture -> one cluster
    rows = case.db.iter_files("rel_path LIKE '%photo_a%'")
    assert len(rows) == 2
    a, b = rows
    assert a["phash"] != b["phash"]              # hashes genuinely differ
    assert a["cluster_id"] and a["cluster_id"] == b["cluster_id"]


def test_visual_stack(case):
    """photo_a + photo_a_edit: different pHash, same picture -> one visual stack."""
    rows = case.db.iter_files("rel_path LIKE '%photo_a%'")
    vs = {r["vstack_id"] for r in rows}
    assert None not in vs and len(vs) == 1

    collapsed = case.db.iter_files(
        "rel_path LIKE '%photo_a%' AND COALESCE(vstack_id, stack_id, id) = id")
    assert len(collapsed) == 1                   # one tile for the group

    # featureless gradients must NOT be grouped (pHash is ~all-zero for them)
    sun = case.db.iter_files("rel_path LIKE '%sunset%'")
    assert all(r["vstack_id"] is None for r in sun)


def test_find_similar(case):
    row = case.db.iter_files("rel_path LIKE '%sunset_01%'")[0]
    hits = find_similar(case, row["id"], threshold=16)
    names = {Path(h["rel_path"]).name for h in hits}
    assert any("sunset" in n for n in names)


def test_hashset_match(case, tmp_path):
    row = case.db.iter_files("kind='image'")[0]
    hs = tmp_path / "known.csv"
    hs.write_text(f"{row['md5']},1\n")
    hashdb.import_hashset(case.db, hs, name="unit-known", kind="known")
    hit = hashdb.match_file(case.db, case.db.get_file(row["id"]))
    assert hit and hit["name"] == "unit-known"
    assert hit["category"] == 1


def test_video_keyframes(case):
    vids = case.db.iter_files("kind='video'")
    if not vids:
        pytest.skip("no videos generated in this environment")
    for v in vids:
        assert case.db.keyframes_for(v["id"])
        assert v["thumb"]


def test_hamming_edges():
    assert hamming(None, "abc") == 999
    assert hamming("ffffffffffffffff", "ffffffffffffffff") == 0


def test_webapp_launcher_without_case():
    from gleapp.web.app import create_app

    app = create_app(None)
    client = app.test_client()
    ctx = client.get("/api/context").get_json()
    assert ctx["needs_case"] is True
    # gallery endpoints 409 until a case is opened
    assert client.get("/api/files").status_code == 409


def test_categories_start_blank_and_are_nameable(tmp_path):
    c = open_case(tmp_path / "cats", create=True, examiner="t")
    try:
        # ships with only "Uncategorized"
        rows = c.db.list_categories()
        assert [r["code"] for r in rows] == [0]
        assert c.db.category_name(0) == "Uncategorized"

        code = c.db.add_category("")               # blank
        assert code == 1
        assert c.db.category_name(1) == "Category 1"   # fallback label
        assert c.db.get_category(1)["color"].startswith("#")

        c.db.update_category(1, name="Grooming set")
        assert c.db.category_name(1) == "Grooming set"

        c2 = c.db.add_category("Weapons")
        assert c2 == 2
        assert c.db.get_category(1)["color"] != c.db.get_category(2)["color"]
    finally:
        c.close()


def test_category_delete_keeps_label_when_in_use(tmp_path):
    c = open_case(tmp_path / "catdel", create=True, examiner="t")
    try:
        code = c.db.add_category("Temp")
        fid = c.db.upsert_file("/x/y.jpg", kind="image", category=code)
        c.db.commit()
        c.db.delete_category(code)                 # soft-delete
        row = c.db.get_category(code)
        assert row is not None and row["active"] == 0
        assert c.db.get_file(fid)["category"] == code   # file keeps its code
        assert c.db.category_name(code) == "Temp"

        c.db.delete_category(code, reassign=True)  # now wipe
        assert c.db.get_file(fid)["category"] == 0
        assert c.db.get_category(code) is None
    finally:
        c.close()


def test_reorder_sets_position(tmp_path):
    c = open_case(tmp_path / "catord", create=True, examiner="t")
    try:
        a, b, d = c.db.add_category("A"), c.db.add_category("B"), c.db.add_category("C")
        c.db.reorder_categories([d, a, b])
        order = [r["code"] for r in c.db.list_categories(include_inactive=False)
                 if r["code"] != 0]
        assert order == [d, a, b]
    finally:
        c.close()


def test_category_migration_seeds_used_codes(tmp_path, evidence):
    """A v1-style case with category codes on files gets placeholder rows."""
    from gleapp.db import CaseDB
    p = tmp_path / "old" / "case.gleapp"
    p.parent.mkdir(parents=True)
    db = CaseDB(p)
    db.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    fid = db.upsert_file("/a/b.jpg", kind="image", category=3)
    db.commit()
    db.close()
    db2 = CaseDB(p)                                # reopen -> migration runs
    try:
        from gleapp.db import SCHEMA_VERSION
        assert db2.get_meta("schema_version") == str(SCHEMA_VERSION)
        row = db2.get_category(3)
        assert row is not None and row["name"] == ""
        # v3 additive columns exist after migration
        cols = {r["name"] for r in db2.conn.execute("PRAGMA table_info(files)")}
        assert {"media_id", "orig_name", "mime", "vic_flags"} <= cols
    finally:
        db2.close()


def test_webapp_category_crud(tmp_path, evidence):
    from gleapp.web.app import create_app

    app = create_app(None)
    client = app.test_client()
    client.post("/api/case/create", json={"path": str(tmp_path / "wcc"), "name": "X"})
    assert client.get("/api/categories").get_json() == [
        {"code": 0, "name": "Uncategorized", "color": "#8b93a3",
         "notable": False, "position": 0, "active": True}
    ]
    made = client.post("/api/categories", json={"name": "Illicit"}).get_json()
    assert made["code"] == 1 and made["name"] == "Illicit"
    client.patch("/api/categories/1", json={"name": "Renamed"})
    cats = {c["code"]: c for c in client.get("/api/categories").get_json()}
    assert cats[1]["name"] == "Renamed"
    client.delete("/api/categories/1")
    assert 1 not in {c["code"] for c in client.get("/api/categories").get_json()}


def test_db_dirty_flag_and_backup(tmp_path):
    from gleapp import backup
    from gleapp.db import CaseDB

    c = open_case(tmp_path / "dcase", create=True, examiner="t")
    try:
        assert c.db.dirty is False
        fid = c.db.upsert_file("/a/b.jpg", kind="image")
        c.db.update_file(fid, category=0, notes="hi")
        assert c.db.dirty is True

        snap = backup.snapshot(c, "before-review")
        assert Path(snap.path).exists()
        assert "before-review" in snap.name
        assert c.db.dirty is False                       # cleared by backup

        # the snapshot is a real, openable case with the same data
        copy = CaseDB(snap.path)
        try:
            assert copy.get_file(fid)["notes"] == "hi"
        finally:
            copy.close()
    finally:
        c.close()


def test_backup_prune_keeps_recent(tmp_path):
    from gleapp import backup

    c = open_case(tmp_path / "pcase", create=True, examiner="t")
    try:
        for i in range(backup.KEEP + 5):
            c.db._touch()
            backup.snapshot(c)
        snaps = backup.list_snapshots(c)
        assert len(snaps) == backup.KEEP
    finally:
        c.close()


def test_report_scopes_and_md5(tmp_path, evidence):
    from gleapp import report

    c = open_case(tmp_path / "rep", create=True, examiner="t")
    try:
        f1 = c.db.upsert_file("/a/1.jpg", kind="image", md5="a" * 32, category=1)
        f2 = c.db.upsert_file("/a/2.jpg", kind="image", md5="b" * 32, category=0)
        c.db.upsert_file("/a/3.jpg", kind="image", md5="a" * 32, category=2)  # dup md5
        c.db.update_file(f2, reviewed=1)
        c.db.commit()

        # md5 list: header + distinct hashes only
        p = report.export_md5(c, tmp_path / "all.csv")
        lines = p.read_text().splitlines()
        assert lines[0] == "md5"
        assert sorted(lines[1:]) == ["a" * 32, "b" * 32]

        # categorized-only scope
        p = report.export_md5(c, tmp_path / "cat.csv", "category != 0")
        assert p.read_text().splitlines()[1:] == ["a" * 32]

        # CSV report honours the where filter
        p = report.export_csv(c, tmp_path / "r.csv", "reviewed = 1")
        import csv as _csv
        rows = list(_csv.DictReader(open(p, encoding="utf-8")))
        assert len(rows) == 1 and rows[0]["md5"] == "b" * 32
    finally:
        c.close()


def test_search_covers_all_metadata(tmp_path, evidence):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "sc"), "name": "SC"})
    c = app.config["STATE"]["case"]
    fid = c.db.upsert_file(
        "/evi/x.jpg", kind="image", rel_path="Files/deadbeef.jpg",
        md5="deadbeef" + "0" * 24, orig_name="IMG_2201.HEIC",
        orig_path="/DCIM/100APPLE/IMG_2201.HEIC", camera="Apple iPhone 14",
        created_dt="2024-07-15T09:30:00", mime="image/heic", notes="suspect A")
    c.db.add_tag(fid, "victim-B")
    c.db.commit()

    def n(q):
        return cl.get("/api/files?q=" + q).get_json()["total"]

    assert n("dcim") == 1              # orig_path
    assert n("IMG_2201") == 1          # orig_name
    assert n("deadbeef") == 1          # md5 (partial)
    assert n("2024-07") == 1           # created_dt
    assert n("iphone") == 1            # camera
    assert n("suspect") == 1          # notes
    assert n("victim-B") == 1          # tag
    assert n("dcim%20iphone") == 1     # two words, both must match
    assert n("dcim%20nope") == 0       # AND fails


def test_webapp_export_endpoints(tmp_path, evidence):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "we"), "name": "WE"})
    cl.post("/api/case/ingest", json={
        "sources": [{"name": "u", "path": str(evidence / "usb1")}],
        "options": {"keyframes": 2, "screen": False}})
    for _ in range(60):
        j = cl.get("/api/job").get_json()
        if not j["running"] and j["stage"] in ("done", "error"):
            break
        time.sleep(0.5)
    ids = [f["id"] for f in cl.get("/api/files?limit=3").get_json()["files"]]
    cl.post("/api/categorize", json={"ids": ids[:2], "category": 1})

    r = cl.post("/api/report", json={"format": ["csv"], "scope": "categorized"}).get_json()
    assert r["ok"] and r["scope"] == "categorized only"
    assert any("categorized" in w for w in r["written"])

    r = cl.post("/api/export/md5", json={"scope": "selected", "ids": ids}).get_json()
    assert r["ok"] and r["count"] >= 1 and Path(r["path"]).exists()
    assert open(r["path"], encoding="utf-8").readline().strip() == "md5"

    r = cl.post("/api/export/md5", json={"scope": "selected", "ids": []})
    assert r.status_code == 400


def test_webapp_snapshot_endpoint(tmp_path, evidence):
    from gleapp.web.app import create_app

    app = create_app(None)
    client = app.test_client()
    client.post("/api/case/create", json={"path": str(tmp_path / "scase"), "name": "S"})
    client.post("/api/categorize", json={"ids": [], "category": 0})  # touch
    r = client.post("/api/snapshot", json={"label": "manual test"}).get_json()
    assert r["ok"] and Path(r["path"]).exists()
    listed = client.get("/api/snapshots").get_json()
    assert any("manual-test" in s["name"] for s in listed)


def _make_vic(tmp_path, evidence):
    """A minimal Project VIC 2.0 file pointing at real sample images."""
    import json
    files_dir = tmp_path / "VIC_Files"
    files_dir.mkdir()
    media = []
    src_imgs = sorted((evidence / "usb1" / "DCIM").glob("*.png"))[:3]
    for i, src in enumerate(src_imgs, start=1):
        dst = files_dir / f"{src.stem}.png"
        dst.write_bytes(src.read_bytes())
        media.append({
            "MD5": "0" * 32, "MediaID": i, "Category": (1 if i == 1 else None),
            "SHA1": "", "MediaSize": dst.stat().st_size,
            "RelativeFilePath": f"VIC_Files\\{dst.name}",
            "MimeType": "image/png", "VictimIdentified": i == 1,
            "OffenderIdentified": False, "IsDistributed": False,
            "MediaFiles": [{"FileName": f"orig_{i}.png",
                            "FilePath": f"/DCIM/orig_{i}.png",
                            "Created": "2024-01-02T03:04:05+00:00"}],
        })
    # one entry whose file is missing
    media.append({
        "MD5": "f" * 32, "MediaID": 99, "Category": None, "SHA1": "",
        "RelativeFilePath": "VIC_Files\\gone.jpg", "MimeType": "image/jpeg",
        "MediaFiles": [],
    })
    doc = {
        "@odata.context": "http://github.com/VICSDATAMODEL/ProjectVic/DataModels/2.0.xml/US/$metadata#Cases",
        "value": [{"CaseID": "abc-123", "CaseNumber": "OP-VIC-1",
                   "SourceApplicationName": "UnitTest", "Media": media}],
    }
    p = tmp_path / "vic.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_projectvic_detect_and_import(tmp_path, evidence):
    from gleapp import projectvic
    from gleapp.pipeline import ingest_sources, process

    vic = _make_vic(tmp_path, evidence)
    assert projectvic.is_vic_file(vic)

    sources, _ = parse_source_spec(vic)
    assert len(sources) == 1 and sources[0].kind == "projectvic"

    c = open_case(tmp_path / "viccase", create=True, examiner="t")
    try:
        ingest_sources(c, sources)
        assert c.db.get_meta("vic_source_json") == str(vic.resolve())
        assert c.db.get_meta("vic_case_id") == "abc-123"

        rows = c.db.iter_files()
        assert len(rows) == 4                       # 3 present + 1 missing
        missing = [r for r in rows if r["error"]]
        assert len(missing) == 1 and missing[0]["media_id"] == 99

        first = c.db.iter_files("media_id = 1")[0]
        assert first["category"] == 1
        assert first["orig_name"] == "orig_1.png"
        assert first["mime"] == "image/png"
        assert json.loads(first["vic_flags"])["victim_identified"] is True
        # a category row was auto-created for VIC code 1 (blank name)
        assert c.db.get_category(1) is not None and c.db.get_category(1)["name"] == ""

        process(c, workers=2, screen=False)
        done = c.db.iter_files("media_id = 1")[0]
        assert done["phash"] and done["thumb"]       # processed
        assert done["created_dt"].startswith("2024-01-02")  # VIC date kept
    finally:
        c.close()


def test_projectvic_export_roundtrip(tmp_path, evidence):
    import json as _json
    from gleapp import projectvic
    from gleapp.pipeline import ingest_sources

    vic = _make_vic(tmp_path, evidence)
    sources, _ = parse_source_spec(vic)
    c = open_case(tmp_path / "vicexp", create=True, examiner="t")
    try:
        ingest_sources(c, sources)
        # examiner categorizes MediaID 2 and adds a note
        fid = c.db.iter_files("media_id = 2")[0]["id"]
        c.db.update_file(fid, category=3, notes="flagged by examiner")
        c.db.commit()

        out = tmp_path / "out.json"
        projectvic.export_vic(c, out)
        doc = _json.loads(out.read_text(encoding="utf-8"))
        by_id = {m["MediaID"]: m for m in doc["value"][0]["Media"]}
        assert by_id[1]["Category"] == 1            # unchanged
        assert by_id[2]["Category"] == 3            # examiner's
        assert by_id[2]["Comments"] == "flagged by examiner"
        assert by_id[99]["Category"] is None
    finally:
        c.close()


def test_lsh_clustering_scales(tmp_path):
    """cluster_near must not be O(n^2) on a few thousand rows."""
    import time as _t
    from gleapp import dedupe

    c = open_case(tmp_path / "big", create=True, examiner="t")
    try:
        with c.db.lock:
            for i in range(4000):
                h = f"{i:016x}"
                c.db.conn.execute(
                    "INSERT INTO files(path, phash, md5) VALUES(?,?,?)",
                    (f"/f/{i}", h, f"{i:032x}"))
            c.db.conn.commit()
        t0 = _t.time()
        dedupe.cluster_near(c.db, threshold=6)
        assert _t.time() - t0 < 15         # naive O(n^2) would be far slower
    finally:
        c.close()


def test_webapp_create_and_ingest(tmp_path, evidence):
    from gleapp.web.app import create_app

    app = create_app(None)
    client = app.test_client()
    r = client.post("/api/case/create",
                    json={"path": str(tmp_path / "webcase"), "name": "WebCase"})
    assert r.get_json()["ok"]
    r = client.post("/api/case/ingest", json={
        "sources": [{"name": "USB", "path": str(evidence / "usb1")}],
        "options": {"keyframes": 3, "screen": False},
    })
    assert r.get_json()["ok"]
    for _ in range(120):
        job = client.get("/api/job").get_json()
        if not job["running"] and job["stage"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert job["stage"] == "done", job
    assert client.get("/api/context").get_json()["stats"]["total"] > 0


def test_cannot_create_case_inside_another_case(tmp_path):
    from gleapp.web.app import create_app

    client = create_app(None).test_client()
    outer = tmp_path / "outer"
    assert client.post("/api/case/create",
                       json={"path": str(outer), "name": "Outer"}).get_json()["ok"]
    # a folder *inside* the outer case tree must be refused
    r = client.post("/api/case/create",
                    json={"path": str(outer / "reports"), "name": "Nested"})
    assert r.status_code == 400
    assert "existing case" in r.get_json()["message"].lower()


def test_recent_cases_hides_empty_and_annotates(tmp_path, evidence):
    from gleapp import appconfig
    from gleapp.web.app import create_app

    client = create_app(None).test_client()
    # an empty case: created but never ingested
    client.post("/api/case/create",
                json={"path": str(tmp_path / "ghost"), "name": "Ghost"})
    assert appconfig.recent_cases() == []          # not remembered - no files

    # a real case: created + ingested
    client.post("/api/case/create",
                json={"path": str(tmp_path / "real"), "name": "Real"})
    client.post("/api/case/ingest", json={
        "sources": [{"name": "USB", "path": str(evidence / "usb1")}],
        "options": {"keyframes": 2, "screen": False}})
    for _ in range(120):
        job = client.get("/api/job").get_json()
        if not job["running"] and job["stage"] in ("done", "error"):
            break
        time.sleep(0.5)
    recent = appconfig.recent_cases()
    assert [r["name"] for r in recent] == ["Real"]
    assert recent[0]["files"] > 0
