"""A case read by several threads at once, each asking for its own rows.

Every thread of the app shares a case's one connection. From Python 3.12 the
sqlite3 module can hand two threads that run the same statement text the same
prepared statement, and each then reads the answer to the other's question: a
different row, no row, or an error. The cause is written up beside
``_SHARED_CONNECTION`` in ``gleapp/db.py``.

``build`` registers rows whose path and size both follow from their number, so
a row that answers for another is told apart from the one asked for. Run as a
script, this file opens the case and has several threads ask for different rows
while another thread keeps the connection busy with a long query, then prints
how many answers were right. ``test_shared_connection_reads.py`` runs it in a
child process under a timeout, so a run that never ends fails that test and
does not hold up the suite.
"""

from __future__ import annotations

import json
import sys
import threading

SIZE_BASE = 1000


def build(db, n: int) -> list[int]:
    """Register ``n`` rows and return their ids, in order."""
    ids = [db.upsert_file(f"/case/staged/f{i:06d}.jpg", rel_path=f"dcim/f{i:06d}.jpg",
                          source="ev", kind="image", ext=".jpg", size=SIZE_BASE + i)
           for i in range(n)]
    db.commit()
    return ids


def expected(i: int) -> tuple[str, int]:
    """The path and size ``build`` gave row number ``i``."""
    return f"/case/staged/f{i:06d}.jpg", SIZE_BASE + i


# The long query: one statement that keeps the connection for a few milliseconds
# on every SQLite (a join over the rows took a fraction of the time on SQLite
# 3.53.4 that it took on 3.43.1).
SLOW_STEPS = 30000
SLOW_SQL = ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < ?) "
            "SELECT COUNT(*) FROM n")


def main(argv: list[str]) -> int:
    """``sharedreads.py <case folder> <db|http> <threads> <rounds>``"""
    from gleapp.web.app import create_app          # pylint: disable=import-outside-toplevel
    case_dir, mode, threads, rounds = argv[0], argv[1], int(argv[2]), int(argv[3])
    app = create_app(None)
    opened = app.test_client().post("/api/case/open", json={"path": case_dir})
    assert opened.status_code == 200, opened.get_data(as_text=True)
    case = app.config["STATE"]["case"]
    ids = [r["id"] for r in case.db.conn.execute("SELECT id FROM files ORDER BY id").fetchall()]
    stop = threading.Event()
    # Set by a reader after every read. The long query waits for it before it
    # runs again, so it takes the connection once per read at most. Run back to
    # back it kept the readers off the connection on Linux, where a thread that
    # lets go of a mutex can take it again before one that was waiting for it:
    # a reader sat through up to 9 long queries for one read on a CI runner
    # (under 1 on macOS), and three runs in 161 were still going at 300 s with
    # every reader waiting for the connection and still advancing.
    read_done = threading.Event()
    read_done.set()
    count_lock = threading.Lock()
    counts = {"ok": 0, "wrong": 0, "none": 0, "error": 0, "slow": 0}
    errors: set[str] = set()

    def note(what: str, error: str | None = None) -> None:
        with count_lock:
            counts[what] += 1
            if error:
                errors.add(error)
        if what != "slow":
            read_done.set()

    def slow() -> None:
        # stands for a job, the indexer or a large count holding the connection
        while not stop.is_set():
            # the timeout only keeps this loop looking at ``stop``
            if not read_done.wait(0.1):
                continue
            read_done.clear()
            try:
                case.db.conn.execute(SLOW_SQL, (SLOW_STEPS,)).fetchone()
            except Exception:                       # pylint: disable=broad-exception-caught
                pass
            note("slow")

    def ask_db(k: int) -> None:
        for j in range(rounds):
            i = (j * 7 + k * 131) % len(ids)
            try:
                row = case.db.get_file(ids[i])
                if row is None:
                    note("none")
                elif (row["id"], row["path"], row["size"]) != (ids[i], *expected(i)):
                    note("wrong")
                else:
                    note("ok")
            except Exception as exc:                # pylint: disable=broad-exception-caught
                note("error", type(exc).__name__)

    def ask_http(k: int) -> None:
        client = app.test_client()
        for j in range(rounds):
            i = (j * 7 + k * 131) % len(ids)
            try:
                reply = client.get(f"/api/file/{ids[i]}")
                if reply.status_code == 404:
                    note("none")
                elif reply.status_code != 200:
                    note("error", f"HTTP {reply.status_code}")
                else:
                    got = reply.get_json()
                    if (got["id"], got["path"], got["size"]) != (ids[i], *expected(i)):
                        note("wrong")
                    else:
                        note("ok")
            except Exception as exc:                # pylint: disable=broad-exception-caught
                note("error", type(exc).__name__)

    ask = {"db": ask_db, "http": ask_http}[mode]
    busy = threading.Thread(target=slow, name="slow-query")
    askers = [threading.Thread(target=ask, args=(k,), name=f"asker-{k}")
              for k in range(threads)]
    busy.start()
    for t in askers:
        t.start()
    for t in askers:
        t.join()
    stop.set()
    busy.join()
    app.config["STATE"]["shutdown"]()
    print("RESULT " + json.dumps({**counts, "asked": threads * rounds,
                                  "errors": sorted(errors)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
