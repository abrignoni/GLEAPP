"""A hash list import commits inside the case lock.

Every thread shares a case's one connection. A commit made outside ``CaseDB.lock`` can
have another thread's commit land inside it, and it then raises "cannot commit - no
transaction is active" although the rows are stored. Measured 2026-10-10 on Python
3.10.20 with a second thread calling ``CaseDB.commit()``: 11 of 6,000 imports raised
before the import's commit took the lock, none of 6,000 after.
"""

from __future__ import annotations

from gleapp.case import open_case


def test_a_hash_list_import_commits_inside_the_case_lock(tmp_path):
    c = open_case(tmp_path / "case", create=True, examiner="t")
    conn, lock, held = c.db.conn, c.db.lock, []

    class Conn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            held.append(lock._is_owned())  # pylint: disable=protected-access
            conn.commit()

    hs = c.db.create_hashset("s", source="x", kind="known")
    c.db.conn = Conn()
    try:
        c.db.add_hashset_entries(hs, [("md5", "0" * 31 + "1", 1)])
        stored = conn.execute("SELECT COUNT(*) FROM hashset_entries").fetchone()[0]
    finally:
        c.db.conn = conn
        c.close()
    assert stored == 1
    assert held and all(held)
