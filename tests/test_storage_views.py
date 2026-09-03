"""One file under several Android storage views is registered once, with the other
spellings kept on the row, so duplicate stacking only sees real copies."""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from gleapp import storage_views
from gleapp.case import open_case, parse_source_spec
from gleapp.pipeline import ingest_sources, process


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))
    from gleapp import hashstore, stash
    hashstore.close()
    stash.close()
    yield
    hashstore.close()
    stash.close()


def _jpg(color):
    buf = io.BytesIO()
    Image.new("RGB", (48, 36), color).save(buf, "JPEG")
    return buf.getvalue()


A = _jpg((200, 40, 40))
B = _jpg((40, 200, 40))
# the same picture with one trailing byte: different size and CRC, so the copies differ.
# (A solid colour one level off quantises to the identical JPEG, which is how the first
# version of this fixture accidentally agreed.)
B2 = B + b"\x00"
C = _jpg((40, 40, 200))
D = _jpg((90, 90, 90))
A10 = _jpg((20, 120, 60))         # the second user's own file
ADE = _jpg((150, 90, 20))         # the device-encrypted file, another file again

# (member, bytes): three views of one app file, a mirrored group whose copies differ,
# a second Android user, device-encrypted storage, three views of shared storage, and
# one real duplicate (the same bytes at two unrelated paths).
MEMBERS = [
    ("Dump/data/data/com.app/files/a.jpg", A),
    ("Dump/data/user/0/com.app/files/a.jpg", A),
    ("Dump/data_mirror/data_ce/null/0/com.app/files/a.jpg", A),
    ("Dump/data/data/com.app/files/b.jpg", B),
    ("Dump/data/user/0/com.app/files/b.jpg", B2),
    ("Dump/data/user/10/com.app/files/a.jpg", A10),
    ("Dump/data/user_de/0/com.app/files/a.jpg", ADE),
    ("Dump/data/media/0/DCIM/c.jpg", C),
    ("Dump/storage/emulated/0/DCIM/c.jpg", C),
    ("Dump/mnt/user/0/emulated/0/DCIM/c.jpg", C),
    ("Dump/data/media/0/DCIM/d.jpg", D),
    ("Dump/data/media/0/Download/d.jpg", D),
]
KEPT = {
    "Dump/data/data/com.app/files/a.jpg",           # of the three ce views, data/data is kept
    "Dump/data/data/com.app/files/b.jpg",           # kept apart: the copies differ
    "Dump/data/user/0/com.app/files/b.jpg",
    "Dump/data/user/10/com.app/files/a.jpg",        # another user is another file
    "Dump/data/user_de/0/com.app/files/a.jpg",      # device encrypted storage is another file
    "Dump/data/media/0/DCIM/c.jpg",                 # of the three shared-storage views, data/media
    "Dump/data/media/0/DCIM/d.jpg",
    "Dump/data/media/0/Download/d.jpg",             # a real duplicate stays a row
}


def _zip(tmp_path):
    z = tmp_path / "EXTRACTION_FFS.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in MEMBERS:
            zf.writestr(member, data)
    return z


def _tar(tmp_path):
    t = tmp_path / "EXTRACTION_FFS.tar"
    with tarfile.open(t, "w") as tf:
        for member, data in MEMBERS:
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mtime = 1_700_000_000
            tf.addfile(info, io.BytesIO(data))
    return t


def _ingest(tmp_path, arc, name, *, stage=False):
    c = open_case(tmp_path / name, create=True, examiner="t")
    sources, _ = parse_source_spec(arc)
    sources[0].stage = stage
    n = ingest_sources(c, sources)
    return c, n


def test_canonical_keys_follow_aleapp_and_add_shared_storage():
    k = storage_views.canonical
    same = {k("Dump/data/data/com.app/f/x")[0], k("Dump/data/user/0/com.app/f/x")[0],
            k("Dump/data_mirror/data_ce/null/0/com.app/f/x")[0]}
    assert len(same) == 1
    assert k("Dump/data/user_de/0/com.app/f/x")[0] not in same        # de is not ce
    assert k("Dump/data/user/10/com.app/f/x")[0] not in same          # user 10 is not user 0
    shared = {k("data/media/0/DCIM/x")[0], k("storage/emulated/0/DCIM/x")[0],
              k("mnt/user/0/emulated/0/DCIM/x")[0], k("sdcard/DCIM/x")[0]}
    assert len(shared) == 1
    assert k("private/var/mobile/Media/DCIM/x") is None               # iOS has no views
    assert k("Dump/data/data/com.app/f/x")[1] < k("Dump/data/user/0/com.app/f/x")[1]


def test_plan_collapses_agreeing_groups_only():
    entries = [(m, len(d), hash(d) & 0xFFFFFFFF) for m, d in MEMBERS]
    alts, drop, differ = storage_views.plan(entries)
    assert differ == 1                                                  # the b.jpg group
    assert set(alts) == {"Dump/data/data/com.app/files/a.jpg", "Dump/data/media/0/DCIM/c.jpg"}
    assert sorted(alts["Dump/data/media/0/DCIM/c.jpg"]) == [
        "Dump/mnt/user/0/emulated/0/DCIM/c.jpg", "Dump/storage/emulated/0/DCIM/c.jpg"]
    assert drop == {m for m, _ in MEMBERS} - KEPT


@pytest.mark.parametrize("build", [_zip, _tar], ids=["zip", "tar"])
def test_mirrors_register_once_and_stack_only_real_copies(tmp_path, build):
    arc = build(tmp_path)
    c, n = _ingest(tmp_path, arc, "case", stage=True)
    try:
        rows = {r["orig_path"]: r for r in c.db.iter_files()}
        assert n == len(KEPT) and set(rows) == KEPT, sorted(rows)
        key = f"archive:{arc.name}"
        assert c.db.get_meta(f"{key}:mirrored") == "4"                 # 2 a.jpg views + 2 c.jpg views
        assert c.db.get_meta(f"{key}:views_differ") == "1"
        a = rows["Dump/data/data/com.app/files/a.jpg"]
        assert sorted(json.loads(a["alt_paths"])) == [
            "Dump/data/user/0/com.app/files/a.jpg",
            "Dump/data_mirror/data_ce/null/0/com.app/files/a.jpg"]
        assert rows["Dump/data/media/0/DCIM/d.jpg"]["alt_paths"] is None
        assert all(Path(r["path"]).is_file() for r in rows.values())   # kept rows are staged
        staged = list(c.staged_dir.rglob("*.jpg"))
        assert len(staged) == len(KEPT)                                 # and nothing else is
        stats = process(c, workers=2, keyframes=2, screen=False)
        assert stats.redundant_duplicates == 1                          # d.jpg twice, nothing else
        stacked = [r for r in c.db.iter_files() if r["stack_id"] and r["stack_id"] != r["id"]]
        assert [r["orig_path"] for r in stacked] == ["Dump/data/media/0/Download/d.jpg"]
    finally:
        c.close()


def test_search_finds_a_file_by_a_folded_view(tmp_path):
    z = _zip(tmp_path)
    c, _ = _ingest(tmp_path, z, "case")
    c.close()
    from gleapp.web.app import create_app
    app = create_app(str(tmp_path / "case"))
    app.config["TESTING"] = True
    client, state = app.test_client(), app.config["STATE"]
    try:
        hit = client.get("/api/files?q=data_mirror").get_json()
        assert hit["total"] == 1
        f = hit["files"][0]
        assert f["orig_path"] == "Dump/data/data/com.app/files/a.jpg"
        assert "data_mirror" in f["alt_paths"]
    finally:
        state["close_current"]()
