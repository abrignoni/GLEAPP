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
