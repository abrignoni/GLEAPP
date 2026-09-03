import itertools
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
    """Keep tests out of the real %APPDATA%\\GLEAPP (recent cases, hash store)."""
    monkeypatch.setenv("GLEAPP_CONFIG_DIR",
                       str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash
    hashstore.close()          # drop any cached connection to a prior tmp dir
    stash.close()
    yield
    hashstore.close()
    stash.close()


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


def _write_cgbi_png(path, rows):
    """Minimal Apple CgBI PNG: BGRA byte order, premultiplied alpha, header-less
    (raw DEFLATE) IDAT. ``rows`` is a list of rows of (r, g, b, a) tuples."""
    import binascii
    import struct
    import zlib

    h, w = len(rows), len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)                                   # filter: None
        for (r, g, b, a) in row:
            raw += bytes((b * a // 255, g * a // 255, r * a // 255, a))
    comp = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    idat = comp.compress(bytes(raw)) + comp.flush()

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", binascii.crc32(typ + data) & 0xffffffff))

    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"CgBI", b"\x50\x00\x20\x02")
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))
    Path(path).write_bytes(blob)


def test_cgbi_png_decoded(tmp_path):
    from PIL import Image, ImageFile

    from gleapp import imaging

    p = tmp_path / "icon.png"
    _write_cgbi_png(p, [
        [(255, 0, 0, 255), (0, 255, 0, 255)],
        [(0, 0, 255, 128), (255, 255, 255, 0)],
    ])

    assert imaging.is_cgbi_png(p)

    # a standard PNG decoder renders it blank - the reason it "won't render"
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    assert Image.open(p).convert("RGB").getextrema() == ((0, 0),) * 3

    im = imaging.load_any(p)
    assert im.mode == "RGBA" and im.size == (2, 2)
    px = im.load()
    assert px[0, 0][:3] == (255, 0, 0) and px[0, 0][3] == 255
    assert px[1, 0][:3] == (0, 255, 0)
    r, g, b, a = px[0, 1]
    assert a == 128 and abs(b - 255) <= 2 and r <= 2 and g <= 2
    assert px[1, 1][3] == 0

    dest = tmp_path / "view.png"
    assert imaging.to_web_png(p, dest)
    out = Image.open(dest)
    out.load()
    assert out.size == (2, 2)


def test_cgbi_png_pipeline_thumbnail(tmp_path):
    from gleapp.web.app import create_app

    src = tmp_path / "src"
    src.mkdir()
    _write_cgbi_png(src / "circle.png", [[(255, 255, 255, 255)] * 4] * 4)

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "CG"})
    cl.post("/api/case/ingest", json={"spec": str(src),
                                      "options": {"screen": False, "keyframes": 0}})
    for _ in range(60):
        j = cl.get("/api/job").get_json()
        if not j["running"] and j["stage"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert j["stage"] == "done"

    cs = app.config["STATE"]["case"]
    row = cs.db.iter_files("kind='image'")[0]
    assert row["thumb"] and (cs.thumb_dir / row["thumb"]).exists()
    # the white icon must not have thumbnailed to black
    from PIL import Image
    thumb = Image.open(cs.thumb_dir / row["thumb"]).convert("L")
    assert min(thumb.getextrema()) > 200

    r = cl.get(f"/view/{row['id']}")
    assert r.status_code == 200 and r.mimetype == "image/png"


def test_thumbnail_encoder_is_deterministic(tmp_path):
    """The JPEG encoder must write the same bytes for the same pixels every time, and
    a flat white tile must decode white. Pillow 10.0.x and 10.1.0 wheels for macOS
    arm64 bundle a libjpeg-turbo 3.0.0 whose Huffman encoder flushes its bit buffer
    on an uninitialised flag, so roughly every other encode of a small image came out
    corrupt (a 16x16 white tile decoded to a grey half and noise); requirements.txt
    floors Pillow at 10.2, whose wheel carries the fixed 3.0.1. Forty encodes here
    turn that coin flip into a certain failure on an affected build."""
    from PIL import Image

    from gleapp import media

    tile = Image.new("RGB", (16, 16), (255, 255, 255))
    outputs = set()
    for i in range(40):
        name = media.make_image_thumb(tmp_path / f"white{i}.png", tmp_path, img=tile)
        assert name
        data = (tmp_path / name).read_bytes()
        outputs.add(data)
        with Image.open(tmp_path / name) as thumb:
            thumb.load()
            assert thumb.size == (16, 16)
            assert min(lo for lo, _hi in thumb.getextrema()) >= 250, i
    assert len(outputs) == 1, f"{len(outputs)} distinct encodings of one 16x16 white tile"


def test_hex_view_endpoint(tmp_path):
    from gleapp.web.app import create_app

    f = tmp_path / "blob.bin"
    f.write_bytes(bytes(range(256)) * 8)                 # 2048 bytes
    app = create_app(None)
    app.test_client().post("/api/case/create",
                           json={"path": str(tmp_path / "c"), "name": "H"})
    c = app.config["STATE"]["case"]
    fid = c.db.upsert_file(str(f), kind="other", rel_path="blob.bin")
    c.db.commit()
    cl = app.test_client()
    try:
        r = cl.get(f"/api/file/{fid}/hex?offset=32&length=48").get_json()
        assert r["size"] == 2048 and r["offset"] == 32
        assert bytes.fromhex(r["bytes"]) == (bytes(range(256)) * 8)[32:80]
        # missing original -> 410
        f.unlink()
        assert cl.get(f"/api/file/{fid}/hex").status_code == 410
    finally:
        c.close()


def test_process_sniffs_extensionless_other(tmp_path):
    """A Project VIC import hands over an extension-less image with an
    image/unknown MIME as kind='other'; processing must content-sniff it and
    render a thumbnail (regression: Instagram/app image caches stayed blank)."""
    from PIL import Image

    src = tmp_path / "00a0de589cce411a472c83e48fcee543"   # md5-named, no suffix
    Image.new("RGB", (48, 48), (200, 60, 60)).save(src, "JPEG")

    c = open_case(tmp_path / "sniffcase", create=True, examiner="t")
    try:
        c.db.upsert_file(str(src), kind="other", rel_path=src.name, ext="",
                         mime="image/unknown", md5="0" * 32,
                         orig_name="fcbaabf001d96b0b2ac152f6ec642b55")
        c.db.commit()
        process(c, workers=1, screen=False)
        r = c.db.iter_files(f"path = '{src}'")[0]
        assert r["kind"] == "image"
        assert r["thumb"] and (c.thumb_dir / r["thumb"]).exists()
        assert r["phash"] and not r["error"]
    finally:
        c.close()


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


def test_hashset_import_endpoint_flags_and_removes(tmp_path):
    """Import a CyberTip-style MD5 list via the web API, flag matches, remove it."""
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "ct"), "name": "CT"})
    c = app.config["STATE"]["case"]
    c.db.upsert_file("/e/hit.jpg", kind="image", md5="a" * 32, rel_path="hit.jpg")
    c.db.upsert_file("/e/miss.jpg", kind="image", md5="b" * 32, rel_path="miss.jpg")
    c.db.commit()

    # a plain list: header row, blank line, upper-case hash -> all tolerated
    lst = tmp_path / "cybertip_9999.txt"
    lst.write_text("MD5\n\n" + "A" * 32 + "\n" + "c" * 32 + "\n")
    r = cl.post("/api/hashset/import",
                json={"path": str(lst), "name": "CyberTip 9999", "kind": "known"}).get_json()
    assert r["ok"] and r["entries"] == 2
    for _ in range(100):
        if not cl.get("/api/job").get_json()["running"]:
            break
        time.sleep(0.02)

    rows = {x["rel_path"]: dict(x) for x in c.db.iter_files()}
    assert rows["hit.jpg"]["hashset_hit"] == "CyberTip 9999"
    assert rows["hit.jpg"]["hashset_kind"] == "known"
    assert rows["miss.jpg"]["hashset_hit"] is None

    sets = cl.get("/api/hashsets/case").get_json()
    assert len(sets) == 1 and sets[0]["hits"] == 1

    # "known-hash hit" filter surfaces only the flagged file
    files = cl.get("/api/files?hashset=1").get_json()["files"]
    assert [f["rel_path"] for f in files] == ["hit.jpg"]
    # filter by the named set, and by "any imported set"
    assert [f["rel_path"] for f in
            cl.get("/api/files?hashset_name=CyberTip 9999").get_json()["files"]] == ["hit.jpg"]
    assert [f["rel_path"] for f in
            cl.get("/api/files?hashset_name=*").get_json()["files"]] == ["hit.jpg"]

    # removal clears the flag immediately, even if a job happens to be running
    app.config["STATE"]["job"] = {"running": True, "stage": "process", "done": 0,
                                  "total": 0, "message": "x", "stats": None, "error": None}
    rr = cl.post("/api/hashset/remove", json={"id": r["id"]})
    assert rr.status_code == 200 and rr.get_json()["rematched"] is False
    assert dict(c.db.iter_files("rel_path='hit.jpg'")[0])["hashset_hit"] is None
    assert cl.get("/api/hashsets/case").get_json() == []


def test_local_hash_stash(tmp_path):
    from gleapp import hashdb, stash
    from gleapp.case import open_case

    c = open_case(tmp_path / "stashcase", create=True, examiner="t")
    try:
        c.db.upsert_file("/x/a.jpg", kind="image", md5="a" * 32, category=1)
        c.db.upsert_file("/x/b.jpg", kind="image", md5="b" * 32, category=3)
        c.db.upsert_file("/x/c.jpg", kind="image", md5="c" * 32, category=5)  # not notable
        c.db.upsert_file("/x/d.jpg", kind="image", md5="d" * 32, category=0)  # uncategorized
        c.db.commit()

        res = stash.add(
            (r["md5"], r["category"])
            for r in c.db.conn.execute("SELECT md5, category FROM files"))
        assert res["submitted"] == 2 and res["added"] == 2 and res["total"] == 2
        assert res["by_category"] == {1: 1, 3: 1}
        assert res["shared"] is False and res["path"].endswith("stash.gleapp")

        # re-stashing the same hash under a MORE severe category wins
        stash.add([("b" * 32, 2)])
        assert stash.summary()["by_category"] == {1: 1, 2: 1}

        # a future case sees the stashed hash as a 'known' hit that carries a category
        c2 = open_case(tmp_path / "future", create=True, examiner="t")
        try:
            fid = c2.db.upsert_file("/y/dup.jpg", kind="image", md5="a" * 32)
            c2.db.commit()
            hit = hashdb.match_file(c2.db, c2.db.get_file(fid))
            assert hit and hit["name"] == stash.STASH_NAME
            assert hit["kind"] == "known" and hit["category"] == 1
        finally:
            c2.close()

        # export -> merge round-trips into a fresh stash
        dump = tmp_path / "shared.csv"
        stash.export(dump)
        assert stash.clear() == 2 and stash.summary()["total"] == 0
        stash.merge(dump)
        assert stash.summary()["by_category"] == {1: 1, 2: 1}
    finally:
        c.close()


def test_stash_separate_file_and_shared_path(tmp_path):
    from gleapp import hashstore, stash

    # the stash file is NOT the global hash store
    assert stash.stash_path() != hashstore.store_path()
    stash.add([("a" * 32, 1)])
    assert stash.stash_path().exists()
    # and it is not registered as a set in the global store
    assert not any(s["name"] == stash.STASH_NAME for s in hashstore.sets())

    # point at a shared location
    shared = tmp_path / "team" / "stash.gleapp"
    stash.set_path(str(shared))
    assert stash.is_shared() and stash.summary()["total"] == 0   # fresh file
    stash.add([("b" * 32, 2)])
    assert shared.exists()
    stash.set_path("")                    # back to default
    assert not stash.is_shared()
    assert stash.summary()["by_category"] == {1: 1}              # original data intact


def test_stash_endpoints(tmp_path):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "S"})
    cdb = app.config["STATE"]["case"].db
    cdb.upsert_file("/x/a.jpg", kind="image", md5="a" * 32, category=2)
    cdb.upsert_file("/x/b.jpg", kind="image", md5="b" * 32, category=1)
    cdb.commit()

    st = cl.get("/api/stash").get_json()
    assert st["case"]["eligible"] == 2 and st["stash"]["total"] == 0

    r = cl.post("/api/stash/add").get_json()
    assert r["ok"] and r["added"] == 2 and r["total"] == 2

    st = cl.get("/api/stash").get_json()
    assert st["stash"]["by_category"] == {"1": 1, "2": 1}

    assert cl.post("/api/stash/clear").get_json()["removed"] == 2


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


def test_vic_presets_seeded_and_locked(tmp_path):
    from gleapp.db import VIC_PRESETS

    c = open_case(tmp_path / "cats", create=True, examiner="t")
    try:
        # every case ships with the locked Project VIC presets 0-5
        rows = {r["code"]: r for r in c.db.list_categories()}
        assert sorted(rows) == [0, 1, 2, 3, 4, 5]
        for code, name, color, notable in VIC_PRESETS:
            assert rows[code]["name"] == name
            assert rows[code]["color"] == color
            assert rows[code]["notable"] == notable
            assert rows[code]["locked"] == 1
        assert c.db.category_name(1) == "CAM (Child Abuse Material)"

        # presets can't be renamed, recolored or deleted
        with pytest.raises(ValueError):
            c.db.update_category(1, name="something else")
        with pytest.raises(ValueError):
            c.db.update_category(4, color="#000000")
        with pytest.raises(ValueError):
            c.db.delete_category(2)
        assert c.db.category_name(1) == "CAM (Child Abuse Material)"

        # the examiner's own categories start at code 6 and are editable
        code = c.db.add_category("")
        assert code == 6
        assert c.db.category_name(6) == "Category 6"   # fallback label
        c.db.update_category(6, name="Grooming set")
        assert c.db.category_name(6) == "Grooming set"

        c2 = c.db.add_category("Weapons")
        assert c2 == 7
        c.db.delete_category(7, reassign=True)
        assert c.db.get_category(7) is None
    finally:
        c.close()


def test_category_delete_keeps_label_when_in_use(tmp_path):
    c = open_case(tmp_path / "catdel", create=True, examiner="t")
    try:
        code = c.db.add_category("Temp")
        assert code == 6                           # after the locked presets
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
                 if not r["locked"]]
        assert order == [d, a, b]
        # locked presets keep their fixed 0-5 positions, ahead of custom ones
        allrows = c.db.list_categories(include_inactive=False)
        assert [r["code"] for r in allrows[:6]] == [0, 1, 2, 3, 4, 5]
    finally:
        c.close()


def test_category_migration_seeds_used_codes(tmp_path, evidence):
    """A v1-style case with a custom category code on files gets a placeholder
    row, and the locked VIC presets are back-filled on open."""
    from gleapp.db import CaseDB
    p = tmp_path / "old" / "case.gleapp"
    p.parent.mkdir(parents=True)
    db = CaseDB(p)
    db.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    db.upsert_file("/a/b.jpg", kind="image", category=9)   # examiner code, >5
    db.commit()
    db.close()
    db2 = CaseDB(p)                                # reopen -> migration runs
    try:
        from gleapp.db import SCHEMA_VERSION
        assert db2.get_meta("schema_version") == str(SCHEMA_VERSION)
        row = db2.get_category(9)
        assert row is not None and row["name"] == "" and not row["locked"]
        # presets were seeded + locked into the old case too
        p1 = db2.get_category(1)
        assert p1["name"] == "CAM (Child Abuse Material)" and p1["locked"] == 1
        # v3 additive columns exist after migration
        cols = {r["name"] for r in db2.conn.execute("PRAGMA table_info(files)")}
        assert {"media_id", "orig_name", "mime", "vic_flags"} <= cols
    finally:
        db2.close()


def test_named_preset_slot_in_old_case_is_kept(tmp_path):
    """If an older case already named code 3, opening it must NOT overwrite that
    with the VIC preset - the examiner's label stays, as an unlocked category."""
    from gleapp.db import CaseDB
    p = tmp_path / "named" / "case.gleapp"
    p.parent.mkdir(parents=True)
    db = CaseDB(p)
    db.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    db.conn.execute("UPDATE categories SET name='My scheme', locked=0 WHERE code=3")
    db.commit()
    db.close()
    db2 = CaseDB(p)
    try:
        row = db2.get_category(3)
        assert row["name"] == "My scheme" and not row["locked"]
        db2.update_category(3, name="still editable")   # no raise
        assert db2.category_name(3) == "still editable"
    finally:
        db2.close()


def test_timezone_setting(tmp_path):
    import datetime as _dt

    from gleapp import report, timeutil
    from gleapp.web.app import create_app

    # DST is honoured: same set of seconds, different offset by season
    jul = _dt.datetime(2024, 7, 1, 16, tzinfo=_dt.timezone.utc).timestamp()
    jan = _dt.datetime(2024, 1, 1, 16, tzinfo=_dt.timezone.utc).timestamp()
    assert timeutil.fmt_epoch(jul, "America/New_York").endswith("EDT")
    assert timeutil.fmt_epoch(jan, "America/New_York").endswith("EST")
    assert "12:00" in timeutil.fmt_epoch(jul, "America/New_York")
    assert timeutil.fmt_epoch(jul, "UTC").endswith("UTC")
    assert timeutil.is_known("America/New_York") and not timeutil.is_known("No/Where")

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "TZ"})
    assert cl.get("/api/context").get_json()["timezone"] == "UTC"

    assert cl.post("/api/settings", json={"timezone": "Xyz/Nope"}).status_code == 400
    r = cl.post("/api/settings", json={"timezone": "America/Chicago"}).get_json()
    assert r["ok"] and r["timezone"] == "America/Chicago"
    assert cl.get("/api/context").get_json()["timezone"] == "America/Chicago"

    # the report renders FS times in that zone; created_dt (EXIF) is untouched
    case = app.config["STATE"]["case"]
    case.db.upsert_file("/x/a.jpg", kind="image", md5="a" * 32,
                        mtime=jul, created_dt="2024:07:01 09:15:00")
    case.db.commit()
    dest = report.export_html(case, tmp_path / "r.html",
                              fields=["name", "created_dt", "mtime"], tz="America/Chicago")
    doc = dest.read_text(encoding="utf-8")
    assert "America/Chicago" in doc
    assert "2024-07-01 11:00 CDT" in doc          # 16:00 UTC -> 11:00 CDT
    assert "2024:07:01 09:15:00" in doc           # EXIF shown verbatim


def test_webapp_category_crud(tmp_path, evidence):
    from gleapp.web.app import create_app

    app = create_app(None)
    client = app.test_client()
    client.post("/api/case/create", json={"path": str(tmp_path / "wcc"), "name": "X"})
    cats0 = {c["code"]: c for c in client.get("/api/categories").get_json()}
    assert sorted(cats0) == [0, 1, 2, 3, 4, 5]
    assert cats0[1]["name"] == "CAM (Child Abuse Material)" and cats0[1]["locked"]

    # locked presets reject rename / delete
    assert client.patch("/api/categories/1", json={"name": "Renamed"}).status_code == 400
    assert client.delete("/api/categories/2").status_code == 400
    assert {c["code"] for c in client.get("/api/categories").get_json()} >= {1, 2}

    # the examiner's own category lands at code 6 and is editable
    made = client.post("/api/categories", json={"name": "Illicit"}).get_json()
    assert made["code"] == 6 and made["name"] == "Illicit" and not made["locked"]
    client.patch("/api/categories/6", json={"name": "Renamed", "color": "#123abc"})
    cats = {c["code"]: c for c in client.get("/api/categories").get_json()}
    assert cats[6]["name"] == "Renamed"
    assert cats[6]["color"] == "#123abc"
    client.delete("/api/categories/6")
    assert 6 not in {c["code"] for c in client.get("/api/categories").get_json()}


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


def test_list_view_sort_and_column_filters(tmp_path):
    import json as _json

    from gleapp.web.app import create_app

    app = create_app(None)
    app.test_client().post("/api/case/create",
                           json={"path": str(tmp_path / "lv"), "name": "L"})
    c = app.config["STATE"]["case"]
    c.db.upsert_file("/a/big.jpg", kind="image", rel_path="big.jpg",
                     size=9000, camera="Canon EOS", ext=".jpg", faces=2)
    c.db.upsert_file("/a/small.png", kind="image", rel_path="small.png",
                     size=100, camera="Nikon", ext=".png", faces=0)
    c.db.upsert_file("/a/clip.mp4", kind="video", rel_path="clip.mp4",
                     size=5000, ext=".mp4", faces=0)
    c.db.commit()
    cl = app.test_client()

    # sort by size desc
    r = cl.get("/api/files?sort=size&dir=desc").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["big.jpg", "clip.mp4", "small.png"]

    # column filter: camera contains "nik" (case-insensitive LIKE)
    cf = _json.dumps([{"col": "camera", "op": "contains", "val": "nik"}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["small.png"]

    # "Name" / "File path" filter on a folder-ingest file (no orig_name/orig_path):
    # must match the displayed fallback (rel_path / path), not the empty column
    c.db.upsert_file("/dcim/100APPLE/IMG_0042.jpg", kind="image",
                     rel_path="100APPLE/IMG_0042.jpg", ext=".jpg")
    c.db.commit()
    cf = _json.dumps([{"col": "name", "op": "contains", "val": "IMG_"}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["100APPLE/IMG_0042.jpg"]
    cf = _json.dumps([{"col": "file_path", "op": "contains", "val": "dcim"}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["100APPLE/IMG_0042.jpg"]
    # and sorting by the virtual "name" column is accepted
    assert cl.get("/api/files?sort=name&dir=asc").status_code == 200

    # numeric range: size between 1000 and 8000
    cf = _json.dumps([{"col": "size", "op": "min", "val": 1000},
                      {"col": "size", "op": "max", "val": 8000}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert {f["rel_path"] for f in r["files"]} == {"clip.mp4"}

    # enum: kind = video
    cf = _json.dumps([{"col": "kind", "op": "eq", "val": "video"}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["clip.mp4"]

    # unknown column is ignored, not an error
    cf = _json.dumps([{"col": "evil; DROP", "op": "contains", "val": "x"}])
    assert cl.get(f"/api/files?colfilters={cf}").status_code == 200

    # GPS: substring match on a decimal column, and "has any value"
    c.db.update_file(1, gps_lat=45.4215)
    c.db.commit()
    cf = _json.dumps([{"col": "gps_lat", "op": "contains", "val": "45"}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["big.jpg"]
    cf = _json.dumps([{"col": "gps_lat", "op": "set"}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["big.jpg"]

    # date range on a filesystem-time column
    c.db.update_file(2, ctime=1717200000.0)   # 2024-06-01 UTC
    c.db.commit()
    cf = _json.dumps([{"col": "ctime", "op": "min", "val": 1717200000},
                      {"col": "ctime", "op": "max", "val": 1717286399}])
    r = cl.get(f"/api/files?colfilters={cf}").get_json()
    assert [f["rel_path"] for f in r["files"]] == ["small.png"]
    c.close()


def test_report_name_prefers_original(tmp_path):
    from gleapp import report
    from gleapp.case import open_case

    c = open_case(tmp_path / "rn", create=True, examiner="t")
    try:
        c.db.upsert_file("/store/deadbeef.jpg", kind="image", rel_path="deadbeef.jpg",
                         orig_name="IMG_0007.HEIC", md5="deadbeef")
        c.db.upsert_file("/store/plain.jpg", kind="image", rel_path="holiday.jpg")
        c.db.commit()
        rows = {report._disk_name(r): r for r in report._rows(c)}
        assert report._disp_name(rows["deadbeef.jpg"]) == "IMG_0007.HEIC"
        assert report._disp_name(rows["holiday.jpg"]) == "holiday.jpg"   # fallback
        assert report._FIELD_DEFS["name"][1](rows["deadbeef.jpg"]) == "IMG_0007.HEIC"
        assert report._FIELD_DEFS["disk_name"][1](rows["deadbeef.jpg"]) == "deadbeef.jpg"

        dest = report.export_html(c, tmp_path / "r.html")
        assert "IMG_0007.HEIC" in dest.read_text(encoding="utf-8")
    finally:
        c.close()


def test_ingest_progress_commits_incrementally(tmp_path):
    """Files are visible (committed) during the scan, not only at the end —
    this is what lets the gallery show files as they come in."""
    from PIL import Image

    from gleapp.case import Source, open_case
    from gleapp.pipeline import ingest_sources

    ev = tmp_path / "ev"
    ev.mkdir()
    for i in range(450):
        Image.new("RGB", (8, 8), (i % 255, 0, 0)).save(ev / f"p{i:03}.png")

    c = open_case(tmp_path / "c", create=True, examiner="t")
    try:
        seen = []
        # a reader on the same connection sees committed rows mid-scan
        ingest_sources(c, [Source(kind="folder", name="ev", path=str(ev))],
                       progress=lambda n: seen.append(
                           (n, c.db.conn.execute(
                               "SELECT COUNT(*) c FROM files").fetchone()["c"])))
        assert seen and seen[0][0] == 200          # first callback at 200 files
        assert seen[0][1] >= 200                    # …and they are already in the DB
        assert c.db.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 450
    finally:
        c.close()


def test_inline_screening_marks_case_screened(case):
    """process(screen=True) must record the screening pass so the UI stops
    offering 'Run screening' for an already-screened collection."""
    assert case.db.get_meta("screened_at") is not None

    # and the /api/context heuristic still reports done even without the meta
    case.db.conn.execute("DELETE FROM meta WHERE key='screened_at'")
    case.db.commit()
    from gleapp.web.app import create_app
    app = create_app(str(case.root))
    try:
        ctx = app.test_client().get("/api/context").get_json()
        assert ctx["screening"]["done"] is True     # inferred from skin_ratio
    finally:
        app.config["STATE"]["case"].close()


def test_context_reports_running_job(tmp_path):
    from gleapp.web.app import create_app

    app = create_app(None)
    cl = app.test_client()
    cl.post("/api/case/create", json={"path": str(tmp_path / "c"), "name": "J"})
    ctx = cl.get("/api/context").get_json()
    assert "job" in ctx and ctx["job"]["running"] is False
    # a job in flight blocks closing the case
    app.config["STATE"]["job"]["running"] = True
    assert cl.post("/api/case/close").status_code == 409
    app.config["STATE"]["job"]["running"] = False


def test_snapshot_restore_endpoint(tmp_path):
    from gleapp.web.app import create_app

    app = create_app(None)
    app.test_client().post("/api/case/create",
                           json={"path": str(tmp_path / "c"), "name": "R"})
    c = app.config["STATE"]["case"]
    fid = c.db.upsert_file("/x/y.jpg", kind="image")
    c.db.update_file(fid, category=1, notes="original")
    c.db.commit()

    cl = app.test_client()
    snap = cl.post("/api/snapshot", json={"label": "good"}).get_json()
    assert snap["ok"] and "good" in snap["name"]

    # mutate past the snapshot
    app.config["STATE"]["case"].db.update_file(fid, category=5, notes="changed")
    app.config["STATE"]["case"].db.commit()

    r = cl.post("/api/snapshot/restore", json={"name": snap["name"]}).get_json()
    assert r["ok"] and r["restored"] == snap["name"]

    restored = app.config["STATE"]["case"]
    row = restored.db.get_file(fid)
    assert row["notes"] == "original" and row["category"] == 1

    # the pre-restore safety snapshot exists
    names = [s.name for s in __import__("gleapp.backup", fromlist=["x"]).list_snapshots(restored)]
    assert any("pre-restore" in n for n in names)

    # bad name is rejected
    assert cl.post("/api/snapshot/restore",
                   json={"name": "../evil.gleapp"}).status_code == 400
    restored.close()


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


def _use_creeping_clock(monkeypatch, backup, step_us=100):
    """Point ``backup`` at a clock that advances by less than a millisecond.

    Whether real snapshots land inside one millisecond depends on how fast the
    machine is, so driving these tests from the real clock would make them pass
    or fail by hardware. A 100 microsecond step puts ten reads in every
    millisecond on every machine.

    The step is deliberately non-zero rather than a clock frozen solid. Frozen
    would only prove the equal case is handled and would miss a stamp source
    that compares raw readings, since two readings 100 microseconds apart do
    differ while still formatting to the same millisecond string.
    """
    import datetime as real_dt

    start = real_dt.datetime(2020, 1, 1, 12, 0, 0, 500000)
    step = real_dt.timedelta(microseconds=step_us)
    reads = itertools.count()

    class _CreepingDateTime:
        @staticmethod
        def now():
            return start + step * next(reads)

    class _CreepingClockModule:
        datetime = _CreepingDateTime
        timedelta = real_dt.timedelta

    monkeypatch.setattr(backup, "_dt", _CreepingClockModule)
    # The class carries the last stamp handed out, so a previous test's real
    # timestamps would otherwise decide where this one starts.
    monkeypatch.setattr(backup._Clock, "_last", None)  # pylint: disable=protected-access


def test_snapshot_stamps_never_repeat_under_a_creeping_clock(monkeypatch):
    """Every stamp must be a millisecond string this process has not used.

    _claim_dest retries when a name is already taken, so a stamp source that can
    repeat is survivable, but only by spending attempts. A clock advancing more
    slowly than the retry loop runs would spend the whole budget inside one
    millisecond and the snapshot would fail outright. Making each call return a
    new string keeps that loop making progress by construction rather than by
    hoping the wall clock outpaces it.
    """
    from gleapp import backup

    _use_creeping_clock(monkeypatch, backup)

    stamps = [backup._Clock.stamp()  # pylint: disable=protected-access
              for _ in range(50)]
    assert len(set(stamps)) == len(stamps), f"stamp repeated: {stamps}"


def test_snapshots_in_one_millisecond_do_not_overwrite(tmp_path, monkeypatch):
    """Snapshot names must stay distinct when several land in one millisecond."""
    from gleapp import backup
    from gleapp.db import CaseDB

    _use_creeping_clock(monkeypatch, backup)

    c = open_case(tmp_path / "mscase", create=True, examiner="t")
    try:
        fid = c.db.upsert_file("/a/b.jpg", kind="image")
        c.db.update_file(fid, category=0, notes="hi")

        taken = []
        for _ in range(5):
            c.db._touch()  # pylint: disable=protected-access
            taken.append(backup.snapshot(c))

        names = [s.name for s in taken]
        assert len(set(names)) == len(names), f"snapshot names collided: {names}"
        assert len(backup.list_snapshots(c)) == len(names)

        # Every one is a real case rather than a truncated or empty file.
        for s in taken:
            assert Path(s.path).is_file()
            assert s.size > 0
            copy = CaseDB(s.path)
            try:
                assert copy.get_file(fid)["notes"] == "hi"
            finally:
                copy.close()
    finally:
        c.close()


def test_report_scopes_and_md5(tmp_path, evidence):
    from gleapp import report

    c = open_case(tmp_path / "rep", create=True, examiner="t")
    try:
        c.db.upsert_file("/a/1.jpg", kind="image", md5="a" * 32, category=1)
        c.db.upsert_file("/a/2.jpg", kind="image", md5="b" * 32, category=0)
        c.db.upsert_file("/a/3.jpg", kind="image", md5="a" * 32, category=2)  # dup md5
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
        p = report.export_csv(c, tmp_path / "r.csv", "category = 0")
        import csv as _csv
        rows = list(_csv.DictReader(open(p, encoding="utf-8")))
        assert len(rows) == 1 and rows[0]["md5"] == "b" * 32
    finally:
        c.close()


def test_html_report_header_fields_grouping(tmp_path, evidence):
    from gleapp import report

    c = open_case(tmp_path / "hrep", create=True, examiner="Examiner X")
    try:
        from PIL import Image
        img = tmp_path / "one.jpg"
        Image.new("RGB", (2600, 1400), (30, 90, 160)).save(img, "JPEG")
        (c.thumb_dir / "one.jpg").parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (150, 150), (30, 90, 160)).save(c.thumb_dir / "t1.jpg")

        c.db.set_meta("case_name", "Op Test")
        c.db.upsert_file(str(img), kind="image", rel_path="a/one.jpg",
                         thumb="t1.jpg", md5="a" * 32, sha256="c" * 64,
                         created_dt="2024-01-02T03:04:05",
                         camera="Apple iPhone 14", category=1)
        c.db.upsert_file("/a/two.jpg", kind="image", rel_path="a/two.jpg",
                         md5="b" * 32, category=5)
        c.db.upsert_file("/a/three.jpg", kind="image", rel_path="a/three.jpg",
                         md5="d" * 32, category=0)
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 400)
        Image.new("RGB", (200, 150), (0, 0, 0)).save(c.thumb_dir / "vk.jpg")
        vfid = c.db.upsert_file(str(vid), kind="video", rel_path="a/clip.mp4",
                                thumb="vk.jpg", md5="e" * 32, category=1)
        c.db.commit()

        header = {"agency": "County SO", "case_number": "24-123",
                  "item_number": "1A", "notes": "line one\nline two"}
        p = report.export_html(c, tmp_path / "r.html", header=header,
                               fields=["name", "md5", "camera"],
                               scope_label="all files")
        doc = p.read_text(encoding="utf-8")

        assert "County SO" in doc and "24-123" in doc and "1A" in doc
        assert "line one<br>line two" in doc
        assert "class='summary'" in doc and "Files in this report" in doc
        assert "Total media in the case" in doc and "By category" in doc
        assert "one.jpg" in doc and "a" * 32 in doc and "Apple iPhone 14" in doc
        assert "c" * 64 not in doc                        # sha256 not picked

        # grouped by category with a clickable TOC, uncategorized last
        assert "<nav class='toc'>" in doc
        assert "id='cat-1'" in doc and "id='cat-5'" in doc and "id='cat-0'" in doc
        assert "href='#cat-1'" in doc                     # TOC anchor
        assert doc.index("id='cat-1'") < doc.index("id='cat-0'")
        # metadata collapsed by default; dark + blur toggles; blur on by default
        assert "<details class='meta'><summary>one.jpg</summary>" in doc
        assert "<details class='meta' open" not in doc
        assert 'id=\'btnDark\'' in doc and 'id=\'btnBlur\'' in doc
        assert '<html class="blur">' in doc
        assert "html.blur .card img{filter:blur" in doc
        assert "Scope:" not in doc
        # full-size image embedded + openable; video embedded as a playable blob
        assert "data-full=" in doc
        assert f"data-video='v{vfid}'" in doc
        assert f"<script type='text/plain' id='v{vfid}'>data:video/mp4;base64," in doc

        # thumbnails-only report has no full-size / video payload
        p2 = report.export_html(c, tmp_path / "r2.html", "category = 1",
                                full_images=False, full_videos=False)
        d2 = p2.read_text(encoding="utf-8")
        assert "data-full=" not in d2 and "data-video=" not in d2
        assert "type='text/plain'" not in d2
        assert "class='rimg video'" in d2          # still shows the key-frame thumb

        # scope = specific categories
        from gleapp.web.app import create_app
        app = create_app(None)
        app.config["STATE"]["case"] = c
        cl = app.test_client()
        r = cl.post("/api/report", json={"format": ["csv"], "scope": "categories",
                                         "categories": [1, 5]}).get_json()
        assert r["ok"]
        import csv as _csv
        rows = list(_csv.DictReader(open(
            Path(r["dir"]) / "report_selection.csv", encoding="utf-8")))
        assert {x["md5"] for x in rows} == {"a" * 32, "b" * 32, "e" * 32}  # cats 1+5, not 3

        # header + fields still round-trip as case prefs
        cl.post("/api/report", json={"format": ["html"], "scope": "all",
                                     "report_header": {"agency": "County SO"},
                                     "fields": ["name", "sha256"]})
        prefs = cl.get("/api/report/prefs").get_json()
        assert prefs["header"]["agency"] == "County SO"
        assert prefs["fields"] == ["name", "sha256"]
    finally:
        c.close()


def test_collapse_matches_groups_via_any_member(tmp_path, evidence):
    """Collapse + an attribute filter must still find a stack when only a
    non-head member carries that attribute (regression: Has-GPS + collapse
    hid whole stacks whose head had no EXIF GPS)."""
    from gleapp.web.app import create_app

    c = open_case(tmp_path / "clp", create=True, examiner="t")
    a = c.db.upsert_file("/x/a.jpg", kind="image", md5="a" * 32)
    b = c.db.upsert_file("/x/b.jpg", kind="image", md5="b" * 32,
                         gps_lat=51.5, gps_lon=-0.12)
    d3 = c.db.upsert_file("/x/c.jpg", kind="image", md5="c" * 32)
    for fid in (a, b, d3):                       # one visual stack, head = a
        c.db.update_file(fid, vstack_id=a)
    solo = c.db.upsert_file("/x/d.jpg", kind="image", md5="d" * 32,
                            gps_lat=40.0, gps_lon=-74.0)
    c.db.commit()

    app = create_app(None)
    app.config["STATE"]["case"] = c
    cl = app.test_client()

    assert cl.get("/api/files?has_gps=1").get_json()["total"] == 2   # b + solo
    coll = cl.get("/api/files?has_gps=1&dupes=collapse").get_json()
    assert coll["total"] == 2                    # the stack (via b) + solo
    ids = {f["id"] for f in coll["files"]}
    assert solo in ids and b in ids             # stack represented by its GPS member
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
    # some exporters write UTF-8 *with* a BOM - GLEAPP must still parse it
    p.write_text(json.dumps(doc), encoding="utf-8-sig")
    return p


def test_projectvic_json_with_bom(tmp_path, evidence):
    """A Project VIC JSON saved as UTF-8-with-BOM parses without a
    'Unexpected UTF-8 BOM' JSONDecodeError."""
    from gleapp import projectvic

    vic = _make_vic(tmp_path, evidence)
    assert vic.read_bytes()[:3] == b"\xef\xbb\xbf"      # BOM really is there
    assert projectvic.is_vic_file(vic)
    doc = projectvic.load(vic)
    recs = list(projectvic.iter_records(doc, json_dir=vic.parent))
    assert len(recs) == 4


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
        # VIC code 1 resolves to the locked preset name
        assert c.db.get_category(1)["name"] == "CAM (Child Abuse Material)"
        assert c.db.get_category(1)["locked"] == 1

        # the VIC "Created" filesystem time lands in ctime, not created_dt
        pre = c.db.iter_files("media_id = 1")[0]
        import datetime as _dt
        want = _dt.datetime.fromisoformat("2024-01-02T03:04:05+00:00").timestamp()
        assert pre["ctime"] == want
        assert pre["created_dt"] is None             # no EXIF capture date

        process(c, workers=2, screen=False)
        done = c.db.iter_files("media_id = 1")[0]
        assert done["phash"] and done["thumb"]       # processed
        assert done["ctime"] == want                 # FS time preserved through processing

        # search matches the device name/path, not the local extraction folder
        from gleapp.web.app import create_app
        app = create_app(str(c.root))
        cl = app.test_client()

        def q(term):
            return {f["media_id"] for f in
                    cl.get(f"/api/files?q={term}").get_json()["files"]}

        assert q("VIC_Files") == set()          # the local media folder: no hits
        assert q("DCIM") == {1, 2, 3}           # MediaFiles.FilePath
        assert q("orig_2") == {2}               # MediaFiles.FileName
        app.config["STATE"]["case"].close()
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
