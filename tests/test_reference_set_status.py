"""A shared-store set's total is recorded only when its import finishes.

The entries are committed in batches as an import runs, and ``hashsets.count``
is written once, at the end (``hashstore._finalize``). A set whose import
stopped part-way (GLEAPP closed, an error) used to list as 0 beside the hashes
it holds; ``hashstore.sets()`` now says "importing" or "incomplete" instead.
"""
# pylint: disable=protected-access

from __future__ import annotations

import sqlite3

import pytest

from gleapp import hashstore
from gleapp.cli import main as cli_main


@pytest.fixture(autouse=True)
def _isolate(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GLEAPP_CONFIG_DIR", str(tmp_path_factory.mktemp("cfg")))
    hashstore.close()
    yield
    hashstore.close()
    hashstore._importing.clear()


def _rds(path, n):
    """A minimal stand-in for an NSRL RDSv3 db with ``n`` distinct MD5s."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE METADATA (metadata_id INTEGER PRIMARY KEY, "
               "file_name TEXT, bytes INTEGER, md5 TEXT)")
    db.executemany("INSERT INTO METADATA(file_name, bytes, md5) VALUES(?,?,?)",
                   [(f"f{i}", 10, f"{i + 1:032x}") for i in range(n)])
    db.commit()
    db.close()


def _only():
    sets = hashstore.sets()
    assert len(sets) == 1, sets
    return sets[0]


class _Stop(Exception):
    pass


def test_an_import_that_stops_part_way_reads_as_incomplete(tmp_path, monkeypatch):
    src = tmp_path / "RDS_test_ios.db"
    _rds(src, 6)
    monkeypatch.setattr(hashstore, "_BATCH", 2)

    def stop(_seen, added):
        if added >= 2:
            raise _Stop

    with pytest.raises(_Stop):
        hashstore.import_sqlite(src, name="ios", algos=("md5",), progress=stop)

    s = _only()
    assert s["status"] == "incomplete"
    assert s["count"] is None
    # what it holds is in the store, and still matched
    assert hashstore.algo_counts(s["id"]) == {"md5": 2}
    assert hashstore.lookup("md5", f"{1:032x}") is not None
    assert hashstore.summary()["entries"] == 0

    # adding the file again under the same name replaces it
    hashstore.import_sqlite(src, name="ios", algos=("md5",))
    s = _only()
    assert (s["status"], s["count"]) == ("complete", 6)


def test_a_set_reads_as_importing_while_it_is_imported(tmp_path, monkeypatch):
    src = tmp_path / "RDS_test_android.db"
    _rds(src, 6)
    monkeypatch.setattr(hashstore, "_BATCH", 2)
    seen = []
    hashstore.import_sqlite(src, name="android", algos=("md5",),
                            progress=lambda _s, _a: seen.append(_only()["status"]))
    # the last progress call reports the recorded total, after _finalize
    assert seen[:-1] and set(seen[:-1]) == {"importing"}
    assert seen[-1] == "complete"
    s = _only()
    assert (s["status"], s["count"]) == ("complete", 6)
    assert not hashstore._importing


def test_a_list_import_that_fails_part_way_leaves_nothing_behind():
    def entries():
        yield ("md5", "a" * 32, None)
        yield ("md5", "b" * 32, None)
        raise _Stop

    with pytest.raises(_Stop):
        hashstore._import_entries("vic", "vic.json", "known", entries())
    assert hashstore.sets() == []
    assert not hashstore._importing


def test_a_zero_an_earlier_version_left_reads_as_incomplete_only_with_entries():
    """Before count started as NULL, an unfinished import left the column's
    default, 0. A set with entries and a 0 is one of those; a 0 with nothing
    stored is a real 0."""
    c = hashstore.connect()
    c.execute("INSERT INTO hashsets(id, name, kind, count, imported_at) "
              "VALUES(1, 'left at 0', 'known-good', 0, 2), "
              "(2, 'empty', 'known-good', 0, 1)")
    c.execute("INSERT INTO hashset_entries(hashset_id, algo, value) "
              "VALUES(1, 'md5', ?)", ("c" * 32,))
    c.commit()
    status = {s["name"]: s["status"] for s in hashstore.sets()}
    assert status == {"left at 0": "incomplete", "empty": "complete"}


class _AnalyzeFails:
    """The store's connection, with ANALYZE failing (a full disk, say)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, *args):
        if sql.strip().upper() == "ANALYZE":
            raise sqlite3.OperationalError("database or disk is full")
        return self._real.execute(sql, *args)


def test_a_failed_analyze_after_every_entry_is_stored_does_not_cost_the_count(tmp_path):
    src = tmp_path / "RDS_test_legacy.db"
    _rds(src, 3)
    hashstore._conn = _AnalyzeFails(hashstore.connect())
    _hs, n = hashstore.import_sqlite(src, name="legacy", algos=("md5",))
    assert n == 3
    s = _only()
    assert (s["status"], s["count"]) == ("complete", 3)


def test_the_command_line_lists_an_incomplete_set_as_such(tmp_path, monkeypatch, capsys):
    src = tmp_path / "RDS_test_ios.db"
    _rds(src, 6)
    monkeypatch.setattr(hashstore, "_BATCH", 2)

    def stop(_seen, added):
        if added >= 2:
            raise _Stop

    with pytest.raises(_Stop):
        hashstore.import_sqlite(src, name="ios", algos=("md5",), progress=stop)
    assert cli_main(["hashset", "--list"]) == 0
    out = capsys.readouterr().out
    assert "ios  incomplete: the import did not finish" in out
