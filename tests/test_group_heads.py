"""The stored duplicate-group representative behind the fast collapsed gallery.

"Collapse duplicates" shows one tile per group, COALESCE(vstack_id, stack_id, id),
the lowest id in it. The default gallery reads that from ``files.grp_head``
instead of working it out per request, so these check it always agrees with the
query it replaced, and that the database marks it stale on every change to group
membership, whoever makes it.
"""

import shutil

import pytest
from PIL import Image

from gleapp.case import Source, open_case
from gleapp.pipeline import ingest_sources, process

GRP = "COALESCE(vstack_id, stack_id, id)"


@pytest.fixture(autouse=True)
def _isolate_appconfig(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("gleapp-cfg")))


def _case_with_duplicates(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir()
    for i in range(6):
        Image.new("RGB", (64, 64), (i * 40, 255 - i * 40, 120)).save(ev / f"img{i}.png")
    for i in (0, 1, 2):                       # exact copies: three exact stacks
        shutil.copy2(ev / f"img{i}.png", ev / f"copy{i}.png")
    case = open_case(tmp_path / "case", create=True, examiner="t")
    ingest_sources(case, [Source(name="ev", path=str(ev))])
    process(case, workers=1, keyframes=2, screen=False)
    return case


def _reference(db) -> list[int]:
    """The representative per group, worked out the way the gallery used to."""
    return [r[0] for r in db.conn.execute(
        f"SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY {GRP} ORDER BY id) "
        "AS rn FROM files WHERE kind != 'archive') WHERE rn = 1 ORDER BY id")]


def _stored(db) -> list[int]:
    return [r[0] for r in db.conn.execute(
        "SELECT id FROM files WHERE grp_head = 1 AND kind != 'archive' ORDER BY id")]


def _state(db) -> str:
    return db.conn.execute("SELECT value FROM meta WHERE key = 'grp_heads'").fetchone()[0]


def test_processing_stores_the_representatives(tmp_path):
    case = _case_with_duplicates(tmp_path)
    try:
        assert case.db.group_heads_fresh()
        assert _stored(case.db) == _reference(case.db)
        assert len(_stored(case.db)) < case.db.conn.execute(
            "SELECT COUNT(*) FROM files").fetchone()[0]      # the copies did collapse
    finally:
        case.close()


def test_any_change_to_group_membership_marks_them_stale(tmp_path):
    case = _case_with_duplicates(tmp_path)
    db = case.db
    try:
        heads = _stored(db)
        changes = [
            ("UPDATE files SET vstack_id = ? WHERE id IN (?, ?)", (heads[0], heads[0], heads[1])),
            ("UPDATE files SET stack_id = NULL WHERE id = ?", (heads[2],)),
            ("DELETE FROM files WHERE id = ?", (heads[3],)),
        ]
        for sql, args in changes:
            db.refresh_group_heads()
            assert _state(db) == "fresh"
            db.conn.execute(sql, args)
            db.conn.commit()
            assert _state(db) == "stale", sql
            db.refresh_group_heads()
            assert _stored(db) == _reference(db), sql
        # a change that cannot move a group leaves them fresh
        db.conn.execute("UPDATE files SET notes = 'x' WHERE id = ?", (heads[0],))
        db.conn.commit()
        assert _state(db) == "fresh"
    finally:
        case.close()


def test_the_collapsed_gallery_matches_the_query_it_replaced(tmp_path):
    from gleapp.web.app import _col_sql, create_app

    case = _case_with_duplicates(tmp_path)
    root = case.root
    case.close()
    client = create_app(str(root)).test_client()
    ref = open_case(root).db
    try:
        for col, direction in (("file_path", "asc"), ("file_path", "desc"), ("size", "desc")):
            expr, d = _col_sql(col), direction.upper()
            want = [r[0] for r in ref.conn.execute(
                f"SELECT id FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY {GRP} ORDER BY id) "
                f"AS rn FROM files WHERE kind != 'archive') WHERE rn = 1 "
                f"ORDER BY {expr} {d}, id {d}")]
            got = client.get(f"/api/files?dupes=collapse&sort={col}&dir={direction}"
                             "&limit=500").get_json()
            assert [f["id"] for f in got["files"]] == want, col
            assert got["total"] == len(want)
            assert isinstance(got["server_ms"], int)
    finally:
        ref.close()


def test_per_row_key_frame_lookups_use_an_index(tmp_path):
    """Each gallery row checks for key frames by file_id. Unindexed, that read the
    whole key-frame table once per row: 8 s for a page of 1,500 on a case with
    150,000 frames."""
    case = open_case(tmp_path / "kcase", create=True, examiner="t")
    try:
        plan = " ".join(str(r[-1]) for r in case.db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM keyframes WHERE file_id = ? LIMIT 1", (1,)))
        assert "USING" in plan and "INDEX" in plan, plan
    finally:
        case.close()


def test_a_cached_count_never_outlives_a_change(tmp_path):
    """The gallery keeps how many rows match per query, since a sort, page or page
    size change does not alter it. Any write to the case must drop it: after a file
    is categorized the Uncategorized total has to equal a fresh count at once."""
    from gleapp.web.app import create_app

    case = _case_with_duplicates(tmp_path)
    root = case.root
    case.close()
    client = create_app(str(root)).test_client()
    ref = open_case(root).db
    fresh = {
        "&dupes=collapse": f"SELECT COUNT(DISTINCT {GRP}) FROM files "
                           "WHERE kind != 'archive' AND category = 0",
        "": "SELECT COUNT(*) FROM files WHERE kind != 'archive' AND category = 0",
    }
    try:
        for collapse, sql in fresh.items():
            base = f"/api/files?category=0&sort=file_path{collapse}"
            before = client.get(base + "&limit=200").get_json()
            assert client.get(base + "&limit=1500").get_json()["total"] == before["total"]
            # a file alone in its group, so categorizing it must change the count
            target = next(f["id"] for f in before["files"]
                          if f["stack_count"] == 1 and not f["vstack_count"])
            assert client.post("/api/categorize",
                               json={"ids": [target], "category": 5}).status_code == 200
            after = client.get(base + "&limit=1500").get_json()
            assert target not in [f["id"] for f in after["files"]]
            assert after["total"] == ref.conn.execute(sql).fetchone()[0], collapse
            assert after["total"] == before["total"] - 1, collapse
    finally:
        ref.close()


def test_the_default_path_order_is_read_from_an_index(tmp_path):
    """The gallery and list sort by File path by default. SQLite uses an index on
    an expression only when the query spells it the same way, so the list's
    FILE_PATH_SQL and the indexes share one definition. Read in index order, a
    page stops after its rows instead of sorting every matching row first:
    0.2-0.4 s became under 0.01 s on about 394,000 rows."""
    from gleapp.db import FILE_PATH_SQL
    from gleapp.web.app import _col_sql

    assert _col_sql("file_path") == FILE_PATH_SQL
    case = open_case(tmp_path / "pcase", create=True, examiner="t")
    try:
        for where in ("kind != 'archive'", "kind != 'archive' AND category = 0",
                      "grp_head = 1 AND kind != 'archive'"):
            for d in ("ASC", "DESC"):
                plan = " ".join(str(r[-1]) for r in case.db.conn.execute(
                    f"EXPLAIN QUERY PLAN SELECT id FROM files WHERE {where} "
                    f"ORDER BY {FILE_PATH_SQL} {d}, id {d} LIMIT 1500"))
                assert "TEMP B-TREE" not in plan, (where, d, plan)
    finally:
        case.close()


def test_categorizing_a_collapsed_tile_categorizes_its_whole_group(tmp_path):
    """A collapsed gallery tile stands for its duplicate group. Categorizing only
    the file shown left its copies uncategorized, so the group came back under
    Uncategorized with another copy as its tile. with_group applies the category to
    every file in the group; without it (list view, a group view) only the file."""
    import json

    from gleapp.web.app import create_app

    case = _case_with_duplicates(tmp_path)
    root = case.root
    groups = {}
    for fid, key in case.db.conn.execute(f"SELECT id, {GRP} FROM files"):
        groups.setdefault(key, []).append(fid)
    multi = [sorted(m) for m in groups.values() if len(m) > 1]
    case.close()
    assert len(multi) >= 2, "the fixture must hold at least two duplicate groups"
    client = create_app(str(root)).test_client()
    ref = open_case(root).db
    cat_of = lambda fid: ref.conn.execute(
        "SELECT category FROM files WHERE id = ?", (fid,)).fetchone()[0]
    try:
        one, other = multi[0], multi[1]
        r = client.post("/api/categorize", json={"ids": [one[0]], "category": 5,
                                                  "with_group": True}).get_json()
        assert r["count"] == len(one) and r["tiles"] == 1
        assert all(cat_of(fid) == 5 for fid in one)
        # without it, just the one file
        client.post("/api/categorize", json={"ids": [other[0]], "category": 5})
        assert cat_of(other[0]) == 5
        assert all(cat_of(fid) != 5 for fid in other[1:])
        audit = [json.loads(a["detail"]) for a in client.get("/api/audit").get_json()
                 if a["action"] == "categorize"]
        grouped = next(d for d in audit if d.get("tiles") == 1)
        assert sorted(grouped["ids"]) == one
    finally:
        ref.close()
